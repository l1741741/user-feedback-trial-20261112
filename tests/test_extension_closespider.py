from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest
from twisted.python.failure import Failure

from scrapy import Request, Spider, signals
from scrapy.extensions import closespider as closespider_mod
from scrapy.extensions.closespider import CloseSpider
from scrapy.http import Response
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Generator

    from scrapy.core.engine import ExecutionEngine
    from scrapy.crawler import Crawler


class _FakeTimer:
    """A delayed call whose firing the test controls."""

    def __init__(self, delay: float, func: Callable[..., None], args: tuple[Any, ...]):
        self.delay: float = delay
        self.func: Callable[..., None] = func
        self.args: tuple[Any, ...] = args
        self.cancels: int = 0

    def cancel(self) -> None:
        self.cancels += 1

    def fire(self) -> None:
        """Run the callback even if it was cancelled, as can happen when the
        event loop had already queued it before the cancellation."""
        self.func(*self.args)


class _FakeClock:
    def __init__(self) -> None:
        self.timers: list[_FakeTimer] = []

    def call_later(self, delay: float, func: Callable[..., None], *args: Any) -> Any:
        timer = _FakeTimer(delay, func, args)
        self.timers.append(timer)
        return timer


class _FakeLoopingCall:
    """A looping call whose ticks the test controls."""

    def __init__(self, func: Callable[[], None]):
        self.func: Callable[[], None] = func
        self.running: bool = False
        self.starts: list[tuple[float, bool]] = []
        self.stops: int = 0

    def start(self, interval: float, now: bool = True) -> None:
        self.running = True
        self.starts.append((interval, now))

    def stop(self) -> None:
        self.running = False
        self.stops += 1

    def tick(self) -> None:
        """Run the callback even if it was stopped, as can happen when the
        event loop had already queued it before the stop."""
        self.func()


class _FakeLoopingCallFactory:
    def __init__(self) -> None:
        self.calls: list[_FakeLoopingCall] = []

    def create(self, func: Callable[[], None]) -> Any:
        looping_call = _FakeLoopingCall(func)
        self.calls.append(looping_call)
        return looping_call

    @property
    def latest(self) -> _FakeLoopingCall:
        return self.calls[-1]


class _Scheduled:
    """Capture the coroutines that the extension schedules, instead of letting
    the event loop run them in the background."""

    def __init__(self) -> None:
        self.coros: list[Coroutine[Any, Any, None]] = []

    def __call__(self, coro: Coroutine[Any, Any, None]) -> None:
        self.coros.append(coro)

    async def run_next(self) -> None:
        assert len(self.coros) == 1
        await self.coros.pop(0)

    def close(self) -> None:
        for coro in self.coros:
            coro.close()
        self.coros.clear()


class _FakeEngine:
    """An engine whose close can be delayed or made to fail."""

    def __init__(self, *, exc: BaseException | None = None) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.exc: BaseException | None = exc
        self.gate: asyncio.Event | None = None

    async def close_spider_async(self, *, reason: str, error: bool = False) -> None:
        self.calls.append((reason, error))
        if self.gate is not None:
            await self.gate.wait()
        if self.exc is not None:
            raise self.exc


@dataclass
class _Setup:
    crawler: Crawler
    ext: CloseSpider
    engine: _FakeEngine
    clock: _FakeClock
    loops: _FakeLoopingCallFactory
    spider: Spider

    def signal(self, signal: object, **kwargs: Any) -> None:
        self.crawler.signals.send_catch_log(signal=signal, spider=self.spider, **kwargs)

    def opened(self) -> None:
        self.signal(signals.spider_opened)

    def closed(self, reason: str = "shutdown", error: bool = False) -> None:
        self.signal(signals.spider_closed, reason=reason, error=error)

    def page(self) -> None:
        request = Request("data:,")
        response = Response("data:,", request=request)
        self.signal(signals.response_received, response=response, request=request)

    def item(self) -> None:
        self.signal(signals.item_scraped, item={}, response=None)

    def error(self) -> None:
        self.signal(
            signals.spider_error, failure=Failure(ValueError("boom")), response=None
        )


