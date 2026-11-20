"""A minimal app module used by the durable-completion process tests.

The tests import this module in child processes (consumer and receiver) so the
task registry matches between processes.  It deliberately depends only on the
standard library and huey.
"""
import os

from huey import SqliteHuey


DB_FILE = os.environ['HUEY_COMPLETION_TEST_DB']
QUEUE = os.environ.get('HUEY_COMPLETION_TEST_QUEUE', 'completion-test')


def make_huey(name=None, **kwargs):
    kwargs.setdefault('utc', False)
    kwargs.setdefault('completion_events', True)
    return SqliteHuey(name or QUEUE, filename=DB_FILE, **kwargs)


huey = make_huey()


@huey.task()
def add(a, b):
    return a + b


@huey.task()
def fail(message):
    raise ValueError(message)


@huey.task(context=True, retries=1, retry_delay=0)
def flaky(failures, task=None):
    """Fails ``failures`` times then succeeds.

    Retries share the task id, so a file keyed by the id tracks attempts
    across re-delivery without another database table.
    """
    path = os.path.join(os.path.dirname(DB_FILE), 'flaky-%s' % task.id)
    count = 0
    if os.path.exists(path):
        with open(path) as fh:
            count = int(fh.read() or 0)
    if count < failures:
        with open(path, 'w') as fh:
            fh.write(str(count + 1))
        raise RuntimeError('flaky failure %s' % (count + 1))
    os.unlink(path)
    return 'recovered'


@huey.task()
def slow(seconds):
    import time
    time.sleep(seconds)
    return seconds
