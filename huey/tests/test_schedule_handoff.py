"""
Tests for the optional reliable schedule-to-queue handoff.

Two layers are covered:

* In-process unit tests exercise the storage primitives and the Huey-level
  handoff orchestration (claim/publish idempotency, partial failures,
  poison-task retention, legacy-database enablement, capability gating).
* Offline regression tests spawn real short-lived OS processes via
  ``huey.tests.handoff_driver`` to reproduce process exit/restart, dual
  schedulers sharing one database file, and partially-completed batches.
  Those cannot be simulated in-thread: nothing short of a real process
  boundary proves the handoff state survives an actual exit.
"""
import datetime
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

try:
    import cysqlite
except ImportError:
    cysqlite = None

from huey import SqliteHuey
from huey import MemoryHuey
from huey.exceptions import ConfigurationError
from huey.storage import BaseStorage
from huey.storage import SqliteStorage
from huey.tests.base import BaseTestCase


def _db_path(name):
    fd, path = tempfile.mkstemp(prefix='handoff-%s-' % name, suffix='.db')
    os.close(fd)
    os.unlink(path)  # SqliteStorage creates the file itself.
    return path


class ReliableHandoffStorageTests(BaseTestCase):
    def get_huey(self):
        return SqliteHuey(filename=_db_path('unit'), reliable_schedule=True,
                          utc=False, timeout=5)

    def setUp(self):
        super(ReliableHandoffStorageTests, self).setUp()
        self.s = self.huey.storage

    def tearDown(self):
        path = self.s.filename
        super(ReliableHandoffStorageTests, self).tearDown()
        for suffix in ('', '-wal', '-shm'):
            if os.path.exists(path + suffix):
                os.unlink(path + suffix)

    def test_handoff_table_created_only_when_enabled(self):
        rows = self.s.sql(
            "select name from sqlite_master where type='table' and "
            "name='schedule_handoff'", results=True)
        self.assertEqual(len(rows), 1)

        disabled = SqliteStorage(filename=_db_path('disabled'),
                                 reliable_schedule=False)
        try:
            rows = disabled.sql(
                "select name from sqlite_master where type='table' and "
                "name='schedule_handoff'", results=True)
            self.assertEqual(rows, [])
        finally:
            disabled.close()
            for suffix in ('', '-wal', '-shm'):
                if os.path.exists(disabled.filename + suffix):
                    os.unlink(disabled.filename + suffix)

    def test_claim_moves_due_rows_and_keeps_future_rows(self):
        now = datetime.datetime.now()
        past = now - datetime.timedelta(seconds=10)
        future = now + datetime.timedelta(hours=1)
        self.s.add_to_schedule(b'due-1', past)
        self.s.add_to_schedule(b'due-2', now)
        self.s.add_to_schedule(b'future', future)

        rows = self.s.claim_scheduled(now)
        self.assertEqual([data for _, data in rows], [b'due-1', b'due-2'])
        self.assertEqual(self.s.schedule_size(), 1)
        self.assertEqual(self.s.handoff_size(), 2)

        # A second claim finds no new due rows but still returns the parked
        # handoff rows, so an interrupted scheduler can always resume.
        again = self.s.claim_scheduled(now)
        self.assertEqual(sorted(data for _, data in again),
                         [b'due-1', b'due-2'])
        self.assertEqual(self.s.schedule_size(), 1)

    def test_publish_is_atomic_and_idempotent(self):
        now = datetime.datetime.now()
        self.s.add_to_schedule(b'x', now)
        handoff_id, data = self.s.claim_scheduled(now)[0]

        self.assertTrue(self.s.publish_scheduled(handoff_id, data, 3.0))
        self.assertEqual(self.s.handoff_size(), 0)
        self.assertEqual(self.s.queue_size(), 1)
        # Priority is carried into the task table.
        priority = self.s.sql('select priority from task',
                              results=True)[0][0]
        self.assertEqual(priority, 3.0)

        # Simulating "commit succeeded but the caller never got the answer":
        # retrying the same handoff id must not enqueue a second copy.
        self.assertFalse(self.s.publish_scheduled(handoff_id, data, 3.0))
        self.assertEqual(self.s.queue_size(), 1)

    def test_publish_rolls_back_when_handoff_row_missing(self):
        now = datetime.datetime.now()
        self.s.add_to_schedule(b'x', now)
        handoff_id, data = self.s.claim_scheduled(now)[0]
        # Another scheduler completes the handoff out from under us.
        self.assertTrue(self.s.publish_scheduled(handoff_id, data, 0.0))
        # This retry must neither insert a duplicate nor raise.
        self.assertFalse(self.s.publish_scheduled(handoff_id, data, 0.0))
        self.assertEqual(self.s.queue_size(), 1)

    def test_retry_scheduled_keeps_row_and_counts_attempts(self):
        now = datetime.datetime.now()
        self.s.add_to_schedule(b'x', now)
        handoff_id, _ = self.s.claim_scheduled(now)[0]
        self.assertTrue(self.s.retry_scheduled(handoff_id))
        self.assertTrue(self.s.retry_scheduled(handoff_id))
        attempts = self.s.sql(
            'select attempts from schedule_handoff where id = ?',
            (handoff_id,), results=True)[0][0]
        self.assertEqual(attempts, 2)
        # Row is still reclaimable.
        self.assertEqual(self.s.handoff_size(), 1)
        self.assertFalse(self.s.retry_scheduled(999999))

    def test_flush_schedule_clears_handoff_area(self):
        now = datetime.datetime.now()
        self.s.add_to_schedule(b'x', now)
        self.s.claim_scheduled(now)
        self.assertEqual(self.s.handoff_size(), 1)
        self.s.flush_schedule()
        self.assertEqual(self.s.handoff_size(), 0)
        self.assertEqual(self.s.schedule_size(), 0)

    def test_two_storages_one_database_never_double_publish(self):
        # Two scheduler processes against the same file, modeled with two
        # independent storage connections (the real process version lives in
        # the offline tests below).
        path = self.s.filename
        other = SqliteStorage(name=self.huey.name, filename=path,
                              reliable_schedule=True, timeout=5)
        try:
            now = datetime.datetime.now()
            self.s.add_to_schedule(b'shared', now)
            first = self.s.claim_scheduled(now)
            # The other connection sees nothing left in the schedule and only
            # the parked handoff row.
            second = other.claim_scheduled(now)
            self.assertEqual(len(first), 1)
            self.assertEqual([r[0] for r in second],
                             [r[0] for r in first])

            handoff_id, data = first[0]
            self.assertTrue(self.s.publish_scheduled(handoff_id, data, 0.0))
            # Loser of the race: its insert is rolled back.
            self.assertFalse(other.publish_scheduled(handoff_id, data, 0.0))
            self.assertEqual(self.s.queue_size(), 1)
        finally:
            other.close()


