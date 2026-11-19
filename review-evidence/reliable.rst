.. _reliable-consumption:

Reliable consumption (leases and acknowledgements)
==================================================

By default, a Huey worker **removes** a task from the queue as soon as it
picks it up. If the consumer process dies while that task is running -- killed
by the OOM killer, restarted by a supervisor, or interrupted by a signal -- the
task is gone and never runs again. For short, cheap tasks this is a reasonable
trade-off. For long local archive jobs it is not.

The *reliable* consumer mode changes this for SQLite backends. It is disabled
by default and must be opted into explicitly:

.. code-block:: python

    from huey import SqliteHuey

    huey = SqliteHuey('archives', filename='/srv/app/archives.db')

    @huey.task(retries=3, retry_delay=30)
    def archive_batch(batch_id):
        ...

.. code-block:: shell

    huey_consumer myapp.huey --reliable --lease-seconds=60

or programmatically:

.. code-block:: python

    consumer = huey.create_consumer(reliable=True, lease_seconds=60)
    consumer.run()

What the mode guarantees
------------------------

Reliable mode provides **recoverable at-least-once delivery**. It does not,
and cannot, provide exactly-once execution. Instead:

* A task is *claimed* into a lease before it runs. The task row moves from the
  queue to a ``lease`` table, but it is not deleted.
* The delivery is *acknowledged* (deleted) only after the task has finished and
  its result / follow-up work has been written. If the process dies first, the
  delivery survives.
* Each claim issues a new opaque *ticket*. Acknowledging, renewing, or
  replacing a delivery is only possible while the caller still holds the
  current ticket.
* A task that runs longer than its lease is renewed automatically in the
  background, so a healthy consumer keeps ownership of a slow task.
* If a consumer disappears, its deliveries become claimable again once their
  lease expires. Another consumer then takes them over.
* A stale consumer that comes back after its lease expired can *not* ack,
  renew, or delete the delivery that was taken over. Its acknowledgement is a
  no-op.
* Acknowledging the same delivery more than once is a no-op after the first
  time.

Supported backends
------------------

Only the SQLite backends implement the leased-delivery API:

* :py:class:`~huey.SqliteHuey` (``huey_consumer`` with ``--reliable``)
* :py:class:`~huey.CySqliteHuey`

Combining ``reliable=True`` with any other storage backend raises
:py:class:`~huey.exceptions.ConfigurationError` **before consumption starts**,
rather than silently falling back to at-most-once delivery. The ``greenlet``
worker type is also rejected: lease renewal runs in a background thread, which
does not work reliably under cooperative greenlets. The supported worker types
are ``thread`` and ``process``.

Lease duration and renewal
--------------------------

``lease_seconds`` (CLI ``--lease-seconds``, default 60) is how long a claim is
valid. While a task runs, the consumer renews the lease every third of that
interval. This means:

* A task that takes longer than ``lease_seconds`` keeps running and keeps its
  ownership, because the expiry keeps moving forward.
* If the consumer dies, the last renewal is the starting point. The orphaned
  delivery is recovered by another consumer at most about ``lease_seconds``
  after the death (not after the task started).

Use a lease that is comfortably longer than the time needed to *start* the
renewal loop and reach the database, but short enough that recovery is timely.
A few tens of seconds is typical. Do not use very small values in production;
the consumer enforces a minimum of 0.5s.

ETA, retries, priority, revocation, and chains
----------------------------------------------

Reliable mode preserves the existing semantics of these features:

``ETA`` and ``schedule``
    A claimed task that is not yet ready to run is moved from its lease into
    the schedule in a single transaction. It is neither acknowledged early nor
    left in the lease table.

``retries`` and ``retry_delay``
    When a task fails with retries remaining, the current delivery is replaced
    by the follow-up message in one transaction: either back on the queue
    (immediate retry), or on the schedule (delayed / backoff retry). There is
    no point at which the old delivery is gone but the retry has not been
    persisted, and the old delivery is never kept alongside the retry, so a
    failing task does not spawn duplicate retries. On final failure the
    delivery is acknowledged and the normal error handling runs.

