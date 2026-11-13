from __future__ import annotations

import json
import warnings
from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import Mock

import pytest

from scrapy.core.downloader import Downloader
from scrapy.core.scheduler import Scheduler
from scrapy.crawler import Crawler
from scrapy.dupefilters import BaseDupeFilter
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import Request
from scrapy.pqueues import ScrapyPriorityQueue
from scrapy.spiders import Spider
from scrapy.utils.defer import ensure_awaitable
from scrapy.utils.misc import build_from_crawler, load_object
from scrapy.utils.test import get_crawler
from tests.mockserver.http import MockServer
from tests.utils.decorators import coroutine_test, inline_callbacks_test
from tests.utils.downloader import MockDownloader

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path


class MockCrawler(Crawler):
    def __init__(self, priority_queue_cls: str, jobdir: Path | None, **settings: Any):
        settings = {
            "SCHEDULER_DEBUG": False,
            "SCHEDULER_DISK_QUEUE": "scrapy.squeues.PickleLifoDiskQueue",
            "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.LifoMemoryQueue",
            "SCHEDULER_PRIORITY_QUEUE": priority_queue_cls,
            "JOBDIR": str(jobdir) if jobdir is not None else None,
            "DUPEFILTER_CLASS": "scrapy.dupefilters.BaseDupeFilter",
            **settings,
        }
        super().__init__(Spider, settings)
        self.engine = Mock(downloader=MockDownloader())
        self.stats = load_object(self.settings["STATS_CLASS"])(self)


@asynccontextmanager
async def create_scheduler(
    priority_queue_cls: str, jobdir: Path | None, **settings: Any
) -> AsyncGenerator[Scheduler]:
    mock_crawler = MockCrawler(priority_queue_cls, jobdir, **settings)
    scheduler = build_from_crawler(Scheduler, mock_crawler)
    spider = Spider.from_crawler(mock_crawler, name="spider")
    await ensure_awaitable(scheduler.open(spider))
    try:
        yield scheduler
    finally:
        await ensure_awaitable(scheduler.close("finished"))
        await mock_crawler.stop_async()
        mock_crawler.engine.downloader.close()


_PRIORITIES = [
    ("http://foo.com/a", -2),
    ("http://foo.com/d", 1),
    ("http://foo.com/b", -1),
    ("http://foo.com/c", 0),
    ("http://foo.com/e", 2),
]


_URLS = {"http://foo.com/a", "http://foo.com/b", "http://foo.com/c"}


def _active_json(jobdir: Path) -> Any:
    path = jobdir / "requests.queue" / "active.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _enqueue_across_queues(scheduler: Scheduler) -> None:
    for name, priority in (("memory", 0), ("disk", 1), ("disk", 0), ("memory", -1)):
        request = Request(f"https://example.com/{name}{priority}", priority=priority)
        if name == "memory":
            request.meta["unserializable"] = lambda: None
        scheduler.enqueue_request(request)


def _dequeue_urls(scheduler: Scheduler) -> list[str]:
    urls = []
    while request := scheduler.next_request():
        urls.append(request.url)
    return urls


def _enqueue_priorities(scheduler: Scheduler, requests_per_priority: dict[int, int]):
    """Enqueue the given number of requests at each (request) priority."""
    for priority, count in sorted(requests_per_priority.items()):
        for index in range(count):
            scheduler.enqueue_request(
                Request(
                    f"https://example.com/p{priority}-{index}",
                    priority=priority,
                )
            )


def _dequeue_priorities(scheduler: Scheduler) -> list[int]:
    priorities = []
    while request := scheduler.next_request():
        priorities.append(request.priority)
    return priorities


