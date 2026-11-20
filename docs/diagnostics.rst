.. _diagnostics:

Diagnostics
===========

Huey ships a read-only diagnostic tool for :py:class:`SqliteHuey` databases,
usable both as a command-line program and as a Python API. It is designed
for troubleshooting a live queue: it never claims a task, deletes a result,
consumes a ``revoke_once`` flag or cleans up a lock, and it never creates a
missing database or missing tables.

.. code-block:: console

    $ huey_diag /var/run/myapp/huey.db --queue myapp
    huey sqlite diagnostics (report v1)
    database: /var/run/myapp/huey.db (36.0 KiB, journal=wal)
    queue:    myapp
    status:   ok
    snapshot: consistent (single-read-transaction)
    pending: 3 (showing 3 from offset 0)
         0 row=12     prio=3.0      133 B  myapp.add [a0a18cbc-...]
    ...

Consistency
-----------

Every report is collected inside a **single read transaction**, so the
counts and the listed entries always describe the same point in time, even
while consumers are concurrently enqueueing, dequeueing and storing
results. You will never see "0 pending" alongside a list of three tasks.
Pagination (``--limit`` / ``--offset``) shares the snapshot of its own
invocation; separate invocations are separate snapshots. Entry ordering is
stable (priority desc/rowid for the queue, timestamp/rowid for the
schedule, key for results), so pages tile predictably.

Safety
------

* The database is opened read-only (``mode=ro`` + ``query_only``); a
  missing database is reported as ``missing`` and never created, and a
  database without huey tables is reported as ``no-schema`` rather than
  being silently initialized.
* Task messages are **never unpickled**. Task ids and names are recovered
  by scanning pickle opcodes, which executes nothing. Undecodable messages
  are reported with a category (``decompress``, ``pickle-parse``,
  ``too-large``, ``layout``) and a byte offset.
* Task arguments and result values are never displayed; entries show the
  task id, task name, and byte sizes only. Output is bounded by
  ``--limit`` (default 20 entries per section), and oversized messages are
  reported by size instead of being decoded.

Machine-readable output
-----------------------

``--json`` emits a report with stable field names (``version``, ``status``,
``detail``, ``warnings``, ``database``, ``queue``, ``snapshot``,
``summary``, ``pending``, ``scheduled``, ``results``, ``control``),
suitable for scripting and monitoring:

.. code-block:: console

    $ huey_diag huey.db --json | jq .summary
    {
      "pending": 3,
      "scheduled": 1,
      "results": 5,
      "revocations": 1,
      "locks": 0,
      "counters": 2
    }

The ``status`` field is one of:

* ``ok`` - report collected successfully.
* ``missing`` - database file does not exist (exit code 2).
* ``locked`` - database stayed locked by a writer past ``--timeout``
  (exit code 3).
* ``no-schema`` - the file is a database but has no huey tables
  (exit code 4).
* ``legacy-schema`` - tables exist with an older layout; counts are
  reported unfiltered by queue and flagged as such.
* ``unreadable`` - the file could not be read as a sqlite database
  (exit code 5).

A database that is replaced or removed during the inspection is flagged in
``warnings``. When several huey queues share one database file, only the
queue selected with ``--queue`` is shown.

Python API
----------

The same report is available programmatically, which works for any huey
sqlite database regardless of the application version that wrote it:

.. code-block:: python

    from huey.diagnostics import format_report, inspect_database

    report = inspect_database('/var/run/myapp/huey.db', queue='myapp',
                              limit=20)
    print(format_report(report))

Command-line options
--------------------

``database``
    Path to the sqlite database file (required, positional).
``-q``, ``--queue``
    Queue name to inspect (default: ``huey``).
``-n``, ``--limit``
    Maximum entries listed per section (default: 20). Counts always cover
    the whole queue regardless of the limit.
``--offset``
    Skip this many entries before listing (stable ordering).
``--timeout``
    Seconds to wait on a locked database before reporting it as ``locked``
    (default: 5.0).
``--json``
    Emit the machine-readable report instead of text.
