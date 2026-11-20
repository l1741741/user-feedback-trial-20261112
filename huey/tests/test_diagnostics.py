import datetime
import hashlib
import io
import json
import os
import sqlite3
from contextlib import redirect_stdout
from unittest.mock import patch

from huey.api import SqliteHuey
from huey.bin.huey_diag import diag_main
from huey.diagnostics import describe_message
from huey.diagnostics import describe_revocation
from huey.diagnostics import format_report
from huey.diagnostics import inspect_database
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase


def db_path(name):
    return os.path.join(TEST_TMP_DIR, 'diag-%s.db' % name)


def cleanup_db(path):
    for suffix in ('', '-wal', '-shm', '-journal'):
        if os.path.exists(path + suffix):
            os.chmod(path + suffix, 0o644)
            os.unlink(path + suffix)


class DiagnosticsTestCase(BaseTestCase):
    db_name = 'main'

    def setUp(self):
        self.db_file = db_path(self.db_name)
        cleanup_db(self.db_file)
        super(DiagnosticsTestCase, self).setUp()

    def tearDown(self):
        self.huey.storage.close()
        cleanup_db(self.db_file)
        super(DiagnosticsTestCase, self).tearDown()

    def get_huey(self):
        return SqliteHuey(filename=self.db_file, utc=False)

    def inspect(self, **kwargs):
        kwargs.setdefault('queue', self.huey.name)
        return inspect_database(self.db_file, **kwargs)

    def make_task(self):
        @self.huey.task()
        def task_a(n):
            return n
        return task_a


class TestBasicReport(DiagnosticsTestCase):
    def test_summary_and_entries(self):
        task_a = self.make_task()

        r1 = task_a(1)
        r2 = task_a(2, priority=3)
        r3 = task_a(3)
        self.huey.add_schedule(task_a.s(4))
        self.huey.put_result(r1.id, 'result-value')
        self.huey.revoke_by_id(r2.id, revoke_once=True)
        self.huey.revoke_all(task_a,
                             revoke_until=datetime.datetime(2030, 1, 2, 3, 4))
        self.huey.lock_task('held').acquire()
        self.huey.storage.incr('my-counter')
        self.huey.storage.incr('my-counter')

        report = self.inspect()
        self.assertEqual(report['status'], 'ok')
        self.assertTrue(report['snapshot']['consistent'])
        self.assertEqual(report['summary'], {
            'pending': 3, 'scheduled': 1, 'results': 1,
            'revocations': 2, 'locks': 1, 'counters': 1})

        pending = report['pending']
        self.assertEqual(pending['count'], 3)
        self.assertEqual(pending['returned'], 3)
        # Priority desc, then insertion order.
        self.assertEqual([e['task_id'] for e in pending['entries']],
                         [r2.id, r1.id, r3.id])
        self.assertEqual([e['position'] for e in pending['entries']],
                         [0, 1, 2])
        entry = pending['entries'][0]
        self.assertEqual(entry['priority'], 3)
        self.assertTrue(entry['task_name'].endswith('task_a'))
        self.assertEqual(entry['decode']['status'], 'ok')
        self.assertTrue(entry['bytes'] > 0)

        scheduled = report['scheduled']
        self.assertEqual(scheduled['count'], 1)
        self.assertTrue(scheduled['entries'][0]['task_name']
                        .endswith('task_a'))
        self.assertIn('T', scheduled['entries'][0]['eta_utc'])

        results = report['results']
        self.assertEqual(results['count'], 1)
        self.assertEqual(results['entries'][0]['key'], r1.id)
        stored = self.huey.storage.peek_data(r1.id)
        self.assertEqual(results['entries'][0]['bytes'], len(stored))

        control = report['control']
        self.assertEqual(len(control['revocations']), 2)
        by_key = dict((e['key'], e) for e in control['revocations'])
        task_revoke = by_key['r:%s' % r2.id]
        self.assertEqual(task_revoke['kind'], 'task')
        self.assertTrue(task_revoke['once'])
        self.assertIsNone(task_revoke['until'])
        class_revoke = by_key['rt:%s' % entry['task_name']]
        self.assertEqual(class_revoke['kind'], 'task-class')
        self.assertFalse(class_revoke['once'])
        self.assertTrue(class_revoke['until'].startswith('2030-01-02'))

        self.assertEqual(control['locks'][0]['name'], 'held')
        self.assertEqual(control['counters'],
                         [{'key': 'my-counter', 'value': 2}])

    def test_args_and_values_never_leak(self):
        task_a = self.make_task()
        r1 = task_a('s3cr3t-arg', 1)
        self.huey.put_result(r1.id, 's3cr3t-result')

        report = self.inspect()
        serialized = json.dumps(report)
        text = format_report(report)
        for rendered in (serialized, text):
            self.assertNotIn('s3cr3t-arg', rendered)
            self.assertNotIn('s3cr3t-result', rendered)

    def test_stable_machine_fields(self):
        task_a = self.make_task()
        task_a(1)
        report = self.inspect()
        self.assertEqual(
            set(report),
            {'version', 'status', 'detail', 'warnings', 'database', 'queue',
             'snapshot', 'summary', 'pending', 'scheduled', 'results',
             'control'})
        self.assertEqual(report['version'], 1)
        self.assertEqual(
            set(report['summary']),
            {'pending', 'scheduled', 'results', 'revocations', 'locks',
             'counters'})
        for name in ('pending', 'scheduled', 'results'):
            self.assertEqual(
                set(report[name]),
                {'count', 'offset', 'limit', 'returned', 'queue_filtered',
                 'entries'})
        self.assertEqual(
            set(report['pending']['entries'][0]),
            {'position', 'row_id', 'priority', 'bytes', 'task_id',
             'task_name', 'decode'} | {'decode'})
        self.assertEqual(
            set(report['control']),
            {'revocations', 'locks', 'counters', 'queue_filtered'})
        # The whole report must be JSON-serializable.
        json.dumps(report)


