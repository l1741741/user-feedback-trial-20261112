import datetime
import os
import threading
import time
import unittest

try:
    import peewee
except ImportError:
    peewee = None

from huey.api import MemoryHuey
from huey.api import SqliteHuey
from huey.api import chord
from huey.api import group
from huey.constants import EmptyData
from huey.exceptions import ConfigurationError
from huey.exceptions import ResultTimeout
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase

try:
    from huey.contrib.sql_huey import SqlHuey
except ImportError:
    SqlHuey = None


class RetentionError(Exception): pass


class RetentionBase(object):
    # Subclasses implement get_retention_huey().

    def test_disabled_by_default(self):
        self.assertIsNone(self.huey.result_retention)
        @self.huey.task()
        def task_a(n):
            return n + 1

        r = task_a(1)
        self.assertEqual(self.execute_next(), 2)
        self.assertEqual(r(), 2)
        # Rewrite through the result API and confirm nothing expires.
        self.assertEqual(self.huey.purge_expired_results(
            now=time.time() + 10 ** 9), 0)
        self.assertEqual(self.huey.result_count(), 0)

    def test_retention_is_exposed(self):
        h = self.get_retention_huey(30)
        self.assertEqual(h.result_retention, 30)
        self.assertEqual(h.storage.result_retention, 30)


class RetentionSemantics(object):
    """Shared behavior for MemoryHuey and SqliteHuey."""

    def test_result_expires_after_retention(self):
        huey = self.get_retention_huey(0.2)
        self.huey = huey

        @huey.task()
        def task_a(n):
            return n + 1

        r = task_a(1)
        self.assertEqual(self.execute_next(), 2)
        self.assertEqual(r(), 2)

        # A second consumer of the same result sees a "not ready" handle after
        # the retention period, and a fresh Result handle agrees.
        r2 = task_a(2)
        self.assertEqual(self.execute_next(), 3)
        time.sleep(0.25)
        fresh = type(r2)(huey, r2.task)
        self.assertFalse(fresh.is_ready())
        self.assertTrue(fresh._get() is EmptyData)
        self.assertEqual(huey.result_count(), 0)

    def test_retrying_error_not_visible_as_final(self):
        huey = self.get_retention_huey(0.1)
        self.huey = huey
        attempts = []

        @huey.task(retries=1)
        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise RetentionError('first attempt')
            return 'ok'

        r = flaky()
        # Intermediate error is stored (store_intermediate_errors default).
        self.assertEqual(self.execute_next(), None)
        self.assertTrue(huey.storage.has_data_for_key(r.id))
        self.assertEqual(len(huey), 1)  # Retry is still queued.

        # The intermediate error is retained as an ordinary result until its
        # own retention period elapses, even though the task is still retrying.
        time.sleep(0.15)
        self.assertEqual(huey.storage.purge_expired_results(), 1)
        self.assertFalse(huey.storage.has_data_for_key(r.id))

        # The retry re-runs; its final success has its own expiration.
        self.assertEqual(self.execute_next(), 'ok')
        self.assertEqual(r.get(), 'ok')

    def test_result_group_expiry(self):
        huey = self.get_retention_huey(0.2)
        self.huey = huey

        @huey.task()
        def task_a(n):
            return n * 10

        group_result = group([task_a.s(1), task_a.s(2), task_a.s(3)])
        rg = huey.enqueue(group_result)
        for _ in range(3):
            self.execute_next()
        self.assertEqual(rg.get(), [10, 20, 30])

        time.sleep(0.25)
        # After expiry, fresh handles for the group members see no result even
        # though the original handles cached the values when read.
        self.assertEqual(huey.result_count(), 0)
        for result in rg:
            fresh = type(result)(huey, result.task)
            self.assertFalse(fresh.is_ready())
            self.assertTrue(fresh._get() is EmptyData)
            self.assertTrue(fresh.get() is None)

    def test_chord_bookkeeping_survives_retention(self):
        # Chord bookkeeping is control data. With zero retention the member
        # *task results* expire immediately, but the collected values and the
        # counter storing the chord's state must survive so the callback fires
        # exactly once with the full set of values.
        huey = self.get_retention_huey(0)
        self.huey = huey
        collected = []

        @huey.task()
        def prod(n):
            return n + 1

        @huey.task()
        def agg(ns):
            collected.append(ns)
            return sum(ns)

        result = huey.enqueue(chord([prod.s(1), prod.s(2), prod.s(3)], agg))
        for _ in range(4):
            self.execute_next()

        self.assertEqual(collected, [[2, 3, 4]])
        # The member task-results are ordinary results and expired; the
        # callback's result is too.
        self.assertEqual(huey.result_count(), 0)
        self.assertTrue(result() is None)

    def test_blocking_wait_does_not_see_expired_result(self):
        huey = self.get_retention_huey(0)
        self.huey = huey

        @huey.task()
        def task_a():
            return 1
        r = task_a()
        self.execute_next()
        # Written with zero retention; there is nothing to wait for.
        with self.assertRaises(ResultTimeout):
            r.get(blocking=True, timeout=0.1)


