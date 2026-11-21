"""消息信封：在 Huey 原始序列化字节外标注业务消息版本。

信封是可选的迁移衔接层，不改变 Huey 自身的序列化内容：

* v1 投递直接以 Huey 默认格式（裸 pickle 字节）入队，不做任何包装，
  因此未接入本包的旧版 Huey 代码仍可原样生产、消费这些消息。
* v2 投递在原始字节外包一层定长二进制头：

  ======== =========== ======================================
  偏移     长度        内容
  ======== =========== ======================================
  0        8 字节      魔数 ``HUEYVB01``（ASCII）
  8        1 字节      信封格式版本号（当前为 1）
  9        2 字节      业务消息版本号（大端无符号短整型，如 2）
  11       4 字节      负载长度（大端无符号整型）
  15       N 字节      Huey 原始序列化字节（负载）
  ======== =========== ======================================

魔数的首字节是 ``H``（0x48），而 Huey 默认 pickle 流以协议操作码 0x80
开头、压缩流以 gzip(0x1f)/zlib(0x78) 开头，三者互不冲突，因此可以仅凭
字节前缀无损区分"信封消息"与"原始默认格式消息"。
"""

import struct

VERSION_1 = 1
VERSION_2 = 2
VERSION_LATEST = VERSION_2

# 信封自身的格式版本（头部布局版本），与业务消息版本是两个概念。
_ENVELOPE_FORMAT = 1
ENVELOPE_MAGIC = b'HUEYVB01'
_HEADER_STRUCT = struct.Struct('>8sBHI')  # magic, envelope fmt, msg version, length
ENVELOPE_HEADER_SIZE = _HEADER_STRUCT.size


class MessageEnvelope:
    """一条带版本标注的队列消息（信封 + Huey 原始负载）。"""

    __slots__ = ('version', 'payload')

    def __init__(self, payload, version=VERSION_LATEST):
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError('envelope payload must be bytes-like, got %r'
                            % type(payload))
        if version <= 0 or version > 65535:
            raise ValueError('message version must be in 1..65535')
        self.version = version
        self.payload = bytes(payload)

    def __repr__(self):
        return 'MessageEnvelope(version=%d, %d payload bytes)' % (
            self.version, len(self.payload))

    def __eq__(self, other):
        return (isinstance(other, MessageEnvelope) and
                self.version == other.version and
                self.payload == other.payload)


def is_envelope(data):
    """字节流是否为本模块定义的版本信封（仅检查前缀，不解析负载）。"""
    return (isinstance(data, (bytes, bytearray, memoryview)) and
            len(data) >= ENVELOPE_HEADER_SIZE and
            bytes(data[:len(ENVELOPE_MAGIC)]) == ENVELOPE_MAGIC)


def envelope_version(data):
    """返回消息的业务版本号。

    原始默认格式（无信封）一律视为 v1；信封消息读取头部版本号。
    无法识别或头部损坏时抛出 :class:`ValueError`。
    """
    if not is_envelope(data):
        return VERSION_1
    magic, envelope_fmt, msg_version, length = _HEADER_STRUCT.unpack(
        bytes(data[:ENVELOPE_HEADER_SIZE]))
    if envelope_fmt != _ENVELOPE_FORMAT:
        raise ValueError('unsupported envelope format %d' % envelope_fmt)
    if len(data) != ENVELOPE_HEADER_SIZE + length:
        raise ValueError('envelope length mismatch: header declares %d, '
                         'actual payload is %d bytes'
                         % (length, len(data) - ENVELOPE_HEADER_SIZE))
    return msg_version


def wrap_envelope(payload, version=VERSION_LATEST):
    """把 Huey 原始序列化字节包装为指定版本的信封字节。"""
    if version <= 0 or version > 65535:
        raise ValueError('message version must be in 1..65535')
    payload = bytes(payload)
    header = _HEADER_STRUCT.pack(ENVELOPE_MAGIC, _ENVELOPE_FORMAT,
                                 version, len(payload))
    return header + payload


def unwrap_envelope(data):
    """拆封信封，返回 :class:`MessageEnvelope`。

    对原始默认格式（无信封）按 v1 原样返回，保证旧消息无需迁移即可读取。
    """
    if not is_envelope(data):
        return MessageEnvelope(bytes(data), version=VERSION_1)
    magic, envelope_fmt, msg_version, length = _HEADER_STRUCT.unpack(
        bytes(data[:ENVELOPE_HEADER_SIZE]))
    if envelope_fmt != _ENVELOPE_FORMAT:
        raise ValueError('unsupported envelope format %d' % envelope_fmt)
    payload = bytes(data[ENVELOPE_HEADER_SIZE:])
    if len(payload) != length:
        raise ValueError('envelope length mismatch: header declares %d, '
                         'actual payload is %d bytes' % (length, len(payload)))
    return MessageEnvelope(payload, version=msg_version)
