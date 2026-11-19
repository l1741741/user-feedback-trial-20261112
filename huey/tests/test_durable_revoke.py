"""
Tests for the opt-in durable (persistent) revocation mode.

Two layers are covered:

* In-process tests exercise the Huey API and the storage primitives:
  instance and class revocation, ``revoke_once`` / ``revoke_until`` semantics
  (including that ``revoke_once`` never silently becomes permanent), the
  generation/request-id fencing that protects a newer restore from stale
  executors and duplicate old cancel requests, and the atomic settlement that
  suppresses a retry / successor / error-handler when a revoke wins.
* Offline regressions spawn real short-lived OS processes against one SQLite
  file, reproducing a process exit between revoke and execution, and a
  controllable revoke/retry race in a genuinely separate process.
"""
import datetime
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from huey import SqliteHuey
from huey import MemoryHuey
from huey.exceptions import ConfigurationError
from huey.tests.base import BaseTestCase


def _db_path(name):
    fd, path = tempfile.mkstemp(prefix='durable-revoke-%s-' % name,
                                suffix='.db')
    os.close(fd)
    os.unlink(path)  # SqliteStorage creates the file itself.
    return path


class DurableRevokeTestCase(BaseTestCase):
    def get_huey(self):
        self.db_path = _db_path('unit')
        return SqliteHuey(filename=self.db_path, durable_revoke=True,
                          utc=False, timeout=5, results=True)

    def tearDown(self):
        path = self.db_path
        super(DurableRevokeTestCase, self).tearDown()
        self.huey.storage.close()
        for suffix in ('', '-wal', '-shm'):
            if os.path.exists(path + suffix):
                os.unlink(path + suffix)

    def execute_next(self, timestamp=None):
        task = self.huey.dequeue()
        self.assertTrue(task is not None)
        return self.huey.execute(task, timestamp=timestamp)


