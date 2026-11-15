from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from email.utils import formatdate
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from twisted.internet.defer import CancelledError as TwistedCancelledError
from twisted.internet.defer import Deferred

from scrapy import signals
from scrapy.downloadermiddlewares.retry import RetryMiddleware, _parse_retry_after_delay
from scrapy.exceptions import NotConfigured
from scrapy.http import Request, Response
from scrapy.utils.asyncio import is_asyncio_available
from scrapy.utils.asyncio import sleep as project_sleep
from scrapy.utils.defer import deferred_from_coro, maybe_deferred_to_future
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from scrapy.crawler import Crawler

_URL = "http://retry-after.example/503"


def _http_date(seconds_from_now: float) -> str:
    return formatdate(
        (datetime.now(UTC) + timedelta(seconds=seconds_from_now)).timestamp(),
        usegmt=True,
    )


class _ControlledSleep:
    """Replaces the sleep used by the retry middleware with a clock the test
    controls, so no test waits for real time.

    Every sleep parks on an object the test releases with :meth:`release`; the
    object is an :class:`asyncio.Event` when an asyncio loop drives the test and
    an unresolved :class:`~twisted.internet.defer.Deferred` otherwise, so the
    same test code runs under every supported reactor.
    """

    def __init__(self) -> None:
        self.requested: list[float] = []
        self._parked: list[Any] = []

    async def __call__(self, delay: float) -> None:
        self.requested.append(delay)
        if is_asyncio_available():
            event = asyncio.Event()
            self._parked.append(event)
            await event.wait()
            return
        deferred: Deferred[None] = Deferred()
        self._parked.append(deferred)
        await maybe_deferred_to_future(deferred)

    @property
    def parked(self) -> int:
        if is_asyncio_available():
            return sum(not event.is_set() for event in self._parked)
        return sum(not deferred.called for deferred in self._parked)

    def release(self) -> None:
        """End the oldest wait that is still parked."""
        for parked in self._parked:
            if isinstance(parked, Deferred):
                if not parked.called:
                    parked.callback(None)
                    return
            elif not parked.is_set():
                parked.set()
                return
        raise AssertionError("no sleep is parked")

    def patch(self) -> Any:
        return mock.patch("scrapy.downloadermiddlewares.retry.sleep", new=self)


async def _yield_control() -> None:
    """Let newly scheduled tasks reach their first suspension point."""
    # A zero-delay sleep works under both asyncio and Twisted reactors.
    for _ in range(5):
        await project_sleep(0)


def _start(coro: Any) -> Deferred[Any]:
    """Schedule a coroutine the same way the project does, on any reactor."""
    return deferred_from_coro(coro)


async def _await_task(task: Deferred[Any]) -> Any:
    return await maybe_deferred_to_future(task)


async def _process_response(
    middleware: RetryMiddleware, request: Request, response: Response
) -> Request | Response:
    """Await process_response() whether it returns a value or a coroutine, like
    the downloader middleware manager does."""
    result = middleware.process_response(request, response)
    if inspect.isawaitable(result):
        return await result
    return result


def _make_crawler(settings: dict[str, Any] | None = None) -> Crawler:
    merged = {"EXTENSIONS": {"scrapy.extensions.corestats.CoreStats": None}}
    if settings:
        merged.update(settings)
    crawler = get_crawler(DefaultSpider, merged)
    crawler.spider = crawler._create_spider()
    return crawler


def _make_middleware(
    settings: dict[str, Any] | None = None,
) -> tuple[RetryMiddleware, Crawler]:
    crawler = _make_crawler(settings)
    middleware = build_from_crawler(RetryMiddleware, crawler)
    return middleware, crawler


def _retry_after_response(value: str | list[str] | None, status: int = 503) -> Response:
    headers = {}
    if value is not None:
        headers["Retry-After"] = value
    return Response(_URL, status=status, headers=headers)