``priority``
    Claims select by priority, then age, then lease expiry, exactly as the
    normal dequeue path does. Requeued retries keep the task's priority.
    Expired deliveries are recovered before fresh work, so a task interrupted
    by a dead consumer is not starved behind a backlog; among expired
    deliveries, priority still decides order.

revocation and expiration
    A claimed task that is revoked or expired is acknowledged and discarded, as
    in the default mode, so it does not linger in the lease table.

``tasks, chains, groups and chords``
    The follow-up task in a chain, chord callbacks, and ``on_error`` handlers
    are enqueued normally before the delivery is acknowledged, so an
    interruption after the work is done but before the ack replays the task
    (see below).

Shutdown behavior
-----------------

Graceful shutdown (``SIGINT`` by default, or ``SIGTERM`` with
``--graceful-signal=TERM``)
    Workers stop claiming new tasks and finish what they are running. Finished
    deliveries are acknowledged. Any delivery still unfinished when
    ``--shutdown-timeout`` elapses is *released*, not acknowledged, and is
    recovered once its lease expires. The queued work that was never claimed
    stays on the queue.

Forced shutdown (``SIGTERM`` by default, or ``SIGINT`` with
``--graceful-signal=TERM``)
    Running tasks are interrupted. Their deliveries are left in the lease
    table without an acknowledgement, so there is a durable record to recover
    from. A subsequent consumer takes them over after the lease expires.

Explicit kill (``SIGKILL``, OOM killer, power loss)
    Same as forced shutdown: the lease row for any in-flight delivery remains
    in the database and is recovered after its lease expires.

In every shutdown path, a delivery that was never acknowledged is eventually
re-delivered. That is the point of the mode.

A delivery abandoned during a forced shutdown stops renewing, but if the task
body finishes anyway (a thread that could not be interrupted, for example), its
acknowledgement is still attempted. If no other consumer has taken the delivery
over in the meantime, the ticket is still current and the ack succeeds, so the
completed task is not run a second time. Once another consumer has taken it
over, the abandoned consumer's ack is fenced and ignored.

Legitimate duplicate execution
------------------------------

At-least-once means duplicates are possible. These are all correct outcomes of
the design, not bugs:

* **Death after the work, before the ack.** The task ran (and may have written
  its output) but the process died before the delivery was acknowledged. The
  delivery is recovered and the task runs again. This is the most common
  duplicate.
* **Lease expiry while the task is still running.** If the consumer is alive
  but unable to renew (e.g. the whole process is stopped by ``SIGSTOP`` for
  longer than the lease, or the database is unreachable for that long), another
  consumer may take the delivery over. Both may then run the task. The original
  consumer is fenced from acking, but the task body has already run.
* **Retry after an interruption.** A task interrupted by a forced shutdown is
  re-delivered, and the retry attempt counts as another execution.

Because of this, task functions used with reliable mode should be
**idempotent**, or should be paired with an application-level dedupe key (for
example, storing the archive under a deterministic filename and replacing it
atomically). Reliable mode removes the "silently lost forever" failure mode; it
does not make an arbitrary function safe to run twice.

Inspecting in-flight work
-------------------------

The number of claimed-but-unacknowledged deliveries is available for
monitoring:

.. code-block:: pycon

    >>> huey.in_flight_count()
    1

A non-zero value during steady state means tasks are running. A value that does
not return to zero after a consumer has stopped can indicate leases waiting to
expire (recovery is pending), which is expected after an abrupt shutdown.

Limitations
-----------

* Reliable mode is SQLite-only. Redis, Postgres, File and Memory backends are
  rejected up front.
* It requires the ``thread`` or ``process`` worker type.
* SQLite's write lock serializes claims and acknowledgements with other writes.
  This is fine for local archive workloads, but it is not designed for many
  workers contending aggressively. Use a small number of workers.
* Leases cover *claimed deliveries*. Promoting a due task from the schedule into
  the queue keeps the scheduler's pre-existing behavior, so a crash in the
  scheduler between reading the schedule and re-enqueueing is still a narrow
  at-most-once window. The lease handoff itself (including delayed retries) is
  atomic: a delayed retry is persisted in the schedule in the same transaction
  that removes the old delivery.
