from functools import partial
import operator
import time

from peewee import *
from playhouse.db_url import connect as db_url_connect

from huey.api import Huey
from huey.constants import EmptyData
from huey.exceptions import ConfigurationError
from huey.storage import BaseStorage
from huey.storage import validate_purge_limit


class BytesBlobField(BlobField):
    def python_value(self, value):
        return value if isinstance(value, bytes) else bytes(value)


class SqlStorage(BaseStorage):
    supports_result_retention = True

    def __init__(self, name='huey', database=None, create_tables=True,
                 **kwargs):
        super(SqlStorage, self).__init__(name, **kwargs)

        if database is None:
            raise ConfigurationError('Use of SqlStorage requires a '
                                     'database= argument, which should be a '
                                     'peewee database or a connection string.')

        if isinstance(database, Database):
            self.database = database
        else:
            # Treat database argument as a URL connection string.
            self.database = db_url_connect(database)

        # Use BEGIN IMMEDIATE w/ SQLite.
        if isinstance(self.database, SqliteDatabase):
            self.atomic = partial(self.database.atomic, 'IMMEDIATE')
        else:
            self.atomic = self.database.atomic

        self.KV, self.Schedule, self.Task, self.Counter = self.create_models()
        if create_tables:
            self.create_tables()

        # Check for FOR UPDATE SKIP LOCKED support.
        if isinstance(self.database, PostgresqlDatabase):
            self.for_update = 'FOR UPDATE SKIP LOCKED'
        elif isinstance(self.database, MySQLDatabase):
            self.for_update = 'FOR UPDATE SKIP LOCKED'  # Assume support.
            # Try to determine if we're using MariaDB or MySQL.
            version, = self.database.execute_sql('select version()').fetchone()
            if 'mariadb' in str(version).lower():
                # MariaDB added support in 10.6.0.
                if self.database.server_version < (10, 6):
                    self.for_update = 'FOR UPDATE'
            elif self.database.server_version < (8, 0, 1):
                # MySQL added support in 8.0.1.
                self.for_update = 'FOR UPDATE'
        else:
            self.for_update = None

    def create_models(self):
        class Base(Model):
            class Meta:
                database = self.database

        class KV(Base):
            queue = CharField()
            key = CharField()
            value = BytesBlobField()
            # NULL for control data and for rows written before retention was
            # enabled; only non-NULL, elapsed timestamps are purged.
            expires = FloatField(null=True)
            class Meta:
                primary_key = CompositeKey('queue', 'key')

        class Schedule(Base):
            queue = CharField()
            data = BytesBlobField()
            timestamp = TimestampField(resolution=1000)
            class Meta:
                indexes = ((('queue', 'timestamp'), False),)

        class Task(Base):
            queue = CharField()
            data = BytesBlobField()
            priority = FloatField(default=0.0)

        Task.add_index(Task.priority.desc(), Task.id)

        class Counter(Base):
            queue = CharField()
            key = CharField()
            value = IntegerField()
            class Meta:
                primary_key = CompositeKey('queue', 'key')

        return (KV, Schedule, Task, Counter)

    def create_tables(self):
        with self.database:
            self.database.create_tables([self.KV, self.Schedule, self.Task,
                                         self.Counter])
            self._migrate_kv_expires()

    def _migrate_kv_expires(self):
        # Add the retention column to databases created by older versions.
        # Existing rows get a NULL expiration and are therefore never purged.
        columns = set(c.name for c in self.database.get_columns('kv'))
        if 'expires' not in columns:
            self.database.execute_sql(
                'alter table kv add column expires real')

        # An index on (queue, expires), used by the purge query. Use the
        # portable form rather than "create index if not exists", which is not
        # understood by every supported database (e.g. MySQL).
        indexes = set(ix.name for ix in self.database.get_indexes('kv'))
        if 'kv_queue_expires' not in indexes:
            self.database.execute_sql(
                'create index kv_queue_expires on kv (queue, expires)')

    def initialize_schema(self):
        # Shared entrypoint for the create_huey_tables management command.
        self.create_tables()

    def drop_tables(self):
        with self.database:
            self.database.drop_tables([self.KV, self.Schedule, self.Task,
                                       self.Counter])

    def close(self):
        closed = self.database.close()
        # A pool keeps the conn idle, and forked workers would share it.
        if hasattr(self.database, 'close_idle'):
            self.database.close_idle()
        return closed

    def tasks(self, *columns):
        return self.Task.select(*columns).where(self.Task.queue == self.name)

    def schedule(self, *columns):
        return (self.Schedule.select(*columns)
                .where(self.Schedule.queue == self.name))

    def kv(self, *columns):
        return self.KV.select(*columns).where(self.KV.queue == self.name)

    def check_conn(self):
        if not self.database.is_connection_usable():
            self.database.close()
            self.database.connect()

    def enqueue(self, data, priority=None):
        self.check_conn()
        self.Task.create(queue=self.name, data=data, priority=priority or 0)

    def dequeue(self):
        self.check_conn()
        query = (self.tasks(self.Task.id, self.Task.data)
                 .order_by(self.Task.priority.desc(), self.Task.id)
                 .limit(1))
        if self.for_update:
            query = query.for_update(self.for_update)

        with self.atomic():
            try:
                task = query.get()
            except self.Task.DoesNotExist:
                return

            nrows = self.Task.delete().where(self.Task.id == task.id).execute()
            if nrows == 1:
                return task.data

    def queue_size(self):
        return self.tasks().count()

    def enqueued_items(self, limit=None):
        query = self.tasks(self.Task.data).order_by(self.Task.priority.desc(),
                                                    self.Task.id)
        if limit is not None:
            query = query.limit(limit)
        return list(map(operator.itemgetter(0), query.tuples()))

    def flush_queue(self):
        self.Task.delete().where(self.Task.queue == self.name).execute()

    def add_to_schedule(self, data, timestamp):
        self.check_conn()
        self.Schedule.create(queue=self.name, data=data, timestamp=timestamp)

    def read_schedule(self, timestamp):
        self.check_conn()
        query = (self.schedule(self.Schedule.id, self.Schedule.data)
                 .where(self.Schedule.timestamp <= timestamp)
                 .order_by(self.Schedule.timestamp, self.Schedule.id)
                 .tuples())
        if self.for_update:
            query = query.for_update(self.for_update)

        with self.atomic():
            results = list(query)
            if not results:
                return []

            id_list, data = zip(*results)
            for chunk in chunked(id_list, 500):
                (self.Schedule
                 .delete()
                 .where(self.Schedule.id.in_(chunk))
                 .execute())

            return list(data)

    def schedule_size(self):
        return self.schedule().count()

    def scheduled_items(self, limit=None):
        tasks = (self.schedule(self.Schedule.data)
                 .order_by(self.Schedule.timestamp)
                 .tuples())
        if limit:
            tasks = tasks.limit(limit)
        return list(map(operator.itemgetter(0), tasks))

    def flush_schedule(self):
        (self.Schedule
         .delete()
         .where(self.Schedule.queue == self.name)
         .execute())

    def put_data(self, key, value, is_result=False):
        self.check_conn()
        # Results expire relative to the time they are written; control data
        # (revocations, locks, rate-limits, chord bookkeeping) is stored with
        # a NULL expiration and is therefore never purged. Re-writing a key
        # replaces any previous expiration.
        expires = self.result_expires_at() if is_result else None
        if isinstance(self.database, PostgresqlDatabase):
            # On conflict, take the incoming value (as before) and refresh the
            # expiration to match this write.
            (self.KV
             .insert(queue=self.name, key=key, value=value, expires=expires)
             .on_conflict(conflict_target=[self.KV.queue, self.KV.key],
                          preserve=[self.KV.value],
                          update={self.KV.expires: expires})
             .execute())
        else:
            self.KV.replace(queue=self.name, key=key, value=value,
                            expires=expires).execute()

    def _live(self):
        # Filter matched to results written under the retention policy that
        # have not yet expired. Control rows have a NULL expiration.
        return ((self.KV.expires.is_null()) |
                (self.KV.expires > time.time()))

    def peek_data(self, key):
        self.check_conn()
        try:
            kv = (self.kv(self.KV.value)
                  .where((self.KV.key == key) & self._live())
                  .get())
        except self.KV.DoesNotExist:
            return EmptyData
        else:
            return kv.value

    def peek_many(self, keys):
        self.check_conn()
        return dict(self.kv(self.KV.key, self.KV.value)
                    .where((self.KV.key.in_(keys)) & self._live())
                    .tuples())

    def pop_data(self, key):
        self.check_conn()
        query = self.kv().where((self.KV.key == key) & self._live())
        if self.for_update:
            query = query.for_update(self.for_update)

        with self.atomic():
            try:
                kv = query.get()
            except self.KV.DoesNotExist:
                return EmptyData
            else:
                dq = self.KV.delete().where(
                    (self.KV.queue == self.name) &
                    (self.KV.key == key))
                return kv.value if dq.execute() == 1 else EmptyData

    def has_data_for_key(self, key):
        self.check_conn()
        return (self.kv()
                .where((self.KV.key == key) & self._live())
                .exists())

    def put_if_empty(self, key, value, ttl=None):
        if ttl is not None:
            raise NotImplementedError('ttl is not supported by this storage.')
        self.check_conn()
        try:
            with self.atomic():
                self.KV.insert(queue=self.name, key=key, value=value).execute()
        except IntegrityError:
            return False
        else:
            return True

    def incr(self, key, amount=1):
        self.check_conn()
        with self.atomic():
            if isinstance(self.database, MySQLDatabase):
                self._incr_mysql(key, amount)
            else:
                self._incr(key, amount)

            return self.Counter.get(
                (self.Counter.queue == self.name) &
                (self.Counter.key == key)).value

    def _incr_mysql(self, key, amount=1):
        (self.Counter
         .insert(queue=self.name, key=key, value=amount)
         .on_conflict(update={self.Counter.value: self.Counter.value + amount})
         .execute())

    def _incr(self, key, amount=1):
        (self.Counter
         .insert(queue=self.name, key=key, value=amount)
         .on_conflict(
             conflict_target=(self.Counter.queue, self.Counter.key),
             update={self.Counter.value: self.Counter.value + amount})
         .execute())

    def delete_counter(self, key):
        self.check_conn()
        with self.atomic():
            self.Counter.delete().where(
                (self.Counter.queue == self.name) &
                (self.Counter.key == key)).execute()

    def result_store_size(self):
        return self.kv().where(self._live()).count()

    def result_items(self):
        query = (self.kv(self.KV.key, self.KV.value)
                 .where(self._live())
                 .tuples())
        return dict((k, v) for k, v in query.iterator())

    def purge_expired_results(self, limit=None, now=None):
        if self.result_retention is None:
            return 0
        limit = validate_purge_limit(limit, self.purge_batch_size)
        if not limit:
            return 0
        if now is None:
            now = time.time()
        self.check_conn()
        # Select candidates first, then delete by primary key, re-checking the
        # expiration. A concurrent update that pushes the expiration into the
        # future therefore wins, and other queues/control data are untouched.
        keys = list(self.kv(self.KV.key)
                    .where((self.KV.expires.is_null(False)) &
                           (self.KV.expires <= now))
                    .order_by(self.KV.expires)
                    .limit(limit)
                    .tuples())
        removed = 0
        with self.atomic():
            for chunk in chunked([k for k, in keys], 200):
                removed += (self.KV
                            .delete()
                            .where((self.KV.queue == self.name) &
                                   (self.KV.key.in_(chunk)) &
                                   (self.KV.expires.is_null(False)) &
                                   (self.KV.expires <= now))
                            .execute())
        return removed

    def flush_results(self):
        self.KV.delete().where(self.KV.queue == self.name).execute()

    def flush_counters(self):
        self.Counter.delete().where(self.Counter.queue == self.name).execute()


SqlHuey = partial(Huey, storage_class=SqlStorage)
