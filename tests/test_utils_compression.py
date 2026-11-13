from __future__ import annotations

import zlib
from gzip import BadGzipFile, GzipFile
from io import BytesIO
from pathlib import Path

import brotli
import pytest
from backports import zstd
from w3lib.encoding import html_to_unicode

from scrapy.http import Response
from scrapy.utils._compression import (
    _DecompressionMaxRatioExceeded,
    _DecompressionMaxSizeExceeded,
    _inflate,
    _unbrotli,
    _unzstd,
    gunzip,
    gzip_magic_number,
)
from tests import tests_datadir

SAMPLEDIR = Path(tests_datadir, "compressed")

_PLAIN = b"a" * 200_000


def _gzip_bytes(data: bytes) -> bytes:
    buf = BytesIO()
    with GzipFile(fileobj=buf, mode="wb") as f:
        f.write(data)
    return buf.getvalue()


_RATIO_CASES = [
    pytest.param(gunzip, _gzip_bytes(_PLAIN), "gzip", id="gzip"),
    pytest.param(_inflate, zlib.compress(_PLAIN), "deflate", id="deflate"),
    pytest.param(_unbrotli, brotli.compress(_PLAIN), "br", id="br"),
    pytest.param(_unzstd, zstd.compress(_PLAIN), "zstd", id="zstd"),
]


@pytest.mark.parametrize(("decode", "compressed", "encoding"), _RATIO_CASES)
class TestMaxRatio:
    def test_disabled_by_default(self, decode, compressed, encoding) -> None:
        assert decode(compressed) == _PLAIN

    def test_zero_byte_input(self, decode, compressed, encoding) -> None:
        assert decode(b"", max_ratio=1.0) == b""

    def test_boundary_ratio_accepted(self, decode, compressed, encoding) -> None:
        ratio = len(_PLAIN) / len(compressed)
        assert decode(compressed, max_ratio=ratio) == _PLAIN

    def test_ratio_exceeded(self, decode, compressed, encoding) -> None:
        with pytest.raises(_DecompressionMaxRatioExceeded) as exc_info:
            decode(compressed, max_ratio=1.0)
        error = exc_info.value
        assert error.compressed_size == len(compressed)
        assert error.decompressed_size < len(_PLAIN)
        assert error.max_ratio == 1.0
        assert error.encoding == ""

    def test_max_size_still_enforced(self, decode, compressed, encoding) -> None:
        with pytest.raises(_DecompressionMaxSizeExceeded):
            decode(compressed, max_size=1000, max_ratio=1e9)

    def test_both_limits_disabled(self, decode, compressed, encoding) -> None:
        assert decode(compressed, max_size=0, max_ratio=0) == _PLAIN


def test_gunzip_basic():
    r1 = Response(
        "http://www.example.com",
        body=(SAMPLEDIR / "feed-sample1.xml.gz").read_bytes(),
    )
    assert gzip_magic_number(r1)

    r2 = Response("http://www.example.com", body=gunzip(r1.body))
    assert not gzip_magic_number(r2)
    assert len(r2.body) == 9950


def test_gunzip_truncated():
    text = gunzip((SAMPLEDIR / "truncated-crc-error.gz").read_bytes())
    assert text.endswith(b"</html")


def test_gunzip_no_gzip_file_raises():
    with pytest.raises(BadGzipFile):
        gunzip((SAMPLEDIR / "feed-sample1.xml").read_bytes())


def test_gunzip_truncated_short():
    r1 = Response(
        "http://www.example.com",
        body=(SAMPLEDIR / "truncated-crc-error-short.gz").read_bytes(),
    )
    assert gzip_magic_number(r1)

    r2 = Response("http://www.example.com", body=gunzip(r1.body))
    assert r2.body.endswith(b"</html>")
    assert not gzip_magic_number(r2)


def test_is_gzipped_empty():
    r1 = Response("http://www.example.com")
    assert not gzip_magic_number(r1)


def test_gunzip_illegal_eof():
    text = html_to_unicode(
        "charset=cp1252", gunzip((SAMPLEDIR / "unexpected-eof.gz").read_bytes())
    )[1]
    expected_text = (SAMPLEDIR / "unexpected-eof-output.txt").read_text(
        encoding="utf-8"
    )
    assert len(text) == len(expected_text)
    assert text == expected_text