class TestRetryAfterHeaderParsing:
    @pytest.mark.parametrize(
        ("value", "max_delay", "expected"),
        [
            ("30", 120, 30.0),
            ("0", 120, 0.0),
            (" 30", 120, 30.0),
            ("30 ", 120, 30.0),
            ("\t30\t", 120, 30.0),
            (" \t ", 120, None),
            ("200", 120, 120.0),
            ("-5", 120, None),
            ("1.5", 120, None),
            ("10 20", 120, None),
            ("30\n", 120, None),
            ("10, 20", 120, None),
            ("not-a-number", 120, None),
            ("Wed, 21 Oct 2015 07:28:00", 120, None),
            ("Wed, 21 Oct 2015 07:28:00 GMT", 120, 0.0),
            ("x" * 257, 120, None),
        ],
    )
    def test_single_value(
        self, value: str, max_delay: int, expected: float | None
    ) -> None:
        assert _parse_retry_after_delay([value.encode()], max_delay) == expected

    def test_non_ascii_rejected(self) -> None:
        assert _parse_retry_after_delay(["30€".encode()], 120) is None

    def test_empty_rejected(self) -> None:
        assert _parse_retry_after_delay([], 120) is None
        assert _parse_retry_after_delay([b""], 120) is None

    def test_repeated_header_rejected(self) -> None:
        assert _parse_retry_after_delay([b"10", b"20"], 120) is None

    def test_future_date_clamped(self) -> None:
        value = _http_date(200).encode()
        assert _parse_retry_after_delay([value], 120) == 120.0

    def test_near_future_date(self) -> None:
        value = _http_date(10).encode()
        delay = _parse_retry_after_delay([value], 120)
        assert delay is not None
        assert 9 <= delay <= 10

    def test_past_date_is_zero(self) -> None:
        value = _http_date(-30).encode()
        assert _parse_retry_after_delay([value], 120) == 0.0

    def test_date_with_offset_timezone_accepted(self) -> None:
        # Two hours ahead of UTC, still in the past as a UTC instant.
        value = b"Wed, 21 Oct 2015 07:28:00 +0200"
        assert _parse_retry_after_delay([value], 120) == 0.0


class TestRetryAfterSettings:
    def test_disabled_by_default(self) -> None:
        middleware, _ = _make_middleware()
        assert middleware.retry_after_enabled is False
        assert middleware._retry_after is None

    def test_retry_disabled_raises_not_configured(self) -> None:
        with pytest.raises(NotConfigured):
            _make_middleware({"RETRY_ENABLED": False, "RETRY_AFTER_ENABLED": True})

    def test_default_max_delay(self) -> None:
        middleware, _ = _make_middleware({"RETRY_AFTER_ENABLED": True})
        assert middleware.retry_after_max_delay == 120.0

    @pytest.mark.parametrize("value", [0, 1, 120, 0.0, 2.5])
    def test_valid_max_delay(self, value: float) -> None:
        middleware, _ = _make_middleware(
            {"RETRY_AFTER_ENABLED": True, "RETRY_AFTER_MAX_DELAY": value}
        )
        assert middleware.retry_after_max_delay == float(value)

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_rejected(self, value: bool) -> None:
        with pytest.raises(TypeError, match="RETRY_AFTER_MAX_DELAY"):
            _make_middleware(
                {"RETRY_AFTER_ENABLED": True, "RETRY_AFTER_MAX_DELAY": value}
            )

    @pytest.mark.parametrize(
        "value", [-1, -0.5, float("inf"), float("-inf"), float("nan")]
    )
    def test_non_finite_or_negative_rejected(self, value: float) -> None:
        with pytest.raises((TypeError, ValueError), match="RETRY_AFTER_MAX_DELAY"):
            _make_middleware(
                {"RETRY_AFTER_ENABLED": True, "RETRY_AFTER_MAX_DELAY": value}
            )

    @pytest.mark.parametrize("value", ["120", None, [120]])
    def test_wrong_type_rejected(self, value: Any) -> None:
        with pytest.raises(TypeError, match="RETRY_AFTER_MAX_DELAY"):
            _make_middleware(
                {"RETRY_AFTER_ENABLED": True, "RETRY_AFTER_MAX_DELAY": value}
            )

    def test_disabled_does_not_validate_max_delay(self) -> None:
        middleware, _ = _make_middleware(
            {"RETRY_AFTER_ENABLED": False, "RETRY_AFTER_MAX_DELAY": "garbage"}
        )
        assert middleware.retry_after_max_delay == "garbage"


class TestRetryAfterDisabled:
    def test_header_ignored_immediate_retry(self) -> None:
        middleware, crawler = _make_middleware()
        request = Request(_URL)
        response = _retry_after_response("60")
        result = middleware.process_response(request, response)
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        # No Retry-After stats exist while the feature is off.
        assert not any(
            key.startswith("retry_after/") for key in crawler.stats.get_stats()
        )

    def test_header_ignored_when_retries_exhausted(self) -> None:
        middleware, crawler = _make_middleware({"RETRY_TIMES": 0})
        request = Request(_URL)
        response = _retry_after_response("60")
        assert middleware.process_response(request, response) is response
        assert crawler.stats.get_value("retry/max_reached") == 1


