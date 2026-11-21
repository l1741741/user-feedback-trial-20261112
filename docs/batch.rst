.. _batch:

Business batches with cancellation
==================================

.. module:: huey.contrib.batch

The :py:class:`BatchSqliteHuey` entry-point groups region tasks into a
business batch (for example, an inventory batch covering several regions)
and joins their outcomes into a single tail step. All join state lives in
the same SQLite database as the queue, so it is shared consistently across
processes and survives consumer restarts.

.. code-block:: python

    from huey.contrib.batch import BatchSqliteHuey

    huey = BatchSqliteHuey('inventory', filename='inventory.db')

    @huey.task()
    def count_region(region):
        return do_inventory_count(region)

    @huey.task()
    def merge_results(results):
        # One entry per member, in declaration order: the region's value,
        # an Error instance for failed members, or the CANCELLED
        # placeholder for withdrawn members.
        ...

    batch = huey.batch('inventory-2026-11', [
        (count_region.s('north'), 'north'),
        (count_region.s('south'), 'south'),
    ], merge_results.s())

Each member settles in the join in exactly one of three outcomes --
``success``, ``failed`` or ``cancelled`` -- and once every member has
settled, the tail step is enqueued **exactly once**, even when the last
members finish concurrently in different processes. The durable join record
is available through :py:meth:`BatchSqliteHuey.batch_result`, which maps
each region to a ``BatchMemberResult(outcome, value)`` and distinguishes a
withdrawn member from one that failed during execution.

Withdrawing and resuming regions
--------------------------------

.. code-block:: python

    huey.cancel_region('inventory-2026-11', 'north')
    huey.resume_region('inventory-2026-11', 'north')
    huey.deliver_region('inventory-2026-11', 'north', count_region.s('north'))

* Cancelling a region only affects that region; the others proceed as
  usual. A delivery that has not started yet is skipped, and a delivery
  that is already inside the task function runs to completion but does not
  dispatch successors -- in both cases the member settles as ``cancelled``.
* Cancellation is generation-based: resuming a region only re-enables
  subsequent deliveries. Deliveries from the withdrawn generation are never
  revived, and an outcome already recorded in the join is never rewritten.
* :py:meth:`BatchSqliteHuey.deliver_region` delivers a region task under an
  existing batch, e.g. to re-deliver after a worker crash. While the region
  is withdrawn such a delivery settles as cancelled; after resuming it runs
  normally.

The batch entry-point is strictly opt-in: tasks enqueued without it carry
no batch configuration and execute with Huey's original semantics.
