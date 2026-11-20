from huey.tests import TEST_TMP_DIR
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import huey.storage as storage_module
from huey.api import FileHuey
from huey.constants import EmptyData
from huey.exceptions import CorruptMessageError
from huey.storage import FileStorage


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# Child programs are run with "python -I -c <prog> <repo-root> <path> ...".
# They insert the repo root into sys.path themselves so -I can be used.

# Crash after the temp file is written and fsynced, before the rename that
# would publish the message.
PUBLISH_CRASH = """\
import os, sys
sys.path.insert(0, sys.argv[1])
from huey.storage import FileStorage
s = FileStorage('crash-pub', path=sys.argv[2], reliable=True)
def _crash(*a):
    os._exit(3)
os.replace = _crash
s.enqueue(b'msg-crash-publish')
os._exit(0)
"""

# Crash after claiming a message (and reading it), before the claim file is
# removed.
CLAIM_CRASH = """\
import os, sys
sys.path.insert(0, sys.argv[1])
from huey.storage import FileStorage
s = FileStorage('crash-claim', path=sys.argv[2], reliable=True)
s.enqueue(b'msg-claimed')
def _crash(*a):
    os._exit(4)
os.unlink = _crash
data = s.dequeue()
os._exit(0 if data == b'msg-claimed' else 9)
"""

# Crash while reading a claimed message.
READ_CRASH = """\
import builtins, os, sys
sys.path.insert(0, sys.argv[1])
from huey.storage import FileStorage
s = FileStorage('crash-read', path=sys.argv[2], reliable=True)
s.enqueue(b'msg-read')
real_open = builtins.open
def crash_open(path, mode='r', *a, **k):
    if str(path).endswith('.tmp') and 'rb' in mode:
        os._exit(5)
    return real_open(path, mode, *a, **k)
builtins.open = crash_open
s.dequeue()
os._exit(9)
"""

PRODUCER = """\
import sys
sys.path.insert(0, sys.argv[1])
from huey.storage import FileStorage
s = FileStorage('race', path=sys.argv[2], reliable=True)
for i in range(int(sys.argv[4])):
    s.enqueue(('%s-%d' % (sys.argv[3], i)).encode('utf8'))
"""

CONSUMER = """\
import sys, time
sys.path.insert(0, sys.argv[1])
from huey.storage import FileStorage
s = FileStorage('race', path=sys.argv[2], reliable=True)
out = open(sys.argv[3], 'wb')
empty = 0
while empty < 20:
    data = s.dequeue()
    if data is None:
        empty += 1
        time.sleep(0.05)
    else:
        empty = 0
        out.write(data + b'\\n')
out.close()
"""


