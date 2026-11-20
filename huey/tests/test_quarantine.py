import datetime
import io
import logging
import os
import pickle
import sqlite3
import threading
import unittest
import uuid

from huey import SqliteHuey
from huey.exceptions import ConfigurationError
from huey.quarantine import main as quarantine_main
from huey.quarantine import ERROR_INVALID
from huey.quarantine import ERROR_UNREGISTERED
from huey.quarantine import scan_message_metadata
from huey.registry import Message
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase


class QuarantineTestCase(BaseTestCase):
    def get_huey(self):
        self.db_file = os.path.join(
            TEST_TMP_DIR, 'quarantine-%s.db' % uuid.uuid4().hex)
        return SqliteHuey(filename=self.db_file, quarantine=True, utc=False)

    def tearDown(self):
        super(QuarantineTestCase, self).tearDown()
        self.huey.storage.close()
        for suffix in ('', '-wal', '-shm'):
            path = self.db_file + suffix
            if os.path.exists(path):
                os.unlink(path)

    def bad_message(self, name='app.missing_task', id=None):
        return pickle.dumps(Message(
            id or str(uuid.uuid4()), name, None, None, None, None,
            ('secret-arg',), {'token': 'hunter2'}, None, None, None, None,
            None, None, None))


class TestQuarantineBasics(QuarantineTestCase):
    def setUp(self):
        super(TestQuarantineBasics, self).setUp()

        @self.huey.task()
        def add(a, b):
            return a + b
        self.add = add

    def test_default_disabled(self):
        huey = SqliteHuey(filename=os.path.join(TEST_TMP_DIR, 'qp-default.db'))
        try:
            self.assertFalse(huey.quarantine_enabled)
            huey.storage.enqueue(b'not-a-message')
            self.assertRaises(Exception, huey.dequeue)
            # The unreadable message was consumed (logged and discarded), the
            # original upstream behavior.
            self.assertEqual(huey.pending_count(), 0)
        finally:
            huey.storage.close()
            os.unlink(os.path.join(TEST_TMP_DIR, 'qp-default.db'))

    def test_unsupported_storage_raises(self):
        from huey import MemoryHuey
        self.assertRaises(ConfigurationError, MemoryHuey, quarantine=True)

    def test_quarantine_unknown_flag_not_forwarded(self):
        self.assertTrue(self.huey.quarantine_enabled)

    def test_bad_then_good_message(self):
        self.huey.storage.enqueue(b'broken-1')
        r1 = self.add(1, 2)
        self.huey.storage.enqueue(self.bad_message())
        r2 = self.add(3, 4)

        self.assertEqual(len(self.huey), 4)
        self.assertEqual(self.execute_next(), 3)
        self.assertEqual(self.execute_next(), 7)
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(self.huey.quarantine_count(), 2)

        records = self.huey.quarantined()
        self.assertEqual([r.source for r in records], ['queue', 'queue'])
        self.assertEqual(records[0].error_class, ERROR_UNREGISTERED)
        self.assertEqual(records[0].task_name, 'app.missing_task')
        self.assertEqual(records[0].task_id is not None, True)
        self.assertEqual(records[1].error_class, 'deserialize-error')
        self.assertEqual(
            [r.id for r in self.huey.quarantined()],
            sorted((r.id for r in records), reverse=True))

    def test_record_preserves_raw_bytes(self):
        payload = b'\x80\x04 not a real pickle \x00\xff'
        self.huey.storage.enqueue(payload)
        self.assertTrue(self.huey.dequeue() is None)
        record, = self.huey.quarantined()
        full = self.huey.get_quarantined(record.id)
        self.assertEqual(full.data, payload)
        self.assertEqual(full.size, len(payload))
        self.assertEqual(full.queue, 'huey')

    def test_original_order_and_priority_preserved(self):
        msg = Message('msg-id-1', 'app.missing_task', None, None, None, 5.0,
                      (), {}, None, None, None, None, None, None, None)
        self.huey.storage.enqueue(pickle.dumps(msg))
        self.huey.dequeue()
        record, = self.huey.quarantined()
        self.assertEqual(record.task_id, 'msg-id-1')
        self.assertEqual(record.priority, 5.0)

    def test_original_eta_preserved(self):
        eta = datetime.datetime(2030, 1, 2, 3, 4, 5)
        msg = Message('scheduled-id', 'app.missing_task', eta, None, None,
                      None, (), {}, None, None, None, None, None, None, None)
        self.huey.storage.enqueue(pickle.dumps(msg))
        self.huey.dequeue()
        record, = self.huey.quarantined()
        self.assertEqual(record.eta, '2030-01-02 03:04:05')

    def test_invalid_structure_quarantined(self):
        # A valid pickle that is not a Message at all.
        self.huey.storage.enqueue(pickle.dumps({'not': 'a message'}))
        self.huey.storage.enqueue(self.huey.serialize_task(self.add.s(2, 2)))
        self.assertEqual(self.execute_next(), 4)
        record, = self.huey.quarantined()
        self.assertEqual(record.error_class, ERROR_INVALID)
        self.assertEqual(self.huey.pending_count(), 0)

    def test_invalid_message_fields_quarantined(self):
        msg = Message('id', 'app.missing_task', 'not-a-datetime', None, None,
                      None, (), {}, None, None, None, None, None, None, None)
        self.huey.storage.enqueue(pickle.dumps(msg))
        self.assertIsNone(self.huey.dequeue())
        record, = self.huey.quarantined()
        self.assertEqual(record.error_class, ERROR_INVALID)
        self.assertEqual(record.task_name, 'app.missing_task')
        self.assertEqual(record.eta, None)

    def test_schedule_batch_with_bad_message(self):
        now = datetime.datetime.now()
        delta = datetime.timedelta
        good1 = self.add.s(1, 2)
        good2 = self.add.s(3, 4)
        self.huey.storage.add_to_schedule(b'bad-0', now - delta(seconds=10))
        self.huey.storage.add_to_schedule(self.huey.serialize_task(good1),
                                          now - delta(seconds=9))
        self.huey.storage.add_to_schedule(self.bad_message(),
                                          now - delta(seconds=8))
        self.huey.storage.add_to_schedule(self.huey.serialize_task(good2),
                                          now - delta(seconds=7))
        self.huey.storage.add_to_schedule(
            self.huey.serialize_task(self.add.s(5, 6)),
            now + delta(seconds=3600))

        self.assertEqual(self.huey.scheduled_count(), 5)
        tasks = self.huey.read_schedule(now)
        self.assertEqual([t.id for t in tasks], [good1.id, good2.id])
        self.assertEqual([self.huey.execute(t) for t in tasks], [3, 7])
        self.assertEqual(self.huey.quarantine_count(), 2)
        self.assertEqual([r.source for r in self.huey.quarantined()],
                         ['schedule', 'schedule'])
        self.assertEqual(self.huey.scheduled_count(), 1)

    def test_mixed_batch_schedule_no_good_lost(self):
        now = datetime.datetime.now()
        for i in range(10):
            if i % 2:
                self.huey.storage.add_to_schedule(b'bad-%d' % i,
                                                  now - datetime.timedelta(
                                                      seconds=100 - i))
            else:
                self.huey.storage.add_to_schedule(
                    self.huey.serialize_task(self.add.s(i, i)),
                    now - datetime.timedelta(seconds=100 - i))
        tasks = self.huey.read_schedule(now)
        self.assertEqual(len(tasks), 5)
        self.assertEqual([self.huey.execute(t) for t in tasks],
                         [0, 4, 8, 12, 16])
        self.assertEqual(self.huey.quarantine_count(), 5)
        self.assertEqual(self.huey.scheduled_count(), 0)

    def test_multi_consumer_single_record(self):
        ntasks = 40
        for i in range(ntasks):
            self.huey.storage.enqueue(('garbage-%d' % i).encode())
            self.huey.storage.enqueue(self.huey.serialize_task(self.add.s(i, 0)))

        results = []
        lock = threading.Lock()

        def run():
            while True:
                task = self.huey.dequeue()
                if task is None:
                    return
                value = self.huey.execute(task)
                with lock:
                    results.append(value)

        threads = [threading.Thread(target=run) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20.)
        self.assertEqual(sorted(results), list(range(ntasks)))
        self.assertEqual(self.huey.quarantine_count(), ntasks)
        # Every record corresponds to a distinct original message.
        sizes = sorted(r.size for r in self.huey.quarantined())
        expected = sorted(len(('garbage-%d' % i).encode())
                          for i in range(ntasks))
        self.assertEqual(sizes, expected)
        self.assertEqual(self.huey.pending_count(), 0)

    def test_peek_does_not_consume_good_message(self):
        self.huey.storage.enqueue(b'bad')
        r = self.add(1, 2)
        # After quarantining the bad message, the good message was peeked and
        # claimed exactly once.
        self.assertEqual(self.execute_next(), 3)
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(self.huey.quarantine_count(), 1)

    def test_nested_unregistered_callback_quarantined(self):
        def child(value):
            return value
        child_task = self.huey.task(name='child_task')(child)
        parent = self.add.s(1, 2).then(child_task.s())
        data = self.huey.serialize_task(parent)
        child_task.unregister()
        self.huey.storage.enqueue(data)
        self.assertIsNone(self.huey.dequeue())
        record, = self.huey.quarantined()
        self.assertEqual(record.error_class, ERROR_UNREGISTERED)
        self.assertIn('child_task', record.error)

    def test_pending_inspection_does_not_quarantine(self):
        self.huey.storage.enqueue(b'bad')
        good = self.add.s(5, 6)
        self.huey.storage.enqueue(self.huey.serialize_task(good))
        # pending() is a non-destructive read; it skips the bad message but
        # must not remove anything from the queue or quarantine it.
        pending = self.huey.pending()
        self.assertEqual([t.id for t in pending], [good.id])
        self.assertEqual(self.huey.pending_count(), 2)
        self.assertEqual(self.huey.quarantine_count(), 0)
        self.assertEqual(self.execute_next(), 11)
        self.assertEqual(self.huey.quarantine_count(), 1)


