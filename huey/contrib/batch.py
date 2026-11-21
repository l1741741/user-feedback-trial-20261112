"""
Business-level task batches ("task families") with per-region withdrawal and
an exactly-once join step, backed by SQLite.

A batch joins the results of several region tasks (e.g. an inventory batch
covering multiple regions). Each member settles in the join in exactly one of
three outcomes -- ``success``, ``failed`` or ``cancelled`` -- and once every
member has settled, the batch's tail step is enqueued exactly once, even when
the last members finish concurrently in different processes.

Withdrawing a region marks its current generation as cancelled. Deliveries
stamped with an older-or-equal generation -- including ones already inside
the task function -- settle as cancelled and never dispatch successors;
other regions are unaffected. Resuming the region bumps its generation, so
only subsequent deliveries run again; withdrawn generations are never
revived, and a member outcome already recorded in the join is never
rewritten.

Everything here is opt-in: tasks enqueued without the batch entry-point carry
no batch configuration and execute with Huey's original semantics.

Example::

    from huey.contrib.batch import BatchSqliteHuey

    huey = BatchSqliteHuey('inventory', filename='inventory.db')

    @huey.task()
    def count_region(region):
        return do_inventory_count(region)

    @huey.task()
    def merge_results(results):
        # results[i] is the region's value, an Error, or the CANCELLED
        # placeholder, in the order members were declared.
        ...

    batch = huey.batch('inventory-2026-11', [
        (count_region.s('north'), 'north'),
        (count_region.s('south'), 'south'),
    ], merge_results.s())

    huey.cancel_region('inventory-2026-11', 'north')   # withdraw one region.
    huey.resume_region('inventory-2026-11', 'north')   # allow new deliveries.
    huey.deliver_region('inventory-2026-11', 'north', count_region.s('north'))
    huey.batch_result('inventory-2026-11')             # per-region outcomes.
"""

from collections import namedtuple

from huey.api import Result
from huey.api import SqliteHuey
from huey.api import _signature
from huey.exceptions import ConfigurationError
from huey.exceptions import HueyException
from huey.storage import SqliteStorage
from huey.utils import BATCH_CANCELLED
from huey.utils import BATCH_FAILED
from huey.utils import BATCH_SUCCESS
from huey.utils import BatchConfig

__all__ = (
    'BatchError',
    'BatchMemberResult',
    'BatchResult',
    'BatchSqliteHuey',
    'BatchSqliteStorage',
)


class BatchError(HueyException):
    pass


# Per-region join outcome. ``outcome`` is one of 'success', 'failed' or
# 'cancelled' (None while the member is still pending); ``value`` holds the
# deserialized success value, the Error instance for failures, and None for
# cancelled or pending members.
BatchMemberResult = namedtuple('BatchMemberResult', ('outcome', 'value'))


class BatchResult(object):
    """
    Handle returned by :py:meth:`BatchSqliteHuey.batch`. ``members`` holds
    the per-member Result handles and ``callback`` the Result handle for the
    batch's tail step.
    """
    def __init__(self, huey, bid, members, callback):
        self.huey = huey
        self.bid = bid
        self.members = members
        self.callback = callback

    def get(self, *args, **kwargs):
        return self.callback.get(*args, **kwargs)
    __call__ = get


