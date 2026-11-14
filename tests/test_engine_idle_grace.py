from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING, Any

import pytest

from scrapy import Request, Spider, signals
from scrapy.core.engine import EngineState, ExecutionEngine
from scrapy.exceptions import CloseSpider, DontCloseSpider
from scrapy.utils.asyncio import CallLaterResult, call_later
from scrapy.utils.defer import deferred_from_coro, maybe_deferred_to_future
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler
from tests.test_engine_loop import MemoryScheduler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import Callable

    from scrapy.crawler import Crawler


def assert_state(engine: ExecutionEngine, state: EngineState) -> None:
    assert engine.state is state, engine.state


class FakeClock:
    """Record the delayed calls made through the engine's call_later() seam
    while still scheduling them on the real event loop.

    Only the idle grace period uses that seam in the engine, so every recorded
    call is a grace-period close timer. Delegating to the real loop keeps the
    engine processing requests while a timer is pending, and the records let
    tests assert on timer creation and cancellation deterministically.
    """

    def __init__(self) -> None:
        self.calls: list[FakeCall] = []

    def __call__(
        self,
        delay: float,
        func: Callable[..., Any],
        *args: Any,
    ) -> CallLaterResult:
        call = FakeCall(func, args)
        self.calls.append(call)
        # A real timer keeps the event loop and the engine alive. Its handle is
        # what gets canceled when the grace period is canceled early.
        real_result = call_later(delay, call.run)
        call.result = real_result
        # Mirror a cancellation of the real handle onto the record.
        real_cancel = real_result.cancel

        def tracked_cancel() -> None:
            real_cancel()
            if call.active:
                call.active = False
                call.canceled = True

        real_result.cancel = tracked_cancel  # type: ignore[method-assign]
        return real_result

    @property
    def pending(self) -> list["FakeCall"]:
        return [call for call in self.calls if call.active]


class FakeCall:
    def __init__(
        self,
        func: Callable[..., Any],
        args: tuple[Any, ...],
    ) -> None:
        self.func = func
        self.args = args
        self.active = True
        self.fired = False
        self.canceled = False
        self.result: CallLaterResult | None = None

    def run(self) -> None:
        if not self.active:
            return
        self.active = False
        self.fired = True
        self.func(*self.args)


def start_crawl(crawler: Crawler) -> Any:
    """Open and start the crawler's manually built engine in the background
    and return something awaitable from both asyncio and Twisted test setups.

    Crawler.crawl_async() would replace the engine with its own one, losing
    the patched timer, so the engine is driven directly here.
    """
    engine = crawler.engine

    async def run() -> None:
        await engine.open_spider_async()
        if engine.state is EngineState.SPIDER_OPEN:
            await engine.start_async()

    return deferred_from_coro(run())


def make_crawler(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    *,
    grace_period: float = 0.05,
    spider_cls: type[Spider] = DefaultSpider,
    settings: dict[str, Any] | None = None,
) -> Crawler:
    settings = {
        "ENGINE_IDLE_GRACE_PERIOD": grace_period,
        "SCHEDULER": MemoryScheduler,
        **(settings or {}),
    }
    crawler = get_crawler(spider_cls, settings)
    engine = ExecutionEngine(crawler)
    monkeypatch.setattr("scrapy.core.engine.call_later", clock)
    crawler.engine = engine
    crawler.spider = crawler._create_spider()
    return crawler


async def _wait_until(
    condition: Callable[[], bool],
    *,
    iterations: int = 1000,
) -> None:
    """Let the event loop run scheduled callbacks until *condition* holds."""
    for _ in range(iterations):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was never met")


class TestIdleGracePeriodIntegration:
    """End-to-end behavior through the public Crawler API with the real
    event-loop timer."""

    @coroutine_test
    async def test_late_request_from_idle_handler_is_processed(self) -> None:
        """A request scheduled by a spider_idle handler during the grace period
        is downloaded instead of the spider closing early."""
        scheduled: list[bool] = []

        def spider_idle(**kwargs: Any) -> None:
            # Schedule exactly one request on the first idle observation, while
            # the grace period is counting down, then let the spider finish.
            if not scheduled:
                scheduled.append(True)
                crawler.engine.crawl(Request("data:,late"))

        crawler = get_crawler(
            DefaultSpider,
            {
                "ENGINE_IDLE_GRACE_PERIOD": 0.3,
                "SCHEDULER": MemoryScheduler,
            },
        )
        crawler.signals.connect(spider_idle, signals.spider_idle)
        await crawler.crawl_async()

        engine = crawler.engine
        assert engine is not None
        assert engine.state is EngineState.STOPPED
        assert crawler.stats.get_value("downloader/request_count") == 1
        assert crawler.stats.get_value("finish_reason") == "finished"

    @coroutine_test
    async def test_grace_delays_finish_reason(self) -> None:
        """The crawl finishes with the default reason, but only after the grace
        period has elapsed while the spider stayed idle."""
        crawler = get_crawler(
            DefaultSpider,
            {
                "ENGINE_IDLE_GRACE_PERIOD": 0.1,
                "SCHEDULER": MemoryScheduler,
            },
        )
        start = time.monotonic()
        await crawler.crawl_async()
        elapsed = time.monotonic() - start

        engine = crawler.engine
        assert engine is not None
        assert engine.state is EngineState.STOPPED
        assert crawler.stats.get_value("finish_reason") == "finished"
        assert elapsed >= 0.1