class TestQuarantineReplay(QuarantineTestCase):
    def setUp(self):
        super(TestQuarantineReplay, self).setUp()
        self.state = []

        def work(value):
            self.state.append(value)
            return value
        self.work = self.huey.task(name='work_task')(work)

    def test_replay_unregistered_task(self):
        data = self.huey.serialize_task(self.work.s('payload'))
        self.work.unregister()
        self.huey.storage.enqueue(data)
        self.assertIsNone(self.huey.dequeue())
        record, = self.huey.quarantined()
        self.assertEqual(record.error_class, ERROR_UNREGISTERED)

        # Replay still fails and does not change the record or create a loop.
        result = self.huey.replay_quarantined(record.id)
        self.assertFalse(result)
        self.assertEqual(self.huey.quarantine_count(), 1)
        self.assertEqual(self.huey.pending_count(), 0)
        # Replaying again is also a failed no-op.
        self.assertFalse(self.huey.replay_quarantined(record.id))
        self.assertEqual(self.huey.quarantine_count(), 1)

        # Re-register the task under the original name and replay successfully.
        re_registered = self.huey.task(name='work_task')(self.work.func)
        result = self.huey.replay_quarantined(record.id)
        self.assertTrue(result)
        self.assertEqual(result.destination, 'queue')
        self.assertEqual(self.huey.quarantine_count(), 0)
        self.assertEqual(self.huey.pending_count(), 1)
        task = self.huey.dequeue()
        self.assertEqual(self.huey.execute(task), 'payload')
        self.assertEqual(self.state, ['payload'])

    def test_replay_preserves_id_and_priority(self):
        task_wrapper = self.huey.task(name='missing_task')(lambda x: x)
        task_str = self.huey._registry.task_to_string(task_wrapper.task_class)
        msg = Message('stable-task-id', task_str, None, None, None,
                      7.0, ('x',), {}, None, None, None, None, None, None,
                      None)
        task_wrapper.unregister()
        self.huey.storage.enqueue(pickle.dumps(msg))
        self.huey.dequeue()
        record, = self.huey.quarantined()

        self.huey.task(name='missing_task')(lambda x: x)
        result = self.huey.replay_quarantined(record.id)
        self.assertTrue(result)
        pending_task = self.huey.pending()[0]
        self.assertEqual(pending_task.id, 'stable-task-id')
        self.assertEqual(pending_task.priority, 7.0)
        self.assertEqual(pending_task.args, ('x',))

    def test_replay_future_eta_goes_to_schedule(self):
        task_wrapper = self.huey.task(name='eta_task')(lambda: None)
        task_str = self.huey._registry.task_to_string(task_wrapper.task_class)
        task_wrapper.unregister()

        eta = datetime.datetime.now() + datetime.timedelta(hours=1)
        msg = Message('eta-task', task_str, eta, None, None, None,
                      (), {}, None, None, None, None, None, None, None)
        self.huey.storage.enqueue(pickle.dumps(msg))
        self.huey.dequeue()
        record, = self.huey.quarantined()
        self.huey.task(name='eta_task')(lambda: None)

        result = self.huey.replay_quarantined(record.id)
        self.assertTrue(result)
        self.assertEqual(result.destination, 'schedule')
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(self.huey.scheduled_count(), 1)
        scheduled, = self.huey.scheduled()
        self.assertEqual(scheduled.id, 'eta-task')
        self.assertEqual(scheduled.eta, eta)

    def test_replay_unparseable_stays_quarantined(self):
        self.huey.storage.enqueue(b'unrecoverable-bytes')
        self.assertIsNone(self.huey.dequeue())
        record, = self.huey.quarantined()
        for _ in range(3):
            result = self.huey.replay_quarantined(record.id)
            self.assertFalse(result)
            self.assertIn('deserialize-error', result.error)
            self.assertEqual(self.huey.quarantine_count(), 1)
            self.assertEqual(self.huey.pending_count(), 0)
            self.assertEqual(self.huey.scheduled_count(), 0)

    def test_discard(self):
        self.huey.storage.enqueue(b'bad')
        self.huey.dequeue()
        record, = self.huey.quarantined()
        self.assertTrue(self.huey.discard_quarantined(record.id))
        self.assertEqual(self.huey.quarantine_count(), 0)
        self.assertFalse(self.huey.discard_quarantined(record.id))
        self.assertIsNone(self.huey.get_quarantined(record.id))

    def test_inspection_does_not_pickle(self):
        # Crafted payload with a __reduce__ that would run code if unpickled.
        state = []

        class Exploit(object):
            def __reduce__(self):
                state.append('executed')
                return (len, ('boom',))

        self.huey.storage.enqueue(pickle.dumps(Exploit()))
        state.clear()  # __reduce__ runs when the payload is created.
        self.assertIsNone(self.huey.dequeue())
        record, = self.huey.quarantined()
        shown = self.huey.get_quarantined(record.id)
        self.assertIn(b'builtins', shown.data)
        self.assertEqual(state, [])  # Never unpickled.
        # Metadata scan likewise executes nothing.
        self.assertEqual(scan_message_metadata(shown.data), {})

    def test_metadata_scan_does_not_unpickle(self):
        state = []

        class Exploit(object):
            def __reduce__(self):
                state.append('executed')
                return (len, ('boom',))

        data = pickle.dumps(Message(
            'x', 'app.task', None, None, None, None, (Exploit(),), {},
            None, None, None, None, None, None, None))
        state.clear()  # __reduce__ runs when the payload is created.
        metadata = scan_message_metadata(data)
        self.assertEqual(metadata['task_id'], 'x')
        self.assertEqual(state, [])

    def test_signed_corrupt_message_metadata_recovered(self):
        from huey.serializer import SignedSerializer
        filename = os.path.join(TEST_TMP_DIR, 'qp-signed.db')
        huey = SqliteHuey(filename=filename, quarantine=True,
                          serializer=SignedSerializer(secret='s3cret'))
        try:
            msg = Message(
                'recover-id', 'app.reg_task',
                datetime.datetime(2030, 5, 6, 7, 8, 9), None, None, 4,
                ('secret-arg',), {}, None, None, None, None, None, None, None)
            raw = huey.serializer.serialize(msg)
            huey.storage.enqueue(raw[:-6] + b'zzzzzz')
            self.assertIsNone(huey.dequeue())
            record, = huey.quarantined()
            self.assertEqual(record.error_class, 'signature-invalid')
            self.assertEqual(record.task_id, 'recover-id')
            self.assertEqual(record.task_name, 'app.reg_task')
            self.assertEqual(record.eta, '2030-05-06 07:08:09')
            self.assertEqual(record.priority, 4.0)
            self.assertNotIn('s3cret', record.error)
            self.assertNotIn('secret-arg', record.error)
        finally:
            huey.storage.close()
            for suffix in ('', '-wal', '-shm'):
                if os.path.exists(filename + suffix):
                    os.unlink(filename + suffix)

    def test_cli_output(self):
        self.huey.storage.enqueue(b'bad-cli')
        self.huey.dequeue()
        record, = self.huey.quarantined()

        out = io.StringIO()
        self.assertEqual(quarantine_main(['list'], huey=self.huey,
                                         stream=out), 0)
        text = out.getvalue()
        self.assertIn('id=%s' % record.id, text)
        self.assertIn('class=deserialize-error', text)
        # Raw bytes and any argument material never appear.
        self.assertNotIn('bad-cli', text)

        out = io.StringIO()
        self.assertEqual(quarantine_main(['show', str(record.id)],
                                         huey=self.huey, stream=out), 0)
        self.assertIn('size=7', out.getvalue())

        out = io.StringIO()
        self.assertEqual(quarantine_main(['replay', str(record.id)],
                                         huey=self.huey, stream=out), 1)
        self.assertIn('Replay failed', out.getvalue())

        out = io.StringIO()
        self.assertEqual(quarantine_main(['discard', str(record.id)],
                                         huey=self.huey, stream=out), 0)
        self.assertIn('Discarded', out.getvalue())
        self.assertEqual(self.huey.quarantine_count(), 0)

    def test_cli_requires_enabled_quarantine(self):
        other = SqliteHuey(filename=os.path.join(TEST_TMP_DIR,
                                                'qp-cli-off.db'))
        try:
            out = io.StringIO()
            self.assertEqual(quarantine_main(['list'], huey=other,
                                             stream=out), 2)
            self.assertIn('not enabled', out.getvalue())
        finally:
            other.storage.close()
            os.unlink(os.path.join(TEST_TMP_DIR, 'qp-cli-off.db'))


