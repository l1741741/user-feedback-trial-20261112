import datetime
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from huey import MemoryHuey
from huey import SqliteHuey
from huey.api import Task
from huey.exceptions import RescheduleConflict
from huey.exceptions import RescheduleNotSupported
from huey.exceptions import TaskNotReschedulable
from huey.reschedule import RescheduleBatch
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase
from huey.tests.base import slow_test

try:
    from huey.contrib.sql_huey import SqlHuey
except ImportError:
    SqlHuey = None


def new_db(name):
    return os.path.join(TEST_TMP_DIR, name)


class RescheduleTests(object):
    """Shared assertions for any storage that supports reschedule().

    Subclasses provide get_huey().
    """
    clean_db = True

    def setUp(self):
        super(RescheduleTests, self).setUp()
        self.state = []

    def make_task(self, **settings):
        @self.huey.task(**settings)
        def record(n):
            self.state.append(n)
            return n
        return record

    def test_reschedule_queued_priority_preserves_id_and_result(self):
        task = self.make_task()
        result = task(1)
        out = self.huey.reschedule(result.id, priority=9)

        self.assertEqual(out.task_id, result.id)
        self.assertEqual(out.status, 'rescheduled')
        self.assertEqual(out.location, 'queue')
        self.assertEqual(out.priority, 9)

        pending = self.huey.pending()
        self.assertEqual([t.id for t in pending], [result.id])
        self.assertEqual(pending[0].priority, 9)

        # The task executes exactly once and the original Result still works.
        self.assertEqual(self.huey.execute(self.huey.dequeue()), 1)
        self.assertEqual(result.get(), 1)
        self.assertEqual(self.state, [1])
        self.assertEqual(self.huey.pending_count(), 0)
        self.assertEqual(self.huey.scheduled_count(), 0)

    def test_reschedule_queued_changes_nothing_else(self):
        seen = {}

        @self.huey.task(retries=3, retry_delay=5, retry_backoff=2,
                        name='kept-name', context=True)
        def keep(n, task=None):
            return n

        @self.huey.task()
        def chained(value):
            return value + 1

        task_obj = keep.s(1)
        task_obj.then(chained)
        # Enqueue manually so we can attach the pipeline and inspect it.
        results = self.huey.enqueue(task_obj)
        middle_result = list(results)[0]

        out = self.huey.reschedule(middle_result.id, delay=0)
        new_task = [t for t in self.huey.pending()
                    if t.id == middle_result.id][0]
        self.assertEqual(new_task.id, middle_result.id)
        self.assertEqual(new_task.args, (1,))
        self.assertEqual(new_task.retries, 3)
        self.assertEqual(new_task.retry_delay, 5)
        self.assertEqual(new_task.retry_backoff, 2)
        self.assertEqual(new_task.name, 'kept-name')
        self.assertIsNotNone(new_task.on_complete)
        self.assertEqual(out.task_id, middle_result.id)

    def test_reschedule_schedule_to_now_and_back(self):
        task = self.make_task()
        # schedule(delay=...) enqueues immediately; the worker moves it to the
        # schedule when it sees a future eta.
        result = task.schedule((5,), delay=3600)
        self.assertEqual(self.huey.execute(self.huey.dequeue()), None)
        self.assertEqual(self.huey.scheduled_count(), 1)
        self.assertEqual(self.huey.pending_count(), 0)

        out = self.huey.reschedule(result.id, eta=datetime.datetime.now())
        self.assertEqual(out.status, 'queued')
        self.assertEqual(out.location, 'queue')
        self.assertEqual(self.huey.scheduled_count(), 0)
        self.assertEqual(self.huey.pending_count(), 1)

        # Push it back into the future again.
        out = self.huey.reschedule(result.id, delay=600)
        self.assertEqual(out.status, 'scheduled')
        self.assertEqual(out.location, 'schedule')
        self.assertEqual(self.huey.scheduled_count(), 1)
        self.assertEqual(self.huey.pending_count(), 0)

        # Bring it back to now and run it; it executes once.
        self.huey.reschedule(result.id, delay=0)
        self.assertEqual(self.huey.execute(self.huey.dequeue()), 5)
        self.assertEqual(result.get(), 5)
        self.assertEqual(self.state, [5])

    def test_reschedule_priority_only_keeps_future_eta(self):
        # A task created via schedule(delay=...) waits in the *queue* carrying
        # a future eta. Changing only its priority must not make it run now.
        task = self.make_task()
        result = task.schedule((7,), delay=3600)
        self.assertEqual(self.huey.pending_count(), 1)
        original_eta = [t for t in self.huey.pending() if t.id == result.id][0].eta

        out = self.huey.reschedule(result.id, priority=3)
        self.assertEqual(out.location, 'queue')
        self.assertEqual(out.priority, 3)
        self.assertEqual(self.huey.pending_count(), 1)
        moved = [t for t in self.huey.pending() if t.id == result.id][0]
        self.assertEqual(moved.priority, 3)
        self.assertEqual(moved.eta, original_eta)

        # Executed before its eta, the worker moves it to the schedule rather
        # than running it.
        self.assertIsNone(self.huey.execute(self.huey.dequeue()))
        self.assertEqual(self.huey.scheduled_count(), 1)
        self.assertEqual(self.state, [])

    def test_reschedule_not_found(self):
        self.assertRaises(TaskNotReschedulable, self.huey.reschedule,
                          'does-not-exist', delay=0)

    def test_reschedule_claimed_task(self):
        task = self.make_task()
        result = task(1)
        claimed = self.huey.dequeue()
        self.assertEqual(claimed.id, result.id)
        try:
            with self.assertRaises(TaskNotReschedulable) as ctx:
                self.huey.reschedule(result.id, delay=0)
            self.assertEqual(ctx.exception.reason, 'not_found')
        finally:
            # The claimed delivery still runs exactly once.
            self.assertEqual(self.huey.execute(claimed), 1)
        self.assertEqual(self.state, [1])

    def test_reschedule_invalid_arguments_rejected_before_change(self):
        task = self.make_task()
        result = task(1)
        before = self.huey.pending()[0]

        bad = [dict(delay=-1), dict(priority=float('nan')),
               dict(priority=float('inf')), dict(priority='high'),
               dict(delay='soon'), dict(eta='not-a-datetime'),
               dict(eta=datetime.datetime.now(), delay=5)]
        for kwargs in bad:
            self.assertRaises(ValueError, self.huey.reschedule, result.id,
                              **kwargs)
            # Nothing changed.
            after = self.huey.pending()[0]
            self.assertEqual(after.id, before.id)
            self.assertEqual(after.priority, before.priority)
            self.assertEqual(after.eta, before.eta)

        # No arguments at all is also rejected.
        self.assertRaises(ValueError, self.huey.reschedule, result.id)

    def test_reschedule_operation_id_replay_and_conflict(self):
        task = self.make_task()
        r1 = task(1)
        r2 = task(2)

        out = self.huey.reschedule(r1.id, delay=60, operation_id='op-a')
        self.assertEqual(out.status, 'scheduled')
        self.assertEqual(self.huey.scheduled_count(), 1)

        # Replaying the same operation id with the same task returns the
        # recorded outcome and does not move the task again.
        replay = self.huey.reschedule(r1.id, delay=60, operation_id='op-a')
        self.assertEqual(replay.status, 'replayed')
        self.assertEqual(replay.location, out.location)
        self.assertEqual(self.huey.scheduled_count(), 1)

        # Reusing the operation id for a different task is an error.
        with self.assertRaises(RescheduleConflict) as ctx:
            self.huey.reschedule(r2.id, delay=60, operation_id='op-a')
        self.assertEqual(ctx.exception.task_id, r2.id)
        self.assertEqual(ctx.exception.existing_task_id, r1.id)
        # The second task was not modified.
        self.assertEqual(self.huey.pending_count(), 1)

    def test_reschedule_many_partial_failure(self):
        task = self.make_task()
        r1, r2 = task(1), task(2)

        batch = self.huey.reschedule_many(
            [r1.id, 'missing-id', r2.id], delay=60)
        self.assertIsInstance(batch, RescheduleBatch)
        self.assertEqual([r.task_id for r in batch.results], [r1.id, r2.id])
        self.assertEqual([e.task_id for e in batch.errors], ['missing-id'])
        self.assertFalse(batch.ok())
        # Both valid tasks were scheduled despite the failure in the middle.
        self.assertEqual(self.huey.scheduled_count(), 2)

    def test_reschedule_storage_write_failure_changes_nothing(self):
        # If the storage raises mid-write, the delivery must be untouched and
        # the operation id must not be recorded (so a retry can apply it).
        task = self.make_task()
        result = task(1)
        original = self.huey.storage.reschedule_task

        def boom(*args, **kwargs):
            raise RuntimeError('disk full')
        self.huey.storage.reschedule_task = boom
        try:
            self.assertRaises(RuntimeError, self.huey.reschedule, result.id,
                              delay=60, operation_id='op-fail')
        finally:
            self.huey.storage.reschedule_task = original

        self.assertEqual([t.id for t in self.huey.pending()], [result.id])
        self.assertEqual(self.huey.scheduled_count(), 0)
        # No operation recorded -> a retry applies the change for real.
        out = self.huey.reschedule(result.id, delay=60,
                                   operation_id='op-fail')
        self.assertEqual(out.status, 'scheduled')
        self.assertEqual(self.huey.scheduled_count(), 1)

    def test_reschedule_does_not_cross_queues(self):
        # Same task id, different queue name, must not be touched.
        other = self.get_other_huey()
        try:
            task_a = self.make_task()
            result = task_a(1)

            @other.task()
            def unrelated(n):
                return n
            other_result = unrelated(2)

            with self.assertRaises(TaskNotReschedulable):
                other.reschedule(result.id, delay=0)

            # The delivery in its own queue is untouched...
            self.assertEqual([t.id for t in self.huey.pending()], [result.id])
            # ...and the other queue's own task is untouched.
            self.assertEqual([t.id for t in other.pending()], [other_result.id])
        finally:
            other.storage.close()

    def test_preview_does_not_modify(self):
        task = self.make_task()
        result = task.schedule((4,), delay=3600)
        # Move the future delivery into the schedule so we preview a real
        # schedule -> queue transition.
        self.huey.execute(self.huey.dequeue())
        self.assertEqual(self.huey.scheduled_count(), 1)
        self.assertEqual(self.huey.pending_count(), 0)
        preview = self.huey.preview_reschedule(result.id, delay=0)
        self.assertTrue(preview.reschedulable)
        self.assertEqual(preview.location, 'queue')
        self.assertEqual(self.huey.scheduled_count(), 1)
        self.assertEqual(self.huey.pending_count(), 0)

        missing = self.huey.preview_reschedule('nope', delay=0)
        self.assertFalse(missing.reschedulable)
        self.assertEqual(missing.reason, 'not_found')

    def test_reschedule_unsupported_storage(self):
        huey = MemoryHuey(utc=False)

        class NoReschedule(object):
            supports_reschedule = False
            supports_task_ids = False
        original = huey.storage
        huey.storage = NoReschedule()
        try:
            self.assertRaises(RescheduleNotSupported, huey.reschedule, 'x',
                              delay=0)
        finally:
            huey.storage = original


