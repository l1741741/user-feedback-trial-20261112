import multiprocessing
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest

from huey import SqliteHuey
from huey.api import Result
from huey.consumer import Consumer
from huey.consumer import ConsumerStopped
from huey.consumer import Worker
from huey.consumer import WorkerExit
from huey.tests.base import BaseTestCase
from huey.tests.base import slow_test


def wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def run_consumer_loop(consumer):
    # Mirrors Consumer.run() minus start() (signal handlers must be
    # registered on the main thread, so start() is called by the test).
    health_check_ts = time.monotonic()
    while True:
        try:
            health_check_ts = consumer.loop(health_check_ts)
        except ConsumerStopped:
            break
    consumer.huey.notify_interrupted_tasks()


def worker_events(consumer, event=None, reason=None):
    return [e for e in consumer.lifecycle_events
            if (event is None or e.event == event) and
            (reason is None or e.reason == reason)]


def live_worker_threads():
    return [t for t in threading.enumerate() if t.name.startswith('Worker-')]


class TestRecycleThreadMode(BaseTestCase):
    def setUp(self):
        super(TestRecycleThreadMode, self).setUp()
        self._handlers = [(s, signal.getsignal(s)) for s in
                          (signal.SIGINT, signal.SIGTERM)]

    def tearDown(self):
        for signum, handler in self._handlers:
            signal.signal(signum, handler)
        super(TestRecycleThreadMode, self).tearDown()

    def make_consumer(self, **kwargs):
        params = dict(workers=1, periodic=False, initial_delay=0.001,
                      max_delay=0.001, check_worker_health=True,
                      health_check_interval=0.01)
        params.update(kwargs)
        return self.huey.create_consumer(**params)

    def start_consumer(self, consumer):
        consumer.start()
        self._loop_t = threading.Thread(target=run_consumer_loop,
                                        args=(consumer,))
        self._loop_t.start()

    def stop_consumer(self, consumer):
        consumer.stop(graceful=True)
        self._loop_t.join(10)
        self.assertFalse(self._loop_t.is_alive())

    def test_recycle_lifecycle_thread(self):
        hooks = []

        @self.huey.on_startup()
        def startup():
            hooks.append('startup')

        @self.huey.on_shutdown()
        def shutdown():
            hooks.append('shutdown')

        @self.huey.task()
        def task_a(n):
            return n + 1

        results = [task_a(i) for i in range(5)]
        consumer = self.make_consumer(max_tasks=2)
        self.start_consumer(consumer)
        try:
            for i, result in enumerate(results):
                self.assertEqual(result.get(blocking=True, timeout=5), i + 1)
            # 5 tasks at 2 tasks/generation -> 3 generations.
            self.assertTrue(wait_for(
                lambda: len(worker_events(consumer, 'start')) == 3, 5))
        finally:
            self.stop_consumer(consumer)

        self.assertEqual(len(self.huey), 0)  # Queue fully drained.

        starts = worker_events(consumer, 'start')
        exits = worker_events(consumer, 'exit')
        self.assertEqual([e.generation for e in starts], [1, 2, 3])
        self.assertEqual(len(exits), 3)

        recycled = [e for e in exits if e.reason == WorkerExit.RECYCLED]
        stopped = [e for e in exits if e.reason == WorkerExit.STOPPED]
        self.assertEqual(len(recycled), 2)
        self.assertEqual(len(stopped), 1)
        for event in recycled:
            self.assertEqual(event.task_count, 2)
            self.assertEqual(event.tasks_completed, 2)
            self.assertEqual(event.tasks_requeued, 0)

        # Each generation ran its startup and shutdown hooks exactly once.
        self.assertEqual(hooks.count('startup'), 3)
        self.assertEqual(hooks.count('shutdown'), 3)

        # No residual worker threads.
        self.assertEqual(live_worker_threads(), [])

    def test_recycle_preserves_results_and_pipeline(self):
        # A worker that reaches its quota must finish storing the result and
        # enqueueing the successor before exiting; the next generation then
        # picks the successor up.
        @self.huey.task()
        def parent(n):
            return n + 1

        @self.huey.task()
        def child(marker, parent_value):
            return parent_value * 10

        pipe = parent.s(1).then(child, 5)
        self.huey.enqueue(pipe)
        parent_result = Result(self.huey, pipe)
        child_result = Result(self.huey, pipe.on_complete)

        consumer = self.make_consumer(max_tasks=1)
        self.start_consumer(consumer)
        try:
            self.assertEqual(child_result.get(blocking=True, timeout=5), 20)
            self.assertEqual(parent_result.get(), 2)
            # max_tasks=1: one generation per task, then an idle one.
            self.assertTrue(wait_for(
                lambda: len(worker_events(consumer, 'start')) == 3, 5))
        finally:
            self.stop_consumer(consumer)

        self.assertEqual(len(self.huey), 0)
        recycled = worker_events(consumer, 'exit', WorkerExit.RECYCLED)
        self.assertEqual(len(recycled), 2)
        for event in recycled:
            self.assertEqual(event.tasks_completed, 1)

    def test_recycle_and_shutdown_race(self):
        # The worker reaches its max_tasks quota at the same moment the
        # consumer is asked to shut down: the in-flight task must finish
        # normally, and no new generation may be spawned afterwards.
        entered = threading.Event()
        release = threading.Event()
        hooks = []

        @self.huey.on_shutdown()
        def shutdown():
            hooks.append('shutdown')

        @self.huey.task()
        def blocker():
            entered.set()
            release.wait(10)
            return 'done'

        result = blocker()
        consumer = self.make_consumer(max_tasks=1)
        self.start_consumer(consumer)
        self.assertTrue(entered.wait(5))

        # Begin the graceful shutdown while the worker is mid-task at quota.
        stop_t = threading.Thread(target=consumer.stop,
                                  kwargs={'graceful': True})
        stop_t.start()
        time.sleep(0.2)  # Let stop() begin joining the worker.
        release.set()
        stop_t.join(10)
        self.assertFalse(stop_t.is_alive())
        self._loop_t.join(10)

        # The in-flight task completed and stored its result.
        self.assertEqual(result.get(blocking=True, timeout=5), 'done')

        # No new generation was spawned once shutdown began.
        self.assertEqual(len(worker_events(consumer, 'start')), 1)
        exits = worker_events(consumer, 'exit')
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].reason, WorkerExit.RECYCLED)
        self.assertEqual(exits[0].tasks_completed, 1)

        # The single generation's shutdown hook ran exactly once.
        self.assertEqual(hooks, ['shutdown'])

        # A late health check must not spawn a replacement.
        self.assertTrue(consumer.check_worker_health())
        self.assertEqual(len(worker_events(consumer, 'start')), 1)
        self.assertEqual(live_worker_threads(), [])

    def test_retry_not_counted_as_completed(self):
        attempts = []

        @self.huey.task(retries=1)
        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise ValueError('boom')
            return 'ok'

        result = flaky()
        consumer = self.consumer()  # No max_tasks.
        worker, _ = consumer.worker_threads[0]

        worker.loop()  # First attempt fails and is requeued for retry.
        self.assertEqual(worker.task_count, 1)
        self.assertEqual(worker.tasks_completed, 0)
        self.assertEqual(worker.tasks_requeued, 1)
        self.assertEqual(len(self.huey), 1)  # Requeued, chain still open.

        worker.loop()  # The retry succeeds and finishes the chain.
        self.assertEqual(worker.task_count, 2)
        self.assertEqual(worker.tasks_completed, 1)
        self.assertEqual(worker.tasks_requeued, 1)
        self.assertEqual(len(self.huey), 0)
        self.assertEqual(result.get(), 'ok')

    def test_shutdown_hooks_continue_on_error_and_run_once(self):
        calls = []

        @self.huey.on_shutdown()
        def failing():
            calls.append('failing')
            raise RuntimeError('hook failed')

        @self.huey.on_shutdown()
        def cleanup():
            calls.append('cleanup')

        consumer = self.consumer()
        worker, _ = consumer.worker_threads[0]
        with self.assertLogs('huey.consumer.Worker', level='ERROR'):
            worker.shutdown()
        # The failing hook did not prevent the remaining cleanup.
        self.assertEqual(calls, ['failing', 'cleanup'])

        # Hooks that already completed are not run again.
        worker.shutdown()
        self.assertEqual(calls, ['failing', 'cleanup'])

    def test_startup_hook_error_does_not_skip_remaining(self):
        calls = []

        @self.huey.on_startup()
        def failing():
            calls.append('failing')
            raise RuntimeError('hook failed')

        @self.huey.on_startup()
        def ok():
            calls.append('ok')

        consumer = self.consumer()
        worker, _ = consumer.worker_threads[0]
        with self.assertLogs('huey.consumer.Worker', level='ERROR'):
            worker.initialize()
        self.assertEqual(calls, ['failing', 'ok'])

    def test_crash_distinguished_from_recycle(self):
        class CrashingWorker(Worker):
            def loop(self, now=None):
                raise RuntimeError('simulated crash')

        class CrashConsumer(Consumer):
            worker_class = CrashingWorker

        consumer = CrashConsumer(
            self.huey, workers=1, periodic=False, initial_delay=0.001,
            max_delay=0.001, max_tasks=100, check_worker_health=True,
            health_check_interval=0.01)
        self.start_consumer(consumer)
        try:
            # Crashed workers are replaced by new generations.
            self.assertTrue(wait_for(
                lambda: len(worker_events(consumer, 'exit',
                                          WorkerExit.CRASHED)) >= 2, 5))
        finally:
            self.stop_consumer(consumer)

        exits = worker_events(consumer, 'exit')
        self.assertGreaterEqual(len(exits), 2)
        # Nothing was recycled: every exit was a crash or the final stop.
        self.assertFalse(
            any(e.reason == WorkerExit.RECYCLED for e in exits))
        self.assertTrue(all(
            e.reason in (WorkerExit.CRASHED, WorkerExit.STOPPED,
                         WorkerExit.INTERRUPTED) for e in exits))
        self.assertEqual(live_worker_threads(), [])

    def test_health_check_after_stop_spawns_nothing(self):
        consumer = self.make_consumer(max_tasks=1)
        self.start_consumer(consumer)
        self.stop_consumer(consumer)

        self.assertEqual(live_worker_threads(), [])
        starts_before = len(worker_events(consumer, 'start'))
        self.assertTrue(consumer.check_worker_health())
        self.assertEqual(len(worker_events(consumer, 'start')),
                         starts_before)
        self.assertEqual(live_worker_threads(), [])

    def test_no_max_tasks_unchanged(self):
        hooks = []

        @self.huey.on_startup()
        def startup():
            hooks.append('startup')

        @self.huey.on_shutdown()
        def shutdown():
            hooks.append('shutdown')

        @self.huey.task()
        def task_a(n):
            return n + 1

        results = [task_a(i) for i in range(3)]
        consumer = self.make_consumer()  # No max_tasks.
        self.start_consumer(consumer)
        try:
            for i, result in enumerate(results):
                self.assertEqual(result.get(blocking=True, timeout=5), i + 1)
        finally:
            self.stop_consumer(consumer)

        # A single generation, no recycling, hooks ran exactly once.
        self.assertEqual(len(worker_events(consumer, 'start')), 1)
        exits = worker_events(consumer, 'exit')
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].reason, WorkerExit.STOPPED)
        self.assertEqual(exits[0].task_count, 3)
        self.assertEqual(exits[0].tasks_completed, 3)
        self.assertEqual(hooks, ['startup', 'shutdown'])
        self.assertEqual(live_worker_threads(), [])