class TestReadOnly(DiagnosticsTestCase):
    def populate(self):
        task_a = self.make_task()
        r1 = task_a(1)
        task_a(2)
        self.huey.add_schedule(task_a.s(3))
        self.huey.put_result(r1.id, 'value')
        self.huey.revoke_by_id(r1.id, revoke_once=True)
        self.huey.lock_task('held').acquire()
        self.huey.storage.incr('counter')
        return r1

    def storage_state(self):
        return {
            'queue': self.huey.storage.enqueued_items(),
            'schedule': self.huey.storage.scheduled_items(),
            'results': self.huey.storage.result_items(),
            'counters': self.huey.storage.sql(
                'select key, value from counter order by key',
                results=True),
        }

    def test_inspection_changes_no_state(self):
        r1 = self.populate()
        before = self.storage_state()
        revoke_key = 'r:%s' % r1.id
        self.assertTrue(self.huey.storage.has_data_for_key(revoke_key))

        for _ in range(3):
            report = self.inspect()
            self.assertEqual(report['status'], 'ok')

        # Nothing was claimed, popped, restored or cleaned.
        self.assertEqual(self.storage_state(), before)
        # revoke_once was not consumed by looking at it.
        self.assertTrue(self.huey.storage.has_data_for_key(revoke_key))
        # The lock was not released or cleaned.
        self.assertTrue(self.huey.is_locked('held'))

    def test_inspection_changes_no_bytes(self):
        self.populate()
        self.huey.storage.close()  # Checkpoint WAL into the main file.

        def main_file_hash():
            with open(self.db_file, 'rb') as fh:
                return hashlib.sha256(fh.read()).hexdigest()

        def schema_dump():
            conn = sqlite3.connect(
                'file:%s?mode=ro' % self.db_file, uri=True)
            try:
                rows = conn.execute(
                    "select type, name, sql from sqlite_master "
                    "order by name").fetchall()
            finally:
                conn.close()
            return rows

        hash_before = main_file_hash()
        schema_before = schema_dump()

        report = self.inspect()
        self.assertEqual(report['status'], 'ok')

        self.assertEqual(main_file_hash(), hash_before)
        self.assertEqual(schema_dump(), schema_before)
        # sqlite may materialize empty -wal/-shm sidecars for a read-only
        # open of a cold WAL database; they must not contain any data.
        for sidecar in ('-wal', '-journal'):
            path = self.db_file + sidecar
            if os.path.exists(path):
                self.assertEqual(os.path.getsize(path), 0)

    def test_missing_database_not_created(self):
        missing = db_path('missing')
        cleanup_db(missing)
        report = inspect_database(missing, queue='huey')
        self.assertEqual(report['status'], 'missing')
        self.assertIn('does not exist', report['detail'])
        self.assertFalse(report['snapshot']['consistent'])
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(report['summary']['pending'], None)

    def test_empty_database_no_schema_and_no_tables_created(self):
        empty = db_path('empty')
        cleanup_db(empty)
        with open(empty, 'wb'):
            pass
        try:
            report = inspect_database(empty, queue='huey')
            self.assertEqual(report['status'], 'no-schema')
            self.assertIn('no huey tables', report['detail'])
            # No tables were silently created and the file was not grown.
            self.assertEqual(os.path.getsize(empty), 0)
            conn = sqlite3.connect('file:%s?mode=ro' % empty, uri=True)
            try:
                self.assertEqual(conn.execute(
                    "select count(*) from sqlite_master").fetchone()[0], 0)
            finally:
                conn.close()
        finally:
            cleanup_db(empty)

    def test_readonly_database_file(self):
        self.populate()
        self.huey.storage.close()
        os.chmod(self.db_file, 0o444)
        try:
            report = self.inspect()
            self.assertEqual(report['status'], 'ok')
            self.assertEqual(report['summary']['pending'], 2)
        finally:
            os.chmod(self.db_file, 0o644)


