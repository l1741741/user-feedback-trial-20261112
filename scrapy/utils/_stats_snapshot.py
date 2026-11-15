from __future__ import annotations

import base64
import json
import math
import os
import sys
from datetime import date, datetime, time, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from tempfile import mkstemp
from typing import TYPE_CHECKING, Any

from scrapy.exceptions import StatsSnapshotError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from scrapy.settings import BaseSettings

# Bounded, redacted, JSON-compatible copies of stats. Nothing here is public
# API; scrapy.statscollectors exposes the documented entry points.

# Value written in place of a mapping value whose key matches a sensitive-key
# pattern.
REDACTED_VALUE = "[REDACTED]"

# Reserved key of the object that represents a bytes value.
BYTES_VALUE_KEY = "__scrapy_bytes_base64__"

# Returned by the scalar conversion to mean "this value is a container or is
# unsupported", as ``None`` is a valid converted scalar.
_NOT_SCALAR = object()

# Key patterns whose values are never copied, at any mapping level. They
# cannot be disabled, so a configuration mistake cannot leak a credential.
_DEFAULT_SENSITIVE_KEYS: tuple[str, ...] = (
    "*access_key*",
    "*api_key*",
    "*apikey*",
    "*auth*",
    "*cookie*",
    "*credential*",
    "*passwd*",
    "*password*",
    "*private_key*",
    "*secret*",
    "*session*",
    "*token*",
)


def type_name(value: Any) -> str:
    """Return the import path of the class of *value*.

    The class attributes are read through :class:`type` so that a custom
    metaclass cannot run code here.
    """
    cls = type(value)
    module = type.__getattribute__(cls, "__module__")
    qualname = type.__getattribute__(cls, "__qualname__")
    return f"{module}.{qualname}"


def _pattern_tuple(patterns: Iterable[Any], setting: str) -> tuple[str, ...]:
    if isinstance(patterns, str):
        # A bare string would otherwise be split into single characters.
        raise StatsSnapshotError(
            f"{setting} must be a list of patterns, not a string", reason="config"
        )
    normalized = []
    for pattern in patterns:
        if not isinstance(pattern, str):
            raise StatsSnapshotError(
                f"{setting} entries must be strings, got {type_name(pattern)}",
                reason="config",
            )
        normalized.append(pattern)
    return tuple(normalized)


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StatsSnapshotError(
            f"{name} must be a positive integer, got {type_name(value)}",
            reason="config",
        )
    return value


def pattern_matches(name: str, patterns: tuple[str, ...]) -> bool:
    """Return whether *name* matches any glob in *patterns*, case-insensitively."""
    folded = name.casefold()
    return any(fnmatchcase(folded, pattern.casefold()) for pattern in patterns)


def _json_size(text: str, path: str) -> int:
    """Return the UTF-8 size of *text* encoded as a JSON string."""
    try:
        return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))
    except UnicodeEncodeError:
        raise StatsSnapshotError(
            f"Cannot create a stats snapshot at {path}: the string is not valid "
            "UTF-8 text",
            path=path,
            reason="unsupported",
        ) from None


def _format_path(path: str, key: Any) -> str:
    # The key is quoted so that a key containing brackets or dots cannot
    # make two different locations print the same way. Only the first 100
    # characters are included, which keeps the message bounded; the copy
    # itself keeps the full key. A lone surrogate is escaped rather than
    # encoded, so the message never contains invalid UTF-8.
    if not isinstance(key, str):
        return f"{path}[<non-string key>]"
    text = str.__str__(key)
    if len(text) > 100:
        text = f"{text[:100]}…"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return f"{path}[{json.dumps(text)}]"
    return f"{path}[{json.dumps(text, ensure_ascii=False)}]"


def _format_index(path: str, index: int) -> str:
    return f"{path}[{index}]"


