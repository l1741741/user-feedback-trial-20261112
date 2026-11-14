from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import pytest

from scrapy._frame_disk_queue import FrameQueueError
from scrapy.core.scheduler import Scheduler
from scrapy.http import Request
from scrapy.spiders import Spider
from scrapy.utils.defer import ensure_awaitable
from scrapy.utils.misc import build_from_crawler
from tests.test_scheduler import MockCrawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


class _ProductSpider(Spider):
    name = "product"

    def parse_product(self, response: Any) -> None:
        pass


_PRIORITY_QUEUE = "scrapy.pqueues.ScrapyPriorityQueue"
_FRAME_DISK_QUEUES = [
    "scrapy.squeues.PickleFifoFrameDiskQueue",
    "scrapy.squeues.PickleLifoFrameDiskQueue",
]


def _product_request(spider: _ProductSpider, index: int) -> Request:
    return Request(
        f"https://example.com/product/{index}",
        callback=spider.parse_product,
        cb_kwargs={"page": index},
        meta={"batch": index % 2, "cursor": f"cursor-{index}"},
    )


def _bound_spider(scheduler: Scheduler) -> _ProductSpider:
    assert isinstance(scheduler.spider, _ProductSpider)
    return scheduler.spider


@asynccontextmanager
async def _frame_scheduler(
    tmp_path: Path, disk_queue: str
) -> AsyncGenerator[Scheduler]:
    crawler = MockCrawler(
        _PRIORITY_QUEUE,
        tmp_path,
        SCHEDULER_DISK_QUEUE=disk_queue,
        DUPEFILTER_CLASS="scrapy.dupefilters.BaseDupeFilter",
    )
    spider = _ProductSpider.from_crawler(crawler, name="product")
    # A real crawl binds the spider onto the crawler before the scheduler
    # opens, which is what lets the disk queues resolve request callbacks.
    crawler.spider = spider
    scheduler = build_from_crawler(Scheduler, crawler)
    await ensure_awaitable(scheduler.open(spider))
    try:
        yield scheduler
    finally:
        await ensure_awaitable(scheduler.close("finished"))
        await crawler.stop_async()
        crawler.engine.downloader.close()


@pytest.mark.parametrize("disk_queue", _FRAME_DISK_QUEUES)
@coroutine_test
async def test_scheduler_no_loss_or_replay_after_restart(
    tmp_path: Path, disk_queue: str
) -> None:
    total = 12
    # First process: enqueue everything, consume only part before stopping.
    async with _frame_scheduler(tmp_path, disk_queue) as first:
        spider = _bound_spider(first)
        for index in range(total):
            assert first.enqueue_request(_product_request(spider, index))

        consumed = []
        for _ in range(4):
            request = first.next_request()
            assert request is not None
            consumed.append(request.url)
        assert len(first) == total - 4

    # Second process resumes from the same JOBDIR.
    seen = set(consumed)
    async with _frame_scheduler(tmp_path, disk_queue) as second:
        assert len(second) == total - 4
        while second.has_pending_requests():
            request = second.next_request()
            assert request is not None
            # A URL handed out before the restart must never be replayed.
            assert request.url not in seen
            assert request.cb_kwargs["page"] >= 0
            assert request.meta["cursor"].startswith("cursor-")
            seen.add(request.url)

    assert len(seen) == total
    assert seen == {f"https://example.com/product/{index}" for index in range(total)}


@coroutine_test
async def test_scheduler_unserializable_request_keeps_disk_intact(
    tmp_path: Path,
) -> None:
    async with _frame_scheduler(tmp_path, _FRAME_DISK_QUEUES[0]) as scheduler:
        spider = _bound_spider(scheduler)
        assert scheduler.enqueue_request(_product_request(spider, 0))
        bad = Request(
            "https://example.com/unserializable",
            meta={"callable": lambda: None},
        )
        # The scheduler falls back to the memory queue instead of corrupting
        # the durable frame queue.
        assert scheduler.enqueue_request(bad)
        assert len(scheduler.dqs) == 1  # type: ignore[arg-type]

    async with _frame_scheduler(tmp_path, _FRAME_DISK_QUEUES[0]) as resumed:
        request = resumed.next_request()
        assert request is not None
        assert request.url == "https://example.com/product/0"
        assert request.cb_kwargs == {"page": 0}
        assert resumed.next_request() is None


@coroutine_test
async def test_scheduler_rejects_second_process(tmp_path: Path) -> None:
    async with _frame_scheduler(tmp_path, _FRAME_DISK_QUEUES[0]) as first:
        first.enqueue_request(
            Request(
                "https://example.com/1", callback=_bound_spider(first).parse_product
            )
        )
        crawler_two = MockCrawler(
            _PRIORITY_QUEUE,
            tmp_path,
            SCHEDULER_DISK_QUEUE=_FRAME_DISK_QUEUES[0],
            DUPEFILTER_CLASS="scrapy.dupefilters.BaseDupeFilter",
        )
        spider_two = _ProductSpider.from_crawler(crawler_two, name="product")
        crawler_two.spider = spider_two
        second = build_from_crawler(Scheduler, crawler_two)
        await ensure_awaitable(second.open(spider_two))
        try:
            # The frame queue directory (and its lock) is created lazily on the
            # first disk push, which is where a second process is rejected.
            with pytest.raises(FrameQueueError, match="Another crawl process"):
                second.enqueue_request(
                    Request(
                        "https://example.com/2",
                        callback=spider_two.parse_product,
                    )
                )
        finally:
            await ensure_awaitable(second.close("finished"))
        await crawler_two.stop_async()
        crawler_two.engine.downloader.close()
