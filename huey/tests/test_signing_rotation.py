import gzip
import hashlib
import hmac
import json
import logging
import os
import pickle
import subprocess
import sys
import tempfile
import unittest
import zlib

from huey import SqliteHuey
from huey.exceptions import ConfigurationError
from huey.exceptions import MessageAuthenticationError
from huey.exceptions import MessageFormatError
from huey.exceptions import MessageRejected
from huey.serializer import constant_time_compare
from huey.serializer import RotatingSignedSerializer
from huey.serializer import SignedSerializer
from huey.serializer import SIGNED_MESSAGE_MAGIC
from huey.serializer import SIGNED_MESSAGE_VERSION
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase
from huey.tests import rotation_fixture


PUBLISHER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'rotation_publisher.py')
CONSUMER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'rotation_consumer.py')
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def legacy_bytes(message, secret, salt='huey', compression=None):
    """Reproduce the pre-rotation wire format exactly."""
    key = hashlib.sha1(salt.encode('utf8') + secret.encode('utf8')).digest()
    data = pickle.dumps(message)
    signature = hmac.new(key, msg=data,
                         digestmod=hashlib.sha1).hexdigest().encode('utf8')
    data = data + b':' + signature
    if compression == 'gzip':
        data = gzip.compress(data)
    elif compression == 'zlib':
        data = zlib.compress(data)
    return data


def envelope_parts(data):
    key_id_len = data[len(SIGNED_MESSAGE_MAGIC) + 2]
    header_len = len(SIGNED_MESSAGE_MAGIC) + 3 + key_id_len
    return {
        'version': data[len(SIGNED_MESSAGE_MAGIC)],
        'flags': data[len(SIGNED_MESSAGE_MAGIC) + 1],
        'key_id': data[len(SIGNED_MESSAGE_MAGIC) + 3:header_len],
        'signature': data[header_len:header_len + 32],
        'payload': data[header_len + 32:],
    }


# ``pickle.loads`` of this payload invokes ``len``. Used to prove that no
# pickle runs before authentication. The counter lives in a list so a
# ``__reduce__`` callback can mutate it.
triggers = [0]


def _bump_length(value):
    triggers[0] += 1
    return len(value)


class _Exploit(object):
    def __reduce__(self):
        return (_bump_length, (b'pwned',))


def malicious_pickle():
    return pickle.dumps(_Exploit())


class SerializerTestCase(BaseTestCase):
    data = [None, 0, 1, b'a' * 1024, ['k1', 'k2'], {'k': 'v'}]

    def assert_rejected(self, serializer, data, reason=None,
                        cls=MessageRejected):
        with self.assertRaises(cls) as ctx:
            serializer.deserialize(data)
        if reason is not None:
            self.assertEqual(ctx.exception.reason, reason)
        return ctx.exception

    def assert_roundtrip(self, serializer):
        for item in self.data:
            result = serializer.deserialize(serializer.serialize(item))
            self.assertEqual(result, item)


class TestSingleSecretCompatibility(SerializerTestCase):
    """The original one-secret usage must keep working unchanged."""

    def test_single_secret_roundtrip(self):
        self.assert_roundtrip(SignedSerializer(secret='sekret'))

    def test_single_secret_compression_modes(self):
        self.assert_roundtrip(SignedSerializer(secret='s', compression=True))
        self.assert_roundtrip(SignedSerializer(secret='s', compression=True,
                                               use_zlib=True))

    def test_constructor_compatibility(self):
        s = SignedSerializer(secret='s', salt='pepper', compression=True,
                             use_zlib=True, compression_level=1,
                             pickle_protocol=4)
        self.assertEqual(s.salt, b'pepper')
        self.assertEqual(s.secret, b's')
        self.assertEqual(s.separator, b':')
        self.assertTrue(s.comp)
        self.assertTrue(s.use_zlib)
        self.assertEqual(s.signing_key_id, b'default')
        self.assertEqual(s.key_ids, (b'default',))
        self.assertFalse(s.legacy_enabled)

    def test_secret_salt_are_required(self):
        for kwargs in ({}, {'secret': ''}, {'secret': b''},
                       {'secret': None}, {'secret': 's', 'salt': ''},
                       {'secret': 's', 'salt': None}):
            with self.assertRaises(ConfigurationError):
                SignedSerializer(**kwargs)

    def test_new_messages_carry_envelope(self):
        data = SignedSerializer(secret='s').serialize({'a': 1})
        self.assertTrue(data.startswith(SIGNED_MESSAGE_MAGIC))
        parts = envelope_parts(data)
        self.assertEqual(parts['version'], SIGNED_MESSAGE_VERSION)
        self.assertEqual(parts['key_id'], b'default')
        self.assertEqual(parts['flags'], 0)

    def test_existing_test_api_contract(self):
        # The documentation promises ValueError on tampering; MessageRejected
        # keeps that contract while remaining a HueyException.
        from huey.exceptions import HueyException
        self.assertTrue(issubclass(MessageRejected, ValueError))
        self.assertTrue(issubclass(MessageRejected, HueyException))
        s = SignedSerializer(secret='test secret')
        with self.assertRaises(ValueError):
            s.deserialize(s.serialize(1) + b'x')

    def test_secret_not_exposed_in_messages(self):
        s = SignedSerializer(secret='top-secret-value')
        data = s.serialize('x')
        self.assertNotIn(b'top-secret-value', data)
        self.assertNotIn(b'top-secret-value', s._derived_keys[b'default'])
        # Multi-key mode retains no raw secrets on the instance at all.
        multi = RotatingSignedSerializer(keys={'k': 'top-secret-value'},
                                         signing_key='k')
        self.assertIsNone(multi.secret)
        for value in vars(multi).values():
            self.assertNotIn(b'top-secret-value', repr(value).encode('utf8'))


