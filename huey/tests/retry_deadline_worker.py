"""
Helper module for the cross-process retry-deadline integration test.

Run as a module with a single argument:

* ``publish`` -- enqueue one always-failing task with a retry deadline and
  print "<task_id> <publish_timestamp>" to stdout.
* ``consume`` -- run a consumer against the same SQLite database.

Configuration is passed through environment variables:

* ``HUEY_IT_DB`` -- path to the SQLite database file (required).
* ``HUEY_IT_LOG`` -- path to a file each task attempt appends a timestamp to.
* ``HUEY_IT_DEADLINE`` -- retry deadline in seconds (default 2.0).
"""
import os
import sys
import time

from huey.api import SqliteHuey

huey = SqliteHuey('retry-deadline-it', filename=os.environ['HUEY_IT_DB'])

LOG_FILE = os.environ['HUEY_IT_LOG']
DEADLINE = float(os.environ.get('HUEY_IT_DEADLINE', '2.0'))


@huey.task(retries=100000, retry_delay=0, retry_deadline=DEADLINE)
def flaky():
    # Record every attempt, then fail so the task is retried until the
    # overall retry deadline (not the retry count) stops it.
    with open(LOG_FILE, 'a') as fh:
        fh.write('%f\n' % time.time())
    time.sleep(0.05)  # Throttle the immediate-requeue loop a little.
    raise RuntimeError('always fails')


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('publish', 'consume'):
        raise SystemExit('usage: python -m %s [publish|consume]' % __name__)

    if sys.argv[1] == 'publish':
        result = flaky()
        print('%s %f' % (result.id, time.time()))
        sys.stdout.flush()
    else:
        consumer = huey.create_consumer(
            workers=1,
            periodic=False,
            initial_delay=0.02,
            max_delay=0.05,
            check_worker_health=False,
            scheduler_interval=1)
        consumer.run()


if __name__ == '__main__':
    main()
