"""
Tests for capability-based task routing.

Covers, for both the in-memory storage and the SQLite storage:

* Consumers only dequeue tasks whose requirements they satisfy; unlabeled
  tasks remain eligible for everyone.
* Eligible tasks are still taken in priority then FIFO order, while blocked
  tasks stay in the queue and do not block other backlog.
* Requirements survive the delayed/schedule path and failure retries.
* Concurrent dequeue of a shared queue never hands the same task out twice.
* on_complete chains resolve requirements from explicit instance values,
  class defaults, or predecessor inheritance -- and an explicit successor
  value is never overwritten by a predecessor.
* Legacy unlabeled messages/rows (old schema, old Message tuple) still run.
"""
import datetime
import os
import sqlite3
import threading
import unittest

from huey import MemoryHuey
from huey import SqliteHuey
from huey.capabilities import can_handle
from huey.capabilities import decode_requirements
from huey.capabilities import encode_requirements
from huey.capabilities import normalize_capabilities
from huey.capabilities import normalize_requirements
from huey.capabilities import resolve_chain_requirements
from huey.storage import SqliteStorage
from huey.tests import TEST_TMP_DIR


class CapabilityHelperTests(unittest.TestCase):
    def test_normalize_requirements(self):
        self.assertIsNone(normalize_requirements(None))
        self.assertIsNone(normalize_requirements(()))
        self.assertEqual(normalize_requirements('pdf'), ('pdf',))
        self.assertEqual(normalize_requirements(['fonts', 'pdf', 'pdf']),
                         ('fonts', 'pdf'))

    def test_normalize_capabilities(self):
        self.assertEqual(normalize_capabilities(None), frozenset())
        self.assertEqual(normalize_capabilities('pdf'), frozenset(['pdf']))
        self.assertEqual(normalize_capabilities(['a', 'b']),
                         frozenset(['a', 'b']))

    def test_can_handle(self):
        self.assertTrue(can_handle(frozenset(), None))
        self.assertTrue(can_handle(frozenset(['pdf']), ('pdf',)))
        self.assertTrue(can_handle(frozenset(['pdf', 'fonts']),
                                   ('fonts', 'pdf')))
        self.assertFalse(can_handle(frozenset(), ('pdf',)))
        self.assertFalse(can_handle(frozenset(['pdf']),
                                    ('fonts', 'pdf')))

    def test_encode_decode_roundtrip(self):
        self.assertIsNone(encode_requirements(None))
        self.assertIsNone(decode_requirements(None))
        encoded = encode_requirements(('fonts', 'pdf'))
        self.assertEqual(decode_requirements(encoded), ('fonts', 'pdf'))
        self.assertEqual(decode_requirements(encoded.encode()),
                         ('fonts', 'pdf'))


