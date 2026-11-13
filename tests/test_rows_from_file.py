import io
import json
from io import BytesIO, StringIO

import pytest

from sqlite_utils.utils import (
    Format,
    RowError,
    RowsFromFileBadJSON,
    rows_from_file,
)


@pytest.mark.parametrize(
    "input,expected_format",
    (
        (b"id,name\n1,Cleo", Format.CSV),
        (b"id\tname\n1\tCleo", Format.TSV),
        (b'[{"id": "1", "name": "Cleo"}]', Format.JSON),
    ),
)
def test_rows_from_file_detect_format(input, expected_format):
    rows, format = rows_from_file(BytesIO(input))
    assert format == expected_format
    rows_list = list(rows)
    assert rows_list == [{"id": "1", "name": "Cleo"}]


@pytest.mark.parametrize("input", (b"", b" \n\t"))
def test_rows_from_file_empty_input(input):
    rows, format = rows_from_file(BytesIO(input))
    assert format == Format.CSV
    assert list(rows) == []


@pytest.mark.parametrize(
    "ignore_extras,extras_key,expected",
    (
        (True, None, [{"id": "1", "name": "Cleo"}]),
        (False, "_rest", [{"id": "1", "name": "Cleo", "_rest": ["oops"]}]),
        # expected of None means expect an error:
        (False, False, None),
    ),
)
def test_rows_from_file_extra_fields_strategies(ignore_extras, extras_key, expected):
    try:
        rows, _format = rows_from_file(
            BytesIO(b"id,name\r\n1,Cleo,oops"),
            format=Format.CSV,
            ignore_extras=ignore_extras,
            extras_key=extras_key,
        )
        list_rows = list(rows)
    except RowError:
        if expected is None:
            # This is fine,
            return
        else:
            # We did not expect an error
            raise
    assert list_rows == expected


def test_rows_from_file_error_on_string_io():
    with pytest.raises(TypeError) as ex:
        rows_from_file(StringIO("id,name\r\n1,Cleo"))  # type: ignore[arg-type]
    assert ex.value.args == (
        "rows_from_file() requires a file-like object that supports peek(), such as io.BytesIO",
    )


@pytest.fixture
def buffered_readers(monkeypatch):
    # Keep wrappers alive so these checks cannot pass due to garbage collection.
    readers = []
    original = io.BufferedReader

    def buffered_reader(*args, **kwargs):
        reader = original(*args, **kwargs)
        readers.append(reader)
        return reader

    monkeypatch.setattr(io, "BufferedReader", buffered_reader)
    yield readers
    for reader in readers:
        reader.close()


@pytest.mark.parametrize(
    "content, expected_format, expected_rows",
    [
        (b'[{"id": 1}]', Format.JSON, [{"id": 1}]),
        (b'{"id": 1}', Format.JSON, [{"id": 1}]),
        (b"[]", Format.JSON, []),
        (b"", Format.CSV, []),
        (b" \n\t", Format.CSV, []),
    ],
)
def test_detect_format_closes_eager_reader(
    tmp_path, buffered_readers, content, expected_format, expected_rows
):
    path = tmp_path / "input"
    path.write_bytes(content)
    with path.open("rb") as fp:
        rows, detected = rows_from_file(fp)
        assert detected == expected_format
        assert list(rows) == expected_rows
        assert len(buffered_readers) == 1
        assert buffered_readers[0].closed


@pytest.mark.parametrize("content", [b"[", b'{"id":'])
def test_detect_format_closes_reader_on_invalid_json(
    tmp_path, buffered_readers, content
):
    path = tmp_path / "input.json"
    path.write_bytes(content)
    with path.open("rb") as fp:
        with pytest.raises(json.JSONDecodeError):
            rows_from_file(fp)
        assert len(buffered_readers) == 1
        assert buffered_readers[0].closed


@pytest.mark.parametrize(
    "delimiter, expected_format", [(b",", Format.CSV), (b"\t", Format.TSV)]
)
def test_detect_format_keeps_streaming_reader_open(
    buffered_readers, delimiter, expected_format
):
    content = b"id" + delimiter + b"name\n" + (b"1" + delimiter + b"Cleo\n") * 2000
    rows, detected = rows_from_file(BytesIO(content))
    assert detected == expected_format
    assert len(buffered_readers) == 1
    assert not buffered_readers[0].closed
    assert len(list(rows)) == 2000
    assert buffered_readers[0].closed


class ForwardOnlyStream:
    """Binary stream with only read() and close(), returning short reads."""

    def __init__(self, data, max_read=None):
        self._data = data
        self._max_read = max_read
        self.read_sizes = []
        self.close_count = 0

    def read(self, size=-1):
        self.read_sizes.append(size)
        limit = size if self._max_read is None else min(size, self._max_read)
        data = self._data[:limit]
        self._data = self._data[limit:]
        return data

    def close(self):
        self.close_count += 1

    @property
    def bytes_read(self):
        return sum(self.read_sizes)