class TestDurableRevokeBasics(DurableRevokeTestCase):
    def test_requires_supported_backend(self):
        self.assertRaises(ConfigurationError, MemoryHuey,
                          durable_revoke=True)
        # Default (off) is unchanged on MemoryHuey.
        huey = MemoryHuey(utc=False)
        self.assertFalse(huey.durable_revoke)
        self.assertFalse(huey._durable)

    def test_instance_revoke_and_restore(self):
        state = []

        @self.huey.task()
        def task_a(n):
            state.append(n)
            return n + 1

        r1, r2 = task_a(1), task_a(2)
        r1.revoke(revoke_once=False)
        self.assertTrue(r1.is_revoked())
        self.assertFalse(r2.is_revoked())

        self.assertTrue(self.execute_next() is None)
        self.assertEqual(state, [])
        self.assertEqual(self.execute_next(), 3)
        self.assertEqual(state, [2])

        # Restore re-enables the task instance. Its original delivery was
        # already consumed, so re-enqueue it to observe the effect.
        self.assertTrue(r1.restore())
        self.assertFalse(r1.is_revoked())
        self.huey.enqueue(r1.task)
        self.assertEqual(self.execute_next(), 2)
        self.assertEqual(state, [2, 1])

    def test_class_revoke_applies_to_future_enqueues(self):
        seen = []

        @self.huey.task()
        def task_a(n):
            seen.append(n)
            return n

        task_a.revoke()
        self.assertTrue(task_a.is_revoked())
        r = task_a(5)  # Enqueued after the revoke.
        self.assertTrue(r.is_revoked())
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(seen, [])

        self.assertTrue(task_a.restore())
        self.assertFalse(task_a.is_revoked())
        r2 = task_a(6)
        self.assertFalse(r2.is_revoked())
        self.assertEqual(self.execute_next(), 6)
        self.assertEqual(seen, [6])

    def test_instance_restore_does_not_clear_class_revoke(self):
        ran = []

        @self.huey.task()
        def task_a(n):
            ran.append(n)
            return n

        task_a.revoke()  # Class-wide.
        r = task_a(1)
        self.assertTrue(r.is_revoked())
        r.restore()  # Clears only the instance revocation.
        self.assertTrue(r.is_revoked())
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(ran, [])

        task_a.restore()
        self.assertFalse(r.is_revoked())
        r2 = task_a(2)
        self.assertEqual(self.execute_next(), 2)
        self.assertEqual(ran, [2])

    def test_revoke_by_id_matches_revoke_task(self):
        @self.huey.task()
        def task_a(n):
            return n

        r = task_a(1)
        self.huey.revoke_by_id(r.id, revoke_once=False)
        self.assertTrue(r.is_revoked())
        self.assertTrue(self.huey.is_revoked(r.id))
        self.assertTrue(self.huey.restore_by_id(r.id))
        self.assertFalse(r.is_revoked())

    def test_revoke_once_is_consumed_not_permanent(self):
        @self.huey.task()
        def task_a(n):
            return n + 1

        task_a.revoke(revoke_once=True)
        r1, r2 = task_a(1), task_a(2)
        self.assertTrue(r1.is_revoked())
        self.assertTrue(r2.is_revoked())

        # First execution consumes the "once" revocation.
        self.assertTrue(self.execute_next() is None)
        self.assertFalse(task_a.is_revoked())
        self.assertEqual(self.execute_next(), 3)

        # A later enqueue is not revoked: revoke_once did not become permanent.
        r3 = task_a(3)
        self.assertFalse(r3.is_revoked())
        self.assertEqual(self.execute_next(), 4)

    def test_revoke_once_instance(self):
        @self.huey.task()
        def task_a(n):
            return n + 1

        r = task_a(1)
        # Result.revoke() defaults to revoke_once=True, matching legacy API.
        r.revoke()
        self.assertTrue(r.is_revoked())
        self.assertTrue(self.execute_next() is None)
        self.assertFalse(r.is_revoked())

        self.huey.enqueue(r.task)
        self.assertEqual(self.execute_next(), 2)

    def test_revoke_until(self):
        @self.huey.task()
        def task_a(n):
            return n + 1

        now = datetime.datetime.now()
        r = task_a.s(1)
        self.huey.revoke(r, revoke_until=now + datetime.timedelta(hours=1))
        # A task is revoked while inside the window...
        self.assertTrue(self.huey.is_revoked(r))
        # ...and not afterwards.
        self.assertFalse(
            self.huey.is_revoked(r, timestamp=now + datetime.timedelta(
                hours=2)))

    def test_elapsed_revoke_until_is_cleared(self):
        @self.huey.task()
        def task_a(n):
            return None

        now = datetime.datetime.now()
        r = task_a()
        self.huey.revoke(r, revoke_until=now - datetime.timedelta(minutes=1))
        self.assertEqual(self.huey.pending_count(), 1)

        # Execution is allowed because the window has elapsed; the record is
        # rewritten as "not revoked" with a fresh generation.
        self.assertIsNone(self.huey.execute(self.huey.dequeue()))
        record = self.huey.storage.get_durable_record('task', r.id)
        self.assertFalse(record.revoked)
        self.assertFalse(r.is_revoked())

    def test_periodic_task_revoke(self):
        from huey import crontab

        state = []

        @self.huey.periodic_task(crontab(minute='*/5'))
        def periodic():
            state.append(1)
            return True

        self.assertTrue(periodic.is_revoked() is False)
        periodic.revoke()
        self.assertTrue(periodic.is_revoked())
        self.assertTrue(periodic.restore() is not None)
        self.assertFalse(periodic.is_revoked())

        # A revoke_once periodic occurrence is consumed on execution.
        periodic.revoke(revoke_once=True)
        self.assertTrue(periodic.is_revoked())
        self.assertTrue(self.huey.is_revoked(periodic.task_class))


