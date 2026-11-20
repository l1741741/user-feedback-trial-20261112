import contextlib
import multiprocessing
import os
import time
import unittest
import uuid

from unittest import mock

import huey.api
import huey.storage

from huey.api import ChordResult
from huey.api import MemoryHuey
from huey.api import Result
from huey.api import ResultGroup
from huey.api import SqliteHuey
from huey.api import Task
from huey.constants import EmptyData
from huey.exceptions import ResultTimeout
from huey.exceptions import TaskException
from huey.storage import MemoryStorage
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase
from huey.utils import Error


class FakeClock(object):
    """Monotonic clock / sleep replacement that only advances when the code
    under test sleeps. Lets tests control exactly when the group deadline
    expires without real sleeps."""
    def __init__(self, now=1000.0):
        self.now = now
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.now += secs

    def __getattr__(self, name):
        return getattr(time, name)


@contextlib.contextmanager
def fake_time(clock):
    # Rebind the "time" module reference inside the huey modules only, so the
    # rest of the process (threading, etc.) keeps the real clock.
    with mock.patch.object(huey.api, 'time', clock), \
         mock.patch.object(huey.storage, 'time', clock):
        yield clock


class ScriptedStorage(MemoryStorage):
    """MemoryStorage that "produces" scheduled results once the fake clock
    reaches their release time, simulating tasks finishing at known times."""
    def __init__(self, clock, *args, **kwargs):
        super(ScriptedStorage, self).__init__(*args, **kwargs)
        self.clock = clock
        self.script = []

    def schedule_result(self, eta, key, value):
        self.script.append((eta, key, value))

    def _release_due(self):
        for eta, key, value in self.script[:]:
            if eta <= self.clock.now:
                self.script.remove((eta, key, value))
                self.put_data(key, value)

    def has_data_for_key(self, key):
        self._release_due()
        return super(ScriptedStorage, self).has_data_for_key(key)

    def peek_data(self, key):
        self._release_due()
        return super(ScriptedStorage, self).peek_data(key)

    def pop_data(self, key):
        self._release_due()
        return super(ScriptedStorage, self).pop_data(key)


def make_error(task_id, error="ValueError('boom')"):
    return Error({'error': error, 'task_id': task_id, 'traceback': 'tb',
                  'retries': 0})


