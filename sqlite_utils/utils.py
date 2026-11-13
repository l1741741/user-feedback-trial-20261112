import base64
import codecs
import contextlib
import csv
import enum
import hashlib
import importlib
import io
import itertools
import json
import os
import sys
from collections.abc import Callable, Generator, Iterable, Iterator
from typing import (
    TYPE_CHECKING,
    Any,
    BinaryIO,
    Generic,
    NoReturn,
    TypeVar,
    Union,
    cast,
)

import click

from . import recipes

if TYPE_CHECKING:
    import sqlite3
    from sqlite3 import dbapi2

    OperationalError = dbapi2.OperationalError
else:
    try:
        sqlite3 = importlib.import_module("pysqlite3")
        dbapi2 = importlib.import_module("pysqlite3.dbapi2")
        OperationalError = dbapi2.OperationalError
    except ImportError:
        import sqlite3  # noqa: F401
        from sqlite3 import dbapi2

        OperationalError = dbapi2.OperationalError


SPATIALITE_PATHS = (
    "/usr/lib/x86_64-linux-gnu/mod_spatialite.so",
    "/usr/lib/aarch64-linux-gnu/mod_spatialite.so",
    "/usr/local/lib/mod_spatialite.dylib",
    "/usr/local/lib/mod_spatialite.so",
    "/opt/homebrew/lib/mod_spatialite.dylib",
)

# Mainly so we can restore it if needed in the tests:
ORIGINAL_CSV_FIELD_SIZE_LIMIT = csv.field_size_limit()

# Type alias for row dictionaries - values can be various SQLite-compatible types
RowValue = None | int | float | str | bytes | bool | list[str]
Row = dict[str, RowValue]

T = TypeVar("T")


class ANY:
    """Marker type for an SQLite ``ANY`` column."""


class _CloseableIterator(Iterator[Row]):
    """Iterator wrapper that closes a file when iteration is complete."""

    def __init__(self, iterator: Iterator[Row], closeable: io.IOBase) -> None:
        self._iterator = iterator
        self._closeable = closeable

    def __iter__(self) -> "_CloseableIterator":
        return self

    def __next__(self) -> Row:
        try:
            return next(self._iterator)
        except StopIteration:
            self._closeable.close()
            raise

    def close(self) -> None:
        self._closeable.close()


def maximize_csv_field_size_limit() -> None:
    """
    Increase the CSV field size limit to the maximum possible.
    """
    # https://stackoverflow.com/a/15063941
    field_size_limit = sys.maxsize

    while True:
        try:
            csv.field_size_limit(field_size_limit)
            break
        except OverflowError:
            field_size_limit = int(field_size_limit / 10)


def find_spatialite() -> str | None:
    """
    The ``find_spatialite()`` function searches for the `SpatiaLite <https://www.gaia-gis.it/fossil/libspatialite/index>`__
    SQLite extension in some common places. It returns a string path to the location, or ``None`` if SpatiaLite was not found.

    You can use it in code like this:

    .. code-block:: python

        from sqlite_utils import Database
        from sqlite_utils.utils import find_spatialite

        db = Database("mydb.db")
        spatialite = find_spatialite()
        if spatialite:
            db.conn.enable_load_extension(True)
            db.conn.load_extension(spatialite)

        # or use with db.init_spatialite like this
        db.init_spatialite(find_spatialite())

    """
    for path in SPATIALITE_PATHS:
        if os.path.exists(path):
            return path
    return None


def suggest_column_types(
    records: Iterable[dict[str, Any]],
) -> dict[str, type]:
    all_column_types: dict[str, set[type]] = {}
    for record in records:
        for key, value in record.items():
            all_column_types.setdefault(key, set()).add(type(value))
    return types_for_column_types(all_column_types)


def types_for_column_types(
    all_column_types: dict[str, set[type]],
) -> dict[str, type]:
    column_types: dict[str, type] = {}
    for key, types in all_column_types.items():
        # Ignore null values if at least one other type present:
        if len(types) > 1:
            types.discard(None.__class__)
        t: type
        if {None.__class__} == types:
            t = str
        elif len(types) == 1:
            t = next(iter(types))
            # But if it's a subclass of list / tuple / dict, use str
            # instead as we will be storing it as JSON in the table
            for superclass in (list, tuple, dict):
                if issubclass(t, superclass):
                    t = str
        elif {int, bool}.issuperset(types):
            t = int
        elif {int, float, bool}.issuperset(types):
            t = float
        elif {bytes, str}.issuperset(types):
            t = bytes
        else:
            t = str
        column_types[key] = t
    return column_types


