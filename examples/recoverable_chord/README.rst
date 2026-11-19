# Recoverable chord — terminal reproduction

This directory is a self-contained reproduction of the recoverable SQLite
chord mode. It uses the stock `group`/`chord`/`Result` APIs and the real
`SqliteHuey` storage backend — there is no demo-only queue.

## What it shows

| Command   | What it proves |
|-----------|----------------|
| `seed`    | A chord is fully persisted (`chord`, `chord_member`, `chord_result` tables) before the enqueuing process exits. |
| `consume` | A fresh multi-process consumer drains the batch; the retrying member reports once, on its final success. |
| `crash`   | A chord is left in the `ready` state with **no** callback in the queue — the on-disk evidence of a worker killed at the join. |
| `recover` | A new process runs `chord_sweep()`: the callback is enqueued exactly once and two further sweeps publish no duplicate. |
| `race`    | Two independent worker processes finish the last members concurrently; exactly one callback task is created. |
| `status`  | Dumps the raw durable-chord tables. |

## Run it

From the repository root, with the project virtualenv active:

```bash
# 1. Persist a batch and exit without consuming anything.
python -m examples.recoverable_chord.demo seed

# 2. Consume it from separate worker processes.
python -m examples.recoverable_chord.demo consume

# 3. Simulate the process dying between "all members done" and
#    "callback enqueued".
python -m examples.recoverable_chord.demo crash

# 4. Restart and recover — no lost callback, no duplicate.
python -m examples.recoverable_chord.demo recover

# 5. Concurrent finishers produce a single callback delivery.
python -m examples.recoverable_chord.demo race
```

## Expected output (abridged)

```
$ python -m examples.recoverable_chord.demo crash
All three members reported; chord state is now: ready

== Simulated crash: ready chord, callback NOT enqueued ==
chord 36b82a8a size=3 state=ready
  reported slots: [(0, 31), (1, 31), (2, 31)]
Queue depth: 0 -- the callback would be lost with the legacy
counter implementation.

$ python -m examples.recoverable_chord.demo recover
Queue depth on startup: 0
sweep #1: {'delivered': ['36b82a8a-...'], 'reconciled': [], 'removed': 0}
sweep #2: {'delivered': [], ...} (no duplicate delivery)
sweep #3: {'delivered': [], ...} (still no duplicate)
Queue depth after sweeps: 1

Recovered callback output:
  ['converted:crash1', 'converted:crash2', 'converted:crash3']
```

The same guarantee holds for a real consumer restart: the consumer runs an
initial `chord_sweep()` before spawning workers and repeats it periodically
from the scheduler (`chord_sweep_interval`, default 5 s;
`chord_sweep_grace`, default 300 s; `chord_sweep_retention`,
default 3600 s).

## Files

* `demo_tasks.py` — `SqliteHuey(durable_chord=True)` instance and the
  document-conversion tasks plus the batch-submission callback.
* `demo.py` — scenario driver. Each subcommand is an independent process,
  so `crash` followed by `recover` is a genuine restart, not an in-memory
  simulation.
