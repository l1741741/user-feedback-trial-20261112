import datetime
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from huey.api import MemoryHuey
from huey.api import SqliteHuey
from huey.exceptions import RateLimitExceeded
from huey.exceptions import RetryTask
from huey.exceptions import TaskException
from huey.exceptions import TaskLockedException
from huey.registry import Message
from huey.tests.base import BaseTestCase
from huey.utils import Error
from huey.utils import normalize_time


class ClockHuey(MemoryHuey):
    """MemoryHuey with a settable clock (no system-clock changes needed)."""
    def __init__(self, *args, **kwargs):
        self.clock = None
        super(ClockHuey, self).__init__(*args, **kwargs)

    def _get_timestamp(self):
        if self.clock is not None:
            return self.clock
        return super(ClockHuey, self)._get_timestamp()


def module_backoff(base, attempt):
    # Module-level so it survives pickling when the task is serialized.
    return base * (2 ** attempt)


class TestRetryDeadline(BaseTestCase):
    def get_huey(self):
        return ClockHuey(utc=False)

    def setUp(self):
        super(TestRetryDeadline, self).setUp()
        self.t0 = datetime.datetime(2026, 11, 20, 9, 0, 0)
        self.huey.clock = self.t0

    def advance(self, seconds):
        self.huey.clock += datetime.timedelta(seconds=seconds)
        return self.huey.clock

    def run_scheduled(self):
        # Dequeue the scheduled retry and execute it at its eta.
        task, = self.huey.scheduled()
        self.huey.storage.flush_schedule()
        self.huey.clock = task.eta
        return task, self.huey.execute(task, task.eta)

    def test_deadline_resolved_once_at_first_enqueue(self):
        @self.huey.task(retries=3, retry_delay=10, retry_deadline=60)
        def task_a():
            raise ValueError('nope')

        r = task_a()
        task = self.huey.dequeue()
        self.assertEqual(task.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=60))

        # A retry does not reset the starting point.
        self.advance(5)
        self.assertTrue(self.huey.execute(task) is None)
        retry, = self.huey.scheduled()
        self.assertEqual(retry.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=60))
        self.assertEqual(retry.retry_attempt, 1)

        # Nor does serialization / deserialization.
        task2 = self.huey.deserialize_task(self.huey.serialize_task(retry))
        self.assertEqual(task2.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=60))
        self.assertEqual(task2.retry_attempt, 1)
        self.assertEqual(task2.retry_deadline, 60)

    def test_deadline_not_reset_by_schedule_or_reenqueue(self):
        @self.huey.task(retries=3, retry_delay=10, retry_deadline=60)
        def task_a():
            raise ValueError('nope')

        # Delayed (eta) scheduling: budget starts at publish, not at eta.
        r = task_a.schedule(delay=3600)
        task = self.huey.dequeue()
        self.assertEqual(task.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=60))

        # The consumer sees the task is not ready and moves it to the
        # schedule; the deadline is unchanged.
        self.huey.execute(task)
        sched, = self.huey.scheduled()
        self.assertEqual(sched.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=60))

        # The scheduler moving a task back onto the queue (or any other
        # re-enqueue) must not re-resolve the deadline.
        self.huey.storage.flush_schedule()
        self.advance(30)
        self.huey.enqueue(sched)
        task = self.huey.dequeue()
        self.assertEqual(task.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=60))

    def test_retry_within_deadline_is_published(self):
        @self.huey.task(retries=2, retry_delay=10, retry_deadline=60)
        def task_a():
            raise ValueError('nope')

        r = task_a()
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(len(self.huey), 0)
        self.assertEqual(self.huey.scheduled_count(), 1)

        task, = self.huey.scheduled()
        self.assertEqual(task.eta, self.t0 + datetime.timedelta(seconds=10))
        self.assertEqual(task.retries, 1)

    def test_retry_beyond_deadline_is_never_published(self):
        @self.huey.task(retries=5, retry_delay=10, retry_deadline=20)
        def task_a():
            raise ValueError('nope')

        r = task_a()
        # Fails at t0; retry eta t0+10 is within budget (deadline t0+20).
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(self.huey.scheduled_count(), 1)

        # Fails at t0+10; retry eta t0+20 == deadline, still allowed.
        task, _ = self.run_scheduled()
        self.assertEqual(self.huey.scheduled_count(), 1)

        # Fails at t0+20; next retry eta t0+30 exceeds the deadline. The
        # retry is not published -- the task fails finally instead.
        task, _ = self.run_scheduled()
        self.assertEqual(self.huey.scheduled_count(), 0)
        self.assertEqual(len(self.huey), 0)

        exc = self.trap_exception(r.get)
        self.assertEqual(exc.metadata['retries'], 3)  # 2 of 5 retries used.
        self.assertIn('ValueError', exc.metadata['error'])

    def test_immediate_retry_beyond_deadline(self):
        @self.huey.task(retries=5, retry_deadline=10)
        def task_a():
            raise ValueError('nope')

        r = task_a()
        # No retry_delay: retried immediately while within budget.
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(len(self.huey), 1)

        # Once the budget has elapsed, the immediate retry is not published.
        self.advance(11)
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(len(self.huey), 0)
        self.assertEqual(self.huey.scheduled_count(), 0)
        self.trap_exception(r.get)

    def test_zero_deadline_disables_retries(self):
        @self.huey.task(retries=3, retry_delay=10, retry_deadline=0)
        def task_a():
            raise ValueError('nope')

        r = task_a()
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(len(self.huey), 0)
        self.assertEqual(self.huey.scheduled_count(), 0)
        self.trap_exception(r.get)

    def test_absolute_datetime_deadline(self):
        deadline = self.t0 + datetime.timedelta(seconds=30)

        @self.huey.task(retries=5, retry_delay=10, retry_deadline=deadline)
        def task_a():
            raise ValueError('nope')

        r = task_a()
        task = self.huey.dequeue()
        self.assertEqual(task.retry_deadline_resolved, deadline)

    def test_aware_and_naive_datetime_deadline(self):
        aware = datetime.datetime(2026, 11, 20, 10, 0, 0,
                                  tzinfo=datetime.timezone.utc)

        huey_utc = ClockHuey(utc=True)
        @huey_utc.task(retries=1, retry_deadline=aware)
        def task_u():
            pass
        task = task_u.s()
        huey_utc.enqueue(task)
        self.assertEqual(task.retry_deadline_resolved,
                         normalize_time(eta=aware, utc=True))
        self.assertIsNone(task.retry_deadline_resolved.tzinfo)

        huey_local = ClockHuey(utc=False)
        @huey_local.task(retries=1, retry_deadline=aware)
        def task_l():
            pass
        task = task_l.s()
        huey_local.enqueue(task)
        self.assertEqual(task.retry_deadline_resolved,
                         normalize_time(eta=aware, utc=False))
        self.assertIsNone(task.retry_deadline_resolved.tzinfo)

    def test_timedelta_deadline(self):
        @self.huey.task(retries=1, retry_deadline=datetime.timedelta(
            seconds=45))
        def task_a():
            pass

        r = task_a()
        task = self.huey.dequeue()
        self.assertEqual(task.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=45))

    def test_deadline_validation(self):
        @self.huey.task()
        def task_a():
            pass

        # Negative durations and non-finite values are rejected.
        for bad in (-1, -0.5, float('inf'), float('-inf'), float('nan'),
                    datetime.timedelta(seconds=-1)):
            self.assertRaises(ValueError, self.huey.task, retry_deadline=bad)
            self.assertRaises(ValueError, task_a.s, retry_deadline=bad)

        # Booleans and non-numeric types are rejected.
        for bad in (True, False, '10', object()):
            self.assertRaises(TypeError, self.huey.task, retry_deadline=bad)
            self.assertRaises(TypeError, task_a.s, retry_deadline=bad)

        # Zero, seconds, timedeltas and datetimes are accepted.
        for good in (0, 10, 10.5, datetime.timedelta(seconds=10),
                     self.t0, None):
            self.huey.task(retry_deadline=good)(lambda: None).unregister()
            task_a.s(retry_deadline=good)

    def test_backoff_max_validation(self):
        for bad in (-1, float('inf'), float('nan')):
            self.assertRaises(ValueError, self.huey.task, retry_backoff_max=bad)
        for bad in (True, '10'):
            self.assertRaises(TypeError, self.huey.task, retry_backoff_max=bad)

        @self.huey.task(retry_backoff_max=datetime.timedelta(seconds=30))
        def task_a():
            pass
        # Timedeltas are normalized to seconds on the task instance.
        self.assertEqual(task_a.s().retry_backoff_max, 30.0)

    def test_jitter_validation(self):
        for bad in (-1, float('inf'), float('nan')):
            self.assertRaises(ValueError, self.huey.task, retry_jitter=bad)
        for bad in ('10', object()):
            self.assertRaises(TypeError, self.huey.task, retry_jitter=bad)

        @self.huey.task(retry_jitter=datetime.timedelta(seconds=2))
        def task_a():
            pass
        self.assertEqual(task_a.s().retry_jitter, 2.0)

    def test_retrytask_eta_delay_vs_deadline(self):
        @self.huey.task(retries=5, retry_deadline=20)
        def task_a(mode):
            if mode == 'eta':
                raise RetryTask(eta=self.t0 + datetime.timedelta(seconds=30))
            elif mode == 'delay':
                raise RetryTask(delay=5)
            raise RetryTask()

        # RetryTask with an explicit eta beyond the deadline: final failure.
        r = task_a(mode='eta')
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(self.huey.scheduled_count(), 0)
        self.assertEqual(len(self.huey), 0)
        self.trap_exception(r.get)

        # RetryTask with a delay within the deadline: retried.
        r = task_a(mode='delay')
        self.assertTrue(self.execute_next() is None)
        task, = self.huey.scheduled()
        self.assertEqual(task.eta, self.t0 + datetime.timedelta(seconds=5))
        self.huey.storage.flush_schedule()

        # RetryTask without eta/delay: immediate retry within the deadline.
        r = task_a(mode='none')
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(len(self.huey), 1)

    def test_rate_limit_vs_deadline(self):
        @self.huey.task(retries=5, retry_deadline=10)
        def task_a(delay):
            raise RateLimitExceeded('key', delay, retry=True)

        # Rate-limit retry beyond the deadline: final failure, not published.
        r = task_a(30)
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(self.huey.scheduled_count(), 0)
        self.assertEqual(len(self.huey), 0)
        self.trap_exception(r.get)

        # Within the deadline: scheduled normally.
        r = task_a(5)
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(self.huey.scheduled_count(), 1)

    def test_lock_failure_vs_deadline(self):
        @self.huey.task(retries=5, retry_delay=30, retry_deadline=10)
        def task_a():
            raise TaskLockedException('locked')

        r = task_a()
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(self.huey.scheduled_count(), 0)
        self.assertEqual(len(self.huey), 0)
        self.trap_exception(r.get)

    def test_final_failure_result_signals_and_error_handler(self):
        events = []
        @self.huey.signal()
        def recorder(sig, task, *args, **kwargs):
            events.append(sig)

        @self.huey.task(retries=10, retry_delay=10, retry_deadline=15)
        def task_a():
            raise ValueError('nope')

        @self.huey.task()
        def err_handler(exc):
            pass

        t = task_a.s()
        t.error(err_handler.s())
        r = self.huey.enqueue(t)

        # First failure at t0: retried (eta t0+10 <= deadline t0+15).
        self.assertTrue(self.execute_next() is None)
        # Second failure at t0+10: next eta t0+20 exceeds deadline -> final.
        self.run_scheduled()

        self.assertEqual(events.count('retrying'), 1)
        self.assertEqual(events.count('error'), 2)
        self.assertEqual(self.huey.scheduled_count(), 0)

        # Exactly one on_error task per failure; nothing else is pending, so
        # no further alerts can be emitted.
        pending = self.huey.pending()
        self.assertEqual(len(pending), 2)
        self.assertTrue(all(isinstance(p.args[0], ValueError)
                            for p in pending))

        exc = self.trap_exception(r.get)
        self.assertEqual(exc.metadata['retries'], 9)  # Cut by deadline.
        self.assertIn('ValueError', exc.metadata['error'])

    def test_backoff_max_caps_delay(self):
        @self.huey.task(retries=3, retry_delay=10, retry_backoff=10,
                        retry_backoff_max=15)
        def task_a():
            raise ValueError('nope')

        r = task_a()
        self.assertTrue(self.execute_next() is None)

        # Waits are 10, then capped at 15; the serialized (grown) retry_delay
        # never exceeds the ceiling either.
        for delay, wait in ((15, 10), (15, 15), (15, 15)):
            task, = self.huey.scheduled()
            self.assertEqual(task.retry_delay, delay)
            self.assertEqual(task.eta, self.huey.clock +
                             datetime.timedelta(seconds=wait))
            self.run_scheduled()

        self.assertEqual(self.huey.scheduled_count(), 0)
        self.assertEqual(len(self.huey), 0)

    def test_full_jitter_with_injected_random(self):
        @self.huey.task(retries=2, retry_delay=10, retry_jitter=True,
                        retry_random=random.Random(1234))
        def task_a():
            raise ValueError('nope')

        r = task_a()
        self.assertTrue(self.execute_next() is None)

        # Full jitter waits a uniform random amount in [0, delay]; the
        # injected random source makes it reproducible.
        expected = random.Random(1234).uniform(0, 10)
        task, = self.huey.scheduled()
        self.assertEqual(task.eta,
                         self.t0 + datetime.timedelta(seconds=expected))

    def test_additive_jitter_with_injected_random(self):
        @self.huey.task(retries=2, retry_delay=10, retry_jitter=5,
                        retry_random=random.Random(99))
        def task_a():
            raise ValueError('nope')

        r = task_a()
        self.assertTrue(self.execute_next() is None)

        expected = 10 + random.Random(99).uniform(0, 5)
        task, = self.huey.scheduled()
        self.assertEqual(task.eta,
                         self.t0 + datetime.timedelta(seconds=expected))

    def test_full_jitter_never_exceeds_backoff_max(self):
        @self.huey.task(retries=5, retry_delay=10, retry_backoff=10,
                        retry_backoff_max=6, retry_jitter=True,
                        retry_random=random.Random(7))
        def task_a():
            raise ValueError('nope')

        r = task_a()
        self.assertTrue(self.execute_next() is None)
        for _ in range(5):
            task, = self.huey.scheduled()
            wait = (task.eta - self.huey.clock).total_seconds()
            self.assertTrue(0 <= wait <= 6)
            self.run_scheduled()
        self.assertEqual(self.huey.scheduled_count(), 0)

    def test_callable_backoff_attempt_survives_serialization(self):
        @self.huey.task(retries=3, retry_delay=10,
                        retry_backoff=module_backoff)
        def task_a():
            raise ValueError('nope')

        r = task_a()
        self.assertTrue(self.execute_next() is None)

        # The backoff sequence index is persisted with the task, so it does
        # not regress when the task is deserialized after each requeue.
        for attempt, wait in ((1, 10), (2, 20), (3, 40)):
            task, = self.huey.scheduled()
            self.assertEqual(task.retry_attempt, attempt)
            self.assertEqual(task.eta, self.huey.clock +
                             datetime.timedelta(seconds=wait))
            self.run_scheduled()

        self.assertEqual(self.huey.scheduled_count(), 0)
        self.trap_exception(r.get)

    def test_pipeline_members_have_independent_deadlines(self):
        @self.huey.task(retries=1, retry_deadline=30)
        def task_a():
            return 'a'

        @self.huey.task(retries=1, retry_deadline=50)
        def task_b(prev):
            return prev

        pipe = task_a.s().then(task_b.s())
        r = self.huey.enqueue(pipe)

        head = self.huey.dequeue()
        self.assertEqual(head.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=30))
        # The pipeline member's budget has not started yet.
        self.assertIsNone(head.on_complete.retry_deadline_resolved)

        # When the member is enqueued later, it gets its own budget starting
        # at its own publish time; no state is shared with the parent.
        self.advance(5)
        self.huey.execute(head)
        member = self.huey.dequeue()
        self.assertEqual(member.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=55))
        self.assertIsNot(member.retry_deadline_resolved,
                         head.retry_deadline_resolved)
        self.assertEqual(head.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=30))

    def test_reschedule_starts_fresh_budget(self):
        @self.huey.task(retries=2, retry_delay=5, retry_deadline=60)
        def task_a():
            raise ValueError('nope')

        r = task_a()
        task = self.huey.dequeue()
        self.assertEqual(task.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=60))

        # reschedule() publishes a new task: the deadline configuration is
        # preserved, but the budget restarts at the new publish time.
        self.advance(10)
        r2 = r.reschedule()
        new_task = self.huey.dequeue()
        self.assertNotEqual(new_task.id, task.id)
        self.assertEqual(new_task.retry_deadline, 60)
        self.assertEqual(new_task.retry_attempt, 0)
        self.assertEqual(new_task.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=70))

    def test_message_roundtrip_preserves_budget_state(self):
        @self.huey.task(retries=3, retry_delay=10, retry_backoff=2,
                        retry_deadline=60, retry_backoff_max=25,
                        retry_jitter=True)
        def task_a():
            raise ValueError('nope')

        r = task_a()
        self.assertTrue(self.execute_next() is None)
        task, = self.huey.scheduled()

        self.assertEqual(task.retry_deadline, 60)
        self.assertEqual(task.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=60))
        self.assertEqual(task.retry_attempt, 1)
        self.assertEqual(task.retry_backoff_max, 25)
        self.assertEqual(task.retry_jitter, True)
        self.assertEqual(task.retry_delay, 20)

    def test_legacy_message_defaults(self):
        @self.huey.task(retries=1)
        def task_a():
            pass

        name = self.huey._registry.task_to_string(task_a.task_class)
        # A message serialized before the retry-budget fields existed.
        msg = Message('tid', name, None, 1, 0, None, (), {}, None, None,
                      None, None, None, None, None)
        task = self.huey._registry.create_task(msg)
        self.assertEqual(task.retry_attempt, 0)
        self.assertIsNone(task.retry_deadline)
        self.assertIsNone(task.retry_deadline_resolved)
        self.assertIsNone(task.retry_backoff_max)
        self.assertIsNone(task.retry_jitter)

    def test_per_call_overrides(self):
        @self.huey.task(retries=3, retry_delay=10)
        def task_a():
            raise ValueError('nope')

        r = task_a(retry_deadline=30, retry_backoff_max=8, retry_jitter=False)
        task = self.huey.dequeue()
        self.assertEqual(task.retry_deadline, 30)
        self.assertEqual(task.retry_backoff_max, 8)
        self.assertIsNone(task.retry_jitter)
        self.assertEqual(task.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=30))

        r = task_a.schedule(delay=5, retry_deadline=datetime.timedelta(
            seconds=45))
        task = self.huey.dequeue()
        self.assertEqual(task.retry_deadline, datetime.timedelta(seconds=45))
        self.assertEqual(task.retry_deadline_resolved,
                         self.t0 + datetime.timedelta(seconds=45))


