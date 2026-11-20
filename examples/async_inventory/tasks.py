"""
Inventory-sync tasks, executed natively as coroutines by the consumer.

The consumer is started with ``enable_async=True`` (see consumer.py), so
each worker thread runs its own event loop. The ``connect_client`` startup
hook runs once per worker and stores the client in thread-local state, so
consecutive tasks on the same worker reuse that worker's connection while
the two workers never share one. On shutdown the ``close_client`` hook
closes the client before the worker's event loop is torn down.

Configuration comes from the environment (with defaults for a manual
demo run):

    ASYNC_INV_DB           SQLite file used by SqliteHuey
    ASYNC_INV_HOST/PORT    address of the inventory service
    ASYNC_INV_CLEANUP_LOG  file the shutdown hook appends "closed" lines to
    ASYNC_INV_ATTEMPTS_LOG file the flaky/failing tasks record attempts in
"""
import asyncio
import os
import threading

from huey import SqliteHuey

from examples.async_inventory.client import InventoryClient


DB_PATH = os.environ.get('ASYNC_INV_DB', 'async-inventory.db')
HOST = os.environ.get('ASYNC_INV_HOST', '127.0.0.1')
PORT = int(os.environ.get('ASYNC_INV_PORT', '13771'))
CLEANUP_LOG = os.environ.get('ASYNC_INV_CLEANUP_LOG',
                             'async-inventory-cleanup.log')
ATTEMPTS_LOG = os.environ.get('ASYNC_INV_ATTEMPTS_LOG',
                              'async-inventory-attempts.log')

huey = SqliteHuey('async-inventory', filename=DB_PATH)

# Per-worker state. Each worker thread runs its own startup hooks, so each
# worker gets its own client bound to its own event loop.
_state = threading.local()


@huey.on_startup()
async def connect_client():
    client = InventoryClient(HOST, PORT)
    await client.connect()
    _state.client = client


@huey.on_shutdown()
async def close_client():
    client = getattr(_state, 'client', None)
    if client is not None:
        await client.close()
        with open(CLEANUP_LOG, 'a') as fh:
            fh.write('%s closed conn=%s\n' % (
                threading.current_thread().name, client.last_conn_id))


@huey.task()
async def fetch_stock(sku):
    # Keep the worker busy briefly so that, with multiple workers, tasks
    # are distributed across them.
    await asyncio.sleep(0.2)
    info = await _state.client.fetch(sku)
    info['worker'] = threading.current_thread().name
    return info


def _record_attempt(key):
    with open(ATTEMPTS_LOG, 'a') as fh:
        fh.write('%s\n' % key)
    with open(ATTEMPTS_LOG) as fh:
        return sum(1 for line in fh if line.strip() == key)


@huey.task(retries=2)
async def flaky_fetch(sku):
    # Fails on the first attempt, succeeds on retry.
    attempts = _record_attempt('flaky:%s' % sku)
    info = await _state.client.fetch(sku)
    if attempts < 2:
        raise ValueError('temporary inventory error')
    info['attempts'] = attempts
    return info


@huey.task(retries=1)
async def failing_fetch(sku):
    # Always fails; used to verify retry-then-error behavior.
    _record_attempt('failing:%s' % sku)
    await _state.client.fetch(sku)
    raise ValueError('permanent inventory error')


@huey.task()
def record_stock(label, conn_id=None, sku=None, stock=None, worker=None):
    # Synchronous successor in a pipeline: the async parent task's return
    # value (a dict) is merged into this task's keyword arguments.
    return {
        'label': label,
        'worker': threading.current_thread().name,
        'parent': {
            'conn_id': conn_id,
            'sku': sku,
            'stock': stock,
            'worker': worker,
        },
    }
