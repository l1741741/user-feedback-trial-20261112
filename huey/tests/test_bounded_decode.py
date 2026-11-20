import datetime
import gzip
import pickle
import threading
import time
import tracemalloc
import zlib

from huey.api import MemoryHuey
from huey.api import Result
from huey.api import ResultGroup
from huey.consumer import Worker
from huey.exceptions import ConfigurationError
from huey.exceptions import MessageDecodeError
from huey.exceptions import MessageTooLarge
from huey.serializer import Serializer
from huey.serializer import SignedSerializer
from huey.tests.base import BaseTestCase


# A safe deserialization probe: unpickling a Probe instance records its token
# in PROBE_LOG. This lets tests assert whether a given message actually
# reached (executable) pickle deserialization.
PROBE_LOG = []


def _record_probe(token):
    PROBE_LOG.append(token)
    return token


class Probe(object):
    def __init__(self, token):
        self.token = token

    def __reduce__(self):
        return (_record_probe, (self.token,))


def gzip_members(*chunks):
    return b''.join(gzip.compress(chunk) for chunk in chunks)


class ProbeTestCase(BaseTestCase):
    def setUp(self):
        super(ProbeTestCase, self).setUp()
        del PROBE_LOG[:]


class TestBoundedSerializerConfig(BaseTestCase):
    def test_invalid_budgets(self):
        for kw in ({'max_message_bytes': -1},
                   {'max_message_bytes': '1000'},
                   {'max_message_bytes': True},
                   {'max_decompressed_bytes': -1},
                   {'max_decompressed_bytes': 1.5}):
            self.assertRaises(ConfigurationError, Serializer, **kw)

    def test_bounded_property(self):
        self.assertFalse(Serializer().bounded)
        self.assertFalse(Serializer(compression=True).bounded)
        self.assertTrue(Serializer(max_message_bytes=10).bounded)
        self.assertTrue(Serializer(max_decompressed_bytes=10).bounded)

    def test_round_trip_with_budget(self):
        data = [None, 0, b'a' * 1024, ['k1', 'k2'], {'k1': 'v1'}]
        for use_zlib in (False, True):
            s = Serializer(compression=True, use_zlib=use_zlib,
                           max_message_bytes=2 ** 20,
                           max_decompressed_bytes=2 ** 20)
            for item in data:
                self.assertEqual(s.deserialize(s.serialize(item)), item)

        # No compression, budgets still apply and round-trip works.
        s = Serializer(max_message_bytes=2 ** 20,
                       max_decompressed_bytes=2 ** 20)
        for item in data:
            self.assertEqual(s.deserialize(s.serialize(item)), item)


class TestBoundedTransferBudget(ProbeTestCase):
    def test_serialized_size_budget(self):
        s = Serializer(max_message_bytes=64)
        small = s.serialize(('ok',))
        self.assertEqual(s.deserialize(small), ('ok',))

        big = s.serialize(('x' * 1024,))
        self.assertTrue(len(big) > 64)
        exc = self.trap_too_large(s, big)
        self.assertEqual(exc.what, 'serialized message size')
        self.assertEqual(exc.limit, 64)

    def test_transfer_budget_checked_before_decompression(self):
        # A bomb whose *compressed* size exceeds the transfer budget must be
        # rejected with the transfer-budget error, before any decompression
        # or deserialization takes place.
        s = Serializer(compression=True, max_message_bytes=16,
                       max_decompressed_bytes=2 ** 30)
        bomb = s.serialize(Probe('bomb')) + b'\x00' * 64
        exc = self.trap_too_large(s, bomb)
        self.assertEqual(exc.what, 'serialized message size')
        self.assertEqual(PROBE_LOG, [])

    def test_pickle_not_reached_when_over_budget(self):
        s = Serializer(max_message_bytes=16)
        data = s.serialize(Probe('never'))
        self.assertRaises(MessageTooLarge, s.deserialize, data)
        self.assertEqual(PROBE_LOG, [])

    def trap_too_large(self, s, data):
        try:
            s.deserialize(data)
        except MessageTooLarge as exc:
            return exc
        self.fail('MessageTooLarge not raised')


