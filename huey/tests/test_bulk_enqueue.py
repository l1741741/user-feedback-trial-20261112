"""
Tests for the bounded, back-pressured bulk-enqueue entry-point
(``Huey.bulk_enqueue`` / :class:`BulkEnqueueSession`).

The scenarios cover:

* a one-shot generator with side-effect counting (never fully materialized);
* peak in-flight bounded by the producer window under a slow consumer;
* real two-producer concurrency against a hard storage queue limit;
* SQLite hard-limit enforcement under concurrent publishing/consuming;
* resume/continuation keyed by the stable task id;
* the success / definite-failure / unknown outcome boundaries;
* cancellation, generator errors, unserializable members and a stopped
  consumer, all without deadlock or lost terminal receipts;
* a fixed timeout budget that is not reset on each wake-up.
"""
import os
import threading
import time

from huey import BULK_FAILED
from huey import BULK_SUCCESS
from huey import BULK_UNKNOWN
from huey import BlackHoleHuey
from huey import BulkEnqueueError
from huey import MemoryHuey
from huey import SqliteHuey
from huey.exceptions import ConfigurationError
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase


class CountingGenerator(object):
    """One-shot iterable that records how often its source was advanced.

    The counter is the observable side effect proving the generator was never
    drained into memory ahead of consumption.
    """
    def __init__(self, n, factory):
        self.n = n
        self.factory = factory
        self.pulled = 0
        self.exhausted = False

    def __iter__(self):
        for i in range(self.n):
            self.pulled += 1
            yield self.factory(i)
        self.exhausted = True


class Unserializable(object):
    def __reduce__(self):
        raise RuntimeError('cannot be serialized')


