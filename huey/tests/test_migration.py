"""Tests for the offline FileHuey -> SqliteHuey migration tool."""
import datetime
import json
import os
import pickle
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from huey import FileHuey
from huey import SqliteHuey
from huey.consumer import Consumer
from huey.exceptions import ResultTimeout
from huey.migration import META_ID
from huey.migration import META_TABLE
from huey.migration import MigrationError
from huey.migration import MigrationPlan
from huey.migration import QUEUE_DIR
from huey.migration import SourceChanged
from huey.migration import SourceSnapshot
from huey.migration import UnrecognizedTask
from huey.migration import classify_key
from huey.migration import decode_message
from huey.migration import dangerous_globals
from huey.migration import destination_state
from huey.migration import import_plan
from huey.migration import safe_loads
from huey.migration import sample_records
from huey.migration import scan_globals
from huey.migration import verify_plan
from huey.tests import TEST_TMP_DIR


def add_impl(a, b):
    return a + b


def total_impl(*values):
    if len(values) == 1 and isinstance(values[0], (list, tuple)):
        values = tuple(values[0])
    return sum(values)


class MigrationTestCase(unittest.TestCase):
    """Base class with real FileStorage sources and SqliteStorage targets."""
    strict_fifo = False

    def setUp(self):
        self.root = tempfile.mkdtemp(dir=TEST_TMP_DIR)
        self.source_path = os.path.join(self.root, 'file-source')
        self.db_path = os.path.join(self.root, 'sqlite-target.db')
        self.name = 'app'

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- helpers ---------------------------------------------------------
    def make_source(self):
        huey = FileHuey(self.name, path=self.source_path,
                        use_thread_lock=True)
        self.add = huey.task()(add_impl)
        self.total = huey.task()(total_impl)
        return huey

    def migrate(self, migration_id='mig-1', **kwargs):
        kwargs.setdefault('strict_fifo', self.strict_fifo)
        plan = MigrationPlan(self.source_path, self.db_path, self.name,
                             migration_id=migration_id, **kwargs)
        self.assertEqual(plan.blockers, [],
                         'unexpected blockers: %s' % plan.blockers)
        return plan

    def run_import(self, plan):
        result = import_plan(plan)
        verification = verify_plan(plan)
        self.assertEqual(verification['problems'], [])
        self.assertTrue(verification['ok'])
        return result

    def source_huey_fixture(self):
        """A source with every state class the tool understands.

        Note that FileStorage runs every task with ``Huey.enqueue()``; a free
        ``eta`` on a task object is *not* routed to the schedule (only
        ``add_schedule()`` is).  ``.schedule(delay=...)`` covers that path.
        """
        huey = self.make_source()
        results = {
            'plain': self.add(1, 2),
            'second': self.add(10, 20),
        }
        # A simple two-task chain: total() runs after add(3, 4).
        results['chain'] = huey.enqueue(self.add.s(3, 4).then(self.total.s()))
        # Priority ordering: higher priority must dequeue first.
        huey.enqueue(self.add.s(100, 100, priority=10))
        huey.enqueue(self.add.s(200, 200, priority=1))
        # A real scheduled entry. Note that Huey.enqueue() always writes to
        # the queue, even for a task with a future eta; only add_schedule()
        # (used by the consumer's scheduler) writes schedule/.
        scheduled = self.add.s(5, 6)
        scheduled.eta = (datetime.datetime.now(datetime.timezone.utc)
                         .replace(tzinfo=None) +
                         datetime.timedelta(hours=2)).replace(microsecond=0)
        huey.add_schedule(scheduled)
        results['scheduled'] = scheduled
        # Completed-task result bytes (not a live task).
        huey.put_result('11111111-1111-1111-1111-111111111111', {'done': 1})
        # Revocations: a per-task and a per-class revocation.
        huey.revoke_by_id('22222222-2222-2222-2222-222222222222')
        huey.revoke_all(self.total)
        # Lock state.
        huey.lock_task('held').acquire()
        # A rate-limit window plus its counter.
        huey.put('%s.rl.demo.w' % self.name, int(time.time()) // 60)
        huey.storage.incr('%s.rl.demo' % self.name)
        # A partially-completed chord: partial result plus the counter.
        huey.put_result('chord:partial-cid:0', 11)
        huey.storage.incr('chord:partial-cid')
        huey.storage.close()
        return huey, results

    def target(self):
        huey = SqliteHuey(self.name, filename=self.db_path,
                          strict_fifo=self.strict_fifo)
        huey.task()(add_impl)
        huey.task()(total_impl)
        return huey


class TestSafeUnpickling(MigrationTestCase):
    def test_malicious_pickle_does_not_execute(self):
        marker = os.path.join(self.root, 'pwned')

        class Evil(object):
            def __reduce__(self):
                return (open, (marker, 'w'))

        Evil.__module__ = 'test_migration'

        payload = pickle.dumps(Evil(), 5)
        obj, unknown = safe_loads(payload)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(unknown, ('_io.open',))

    def test_opaque_placeholder_ignores_constructor_arguments(self):
        calls = []

        import types

        module = types.ModuleType('_migration_test_boom')
        exec('class Boom(object):\n'
             '    def __init__(self, *args, **kwargs):\n'
             '        self.constructed = True\n'
             '    def __setstate__(self, state):\n'
             '        raise AssertionError("setstate ran")\n',
             module.__dict__)
        sys.modules['_migration_test_boom'] = module
        try:
            original = module.Boom.__new__
            instance = object.__new__(module.Boom)
            payload = pickle.dumps(instance, 5)
        finally:
            del sys.modules['_migration_test_boom']
        obj, unknown = safe_loads(payload)
        self.assertEqual(unknown, ('_migration_test_boom.Boom',))

    def test_persistent_ids_and_extensions_rejected(self):
        class Persistent(object):
            def __reduce__(self):
                return (pickle.PersistentPickler, ())

        with self.assertRaises(UnrecognizedTask):
            safe_loads(b'Q' + b'not-a-pid' + b'.')

    def test_trailing_data_rejected(self):
        payload = pickle.dumps({'a': 1}, 5) + b'extra'
        with self.assertRaises(UnrecognizedTask):
            safe_loads(payload)

    def test_truncated_pickle_rejected(self):
        payload = pickle.dumps({'a': 1}, 5)[:-3]
        with self.assertRaises(UnrecognizedTask):
            safe_loads(payload)

    def test_scan_globals_reports_dangerous_reductions(self):
        class Evil(object):
            def __reduce__(self):
                return (eval, ('1+1',))

        names = scan_globals(pickle.dumps(Evil(), 5))
        self.assertIn('builtins.eval', names)
        self.assertEqual(dangerous_globals(names), ['builtins.eval'])

    def test_decode_message_reconstructs_envelope(self):
        huey = self.make_source()
        task = self.add.s(1, 2, priority=3)
        data = huey.serialize_task(task)
        msg, unknown = decode_message(data)
        self.assertEqual(msg.id, task.id)
        self.assertEqual(msg.args, (1, 2))
        self.assertEqual(msg.priority, 3)
        self.assertEqual(unknown, ())
        huey.storage.close()

    def test_decode_message_detects_serialization_mismatch(self):
        from huey.serializer import Serializer
        payload = Serializer(compression=True).serialize({'a': 1})
        with self.assertRaises(UnrecognizedTask):
            decode_message(payload, 'pickle')
        with self.assertRaises(UnrecognizedTask):
            decode_message(pickle.dumps({'a': 1}, 5), 'gzip')


class TestKeyClassification(MigrationTestCase):
    def test_classification(self):
        self.assertEqual(classify_key(str(uuid.uuid4())), 'result')
        self.assertEqual(classify_key('r:%s' % uuid.uuid4()),
                         'revocation')
        self.assertEqual(classify_key('rt:__main__.add'), 'revocation')
        self.assertEqual(classify_key('app.lock.demo'), 'lock')
        self.assertEqual(classify_key('app.rl.demo.w'), 'rate_window')
        self.assertEqual(classify_key('chord:cid:0'), 'chord_result')
        self.assertEqual(classify_key('chord:cid'), 'chord_counter')
        self.assertEqual(classify_key('some.other.key'), 'opaque')

    def test_bad_result_value_is_a_hard_error(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.put_result('33333333-3333-3333-3333-333333333333',
                        {'ok': True})
        huey.storage.close()
        # Corrupt the stored result bytes.
        from huey.storage import FileStorage
        storage = FileStorage(self.name, path=self.source_path,
                              use_thread_lock=True)
        key = b'33333333-3333-3333-3333-333333333333'
        path = storage.path_for_key(key)
        with open(path, 'wb') as fh:
            fh.write(struct.pack('>I', len(key)) + key + b'not-a-pickle')
        plan = MigrationPlan(self.source_path, self.db_path, self.name)
        self.assertTrue(any('undecodable record' in blocker
                            for blocker in plan.blockers))


class TestMigration(MigrationTestCase):
    def test_preview_does_not_touch_source_or_destination(self):
        huey, results = self.source_huey_fixture()
        from huey.migration import _tree_digest
        before = _tree_digest(self.source_path)
        plan = self.migrate()
        self.assertEqual(_tree_digest(self.source_path), before)
        self.assertFalse(os.path.exists(self.db_path))
        # Preparing a plan must not create SQLite sidecar files either.
        self.assertEqual(os.listdir(self.root), ['file-source'])
        self.assertEqual(plan.destination['fresh'], True)

    def test_full_migration_preserves_every_state_class(self):
        huey, results = self.source_huey_fixture()
        plan = self.migrate()
        counts = plan.expected_metrics()
        self.assertEqual(counts['task'], 5)
        self.assertEqual(counts['schedule'], 1)
        self.assertEqual(counts['result'], 1)  # the stored task result
        self.assertEqual(counts['revocation'], 2)
        self.assertEqual(counts['lock'], 1)
        self.assertEqual(counts['rate_window'], 1)
        self.assertEqual(counts['chord_result'], 1)  # partial chord result
        self.assertEqual(counts['counter'], 2)  # chord + rate-limit
        result = self.run_import(plan)
        self.assertEqual(result['status'], 'imported')
        for metric, expected in counts.items():
            self.assertEqual(result['counts'][metric], expected, metric)

        target = self.target()
        # Result bytes are preserved verbatim.
        self.assertEqual(
            target.result('11111111-1111-1111-1111-111111111111',
                          preserve=True), {'done': 1})
        # Revocations survive.
        self.assertTrue(target.is_revoked(
            '22222222-2222-2222-2222-222222222222'))
        self.assertTrue(target.is_revoked(self.total))
        # Locks survive.
        self.assertTrue(target.is_locked('held'))
        # Rate-limit window survives.
        self.assertIsNotNone(target.get('%s.rl.demo.w' % self.name,
                                        peek=True))
        self.assertEqual(target.storage.incr('%s.rl.demo' % self.name, 0),
                         plan.analysis.counter_values['%s.rl.demo' % self.name])
        target.storage.close()

    def test_task_ids_priorities_and_chain_are_preserved(self):
        huey, results = self.source_huey_fixture()
        source_pending = huey.pending()
        plan = self.migrate()
        self.run_import(plan)
        target = self.target()
        migrated = target.pending()
        self.assertEqual([task.id for task in migrated],
                         [task.id for task in source_pending])
        self.assertEqual([task.priority for task in migrated],
                         [task.priority for task in source_pending])
        self.assertEqual([task.data for task in migrated],
                         [task.data for task in source_pending])
        # Chain association: add(3,4) -> total().
        chained = [task for task in migrated
                   if task.on_complete is not None]
        self.assertEqual(len(chained), 1)
        self.assertEqual(chained[0].args, (3, 4))
        self.assertEqual(chained[0].on_complete.args, ())
        # Delivery order matches FileStorage's priority-desc, FIFO order.
        source_order = self.expected_queue_ids(source_pending)
        self.assertEqual([task.id for task in migrated], source_order)
        target.storage.close()

    def expected_queue_ids(self, tasks):
        # FileStorage enqueues in filename order; consumers dequeue by
        # priority desc then insertion order.
        return [task.id for task in sorted(
            tasks, key=lambda t: (-(t.priority or 0), tasks.index(t)))]

    def test_delivery_order_matches_source(self):
        huey = self.make_source()
        # Interleave priorities to make FIFO-vs-priority ordering observable.
        for priority in (0, 5, 0, 5, 0):
            huey.enqueue(self.add.s(priority, priority, priority=priority))
        source_order = [task.args[0] for task in huey.pending()]
        huey.storage.close()
        plan = self.migrate()
        self.run_import(plan)
        target = self.target()
        self.assertEqual([task.args[0] for task in target.pending()],
                         source_order)
        target.storage.close()

    def test_scheduled_eta_and_bytes_preserved(self):
        huey = self.make_source()
        eta = (datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None) + datetime.timedelta(hours=2)).replace(
                microsecond=0)
        scheduled = self.add.s(7, 8)
        scheduled.eta = eta
        huey.add_schedule(scheduled)
        result = scheduled
        source_eta = eta
        huey.storage.close()
        plan = self.migrate()
        self.run_import(plan)
        target = self.target()
        task = target.scheduled()[0]
        self.assertEqual(task.id, result.id)
        self.assertEqual(task.eta, source_eta)
        self.assertEqual(task.priority, None)
        target.storage.close()

    def test_resume_consumption_after_migration(self):
        huey, results = self.source_huey_fixture()
        huey.storage.close()
        plan = self.migrate()
        self.run_import(plan)
        target = self.target()
        consumer = Consumer(target, workers=1, periodic=False,
                            initial_delay=0.01, max_delay=0.02,
                            check_worker_health=False)
        consumer.start()
        try:
            deadline = time.time() + 20
            while target.pending_count() and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
        finally:
            consumer.stop(graceful=True)
        self.assertEqual(target.pending_count(), 0)
        self.assertEqual(target.result(results['plain'].id,
                                       blocking=True, timeout=5,
                                       preserve=True), 3)
        self.assertEqual(target.result(results['second'].id,
                                       blocking=True, timeout=5,
                                       preserve=True), 30)
        # The chain ran: total() received add(3, 4)'s return value.
        # The chain's first task ran and stored its value. The second task
        # is a total() task, and the migrated rt: revocation for total()
        # must skip it - exactly as it would have in the source.
        chain_result_ids = [result.id for result in results['chain']]
        self.assertEqual(target.result(chain_result_ids[0],
                                       blocking=True, timeout=5,
                                       preserve=True), 7)
        with self.assertRaises(ResultTimeout):
            target.result(chain_result_ids[1], blocking=True, timeout=0.2)
        # The scheduled task is still scheduled, not executed.
        self.assertEqual(target.scheduled_count(), 1)
        self.assertTrue(target.is_locked('held'))
        self.assertTrue(target.is_revoked(
            '22222222-2222-2222-2222-222222222222'))
        target.storage.close()

    def test_metadata_does_not_pollute_result_store(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        plan = self.migrate()
        self.run_import(plan)
        target = self.target()
        self.assertEqual(target.result_count(), 0)
        self.assertEqual(target.all_results(), {})
        target.storage.close()
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute('select key from %s' % META_TABLE).fetchall()
        finally:
            conn.close()
        self.assertTrue(any(row[0] == META_ID for row in rows))

    def test_idempotent_rerun_adds_no_second_delivery(self):
        huey, results = self.source_huey_fixture()
        plan = self.migrate()
        self.assertEqual(import_plan(plan)['status'], 'imported')
        conn = sqlite3.connect(self.db_path)
        try:
            before = conn.execute('select count(*) from task').fetchone()[0]
        finally:
            conn.close()
        # A restarted process rebuilds the plan from scratch with the same
        # migration id; that must not enqueue anything a second time.
        restarted = MigrationPlan(self.source_path, self.db_path, self.name,
                                  migration_id='mig-1')
        again = import_plan(restarted)
        self.assertEqual(again['status'], 'already-imported')
        conn = sqlite3.connect(self.db_path)
        try:
            after = conn.execute('select count(*) from task').fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(before, after)

    def test_interrupted_import_leaves_nothing_consumable(self):
        huey, results = self.source_huey_fixture()
        plan = self.migrate()
        # Simulate a crash inside the transaction by making the marker write
        # fail after all tasks/schedule/kv rows have been inserted.
        original = plan.analysis.counters_for_migration
        calls = []

        def fail(*args, **kwargs):
            calls.append(1)
            raise RuntimeError('simulated crash')

        plan.analysis.counters_for_migration = fail
        with self.assertRaises(RuntimeError):
            import_plan(plan)
        plan.analysis.counters_for_migration = original
        self.assertTrue(calls)
        state = destination_state(self.db_path, self.name)
        # Task taken with BEGIN...COMMIT: either nothing or everything.
        rows = sum(state['counts'].values()) if state['counts'] else 0
        self.assertEqual(rows, 0)
        self.assertIsNone(state['marker'])
        # A restarted process (fresh plan, same migration id) imports once.
        restarted = MigrationPlan(self.source_path, self.db_path, self.name,
                                   migration_id='mig-1')
        self.assertEqual(restarted.blockers, [])
        self.assertEqual(import_plan(restarted)['status'], 'imported')
        self.assertEqual(verify_plan(restarted)['problems'], [])
        state = destination_state(self.db_path, self.name)
        self.assertEqual(state['counts']['task'],
                         restarted.expected_metrics()['task'])

    def test_preview_does_not_create_sidecars_on_closed_destination(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        # An empty SqliteHuey database, closed cleanly.
        target = SqliteHuey(self.name, filename=self.db_path)
        target.storage.close()
        self.assertEqual(sorted(os.listdir(self.root)),
                         ['file-source', 'sqlite-target.db'])
        plan = MigrationPlan(self.source_path, self.db_path, self.name,
                             migration_id='mig-sidecar')
        self.assertEqual(plan.blockers, [])
        self.assertTrue(any('empty SqliteHuey database' in warning
                            for warning in plan.warnings))
        self.assertEqual(sorted(os.listdir(self.root)),
                         ['file-source', 'sqlite-target.db'])
        self.assertEqual(import_plan(plan)['status'], 'imported')
        self.assertEqual(verify_plan(plan)['problems'], [])

    def test_zero_byte_abandoned_destination_is_reused(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        # An earlier attempt can leave a zero-byte file after a rolled-back
        # schema creation.
        open(self.db_path, 'wb').close()
        plan = self.migrate(migration_id='mig-zero')
        result = import_plan(plan)
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(verify_plan(plan)['problems'], [])
        retried = MigrationPlan(self.source_path, self.db_path, self.name,
                                migration_id='mig-zero')
        self.assertEqual(retried.blockers, [])
        self.assertEqual(import_plan(retried)['status'], 'already-imported')

    def test_strict_fifo_requires_matching_destination(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        # Default (non-strict) SqliteHuey database, closed cleanly.
        SqliteHuey(self.name, filename=self.db_path).storage.close()
        plan = MigrationPlan(self.source_path, self.db_path, self.name,
                             migration_id='mig-fifo', strict_fifo=True)
        self.assertTrue(any('AUTOINCREMENT' in blocker
                            for blocker in plan.blockers))
        with self.assertRaises(MigrationError):
            import_plan(plan)

    def test_verify_detects_tampering(self):
        huey, results = self.source_huey_fixture()
        plan = self.migrate()
        self.run_import(plan)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("update task set priority = priority + 1 "
                         "where id = (select min(id) from task)")
            conn.commit()
        finally:
            conn.close()
        report = verify_plan(plan)
        self.assertFalse(report['ok'])
        self.assertTrue(any('priority' in problem
                            for problem in report['problems']))

    def test_verify_detects_missing_result(self):
        huey, results = self.source_huey_fixture()
        plan = self.migrate()
        self.run_import(plan)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute('delete from kv where key = ?',
                         ('11111111-1111-1111-1111-111111111111',))
            conn.commit()
        finally:
            conn.close()
        report = verify_plan(plan)
        self.assertFalse(report['ok'])
        self.assertTrue(any('missing key' in problem
                            for problem in report['problems']))

    def test_completed_task_bytes_are_exact(self):
        huey = self.make_source()
        self.add(1, 2)  # Enqueue only; results byte-exactness is about kv.
        huey.put_result('44444444-4444-4444-4444-444444444444',
                        {'payload': list(range(50))})
        source_bytes = dict(huey.storage.result_items())[
            '44444444-4444-4444-4444-444444444444']
        huey.storage.close()
        plan = self.migrate()
        self.run_import(plan)
        conn = sqlite3.connect(self.db_path)
        try:
            stored = conn.execute(
                'select value from kv where key = ?',
                ('44444444-4444-4444-4444-444444444444',)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(bytes(stored), source_bytes)


class TestStrictFifoMigration(MigrationTestCase):
    strict_fifo = True

    def test_strict_fifo_schema_is_used(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        plan = self.migrate()
        self.run_import(plan)
        conn = sqlite3.connect(self.db_path)
        try:
            sql = conn.execute(
                "select sql from sqlite_master where type='table' "
                "and name='task'").fetchone()[0]
        finally:
            conn.close()
        self.assertIn('AUTOINCREMENT', sql.upper())
        target = self.target()
        self.assertEqual(target.pending_count(), 1)
        target.storage.close()


class TestConflicts(MigrationTestCase):
    def test_same_id_different_source_is_refused(self):
        huey = self.make_source()
        self.add(1, 2)
        plan = self.migrate(migration_id='cutover')
        self.run_import(plan)
        # The source changes after import.
        huey2 = FileHuey(self.name, path=self.source_path,
                         use_thread_lock=True)
        add2 = huey2.task()(add_impl)
        add2(50, 50)
        huey2.storage.close()
        conflicting = MigrationPlan(self.source_path, self.db_path, self.name,
                                    migration_id='cutover')
        self.assertTrue(conflicting.blockers)
        self.assertIn('different source snapshot',
                      ' '.join(conflicting.blockers))
        with self.assertRaises(MigrationError):
            import_plan(conflicting)

    def test_different_id_on_populated_destination_is_refused(self):
        huey = self.make_source()
        self.add(1, 2)
        plan = self.migrate(migration_id='first')
        self.run_import(plan)
        other = MigrationPlan(self.source_path, self.db_path, self.name,
                              migration_id='second')
        self.assertTrue(other.blockers)
        self.assertIn('already holds migration',
                      ' '.join(other.blockers))

    def test_destination_with_another_queues_migration_is_refused(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        plan = self.migrate(migration_id='queue-a')
        self.run_import(plan)
        # Same database, same on-disk state, but a different queue name. The
        # name matches nothing in the source lock keys, so use a fresh
        # source whose lock keys agree.
        other = MigrationPlan(self.source_path, self.db_path, self.name,
                              migration_id='queue-b')
        self.assertTrue(any('already holds migration' in blocker
                            for blocker in other.blockers))

    def test_unrelated_non_empty_destination_is_refused(self):
        target = SqliteHuey(self.name, filename=self.db_path)
        task = target.task()(add_impl)
        task(1, 2)
        target.storage.close()
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        plan = MigrationPlan(self.source_path, self.db_path, self.name,
                             migration_id='mig-x')
        # SqliteHuey's schema has no migration table, so it is reported as
        # missing rather than as a populated target.
        self.assertTrue(plan.blockers)
        self.assertIn('not created by this migration tool',
                      ' '.join(plan.blockers))

    def test_open_destination_is_refused(self):
        huey = self.make_source()
        self.add(1, 2)
        target = SqliteHuey(self.name, filename=self.db_path)
        target.task()(add_impl)
        with target.storage.db(commit=True) as curs:
            curs.execute('select 1')
        # Force WAL sidecars to exist by holding a write transaction open.
        conn = sqlite3.connect(self.db_path)
        conn.execute('pragma journal_mode=wal')
        conn.execute('insert into kv (queue, key, value) values (?, ?, ?)',
                     (self.name, 'x', b'y'))
        conn.commit()
        self.assertTrue(os.path.exists(self.db_path + '-wal'))
        plan = MigrationPlan(self.source_path, self.db_path, self.name,
                             migration_id='mig-y')
        self.assertTrue(any('sidecar' in blocker
                            for blocker in plan.blockers))
        conn.close()
        target.storage.close()
        huey.storage.close()

    def test_queue_name_mismatch_is_refused(self):
        huey = FileHuey('real-name', path=self.source_path,
                        use_thread_lock=True)
        task = huey.task()(add_impl)
        task(1, 2)
        huey.lock_task('x').acquire()
        huey.storage.close()
        plan = MigrationPlan(self.source_path, self.db_path, 'wrong-name',
                             migration_id='mig')
        self.assertTrue(any('does not match the name recorded' in blocker
                            for blocker in plan.blockers))

    def test_queue_name_inferred_from_lock_keys_is_accepted(self):
        huey = FileHuey('real-name', path=self.source_path,
                        use_thread_lock=True)
        task = huey.task()(add_impl)
        task(1, 2)
        huey.lock_task('x').acquire()
        huey.storage.close()
        plan = MigrationPlan(self.source_path, self.db_path, 'real-name',
                             migration_id='mig')
        self.assertEqual(plan.blockers, [])
        self.assertEqual(plan.queue_identity['evidence'],
                         'lock/rate-limit keys')


class TestUnknownDataPolicy(MigrationTestCase):
    def _source_with_opaque_key(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.put('application.cache.key', {'v': 1})
        huey.storage.close()

    def test_reject_is_default(self):
        self._source_with_opaque_key()
        plan = MigrationPlan(self.source_path, self.db_path, self.name,
                             migration_id='mig')
        self.assertTrue(any('outside the recognized' in blocker
                            for blocker in plan.blockers))
        self.assertTrue(any('application.cache.key' in blocker
                            for blocker in plan.blockers))

    def test_skip_leaves_them_behind_and_lists_them(self):
        self._source_with_opaque_key()
        plan = self.migrate(unrecognized='skip')
        self.assertEqual(plan.summary()['skipped']['unexplained_keys'],
                         ['application.cache.key'])
        self.run_import(plan)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute('select 1 from kv where key = ?',
                               ('application.cache.key',)).fetchone()
            manifest = conn.execute(
                'select value from %s where key = ?' % META_TABLE,
                ('manifest',)).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(row)
        self.assertIn('application.cache.key', json.loads(manifest)['skipped'][
            'unexplained_keys'])

    def test_copy_migrates_verbatim(self):
        self._source_with_opaque_key()
        plan = self.migrate(unrecognized='copy')
        self.run_import(plan)
        huey = self.make_source()
        value = huey.storage.peek_data('application.cache.key')
        huey.storage.close()
        conn = sqlite3.connect(self.db_path)
        try:
            stored = conn.execute('select value from kv where key = ?',
                                  ('application.cache.key',)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(bytes(stored), value)

    def test_unattributable_counter_rejected_by_default(self):
        huey = self.make_source()
        self.add(1, 2)
        # A counter with no identifying key anywhere in the tree.
        huey.storage.incr('mystery-counter')
        huey.storage.close()
        plan = MigrationPlan(self.source_path, self.db_path, self.name,
                             migration_id='mig')
        self.assertTrue(any('cannot be attributed' in blocker
                            for blocker in plan.blockers))

    def test_unattributable_counter_can_be_skipped_explicitly(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.incr('mystery-counter')
        huey.storage.close()
        plan = self.migrate(counters='skip')
        self.assertTrue(any('unattributable counter' in warning
                            for warning in plan.warnings))
        self.run_import(plan)
        manifest = json.loads(
            sqlite3.connect(self.db_path).execute(
                'select value from %s where key = ?' % META_TABLE,
                ('manifest',)).fetchone()[0])
        self.assertEqual(len(manifest['skipped']['counters']), 1)

    def test_undecodable_task_blocks_migration(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        # Drop a garbage file into the queue directory.
        with open(os.path.join(self.source_path, QUEUE_DIR,
                               'ffff-000000000001'), 'wb') as fh:
            fh.write(b'not a pickle at all')
        plan = MigrationPlan(self.source_path, self.db_path, self.name,
                             migration_id='mig')
        self.assertTrue(any('undecodable task' in blocker
                            for blocker in plan.blockers))

    def test_unexpected_file_blocks_migration(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        with open(os.path.join(self.source_path, QUEUE_DIR, 'junk'), 'wb') as fh:
            fh.write(b'x')
        plan = MigrationPlan(self.source_path, self.db_path, self.name,
                             migration_id='mig')
        self.assertTrue(any('unexpected queue filename' in blocker
                            for blocker in plan.blockers))


class TestSourceQuiescence(MigrationTestCase):
    def test_source_snapshot_is_self_consistent(self):
        huey, results = self.source_huey_fixture()
        snapshot = SourceSnapshot(self.source_path)
        first = snapshot.fingerprint()
        second = SourceSnapshot(self.source_path).fingerprint()
        self.assertEqual(first, second)

    def test_fingerprint_ignores_mtime_only_changes(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        first = SourceSnapshot(self.source_path).fingerprint()
        # A restore from backup can reset mtimes without changing content.
        for root, dirs, files in os.walk(self.source_path):
            for name in files:
                os.utime(os.path.join(root, name), (1, 1))
        self.assertEqual(SourceSnapshot(self.source_path).fingerprint(),
                         first)

    def test_changed_source_is_detected_before_import(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        plan = self.migrate()
        # Another writer appends to the source after the plan was built.
        late = FileHuey(self.name, path=self.source_path,
                        use_thread_lock=True)
        late.task()(add_impl)(5, 5)
        late.storage.close()
        with self.assertRaises(SourceChanged):
            import_plan(plan)
        self.assertFalse(os.path.exists(self.db_path))

    def test_late_writer_rolls_back_nothing_partial(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        plan = self.migrate()
        late = FileHuey(self.name, path=self.source_path,
                        use_thread_lock=True)
        late.task()(add_impl)(5, 5)
        late.storage_close = None
        late.storage.close()
        try:
            import_plan(plan)
        except SourceChanged:
            pass
        state = destination_state(self.db_path, self.name)
        self.assertFalse(state['exists'])


class TestSampling(MigrationTestCase):
    def test_safe_sample_marks_opaque_arguments(self):
        huey = self.make_source()
        self.add(1, 2)
        self.add.s(complex(1, 2))
        huey.storage.close()
        plan = self.migrate()
        rows = sample_records(plan, limit=2)
        self.assertTrue(rows)
        for row in rows:
            self.assertIsNotNone(row['name'])

    def test_sample_with_real_unpickler(self):
        huey = self.make_source()
        result = self.add(4, 5)
        huey.storage.close()
        plan = self.migrate()
        rows = sample_records(plan, limit=1, app_huey=huey)
        self.assertEqual(rows[0]['args'], '(4, 5)')
        self.assertEqual(rows[0]['id'], result.id)


class TestCli(MigrationTestCase):
    def run_cli(self, *args):
        command = [sys.executable, '-m', 'huey.migration'] + list(args)
        return subprocess.run(command, capture_output=True, text=True,
                              cwd=os.path.dirname(os.path.dirname(
                                  os.path.dirname(__file__))))

    def common(self):
        return ['--source', self.source_path, '--destination', self.db_path,
                '--queue-name', self.name]

    def test_check_and_preview_write_nothing(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        for command in ('check', 'preview'):
            done = self.run_cli(command, *self.common(), '--json')
            self.assertEqual(done.returncode, 0, done.stderr)
            report = json.loads(done.stdout)
            self.assertTrue(report['ok'])
            self.assertEqual(report['counts']['task'], 1)
            self.assertFalse(os.path.exists(self.db_path))

    def test_run_verify_rerun(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        done = self.run_cli('run', *self.common(),
                            '--migration-id', 'cli-1', '--json')
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertEqual(report['status'], 'imported')
        self.assertTrue(report['ok'])

        done = self.run_cli('run', *self.common(),
                            '--migration-id', 'cli-1', '--json')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)['status'],
                         'already-imported')

        done = self.run_cli('verify', *self.common(),
                            '--migration-id', 'cli-1', '--json')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(json.loads(done.stdout)['ok'])

    def test_blocked_run_exits_nonzero(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.put('unexplained.key', 1)
        huey.storage.close()
        done = self.run_cli('run', *self.common(), '--json')
        self.assertEqual(done.returncode, 1)
        report = json.loads(done.stdout)
        self.assertFalse(report['ok'])
        self.assertFalse(os.path.exists(self.db_path))

    def test_sample_requires_unsafe_opt_in_for_app(self):
        huey = self.make_source()
        self.add(1, 2)
        huey.storage.close()
        done = self.run_cli('sample', *self.common(),
                            '--app', 'huey.tests.test_migration:add_impl')
        self.assertEqual(done.returncode, 2)
        self.assertIn('--allow-unsafe-pickle', done.stdout)

    def test_subprocess_consumer_runs_a_migrated_queue(self):
        """A real huey consumer child process drains the migrated queue."""
        huey = self.make_source()
        result = self.add(4, 5)
        huey.put_result('11111111-1111-1111-1111-111111111111', 'kept')
        huey.revoke_by_id('22222222-2222-2222-2222-222222222222')
        huey.lock_task('held').acquire()
        huey.storage.close()
        done = self.run_cli('run', *self.common(),
                            '--migration-id', 'proc-1', '--json')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        script = os.path.join(self.root, 'consume.py')
        with open(script, 'w') as fh:
            fh.write(
                'import sys\n'
                'sys.path.insert(0, %r)\n'
                'from huey import SqliteHuey\n'
                'from huey.consumer import Consumer\n'
                'from huey.tests.test_migration import add_impl, total_impl\n'
                'h = SqliteHuey(%r, filename=%r)\n'
                'h.task()(add_impl); h.task()(total_impl)\n'
                'c = Consumer(h, workers=1, periodic=False, initial_delay='
                '0.01, max_delay=0.02, check_worker_health=False)\n'
                'c.start()\n'
                'import time\n'
                'deadline = time.time() + 20\n'
                'while h.pending_count() and time.time() < deadline:\n'
                '    time.sleep(0.02)\n'
                'time.sleep(0.2)\n'
                'c.stop(graceful=True)\n'
                'print(h.pending_count())\n'
                'print(h.result(%r, blocking=True, timeout=5, '
                'preserve=True))\n'
                'print(h.is_locked(\'held\'))\n'
                'print(h.is_revoked(%r))\n'
                'print(h.result(%r, preserve=True))\n'
                % (os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__)))),
                   self.name, self.db_path, result.id,
                   '22222222-2222-2222-2222-222222222222',
                   '11111111-1111-1111-1111-111111111111'))
        environment = dict(os.environ)
        environment['PYTHONPATH'] = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        done = subprocess.run([sys.executable, script], capture_output=True,
                              text=True, env=environment, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = done.stdout.strip().splitlines()
        self.assertEqual(lines[-5:], ['0', '9', 'True', 'True', 'kept'],
                         done.stdout)

    def test_demo_runs(self):
        done = self.run_cli('demo')
        self.assertFalse(done.stderr, done.stderr)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('Demo complete', done.stdout)
        self.assertIn('all checks passed', done.stdout)
        self.assertNotIn('FAIL:', done.stdout)
        self.assertIn('scheduled:   1', done.stdout)


if __name__ == '__main__':
    unittest.main()
