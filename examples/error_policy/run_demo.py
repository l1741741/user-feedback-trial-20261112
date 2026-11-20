"""
End-to-end demo: error-callback trigger policy on a real queue.

Runs a real Huey consumer (subprocess) against a SQLite-backed queue with a
mix of failing / recovering / cancelled / deferred tasks, then kills and
restarts the consumer to verify the scheduled retry survives in SQLite.

Usage:  python run_demo.py
"""
import os
import shutil
import signal
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, BASE_DIR)

# Clean the run directory *before* importing tasks, since importing tasks
# opens the SQLite database inside it.
RUN_DIR = os.path.join(BASE_DIR, 'run')
if os.path.exists(RUN_DIR):
    shutil.rmtree(RUN_DIR)

import tasks
from huey.exceptions import TaskException
from huey.utils import Error

huey = tasks.huey
CHECKS = []


def check(label, ok, detail=''):
    CHECKS.append((label, ok, detail))
    print('  [%s] %s%s' % ('PASS' if ok else 'FAIL', label,
                           (' -- ' + detail) if detail else ''))


def wait_for(predicate, timeout=30, interval=0.1, desc='condition'):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    print('  ... timed out waiting for %s' % desc)
    return False


def read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [line.strip() for line in fh if line.strip()]


def alert_labels():
    return [line.split(' label=')[1].split(' ')[0]
            for line in read_lines(tasks.ALERTS_LOG)]


def signals_for(task_id):
    return [line.split(' ')[0].split('=')[1]
            for line in read_lines(tasks.EVENTS_LOG)
            if ' id=%s' % task_id in line]


