import io
import json

import pytest

from sqlite_utils.utils import RowsFromFileBadJSON, iter_json_rows


class ForwardOnlyStream:
    """
    Minimal binary stream: only read() and close().

    Optionally returns short reads, so a short read must never be treated
    as end-of-file.  Records every requested read size and close count.
    """

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


def make_large_array(count=5000):
    return json.dumps([{"id": i, "name": "déjà vu 名前"} for i in range(count)]).encode(
        "utf-8"
    )


def test_constructing_does_not_read_or_close():
    stream = ForwardOnlyStream(b'[{"id": 1}]')
    iterator = iter_json_rows(stream, close_fp=True)
    assert stream.read_sizes == []
    assert stream.close_count == 0
    assert iterator is not None
    iterator.close()
    assert stream.close_count == 1
    assert stream.read_sizes == []


@pytest.mark.parametrize("close_fp,expected", ((False, 0), (True, 1)))
def test_explicit_close_is_idempotent(close_fp, expected):
    stream = ForwardOnlyStream(b'[{"id": 1}]')
    iterator = iter_json_rows(stream, close_fp=close_fp)
    iterator.close()
    iterator.close()
    assert stream.close_count == expected
    assert list(iterator) == []
    assert stream.read_sizes == []


@pytest.mark.parametrize("close_fp,expected", ((False, 0), (True, 1)))
def test_close_after_exhaustion_is_idempotent(close_fp, expected):
    stream = ForwardOnlyStream(b'[{"id": 1}]')
    iterator = iter_json_rows(stream, close_fp=close_fp)
    assert list(iterator) == [{"id": 1}]
    iterator.close()
    assert stream.close_count == expected


def test_first_row_is_lazy_and_does_not_read_whole_document():
    data = make_large_array()
    stream = ForwardOnlyStream(data, max_read=37)
    iterator = iter_json_rows(stream, chunk_size=64, close_fp=True)
    first = next(iterator)
    assert first["id"] == 0
    # A whole 5000-row document is hundreds of kilobytes; the first row is
    # available after a small prefix of it has been read.
    assert stream.bytes_read < len(data) // 10
    assert stream.close_count == 0
    assert len(list(iterator)) == 4999
    assert stream.close_count == 1


def test_large_array_with_short_reads_and_read_size_bounds():
    data = make_large_array(2000)
    stream = ForwardOnlyStream(data, max_read=13)
    rows = list(iter_json_rows(stream, chunk_size=32, close_fp=True))
    assert [row["id"] for row in rows] == list(range(2000))
    assert all(1 <= size <= 32 for size in stream.read_sizes)
    assert stream.close_count == 1


def test_short_reads_at_every_length_are_not_eof():
    data = b'[{"id": 1}, {"id": 2}, {"id": 3}]'
    for max_read in (1, 2, 3, 5, 7):
        stream = ForwardOnlyStream(data, max_read=max_read)
        assert list(iter_json_rows(stream, chunk_size=4)) == [
            {"id": 1},
            {"id": 2},
            {"id": 3},
        ]


@pytest.mark.parametrize(
    "data,expected",
    [
        (b'[{"id": 1}, {"id": 2}]', [{"id": 1}, {"id": 2}]),
        (b'{"id": 1}', [{"id": 1}]),
        (b'  \n\t {"id": 1} \r\n ', [{"id": 1}]),
        (b"[]", []),
        (b" [ ] ", []),
        (b'[{"a": {"b": [1, 2, {"c": "}"}]}}]', [{"a": {"b": [1, 2, {"c": "}"}]}}]),
    ],
)
def test_valid_documents(data, expected):
    assert list(iter_json_rows(io.BytesIO(data))) == expected