@pytest.mark.parametrize("format", (Format.JSON, Format.NL))
def test_stream_json_explicit_format_is_lazy_and_closes(format):
    if format == Format.JSON:
        content = b'[{"id": 1}, {"id": 2}, {"id": '
    else:
        content = b'{"id": 1}\n{"id": 2}\n{"id": '
    stream = ForwardOnlyStream(content, max_read=5)
    rows, detected = rows_from_file(
        stream, format=format, stream_json=True, stream_json_chunk_size=8
    )
    assert detected == format
    assert next(rows) == {"id": 1}
    assert next(rows) == {"id": 2}
    with pytest.raises(RowsFromFileBadJSON):
        next(rows)
    assert stream.close_count == 1


def test_stream_json_auto_detect_json_without_peek():
    content = b'  [{"id": 1}, {"id": 2}, {"id": 3}]'
    stream = ForwardOnlyStream(content)
    rows, detected = rows_from_file(stream, stream_json=True, stream_json_chunk_size=4)
    assert detected == Format.JSON
    assert next(rows) == {"id": 1}
    # Only a small prefix was consumed to produce the first row.
    assert stream.bytes_read < len(content)
    assert list(rows) == [{"id": 2}, {"id": 3}]
    assert stream.close_count == 1


def test_stream_json_auto_detect_single_object():
    stream = ForwardOnlyStream(b' {"id": 1} ')
    rows, detected = rows_from_file(stream, stream_json=True, stream_json_chunk_size=2)
    assert detected == Format.JSON
    assert list(rows) == [{"id": 1}]
    assert stream.close_count == 1


def test_stream_json_auto_detect_does_not_copy_large_array():
    content = json.dumps([{"id": i, "text": "é" * 20} for i in range(4000)]).encode()
    stream = ForwardOnlyStream(content, max_read=97)
    rows, detected = rows_from_file(stream, stream_json=True, stream_json_chunk_size=64)
    assert detected == Format.JSON
    # Format detection itself consumes exactly one chunk, not the document.
    assert stream.bytes_read == 64
    assert next(rows) == {"id": 0, "text": "é" * 20}
    assert len(list(rows)) == 3999
    assert stream.close_count == 1


def test_stream_json_auto_detect_closes_on_bad_json():
    stream = ForwardOnlyStream(b'[{"id": 1},')
    rows, detected = rows_from_file(stream, stream_json=True)
    assert detected == Format.JSON
    assert next(rows) == {"id": 1}
    with pytest.raises(RowsFromFileBadJSON):
        next(rows)
    assert stream.close_count == 1


def test_stream_json_leaves_csv_and_default_behaviour_unchanged():
    # CSV keeps its existing behaviour: the caller owns the file and the
    # CSV reader's TextIOWrapper closes it when iteration finishes.
    stream = ForwardOnlyStream(b"id,name\r\n1,Cleo", max_read=3)
    rows, detected = rows_from_file(stream, stream_json=True, stream_json_chunk_size=4)
    assert detected == Format.CSV
    assert list(rows) == [{"id": "1", "name": "Cleo"}]


def test_stream_json_auto_detect_tsv_with_short_reads():
    stream = ForwardOnlyStream(b"id\tname\r\n1\tCleo\r\n", max_read=2)
    rows, detected = rows_from_file(stream, stream_json=True, stream_json_chunk_size=3)
    assert detected == Format.TSV
    assert list(rows) == [{"id": "1", "name": "Cleo"}]


def test_stream_json_auto_detect_empty_input_closes_stream():
    stream = ForwardOnlyStream(b" \n\t")
    rows, detected = rows_from_file(stream, stream_json=True)
    assert detected == Format.CSV
    assert list(rows) == []
    assert stream.close_count == 1


def test_stream_json_ignore_extras_conflict_still_raises():
    with pytest.raises(ValueError):
        rows_from_file(
            BytesIO(b"id,name\n1,Cleo"),
            stream_json=True,
            ignore_extras=True,
            extras_key="rest",
        )


@pytest.mark.parametrize("chunk_size", (0, 1048577, True, 1.5))
def test_stream_json_invalid_chunk_size_before_reading(chunk_size):
    stream = ForwardOnlyStream(b'[{"id": 1}]')
    with pytest.raises(ValueError):
        rows_from_file(
            stream,
            format=Format.JSON,
            stream_json=True,
            stream_json_chunk_size=chunk_size,
        )
    assert stream.read_sizes == []
    assert stream.close_count == 0