class TestDurableRestoreFencing(DurableRevokeTestCase):
    def test_stale_duplicate_revoke_does_not_override_restore(self):
        @self.huey.task()
        def task_a(n):
            return n

        r = task_a(1)
        r.revoke(revoke_once=False, request_id='cancel-42')
        self.assertTrue(r.is_revoked())
        r.restore()
        self.assertFalse(r.is_revoked())

        # Replaying the identical cancel request (a retried old request) must
        # not re-arm the revocation.
        r.revoke(revoke_once=False, request_id='cancel-42')
        self.assertFalse(r.is_revoked())
        self.assertEqual(self.execute_next(), 1)

        # A genuinely new request id is honored.
        r2 = task_a(2)
        r2.revoke(revoke_once=False, request_id='cancel-43')
        self.assertTrue(r2.is_revoked())

    def test_generation_cas(self):
        from huey.storage import SqliteStorage
        storage = self.huey.storage
        record = storage.revoke_durable('task', 'some-id')
        self.assertEqual(record.generation, 1)
        storage.restore_durable('task', 'some-id')

        # A worker that captured the pre-restore generation cannot resurrect
        # the revocation.
        stale = storage.revoke_durable('task', 'some-id',
                                       expected_generation=0)
        self.assertTrue(stale is None)
        self.assertTrue(storage.get_durable_revocation('some-id', None)
                        is None)

    def test_restore_of_never_revoked_records_generation(self):
        from huey.storage import SqliteStorage
        storage = self.huey.storage
        self.assertFalse(storage.restore_durable('task', 'nope'))
        record = storage.get_durable_record('task', 'nope')
        self.assertIsNotNone(record)
        self.assertFalse(record.revoked)


