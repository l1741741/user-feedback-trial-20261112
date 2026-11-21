import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from huey.api import chord
from huey.contrib.batch import BatchError
from huey.contrib.batch import BatchMemberResult
from huey.contrib.batch import BatchSqliteHuey
from huey.tests import TEST_TMP_DIR
from huey.tests import mp_batch_tasks as mpw
from huey.tests.base import BaseTestCase
from huey.utils import BATCH_CANCELLED
from huey.utils import BATCH_FAILED
from huey.utils import BATCH_SUCCESS
from huey.utils import CANCELLED
from huey.utils import Error


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


class TestBatch(BaseTestCase):
    def get_huey(self):
        db_file = os.path.join(TEST_TMP_DIR, 'batch-%s.db' % uuid.uuid4().hex)
        return BatchSqliteHuey('batch-test', filename=db_file, utc=False)

    def run_next(self):
        task = self.huey.dequeue()
        self.assertTrue(task is not None)
        return self.huey.execute(task)

    def region_tasks(self, calls):
        @self.huey.task()
        def region(region):
            calls.append(region)
            return 'val-%s' % region

        @self.huey.task()
        def merge(results):
            calls.append('merge')
            return list(results)

        return region, merge

    def test_join_success(self):
        region, merge = self.region_tasks([])
        b = self.huey.batch('b1', [(region.s(r), r) for r in ('n', 's', 'e')],
                            merge.s())
        self.assertEqual(len(self.huey), 3)
        self.assertEqual([self.run_next() for _ in range(3)],
                         ['val-n', 'val-s', 'val-e'])

        # All members settled: the tail step is enqueued and runs once.
        self.assertEqual(len(self.huey), 1)
        self.assertEqual(self.run_next(), ['val-n', 'val-s', 'val-e'])
        self.assertEqual(len(self.huey), 0)
        self.assertEqual(b(), ['val-n', 'val-s', 'val-e'])

        outcomes = self.huey.batch_result('b1')
        self.assertEqual([outcomes[r].outcome for r in ('n', 's', 'e')],
                         [BATCH_SUCCESS] * 3)
        self.assertEqual(outcomes['s'].value, 'val-s')

    def test_failed_and_cancelled_distinguished(self):
        @self.huey.task()
        def region(region):
            if region == 'bad':
                raise ValueError('boom')
            return 'val-%s' % region

        @self.huey.task()
        def merge(results):
            return list(results)

        self.huey.batch('b2', [
            (region.s('ok'), 'ok'),
            (region.s('bad'), 'bad'),
            (region.s('wd'), 'wd'),
        ], merge.s())
        self.huey.cancel_region('b2', 'wd')
        for _ in range(3):
            self.run_next()

        ok, bad, wd = self.run_next()  # Tail step.
        self.assertEqual(ok, 'val-ok')
        self.assertTrue(isinstance(bad, Error))
        self.assertTrue(wd is CANCELLED)

        outcomes = self.huey.batch_result('b2')
        self.assertEqual(outcomes['ok'].outcome, BATCH_SUCCESS)
        self.assertEqual(outcomes['bad'].outcome, BATCH_FAILED)
        self.assertTrue(isinstance(outcomes['bad'].value, Error))
        self.assertEqual(outcomes['bad'].value.metadata['error'],
                         "ValueError('boom')")
        # Cancelled is distinct from failed: no error payload.
        self.assertEqual(outcomes['wd'].outcome, BATCH_CANCELLED)
        self.assertIsNone(outcomes['wd'].value)

    def test_cancel_before_start_skips_function(self):
        calls = []
        region, merge = self.region_tasks(calls)
        self.huey.batch('b3', [(region.s('a'), 'a'), (region.s('b'), 'b')],
                        merge.s())
        self.huey.cancel_region('b3', 'a')
        self.run_next()  # a: skipped, settles cancelled.
        self.run_next()  # b: runs normally.
        self.assertEqual(calls, ['b'])
        self.assertEqual(self.run_next(), [CANCELLED, 'val-b'])
        self.assertEqual(calls, ['b', 'merge'])

    def test_inflight_cancel_suppresses_successors(self):
        calls = []

        @self.huey.task()
        def region(region):
            calls.append(region)
            if region == 'x':
                # Withdrawn while already inside the function.
                self.huey.cancel_region('b4', 'x')
            return 'val-%s' % region

        @self.huey.task()
        def succ(tag, prev=None):
            calls.append('succ-%s' % tag)
            return tag

        @self.huey.task()
        def merge(results):
            return list(results)

        self.huey.batch('b4', [
            (region.s('x').then(succ, 'x'), 'x'),
            (region.s('y').then(succ, 'y'), 'y'),
        ], merge.s())
        self.run_next()  # x: completes but is cancelled -> no successor.
        self.run_next()  # y: runs and dispatches its successor.
        # Two tasks remain: y's successor and the tail step; their relative
        # order in the queue is unspecified.
        remaining = [self.run_next() for _ in range(2)]
        self.assertIn('y', remaining)  # succ('y') ran.
        self.assertIn([CANCELLED, 'val-y'], remaining)  # Tail step ran.
        self.assertEqual(calls, ['x', 'y', 'succ-y'])
        self.assertEqual(len(self.huey), 0)

    def test_inflight_cancel_stores_own_result(self):
        @self.huey.task()
        def region(region):
            self.huey.cancel_region('b5', region)
            return 'val-%s' % region

        @self.huey.task()
        def merge(results):
            return list(results)

        b = self.huey.batch('b5', [(region.s('x'), 'x')], merge.s())
        self.run_next()
        self.assertEqual(self.run_next(), [CANCELLED])
        # The function ran to completion, so its own result is stored even
        # though the join records the member as cancelled.
        self.assertEqual(b.members[0].get(), 'val-x')
        outcomes = self.huey.batch_result('b5')
        self.assertEqual(outcomes['x'].outcome, BATCH_CANCELLED)

    def test_inflight_failure_after_cancel_records_cancelled(self):
        @self.huey.task()
        def region(region):
            self.huey.cancel_region('b6', region)
            raise ValueError('boom')

        @self.huey.task()
        def merge(results):
            return list(results)

        self.huey.batch('b6', [(region.s('x'), 'x')], merge.s())
        self.run_next()
        self.assertEqual(self.run_next(), [CANCELLED])
        outcomes = self.huey.batch_result('b6')
        self.assertEqual(outcomes['x'].outcome, BATCH_CANCELLED)

    def test_resume_allows_only_subsequent_deliveries(self):
        calls = []
        region, merge = self.region_tasks(calls)
        self.huey.batch('b7', [(region.s('r1'), 'r1'), (region.s('r2'), 'r2')],
                        merge.s())
        self.huey.cancel_region('b7', 'r1')
        self.run_next()  # r1: skipped.
        self.run_next()  # r2: runs.
        self.assertEqual(self.run_next(), [CANCELLED, 'val-r2'])
        self.assertEqual(calls, ['r2', 'merge'])

        # A delivery while the region is still withdrawn does not run.
        self.huey.deliver_region('b7', 'r1', region.s('r1'))
        self.run_next()
        self.assertEqual(calls, ['r2', 'merge'])

        # After resuming, a subsequent delivery runs normally.
        self.huey.resume_region('b7', 'r1')
        self.huey.deliver_region('b7', 'r1', region.s('r1'))
        self.assertEqual(self.run_next(), 'val-r1')
        self.assertEqual(calls, ['r2', 'merge', 'r1'])

        # The withdrawn member is not resurrected: its recorded outcome
        # stands and the tail step is not fired again.
        outcomes = self.huey.batch_result('b7')
        self.assertEqual(outcomes['r1'].outcome, BATCH_CANCELLED)
        self.assertEqual(outcomes['r2'].outcome, BATCH_SUCCESS)
        self.assertEqual(len(self.huey), 0)
        self.assertEqual(calls.count('merge'), 1)

    def test_resume_does_not_revive_inflight_generation(self):
        calls = []
        state = {'done': False}

        @self.huey.task()
        def region(region):
            calls.append(region)
            if not state['done']:
                # Withdraw and immediately resume while in-flight; this
                # delivery still belongs to the withdrawn generation.
                state['done'] = True
                self.huey.cancel_region('b8', region)
                self.huey.resume_region('b8', region)
            return 'val-%s' % region

        @self.huey.task()
        def succ(tag, prev=None):
            calls.append('succ-%s' % tag)

        @self.huey.task()
        def merge(results):
            return list(results)

        self.huey.batch('b8', [(region.s('r').then(succ, 'r'), 'r')],
                        merge.s())
        self.run_next()  # Old generation: cancelled despite the resume.
        self.assertEqual(self.run_next(), [CANCELLED])
        self.assertEqual(calls, ['r'])  # No successor dispatched.

        # A delivery made after the resume runs with the new generation.
        self.huey.deliver_region('b8', 'r', region.s('r'))
        self.assertEqual(self.run_next(), 'val-r')
        self.assertEqual(calls, ['r', 'r'])
        outcomes = self.huey.batch_result('b8')
        self.assertEqual(outcomes['r'].outcome, BATCH_CANCELLED)

    def test_retry_then_cancelled_before_retry(self):
        attempts = []

        @self.huey.task(retries=1)
        def region(region):
            attempts.append(region)
            if region == 'flaky':
                raise ValueError('transient')
            return 'val-%s' % region

        @self.huey.task()
        def merge(results):
            return list(results)

        self.huey.batch('b9', [(region.s('flaky'), 'flaky'),
                               (region.s('ok'), 'ok')], merge.s())
        self.run_next()  # flaky fails, is requeued.
        self.run_next()  # ok succeeds.
        self.huey.cancel_region('b9', 'flaky')
        self.run_next()  # flaky's retry is skipped, settles cancelled.
        self.assertEqual(attempts, ['flaky', 'ok'])
        self.assertEqual(self.run_next(), [CANCELLED, 'val-ok'])

    def test_revoked_member_settles_cancelled(self):
        region, merge = self.region_tasks([])
        b = self.huey.batch('b10', [(region.s('a'), 'a'),
                                    (region.s('b'), 'b')], merge.s())
        b.members[0].revoke()
        self.run_next()  # a: revoked, settles cancelled.
        self.run_next()  # b: runs.
        self.assertEqual(self.run_next(), [CANCELLED, 'val-b'])
        outcomes = self.huey.batch_result('b10')
        self.assertEqual(outcomes['a'].outcome, BATCH_CANCELLED)

    def test_batch_result_pending(self):
        region, merge = self.region_tasks([])
        self.huey.batch('b11', [(region.s('a'), 'a')], merge.s())
        outcomes = self.huey.batch_result('b11')
        self.assertEqual(outcomes, {'a': BatchMemberResult(None, None)})

    def test_empty_batch(self):
        @self.huey.task()
        def merge(results):
            return list(results)

        b = self.huey.batch('b12', [], merge.s())
        self.assertEqual(len(self.huey), 1)  # Tail enqueued immediately.
        self.assertEqual(self.run_next(), [])
        self.assertEqual(b(), [])
        self.assertEqual(self.huey.batch_result('b12'), {})

    def test_duplicate_batch_rejected(self):
        region, merge = self.region_tasks([])
        self.huey.batch('b13', [(region.s('a'), 'a')], merge.s())
        with self.assertRaises(BatchError):
            self.huey.batch('b13', [(region.s('a'), 'a')], merge.s())

    def test_unknown_batch_and_region(self):
        region, merge = self.region_tasks([])
        self.huey.batch('b14', [(region.s('a'), 'a')], merge.s())
        for fn in (self.huey.cancel_region, self.huey.resume_region):
            with self.assertRaises(BatchError):
                fn('b14', 'nope')
            with self.assertRaises(BatchError):
                fn('nope', 'a')
        with self.assertRaises(BatchError):
            self.huey.deliver_region('b14', 'nope', region.s('x'))
        self.assertIsNone(self.huey.batch_result('nope'))

    def test_settle_first_write_wins_and_tail_claimed_once(self):
        s = self.huey.storage
        s.create_batch('b15', ['a', 'b'], b'callback')
        self.assertEqual(
            s.settle_batch_member('b15', 0, 'a', 0, BATCH_SUCCESS, b'1'),
            (True, False, False))
        # Second settle of the same member is ignored.
        self.assertEqual(
            s.settle_batch_member('b15', 0, 'a', 0, BATCH_FAILED, b'2'),
            (False, False, False))
        # Completing the join claims the tail exactly once.
        self.assertEqual(
            s.settle_batch_member('b15', 1, 'b', 0, BATCH_SUCCESS, b'3'),
            (True, True, False))
        self.assertEqual(
            s.settle_batch_member('b15', 1, 'b', 0, BATCH_SUCCESS, b'4'),
            (False, False, False))
        # A cancellation covering the delivery's generation downgrades the
        # outcome atomically and is reported to the caller.
        s.cancel_batch_region('b15', 'b')
        self.assertEqual(
            s.settle_batch_member('b15', 1, 'b', 0, BATCH_SUCCESS, b'5'),
            (False, False, True))
        outcomes = s.batch_member_outcomes('b15')
        self.assertEqual([row[2] for row in outcomes],
                         [BATCH_SUCCESS, BATCH_SUCCESS])

    def test_plain_tasks_keep_original_semantics(self):
        # Tasks enqueued outside the batch entry-point are unaffected.
        calls = []

        @self.huey.task()
        def add(a, b):
            calls.append(('add', a, b))
            return a + b

        @self.huey.task()
        def show(v):
            calls.append(('show', v))
            return v

        # Pipelines dispatch successors as usual.
        r = self.huey.enqueue(add.s(1, 2).then(show))
        self.assertEqual(self.run_next(), 3)
        self.assertEqual(self.run_next(), 3)
        self.assertEqual(calls, [('add', 1, 2), ('show', 3)])
        self.assertEqual(r[0], 3)
        self.assertEqual(r[1], 3)

        # Revocation works as usual.
        r2 = self.huey.enqueue(add.s(3, 4))
        r2.revoke()
        self.assertIsNone(self.run_next())
        self.assertEqual(len(calls), 2)

        # Chords still join as usual.
        @self.huey.task()
        def agg(values):
            return sum(values)

        cr = self.huey.enqueue(chord([add.s(1, 1), add.s(2, 2)], agg.s()))
        self.assertEqual([self.run_next() for _ in range(2)], [2, 4])
        self.assertEqual(self.run_next(), 6)
        self.assertEqual(cr(), 6)


