from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import Mock

import pytest

from scrapy.core.scheduler import Scheduler
from scrapy.crawler import Crawler
from scrapy.dupefilters import DiskDupeFilter, RFPDupeFilter
from scrapy.http import Request
from scrapy.pqueues import ScrapyPriorityQueue
from scrapy.spiders import Spider
from scrapy.utils.defer import ensure_awaitable
from scrapy.utils.misc import build_from_crawler, load_object
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test
from tests.utils.downloader import MockDownloader

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


# Faults injected by the fake downstream queues. Each fault class is
# module-level so the settings can name them by their full import path.


class PushBeforeStorageError(RuntimeError):
    """Raised before the request is stored, so no push actually happened."""


class PushAfterStorageError(RuntimeError):
    """Raised after the request was stored: the storage result is committed."""


class _FakeDownstreamQueue:
    """Downstream queue stand-in with push fault injection.

    Instances are created by ScrapyPriorityQueue through
    :func:`~scrapy.utils.misc.build_from_crawler`, which passes the crawler and
    the per-priority key directory.
    """

    #: When set, the next push raises this exception class, then it is cleared.
    next_push_error: type[BaseException] | None = None
    #: Whether PushAfterStorageError stores the request before raising.
    stores_on_after_error: bool = True

    def __init__(self, crawler: Crawler, key: str) -> None:
        self.crawler = crawler
        self.key = key
        self.items: list[Request] = []
        self.closed = False

    @classmethod
    def from_crawler(
        cls, crawler: Crawler, key: str, *args: Any, **kwargs: Any
    ) -> Self:
        return cls(crawler, key)

    def push(self, request: Request) -> None:
        error = self.next_push_error
        if error is PushAfterStorageError and self.stores_on_after_error:
            self.items.append(request)
        if error is not None:
            type(self).next_push_error = None
            raise error
        self.items.append(request)

    def pop(self) -> Request | None:
        if not self.items:
            return None
        return self.items.pop()

    def close(self) -> None:
        self.closed = True

    def __len__(self) -> int:
        return len(self.items)


class FaultyDiskQueue(_FakeDownstreamQueue):
    next_push_error: type[BaseException] | None = None
    next_serialize_error: type[BaseException] | None = None
    stores_on_after_error: bool = True

    @classmethod
    def serialize(cls, request: Request, *, spider: Any = None) -> dict[str, Any]:
        error = cls.next_serialize_error
        if error is not None:
            cls.next_serialize_error = None
            raise error
        return request.to_dict(spider=spider)


class FaultyMemoryQueue(_FakeDownstreamQueue):
    next_push_error: type[BaseException] | None = None


class _PriorityQueueWithPostPushFault:
    """Priority queue wrapper that raises after the downstream push succeeds.

    The request is stored (and remains reachable through pop), but the
    scheduler sees a failed push, which makes the admission result uncertain.
    """

    post_push_error: type[BaseException] | None = None

    def __init__(self, queue: ScrapyPriorityQueue) -> None:
        self._queue = queue

    @classmethod
    def from_crawler(cls, crawler: Any, *args: Any, **kwargs: Any) -> Self:
        return cls(ScrapyPriorityQueue.from_crawler(crawler, *args, **kwargs))

    def push(self, request: Request) -> None:
        self._queue.push(request)
        error = type(self).post_push_error
        if error is not None:
            type(self).post_push_error = None
            raise error

    def pop(self) -> Request | None:
        return self._queue.pop()

    def close(self) -> Any:
        return self._queue.close()

    def __len__(self) -> int:
        return len(self._queue)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._queue, name)


