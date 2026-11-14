from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from twisted.internet.defer import Deferred, fail, succeed

from scrapy import Request, Spider, signals
from scrapy.exceptions import DropItem, ScrapyDeprecationWarning
from scrapy.pipelines import ItemPipelineManager
from scrapy.utils.asyncio import call_later
from scrapy.utils.conf import build_component_list
from scrapy.utils.defer import deferred_to_future, maybe_deferred_to_future
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler, get_from_asyncio_queue
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from scrapy.crawler import Crawler
    from tests.mockserver.http import MockServer


class SimplePipeline:
    def process_item(self, item):
        item["pipeline_passed"] = True
        return item


class DeprecatedSpiderArgPipeline:
    def open_spider(self, spider):
        pass

    def close_spider(self, spider):
        pass

    def process_item(self, item, spider):
        item["pipeline_passed"] = True
        return item


class DeferredPipeline:
    def cb(self, item):
        item["pipeline_passed"] = True
        return item

    def open_spider(self):
        return succeed(None)

    def close_spider(self):
        return succeed(None)

    def process_item(self, item):
        d: Deferred[Any] = Deferred()
        d.addCallback(self.cb)
        d.callback(item)
        return d


class AsyncDefPipeline:
    async def process_item(self, item):
        d: Deferred[Any] = Deferred()
        call_later(0, d.callback, None)
        await maybe_deferred_to_future(d)
        item["pipeline_passed"] = True
        return item


class AsyncDefAsyncioPipeline:
    async def process_item(self, item):
        d: Deferred[Any] = Deferred()
        loop = asyncio.get_running_loop()
        loop.call_later(0, d.callback, None)
        await deferred_to_future(d)
        await asyncio.sleep(0.2)
        item["pipeline_passed"] = await get_from_asyncio_queue(True)
        return item


class AsyncDefNotAsyncioPipeline:
    async def process_item(self, item):
        d1: Deferred[Any] = Deferred()
        from twisted.internet import reactor

        reactor.callLater(0, d1.callback, None)
        await d1
        d2: Deferred[Any] = Deferred()
        reactor.callLater(0, d2.callback, None)
        await maybe_deferred_to_future(d2)
        item["pipeline_passed"] = True
        return item


class ProcessItemExceptionPipeline:
    def process_item(self, item):
        raise ValueError("process_item error")


class ProcessItemExceptionDeferredPipeline:
    def process_item(self, item):
        return fail(ValueError("process_item error"))


class ProcessItemExceptionAsyncPipeline:
    async def process_item(self, item):
        raise ValueError("process_item error")


class OpenSpiderExceptionPipeline:
    def open_spider(self):
        raise ValueError("open_spider error")


class OpenSpiderExceptionDeferredPipeline:
    def open_spider(self):
        return fail(ValueError("open_spider error"))


class OpenSpiderExceptionAsyncPipeline:
    async def open_spider(self):
        raise ValueError("open_spider error")


class ItemSpider(Spider):
    name = "itemspider"

    mockserver: MockServer

    async def start(self):
        yield Request(self.mockserver.url("/status?n=200"))

    def parse(self, response):
        return {"field": 42}