class TestSchedulerBase(ABC):
    reopen = False

    @property
    @abstractmethod
    def priority_queue_cls(self) -> str:
        raise NotImplementedError

    @pytest.fixture
    def jobdir(self) -> Path | None:
        return None

    def create_scheduler(
        self, jobdir: Path | None, **settings: Any
    ) -> AbstractAsyncContextManager[Scheduler]:
        return create_scheduler(self.priority_queue_cls, jobdir, **settings)

    @asynccontextmanager
    async def create_scheduler_for_assertions(
        self, jobdir: Path | None, setup: Callable[[Scheduler], None]
    ) -> AsyncGenerator[Scheduler]:
        if self.reopen:
            async with self.create_scheduler(jobdir) as scheduler:
                setup(scheduler)
            async with self.create_scheduler(jobdir) as scheduler:
                yield scheduler
        else:
            async with self.create_scheduler(jobdir) as scheduler:
                setup(scheduler)
                yield scheduler


class SchedulerTestMixin(TestSchedulerBase):
    @coroutine_test
    async def test_length(self, jobdir: Path | None) -> None:
        def _setup(scheduler: Scheduler) -> None:
            assert not scheduler.has_pending_requests()
            assert len(scheduler) == 0

            for url in _URLS:
                scheduler.enqueue_request(Request(url))

        async with self.create_scheduler_for_assertions(jobdir, _setup) as scheduler:
            assert scheduler.has_pending_requests()
            assert len(scheduler) == len(_URLS)

    @coroutine_test
    async def test_dequeue(self, jobdir: Path | None) -> None:
        def _setup(scheduler: Scheduler) -> None:
            for url in _URLS:
                scheduler.enqueue_request(Request(url))

        urls = set()
        async with self.create_scheduler_for_assertions(jobdir, _setup) as scheduler:
            while scheduler.has_pending_requests():
                request = scheduler.next_request()
                assert request is not None
                urls.add(request.url)

        assert urls == _URLS

    @coroutine_test
    async def test_dequeue_priorities(self, jobdir: Path | None) -> None:
        def _setup(scheduler: Scheduler) -> None:
            for url, priority in _PRIORITIES:
                scheduler.enqueue_request(Request(url, priority=priority))

        priorities = []
        async with self.create_scheduler_for_assertions(jobdir, _setup) as scheduler:
            while scheduler.has_pending_requests():
                request = scheduler.next_request()
                assert request is not None
                priorities.append(request.priority)

        assert priorities == sorted([x[1] for x in _PRIORITIES], key=lambda x: -x)


class TestSchedulerInMemoryBase(SchedulerTestMixin):
    pass


class TestSchedulerOnDiskBase(SchedulerTestMixin):
    reopen = True

    @pytest.fixture
    def jobdir(self, tmp_path: Path) -> Path | None:
        return tmp_path

    @coroutine_test
    async def test_dequeue_priorities_across_queues(self, jobdir: Path | None) -> None:
        async with self.create_scheduler(jobdir) as scheduler:
            _enqueue_across_queues(scheduler)
            assert _dequeue_urls(scheduler) == [
                "https://example.com/disk1",
                "https://example.com/memory0",
                "https://example.com/disk0",
                "https://example.com/memory-1",
            ]


class TestSchedulerInMemory(TestSchedulerInMemoryBase):
    priority_queue_cls = "scrapy.pqueues.ScrapyPriorityQueue"


class TestSchedulerOnDisk(TestSchedulerOnDiskBase):
    priority_queue_cls = "scrapy.pqueues.ScrapyPriorityQueue"

    @coroutine_test
    async def test_active_json(self, jobdir: Path | None) -> None:
        assert jobdir is not None
        path = jobdir / "requests.queue" / "active.json"
        async with self.create_scheduler(jobdir) as scheduler:
            for url, priority in _PRIORITIES:
                scheduler.enqueue_request(Request(url, priority=priority))
            assert not path.exists()
        assert _active_json(jobdir) == [-2, -1, 0, 1, 2]

    @coroutine_test
    async def test_active_json_sync_every(self, jobdir: Path | None) -> None:
        assert jobdir is not None
        async with self.create_scheduler(jobdir, JOBDIR_SYNC_EVERY=1) as scheduler:
            for url, priority in _PRIORITIES:
                scheduler.enqueue_request(Request(url, priority=priority))
            assert _active_json(jobdir) == [-2, -1, 0, 1, 2]
            scheduler.next_request()
            assert _active_json(jobdir) == [-1, 0, 1, 2]

    @coroutine_test
    async def test_active_json_sync_window(self, jobdir: Path | None) -> None:
        assert jobdir is not None
        path = jobdir / "requests.queue" / "active.json"
        async with self.create_scheduler(jobdir, JOBDIR_SYNC_EVERY=2) as scheduler:
            scheduler.enqueue_request(Request("http://foo.com/a", priority=-2))
            assert not path.exists()
            scheduler.enqueue_request(Request("http://foo.com/b", priority=1))
            assert _active_json(jobdir) == [-1, 2]
            scheduler.enqueue_request(Request("http://foo.com/c", priority=0))
            assert _active_json(jobdir) == [-1, 2]
            scheduler.enqueue_request(Request("http://foo.com/d", priority=1))
            assert _active_json(jobdir) == [-1, 2]
            scheduler.enqueue_request(Request("http://foo.com/e", priority=2))
            assert _active_json(jobdir) == [-2, -1, 0, 2]


