# Error callback trigger policy demo

End-to-end scenario for the `error_policy` feature: a file-processing
pipeline with retries and error successors (alerts), where the business wants
exactly one alert when a job fails for good.

Run from the repository root (or this directory):

```bash
python examples/error_policy/run_demo.py
```

The driver (`run_demo.py`):

* **Phase A** enqueues a mixed workload on a real `SqliteHuey` queue and runs
  a real consumer subprocess (`huey_consumer`):
  * `flaky_import` -- fails twice, recovers on the 3rd attempt
    (`error_policy='final'`): no alert, only the success chain runs.
  * `broken_convert` -- always fails (`error_policy='final'`): exactly one
    alert, after the last attempt; the stored error is linked to the original
    task ID with `retries=0`.
  * `legacy_job` -- no policy (default, backwards compatible): one alert per
    surfaced attempt.
  * `deferred_retry` -- raises `RetryTask(delay=...)`, then succeeds: never
    an error, no alert.
  * `cancelled_job` -- raises `CancelExecution(retry=False)` despite declared
    retries: stored as an error (not disguised as success), one alert.
  * It also verifies the consumer signal order (`executing` / `error` /
    `retrying` / `scheduled` / `enqueued`) matches the retry lifecycle.
* **Phase B** kills the consumer while a retry is waiting in the SQLite
  schedule table, verifies the scheduled retry and the intermediate error
  state survive the restart, then restarts the consumer and confirms the job
  completes with a consistent event order and no alert.

Runtime artifacts (SQLite db, event/alert logs) are written to `run/`, which
is deleted at the start of each run.
