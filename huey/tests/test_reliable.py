import datetime
import os
import shutil
import tempfile
import threading
import time
import unittest
import uuid

from huey import FileHuey
from huey import MemoryHuey
from huey import SqliteHuey
from huey.consumer import Consumer
from huey.consumer import Worker
from huey.consumer import WorkerRecycle
from huey.consumer_options import ConsumerConfig
from huey.consumer_options import OptionParserHandler
from huey.exceptions import ConfigurationError
from huey.tests.base import BaseTestCase


class ReliableTestCase(BaseTestCase):
    def get_huey(self):
        self._tmp_dir = tempfile.mkdtemp(prefix='huey-reliable-%s-' %
                                         uuid.uuid4().hex[:8])
        self.db_file = os.path.join(self._tmp_dir, 'huey.db')
        return SqliteHuey(filename=self.db_file, utc=False)

    def tearDown(self):
        super(ReliableTestCase, self).tearDown()
        self.huey.storage.close()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)


class TestReliableConfiguration(ReliableTestCase):
    def test_default_mode_is_unchanged(self):
        consumer = self.huey.create_consumer()
        self.assertFalse(consumer.reliable)
        self.assertNotIn('reliable', ConsumerConfig().values)
        self.assertNotIn('lease_seconds', ConsumerConfig().values)

    def test_reliable_requires_lease_capable_storage(self):
        from huey.api import Huey
        from huey.storage import BlackHoleStorage
        from huey.storage import FileStorage

        for huey in (MemoryHuey(utc=False),
                     FileHuey('huey', path=self._tmp_dir),
                     Huey(storage_class=BlackHoleStorage)):
            self.assertFalse(huey.storage.supports_leases)
            with self.assertRaises(ConfigurationError) as ctx:
                huey.create_consumer(reliable=True)
            self.assertIn('supports leased delivery',
                          str(ctx.exception))

    def test_reliable_rejects_greenlet_workers(self):
        with self.assertRaises(ConfigurationError):
            self.huey.create_consumer(reliable=True,
                                      worker_type='greenlet')

    def test_reliable_lease_seconds_validation(self):
        for value in (0, -1, 0.25, '5', True):
            with self.assertRaises(ConfigurationError):
                self.huey.create_consumer(reliable=True,
                                          lease_seconds=value)

        consumer = self.huey.create_consumer(reliable=True,
                                             lease_seconds=0.5)
        self.assertEqual(consumer.lease_seconds, 0.5)
        self.assertTrue(consumer.reliable)

    def test_reliable_config_and_cli(self):
        parser = OptionParserHandler().get_option_parser()
        options, _ = parser.parse_args(['--reliable', '--lease-seconds=5'])
        config = ConsumerConfig(**{
            k: v for k, v in options.__dict__.items() if v is not None})
        config.validate()
        self.assertTrue(config.values['reliable'])
        self.assertEqual(config.values['lease_seconds'], 5)

        # Reliable options are omitted from the default CLI config, so the
        # Consumer defaults apply and the native option set is preserved.
        options, _ = parser.parse_args([])
        config = ConsumerConfig(**{
            k: v for k, v in options.__dict__.items() if v is not None})
        config.validate()
        self.assertNotIn('reliable', config.values)
        self.assertNotIn('lease_seconds', config.values)

        config = ConsumerConfig(reliable=True, lease_seconds=None)
        self.assertRaises(ValueError, config.validate)

    def test_worker_receives_reliable_settings(self):
        consumer = self.huey.create_consumer(
            reliable=True, lease_seconds=3, workers=1)
        worker, _ = consumer.worker_threads[0]
        self.assertTrue(worker.reliable)
        self.assertEqual(worker.lease_seconds, 3)
        self.assertIs(worker.deliveries, consumer._deliveries)

    def test_in_flight_count_zero_without_leases(self):
        self.assertEqual(MemoryHuey(utc=False).in_flight_count(), 0)

    def test_legacy_worker_class_in_default_mode(self):
        # A custom worker class predating the reliable-mode keywords must keep
        # working in default mode.
        class LegacyWorker(Worker):
            def __init__(self, huey, stop_flag, default_delay, max_delay,
                         backoff, max_tasks=None):
                super(LegacyWorker, self).__init__(
                    huey, stop_flag, default_delay, max_delay, backoff,
                    max_tasks)

        class LegacyConsumer(Consumer):
            worker_class = LegacyWorker

        consumer = LegacyConsumer(self.huey)
        self.assertEqual(len(consumer.worker_threads), 1)

        # ...but it cannot be used for reliable mode.
        with self.assertRaises(ConfigurationError):
            LegacyConsumer(self.huey, reliable=True)


