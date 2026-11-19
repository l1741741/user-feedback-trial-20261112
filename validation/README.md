# Offline validation for keyspace isolation

These scripts exercise the opt-in keyspace isolation mode entirely through
Huey's public APIs. They are kept separate from the package unit tests
(`huey/tests/test_keyspace_isolation.py`) because they span multiple
processes and on-disk SQLite databases.

Run them with the repository interpreter and isolated mode (`-I`), from the
repository root:

```
.venv/bin/python -I validation/scripts/run_e2e.py
```

## What is covered

* `run_e2e.py` - end-to-end checks for both `MemoryHuey` and `SqliteHuey`:
  the identical literal used as business key, result id, single-task and
  whole-class revoke id, lock name and rate-limit name; two queues sharing
  one SQLite file; a database with legacy unmarked rows and both
  `legacy_policy` values; rejection of storage backends that do not support
  isolation; and a default-mode regression check.
* `run_multiprocess.py` / `mp_worker.py` - the persistence contract across
  real process restarts. Each phase (`write`, `verify`, `mutate`) runs in a
  fresh `python -I` subprocess with a new Huey instance and a new SQLite
  connection, proving control-state survives restarts and that a second
  queue on the same file stays isolated.

Temporary databases are created in a fresh OS temp directory per run, never
next to the scripts, and are left to the OS to clean up.
