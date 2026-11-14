from __future__ import annotations

import email.utils
import gc
import logging
import pickle
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from twisted.internet.defer import Deferred
from twisted.internet.error import ConnectionLost

from scrapy import signals
from scrapy.core.downloader.middleware import DownloaderMiddlewareManager
from scrapy.downloadermiddlewares.httpcache import HttpCacheMiddleware
from scrapy.exceptions import IgnoreRequest
from scrapy.extensions.httpcache import DummyPolicy, rfc1123_to_epoch
from scrapy.http import BodyFileResponse, HtmlResponse, Request, Response
from scrapy.spiders import Spider
from scrapy.utils.asyncio import sleep
from scrapy.utils.defer import deferred_from_coro, maybe_deferred_to_future
from scrapy.utils.misc import build_from_crawler, load_object
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from scrapy.crawler import Crawler


class CustomResponse(Response):
    attributes: tuple[str, ...] = (*Response.attributes, "custom")

    def __init__(self, *args: Any, custom: str | None = None, **kwargs: Any):
        self.custom = custom
        super().__init__(*args, **kwargs)


class AlwaysStalePolicy(DummyPolicy):
    """:class:`~scrapy.extensions.httpcache.DummyPolicy` that always
    revalidates cached responses."""

    def is_cached_response_fresh(self, cachedresponse, request):
        return False


class TestBase:
    """Base class with common setup and helper methods."""

    policy_class: str
    storage_class: str

    def setup_method(self):
        self.yesterday = email.utils.formatdate(time.time() - 86400)
        self.today = email.utils.formatdate()
        self.tomorrow = email.utils.formatdate(time.time() + 86400)
        self.tmpdir = tempfile.mkdtemp()
        self.request = Request("http://www.example.com", headers={"User-Agent": "test"})
        self.response = Response(
            "http://www.example.com",
            headers={"Content-Type": "text/html"},
            body=b"test body",
            status=202,
        )

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def _get_settings(self, **new_settings: Any) -> dict[str, Any]:
        settings = {
            "HTTPCACHE_ENABLED": True,
            "HTTPCACHE_DIR": self.tmpdir,
            "HTTPCACHE_IGNORE_HTTP_CODES": [],
            "HTTPCACHE_POLICY": self.policy_class,
            "HTTPCACHE_STORAGE": self.storage_class,
        }
        settings.update(new_settings)
        return settings

    @contextmanager
    def _get_crawler(self, **new_settings: Any) -> Generator[Crawler]:
        settings = self._get_settings(**new_settings)
        crawler = get_crawler(Spider, settings)
        crawler.spider = crawler._create_spider("example.com")
        crawler.stats.open_spider()
        try:
            yield crawler
        finally:
            crawler.stats.close_spider()

    @contextmanager
    def _storage(self, **new_settings: Any):
        with self._middleware(**new_settings) as mw:
            yield mw.storage, mw.crawler

    @contextmanager
    def _middleware(self, **new_settings: Any) -> Generator[HttpCacheMiddleware]:
        with self._get_crawler(**new_settings) as crawler:
            assert crawler.spider
            mw = build_from_crawler(HttpCacheMiddleware, crawler)
            mw.spider_opened(crawler.spider)
            try:
                yield mw
            finally:
                mw.spider_closed(crawler.spider)

    def assertEqualResponse(self, response1: Response, response2: Response) -> None:
        assert response1.url == response2.url
        assert response1.status == response2.status
        assert response1.headers == response2.headers
        assert response1.body == response2.body


class StorageTestMixin(TestBase):
    """Mixin containing storage-specific test methods."""

    def _corrupt_cache_entry(
        self, storage: Any, spider: Spider, request: Request
    ) -> None:
        """Make the cache entry of *request* unreadable for *storage*."""
        raise NotImplementedError

    def _downgrade_cache_entry(
        self, storage: Any, spider: Spider, request: Request
    ) -> None:
        """Rewrite the cache entry of *request* as Scrapy 2.14 would have."""
        raise NotImplementedError

    def test_storage(self):
        with self._storage(HTTPCACHE_EXPIRATION_SECS=100) as (storage, crawler):
            request2 = self.request.copy()
            assert storage.retrieve_response(crawler.spider, request2) is None

            before = time.time()
            storage.store_response(crawler.spider, self.request, self.response)
            after = time.time()
            response2 = storage.retrieve_response(crawler.spider, request2)
            assert isinstance(response2, HtmlResponse)  # content-type header
            self.assertEqualResponse(self.response, response2)
            assert before <= request2.meta["cache_timestamp"] <= after

            expired = time.time() + storage.expiration_secs + 1
            with mock.patch("scrapy.extensions.httpcache.time", return_value=expired):
                assert storage.retrieve_response(crawler.spider, request2) is None

    def test_storage_never_expire(self):
        with self._storage(HTTPCACHE_EXPIRATION_SECS=0) as (storage, crawler):
            assert storage.retrieve_response(crawler.spider, self.request) is None
            storage.store_response(crawler.spider, self.request, self.response)
            future = time.time() + 10**6
            with mock.patch("scrapy.extensions.httpcache.time", return_value=future):
                assert storage.retrieve_response(crawler.spider, self.request)

    @coroutine_test
    async def test_corrupted_cache_entry_is_a_miss(self, caplog):
        with self._middleware() as mw:
            spider = mw.crawler.spider
            assert spider
            mw.storage.store_response(spider, self.request, self.response)
            self._corrupt_cache_entry(mw.storage, spider, self.request)

            caplog.clear()
            with caplog.at_level(logging.WARNING):
                assert await mw.process_request(self.request) is None

            assert "treating it as a cache miss" in caplog.text
            assert mw.crawler.stats.get_value("httpcache/retrieve_error") == 1
            assert mw.crawler.stats.get_value("httpcache/miss") == 1

            # Storing the response again replaces the corrupted cache entry.
            mw.storage.store_response(spider, self.request, self.response)
            self.assertEqualResponse(
                self.response, mw.storage.retrieve_response(spider, self.request)
            )

    @coroutine_test
    async def test_corrupted_cache_entry_ignore_missing(self):
        with self._middleware(HTTPCACHE_IGNORE_MISSING=True) as mw:
            spider = mw.crawler.spider
            assert spider
            mw.storage.store_response(spider, self.request, self.response)
            self._corrupt_cache_entry(mw.storage, spider, self.request)

            with pytest.raises(IgnoreRequest):
                await mw.process_request(self.request)

            assert mw.crawler.stats.get_value("httpcache/retrieve_error") == 1
            assert mw.crawler.stats.get_value("httpcache/ignore") == 1

    def test_storage_no_content_type_header(self):
        """Test that the response body is used to get the right response class
        even if there is no Content-Type header"""
        with self._storage() as (storage, crawler):
            assert storage.retrieve_response(crawler.spider, self.request) is None
            response = Response(
                "http://www.example.com",
                body=b"<!DOCTYPE html>\n<title>.</title>",
                status=202,
            )
            storage.store_response(crawler.spider, self.request, response)
            cached_response = storage.retrieve_response(crawler.spider, self.request)
            assert isinstance(cached_response, HtmlResponse)
            self.assertEqualResponse(response, cached_response)

    def test_storage_response_class(self):
        with self._storage() as (storage, crawler):
            response = CustomResponse(
                "http://www.example.com", body=b"test body", custom="value"
            )
            storage.store_response(crawler.spider, self.request, response)
            cached_response = storage.retrieve_response(crawler.spider, self.request)
            assert isinstance(cached_response, CustomResponse)
            assert cached_response.custom == "value"

    def test_storage_encoding(self):
        """The encoding of the stored response is kept even when it cannot be
        inferred from the response data."""
        with self._storage() as (storage, crawler):
            response = HtmlResponse(
                "http://www.example.com",
                body='<meta charset="iso-8859-1">€'.encode(),
                encoding="utf-8",
            )
            storage.store_response(crawler.spider, self.request, response)
            cached_response = storage.retrieve_response(crawler.spider, self.request)
            assert cached_response.encoding == "utf-8"
            assert cached_response.text == response.text

    def test_storage_headers(self) -> None:
        """Repeated headers, the case of header names and header values that
        are not valid UTF-8 are kept."""
        with self._storage() as (storage, crawler):
            response = Response(
                "http://www.example.com",
                headers=[
                    ("Set-Cookie", "a=1"),
                    ("content-type", "text/html"),
                    ("set-cookie", "b=2"),
                    ("X-Custom-Header", b"caf\xe9"),
                ],
                body=b"test body",
            )
            storage.store_response(crawler.spider, self.request, response)
            cached_response = storage.retrieve_response(crawler.spider, self.request)
            self.assertEqualResponse(response, cached_response)

    def test_storage_old_cache_entry(self):
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            self._downgrade_cache_entry(storage, crawler.spider, self.request)
            cached_response = storage.retrieve_response(crawler.spider, self.request)
            assert isinstance(cached_response, HtmlResponse)
            self.assertEqualResponse(self.response, cached_response)


