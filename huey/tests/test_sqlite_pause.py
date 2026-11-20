import datetime
import json
import os
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from huey import SqliteHuey
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def remove_db(path):
    for suffix in ('', '-wal', '-shm'):
        if os.path.exists(path + suffix):
            os.unlink(path + suffix)


class PauseTestCase(BaseTestCase):
    """A SqliteHuey over a fresh temp file, plus helpers for other queues."""
    def setUp(self):
        self.db_file = os.path.join(
            TEST_TMP_DIR, 'huey-pause-%s-%d.db' % (
                type(self).__name__, id(self)))
        remove_db(self.db_file)
        self.addCleanup(remove_db, self.db_file)

        super(PauseTestCase, self).setUp()
        # Closes run before the file is removed (cleanups are LIFO), because
        # open_queue() registers its own close after this one.
        self.addCleanup(self.huey.storage.close)

    def get_huey(self):
        return SqliteHuey('prints', filename=self.db_file, utc=False,
                          timeout=3)

    def open_queue(self, name='prints', create_tables=True):
        huey = SqliteHuey(name, filename=self.db_file, utc=False, timeout=3,
                          create_tables=create_tables)
        self.addCleanup(huey.storage.close)
        return huey


class TestSqlitePauseStorage(PauseTestCase):
    def test_default_queue_is_not_paused(self):
        # Callers that never touch pause keep the original behavior.
        self.assertFalse(self.huey.is_paused())
        self.assertEqual(self.huey.queue_status(),
                         {'queue': 'prints', 'paused': False,
                          'pending': 0, 'scheduled': 0})

        self.huey.storage.enqueue(b'a')
        self.assertEqual(self.huey.storage.dequeue(), b'a')

    def test_pause_blocks_new_dequeues_but_keeps_backlog(self):
        s = self.huey.storage
        s.enqueue(b'a')
        s.enqueue(b'b')

        self.assertTrue(self.huey.pause())  # The receipt is the return.
        self.assertTrue(self.huey.is_paused())

        # Dequeueing is blocked, yet nothing was removed.
        self.assertIsNone(s.dequeue())
        self.assertEqual(s.queue_size(), 2)
        self.assertEqual(self.huey.pending_count(), 2)
        self.assertEqual(len(self.huey), 2)

        # A status query must not report the paused backlog as drained.
        self.assertEqual(self.huey.queue_status(),
                         {'queue': 'prints', 'paused': True,
                          'pending': 2, 'scheduled': 0})

        self.assertTrue(self.huey.resume())
        self.assertFalse(self.huey.is_paused())
        self.assertEqual([s.dequeue(), s.dequeue(), s.dequeue()],
                         [b'a', b'b', None])

    def test_pause_and_resume_are_idempotent(self):
        self.assertTrue(self.huey.pause())
        self.assertTrue(self.huey.pause())
        self.assertTrue(self.huey.is_paused())

        self.huey.storage.enqueue(b'a')
        self.assertTrue(self.huey.resume())
        self.assertTrue(self.huey.resume())  # Resume while not paused.
        self.assertFalse(self.huey.is_paused())

        # Repeated calls did not lose or duplicate the backlog.
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertEqual(self.huey.storage.dequeue(), b'a')

    def test_pause_survives_new_connections_and_restarts(self):
        self.huey.storage.enqueue(b'a')
        self.assertTrue(self.huey.pause())

        # A fresh instance on the same file is what a restarted consumer or a
        # separate maintenance process gets.
        other = self.open_queue()
        self.assertTrue(other.is_paused())
        self.assertIsNone(other.storage.dequeue())
        self.assertEqual(other.pending_count(), 1)
        self.assertTrue(other.queue_status()['paused'])

        # Still true from yet another instance: this is not a per-connection
        # cache, and no consumer shutdown cleared it.
        third = self.open_queue()
        self.assertTrue(third.is_paused())

        other.resume()
        self.assertFalse(third.is_paused())
        self.assertEqual(third.storage.dequeue(), b'a')

    def test_pause_is_scoped_to_one_queue(self):
        other = self.open_queue('reports')
        self.huey.storage.enqueue(b'a')
        other.storage.enqueue(b'b')

        self.assertTrue(self.huey.pause())

        self.assertTrue(self.huey.is_paused())
        self.assertFalse(other.is_paused())
        self.assertIsNone(self.huey.storage.dequeue())
        self.assertEqual(other.storage.dequeue(), b'b')

        # Pausing the second queue does not lift the first queue's pause.
        self.assertTrue(other.pause())
        self.assertTrue(self.huey.is_paused())
        self.assertTrue(other.is_paused())
        other.resume()
        self.assertTrue(self.huey.is_paused())
        self.assertFalse(other.is_paused())

    def test_enqueue_and_schedule_while_paused(self):
        s = self.huey.storage
        self.assertTrue(self.huey.pause())

        # Producers keep accepting work during maintenance.
        s.enqueue(b'b')
        s.enqueue(b'c', priority=5)

        ts = datetime.datetime(2000, 1, 1)
        s.add_to_schedule(b's1', ts)
        self.assertEqual(self.huey.scheduled_count(), 1)
        self.assertEqual(self.huey.queue_status()['scheduled'], 1)
        self.assertEqual(s.read_schedule(ts), [b's1'])

        # Landing a due scheduled task in the queue is also just an enqueue.
        s.enqueue(b's1')
        self.assertEqual(self.huey.pending_count(), 3)
        self.assertIsNone(s.dequeue())

    def test_resume_restores_priority_then_fifo_order(self):
        s = self.huey.storage
        self.assertTrue(self.huey.pause())
        s.enqueue(b'low-1')
        s.enqueue(b'high-1', priority=5)
        s.enqueue(b'low-2')
        s.enqueue(b'mid-1', priority=2)
        s.enqueue(b'high-2', priority=5)
        s.enqueue(b'mid-2', priority=2)

        # The order while paused is exactly the order that is used on resume.
        paused_order = s.enqueued_items()

        self.assertTrue(self.huey.resume())
        order = [s.dequeue() for _ in range(6)]
        self.assertEqual(order, paused_order)
        # Priority descending, then enqueue order within a priority.
        self.assertEqual(order, [b'high-1', b'high-2', b'mid-1', b'mid-2',
                                 b'low-1', b'low-2'])

    def test_paused_dequeue_does_not_wait_for_write_lock(self):
        # Idle consumers on a paused queue must not sit in the busy-timeout
        # waiting for a writer, nor contend with producers.
        s = self.huey.storage
        s.enqueue(b'a')
        self.assertTrue(self.huey.pause())

        holder = sqlite3.connect(self.db_file, isolation_level=None,
                                 check_same_thread=False)
        holder.execute('begin immediate')
        try:
            start = time.monotonic()
            self.assertIsNone(s.dequeue())
            self.assertLess(time.monotonic() - start, 1.0)
        finally:
            holder.execute('rollback')
            holder.close()

        self.assertEqual(s.queue_size(), 1)  # Nothing was removed.

    def test_paused_worker_does_not_busy_poll(self):
        # An idle worker on a paused queue backs off just like it does on an
        # empty queue: dequeue returns None (it does not error) and the
        # worker sleeps between attempts.
        executed = []
        released = threading.Event()

        @self.huey.task()
        def record():
            executed.append(1)
            released.set()

        record()
        self.assertTrue(self.huey.pause())

        calls = []
        orig_dequeue = self.huey.storage.dequeue

        def counting_dequeue():
            calls.append(time.monotonic())
            return orig_dequeue()

        self.huey.storage.dequeue = counting_dequeue
        self.addCleanup(setattr, self.huey.storage, 'dequeue', orig_dequeue)

        consumer = self.huey.create_consumer(
            workers=1, periodic=False, initial_delay=0.02, backoff=1.5,
            max_delay=0.5, check_worker_health=False)
        consumer.start()
        try:
            time.sleep(0.7)
            # ~0.7s of backoff from 0.02s to 0.5s is a handful of polls;
            # busy-polling would be thousands.
            self.assertLess(len(calls), 20)
            self.assertEqual(executed, [])
            self.assertEqual(self.huey.pending_count(), 1)

            self.assertTrue(self.huey.resume())
            self.assertTrue(released.wait(10))
        finally:
            consumer.stop(graceful=True)

    def test_pause_committed_first_blocks_the_waiting_dequeue(self):
        # Dequeue and pause contend for SQLite's single write lock. This pins
        # the ordering deterministically: the pause is committed as the last
        # write of a transaction holding the write lock, while a dequeue is
        # already waiting on BEGIN EXCLUSIVE. When the lock is released the
        # dequeue must observe the committed pause and remove nothing.
        s = self.huey.storage
        s.enqueue(b'a')
        other = self.open_queue()

        holder = sqlite3.connect(self.db_file, isolation_level=None,
                                 check_same_thread=False)
        holder.execute('begin exclusive')
        # Exactly the row pause() writes, kept uncommitted for now.
        holder.execute('insert or replace into pause (queue, paused) '
                       'values (?, 1)', ('prints',))

        result = {}
        dequeue_t = threading.Thread(
            target=lambda: result.setdefault('data', s.dequeue()))
        dequeue_t.start()
        time.sleep(0.2)  # Let dequeue reach BEGIN EXCLUSIVE and block.

        holder.execute('commit')  # Pause is now strictly first.
        dequeue_t.join(timeout=10)
        holder.close()

        self.assertFalse(dequeue_t.is_alive())
        self.assertIsNone(result.get('data'))
        self.assertEqual(s.queue_size(), 1)
        self.assertTrue(other.is_paused())

    def test_dequeue_completed_first_is_allowed_to_continue(self):
        # The mirror ordering: the dequeue transaction commits first, so the
        # dequeued task is legitimately taken and continues to run. A pause
        # committed afterwards applies only to what is still queued.
        s = self.huey.storage
        s.enqueue(b'a')
        self.assertEqual(s.dequeue(), b'a')  # Dequeue completes first.

        self.assertTrue(self.huey.pause())
        s.enqueue(b'b')
        self.assertIsNone(s.dequeue())
        self.assertEqual(self.huey.pending_count(), 1)
        self.assertEqual(self.huey.storage.enqueued_items(), [b'b'])

    def test_pause_state_not_cleared_by_flush_queue(self):
        # Flushing is a producer-side operation; it must not turn pause back
        # into a running queue.
        s = self.huey.storage
        s.enqueue(b'a')
        self.assertTrue(self.huey.pause())
        s.flush_queue()
        self.assertTrue(self.huey.is_paused())
        self.assertEqual(self.huey.queue_status()['pending'], 0)

    def test_legacy_database_without_pause_table(self):
        # A database whose schema predates this feature must behave as an
        # ordinary, un-paused queue. Reopening with create_tables=True would
        # add the table, so use create_tables=False to simulate it.
        legacy = self.open_queue('legacy')
        legacy.storage.close()

        conn = sqlite3.connect(self.db_file, isolation_level=None)
        conn.execute('drop table if exists pause')
        conn.close()

        old = self.open_queue('legacy', create_tables=False)
        self.assertFalse(old.is_paused())
        self.assertFalse(old.queue_status()['paused'])
        old.storage.enqueue(b'a')
        self.assertEqual(old.storage.dequeue(), b'a')

        # Pause creates the table on demand, so the queue is then pausable.
        self.assertTrue(old.pause())
        self.assertTrue(old.is_paused())
        old.storage.enqueue(b'b')
        self.assertIsNone(old.storage.dequeue())
        old.resume()
        self.assertEqual(old.storage.dequeue(), b'b')

    def test_consumer_integration(self):
        # Intentional skip: real consumers are exercised as subprocesses in
        # TestSqlitePauseMultiProcess.
        pass