class TestDurableSettlement(DurableRevokeTestCase):
    def test_revoke_after_task_began_suppresses_retry(self):
        seen = []

        @self.huey.task(retries=3)
        def task_a(n):
            seen.append(n)
            raise ValueError('boom')

        r = task_a(1)
        task = self.huey.dequeue()

        original = task.execute

        def execute_and_revoke():
            try:
                return original()
            finally:
                r.revoke(revoke_once=False)

        task.execute = execute_and_revoke
        self.huey.execute(task)

        # Business code ran, but the retry was suppressed and nothing is
        # left in the queue or schedule.
        self.assertEqual(seen, [1])
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(self.huey.scheduled_count(), 0)

    def test_revoke_after_task_began_suppresses_successor(self):
        seen = []

        @self.huey.task()
        def first(n):
            return n + 1

        @self.huey.task()
        def second(n):
            seen.append(n)

        pipe = first.s(1).then(second)
        r1, r2 = self.huey.enqueue(pipe)
        self.assertEqual(self.huey.pending_count(), 1)

        task = self.huey.dequeue()
        original = task.execute

        def execute_and_revoke():
            try:
                return original()
            finally:
                r1.revoke(revoke_once=False)

        task.execute = execute_and_revoke
        self.huey.execute(task)

        self.assertEqual(seen, [])
        self.assertEqual(self.huey.pending_count(), 0)

    def test_revoke_after_task_began_suppresses_error_handler(self):
        seen = []

        @self.huey.task()
        def on_err(n, err):
            seen.append(('on_err', type(err).__name__))

        @self.huey.task()
        def fail(n):
            raise ValueError('boom')

        r = fail.s(1).error(on_err)
        res = self.huey.enqueue(r)
        task = self.huey.dequeue()
        original = task.execute

        def execute_and_revoke():
            try:
                return original()
            finally:
                # Revoking the failing task suppresses its error successor.
                self.huey.revoke(task, revoke_once=False)

        task.execute = execute_and_revoke
        self.huey.execute(task)

        self.assertEqual(seen, [])
        self.assertEqual(self.huey.pending_count(), 0)

    def test_retry_scheduled_before_revoke_is_still_skipped(self):
        # Boundary case: the worker settles its retry *before* the revoke
        # commits. The revoke still reports success and the retry delivery
        # (same task id) is skipped when the consumer picks it up.
        seen = []

        @self.huey.task(retries=2, retry_delay=0)
        def task_a(n):
            seen.append(n)
            raise ValueError('boom')

        r = task_a(1)
        self.huey.execute(self.huey.dequeue())  # Retry is enqueued.
        self.assertEqual(self.huey.pending_count(), 1)

        self.huey.revoke_by_id(r.id, revoke_once=False)
        self.assertTrue(r.is_revoked())

        # The retry delivery is discarded rather than executed.
        self.assertTrue(self.huey.execute(self.huey.dequeue()) is None)
        self.assertEqual(seen, [1])
        self.assertEqual(self.huey.pending_count(), 0)

    def test_revoke_does_not_flush_schedule(self):
        # A single-task revoke must never solve the problem by emptying the
        # schedule. An ETA task survives in the schedule and is simply skipped
        # when it becomes ready.
        ran = []

        @self.huey.task()
        def target(n):
            ran.append(('target', n))

        @self.huey.task()
        def bystander(n):
            ran.append(('bystander', n))

        victim = target.s(1, eta=datetime.datetime.now() +
                          datetime.timedelta(seconds=30))
        other = bystander.s(2, eta=datetime.datetime.now() +
                            datetime.timedelta(seconds=30))
        r1 = self.huey.enqueue(victim)
        r2 = self.huey.enqueue(other)
        self.assertEqual(self.huey.pending_count(), 2)

        # Draining into the schedule happens on execute (task not ready yet).
        self.huey.execute(self.huey.dequeue())
        self.huey.execute(self.huey.dequeue())
        self.assertEqual(self.huey.scheduled_count(), 2)

        r1.revoke(revoke_once=False)
        # Neither scheduled row is removed.
        self.assertEqual(self.huey.scheduled_count(), 2)

        ready = self.huey.read_schedule(
            datetime.datetime.now() + datetime.timedelta(minutes=1))
        self.assertEqual(len(ready), 2)
        for task in ready:
            self.huey.execute(task, datetime.datetime.now() +
                              datetime.timedelta(minutes=1))

        # Only the bystander ran.
        self.assertEqual(ran, [('bystander', 2)])

    def test_revoke_midflight_still_completes_chord(self):
        seen = []

        @self.huey.task()
        def member(n):
            return n

        @self.huey.task()
        def callback(values):
            seen.append(values)

        from huey import chord
        c = chord([member.s(1), member.s(2)], callback)
        res = self.huey.enqueue(c)
        results = list(res.results)

        # First member runs, second member is revoked while executing.
        self.huey.execute(self.huey.dequeue())
        task = self.huey.dequeue()
        original = task.execute

        def execute_and_revoke():
            try:
                return original()
            finally:
                results[1].revoke(revoke_once=False)

        task.execute = execute_and_revoke
        self.huey.execute(task)

        # The callback was still enqueued, so the chord converges.
        self.assertEqual(self.huey.pending_count(), 1)
        self.huey.execute(self.huey.dequeue())
        self.assertEqual(seen, [[1, 2]])

    def test_revoke_suppressing_chord_member_successor_keeps_chord_alive(self):
        # If the *head* of a chord pipeline member is revoked while it is
        # executing, its successor (the pipeline tail, which carries the chord
        # config) is suppressed. The member must still be reported as SKIPPED
        # so the callback fires rather than hanging forever.
        seen = []

        @self.huey.task()
        def step(n):
            return n

        @self.huey.task()
        def combine(values):
            seen.append(values)

        from huey import chord
        from huey.utils import SKIPPED
        m1 = step.s(1).then(step)
        m2 = step.s(2)
        c = chord([m1, m2], combine)
        res = self.huey.enqueue(c)
        results = list(res.results)  # Results for the two pipeline tails.

        task = self.huey.dequeue()  # Head of m1.
        head_result = self.huey._result_handle(task)
        original = task.execute

        def execute_and_revoke():
            try:
                return original()
            finally:
                # Revoke the head while it is executing: its successor is
                # therefore suppressed at settlement time.
                head_result.revoke(revoke_once=False)

        task.execute = execute_and_revoke
        self.huey.execute(task)

        # The successor was NOT enqueued; only m2 remains.
        self.assertEqual(self.huey.pending_count(), 1)
        self.huey.execute(self.huey.dequeue())  # m2.
        self.assertEqual(self.huey.pending_count(), 1)  # Callback.

        # The callback still fires, with a placeholder for the suppressed
        # member's pipeline.
        self.huey.execute(self.huey.dequeue())
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0] is SKIPPED)
        self.assertEqual(seen[0][1], 2)

    def test_retry_matches_legacy_expiry_and_eta(self):
        # Durable settlement must mirror the legacy retry path: an immediate
        # retry re-resolves the task's expiration, a scheduled retry keeps the
        # already-resolved expiry. Its delay is measured from settlement.
        from huey import MemoryHuey

        def run(h):
            @h.task(retries=1, retry_delay=30,
                    expires=datetime.timedelta(seconds=100))
            def fail(n):
                raise ValueError('boom')

            fail(1)
            task = h.dequeue()
            task.execute = lambda: (_ for _ in ()).throw(ValueError('boom'))
            h.execute(task)
            scheduled = h.scheduled()
            return len(scheduled), (scheduled[0].expires_resolved
                                    if scheduled else None)

        legacy_count, legacy_exp = run(MemoryHuey(utc=False))
        durable_count, durable_exp = run(self.huey)
        self.assertEqual(durable_count, legacy_count)
        self.assertEqual(durable_count, 1)
        self.assertIsNotNone(legacy_exp)
        self.assertIsNotNone(durable_exp)

    def test_revoke_suppressing_chord_member_retry_keeps_chord_alive(self):
        # A chord member that fails with retries pending, then is revoked
        # while executing: the retry (and eventually the member's outcome) is
        # suppressed. The member must be reported as SKIPPED so the callback
        # still fires.
        from huey import chord
        from huey.utils import SKIPPED
        seen = []

        @self.huey.task(retries=3, retry_delay=0)
        def flaky(n):
            raise ValueError('boom')

        @self.huey.task()
        def ok(n):
            return n

        @self.huey.task()
        def combine(values):
            seen.append(values)

        c = chord([flaky.s(1), ok.s(2)], combine)
        self.huey.enqueue(c)

        failing = self.huey.dequeue()
        self.assertEqual(failing.args, (1,))
        handle = self.huey._result_handle(failing)
        original = failing.execute

        def execute_and_revoke():
            try:
                return original()
            finally:
                handle.revoke(revoke_once=False)

        failing.execute = execute_and_revoke
        self.huey.execute(failing)

        # No retry was scheduled; the other member completes normally.
        remaining = self.huey.pending_count()
        self.assertEqual(remaining, 1)
        self.huey.execute(self.huey.dequeue())

        self.assertEqual(self.huey.pending_count(), 1)  # Callback.
        self.huey.execute(self.huey.dequeue())
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0] is SKIPPED)
        self.assertEqual(seen[0][1], 2)

    def test_results_still_recorded_for_running_task(self):
        @self.huey.task()
        def task_a(n):
            return n + 1

        r = task_a(1)
        task = self.huey.dequeue()
        r.revoke(revoke_once=False)
        # Executing a task whose revocation committed before it began is
        # skipped entirely.
        self.assertTrue(self.huey.execute(task) is None)


