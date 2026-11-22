.. _shard_migration:

SQLite Shard Migration
======================

When a deployment backed by the SQL/SQLite storage backend is resized, the
pending work of one business group (e.g. an order group) may need to move to a
separate SQLite database while the other groups keep being consumed from the
source. The optional :py:class:`~huey.contrib.migrate.ShardMigrator` toolkit
performs that move without changing producers or consumers.

The module is never imported by Huey itself: an operator starts a migration
explicitly through the Python API or the ``python -m huey.bin.migrate_shard``
command.

Supported producer versions
---------------------------

Queue and schedule rows store the pickle of a
:py:class:`huey.registry.Message` namedtuple. That wire format was introduced
in Huey 2.0.0 and has only been extended by appending fields with defaults, so
messages from any 2.x/3.x release deserialize on the current runtime. The
relational layout of the ``task``, ``schedule``, ``kv`` and ``counter`` tables
has been stable since the SqlStorage backend shipped.

Messages enqueued by **Huey >= 2.0.0, < 4.0.0** are supported (the exact spec
is exported as ``huey.contrib.migrate.SUPPORTED_PRODUCER_SPEC``). Both Huey
instances must additionally:

* use identical Serializer settings (compression, and for
  ``SignedSerializer`` the same secret and salt);
* use the same queue name.

A serializer round-trip self-check runs before every migration pass.

What is preserved
-----------------

* **Relative delivery order (FIFO).** Selected rows are applied to the
  destination shard in the same ``priority DESC, id ASC`` dequeue order the
  storage uses.
* **Planned time.** Schedule rows keep their original ``timestamp``/ETA.
* **Business identity.** Idempotency and conflict detection key off the
  producer-assigned task UUID inside the message, not the per-database
  autoincrement row id.
* **Persistent progress.** Resume state lives in a
  ``huey_migration_progress`` table in the destination database, keyed by
  ``(migration_id, kind)``. An interrupted run resumes from the persisted
  high-watermark; re-running a finished migration creates nothing new.

Selecting the migrating group
------------------------------

A ``selector(message) -> bool`` callable receives each decoded
:py:class:`~huey.registry.Message` and decides whether it belongs to the
migrating group. Only selected rows move; everything else stays in the source
and keeps being consumed. KV rows (result blobs, revoke markers) are filtered
separately through an optional ``key_selector``; counters are always carried
over.

A helper for the common "group id in task kwargs" case is provided::

    from huey.contrib.migrate import message_group_selector
    selector = message_group_selector('group', 'A')

Conflict handling
-----------------

If the destination already holds a message with the same task UUID:

* identical payload -> the insert is skipped, the source row is deleted and
  the conflict is counted;
* different payload -> :py:class:`~huey.contrib.migrate.MigrationError` is
  raised and the source row is left untouched.

Writes are ordered destination-first, source-delete-second inside per-batch
``BEGIN IMMEDIATE`` transactions, so a crash at any point is repaired by simply
re-running the same ``migration_id``.

Example
-------

A complete two-database, two-consumer demo ships in
``examples/shard_migration`` (see its README). The equivalent library call is::

    from huey.contrib.migrate import ShardMigrator

    migrator = ShardMigrator(
        source_huey.storage, target_huey.storage,
        selector, migration_id='orders-group-A-to-shard2',
        serializer=source_huey.serializer,
        batch_size=200)
    stats = migrator.migrate(max_batches=None)  # interruptible, re-runnable