_URLS_WITH_SLOTS = [
    ("http://foo.com/a", "a"),
    ("http://foo.com/b", "a"),
    ("http://foo.com/c", "b"),
    ("http://foo.com/d", "b"),
    ("http://foo.com/e", "c"),
    ("http://foo.com/f", "c"),
]


class FairnessTestMixin:
    """Bounded fairness tests shared by the memory and disk test classes."""

    @pytest.mark.parametrize("limit", [1, 2, 5])
    @coroutine_test
    async def test_fairness_interleaving(
        self, jobdir: Path | None, limit: int
    ) -> None:
        async with self.create_scheduler(
            jobdir, SCHEDULER_PRIORITY_FAIRNESS=limit
        ) as scheduler:
            _enqueue_priorities(scheduler, {10: 6, -10: 4})
            priorities = _dequeue_priorities(scheduler)

        assert len(priorities) == 10
        # While lower-priority requests remain, no more than ``limit`` high
        # priority requests come out in a row.
        run = 0
        lows_remaining = 4
        for priority in priorities:
            if priority == -10:
                run = 0
                lows_remaining -= 1
            elif lows_remaining:
                run += 1
                assert run <= limit
        # Every request is returned exactly once.
        assert priorities.count(10) == 6
        assert priorities.count(-10) == 4

    @coroutine_test
    async def test_fairness_limit_one_alternates(
        self, jobdir: Path | None
    ) -> None:
        async with self.create_scheduler(
            jobdir, SCHEDULER_PRIORITY_FAIRNESS=1
        ) as scheduler:
            _enqueue_priorities(scheduler, {10: 3, -10: 3})
            assert _dequeue_priorities(scheduler) == [10, -10, 10, -10, 10, -10]

    @coroutine_test
    async def test_fairness_keeps_fifo_order(
        self, jobdir: Path | None
    ) -> None:
        """Requests of the same priority keep the queue's FIFO/LIFO order."""
        async with self.create_scheduler(
            jobdir,
            SCHEDULER_PRIORITY_FAIRNESS=1,
            SCHEDULER_DISK_QUEUE="scrapy.squeues.PickleFifoDiskQueue",
            SCHEDULER_MEMORY_QUEUE="scrapy.squeues.FifoMemoryQueue",
        ) as scheduler:
            _enqueue_priorities(scheduler, {10: 3, -10: 3})
            urls = _dequeue_urls(scheduler)
        assert urls == [
            "https://example.com/p10-0",
            "https://example.com/p-10-0",
            "https://example.com/p10-1",
            "https://example.com/p-10-1",
            "https://example.com/p10-2",
            "https://example.com/p-10-2",
        ]

    @coroutine_test
    async def test_fairness_three_priorities(
        self, jobdir: Path | None
    ) -> None:
        """The lowest available priority is the one that gets a turn."""
        async with self.create_scheduler(
            jobdir, SCHEDULER_PRIORITY_FAIRNESS=1
        ) as scheduler:
            _enqueue_priorities(scheduler, {20: 2, 0: 2, -20: 2})
            priorities = _dequeue_priorities(scheduler)
        # After every high request, the lowest available priority is picked:
        # -20 first, then 0 once -20 is drained.
        assert priorities == [20, -20, 20, -20, 0, 0]

    @coroutine_test
    async def test_fairness_single_priority_unchanged(
        self, jobdir: Path | None
    ) -> None:
        async with self.create_scheduler(
            jobdir, SCHEDULER_PRIORITY_FAIRNESS=1
        ) as scheduler:
            _enqueue_priorities(scheduler, {0: 4})
            assert len(_dequeue_priorities(scheduler)) == 4

    @coroutine_test
    async def test_fairness_empty_queue(self, jobdir: Path | None) -> None:
        async with self.create_scheduler(
            jobdir, SCHEDULER_PRIORITY_FAIRNESS=1
        ) as scheduler:
            assert scheduler.next_request() is None
            assert not scheduler.has_pending_requests()

    @coroutine_test
    async def test_fairness_disabled_by_default(
        self, jobdir: Path | None
    ) -> None:
        async with self.create_scheduler(jobdir) as scheduler:
            _enqueue_priorities(scheduler, {10: 4, -10: 2})
            assert _dequeue_priorities(scheduler) == [10, 10, 10, 10, -10, -10]

    @coroutine_test
    async def test_fairness_limit_larger_than_queue(
        self, jobdir: Path | None
    ) -> None:
        """A limit above the number of high-priority requests never triggers."""
        async with self.create_scheduler(
            jobdir, SCHEDULER_PRIORITY_FAIRNESS=100
        ) as scheduler:
            _enqueue_priorities(scheduler, {10: 3, -10: 2})
            assert _dequeue_priorities(scheduler) == [10, 10, 10, -10, -10]

    @pytest.mark.parametrize("value", [-1, -100])
    @coroutine_test
    async def test_fairness_rejects_non_positive(
        self, jobdir: Path | None, value: int
    ) -> None:
        with pytest.raises(
            ValueError, match="SCHEDULER_PRIORITY_FAIRNESS must be a positive"
        ):
            async with self.create_scheduler(
                jobdir, SCHEDULER_PRIORITY_FAIRNESS=value
            ):
                pass