class TestEnvelopeAuthentication(SerializerTestCase):
    """Version, key id, flags and payload are all authenticated."""

    def setUp(self):
        super(TestEnvelopeAuthentication, self).setUp()
        self.serializer = SignedSerializer(secret='s', compression=True)
        self.message = self.serializer.serialize({'a': [1, 2, 3]})

    def _with(self, **replace):
        parts = envelope_parts(self.message)
        version = replace.get('version', parts['version'])
        flags = replace.get('flags', parts['flags'])
        key_id = replace.get('key_id', parts['key_id'])
        header = (SIGNED_MESSAGE_MAGIC + bytes([version, flags, len(key_id)]) +
                  key_id)
        payload = replace.get('payload', parts['payload'])
        signature = replace.get('signature',
                                self.message[len(header):len(header) + 32])
        return header + signature + payload

    def test_valid_message_roundtrip(self):
        self.assertEqual(self.serializer.deserialize(self.message),
                         {'a': [1, 2, 3]})

    def test_tampered_key_id(self):
        self.assert_rejected(
            self.serializer, self._with(key_id=b'other'),
            'unknown_key_id', MessageAuthenticationError)

    def test_tampered_key_id_to_known_key(self):
        writer = RotatingSignedSerializer(keys={'a': 'one', 'b': 'two'},
                                          signing_key='a')
        other = RotatingSignedSerializer(keys={'a': 'one', 'b': 'two'},
                                         signing_key='b')
        message = writer.serialize({'x': 1})
        # Swap the key id for a key the reader also knows. The signature no
        # longer matches, so this must not decode via the other key.
        forged = message.replace(b'\x01\x00\x01a', b'\x01\x00\x01b', 1)
        self.assertNotEqual(forged, message)
        self.assert_rejected(other, forged, 'signature_mismatch',
                             MessageAuthenticationError)

    def test_tampered_compression_flags(self):
        parts = envelope_parts(self.message)
        # Flip the zlib bit on/off and the compressed bit off.
        for flags in (parts['flags'] ^ 0x02, parts['flags'] ^ 0x01,
                      parts['flags'] | 0x80):
            self.assert_rejected(self.serializer, self._with(flags=flags),
                                 reason=None)

    def test_tampered_payload(self):
        parts = envelope_parts(self.message)
        payload = bytearray(parts['payload'])
        payload[0] ^= 0xff
        self.assert_rejected(self.serializer,
                             self._with(payload=bytes(payload)),
                             'signature_mismatch',
                             MessageAuthenticationError)

    def test_tampered_version(self):
        self.assert_rejected(self.serializer, self._with(version=99),
                             'unsupported_version', MessageFormatError)

    def test_truncated_and_truncated_header(self):
        for size in (0, 1, 5, 8, 9, 10, 40):
            self.assert_rejected(self.serializer, self.message[:size],
                                 cls=MessageRejected)

    def test_non_bytes_input_rejected(self):
        # An int would otherwise be accepted by ``bytes(int)`` as a zero-filled
        # buffer of that length.
        for value in (None, 123, object()):
            self.assert_rejected(self.serializer, value,
                                 'malformed_message', MessageFormatError)

    def test_bytes_like_input_accepted(self):
        for value in (memoryview(self.message), bytearray(self.message)):
            self.assertEqual(self.serializer.deserialize(value),
                             {'a': [1, 2, 3]})

    def test_signature_is_not_prefix_trusted(self):
        parts = envelope_parts(self.message)
        forged = self._with(signature=b'\x00' * 32)
        self.assert_rejected(self.serializer, forged, 'signature_mismatch')

    def test_key_id_length_mismatch(self):
        # Claim a longer key id than the bytes present: the frame is truncated
        # and rejected before any key lookup.
        parts = envelope_parts(self.message)
        forged = (SIGNED_MESSAGE_MAGIC +
                  bytes([1, parts['flags'], 200]) + parts['key_id'] +
                  parts['signature'] + parts['payload'])
        self.assert_rejected(self.serializer, forged, 'malformed_message',
                             MessageFormatError)

    def test_key_id_length_shorter_than_key(self):
        # Claim a shorter key id: the remaining id bytes become signature
        # bytes, so authentication fails rather than selecting a prefix key.
        writer = RotatingSignedSerializer(keys={'ab': 'x'}, signing_key='ab')
        data = writer.serialize(1)
        forged = (SIGNED_MESSAGE_MAGIC + bytes([1, 0, 1]) + b'a' + data[10:])
        self.assert_rejected(writer, forged, cls=MessageRejected)


class TestCompressionContract(SerializerTestCase):
    """gzip/zlib mode is authenticated per message, not inferred."""

    def test_compression_modes_roundtrip_independently(self):
        for use_zlib in (False, True):
            s = SignedSerializer(secret='s', compression=True,
                                 use_zlib=use_zlib)
            self.assert_roundtrip(s)
            parts = envelope_parts(s.serialize('x'))
            self.assertEqual(parts['flags'] & 0x01, 1)
            self.assertEqual(bool(parts['flags'] & 0x02), use_zlib)

    def test_reader_without_compression_still_reads_compressed(self):
        writer = SignedSerializer(secret='s', compression=True)
        reader = SignedSerializer(secret='s')
        self.assertEqual(reader.deserialize(writer.serialize([1, 2])), [1, 2])

    def test_reader_with_wrong_compressor_reads_via_flags(self):
        writer = SignedSerializer(secret='s', compression=True)
        wrong = SignedSerializer(secret='s', compression=True, use_zlib=True)
        # Decoding follows the authenticated flag, so the "wrong" reader still
        # succeeds rather than guessing.
        self.assertEqual(wrong.deserialize(writer.serialize([1])), [1])

    def test_flags_are_not_inferred_from_payload(self):
        # A payload that looks like gzip must not be decompressed just because
        # its first byte resembles a gzip header; decoding follows the
        # authenticated flags only.
        s = SignedSerializer(secret='s')
        payload = gzip.compress(pickle.dumps('not really a task'))
        header = SIGNED_MESSAGE_MAGIC + bytes([1, 0, 7]) + b'default'
        signature = hmac.new(b'irrelevant', header + payload,
                             hashlib.sha256).digest()
        # Correctly framed, but with a bogus signature and no compression bit.
        forged = header + signature + payload
        self.assert_rejected(s, forged, 'signature_mismatch',
                             MessageAuthenticationError)

    def test_compression_bit_cannot_be_stripped(self):
        # A signed compressed message with its compression bit cleared fails
        # authentication (the flag is inside the signed header).
        s = SignedSerializer(secret='s', compression=True)
        data = s.serialize('payload')
        forged = data[:7] + bytes([data[7] & ~0x01]) + data[8:]
        self.assert_rejected(s, forged, 'signature_mismatch',
                             MessageAuthenticationError)


