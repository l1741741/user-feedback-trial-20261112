import gzip
import hashlib
import hmac
import logging
import pickle
import zlib

from huey.exceptions import ConfigurationError
from huey.exceptions import MessageDecodeError
from huey.exceptions import MessageTooLarge
from huey.utils import encode


logger = logging.getLogger('huey.serializer')

# Size of the chunks fed to the streaming decompressor. Kept small enough that
# the in-flight memory stays proportional to the configured budget rather than
# to the (potentially huge) decompressed payload.
_DECOMPRESS_CHUNK = 65536


def is_compressed(data):
    return data and (data[0] == 0x1f or data[0] == 0x78)


def _bounded_decompress(data, max_bytes, use_zlib):
    """
    Decompress ``data`` incrementally, enforcing a cumulative limit of
    ``max_bytes`` on the decompressed output (``None`` means no limit). The
    limit is enforced as the output is produced -- the stream is never fully
    decompressed first and measured afterwards -- so peak memory stays
    proportional to the budget rather than to the payload size.

    Policy for malformed input (all raised as sanitized MessageDecodeError):

    * truncated streams (input ends before the stream is complete) -> error
    * checksum / format failures -> error
    * gzip: concatenated members are supported, with the budget applied
      cumulatively across all members; anything after the final member that
      is not another well-formed member -> error
    * zlib: any data after the end of the stream -> error

    Nothing is returned (and hence nothing reaches pickle) until the whole
    stream has been verified to be complete and well-formed.
    """
    wbits = 15 if use_zlib else 31  # 31 = gzip header + trailer checksum.
    kind = 'zlib' if use_zlib else 'gzip'
    parts = []
    total = 0
    buf = bytes(data)

    while True:
        decomp = zlib.decompressobj(wbits)
        pos = 0
        end = len(buf)
        while True:
            if max_bytes is None:
                room = 0  # Zero means "unlimited" for decompress().
            else:
                # Allow one byte past the budget so an oversized stream is
                # detected precisely instead of silently truncated.
                room = max_bytes - total + 1
            piece = buf[pos:pos + _DECOMPRESS_CHUNK]
            pos += len(piece)
            try:
                out = decomp.decompress(piece, room)
            except zlib.error as exc:
                # zlib error messages are static descriptions (e.g. "incorrect
                # data check") and never contain message content.
                raise MessageDecodeError('invalid %s stream: %s' % (kind, exc))
            total += len(out)
            parts.append(out)
            if max_bytes is not None and total > max_bytes:
                raise MessageTooLarge('decompressed message size', max_bytes)
            if decomp.unconsumed_tail:
                # The output cap was reached with input still unprocessed,
                # which means the stream produces more than the budget.
                raise MessageTooLarge('decompressed message size', max_bytes)
            if decomp.eof:
                out = decomp.flush()
                if out:
                    total += len(out)
                    parts.append(out)
                    if max_bytes is not None and total > max_bytes:
                        raise MessageTooLarge('decompressed message size',
                                              max_bytes)
                break
            if pos >= end:
                raise MessageDecodeError('truncated %s stream' % kind)

        rest = decomp.unused_data
        if not rest:
            break
        if use_zlib:
            raise MessageDecodeError(
                'unexpected trailing data after zlib stream')
        if rest[:2] != b'\x1f\x8b':
            raise MessageDecodeError(
                'unexpected trailing data after gzip stream')
        # Another gzip member follows; decompress it under the same
        # cumulative budget.
        buf = rest

    return b''.join(parts)


class Serializer(object):
    def __init__(self, compression=False, compression_level=6, use_zlib=False,
                 pickle_protocol=pickle.HIGHEST_PROTOCOL,
                 max_message_bytes=None, max_decompressed_bytes=None):
        self.comp = compression
        self.comp_level = compression_level
        self.use_zlib = use_zlib
        self.pickle_protocol = pickle_protocol or pickle.HIGHEST_PROTOCOL

        for name, value in (('max_message_bytes', max_message_bytes),
                            ('max_decompressed_bytes', max_decompressed_bytes)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value < 0:
                raise ConfigurationError(
                    '%s must be a non-negative integer or None, got %r'
                    % (name, value))
        self.max_message_bytes = max_message_bytes
        self.max_decompressed_bytes = max_decompressed_bytes

    @property
    def bounded(self):
        """
        Whether a decoding budget is in effect. When no budget is configured
        the legacy code paths (and their exact behavior) are used.
        """
        return self.max_message_bytes is not None or \
            self.max_decompressed_bytes is not None

    def _serialize(self, data):
        return pickle.dumps(data, self.pickle_protocol)

    def _deserialize(self, data):
        return pickle.loads(data)

    def serialize(self, data):
        data = self._serialize(data)
        if self.comp:
            if self.use_zlib:
                data = zlib.compress(data, self.comp_level)
            else:
                data = gzip.compress(data, self.comp_level)
        return data

    def _decompress(self, data):
        if self.bounded:
            return _bounded_decompress(data, self.max_decompressed_bytes,
                                       self.use_zlib)
        if self.use_zlib:
            return zlib.decompress(data)
        return gzip.decompress(data)

    def deserialize(self, data):
        # Enforce the transfer budget before anything else is done with the
        # message -- in particular before decompression and before pickle.
        if self.max_message_bytes is not None \
                and len(data) > self.max_message_bytes:
            raise MessageTooLarge('serialized message size',
                                  self.max_message_bytes)
        if self.comp:
            if not is_compressed(data):
                logger.warning('compression enabled but message data does not '
                               'appear to be compressed.')
            else:
                data = self._decompress(data)
        # Covers messages that were not compressed (either because compression
        # is disabled or because the data was not actually compressed): the
        # payload handed to the deserializer must still respect the budget.
        if self.max_decompressed_bytes is not None \
                and len(data) > self.max_decompressed_bytes:
            raise MessageTooLarge('decompressed message size',
                                  self.max_decompressed_bytes)
        return self._deserialize(data)


def constant_time_compare(s1, s2):
    return hmac.compare_digest(s1, s2)


class SignedSerializer(Serializer):
    def __init__(self, secret=None, salt='huey', **kwargs):
        super(SignedSerializer, self).__init__(**kwargs)
        if not secret or not salt:
            raise ConfigurationError('The secret and salt parameters are '
                                     'required by %r' % type(self))
        self.secret = encode(secret)
        self.salt = encode(salt)
        self.separator = b':'
        self._key = hashlib.sha1(self.salt + self.secret).digest()

    def _signature(self, message):
        signature = hmac.new(self._key, msg=message, digestmod=hashlib.sha1)
        return signature.hexdigest().encode('utf8')

    def _sign(self, message):
        return message + self.separator + self._signature(message)

    def _unsign(self, signed):
        # NOTE: the error messages are deliberately generic. They must not
        # echo the supplied signature, keying material, or any part of the
        # message content, since these errors are typically logged.
        if self.separator not in signed:
            raise ValueError('Message is missing its signature.')

        msg, sig = signed.rsplit(self.separator, 1)
        if constant_time_compare(sig, self._signature(msg)):
            return msg

        raise ValueError('Message signature does not match.')

    def _serialize(self, message):
        data = super(SignedSerializer, self)._serialize(message)
        return self._sign(data)

    def _deserialize(self, data):
        # Authentication always happens before the (executable) pickle
        # deserialization: an unauthenticated message never reaches
        # pickle.loads().
        return super(SignedSerializer, self)._deserialize(self._unsign(data))