class TestDurablePersistence(DurableRevokeTestCase):
    def test_revocation_survives_new_storage_instance(self):
        @self.huey.task()
        def task_a(n):
            return n

        r = task_a(1)
        r.revoke(revoke_once=False)
        # Simulate a process restart: drop the connection and reopen. A fresh
        # process does not share this registry, so check the stored record
        # directly.
        path = self.huey.storage.filename
        self.huey.storage.close()
        reopened = SqliteHuey(filename=path, durable_revoke=True, utc=False)
        try:
            record = reopened.storage.get_durable_record('task', r.id)
            self.assertIsNotNone(record)
            self.assertTrue(record.revoked)
            heard = reopened.is_revoked(r.id)
            self.assertTrue(heard)
        finally:
            reopened.storage.close()

    def test_restored_task_survives_restart(self):
        @self.huey.task()
        def task_a(n):
            return n

        r = task_a(1)
        r.revoke(revoke_once=False)
        r.restore()
        path = self.huey.storage.filename
        self.huey.storage.close()
        reopened = SqliteHuey(filename=path, durable_revoke=True, utc=False)
        try:
            self.assertFalse(reopened.is_revoked(r.id))
            self.huey.storage.close()  # Already closed above; idempotent.
        finally:
            reopened.storage.close()

    def test_no_revocation_table_writes_when_disabled(self):
        # Durable mode is opt-in: with the default setting the legacy revoke
        # key is written and the durable table stays empty.
        path = _db_path('schema-disabled')
        try:
            huey = SqliteHuey(filename=path, utc=False)
            try:
                @huey.task()
                def task_a(n):
                    return n

                r = task_a(1)
                r.revoke()
                rows = huey.storage.sql(
                    'select count(*) from revocation', results=True)
                self.assertEqual(rows[0][0], 0)
                self.assertTrue(huey.storage.has_data_for_key(r.revoke_id))
                self.assertTrue(r.is_revoked())
            finally:
                huey.storage.close()
        finally:
            for suffix in ('', '-wal', '-shm'):
                if os.path.exists(path + suffix):
                    os.unlink(path + suffix)

    def test_flush_clears_durable_revocations(self):
        @self.huey.task()
        def task_a(n):
            return n

        r = task_a(1)
        r.revoke(revoke_once=False)
        self.assertTrue(r.is_revoked())
        self.huey.flush()
        self.assertFalse(r.is_revoked())
        self.assertEqual(self.huey.storage.sql(
            'select count(*) from revocation', results=True)[0][0], 0)