def test_rows_before_bad_record_are_yielded():
    stream = ForwardOnlyStream(b'[{"id": 1}, {"id": 2}, {"id": ')
    iterator = iter_json_rows(stream, close_fp=True)
    assert next(iterator) == {"id": 1}
    assert next(iterator) == {"id": 2}
    with pytest.raises(RowsFromFileBadJSON) as ex:
        next(iterator)
    assert "record 3" in str(ex.value)
    assert stream.close_count == 1


@pytest.mark.parametrize(
    "data,expected_record,expected_rows",
    [
        (b"[", 1, []),
        (b"", 1, []),
        (b"   ", 1, []),
        (b"null", 1, []),
        (b'"string"', 1, []),
        (b"123", 1, []),
        (b"true", 1, []),
        (b"[1]", 1, []),
        (b'["a"]', 1, []),
        (b"[null]", 1, []),
        (b'[{"a": 1}, 2]', 2, [{"a": 1}]),
        (b'[{"a": 1} {"b": 2}]', 2, [{"a": 1}]),
        (b'[{"a": 1},]', 2, [{"a": 1}]),
        (b'[{"a": 1}] trailing', 2, [{"a": 1}]),
        (b'[{"a": 1}]]', 2, [{"a": 1}]),
        (b'[{"a": 1}, {"b": 2}', 3, [{"a": 1}, {"b": 2}]),
        (b'[{"a": 1}, {"b": "unterminated}]', 2, [{"a": 1}]),
        (b'[{"a": 1}, {"b": "bad \\q escape"}]', 2, [{"a": 1}]),
        (b'{"a": 1}{"b": 2}', 2, [{"a": 1}]),
        (b'{"a": "unterminated}', 1, []),
    ],
)
def test_bad_json_documents(data, expected_record, expected_rows):
    stream = ForwardOnlyStream(data)
    iterator = iter_json_rows(stream, chunk_size=3, close_fp=True)
    rows = []
    with pytest.raises(RowsFromFileBadJSON) as ex:
        while True:
            rows.append(next(iterator))
    assert f"record {expected_record}" in str(ex.value)
    assert stream.close_count == 1
    # Every row parsed before the failure is still available.
    assert rows == expected_rows


def test_error_number_counts_records_not_array_positions():
    iterator = iter_json_rows(io.BytesIO(b'[{"a": 1}, {"b": 2}, 3]'))
    assert next(iterator) == {"a": 1}
    assert next(iterator) == {"b": 2}
    with pytest.raises(RowsFromFileBadJSON) as ex:
        next(iterator)
    assert "record 3" in str(ex.value)


@pytest.mark.parametrize(
    "data,expected",
    [
        (b'{"a": 1}\n{"a": 2}\n', [{"a": 1}, {"a": 2}]),
        (b'{"a": 1}\r\n{"a": 2}', [{"a": 1}, {"a": 2}]),
        (b'{"a": 1}\n\n  \t\n{"a": 2}\n', [{"a": 1}, {"a": 2}]),
        (b"", []),
        (b" \n\t\r\n", []),
        (b'{"a": "contains \\n escape"}', [{"a": "contains \n escape"}]),
        (b'\xef\xbb\xbf{"a": 1}\n{"a": 2}', [{"a": 1}, {"a": 2}]),
    ],
)
def test_ndjson_valid(data, expected):
    assert list(iter_json_rows(io.BytesIO(data), newline_delimited=True)) == expected


@pytest.mark.parametrize(
    "data,expected_record,expected_line",
    [
        (b'{"a": 1}\n{"b": 2} {"c": 3}\n', 2, 2),
        (b'{"a": 1}\n[]\n', 2, 2),
        (b'{"a": 1}\nnope\n', 2, 2),
        (b'{"a": 1}\n\n\n{bad\n', 2, 4),
        (b'{"a": 1}\r\n\r\n{"b": 2}\r\n[1]\r\n', 3, 4),
        (b'{"a": 1}\n{"b": 2}\n{"c": "unterminated\n', 3, 3),
    ],
)
def test_ndjson_error_numbers(data, expected_record, expected_line):
    iterator = iter_json_rows(io.BytesIO(data), newline_delimited=True)
    rows = []
    with pytest.raises(RowsFromFileBadJSON) as ex:
        while True:
            rows.append(next(iterator))
    message = str(ex.value)
    assert f"record {expected_record}" in message
    assert f"line {expected_line}" in message
    assert rows[0]["a"] == 1