class TestFairnessInMemory(FairnessTestMixin):
    priority_queue_cls = "scrapy.pqueues.ScrapyPriorityQueue"

    @pytest.fixture
    def jobdir(self) -> Path | None:
        return None

    def create_scheduler(
        self, jobdir: Path | None, **settings: Any
    ) -> AbstractAsyncContextManager[Scheduler]:
        return create_scheduler(self.priority_queue_cls, jobdir, **settings)


class TestFairnessOnDisk(FairnessTestMixin):
    priority_queue_cls = "scrapy.pqueues.ScrapyPriorityQueue"

    @pytest.fixture
    def jobdir(self, tmp_path: Path) -> Path | None:
        return tmp_path

    def create_scheduler(
        self, jobdir: Path | None, **settings: Any
    ) -> AbstractAsyncContextManager[Scheduler]:
        return create_scheduler(self.priority_queue_cls, jobdir, **settings)

    @coroutine_test
    async def test_fairness_survives_restart(self, jobdir: Path) -> None:
        async with self.create_scheduler(
            jobdir, SCHEDULER_PRIORITY_FAIRNESS=2
        ) as scheduler:
            _enqueue_priorities(scheduler, {10: 5, -10: 3})
            first = [scheduler.next_request().priority for _ in range(4)]
            assert first == [10, 10, -10, 10]

        # The counter restarts, but every request survives exactly once.
        async with self.create_scheduler(
            jobdir, SCHEDULER_PRIORITY_FAIRNESS=2
        ) as scheduler:
            assert len(scheduler) == 4
            rest = _dequeue_priorities(scheduler)
        combined = first + rest
        assert combined.count(10) == 5
        assert combined.count(-10) == 3

    @coroutine_test
    async def test_fairness_mixed_memory_and_disk(
        self, jobdir: Path
    ) -> None:
        async with self.create_scheduler(
            jobdir, SCHEDULER_PRIORITY_FAIRNESS=2
        ) as scheduler:
            for index in range(4):
                request = Request(
                    f"https://example.com/memory-{index}", priority=10
                )
                request.meta["unserializable"] = lambda: None
                scheduler.enqueue_request(request)
            for index in range(3):
                scheduler.enqueue_request(
                    Request(f"https://example.com/disk-{index}", priority=-10)
                )
            priorities = _dequeue_priorities(scheduler)
        assert priorities.count(10) == 4
        assert priorities.count(-10) == 3
        run = 0
        lows_remaining = 3
        for priority in priorities:
            if priority == -10:
                run = 0
                lows_remaining -= 1
            elif lows_remaining:
                run += 1
                assert run <= 2