class _SnapshotBuilder:
    def __init__(
        self,
        *,
        include: tuple[str, ...],
        sensitive: tuple[str, ...],
        max_depth: int,
        max_nodes: int,
        max_bytes: int,
    ):
        self.include = include
        self.sensitive = sensitive
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.max_bytes = max_bytes
        self.nodes = 0
        self.size = 0
        self.ancestors: set[int] = set()

    def build(self, stats: Any) -> dict[str, Any]:
        # The collector always holds a dict; this guards against a subclass or
        # a caller that replaced it with something else.
        if not isinstance(stats, dict):  # pragma: no cover
            raise StatsSnapshotError(
                "Cannot create a stats snapshot at stats: the stats container "
                f"is of unsupported type {type_name(stats)}",
                path="stats",
                reason="unsupported",
            )
        return self._mapping(stats, depth=1, path="stats", root=True)

    def _fail(
        self,
        path: str,
        reason: str,
        detail: str,
        *,
        maximum: int | None = None,
        actual: int | None = None,
    ) -> None:
        raise StatsSnapshotError(
            f"Cannot create a stats snapshot at {path}: {detail}",
            path=path,
            reason=reason,
            maximum=maximum,
            actual=actual,
        )

    def _tick(self, path: str) -> None:
        self.nodes += 1
        if self.nodes > self.max_nodes:
            self._fail(
                path,
                "nodes",
                f"the maximum of {self.max_nodes} values and keys is exceeded",
                maximum=self.max_nodes,
                actual=self.nodes,
            )

    def _add_size(self, path: str, amount: int) -> None:
        self.size += amount
        if self.size > self.max_bytes:
            self._fail(
                path,
                "bytes",
                f"the maximum of {self.max_bytes} encoded bytes is exceeded",
                maximum=self.max_bytes,
                actual=self.size,
            )

    def _enter(self, value: Any, depth: int, path: str) -> None:
        if depth > self.max_depth:
            self._fail(
                path,
                "depth",
                f"the maximum nesting depth of {self.max_depth} is exceeded",
                maximum=self.max_depth,
                actual=depth,
            )
        value_id = id(value)
        if value_id in self.ancestors:
            self._fail(path, "circular", "the value contains a circular reference")
        self.ancestors.add(value_id)

    def _leave(self, value: Any) -> None:
        self.ancestors.discard(id(value))

    def _mapping(
        self, value: dict[Any, Any], *, depth: int, path: str, root: bool = False
    ) -> dict[str, Any]:
        self._enter(value, depth, path)
        try:
            self._tick(path)
            self._add_size(path, 2)
            result: dict[str, Any] = {}
            for key, item in dict.items(value):
                if not isinstance(key, str):
                    self._fail(
                        _format_path(path, key),
                        "key",
                        f"mapping keys must be strings, got {type_name(key)}",
                    )
                text = str.__str__(key)
                if root and self.include and not pattern_matches(text, self.include):
                    continue
                key_path = _format_path(path, key)
                if result:
                    self._add_size(path, 1)
                self._tick(path)
                self._add_size(key_path, _json_size(text, key_path) + 1)
                if pattern_matches(text, self.sensitive):
                    self._tick(path)
                    self._add_size(key_path, _json_size(REDACTED_VALUE, key_path))
                    result[text] = REDACTED_VALUE
                else:
                    result[text] = self._convert(item, depth + 1, key_path)
            return result
        finally:
            self._leave(value)

    def _sequence(
        self, value: Any, *, depth: int, path: str, iterator: Any
    ) -> list[Any]:
        self._enter(value, depth, path)
        try:
            self._tick(path)
            self._add_size(path, 2)
            result: list[Any] = []
            for index, item in enumerate(iterator(value)):
                if index:
                    self._add_size(path, 1)
                result.append(
                    self._convert(item, depth + 1, _format_index(path, index))
                )
            return result
        finally:
            self._leave(value)

    def _set(self, value: Any, *, depth: int, path: str, iterator: Any) -> list[Any]:
        self._enter(value, depth, path)
        try:
            self._tick(path)
            self._add_size(path, 2)
            result = [
                self._convert(item, depth + 1, _format_index(path, index))
                for index, item in enumerate(iterator(value))
            ]
            if len(result) > 1:
                self._add_size(path, len(result) - 1)
            # Set iteration order is unspecified, so sort by the JSON encoding
            # of the converted values to keep repeated snapshots comparable.
            result.sort(key=_canonical_json)
            return result
        finally:
            self._leave(value)

    def _convert(self, value: Any, depth: int, path: str) -> Any:
        scalar = self._scalar(value, path)
        if scalar is not _NOT_SCALAR:
            return scalar
        # Only the containers below recurse, so they need the depth check.
        if isinstance(value, dict):
            return self._mapping(value, depth=depth, path=path)
        if isinstance(value, (list, tuple)):
            unbound_list_iterator = (
                list.__iter__ if isinstance(value, list) else tuple.__iter__
            )
            return self._sequence(
                value, depth=depth, path=path, iterator=unbound_list_iterator
            )
        if isinstance(value, (set, frozenset)):
            unbound_set_iterator = (
                set.__iter__ if isinstance(value, set) else frozenset.__iter__
            )
            return self._set(
                value, depth=depth, path=path, iterator=unbound_set_iterator
            )
        self._fail(path, "unsupported", f"unsupported type {type_name(value)}")
        raise AssertionError  # pragma: no cover

    def _scalar(self, value: Any, path: str) -> Any:
        # The checks are ordered from the cheapest and most common types to
        # the rarer ones, and before the containers, because ``bool`` is a
        # subclass of ``int`` and ``datetime`` is a subclass of ``date``.
        # Each branch counts its own JSON bytes.
        if value is None:
            self._tick(path)
            self._add_size(path, 4)
            return None
        if isinstance(value, bool):
            self._tick(path)
            self._add_size(path, 4 if value else 5)
            return value
        if isinstance(value, int):
            return self._int(value, path)
        if isinstance(value, float):
            return self._float(value, path)
        if isinstance(value, str):
            return self._string(str.__str__(value), path)
        if isinstance(value, bytes):
            return self._bytes(value, path)
        if isinstance(value, datetime):
            text = datetime.isoformat(value)
            if datetime.utcoffset(value) is None:
                # A naive datetime carries no zone, so expose it as UTC
                # instead of leaving readers to guess the local time.
                text += "+00:00"
            return self._string(text, path)
        if isinstance(value, date):
            return self._string(date.isoformat(value), path)
        if isinstance(value, time):
            return self._string(time.isoformat(value), path)
        if isinstance(value, timedelta):
            return self._timedelta(value, path)
        return _NOT_SCALAR

    def _int(self, value: int, path: str) -> int:
        number = int.__int__(value)
        # Reject huge integers from their bit length before formatting them;
        # this bound never underestimates the decimal digit count.
        digits = int.bit_length(number) * 30103 // 100000 + 2
        if digits > self.max_bytes:
            self._fail(
                path,
                "bytes",
                f"an integer value exceeds {self.max_bytes} encoded bytes",
                maximum=self.max_bytes,
            )
        self._tick(path)
        self._add_size(path, len(str(number)))
        return number

    def _float(self, value: float, path: str) -> float:
        number = float.__float__(value)
        if not math.isfinite(number):
            self._fail(
                path,
                "non-finite",
                "non-finite float values cannot be represented in JSON",
            )
        self._tick(path)
        self._add_size(path, len(json.dumps(number)))
        return number

    def _timedelta(self, value: timedelta, path: str) -> float:
        seconds = timedelta.total_seconds(value)
        self._tick(path)
        self._add_size(path, len(json.dumps(seconds)))
        return seconds

    def _string(self, text: str, path: str) -> str:
        # Check the character count first: a string longer than the byte
        # budget cannot fit, and encoding it just to find that out would
        # allocate a copy as large as the string itself.
        if str.__len__(text) > self.max_bytes:
            self._fail(
                path,
                "bytes",
                f"a string value exceeds the maximum of {self.max_bytes} encoded bytes",
                maximum=self.max_bytes,
            )
        size = _json_size(text, path)
        self._tick(path)
        self._add_size(path, size)
        return text

    def _bytes(self, value: bytes, path: str) -> dict[str, str]:
        # Base64 output is longer than the input, so a value longer than the
        # whole byte budget cannot fit and does not need to be encoded.
        if bytes.__len__(value) > self.max_bytes:
            self._fail(
                path,
                "bytes",
                f"a bytes value exceeds the maximum of {self.max_bytes} encoded bytes",
                maximum=self.max_bytes,
            )
        raw = bytes.__getitem__(value, slice(None))
        encoded = base64.b64encode(raw).decode("ascii")
        marker_size = (
            2 + _json_size(BYTES_VALUE_KEY, path) + 1 + _json_size(encoded, path)
        )
        self._tick(path)
        self._add_size(path, marker_size)
        return {BYTES_VALUE_KEY: encoded}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_stats_snapshot(
    stats: Any,
    *,
    include: tuple[str, ...],
    sensitive: tuple[str, ...],
    max_depth: int,
    max_nodes: int,
    max_bytes: int,
) -> dict[str, Any]:
    """Return a bounded, redacted, JSON-compatible copy of *stats*.

    The nesting limit is also capped by the interpreter recursion limit, so a
    pathologically deep value raises :exc:`StatsSnapshotError` instead of a
    :exc:`RecursionError`.
    """
    builder = _SnapshotBuilder(
        include=include,
        sensitive=sensitive,
        # This implementation recurses once per nesting level, so a limit
        # above the stack allows is lowered rather than risking a crash.
        max_depth=min(max_depth, sys.getrecursionlimit() // 4),
        max_nodes=max_nodes,
        max_bytes=max_bytes,
    )
    return builder.build(stats)


def snapshot_settings(
    settings: BaseSettings,
) -> tuple[tuple[str, ...], tuple[str, ...], int, int, int]:
    """Return the validated snapshot settings of *settings*."""
    include = _pattern_tuple(
        settings.getlist("STATS_SNAPSHOT_INCLUDE"), "STATS_SNAPSHOT_INCLUDE"
    )
    extra_sensitive = _pattern_tuple(
        settings.getlist("STATS_SNAPSHOT_SENSITIVE_KEYS"),
        "STATS_SNAPSHOT_SENSITIVE_KEYS",
    )
    limits = (
        _positive(
            settings.getint("STATS_SNAPSHOT_MAX_DEPTH"), "STATS_SNAPSHOT_MAX_DEPTH"
        ),
        _positive(
            settings.getint("STATS_SNAPSHOT_MAX_NODES"), "STATS_SNAPSHOT_MAX_NODES"
        ),
        _positive(
            settings.getint("STATS_SNAPSHOT_MAX_BYTES"), "STATS_SNAPSHOT_MAX_BYTES"
        ),
    )
    return (include, extra_sensitive, *limits)


def snapshot_params(
    settings: BaseSettings,
    *,
    include: Iterable[Any] | None = None,
    sensitive_keys: Iterable[Any] | None = None,
    max_depth: int | None = None,
    max_nodes: int | None = None,
    max_bytes: int | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...], int, int, int]:
    """Combine the arguments of ``get_stats_snapshot()`` with *settings*.

    Explicit arguments take precedence over the settings; *sensitive_keys* is
    added to the sensitive-key setting rather than replacing it. The built-in
    sensitive-key patterns always apply.
    """
    (
        setting_include,
        setting_sensitive,
        setting_max_depth,
        setting_max_nodes,
        setting_max_bytes,
    ) = snapshot_settings(settings)
    return (
        setting_include if include is None else _pattern_tuple(include, "include"),
        _DEFAULT_SENSITIVE_KEYS
        + setting_sensitive
        + _pattern_tuple(sensitive_keys or (), "sensitive_keys"),
        setting_max_depth if max_depth is None else _positive(max_depth, "max_depth"),
        setting_max_nodes if max_nodes is None else _positive(max_nodes, "max_nodes"),
        setting_max_bytes if max_bytes is None else _positive(max_bytes, "max_bytes"),
    )


def _write_file(payload: bytes, target: Path) -> None:
    temp_path: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, temp_name = mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temp_path = Path(temp_name)
        with os.fdopen(descriptor, "wb") as temp_file:
            descriptor = None
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.replace(target)
        temp_path = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:  # pragma: no cover - depends on the filesystem
                # Leaving the temporary file behind is harmless, and the
                # previous snapshot was preserved either way.
                pass


def write_stats_snapshot(
    snapshot: Mapping[str, Any], path: str | os.PathLike[str]
) -> None:
    """Write *snapshot* to *path* atomically.

    The JSON text goes to a temporary file in the same directory, which is
    flushed and synced before replacing *path*. On failure *path* keeps its
    previous contents, if any, and the temporary file is removed.
    """
    payload = _canonical_json(snapshot).encode("utf-8")
    target = Path(path)
    try:
        _write_file(payload, target)
    except OSError as exc:
        raise StatsSnapshotError(
            f"Cannot write the stats snapshot to {os.fspath(target)!r}: "
            f"{type_name(exc)}",
            reason="write",
        ) from exc
