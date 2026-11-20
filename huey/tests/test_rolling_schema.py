"""
End-to-end rolling-deployment checks against a shared SQLite queue.

Two separate *processes* are used, each defining its own task module:

* the **old producer** registers only the old task name and writes the legacy
  message format;
* the **new consumer** registers only the new task name, with an alias and a
  registered argument migration.

The consumer must be able to read the messages the old process left behind,
and must refuse a message stamped with a future version, without executing it
and without leaking its arguments.
"""
import json
import os
import subprocess
import sys
import textwrap

from huey.api import SqliteHuey
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase


HUEY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

OLD_PRODUCER = textwrap.dedent('''
    import json
    import sys

    from huey import SqliteHuey

    db_path, out_path = sys.argv[1], sys.argv[2]
    huey = SqliteHuey('rolling', filename=db_path)

    # Old release: the task lives in the old module and takes (a, b).
    @huey.task(name='send_report_old')
    def send_report(a, b):
        return a + b

    task = send_report.s(*[json.loads(arg) for arg in sys.argv[3:]])
    # Follow-up in the same chain, to prove nested messages migrate too.
    task.then(send_report.s('chained'))
    huey.enqueue(task)

    size = huey.storage.queue_size()
    huey.storage.close()
    with open(out_path, 'w') as fh:
        json.dump({'queue_size': size}, fh)
''')

NEW_CONSUMER = textwrap.dedent('''
    import json
    import sys

    from huey import SqliteHuey
    from huey.schema import MessageSchema

    db_path, out_path, max_count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    OLD_NAME = '__main__.send_report_old'
    NEW_NAME = '__main__.send_report'

    def migrate_report(args, kwargs):
        # Old messages passed (a, b); the new task expects one payload list.
        if len(args) == 2 and 'chained' not in args:
            return ([list(args)], kwargs)
        return (args, kwargs)

    # Only the explicit alias and the explicit migration convert old
    # messages. Nothing is guessed from the short function name.
    schema = MessageSchema(
        version=2,
        legacy_version=1,
        aliases={OLD_NAME: NEW_NAME},
        migrations={NEW_NAME: [(1, 2, migrate_report)]})
    huey = SqliteHuey('rolling', filename=db_path, message_schema=schema)

    @huey.task(name='send_report')
    def send_report(*args):
        return list(args)

    executed = []
    rejected = []
    for _ in range(max_count):
        try:
            task = huey.dequeue()
        except Exception as exc:
            rejected.append({'type': type(exc).__name__, 'text': str(exc)})
            continue
        if task is None:
            break
        executed.append(huey.execute(task))
        # A completed task may have enqueued its on_complete follow-up.

    huey.storage.close()
    with open(out_path, 'w') as fh:
        json.dump({'executed': executed, 'rejected': rejected}, fh)
''')


NEW_PRODUCER = textwrap.dedent('''
    import json
    import sys

    from huey import SqliteHuey
    from huey.schema import MessageSchema

    db_path, out_path = sys.argv[1], sys.argv[2]
    # The new release bumps the message version and writes versioned messages.
    schema = MessageSchema(version=3, legacy_version=1)
    huey = SqliteHuey('rolling', filename=db_path, message_schema=schema)

    @huey.task(name='send_report')
    def send_report(a, b):
        return a + b

    huey.enqueue(send_report.s(*[json.loads(arg) for arg in sys.argv[3:]]))
    size = huey.storage.queue_size()
    huey.storage.close()
    with open(out_path, 'w') as fh:
        json.dump({'queue_size': size}, fh)
''')

LEGACY_CONSUMER = textwrap.dedent('''
    import json
    import sys

    from huey import SqliteHuey

    db_path, out_path = sys.argv[1], sys.argv[2]
    # Old release: no message schema support at all.
    huey = SqliteHuey('rolling', filename=db_path)

    @huey.task(name='send_report')
    def send_report(a, b):
        return a + b

    executed = []
    rejected = []
    for _ in range(5):
        try:
            task = huey.dequeue()
        except Exception as exc:
            rejected.append({'type': type(exc).__name__, 'text': str(exc)})
            continue
        if task is None:
            break
        executed.append(huey.execute(task))

    huey.storage.close()
    with open(out_path, 'w') as fh:
        json.dump({'executed': executed, 'rejected': rejected}, fh)
''')


CHORD_PRODUCER = textwrap.dedent('''
    import json
    import sys

    from huey import SqliteHuey, group

    db_path, out_path = sys.argv[1], sys.argv[2]
    # Old release produces a group + chord callback under the old names.
    huey = SqliteHuey('rolling', filename=db_path)

    @huey.task(name='member_old')
    def member(n):
        return n * 2

    @huey.task(name='callback_old')
    def callback(results):
        return results

    huey.enqueue(group([member.s(1), member.s(2)]).then(callback.s()))
    size = huey.storage.queue_size()
    huey.storage.close()
    with open(out_path, 'w') as fh:
        json.dump({'queue_size': size}, fh)
''')


CHORD_CONSUMER = textwrap.dedent('''
    import json
    import sys

    from huey import SqliteHuey
    from huey.schema import MessageSchema

    db_path, out_path, max_count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    schema = MessageSchema(
        version=2,
        # The argument shape did not change for this deployment: only the
        # task names moved. Unversioned messages are already current.
        aliases={
            '__main__.member_old': '__main__.member',
            '__main__.callback_old': '__main__.callback',
        })
    huey = SqliteHuey('rolling', filename=db_path, message_schema=schema)

    @huey.task(name='member')
    def member(n):
        return n * 2

    @huey.task(name='callback')
    def callback(results):
        return results

    executed = []
    rejected = []
    for _ in range(max_count):
        try:
            task = huey.dequeue()
        except Exception as exc:
            rejected.append({'type': type(exc).__name__, 'text': str(exc)})
            continue
        if task is None:
            break
        executed.append(huey.execute(task))

    huey.storage.close()
    with open(out_path, 'w') as fh:
        json.dump({'executed': executed, 'rejected': rejected}, fh)
''')


