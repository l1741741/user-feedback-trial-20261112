"""Two real Huey instances backed by two real SQLite files.

Source shard ........ sqlite:///$DEMO_DIR/source.db  (queue name "orders")
Destination shard ... sqlite:///$DEMO_DIR/target.db  (queue name "orders")

Both instances register the same task callables under the same names, so any
message decoded on either side can be executed by either consumer.
"""
import os

from huey.contrib.sql_huey import SqlHuey
from huey.registry import Message

from examples.shard_migration import tasks


DEMO_DIR = os.environ.get(
    'SHARD_DEMO_DIR',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runtime'))
os.makedirs(DEMO_DIR, exist_ok=True)
SOURCE_DB = os.path.join(DEMO_DIR, 'source.db')
TARGET_DB = os.path.join(DEMO_DIR, 'target.db')

# Serializer settings MUST be identical on both sides (and on the producers).
source_huey = SqlHuey('orders', database='sqlite:///%s' % SOURCE_DB,
                      utc=False, results=True)
target_huey = SqlHuey('orders', database='sqlite:///%s' % TARGET_DB,
                      utc=False, results=True)

# Register the same tasks on both registries.
source_process_order = source_huey.task(name='process_order')(
    tasks.process_order)
target_process_order = target_huey.task(name='process_order')(
    tasks.process_order)
source_invoice_order = source_huey.task(name='invoice_order')(
    tasks.invoice_order)
target_invoice_order = target_huey.task(name='invoice_order')(
    tasks.invoice_order)


def order_group_selector(message):
    """Migrate exactly the messages whose business group is "A"."""
    if not isinstance(message, Message):
        return False
    kwargs = message.kwargs or {}
    return kwargs.get('group') == 'A'


def revoke_key_selector(kind, queue, key):
    # Only carry over revoke markers (r:<task-id>) for the migrating group;
    # result blobs belong to already-finished work and stay behind.
    return kind == 'kv' and key.startswith('r:')
