"""
Demo tasks for the error-callback trigger policy end-to-end scenario.

A file-processing pipeline with retries and error successors (alerts). The
business wants exactly one alert when a job fails for good -- not one per
attempt. State (attempt counters, events, alerts) is kept in files under
run/ so it survives consumer restarts and can be inspected by the driver.
"""
import os

from huey import SqliteHuey
from huey.exceptions import CancelExecution
from huey.exceptions import RetryTask

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(BASE_DIR, 'run')
DB_FILE = os.path.join(RUN_DIR, 'demo.db')
EVENTS_LOG = os.path.join(RUN_DIR, 'events.log')
ALERTS_LOG = os.path.join(RUN_DIR, 'alerts.log')
STATE_DIR = os.path.join(RUN_DIR, 'state')

for d in (RUN_DIR, STATE_DIR):
    if not os.path.exists(d):
        os.makedirs(d)

huey = SqliteHuey('error-policy-demo', filename=DB_FILE)


def log_event(message):
    with open(EVENTS_LOG, 'a') as fh:
        fh.write(message + '\n')


@huey.signal()
def signal_logger(signal, task, exc=None):
    extra = ' exc=%s' % type(exc).__name__ if exc is not None else ''
    log_event('signal=%-10s task=%-16s id=%s%s'
              % (signal, task.name, task.id, extra))


def attempt(key):
    """Persistent per-job attempt counter (survives consumer restarts)."""
    path = os.path.join(STATE_DIR, key)
    n = int(open(path).read()) + 1 if os.path.exists(path) else 1
    with open(path, 'w') as fh:
        fh.write(str(n))
    return n


@huey.task()
def send_alert(exc, label=None):
    """The error successor: business alert, must fire once per failed job."""
    with open(ALERTS_LOG, 'a') as fh:
        fh.write('ALERT label=%s error=%r\n' % (label, exc))
    log_event('alert-sent label=%s error=%r' % (label, exc))


@huey.task()
def notify_success(value, label=None):
    """The success successor: only runs when the job actually succeeded."""
    log_event('notify-success label=%s value=%s' % (label, value))
    return value


# 1. Fails twice with a transient error, then recovers. Final policy: no
# alert may be sent, and only the success chain runs.
@huey.task(retries=3, retry_delay=1, error_policy='final')
def flaky_import():
    if attempt('flaky') < 3:
        raise IOError('transient parse error')
    return 'imported'


# 2. Always fails. Final policy: exactly one alert, after the last attempt.
@huey.task(retries=2, retry_delay=1, error_policy='final')
def broken_convert():
    attempt('broken')
    raise ValueError('corrupt file')


# 3. Default mode (no policy, backwards compatible): the alert fires on
# every surfaced attempt.
@huey.task(retries=1, retry_delay=1)
def legacy_job():
    attempt('legacy')
    raise RuntimeError('legacy failure')


# 4. Explicit RetryTask with a delay, then success: never an error, no alert.
@huey.task(error_policy='final')
def deferred_retry():
    if attempt('deferred') < 2:
        raise RetryTask(delay=2)
    return 'deferred-ok'


# 5. CancelExecution(retry=False) despite declared retries: a final cancel,
# surfaced as an error (not a success), triggering one alert.
@huey.task(retries=3, error_policy='final')
def cancelled_job():
    attempt('cancelled')
    raise CancelExecution(retry=False)


# 6. Used for the restart scenario: fails once, waits in the SQLite schedule
# table, and succeeds after the consumer is restarted.
@huey.task(retries=2, retry_delay=4, error_policy='final')
def restart_job():
    if attempt('restart') < 2:
        raise IOError('fails before restart')
    return 'restart-ok'