class TestRollingSqlite(BaseTestCase):
    def setUp(self):
        super(TestRollingSqlite, self).setUp()
        self.db_path = os.path.join(TEST_TMP_DIR, 'rolling-%s.db' % id(self))

    def tearDown(self):
        for suffix in ('', '-shm', '-wal'):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.unlink(path)
        super(TestRollingSqlite, self).tearDown()

    def _run(self, source, args):
        path = os.path.join(TEST_TMP_DIR, 'rolling_script_%s.py' % id(args))
        with open(path, 'w') as fh:
            fh.write(source)
        try:
            proc = subprocess.run(
                [sys.executable, '-I', path] + list(args),
                cwd=HUEY_ROOT, capture_output=True, text=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def _tmp(self, name):
        return os.path.join(TEST_TMP_DIR, 'rolling-%s-%s' % (id(self), name))

    def test_old_producer_new_consumer_rolling_order(self):
        # 1. Old release enqueues a legacy message plus a chained follow-up.
        out = self._tmp('old.json')
        self._run(OLD_PRODUCER, [self.db_path, out, '1', '2'])
        with open(out) as fh:
            self.assertEqual(json.load(fh)['queue_size'], 1)

        # 2. New release consumes it, migrating args and the nested message
        #    using only explicitly registered aliases and migrations.
        out2 = self._tmp('new.json')
        self._run(NEW_CONSUMER, [self.db_path, out2, '1'])
        with open(out2) as fh:
            result = json.load(fh)
        self.assertEqual(result['rejected'], [])
        # (1, 2) was converted by the registered migration to ([(1, 2)],).
        self.assertEqual(result['executed'], [[[1, 2]]])

        # 3. Executing the parent enqueued the migrated follow-up message,
        #    which the new consumer reads with the same alias and migration.
        out3 = self._tmp('new2.json')
        self._run(NEW_CONSUMER, [self.db_path, out3, '5'])
        with open(out3) as fh:
            result = json.load(fh)
        self.assertEqual(result['rejected'], [])
        # The follow-up kept its own ('chained',) argument and received the
        # migrated parent's return value, exactly as an un-migrated chain
        # would.
        self.assertEqual(result['executed'], [['chained', [[1, 2]]]])

    def test_future_version_rejected_in_subprocess(self):
        from huey.registry import Message
        from huey.registry import VersionedMessage
        from huey.serializer import Serializer

        payload = Serializer().serialize(VersionedMessage(99, Message(
            'task-future', '__main__.send_report', None, 0, 0, 0,
            ('s3cret-arg', 's3cret-arg-2'), {}, None, None, None, None,
            None, None, None)))
        huey = SqliteHuey('rolling', filename=self.db_path)
        huey.storage.enqueue(payload)
        huey.storage.close()

        out = self._tmp('future.json')
        self._run(NEW_CONSUMER, [self.db_path, out, '5'])
        with open(out) as fh:
            result = json.load(fh)
        self.assertEqual(result['executed'], [])
        self.assertEqual(len(result['rejected']), 1)
        rejected = result['rejected'][0]
        self.assertIn('MessageVersionError', rejected['type'])
        self.assertIn('version=99', rejected['text'])
        self.assertIn('task-future', rejected['text'])
        self.assertNotIn('s3cret', rejected['text'])

        # The message was dequeued and rejected, not executed; nothing is left
        # in the queue to be run by a later, older consumer.
        huey = SqliteHuey('rolling', filename=self.db_path)
        try:
            self.assertEqual(huey.pending_count(), 0)
        finally:
            huey.storage.close()

    def test_chord_callback_migrated_in_subprocess(self):
        # Old producer: group + chord callback under old task names.
        out = self._tmp('chordold.json')
        self._run(CHORD_PRODUCER, [self.db_path, out])
        with open(out) as fh:
            self.assertEqual(json.load(fh)['queue_size'], 2)

        # New consumer: aliases cover the member and the nested callback.
        out2 = self._tmp('chordnew.json')
        self._run(CHORD_CONSUMER, [self.db_path, out2, '4'])
        with open(out2) as fh:
            result = json.load(fh)
        self.assertEqual(result['rejected'], [])
        # Two members ran, then the callback received both results.
        self.assertEqual(result['executed'], [2, 4, [2, 4]])

    def test_new_producer_old_consumer_direction(self):
        # A new process (version 3) writes a versioned message. A process that
        # was not upgraded cannot interpret it: it must fail loudly rather
        # than run it with defaults.
        out = self._tmp('newproducer.json')
        self._run(NEW_PRODUCER, [self.db_path, out, '2', '3'])
        with open(out) as fh:
            self.assertEqual(json.load(fh)['queue_size'], 1)

        out2 = self._tmp('oldconsumer.json')
        self._run(LEGACY_CONSUMER, [self.db_path, out2])
        with open(out2) as fh:
            result = json.load(fh)
        self.assertEqual(result['executed'], [])
        self.assertEqual(len(result['rejected']), 1)
        # An old consumer fails at unpickling; the payload is opaque to it.
        self.assertIn('Error', result['rejected'][0]['type'])
