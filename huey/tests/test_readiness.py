"""
Tests for consumer startup readiness coordination.

Two layers are covered:

* in-process state-machine tests (thread environment, plus the process
  environment's pipe channel) for generation filtering, timeout, early
  shutdown, business-task isolation and max_tasks recycling;
* real-subprocess integration tests driven through
  ``huey.tests.readiness_fixture`` (both thread and process worker types),
  covering partial-success-then-failure, startup timeout, early shutdown,
  immediate worker exit, unavailable storage, receipt contents, process
  resource cleanup and CLI compatibility/default behavior.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from huey import MemoryHuey
from huey.consumer import Consumer
from huey.consumer import ProcessReadinessChannel
from huey.consumer import ThreadReadinessChannel
from huey.consumer_options import ConsumerConfig
from huey.consumer_options import OptionParserHandler
from huey.exceptions import ConsumerStartupError
from huey.readiness import STATE_FAILED
from huey.readiness import STATE_READY
from huey.readiness import ReadinessMessage
from huey.readiness import REPORT_ERROR
from huey.readiness import REPORT_READY
from huey.readiness import write_readiness_receipt
from huey.tests.base import BaseTestCase


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


class HookWorker(Consumer.worker_class):
    """Worker whose initialize() can be remote-controlled per unit name."""
    fail_units = ()
    block_event = None
    stop_flag_ref = None

    def initialize(self):
        name = threading.current_thread().name
        if name in self.fail_units:
            raise RuntimeError('controlled startup failure in %s' % name)
        if self.block_event is not None:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.stop_flag_ref.is_set():
                    break
                time.sleep(0.01)
        super(HookWorker, self).initialize()


class ImmediateExitWorker(Consumer.worker_class):
    """Worker whose runnable dies the moment initialization completes."""
    exit_units = ()

    def initialize(self):
        super(ImmediateExitWorker, self).initialize()
        if threading.current_thread().name in self.exit_units:
            raise SystemExit(23)


class ReadinessConsumer(Consumer):
    worker_class = HookWorker


class TestReadinessStateMachine(BaseTestCase):
    def consumer(self, **params):
        params.setdefault('initial_delay', 0.005)
        params.setdefault('max_delay', 0.005)
        params.setdefault('workers', 2)
        params.setdefault('startup_timeout', 5)
        return ReadinessConsumer(self.huey, **params)

    def test_ready_requires_every_unit(self):
        consumer = self.consumer(workers=3)
        consumer.start()
        try:
            receipt = consumer.wait_until_ready()
            self.assertTrue(consumer.is_ready())
            self.assertEqual(consumer.startup_state, STATE_READY)
            self.assertEqual(
                receipt['units'],
                ['Scheduler', 'Worker-1', 'Worker-2', 'Worker-3'])
            self.assertEqual(receipt['status'], STATE_READY)
            self.assertEqual(receipt['generation'], 1)
        finally:
            consumer.stop(graceful=True)

    def test_multiple_waiters_all_released(self):
        consumer = self.consumer(workers=1)
        consumer.start()
        results = []

        def wait():
            results.append(consumer.wait_until_ready())

        threads = [threading.Thread(target=wait) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
            self.assertFalse(t.is_alive())
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r['status'] == STATE_READY for r in results))
        consumer.stop(graceful=True)

    def test_startup_hook_failure_partial_success(self):
        consumer = self.consumer(workers=3)
        HookWorker.fail_units = ('Worker-2',)
        consumer.start()
        try:
            with self.assertRaises(ConsumerStartupError) as ctx:
                consumer.wait_until_ready()
            error = ctx.exception
            # The error report and the liveness check race once the unit
            # dies; both outcomes identify the same failing unit.
            self.assertIn(error.reason, ('startup_hook', 'unit_died'))
            self.assertEqual(error.unit, 'Worker-2')
            if error.reason == 'startup_hook':
                self.assertIn('RuntimeError', error.error_type)
            self.assertEqual(consumer.startup_state, STATE_FAILED)
            # Every already-started sibling unit is reclaimed.
            self.assertFalse(any(
                p.is_alive() for _, p in consumer.worker_threads))
            self.assertFalse(consumer.scheduler.is_alive())
        finally:
            HookWorker.fail_units = ()
            consumer.stop_flag.set()

    def test_worker_immediate_exit(self):
        class ExitConsumer(Consumer):
            worker_class = ImmediateExitWorker
        ImmediateExitWorker.exit_units = ('Worker-1',)
        consumer = ExitConsumer(self.huey, workers=2,
                                initial_delay=0.005, max_delay=0.005,
                                startup_timeout=5)
        consumer.start()
        try:
            with self.assertRaises(ConsumerStartupError) as ctx:
                consumer.wait_until_ready()
            self.assertEqual(ctx.exception.reason, 'unit_died')
            self.assertEqual(ctx.exception.unit, 'Worker-1')
            self.assertEqual(consumer.startup_state, STATE_FAILED)
            self.assertFalse(any(
                p.is_alive() for _, p in consumer.worker_threads))
        finally:
            ImmediateExitWorker.exit_units = ()
            consumer.stop_flag.set()

    def test_startup_timeout(self):
        consumer = self.consumer(workers=1, startup_timeout=0.4)
        HookWorker.block_event = threading.Event()
        HookWorker.stop_flag_ref = consumer.stop_flag
        consumer.start()
        start = time.monotonic()
        try:
            with self.assertRaises(ConsumerStartupError) as ctx:
                consumer.wait_until_ready()
            self.assertEqual(ctx.exception.reason, 'timeout')
            self.assertLess(time.monotonic() - start, 2.0)
            # The wait itself bounded by the same unified timeout.
            self.assertGreaterEqual(time.monotonic() - start, 0.35)
            # No background units survive the failure.
            time.sleep(0.1)
            self.assertFalse(consumer.scheduler.is_alive())
            self.assertFalse(any(
                p.is_alive() for _, p in consumer.worker_threads))
        finally:
            HookWorker.block_event = None
            HookWorker.stop_flag_ref = None
            consumer.stop_flag.set()

    def test_shutdown_before_ready_releases_waiters(self):
        consumer = self.consumer(workers=1, startup_timeout=10)
        HookWorker.block_event = threading.Event()
        HookWorker.stop_flag_ref = consumer.stop_flag
        consumer.start()

        errors = []

        def wait():
            try:
                consumer.wait_until_ready()
            except ConsumerStartupError as exc:
                errors.append(exc)

        waiters = [threading.Thread(target=wait) for _ in range(3)]
        for t in waiters:
            t.start()
        time.sleep(0.15)
        consumer.stop()  # Shutdown while startup is still pending.
        for t in waiters:
            t.join(5)
            self.assertFalse(t.is_alive())
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(e.reason == 'shutdown' for e in errors))
        self.assertFalse(consumer.scheduler.is_alive())
        HookWorker.block_event = None
        HookWorker.stop_flag_ref = None

    def test_stale_generation_messages_rejected(self):
        channel = ThreadReadinessChannel()
        current_generation = 2
        channel.sender_for('Worker-1')(
            ReadinessMessage(1, 'Worker-1', REPORT_READY, None, None))
        channel.sender_for('Worker-1')(
            ReadinessMessage(current_generation, 'Worker-1',
                             REPORT_READY, None, None))
        stale = channel.recv(0.1)
        fresh = channel.recv(0.1)
        self.assertEqual(stale.generation, 1)
        self.assertEqual(fresh.generation, current_generation)

        # End-to-end: an old message injected into a running consumer's
        # channel neither confirms anything nor blocks readiness.
        consumer = self.consumer(workers=1)
        consumer.start()
        consumer._readiness_channel._queue.put(
            ReadinessMessage(consumer.generation - 50, 'Worker-1',
                             REPORT_READY, None, None))
        consumer.wait_until_ready()
        self.assertTrue(consumer.is_ready())
        consumer.stop(graceful=True)

    def test_restart_bumps_generation_and_reaches_ready(self):
        consumer = self.consumer(workers=1)
        consumer.start()
        consumer.wait_until_ready()
        first = consumer.generation
        consumer.stop(graceful=True)
        for _, proc in consumer.worker_threads:
            proc.join(2)
        consumer.start()
        consumer.wait_until_ready()
        try:
            self.assertEqual(consumer.generation, first + 1)
            self.assertTrue(consumer.is_ready())
        finally:
            consumer.stop(graceful=True)

    def test_business_task_failure_keeps_ready_state(self):
        @self.huey.task(retries=0)
        def boom():
            raise ValueError('business failure, not startup')

        consumer = self.consumer(workers=1, check_worker_health=True,
                                 health_check_interval=0.2)
        consumer.start()
        consumer.wait_until_ready()
        result = boom()
        with self.assertRaises(Exception):
            result.get(blocking=True, timeout=3)
        time.sleep(0.4)
        self.assertEqual(consumer.startup_state, STATE_READY)
        consumer.stop(graceful=True)

    def test_max_tasks_recycle_does_not_fail_startup(self):
        from huey.consumer import ConsumerStopped

        @self.huey.task()
        def echo(n):
            return n

        consumer = self.consumer(workers=1, max_tasks=2,
                                 check_worker_health=True,
                                 health_check_interval=0.1)
        consumer.start()
        consumer.wait_until_ready()

        # The health check runs from the consumer main loop, which needs its
        # own thread here so result.get() blocking the test cannot prevent a
        # recycled worker from being replaced.
        def run_loop():
            ts = time.monotonic()
            while True:
                try:
                    ts = consumer.loop(ts)
                except ConsumerStopped:
                    return

        driver = threading.Thread(target=run_loop)
        driver.start()
        try:
            for i in range(5):
                self.assertEqual(echo(i).get(blocking=True, timeout=3), i)
            # Allow at least one recycle + health-driven replacement; the
            # initial startup state must remain "ready" throughout.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                self.assertEqual(consumer.startup_state, STATE_READY)
                time.sleep(0.1)
        finally:
            consumer.stop(graceful=True)
            driver.join(3)

    def test_wait_before_start_is_usage_error(self):
        consumer = self.consumer()
        with self.assertRaises(RuntimeError):
            consumer.wait_until_ready()

    def test_storage_preflight_failure(self):
        class DeadStorage(self.huey.storage.__class__):
            def queue_size(self):
                raise OSError('database is gone')

        self.huey.storage = DeadStorage('dead')
        consumer = self.consumer()
        with self.assertRaises(ConsumerStartupError) as ctx:
            consumer.start()
        self.assertEqual(ctx.exception.reason, 'storage_unavailable')
        self.assertEqual(consumer.startup_state, STATE_FAILED)

    def test_invalid_startup_timeout(self):
        with self.assertRaises(Exception):
            ReadinessConsumer(self.huey, startup_timeout=0)

    # --- channels -------------------------------------------------------

    def test_thread_channel_roundtrip_and_drain(self):
        channel = ThreadReadinessChannel()
        send = channel.sender_for('Worker-1')
        self.assertIsNone(channel.recv(0.05))
        send(ReadinessMessage(1, 'Worker-1', REPORT_READY, None, None))
        message = channel.recv(1)
        self.assertEqual(message.status, REPORT_READY)
        send(ReadinessMessage(1, 'Worker-1', REPORT_ERROR, 'E', 'x'))
        channel.drain()
        self.assertIsNone(channel.recv(0.05))

    @unittest.skipIf(sys.platform == 'win32', 'requires fork()')
    def test_process_channel_pipe_roundtrip(self):
        channel = ProcessReadinessChannel()
        send = channel.sender_for('Worker-1')
        import multiprocessing as mp
        ctx = mp.get_context('fork')
        proc = ctx.Process(
            target=lambda: send(
                ReadinessMessage(7, 'Worker-1', REPORT_READY, None, None)))
        proc.start()
        proc.join(5)
        message = channel.recv(2)
        self.assertIsNotNone(message)
        self.assertEqual(message.generation, 7)
        self.assertEqual(message.unit, 'Worker-1')

    def test_receipt_file_is_atomic_and_minimal(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'receipt.json')
        consumer = ReadinessConsumer(
            self.huey, workers=2, startup_timeout=3.0,
            readiness_file=path, initial_delay=0.005, max_delay=0.005)
        consumer.start()
        consumer.wait_until_ready()
        with open(path) as fh:
            payload = json.load(fh)
        self.assertEqual(payload['status'], STATE_READY)
        self.assertEqual(
            set(payload),
            {'status', 'generation', 'worker_type', 'units',
             'startup_timeout'})
        self.assertNotIn('argv', json.dumps(payload))
        self.assertNotIn('filename', json.dumps(payload))
        consumer.stop(graceful=True)

        failure = ConsumerStartupError('timeout', 'boom', unit='Worker-1',
                                       error_type='builtins.RuntimeError')
        payload = write_readiness_receipt(
            path, STATE_FAILED, 3, 'thread', ['Scheduler', 'Worker-1'],
            3.0, failure)
        with open(path) as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk, payload)
        self.assertEqual(on_disk['error']['reason'], 'timeout')
        self.assertEqual(set(on_disk['error']),
                         {'reason', 'unit', 'error_type', 'message'})

    def test_config_wiring(self):
        config = ConsumerConfig(startup_timeout=2.5,
                                readiness_file='/tmp/r.json')
        config.validate()
        consumer = self.huey.create_consumer(**config.values)
        self.assertEqual(consumer.startup_timeout, 2.5)
        self.assertEqual(consumer.readiness_file, '/tmp/r.json')

        parser = OptionParserHandler().get_option_parser()
        options, _ = parser.parse_args(
            ['-u', '2.5', '--readiness-file=/tmp/r.json'])
        self.assertEqual(options.startup_timeout, 2.5)
        self.assertEqual(options.readiness_file, '/tmp/r.json')

        # Defaults remain unchanged for every pre-existing option.
        default = ConsumerConfig()
        self.assertEqual(default.startup_timeout, 10.0)
        self.assertIsNone(default.readiness_file)


class TestReadinessSubprocess(unittest.TestCase):
    """Real-subprocess coverage using the readiness_fixture module."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='huey-readiness-test-')
        self.receipt = os.path.join(self.tmp, 'receipt.json')
        self.env = self._clean_env()
        self.env['HUEY_TEST_READINESS_FILE'] = self.receipt

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _clean_env(self):
        # Minimal environment: -I ignores user site/packages; keep only what
        # the interpreter and sqlite need, plus the repo on PYTHONPATH.
        keep = ('PATH', 'HOME', 'TMPDIR', 'COVERAGE_PROCESS_START')
        env = {k: v for k, v in os.environ.items() if k in keep}
        env['PYTHONPATH'] = REPO_ROOT
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        return env

    def _launch(self, scenario, worker_type='thread', workers=2,
                startup_timeout=10, extra_env=None, expect_signal=False):
        env = dict(self.env)
        env['HUEY_TEST_SCENARIO'] = scenario
        env['HUEY_TEST_WORKER_TYPE'] = worker_type
        env['HUEY_TEST_WORKERS'] = str(workers)
        env['HUEY_TEST_STARTUP_TIMEOUT'] = str(startup_timeout)
        if extra_env:
            env.update(extra_env)
        cmd = [sys.executable, '-I', '-m', 'huey.tests.readiness_fixture']
        return subprocess.Popen(
            cmd, env=env, cwd=self.tmp,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # New session so we can signal the whole group if needed.
            start_new_session=True)

    def _cleanup(self, proc):
        """Ensure the process group is gone and its pipes are closed."""
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    def _wait_receipt(self, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(self.receipt):
                with open(self.receipt) as fh:
                    return json.load(fh)
            time.sleep(0.05)
        self.fail('receipt was not written within %ss' % timeout)

    def _assert_no_descendants(self, proc, timeout=5):
        # Reap the process, then verify no child/worker remains. pgrep on the
        # fixture module name catches any orphaned worker unit.
        proc.wait(timeout=timeout)
        time.sleep(0.2)
        check = subprocess.run(
            ['pgrep', '-f', 'huey.tests.readiness_fixture'],
            stdout=subprocess.PIPE)
        survivors = [p for p in check.stdout.decode().split()
                     if p != str(proc.pid)]
        self.assertEqual(survivors, [],
                         'orphaned worker processes: %s' % survivors)

    def test_success_thread_and_process(self):
        for worker_type in ('thread', 'process'):
            with self.subTest(worker_type=worker_type):
                receipt_path = os.path.join(self.tmp,
                                            'ok-%s.json' % worker_type)
                env = dict(self.env)
                env['HUEY_TEST_READINESS_FILE'] = receipt_path
                env.update(HUEY_TEST_SCENARIO='success',
                           HUEY_TEST_WORKER_TYPE=worker_type,
                           HUEY_TEST_WORKERS='2',
                           HUEY_TEST_STARTUP_TIMEOUT='10')
                proc = subprocess.Popen(
                    [sys.executable, '-I', '-m',
                     'huey.tests.readiness_fixture'],
                    env=env, cwd=self.tmp, start_new_session=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    self._wait_file(receipt_path)
                    with open(receipt_path) as fh:
                        payload = json.load(fh)
                    self.assertEqual(payload['status'], STATE_READY)
                    self.assertEqual(payload['worker_type'], worker_type)
                    self.assertEqual(
                        payload['units'],
                        ['Scheduler', 'Worker-1', 'Worker-2'])
                    # The fixture completes a real task before parking.
                    self._wait_log(proc, 'ready and task processed')
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=10)
                    self.assertEqual(proc.returncode, 0)
                finally:
                    self._cleanup(proc)

    def _wait_file(self, path, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(path):
                return
            time.sleep(0.05)
        self.fail('file %s missing after %ss' % (path, timeout))

    def _wait_log(self, proc, text, timeout=15):
        deadline = time.monotonic() + timeout
        # stderr is piped; read in a thread to avoid blocking the child.
        lines = []

        def reader():
            for raw in proc.stderr:
                lines.append(raw.decode())
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        while time.monotonic() < deadline:
            if any(text in line for line in lines):
                return
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        self.fail('log %r not observed; got %s' % (text, ''.join(lines)))

    def test_partial_success_then_failure(self):
        for worker_type in ('thread', 'process'):
            with self.subTest(worker_type=worker_type):
                receipt_path = os.path.join(
                    self.tmp, 'partial-%s.json' % worker_type)
                env = dict(self.env)
                env['HUEY_TEST_READINESS_FILE'] = receipt_path
                env.update(HUEY_TEST_SCENARIO='partial_failure',
                           HUEY_TEST_WORKER_TYPE=worker_type,
                           HUEY_TEST_WORKERS='3',
                           HUEY_TEST_FAIL_WORKER='Worker-2',
                           HUEY_TEST_STARTUP_TIMEOUT='10')
                proc = subprocess.Popen(
                    [sys.executable, '-I', '-m',
                     'huey.tests.readiness_fixture'],
                    env=env, cwd=self.tmp, start_new_session=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    proc.wait(timeout=20)
                    self.assertEqual(proc.returncode, 1)
                    with open(receipt_path) as fh:
                        payload = json.load(fh)
                    self.assertEqual(payload['status'], STATE_FAILED)
                    # The explicit error report and the liveness check race
                    # once the failing unit dies; both identify the same unit.
                    self.assertIn(payload['error']['reason'],
                                  ('startup_hook', 'unit_died'))
                    self.assertEqual(payload['error']['unit'], 'Worker-2')
                    if payload['error']['reason'] == 'startup_hook':
                        self.assertIn('RuntimeError',
                                      payload['error']['error_type'])
                    # Receipt must carry no credentials or parameters.
                    self.assertNotIn('dsn', json.dumps(payload).lower())
                    self._assert_no_descendants(proc)
                finally:
                    self._cleanup(proc)

    def test_startup_timeout(self):
        proc = self._launch('hook_block', startup_timeout=0.6)
        try:
            proc.wait(timeout=20)
            self.assertEqual(proc.returncode, 1)
            payload = self._wait_receipt()
            self.assertEqual(payload['status'], STATE_FAILED)
            self.assertEqual(payload['error']['reason'], 'timeout')
            self._assert_no_descendants(proc)
        finally:
            self._cleanup(proc)

    def test_shutdown_before_ready(self):
        proc = self._launch('shutdown_before_ready', startup_timeout=30)
        try:
            time.sleep(0.4)  # Inside the blocking startup hook.
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 1)
            payload = self._wait_receipt()
            self.assertEqual(payload['error']['reason'], 'shutdown')
            self._assert_no_descendants(proc)
        finally:
            self._cleanup(proc)

    def test_worker_immediate_exit(self):
        for worker_type in ('thread', 'process'):
            with self.subTest(worker_type=worker_type):
                receipt_path = os.path.join(
                    self.tmp, 'exit-%s.json' % worker_type)
                env = dict(self.env)
                env['HUEY_TEST_READINESS_FILE'] = receipt_path
                env.update(HUEY_TEST_SCENARIO='worker_exit',
                           HUEY_TEST_WORKER_TYPE=worker_type,
                           HUEY_TEST_WORKERS='2',
                           HUEY_TEST_STARTUP_TIMEOUT='10')
                proc = subprocess.Popen(
                    [sys.executable, '-I', '-m',
                     'huey.tests.readiness_fixture'],
                    env=env, cwd=self.tmp, start_new_session=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    proc.wait(timeout=20)
                    self.assertEqual(proc.returncode, 1)
                    with open(receipt_path) as fh:
                        payload = json.load(fh)
                    self.assertEqual(payload['error']['reason'],
                                     'unit_died')
                    self._assert_no_descendants(proc)
                finally:
                    self._cleanup(proc)

    def test_storage_unavailable(self):
        proc = self._launch('storage_missing_dir')
        try:
            proc.wait(timeout=15)
            self.assertEqual(proc.returncode, 1)
            payload = self._wait_receipt()
            self.assertEqual(payload['error']['reason'],
                             'storage_unavailable')
        finally:
            self._cleanup(proc)

    def test_cli_compatibility_and_defaults(self):
        # Original CLI usage still parses and the new options are additive.
        # optparse reports None when an option is not supplied; the actual
        # default is applied later by ConsumerConfig.
        parser = OptionParserHandler().get_option_parser()
        options, args = parser.parse_args(['app.queue.huey', '-w', '4',
                                           '-k', 'thread', '-g', 'TERM',
                                           '-t', '55'])
        self.assertEqual(args, ['app.queue.huey'])
        self.assertEqual(options.workers, 4)
        self.assertEqual(options.graceful_signal, 'TERM')
        self.assertEqual(options.shutdown_timeout, 55)
        self.assertIsNone(options.startup_timeout)
        self.assertIsNone(options.readiness_file)

    def test_receipt_excludes_argv_and_credentials(self):
        proc = self._launch('partial_failure',
                            extra_env={'HUEY_TEST_FAIL_WORKER': 'Worker-1'})
        try:
            proc.wait(timeout=20)
            payload = self._wait_receipt()
            raw = json.dumps(payload)
            for forbidden in ('readiness_fixture', 'PYTHONPATH', '-I',
                              'password', 'secret', 'argv'):
                self.assertNotIn(forbidden, raw)
        finally:
            self._cleanup(proc)