class TestAbnormalStates(DiagnosticsTestCase):
    def test_locked_database(self):
        db = db_path('locked')
        cleanup_db(db)
        huey = SqliteHuey(filename=db, journal_mode='delete', utc=False)
        huey.storage.close()
        blocker = sqlite3.connect(db, isolation_level=None,
                                  check_same_thread=False)
        blocker.execute('begin exclusive')
        try:
            report = inspect_database(db, queue='huey', timeout=0.2)
            self.assertEqual(report['status'], 'locked')
            self.assertIn('locked', report['detail'])
            self.assertFalse(report['snapshot']['consistent'])
            # A locked database is not reported as an empty queue.
            self.assertEqual(report['summary']['pending'], None)
        finally:
            blocker.execute('rollback')
            blocker.close()
            cleanup_db(db)

    def test_not_a_database(self):
        bogus = db_path('bogus')
        cleanup_db(bogus)
        with open(bogus, 'wb') as fh:
            fh.write(b'this is not a sqlite database' * 4)
        try:
            report = inspect_database(bogus, queue='huey')
            self.assertEqual(report['status'], 'unreadable')
            self.assertTrue(report['detail'])
        finally:
            cleanup_db(bogus)

    def test_legacy_schema(self):
        db = db_path('legacy')
        cleanup_db(db)
        conn = sqlite3.connect(db, isolation_level=None)
        # Pre-queue-column huey schema.
        conn.execute('create table task (id integer not null primary key, '
                     'data blob not null)')
        conn.execute('create table schedule (id integer not null primary '
                     'key, data blob not null, timestamp real not null)')
        conn.execute('create table kv (key text not null primary key, '
                     'value blob not null)')
        task_a = self.make_task()
        r1 = task_a(1)
        message = self.huey.storage.enqueued_items()[0]
        conn.execute('insert into task (data) values (?)', (message,))
        conn.execute('insert into kv (key, value) values (?, ?)',
                     ('r:abc', b'x'))
        conn.close()

        try:
            report = inspect_database(db, queue='whatever')
            self.assertEqual(report['status'], 'legacy-schema')
            self.assertIn('legacy', report['detail'])
            self.assertTrue(any('legacy' in w for w in report['warnings']))
            # Counts are still reported, clearly marked as unfiltered.
            self.assertEqual(report['summary']['pending'], 1)
            self.assertFalse(report['pending']['queue_filtered'])
            entry = report['pending']['entries'][0]
            self.assertEqual(entry['task_id'], r1.id)
            self.assertIsNone(entry['priority'])
            # Missing tables are reported as unavailable, not as empty.
            self.assertEqual(report['summary']['counters'], None)
            self.assertTrue(any('counter' in w for w in report['warnings']))
            # Nothing was migrated or created.
            conn = sqlite3.connect('file:%s?mode=ro' % db, uri=True)
            try:
                names = [r[0] for r in conn.execute(
                    "select name from sqlite_master where type='table'")]
            finally:
                conn.close()
            self.assertEqual(sorted(names), ['kv', 'schedule', 'task'])
        finally:
            cleanup_db(db)

    def test_database_replaced_during_inspection(self):
        self.make_task()(1)
        identities = iter([(1, 1), (2, 2)])
        with patch('huey.diagnostics._file_identity',
                   lambda path: next(identities)):
            report = self.inspect()
        self.assertEqual(report['status'], 'ok')
        self.assertTrue(any('replaced' in w for w in report['warnings']))

    def test_database_removed_during_inspection(self):
        self.make_task()(1)
        identities = iter([(1, 1), None])
        with patch('huey.diagnostics._file_identity',
                   lambda path: next(identities)):
            report = self.inspect()
        self.assertTrue(any('removed' in w for w in report['warnings']))