class TestPipeline:
    def _on_item_scraped(self, item: dict[str, Any]) -> None:
        assert isinstance(item, dict)
        assert item.get("pipeline_passed")
        self.items.append(item)

    def _create_crawler(self, pipeline_class: type) -> Crawler:
        settings = {
            "ITEM_PIPELINES": {pipeline_class: 1},
        }
        crawler = get_crawler(ItemSpider, settings)
        crawler.signals.connect(self._on_item_scraped, signals.item_scraped)
        self.items: list[Any] = []
        return crawler

    @pytest.mark.parametrize(
        "pipeline_class",
        [
            SimplePipeline,
            AsyncDefPipeline,
            pytest.param(AsyncDefAsyncioPipeline, marks=pytest.mark.only_asyncio),
            pytest.param(
                AsyncDefNotAsyncioPipeline, marks=pytest.mark.only_not_asyncio
            ),
        ],
    )
    @coroutine_test
    async def test_pipeline(self, mockserver: MockServer, pipeline_class: type) -> None:
        crawler = self._create_crawler(pipeline_class)
        await crawler.crawl_async(mockserver=mockserver)
        assert len(self.items) == 1

    @coroutine_test
    async def test_pipeline_deferred(self, mockserver: MockServer) -> None:
        crawler = self._create_crawler(DeferredPipeline)
        with (
            pytest.warns(
                ScrapyDeprecationWarning,
                match="DeferredPipeline.open_spider returned a Deferred",
            ),
            pytest.warns(
                ScrapyDeprecationWarning,
                match="DeferredPipeline.close_spider returned a Deferred",
            ),
            pytest.warns(
                ScrapyDeprecationWarning,
                match="DeferredPipeline.process_item returned a Deferred",
            ),
        ):
            await crawler.crawl_async(mockserver=mockserver)
        assert len(self.items) == 1

    @coroutine_test
    async def test_deprecated_spider_arg(self, mockserver: MockServer) -> None:
        crawler = self._create_crawler(DeprecatedSpiderArgPipeline)
        with (
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"DeprecatedSpiderArgPipeline.open_spider\(\) requires a spider argument",
            ),
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"DeprecatedSpiderArgPipeline.close_spider\(\) requires a spider argument",
            ),
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"DeprecatedSpiderArgPipeline.process_item\(\) requires a spider argument",
            ),
        ):
            await crawler.crawl_async(mockserver=mockserver)

        assert len(self.items) == 1

    @pytest.mark.parametrize(
        "pipeline_class",
        [
            ProcessItemExceptionPipeline,
            pytest.param(
                ProcessItemExceptionDeferredPipeline,
                marks=pytest.mark.filterwarnings(
                    "ignore::scrapy.exceptions.ScrapyDeprecationWarning"
                ),
            ),
            ProcessItemExceptionAsyncPipeline,
        ],
    )
    @coroutine_test
    async def test_process_item_exception(
        self,
        caplog: pytest.LogCaptureFixture,
        mockserver: MockServer,
        pipeline_class: type,
    ) -> None:
        crawler = self._create_crawler(pipeline_class)
        await crawler.crawl_async(mockserver=mockserver)
        assert "Error processing {'field': 42}" in caplog.text
        assert "process_item error" in caplog.text

    @pytest.mark.parametrize(
        "pipeline_class",
        [
            OpenSpiderExceptionPipeline,
            pytest.param(
                OpenSpiderExceptionDeferredPipeline,
                marks=pytest.mark.filterwarnings(
                    "ignore::scrapy.exceptions.ScrapyDeprecationWarning"
                ),
            ),
            OpenSpiderExceptionAsyncPipeline,
        ],
    )
    @coroutine_test
    async def test_open_spider_exception(
        self, mockserver: MockServer, pipeline_class: type
    ) -> None:
        crawler = self._create_crawler(pipeline_class)
        with pytest.raises(ValueError, match="open_spider error"):
            await crawler.crawl_async(mockserver=mockserver)


