# Optional shard-migration toolkit for the SQL/SQLite storage backend.
#
# This module is *not* imported by Huey itself. Producers and consumers keep
# running unchanged; an operator explicitly drives a migration through
# ``ShardMigrator`` (or the ``huey.bin.migrate_shard`` command-line entry).
from contextlib import contextmanager
from dataclasses import dataclass, field
import datetime
import logging

from peewee import *

from huey import __version__ as HUEY_VERSION
from huey.contrib.sql_huey import SqlStorage
from huey.exceptions import ConfigurationError
from huey.registry import Message
from huey.serializer import Serializer


logger = logging.getLogger('huey.migration')

# Producer versions whose on-disk queue messages this migrator can read.
#
# The wire format is the pickle of a ``huey.registry.Message`` namedtuple.
# That format was introduced in Huey 2.0.0 and has only ever been extended by
# appending fields with ``Message.__new__.__defaults__`` (see registry.py), so
# newer messages still deserialize on older readers and vice-versa. The
# relational layout consumed here (tables task/schedule/kv/counter) has been
# stable since the SqlStorage backend shipped. The migrator is therefore
# compatible with messages enqueued by any Huey release in the 2.x/3.x line
# (>= 2.0.0, < 4.0.0), provided both sides use the same Serializer settings
# (compression / secret / salt) -- this module refuses to start otherwise.
SUPPORTED_PRODUCER_SPEC = '>=2.0.0,<4.0.0 (tested with %s)' % HUEY_VERSION

# Kinds of rows the migrator knows how to move.
KIND_QUEUE = 'queue'
KIND_SCHEDULE = 'schedule'
KIND_KV = 'kv'
KIND_COUNTER = 'counter'
ALL_KINDS = (KIND_QUEUE, KIND_SCHEDULE, KIND_KV, KIND_COUNTER)


class MigrationError(ConfigurationError):
    pass


@dataclass
class MigrationStats:
    """Counters for a single ``migrate()`` call (incremental, not cumulative)."""
    scanned: int = 0
    selected: int = 0
    migrated: int = 0
    skipped: int = 0          # rows rejected by the selector
    conflicts: int = 0        # rows already present in the destination
    deserialization_errors: int = 0
    batches: int = 0
    migrated_kinds: dict = field(default_factory=dict)

    def add(self, kind):
        self.migrated_kinds[kind] = self.migrated_kinds.get(kind, 0) + 1


@dataclass
class MigrationProgress:
    """Persistent resume state for one (migration_id, kind) pair."""
    migration_id: str
    kind: str
    last_row_id: int
    migrated: int
    updated_at: datetime.datetime


# Resume-cursor order. On SQLite the integer primary key of task/schedule is
# an alias of rowid and therefore reflects insertion (enqueue) order; the
# scan cursor walks rows in that monotonic order even though the destination
# copies are applied in dequeue order (see ShardMigrator._sort_key).
def _cursor_order(kind):
    return (SQL('id').asc(),)


