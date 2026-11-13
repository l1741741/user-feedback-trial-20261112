"""Tests for origin isolation, the shared download lifecycle and the bounded
robots.txt cache of :class:`RobotsTxtMiddleware`.

All downloads go through a controllable in-memory fake downloader fed with
locally generated robots.txt bodies: nothing here reaches the network.
"""

from __future__ import annotations

import asyncio
from itertools import count
from typing import Any
from unittest import mock

import pytest
from twisted.internet.defer import CancelledError, Deferred, DeferredList
from twisted.python import failure

from scrapy.downloadermiddlewares.robotstxt import (
    RobotsTxtMiddleware,
    _robots_origin,
    _robots_url,
)
from scrapy.exceptions import CannotResolveHostError, IgnoreRequest
from scrapy.http import Request, Response, TextResponse
from scrapy.http.request import NO_CALLBACK
from scrapy.settings import Settings
from scrapy.utils.defer import deferred_from_coro, maybe_deferred_to_future
from tests.utils.decorators import coroutine_test

ROBOTS_ALLOW_ADMIN = b"User-Agent: *\nDisallow: /admin/\n"
ROBOTS_ALLOW_ALL = b"User-Agent: *\nAllow: /\n"


class FakeDownloader:
    """A controllable stand-in for engine.download_async().

    Each robots.txt request gets a :class:`Deferred` held in *pending* until the
    test releases it, so concurrency and cancellation can be driven
    deterministically. Bodies and failures are chosen per URL.
    """

    def __init__(self, bodies: dict[str, bytes] | None = None) -> None:
        self._bodies = bodies or {}
        self.pending: dict[str, tuple[str, Deferred[Response]]] = {}
        self.calls: list[Request] = []
        self._counter = count()

    async def download(self, request: Request) -> Response:
        self.calls.append(request)
        d: Deferred[Response] = Deferred()
        # Each concurrent request gets its own pending entry keyed by an id,
        # remembering the URL its response must be built for.
        self.pending[f"req-{next(self._counter)}"] = (request.url, d)
        return await maybe_deferred_to_future(d)

    def release(
        self,
        url: str | None = None,
        *,
        body: bytes | None = None,
        status: int = 200,
    ) -> None:
        """Succeed every (or the matching) pending download."""
        for key in list(self.pending):
            req_url, d = self.pending[key]
            if url is not None and req_url != url:
                continue
            del self.pending[key]
            if not d.called:
                response_body = (
                    body if body is not None else self._bodies.get(req_url, b"")
                )
                d.callback(
                    TextResponse(req_url, body=response_body, status=status)
                )

    def fail(self, exc: Exception, url: str | None = None) -> None:
        for key in list(self.pending):
            req_url, d = self.pending[key]
            if url is not None and req_url != url:
                continue
            del self.pending[key]
            if not d.called:
                d.errback(failure.Failure(exc))

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def urls(self) -> list[str]:
        return [r.url for r in self.calls]


def _make_middleware(
    downloader: FakeDownloader,
    *,
    settings: dict[str, Any] | None = None,
) -> RobotsTxtMiddleware:
    crawler = mock.MagicMock()
    base = {"ROBOTSTXT_OBEY": True}
    if settings:
        base.update(settings)
    crawler.settings = Settings(base)
    crawler.engine.download_async = mock.AsyncMock(side_effect=downloader.download)
    crawler.signals.send_catch_log_async = mock.AsyncMock(return_value=[])
    return RobotsTxtMiddleware(crawler)


def _drain(loops: int = 6) -> Any:
    async def _sleep() -> None:
        for _ in range(loops):
            await asyncio.sleep(0)

    return deferred_from_coro(_sleep())