def test_ndjson_blank_lines_do_not_increment_record_number():
    iterator = iter_json_rows(
        io.BytesIO(b'{"a": 1}\n\n\n{"b": 2}\n\n{}\n\n[1]\n'), newline_delimited=True
    )
    assert next(iterator) == {"a": 1}
    assert next(iterator) == {"b": 2}
    assert next(iterator) == {}
    with pytest.raises(RowsFromFileBadJSON) as ex:
        next(iterator)
    assert "record 4 (line 8)" in str(ex.value)


@pytest.mark.parametrize("chunk_size", (1, 2, 3))
def test_utf8_bom_and_multibyte_split_across_chunks(chunk_size):
    data = '﻿[{"name": "日本語 déjà"}]\n'.encode("utf-8")
    stream = ForwardOnlyStream(data, max_read=1)
    assert list(iter_json_rows(stream, chunk_size=chunk_size)) == [
        {"name": "日本語 déjà"}
    ]


def test_explicit_encoding_incremental_decode():
    data = '[{"name": "été"}]'.encode("utf-16")
    stream = ForwardOnlyStream(data, max_read=1)
    assert list(iter_json_rows(stream, encoding="utf-16", chunk_size=1)) == [
        {"name": "été"}
    ]
    ndjson = '{"name": "été"}\n{"name": "été"}'.encode("utf-16")
    stream = ForwardOnlyStream(ndjson, max_read=1)
    assert list(
        iter_json_rows(stream, encoding="utf-16", chunk_size=1, newline_delimited=True)
    ) == [{"name": "été"}, {"name": "été"}]


def test_invalid_encoded_bytes_chain_original_exception():
    stream = ForwardOnlyStream(b'[{"a": "\xff\xfe"}]', max_read=2)
    iterator = iter_json_rows(stream, chunk_size=2, close_fp=True)
    with pytest.raises(RowsFromFileBadJSON) as ex:
        next(iterator)
    assert "record 1" in str(ex.value)
    assert isinstance(ex.value.__cause__, UnicodeDecodeError)
    assert stream.close_count == 1


def test_bad_byte_late_in_chunk_keeps_earlier_records():
    # A single chunk holds a good record followed by an undecodable byte.
    data = b'[{"a": 1}, {"b": "\xff"}]'
    stream = ForwardOnlyStream(data, max_read=len(data))
    iterator = iter_json_rows(stream, chunk_size=len(data), close_fp=True)
    assert next(iterator) == {"a": 1}
    with pytest.raises(RowsFromFileBadJSON) as ex:
        next(iterator)
    assert "record 2" in str(ex.value)
    assert isinstance(ex.value.__cause__, UnicodeDecodeError)
    assert stream.close_count == 1


def test_bad_byte_after_pending_multibyte_sequence():
    data = '[{"a": "é"}, {"b": "é�"}]'.encode()
    data = data.replace(b"\xef\xbf\xbd", b"\xff")
    stream = ForwardOnlyStream(data, max_read=len(data))
    iterator = iter_json_rows(stream, chunk_size=len(data))
    assert next(iterator) == {"a": "é"}
    with pytest.raises(RowsFromFileBadJSON) as ex:
        next(iterator)
    assert "record 2" in str(ex.value)
    assert isinstance(ex.value.__cause__, UnicodeDecodeError)