class TestGroupDeadline(BaseTestCase):
    def setUp(self):
        super(TestGroupDeadline, self).setUp()
        self.clock = FakeClock()
        self.storage = ScriptedStorage(self.clock, 'huey')
        self.huey.storage = self.storage
        self._patcher = fake_time(self.clock)
        self._patcher.__enter__()
        self.addCleanup(self._patcher.__exit__, None, None, None)

    def make_results(self, *ids):
        return [Result(self.huey, Task(id=task_id)) for task_id in ids]

    def put(self, task_id, value):
        self.huey.put_result(task_id, value)

    def put_at(self, eta_offset, task_id, value):
        self.storage.schedule_result(
            self.clock.now + eta_offset, task_id,
            self.huey.serializer.serialize(value))

    def test_legacy_timeout_stacks_per_member(self):
        # Without group_timeout the old behavior is preserved: each member's
        # timeout applies independently, so the waits stack.
        results = self.make_results('a', 'b', 'c')
        grp = ResultGroup(results)
        self.put_at(1.9, 'a', 'A')
        self.put_at(3.8, 'b', 'B')
        # 'c' never arrives.

        start = self.clock.now
        self.assertRaises(ResultTimeout,
                          lambda: grp.get(blocking=True, timeout=2.0))
        # 1.9 (wait for a) + 1.9 (wait for b) + 2.0 (timeout on c).
        self.assertTrue(self.clock.now - start >= 5.5)

    def test_group_timeout_uses_single_budget(self):
        # The whole group shares one monotonic budget: members that finish
        # early do not reset the deadline for the rest.
        results = self.make_results('a', 'b', 'c')
        grp = ResultGroup(results)
        self.put_at(1.9, 'a', 'A')
        self.put_at(3.8, 'b', 'B')
        # 'c' never arrives.

        start = self.clock.now
        exc = None
        try:
            grp.get(blocking=True, group_timeout=2.0)
        except ResultTimeout as exc_val:
            exc = exc_val
        self.assertTrue(exc is not None)
        # Deadline expired at ~2.0s even though 'a' arrived at 1.9s.
        self.assertTrue(self.clock.now - start < 3.0)
        # The missing members are identifiable from the exception.
        self.assertIn('b', str(exc))
        self.assertIn('c', str(exc))

    def test_group_timeout_implies_blocking(self):
        results = self.make_results('a')
        grp = ResultGroup(results)
        self.put_at(1.0, 'a', 'A')
        # blocking=False is overridden by the presence of group_timeout.
        self.assertEqual(grp.get(group_timeout=5.0), ['A'])

    def test_partial_results_reread_after_timeout(self):
        results = self.make_results('a', 'b', 'c')
        grp = ResultGroup(results)
        self.put('a', 'A')

        self.assertRaises(ResultTimeout,
                          lambda: grp.get(blocking=True, group_timeout=1.0))

        # The timed-out wait consumed nothing: the partial result that did
        # arrive is still in the result store.
        self.assertTrue(self.storage.peek_data('a') is not EmptyData)

        # Once the rest arrive, the whole group can be re-read.
        self.put('b', 'B')
        self.put('c', 'C')
        self.assertEqual(grp.get(blocking=True, group_timeout=1.0),
                         ['A', 'B', 'C'])
        self.assertEqual(self.huey.result_count(), 0)

    def test_member_failure_identifies_task_and_consumes_nothing(self):
        results = self.make_results('a', 'b', 'c')
        grp = ResultGroup(results)
        self.put('a', 'A')
        self.put('b', make_error('b'))
        self.put('c', 'C')

        exc = None
        try:
            grp.get(blocking=True, group_timeout=1.0)
        except TaskException as exc_val:
            exc = exc_val
        self.assertTrue(exc is not None)
        # The failure is locatable to the corresponding task.
        self.assertEqual(exc.task_id, 'b')
        self.assertEqual(exc.metadata['task_id'], 'b')

        # Hitting the error did not consume the other members' results, so
        # the caller can retry the read.
        for task_id in ('a', 'b', 'c'):
            self.assertTrue(self.storage.peek_data(task_id) is not EmptyData)

        # The failed task is retried and overwrites its error result.
        self.put('b', 'B-fixed')
        self.assertEqual(grp.get(blocking=True, group_timeout=1.0),
                         ['A', 'B-fixed', 'C'])
        self.assertEqual(self.huey.result_count(), 0)

    def test_nested_group_failure_locates_task(self):
        ra, rb = self.make_results('na', 'nb')
        rc, = self.make_results('nc')
        grp = ResultGroup([ResultGroup([ra, rb]), rc])
        self.put('na', 'A')
        self.put('nb', make_error('nb'))
        self.put('nc', 'C')

        exc = None
        try:
            grp.get(blocking=True, group_timeout=1.0)
        except TaskException as exc_val:
            exc = exc_val
        self.assertTrue(exc is not None)
        self.assertEqual(exc.task_id, 'nb')

        self.put('nb', 'B-fixed')
        self.assertEqual(grp.get(blocking=True, group_timeout=1.0),
                         [['A', 'B-fixed'], 'C'])

    def test_nested_group_and_chord_positions(self):
        r1, r2 = self.make_results('g1', 'g2')
        m1, m2, cb = self.make_results('m1', 'm2', 'cb')
        chord_result = ChordResult([m1, m2], cb)
        grp = ResultGroup([ResultGroup([r1, r2]), chord_result])

        # The chord's value is its callback result; member results of the
        # chord are not fetched by get().
        self.put('g1', 1)
        self.put('g2', 2)
        self.put('cb', 3)

        self.assertEqual(grp.get(blocking=True, group_timeout=1.0),
                         [[1, 2], 3])
        self.assertEqual(self.huey.result_count(), 0)

    def test_duplicate_task_references_popped_once(self):
        ra1, ra2, rb = self.make_results('a', 'a', 'b')
        grp = ResultGroup([ra1, ra2, rb])
        self.put('a', 'A')
        self.put('b', 'B')

        pops = []
        orig_pop = self.storage.pop_data
        self.storage.pop_data = lambda key: pops.append(key) or orig_pop(key)

        self.assertEqual(grp.get(blocking=True, group_timeout=1.0),
                         ['A', 'A', 'B'])
        # The shared task was destructively read exactly once.
        self.assertEqual(sorted(pops), ['a', 'b'])
        self.assertEqual(self.huey.result_count(), 0)

    def test_preserve_true_keeps_results_in_store(self):
        results = self.make_results('a', 'b')
        grp = ResultGroup(results)
        self.put('a', 'A')
        self.put('b', 'B')

        self.assertEqual(
            grp.get(blocking=True, group_timeout=1.0, preserve=True),
            ['A', 'B'])
        # preserve=True: nothing was removed, so the results can be read
        # again (by this group or another handle).
        self.assertEqual(self.huey.result_count(), 2)
        self.assertEqual(
            grp.get(blocking=True, group_timeout=1.0, preserve=True),
            ['A', 'B'])

        # A subsequent non-preserving read consumes them.
        fresh = ResultGroup(self.make_results('a', 'b'))
        self.assertEqual(fresh.get(blocking=True, group_timeout=1.0),
                         ['A', 'B'])
        self.assertEqual(self.huey.result_count(), 0)

    def test_revoke_on_timeout_revokes_only_missing_members(self):
        results = self.make_results('a', 'b')
        grp = ResultGroup(results)
        self.put('a', 'A')

        self.assertRaises(
            ResultTimeout,
            lambda: grp.get(blocking=True, group_timeout=1.0,
                            revoke_on_timeout=True))

        # Only the member that was not ready by the deadline is revoked.
        self.assertFalse(results[0].is_revoked())
        self.assertTrue(results[1].is_revoked())

        # ...and the arrived result is still fetchable.
        self.assertTrue(self.storage.peek_data('a') is not EmptyData)

    def test_cached_results_not_read_from_store_again(self):
        ra, rb = self.make_results('a', 'b')
        grp = ResultGroup([ra, rb])

        # 'a' is read individually first: its handle caches the value and the
        # store copy is consumed.
        self.put('a', 'A')
        self.assertEqual(ra.get(), 'A')

        # A retry of task 'a' later writes a *new* result under the same key.
        self.put('a', 'A-new')
        self.put('b', 'B')

        pops = []
        orig_pop = self.storage.pop_data
        self.storage.pop_data = lambda key: pops.append(key) or orig_pop(key)
        waits = []
        orig_wait = self.storage.wait_result
        def wait_result(key, *args):
            waits.append(key)
            return orig_wait(key, *args)
        self.storage.wait_result = wait_result

        # The cached handle neither waits nor touches the store again: its
        # (old) value is used, and the new store copy is left alone.
        self.assertEqual(grp.get(blocking=True, group_timeout=1.0), ['A', 'B'])
        self.assertEqual(waits, ['b'])
        self.assertEqual(pops, ['b'])
        self.assertEqual(self.huey.get('a', peek=True), 'A-new')

    def test_late_notification_still_reads_real_result(self):
        # A producer writes the result but the notification is lost / arrives
        # late (wait_result reports timeout): the final non-destructive read
        # still observes the real value.
        results = self.make_results('a', 'b')
        grp = ResultGroup(results)

        def wait_result(key, *args):
            self.storage.put_data(key, self.huey.serializer.serialize(
                key.upper()))
            return False  # Notification lost.
        self.storage.wait_result = wait_result

        self.assertEqual(grp.get(blocking=True, group_timeout=1.0),
                         ['A', 'B'])

    def test_wait_delegated_to_storage_not_busy_poll(self):
        results = self.make_results('a', 'b', 'c')
        grp = ResultGroup(results)

        calls = []
        def wait_result(key, timeout=None, *args):
            calls.append((key, timeout))
            return False
        self.storage.wait_result = wait_result

        self.assertRaises(ResultTimeout,
                          lambda: grp.get(blocking=True, group_timeout=2.0))
        # One bounded wait per pending member, each given the remaining
        # budget -- the group code never polls the result store itself.
        self.assertEqual([key for key, _ in calls], ['a', 'b', 'c'])
        for _, timeout in calls:
            self.assertTrue(0 < timeout <= 2.0)

    def test_legacy_nonblocking_and_blocking(self):
        results = self.make_results('a', 'b')
        grp = ResultGroup(results)

        # Non-blocking: not-ready results come back as None.
        self.assertEqual(grp.get(), [None, None])

        # Blocking with per-member timeout still raises ResultTimeout.
        start = self.clock.now
        self.assertRaises(ResultTimeout,
                          lambda: grp.get(blocking=True, timeout=1.0))
        self.assertTrue(self.clock.now - start >= 1.0)

        self.put('a', 'A')
        self.put('b', 'B')
        self.assertEqual(grp.get(blocking=True, timeout=1.0), ['A', 'B'])