class PolicyTestMixin(TestBase):
    """Mixin containing policy-specific test methods."""

    @coroutine_test
    async def test_cache_timestamp(self):
        with self._middleware() as mw:
            assert await mw.process_request(self.request) is None
            assert "cache_timestamp" not in self.request.meta

            before = time.time()
            mw.process_response(self.request, self.response)
            after = time.time()

            if not mw.policy.should_cache_response(self.response, self.request):
                return

            response = await mw.process_request(self.request)
            assert isinstance(response, Response)
            assert before <= self.request.meta["cache_timestamp"] <= after

    @coroutine_test
    async def test_dont_cache(self):
        with self._middleware() as mw:
            self.request.meta["dont_cache"] = True
            assert await mw.process_request(self.request) is None
            mw.process_response(self.request, self.response)
            assert mw.storage.retrieve_response(mw.crawler.spider, self.request) is None

        with self._middleware() as mw:
            self.request.meta["dont_cache"] = False
            mw.process_response(self.request, self.response)
            if mw.policy.should_cache_response(self.response, self.request):
                assert isinstance(
                    mw.storage.retrieve_response(mw.crawler.spider, self.request),
                    self.response.__class__,
                )


class DummyPolicyTestMixin(PolicyTestMixin):
    """Mixin containing dummy policy specific test methods."""

    @coroutine_test
    async def test_middleware(self):
        with self._middleware() as mw:
            assert await mw.process_request(self.request) is None
            mw.process_response(self.request, self.response)
            response = await mw.process_request(self.request)
            assert isinstance(response, HtmlResponse)
            self.assertEqualResponse(self.response, response)
            assert "cached" in response.flags

    @coroutine_test
    async def test_body_file(self):
        with self._middleware() as mw:
            mw.process_response(self.request, self.response)
            request = self.request.replace(meta={"body_file": "body"})
            assert await mw.process_request(request) is None
            response = BodyFileResponse(request.url, body_file="body")
            assert mw.process_response(request, response) is response
            cached = await mw.process_request(self.request)
            assert isinstance(cached, HtmlResponse)
            self.assertEqualResponse(self.response, cached)

    @coroutine_test
    async def test_different_request_response_urls(self):
        with self._middleware() as mw:
            req = Request("http://host.com/path")
            res = Response("http://host2.net/test.html")
            assert await mw.process_request(req) is None
            mw.process_response(req, res)
            cached = await mw.process_request(req)
            assert isinstance(cached, Response)
            self.assertEqualResponse(res, cached)
            assert "cached" in cached.flags

    @coroutine_test
    async def test_middleware_ignore_missing(self):
        with self._middleware(HTTPCACHE_IGNORE_MISSING=True) as mw:
            with pytest.raises(IgnoreRequest):
                await mw.process_request(self.request)
            mw.process_response(self.request, self.response)
            response = await mw.process_request(self.request)
            assert isinstance(response, HtmlResponse)
            self.assertEqualResponse(self.response, response)
            assert "cached" in response.flags

    @coroutine_test
    async def test_middleware_ignore_schemes(self):
        # http responses are cached by default
        req, res = Request("http://test.com/"), Response("http://test.com/")
        with self._middleware() as mw:
            assert await mw.process_request(req) is None
            mw.process_response(req, res)

            cached = await mw.process_request(req)
            assert isinstance(cached, Response), type(cached)
            self.assertEqualResponse(res, cached)
            assert "cached" in cached.flags

        # file response is not cached by default
        req, res = Request("file:///tmp/t.txt"), Response("file:///tmp/t.txt")
        with self._middleware() as mw:
            assert await mw.process_request(req) is None
            mw.process_response(req, res)

            assert mw.storage.retrieve_response(mw.crawler.spider, req) is None
            assert await mw.process_request(req) is None

        # s3 scheme response is cached by default
        req, res = Request("s3://bucket/key"), Response("s3://bucket/key")
        with self._middleware() as mw:
            assert await mw.process_request(req) is None
            mw.process_response(req, res)

            cached = await mw.process_request(req)
            assert isinstance(cached, Response), type(cached)
            self.assertEqualResponse(res, cached)
            assert "cached" in cached.flags

        # ignore s3 scheme
        req, res = Request("s3://bucket/key2"), Response("s3://bucket/key2")
        with self._middleware(HTTPCACHE_IGNORE_SCHEMES=["s3"]) as mw:
            assert await mw.process_request(req) is None
            mw.process_response(req, res)

            assert mw.storage.retrieve_response(mw.crawler.spider, req) is None
            assert await mw.process_request(req) is None

    @coroutine_test
    async def test_middleware_ignore_http_codes(self):
        # test response is not cached
        with self._middleware(HTTPCACHE_IGNORE_HTTP_CODES=[202]) as mw:
            assert await mw.process_request(self.request) is None
            mw.process_response(self.request, self.response)

            assert mw.storage.retrieve_response(mw.crawler.spider, self.request) is None
            assert await mw.process_request(self.request) is None

        # test response is cached
        with self._middleware(HTTPCACHE_IGNORE_HTTP_CODES=[203]) as mw:
            mw.process_response(self.request, self.response)
            response = await mw.process_request(self.request)
            assert isinstance(response, HtmlResponse)
            self.assertEqualResponse(self.response, response)
            assert "cached" in response.flags

    @coroutine_test
    async def test_revalidation_keeps_cached_response(self):
        # The dummy policy considers every cached response valid, so a policy
        # that subclasses it to force revalidation always gets the cached
        # response back, whatever the new response is.
        with self._middleware(HTTPCACHE_POLICY=AlwaysStalePolicy) as mw:
            assert await mw.process_request(self.request) is None
            mw.process_response(self.request, self.response)

            assert await mw.process_request(self.request) is None
            fresh_response = self.response.replace(body=b"new body")
            response = mw.process_response(self.request, fresh_response)
            assert isinstance(response, Response)
            self.assertEqualResponse(self.response, response)
            assert "cached" in response.flags
            assert mw.stats.get_value("httpcache/revalidate") == 1