class TestCustomPipelineManager:
    @coroutine_test
    async def test_deprecated_process_item_spider_arg(self) -> None:
        class CustomPipelineManager(ItemPipelineManager):
            def process_item(self, item: Any, spider: Spider) -> Deferred[Any]:  # pylint: disable=useless-parent-delegation
                return super().process_item(item, spider)

        crawler = get_crawler(DefaultSpider)
        crawler.spider = crawler._create_spider()
        itemproc = build_from_crawler(CustomPipelineManager, crawler)
        with pytest.warns(
            ScrapyDeprecationWarning,
            match=r"CustomPipelineManager.process_item\(\) is deprecated, use process_item_async\(\)",
        ):
            itemproc.process_item({}, crawler.spider)

    @coroutine_test
    async def test_integration_recommended(self, mockserver: MockServer) -> None:
        class CustomPipelineManager(ItemPipelineManager):
            async def process_item_async(self, item):
                return await super().process_item_async(item)

        items = []

        def _on_item_scraped(item: dict[str, Any]) -> None:
            assert isinstance(item, dict)
            assert item.get("pipeline_passed")
            items.append(item)

        crawler = get_crawler(
            ItemSpider,
            {
                "ITEM_PROCESSOR": CustomPipelineManager,
                "ITEM_PIPELINES": {SimplePipeline: 1},
            },
        )
        crawler.spider = crawler._create_spider()
        crawler.signals.connect(_on_item_scraped, signals.item_scraped)
        await crawler.crawl_async(mockserver=mockserver)

        assert len(items) == 1

    @coroutine_test
    async def test_integration_no_async_subclass(self, mockserver: MockServer) -> None:
        class CustomPipelineManager(ItemPipelineManager):
            def open_spider(self, spider):
                with pytest.warns(
                    ScrapyDeprecationWarning,
                    match=r"CustomPipelineManager.open_spider\(\) is deprecated, use open_spider_async\(\)",
                ):
                    return super().open_spider(spider)

            def close_spider(self, spider):
                with pytest.warns(
                    ScrapyDeprecationWarning,
                    match=r"CustomPipelineManager.close_spider\(\) is deprecated, use close_spider_async\(\)",
                ):
                    return super().close_spider(spider)

            def process_item(self, item, spider):
                with pytest.warns(
                    ScrapyDeprecationWarning,
                    match=r"CustomPipelineManager.process_item\(\) is deprecated, use process_item_async\(\)",
                ):
                    return super().process_item(item, spider)

        items = []

        def _on_item_scraped(item: dict[str, Any]) -> None:
            assert isinstance(item, dict)
            assert item.get("pipeline_passed")
            items.append(item)

        crawler = get_crawler(
            ItemSpider,
            {
                "ITEM_PROCESSOR": CustomPipelineManager,
                "ITEM_PIPELINES": {SimplePipeline: 1},
            },
        )
        crawler.spider = crawler._create_spider()
        crawler.signals.connect(_on_item_scraped, signals.item_scraped)
        with (
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"CustomPipelineManager overrides open_spider\(\) but doesn't override open_spider_async\(\)",
            ),
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"CustomPipelineManager overrides close_spider\(\) but doesn't override close_spider_async\(\)",
            ),
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"CustomPipelineManager overrides process_item\(\) but doesn't override process_item_async\(\)",
            ),
        ):
            await crawler.crawl_async(mockserver=mockserver)

        assert len(items) == 1

    @coroutine_test
    async def test_integration_no_async_not_subclass(
        self, mockserver: MockServer
    ) -> None:
        class CustomPipelineManager:
            def __init__(self, crawler):
                self.pipelines = [
                    p()
                    for p in build_component_list(
                        crawler.settings.getwithbase("ITEM_PIPELINES")
                    )
                ]

            @classmethod
            def from_crawler(cls, crawler):
                return cls(crawler)

            def open_spider(self, spider):
                return succeed(None)

            def close_spider(self, spider):
                return succeed(None)

            def process_item(self, item, spider):
                for pipeline in self.pipelines:
                    item = pipeline.process_item(item)
                return succeed(item)

        items = []

        def _on_item_scraped(item: dict[str, Any]) -> None:
            assert isinstance(item, dict)
            assert item.get("pipeline_passed")
            items.append(item)

        crawler = get_crawler(
            ItemSpider,
            {
                "ITEM_PROCESSOR": CustomPipelineManager,
                "ITEM_PIPELINES": {SimplePipeline: 1},
            },
        )
        crawler.spider = crawler._create_spider()
        crawler.signals.connect(_on_item_scraped, signals.item_scraped)
        with (
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"CustomPipelineManager doesn't define a open_spider_async\(\) method",
            ),
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"CustomPipelineManager doesn't define a close_spider_async\(\) method",
            ),
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"CustomPipelineManager doesn't define a process_item_async\(\) method",
            ),
        ):
            await crawler.crawl_async(mockserver=mockserver)

        assert len(items) == 1


class GatedPipeline:
    """Blocks each item on a future that the test fires explicitly."""

    def __init__(self) -> None:
        self.entered: list[Any] = []
        self.gates: dict[Any, asyncio.Future[None]] = {}
        self.closed = False

    async def process_item(self, item):
        self.entered.append(item)
        gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.gates[item] = gate
        await gate
        return item

    def close_spider(self):
        self.closed = True


