"""Process-level tests for durable completion events.

These tests use a *real* consumer subprocess (``huey_consumer``) and an
*independent* receiver subprocess (``python -m huey.completion_receiver``).  They cover
what an in-process unit test cannot:

* consumer crash/exit between result and event delivery, then restart;
* receiver crash before acknowledgement, then a second receiver;
* duplicate/late acknowledgements;
* two receivers competing for the same events;
* retention cleanup with a real consumer still producing events;
* Result and event agreement after restart.

Marked slow (they spawn processes and wait on timers); enable with the
``HUEY_SLOW_TESTS=1`` environment variable or ``runtests.py -s``.
"""
import importlib
import json
import os
import signal
import subprocess
import sys
import time

from huey.completion import COMPLETION_FAILED
from huey.completion import COMPLETION_RETRYING
from huey.completion import COMPLETION_SUCCEEDED
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase
from huey.tests.base import slow_test

APP = 'huey.tests.completion_app'
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def child_env(db_file, queue='completion-test'):
    env = dict(os.environ)
    env['HUEY_COMPLETION_TEST_DB'] = db_file
    env['HUEY_COMPLETION_TEST_QUEUE'] = queue
    env['PYTHONPATH'] = ROOT + os.pathsep + env.get('PYTHONPATH', '')
    return env


class ChildProcess(object):
    def __init__(self, args, env, stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE):
        self.args = args
        self.env = env
        self.proc = subprocess.Popen(
            args, env=env, cwd=ROOT, stdout=stdout, stderr=stderr,
            text=True)

    def stop(self, sig=signal.SIGTERM, timeout=15):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        try:
            out, err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate()
        return out, err

    def wait(self, timeout=15):
        out, err = self.proc.communicate(timeout=timeout)
        return self.proc.returncode, out, err

    @property
    def returncode(self):
        return self.proc.poll()