def run_child(program, *args):
    return subprocess.run(
        [sys.executable, '-I', '-c', program, REPO_ROOT] +
        [str(a) for a in args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)


def dead_pid():
    proc = subprocess.Popen([sys.executable, '-I', '-c', 'pass'])
    proc.wait()
    return proc.pid


class BaseReliableTestCase(unittest.TestCase):
    def setUp(self):
        super(BaseReliableTestCase, self).setUp()
        self.path = tempfile.mkdtemp(dir=TEST_TMP_DIR)
        self.addCleanup(shutil.rmtree, self.path, True)

    def storage(self, name='test', **kwargs):
        kwargs.setdefault('reliable', True)
        return FileStorage(name, path=self.path, **kwargs)


class TestDefaultModeUnchanged(BaseReliableTestCase):
    def test_default_is_not_reliable(self):
        s = FileStorage('default', path=self.path)
        self.assertFalse(s.reliable)
        self.assertEqual(s.recover(), {'requeued': 0, 'discarded': 0})

    def test_default_writes_raw_payload(self):
        s = FileStorage('default', path=self.path)
        s.enqueue(b'raw-message')
        names = os.listdir(s.queue_path)
        self.assertEqual(len(names), 1)
        with open(os.path.join(s.queue_path, names[0]), 'rb') as fh:
            # No framing header is added in default mode.
            self.assertEqual(fh.read(), b'raw-message')
        self.assertEqual(s.dequeue(), b'raw-message')
        self.assertIsNone(s.dequeue())

    def test_default_schedule_and_results(self):
        s = FileStorage('default', path=self.path)
        now = datetime.datetime.now()
        past = now - datetime.timedelta(seconds=10)
        s.add_to_schedule(b'due', past)
        self.assertEqual(s.read_schedule(now), [b'due'])
        s.put_data(b'k', b'v')
        self.assertEqual(s.pop_data(b'k'), b'v')
        self.assertEqual(s.incr(b'c'), 1)


class TestReliableDelivery(BaseReliableTestCase):
    def test_roundtrip_uses_framing(self):
        s = self.storage()
        s.enqueue(b'hello')
        names = os.listdir(s.queue_path)
        self.assertEqual(len(names), 1)
        with open(os.path.join(s.queue_path, names[0]), 'rb') as fh:
            payload = fh.read()
        self.assertTrue(payload.startswith(b'HMF1'))
        self.assertEqual(s.dequeue(), b'hello')
        self.assertIsNone(s.dequeue())
        self.assertEqual(os.listdir(s.queue_path), [])

    def test_priority_and_fifo(self):
        s = self.storage()
        s.enqueue(b'low', priority=1)
        s.enqueue(b'high', priority=9)
        s.enqueue(b'mid', priority=5)
        s.enqueue(b'a')
        s.enqueue(b'b')
        s.enqueue(b'c')
        order = [s.dequeue() for _ in range(6)]
        self.assertEqual(order, [b'high', b'mid', b'low', b'a', b'b', b'c'])

    def test_enqueued_items_decodes(self):
        s = self.storage()
        s.enqueue(b'one')
        s.enqueue(b'two')
        self.assertEqual(s.enqueued_items(), [b'one', b'two'])
        self.assertEqual(s.queue_size(), 2)

    def test_legacy_files_coexist(self):
        legacy = FileStorage('coexist', path=self.path)  # Default mode.
        legacy.enqueue(b'legacy-1')
        legacy.enqueue(b'legacy-2')

        # New-format deliveries coexist with legacy files in the same
        # directory; enqueue before dequeuing so filename ordering is
        # deterministic even within the same millisecond.
        s = FileStorage('coexist', path=self.path, reliable=True)
        s.enqueue(b'new-1')
        self.assertEqual(s.dequeue(), b'legacy-1')
        self.assertEqual(s.dequeue(), b'legacy-2')
        self.assertEqual(s.dequeue(), b'new-1')
        self.assertIsNone(s.dequeue())

    def test_huey_integration(self):
        huey = FileHuey('reliable-huey', path=self.path, reliable=True)
        self.assertTrue(huey.storage.reliable)

        @huey.task()
        def add(a, b):
            return a + b

        add(1, 2)
        self.assertEqual(huey.pending_count(), 1)
        task = huey.dequeue()
        self.assertIsNotNone(task)
        self.assertEqual(huey.execute(task), 3)
        self.assertEqual(huey.pending_count(), 0)


class TestPublishFailures(BaseReliableTestCase):
    def tmp_files(self, s):
        return [f for f in os.listdir(s.queue_path) if f.endswith('.tmp')]

    def test_fsync_failure_no_false_success(self):
        s = self.storage()
        with mock.patch.object(os, 'fsync',
                               side_effect=OSError(5, 'injected fsync')):
            self.assertRaises(OSError, s.enqueue, b'msg')
        # Not published, but the temp file remains as a recoverable scene.
        self.assertEqual(s.queue_size(), 0)
        self.assertIsNone(s.dequeue())
        self.assertEqual(len(self.tmp_files(s)), 1)
        # The temp file belongs to this (live) process, so recovery must
        # not discard it.
        self.assertEqual(s.recover(), {'requeued': 0, 'discarded': 0})
        self.assertEqual(len(self.tmp_files(s)), 1)

    def test_rename_failure_no_false_success(self):
        s = self.storage()
        with mock.patch.object(os, 'replace',
                               side_effect=OSError(5, 'injected rename')):
            self.assertRaises(OSError, s.enqueue, b'msg')
        self.assertEqual(s.queue_size(), 0)
        self.assertEqual(len(self.tmp_files(s)), 1)

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0,
                     'chmod does not restrict root')
    def test_write_failure_readonly_dir(self):
        s = self.storage()
        os.makedirs(s.queue_path)
        os.chmod(s.queue_path, 0o555)
        self.addCleanup(os.chmod, s.queue_path, 0o755)
        self.assertRaises(OSError, s.enqueue, b'msg')
        self.assertEqual(s.queue_size(), 0)