class TestFairnessDownloaderAware:
    priority_queue_cls = "scrapy.pqueues.DownloaderAwarePriorityQueue"

    @pytest.fixture
    def jobdir(self) -> Path | None:
        return None

    def create_scheduler(
        self, jobdir: Path | None, **settings: Any
    ) -> AbstractAsyncContextManager[Scheduler]:
        return create_scheduler(self.priority_queue_cls, jobdir, **settings)

    @coroutine_test
    async def test_fairness_interleaving(self) -> None:
        async with self.create_scheduler(
            None, SCHEDULER_PRIORITY_FAIRNESS=2
        ) as scheduler:
            for index in range(5):
                request = Request(
                    f"https://example.com/high-{index}", priority=10
                )
                request.meta[Downloader.DOWNLOAD_SLOT] = "a"
                scheduler.enqueue_request(request)
            for index in range(3):
                request = Request(
                    f"https://example.com/low-{index}", priority=-10
                )
                request.meta[Downloader.DOWNLOAD_SLOT] = "b"
                scheduler.enqueue_request(request)
            priorities = _dequeue_priorities(scheduler)
        assert priorities.count(10) == 5
        assert priorities.count(-10) == 3
        run = 0
        lows_remaining = 3
        for priority in priorities:
            if priority == -10:
                run = 0
                lows_remaining -= 1
            elif lows_remaining:
                run += 1
                assert run <= 2


class TestFairnessDownloaderAwareOnDisk(TestFairnessDownloaderAware):
    @pytest.fixture
    def jobdir(self, tmp_path: Path) -> Path:
        return tmp_path

    @coroutine_test
    async def test_fairness_disk_restart(self, jobdir: Path) -> None:
        def populate(scheduler: Scheduler) -> None:
            for index in range(3):
                request = Request(
                    f"https://example.com/high-{index}", priority=10
                )
                request.meta[Downloader.DOWNLOAD_SLOT] = "a"
                scheduler.enqueue_request(request)
            for index in range(2):
                request = Request(
                    f"https://example.com/low-{index}", priority=-10
                )
                request.meta[Downloader.DOWNLOAD_SLOT] = "b"
                scheduler.enqueue_request(request)

        async with self.create_scheduler(
            jobdir, SCHEDULER_PRIORITY_FAIRNESS=1
        ) as scheduler:
            populate(scheduler)
            first = scheduler.next_request().priority

        async with self.create_scheduler(
            jobdir, SCHEDULER_PRIORITY_FAIRNESS=1
        ) as scheduler:
            assert len(scheduler) == 4
            rest = _dequeue_priorities(scheduler)
        combined = [first, *rest]
        assert combined.count(10) == 3
        assert combined.count(-10) == 2


@coroutine_test
async def test_fairness_unsupported_priority_queue(tmp_path: Path) -> None:
    """A custom priority queue without the fairness API fails clearly once
    the fairness limit is reached."""
    async with create_scheduler(
        f"{__name__}._PriorityQueueWithoutNextPriority",
        tmp_path,
        SCHEDULER_PRIORITY_FAIRNESS=1,
    ) as scheduler:
        scheduler.enqueue_request(Request("https://example.com/1", priority=1))
        scheduler.enqueue_request(Request("https://example.com/2", priority=-1))
        assert scheduler.next_request() is not None
        with pytest.raises(
            NotImplementedError, match="SCHEDULER_PRIORITY_FAIRNESS"
        ):
            scheduler.next_request()