class MemoryRetentionTests(RetentionSemantics, RetentionBase, BaseTestCase):
    def get_huey(self):
        return MemoryHuey(utc=False)

    def get_retention_huey(self, retention):
        return MemoryHuey(utc=False, result_retention=retention)

    def test_result_expiry_does_not_touch_control_keys(self):
        huey = self.get_retention_huey(0)
        self.huey = huey
        storage = huey.storage

        # Control state in the same keyspace.
        huey.revoke_by_id('abc', revoke_until=datetime.datetime(2999, 1, 1))
        lock = huey.lock_task('mylock')
        lock.acquire()
        rl = huey.rate_limit('api', 10, 60)
        rl.acquire()

        @huey.task()
        def task_a():
            return 1
        r = task_a()
        self.execute_next()

        now = time.time()
        self.assertTrue(storage.peek_data(r.id) is EmptyData)
        self.assertEqual(storage.purge_expired_results(now=now + 10 ** 6), 0)

        self.assertTrue(huey.is_revoked('abc'))
        self.assertTrue(lock.is_locked())
        self.assertEqual(rl.current_usage(), 1)


@unittest.skipIf(peewee is None, 'requires peewee')
class SqlHueyRetentionTests(RetentionBase, BaseTestCase):
    db_file = os.path.join(TEST_TMP_DIR, 'huey-retention.db')

    def setUp(self):
        if os.path.exists(self.db_file):
            os.unlink(self.db_file)
        super(SqlHueyRetentionTests, self).setUp()

    def get_huey(self):
        return SqlHuey(database='sqlite:///%s' % self.db_file, utc=False)

    def get_retention_huey(self, retention):
        # Reuse the database created by setUp; it was already unlinked there
        # and must not be removed while self.huey's connection is open.
        self.huey.storage.flush_all()
        return SqlHuey(database='sqlite:///%s' % self.db_file, utc=False,
                       result_retention=retention)

    def test_purge_only_touches_own_queue(self):
        a = self.get_retention_huey(0)
        # Queue "other" has retention disabled, so its result is never expired.
        b = SqlHuey(name='other', database='sqlite:///%s' % self.db_file,
                    utc=False)
        try:
            a.storage.put_data('ra', b'a', is_result=True)
            b.storage.put_data('rb', b'b', is_result=True)
            self.assertEqual(a.storage.purge_expired_results(), 1)
            self.assertEqual(b.storage.result_items(), {'rb': b'b'})
        finally:
            a.storage.close()
            b.storage.close()

    def test_concurrent_update_survives_stale_purge(self):
        # An update refreshes the expiration; a purge that selected the row
        # before the update must not delete the newer result.
        s = self.get_retention_huey(0.2).storage
        try:
            s.put_data('r', b'old', is_result=True)
            time.sleep(0.25)  # Now expired; a stale purge would match.
            s.put_data('r', b'new', is_result=True)  # Refresh expiration.
            self.assertEqual(s.purge_expired_results(now=time.time() + 0.01),
                             0)
            self.assertEqual(s.peek_data('r'), b'new')
        finally:
            s.close()

    def test_purge_is_resumable_and_bounded(self):
        s = self.get_retention_huey(0).storage
        try:
            for i in range(7):
                s.put_data('r%d' % i, b'v', is_result=True)
            self.assertEqual(s.purge_batch_size, 500)
            self.assertEqual(s.purge_expired_results(limit=3), 3)
            self.assertEqual(s.purge_expired_results(limit=3), 3)
            self.assertEqual(s.purge_expired_results(limit=3), 1)
            self.assertEqual(s.purge_expired_results(limit=3), 0)
        finally:
            s.close()

    def test_purge_includes_other_queues_own_results_only(self):
        # Purges are per-queue even when a limit spans multiple queues.
        a = self.get_retention_huey(0)
        b = SqlHuey(name='other', database='sqlite:///%s' % self.db_file,
                    utc=False, result_retention=3600)
        try:
            for i in range(3):
                a.storage.put_data('a%d' % i, b'v', is_result=True)
                b.storage.put_data('b%d' % i, b'v', is_result=True)
            self.assertEqual(a.storage.purge_expired_results(limit=10), 3)
            # "other" results are live and untouched.
            self.assertEqual(b.storage.result_store_size(), 3)
            self.assertEqual(a.storage.result_store_size(), 0)
        finally:
            a.storage.close()
            b.storage.close()

    def test_restart_preserves_control_and_expirations(self):
        a = self.get_retention_huey(0.2)
        a.revoke_by_id('x', revoke_until=datetime.datetime(2999, 1, 1))
        a.storage.put_data('ctl', b'keep')
        a.storage.put_data('res', b'value', is_result=True)
        a.storage.close()

        # A new process opens the same database file.
        b = SqlHuey(database='sqlite:///%s' % self.db_file, utc=False,
                    result_retention=0.2)
        try:
            self.assertEqual(b.storage.peek_data('ctl'), b'keep')
            self.assertTrue(b.is_revoked('x'))
            self.assertEqual(b.storage.peek_data('res'), b'value')
            time.sleep(0.25)
            self.assertTrue(b.storage.peek_data('res') is EmptyData)
            self.assertEqual(b.purge_expired_results(), 1)
            self.assertEqual(b.storage.peek_data('ctl'), b'keep')
        finally:
            b.storage.close()


