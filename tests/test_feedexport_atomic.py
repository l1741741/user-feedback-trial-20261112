"""Tests for the ``atomic`` feed option of the local filesystem storage."""

from __future__ import annotations

import gzip
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

import pytest

import scrapy
from scrapy import signals
from scrapy.exporters import JsonItemExporter
from scrapy.extensions.feedexport import FeedExporter, FileFeedStorage
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test
from tests.utils.feedexport import (
    MyItem,
    crawl_items,
    path_to_url,
    printf_escape,
    unique_path,
)

if TYPE_CHECKING:
    from tests.mockserver.http import MockServer


class FailingGzipPlugin:
    """Post-processing plugin whose close() always fails."""

    def __init__(self, file: Any, feed_options: dict[str, Any]) -> None:
        self.file = file

    def write(self, data: bytes) -> int:
        return cast("int", self.file.write(data))

    def close(self) -> None:
        raise OSError("compression failed")


class FailingFinishJsonExporter(JsonItemExporter):
    """JsonItemExporter whose finish_exporting() always fails."""

    def finish_exporting(self) -> None:
        raise RuntimeError("cannot finalize the export")


class SlotCloseRecorder:
    """Strongly-referenced receiver for feed_slot_closed.

    pydispatch connects receivers through weak references, so a local function
    or lambda can be garbage-collected before the signal is sent from an
    asyncio task. Tests keep an instance of this class alive instead.
    """

    def __init__(self) -> None:
        self.slots: list[Any] = []

    def __call__(self, slot: Any) -> None:
        self.slots.append(slot)


class TestAtomicFileFeedStorageUnit:
    """Direct tests of FileFeedStorage in atomic mode."""

    @staticmethod
    def _storage(path: Path, **feed_options: Any) -> FileFeedStorage:
        options = {"mode": "overwrite", "atomic": True, **feed_options}
        return FileFeedStorage(str(path), feed_options=options)

    def test_atomic_requires_overwrite_mode(self, tmp_path: Path) -> None:
        path = tmp_path / "feed.json"
        for options in (
            {"atomic": True, "mode": "append"},
            {"atomic": True, "mode": "create"},
            {"atomic": True},
        ):
            with pytest.raises(ValueError, match="requires the 'overwrite'"):
                FileFeedStorage(str(path), feed_options=options)

    def test_atomic_with_legacy_overwrite_option(self, tmp_path: Path) -> None:
        path = tmp_path / "feed.json"
        storage = FileFeedStorage(
            str(path), feed_options={"atomic": True, "overwrite": True}
        )
        assert storage.atomic is True

    @pytest.mark.parametrize("feed_options", [None, {}, {"atomic": False}])
    def test_atomic_defaults_to_off(
        self, tmp_path: Path, feed_options: dict[str, Any] | None
    ) -> None:
        storage = FileFeedStorage(
            str(tmp_path / "feed.json"), feed_options=feed_options
        )
        assert storage.atomic is False

    def test_writes_to_temp_file_and_replaces_target(self, tmp_path: Path) -> None:
        path = tmp_path / "feed.json"
        storage = self._storage(path)
        spider = scrapy.Spider("default")

        file = storage.open(spider)
        assert not path.exists()
        temp_path = Path(file.name)
        assert temp_path.parent == tmp_path
        file.write(b"content")
        storage.store(file)

        assert path.read_bytes() == b"content"
        assert not temp_path.exists()
        assert not list(tmp_path.glob(".*.tmp"))

    def test_missing_parent_directory_is_created(self, tmp_path: Path) -> None:
        path = tmp_path / "more" / "paths" / "feed.json"
        storage = self._storage(path)
        file = storage.open(scrapy.Spider("default"))
        file.write(b"content")
        storage.store(file)
        assert path.read_bytes() == b"content"

    def test_failed_replace_keeps_target_and_removes_temp(self, tmp_path: Path) -> None:
        path = tmp_path / "feed.json"
        path.write_bytes(b"old content")
        storage = self._storage(path)
        file = storage.open(scrapy.Spider("default"))
        temp_path = Path(file.name)
        file.write(b"new content")

        with (
            mock.patch.object(Path, "replace", side_effect=OSError("nope")),
            pytest.raises(OSError, match="nope"),
        ):
            storage.store(file)

        assert path.read_bytes() == b"old content"
        assert not temp_path.exists()
        assert file.closed

    def test_failed_close_keeps_target_and_removes_temp(self, tmp_path: Path) -> None:
        path = tmp_path / "feed.json"
        path.write_bytes(b"old content")
        storage = self._storage(path)
        file = storage.open(scrapy.Spider("default"))
        temp_path = Path(file.name)
        file.write(b"new content")
        real_close = file.close
        calls = 0

        def fail_first_close() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("disk gone")
            real_close()

        file.close = fail_first_close  # type: ignore[method-assign]

        with pytest.raises(OSError, match="disk gone"):
            storage.store(file)

        assert path.read_bytes() == b"old content"
        assert not temp_path.exists()
        assert file.closed

    def test_abort_discards_temp_file(self, tmp_path: Path) -> None:
        path = tmp_path / "feed.json"
        storage = self._storage(path)
        file = storage.open(scrapy.Spider("default"))
        temp_path = Path(file.name)
        file.write(b"content")

        storage._abort(file)

        assert not path.exists()
        assert not temp_path.exists()
        assert file.closed

    def test_abort_is_safe_without_open(self, tmp_path: Path) -> None:
        storage = self._storage(tmp_path / "feed.json")
        storage._temp_path = None
        storage._abort(mock.Mock(closed=True))

    def test_non_atomic_storage_touches_target_directly(self, tmp_path: Path) -> None:
        path = tmp_path / "feed.json"
        storage = FileFeedStorage(
            str(path), feed_options={"mode": "overwrite", "atomic": False}
        )
        file = storage.open(scrapy.Spider("default"))
        assert Path(file.name) == path
        file.write(b"content")
        storage.store(file)
        assert path.read_bytes() == b"content"
        assert not list(tmp_path.glob(".*.tmp"))