class RFC2616PolicyTestMixin(PolicyTestMixin):
    """Mixin containing RFC2616 policy specific test methods."""

    @staticmethod
    async def _process_requestresponse(
        mw: HttpCacheMiddleware, request: Request, response: Response | None
    ) -> Response:
        result: Request | Response | None = None
        try:
            result = await mw.process_request(request)
            if result:
                assert isinstance(result, Response)
                return result
            assert response is not None
            result = mw.process_response(request, response)
            assert isinstance(result, Response)
            return result
        except Exception:
            print("Request", request)
            print("Response", response)
            print("Result", result)
            raise

    @coroutine_test
    async def test_request_cacheability(self):
        res0 = Response(
            self.request.url, status=200, headers={"Expires": self.tomorrow}
        )
        req0 = Request("http://example.com")
        req1 = req0.replace(headers={"Cache-Control": "no-store"})
        req2 = req0.replace(headers={"Cache-Control": "no-cache"})
        with self._middleware() as mw:
            # response for a request with no-store must not be cached
            res1 = await self._process_requestresponse(mw, req1, res0)
            self.assertEqualResponse(res1, res0)
            assert mw.storage.retrieve_response(mw.crawler.spider, req1) is None
            # Re-do request without no-store and expect it to be cached
            res2 = await self._process_requestresponse(mw, req0, res0)
            assert "cached" not in res2.flags
            res3 = await mw.process_request(req0)
            assert isinstance(res3, Response)
            assert "cached" in res3.flags
            self.assertEqualResponse(res2, res3)
            # request with no-cache directive must not return cached response
            # but it allows new response to be stored
            res0b = res0.replace(body=b"foo")
            res4 = await self._process_requestresponse(mw, req2, res0b)
            self.assertEqualResponse(res4, res0b)
            assert "cached" not in res4.flags
            res5 = await self._process_requestresponse(mw, req0, None)
            self.assertEqualResponse(res5, res0b)
            assert "cached" in res5.flags

    @coroutine_test
    async def test_request_cache_control_none(self):
        res0 = Response(
            self.request.url, status=200, headers={"Expires": self.tomorrow}
        )
        req0 = Request("http://example.com", headers={"Cache-Control": None})
        with self._middleware() as mw:
            res1 = await self._process_requestresponse(mw, req0, res0)
            assert "cached" not in res1.flags
            res2 = await mw.process_request(req0)
            assert isinstance(res2, Response)
            assert "cached" in res2.flags

    @coroutine_test
    async def test_response_cacheability(self):
        responses = [
            # 304 is not cacheable no matter what servers sends
            (False, 304, {}),
            (False, 304, {"Last-Modified": self.yesterday}),
            (False, 304, {"Expires": self.tomorrow}),
            (False, 304, {"Etag": "bar"}),
            (False, 304, {"Cache-Control": "max-age=3600"}),
            # Always obey no-store cache control
            (False, 200, {"Cache-Control": "no-store"}),
            (False, 200, {"Cache-Control": "no-store, max-age=300"}),  # invalid
            (
                False,
                200,
                {"Cache-Control": "no-store", "Expires": self.tomorrow},
            ),  # invalid
            # Ignore responses missing expiration and/or validation headers
            (False, 200, {}),
            (False, 302, {}),
            (False, 307, {}),
            (False, 404, {}),
            # Cache responses with expiration and/or validation headers
            (True, 200, {"Last-Modified": self.yesterday}),
            (True, 203, {"Last-Modified": self.yesterday}),
            (True, 300, {"Last-Modified": self.yesterday}),
            (True, 301, {"Last-Modified": self.yesterday}),
            (True, 308, {"Last-Modified": self.yesterday}),
            (True, 401, {"Last-Modified": self.yesterday}),
            (True, 404, {"Cache-Control": "public, max-age=600"}),
            (True, 302, {"Expires": self.tomorrow}),
            (True, 200, {"Etag": "foo"}),
        ]
        with self._middleware() as mw:
            for idx, (shouldcache, status, headers) in enumerate(responses):
                req0 = Request(f"http://example-{idx}.com")
                res0 = Response(req0.url, status=status, headers=headers)
                res1 = await self._process_requestresponse(mw, req0, res0)
                res304 = res0.replace(status=304)
                res2 = await self._process_requestresponse(
                    mw, req0, res304 if shouldcache else res0
                )
                self.assertEqualResponse(res1, res0)
                self.assertEqualResponse(res2, res0)
                resc = mw.storage.retrieve_response(mw.crawler.spider, req0)
                if shouldcache:
                    self.assertEqualResponse(resc, res1)
                    assert "cached" in res2.flags
                    assert res2.status != 304
                else:
                    assert not resc
                    assert "cached" not in res2.flags

        # cache unconditionally unless response contains no-store or is a 304
        with self._middleware(HTTPCACHE_ALWAYS_STORE=True) as mw:
            for idx, (_, status, headers) in enumerate(responses):
                shouldcache = (
                    "no-store" not in headers.get("Cache-Control", "") and status != 304
                )
                req0 = Request(f"http://example2-{idx}.com")
                res0 = Response(req0.url, status=status, headers=headers)
                res1 = await self._process_requestresponse(mw, req0, res0)
                res304 = res0.replace(status=304)
                res2 = await self._process_requestresponse(
                    mw, req0, res304 if shouldcache else res0
                )
                self.assertEqualResponse(res1, res0)
                self.assertEqualResponse(res2, res0)
                resc = mw.storage.retrieve_response(mw.crawler.spider, req0)
                if shouldcache:
                    self.assertEqualResponse(resc, res1)
                    assert "cached" in res2.flags
                    assert res2.status != 304
                else:
                    assert not resc
                    assert "cached" not in res2.flags

    @coroutine_test
    async def test_cached_and_fresh(self):
        sampledata = [
            (200, {"Date": self.yesterday, "Expires": self.tomorrow}),
            (200, {"Date": self.yesterday, "Cache-Control": "max-age=86405"}),
            (200, {"Age": "299", "Cache-Control": "max-age=300"}),
            # Obey max-age if present over any others
            (
                200,
                {
                    "Date": self.today,
                    "Age": "86405",
                    "Cache-Control": "max-age=" + str(86400 * 3),
                    "Expires": self.yesterday,
                    "Last-Modified": self.yesterday,
                },
            ),
            # obey Expires if max-age is not present
            (
                200,
                {
                    "Date": self.yesterday,
                    "Age": "86400",
                    "Cache-Control": "public",
                    "Expires": self.tomorrow,
                    "Last-Modified": self.yesterday,
                },
            ),
            # Default missing Date header to right now
            (200, {"Expires": self.tomorrow}),
            # Firefox - Expires if age is greater than 10% of (Date - Last-Modified)
            (
                200,
                {
                    "Date": self.today,
                    "Last-Modified": self.yesterday,
                    "Age": str(86400 / 10 - 1),
                },
            ),
            # Firefox - Set one year maxage to permanent redirects missing expiration info
            (300, {}),
            (301, {}),
            (308, {}),
        ]
        with self._middleware() as mw:
            for idx, (status, headers) in enumerate(sampledata):
                req0 = Request(f"http://example-{idx}.com")
                res0 = Response(req0.url, status=status, headers=headers)
                # cache fresh response
                res1 = await self._process_requestresponse(mw, req0, res0)
                self.assertEqualResponse(res1, res0)
                assert "cached" not in res1.flags
                # return fresh cached response without network interaction
                res2 = await self._process_requestresponse(mw, req0, None)
                self.assertEqualResponse(res1, res2)
                assert "cached" in res2.flags
                # validate cached response if request max-age set as 0
                req1 = req0.replace(headers={"Cache-Control": "max-age=0"})
                res304 = res0.replace(status=304)
                assert await mw.process_request(req1) is None
                res3 = await self._process_requestresponse(mw, req1, res304)
                self.assertEqualResponse(res1, res3)
                assert "cached" in res3.flags

    @coroutine_test
    async def test_cached_and_stale(self):
        sampledata = [
            (200, {"Date": self.today, "Expires": self.yesterday}),
            (
                200,
                {
                    "Date": self.today,
                    "Expires": self.yesterday,
                    "Last-Modified": self.yesterday,
                },
            ),
            (200, {"Expires": self.yesterday}),
            (200, {"Expires": self.yesterday, "ETag": "foo"}),
            (200, {"Expires": self.yesterday, "Last-Modified": self.yesterday}),
            (200, {"Expires": self.tomorrow, "Age": "86405"}),
            (200, {"Cache-Control": "max-age=86400", "Age": "86405"}),
            # no-cache forces expiration, also revalidation if validators exists
            (200, {"Cache-Control": "no-cache"}),
            (200, {"Cache-Control": "no-cache", "ETag": "foo"}),
            (200, {"Cache-Control": "no-cache", "Last-Modified": self.yesterday}),
            (
                200,
                {
                    "Cache-Control": "no-cache,must-revalidate",
                    "Last-Modified": self.yesterday,
                },
            ),
            (
                200,
                {
                    "Cache-Control": "must-revalidate",
                    "Expires": self.yesterday,
                    "Last-Modified": self.yesterday,
                },
            ),
            (200, {"Cache-Control": "max-age=86400,must-revalidate", "Age": "86405"}),
        ]
        with self._middleware() as mw:
            for idx, (status, headers) in enumerate(sampledata):
                req0 = Request(f"http://example-{idx}.com")
                res0a = Response(req0.url, status=status, headers=headers)
                # cache expired response
                res1 = await self._process_requestresponse(mw, req0, res0a)
                self.assertEqualResponse(res1, res0a)
                assert "cached" not in res1.flags
                # Same request but as cached response is stale a new response must
                # be returned
                res0b = res0a.replace(body=b"bar")
                res2 = await self._process_requestresponse(mw, req0, res0b)
                self.assertEqualResponse(res2, res0b)
                assert "cached" not in res2.flags
                cc = headers.get("Cache-Control", "")
                # Previous response expired too, subsequent request to same
                # resource must revalidate and succeed on 304 if validators
                # are present
                if "ETag" in headers or "Last-Modified" in headers:
                    res0c = res0b.replace(status=304)
                    res3 = await self._process_requestresponse(mw, req0, res0c)
                    self.assertEqualResponse(res3, res0b)
                    assert "cached" in res3.flags
                    # get cached response on server errors unless must-revalidate
                    # in cached response
                    res0d = res0b.replace(status=500)
                    res4 = await self._process_requestresponse(mw, req0, res0d)
                    if "must-revalidate" in cc:
                        assert "cached" not in res4.flags
                        self.assertEqualResponse(res4, res0d)
                    else:
                        assert "cached" in res4.flags
                        self.assertEqualResponse(res4, res0b)
                # Requests with max-stale can fetch expired cached responses
                # unless cached response has must-revalidate
                req1 = req0.replace(headers={"Cache-Control": "max-stale"})
                res5 = await self._process_requestresponse(mw, req1, res0b)
                self.assertEqualResponse(res5, res0b)
                if "no-cache" in cc or "must-revalidate" in cc:
                    assert "cached" not in res5.flags
                else:
                    assert "cached" in res5.flags

    @coroutine_test
    async def test_middleware_ignore_schemes(self):
        # file responses are not cached by default
        req = Request("file:///tmp/t.txt")
        res = Response(req.url, headers={"Expires": self.tomorrow})
        with self._middleware() as mw:
            assert await mw.process_request(req) is None
            mw.process_response(req, res)

            assert mw.storage.retrieve_response(mw.crawler.spider, req) is None
            assert await mw.process_request(req) is None

    @coroutine_test
    async def test_max_stale_with_value(self):
        # A response that expired one day ago.
        headers = {"Date": self.yesterday, "Expires": self.yesterday}
        with self._middleware() as mw:
            req0 = Request("http://example.com")
            res0 = Response(req0.url, headers=headers)
            await self._process_requestresponse(mw, req0, res0)

            # max-stale greater than the staleness of the cached response
            req1 = req0.replace(headers={"Cache-Control": "max-stale=172800"})
            res1 = await mw.process_request(req1)
            assert isinstance(res1, Response)
            assert "cached" in res1.flags

            # max-stale lower than the staleness of the cached response
            req2 = req0.replace(headers={"Cache-Control": "max-stale=60"})
            assert await mw.process_request(req2) is None
            mw.process_exception(req2, IgnoreRequest())

            # a non-integer max-stale value is ignored
            req3 = req0.replace(headers={"Cache-Control": "max-stale=soon"})
            assert await mw.process_request(req3) is None

    @coroutine_test
    async def test_freshen_response_on_revalidation(self):
        # A response successfully revalidated (304) must have its Date
        # header, and thus its age, refreshed in the cache.
        headers = {
            "Date": self.yesterday,
            "Cache-Control": "max-age=60",
            "Last-Modified": self.yesterday,
            "Warning": ["110 - old", "214 - keep"],
        }
        with self._middleware() as mw:
            req0 = Request("http://example.com")
            res0 = Response(req0.url, headers=headers)
            await self._process_requestresponse(mw, req0, res0)

            # The cached response is stale, so the request must be
            # revalidated with the server.
            assert await mw.process_request(req0) is None
            res304 = Response(req0.url, status=304, headers={"Date": self.today})
            res1 = mw.process_response(req0, res304)
            assert res1.headers["Date"] == self.today.encode()
            assert res1.headers["Warning"] == b"214 - keep"

            # The freshened response must now be considered fresh again,
            # without a further round trip to the server.
            res2 = await mw.process_request(req0)
            assert isinstance(res2, Response)
            assert "cached" in res2.flags
            assert res2.headers["Date"] == self.today.encode()

    @coroutine_test
    async def test_response_dated_in_the_future(self):
        # A Date header ahead of the local clock must not make the cached
        # response look aged.
        headers = {"Date": self.tomorrow, "Cache-Control": "max-age=10"}
        with self._middleware() as mw:
            req0 = Request("http://example.com")
            res0 = Response(req0.url, headers=headers)
            res1 = await self._process_requestresponse(mw, req0, res0)
            assert "cached" not in res1.flags

            res2 = await self._process_requestresponse(mw, req0, None)
            self.assertEqualResponse(res1, res2)
            assert "cached" in res2.flags

    @coroutine_test
    async def test_cache_timestamp_not_leaked_on_invalidate(self):
        # A stale cached response invalidated by a fresh network response must
        # not leave its storage timestamp behind on the request meta.
        with self._middleware() as mw:
            req0 = Request("http://example.com")
            res0 = Response(req0.url, headers={"Expires": self.yesterday})
            await self._process_requestresponse(mw, req0, res0)

            assert await mw.process_request(req0) is None
            assert "cache_timestamp" in req0.meta

            res1 = res0.replace(body=b"new body")
            response = mw.process_response(req0, res1)
            assert response is res1
            assert "cache_timestamp" not in req0.meta

    @coroutine_test
    async def test_process_exception(self):
        with self._middleware() as mw:
            res0 = Response(self.request.url, headers={"Expires": self.yesterday})
            req0 = Request(self.request.url)
            await self._process_requestresponse(mw, req0, res0)
            for e in mw.DOWNLOAD_EXCEPTIONS:
                # Simulate encountering an error on download attempts
                assert await mw.process_request(req0) is None
                res1 = mw.process_exception(req0, e("foo"))
                # Use cached response as recovery
                assert isinstance(res1, Response)
                assert "cached" in res1.flags
                self.assertEqualResponse(res0, res1)
            # Do not use cached response for unhandled exceptions
            await mw.process_request(req0)
            assert mw.process_exception(req0, Exception("foo")) is None

    @coroutine_test
    async def test_ignore_response_cache_controls(self):
        sampledata = [
            (200, {"Date": self.yesterday, "Expires": self.tomorrow}),
            (200, {"Date": self.yesterday, "Cache-Control": "no-store,max-age=86405"}),
            (200, {"Age": "299", "Cache-Control": "max-age=300,no-cache"}),
            (300, {"Cache-Control": "no-cache"}),
            (200, {"Expires": self.tomorrow, "Cache-Control": "no-store"}),
        ]
        with self._middleware(
            HTTPCACHE_IGNORE_RESPONSE_CACHE_CONTROLS=["no-cache", "no-store"]
        ) as mw:
            for idx, (status, headers) in enumerate(sampledata):
                req0 = Request(f"http://example-{idx}.com")
                res0 = Response(req0.url, status=status, headers=headers)
                # cache fresh response
                res1 = await self._process_requestresponse(mw, req0, res0)
                self.assertEqualResponse(res1, res0)
                assert "cached" not in res1.flags
                # return fresh cached response without network interaction
                res2 = await self._process_requestresponse(mw, req0, None)
                self.assertEqualResponse(res1, res2)
                assert "cached" in res2.flags


