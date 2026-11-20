"""
Tests for tenant-fair scheduling.

The feature is disabled by default; when enabled, tenants with executable
tasks share the consumer via a persistent, storage-side round-robin:

* a tenant cannot take a second turn before every other active tenant got
  one, regardless of task volume or priority;
* within a tenant, priority ordering and same-priority FIFO are unchanged;
* the rotation state lives in the storage (database tables or a shared
  in-process lock), not in individual consumers;
* scheduled/ETA tasks only join the rotation once they become executable;
* empty tenants do not burn turns and cannot hold long-term bias.
"""
import datetime
import os
import sqlite3
import threading
from queue import Queue

from huey.api import MemoryHuey
from huey.api import SqliteHuey
from huey.api import Task
from huey.exceptions import ConfigurationError
from huey.exceptions import TaskException
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase


class TenantFairTestMixin(object):
    # Subclasses provide get_huey(tenant_fair=...).
    fair = True

    def get_fair_huey(self, **kwargs):
        raise NotImplementedError

    def setUp(self):
        super(TenantFairTestMixin, self).setUp()
        self.fair_huey = self.get_fair_huey()

        def record(state):
            def task(n):
                state.append(n)
                return n
            return task

        self.state_a = []
        self.state_b = []
        self.task_a = self.fair_huey.task(name='tf_task_a')(
            record(self.state_a))
        self.task_b = self.fair_huey.task(name='tf_task_b')(
            record(self.state_b))

    def tearDown(self):
        self.fair_huey.storage.close()
        super(TenantFairTestMixin, self).tearDown()

    def dequeue_tenants(self, n):
        tenants = []
        for _ in range(n):
            task = self.fair_huey.dequeue()
            self.assertIsNotNone(task)
            tenants.append(task.tenant)
        return tenants

    def test_disabled_by_default_is_strict_priority(self):
        huey = self.get_fair_huey(tenant_fair=False)
        try:
            @huey.task()
            def task(n):
                return n

            for i in range(3):
                task(('a', i), tenant='alpha')
            task(('b', 0), tenant='beta', priority=5)
            # Without fairness the high-priority beta task goes first and
            # the tenant argument is otherwise ignored by the storage.
            first = huey.dequeue()
            self.assertEqual(first.args, (('b', 0),))
            self.assertEqual(first.tenant, 'beta')
            for i in range(3):
                self.assertEqual(huey.dequeue().tenant, 'alpha')
            self.assertIsNone(huey.dequeue())
        finally:
            huey.storage.close()

    def test_round_robin_between_active_tenants(self):
        for i in range(3):
            self.task_a(('a', i), tenant='alpha')
            self.task_b(('b', i), tenant='beta')

        self.assertEqual(
            self.dequeue_tenants(6),
            ['alpha', 'beta', 'alpha', 'beta', 'alpha', 'beta'])

    def test_flooding_tenant_cannot_starve_others(self):
        # Alpha floods both volume and priority.
        for i in range(8):
            self.task_a(('a', i), tenant='alpha', priority=9)
        self.task_b(('b', 0), tenant='beta')
        self.task_b(('b', 1), tenant='beta')

        tenants = self.dequeue_tenants(4)
        # Alpha may open the rotation, but beta must be served within the
        # first two dequeues and again before alpha gets a third turn.
        self.assertEqual(tenants[0], 'alpha')
        self.assertEqual(tenants[1], 'beta')
        self.assertEqual(tenants[2], 'alpha')
        self.assertEqual(tenants[3], 'beta')

        # Once beta drains, alpha consumes the remainder without error.
        remainder = self.dequeue_tenants(6)
        self.assertEqual(remainder, ['alpha'] * 6)
        self.assertIsNone(self.fair_huey.dequeue())

    def test_priority_and_fifo_preserved_within_tenant(self):
        @self.fair_huey.task()
        def task(n):
            return n

        task(('a-low-1',), tenant='alpha')
        task(('a-hi',), tenant='alpha', priority=5)
        task(('a-low-2',), tenant='alpha')
        task(('b-1',), tenant='beta')

        first = self.fair_huey.dequeue()
        self.assertEqual(first.tenant, 'alpha')
        self.assertEqual(first.args, (('a-hi',),))  # Priority wins in tenant.
        second = self.fair_huey.dequeue()
        self.assertEqual(second.tenant, 'beta')
        third = self.fair_huey.dequeue()
        self.assertEqual(third.args, (('a-low-1',),))  # FIFO same priority.
        fourth = self.fair_huey.dequeue()
        self.assertEqual(fourth.args, (('a-low-2',),))

    def test_interleaved_enqueue_keeps_within_tenant_ordering(self):
        # The in-memory heap is only ordered by tenant; interleaving tenants
        # and priorities must not let a later low-priority task shadow an
        # earlier high-priority task within the same tenant.
        @self.fair_huey.task()
        def task(n):
            return n

        task(('a1',), tenant='alpha')
        task(('b1',), tenant='beta')
        task(('a-hi',), tenant='alpha', priority=8)
        task(('b2',), tenant='beta')
        task(('a2',), tenant='alpha')

        first = self.fair_huey.dequeue()
        self.assertEqual(first.args, (('a-hi',),))
        second = self.fair_huey.dequeue()
        self.assertEqual(second.args, (('b1',),))
        third = self.fair_huey.dequeue()
        self.assertEqual(third.args, (('a1',),))
        fourth = self.fair_huey.dequeue()
        self.assertEqual(fourth.args, (('b2',),))
        fifth = self.fair_huey.dequeue()
        self.assertEqual(fifth.args, (('a2',),))

    def test_unassigned_tasks_share_default_bucket(self):
        @self.fair_huey.task()
        def task(n):
            return n

        task(('d1',))
        task(('d2',))
        task(('a1',), tenant='alpha')

        # Default-bucket tasks behave as one tenant inside the rotation.
        first = self.fair_huey.dequeue()
        self.assertIsNone(first.tenant)
        second = self.fair_huey.dequeue()
        self.assertEqual(second.tenant, 'alpha')
        third = self.fair_huey.dequeue()
        self.assertIsNone(third.tenant)
        # The two default tasks stayed FIFO relative to each other.
        self.assertEqual([t.args[0] for t in (first, third)],
                         [('d1',), ('d2',)])

    def test_empty_tenant_does_not_burn_turns(self):
        self.task_a(('a1',), tenant='alpha')
        self.task_b(('b1',), tenant='beta')
        self.task_b(('b2',), tenant='beta')

        self.assertEqual(self.dequeue_tenants(3),
                         ['alpha', 'beta', 'beta'])

        # Alpha was removed from the rotation when it drained. New work for
        # alpha re-registers it at the back rather than resurrecting stale
        # state; the freshly enqueued beta task is then served in its turn.
        self.task_a(('a2',), tenant='alpha')
        self.task_b(('b3',), tenant='beta')
        self.assertEqual(self.dequeue_tenants(2), ['alpha', 'beta'])

    def test_scheduled_tasks_join_rotation_only_when_due(self):
        # Use delay= so the ETA is built on the huey's own time basis
        # (SqliteHuey defaults to UTC and treats naive eta= values as local).
        now = self.fair_huey._get_timestamp()
        self.task_a.schedule(('a-late',), delay=60, tenant='alpha')
        self.task_b(('b-now',), tenant='beta')
        self.assertEqual(self.fair_huey.pending_count(), 2)

        # A worker claims the not-yet-due task first (both are executable
        # rows); Huey's execute() then parks it in the schedule table. Once
        # parked it must not participate in the queue rotation.
        early = self.fair_huey.dequeue()
        self.assertEqual(early.tenant, 'alpha')
        self.fair_huey.execute(early, timestamp=now)
        self.assertEqual(self.fair_huey.scheduled_count(), 1)
        self.assertEqual(self.fair_huey.pending_count(), 1)

        ready = self.fair_huey.dequeue()
        self.assertEqual(ready.tenant, 'beta')
        self.assertIsNone(self.fair_huey.dequeue())

        # The scheduler promotes the due task; it then participates normally.
        due = now + datetime.timedelta(seconds=61)
        promoted = self.fair_huey.read_schedule(timestamp=due)
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0].tenant, 'alpha')
        for t in promoted:
            self.fair_huey.enqueue(t)
        task = self.fair_huey.dequeue()
        self.assertEqual(task.tenant, 'alpha')
        self.assertEqual(task.args, ('a-late',))

    def test_retry_keeps_tenant(self):
        @self.fair_huey.task(retries=1)
        def flaky(n):
            raise ValueError('boom')

        @self.fair_huey.task()
        def ok(n):
            return n

        r = flaky(('a',), tenant='alpha')
        ok(('b',), tenant='beta')

        task = self.fair_huey.dequeue()
        self.assertEqual(task.tenant, 'alpha')
        self.fair_huey.execute(task)

        # The retry is re-enqueued carrying the same tenant and goes to the
        # tail of the rotation, so beta (still waiting) is served first.
        other = self.fair_huey.dequeue()
        self.assertEqual(other.tenant, 'beta')
        retried = self.fair_huey.dequeue()
        self.assertEqual(retried.id, r.id)
        self.assertEqual(retried.tenant, 'alpha')
        try:
            self.fair_huey.execute(retried)
        except TaskException:
            pass

    def test_pipeline_and_chord_inherit_origin_tenant(self):
        @self.fair_huey.task()
        def add(n):
            return n

        @self.fair_huey.task()
        def cb(results):
            return results

        # Pipeline created in one request: both hops carry the origin tenant.
        r = add.s(('a',), tenant='alpha').then(add, ('child',))
        self.fair_huey.enqueue(r)
        first = self.fair_huey.dequeue()
        self.assertEqual(first.tenant, 'alpha')
        self.fair_huey.execute(first)
        second = self.fair_huey.dequeue()
        self.assertEqual(second.tenant, 'alpha')
        self.assertEqual(second.args, (('child',), 'a'))

        # A task published afterwards without a tenant must not inherit the
        # previous request's tenant through any shared state.
        add(('plain',))
        plain = self.fair_huey.dequeue()
        self.assertIsNone(plain.tenant)

        # Chord members keep their own tenants; the callback is an independent
        # task whose tenant is whatever it was published with.
        from huey.api import chord
        c = chord([add.s(('m1',), tenant='alpha'),
                   add.s(('m2',), tenant='beta')],
                  cb.s(('callback',)))
        self.fair_huey.enqueue(c)
        members = sorted(
            (self.fair_huey.dequeue().tenant,
             self.fair_huey.dequeue().tenant))
        self.assertEqual(members, ['alpha', 'beta'])

    def test_invalid_tenant_rejected(self):
        @self.fair_huey.task()
        def task(n):
            return n

        for bad in ('', 123, b'bytes-tenant'):
            self.assertRaises(ValueError, task, ('x',), tenant=bad)

    def test_decorator_default_tenant_and_override(self):
        @self.fair_huey.task(tenant='acme', name='tf_acme_task')
        def acme(n):
            return n

        @self.fair_huey.task(name='tf_plain_task')
        def plain(n):
            return n

        self.assertEqual(acme(1).task.tenant, 'acme')
        self.assertEqual(acme(2, tenant='globex').task.tenant, 'globex')
        self.assertIsNone(plain(3).task.tenant)
        self.assertEqual(plain(4, tenant='inline').task.tenant, 'inline')
        self.assertEqual(acme.task_class.default_tenant, 'acme')

        # Invalid keys are rejected when the decorator is applied.
        def build_bad_task():
            @self.fair_huey.task(tenant='', name='tf_bad_task')
            def bad(n):
                return n
        self.assertRaises(ValueError, build_bad_task)

    def test_tenant_round_trips_through_serialization(self):
        @self.fair_huey.task()
        def task(n):
            return n

        task(('a',), tenant='alpha')
        raw = self.fair_huey.storage.dequeue()
        restored = self.fair_huey.deserialize_task(raw)
        self.assertIsInstance(restored, Task)
        self.assertEqual(restored.tenant, 'alpha')
        # Put it back so tearDown flush does not need special handling.
        self.fair_huey.storage.enqueue(raw, restored.priority,
                                       restored.tenant)

    def test_enqueued_items_matches_dequeue_order(self):
        @self.fair_huey.task()
        def task(n):
            return n

        task(('a1',), tenant='alpha', priority=3)
        task(('b1',), tenant='beta')
        task(('a2',), tenant='alpha')
        task(('b2',), tenant='beta')

        preview = [self.fair_huey.deserialize_task(raw).args
                   for raw in self.fair_huey.storage.enqueued_items()]
        actual = []
        while True:
            queued = self.fair_huey.dequeue()
            if queued is None:
                break
            actual.append(queued.args)
        self.assertEqual(preview, actual)
        # Limit is honored on the rotated view.
        task(('c1',), tenant='gamma')
        task(('a3',), tenant='alpha')
        limited = self.fair_huey.storage.enqueued_items(1)
        self.assertEqual(len(limited), 1)