class TestMigration:
    @coroutine_test
    async def test_migration(self, tmp_path: Path) -> None:
        async with create_scheduler(
            "scrapy.pqueues.ScrapyPriorityQueue", tmp_path
        ) as prev_scheduler:
            for url in _URLS:
                prev_scheduler.enqueue_request(Request(url))

        with pytest.raises(
            ValueError,
            match="DownloaderAwarePriorityQueue accepts ``slot_startprios`` as a dict",
        ):
            async with create_scheduler(
                "scrapy.pqueues.DownloaderAwarePriorityQueue", tmp_path
            ):
                pass


def _is_scheduling_fair(enqueued_slots: list[str], dequeued_slots: list[str]) -> bool:
    """
    We enqueued same number of requests for every slot.
    Assert correct order, e.g.

    >>> enqueued = ['a', 'b', 'c'] * 2
    >>> correct = ['a', 'c', 'b', 'b', 'a', 'c']
    >>> incorrect = ['a', 'a', 'b', 'c', 'c', 'b']
    >>> _is_scheduling_fair(enqueued, correct)
    True
    >>> _is_scheduling_fair(enqueued, incorrect)
    False
    """
    if len(dequeued_slots) != len(enqueued_slots):
        return False

    slots_number = len(set(enqueued_slots))
    for i in range(0, len(dequeued_slots), slots_number):
        part = dequeued_slots[i : i + slots_number]
        if len(part) != len(set(part)):
            return False

    return True


class DownloaderAwareSchedulerTestMixin(TestSchedulerBase):
    reopen = False
    priority_queue_cls = "scrapy.pqueues.DownloaderAwarePriorityQueue"

    @coroutine_test
    async def test_logic(self, jobdir: Path | None) -> None:
        def _setup(scheduler: Scheduler) -> None:
            for url, slot in _URLS_WITH_SLOTS:
                request = Request(url)
                request.meta[Downloader.DOWNLOAD_SLOT] = slot
                scheduler.enqueue_request(request)

        def _assert(scheduler: Scheduler) -> None:
            dequeued_slots: list[str] = []
            requests: list[Request] = []
            assert scheduler.crawler
            downloader = scheduler.crawler.engine.downloader
            assert isinstance(downloader, MockDownloader)
            while scheduler.has_pending_requests():
                request = scheduler.next_request()
                assert request is not None
                slot = downloader.get_slot_key(request)
                dequeued_slots.append(slot)
                downloader.increment(slot)
                requests.append(request)

            for request in requests:
                slot = downloader.get_slot_key(request)
                downloader.decrement(slot)

            assert _is_scheduling_fair([s for u, s in _URLS_WITH_SLOTS], dequeued_slots)
            assert sum(len(s.active) for s in downloader.slots.values()) == 0

        if self.reopen:
            async with self.create_scheduler(jobdir) as scheduler:
                _setup(scheduler)
            async with self.create_scheduler(jobdir) as scheduler:
                _assert(scheduler)
        else:
            async with self.create_scheduler(jobdir) as scheduler:
                _setup(scheduler)
                _assert(scheduler)


class TestSchedulerWithDownloaderAwareInMemory(
    DownloaderAwareSchedulerTestMixin, TestSchedulerInMemoryBase
):
    pass


class TestSchedulerWithDownloaderAwareOnDisk(
    DownloaderAwareSchedulerTestMixin, TestSchedulerOnDiskBase
):
    reopen = True

    @coroutine_test
    async def test_active_json_sync_every(self, jobdir: Path | None) -> None:
        assert jobdir is not None
        async with self.create_scheduler(jobdir, JOBDIR_SYNC_EVERY=1) as scheduler:

            def enqueue(slot: str, priority: int) -> None:
                request = Request(f"http://foo.com/{slot}{priority}", priority=priority)
                request.meta[Downloader.DOWNLOAD_SLOT] = slot
                scheduler.enqueue_request(request)

            enqueue("a", 1)
            assert _active_json(jobdir) == {"a": [-1]}
            enqueue("a", 2)
            assert _active_json(jobdir) == {"a": [-2, -1]}
            enqueue("b", 1)
            assert _active_json(jobdir) == {"a": [-2, -1], "b": [-1]}
            scheduler.next_request()
            assert _active_json(jobdir) == {"a": [-1], "b": [-1]}