def column_affinity(column_type: str) -> type:
    # Implementation of SQLite affinity rules from
    # https://www.sqlite.org/datatype3.html#determination_of_column_affinity
    assert isinstance(column_type, str)
    column_type = column_type.upper().strip()
    if column_type == "":
        return str  # We differ from spec, which says it should be BLOB
    if "INT" in column_type:
        return int
    if "CHAR" in column_type or "CLOB" in column_type or "TEXT" in column_type:
        return str
    if "BLOB" in column_type:
        return bytes
    if "REAL" in column_type or "FLOA" in column_type or "DOUB" in column_type:
        return float
    if column_type == "ANY":
        return ANY
    # Default is 'NUMERIC', which we currently also treat as float
    return float


def decode_base64_values(doc: dict[str, Any]) -> dict[str, Any]:
    # Looks for '{"$base64": true..., "encoded": ...}' values and decodes them
    to_fix = [
        k
        for k in doc
        if isinstance(doc[k], dict)
        and cast(dict, doc[k]).get("$base64") is True
        and "encoded" in cast(dict, doc[k])
    ]
    if not to_fix:
        return doc
    return dict(
        doc, **{k: base64.b64decode(cast(dict, doc[k])["encoded"]) for k in to_fix}
    )


class UpdateWrapper:
    def __init__(self, wrapped: io.IOBase, update: Callable[[int], None]) -> None:
        self._wrapped = wrapped
        self._update = update

    def __iter__(self) -> Iterator[bytes]:
        for line in self._wrapped:
            self._update(len(line))
            yield line

    def read(self, size: int = -1) -> bytes:
        data = self._wrapped.read(size)
        self._update(len(data))
        return data


@contextlib.contextmanager
def file_progress(
    file: io.IOBase, silent: bool = False, **kwargs: object
) -> Generator[Union[io.IOBase, "UpdateWrapper"], None, None]:
    if silent:
        yield file
        return
    # file.fileno() throws an exception in our test suite
    try:
        fileno = file.fileno()
    except io.UnsupportedOperation:
        yield file
        return
    if fileno == 0:  # 0 means stdin
        yield file
    else:
        file_length = os.path.getsize(file.name)  # type: ignore
        with click.progressbar(length=file_length, **kwargs) as bar:  # type: ignore
            yield UpdateWrapper(file, bar.update)


class Format(enum.Enum):
    CSV = 1
    TSV = 2
    JSON = 3
    NL = 4


class RowsFromFileError(Exception):
    pass


class RowsFromFileBadJSON(RowsFromFileError):
    pass


class RowError(Exception):
    pass


_JSON_WHITESPACE = " \t\r\n"
_ASCII_WHITESPACE = " \t\r\n\v\f"
_JSON_STREAM_END = object()