class ReliableHandoffHueyTests(BaseTestCase):
    def get_huey(self):
        return SqliteHuey(filename=_db_path('huey'), reliable_schedule=True,
                          utc=False, timeout=5)

    def tearDown(self):
        path = self.huey.storage.filename
        super(ReliableHandoffHueyTests, self).tearDown()
        for suffix in ('', '-wal', '-shm'):
            if os.path.exists(path + suffix):
                os.unlink(path + suffix)

    def _register_tasks(self):
        @self.huey.task()
        def task_a(n):
            return n + 1

        @self.huey.task(priority=7)
        def task_b(n):
            return n * 10

        return task_a, task_b

    def test_handoff_preserves_id_eta_priority_and_chain(self):
        task_a, task_b = self._register_tasks()
        now = datetime.datetime.now()

        head = task_a.s(1, id='head', eta=now - datetime.timedelta(
            seconds=5))
        tail = task_b.s(2, id='tail')
        head.then(tail)
        prio = task_b.s(3, id='prio', eta=now, priority=4)
        future = task_a.s(4, id='future',
                          eta=now + datetime.timedelta(hours=1))
        for task in (head, prio, future):
            self.huey.add_schedule(task)

        published = self.huey.handoff_scheduled(now)
        self.assertEqual(published, 2)
        self.assertEqual(self.huey.scheduled_count(), 1)  # future untouched
        self.assertEqual(self.huey.storage.handoff_size(), 0)

        # Higher priority dequeued first, with priority preserved.
        first = self.huey.dequeue()
        self.assertEqual(first.id, 'prio')
        self.assertEqual(first.priority, 4)

        second = self.huey.dequeue()
        self.assertEqual(second.id, 'head')
        # ETA and task-chain information survive the round-trip.
        self.assertEqual(second.eta, head.eta)
        self.assertIsNotNone(second.on_complete)
        self.assertEqual(second.on_complete.id, 'tail')

        self.assertIsNone(self.huey.dequeue())

    def test_handoff_is_idempotent_across_repeated_calls(self):
        task_a, _ = self._register_tasks()
        now = datetime.datetime.now()
        self.huey.add_schedule(task_a.s(1, id='once', eta=now))

        self.assertEqual(self.huey.handoff_scheduled(now), 1)
        # No replay of already-completed handoffs.
        self.assertEqual(self.huey.handoff_scheduled(now), 0)
        self.assertEqual(self.huey.pending_count(), 1)

    def test_one_bad_task_does_not_lose_or_replay_the_batch(self):
        task_a, _ = self._register_tasks()
        now = datetime.datetime.now()
        self.huey.add_schedule(task_a.s(1, id='good-1', eta=now))
        self.huey.add_schedule(task_a.s(2, id='good-2', eta=now))
        # A raw handoff row that no serializer can read.
        self.huey.storage.sql(
            'insert into schedule_handoff (queue, data, attempts, created_at)'
            ' values (?, ?, 0, ?)',
            (self.huey.name, b'\x80\x04not-a-real-message', time.time()),
            commit=True)

        published = self.huey.handoff_scheduled(now)
        self.assertEqual(published, 2)
        # The poison row is retained with an incremented attempt counter and
        # the good tasks are both exactly once in the queue.
        parked = self.huey.storage.sql(
            'select attempts from schedule_handoff where queue = ?',
            (self.huey.name,), results=True)
        self.assertEqual(parked, [(1,)])
        self.assertEqual(
            sorted(t.id for t in self.huey.pending()),
            ['good-1', 'good-2'])

        # Re-running keeps the poison row parked without duplicating work.
        self.assertEqual(self.huey.handoff_scheduled(now), 0)
        self.assertEqual(self.huey.storage.handoff_size(), 1)
        self.assertEqual(self.huey.pending_count(), 2)

    def test_publish_failure_before_commit_is_retried_safely(self):
        task_a, _ = self._register_tasks()
        now = datetime.datetime.now()
        self.huey.add_schedule(task_a.s(1, id='retry-me', eta=now))

        storage = self.huey.storage
        real_publish = storage.publish_scheduled
        calls = {'n': 0}

        def flaky_publish(handoff_id, data, priority=0.0):
            calls['n'] += 1
            if calls['n'] == 1:
                # Failure before anything durable: handoff row must survive.
                raise sqlite3.OperationalError('simulated pre-commit fault')
            return real_publish(handoff_id, data, priority)

        storage.publish_scheduled = flaky_publish
        try:
            published = self.huey.handoff_scheduled(now)
        finally:
            storage.publish_scheduled = real_publish

        # The first call parked the task (retry_scheduled), so a fresh
        # handoff run picks it up and publishes exactly once.
        self.assertEqual(published, 0)
        self.assertEqual(storage.handoff_size(), 1)
        self.assertEqual(self.huey.handoff_scheduled(now), 1)
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertEqual(self.huey.dequeue().id, 'retry-me')

    def test_legacy_database_picks_up_old_schedule_rows(self):
        # Build a database with the legacy schema and rows first, then open
        # it with the feature enabled. Existing schedule rows must remain
        # recoverable through the handoff.
        path = _db_path('legacy')
        legacy = SqliteHuey(filename=path, reliable_schedule=False, utc=False)

        @legacy.task()
        def legacy_task(n):
            return n

        now = datetime.datetime.now()
        legacy.add_schedule(legacy_task.s(1, id='legacy-1', eta=now))
        legacy.storage.close()

        upgraded = SqliteHuey(filename=path, reliable_schedule=True,
                              utc=False)
        # Share the registry: in production the same application code
        # registers tasks in every process; here the two huey instances are
        # constructed locally, so wire the registry explicitly.
        upgraded._registry = legacy._registry
        try:
            # Handoff table created lazily on open; the old schedule row is
            # still there.
            self.assertEqual(upgraded.scheduled_count(), 1)
            self.assertEqual(upgraded.handoff_scheduled(now), 1)
            self.assertEqual(upgraded.dequeue().id, 'legacy-1')
            self.assertEqual(upgraded.storage.handoff_size(), 0)
        finally:
            upgraded.storage.close()
            for suffix in ('', '-wal', '-shm'):
                if os.path.exists(path + suffix):
                    os.unlink(path + suffix)

    def test_real_scheduler_loop_drives_handoff(self):
        # The actual consumer.Scheduler integration point, in-process.
        from huey.consumer import Scheduler

        class StopFlag(object):
            def wait(self, timeout=None):
                return True

            def is_set(self):
                return False

        task_a, _ = self._register_tasks()
        now = datetime.datetime.now()
        self.huey.add_schedule(task_a.s(1, id='sched-1', eta=now))
        self.huey.add_schedule(task_a.s(2, id='sched-future',
                                        eta=now + datetime.timedelta(hours=1)))

        scheduler = Scheduler(self.huey, StopFlag(), interval=1,
                              periodic=False)
        scheduler.loop(now)
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertEqual(self.huey.dequeue().id, 'sched-1')
        self.assertEqual(self.huey.scheduled_count(), 1)
        self.assertEqual(self.huey.storage.handoff_size(), 0)


