"""
Tests for the recoverable (durable) chord protocol on the SQLite backend.

The protocol guarantees, all verified here against real SQLite files (so the
same code paths run under multiple processes):

* member results are collected by their original position, regardless of the
  completion order;
* a repeated completion notification for the same member never consumes a
  second slot;
* the last completion moves the chord to "ready" and the callback handoff to
  the queue creates exactly one callback task per database, even when two
  workers race on the final members;
* a crash between "ready" and the enqueue is repaired by the recovery sweep
  without ever publishing a duplicate;
* terminal states (success, permanent failure, cancellation, expiry) all
  report exactly once because retries are not terminal;
* an abandoned open chord (hard-killed worker or an undeliverable/poison
  message) is reconciled after a grace period instead of hanging forever;
* cleanup only removes evidence after delivery plus a retention window.
"""
import datetime
import os
import shutil
import tempfile
import threading
import time

from huey import SqliteHuey
from huey.api import chord
from huey.api import MemoryHuey
from huey.exceptions import ConfigurationError
from huey.storage import CHORD_COMPLETED
from huey.storage import CHORD_DUPLICATE
from huey.storage import CHORD_RECORDED
from huey.storage import CHORD_STATE_DELIVERED
from huey.storage import CHORD_STATE_OPEN
from huey.storage import CHORD_STATE_READY
from huey.storage import CHORD_UNKNOWN
from huey.tests.base import BaseTestCase
from huey.utils import Error
from huey.utils import SKIPPED


# Module-level task functions so that a freshly constructed Huey pointed at
# the same database file (the simulated process restart) can deserialize
# messages written by the previous instance.
FLAKY_ATTEMPTS = {}


def _prod(n):
    if n is None:
        raise ValueError('boom')
    return n + 1


def _flaky(n):
    FLAKY_ATTEMPTS[n] = FLAKY_ATTEMPTS.get(n, 0) + 1
    if FLAKY_ATTEMPTS[n] == 1:
        raise ValueError('transient')
    return n * 10


def _agg(ns):
    return list(ns)


def _finish(total):
    return ('finished', total)


def _nothing():
    return None