def test_bom_requiring_codec_without_bom_is_reported():
    stream = ForwardOnlyStream(b'[{"a": 1}]', max_read=2)
    iterator = iter_json_rows(stream, encoding="utf-16", chunk_size=2, close_fp=True)
    with pytest.raises(RowsFromFileBadJSON) as ex:
        next(iterator)
    assert stream.close_count == 1
    assert isinstance(ex.value.__cause__, UnicodeError)


def test_invalid_encoding_name_raises_before_reading():
    stream = ForwardOnlyStream(b"[]")
    with pytest.raises(LookupError):
        iter_json_rows(stream, encoding="not-a-real-encoding", close_fp=True)
    assert stream.read_sizes == []
    assert stream.close_count == 0


@pytest.mark.parametrize("chunk_size", (0, -1, 1048577, True, False, 1.5, "10", None))
def test_invalid_chunk_size_before_reading_or_closing(chunk_size):
    stream = ForwardOnlyStream(b'[{"id": 1}]')
    with pytest.raises(ValueError):
        iter_json_rows(stream, chunk_size=chunk_size, close_fp=True)
    assert stream.read_sizes == []
    assert stream.close_count == 0


@pytest.mark.parametrize("chunk_size", (1, 1048576))
def test_chunk_size_boundaries_accepted(chunk_size):
    stream = ForwardOnlyStream(b'[{"id": 1}]')
    assert list(iter_json_rows(stream, chunk_size=chunk_size)) == [{"id": 1}]


class FailingStream(ForwardOnlyStream):
    def read(self, size=-1):
        self.read_sizes.append(size)
        raise OSError("disk on fire")


def test_oserror_is_propagated_and_cleaned_up():
    stream = FailingStream(b"")
    iterator = iter_json_rows(stream, close_fp=True)
    with pytest.raises(OSError) as ex:
        next(iterator)
    assert str(ex.value) == "disk on fire"
    assert stream.close_count == 1
    # Already finished: further next() calls stop without reading again.
    reads = list(stream.read_sizes)
    assert list(iterator) == []
    assert stream.read_sizes == reads


@pytest.mark.parametrize("close_fp,expected", ((False, 0), (True, 1)))
def test_with_block_closes_on_exception_as_configured(close_fp, expected):
    stream = ForwardOnlyStream(b'[{"id": 1}, {"id": 2}]')

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with iter_json_rows(stream, close_fp=close_fp) as rows:
            assert next(rows) == {"id": 1}
            raise Boom()
    assert stream.close_count == expected


@pytest.mark.parametrize("close_fp,expected", ((False, 0), (True, 1)))
def test_with_block_closes_on_normal_exit_as_configured(close_fp, expected):
    stream = ForwardOnlyStream(b'[{"id": 1}]')
    with iter_json_rows(stream, close_fp=close_fp) as rows:
        assert list(rows) == [{"id": 1}]
    assert stream.close_count == expected


def test_close_during_iteration_stops_reading():
    stream = ForwardOnlyStream(make_large_array(100), max_read=5)
    iterator = iter_json_rows(stream, chunk_size=16, close_fp=True)
    next(iterator)
    iterator.close()
    reads_at_close = list(stream.read_sizes)
    assert list(iterator) == []
    assert stream.read_sizes == reads_at_close
    assert stream.close_count == 1


def test_input_is_never_rewound_or_seeked():
    class NoSeekStream(ForwardOnlyStream):
        def seek(self, *args, **kwargs):
            raise AssertionError("seek() must not be called")

        def peek(self, *args, **kwargs):
            raise AssertionError("peek() must not be called")

        def readinto(self, *args, **kwargs):
            raise AssertionError("readinto() must not be called")

        def fileno(self):
            raise AssertionError("fileno() must not be called")

    stream = NoSeekStream(make_large_array(20), max_read=11)
    assert len(list(iter_json_rows(stream, chunk_size=8))) == 20


def test_text_mode_input_is_rejected():
    with pytest.raises(TypeError):
        next(iter_json_rows(io.StringIO('{"a": 1}')))
