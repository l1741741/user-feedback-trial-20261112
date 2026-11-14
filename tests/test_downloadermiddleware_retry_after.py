from __future__ import annotations

import asyncio
import gc
import threading
import weakref
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from twisted.internet.defer import CancelledError
from twisted.internet.task import Clock

from scrapy.downloadermiddlewares.retry import RetryMiddleware, _retry_after_delay
from scrapy.http import Request, Response
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from scrapy.http.request import Request as ScrapyRequest

_NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC).timestamp()


def _response(
    *,
    status: int = 429,
    retry_after: Any = None,
    date: Any = ...,
) -> Response:
    headers: dict[str, Any] = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    if date is not ...:
        headers["Date"] = date
    return Response(
        "http://example.com/rate-limited", body=b"", status=status, headers=headers
    )


def _build_middleware(
    settings: dict[str, Any] | None = None,
    *,
    clock: Clock | None = None,
) -> tuple[RetryMiddleware, Clock, Any]:
    crawler = get_crawler(DefaultSpider, settings or {})
    crawler.spider = crawler._create_spider()
    if clock is None:
        clock = Clock()
        clock.rightNow = _NOW
    middleware = build_from_crawler(RetryMiddleware, crawler, clock=clock)
    return middleware, clock, crawler


_ENABLED = {"RETRY_AFTER_ENABLED": True}


async def _cancel_and_capture(
    middleware: RetryMiddleware, clock: Clock, refs: list[weakref.ref[Any]]
) -> None:
    """Schedule a delayed retry, then cancel it, capturing the timer by weak
    reference. Everything strongly reachable from the wait is local to this
    coroutine, so finishing it leaves only cycles for the GC to collect."""
    request = Request("http://example.com/429")
    response = _response(retry_after=b"30")
    task = asyncio.ensure_future(middleware.process_response(request, response))
    await asyncio.sleep(0)
    refs.append(weakref.ref(clock.getDelayedCalls()[0]))
    task.cancel()
    with pytest.raises((asyncio.CancelledError, CancelledError)):
        await task


def test_cancelled_wait_is_garbage_collected() -> None:
    """Cancellation must not leave the timer or the wait reachable.

    The scenario runs in a dedicated event loop on a worker thread: under the
    shared pytest-asyncio loop, cancelled task frames stay reachable until loop
    teardown, which would mask a real middleware leak or fake a false one.
    """
    outcome: dict[str, Any] = {}

    def worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_garbage_collection_scenario(outcome))
        except BaseException as ex:  # propagated to the main thread below
            outcome["error"] = ex
        finally:
            loop.close()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    gc.collect()
    assert outcome["clean"] is True
    assert outcome["ref"]() is None


async def _garbage_collection_scenario(outcome: dict[str, Any]) -> None:
    middleware, clock, _ = _build_middleware(_ENABLED)
    refs: list[weakref.ref[Any]] = []
    await _cancel_and_capture(middleware, clock, refs)
    outcome["clean"] = clock.getDelayedCalls() == []
    outcome["ref"] = refs[0]