class ReliableHandoffCapabilityTests(BaseTestCase):
    def get_huey(self):
        return MemoryHuey(utc=False)

    def test_unsupported_backend_raises_at_construction(self):
        # The error must surface from configuration -- before any schedule
        # row is read or deleted.
        with self.assertRaises(ConfigurationError) as cm:
            MemoryHuey(reliable_schedule=True)
        self.assertIn('MemoryStorage', str(cm.exception))

    def test_explicitly_declined_custom_backend_raises(self):
        class CustomStorage(BaseStorage):
            supports_reliable_schedule = False

        with self.assertRaises(ConfigurationError):
            SqliteHuey(storage_class=CustomStorage,
                       reliable_schedule=True)

    def test_default_construction_keeps_legacy_behavior(self):
        # No flag, no handoff table, and the classic read_schedule path still
        # destructively returns due tasks.
        huey = SqliteHuey(filename=_db_path('default'), utc=False)
        try:
            self.assertFalse(huey.reliable_schedule)
            self.assertFalse(huey.storage.reliable_schedule)
            rows = huey.storage.sql(
                "select name from sqlite_master where type='table' and "
                "name='schedule_handoff'", results=True)
            self.assertEqual(rows, [])

            now = datetime.datetime.now()
            huey.storage.add_to_schedule(b'classic', now)
            self.assertEqual(huey.storage.read_schedule(now), [b'classic'])
            self.assertEqual(huey.storage.schedule_size(), 0)
        finally:
            huey.storage.close()
            for suffix in ('', '-wal', '-shm'):
                if os.path.exists(huey.storage.filename + suffix):
                    os.unlink(huey.storage.filename + suffix)