class TestReliableStorageCompat(ReliableTestCase):
    def test_flush_all_without_lease_table(self):
        # A database created before the lease table existed (create_tables=False
        # with an externally-managed schema) must still flush cleanly.
        import sqlite3

        self.huey.storage.close()
        filename = os.path.join(self._tmp_dir, 'old.db')
        conn = sqlite3.connect(filename)
        storage_class = type(self.huey.storage)
        for sql in (storage_class.table_kv, storage_class.table_sched,
                    storage_class.index_sched, storage_class.table_task,
                    storage_class.index_task, storage_class.table_counter):
            conn.execute(sql)
        conn.commit()
        conn.close()

        old = SqliteHuey(filename=filename, create_tables=False)
        try:
            old.flush()  # Must not raise "no such table: lease".
            # Reliable mode, however, refuses this database up front.
            old.pending_count()
            with self.assertRaises(ConfigurationError):
                old.create_consumer(reliable=True)
        finally:
            old.storage.close()


class TestReliableWorkerLoop(ReliableTestCase):
    def test_worker_processes_task_under_lease(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        result = add(1, 2)
        consumer = self.huey.create_consumer(reliable=True, lease_seconds=30)
        worker, _ = consumer.worker_threads[0]

        self.assertEqual(self.huey.pending_count(), 1)
        worker.loop()
        self.assertEqual(result.get(), 3)
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(self.huey.in_flight_count(), 0)

    def test_worker_leaves_delivery_on_unexpected_error(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        result = add(1, 2)
        consumer = self.huey.create_consumer(reliable=True, lease_seconds=30)
        worker, _ = consumer.worker_threads[0]

        # Make execution blow up outside the task body, before ack.
        def broken_execute(task, timestamp=None, delivery=None):
            raise RuntimeError('boom')
        self.huey.execute = broken_execute

        worker.loop()
        # The delivery is retained for recovery once the lease expires.
        self.assertEqual(self.huey.in_flight_count(), 1)
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertFalse(result.is_ready())

    def test_max_tasks_recycles_after_ack(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        add(1, 2)
        consumer = self.huey.create_consumer(reliable=True, lease_seconds=30,
                                             max_tasks=1,
                                             check_worker_health=True)
        worker, _ = consumer.worker_threads[0]
        self.assertRaises(WorkerRecycle, worker.loop)
        self.assertEqual(self.huey.in_flight_count(), 0)


class TestReliableExecution(ReliableTestCase):
    def setUp(self):
        super(TestReliableExecution, self).setUp()
        self.signals = []
        @self.huey.signal()
        def capture(sig, task, *args):
            self.signals.append((sig, task.id))

    def claim_and_execute(self):
        delivery = self.huey.claim_lease(30)
        self.assertIsNotNone(delivery)
        delivery.start_renewal()
        return delivery, self.huey.execute(delivery.task, delivery=delivery)

    def test_revoke_acks_delivery(self):
        @self.huey.task()
        def task_a(n):
            return n

        task_a.revoke()
        task_a(1)
        delivery, value = self.claim_and_execute()
        self.assertIsNone(value)
        self.assertEqual(self.huey.in_flight_count(), 0)

    def test_expired_task_acks_delivery(self):
        @self.huey.task(expires=1)
        def task_a(n):
            return n

        r = task_a(1)
        delivery = self.huey.claim_lease(30)
        future = datetime.datetime.now() + datetime.timedelta(seconds=5)
        self.assertIsNone(self.huey.execute(delivery.task,
                                            timestamp=future,
                                            delivery=delivery))
        self.assertEqual(self.huey.in_flight_count(), 0)

    def test_scheduled_task_moves_lease_to_schedule(self):
        @self.huey.task()
        def task_a(n):
            return n

        r = task_a.schedule((1,), delay=60)
        delivery = self.huey.claim_lease(30)
        self.assertIsNone(self.huey.execute(delivery.task, delivery=delivery))
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(self.huey.scheduled_count(), 1)
        task, = self.huey.scheduled()
        self.assertEqual(task.id, r.id)

    def test_duplicate_ack_does_not_change_state(self):
        @self.huey.task()
        def task_a(n):
            return n

        r = task_a(1)
        delivery, value = self.claim_and_execute()
        self.assertEqual(value, 1)
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(r.get(), 1)

        # A repeated ack (from the same delivery object, or an equivalent
        # stale one) is a no-op and cannot resurrect or delete anything.
        self.assertFalse(delivery.ack())
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 0)

    def test_stale_consumer_cannot_ack_reclaimed_delivery(self):
        @self.huey.task()
        def task_a(n):
            return n

        r = task_a(1)
        stale = self.huey.claim_lease(0.05)
        time.sleep(0.1)

        delivery = self.huey.claim_lease(30)
        self.assertEqual(delivery.task.id, stale.task.id)
        self.assertEqual(delivery.attempt, 2)

        # The stale consumer finishing late must not ack or delete the new
        # consumer's delivery.
        self.assertFalse(stale.ack())
        self.assertFalse(stale.retry(self.huey.serialize_task(stale.task)))
        self.assertEqual(self.huey.in_flight_count(), 1)

        # Executing through the current delivery works and cleans up.
        self.assertEqual(
            self.huey.execute(delivery.task, delivery=delivery), 1)
        self.assertEqual(self.huey.in_flight_count(), 0)

    def test_retry_handoff_is_atomic_and_has_no_duplicates(self):
        @self.huey.task(retries=2)
        def task_a():
            raise ValueError('nope')

        r = task_a()
        delivery = self.huey.claim_lease(30)
        self.assertIsNone(self.huey.execute(delivery.task, delivery=delivery))

        # Exactly one retry exists, and the original delivery is gone.
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertEqual(self.huey.scheduled_count(), 0)
        task, = self.huey.pending()
        self.assertEqual(task.id, r.id)
        self.assertEqual(task.retries, 1)

        # The old delivery cannot produce a second copy.
        self.assertFalse(delivery.retry(self.huey.serialize_task(task)))
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertEqual(self.huey.in_flight_count(), 0)

        # The retry runs and is acknowledged. It is a fresh queue entry, so
        # the delivery attempt counter starts over; retries are tracked on the
        # serialized task itself.
        delivery = self.huey.claim_lease(30)
        self.assertEqual(delivery.attempt, 1)
        self.assertIsNone(self.huey.execute(delivery.task, delivery=delivery))
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 1)  # Final retry queued.
        task, = self.huey.pending()
        self.assertEqual(task.retries, 0)

        delivery = self.huey.claim_lease(30)
        self.assertIsNone(self.huey.execute(delivery.task, delivery=delivery))
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(self.huey.scheduled_count(), 0)
        self.assertRaises(Exception, r.get)

    def test_retry_delay_handoff_goes_to_schedule_atomically(self):
        @self.huey.task(retries=1, retry_delay=60)
        def task_a():
            raise ValueError('nope')

        r = task_a()
        delivery = self.huey.claim_lease(30)
        self.assertIsNone(self.huey.execute(delivery.task, delivery=delivery))

        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(self.huey.scheduled_count(), 1)
        task, = self.huey.scheduled()
        self.assertEqual(task.id, r.id)
        self.assertEqual(task.retries, 0)
        self.assertTrue(task.eta > datetime.datetime.now())

    def test_retry_backoff_is_persisted_with_delivery_handoff(self):
        @self.huey.task(retries=2, retry_delay=2, retry_backoff=2)
        def failing():
            raise ValueError('nope')

        failing()
        delivery = self.huey.claim_lease(30)
        self.huey.execute(delivery.task, delivery=delivery)

        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 0)
        task, = self.huey.scheduled()
        self.assertEqual(task.retries, 1)
        self.assertEqual(task.retry_delay, 4)  # Grown by retry_backoff.
        self.assertEqual(task.retry_backoff, 2)

    def test_retry_task_exception_handoff(self):
        from huey.exceptions import RetryTask

        @self.huey.task()
        def retry_later():
            raise RetryTask(delay=30)

        retry_later()
        delivery = self.huey.claim_lease(30)
        self.huey.execute(delivery.task, delivery=delivery)
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(self.huey.scheduled_count(), 1)

    def test_retry_emits_enqueued_signal_like_default_mode(self):
        from huey.signals import SIGNAL_ENQUEUED
        seen = []

        @self.huey.signal()
        def capture(sig, task, *args):
            seen.append(sig)

        @self.huey.task(retries=1)
        def failing():
            raise ValueError('nope')

        failing()
        delivery = self.huey.claim_lease(30)
        self.huey.execute(delivery.task, delivery=delivery)
        # The immediate retry was placed on the queue. As with the default
        # mode, that emits SIGNAL_ENQUEUED for the retry.
        self.assertIn(SIGNAL_ENQUEUED, seen)
        # No SIGNAL_SCHEDULED: nothing was scheduled.
        self.assertNotIn('scheduled', seen)

    def test_retry_resolves_relative_expiry(self):
        # A retried task re-enqueued through the lease path must resolve its
        # relative ``expires`` again, exactly as the default path does through
        # ``enqueue()``.
        @self.huey.task(retries=1, expires=300)
        def failing():
            raise ValueError('nope')

        failing()
        before = self.huey.pending()[0].expires_resolved
        self.assertIsNotNone(before)

        delivery = self.huey.claim_lease(30)
        self.huey.execute(delivery.task, delivery=delivery)
        retried, = self.huey.pending()
        self.assertIsNotNone(retried.expires_resolved)
        self.assertGreaterEqual(retried.expires_resolved, before)

    def test_success_acks_after_followup_is_persisted(self):
        calls = []

        @self.huey.task()
        def first():
            calls.append('first')
            return 'value'

        @self.huey.task()
        def second():
            calls.append('second')

        pipeline = first.s().then(second.s())
        self.huey.enqueue(pipeline)
        second_id = pipeline.on_complete.id

        delivery = self.huey.claim_lease(30)
        self.assertEqual(self.huey.execute(delivery.task, delivery=delivery),
                         'value')
        # The follow-up is enqueued before the delivery is acknowledged.
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 1)
        task, = self.huey.pending()
        self.assertEqual(task.id, second_id)

    def test_on_error_handler_only_after_final_failure(self):
        # With intermediate errors suppressed, the error handler only runs
        # once retries are exhausted -- the same as the default mode.
        self.huey.store_intermediate_errors = False
        errors = []

        @self.huey.task(retries=1)
        def failing():
            errors.append('attempt')
            raise ValueError('boom')

        @self.huey.task()
        def on_error(exc):
            errors.append('handler')

        task = failing.s()
        task.error(on_error.s())
        self.huey.enqueue(task)

        # First failure: retry, no error handler yet.
        delivery = self.huey.claim_lease(30)
        self.huey.execute(delivery.task, delivery=delivery)
        self.assertEqual(errors, ['attempt'])
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.scheduled_count(), 0)

        # Second failure: error handler runs, delivery acknowledged.
        delivery = self.huey.claim_lease(30)
        self.huey.execute(delivery.task, delivery=delivery)
        self.assertEqual(errors, ['attempt', 'attempt'])
        self.assertEqual(self.huey.in_flight_count(), 0)
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertEqual(self.huey.scheduled_count(), 0)
        delivery = self.huey.claim_lease(30)
        self.huey.execute(delivery.task, delivery=delivery)
        self.assertEqual(errors, ['attempt', 'attempt', 'handler'])
        self.assertEqual(self.huey.in_flight_count(), 0)

    def test_priority_preserved(self):
        @self.huey.task()
        def task_a(n):
            return n

        task_a(1, priority=0)
        task_a(2, priority=9)
        task_a(3, priority=5)
        order = []
        for _ in range(3):
            delivery = self.huey.claim_lease(30)
            order.append(delivery.task.args[0])
            self.huey.execute(delivery.task, delivery=delivery)
        self.assertEqual(order, [2, 3, 1])

    def test_periodic_task_path(self):
        from huey.api import crontab
        @self.huey.periodic_task(crontab(minute='*'))
        def task_p():
            return 'p'

        task = self.huey.read_periodic(
            datetime.datetime(2000, 1, 1, 0, 0))[0]
        self.huey.enqueue(task)
        delivery = self.huey.claim_lease(30)
        self.assertEqual(self.huey.execute(delivery.task, delivery=delivery),
                         'p')
        self.assertEqual(self.huey.in_flight_count(), 0)