class TestUnknownAndRetiredKeys(SerializerTestCase):
    def test_reader_lacking_write_key_rejects(self):
        writer = RotatingSignedSerializer(keys={'v2': 'two'}, signing_key='v2')
        reader = RotatingSignedSerializer(keys={'v1': 'one'}, signing_key='v1')
        self.assert_rejected(reader, writer.serialize(1), 'unknown_key_id',
                             MessageAuthenticationError)

    def test_removed_key_is_rejected(self):
        writer = RotatingSignedSerializer(keys={'v1': 'one'}, signing_key='v1')
        message = writer.serialize({'job': 1})
        with_old = RotatingSignedSerializer(
            keys={'v2': 'two', 'v1': 'one'}, signing_key='v2')
        self.assertEqual(with_old.deserialize(message), {'job': 1})

        # Retire v1: backlog signed under it is now unreadable.
        retired = RotatingSignedSerializer(keys={'v2': 'two'},
                                           signing_key='v2')
        self.assert_rejected(retired, message, 'unknown_key_id',
                             MessageAuthenticationError)

    def test_retired_key_result_message_rejected(self):
        writer = RotatingSignedSerializer(keys={'v1': 'one'}, signing_key='v1')
        result = writer.serialize(42)
        retired = RotatingSignedSerializer(keys={'v2': 'two'},
                                           signing_key='v2')
        self.assert_rejected(retired, result, 'unknown_key_id')

    def test_nested_successor_and_chord_use_same_contract(self):
        # A task's on_complete successor and chord callback are embedded in the
        # outer message and covered by the single outer signature. They have no
        # independent signing or decoding path, so a retired or unknown key
        # rejects the whole message.
        from huey.api import MemoryHuey

        writer = RotatingSignedSerializer(keys={'v1': 'one'}, signing_key='v1')
        huey = MemoryHuey()
        add, record, noop = rotation_fixture.register(huey)
        task = add.s(1, 2).then(record, 3)
        message = huey._registry.create_message(task)
        data = writer.serialize(message)

        # The successor lives inside the signed envelope, not as a separate
        # signed blob: only the outer message is framed, and the whole payload
        # fails authentication if any embedded successor bytes change.
        parts = envelope_parts(data)
        self.assertNotEqual(parts['payload'][:len(SIGNED_MESSAGE_MAGIC)],
                            SIGNED_MESSAGE_MAGIC)
        successor_pos = data.find(b'huey.tests.rotation_fixture.record')
        payload_start = len(data) - len(parts['payload'])
        self.assertGreater(successor_pos, payload_start)
        # Flip a byte inside the embedded successor: the outer signature
        # covers it, so the whole message is rejected.
        corrupted = (data[:successor_pos] +
                     bytes([data[successor_pos] ^ 1]) +
                     data[successor_pos + 1:])
        self.assert_rejected(writer, corrupted, 'signature_mismatch',
                             MessageAuthenticationError)

        decoded = huey._registry.create_task(writer.deserialize(data))
        self.assertEqual(decoded.on_complete.args, (3,))

        # Chord callbacks use the same nesting.
        chord_task = add.s(1, 1)
        from huey.utils import ChordConfig
        chord_task.chord_config = ChordConfig(
            'cid', 1, 0, huey._registry.create_task(
                huey._registry.create_message(record.s('cb'))))
        chord_message = huey._registry.create_message(chord_task)
        chord_data = writer.serialize(chord_message)
        restored = huey._registry.create_task(writer.deserialize(chord_data))
        self.assertEqual(restored.chord_config.callback.args, ('cb',))

        retired = RotatingSignedSerializer(keys={'v2': 'two'},
                                           signing_key='v2')
        self.assert_rejected(retired, data, 'unknown_key_id',
                             MessageAuthenticationError)
        self.assert_rejected(retired, chord_data, 'unknown_key_id',
                             MessageAuthenticationError)

    def test_results_use_envelope_contract(self):
        # Result messages go through the same serializer, so a retired key
        # rejects them identically.
        writer = RotatingSignedSerializer(keys={'v1': 'one'}, signing_key='v1')
        data = writer.serialize({'result': 123})
        self.assertTrue(data.startswith(SIGNED_MESSAGE_MAGIC))
        retired = RotatingSignedSerializer(keys={'v2': 'two'},
                                           signing_key='v2')
        self.assert_rejected(retired, data, 'unknown_key_id',
                             MessageAuthenticationError)


