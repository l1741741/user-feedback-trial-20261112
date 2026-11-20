"""
Consumer entry-point for the async inventory demo.

Run from the repository root:

    python -m examples.async_inventory.consumer [workers]

The consumer is started with ``enable_async=True``, which requires the
default "thread" worker-type. Each worker gets its own event loop and its
own inventory-service connection (see tasks.py).
"""
import logging
import sys

from examples.async_inventory.tasks import huey


def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s:%(name)s:%(threadName)s:%(message)s')
    consumer = huey.create_consumer(
        workers=workers,
        enable_async=True,
        periodic=False,
        initial_delay=0.05,
        max_delay=0.2,
        check_worker_health=False)
    consumer.run()


if __name__ == '__main__':
    main()