class TestOriginKeyAndUrl:
    def test_scheme_isolation(self) -> None:
        assert _robots_origin(Request("http://example.com/")) != _robots_origin(
            Request("https://example.com/")
        )

    def test_default_port_normalization_http(self) -> None:
        assert _robots_origin(Request("http://example.com/")) == _robots_origin(
            Request("http://example.com:80/")
        )

    def test_default_port_normalization_https(self) -> None:
        assert _robots_origin(Request("https://example.com/")) == _robots_origin(
            Request("https://example.com:443/")
        )

    def test_non_default_port_isolation(self) -> None:
        origins = {
            _robots_origin(Request("http://example.com/")),
            _robots_origin(Request("http://example.com:8080/")),
            _robots_origin(Request("https://example.com:8443/")),
            _robots_origin(Request("https://example.com/")),
        }
        assert len(origins) == 4

    def test_hostname_lowercased(self) -> None:
        assert _robots_origin(Request("http://Example.COM/")) == _robots_origin(
            Request("http://example.com/")
        )

    def test_scheme_lowercased(self) -> None:
        assert _robots_origin(Request("HTTP://example.com/")) == (
            "http",
            "example.com",
            80,
        )

    def test_ipv6_keeps_brackets_in_url(self) -> None:
        origin = _robots_origin(Request("http://[2001:db8::1]:8080/path"))
        assert origin == ("http", "2001:db8::1", 8080)
        assert _robots_url(origin) == "http://[2001:db8::1]:8080/robots.txt"

    def test_ipv6_default_port(self) -> None:
        origin = _robots_origin(Request("https://[::1]/"))
        assert origin == ("https", "::1", 443)
        assert _robots_url(origin) == "https://[::1]/robots.txt"

    def test_userinfo_is_not_part_of_host(self) -> None:
        origin = _robots_origin(
            Request("http://user:password@example.com:80/path?q=1#frag")
        )
        assert origin == ("http", "example.com", 80)
        assert _robots_url(origin) == "http://example.com/robots.txt"

    def test_ipv6_non_default_port_url(self) -> None:
        origin = _robots_origin(Request("http://[2001:db8::1]:8080/"))
        assert _robots_url(origin) == "http://[2001:db8::1]:8080/robots.txt"

    @coroutine_test
    async def test_ipv6_end_to_end_request_url(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        task = deferred_from_coro(
            mw.process_request(Request("http://[::1]:8080/page"))
        )
        await maybe_deferred_to_future(_drain())
        assert downloader.urls() == ["http://[::1]:8080/robots.txt"]
        downloader.release(
            "http://[::1]:8080/robots.txt", body=ROBOTS_ALLOW_ALL
        )
        await maybe_deferred_to_future(task)


class TestRobotsRequestSanitization:
    @coroutine_test
    async def test_no_userinfo_query_or_fragment(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        task = deferred_from_coro(
            mw.process_request(
                Request("http://user:pass@example.com/page?x=1#section")
            )
        )
        await maybe_deferred_to_future(_drain())
        assert downloader.urls() == ["http://example.com/robots.txt"]
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(task)

    @coroutine_test
    async def test_no_cookie_or_authorization_headers(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        page = Request(
            "http://example.com/page",
            headers={"Authorization": "Bearer secret", "Cookie": "a=b"},
            cookies={"session": "abc"},
        )
        task = deferred_from_coro(mw.process_request(page))
        await maybe_deferred_to_future(_drain())
        robotsreq = downloader.calls[0]
        assert b"Authorization" not in robotsreq.headers
        assert b"Cookie" not in robotsreq.headers
        assert not robotsreq.cookies
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(task)

    @coroutine_test
    async def test_keeps_priority_callback_and_meta(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        page = Request(
            "http://example.com/page", meta={"download_handler": "named"}
        )
        task = deferred_from_coro(mw.process_request(page))
        await maybe_deferred_to_future(_drain())
        robotsreq = downloader.calls[0]
        assert robotsreq.callback is NO_CALLBACK
        assert robotsreq.priority == RobotsTxtMiddleware.DOWNLOAD_PRIORITY
        assert robotsreq.meta["dont_obey_robotstxt"] is True
        assert robotsreq.meta["is_robotstxt_request"] is True
        assert robotsreq.meta["download_handler"] == "named"
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(task)


class TestOriginIsolationDownloads:
    @coroutine_test
    async def test_http_and_https_fetch_separately(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        t1 = deferred_from_coro(mw.process_request(Request("http://example.com/a")))
        t2 = deferred_from_coro(mw.process_request(Request("https://example.com/a")))
        await maybe_deferred_to_future(_drain())
        assert sorted(downloader.urls()) == [
            "http://example.com/robots.txt",
            "https://example.com/robots.txt",
        ]
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        downloader.release("https://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(DeferredList([t1, t2], fireOnOneErrback=True))

    @coroutine_test
    async def test_default_port_shares_one_download(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        t1 = deferred_from_coro(mw.process_request(Request("http://example.com/a")))
        t2 = deferred_from_coro(
            mw.process_request(Request("http://example.com:80/b"))
        )
        await maybe_deferred_to_future(_drain())
        assert downloader.call_count == 1
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(DeferredList([t1, t2], fireOnOneErrback=True))

    @coroutine_test
    async def test_different_ports_fetch_separately(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        t1 = deferred_from_coro(mw.process_request(Request("http://example.com/a")))
        t2 = deferred_from_coro(
            mw.process_request(Request("http://example.com:8080/a"))
        )
        await maybe_deferred_to_future(_drain())
        assert sorted(downloader.urls()) == [
            "http://example.com/robots.txt",
            "http://example.com:8080/robots.txt",
        ]
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        downloader.release("http://example.com:8080/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(DeferredList([t1, t2], fireOnOneErrback=True))

    @coroutine_test
    async def test_http_and_https_waiters_do_not_cross_wake(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        http_task = deferred_from_coro(
            mw.process_request(Request("http://example.com/a"))
        )
        https_task = deferred_from_coro(
            mw.process_request(Request("https://example.com/a"))
        )
        await maybe_deferred_to_future(_drain())
        downloader.release(
            "http://example.com/robots.txt", body=ROBOTS_ALLOW_ADMIN
        )
        await maybe_deferred_to_future(_drain())
        # The HTTPS waiter must still be blocked on its own download.
        assert not https_task.called
        downloader.release(
            "https://example.com/robots.txt", body=ROBOTS_ALLOW_ALL
        )
        await maybe_deferred_to_future(
            DeferredList([http_task, https_task], fireOnOneErrback=True)
        )


class TestConcurrentWaiters:
    @coroutine_test
    async def test_single_download_for_concurrent_same_origin(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        tasks = [
            deferred_from_coro(mw.process_request(Request(f"http://example.com/{i}")))
            for i in range(5)
        ]
        await maybe_deferred_to_future(_drain())
        assert downloader.call_count == 1
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(
            DeferredList(tasks, fireOnOneErrback=True)
        )

    @pytest.mark.only_asyncio
    @coroutine_test
    async def test_single_download_asyncio_gather(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)

        async def run() -> None:
            await asyncio.gather(
                *[
                    mw.process_request(Request(f"http://example.com/{i}"))
                    for i in range(5)
                ]
            )

        task = deferred_from_coro(run())
        await maybe_deferred_to_future(_drain())
        assert downloader.call_count == 1
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(task)

    @coroutine_test
    async def test_first_caller_cancel_keeps_shared_download(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        first = deferred_from_coro(
            mw.process_request(Request("http://example.com/allowed"))
        )
        second = deferred_from_coro(
            mw.process_request(Request("http://example.com/ok1"))
        )
        third = deferred_from_coro(
            mw.process_request(Request("http://example.com/ok2"))
        )
        await maybe_deferred_to_future(_drain())
        assert downloader.call_count == 1
        # The first caller is the one that triggered the download. Cancelling
        # it must not cancel the shared download nor the other waiters.
        first.cancel()
        with pytest.raises((CancelledError, asyncio.CancelledError)):
            await maybe_deferred_to_future(first)
        assert mw._inflight  # the shared fetch survives
        assert not second.called and not third.called
        downloader.release(
            "http://example.com/robots.txt", body=ROBOTS_ALLOW_ADMIN
        )
        results = await maybe_deferred_to_future(
            DeferredList([second, third])
        )
        assert all(success for success, _ in results)
        # Only one robots.txt was ever downloaded.
        assert downloader.call_count == 1

    @coroutine_test
    async def test_later_caller_cancel_does_not_affect_others(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        first = deferred_from_coro(
            mw.process_request(Request("http://example.com/allowed"))
        )
        second = deferred_from_coro(
            mw.process_request(Request("http://example.com/allowed2"))
        )
        await maybe_deferred_to_future(_drain())
        second.cancel()
        with pytest.raises((CancelledError, asyncio.CancelledError)):
            await maybe_deferred_to_future(second)
        assert mw._inflight
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(first)
        assert downloader.call_count == 1

    @coroutine_test
    async def test_driver_cancellation_releases_waiters_and_slot(self) -> None:
        # If the shared download itself is cancelled (e.g. the event loop is
        # shutting down), every waiter must be released fail-open and the
        # in-flight slot cleared, with nothing left pending.
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        waiter_task = deferred_from_coro(
            mw.process_request(Request("http://example.com/a"))
        )
        await maybe_deferred_to_future(_drain())
        origin = ("http", "example.com", 80)
        assert origin in mw._inflight
        # Cancel the underlying download directly, simulating shutdown. The
        # Twisted CancelledError is what the waiter Deferreds use.
        for key, (_, d) in list(downloader.pending.items()):
            del downloader.pending[key]
            d.errback(failure.Failure(CancelledError()))
        # The waiter resolves (fail open) instead of hanging forever.
        await maybe_deferred_to_future(waiter_task)
        assert origin not in mw._inflight
        # No background fetch remains.
        assert not mw._inflight

    @coroutine_test
    async def test_no_pending_cache_left_after_completion(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        task = deferred_from_coro(
            mw.process_request(Request("http://example.com/a"))
        )
        await maybe_deferred_to_future(_drain())
        assert ("http", "example.com", 80) in mw._inflight
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(task)
        assert not mw._inflight
        assert ("http", "example.com", 80) in mw._cache


class TestBoundedCache:
    @coroutine_test
    async def test_lru_eviction_only_completed(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(
            downloader, settings={"ROBOTSTXT_CACHE_MAXSIZE": 2}
        )

        async def warm(host: str) -> None:
            task = deferred_from_coro(
                mw.process_request(Request(f"http://{host}/a"))
            )
            await maybe_deferred_to_future(_drain())
            downloader.release(f"http://{host}/robots.txt", body=ROBOTS_ALLOW_ALL)
            await maybe_deferred_to_future(task)

        await warm("h1.local")
        await warm("h2.local")
        assert len(mw._cache) == 2
        # Touch h1 so h2 becomes the least recently used.
        await mw.process_request(Request("http://h1.local/b"))
        await warm("h3.local")
        assert len(mw._cache) == 2
        assert ("http", "h1.local", 80) in mw._cache
        assert ("http", "h3.local", 80) in mw._cache
        assert ("http", "h2.local", 80) not in mw._cache

    @coroutine_test
    async def test_eviction_does_not_touch_inflight(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(
            downloader, settings={"ROBOTSTXT_CACHE_MAXSIZE": 1}
        )
        inflight_task = deferred_from_coro(
            mw.process_request(Request("http://inflight.local/a"))
        )
        await maybe_deferred_to_future(_drain())
        inflight_origin = ("http", "inflight.local", 80)
        assert inflight_origin in mw._inflight

        async def fill_cache(host: str) -> None:
            task = deferred_from_coro(
                mw.process_request(Request(f"http://{host}/a"))
            )
            await maybe_deferred_to_future(_drain())
            downloader.release(f"http://{host}/robots.txt", body=ROBOTS_ALLOW_ALL)
            await maybe_deferred_to_future(task)

        await fill_cache("h1.local")
        await fill_cache("h2.local")
        # The in-flight fetch must still be there despite the tiny cache.
        assert inflight_origin in mw._inflight
        downloader.release(
            "http://inflight.local/robots.txt", body=ROBOTS_ALLOW_ALL
        )
        await maybe_deferred_to_future(inflight_task)

    @coroutine_test
    async def test_inflight_not_evicted_on_expiry(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader, settings={"ROBOTSTXT_CACHE_TTL": 10})
        current = [100.0]
        mw._monotonic = lambda: current[0]
        task = deferred_from_coro(
            mw.process_request(Request("http://example.com/a"))
        )
        await maybe_deferred_to_future(_drain())
        origin = ("http", "example.com", 80)
        # Jump past TTL while the download is still in flight.
        current[0] = 1000.0
        assert origin in mw._inflight
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(task)
        assert origin in mw._cache


class TestTTLEntries:
    @coroutine_test
    async def test_success_ttl_refresh(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader, settings={"ROBOTSTXT_CACHE_TTL": 10})
        current = [0.0]
        mw._monotonic = lambda: current[0]
        task = deferred_from_coro(
            mw.process_request(Request("http://example.com/a"))
        )
        await maybe_deferred_to_future(_drain())
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(task)
        assert downloader.call_count == 1
        # Within the TTL a second request is served from the cache.
        current[0] = 9.0
        await mw.process_request(Request("http://example.com/b"))
        assert downloader.call_count == 1
        # After expiry the robots.txt is requested again.
        current[0] = 11.0
        task2 = deferred_from_coro(
            mw.process_request(Request("http://example.com/c"))
        )
        await maybe_deferred_to_future(_drain())
        assert downloader.call_count == 2
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(task2)

    @coroutine_test
    async def test_failure_ttl_negative_cache_then_recover(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(
            downloader,
            settings={"ROBOTSTXT_CACHE_TTL": 100, "ROBOTSTXT_FAILURE_TTL": 5},
        )
        current = [0.0]
        mw._monotonic = lambda: current[0]
        err = CannotResolveHostError("no host")

        async def fail_open_once() -> None:
            task = deferred_from_coro(
                mw.process_request(Request("http://example.com/a"))
            )
            await maybe_deferred_to_future(_drain())
            downloader.fail(err, "http://example.com/robots.txt")
            # Fail open: the request is not ignored.
            await maybe_deferred_to_future(task)

        await fail_open_once()
        assert downloader.call_count == 1
        entry = mw._cache[("http", "example.com", 80)]
        assert entry.parser is None
        # Negatively cached: no new download within the failure TTL.
        current[0] = 4.0
        await mw.process_request(Request("http://example.com/b"))
        assert downloader.call_count == 1
        # After the failure TTL (but well within the success TTL) a retry happens.
        current[0] = 6.0
        recovery = deferred_from_coro(
            mw.process_request(Request("http://example.com/c"))
        )
        await maybe_deferred_to_future(_drain())
        assert downloader.call_count == 2
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(recovery)
        # Successful result replaces the negative cache entry.
        assert mw._cache[("http", "example.com", 80)].parser is not None

    @coroutine_test
    async def test_zero_success_ttl_disables_caching(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader, settings={"ROBOTSTXT_CACHE_TTL": 0})
        for _ in range(2):
            task = deferred_from_coro(
                mw.process_request(Request("http://example.com/a"))
            )
            await maybe_deferred_to_future(_drain())
            downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
            await maybe_deferred_to_future(task)
        assert downloader.call_count == 2
        assert not mw._cache

    @coroutine_test
    async def test_zero_failure_ttl_disables_negative_caching(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(
            downloader, settings={"ROBOTSTXT_FAILURE_TTL": 0}
        )
        err = CannotResolveHostError("no host")
        for _ in range(2):
            task = deferred_from_coro(
                mw.process_request(Request("http://example.com/a"))
            )
            await maybe_deferred_to_future(_drain())
            downloader.fail(err, "http://example.com/robots.txt")
            await maybe_deferred_to_future(task)
        assert downloader.call_count == 2
        assert not mw._cache


class TestInvalidSettings:
    def test_invalid_maxsize_falls_back_to_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        crawler = mock.MagicMock()
        crawler.settings = Settings(
            {
                "ROBOTSTXT_OBEY": True,
                "ROBOTSTXT_CACHE_MAXSIZE": 0,
            }
        )
        crawler.engine.download_async = mock.AsyncMock()
        crawler.signals.send_catch_log_async = mock.AsyncMock(return_value=[])
        mw = RobotsTxtMiddleware(crawler)
        assert mw._cache_maxsize == 256
        assert "ROBOTSTXT_CACHE_MAXSIZE" in caplog.text

    def test_negative_maxsize_falls_back_to_default(self) -> None:
        crawler = mock.MagicMock()
        crawler.settings = Settings(
            {"ROBOTSTXT_OBEY": True, "ROBOTSTXT_CACHE_MAXSIZE": -3}
        )
        crawler.engine.download_async = mock.AsyncMock()
        crawler.signals.send_catch_log_async = mock.AsyncMock(return_value=[])
        mw = RobotsTxtMiddleware(crawler)
        assert mw._cache_maxsize == 256

    def test_negative_ttl_falls_back_to_default(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        crawler = mock.MagicMock()
        crawler.settings = Settings(
            {"ROBOTSTXT_OBEY": True, "ROBOTSTXT_CACHE_TTL": -1}
        )
        crawler.engine.download_async = mock.AsyncMock()
        crawler.signals.send_catch_log_async = mock.AsyncMock(return_value=[])
        mw = RobotsTxtMiddleware(crawler)
        assert mw._cache_ttl == 3600.0
        assert "ROBOTSTXT_CACHE_TTL" in caplog.text

    def test_negative_failure_ttl_falls_back_to_default(self) -> None:
        crawler = mock.MagicMock()
        crawler.settings = Settings(
            {"ROBOTSTXT_OBEY": True, "ROBOTSTXT_FAILURE_TTL": -1}
        )
        crawler.engine.download_async = mock.AsyncMock()
        crawler.signals.send_catch_log_async = mock.AsyncMock(return_value=[])
        mw = RobotsTxtMiddleware(crawler)
        assert mw._failure_ttl == 30.0


class TestStatsCountDownloads:
    @coroutine_test
    async def test_stats_count_one_download_many_waiters(self) -> None:
        downloader = FakeDownloader()
        crawler = mock.MagicMock()
        crawler.settings = Settings({"ROBOTSTXT_OBEY": True})
        crawler.engine.download_async = mock.AsyncMock(side_effect=downloader.download)
        crawler.signals.send_catch_log_async = mock.AsyncMock(return_value=[])
        mw = RobotsTxtMiddleware(crawler)
        tasks = [
            deferred_from_coro(mw.process_request(Request(f"http://example.com/{i}")))
            for i in range(4)
        ]
        await maybe_deferred_to_future(_drain())
        downloader.release(
            "http://example.com/robots.txt", body=ROBOTS_ALLOW_ADMIN
        )
        await maybe_deferred_to_future(DeferredList(tasks))
        incs = [c.args[0] for c in crawler.stats.inc_value.call_args_list]
        assert incs.count("robotstxt/request_count") == 1
        assert incs.count("robotstxt/response_count") == 1
        assert incs.count("robotstxt/response_status_count/200") == 1

    @coroutine_test
    async def test_failure_stats_then_retry_after_failure_ttl(self) -> None:
        downloader = FakeDownloader()
        crawler = mock.MagicMock()
        crawler.settings = Settings(
            {
                "ROBOTSTXT_OBEY": True,
                "ROBOTSTXT_FAILURE_TTL": 5,
            }
        )
        crawler.engine.download_async = mock.AsyncMock(side_effect=downloader.download)
        crawler.signals.send_catch_log_async = mock.AsyncMock(return_value=[])
        mw = RobotsTxtMiddleware(crawler)
        current = [0.0]
        mw._monotonic = lambda: current[0]
        err = CannotResolveHostError("boom")

        async def attempt() -> Deferred[Any]:
            task = deferred_from_coro(
                mw.process_request(Request("http://example.com/a"))
            )
            await maybe_deferred_to_future(_drain())
            return task

        t1 = await attempt()
        downloader.fail(err, "http://example.com/robots.txt")
        await maybe_deferred_to_future(t1)
        current[0] = 6.0
        t2 = await attempt()
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(t2)
        incs = [c.args[0] for c in crawler.stats.inc_value.call_args_list]
        assert incs.count("robotstxt/request_count") == 2
        assert incs.count("robotstxt/response_count") == 1
        assert incs.count(f"robotstxt/exception_count/{CannotResolveHostError}") == 1


class TestExplicitRobotsRequestCooperation:
    @coroutine_test
    async def test_process_response_populates_cache(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        robots_req = Request("http://example.com/robots.txt")
        await mw.process_request(robots_req)
        origin = ("http", "example.com", 80)
        assert origin in mw._inflight
        response = TextResponse(
            "http://example.com/robots.txt", body=ROBOTS_ALLOW_ADMIN
        )
        await mw.process_response(robots_req, response)
        assert origin not in mw._inflight
        assert origin in mw._cache
        # A following page request needs no new download and honors the rules.
        with pytest.raises(IgnoreRequest):
            await mw.process_request(Request("http://example.com/admin/x"))
        assert downloader.call_count == 0

    @coroutine_test
    async def test_process_exception_fail_opens_and_clears_inflight(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        robots_req = Request("http://example.com/robots.txt")
        await mw.process_request(robots_req)
        origin = ("http", "example.com", 80)
        assert origin in mw._inflight
        err = CannotResolveHostError("boom")
        await mw.process_exception(robots_req, err)
        assert origin not in mw._inflight
        # Fail open: page requests are allowed.
        await mw.process_request(Request("http://example.com/anything"))

    @coroutine_test
    async def test_explicit_request_shares_inflight_with_page_request(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        # The page request starts the internal shared download.
        page_task = deferred_from_coro(
            mw.process_request(Request("http://example.com/page"))
        )
        await maybe_deferred_to_future(_drain())
        # An explicit robots.txt request joins the same in-flight fetch instead
        # of creating a second slot or a second download.
        robots_task = deferred_from_coro(
            mw.process_request(Request("http://example.com/robots.txt"))
        )
        await maybe_deferred_to_future(_drain())
        assert len(mw._inflight) == 1
        assert downloader.call_count == 1
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(
            DeferredList([page_task, robots_task], fireOnOneErrback=True)
        )
        assert downloader.call_count == 1

    @coroutine_test
    async def test_internal_fetch_ignored_by_process_response(self) -> None:
        # A response delivered through process_response() for an internal fetch
        # must not be parsed a second time: the internal driver owns it.
        downloader = FakeDownloader()
        crawler = mock.MagicMock()
        crawler.settings = Settings({"ROBOTSTXT_OBEY": True})
        crawler.engine.download_async = mock.AsyncMock(side_effect=downloader.download)
        crawler.signals.send_catch_log_async = mock.AsyncMock(return_value=[])
        mw = RobotsTxtMiddleware(crawler)
        page_task = deferred_from_coro(
            mw.process_request(Request("http://example.com/page"))
        )
        await maybe_deferred_to_future(_drain())
        fetch = next(iter(mw._inflight.values()))
        assert fetch.external is False
        response = TextResponse(
            "http://example.com/robots.txt", body=ROBOTS_ALLOW_ADMIN
        )
        # process_response must not touch the internal fetch.
        await mw.process_response(
            Request("http://example.com/robots.txt"), response
        )
        assert ("http", "example.com", 80) in mw._inflight
        downloader.release("http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL)
        await maybe_deferred_to_future(page_task)
        # Only one parse happened (one response stat, one signal).
        incs = [c.args[0] for c in crawler.stats.inc_value.call_args_list]
        assert incs.count("robotstxt/response_count") == 1
        signals = [
            kw
            for _, kw in crawler.signals.send_catch_log_async.call_args_list
            if kw.get("signal") is not None
        ]
        assert len(signals) == 1

    @coroutine_test
    async def test_explicit_request_joining_internal_fetch_not_double_handled(
        self,
    ) -> None:
        # An explicit robots.txt request that arrives while an internal fetch
        # is in flight waits on the shared result without starting a second
        # download, while ordinary page requests still get their rules applied.
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        page_task = deferred_from_coro(
            mw.process_request(Request("http://example.com/page"))
        )
        await maybe_deferred_to_future(_drain())
        robots_task = deferred_from_coro(
            mw.process_request(Request("http://example.com/robots.txt"))
        )
        await maybe_deferred_to_future(_drain())
        assert len(mw._inflight) == 1
        downloader.release(
            "http://example.com/robots.txt", body=ROBOTS_ALLOW_ADMIN
        )
        # Both waiting requests complete without an unhandled error.
        await maybe_deferred_to_future(
            DeferredList([page_task, robots_task], fireOnOneErrback=True)
        )
        assert downloader.call_count == 1
        # Rules are still enforced for a fresh, disallowed page request.
        with pytest.raises(IgnoreRequest):
            await mw.process_request(Request("http://example.com/admin/x"))


class TestStaleCallbackProtection:
    @coroutine_test
    async def test_old_fetch_cannot_overwrite_newer_generation(self) -> None:
        downloader = FakeDownloader()
        mw = _make_middleware(downloader)
        origin = ("http", "example.com", 80)
        # First fetch, whose download stays pending.
        first = deferred_from_coro(
            mw.process_request(Request("http://example.com/a"))
        )
        await maybe_deferred_to_future(_drain())
        old_fetch = mw._inflight[origin]
        # A newer generation takes over the slot before the old download
        # finishes.
        new_fetch = type(old_fetch)(origin, old_fetch.robots_url)
        mw._inflight[origin] = new_fetch
        try:
            # Completing the old fetch must be a no-op: the slot, the new
            # fetch and the cache are all left untouched, and the old waiters
            # are not fired a second time later.
            mw._finish_fetch(old_fetch, None, cache=True)
            assert not mw._cache
            assert mw._inflight[origin] is new_fetch
        finally:
            # Release the real pending download and let the (now stale) driver
            # finish so no background task is left behind.
            downloader.release(
                "http://example.com/robots.txt", body=ROBOTS_ALLOW_ALL
            )
            # The first caller's waiter was orphaned by the slot replacement;
            # resolve it so its coroutine can complete.
            old_fetch.fire(None)
            await maybe_deferred_to_future(first)
            await maybe_deferred_to_future(_drain())
        assert not downloader.pending
        # The stale driver never overwrote the current generation.
        assert mw._inflight.get(origin) is new_fetch