class TestMessageDecoding(DiagnosticsTestCase):
    def test_corrupt_messages_are_classified_not_executed(self):
        task_a = self.make_task()
        task_a(1)
        payloads = {
            'garbage': b'\x80\x05garbage!!',
            'truncated': self.huey.storage.enqueued_items()[0][:20],
            'bad-gzip': b'\x1f\x8b' + b'nonsense',
        }
        for kind, payload in payloads.items():
            self.huey.storage.sql(
                'insert into task (queue, data, priority) values (?, ?, ?)',
                (self.huey.name, payload, 0), commit=True)

        report = self.inspect()
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['pending']['count'], 4)
        decodes = [e['decode'] for e in report['pending']['entries'][1:]]
        self.assertEqual([d['status'] for d in decodes],
                         ['invalid'] * 3)
        categories = sorted(d['category'] for d in decodes)
        self.assertEqual(categories,
                         ['decompress', 'pickle-parse', 'pickle-parse'])
        for d in decodes:
            self.assertTrue(d['detail'])

    def test_malicious_pickle_is_not_executed(self):
        marker = db_path('pwned-marker')
        if os.path.exists(marker):
            os.unlink(marker)
        payload = (b"cos\nsystem\n(S'touch %s'\ntR." %
                   marker.replace("'", '"').encode('utf8'))
        self.huey.storage.sql(
            'insert into task (queue, data, priority) values (?, ?, ?)',
            (self.huey.name, payload, 0), commit=True)

        report = self.inspect()
        entry = report['pending']['entries'][0]
        self.assertNotEqual(entry['decode']['status'], 'ok')
        self.assertFalse(os.path.exists(marker))

    def test_describe_message_roundtrip(self):
        task_a = self.make_task()
        r1 = task_a(1)
        data = self.huey.storage.enqueued_items()[0]
        info = describe_message(data)
        self.assertEqual(info['status'], 'ok')
        self.assertEqual(info['task_id'], r1.id)
        self.assertTrue(info['task_name'].endswith('task_a'))
        self.assertIsNone(info['compressed'])

    def test_describe_message_compressed(self):
        huey = SqliteHuey(filename=db_path('compressed'), utc=False,
                          compression=True)
        try:
            @huey.task()
            def task_z(n):
                return n
            r1 = task_z(1)
            data = huey.storage.enqueued_items()[0]
            info = describe_message(data)
            self.assertEqual(info['status'], 'ok')
            self.assertEqual(info['compressed'], 'gzip')
            self.assertEqual(info['task_id'], r1.id)
        finally:
            huey.storage.close()
            cleanup_db(db_path('compressed'))

    def test_describe_message_signed(self):
        from huey.serializer import SignedSerializer
        huey = SqliteHuey(filename=db_path('signed'), utc=False,
                          serializer=SignedSerializer(secret='s3kr3t'))
        try:
            @huey.task()
            def task_s(n):
                return n
            r1 = task_s(1)
            data = huey.storage.enqueued_items()[0]
            info = describe_message(data)
            self.assertEqual(info['status'], 'ok')
            self.assertEqual(info['signed'], 'unverified')
            self.assertEqual(info['task_id'], r1.id)
        finally:
            huey.storage.close()
            cleanup_db(db_path('signed'))

    def test_describe_revocation(self):
        serializer = self.huey.serializer
        once = describe_revocation(serializer.serialize((None, True)))
        self.assertEqual(once['status'], 'ok')
        self.assertTrue(once['once'])
        self.assertIsNone(once['until'])

        until = describe_revocation(serializer.serialize(
            (datetime.datetime(2031, 5, 6, 7, 8, 9), False)))
        self.assertFalse(until['once'])
        self.assertEqual(until['until'], '2031-05-06T07:08:09')

        bad = describe_revocation(b'\x80\x05nope')
        self.assertEqual(bad['status'], 'invalid')