# Concrete test classes that combine storage and policy mixins


class FilesystemStorageTestMixin(StorageTestMixin):
    storage_class = "scrapy.extensions.httpcache.FilesystemCacheStorage"

    def _corrupt_cache_entry(self, storage, spider, request) -> None:
        rpath = Path(storage._get_request_path(spider, request))
        (rpath / "response_body").unlink()

    def _downgrade_cache_entry(self, storage, spider, request) -> None:
        rpath = Path(storage._get_request_path(spider, request))
        (rpath / "response_data").unlink()

    @pytest.mark.parametrize("filename", ["request_headers", "response_headers"])
    def test_storage_header_file(self, filename: str) -> None:
        headers = [
            ("X-Repeated", "1"),
            ("content-type", "text/html"),
            ("x-repeated", "2"),
            ("X-Removed", None),
            ("X-Custom-Header", b"caf\xe9"),
        ]
        expected = (
            b"X-Repeated: 1\r\nX-Repeated: 2\r\ncontent-type: text/html\r\n"
            b"X-Custom-Header: caf\xe9"
        )
        request = Request("http://www.example.com", headers=headers)
        response = Response("http://www.example.com", headers=headers)
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, request, response)
            rpath = Path(storage._get_request_path(crawler.spider, request))
            with storage._open(rpath / filename, "rb") as f:
                assert f.read() == expected


class DbmStorageTestMixin(StorageTestMixin):
    storage_class = "scrapy.extensions.httpcache.DbmCacheStorage"

    def _corrupt_cache_entry(self, storage, spider, request) -> None:
        key = storage._fingerprinter.fingerprint(request).hex()
        storage.db[f"{key}_data"] = b"not a pickle"

    def _downgrade_cache_entry(self, storage, spider, request) -> None:
        key = storage._fingerprinter.fingerprint(request).hex()
        data = pickle.loads(storage.db[f"{key}_data"])
        data = {
            k: v for k, v in data.items() if k in ("status", "url", "headers", "body")
        }
        storage.db[f"{key}_data"] = pickle.dumps(data, protocol=4)


class TestFilesystemStorageWithDummyPolicy(
    FilesystemStorageTestMixin, DummyPolicyTestMixin
):
    policy_class = "scrapy.extensions.httpcache.DummyPolicy"


class TestFilesystemStorageWithRFC2616Policy(
    FilesystemStorageTestMixin, RFC2616PolicyTestMixin
):
    policy_class = "scrapy.extensions.httpcache.RFC2616Policy"


class TestDbmStorageWithDummyPolicy(DbmStorageTestMixin, DummyPolicyTestMixin):
    policy_class = "scrapy.extensions.httpcache.DummyPolicy"


class TestDbmStorageWithRFC2616Policy(DbmStorageTestMixin, RFC2616PolicyTestMixin):
    policy_class = "scrapy.extensions.httpcache.RFC2616Policy"


class TestDbmStorageWithCustomDbmModule(TestDbmStorageWithDummyPolicy):
    dbm_module = "tests.mocks.dummydbm"

    def _get_settings(self, **new_settings) -> dict[str, Any]:
        new_settings.setdefault("HTTPCACHE_DBM_MODULE", self.dbm_module)
        return super()._get_settings(**new_settings)

    def test_custom_dbm_module_loaded(self):
        # make sure our dbm module has been loaded
        with self._storage() as (storage, _):
            assert storage.dbmodule.__name__ == self.dbm_module


class TestFilesystemStorageGzipWithDummyPolicy(TestFilesystemStorageWithDummyPolicy):
    def _get_settings(self, **new_settings) -> dict[str, Any]:
        new_settings.setdefault("HTTPCACHE_GZIP", True)
        return super()._get_settings(**new_settings)

    def _corrupt_cache_entry(self, storage, spider, request) -> None:
        # A spider killed while writing a gzip file leaves it truncated.
        body_path = Path(storage._get_request_path(spider, request), "response_body")
        body_path.write_bytes(body_path.read_bytes()[:-5])


