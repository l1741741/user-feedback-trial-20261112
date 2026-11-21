"""Consumer runner used by the migration demo.

Runs a real consumer process against the new task definitions (with the
old-name alias registered) until the queue and schedule are empty, or until
the timeout elapses.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tasks_new


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=float, default=30.0)
    parser.add_argument('--workers', type=int, default=1)
    args = parser.parse_args()

    consumer = tasks_new.huey.create_consumer(
        workers=args.workers,
        periodic=False,
        initial_delay=0.05,
        max_delay=0.1,
        scheduler_interval=1,
        check_worker_health=False)
    consumer.start()
    deadline = time.monotonic() + args.timeout
    idle_since = None
    try:
        while time.monotonic() < deadline:
            pending = tasks_new.huey.pending_count()
            scheduled = tasks_new.huey.scheduled_count()
            if pending or scheduled:
                idle_since = None
            elif idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= 0.5:
                break  # Queue and schedule empty and no task claimed.
            time.sleep(0.05)
    finally:
        consumer.stop(graceful=True)
        tasks_new.huey.storage.close()


if __name__ == '__main__':
    main()