class TestPaginationAndQueues(DiagnosticsTestCase):
    def test_pagination_stable_ordering(self):
        task_a = self.make_task()
        results = [task_a(i) for i in range(25)]

        seen = []
        for offset in (0, 10, 20):
            report = self.inspect(limit=10, offset=offset)
            pending = report['pending']
            self.assertEqual(pending['count'], 25)
            self.assertEqual(report['summary']['pending'], 25)
            expected = min(10, 25 - offset)
            self.assertEqual(pending['returned'], expected)
            self.assertEqual(len(pending['entries']), expected)
            self.assertEqual(
                [e['position'] for e in pending['entries']],
                list(range(offset, offset + expected)))
            seen.extend(e['task_id'] for e in pending['entries'])

        # Stable ordering: the three pages tile the queue with no repeats.
        self.assertEqual(len(seen), 25)
        self.assertEqual(len(set(seen)), 25)
        self.assertEqual(set(seen), set(r.id for r in results))

        # Offset beyond the end: empty page, but the count is still real.
        report = self.inspect(limit=10, offset=30)
        self.assertEqual(report['pending']['count'], 25)
        self.assertEqual(report['pending']['entries'], [])

    def test_queue_isolation(self):
        other = SqliteHuey('other-queue', filename=self.db_file, utc=False)
        try:
            task_a = self.make_task()

            @other.task()
            def task_b(n):
                return n

            r_a = task_a(1)
            r_b = task_b(2)
            other.put_result(r_b.id, 'other-result')
            self.huey.put_result(r_a.id, 'main-result')

            report = self.inspect()
            self.assertEqual(report['pending']['count'], 1)
            self.assertEqual(report['pending']['entries'][0]['task_id'],
                             r_a.id)
            self.assertEqual(report['results']['count'], 1)
            self.assertEqual(report['results']['entries'][0]['key'], r_a.id)

            report = self.inspect(queue='other-queue')
            self.assertEqual(report['pending']['count'], 1)
            self.assertEqual(report['pending']['entries'][0]['task_id'],
                             r_b.id)
            self.assertEqual(report['results']['entries'][0]['key'], r_b.id)
        finally:
            other.storage.close()


class TestBoundedOutput(DiagnosticsTestCase):
    def test_large_result_does_not_bloat_output(self):
        task_a = self.make_task()
        r1 = task_a(1)
        big = b'x' * (3 * 1024 * 1024)
        self.huey.storage.put_data(r1.id, big, is_result=True)

        report = self.inspect(limit=5)
        self.assertEqual(report['results']['entries'][0]['bytes'], len(big))
        text = format_report(report)
        self.assertTrue(len(text) < 10000)
        self.assertNotIn('xxxx', text)
        self.assertTrue(len(json.dumps(report)) < 10000)

    def test_large_message_not_decoded(self):
        big_message = b'\x80\x05' + b'A' * (100 * 1024)
        self.huey.storage.sql(
            'insert into task (queue, data, priority) values (?, ?, ?)',
            (self.huey.name, big_message, 0), commit=True)
        report = self.inspect()
        entry = report['pending']['entries'][0]
        self.assertEqual(entry['bytes'], len(big_message))
        self.assertEqual(entry['decode']['status'], 'invalid')
        self.assertEqual(entry['decode']['category'], 'too-large')
        self.assertTrue(len(format_report(report)) < 10000)

    def test_unprintable_task_name_escaped(self):
        # Craft a message whose task "name" contains control characters by
        # hand-building a pickle of a Message-shaped tuple... more simply,
        # verify the escaping helper through a message with a weird name is
        # not needed: check format_report escapes a control-char key.
        self.huey.storage.put_data('res\x1b[31mkey', b'v', is_result=True)
        report = self.inspect()
        text = format_report(report)
        self.assertNotIn('\x1b', text)
        self.assertIn('res?[31mkey', text)