class TestPauseUnsupportedStorage(BaseTestCase):
    def get_huey(self):
        from huey import MemoryHuey
        return MemoryHuey(utc=False)

    def test_unsupported_storage_raises(self):
        # Explicit pause is a Sqlite feature; other backends must say so
        # rather than silently pretending the queue was paused.
        self.assertRaises(NotImplementedError, self.huey.pause)
        self.assertRaises(NotImplementedError, self.huey.resume)
        self.assertRaises(NotImplementedError, self.huey.is_paused)


# --- Multi-process end-to-end ----------------------------------------------
#
# Helper modules imported by independent subprocesses. Consumers append to an
# event log, so the test can tell exactly which tasks ran and which waited. A
# "blocker" task parks on a file marker, letting the test hold an already
# dequeued task in flight across the pause.

TASKS_MODULE = '''\
import os
import time

from huey import SqliteHuey

LOG_DIR = os.environ['PAUSE_LOG_DIR']
huey = SqliteHuey('prints', filename=os.environ['PAUSE_DB_FILE'], utc=False)


def log(entry):
    with open(os.path.join(LOG_DIR, 'events.log'), 'a') as fh:
        fh.write(entry + '\\n')
        fh.flush()
        os.fsync(fh.fileno())


def wait_for_marker(name, timeout=60):
    path = os.path.join(LOG_DIR, name)
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise RuntimeError('timed out waiting for ' + name)
        time.sleep(0.01)


@huey.task(name='record')
def record(label):
    log('start:%s' % label)
    log('done:%s' % label)


@huey.task(name='blocker', retries=1)
def blocker(label):
    log('start:%s' % label)
    open(os.path.join(LOG_DIR, 'held-' + label), 'w').close()
    wait_for_marker('release-' + label)
    if not os.path.exists(os.path.join(LOG_DIR, 'retried-' + label)):
        # Fail once so the task requeues itself while the queue is paused.
        open(os.path.join(LOG_DIR, 'retried-' + label), 'w').close()
        log('retry:%s' % label)
        raise RuntimeError('transient failure')
    log('done:%s' % label)
'''

