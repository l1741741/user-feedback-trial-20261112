"""
Multi-process tests for timezone-aware periodic scheduling.

These tests spawn *real* OS subprocesses (via the
``_tz_multiprocess_driver`` module) against one shared SQLite file. They do
not access the network and do not change the system clock: the current time
is injected per process through the ``HUEY_TZ_NOW`` environment variable.

Coverage required by the business case:
* a paused/restarted consumer honors the explicit catch-up window and never
  re-delivers by re-checking the same minute (real restart with a fresh
  interpreter process);
* two schedulers sharing one SQLite database produce exactly one delivery for
  a given planned occurrence (dual-scheduler race);
* an ordinary day-of-month boundary is handled correctly.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


DRIVER = 'huey.tests._tz_multiprocess_driver'
REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..'))


class MultiprocessTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='huey-tz-mp-')
        self.db = os.path.join(self.tmp, 'huey.db')
        self.results = os.path.join(self.tmp, 'results.log')
        self.sentinel = os.path.join(self.tmp, 'stop')
        self.pidfile = os.path.join(self.tmp, 'pids')
        open(self.results, 'w').close()

    def tearDown(self):
        if os.path.exists(self.sentinel):
            os.unlink(self.sentinel)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _base_env(self, now, **extra):
        env = dict(os.environ)
        env.update({
            'HUEY_TZ_NOW': now.isoformat(),
            'HUEY_TZ_DB': self.db,
            'HUEY_TZ_RESULTS': self.results,
        })
        env.update(extra)
        return env

    def run_driver(self, mode, now, env_extra=None, timeout=30):
        env = self._base_env(now, HUEY_TZ_MODE=mode)
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(
            [sys.executable, '-I', '-m', DRIVER],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            timeout=timeout)
        if proc.returncode != 0:
            self.fail('driver %s failed rc=%s\nstdout=%s\nstderr=%s' % (
                mode, proc.returncode, proc.stdout, proc.stderr))
        return proc.stdout.strip().splitlines()

    def read_results(self):
        with open(self.results) as fh:
            return [line.strip() for line in fh if line.strip()]

    def spawn_serve(self, now):
        env = self._base_env(
            now, HUEY_TZ_MODE='serve', HUEY_TZ_PIDFILE=self.pidfile,
            HUEY_TZ_SENTINEL=self.sentinel)
        log = open(os.path.join(self.tmp, 'serve.log'), 'a')
        proc = subprocess.Popen(
            [sys.executable, '-I', '-m', DRIVER],
            cwd=REPO_ROOT, env=env, stdout=log, stderr=log)
        return proc, log

    def wait_for_pids(self, count, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(self.pidfile):
                with open(self.pidfile) as fh:
                    pids = [l.strip() for l in fh if l.strip()]
                if len(pids) >= count:
                    return pids
            time.sleep(0.1)
        self.fail('timed out waiting for %d pids' % count)


class DualSchedulerTest(MultiprocessTestBase):
    def test_two_schedulers_one_delivery(self):
        # Two independent scheduler processes tick the same fake minute over
        # the same SQLite file. The atomic claim table must collapse this to
        # exactly one delivery.
        now = __import__('datetime').datetime(2026, 6, 1, 6, 31)
        procs = [
            subprocess.Popen(
                [sys.executable, '-I', '-m', DRIVER],
                cwd=REPO_ROOT,
                env=self._base_env(now, HUEY_TZ_MODE='tick'),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            for _ in range(2)]
        outputs = [p.communicate(timeout=30) for p in procs]
        for p, (out, err) in zip(procs, outputs):
            self.assertEqual(p.returncode, 0, err)
        delivered = [line for out, _ in outputs for line in
                     out.strip().splitlines() if line]
        self.assertEqual(len(delivered), 1, delivered)
        self.assertEqual(delivered, ['20260601T063000#0'])
        # The task body ran once with the correct occurrence identity.
        self.assertEqual(self.read_results(), ['20260601T063000#0'])

    def test_in_process_dual_thread_contention(self):
        # Same guarantee with the driver's built-in barrier-synchronized
        # dual storage objects (exercises the SQLite IntegrityError path).
        now = __import__('datetime').datetime(2026, 6, 2, 6, 31)
        out = self.run_driver('dual', now)
        self.assertEqual(len(out), 1, out)
        self.assertEqual(self.read_results(), ['20260602T063000#0'])


class RestartCatchupTest(MultiprocessTestBase):
    def test_restart_within_catchup_delivers_once(self):
        import datetime
        # First process ticks before the occurrence and delivers nothing.
        self.run_driver('tick', datetime.datetime(2026, 6, 1, 6, 20))
        self.assertEqual(self.read_results(), [])

        # Simulate a consumer restart (a brand-new interpreter + new Huey
        # instance over the same DB) 10 minutes after the occurrence, within
        # the default 15-minute catch-up window.
        enqueued = self.run_driver(
            'tick', datetime.datetime(2026, 6, 1, 6, 40))
        self.assertEqual(enqueued, ['20260601T063000#0'])
        self.assertEqual(self.read_results(), ['20260601T063000#0'])

        # A third process re-checking the same/following minute must not
        # redeliver, even though its tick window still overlaps.
        again = self.run_driver(
            'tick', datetime.datetime(2026, 6, 1, 6, 41))
        self.assertEqual(again, [])
        self.assertEqual(self.read_results(), ['20260601T063000#0'])

    def test_restart_after_catchup_expiry_drops_occurrence(self):
        import datetime
        # 20 minutes late: outside the default 900s catch-up window.
        enqueued = self.run_driver(
            'tick', datetime.datetime(2026, 6, 1, 6, 50))
        self.assertEqual(enqueued, [])
        self.assertEqual(self.read_results(), [])

    def test_real_process_restart_preserves_dedup(self):
        import datetime
        # A full consumer process is started, killed (real process death, as
        # in a deploy or crash), and replaced by a fresh interpreter over the
        # same SQLite file. Claimed occurrences must survive the restart and
        # never be delivered twice.
        now = datetime.datetime(2026, 6, 1, 6, 31)
        proc1, log1 = self.spawn_serve(now)
        try:
            pids1 = self.wait_for_pids(1)
            # A separate scheduler process claims and delivers the occurrence.
            self.assertEqual(
                self.run_driver('tick', now), ['20260601T063000#0'])
        finally:
            # SIGKILL simulates a hard crash/deploy: no graceful cleanup, so
            # the next process must rely solely on persisted SQLite state.
            proc1.kill()
            proc1.wait(timeout=10)
            log1.close()

        # Restart: a brand-new consumer process over the same database.
        proc2, log2 = self.spawn_serve(now)
        try:
            pids2 = self.wait_for_pids(2)
            self.assertNotEqual(pids1[0], pids2[-1])
            # The restarted scheduler re-checking the same minute must not
            # redeliver the claimed occurrence.
            self.assertEqual(self.run_driver('tick', now), [])
        finally:
            open(self.sentinel, 'w').close()
            try:
                proc2.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc2.kill()
                proc2.wait()
            log2.close()
        # Exactly one execution across the entire restart lifecycle.
        self.assertEqual(self.read_results(), ['20260601T063000#0'])


class MidnightBoundaryTest(MultiprocessTestBase):
    def test_occurrence_on_previous_local_date(self):
        import datetime
        # A 22:30 New York task on May 31 (EDT, UTC-4) is scheduled at
        # 02:30Z on Jun 1: the UTC date has crossed midnight while the local
        # date is still May 31. A tick at 02:20Z (before 02:30Z) delivers
        # nothing; a tick at 02:31Z delivers exactly that occurrence.
        before = datetime.datetime(2026, 6, 1, 2, 20)
        at = datetime.datetime(2026, 6, 1, 2, 31)
        self.assertEqual(
            self.run_driver(
                'tick', before,
                env_extra={'HUEY_TZ_MINUTE': '30', 'HUEY_TZ_HOUR': '22'}),
            [])
        self.assertEqual(
            self.run_driver(
                'tick', at,
                env_extra={'HUEY_TZ_MINUTE': '30', 'HUEY_TZ_HOUR': '22'}),
            ['20260601T023000#0'])
        self.assertEqual(self.read_results(), ['20260601T023000#0'])

    def test_local_midnight_crossing(self):
        import datetime
        # A 00:30 NY task on Jun 2 (EDT, UTC-4) runs at 04:30Z the same UTC
        # date -- exercising a schedule whose local hour is midnight.
        now = datetime.datetime(2026, 6, 2, 4, 31)
        out = self.run_driver(
            'tick', now,
            env_extra={'HUEY_TZ_MINUTE': '30', 'HUEY_TZ_HOUR': '0'})
        self.assertEqual(out, ['20260602T043000#0'])
        self.assertEqual(self.read_results(), ['20260602T043000#0'])


class DstTransitionMultiprocessTest(MultiprocessTestBase):
    def test_spring_gap_skip_across_processes(self):
        import datetime
        # 02:30 NY does not exist on Mar 8 2026. With gap=skip, two separate
        # scheduler processes both conclude there is nothing to deliver.
        tick = datetime.datetime(2026, 3, 8, 7, 31)
        env = {'HUEY_TZ_GAP_POLICY': 'skip'}
        self.assertEqual(self.run_driver('tick', tick, env_extra=env), [])
        self.assertEqual(self.run_driver('tick', tick, env_extra=env), [])
        self.assertEqual(self.read_results(), [])

    def test_spring_gap_next_valid_single_delivery(self):
        import datetime
        # next_valid resolves the missing 02:30 to 03:30 EDT == 07:30Z.
        tick = datetime.datetime(2026, 3, 8, 7, 31)
        env = {'HUEY_TZ_GAP_POLICY': 'next_valid'}
        first = self.run_driver('tick', tick, env_extra=env)
        self.assertEqual(first, ['20260308T073000#0'])
        # A second process at the same minute must not duplicate it.
        self.assertEqual(
            self.run_driver('tick', tick, env_extra=env), [])
        self.assertEqual(self.read_results(), ['20260308T073000#0'])

    def test_fall_fold_twice_two_distinct_executions(self):
        import datetime
        # fold=twice with a 62-minute catch-up so one tick spans both halves.
        env = {'HUEY_TZ_FOLD_POLICY': 'twice', 'HUEY_TZ_CATCHUP': '3720',
               'HUEY_TZ_MINUTE': '30', 'HUEY_TZ_HOUR': '1'}
        tick = datetime.datetime(2026, 11, 1, 6, 30, 1)
        out = self.run_driver('tick', tick, env_extra=env)
        self.assertEqual(sorted(out),
                         ['20261101T053000#0', '20261101T063000#1'])
        self.assertEqual(sorted(self.read_results()),
                         ['20261101T053000#0', '20261101T063000#1'])
        # Re-ticking from a fresh process adds neither half again.
        self.assertEqual(
            self.run_driver('tick', tick, env_extra=env), [])


if __name__ == '__main__':
    unittest.main()
