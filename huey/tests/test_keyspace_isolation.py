"""Tests for keyspace isolation between business data, task results and the
internal control-state (revocation flags, locks, rate-limit windows).

The same literal string is used deliberately as a business key, task/result
id, single-task revoke id, class-revoke suffix, lock name and rate-limit
name: under isolation each of these must resolve to an independent keyspace.
"""
import os
import sqlite3
import tempfile

from huey import MemoryHuey
from huey import SqliteHuey
from huey.api import Huey
from huey.api import Task
from huey.api import chord
from huey.api import group
from huey.constants import EmptyData
from huey.exceptions import ConfigurationError
from huey.exceptions import RateLimitExceeded
from huey.exceptions import RetryTask
from huey.exceptions import TaskLockedException
from huey.storage import BlackHoleStorage
from huey.tests.base import BaseTestCase


COLLIDING_ID = 'collide'


class KeyspaceIsolationTests(object):
    """Backend-agnostic isolation contract. Mixed in per storage backend."""
    def get_isolated_huey(self, name='iso', **kwargs):
        raise NotImplementedError

    # -----------------------------------------------------------
    # The core six-plane collision contract.
    # -----------------------------------------------------------
    def test_same_literal_is_independent_across_all_planes(self):
        h = self.get_isolated_huey()
        key = COLLIDING_ID

        h.put(key, 'business')
        h.put_result(key, 'result')
        h.revoke_by_id(key)
        lock = h.lock_task(key)
        lock.acquire()
        rl = h.rate_limit(key, 5, 60)
        rl.acquire()

        @h.task()
        def collide_task():
            return 'task'
        h.revoke_all(collide_task)

        # Every plane holds its own value for the identical literal.
        self.assertEqual(h.get(key, peek=True), 'business')
        self.assertEqual(h.result(key, preserve=True), 'result')
        self.assertTrue(h.is_revoked(Task(id=key)))
        self.assertTrue(collide_task.is_revoked())
        self.assertTrue(h.is_locked(key))
        self.assertEqual(rl.current_usage(), 1)

    def test_business_access_cannot_read_or_delete_control_state(self):
        h = self.get_isolated_huey()
        h.revoke_by_id(COLLIDING_ID)
        h.lock_task(COLLIDING_ID).acquire()

        # Nothing on the business plane for that literal.
        self.assertIsNone(h.get(COLLIDING_ID))
        self.assertIs(h.get_raw(COLLIDING_ID), EmptyData)
        self.assertFalse(h.delete(COLLIDING_ID))

        # Control state is untouched.
        self.assertTrue(h.is_revoked(Task(id=COLLIDING_ID)))
        self.assertTrue(h.is_locked(COLLIDING_ID))

    def test_result_access_cannot_read_control_state(self):
        h = self.get_isolated_huey()
        h.revoke_by_id(COLLIDING_ID)
        h.lock_task(COLLIDING_ID).acquire()
        h.rate_limit(COLLIDING_ID, 1, 60).acquire()

        self.assertIsNone(h.result(COLLIDING_ID))
        self.assertIs(h.storage.peek_data(COLLIDING_ID), EmptyData)
        self.assertTrue(h.is_revoked(Task(id=COLLIDING_ID)))
        self.assertTrue(h.is_locked(COLLIDING_ID))

    def test_flush_results_preserves_control_and_business(self):
        h = self.get_isolated_huey()
        h.put(COLLIDING_ID, 'business')
        h.put_result(COLLIDING_ID, 'result')
        h.revoke_by_id(COLLIDING_ID)
        h.lock_task(COLLIDING_ID).acquire()
        rl = h.rate_limit(COLLIDING_ID, 5, 60)
        rl.acquire()

        h.storage.flush_results()

        self.assertEqual(h.storage.result_store_size(), 0)
        self.assertIsNone(h.result(COLLIDING_ID))
        # Locks, revocations and rate-limit windows survive.
        self.assertTrue(h.is_locked(COLLIDING_ID))
        self.assertTrue(h.is_revoked(Task(id=COLLIDING_ID)))
        self.assertEqual(rl.current_usage(), 1)
        # Business data survives too.
        self.assertEqual(h.get(COLLIDING_ID, peek=True), 'business')

    def test_flush_control_preserves_results_and_business(self):
        h = self.get_isolated_huey()
        h.put(COLLIDING_ID, 'business')
        h.put_result(COLLIDING_ID, 'result')
        h.revoke_by_id(COLLIDING_ID)
        h.lock_task(COLLIDING_ID).acquire()

        h.storage.flush_control()

        self.assertFalse(h.is_revoked(Task(id=COLLIDING_ID)))
        self.assertFalse(h.is_locked(COLLIDING_ID))
        self.assertEqual(h.result(COLLIDING_ID, preserve=True), 'result')
        self.assertEqual(h.get(COLLIDING_ID, peek=True), 'business')

    def test_result_and_business_planes_are_separate(self):
        h = self.get_isolated_huey()
        h.put(COLLIDING_ID, 'business')
        h.put_result(COLLIDING_ID, 'result')

        self.assertEqual(h.get(COLLIDING_ID, peek=True), 'business')
        self.assertEqual(h.result(COLLIDING_ID, preserve=True), 'result')

        # Popping the business value leaves the result in place and vice versa.
        self.assertEqual(h.get(COLLIDING_ID), 'business')
        self.assertIsNone(h.get(COLLIDING_ID))
        self.assertEqual(h.result(COLLIDING_ID, preserve=True), 'result')
        # Deleting the result through the result-plane API leaves business
        # empty and does not touch any control state.
        self.assertTrue(h.delete_result(COLLIDING_ID))
        self.assertIsNone(h.result(COLLIDING_ID))
        self.assertIs(h.get_raw(COLLIDING_ID), EmptyData)

    def test_lock_blocks_colliding_business_write_only_on_control_plane(self):
        h = self.get_isolated_huey()
        lock = h.lock_task(COLLIDING_ID)
        self.assertTrue(lock.acquire())
        # A second acquire fails on the control plane.
        self.assertRaises(TaskLockedException, lock.acquire)
        # But business and result writes with the same key are unaffected.
        h.put(COLLIDING_ID, 'still-works')
        h.put_result(COLLIDING_ID, 'also-works')
        self.assertEqual(h.get(COLLIDING_ID, peek=True), 'still-works')
        self.assertTrue(lock.clear())
        self.assertFalse(h.is_locked(COLLIDING_ID))

    def test_rate_limit_window_is_independent(self):
        h = self.get_isolated_huey()
        rl = h.rate_limit(COLLIDING_ID, limit=2, per=60)
        rl.acquire()
        rl.acquire()
        self.assertRaises(RateLimitExceeded, rl.acquire)
        # Business and result planes with the same name are unaffected.
        h.put(COLLIDING_ID, 'biz')
        h.put_result(COLLIDING_ID, 'res')
        self.assertEqual(h.get(COLLIDING_ID, peek=True), 'biz')
        rl.reset()
        rl.acquire()  # Window reset works.

    # -----------------------------------------------------------
    # Real public workflow: chain, group/chord, Result/ResultGroup,
    # retry and revoke/restore.
    # -----------------------------------------------------------
    def test_chain_and_result_group_with_isolation(self):
        h = self.get_isolated_huey()

        @h.task()
        def add(a, b):
            return a + b

        pipe = add.s(1, 2).then(add.s(3))
        r1, r2 = h.enqueue(pipe)
        # Only the pipeline head is enqueued up-front; the next task is
        # enqueued automatically once the head finishes.
        self.assertEqual(len(h), 1)
        self.assertEqual(h.execute(h.dequeue()), 3)
        self.assertEqual(len(h), 1)
        self.assertEqual(h.execute(h.dequeue()), 6)
        self.assertEqual(r1.get(), 3)
        self.assertEqual(r2.get(), 6)

    def test_group_and_result_group(self):
        h = self.get_isolated_huey()

        @h.task()
        def dbl(n):
            return n * 2

        rg = h.enqueue(group([dbl.s(i) for i in range(4)]))
        while len(h):
            h.execute(h.dequeue())
        self.assertEqual(rg.get(), [0, 2, 4, 6])

    def test_chord_workflow(self):
        h = self.get_isolated_huey()

        @h.task()
        def prod(n):
            return n

        @h.task()
        def agg(items):
            return sum(items)

        cr = h.enqueue(chord([prod.s(i) for i in range(4)], agg.s()))
        # The four member tasks run first; the callback is only enqueued once
        # every member has reported its chord result.
        self.assertEqual(len(h), 4)
        for _ in range(4):
            h.execute(h.dequeue())
        self.assertEqual(len(h), 1)
        h.execute(h.dequeue())
        self.assertEqual(len(h), 0)
        self.assertEqual(cr.get(), 0 + 1 + 2 + 3)

    def test_retry_surfaces_and_succeeds(self):
        h = self.get_isolated_huey()
        state = {'n': 0}

        @h.task(retries=2, retry_delay=0)
        def flaky():
            state['n'] += 1
            if state['n'] < 3:
                raise RetryTask()
            return 'ok'

        result = flaky()
        task = h.dequeue()
        h.execute(task)          # First attempt: retried, re-enqueued.
        self.assertEqual(state['n'], 1)
        h.execute(h.dequeue())   # Second attempt.
        self.assertEqual(state['n'], 2)
        h.execute(h.dequeue())   # Third attempt succeeds.
        self.assertEqual(state['n'], 3)
        self.assertEqual(result.get(), 'ok')

    def test_revoke_then_restore_through_public_api(self):
        h = self.get_isolated_huey()

        @h.task()
        def work():
            return 'done'

        result = work()
        result.revoke()
        self.assertTrue(result.is_revoked())
        task = h.dequeue()
        # Revoked task is skipped, no result stored.
        self.assertIsNone(h.execute(task))
        self.assertIsNone(result.get())

        # Re-enqueue and restore: it now runs.
        result2 = work()
        result2.restore()
        self.assertFalse(result2.is_revoked())
        h.execute(h.dequeue())
        self.assertEqual(result2.get(), 'done')

    def test_revoke_once_is_consumed_but_kept_in_control_plane(self):
        h = self.get_isolated_huey()

        @h.task()
        def work():
            return 1

        result = work()
        result.revoke(revoke_once=True)
        self.assertTrue(result.is_revoked())
        # Business plane still cannot see the revoke marker.
        self.assertIsNone(h.get(result.revoke_id))

    def test_revoke_all_restore_all(self):
        h = self.get_isolated_huey()

        @h.task()
        def work():
            return 1

        work.revoke()
        self.assertTrue(work.is_revoked())
        # Class revoke key must not leak onto the business plane.
        rt_key = h._task_key(work, 'rt')
        self.assertIsNone(h.get(rt_key))
        self.assertTrue(work.restore())
        self.assertFalse(work.is_revoked())

    def test_flush_all_covers_every_keyspace(self):
        h = self.get_isolated_huey()
        h.put(COLLIDING_ID, 'business')
        h.put_result(COLLIDING_ID, 'result')
        h.revoke_by_id(COLLIDING_ID)
        h.lock_task(COLLIDING_ID).acquire()
        h.rate_limit(COLLIDING_ID, 5, 60).acquire()

        h.flush()

        self.assertIsNone(h.get(COLLIDING_ID))
        self.assertIsNone(h.result(COLLIDING_ID))
        self.assertFalse(h.is_revoked(Task(id=COLLIDING_ID)))
        self.assertFalse(h.is_locked(COLLIDING_ID))
        self.assertEqual(h.storage.result_store_size(), 0)
        self.assertEqual(h.storage.business_store_size(), 0)
        self.assertEqual(h.storage.control_store_size(), 0)