MAINTENANCE_SCRIPT = '''\
import json
import os
import sys

from huey import SqliteHuey


def main():
    db_file, queue, command = sys.argv[1:4]
    huey = SqliteHuey(queue, filename=db_file, utc=False, timeout=10)
    if command == 'pause':
        receipt = huey.pause()
    elif command == 'resume':
        receipt = huey.resume()
    elif command == 'status':
        receipt = huey.queue_status()
    elif command == 'pending':
        # Listing tasks requires the application's task registry, so use the
        # app's own Huey instance (same queue, same database).
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import pause_tasks
        receipt = [t.args[0] for t in pause_tasks.huey.pending()]
    else:
        raise SystemExit('unknown command ' + command)
    print(json.dumps({'command': command, 'receipt': receipt},
                     sort_keys=True), flush=True)
    huey.storage.close()


main()
'''

CONSUMER_SCRIPT = '''\
import logging
import sys

sys.path.insert(0, sys.argv[1])

import pause_tasks
from huey.consumer import Consumer

logging.basicConfig(level=logging.CRITICAL)


def main():
    consumer = Consumer(
        pause_tasks.huey,
        workers=1,
        periodic=False,
        worker_type='thread',
        initial_delay=0.02,
        backoff=1.5,
        max_delay=0.25,
        scheduler_interval=1,
        check_worker_health=False)
    print('consumer-ready', flush=True)
    consumer.run()


main()
'''