class _RoutingTestMixin(object):
    def get_huey(self, capabilities=None):
        raise NotImplementedError

    def test_basic_routing_and_backlog_independence(self):
        huey = self.get_huey()

        @huey.task()
        def csv_report(name):
            return 'csv:%s' % name

        @huey.task(requirements='pdf')
        def pdf_report(name):
            return 'pdf:%s' % name

        csv_report('a')
        pdf_report('b')
        csv_report('c')

        # A plain consumer drains both CSV tasks but cannot touch the PDF.
        first = huey.dequeue(())
        self.assertEqual(first.name, 'csv_report')
        self.assertIsNone(first.requirements)
        second = huey.dequeue(())
        self.assertEqual(second.name, 'csv_report')
        self.assertIsNone(huey.dequeue(()))
        self.assertEqual(huey.pending_count(), 1)

        # The blocked task is still first in line for a capable consumer.
        routed = huey.dequeue(('pdf',))
        self.assertEqual(routed.name, 'pdf_report')
        self.assertEqual(routed.requirements, ('pdf',))
        self.assertEqual(huey.pending_count(), 0)

    def test_priority_and_fifo_within_eligible_subset(self):
        huey = self.get_huey()

        @huey.task()
        def generic(n):
            return n

        @huey.task(requirements='pdf')
        def pdf_task(n):
            return n

        generic('fifo-1')                       # priority 0
        pdf_task('pdf-low')                    # priority 0
        generic('fifo-2', priority=5)          # priority 5
        pdf_task('pdf-high', priority=10)      # priority 10
        generic('fifo-3')                      # priority 0

        # Plain consumer: high-priority generic first, then FIFO generics.
        self.assertEqual(huey.dequeue(()).args, ('fifo-2',))
        self.assertEqual(huey.dequeue(()).args, ('fifo-1',))
        self.assertEqual(huey.dequeue(()).args, ('fifo-3',))
        self.assertIsNone(huey.dequeue(()))

        # PDF consumer sees its own subset ordered by priority, then FIFO.
        self.assertEqual(huey.dequeue(('pdf',)).args, ('pdf-high',))
        self.assertEqual(huey.dequeue(('pdf',)).args, ('pdf-low',))
        self.assertIsNone(huey.dequeue(('pdf',)))

    def test_multi_requirement_subset_match(self):
        huey = self.get_huey()

        @huey.task(requirements=('pdf', 'fonts'))
        def rich_pdf(name):
            return name

        rich_pdf('x')
        self.assertIsNone(huey.dequeue(('pdf',)))
        self.assertIsNone(huey.dequeue(('fonts',)))
        task = huey.dequeue(('fonts', 'pdf', 'extra'))
        self.assertIsNotNone(task)
        self.assertEqual(task.requirements, ('fonts', 'pdf'))

    def test_requirements_survive_schedule_and_retry(self):
        huey = self.get_huey()

        state = {'runs': 0}

        @huey.task(retries=2, requirements='pdf')
        def flaky_pdf():
            state['runs'] += 1
            if state['runs'] < 2:
                raise ValueError('transient failure')
            return 'ok'

        # Delayed enqueue follows the real Huey path: schedule() puts the task
        # in the queue with an eta; execute() on a not-yet-ready task moves it
        # into the schedule table; the scheduler later re-enqueues it. At
        # every hop the requirements must be retained.
        eta = huey._get_timestamp() + datetime.timedelta(seconds=60)
        flaky_pdf.schedule((), eta=eta)
        self.assertEqual(huey.pending_count(), 1)
        self.assertEqual(huey.scheduled_count(), 0)

        # A plain consumer must not even dequeue the not-ready PDF task.
        self.assertIsNone(huey.dequeue(()))
        delayed = huey.dequeue(('pdf',))
        self.assertEqual(delayed.requirements, ('pdf',))

        # Executing before the eta moves the task into the schedule table.
        self.assertFalse(huey.ready_to_run(delayed))
        huey.execute(delayed)
        self.assertEqual(huey.pending_count(), 0)
        self.assertEqual(huey.scheduled_count(), 1)
        scheduled = huey.scheduled()
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0].requirements, ('pdf',))

        # Scheduler read-and-requeue at maturity keeps the requirements. The
        # scheduler re-enqueues due tasks with their eta cleared, mirroring
        # Huey's Scheduler.loop() -> enqueue() flow in a live consumer.
        due = huey.read_schedule(eta)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].requirements, ('pdf',))
        for task in due:
            task.eta = None
            huey.enqueue(task)
        self.assertEqual(huey.pending_count(), 1)
        self.assertIsNone(huey.dequeue(()))
        task = huey.dequeue(('pdf',))
        self.assertEqual(task.requirements, ('pdf',))

        # Failing run requeues with retries; requirements are retained.
        huey.execute(task)
        self.assertEqual(state['runs'], 1)
        self.assertEqual(huey.pending_count(), 1)
        # Plain consumer still cannot take the retry.
        self.assertIsNone(huey.dequeue(()))
        retried = huey.dequeue(('pdf',))
        self.assertIsNotNone(retried)
        self.assertEqual(retried.requirements, ('pdf',))
        self.assertEqual(retried.retries, 1)
        result = huey.execute(retried)
        self.assertEqual(result, 'ok')
        self.assertEqual(huey.pending_count(), 0)

    def test_chain_requirement_sources(self):
        huey = self.get_huey()

        @huey.task()
        def plain(n):
            return n

        @huey.task(requirements='csv')
        def class_default(n):
            return n

        @huey.task(requirements='pdf')
        def pdf_step(n):
            return n

        @huey.task()
        def finish(n):
            return n

        # Chain: explicit pdf on the root, an unlabeled successor that must
        # inherit, and a successor with its own explicit class default.
        task = pdf_step.s(1)
        task.then(plain, 2)
        task.then(class_default, 3)
        resolve_chain_requirements(task)

        root, middle, tail = task, task.on_complete, task.on_complete.on_complete
        self.assertEqual(root.requirements, ('pdf',))
        # No explicit value and no class default -> inherit predecessor.
        self.assertEqual(middle.requirements, ('pdf',))
        # Explicit class default wins over the inherited value.
        self.assertEqual(tail.requirements, ('csv',))

        # Explicit per-instance requirement on the middle node beats both the
        # predecessor and the class default.
        task2 = pdf_step.s(1)
        task2.then(class_default, 2, requirements='fonts')
        resolve_chain_requirements(task2)
        self.assertEqual(task2.on_complete.requirements, ('fonts',))

        # An entirely unlabeled chain stays unlabeled (anyone may run it).
        task3 = plain.s(1)
        task3.then(plain, 2)
        resolve_chain_requirements(task3)
        self.assertIsNone(task3.requirements)
        self.assertIsNone(task3.on_complete.requirements)

    def test_on_error_chain_requirement_sources(self):
        huey = self.get_huey()

        @huey.task()
        def plain(n):
            return n

        @huey.task(requirements='csv')
        def csv_default(n):
            return n

        @huey.task(requirements='pdf')
        def may_fail(n):
            return n

        # Unlabeled error handler inherits the root task's requirements.
        task = may_fail.s(1)
        task.error(plain, 'err')
        resolve_chain_requirements(task)
        self.assertEqual(task.requirements, ('pdf',))
        self.assertEqual(task.on_error.requirements, ('pdf',))

        # An error handler with its own class default keeps it.
        task2 = may_fail.s(1)
        task2.error(csv_default, 'err')
        resolve_chain_requirements(task2)
        self.assertEqual(task2.on_error.requirements, ('csv',))

        # An explicit per-instance value on the handler beats everything.
        task3 = may_fail.s(1)
        task3.error(csv_default, 'err', requirements='fonts')
        resolve_chain_requirements(task3)
        self.assertEqual(task3.on_error.requirements, ('fonts',))

    def test_chain_enqueue_and_on_complete_carries_requirements(self):
        huey = self.get_huey()
        results = []

        @huey.task(requirements='pdf')
        def step_one(n):
            results.append(('one', n))
            return n + 1

        @huey.task()
        def step_two(n):
            results.append(('two', n))
            return n + 1

        task = step_one.s(1)
        task.then(step_two)
        huey.enqueue(task)

        # Plain consumer cannot start the chain; a PDF consumer can.
        self.assertIsNone(huey.dequeue(()))
        first = huey.dequeue(('pdf',))
        self.assertEqual(first.requirements, ('pdf',))
        huey.execute(first)

        # The successor inherited the root's requirements.
        self.assertEqual(huey.pending_count(), 1)
        self.assertIsNone(huey.dequeue(()))
        second = huey.dequeue(('pdf',))
        self.assertEqual(second.requirements, ('pdf',))
        huey.execute(second)
        self.assertEqual(results, [('one', 1), ('two', 2)])

    def test_chord_callback_keeps_its_requirements(self):
        from huey import chord as chord_cls

        huey = self.get_huey()

        @huey.task()
        def head(n):
            return n

        @huey.task(requirements='pdf')
        def callback(results):
            return results

        huey.enqueue(chord_cls((head.s(1), head.s(2)), callback.s()))
        for _ in range(2):
            huey.execute(huey.dequeue(()))

        # The callback is enqueued via _check_chord, not Huey.enqueue
        # directly, but must still carry its class-default requirement.
        self.assertEqual(huey.pending_count(), 1)
        self.assertIsNone(huey.dequeue(()))
        cb = huey.dequeue(('pdf',))
        self.assertIsNotNone(cb)
        self.assertEqual(cb.requirements, ('pdf',))

    def test_on_error_handler_inherits_and_overrides(self):
        huey = self.get_huey()

        @huey.task(requirements='pdf')
        def risky():
            raise ValueError('boom')

        @huey.task()
        def plain_handler(exc):
            return 'handled'

        task = risky.s()
        task.error(plain_handler)
        huey.enqueue(task)

        root = huey.dequeue(('pdf',))
        self.assertEqual(root.on_error.requirements, ('pdf',))
        huey.execute(root)  # Fails; the error handler is enqueued.
        self.assertIsNone(huey.dequeue(()))
        handler = huey.dequeue(('pdf',))
        self.assertIsNotNone(handler)
        self.assertEqual(handler.requirements, ('pdf',))

    def test_legacy_unlabeled_message_runs(self):
        # A message serialized without a requirements field must deserialize
        # to an unlabeled task and remain runnable by every consumer.
        huey = self.get_huey()

        @huey.task()
        def old_task(n):
            return n * 2

        from huey.registry import Message
        message = Message(
            'legacy-id',
            huey._registry.task_to_string(old_task.task_class),
            None, 0, 0, None, (21,), {}, None, None, None, None, None,
            None, None)  # 15 fields, no requirements (old format).
        raw = huey.serializer.serialize(message)
        huey.storage.enqueue(raw, None, None)

        task = huey.dequeue(())
        self.assertIsNotNone(task)
        self.assertEqual(task.id, 'legacy-id')
        self.assertIsNone(task.requirements)
        self.assertEqual(huey.execute(task), 42)


