import asyncio
import math
import subprocess
import sys
import zlib
from gzip import GzipFile
from io import BytesIO
from logging import WARNING
from pathlib import Path
from threading import Thread
from typing import Any

import brotli
import pytest
from w3lib.encoding import resolve_encoding

from scrapy.downloadermiddlewares.httpcompression import (
    ACCEPTED_ENCODINGS,
    HttpCompressionMiddleware,
    HttpCompressionRatioExceeded,
)
from scrapy.exceptions import (
    DecompressionError,
    IgnoreRequest,
    NotConfigured,
    ScrapyDeprecationWarning,
)
from scrapy.http import BodyFileResponse, HtmlResponse, Request, Response
from scrapy.responsetypes import responsetypes
from scrapy.spiders import Spider
from scrapy.utils._compression import (
    _CHUNK_SIZE,
    _DecompressionMaxSizeExceeded,
    _inflate,
    gunzip,
)
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests import tests_datadir
from tests.utils.decorators import coroutine_test

if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd

SAMPLEDIR = Path(tests_datadir, "compressed")

FORMAT = {
    "gzip": ("html-gzip.bin", "gzip"),
    "x-gzip": ("html-gzip.bin", "x-gzip"),
    "rawdeflate": ("html-rawdeflate.bin", "deflate"),
    "zlibdeflate": ("html-zlibdeflate.bin", "deflate"),
    "gzip-deflate": ("html-gzip-deflate.bin", "gzip, deflate"),
    "gzip-deflate-gzip": ("html-gzip-deflate-gzip.bin", "gzip, deflate, gzip"),
    "br": ("html-br.bin", "br"),
    # $ zstd raw.html --content-size -o html-zstd-static-content-size.bin
    "zstd-static-content-size": ("html-zstd-static-content-size.bin", "zstd"),
    # $ zstd raw.html --no-content-size -o html-zstd-static-no-content-size.bin
    "zstd-static-no-content-size": ("html-zstd-static-no-content-size.bin", "zstd"),
    # $ cat raw.html | zstd -o html-zstd-streaming-no-content-size.bin
    "zstd-streaming-no-content-size": (
        "html-zstd-streaming-no-content-size.bin",
        "zstd",
    ),
    **{
        f"bomb-{format_id}": (f"bomb-{format_id}.bin", format_id)
        for format_id in (
            "br",  # 34 → 11 511 612
            "deflate",  # 27 968 → 11 511 612
            "gzip",  # 27 988 → 11 511 612
            "zstd",  # 1 096 → 11 511 612
        )
    },
}


