"""
Multi-process and consumer-integration tests for the recoverable chord.

These tests deliberately use real subprocesses / forked worker processes and
a shared SQLite database file rather than mocks, because the guarantee being
tested ("exactly one callback delivery per database") only exists at the
SQLite transaction boundary between independent processes.
"""
import multiprocessing
import os
import shutil
import sys
import tempfile
import time
import unittest

from huey import SqliteHuey
from huey import chord
from huey.consumer import WORKER_PROCESS
from huey.tests.base import BaseTestCase
from huey.utils import Error


# Module-level task functions: forked workers and subprocesses resolve task
# classes through the registry by their module-qualified names.
BARRIER_DIR = None


def _mp_probe(name):
    """Touch a per-call file so the test can count real executions."""
    path = os.path.join(BARRIER_DIR, name + '.count')
    with open(path, 'a') as fh:
        fh.write('x')
    return name


def _mp_agg(ns):
    path = os.path.join(BARRIER_DIR, 'agg.count')
    with open(path, 'a') as fh:
        fh.write('x')
    return list(ns)


def _mp_slow_probe(name):
    # Coordinate two workers so they finish the last two members at the
    # same instant.
    gate = os.path.join(BARRIER_DIR, 'gate')
    ready = os.path.join(BARRIER_DIR, 'ready-%s' % name)
    open(ready, 'w').close()
    deadline = time.time() + 10
    while not os.path.exists(gate) and time.time() < deadline:
        time.sleep(0.005)
    return _mp_probe(name)


def _subprocess_worker(db_path, queue_name, max_tasks):
    """Standalone worker loop used by the subprocess race test."""
    huey = SqliteHuey(queue_name, filename=db_path, durable_chord=True,
                      utc=False, timeout=10)
    for name, fn in (('mp_probe', _mp_probe),
                     ('mp_agg', _mp_agg),
                     ('mp_slow_probe', _mp_slow_probe)):
        huey.task(name=name)(fn)
    executed = 0
    idle_since = None
    while executed < max_tasks:
        task = huey.dequeue()
        if task is None:
            if idle_since is None:
                idle_since = time.time()
            elif time.time() - idle_since > 15:
                break
            time.sleep(0.01)
            continue
        idle_since = None
        huey.execute(task)
        executed += 1
    huey.storage.close()