class TestReliableRenewal(ReliableTestCase):
    def test_renewal_extends_lease_past_original_expiry(self):
        @self.huey.task()
        def noop():
            pass

        noop()
        delivery = self.huey.claim_lease(0.6)
        delivery.renew_interval = 0.1
        delivery.start_renewal()
        try:
            # Renewal runs every 0.1s, so the delivery stays owned well past
            # the point where the original 0.6s lease would have expired.
            time.sleep(1.0)
            self.assertIsNone(self.huey.claim_lease(0.6))
            self.assertEqual(self.huey.in_flight_count(), 1)
        finally:
            self.assertTrue(delivery.ack())
        self.assertEqual(self.huey.in_flight_count(), 0)

    def test_renewal_after_expiry_without_takeover_still_acks(self):
        # If the lease lapsed (e.g. the consumer was stalled) but nobody
        # reclaimed it, the ticket is still current and the work can still be
        # acknowledged. Marking the delivery dead on a failed renewal would
        # leave a finished task in the lease table to be run again.
        @self.huey.task()
        def noop():
            return 'done'

        noop()
        delivery = self.huey.claim_lease(0.15)
        delivery.renew_interval = 5  # Simulate a stalled renewal loop.
        delivery.start_renewal()
        time.sleep(0.25)  # Lease expired; nothing else claimed it.

        self.assertFalse(delivery.done)
        self.assertEqual(self.huey.execute(delivery.task, delivery=delivery),
                         'done')
        self.assertEqual(self.huey.in_flight_count(), 0)

    def test_stalled_lease_is_renewed_after_lock_wait(self):
        # The clock used to compute the new expiry must be read after the
        # write lock is acquired. Otherwise a renewal that waited on the lock
        # would measure the new lease from before the wait.
        import sqlite3

        @self.huey.task()
        def noop():
            pass

        noop()
        delivery = self.huey.claim_lease(1.0)
        held = sqlite3.connect(self.db_file, isolation_level=None,
                               check_same_thread=False)
        held.execute('begin immediate')
        timer = threading.Timer(0.6, held.execute, ('commit',))
        timer.start()
        try:
            start = time.monotonic()
            self.assertTrue(self.huey.storage.renew_lease(delivery, 1.0))
            waited = time.monotonic() - start
            self.assertGreaterEqual(waited, 0.4)
        finally:
            timer.join()
            held.close()

        # The full renewed lease should remain: with the clock read before the
        # wait, roughly `waited` seconds of it would already have elapsed.
        time.sleep(0.7)
        self.assertIsNone(self.huey.claim_lease(0.1))
        self.assertTrue(delivery.ack())

    def test_renew_of_already_expired_lease_is_refused(self):
        # If the renewal itself was delayed past the lease, ownership is
        # genuinely lost: renew must report failure rather than install an
        # expiry computed from a stale clock reading.
        import sqlite3

        @self.huey.task()
        def noop():
            pass

        noop()
        delivery = self.huey.claim_lease(0.4)
        held = sqlite3.connect(self.db_file, isolation_level=None,
                               check_same_thread=False)
        held.execute('begin immediate')
        timer = threading.Timer(0.6, held.execute, ('commit',))
        timer.start()
        try:
            self.assertFalse(self.huey.storage.renew_lease(delivery, 0.4))
        finally:
            timer.join()
            held.close()
        # The expired delivery is now claimable by another consumer.
        self.assertIsNotNone(self.huey.claim_lease(5))

    def test_renewal_survives_transient_storage_error(self):
        @self.huey.task()
        def noop():
            pass

        noop()
        delivery = self.huey.claim_lease(0.5)
        delivery.renew_interval = 0.05
        real_renew = self.huey.storage.renew_lease
        calls = []

        def flaky_renew(lease, seconds):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('database is locked')
            return real_renew(lease, seconds)

        self.huey.storage.renew_lease = flaky_renew
        try:
            delivery.start_renewal()
            time.sleep(0.9)  # Past the 0.5s lease.
            # Renewal kept going after the error, so the delivery is still
            # owned and was not stolen.
            self.assertGreater(len(calls), 2)
            self.assertIsNone(self.huey.storage.claim_lease(1))
        finally:
            self.huey.storage.renew_lease = real_renew
        self.assertTrue(delivery.ack())

    def test_expired_lease_stops_renewal_and_is_reclaimable(self):
        @self.huey.task()
        def noop():
            pass

        noop()
        delivery = self.huey.claim_lease(0.2)
        delivery.renew_interval = 5  # Do not renew before expiry.
        delivery.start_renewal()
        time.sleep(0.3)
        new = self.huey.claim_lease(30)
        self.assertIsNotNone(new)
        self.assertEqual(new.attempt, 2)
        self.assertFalse(delivery.ack())
        self.assertTrue(new.ack())

    def test_long_task_keeps_ownership(self):
        release = threading.Event()
        started = threading.Event()

        @self.huey.task()
        def long_running():
            started.set()
            release.wait(5)
            return 'done'

        result = long_running()
        consumer = self.huey.create_consumer(reliable=True,
                                             lease_seconds=0.6,
                                             initial_delay=0.01,
                                             max_delay=0.01,
                                             check_worker_health=False)
        _, worker_t = consumer.worker_threads[0]
        consumer.scheduler.start()
        worker_t.start()
        try:
            self.assertTrue(started.wait(2))
            # Task runs far longer than the initial 0.6s lease. A competing
            # claim must not be able to steal it.
            deadline = time.time() + 1.5
            while time.time() < deadline:
                self.assertIsNone(self.huey.storage.claim_lease(0.6))
                time.sleep(0.1)
            release.set()
            self.assertEqual(result.get(blocking=True, timeout=5), 'done')
            self.assertEqual(self.huey.in_flight_count(), 0)
        finally:
            release.set()
            consumer.stop(graceful=True)