class RetentionValidationTests(unittest.TestCase):
    def test_invalid_retention_values(self):
        for bad in ('5', -1, -0.5, True, False, float('inf'), float('nan'),
                    [], object()):
            self.assertRaises(ConfigurationError, MemoryHuey,
                              result_retention=bad)

    def test_backends_reject_retention(self):
        # File and black-hole storage cannot honor retention.
        from huey.api import BlackHoleHuey
        from huey.api import FileHuey
        from huey.storage import FileStorage

        path = os.path.join(TEST_TMP_DIR, 'reject-file')
        self.assertRaises(ConfigurationError, FileHuey, 'f',
                          path=path, result_retention=10)
        self.assertRaises(ConfigurationError, BlackHoleHuey,
                          result_retention=10)
        self.assertRaises(ConfigurationError, FileStorage, 'f', path,
                          result_retention=10)

    def test_backends_without_retention_default_is_fine(self):
        # Default (None) must not break backends that do not implement it.
        from huey.storage import FileStorage
        path = os.path.join(TEST_TMP_DIR, 'default-file')
        s = FileStorage('f', path)
        try:
            self.assertIsNone(s.result_retention)
            s.put_data('k', b'v', is_result=True)
            self.assertEqual(s.purge_expired_results(), 0)
            self.assertEqual(s.peek_data('k'), b'v')
        finally:
            s.flush_all()

    def test_purge_limit_validation(self):
        s = MemoryHuey(result_retention=10).storage
        for bad in (-1, 1.5, True, 'x'):
            self.assertRaises(ValueError, s.purge_expired_results, limit=bad)
        # Zero is an explicit no-op.
        self.assertEqual(s.purge_expired_results(limit=0), 0)


class TestConsumerPurity(BaseTestCase):
    """The scheduler performs a bounded purge on each loop."""

    class _Scheduler(object):
        def make(self, huey):
            from huey.consumer import Scheduler

            class FastScheduler(Scheduler):
                def sleep_for_interval(self, current, interval):
                    pass
            return FastScheduler(huey, threading.Event(), interval=1,
                                 periodic=False)

    def test_scheduler_purges_expired_results(self):
        huey = MemoryHuey(utc=False, result_retention=0)
        huey.storage.put_data('r', b'v', is_result=True)
        self.assertEqual(huey.result_count(), 0)

        scheduler = self._Scheduler().make(huey)
        scheduler._next_loop = time.monotonic() + 60
        scheduler.loop()
        self.assertEqual(huey.storage._results, {})

    def test_scheduler_noop_without_retention(self):
        huey = MemoryHuey(utc=False)  # Retention disabled.
        huey.storage.put_data('k', b'v', is_result=True)
        scheduler = self._Scheduler().make(huey)
        scheduler._next_loop = time.monotonic() + 60
        scheduler.loop()
        self.assertEqual(huey.storage.peek_data('k'), b'v')
