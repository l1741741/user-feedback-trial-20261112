import asyncio
import importlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from huey.consumer import AsyncWorker
from huey.exceptions import ConfigurationError
from huey.exceptions import TaskException
from huey.tests.base import BaseTestCase
from huey.utils import Error


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class TestAsyncConsumer(BaseTestCase):
    def test_async_result_and_pipeline(self):
        @self.huey.task()
        async def task_a(n):
            await asyncio.sleep(0.01)
            return n + 1

        @self.huey.task()
        def task_b(parent):
            return parent * 10

        with self.consumer_context(enable_async=True, workers=1):
            res = self.huey.enqueue(task_a.s(1).then(task_b))
            self.assertEqual(res.get(blocking=True, timeout=5), [2, 20])

    def test_sync_tasks_unaffected(self):
        @self.huey.task()
        def task_s(n):
            return n * 2

        with self.consumer_context(enable_async=True, workers=1):
            self.assertEqual(task_s(3).get(blocking=True, timeout=5), 6)

    def test_async_retry_success(self):
        attempts = []

        @self.huey.task(retries=1)
        async def task_f():
            attempts.append(1)
            if len(attempts) < 2:
                raise ValueError('boom')
            return 'ok'

        with self.consumer_context(enable_async=True, workers=1):
            res = task_f()
            self.assertEqual(res.get(blocking=True, timeout=5), 'ok')
        self.assertEqual(len(attempts), 2)

    def test_async_retry_exhausted(self):
        attempts = []

        @self.huey.task(retries=1)
        async def task_f():
            attempts.append(1)
            raise ValueError('nope')

        with self.consumer_context(enable_async=True, workers=1):
            res = task_f()
            exc = self.trap_exception(
                lambda: res.get(blocking=True, timeout=5))
        self.assertEqual(len(attempts), 2)
        self.assertIn('nope', str(exc))

    def test_async_task_without_enable_async(self):
        # A consumer without async support must not execute the coroutine
        # or store it as the task result -- the task fails with a clear
        # configuration error instead.
        @self.huey.task()
        async def task_a():
            return 1

        with self.consumer_context():
            res = task_a()
            exc = self.trap_exception(
                lambda: res.get(blocking=True, timeout=5, preserve=True))
            self.assertIn('enable_async', str(exc))
            self.assertIsInstance(self.huey.get(res.id, peek=True), Error)

    def test_enable_async_worker_type(self):
        self.assertRaises(ConfigurationError, self.consumer,
                          enable_async=True, worker_type='process')
        self.assertRaises(ConfigurationError, self.consumer,
                          enable_async=True, worker_type='greenlet')

    def test_per_worker_client_reuse_and_cleanup(self):
        state = threading.local()
        events = []

        class Client(object):
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True
                events.append('closed')

        @self.huey.on_startup()
        async def start_client():
            state.client = Client()
            events.append('started')

        @self.huey.on_shutdown()
        async def stop_client():
            await state.client.close()

        @self.huey.task()
        async def client_id():
            return id(state.client)

        consumer = self.consumer(enable_async=True, workers=1)
        worker, _ = consumer.worker_threads[0]
        self.assertIsInstance(worker, AsyncWorker)
        consumer.start()
        try:
            r1 = client_id()
            r2 = client_id()
            # Consecutive tasks on the same worker reuse its client.
            self.assertEqual(r1.get(blocking=True, timeout=5),
                             r2.get(blocking=True, timeout=5))
            self.assertEqual(events, ['started'])
        finally:
            consumer.stop(graceful=True)

        # Shutdown ran the async shutdown hook and tore down the loop.
        self.assertEqual(events, ['started', 'closed'])
        self.assertTrue(worker._loop is None)

    def test_workers_do_not_share_clients(self):
        state = threading.local()

        @self.huey.on_startup()
        async def start_client():
            state.client = object()

        @self.huey.task()
        async def who():
            await asyncio.sleep(0.1)
            return (threading.current_thread().name, id(state.client))

        with self.consumer_context(enable_async=True, workers=2):
            results = [who() for _ in range(4)]
            pairs = [r.get(blocking=True, timeout=5) for r in results]

        by_worker = {}
        for name, client_id in pairs:
            by_worker.setdefault(name, set()).add(client_id)
        self.assertEqual(len(by_worker), 2)
        for name, client_ids in by_worker.items():
            self.assertEqual(len(client_ids), 1, name)
        all_ids = [cid for ids in by_worker.values() for cid in ids]
        self.assertEqual(len(set(all_ids)), 2)

    def test_startup_failure_stops_worker(self):
        events = []

        @self.huey.on_startup()
        async def good_hook():
            events.append('started')

        @self.huey.on_startup()
        def bad_hook():
            raise RuntimeError('cannot initialize')

        @self.huey.on_shutdown()
        async def cleanup_hook():
            events.append('cleaned')

        consumer = self.consumer(enable_async=True, workers=1)
        worker, worker_t = consumer.worker_threads[0]
        consumer.start()
        try:
            worker_t.join(5)
            # The worker must not appear to be running after a partial
            # initialization failure, and partial state is cleaned up.
            self.assertFalse(worker_t.is_alive())
            self.assertEqual(events, ['started', 'cleaned'])
            self.assertTrue(worker._loop is None)
        finally:
            consumer.stop(graceful=True)


