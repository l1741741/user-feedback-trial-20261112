"""Tests for the opt-in per-item isolation of feed exports."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import pytest

from scrapy import Spider
from scrapy.exceptions import NotConfigured
from scrapy.exporters import CsvItemExporter, JsonLinesItemExporter
from scrapy.extensions.feedexport import FeedExporter
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test
from tests.utils.feedexport import crawl_items, unique_path

if TYPE_CHECKING:
    from tests.mockserver.http import MockServer


class _Unencodable:
    """Value that the JSON encoder cannot serialize, mimicking a field whose
    conversion fails."""

    def __str__(self) -> str:
        raise ValueError("not convertible to text")


class FieldFailureExporter(JsonLinesItemExporter):
    """Exporter that raises while converting a specific field."""

    def serialize_field(self, field: Any, name: str, value: Any) -> Any:
        if name == "broken":
            raise RuntimeError("field conversion exploded")
        return super().serialize_field(field, name, value)


class CsvFieldFailureExporter(CsvItemExporter):
    """CSV exporter that fails on a field value. CSV wraps the output in an
    io.TextIOWrapper, which probes the full file object protocol."""

    def serialize_field(self, field: Any, name: str, value: Any) -> Any:
        if value is _CSV_BAD:
            raise RuntimeError("csv field conversion exploded")
        return super().serialize_field(field, name, value)


_CSV_BAD = object()


class _FailingSink:
    """File-like object whose write-side methods fail like a full disk."""

    def __init__(self) -> None:
        self.closed = False

    def write(self, data: bytes) -> int:
        raise OSError(28, "No space left on device")

    def writelines(self, lines: Any) -> None:
        raise OSError(28, "No space left on device")

    def flush(self) -> None:
        raise OSError(28, "No space left on device")

    def tell(self) -> int:
        return 0

    def seek(self, *args: Any) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def writable(self) -> bool:
        return True


class DiskFullStorage:
    """Storage whose output file rejects every write."""

    supported_modes = frozenset({"append", "create", "overwrite"})

    def __init__(self, uri: str, *, feed_options: dict[str, Any] | None = None):
        self.uri = uri

    def open(self, spider: Any) -> Any:
        return _FailingSink()

    def store(self, file: IO[Any]) -> None:
        file.close()


class BrokenCompressor:
    """Post-processing plugin whose write path is corrupted."""

    def __init__(self, file: IO[Any], feed_options: dict[str, Any]):
        self.file = file

    def write(self, data: bytes) -> int:
        raise RuntimeError("compressor stream corrupted")

    def close(self) -> None:
        self.file.close()


def _read_manifest(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _storage_overrides(storage_cls: type) -> dict[str, Any]:
    # Absolute paths resolve to the empty-scheme storage, which is distinct
    # from the "file" key in FEED_STORAGES_BASE.
    return {"": storage_cls, "file": storage_cls}


class TestIsolationDisabledByDefault:
    @coroutine_test
    async def test_default_behaviour_raises(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        """Without the option, a serialization failure keeps propagating and
        no rejection manifest is created."""
        feed = unique_path(tmp_path)
        settings = {
            "FEEDS": {feed: {"format": "jsonlines"}},
            "FEED_EXPORTERS": {"jsonlines": FieldFailureExporter},
        }
        crawler = await crawl_items(mockserver, [{"id": 1, "broken": "x"}], settings)
        assert not Path(str(feed) + ".rejected.jsonl").exists()
        assert (
            "feedexport/rejected_count/FileFeedStorage" not in crawler.stats.get_stats()
        )

    def test_default_setting_is_off(self) -> None:
        crawler = get_crawler(
            settings_dict={"FEEDS": {"x.jsonl": {"format": "jsonlines"}}}
        )
        exporter = FeedExporter(crawler)
        assert exporter.feeds["x.jsonl"]["isolate_invalid_items"] is False


class TestIsolation:
    @coroutine_test
    async def test_bad_item_isolated_good_items_continue(
        self, caplog: pytest.LogCaptureFixture, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                feed: {
                    "format": "jsonlines",
                    "isolate_invalid_items": True,
                }
            },
            "FEED_EXPORTERS": {"jsonlines": FieldFailureExporter},
        }
        items = [
            {"id": 1, "ok": "first"},
            {"id": 2, "broken": "x"},
            {"id": 3, "ok": "third"},
        ]
        with caplog.at_level(logging.WARNING):
            crawler = await crawl_items(mockserver, items, settings)

        assert [json.loads(line) for line in Path(feed).read_text().splitlines()] == [
            {"id": 1, "ok": "first"},
            {"id": 3, "ok": "third"},
        ]
        manifest = _read_manifest(Path(str(feed) + ".rejected.jsonl"))
        assert manifest == [
            {
                "batch_id": 1,
                "item_index": 2,
                "field_path": "broken",
                "exception_class": "RuntimeError",
                "exception_module": "builtins",
                "item_id": {"id": 2},
            }
        ]
        stats = crawler.stats.get_stats()
        assert stats["feedexport/attempted_count/FileFeedStorage"] == 3
        assert stats["feedexport/item_count/FileFeedStorage"] == 2
        assert stats["feedexport/rejected_count/FileFeedStorage"] == 1
        assert "Isolated item #2" in caplog.text
        # The manifest never embeds the item or a repr address.
        assert "0x" not in Path(str(feed) + ".rejected.jsonl").read_text()
        assert "broken" in caplog.text

    @coroutine_test
    async def test_consecutive_bad_items(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                feed: {
                    "format": "jsonlines",
                    "isolate_invalid_items": True,
                    "item_id_fields": ("id",),
                }
            },
            "FEED_EXPORTERS": {"jsonlines": FieldFailureExporter},
        }
        items = [
            {"id": 10, "broken": "a"},
            {"id": 11, "broken": "b"},
            {"id": 12, "ok": "fine"},
        ]
        crawler = await crawl_items(mockserver, items, settings)
        manifest = _read_manifest(Path(str(feed) + ".rejected.jsonl"))
        assert [entry["item_index"] for entry in manifest] == [1, 2]
        assert [entry["item_id"] for entry in manifest] == [
            {"id": 10},
            {"id": 11},
        ]
        stats = crawler.stats.get_stats()
        assert stats["feedexport/attempted_count/FileFeedStorage"] == 3
        assert stats["feedexport/item_count/FileFeedStorage"] == 1
        assert stats["feedexport/rejected_count/FileFeedStorage"] == 2

    @coroutine_test
    async def test_encoding_failure_field_path_and_safe_ids(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        """A failure at the encoding stage is isolated too, and identifier
        values that are not JSON-serializable are dropped from the record."""
        feed = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                feed: {
                    "format": "jsonlines",
                    "isolate_invalid_items": True,
                    "item_id_fields": ("id", "secret"),
                }
            }
        }
        crawler = await crawl_items(
            mockserver,
            [
                {"id": 1, "secret": "shh", "payload": _Unencodable()},
                {"id": 2, "secret": _Unencodable(), "ok": "good"},
            ],
            settings,
        )
        manifest = _read_manifest(Path(str(feed) + ".rejected.jsonl"))
        # The first item fails while the payload field is being converted,
        # which the serializer hook attributes precisely. The second fails
        # later, when the JSON encoder walks the already-serialized fields,
        # so its path is best effort rather than exact.
        assert manifest[0]["field_path"] == "payload"
        assert manifest[1]["field_path"] is not None
        assert all(entry["exception_class"] == "TypeError" for entry in manifest)
        # JSON-safe identifier values are kept; non-serializable ones are
        # omitted rather than crashing the manifest or leaking an object.
        assert [entry["item_id"] for entry in manifest] == [
            {"id": 1, "secret": "shh"},
            {"id": 2},
        ]
        assert crawler.stats.get_value("feedexport/item_count/FileFeedStorage") == 0
        assert crawler.stats.get_value("feedexport/rejected_count/FileFeedStorage") == 2

    @coroutine_test
    async def test_no_rejection_file_when_all_items_valid(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed = unique_path(tmp_path)
        settings = {
            "FEEDS": {feed: {"format": "jsonlines", "isolate_invalid_items": True}}
        }
        await crawl_items(mockserver, [{"id": 1}, {"id": 2}], settings)
        assert not Path(str(feed) + ".rejected.jsonl").exists()

    @coroutine_test
    async def test_all_items_rejected_keeps_manifest_drops_empty_feed(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                feed: {
                    "format": "jsonlines",
                    "isolate_invalid_items": True,
                    "store_empty": False,
                }
            },
            "FEED_EXPORTERS": {"jsonlines": FieldFailureExporter},
        }
        crawler = await crawl_items(
            mockserver, [{"id": 1, "broken": "a"}, {"id": 2, "broken": "b"}], settings
        )
        assert not Path(feed).exists()
        manifest = _read_manifest(Path(str(feed) + ".rejected.jsonl"))
        assert [entry["item_id"] for entry in manifest] == [{"id": 1}, {"id": 2}]
        assert "feedexport/item_count/FileFeedStorage" not in crawler.stats.get_stats()
        assert crawler.stats.get_value("feedexport/rejected_count/FileFeedStorage") == 2
        assert (
            crawler.stats.get_value("feedexport/attempted_count/FileFeedStorage") == 2
        )


class TestExporterFormats:
    @coroutine_test
    async def test_csv_text_io_wrapper_sink(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        """Exporters that wrap the output in io.TextIOWrapper (CSV) probe the
        whole file protocol; the sink marker must keep delegating it."""
        feed = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                feed: {
                    "format": "csv",
                    "isolate_invalid_items": True,
                    "fields": ["id", "value"],
                }
            },
            "FEED_EXPORTERS": {"csv": CsvFieldFailureExporter},
        }
        items = [
            {"id": 1, "value": "good"},
            {"id": 2, "value": _CSV_BAD},
            {"id": 3, "value": "good3"},
        ]
        crawler = await crawl_items(mockserver, items, settings)
        rows = list(csv.DictReader(Path(feed).read_text().splitlines()))
        assert rows == [
            {"id": "1", "value": "good"},
            {"id": "3", "value": "good3"},
        ]
        manifest = _read_manifest(Path(str(feed) + ".rejected.jsonl"))
        assert [entry["item_id"] for entry in manifest] == [{"id": 2}]
        assert manifest[0]["field_path"] == "value"
        assert crawler.stats.get_value("feedexport/item_count/FileFeedStorage") == 2
        assert crawler.stats.get_value("feedexport/rejected_count/FileFeedStorage") == 1


class TestBatchBoundaries:
    @coroutine_test
    async def test_batch_item_count_counts_successes_only(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        batch_dir = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                batch_dir / "%(batch_id)d.jsonl": {
                    "format": "jsonlines",
                    "isolate_invalid_items": True,
                    "batch_item_count": 2,
                }
            },
            "FEED_EXPORTERS": {"jsonlines": FieldFailureExporter},
        }
        items = [
            {"id": 1, "ok": "a"},
            {"id": 2, "broken": "x"},
            {"id": 3, "ok": "b"},  # second success closes batch 1
            {"id": 4, "broken": "y"},
            {"id": 5, "ok": "c"},
        ]
        await crawl_items(mockserver, items, settings)

        batch_files = sorted(
            p
            for p in batch_dir.iterdir()
            if p.name.endswith(".jsonl") and ".rejected." not in p.name
        )
        assert [p.name for p in batch_files] == ["1.jsonl", "2.jsonl"]
        assert [
            json.loads(line) for line in batch_files[0].read_text().splitlines()
        ] == [
            {"id": 1, "ok": "a"},
            {"id": 3, "ok": "b"},
        ]
        assert [
            json.loads(line) for line in batch_files[1].read_text().splitlines()
        ] == [{"id": 5, "ok": "c"}]
        # Each batch gets its own manifest with per-batch item indices.
        manifest_1 = _read_manifest(batch_dir / "1.jsonl.rejected.jsonl")
        manifest_2 = _read_manifest(batch_dir / "2.jsonl.rejected.jsonl")
        assert [(m["batch_id"], m["item_index"]) for m in manifest_1] == [(1, 2)]
        assert [(m["batch_id"], m["item_index"]) for m in manifest_2] == [(2, 1)]


class TestOutputChainFailures:
    @coroutine_test
    async def test_disk_full_is_not_quarantined(
        self, caplog: pytest.LogCaptureFixture, mockserver: MockServer, tmp_path: Path
    ) -> None:
        """A failure while writing encoded bytes (full disk) must propagate
        instead of being recorded as a bad item."""
        feed = unique_path(tmp_path)
        settings = {
            "FEEDS": {feed: {"format": "jsonlines", "isolate_invalid_items": True}},
            "FEED_STORAGES": _storage_overrides(DiskFullStorage),
        }
        with caplog.at_level(logging.ERROR):
            crawler = await crawl_items(mockserver, [{"id": 1, "ok": "a"}], settings)
        assert not Path(str(feed) + ".rejected.jsonl").exists()
        # The item was attempted, but nothing was recorded as a bad item:
        # the write failure is not an item failure.
        assert (
            crawler.stats.get_value("feedexport/attempted_count/DiskFullStorage") == 1
        )
        assert (
            "feedexport/rejected_count/DiskFullStorage" not in crawler.stats.get_stats()
        )
        assert "No space left on device" in caplog.text
        assert "Error caught on signal handler" in caplog.text

    @coroutine_test
    async def test_broken_compressor_is_not_quarantined(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        """A corrupted compression plugin fails on the write path, so the
        error must escape item isolation."""
        feed = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                feed: {
                    "format": "jsonlines",
                    "isolate_invalid_items": True,
                    "postprocessing": [BrokenCompressor],
                }
            },
        }
        crawler = await crawl_items(mockserver, [{"id": 1, "ok": "a"}], settings)
        assert not Path(str(feed) + ".rejected.jsonl").exists()
        assert (
            "feedexport/rejected_count/FileFeedStorage" not in crawler.stats.get_stats()
        )
        assert (
            crawler.stats.get_value("feedexport/attempted_count/FileFeedStorage") == 1
        )

    @coroutine_test
    async def test_storage_store_failure_is_reported_not_quarantined(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        """A failure of storage.store() at close time is a storage failure,
        counted as a failed delivery, never as a bad item."""

        class FailingStoreStorage:
            supported_modes = frozenset({"append", "create", "overwrite"})

            def __init__(self, uri: str, *, feed_options: dict[str, Any] | None = None):
                self.path = Path(uri)

            def open(self, spider: Any) -> IO[Any]:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                return self.path.open("wb")

            def store(self, file: IO[Any]) -> None:
                file.close()
                raise OSError("storage backend on fire")

        feed = unique_path(tmp_path)
        settings = {
            "FEEDS": {feed: {"format": "jsonlines", "isolate_invalid_items": True}},
            "FEED_STORAGES": _storage_overrides(FailingStoreStorage),
        }
        crawler = await crawl_items(mockserver, [{"id": 1, "ok": "a"}], settings)
        assert not Path(str(feed) + ".rejected.jsonl").exists()
        assert (
            crawler.stats.get_value("feedexport/failed_count/FailingStoreStorage") == 1
        )
        # The item serialized and was written successfully; only the final
        # store() call failed, so it is not a rejected item.
        assert (
            "feedexport/rejected_count/FailingStoreStorage"
            not in crawler.stats.get_stats()
        )
        assert (
            crawler.stats.get_value("feedexport/attempted_count/FailingStoreStorage")
            == 1
        )


class TestMultipleFeedsAndConfiguration:
    @coroutine_test
    async def test_slots_keep_independent_counters(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_a = unique_path(tmp_path)
        feed_b = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                feed_a: {"format": "jsonlines", "isolate_invalid_items": True},
                feed_b: {"format": "jsonlines", "isolate_invalid_items": True},
            },
            "FEED_EXPORTERS": {"jsonlines": FieldFailureExporter},
        }
        crawler = await crawl_items(
            mockserver,
            [
                {"id": 1, "ok": "a"},
                {"id": 2, "broken": "x"},
            ],
            settings,
        )
        for feed in (feed_a, feed_b):
            manifest = _read_manifest(Path(str(feed) + ".rejected.jsonl"))
            assert [entry["item_id"] for entry in manifest] == [{"id": 2}]
        # One storage type, but two slots each attempted two items.
        assert (
            crawler.stats.get_value("feedexport/attempted_count/FileFeedStorage") == 4
        )
        assert crawler.stats.get_value("feedexport/item_count/FileFeedStorage") == 2
        assert crawler.stats.get_value("feedexport/rejected_count/FileFeedStorage") == 2

    @coroutine_test
    async def test_isolated_and_plain_feed_coexist(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        """A bad item is isolated from the isolating feed but still aborts
        export to the feed where the option is off."""
        isolated_feed = unique_path(tmp_path)
        plain_feed = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                isolated_feed: {
                    "format": "jsonlines",
                    "isolate_invalid_items": True,
                },
                plain_feed: {"format": "jsonlines"},
            },
            "FEED_EXPORTERS": {"jsonlines": FieldFailureExporter},
        }
        crawler = await crawl_items(
            mockserver, [{"id": 1, "broken": "x"}, {"id": 2, "ok": "y"}], settings
        )
        # The isolating feed still delivers the good item, even though the
        # same item aborts the plain feed through the signal machinery.
        isolated_rows = [
            json.loads(line) for line in Path(isolated_feed).read_text().splitlines()
        ]
        assert isolated_rows == [{"id": 2, "ok": "y"}]
        assert Path(str(isolated_feed) + ".rejected.jsonl").exists()
        # Only the isolating feed recorded a rejection.
        assert crawler.stats.get_value("feedexport/rejected_count/FileFeedStorage") == 1

    def test_non_local_feed_requires_local_manifest(self, tmp_path: Path) -> None:
        settings = {
            "FEEDS": {
                "s3://bucket/feed.jsonl": {
                    "format": "jsonlines",
                    "isolate_invalid_items": True,
                }
            }
        }
        crawler = get_crawler(settings_dict=settings)
        with pytest.raises(NotConfigured):
            FeedExporter(crawler)

    @coroutine_test
    async def test_explicit_rejection_log_uri(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed = unique_path(tmp_path)
        manifest = unique_path(tmp_path) / "custom-rejections.jsonl"
        settings = {
            "FEEDS": {
                feed: {
                    "format": "jsonlines",
                    "isolate_invalid_items": True,
                    "rejection_log_uri": str(manifest),
                }
            },
            "FEED_EXPORTERS": {"jsonlines": FieldFailureExporter},
        }
        await crawl_items(mockserver, [{"id": 1, "broken": "x"}], settings)
        entries = _read_manifest(manifest)
        assert entries[0]["item_id"] == {"id": 1}
        # The default sibling manifest was not created.
        assert not Path(str(feed) + ".rejected.jsonl").exists()


class TestSpiderStats:
    @coroutine_test
    async def test_stats_present_with_real_spider(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        class MixedSpider(Spider):
            name = "mixed"
            start_urls = [mockserver.url("/")]

            def parse(self, response: Any) -> Any:
                yield {"id": 1, "ok": "a"}
                yield {"id": 2, "broken": "x"}
                yield {"id": 3, "ok": "b"}

        feed = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                feed: {
                    "format": "jsonlines",
                    "isolate_invalid_items": True,
                }
            },
            "FEED_EXPORTERS": {"jsonlines": FieldFailureExporter},
        }
        crawler = get_crawler(MixedSpider, settings)
        await crawler.crawl_async()
        stats = crawler.stats.get_stats()
        attempted = stats["feedexport/attempted_count/FileFeedStorage"]
        succeeded = stats["feedexport/item_count/FileFeedStorage"]
        rejected = stats["feedexport/rejected_count/FileFeedStorage"]
        assert attempted == succeeded + rejected
        assert rejected == 1
        assert succeeded == 2
        assert attempted == 3
