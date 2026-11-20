"""Tests for durable task-completion events (SqliteHuey ``completion_events``).

Covers the event contract, atomic result/event consistency, lease/ack
semantics including late acknowledgement after takeover, retention-based
purge, queue isolation, payload bounds, outlet-failure behaviour, and the
unchanged default signal behaviour.

Multi-process behaviour (real consumer + independent receiver surviving a
crash/restart) lives in ``test_completion_process.py``.
"""
import json
import os
import threading
import time
import unittest
import uuid

from huey import SqliteHuey
from huey.api import Huey
from huey.api import MemoryHuey
from huey.completion import build_error_summary
from huey.completion import build_payload
from huey.completion import COMPLETION_CANCELED
from huey.completion import COMPLETION_EVENT_VERSION
from huey.completion import COMPLETION_EXPIRED
from huey.completion import COMPLETION_FAILED
from huey.completion import COMPLETION_RETRYING
from huey.completion import COMPLETION_REVOKED
from huey.completion import COMPLETION_SUCCEEDED
from huey.completion import LocalCompletionReceiver
from huey.completion import TERMINAL_STATUSES
from huey.exceptions import ConfigurationError
from huey.exceptions import TaskException
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase


class CompletionTestCase(BaseTestCase):
    def setUp(self):
        # BaseTestCase.setUp() calls get_huey(), so the path must exist first.
        self.db_file = os.path.join(TEST_TMP_DIR,
                                    'completion-%s.db' % uuid.uuid4().hex)
        super(CompletionTestCase, self).setUp()

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            path = self.db_file + suffix
            if os.path.exists(path):
                os.unlink(path)
        super(CompletionTestCase, self).tearDown()

    def get_huey(self):
        return SqliteHuey(filename=self.db_file, utc=False,
                          completion_events=True)

    def other_huey(self, name):
        return SqliteHuey(name=name, filename=self.db_file, utc=False,
                          completion_events=True)

    def pull(self, **kwargs):
        kwargs.setdefault('lease_seconds', 30)
        return self.huey.pull_completion_events(**kwargs)

    def statuses(self, events):
        return [e.status for e in events]