class TestLegacyPolicy(SerializerTestCase):
    """Legacy format acceptance is an explicit, caller-controlled switch."""

    def test_legacy_rejected_by_default(self):
        legacy = legacy_bytes({'a': 1}, 'old')
        self.assert_rejected(SignedSerializer(secret='old'), legacy,
                             'legacy_format_disabled',
                             MessageAuthenticationError)

    def test_legacy_accepted_when_enabled(self):
        legacy = legacy_bytes({'a': 1}, 'old')
        s = SignedSerializer(secret='old', allow_legacy=True)
        self.assertTrue(s.legacy_enabled)
        self.assertEqual(s.deserialize(legacy), {'a': 1})

    def test_legacy_compressed_accepted_when_enabled(self):
        for mode in ('gzip', 'zlib'):
            legacy = legacy_bytes([1, 2], 'old', compression=mode)
            s = SignedSerializer(secret='old', allow_legacy=True,
                                 compression=True,
                                 use_zlib=mode == 'zlib')
            self.assertEqual(s.deserialize(legacy), [1, 2])

    def test_legacy_wrong_secret_rejected(self):
        legacy = legacy_bytes({'a': 1}, 'old')
        s = SignedSerializer(secret='new', allow_legacy=True)
        self.assert_rejected(s, legacy, 'legacy_signature_mismatch',
                             MessageAuthenticationError)

    def test_magic_downgrade_is_rejected(self):
        # Stripping the envelope magic routes the bytes to the legacy reader.
        # Even with legacy enabled, the message has no valid legacy HMAC, so
        # it cannot be re-decoded through the unrestricted path.
        writer = SignedSerializer(secret='s', allow_legacy=True)
        data = writer.serialize({'x': 1})
        stripped = b'\x00' * len(SIGNED_MESSAGE_MAGIC) + \
            data[len(SIGNED_MESSAGE_MAGIC):]
        self.assert_rejected(writer, stripped, cls=MessageRejected)

    def test_no_fallback_after_envelope_auth_failure(self):
        # Build a valid envelope then corrupt the signature. Even with legacy
        # enabled and legacy_key_id set, the reader must not re-interpret the
        # bytes as a legacy message.
        writer = RotatingSignedSerializer(
            keys={'v1': 'one'}, signing_key='v1', allow_legacy=True,
            legacy_key_id='v1')
        data = writer.serialize({'x': 1})
        corrupted = data[:-1] + bytes([data[-1] ^ 0xff])
        self.assert_rejected(writer, corrupted, 'signature_mismatch',
                             MessageAuthenticationError)

    def test_legacy_signature_failure_does_not_deserialize(self):
        legacy = legacy_bytes({'a': 1}, 'old')
        # Corrupt the message but keep it a well-formed legacy shape.
        message, sig = legacy.rsplit(b':', 1)
        tampered = message[:-1] + bytes([message[-1] ^ 1]) + b':' + sig
        s = SignedSerializer(secret='old', allow_legacy=True)
        self.assert_rejected(s, tampered, 'legacy_signature_mismatch',
                             MessageAuthenticationError)

    def test_legacy_key_id_selection(self):
        legacy = legacy_bytes({'a': 1}, 'old')
        s = RotatingSignedSerializer(
            keys={'v1': 'old', 'v2': 'new'}, signing_key='v2',
            allow_legacy=True, legacy_key_id='v1')
        self.assertEqual(s.deserialize(legacy), {'a': 1})
        # Pointing legacy_key_id at the other key must fail.
        s2 = RotatingSignedSerializer(
            keys={'v1': 'old', 'v2': 'new'}, signing_key='v2',
            allow_legacy=True, legacy_key_id='v2')
        self.assert_rejected(s2, legacy, 'legacy_signature_mismatch')

    def test_allow_legacy_must_be_bool(self):
        with self.assertRaises(ConfigurationError):
            SignedSerializer(secret='s', allow_legacy='yes')


class TestConfigurationAtomicity(SerializerTestCase):
    def test_empty_keys(self):
        with self.assertRaises(ConfigurationError):
            RotatingSignedSerializer(keys={}, signing_key='a')
        with self.assertRaises(ConfigurationError):
            RotatingSignedSerializer(keys={'a': ''}, signing_key='a')
        with self.assertRaises(ConfigurationError):
            RotatingSignedSerializer(keys={'': 's'}, signing_key='')

    def test_duplicate_key_ids(self):
        # str and bytes ids are the same id; dict keys cannot collide unless
        # normalization maps them together.
        with self.assertRaises(ConfigurationError):
            RotatingSignedSerializer(keys={b'a': 'one', 'a': 'two'},
                                     signing_key='a')

    def test_signing_key_must_exist(self):
        with self.assertRaises(ConfigurationError):
            RotatingSignedSerializer(keys={'a': 'one'}, signing_key='b')

    def test_mixed_forms_rejected(self):
        with self.assertRaises(ConfigurationError):
            SignedSerializer(secret='s', keys={'a': 'one'}, signing_key='a')
        with self.assertRaises(ConfigurationError):
            SignedSerializer(secret='s', signing_key='a')
        with self.assertRaises(ConfigurationError):
            SignedSerializer(secret='s', legacy_key_id='a')

    def test_legacy_key_id_requires_allow_legacy(self):
        with self.assertRaises(ConfigurationError):
            RotatingSignedSerializer(keys={'a': 'one', 'b': 'two'},
                                     signing_key='a', legacy_key_id='b')

    def test_failed_config_is_atomic(self):
        # Construction validates everything before deriving or publishing
        # signing state, so a failure cannot produce an object that signs with
        # an unvalidated key, and callers never receive a partial instance.
        with self.assertRaises(ConfigurationError) as ctx:
            RotatingSignedSerializer(keys={'a': 'one'}, signing_key='missing')
        self.assertIn('signing_key', str(ctx.exception))

        # Key material is immutable after construction, so a "configuration
        # change" requires a new serializer. There is no in-place mutation that
        # could leave old and new keys half-applied.
        s = RotatingSignedSerializer(keys={'a': 'one'}, signing_key='a')
        with self.assertRaises(AttributeError):
            s.signing_key_id = b'b'

    def test_salt_is_bound_into_keys(self):
        a = SignedSerializer(secret='s', salt='huey')
        b = SignedSerializer(secret='s', salt='other')
        self.assert_rejected(b, a.serialize(1), 'signature_mismatch',
                             MessageAuthenticationError)

    def test_key_id_is_bound_into_derived_key(self):
        # Two ids with the same secret derive distinct keys, so swapping the
        # (authenticated) key id cannot select the other id's key material.
        writer = RotatingSignedSerializer(keys={'a': 'same', 'b': 'same'},
                                          signing_key='a')
        reader = RotatingSignedSerializer(keys={'a': 'same', 'b': 'same'},
                                          signing_key='b')
        forged = writer.serialize(1).replace(b'\x01\x00\x01a',
                                             b'\x01\x00\x01b', 1)
        self.assert_rejected(reader, forged, 'signature_mismatch',
                             MessageAuthenticationError)

    def test_rotation_requires_new_instance(self):
        # The write key cannot be switched in place; the old serializer keeps
        # its own consistent view.
        old = RotatingSignedSerializer(keys={'v1': 'one'}, signing_key='v1')
        new = RotatingSignedSerializer(
            keys={'v2': 'two', 'v1': 'one'}, signing_key='v2')
        old_message = old.serialize({'a': 1})
        new_message = new.serialize({'a': 2})
        self.assertEqual(envelope_parts(old_message)['key_id'], b'v1')
        self.assertEqual(envelope_parts(new_message)['key_id'], b'v2')
        # The new reader accepts both; the old reader accepts only its own.
        self.assertEqual(new.deserialize(old_message), {'a': 1})
        self.assertEqual(old.deserialize(old_message), {'a': 1})
        with self.assertRaises(MessageAuthenticationError):
            old.deserialize(new_message)

    def test_serializer_is_picklable(self):
        # Callers that pickle the serializer (or a config object holding it)
        # must not be broken by internal state, while the restored copy still
        # verifies and signs identically.
        serializers = [
            SignedSerializer(secret='s', compression=True),
            RotatingSignedSerializer(keys={'v1': 'one', 'v2': 'two'},
                                     signing_key='v2', allow_legacy=True,
                                     legacy_key_id='v1'),
        ]
        for original in serializers:
            restored = pickle.loads(pickle.dumps(original))
            message = original.serialize({'a': 1})
            self.assertEqual(restored.deserialize(message), {'a': 1})
            roundtrip = restored.serialize({'a': 1})
            self.assertEqual(original.deserialize(roundtrip), {'a': 1})

    def test_key_id_normalization_and_limits(self):
        with self.assertRaises(ConfigurationError):
            SignedSerializer(secret='s', key_id=123)
        with self.assertRaises(ConfigurationError):
            SignedSerializer(secret='s', key_id='k' * 256)
        # str and bytes ids collide only after normalization.
        with self.assertRaises(ConfigurationError):
            RotatingSignedSerializer(keys={'k': 'one', b'k': 'two'},
                                     signing_key='k')

    def test_properties_expose_no_secrets(self):
        secrets = {'a': 'secret-material-alpha',
                   'b': 'secret-material-bravo'}
        s = RotatingSignedSerializer(keys=secrets, signing_key='a')
        self.assertEqual(s.signing_key_id, b'a')
        self.assertEqual(s.key_ids, (b'a', b'b'))
        self.assertEqual(s.read_key_ids, (b'a', b'b'))
        # Raw secrets are not retained anywhere in multi-key mode; only
        # derived key material is, and it is not the secret itself.
        self.assertIsNone(s.secret)
        rendered = repr(s.__dict__).encode('utf8')
        for secret in secrets.values():
            self.assertNotIn(secret.encode('utf8'), rendered)
            self.assertNotIn(secret.encode('utf8'),
                             s.serialize('x'))