class MultiProcessDurableChordTestCase(BaseTestCase):
    def get_huey(self):
        self._tmpdir = tempfile.mkdtemp(prefix='huey-mp-chord-')
        self._db_path = os.path.join(self._tmpdir, 'mp.db')
        global BARRIER_DIR
        BARRIER_DIR = self._tmpdir
        huey = SqliteHuey('mpchord', filename=self._db_path,
                          durable_chord=True, utc=False, timeout=10)
        self.probe = huey.task(name='mp_probe')(_mp_probe)
        self.slow_probe = huey.task(name='mp_slow_probe')(_mp_slow_probe)
        self.agg = huey.task(name='mp_agg')(_mp_agg)
        return huey

    def tearDown(self):
        super(MultiProcessDurableChordTestCase, self).tearDown()
        self.huey.storage.close()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _count(self, name):
        path = os.path.join(self._tmpdir, name + '.count')
        if not os.path.exists(path):
            return 0
        with open(path) as fh:
            return len(fh.read())

    @unittest.skipIf(sys.platform == 'win32', 'requires fork()')
    def test_two_processes_race_last_members_single_callback(self):
        # Two independent processes hammer the same database, each consuming
        # members of a four-member chord and racing at the join. Five tasks
        # exist in total: four members plus one callback.
        self.huey.enqueue(chord(
            [self.slow_probe.s('a'), self.slow_probe.s('b'),
             self.slow_probe.s('c'), self.slow_probe.s('d')],
            self.agg.s()))
        self.assertEqual(self.huey.pending_count(), 4)

        ctx = multiprocessing.get_context('fork')
        procs = []
        for _ in range(2):
            p = ctx.Process(target=_subprocess_worker,
                            args=(self._db_path, 'mpchord', 5))
            p.start()
            procs.append(p)

        # Wait until both workers have dequeued and parked inside a member
        # task, then release the gate so the in-flight members finish at the
        # same instant.
        deadline = time.time() + 10
        while (len([f for f in os.listdir(self._tmpdir)
                    if f.startswith('ready-')]) < 2
               and time.time() < deadline):
            time.sleep(0.01)
        # Give both workers time to be parked inside the task.
        time.sleep(0.2)
        open(os.path.join(self._tmpdir, 'gate'), 'w').close()

        for p in procs:
            p.join(20)
        for p in procs:
            self.assertFalse(p.is_alive(), 'worker process hung')
            self.assertEqual(p.exitcode, 0)

        # Every member ran once; the aggregator callback ran exactly once.
        self.assertEqual(self._count('a'), 1)
        self.assertEqual(self._count('b'), 1)
        self.assertEqual(self._count('c'), 1)
        self.assertEqual(self._count('d'), 1)
        self.assertEqual(self._count('agg'), 1)
        self.assertEqual(self.huey.pending_count(), 0)

    @unittest.skipIf(sys.platform == 'win32', 'requires fork()')
    def test_consumer_process_workers_end_to_end(self):
        # The actual Consumer with forked worker processes, the production
        # execution path for multi-process deployments.
        r = self.huey.enqueue(chord(
            [self.probe.s('x'), self.probe.s('y'), self.probe.s('z')],
            self.agg.s()))
        consumer = self.huey.create_consumer(
            workers=3, worker_type=WORKER_PROCESS, periodic=False,
            initial_delay=0.01, max_delay=0.05, check_worker_health=False,
            chord_sweep_interval=1, chord_sweep_grace=1)
        consumer.start()
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                if self._count('agg'):
                    break
                time.sleep(0.05)
            self.assertEqual(self._count('agg'), 1)
            for name in ('x', 'y', 'z'):
                self.assertEqual(self._count(name), 1)
            self.assertEqual(r.get(blocking=True, timeout=5),
                             ['x', 'y', 'z'])
        finally:
            consumer.stop(graceful=True)

    @unittest.skipIf(sys.platform == 'win32', 'requires fork()')
    def test_consumer_restart_recovers_callback_after_hard_kill(self):
        # Reproduce the business-case crash deterministically: a worker dies
        # at the join, leaving a fully-reported chord whose callback never
        # reached the queue, plus one abandoned open chord. A fresh consumer
        # process must recover both on startup, each with exactly one
        # callback delivery.
        ready_r = self.huey.enqueue(chord(
            [self.probe.s('p'), self.probe.s('q')], self.agg.s()))
        ready_cid = self.huey.storage.chord_open_cids()[0]
        ser = self.huey.serializer
        for idx, value in enumerate(('p', 'q')):
            self.huey.storage.chord_report(
                ready_cid, idx, ser.serialize(value))
        self.assertEqual(self.huey.storage.chord_state(ready_cid), 'ready')
        # Remove the member queue messages, as a hard-killed worker would
        # leave behind nothing for them.
        self.huey.storage.flush_queue()

        open_r = self.huey.enqueue(chord(
            [self.probe.s('u'), self.probe.s('v')], self.agg.s()))
        open_cid = self.huey.storage.chord_open_cids()[0]
        # Member 'u' reports normally; member 'v' is lost with its worker.
        self.huey.storage.chord_report(
            open_cid, 0, ser.serialize('u'))
        self.huey.storage.flush_queue()
        self.huey.storage.sql(
            'update chord set updated=? where cid=?',
            (time.time() - 1000, open_cid), commit=True)

        self.assertEqual(self._count('agg'), 0)

        # A brand-new consumer over the same file runs the startup sweep and
        # then processes the recovered callback tasks in forked workers.
        consumer = self.huey.create_consumer(
            workers=2, worker_type=WORKER_PROCESS, periodic=False,
            initial_delay=0.01, max_delay=0.05, check_worker_health=False,
            chord_sweep_grace=1, chord_sweep_interval=1)
        consumer.start()
        try:
            deadline = time.time() + 20
            while time.time() < deadline and self._count('agg') < 2:
                time.sleep(0.05)
            # Exactly one delivery per chord -- never a duplicated callback.
            self.assertEqual(self._count('agg'), 2)
            time.sleep(0.5)
            self.assertEqual(self._count('agg'), 2)

            ready_values = ready_r.get(blocking=True, timeout=5)
            self.assertEqual(ready_values, ['p', 'q'])
            open_values = open_r.get(blocking=True, timeout=5)
            self.assertEqual(open_values[0], 'u')
            self.assertIsInstance(open_values[1], Error)
        finally:
            consumer.stop(graceful=True)
        # Shutdown and any later periodic sweep cannot republish either.
        self.assertEqual(self._count('agg'), 2)
        self.assertEqual(self.huey.pending_count(), 0)
