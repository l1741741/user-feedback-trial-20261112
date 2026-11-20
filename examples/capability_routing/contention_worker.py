#!/usr/bin/env python
"""
Subprocess worker for the cross-process contention check.

Each invocation is a separate OS process running its own real Huey Consumer
against the shared SQLite database. Capability filtering happens inside
SqliteStorage's exclusive transaction, so two processes must never remove
the same row.

Usage:
    python -I contention_worker.py <db_path> <capability> [<worker_count>]
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from huey import SqliteHuey  # noqa: E402
from huey.consumer import Consumer  # noqa: E402
import tasks as report_tasks  # noqa: E402


logging.getLogger('huey').disabled = True


def main():
    filename = sys.argv[1]
    capability = sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 2

    huey = SqliteHuey('reports', filename=filename, utc=False)
    # Re-register the same task class/name the producer script enqueues.
    huey.task(requirements='pdf')(report_tasks.pdf_report)

    consumer = Consumer(
        huey,
        workers=workers,
        periodic=False,
        initial_delay=0.01,
        max_delay=0.05,
        scheduler_interval=1,
        check_worker_health=False,
        graceful_signal='TERM',
        capabilities=(capability,))
    consumer.start()

    # Exit once the queue has been drained; the parent decides the overall
    # deadline and terminates stragglers.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if huey.pending_count() == 0:
            time.sleep(0.2)
            if huey.pending_count() == 0:
                break
        time.sleep(0.05)

    consumer.stop(graceful=True)
    huey.storage.close()


if __name__ == '__main__':
    main()