class TestMemoryReschedule(RescheduleTests, BaseTestCase):
    def get_huey(self):
        return MemoryHuey(utc=False)

    def get_other_huey(self):
        return MemoryHuey(name='other', utc=False)


class TestSqliteReschedule(RescheduleTests, BaseTestCase):
    db_file = None

    def get_huey(self):
        return SqliteHuey(filename=self.db_file, utc=False, timeout=3)

    def get_other_huey(self):
        return SqliteHuey(name='other', filename=self.db_file, utc=False,
                          timeout=3)

    def setUp(self):
        self.db_file = new_db('resched-%s.db' % id(self))
        for suffix in ('', '-wal', '-shm'):
            if os.path.exists(self.db_file + suffix):
                os.unlink(self.db_file + suffix)
        super(TestSqliteReschedule, self).setUp()

    def tearDown(self):
        self.huey.storage.close()
        super(TestSqliteReschedule, self).tearDown()
        for suffix in ('', '-wal', '-shm'):
            if os.path.exists(self.db_file + suffix):
                os.unlink(self.db_file + suffix)

    def test_migration_adds_task_id_columns(self):
        # Simulate a database created by an older version without task_id.
        import sqlite3
        filename = new_db('resched-old.db')
        if os.path.exists(filename):
            os.unlink(filename)
        conn = sqlite3.connect(filename)
        conn.execute('create table task (id integer not null primary key, '
                     'queue text not null, data blob not null, '
                     'priority real not null default 0.0)')
        conn.execute('create table schedule (id integer not null primary key, '
                     'queue text not null, data blob not null, '
                     'timestamp real not null)')
        conn.commit()
        conn.close()
        try:
            huey = SqliteHuey(filename=filename, utc=False)
            huey.storage.initialize_schema()
            cols = [r[1] for r in huey.storage.conn.execute(
                'pragma table_info(task)')]
            self.assertIn('task_id', cols)
            scols = [r[1] for r in huey.storage.conn.execute(
                'pragma table_info(schedule)')]
            self.assertIn('task_id', scols)
            huey.storage.close()
        finally:
            for suffix in ('', '-wal', '-shm'):
                if os.path.exists(filename + suffix):
                    os.unlink(filename + suffix)

    def test_reschedule_survives_reopen(self):
        task = self.make_task()
        # Get the future delivery into the schedule, then move it earlier.
        result = task.schedule((8,), delay=3600)
        self.huey.execute(self.huey.dequeue())
        self.assertEqual(self.huey.scheduled_count(), 1)
        self.huey.reschedule(result.id, delay=600)
        self.huey.storage.close()

        # Reopen with a Huey that registers the same task class so the raw
        # delivery can be deserialized again. The moved delivery (same id)
        # must still be present.
        self.huey = SqliteHuey(filename=self.db_file, utc=False, timeout=3)

        @self.huey.task(name='record', module='huey.tests.test_reschedule')
        def record(n):
            return n

        scheduled = self.huey.scheduled()
        self.assertEqual([t.id for t in scheduled], [result.id])
        self.assertEqual(self.huey.pending_count(), 0)
        self.huey.reschedule(result.id, delay=0)
        self.assertEqual(self.huey.execute(self.huey.dequeue()), 8)