class TestConsumeFailures(BaseReliableTestCase):
    def test_read_failure_does_not_delete_original(self):
        s = self.storage()
        s.enqueue(b'precious')
        (original,) = os.listdir(s.queue_path)

        real_open = open

        def flaky_open(path, mode='r', *args, **kwargs):
            if str(path).endswith('.tmp') and 'rb' in mode:
                raise OSError(5, 'injected read failure')
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(storage_module, 'open', create=True,
                               side_effect=flaky_open):
            self.assertRaises(OSError, s.dequeue)

        # The only copy was restored under its original name, not deleted.
        self.assertEqual(os.listdir(s.queue_path), [original])
        self.assertEqual(s.dequeue(), b'precious')
        self.assertIsNone(s.dequeue())

    def test_corrupt_message_quarantined(self):
        s = self.storage()
        os.makedirs(s.queue_path, exist_ok=True)
        with open(os.path.join(s.queue_path, '0000-000000000001'), 'wb') as f:
            # Valid framing magic but a bad CRC.
            f.write(storage_module._MESSAGE_HEADER.pack(
                storage_module._MESSAGE_MAGIC, 5, 0) + b'abcde')
        s.enqueue(b'good')

        self.assertRaises(CorruptMessageError, s.dequeue)
        corrupt = s.corrupted_files()
        self.assertEqual(len(corrupt), 1)
        self.assertTrue(corrupt[0].endswith('.corrupt'))
        with open(corrupt[0], 'rb') as fh:
            self.assertTrue(fh.read().startswith(b'HMF1'))

        # The damaged message was not mistaken for an empty queue, and the
        # remaining good message is still delivered.
        self.assertEqual(s.dequeue(), b'good')
        self.assertIsNone(s.dequeue())

    def test_truncated_message_quarantined(self):
        s = self.storage()
        os.makedirs(s.queue_path, exist_ok=True)
        with open(os.path.join(s.queue_path, '0000-000000000001'), 'wb') as f:
            f.write(b'HMF1\x00')  # Truncated header.
        self.assertRaises(CorruptMessageError, s.dequeue)
        self.assertEqual(len(s.corrupted_files()), 1)