class TestHttpCompression:
    def setup_method(self):
        self.crawler = get_crawler(Spider)
        self.mw = build_from_crawler(HttpCompressionMiddleware, self.crawler)
        self.crawler.stats.open_spider()

    def _mw(self, max_encodings: int) -> HttpCompressionMiddleware:
        crawler = get_crawler(
            Spider, settings_dict={"COMPRESSION_MAX_ENCODINGS": max_encodings}
        )
        return build_from_crawler(HttpCompressionMiddleware, crawler)

    def _getresponse(self, coding: str) -> Response:
        if coding not in FORMAT:
            raise ValueError

        samplefile, contentencoding = FORMAT[coding]

        body = (SAMPLEDIR / samplefile).read_bytes()

        headers = {
            "Server": "Yaws/1.49 Yet Another Web Server",
            "Date": "Sun, 08 Mar 2009 00:41:03 GMT",
            "Content-Length": len(body),
            "Content-Type": "text/html",
            "Content-Encoding": contentencoding,
        }

        response = Response("http://scrapytest.org/", body=body, headers=headers)
        response.request = Request(
            "http://scrapytest.org", headers={"Accept-Encoding": "gzip, deflate"}
        )
        return response

    def assertStatsEqual(self, key: str, value: Any) -> None:
        assert self.crawler.stats.get_value(key) == value, str(
            self.crawler.stats.get_stats()
        )

    def test_setting_false_compression_enabled(self):
        with pytest.raises(NotConfigured):
            build_from_crawler(
                HttpCompressionMiddleware,
                get_crawler(settings_dict={"COMPRESSION_ENABLED": False}),
            )

    def test_setting_default_compression_enabled(self):
        assert isinstance(
            build_from_crawler(HttpCompressionMiddleware, get_crawler()),
            HttpCompressionMiddleware,
        )

    def test_setting_true_compression_enabled(self):
        assert isinstance(
            build_from_crawler(
                HttpCompressionMiddleware,
                get_crawler(settings_dict={"COMPRESSION_ENABLED": True}),
            ),
            HttpCompressionMiddleware,
        )

    @coroutine_test
    async def test_no_crawler_constructor(self):
        with pytest.warns(ScrapyDeprecationWarning, match="HttpCompressionMiddleware"):
            mw = HttpCompressionMiddleware()
        buf = BytesIO()
        with GzipFile(fileobj=buf, mode="wb") as f:
            f.write(b"hello")
        body = buf.getvalue()
        request = Request("http://scrapytest.org")
        response = Response(
            "http://scrapytest.org",
            body=body,
            headers={"Content-Encoding": "gzip"},
        )
        newresponse = await mw.process_response(request, response)
        assert newresponse.body == b"hello"

    def test_process_request(self):
        request = Request("http://scrapytest.org")
        assert "Accept-Encoding" not in request.headers
        self.mw.process_request(request)
        assert request.headers.get("Accept-Encoding") == b", ".join(ACCEPTED_ENCODINGS)

    def test_process_request_body_file(self):
        request = Request("https://example.com", meta={"body_file": "body"})
        self.mw.process_request(request)
        assert "Accept-Encoding" not in request.headers

    @coroutine_test
    async def test_process_response_body_file(self):
        request = Request("https://example.com", meta={"body_file": "body"})
        response = BodyFileResponse(
            "https://example.com",
            headers={"Content-Encoding": "gzip"},
            body_file="body",
            request=request,
        )
        assert await self.mw.process_response(request, response) is response

    @coroutine_test
    async def test_process_response_gzip(self):
        response = self._getresponse("gzip")
        assert response.request
        request = response.request

        assert response.headers["Content-Encoding"] == b"gzip"
        newresponse = await self.mw.process_response(request, response)
        assert newresponse is not response
        assert newresponse.body.startswith(b"<!DOCTYPE")
        assert "Content-Encoding" not in newresponse.headers
        self.assertStatsEqual("httpcompression/response_count", 1)
        self.assertStatsEqual("httpcompression/response_bytes", 74837)

    @coroutine_test
    async def test_process_response_br(self):
        response = self._getresponse("br")
        assert response.request
        request = response.request
        assert response.headers["Content-Encoding"] == b"br"
        newresponse = await self.mw.process_response(request, response)
        assert newresponse is not response
        assert newresponse.body.startswith(b"<!DOCTYPE")
        assert "Content-Encoding" not in newresponse.headers
        self.assertStatsEqual("httpcompression/response_count", 1)
        self.assertStatsEqual("httpcompression/response_bytes", 74837)

    @coroutine_test
    async def test_process_response_zstd(self):
        raw_content = None
        for check_key in FORMAT:
            if not check_key.startswith("zstd-"):
                continue
            response = self._getresponse(check_key)
            assert response.request
            request = response.request
            assert response.headers["Content-Encoding"] == b"zstd"
            newresponse = await self.mw.process_response(request, response)
            if raw_content is None:
                raw_content = newresponse.body
            else:
                assert raw_content == newresponse.body
            assert newresponse is not response
            assert newresponse.body.startswith(b"<!DOCTYPE")
            assert "Content-Encoding" not in newresponse.headers

    @coroutine_test
    async def test_process_response_rawdeflate(self):
        response = self._getresponse("rawdeflate")
        assert response.request
        request = response.request

        assert response.headers["Content-Encoding"] == b"deflate"
        newresponse = await self.mw.process_response(request, response)
        assert newresponse is not response
        assert newresponse.body.startswith(b"<!DOCTYPE")
        assert "Content-Encoding" not in newresponse.headers
        self.assertStatsEqual("httpcompression/response_count", 1)
        self.assertStatsEqual("httpcompression/response_bytes", 74840)

    @coroutine_test
    async def test_process_response_zlibdelate(self):
        response = self._getresponse("zlibdeflate")
        assert response.request
        request = response.request

        assert response.headers["Content-Encoding"] == b"deflate"
        newresponse = await self.mw.process_response(request, response)
        assert newresponse is not response
        assert newresponse.body.startswith(b"<!DOCTYPE")
        assert "Content-Encoding" not in newresponse.headers
        self.assertStatsEqual("httpcompression/response_count", 1)
        self.assertStatsEqual("httpcompression/response_bytes", 74840)

    @coroutine_test
    async def test_process_response_plain(self):
        response = Response("http://scrapytest.org", body=b"<!DOCTYPE...")
        request = Request("http://scrapytest.org")

        assert not response.headers.get("Content-Encoding")
        newresponse = await self.mw.process_response(request, response)
        assert newresponse is response
        assert newresponse.body.startswith(b"<!DOCTYPE")
        self.assertStatsEqual("httpcompression/response_count", None)
        self.assertStatsEqual("httpcompression/response_bytes", None)

    @coroutine_test
    async def test_multipleencodings(self):
        response = self._getresponse("gzip")
        response.headers["Content-Encoding"] = ["uuencode", "gzip"]
        assert response.request
        request = response.request
        newresponse = await self.mw.process_response(request, response)
        assert newresponse is not response
        assert newresponse.headers.getlist("Content-Encoding") == [b"uuencode"]

    @coroutine_test
    async def test_multi_compression_single_header(self):
        response = self._getresponse("gzip-deflate")
        assert response.request
        request = response.request
        newresponse = await self._mw(2).process_response(request, response)
        assert newresponse is not response
        assert "Content-Encoding" not in newresponse.headers
        assert newresponse.body.startswith(b"<!DOCTYPE")

    @coroutine_test
    async def test_multi_compression_single_header_invalid_compression(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        response = self._getresponse("gzip-deflate")
        response.headers["Content-Encoding"] = [b"gzip, foo, deflate"]
        assert response.request
        request = response.request
        caplog.clear()
        with caplog.at_level(
            WARNING, logger="scrapy.downloadermiddlewares.httpcompression"
        ):
            newresponse = await self.mw.process_response(request, response)
        assert caplog.record_tuples == [
            (
                "scrapy.downloadermiddlewares.httpcompression",
                WARNING,
                (
                    "HttpCompressionMiddleware cannot decode the response for"
                    " http://scrapytest.org/ from unsupported encoding(s) 'gzip,foo'."
                ),
            ),
        ]
        assert newresponse is not response
        assert newresponse.headers.getlist("Content-Encoding") == [b"gzip", b"foo"]

    @coroutine_test
    async def test_multi_compression_multiple_header(self):
        response = self._getresponse("gzip-deflate")
        response.headers["Content-Encoding"] = ["gzip", "deflate"]
        assert response.request
        request = response.request
        newresponse = await self._mw(2).process_response(request, response)
        assert newresponse is not response
        assert "Content-Encoding" not in newresponse.headers
        assert newresponse.body.startswith(b"<!DOCTYPE")

    @coroutine_test
    async def test_multi_compression_multiple_header_invalid_compression(self):
        response = self._getresponse("gzip-deflate")
        response.headers["Content-Encoding"] = ["gzip", "foo", "deflate"]
        assert response.request
        request = response.request
        newresponse = await self.mw.process_response(request, response)
        assert newresponse is not response
        assert newresponse.headers.getlist("Content-Encoding") == [b"gzip", b"foo"]

    @coroutine_test
    async def test_multi_compression_single_and_multiple_header(self):
        response = self._getresponse("gzip-deflate-gzip")
        response.headers["Content-Encoding"] = ["gzip", "deflate, gzip"]
        assert response.request
        request = response.request
        newresponse = await self._mw(3).process_response(request, response)
        assert newresponse is not response
        assert "Content-Encoding" not in newresponse.headers
        assert newresponse.body.startswith(b"<!DOCTYPE")

    @coroutine_test
    async def test_multi_compression_single_and_multiple_header_invalid_compression(
        self,
    ):
        response = self._getresponse("gzip-deflate")
        response.headers["Content-Encoding"] = ["gzip", "foo,deflate"]
        assert response.request
        request = response.request
        newresponse = await self.mw.process_response(request, response)
        assert newresponse is not response
        assert newresponse.headers.getlist("Content-Encoding") == [b"gzip", b"foo"]

    @coroutine_test
    async def test_max_encodings_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        response = self._getresponse("gzip-deflate")
        assert response.request
        request = response.request
        with (
            caplog.at_level(
                WARNING, logger="scrapy.downloadermiddlewares.httpcompression"
            ),
            pytest.raises(IgnoreRequest),
        ):
            await self.mw.process_response(request, response)
        assert "COMPRESSION_MAX_ENCODINGS (1)" in caplog.text

    @coroutine_test
    async def test_max_encodings_rejects_before_decompressing(self) -> None:
        response = self._getresponse("gzip")
        response.headers["Content-Encoding"] = [b", ".join([b"gzip"] * 5000)]
        assert response.request
        request = response.request
        with pytest.raises(IgnoreRequest, match="lists 5000 supported encodings"):
            await self.mw.process_response(request, response)

    @coroutine_test
    async def test_process_response_encoding_inside_body(self):
        headers = {
            "Content-Type": "text/html",
            "Content-Encoding": "gzip",
        }
        f = BytesIO()
        plainbody = (
            b"<html><head><title>Some page</title>"
            b'<meta http-equiv="Content-Type" content="text/html; charset=gb2312">'
        )
        zf = GzipFile(fileobj=f, mode="wb")
        zf.write(plainbody)
        zf.close()
        response = Response(
            "http://www.example.com/", headers=headers, body=f.getvalue()
        )
        request = Request("http://www.example.com/")

        newresponse = await self.mw.process_response(request, response)
        assert isinstance(newresponse, HtmlResponse)
        assert newresponse.body == plainbody
        assert newresponse.encoding == resolve_encoding("gb2312")
        self.assertStatsEqual("httpcompression/response_count", 1)
        self.assertStatsEqual("httpcompression/response_bytes", len(plainbody))

    @coroutine_test
    async def test_process_response_force_recalculate_encoding(self):
        headers = {
            "Content-Type": "text/html",
            "Content-Encoding": "gzip",
        }
        f = BytesIO()
        plainbody = (
            b"<html><head><title>Some page</title>"
            b'<meta http-equiv="Content-Type" content="text/html; charset=gb2312">'
        )
        zf = GzipFile(fileobj=f, mode="wb")
        zf.write(plainbody)
        zf.close()
        response = HtmlResponse(
            "http://www.example.com/page.html", headers=headers, body=f.getvalue()
        )
        request = Request("http://www.example.com/")

        newresponse = await self.mw.process_response(request, response)
        assert isinstance(newresponse, HtmlResponse)
        assert newresponse.body == plainbody
        assert newresponse.encoding == resolve_encoding("gb2312")
        self.assertStatsEqual("httpcompression/response_count", 1)
        self.assertStatsEqual("httpcompression/response_bytes", len(plainbody))

    @coroutine_test
    async def test_process_response_no_content_type_header(self):
        headers = {b"Content-Encoding": b"identity"}
        plainbody = (
            b"<html><head><title>Some page</title>"
            b'<meta http-equiv="Content-Type" content="text/html; charset=gb2312">'
        )
        respcls = responsetypes.from_args(
            url="http://www.example.com/index", headers=headers, body=plainbody
        )
        response = respcls(
            "http://www.example.com/index", headers=headers, body=plainbody
        )
        request = Request("http://www.example.com/index")

        newresponse = await self.mw.process_response(request, response)
        assert isinstance(newresponse, respcls)
        assert isinstance(newresponse, HtmlResponse)
        assert newresponse.body == plainbody
        assert newresponse.encoding == resolve_encoding("gb2312")
        self.assertStatsEqual("httpcompression/response_count", 1)
        self.assertStatsEqual("httpcompression/response_bytes", len(plainbody))

    @coroutine_test
    async def test_process_response_gzipped_contenttype(self):
        response = self._getresponse("gzip")
        response.headers["Content-Type"] = "application/gzip"
        assert response.request
        request = response.request

        newresponse = await self.mw.process_response(request, response)
        assert newresponse is not response
        assert newresponse.body.startswith(b"<!DOCTYPE")
        assert "Content-Encoding" not in newresponse.headers
        self.assertStatsEqual("httpcompression/response_count", 1)
        self.assertStatsEqual("httpcompression/response_bytes", 74837)

    @coroutine_test
    async def test_process_response_gzip_app_octetstream_contenttype(self):
        response = self._getresponse("gzip")
        response.headers["Content-Type"] = "application/octet-stream"
        assert response.request
        request = response.request

        newresponse = await self.mw.process_response(request, response)
        assert newresponse is not response
        assert newresponse.body.startswith(b"<!DOCTYPE")
        assert "Content-Encoding" not in newresponse.headers
        self.assertStatsEqual("httpcompression/response_count", 1)
        self.assertStatsEqual("httpcompression/response_bytes", 74837)

    @coroutine_test
    async def test_process_response_gzip_binary_octetstream_contenttype(self):
        response = self._getresponse("x-gzip")
        response.headers["Content-Type"] = "binary/octet-stream"
        assert response.request
        request = response.request

        newresponse = await self.mw.process_response(request, response)
        assert newresponse is not response
        assert newresponse.body.startswith(b"<!DOCTYPE")
        assert "Content-Encoding" not in newresponse.headers
        self.assertStatsEqual("httpcompression/response_count", 1)
        self.assertStatsEqual("httpcompression/response_bytes", 74837)

    @coroutine_test
    async def test_process_response_gzipped_gzip_file(self):
        """Test that a gzip Content-Encoded .gz file is gunzipped
        only once by the middleware, leaving gunzipping of the file
        to upper layers.
        """
        headers = {
            "Content-Type": "application/gzip",
            "Content-Encoding": "gzip",
        }
        # build a gzipped file (here, a sitemap)
        f = BytesIO()
        plainbody = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.google.com/schemas/sitemap/0.84">
  <url>
    <loc>http://www.example.com/</loc>
    <lastmod>2009-08-16</lastmod>
    <changefreq>daily</changefreq>
    <priority>1</priority>
  </url>
  <url>
    <loc>http://www.example.com/Special-Offers.html</loc>
    <lastmod>2009-08-16</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>"""
        gz_file = GzipFile(fileobj=f, mode="wb")
        gz_file.write(plainbody)
        gz_file.close()

        # build a gzipped response body containing this gzipped file
        r = BytesIO()
        gz_resp = GzipFile(fileobj=r, mode="wb")
        gz_resp.write(f.getvalue())
        gz_resp.close()

        response = Response(
            "http://www.example.com/", headers=headers, body=r.getvalue()
        )
        request = Request("http://www.example.com/")

        newresponse = await self.mw.process_response(request, response)
        assert gunzip(newresponse.body) == plainbody
        self.assertStatsEqual("httpcompression/response_count", 1)
        self.assertStatsEqual("httpcompression/response_bytes", len(f.getvalue()))

    @coroutine_test
    async def test_process_response_head_request_no_decode_required(self):
        response = self._getresponse("gzip")
        response.headers["Content-Type"] = "application/gzip"
        assert response.request
        request = response.request
        request.method = "HEAD"
        response = response.replace(body=None)
        newresponse = await self.mw.process_response(request, response)
        assert newresponse is response
        assert response.body == b""
        self.assertStatsEqual("httpcompression/response_count", None)
        self.assertStatsEqual("httpcompression/response_bytes", None)

    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param(b"garbage", id="trailing-garbage"),
            pytest.param(zlib.compress(b"b" * 100_000), id="second-zlib-stream"),
        ],
    )
    def test_deflate_extra_data_after_stream(self, extra: bytes) -> None:
        """Bytes after the end of a deflate stream must not stall decompression.

        Such bytes stay in ``unconsumed_tail`` and produce no output, so feeding
        them back to the decompressor never makes progress. Decompressing in a
        thread lets the test fail instead of hanging if that ever regresses.
        """
        # The body must decompress to more than _CHUNK_SIZE, or the whole stream
        # is consumed by the first decompress() call and no loop is entered.
        plain = b"a" * 100_000
        response = Response(
            "http://example.com",
            body=zlib.compress(plain) + extra,
            headers={"Content-Encoding": "deflate"},
        )
        result: list[Request | Response] = []
        thread = Thread(
            target=lambda: result.append(
                asyncio.run(
                    self.mw.process_response(Request("http://example.com"), response)
                )
            ),
            daemon=True,
        )
        thread.start()
        thread.join(60)
        assert not thread.is_alive(), "decompression did not terminate"
        assert result[0].body == plain

    @pytest.mark.parametrize(
        "size", [_CHUNK_SIZE - 1, _CHUNK_SIZE, _CHUNK_SIZE + 1, 4 * _CHUNK_SIZE]
    )
    def test_deflate_chunked_output_leaves_nothing_to_flush(self, size: int) -> None:
        """Guard the zlib behaviour that lets ``_inflate()`` skip ``flush()``.

        ``decompress(max_length=...)`` leaves the input it could not turn into
        output in ``unconsumed_tail``, so once that is empty every complete
        stream has been fully emitted. If a future zlib buffers output instead,
        this fails and ``_inflate()`` needs to flush the remainder again.
        """
        decompressor = zlib.decompressobj()
        decompressor.decompress(zlib.compress(b"a" * size), max_length=_CHUNK_SIZE)
        while decompressor.unconsumed_tail and not decompressor.eof:
            decompressor.decompress(
                decompressor.unconsumed_tail, max_length=_CHUNK_SIZE
            )
        assert decompressor.flush() == b""

    async def _test_compression_bomb_setting(self, compression_id: str) -> None:
        settings = {"DOWNLOAD_MAXSIZE": 1_000_000}
        crawler = get_crawler(Spider, settings_dict=settings)
        spider = crawler._create_spider("scrapytest.org")
        mw = build_from_crawler(HttpCompressionMiddleware, crawler)
        mw.open_spider(spider)

        response = self._getresponse(f"bomb-{compression_id}")  # 11_511_612 B
        assert response.request
        with pytest.raises(IgnoreRequest) as exc_info:
            await mw.process_response(response.request, response)
        cause = exc_info.value.__cause__
        assert isinstance(cause, _DecompressionMaxSizeExceeded)
        assert cause.decompressed_size < 1_100_000
        assert str(cause) == (
            f"The number of bytes decompressed so far ({cause.decompressed_size} B) "
            "exceeded the specified maximum (1000000 B)."
        )

    @coroutine_test
    async def test_compression_bomb_setting_br(self):
        await self._test_compression_bomb_setting("br")

    @coroutine_test
    async def test_compression_bomb_setting_deflate(self):
        await self._test_compression_bomb_setting("deflate")

    @coroutine_test
    async def test_compression_bomb_setting_gzip(self):
        await self._test_compression_bomb_setting("gzip")

    @coroutine_test
    async def test_compression_bomb_setting_zstd(self):
        await self._test_compression_bomb_setting("zstd")

    @coroutine_test
    async def test_compression_bomb_setting_logs_warning(self, caplog):
        settings = {"DOWNLOAD_MAXSIZE": 1_000_000}
        crawler = get_crawler(Spider, settings_dict=settings)
        spider = crawler._create_spider("scrapytest.org")
        mw = build_from_crawler(HttpCompressionMiddleware, crawler)
        mw.open_spider(spider)

        response = self._getresponse("bomb-gzip")  # 11_511_612 B
        assert response.request
        caplog.clear()
        with (
            caplog.at_level(
                WARNING, logger="scrapy.downloadermiddlewares.httpcompression"
            ),
            pytest.raises(IgnoreRequest) as exc_info,
        ):
            await mw.process_response(response.request, response)
        assert caplog.record_tuples == [
            (
                "scrapy.downloadermiddlewares.httpcompression",
                WARNING,
                str(exc_info.value),
            )
        ]

    async def _test_compression_bomb_spider_attr(self, compression_id: str) -> None:
        class DownloadMaxSizeSpider(Spider):
            download_maxsize = 1_000_000

        crawler = get_crawler(DownloadMaxSizeSpider)
        spider = crawler._create_spider("scrapytest.org")
        mw = build_from_crawler(HttpCompressionMiddleware, crawler)
        mw.open_spider(spider)

        response = self._getresponse(f"bomb-{compression_id}")
        assert response.request
        with pytest.raises(IgnoreRequest) as exc_info:
            await mw.process_response(response.request, response)
        cause = exc_info.value.__cause__
        assert isinstance(cause, _DecompressionMaxSizeExceeded)
        assert cause.decompressed_size < 1_100_000

    @pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
    @coroutine_test
    async def test_compression_bomb_spider_attr_br(self):
        await self._test_compression_bomb_spider_attr("br")

    @pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
    @coroutine_test
    async def test_compression_bomb_spider_attr_deflate(self):
        await self._test_compression_bomb_spider_attr("deflate")

    @pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
    @coroutine_test
    async def test_compression_bomb_spider_attr_gzip(self):
        await self._test_compression_bomb_spider_attr("gzip")

    @pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
    @coroutine_test
    async def test_compression_bomb_spider_attr_zstd(self):
        await self._test_compression_bomb_spider_attr("zstd")

    async def _test_compression_bomb_request_meta(self, compression_id: str) -> None:
        crawler = get_crawler(Spider)
        spider = crawler._create_spider("scrapytest.org")
        mw = build_from_crawler(HttpCompressionMiddleware, crawler)
        mw.open_spider(spider)

        response = self._getresponse(f"bomb-{compression_id}")
        response.meta["download_maxsize"] = 1_000_000
        assert response.request
        with pytest.raises(IgnoreRequest) as exc_info:
            await mw.process_response(response.request, response)
        cause = exc_info.value.__cause__
        assert isinstance(cause, _DecompressionMaxSizeExceeded)
        assert cause.decompressed_size < 1_100_000

    @coroutine_test
    async def test_compression_bomb_request_meta_br(self):
        await self._test_compression_bomb_request_meta("br")

    @coroutine_test
    async def test_compression_bomb_request_meta_deflate(self):
        await self._test_compression_bomb_request_meta("deflate")

    @coroutine_test
    async def test_compression_bomb_request_meta_gzip(self):
        await self._test_compression_bomb_request_meta("gzip")

    @coroutine_test
    async def test_compression_bomb_request_meta_zstd(self):
        await self._test_compression_bomb_request_meta("zstd")

    async def _test_download_warnsize_setting(
        self, caplog: pytest.LogCaptureFixture, compression_id: str
    ) -> None:
        settings = {"DOWNLOAD_WARNSIZE": 10_000_000}
        crawler = get_crawler(Spider, settings_dict=settings)
        spider = crawler._create_spider("scrapytest.org")
        mw = build_from_crawler(HttpCompressionMiddleware, crawler)
        mw.open_spider(spider)
        response = self._getresponse(f"bomb-{compression_id}")

        assert response.request
        caplog.clear()
        with caplog.at_level(
            WARNING, logger="scrapy.downloadermiddlewares.httpcompression"
        ):
            await mw.process_response(response.request, response)
        assert caplog.record_tuples == [
            (
                "scrapy.downloadermiddlewares.httpcompression",
                WARNING,
                (
                    "<200 http://scrapytest.org/> body size after "
                    "decompression (11511612 B) is larger than the download "
                    "warning size (10000000 B)."
                ),
            ),
        ]

    @coroutine_test
    async def test_download_warnsize_setting_br(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._test_download_warnsize_setting(caplog, "br")

    @coroutine_test
    async def test_download_warnsize_setting_deflate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._test_download_warnsize_setting(caplog, "deflate")

    @coroutine_test
    async def test_download_warnsize_setting_gzip(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._test_download_warnsize_setting(caplog, "gzip")

    @coroutine_test
    async def test_download_warnsize_setting_zstd(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._test_download_warnsize_setting(caplog, "zstd")

    async def _test_download_warnsize_spider_attr(
        self, caplog: pytest.LogCaptureFixture, compression_id: str
    ) -> None:
        class DownloadWarnSizeSpider(Spider):
            download_warnsize = 10_000_000

        crawler = get_crawler(DownloadWarnSizeSpider)
        spider = crawler._create_spider("scrapytest.org")
        mw = build_from_crawler(HttpCompressionMiddleware, crawler)
        mw.open_spider(spider)
        response = self._getresponse(f"bomb-{compression_id}")

        assert response.request
        caplog.clear()
        with caplog.at_level(
            WARNING, logger="scrapy.downloadermiddlewares.httpcompression"
        ):
            await mw.process_response(response.request, response)
        assert caplog.record_tuples == [
            (
                "scrapy.downloadermiddlewares.httpcompression",
                WARNING,
                (
                    "<200 http://scrapytest.org/> body size after "
                    "decompression (11511612 B) is larger than the download "
                    "warning size (10000000 B)."
                ),
            ),
        ]

    @pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
    @coroutine_test
    async def test_download_warnsize_spider_attr_br(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._test_download_warnsize_spider_attr(caplog, "br")

    @pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
    @coroutine_test
    async def test_download_warnsize_spider_attr_deflate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._test_download_warnsize_spider_attr(caplog, "deflate")

    @pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
    @coroutine_test
    async def test_download_warnsize_spider_attr_gzip(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._test_download_warnsize_spider_attr(caplog, "gzip")

    @pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
    @coroutine_test
    async def test_download_warnsize_spider_attr_zstd(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._test_download_warnsize_spider_attr(caplog, "zstd")

    async def _test_download_warnsize_request_meta(
        self, caplog: pytest.LogCaptureFixture, compression_id: str
    ) -> None:
        crawler = get_crawler(Spider)
        spider = crawler._create_spider("scrapytest.org")
        mw = build_from_crawler(HttpCompressionMiddleware, crawler)
        mw.open_spider(spider)
        response = self._getresponse(f"bomb-{compression_id}")
        response.meta["download_warnsize"] = 10_000_000

        assert response.request
        caplog.clear()
        with caplog.at_level(
            WARNING, logger="scrapy.downloadermiddlewares.httpcompression"
        ):
            await mw.process_response(response.request, response)
        assert caplog.record_tuples == [
            (
                "scrapy.downloadermiddlewares.httpcompression",
                WARNING,
                (
                    "<200 http://scrapytest.org/> body size after "
                    "decompression (11511612 B) is larger than the download "
                    "warning size (10000000 B)."
                ),
            ),
        ]

    @coroutine_test
    async def test_download_warnsize_setting_ignored_with_request_maxsize(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = {"DOWNLOAD_WARNSIZE": 10_000_000}
        crawler = get_crawler(Spider, settings_dict=settings)
        spider = crawler._create_spider("scrapytest.org")
        mw = build_from_crawler(HttpCompressionMiddleware, crawler)
        mw.open_spider(spider)
        response = self._getresponse("bomb-gzip")
        response.meta["download_maxsize"] = 20_000_000

        assert response.request
        caplog.clear()
        with caplog.at_level(
            WARNING, logger="scrapy.downloadermiddlewares.httpcompression"
        ):
            await mw.process_response(response.request, response)
        assert not caplog.records

    @coroutine_test
    async def test_download_warnsize_request_meta_br(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._test_download_warnsize_request_meta(caplog, "br")

    @coroutine_test
    async def test_download_warnsize_request_meta_deflate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._test_download_warnsize_request_meta(caplog, "deflate")

    @coroutine_test
    async def test_download_warnsize_request_meta_gzip(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._test_download_warnsize_request_meta(caplog, "gzip")

    @coroutine_test
    async def test_download_warnsize_request_meta_zstd(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        await self._test_download_warnsize_request_meta(caplog, "zstd")

    async def _get_truncated_response(self, compression_id: str) -> Response:
        crawler = get_crawler(Spider)
        spider = crawler._create_spider("scrapytest.org")
        mw = build_from_crawler(HttpCompressionMiddleware, crawler)
        mw.open_spider(spider)
        response = self._getresponse(compression_id)
        truncated_body = response.body[: len(response.body) // 2]
        response = response.replace(body=truncated_body)
        assert response.request
        new_response = await mw.process_response(response.request, response)
        assert isinstance(new_response, Response)
        return new_response

    @coroutine_test
    async def test_process_truncated_response_br(self):
        resp = await self._get_truncated_response("br")
        assert resp.body.startswith(b"<!DOCTYPE")

    @coroutine_test
    async def test_process_truncated_response_zlibdeflate(self):
        resp = await self._get_truncated_response("zlibdeflate")
        assert resp.body.startswith(b"<!DOCTYPE")

    @coroutine_test
    async def test_process_truncated_response_gzip(self):
        resp = await self._get_truncated_response("gzip")
        assert resp.body.startswith(b"<!DOCTYPE")

    @pytest.mark.parametrize(
        "check_key", [key for key in FORMAT if key.startswith("zstd-")]
    )
    @coroutine_test
    async def test_process_truncated_response_zstd(self, check_key: str) -> None:
        resp = await self._get_truncated_response(check_key)
        assert len(resp.body) == 0

    @pytest.mark.parametrize(
        ("body", "encoding"),
        [
            (b"not compressed", "gzip"),
            (b"\x1f\x8b\x08\x00", "gzip"),
            (b"\x1f\x8b\x08" + b"\x00" * 7 + b"\xff" * 20, "gzip"),
            (b"not compressed", "deflate"),
            (b"not compressed", "br"),
            (b"not compressed", "zstd"),
        ],
    )
    @coroutine_test
    async def test_process_response_invalid(self, body: bytes, encoding: str) -> None:
        request = Request("http://example.com")
        response = Response(
            "http://example.com", body=body, headers={"Content-Encoding": encoding}
        )
        with pytest.raises(DecompressionError):
            await self.mw.process_response(request, response)




def _gzip_bytes(data: bytes, *, level: int = 9) -> bytes:
    buf = BytesIO()
    with GzipFile(fileobj=buf, mode="wb", compresslevel=level) as f:
        f.write(data)
    return buf.getvalue()


def _compress(codec: bytes, data: bytes) -> bytes:
    if codec in (b"gzip", b"x-gzip"):
        return _gzip_bytes(data)
    if codec == b"deflate":
        return zlib.compress(data, 9)
    if codec == b"br":
        return brotli.compress(data, quality=11)
    if codec == b"zstd":
        return zstd.compress(data, level=22)
    raise ValueError(codec)


class TestCompressionRatio:
    """Tests for COMPRESSION_MAX_RATIO / COMPRESSION_RATIO_MIN_SIZE."""

    def _mw(self, **settings: Any) -> HttpCompressionMiddleware:
        crawler = get_crawler(Spider, settings_dict=settings)
        spider = crawler._create_spider("scrapytest.org")
        mw = build_from_crawler(HttpCompressionMiddleware, crawler)
        mw.open_spider(spider)
        crawler.stats.open_spider()
        return mw

    @staticmethod
    def _response(body: bytes, encoding: str = "gzip") -> Response:
        response = Response(
            "http://scrapytest.org/",
            body=body,
            headers={"Content-Encoding": encoding},
        )
        response.request = Request("http://scrapytest.org")
        return response

    # --- setting validation -------------------------------------------------

    @pytest.mark.parametrize(
        "value",
        [
            True,
            False,
            0,
            0.0,
            -1.0,
            float("nan"),
            float("inf"),
            float("-inf"),
            "100.0",
            [100.0],
        ],
    )
    def test_invalid_max_ratio(self, value: Any) -> None:
        with pytest.raises(ValueError, match="COMPRESSION_MAX_RATIO"):
            self._mw(COMPRESSION_MAX_RATIO=value)

    @pytest.mark.parametrize("value", [1, 1.0, 2.5, 100.0, 1_000_000.0])
    def test_valid_max_ratio(self, value: float) -> None:
        mw = self._mw(COMPRESSION_MAX_RATIO=value)
        assert mw._max_ratio == float(value)

    def test_max_ratio_disabled_by_default(self) -> None:
        mw = self._mw()
        assert mw._max_ratio is None
        assert mw._ratio_min_size == 1024

    @pytest.mark.parametrize("value", [True, False, -1, 1.5, "1024"])
    def test_invalid_ratio_min_size(self, value: Any) -> None:
        # Note: None is normalized to the default by Settings.
        with pytest.raises(ValueError, match="COMPRESSION_RATIO_MIN_SIZE"):
            self._mw(COMPRESSION_RATIO_MIN_SIZE=value)

    @pytest.mark.parametrize("value", [0, 1, 1024, 10_000_000])
    def test_valid_ratio_min_size(self, value: int) -> None:
        mw = self._mw(COMPRESSION_RATIO_MIN_SIZE=value)
        assert mw._ratio_min_size == value

    # --- disabled by default / threshold exemption --------------------------

    @coroutine_test
    async def test_high_ratio_allowed_when_disabled(self) -> None:
        body = _gzip_bytes(b"a" * 200_000)
        response = self._response(body)
        mw = self._mw()
        new_response = await mw.process_response(response.request, response)
        assert new_response.body == b"a" * 200_000
        assert mw.stats.get_value("httpcompression/ratio_rejected") is None

    @coroutine_test
    async def test_below_min_size_not_checked(self) -> None:
        # Even at an extreme ratio, a body below the minimum size goes through.
        body = _gzip_bytes(b"a" * 4_000)
        assert len(body) < 1024
        response = self._response(body)
        mw = self._mw(COMPRESSION_MAX_RATIO=1.0, COMPRESSION_RATIO_MIN_SIZE=1024)
        new_response = await mw.process_response(response.request, response)
        assert new_response.body == b"a" * 4_000
        assert mw.stats.get_value("httpcompression/ratio_rejected") is None

    @coroutine_test
    async def test_min_size_zero_checks_nonempty_inputs(self) -> None:
        body = _gzip_bytes(b"ab" * 50)
        assert len(body) > 0
        response = self._response(body)
        mw = self._mw(COMPRESSION_MAX_RATIO=1.0, COMPRESSION_RATIO_MIN_SIZE=0)
        with pytest.raises(HttpCompressionRatioExceeded):
            await mw.process_response(response.request, response)

    # --- exact boundaries ---------------------------------------------------

    @coroutine_test
    async def test_ratio_boundary_accepted_and_rejected(self) -> None:
        plain = b"a" * 5_000_000
        body = _gzip_bytes(plain)
        compressed_size = len(body)
        assert compressed_size >= 1024
        exact_ratio = len(plain) / compressed_size

        accepted = self._response(body)
        mw_ok = self._mw(
            COMPRESSION_MAX_RATIO=exact_ratio, COMPRESSION_RATIO_MIN_SIZE=1024
        )
        new_response = await mw_ok.process_response(accepted.request, accepted)
        assert new_response.body == plain

        rejected = self._response(body)
        mw_bad = self._mw(
            COMPRESSION_MAX_RATIO=exact_ratio - 1e-12,
            COMPRESSION_RATIO_MIN_SIZE=1024,
        )
        with pytest.raises(HttpCompressionRatioExceeded) as exc_info:
            await mw_bad.process_response(rejected.request, rejected)
        assert isinstance(exc_info.value, IgnoreRequest)
        cause = exc_info.value.__cause__
        assert cause.__class__.__name__ == "_CompressionRatioExceeded"
        assert math.isclose(cause.ratio, exact_ratio)
        assert cause.compressed_size == compressed_size
        assert cause.decompressed_size == len(plain)

    @coroutine_test
    async def test_min_size_boundary(self) -> None:
        # A compressed body of exactly COMPRESSION_RATIO_MIN_SIZE bytes is
        # checked; one byte longer threshold exempts the same body.
        plain = b"a" * 50_000
        body = _gzip_bytes(plain)
        min_size = len(body)

        exempt = self._response(body)
        mw_exempt = self._mw(
            COMPRESSION_MAX_RATIO=0.1, COMPRESSION_RATIO_MIN_SIZE=min_size + 1
        )
        new_response = await mw_exempt.process_response(exempt.request, exempt)
        assert new_response.body == plain

        checked = self._response(body)
        mw_checked = self._mw(
            COMPRESSION_MAX_RATIO=0.1, COMPRESSION_RATIO_MIN_SIZE=min_size
        )
        with pytest.raises(HttpCompressionRatioExceeded):
            await mw_checked.process_response(checked.request, checked)

    # --- every supported codec ----------------------------------------------

    @pytest.mark.parametrize(
        ("codec", "encoding"),
        [
            ("gzip", "gzip"),
            ("x-gzip", "x-gzip"),
            ("deflate", "deflate"),
            ("br", "br"),
            ("zstd", "zstd"),
        ],
    )
    @coroutine_test
    async def test_ratio_rejected_each_codec(
        self, codec: str, encoding: str
    ) -> None:
        body = _compress(codec.encode(), b"a" * 200_000)
        response = self._response(body, encoding=encoding)
        mw = self._mw(COMPRESSION_MAX_RATIO=10.0, COMPRESSION_RATIO_MIN_SIZE=0)
        with pytest.raises(HttpCompressionRatioExceeded):
            await mw.process_response(response.request, response)
        assert mw.stats.get_value("httpcompression/ratio_rejected") == 1
        assert mw.stats.get_value("httpcompression/size_rejected") is None

    @pytest.mark.parametrize("codec", ["gzip", "deflate", "br", "zstd"])
    @coroutine_test
    async def test_ratio_accepted_each_codec(self, codec: str) -> None:
        # Deterministic, nearly incompressible data keeps the ratio close to 1.
        plain = bytes((i * 131 + 17) % 256 for i in range(8_192))
        body = _compress(codec.encode(), plain)
        response = self._response(body, encoding=codec)
        mw = self._mw(COMPRESSION_MAX_RATIO=100.0, COMPRESSION_RATIO_MIN_SIZE=0)
        new_response = await mw.process_response(response.request, response)
        assert new_response.body == plain
        assert mw.stats.get_value("httpcompression/response_count") == 1
        assert mw.stats.get_value("httpcompression/ratio_rejected") is None

    # --- empty compressed input ---------------------------------------------

    @pytest.mark.parametrize("codec", ["gzip", "deflate", "br", "zstd"])
    @coroutine_test
    async def test_empty_compressed_input_exempt(self, codec: str) -> None:
        # Zero-byte compressed inputs decode to zero-byte outputs and have no
        # defined ratio (0 / 0): they are accepted even with a tiny budget.
        response = self._response(b"", encoding=codec)
        mw = self._mw(COMPRESSION_MAX_RATIO=0.5, COMPRESSION_RATIO_MIN_SIZE=0)
        new_response = await mw.process_response(response.request, response)
        assert new_response.body == b""
        assert mw.stats.get_value("httpcompression/ratio_rejected") is None

    # --- multi-layer encodings ----------------------------------------------

    @coroutine_test
    async def test_multi_layer_uses_full_wire_input_denominator(self) -> None:
        # gzip(gzip(plain)) where the inner stream is a concatenation of many
        # gzip members. With a per-layer-local denominator both layers pass the
        # budget; with the full wire input as denominator the total expansion
        # rejects it. This is the bypass the full-input denominator closes.
        members = [_gzip_bytes(b"q" * 50_000) for _ in range(30)]
        inner = b"".join(members)
        outer = _gzip_bytes(inner)
        plain = gunzip(inner)
        wire_size = len(outer)
        layer1_ratio = len(inner) / wire_size
        layer2_local_ratio = len(plain) / len(inner)
        total_ratio = len(plain) / wire_size
        # Structural guarantee of the bypass construction.
        assert total_ratio > max(layer1_ratio, layer2_local_ratio)
        budget = math.sqrt(max(layer1_ratio, layer2_local_ratio) * total_ratio)
        assert layer1_ratio <= budget
        assert layer2_local_ratio <= budget
        assert total_ratio > budget

        response = self._response(outer, encoding="gzip, gzip")
        mw = self._mw(
            COMPRESSION_MAX_RATIO=budget,
            COMPRESSION_RATIO_MIN_SIZE=0,
            COMPRESSION_MAX_ENCODINGS=2,
        )
        with pytest.raises(HttpCompressionRatioExceeded) as exc_info:
            await mw.process_response(response.request, response)
        cause = exc_info.value.__cause__
        # The rejection always cites the full wire input as denominator.
        assert cause.compressed_size == wire_size

    @coroutine_test
    async def test_multi_layer_each_layer_checked_against_wire_input(
        self,
    ) -> None:
        # Incompressible inner content: the first layer expands slightly, and
        # that expansion is itself checked against the full wire input.
        plain = bytes((i * 131 + 17) % 256 for i in range(20_000))
        inner = _gzip_bytes(plain, level=1)
        outer = _gzip_bytes(inner, level=9)
        wire_size = len(outer)
        layer1_ratio = len(inner) / wire_size
        total_ratio = len(plain) / wire_size

        # Budget below the first layer's ratio: rejection happens at layer 1.
        response = self._response(outer, encoding="gzip, gzip")
        mw = self._mw(
            COMPRESSION_MAX_RATIO=layer1_ratio - 1e-9,
            COMPRESSION_RATIO_MIN_SIZE=0,
            COMPRESSION_MAX_ENCODINGS=2,
        )
        with pytest.raises(HttpCompressionRatioExceeded) as exc_info:
            await mw.process_response(response.request, response)
        assert exc_info.value.__cause__.decompressed_size == len(inner)
        assert exc_info.value.__cause__.compressed_size == wire_size

        # Budget at the exact total ratio: both layers pass and decoding wins.
        accepted = self._response(outer, encoding="gzip, gzip")
        mw_ok = self._mw(
            COMPRESSION_MAX_RATIO=total_ratio,
            COMPRESSION_RATIO_MIN_SIZE=0,
            COMPRESSION_MAX_ENCODINGS=2,
        )
        new_response = await mw_ok.process_response(accepted.request, accepted)
        assert new_response.body == plain

    @coroutine_test
    async def test_multi_layer_mixed_codecs(self) -> None:
        # Content-Encoding "gzip, deflate": deflate was applied last, so the
        # wire body is deflate(gzip(plain)) and deflate is decoded first.
        plain = b"z" * 100_000
        body = zlib.compress(_gzip_bytes(plain), 9)
        response = self._response(body, encoding="gzip, deflate")
        mw = self._mw(
            COMPRESSION_MAX_RATIO=10.0,
            COMPRESSION_RATIO_MIN_SIZE=0,
            COMPRESSION_MAX_ENCODINGS=2,
        )
        with pytest.raises(HttpCompressionRatioExceeded):
            await mw.process_response(response.request, response)

    # --- multi-member gzip ---------------------------------------------------

    @coroutine_test
    async def test_multi_member_gzip_cannot_bypass_ratio(self) -> None:
        # Concatenated gzip members: the denominator is the whole concatenated
        # wire body, never reset per member.
        members = [_gzip_bytes(b"a" * 50_000) for _ in range(30)]
        body = b"".join(members)
        total_plain = 30 * 50_000
        exact_ratio = total_plain / len(body)
        assert len(body) >= 1024

        response = self._response(body)
        mw_ok = self._mw(
            COMPRESSION_MAX_RATIO=exact_ratio, COMPRESSION_RATIO_MIN_SIZE=1024
        )
        new_response = await mw_ok.process_response(response.request, response)
        assert new_response.body == b"a" * total_plain

        rejected = self._response(body)
        mw_bad = self._mw(
            COMPRESSION_MAX_RATIO=exact_ratio - 1e-12,
            COMPRESSION_RATIO_MIN_SIZE=1024,
        )
        with pytest.raises(HttpCompressionRatioExceeded):
            await mw_bad.process_response(rejected.request, rejected)

    # --- truncated input -----------------------------------------------------

    @coroutine_test
    async def test_truncated_body_checked_with_partial_output(self) -> None:
        full = _gzip_bytes(b"x" * 5_000_000)
        truncated = full[: len(full) // 2]
        assert len(truncated) >= 1024
        partial = gunzip(truncated)  # gunzip returns as much data as it can.
        assert partial
        assert len(partial) / len(truncated) > 1.0
        response = self._response(truncated)
        mw = self._mw(COMPRESSION_MAX_RATIO=1.0)
        with pytest.raises(HttpCompressionRatioExceeded):
            await mw.process_response(response.request, response)
        assert mw.stats.get_value("httpcompression/ratio_rejected") == 1

    # --- original objects untouched / no partial output leakage --------------

    @coroutine_test
    async def test_rejection_does_not_mutate_response_or_request(self) -> None:
        body = _gzip_bytes(b"a" * 200_000)
        response = self._response(body)
        request = response.request
        original_encoding = response.headers.getlist("Content-Encoding")
        original_body = response.body
        original_request_headers = dict(request.headers)
        mw = self._mw(COMPRESSION_MAX_RATIO=1.0, COMPRESSION_RATIO_MIN_SIZE=0)
        with pytest.raises(HttpCompressionRatioExceeded):
            await mw.process_response(request, response)
        assert response.body == original_body
        assert response.headers.getlist("Content-Encoding") == original_encoding
        assert dict(request.headers) == original_request_headers
        # No partial decoding products leak into the success stats.
        assert mw.stats.get_value("httpcompression/response_count") is None
        assert mw.stats.get_value("httpcompression/response_bytes") is None
        assert mw.stats.get_value("httpcompression/ratio_rejected") == 1

    # --- distinct ratio vs size stats ---------------------------------------

    @coroutine_test
    async def test_size_rejection_uses_separate_stat(self) -> None:
        samplefile = SAMPLEDIR / "bomb-gzip.bin"
        body = samplefile.read_bytes()
        response = self._response(body)
        crawler = get_crawler(Spider, settings_dict={"DOWNLOAD_MAXSIZE": 1_000_000})
        spider = crawler._create_spider("scrapytest.org")
        mw = build_from_crawler(HttpCompressionMiddleware, crawler)
        mw.open_spider(spider)
        crawler.stats.open_spider()
        with pytest.raises(IgnoreRequest) as exc_info:
            await mw.process_response(response.request, response)
        assert not isinstance(exc_info.value, HttpCompressionRatioExceeded)
        assert isinstance(exc_info.value.__cause__, _DecompressionMaxSizeExceeded)
        assert mw.stats.get_value("httpcompression/size_rejected") == 1
        assert mw.stats.get_value("httpcompression/ratio_rejected") is None

    @coroutine_test
    async def test_ratio_and_size_limits_coexist(self) -> None:
        # The ratio limit trips first; DOWNLOAD_MAXSIZE remains in force
        # independently and neither weakens the encoding-count limit.
        body = _gzip_bytes(b"a" * 200_000)
        response = self._response(body)
        mw = self._mw(
            COMPRESSION_MAX_RATIO=1.0,
            COMPRESSION_RATIO_MIN_SIZE=0,
            DOWNLOAD_MAXSIZE=10_000_000,
        )
        with pytest.raises(HttpCompressionRatioExceeded):
            await mw.process_response(response.request, response)
        assert mw.stats.get_value("httpcompression/ratio_rejected") == 1
        assert mw.stats.get_value("httpcompression/size_rejected") is None

    # --- unknown encoding stays compatible -----------------------------------

    @coroutine_test
    async def test_unknown_encoding_kept_with_ratio_enabled(self) -> None:
        plain = b"<!DOCTYPE html>"
        response = Response(
            "http://scrapytest.org/",
            body=plain,
            headers={"Content-Encoding": "uuencode"},
        )
        response.request = Request("http://scrapytest.org")
        mw = self._mw(COMPRESSION_MAX_RATIO=1.0)
        new_response = await mw.process_response(response.request, response)
        assert new_response.headers.getlist("Content-Encoding") == [b"uuencode"]
        assert new_response.body == plain
        assert mw.stats.get_value("httpcompression/ratio_rejected") is None

    # --- missing optional decoding libraries ---------------------------------

    @coroutine_test
    async def test_missing_brotli_library_treated_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import scrapy.utils._compression as compression_mod

        monkeypatch.setattr(compression_mod, "brotli", None)
        body = brotli.compress(b"a" * 4_096)
        response = self._response(body, encoding="br")
        mw = self._mw(COMPRESSION_MAX_RATIO=1.0)
        # A missing optional library must not crash: the coding is kept on the
        # response, exactly like an unknown encoding.
        new_response = await mw.process_response(response.request, response)
        assert new_response.headers.getlist("Content-Encoding") == [b"br"]
        assert new_response.body == body
        assert mw.stats.get_value("httpcompression/ratio_rejected") is None

    @coroutine_test
    async def test_missing_zstd_library_treated_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import scrapy.utils._compression as compression_mod

        monkeypatch.setattr(compression_mod, "zstd", None)
        body = zstd.compress(b"a" * 4_096)
        response = self._response(body, encoding="zstd")
        mw = self._mw(COMPRESSION_MAX_RATIO=1.0)
        new_response = await mw.process_response(response.request, response)
        assert new_response.headers.getlist("Content-Encoding") == [b"zstd"]
        assert new_response.body == body
        assert mw.stats.get_value("httpcompression/ratio_rejected") is None

    @coroutine_test
    async def test_gzip_works_with_optional_libraries_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import scrapy.utils._compression as compression_mod

        monkeypatch.setattr(compression_mod, "brotli", None)
        monkeypatch.setattr(compression_mod, "zstd", None)
        body = _gzip_bytes(b"a" * 4_096)
        response = self._response(body)
        mw = self._mw(COMPRESSION_MAX_RATIO=100.0)
        new_response = await mw.process_response(response.request, response)
        assert new_response.body == b"a" * 4_096

    @coroutine_test
    async def test_missing_libraries_not_advertised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import scrapy.utils._compression as compression_mod

        monkeypatch.setattr(compression_mod, "brotli", None)
        monkeypatch.setattr(compression_mod, "zstd", None)
        mw = self._mw()
        request = Request("http://scrapytest.org")
        mw.process_request(request)
        assert request.headers.get("Accept-Encoding") == b"gzip, deflate"

        monkeypatch.undo()
        request2 = Request("http://scrapytest.org")
        mw.process_request(request2)
        assert request2.headers.get("Accept-Encoding") == b", ".join(
            ACCEPTED_ENCODINGS
        )

    def test_decompression_errors_include_installed_library_classes(self) -> None:
        # With the libraries installed the registry keeps their error types,
        # and drops them gracefully when absent (see monkeypatched tests above).
        import scrapy.utils._compression as compression_mod

        assert compression_mod.brotli is not None
        assert compression_mod.zstd is not None
        assert compression_mod.brotli.error in compression_mod._DECOMPRESSION_ERRORS
        assert compression_mod.zstd.ZstdError in compression_mod._DECOMPRESSION_ERRORS


_MISSING_LIB_SCRIPT = """
import sys


class _ImportBlocker:
    def find_spec(self, name, path, target=None):
        if name in ("brotli", "backports.zstd", "compression.zstd"):
            raise ImportError(f"blocked {name}")
        return None


sys.meta_path.insert(0, _ImportBlocker())
sys.modules.pop("brotli", None)
sys.modules.pop("backports.zstd", None)

import scrapy.utils._compression as compression_mod
from scrapy.downloadermiddlewares.httpcompression import HttpCompressionMiddleware

assert compression_mod.brotli is None
assert compression_mod.zstd is None
assert HttpCompressionMiddleware._accepted_encodings() == [b"gzip", b"deflate"]

# Direct helper use raises ImportError; the middleware never reaches them
# because the codings are treated as unknown.
for helper in (compression_mod._unbrotli, compression_mod._unzstd):
    try:
        helper(b"x")
    except ImportError:
        pass
    else:
        raise AssertionError("expected ImportError")

# Built-in codecs keep working without the optional libraries.
from gzip import GzipFile
from io import BytesIO

from scrapy.utils._compression import gunzip

buf = BytesIO()
with GzipFile(fileobj=buf, mode="wb") as f:
    f.write(b"hello")
assert gunzip(buf.getvalue()) == b"hello"
print("missing optional libraries handled")
"""


def test_missing_optional_libraries_subprocess(
    tmp_path: Path,
) -> None:
    """A fresh interpreter without brotli/zstd imports Scrapy fine and only
    advertises the built-in gzip/deflate codings."""
    script = tmp_path / "missing_libs.py"
    script.write_text(_MISSING_LIB_SCRIPT)
    result = subprocess.run(
        [sys.executable, "-I", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "missing optional libraries handled" in result.stdout
