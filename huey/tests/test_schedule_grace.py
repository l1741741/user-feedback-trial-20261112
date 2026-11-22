"""
Demonstrates the optional fault-tolerance window (schedule_grace) for the
SQLite schedule.

Scenarios covered, all with a controlled clock (explicit datetimes, no
monkey-patching of time):

* tasks on both sides of the grace deadline: in-window tasks are dispatched
  immediately, past-window tasks go to the ops-readable schedule_log table.
* two concurrent scheduler processes racing to scan the same database file:
  every due task is dispatched or skipped exactly once.
* a scheduler process exiting after decisions were recorded: a new scheduler
  never re-dispatches tasks that were already dispatched or skipped, even if
  the same schedule rows re-appear.
* a queue that does not enable the window keeps the legacy behavior of
  dispatching every due task, however stale.
"""
import datetime
import multiprocessing
import os
import threading
import unittest

from huey.api import SqliteHuey
from huey.consumer import Scheduler
from huey.tests import TEST_TMP_DIR


GRACE = 300.0  # Fault-tolerance window, in seconds.
NOW = datetime.datetime(2026, 11, 22, 3, 0, 0)  # Controlled "now".
QUEUE = 'grace-demo'


def nightly_batch(job_id):
    return job_id


def make_huey(db_file, grace=GRACE, name=QUEUE):
    # utc=False keeps etas and the scheduler clock in a single naive frame,
    # so the controlled clock is independent of the host time zone.
    huey = SqliteHuey(name=name, filename=db_file, schedule_grace=grace,
                      timeout=30, utc=False)
    return huey, huey.task()(nightly_batch)


def scheduler_process_main(db_file, grace, now, start_event):
    # Entry point for a standalone scheduler process sharing the db file.
    huey, _ = make_huey(db_file, grace)
    scheduler = Scheduler(huey, threading.Event(), interval=1, periodic=False)
    try:
        start_event.wait(30)
        scheduler.loop(now=now)
    finally:
        huey.storage.close()


