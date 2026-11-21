"""
Worker entry-point run as a subprocess by the multi-process tests in
``huey/tests/test_batch.py``. It is executed with::

    python -m huey.tests.mp_batch_worker

and configured through the environment variables documented in
``huey/tests/mp_batch_tasks.py``. The worker dequeues and executes tasks
until a ``stop`` marker file appears in the sync directory.
"""
import os
import sys
import time

from huey.tests.mp_batch_tasks import _marker
from huey.tests.mp_batch_tasks import huey


def main():
    stop = _marker('stop')
    while not os.path.exists(stop):
        task = huey.dequeue()
        if task is None:
            time.sleep(0.05)
        else:
            huey.execute(task)
    return 0


if __name__ == '__main__':
    sys.exit(main())
