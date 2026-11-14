"""Tests for the optional MEDIA_RESULTS_BY_SOURCE_INDEX mode of the media
pipelines.

Everything here runs against a local fake downloader with manually controlled
asyncio futures: no real network request is ever sent.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest
from twisted.python.failure import Failure

from scrapy.http import Request, Response
from scrapy.pipelines.media import (
    FileInfo,
    MediaPipeline,
    _MediaPipelineClosed,
)
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import Awaitable


class ControllableDownloader:
    """Fake ``engine.download_async`` replacement.

    Every request blocks on a manually resolvable :class:`asyncio.Future`, so
    tests decide the completion order. ``downloads`` maps request URL to the
    number of actual download attempts, which makes fingerprint merging
    observable.
    """

    def __init__(self) -> None:
        self.downloads: dict[str, int] = {}
        self.pending: dict[str, asyncio.Future[Response]] = {}

    def _key(self, request: Request) -> str:
        return request.url

    async def __call__(self, request: Request) -> Response:
        key = self._key(request)
        self.downloads[key] = self.downloads.get(key, 0) + 1
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        return await future

    def resolve(self, url: str, response: Response | BaseException) -> None:
        future = self.pending.pop(url)
        if isinstance(response, BaseException):
            future.set_exception(response)
        else:
            future.set_result(response)


class IndexedMediaPipeline(MediaPipeline):
    """Minimal MediaPipeline that turns fake responses into FileInfo dicts."""

    MEDIA_NAME = "media"

    def media_to_download(
        self, request: Request, info: MediaPipeline.SpiderInfo, *, item: Any = None
    ) -> None:
        return None

    def get_media_requests(
        self, item: Any, info: MediaPipeline.SpiderInfo
    ) -> list[Request]:
        return cast("list[Request]", item["requests"])

    def media_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> FileInfo:
        return {
            "url": request.url,
            "path": f"store/{request.url.rsplit('/', 1)[-1]}",
            "checksum": f"sum-{request.url}",
            "status": "downloaded",
        }

    def media_failed(
        self, failure: Failure, request: Request, info: MediaPipeline.SpiderInfo
    ) -> Failure:
        failure.raiseException()

    def file_path(
        self,
        request: Request,
        response: Response | None = None,
        info: MediaPipeline.SpiderInfo | None = None,
        *,
        item: Any = None,
    ) -> str:
        return ""

    def item_completed(
        self,
        results: list[tuple[bool, dict[str, Any]]],
        item: Any,
        info: MediaPipeline.SpiderInfo,
    ) -> Any:
        item["results"] = results
        return item


def _build_pipeline(
    downloader: ControllableDownloader,
    settings: dict[str, Any] | None = None,
) -> IndexedMediaPipeline:
    crawler = get_crawler(DefaultSpider, settings)
    crawler.spider = crawler._create_spider()
    crawler.engine = MagicMock(download_async=downloader)
    pipeline = build_from_crawler(IndexedMediaPipeline, crawler)
    pipeline.open_spider()
    return pipeline


async def _wait_until(condition: Any, *, limit: int = 100) -> None:
    """Pump the event loop until *condition* holds.

    The media pipeline yields several times per request, so a fixed number of
    sleeps is not enough to reach the fake downloader deterministically.
    """
    for _ in range(limit):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met before the wait limit")


class TestSourceIndexMediaPipeline:
    def setup_method(self) -> None:
        self.downloader = ControllableDownloader()
        self.pipe = _build_pipeline(
            self.downloader, {"MEDIA_RESULTS_BY_SOURCE_INDEX": True}
        )
        self.info = self.pipe.spiderinfo

    def teardown_method(self) -> None:
        # Fail loudly if a test leaves positions behind without closing.
        assert not self.info.inflight

    @coroutine_test
    async def test_setting_disabled_keeps_default_results(self) -> None:
        downloader = ControllableDownloader()
        pipe = _build_pipeline(downloader, {"MEDIA_RESULTS_BY_SOURCE_INDEX": False})
        assert pipe.results_by_source_index is False
        item = {"requests": [Request("http://example.com/a")]}
        task = asyncio.ensure_future(pipe.process_item(item))
        await _wait_until(lambda: "http://example.com/a" in downloader.pending)
        downloader.resolve("http://example.com/a", Response("http://example.com/a"))
        new_item = await task
        # Default behavior: plain FileInfo tuples without source_index.
        assert new_item["results"] == [
            (
                True,
                {
                    "url": "http://example.com/a",
                    "path": "store/a",
                    "checksum": "sum-http://example.com/a",
                    "status": "downloaded",
                },
            )
        ]

    @coroutine_test
    async def test_out_of_order_completion_keeps_input_order(self) -> None:
        item = {
            "requests": [
                Request("http://example.com/one"),
                Request("http://example.com/two"),
                Request("http://example.com/three"),
            ]
        }
        task = asyncio.ensure_future(self.pipe.process_item(item))
        await _wait_until(lambda: len(self.downloader.pending) == 3)
        assert set(self.downloader.pending) == {
            "http://example.com/one",
            "http://example.com/two",
            "http://example.com/three",
        }
        # Resolve in reverse order: results must still be indexed by position.
        self.downloader.resolve(
            "http://example.com/three", Response("http://example.com/three")
        )
        self.downloader.resolve(
            "http://example.com/two", Response("http://example.com/two")
        )
        self.downloader.resolve(
            "http://example.com/one", Response("http://example.com/one")
        )
        new_item = await task
        results = new_item["results"]
        assert [record["source_index"] for _ok, record in results] == [0, 1, 2]
        assert [record["url"] for _ok, record in results] == [
            "http://example.com/one",
            "http://example.com/two",
            "http://example.com/three",
        ]
        assert all(ok for ok, _record in results)
        # Real path/checksum from the actual download, not log-derived guesses.
        first = results[0][1]
        assert first["path"] == "store/one"
        assert first["checksum"] == "sum-http://example.com/one"

    @coroutine_test
    async def test_duplicate_urls_download_once_two_source_positions(self) -> None:
        shared = "http://example.com/shared"
        item = {
            "requests": [
                Request(shared),
                Request("http://example.com/other"),
                Request(shared),
            ]
        }
        task = asyncio.ensure_future(self.pipe.process_item(item))
        await _wait_until(lambda: len(self.downloader.pending) == 2)
        # The shared URL is downloaded once, the other URL once.
        assert self.downloader.downloads == {
            shared: 1,
            "http://example.com/other": 1,
        }
        self.downloader.resolve(shared, Response(shared))
        self.downloader.resolve(
            "http://example.com/other", Response("http://example.com/other")
        )
        new_item = await task
        results = new_item["results"]
        assert len(results) == 3
        assert [record["source_index"] for _ok, record in results] == [0, 1, 2]
        # Both source positions reference the same downloaded artifact.
        assert results[0][1]["path"] == results[2][1]["path"] == "store/shared"
        assert (
            results[0][1]["checksum"]
            == results[2][1]["checksum"]
            == f"sum-{shared}"
        )
        assert results[1][1]["url"] == "http://example.com/other"

    @coroutine_test
    async def test_duplicate_url_across_two_items_has_independent_containers(
        self,
    ) -> None:
        shared = "http://example.com/shared"
        item1 = {"requests": [Request(shared), Request("http://example.com/a1")]}
        item2 = {"requests": [Request(shared), Request("http://example.com/a2")]}
        task1 = asyncio.ensure_future(self.pipe.process_item(item1))
        task2 = asyncio.ensure_future(self.pipe.process_item(item2))
        await _wait_until(lambda: len(self.downloader.pending) == 3)
        # One actual download for the URL shared by both items.
        assert self.downloader.downloads == {
            shared: 1,
            "http://example.com/a1": 1,
            "http://example.com/a2": 1,
        }
        self.downloader.resolve(shared, Response(shared))
        self.downloader.resolve(
            "http://example.com/a1", Response("http://example.com/a1")
        )
        self.downloader.resolve(
            "http://example.com/a2", Response("http://example.com/a2")
        )
        result1, result2 = await asyncio.gather(task1, task2)

        records1 = result1["results"]
        records2 = result2["results"]
        assert records1 is not records2
        assert [r["source_index"] for _ok, r in records1] == [0, 1]
        assert [r["source_index"] for _ok, r in records2] == [0, 1]
        # Mutating one item's mapping must not touch the other item.
        records1[0][1]["path"] = "tampered"
        assert records2[0][1]["path"] == "store/shared"
        assert records2[0][1]["checksum"] == f"sum-{shared}"

    @coroutine_test
    async def test_failure_kept_in_place_does_not_shift_indices(self) -> None:
        item = {
            "requests": [
                Request("http://example.com/ok1"),
                Request("http://example.com/broken"),
                Request("http://example.com/ok2"),
            ]
        }
        task = asyncio.ensure_future(self.pipe.process_item(item))
        await _wait_until(lambda: len(self.downloader.pending) == 3)
        self.downloader.resolve(
            "http://example.com/broken", RuntimeError("boom")
        )
        self.downloader.resolve(
            "http://example.com/ok1", Response("http://example.com/ok1")
        )
        self.downloader.resolve(
            "http://example.com/ok2", Response("http://example.com/ok2")
        )
        new_item = await task
        results = new_item["results"]
        assert [record["source_index"] for _ok, record in results] == [0, 1, 2]
        ok_flags = [ok for ok, _record in results]
        assert ok_flags == [True, False, True]

        failure_record = results[1][1]
        assert failure_record["source_index"] == 1
        assert failure_record["source_url"] == "http://example.com/broken"
        assert failure_record["url"] == "http://example.com/broken"
        assert failure_record["error_type"] == "builtins.RuntimeError"
        assert failure_record["error_message"] == "boom"
        # The neighboring success rows keep their real data.
        assert results[0][1]["path"] == "store/ok1"
        assert results[2][1]["path"] == "store/ok2"

    @coroutine_test
    async def test_source_url_pre_normalization_is_preserved(self) -> None:
        raw_url = "http://example.com/a file?.bmp"
        request = Request(raw_url)
        # Request percent-encodes the URL; the raw URL is gone from request.url.
        assert request.url != raw_url
        request.meta["media_source_url"] = raw_url
        normalized = request.url
        item = {"requests": [request]}
        task = asyncio.ensure_future(self.pipe.process_item(item))
        await _wait_until(lambda: normalized in self.downloader.pending)
        self.downloader.resolve(normalized, Response(normalized))
        new_item = await task
        record = new_item["results"][0][1]
        assert record["source_url"] == raw_url
        assert record["url"] == normalized

    @coroutine_test
    async def test_cached_failure_reused_but_two_positions_recorded(self) -> None:
        broken = "http://example.com/broken"
        first = asyncio.ensure_future(
            self.pipe.process_item({"requests": [Request(broken)]})
        )
        await _wait_until(lambda: broken in self.downloader.pending)
        self.downloader.resolve(broken, RuntimeError("down"))
        await first

        # Second item references the same URL and an OK one. The failure is
        # served from the outcome cache, no new download attempt.
        item = {
            "requests": [Request(broken), Request("http://example.com/fine")]
        }
        task = asyncio.ensure_future(self.pipe.process_item(item))
        await _wait_until(
            lambda: "http://example.com/fine" in self.downloader.pending
        )
        self.downloader.resolve(
            "http://example.com/fine", Response("http://example.com/fine")
        )
        new_item = await task
        results = new_item["results"]
        assert [r["source_index"] for _ok, r in results] == [0, 1]
        assert results[0][0] is False
        assert results[0][1]["error_type"] == "builtins.RuntimeError"
        assert results[1][0] is True
        assert self.downloader.downloads == {
            broken: 1,
            "http://example.com/fine": 1,
        }

    @coroutine_test
    async def test_dont_filter_does_not_change_merging_or_other_items(
        self,
    ) -> None:
        shared = "http://example.com/shared"
        item1 = {
            "requests": [Request(shared, dont_filter=True), Request(shared)]
        }
        item2 = {"requests": [Request(shared)]}
        tasks = [
            asyncio.ensure_future(self.pipe.process_item(item1)),
            asyncio.ensure_future(self.pipe.process_item(item2)),
        ]
        await _wait_until(lambda: shared in self.downloader.pending)
        # Fingerprint merging ignores dont_filter: one download for the URL.
        assert self.downloader.downloads == {shared: 1}
        self.downloader.resolve(shared, Response(shared))
        new1, new2 = await asyncio.gather(*tasks)
        assert [r["source_index"] for _ok, r in new1["results"]] == [0, 1]
        assert [r["source_index"] for _ok, r in new2["results"]] == [0]
        assert all(ok for item in (new1, new2) for ok, _r in item["results"])
        assert self.downloader.downloads == {shared: 1}

    @coroutine_test
    async def test_cancelling_one_waiter_keeps_other_results(self) -> None:
        shared = "http://example.com/shared"
        item1 = {"requests": [Request(shared)]}
        item2 = {"requests": [Request(shared), Request("http://example.com/other")]}
        task1 = asyncio.ensure_future(self.pipe.process_item(item1))
        task2 = asyncio.ensure_future(self.pipe.process_item(item2))
        await _wait_until(lambda: len(self.downloader.pending) == 2)
        # Cancel the first waiter while the shared download is in flight.
        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1
        # The download itself continues and the other waiter still gets it.
        self.downloader.resolve(shared, Response(shared))
        self.downloader.resolve(
            "http://example.com/other", Response("http://example.com/other")
        )
        new2 = await task2
        assert [r["source_index"] for _ok, r in new2["results"]] == [0, 1]
        assert new2["results"][0][1]["path"] == "store/shared"
        assert new2["results"][1][1]["path"] == "store/other"
        assert self.downloader.downloads == {
            shared: 1,
            "http://example.com/other": 1,
        }

    @coroutine_test
    async def test_close_spider_releases_inflight_positions(self) -> None:
        item = {
            "requests": [
                Request("http://example.com/hanging"),
                Request("http://example.com/also-hanging"),
            ]
        }
        task = asyncio.ensure_future(self.pipe.process_item(item))
        await _wait_until(lambda: len(self.info.inflight) == 2)
        assert len(self.info.inflight) == 2
        await self.pipe.close_spider()
        new_item = await task
        assert not self.info.inflight
        results = new_item["results"]
        assert [r["source_index"] for _ok, r in results] == [0, 1]
        assert all(not ok for ok, _r in results)
        for _ok, record in results:
            assert record["error_type"] == (
                "scrapy.pipelines.media._MediaPipelineClosed"
            )
        # Nothing was downloaded because the futures never resolved.
        assert self.downloader.downloads == {
            "http://example.com/hanging": 1,
            "http://example.com/also-hanging": 1,
        }

    @coroutine_test
    async def test_two_families_of_stats_are_separate(self) -> None:
        shared = "http://example.com/shared"
        item = {
            "requests": [
                Request(shared),
                Request(shared),
                Request("http://example.com/broken"),
            ]
        }
        task = asyncio.ensure_future(self.pipe.process_item(item))
        await _wait_until(lambda: len(self.downloader.pending) == 2)
        self.downloader.resolve(
            "http://example.com/broken", RuntimeError("nope")
        )
        self.downloader.resolve(shared, Response(shared))
        await task
        stats = self.pipe.crawler.stats.get_stats()
        # Per-source-position stats: 3 input positions.
        assert stats["media_source_count"] == 3
        assert stats["media_source_status_count/ok"] == 2
        assert stats["media_source_status_count/failed"] == 1

    def test_prefixed_setting_is_supported(self) -> None:
        crawler = get_crawler(
            DefaultSpider,
            {"INDEXEDMEDIAPIPELINE_MEDIA_RESULTS_BY_SOURCE_INDEX": True},
        )
        crawler.spider = crawler._create_spider()
        crawler.engine = MagicMock(download_async=ControllableDownloader())
        pipe = build_from_crawler(IndexedMediaPipeline, crawler)
        assert pipe.results_by_source_index is True

    @coroutine_test
    async def test_errback_recovery_is_delivered_as_success(self) -> None:
        def errback(failure: Failure) -> dict[str, Any]:
            return {"recovered_from": str(failure.value)}

        broken = "http://example.com/broken"
        # Prime the outcome cache with a failure.
        priming = asyncio.ensure_future(
            self.pipe.process_item({"requests": [Request(broken)]})
        )
        await _wait_until(lambda: broken in self.downloader.pending)
        self.downloader.resolve(broken, RuntimeError("down"))
        await priming

        item = {"requests": [Request(broken, errback=errback)]}
        new_item = await self.pipe.process_item(item)
        ok, record = new_item["results"][0]
        assert ok is True
        assert record["source_index"] == 0
        assert record["source_url"] == broken
        assert record["url"] == broken
        assert record["recovered_from"] == "down"

    @coroutine_test
    async def test_non_dict_errback_recovery_kept_in_value_field(self) -> None:
        def errback(failure: Failure) -> str:
            # A custom pipeline may recover with a non-dict value.
            return f"recovered:{failure.value}"

        broken = "http://example.com/broken"
        priming = asyncio.ensure_future(
            self.pipe.process_item({"requests": [Request(broken)]})
        )
        await _wait_until(lambda: broken in self.downloader.pending)
        self.downloader.resolve(broken, RuntimeError("down"))
        await priming

        new_item = await self.pipe.process_item(
            {"requests": [Request(broken, errback=errback)]}
        )
        ok, record = new_item["results"][0]
        assert ok is True
        assert record["source_index"] == 0
        assert record["value"] == "recovered:down"
        assert record["path"] == ""
        assert record["checksum"] is None

    @coroutine_test
    async def test_close_consumes_finished_download_task_result(self) -> None:
        """A download that finishes on its own while the pipeline closes must
        have its task result retrieved, never reported as unhandled."""
        url = "http://example.com/finishes"
        task = asyncio.ensure_future(
            self.pipe.process_item({"requests": [Request(url)]})
        )
        await _wait_until(lambda: url in self.downloader.pending)
        # Resolve the real download, then close without awaiting the item.
        self.downloader.resolve(url, Response(url))
        await self.pipe.close_spider()
        await task
        assert not self.info.download_tasks
        assert not self.info.inflight