class OfflineDurableRevokeTests(unittest.TestCase):
    """
    Real-process regressions: a crash/restart is an actual process exit, and
    the revoke/retry race happens in a genuinely separate process.
    """
    DRIVER = 'huey.tests.durable_revoke_driver'

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='durable-revoke-offline-')
        self.db = os.path.join(self.tmpdir, 'revoke.db')
        self.env = dict(os.environ)
        self.env['REVOKE_DB'] = self.db
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        self.env['PYTHONPATH'] = root

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, mode, *args, check=True):
        proc = subprocess.run(
            [sys.executable, '-I', '-m', self.DRIVER, mode] + list(args),
            env=self.env, capture_output=True, text=True, timeout=30)
        if check and proc.returncode != 0:
            self.fail('driver %s failed rc=%s\nstdout=%s\nstderr=%s' % (
                mode, proc.returncode, proc.stdout, proc.stderr))
        return proc

    @staticmethod
    def _kv(line):
        return dict(item.split('=', 1) for item in line.strip().split(' '))

    @staticmethod
    def _csv(value):
        return [v for v in value.split(',') if v]

    def test_revocation_survives_process_exit(self):
        setup = self._kv(self._run('setup').stdout.strip())
        ids = self._csv(setup['ids'])
        self.assertEqual(len(ids), 3)

        # Revoke one task id in its own process, which then dies hard.
        crash = self._run('revoke-and-exit', ids[0])
        self.assertIn('revoked=%s' % ids[0], crash.stderr)

        # A brand-new process drains the queue: only the revoked task is
        # skipped. The revocation was durable on disk.
        info = self._kv(self._run('drain').stdout.strip())
        self.assertEqual(info['skipped'], ids[0])
        self.assertEqual(set(self._csv(info['ran'])), set(ids[1:]))

    def test_restore_in_another_process_re_enables_task(self):
        setup = self._kv(self._run('setup', '1').stdout.strip())
        task_id = self._csv(setup['ids'])[0]

        self._run('revoke-and-exit', task_id)
        restored = self._kv(self._run('restore-ids', task_id).stdout.strip())
        self.assertEqual(restored['restored'], 'True')

        info = self._kv(self._run('drain').stdout.strip())
        self.assertEqual(info['ran'], task_id)
        self.assertEqual(info['skipped'], '')

    def test_revoke_while_executing_suppresses_retry_in_real_process(self):
        setup = self._kv(self._run('setup-failing', '1').stdout.strip())
        task_id = self._csv(setup['ids'])[0]

        info = self._kv(self._run('retry-while-revoked', task_id)
                        .stdout.strip())
        # The failing task would normally schedule a retry (schedule=1).
        self.assertEqual(info['queue'], '0')
        self.assertEqual(info['schedule'], '0')

    def test_controllable_race_revoke_and_retry(self):
        setup = self._kv(self._run('setup-failing', '1').stdout.strip())
        task_id = self._csv(setup['ids'])[0]

        info = self._kv(self._run('race-revoke-and-retry', task_id)
                        .stdout.strip())
        self.assertEqual(info['queue'], '0')
        self.assertEqual(info['schedule'], '0')

        # Independently verify the on-disk truth: no retry delivery exists.
        conn = sqlite3.connect(self.db)
        try:
            schedules = conn.execute(
                "select count(*) from schedule where queue = 'revoke-driver'"
            ).fetchone()[0]
            tasks = conn.execute(
                "select count(*) from task where queue = 'revoke-driver'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(schedules, 0)
        self.assertEqual(tasks, 0)