class Consumer(object):
    def __init__(self):
        self.proc = None

    def start(self):
        env = dict(os.environ)
        env['PYTHONPATH'] = REPO_ROOT + os.pathsep + BASE_DIR
        self.proc = subprocess.Popen(
            [sys.executable, '-m', 'huey.bin.huey_consumer', 'tasks.huey',
             '--workers', '1'],
            cwd=BASE_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        # Give the consumer a moment to initialize.
        time.sleep(1.0)
        return self.proc.poll() is None

    def stop(self):
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def phase_a():
    print('Phase A: mixed failures, recovery and default-mode compat')
    print('  (enqueueing 5 jobs, then starting the consumer)')

    # 1. Fails twice, recovers on the 3rd attempt (final policy).
    t = tasks.flaky_import.s().then(tasks.notify_success, label='flaky')
    t.error(tasks.send_alert, label='flaky')
    res_flaky = huey.enqueue(t)

    # 2. Always fails (final policy): one alert at the very end.
    t = tasks.broken_convert.s()
    t.error(tasks.send_alert, label='broken')
    res_broken = huey.enqueue(t)

    # 3. Default mode (no policy): backwards-compatible alert per attempt.
    t = tasks.legacy_job.s()
    t.error(tasks.send_alert, label='legacy')
    res_legacy = huey.enqueue(t)

    # 4. RetryTask with explicit delay, then succeeds: never an error.
    t = tasks.deferred_retry.s()
    t.error(tasks.send_alert, label='deferred')
    res_deferred = huey.enqueue(t)

    # 5. CancelExecution(retry=False) with declared retries: final cancel.
    t = tasks.cancelled_job.s()
    t.error(tasks.send_alert, label='cancelled')
    res_cancelled = huey.enqueue(t)

    consumer = Consumer()
    assert consumer.start(), 'consumer failed to start'
    try:
        ok = wait_for(lambda: len(alert_labels()) >= 4, timeout=40,
                      desc='4 alerts (broken x1, legacy x2, cancelled x1)')
        labels = sorted(alert_labels())
        check('alerts fired exactly as policy dictates',
              ok and labels == ['broken', 'cancelled', 'legacy', 'legacy'],
              'alerts=%s' % labels)

        # Recovery: success value and success chain, no alert.
        check('flaky job recovered with success value',
              res_flaky[0] == 'imported')
        check('success chain ran only after recovery',
              any('notify-success label=flaky value=imported' in l
                  for l in read_lines(tasks.EVENTS_LOG)))

        # Final failure: error state tied to the original task ID.
        try:
            res_broken.get(blocking=True, timeout=10)
            check('broken job result raises TaskException', False)
        except TaskException as exc:
            md = exc.metadata
            check('broken job error state is final and linked to task id',
                  md['task_id'] == res_broken.id and md['retries'] == 0 and
                  md['error'] == "ValueError('corrupt file')",
                  'metadata=%r' % (md,))

        # Event order for the broken job: error+retrying until final error.
        # (Retries with a retry_delay pass through the schedule, hence the
        # scheduled/enqueued pairs.)
        seq = signals_for(res_broken.id)
        check('consumer signal order matches retry lifecycle',
              seq == ['enqueued',
                      'executing', 'error', 'retrying', 'scheduled',
                      'enqueued', 'executing', 'error', 'retrying',
                      'scheduled',
                      'enqueued', 'executing', 'error'],
              'signals=%s' % seq)

        # RetryTask(delay=...) then success: no error ever surfaced.
        check('deferred RetryTask job succeeded without alert',
              res_deferred.get(blocking=True, timeout=15) == 'deferred-ok')

        # CancelExecution(retry=False): stored as error, not success.
        try:
            res_cancelled.get(blocking=True, timeout=10)
            check('cancelled job not disguised as success', False)
        except TaskException as exc:
            check('cancelled job stored as error result',
                  exc.metadata['error'] == 'CancelExecution()')

        # Default mode compatibility: legacy job alerted on every attempt.
        check('default mode alerts on every surfaced attempt (compat)',
              alert_labels().count('legacy') == 2)
    finally:
        consumer.stop()


def phase_b():
    print('Phase B: scheduled retry survives a SQLite/consumer restart')
    t = tasks.restart_job.s()
    t.error(tasks.send_alert, label='restart')
    res_restart = huey.enqueue(t)

    consumer = Consumer()
    assert consumer.start(), 'consumer failed to start'
    # Wait for the first attempt to fail and the retry to be scheduled.
    wait_for(lambda: 'retrying' in signals_for(res_restart.id), timeout=20,
             desc='first attempt of restart_job to fail')
    consumer.stop()
    check('consumer stopped after first failed attempt', True)

    # Inspect the SQLite database from a fresh connection: the retry is
    # still scheduled, nothing was lost or marked successful.
    check('scheduled retry persisted in SQLite after shutdown',
          huey.scheduled_count() == 1,
          'scheduled_count=%d' % huey.scheduled_count())
    raw = res_restart.get_raw_result()
    check('only the intermediate error is stored so far (default mode)',
          isinstance(raw, Error) and raw.metadata['retries'] == 2,
          'raw=%r' % (raw,))

    consumer = Consumer()
    assert consumer.start(), 'consumer failed to restart'
    try:
        wait_for(lambda: 'complete' in signals_for(res_restart.id),
                 timeout=30, desc='complete signal after restart')
        # Default mode stores intermediate errors, so the handle must be
        # reset to see the success value written by the retry.
        res_restart.reset()
        value = res_restart.get(blocking=True, timeout=10)
        check('job completed after consumer restart', value == 'restart-ok',
              'value=%r' % value)
        seq = signals_for(res_restart.id)
        check('event order across restart is consistent',
              seq == ['enqueued', 'executing', 'error', 'retrying',
                      'scheduled', 'enqueued', 'executing', 'complete'],
              'signals=%s' % seq)
        check('no alert for a job that eventually succeeded',
              'restart' not in alert_labels())
    finally:
        consumer.stop()


def main():
    phase_a()
    print()
    phase_b()

    print()
    failed = [c for c in CHECKS if not c[1]]
    print('=' * 60)
    print('Demo finished: %d checks, %d passed, %d failed'
          % (len(CHECKS), len(CHECKS) - len(failed), len(failed)))
    if failed:
        sys.exit(1)
    print('All checks passed.')


if __name__ == '__main__':
    main()
