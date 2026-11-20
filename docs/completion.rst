.. _completion-events:

Completion Events
=================

Signals are process-local: a receiver connected with :py:meth:`Huey.signal`
only sees events emitted while *its* process is running. If the consumer exits
before another process subscribes, the notification is gone. That is fine for
logging and metrics, but it cannot drive a downstream system that must be told
about every finished task.

:py:class:`SqliteHuey` offers an optional durable completion-event outbox for
this case. When ``completion_events=True``, terminal task outcomes are written
to the same SQLite database as the task result, in the same transaction. A
separate local receiver process then *pulls* events and acknowledges them.
No ticket service or message broker is required.

.. code-block:: python

    from huey import SqliteHuey

    huey = SqliteHuey('orders', filename='huey.db', completion_events=True)

    @huey.task()
    def charge(order_id):
        return gateway.charge(order_id)

Then, in a separate process (or a test harness):

.. code-block:: python

    from huey.completion import LocalCompletionReceiver

    receiver = LocalCompletionReceiver(huey, lease_seconds=30)

    def handle(event):
        # Stable id -> safe to de-duplicate on redelivery.
        ticket_system.upsert(event.event_id, event.data)

    receiver.run(handle, interval=1.0)

A ready-to-use process is also available:

.. code-block:: bash

    python -m huey.completion_receiver app.queue.huey_instance --once
    python -m huey.completion_receiver app.queue.huey_instance --drain
    python -m huey.completion_receiver app.queue.huey_instance   # poll forever

It prints one JSON object per line to stdout and acknowledges after printing.

Why an outbox instead of signals
--------------------------------

Signals are still emitted exactly as before; enabling completion events does
not change or suppress them. The outbox adds a second, durable path:

* The event is written in the same transaction as the task result, so a process
  that exits between the two cannot leave a result without an event or an
  event without a result.
* Events survive consumer restarts and can be replayed by a receiver.
* Delivery is decoupled from the consumer: a slow or failed receiver blocks
  neither task execution nor the consumer.

Event contract
--------------

Every event is a versioned JSON payload with these fields:

``version``
    Payload schema version (currently ``1``). Consumers should reject or
    explicitly handle unknown versions.
``status``
    One of the statuses below.
``final``
    ``true`` only for terminal statuses.
``task_id``, ``task_name``, ``occurred_at``, ``retries``
    Identification and timing. ``task_name`` is the registry name
    (``module.ClassName``) and may be empty for events backfilled by
    :py:meth:`Huey.reconcile_completion_events`.
``error``
    Optional. The exception *type* only (for example ``ValueError``). The
    message, traceback and task arguments are never included, so credentials
    passed to tasks are not copied into the outbox.
``truncated``
    Present and ``true`` if optional fields were dropped to satisfy the size
    bound.

Statuses
~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 20 12 45

   * - Status
     - Final
     - Meaning
   * - ``succeeded``
     - yes
     - Task returned normally. The result is stored (unless ``None`` and
       ``store_none=False``) atomically with this event.
   * - ``failed``
     - yes
     - Task raised an unhandled exception and has no retries remaining. The
       error result is stored atomically with this event.
   * - ``canceled``
     - yes
     - Task raised :py:class:`CancelExecution` and will not be retried
       (either in a ``pre_execute`` hook or inside the task).
   * - ``revoked``
     - yes
     - Task was revoked and was not executed. No result is stored.
   * - ``expired``
     - yes
     - Task's expiration time had passed, so it was not executed. No result
       is stored.
   * - ``retrying``
     - no
     - The attempt failed but will be retried. This is an observation, not a
       completion: it never counts as a final outcome. Retry-only exceptions
       (:py:class:`RetryTask`, rate limits, lock contention) also surface as
       ``retrying``.

A final failure is therefore always distinguishable from a retry in progress,
and a task with retries produces one or more ``retrying`` events followed by
exactly one terminal event.

.. note::
    Periodic tasks do not write results (they run on a schedule forever), and
    for the same reason they do not write completion events. Use signals or
    the :ref:`stats <monitoring>` recorder to observe periodic executions.

Delivery semantics
------------------

**At-least-once.** A receiver pulls a batch, which *leases* each event to a
random token for ``lease_seconds``. The event must be acknowledged with that
token:

.. code-block:: python

    events = huey.pull_completion_events(limit=10, lease_seconds=30,
                                         owner='worker-1')
    for event in events:
        handle(event)
        if not huey.ack_completion_event(event.event_id, event.lease_token):
            # Our lease expired and another receiver took the event over.
            # Do not treat this as a failure; de-duplicate on event_id.
            pass

* If the process exits before acknowledging, the lease expires and the event
  is redelivered. De-duplicate on the stable ``event_id``.
* Acknowledgement is scoped to the *current* lease token. A late
  acknowledgement from a receiver whose lease was taken over fails, so it can
  never delete an event another receiver now owns.
* Acknowledgement does **not** delete the event. Acknowledged events are kept
  so late/duplicate acknowledgements stay idempotent, and are removed only by
  retention cleanup (below).

**Stable event ids.** Terminal events derive their id deterministically from
the queue, task id and status. A task re-delivered after a crash -- or an event
backfilled by reconciliation -- reuses the same id, so duplicate rows are not
created. ``retrying`` observations use a random suffix because several attempts
can legitimately share the same retry count.

Failure behaviour
-----------------

The outbox is deliberately isolated from task execution:

* If writing the event fails, the task is **not** re-executed. The result is
  still written (a plain fallback write), the error is logged, and the consumer
  continues. The consumer is never blocked indefinitely by a failing outlet.
  The execute path also parks the missing event in a *pending* row (written in
  the same transaction as the fallback result), so nothing is silently lost.
* :py:meth:`Huey.reconcile_completion_events` materializes pending events. It
  also has a last-resort scan of UUID-keyed result rows that have neither an
  event nor a pending row. It reads results non-destructively and never
  executes a task. ``pull_completion_events()`` drains pending rows
  automatically, so a receiver repairs them without operator intervention.
* A receiver that crashes or raises from its handler leaves the event
  unacknowledged; it is redelivered after the lease expires.

.. note::
    As with Huey generally, a consumer killed *while a task function is
    running* (after ``dequeue`` removed the message) cannot record an outcome
    for that execution -- there is nothing to record yet. The outbox guarantee
    covers the window *after* the task returns: result, event, delivery and
    acknowledgement are consistent and recoverable across a process exit.

Retention and isolation
-----------------------

.. code-block:: python

    # Delete acknowledged events older than 7 days. Unacknowledged (including
    # leased) events are never deleted, however old.
    huey.purge_completion_events(retention_seconds=7 * 86400)

Events are scoped to the queue name, so multiple queues sharing one SQLite
file never see or delete each other's events. ``Huey.flush()`` also clears the
current queue's outbox.

.. note::
    Completion events are opt-in (``completion_events=True``) and currently
    supported by :py:class:`SqliteHuey` and :py:class:`CySqliteHuey`. Enabling
    them on another backend raises :py:class:`ConfigurationError`.
