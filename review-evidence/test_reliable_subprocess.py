"""
Real-subprocess tests for the opt-in reliable consumer mode.

These launch the helper process rather than threads so that process death,
lease takeover, renewal across a long task, and retry handoff are exercised
the way they behave in production.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from huey import SqliteHuey
from huey.tests import reliable_tasks


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
HELPER = 'huey.tests.reliable_helper'


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


class SubprocessReliableTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix='huey-reliable-sub-')
        self.db = os.path.join(self.tmp_dir, 'huey.db')
        self.log = self.db + '.log'
        self.huey = SqliteHuey('rel', filename=self.db, utc=False)
        self.archive, self.flaky = reliable_tasks.configure(self.huey,
                                                            self.log)
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if proc.stdout is not None:
                proc.stdout.close()
        self.huey.storage.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def spawn(self, mode, lease=5):
        argv = [sys.executable, '-m', HELPER, mode, self.db, str(lease)]
        env = dict(os.environ)
        env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
        proc = subprocess.Popen(argv, cwd=REPO_ROOT, env=env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        self.procs.append(proc)
        return proc

    def stop_consumer(self, proc):
        proc.send_signal(signal.SIGINT)
        output = proc.communicate(timeout=15)[0]
        return proc.returncode, output

    def read_log(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [line.strip() for line in fh if line.strip()]

    def log_entries(self, kind):
        return [e for e in self.read_log() if e.split(':', 1)[0] == kind]

    def wait_for_log(self, kind, count=1, timeout=10):
        return wait_for(lambda: len(self.log_entries(kind)) >= count,
                        timeout=timeout)

    def wait_for_consumer_pid(self, timeout=10):
        return wait_for(lambda: os.path.exists(self.db + '.pid'),
                        timeout=timeout)


class TestCrashRecovery(SubprocessReliableTestCase):
    def test_killed_consumer_delivery_is_reclaimed(self):
        # Enqueue, then have a real subprocess claim and die mid-delivery.
        self.archive('job-1')
        proc = self.spawn('crash', lease=1)
        self.assertEqual(proc.wait(timeout=10), 17)
        with open(self.db + '.claim') as fh:
            claim = json.load(fh)
        self.assertTrue(claim['claimed'])

        # The delivery is still recorded after the process disappeared.
        self.assertEqual(self.huey.in_flight_count(), 1)
        # ...but is not claimable until the lease expires.
        self.assertIsNone(self.huey.storage.claim_lease(1))

        time.sleep(1.2)
        # A second real process reclaims it without any explicit cleanup.
        self.spawn('reclaim', lease=5).wait(timeout=10)
        with open(self.db + '.reclaim') as fh:
            reclaimed = json.load(fh)
        self.assertEqual(reclaimed, [{'task': claim['task'], 'attempt': 2}])
        self.assertEqual(self.huey.in_flight_count(), 0)

    def test_exited_consumer_does_not_ack_live_task(self):
        # A task that completed but was not acked before the process died is
        # delivered again -- the documented at-least-once window.
        self.archive('job-2')
        proc = self.spawn('exit-after-claim', lease=1)
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual(self.huey.in_flight_count(), 1)

        time.sleep(1.2)
        self.spawn('reclaim', lease=5).wait(timeout=10)
        with open(self.db + '.reclaim') as fh:
            self.assertEqual(len(json.load(fh)), 1)
        self.assertEqual(self.huey.in_flight_count(), 0)


class TestConsumerSubprocess(SubprocessReliableTestCase):
    def test_real_consumer_recovers_after_kill(self):
        self.archive('job-1')
        self.spawn('crash', lease=1).wait(timeout=10)
        with open(self.db + '.claim') as fh:
            claim = json.load(fh)
        self.assertTrue(claim['claimed'])
        time.sleep(1.1)

        # Start a real consumer; it recovers the orphaned delivery.
        consumer = self.spawn('consume', lease=1)
        self.assertTrue(self.wait_for_consumer_pid())
        self.assertTrue(self.wait_for_log('done', 1, timeout=15))
        wait_for(lambda: self.huey.in_flight_count() == 0, timeout=10)
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 0)

        code, output = self.stop_consumer(consumer)
        self.assertEqual(code, 0, output.decode('utf8', 'replace'))

    def test_long_task_renews_lease(self):
        self.archive('long-job', seconds=2.5)
        consumer = self.spawn('consume', lease=0.6)
        self.assertTrue(self.wait_for_consumer_pid())
        self.assertTrue(self.wait_for_log('start', 1, timeout=10))

        # While the task runs, a competing claim in this process must keep
        # failing even though the initial 0.6s lease has long expired.
        time.sleep(1.5)
        self.assertIsNone(self.huey.storage.claim_lease(0.6))
        self.assertEqual(self.huey.in_flight_count(), 1)

        self.assertTrue(self.wait_for_log('done', 1, timeout=10))
        wait_for(lambda: self.huey.in_flight_count() == 0, timeout=10)
        self.assertEqual(self.huey.in_flight_count(), 0)

        code, output = self.stop_consumer(consumer)
        self.assertEqual(code, 0, output.decode('utf8', 'replace'))
        self.assertEqual(self.huey.in_flight_count(), 0)

    def test_retry_handoff_across_processes(self):
        self.flaky('flaky-1')
        consumer = self.spawn('consume', lease=1)
        self.assertTrue(self.wait_for_consumer_pid())

        # The failing task is retried once, then acknowledged after the final
        # failure. Exactly two attempts, no duplicate retries.
        self.assertTrue(self.wait_for_log('flaky', 2, timeout=15))
        wait_for(lambda: self.huey.in_flight_count() == 0, timeout=10)
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(self.huey.scheduled_count(), 0)
        self.assertEqual(len(self.log_entries('flaky')), 2)

        code, output = self.stop_consumer(consumer)
        self.assertEqual(code, 0, output.decode('utf8', 'replace'))

    def test_graceful_shutdown_stops_claiming_but_finishes_inflight(self):
        self.archive('long-job', seconds=2)
        self.archive('queued-job')
        consumer = self.spawn('consume', lease=1)
        self.assertTrue(self.wait_for_consumer_pid())
        self.assertTrue(self.wait_for_log('start', 1, timeout=10))

        # Graceful signal during the long task: it must finish and be acked,
        # while the queued task is left untouched.
        consumer.send_signal(signal.SIGINT)
        code = consumer.wait(timeout=15)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.db + '.pid'))

        names = [e.split(':')[1] for e in self.log_entries('done')]
        self.assertEqual(names, ['long-job'])
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 1)
        task, = self.huey.pending()
        self.assertEqual(task.args[0], 'queued-job')

    def test_recovery_preempts_backlog(self):
        # An orphaned delivery is recovered before fresh backlog, so an
        # interrupted task is not starved behind new work. Priority still
        # decides the order among expired deliveries and among new tasks.
        self.archive('job-1')
        self.spawn('crash', lease=1).wait(timeout=10)
        with open(self.db + '.claim') as fh:
            claim = json.load(fh)
        self.assertEqual(self.huey.in_flight_count(), 1)

        # Fresh backlog enqueued after the crash.
        self.archive('job-2', priority=100)
        time.sleep(1.1)

        reclaimed = self.huey.storage.claim_lease(5)
        self.assertIsNotNone(reclaimed)
        self.assertEqual(
            self.huey.deserialize_task(reclaimed.data).args[0], 'job-1')
        self.assertTrue(self.huey.storage.ack_lease(reclaimed))
        # The high-priority backlog is still there, untouched.
        self.assertEqual(self.huey.pending_count(), 1)
        task, = self.huey.pending()
        self.assertEqual(task.args[0], 'job-2')
        self.assertEqual(claim['attempt'], 1)

    def test_forced_shutdown_keeps_recovery_evidence(self):
        self.archive('long-job', seconds=30)
        consumer = self.spawn('consume', lease=0.6)
        self.assertTrue(self.wait_for_consumer_pid())
        self.assertTrue(self.wait_for_log('start', 1, timeout=10))

        # SIGTERM is the interrupt signal by default: the worker is
        # interrupted mid-task and the delivery must remain on record.
        consumer.send_signal(signal.SIGTERM)
        self.assertEqual(consumer.wait(timeout=15), 0)
        self.assertEqual(self.log_entries('done'), [])
        self.assertEqual(self.huey.in_flight_count(), 1)

        # The lease was being renewed while the task ran, so it may still be
        # live; wait it out, then verify another consumer can recover it.
        time.sleep(1.0)
        reclaimed = self.huey.claim_lease(5)
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.attempt, 2)
        self.assertTrue(reclaimed.ack())
        self.assertEqual(self.huey.in_flight_count(), 0)


if __name__ == '__main__':
    unittest.main()
