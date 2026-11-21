"""
Task definitions shared by the multi-process batch tests.

The test process (``huey/tests/test_batch.py``) imports this module to build
task signatures, while worker subprocesses get the same task classes by
importing it through ``huey/tests/mp_batch_worker.py`` (run with
``python -m huey.tests.mp_batch_worker``). Keeping the tasks in their own
module ensures the registry names match in both processes.

Configuration comes from environment variables:

* ``BATCH_TEST_DB`` -- SQLite database file shared with the test process.
* ``BATCH_TEST_SYNC`` -- directory used for marker files that synchronize
  the worker with the test process.
* ``BATCH_TEST_NAME`` -- queue name shared with the test process.
"""
import os
import tempfile
import time

from huey.contrib.batch import BatchSqliteHuey

SYNC_DIR = os.environ.get('BATCH_TEST_SYNC') or tempfile.gettempdir()
DB_FILE = os.environ.get('BATCH_TEST_DB') or os.path.join(
    tempfile.gettempdir(), 'huey-mp-batch-default.db')
QUEUE_NAME = os.environ.get('BATCH_TEST_NAME', 'huey-mp-batch')

huey = BatchSqliteHuey(QUEUE_NAME, filename=DB_FILE, timeout=30, utc=False)


def _marker(name):
    return os.path.join(SYNC_DIR, name)


def touch(name):
    with open(_marker(name), 'w') as fh:
        fh.write('1')


def wait_for(name, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(_marker(name)):
            return True
        time.sleep(0.05)
    return False


@huey.task()
def mp_region(region):
    touch('started-%s' % region)
    wait_for('release-%s' % region)
    return 'val-%s' % region


@huey.task()
def mp_region_shared(region):
    touch('started-%s' % region)
    wait_for('release-all')
    return 'val-%s' % region


@huey.task()
def mp_successor(tag, prev=None):
    touch('successor-%s' % tag)
    return tag


@huey.task()
def mp_merge(results):
    # Append one line per invocation; the tests assert the tail step ran
    # exactly once.
    with open(_marker('merge-log'), 'a') as fh:
        fh.write(repr(list(results)) + '\n')
    return list(results)


@huey.task()
def mp_plain(n):
    touch('plain-%s' % n)
    return n