class MockCrawler(Crawler):
    def __init__(self, jobdir: Path | None, **settings: Any):
        settings = {
            "SCHEDULER_DEBUG": True,
            "SCHEDULER_DISK_QUEUE": f"{__name__}.FaultyDiskQueue",
            "SCHEDULER_MEMORY_QUEUE": f"{__name__}.FaultyMemoryQueue",
            "SCHEDULER_PRIORITY_QUEUE": "scrapy.pqueues.ScrapyPriorityQueue",
            "JOBDIR": str(jobdir) if jobdir is not None else None,
            "SCHEDULER_ADMISSION_TRANSACTION": True,
            **settings,
        }
        super().__init__(Spider, settings)
        self.engine = Mock(downloader=MockDownloader())
        self.stats = load_object(self.settings["STATS_CLASS"])(self)
        self.request_fingerprinter = build_from_crawler(
            load_object(self.settings["REQUEST_FINGERPRINTER_CLASS"]),
            self,
        )


@asynccontextmanager
async def create_scheduler(
    jobdir: Path | None, **settings: Any
) -> AsyncGenerator[Scheduler]:
    mock_crawler = MockCrawler(jobdir, **settings)
    scheduler = build_from_crawler(Scheduler, mock_crawler)
    spider = Spider.from_crawler(mock_crawler, name="spider")
    await ensure_awaitable(scheduler.open(spider))
    try:
        yield scheduler
    finally:
        await ensure_awaitable(scheduler.close("finished"))
        await mock_crawler.stop_async()
        mock_crawler.engine.downloader.close()


class RealQueueCrawler(MockCrawler):
    """Crawler using the real, persistent disk and memory queues."""

    def __init__(self, jobdir: Path | None, **settings: Any):
        settings = {
            "SCHEDULER_DISK_QUEUE": "scrapy.squeues.PickleLifoDiskQueue",
            "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.LifoMemoryQueue",
            **settings,
        }
        super().__init__(jobdir, **settings)


@asynccontextmanager
async def create_real_scheduler(
    jobdir: Path | None, **settings: Any
) -> AsyncGenerator[Scheduler]:
    mock_crawler = RealQueueCrawler(jobdir, **settings)
    scheduler = build_from_crawler(Scheduler, mock_crawler)
    spider = Spider.from_crawler(mock_crawler, name="spider")
    await ensure_awaitable(scheduler.open(spider))
    try:
        yield scheduler
    finally:
        await ensure_awaitable(scheduler.close("finished"))
        await mock_crawler.stop_async()
        mock_crawler.engine.downloader.close()


def _dequeue_urls(scheduler: Scheduler) -> list[str]:
    return [request.url for request in iter(scheduler.next_request, None)]


