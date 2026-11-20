.. _poison-messages:

Quarantining Poison Messages
============================

A rolling deploy can leave messages in the queue produced by a version of the
code that no longer matches the consumer: a task function may have been renamed
or removed, or the message payload may be corrupted. Ordinarily Huey logs an
error and discards the message as it is read from the queue, which means the
original bytes are gone by the time anyone investigates.

For the SQLite backend you can opt in to **quarantine**: unreadable messages
are moved to a persistent table where they can be inspected, replayed or
discarded later. The feature is disabled by default and the default behavior
is unchanged.

.. code-block:: python

    from huey import SqliteHuey

    huey = SqliteHuey('my-app', filename='huey.db', quarantine=True)

.. note::
    ``quarantine=True`` is only supported by :py:class:`SqliteHuey` (and
    :py:class:`CySqliteHuey`, which shares the same storage). Passing it with
    another backend raises :py:class:`ConfigurationError`.

What gets quarantined
---------------------

A message is quarantined, rather than dropped, when it cannot be decoded or
accepted:

* it cannot be deserialized (including a failed signature check with a
  :py:class:`~huey.serializer.SignedSerializer`);
* its task name is not in the consumer's registry;
* its structure is invalid (not a task message, wrong field types, and so on).

In every case the consumer keeps going: the following valid messages are
processed normally. This applies to both the normal queue and messages read
from the schedule, including a whole batch of due scheduled tasks, so one bad
message does not prevent its neighbours from running.

Each quarantine record stores:

* the original message bytes (``data``);
* the queue name and the source (``queue`` or ``schedule``);
* an error classification (``deserialize-error``, ``signature-invalid``,
  ``unregistered-task`` or ``invalid-message``) and a short, safe description;
* the task id, task name, eta and priority, when they can be recovered by
  inspecting the pickle opcodes **without executing the pickle**.

Task arguments and keyword arguments are never read for this purpose. For
messages that could be decoded (for example an unregistered task), the
metadata comes from the decoded message object. For messages that could not be
decoded at all, only the leading opcodes are inspected, so a message containing
a malicious ``__reduce__`` is never executed while being described.

The raw bytes are preserved for forensics, but are only returned by
:py:meth:`Huey.get_quarantined`; list operations and the command-line output
omit them, so arguments and signature material are not printed.

Durability
----------

Moving a message into quarantine happens in a single SQLite transaction: the
insert into the quarantine table and the delete from the task (or schedule)
table either both commit or neither does. Consequently:

* if quarantine storage fails (for example the database is read-only, or the
  disk is full), the message is left in the queue and ``dequeue()`` raises, so
  the worker logs the error, backs off and retries -- nothing is lost;
* if the process is killed mid-move, SQLite rolls the transaction back and the
  message remains in place;
* with several consumers running, only the consumer that wins the delete
  writes a quarantine record, so a message produces exactly one record.

.. note::
    Quarantine covers messages that were successfully read from the queue but
    could not be decoded. A crash between claiming a valid message and running
    it is the normal at-most-once behavior of Huey's queue. If you need
    at-least-once delivery for valid messages, use a lease/acknowledgement
    consumer mode if your storage backend provides one.

Inspecting quarantine
----------------------

From Python:

.. code-block:: python

    records = huey.quarantined(limit=20)
    for record in records:
        print(record)  # id, queue, source, class, task name/id, size, time.

    record = huey.get_quarantined(records[0].id)
    record.data    # The original, raw message bytes.
    huey.quarantine_count()

From the terminal (the console script is installed with Huey, or use the
equivalent ``python -m huey.bin.huey_quarantine``):

.. code-block:: shell

    huey_quarantine my_app.huey list
    huey_quarantine my_app.huey show 17
    huey_quarantine my_app.huey replay 17
    huey_quarantine my_app.huey discard 17

The command-line output is deliberately compact and never includes the raw
message, task arguments or signature data.

Replaying and discarding
------------------------

Replay is always explicit:

.. code-block:: python

    result = huey.replay_quarantined(record_id)
    if result:
        print('replayed to', result.destination)
    else:
        print('still unreadable:', result.error)

The message is decoded and validated first, then re-enqueued; only after that
succeeds is the quarantine record removed. If the message still cannot be
decoded (for example the task is still not registered), nothing changes: the
record stays in quarantine and a failed result is returned. Replaying a bad
record can therefore never create an endless quarantine/replay loop.

When replaying, the original queue, task id, eta and priority are preserved
where they can be determined reliably. A message whose eta is still in the
future is restored to the schedule; otherwise it goes back onto the queue.

To drop a record without replaying it:

.. code-block:: python

    huey.discard_quarantined(record_id)

Logging
-------

Quarantining a message logs a single warning with the source, error class and
(when known) the task name and id -- never the task arguments or the message
bytes:

.. code-block:: text

    Quarantined unreadable message (source=queue, class=unregistered-task,
    task=my_app.send_invoice, id=2f9c...).

When quarantine storage itself fails, the exception is logged by the worker and
the message is left in place for a later attempt.