class TestIdleGracePeriod:
    @coroutine_test
    async def test_default_setting_closes_immediately(self) -> None:
        """With the default grace period of 0, the spider closes straight from
        the idle observation, without any delayed call."""
        crawler = get_crawler(DefaultSpider)
        await crawler.crawl_async()
        engine = crawler.engine
        assert engine is not None
        assert engine._idle_grace_period == 0
        assert engine._idle_close_call is None
        assert engine.state is EngineState.STOPPED
        assert crawler.stats.get_value("finish_reason") == "finished"

    @pytest.mark.parametrize("value", [-1.0, -0.1, math.inf, math.nan])
    @coroutine_test
    async def test_invalid_setting(self, value: float) -> None:
        crawler = get_crawler(DefaultSpider, {"ENGINE_IDLE_GRACE_PERIOD": value})
        with pytest.raises(ValueError, match="ENGINE_IDLE_GRACE_PERIOD"):
            ExecutionEngine(crawler)

    @coroutine_test
    async def test_close_after_grace_expires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An idle spider stays open until the grace period expires, and closes
        once the delayed call fires and the components are re-checked."""
        clock = FakeClock()
        crawler = make_crawler(monkeypatch, clock)
        engine = crawler.engine

        crawl_coro = start_crawl(crawler)
        await _wait_until(
            lambda: engine.state is EngineState.RUNNING and bool(clock.pending)
        )
        assert engine.state is EngineState.RUNNING
        assert len(clock.pending) == 1

        await maybe_deferred_to_future(crawl_coro)

        assert_state(engine, EngineState.STOPPED)
        assert crawler.stats.get_value("finish_reason") == "finished"
        assert engine._idle_close_call is None
        assert clock.calls[0].fired

    @coroutine_test
    async def test_request_at_the_same_moment_cancels_close(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A request that reaches the engine while the grace period is
        counting down cancels the pending close: the scheduled request makes
        the spider non-idle before the delayed close can run.

        The request arrives through the same crawl() call an asynchronous
        Spider.start() or a spider_idle handler would make.
        """
        clock = FakeClock()
        # Long enough to act before the real timer fires.
        crawler = make_crawler(monkeypatch, clock, grace_period=0.3)
        engine = crawler.engine
        await engine.open_spider_async()

        crawl_coro = deferred_from_coro(engine.start_async())
        await _wait_until(lambda: bool(clock.pending))

        # The request reaches the engine before the delayed close fires.
        engine.crawl(Request("data:,same_moment"))
        assert not clock.pending  # the grace timer was canceled
        assert clock.calls[0].canceled

        await maybe_deferred_to_future(crawl_coro)

        assert engine.state is EngineState.STOPPED
        assert crawler.stats.get_value("downloader/request_count") >= 1
        assert crawler.stats.get_value("finish_reason") == "finished"

    @coroutine_test
    async def test_recheck_still_busy_at_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the spider is busy again when the grace timer fires, the close is
        not performed and a new observation is made later."""
        clock = FakeClock()
        # The first timer is canceled by the download starting, then a second
        # timer fires for real while nothing else is happening.
        crawler = make_crawler(monkeypatch, clock, grace_period=0.02)
        engine = crawler.engine

        crawl_coro = start_crawl(crawler)
        await _wait_until(lambda: bool(clock.pending))
        first_generation = engine._idle_generation

        # A download starts and keeps the spider busy past the first deadline.
        download = asyncio.create_task(engine.download_async(Request("data:,")))
        await asyncio.sleep(0)
        assert not clock.pending  # activity canceled the first timer
        canceled_generation = engine._idle_generation
        assert canceled_generation > first_generation

        response = await download
        assert response.status == 200

        # The engine is idle again and schedules a fresh grace period.
        await _wait_until(lambda: bool(clock.pending))
        assert engine._idle_generation > canceled_generation

        await maybe_deferred_to_future(crawl_coro)
        assert engine.state is EngineState.STOPPED

    @coroutine_test
    async def test_deadline_recheck_finds_busy_components(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A grace timer that fires while the downloader is busy does not close
        the spider: the deadline re-check finds active work and waits for the
        next idle observation."""
        clock = FakeClock()

        class BusyDownloader:
            """Reports an active request until the test releases it."""

            def __init__(self, crawler: Crawler) -> None:
                self.active: set[Request] = set()
                self._crawler = crawler

            def fetch(self, request: Request) -> Any:
                raise NotImplementedError

            def needs_backout(self) -> bool:
                return False

            def close(self) -> None:
                pass

        crawler = make_crawler(
            monkeypatch,
            clock,
            grace_period=0.05,
            settings={"DOWNLOADER": BusyDownloader},
        )
        engine = crawler.engine

        crawl_coro = start_crawl(crawler)
        await _wait_until(lambda: bool(clock.pending))
        # Pretend a request reached the downloader without the engine knowing.
        engine.downloader.active.add(Request("data:,hidden"))

        # Let the first deadline pass; the re-check must keep the spider open.
        await asyncio.sleep(0.1)
        assert engine.state is EngineState.RUNNING
        assert engine._idle_close_call is None  # fired, but did not close

        # Releasing the downloader lets the next observation close the spider.
        engine.downloader.active.clear()
        assert engine._slot is not None
        engine._slot.nextcall.schedule()
        await maybe_deferred_to_future(crawl_coro)
        assert_state(engine, EngineState.STOPPED)
        assert crawler.stats.get_value("finish_reason") == "finished"

    @coroutine_test
    async def test_recheck_closes_when_still_idle_at_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the timer fires and every component is still idle, the close is
        performed after the re-check, with the default idle reason."""
        clock = FakeClock()
        crawler = make_crawler(monkeypatch, clock, grace_period=0.02)
        engine = crawler.engine

        crawl_coro = start_crawl(crawler)
        await _wait_until(lambda: bool(clock.pending))
        call = clock.pending[0]

        await maybe_deferred_to_future(crawl_coro)
        assert call.fired
        assert engine.state is EngineState.STOPPED
        assert crawler.stats.get_value("finish_reason") == "finished"

    @coroutine_test
    async def test_repeated_idle_does_not_stack_closes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated idle observations while a grace period is counting down do
        not stack several delayed closes; the first deadline keeps counting."""
        clock = FakeClock()
        crawler = make_crawler(monkeypatch, clock, grace_period=0.3)
        engine = crawler.engine
        idle_calls: list[int] = []

        def spider_idle(**kwargs: Any) -> None:
            idle_calls.append(engine._idle_generation)
            # Wake the engine up again without adding work.
            assert engine._slot is not None
            engine._slot.nextcall.schedule()

        crawler.signals.connect(spider_idle, signals.spider_idle)

        crawl_coro = start_crawl(crawler)
        await _wait_until(lambda: len(clock.calls) >= 1 and len(idle_calls) >= 2)

        assert len(clock.pending) == 1  # a single delayed close
        assert len(clock.calls) == 1

        await engine.close_spider_async(reason="manual")
        await maybe_deferred_to_future(crawl_coro)
        assert engine.state is EngineState.STOPPED
        assert crawler.stats.get_value("finish_reason") == "manual"

    @coroutine_test
    async def test_explicit_close_not_delayed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit close_spider_async() cancels the grace timer and closes
        the spider right away with its own reason."""
        clock = FakeClock()
        crawler = make_crawler(monkeypatch, clock, grace_period=0.3)
        engine = crawler.engine

        crawl_coro = start_crawl(crawler)
        await _wait_until(lambda: bool(clock.pending))

        await engine.close_spider_async(reason="manual")
        await maybe_deferred_to_future(crawl_coro)

        assert not clock.pending
        assert clock.calls[0].canceled
        assert engine.state is EngineState.STOPPED
        assert crawler.stats.get_value("finish_reason") == "manual"

    @coroutine_test
    async def test_stop_not_delayed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stop_async() is not held back by a pending grace period."""
        clock = FakeClock()
        crawler = make_crawler(monkeypatch, clock, grace_period=0.3)
        engine = crawler.engine

        crawl_coro = start_crawl(crawler)
        await _wait_until(lambda: bool(clock.pending))

        await engine.stop_async()
        await maybe_deferred_to_future(crawl_coro)

        assert not clock.pending
        assert engine.state is EngineState.STOPPED
        assert crawler.stats.get_value("finish_reason") == "shutdown"

    @coroutine_test
    async def test_close_spider_from_idle_handler_bypasses_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A CloseSpider raised by a spider_idle handler is an explicit close
        and is not deferred by the grace period."""
        clock = FakeClock()

        def spider_idle(**kwargs: Any) -> None:
            raise CloseSpider("custom_reason")

        crawler = make_crawler(monkeypatch, clock, grace_period=0.3)
        engine = crawler.engine
        crawler.signals.connect(spider_idle, signals.spider_idle)

        await maybe_deferred_to_future(start_crawl(crawler))
        assert clock.calls == []  # no grace timer was scheduled
        assert engine.state is EngineState.STOPPED
        assert crawler.stats.get_value("finish_reason") == "custom_reason"

    @coroutine_test
    async def test_dont_close_spider_cancels_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DontCloseSpider cancels a pending grace period; a later idle
        observation without it starts a fresh one."""
        clock = FakeClock()
        idle_calls = 0

        def spider_idle(**kwargs: Any) -> None:
            nonlocal idle_calls
            idle_calls += 1
            if idle_calls == 1:
                crawler.engine.crawl(Request("data:,kept"))
                raise DontCloseSpider

        crawler = make_crawler(monkeypatch, clock, grace_period=0.3)
        engine = crawler.engine
        crawler.signals.connect(spider_idle, signals.spider_idle)

        crawl_coro = start_crawl(crawler)
        await _wait_until(lambda: idle_calls >= 1)
        assert not clock.pending  # DontCloseSpider canceled the timer

        # A fresh timer arms once the scheduled request has been processed.
        await _wait_until(lambda: bool(clock.pending))
        await maybe_deferred_to_future(crawl_coro)

        assert engine.state is EngineState.STOPPED
        assert crawler.stats.get_value("finish_reason") == "finished"
        assert crawler.stats.get_value("downloader/request_count") == 1

    @coroutine_test
    async def test_generation_guards_stale_timer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timer whose observation was superseded closes nothing when it
        fires: the generation it carries no longer matches the engine's."""
        clock = FakeClock()
        crawler = make_crawler(monkeypatch, clock, grace_period=0.3)
        engine = crawler.engine

        crawl_coro = start_crawl(crawler)
        await _wait_until(lambda: bool(clock.pending))
        stale_generation = engine._idle_generation

        engine.crawl(Request("data:,new"))
        assert not clock.pending
        assert engine._idle_generation != stale_generation
        assert engine.state is EngineState.RUNNING

        # The crawl still finishes on its own, with a fresh grace period.
        await maybe_deferred_to_future(crawl_coro)
        assert_state(engine, EngineState.STOPPED)
        assert crawler.stats.get_value("finish_reason") == "finished"

    @coroutine_test
    async def test_timer_after_stop_is_a_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A grace timer that outlives an explicit close (its handle could not
        be canceled) does nothing when it fires on a stopped engine."""
        clock = FakeClock()
        crawler = make_crawler(monkeypatch, clock, grace_period=0.3)
        engine = crawler.engine

        crawl_coro = start_crawl(crawler)
        await _wait_until(lambda: bool(clock.pending))
        stale_call = clock.pending[0]

        # Simulate a close whose cancel did not reach the timer, leaving it
        # scheduled, then let it fire after the engine has fully stopped.
        await engine.close_spider_async(reason="manual")
        await maybe_deferred_to_future(crawl_coro)
        assert_state(engine, EngineState.STOPPED)

        stale_call.active = True
        stale_call.run()
        assert_state(engine, EngineState.STOPPED)
        assert crawler.stats.get_value("finish_reason") == "manual"

    @coroutine_test
    async def test_pause_cancels_and_unpause_rearms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pausing cancels the grace timer; unpausing resumes the observation
        and schedules a new timer once the spider is idle again."""
        clock = FakeClock()
        crawler = make_crawler(monkeypatch, clock, grace_period=0.3)
        engine = crawler.engine

        crawl_coro = start_crawl(crawler)
        await _wait_until(lambda: bool(clock.pending))

        engine.pause()
        assert not clock.pending
        assert engine.paused

        engine.unpause()
        await _wait_until(lambda: bool(clock.pending))
        assert len(clock.pending) == 1

        await maybe_deferred_to_future(crawl_coro)
        assert engine.state is EngineState.STOPPED

    @coroutine_test
    async def test_item_activity_cancels_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An item going through the pipeline while the grace period is
        counting down cancels the close until the item has been processed:
        the next engine tick observes the scraper is no longer idle."""
        clock = FakeClock()
        crawler = make_crawler(monkeypatch, clock, grace_period=0.3)
        engine = crawler.engine

        crawl_coro = start_crawl(crawler)
        await _wait_until(lambda: bool(clock.pending))
        assert engine.scraper.slot is not None
        assert engine.scraper.slot.is_idle()

        engine.scraper._start_itemproc_nowait({"field": "value"})
        assert engine.scraper.slot.itemproc_size == 1
        # The next engine ticks see the busy scraper and cancel the timer.
        assert engine._slot is not None
        engine._slot.nextcall.schedule()
        await _wait_until(lambda: not clock.pending)

        # A fresh timer arms once the item pipeline is done, and closes the
        # spider after it fires.
        await maybe_deferred_to_future(crawl_coro)
        assert engine.state is EngineState.STOPPED
        assert crawler.stats.get_value("item_scraped_count") == 1
