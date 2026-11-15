from __future__ import annotations

import email.utils
import gc
import hashlib
import json
import logging
import os
import pickle
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from twisted.internet.defer import Deferred

from scrapy import signals
from scrapy.core.downloader.middleware import DownloaderMiddlewareManager
from scrapy.downloadermiddlewares.httpcache import HttpCacheMiddleware
from scrapy.exceptions import IgnoreRequest
from scrapy.extensions.httpcache import (
    DummyPolicy,
    GenerationCorruptError,
    GenerationPublishError,
    GenerationUncertainError,
    rfc1123_to_epoch,
)
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


class GenerationStorageTestMixin(FilesystemStorageTestMixin):
    """Generation-publishing mode runs the whole filesystem storage suite."""

    def _get_settings(self, **new_settings: Any) -> dict[str, Any]:
        new_settings.setdefault("HTTPCACHE_GENERATION", True)
        return super()._get_settings(**new_settings)

    def _generation_path(self, storage: Any, spider: Spider, request: Request) -> Path:
        entry_dir = Path(storage._get_request_path(spider, request))
        pointer = storage._read_pointer(entry_dir)
        assert pointer is not None
        return entry_dir / f"gen-{pointer['generation']}"

    def _corrupt_cache_entry(self, storage, spider, request) -> None:
        (self._generation_path(storage, spider, request) / "response_body").unlink()

    def test_storage_old_cache_entry(self):
        # The generation layout always writes response_data, so there is no
        # Scrapy 2.14-style entry to downgrade to; the missing-artifact
        # behavior is covered in the generation contract tests.
        pass

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
            entry_dir = Path(storage._get_request_path(crawler.spider, request))
            pointer = storage._read_pointer(entry_dir)
            assert pointer is not None
            generation_path = entry_dir / f"gen-{pointer['generation']}"
            manifest = storage._load_manifest(
                generation_path, pointer["manifest_digest"]
            )
            with storage._payload_opener(bool(manifest["gzip"]))(
                generation_path / filename, "rb"
            ) as f:
                assert f.read() == expected


class TestFilesystemGenerationStorageWithDummyPolicy(
    GenerationStorageTestMixin, DummyPolicyTestMixin
):
    policy_class = "scrapy.extensions.httpcache.DummyPolicy"


class TestFilesystemGenerationStorageWithRFC2616Policy(
    GenerationStorageTestMixin, RFC2616PolicyTestMixin
):
    policy_class = "scrapy.extensions.httpcache.RFC2616Policy"


class TestFilesystemGenerationStorageGzipWithDummyPolicy(
    TestFilesystemGenerationStorageWithDummyPolicy
):
    def _get_settings(self, **new_settings: Any) -> dict[str, Any]:
        new_settings.setdefault("HTTPCACHE_GZIP", True)
        return super()._get_settings(**new_settings)