class TestQuarantineFailure(QuarantineTestCase):
    def setUp(self):
        super(TestQuarantineFailure, self).setUp()

        @self.huey.task()
        def add(a, b):
            return a + b
        self.add = add

    def _raise_on_quarantine(self):
        self.huey.storage.conn.execute(
            'create trigger fail_quarantine before insert on quarantine '
            "begin select raise(abort, 'storage offline'); end")

    def _remove_trigger(self):
        self.huey.storage.conn.execute('drop trigger fail_quarantine')

    def test_quarantine_storage_failure_keeps_message(self):
        self.huey.storage.enqueue(b'bad')
        r = self.add(1, 2)
        self._raise_on_quarantine()
        try:
            self.assertRaises(sqlite3.IntegrityError, self.huey.dequeue)
        finally:
            self._remove_trigger()
        # Neither the original message nor a record was written.
        self.assertEqual(self.huey.pending_count(), 2)
        self.assertEqual(self.huey.quarantine_count(), 0)
        # Once storage recovers, the queue continues normally.
        self.assertEqual(self.execute_next(), 3)
        self.assertEqual(self.huey.quarantine_count(), 1)
        self.assertEqual(self.huey.pending_count(), 0)

    def test_move_failure_rolls_back(self):
        self.huey.storage.enqueue(b'bad')
        row_id, data = self.huey.storage.peek_message()
        original = self.huey.storage._insert_quarantine

        def fail(*args, **kwargs):
            raise RuntimeError('process killed')
        self.huey.storage._insert_quarantine = fail
        try:
            self.assertRaises(RuntimeError, self.huey.storage.quarantine_message,
                              'queue', row_id, data, 'x', 'y')
        finally:
            self.huey.storage._insert_quarantine = original
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertEqual(self.huey.quarantine_count(), 0)

    def test_schedule_storage_failure_keeps_message(self):
        now = datetime.datetime.now()
        self.huey.storage.add_to_schedule(b'bad', now - datetime.timedelta(
            seconds=5))
        self._raise_on_quarantine()
        try:
            self.assertRaises(sqlite3.IntegrityError, self.huey.read_schedule,
                              now)
        finally:
            self._remove_trigger()
        self.assertEqual(self.huey.scheduled_count(), 1)
        self.assertEqual(self.huey.quarantine_count(), 0)

    def test_restart_persistence(self):
        self.huey.storage.enqueue(b'bad-persisted')
        self.huey.dequeue()
        record, = self.huey.quarantined()
        self.huey.storage.close()

        reopened = SqliteHuey(filename=self.db_file, quarantine=True)
        try:
            self.assertEqual(reopened.quarantine_count(), 1)
            restored = reopened.get_quarantined(record.id)
            self.assertEqual(restored.data, b'bad-persisted')
            self.assertEqual(restored.error_class, 'deserialize-error')
        finally:
            reopened.storage.close()

    def test_quarantine_survives_flush_queue(self):
        self.huey.storage.enqueue(b'bad')
        self.huey.dequeue()
        self.huey.storage.flush_queue()
        self.assertEqual(self.huey.quarantine_count(), 1)
        self.huey.flush()
        self.assertEqual(self.huey.quarantine_count(), 0)


if __name__ == '__main__':
    unittest.main()