class _ControlledClock:
    """Clock whose current time tests move explicitly."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _HeaderAwareFingerprinter:
    """Fingerprinter that includes the X-Shop header, so requests differing
    only by that header get distinct cache entries."""

    def __init__(self, crawler: Crawler | None = None) -> None:
        self._crawler = crawler

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> _HeaderAwareFingerprinter:
        return cls(crawler)

    def fingerprint(self, request: Request) -> bytes:
        from scrapy.utils.request import fingerprint

        return fingerprint(request, include_headers=["X-Shop"])


class _FakeCacheStorage:
    """In-memory cache storage that hands back a fresh copy of every stored
    response, like the real storages do, and never forgets an entry on its
    own: expiration is entirely the policy's responsibility."""

    def __init__(self, settings: Any) -> None:
        self.entries: dict[bytes, Response] = {}
        self.store_calls = 0
        self._fingerprinter: Any = None
        # Matches the bundled storages: None until the middleware injects its
        # shared clock.
        self._clock: Callable[[], float] | None = None

    def open_spider(self, spider: Spider) -> None:
        self._fingerprinter = spider.crawler.request_fingerprinter

    def close_spider(self, spider: Spider) -> None:
        pass

    def store_response(
        self, spider: Spider, request: Request, response: Response
    ) -> None:
        self.store_calls += 1
        self.entries[self._fingerprinter.fingerprint(request)] = response

    def retrieve_response(self, spider: Spider, request: Request) -> Response | None:
        response = self.entries.get(self._fingerprinter.fingerprint(request))
        if response is None:
            return None
        # The real storages rebuild the response from disk, so flags added to
        # a returned copy never reach the stored object.
        request.meta["cache_timestamp"] = 0.0
        return response.replace()