class _JsonRowIterator(Iterator[Row]):
    """
    Lazy, forward-only JSON row iterator.

    Reads bytes in chunks of at most ``chunk_size`` and parses one object
    at a time, so an error in a later record does not discard earlier rows.
    """

    def __init__(
        self,
        fp: BinaryIO,
        *,
        newline_delimited: bool,
        encoding: str,
        chunk_size: int,
        close_fp: bool,
    ) -> None:
        self._fp = fp
        self._newline_delimited = bool(newline_delimited)
        self._encoding = encoding
        self._chunk_size = chunk_size
        self._close_fp = bool(close_fp)
        # Constructing the decoder does not read from fp.  An unknown
        # encoding raises here, before any read or close.
        self._decoder = codecs.getincrementaldecoder(encoding)()
        self._pending_error: UnicodeError | None = None
        self._text = ""
        self._index = 0
        self._eof = False
        self._rows_parsed = 0
        self._ndjson_record = 0
        self._line_number = 1
        self._mode: str | None = None
        self._array_state = ""
        self._closed = False
        self._finished = False
        self._input_closed = False

    # Iterator / context manager protocol

    def __iter__(self) -> "_JsonRowIterator":
        return self

    def __next__(self) -> Row:
        if self._closed or self._finished:
            raise StopIteration
        try:
            result = self._next_row()
        except BaseException:
            self._finished = True
            try:
                self._close_input()
            except Exception:
                pass
            raise
        if result is _JSON_STREAM_END:
            self._finished = True
            self._close_input()
            raise StopIteration
        return cast(Row, result)

    def __enter__(self) -> "_JsonRowIterator":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._finished = True
        self._close_input()

    def _close_input(self) -> None:
        # Exactly one close() call on the caller's file, ever.
        if not self._close_fp or self._input_closed:
            return
        self._input_closed = True
        self._fp.close()

    # Low-level chunked reading

    def _decode(self, data: bytes) -> str:
        """
        Incrementally decode ``data``.

        If the bytes contain an invalid sequence, every character before it
        is returned normally and the error is remembered in
        ``self._pending_error`` so it is raised only once the parser has
        consumed that valid text - an invalid byte in a later record must not
        discard earlier records that shared the same chunk.
        """
        snapshot = self._decoder.getstate()
        try:
            text = self._decoder.decode(data)
        except UnicodeError as ex:
            text = self._recover_after_decode_error(data, snapshot, ex)
        if not isinstance(text, str):
            raise TypeError(f"encoding {self._encoding!r} does not produce text")
        return text

    def _recover_after_decode_error(
        self, data: bytes, snapshot: Any, error: UnicodeError
    ) -> str:
        if not isinstance(error, UnicodeDecodeError):
            # e.g. a codec that requires a BOM and did not find one - nothing
            # in this chunk can be decoded.
            self._pending_error = error
            return ""
        pending = snapshot[0]
        # error.start is an offset into error.object, which includes any
        # bytes the decoder was buffering before this call.
        valid_length = max(0, error.start - len(pending))
        self._decoder = self._fresh_decoder()
        self._decoder.setstate(snapshot)
        text = self._decoder.decode(data[:valid_length])
        self._pending_error = error
        return text

    def _fresh_decoder(self) -> Any:
        return codecs.getincrementaldecoder(self._encoding)()

    def _raise_pending_error(self) -> None:
        error = self._pending_error
        self._pending_error = None
        self._bad(f"invalid {self._encoding} encoded data: {error}", error)

    def _read_chunk(self) -> bool:
        """
        Read and decode one chunk, returning True if text is available.

        Every read is bounded by ``chunk_size``.  A short read or a chunk
        that decodes to nothing (a split multi-byte character) never
        terminates the stream: only ``b""`` does.
        """
        while True:
            if self._pending_error is not None:
                self._raise_pending_error()
            if self._eof:
                return False
            data = self._fp.read(self._chunk_size)
            if isinstance(data, str):
                raise TypeError("iter_json_rows() requires a binary file-like object")
            if data == b"":
                self._eof = True
                try:
                    tail = self._decoder.decode(b"", final=True)
                except UnicodeError as ex:
                    self._bad(f"invalid {self._encoding} encoded data: {ex}", ex)
                if isinstance(tail, str) and tail:
                    self._text = tail
                    self._index = 0
                    return True
                continue
            text = self._decode(data)
            if text:
                self._text = text
                self._index = 0
                return True
            # Partial multi-byte sequence - keep reading.

    def _advance(self) -> bool:
        """Make sure a character is available, reading chunks as needed."""
        while self._index >= len(self._text):
            if self._eof:
                return False
            if not self._read_chunk():
                return False
        return True

    def _peek_char(self) -> str | None:
        if not self._advance():
            return None
        return self._text[self._index]

    def _take_char(self) -> str:
        char = self._text[self._index]
        self._index += 1
        return char

    def _skip_whitespace(self) -> bool:
        """Skip JSON whitespace; return False if EOF is reached."""
        while True:
            if not self._advance():
                return False
            text = self._text
            index = self._index
            length = len(text)
            while index < length and text[index] in _JSON_WHITESPACE:
                index += 1
            self._index = index
            if index < length:
                return True

    # Error reporting

    def _error_context(self, record: int | None = None, line: int | None = None) -> str:
        if self._newline_delimited:
            record_number = record if record is not None else self._ndjson_record + 1
            line_number = line if line is not None else self._line_number
            return f"record {record_number} (line {line_number})"
        record_number = record if record is not None else self._rows_parsed + 1
        return f"record {record_number}"

    def _bad(
        self,
        message: str,
        cause: BaseException | None = None,
        *,
        record: int | None = None,
        line: int | None = None,
    ) -> NoReturn:
        context = self._error_context(record, line)
        if cause is not None:
            raise RowsFromFileBadJSON(f"{context}: {message}") from cause
        raise RowsFromFileBadJSON(f"{context}: {message}")

    # Normal JSON mode

    def _next_row(self) -> Any:
        if self._newline_delimited:
            return self._next_ndjson_row()
        return self._next_json_row()

    def _next_json_row(self) -> Any:
        if self._mode is None:
            if not self._skip_whitespace():
                self._bad("expected a JSON object but the input was empty")
            char = self._peek_char()
            if char == "{":
                self._mode = "single"
            elif char == "[":
                self._mode = "array"
                self._array_state = "first"
                self._take_char()
            else:
                self._bad(f"expected a JSON object or array, found {char!r}")

        if self._mode == "single":
            self._mode = "single_done"
            return self._parse_object()

        if self._mode == "single_done":
            self._require_end_of_input()
            return _JSON_STREAM_END

        # Array mode
        while True:
            if self._array_state == "first":
                if not self._skip_whitespace():
                    self._bad("unexpected end of input, expected ']' or an object")
                char = self._peek_char()
                if char == "]":
                    self._take_char()
                    self._array_state = "done"
                    continue
                if char != "{":
                    self._bad(f"expected a JSON object, found {char!r}")
                self._array_state = "after_value"
                return self._parse_object()

            if self._array_state == "value":
                if not self._skip_whitespace():
                    self._bad("unexpected end of input, expected a JSON object")
                char = self._peek_char()
                if char == "]":
                    self._bad("trailing comma before ']'")
                if char != "{":
                    self._bad(f"expected a JSON object, found {char!r}")
                self._array_state = "after_value"
                return self._parse_object()

            if self._array_state == "after_value":
                if not self._skip_whitespace():
                    self._bad("unexpected end of input, expected ',' or ']'")
                char = self._peek_char()
                if char == ",":
                    self._take_char()
                    self._array_state = "value"
                    continue
                if char == "]":
                    self._take_char()
                    self._array_state = "done"
                    continue
                self._bad(f"expected ',' or ']', found {char!r}")

            # self._array_state == "done"
            self._require_end_of_input()
            return _JSON_STREAM_END

    def _require_end_of_input(self) -> None:
        if not self._skip_whitespace():
            return
        char = self._peek_char()
        self._bad(f"unexpected trailing data after JSON value, found {char!r}")

    def _parse_object(self) -> Row:
        char = self._peek_char()
        if char != "{":
            self._bad(f"expected a JSON object, found {char!r}")
        self._take_char()
        parts = ["{"]
        depth = 1
        in_string = False
        escaped = False
        while depth:
            if not self._advance():
                if in_string:
                    self._bad("unexpected end of input inside a JSON string")
                self._bad("unexpected end of input while parsing a JSON object")
            text = self._text
            start = self._index
            index = start
            length = len(text)
            while index < length and depth:
                current = text[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif current == "\\":
                        escaped = True
                    elif current == '"':
                        in_string = False
                else:
                    if current == '"':
                        in_string = True
                    elif current in "{[":
                        depth += 1
                    elif current in "}]":
                        depth -= 1
                index += 1
            parts.append(text[start:index])
            self._index = index
        record_text = "".join(parts)
        try:
            value = json.loads(record_text)
        except ValueError as ex:
            self._bad(f"invalid JSON: {ex}", ex)
        if not isinstance(value, dict):
            self._bad("expected a JSON object")
        self._rows_parsed += 1
        return cast(Row, value)

    # Newline-delimited mode

    def _read_line(self) -> tuple[int, str] | None:
        """Read one physical line, tracking 1-based line numbers."""
        parts: list[str] = []
        while True:
            if not self._advance():
                if parts:
                    return self._line_number, "".join(parts)
                return None
            text = self._text
            start = self._index
            newline = text.find("\n", start)
            if newline == -1:
                parts.append(text[start:])
                self._index = len(text)
                continue
            parts.append(text[start:newline])
            self._index = newline + 1
            line_number = self._line_number
            self._line_number += 1
            return line_number, "".join(parts)

    def _next_ndjson_row(self) -> Any:
        while True:
            line_info = self._read_line()
            if line_info is None:
                return _JSON_STREAM_END
            line_number, line = line_info
            stripped = line.strip(_JSON_WHITESPACE)
            if not stripped:
                # Blank lines are ignored and don't increment the record number.
                continue
            record_number = self._ndjson_record + 1
            try:
                value = json.loads(stripped)
            except ValueError as ex:
                self._bad(
                    f"invalid JSON: {ex}",
                    ex,
                    record=record_number,
                    line=line_number,
                )
            if not isinstance(value, dict):
                self._bad(
                    "expected a JSON object",
                    record=record_number,
                    line=line_number,
                )
            self._ndjson_record += 1
            return cast(Row, value)


def _validate_chunk_size(chunk_size: Any) -> int:
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
        raise ValueError("chunk_size must be an integer between 1 and 1048576")
    if not 1 <= chunk_size <= 1048576:
        raise ValueError("chunk_size must be an integer between 1 and 1048576")
    return chunk_size


def iter_json_rows(
    fp: BinaryIO,
    *,
    newline_delimited: bool = False,
    encoding: str = "utf-8-sig",
    chunk_size: int = 65536,
    close_fp: bool = False,
) -> Iterator[Row]:
    """
    Lazily parse JSON objects from a binary file-like object.

    Only ``fp.read()`` and ``fp.close()`` are used: the input does not
    need to support ``seek()``, ``peek()``, ``readinto()``, ``fileno()`` or
    report its length.  At most one incomplete record, the incremental
    decoder state and one chunk are held in memory, and the first row is
    returned as soon as its closing ``}`` has been read.

    In the default mode the document must be either a single JSON object
    or an array of JSON objects.  With ``newline_delimited=True`` each
    non-blank physical line must contain exactly one JSON object.

    Invalid JSON raises :class:`RowsFromFileBadJSON`, whose message
    includes the 1-based record number (and, for newline-delimited input,
    the physical line number).  Rows parsed before the bad record are
    still returned by earlier ``next()`` calls.  Invalid encoded bytes are
    reported the same way, with the original ``UnicodeDecodeError``
    chained.  An ``OSError`` raised by ``fp.read()`` is propagated
    unchanged.

    The iterator can be used as a context manager and has an explicit
    ``close()`` method.  With ``close_fp=True`` the caller's file is
    closed exactly once, whether iteration finishes, fails, is closed
    early or the ``with`` block exits with an exception.  With the
    default ``close_fp=False`` the caller remains responsible for
    closing it.

    .. code-block:: python

        from sqlite_utils.utils import iter_json_rows

        with iter_json_rows(fp, chunk_size=4096) as rows:
            for row in rows:
                print(row)

    :param fp: binary file-like object
    :param newline_delimited: parse one JSON object per physical line
    :param encoding: encoding for incremental decoding (default allows a
      leading UTF-8 BOM)
    :param chunk_size: maximum bytes requested per ``read()`` call,
      between 1 and 1048576
    :param close_fp: close ``fp`` when iteration ends, is closed or fails
    """
    _validate_chunk_size(chunk_size)
    return _JsonRowIterator(
        fp,
        newline_delimited=newline_delimited,
        encoding=encoding,
        chunk_size=chunk_size,
        close_fp=close_fp,
    )


def _extra_key_strategy(
    reader: Iterable[dict[str | None, object]],
    ignore_extras: bool | None = False,
    extras_key: str | None = None,
) -> Iterable[Row]:
    # Logic for handling CSV rows with more values than there are headings
    for row in reader:
        # DictReader adds a 'None' key with extra row values
        if None not in row:
            yield cast(Row, row)
        elif ignore_extras:
            # ignoring row.pop(none) because of this issue:
            # https://github.com/simonw/sqlite-utils/issues/440#issuecomment-1155358637
            row.pop(None)
            yield cast(Row, row)
        elif not extras_key:
            extras = row.pop(None)
            raise RowError(f"Row {row} contained these extra values: {extras}")
        else:
            extras_value = row.pop(None)
            row_out = cast(Row, row)
            row_out[extras_key] = cast(RowValue, extras_value)
            yield row_out


class _PrefixedStream(io.RawIOBase):
    """
    Wrap a forward-only binary stream with a prefix of bytes already read.

    Used by ``rows_from_file()`` when it has to sniff the format before
    streaming JSON rows, so no data has to be copied or re-read.
    """

    def __init__(self, fp: BinaryIO, prefix: bytes) -> None:
        self._fp = fp
        self._prefix = prefix

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            # Not used by the streaming reader, but behave like read-all.
            data = self._prefix + self._fp.read()
            self._prefix = b""
            return data
        if self._prefix:
            data = self._prefix[:size]
            self._prefix = self._prefix[size:]
            return data
        return self._fp.read(size)

    def readinto(self, buffer: Any) -> int:
        if self._prefix:
            length = min(len(self._prefix), len(buffer))
            buffer[:length] = self._prefix[:length]
            self._prefix = self._prefix[length:]
            return length
        data = self._fp.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._fp.close()
        finally:
            super().close()


def rows_from_file(
    fp: BinaryIO,
    format: Format | None = None,
    dialect: type[csv.Dialect] | None = None,
    encoding: str | None = None,
    ignore_extras: bool | None = False,
    extras_key: str | None = None,
    stream_json: bool = False,
    stream_json_chunk_size: int = 65536,
) -> tuple[Iterable[Row], Format]:
    """
    Load a sequence of dictionaries from a file-like object containing one of four different formats.

    .. code-block:: python

        from sqlite_utils.utils import rows_from_file
        import io

        rows, format = rows_from_file(io.StringIO("id,name\\n1,Cleo")))
        print(list(rows), format)
        # Outputs [{'id': '1', 'name': 'Cleo'}] Format.CSV

    This defaults to attempting to automatically detect the format of the data, or you can pass in an
    explicit format using the format= option.

    Returns a tuple of ``(rows_generator, format_used)`` where ``rows_generator`` can be iterated over
    to return dictionaries, while ``format_used`` is a value from the ``sqlite_utils.utils.Format`` enum:

    .. code-block:: python

        class Format(enum.Enum):
            CSV = 1
            TSV = 2
            JSON = 3
            NL = 4

    If a CSV or TSV file includes rows with more fields than are declared in the header a
    ``sqlite_utils.utils.RowError`` exception will be raised when you loop over the generator.

    You can instead ignore the extra data by passing ``ignore_extras=True``.

    Or pass ``extras_key="rest"`` to put those additional values in a list in a key called ``rest``.

    :param fp: a file-like object containing binary data
    :param format: the format to use - omit this to detect the format
    :param dialect: the CSV dialect to use - omit this to detect the dialect
    :param encoding: the character encoding to use when reading CSV/TSV data
    :param ignore_extras: ignore any extra fields on rows
    :param extras_key: put any extra fields in a list with this key
    :param stream_json: lazily stream JSON and newline-delimited JSON rows
    :param stream_json_chunk_size: maximum bytes to read at a time when
      streaming JSON
    """
    if ignore_extras and extras_key:
        raise ValueError("Cannot use ignore_extras= and extras_key= together")
    if stream_json and format in (None, Format.JSON, Format.NL):
        _validate_chunk_size(stream_json_chunk_size)

    def _stream_rows(
        format: Format,
        source: BinaryIO,
        close_fp: bool,
        encoding: str | None = None,
    ) -> Iterator[Row]:
        return iter_json_rows(
            source,
            newline_delimited=format == Format.NL,
            encoding=encoding or "utf-8-sig",
            chunk_size=stream_json_chunk_size,
            close_fp=close_fp,
        )

    use_encoding: str = encoding or "utf-8-sig"

    if format is None and stream_json:
        # Mirror the non-streaming detector: look at up to the first 2048
        # bytes, strip ASCII whitespace and inspect the first byte.  The
        # window always starts at byte 0 and is contiguous, so a BOM or a
        # multi-byte character split across reads survives replay.
        window = bytearray()
        decoder = codecs.getincrementaldecoder(use_encoding)()
        first_nonspace: str | None = None
        decode_failed = False
        while not decode_failed and first_nonspace is None and len(window) < 2048:
            data = fp.read(min(stream_json_chunk_size, 2048 - len(window)))
            if isinstance(data, str):
                raise TypeError(
                    "rows_from_file() requires a file-like object opened in binary mode"
                )
            if data == b"":
                break
            window.extend(data)
            try:
                text = decoder.decode(data)
            except UnicodeDecodeError:
                # Not a decision point: fall through to the JSON reader,
                # which reports undecodable bytes with a record number.
                decode_failed = True
                break
            for char in text:
                if char not in _ASCII_WHITESPACE:
                    first_nonspace = char
                    break
        if first_nonspace is None and not decode_failed:
            # Matches the existing auto-detection: empty or whitespace-only
            # input is an empty CSV file.  Nothing will iterate it, so close.
            fp.close()
            return (), Format.CSV
        if decode_failed or first_nonspace == "[" or first_nonspace == "{":
            source = _PrefixedStream(fp, bytes(window))
            return (
                _stream_rows(Format.JSON, cast(BinaryIO, source), True, encoding),
                Format.JSON,
            )
        # CSV/TSV detection needs a little more than the first character.
        while len(window) < 2048:
            data = fp.read(min(stream_json_chunk_size, 2048 - len(window)))
            if isinstance(data, str):
                raise TypeError(
                    "rows_from_file() requires a file-like object opened in binary mode"
                )
            if data == b"":
                break
            window.extend(data)
        source = _PrefixedStream(fp, bytes(window))
        sniff_window = bytes(window).strip()
        try:
            sample = sniff_window.decode(use_encoding)
        except UnicodeDecodeError:
            sample = sniff_window.decode(use_encoding, "ignore")
        dialect = csv.Sniffer().sniff(sample)
        rows, _ = rows_from_file(
            cast(BinaryIO, source),
            format=Format.CSV,
            dialect=dialect,
            encoding=encoding,
            ignore_extras=ignore_extras,
            extras_key=extras_key,
            stream_json=stream_json,
            stream_json_chunk_size=stream_json_chunk_size,
        )
        detected_format = Format.TSV if dialect.delimiter == "\t" else Format.CSV
        return rows, detected_format

    if format == Format.JSON:
        if stream_json:
            return _stream_rows(Format.JSON, fp, True, encoding), Format.JSON
        decoded = json.load(fp)
        if isinstance(decoded, dict):
            decoded = [decoded]
        if not isinstance(decoded, list):
            raise RowsFromFileBadJSON("JSON must be a list or a dictionary")
        return decoded, Format.JSON
    elif format == Format.NL:
        if stream_json:
            return _stream_rows(Format.NL, fp, True, encoding), Format.NL
        return (json.loads(line) for line in fp if line.strip()), Format.NL
    elif format == Format.CSV:
        decoded_fp = io.TextIOWrapper(fp, encoding=use_encoding)
        if dialect is not None:
            reader = csv.DictReader(decoded_fp, dialect=dialect)
        else:
            reader = csv.DictReader(decoded_fp)
        rows = _extra_key_strategy(
            cast(Iterable[dict[str | None, object]], reader),
            ignore_extras,
            extras_key,
        )
        return _CloseableIterator(iter(rows), decoded_fp), Format.CSV
    elif format == Format.TSV:
        rows, _ = rows_from_file(
            fp, format=Format.CSV, dialect=csv.excel_tab, encoding=encoding
        )
        return (
            _extra_key_strategy(
                cast(Iterable[dict[str | None, object]], rows),
                ignore_extras,
                extras_key,
            ),
            Format.TSV,
        )
    elif format is None:
        # Detect the format, then call this recursively
        buffered = io.BufferedReader(cast(io.RawIOBase, fp), buffer_size=4096)
        try:
            first_bytes = buffered.peek(2048).strip()
        except AttributeError:
            # Likely the user passed a TextIO when this needs a BytesIO
            raise TypeError(
                "rows_from_file() requires a file-like object that supports peek(), such as io.BytesIO"
            )
        if not first_bytes:
            buffered.close()
            return (), Format.CSV
        if first_bytes.startswith((b"[", b"{")):
            # JSON is read eagerly, so the detection wrapper can close now,
            # including when parsing raises an error.
            # TODO: Detect newline-JSON
            with buffered:
                return rows_from_file(buffered, format=Format.JSON)
        else:
            dialect = csv.Sniffer().sniff(
                first_bytes.decode(encoding or "utf-8-sig", "ignore")
            )
            rows, _ = rows_from_file(
                buffered, format=Format.CSV, dialect=dialect, encoding=encoding
            )
            # Make sure we return the format we detected
            detected_format = Format.TSV if dialect.delimiter == "\t" else Format.CSV
            return (
                _extra_key_strategy(
                    cast(Iterable[dict[str | None, object]], rows),
                    ignore_extras,
                    extras_key,
                ),
                detected_format,
            )
    else:
        raise RowsFromFileError("Bad format")


class TypeTracker:
    """
    Wrap an iterator of dictionaries and keep track of which SQLite column
    types are the most likely fit for each of their keys.

    Example usage:

    .. code-block:: python

        from sqlite_utils.utils import TypeTracker
        import sqlite_utils

        db = sqlite_utils.Database(memory=True)
        tracker = TypeTracker()
        rows = [{"id": "1", "name": "Cleo", "id": "2", "name": "Cardi"}]
        db["creatures"].insert_all(tracker.wrap(rows))
        print(tracker.types)
        # Outputs {'id': 'integer', 'name': 'text'}
        db["creatures"].transform(types=tracker.types)
    """

    def __init__(self) -> None:
        self.trackers: dict[str, ValueTracker] = {}

    def wrap(self, iterator: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        """
        Use this to loop through an existing iterator, tracking the column types
        as part of the iteration.

        :param iterator: The iterator to wrap
        """
        for row in iterator:
            for key, value in row.items():
                tracker = self.trackers.setdefault(key, ValueTracker())
                tracker.evaluate(value)
            yield row

    @property
    def types(self) -> dict[str, str]:
        """
        A dictionary mapping column names to their detected types. This can be passed
        to the ``db[table_name].transform(types=tracker.types)`` method.
        """
        return {key: tracker.guessed_type for key, tracker in self.trackers.items()}


class ValueTracker:
    couldbe: dict[str, Callable[[object], bool]]

    def __init__(self) -> None:
        self.couldbe = {key: getattr(self, "test_" + key) for key in self.get_tests()}

    @classmethod
    def get_tests(cls) -> list[str]:
        return [
            key.split("test_")[-1] for key in cls.__dict__ if key.startswith("test_")
        ]

    def test_integer(self, value: object) -> bool:
        try:
            int(cast(Any, value))
            return True
        except (ValueError, TypeError):
            return False

    def test_float(self, value: object) -> bool:
        try:
            float(cast(Any, value))
            return True
        except (ValueError, TypeError):
            return False

    def __repr__(self) -> str:
        return self.guessed_type + ": possibilities = " + repr(self.couldbe)

    @property
    def guessed_type(self) -> str:
        options = set(self.couldbe.keys())
        # Return based on precedence
        for key in self.get_tests():
            if key in options:
                return key
        return "text"

    def evaluate(self, value: object) -> None:
        if not value or not self.couldbe:
            return
        not_these: list[str] = []
        for name, test in self.couldbe.items():
            if not test(value):
                not_these.append(name)
        for key in not_these:
            del self.couldbe[key]


class NullProgressBar(Generic[T]):
    def __init__(self, *args: Iterable[T]) -> None:
        self.args = args

    def __iter__(self) -> Iterator[T]:
        yield from self.args[0]

    def update(self, value: int) -> None:
        pass


@contextlib.contextmanager
def progressbar(*args: Iterable[T], **kwargs: Any) -> Generator[Any, None, None]:
    silent = kwargs.pop("silent")
    if silent:
        yield NullProgressBar(*args)
    else:
        with click.progressbar(*args, **kwargs) as bar:  # type: ignore
            yield bar


def _compile_code(
    code: str, imports: Iterable[str], variable: str = "value"
) -> Callable[..., Any]:
    globals_dict: dict[str, Any] = {"r": recipes, "recipes": recipes}
    # Handle imports first so they're available for all approaches
    for import_ in imports:
        globals_dict[import_.split(".")[0]] = __import__(import_)

    # If user defined a convert() function, return that
    try:
        exec(code, globals_dict)  # noqa: S102
        return cast(Callable[..., object], globals_dict["convert"])
    except (AttributeError, SyntaxError, NameError, KeyError, TypeError):
        pass

    # Check if code is a direct callable reference
    # e.g. "r.parsedate" instead of "r.parsedate(value)"
    try:
        fn = eval(code, globals_dict)
        if callable(fn):
            return cast(Callable[..., object], fn)
    except Exception:  # noqa: BLE001, S110
        pass

    # Try compiling their code as a function instead
    body_variants = [code]
    # If single line and no 'return', try adding the return
    if "\n" not in code and not code.strip().startswith("return "):
        body_variants.insert(0, f"return {code}")

    code_o = None
    for variant in body_variants:
        new_code = [f"def fn({variable}):"]
        for line in variant.split("\n"):
            new_code.append(f"    {line}")
        try:
            code_o = compile("\n".join(new_code), "<string>", "exec")
            break
        except SyntaxError:
            # Try another variant, e.g. for 'return row["column"] = 1'
            continue

    if code_o is None:
        raise SyntaxError("Could not compile code")

    exec(code_o, globals_dict)  # noqa: S102
    return cast(Callable[..., object], globals_dict["fn"])


def chunks(sequence: Iterable[T], size: int) -> Iterable[Iterable[T]]:
    """
    Iterate over chunks of the sequence of the given size.

    :param sequence: Any Python iterator
    :param size: The size of each chunk
    """
    iterator = iter(sequence)
    for item in iterator:
        yield itertools.chain([item], itertools.islice(iterator, size - 1))


def hash_record(record: dict[str, Any], keys: Iterable[str] | None = None) -> str:
    """
    ``record`` should be a Python dictionary. Returns a sha1 hash of the
    keys and values in that record.

    If ``keys=`` is provided, uses just those keys to generate the hash.

    Example usage::

        from sqlite_utils.utils import hash_record

        hashed = hash_record({"name": "Cleo", "twitter": "CleoPaws"})
        # Or with the keys= option:
        hashed = hash_record(
            {"name": "Cleo", "twitter": "CleoPaws", "age": 7},
            keys=("name", "twitter")
        )

    :param record: Record to generate a hash for
    :param keys: Subset of keys to use for that hash
    """
    to_hash: dict[str, Any] = record
    if keys is not None:
        to_hash = {key: record[key] for key in keys}
    return hashlib.sha1(
        json.dumps(to_hash, separators=(",", ":"), sort_keys=True, default=repr).encode(
            "utf8"
        )
    ).hexdigest()


def dedupe_keys(keys: Iterable[str]) -> list[str]:
    """
    Rename duplicates in a list of column names so every name is unique,
    by appending ``_2``, ``_3``... to later occurrences - skipping any
    suffix that would collide with another column in the list.

    Used when converting SQL query rows to dictionaries, where duplicate
    column names would otherwise silently overwrite each other.

    :param keys: List of column names, possibly containing duplicates
    """
    keys = list(keys)
    taken = set(keys)
    if len(taken) == len(keys):
        # No duplicates - the common case
        return keys
    seen: set = set()
    result = []
    for key in keys:
        if key in seen:
            new_key = key
            suffix = 2
            while new_key in seen or new_key in taken:
                new_key = f"{key}_{suffix}"
                suffix += 1
            key = new_key
        seen.add(key)
        result.append(key)
    return result


def _flatten(d: dict[str, Any]) -> Generator[tuple[str, Any], None, None]:
    for key, value in d.items():
        if isinstance(value, dict):
            for key2, value2 in _flatten(value):
                yield key + "_" + key2, value2
        else:
            yield key, value


def flatten(row: dict[str, Any]) -> dict[str, Any]:
    """
    Turn a nested dict e.g. ``{"a": {"b": 1}}`` into a flat dict: ``{"a_b": 1}``

    :param row: A Python dictionary, optionally with nested dictionaries
    """
    return dict(_flatten(row))
