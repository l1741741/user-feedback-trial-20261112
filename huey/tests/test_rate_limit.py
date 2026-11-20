"""
Tests for the persistent fixed-window rate limiter.

The storage-level semantics are run against both MemoryStorage (deterministic,
in-process) and SqliteStorage (durable). Cross-process contention on SQLite is
verified separately with real subprocesses in test_rate_limit_multiprocess.py,
because in-process threads cannot prove the database-level guarantee.
"""
from huey.tests import TEST_TMP_DIR
from huey.exceptions import ConfigurationError
from huey.exceptions import RateLimitExceeded
from huey.storage import RateLimitResult
from huey.tests.base import BaseTestCase

import os
import sqlite3
import unittest


class _StorageContract(object):
    """Shared assertions for every backend implementing rate_limit_add()."""
    key = 'third-party-api'

    def add(self, key, window, limit, per, now=None):
        return self.s.rate_limit_add(key, window, limit, per, now)

    def test_returns_result_object(self):
        result = self.add(self.key, 1000.0, 2, 60, now=1000.0)
        self.assertIsInstance(result, RateLimitResult)
        self.assertEqual(result, (True, 1, 0.0))
        self.assertTrue(result.allowed)
        self.assertEqual(result.count, 1)
        self.assertEqual(result.retry_after, 0.0)

    def test_allows_exactly_limit_then_denies(self):
        for i in range(1, 4):
            result = self.add(self.key, 1000.0, 3, 60, now=1000.0 + i)
            self.assertTrue(result.allowed)
            self.assertEqual(result.count, i)

        # Rejected requests report the time until the window opens and do not
        # change the stored count.
        denied = self.add(self.key, 1000.0, 3, 60, now=1020.0)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.count, 3)
        self.assertAlmostEqual(denied.retry_after, 40.0)
        denied_again = self.add(self.key, 1000.0, 3, 60, now=1059.9)
        self.assertFalse(denied_again.allowed)
        self.assertEqual(denied_again.count, 3)
        self.assertAlmostEqual(denied_again.retry_after, 0.1)

        # The row must not grow without bound from rejected requests.
        row = self.s.rate_limit_peek(self.key)
        self.assertEqual(row['count'], 3)

    def test_denial_with_limit_zero_creates_no_state(self):
        result = self.add(self.key, 1000.0, 0, 60, now=1000.0)
        self.assertFalse(result.allowed)
        self.assertEqual(result.count, 0)
        self.assertEqual(result.retry_after, 60.0)
        self.assertIsNone(self.s.rate_limit_peek(self.key))

    def test_window_rollover_resets_count_once(self):
        for i in range(3):
            self.assertTrue(
                self.add(self.key, 1000.0, 3, 60, now=1000.0 + i).allowed)
        self.assertFalse(
            self.add(self.key, 1000.0, 3, 60, now=1059.0).allowed)

        # First request in the new window opens it with count 1.
        first = self.add(self.key, 1060.0, 3, 60, now=1060.0)
        self.assertEqual(first, (True, 1, 0.0))
        self.assertEqual(self.s.rate_limit_peek(self.key)['window'], 1060.0)

        # A late request carrying the *old* window must not reset the new
        # window's count.
        stale = self.add(self.key, 1000.0, 3, 60, now=1060.5)
        self.assertFalse(stale.allowed)
        row = self.s.rate_limit_peek(self.key)
        self.assertEqual(row['window'], 1060.0)
        self.assertEqual(row['count'], 1)

    def test_stale_window_after_rollover_never_decrements(self):
        self.assertTrue(self.add(self.key, 1060.0, 2, 60, now=1060.0).allowed)
        self.assertTrue(self.add(self.key, 1060.0, 2, 60, now=1061.0).allowed)
        # Many late, old-window requests all get rejected and change nothing.
        for ts in (1059.0, 1000.0, 1059.9):
            result = self.add(self.key, 1000.0, 2, 60, now=ts)
            self.assertFalse(result.allowed)
        self.assertEqual(self.s.rate_limit_peek(self.key)['count'], 2)

    def test_clock_rollback_is_rejected_until_window_end(self):
        self.assertTrue(self.add(self.key, 1000.0, 2, 60, now=1000.0).allowed)
        self.assertTrue(self.add(self.key, 1000.0, 2, 60, now=1001.0).allowed)

        # Clock jumps back 30 seconds: the already-observed window is kept.
        result = self.add(self.key, 960.0, 2, 60, now=970.0)
        self.assertFalse(result.allowed)
        # Retry-after is computed against the stored window's end (1060).
        self.assertEqual(result.retry_after, 90.0)
        self.assertEqual(self.s.rate_limit_peek(self.key)['window'], 1000.0)

        # Even an apparently free old window cannot overwrite the new one.
        result = self.add(self.key, 960.0, 2, 60, now=971.0)
        self.assertFalse(result.allowed)
        self.assertEqual(self.s.rate_limit_peek(self.key)['count'], 2)

    def test_clock_rollback_recovers_at_real_boundary(self):
        self.assertTrue(self.add(self.key, 1000.0, 1, 60, now=1000.0).allowed)
        self.assertFalse(
            self.add(self.key, 960.0, 1, 60, now=970.0).allowed)
        # Once real time reaches the stored window's end, the next window is
        # accepted normally.
        self.assertTrue(
            self.add(self.key, 1060.0, 1, 60, now=1060.0).allowed)

    def test_changed_per_starts_fresh_window(self):
        for _ in range(2):
            self.assertTrue(
                self.add(self.key, 1000.0, 2, 60, now=1000.0).allowed)
        self.assertFalse(
            self.add(self.key, 1000.0, 2, 60, now=1010.0).allowed)

        # Same wall-clock moment, but the window length changed: fresh window.
        result = self.add(self.key, 1000.0, 2, 30, now=1010.0)
        self.assertTrue(result.allowed)
        self.assertEqual(result.count, 1)
        row = self.s.rate_limit_peek(self.key)
        self.assertEqual(row['per'], 30)
        self.assertEqual(row['window'], 1000.0)

    def test_changed_limit_takes_effect_in_current_window(self):
        for _ in range(2):
            self.assertTrue(
                self.add(self.key, 1000.0, 2, 60, now=1000.0).allowed)
        self.assertFalse(
            self.add(self.key, 1000.0, 2, 60, now=1001.0).allowed)
        # Raising the limit admits further requests in the same window.
        self.assertTrue(
            self.add(self.key, 1000.0, 4, 60, now=1002.0).allowed)
        self.assertTrue(
            self.add(self.key, 1000.0, 4, 60, now=1003.0).allowed)
        self.assertFalse(
            self.add(self.key, 1000.0, 4, 60, now=1004.0).allowed)
        self.assertEqual(self.s.rate_limit_peek(self.key)['count'], 4)

    def test_distinct_keys_are_independent(self):
        self.assertTrue(self.add('a', 1000.0, 1, 60, now=1000.0).allowed)
        self.assertFalse(self.add('a', 1000.0, 1, 60, now=1001.0).allowed)
        self.assertTrue(self.add('b', 1000.0, 1, 60, now=1001.0).allowed)

    def test_reset_and_peek(self):
        self.assertIsNone(self.s.rate_limit_peek(self.key))
        self.add(self.key, 1000.0, 2, 60, now=1000.0)
        self.assertEqual(self.s.rate_limit_peek(self.key),
                         {'window': 1000.0, 'per': 60.0, 'count': 1})
        self.s.rate_limit_reset(self.key)
        self.assertIsNone(self.s.rate_limit_peek(self.key))
        # Reset is also the documented escape hatch for a stuck clock.
        self.assertTrue(
            self.add(self.key, 960.0, 2, 60, now=970.0).allowed)

    def test_flush_rate_limits(self):
        self.add(self.key, 1000.0, 2, 60, now=1000.0)
        self.s.flush_rate_limits()
        self.assertIsNone(self.s.rate_limit_peek(self.key))


