import datetime
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import unittest

from huey import BatchConflictError
from huey import BatchIdConflictError
from huey import BatchPublishError
from huey import SqliteHuey
from huey import chord
from huey import group
from huey.api import BatchResult
from huey.api import ChordResult
from huey.api import Result
from huey.api import ResultGroup
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase
from huey.tests.batch_procs import build

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


class BatchTestCase(BaseTestCase):
    counter = 0

    def get_huey(self):
        BatchTestCase.counter += 1
        filename = os.path.join(
            TEST_TMP_DIR, 'batch-%s.db' % BatchTestCase.counter)
        return SqliteHuey(filename=filename, utc=False)

    def tearDown(self):
        self.huey.storage.close()
        super(BatchTestCase, self).tearDown()
        for suffix in ('', '-wal', '-shm'):
            path = self.huey.storage.filename + suffix
            if os.path.exists(path):
                os.unlink(path)

    def declare(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        @self.huey.task()
        def total(values):
            return sum(values)

        return add, total

    def drain(self):
        """Dequeue and execute everything currently queued."""
        values = []
        while True:
            task = self.huey.dequeue()
            if task is None:
                return values
            values.append(self.huey.execute(task))

    def table_count(self, table):
        return self.huey.storage.sql('select count(*) from %s' % table,
                                     results=True)[0][0]


class TestBatchBasics(BatchTestCase):
    def test_publishes_in_input_order(self):
        add, _ = self.declare()
        result = self.huey.publish_batch([add.s(1, 1), add.s(2, 2),
                                          add.s(3, 3)])
        self.assertTrue(isinstance(result, BatchResult))
        self.assertEqual(len(result), 3)
        ids = [r.id for r in result]
        pending = self.huey.pending()
        self.assertEqual([t.id for t in pending], ids)
        self.assertEqual(self.drain(), [2, 4, 6])
        self.assertEqual(result.get(), [2, 4, 6])

    def test_preserves_id_priority_and_eta(self):
        add, _ = self.declare()
        eta = datetime.datetime(2030, 1, 1)
        result = self.huey.publish_batch([
            add.s(1, 1, id='low', priority=1),
            add.s(2, 2, id='high', priority=9),
            add.s(3, 3, id='later', eta=eta)])
        self.assertEqual([r.id for r in result], ['low', 'high', 'later'])
        pending = self.huey.pending()
        self.assertEqual([t.id for t in pending], ['high', 'low', 'later'])
        self.assertEqual(pending[0].priority, 9)
        self.assertEqual(pending[1].priority, 1)
        # Like ordinary enqueue, an eta task sits in the queue until a worker
        # picks it up, then moves to the schedule. Its eta survives intact.
        self.assertEqual(pending[2].eta, eta)
        self.huey.execute(self.huey.dequeue())  # 'high'
        self.huey.execute(self.huey.dequeue())  # 'low'
        later = self.huey.dequeue()
        self.assertEqual(later.id, 'later')
        self.assertIsNone(self.huey.execute(later))
        self.assertEqual([t.id for t in self.huey.scheduled()], ['later'])
        self.assertEqual(self.huey.scheduled()[0].eta, eta)

    def test_ordinary_enqueue_unchanged(self):
        add, _ = self.declare()
        r = add(1, 2)
        self.assertTrue(isinstance(r, Result))
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertEqual(self.drain(), [3])

    def test_empty_batch(self):
        add, _ = self.declare()
        result = self.huey.publish_batch([])
        self.assertEqual(len(result), 0)
        self.assertEqual(list(result), [])
        self.assertEqual(result.get(), [])
        self.assertEqual(self.huey.pending_count(), 0)

        result = self.huey.publish_batch([], key='empty-key')
        self.assertEqual(len(result), 0)
        self.assertEqual(len(self.huey.get_batch('empty-key')), 0)
        self.assertEqual(self.huey.pending_count(), 0)

    def test_pipeline_and_group(self):
        add, total = self.declare()
        pipeline = add.s(1, 1).then(add, 10).then(add, 100)
        result = self.huey.publish_batch([pipeline, group([add.s(2, 2),
                                                           add.s(3, 3)])])
        self.assertTrue(isinstance(result[0], ResultGroup))
        self.assertEqual(len(result[0]), 3)
        self.assertTrue(isinstance(result[1], ResultGroup))
        self.assertEqual(len(result[1]), 2)
        self.assertEqual(self.huey.pending_count(), 3)  # Pipeline + 2 heads.
        self.drain()
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(result[0].get(), [2, 12, 112])
        self.assertEqual(result[1].get(), [4, 6])

    def test_nested_chord(self):
        add, total = self.declare()
        # A chord whose first member is itself a chord, plus an extra member
        # and a chord callback pipeline.
        inner = chord([add.s(1, 1), add.s(2, 2)], total)
        outer = chord([inner, add.s(3, 3).then(add, 1)],
                      total.s().then(add, 1000))
        result = self.huey.publish_batch([outer, add.s(10, 10)])
        self.assertTrue(isinstance(result[0], ChordResult))
        # The chord callback has a .then(), so a pipeline is exposed.
        self.assertTrue(isinstance(result[0].pipeline_results, ResultGroup))
        self.assertEqual(len(result[0].pipeline_results), 2)
        self.assertEqual(self.huey.pending_count(), 4)  # Inner 2 + 1 + 1.
        # The whole graph drains without a half-built chord: the outer
        # callback only runs after both members (one of which is a nested
        # chord) have reported.
        self.drain()
        self.assertEqual(self.huey.pending_count(), 0)
        # Chord bookkeeping is cleaned up once the callbacks have run.
        self.assertEqual(self.table_count('counter'), 0)
        # inner: 2 + 4 = 6; other member: (3 + 3) + 1 = 7.
        # outer callback: 6 + 7 = 13, then + 1000.
        # Results are a destructive read, so read the pipeline first.
        self.assertEqual(result[0].pipeline_results.get(), [13, 1013])
        self.assertEqual(result[1].get(), 20)

    def test_empty_chord(self):
        _, total = self.declare()
        result = self.huey.publish_batch([chord([], total)])
        self.assertTrue(isinstance(result[0], ChordResult))
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertEqual(self.drain(), [0])
        self.assertEqual(result[0].get(), 0)

    def test_empty_group_and_nested_empty_chord(self):
        add, total = self.declare()
        result = self.huey.publish_batch([
            group([]),
            group([chord([], total), add.s(1, 1)])], key='empties')
        self.assertEqual(len(result[0]), 0)
        self.assertEqual(len(result[1]), 2)
        self.assertEqual(self.huey.pending_count(), 2)  # add + empty chord cb.
        self.drain()
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(result[1].get(), [0, 2])

    def test_flush_all_clears_batch_state(self):
        add, _ = self.declare()
        self.huey.publish_batch([add.s(1, 1, id='f1')], key='f')
        self.assertTrue(self.huey.get_batch('f') is not None)
        self.huey.flush()
        self.assertTrue(self.huey.get_batch('f') is None)
        # Ids are released with the batch state, so they can be used again.
        self.huey.publish_batch([add.s(1, 1, id='f1')], key='f')
        self.assertEqual(self.huey.pending_count(), 1)

    def test_chord_callback_id_reserved(self):
        add, total = self.declare()
        callback = total.s(id='shared-callback')
        self.huey.publish_batch([chord([add.s(1, 1)], callback)])
        # The chord callback is not written yet (it is enqueued on
        # completion), but its id now belongs to the batch.
        self.assertEqual(self.huey.pending_count(), 1)
        with self.assertRaises(BatchIdConflictError):
            self.huey.publish_batch([add.s(2, 2, id='shared-callback')])
        self.assertEqual(self.huey.pending_count(), 1)


class TestBatchAtomicity(BatchTestCase):
    def test_unserializable_member_writes_nothing(self):
        add, _ = self.declare()

        class Unpicklable(object):
            def __reduce__(self):
                raise TypeError('cannot pickle me')

        before = self.table_count('task')
        ids_before = self.table_count('task_id')
        with self.assertRaises(TypeError):
            self.huey.publish_batch([add.s(1, 1), add.s(Unpicklable(), 1)])
        self.assertEqual(self.table_count('task'), before)
        self.assertEqual(self.table_count('task_id'), ids_before)
        self.assertEqual(self.huey.pending_count(), 0)

    def test_duplicate_ids_in_batch_rejected(self):
        add, _ = self.declare()
        with self.assertRaises(BatchIdConflictError):
            self.huey.publish_batch([add.s(1, 1, id='same'),
                                     add.s(2, 2, id='same')])
        self.assertEqual(self.huey.pending_count(), 0)
        # Ids are reserved, so the rejected batch left nothing behind.
        self.huey.publish_batch([add.s(3, 3, id='same')])
        self.assertEqual(self.huey.pending_count(), 1)

    def test_id_conflict_with_previous_batch(self):
        add, _ = self.declare()
        self.huey.publish_batch([add.s(1, 1, id='fixed')])
        with self.assertRaises(BatchIdConflictError):
            self.huey.publish_batch([add.s(2, 2), add.s(3, 3, id='fixed')])
        self.assertEqual(self.huey.pending_count(), 1)

    def test_failure_mid_insert_rolls_back(self):
        add, _ = self.declare()
        good = [add.s(i, i) for i in range(5)]
        # Corrupt the task table so the third insert fails, after the first
        # two have been executed inside the transaction.
        self.huey.storage.sql(
            'create trigger fail_insert before insert on task '
            'when (select count(*) from task) >= 2 '
            'begin select raise(abort, "boom"); end')
        try:
            with self.assertRaises(BatchPublishError) as ctx:
                self.huey.publish_batch(good)
        finally:
            self.huey.storage.sql('drop trigger fail_insert')
        self.assertTrue(isinstance(ctx.exception.__cause__, sqlite3.Error))
        self.assertEqual(self.huey.pending_count(), 0)
        # The transaction was rolled back, including the id reservations.
        self.assertEqual(self.table_count('task_id'), 0)
        # And the same batch can be published now.
        self.huey.publish_batch(good)
        self.assertEqual(self.huey.pending_count(), 5)

    def test_missing_schema_does_not_leave_transaction_open(self):
        # create_tables=False and no schema: the failure must roll back and
        # classify, not leave the connection inside a transaction.
        huey = SqliteHuey(filename=os.path.join(TEST_TMP_DIR, 'batch-ct.db'),
                          utc=False, create_tables=False)
        try:
            @huey.task()
            def task_a(a):
                return a

            with self.assertRaises(BatchPublishError):
                huey.publish_batch([task_a.s(1)])
            self.assertFalse(huey.storage.conn.in_transaction)
            huey.storage.initialize_schema()
            huey.publish_batch([task_a.s(2)])
            self.assertEqual(huey.pending_count(), 1)
        finally:
            huey.storage.close()
            for suffix in ('', '-wal', '-shm'):
                path = huey.storage.filename + suffix
                if os.path.exists(path):
                    os.unlink(path)

    def test_write_failure_leaves_no_idempotency_record(self):
        add, _ = self.declare()
        self.huey.storage.sql(
            'create trigger fail_insert before insert on task '
            'begin select raise(abort, "boom"); end')
        try:
            with self.assertRaises(BatchPublishError):
                self.huey.publish_batch([add.s(1, 1)], key='k1')
        finally:
            self.huey.storage.sql('drop trigger fail_insert')
        self.assertTrue(self.huey.get_batch('k1') is None)
        self.huey.publish_batch([add.s(1, 1)], key='k1')
        self.assertTrue(self.huey.get_batch('k1') is not None)

    def test_failed_batch_leaves_no_chord_or_result_state(self):
        add, total = self.declare()
        self.huey.publish_batch([add.s(1, 1, id='ok')])
        self.huey.storage.sql(
            'create trigger fail_insert before insert on task '
            'when (select count(*) from task) >= 2 '
            'begin select raise(abort, "boom"); end')
        try:
            with self.assertRaises(BatchPublishError):
                self.huey.publish_batch([
                    chord([add.s(2, 2), add.s(3, 3)], total.s(id='cb'))])
        finally:
            self.huey.storage.sql('drop trigger fail_insert')
        # No chord counters, results or reserved ids survive the rollback.
        self.assertEqual(self.table_count('counter'), 0)
        self.assertEqual(self.table_count('kv'), 0)
        self.assertEqual(self.huey.pending_count(), 1)
        # Nor is the failed batch's callback id burned.
        self.huey.publish_batch([add.s(4, 4, id='cb')])
        self.assertEqual(self.huey.pending_count(), 2)

    def test_open_caller_transaction_is_not_touched(self):
        add, _ = self.declare()
        conn = self.huey.storage.conn
        conn.execute('create table caller_data (value)')
        conn.execute('begin immediate')
        conn.execute('insert into caller_data values (1)')
        try:
            with self.assertRaises(BatchPublishError):
                self.huey.publish_batch([add.s(1, 1)])
            self.assertTrue(conn.in_transaction)
            self.assertEqual(conn.execute(
                'select value from caller_data').fetchall(), [(1,)])
            self.assertEqual(self.huey.pending_count(), 0)
            # It is still the caller's transaction to commit.
            conn.commit()
        finally:
            if conn.in_transaction:
                conn.rollback()
        self.assertEqual(conn.execute(
            'select value from caller_data').fetchall(), [(1,)])
        self.huey.publish_batch([add.s(2, 2)])
        self.assertEqual(self.huey.pending_count(), 1)

    def test_commit_failure_rolls_back_and_classifies(self):
        add, _ = self.declare()
        real_conn = self.huey.storage.conn  # Force the connection open.

        class FailingCommit(object):
            def __init__(self, real):
                self._real = real
            def __getattr__(self, name):
                return getattr(self._real, name)
            def commit(self):
                raise sqlite3.OperationalError('disk I/O error')

        self.huey.storage._conn = FailingCommit(real_conn)
        try:
            with self.assertRaises(BatchPublishError) as ctx:
                self.huey.publish_batch([add.s(1, 1), add.s(2, 2)])
        finally:
            self.huey.storage._conn = real_conn
        self.assertTrue(isinstance(ctx.exception.__cause__, sqlite3.Error))
        self.assertFalse(real_conn.in_transaction)
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(self.table_count('task_id'), 0)
        self.huey.publish_batch([add.s(3, 3)])
        self.assertEqual(self.huey.pending_count(), 1)

    def test_caller_transaction_inside_db_block(self):
        # The same guard must work when the caller holds huey's lock through
        # db(), i.e. it must raise rather than deadlock or commit for them.
        add, _ = self.declare()
        with self.huey.storage.db(commit=True) as curs:
            curs.execute('create table caller_tx (value)')
            curs.execute('insert into caller_tx values (7)')
            with self.assertRaises(BatchPublishError):
                self.huey.publish_batch([add.s(1, 1)])
            self.assertEqual(curs.execute(
                'select value from caller_tx').fetchall(), [(7,)])
        self.assertEqual(self.huey.pending_count(), 0)
        # The db() block committed the caller's own transaction normally.
        self.assertEqual(self.huey.storage.sql(
            'select value from caller_tx', results=True), [(7,)])
        self.huey.publish_batch([add.s(2, 2)])
        self.assertEqual(self.huey.pending_count(), 1)

    def test_other_connection_cannot_see_mid_transaction_rows(self):
        # Deterministic version of the visibility requirement: a trigger
        # samples an independent connection after the first rows are written
        # but before the commit. That connection must still see nothing.
        add, _ = self.declare()
        filename = self.huey.storage.filename
        observed = []

        def probe():
            other = sqlite3.connect(filename, timeout=1)
            try:
                observed.append(other.execute(
                    'select count(*) from task').fetchone()[0])
            finally:
                other.close()
            return 0

        self.huey.storage.conn.create_function('probe_batch', 0, probe)
        self.huey.storage.sql(
            'create trigger probe_batch after insert on task '
            'when (select count(*) from task) = 2 '
            'begin select probe_batch(); end')
        try:
            self.huey.publish_batch([add.s(1, 1), add.s(2, 2), add.s(3, 3)])
        finally:
            self.huey.storage.sql('drop trigger probe_batch')
        self.assertEqual(observed, [0])
        self.assertEqual(self.huey.pending_count(), 3)

    def test_multiple_consumers_never_see_half_batch(self):
        add, _ = self.declare()
        n_batches, batch_size = 40, 4
        stop = threading.Event()
        problems = []

        def consume():
            # A separate connection, like a real worker process.
            conn = sqlite3.connect(self.huey.storage.filename)
            try:
                while not stop.is_set():
                    # One snapshot sees either none or all of each batch;
                    # any snapshot with a partial batch is a failure.
                    rows = conn.execute(
                        'select data from task order by id').fetchall()
                    counts = {}
                    for (data,) in rows:
                        task = self.huey.deserialize_task(data)
                        counts[task.args[0]] = counts.get(task.args[0], 0) + 1
                    for batch, count in counts.items():
                        if count != batch_size:
                            problems.append((batch, count))
            finally:
                conn.close()

        consumers = [threading.Thread(target=consume) for _ in range(2)]
        for t in consumers:
            t.start()
        try:
            for n in range(n_batches):
                self.huey.publish_batch(
                    [add.s(n * 10, i) for i in range(batch_size)])
                time.sleep(0.001)
        finally:
            stop.set()
            for t in consumers:
                t.join(timeout=10)

        self.assertEqual(problems, [])
        self.assertEqual(self.huey.pending_count(), n_batches * batch_size)
        self.assertEqual(len(set(self.drain())), n_batches * batch_size)
        self.assertEqual(self.huey.pending_count(), 0)


class TestBatchIdempotency(BatchTestCase):
    def test_retry_with_same_key_returns_original(self):
        add, _ = self.declare()
        first = self.huey.publish_batch([add.s(1, 1), add.s(2, 2)], key='k')
        second = self.huey.publish_batch([add.s(1, 1), add.s(2, 2)], key='k')
        self.assertEqual([r.id for r in first], [r.id for r in second])
        self.assertEqual(self.huey.pending_count(), 2)

    def test_retry_with_different_content_rejected(self):
        add, _ = self.declare()
        self.huey.publish_batch([add.s(1, 1)], key='k')
        with self.assertRaises(BatchConflictError):
            self.huey.publish_batch([add.s(2, 2)], key='k')
        self.assertEqual(self.huey.pending_count(), 1)

    def test_content_shape_is_part_of_identity(self):
        add, total = self.declare()
        self.huey.publish_batch([group([add.s(1, 1)])], key='g')
        with self.assertRaises(BatchConflictError):
            self.huey.publish_batch([add.s(1, 1)], key='g')
        self.huey.publish_batch([group([add.s(1, 1), add.s(2, 2)])], key='g2')

    def test_schedule_and_context_fields_in_identity(self):
        add, _ = self.declare()
        self.huey.publish_batch([add.s(1, 1, priority=5)], key='p')
        with self.assertRaises(BatchConflictError):
            self.huey.publish_batch([add.s(1, 1, priority=6)], key='p')

        self.huey.publish_batch([add.s(1, 1, retries=2)], key='r')
        with self.assertRaises(BatchConflictError):
            self.huey.publish_batch([add.s(1, 1, retries=3)], key='r')

        # A relative delay retried later resolves to a different clock time,
        # but is still the same logical batch.
        first = self.huey.publish_batch([add.s(1, 1, delay=60)], key='d')
        second = self.huey.publish_batch([add.s(1, 1, delay=60)], key='d')
        self.assertEqual(first[0].id, second[0].id)

        # An absolute eta is exact content.
        eta = datetime.datetime(2030, 1, 1)
        self.huey.publish_batch([add.s(1, 1, eta=eta)], key='e')
        self.huey.publish_batch([add.s(1, 1, eta=eta)], key='e-again')
        with self.assertRaises(BatchConflictError):
            self.huey.publish_batch(
                [add.s(1, 1, eta=datetime.datetime(2031, 1, 1))], key='e')

    def test_explicit_id_is_part_of_identity(self):
        add, _ = self.declare()
        self.huey.publish_batch([add.s(1, 1, id='mine')], key='ids')
        with self.assertRaises(BatchConflictError):
            self.huey.publish_batch([add.s(1, 1, id='other')], key='ids')

    def test_set_and_dict_order_not_part_of_identity(self):
        add, _ = self.declare()
        first = self.huey.publish_batch(
            [add.s({'a', 'b', 'c'}, 1)], key='unordered')
        second = self.huey.publish_batch(
            [add.s({'c', 'b', 'a'}, 1)], key='unordered')
        self.assertEqual(first[0].id, second[0].id)

        # A dict argument built in a different order is the same content.
        self.huey.publish_batch([add.s({'x': 1, 'y': 2}, 1)], key='dict')
        self.huey.publish_batch([add.s({'y': 2, 'x': 1}, 1)], key='dict')
        self.assertEqual(self.huey.pending_count(), 2)

    def test_unknown_commit_result_is_recoverable(self):
        add, _ = self.declare()
        # Simulate "database committed but caller lost the response": the
        # retry builds a fresh set of tasks (new random ids) and asks the
        # same key. It must return the original handles, not re-publish.
        self.huey.publish_batch([add.s(1, 1)], key='settle')
        recovered = self.huey.get_batch('settle')
        self.assertIsNotNone(recovered)
        self.assertEqual(len(recovered), 1)
        again = self.huey.publish_batch([add.s(1, 1)], key='settle')
        self.assertEqual(recovered[0].id, again[0].id)
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertEqual(self.huey.get_batch('missing'), None)

    def test_unsupported_backend(self):
        from huey import MemoryHuey
        huey = MemoryHuey()

        @huey.task()
        def task_a(a):
            return a

        with self.assertRaises(BatchPublishError):
            huey.publish_batch([task_a.s(1)])
        with self.assertRaises(BatchPublishError):
            huey.get_batch('k')
        # Ordinary enqueue is unchanged.
        task_a(1)
        self.assertEqual(huey.pending_count(), 1)

    def test_immediate_mode_rejected(self):
        huey = SqliteHuey(filename=self.huey.storage.filename, utc=False,
                          immediate=True, immediate_use_memory=False)
        try:
            @huey.task()
            def task_a(a):
                return a

            with self.assertRaises(BatchPublishError):
                huey.publish_batch([task_a.s(1)])
            self.assertEqual(huey.pending_count(), 0)
            # Ordinary immediate enqueue keeps executing synchronously.
            self.assertEqual(task_a(2)(), 2)
        finally:
            huey.storage.close()

    def test_results_disabled_returns_none_members(self):
        huey = SqliteHuey(filename=os.path.join(TEST_TMP_DIR, 'batch-nr.db'),
                          utc=False, results=False)
        try:
            @huey.task()
            def task_a(a):
                return a

            batch = huey.publish_batch([task_a.s(1), task_a.s(2)], key='nr')
            self.assertEqual(batch.get(), [None, None])
            self.assertEqual(len(batch), 2)
            self.assertEqual(huey.pending_count(), 2)
            # The idempotent retry returns the same shapes.
            again = huey.publish_batch([task_a.s(1), task_a.s(2)], key='nr')
            self.assertEqual(again.get(), [None, None])
            self.assertEqual(huey.pending_count(), 2)
        finally:
            huey.storage.close()
            for suffix in ('', '-wal', '-shm'):
                path = huey.storage.filename + suffix
                if os.path.exists(path):
                    os.unlink(path)


class TestBatchSignals(BatchTestCase):
    def test_signals_emitted_after_commit(self):
        add, _ = self.declare()
        seen = []
        filename = self.huey.storage.filename

        @self.huey.signal()
        def on_enqueued(signal, task, *args, **kwargs):
            # The row must be visible to an independent connection: the
            # transaction has committed before the signal fires.
            other = sqlite3.connect(filename)
            try:
                count = other.execute('select count(*) from task').fetchone()
            finally:
                other.close()
            seen.append((signal, task.id, count[0]))

        self.huey.publish_batch([add.s(1, 1), add.s(2, 2)])
        self.assertEqual([s[0] for s in seen], ['enqueued', 'enqueued'])
        self.assertEqual([s[2] for s in seen], [2, 2])

    def test_signal_receiver_cannot_roll_back(self):
        add, _ = self.declare()
        calls = []

        @self.huey.signal()
        def on_enqueued(signal, task, *args, **kwargs):
            calls.append(task.id)
            raise ValueError('subscriber failure')

        result = self.huey.publish_batch([add.s(1, 1)], key='k')
        self.assertEqual(calls, [result[0].id])
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertIsNotNone(self.huey.get_batch('k'))

    def test_no_signal_on_idempotent_retry(self):
        add, _ = self.declare()
        calls = []

        @self.huey.signal()
        def on_enqueued(signal, task, *args, **kwargs):
            calls.append(task.id)

        self.huey.publish_batch([add.s(1, 1)], key='k')
        self.huey.publish_batch([add.s(1, 1)], key='k')
        self.assertEqual(len(calls), 1)

    def test_no_signal_on_failure(self):
        add, _ = self.declare()
        calls = []

        @self.huey.signal()
        def on_enqueued(signal, task, *args, **kwargs):
            calls.append(task.id)

        with self.assertRaises(BatchIdConflictError):
            self.huey.publish_batch([add.s(1, 1, id='x'),
                                     add.s(2, 2, id='x')])
        self.assertEqual(calls, [])


class TestBatchAcrossProcesses(BatchTestCase):
    """Two real interpreter processes sharing one database file."""

    def run_process(self, code, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get(
            'PYTHONPATH', '')
        return subprocess.run(
            [sys.executable, '-c', textwrap.dedent(code)] + list(args),
            env=env, capture_output=True, text=True, timeout=60)

    def test_committed_batch_recoverable_from_second_process(self):
        filename = self.huey.storage.filename
        publisher = """
            import sys
            from huey.tests.batch_procs import build

            huey, add, total = build(sys.argv[1])
            result = huey.publish_batch(
                [add.s(1, 2), add.s(3, 4)], key='month-end')
            print(result[0].id)
            print(result[1].id)
        """
        # Publisher commits, then "dies" before reporting success to the
        # caller: the caller simply never received stdout.
        proc = self.run_process(publisher, filename)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        published = proc.stdout.split()
        self.assertEqual(len(published), 2)

        reader = """
            import sys
            from huey.tests.batch_procs import build

            huey, add, total = build(sys.argv[1])
            recovered = huey.get_batch('month-end')
            retried = huey.publish_batch([add.s(1, 2), add.s(3, 4)],
                                         key='month-end')
            print(','.join(r.id for r in recovered))
            print(','.join(r.id for r in retried))
            print(huey.pending_count())
        """
        proc = self.run_process(reader, filename)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        recovered, retried, pending = proc.stdout.split()
        self.assertEqual(recovered.split(','), published)
        self.assertEqual(retried.split(','), published)
        self.assertEqual(int(pending), 2)  # Not published twice.

    def test_second_process_cannot_reuse_committed_ids(self):
        filename = self.huey.storage.filename
        publisher = """
            import sys
            from huey.tests.batch_procs import build

            huey, add, total = build(sys.argv[1])
            huey.publish_batch([add.s(1, 2, id='settle-line-1')])
            print('ok')
        """
        proc = self.run_process(publisher, filename)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        intruder = """
            import sys
            from huey.tests.batch_procs import build
            from huey.exceptions import BatchIdConflictError

            huey, add, total = build(sys.argv[1])
            try:
                huey.publish_batch([add.s(9, 9),
                                    add.s(1, 2, id='settle-line-1')])
            except BatchIdConflictError:
                print('rejected')
            print(huey.pending_count())
        """
        proc = self.run_process(intruder, filename)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ['rejected', '1'])

    def test_fingerprint_stable_across_processes(self):
        # Different hash seeds and dict insertion order must not change the
        # batch identity, or an idempotent retry from another process would
        # be rejected as a conflict.
        filename = self.huey.storage.filename
        code = """
            import sys
            from huey.tests.batch_procs import build

            huey, add, total = build(sys.argv[1])
            if sys.argv[2] == 'a':
                kwargs = {'x': 1, 'y': 2}
                payload = {'b', 'a', 'c'}
            else:
                kwargs = {'y': 2, 'x': 1}
                payload = {'c', 'a', 'b'}
            result = huey.publish_batch(
                [add.s(1, 1), total.s([1, 2, 3], **kwargs)], key='stable')
            print(result[0].id)
        """
        outputs = []
        for seed, variant in (('1', 'a'), ('2', 'b')):
            env = dict(os.environ)
            env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get(
                'PYTHONPATH', '')
            env['PYTHONHASHSEED'] = seed
            proc = subprocess.run(
                [sys.executable, '-c', textwrap.dedent(code), filename,
                 variant],
                env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs.append(proc.stdout.strip())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(self.huey.pending_count(), 2)

    def test_concurrent_same_key_same_content_single_publish(self):
        # Several processes race with identical content and key: all must
        # report the same handles, and exactly one batch is written.
        filename = self.huey.storage.filename
        self.huey.publish_batch([])  # Create the schema up front.
        code = """
            import sys
            from huey.tests.batch_procs import build

            huey, add, total = build(sys.argv[1])
            result = huey.publish_batch([add.s(1, 2), add.s(3, 4)],
                                        key='same')
            print(','.join(r.id for r in result))
            print(huey.pending_count())
        """
        env = dict(os.environ)
        env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
        procs = [subprocess.Popen(
                    [sys.executable, '-c', textwrap.dedent(code), filename],
                    env=env, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                 for _ in range(4)]
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
            outputs.append(tuple(out.split()))
        self.assertEqual(len(set(outputs)), 1)
        ids, pending = outputs[0]
        self.assertEqual(len(ids.split(',')), 2)
        self.assertEqual(int(pending), 2)

    def test_concurrent_same_key_single_winner(self):
        filename = self.huey.storage.filename
        code = """
            import sys
            from huey.tests.batch_procs import build
            from huey.exceptions import BatchConflictError

            huey, add, total = build(sys.argv[1])
            n = int(sys.argv[2])
            try:
                result = huey.publish_batch(
                    [add.s(n, n), add.s(n + 1, n + 1)], key='race')
                print('published')
            except BatchConflictError:
                print('conflict')
        """
        env = dict(os.environ)
        env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
        procs = [subprocess.Popen(
                    [sys.executable, '-c', textwrap.dedent(code), filename,
                     str(i)],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True)
                 for i in (1, 3)]
        outcomes = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
            outcomes.append(out.strip())
        # Distinct content with one key: exactly one batch may win, the
        # other must be told it conflicts -- never merged or duplicated.
        self.assertEqual(sorted(outcomes), ['conflict', 'published'])
        self.assertEqual(self.huey.pending_count(), 2)

    def test_consumer_process_sees_all_or_nothing(self):
        filename = self.huey.storage.filename
        publisher, add, total = build(filename)
        try:
            # A chord means the callback only fires if every member (and the
            # counter bookkeeping) committed together.
            publisher.publish_batch([
                chord([add.s(i, i) for i in range(4)], total)])
        finally:
            publisher.storage.close()

        consumer = """
            import sys
            from huey.tests.batch_procs import build

            huey, add, total = build(sys.argv[1])
            seen = []
            value = None
            for _ in range(10):
                task = huey.dequeue()
                if task is None:
                    break
                seen.append(task.id)
                value = huey.execute(task)
            print(value)
            print(len(seen))
            print(huey.pending_count())
        """
        proc = self.run_process(consumer, filename)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value, seen, pending = proc.stdout.split()
        self.assertEqual(int(value), 12)
        self.assertEqual(int(seen), 5)  # 4 members + chord callback.
        self.assertEqual(int(pending), 0)

    def test_two_consumer_processes_race_publisher(self):
        # Two real consumer processes poll while a third process publishes
        # batches. Every snapshot each consumer takes must contain whole
        # batches only.
        filename = self.huey.storage.filename
        # Create the schema up front so the consumers start clean.
        self.huey.publish_batch([])
        batch_size, n_batches = 4, 25

        consumer = """
            import json, sqlite3, sys, time
            from huey.tests.batch_procs import build

            huey, add, total = build(sys.argv[1])
            expected = int(sys.argv[2])
            conn = sqlite3.connect(sys.argv[1])
            deadline = time.time() + 10
            problems = []
            snapshots = 0
            while time.time() < deadline:
                rows = conn.execute('select data from task').fetchall()
                counts = {}
                for (data,) in rows:
                    task = huey.deserialize_task(data)
                    counts[task.args[0]] = counts.get(task.args[0], 0) + 1
                snapshots += 1
                for batch, count in counts.items():
                    if count != %(batch_size)d:
                        problems.append([batch, count])
                if sum(counts.values()) == expected:
                    break
                time.sleep(0.0005)
            conn.close()
            print(json.dumps({'problems': problems,
                              'snapshots': snapshots}))
        """ % dict(batch_size=batch_size)

        env = dict(os.environ)
        env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
        consumers = [subprocess.Popen(
                        [sys.executable, '-c', textwrap.dedent(consumer),
                         filename, str(n_batches * batch_size)],
                        env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True)
                     for _ in range(2)]
        time.sleep(0.2)
        # Publish from this process while the others are watching. Each
        # batch stamps its members with a distinct first argument.
        publisher, add, total = build(filename)
        try:
            for n in range(n_batches):
                publisher.publish_batch(
                    [add.s(n * 10, i) for i in range(batch_size)],
                    key='race-%s' % n)
                time.sleep(0.002)
        finally:
            publisher.storage.close()

        for proc in consumers:
            out, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, err)
            report = json.loads(out)
            self.assertEqual(report['problems'], [])
            self.assertGreater(report['snapshots'], 0)

        self.assertEqual(self.huey.pending_count(), n_batches * batch_size)


if __name__ == '__main__':
    unittest.main()
