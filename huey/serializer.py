import gzip
import hashlib
import hmac
import logging
import pickle
import zlib

from huey.exceptions import ConfigurationError
from huey.exceptions import MessageAuthenticationError
from huey.exceptions import MessageFormatError
from huey.exceptions import MessageRejected
from huey.utils import encode


logger = logging.getLogger('huey.serializer')

# Versioned, authenticated envelope written by SignedSerializer:
#
#     magic | version | flags | key-id-length | key-id | signature | payload
#
# The signature is HMAC-SHA256(key, header + payload), where "header" is
# everything through the key-id. The version, compression flags and key id are
# therefore authenticated: changing any of them invalidates the signature. The
# reader never guesses the compression algorithm and never falls back to the
# legacy format after an authentication failure.
SIGNED_MESSAGE_MAGIC = b'HUEY3S'
SIGNED_MESSAGE_VERSION = 1

_FLAG_COMPRESSED = 0x01
_FLAG_ZLIB = 0x02
_KNOWN_FLAGS = _FLAG_COMPRESSED | _FLAG_ZLIB
_SIGNATURE_LEN = hashlib.sha256().digest_size
_HEADER_PREFIX_LEN = len(SIGNED_MESSAGE_MAGIC) + 3  # magic + version + flags.
_MAX_KEY_ID_LEN = 255

_DEFAULT_KEY_ID = b'default'
_LEGACY_SEPARATOR = b':'
_KEY_DERIVATION_LABEL = b'huey-signed-serializer-v1'


def is_compressed(data):
    return data and (data[0] == 0x1f or data[0] == 0x78)


def _normalize_key_id(key_id):
    if isinstance(key_id, bytes):
        value = key_id
    elif isinstance(key_id, str):
        value = key_id.encode('utf8')
    else:
        raise ConfigurationError('key ids must be str or bytes')
    if not value:
        raise ConfigurationError('key ids must not be empty')
    if len(value) > _MAX_KEY_ID_LEN:
        raise ConfigurationError('key ids must be at most %d bytes'
                                 % _MAX_KEY_ID_LEN)
    return value


def _normalize_secret(secret):
    value = encode(secret)
    if not value:
        raise ConfigurationError('signing keys must not be empty')
    return value


def _derive_key(salt, key_id, secret):
    mac = hmac.new(_KEY_DERIVATION_LABEL, digestmod=hashlib.sha256)
    mac.update(salt)
    mac.update(b'\0')
    mac.update(key_id)
    mac.update(b'\0')
    mac.update(secret)
    return mac.digest()


def _legacy_key(salt, secret):
    # Matches the derivation used by the original single-secret
    # SignedSerializer so previously-enqueued messages can still be read.
    return hashlib.sha1(salt + secret).digest()


class Serializer(object):
    def __init__(self, compression=False, compression_level=6, use_zlib=False,
                 pickle_protocol=pickle.HIGHEST_PROTOCOL):
        self.comp = compression
        self.comp_level = compression_level
        self.use_zlib = use_zlib
        self.pickle_protocol = pickle_protocol or pickle.HIGHEST_PROTOCOL

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

    def deserialize(self, data):
        if self.comp:
            if not is_compressed(data):
                logger.warning('compression enabled but message data does not '
                               'appear to be compressed.')
            elif self.use_zlib:
                data = zlib.decompress(data)
            else:
                data = gzip.decompress(data)
        return self._deserialize(data)


def constant_time_compare(s1, s2):
    return hmac.compare_digest(s1, s2)