class TestSqlitePauseMultiProcess(unittest.TestCase):
    """
    Temporary database, a separate maintenance process, and real consumers.

    Scenario:
      1. Two consumers run. One dequeues task 'held' and parks inside it.
      2. A separate maintenance process pauses 'prints' and returns a receipt.
      3. Only after the receipt is 'held' released. It fails once and its
         retry is written back to the still-paused queue.
      4. While paused, new tasks, a higher-priority task and a delayed task
         are produced; none may run. A sibling queue in the same database
         keeps running.
      5. Consumers are restarted; the pause survives.
      6. Resume drains the backlog in the queue's existing order (priority,
         then FIFO within a priority), including the retry.

    Every step waits on a concrete marker (a file, an exact queue count, or a
    logged event), never on a sleep that guesses execution order.
    """
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='huey-pause-e2e-', dir=TEST_TMP_DIR)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = os.path.join(self.tmp, 'huey.db')
        self.log_dir = os.path.join(self.tmp, 'logs')
        os.mkdir(self.log_dir)

        for name, body in (('pause_tasks.py', TASKS_MODULE),
                           ('maintenance.py', MAINTENANCE_SCRIPT),
                           ('consumer.py', CONSUMER_SCRIPT)):
            with open(os.path.join(self.tmp, name), 'w') as fh:
                fh.write(body)

        self.env = dict(os.environ)
        self.env['PAUSE_LOG_DIR'] = self.log_dir
        self.env['PAUSE_DB_FILE'] = self.db
        self.env['PYTHONPATH'] = REPO_ROOT + os.pathsep + \
            self.env.get('PYTHONPATH', '')

        self.consumers = []
        self.addCleanup(self.stop_all)

        sys.path.insert(0, self.tmp)
        self.addCleanup(sys.path.remove, self.tmp)
        self.addCleanup(sys.modules.pop, 'pause_tasks', None)
        old_env = dict(os.environ)
        os.environ.update(PAUSE_LOG_DIR=self.log_dir, PAUSE_DB_FILE=self.db)
        self.addCleanup(lambda: (os.environ.clear(),
                                 os.environ.update(old_env)))
        import pause_tasks
        self.tasks = pause_tasks
        # Same queue, same registry: parent-side enqueue and inspection.
        self.huey = pause_tasks.huey
        self.addCleanup(self.huey.storage.close)

        self.other = SqliteHuey('reports', filename=self.db, utc=False,
                                timeout=10)
        self.addCleanup(self.other.storage.close)

        @self.other.task(name='report')
        def report(label):
            path = os.path.join(self.log_dir, 'reports.log')
            with open(path, 'a') as fh:
                fh.write(label + '\n')
                fh.flush()
                os.fsync(fh.fileno())

        self.report = report

    # -- subprocess control -------------------------------------------------

    def start_consumer(self):
        stderr_path = os.path.join(self.tmp, 'consumer-%d.err'
                                   % len(self.consumers))
        stderr = open(stderr_path, 'w')
        self.addCleanup(stderr.close)
        proc = subprocess.Popen(
            [sys.executable, os.path.join(self.tmp, 'consumer.py'), REPO_ROOT],
            cwd=self.tmp, env=self.env, stdout=subprocess.PIPE,
            stderr=stderr, text=True)
        self.consumers.append(proc)
        ready, _, _ = select.select([proc.stdout], [], [], 20)
        if not ready or proc.stdout.readline().strip() != 'consumer-ready':
            self.fail('consumer failed to start')
        return proc

    def stop(self, proc):
        if proc.poll() is not None:
            return
        proc.send_signal(signal.SIGINT)  # Graceful stop.
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    def stop_all(self):
        for proc in self.consumers:
            self.stop(proc)
        self.consumers = []

    def assert_consumers_alive(self):
        for proc in self.consumers:
            self.assertIsNone(proc.poll(),
                              'consumer %s exited early' % proc.pid)

    def maintenance(self, command, queue='prints'):
        result = subprocess.run(
            [sys.executable, os.path.join(self.tmp, 'maintenance.py'),
             self.db, queue, command],
            cwd=self.tmp, env=self.env, capture_output=True, text=True,
            timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['receipt']

    # -- observation --------------------------------------------------------

    def marker(self, name):
        return os.path.join(self.log_dir, name)

    def events(self):
        path = os.path.join(self.log_dir, 'events.log')
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [line.strip() for line in fh if line.strip()]

    def started(self, events=None):
        return sorted(e.split(':', 1)[1] for e in (events or self.events())
                      if e.startswith('start:'))

    def finished(self, events=None):
        return sorted(e.split(':', 1)[1] for e in (events or self.events())
                      if e.startswith('done:'))

    def wait_for(self, predicate, description, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.assert_consumers_alive()
            if predicate():
                return
            time.sleep(0.01)
        self.fail('timed out waiting for %s (events=%r)' % (
            description, self.events()))

    # -- scenario -----------------------------------------------------------

    def test_pause_barrier_end_to_end(self):
        # Backlog that is legitimately already in the queue when we pause.
        self.tasks.blocker('held')
        self.tasks.record('pre-1')
        self.tasks.record('pre-2')

        self.start_consumer()
        self.start_consumer()

        # One consumer dequeues 'held' and parks inside it; the other drains
        # the two 'record' tasks.
        self.wait_for(lambda: os.path.exists(self.marker('held-held')),
                      "task 'held' to be dequeued")
        self.wait_for(lambda: self.finished() == ['pre-1', 'pre-2'],
                      'the pre-pause backlog to finish')
        self.assertEqual(self.started(), ['held', 'pre-1', 'pre-2'])
        self.assertNotIn('held', self.finished())

        # Pause from a separate process. The returned receipt means the state
        # is committed; repeating it is safe.
        self.assertEqual(self.maintenance('pause'), True)
        self.assertEqual(self.maintenance('pause'), True)
        status = self.maintenance('status')
        self.assertTrue(status['paused'])
        # The in-flight task was already taken, so it is not pending.
        self.assertEqual(status['pending'], 0)

        # Producers keep working during the pause: plain and high-priority
        # tasks, plus a task that is already due on the schedule.
        self.tasks.record('post-1')
        self.tasks.record('post-urgent', priority=10)
        self.tasks.record('post-2')
        due = self.tasks.record.s('post-due')
        due.eta = datetime.datetime.now() - datetime.timedelta(seconds=1)
        self.huey.add_schedule(due)
        self.assertEqual(self.maintenance('status')['scheduled'], 1)

        # The due scheduled task is moved into the queue by the consumers'
        # schedulers (a plain enqueue), but nothing may be dequeued while the
        # queue is paused.
        self.wait_for(lambda: self.maintenance('status')['scheduled'] == 0,
                      'the due task to leave the schedule')
        self.wait_for(lambda: self.maintenance('status')['pending'] == 4,
                      'four tasks to wait in the paused queue')
        self.assertEqual(self.started(), ['held', 'pre-1', 'pre-2'])
        self.assertEqual(self.finished(), ['pre-1', 'pre-2'])

        # Release 'held' only now. It fails once; the retry is written back
        # to the paused queue by the consumer and must not run.
        open(self.marker('release-held'), 'w').close()
        self.wait_for(lambda: 'retry:held' in self.events(),
                      "task 'held' to raise its retry")
        self.wait_for(lambda: self.maintenance('status')['pending'] == 5,
                      "task 'held' retry to be re-enqueued")
        self.assertNotIn('held', self.finished())
        self.assertEqual(self.started(), ['held', 'pre-1', 'pre-2'])
        self.assertEqual(self.finished(), ['pre-1', 'pre-2'])

        # A sibling queue in the same database is unaffected by the pause.
        self.report('sibling-1')
        consumer = self.other.create_consumer(
            workers=1, periodic=False, initial_delay=0.02, backoff=1.5,
            max_delay=0.25, check_worker_health=False)
        consumer.start()
        try:
            path = os.path.join(self.log_dir, 'reports.log')
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if os.path.exists(path):
                    with open(path) as fh:
                        if 'sibling-1' in fh.read():
                            break
                time.sleep(0.01)
            else:
                self.fail("sibling queue 'reports' did not run while "
                          "'prints' was paused")
        finally:
            consumer.stop(graceful=True)

        # Record the order the paused queue would dequeue in. This is the
        # order that resume must preserve.
        paused_order = self.maintenance('pending')
        self.assertEqual(sorted(paused_order),
                         ['held', 'post-1', 'post-2', 'post-due',
                          'post-urgent'])
        self.assertEqual(paused_order[0], 'post-urgent')
        events_before_resume = self.events()

        # Restart every consumer. A restart must not lift the pause.
        self.stop_all()
        self.start_consumer()

        status = self.maintenance('status')
        self.assertTrue(status['paused'])
        self.assertEqual(status['pending'], 5)
        self.assertEqual(self.maintenance('pending'), paused_order)
        self.assertEqual(self.events(), events_before_resume)

        # Resume, twice (idempotent). One consumer drains deterministically.
        self.assertEqual(self.maintenance('resume'), True)
        self.assertEqual(self.maintenance('resume'), True)
        self.assertFalse(self.maintenance('status')['paused'])

        self.wait_for(lambda: self.maintenance('status')['pending'] == 0,
                      'queue to drain after resume', timeout=30)
        self.wait_for(lambda: 'held' in self.finished(),
                      "the retried task 'held' to finish", timeout=30)

        # Every task that ran after the resume ran in the preserved order.
        # Take the log once the consumer is idle, so no late line is missed.
        for proc in self.consumers:
            self.stop(proc)
        self.consumers = []
        after = self.events()[len(events_before_resume):]
        self.assertEqual([e.split(':', 1)[1] for e in after
                          if e.startswith('start:')], paused_order)
        self.assertEqual(self.finished(), ['held', 'post-1', 'post-2',
                                           'post-due', 'post-urgent',
                                           'pre-1', 'pre-2'])


if __name__ == '__main__':
    unittest.main()