class TestStaleIfError:
    """Serving a cached response within its stale-if-error window when the
    download fails, based on RFC 5861 with a controllable clock."""

    url = "http://www.example.com/price"
    # The response is stored at t=1000 and is fresh for 100 seconds, with a
    # 50-second stale-if-error window. Window math therefore happens at fixed
    # points on the fake clock.
    store_time = 1000.0
    max_age = 100
    stale_window = 50

    def _settings(self, **new_settings: Any) -> dict[str, Any]:
        settings = {
            "HTTPCACHE_ENABLED": True,
            "HTTPCACHE_POLICY": "scrapy.extensions.httpcache.RFC2616Policy",
            "HTTPCACHE_STORAGE": (
                "tests.test_downloadermiddleware_httpcache._FakeCacheStorage"
            ),
        }
        settings.update(new_settings)
        return settings

    @contextmanager
    def _middleware(self, **new_settings: Any):
        settings = self._settings(**new_settings)
        crawler = get_crawler(Spider, settings)
        crawler.spider = crawler._create_spider("example.com")
        crawler.stats.open_spider()
        try:
            mw = build_from_crawler(HttpCacheMiddleware, crawler)
            mw.spider_opened(crawler.spider)
            clock = _ControlledClock(self.store_time)
            mw._set_clock(clock)
            try:
                yield mw, clock
            finally:
                mw.spider_closed(crawler.spider)
        finally:
            crawler.stats.close_spider()

    def _cached_response(
        self,
        cache_control: str = f"max-age={max_age}, stale-if-error={stale_window}",
        *,
        status: int = 200,
        body: bytes = b"old price",
        headers: dict[str, Any] | None = None,
    ) -> Response:
        response_headers = {
            "Date": email.utils.formatdate(
                self.store_time, usegmt=True
            ),
            "Cache-Control": cache_control,
        }
        if headers:
            response_headers.update(headers)
        return Response(self.url, status=status, headers=response_headers, body=body)

    async def _prime_cache(
        self, mw: HttpCacheMiddleware, response: Response | None = None
    ) -> Request:
        """Run one successful 200 collection so the cache holds a response."""
        request = Request(self.url)
        assert await mw.process_request(request) is None
        mw.process_response(request, response or self._cached_response())
        return request

    async def _failed_download(
        self,
        mw: HttpCacheMiddleware,
        status: int | None = None,
        exception: Exception | None = None,
    ) -> Response:
        """Re-request the primed URL and simulate a failed download."""
        request = Request(self.url)
        assert await mw.process_request(request) is None
        if exception is not None:
            result = mw.process_exception(request, exception)
            assert result is not None
            assert isinstance(result, Response)
            return result
        assert status is not None
        result = mw.process_response(
            request, Response(self.url, status=status, body=b"error page")
        )
        assert isinstance(result, Response)
        return result

    @coroutine_test
    async def test_disabled_by_default_keeps_legacy_5xx_behavior(self):
        # With the option off, a 5xx still goes through the pre-existing
        # policy recovery: the cached response comes back flagged "cached"
        # and no stale stat is recorded.
        with self._middleware() as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age + 10

            response = await self._failed_download(mw, status=503)
            assert response.status == 200
            assert response.body == b"old price"
            assert "cached" in response.flags
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") is None
            assert mw.stats.get_value("httpcache/errorrecovery") is None
            assert mw.stats.get_value("httpcache/revalidate") == 1

    @coroutine_test
    async def test_disabled_by_default_network_exception_keeps_legacy_behavior(self):
        with self._middleware() as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age + 10

            response = await self._failed_download(
                mw, exception=ConnectionLost("down")
            )
            assert response.status == 200
            assert response.flags == ["cached"]
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") is None
            assert mw.stats.get_value("httpcache/errorrecovery") == 1

    @coroutine_test
    async def test_503_within_window_returns_stale_cached_response(self):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age + 10

            response = await self._failed_download(mw, status=503)
            assert response.status == 200
            assert response.body == b"old price"
            assert response.flags == ["cached", "stale"]
            assert mw.stats.get_value("httpcache/stale_if_error") == 1

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    @coroutine_test
    async def test_configured_5xx_codes_fall_back(self, status: int):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age + 10

            response = await self._failed_download(mw, status=status)
            assert response.status == 200
            assert "stale" in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") == 1

    @pytest.mark.parametrize("status", [401, 403, 404])
    @coroutine_test
    async def test_4xx_responses_are_real_failures(self, status: int):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age + 10

            response = await self._failed_download(mw, status=status)
            assert response.status == status
            assert response.body == b"error page"
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") is None

    @coroutine_test
    async def test_5xx_not_in_allowed_codes_is_not_marked_stale(self):
        with self._middleware(
            HTTPCACHE_STALE_IF_ERROR=True,
            HTTPCACHE_STALE_IF_ERROR_HTTP_CODES=[503],
        ) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age + 10

            # 500 is outside the configured codes, so the stale-if-error
            # window is not consulted. The pre-existing RFC2616 recovery may
            # still serve the cached response, but it is not marked stale.
            response = await self._failed_download(mw, status=500)
            assert response.status == 200
            assert response.flags == ["cached"]
            assert mw.stats.get_value("httpcache/stale_if_error") is None
            assert mw.stats.get_value("httpcache/revalidate") == 1

            # The configured 503 uses the window and gets the stale marker.
            allowed = await self._failed_download(mw, status=503)
            assert allowed.status == 200
            assert "stale" in allowed.flags
            assert mw.stats.get_value("httpcache/stale_if_error") == 1

    @pytest.mark.parametrize(
        ("offset", "expected_stale"),
        [
            # One second stale, well inside the window.
            (max_age + 1, True),
            # Last second inside the window: age 149 < 100 + 50.
            (max_age + stale_window - 1, True),
            # Exactly at the window boundary, age == lifetime + window:
            # the window is closed.
            (max_age + stale_window, False),
            # Well past the window.
            (max_age + stale_window + 100, False),
        ],
    )
    @coroutine_test
    async def test_precise_window_boundaries(
        self, offset: int, expected_stale: bool
    ):
        # The policy method gives the exact boolean the middleware branches
        # on, so the boundary can be asserted without the pre-existing 5xx
        # recovery path interfering.
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + offset
            request = Request(self.url)
            cached = mw.storage.retrieve_response(mw.crawler.spider, request)
            assert cached is not None
            assert (
                mw.policy.is_cached_response_stale_if_error(cached, request)
                is expected_stale
            )

            # End-to-end: inside the window the 503 is replaced by the stale
            # cached response; outside it no stale-if-error stat is recorded.
            response = await self._failed_download(mw, status=503)
            if expected_stale:
                assert response.status == 200
                assert "stale" in response.flags
                assert mw.stats.get_value("httpcache/stale_if_error") == 1
            else:
                assert "stale" not in response.flags
                assert mw.stats.get_value("httpcache/stale_if_error") is None

    @coroutine_test
    async def test_fresh_response_is_a_cache_hit_without_downloading(self):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age - 1

            request = Request(self.url)
            response = await mw.process_request(request)
            assert isinstance(response, Response)
            assert response.status == 200
            assert response.flags == ["cached"]
            assert mw.stats.get_value("httpcache/hit") == 1
            assert mw.stats.get_value("httpcache/stale_if_error") is None

    @coroutine_test
    async def test_past_window_keeps_the_real_5xx_with_must_revalidate(self):
        # must-revalidate disables the pre-existing RFC2616 5xx recovery, so
        # once the stale-if-error window closes the 503 reaches the spider.
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(
                mw,
                self._cached_response(
                    "max-age=100, stale-if-error=50, must-revalidate"
                ),
            )
            clock.now = self.store_time + self.max_age + self.stale_window

            response = await self._failed_download(mw, status=503)
            assert response.status == 503
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") is None

    @coroutine_test
    async def test_missing_stale_if_error_directive_never_falls_back(self):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(
                mw,
                self._cached_response(
                    "max-age=100, must-revalidate"
                ),
            )
            clock.now = self.store_time + 110

            response = await self._failed_download(mw, status=503)
            assert response.status == 503
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") is None

    @pytest.mark.parametrize(
        "directive",
        [
            "max-age=100, stale-if-error=50, must-revalidate",
            "max-age=100, stale-if-error=50, private",
        ],
    )
    @coroutine_test
    async def test_revalidation_directives_block_stale_fallback(
        self, directive: str
    ):
        # Both responses are stored, but their directive forbids reusing them
        # once stale. must-revalidate also disables the ordinary 5xx recovery,
        # so the failure reaches the spider; for private the ordinary recovery
        # may still apply, but the stale marker never does.
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw, self._cached_response(directive))
            clock.now = self.store_time + 110

            request = Request(self.url)
            cached = mw.storage.retrieve_response(mw.crawler.spider, request)
            assert cached is not None
            assert not mw.policy.is_cached_response_stale_if_error(
                cached, request
            )

            response = await self._failed_download(mw, status=503)
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") is None
            if "must-revalidate" in directive:
                assert response.status == 503

    @coroutine_test
    async def test_no_store_response_is_never_cached_or_served_stale(self):
        directive = "max-age=100, stale-if-error=50, no-store"
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            # no-store responses are never cached in the first place, so the
            # failed download has nothing to fall back to.
            request = Request(self.url)
            assert await mw.process_request(request) is None
            mw.process_response(request, self._cached_response(directive))
            assert mw.storage.retrieve_response(mw.crawler.spider, request) is None
            clock.now = self.store_time + 110
            response = mw.process_response(
                request, Response(self.url, status=503)
            )
            assert response.status == 503
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") is None

    @coroutine_test
    async def test_request_no_store_does_not_cache_and_does_not_fall_back(self):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            request = Request(self.url, headers={"Cache-Control": "no-store"})
            assert await mw.process_request(request) is None
            mw.process_response(request, self._cached_response())
            assert mw.storage.retrieve_response(mw.crawler.spider, request) is None

            clock.now = self.store_time + 110
            assert await mw.process_request(request) is None
            response = mw.process_response(request, Response(self.url, status=503))
            assert response.status == 503
            assert "stale" not in response.flags

    @coroutine_test
    async def test_dont_cache_request_never_falls_back(self):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + 110

            request = Request(self.url, meta={"dont_cache": True})
            assert await mw.process_request(request) is None
            response = mw.process_response(
                request, Response(self.url, status=503)
            )
            assert response.status == 503
            assert "stale" not in response.flags
            assert mw.process_exception(request, ConnectionLost("down")) is None

    @coroutine_test
    async def test_network_exception_within_window_returns_stale(self):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age + 10

            response = await self._failed_download(
                mw, exception=ConnectionLost("connection lost")
            )
            assert response.status == 200
            assert response.body == b"old price"
            assert response.flags == ["cached", "stale"]
            assert mw.stats.get_value("httpcache/stale_if_error") == 1
            # The legacy error-recovery counter must not double-count it.
            assert mw.stats.get_value("httpcache/errorrecovery") is None

    @coroutine_test
    async def test_unhandled_exception_is_a_real_failure(self):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age + 10

            request = Request(self.url)
            assert await mw.process_request(request) is None
            assert (
                mw.process_exception(request, ValueError("not a download error"))
                is None
            )
            assert mw.stats.get_value("httpcache/stale_if_error") is None

    @coroutine_test
    async def test_consecutive_failures_keep_original_age(self):
        # Repeated failures must not refresh the stored entry's age: every
        # attempt is judged against the original freshness lifetime, so once
        # the window closes, further failures stop being masked.
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)

            clock.now = self.store_time + self.max_age + 10
            first = await self._failed_download(mw, status=503)
            assert "stale" in first.flags

            # A second failure later, still presented as within a window that
            # would reopen if the age had been reset.
            clock.now = self.store_time + self.max_age + 40
            second = await self._failed_download(mw, status=503)
            assert "stale" in second.flags
            assert mw.stats.get_value("httpcache/stale_if_error") == 2

            # No fallback stored a new entry: the storage still holds exactly
            # one object, with its original body and no stale flag.
            assert len(mw.storage.entries) == 1
            stored = next(iter(mw.storage.entries.values()))
            assert stored.body == b"old price"
            assert stored.flags == []
            assert mw.storage.store_calls == 1

            # Past the original window, a third failure is no longer covered
            # by stale-if-error; the ordinary recovery may still answer, but
            # the stale marker and counter stay unchanged.
            clock.now = self.store_time + self.max_age + self.stale_window + 1
            third = await self._failed_download(mw, status=503)
            assert "stale" not in third.flags
            assert mw.stats.get_value("httpcache/stale_if_error") == 2
            assert mw.storage.store_calls == 1

    @coroutine_test
    async def test_5xx_response_is_not_stored_as_a_new_success(self):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age + 10

            await self._failed_download(mw, status=503)
            assert mw.storage.store_calls == 1
            stored = next(iter(mw.storage.entries.values()))
            assert stored.status == 200
            assert stored.body == b"old price"
            assert mw.stats.get_value("httpcache/store") == 1
            assert mw.stats.get_value("httpcache/uncacheable") is None

    @coroutine_test
    async def test_returning_stale_does_not_mutate_the_stored_object(self):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age + 10

            response = await self._failed_download(mw, status=503)
            assert "stale" in response.flags

            # A different request for the same resource reads a clean copy.
            other = Request(self.url)
            retrieved = mw.storage.retrieve_response(mw.crawler.spider, other)
            assert retrieved is not None
            assert retrieved.flags == []
            assert retrieved.body == response.body

    @coroutine_test
    async def test_304_revalidation_follows_the_normal_cache_flow(self):
        # A successful conditional revalidation inside the stale period keeps
        # the cached response through the regular revalidation path, with no
        # stale marking.
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(
                mw,
                self._cached_response(
                    headers={"Last-Modified": email.utils.formatdate(0, usegmt=True)}
                ),
            )
            clock.now = self.store_time + self.max_age + 10

            request = Request(self.url)
            assert await mw.process_request(request) is None
            # The policy attached a conditional validator.
            assert b"If-Modified-Since" in request.headers
            not_modified = Response(
                self.url,
                status=304,
                headers={"Date": request.headers.get(b"Date")},
            )
            response = mw.process_response(request, not_modified)
            assert response.status == 200
            assert "cached" in response.flags
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/revalidate") == 1
            assert mw.stats.get_value("httpcache/stale_if_error") is None

    @coroutine_test
    async def test_normal_200_replaces_stale_cache_entry(self):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age + 10

            request = Request(self.url)
            assert await mw.process_request(request) is None
            fresh = Response(
                self.url,
                status=200,
                headers={
                    "Date": email.utils.formatdate(clock.now, usegmt=True),
                    "Cache-Control": "max-age=100, stale-if-error=50",
                },
                body=b"new price",
            )
            response = mw.process_response(request, fresh)
            assert response is fresh
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/invalidate") == 1
            stored = next(iter(mw.storage.entries.values()))
            assert stored.body == b"new price"

    @coroutine_test
    async def test_different_request_fingerprints_do_not_share_stale_data(self):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + self.max_age + 10

            # A different URL has no cache entry and therefore nothing to
            # fall back to.
            other = Request("http://www.example.com/other-price")
            assert await mw.process_request(other) is None
            response = mw.process_response(
                other, Response(other.url, status=503)
            )
            assert response.status == 503
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") is None

    @coroutine_test
    async def test_different_request_headers_do_not_share_stale_data(self):
        with self._middleware(
            HTTPCACHE_STALE_IF_ERROR=True,
            REQUEST_FINGERPRINTER_CLASS=(
                "tests.test_downloadermiddleware_httpcache._HeaderAwareFingerprinter"
            ),
        ) as (mw, clock):
            keyed = Request(self.url, headers={"X-Shop": "own"})
            assert await mw.process_request(keyed) is None
            mw.process_response(keyed, self._cached_response())
            clock.now = self.store_time + self.max_age + 10

            # The same URL with another header value has its own fingerprint
            # and therefore no cache entry to fall back to.
            competitor = Request(self.url, headers={"X-Shop": "competitor"})
            assert (
                mw._fingerprinter.fingerprint(competitor)
                != mw._fingerprinter.fingerprint(keyed)
            )
            assert await mw.process_request(competitor) is None
            response = mw.process_response(
                competitor, Response(self.url, status=503)
            )
            assert response.status == 503
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") is None

    @coroutine_test
    async def test_age_header_extends_computed_age(self):
        # The Age header pins the computed age to 120 while the Date-based age
        # is lower. The window ends when current age reaches lifetime + window
        # (150), which happens once the Date-based age passes 150, at
        # t = 1000 + 150.
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(
                mw,
                self._cached_response(
                    headers={"Age": "120"},
                ),
            )
            clock.now = self.store_time + 40
            response = await self._failed_download(mw, status=503)
            assert "stale" in response.flags  # age max(40, 120) = 120 < 150

            clock.now = self.store_time + 150
            request = Request(self.url)
            cached = mw.storage.retrieve_response(mw.crawler.spider, request)
            assert cached is not None
            assert not mw.policy.is_cached_response_stale_if_error(
                cached, request
            )  # age 150 == lifetime + window
            response = await self._failed_download(mw, status=503)
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") == 1

    @coroutine_test
    async def test_invalid_stale_if_error_value_is_ignored(self):
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(
                mw,
                self._cached_response(
                    "max-age=100, stale-if-error=soon, must-revalidate"
                ),
            )
            clock.now = self.store_time + 110
            response = await self._failed_download(mw, status=503)
            assert response.status == 503
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") is None

    @coroutine_test
    async def test_unparsable_stale_if_error_value_closes_the_window(self):
        # Without must-revalidate the ordinary 5xx recovery may still answer,
        # but a stale-if-error value that is not an integer must never open
        # the window, so the response is not branded stale.
        with self._middleware(HTTPCACHE_STALE_IF_ERROR=True) as (mw, clock):
            await self._prime_cache(
                mw,
                self._cached_response("max-age=100, stale-if-error=soon"),
            )
            clock.now = self.store_time + 110
            request = Request(self.url)
            cached = mw.storage.retrieve_response(mw.crawler.spider, request)
            assert cached is not None
            assert not mw.policy.is_cached_response_stale_if_error(
                cached, request
            )
            response = await self._failed_download(mw, status=503)
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") is None

    @coroutine_test
    async def test_dummy_policy_never_serves_stale(self):
        # The default DummyPolicy considers every cached response forever
        # fresh, so enabling the option cannot produce a stale fallback: the
        # entry is a plain cache hit before any download is attempted.
        with self._middleware(
            HTTPCACHE_STALE_IF_ERROR=True,
            HTTPCACHE_POLICY="scrapy.extensions.httpcache.DummyPolicy",
        ) as (mw, clock):
            await self._prime_cache(mw)
            clock.now = self.store_time + 10**6

            request = Request(self.url)
            response = await mw.process_request(request)
            assert isinstance(response, Response)
            assert response.flags == ["cached"]
            assert "stale" not in response.flags
            assert mw.stats.get_value("httpcache/stale_if_error") is None

    @coroutine_test
    async def test_set_clock_drives_policy_and_storage_together(self):
        # The injected clock must reach both components that compute age, and
        # a component without a _clock attribute (DummyPolicy) is skipped.
        with self._middleware(
            HTTPCACHE_POLICY="scrapy.extensions.httpcache.DummyPolicy"
        ) as (mw, clock):
            assert not hasattr(mw.policy, "_clock")
            mw._set_clock(clock)  # must not raise on the clockless policy
            assert mw.storage._clock is clock
            await self._prime_cache(mw)
            assert mw.storage.retrieve_response(
                mw.crawler.spider, Request(self.url)
            ) is not None

    @coroutine_test
    async def test_real_filesystem_storage_uses_the_controlled_clock(self):
        # End-to-end through the bundled filesystem storage: a controlled
        # clock drives both the storage's own expiration and the policy's
        # stale-if-error window with no real time passing.
        import tempfile

        with tempfile.TemporaryDirectory() as cachedir:
            settings = self._settings(
                HTTPCACHE_STALE_IF_ERROR=True,
                HTTPCACHE_STORAGE=(
                    "scrapy.extensions.httpcache.FilesystemCacheStorage"
                ),
                HTTPCACHE_DIR=cachedir,
            )
            crawler = get_crawler(Spider, settings)
            crawler.spider = crawler._create_spider("example.com")
            crawler.stats.open_spider()
            try:
                mw = build_from_crawler(HttpCacheMiddleware, crawler)
                mw.spider_opened(crawler.spider)
                clock = _ControlledClock(self.store_time)
                mw._set_clock(clock)
                try:
                    await self._prime_cache(mw)

                    # Inside the window: the stale entry is still on disk and
                    # a 503 falls back to it.
                    clock.now = self.store_time + self.max_age + 10
                    response = await self._failed_download(mw, status=503)
                    assert response.status == 200
                    assert "stale" in response.flags

                    # The storage keeps the original timestamp, so a later
                    # request reads the same entry and the window stays
                    # anchored to the original freshness lifetime.
                    clock.now = self.store_time + self.max_age + self.stale_window
                    later = await self._failed_download(mw, status=503)
                    assert "stale" not in later.flags
                finally:
                    mw.spider_closed(crawler.spider)
            finally:
                crawler.stats.close_spider()