@unittest.skipIf(sys.platform == 'win32', 'requires fork()')
class TestRecycleProcessMode(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.huey = SqliteHuey(
            'recycle-process-test',
            filename=os.path.join(self.tmp_dir, 'huey.db'))
        self._handlers = [(s, signal.getsignal(s)) for s in
                          (signal.SIGINT, signal.SIGTERM)]

    def tearDown(self):
        for signum, handler in self._handlers:
            signal.signal(signum, handler)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def make_consumer(self, **kwargs):
        params = dict(workers=1, periodic=False, worker_type='process',
                      initial_delay=0.01, max_delay=0.01,
                      check_worker_health=True, health_check_interval=0.1)
        params.update(kwargs)
        return self.huey.create_consumer(**params)

    def read_hooks(self):
        hook_log = os.path.join(self.tmp_dir, 'hooks.log')
        if not os.path.exists(hook_log):
            return []
        with open(hook_log) as fh:
            return [line.split() for line in fh.read().splitlines()]

    def register_hooks(self):
        hook_log = os.path.join(self.tmp_dir, 'hooks.log')

        @self.huey.on_startup()
        def startup():
            with open(hook_log, 'a') as fh:
                fh.write('startup %d\n' % os.getpid())

        @self.huey.on_shutdown()
        def shutdown():
            with open(hook_log, 'a') as fh:
                fh.write('shutdown %d\n' % os.getpid())

    def assert_process_gone(self, pid):
        self.assertRaises(OSError, os.kill, pid, 0)

    @slow_test()
    def test_process_recycle_lifecycle(self):
        self.register_hooks()

        @self.huey.task()
        def whoami(n):
            # The connection must have been opened by this process -- a
            # handle inherited across fork() must not be reused.
            return (n, os.getpid(), self.huey.storage._conn_pid)

        results = [whoami(i) for i in range(4)]
        consumer = self.make_consumer(max_tasks=2)
        consumer.start()
        loop_t = threading.Thread(target=run_consumer_loop,
                                  args=(consumer,))
        loop_t.start()
        try:
            out = [r.get(blocking=True, timeout=20) for r in results]
            # 4 tasks at 2 tasks/generation -> 3rd generation comes up.
            self.assertTrue(wait_for(
                lambda: len(worker_events(consumer, 'start')) == 3, 10))
        finally:
            consumer.stop(graceful=True)
            loop_t.join(10)
        self.assertFalse(loop_t.is_alive())

        self.assertEqual(sorted(n for n, _, _ in out), [0, 1, 2, 3])
        worker_pids = set(pid for _, pid, _ in out)
        self.assertEqual(len(worker_pids), 2)  # Two generations did work.
        self.assertNotIn(os.getpid(), worker_pids)
        for _, pid, conn_pid in out:
            # No database handle was reused across processes.
            self.assertEqual(conn_pid, pid)

        exits = worker_events(consumer, 'exit')
        self.assertEqual(len(worker_events(consumer, 'start')), 3)
        self.assertEqual(len(exits), 3)
        recycled = [e for e in exits if e.reason == WorkerExit.RECYCLED]
        stopped = [e for e in exits if e.reason == WorkerExit.STOPPED]
        self.assertEqual(len(recycled), 2)
        self.assertEqual(len(stopped), 1)
        self.assertEqual([e.generation for e in recycled], [1, 2])

        # Each generation ran its hooks exactly once, in its own process.
        hooks = self.read_hooks()
        self.assertEqual(len(hooks), 6)
        self.assertEqual([h[0] for h in hooks[:1] + hooks[-1:]],
                         ['startup', 'shutdown'])
        self.assertEqual(sum(1 for h in hooks if h[0] == 'startup'), 3)
        self.assertEqual(sum(1 for h in hooks if h[0] == 'shutdown'), 3)
        hook_pids = set(int(h[1]) for h in hooks if h[0] == 'startup')
        self.assertEqual(len(hook_pids), 3)
        self.assertNotIn(os.getpid(), hook_pids)

        # No residual processes: every generation was reaped.
        for pid in hook_pids:
            self.assert_process_gone(pid)
        for _, proc in consumer.worker_threads:
            self.assertFalse(proc.is_alive())

    @slow_test()
    def test_process_recycle_and_shutdown_race(self):
        self.register_hooks()
        entered = multiprocessing.Event()
        release = multiprocessing.Event()

        @self.huey.task()
        def blocker():
            entered.set()
            release.wait(30)
            return os.getpid()

        result = blocker()
        consumer = self.make_consumer(max_tasks=1, shutdown_timeout=15)
        consumer.start()
        loop_t = threading.Thread(target=run_consumer_loop,
                                  args=(consumer,))
        loop_t.start()
        try:
            self.assertTrue(entered.wait(10))
            # Begin the graceful shutdown while the worker is mid-task at
            # its max_tasks quota.
            stop_t = threading.Thread(target=consumer.stop,
                                      kwargs={'graceful': True})
            stop_t.start()
            time.sleep(0.3)
            release.set()
            stop_t.join(20)
            self.assertFalse(stop_t.is_alive())
            loop_t.join(10)
        finally:
            if not consumer.stop_flag.is_set():
                consumer.stop(graceful=True)
            loop_t.join(10)

        # The in-flight task finished and stored its result.
        worker_pid = result.get(blocking=True, timeout=10)
        self.assertNotEqual(worker_pid, os.getpid())

        # No new generation was spawned after shutdown began.
        self.assertEqual(len(worker_events(consumer, 'start')), 1)
        exits = worker_events(consumer, 'exit')
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].reason, WorkerExit.RECYCLED)

        # The single generation's hooks ran exactly once each.
        hooks = self.read_hooks()
        self.assertEqual([h[0] for h in hooks], ['startup', 'shutdown'])
        self.assertEqual(int(hooks[0][1]), worker_pid)
        self.assertEqual(int(hooks[1][1]), worker_pid)

        # The worker process is gone.
        self.assert_process_gone(worker_pid)
        for _, proc in consumer.worker_threads:
            self.assertFalse(proc.is_alive())