def _reserve_concurrently(dupefilter: Any, request: Request, count: int) -> list[bool]:
    barrier = threading.Barrier(count)
    outcomes: list[bool] = []

    def reserve() -> None:
        barrier.wait()
        outcomes.append(dupefilter.reserve_fingerprint(request))

    threads = [threading.Thread(target=reserve) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def _enqueue_concurrently(
    scheduler: Scheduler, request: Request, count: int
) -> list[bool]:
    barrier = threading.Barrier(count)
    outcomes: list[bool] = []

    def enqueue() -> None:
        barrier.wait()
        outcomes.append(scheduler.enqueue_request(request))

    threads = [threading.Thread(target=enqueue) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def _stat(scheduler: Scheduler, name: str) -> Any:
    assert scheduler.stats is not None
    return scheduler.stats.get_value(name)


@pytest.fixture(autouse=True)
def _reset_queue_faults() -> Any:
    FaultyDiskQueue.next_push_error = None
    FaultyDiskQueue.next_serialize_error = None
    FaultyMemoryQueue.next_push_error = None
    _PriorityQueueWithPostPushFault.post_push_error = None
    yield
    FaultyDiskQueue.next_push_error = None
    FaultyDiskQueue.next_serialize_error = None
    FaultyMemoryQueue.next_push_error = None
    _PriorityQueueWithPostPushFault.post_push_error = None


class TestAdmissionTransactionInMemory:
    @coroutine_test
    async def test_success_stats_and_pop(self) -> None:
        async with create_scheduler(None) as scheduler:
            request = Request("https://example.com/a")
            assert scheduler.enqueue_request(request)
            assert _stat(scheduler, "scheduler/admission/succeeded") == 1
            assert _stat(scheduler, "scheduler/admission/rejected") is None
            assert _stat(scheduler, "scheduler/admission/uncertain") is None
            popped = scheduler.next_request()
            assert popped is not None
            assert popped.url == "https://example.com/a"
            assert popped.callback == request.callback

    @coroutine_test
    async def test_duplicate_rejected_once(self) -> None:
        async with create_scheduler(None) as scheduler:
            request = Request("https://example.com/a")
            assert scheduler.enqueue_request(request)
            assert not scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/rejected") == 1
            assert _stat(scheduler, "scheduler/admission/succeeded") == 1
            assert _dequeue_urls(scheduler) == ["https://example.com/a"]

    @coroutine_test
    async def test_dont_filter_bypasses_reservation(self) -> None:
        async with create_scheduler(None) as scheduler:
            for _ in range(3):
                assert scheduler.enqueue_request(
                    Request("https://example.com/a", dont_filter=True)
                )
            assert _stat(scheduler, "scheduler/admission/succeeded") == 3
            assert _stat(scheduler, "scheduler/admission/rejected") is None
            assert len(_dequeue_urls(scheduler)) == 3
            assert not scheduler.df._reservations  # type: ignore[attr-defined]

    @coroutine_test
    async def test_priorities(self) -> None:
        async with create_scheduler(None) as scheduler:
            for url, priority in (
                ("https://example.com/low", -2),
                ("https://example.com/high", 2),
                ("https://example.com/mid", 0),
            ):
                assert scheduler.enqueue_request(Request(url, priority=priority))
            assert _dequeue_urls(scheduler) == [
                "https://example.com/high",
                "https://example.com/mid",
                "https://example.com/low",
            ]

    @coroutine_test
    async def test_push_before_storage_failure_revokes(self) -> None:
        FaultyMemoryQueue.next_push_error = PushBeforeStorageError
        async with create_scheduler(None) as scheduler:
            assert not scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/uncertain") == 1
            assert _stat(scheduler, "scheduler/admission/succeeded") is None
            # The push failed before storing anything: nothing to pop.
            assert scheduler.next_request() is None
            # The reservation is kept, so the same fingerprint is rejected.
            assert not scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/rejected") == 1

    @coroutine_test
    async def test_push_after_storage_failure_keeps_reservation(self) -> None:
        _PriorityQueueWithPostPushFault.post_push_error = PushAfterStorageError
        async with create_scheduler(
            None,
            SCHEDULER_PRIORITY_QUEUE=(f"{__name__}._PriorityQueueWithPostPushFault"),
        ) as scheduler:
            assert not scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/uncertain") == 1
            # The fake queue stored the request even though push raised:
            # the stored request must still be retrievable.
            popped = scheduler.next_request()
            assert popped is not None
            assert popped.url == "https://example.com/a"
            # The reservation prevents a second enqueue in the same process.
            assert not scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/rejected") == 1

    @pytest.mark.parametrize("iteration", range(5))
    @coroutine_test
    async def test_concurrent_same_fingerprint_enqueued_once(
        self, iteration: int
    ) -> None:
        async with create_scheduler(None) as scheduler:
            thread_count = 8
            results = _enqueue_concurrently(
                scheduler, Request("https://example.com/a"), thread_count
            )
            assert results.count(True) == 1
            assert results.count(False) == thread_count - 1
            assert _stat(scheduler, "scheduler/admission/succeeded") == 1
            assert _stat(scheduler, "scheduler/admission/rejected") == (
                thread_count - 1
            )
            assert len(_dequeue_urls(scheduler)) == 1

    @coroutine_test
    async def test_original_request_unchanged(self) -> None:
        async with create_scheduler(None) as scheduler:

            def callback(response: Any) -> None:
                return None

            request = Request("https://example.com/a", callback=callback)
            assert scheduler.enqueue_request(request)
            popped = scheduler.next_request()
            assert popped is not None
            assert popped.callback is callback
            assert request.url == "https://example.com/a"


class TestAdmissionTransactionOnDisk:
    @coroutine_test
    async def test_disk_success_persists_fingerprint(self, tmp_path: Path) -> None:
        async with create_real_scheduler(tmp_path) as scheduler:
            assert scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/enqueued/disk") == 1
        # Reopen the JOBDIR: both the queued request and the fingerprint
        # survive, so the URL is treated as already scheduled.
        async with create_real_scheduler(tmp_path) as scheduler:
            assert not scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/rejected") == 1
            popped = scheduler.next_request()
            assert popped is not None
            assert popped.url == "https://example.com/a"

    @coroutine_test
    async def test_revoked_reservation_not_persisted(self, tmp_path: Path) -> None:
        """A reservation of an admission that never confirmed must not survive
        a restart, while a later successful admission does persist."""
        FaultyDiskQueue.next_push_error = PushBeforeStorageError
        async with create_scheduler(tmp_path) as scheduler:
            assert not scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/uncertain") == 1
        async with create_real_scheduler(tmp_path) as scheduler:
            assert scheduler.enqueue_request(Request("https://example.com/a"))
            popped = scheduler.next_request()
            assert popped is not None
            assert popped.url == "https://example.com/a"

    @coroutine_test
    async def test_uncertain_after_storage_restart_state(self, tmp_path: Path) -> None:
        _PriorityQueueWithPostPushFault.post_push_error = PushAfterStorageError
        async with create_scheduler(
            tmp_path,
            SCHEDULER_PRIORITY_QUEUE=(f"{__name__}._PriorityQueueWithPostPushFault"),
        ) as scheduler:
            assert not scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/uncertain") == 1
            # The request did reach the fake queue's storage.
            popped = scheduler.next_request()
            assert popped is not None
            assert popped.url == "https://example.com/a"
        async with create_real_scheduler(tmp_path) as scheduler:
            # Confirmation never ran, so the fingerprint is not durable; the
            # URL can be scheduled again after the process restarts.
            assert scheduler.enqueue_request(Request("https://example.com/a"))

    @coroutine_test
    async def test_unserializable_falls_back_to_memory(self, tmp_path: Path) -> None:
        async with create_real_scheduler(tmp_path) as scheduler:
            request = Request(
                "https://example.com/a", meta={"unserializable": lambda: None}
            )
            assert scheduler.enqueue_request(request)
            assert _stat(scheduler, "scheduler/unserializable") == 1
            assert _stat(scheduler, "scheduler/enqueued/memory") == 1
            popped = scheduler.next_request()
            assert popped is not None
            assert popped.url == "https://example.com/a"

    @coroutine_test
    async def test_memory_fallback_push_failure(self, tmp_path: Path) -> None:
        FaultyDiskQueue.next_push_error = PushBeforeStorageError
        FaultyMemoryQueue.next_push_error = PushBeforeStorageError
        async with create_scheduler(tmp_path) as scheduler:
            assert not scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/uncertain") == 1
            assert scheduler.next_request() is None

    @coroutine_test
    async def test_priorities_across_restart(self, tmp_path: Path) -> None:
        async with create_real_scheduler(tmp_path) as scheduler:
            for url, priority in (
                ("https://example.com/low", -2),
                ("https://example.com/high", 2),
                ("https://example.com/mid", 0),
            ):
                assert scheduler.enqueue_request(Request(url, priority=priority))
        async with create_real_scheduler(tmp_path) as scheduler:
            assert _dequeue_urls(scheduler) == [
                "https://example.com/high",
                "https://example.com/mid",
                "https://example.com/low",
            ]

    @coroutine_test
    async def test_duplicate_across_restart(self, tmp_path: Path) -> None:
        async with create_real_scheduler(tmp_path) as scheduler:
            assert scheduler.enqueue_request(Request("https://example.com/a"))
        async with create_real_scheduler(tmp_path) as scheduler:
            assert not scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/rejected") == 1


class TestCloseCancellation:
    @coroutine_test
    async def test_enqueue_during_close_rejected(self, tmp_path: Path) -> None:
        async with create_scheduler(tmp_path) as scheduler:
            await ensure_awaitable(scheduler.close("finished"))
            assert not scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/rejected") == 1
            assert scheduler.next_request() is None
            assert not scheduler.df._reservations  # type: ignore[attr-defined]


class LegacyOnlyDupeFilter:
    """A custom dupefilter without the reservation protocol."""

    def __init__(self) -> None:
        self.seen: set[bytes] = set()

    @classmethod
    def from_crawler(cls, crawler: Any) -> LegacyOnlyDupeFilter:
        return cls()

    def open(self) -> None:
        pass

    def close(self, reason: str) -> None:
        pass

    def log(self, request: Request, spider: Any) -> None:
        pass

    def request_seen(self, request: Request) -> bool:
        fp = request.url.encode()
        if fp in self.seen:
            return True
        self.seen.add(fp)
        return False


class PartialProtocolDupeFilter:
    """Implements only some of the reservation protocol methods."""

    def __init__(self) -> None:
        self.seen: set[bytes] = set()

    @classmethod
    def from_crawler(cls, crawler: Any) -> PartialProtocolDupeFilter:
        return cls()

    def open(self) -> None:
        pass

    def close(self, reason: str) -> None:
        pass

    def log(self, request: Request, spider: Any) -> None:
        pass

    def request_seen(self, request: Request) -> bool:
        fp = request.url.encode()
        if fp in self.seen:
            return True
        self.seen.add(fp)
        return False

    def reserve_fingerprint(self, request: Request) -> bool:
        return self.request_seen(request)


class TestBackwardCompatibility:
    @coroutine_test
    async def test_default_setting_keeps_legacy_path(self, tmp_path: Path) -> None:
        crawler = MockCrawler(tmp_path, SCHEDULER_ADMISSION_TRANSACTION=False)
        scheduler = build_from_crawler(Scheduler, crawler)
        spider = Spider.from_crawler(crawler, name="spider")
        await ensure_awaitable(scheduler.open(spider))
        try:
            assert not scheduler._admission_transaction_active
            assert scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/succeeded") is None
        finally:
            await ensure_awaitable(scheduler.close("finished"))
            await crawler.stop_async()
            crawler.engine.downloader.close()

    @coroutine_test
    async def test_legacy_dupefilter_falls_back_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with create_scheduler(
            tmp_path, DUPEFILTER_CLASS=f"{__name__}.LegacyOnlyDupeFilter"
        ) as scheduler:
            assert not scheduler._admission_transaction_active
            assert "reservation protocol" in caplog.text
            assert scheduler.enqueue_request(Request("https://example.com/a"))
            # The legacy request_seen() path marks duplicates as before.
            assert not scheduler.enqueue_request(Request("https://example.com/a"))

    @coroutine_test
    async def test_partial_protocol_dupefilter_falls_back(self, tmp_path: Path) -> None:
        async with create_scheduler(
            tmp_path, DUPEFILTER_CLASS=f"{__name__}.PartialProtocolDupeFilter"
        ) as scheduler:
            assert not scheduler._admission_transaction_active

    @coroutine_test
    async def test_base_dupefilter_supports_protocol(self, tmp_path: Path) -> None:
        async with create_scheduler(
            tmp_path, DUPEFILTER_CLASS="scrapy.dupefilters.BaseDupeFilter"
        ) as scheduler:
            assert scheduler._admission_transaction_active
            for _ in range(2):
                assert scheduler.enqueue_request(
                    Request("https://example.com/a", dont_filter=True)
                )


class FaultyReservationDupeFilter:
    """Dupefilter whose reservation protocol steps raise on demand."""

    fail_on_confirm = False
    fail_on_reserve = False

    def __init__(self) -> None:
        self.reserved: set[bytes] = set()

    @classmethod
    def from_crawler(cls, crawler: Any) -> FaultyReservationDupeFilter:
        return cls()

    def open(self) -> None:
        pass

    def close(self, reason: str) -> None:
        pass

    def log(self, request: Request, spider: Any) -> None:
        pass

    def request_seen(self, request: Request) -> bool:
        return False

    def reserve_fingerprint(self, request: Request) -> bool:
        if self.fail_on_reserve:
            raise RuntimeError("reserve failure")
        fp = request.url.encode()
        if fp in self.reserved:
            return True
        self.reserved.add(fp)
        return False

    def confirm_fingerprint(self, request: Request) -> None:
        if self.fail_on_confirm:
            raise RuntimeError("confirm failure")

    def revoke_fingerprint(self, request: Request) -> None:
        self.reserved.discard(request.url.encode())


class TestAdmissionFailureBranches:
    @coroutine_test
    async def test_confirm_failure_is_uncertain(self, tmp_path: Path) -> None:
        FaultyReservationDupeFilter.fail_on_confirm = True
        try:
            async with create_scheduler(
                tmp_path,
                DUPEFILTER_CLASS=f"{__name__}.FaultyReservationDupeFilter",
            ) as scheduler:
                assert not scheduler.enqueue_request(Request("https://example.com/a"))
                assert _stat(scheduler, "scheduler/admission/uncertain") == 1
                # The reservation remains, so a retry is rejected, not resent.
                assert not scheduler.enqueue_request(Request("https://example.com/a"))
                assert _stat(scheduler, "scheduler/admission/rejected") == 1
        finally:
            FaultyReservationDupeFilter.fail_on_confirm = False
            FaultyReservationDupeFilter.fail_on_reserve = False

    @coroutine_test
    async def test_reserve_failure_is_uncertain(self, tmp_path: Path) -> None:
        FaultyReservationDupeFilter.fail_on_reserve = True
        try:
            async with create_scheduler(
                tmp_path,
                DUPEFILTER_CLASS=f"{__name__}.FaultyReservationDupeFilter",
            ) as scheduler:
                with pytest.raises(RuntimeError, match="reserve failure"):
                    scheduler.enqueue_request(Request("https://example.com/a"))
                assert scheduler.next_request() is None
        finally:
            FaultyReservationDupeFilter.fail_on_reserve = False

    @coroutine_test
    async def test_unexpected_serialization_error_is_uncertain(
        self, tmp_path: Path
    ) -> None:
        FaultyDiskQueue.next_serialize_error = OSError
        async with create_scheduler(tmp_path) as scheduler:
            assert not scheduler.enqueue_request(Request("https://example.com/a"))
            assert _stat(scheduler, "scheduler/admission/uncertain") == 1
            assert scheduler.next_request() is None


class TestRfpReservationProtocol:
    @pytest.mark.parametrize("dupefilter_class", [RFPDupeFilter, DiskDupeFilter])
    def test_reserve_confirm_revoke_lifecycle(
        self, dupefilter_class: type[RFPDupeFilter], tmp_path: Path
    ) -> None:
        jobdir = tmp_path / dupefilter_class.__name__
        crawler = get_crawler(
            settings_dict={
                "DUPEFILTER_CLASS": (
                    f"{dupefilter_class.__module__}.{dupefilter_class.__name__}"
                ),
                "JOBDIR": str(jobdir),
            }
        )
        dupefilter = build_from_crawler(dupefilter_class, crawler)
        dupefilter.open()
        request = Request("https://example.com/a")
        try:
            assert not dupefilter.reserve_fingerprint(request)
            # A second reservation of the same fingerprint is rejected.
            assert dupefilter.reserve_fingerprint(request)
            dupefilter.revoke_fingerprint(request)
            assert not dupefilter.reserve_fingerprint(request)
            dupefilter.confirm_fingerprint(request)
            assert dupefilter.request_seen(request)
            # Revoking an unknown or already confirmed fingerprint is a no-op.
            dupefilter.revoke_fingerprint(Request("https://example.com/other"))
        finally:
            dupefilter.close("finished")

    def test_concurrent_reservation_only_one_succeeds(self, tmp_path: Path) -> None:
        # The in-memory RFPDupeFilter keeps its reservation set in Python, so
        # the reservation lock makes reserve_fingerprint() atomic across
        # threads. DiskDupeFilter uses an SQLite connection bound to one
        # thread (the scheduler runs on the reactor thread), so it is not
        # exercised here; its single-threaded ordering is covered by the
        # lifecycle and durability tests above.
        crawler = get_crawler(
            settings_dict={
                "DUPEFILTER_CLASS": "scrapy.dupefilters.RFPDupeFilter",
                "JOBDIR": str(tmp_path),
            }
        )
        dupefilter = build_from_crawler(RFPDupeFilter, crawler)
        dupefilter.open()
        request = Request("https://example.com/a")
        try:
            count = 8
            for _ in range(5):
                outcomes = _reserve_concurrently(dupefilter, request, count)
                assert outcomes.count(False) == 1
                assert outcomes.count(True) == count - 1
                # Reset for the next iteration.
                dupefilter.revoke_fingerprint(request)
        finally:
            dupefilter.close("finished")

    @pytest.mark.parametrize("dupefilter_class", [RFPDupeFilter, DiskDupeFilter])
    def test_reservations_not_durable(
        self, dupefilter_class: type[RFPDupeFilter], tmp_path: Path
    ) -> None:
        jobdir = tmp_path / dupefilter_class.__name__
        settings = {
            "DUPEFILTER_CLASS": (
                f"{dupefilter_class.__module__}.{dupefilter_class.__name__}"
            ),
            "JOBDIR": str(jobdir),
        }
        crawler = get_crawler(settings_dict=settings)
        first = build_from_crawler(dupefilter_class, crawler)
        first.open()
        request = Request("https://example.com/a")
        assert not first.reserve_fingerprint(request)
        first.close("finished")

        resumed = build_from_crawler(
            dupefilter_class, get_crawler(settings_dict=settings)
        )
        resumed.open()
        try:
            assert not resumed.request_seen(request)
        finally:
            resumed.close("finished")

    @pytest.mark.parametrize("dupefilter_class", [RFPDupeFilter, DiskDupeFilter])
    def test_confirmed_fingerprint_durable(
        self, dupefilter_class: type[RFPDupeFilter], tmp_path: Path
    ) -> None:
        jobdir = tmp_path / dupefilter_class.__name__
        settings = {
            "DUPEFILTER_CLASS": (
                f"{dupefilter_class.__module__}.{dupefilter_class.__name__}"
            ),
            "JOBDIR": str(jobdir),
        }
        first = build_from_crawler(
            dupefilter_class, get_crawler(settings_dict=settings)
        )
        first.open()
        request = Request("https://example.com/a")
        assert not first.reserve_fingerprint(request)
        first.confirm_fingerprint(request)
        first.close("finished")

        resumed = build_from_crawler(
            dupefilter_class, get_crawler(settings_dict=settings)
        )
        resumed.open()
        try:
            assert resumed.request_seen(request)
        finally:
            resumed.close("finished")


class TestAdmissionTransactionIntegration:
    @coroutine_test
    async def test_crawl_with_default_priority_queue(self, tmp_path: Path) -> None:
        # The default DownloaderAwarePriorityQueue delegates serialize() to
        # the downstream disk queue, so the transactional path works end to end.
        async with create_real_scheduler(
            tmp_path,
            SCHEDULER_PRIORITY_QUEUE=("scrapy.pqueues.DownloaderAwarePriorityQueue"),
        ) as scheduler:
            for url in (
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/a",
            ):
                scheduler.enqueue_request(Request(url))
            assert _stat(scheduler, "scheduler/admission/succeeded") == 2
            assert _stat(scheduler, "scheduler/admission/rejected") == 1
            assert _stat(scheduler, "scheduler/admission/uncertain") is None
            assert len(_dequeue_urls(scheduler)) == 2

    @coroutine_test
    async def test_skip_dupefilter_once_meta_removed(self, tmp_path: Path) -> None:
        async with create_real_scheduler(tmp_path) as scheduler:
            request = Request("https://example.com/a")
            request.meta["skip_dupefilter_once"] = True
            assert scheduler.enqueue_request(request)
            assert "skip_dupefilter_once" not in request.meta
            # No reservation is taken for a skip_dupefilter_once request.
            assert not scheduler.df._reservations  # type: ignore[attr-defined]