class _LateMiddleware:
    """Downloader middleware that runs after :class:`HttpCacheMiddleware`."""

    priority = 950


class _IgnoreOnRequest(_LateMiddleware):
    def __init__(self) -> None:
        self.gate: Deferred[None] = Deferred()

    async def process_request(self, request: Request) -> None:
        await maybe_deferred_to_future(self.gate)
        raise IgnoreRequest


class _RaiseOnResponse(_LateMiddleware):
    def process_response(self, request: Request, response: Response) -> Response:
        raise ValueError("Late middleware failure")


class _RedirectOnResponse(_LateMiddleware):
    def process_response(self, request: Request, response: Response) -> Request:
        return Request("http://www.example.com/elsewhere")


class _Downloads:
    """Download function that records its calls and, until released, blocks."""

    def __init__(self, status: int = 200) -> None:
        self.urls: list[str] = []
        self.status = status
        self.blocking = True
        self._blocked: list[Deferred[None]] = []

    async def __call__(self, request: Request) -> Response:
        self.urls.append(request.url)
        if self.blocking:
            blocked: Deferred[None] = Deferred()
            self._blocked.append(blocked)
            await maybe_deferred_to_future(blocked)
        return Response(request.url, status=self.status, body=b"body")

    def release(self) -> None:
        self.blocking = False
        while self._blocked:
            self._blocked.pop(0).callback(None)