class TestLoggingPolicy(SerializerTestCase):
    def test_rejected_log_has_only_classification(self):
        logger = logging.getLogger('huey.serializer')
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.WARNING)
        try:
            writer = RotatingSignedSerializer(
                keys={'secret-key-id': 'super-secret-material'},
                signing_key='secret-key-id')
            data = writer.serialize({'business': 'parameter'})
            reader = RotatingSignedSerializer(keys={'other': 'x'},
                                              signing_key='other')
            with self.assertRaises(MessageRejected):
                reader.deserialize(data)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        self.assertTrue(records)
        joined = '\n'.join(records)
        for forbidden in ('super-secret-material', 'secret-key-id',
                          'business', 'parameter', data[:20].hex()):
            self.assertNotIn(forbidden, joined)
        self.assertIn('unknown_key_id', joined)

    def test_signature_mismatch_log_has_no_raw_data(self):
        s = SignedSerializer(secret='s')
        data = s.serialize({'private': 'value'})
        corrupted = data[:-1] + bytes([data[-1] ^ 1])
        logger = logging.getLogger('huey.serializer')
        with self.assertLogs(logger, level='WARNING') as ctx:
            with self.assertRaises(MessageRejected):
                s.deserialize(corrupted)
        output = '\n'.join(ctx.output)
        self.assertIn('signature_mismatch', output)
        self.assertNotIn('private', output)
        self.assertNotIn('value', output)


class TestNoPickleBeforeAuthentication(SerializerTestCase):
    """Pickle/migration callbacks must never run on unauthenticated bytes."""

    def test_callback_not_invoked_for_bad_signature(self):
        payload = malicious_pickle()
        s = SignedSerializer(secret='s')
        # Build a structurally valid envelope under a different key, then swap
        # in the malicious payload. Authentication must fail first.
        wrong = SignedSerializer(secret='other')
        message = wrong.serialize({'x': 1})
        key_id_len = message[8]
        header_len = 9 + key_id_len
        forged = (message[:header_len] + message[header_len:header_len + 32] +
                  payload)
        triggers[0] = 0
        with self.assertRaises(MessageRejected):
            s.deserialize(forged)
        self.assertEqual(triggers[0], 0)

    def test_callback_not_invoked_for_unknown_key(self):
        s = SignedSerializer(secret='s')
        data = (SIGNED_MESSAGE_MAGIC + bytes([1, 0, 4]) + b'nope' +
                b'\x00' * 32 + malicious_pickle())
        triggers[0] = 0
        with self.assertRaises(MessageRejected):
            s.deserialize(data)
        self.assertEqual(triggers[0], 0)

    def test_callback_not_invoked_for_bad_flags(self):
        payload = malicious_pickle()
        s = SignedSerializer(secret='s')
        valid = s.serialize({'x': 1})
        # Flip the compression flags; the signature no longer matches.
        forged = valid[:7] + bytes([valid[7] | 0x01]) + valid[8:]
        triggers[0] = 0
        with self.assertRaises(MessageRejected):
            s.deserialize(forged)
        self.assertEqual(triggers[0], 0)

    def test_signature_compared_in_constant_time(self):
        # hmac.compare_digest is used for both envelope and legacy signatures.
        import huey.serializer as mod
        self.assertTrue(callable(mod.constant_time_compare))
        self.assertTrue(constant_time_compare(b'a', b'a'))
        self.assertFalse(constant_time_compare(b'a', b'b'))


