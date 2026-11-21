"""信封层单元测试：版本标注、往返一致性与与 Huey 默认格式的区分。"""

import pickle
import unittest

from version_bridge.envelope import ENVELOPE_HEADER_SIZE
from version_bridge.envelope import ENVELOPE_MAGIC
from version_bridge.envelope import VERSION_1
from version_bridge.envelope import VERSION_2
from version_bridge.envelope import MessageEnvelope
from version_bridge.envelope import envelope_version
from version_bridge.envelope import is_envelope
from version_bridge.envelope import unwrap_envelope
from version_bridge.envelope import wrap_envelope


class EnvelopeTestCase(unittest.TestCase):
    def test_roundtrip_v2(self):
        payload = pickle.dumps({'id': 'o-1', 'channel': 'web'})
        raw = wrap_envelope(payload, version=VERSION_2)
        self.assertTrue(is_envelope(raw))
        self.assertEqual(envelope_version(raw), VERSION_2)
        envelope = unwrap_envelope(raw)
        self.assertEqual(envelope, MessageEnvelope(payload, VERSION_2))
        self.assertEqual(pickle.loads(envelope.payload),
                         {'id': 'o-1', 'channel': 'web'})

    def test_default_format_is_v1_and_unwrapped(self):
        # 模拟 Huey 默认 Serializer 产出的裸 pickle 字节。
        raw = pickle.dumps(('id', 'process_order', (), {}))
        self.assertFalse(is_envelope(raw))
        self.assertEqual(envelope_version(raw), VERSION_1)
        envelope = unwrap_envelope(raw)
        self.assertEqual(envelope.version, VERSION_1)
        self.assertEqual(envelope.payload, raw)

    def test_magic_does_not_collide_with_huey_byte_streams(self):
        # pickle 协议 2+ 以 0x80 开头；gzip 0x1f；zlib 0x78。
        for stream in (pickle.dumps('x', protocol=2),
                       b'\x1f\x8b\x08compressed-like',
                       b'\x78\x9czlib-like'):
            self.assertFalse(is_envelope(stream))
        self.assertTrue(is_envelope(ENVELOPE_MAGIC + b'\x00' * 7))

    def test_header_layout(self):
        raw = wrap_envelope(b'abc', version=VERSION_2)
        self.assertEqual(len(raw), ENVELOPE_HEADER_SIZE + 3)
        self.assertEqual(raw[:8], ENVELOPE_MAGIC)
        # 负载长度字段（偏移 11..15，大端无符号整型）应为 3。
        self.assertEqual(int.from_bytes(raw[11:15], 'big'), 3)

    def test_corrupt_length_rejected(self):
        raw = wrap_envelope(b'abc', version=VERSION_2)
        # 篡改负载长度字段为 99，解析必须报错而不是静默截断。
        tampered = raw[:11] + (99).to_bytes(4, 'big') + raw[15:]
        with self.assertRaises(ValueError):
            envelope_version(tampered)
        with self.assertRaises(ValueError):
            unwrap_envelope(tampered)

    def test_unsupported_envelope_format_rejected(self):
        raw = wrap_envelope(b'abc', version=VERSION_2)
        # 偏移 8 是信封布局版本号，改成未知值。
        tampered = raw[:8] + b'\x09' + raw[9:]
        with self.assertRaises(ValueError):
            envelope_version(tampered)

    def test_version_bounds_and_type_checks(self):
        with self.assertRaises(ValueError):
            wrap_envelope(b'x', version=0)
        with self.assertRaises(ValueError):
            wrap_envelope(b'x', version=70000)
        with self.assertRaises(TypeError):
            wrap_envelope('not-bytes')

    def test_empty_payload_supported(self):
        raw = wrap_envelope(b'', version=VERSION_2)
        self.assertEqual(unwrap_envelope(raw),
                         MessageEnvelope(b'', VERSION_2))


if __name__ == '__main__':
    unittest.main()