class BatchSqliteStorage(SqliteStorage):
    """
    SqliteStorage extended with the tables and atomic operations needed to
    settle batch joins. All batch state -- member outcomes, the tail claim,
    region cancellations and delivery generations -- lives in the same
    SQLite database as the queue, so it survives consumer restarts and is
    shared consistently across processes.
    """
    table_batch_member = (
        'create table if not exists batch_member ('
        'queue text not null, bid text not null, idx integer not null, '
        'region text not null, outcome text, value blob, '
        'primary key (queue, bid, idx))')
    table_batch_meta = (
        'create table if not exists batch_meta ('
        'queue text not null, bid text not null, callback blob not null, '
        'primary key (queue, bid))')
    table_batch_tail = (
        'create table if not exists batch_tail ('
        'queue text not null, bid text not null, '
        'primary key (queue, bid))')
    table_batch_cancel = (
        'create table if not exists batch_cancel ('
        'queue text not null, bid text not null, region text not null, '
        'cancel_gen integer not null, '
        'primary key (queue, bid, region))')
    ddl = SqliteStorage.ddl + [
        table_batch_member,
        table_batch_meta,
        table_batch_tail,
        table_batch_cancel]

    # -- Batch registration -------------------------------------------------

    def create_batch(self, bid, regions, callback_data):
        """
        Register a batch and its members. Raises IntegrityError when the
        batch id is already in use.
        """
        with self.db(commit=True) as curs:
            curs.execute('insert into batch_meta (queue, bid, callback) '
                         'values (?, ?, ?)',
                         (self.name, bid, self.to_blob(callback_data)))
            for idx, region in enumerate(regions):
                curs.execute('insert into batch_member '
                             '(queue, bid, idx, region) values (?, ?, ?, ?)',
                             (self.name, bid, idx, region))

    def batch_callback_data(self, bid):
        res = self.sql('select callback from batch_meta '
                       'where queue = ? and bid = ?', (self.name, bid),
                       results=True)
        if not res:
            raise BatchError('unknown batch %r' % bid)
        return res[0][0]

    def batch_exists(self, bid):
        return bool(self.sql('select 1 from batch_meta '
                             'where queue = ? and bid = ?', (self.name, bid),
                             results=True))

    def batch_member_idx(self, bid, region):
        res = self.sql('select idx from batch_member '
                       'where queue = ? and bid = ? and region = ?',
                       (self.name, bid, region), results=True)
        return res[0][0] if res else None

    def batch_member_outcomes(self, bid):
        return self.sql('select idx, region, outcome, value from batch_member '
                        'where queue = ? and bid = ? order by idx',
                        (self.name, bid), results=True)

    # -- Cancellation and generations ----------------------------------------

    def _generation_key(self, bid, region):
        return 'batch:%s:gen:%s' % (bid, region)

    def batch_generation(self, bid, region):
        return self.incr(self._generation_key(bid, region), 0)

    def cancel_batch_region(self, bid, region):
        """
        Withdraw the region at its current generation. Deliveries stamped
        with this or an older generation settle as cancelled.
        """
        generation = self.batch_generation(bid, region)
        self.sql('insert into batch_cancel (queue, bid, region, cancel_gen) '
                 'values (?, ?, ?, ?) on conflict (queue, bid, region) '
                 'do update set cancel_gen = excluded.cancel_gen',
                 (self.name, bid, region, generation), commit=True)
        return generation

    def resume_batch_region(self, bid, region):
        """
        Resume deliveries for the region. Only deliveries stamped after this
        point escape the cancellation; the cancel record itself is kept, so
        older generations remain cancelled.
        """
        return self.incr(self._generation_key(bid, region), 1)

    def batch_member_cancelled(self, bid, region, generation):
        res = self.sql('select cancel_gen from batch_cancel '
                       'where queue = ? and bid = ? and region = ?',
                       (self.name, bid, region), results=True)
        return bool(res) and generation <= res[0][0]

    # -- Join settlement ------------------------------------------------------

    def settle_batch_member(self, bid, idx, region, generation, outcome,
                            payload):
        """
        Atomically record a member outcome and, when it completes the join,
        claim the batch's tail step. Returns a 3-tuple of booleans:

        * whether this call recorded the outcome (first settle wins),
        * whether the caller claimed the tail step (exactly one caller ever
          wins, across processes),
        * whether the delivery's generation is covered by a cancellation
          (computed inside the same transaction, so a cancel that races the
          settle is still reflected in the recorded outcome).
        """
        if payload is not None:
            payload = self.to_blob(payload)
        with self.db(commit=True) as curs:
            # A withdrawal recorded at or after this delivery's generation
            # downgrades the outcome to cancelled.
            curs.execute('select cancel_gen from batch_cancel '
                         'where queue = ? and bid = ? and region = ?',
                         (self.name, bid, region))
            row = curs.fetchone()
            cancelled = row is not None and generation <= row[0]
            if cancelled:
                outcome, payload = BATCH_CANCELLED, None

            curs.execute('update batch_member set outcome = ?, value = ? '
                         'where queue = ? and bid = ? and idx = ? and '
                         'outcome is null',
                         (outcome, payload, self.name, bid, idx))
            if curs.rowcount != 1:
                return False, False, cancelled  # Already settled.

            curs.execute('select count(*), count(outcome) from batch_member '
                         'where queue = ? and bid = ?', (self.name, bid))
            total, settled = curs.fetchone()
            if total != settled:
                return True, False, cancelled  # Waiting on other members.

            try:
                curs.execute('insert into batch_tail (queue, bid) '
                             'values (?, ?)', (self.name, bid))
            except self.integrity_error:
                return True, False, cancelled  # Tail already claimed.
            return True, True, cancelled

    def claim_batch_tail(self, bid):
        try:
            with self.db(commit=True) as curs:
                curs.execute('insert into batch_tail (queue, bid) '
                             'values (?, ?)', (self.name, bid))
        except self.integrity_error:
            return False
        return True

    def clear_batch(self, bid):
        """
        Remove all join state for a batch (members, callback, tail claim,
        cancellations and delivery generations).
        """
        with self.db(commit=True) as curs:
            for table in ('batch_member', 'batch_meta', 'batch_tail',
                          'batch_cancel'):
                curs.execute('delete from %s where queue = ? and bid = ?'
                             % table, (self.name, bid))
        rows = self.sql('select key from counter where queue = ? and key '
                        'like ?', (self.name, 'batch:%s:gen:%%' % bid),
                        results=True)
        for key, in rows:
            self.delete_counter(key)

    def flush_all(self):
        super(BatchSqliteStorage, self).flush_all()
        with self.db(commit=True) as curs:
            for table in ('batch_member', 'batch_meta', 'batch_tail',
                          'batch_cancel'):
                curs.execute('delete from %s where queue = ?' % table,
                             (self.name,))