class TestMemoryRateLimitStorage(_StorageContract, BaseTestCase):
    def setUp(self):
        super(TestMemoryRateLimitStorage, self).setUp()
        self.s = self.huey.storage

    def test_flush_all_clears_rate_limits(self):
        self.add(self.key, 1000.0, 2, 60, now=1000.0)
        self.s.flush_all()
        self.assertIsNone(self.s.rate_limit_peek(self.key))


@unittest.skipIf(sqlite3 is None, 'requires sqlite3')
class TestSqliteRateLimitStorage(_StorageContract, BaseTestCase):
    db_file = os.path.join(TEST_TMP_DIR, 'huey-rate-limit.db')

    def get_huey(self):
        from huey import SqliteHuey
        return SqliteHuey('rl-test', filename=self.db_file)

    def setUp(self):
        if os.path.exists(self.db_file):
            os.unlink(self.db_file)
        super(TestSqliteRateLimitStorage, self).setUp()
        self.s = self.huey.storage

    def tearDown(self):
        super(TestSqliteRateLimitStorage, self).tearDown()
        self.s.close()
        if os.path.exists(self.db_file):
            os.unlink(self.db_file)

    def test_flush_all_clears_rate_limits(self):
        self.add(self.key, 1000.0, 2, 60, now=1000.0)
        self.s.flush_all()
        self.assertIsNone(self.s.rate_limit_peek(self.key))

    def test_state_survives_storage_reopen(self):
        self.add(self.key, 1000.0, 2, 60, now=1000.0)
        self.s.close()
        # A second "consumer process" opening the same database sees the row.
        huey2 = self.get_huey()
        try:
            self.assertEqual(huey2.storage.rate_limit_peek(self.key),
                             {'window': 1000.0, 'per': 60.0, 'count': 1})
            denied = huey2.storage.rate_limit_add(
                self.key, 1000.0, 1, 60, now=1010.0)
            self.assertFalse(denied.allowed)
        finally:
            huey2.storage.close()

    def test_queues_sharing_database_are_isolated(self):
        from huey import SqliteHuey
        other = SqliteHuey('rl-other-queue', filename=self.db_file)
        try:
            self.assertTrue(
                self.add(self.key, 1000.0, 1, 60, now=1000.0).allowed)
            self.assertFalse(
                self.add(self.key, 1000.0, 1, 60, now=1001.0).allowed)
            # Same limiter name, different queue: independent quota.
            self.assertTrue(other.storage.rate_limit_add(
                self.key, 1000.0, 1, 60, now=1001.0).allowed)
        finally:
            other.storage.close()