class TestAsyncConsumerProcess(unittest.TestCase):
    """
    End-to-end demonstration: a real consumer process with two thread
    workers executes async tasks against a local inventory service, using
    only a temporary SQLite file and a service on localhost.
    """
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='huey-async-inv-')
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def test_async_consumer_process(self):
        from examples.async_inventory.service import InventoryService

        service = InventoryService()
        service.start_in_thread()
        self.addCleanup(service.stop)

        env_overrides = {
            'ASYNC_INV_DB': os.path.join(self.tmpdir, 'huey.db'),
            'ASYNC_INV_PORT': str(service.port),
            'ASYNC_INV_CLEANUP_LOG': os.path.join(self.tmpdir, 'cleanup.log'),
            'ASYNC_INV_ATTEMPTS_LOG': os.path.join(self.tmpdir, 'attempts.log'),
        }
        # Must be set before the demo tasks module is imported.
        os.environ.update(env_overrides)
        tasks = importlib.import_module('examples.async_inventory.tasks')
        huey = tasks.huey

        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join(
            [REPO_ROOT, env.get('PYTHONPATH', '')])

        consumer_log = os.path.join(self.tmpdir, 'consumer.log')
        log_fh = open(consumer_log, 'wb')
        proc = subprocess.Popen(
            [sys.executable, '-m', 'examples.async_inventory.consumer', '2'],
            cwd=REPO_ROOT, env=env, stdout=log_fh, stderr=subprocess.STDOUT)
        try:
            self._exercise_consumer(huey, tasks)
            # Graceful shutdown: in-flight work finishes, then the workers'
            # clients and event loops are cleaned up.
            proc.send_signal(signal.SIGINT)
            proc.wait(30)
        except Exception:
            proc.kill()
            proc.wait()
            with open(consumer_log) as fh:
                sys.stderr.write('\n---- consumer.log ----\n%s\n' % fh.read())
            raise
        finally:
            log_fh.close()

        self.assertEqual(proc.returncode, 0)
        self._assert_shutdown_cleanup(tasks, service)

    def _exercise_consumer(self, huey, tasks):
        # 1. Consecutive tasks on the same worker reuse its connection;
        #    the two workers use independent connections.
        results = [tasks.fetch_stock('sku-%d' % i) for i in range(6)]
        infos = [res.get(blocking=True, timeout=30) for res in results]
        by_worker = {}
        for info in infos:
            by_worker.setdefault(info['worker'], set()).add(info['conn_id'])
        self.assertEqual(sorted(by_worker), ['Worker-1', 'Worker-2'])
        for worker, conn_ids in by_worker.items():
            self.assertEqual(len(conn_ids), 1,
                             '%s did not reuse its connection' % worker)
        conn_ids = [cid for ids in by_worker.values() for cid in ids]
        self.assertEqual(len(set(conn_ids)), 2, 'workers shared a connection')

        # 2. A failing async task is retried per its retry settings, then
        #    succeeds; a permanently-failing task stores the error.
        info = tasks.flaky_fetch('sku-flaky').get(blocking=True, timeout=30)
        self.assertEqual(info['attempts'], 2)

        res = tasks.failing_fetch('sku-bad')
        try:
            res.get(blocking=True, timeout=30)
        except TaskException as exc:
            self.assertIn('permanent inventory error', str(exc))
        else:
            self.fail('expected TaskException')

        with open(tasks.ATTEMPTS_LOG) as fh:
            attempts = fh.read().splitlines()
        self.assertEqual(attempts.count('flaky:sku-flaky'), 2)
        self.assertEqual(attempts.count('failing:sku-bad'), 2)

        # 3. A synchronous successor task reads the async parent's result.
        pipeline = tasks.fetch_stock.s('sku-pipe').then(
            tasks.record_stock, 'sync-label')
        parent, successor = huey.enqueue(pipeline).get(
            blocking=True, timeout=30)
        self.assertEqual(parent['sku'], 'sku-pipe')
        self.assertEqual(successor['label'], 'sync-label')
        self.assertEqual(successor['parent'], parent)

    def _assert_shutdown_cleanup(self, tasks, service):
        # Each worker's async shutdown hook closed its client.
        with open(tasks.CLEANUP_LOG) as fh:
            closed = [line.split()[0] for line in fh if line.strip()]
        self.assertEqual(sorted(closed), ['Worker-1', 'Worker-2'])

        # The service saw exactly the two worker connections, both closed.
        deadline = time.time() + 10
        while time.time() < deadline:
            if len(service.connection_events('disconnect')) >= 2:
                break
            time.sleep(0.05)
        self.assertEqual(sorted(service.connection_events('connect')), [1, 2])
        self.assertEqual(sorted(service.connection_events('disconnect')),
                         [1, 2])


if __name__ == '__main__':
    unittest.main()
