"""Shared task definitions for the shard-migration demo.

Two order groups ("A" and "B") are published into the SAME source SQLite
database. Group A is being moved to a second shard; group B must keep being
consumed from the source throughout.

The business identity of every message is the (order_id, group) pair carried
in task kwargs -- the migrator's selector reads it straight from the decoded
``huey.registry.Message`` without importing the task class.
"""
import datetime


# A module-level execution log. Both demo consumers append here so the runner
# can assert FIFO delivery and which shard handled each order.
EXECUTION_LOG = []


def process_order(order_id, group, note=''):
    """Pretend to process one order; record where/when it ran."""
    EXECUTION_LOG.append({
        'order_id': order_id,
        'group': group,
        'note': note,
        'ran_at': datetime.datetime.utcnow().isoformat(),
    })
    return 'order-%s-group-%s-ok' % (order_id, group)


def invoice_order(order_id, group, amount=0):
    EXECUTION_LOG.append({
        'order_id': order_id,
        'group': group,
        'note': 'invoice %.2f' % amount,
        'ran_at': datetime.datetime.utcnow().isoformat(),
    })
    return 'invoiced-%s' % order_id
