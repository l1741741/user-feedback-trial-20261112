from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from email.utils import formatdate
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

import pytest
from twisted.internet.error import ConnectError, ConnectionDone, ConnectionLost

from scrapy.core.scheduler import Scheduler
from scrapy.downloadermiddlewares.retry import RetryMiddleware, get_retry_request
from scrapy.exceptions import (
    CannotResolveHostError,
    DownloadCancelledError,
    DownloadConnectionRefusedError,
    DownloadTimeoutError,
    IgnoreRequest,
    NotConfigured,
    ScrapyDeprecationWarning,
)
from scrapy.http import Request, Response
from scrapy.settings.default_settings import RETRY_EXCEPTIONS
from scrapy.spiders import Spider
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from tests.mockserver.http import MockServer


class _FakeTimer:
    """A controllable replacement for scrapy.utils.asyncio.CallLaterResult."""

    def __init__(
        self,
        scheduler: _FakeScheduler,
        delay: float,
        func: Callable[[], Any],
    ):
        self._scheduler = scheduler
        self.delay = delay
        self._func = func
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        self._scheduler._discard(self)

    def _run(self) -> None:
        self._func()


class _FakeScheduler:
    """Records scheduled timers instead of touching an event loop."""

    def __init__(self) -> None:
        self.timers: list[_FakeTimer] = []

    def __call__(self, delay: float, func: Callable[[], Any]) -> _FakeTimer:
        timer = _FakeTimer(self, delay, func)
        self.timers.append(timer)
        return timer

    def _discard(self, timer: _FakeTimer) -> None:
        if timer in self.timers:
            self.timers.remove(timer)

    def fire(self) -> None:
        timers = list(self.timers)
        self.timers.clear()
        for timer in timers:
            timer._run()


