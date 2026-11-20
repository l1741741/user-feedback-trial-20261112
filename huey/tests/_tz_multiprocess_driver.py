"""
Subprocess driver for the multi-process timezone periodic tests.

This module is imported by ``test_tz_periodic_multiprocess.py`` and also
executed directly with ``python -m``. Every process receives a *fake current
time* through the ``HUEY_TZ_NOW`` environment variable (an ISO-8601 naive UTC
timestamp), so the tests exercise real process boundaries and a real SQLite
database without touching the system clock or the network.

Modes (selected with ``HUEY_TZ_MODE``):

* ``tick``      -- run one scheduler periodic tick at the fake time.
* ``dual``      -- two scheduler instances tick concurrently in the same
                   process tree, sharing one SQLite file.
* ``serve``     -- run a full consumer until the sentinel file appears
                   (used for the real-restart / SIGHUP test).
* ``count``     -- print the number of task rows currently in the queue.

The scheduled function appends one line per execution to a results file; the
line includes the task's occurrence identity so DST fold runs are
distinguishable.
"""
import datetime
import os
import sys
import threading
import time

from huey import SqliteHuey
from huey import tzcrontab
from huey.consumer import Consumer


def _now():
    raw = os.environ.get('HUEY_TZ_NOW')
    if not raw:
        raise RuntimeError('HUEY_TZ_NOW is required')
    return datetime.datetime.fromisoformat(raw)


def _db_path():
    path = os.environ['HUEY_TZ_DB']
    return path


def _results_path():
    return os.environ['HUEY_TZ_RESULTS']


def build_huey():
    return SqliteHuey('tz-multiprocess', filename=_db_path())


def register_context_tasks(huey, **tz_kwargs):
    tz_kwargs.setdefault('timezone', os.environ.get(
        'HUEY_TZ_ZONE', 'America/New_York'))
    tz_kwargs.setdefault('minute', os.environ.get('HUEY_TZ_MINUTE', '30'))
    tz_kwargs.setdefault('hour', os.environ.get('HUEY_TZ_HOUR', '2'))
    if 'HUEY_TZ_GAP_POLICY' in os.environ:
        tz_kwargs['gap_policy'] = os.environ['HUEY_TZ_GAP_POLICY']
    if 'HUEY_TZ_FOLD_POLICY' in os.environ:
        tz_kwargs['fold_policy'] = os.environ['HUEY_TZ_FOLD_POLICY']
    if 'HUEY_TZ_CATCHUP' in os.environ:
        tz_kwargs['catchup'] = int(os.environ['HUEY_TZ_CATCHUP'])

    @huey.periodic_task(tzcrontab(**tz_kwargs), context=True)
    def ops_daily(task=None):
        with open(_results_path(), 'a') as fh:
            fh.write('%s\n' % task.occurrence_id)

    return ops_daily


def run_tick(now=None):
    now = now or _now()
    huey = build_huey()
    register_context_tasks(huey)
    enqueued = huey.enqueue_due_periodic(now)
    # Drain the queue in-process so a worker is not required for the
    # dedup-focused tests; execution records the occurrence id.
    while True:
        task = huey.dequeue()
        if task is None:
            break
        huey.execute(task, now)
    huey.storage.close()
    return [t.occurrence_id for t in enqueued]


def run_dual():
    # Two independent Huey/storage objects over the same SQLite file, ticking
    # concurrently on threads with a start barrier to maximize the race.
    now = _now()
    results = []
    barrier = threading.Barrier(2)

    def worker():
        huey = build_huey()
        register_context_tasks(huey)
        barrier.wait()
        enqueued = huey.enqueue_due_periodic(now)
        while True:
            task = huey.dequeue()
            if task is None:
                break
            huey.execute(task, now)
        results.append([t.occurrence_id for t in enqueued])
        huey.storage.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def run_serve():
    # Run a live consumer (real scheduler + worker processes/threads) until
    # the stop sentinel appears. The multi-process tests exercise a real
    # restart by terminating this process and spawning a fresh interpreter
    # against the same SQLite database. A pidfile heartbeat records each
    # incarnation.
    huey = build_huey()
    register_context_tasks(huey)
    with open(os.environ['HUEY_TZ_PIDFILE'], 'a') as fh:
        fh.write('%s\n' % os.getpid())

    consumer = Consumer(huey, workers=1, scheduler_interval=1,
                        initial_delay=0.01, max_delay=0.01,
                        check_worker_health=False)
    consumer.start()
    stop_sentinel = os.environ['HUEY_TZ_SENTINEL']
    try:
        while not os.path.exists(stop_sentinel):
            time.sleep(0.05)
    finally:
        consumer.stop(graceful=True)
        huey.storage.close()


def run_count():
    huey = build_huey()
    count = huey.pending_count()
    huey.storage.close()
    return count


def main(argv=None):
    mode = os.environ.get('HUEY_TZ_MODE', 'tick')
    if mode == 'tick':
        print('\n'.join(run_tick()))
    elif mode == 'dual':
        for chunk in run_dual():
            print('\n'.join(chunk))
    elif mode == 'serve':
        run_serve()
    elif mode == 'count':
        print(run_count())
    else:
        print('unknown mode %s' % mode, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