@unittest.skipIf(SqlHuey is None, 'requires peewee')
class TestSqlHueyRescheduleUnsupported(BaseTestCase):
    def test_sql_huey_reports_unsupported(self):
        huey = SqlHuey(database='sqlite:///%s' % new_db('resched-peewee.db'),
                       utc=False)
        try:
            self.assertRaises(RescheduleNotSupported, huey.reschedule, 'x',
                              delay=0)
        finally:
            huey.storage.drop_tables()


class TestRescheduleCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='resched-cli-')
        self.db = os.path.join(self.tmp, 'cli.db')
        self.module = os.path.join(self.tmp, 'cliapp.py')
        with open(self.module, 'w') as fh:
            fh.write(textwrap.dedent('''\
                from huey import SqliteHuey
                import os
                huey = SqliteHuey('clique', filename=os.environ['CLI_DB'],
                                  utc=False)
                @huey.task()
                def add(a, b):
                    return a + b
            '''))
        self._old_path = list(sys.path)
        sys.path.insert(0, self.tmp)
        self._old_cli_db = os.environ.get('CLI_DB')
        os.environ['CLI_DB'] = self.db
        for name in ('cliapp',):
            sys.modules.pop(name, None)

    def tearDown(self):
        sys.path[:] = self._old_path
        sys.modules.pop('cliapp', None)
        if self._old_cli_db is None:
            os.environ.pop('CLI_DB', None)
        else:
            os.environ['CLI_DB'] = self._old_cli_db
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self):
        env = dict(os.environ)
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        env['PYTHONPATH'] = os.pathsep.join([self.tmp, root])
        env['CLI_DB'] = self.db
        return env

    def _run(self, *args):
        proc = subprocess.run(
            [sys.executable, '-m', 'huey.bin.huey_reschedule', 'cliapp.huey',
             *args],
            env=self._env(), capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    def _make_task(self, delay=3600):
        import cliapp
        result = cliapp.add.schedule((2, 3), delay=delay)
        cliapp.huey.storage.close()
        return result.id

    def test_cli_dry_run_does_not_modify(self):
        task_id = self._make_task()
        code, out, err = self._run('--id', task_id, '--delay', '0',
                                   '--dry-run')
        self.assertEqual(code, 0, err)
        self.assertIn('OK -> queue', out)

        import cliapp
        huey = cliapp.huey
        # Still where it was: queued with a future eta, not moved.
        self.assertEqual(huey.pending_count(), 1)
        self.assertEqual(huey.scheduled_count(), 0)
        huey.storage.close()

    def test_cli_apply_and_report(self):
        task_id = self._make_task()
        code, out, err = self._run('--id', task_id, '--delay', '0',
                                   '--priority', '4', '--json')
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload['task_id'], task_id)
        self.assertEqual(payload['location'], 'queue')
        self.assertEqual(payload['priority'], 4.0)

    def test_cli_operation_id_replay(self):
        task_id = self._make_task()
        code, out, err = self._run('--id', task_id, '--delay', '0',
                                   '--operation-id', 'retry-1', '--json')
        self.assertEqual(code, 0, err)
        first = json.loads(out)
        self.assertEqual(first['status'], 'rescheduled')

        code, out, err = self._run('--id', task_id, '--delay', '0',
                                   '--operation-id', 'retry-1', '--json')
        self.assertEqual(code, 0, err)
        second = json.loads(out)
        self.assertEqual(second['status'], 'replayed')
        self.assertEqual(second['location'], first['location'])

    def test_cli_not_found_exit_code(self):
        code, out, err = self._run('--id', 'missing', '--delay', '0')
        self.assertEqual(code, 2)
        self.assertIn('not_found', err)

    def test_cli_rejects_invalid_time(self):
        task_id = self._make_task()
        code, out, err = self._run('--id', task_id, '--delay', '-5')
        self.assertEqual(code, 1)
        self.assertIn('Error', err)

        import cliapp
        self.assertEqual(cliapp.huey.pending_count(), 1)
        cliapp.huey.storage.close()

    def test_cli_batch_reports_partial_failure(self):
        task_id = self._make_task()
        code, out, err = self._run('--id', '%s,missing' % task_id,
                                   '--delay', '0', '--json')
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(len(payload['results']), 1)
        self.assertEqual(len(payload['errors']), 1)
        self.assertEqual(payload['errors'][0]['task_id'], 'missing')


