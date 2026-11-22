import datetime
import os
import unittest

try:
    import peewee
except ImportError:
    peewee = None

from huey.constants import EmptyData
from huey.contrib.migrate import (ALL_KINDS, KIND_COUNTER, KIND_KV,
                                  KIND_QUEUE, MigrationError, ShardMigrator,
                                  SUPPORTED_PRODUCER_SPEC,
                                  message_group_selector)
from huey.contrib.sql_huey import SqlHuey
from huey.serializer import SignedSerializer
from huey.tests import TEST_TMP_DIR


@unittest.skipIf(peewee is None, 'requires peewee')
class TestShardMigrator(unittest.TestCase):
    def setUp(self):
        self.src_db = os.path.join(TEST_TMP_DIR, 'migrate-src.db')
        self.dst_db = os.path.join(TEST_TMP_DIR, 'migrate-dst.db')
        for path in (self.src_db, self.dst_db):
            if os.path.exists(path):
                os.unlink(path)
        self.src = SqlHuey('orders', database='sqlite:///%s' % self.src_db,
                           utc=False, results=True)
        self.dst = SqlHuey('orders', database='sqlite:///%s' % self.dst_db,
                           utc=False, results=True)

        @self.src.task(name='process_order')
        def process_order(order_id, group, note=''):
            return (order_id, group)

        @self.dst.task(name='process_order')
        def process_order_dst(order_id, group, note=''):
            return (order_id, group)

        self.process_order = process_order
        self.selector = message_group_selector('group', 'A')

    def tearDown(self):
        self.src.storage.close()
        self.dst.storage.close()
        for path in (self.src_db, self.dst_db):
            for suffix in ('', '-wal', '-shm'):
                if os.path.exists(path + suffix):
                    os.unlink(path + suffix)

    def migrator(self, **kwargs):
        kwargs.setdefault('migration_id', 'test-migration')
        kwargs.setdefault('serializer', self.src.serializer)
        kwargs.setdefault('batch_size', 2)
        return ShardMigrator(self.src.storage, self.dst.storage,
                             self.selector, **kwargs)

    def ids_in_queue(self, huey):
        return [t.id for t in huey.pending()]

    def groups_in_queue(self, huey):
        return [t.kwargs['group'] for t in huey.pending()]

    def test_supported_spec_document(self):
        self.assertTrue(SUPPORTED_PRODUCER_SPEC.startswith('>=2.0.0,<4.0.0'))

    def test_incompatible_serializer_is_rejected(self):
        signed_src = SqlHuey(
            'orders',
            database='sqlite:///%s/signed-src.db' % TEST_TMP_DIR, utc=False,
            serializer=SignedSerializer(secret='good', salt='huey'))
        signed_dst = SqlHuey(
            'orders',
            database='sqlite:///%s/signed-dst.db' % TEST_TMP_DIR, utc=False,
            serializer=SignedSerializer(secret='good', salt='huey'))
        wrong_dst = SqlHuey(
            'orders',
            database='sqlite:///%s/signed-wrong.db' % TEST_TMP_DIR,
            utc=False,
            serializer=SignedSerializer(secret='bad', salt='huey'))
        try:
            # Matching settings pass the cross-shard self-check.
            ok = ShardMigrator(
                signed_src.storage, signed_dst.storage, self.selector,
                migration_id='signed-ok',
                serializer=signed_src.serializer,
                destination_serializer=signed_dst.serializer)
            ok.migrate()
            # Mismatched secret fails loudly before any row is moved.
            with self.assertRaises(MigrationError):
                ShardMigrator(
                    signed_src.storage, wrong_dst.storage, self.selector,
                    migration_id='signed-bad',
                    serializer=signed_src.serializer,
                    destination_serializer=wrong_dst.serializer).migrate()
        finally:
            for huey in (signed_src, signed_dst, wrong_dst):
                huey.storage.close()

    def test_requires_two_distinct_sqlite_databases(self):
        with self.assertRaises(MigrationError):
            ShardMigrator(self.src.storage, self.src.storage,
                          self.selector, serializer=self.src.serializer)

    def test_rejects_different_queue_names(self):
        other = SqlHuey('different-name',
                        database='sqlite:///%s/other.db' % TEST_TMP_DIR,
                        utc=False)
        try:
            with self.assertRaises(MigrationError):
                ShardMigrator(self.src.storage, other.storage,
                              self.selector, serializer=self.src.serializer)
        finally:
            other.storage.close()

    def test_partial_then_resume_preserves_fifo_and_eta(self):
        # Interleave two groups; record enqueue order of group A.
        expected_a_order = []
        eta = (datetime.datetime.now() +
               datetime.timedelta(hours=1)).replace(microsecond=0)
        for order_id, group in [(1, 'A'), (2, 'B'), (3, 'A'), (4, 'A'),
                                (5, 'B'), (6, 'A')]:
            result = self.process_order(order_id=order_id, group=group)
            if group == 'A':
                expected_a_order.append(result.id)

        scheduled = self.process_order.task_class(
            kwargs=dict(order_id=99, group='A'), eta=eta)
        self.src.add_schedule(scheduled)
        scheduled_b = self.process_order.task_class(
            kwargs=dict(order_id=98, group='B'), eta=eta)
        self.src.add_schedule(scheduled_b)

        migrator = self.migrator()
        first = migrator.migrate(max_batches=1)
        self.assertGreater(first.scanned, 0)
        self.assertLess(self.src.storage.queue_size() +
                        self.dst.storage.queue_size(), 100)  # sanity
        partial_dst = self.ids_in_queue(self.dst)
        self.assertTrue(partial_dst)  # a partial move really happened

        # Throw the migrator away and resume from persisted progress only.
        del migrator
        migrator = self.migrator()
        progress = {p.kind: p for p in migrator.progress()}
        self.assertIn(KIND_QUEUE, progress)
        second = migrator.migrate()

        # All group-A queue rows ended up in the destination, in the exact
        # relative enqueue (FIFO) order; no group-B row was copied.
        self.assertEqual(self.ids_in_queue(self.dst), expected_a_order)
        self.assertEqual(set(self.groups_in_queue(self.dst)), {'A'})
        self.assertEqual(set(self.groups_in_queue(self.src)), {'B'})
        self.assertEqual(second.conflicts, 0)

        # Schedule: A moved with its exact ETA, B stayed behind.
        dst_sched = self.dst.scheduled()
        src_sched = self.src.scheduled()
        self.assertEqual([t.id for t in dst_sched], [scheduled.id])
        self.assertEqual(dst_sched[0].eta, eta)
        self.assertEqual([t.id for t in src_sched], [scheduled_b.id])

        # Re-running is a no-op even though the source queue is now drained
        # of group A: nothing is re-created.
        before = self.dst.storage.queue_size()
        third = self.migrator().migrate()
        self.assertEqual(third.migrated, 0)
        self.assertEqual(third.selected, 0)
        self.assertEqual(self.dst.storage.queue_size(), before)

    def test_conflict_by_business_identity_is_idempotent(self):
        result = self.process_order(order_id=7, group='A')
        row = (self.src.storage.Task.select()
               .order_by(self.src.storage.Task.id.desc()).get())
        # Simulate a crashed earlier run: destination already has the message.
        self.dst.storage.Task.create(queue=row.queue, data=row.data,
                                     priority=row.priority)
        stats = self.migrator(migration_id='replay').migrate()
        self.assertEqual(stats.conflicts, 1)
        self.assertEqual(stats.migrated, 0)
        # Exactly one copy remains, keyed by the task UUID.
        uuids = [self.dst.serializer.deserialize(data).id
                 for (data,) in self.dst.storage.Task
                 .select(self.dst.storage.Task.data).tuples()]
        self.assertEqual(uuids, [result.id])
        # The source row was removed after the identical copy was confirmed.
        self.assertFalse(self.src.storage.Task.select().where(
            self.src.storage.Task.id == row.id).exists())

    def test_payload_mismatch_on_same_identity_raises(self):
        self.process_order(order_id=8, group='A', note='collision')
        row = (self.src.storage.Task.select()
               .order_by(self.src.storage.Task.id.desc()).get())
        tampered = row.data.replace(b'collision', b'TAMPERED!')
        self.assertEqual(len(tampered), len(row.data))
        self.dst.storage.Task.create(queue=row.queue, data=tampered,
                                     priority=row.priority)
        with self.assertRaises(MigrationError):
            self.migrator(migration_id='tamper').migrate()
        # The source row is preserved when the conflict cannot be resolved.
        self.assertTrue(self.src.storage.Task.select().where(
            self.src.storage.Task.id == row.id).exists())

    def test_unselectable_and_undecodable_rows_are_skipped(self):
        self.process_order(order_id=1, group='B')
        # Raw garbage in the queue must not abort the migration.
        self.src.storage.Task.create(queue='orders', data=b'not-a-pickle',
                                     priority=0.0)
        stats = self.migrator(kinds=(KIND_QUEUE,)).migrate()
        self.assertEqual(stats.selected, 0)
        self.assertEqual(stats.migrated, 0)
        self.assertEqual(stats.deserialization_errors, 1)
        self.assertEqual(self.src.storage.queue_size(), 2)
        self.assertEqual(self.dst.storage.queue_size(), 0)

    def test_dry_run_writes_nothing(self):
        for i in range(4):
            self.process_order(order_id=i, group='A')
        migrator = self.migrator()
        stats = migrator.migrate(dry_run=True)
        self.assertEqual(stats.selected, 4)
        self.assertEqual(stats.migrated, 0)
        self.assertEqual(self.dst.storage.queue_size(), 0)
        # No progress persisted either.
        self.assertEqual(migrator.progress(), [])
        self.assertEqual(self.src.storage.queue_size(), 4)

    def test_kv_revoke_markers_and_counters(self):
        # Revoke marker for a group-A task and an unrelated result blob.
        self.src.storage.put_data('r:uuid-A', b'revoke-payload')
        self.src.storage.put_data('result:uuid-B', b'result-payload')
        self.src.storage.incr('events:groupA')
        self.src.storage.incr('events:groupA')

        def key_selector(kind, queue, key):
            return kind == KIND_KV and key.startswith('r:')

        migrator = self.migrator(kinds=ALL_KINDS, key_selector=key_selector)
        stats = migrator.migrate()
        # Only the revoke marker passes the KV selector; counters have no
        # selector and are always moved.
        self.assertEqual(stats.migrated_kinds.get(KIND_KV), 1)
        self.assertEqual(stats.migrated_kinds.get(KIND_COUNTER), 1)
        self.assertEqual(self.dst.storage.peek_data('r:uuid-A'),
                         b'revoke-payload')
        self.assertIs(self.dst.storage.peek_data('result:uuid-B'), EmptyData)
        self.assertEqual(self.src.storage.peek_data('result:uuid-B'),
                         b'result-payload')
        self.assertEqual(
            self.dst.storage.Counter.get(
                (self.dst.storage.Counter.queue == 'orders') &
                (self.dst.storage.Counter.key == 'events:groupA')).value, 2)

        # Re-run: KV conflict keeps the existing value, no duplicate rows.
        again = self.migrator(kinds=ALL_KINDS,
                              key_selector=key_selector).migrate()
        self.assertEqual(again.migrated, 0)

    def test_counter_conflict_sums_values(self):
        self.src.storage.incr('shared-counter')
        self.dst.storage.incr('shared-counter')
        self.dst.storage.incr('shared-counter')
        self.migrator(kinds=(KIND_COUNTER,)).migrate()
        value = self.dst.storage.Counter.get(
            (self.dst.storage.Counter.queue == 'orders') &
            (self.dst.storage.Counter.key == 'shared-counter')).value
        self.assertEqual(value, 3)

    def test_resume_from_persisted_progress_with_fresh_objects(self):
        for order_id in range(8):
            self.process_order(order_id=order_id, group='A')
        # First, deliberately partial pass.
        self.migrator(batch_size=3, kinds=(KIND_QUEUE,)).migrate(max_batches=1)
        moved_partial = self.dst.storage.queue_size()
        self.assertGreater(moved_partial, 0)
        self.assertLess(moved_partial, 8)

        # Drop every in-memory object and re-open both database files. Resume
        # must rely solely on the progress rows persisted in the destination.
        self.src.storage.close()
        self.dst.storage.close()
        self.src = SqlHuey('orders',
                           database='sqlite:///%s' % self.src_db, utc=False)
        self.dst = SqlHuey('orders',
                           database='sqlite:///%s' % self.dst_db, utc=False)
        resumed = self.migrator(batch_size=3, kinds=(KIND_QUEUE,))
        self.assertEqual([p.kind for p in resumed.progress()], [KIND_QUEUE])
        resumed.migrate()

        self.assertEqual(self.src.storage.queue_size(), 0)
        self.assertEqual(self.dst.storage.queue_size(), 8)
        # Final no-op re-run.
        self.assertEqual(
            self.migrator(kinds=(KIND_QUEUE,)).migrate().migrated, 0)

    def test_crash_between_destination_write_and_progress_save(self):
        # Emulate the dangerous window: destination already has the message
        # (written) while the source row and progress were never updated.
        results = [self.process_order(order_id=i, group='A') for i in range(4)]
        first_row = self.src.storage.Task.get(
            self.src.storage.Task.id == 1)
        self.dst.storage.Task.create(queue=first_row.queue,
                                     data=first_row.data,
                                     priority=first_row.priority)
        stats = self.migrator(batch_size=10).migrate()
        # The pre-existing copy is reported as a conflict, never duplicated,
        # and all four messages end up exactly once on the destination side.
        self.assertEqual(stats.conflicts, 1)
        self.assertEqual(stats.migrated, 3)
        uuids = sorted(
            self.dst.serializer.deserialize(data).id
            for (data,) in self.dst.storage.Task
            .select(self.dst.storage.Task.data).tuples())
        self.assertEqual(uuids, sorted(r.id for r in results))
        self.assertEqual(self.src.storage.queue_size(), 0)