class TestHueyIntegration(SerializerTestCase):
    """Ordinary tasks, pipelines and results all use the same contract."""

    def setUp(self):
        super(TestHueyIntegration, self).setUp()
        self.huey.flush()
        self.add, self.record, self.noop = rotation_fixture.register(self.huey)

    def get_huey(self):
        return SqliteHuey('rotation-it',
                          filename=self.db_path(),
                          utc=False,
                          serializer=RotatingSignedSerializer(
                              keys={'v2': 'two', 'v1': 'one'},
                              signing_key='v2'))

    @classmethod
    def db_path(cls):
        return os.path.join(TEST_TMP_DIR,
                            'rotation-it-%s.db' % id(cls))

    def test_task_pipeline_and_result(self):
        task = self.add.s(1, 2).then(self.record, 'done')
        self.huey.storage.enqueue(self.huey.serialize_task(task))
        first = self.huey.dequeue()
        self.assertEqual(self.huey.execute(first), 3)
        second = self.huey.dequeue()
        # The successor receives its configured args plus the parent result.
        self.assertEqual(second.args, ('done', 3))
        self.huey.execute(second)
        self.assertEqual(self.huey.result(first.id), 3)

    def test_stored_results_are_signed(self):
        self.huey.put_result('r1', {'value': 7})
        raw = self.huey.storage.peek_data('r1')
        self.assertTrue(bytes(raw).startswith(SIGNED_MESSAGE_MAGIC))
        self.assertEqual(self.huey.result('r1'), {'value': 7})

    def test_retired_key_blocks_backlog_task_and_result(self):
        db = os.path.join(TEST_TMP_DIR, 'rotation-retired-%s.db' % id(self))
        old = SqliteHuey('rotation-retired', filename=db, utc=False,
                         serializer=RotatingSignedSerializer(
                             keys={'v1': 'one'}, signing_key='v1'))
        old.flush()
        add, record, noop = rotation_fixture.register(old)
        old.storage.enqueue(old.serialize_task(add.s(5, 6)))
        old.put_result('old-result', 11)

        # Reader has retired v1 entirely.
        new = SqliteHuey('rotation-retired', filename=db, utc=False,
                         serializer=RotatingSignedSerializer(
                             keys={'v2': 'two'}, signing_key='v2'))
        rotation_fixture.register(new)
        with self.assertRaises(MessageRejected):
            new.dequeue()
        with self.assertRaises(MessageRejected):
            new.result('old-result')

    def test_tampered_queue_message_rejected(self):
        task = self.add.s(1, 2)
        data = self.huey.serialize_task(task)
        corrupted = data[:-1] + bytes([data[-1] ^ 1])
        self.huey.storage.enqueue(corrupted)
        with self.assertRaises(MessageRejected):
            self.huey.dequeue()

    def test_inspection_skips_rejected_without_leaking(self):
        # pending()/scheduled() are non-destructive inspection helpers. A
        # rejected message is skipped and logged by classification only; the
        # log carries no traceback, raw message, or task arguments.
        db = os.path.join(TEST_TMP_DIR, 'rotation-inspect-%s.db' % id(self))
        writer = SqliteHuey('rotation-inspect', filename=db, utc=False,
                            serializer=RotatingSignedSerializer(
                                keys={'v1': 'one'}, signing_key='v1'))
        add, record, noop = rotation_fixture.register(writer)
        writer.storage.enqueue(
            writer.serialize_task(add.s('sensitive-arg-xyz', 2)))

        reader = SqliteHuey('rotation-inspect', filename=db, utc=False,
                            serializer=RotatingSignedSerializer(
                                keys={'v2': 'two'}, signing_key='v2'))
        rotation_fixture.register(reader)
        logger = logging.getLogger('huey')
        with self.assertLogs(logger, level='WARNING') as ctx:
            self.assertEqual(reader.pending(), [])
        output = '\n'.join(ctx.output)
        self.assertIn('unknown_key_id', output)
        self.assertNotIn('sensitive-arg-xyz', output)
        self.assertNotIn('Traceback', output)
        self.assertEqual(reader.pending_count(), 1)

    def test_rejected_message_does_not_wedge_the_queue(self):
        # dequeue() is destructive in Huey's storages, so a rejected message is
        # consumed rather than retried forever. The consumer logs the failure
        # and moves on; the next valid message is processed normally.
        self.huey.storage.enqueue(b'not a signed message')
        with self.assertRaises(MessageRejected):
            self.huey.dequeue()
        self.huey.storage.enqueue(self.huey.serialize_task(self.add.s(2, 2)))
        task = self.huey.dequeue()
        self.assertEqual(self.huey.execute(task), 4)


class RotationPublisherMixin(object):
    """Helpers for driving the independent publisher subprocess."""

    def setUp(self):
        super(RotationPublisherMixin, self).setUp()
        self.db = os.path.join(
            TEST_TMP_DIR, 'rotation-pub-%s.db' % id(self))

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            path = self.db + suffix
            if os.path.exists(path):
                os.unlink(path)
        super(RotationPublisherMixin, self).tearDown()

    def publish(self, expect_ok=True, **config):
        config.setdefault('db', self.db)
        config.setdefault('salt', 'huey')
        proc = subprocess.run(
            [sys.executable, PUBLISHER, json.dumps(config)],
            cwd=REPO_ROOT, capture_output=True, text=True)
        if expect_ok:
            if proc.returncode != 0:
                raise AssertionError(
                    'publisher failed: %s\n%s' % (proc.stderr, proc.stdout))
            return json.loads(proc.stdout)
        return proc

    def consumer(self, serializer):
        huey = SqliteHuey('huey', filename=self.db, utc=False,
                          serializer=serializer)
        rotation_fixture.register(huey)
        return huey

    def run_consumer(self, expect_ok=True, **config):
        config.setdefault('db', self.db)
        proc = subprocess.run(
            [sys.executable, CONSUMER, json.dumps(config)],
            cwd=REPO_ROOT, capture_output=True, text=True)
        if expect_ok:
            if proc.returncode != 0:
                raise AssertionError(
                    'consumer failed: %s\n%s' % (proc.stderr, proc.stdout))
            return json.loads(proc.stdout)
        return proc