class TestMemoryKeyspaceIsolation(KeyspaceIsolationTests, BaseTestCase):
    def get_huey(self):
        # Default mode stays the plain MemoryHuey for the inherited contract.
        return MemoryHuey(utc=False)

    def get_isolated_huey(self, name='iso', **kwargs):
        return MemoryHuey(name, utc=False, keyspace_isolation=True, **kwargs)

    def test_default_mode_shares_single_store(self):
        # Backwards-compatibility guarantee: isolation off means the legacy
        # shared keyspace, where a business put and result truly collide.
        h = MemoryHuey(utc=False)
        self.assertFalse(h.storage.keyspace_isolation)
        h.put(COLLIDING_ID, 'first')
        h.put_result(COLLIDING_ID, 'second')
        self.assertEqual(h.result(COLLIDING_ID, preserve=True), 'second')


class TestSqliteKeyspaceIsolation(KeyspaceIsolationTests, BaseTestCase):
    def setUp(self):
        self._fd, self._db_path = tempfile.mkstemp(suffix='.db')
        os.close(self._fd)
        os.unlink(self._db_path)
        super(TestSqliteKeyspaceIsolation, self).setUp()

    def tearDown(self):
        super(TestSqliteKeyspaceIsolation, self).tearDown()
        for suffix in ('', '-wal', '-shm'):
            path = self._db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def get_huey(self):
        return SqliteHuey(filename=self._db_path, timeout=3, utc=False)

    def get_isolated_huey(self, name='iso', **kwargs):
        return SqliteHuey(name, filename=self._db_path, utc=False,
                          keyspace_isolation=True, **kwargs)

    def test_default_mode_shares_single_store(self):
        h = SqliteHuey(filename=self._db_path, utc=False)
        self.assertFalse(h.storage.keyspace_isolation)
        h.put(COLLIDING_ID, 'first')
        h.put_result(COLLIDING_ID, 'second')
        self.assertEqual(h.result(COLLIDING_ID, preserve=True), 'second')
        h.storage.close()

    def test_two_queues_sharing_one_file_remain_isolated(self):
        a = self.get_isolated_huey('queueA')
        b = self.get_isolated_huey('queueB')
        key = COLLIDING_ID
        for h, tag in ((a, 'A'), (b, 'B')):
            h.put(key, 'biz-%s' % tag)
            h.put_result(key, 'res-%s' % tag)
            h.revoke_by_id(key)
            h.lock_task(key).acquire()

        self.assertEqual(a.get(key, peek=True), 'biz-A')
        self.assertEqual(b.get(key, peek=True), 'biz-B')
        self.assertEqual(a.result(key, preserve=True), 'res-A')
        self.assertEqual(b.result(key, preserve=True), 'res-B')

        a.storage.flush_results()
        self.assertIsNone(a.result(key))
        self.assertEqual(b.result(key, preserve=True), 'res-B')
        # Both queues' control state survives A's result flush.
        self.assertTrue(a.is_revoked(Task(id=key)))
        self.assertTrue(b.is_revoked(Task(id=key)))
        self.assertTrue(a.is_locked(key) and b.is_locked(key))

        a.storage.flush_control()
        self.assertFalse(a.is_revoked(Task(id=key)))
        self.assertTrue(b.is_revoked(Task(id=key)))
        a.storage.close()
        b.storage.close()

    def test_persistence_across_reopen(self):
        h = self.get_isolated_huey()
        h.put(COLLIDING_ID, 'business')
        h.put_result(COLLIDING_ID, 'result')
        h.revoke_by_id(COLLIDING_ID)
        h.lock_task(COLLIDING_ID).acquire()
        h.rate_limit(COLLIDING_ID, 5, 60).acquire()
        h.storage.close()

        reopened = self.get_isolated_huey()
        self.assertEqual(reopened.get(COLLIDING_ID, peek=True), 'business')
        self.assertEqual(
            reopened.result(COLLIDING_ID, preserve=True), 'result')
        self.assertTrue(reopened.is_revoked(Task(id=COLLIDING_ID)))
        self.assertTrue(reopened.is_locked(COLLIDING_ID))
        reopened.storage.close()

    def test_legacy_database_error_policy_refuses_and_preserves(self):
        # Build an unmarked legacy database.
        old = SqliteHuey('legacy', filename=self._db_path, utc=False)
        old.put('k1', 'business')
        old.put_result('k2', 'result')
        old.revoke_by_id('k2')
        old.storage.close()

        conn = sqlite3.connect(self._db_path)
        before = conn.execute('select count(*) from kv').fetchone()[0]
        conn.close()
        self.assertEqual(before, 3)

        with self.assertRaises(ConfigurationError):
            SqliteHuey('legacy', filename=self._db_path, utc=False,
                       keyspace_isolation=True)

        # Nothing was deleted or reinterpreted.
        conn = sqlite3.connect(self._db_path)
        after = conn.execute('select count(*) from kv').fetchone()[0]
        conn.close()
        self.assertEqual(after, 3)

    def test_legacy_database_quarantine_policy(self):
        old = SqliteHuey('legacy', filename=self._db_path, utc=False)
        old.put('k1', 'business')
        old.revoke_by_id('task-1')
        old.storage.close()

        h = SqliteHuey('legacy', filename=self._db_path, utc=False,
                       keyspace_isolation=True,
                       legacy_policy='quarantine')
        self.assertEqual(h.storage.quarantine_info(), 2)
        self.assertEqual(h.storage.result_store_size(), 0)
        self.assertEqual(h.storage.business_store_size(), 0)
        self.assertEqual(h.storage.control_store_size(), 0)

        conn = sqlite3.connect(self._db_path)
        quarantined = conn.execute(
            'select key from kv_legacy_quarantine order by key').fetchall()
        self.assertEqual([row[0] for row in quarantined],
                         ['k1', 'r:task-1'])
        # The active kv table only contains marked rows (none here).
        active = conn.execute(
            "select count(*) from kv where cast(key as blob) not like "
            "char(1)||'b:%' and cast(key as blob) not like char(1)||'r:%' "
            "and cast(key as blob) not like char(1)||'c:%'").fetchone()[0]
        self.assertEqual(active, 0)
        conn.close()

        # A second isolated open is clean and retains the quarantine archive.
        h.put_result('new', 'v')
        h.storage.close()
        again = SqliteHuey('legacy', filename=self._db_path, utc=False,
                          keyspace_isolation=True)
        self.assertEqual(again.result('new', preserve=True), 'v')
        self.assertEqual(again.storage.quarantine_info(), 2)
        again.storage.close()

    def test_invalid_legacy_policy_rejected(self):
        with self.assertRaises(ConfigurationError):
            SqliteHuey('x', filename=self._db_path,
                       keyspace_isolation=True, legacy_policy='bogus')


class TestUnsupportedStorageRejected(BaseTestCase):
    def get_huey(self):
        return MemoryHuey(utc=False)

    def test_blackhole_storage_rejected_before_writes(self):
        with self.assertRaises(ConfigurationError):
            Huey('x', storage_class=BlackHoleStorage,
                 keyspace_isolation=True)

    def test_custom_storage_without_capability_rejected(self):
        class LegacyStorage(BlackHoleStorage):
            def __init__(self, name):
                super(LegacyStorage, self).__init__(name)

        with self.assertRaises(ConfigurationError):
            Huey('x', storage_class=LegacyStorage,
                 keyspace_isolation=True)

        # The same storage keeps working when isolation is not requested.
        h = Huey('x', storage_class=LegacyStorage)
        self.assertFalse(h.keyspace_isolation)
        self.assertIsInstance(h.storage, LegacyStorage)