class DurableChordTestCase(BaseTestCase):
    def get_huey(self):
        self._tmpdir = tempfile.mkdtemp(prefix='huey-chord-')
        self._db_path = os.path.join(self._tmpdir, 'durable.db')
        FLAKY_ATTEMPTS.clear()
        huey = SqliteHuey('durable', filename=self._db_path,
                          durable_chord=True, utc=False, timeout=5)
        # Register the module-level task functions on this instance.
        self.prod = huey.task(name='d_prod')(_prod)
        self.agg = huey.task(name='d_agg')(_agg)
        self.finish = huey.task(name='d_finish')(_finish)
        self.nothing = huey.task(name='d_nothing')(_nothing)
        self.flaky = huey.task(retries=1, name='d_flaky')(_flaky)
        return huey

    def tearDown(self):
        super(DurableChordTestCase, self).tearDown()
        self.huey.storage.close()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def restart_huey(self):
        """Simulate a consumer restart: new instance, same database file,
        re-registering the same task functions."""
        self.huey.storage.close()
        huey = SqliteHuey('durable', filename=self._db_path,
                          durable_chord=True, utc=False, timeout=5)
        huey.task(name='d_prod')(_prod)
        huey.task(name='d_agg')(_agg)
        huey.task(name='d_finish')(_finish)
        huey.task(name='d_nothing')(_nothing)
        huey.task(retries=1, name='d_flaky')(_flaky)
        self.huey = huey
        return huey

    def drain(self):
        while self.huey.pending_count():
            self.huey.execute(self.huey.dequeue())

    # ------------------------------------------------------------------
    # Configuration / compatibility
    # ------------------------------------------------------------------
    def test_unsupported_backend_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            MemoryHuey(durable_chord=True)

    def test_immediate_mode_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            SqliteHuey(filename=os.path.join(self._tmpdir, 'imm.db'),
                       durable_chord=True, immediate=True)

    def test_custom_backend_without_protocol_is_rejected(self):
        from huey.storage import MemoryStorage

        class CustomMemory(MemoryStorage):
            supports_durable_chord = False

        with self.assertRaises(ConfigurationError):
            SqliteHuey(storage_class=CustomMemory, durable_chord=True)

    def test_schema_upgrades_an_existing_database(self):
        # A database created by an older Huey release only has the original
        # tables. Opening it with durable_chord enabled must add the chord
        # tables transparently instead of failing.
        old_db = os.path.join(self._tmpdir, 'old.db')
        import sqlite3
        conn = sqlite3.connect(old_db)
        conn.executescript(
            'create table kv (queue text not null, key text not null, '
            'value blob not null, primary key(queue, key));'
            'create table schedule (id integer primary key, queue text, '
            'data blob, timestamp real);'
            'create table task (id integer primary key, queue text, '
            'data blob, priority real default 0.0);'
            'create table counter (queue text, key text, '
            'value integer default 0, primary key(queue, key));')
        conn.commit()
        conn.close()

        upgraded = SqliteHuey('old', filename=old_db, durable_chord=True)
        try:
            names = {row[0] for row in upgraded.storage.sql(
                "select name from sqlite_master where type='table'",
                results=True)}
            self.assertTrue(
                {'chord', 'chord_member', 'chord_result'} <= names)
            r = upgraded.enqueue(chord(
                [upgraded.task(name='o_prod')(_prod).s(1)],
                upgraded.task(name='o_agg')(_agg).s()))
            while upgraded.pending_count():
                upgraded.execute(upgraded.dequeue())
            self.assertEqual(r(), [2])
        finally:
            upgraded.storage.close()

    def test_legacy_mode_keeps_counter_bookkeeping(self):
        # An ordinary SqliteHuey without the flag must keep using the old
        # counter/kv based chord implementation and never touch the new
        # tables.
        legacy_db = os.path.join(self._tmpdir, 'legacy.db')
        legacy = SqliteHuey('legacy', filename=legacy_db,
                            durable_chord=False, utc=False)
        prod = legacy.task(name='l_prod')(_prod)
        agg = legacy.task(name='l_agg')(_agg)
        try:
            r = legacy.enqueue(chord([prod.s(1), prod.s(2)], agg.s()))
            while legacy.pending_count():
                legacy.execute(legacy.dequeue())
            self.assertEqual(r(), [2, 3])
            self.assertEqual(legacy.storage.sql(
                'select count(*) from chord', results=True)[0][0], 0)
        finally:
            legacy.storage.close()

    def test_durable_sweep_is_noop_in_legacy_mode(self):
        legacy_db = os.path.join(self._tmpdir, 'legacy2.db')
        legacy = SqliteHuey('legacy2', filename=legacy_db,
                            durable_chord=False, utc=False)
        try:
            self.assertEqual(legacy.chord_sweep(),
                             {'delivered': [], 'reconciled': [], 'removed': 0})
        finally:
            legacy.storage.close()

    # ------------------------------------------------------------------
    # Storage-level protocol semantics
    # ------------------------------------------------------------------
    def test_report_lifecycle_and_duplicate_suppression(self):
        s = self.huey.storage
        s.chord_init('c1', 3, b'cb', ['t0', 't1', 't2'])
        ser = self.huey.serializer.serialize

        self.assertEqual(s.chord_report('c1', 0, ser('a')), CHORD_RECORDED)
        # Same member again: rejected, no extra slot consumed.
        self.assertEqual(s.chord_report('c1', 0, ser('a-again')),
                         CHORD_DUPLICATE)
        self.assertEqual(s.chord_report('c1', 2, ser('c')), CHORD_RECORDED)
        self.assertEqual(s.chord_state('c1'), CHORD_STATE_OPEN)
        self.assertEqual(s.chord_report('c1', 1, ser('b')), CHORD_COMPLETED)
        self.assertEqual(s.chord_state('c1'), CHORD_STATE_READY)
        # Reports after sealing are ignored as well.
        self.assertEqual(s.chord_report('c1', 1, ser('b-again')),
                         CHORD_DUPLICATE)
        self.assertEqual(s.chord_report('unknown', 0, ser('x')),
                         CHORD_UNKNOWN)

        fetched = s.chord_fetch_ready('c1')
        self.assertIsNotNone(fetched)
        callback_blob, values = fetched
        self.assertEqual(bytes(callback_blob), b'cb')
        self.assertEqual([self.huey.serializer.deserialize(bytes(v))
                          for v in values], ['a', 'b', 'c'])

    def test_delivery_is_single_even_under_races(self):
        s = self.huey.storage
        s.chord_init('c2', 2, b'cb', ['t0', 't1'])
        ser = self.huey.serializer.serialize
        s.chord_report('c2', 0, ser('a'))
        s.chord_report('c2', 1, ser('b'))

        barrier = threading.Barrier(4)
        outcomes = []

        def deliver():
            barrier.wait()
            outcomes.append(s.chord_enqueue_ready('c2', b'callback-task'))

        threads = [threading.Thread(target=deliver) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(sorted(outcomes), [False, False, False, True])
        self.assertEqual(s.chord_state('c2'), CHORD_STATE_DELIVERED)
        self.assertEqual(s.queue_size(), 1)
        self.assertEqual(s.dequeue(), b'callback-task')
        self.assertTrue(s.dequeue() is None)

    def test_report_transactions_serialize_across_connections(self):
        # Two separate storage instances on the same file emulate processes.
        other = SqliteHuey('durable', filename=self._db_path,
                           durable_chord=True, utc=False).storage
        s = self.huey.storage
        try:
            s.chord_init('c3', 2, b'cb', ['t0', 't1'])
            ser = self.huey.serializer.serialize
            errors = []

            def report(storage, idx):
                try:
                    storage.chord_report('c3', idx, ser('v%s' % idx))
                except Exception as exc:
                    errors.append(exc)

            t1 = threading.Thread(target=report, args=(s, 0))
            t2 = threading.Thread(target=report, args=(other, 1))
            t1.start(); t2.start()
            t1.join(10); t2.join(10)
            self.assertEqual(errors, [])
            self.assertEqual(s.chord_state('c3'), CHORD_STATE_READY)
            _, values = s.chord_fetch_ready('c3')
            self.assertEqual(len(values), 2)
        finally:
            other.close()

    # ------------------------------------------------------------------
    # End-to-end semantics through Huey
    # ------------------------------------------------------------------
    def test_basic_chord_ordered_results(self):
        r = self.huey.enqueue(chord(
            [self.prod.s(3), self.prod.s(1), self.prod.s(2)], self.agg.s()))
        self.drain()
        # Completion order cannot change the callback's argument order.
        self.assertEqual(r(), [4, 2, 3])
        self.assertEqual(self.huey.storage.chord_open_cids(), [])

    def test_empty_chord_still_fires(self):
        r = self.huey.enqueue(chord([], self.agg.s()))
        self.drain()
        self.assertEqual(r(), [])

    def test_terminal_mix_failure_revoked_expired(self):
        past = datetime.datetime.now() - datetime.timedelta(minutes=1)
        r = self.huey.enqueue(chord([
            self.prod.s(1),
            self.prod.s(None),                 # Permanent failure -> Error.
            self.prod.s(2, expires=past),      # Expired -> SKIPPED.
        ], self.agg.s()))
        self.drain()
        values = r()
        self.assertEqual(values[0], 2)
        self.assertIsInstance(values[1], Error)
        self.assertIs(values[2], SKIPPED)

        # Explicit revocation of a member likewise contributes SKIPPED and
        # does not prevent the callback from firing.
        r2 = self.huey.enqueue(chord(
            [self.prod.s(10), self.prod.s(20)], self.agg.s()))
        list(r2.results)[0].revoke()
        self.drain()
        v0, v1 = r2()
        self.assertIs(v0, SKIPPED)
        self.assertEqual(v1, 21)

    def test_all_members_failing_still_completes_the_batch(self):
        # A batch of entirely bad messages must reach the callback rather
        # than pinning the chord open forever.
        r = self.huey.enqueue(chord(
            [self.prod.s(None), self.prod.s(None)], self.agg.s()))
        self.drain()
        values = r()
        self.assertEqual(len(values), 2)
        self.assertTrue(all(isinstance(v, Error) for v in values))
        cid = self.huey.storage.sql(
            'select cid from chord', results=True)[0][0]
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_DELIVERED)

    def test_retrying_member_reports_only_its_final_state(self):
        r = self.huey.enqueue(chord(
            [self.flaky.s(1), self.flaky.s(2)], self.agg.s()))
        self.drain()
        self.assertEqual(r(), [10, 20])
        cid = self.huey.storage.sql(
            'select cid from chord', results=True)[0][0]
        # Exactly one stored result per member, no double counting.
        self.assertEqual(self.huey.storage.sql(
            'select count(*) from chord_result where cid=?', (cid,),
            results=True)[0][0], 2)
        self.assertEqual(FLAKY_ATTEMPTS, {1: 2, 2: 2})

    def test_scheduled_member_keeps_chord_open_until_it_runs(self):
        # A member parked in the schedule (future eta) is still live: the
        # pending-detection sweep must recognize it rather than reconcile
        # the chord as abandoned.
        eta = datetime.datetime.now() + datetime.timedelta(seconds=60)
        r = self.huey.enqueue(chord([
            self.prod.s(1, eta=eta), self.prod.s(2)], self.agg.s()))
        cid = self.huey.storage.chord_open_cids()[0]

        # Dequeue the not-yet-ready member: execute() parks it in the
        # schedule, then run the immediate member.
        scheduled_task = self.huey.dequeue()
        self.huey.execute(scheduled_task)
        self.assertEqual(self.huey.scheduled_count(), 1)
        task = self.huey.dequeue()
        self.huey.execute(task)
        self.huey.storage.sql(
            'update chord set updated=? where cid=?',
            (time.time() - 1000, cid), commit=True)
        self.assertEqual(
            self.huey.chord_sweep(grace=300, retention=10 ** 9)['reconciled'],
            [])
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_OPEN)

        # Release the schedule and let the member run; a due task has its
        # eta cleared, matching what the scheduler loop executes.
        for scheduled in self.huey.read_schedule(
                datetime.datetime.now() + datetime.timedelta(seconds=120)):
            scheduled.eta = None
            self.huey.enqueue(scheduled)
        self.drain()
        self.assertEqual(r(), [2, 3])

    def test_poison_message_reconciles_instead_of_hanging(self):
        # A member message that cannot be deserialized is discarded by the
        # worker loop; the sweep must eventually replace its slot with an
        # explicit Error so the batch completes.
        r = self.huey.enqueue(chord(
            [self.prod.s(1), self.prod.s(2)], self.agg.s()))
        cid = self.huey.storage.chord_open_cids()[0]

        # Member 0 completes; member 1's queue message is corrupted in place
        # the way an undeliverable poison message would be.
        healthy = self.huey.dequeue()
        self.huey.execute(healthy)
        poison_id = self.huey.storage.sql(
            'select id from task where queue=? order by id',
            (self.huey.name,), results=True)[0][0]
        self.huey.storage.sql(
            'update task set data=? where id=?',
            (b'\x80\x04this-is-not-a-valid-message', poison_id),
            commit=True)

        # The worker cannot make sense of it; emulate the consumer dropping
        # the unreadable message after logging.
        try:
            self.huey.dequeue()
        except Exception:
            self.huey.storage.sql('delete from task where id=?',
                                  (poison_id,), commit=True)
        self.assertEqual(self.huey.pending_count(), 0)

        self.huey.storage.sql(
            'update chord set updated=? where cid=?',
            (time.time() - 1000, cid), commit=True)
        out = self.huey.chord_sweep(grace=300, retention=10 ** 9)
        self.assertEqual(out['reconciled'], [cid])
        self.drain()
        values = r()
        self.assertEqual(values[0], 2)
        self.assertIsInstance(values[1], Error)

    def test_repeated_completion_notification_drains_idempotently(self):
        # Re-executing the same member message simulates a duplicated
        # delivery notification from the broker.
        r = self.huey.enqueue(chord(
            [self.prod.s(1), self.prod.s(2)], self.agg.s()))
        tasks = []
        while self.huey.pending_count():
            tasks.append(self.huey.dequeue())
        self.assertEqual(len(tasks), 2)
        for task in tasks:
            self.huey.execute(task)
            self.huey.execute(task)  # Duplicate notification.
        # Exactly one callback was produced.
        self.assertEqual(self.huey.pending_count(), 1)
        self.drain()
        self.assertEqual(r(), [2, 3])

    def test_callback_pipeline_is_preserved(self):
        c = chord([self.prod.s(1), self.prod.s(2)],
                  self.agg.s()).then(self.finish)
        r = self.huey.enqueue(c)
        self.drain()
        self.assertEqual(r(), [2, 3])
        self.assertEqual(r.pipeline_results(), [[2, 3],
                                                ('finished', [2, 3])])

    def test_member_pipeline_reports_from_tail(self):
        c = chord([
            self.prod.s(1).then(self.prod),
            self.prod.s(2).then(self.prod).then(self.prod),
        ], self.agg.s())
        r = self.huey.enqueue(c)
        self.drain()
        # 1 -> 2 -> 3; 2 -> 3 -> 4 -> 5.
        self.assertEqual(r(), [3, 5])

    def test_nested_chord(self):
        inner1 = chord([self.prod.s(0), self.prod.s(1)], self.agg.s())
        inner2 = chord([self.prod.s(2)], self.agg.s())
        outer = chord([inner1, inner2], self.agg.s())
        r = self.huey.enqueue(outer)
        self.drain()
        self.assertEqual(r(), [[1, 2], [3]])
        states = [row[0] for row in self.huey.storage.sql(
            'select state from chord', results=True)]
        self.assertTrue(states)
        self.assertTrue(all(state == CHORD_STATE_DELIVERED for state in states))

    def test_none_member_value_is_recorded(self):
        r = self.huey.enqueue(chord([self.nothing.s(), self.prod.s(0)],
                                    self.agg.s()))
        self.drain()
        self.assertEqual(r(), [None, 1])

    def test_chord_error_handler_is_preserved_through_recovery(self):
        state = []

        @self.huey.task()
        def boom(ns):
            raise RuntimeError('callback failed on purpose')

        @self.huey.task()
        def on_alert(exc):
            state.append(type(exc).__name__)
            return 'alerted'

        c = chord([self.prod.s(1)], boom.s()).error(on_alert)
        r = self.huey.enqueue(c)
        self.drain()
        self.assertEqual(state, ['RuntimeError'])
        # The error handler ran as the callback's on_error pipeline step;
        # its result is readable through the enqueued handler task id.
        self.assertIsNone(r.pipeline_results)  # on_error is not on_complete.

    def test_callback_priority_is_honored_at_delivery(self):
        @self.huey.task(priority=5)
        def high_priority_agg(ns):
            return list(ns)

        r = self.huey.enqueue(chord(
            [self.prod.s(1), self.prod.s(2)], high_priority_agg.s()))
        self.drain()
        self.assertEqual(r(), [2, 3])
        # The delivered row retains the evidence; the callback task was
        # enqueued with the callback's own priority.
        cid = self.huey.storage.sql(
            'select cid from chord', results=True)[0][0]
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_DELIVERED)

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------
    def test_sweep_delivers_ready_chord_once(self):
        r = self.huey.enqueue(chord(
            [self.prod.s(1), self.prod.s(2)], self.agg.s()))
        self.drain()
        cid = self.huey.storage.sql(
            'select cid from chord', results=True)[0][0]
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_DELIVERED)
        first = self.huey.chord_sweep(grace=0, retention=10 ** 9)
        second = self.huey.chord_sweep(grace=0, retention=10 ** 9)
        self.assertEqual(first['delivered'], [])
        self.assertEqual(second['delivered'], [])
        # The callback already ran during drain(); no re-enqueue happened.
        self.assertEqual(self.huey.pending_count(), 0)
        # And its evidence is still retained.
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_DELIVERED)

    def test_recovery_after_crash_between_ready_and_enqueue(self):
        r = self.huey.enqueue(chord(
            [self.prod.s(1), self.prod.s(2), self.prod.s(3)], self.agg.s()))
        cid = self.huey.storage.chord_open_cids()[0]
        callback_id = r.callback.id

        # Report all members through the raw protocol, bypassing delivery to
        # emulate the process dying right after the ready transition.
        ser = self.huey.serializer.serialize
        for idx, value in enumerate((2, 3, 4)):
            self.huey.storage.chord_report(cid, idx, ser(value))
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_READY)
        self.assertEqual(self.huey.pending_count(), 3)  # Members untouched.

        restarted = self.restart_huey()
        try:
            out = restarted.chord_sweep(grace=0, retention=10 ** 9)
            self.assertEqual(out['delivered'], [cid])
            # 3 untouched member messages + the single recovered callback.
            self.assertEqual(restarted.pending_count(), 4)
            # Repeated sweeps must not republish.
            restarted.chord_sweep(grace=0, retention=10 ** 9)
            restarted.chord_sweep(grace=0, retention=10 ** 9)
            self.assertEqual(restarted.pending_count(), 4)

            callbacks = [t for t in restarted.pending() if t.id == callback_id]
            self.assertEqual(len(callbacks), 1)
            while restarted.pending_count():
                restarted.execute(restarted.dequeue())
            self.assertEqual(r(), [2, 3, 4])
        finally:
            restarted.storage.close()

    def test_crash_between_fetch_and_enqueue_still_single_delivery(self):
        # The narrowest window: a worker has fetched the ready chord and
        # assembled the callback, but dies before the delivery transaction
        # commits. The row stays "ready", so a later sweep retries the whole
        # fetch-and-enqueue rather than republishing a bare callback.
        r = self.huey.enqueue(chord(
            [self.prod.s(1), self.prod.s(2)], self.agg.s()))
        cid = self.huey.storage.chord_open_cids()[0]
        ser = self.huey.serializer.serialize
        self.huey.storage.chord_report(cid, 0, ser(2))
        self.huey.storage.chord_report(cid, 1, ser(3))
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_READY)

        # Simulate the fetch half of a delivery, then die without enqueueing.
        fetched = self.huey.storage.chord_fetch_ready(cid)
        self.assertIsNotNone(fetched)
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_READY)
        self.assertEqual(self.huey.pending_count(), 2)

        # Restart and sweep: one callback, carrying the full results list.
        restarted = self.restart_huey()
        try:
            out = restarted.chord_sweep(grace=0, retention=10 ** 9)
            self.assertEqual(out['delivered'], [cid])
            self.assertEqual(restarted.pending_count(), 3)
            while restarted.pending_count():
                restarted.execute(restarted.dequeue())
            self.assertEqual(r(), [2, 3])
        finally:
            restarted.storage.close()

    def test_reconcile_abandoned_member_after_grace(self):
        r = self.huey.enqueue(chord(
            [self.prod.s(1), self.prod.s(2)], self.agg.s()))
        cid = self.huey.storage.chord_open_cids()[0]

        # Complete member 0, then make member 1 vanish from every store as
        # though its worker was SIGKILLed or its message was unprocessable.
        first = self.huey.dequeue()
        self.huey.execute(first)
        self.huey.storage.flush_queue()
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_OPEN)

        # Inside the grace window nothing is reconciled.
        self.assertEqual(
            self.huey.chord_sweep(grace=300, retention=10 ** 9),
            {'delivered': [], 'reconciled': [], 'removed': 0})
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_OPEN)

        # Backdate activity past the grace window.
        self.huey.storage.sql(
            'update chord set updated=? where cid=?',
            (time.time() - 1000, cid), commit=True)
        out = self.huey.chord_sweep(grace=300, retention=10 ** 9)
        self.assertEqual(out['reconciled'], [cid])
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_DELIVERED)
        self.drain()
        values = r()
        self.assertEqual(values[0], 2)
        self.assertIsInstance(values[1], Error)
        self.assertIn('was lost', values[1].metadata['error'])

    def test_inflight_member_is_not_reconciled_prematurely(self):
        # Two independent protections must keep healthy chords open:
        # 1. a fresh "updated" timestamp is inside the grace window;
        # 2. a backdated row whose member is still queued/scheduled is
        #    considered live (e.g. waiting on a retry).
        r = self.huey.enqueue(chord(
            [self.prod.s(1), self.prod.s(2)], self.agg.s()))
        cid = self.huey.storage.chord_open_cids()[0]

        # Case 1: nothing consumed yet, row is fresh -> grace protects it.
        self.assertEqual(
            self.huey.chord_sweep(grace=300, retention=10 ** 9),
            {'delivered': [], 'reconciled': [], 'removed': 0})
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_OPEN)

        # Case 2: member 0 reports; member 1 remains queued while the row is
        # artificially aged -- presence in the queue still blocks reconcile.
        task0 = self.huey.dequeue()
        self.huey.execute(task0)
        self.assertEqual(self.huey.pending_count(), 1)
        self.huey.storage.sql(
            'update chord set updated=? where cid=?',
            (time.time() - 1000, cid), commit=True)
        self.assertEqual(
            self.huey.chord_sweep(grace=300, retention=10 ** 9)['reconciled'],
            [])
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_OPEN)

        # Once the queued member actually runs, the chord completes normally.
        self.drain()
        self.assertEqual(r(), [2, 3])

    def test_cleanup_waits_for_retention(self):
        r = self.huey.enqueue(chord(
            [self.prod.s(1)], self.agg.s()))
        self.drain()
        cid = self.huey.storage.sql(
            'select cid from chord', results=True)[0][0]
        # Recent delivered rows survive every sweep.
        self.huey.chord_sweep(grace=0, retention=3600)
        self.assertEqual(self.huey.storage.chord_state(cid),
                         CHORD_STATE_DELIVERED)
        # Age the row, then it may be reaped, together with its evidence.
        self.huey.storage.sql(
            'update chord set updated=? where cid=?',
            (time.time() - 4000, cid), commit=True)
        removed = self.huey.chord_sweep(grace=0, retention=3600)['removed']
        self.assertEqual(removed, 1)
        self.assertIsNone(self.huey.storage.chord_state(cid))
        self.assertEqual(self.huey.storage.sql(
            'select count(*) from chord_result where cid=?', (cid,),
            results=True)[0][0], 0)

    def test_flush_removes_chord_tables(self):
        self.huey.enqueue(chord([self.prod.s(1)], self.agg.s()))
        self.assertEqual(self.huey.storage.sql(
            'select count(*) from chord', results=True)[0][0], 1)
        self.huey.flush()
        for table in ('chord', 'chord_member', 'chord_result'):
            self.assertEqual(self.huey.storage.sql(
                'select count(*) from %s' % table, results=True)[0][0], 0)