def _producer_main(db_path, queue_name, phase1, phase2, ev_phase1,
                   ev_release, ev_done):
    # Separate process that plays the role of an external consumer, writing
    # task results into the shared SQLite result store. Ordering is driven
    # entirely by the events, not by sleeps.
    from huey.api import SqliteHuey
    from huey.utils import Error

    huey = SqliteHuey(queue_name, filename=db_path, utc=False)

    def write(plan):
        for task_id, kind, payload in plan:
            if kind == 'error':
                value = Error({'error': payload, 'task_id': task_id,
                               'traceback': 'tb', 'retries': 0})
            else:
                value = payload
            huey.put_result(task_id, value)

    write(phase1)
    ev_phase1.set()
    ev_release.wait(30)  # Failsafe only; sequencing is via the events.
    write(phase2)
    ev_done.set()
    huey.storage.close()


class TestGroupDeadlineSqlite(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(
            TEST_TMP_DIR, 'grp-deadline-%s.db' % uuid.uuid4().hex)
        self.huey = SqliteHuey('grp-deadline', filename=self.db_path,
                               utc=False)

    def tearDown(self):
        self.huey.storage.close()

    def start_producer(self, phase1, phase2):
        ctx = multiprocessing.get_context('spawn')
        ev_phase1, ev_release, ev_done = ctx.Event(), ctx.Event(), ctx.Event()
        proc = ctx.Process(
            target=_producer_main,
            args=(self.db_path, self.huey.name, phase1, phase2,
                  ev_phase1, ev_release, ev_done))
        proc.start()
        self.addCleanup(self._join_producer, proc)
        return ev_phase1, ev_release, ev_done

    def _join_producer(self, proc):
        proc.join(30)
        if proc.is_alive():
            proc.terminate()
            proc.join()

    def make_results(self, *ids):
        return [Result(self.huey, Task(id=task_id)) for task_id in ids]

    def test_partial_completion_timeout_then_reread(self):
        results = self.make_results('sql-a', 'sql-b', 'sql-c')
        grp = ResultGroup(results)

        ev_phase1, ev_release, ev_done = self.start_producer(
            phase1=[('sql-a', 'value', 'A')],
            phase2=[('sql-b', 'value', 'B'), ('sql-c', 'value', 'C')])

        # Wait until the producer has written the first result, then wait on
        # the group with a single 0.5s budget: 'a' is ready but 'b'/'c' are
        # not, so the wait must expire at ~0.5s (not 3 x 0.5s).
        self.assertTrue(ev_phase1.wait(30))
        start = time.monotonic()
        exc = None
        try:
            grp.get(blocking=True, group_timeout=0.5)
        except ResultTimeout as exc_val:
            exc = exc_val
        elapsed = time.monotonic() - start
        self.assertTrue(exc is not None)
        self.assertTrue(0.4 <= elapsed < 5.0, elapsed)
        self.assertIn('sql-b', str(exc))
        self.assertIn('sql-c', str(exc))

        # The partial result written before the timeout was not consumed.
        self.assertTrue(self.huey.storage.peek_data('sql-a') is not EmptyData)

        # Let the producer finish the remaining results, then re-read the
        # whole group: every value is still available, in input order.
        ev_release.set()
        self.assertTrue(ev_done.wait(30))
        self.assertEqual(grp.get(blocking=True, group_timeout=10),
                         ['A', 'B', 'C'])
        self.assertEqual(self.huey.result_count(), 0)

    def test_nested_group_failure_then_retry(self):
        ra, rb = self.make_results('nest-a', 'nest-b')
        rc, = self.make_results('nest-c')
        grp = ResultGroup([ResultGroup([ra, rb]), rc])

        ev_phase1, ev_release, ev_done = self.start_producer(
            phase1=[('nest-a', 'value', 'A'),
                    ('nest-b', 'error', "ValueError('boom')"),
                    ('nest-c', 'value', 'C')],
            phase2=[('nest-b', 'value', 'B-fixed')])

        self.assertTrue(ev_phase1.wait(30))
        exc = None
        try:
            grp.get(blocking=True, group_timeout=10)
        except TaskException as exc_val:
            exc = exc_val
        self.assertTrue(exc is not None)
        # The nested failed member is locatable by task id.
        self.assertEqual(exc.task_id, 'nest-b')

        # Nothing was consumed, including the failed member's error result.
        for task_id in ('nest-a', 'nest-b', 'nest-c'):
            self.assertTrue(
                self.huey.storage.peek_data(task_id) is not EmptyData)

        # The producer "retries" the failed task and overwrites its result;
        # the group can then be read in full, preserving the nesting.
        ev_release.set()
        self.assertTrue(ev_done.wait(30))
        self.assertEqual(grp.get(blocking=True, group_timeout=10),
                         [['A', 'B-fixed'], 'C'])
        self.assertEqual(self.huey.result_count(), 0)