class TestAtomicFeedExport:
    """End-to-end tests through the FeedExporter extension."""

    @coroutine_test
    async def test_successful_export_replaces_target(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        path = unique_path(tmp_path)
        path.write_bytes(b"old content")
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(path))): {
                    "format": "json",
                    "mode": "overwrite",
                    "atomic": True,
                }
            }
        }
        crawler = await crawl_items(mockserver, [MyItem({"foo": "bar"})], settings)
        assert path.read_bytes() != b"old content"
        assert b"bar" in path.read_bytes()
        assert not list(tmp_path.glob(".*.tmp"))
        assert crawler.stats.get_value("feedexport/success_count/FileFeedStorage") == 1
        assert (
            "feedexport/failed_count/FileFeedStorage" not in crawler.stats.get_stats()
        )

    @coroutine_test
    async def test_failed_export_keeps_target_and_cleans_up(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        path = unique_path(tmp_path)
        path.write_bytes(b"old content")
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(path))): {
                    "format": "json",
                    "mode": "overwrite",
                    "atomic": True,
                }
            }
        }
        crawler = get_crawler(settings_dict=settings)

        def fail_replace(self: Path, target: Any) -> None:
            raise OSError("cannot replace")

        with mock.patch.object(Path, "replace", fail_replace):
            await crawler.crawl_async(mockserver=mockserver)

        assert path.read_bytes() == b"old content"
        assert not list(tmp_path.glob(".*.tmp"))
        assert crawler.stats.get_value("feedexport/failed_count/FileFeedStorage") == 1
        assert (
            "feedexport/success_count/FileFeedStorage" not in crawler.stats.get_stats()
        )

    @coroutine_test
    async def test_first_export_failure_leaves_no_target(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        path = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(path))): {
                    "format": "json",
                    "mode": "overwrite",
                    "atomic": True,
                }
            }
        }
        crawler = get_crawler(settings_dict=settings)

        with mock.patch.object(Path, "replace", side_effect=OSError("cannot replace")):
            await crawler.crawl_async(mockserver=mockserver)

        assert not path.exists()
        assert not list(tmp_path.glob(".*.tmp"))
        assert crawler.stats.get_value("feedexport/failed_count/FileFeedStorage") == 1

    @coroutine_test
    async def test_postprocessing_failure_keeps_target(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        path = unique_path(tmp_path)
        path.write_bytes(b"old content")
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(path))): {
                    "format": "json",
                    "mode": "overwrite",
                    "atomic": True,
                    "postprocessing": [
                        f"{FailingGzipPlugin.__module__}.{FailingGzipPlugin.__name__}"
                    ],
                }
            }
        }
        crawler = await crawl_items(mockserver, [{"foo": "bar"}], settings)

        assert path.read_bytes() == b"old content"
        assert not list(tmp_path.glob(".*.tmp"))
        assert crawler.stats.get_value("feedexport/failed_count/FileFeedStorage") == 1
        assert (
            "feedexport/success_count/FileFeedStorage" not in crawler.stats.get_stats()
        )

    @coroutine_test
    async def test_exporter_finish_failure_keeps_target(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        path = unique_path(tmp_path)
        path.write_bytes(b"old content")
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(path))): {
                    "format": "json",
                    "mode": "overwrite",
                    "atomic": True,
                }
            },
            "FEED_EXPORTERS": {"json": FailingFinishJsonExporter},
        }
        crawler = await crawl_items(mockserver, [{"foo": "bar"}], settings)

        assert path.read_bytes() == b"old content"
        assert not list(tmp_path.glob(".*.tmp"))
        assert crawler.stats.get_value("feedexport/failed_count/FileFeedStorage") == 1
        assert (
            "feedexport/success_count/FileFeedStorage" not in crawler.stats.get_stats()
        )

    @coroutine_test
    async def test_write_failure_leaves_no_target(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        class FailingWriteStorage(FileFeedStorage):
            def open(self, spider: scrapy.Spider):
                file = super().open(spider)
                file.write = mock.Mock(  # type: ignore[method-assign]
                    side_effect=OSError("disk full")
                )
                return file

        path = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(path))): {
                    "format": "json",
                    "mode": "overwrite",
                    "atomic": True,
                }
            },
            "FEED_STORAGES": {"file": FailingWriteStorage},
        }
        crawler = await crawl_items(mockserver, [{"foo": "bar"}], settings)

        assert not path.exists()
        assert not list(tmp_path.glob(".*.tmp"))
        assert (
            crawler.stats.get_value("feedexport/failed_count/FailingWriteStorage") == 1
        )

    @coroutine_test
    async def test_gzip_postprocessing_is_delivered(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        path = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(path))): {
                    "format": "jsonlines",
                    "mode": "overwrite",
                    "atomic": True,
                    "postprocessing": ["scrapy.extensions.postprocessing.GzipPlugin"],
                }
            }
        }
        await crawl_items(mockserver, [{"foo": "bar"}], settings)
        assert gzip.decompress(path.read_bytes()) == b'{"foo": "bar"}\n'
        assert not list(tmp_path.glob(".*.tmp"))

    @coroutine_test
    async def test_empty_feed_is_stored_atomically(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        path = unique_path(tmp_path)
        path.write_bytes(b"old content")
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(path))): {
                    "format": "json",
                    "mode": "overwrite",
                    "atomic": True,
                }
            },
            "FEED_STORE_EMPTY": True,
        }
        crawler = await crawl_items(mockserver, [], settings)
        assert path.read_bytes() == b"[\n\n]"
        assert not list(tmp_path.glob(".*.tmp"))
        assert crawler.stats.get_value("feedexport/success_count/FileFeedStorage") == 1

    @coroutine_test
    async def test_empty_feed_not_stored_leaves_target_untouched(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        path = unique_path(tmp_path)
        path.write_bytes(b"old content")
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(path))): {
                    "format": "json",
                    "mode": "overwrite",
                    "atomic": True,
                }
            },
            "FEED_STORE_EMPTY": False,
        }
        await crawl_items(mockserver, [], settings)
        assert path.read_bytes() == b"old content"
        assert not list(tmp_path.glob(".*.tmp"))

    @coroutine_test
    async def test_batches_commit_independently(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        directory = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                str(directory / "%(batch_id)d.json"): {
                    "format": "json",
                    "mode": "overwrite",
                    "atomic": True,
                }
            },
            "FEED_EXPORT_BATCH_ITEM_COUNT": 1,
        }
        items = [{"foo": "bar1"}, {"foo": "bar2"}, {"foo": "bar3"}]

        real_replace = Path.replace
        failed: list[bool] = []

        def fail_second_batch(self: Path, target: Any) -> Any:
            if Path(target).name == "2.json" and not failed:
                failed.append(True)
                raise OSError("cannot replace batch 2")
            return real_replace(self, target)

        with mock.patch.object(Path, "replace", fail_second_batch):
            crawler = await crawl_items(mockserver, items, settings)

        batch1 = directory / "1.json"
        batch2 = directory / "2.json"
        batch3 = directory / "3.json"
        assert batch1.read_bytes() == b'[\n{"foo": "bar1"}\n]'
        assert not batch2.exists()
        assert batch3.read_bytes() == b'[\n{"foo": "bar3"}\n]'
        assert not list(directory.glob(".*.tmp"))
        stats = crawler.stats.get_stats()
        assert stats.get("feedexport/success_count/FileFeedStorage") == 2
        assert stats.get("feedexport/failed_count/FileFeedStorage") == 1

    @coroutine_test
    async def test_slot_closed_signal_not_sent_on_failure(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        path = unique_path(tmp_path)
        path.write_bytes(b"old content")
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(path))): {
                    "format": "json",
                    "mode": "overwrite",
                    "atomic": True,
                }
            }
        }
        recorder = SlotCloseRecorder()
        crawler = get_crawler(settings_dict=settings)
        crawler.signals.connect(
            recorder,
            signal=signals.feed_slot_closed,
        )
        with mock.patch.object(Path, "replace", side_effect=OSError("cannot replace")):
            await crawler.crawl_async(mockserver=mockserver)
        assert recorder.slots == []
        assert path.read_bytes() == b"old content"

    @coroutine_test
    async def test_slot_closed_signal_sent_on_commit(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        path = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(path))): {
                    "format": "json",
                    "mode": "overwrite",
                    "atomic": True,
                }
            }
        }
        recorder = SlotCloseRecorder()
        crawler = get_crawler(settings_dict=settings)
        feed_exporter = build_from_crawler(FeedExporter, crawler)
        spider = scrapy.Spider.from_crawler(crawler, "default")
        crawler.signals.connect(
            recorder,
            signal=signals.feed_slot_closed,
        )
        feed_exporter.open_spider(spider)
        await feed_exporter.item_scraped(MyItem({"foo": "bar"}), spider)
        await feed_exporter.close_spider(spider)

        assert len(recorder.slots) == 1
        assert b"bar" in path.read_bytes()

    @coroutine_test
    async def test_concurrent_slots_keep_their_temp_files(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        paths = [unique_path(tmp_path) for _ in range(3)]
        for path in paths:
            path.write_bytes(b"old content")
        settings = {
            "FEEDS": {
                printf_escape(path_to_url(str(path))): {
                    "format": "json",
                    "mode": "overwrite",
                    "atomic": True,
                }
                for path in paths
            }
        }
        crawler = await crawl_items(
            mockserver, [{"foo": f"bar{index}"} for index in range(3)], settings
        )
        for index, path in enumerate(paths):
            assert f"bar{index}".encode() in path.read_bytes()
        assert not list(tmp_path.glob(".*.tmp"))
        assert crawler.stats.get_value("feedexport/success_count/FileFeedStorage") == 3


class TestAtomicFeedExportConfig:
    def test_atomic_rejected_with_append_mode(self) -> None:
        settings = {
            "FEEDS": {
                "file:///tmp/items.json": {
                    "format": "json",
                    "mode": "append",
                    "atomic": True,
                }
            }
        }
        with pytest.raises(ValueError, match="requires the 'overwrite'"):
            get_crawler(settings_dict=settings)

    def test_atomic_rejected_with_create_mode(self) -> None:
        settings = {
            "FEEDS": {
                "file:///tmp/items.json": {
                    "format": "json",
                    "mode": "create",
                    "atomic": True,
                }
            }
        }
        with pytest.raises(ValueError, match="requires the 'overwrite'"):
            get_crawler(settings_dict=settings)

    def test_atomic_ignored_by_non_local_storage(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class StdoutLikeStorage:
            def __init__(self, uri: str, *, feed_options: Any = None) -> None:
                pass

        settings = {
            "FEEDS": {
                "custom://items.json": {
                    "format": "json",
                    "atomic": True,
                }
            },
            "FEED_STORAGES": {"custom": StdoutLikeStorage},
        }
        with caplog.at_level(logging.WARNING):
            get_crawler(settings_dict=settings)
        assert "atomic feed option" in caplog.text
        assert "ignored" in caplog.text

    def test_original_feeds_dict_is_not_modified(self, tmp_path: Path) -> None:
        feed_options = {
            "format": "json",
            "mode": "overwrite",
            "atomic": True,
        }
        feeds = {str(tmp_path / "items.json"): feed_options}
        original = repr(feeds)
        crawler = get_crawler(settings_dict={"FEEDS": feeds})
        build_from_crawler(FeedExporter, crawler)
        assert repr(feeds) == original
        assert feeds[str(tmp_path / "items.json")] is feed_options
        assert feed_options == {
            "format": "json",
            "mode": "overwrite",
            "atomic": True,
        }