class BulkEnqueueMemoryTests(BaseTestCase):
    def get_huey(self):
        return MemoryHuey(utc=False)

    def setUp(self):
        super(BulkEnqueueMemoryTests, self).setUp()

        @self.huey.task()
        def add(n):
            return n
        self.add = add

    # -- basic behaviour ---------------------------------------------------

    def test_bulk_enqueue_delivers_one_receipt_per_member(self):
        gen = CountingGenerator(25, lambda i: self.add.s(i))
        with self.huey.bulk_enqueue(gen, max_in_flight=4) as session:
            receipts = list(session)

        self.assertEqual(len(receipts), 25)
        self.assertTrue(all(r.status == BULK_SUCCESS for r in receipts))
        self.assertTrue(all(r.ok for r in receipts))
        self.assertEqual(gen.pulled, 25)
        self.assertTrue(gen.exhausted)
        self.assertEqual(self.huey.pending_count(), 25)
        # Every receipt carries the stable task id of its member.
        self.assertEqual(
            sorted(r.task_id for r in receipts),
            sorted(t.id for t in self.huey.pending()))

    def test_generator_is_consumed_lazily(self):
        gate = threading.Event()
        pulled = []

        def gen():
            for i in range(100):
                pulled.append(i)
                yield self.add.s(i)
                if i == 0:
                    gate.wait(2.0)  # Pause the producer after the first item.

        session = self.huey.bulk_enqueue(gen(), max_in_flight=2)
        first = next(session)
        self.assertEqual(first.status, BULK_SUCCESS)
        # Give the producer room to fill only its small window, then assert it
        # never raced ahead through the whole generator.
        time.sleep(0.05)
        self.assertLess(len(pulled), 10)
        gate.set()
        rest = list(session)
        session.close()
        self.assertEqual(len(rest) + 1, 100)
        self.assertEqual(len(pulled), 100)

    def test_peak_in_flight_bounded_by_window(self):
        # Window smaller than the storage limit: producer may run ahead only
        # up to max_in_flight while a slow consumer drains the queue.
        gen = CountingGenerator(60, lambda i: self.add.s(i))
        session = self.huey.bulk_enqueue(
            gen, max_in_flight=3, queue_limit=10, timeout=5,
            poll_interval=0.002)
        seen = 0
        for receipt in session:
            seen += 1
            time.sleep(0.003)  # Slow consumer frees storage capacity.
            self.huey.storage.dequeue()
        session.close()
        self.assertEqual(seen, 60)
        self.assertLessEqual(session.peak_in_flight, 3)
        self.assertEqual(session.in_flight, 0)
        # All control records were released by the end.
        self.assertEqual(session._slots._value, 3)

    def test_unbounded_queue_limit_still_windows_production(self):
        # Without a storage hard limit the in-flight window alone bounds how
        # far the generator is pulled ahead of receipt delivery.
        gen = CountingGenerator(40, lambda i: self.add.s(i))
        session = self.huey.bulk_enqueue(
            gen, max_in_flight=5, poll_interval=0.002)
        seen = 0
        for receipt in session:
            seen += 1
            time.sleep(0.002)
        session.close()
        self.assertEqual(seen, 40)
        self.assertLessEqual(session.peak_in_flight, 5)

    # -- hard capacity -----------------------------------------------------

    def test_queue_limit_blocks_until_consumer_frees_space(self):
        gen = CountingGenerator(30, lambda i: self.add.s(i))
        session = self.huey.bulk_enqueue(
            gen, max_in_flight=3, queue_limit=5, timeout=5,
            poll_interval=0.002)
        count = 0
        for receipt in session:
            self.assertTrue(receipt.ok)
            count += 1
            self.assertLessEqual(self.huey.pending_count(), 5)
            self.huey.storage.dequeue()  # Real consumer frees capacity.
        session.close()
        self.assertEqual(count, 30)
        self.assertEqual(self.huey.pending_count(), 0)

    def test_queue_full_timeout_is_definite_failure(self):
        # Pre-fill to the hard limit and never consume. Once the fixed budget
        # expires the session terminates; every member that reached the
        # capacity check is reported as a definite failure (nothing written),
        # and the untouched remainder is simply not pulled from the generator.
        for _ in range(3):
            self.huey.storage.enqueue(b'x')
        session = self.huey.bulk_enqueue(
            (self.add.s(i) for i in range(4)), max_in_flight=1,
            queue_limit=3, timeout=0.1, poll_interval=0.02)
        start = time.monotonic()
        receipts = list(session)
        elapsed = time.monotonic() - start
        session.close()

        self.assertTrue(receipts)
        self.assertTrue(all(
            r.status == BULK_FAILED and r.reason == 'queue-full-timeout'
            for r in receipts))
        # The deadline is fixed for the whole session.
        self.assertLess(elapsed, 1.0)
        # Nothing was written for the rejected members; limit held.
        self.assertEqual(self.huey.pending_count(), 3)
        # Each definite failure still carries its stable id for resumption.
        self.assertTrue(all(r.task_id for r in receipts))

    def test_timeout_budget_is_not_reset_per_wakeup(self):
        for _ in range(2):
            self.huey.storage.enqueue(b'x')
        session = self.huey.bulk_enqueue(
            (self.add.s(i) for i in range(3)), max_in_flight=3,
            queue_limit=2, timeout=0.25, poll_interval=0.02)
        start = time.monotonic()
        list(session)
        elapsed = time.monotonic() - start
        session.close()
        # Many wake-ups happen within the window; total time must track the
        # single fixed deadline, not N * timeout.
        self.assertLess(elapsed, 1.0)

    # -- two real producers ------------------------------------------------

    def test_two_producers_respect_hard_limit(self):
        limit = 15
        observed_max = [0]
        counter_lock = threading.Lock()
        stop = threading.Event()

        def consumer():
            while not stop.is_set():
                if self.huey.storage.dequeue() is None:
                    time.sleep(0.001)
                    continue
                with counter_lock:
                    size = self.huey.storage.queue_size()
                    if size > observed_max[0]:
                        observed_max[0] = size

        worker = threading.Thread(target=consumer)
        worker.start()

        results = [[], []]

        def produce(idx, n):
            sess = self.huey.bulk_enqueue(
                (self.add.s(idx * 1000 + i) for i in range(n)),
                max_in_flight=6, queue_limit=limit, timeout=20,
                poll_interval=0.002)
            for r in sess:
                results[idx].append(r)
            sess.close()

        try:
            t1 = threading.Thread(target=produce, args=(0, 150))
            t2 = threading.Thread(target=produce, args=(1, 150))
            t1.start()
            t2.start()
            t1.join(5)
            t2.join(5)
            self.assertFalse(t1.is_alive() or t2.is_alive())
        finally:
            stop.set()
            worker.join()

        all_receipts = results[0] + results[1]
        self.assertEqual(len(all_receipts), 300)
        self.assertTrue(all(r.ok for r in all_receipts))
        self.assertLessEqual(observed_max[0], limit)

    # -- failure boundaries ------------------------------------------------

    def test_unserializable_member_is_definite_failure(self):
        def gen():
            yield self.add.s(1)
            yield self.add.s(Unserializable())
            yield self.add.s(3)

        with self.huey.bulk_enqueue(gen(), max_in_flight=2) as session:
            receipts = list(session)

        statuses = [(r.status, r.reason) for r in receipts]
        self.assertEqual(statuses[0], (BULK_SUCCESS, None))
        self.assertEqual(statuses[1], (BULK_FAILED, 'unserializable'))
        self.assertEqual(statuses[2], (BULK_SUCCESS, None))
        self.assertIsInstance(receipts[1].error, RuntimeError)
        # Stream continues after a bad member.
        self.assertEqual(self.huey.pending_count(), 2)

    def test_invalid_member_is_definite_failure(self):
        with self.huey.bulk_enqueue(
                iter([self.add.s(1), 'not-a-task', self.add.s(2)]),
                max_in_flight=2) as session:
            receipts = list(session)
        self.assertEqual([r.reason for r in receipts],
                         [None, 'invalid-member', None])
        self.assertEqual(receipts[1].status, BULK_FAILED)

    def test_storage_error_is_unknown_outcome(self):
        def boom(data, priority=None, limit=None):
            raise RuntimeError('connection lost')
        self.huey.storage.enqueue_if_space = boom

        with self.huey.bulk_enqueue(
                (self.add.s(i) for i in range(3)), max_in_flight=2,
                queue_limit=5) as session:
            receipts = list(session)
        self.assertTrue(all(
            r.status == BULK_UNKNOWN and r.reason == 'storage-error'
            for r in receipts))
        self.assertTrue(all(r.task_id for r in receipts))

    def test_resume_after_partial_failure_uses_stable_ids(self):
        def gen():
            yield self.add.s(1)
            yield self.add.s(Unserializable())
            yield self.add.s(3)

        with self.huey.bulk_enqueue(gen(), max_in_flight=2) as session:
            first = list(session)

        succeeded = {r.task_id for r in first if r.status == BULK_SUCCESS}
        failed = [r for r in first if r.status == BULK_FAILED]
        self.assertEqual(len(succeeded), 2)
        self.assertEqual(len(failed), 1)

        # Resume by re-sending only a replacement for the failed member.
        replacement = self.add.s(42)
        with self.huey.bulk_enqueue(iter([replacement]),
                                    max_in_flight=2) as session:
            second = list(session)
        self.assertEqual(len(second), 1)
        self.assertTrue(second[0].ok)

        queued_ids = {t.id for t in self.huey.pending()}
        # Original successes remain, each exactly once (no blind resend).
        self.assertEqual(len(queued_ids), 3)
        self.assertTrue(succeeded.issubset(queued_ids))
        self.assertIn(replacement.id, queued_ids)

    def test_generator_error_delivers_prior_receipts_then_raises(self):
        def gen():
            yield self.add.s(1)
            yield self.add.s(2)
            raise ValueError('generator blew up')

        with self.huey.bulk_enqueue(gen(), max_in_flight=2) as session:
            with self.assertRaises(BulkEnqueueError) as ctx:
                list(session)
        self.assertIsInstance(ctx.exception.original, ValueError)
        # The two members produced before the error were still committed.
        self.assertEqual(self.huey.pending_count(), 2)
        self.assertEqual(session._slots._value, 2)

    # -- cancellation / shutdown ------------------------------------------

    def test_cancel_stops_production_without_losing_receipts(self):
        event = threading.Event()

        def slow_gen():
            for i in range(100):
                time.sleep(0.004)
                yield self.add.s(i)

        session = self.huey.bulk_enqueue(
            slow_gen(), max_in_flight=2, queue_limit=4, cancel_event=event,
            poll_interval=0.01)
        consumed = 0
        for receipt in session:
            consumed += 1
            self.huey.storage.dequeue()
            if consumed == 6:
                event.set()
                break

        # Remaining already-produced receipts are still readable, then the
        # iterator terminates rather than hanging.
        rest = list(session)
        session.close()
        self.assertTrue(session.cancelled)
        self.assertFalse(session._thread.is_alive())
        self.assertEqual(session._slots._value, 2)
        self.assertLess(consumed + len(rest), 100)

    def test_cancel_interrupts_full_queue_wait(self):
        event = threading.Event()
        # Fill the hard limit; no consumer, so the producer must block.
        for _ in range(2):
            self.huey.storage.enqueue(b'x')
        session = self.huey.bulk_enqueue(
            (self.add.s(i) for i in range(5)), max_in_flight=2,
            queue_limit=2, cancel_event=event, poll_interval=0.02)

        def cancel_soon():
            time.sleep(0.15)
            event.set()
        threading.Thread(target=cancel_soon).start()

        start = time.monotonic()
        receipts = list(session)
        elapsed = time.monotonic() - start
        session.close()
        self.assertLess(elapsed, 2.0)
        self.assertTrue(all(
            r.status == BULK_FAILED and r.reason == 'cancelled'
            for r in receipts))

    def test_stopped_consumer_close_does_not_deadlock(self):
        session = self.huey.bulk_enqueue(
            (self.add.s(i) for i in range(50)), max_in_flight=4,
            queue_limit=4, poll_interval=0.01)
        first = next(session)
        self.assertTrue(first.ok)
        time.sleep(0.05)
        # Caller stops reading entirely. close() must still terminate.
        start = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - start, 5.0)
        self.assertFalse(session._thread.is_alive())
        self.assertEqual(session._slots._value, 4)

    def test_close_is_idempotent(self):
        session = self.huey.bulk_enqueue(
            (self.add.s(i) for i in range(5)), max_in_flight=2)
        list(session)
        session.close()
        session.close()  # Must not raise or block.
        self.assertFalse(session._thread.is_alive())

    # -- configuration rejection ------------------------------------------

    def test_unsupported_backend_rejected_before_consuming_generator(self):
        blackhole = BlackHoleHuey(utc=False)
        pulled = []

        def gen():
            pulled.append(1)
            yield self.add.s(1)

        with self.assertRaises(ConfigurationError):
            blackhole.bulk_enqueue(gen(), max_in_flight=2)
        self.assertEqual(pulled, [])  # Generator never touched.

    def test_immediate_mode_rejected(self):
        immediate = MemoryHuey(utc=False, immediate=True)
        with self.assertRaises(ConfigurationError):
            immediate.bulk_enqueue(iter([self.add.s(1)]))

    def test_invalid_arguments_rejected(self):
        with self.assertRaises(ValueError):
            self.huey.bulk_enqueue(iter([]), max_in_flight=0)
        with self.assertRaises(ValueError):
            self.huey.bulk_enqueue(iter([]), queue_limit=0)
        with self.assertRaises(ValueError):
            self.huey.bulk_enqueue(iter([]), poll_interval=0)
        with self.assertRaises(ValueError):
            self.huey.bulk_enqueue(iter([]), timeout=-1)