class TestRetryAfterWait:
    def _middleware(self) -> tuple[RetryMiddleware, Crawler]:
        return _make_middleware(
            {"RETRY_AFTER_ENABLED": True, "RETRY_AFTER_MAX_DELAY": 120}
        )

    @coroutine_test
    async def test_integer_delay_waited(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        with sleep.patch():
            task = _start(
                middleware.process_response(Request(_URL), _retry_after_response("30"))
            )
            await _yield_control()
            assert sleep.parked == 1
            assert task.called is False
            sleep.release()
            result = await _await_task(task)
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert sleep.requested == [30.0]
        stats = crawler.stats
        assert stats.get_value("retry_after/accepted") == 1
        assert stats.get_value("retry_after/waited_seconds") == 30.0
        assert stats.get_value("retry_after/invalid") is None
        assert stats.get_value("retry_after/cancelled") is None

    @coroutine_test
    async def test_delay_clamped_to_max(self) -> None:
        sleep = _ControlledSleep()
        middleware, _ = self._middleware()
        with sleep.patch():
            task = _start(
                middleware.process_response(Request(_URL), _retry_after_response("500"))
            )
            await _yield_control()
            assert sleep.requested == [120.0]
            sleep.release()
            await _await_task(task)

    @coroutine_test
    async def test_future_date_waited_clamped(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        response = _retry_after_response(_http_date(200))
        with sleep.patch():
            task = _start(middleware.process_response(Request(_URL), response))
            await _yield_control()
            assert sleep.requested == [120.0]
            sleep.release()
            result = await _await_task(task)
        assert isinstance(result, Request)
        assert crawler.stats.get_value("retry_after/waited_seconds") == 120.0

    @coroutine_test
    async def test_past_date_waits_zero(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        response = _retry_after_response(_http_date(-30))
        with sleep.patch():
            task = _start(middleware.process_response(Request(_URL), response))
            await _yield_control()
            assert sleep.requested == [0.0]
            sleep.release()
            result = await _await_task(task)
        assert isinstance(result, Request)
        assert crawler.stats.get_value("retry_after/waited_seconds") == 0.0

    @coroutine_test
    async def test_invalid_header_falls_back_to_immediate_retry(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        response = _retry_after_response("-3")
        with sleep.patch():
            result = await _process_response(middleware, Request(_URL), response)
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert sleep.requested == []
        assert crawler.stats.get_value("retry_after/invalid") == 1
        assert crawler.stats.get_value("retry_after/accepted") is None

    @coroutine_test
    async def test_duplicate_header_falls_back(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        response = _retry_after_response(["10", "20"])
        with sleep.patch():
            result = await _process_response(middleware, Request(_URL), response)
        assert isinstance(result, Request)
        assert sleep.requested == []
        assert crawler.stats.get_value("retry_after/invalid") == 1

    @coroutine_test
    async def test_dont_retry_not_retried(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        request = Request(_URL, meta={"dont_retry": True})
        response = _retry_after_response("30")
        with sleep.patch():
            result = middleware.process_response(request, response)
        assert result is response
        assert sleep.requested == []
        assert not any(
            key.startswith("retry_after/") for key in crawler.stats.get_stats()
        )

    @coroutine_test
    async def test_non_retry_status_ignored(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        response = _retry_after_response("30", status=200)
        with sleep.patch():
            result = middleware.process_response(Request(_URL), response)
        assert result is response
        assert sleep.requested == []
        assert not any(
            key.startswith("retry_after/") for key in crawler.stats.get_stats()
        )

    @coroutine_test
    async def test_retries_exhausted_not_waited(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = _make_middleware(
            {
                "RETRY_AFTER_ENABLED": True,
                "RETRY_AFTER_MAX_DELAY": 120,
                "RETRY_TIMES": 0,
            }
        )
        response = _retry_after_response("30")
        with sleep.patch():
            result = await _process_response(middleware, Request(_URL), response)
        # No retry is left, so the request is given up on without waiting and
        # no Retry-After accounting takes place.
        assert result is response
        assert sleep.requested == []
        assert middleware._retry_after is not None
        assert middleware._retry_after.pending == 0
        assert crawler.stats.get_value("retry_after/accepted") is None
        assert crawler.stats.get_value("retry_after/waited_seconds") is None
        assert crawler.stats.get_value("retry/max_reached") == 1

    @coroutine_test
    async def test_exhausted_after_spending_retries_not_waited(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        response = _retry_after_response("30")
        request = Request(_URL, meta={"retry_times": 2})
        with sleep.patch():
            result = await _process_response(middleware, request, response)
        assert result is response
        assert sleep.requested == []
        assert crawler.stats.get_value("retry_after/accepted") is None
        assert crawler.stats.get_value("retry/max_reached") == 1

    @coroutine_test
    async def test_per_request_max_retry_times_none_falls_back_to_setting(
        self,
    ) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = _make_middleware(
            {"RETRY_AFTER_ENABLED": True, "RETRY_TIMES": 1}
        )
        response = _retry_after_response("30")
        request = Request(_URL, meta={"max_retry_times": None})
        with sleep.patch():
            task = _start(middleware.process_response(request, response))
            await _yield_control()
            assert sleep.parked == 1
            sleep.release()
            result = await _await_task(task)
        assert isinstance(result, Request)
        assert crawler.stats.get_value("retry_after/accepted") == 1

    @coroutine_test
    async def test_per_request_budget_exhausted_not_waited(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        response = _retry_after_response("30")
        request = Request(_URL, meta={"max_retry_times": 1, "retry_times": 1})
        with sleep.patch():
            result = await _process_response(middleware, request, response)
        assert result is response
        assert sleep.requested == []
        assert crawler.stats.get_value("retry_after/accepted") is None
        assert crawler.stats.get_value("retry/max_reached") == 1

    @coroutine_test
    async def test_retry_request_preserves_attributes(self) -> None:
        sleep = _ControlledSleep()
        middleware, _ = self._middleware()

        def callback(response: Response) -> None:
            pass

        def errback(failure: object) -> None:
            pass

        request = Request(
            _URL,
            callback=callback,
            errback=errback,
            body=b"request body",
            cb_kwargs={"page": 7},
            headers={"X-Test": "1"},
            meta={"custom": "value"},
        )
        with sleep.patch():
            task = _start(
                middleware.process_response(request, _retry_after_response("0"))
            )
            await _yield_control()
            sleep.release()
            result = await _await_task(task)
        assert isinstance(result, Request)
        assert result.callback is callback
        assert result.errback is errback
        assert result.body == b"request body"
        assert result.cb_kwargs == {"page": 7}
        assert result.meta["custom"] == "value"
        assert result.headers.get("X-Test") == b"1"
        # The original request is not mutated.
        assert "retry_times" not in request.meta
        assert request.headers.get("X-Test") == b"1"

    @coroutine_test
    async def test_external_cancellation_propagates(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        response = _retry_after_response("30")
        with sleep.patch():
            task = _start(middleware.process_response(Request(_URL), response))
            await _yield_control()
            assert middleware._retry_after is not None
            assert middleware._retry_after.pending == 1
            task.cancel()
            # A cancellation from outside the scheduler keeps propagating, so
            # the engine can drop the download output instead of scraping it.
            with pytest.raises((asyncio.CancelledError, TwistedCancelledError)):
                await _await_task(task)
        assert middleware._retry_after.pending == 0
        # An external cancellation is not reported as a successful scheduler
        # cancellation, and never produces a retry request.
        assert crawler.stats.get_value("retry_after/cancelled") is None
        assert crawler.stats.get_value("retry_after/accepted") == 1
        assert crawler.stats.get_value("retry_after/waited_seconds") is None
        assert crawler.stats.get_value("retry/count") is None

    @coroutine_test
    async def test_spider_closed_cancels_wait(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        response = _retry_after_response("30")
        with sleep.patch():
            task = _start(middleware.process_response(Request(_URL), response))
            await _yield_control()
            assert middleware._retry_after is not None
            assert middleware._retry_after.pending == 1
            await crawler.signals.send_catch_log_async(
                signal=signals.spider_closed,
                spider=crawler.spider,
                reason="shutdown",
            )
            result = await _await_task(task)
        # The coroutine still finishes, but no retry request is published.
        assert result is response
        assert middleware._retry_after.pending == 0
        assert crawler.stats.get_value("retry_after/cancelled") == 1
        assert crawler.stats.get_value("retry_after/waited_seconds") is None
        assert crawler.stats.get_value("retry/count") is None

    @coroutine_test
    async def test_two_crawlers_are_isolated(self) -> None:
        sleep = _ControlledSleep()
        middleware_a, crawler_a = self._middleware()
        middleware_b, crawler_b = self._middleware()
        response_a = _retry_after_response("30")
        response_b = _retry_after_response("30")
        with sleep.patch():
            task_a = _start(middleware_a.process_response(Request(_URL), response_a))
            task_b = _start(middleware_b.process_response(Request(_URL), response_b))
            await _yield_control()
            assert middleware_a._retry_after is not None
            assert middleware_b._retry_after is not None
            assert middleware_a._retry_after.pending == 1
            assert middleware_b._retry_after.pending == 1

            # Closing one crawler cancels only its own wait.
            await crawler_a.signals.send_catch_log_async(
                signal=signals.spider_closed,
                spider=crawler_a.spider,
                reason="shutdown",
            )
            result_a = await _await_task(task_a)
            assert result_a is response_a
            assert not isinstance(result_a, Request)
            assert middleware_b._retry_after.pending == 1

            task_b.cancel()
            with pytest.raises((asyncio.CancelledError, TwistedCancelledError)):
                await _await_task(task_b)
        # Crawler A's wait was cancelled by its own spider_closed signal and
        # reported as such; crawler B's external cancellation is not.
        assert crawler_a.stats.get_value("retry_after/cancelled") == 1
        assert crawler_b.stats.get_value("retry_after/cancelled") is None
        assert crawler_a.stats.get_value("retry_after/waited_seconds") is None
        assert crawler_b.stats.get_value("retry_after/waited_seconds") is None
        assert crawler_b.stats.get_value("retry/count") is None

    @coroutine_test
    async def test_waited_seconds_accumulates(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        with sleep.patch():
            for header in ("10", "20", "30"):
                task = _start(
                    middleware.process_response(
                        Request(_URL), _retry_after_response(header)
                    )
                )
                await _yield_control()
                sleep.release()
                await _await_task(task)
        # Every completed wait contributes its (clamped) delay, so the stat is
        # a running total rather than the value of the last wait.
        assert sleep.requested == [10.0, 20.0, 30.0]
        assert crawler.stats.get_value("retry_after/waited_seconds") == 60.0
        assert crawler.stats.get_value("retry_after/accepted") == 3

    @coroutine_test
    async def test_fixed_stats_keys(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        with sleep.patch():
            for header in ("10", "bad"):
                task = _start(
                    middleware.process_response(
                        Request(_URL), _retry_after_response(header)
                    )
                )
                await _yield_control()
                if sleep.parked:
                    sleep.release()
                await _await_task(task)
        keys = {
            key for key in crawler.stats.get_stats() if key.startswith("retry_after/")
        }
        assert keys == {
            "retry_after/accepted",
            "retry_after/invalid",
            "retry_after/waited_seconds",
        }
        # No URL or header value leaks into a stat key.
        assert all(_URL not in key and "10" not in key for key in keys)

    @coroutine_test
    async def test_cancelled_key_is_fixed_name(self) -> None:
        sleep = _ControlledSleep()
        middleware, crawler = self._middleware()
        response = _retry_after_response("30")
        with sleep.patch():
            task = _start(middleware.process_response(Request(_URL), response))
            await _yield_control()
            await crawler.signals.send_catch_log_async(
                signal=signals.spider_closed,
                spider=crawler.spider,
                reason="shutdown",
            )
            await _await_task(task)
        keys = {
            key for key in crawler.stats.get_stats() if key.startswith("retry_after/")
        }
        assert keys == {"retry_after/accepted", "retry_after/cancelled"}
        assert all(_URL not in key for key in keys)


class TestCrawlCloseCancellation:
    """End-to-end check through the crawler signals: a spider_closed
    notification cancels a pending Retry-After wait instead of scheduling the
    retry afterwards."""

    @coroutine_test
    async def test_close_during_wait(self) -> None:
        sleep = _ControlledSleep()
        long_delay = 3600
        middleware, crawler = _make_middleware(
            {
                "RETRY_AFTER_ENABLED": True,
                "RETRY_AFTER_MAX_DELAY": long_delay,
            }
        )
        with sleep.patch():
            task = _start(
                middleware.process_response(
                    Request(_URL), _retry_after_response(str(long_delay))
                )
            )
            await _yield_control()
            assert sleep.requested == [float(long_delay)]
            assert middleware._retry_after is not None
            assert middleware._retry_after.pending == 1

            await crawler.signals.send_catch_log_async(
                signal=signals.spider_closed,
                spider=crawler.spider,
                reason="shutdown",
            )
            result = await _await_task(task)
        assert isinstance(result, Response)
        assert not isinstance(result, Request)
        assert middleware._retry_after.pending == 0
        assert crawler.stats.get_value("retry_after/cancelled") == 1
        assert crawler.stats.get_value("retry/count") is None