class MemoryRoutingTests(_RoutingTestMixin, unittest.TestCase):
    def get_huey(self, capabilities=None):
        return MemoryHuey(utc=False, capabilities=capabilities)

    def test_concurrent_dequeue_no_duplicates(self):
        huey = self.get_huey()

        @huey.task(requirements='pdf')
        def pdf_task(n):
            return n

        total = 200
        for i in range(total):
            pdf_task(i)

        seen = []
        lock = threading.Lock()

        def worker():
            while True:
                task = huey.dequeue(('pdf',))
                if task is None:
                    return
                with lock:
                    seen.append(task.id)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(huey.pending_count(), 0)
        self.assertEqual(len(seen), total)
        self.assertEqual(len(set(seen)), total)


class SqliteRoutingTests(_RoutingTestMixin, unittest.TestCase):
    def setUp(self):
        self.filename = os.path.join(TEST_TMP_DIR,
                                     'capability-routing-%d.db' % id(self))
        if os.path.exists(self.filename):
            os.unlink(self.filename)

    def tearDown(self):
        if os.path.exists(self.filename):
            os.unlink(self.filename)
        for suffix in ('-wal', '-shm'):
            path = self.filename + suffix
            if os.path.exists(path):
                os.unlink(path)

    def get_huey(self, capabilities=None):
        return SqliteHuey('routing', filename=self.filename, utc=False,
                          capabilities=capabilities)

    def test_legacy_schema_migration(self):
        # Build a database with the pre-feature task table.
        if os.path.exists(self.filename):
            os.unlink(self.filename)
        conn = sqlite3.connect(self.filename)
        conn.execute('create table task (id integer not null primary key, '
                     'queue text not null, data blob not null, '
                     'priority real not null default 0.0)')
        conn.execute('create table kv (queue text not null, key text not null, '
                     'value blob not null, primary key(queue, key))')
        conn.execute('create table schedule (id integer not null primary key, '
                     'queue text not null, data blob not null, '
                     'timestamp real not null)')
        conn.execute('create index schedule_queue_timestamp on schedule '
                     '(queue, timestamp)')
        conn.execute('create index task_queue_priority_id on task '
                     '(queue, priority desc, id)')
        conn.execute('create table counter (queue text not null, key text '
                     'not null, value integer not null default 0, '
                     'primary key(queue, key))')
        conn.commit()
        conn.close()

        huey = self.get_huey()  # Opening triggers the migration.
        columns = [row[1] for row in
                   huey.storage.sql('pragma table_info(task)', results=True)]
        self.assertIn('requirements', columns)

        @huey.task()
        def csv_report(name):
            return name

        # A raw unlabeled row (NULL requirements) plus a new labeled row.
        legacy_blob = huey.serialize_task(csv_report.s('old'))
        huey.storage.sql(
            'insert into task (queue, data, priority, requirements) '
            'values (?, ?, ?, ?)',
            (huey.name, legacy_blob, 0, None), commit=True)
        csv_report('new')

        self.assertEqual(huey.pending_count(), 2)
        for _ in range(2):
            task = huey.dequeue(())
            self.assertIsNotNone(task)
            self.assertIsNone(task.requirements)
        self.assertIsNone(huey.dequeue(()))
        huey.storage.close()

    def test_concurrent_dequeue_no_duplicates(self):
        # Exercise the storage layer directly with independent connections,
        # as separate consumer processes would have: the same delivery must
        # only be removed from the queue by one of them.
        huey = self.get_huey()
        total = 200
        for i in range(total):
            huey.storage.enqueue(b'payload-%d' % i, None, ('pdf',))

        seen = []
        lock = threading.Lock()

        def worker():
            local = SqliteStorage('routing', filename=self.filename)
            try:
                while True:
                    data = local.dequeue(('pdf',))
                    if data is None:
                        return
                    with lock:
                        seen.append(bytes(data))
            finally:
                local.close()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(huey.pending_count(), 0)
        self.assertEqual(len(seen), total)
        self.assertEqual(len(set(seen)), total)
        huey.storage.close()


if __name__ == '__main__':
    unittest.main()
