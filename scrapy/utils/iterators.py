from __future__ import annotations

import csv
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from io import StringIO
from typing import TYPE_CHECKING, Any, Literal, Self, cast, overload
from warnings import warn

from lxml import etree

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import Response, TextResponse
from scrapy.selector import Selector

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Protocol

    class _BinaryReadable(Protocol):
        def read(self, size: int, /) -> bytes: ...


logger = logging.getLogger(__name__)

_JSONL_BOM = b"\xef\xbb\xbf"
_JSONL_WHITESPACE = " \t\r"


def xmliter_lxml(
    obj: Response | str | bytes,
    nodename: str,
    namespace: str | None = None,
    prefix: str = "x",
) -> Iterator[Selector]:
    reader = _StreamReader(obj)
    tag = f"{{{namespace}}}{nodename}" if namespace else nodename
    iterable = etree.iterparse(
        reader,
        encoding=reader.encoding,
        events=("end", "start-ns"),
        resolve_entities=False,
        huge_tree=True,
    )
    selxpath = "//" + (f"{prefix}:{nodename}" if namespace else nodename)
    needs_namespace_resolution = not namespace and ":" in nodename
    if needs_namespace_resolution:
        prefix, nodename = nodename.split(":", maxsplit=1)
    for event, data in iterable:
        if event == "start-ns":
            assert isinstance(data, tuple)
            if needs_namespace_resolution:
                _prefix, _namespace = data
                if _prefix != prefix:
                    continue
                namespace = _namespace
                needs_namespace_resolution = False
                selxpath = f"//{prefix}:{nodename}"
                tag = f"{{{namespace}}}{nodename}"
            continue
        assert isinstance(data, etree._Element)
        node = data
        if node.tag != tag:
            continue
        nodetext = etree.tostring(node, encoding="unicode")
        node.clear()
        xs = Selector(text=nodetext, type="xml")
        if namespace:
            xs.register_namespace(prefix, namespace)
        yield xs.xpath(selxpath)[0]


class _StreamReader:
    def __init__(self, obj: Response | str | bytes):
        self._ptr: int = 0
        self._text: str | bytes
        if isinstance(obj, TextResponse):
            self._text, self.encoding = obj.body, obj.encoding
        elif isinstance(obj, Response):
            self._text, self.encoding = obj.body, "utf-8"
        else:
            self._text, self.encoding = obj, "utf-8"
        self._is_unicode: bool = isinstance(self._text, str)
        self._is_first_read: bool = True

    def read(self, n: int = 65535) -> bytes:
        method: Callable[[int], bytes] = (
            self._read_unicode if self._is_unicode else self._read_string
        )
        result = method(n)
        if self._is_first_read:
            self._is_first_read = False
            result = result.lstrip()
        return result

    def _read_string(self, n: int = 65535) -> bytes:
        s, e = self._ptr, self._ptr + n
        self._ptr = e
        return cast("bytes", self._text)[s:e]

    def _read_unicode(self, n: int = 65535) -> bytes:
        s, e = self._ptr, self._ptr + n
        self._ptr = e
        return cast("str", self._text)[s:e].encode()


def csviter(
    obj: Response | str | bytes,
    delimiter: str | None = None,
    headers: list[str] | None = None,
    encoding: str | None = None,
    quotechar: str | None = None,
) -> Iterator[dict[str, str]]:
    """Returns an iterator of dictionaries from the given csv object

    obj can be:
    - a Response object
    - a unicode string
    - a string encoded as utf-8

    delimiter is the character used to separate fields on the given obj.

    headers is an iterable that when provided offers the keys
    for the returned dictionaries, if not the first row is used.

    quotechar is the character used to enclosure fields on the given obj.
    """

    if encoding is not None:  # pragma: no cover
        warn(
            "The encoding argument of csviter() is ignored and will be removed"
            " in a future Scrapy version.",
            category=ScrapyDeprecationWarning,
            stacklevel=2,
        )

    lines = StringIO(_body_or_str(obj, unicode=True))

    kwargs: dict[str, Any] = {}
    if delimiter:
        kwargs["delimiter"] = delimiter
    if quotechar:
        kwargs["quotechar"] = quotechar
    csv_r = csv.reader(lines, **kwargs)

    if not headers:
        try:
            headers = next(csv_r)
        except StopIteration:
            return

    for row in csv_r:
        if len(row) != len(headers):
            logger.warning(
                "ignoring row %(csvlnum)d (length: %(csvrow)d, "
                "should be: %(csvheader)d)",
                {
                    "csvlnum": csv_r.line_num,
                    "csvrow": len(row),
                    "csvheader": len(headers),
                },
            )
            continue
        yield dict(zip(headers, row, strict=False))