@unittest.skipIf(cysqlite is None, 'requires cysqlite')
class CySqliteReliableHandoffTests(ReliableHandoffStorageTests):
    def get_huey(self):
        from huey import CySqliteHuey
        return CySqliteHuey(filename=_db_path('cy'), reliable_schedule=True,
                            utc=False, timeout=5)


class OfflineHandoffRegressionTests(unittest.TestCase):
    """
    Real-process regressions. Each case spawns fresh interpreter processes
    against one shared database file, so a crash is an actual process exit
    and the two schedulers are genuinely independent OS processes.
    """
    DRIVER = 'huey.tests.handoff_driver'

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='handoff-offline-')
        self.db = os.path.join(self.tmpdir, 'handoff.db')
        self.env = dict(os.environ)
        self.env['HANDOFF_DB'] = self.db
        # -I ignores PYTHONPATH; the repository root is already on sys.path
        # for this interpreter, so pass it explicitly to the children.
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        self.env['PYTHONPATH'] = root

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, mode, check=True):
        proc = subprocess.run(
            [sys.executable, '-I', '-m', self.DRIVER, mode],
            env=self.env, capture_output=True, text=True, timeout=30)
        if check and proc.returncode != 0:
            self.fail('driver %s failed rc=%s\nstdout=%s\nstderr=%s' % (
                mode, proc.returncode, proc.stdout, proc.stderr))
        return proc

    @staticmethod
    def _kv(line):
        return dict(item.split('=', 1) for item in line.strip().split(' '))

    def test_process_exit_after_claim_resumes_without_loss(self):
        self._run('setup')
        # Hard exit between claim and publish: state exists only on disk.
        crash = self._run('claim-crash')
        self.assertIn('claimed=5', crash.stderr)

        # A brand-new process takes over.
        out = self._run('drain').stdout.strip()
        info = self._kv(out)
        self.assertEqual(info['queue'], '5')
        self.assertEqual(info['handoff'], '0')
        self.assertEqual(info['schedule'], '1')  # future task stays behind
        ids = info['ids'].split(',')
        self.assertEqual(sorted(ids),
                         ['due-0', 'due-1', 'due-2', 'due-3', 'due-4'])

    def test_process_exit_after_partial_publish_does_not_replay(self):
        self._run('setup')
        crash = self._run('publish-crash')
        self.assertIn('published_one=True', crash.stderr)

        out = self._run('drain').stdout.strip()
        info = self._kv(out)
        # First task already durable in the queue before the crash; the
        # restarted scheduler publishes only the remaining four.
        self.assertEqual(info['queue'], '5')
        self.assertEqual(info['handoff'], '0')
        ids = info['ids'].split(',')
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)  # no duplicate publication

    def test_two_independent_schedulers_publish_each_task_once(self):
        self._run('setup')
        self._run('claim-crash')  # Park all due rows in the handoff area.

        procs = [
            subprocess.Popen(
                [sys.executable, '-I', '-m', self.DRIVER, 'drain'],
                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            for _ in range(2)]
        outputs = [p.communicate(timeout=30) for p in procs]
        for stdout, stderr in outputs:
            self.assertEqual(stderr, '')
            info = self._kv(stdout.strip())
            self.assertEqual(info['handoff'], '0')

        # Verify the on-disk truth independently of either contender.
        import sqlite3 as _sql
        conn = _sql.connect(self.db)
        try:
            queue_count = conn.execute(
                "select count(*) from task where queue = 'handoff-driver'"
            ).fetchone()[0]
            handoff_count = conn.execute(
                "select count(*) from schedule_handoff "
                "where queue = 'handoff-driver'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(queue_count, 5)
        self.assertEqual(handoff_count, 0)

    def test_poison_batch_is_isolated_in_real_process(self):
        out = self._run('poison').stdout.strip()
        info = self._kv(out)
        self.assertEqual(info['published'], '2')
        self.assertEqual(info['handoff_attempts'], '[(1,)]')
        self.assertEqual(info['queue_ids'], 'good-1,good-2')

    def test_real_scheduler_loop_uses_reliable_handoff(self):
        self._run('setup')
        out = self._run('scheduler').stdout.strip()
        info = self._kv(out)
        self.assertEqual(info['queue'], '5')
        self.assertEqual(info['handoff'], '0')
        self.assertEqual(info['schedule'], '1')