class TestMemoryTenantFair(TenantFairTestMixin, BaseTestCase):
    def get_fair_huey(self, tenant_fair=True):
        return MemoryHuey(utc=False, tenant_fair=tenant_fair)

    def test_unsupported_backend_rejects_configuration(self):
        from huey.storage import FileStorage
        self.assertRaises(ConfigurationError, FileStorage,
                          name='huey', path=TEST_TMP_DIR, tenant_fair=True)


class TestSqliteTenantFair(TenantFairTestMixin, BaseTestCase):
    db_file = os.path.join(TEST_TMP_DIR, 'huey-tenant-fair.db')

    def get_fair_huey(self, tenant_fair=True):
        if os.path.exists(self.db_file):
            os.unlink(self.db_file)
        return SqliteHuey(filename=self.db_file, timeout=3,
                          tenant_fair=tenant_fair)

    def tearDown(self):
        super(TestSqliteTenantFair, self).tearDown()
        for suffix in ('', '-wal', '-shm'):
            path = self.db_file + suffix
            if os.path.exists(path):
                os.unlink(path)

    def test_rotation_state_persists_across_restarts(self):
        self.task_a(('a1',), tenant='alpha')
        self.task_b(('b1',), tenant='beta')
        self.assertEqual(self.dequeue_tenants(2), ['alpha', 'beta'])

        # Both tenants still have backlogged work; a brand-new consumer
        # process (new storage connection) must observe the same rotation.
        self.task_a(('a2',), tenant='alpha')
        self.task_b(('b2',), tenant='beta')
        reborn = SqliteHuey(filename=self.db_file, timeout=3,
                            tenant_fair=True)
        # Simulate the consumer re-importing the task modules on startup.
        try:
            @reborn.task(name='tf_task_a')
            def task_a(n):
                return n

            @reborn.task(name='tf_task_b')
            def task_b(n):
                return n

            seq = []
            for _ in range(2):
                task = reborn.dequeue()
                seq.append(task.tenant)
            self.assertEqual(seq, ['alpha', 'beta'])
        finally:
            reborn.storage.close()

    def test_shared_rotation_under_concurrent_consumers(self):
        nthreads, per_tenant = 8, 40
        for i in range(per_tenant):
            self.task_a(('a', i), tenant='alpha', priority=7)
            self.task_b(('b', i), tenant='beta')

        out_q = Queue()

        def consume():
            while True:
                try:
                    task = self.fair_huey.dequeue()
                except sqlite3.Error:
                    return
                if task is None:
                    return
                out_q.put(task.tenant)

        threads = [threading.Thread(target=consume)
                   for _ in range(nthreads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        tenants = [out_q.get() for _ in range(out_q.qsize())]
        self.assertEqual(len(tenants), per_tenant * 2)
        # No tenant ever takes two turns in a row while both are active.
        for prev, nxt in zip(tenants, tenants[1:]):
            self.assertNotEqual(prev, nxt)
        self.assertEqual(sorted(tenants).count('alpha'), per_tenant)
        self.assertEqual(sorted(tenants).count('beta'), per_tenant)
        self.assertEqual(self.fair_huey.pending_count(), 0)

        # The rotation table must be empty once every tenant drained.
        rows = self.fair_huey.storage.sql(
            'select count(*) from tenant_state', results=True)
        self.assertEqual(rows[0][0], 0)

    def test_failed_claim_leaves_state_intact(self):
        self.task_a(('a1',), tenant='alpha')
        self.task_b(('b1',), tenant='beta')

        storage = self.fair_huey.storage
        # Hold an external write lock; the fair dequeue retries then fails
        # without deleting the task or mutating rotation state.
        conn = sqlite3.connect(self.db_file, isolation_level=None,
                               check_same_thread=False)
        conn.execute('begin immediate')
        timer = threading.Timer(0.3, conn.execute, ('commit',))
        timer.start()
        try:
            task = self.fair_huey.dequeue()
        finally:
            timer.join()
            conn.close()
        self.assertEqual(task.tenant, 'alpha')
        self.assertEqual(self.fair_huey.pending_count(), 1)

        # After the interrupted attempt the next claim is beta, proving the
        # first (committed) rotation advanced exactly once and nothing was
        # double-counted.
        follow_up = self.fair_huey.dequeue()
        self.assertEqual(follow_up.tenant, 'beta')

    def test_legacy_database_migrates_and_defaults_to_bucket(self):
        legacy = os.path.join(TEST_TMP_DIR, 'huey-legacy.db')
        if os.path.exists(legacy):
            os.unlink(legacy)
        # Create a database with the pre-feature schema.
        conn = sqlite3.connect(legacy)
        conn.execute('create table task (id integer not null primary key, '
                     "queue text not null, data blob not null, "
                     'priority real not null default 0.0)')
        conn.execute("insert into task (queue, data, priority) values "
                     "('huey', x'00', 0)")
        conn.commit()
        conn.close()

        migrated = SqliteHuey(filename=legacy, timeout=3, tenant_fair=True)
        try:
            cols = [r[1] for r in migrated.storage.sql(
                'pragma table_info(task)', results=True)]
            self.assertIn('tenant', cols)
            # Legacy rows are readable at the storage layer and treated as
            # the explicit default bucket (raw bytes bypass deserialization).
            data = migrated.storage.dequeue()
            self.assertEqual(bytes(data), b'\x00')
            self.assertIsNone(migrated.storage.dequeue())
            # No stale rotation state survives.
            rows = migrated.storage.sql(
                'select count(*) from tenant_state', results=True)
            self.assertEqual(rows[0][0], 0)
        finally:
            migrated.storage.close()
            for suffix in ('', '-wal', '-shm'):
                path = legacy + suffix
                if os.path.exists(path):
                    os.unlink(path)

    def test_real_multi_worker_consumer_respects_fairness(self):
        # Drive an actual Consumer with several worker threads so the claim
        # path is exercised concurrently against the shared database.
        import time
        from huey.consumer import Consumer

        consumed = []

        @self.fair_huey.task(name='tf_consumer_task')
        def consumer_task(n):
            consumed.append(n)

        for i in range(20):
            consumer_task(('alpha', i), tenant='alpha', priority=5)
        for i in range(5):
            consumer_task(('beta', i), tenant='beta')

        consumer = Consumer(
            self.fair_huey, workers=4, initial_delay=0.001,
            max_delay=0.005, check_worker_health=False, periodic=False)
        consumer.start()
        try:
            deadline = time.time() + 15
            while time.time() < deadline and len(consumed) < 25:
                time.sleep(0.05)
        finally:
            consumer.stop(graceful=True)

        self.assertEqual(len(consumed), 25)
        tenants = [n[0] for n in consumed]
        # Despite alpha's high-priority flood, beta is reached within the
        # first couple of claims.
        self.assertLessEqual(tenants.index('beta'), 2)
        self.assertEqual(sorted(tenants).count('beta'), 5)