class TestFilesystemGenerationPublish(TestBase):
    """Contracts of the generation-publishing mode: atomicity, recovery and
    cleanup under injected disk failures."""

    policy_class = "scrapy.extensions.httpcache.DummyPolicy"
    storage_class = "scrapy.extensions.httpcache.FilesystemCacheStorage"

    def _get_settings(self, **new_settings: Any) -> dict[str, Any]:
        new_settings.setdefault("HTTPCACHE_GENERATION", True)
        return super()._get_settings(**new_settings)

    @staticmethod
    def _generation_entries(entry_dir: Path) -> list[str]:
        return sorted(
            child.name for child in entry_dir.iterdir() if child.name.startswith("gen-")
        )

    @staticmethod
    def _temporary_entries(entry_dir: Path) -> list[str]:
        return sorted(
            child.name
            for child in entry_dir.iterdir()
            if child.name.startswith((".staging-", ".current-"))
        )

    def test_repeated_store_publishes_a_single_generation(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            second = self.response.replace(body=b"second body")
            storage.store_response(crawler.spider, self.request, second)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            assert self._generation_entries(entry_dir) == ["gen-1"]
            assert self._temporary_entries(entry_dir) == []
            cached = storage.retrieve_response(crawler.spider, self.request.copy())
            self.assertEqualResponse(second, cached)

    def test_two_urls_are_isolated(self) -> None:
        other_request = Request("http://other.example.com/path")
        other_response = Response("http://other.example.com/path", body=b"other body")
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            storage.store_response(crawler.spider, other_request, other_response)
            first_path = Path(storage._get_request_path(crawler.spider, self.request))
            other_path = Path(storage._get_request_path(crawler.spider, other_request))
            assert first_path != other_path
            self.assertEqualResponse(
                self.response,
                storage.retrieve_response(crawler.spider, self.request.copy()),
            )
            self.assertEqualResponse(
                other_response,
                storage.retrieve_response(crawler.spider, other_request.copy()),
            )

    def test_reopen_storage_keeps_the_published_generation(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
        with self._storage() as (storage, crawler):
            cached = storage.retrieve_response(crawler.spider, self.request.copy())
            self.assertEqualResponse(self.response, cached)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            assert self._temporary_entries(entry_dir) == []

    def test_empty_body_round_trip(self) -> None:
        response = Response("http://www.example.com", body=b"")
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, response)
            cached = storage.retrieve_response(crawler.spider, self.request.copy())
            assert cached is not None
            assert cached.body == b""

    @pytest.mark.parametrize("use_gzip", [False, True])
    def test_compression_format_is_self_describing(self, use_gzip: bool) -> None:
        with self._storage(HTTPCACHE_GZIP=use_gzip) as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            pointer = storage._read_pointer(entry_dir)
            assert pointer is not None
            generation_path = entry_dir / f"gen-{pointer['generation']}"
            manifest = storage._load_manifest(
                generation_path, pointer["manifest_digest"]
            )
            assert manifest["gzip"] is use_gzip
            payload = (generation_path / "response_body").read_bytes()
            assert payload.startswith(b"\x1f\x8b") is use_gzip
            cached = storage.retrieve_response(crawler.spider, self.request.copy())
            self.assertEqualResponse(self.response, cached)

    def test_fsync_disabled_never_calls_fsync(self) -> None:
        with self._storage(HTTPCACHE_FSYNC=False) as (storage, crawler):
            with mock.patch(
                "scrapy.extensions.httpcache.os.fsync",
                side_effect=AssertionError("fsync must not be called"),
            ):
                storage.store_response(crawler.spider, self.request, self.response)
                cached = storage.retrieve_response(crawler.spider, self.request.copy())
            self.assertEqualResponse(self.response, cached)

    def test_credentials_are_not_written_to_disk(self) -> None:
        request = Request(
            "http://www.example.com",
            headers={
                "Authorization": "Bearer secret-token",
                "Proxy-Authorization": "Basic dXNlcjpwYXNz",
                "Content-Type": "text/html",
            },
        )
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, request, self.response)
            assert request.headers[b"Authorization"] == b"Bearer secret-token"
            entry_dir = Path(storage._get_request_path(crawler.spider, request))
            for path in entry_dir.rglob("*"):
                if path.is_file():
                    assert b"secret-token" not in path.read_bytes()
                    assert b"Basic dXNlcjpwYXNz" not in path.read_bytes()

    @pytest.mark.parametrize(
        ("failure", "expected_error"),
        [
            ("payload_write", GenerationPublishError),
            ("payload_fsync", GenerationPublishError),
            ("staging_fsync", GenerationPublishError),
            ("verification", GenerationPublishError),
            ("generation_replace", GenerationPublishError),
            ("pointer_replace", GenerationPublishError),
            ("entry_fsync", GenerationUncertainError),
        ],
    )
    def test_failure_at_each_stage_keeps_a_readable_generation(
        self, failure: str, expected_error: type[Exception]
    ) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            updated = self.response.replace(
                body=b"updated body", headers={"X-Stage": failure}
            )
            real_fsync = os.fsync
            real_open = storage._open

            def payload_write(path: str | os.PathLike[str], mode: str, *args, **kwargs):
                if (
                    mode == "wb"
                    and ".staging-" in str(path)
                    and str(path).endswith("response_body")
                ):
                    raise OSError("injected payload write failure")
                return real_open(path, mode, *args, **kwargs)

            def payload_fsync(fd: int) -> None:
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("injected payload fsync failure")
                real_fsync(fd)

            def staging_fsync(fd: int) -> None:
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("injected staging fsync failure")
                real_fsync(fd)

            real_replace = Path.replace

            def generation_replace(self: Path, target: Path) -> Path:
                if target.name.endswith("gen-1"):
                    raise OSError("injected generation rename failure")
                return real_replace(self, target)

            def pointer_replace(self: Path, target: Path) -> Path:
                if target.name == "current.json":
                    raise OSError("injected pointer rename failure")
                return real_replace(self, target)

            def entry_fsync(fd: int) -> None:
                stat_result = os.fstat(fd)
                if (
                    stat.S_ISDIR(stat_result.st_mode)
                    and stat_result.st_ino == entry_dir.stat().st_ino
                ):
                    raise OSError("injected entry fsync failure")
                real_fsync(fd)

            patches = {
                "payload_write": mock.patch.object(
                    storage, "_open", side_effect=payload_write
                ),
                "payload_fsync": mock.patch(
                    "scrapy.extensions.httpcache.os.fsync",
                    side_effect=payload_fsync,
                ),
                "staging_fsync": mock.patch(
                    "scrapy.extensions.httpcache.os.fsync",
                    side_effect=staging_fsync,
                ),
                "verification": mock.patch.object(
                    storage,
                    "_verify_generation",
                    side_effect=GenerationPublishError("injected"),
                ),
                "generation_replace": mock.patch.object(
                    Path, "replace", autospec=True, side_effect=generation_replace
                ),
                "pointer_replace": mock.patch.object(
                    Path, "replace", autospec=True, side_effect=pointer_replace
                ),
                "entry_fsync": mock.patch(
                    "scrapy.extensions.httpcache.os.fsync",
                    side_effect=entry_fsync,
                ),
            }
            with patches[failure], pytest.raises(expected_error):
                storage.store_response(crawler.spider, self.request, updated)

            assert self._temporary_entries(entry_dir) == []
            cached = storage.retrieve_response(crawler.spider, self.request.copy())
            if failure == "entry_fsync":
                # The pointer was switched before the failure, so the new
                # generation may be visible; what matters is that it is
                # complete, not that the old one was kept.
                assert cached is not None
                assert cached.body in {self.response.body, updated.body}
            else:
                self.assertEqualResponse(self.response, cached)
                assert self._generation_entries(entry_dir) == ["gen-0"]

            # A later attempt without the failure publishes successfully.
            storage.store_response(crawler.spider, self.request, updated)
            recovered = storage.retrieve_response(crawler.spider, self.request.copy())
            self.assertEqualResponse(updated, recovered)
            assert self._temporary_entries(entry_dir) == []

    def test_staging_directory_left_by_a_crash_is_cleaned_up(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            orphan = entry_dir / ".staging-99-1-0"
            orphan.mkdir()
            (orphan / "response_body").write_bytes(b"partial")
            storage.store_response(
                crawler.spider,
                self.request,
                self.response.replace(body=b"after crash"),
            )
            assert not orphan.exists()
            assert self._temporary_entries(entry_dir) == []

    @pytest.mark.parametrize("use_gzip", [False, True])
    def test_reopened_storage_after_each_failure_keeps_body_and_headers(
        self, use_gzip: bool
    ) -> None:
        # Every injected failure must leave an entry that a freshly opened
        # storage reads back with the original body and headers.
        original = Response(
            self.request.url,
            headers={"Content-Type": "text/html", "X-Keep": "yes"},
            body=b"original body",
        )
        updated = original.replace(
            body=b"updated body", headers={"Content-Type": "text/html", "X-Keep": "no"}
        )
        with self._storage(HTTPCACHE_GZIP=use_gzip) as (storage, crawler):
            storage.store_response(crawler.spider, self.request, original)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            real_fsync = os.fsync
            real_open = storage._open

            def payload_write(path, mode, *args, **kwargs):
                if (
                    mode == "wb"
                    and ".staging-" in str(path)
                    and str(path).endswith("response_body")
                ):
                    raise OSError("injected payload write failure")
                return real_open(path, mode, *args, **kwargs)

            def file_fsync(fd: int) -> None:
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("injected file fsync failure")
                real_fsync(fd)

            real_replace = Path.replace

            def pointer_replace(self: Path, target: Path) -> Path:
                if target.name == "current.json":
                    raise OSError("injected pointer rename failure")
                return real_replace(self, target)

            failures = [
                mock.patch.object(storage, "_open", side_effect=payload_write),
                mock.patch(
                    "scrapy.extensions.httpcache.os.fsync",
                    side_effect=file_fsync,
                ),
                mock.patch.object(
                    Path, "replace", autospec=True, side_effect=pointer_replace
                ),
            ]
            for failure in failures:
                with failure, pytest.raises(GenerationPublishError):
                    storage.store_response(crawler.spider, self.request, updated)

        # A brand-new storage instance reads only complete generations.
        with self._storage(HTTPCACHE_GZIP=use_gzip) as (storage, crawler):
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            assert self._temporary_entries(entry_dir) == []
            cached = storage.retrieve_response(crawler.spider, self.request.copy())
            assert cached is not None
            assert cached.body == original.body
            assert cached.headers == original.headers

        # And a clean store after the failures is read back identically.
        with self._storage(HTTPCACHE_GZIP=use_gzip) as (storage, crawler):
            storage.store_response(crawler.spider, self.request, updated)
        with self._storage(HTTPCACHE_GZIP=use_gzip) as (storage, crawler):
            cached = storage.retrieve_response(crawler.spider, self.request.copy())
            assert cached is not None
            assert cached.body == updated.body
            assert cached.headers == updated.headers

    def test_legacy_cache_is_invisible_without_the_explicit_policy(
        self,
    ) -> None:
        with self._storage(HTTPCACHE_GENERATION=False) as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
        with self._storage() as (storage, crawler):
            assert (
                storage.retrieve_response(crawler.spider, self.request.copy()) is None
            )

    def test_legacy_cache_is_read_with_the_explicit_policy(self) -> None:
        with self._storage(HTTPCACHE_GENERATION=False) as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
        with self._storage(HTTPCACHE_GENERATION_READ_LEGACY=True) as (storage, crawler):
            cached = storage.retrieve_response(crawler.spider, self.request.copy())
            self.assertEqualResponse(self.response, cached)
            # Publishing a generation makes the legacy entry unreachable.
            storage.store_response(
                crawler.spider,
                self.request,
                self.response.replace(body=b"generation body"),
            )
            cached = storage.retrieve_response(crawler.spider, self.request.copy())
            assert cached is not None
            assert cached.body == b"generation body"

    def test_missing_optional_response_data_is_a_miss_contract(self) -> None:
        # response_data is a required, checksummed generation artifact; an
        # entry missing it cannot be mistaken for a complete generation.
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            pointer = storage._read_pointer(entry_dir)
            assert pointer is not None
            generation_path = entry_dir / f"gen-{pointer['generation']}"
            (generation_path / "response_data").unlink()
            with pytest.raises(GenerationCorruptError):
                storage.retrieve_response(crawler.spider, self.request.copy())

    def test_tampered_body_is_reported_as_corrupt(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            pointer = storage._read_pointer(entry_dir)
            assert pointer is not None
            generation_path = entry_dir / f"gen-{pointer['generation']}"
            # Same length, different content: the size check passes and the
            # digest check has to catch the tampering.
            body_path = generation_path / "response_body"
            body_path.write_bytes(b"X" * len(self.response.body))
            with pytest.raises(GenerationCorruptError, match="Digest mismatch"):
                storage.retrieve_response(crawler.spider, self.request.copy())

    def test_resized_body_is_reported_as_corrupt(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            pointer = storage._read_pointer(entry_dir)
            assert pointer is not None
            generation_path = entry_dir / f"gen-{pointer['generation']}"
            (generation_path / "response_body").write_bytes(b"tampered")
            with pytest.raises(
                GenerationCorruptError, match="Invalid manifest payload list"
            ):
                storage.retrieve_response(crawler.spider, self.request.copy())

    def test_missing_generation_directory_is_reported_as_corrupt(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            pointer = storage._read_pointer(entry_dir)
            assert pointer is not None
            shutil.rmtree(entry_dir / f"gen-{pointer['generation']}")
            with pytest.raises(GenerationCorruptError, match="missing generation"):
                storage.retrieve_response(crawler.spider, self.request.copy())

    def test_manifest_cannot_reference_files_outside_the_entry(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            pointer = storage._read_pointer(entry_dir)
            assert pointer is not None
            generation_path = entry_dir / f"gen-{pointer['generation']}"
            with pytest.raises(GenerationCorruptError):
                storage._manifest_path(generation_path, "../../../etc/passwd")

    def test_garbage_pointer_is_reported_as_corrupt(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            (entry_dir / "current.json").write_bytes(b"{not json")
            with pytest.raises(GenerationCorruptError):
                storage.retrieve_response(crawler.spider, self.request.copy())

    @pytest.mark.parametrize(
        "pointer",
        [
            {"version": 99, "generation": 0, "manifest_digest": "0"},
            {"version": 1, "generation": -1, "manifest_digest": "0"},
            {"version": 1, "generation": 0},
        ],
    )
    def test_malformed_pointer_is_reported_as_corrupt(
        self, pointer: dict[str, Any]
    ) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            (entry_dir / "current.json").write_text(json.dumps(pointer))
            with pytest.raises(GenerationCorruptError):
                storage.retrieve_response(crawler.spider, self.request.copy())

    def _stored_manifest(
        self, storage: Any, spider: Spider, request: Request
    ) -> tuple[Path, Path, dict[str, Any]]:
        entry_dir = Path(storage._get_request_path(spider, request))
        pointer = storage._read_pointer(entry_dir)
        assert pointer is not None
        generation_path = entry_dir / f"gen-{pointer['generation']}"
        manifest = json.loads((generation_path / "manifest.json").read_text())
        return entry_dir, generation_path, manifest

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            ("version", "Unreadable manifest"),
            ("files", "Unreadable manifest"),
            ("metadata", "Unreadable manifest"),
            ("gzip", "Unreadable manifest"),
            ("size", "Invalid manifest payload list"),
            ("digest", "Invalid manifest payload list"),
        ],
    )
    def test_malformed_manifest_is_reported_as_corrupt(
        self, mutate: str, message: str
    ) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir, generation_path, manifest = self._stored_manifest(
                storage, crawler.spider, self.request
            )
            if mutate == "version":
                manifest["version"] = 99
            elif mutate == "files":
                manifest["files"] = []
            elif mutate == "metadata":
                manifest["metadata"] = []
            elif mutate == "gzip":
                manifest["gzip"] = "yes"
            elif mutate == "size":
                manifest["files"]["response_body"]["size"] = 999
            elif mutate == "digest":
                manifest["files"]["response_body"]["sha256"] = 1
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode()
            (generation_path / "manifest.json").write_bytes(manifest_bytes)
            pointer_path = entry_dir / "current.json"
            pointer = json.loads(pointer_path.read_text())
            pointer["manifest_digest"] = hashlib.sha256(manifest_bytes).hexdigest()
            pointer_path.write_text(
                json.dumps(pointer, sort_keys=True, separators=(",", ":"))
            )
            with pytest.raises(GenerationCorruptError, match=message):
                storage.retrieve_response(crawler.spider, self.request.copy())

    def test_manifest_digest_mismatch_is_reported_as_corrupt(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir, _, _ = self._stored_manifest(
                storage, crawler.spider, self.request
            )
            pointer_path = entry_dir / "current.json"
            pointer = json.loads(pointer_path.read_text())
            pointer["manifest_digest"] = "0" * 64
            pointer_path.write_text(
                json.dumps(pointer, sort_keys=True, separators=(",", ":"))
            )
            with pytest.raises(GenerationCorruptError, match="does not match"):
                storage.retrieve_response(crawler.spider, self.request.copy())

    def test_missing_manifest_is_reported_as_corrupt(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            _, generation_path, _ = self._stored_manifest(
                storage, crawler.spider, self.request
            )
            (generation_path / "manifest.json").unlink()
            with pytest.raises(GenerationCorruptError, match="Missing manifest"):
                storage.retrieve_response(crawler.spider, self.request.copy())

    def test_invalid_manifest_metadata_is_reported_as_corrupt(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir, generation_path, manifest = self._stored_manifest(
                storage, crawler.spider, self.request
            )
            manifest["metadata"]["status"] = "not-a-status"
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode()
            (generation_path / "manifest.json").write_bytes(manifest_bytes)
            pointer_path = entry_dir / "current.json"
            pointer = json.loads(pointer_path.read_text())
            pointer["manifest_digest"] = hashlib.sha256(manifest_bytes).hexdigest()
            pointer_path.write_text(
                json.dumps(pointer, sort_keys=True, separators=(",", ":"))
            )
            with pytest.raises(GenerationCorruptError, match="Invalid metadata"):
                storage.retrieve_response(crawler.spider, self.request.copy())

    def test_preexisting_generation_directory_seeds_the_next_id(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            # A leftover generation directory without a pointer must be
            # accounted for when choosing the next generation id.
            (entry_dir / "gen-7").mkdir()
            (entry_dir / "gen-not-a-number").mkdir()
            storage.store_response(crawler.spider, self.request, self.response)
            pointer = storage._read_pointer(entry_dir)
            assert pointer is not None
            assert pointer["generation"] == 8

    def test_stale_generation_directories_are_reported_as_corrupt(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            # A symlink entry cannot escape the cache root.
            outside = entry_dir.parent / "outside"
            outside.mkdir()
            (entry_dir / "gen-0").rename(entry_dir / "gen-0-real")
            (entry_dir / "gen-0").symlink_to(outside, target_is_directory=True)
            with pytest.raises(GenerationCorruptError):
                storage.retrieve_response(crawler.spider, self.request.copy())

    def test_verification_failures_are_publish_errors(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            real_verify = storage._verify_generation

            def broken_verify(generation_path, manifest, manifest_bytes):
                # Simulate the on-disk payload changing after the digest was
                # computed but before verification reads it back, without
                # changing its length so that the digest check fires.
                response_body = generation_path / "response_body"
                response_body.write_bytes(b"X" * response_body.stat().st_size)
                return real_verify(generation_path, manifest, manifest_bytes)

            with (
                mock.patch.object(
                    storage, "_verify_generation", side_effect=broken_verify
                ),
                pytest.raises(GenerationPublishError, match="Verification"),
            ):
                storage.store_response(
                    crawler.spider,
                    self.request,
                    self.response.replace(body=b"second"),
                )
            assert self._temporary_entries(entry_dir) == []
            cached = storage.retrieve_response(crawler.spider, self.request.copy())
            self.assertEqualResponse(self.response, cached)

    def test_manifest_rewrite_before_verification_is_a_publish_error(
        self,
    ) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            real_verify = storage._verify_generation

            def broken_verify(generation_path, manifest, manifest_bytes):
                # Simulate the manifest file being overwritten after the
                # in-memory manifest was serialized.
                (generation_path / "manifest.json").write_bytes(b"{}")
                return real_verify(generation_path, manifest, manifest_bytes)

            with (
                mock.patch.object(
                    storage, "_verify_generation", side_effect=broken_verify
                ),
                pytest.raises(GenerationPublishError, match="Verification"),
            ):
                storage.store_response(
                    crawler.spider,
                    self.request,
                    self.response.replace(body=b"second"),
                )
            assert self._temporary_entries(entry_dir) == []
            self.assertEqualResponse(
                self.response,
                storage.retrieve_response(crawler.spider, self.request.copy()),
            )

    def test_verification_size_and_record_checks_are_publish_errors(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            real_verify = storage._verify_generation

            def size_mismatch(generation_path, manifest, manifest_bytes):
                response_body = generation_path / "response_body"
                response_body.write_bytes(response_body.read_bytes() + b"extra")
                return real_verify(generation_path, manifest, manifest_bytes)

            with (
                mock.patch.object(
                    storage, "_verify_generation", side_effect=size_mismatch
                ),
                pytest.raises(GenerationPublishError, match="Verification"),
            ):
                storage.store_response(
                    crawler.spider,
                    self.request,
                    self.response.replace(body=b"second"),
                )

            def bad_record(generation_path, manifest, manifest_bytes):
                manifest["files"]["response_body"] = "not-a-record"
                return real_verify(generation_path, manifest, manifest_bytes)

            with (
                mock.patch.object(
                    storage, "_verify_generation", side_effect=bad_record
                ),
                pytest.raises(GenerationPublishError, match="Verification"),
            ):
                storage.store_response(
                    crawler.spider,
                    self.request,
                    self.response.replace(body=b"third"),
                )

    def test_remove_tree_unlinks_symlinks_without_following_them(self) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            entry_dir = Path(storage._get_request_path(crawler.spider, self.request))
            outside = entry_dir.parent / "outside"
            outside.mkdir()
            (outside / "keep-me").write_bytes(b"must survive")
            link = entry_dir / "dangling-link"
            link.symlink_to(outside / "keep-me")
            storage._remove_tree(link)
            assert not link.exists()
            assert (outside / "keep-me").read_bytes() == b"must survive"

    def test_previous_generation_cleanup_failure_is_logged(self, caplog) -> None:
        with self._storage() as (storage, crawler):
            storage.store_response(crawler.spider, self.request, self.response)
            real_remove = storage._remove_tree

            def fail_on_old_generation(path: Path) -> None:
                if path.name == "gen-0":
                    raise OSError("injected cleanup failure")
                real_remove(path)

            with mock.patch.object(
                storage,
                "_remove_tree",
                autospec=True,
                side_effect=fail_on_old_generation,
            ):
                storage.store_response(
                    crawler.spider,
                    self.request,
                    self.response.replace(body=b"second"),
                )
            assert "previous cache generation" in caplog.text
            cached = storage.retrieve_response(crawler.spider, self.request.copy())
            assert cached is not None
            assert cached.body == b"second"

    @coroutine_test
    async def test_304_revalidation_publishes_updated_generation(self) -> None:
        with self._middleware(HTTPCACHE_POLICY=AlwaysStalePolicy) as mw:
            spider = mw.crawler.spider
            assert spider is not None
            initial = Response(
                self.request.url,
                headers={
                    "Content-Type": "text/html",
                    "Last-Modified": self.yesterday,
                },
                body=b"original body",
            )
            mw.process_response(self.request, initial)
            entry_dir = Path(mw.storage._get_request_path(spider, self.request))

            assert await mw.process_request(self.request) is None
            not_modified = Response(
                self.request.url,
                status=304,
                headers={"X-Refreshed": "1", "Date": self.today},
            )
            mw.process_response(self.request, not_modified)

            pointer = mw.storage._read_pointer(entry_dir)
            assert pointer is not None
            assert pointer["generation"] == 1
            cached = mw.storage.retrieve_response(spider, self.request.copy())
            assert cached is not None
            assert cached.body == b"original body"
            assert cached.headers[b"X-Refreshed"] == b"1"
            assert self._temporary_entries(entry_dir) == []


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