class ScheduleGraceTestCase(unittest.TestCase):
    def setUp(self):
        self.db_file = os.path.join(
            TEST_TMP_DIR, 'grace-%s.db' % self.id().rsplit('.', 1)[-1])
        self.huey, self.task = make_huey(self.db_file)
        self.storage = self.huey.storage

    def tearDown(self):
        self.huey.storage.close()

    def schedule(self, *jobs):
        # jobs: list of (job_id, eta) tuples. Returns {job_id: task}.
        tasks = {}
        for job_id, eta in jobs:
            task = self.task.s(job_id, eta=eta)
            self.huey.add_schedule(task)
            tasks[job_id] = task
        return tasks

    def read_ids(self, now, huey=None):
        huey = huey or self.huey
        return [task.args[0] for task in
                huey._deserialize_all(huey.storage.read_schedule(now))]

    def log_decisions(self, decision=None, huey=None):
        huey = huey or self.huey
        entries = huey.storage.schedule_log_entries(decision)
        return [(huey.deserialize_task(data).args[0], eta, dec, at)
                for data, eta, dec, at in entries]

    def enqueued_ids(self, huey=None):
        huey = huey or self.huey
        return [task.args[0] for task in
                huey._deserialize_all(huey.storage.enqueued_items())]

    def run_schedulers(self, now, count=1, grace=GRACE):
        ctx = multiprocessing.get_context('spawn')
        start = ctx.Event()
        procs = [ctx.Process(target=scheduler_process_main,
                             args=(self.db_file, grace, now, start))
                 for _ in range(count)]
        for proc in procs:
            proc.start()
        start.set()
        try:
            for proc in procs:
                proc.join(60)
                self.assertEqual(proc.exitcode, 0)
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                    proc.join()

    def test_within_window_dispatched(self):
        eta = NOW - datetime.timedelta(seconds=GRACE - 1)
        future = NOW + datetime.timedelta(hours=1)
        tasks = self.schedule(('stale-ok', eta), ('future', future))

        self.assertEqual(self.read_ids(NOW), ['stale-ok'])
        # The dispatch decision is recorded with the scan time.
        self.assertEqual(
            self.log_decisions(),
            [('stale-ok', eta.timestamp(), 'dispatched', NOW.timestamp())])
        # The not-yet-due task is untouched and has no log entry.
        self.assertEqual(self.storage.scheduled_items(),
                         [self.huey.serialize_task(tasks['future'])])

    def test_past_window_recorded_not_dispatched(self):
        eta = NOW - datetime.timedelta(seconds=GRACE + 1)
        self.schedule(('too-late', eta))

        self.assertEqual(self.read_ids(NOW), [])
        self.assertEqual(self.storage.schedule_size(), 0)
        # The skipped task lands in the ops-readable record: which task, its
        # original eta, the decision and when it was made.
        self.assertEqual(
            self.log_decisions(),
            [('too-late', eta.timestamp(), 'skipped', NOW.timestamp())])
        self.assertEqual(self.log_decisions('dispatched'), [])

    def test_grace_zero_boundary(self):
        huey, task = make_huey(self.db_file, grace=0)
        try:
            on_time = task.s('on-time', eta=NOW)
            late = task.s('late', eta=NOW - datetime.timedelta(microseconds=1))
            huey.add_schedule(on_time)
            huey.add_schedule(late)

            self.assertEqual(self.read_ids(NOW, huey), ['on-time'])
            self.assertEqual(
                [d[:3] for d in self.log_decisions(huey=huey)],
                [('late', late.eta.timestamp(), 'skipped'),
                 ('on-time', NOW.timestamp(), 'dispatched')])
        finally:
            huey.storage.close()

    def test_window_disabled_keeps_legacy_behavior(self):
        # Two queues share one database file. The legacy queue does not
        # enable the window and dispatches everything due, however stale.
        legacy, legacy_task = make_huey(self.db_file, grace=None,
                                        name='legacy')
        try:
            eta = NOW - datetime.timedelta(days=1)
            self.schedule(('grace-job', eta))
            stale = legacy_task.s('legacy-job', eta=eta)
            legacy.add_schedule(stale)

            self.assertEqual(self.read_ids(NOW, legacy), ['legacy-job'])
            self.assertEqual(legacy.storage.schedule_log_entries(), [])

            self.assertEqual(self.read_ids(NOW), [])
            self.assertEqual([d[:3] for d in self.log_decisions()],
                             [('grace-job', eta.timestamp(), 'skipped')])
        finally:
            legacy.storage.close()

    def test_restart_does_not_redispatch(self):
        eta_stale = NOW - datetime.timedelta(seconds=GRACE + 60)
        eta_fresh = NOW - datetime.timedelta(seconds=10)
        tasks = self.schedule(('stale', eta_stale), ('fresh', eta_fresh))
        blobs = dict((job_id, self.huey.serialize_task(task))
                     for job_id, task in tasks.items())

        # First run: fresh task dispatched, stale task skipped.
        self.assertEqual(self.read_ids(NOW), ['fresh'])
        self.assertEqual(len(self.storage.schedule_log_entries()), 2)
        self.huey.storage.close()

        # The scheduler restarts and the same schedule rows re-appear (e.g.
        # they were republished while it was down). The recorded decisions
        # prevent both re-dispatch of the skipped task and a second dispatch
        # of the already-dispatched one.
        huey2, _ = make_huey(self.db_file)
        try:
            huey2.storage.add_to_schedule(blobs['stale'], eta_stale)
            huey2.storage.add_to_schedule(blobs['fresh'], eta_fresh)
            later = NOW + datetime.timedelta(seconds=60)
            self.assertEqual(self.read_ids(later, huey2), [])
            self.assertEqual(huey2.storage.schedule_size(), 0)
            self.assertEqual(
                [d[:3] for d in self.log_decisions(huey=huey2)],
                [('stale', eta_stale.timestamp(), 'skipped'),
                 ('fresh', eta_fresh.timestamp(), 'dispatched')])
        finally:
            huey2.storage.close()
        self.huey = huey2  # For tearDown.

    def test_invalid_grace_rejected(self):
        self.assertRaises(ValueError, SqliteHuey, filename=self.db_file,
                          schedule_grace=-1)

    def test_two_schedulers_scan_race(self):
        n = 50
        eta_in = NOW - datetime.timedelta(seconds=10)  # Within the window.
        eta_out = NOW - datetime.timedelta(seconds=GRACE + 60)  # Past it.
        self.schedule(
            *[(i, eta_in) for i in range(n)],
            *[(n + i, eta_out) for i in range(n)])

        # Two scheduler processes scan the same database simultaneously.
        self.run_schedulers(NOW, count=2)

        # Every in-window task was dispatched exactly once across both
        # processes; every past-window task was skipped exactly once.
        self.assertEqual(sorted(self.enqueued_ids()), list(range(n)))
        self.assertEqual(
            sorted(job_id for job_id, _, _, _
                   in self.log_decisions('dispatched')),
            list(range(n)))
        self.assertEqual(
            sorted(job_id for job_id, _, _, _
                   in self.log_decisions('skipped')),
            list(range(n, 2 * n)))
        self.assertEqual(self.storage.schedule_size(), 0)

    def test_scheduler_exit_and_restart(self):
        eta_stale = NOW - datetime.timedelta(seconds=GRACE + 60)
        eta_fresh = NOW - datetime.timedelta(seconds=10)
        tasks = self.schedule(('stale', eta_stale), ('fresh', eta_fresh))
        blobs = dict((job_id, self.huey.serialize_task(task))
                     for job_id, task in tasks.items())

        # Scheduler process runs once and exits (e.g. nightly stop).
        self.run_schedulers(NOW)
        self.assertEqual(self.enqueued_ids(), ['fresh'])
        self.assertEqual(len(self.storage.schedule_log_entries()), 2)

        # Workers drain the dispatched task before the next run.
        self.storage.flush_queue()

        # While the scheduler was down the same rows were republished. A new
        # scheduler process must not re-dispatch either of them.
        self.storage.add_to_schedule(blobs['stale'], eta_stale)
        self.storage.add_to_schedule(blobs['fresh'], eta_fresh)
        self.run_schedulers(NOW + datetime.timedelta(seconds=60))

        self.assertEqual(self.enqueued_ids(), [])
        self.assertEqual(self.storage.schedule_size(), 0)
        self.assertEqual(
            [d[:3] for d in self.log_decisions()],
            [('stale', eta_stale.timestamp(), 'skipped'),
             ('fresh', eta_fresh.timestamp(), 'dispatched')])


if __name__ == '__main__':
    unittest.main()
