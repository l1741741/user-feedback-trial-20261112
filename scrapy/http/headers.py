from __future__ import annotations

import warnings
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, Self, TypeAlias, cast

from w3lib.http import headers_dict_to_raw

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.utils._datatypes import CaseInsensitiveDict

if TYPE_CHECKING:
    from collections.abc import Iterable


_RawValue: TypeAlias = bytes | str | int

#: Value written in place of a redacted header or cookie value.
REDACTION_PLACEHOLDER: bytes = b"[REDACTED]"

# Header fields whose values are credentials and are always redacted.
_DEFAULT_REDACTED_FIELDS: tuple[bytes, ...] = (
    b"Authorization",
    b"Proxy-Authorization",
)

# RFC 9110 token characters, used to recognize a safe cookie name.
_COOKIE_NAME_CHARS = frozenset(
    b"!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)

_RedactionStrategy: TypeAlias = Literal["mask", "remove"]


def _is_cookie_name(name: bytes) -> bool:
    """Return whether *name* is an RFC 9110 token usable as a cookie name."""
    return bool(name) and all(char in _COOKIE_NAME_CHARS for char in name)


def _parse_cookie_pairs(value: bytes) -> list[tuple[bytes, bytes]] | None:
    """Split a ``Cookie`` request header into ``(name, raw_pair)`` pairs.

    The raw pair keeps the original spelling, including any ``=`` inside the
    cookie value. Return ``None`` when the header cannot be parsed safely, so
    the caller can redact it as a whole without inspecting its contents.
    """
    pairs: list[tuple[bytes, bytes]] = []
    # RFC 6265 separates cookie pairs with "; "; a ";" without a space would
    # make the bytes after it part of the previous cookie's value, so it
    # could smuggle a non-whitelisted cookie past the name check.
    if b";" in value.replace(b"; ", b""):
        return None
    for raw_pair in value.split(b"; "):
        if b"=" not in raw_pair:
            return None
        name = raw_pair.split(b"=", 1)[0]
        if not _is_cookie_name(name):
            return None
        pairs.append((name, raw_pair))
    return pairs


def _parse_set_cookie_name(value: bytes) -> bytes | None:
    """Return the cookie name of a ``Set-Cookie`` header value, or ``None``.

    Only the leading ``name=value`` pair is inspected; attributes after the
    first semicolon, whose values may contain commas (e.g. ``Expires``), are
    never split or otherwise interpreted.
    """
    first_pair = value.split(b";", 1)[0].strip()
    if b"=" not in first_pair:
        return None
    name = first_pair.split(b"=", 1)[0]
    return name if _is_cookie_name(name) else None


def _validate_names(names: Iterable[str | bytes], encoding: str) -> list[bytes]:
    """Materialize and validate header or cookie names from a parameter.

    The iterable is consumed eagerly so a failing parameter cannot leave the
    caller's iterator partially consumed. Names containing CR or LF are
    rejected to keep redacted output free of injected header lines.
    """
    normalized: list[bytes] = []
    for name in tuple(names):
        if not isinstance(name, (str, bytes)):
            raise TypeError(
                "Header and cookie names must be str or bytes, got "
                f"{type(name).__name__}"
            )
        raw_name = name.encode(encoding) if isinstance(name, str) else name
        if not raw_name:
            raise ValueError("Header and cookie names must not be empty")
        if b"\r" in raw_name or b"\n" in raw_name:
            raise ValueError(
                "Header and cookie names must not contain CR or LF characters"
            )
        normalized.append(raw_name)
    return normalized


class Headers(dict):  # type: ignore[type-arg]
    """Case insensitive http headers dictionary"""

    def __init__(
        self,
        seq: Mapping[str, Any]
        | Mapping[bytes, Any]
        | Iterable[tuple[str | bytes, Any]]
        | None = None,
        encoding: str = "utf-8",
    ):
        self.encoding: str = encoding
        super().__init__()
        if seq:
            self.update(seq)

    def __setitem__(self, key: str | bytes, value: Any) -> None:
        dict.__setitem__(self, self._normkey(key), self._normvalue(value))

    def __delitem__(self, key: str | bytes) -> None:
        dict.__delitem__(self, self._normkey(key))

    def __contains__(self, key: str | bytes) -> bool:  # type: ignore[override]
        return dict.__contains__(self, self._normkey(key))

    def has_key(self, key: str | bytes) -> bool:
        warnings.warn(
            'Headers.has_key() is deprecated, use the "in" operator instead.',
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return key in self

    def setdefault(self, key: str | bytes, def_val: Any = None) -> Any:
        return dict.setdefault(self, self._normkey(key), self._normvalue(def_val))

    @classmethod
    def fromkeys(  # type: ignore[override]
        cls, keys: Iterable[str | bytes], value: Any = None
    ) -> Self:
        warnings.warn(
            "Headers.fromkeys() is deprecated, pass (key, value) pairs to"
            " Headers() instead.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return cls((k, value) for k in keys)

    def pop(self, key: str | bytes, *args: Any) -> Any:
        return dict.pop(self, self._normkey(key), *args)

    def update(  # type: ignore[override]
        self,
        seq: Mapping[str, Any]
        | Mapping[bytes, Any]
        | Iterable[tuple[str | bytes, Any]],
    ) -> None:
        items = seq.items() if isinstance(seq, Mapping) else seq
        iseq: dict[bytes, list[bytes]] = {}
        # normkey() only sees keys already stored, so keys from seq that differ
        # only in case are mapped to a single spelling here.
        spellings: dict[bytes, bytes] = {}
        for k, v in items:
            key = self._normkey(k)
            key = spellings.setdefault(key.lower(), key)
            iseq.setdefault(key, []).extend(self._normvalue(v))
        dict.update(self, iseq)

    def _normkey(self, key: str | bytes) -> bytes:
        """Normalize key to bytes, matching the case of an existing key if any."""
        key = self._tobytes(key)
        if dict.__contains__(self, key):
            return key
        lower_key = key.lower()
        existing_key: bytes
        for existing_key in dict.keys(self):
            if existing_key.lower() == lower_key:
                return existing_key
        return key

    def normkey(self, key: str | bytes) -> bytes:  # pragma: no cover
        warnings.warn(
            "Headers.normkey() is deprecated",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return self._normkey(key)

    def _normvalue(self, value: _RawValue | Iterable[_RawValue]) -> list[bytes]:
        """Normalize values to bytes."""
        _value: Iterable[_RawValue]
        if value is None:
            _value = []
        elif isinstance(value, (str, bytes)):
            _value = [value]
        elif hasattr(value, "__iter__"):
            _value = value
        else:
            _value = [value]

        return [self._tobytes(x) for x in _value]

    def normvalue(
        self, value: _RawValue | Iterable[_RawValue]
    ) -> list[bytes]:  # pragma: no cover
        warnings.warn(
            "Headers.normvalue() is deprecated",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return self._normvalue(value)

    def _tobytes(self, x: _RawValue) -> bytes:
        if isinstance(x, bytes):
            return x
        if isinstance(x, str):
            return x.encode(self.encoding)
        if isinstance(x, int):
            return str(x).encode(self.encoding)
        raise TypeError(f"Unsupported value type: {type(x)}")

    def __getitem__(self, key: str | bytes) -> bytes | None:
        try:
            return cast("list[bytes]", dict.__getitem__(self, self._normkey(key)))[-1]
        except IndexError:
            return None

    def get(self, key: str | bytes, def_val: Any = None) -> bytes | None:
        values = cast("list[bytes] | None", dict.get(self, self._normkey(key)))
        if not values:
            # The header is missing, or it was set to None so that it is not
            # sent; neither has a value to return.
            values = self._normvalue(def_val)
        return values[-1] if values else None

    def getlist(
        self, key: str | bytes, def_val: _RawValue | Iterable[_RawValue] | None = None
    ) -> list[bytes]:
        try:
            return cast("list[bytes]", dict.__getitem__(self, self._normkey(key)))
        except KeyError:
            if def_val is not None:
                return self._normvalue(def_val)
            return []

    def setlist(self, key: str | bytes, list_: Iterable[_RawValue]) -> None:
        warnings.warn(
            "Headers.setlist() is deprecated, assign the list instead:"
            " headers[key] = values.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        self[key] = list_

    def setlistdefault(
        self, key: str | bytes, default_list: Iterable[_RawValue] = ()
    ) -> Any:
        warnings.warn(
            "Headers.setlistdefault() is deprecated, use setdefault() instead.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return self.setdefault(key, default_list)

    def appendlist(self, key: str | bytes, value: Iterable[_RawValue]) -> None:
        lst = self.getlist(key)
        lst.extend(self._normvalue(value))
        self[key] = lst

    def items(self) -> Iterable[tuple[bytes, list[bytes]]]:  # type: ignore[override]
        return ((k, self.getlist(k)) for k in self.keys())

    def values(self) -> list[bytes | None]:  # type: ignore[override]
        return [
            self[k]
            for k in self.keys()  # pylint: disable=consider-using-dict-items
        ]

    def to_string(self) -> bytes:
        return headers_dict_to_raw(self)

    def to_unicode_dict(self) -> CaseInsensitiveDict:
        warnings.warn(
            "Headers.to_unicode_dict() is deprecated, use to_tuple_list() instead.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return CaseInsensitiveDict(
            (
                key.decode(self.encoding, errors="replace"),
                b",".join(value).decode(self.encoding, errors="replace"),
            )
            for key, value in self.items()
        )

    def to_tuple_list(self) -> list[tuple[str, str]]:
        """Return headers as a list of ``(key, value)`` tuples.

        Multiple values are represented as multiple tuples with the same key.
        Bytes that cannot be decoded are replaced with U+FFFD.
        """
        return [
            (
                key.decode(self.encoding, errors="replace"),
                value.decode(self.encoding, errors="replace"),
            )
            for key, values in self.items()
            for value in values
        ]

    def redacted_copy(
        self,
        *,
        fields: Iterable[str | bytes] = (),
        allowed_cookies: Iterable[str | bytes] = (),
        strategy: _RedactionStrategy = "mask",
    ) -> Self:
        """Return a copy with credentials and sensitive headers redacted.

        The object itself is never modified, and the returned copy does not
        share any value lists with it, so the copy can be logged or attached
        to a support ticket safely.

        Header field names in *fields* are matched case-insensitively; the
        ``Authorization`` and ``Proxy-Authorization`` fields are always
        redacted. With the default ``"mask"`` *strategy*, every value of a
        redacted field is replaced by :data:`REDACTION_PLACEHOLDER`, keeping
        one placeholder per original value. With ``"remove"``, redacted
        fields are left out of the copy.

        ``Cookie`` and ``Set-Cookie`` are handled by cookie name instead of
        as opaque fields: cookies whose name is in *allowed_cookies*
        (case-sensitive) are kept verbatim. In a ``Cookie`` request header
        each other cookie keeps its name while its value is masked, and a
        header whose cookie names cannot safely be parsed is redacted as a
        whole. Each ``Set-Cookie`` value is treated as one cookie, so its
        multi-value boundaries and attributes such as ``Expires`` are
        preserved; non-whitelisted and unparseable values are redacted as a
        whole. Under ``"remove"``, non-allowed cookies and unparseable
        values are dropped.

        All parameters are validated before any data is read from the
        object, so an invalid argument raises without modifying the source
        or producing a partially redacted result.

        .. versionadded:: VERSION
        """
        if strategy not in ("mask", "remove"):
            raise ValueError(f"strategy must be 'mask' or 'remove', got {strategy!r}")
        extra_fields = _validate_names(fields, self.encoding)
        allowed = frozenset(_validate_names(allowed_cookies, self.encoding))
        redacted_fields = frozenset(
            name.lower() for name in (*_DEFAULT_REDACTED_FIELDS, *extra_fields)
        )

        redacted: dict[bytes, list[bytes]] = {}
        for key, values in dict.items(self):
            lower_key = key.lower()
            if lower_key in redacted_fields:
                if strategy == "mask":
                    # One placeholder per original value; an empty list is a
                    # header set to None and is kept as an empty list.
                    redacted[key] = [REDACTION_PLACEHOLDER for _ in values]
                continue
            if lower_key == b"cookie":
                new_values = self._redact_cookie_values(values, allowed, strategy)
            elif lower_key == b"set-cookie":
                new_values = self._redact_set_cookie_values(values, allowed, strategy)
            else:
                new_values = list(values)
            if new_values or not values:
                redacted[key] = new_values
        return self.__class__(redacted, encoding=self.encoding)

    @staticmethod
    def _redact_cookie_values(
        values: list[bytes],
        allowed: frozenset[bytes],
        strategy: _RedactionStrategy,
    ) -> list[bytes]:
        redacted_values: list[bytes] = []
        for value in values:
            pairs = _parse_cookie_pairs(value)
            if pairs is None:
                if strategy == "mask":
                    redacted_values.append(REDACTION_PLACEHOLDER)
                continue
            if all(name in allowed for name, _ in pairs):
                redacted_values.append(value)
                continue
            kept_pairs: list[bytes] = []
            for name, raw_pair in pairs:
                if name in allowed:
                    kept_pairs.append(raw_pair)
                elif strategy == "mask":
                    kept_pairs.append(name + b"=" + REDACTION_PLACEHOLDER)
            if kept_pairs:
                redacted_values.append(b"; ".join(kept_pairs))
        return redacted_values

    @staticmethod
    def _redact_set_cookie_values(
        values: list[bytes],
        allowed: frozenset[bytes],
        strategy: _RedactionStrategy,
    ) -> list[bytes]:
        redacted_values: list[bytes] = []
        for value in values:
            name = _parse_set_cookie_name(value)
            if name is not None and name in allowed:
                redacted_values.append(value)
            elif strategy == "mask":
                redacted_values.append(REDACTION_PLACEHOLDER)
        return redacted_values

    def __copy__(self) -> Self:
        return self.__class__(self)

    copy = __copy__
