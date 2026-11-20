from huey.tests import TEST_TMP_DIR
import os
import sqlite3
import threading
import time
import unittest

try:
    import peewee
except ImportError:
    peewee = None

try:
    from huey.contrib.sql_huey import SqlHuey
except ImportError:
    if peewee is not None:
        raise
from huey.tests.base import BaseTestCase
from huey.tests.test_storage import StorageTests


SQLHUEY_URL = os.environ.get('SQLHUEY_URL') or ('sqlite:///%s' % os.path.join(TEST_TMP_DIR, 'huey-sqlite.db'))


@unittest.skipIf(peewee is None, 'requires peewee')
class TestSqlStorage(StorageTests, BaseTestCase):
    db_file = os.path.join(TEST_TMP_DIR, 'huey-sqlite.db')
    supports_ttl = False
    supports_result_retention = True

    def setUp(self):
        if os.path.exists(self.db_file):
            os.unlink(self.db_file)
        super(TestSqlStorage, self).setUp()

    def tearDown(self):
        super(TestSqlStorage, self).tearDown()
        self.huey.storage.drop_tables()

    @classmethod
    def tearDownClass(cls):
        super(TestSqlStorage, cls).tearDownClass()
        if os.path.exists(cls.db_file):
            os.unlink(cls.db_file)

    def get_huey(self):
        return SqlHuey(database=SQLHUEY_URL, utc=False)

    def get_retention_huey(self, retention):
        return SqlHuey(database=SQLHUEY_URL, utc=False,
                       result_retention=retention)

    def test_sqlite_waits_for_write_lock(self):
        if not isinstance(self.s.database, peewee.SqliteDatabase):
            self.skipTest('requires sqlite')
        self.s.enqueue(b'a')
        conn = sqlite3.connect(self.s.database.database, isolation_level=None,
                               check_same_thread=False)
        conn.execute('begin immediate')  # Hold the write lock briefly.
        t = threading.Timer(0.1, conn.execute, ('commit',))
        t.start()
        self.assertEqual(self.s.dequeue(), b'a')
        t.join()
        conn.close()

    def test_sql_huey_basic(self):
        @self.huey.task()
        def task_a(n):
            return n + 1

        r1 = task_a(1)
        r2 = task_a(2)
        self.assertEqual(self.execute_next(), 2)
        self.assertEqual(len(self.huey), 1)
        self.assertEqual(self.huey.result_count(), 1)
        r2.revoke()
        self.assertEqual(self.huey.result_count(), 2)

        self.assertTrue(self.execute_next() is None)
        self.assertEqual(len(self.huey), 0)
        self.assertEqual(self.huey.result_count(), 1)

        r3 = task_a.schedule((3,), delay=10)
        self.assertEqual(len(self.huey), 1)
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(self.huey.scheduled_count(), 1)
        self.assertEqual(len(self.huey), 0)
        self.assertEqual(self.huey.result_count(), 1)

        tasks = self.huey.read_schedule(r3.task.eta)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].id, r3.id)

    def test_sql_huey_priority(self):
        @self.huey.task()
        def task_a(n):
            return n

        @self.huey.task(priority=1)
        def task_b(n):
            return n * 10

        task_a(1)
        task_b(2)
        task_a(3, priority=2)
        task_b(4, priority=2)
        task_a(5, priority=1)
        task_b(6, priority=0)
        task_a(7)
        task_b(8)

        results = [3, 40, 20, 5, 80, 1, 60, 7]
        for result in results:
            self.assertEqual(self.execute_next(), result)

        self.assertEqual(len(self.huey), 0)

    def test_create_tables(self):
        filename = os.path.join(TEST_TMP_DIR, 'huey-sqlite-ct.db')
        if os.path.exists(filename):
            os.unlink(filename)
        huey = SqlHuey('ct', database='sqlite:///%s' % filename,
                       create_tables=False)
        storage = huey.storage
        try:
            self.assertRaises(peewee.OperationalError, storage.queue_size)
            storage.initialize_schema()
            self.assertEqual(storage.queue_size(), 0)
        finally:
            storage.close()
            os.unlink(filename)

    def test_result_retention_concurrent_purge_and_update(self):
        # A purger and a result-writer run on separate connections against the
        # same database file, as they would in separate processes.
        filename = os.path.join(TEST_TMP_DIR, 'huey-sqlite-retention.db')
        if os.path.exists(filename):
            os.unlink(filename)
        url = 'sqlite:///%s' % filename
        # A long retention means the hot keys below never expire on their own,
        # so any loss would be a purge deleting a newer result by mistake.
        writer = SqlHuey('q', database=url, result_retention=3600,
                         utc=False).storage
        purger = SqlHuey('q', database=url, result_retention=3600,
                         utc=False).storage

        # Seed rows that are already expired to give the purger steady work.
        past = time.time() - 1
        for i in range(300):
            writer.database.execute_sql(
                'insert or replace into kv (queue, key, value, expires) '
                'values (?, ?, ?, ?)',
                ('q', 'cold%d' % i, b'x', past))

        hot_keys = ['hot%d' % i for i in range(5)]
        stop = threading.Event()
        errors = []

        def purge():
            try:
                while not stop.is_set():
                    purger.purge_expired_results(limit=10)
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        try:
            purger_thread = threading.Thread(target=purge)
            purger_thread.start()
            for i in range(100):
                for key in hot_keys:
                    writer.put_data(key, b'v%d' % i, is_result=True)
                time.sleep(0.001)
            stop.set()
            purger_thread.join()
            self.assertEqual(errors, [])

            # Every hot key still holds its most recent value.
            for key in hot_keys:
                self.assertEqual(writer.peek_data(key), b'v99')
            # Control data is never purged, even alongside expired results.
            writer.put_data('ctl', b'keep')
            purger.purge_expired_results(limit=10000)
            self.assertEqual(writer.peek_data('ctl'), b'keep')
            self.assertEqual(writer.result_store_size(), len(hot_keys) + 1)
        finally:
            stop.set()
            writer.close()
            purger.close()
            for suffix in ('', '-wal', '-shm'):
                if os.path.exists(filename + suffix):
                    os.unlink(filename + suffix)

    def test_legacy_database_control_values_not_expired(self):
        # A database created before retention existed has no "expires" column.
        # Enabling retention must not retroactively expire historical values.
        filename = os.path.join(TEST_TMP_DIR, 'huey-sqlite-legacy.db')
        if os.path.exists(filename):
            os.unlink(filename)
        conn = sqlite3.connect(filename)
        conn.execute('create table kv (queue text not null, key text not null, '
                     'value blob not null, primary key(queue, key))')
        conn.execute("insert into kv (queue, key, value) values "
                     "('huey', 'r:legacy-task', ?), ('huey', 'chord:c:0', ?)",
                     (sqlite3.Binary(b'\x80revoke'), sqlite3.Binary(b'\x80c')))
        conn.commit()
        conn.close()
        huey = SqlHuey('huey', database='sqlite:///%s' % filename,
                       result_retention=0.01, utc=False)
        storage = huey.storage
        try:
            # Historical values have NULL expiration and are still readable.
            self.assertEqual(storage.peek_data('r:legacy-task'),
                             b'\x80revoke')
            self.assertEqual(storage.peek_data('chord:c:0'), b'\x80c')
            self.assertEqual(storage.purge_expired_results(), 0)
            self.assertEqual(storage.result_store_size(), 2)
        finally:
            storage.close()
            os.unlink(filename)

    def test_consumer_integration(self):
        # Intentional skip.
        pass
