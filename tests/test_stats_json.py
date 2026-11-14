from __future__ import annotations

import base64
import gc
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

from scrapy.spiders import Spider
from scrapy.statscollectors import MemoryStatsCollector, StatsCollector
from scrapy.utils import _stats_json
from scrapy.utils.test import get_crawler
from tests.spiders import SimpleSpider
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    import pytest

    from scrapy.crawler import Crawler


def _make_crawler(tmp_path: Path, **settings: Any) -> Crawler:
    return get_crawler(
        Spider,
        {
            "STATS_DUMP": False,
            "STATS_JSON_PATH": str(tmp_path / "stats.json"),
            **settings,
        },
    )


def _make_collector(tmp_path: Path, **settings: Any) -> StatsCollector:
    crawler = _make_crawler(tmp_path, **settings)
    return StatsCollector(crawler)


def _read_snapshot(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


class TestStatsJsonSnapshot:
    def test_disabled_by_default(self, tmp_path: Path) -> None:
        crawler = get_crawler(Spider, {"STATS_DUMP": False})
        stats = StatsCollector(crawler)
        stats.set_value("a", 1)
        stats.close_spider(reason="finished")
        assert not any(tmp_path.iterdir())
        assert stats._json_snapshot_frozen is False

    def test_first_write(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "stats.json"
        crawler = _make_crawler(tmp_path)
        stats = StatsCollector(crawler)
        spider = crawler._create_spider("foo")
        crawler.spider = spider
        fixed_dt = datetime(2019, 12, 1, 11, 38, tzinfo=UTC)
        stats.set_value("count", 3)
        stats.set_value("start_time", fixed_dt)
        stats.set_value("naive_time", datetime(2020, 1, 1))
        stats.set_value("elapsed", timedelta(seconds=90, microseconds=500_000))
        stats.set_value("payload", b"\x00\xff")
        stats.set_value("nested", {"list": [1, ("a", "b")], "none": None})
        stats.set_value("unicode", "снег")

        with caplog.at_level(logging.WARNING):
            stats.close_spider(reason="finished")

        assert path.exists()
        assert caplog.records == []
        raw = path.read_bytes()
        assert raw.decode("utf-8")
        snapshot = json.loads(raw)
        assert snapshot == {
            "spider_name": "foo",
            "close_reason": "finished",
            "stats": {
                "count": 3,
                "start_time": "2019-12-01T11:38:00+00:00",
                "naive_time": "2020-01-01T00:00:00+00:00",
                "elapsed": 90.5,
                "payload": {
                    "__scrapy_bytes_base64__": base64.b64encode(b"\x00\xff").decode(
                        "ascii"
                    )
                },
                "nested": {"list": [1, ["a", "b"]], "none": None},
                "unicode": "снег",
            },
            "unsupported_types": [],
        }
        # Deterministic key order: every object's keys are sorted.
        decoded = raw.decode("utf-8")
        assert list(snapshot) == sorted(snapshot)
        assert '"close_reason"' in decoded
        assert (
            base64.b64decode(snapshot["stats"]["payload"]["__scrapy_bytes_base64__"])
            == b"\x00\xff"
        )
        # The temp file was cleaned up.
        assert list(tmp_path.glob("*.tmp")) == []
        assert stats._json_snapshot_frozen is True

    def test_overwrite_replaces_previous_snapshot(self, tmp_path: Path) -> None:
        path = tmp_path / "stats.json"
        path.write_text('{"old": true}', encoding="utf-8")
        stats = _make_collector(tmp_path)
        stats.set_value("count", 1)
        stats.close_spider(reason="finished")
        snapshot = _read_snapshot(path)
        assert snapshot["stats"] == {"count": 1}
        assert "old" not in snapshot

    def test_stats_are_independent_deep_copy(self, tmp_path: Path) -> None:
        stats = _make_collector(tmp_path)
        nested = {"inner": [1, 2]}
        stats.set_value("nested", nested)
        stats.close_spider(reason="finished")
        # Mutating the live stats after close must not touch the snapshot,
        # and the snapshot holds its own copy of nested containers.
        nested["inner"].append(3)
        stats.set_value("another", True)
        snapshot = _read_snapshot(tmp_path / "stats.json")
        assert snapshot["stats"] == {"nested": {"inner": [1, 2]}}

    def test_unsupported_type_warning_and_count(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        stats = _make_collector(tmp_path)
        obj = object()
        stats.set_value("good", 1)
        stats.set_value("bad", obj)
        stats.set_value("nested", {"also_bad": {1, 2}})
        with caplog.at_level(logging.WARNING):
            stats.close_spider(reason="finished")
        snapshot = _read_snapshot(tmp_path / "stats.json")
        assert snapshot["stats"]["good"] == 1
        assert snapshot["stats"]["bad"] is None
        assert snapshot["stats"]["nested"] == {"also_bad": None}
        assert snapshot["unsupported_types"] == [
            {"path": "stats.bad", "type": "builtins.object"},
            {"path": "stats.nested.also_bad", "type": "builtins.set"},
        ]
        messages = [r.getMessage() for r in caplog.records]
        assert any("stats.bad" in m and "builtins.object" in m for m in messages)
        assert any(
            "stats.nested.also_bad" in m and "builtins.set" in m for m in messages
        )

    def test_circular_reference(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        stats = _make_collector(tmp_path)
        cycle: dict[str, Any] = {}
        cycle["self"] = cycle
        stats.set_value("cycle", cycle)
        with caplog.at_level(logging.WARNING):
            stats.close_spider(reason="finished")
        snapshot = _read_snapshot(tmp_path / "stats.json")
        assert snapshot["stats"] == {"cycle": {"self": None}}
        assert snapshot["unsupported_types"] == [
            {"path": "stats.cycle.self", "type": "builtins.dict"}
        ]

    def test_no_reason_and_non_string_keys(self, tmp_path: Path) -> None:
        stats = _make_collector(tmp_path)
        stats.set_value("mapped", {1: "one", ("k",): "tuple-key"})
        stats.close_spider()
        snapshot = _read_snapshot(tmp_path / "stats.json")
        assert snapshot["close_reason"] is None
        assert snapshot["stats"]["mapped"] == {"1": "one", "('k',)": "tuple-key"}

    def test_repeated_close_keeps_frozen_snapshot(self, tmp_path: Path) -> None:
        stats = _make_collector(tmp_path)
        stats.set_value("count", 1)
        stats.close_spider(reason="finished")
        first_mtime = (tmp_path / "stats.json").stat().st_mtime_ns
        # A second close with changed stats must not overwrite the file.
        stats.set_value("count", 2)
        stats.close_spider(reason="finished again")
        path = tmp_path / "stats.json"
        assert path.stat().st_mtime_ns == first_mtime
        snapshot = _read_snapshot(path)
        assert snapshot["stats"] == {"count": 1}
        assert snapshot["close_reason"] == "finished"

    def test_failed_write_keeps_previous_snapshot_and_retries(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        stats = _make_collector(tmp_path)
        stats.set_value("count", 1)
        stats.close_spider(reason="finished")
        path = tmp_path / "stats.json"
        first = path.read_text(encoding="utf-8")

        # Simulate an os.replace failure on the next attempt after a reset:
        # the old file must remain valid and no temp file may be left behind.
        stats._json_snapshot_frozen = False
        stats.set_value("count", 2)
        with (
            mock.patch("os.replace", side_effect=OSError("boom")),
            caplog.at_level(logging.ERROR),
        ):
            stats.close_spider(reason="failed")
        assert path.read_text(encoding="utf-8") == first
        assert list(tmp_path.glob("*.tmp")) == []
        assert stats._json_snapshot_frozen is False
        assert any(
            "Could not write the JSON stats snapshot" in r.getMessage()
            for r in caplog.records
        )

        # Retry with the failure gone: the new snapshot now lands.
        stats.close_spider(reason="retried")
        snapshot = _read_snapshot(path)
        assert snapshot["stats"] == {"count": 2}
        assert snapshot["close_reason"] == "retried"
        assert stats._json_snapshot_frozen is True

    def test_serialization_failure_does_not_break_close(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        crawler = _make_crawler(tmp_path)
        stats = MemoryStatsCollector(crawler)
        crawler.spider = crawler._create_spider("foo")
        stats.set_value("kept", True)
        with (
            mock.patch.object(
                _stats_json, "_build_snapshot", side_effect=ValueError("boom")
            ),
            caplog.at_level(logging.ERROR),
        ):
            stats.close_spider(reason="finished")
        # Memory persistence still ran, and the failure was swallowed.
        assert stats.spider_stats == {"foo": {"kept": True}}
        assert stats._json_snapshot_frozen is False
        assert not (tmp_path / "stats.json").exists()
        assert not any(tmp_path.glob("*.tmp"))
        assert any(
            "Could not write the JSON stats snapshot" in r.getMessage()
            for r in caplog.records
        )
        # A later close retries the write, since no snapshot was frozen.
        stats.close_spider(reason="retried")
        snapshot = _read_snapshot(tmp_path / "stats.json")
        assert snapshot["close_reason"] == "retried"
        assert stats._json_snapshot_frozen is True

    def test_fsync_failure_cleans_up_temp_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        stats = _make_collector(tmp_path)
        stats.set_value("a", 1)
        with (
            mock.patch("os.fsync", side_effect=OSError("boom")),
            caplog.at_level(logging.ERROR),
        ):
            stats.close_spider(reason="finished")
        assert not (tmp_path / "stats.json").exists()
        assert list(tmp_path.glob("*.tmp")) == []
        assert stats._json_snapshot_frozen is False

    def test_temp_file_open_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        stats = _make_collector(tmp_path)
        with (
            mock.patch.object(
                _stats_json, "NamedTemporaryFile", side_effect=OSError("boom")
            ),
            caplog.at_level(logging.ERROR),
        ):
            stats.close_spider(reason="finished")
        assert not (tmp_path / "stats.json").exists()
        assert stats._json_snapshot_frozen is False

    def test_missing_parent_directory_fails_cleanly(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        missing = tmp_path / "no-such-dir" / "stats.json"
        crawler = get_crawler(
            Spider,
            {"STATS_DUMP": False, "STATS_JSON_PATH": str(missing)},
        )
        stats = StatsCollector(crawler)
        with caplog.at_level(logging.ERROR):
            stats.close_spider(reason="finished")
        assert not missing.exists()
        assert stats._json_snapshot_frozen is False

    def test_spider_placeholder(self, tmp_path: Path) -> None:
        path_template = str(tmp_path / "%(spider)s-stats.json")
        crawler = get_crawler(
            Spider,
            {"STATS_DUMP": False, "STATS_JSON_PATH": path_template},
        )
        stats = StatsCollector(crawler)
        crawler.spider = crawler._create_spider("foo")
        stats.set_value("a", 1)
        stats.close_spider(reason="finished")
        assert (tmp_path / "foo-stats.json").exists()
        snapshot = _read_snapshot(tmp_path / "foo-stats.json")
        assert snapshot["spider_name"] == "foo"

    def test_spider_placeholder_cannot_escape_directory(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        outside = tmp_path / "outside.json"
        path_template = str(tmp_path / "%(spider)s.json")
        crawler = get_crawler(
            Spider,
            {"STATS_DUMP": False, "STATS_JSON_PATH": path_template},
        )
        stats = StatsCollector(crawler)
        crawler.spider = crawler._create_spider("../outside")
        with caplog.at_level(logging.WARNING):
            stats.close_spider(reason="finished")
        assert not outside.exists()
        written = list(tmp_path.glob("*.json"))
        assert len(written) == 1
        assert written[0].name == "..%2Foutside.json"

    def test_placeholder_without_spider_name_skips(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        crawler = get_crawler(
            Spider,
            {
                "STATS_DUMP": False,
                "STATS_JSON_PATH": str(tmp_path / "%(spider)s.json"),
            },
        )
        stats = StatsCollector(crawler)
        assert crawler.spider is None
        with caplog.at_level(logging.WARNING):
            stats.close_spider(reason="finished")
        assert not any(tmp_path.iterdir())
        assert stats._json_snapshot_frozen is False

    def test_memory_persistence_runs_even_when_json_fails(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        crawler = _make_crawler(tmp_path)
        stats = MemoryStatsCollector(crawler)
        crawler.spider = crawler._create_spider("foo")
        stats.set_value("count", 7)
        with (
            mock.patch.object(
                _stats_json, "_write_snapshot_atomic", side_effect=OSError("boom")
            ),
            caplog.at_level(logging.ERROR),
        ):
            stats.close_spider(reason="finished")
        assert stats.spider_stats == {"foo": {"count": 7}}

    def test_two_collectors_do_not_share_state(self, tmp_path: Path) -> None:
        path_template = str(tmp_path / "%(spider)s.json")
        crawler_a = get_crawler(
            Spider,
            {"STATS_DUMP": False, "STATS_JSON_PATH": path_template},
        )
        crawler_b = get_crawler(
            Spider,
            {"STATS_DUMP": False, "STATS_JSON_PATH": path_template},
        )
        stats_a = MemoryStatsCollector(crawler_a)
        stats_b = MemoryStatsCollector(crawler_b)
        crawler_a.spider = crawler_a._create_spider("alpha")
        crawler_b.spider = crawler_b._create_spider("beta")
        stats_a.set_value("which", "a")
        stats_b.set_value("which", "b")
        stats_a.close_spider(reason="finished")
        stats_b.close_spider(reason="cancelled")

        snapshot_a = _read_snapshot(tmp_path / "alpha.json")
        snapshot_b = _read_snapshot(tmp_path / "beta.json")
        assert snapshot_a["spider_name"] == "alpha"
        assert snapshot_a["close_reason"] == "finished"
        assert snapshot_a["stats"] == {"which": "a"}
        assert snapshot_b["spider_name"] == "beta"
        assert snapshot_b["close_reason"] == "cancelled"
        assert snapshot_b["stats"] == {"which": "b"}
        assert stats_a.spider_stats == {"alpha": {"which": "a"}}
        assert stats_b.spider_stats == {"beta": {"which": "b"}}

    def test_no_file_descriptor_leftovers(self, tmp_path: Path) -> None:
        stats = _make_collector(tmp_path)
        stats.set_value("a", 1)
        fd_dir = Path("/dev/fd")
        open_fds_before = {entry.name for entry in fd_dir.iterdir()}
        stats.close_spider(reason="finished")
        gc.collect()
        open_fds_after = {entry.name for entry in fd_dir.iterdir()}
        # No descriptor may point at a temp file after the close.
        for fd in open_fds_after:
            try:
                target = (fd_dir / fd).readlink()
            except OSError:
                continue
            assert not str(target).startswith(str(tmp_path))
        assert len(open_fds_after) <= len(open_fds_before) + 1

    @coroutine_test
    async def test_end_to_end_crawl(self, tmp_path: Path) -> None:
        path = tmp_path / "stats.json"
        crawler = get_crawler(
            SimpleSpider,
            {"STATS_DUMP": False, "STATS_JSON_PATH": str(path)},
        )
        await crawler.crawl_async(url="data:,")
        assert path.exists()
        snapshot = _read_snapshot(path)
        assert snapshot["spider_name"] == "simple"
        assert snapshot["close_reason"] == "finished"
        assert snapshot["stats"]["response_received_count"] == 1
        assert isinstance(snapshot["stats"]["start_time"], str)
        assert isinstance(snapshot["stats"]["finish_time"], str)
        assert snapshot["stats"]["start_time"].endswith("+00:00")

    @coroutine_test
    async def test_end_to_end_two_crawlers(self, tmp_path: Path) -> None:
        path_template = str(tmp_path / "%(spider)s.json")
        settings = {"STATS_DUMP": False, "STATS_JSON_PATH": path_template}
        crawler_a = get_crawler(SimpleSpider, settings)
        crawler_b = get_crawler(SimpleSpider, settings)
        await crawler_a.crawl_async(url="data:,")
        await crawler_b.crawl_async(url="data:,")
        snapshot_a = _read_snapshot(tmp_path / "simple.json")
        # Both crawlers use the same spider name, so the second crawl
        # replaces the first file; each collector's memory stats stay separate.
        assert snapshot_a["spider_name"] == "simple"
        stats_a = crawler_a.stats
        stats_b = crawler_b.stats
        assert isinstance(stats_a, MemoryStatsCollector)
        assert isinstance(stats_b, MemoryStatsCollector)
        assert stats_a.spider_stats != {}
        assert stats_b.spider_stats != {}
        assert stats_a.spider_stats is not stats_b.spider_stats