class BatchSqliteHuey(SqliteHuey):
    """
    SqliteHuey with an opt-in entry-point for business batches: region tasks
    whose results are joined into a single tail step, with per-region
    withdrawal and resumption. Tasks enqueued outside of ``batch()`` /
    ``deliver_region()`` execute with Huey's original semantics.
    """
    storage_class = BatchSqliteStorage

    def _require_batch_storage(self):
        if not isinstance(self.storage, BatchSqliteStorage):
            raise ConfigurationError(
                'batch support requires BatchSqliteStorage (or '
                'BatchSqliteHuey).')

    def _require_region(self, bid, region):
        idx = self.storage.batch_member_idx(bid, region)
        if idx is None:
            raise BatchError('unknown region %r in batch %r' % (region, bid))
        return idx

    def batch(self, bid, members, callback):
        """
        Create a batch and enqueue its members.

        :param bid: business id of the batch (unique until cleared).
        :param members: list of ``(task, region)`` tuples, or a dict mapping
            region names to tasks. Tasks are signatures, e.g. ``task.s(...)``.
        :param callback: tail step, enqueued exactly once with the list of
            member outcomes (in declaration order) once every member has
            settled. Cancelled members contribute the CANCELLED placeholder,
            failed members an Error instance.
        :return: BatchResult handle.
        """
        self._require_batch_storage()
        if isinstance(members, dict):
            members = [(task, region) for region, task in members.items()]
        members = [(task, region) for task, region in members]
        regions = [region for _, region in members]
        if len(set(regions)) != len(regions):
            raise BatchError('duplicate region in batch %r' % bid)

        callback = _signature(callback)
        try:
            self.storage.create_batch(
                bid, regions, self.serialize_task(callback))
        except self.storage.integrity_error:
            raise BatchError('batch %r already exists' % bid)

        results = []
        for idx, (task, region) in enumerate(members):
            task.batch_config = BatchConfig(
                bid, region, self.storage.batch_generation(bid, region), idx)
            results.append(self.enqueue(task))

        if not members and self.storage.claim_batch_tail(bid):
            self._enqueue_batch_callback(bid)

        callback_result = Result(self, callback) if self.results else None
        return BatchResult(self, bid, results, callback_result)

    def deliver_region(self, bid, region, task):
        """
        Deliver a region task under an existing batch. The delivery is
        stamped with the region's current generation, so it is cancelled
        while the region is withdrawn and runs normally once resumed. When
        the member's join outcome is already recorded, a delivery executes
        as an ordinary task but never rewrites the recorded outcome.
        """
        self._require_batch_storage()
        idx = self._require_region(bid, region)
        task = _signature(task)
        task.batch_config = BatchConfig(
            bid, region, self.storage.batch_generation(bid, region), idx)
        return self.enqueue(task)

    def cancel_region(self, bid, region):
        """
        Withdraw a region: queued and in-flight deliveries at the current
        generation settle as cancelled and do not dispatch successors. Other
        regions are unaffected.
        """
        self._require_batch_storage()
        self._require_region(bid, region)
        return self.storage.cancel_batch_region(bid, region)

    def resume_region(self, bid, region):
        """
        Resume a withdrawn region. Subsequent deliveries run normally;
        deliveries from withdrawn generations are never revived.
        """
        self._require_batch_storage()
        self._require_region(bid, region)
        return self.storage.resume_batch_region(bid, region)

    def batch_result(self, bid):
        """
        Return the join record for a batch as a dict of region name to
        BatchMemberResult, or None if the batch is unknown. Pending members
        have an outcome of None.
        """
        self._require_batch_storage()
        if not self.storage.batch_exists(bid):
            return None
        result = {}
        for _, region, outcome, value in \
                self.storage.batch_member_outcomes(bid):
            if outcome == BATCH_SUCCESS or outcome == BATCH_FAILED:
                value = self.serializer.deserialize(value)
            else:
                value = None
            result[region] = BatchMemberResult(outcome, value)
        return result

    def clear_batch(self, bid):
        self._require_batch_storage()
        self.storage.clear_batch(bid)