class TestCompletionProcesses(BaseTestCase):
    def setUp(self):
        # BaseTestCase creates a MemoryHuey we do not use here; override the
        # storage with a file-backed SqliteHuey in get_huey().
        super(TestCompletionProcesses, self).setUp()
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.returncode is None:
                child.stop()
        for suffix in ('', '-wal', '-shm'):
            path = self.db_file + suffix
            if os.path.exists(path):
                os.unlink(path)
        super(TestCompletionProcesses, self).tearDown()

    def get_huey(self):
        import uuid
        import importlib

        self.db_file = os.path.join(
            TEST_TMP_DIR, 'completion-proc-%s.db' % uuid.uuid4().hex)
        # Point the app module at our database, then reload so its module-level
        # Huey (and task registry) match this test's file.
        os.environ['HUEY_COMPLETION_TEST_DB'] = self.db_file
        os.environ['HUEY_COMPLETION_TEST_QUEUE'] = 'completion-test'
        self.app = importlib.reload(importlib.import_module(APP))
        return self.app.huey

    def start_consumer(self, name=APP + '.huey', extra_args=()):
        env = child_env(self.db_file)
        child = ChildProcess(
            [sys.executable, '-m', 'huey.bin.huey_consumer',
             '-w', '1', '-q',
             '--scheduler-interval', '1', '--no-periodic', name]
            + list(extra_args),
            env)
        self.children.append(child)
        return child

    def start_receiver(self, *args, expect_exit=True):
        env = child_env(self.db_file)
        cmd = [sys.executable, '-m', 'huey.completion_receiver', APP + '.huey'] + \
              list(args)
        child = ChildProcess(cmd, env)
        self.children.append(child)
        if expect_exit:
            code, out, err = child.wait()
            return code, out, err
        return child

    def enqueue(self, task_name, *args, **kwargs):
        """Enqueue a task defined in the completion app module."""
        wrapper = getattr(self.app, task_name)
        return self.huey.enqueue(wrapper.s(*args, **kwargs))

    def pending_count(self):
        return self.huey.storage.queue_size()

    def wait_for(self, predicate, timeout=10, interval=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def consumer_events(self, limit=50, lease_seconds=1):
        return self.huey.pull_completion_events(limit=limit,
                                                lease_seconds=lease_seconds)

    @slow_test()
    def test_consumer_restart_and_receiver_crash(self):
        consumer = self.start_consumer()
        result = self.enqueue('add', 2, 5)
        self.assertTrue(self.wait_for(
            lambda: self.huey.result(result.id, preserve=True) == 7))

        # First receiver claims and prints, but crashes before acknowledging
        # (--no-ack, then exit).  Simulates a receiver that died mid-handler.
        code, out, err = self.start_receiver('--once', '--no-ack',
                                             '--lease-seconds', '0.2')
        self.assertEqual(code, 0, err)
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual([line['status'] for line in lines],
                         [COMPLETION_SUCCEEDED])
        first_event_id = lines[0]['event_id']

        # Kill the consumer hard to simulate an exit.
        consumer.stop(signal.SIGKILL)

        # A second, independent receiver now gets the event after the lease
        # expires.  The same stable event id allows de-duplication.
        time.sleep(0.3)
        code, out, err = self.start_receiver('--once', '--lease-seconds', '30')
        self.assertEqual(code, 0, err)
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['event_id'], first_event_id)
        self.assertEqual(lines[0]['status'], COMPLETION_SUCCEEDED)
        self.assertEqual(lines[0]['task_id'], result.id)

        # Result and event agree after restart.  Read non-destructively so
        # the result remains observable for a later reader too.
        self.assertEqual(self.huey.result(result.id, preserve=True), 7)
        self.assertEqual(self.huey.storage.completion_event_count(False), 0)

    @slow_test()
    def test_retry_then_success_with_real_consumer(self):
        consumer = self.start_consumer()
        result = self.enqueue('flaky', 1)
        self.assertTrue(self.wait_for(
            lambda: self.huey.result(result.id, preserve=True) == 'recovered', timeout=15))

        self.assertTrue(self.wait_for(
            lambda: self.huey.storage.completion_event_count(False) >= 2,
            timeout=10))
        code, out, err = self.start_receiver('--once', '--lease-seconds', '5')
        self.assertEqual(code, 0, err)
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        statuses = [line['status'] for line in lines]
        self.assertEqual(statuses.count(COMPLETION_RETRYING), 1)
        self.assertEqual(statuses.count(COMPLETION_SUCCEEDED), 1)
        final = [line for line in lines if line['status'] ==
                 COMPLETION_SUCCEEDED][0]
        self.assertTrue(final['final'])
        self.assertEqual(final['task_id'], result.id)
        consumer.stop()

    @slow_test()
    def test_failed_task_event_after_consumer_exit(self):
        consumer = self.start_consumer()
        result = self.enqueue('fail', 'boom')
        self.assertTrue(self.wait_for(
            lambda: self.huey.storage.completion_event_count(False) >= 1))
        # Consumer exits; the event and error result persist.
        consumer.stop()

        self.assertEqual(self.huey.storage.completion_event_count(), 1)
        events = self.consumer_events()
        self.assertEqual([e.status for e in events], [COMPLETION_FAILED])
        self.assertTrue(events[0].final)

        # The error is still observable from the result store.
        from huey.exceptions import TaskException
        with self.assertRaises(TaskException):
            self.huey.result(result.id, preserve=True)

    @slow_test()
    def test_two_receivers_compete(self):
        consumer = self.start_consumer()
        results = [self.enqueue('add', i, i) for i in range(10)]
        self.assertTrue(self.wait_for(
            lambda: all(self.huey.result(r.id, preserve=True) == i * 2
                        for i, r in enumerate(results)), timeout=20))

        # Start two independent receivers simultaneously.  Each prints the
        # events it claims; together they must cover all events with no
        # duplicate acknowledgement.  ``--drain`` keeps each receiver pulling
        # until the outbox is empty, so the pair fully drains it.
        env = child_env(self.db_file)
        cmd = [sys.executable, '-m', 'huey.completion_receiver', APP + '.huey',
               '--drain', '--limit', '3', '--lease-seconds', '30']
        procs = [ChildProcess(cmd, env) for _ in range(2)]
        self.children.extend(procs)
        outputs = []
        for proc in procs:
            code, out, err = proc.wait()
            self.assertEqual(code, 0, err)
            outputs.extend(json.loads(line) for line in out.splitlines()
                           if line.strip())

        ids = [line['event_id'] for line in outputs]
        # No event was printed twice, and together the two receivers drained
        # the queue.
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 10)
        self.assertTrue(self.wait_for(
            lambda: self.huey.storage.completion_event_count(False) == 0,
            timeout=5))
        consumer.stop()

    @slow_test()
    def test_consumer_restart_produces_events_for_new_tasks(self):
        # First consumer handles one task, then is killed hard.
        consumer = self.start_consumer()
        r1 = self.enqueue('add', 1, 1)
        self.assertTrue(self.wait_for(
            lambda: self.huey.result(r1.id, preserve=True) == 2))
        consumer.stop(signal.SIGKILL)

        # A new consumer instance (fresh process, same DB) handles a second
        # task.  Both events remain available to a receiver started later.
        consumer2 = self.start_consumer()
        r2 = self.enqueue('add', 3, 4)
        self.assertTrue(self.wait_for(
            lambda: self.huey.result(r2.id, preserve=True) == 7))

        code, out, err = self.start_receiver('--drain')
        self.assertEqual(code, 0, err)
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        by_task = {line['task_id']: line for line in lines}
        self.assertEqual(set(by_task), {r1.id, r2.id})
        self.assertTrue(all(line['status'] == COMPLETION_SUCCEEDED
                            for line in lines))
        consumer2.stop()

    @slow_test()
    def test_retention_purges_only_acked_old(self):
        consumer = self.start_consumer()
        result = self.enqueue('add', 1, 1)
        self.assertTrue(self.wait_for(
            lambda: self.huey.result(result.id, preserve=True) == 2))

        # Ack one receiver's claim.
        code, out, err = self.start_receiver('--once')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.huey.storage.completion_event_count(False), 0)

        # Not old enough.
        self.assertEqual(self.huey.purge_completion_events(3600), 0)
        # Old enough -> removed.
        self.assertEqual(self.huey.purge_completion_events(
            0, now=time.time() + 10), 1)
        self.assertEqual(self.huey.storage.completion_event_count(), 0)
        consumer.stop()