class TestPersistentRateLimitApi(BaseTestCase):
    def test_factory_returns_persistent_limiter(self):
        from huey.rate_limit import PersistentRateLimit
        rl = self.huey.rate_limit('x', 1, 60, persistent=True)
        self.assertIsInstance(rl, PersistentRateLimit)

    def test_default_factory_keeps_legacy_class(self):
        from huey.api import RateLimit
        rl = self.huey.rate_limit('x', 1, 60)
        self.assertIsInstance(rl, RateLimit)

    def test_unsupported_backend_raises_configuration_error(self):
        from huey import BlackHoleHuey
        # BlackHole allows everything by default, so it does implement the
        # API; use a bare BaseStorage subclass that leaves it unimplemented.
        from huey.storage import BaseStorage

        class DummyStorage(BaseStorage):
            pass

        from huey import Huey
        huey = Huey(utc=False, storage_class=DummyStorage)
        with self.assertRaises(ConfigurationError):
            huey.rate_limit('x', 1, 60, persistent=True)

    def test_acquire_raises_with_window_boundary_flag(self):
        rl = self.huey.rate_limit('x', limit=1, per=60, persistent=True)
        rl.acquire(now=960.0)
        with self.assertRaises(RateLimitExceeded) as ctx:
            rl.acquire(now=980.0)
        exc = ctx.exception
        self.assertTrue(exc.window_boundary)
        self.assertEqual(exc.delay, 40.0)
        self.assertTrue(exc.retry)

    def test_decorator_and_context_manager(self):
        rl = self.huey.rate_limit('x', limit=1, per=60, persistent=True)

        @rl
        def fn():
            return 'ok'

        self.assertEqual(fn(), 'ok')
        with rl.__class__(self.huey, 'y', 1, 60):
            pass
        with self.assertRaises(RateLimitExceeded):
            fn()

    def test_current_usage_respects_window_end(self):
        rl = self.huey.rate_limit('x', limit=3, per=60, persistent=True)
        self.assertEqual(rl.current_usage(now=960.0), 0)
        rl.acquire(now=960.0)
        rl.acquire(now=970.0)
        self.assertEqual(rl.current_usage(now=980.0), 2)
        # After the window closes the stale row reads as zero.
        self.assertEqual(rl.current_usage(now=1020.0), 0)

    def test_reset_clears_state(self):
        rl = self.huey.rate_limit('x', limit=1, per=60, persistent=True)
        rl.acquire(now=960.0)
        with self.assertRaises(RateLimitExceeded):
            rl.acquire(now=970.0)
        rl.reset()
        rl.acquire(now=970.0)  # Fresh window available again.
