"""
Terminal demo of the consumer drain-shutdown API.

Runs a real Huey consumer (thread and process worker modes) against a
local sqlite backend -- no external services required -- and demonstrates:

  1. drain() waits for an in-flight task; its result is written back.
  2. drain() at startup: a queued task is never picked up and not lost.
  3. drain() timeout: reports the remaining task and how to force-stop,
     instead of claiming success.
  4. repeated drain() calls and multiple waiters see one consistent
     result.
  5. all of the above with process workers.

Usage (from the repository root):

    python examples/drain_shutdown.py
"""
import logging
import multiprocessing
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from huey import SqliteHuey


TMP_DIR = tempfile.mkdtemp(prefix='huey-drain-demo-')


def banner(text):
    print('\n=== %s %s' % (text, '=' * max(60 - len(text), 0)))


def make_huey(name):
    return SqliteHuey(name, filename=os.path.join(TMP_DIR, '%s.db' % name),
                      utc=False)


def scenario_inflight(worker_type):
    banner('drain waits for in-flight task (%s workers)' % worker_type)
    huey = make_huey('inflight-%s' % worker_type)

    if worker_type == 'process':
        mp = multiprocessing.get_context('fork')
        started, release = mp.Event(), mp.Event()
    else:
        started, release = threading.Event(), threading.Event()

    @huey.task()
    def blocking_task():
        started.set()
        release.wait(30)
        return 'task-result'

    huey.pending_count()  # Open the connection before any fork().
    consumer = huey.create_consumer(workers=2, worker_type=worker_type,
                                    check_worker_health=False)
    result = blocking_task()
    consumer.start()
    try:
        assert started.wait(10), 'task did not start'
        print('task is running; requesting drain (timeout=10s)')
        drain = consumer.drain(timeout=10)
        print('drain done immediately? %s (expected False)' % drain.done)

        snapshot = drain.wait(0.1)
        print('snapshot while draining: drained=%s resolved=%s pending=%s'
              % (snapshot.drained, snapshot.resolved, snapshot.pending))

        print('releasing the task...')
        release.set()
        report = drain.wait(10)
        print('final report: drained=%s resolved=%s pending=%s'
              % (report.drained, report.resolved, report.pending))
        print('message: %s' % report.message)
        print('result written back: %r' % result.get(blocking=True,
                                                     timeout=5))
        assert report.drained
    finally:
        release.set()
        consumer.stop(graceful=True)


def scenario_drain_at_startup(worker_type):
    banner('drain at startup: queued task not picked up (%s)' % worker_type)
    huey = make_huey('startup-%s' % worker_type)

    @huey.task()
    def queued_task(n):
        return n + 1

    huey.pending_count()
    consumer = huey.create_consumer(workers=1, worker_type=worker_type,
                                    check_worker_health=False)
    queued_task(41)
    print('enqueued 1 task; requesting drain before start()')
    drain = consumer.drain(timeout=10)
    consumer.start()
    try:
        report = drain.wait(10)
        print('report: drained=%s, queue depth=%d (task preserved)'
              % (report.drained, huey.pending_count()))
        assert report.drained
        assert huey.pending_count() == 1, 'queued task was lost!'
    finally:
        consumer.stop(graceful=True)


def scenario_timeout_and_force():
    banner('drain timeout reports pending task, then forced stop')
    huey = make_huey('timeout')
    started, release = threading.Event(), threading.Event()

    @huey.task()
    def stuck_task():
        started.set()
        release.wait(30)
        return 'done'

    consumer = huey.create_consumer(workers=1, worker_type='thread',
                                    check_worker_health=False)
    result = stuck_task()
    consumer.start()
    try:
        assert started.wait(10)
        drain = consumer.drain(timeout=0.5)
        report = drain.wait(10)
        print('report: drained=%s resolved=%s pending=%s'
              % (report.drained, report.resolved, report.pending))
        print('message: %s' % report.message)
        assert not report.drained, 'must not claim success'
        assert len(report.pending) == 1

        print('escalating: releasing task and forcing stop()')
        release.set()
        consumer.stop(graceful=True)
        print('task result after forced stop: %r'
              % result.get(blocking=True, timeout=5))
    finally:
        release.set()
        consumer.stop(graceful=True)


def scenario_repeated_drain():
    banner('repeated drain + two waiters see one consistent result')
    huey = make_huey('repeat')
    started, release = threading.Event(), threading.Event()

    @huey.task()
    def blocking_task():
        started.set()
        release.wait(30)

    consumer = huey.create_consumer(workers=1, worker_type='thread',
                                    check_worker_health=False)
    blocking_task()
    consumer.start()
    try:
        assert started.wait(10)
        drain1 = consumer.drain(timeout=10)
        drain2 = consumer.drain(timeout=10)
        print('same drain object for repeated calls: %s' % (drain1 is drain2))
        assert drain1 is drain2

        reports = []
        waiters = [threading.Thread(
            target=lambda: reports.append(drain1.wait(10)))
            for _ in range(2)]
        for t in waiters:
            t.start()
        release.set()
        main_report = drain1.wait(10)
        for t in waiters:
            t.join(10)
        print('all waiters observed the same report: %s (drained=%s)'
              % (all(r is main_report for r in reports),
                 main_report.drained))
        assert len(reports) == 2 and all(r is main_report for r in reports)
    finally:
        release.set()
        consumer.stop(graceful=True)


def main():
    logging.basicConfig(level=logging.WARNING,
                        format='  [consumer] %(message)s')

    scenario_inflight('thread')
    scenario_drain_at_startup('thread')
    scenario_timeout_and_force()
    scenario_repeated_drain()
    if sys.platform != 'win32':
        scenario_inflight('process')
        scenario_drain_at_startup('process')

    banner('all drain scenarios completed')
    shutil.rmtree(TMP_DIR, ignore_errors=True)


if __name__ == '__main__':
    main()