class TestCompletionContract(CompletionTestCase):
    def test_success_writes_result_and_event(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        result = add(2, 3)
        self.assertEqual(self.execute_next(), 5)

        # The default result API is unchanged.
        self.assertEqual(result.get(), 5)

        events = self.pull()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.status, COMPLETION_SUCCEEDED)
        self.assertTrue(event.final)
        self.assertTrue(event.result_available)
        self.assertEqual(event.task_id, result.id)
        self.assertEqual(event.queue, 'huey')
        data = event.data
        self.assertEqual(data['version'], COMPLETION_EVENT_VERSION)
        self.assertEqual(data['status'], COMPLETION_SUCCEEDED)
        self.assertTrue(data['final'])

    def test_final_failure_distinguished_from_retrying(self):
        attempts = []

        @self.huey.task(retries=1)
        def flaky():
            attempts.append(1)
            raise ValueError('nope')

        r = flaky()
        self.execute_next()  # First attempt: retry pending.
        first = self.pull()
        self.assertEqual(self.statuses(first), [COMPLETION_RETRYING])
        self.assertFalse(first[0].final)
        self.assertIn(first[0].status,
                      {'retrying'})  # Not a terminal status.

        self.execute_next()  # Second attempt: retries exhausted.
        second = self.pull()
        self.assertEqual(self.statuses(second), [COMPLETION_FAILED])
        self.assertTrue(second[0].final)
        # The final failure is the only one that stores an error result.
        with self.assertRaises(TaskException):
            r.get()
        self.assertEqual(len(attempts), 2)

    def test_retry_only_exception_is_never_final(self):
        from huey.exceptions import RetryTask
        from huey.exceptions import RateLimitExceeded

        @self.huey.task(retries=1)
        def wants_retry():
            raise RetryTask(delay=0)

        @self.huey.task()
        def rate_limited():
            raise RateLimitExceeded('key', 1.0, retry=True)

        wants_retry()
        self.assertIsNone(self.execute_next())
        rate_limited()
        self.assertIsNone(self.execute_next())

        events = self.pull()
        self.assertEqual(self.statuses(events),
                         [COMPLETION_RETRYING, COMPLETION_RETRYING])
        self.assertFalse(any(e.final for e in events))
        # No error result stored for a retry-pending attempt.
        self.assertFalse(any(e.result_available for e in events))

    def test_intermediate_error_withheld(self):
        huey = SqliteHuey(filename=self.db_file, utc=False,
                          completion_events=True,
                          store_intermediate_errors=False)

        @huey.task(retries=1)
        def flaky():
            raise ValueError('nope')

        flaky()
        huey.execute(huey.dequeue())  # Non-final, no intermediate error.
        events = huey.pull_completion_events()
        self.assertEqual([e.status for e in events], [COMPLETION_RETRYING])
        self.assertFalse(events[0].result_available)
        self.assertEqual(huey.storage.result_store_size(), 0)

    def test_revoked_task_event(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        add.revoke(revoke_once=True)
        r = add(1, 2)
        self.assertIsNone(self.execute_next())

        events = self.pull()
        self.assertEqual(self.statuses(events), [COMPLETION_REVOKED])
        self.assertTrue(events[0].final)
        self.assertFalse(events[0].result_available)
        # No result was stored for the revoked task.
        self.assertFalse(r.is_ready())

    def test_expired_task_event(self):
        import datetime

        @self.huey.task()
        def add(a, b):
            return a + b

        past = datetime.datetime.now() - datetime.timedelta(minutes=1)
        r = add.s(1, 2, expires=past)
        self.huey.enqueue(r)
        self.assertIsNone(self.execute_next())

        events = self.pull()
        self.assertEqual(self.statuses(events), [COMPLETION_EXPIRED])
        self.assertTrue(events[0].final)
        self.assertFalse(events[0].result_available)

    def test_canceled_task_event(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        @self.huey.pre_execute()
        def cancel(task):
            from huey.exceptions import CancelExecution
            raise CancelExecution('do not run')

        r = add(1, 2)
        self.assertIsNone(self.execute_next())
        events = self.pull()
        self.assertEqual(self.statuses(events), [COMPLETION_CANCELED])
        self.assertTrue(events[0].final)

    def test_cancel_execution_inside_task(self):
        from huey.exceptions import CancelExecution

        @self.huey.task()
        def cancel_me():
            raise CancelExecution('abort')

        r = cancel_me()
        self.assertIsNone(self.execute_next())
        events = self.pull()
        self.assertEqual(self.statuses(events), [COMPLETION_CANCELED])
        self.assertTrue(events[0].final)
        # A canceled execution still produces the conventional error result,
        # so the event advertises one.
        self.assertTrue(events[0].result_available)
        with self.assertRaises(TaskException):
            r.get()

    def test_terminal_statuses_are_final(self):
        self.assertEqual(
            TERMINAL_STATUSES,
            {COMPLETION_SUCCEEDED, COMPLETION_FAILED, COMPLETION_CANCELED,
             COMPLETION_REVOKED, COMPLETION_EXPIRED})
        self.assertNotIn(COMPLETION_RETRYING, TERMINAL_STATUSES)


class TestDefaultSignalBehaviour(CompletionTestCase):
    def test_signals_unchanged_when_enabled(self):
        signals = []

        @self.huey.signal()
        def capture(signal, task, *args):
            signals.append(signal)

        @self.huey.task()
        def add(a, b):
            return a + b

        add(1, 2)
        self.execute_next()
        self.assertEqual(signals, ['enqueued', 'executing', 'complete'])

    def test_signals_unchanged_when_disabled(self):
        huey = SqliteHuey(filename=self.db_file, utc=False)
        self.assertFalse(huey.completion_events)

        signals = []

        @huey.signal()
        def capture(signal, task, *args):
            signals.append(signal)

        @huey.task()
        def add(a, b):
            return a + b

        r = add(1, 2)
        huey.execute(huey.dequeue())
        self.assertEqual(signals, ['enqueued', 'executing', 'complete'])
        self.assertEqual(r.get(), 3)
        # Nothing was recorded in the outbox.
        self.assertEqual(huey.storage.completion_event_count(), 0)

    def test_completion_unavailable_on_unsupported_storage(self):
        with self.assertRaises(ConfigurationError):
            MemoryHuey(completion_events=True)

    def test_default_off(self):
        self.assertFalse(SqliteHuey(filename=self.db_file, utc=False)
                         .completion_events)


class TestPayload(CompletionTestCase):
    def test_payload_excludes_args_and_traceback(self):
        @self.huey.task()
        def leaky(secret, password=None):
            raise ValueError('top-secret-value')

        r = leaky('my-api-key', password='hunter2')
        self.execute_next()

        event = self.pull()[0]
        data = event.data
        blob = json.dumps(data)
        self.assertNotIn('my-api-key', blob)
        self.assertNotIn('hunter2', blob)
        self.assertNotIn('top-secret-value', blob)
        self.assertNotIn('Traceback', blob)
        self.assertNotIn('args', data)
        self.assertNotIn('kwargs', data)
        # Only the exception type is recorded by default.
        self.assertEqual(data['error'], 'ValueError')

    def test_build_error_summary_type_only(self):
        exc = ValueError('contains a secret')
        self.assertEqual(build_error_summary(exc), 'ValueError')
        self.assertIn('contains a secret',
                      build_error_summary(exc, include_message=True))

    def test_payload_size_bounded(self):
        payload = build_payload(
            COMPLETION_FAILED, 'task-id', 'module.' + 'x' * 5000, time.time(),
            error='E' * 5000, max_bytes=512)
        self.assertLessEqual(len(payload), 512)
        data = json.loads(payload)
        self.assertTrue(data.get('truncated'))
        self.assertEqual(data['version'], COMPLETION_EVENT_VERSION)

    def test_configured_payload_bound_enforced(self):
        huey = SqliteHuey(filename=self.db_file, utc=False,
                          completion_events=True,
                          completion_max_payload_bytes=300)

        # A long, automatically-generated task name would blow the default
        # bound if not trimmed.
        long_name = 'test_task_' + 'y' * 2000

        @huey.task(name=long_name)
        def long_task():
            return 1

        r = long_task()
        huey.execute(huey.dequeue())
        event = huey.pull_completion_events()[0]
        self.assertLessEqual(len(event.payload), 300)
        self.assertTrue(event.data.get('truncated'))

    def test_too_small_bound_rejected(self):
        with self.assertRaises(ConfigurationError):
            SqliteHuey(filename=self.db_file, utc=False,
                       completion_events=True,
                       completion_max_payload_bytes=10)


class TestLeaseAndAck(CompletionTestCase):
    def make_event(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        r = add(1, 2)
        self.execute_next()
        return r

    def test_ack_is_the_delete_boundary(self):
        self.make_event()
        event = self.pull()[0]
        self.assertEqual(self.huey.storage.completion_event_count(), 1)

        self.assertTrue(self.huey.ack_completion_event(event.event_id,
                                                       event.lease_token))
        # Acked events are retained for dedup/retention but never redelivered.
        self.assertEqual(self.huey.storage.completion_event_count(), 1)
        self.assertEqual(self.pull(), [])

    def test_duplicate_ack_is_idempotent(self):
        self.make_event()
        event = self.pull()[0]
        self.assertTrue(self.huey.ack_completion_event(event.event_id,
                                                       event.lease_token))
        self.assertFalse(self.huey.ack_completion_event(event.event_id,
                                                        event.lease_token))
        self.assertEqual(self.pull(), [])

    def test_wrong_token_cannot_ack(self):
        self.make_event()
        event = self.pull()[0]
        self.assertFalse(self.huey.ack_completion_event(event.event_id,
                                                        'bogus-token'))
        # Still owned by the original lease, so it is not redelivered.
        self.assertEqual(self.pull(), [])
        # Release the lease, then it becomes available again.
        self.huey.release_completion_event(event.event_id, event.lease_token)
        self.assertEqual(len(self.pull()), 1)

    def test_lease_takeover_and_late_ack(self):
        self.make_event()
        first = self.pull(lease_seconds=0.05)[0]
        time.sleep(0.1)  # Let the first lease expire.
        second = self.pull(lease_seconds=30)[0]
        self.assertEqual(first.event_id, second.event_id)
        self.assertNotEqual(first.lease_token, second.lease_token)

        # The stale receiver's late ack must not delete the row owned by the
        # second receiver.
        self.assertFalse(self.huey.ack_completion_event(first.event_id,
                                                        first.lease_token))
        self.assertEqual(self.huey.storage.completion_event_count(False), 1)

        # The current holder can ack normally.
        self.assertTrue(self.huey.ack_completion_event(second.event_id,
                                                       second.lease_token))
        self.assertEqual(self.pull(), [])

    def test_release_makes_event_available(self):
        self.make_event()
        event = self.pull()[0]
        self.assertTrue(self.huey.release_completion_event(event.event_id,
                                                           event.lease_token))
        self.assertEqual(len(self.pull()), 1)

    def test_competing_receivers_never_share_a_claim(self):
        # Ten events, several receiver threads racing for small batches.
        @self.huey.task()
        def add(a, b):
            return a + b

        for i in range(10):
            add(i, i + 1)
        for _ in range(10):
            self.execute_next()

        claimed = {}
        lock = threading.Lock()

        def run(owner):
            while True:
                events = self.huey.pull_completion_events(
                    limit=2, lease_seconds=30, owner=owner)
                if not events:
                    return
                for event in events:
                    with lock:
                        # A given claim is exclusively owned.
                        self.assertNotIn(event.event_id, claimed)
                        claimed[event.event_id] = event.lease_token
                    self.huey.ack_completion_event(event.event_id,
                                                   event.lease_token)

        threads = [threading.Thread(target=run, args=('r%d' % i,))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(len(claimed), 10)
        self.assertEqual(self.pull(), [])


class TestAtomicityAndIsolation(CompletionTestCase):
    def test_result_and_event_consistent(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        r = add(1, 2)
        self.execute_next()
        # Result present and event present.
        self.assertEqual(r.get(), 3)
        self.assertEqual(len(self.pull()), 1)

    def test_no_event_without_result(self):
        """Simulate a process dying after the transaction commits."""
        from huey.completion import build_payload

        @self.huey.task()
        def add(a, b):
            return a + b

        task = add.s(1, 2)
        event_id = self.huey._completion_event_id(task, COMPLETION_SUCCEEDED)
        payload = build_payload(COMPLETION_SUCCEEDED, task.id, 'app.add',
                                time.time())
        meta = {'task_id': task.id, 'task_name': 'app.add',
                'status': COMPLETION_SUCCEEDED, 'final': True,
                'result_available': True}
        self.assertTrue(self.huey.storage.put_result_with_completion(
            task.id, self.huey.serializer.serialize(3), event_id, payload,
            time.time(), meta))

        # Re-applying (as a re-delivery/reconcile would) must not duplicate
        # the event nor overwrite the existing result.
        self.assertFalse(self.huey.storage.put_result_with_completion(
            task.id, self.huey.serializer.serialize(999), event_id, payload,
            time.time(), meta))
        self.assertEqual(self.huey.result(task.id, preserve=True), 3)
        self.assertEqual(self.huey.storage.completion_event_count(), 1)

    def test_result_and_event_share_a_transaction(self):
        """A failure mid-transaction leaves neither the result nor the event."""
        from huey.completion import build_payload

        @self.huey.task()
        def add(a, b):
            return a + b

        task = add.s(1, 2)
        event_id = self.huey._completion_event_id(task, COMPLETION_SUCCEEDED)
        payload = build_payload(COMPLETION_SUCCEEDED, task.id, 'app.add',
                                time.time())
        meta = {'task_id': task.id, 'task_name': 'app.add',
                'status': COMPLETION_SUCCEEDED, 'final': True,
                'result_available': True}

        storage = self.huey.storage
        original = storage.to_blob

        # Fail while writing the *result*, after the event insert in the same
        # transaction has already executed.
        storage.to_blob = lambda value: (_ for _ in ()).throw(
            RuntimeError('disk full'))
        try:
            with self.assertRaises(RuntimeError):
                storage.put_result_with_completion(
                    task.id, b'value', event_id, payload, time.time(), meta)
        finally:
            storage.to_blob = original

        # The transaction rolled back: no orphan event, no result.
        self.assertEqual(storage.completion_event_count(), 0)
        self.assertFalse(storage.has_data_for_key(task.id))

    def test_periodic_task_records_no_event(self):
        from huey.api import crontab

        @self.huey.periodic_task(crontab(minute='*'))
        def periodic():
            return 'ran'

        periodic()
        # Enqueue is normal, but periodic executions do not write results or
        # completion events (they would accumulate forever).
        self.execute_next()
        self.assertEqual(self.huey.storage.completion_event_count(), 0)

    def test_outlet_failure_does_not_rerun_task_or_block(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        calls = []
        original = self.huey.storage.put_result_with_completion

        def broken(*args, **kwargs):
            calls.append(1)
            raise RuntimeError('outlet down')

        self.huey.storage.put_result_with_completion = broken
        try:
            r = add(1, 2)
            # Execution succeeds; the result still lands (the fallback write),
            # and no exception escapes to the worker.
            self.assertEqual(self.execute_next(), 3)
        finally:
            self.huey.storage.put_result_with_completion = original

        self.assertEqual(len(calls), 1)
        # No event was recorded by the failing outlet...
        self.assertEqual(self.huey.storage.completion_event_count(), 0)
        # ...but reconcile can repair it from the stored result without
        # re-executing the task.  (Read the result non-destructively so the
        # reconcile pass can still see it.)
        self.assertEqual(r.get(preserve=True), 3)
        self.assertEqual(self.huey.reconcile_completion_events(), 1)
        events = self.pull()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, COMPLETION_SUCCEEDED)
        self.assertEqual(r.get(), 3)

    def test_outlet_failure_does_not_block_consumer(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        storage = self.huey.storage
        originals = (storage.put_result_with_completion,
                     storage.put_result_with_pending_completion)
        calls = []

        def broken(*args, **kwargs):
            calls.append(1)
            raise RuntimeError('outlet down')

        storage.put_result_with_completion = broken
        storage.put_result_with_pending_completion = broken
        try:
            first = add(1, 2)
            # The worker's execute() returns normally even with a broken
            # outlet, so it can move to the next task immediately.
            self.assertEqual(self.execute_next(), 3)
            second = add(3, 4)
            self.assertEqual(self.execute_next(), 7)
        finally:
            (storage.put_result_with_completion,
             storage.put_result_with_pending_completion) = originals

        # Primary + repair attempted once per task; both failed.
        self.assertEqual(len(calls), 4)
        # Both business functions ran exactly once, and both results exist.
        self.assertEqual(first.get(preserve=True), 3)
        self.assertEqual(second.get(preserve=True), 7)

    def test_reconcile_failed_result(self):
        @self.huey.task()
        def bad():
            raise ValueError('boom')

        original = self.huey.storage.put_result_with_completion
        self.huey.storage.put_result_with_completion = (
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('down')))
        try:
            r = bad()
            self.assertIsNone(self.execute_next())
        finally:
            self.huey.storage.put_result_with_completion = original

        self.assertEqual(self.huey.reconcile_completion_events(), 1)
        events = self.pull()
        self.assertEqual(self.statuses(events), [COMPLETION_FAILED])
        self.assertTrue(events[0].final)

    def test_reconciliation_ignores_non_result_keys(self):
        """Locks, rate-limits, revocations and chords share the kv table."""
        @self.huey.task()
        def add(a, b):
            return a + b

        # Create foreground noise that lives in the kv table but is not a task
        # result: a lock, a rate-limit window and a revocation.
        lock = self.huey.lock_task('noise')
        lock.acquire()
        rl = self.huey.rate_limit('noise', 5, 60)
        rl.acquire()
        add.revoke(revoke_once=True)

        # A normal task + event.
        r = add(1, 2)
        self.execute_next()
        self.assertEqual(self.huey.storage.completion_event_count(False), 1)

        # Reconciliation must not invent events for the non-result keys.
        before = self.huey.storage.completion_event_count()
        self.assertEqual(self.huey.reconcile_completion_events(), 0)
        self.assertEqual(self.huey.storage.completion_event_count(), before)

    def test_reconcile_repairs_pending_when_event_write_also_failed(self):
        """Both the atomic write and the pending write fail, but the result
        survives; reconcile still backfills from the UUID-keyed result."""

        @self.huey.task()
        def add(a, b):
            return a + b

        storage = self.huey.storage
        originals = (storage.put_result_with_completion,
                     storage.put_result_with_pending_completion)

        def broken(*a, **k):
            raise RuntimeError('outlet down')

        storage.put_result_with_completion = broken
        storage.put_result_with_pending_completion = broken
        try:
            r = add(1, 2)
            self.assertEqual(self.execute_next(), 3)
        finally:
            (storage.put_result_with_completion,
             storage.put_result_with_pending_completion) = originals

        self.assertEqual(storage.pending_completion_count(), 0)
        self.assertEqual(storage.completion_event_count(), 0)
        self.assertEqual(self.huey.reconcile_completion_events(), 1)
        events = self.pull()
        self.assertEqual([e.status for e in events], [COMPLETION_SUCCEEDED])
        self.assertEqual(events[0].task_id, r.id)

    def test_reconciliation_does_not_mark_retrying_as_failed(self):
        # A retrying attempt leaves an intermediate Error result when
        # store_intermediate_errors is on.  If no pending row exists (both
        # outlet writes failed), the last-resort scan must not report that
        # intermediate error as a terminal failure.
        @self.huey.task(retries=1)
        def flaky():
            raise ValueError('boom')

        storage = self.huey.storage
        originals = (storage.put_result_with_completion,
                     storage.put_result_with_pending_completion)

        def broken(*a, **k):
            raise RuntimeError('down')

        storage.put_result_with_completion = broken
        storage.put_result_with_pending_completion = broken
        try:
            flaky()
            self.execute_next()  # Non-final attempt; error is stored.
        finally:
            (storage.put_result_with_completion,
             storage.put_result_with_pending_completion) = originals

        self.assertEqual(storage.result_store_size(), 1)
        # The stored error has retries remaining, so no terminal event.
        self.assertEqual(self.huey.reconcile_completion_events(), 0)
        self.assertEqual(storage.completion_event_count(), 0)

    def test_pull_auto_materializes_pending(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        storage = self.huey.storage
        original = storage.put_result_with_completion
        storage.put_result_with_completion = (
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('down')))
        try:
            add(1, 2)
            self.assertEqual(self.execute_next(), 3)
        finally:
            storage.put_result_with_completion = original

        self.assertEqual(storage.pending_completion_count(), 1)
        self.assertEqual(storage.completion_event_count(), 0)
        # A receiver pull repairs the pending event automatically.
        events = self.pull()
        self.assertEqual([e.status for e in events], [COMPLETION_SUCCEEDED])
        self.assertEqual(storage.pending_completion_count(), 0)

    def test_flush_clears_outbox_and_pending(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        add(1, 2)
        self.execute_next()
        self.assertEqual(self.huey.storage.completion_event_count(), 1)
        self.huey.flush()
        self.assertEqual(self.huey.storage.completion_event_count(), 0)

    def test_multi_queue_isolation(self):
        other = self.other_huey('other-queue')

        @self.huey.task()
        def a():
            return 'a'

        @other.task()
        def b():
            return 'b'

        a()
        self.execute_next()
        b()
        task = other.dequeue()
        other.execute(task)

        mine = self.pull()
        theirs = other.pull_completion_events()
        self.assertEqual(len(mine), 1)
        self.assertEqual(len(theirs), 1)
        self.assertEqual(mine[0].queue, 'huey')
        self.assertEqual(theirs[0].queue, 'other-queue')

        # Purging one queue does not touch the other (both must be acked,
        # since purge only removes acknowledged rows).
        self.assertTrue(other.ack_completion_event(theirs[0].event_id,
                                                   theirs[0].lease_token))
        self.assertTrue(self.huey.ack_completion_event(mine[0].event_id,
                                                       mine[0].lease_token))
        other.purge_completion_events(0, now=theirs[0].occurred_at + 3600)
        self.assertEqual(other.storage.completion_event_count(), 0)
        self.assertEqual(self.huey.storage.completion_event_count(), 1)
        self.huey.purge_completion_events(0, now=mine[0].occurred_at + 3600)
        self.assertEqual(self.huey.storage.completion_event_count(), 0)


class TestRetention(CompletionTestCase):
    def test_purge_only_acked_and_expired(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        add(1, 2)
        self.execute_next()
        add(3, 4)
        self.execute_next()

        acked = self.pull(limit=1)[0]
        pending = self.pull(limit=1)[0]
        self.huey.ack_completion_event(acked.event_id, acked.lease_token)

        # Release the pending lease so the checks below are about retention,
        # not about the lease.
        self.huey.release_completion_event(pending.event_id,
                                           pending.lease_token)

        now = time.time()
        # Nothing is old enough yet.
        self.assertEqual(self.huey.purge_completion_events(
            3600, now=now), 0)
        # Past the retention window: only the acknowledged row is eligible;
        # the never-acked row survives.
        deleted = self.huey.purge_completion_events(1, now=now + 10)
        self.assertEqual(deleted, 1)
        self.assertEqual(self.huey.storage.completion_event_count(), 1)
        # The surviving event is the pending one (redeliverable).
        redelivered = self.pull()
        self.assertEqual([e.event_id for e in redelivered],
                         [pending.event_id])

    def test_purge_never_deletes_unacked(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        add(1, 2)
        self.execute_next()
        far_future = time.time() + 10 ** 9
        self.assertEqual(self.huey.purge_completion_events(0,
                                                           now=far_future), 0)
        self.assertEqual(self.huey.storage.completion_event_count(), 1)


class TestLocalReceiver(CompletionTestCase):
    def test_local_receiver_process_once(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        for i in range(3):
            add(i, 0)
            self.execute_next()

        seen = []
        receiver = LocalCompletionReceiver(self.huey, limit=10,
                                           owner='unit-test')
        processed = receiver.process_once(lambda event: seen.append(
            event.event_id))
        self.assertEqual(processed, 3)
        self.assertEqual(len(seen), 3)
        self.assertEqual(receiver.pull(), [])

    def test_local_receiver_handler_error_leaves_event(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        add(1, 2)
        self.execute_next()

        receiver = LocalCompletionReceiver(self.huey, lease_seconds=0.05)

        def fail_handler(event):
            raise RuntimeError('ticket system down')

        with self.assertRaises(RuntimeError):
            receiver.process_once(fail_handler)
        # Unacknowledged, so after the lease expires it is redelivered.
        time.sleep(0.1)
        seen = []
        receiver.process_once(lambda event: seen.append(event.event_id))
        self.assertEqual(len(seen), 1)


class TestHueyConstructor(CompletionTestCase):
    def test_completion_events_flag_on_storage(self):
        self.assertTrue(self.huey.storage.completion_events)
        self.assertTrue(self.huey.storage.supports_completion_events)

    def test_config_validation(self):
        with self.assertRaises(ConfigurationError):
            Huey(storage_class=type('NoEvents',
                                    (MemoryHuey.storage_class,),
                                    {'supports_completion_events': False}),
                 completion_events=True)


if __name__ == '__main__':
    unittest.main()