class TestRetryDeadlineSqliteIntegration(unittest.TestCase):
    """
    Prove, with a real SQLite broker, a separate publisher process and a
    consumer process that is restarted mid-budget, that the retry deadline
    is anchored at the first publish and is not reset by the restart.
    """
    def test_budget_survives_consumer_restart(self):
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        tmpdir = tempfile.mkdtemp(prefix='huey-retry-deadline-')
        db = os.path.join(tmpdir, 'huey.db')
        log = os.path.join(tmpdir, 'attempts.log')

        env = dict(os.environ)
        env['HUEY_IT_DB'] = db
        env['HUEY_IT_LOG'] = log
        env['HUEY_IT_DEADLINE'] = '3.0'
        env['PYTHONPATH'] = repo + os.pathsep + env.get('PYTHONPATH', '')

        cmd = [sys.executable, '-m', 'huey.tests.retry_deadline_worker']

        # Separate publisher process.
        pub = subprocess.run(cmd + ['publish'], cwd=repo, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(pub.returncode, 0, pub.stderr)
        task_id, ts = pub.stdout.split()
        publish_ts = float(ts)
        deadline_ts = publish_ts + 3.0

        consumers = []
        def start_consumer():
            p = subprocess.Popen(cmd + ['consume'], cwd=repo, env=env,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            consumers.append(p)
            return p

        def read_attempts():
            if not os.path.exists(log):
                return []
            with open(log) as fh:
                return [float(line) for line in fh if line.strip()]

        try:
            c1 = start_consumer()

            # Wait for the first attempts, then stop the consumer gracefully
            # (SIGINT) in the middle of the budget window.
            give_up = time.time() + 30
            while not read_attempts() and time.time() < give_up:
                time.sleep(0.05)
            self.assertTrue(read_attempts(), 'consumer 1 ran no attempts')
            time.sleep(0.5)
            c1.send_signal(signal.SIGINT)
            self.assertEqual(c1.wait(timeout=30), 0)
            kill_ts = time.time()
            self.assertLess(kill_ts, deadline_ts,
                            'test too slow: budget elapsed before restart')

            # Restart the consumer; the retry budget must carry over.
            c2 = start_consumer()

            # Wait until the deadline has fully passed, then verify no
            # further attempts are appended.
            time.sleep(max(0, deadline_ts + 1.5 - time.time()))
            count = len(read_attempts())
            time.sleep(0.5)
            self.assertEqual(len(read_attempts()), count,
                             'attempts continued past the retry deadline')
            c2.send_signal(signal.SIGINT)
            self.assertEqual(c2.wait(timeout=30), 0)
        finally:
            for p in consumers:
                if p.poll() is None:
                    p.kill()
                    p.wait()

        attempts = read_attempts()
        self.assertGreaterEqual(len(attempts), 3)
        self.assertLess(len(attempts), 10000)

        # The restarted consumer did execute retries (state survived).
        self.assertTrue(any(t > kill_ts for t in attempts),
                        'no attempts after consumer restart')

        # Every attempt happened within the budget anchored at the first
        # publish -- the restart did not reset or extend it.
        self.assertLessEqual(max(attempts), deadline_ts + 1.0)
        self.assertGreaterEqual(min(attempts), publish_ts - 1.0)

        # The final result expresses the (final) failure, with retries left
        # over -- processing was stopped by the deadline, not the count.
        huey = SqliteHuey('retry-deadline-it', filename=db)
        err = huey.get(task_id)
        self.assertIsInstance(err, Error)
        self.assertGreater(err.metadata['retries'], 0)
        self.assertIn('RuntimeError', err.metadata['error'])


if __name__ == '__main__':
    unittest.main()