class TestRecovery(BaseReliableTestCase):
    def write_file(self, directory, name, content):
        os.makedirs(directory, exist_ok=True)
        filename = os.path.join(directory, name)
        with open(filename, 'wb') as fh:
            fh.write(content)
        return filename

    def test_requeues_dead_claim(self):
        s = self.storage()
        dead = dead_pid()
        self.write_file(s.queue_path,
                        '0000-0000000000ab.claim.%d.7.tmp' % dead,
                        b'recovered')
        self.assertEqual(s.recover(), {'requeued': 1, 'discarded': 0})
        self.assertEqual(s.dequeue(), b'recovered')
        self.assertIsNone(s.dequeue())

    def test_discards_dead_publish_tmp(self):
        s = self.storage()
        dead = dead_pid()
        tmp = self.write_file(s.queue_path,
                              '0000-0000000000ab.%d.3.tmp' % dead, b'junk')
        self.assertEqual(s.recover(), {'requeued': 0, 'discarded': 1})
        self.assertFalse(os.path.exists(tmp))
        self.assertEqual(s.queue_size(), 0)
        self.assertIsNone(s.dequeue())

    def test_requeues_legacy_claim(self):
        s = self.storage()
        self.write_file(s.queue_path, '0000-0000000000ab.tmp', b'legacy')
        self.assertEqual(s.recover(), {'requeued': 1, 'discarded': 0})
        self.assertEqual(s.dequeue(), b'legacy')

    def test_live_process_files_untouched(self):
        s = self.storage()
        live = os.getpid()
        tmp = self.write_file(s.queue_path,
                              '0000-0000000000ab.%d.1.tmp' % live, b'w')
        claim = self.write_file(s.queue_path,
                                '0000-0000000000ac.claim.%d.1.tmp' % live,
                                b'c')
        self.assertEqual(s.recover(), {'requeued': 0, 'discarded': 0})
        self.assertTrue(os.path.exists(tmp))
        self.assertTrue(os.path.exists(claim))

    def test_schedule_claim_recovered(self):
        s = self.storage()
        now = datetime.datetime.now()
        past = now - datetime.timedelta(seconds=10)
        s.add_to_schedule(b'delayed', past)
        (name,) = os.listdir(s.schedule_path)
        dead = dead_pid()
        os.rename(
            os.path.join(s.schedule_path, name),
            os.path.join(s.schedule_path,
                         '%s.claim.%d.1.tmp' % (name, dead)))
        # read_schedule() recovers the interrupted claim before reading.
        self.assertEqual(s.read_schedule(now), [b'delayed'])
        self.assertEqual(s.schedule_size(), 0)

    @unittest.skipUnless(hasattr(os, 'symlink'), 'requires symlink support')
    def test_symlink_entries_not_followed(self):
        s = self.storage()
        outside = tempfile.mkdtemp(dir=TEST_TMP_DIR)
        self.addCleanup(shutil.rmtree, outside, True)
        target = os.path.join(outside, 'payload')
        with open(target, 'wb') as fh:
            fh.write(b'important')

        dead = dead_pid()
        os.makedirs(s.queue_path, exist_ok=True)
        link = os.path.join(s.queue_path,
                            '0000-0000000000ab.claim.%d.1.tmp' % dead)
        os.symlink(target, link)

        self.assertEqual(s.recover(), {'requeued': 0, 'discarded': 0})
        self.assertTrue(os.path.islink(link))
        with open(target, 'rb') as fh:
            self.assertEqual(fh.read(), b'important')
        self.assertEqual(s.queue_size(), 0)

    @unittest.skipUnless(hasattr(os, 'symlink'), 'requires symlink support')
    def test_recovery_does_not_cross_queue_root(self):
        s = self.storage()
        outside = tempfile.mkdtemp(dir=TEST_TMP_DIR)
        self.addCleanup(shutil.rmtree, outside, True)
        dead = dead_pid()
        fake = os.path.join(outside, '0000-0000000000ab.claim.%d.1.tmp' % dead)
        with open(fake, 'wb') as fh:
            fh.write(b'external')

        # The "queue" directory is a symlink escaping the storage root.
        os.symlink(outside, s.queue_path)
        self.assertEqual(s.recover(), {'requeued': 0, 'discarded': 0})
        with open(fake, 'rb') as fh:
            self.assertEqual(fh.read(), b'external')
        os.unlink(s.queue_path)


class TestScheduleAndResults(BaseReliableTestCase):
    def test_schedule_reliable(self):
        s = self.storage()
        now = datetime.datetime.now()
        past = now - datetime.timedelta(seconds=10)
        future = now + datetime.timedelta(hours=1)
        s.add_to_schedule(b'due-1', past)
        s.add_to_schedule(b'due-2', past)
        s.add_to_schedule(b'later', future)
        self.assertEqual(s.schedule_size(), 3)
        self.assertEqual(s.read_schedule(now), [b'due-1', b'due-2'])
        self.assertEqual(s.scheduled_items(), [b'later'])
        self.assertEqual(s.schedule_size(), 1)

    def test_results_reliable(self):
        s = self.storage()
        s.put_data(b'k1', b'v1')
        self.assertEqual(s.peek_data(b'k1'), b'v1')
        self.assertTrue(s.has_data_for_key(b'k1'))
        self.assertFalse(s.put_if_empty(b'k1', b'x'))
        self.assertTrue(s.put_if_empty(b'k2', b'v2'))
        self.assertEqual(s.result_items(), {'k1': b'v1', 'k2': b'v2'})
        self.assertEqual(s.pop_data(b'k1'), b'v1')
        self.assertIs(s.pop_data(b'k1'), EmptyData)

    def test_result_write_failure_keeps_old_value(self):
        s = self.storage()
        s.put_data(b'k', b'old')
        with mock.patch.object(os, 'replace',
                               side_effect=OSError(5, 'injected rename')):
            self.assertRaises(OSError, s.put_data, b'k', b'new')
        self.assertEqual(s.peek_data(b'k'), b'old')
        self.assertEqual(s.result_items(), {'k': b'old'})

    def test_counters_reliable(self):
        s = self.storage()
        self.assertEqual(s.incr(b'c'), 1)
        self.assertEqual(s.incr(b'c', 5), 6)
        # Persisted across storage instances.
        s2 = self.storage()
        self.assertEqual(s2.incr(b'c'), 7)
        s2.delete_counter(b'c')
        self.assertEqual(s.incr(b'c'), 1)