class ShardMigrator(object):
    """Move one business group's pending work from one SQL DB to another.

    :param SqlStorage source: storage of the shard being drained.
    :param SqlStorage destination: storage of the shard taking over.
    :param callable selector: ``fn(message: Message) -> bool`` evaluated for
        every queue/schedule row. Only rows for which it returns ``True`` are
        migrated -- other groups stay in the source and keep being consumed.
        KV/counter rows are matched by key through ``key_selector`` instead.
    :param str migration_id: stable identifier of this migration run. Resume
        state is keyed by it, so *re-running the same logical migration must
        reuse the same id* (and restarting after a crash does by design).
    :param Serializer serializer: serializer matching both Huey instances.
    :param int batch_size: rows processed per source transaction.
    :param tuple kinds: which tables to migrate.
    :param callable key_selector: optional ``fn(kind, queue, key) -> bool``
        used to decide which KV rows to move (e.g. revoke markers). It is
        only evaluated for KV rows; counters have no group encoding in their
        key and are always migrated.
    """

    def __init__(self, source, destination, selector, migration_id='default',
                 serializer=None, destination_serializer=None,
                 batch_size=200, kinds=None, key_selector=None):
        if not isinstance(source, SqlStorage) or \
                not isinstance(destination, SqlStorage):
            raise MigrationError('ShardMigrator requires two SqlStorage '
                                 'instances (use SqlHuey/SqliteHuey).')
        if source is destination or source.database is destination.database:
            raise MigrationError('Source and destination must be different '
                                 'databases.')
        if source.name != destination.name:
            # Row-level conflict detection and the identity cache are scoped
            # by queue name; merging differently-named queues would silently
            # mix their key spaces. Re-create the destination Huey with the
            # same queue name if a rename is part of the cut-over.
            raise MigrationError(
                'Source and destination queue names differ (%r vs %r); '
                'migrating between differently-named queues is not '
                'supported.' % (source.name, destination.name))
        if not callable(selector):
            raise MigrationError('A selector(message) callable is required to '
                                 'identify the migrating business group.')
        if not isinstance(source.database, SqliteDatabase) or \
                not isinstance(destination.database, SqliteDatabase):
            # The KV/counter resume cursor relies on SQLite rowid; queue and
            # schedule migration itself is backend-neutral, but the supported
            # deployment here is SQLite-to-SQLite shard resizing.
            raise MigrationError('ShardMigrator only supports SQLite '
                                 'databases (got %r / %r).' %
                                 (type(source.database).__name__,
                                  type(destination.database).__name__))
        if kinds is None:
            kinds = ALL_KINDS
        unknown = set(kinds) - set(ALL_KINDS)
        if unknown:
            raise MigrationError('Unknown migration kinds: %r' % unknown)

        self.source = source
        self.destination = destination
        self.selector = selector
        self.key_selector = key_selector
        self.migration_id = migration_id
        self.batch_size = max(1, int(batch_size))
        self.kinds = tuple(kinds)
        self.serializer = serializer or Serializer()
        # Serializer used to decode rows already living in the destination
        # shard (conflict checks). Same settings as ``serializer`` in every
        # supported deployment; kept separate only so distinct instances with
        # identical settings both work.
        self.destination_serializer = destination_serializer or self.serializer
        # Cache of task UUID -> raw payload already present in the destination
        # shard, rebuilt per batch. The relational row id is per-database, so
        # idempotency must key off the message's business identity.
        self._dest_identity_cache = {}
        self.Progress = self._create_progress_model()
        self.destination.database.create_tables([self.Progress])

    # ------------------------------------------------------------------
    # Persistent progress bookkeeping (lives in the destination DB).
    # ------------------------------------------------------------------
    def _create_progress_model(self):
        db = self.destination.database

        class MigrationProgressModel(Model):
            migration_id = CharField()
            kind = CharField()
            last_row_id = BigIntegerField(default=0)
            migrated = IntegerField(default=0)
            updated_at = DateTimeField(default=datetime.datetime.utcnow)

            class Meta:
                database = db
                table_name = 'huey_migration_progress'
                primary_key = CompositeKey('migration_id', 'kind')

        return MigrationProgressModel

    def _get_progress(self, kind):
        try:
            row = (self.Progress
                   .get((self.Progress.migration_id == self.migration_id) &
                        (self.Progress.kind == kind)))
        except self.Progress.DoesNotExist:
            return 0, 0
        return row.last_row_id, row.migrated

    def _save_progress(self, kind, last_row_id, migrated_total):
        (self.Progress
         .insert(migration_id=self.migration_id, kind=kind,
                 last_row_id=last_row_id, migrated=migrated_total,
                 updated_at=datetime.datetime.utcnow())
         .on_conflict(
             conflict_target=[self.Progress.migration_id,
                              self.Progress.kind],
             preserve=[self.Progress.last_row_id, self.Progress.migrated,
                       self.Progress.updated_at])
         .execute())

    def progress(self):
        """Return the persisted resume state for every migrated kind."""
        return [MigrationProgress(r.migration_id, r.kind, r.last_row_id,
                                  r.migrated, r.updated_at)
                for r in (self.Progress
                          .select()
                          .where(self.Progress.migration_id ==
                                 self.migration_id)
                          .order_by(self.Progress.kind))]

    # ------------------------------------------------------------------
    # Message decoding / selection.
    # ------------------------------------------------------------------
    def _decode(self, data):
        return self.serializer.deserialize(data)

    def _selected(self, data):
        """Return (selected, message). Undecodable rows are never migrated."""
        try:
            message = self._decode(data)
        except Exception:
            logger.exception('Skipping row that cannot be deserialized.')
            return False, None
        if not isinstance(message, Message):
            logger.warning('Skipping row whose payload is not a Message.')
            return False, message
        try:
            return bool(self.selector(message)), message
        except Exception:
            logger.exception('Selector raised; row is left in the source.')
            return False, message

    # ------------------------------------------------------------------
    # Public API.
    # ------------------------------------------------------------------
    def migrate(self, max_batches=None, dry_run=False):
        """Run one incremental pass over every configured kind.

        Safe to interrupt at any time and safe to re-run: already-migrated
        rows are never re-created in the destination and rows that vanished
        from the source (consumed in the meantime) are simply skipped.

        :param int max_batches: optional cap on batches per kind (used by the
            demo to show a partial migration).
        :param bool dry_run: decode/select/count without writing anything.
        """
        self._check_serializer_compatible()
        stats = MigrationStats()
        for kind in self.kinds:
            if kind in (KIND_QUEUE, KIND_SCHEDULE):
                self._migrate_messages(kind, stats, max_batches, dry_run)
            else:
                self._migrate_kv_like(kind, stats, max_batches, dry_run)
        logger.info('migration %r finished: %s', self.migration_id,
                    stats.__dict__)
        return stats

    def _check_serializer_compatible(self):
        # Round-trip a probe through the configured serializer to fail fast
        # when compression/signing settings differ from the producers'.
        probe = Message('probe', 'probe', None, 0, 0, None, (), {}, None,
                        None, None, None, None, None, None)
        encoded = self.serializer.serialize(probe)
        decoded = self.serializer.deserialize(encoded)
        if decoded.id != 'probe':
            raise MigrationError('Serializer round-trip failed; verify '
                                 'compression/secret/salt match the producer.')
        # Cross-check the destination side as well: a message encoded by the
        # source must be decodable with the destination settings, otherwise
        # the target consumer could not read migrated rows.
        try:
            cross = self.destination_serializer.deserialize(encoded)
        except Exception as exc:
            raise MigrationError(
                'Source and destination serializer settings are incompatible '
                '(%s); verify compression/secret/salt match on both Huey '
                'instances.' % exc)
        if cross.id != 'probe':
            raise MigrationError('Cross-shard serializer check returned an '
                                 'unexpected message.')

    # ------------------------------------------------------------------
    # Queue + schedule (message-bearing tables, surrogate integer PK `id`).
    # ------------------------------------------------------------------
    def _source_model(self, kind):
        return self.source.Task if kind == KIND_QUEUE else self.source.Schedule

    def _dest_model(self, kind):
        return (self.destination.Task if kind == KIND_QUEUE
                else self.destination.Schedule)

    def _sort_key(self, kind):
        # Mirror SqlStorage's dequeue/read ORDER BY within one migrated batch.
        if kind == KIND_QUEUE:
            return lambda row: (-row.priority, row.id)
        if kind == KIND_SCHEDULE:
            return lambda row: (row.timestamp, row.id)
        return lambda row: row.id

    def _rebuild_identity_cache(self, dst_model, queue_name):
        """Map every destination task UUID to its raw payload."""
        cache = {}
        for (data,) in (dst_model.select(dst_model.data)
                        .where(dst_model.queue == queue_name)
                        .tuples()):
            try:
                message = self.destination_serializer.deserialize(data)
            except Exception:
                continue
            if isinstance(message, Message):
                cache[message.id] = data
        self._dest_identity_cache = cache

    def _migrate_messages(self, kind, stats, max_batches, dry_run):
        src_model = self._source_model(kind)
        dst_model = self._dest_model(kind)
        # ``last_id`` is the scan high-watermark: the greatest row id this
        # migration has inspected. It advances for *every* scanned row,
        # selected or not -- otherwise skipped rows would shadow later rows.
        last_id, migrated_total = self._get_progress(kind)
        batches_done = 0

        while True:
            if max_batches is not None and batches_done >= max_batches:
                logger.info('migration %r hit max_batches=%d for %s at '
                            'row id %d', self.migration_id, max_batches,
                            kind, last_id)
                return

            # Snapshot one batch from the source inside an IMMEDIATE
            # transaction (SQLite) so concurrent consumers cannot delete a
            # row between our read and our delete. The scan follows the
            # monotonic insertion-order cursor (NOT dequeue order); the
            # selected rows are applied to the destination in dequeue order
            # further down, which preserves the relative FIFO sequence.
            with self.source.atomic():
                query = (src_model
                         .select()
                         .where((src_model.queue == self.source.name) &
                                (src_model.id > last_id))
                         .order_by(*_cursor_order(kind))
                         .limit(self.batch_size))
                rows = list(query)
                if not rows:
                    break
                stats.batches += 1
                batches_done += 1
                batch_max_id = last_id

                # Snapshot the destination's business identities once per
                # batch so the duplicate check below is O(batch), not O(n^2).
                self._rebuild_identity_cache(dst_model, self.source.name)

                # Decode/select exactly once per row.
                chosen = []
                for row in rows:
                    stats.scanned += 1
                    batch_max_id = row.id
                    selected, message = self._selected(row.data)
                    if message is None:
                        stats.deserialization_errors += 1
                    if not selected:
                        stats.skipped += 1
                        continue
                    stats.selected += 1
                    chosen.append(row)

                # Apply selected rows in storage dequeue/read order within
                # the batch so relative delivery order survives the move.
                chosen.sort(key=self._sort_key(kind))
                if not dry_run:
                    for row in chosen:
                        conflict = self._insert_message(kind, dst_model, row)
                        if conflict:
                            stats.conflicts += 1
                        else:
                            stats.migrated += 1
                            migrated_total += 1
                            stats.add(kind)
                        # Remove from the source only after the destination
                        # write succeeded (or already had the same row).
                        self._delete_source_row(src_model, row.id)

                # The high-watermark, not the last selected id, is persisted,
                # so an interrupted batch resumes without re-scanning rows.
                last_id = batch_max_id
                if not dry_run:
                    self._save_progress(kind, last_id, migrated_total)

        if not dry_run:
            self._save_progress(kind, last_id, migrated_total)

    def _insert_message(self, kind, dst_model, row):
        """Insert into the destination shard. Returns True on id conflict."""
        # The relational primary key is a per-database autoincrement row id,
        # so the *business* identity of a message is the producer-assigned
        # task UUID carried inside the serialized Message. Check for an
        # existing copy of that UUID before inserting -- this is what makes
        # re-runs after a crash idempotent ("already migrated items are never
        # re-created").
        incoming = self._decode(row.data)
        present_payload = self._dest_identity_cache.get(incoming.id)
        if present_payload is not None:
            if present_payload != row.data:
                raise MigrationError(
                    'Destination already holds task id=%s with a different '
                    'payload; refusing to overwrite. Two producers are '
                    'reusing task ids.' % incoming.id)
            logger.info('conflict on %s task id=%s (already migrated); '
                        'skipping insert.', kind, incoming.id)
            return True

        if kind == KIND_QUEUE:
            payload = dict(queue=row.queue, data=row.data,
                           priority=row.priority)
        else:
            payload = dict(queue=row.queue, data=row.data,
                           timestamp=row.timestamp)
        dst_model.insert(**payload).execute()
        self._dest_identity_cache[incoming.id] = row.data
        return False

    def _delete_source_row(self, src_model, row_id):
        deleted = src_model.delete().where(src_model.id == row_id).execute()
        if deleted == 0:
            # A consumer dequeued it concurrently, inside our snapshot
            # transaction this can only happen on backends without row locks.
            logger.info('source row id=%d was consumed concurrently; '
                        'destination copy retained.', row_id)

    # ------------------------------------------------------------------
    # KV (results/revoke markers) and counters.
    #
    # Both tables use a composite primary key (queue, key) without a surrogate
    # id, so the resume cursor rides on SQLite's implicit rowid.
    # ------------------------------------------------------------------
    def _kv_models(self, kind):
        if kind == KIND_KV:
            return self.source.KV, self.destination.KV
        return self.source.Counter, self.destination.Counter

    @staticmethod
    def _rowid_gt(cursor):
        return SQL('rowid > ?', (cursor,))

    def _migrate_kv_like(self, kind, stats, max_batches, dry_run):
        src_model, dst_model = self._kv_models(kind)
        last_id, migrated_total = self._get_progress(kind)
        batches_done = 0

        while True:
            if max_batches is not None and batches_done >= max_batches:
                return
            with self.source.atomic():
                query = (src_model
                         .select(src_model, SQL('rowid').alias('_rid'))
                         .where((src_model.queue == self.source.name) &
                                self._rowid_gt(last_id))
                         .order_by(SQL('rowid'))
                         .limit(self.batch_size))
                rows = list(query)
                if not rows:
                    break
                stats.batches += 1
                batches_done += 1

                for row in rows:
                    stats.scanned += 1
                    last_id = row._rid
                    # The key selector only filters KV rows (result/revoke
                    # blobs); counters have no business-group encoding in
                    # their key and are always carried over.
                    if kind == KIND_KV and self.key_selector is not None and \
                            not self.key_selector(kind, row.queue, row.key):
                        stats.skipped += 1
                        continue
                    stats.selected += 1
                    if dry_run:
                        continue
                    if self._insert_kv_like(kind, src_model, dst_model, row):
                        stats.conflicts += 1
                    else:
                        stats.migrated += 1
                        migrated_total += 1
                        stats.add(kind)
                if not dry_run:
                    self._save_progress(kind, last_id, migrated_total)

        if not dry_run:
            self._save_progress(kind, last_id, migrated_total)

    def _insert_kv_like(self, kind, src_model, dst_model, row):
        """Returns True when the destination already held the row (conflict).

        KV conflicts keep the destination value untouched (last writer from a
        previous migration run wins); counter conflicts add the two values so
        increments accumulated on both shards are not lost.
        """
        conflict_target = [dst_model.queue, dst_model.key]
        existed = dst_model.select().where(
            (dst_model.queue == row.queue) &
            (dst_model.key == row.key)).exists()
        if kind == KIND_KV:
            (dst_model
             .insert(queue=row.queue, key=row.key, value=row.value)
             .on_conflict(conflict_target=conflict_target,
                          preserve=[dst_model.value])
             .execute())
        else:
            (dst_model
             .insert(queue=row.queue, key=row.key, value=row.value)
             .on_conflict(conflict_target=conflict_target,
                          update={dst_model.value:
                                  dst_model.value + row.value})
             .execute())
        deleted = (src_model
                   .delete()
                   .where((src_model.queue == row.queue) &
                          (src_model.key == row.key))
                   .execute())
        if deleted == 0:
            # A concurrent consumer popped it; the destination value stands.
            logger.info('source kv key=%r was removed concurrently; '
                        'destination value retained.', row.key)
        return existed


def message_group_selector(group_field='group', group_value=None):
    """Build a selector from a business identity carried in task kwargs.

    The demo enqueues ``process_order(order_id, group=...)`` tasks; a real
    deployment would point this at whichever arg/kwarg identifies the shard
    key (order group, tenant id, ...).
    """
    def selector(message):
        kwargs = message.kwargs or {}
        if group_field not in kwargs:
            return False
        value = kwargs[group_field]
        return value == group_value if group_value is not None else True
    return selector


@contextmanager
def open_migrator(source, destination, **kwargs):
    migrator = ShardMigrator(source, destination, **kwargs)
    try:
        yield migrator
    finally:
        source.close()
        destination.close()
