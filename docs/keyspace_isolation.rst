# Keyspace isolation

## Why it exists

By default every key/value that Huey stores shares one keyspace:

* task results (keyed by task id),
* data written by application code through ``Huey.put()`` / ``get()`` /
  ``delete()``,
* internal control-state: single-task and whole-class revocation flags,
  task locks (``Huey.lock_task``) and rate-limit window markers.

If an application lets callers choose their own task ids, or stores its own
auxiliary data with ``Huey.put``, a business key can be identical to an
internal key. The two writes then overwrite each other, reading a result can
return control-state (or vice versa), and ``flush_results()`` silently clears
locks, revocations and rate-limit windows.

``keyspace_isolation`` separates these into three logical keyspaces:

1. **business** - user data written via ``Huey.put/get/delete`` and
   ``put_if_empty``;
2. **result** - task results, ``Result`` / ``ResultGroup`` reads and chord
   member values;
3. **control** - revocation flags, task locks and rate-limit window markers.

Counters (rate-limit counters and chord counters) already live in their own
physical table/dictionary and remain separate from all three keyspaces.

The mode is **off by default**; enabling it never changes behavior when it is
off. It is implemented for ``MemoryHuey`` and ``SqliteHuey``.

## Enabling

```python
from huey import MemoryHuey, SqliteHuey

huey = MemoryHuey('my-app', keyspace_isolation=True)
huey = SqliteHuey('my-app', filename='huey.db', keyspace_isolation=True)
```

When enabled, the same literal string used as a result id, business key,
single-task revoke id, class revoke suffix, lock name or rate-limit name
resolves to an independent entry in its own keyspace.

* ``Huey.get/delete`` and ``Huey.result`` can never read or remove
  control-state, and cannot see each other's plane.
* ``storage.flush_results()`` only removes the result keyspace; locks,
  revocations, rate-limit windows and business data survive.
* ``storage.flush_control()`` only removes the control keyspace.
* ``storage.flush_business()`` only removes user data.
* ``Huey.flush()`` / ``storage.flush_all()`` still clear every keyspace.
* Two queues configured with different ``name`` values but sharing one SQLite
  file stay isolated: every query is scoped by both the queue name and the
  keyspace marker, so flushing one queue never touches another.

## How it is implemented

* ``MemoryStorage`` keeps three dictionaries (``_business``, ``_results``,
  ``_control``) plus the existing independent ``_counters`` dictionary. In
  legacy mode only ``_results`` is used, exactly as before.
* ``SqliteStorage`` keeps the existing schema and encodes the keyspace in the
  key itself, using a ``0x01`` control-byte prefix (``\x01b:`` business,
  ``\x01r:`` result, ``\x01c:`` control). Every read, delete, batch lookup
  (``peek_many``), size/introspection query and flush carries the same marker
  - there is no code path that falls back to the unmarked key. A NUL byte
  cannot be used as the marker because SQLite's ``LIKE`` pattern matching
  treats embedded NUL bytes as a C-string terminator.

## Existing databases: migration and compatibility

A database created before isolation was enabled contains **unmarked** rows.
Their ownership (business value, task result, or internal control-state)
cannot be determined from the data itself, so an isolated storage never
silently adopts them as control-state and never deletes them.

When an isolated ``SqliteHuey`` opens a database that contains unmarked rows
for its queue, the ``legacy_policy`` parameter decides what happens:

### ``legacy_policy="error"`` (default)

A ``huey.exceptions.ConfigurationError`` is raised and the database file is
left completely untouched. Resolve it in one of these ways:

* inspect the rows and delete or keep them manually, then re-open;
* re-open with ``legacy_policy="quarantine"``;
* keep that database on the default (non-isolated) mode and start a fresh
  database for isolated workloads.

### ``legacy_policy="quarantine"``

All ambiguous, unmarked rows for the queue are copied **verbatim** into a
``kv_legacy_quarantine`` table (preserving queue, key, value and a
quarantine timestamp) and then removed from the active ``kv`` table. Nothing
is deleted and nothing is reinterpreted. The isolated store starts empty and
subsequent opens are clean; the quarantine table is retained for as long as
you need it. The number of quarantined rows is available through
``storage.quarantine_info()``.

Rows already carrying a keyspace marker (written by a previous isolated run)
are never treated as legacy. Only rows for the opening queue name are
examined; other queues' rows in the same file are unaffected.

## Conflict and error boundaries

* Enabling isolation on a backend without ``supports_keyspace_isolation``
  (Redis, Postgres, file storage, BlackHole, or any custom storage that does
  not opt in) raises ``ConfigurationError`` while constructing the Huey
  instance, before any task or control-state is written.
* Custom storage classes written against the older storage signature keep
  working with isolation disabled; the new keyword is only forwarded to
  constructors that accept it.
* An invalid ``legacy_policy`` value raises ``ConfigurationError`` at
  construction time.
* Keys beginning with the ``0x01`` marker byte are still stored safely: the
  marker is applied on top of the caller's key, so a business key cannot
  spoof an entry in the result or control keyspace. Application code that
  needs to interoperate with the raw ``kv`` table should treat keys starting
  with ``0x01`` as reserved.

## Rollback limitations

* Isolation is a one-way data-layout change for a given SQLite database: once
  marked rows exist, opening the database with isolation disabled will not
  remove or understand the markers. Roll back by restoring a backup taken
  before enabling isolation, or by exporting values through the public APIs
  and re-importing them into a fresh database.
* Quarantined rows are preserved unchanged and can be inspected or moved
  manually, but they are not automatically re-imported into a keyspace.
* The in-memory backend has no persistence, so its rollback is simply
  recreating the Huey instance with isolation disabled.