class TestSubprocessCrashRecovery(BaseReliableTestCase):
    def test_crash_before_publish(self):
        proc = run_child(PUBLISH_CRASH, self.path)
        self.assertEqual(proc.returncode, 3, proc.stderr.decode('utf8'))

        # The interrupted publish left a temp file that is not a task.
        names = os.listdir(os.path.join(self.path, 'queue'))
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith('.tmp'))
        legacy = FileStorage('crash-pub', path=self.path)
        self.assertEqual(legacy.queue_size(), 0)
        self.assertIsNone(legacy.dequeue())

        # Restarting recovers: the dead writer's temp file is discarded and
        # nothing is falsely delivered.
        s = FileStorage('crash-pub', path=self.path, reliable=True)
        self.assertEqual(os.listdir(os.path.join(self.path, 'queue')), [])
        self.assertEqual(s.queue_size(), 0)
        self.assertIsNone(s.dequeue())

    def test_crash_after_claim(self):
        proc = run_child(CLAIM_CRASH, self.path)
        self.assertEqual(proc.returncode, 4, proc.stderr.decode('utf8'))

        # The message was claimed but the claim was never committed; it is
        # invisible to listings but recoverable.
        legacy = FileStorage('crash-claim', path=self.path)
        self.assertEqual(legacy.queue_size(), 0)

        s = FileStorage('crash-claim', path=self.path, reliable=True)
        self.assertEqual(s.dequeue(), b'msg-claimed')
        self.assertIsNone(s.dequeue())
        self.assertEqual(os.listdir(os.path.join(self.path, 'queue')), [])

    def test_crash_during_claim_read(self):
        proc = run_child(READ_CRASH, self.path)
        self.assertEqual(proc.returncode, 5, proc.stderr.decode('utf8'))

        s = FileStorage('crash-read', path=self.path, reliable=True)
        self.assertEqual(s.dequeue(), b'msg-read')
        self.assertIsNone(s.dequeue())
        self.assertEqual(os.listdir(os.path.join(self.path, 'queue')), [])


class TestMultiProcessRace(BaseReliableTestCase):
    def test_concurrent_consumers_no_duplicates(self):
        nproducers, nmessages, nconsumers = 4, 25, 4

        producers = []
        for i in range(nproducers):
            producers.append(subprocess.Popen(
                [sys.executable, '-I', '-c', PRODUCER, REPO_ROOT, self.path,
                 'p%d' % i, str(nmessages)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        for i, proc in enumerate(producers):
            _, stderr = proc.communicate()
            self.assertEqual(proc.returncode, 0,
                             'producer %d: %s' % (i, stderr.decode('utf8')))

        outfiles = [os.path.join(self.path, 'out-%d' % i)
                    for i in range(nconsumers)]
        consumers = []
        for outfile in outfiles:
            consumers.append(subprocess.Popen(
                [sys.executable, '-I', '-c', CONSUMER, REPO_ROOT, self.path,
                 outfile],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        for i, proc in enumerate(consumers):
            _, stderr = proc.communicate()
            self.assertEqual(proc.returncode, 0,
                             'consumer %d: %s' % (i, stderr.decode('utf8')))

        consumed = []
        for outfile in outfiles:
            with open(outfile, 'rb') as fh:
                consumed.extend(l for l in fh.read().splitlines() if l)

        expected = sorted(
            ('p%d-%d' % (i, j)).encode('utf8')
            for i in range(nproducers) for j in range(nmessages))
        # Every delivery went to exactly one consumer: no losses, no dupes.
        self.assertEqual(sorted(consumed), expected)

        s = FileStorage('race', path=self.path, reliable=True)
        self.assertEqual(s.queue_size(), 0)