class TestRotationBacklogSqlite(RotationPublisherMixin, BaseTestCase):
    """Backlog drain using independent publisher, consumer and publisher."""

    def test_backlog_old_publisher_read_by_rotating_consumer(self):
        # Stage 1: old publisher signs with the legacy single-secret format.
        self.publish(mode='legacy', secret='old-secret', task='add',
                     args=[2, 3], result_for='r-old', result=99)

        # Stage 2: new consumer accepts legacy only because it is configured.
        rotation = RotatingSignedSerializer(
            keys={'v2': 'new-secret', 'v1': 'old-secret'},
            signing_key='v2', allow_legacy=True, legacy_key_id='v1')
        huey = self.consumer(rotation)
        self.assertEqual(huey.pending_count(), 1)
        self.assertEqual(huey.result('r-old'), 99)
        task = huey.dequeue()
        self.assertEqual(huey.execute(task), 5)
        self.assertEqual(huey.pending_count(), 0)

    def test_backlog_old_format_rejected_without_opt_in(self):
        self.publish(mode='legacy', secret='old-secret', task='add',
                     args=[1, 1])
        huey = self.consumer(RotatingSignedSerializer(
            keys={'v2': 'new-secret', 'v1': 'old-secret'},
            signing_key='v2'))
        with self.assertRaises(MessageAuthenticationError):
            huey.dequeue()

    def test_envelope_backlog_survives_write_key_rotation(self):
        # Both publishers use the authenticated envelope, first with v1 as the
        # write key, then with v2. A consumer holding both drains everything.
        self.publish(mode='envelope', keys={'v1': 'one'}, signing_key='v1',
                     task='add', args=[1, 2], count=2)
        self.publish(mode='envelope', keys={'v2': 'two', 'v1': 'one'},
                     signing_key='v2', task='add', args=[10, 20], count=1)
        huey = self.consumer(RotatingSignedSerializer(
            keys={'v2': 'two', 'v1': 'one'}, signing_key='v2'))
        self.assertEqual(huey.pending_count(), 3)
        values = []
        while True:
            task = huey.dequeue()
            if task is None:
                break
            values.append(huey.execute(task))
        self.assertEqual(values, [3, 3, 30])

    def test_retired_key_backlog_rejected_after_removal(self):
        self.publish(mode='envelope', keys={'v1': 'one'}, signing_key='v1',
                     task='record', args=['stale'])
        # Key v1 has been retired from the reader's configuration.
        huey = self.consumer(RotatingSignedSerializer(
            keys={'v2': 'two'}, signing_key='v2'))
        # Preserve the exact bytes: Huey's dequeue is destructive, so a
        # rejected message is consumed just like any other unreadable one.
        raw = huey.storage.dequeue()
        with self.assertRaises(MessageAuthenticationError):
            huey.serializer.deserialize(raw)

        # Rollback boundary: the same bytes are readable again once v1 is
        # restored as a read key.
        rollback = RotatingSignedSerializer(keys={'v2': 'two', 'v1': 'one'},
                                            signing_key='v1')
        message = rollback.deserialize(raw)
        huey2 = self.consumer(rollback)
        task = huey2._registry.create_task(message)
        self.assertEqual(task.args, ('stale',))

    def test_rollback_after_rotation(self):
        # A publisher under v2 enqueues; rolling the consumer back to a config
        # that only knows v1 rejects the new message (read keys are explicit).
        self.publish(mode='envelope', keys={'v2': 'two'}, signing_key='v2',
                     task='noop', args=[])
        rollback = self.consumer(RotatingSignedSerializer(
            keys={'v1': 'one'}, signing_key='v1'))
        with self.assertRaises(MessageAuthenticationError):
            rollback.dequeue()


class TestRotationProcessBoundaries(RotationPublisherMixin, BaseTestCase):
    """All three roles run as separate OS processes against one SQLite file."""

    def test_independent_consumer_drains_mixed_backlog(self):
        # Old publisher (legacy wire format), a v1 envelope publisher, and a
        # v2 envelope publisher all contribute to the same queue.
        self.publish(mode='legacy', secret='old-secret', task='add',
                     args=[1, 1])
        self.publish(mode='envelope', keys={'v1': 'one'}, signing_key='v1',
                     task='add', args=[2, 2])
        self.publish(mode='envelope', keys={'v2': 'two', 'v1': 'one'},
                     signing_key='v2', task='add', args=[3, 3])

        result = self.run_consumer(
            keys={'v2': 'two', 'v1': 'one', 'v0': 'old-secret'},
            signing_key='v2', allow_legacy=True, legacy_key_id='v0',
            strict=True)
        self.assertEqual(sorted(result['executed']), [2, 4, 6])
        self.assertEqual(result['rejected'], [])

    def test_independent_consumer_rejects_retired_and_unknown(self):
        self.publish(mode='envelope', keys={'v1': 'one'}, signing_key='v1',
                     task='noop', args=[])
        self.publish(mode='envelope', keys={'vX': 'unknown'},
                     signing_key='vX', task='noop', args=[])
        self.publish(mode='envelope', keys={'v2': 'two'}, signing_key='v2',
                     task='add', args=[4, 4])

        result = self.run_consumer(keys={'v2': 'two'}, signing_key='v2',
                                   strict=False)
        # Only the v2 message executes; the retired key and the unknown key id
        # are rejected by classification, and the consumer keeps going.
        self.assertEqual(result['executed'], [8])
        self.assertEqual(sorted(result['rejected']),
                         ['unknown_key_id', 'unknown_key_id'])

        # In strict mode a rejected message makes the consumer exit non-zero.
        self.publish(mode='envelope', keys={'v1': 'one'}, signing_key='v1',
                     task='noop', args=[])
        proc = self.run_consumer(keys={'v2': 'two'}, signing_key='v2',
                                 strict=True, expect_ok=False)
        self.assertEqual(proc.returncode, 2)

    def test_process_rollback_boundary(self):
        # After rotation, rolling the consumer back to a v1-only read set makes
        # newly-published v2 messages unreadable. Re-adding v2 recovers them
        # only if they are still in the queue.
        self.publish(mode='envelope', keys={'v2': 'two'}, signing_key='v2',
                     task='add', args=[5, 5])
        rollback = self.run_consumer(keys={'v1': 'one'}, signing_key='v1',
                                     strict=False)
        self.assertEqual(rollback['executed'], [])
        self.assertEqual(rollback['rejected'], ['unknown_key_id'])

        # dequeue() consumed the rejected message, so the queue is empty.
        forward = self.run_consumer(keys={'v2': 'two'}, signing_key='v2',
                                    strict=False)
        self.assertEqual(forward['executed'], [])


    def test_compression_is_authenticated_across_processes(self):
        # A compressed publisher and an uncompressed consumer agree because the
        # per-message flag, not the consumer's configuration, decides how the
        # payload is decoded.
        for mode in ('gzip', 'zlib'):
            db = os.path.join(TEST_TMP_DIR,
                              'rotation-compress-%s-%s.db' % (mode, id(self)))
            self.publish(mode='envelope', keys={'v1': 'one'},
                         signing_key='v1', compression=mode, task='add',
                         args=[7, 7], db=db)
            consumer = SqliteHuey('huey', filename=db, utc=False,
                                  serializer=RotatingSignedSerializer(
                                      keys={'v1': 'one'},
                                      signing_key='v1'))
            rotation_fixture.register(consumer)
            task = consumer.dequeue()
            self.assertEqual(consumer.execute(task), 14)

    def test_compression_flag_tamper_rejected_across_processes(self):
        self.publish(mode='envelope', keys={'v1': 'one'}, signing_key='v1',
                     compression='gzip', task='noop', args=[])
        store = SqliteHuey('huey', filename=self.db, utc=False,
                           serializer=RotatingSignedSerializer(
                               keys={'v1': 'one'}, signing_key='v1'))
        data = store.storage.dequeue()
        # Clear the compression bit and correct nothing else.
        forged = data[:7] + bytes([data[7] & ~0x01]) + data[8:]
        store.storage.enqueue(forged)
        with self.assertRaises(MessageAuthenticationError):
            store.dequeue()