class SyncRaisePipeline:
    def process_item(self, item):
        if item == "bad":
            raise ValueError("process_item error")
        return item


class AsyncRaisePipeline:
    async def process_item(self, item):
        if item == "bad":
            raise ValueError("process_item error")
        return item


class DeferredFailPipeline:
    def process_item(self, item):
        if item == "bad":
            return fail(ValueError("process_item error"))
        return item


class DropItemPipeline:
    def process_item(self, item):
        if item == "bad":
            raise DropItem("dropped")
        return item


class CloseRaisePipeline(GatedPipeline):
    def close_spider(self):
        super().close_spider()
        raise ValueError("close_spider error")


async def _tick(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


class TestItemPipelineMaxPending:
    def _build_manager(
        self,
        pipelines: dict[Any, int],
        max_pending: Any = None,
    ) -> ItemPipelineManager:
        settings: dict[str, Any] = {"ITEM_PIPELINES": pipelines}
        if max_pending is not None:
            settings["ITEM_PIPELINE_MAX_PENDING"] = max_pending
        crawler = get_crawler(DefaultSpider, settings)
        crawler.spider = crawler._create_spider()
        return build_from_crawler(ItemPipelineManager, crawler)

    @coroutine_test
    async def test_limit_and_fifo(self) -> None:
        manager = self._build_manager({GatedPipeline: 1}, max_pending=2)
        pipeline = manager.middlewares[0]
        assert isinstance(pipeline, GatedPipeline)
        tasks = [
            asyncio.ensure_future(manager.process_item_async(i)) for i in range(4)
        ]
        await _tick()
        assert pipeline.entered == [0, 1]
        pipeline.gates[0].set_result(None)
        await _tick()
        assert pipeline.entered == [0, 1, 2]
        pipeline.gates[1].set_result(None)
        await _tick()
        assert pipeline.entered == [0, 1, 2, 3]
        pipeline.gates[2].set_result(None)
        pipeline.gates[3].set_result(None)
        assert await asyncio.gather(*tasks) == [0, 1, 2, 3]

    @coroutine_test
    async def test_unlimited_by_default(self) -> None:
        manager = self._build_manager({GatedPipeline: 1})
        pipeline = manager.middlewares[0]
        assert isinstance(pipeline, GatedPipeline)
        tasks = [
            asyncio.ensure_future(manager.process_item_async(i)) for i in range(5)
        ]
        await _tick()
        assert pipeline.entered == [0, 1, 2, 3, 4]
        for gate in pipeline.gates.values():
            gate.set_result(None)
        await asyncio.gather(*tasks)

    @coroutine_test
    async def test_waiter_cancelled(self) -> None:
        """Cancelling a queued item removes only that item and frees no slot."""
        manager = self._build_manager({GatedPipeline: 1}, max_pending=1)
        pipeline = manager.middlewares[0]
        assert isinstance(pipeline, GatedPipeline)
        tasks = [
            asyncio.ensure_future(manager.process_item_async(i)) for i in range(3)
        ]
        await _tick()
        assert pipeline.entered == [0]
        tasks[2].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[2]
        pipeline.gates[0].set_result(None)
        await _tick()
        assert pipeline.entered == [0, 1]
        pipeline.gates[1].set_result(None)
        assert await tasks[0] == 0
        assert await tasks[1] == 1
        # no slot leaked: a new item is processed immediately
        task = asyncio.ensure_future(manager.process_item_async(3))
        await _tick()
        assert pipeline.entered == [0, 1, 3]
        pipeline.gates[3].set_result(None)
        assert await task == 3

    @coroutine_test
    async def test_waiter_cancelled_before_release(self) -> None:
        """A waiter cancelled before a release runs is skipped by it."""
        manager = self._build_manager({GatedPipeline: 1}, max_pending=1)
        pipeline = manager.middlewares[0]
        assert isinstance(pipeline, GatedPipeline)
        tasks = [
            asyncio.ensure_future(manager.process_item_async(i)) for i in range(3)
        ]
        await _tick()
        assert pipeline.entered == [0]
        pipeline.gates[0].set_result(None)
        tasks[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[1]
        await _tick()
        assert pipeline.entered == [0, 2]
        pipeline.gates[2].set_result(None)
        assert await tasks[0] == 0
        assert await tasks[2] == 2

    @coroutine_test
    async def test_granted_waiter_cancelled(self) -> None:
        """Cancelling a queued item right as its slot is granted hands the
        slot to the next waiter instead of losing it."""
        manager = self._build_manager({GatedPipeline: 1}, max_pending=1)
        pipeline = manager.middlewares[0]
        assert isinstance(pipeline, GatedPipeline)
        task0 = asyncio.ensure_future(manager.process_item_async(0))
        task1 = asyncio.ensure_future(manager.process_item_async(1))
        await _tick()
        assert pipeline.entered == [0]
        pipeline.gates[0].set_result(None)
        # let item 0 finish and grant its slot to the queued item 1, then
        # cancel item 1 before its coroutine resumes
        await asyncio.sleep(0)
        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1
        assert await task0 == 0
        assert pipeline.entered == [0]
        # the granted slot was released: a new item is processed immediately
        task2 = asyncio.ensure_future(manager.process_item_async(2))
        await _tick()
        assert pipeline.entered == [0, 2]
        pipeline.gates[2].set_result(None)
        assert await task2 == 2

    @coroutine_test
    async def test_item_cancelled_in_chain(self) -> None:
        """Cancelling an item inside the pipeline releases its slot."""
        manager = self._build_manager({GatedPipeline: 1}, max_pending=1)
        pipeline = manager.middlewares[0]
        assert isinstance(pipeline, GatedPipeline)
        task0 = asyncio.ensure_future(manager.process_item_async(0))
        await _tick()
        assert pipeline.entered == [0]
        task0.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task0
        task1 = asyncio.ensure_future(manager.process_item_async(1))
        await _tick()
        assert pipeline.entered == [0, 1]
        pipeline.gates[1].set_result(None)
        assert await task1 == 1

    @pytest.mark.parametrize(
        ("pipeline_class", "exception"),
        [
            (SyncRaisePipeline, ValueError),
            (AsyncRaisePipeline, ValueError),
            pytest.param(
                DeferredFailPipeline,
                ValueError,
                marks=pytest.mark.filterwarnings(
                    "ignore::scrapy.exceptions.ScrapyDeprecationWarning"
                ),
            ),
            (DropItemPipeline, DropItem),
        ],
    )
    @coroutine_test
    async def test_exception_releases_exactly_one_slot(
        self, pipeline_class: type, exception: type[Exception]
    ) -> None:
        """A pipeline step failing, synchronously or asynchronously, releases
        the item's slot exactly once."""
        manager = self._build_manager(
            {pipeline_class: 1, GatedPipeline: 2}, max_pending=1
        )
        gated = manager.middlewares[1]
        assert isinstance(gated, GatedPipeline)
        with pytest.raises(exception):
            await manager.process_item_async("bad")
        tasks = [
            asyncio.ensure_future(manager.process_item_async("good1")),
            asyncio.ensure_future(manager.process_item_async("good2")),
        ]
        await _tick()
        # had the slot been released twice, both items would have entered
        assert gated.entered == ["good1"]
        gated.gates["good1"].set_result(None)
        await _tick()
        assert gated.entered == ["good1", "good2"]
        gated.gates["good2"].set_result(None)
        assert await asyncio.gather(*tasks) == ["good1", "good2"]

    @coroutine_test
    async def test_deferred_pipeline(self) -> None:
        """Deferred-returning pipeline steps work under the limit."""
        manager = self._build_manager({DeferredPipeline: 1}, max_pending=1)
        with pytest.warns(
            ScrapyDeprecationWarning,
            match="DeferredPipeline.process_item returned a Deferred",
        ):
            for _ in range(2):
                item = await manager.process_item_async({})
                assert item["pipeline_passed"]

    @coroutine_test
    async def test_close(self) -> None:
        """Closing fails queued items without running them, waits for items
        in the pipeline and prevents new items from entering."""
        manager = self._build_manager({GatedPipeline: 1}, max_pending=1)
        pipeline = manager.middlewares[0]
        assert isinstance(pipeline, GatedPipeline)
        tasks = [
            asyncio.ensure_future(manager.process_item_async(i)) for i in range(3)
        ]
        await _tick()
        assert pipeline.entered == [0]
        close_task = asyncio.ensure_future(manager.close_spider_async())
        await _tick()
        for task in tasks[1:]:
            with pytest.raises(RuntimeError, match="closed before this item"):
                await task
        assert pipeline.entered == [0]
        assert not close_task.done()
        pipeline.gates[0].set_result(None)
        assert await tasks[0] == 0
        await close_task
        assert pipeline.closed
        with pytest.raises(RuntimeError, match="cannot process more items"):
            await manager.process_item_async(3)

    @coroutine_test
    async def test_close_propagates_pipeline_exception(self) -> None:
        """Closing waits for items in the pipeline and does not swallow
        exceptions raised by pipeline close_spider() methods."""
        manager = self._build_manager({CloseRaisePipeline: 1}, max_pending=1)
        pipeline = manager.middlewares[0]
        assert isinstance(pipeline, CloseRaisePipeline)
        task = asyncio.ensure_future(manager.process_item_async(0))
        await _tick()
        close_task = asyncio.ensure_future(manager.close_spider_async())
        await _tick()
        assert not close_task.done()
        pipeline.gates[0].set_result(None)
        assert await task == 0
        with pytest.raises(ValueError, match="close_spider error"):
            await close_task

    @coroutine_test
    async def test_crawlers_do_not_share_limit(self) -> None:
        managers = [
            self._build_manager({GatedPipeline: 1}, max_pending=1)
            for _ in range(2)
        ]
        pipelines = [m.middlewares[0] for m in managers]
        tasks = [
            asyncio.ensure_future(m.process_item_async(i))
            for i, m in enumerate(managers)
        ]
        await _tick()
        for i, pipeline in enumerate(pipelines):
            assert isinstance(pipeline, GatedPipeline)
            assert pipeline.entered == [i]
            pipeline.gates[i].set_result(None)
        assert await asyncio.gather(*tasks) == [0, 1]

    @pytest.mark.parametrize("value", [0, -1, "foo", []])
    @coroutine_test
    async def test_invalid_setting(self, value: Any) -> None:
        with pytest.raises(ValueError, match="ITEM_PIPELINE_MAX_PENDING"):
            self._build_manager({GatedPipeline: 1}, max_pending=value)

    @coroutine_test
    async def test_integration(self, mockserver: MockServer) -> None:
        crawler = get_crawler(
            ItemSpider,
            {
                "ITEM_PIPELINES": {SimplePipeline: 1},
                "ITEM_PIPELINE_MAX_PENDING": 1,
            },
        )
        items: list[Any] = []

        def _on_item_scraped(item: dict[str, Any]) -> None:
            items.append(item)

        crawler.signals.connect(_on_item_scraped, signals.item_scraped)
        await crawler.crawl_async(mockserver=mockserver)
        assert len(items) == 1
        assert items[0].get("pipeline_passed")


class TestMiddlewareManagerSpider:
    """Tests for the deprecated spider arg handling in MiddlewareManager.

    Here because MiddlewareManager doesn't have methods that could take a spider arg."""

    @pytest.fixture
    def crawler(self) -> Crawler:
        return get_crawler(Spider)

    @coroutine_test
    async def test_deprecated_spider_arg_no_crawler_spider(
        self, crawler: Crawler
    ) -> None:
        """Crawler is provided, but doesn't have a spider, the methods raise an exception.
        The instance passed to a deprecated method is ignored."""
        mwman = ItemPipelineManager(crawler=crawler)
        with (
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"DeprecatedSpiderArgPipeline.open_spider\(\) requires a spider argument",
            ),
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"DeprecatedSpiderArgPipeline.close_spider\(\) requires a spider argument",
            ),
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"DeprecatedSpiderArgPipeline.process_item\(\) requires a spider argument",
            ),
        ):
            mwman._add_middleware(DeprecatedSpiderArgPipeline())
        with (
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"ItemPipelineManager.open_spider\(\) is deprecated, use open_spider_async\(\) instead",
            ),
            pytest.raises(
                ValueError,
                match=r"ItemPipelineManager needs to access self\.crawler\.spider but it is None",
            ),
        ):
            await maybe_deferred_to_future(mwman.open_spider(DefaultSpider()))
        with pytest.raises(
            ValueError,
            match=r"ItemPipelineManager needs to access self\.crawler\.spider but it is None",
        ):
            await mwman.open_spider_async()
        with (
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"ItemPipelineManager.close_spider\(\) is deprecated, use close_spider_async\(\) instead",
            ),
            pytest.raises(
                ValueError,
                match=r"ItemPipelineManager needs to access self\.crawler\.spider but it is None",
            ),
        ):
            await maybe_deferred_to_future(mwman.close_spider(DefaultSpider()))
        with pytest.raises(
            ValueError,
            match=r"ItemPipelineManager needs to access self\.crawler\.spider but it is None",
        ):
            await mwman.close_spider_async()

    @coroutine_test
    async def test_deprecated_spider_arg_with_crawler(self, crawler: Crawler) -> None:
        """Crawler is provided and has a spider, works. The instance passed to a deprecated method
        is ignored, even if mismatched."""
        mwman = ItemPipelineManager(crawler=crawler)
        crawler.spider = crawler._create_spider("foo")
        with pytest.warns(
            ScrapyDeprecationWarning,
            match=r"ItemPipelineManager.open_spider\(\) is deprecated, use open_spider_async\(\) instead",
        ):
            await maybe_deferred_to_future(mwman.open_spider(DefaultSpider()))
        with pytest.warns(
            ScrapyDeprecationWarning,
            match=r"ItemPipelineManager.close_spider\(\) is deprecated, use close_spider_async\(\) instead",
        ):
            await maybe_deferred_to_future(mwman.close_spider(DefaultSpider()))

    @coroutine_test
    async def test_deprecated_spider_arg_without_crawler(self) -> None:
        """The first instance passed to a deprecated method is used. Mismatched ones raise an error."""
        with pytest.warns(
            ScrapyDeprecationWarning,
            match="was called without the crawler argument",
        ):
            mwman = ItemPipelineManager()
        spider = DefaultSpider()
        with pytest.warns(
            ScrapyDeprecationWarning,
            match=r"ItemPipelineManager.open_spider\(\) is deprecated, use open_spider_async\(\) instead",
        ):
            await maybe_deferred_to_future(mwman.open_spider(spider))
        with (
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"ItemPipelineManager.close_spider\(\) is deprecated, use close_spider_async\(\) instead",
            ),
            pytest.raises(
                RuntimeError, match="Different instances of Spider were passed"
            ),
        ):
            await maybe_deferred_to_future(mwman.close_spider(DefaultSpider()))
        with pytest.warns(
            ScrapyDeprecationWarning,
            match=r"ItemPipelineManager.close_spider\(\) is deprecated, use close_spider_async\(\) instead",
        ):
            await maybe_deferred_to_future(mwman.close_spider(spider))

    @coroutine_test
    async def test_no_spider_arg_without_crawler(self) -> None:
        """If no crawler and no spider arg, raise an error."""
        with pytest.warns(
            ScrapyDeprecationWarning,
            match="was called without the crawler argument",
        ):
            mwman = ItemPipelineManager()
        with (
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"DeprecatedSpiderArgPipeline.open_spider\(\) requires a spider argument",
            ),
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"DeprecatedSpiderArgPipeline.close_spider\(\) requires a spider argument",
            ),
            pytest.warns(
                ScrapyDeprecationWarning,
                match=r"DeprecatedSpiderArgPipeline.process_item\(\) requires a spider argument",
            ),
        ):
            mwman._add_middleware(DeprecatedSpiderArgPipeline())
        with (
            pytest.raises(
                ValueError,
                match="has no known Spider instance",
            ),
        ):
            await mwman.open_spider_async()