@overload
def _body_or_str(obj: Response | str | bytes) -> str: ...


@overload
def _body_or_str(obj: Response | str | bytes, unicode: Literal[True]) -> str: ...


@overload
def _body_or_str(obj: Response | str | bytes, unicode: Literal[False]) -> bytes: ...


def _body_or_str(obj: Response | str | bytes, unicode: bool = True) -> str | bytes:
    expected_types = (Response, str, bytes)
    if not isinstance(obj, expected_types):
        expected_types_str = " or ".join(t.__name__ for t in expected_types)
        raise TypeError(
            f"Object {obj!r} must be {expected_types_str}, not {type(obj).__name__}"
        )
    if isinstance(obj, Response):
        if not unicode:
            return obj.body
        if isinstance(obj, TextResponse):
            return obj.text
        return obj.body.decode()
    if isinstance(obj, str):
        return obj if unicode else obj.encode()
    return obj.decode() if unicode else obj


class JsonlError(ValueError):
    """Raised by :func:`jsonl_iter` for an invalid JSON Lines row.

    .. versionadded:: VERSION

    *line* is the 1-based physical line number of the row. *exception* is the
    class of the underlying error, such as :exc:`json.JSONDecodeError` or
    :exc:`UnicodeDecodeError`, and the underlying error itself is available as
    the exception cause.
    """

    def __init__(
        self,
        message: str,
        *,
        line: int,
        exception: type[BaseException],
    ) -> None:
        super().__init__(message)
        self.line = line
        self.exception = exception


class JsonlBudgetError(JsonlError):
    """Raised by :func:`jsonl_iter` when a row or the input exceeds a budget.

    .. versionadded:: VERSION
    """


@dataclass(frozen=True, slots=True)
class JsonlRejection:
    """A row skipped by :func:`jsonl_iter` with ``skip_invalid=True``.

    .. versionadded:: VERSION

    It holds only the 1-based physical *line* number and the *exception*
    class that would have been raised, never the row contents.
    """

    line: int
    exception: type[BaseException]


_JSONL_END = object()