class BulkEnqueueSqliteTests(BaseTestCase):
    def get_huey(self):
        self.db_path = os.path.join(TEST_TMP_DIR, 'bulk-enqueue.db')
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        return SqliteHuey(filename=self.db_path, timeout=10)

    def tearDown(self):
        super(BulkEnqueueSqliteTests, self).tearDown()
        self.huey.storage.close()
        for suffix in ('', '-wal', '-shm'):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)

    def test_sqlite_hard_limit_under_two_producers(self):
        @self.huey.task()
        def task(n):
            return n

        limit = 12
        observed_max = [0]
        lock = threading.Lock()
        stop = threading.Event()

        # A second storage handle on the same database file. The producers
        # share the huey registry (as separate processes sharing task code
        # would) but contend on the same on-disk queue through two
        # connections, which is exactly where the hard limit must hold.
        other_storage = SqliteHuey(
            name=self.huey.name, filename=self.db_path, timeout=10).storage

        def consumer():
            while not stop.is_set():
                if self.huey.storage.dequeue() is None:
                    time.sleep(0.001)
                    continue
                with lock:
                    size = self.huey.storage.queue_size()
                    if size > observed_max[0]:
                        observed_max[0] = size

        worker = threading.Thread(target=consumer)
        worker.start()
        results = [[], []]

        def produce(storage, idx, n):
            session = self.huey.bulk_enqueue(
                (task.s(idx * 1000 + i) for i in range(n)),
                max_in_flight=5, queue_limit=limit, timeout=20,
                poll_interval=0.002)
            # Publish through the contended connection while keeping the
            # shared registry for serialization.
            session.huey.storage = storage
            for r in session:
                results[idx].append(r)
            session.close()

        try:
            t1 = threading.Thread(
                target=produce, args=(self.huey.storage, 0, 120))
            t2 = threading.Thread(target=produce, args=(other_storage, 1, 120))
            t1.start()
            t2.start()
            t1.join(10)
            t2.join(10)
            self.assertFalse(t1.is_alive() or t2.is_alive())
        finally:
            stop.set()
            worker.join()
            other_storage.close()

        receipts = results[0] + results[1]
        self.assertEqual(len(receipts), 240)
        self.assertTrue(all(r.ok for r in receipts))
        self.assertLessEqual(observed_max[0], limit)

    def test_sqlite_control_records_released_after_drain(self):
        @self.huey.task()
        def task(n):
            return n

        session = self.huey.bulk_enqueue(
            (task.s(i) for i in range(20)), max_in_flight=4, queue_limit=8)
        count = 0
        for receipt in session:
            count += 1
            self.huey.storage.dequeue()
        session.close()
        self.assertEqual(count, 20)
        self.assertEqual(session._slots._value, 4)
        self.assertEqual(session.in_flight, 0)
        self.assertFalse(session._thread.is_alive())