# ---------------------------------------------------------------------------
# Real multi-process tests. These spawn actual consumer processes against a
# shared SQLite database. A controlled sync point (the worker's
# ``_on_task_claimed`` hook, signaled through a marker file) lets us interleave
# a reschedule with a claim deterministically.
# ---------------------------------------------------------------------------

WORKER_SCRIPT = textwrap.dedent('''\
    import os, sys, time
    from shared_tasks import huey, record

    gate = sys.argv[1]  # claimed-marker path; consumer waits while it exists.

    if gate != '-':
        def on_claimed(task):
            # Signal that the delivery has been atomically removed, then wait
            # for the parent to remove the gate file.
            with open(gate, 'w') as fh:
                fh.write(task.id)
            deadline = time.time() + 20
            while os.path.exists(gate + '.hold') and time.time() < deadline:
                time.sleep(0.01)
        huey._on_task_claimed = on_claimed

    consumer = huey.create_consumer(workers=1, initial_delay=0.01,
                                    max_delay=0.02, check_worker_health=False,
                                    periodic=False)
    consumer.start()
    try:
        while True:
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        consumer.stop(graceful=True)
''')

# The worker process and the test process must agree on the task's registered
# name, or the worker cannot deserialize the message. A shared module imported
# by both gives both the same ``shared_tasks.record`` registry key.
SHARED_TASKS = textwrap.dedent('''\
    import os
    from huey import SqliteHuey

    huey = SqliteHuey('procque', filename=os.environ['RESCHED_DB'],
                      utc=False, timeout=10)

    @huey.task()
    def record(n):
        log = os.environ['RESCHED_LOG']
        with open(log, 'a') as fh:
            fh.write('%s\\n' % n)
        return n
''')


class TestRescheduleMultiProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='resched-proc-')
        self.db = os.path.join(self.tmp, 'queue.db')
        self.log = os.path.join(self.tmp, 'ran.log')
        self.gate = os.path.join(self.tmp, 'claimed')
        self.script = os.path.join(self.tmp, 'worker.py')
        self.shared = os.path.join(self.tmp, 'shared_tasks.py')
        with open(self.script, 'w') as fh:
            fh.write(WORKER_SCRIPT)
        with open(self.shared, 'w') as fh:
            fh.write(SHARED_TASKS)
        self.proc = None
        self._shared_module = None
        self._old_path = list(sys.path)
        if self.tmp not in sys.path:
            sys.path.insert(0, self.tmp)

    def tearDown(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.send_signal(2)
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            if self.proc.stderr is not None:
                self.proc.stderr.close()
        if os.path.exists(self.gate + '.hold'):
            os.unlink(self.gate + '.hold')
        sys.path[:] = self._old_path
        sys.modules.pop('shared_tasks', None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self):
        env = dict(os.environ)
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        env['PYTHONPATH'] = os.pathsep.join(
            [self.tmp, root] + [p for p in [env.get('PYTHONPATH')] if p])
        env['RESCHED_DB'] = self.db
        env['RESCHED_LOG'] = self.log
        return env

    def _open_shared(self):
        os.environ['RESCHED_DB'] = self.db
        os.environ['RESCHED_LOG'] = self.log
        # Re-import so the module binds to this test's database.
        sys.modules.pop('shared_tasks', None)
        import shared_tasks
        return shared_tasks.huey, shared_tasks.record

    def _start_worker(self, gate='-'):
        self.proc = subprocess.Popen(
            [sys.executable, self.script, gate],
            env=self._env(), cwd=self.tmp,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(self.proc.stderr.read().decode())
            if os.path.exists(self.db):
                # Confirm the schema exists and is queryable.
                from huey import SqliteHuey
                try:
                    probe = SqliteHuey('procque', filename=self.db, utc=False,
                                       timeout=2)
                    probe.pending_count()
                    probe.storage.close()
                    return
                except Exception:
                    pass
            time.sleep(0.05)
        raise RuntimeError('worker did not start')

    def _enqueue(self, n, delay=None):
        huey, record = self._open_shared()
        if delay is None:
            result = record(n)
        else:
            result = record.schedule((n,), delay=delay)
        huey.storage.close()
        return result.id

    def _ran(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [line.strip() for line in fh if line.strip()]

    def _wait_for(self, predicate, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _reschedule(self, task_id, **kwargs):
        huey, record = self._open_shared()
        try:
            return huey.reschedule(task_id, **kwargs)
        finally:
            huey.storage.close()

    def _counts(self):
        huey, record = self._open_shared()
        try:
            return huey.pending_count(), huey.scheduled_count()
        finally:
            huey.storage.close()

    @slow_test()
    def test_claim_race_with_sync_point(self):
        # The worker claims the delivery, then blocks. While it is blocked, a
        # reschedule attempt must report "not reschedulable" (the delivery is
        # gone from the queue), and the task must still run exactly once.
        task_id = self._enqueue(101)
        open(self.gate + '.hold', 'w').close()
        try:
            self._start_worker(gate=self.gate)
            self.assertTrue(self._wait_for(
                lambda: os.path.exists(self.gate)), 'worker never claimed')
            with self.assertRaises(TaskNotReschedulable) as ctx:
                self._reschedule(task_id, delay=0)
            self.assertEqual(ctx.exception.reason, 'not_found')
        finally:
            if os.path.exists(self.gate + '.hold'):
                os.unlink(self.gate + '.hold')

        # Release the worker; the task runs exactly once.
        self.assertTrue(self._wait_for(lambda: self._ran() == ['101']))
        time.sleep(0.5)
        self.assertEqual(self._ran(), ['101'])

    @slow_test()
    def test_reschedule_before_claim_moves_work(self):
        # The task starts far in the future. The worker moves it from the
        # queue to the schedule (without running it). We then reschedule it to
        # "now" while the consumer is live; it must run exactly once.
        task_id = self._enqueue(202, delay=3600)
        self._start_worker()
        # Wait until the worker has moved the future delivery into the
        # schedule.
        self.assertTrue(self._wait_for(lambda: self._counts() == (0, 1)),
                        'delivery never reached the schedule')
        self.assertEqual(self._ran(), [])

        out = self._reschedule(task_id, delay=0)
        self.assertEqual(out.status, 'queued')
        self.assertTrue(self._wait_for(lambda: self._ran() == ['202']))
        time.sleep(0.5)
        # Exactly once, despite the scheduler polling in the meantime.
        self.assertEqual(self._ran(), ['202'])


if __name__ == '__main__':
    unittest.main()
