"""
Task-level integration tests for persistent rate limiting.

These cover the interaction between the persistent limiter and Huey's
retry/schedule machinery: window holds keep the task out of the queue until
the window opens, do not consume failure-retry attempts, and revoked or
expired tasks are never re-enqueued indefinitely.
"""
from huey.exceptions import RateLimitExceeded
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase

import datetime
import os
import sqlite3
import unittest


class _TaskExecutionContract(object):
    def make_huey(self):
        return self.huey

    def test_window_hold_preserves_retry_budget(self):
        huey = self.make_huey()

        @huey.task(retries=2, retry_delay=10)
        @huey.rate_limit('api', limit=1, per=60, persistent=True)
        def task_a(n):
            return n + 1

        r1 = task_a(1)
        r2 = task_a(2)
        # First call consumes the single slot.
        self.assertEqual(self.execute_next(), 2)
        # Second call is held, not executed.
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(huey.scheduled_count(), 1)
        self.assertEqual(huey.pending_count(), 0)

        task, = huey.scheduled()
        # The hold does not spend the task's two failure retries.
        self.assertEqual(task.retries, 2)
        # ETA is the limiter's window boundary, not the task retry_delay.
        age = (task.eta - huey._get_timestamp()).total_seconds()
        self.assertTrue(0.0 < age <= 60.0)
        huey.storage.flush_schedule()

    def test_window_hold_works_without_declared_retries(self):
        huey = self.make_huey()

        @huey.task()
        @huey.rate_limit('api', limit=1, per=60, persistent=True)
        def task_b(n):
            return n + 1

        task_b(1)
        task_b(2)
        self.assertEqual(self.execute_next(), 2)
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(huey.scheduled_count(), 1)
        task, = huey.scheduled()
        self.assertEqual(task.retries, 0)
        huey.storage.flush_schedule()

    def test_retry_false_uses_ordinary_budget_only(self):
        huey = self.make_huey()

        @huey.task(retries=1, retry_delay=600)
        @huey.rate_limit('api', limit=1, per=60, persistent=True,
                         retry=False)
        def task_c(n):
            return n + 1

        task_c(1)
        task_c(2)
        self.assertEqual(self.execute_next(), 2)
        self.assertTrue(self.execute_next() is None)
        task, = huey.scheduled()
        # Ordinary retry path: one failure attempt is spent.
        self.assertEqual(task.retries, 0)
        huey.storage.flush_schedule()

        # A task with *no* budget left, hitting the same exhausted limiter,
        # is terminated instead of being parked forever.
        @huey.task()
        @huey.rate_limit('api', limit=1, per=60, persistent=True,
                         retry=False)
        def task_c2(n):
            return n + 1

        r3 = task_c2(3)
        self.assertTrue(self.execute_next() is None)
        err = self.trap_exception(r3)
        self.assertTrue('RateLimitExceeded' in err.metadata['error'])
        self.assertEqual(huey.scheduled_count(), 0)
        self.assertEqual(huey.pending_count(), 0)

    def test_held_task_runs_after_window_opens(self):
        huey = self.make_huey()

        @huey.task()
        @huey.rate_limit('api', limit=1, per=60, persistent=True)
        def task_d(n):
            return n + 1

        task_d(1)
        task_d(2)
        self.assertEqual(self.execute_next(), 2)
        self.assertTrue(self.execute_next() is None)
        task, = huey.scheduled()

        # Before the ETA it stays scheduled and is not re-enqueued.
        early = huey._get_timestamp() - datetime.timedelta(seconds=1)
        self.assertEqual(huey.read_schedule(early), [])
        self.assertEqual(huey.scheduled_count(), 1)

        # At/after the ETA the scheduler returns it to the queue and it runs.
        due, = huey.read_schedule(task.eta)
        huey.enqueue(due)
        ready = huey.dequeue()
        opened = task.eta + datetime.timedelta(seconds=1)
        self.assertEqual(huey.execute(ready, timestamp=opened), 3)

    def test_revoked_held_task_is_dropped_not_looped(self):
        huey = self.make_huey()

        @huey.task()
        @huey.rate_limit('api', limit=1, per=60, persistent=True)
        def task_e(n):
            return n + 1

        r1 = task_e(1)
        r2 = task_e(2)
        self.assertEqual(self.execute_next(), 2)
        self.assertTrue(self.execute_next() is None)
        held, = huey.scheduled()

        # Revoke while the task waits for the window. The scheduler later
        # returns it to the queue exactly once, when its ETA is due.
        r2.revoke()
        due, = huey.read_schedule(held.eta)
        huey.enqueue(due)
        # Worker dequeues and executes; the execute() gate must drop it
        # instead of running it (which would re-enter the limiter).
        task = huey.dequeue()
        self.assertTrue(huey.execute(task, timestamp=held.eta) is None)
        self.assertEqual(huey.scheduled_count(), 0)
        self.assertEqual(huey.pending_count(), 0)

    def test_expired_held_task_is_dropped_not_looped(self):
        huey = self.make_huey()

        @huey.task()
        @huey.rate_limit('api', limit=1, per=60, persistent=True)
        def task_f(n):
            return n + 1

        expires = huey._get_timestamp() + datetime.timedelta(seconds=30)
        task_f(1)
        task_f(2, expires=expires)
        self.assertEqual(self.execute_next(), 2)
        self.assertTrue(self.execute_next() is None)
        held, = huey.scheduled()

        # The window opens after the task already expired. The scheduler
        # returns it once; the execute() gate drops it without rescheduling.
        future = expires + datetime.timedelta(seconds=60)
        due, = huey.read_schedule(held.eta)
        huey.enqueue(due)
        task = huey.dequeue()
        self.assertTrue(huey.execute(task, timestamp=future) is None)
        self.assertEqual(huey.scheduled_count(), 0)
        self.assertEqual(huey.pending_count(), 0)

    def test_rate_limit_then_ordinary_failure_budgets_are_separate(self):
        huey = self.make_huey()

        @huey.task(retries=1, retry_delay=10)
        @huey.rate_limit('api', limit=1, per=60, persistent=True)
        def limited(n):
            return n + 1

        # First call claims the only slot; second is parked by the limiter
        # and keeps its full ordinary failure-retry budget.
        limited(1)
        limited(2)
        self.assertEqual(self.execute_next(), 2)
        self.assertTrue(self.execute_next() is None)
        held, = huey.scheduled()
        self.assertEqual(held.retries, 1)  # Limiter hold spent nothing.
        huey.storage.flush_schedule()

        # An ordinary failure (separate task, no limiter) still spends one
        # failure-retry attempt, proving the two paths stay independent.
        state = {'fail': True}

        @huey.task(retries=1, retry_delay=10)
        def plain(n):
            if state['fail']:
                state['fail'] = False
                raise ValueError('boom')
            return n + 1

        plain(3)
        self.assertTrue(self.execute_next() is None)
        task, = huey.scheduled()
        self.assertEqual(task.retries, 0)  # Ordinary failure spent one.
        huey.storage.flush_schedule()

    def test_legacy_rate_limit_behavior_is_unchanged(self):
        huey = self.make_huey()

        @huey.task()
        @huey.rate_limit('api', limit=1, per=60)
        def task_h(n):
            return n + 1

        task_h(1)
        task_h(2)
        self.assertEqual(self.execute_next(), 2)
        self.assertTrue(self.execute_next() is None)
        task, = huey.scheduled()
        # Legacy path books an internal retry (retries goes 0 -> 1 -> 0).
        self.assertEqual(task.retries, 0)
        huey.storage.flush_schedule()