class TestRetryAfterParsing:
    def test_missing_header(self) -> None:
        assert _retry_after_delay(_response(), _NOW) is None

    def test_seconds(self) -> None:
        assert _retry_after_delay(_response(retry_after=b"30"), _NOW) == 30.0

    def test_zero_seconds(self) -> None:
        assert _retry_after_delay(_response(retry_after=b"0"), _NOW) == 0.0

    @pytest.mark.parametrize("value", [b"-5", b"1.5", b"", b"   ", b"abc"])
    def test_invalid_seconds(self, value: bytes) -> None:
        assert _retry_after_delay(_response(retry_after=value), _NOW) is None

    def test_overflow_seconds(self) -> None:
        assert _retry_after_delay(_response(retry_after=b"9" * 100), _NOW) is None

    def test_seconds_above_scheduling_cap(self) -> None:
        assert _retry_after_delay(_response(retry_after=b"2147483648"), _NOW) is None

    def test_seconds_large_enough_to_overflow_float(self) -> None:
        # Long enough that float(int(...)) itself raises OverflowError rather
        # than merely exceeding the scheduling cap.
        assert _retry_after_delay(_response(retry_after=b"9" * 400), _NOW) is None

    def test_multiple_headers(self) -> None:
        response = _response(retry_after=[b"30", b"60"])
        assert _retry_after_delay(response, _NOW) is None

    def test_duplicate_same_header(self) -> None:
        response = _response(retry_after=[b"30", b"30"])
        assert _retry_after_delay(response, _NOW) is None

    @pytest.mark.parametrize("value", [b"not a date", b"Wed, 21 Oct 2026"])
    def test_invalid_date(self, value: bytes) -> None:
        assert _retry_after_delay(_response(retry_after=value), _NOW) is None

    def test_http_date_future(self) -> None:
        retry_after = b"Sat, 14 Nov 2026 12:00:30 GMT"
        assert _retry_after_delay(_response(retry_after=retry_after), _NOW) == 30.0

    def test_http_date_past_clamps_to_zero(self) -> None:
        retry_after = b"Sat, 14 Nov 2026 11:59:30 GMT"
        assert _retry_after_delay(_response(retry_after=retry_after), _NOW) == 0.0

    def test_http_date_with_skewed_server_date(self) -> None:
        # The client clock is 5 minutes ahead of the server clock. Without
        # using the Date header the 30-second Retry-After would look 330
        # seconds away; against the server's own clock it is exactly 30.
        retry_after = b"Sat, 14 Nov 2026 12:00:30 GMT"
        server_date = b"Sat, 14 Nov 2026 12:00:00 GMT"
        response = _response(retry_after=retry_after, date=server_date)
        assert _retry_after_delay(response, _NOW + 300) == 30.0

    def test_http_date_server_in_the_past(self) -> None:
        # Retry-After earlier than the server's own Date clamps to zero
        # regardless of client clock skew.
        retry_after = b"Sat, 14 Nov 2026 11:59:00 GMT"
        server_date = b"Sat, 14 Nov 2026 12:00:00 GMT"
        response = _response(retry_after=retry_after, date=server_date)
        assert _retry_after_delay(response, _NOW - 9999) == 0.0

    def test_invalid_server_date_falls_back_to_client_clock(self) -> None:
        retry_after = b"Sat, 14 Nov 2026 12:00:30 GMT"
        response = _response(retry_after=retry_after, date=b"not a date")
        assert _retry_after_delay(response, _NOW) == 30.0

    def test_multiple_server_dates_fall_back_to_client_clock(self) -> None:
        retry_after = b"Sat, 14 Nov 2026 12:00:30 GMT"
        response = _response(
            retry_after=retry_after,
            date=[b"Sat, 14 Nov 2026 12:00:00 GMT", b"Sat, 14 Nov 2026 12:00:00 GMT"],
        )
        assert _retry_after_delay(response, _NOW) == 30.0