class TestConcurrentDownloads(TestBase):
    """Requests for a resource that a concurrent request is already
    downloading wait for that download and read its response from the
    cache."""

    policy_class = "scrapy.extensions.httpcache.DummyPolicy"
    storage_class = "scrapy.extensions.httpcache.FilesystemCacheStorage"
    url = "http://www.example.com/"

    @contextmanager
    def _manager(
        self, *late_middlewares: type[_LateMiddleware], **new_settings: Any
    ) -> Generator[tuple[DownloaderMiddlewareManager, Crawler]]:
        new_settings.setdefault(
            "DOWNLOADER_MIDDLEWARES",
            {mw: mw.priority for mw in late_middlewares},
        )
        with self._get_crawler(**new_settings) as crawler:
            manager = DownloaderMiddlewareManager.from_crawler(crawler)
            crawler.signals.send_catch_log(signals.spider_opened, spider=crawler.spider)
            try:
                yield manager, crawler
            finally:
                crawler.signals.send_catch_log(
                    signals.spider_closed, spider=crawler.spider, reason="finished"
                )

    @staticmethod
    def _middleware_of(manager: DownloaderMiddlewareManager, base: type) -> Any:
        return next(mw for mw in manager.middlewares if isinstance(mw, base))

    @staticmethod
    async def _wait_until(condition: Callable[[], Any]) -> None:
        for _ in range(100):
            if condition():
                return
            await sleep(0)
        raise AssertionError("The expected state was never reached")

    async def _start(
        self,
        manager: DownloaderMiddlewareManager,
        download: _Downloads,
        crawler: Crawler,
    ) -> tuple[Request, Deferred[Any], Deferred[Any]]:
        """Start two concurrent requests for the same URL, the second one
        waiting for the first one.

        Return the first request, which callers must keep a reference to for
        as long as they do not want it garbage-collected, and the results of
        both requests.
        """
        request = Request(self.url)
        first = deferred_from_coro(manager.download_async(download, request))
        await self._wait_until(lambda: download.urls)
        second = deferred_from_coro(manager.download_async(download, Request(self.url)))
        stats = crawler.stats
        assert stats
        await self._wait_until(lambda: stats.get_value("httpcache/wait"))
        return request, first, second

    @coroutine_test
    async def test_cached(self):
        with self._manager() as (manager, crawler):
            download = _Downloads()
            _request, first, second = await self._start(manager, download, crawler)
            download.release()

            assert isinstance(await maybe_deferred_to_future(first), Response)
            response = await maybe_deferred_to_future(second)
            assert isinstance(response, Response)
            assert "cached" in response.flags
            assert response.body == b"body"
            assert download.urls == [self.url]

    @coroutine_test
    async def test_uncacheable_response(self):
        with self._manager(HTTPCACHE_IGNORE_HTTP_CODES=[404]) as (manager, crawler):
            download = _Downloads(status=404)
            _request, first, second = await self._start(manager, download, crawler)
            download.release()

            for result in (first, second):
                response = await maybe_deferred_to_future(result)
                assert isinstance(response, Response)
                assert "cached" not in response.flags
            assert download.urls == [self.url] * 2

    @coroutine_test
    async def test_download_exception(self):
        with self._manager() as (manager, crawler):
            download = _Downloads()
            _request, first, second = await self._start(manager, download, crawler)
            download._blocked.pop(0).errback(ValueError("Download failure"))

            with pytest.raises(ValueError, match="Download failure"):
                await maybe_deferred_to_future(first)
            download.release()
            assert isinstance(await maybe_deferred_to_future(second), Response)
            assert download.urls == [self.url] * 2

    @coroutine_test
    async def test_ignored_by_a_later_middleware(self):
        with self._manager(_IgnoreOnRequest) as (manager, crawler):
            download = _Downloads()
            late = self._middleware_of(manager, _IgnoreOnRequest)
            middleware = self._middleware_of(manager, HttpCacheMiddleware)
            request = Request(self.url)
            first = deferred_from_coro(manager.download_async(download, request))
            await self._wait_until(lambda: middleware._downloading)
            second = deferred_from_coro(
                manager.download_async(download, Request(self.url))
            )
            stats = crawler.stats
            assert stats
            await self._wait_until(lambda: stats.get_value("httpcache/wait"))
            late.gate.callback(None)

            for result in (first, second):
                with pytest.raises(IgnoreRequest):
                    await maybe_deferred_to_future(result)
            assert not download.urls

    @coroutine_test
    async def test_response_replaced_by_a_later_middleware(self):
        # A middleware with a higher priority returning a request from
        # process_response() keeps HttpCacheMiddleware.process_response() from
        # running, so waiters are only woken up once the first request object
        # is garbage-collected.
        with self._manager(_RedirectOnResponse) as (manager, crawler):
            download = _Downloads()
            request, first, second = await self._start(manager, download, crawler)
            middleware = self._middleware_of(manager, HttpCacheMiddleware)
            download.release()

            assert isinstance(await maybe_deferred_to_future(first), Request)
            assert middleware._downloading

            def collected() -> bool:
                gc.collect()
                return not middleware._downloading

            del request
            await self._wait_until(collected)
            assert isinstance(await maybe_deferred_to_future(second), Request)
            assert download.urls == [self.url] * 2

    @coroutine_test
    async def test_response_error_in_a_later_middleware(self):
        # A middleware with a higher priority raising from process_response()
        # also keeps HttpCacheMiddleware.process_response() from running, but
        # here the resulting failure keeps the first request object alive, so
        # waiters block until the spider is closed, unless
        # DOWNLOADER_MIDDLEWARE_RESPONSE_EXCEPTIONS is True.
        with self._manager(_RaiseOnResponse) as (manager, crawler):
            download = _Downloads()
            request, first, second = await self._start(manager, download, crawler)
            middleware = self._middleware_of(manager, HttpCacheMiddleware)
            download.release()

            with pytest.raises(ValueError, match="Late middleware failure"):
                await maybe_deferred_to_future(first)
            del request
            gc.collect()
            assert middleware._downloading
            assert not second.called

            middleware.spider_closed(crawler.spider)
            with pytest.raises(ValueError, match="Late middleware failure"):
                await maybe_deferred_to_future(second)
            assert download.urls == [self.url] * 2

    @coroutine_test
    async def test_response_error_in_a_later_middleware_reaching_process_exception(
        self,
    ):
        with self._manager(
            _RaiseOnResponse, DOWNLOADER_MIDDLEWARE_RESPONSE_EXCEPTIONS=True
        ) as (manager, crawler):
            download = _Downloads()
            _request, first, second = await self._start(manager, download, crawler)
            download.release()

            for result in (first, second):
                with pytest.raises(ValueError, match="Late middleware failure"):
                    await maybe_deferred_to_future(result)
            assert download.urls == [self.url] * 2
            assert not self._middleware_of(manager, HttpCacheMiddleware)._downloading

    @coroutine_test
    async def test_cancelled_waiter(self):
        with self._manager() as (manager, crawler):
            download = _Downloads()
            _request, first, second = await self._start(manager, download, crawler)
            second.cancel()
            download.release()

            assert isinstance(await maybe_deferred_to_future(first), Response)
            assert download.urls == [self.url]
            assert not self._middleware_of(manager, HttpCacheMiddleware)._downloading


class TestInFlightTracking(TestBase):
    """Bookkeeping of requests that are being downloaded."""

    policy_class = "scrapy.extensions.httpcache.DummyPolicy"
    storage_class = "scrapy.extensions.httpcache.FilesystemCacheStorage"
    url = "http://www.example.com/"

    @coroutine_test
    async def test_garbage_collected_request(self):
        with self._middleware() as mw:
            request = Request(self.url)
            assert await mw.process_request(request) is None
            assert mw._downloading

            del request
            gc.collect()
            assert not mw._downloading

    @coroutine_test
    async def test_same_request_twice(self):
        with self._middleware() as mw:
            request = Request(self.url)
            assert await mw.process_request(request) is None
            assert await mw.process_request(request) is None

    @coroutine_test
    async def test_spider_closed(self):
        with self._middleware() as mw:
            request = Request(self.url)
            assert await mw.process_request(request) is None
            waiting = deferred_from_coro(mw.process_request(Request(self.url)))
            assert mw.crawler.stats
            for _ in range(100):
                if mw.crawler.stats.get_value("httpcache/wait"):
                    break
                await sleep(0)
            assert not waiting.called

            fingerprint = mw._fingerprinter.fingerprint(request)
            waiters = mw._downloading[fingerprint]

            assert mw.crawler.spider
            mw.spider_closed(mw.crawler.spider)
            assert await maybe_deferred_to_future(waiting) is None
            # The woken request found no cache entry, so it took over as the
            # in-flight download, with a fresh list of waiters.
            assert not waiters
            assert mw._downloading.get(fingerprint) is not waiters


class ScopeTestMixin(TestBase):
    policy_class = "scrapy.extensions.httpcache.DummyPolicy"

    @contextmanager
    def _spider_storage(self, crawler: Crawler, name: str) -> Generator[Any]:
        spider = crawler._create_spider(name)
        storage = load_object(self.storage_class)(crawler.settings)
        storage.open_spider(spider)
        try:
            yield storage, spider
        finally:
            storage.close_spider(spider)

    def _cross_spider_retrieval(self, scope: str) -> Any:
        # Storages are opened one at a time because a DBM database shared by
        # two spiders does not support concurrent access.
        with self._get_crawler(HTTPCACHE_SCOPE=scope) as crawler:
            with self._spider_storage(crawler, "a") as (storage, spider):
                storage.store_response(spider, self.request, self.response)
            with self._spider_storage(crawler, "b") as (storage, spider):
                return storage.retrieve_response(spider, self.request)

    def test_spider_scope(self):
        assert self._cross_spider_retrieval("spider") is None

    def test_no_scope(self):
        assert self._cross_spider_retrieval("none") is not None

    def test_unknown_scope(self):
        with (
            self._get_crawler(HTTPCACHE_SCOPE="unknown") as crawler,
            pytest.raises(ValueError, match="Unsupported HTTPCACHE_SCOPE"),
        ):
            load_object(self.storage_class)(crawler.settings)


class TestFilesystemStorageScope(ScopeTestMixin):
    storage_class = "scrapy.extensions.httpcache.FilesystemCacheStorage"

    def test_domain_scope(self):
        assert self._cross_spider_retrieval("domain") is not None

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://user:pass@WWW.Example.com:8080/", "www.example.com"),
            ("http://[::1]/", "%3A%3A1"),
            ("file:///tmp/t.txt", "_"),
        ],
    )
    def test_domain_scope_path(self, url, expected):
        with (
            self._get_crawler(HTTPCACHE_SCOPE="domain") as crawler,
            self._spider_storage(crawler, "a") as (storage, spider),
        ):
            path = Path(storage._get_request_path(spider, Request(url)))
            assert path.relative_to(self.tmpdir).parts[0] == expected


class TestDbmStorageScope(ScopeTestMixin):
    storage_class = "scrapy.extensions.httpcache.DbmCacheStorage"

    def test_domain_scope_unsupported(self):
        with (
            self._get_crawler(HTTPCACHE_SCOPE="domain") as crawler,
            pytest.raises(ValueError, match="Unsupported HTTPCACHE_SCOPE"),
        ):
            load_object(self.storage_class)(crawler.settings)


@pytest.mark.parametrize(
    ("string", "expected"),
    [
        # RFC examples
        ("Sun, 06 Nov 1994 08:49:37 GMT", 784111777),
        ("Sunday, 06-Nov-94 08:49:37 GMT", 784111777),
        ("Sun Nov  6 08:49:37 1994", 784111777),
        (None, None),
        ("foo", None),
    ],
)
def test_rfc1123_to_epoch(string: str | None, expected: int | None) -> None:
    assert rfc1123_to_epoch(string) == expected