class TestBoundedDecompression(ProbeTestCase):
    def make_bomb(self, use_zlib, size=8 * 1024 * 1024):
        payload = pickle.dumps(b'\x00' * size)
        if use_zlib:
            return zlib.compress(payload, 9)
        return gzip.compress(payload, 9)

    def test_bomb_gzip(self):
        bomb = self.make_bomb(use_zlib=False)
        self.assertTrue(len(bomb) < 100 * 1024)  # Highly compressible.
        s = Serializer(compression=True, max_decompressed_bytes=1024 * 1024)
        exc = self.trap_too_large(s, bomb)
        self.assertEqual(exc.what, 'decompressed message size')
        self.assertEqual(exc.limit, 1024 * 1024)

    def test_bomb_zlib(self):
        bomb = self.make_bomb(use_zlib=True)
        s = Serializer(compression=True, use_zlib=True,
                       max_decompressed_bytes=1024 * 1024)
        self.trap_too_large(s, bomb)

    def test_bomb_memory_stays_bounded(self):
        # Prove decompression is incremental: a 64MB bomb decoded under a 1MB
        # budget must not transiently materialize anywhere near 64MB.
        bomb = self.make_bomb(use_zlib=False, size=64 * 1024 * 1024)
        s = Serializer(compression=True, max_decompressed_bytes=1024 * 1024)
        tracemalloc.start()
        try:
            self.trap_too_large(s, bomb)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(peak < 8 * 1024 * 1024,
                        'peak memory %d suggests full decompression' % peak)

    def test_exact_budget_allowed(self):
        s = Serializer(compression=True)
        data = s.serialize(['a' * 100])
        raw = gzip.decompress(data)
        bounded = Serializer(compression=True,
                             max_decompressed_bytes=len(raw))
        self.assertEqual(bounded.deserialize(data), ['a' * 100])
        too_small = Serializer(compression=True,
                               max_decompressed_bytes=len(raw) - 1)
        self.trap_too_large(too_small, data)

    def test_truncated_streams(self):
        s = Serializer(compression=True, max_decompressed_bytes=2 ** 20)
        data = s.serialize(('hello',) * 100)
        for cut in (2, len(data) // 2, len(data) - 1):
            self.assertRaises(MessageDecodeError, s.deserialize, data[:cut])

        sz = Serializer(compression=True, use_zlib=True,
                        max_decompressed_bytes=2 ** 20)
        data = sz.serialize(('hello',) * 100)
        for cut in (2, len(data) // 2, len(data) - 1):
            self.assertRaises(MessageDecodeError, sz.deserialize, data[:cut])

    def test_checksum_failure(self):
        s = Serializer(compression=True, max_decompressed_bytes=2 ** 20)
        data = bytearray(s.serialize(('hello',) * 100))
        data[-3] ^= 0xff  # Corrupt the gzip CRC.
        self.assertRaises(MessageDecodeError, s.deserialize, bytes(data))

        sz = Serializer(compression=True, use_zlib=True,
                        max_decompressed_bytes=2 ** 20)
        data = bytearray(sz.serialize(('hello',) * 100))
        data[-1] ^= 0xff  # Corrupt the zlib adler32.
        self.assertRaises(MessageDecodeError, sz.deserialize, bytes(data))

    def test_multi_member_gzip(self):
        # Concatenated gzip members are decoded, with the budget applied
        # cumulatively across members.
        payload = pickle.dumps(('x' * 1000,))
        mid = len(payload) // 2
        data = gzip_members(payload[:mid], payload[mid:])

        s = Serializer(compression=True, max_decompressed_bytes=len(payload))
        self.assertEqual(s.deserialize(data), ('x' * 1000,))

        too_small = Serializer(compression=True,
                               max_decompressed_bytes=mid + 1)
        self.assertRaises(MessageTooLarge, too_small.deserialize, data)

    def test_trailing_data_rejected(self):
        payload = pickle.dumps(('ok',))
        # Trailing garbage after a valid gzip member.
        s = Serializer(compression=True, max_decompressed_bytes=2 ** 20)
        self.assertRaises(MessageDecodeError, s.deserialize,
                          gzip.compress(payload) + b'garbage')
        # A second "member" that starts with the gzip magic but is truncated.
        self.assertRaises(MessageDecodeError, s.deserialize,
                          gzip.compress(payload) + b'\x1f\x8b')
        # Trailing data after a zlib stream.
        sz = Serializer(compression=True, use_zlib=True,
                        max_decompressed_bytes=2 ** 20)
        self.assertRaises(MessageDecodeError, sz.deserialize,
                          zlib.compress(payload) + b'x')

    def test_uncompressed_data_still_bounded(self):
        # Compression enabled but the data is not actually compressed: the
        # payload budget still applies before pickle.
        s = Serializer(compression=True, max_decompressed_bytes=32)
        data = pickle.dumps(('x' * 1024,))
        self.assertRaises(MessageTooLarge, s.deserialize, data)

        # Compression disabled entirely: budget applies to the payload.
        s = Serializer(max_decompressed_bytes=32)
        self.assertRaises(MessageTooLarge, s.deserialize, data)

    def test_pickle_not_reached_for_bad_streams(self):
        s = Serializer(compression=True, max_decompressed_bytes=2 ** 20)
        good = s.serialize(Probe('good'))
        self.assertRaises(MessageDecodeError, s.deserialize,
                          good[:len(good) // 2])
        self.assertRaises(MessageDecodeError, s.deserialize, good + b'junk')
        self.assertEqual(PROBE_LOG, [])

        # Sanity check: the intact probe message does reach pickle (the
        # probe's __reduce__ returns its token).
        self.assertEqual(s.deserialize(good), 'good')
        self.assertEqual(PROBE_LOG, ['good'])

    def trap_too_large(self, s, data):
        try:
            s.deserialize(data)
        except MessageTooLarge as exc:
            return exc
        self.fail('MessageTooLarge not raised')


class TestUnboundedCompatibility(BaseTestCase):
    """
    Without a configured budget, behavior (including exception types for
    malformed data) is unchanged from the legacy implementation.
    """
    def test_legacy_exceptions(self):
        s = Serializer(compression=True)
        data = s.serialize(('hello',) * 100)
        self.assertRaises(EOFError, s.deserialize, data[:len(data) // 2])
        corrupt = bytearray(data)
        corrupt[-3] ^= 0xff
        self.assertRaises(gzip.BadGzipFile, s.deserialize, bytes(corrupt))

        sz = Serializer(compression=True, use_zlib=True)
        data = sz.serialize(('hello',) * 100)
        self.assertRaises(zlib.error, sz.deserialize, data[:len(data) // 2])

    def test_legacy_multi_member(self):
        payload = pickle.dumps(('x',))
        mid = len(payload) // 2
        data = gzip_members(payload[:mid], payload[mid:])
        self.assertEqual(Serializer(compression=True).deserialize(data),
                         ('x',))

    def test_legacy_round_trip(self):
        for use_zlib in (False, True):
            s = Serializer(compression=True, use_zlib=use_zlib)
            self.assertEqual(s.deserialize(s.serialize([1, 2, 3])), [1, 2, 3])


class TestBoundedSignedSerializer(ProbeTestCase):
    secret = 'test-secret-key'
    args_sentinel = 'sensitive-task-argument'

    def get_serializer(self, **kwargs):
        kwargs.setdefault('secret', self.secret)
        return SignedSerializer(**kwargs)

    def test_round_trip(self):
        for use_zlib in (False, True):
            s = self.get_serializer(compression=True, use_zlib=use_zlib,
                                    max_message_bytes=2 ** 20,
                                    max_decompressed_bytes=2 ** 20)
            self.assertEqual(s.deserialize(s.serialize(('a', 1))), ('a', 1))

    def test_tampered_message_sanitized_error(self):
        s = self.get_serializer()
        data = bytearray(s.serialize((self.args_sentinel,)))
        data[len(data) // 2] ^= 0x01  # Flip a bit in the payload.
        try:
            s.deserialize(bytes(data))
        except ValueError as exc:
            msg = str(exc)
        else:
            self.fail('ValueError not raised')
        self.assertNotIn(self.secret, msg)
        self.assertNotIn(self.args_sentinel, msg)
        self.assertNotIn(bytes(data).hex(), msg)

    def test_missing_signature_sanitized_error(self):
        s = self.get_serializer()
        try:
            s.deserialize(pickle.dumps((self.args_sentinel,)))
        except ValueError as exc:
            msg = str(exc)
        else:
            self.fail('ValueError not raised')
        self.assertNotIn(self.args_sentinel, msg)

    def test_authentication_precedes_pickle(self):
        # A probe payload with a bad signature must never reach pickle.
        s = self.get_serializer()
        forged = pickle.dumps(Probe('forged')) + b':deadbeef'
        self.assertRaises(ValueError, s.deserialize, forged)
        self.assertEqual(PROBE_LOG, [])

        # Same probe, correctly signed: authenticates and deserializes.
        self.assertEqual(s.deserialize(s.serialize(Probe('real'))), 'real')
        self.assertEqual(PROBE_LOG, ['real'])

    def test_signed_compressed_budget(self):
        s = self.get_serializer(compression=True, max_message_bytes=2 ** 20,
                                max_decompressed_bytes=1024 * 1024)
        # A validly-signed bomb is stopped by the budget.
        bomb = s.serialize(b'\x00' * (8 * 1024 * 1024))
        self.assertRaises(MessageTooLarge, s.deserialize, bomb)

        # An unsigned bomb is stopped as well, and never reaches pickle.
        raw_bomb = gzip.compress(
            pickle.dumps(Probe('bomb')) + b'\x00' * (8 * 1024 * 1024), 9)
        try:
            s.deserialize(raw_bomb)
        except (MessageTooLarge, ValueError):
            pass
        else:
            self.fail('expected MessageTooLarge or ValueError')
        self.assertEqual(PROBE_LOG, [])

    def test_signed_budget_errors_sanitized(self):
        s = self.get_serializer(compression=True, max_message_bytes=16)
        data = s.serialize((self.args_sentinel,))
        try:
            s.deserialize(data)
        except MessageTooLarge as exc:
            msg = str(exc)
        else:
            self.fail('MessageTooLarge not raised')
        self.assertNotIn(self.secret, msg)
        self.assertNotIn(self.args_sentinel, msg)


class TestBoundedDecodeAPI(ProbeTestCase):
    def get_huey(self, **kwargs):
        kwargs.setdefault('compression', True)
        kwargs.setdefault('max_message_bytes', 4096)
        kwargs.setdefault('max_decompressed_bytes', 65536)
        return MemoryHuey(utc=False, **kwargs)

    def make_bomb(self, size=1024 * 1024):
        return gzip.compress(pickle.dumps(b'\x00' * size), 9)

    def test_huey_constructor_sets_budget(self):
        huey = MemoryHuey(max_message_bytes=100, max_decompressed_bytes=200)
        self.assertEqual(huey.serializer.max_message_bytes, 100)
        self.assertEqual(huey.serializer.max_decompressed_bytes, 200)
        self.assertTrue(huey.serializer.bounded)

    def test_dequeue_mixed_messages(self):
        executed = []

        @self.huey.task()
        def record(n):
            executed.append(n)
            return n

        # FIFO order: 1, 2, 3, bomb, 4.
        record(1)
        record(2)
        record(3)
        self.huey.storage.enqueue(self.make_bomb())
        record(4)

        for _ in range(3):
            self.huey.execute(self.huey.dequeue())

        # The anomalous message raises a sanitized error and is discarded.
        self.assertRaises(MessageTooLarge, self.huey.dequeue)

        # Subsequent legitimate messages are unaffected.
        self.huey.execute(self.huey.dequeue())
        self.assertEqual(executed, [1, 2, 3, 4])
        self.assertIsNone(self.huey.dequeue())

    def test_worker_survives_bad_message(self):
        executed = []

        @self.huey.task()
        def record(n):
            executed.append(n)
            return n

        record(1)
        self.huey.storage.enqueue(self.make_bomb())
        record(2)

        stop_flag = threading.Event()
        worker = Worker(self.huey, stop_flag, 0.001, 0.001, 1.15)
        worker.loop()  # Executes task 1.
        worker.loop()  # Hits the bomb: logs, sleeps, keeps going.
        worker.loop()  # Executes task 2.
        self.assertEqual(executed, [1, 2])
        self.assertEqual(len(self.huey), 0)

    def test_read_schedule_batch_isolation(self):
        @self.huey.task()
        def task_a(n):
            return n

        ts = datetime.datetime(2020, 1, 1)
        self.huey.add_schedule(task_a.s(1))
        self.huey.storage.add_to_schedule(self.make_bomb(), ts)
        self.huey.add_schedule(task_a.s(2))

        tasks = self.huey.read_schedule(timestamp=datetime.datetime(2020, 1, 2))
        # The bad message is logged and skipped; both legitimate tasks in
        # the same batch are returned.
        self.assertEqual(sorted(t.args[0] for t in tasks), [1, 2])

    def test_pending_isolation(self):
        @self.huey.task()
        def task_a(n):
            return n

        task_a(1)
        self.huey.storage.enqueue(self.make_bomb())
        task_a(2)
        pending = self.huey.pending()
        self.assertEqual(sorted(t.args[0] for t in pending), [1, 2])

    def test_result_budget(self):
        @self.huey.task()
        def task_a(n):
            return n

        # Legitimate result reads fine.
        res = task_a(3)
        self.execute_next()
        self.assertEqual(res.get(), 3)

        # An oversized value placed in the result store is rejected when read.
        self.huey.storage.put_data('big-result', self.make_bomb(),
                                   is_result=True)
        self.assertRaises(MessageTooLarge, self.huey.result, 'big-result')

        # A truncated stream in the result store is likewise rejected.
        bomb = self.make_bomb()
        self.huey.storage.put_data('truncated', bomb[:len(bomb) // 2],
                                   is_result=True)
        self.assertRaises(MessageDecodeError, self.huey.result, 'truncated')

    def test_result_group_isolation(self):
        @self.huey.task()
        def task_a(n):
            return n

        r1 = task_a(1)
        r2 = task_a(2)
        r3 = task_a(3)
        for _ in range(3):
            self.execute_next()

        # Corrupt one of the three results in the store.
        self.huey.storage.put_data(r2.id, self.make_bomb(), is_result=True)

        group = ResultGroup([Result(self.huey, r.task) for r in (r1, r2, r3)])
        self.assertEqual(group._results[0].get(), 1)
        self.assertRaises(MessageTooLarge, group._results[1].get)
        self.assertEqual(group._results[2].get(), 3)

    def test_pipeline_with_budget(self):
        @self.huey.task()
        def add(a, b):
            return a + b

        @self.huey.task()
        def double(n):
            return n * 2

        pipe = add.s(1, 2).then(double)
        res = self.huey.enqueue(pipe)  # ResultGroup over both pipeline tasks.

        self.huey.execute(self.huey.dequeue())  # Runs add, enqueues double.
        self.huey.execute(self.huey.dequeue())  # Runs double.
        self.assertEqual(res.get(), [3, 6])

    def test_result_read_back_bounded(self):
        # Results written by a task are decoded under budget when read back.
        @self.huey.task()
        def produce():
            return 'x' * 100  # Serialized result well within budget.

        res = produce()
        self.execute_next()
        self.assertEqual(res.get(preserve=True), 'x' * 100)

        # Tighten the budget and the same read is rejected.
        self.huey.serializer.max_decompressed_bytes = 8
        res.reset()
        self.assertRaises(MessageTooLarge, res.get)

    def test_signed_serializer_end_to_end(self):
        huey = MemoryHuey(
            utc=False,
            serializer=SignedSerializer(secret='e2e-secret', compression=True,
                                        max_message_bytes=4096,
                                        max_decompressed_bytes=65536))
        executed = []

        @huey.task()
        def record(n):
            executed.append(n)
            return n

        record(1)
        # Forged message (valid gzip + pickle, no valid signature).
        huey.storage.enqueue(gzip.compress(pickle.dumps(Probe('forged'))))
        record(2)

        huey.execute(huey.dequeue())
        self.assertRaises(ValueError, huey.dequeue)
        huey.execute(huey.dequeue())

        self.assertEqual(executed, [1, 2])
        self.assertEqual(PROBE_LOG, [])


class TestBoundedDecodeConsumer(ProbeTestCase):
    def get_huey(self):
        return MemoryHuey(utc=False, compression=True, max_message_bytes=4096,
                          max_decompressed_bytes=65536)

    def test_consumer_processes_good_around_bad(self):
        executed = []

        @self.huey.task()
        def record(n):
            executed.append(n)
            return n

        record(1)
        self.huey.storage.enqueue(
            gzip.compress(pickle.dumps(b'\x00' * 1024 * 1024), 9))
        record(2)

        with self.consumer_context(workers=1):
            deadline = time.time() + 5
            while time.time() < deadline and len(executed) < 2:
                time.sleep(0.01)

        self.assertEqual(sorted(executed), [1, 2])
        self.assertEqual(len(self.huey), 0)