class _RejectedRow(Exception):
    """Internal signal for an invalid line, converted to a rejection or an
    error by :meth:`JsonlIterator._next_row`."""

    def __init__(
        self,
        message: str,
        line: int,
        cls: type[JsonlError],
        rejection: type[BaseException] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.cls = cls
        self.rejection = cls if rejection is None else rejection
        self.cause = cause

    def error(self) -> JsonlError:
        return self.cls(self.message, line=self.line, exception=self.rejection)


class JsonlIterator(Iterator[dict[str, Any]]):
    """Iterator returned by :func:`jsonl_iter`.

    .. versionadded:: VERSION

    Iterate it to get the JSON objects, read ``rejections`` to get the rows
    skipped with ``skip_invalid=True``, and call :meth:`close` or use it as a
    context manager to release its buffers early. Closing it never closes the
    caller-provided stream.
    """

    def __init__(
        self,
        obj: Response | bytes | _BinaryReadable,
        *,
        chunk_size: int,
        skip_invalid: bool,
        max_encoded_bytes: int | None,
        max_line_bytes: int,
        max_depth: int,
    ) -> None:
        self.rejections: list[JsonlRejection] = []
        self._chunk_size = chunk_size
        self._skip_invalid = skip_invalid
        self._max_encoded_bytes = max_encoded_bytes
        self._max_line_bytes = max_line_bytes
        self._max_depth = max_depth
        self._stream: _BinaryReadable | None
        if isinstance(obj, Response):
            self._data: bytes | None = obj.body
            self._stream = None
        elif isinstance(obj, bytes):
            self._data = obj
            self._stream = None
        else:
            self._data = None
            self._stream = obj
        self._data_pos = 0
        self._chunk = b""
        self._chunk_pos = 0
        self._eof = False
        self._input_bytes = 0
        self._line_number = 1
        self._closed = False

    def __iter__(self) -> JsonlIterator:
        return self

    def __next__(self) -> dict[str, Any]:
        if self._closed:
            raise StopIteration
        try:
            row = self._next_row()
        except BaseException:
            # Release the buffers on parsing errors too, not only on close or
            # exhaustion.
            self._release()
            raise
        if row is _JSONL_END:
            self._release()
            raise StopIteration
        return cast("dict[str, Any]", row)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the internal buffers. The input stream is left open."""
        self._release()

    def _release(self) -> None:
        self._closed = True
        self._chunk = b""
        self._chunk_pos = 0
        self._data = None
        self._stream = None

    def _read_source(self, size: int) -> bytes:
        if self._data is not None:
            chunk = self._data[self._data_pos : self._data_pos + size]
            self._data_pos += len(chunk)
            return chunk
        assert self._stream is not None
        chunk = self._stream.read(size)
        if not isinstance(chunk, bytes):
            raise TypeError(
                f"JSON Lines input read() must return bytes, not {type(chunk).__name__}"
            )
        return chunk

    def _read_more(self) -> bytes:
        if self._eof:
            return b""
        size = self._chunk_size
        if self._max_encoded_bytes is not None:
            remaining = self._max_encoded_bytes - self._input_bytes
            if remaining <= 0:
                # Read one more byte only to tell an input that ends exactly
                # at the budget from one that continues past it.
                if self._read_source(1):
                    raise JsonlBudgetError(
                        f"JSON Lines input exceeds {self._max_encoded_bytes} "
                        f"encoded bytes at physical line {self._line_number}",
                        line=self._line_number,
                        exception=JsonlBudgetError,
                    )
                self._eof = True
                return b""
            size = min(size, remaining)
        chunk = self._read_source(size)
        self._input_bytes += len(chunk)
        if not chunk:
            self._eof = True
        return chunk

    def _read_raw_line(self) -> tuple[int, bytes | None] | None:
        r"""Return the next physical line and its number, or ``None`` at the
        end of the input. An overlong line is returned as ``(number, None)``
        after being scanned to its end without being kept in memory, or,
        unless invalid rows are being skipped, raises :exc:`JsonlBudgetError`
        as soon as the limit is exceeded.

        The line terminator is not included, and neither is the carriage
        return of a ``\r\n`` terminator.
        """
        line_number = self._line_number
        parts: list[bytes] = []
        size = 0
        # One byte beyond the limit is buffered so that a trailing carriage
        # return can still be recognized as part of the terminator.
        limit = self._max_line_bytes + 1
        oversized = False
        while True:
            if self._chunk_pos >= len(self._chunk):
                if not self._eof:
                    self._chunk = self._read_more()
                    self._chunk_pos = 0
                    continue
                if oversized:
                    self._line_number += 1
                    return line_number, None
                if not parts:
                    return None
                self._line_number += 1
                return line_number, self._check_line_size(b"".join(parts))
            newline = self._chunk.find(b"\n", self._chunk_pos)
            end = len(self._chunk) if newline < 0 else newline
            length = end - self._chunk_pos
            if not oversized and size + length > limit:
                if not self._skip_invalid:
                    raise JsonlBudgetError(
                        f"JSON Lines row on physical line {line_number} "
                        f"exceeds {self._max_line_bytes} bytes",
                        line=line_number,
                        exception=JsonlBudgetError,
                    )
                oversized = True
                parts.clear()
            if not oversized:
                parts.append(self._chunk[self._chunk_pos : end])
                size += length
            if newline < 0:
                self._chunk_pos = len(self._chunk)
                continue
            self._chunk_pos = newline + 1
            self._line_number += 1
            if oversized:
                return line_number, None
            return line_number, self._check_line_size(b"".join(parts))

    def _check_line_size(self, raw: bytes) -> bytes | None:
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        return None if len(raw) > self._max_line_bytes else raw

    def _parse_line(self, line_number: int, raw: bytes) -> dict[str, Any] | None:
        """Parse one line, returning its object or ``None`` for a blank line.

        Any invalid line raises :exc:`_RejectedRow`, which carries the error
        to report if the row is not being skipped.
        """
        if line_number == 1:
            raw = raw.removeprefix(_JSONL_BOM)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as cause:
            raise _RejectedRow(
                f"Invalid UTF-8 on physical line {line_number}: {cause}",
                line_number,
                JsonlError,
                UnicodeDecodeError,
                cause,
            ) from cause
        text = text.strip(_JSONL_WHITESPACE)
        if not text:
            return None
        # Check the nesting depth before parsing, so that a pathologically
        # nested row is rejected without reaching the recursive parser.
        depth = _json_depth(text)
        if depth > self._max_depth:
            raise _RejectedRow(
                f"JSON value on physical line {line_number} is nested "
                f"{depth} levels deep, over the limit of {self._max_depth}",
                line_number,
                JsonlBudgetError,
            )
        try:
            value = json.loads(text)
        except RecursionError as cause:
            raise _RejectedRow(
                f"JSON value on physical line {line_number} is too deeply "
                f"nested to parse within the limit of {self._max_depth}",
                line_number,
                JsonlBudgetError,
                cause=cause,
            ) from cause
        except ValueError as cause:
            raise _RejectedRow(
                f"Invalid JSON on physical line {line_number}: {cause}",
                line_number,
                JsonlError,
                json.JSONDecodeError,
                cause,
            ) from cause
        if not isinstance(value, dict):
            not_an_object = JsonlError(
                f"expected a JSON object, got {type(value).__name__}",
                line=line_number,
                exception=JsonlError,
            )
            raise _RejectedRow(
                f"JSON value on physical line {line_number} is "
                f"{type(value).__name__}, not an object",
                line_number,
                JsonlError,
                cause=not_an_object,
            ) from not_an_object
        return value

    def _next_row(self) -> Any:
        while True:
            raw_line = self._read_raw_line()
            if raw_line is None:
                return _JSONL_END
            line_number, raw = raw_line
            try:
                if raw is None:
                    raise _RejectedRow(
                        f"JSON Lines row on physical line {line_number} "
                        f"exceeds {self._max_line_bytes} bytes",
                        line_number,
                        JsonlBudgetError,
                    )
                value = self._parse_line(line_number, raw)
            except _RejectedRow as rejected:
                if self._skip_invalid:
                    self.rejections.append(
                        JsonlRejection(rejected.line, rejected.rejection)
                    )
                    continue
                raise rejected.error() from rejected.cause
            if value is not None:
                return value


def _json_depth(text: str) -> int:
    """Return the maximum nesting depth of JSON containers in *text*."""
    depth = 0
    max_depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            max_depth = max(max_depth, depth)
        elif char in "]}":
            depth -= 1
    return max_depth


def _check_int(name: str, value: object, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def jsonl_iter(
    obj: Response | bytes | _BinaryReadable,
    *,
    chunk_size: int = 65536,
    skip_invalid: bool = False,
    max_encoded_bytes: int | None = None,
    max_line_bytes: int = 1024 * 1024,
    max_depth: int = 100,
) -> JsonlIterator:
    """Return an iterator of JSON objects from a JSON Lines document.

    .. versionadded:: VERSION

    *obj* can be a :class:`~scrapy.http.Response` or a
    :class:`~scrapy.http.TextResponse`, its body as :class:`bytes`, or a
    caller-owned binary stream that only needs to have a ``read(size)``
    method. The stream is never closed by the iterator, and a short read is
    not treated as the end of the input: iteration stops only when ``read()``
    returns ``b""``.

    The input is read in chunks of at most *chunk_size* bytes, and a physical
    line may span any number of chunks. Each line is decoded strictly as
    UTF-8; invalid bytes, invalid JSON and non-object top-level values raise
    :exc:`JsonlError` with the 1-based physical line number. A leading UTF-8
    byte order mark is accepted on the first line, CRLF line endings and a
    final line without a line ending are allowed, and blank lines are
    skipped. None of these count as rows.

    With *skip_invalid* set to ``True``, such rows are skipped instead, and
    a :class:`JsonlRejection` holding only the line number and the exception
    class is appended to the returned iterator's ``rejections`` list. Invalid
    UTF-8 and invalid JSON are therefore detected and discarded line by line
    without replaying or seeking the input.

    *max_line_bytes* bounds the encoded size of a single line, excluding its
    line terminator; *max_encoded_bytes* bounds the encoded size of the whole
    input and, when set, is always an error, even with *skip_invalid*;
    *max_depth* bounds how deeply JSON objects and arrays may nest, and is
    checked before parsing, so a deeply nested line never reaches the
    recursive JSON parser. A line over *max_line_bytes* or *max_depth* is
    reported as a :exc:`JsonlBudgetError` before it is yielded, and with
    *skip_invalid* an overlong line is scanned to its end in bounded chunks
    instead of being kept in memory. Set *max_encoded_bytes* to ``None`` to
    read inputs of any size.

    Call :meth:`~JsonlIterator.close` or use the iterator as a context
    manager to release its internal buffers before the input is exhausted.
    Rows already returned are never returned again.

    .. code-block:: python

        from scrapy.utils.iterators import jsonl_iter

        for row in jsonl_iter(response, max_line_bytes=65536):
            process(row)
    """
    if not isinstance(obj, (Response, bytes)) and not callable(
        getattr(obj, "read", None)
    ):
        raise TypeError(
            f"Object {obj!r} must be a Response, bytes or a binary stream "
            f"with a read() method, not {type(obj).__name__}"
        )
    if not isinstance(skip_invalid, bool):
        raise ValueError("skip_invalid must be a bool")
    if max_encoded_bytes is not None:
        max_encoded_bytes = _check_int(
            "max_encoded_bytes", max_encoded_bytes, minimum=0
        )
    return JsonlIterator(
        obj,
        chunk_size=_check_int("chunk_size", chunk_size, minimum=1),
        skip_invalid=skip_invalid,
        max_encoded_bytes=max_encoded_bytes,
        max_line_bytes=_check_int("max_line_bytes", max_line_bytes, minimum=1),
        max_depth=_check_int("max_depth", max_depth, minimum=1),
    )