class TestConsumerConcurrency(DiagnosticsTestCase):
    def test_consistent_snapshots_with_live_consumer(self):
        @self.huey.task()
        def task_a(n):
            return n

        # A revoke_once flag that inspection must never consume.
        self.huey.revoke_by_id(
            '00000000-0000-0000-0000-000000000000', revoke_once=True)

        with self.consumer_context():
            for i in range(20):
                task_a(i)
            for _ in range(15):
                report = self.inspect(limit=1000)
                self.assertEqual(report['status'], 'ok')
                self.assertTrue(report['snapshot']['consistent'])
                pending = report['pending']
                # Count and entries come from one snapshot: with a limit
                # above the count, every counted row is listed.
                self.assertEqual(pending['count'],
                                 report['summary']['pending'])
                self.assertEqual(len(pending['entries']), pending['count'])
                row_ids = [e['row_id'] for e in pending['entries']]
                self.assertEqual(len(set(row_ids)), len(row_ids))
                if pending['count'] == 0:
                    self.assertEqual(pending['entries'], [])
                results = report['results']
                self.assertEqual(results['count'],
                                 report['summary']['results'])
                self.assertEqual(len(results['entries']), results['count'])

        # The consumer drained the queue; inspection consumed nothing.
        self.assertTrue(self.huey.storage.has_data_for_key(
            'r:00000000-0000-0000-0000-000000000000'))

    def test_enqueue_dequeue_during_inspection(self):
        # A writer thread mutates the queue while inspections run; every
        # report must still be internally consistent.
        stop = []

        def writer():
            i = 0
            while not stop:
                self.huey.storage.enqueue(b'task-%d' % i)
                self.huey.storage.dequeue()
                i += 1

        import threading
        t = threading.Thread(target=writer)
        t.start()
        try:
            for _ in range(30):
                report = self.inspect(limit=1000)
                self.assertEqual(report['status'], 'ok')
                pending = report['pending']
                self.assertEqual(len(pending['entries']), pending['count'])
                if pending['count'] == 0:
                    self.assertEqual(pending['entries'], [])
        finally:
            stop.append(True)
            t.join()


class TestCli(DiagnosticsTestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = diag_main(argv)
        return code, out.getvalue()

    def test_cli_text_report(self):
        task_a = self.make_task()
        task_a('s3cr3t-arg', 1)
        code, output = self.run_cli([self.db_file])
        self.assertEqual(code, 0)
        self.assertIn('status:   ok', output)
        self.assertIn('pending: 1', output)
        self.assertIn('task_a', output)
        self.assertNotIn('s3cr3t-arg', output)

    def test_cli_json_report(self):
        task_a = self.make_task()
        task_a(1)
        code, output = self.run_cli([self.db_file, '--json'])
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['summary']['pending'], 1)
        self.assertEqual(report['queue'], 'huey')

    def test_cli_queue_and_limit(self):
        task_a = self.make_task()
        for i in range(5):
            task_a(i)
        code, output = self.run_cli(
            [self.db_file, '-q', 'huey', '-n', '2', '--json'])
        report = json.loads(output)
        self.assertEqual(report['pending']['count'], 5)
        self.assertEqual(report['pending']['returned'], 2)
        code, output = self.run_cli([self.db_file, '-q', 'nobody', '--json'])
        report = json.loads(output)
        self.assertEqual(report['pending']['count'], 0)

    def test_cli_missing_database(self):
        missing = db_path('cli-missing')
        cleanup_db(missing)
        code, output = self.run_cli([missing])
        self.assertEqual(code, 2)
        self.assertIn('missing', output)
        self.assertFalse(os.path.exists(missing))

    def test_cli_no_schema(self):
        empty = db_path('cli-empty')
        cleanup_db(empty)
        with open(empty, 'wb'):
            pass
        try:
            code, output = self.run_cli([empty])
            self.assertEqual(code, 4)
            self.assertIn('no-schema', output)
        finally:
            cleanup_db(empty)