class TestRetryAfterScheduling:
    def _process(
        self,
        middleware: RetryMiddleware,
        request: Request,
        response: Response,
    ) -> asyncio.Task[Any]:
        return asyncio.ensure_future(middleware.process_response(request, response))

    @coroutine_test
    async def test_disabled_by_default_retries_immediately(self) -> None:
        middleware, _, crawler = _build_middleware()
        request = Request("http://example.com/429")
        response = _response(retry_after=b"30")
        result = await middleware.process_response(request, response)
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert crawler.stats.get_value("retry/count") == 1

    @coroutine_test
    async def test_enabled_without_header_retries_immediately(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        request = Request("http://example.com/429")
        result = await middleware.process_response(request, _response())
        assert isinstance(result, Request)
        assert clock.getDelayedCalls() == []

    @coroutine_test
    async def test_invalid_header_retries_immediately(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        request = Request("http://example.com/429")
        result = await middleware.process_response(
            request, _response(retry_after=b"not valid")
        )
        assert isinstance(result, Request)
        assert clock.getDelayedCalls() == []

    @coroutine_test
    async def test_delay_scheduled_and_fired(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        request = Request("http://example.com/429")
        task = self._process(middleware, request, _response(retry_after=b"10"))
        await asyncio.sleep(0)
        assert not task.done()
        assert len(clock.getDelayedCalls()) == 1
        clock.advance(9)
        assert not task.done()
        clock.advance(1)
        result = await task
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert clock.getDelayedCalls() == []

    @coroutine_test
    async def test_zero_delay_fires_without_advancing_clock(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        request = Request("http://example.com/429")
        task = self._process(middleware, request, _response(retry_after=b"0"))
        await asyncio.sleep(0)
        assert len(clock.getDelayedCalls()) == 1
        clock.advance(0)
        result = await task
        assert isinstance(result, Request)
        assert clock.getDelayedCalls() == []

    @coroutine_test
    async def test_cancellation_clears_timer(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        request = Request("http://example.com/429")
        task = self._process(middleware, request, _response(retry_after=b"30"))
        await asyncio.sleep(0)
        assert len(clock.getDelayedCalls()) == 1
        task.cancel()
        with pytest.raises((asyncio.CancelledError, CancelledError)):
            await task
        assert clock.getDelayedCalls() == []

    @coroutine_test
    async def test_two_requests_have_independent_delays(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        request1 = Request("http://example.com/a")
        request2 = Request("http://example.com/b")
        task1 = self._process(middleware, request1, _response(retry_after=b"10"))
        task2 = self._process(middleware, request2, _response(retry_after=b"20"))
        await asyncio.sleep(0)
        assert len(clock.getDelayedCalls()) == 2

        clock.advance(10)
        result1 = await task1
        assert isinstance(result1, Request)
        assert result1.url == "http://example.com/a"
        assert not task2.done()
        assert len(clock.getDelayedCalls()) == 1

        clock.advance(10)
        result2 = await task2
        assert isinstance(result2, Request)
        assert result2.url == "http://example.com/b"
        assert clock.getDelayedCalls() == []

    @coroutine_test
    async def test_cancelling_one_request_keeps_the_other(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        task1 = self._process(
            middleware,
            Request("http://example.com/a"),
            _response(retry_after=b"10"),
        )
        task2 = self._process(
            middleware,
            Request("http://example.com/b"),
            _response(retry_after=b"20"),
        )
        await asyncio.sleep(0)
        task1.cancel()
        with pytest.raises((asyncio.CancelledError, CancelledError)):
            await task1
        assert len(clock.getDelayedCalls()) == 1
        clock.advance(20)
        result2 = await task2
        assert isinstance(result2, Request)
        assert result2.url == "http://example.com/b"

    @coroutine_test
    async def test_delay_over_budget_propagates_response(self) -> None:
        middleware, clock, crawler = _build_middleware(
            {**_ENABLED, "RETRY_AFTER_MAX_DELAY": 5}
        )
        request = Request("http://example.com/429")
        response = _response(retry_after=b"10")
        result = await middleware.process_response(request, response)
        assert result is response
        assert clock.getDelayedCalls() == []
        # The retry request was built (one attempt counted) then refused.
        assert crawler.stats.get_value("retry/count") == 1
        assert crawler.stats.get_value("retry/max_reached") is None

    @coroutine_test
    async def test_delay_within_budget_is_scheduled(self) -> None:
        middleware, clock, _ = _build_middleware(
            {**_ENABLED, "RETRY_AFTER_MAX_DELAY": 10}
        )
        request = Request("http://example.com/429")
        task = self._process(middleware, request, _response(retry_after=b"10"))
        await asyncio.sleep(0)
        assert len(clock.getDelayedCalls()) == 1
        clock.advance(10)
        assert isinstance(await task, Request)

    @coroutine_test
    async def test_budget_can_be_overridden_per_request(self) -> None:
        middleware, clock, _ = _build_middleware(
            {**_ENABLED, "RETRY_AFTER_MAX_DELAY": 100}
        )
        request = Request("http://example.com/429", meta={"retry_after_max_delay": 5})
        response = _response(retry_after=b"10")
        result = await middleware.process_response(request, response)
        assert result is response
        assert clock.getDelayedCalls() == []

    @coroutine_test
    async def test_unlimited_budget_schedules_large_delay(self) -> None:
        middleware, clock, _ = _build_middleware(
            {**_ENABLED, "RETRY_AFTER_MAX_DELAY": -1}
        )
        request = Request("http://example.com/429")
        task = self._process(middleware, request, _response(retry_after=b"1000"))
        await asyncio.sleep(0)
        assert len(clock.getDelayedCalls()) == 1
        task.cancel()
        with pytest.raises((asyncio.CancelledError, CancelledError)):
            await task

    @coroutine_test
    async def test_deadline_intersection_propagates_response(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        # Deadline leaves only 5 seconds, Retry-After asks for 10.
        request = Request(
            "http://example.com/429",
            meta={"download_deadline": clock.seconds() + 5},
        )
        response = _response(retry_after=b"10")
        result = await middleware.process_response(request, response)
        assert result is response
        assert clock.getDelayedCalls() == []

    @coroutine_test
    async def test_deadline_in_the_past_propagates_response(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        request = Request(
            "http://example.com/429",
            meta={"download_deadline": clock.seconds() - 1},
        )
        response = _response(retry_after=b"0")
        result = await middleware.process_response(request, response)
        assert result is response

    @coroutine_test
    async def test_delay_fitting_deadline_is_scheduled(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        request = Request(
            "http://example.com/429",
            meta={"download_deadline": clock.seconds() + 10},
        )
        task = self._process(middleware, request, _response(retry_after=b"10"))
        await asyncio.sleep(0)
        assert len(clock.getDelayedCalls()) == 1
        clock.advance(10)
        assert isinstance(await task, Request)

    @coroutine_test
    async def test_budget_and_deadline_intersect(self) -> None:
        middleware, clock, _ = _build_middleware(
            {**_ENABLED, "RETRY_AFTER_MAX_DELAY": 50}
        )
        # Budget allows 50, but the deadline only leaves 7 seconds.
        request = Request(
            "http://example.com/429",
            meta={"download_deadline": clock.seconds() + 7},
        )
        response = _response(retry_after=b"20")
        result = await middleware.process_response(request, response)
        assert result is response

    @coroutine_test
    async def test_http_date_uses_injected_clock(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        retry_after = b"Sat, 14 Nov 2026 12:00:30 GMT"
        request = Request("http://example.com/429")
        task = self._process(middleware, request, _response(retry_after=retry_after))
        await asyncio.sleep(0)
        scheduled = clock.getDelayedCalls()
        assert len(scheduled) == 1
        assert scheduled[0].getTime() - clock.seconds() == pytest.approx(30.0)
        clock.advance(30)
        assert isinstance(await task, Request)

    @coroutine_test
    async def test_non_429_status_ignores_header(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        request = Request("http://example.com/503")
        response = _response(status=503, retry_after=b"30")
        result = await middleware.process_response(request, response)
        assert isinstance(result, Request)
        assert clock.getDelayedCalls() == []

    @coroutine_test
    async def test_dont_retry_still_respected(self) -> None:
        middleware, clock, _ = _build_middleware(_ENABLED)
        request = Request("http://example.com/429", meta={"dont_retry": True})
        response = _response(retry_after=b"30")
        result = await middleware.process_response(request, response)
        assert result is response
        assert clock.getDelayedCalls() == []

    @coroutine_test
    async def test_exhausted_retries_propagate_response(self) -> None:
        middleware, clock, crawler = _build_middleware(_ENABLED)
        middleware.max_retry_times = 0
        request = Request("http://example.com/429")
        response = _response(retry_after=b"30")
        result = await middleware.process_response(request, response)
        assert result is response
        assert clock.getDelayedCalls() == []
        assert crawler.stats.get_value("retry/max_reached") == 1

    @coroutine_test
    async def test_stats_counted_once_per_retry(self) -> None:
        middleware, clock, crawler = _build_middleware(_ENABLED)
        request = Request("http://example.com/429")
        response = _response(retry_after=b"5")
        task = self._process(middleware, request, response)
        await asyncio.sleep(0)
        # The single scheduling path increments stats exactly once.
        assert crawler.stats.get_value("retry/count") == 1
        clock.advance(5)
        retry_request = cast("ScrapyRequest", await task)
        assert crawler.stats.get_value("retry/count") == 1
        # The retried request carries the next attempt number.
        assert retry_request.meta["retry_times"] == 1
        assert crawler.stats.get_value("retry/reason_count/429 Unknown Status") == 1

    @coroutine_test
    async def test_full_retry_chain_uses_independent_delays(self) -> None:
        middleware, clock, crawler = _build_middleware({**_ENABLED, "RETRY_TIMES": 2})
        request: Any = Request("http://example.com/429")
        response = _response(retry_after=b"5")
        for attempt in (1, 2):
            task = self._process(middleware, request, response)
            await asyncio.sleep(0)
            clock.advance(5)
            request = await task
            assert isinstance(request, Request)
            assert request.meta["retry_times"] == attempt
            assert clock.getDelayedCalls() == []
        assert crawler.stats.get_value("retry/count") == 2
        # The third failure exhausts the retries and returns the response.
        result = await middleware.process_response(request, response)
        assert result is response
        assert crawler.stats.get_value("retry/max_reached") == 1