def _setup(monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any]) -> _Setup:
    crawler = get_crawler(Spider, settings)
    engine = _FakeEngine()
    crawler.engine = cast("ExecutionEngine", engine)
    spider = Spider(name="test")
    crawler.spider = spider

    clock = _FakeClock()
    loops = _FakeLoopingCallFactory()
    monkeypatch.setattr(closespider_mod, "call_later", clock.call_later)
    monkeypatch.setattr(closespider_mod, "create_looping_call", loops.create)

    # get_crawler() has already built the extension from the crawler settings
    # and connected it to the signals.
    ext = crawler.get_extension(CloseSpider)
    assert ext is not None
    return _Setup(crawler, ext, engine, clock, loops, spider)


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch) -> Generator[_Scheduled]:
    """Capture scheduled close coroutines for the whole test, so that several
    crawlers in the same test do not overwrite each other's capture."""
    scheduled = _Scheduled()
    monkeypatch.setattr(closespider_mod, "_schedule_coro", scheduled)
    yield scheduled
    scheduled.close()


@coroutine_test
async def test_no_close_before_threshold(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    setup = _setup(monkeypatch, {"CLOSESPIDER_PAGECOUNT": 3})
    setup.page()
    setup.page()
    assert scheduler.coros == []
    assert setup.ext.close_reason is None


@coroutine_test
async def test_same_response_pagecount_wins(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    """Both page-based thresholds hit in one response: the page count wins,
    matching the order in which the handler checks them."""
    setup = _setup(
        monkeypatch,
        {
            "CLOSESPIDER_PAGECOUNT": 3,
            "CLOSESPIDER_PAGECOUNT_NO_ITEM": 3,
        },
    )
    for _ in range(3):
        setup.page()
    assert len(scheduler.coros) == 1
    assert setup.ext.close_reason == "closespider_pagecount"
    await scheduler.run_next()
    assert setup.engine.calls == [("closespider_pagecount", False)]


@coroutine_test
async def test_pagecount_no_item_wins_when_reached_first(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    setup = _setup(
        monkeypatch,
        {
            "CLOSESPIDER_PAGECOUNT": 10,
            "CLOSESPIDER_PAGECOUNT_NO_ITEM": 2,
        },
    )
    setup.page()
    setup.page()
    assert len(scheduler.coros) == 1
    assert setup.ext.close_reason == "closespider_pagecount_no_item"


@coroutine_test
async def test_item_resets_pagecount_no_item(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    setup = _setup(monkeypatch, {"CLOSESPIDER_PAGECOUNT_NO_ITEM": 3})
    setup.page()
    setup.page()
    setup.item()
    setup.page()
    setup.page()
    assert scheduler.coros == []
    setup.page()
    assert setup.ext.close_reason == "closespider_pagecount_no_item"


@coroutine_test
@pytest.mark.parametrize("first", ["pagecount", "itemcount"])
async def test_first_processed_condition_wins(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled, first: str
) -> None:
    """Signals arrive one after the other before the close coroutine runs; the
    condition processed first decides the reason."""
    setup = _setup(
        monkeypatch,
        {
            "CLOSESPIDER_PAGECOUNT": 2,
            "CLOSESPIDER_ITEMCOUNT": 2,
        },
    )
    if first == "pagecount":
        setup.page()
        setup.page()
        setup.item()
        setup.item()
        expected = "closespider_pagecount"
    else:
        setup.item()
        setup.item()
        setup.page()
        setup.page()
        expected = "closespider_itemcount"
    assert setup.ext.close_reason == expected
    assert len(scheduler.coros) == 1


@coroutine_test
@pytest.mark.parametrize("first", ["timeout", "itemcount"])
async def test_first_processed_condition_wins_with_timeout(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled, first: str
) -> None:
    setup = _setup(
        monkeypatch,
        {
            "CLOSESPIDER_TIMEOUT": 1.0,
            "CLOSESPIDER_ITEMCOUNT": 1,
        },
    )
    setup.opened()
    timer = setup.clock.timers[0]
    if first == "timeout":
        timer.fire()
        setup.item()
        assert setup.ext.close_reason == "closespider_timeout"
    else:
        setup.item()
        timer.fire()
        assert setup.ext.close_reason == "closespider_itemcount"
    assert len(scheduler.coros) == 1


@coroutine_test
@pytest.mark.only_asyncio
async def test_delayed_close_coroutine_keeps_first_reason(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    setup = _setup(
        monkeypatch,
        {
            "CLOSESPIDER_ITEMCOUNT": 1,
            "CLOSESPIDER_PAGECOUNT": 1,
            "CLOSESPIDER_ERRORCOUNT": 1,
        },
    )
    setup.engine.gate = asyncio.Event()
    setup.item()
    assert len(scheduler.coros) == 1

    # The coroutine is scheduled but cannot finish yet; other conditions are
    # still processed in the meantime.
    task = asyncio.create_task(scheduler.coros.pop())
    setup.page()
    setup.error()
    assert scheduler.coros == []
    assert setup.ext.close_reason == "closespider_itemcount"

    setup.engine.gate.set()
    await task
    assert setup.engine.calls == [("closespider_itemcount", False)]


@coroutine_test
async def test_timeout_configuration(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    setup = _setup(monkeypatch, {"CLOSESPIDER_TIMEOUT": 2.5})
    setup.opened()
    assert len(setup.clock.timers) == 1
    assert setup.clock.timers[0].delay == 2.5
    setup.clock.timers[0].fire()
    assert setup.ext.close_reason == "closespider_timeout"
    assert len(scheduler.coros) == 1


@coroutine_test
async def test_timeout_no_item_loop(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    setup = _setup(monkeypatch, {"CLOSESPIDER_TIMEOUT_NO_ITEM": 5})
    setup.opened()
    looping_call = setup.loops.latest
    assert looping_call.starts == [(5, False)]

    # Items in the period reset the counter instead of closing.
    setup.item()
    looping_call.tick()
    assert setup.ext.close_reason is None
    looping_call.tick()
    assert setup.ext.close_reason == "closespider_timeout_no_item"
    assert len(scheduler.coros) == 1


@coroutine_test
async def test_timeout_no_item_races_pagecount(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    """The no-item timer and a page-count threshold can be met without an
    intervening signal; the one processed first locks the reason."""
    setup = _setup(
        monkeypatch,
        {
            "CLOSESPIDER_TIMEOUT_NO_ITEM": 5,
            "CLOSESPIDER_PAGECOUNT": 1,
        },
    )
    setup.opened()
    looping_call = setup.loops.latest

    setup.page()
    assert setup.ext.close_reason == "closespider_pagecount"
    looping_call.tick()
    assert setup.ext.close_reason == "closespider_pagecount"
    assert len(scheduler.coros) == 1


@coroutine_test
async def test_timeout_no_item_races_pagecount_first_tick(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    setup = _setup(
        monkeypatch,
        {
            "CLOSESPIDER_TIMEOUT_NO_ITEM": 5,
            "CLOSESPIDER_PAGECOUNT": 1,
        },
    )
    setup.opened()
    looping_call = setup.loops.latest

    looping_call.tick()
    assert setup.ext.close_reason == "closespider_timeout_no_item"
    setup.page()
    assert setup.ext.close_reason == "closespider_timeout_no_item"
    assert len(scheduler.coros) == 1


@coroutine_test
async def test_pagecount_no_item_with_itemcount(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    """The item count and the page count without items are both configured:
    the item count is reset by items, and whichever threshold is reached
    first locks the reason."""
    setup = _setup(
        monkeypatch,
        {
            "CLOSESPIDER_ITEMCOUNT": 3,
            "CLOSESPIDER_PAGECOUNT_NO_ITEM": 2,
        },
    )
    setup.page()
    setup.page()
    assert setup.ext.close_reason == "closespider_pagecount_no_item"

    setup = _setup(
        monkeypatch,
        {
            "CLOSESPIDER_ITEMCOUNT": 2,
            "CLOSESPIDER_PAGECOUNT_NO_ITEM": 5,
        },
    )
    setup.item()
    setup.item()
    assert setup.ext.close_reason == "closespider_itemcount"
    assert len(scheduler.coros) == 2


@coroutine_test
async def test_errorcount_sets_error_flag(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    setup = _setup(monkeypatch, {"CLOSESPIDER_ERRORCOUNT": 2})
    setup.error()
    assert setup.ext.close_reason is None
    setup.error()
    assert setup.ext.close_reason == "closespider_errorcount"
    await scheduler.run_next()
    assert setup.engine.calls == [("closespider_errorcount", True)]


@coroutine_test
async def test_external_close_locks_queued_callbacks(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    """A close that did not come from this extension neither gets a made-up
    reason nor lets queued callbacks request a close later."""
    setup = _setup(
        monkeypatch,
        {
            "CLOSESPIDER_TIMEOUT": 1.0,
            "CLOSESPIDER_TIMEOUT_NO_ITEM": 1,
            "CLOSESPIDER_ITEMCOUNT": 1,
            "CLOSESPIDER_PAGECOUNT": 1,
            "CLOSESPIDER_ERRORCOUNT": 1,
        },
    )
    setup.opened()
    timer = setup.clock.timers[0]
    looping_call = setup.loops.latest

    setup.closed(reason="shutdown")

    # These callbacks were already queued when the spider closed.
    timer.fire()
    looping_call.tick()
    setup.page()
    setup.item()
    setup.error()

    assert scheduler.coros == []
    assert setup.ext.close_reason is None
    assert setup.ext.close_failure is None


@coroutine_test
async def test_scheduled_close_is_noop_after_external_close(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    setup = _setup(monkeypatch, {"CLOSESPIDER_ITEMCOUNT": 1})
    setup.item()
    assert len(scheduler.coros) == 1
    setup.closed(reason="shutdown")
    await scheduler.run_next()
    assert setup.engine.calls == []
    # The reason is kept, so it is still visible which condition asked for
    # the close, but the external close wins.
    assert setup.ext.close_reason == "closespider_itemcount"


@coroutine_test
async def test_second_spider_closed_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    setup = _setup(
        monkeypatch,
        {
            "CLOSESPIDER_TIMEOUT": 1.0,
            "CLOSESPIDER_TIMEOUT_NO_ITEM": 1,
        },
    )
    setup.opened()
    timer = setup.clock.timers[0]
    looping_call = setup.loops.latest

    setup.closed()
    setup.closed()
    setup.closed()

    assert timer.cancels == 1
    assert looping_call.stops == 1
    assert setup.ext.task is None
    assert setup.ext.task_no_item is None
    assert scheduler.coros == []


@coroutine_test
async def test_close_failure_is_logged_reported_and_latched(
    monkeypatch: pytest.MonkeyPatch,
    scheduler: _Scheduled,
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = _setup(monkeypatch, {"CLOSESPIDER_ITEMCOUNT": 1})
    setup.engine.exc = RuntimeError("engine is gone")
    reported: list[Failure] = []

    def on_spider_error(failure: Failure, response: Any, spider: Spider) -> None:
        reported.append(failure)

    # pydispatch keeps only a weak reference to signal handlers.
    setup.crawler.signals.connect(on_spider_error, signal=signals.spider_error)

    with caplog.at_level(logging.ERROR, logger="scrapy.extensions.closespider"):
        setup.item()
        await scheduler.run_next()

    assert setup.engine.calls == [("closespider_itemcount", False)]
    assert setup.ext.close_reason == "closespider_itemcount"
    assert isinstance(setup.ext.close_failure, Failure)
    assert isinstance(setup.ext.close_failure.value, RuntimeError)
    assert reported == [setup.ext.close_failure]
    assert "Error while closing the spider (closespider_itemcount)" in caplog.text

    # The latch stays set, so the failed close is not retried.
    setup.page()
    setup.item()
    setup.closed()
    assert scheduler.coros == []
    assert len(setup.engine.calls) == 1


@coroutine_test
@pytest.mark.only_asyncio
async def test_close_failure_is_not_an_unhandled_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(monkeypatch, {"CLOSESPIDER_ITEMCOUNT": 1})
    setup.engine.exc = RuntimeError("engine is gone")

    tasks: list[asyncio.Task[None]] = []
    real_create_task = asyncio.create_task

    def create_task(
        coro: Coroutine[Any, Any, None], **kwargs: Any
    ) -> asyncio.Task[None]:
        task = real_create_task(coro, **kwargs)
        tasks.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", create_task)

    loop = asyncio.get_running_loop()
    contexts: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    try:
        setup.item()
        assert len(tasks) == 1
        await tasks[0]
    finally:
        loop.set_exception_handler(previous_handler)

    assert not tasks[0].cancelled()
    assert tasks[0].exception() is None
    assert contexts == []
    assert isinstance(setup.ext.close_failure, Failure)


@coroutine_test
async def test_crawlers_are_independent(
    monkeypatch: pytest.MonkeyPatch, scheduler: _Scheduled
) -> None:
    first = _setup(monkeypatch, {"CLOSESPIDER_ITEMCOUNT": 1})
    second = _setup(monkeypatch, {"CLOSESPIDER_ITEMCOUNT": 1})

    first.item()
    assert first.ext.close_reason == "closespider_itemcount"
    assert second.ext.close_reason is None
    assert len(scheduler.coros) == 1

    first.closed()
    second.page()
    assert second.ext.close_reason is None
    assert len(scheduler.coros) == 1

    second.item()
    assert second.ext.close_reason == "closespider_itemcount"
    assert len(scheduler.coros) == 2