class TestBatchMultiProcess(unittest.TestCase):
    """
    Verify batch cancellation and exactly-once settlement across separate
    OS processes sharing one SQLite database.
    """
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='huey-batch-mp-', dir=TEST_TMP_DIR)
        self.db = os.path.join(self.tmp, 'mp.db')
        self.sync = os.path.join(self.tmp, 'sync')
        os.makedirs(self.sync)
        self.name = 'batch-mp-%s' % uuid.uuid4().hex[:12]
        self.bid = 'bid-%s' % uuid.uuid4().hex[:12]
        self.huey = BatchSqliteHuey(self.name, filename=self.db, timeout=30,
                                    utc=False)
        for wrapper in (mpw.mp_region, mpw.mp_region_shared, mpw.mp_successor,
                        mpw.mp_merge, mpw.mp_plain):
            self.huey._registry.register(wrapper.task_class)
        self.procs = []
        self.logs = []

    def tearDown(self):
        try:
            open(os.path.join(self.sync, 'stop'), 'w').close()
        except OSError:
            pass
        for proc in self.procs:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for log in self.logs:
            log.close()
        self.huey.storage.close()

    def start_worker(self):
        env = dict(os.environ)
        env.update({
            'BATCH_TEST_DB': self.db,
            'BATCH_TEST_SYNC': self.sync,
            'BATCH_TEST_NAME': self.name,
            'PYTHONPATH': os.pathsep.join(
                [REPO_ROOT, env.get('PYTHONPATH', '')]),
        })
        log = open(os.path.join(self.tmp, 'worker-%d.log' % len(self.procs)),
                   'wb')
        self.logs.append(log)
        proc = subprocess.Popen(
            [sys.executable, '-m', 'huey.tests.mp_batch_worker'],
            env=env, cwd=REPO_ROOT, stdout=log, stderr=log)
        self.procs.append(proc)
        return proc

    def touch(self, name):
        open(os.path.join(self.sync, name), 'w').close()

    def exists(self, name):
        return os.path.exists(os.path.join(self.sync, name))

    def wait_file(self, name, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.exists(name):
                return
            time.sleep(0.05)
        raise AssertionError('timed out waiting for marker %s' % name)

    def wait_join_settled(self, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            outcomes = self.huey.batch_result(self.bid)
            if outcomes and all(o.outcome is not None
                                for o in outcomes.values()):
                return outcomes
            time.sleep(0.05)
        raise AssertionError('timed out waiting for the join to settle')

    def merge_log(self):
        with open(os.path.join(self.sync, 'merge-log')) as fh:
            return [line.strip() for line in fh if line.strip()]

    def test_inflight_cancel_across_processes(self):
        # A region withdrawn while its task is executing in another process
        # completes without dispatching successors; the other region is
        # unaffected, and the tail step runs once.
        self.huey.batch(self.bid, [
            (mpw.mp_region.s('slow').then(mpw.mp_successor, 'slow'), 'slow'),
            (mpw.mp_region.s('fast').then(mpw.mp_successor, 'fast'), 'fast'),
        ], mpw.mp_merge.s())
        self.start_worker()
        self.wait_file('started-slow')
        self.huey.cancel_region(self.bid, 'slow')
        self.touch('release-slow')
        self.touch('release-fast')

        outcomes = self.wait_join_settled()
        self.assertEqual(outcomes['slow'].outcome, BATCH_CANCELLED)
        self.assertEqual(outcomes['fast'].outcome, BATCH_SUCCESS)
        self.assertEqual(outcomes['fast'].value, 'val-fast')

        self.wait_file('merge-log')
        self.wait_file('successor-fast')
        time.sleep(0.5)  # A stray successor would have appeared by now.
        self.assertFalse(self.exists('successor-slow'))
        lines = self.merge_log()
        self.assertEqual(len(lines), 1)
        self.assertIn('CANCELLED', lines[0])
        self.assertIn('val-fast', lines[0])

    def test_concurrent_tail_members_settle_once(self):
        # The last two members finish at the same time in different
        # processes; the tail step must still be enqueued exactly once.
        self.huey.batch(self.bid, [
            (mpw.mp_region_shared.s('m1'), 'm1'),
            (mpw.mp_region_shared.s('m2'), 'm2'),
        ], mpw.mp_merge.s())
        self.start_worker()
        self.start_worker()
        self.wait_file('started-m1')
        self.wait_file('started-m2')
        self.touch('release-all')  # Both members settle concurrently.

        outcomes = self.wait_join_settled()
        self.assertEqual(outcomes['m1'].outcome, BATCH_SUCCESS)
        self.assertEqual(outcomes['m2'].outcome, BATCH_SUCCESS)
        self.wait_file('merge-log')
        time.sleep(0.5)  # A duplicate tail would append a second line.
        self.assertEqual(len(self.merge_log()), 1)

    def test_restart_preserves_cancel_and_join(self):
        # A worker is killed mid-flight; a restarted worker continues the
        # batch. Cancellation state survives the restart, the lost delivery
        # can be re-delivered, and the tail step fires once.
        self.huey.batch(self.bid, [
            (mpw.mp_region.s('a'), 'a'),
            (mpw.mp_region.s('b'), 'b'),
            (mpw.mp_region.s('c'), 'c'),
        ], mpw.mp_merge.s())
        self.huey.cancel_region(self.bid, 'c')

        worker = self.start_worker()
        self.wait_file('started-a')
        worker.kill()  # Crash while 'a' is in-flight; the delivery is lost.
        worker.wait()

        self.start_worker()  # Restart.
        self.huey.deliver_region(self.bid, 'a', mpw.mp_region.s('a'))
        self.touch('release-a')
        self.touch('release-b')
        self.touch('release-c')

        outcomes = self.wait_join_settled()
        self.assertEqual(outcomes['a'].outcome, BATCH_SUCCESS)
        self.assertEqual(outcomes['b'].outcome, BATCH_SUCCESS)
        # The pre-restart cancellation is still in effect: c never ran.
        self.assertEqual(outcomes['c'].outcome, BATCH_CANCELLED)
        self.assertFalse(self.exists('started-c'))
        self.wait_file('merge-log')
        time.sleep(0.5)
        self.assertEqual(len(self.merge_log()), 1)

    def test_plain_tasks_original_semantics_across_processes(self):
        # Without the batch entry-point, tasks keep Huey's original
        # semantics: pipelines dispatch successors and revocation works.
        r = self.huey.enqueue(mpw.mp_plain.s(1).then(mpw.mp_successor, 'one'))
        revoked = self.huey.enqueue(mpw.mp_plain.s(2))
        revoked.revoke()
        self.start_worker()
        self.wait_file('successor-one')
        self.assertTrue(self.exists('plain-1'))
        time.sleep(0.5)  # The revoked task would have run by now.
        self.assertFalse(self.exists('plain-2'))
        self.assertEqual(r[0], 1)
        self.assertEqual(r[1], 'one')