class TestRotationRejectionSqlite(RotationPublisherMixin, BaseTestCase):
    """Tampering, unknown ids, and removed keys, via real SQLite bytes."""

    def _stored(self, config):
        self.publish(**config)
        huey = SqliteHuey('huey', filename=self.db, utc=False,
                          serializer=SignedSerializer(secret='unused'))
        return huey.storage.dequeue()

    def test_tampered_envelope_from_storage(self):
        raw = self._stored(dict(mode='envelope', keys={'v1': 'one'},
                                signing_key='v1', task='add', args=[1, 2]))
        corrupted = raw[:-1] + bytes([raw[-1] ^ 0xff])
        huey = self.consumer(RotatingSignedSerializer(
            keys={'v1': 'one'}, signing_key='v1'))
        huey.storage.enqueue(corrupted)
        with self.assertRaises(MessageAuthenticationError):
            huey.dequeue()

    def test_unknown_key_id_from_storage(self):
        raw = self._stored(dict(mode='envelope', keys={'v9': 'nine'},
                                signing_key='v9', task='noop', args=[]))
        huey = self.consumer(RotatingSignedSerializer(
            keys={'v1': 'one'}, signing_key='v1'))
        huey.storage.enqueue(raw)
        with self.assertRaises(MessageAuthenticationError):
            huey.dequeue()

    def test_publisher_emits_envelope_with_authenticated_key_id(self):
        raw = self._stored(dict(mode='envelope', keys={'v7': 'seven'},
                                signing_key='v7', task='noop', args=[]))
        parts = envelope_parts(raw)
        self.assertEqual(parts['key_id'], b'v7')
        self.assertEqual(parts['version'], SIGNED_MESSAGE_VERSION)


class TestExecutableRotationSteps(unittest.TestCase):
    """
    The documented rotation procedure, executed as written.

    Step 1  publish v1-only (old release), backlog accumulates.
    Step 2  deploy readers configured with {v1 read, v2 read}, write v1.
    Step 3  switch publishers to write v2; readers keep both.
    Step 4  drain the backlog; every message is readable.
    Step 5  retire v1 from every configuration; only new messages work.
    """

    def test_documented_rotation_sequence(self):
        class Fixture(RotationPublisherMixin, BaseTestCase):
            def runTest(self):
                pass

        mixin = Fixture()
        mixin.setUp()
        db = mixin.db

        def consumer(keys, signing_key):
            huey = SqliteHuey('huey', filename=db, utc=False,
                              serializer=RotatingSignedSerializer(
                                  keys=keys, signing_key=signing_key))
            rotation_fixture.register(huey)
            return huey

        # Step 1.
        mixin.publish(mode='envelope', keys={'v1': 'one'}, signing_key='v1',
                      task='add', args=[1, 1], count=2)

        # Step 2: reader can already read v1 and knows v2 for the switch.
        reader = consumer({'v2': 'two', 'v1': 'one'}, 'v1')
        self.assertEqual(reader.pending_count(), 2)

        # Step 3: publishers move to v2, adding one more message.
        mixin.publish(mode='envelope', keys={'v2': 'two', 'v1': 'one'},
                      signing_key='v2', task='add', args=[2, 2], count=1)

        # Step 4: drain everything under the mixed configuration.
        values = []
        while True:
            task = reader.dequeue()
            if task is None:
                break
            values.append(reader.execute(task))
        self.assertEqual(values, [2, 2, 4])

        # Step 5: retire v1. A v2 message still works; a hand-crafted v1
        # backlog message does not.
        mixin.publish(mode='envelope', keys={'v1': 'one'}, signing_key='v1',
                      task='add', args=[9, 9])
        retired = consumer({'v2': 'two'}, 'v2')
        with self.assertRaises(MessageAuthenticationError):
            retired.dequeue()

        mixin.publish(mode='envelope', keys={'v2': 'two'}, signing_key='v2',
                      task='add', args=[5, 5])
        task = retired.dequeue()
        self.assertEqual(retired.execute(task), 10)
