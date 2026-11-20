from huey.tests import TEST_TMP_DIR
import os
import subprocess
import sys
import threading
import time
import unittest

from huey.api import MemoryHuey
from huey.api import SqliteHuey
from huey.api import TaskLease
from huey.api import TaskLock
from huey.exceptions import CancelExecution
from huey.exceptions import TaskLockedException
from huey.storage import BaseStorage
from huey.storage import decode_lease
from huey.storage import encode_lease
from huey.tests.base import BaseTestCase


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


class FakeClock(object):
    def __init__(self, now=1000000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LeaseStorageTests(object):
    """
    Storage-level lease semantics, exercised against both MemoryStorage and
    SqliteStorage with a controllable clock.
    """
    def test_acquire_release_cycle(self):
        s = self.huey.storage
        self.assertTrue(s.acquire_lease('k1', 'owner-a', 10))
        self.assertFalse(s.acquire_lease('k1', 'owner-b', 10))
        self.assertFalse(s.acquire_lease('k1', 'owner-a', 10))
        self.assertTrue(s.release_lease('k1', 'owner-a'))
        self.assertFalse(s.release_lease('k1', 'owner-a'))
        self.assertTrue(s.acquire_lease('k1', 'owner-b', 10))

    def test_takeover_after_expiry(self):
        s = self.huey.storage
        self.assertTrue(s.acquire_lease('k1', 'owner-a', 10))
        self.clock.advance(9.9)
        self.assertFalse(s.acquire_lease('k1', 'owner-b', 10))
        self.clock.advance(0.2)  # Now past the expiry.
        self.assertTrue(s.acquire_lease('k1', 'owner-b', 10))

    def test_stale_holder_cannot_release_or_renew(self):
        s = self.huey.storage
        self.assertTrue(s.acquire_lease('k1', 'owner-a', 10))
        self.clock.advance(11)  # owner-a's lease expires.
        self.assertTrue(s.acquire_lease('k1', 'owner-b', 10))

        # The old holder comes back to life: it cannot release or renew the
        # new holder's lease.
        self.assertFalse(s.release_lease('k1', 'owner-a'))
        self.assertFalse(s.renew_lease('k1', 'owner-a', 10))
        self.assertFalse(s.acquire_lease('k1', 'owner-a', 10))

        # The current holder is unaffected.
        self.assertTrue(s.renew_lease('k1', 'owner-b', 10))
        self.assertTrue(s.release_lease('k1', 'owner-b'))

    def test_expired_lease_not_renewable(self):
        # Even when nobody took the lock over, a lease that has already
        # expired cannot be renewed - the holder must re-acquire.
        s = self.huey.storage
        self.assertTrue(s.acquire_lease('k1', 'owner-a', 10))
        self.clock.advance(11)
        self.assertFalse(s.renew_lease('k1', 'owner-a', 10))
        # Re-acquisition works, though.
        self.assertTrue(s.acquire_lease('k1', 'owner-a', 10))

    def test_renew_boundaries(self):
        s = self.huey.storage
        self.assertTrue(s.acquire_lease('k1', 'owner-a', 10))
        self.clock.advance(9)
        self.assertFalse(s.renew_lease('k1', 'owner-b', 10))  # Wrong owner.
        self.assertTrue(s.renew_lease('k1', 'owner-a', 10))  # Expires at 19.
        self.clock.advance(9.9)
        self.assertFalse(s.acquire_lease('k1', 'owner-b', 10))
        self.clock.advance(0.2)  # Past 19.
        self.assertTrue(s.acquire_lease('k1', 'owner-b', 10))

    def test_release_wrong_owner_is_noop(self):
        s = self.huey.storage
        self.assertTrue(s.acquire_lease('k1', 'owner-a', 10))
        self.assertFalse(s.release_lease('k1', 'owner-b'))
        self.assertFalse(s.release_lease('k1', 'nobody'))
        # Lock still held and usable by the real owner.
        self.assertTrue(s.renew_lease('k1', 'owner-a', 10))

    def test_legacy_lock_blocks_lease(self):
        # A legacy (no-lease) lock at the same key is an unexpiring hold:
        # leases cannot take it over and lease-release cannot remove it.
        s = self.huey.storage
        s.put_data('k1', b'1')
        self.assertFalse(s.acquire_lease('k1', 'owner-a', 10))
        self.clock.advance(100000)
        self.assertFalse(s.acquire_lease('k1', 'owner-a', 10))
        self.assertFalse(s.release_lease('k1', 'owner-a'))
        self.assertFalse(s.renew_lease('k1', 'owner-a', 10))
        self.assertEqual(s.peek_data('k1'), b'1')

    def test_lease_blocks_legacy_lock(self):
        # One valid holder per name across both locking modes.
        s = self.huey.storage
        self.assertTrue(s.acquire_lease('k1', 'owner-a', 10))
        self.assertFalse(s.put_if_empty('k1', b'1'))
        self.clock.advance(11)  # Lease expires; legacy lock still blocked
        self.assertFalse(s.put_if_empty('k1', b'1'))  # (key still exists).
        self.assertTrue(s.release_lease('k1', 'owner-a'))
        self.assertTrue(s.put_if_empty('k1', b'1'))

    def test_lease_value_encoding(self):
        value = encode_lease('owner-a', 1234.5)
        self.assertEqual(decode_lease(value), ('owner-a', 1234.5))
        self.assertEqual(decode_lease(memoryview(value)), ('owner-a', 1234.5))
        self.assertIsNone(decode_lease(b'1'))
        self.assertIsNone(decode_lease('1'))
        self.assertIsNone(decode_lease(None))
        self.assertIsNone(decode_lease(b'\x00huey-lease\x00short'))


class LeaseApiTests(object):
    """
    Huey-level behavior of lock_task(lease=...), including the actual
    decorator path through task execution.
    """
    def test_lock_task_lease_returns_task_lease(self):
        lock = self.huey.lock_task('lk', lease=10)
        self.assertIsInstance(lock, TaskLease)
        # Legacy usage is unchanged.
        legacy = self.huey.lock_task('lk2')
        self.assertIsInstance(legacy, TaskLock)
        self.assertNotIsInstance(legacy, TaskLease)
        legacy_ttl = self.huey.lock_task('lk3', ttl=10)
        self.assertIsInstance(legacy_ttl, TaskLock)

    def test_lock_task_validation(self):
        self.assertRaises(ValueError, self.huey.lock_task, 'lk', 1, 1)
        self.assertRaises(ValueError, self.huey.lock_task, 'lk', ttl=1,
                          lease=1)
        self.assertRaises(ValueError, self.huey.lock_task, 'lk', lease=0)
        self.assertRaises(ValueError, self.huey.lock_task, 'lk', lease=-5)

    def test_context_manager_and_is_locked(self):
        lock = self.huey.lock_task('lk', lease=10)
        self.assertFalse(lock.is_locked())
        with lock:
            self.assertTrue(lock.is_locked())
            self.assertTrue(lock.owner is not None)
        self.assertFalse(lock.is_locked())
        self.assertTrue(lock.owner is None)

    def test_is_locked_expiry_aware(self):
        lock = self.huey.lock_task('lk', lease=10)
        lock.acquire()
        self.clock.advance(11)
        # The stale entry still exists in the key/value store, but the lease
        # is expired and no longer counts as locked.
        self.assertFalse(lock.is_locked())

    def test_failed_acquire_cannot_delete_others_lock(self):
        holder = self.huey.lock_task('lk', lease=100)
        holder.acquire()

        other = self.huey.lock_task('lk', lease=100)
        with self.assertRaises(TaskLockedException):
            with other:
                pass
        # The failed context did not disturb the holder's lock.
        self.assertTrue(holder.is_locked())
        # Explicit release by the non-holder is a no-op, not a delete.
        self.assertFalse(other.release())
        self.assertFalse(other.clear())
        self.assertTrue(holder.is_locked())
        self.assertTrue(holder.release())
        self.assertFalse(holder.is_locked())

    def test_not_reentrant(self):
        lock = self.huey.lock_task('lk', lease=100)
        with lock:
            self.assertRaises(TaskLockedException, lock.acquire)
            with self.assertRaises(TaskLockedException):
                with lock:
                    pass
            # The outer hold is intact after the failed nested attempts.
            self.assertTrue(lock.is_locked())
            self.assertTrue(lock.renew())
        self.assertFalse(lock.is_locked())

    def test_owner_changes_per_acquire(self):
        lock = self.huey.lock_task('lk', lease=100)
        lock.acquire()
        first = lock.owner
        lock.release()
        lock.acquire()
        self.assertTrue(lock.owner is not None)
        self.assertNotEqual(lock.owner, first)
        lock.release()

    def test_clear_is_owner_checked(self):
        holder = self.huey.lock_task('lk', lease=100)
        holder.acquire()
        bystander = self.huey.lock_task('lk', lease=100)
        self.assertFalse(bystander.clear())
        self.assertTrue(holder.is_locked())
        self.assertTrue(holder.clear())
        self.assertFalse(holder.is_locked())

    def test_flush_locks_removes_lease(self):
        # flush_locks() is the administrative escape hatch: it removes leases
        # regardless of ownership.
        lock = self.huey.lock_task('lk', lease=100)
        lock.acquire()
        self.assertEqual(self.huey.flush_locks(), {'lk'})
        self.assertFalse(lock.is_locked())
        # The displaced holder's release is a harmless no-op, and the name
        # can be re-acquired immediately without waiting for the ttl.
        self.assertFalse(lock.release())
        other = self.huey.lock_task('lk', lease=100)
        other.acquire()
        other.release()

    def test_flush_locks_by_name(self):
        # Consistent with legacy locks, flush_locks(*names) removes the named
        # locks plus every lock registered on this huey instance - including
        # leases, regardless of ownership.
        l1 = self.huey.lock_task('lk1', lease=100)
        l2 = self.huey.lock_task('lk2', lease=100)
        l1.acquire()
        l2.acquire()
        self.assertEqual(self.huey.flush_locks('lk1'), {'lk1', 'lk2'})
        self.assertFalse(l1.is_locked())
        self.assertFalse(l2.is_locked())

    def test_no_background_threads(self):
        # Acquiring, renewing and releasing leases must not spawn any
        # background renewal threads.
        before = set(threading.enumerate())
        lock = self.huey.lock_task('lk', lease=100)
        with lock:
            lock.renew()
        self.assertEqual(set(threading.enumerate()), before)

    def test_locked_task_retried_after_lease_expires(self):
        @self.huey.task(retries=1)
        @self.huey.lock_task('lease_a', lease=10)
        def task_a(n):
            return n + 1

        holder = self.huey.lock_task('lease_a', lease=10)
        holder.acquire()

        r = task_a(3)
        # Blocked by the lock; a retry is pending so no error is stored.
        self.assertTrue(self.execute_next() is None)
        self.assertTrue(r.get() is None)

        # The holder dies without releasing; once its lease expires the
        # retried task takes the lock over and runs.
        self.clock.advance(11)
        self.assertEqual(self.execute_next(), 4)

        # The stale holder cannot release or renew the task's lease (which
        # has already been released normally by task exit).
        self.assertFalse(holder.release())
        self.assertFalse(holder.renew())
        self.assertFalse(self.huey.lock_task('lease_a', lease=10).is_locked())

    def test_lease_released_on_task_exception(self):
        @self.huey.task()
        @self.huey.lock_task('lease_exc', lease=10)
        def task_exc():
            raise RuntimeError('boom')

        task_exc()
        self.assertTrue(self.execute_next() is None)
        # The lease was released on the way out; the name is immediately
        # available again.
        lock = self.huey.lock_task('lease_exc', lease=10)
        self.assertFalse(lock.is_locked())
        lock.acquire()
        lock.release()

    def test_lease_released_on_cancel(self):
        @self.huey.task()
        @self.huey.lock_task('lease_cancel', lease=10)
        def task_cancel():
            raise CancelExecution()

        task_cancel()
        self.execute_next()
        self.assertFalse(
            self.huey.lock_task('lease_cancel', lease=10).is_locked())

    def test_renew_inside_task(self):
        lock = self.huey.lock_task('lease_rn', lease=10)

        @self.huey.task()
        @lock
        def task_rn():
            self.clock.advance(9)
            return lock.renew()

        task_rn()
        self.assertTrue(self.execute_next())
        # Lease was released at task exit regardless of the renewal.
        self.assertFalse(lock.is_locked())

    def test_legacy_and_lease_mutually_exclusive(self):
        # A legacy lock and a lease on the same name exclude each other.
        legacy = self.huey.lock_task('shared')
        legacy.acquire()
        self.assertRaises(TaskLockedException,
                          self.huey.lock_task('shared', lease=10).acquire)
        legacy.release()

        lease = self.huey.lock_task('shared', lease=10)
        lease.acquire()
        self.assertRaises(TaskLockedException, legacy.acquire)
        lease.release()


class TestMemoryLease(LeaseStorageTests, LeaseApiTests, BaseTestCase):
    def setUp(self):
        self.clock = FakeClock()
        super(TestMemoryLease, self).setUp()

    def get_huey(self):
        return MemoryHuey(utc=False, clock=self.clock)


class TestSqliteLease(LeaseStorageTests, LeaseApiTests, BaseTestCase):
    db_file = os.path.join(TEST_TMP_DIR, 'huey-lease-test.db')

    def setUp(self):
        self.clock = FakeClock()
        super(TestSqliteLease, self).setUp()

    def tearDown(self):
        super(TestSqliteLease, self).tearDown()
        self.huey.storage.close()
        for suffix in ('', '-wal', '-shm'):
            if os.path.exists(self.db_file + suffix):
                os.unlink(self.db_file + suffix)

    def get_huey(self):
        return SqliteHuey(filename=self.db_file, utc=False, timeout=3,
                          clock=self.clock)

    def test_shared_db_queues_isolated(self):
        # Two queues sharing one database file have independent lock
        # namespaces.
        other = SqliteHuey('other-queue', filename=self.db_file, utc=False,
                           timeout=3, clock=self.clock)
        try:
            lock_a = self.huey.lock_task('same-name', lease=100)
            lock_b = other.lock_task('same-name', lease=100)
            lock_a.acquire()
            lock_b.acquire()  # Different queue: not blocked.

            self.assertEqual(self.huey.flush_locks(), {'same-name'})
            self.assertFalse(lock_a.is_locked())
            self.assertTrue(lock_b.is_locked())
            self.assertTrue(lock_b.release())
            self.assertEqual(other.flush_locks(), set())
        finally:
            other.storage.close()


class TestLeaseUnsupportedBackend(BaseTestCase):
    def test_base_storage_raises(self):
        s = BaseStorage('huey')
        self.assertRaises(NotImplementedError, s.acquire_lease, 'k', 'o', 1)
        self.assertRaises(NotImplementedError, s.renew_lease, 'k', 'o', 1)
        self.assertRaises(NotImplementedError, s.release_lease, 'k', 'o')


# Child script: takes the lease through the actual lock_task(lease=...)
# decorator API, then "crashes" (os._exit) inside the critical section
# without releasing. Writes a marker file right after acquiring so the
# parent knows when the lease was taken.
CRASH_CHILD = '''\
import os
import sys
import time

from huey import SqliteHuey

huey = SqliteHuey('lease-sub', filename=sys.argv[1], utc=False)
lock = huey.lock_task('crash-lock', lease=float(sys.argv[2]))

@lock
def critical_section():
    with open(sys.argv[3], 'w') as fh:
        fh.write(str(time.time()))
    os._exit(0)  # Hard crash: no __exit__, no release.

critical_section()
'''

# Child script: acquires the lease, publishes its owner token, then waits for
# the parent to take the lock over before attempting a (stale) renew and
# release with its old token.
STALE_CHILD = '''\
import os
import sys
import time

from huey import SqliteHuey

huey = SqliteHuey('lease-sub', filename=sys.argv[1], utc=False)
lock = huey.lock_task('stale-lock', lease=float(sys.argv[2]))
lock.acquire()
with open(sys.argv[3], 'w') as fh:
    fh.write(lock.owner)

go_file = sys.argv[4]
deadline = time.time() + 20
while not os.path.exists(go_file) and time.time() < deadline:
    time.sleep(0.05)

renewed = lock.renew()
released = lock.release()
with open(sys.argv[5], 'w') as fh:
    fh.write('renew=%s release=%s' % (renewed, released))
'''


class TestLeaseSubprocess(unittest.TestCase):
    """
    Prove cross-process lease behavior using real subprocesses and real
    time: takeover after an abnormal exit, and rejection of a stale
    holder's release/renew.
    """
    def setUp(self):
        import tempfile
        self.workdir = tempfile.mkdtemp(dir=TEST_TMP_DIR)
        self.db_file = os.path.join(self.workdir, 'lease-sub.db')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _write_script(self, name, source):
        path = os.path.join(self.workdir, name)
        with open(path, 'w') as fh:
            fh.write(source)
        return path

    def _popen(self, script, *args):
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join(
            [REPO_ROOT, env.get('PYTHONPATH', '')])
        return subprocess.Popen(
            [sys.executable, script] + list(args),
            cwd=self.workdir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _wait_for_file(self, path, timeout=15):
        deadline = time.time() + timeout
        while not os.path.exists(path):
            if time.time() > deadline:
                self.fail('timed out waiting for %s' % path)
            time.sleep(0.05)

    def test_takeover_after_abnormal_exit(self):
        marker_file = os.path.join(self.workdir, 'acquired')
        script = self._write_script('crash_child.py', CRASH_CHILD)
        proc = self._popen(script, self.db_file, '0.5', marker_file)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err.decode('utf-8', 'replace'))
        self._wait_for_file(marker_file)
        with open(marker_file) as fh:
            acquired_at = float(fh.read())

        huey = SqliteHuey('lease-sub', filename=self.db_file, utc=False)
        try:
            lock = huey.lock_task('crash-lock', lease=30)
            if time.time() - acquired_at < 0.5:
                # The dead child's lease has not expired yet: acquisition
                # must fail.
                self.assertRaises(TaskLockedException, lock.acquire)

            # Once the crashed child's lease expires, we take over.
            deadline = time.time() + 15
            while True:
                try:
                    lock.acquire()
                    break
                except TaskLockedException:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.05)
            self.assertTrue(time.time() - acquired_at >= 0.4)
            self.assertTrue(lock.is_locked())
            self.assertTrue(lock.release())
        finally:
            huey.storage.close()

    def test_stale_holder_cannot_release_or_renew(self):
        token_file = os.path.join(self.workdir, 'token')
        go_file = os.path.join(self.workdir, 'go')
        result_file = os.path.join(self.workdir, 'result')
        script = self._write_script('stale_child.py', STALE_CHILD)
        proc = self._popen(script, self.db_file, '0.5', token_file, go_file,
                           result_file)
        try:
            self._wait_for_file(token_file)
            with open(token_file) as fh:
                child_token = fh.read()
            self.assertTrue(child_token)

            # Let the child's lease expire, then take the lock over.
            time.sleep(0.7)
            huey = SqliteHuey('lease-sub', filename=self.db_file, utc=False)
            try:
                lock = huey.lock_task('stale-lock', lease=30)
                lock.acquire()

                # Wake the child, which now attempts to renew and release
                # using its stale token.
                open(go_file, 'w').close()
                self._wait_for_file(result_file)
                with open(result_file) as fh:
                    result = fh.read()
                self.assertEqual(result, 'renew=False release=False')

                # The takeover holder's lease survived the stale holder's
                # attempts.
                self.assertTrue(lock.is_locked())
                self.assertTrue(lock.renew())
                self.assertRaises(
                    TaskLockedException,
                    huey.lock_task('stale-lock', lease=1).acquire)
                self.assertTrue(lock.release())
            finally:
                huey.storage.close()
        finally:
            proc.wait(timeout=30)
            if proc.returncode != 0:
                out, err = proc.communicate()
                self.fail('child failed: %s' % err.decode('utf-8', 'replace'))