class TestPersistentRateLimitTasks(_TaskExecutionContract, BaseTestCase):
    """Runs the contract against MemoryStorage with the real task machinery."""
    def execute_next(self, timestamp=None):
        task = self.huey.dequeue()
        assert task is not None
        return self.huey.execute(task, timestamp=timestamp)


@unittest.skipIf(sqlite3 is None, 'requires sqlite3')
class TestSqlitePersistentRateLimitTasks(_TaskExecutionContract, BaseTestCase):
    def get_huey(self):
        from huey import SqliteHuey
        return SqliteHuey('rl-tasks', filename=self.db_file, utc=False)

    def make_huey(self):
        return self.huey

    def setUp(self):
        # Unique database per test so persisted limiter rows from other tests
        # cannot leak in through the shared limiter name.
        self.db_file = os.path.join(
            TEST_TMP_DIR, 'huey-rl-tasks-%s.db' % self.id().split('.')[-1])
        super(TestSqlitePersistentRateLimitTasks, self).setUp()

    def tearDown(self):
        super(TestSqlitePersistentRateLimitTasks, self).tearDown()
        self.huey.storage.close()
        for suffix in ('', '-wal', '-shm'):
            path = self.db_file + suffix
            if os.path.exists(path):
                os.unlink(path)

    def execute_next(self, timestamp=None):
        task = self.huey.dequeue()
        assert task is not None
        return self.huey.execute(task, timestamp=timestamp)