class StartUrlsSpider(Spider):
    def __init__(self, start_urls):
        self.start_urls = start_urls
        super().__init__(name="StartUrlsSpider")

    def parse(self, response):
        pass


class TestIntegrationWithDownloaderAwareInMemory:
    def setup_method(self):
        self.crawler = get_crawler(
            spidercls=StartUrlsSpider,
            settings_dict={
                "SCHEDULER_PRIORITY_QUEUE": "scrapy.pqueues.DownloaderAwarePriorityQueue",
                "DUPEFILTER_CLASS": "scrapy.dupefilters.BaseDupeFilter",
            },
        )

    @inline_callbacks_test
    def test_integration_downloader_aware_priority_queue(self):
        with MockServer() as mockserver:
            url = mockserver.url("/status?n=200", is_secure=False)
            start_urls = [url] * 6
            yield self.crawler.crawl(start_urls)
            assert self.crawler.stats.get_value("downloader/response_count") == len(
                start_urls
            )


class TestIncompatibility:
    def _incompatible(self) -> None:
        settings = {
            "SCHEDULER_PRIORITY_QUEUE": "scrapy.pqueues.DownloaderAwarePriorityQueue",
            "CONCURRENT_REQUESTS_PER_IP": 1,
        }
        crawler = get_crawler(Spider, settings)
        scheduler = build_from_crawler(Scheduler, crawler)
        spider = Spider.from_crawler(crawler, name="spider")
        scheduler.open(spider)

    def test_incompatibility(self):
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=ScrapyDeprecationWarning,
                message="The CONCURRENT_REQUESTS_PER_IP setting is deprecated",
            )
            with pytest.raises(
                ValueError, match="does not support CONCURRENT_REQUESTS_PER_IP"
            ):
                self._incompatible()


class _PriorityQueueWithoutNextPriority:
    @classmethod
    def from_crawler(cls, crawler: Crawler, *args: Any, **kwargs: Any) -> Self:
        return cls(ScrapyPriorityQueue.from_crawler(crawler, *args, **kwargs))

    def __init__(self, queue: ScrapyPriorityQueue):
        self._queue = queue

    def push(self, request: Request) -> None:
        self._queue.push(request)

    def pop(self) -> Request | None:
        return self._queue.pop()

    def close(self) -> list[int]:
        return self._queue.close()

    def __len__(self) -> int:
        return len(self._queue)


@coroutine_test
async def test_priority_queue_without_next_priority(tmp_path: Path) -> None:
    async with create_scheduler(
        f"{__name__}._PriorityQueueWithoutNextPriority", tmp_path
    ) as scheduler:
        _enqueue_across_queues(scheduler)
        assert _dequeue_urls(scheduler) == [
            "https://example.com/memory0",
            "https://example.com/memory-1",
            "https://example.com/disk1",
            "https://example.com/disk0",
        ]


def test_scheduler_without_crawler() -> None:
    """The crawler argument of Scheduler is optional."""
    scheduler = Scheduler(BaseDupeFilter())
    assert scheduler.crawler is None


@coroutine_test
async def test_unserializable_requests(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Requests that cannot be serialized fall back to the memory queue."""

    class UnserializableSpider(Spider):
        name = "unserializable"

        async def start(self):
            for index in range(2):
                yield Request(f"data:,{index}", meta={"callable": lambda: None})

    crawler = get_crawler(
        UnserializableSpider, {"JOBDIR": str(tmp_path), "SCHEDULER_DEBUG": True}
    )
    await crawler.crawl_async()
    assert crawler.stats.get_value("scheduler/unserializable") == 2
    assert crawler.stats.get_value("scheduler/enqueued/memory") == 2
    assert caplog.text.count("Unable to serialize request") == 1
