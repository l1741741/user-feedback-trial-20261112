# Renaming a task without dropping queued messages

This example shows a real producer/consumer split against a single SQLite
database:

* `tasks_old.py` -- the publisher deployed **before** the rename. It only
  knows the old task name `tasks_old.generate_report`.
* `tasks_new.py` -- the consumer deployed **after** the rename. It defines
  `tasks_new.generate_report` and registers the old name as a migration alias
  with `huey.register_task_alias(...)`.
* `alias_config.py` -- the shared SQLite path and the old/new task names.
* `publish_demo.py`, `consume.py`, `status_cli.py`, `after_retire.py` -- the
  small programs the driver starts in separate processes.
* `run_demo.py` -- the end-to-end driver.

## What the demo proves

1. Messages published under the old name (queue, future schedule, and a
   message that will fail twice and retry) are executed by the new task.
2. Every message keeps its task ID and therefore its `Result`; the flaky
   report succeeds on its third attempt, with the remaining retry count
   preserved across executions.
3. Retries are re-enqueued under the old name while the alias is registered,
   so the old-name backlog is visible to `task_alias_status()`.
4. Retiring the alias is refused while a message is in-flight (claimed by a
   worker but not yet finished) even though the queue looks short. The
   in-flight count is stored in the shared SQLite backend, so every process
   sees it.
5. Once `task_alias_status()` reports `total == 0`, the alias is retired and a
   stale old-name message is rejected, which is the intended outcome after
   the migration window closes.

## Run it

From the repository root:

```console
$ python examples/task_alias/run_demo.py
```

The driver creates a temporary directory for the database and state files, so
it can be run repeatedly and does not touch any development database.

## Manual walkthrough

```console
$ export HUEY_ALIAS_DB=/tmp/reports.db HUEY_ALIAS_STATE=/tmp/reports-state
$ PYTHONPATH=. python examples/task_alias/publish_demo.py
$ PYTHONPATH=. python examples/task_alias/status_cli.py --status
$ PYTHONPATH=. python examples/task_alias/consume.py --timeout 30
$ PYTHONPATH=. python examples/task_alias/status_cli.py --retire
```

`--retire` exits with status 2 and prints the `TaskAliasInUse` details if any
old-name message remains.

## Notes

* The alias is code, not data: every consumer process must run code that
  registers it. Close the alias with a deploy that removes the
  `register_task_alias()` call, once `task_alias_status()` is empty.
* `queue` and `schedule` are counted by reading those stores. `inflight` is a
  shared counter maintained only for messages that arrived under an alias.
* `unregister_task_alias(..., force=True)` exists for emergencies, but
  strands whatever old-name messages remain.