class TestReliableShutdown(ReliableTestCase):
    def test_graceful_shutdown_finishes_inflight_then_acks(self):
        release = threading.Event()
        started = threading.Event()

        @self.huey.task()
        def blocking():
            started.set()
            release.wait(5)
            return 42

        @self.huey.task()
        def leftover():
            return 'never'

        result = blocking()
        leftover()  # Must not be claimed once shutdown begins.
        consumer = self.huey.create_consumer(reliable=True, lease_seconds=30,
                                             initial_delay=0.01,
                                             max_delay=0.01,
                                             check_worker_health=False)
        _, worker_t = consumer.worker_threads[0]
        consumer.scheduler.start()
        worker_t.start()
        self.assertTrue(started.wait(2))

        stopper = threading.Thread(target=consumer.stop,
                                   kwargs={'graceful': True})
        stopper.start()
        time.sleep(0.1)
        # The in-flight task is still owned, not abandoned mid-run.
        self.assertEqual(self.huey.in_flight_count(), 1)
        release.set()
        stopper.join(5)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(worker_t.is_alive())

        self.assertEqual(result.get(), 42)
        self.assertEqual(self.huey.in_flight_count(), 0)
        # The queued task was never claimed, and is still pending.
        self.assertEqual(self.huey.pending_count(), 1)

    def test_forced_shutdown_leaves_delivery_for_recovery(self):
        release = threading.Event()
        started = threading.Event()

        @self.huey.task()
        def blocking():
            started.set()
            release.wait(5)
            return 'value'

        result = blocking()
        consumer = self.huey.create_consumer(reliable=True,
                                             lease_seconds=0.5,
                                             shutdown_timeout=0.2,
                                             initial_delay=0.01,
                                             max_delay=0.01,
                                             check_worker_health=False)
        _, worker_t = consumer.worker_threads[0]
        consumer.scheduler.start()
        worker_t.start()
        self.assertTrue(started.wait(2))

        with self.assertLogs('huey.consumer', 'WARNING'):
            consumer.stop(graceful=True)  # Times out, abandons delivery.

        # The delivery is still recorded, i.e. there is recovery evidence.
        self.assertEqual(self.huey.in_flight_count(), 1)

        # Its renewal was stopped by the shutdown, so the lease expires and
        # another consumer can take the task over while the abandoned task is
        # still blocked.
        time.sleep(0.6)
        delivery = self.huey.claim_lease(30)
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery.task.id, result.id)
        self.assertEqual(delivery.attempt, 2)

        # The abandoned consumer now finishes. Its acknowledgement must be
        # ignored (fenced), and it must not delete the new consumer's delivery.
        release.set()
        worker_t.join(3)
        self.assertEqual(self.huey.in_flight_count(), 1)
        self.assertTrue(delivery.ack())
        self.assertEqual(self.huey.in_flight_count(), 0)


if __name__ == '__main__':
    unittest.main()