class SignedSerializer(Serializer):
    """
    Serializer that signs every message with HMAC-SHA256.

    The default, single-secret form is unchanged from previous releases::

        SignedSerializer(secret='...')

    New messages use a versioned envelope containing an authenticated key id
    and compression flags. For key rotation, pass explicit ``keys`` and
    ``signing_key`` arguments::

        SignedSerializer(
            keys={
                '2026-11': 'new-secret',   # write key
                '2026-10': 'old-secret',   # read-only during the drain
            },
            signing_key='2026-11')

    Old, unversioned messages are rejected unless ``allow_legacy=True`` is
    passed explicitly. When enabled, ``legacy_key_id`` selects the single key
    used to verify them; there is no automatic fallback after a new-format
    authentication failure.

    :param secret: secret for the single-key form.
    :param salt: salt combined with key material.
    :param keys: mapping of key id -> secret for rotation mode.
    :param signing_key: key id used to sign new messages (rotation mode).
    :param key_id: key id for the single-secret form (default ``'default'``).
    :param allow_legacy: explicitly accept unversioned old-format messages.
    :param legacy_key_id: configured key id whose secret verifies old-format
        messages. Required when ``keys`` is used with ``allow_legacy``; with a
        single ``secret`` it is fixed to that secret's key id.
    """
    def __init__(self, secret=None, salt='huey', keys=None, signing_key=None,
                 key_id=_DEFAULT_KEY_ID, allow_legacy=False,
                 legacy_key_id=None, **kwargs):
        super(SignedSerializer, self).__init__(**kwargs)

        if not salt:
            raise ConfigurationError('The salt parameter is required by %r'
                                     % type(self))
        if not isinstance(allow_legacy, bool):
            raise ConfigurationError('allow_legacy must be a bool')

        if keys is not None:
            if secret is not None:
                raise ConfigurationError('Specify either secret or keys, '
                                         'not both')
            if _normalize_key_id(key_id) != _DEFAULT_KEY_ID:
                raise ConfigurationError('key_id is only valid with secret; '
                                         'use signing_key with keys')
            if not keys:
                raise ConfigurationError('keys must not be empty')
            if not hasattr(keys, 'items'):
                raise ConfigurationError('keys must be a mapping of key id '
                                         'to secret')
            if signing_key is None:
                raise ConfigurationError('signing_key is required with keys')

            normalized_keys = {}
            for raw_id, raw_secret in keys.items():
                normalized_id = _normalize_key_id(raw_id)
                normalized_secret = _normalize_secret(raw_secret)
                if normalized_id in normalized_keys:
                    raise ConfigurationError('duplicate key id after '
                                             'normalization')
                normalized_keys[normalized_id] = normalized_secret

            signing_key_id = _normalize_key_id(signing_key)
            if signing_key_id not in normalized_keys:
                raise ConfigurationError('signing_key is not present in keys')
        else:
            if secret is None:
                raise ConfigurationError('The secret parameter is required '
                                         'by %r' % type(self))
            if signing_key is not None:
                raise ConfigurationError('signing_key requires keys')
            if legacy_key_id is not None:
                raise ConfigurationError('legacy_key_id requires keys')
            normalized_keys = {
                _normalize_key_id(key_id): _normalize_secret(secret)}
            signing_key_id = _normalize_key_id(key_id)

        if allow_legacy and keys is not None and legacy_key_id is None:
            raise ConfigurationError('legacy_key_id is required with keys '
                                     'when allow_legacy is enabled')
        if legacy_key_id is not None and not allow_legacy:
            raise ConfigurationError('legacy_key_id requires allow_legacy')
        if legacy_key_id is None:
            legacy_key_id = signing_key_id
        else:
            legacy_key_id = _normalize_key_id(legacy_key_id)
            if legacy_key_id not in normalized_keys:
                raise ConfigurationError('legacy_key_id is not present in '
                                         'keys')

        # Derive all key material before publishing any instance state, so a
        # configuration error cannot leave a partially-configured serializer.
        normalized_salt = encode(salt)
        derived_keys = {
            key_id: _derive_key(normalized_salt, key_id, key_secret)
            for key_id, key_secret in normalized_keys.items()}
        legacy_secret = normalized_keys[legacy_key_id]
        signing_material = derived_keys[signing_key_id]

        self.salt = normalized_salt
        # ``secret`` is retained for compatibility with the original
        # single-secret class. Multi-key mode does not expose any raw secret.
        self.secret = (normalized_keys[signing_key_id]
                       if keys is None else None)
        self.separator = _LEGACY_SEPARATOR
        # Plain dict and tuple keep the serializer picklable (fork-inherited
        # process workers do not require it, but callers may pickle it). No
        # public API exposes the mapping for mutation; key rotation requires a
        # new instance.
        self._derived_keys = derived_keys
        self._key_id_order = tuple(derived_keys)
        self._signing_key_id = signing_key_id
        self._signing_material = signing_material
        self._allow_legacy = allow_legacy
        self._legacy_key_id = legacy_key_id
        self._legacy_material = (_legacy_key(normalized_salt, legacy_secret)
                                 if allow_legacy else None)

    @property
    def signing_key_id(self):
        """The key id used for new messages (bytes)."""
        return self._signing_key_id

    @property
    def key_ids(self):
        """Configured key ids, including read-only keys (bytes)."""
        return self._key_id_order

    @property
    def read_key_ids(self):
        """Key ids accepted for reads (bytes)."""
        return self._key_id_order

    @property
    def legacy_enabled(self):
        return self._allow_legacy

    def _new_signature(self, header, payload):
        signature = hmac.new(self._signing_material,
                             msg=header + payload,
                             digestmod=hashlib.sha256)
        return signature.digest()

    def _legacy_signature(self, message):
        signature = hmac.new(self._legacy_material, msg=message,
                             digestmod=hashlib.sha1)
        return signature.hexdigest().encode('utf8')

    def serialize(self, data):
        payload = self._serialize(data)
        flags = 0
        if self.comp:
            flags |= _FLAG_COMPRESSED
            if self.use_zlib:
                flags |= _FLAG_ZLIB
                payload = zlib.compress(payload, self.comp_level)
            else:
                payload = gzip.compress(payload, self.comp_level)

        key_id = self._signing_key_id
        header = (SIGNED_MESSAGE_MAGIC +
                  bytes((SIGNED_MESSAGE_VERSION, flags, len(key_id))) +
                  key_id)
        return header + self._new_signature(header, payload) + payload

    def _parse_envelope(self, data):
        if len(data) < _HEADER_PREFIX_LEN + _SIGNATURE_LEN:
            raise MessageFormatError('malformed_message')
        if data[:len(SIGNED_MESSAGE_MAGIC)] != SIGNED_MESSAGE_MAGIC:
            raise MessageFormatError('malformed_message')

        version = data[len(SIGNED_MESSAGE_MAGIC)]
        if version != SIGNED_MESSAGE_VERSION:
            raise MessageFormatError('unsupported_version')

        flags = data[len(SIGNED_MESSAGE_MAGIC) + 1]
        if flags & ~_KNOWN_FLAGS:
            raise MessageFormatError('invalid_flags')
        if (flags & _FLAG_ZLIB) and not (flags & _FLAG_COMPRESSED):
            raise MessageFormatError('invalid_flags')

        key_id_len = data[len(SIGNED_MESSAGE_MAGIC) + 2]
        if key_id_len == 0:
            raise MessageFormatError('invalid_key_id')

        header_len = _HEADER_PREFIX_LEN + key_id_len
        if len(data) < header_len + _SIGNATURE_LEN:
            raise MessageFormatError('malformed_message')

        key_id = data[_HEADER_PREFIX_LEN:header_len]
        header = data[:header_len]
        signature = data[header_len:header_len + _SIGNATURE_LEN]
        payload = data[header_len + _SIGNATURE_LEN:]
        return flags, key_id, header, signature, payload

    def _deserialize_envelope(self, data):
        flags, key_id, header, signature, payload = self._parse_envelope(data)

        # Authentication happens before decompression and before any pickle
        # load. An unknown or retired key id is rejected here.
        material = self._derived_keys.get(key_id)
        if material is None:
            raise MessageAuthenticationError('unknown_key_id')

        expected = hmac.new(material, msg=header + payload,
                            digestmod=hashlib.sha256).digest()
        if not constant_time_compare(signature, expected):
            raise MessageAuthenticationError('signature_mismatch')

        if flags & _FLAG_COMPRESSED:
            try:
                if flags & _FLAG_ZLIB:
                    payload = zlib.decompress(payload)
                else:
                    payload = gzip.decompress(payload)
            except Exception:
                raise MessageFormatError('decompression_failed')

        try:
            return self._deserialize(payload)
        except Exception:
            raise MessageFormatError('deserialization_failed')

    def _deserialize_legacy(self, data):
        if not self._allow_legacy:
            raise MessageAuthenticationError('legacy_format_disabled')

        # Legacy messages carry no authenticated compression flag. This
        # sniffing path exists only because the caller explicitly opted into
        # legacy compatibility, and is never reached after an envelope
        # authentication failure.
        if self.comp:
            if not is_compressed(data):
                logger.warning('compression enabled but legacy message data '
                               'does not appear to be compressed.')
            else:
                try:
                    if self.use_zlib:
                        data = zlib.decompress(data)
                    else:
                        data = gzip.decompress(data)
                except Exception:
                    raise MessageFormatError('decompression_failed')

        if _LEGACY_SEPARATOR not in data:
            raise MessageFormatError('malformed_legacy_message')

        message, signature = data.rsplit(_LEGACY_SEPARATOR, 1)
        if not constant_time_compare(signature,
                                     self._legacy_signature(message)):
            raise MessageAuthenticationError('legacy_signature_mismatch')

        try:
            return self._deserialize(message)
        except Exception:
            raise MessageFormatError('deserialization_failed')

    def deserialize(self, data):
        try:
            if not isinstance(data, bytes):
                try:
                    if isinstance(data, int):
                        raise TypeError
                    data = bytes(data)
                except Exception:
                    raise MessageFormatError('malformed_message')

            if data[:len(SIGNED_MESSAGE_MAGIC)] == SIGNED_MESSAGE_MAGIC:
                return self._deserialize_envelope(data)
            return self._deserialize_legacy(data)
        except MessageRejected as exc:
            # Log the classification only: no key material, key id, raw
            # message, or task arguments.
            logger.warning('Message rejected: %s', exc.reason)
            raise


class RotatingSignedSerializer(SignedSerializer):
    """
    Explicit multi-key mode. Requires a write key and a non-empty mapping of
    read keys::

        RotatingSignedSerializer(
            keys={'2026-11': 'new', '2026-10': 'old'},
            signing_key='2026-11')

    Removing a key from ``keys`` immediately rejects messages signed with it.
    """
    def __init__(self, keys, signing_key, salt='huey', **kwargs):
        super(RotatingSignedSerializer, self).__init__(
            keys=keys, signing_key=signing_key, salt=salt, **kwargs)