class _ControllableClock:
    """Wall clock whose value the tests set explicitly."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestRetry:
    def setup_method(self) -> None:
        self.crawler = get_crawler(DefaultSpider)
        self.crawler.spider = self.crawler._create_spider()
        self.mw: RetryMiddleware = build_from_crawler(RetryMiddleware, self.crawler)
        self.mw.max_retry_times = 2

    def test_priority_adjust(self):
        req = Request("http://www.scrapytest.org/503")
        rsp = Response("http://www.scrapytest.org/503", body=b"", status=503)
        req2 = self.mw.process_response(req, rsp)
        assert isinstance(req2, Request)
        assert req2.priority < req.priority

    def test_404(self):
        req = Request("http://www.scrapytest.org/404")
        rsp = Response("http://www.scrapytest.org/404", body=b"", status=404)

        # dont retry 404s
        assert self.mw.process_response(req, rsp) is rsp

    def test_dont_retry(self):
        req = Request("http://www.scrapytest.org/503", meta={"dont_retry": True})
        rsp = Response("http://www.scrapytest.org/503", body=b"", status=503)

        # no retry
        r = self.mw.process_response(req, rsp)
        assert r is rsp

        # Test retry when dont_retry set to False
        req = Request("http://www.scrapytest.org/503", meta={"dont_retry": False})
        rsp = Response("http://www.scrapytest.org/503", body=b"", status=503)

        # first retry
        req2 = self.mw.process_response(req, rsp)
        assert isinstance(req2, Request)
        assert req2.meta["retry_times"] == 1

    def test_dont_retry_exc(self):
        req = Request("http://www.scrapytest.org/503", meta={"dont_retry": True})

        r = self.mw.process_exception(req, CannotResolveHostError())
        assert r is None

    def test_503(self):
        req = Request("http://www.scrapytest.org/503")
        rsp = Response("http://www.scrapytest.org/503", body=b"", status=503)

        # first retry
        req2 = self.mw.process_response(req, rsp)
        assert isinstance(req2, Request)
        assert req2.meta["retry_times"] == 1

        # second retry
        req3 = self.mw.process_response(req2, rsp)
        assert isinstance(req3, Request)
        assert req3.meta["retry_times"] == 2

        # discard it
        assert self.mw.process_response(req3, rsp) is rsp

        assert self.crawler.stats.get_value("retry/max_reached") == 1
        assert (
            self.crawler.stats.get_value("retry/reason_count/503 Service Unavailable")
            == 2
        )
        assert self.crawler.stats.get_value("retry/count") == 2

    def test_give_up_log_level_setting(self, caplog: pytest.LogCaptureFixture) -> None:
        crawler = get_crawler(
            DefaultSpider, settings_dict={"RETRY_GIVE_UP_LOG_LEVEL": "WARNING"}
        )
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(RetryMiddleware, crawler)
        mw.max_retry_times = 0
        req = Request("http://example.com/503")
        rsp = Response("http://example.com/503", body=b"", status=503)
        with caplog.at_level(logging.WARNING):
            assert mw.process_response(req, rsp) is rsp
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.WARNING,
            f"Gave up retrying {req} (failed 1 times): 503 Service Unavailable",
        ) in caplog.record_tuples

    def test_give_up_log_level_meta(self, caplog: pytest.LogCaptureFixture) -> None:
        self.mw.max_retry_times = 0
        req = Request("http://example.com/503", meta={"give_up_log_level": "WARNING"})
        rsp = Response("http://example.com/503", body=b"", status=503)
        with caplog.at_level(logging.WARNING):
            assert self.mw.process_response(req, rsp) is rsp
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.WARNING,
            f"Gave up retrying {req} (failed 1 times): 503 Service Unavailable",
        ) in caplog.record_tuples

    def test_twistederrors(self):
        exceptions = [
            ConnectError,
            ConnectionDone,
            ConnectionLost,
            DownloadTimeoutError,
            DownloadConnectionRefusedError,
            CannotResolveHostError,
        ]

        for exc in exceptions:
            req = Request(f"http://www.scrapytest.org/{exc.__name__}")
            self._test_retry_exception(req, exc("foo"))

        stats = self.crawler.stats
        assert stats.get_value("retry/max_reached") == len(exceptions)
        assert stats.get_value("retry/count") == len(exceptions) * 2
        assert (
            stats.get_value("retry/reason_count/scrapy.exceptions.DownloadTimeoutError")
            == 2
        )

    def test_exception_to_retry_added(self):
        exc = ValueError
        settings_dict = {
            "RETRY_EXCEPTIONS": [*RETRY_EXCEPTIONS, exc],
        }
        crawler = get_crawler(DefaultSpider, settings_dict=settings_dict)
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(RetryMiddleware, crawler)
        req = Request(f"http://www.scrapytest.org/{exc.__name__}")
        self._test_retry_exception(req, exc("foo"), mw)

    def _test_retry_exception(
        self, req: Request, exception: Exception, mw: RetryMiddleware | None = None
    ) -> None:
        if mw is None:
            mw = self.mw

        # first retry
        req2 = mw.process_exception(req, exception)
        assert isinstance(req2, Request)
        assert req2.meta["retry_times"] == 1

        # second retry
        req3 = mw.process_exception(req2, exception)
        assert isinstance(req3, Request)
        assert req3.meta["retry_times"] == 2

        # discard it
        assert mw.process_exception(req3, exception) is None


class TestMaxRetryTimes:
    invalid_url = "http://www.scrapytest.org/invalid_url"

    def get_middleware(self, settings: dict[str, Any] | None = None) -> RetryMiddleware:
        crawler = get_crawler(DefaultSpider, settings or {})
        crawler.spider = crawler._create_spider()
        return build_from_crawler(RetryMiddleware, crawler)

    def test_with_settings_zero(self):
        max_retry_times = 0
        settings = {"RETRY_TIMES": max_retry_times}
        middleware = self.get_middleware(settings)
        req = Request(self.invalid_url)
        self._test_retry(
            req,
            CannotResolveHostError("foo"),
            max_retry_times,
            middleware=middleware,
        )

    def test_with_metakey_zero(self):
        max_retry_times = 0
        middleware = self.get_middleware()
        meta = {"max_retry_times": max_retry_times}
        req = Request(self.invalid_url, meta=meta)
        self._test_retry(
            req,
            CannotResolveHostError("foo"),
            max_retry_times,
            middleware=middleware,
        )

    def test_without_metakey(self):
        max_retry_times = 5
        settings = {"RETRY_TIMES": max_retry_times}
        middleware = self.get_middleware(settings)
        req = Request(self.invalid_url)
        self._test_retry(
            req,
            CannotResolveHostError("foo"),
            max_retry_times,
            middleware=middleware,
        )

    def test_with_metakey_greater(self):
        meta_max_retry_times = 3
        middleware_max_retry_times = 2

        req1 = Request(self.invalid_url, meta={"max_retry_times": meta_max_retry_times})
        req2 = Request(self.invalid_url)

        settings = {"RETRY_TIMES": middleware_max_retry_times}
        middleware = self.get_middleware(settings)

        self._test_retry(
            req1,
            CannotResolveHostError("foo"),
            meta_max_retry_times,
            middleware=middleware,
        )
        self._test_retry(
            req2,
            CannotResolveHostError("foo"),
            middleware_max_retry_times,
            middleware=middleware,
        )

    def test_with_metakey_lesser(self):
        meta_max_retry_times = 4
        middleware_max_retry_times = 5

        req1 = Request(self.invalid_url, meta={"max_retry_times": meta_max_retry_times})
        req2 = Request(self.invalid_url)

        settings = {"RETRY_TIMES": middleware_max_retry_times}
        middleware = self.get_middleware(settings)

        self._test_retry(
            req1,
            CannotResolveHostError("foo"),
            meta_max_retry_times,
            middleware=middleware,
        )
        self._test_retry(
            req2,
            CannotResolveHostError("foo"),
            middleware_max_retry_times,
            middleware=middleware,
        )

    def test_with_dont_retry(self):
        max_retry_times = 4
        middleware = self.get_middleware()
        meta = {
            "max_retry_times": max_retry_times,
            "dont_retry": True,
        }
        req = Request(self.invalid_url, meta=meta)
        self._test_retry(
            req,
            CannotResolveHostError("foo"),
            0,
            middleware=middleware,
        )

    def _test_retry(
        self,
        req: Request,
        exception: Exception,
        max_retry_times: int,
        middleware: RetryMiddleware,
    ) -> None:
        for _ in range(max_retry_times):
            result = middleware.process_exception(req, exception)
            assert isinstance(result, Request)
            req = result

        # discard it
        assert middleware.process_exception(req, exception) is None


class TestRetryAfterSettings:
    def _build(
        self,
        settings: dict[str, Any] | None = None,
        *,
        wall_clock: Callable[[], float] | None = None,
        scheduler: _FakeScheduler | None = None,
    ) -> tuple[Any, RetryMiddleware]:
        crawler = get_crawler(DefaultSpider, settings)
        crawler.spider = crawler._create_spider()
        kwargs: dict[str, Any] = {}
        if wall_clock is not None:
            kwargs["wall_clock"] = wall_clock
        if scheduler is not None:
            kwargs["call_later_function"] = scheduler
        middleware = RetryMiddleware(crawler.settings, **kwargs)
        middleware.crawler = crawler
        return crawler, middleware

    def test_disabled_by_default(self):
        crawler, middleware = self._build()
        assert middleware.retry_after_enabled is False
        assert middleware.retry_after_max_delay == 60

    def test_disabled_ignores_header(self):
        crawler, middleware = self._build(
            {"RETRY_AFTER_ENABLED": False}, scheduler=_FakeScheduler()
        )
        req = Request("http://example.com/429")
        rsp = Response(
            "http://example.com/429",
            body=b"",
            status=429,
            headers={"Retry-After": "30"},
        )
        result = middleware.process_response(req, rsp)
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert middleware._pending_delays == set()
        assert crawler.stats.get_value("retry/retry_after/delayed") is None

    @pytest.mark.parametrize("bad_delay", [0, -1, -0.1, float("inf"), float("nan")])
    def test_invalid_max_delay_raises(self, bad_delay: float):
        with pytest.raises(ValueError, match="RETRY_AFTER_MAX_DELAY"):
            self._build(
                {"RETRY_AFTER_ENABLED": True, "RETRY_AFTER_MAX_DELAY": bad_delay}
            )

    def test_invalid_max_delay_ignored_when_disabled(self):
        # The component must stay configurable when the feature is off.
        _, middleware = self._build(
            {"RETRY_AFTER_ENABLED": False, "RETRY_AFTER_MAX_DELAY": 0}
        )
        assert middleware.retry_after_enabled is False

    def test_retry_disabled_still_not_configured(self):
        with pytest.raises(NotConfigured):
            self._build(
                {
                    "RETRY_ENABLED": False,
                    "RETRY_AFTER_ENABLED": True,
                }
            )


class TestRetryAfter:
    url = "http://example.com/limited"

    def _build(
        self,
        settings: dict[str, Any] | None = None,
        *,
        wall_clock: Callable[[], float] | None = None,
        scheduler: _FakeScheduler | None = None,
    ) -> tuple[Any, RetryMiddleware]:
        full_settings = {"RETRY_AFTER_ENABLED": True, "RETRY_AFTER_MAX_DELAY": 10}
        if settings:
            full_settings.update(settings)
        crawler = get_crawler(DefaultSpider, full_settings)
        crawler.spider = crawler._create_spider()
        kwargs: dict[str, Any] = {}
        if wall_clock is not None:
            kwargs["wall_clock"] = wall_clock
        if scheduler is not None:
            kwargs["call_later_function"] = scheduler
        middleware = RetryMiddleware(crawler.settings, **kwargs)
        middleware.crawler = crawler
        return crawler, middleware

    @staticmethod
    def _response(
        status: int = 429, retry_after: str | bytes | None = None
    ) -> Response:
        headers = {}
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        return Response(
            f"http://example.com/{status}", body=b"", status=status, headers=headers
        )

    @staticmethod
    async def _start(result: Any) -> None:
        """Run the middleware coroutine until it suspends on its timer."""
        await asyncio.sleep(0)

    @coroutine_test
    async def test_integer_seconds_schedules_timer(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url)
        task = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(req, self._response(429, "5")),
            )
        )
        await self._start(task)
        assert len(scheduler.timers) == 1
        assert scheduler.timers[0].delay == 5.0
        assert len(middleware._pending_delays) == 1
        # Nothing has been retried yet while the wait is pending.
        assert crawler.stats.get_value("retry/count") is None
        scheduler.fire()
        retry_request = await task
        assert isinstance(retry_request, Request)
        assert middleware._pending_delays == set()

    @coroutine_test
    async def test_integer_seconds_waits_then_retries(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url)
        task = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(req, self._response(503, "3")),
            )
        )
        await self._start(task)
        assert scheduler.timers[0].delay == 3.0

        asyncio.get_running_loop().call_soon(scheduler.fire)
        retry_request = await task
        assert isinstance(retry_request, Request)
        assert retry_request.meta["retry_times"] == 1
        assert retry_request.priority == req.priority - 1
        assert middleware._pending_delays == set()
        assert crawler.stats.get_value("retry/retry_after/delayed") == 1
        assert crawler.stats.get_value("retry/count") == 1

    @coroutine_test
    async def test_http_date_uses_wall_clock(self):
        clock = _ControllableClock()
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(wall_clock=clock, scheduler=scheduler)
        future = datetime.fromtimestamp(clock.now + 7, tz=UTC)
        req = Request(self.url)
        task = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(
                    req,
                    self._response(
                        503, formatdate(future.timestamp(), usegmt=True)
                    ),
                ),
            )
        )
        await self._start(task)
        assert scheduler.timers[0].delay == pytest.approx(7.0)

        scheduler.fire()
        retry_request = await task
        assert isinstance(retry_request, Request)
        assert retry_request.meta["retry_times"] == 1
        assert crawler.stats.get_value("retry/retry_after/delayed") == 1

    @coroutine_test
    async def test_truncated_to_max_delay(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(
            {"RETRY_AFTER_MAX_DELAY": 10}, scheduler=scheduler
        )
        req = Request(self.url)
        task = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(req, self._response(429, "600")),
            )
        )
        await self._start(task)
        assert scheduler.timers[0].delay == 10.0
        assert crawler.stats.get_value("retry/retry_after/truncated") == 1

        scheduler.fire()
        retry_request = await task
        assert isinstance(retry_request, Request)
        assert retry_request.meta["retry_times"] == 1
        assert crawler.stats.get_value("retry/retry_after/delayed") == 1

    @coroutine_test
    async def test_http_date_far_ahead_truncated(self):
        clock = _ControllableClock()
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(
            {"RETRY_AFTER_MAX_DELAY": 4.5},
            wall_clock=clock,
            scheduler=scheduler,
        )
        future = datetime.fromtimestamp(clock.now + 100, tz=UTC)
        req = Request(self.url)
        task = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(
                    req,
                    self._response(
                        503, formatdate(future.timestamp(), usegmt=True)
                    ),
                ),
            )
        )
        await self._start(task)
        assert scheduler.timers[0].delay == 4.5
        assert crawler.stats.get_value("retry/retry_after/truncated") == 1
        scheduler.fire()
        assert isinstance(await task, Request)

    @pytest.mark.parametrize(
        "header_value",
        [
            "soon",
            "12.5",
            "Wed, 32 Jan 2022 00:00:00 GMT",
            "Wed, 21 Oct 99999999999 07:28:00 GMT",
            # A parseable date without a time zone is not a valid HTTP date.
            "Wed, 21 Oct 2015 07:28:00",
            "   ",
            "x" * 257,
        ],
    )
    def test_malformed_header_retries_immediately(self, header_value: str):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url)
        result = middleware.process_response(req, self._response(429, header_value))
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert scheduler.timers == []
        assert crawler.stats.get_value("retry/retry_after/invalid") == 1
        assert crawler.stats.get_value("retry/retry_after/delayed") is None

    def test_missing_header_retries_immediately(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url)
        result = middleware.process_response(req, self._response(429))
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert scheduler.timers == []
        assert crawler.stats.get_value("retry/retry_after/invalid") is None

    def test_negative_seconds_not_waited(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url)
        result = middleware.process_response(req, self._response(503, "-10"))
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert scheduler.timers == []
        assert crawler.stats.get_value("retry/retry_after/ignored") == 1

    def test_expired_http_date_not_waited(self):
        clock = _ControllableClock()
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(wall_clock=clock, scheduler=scheduler)
        past = datetime.fromtimestamp(clock.now - 30, tz=UTC)
        req = Request(self.url)
        result = middleware.process_response(
            req, self._response(503, formatdate(past.timestamp(), usegmt=True))
        )
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert scheduler.timers == []
        assert crawler.stats.get_value("retry/retry_after/ignored") == 1

    def test_header_ignored_for_other_retry_statuses(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url)
        result = middleware.process_response(req, self._response(500, "30"))
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert scheduler.timers == []
        assert crawler.stats.get_value("retry/retry_after/delayed") is None

    def test_header_ignored_for_dont_retry(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url, meta={"dont_retry": True})
        rsp = self._response(429, "30")
        assert middleware.process_response(req, rsp) is rsp
        assert scheduler.timers == []
        assert middleware._pending_delays == set()

    @coroutine_test
    async def test_zero_seconds_waits_with_timer(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url)
        task = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(req, self._response(429, "0")),
            )
        )
        await self._start(task)
        assert len(scheduler.timers) == 1
        assert scheduler.timers[0].delay == 0.0
        scheduler.fire()
        retry_request = await task
        assert isinstance(retry_request, Request)
        assert retry_request.meta["retry_times"] == 1
        assert crawler.stats.get_value("retry/retry_after/delayed") == 1

    @coroutine_test
    async def test_spider_closed_cancels_pending_wait(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url)
        task = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(req, self._response(429, "60")),
            )
        )
        await self._start(task)
        timer = scheduler.timers[0]

        middleware.spider_closed(crawler.spider)
        assert timer.cancelled is True
        assert middleware._pending_delays == set()

        with pytest.raises(DownloadCancelledError):
            await task
        # The cancelled wait must not count as a sent retry.
        assert crawler.stats.get_value("retry/count") is None
        assert crawler.stats.get_value("retry/retry_after/delayed") is None

    @coroutine_test
    async def test_late_timer_after_close_does_not_retry(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url)
        task = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(req, self._response(429, "60")),
            )
        )
        await self._start(task)
        pending = next(iter(middleware._pending_delays))

        middleware.spider_closed(crawler.spider)
        # Simulate a timer that the loop still runs despite cancellation.
        pending._fire()
        with pytest.raises(DownloadCancelledError):
            await task
        assert crawler.stats.get_value("retry/count") is None

    @coroutine_test
    async def test_double_settlement_does_not_double_fire(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url)
        task = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(req, self._response(429, "60")),
            )
        )
        await self._start(task)
        pending = next(iter(middleware._pending_delays))
        pending._fire()
        # A second firing, e.g. from a timer that was not removed in time,
        # must not raise AlreadyCalledError.
        pending._fire()
        retry_request = await task
        assert isinstance(retry_request, Request)
        assert retry_request.meta["retry_times"] == 1

    @coroutine_test
    async def test_cancel_twice_is_safe(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url)
        task = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(req, self._response(429, "60")),
            )
        )
        await self._start(task)
        pending = next(iter(middleware._pending_delays))
        pending.cancel()
        pending.cancel()
        with pytest.raises(DownloadCancelledError):
            await task

    @coroutine_test
    async def test_close_without_pending_waits_is_safe(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        middleware.spider_closed(crawler.spider)
        assert middleware._pending_delays == set()

    @coroutine_test
    async def test_retry_budget_unchanged_after_wait(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(
            {"RETRY_TIMES": 1}, scheduler=scheduler
        )
        req = Request(self.url)
        task = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(req, self._response(429, "1")),
            )
        )
        await self._start(task)
        scheduler.fire()
        retry_request = await task
        assert isinstance(retry_request, Request)
        assert retry_request.meta["retry_times"] == 1

        # The budget comes from the regular retry machinery: one more failure
        # gives up instead of scheduling another wait.
        exhausted = middleware.process_response(
            retry_request, self._response(429, "1")
        )
        assert isinstance(exhausted, Response)
        assert crawler.stats.get_value("retry/max_reached") == 1
        assert scheduler.timers == []

    def test_exhausted_budget_with_header_does_not_wait(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url, meta={"max_retry_times": 0})
        result = middleware.process_response(req, self._response(429, "5"))
        assert isinstance(result, Response)
        assert scheduler.timers == []
        assert middleware._pending_delays == set()
        assert crawler.stats.get_value("retry/max_reached") == 1

    @coroutine_test
    async def test_exception_retry_unchanged(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req = Request(self.url)
        result = middleware.process_exception(req, CannotResolveHostError())
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert scheduler.timers == []
        assert middleware._pending_delays == set()

    @coroutine_test
    async def test_two_pending_waits_are_independent(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        req1 = Request(f"{self.url}/1")
        req2 = Request(f"{self.url}/2")
        task1 = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(req1, self._response(429, "10")),
            )
        )
        await self._start(task1)
        task2 = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(req2, self._response(429, "5")),
            )
        )
        await self._start(task2)
        assert len(middleware._pending_delays) == 2
        assert {timer.delay for timer in scheduler.timers} == {10.0, 5.0}

        middleware.spider_closed(crawler.spider)
        for task in (task1, task2):
            with pytest.raises(DownloadCancelledError):
                await task
        assert crawler.stats.get_value("retry/count") is None

    @coroutine_test
    async def test_wait_does_not_block_other_requests(self):
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(scheduler=scheduler)
        blocked = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(
                    Request(self.url), self._response(429, "60")
                ),
            )
        )
        await self._start(blocked)
        assert len(middleware._pending_delays) == 1

        # Another request goes through the whole retry machinery while the
        # first one is still waiting.
        other = middleware.process_response(
            Request("http://example.com/other"), self._response(429)
        )
        assert isinstance(other, Request)
        assert other.meta["retry_times"] == 1

        middleware.spider_closed(crawler.spider)
        with pytest.raises(DownloadCancelledError):
            await blocked

    @coroutine_test
    async def test_bytes_header_value_supported(self):
        clock = _ControllableClock()
        scheduler = _FakeScheduler()
        crawler, middleware = self._build(wall_clock=clock, scheduler=scheduler)
        future = datetime.fromtimestamp(clock.now + 2, tz=UTC)
        raw_date = formatdate(future.timestamp(), usegmt=True).encode("ascii")
        req = Request(self.url)
        task = asyncio.ensure_future(
            cast(
                "Any",
                middleware.process_response(req, self._response(503, raw_date)),
            )
        )
        await self._start(task)
        assert scheduler.timers[0].delay == pytest.approx(2.0)
        scheduler.fire()
        assert isinstance(await task, Request)


class TestRetryAfterRealLoop:
    """The waits above use a fake scheduler; these exercise the real
    non-blocking timer integration through call_later()."""

    @coroutine_test
    async def test_real_timer_waits_and_retries(self):
        crawler = get_crawler(
            DefaultSpider,
            {"RETRY_AFTER_ENABLED": True, "RETRY_AFTER_MAX_DELAY": 10},
        )
        crawler.spider = crawler._create_spider()
        middleware = build_from_crawler(RetryMiddleware, crawler)
        req = Request("http://example.com/429")
        rsp = Response(
            "http://example.com/429",
            body=b"",
            status=429,
            headers={"Retry-After": "1"},
        )
        waited = cast("Any", middleware.process_response(req, rsp))
        retry_request = await asyncio.wait_for(waited, timeout=5)
        assert isinstance(retry_request, Request)
        assert retry_request.meta["retry_times"] == 1
        assert crawler.stats.get_value("retry/retry_after/delayed") == 1
        assert middleware._pending_delays == set()

    @coroutine_test
    async def test_real_wait_does_not_block_event_loop(self):
        crawler = get_crawler(
            DefaultSpider,
            {"RETRY_AFTER_ENABLED": True, "RETRY_AFTER_MAX_DELAY": 10},
        )
        crawler.spider = crawler._create_spider()
        middleware = build_from_crawler(RetryMiddleware, crawler)
        req = Request("http://example.com/429")
        rsp = Response(
            "http://example.com/429",
            body=b"",
            status=429,
            headers={"Retry-After": "2"},
        )
        waited = cast("Any", middleware.process_response(req, rsp))

        # The loop stays responsive while the request is waiting.
        async def heartbeat() -> None:
            for _ in range(3):
                await asyncio.sleep(0)

        await asyncio.wait_for(heartbeat(), timeout=1)
        retry_request = await asyncio.wait_for(waited, timeout=5)
        assert isinstance(retry_request, Request)

    @coroutine_test
    async def test_real_timer_cancelled_on_close(self):
        crawler = get_crawler(
            DefaultSpider,
            {"RETRY_AFTER_ENABLED": True, "RETRY_AFTER_MAX_DELAY": 60},
        )
        crawler.spider = crawler._create_spider()
        middleware = build_from_crawler(RetryMiddleware, crawler)
        req = Request("http://example.com/429")
        rsp = Response(
            "http://example.com/429",
            body=b"",
            status=429,
            headers={"Retry-After": "60"},
        )
        waited = asyncio.ensure_future(
            cast("Any", middleware.process_response(req, rsp))
        )
        await asyncio.sleep(0)
        assert len(middleware._pending_delays) == 1
        middleware.spider_closed(crawler.spider)
        with pytest.raises(DownloadCancelledError):
            await waited
        assert crawler.stats.get_value("retry/count") is None


class TestGetRetryRequest:
    @staticmethod
    def get_spider(settings: dict[str, Any] | None = None) -> Spider:
        crawler = get_crawler(Spider, settings or {})
        return crawler._create_spider("foo")

    def test_basic_usage(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.DEBUG):
            new_request = get_retry_request(
                request,
                spider=spider,
            )
        assert isinstance(new_request, Request)
        assert new_request != request
        assert new_request.meta["skip_dupefilter_once"] is True
        expected_retry_times = 1
        assert new_request.meta["retry_times"] == expected_retry_times
        assert new_request.priority == -1
        expected_reason = "unspecified"
        for stat in ("retry/count", f"retry/reason_count/{expected_reason}"):
            assert spider.crawler.stats.get_value(stat) == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_max_retries_reached(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        max_retry_times = 0
        with caplog.at_level(logging.DEBUG):
            new_request = get_retry_request(
                request,
                spider=spider,
                max_retry_times=max_retry_times,
            )
        assert new_request is None
        assert spider.crawler.stats.get_value("retry/max_reached") == 1
        failure_count = max_retry_times + 1
        expected_reason = "unspecified"
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.ERROR,
            (
                f"Gave up retrying {request} (failed {failure_count} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_one_retry(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.DEBUG):
            new_request = get_retry_request(
                request,
                spider=spider,
                max_retry_times=1,
            )
        assert isinstance(new_request, Request)
        assert new_request != request
        assert new_request.meta["skip_dupefilter_once"] is True
        expected_retry_times = 1
        assert new_request.meta["retry_times"] == expected_retry_times
        assert new_request.priority == -1
        expected_reason = "unspecified"
        for stat in ("retry/count", f"retry/reason_count/{expected_reason}"):
            assert spider.crawler.stats.get_value(stat) == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_two_retries(self, caplog: pytest.LogCaptureFixture) -> None:
        spider = self.get_spider()
        request = Request("https://example.com")
        new_request = request
        max_retry_times = 2
        for index in range(max_retry_times):
            caplog.clear()
            with caplog.at_level(logging.DEBUG):
                new_request = cast(
                    "Request",
                    get_retry_request(
                        new_request,
                        spider=spider,
                        max_retry_times=max_retry_times,
                    ),
                )
            assert isinstance(new_request, Request)
            assert new_request != request
            assert new_request.meta["skip_dupefilter_once"] is True
            expected_retry_times = index + 1
            assert new_request.meta["retry_times"] == expected_retry_times
            assert new_request.priority == -expected_retry_times
            expected_reason = "unspecified"
            for stat in ("retry/count", f"retry/reason_count/{expected_reason}"):
                value = spider.crawler.stats.get_value(stat)
                assert value == expected_retry_times
            assert (
                "scrapy.downloadermiddlewares.retry",
                logging.DEBUG,
                (
                    f"Retrying {request} (failed {expected_retry_times} times): "
                    f"{expected_reason}"
                ),
            ) in caplog.record_tuples

        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            new_request = cast(
                "Request",
                get_retry_request(
                    new_request,
                    spider=spider,
                    max_retry_times=max_retry_times,
                ),
            )
        assert new_request is None
        assert spider.crawler.stats.get_value("retry/max_reached") == 1
        failure_count = max_retry_times + 1
        expected_reason = "unspecified"
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.ERROR,
            (
                f"Gave up retrying {request} (failed {failure_count} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_no_spider(self):
        request = Request("https://example.com")
        with pytest.raises(TypeError):
            get_retry_request(request)  # type: ignore[call-arg]  # pylint: disable=missing-kwoa

    def test_max_retry_times_setting(self):
        max_retry_times = 0
        spider = self.get_spider({"RETRY_TIMES": max_retry_times})
        request = Request("https://example.com")
        new_request = get_retry_request(
            request,
            spider=spider,
        )
        assert new_request is None

    def test_max_retry_times_meta(self):
        max_retry_times = 0
        spider = self.get_spider({"RETRY_TIMES": max_retry_times + 1})
        meta = {"max_retry_times": max_retry_times}
        request = Request("https://example.com", meta=meta)
        new_request = get_retry_request(
            request,
            spider=spider,
        )
        assert new_request is None

    def test_max_retry_times_argument(self):
        max_retry_times = 0
        spider = self.get_spider({"RETRY_TIMES": max_retry_times + 1})
        meta = {"max_retry_times": max_retry_times + 1}
        request = Request("https://example.com", meta=meta)
        new_request = get_retry_request(
            request,
            spider=spider,
            max_retry_times=max_retry_times,
        )
        assert new_request is None

    def test_priority_adjust_setting(self):
        priority_adjust = 1
        spider = self.get_spider({"RETRY_PRIORITY_ADJUST": priority_adjust})
        request = Request("https://example.com")
        new_request = get_retry_request(
            request,
            spider=spider,
        )
        assert new_request
        assert new_request.priority == priority_adjust

    def test_priority_adjust_argument(self):
        priority_adjust = 1
        spider = self.get_spider({"RETRY_PRIORITY_ADJUST": priority_adjust + 1})
        request = Request("https://example.com")
        new_request = get_retry_request(
            request,
            spider=spider,
            priority_adjust=priority_adjust,
        )
        assert new_request
        assert new_request.priority == priority_adjust

    def test_log_extra_retry_success(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
            )
        assert any(getattr(r, "spider", None) is spider for r in caplog.records)

    def test_log_extra_retries_exceeded(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                max_retry_times=0,
            )
        assert any(getattr(r, "spider", None) is spider for r in caplog.records)

    def test_reason_string(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = "because"
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                reason=expected_reason,
            )
        expected_retry_times = 1
        for stat in ("retry/count", f"retry/reason_count/{expected_reason}"):
            assert spider.crawler.stats.get_value(stat) == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_reason_builtin_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = NotImplementedError()
        expected_reason_string = "builtins.NotImplementedError"
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                reason=expected_reason,
            )
        expected_retry_times = 1
        stat = spider.crawler.stats.get_value(
            f"retry/reason_count/{expected_reason_string}"
        )
        assert stat == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_reason_builtin_exception_class(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = NotImplementedError
        expected_reason_string = "builtins.NotImplementedError"
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                reason=expected_reason,
            )
        expected_retry_times = 1
        stat = spider.crawler.stats.get_value(
            f"retry/reason_count/{expected_reason_string}"
        )
        assert stat == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_reason_custom_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = IgnoreRequest()
        expected_reason_string = "scrapy.exceptions.IgnoreRequest"
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                reason=expected_reason,
            )
        expected_retry_times = 1
        stat = spider.crawler.stats.get_value(
            f"retry/reason_count/{expected_reason_string}"
        )
        assert stat == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_reason_custom_exception_class(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = IgnoreRequest
        expected_reason_string = "scrapy.exceptions.IgnoreRequest"
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                reason=expected_reason,
            )
        expected_retry_times = 1
        stat = spider.crawler.stats.get_value(
            f"retry/reason_count/{expected_reason_string}"
        )
        assert stat == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_custom_logger(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = logging.getLogger("custom-logger")
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = "because"
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                reason=expected_reason,
                logger=logger,
            )
        assert (
            "custom-logger",
            logging.DEBUG,
            f"Retrying {request} (failed 1 times): {expected_reason}",
        ) in caplog.record_tuples

    def test_give_up_log_level_default(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.ERROR):
            get_retry_request(
                request,
                spider=spider,
                max_retry_times=0,
            )
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.ERROR,
            f"Gave up retrying {request} (failed 1 times): unspecified",
        ) in caplog.record_tuples

    def test_give_up_log_level_argument_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.WARNING):
            get_retry_request(
                request,
                spider=spider,
                max_retry_times=0,
                give_up_log_level="WARNING",
            )
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.WARNING,
            f"Gave up retrying {request} (failed 1 times): unspecified",
        ) in caplog.record_tuples

    def test_give_up_log_level_argument_number(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.WARNING):
            get_retry_request(
                request,
                spider=spider,
                max_retry_times=0,
                give_up_log_level=logging.WARNING,
            )
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.WARNING,
            f"Gave up retrying {request} (failed 1 times): unspecified",
        ) in caplog.record_tuples

    def test_give_up_log_level_setting(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider({"RETRY_GIVE_UP_LOG_LEVEL": "WARNING"})
        with caplog.at_level(logging.WARNING):
            get_retry_request(
                request,
                spider=spider,
                max_retry_times=0,
            )
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.WARNING,
            f"Gave up retrying {request} (failed 1 times): unspecified",
        ) in caplog.record_tuples

    def test_give_up_log_level_invalid(self):
        request = Request("https://example.com")
        spider = self.get_spider()
        with pytest.raises(ValueError, match="Invalid give-up log level"):
            get_retry_request(
                request,
                spider=spider,
                max_retry_times=0,
                give_up_log_level="NOT_A_LEVEL",
            )

    def test_custom_stats_key(self):
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = "because"
        stats_key = "custom_retry"
        get_retry_request(
            request,
            spider=spider,
            reason=expected_reason,
            stats_base_key=stats_key,
        )
        for stat in (
            f"{stats_key}/count",
            f"{stats_key}/reason_count/{expected_reason}",
        ):
            assert spider.crawler.stats.get_value(stat) == 1


class _RetryThenRedirectSpider(Spider):
    """Retries two requests whose retries redirect to an already-seen URL."""

    name = "retry_then_redirect"
    mockserver: MockServer

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen_parsed = 0

    async def start(self) -> AsyncIterator[Any]:
        seen = self.mockserver.url("/status?n=200")
        yield Request(seen)
        for n in range(2):
            url = self.mockserver.url(
                "/redirect-to?" + urlencode({"goto": seen, "n": n})
            )
            yield Request(
                url, meta={"dont_redirect": True, "handle_httpstatus_list": [302]}
            )

    def parse(self, response: Response) -> Request | None:
        if response.status == 200:
            self.seen_parsed += 1
            return None
        assert response.request is not None
        request = get_retry_request(response.request, spider=self)
        assert request is not None
        del request.meta["dont_redirect"]
        del request.meta["handle_httpstatus_list"]
        return request


class _LegacyScheduler(Scheduler):
    supports_skip_dupefilter_once = False


class _OverridingScheduler(Scheduler):
    def enqueue_request(self, request: Request) -> bool:
        if not request.dont_filter and self.df.request_seen(request):
            self.df.log(request, self.spider)
            return False
        self._mqpush(request)
        return True


@coroutine_test
async def test_retry_redirect_dupefilter(mockserver: MockServer) -> None:
    crawler = get_crawler(_RetryThenRedirectSpider)
    await crawler.crawl_async(mockserver=mockserver)
    assert crawler.spider is not None
    assert cast("_RetryThenRedirectSpider", crawler.spider).seen_parsed == 1
    assert crawler.stats is not None
    assert crawler.stats.get_value("dupefilter/filtered") == 2


@pytest.mark.parametrize("scheduler", [_LegacyScheduler, _OverridingScheduler])
@coroutine_test
async def test_retry_redirect_dupefilter_legacy_scheduler(
    mockserver: MockServer, scheduler: type[Scheduler]
) -> None:
    crawler = get_crawler(_RetryThenRedirectSpider, {"SCHEDULER": scheduler})
    with pytest.warns(
        ScrapyDeprecationWarning, match="supports_skip_dupefilter_once"
    ) as record:
        await crawler.crawl_async(mockserver=mockserver)
    assert sum("supports_skip_dupefilter_once" in str(w.message) for w in record) == 1
    assert crawler.spider is not None
    assert cast("_RetryThenRedirectSpider", crawler.spider).seen_parsed == 3


class _RetryAfterSpider(Spider):
    """Crawls a rate-limited URL next to a healthy one.

    The 429 URL keeps answering 429 with a Retry-After header, so its request
    is delayed before each retry until its budget runs out, while the 200 URL
    is crawled normally in the meantime.
    """

    name = "retry_after"
    mockserver: MockServer

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.statuses: list[int] = []

    async def start(self) -> AsyncIterator[Any]:
        yield Request(
            self.mockserver.url("/response-headers?n=429"),
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"Retry-After": "1"}),
            dont_filter=True,
        )
        yield Request(self.mockserver.url("/status?n=200"), dont_filter=True)

    def parse(self, response: Response) -> Any:
        self.statuses.append(response.status)
        return None


@coroutine_test
async def test_retry_after_end_to_end(mockserver: MockServer) -> None:
    settings = {
        "RETRY_AFTER_ENABLED": True,
        "RETRY_AFTER_MAX_DELAY": 10,
        "RETRY_TIMES": 1,
    }
    crawler = get_crawler(_RetryAfterSpider, settings)
    await crawler.crawl_async(mockserver=mockserver)
    assert crawler.spider is not None
    spider = cast("_RetryAfterSpider", crawler.spider)
    # The healthy response reaches the callback even while the rate-limited
    # request is waiting; the exhausted 429 request is dropped by the
    # middleware and never reaches the callback.
    assert spider.statuses == [200]
    assert crawler.stats is not None
    # One wait for the single allowed retry, then the budget is exhausted.
    assert crawler.stats.get_value("retry/retry_after/delayed") == 1
    assert crawler.stats.get_value("retry/count") == 1
    assert crawler.stats.get_value("retry/max_reached") == 1
