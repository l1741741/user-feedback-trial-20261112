from __future__ import annotations

import base64
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from scrapy.exceptions import StatsSnapshotError
from scrapy.spiders import Spider
from scrapy.statscollectors import MemoryStatsCollector, StatsCollector
from scrapy.utils.test import get_crawler

if TYPE_CHECKING:
    from scrapy.crawler import Crawler


def _collector(**settings: Any) -> StatsCollector:
    crawler: Crawler = get_crawler(Spider, {"STATS_DUMP": False, **settings})
    return StatsCollector(crawler)


def _snapshot_error(func: Any) -> StatsSnapshotError:
    with pytest.raises(StatsSnapshotError) as excinfo:
        func()
    return excinfo.value


class Trap:
    """A value whose every protocol method raises if it is ever called."""

    def __repr__(self) -> str:
        raise AssertionError("repr() called")

    def __str__(self) -> str:
        raise AssertionError("str() called")

    def __iter__(self) -> Any:
        raise AssertionError("iter() called")

    def __json__(self) -> Any:
        raise AssertionError("__json__() called")

    def __len__(self) -> int:
        raise AssertionError("len() called")

    def __getitem__(self, key: Any) -> Any:
        raise AssertionError("__getitem__() called")


class TestStatsSnapshotValues:
    def test_scalars_are_copied(self) -> None:
        stats = _collector()
        stats.set_value("none", None)
        stats.set_value("true", True)
        stats.set_value("false", False)
        stats.set_value("int", 3)
        stats.set_value("float", 1.5)
        stats.set_value("str", "text")
        assert stats.get_stats_snapshot() == {
            "none": None,
            "true": True,
            "false": False,
            "int": 3,
            "float": 1.5,
            "str": "text",
        }

    def test_bytes_are_base64(self) -> None:
        stats = _collector()
        stats.set_value("payload", b"\x00\xff")
        snapshot = stats.get_stats_snapshot()
        assert snapshot == {"payload": {"__scrapy_bytes_base64__": "AP8="}}
        assert (
            base64.b64decode(snapshot["payload"]["__scrapy_bytes_base64__"])
            == b"\x00\xff"
        )

    def test_datetimes_keep_their_zone(self) -> None:
        stats = _collector()
        stats.set_value("utc", datetime(2019, 12, 1, 11, 38, tzinfo=UTC))
        stats.set_value(
            "offset", datetime(2019, 12, 1, 11, 38, tzinfo=ZoneInfo("Europe/Madrid"))
        )
        stats.set_value("naive", datetime(2020, 1, 1))
        assert stats.get_stats_snapshot() == {
            "utc": "2019-12-01T11:38:00+00:00",
            "offset": "2019-12-01T11:38:00+01:00",
            "naive": "2020-01-01T00:00:00+00:00",
        }

    def test_date_time_and_timedelta(self) -> None:
        stats = _collector()
        stats.set_value("date", date(2020, 1, 2))
        stats.set_value("time", time(3, 4, 5))
        stats.set_value("elapsed", timedelta(seconds=90, microseconds=500_000))
        assert stats.get_stats_snapshot() == {
            "date": "2020-01-02",
            "time": "03:04:05",
            "elapsed": 90.5,
        }

    def test_containers_are_copied_recursively(self) -> None:
        stats = _collector()
        stats.set_value("list", [1, ("a", "b"), {"k": [None]}])
        assert stats.get_stats_snapshot() == {"list": [1, ["a", "b"], {"k": [None]}]}

    def test_sets_are_sorted_arrays(self) -> None:
        stats = _collector()
        stats.set_value("set", {3, 1, 2})
        stats.set_value("frozenset", frozenset({"b", "a"}))
        snapshot = stats.get_stats_snapshot()
        assert snapshot["set"] == [1, 2, 3]
        assert snapshot["frozenset"] == ["a", "b"]

    def test_set_order_comes_from_the_converted_values(self) -> None:
        stats = _collector()
        stats.set_value("mixed", {2, "1"})
        # Items are sorted by their canonical JSON text: '"1"' sorts before
        # '2' because the quote character precedes the digits.
        assert stats.get_stats_snapshot() == {"mixed": ["1", 2]}

    def test_snapshot_is_json_serializable(self) -> None:
        stats = _collector()
        stats.set_value("time", datetime.now(tz=UTC))
        stats.set_value("payload", b"x")
        stats.set_value("nested", {"a": [1, 2]})
        json.dumps(stats.get_stats_snapshot())


class TestStatsSnapshotRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "cookie",
            "request_cookies",
            "auth_header",
            "my_password",
            "api_key",
            "apiKey",
            "access_key_id",
            "Secret",
            "session_id",
            "token",
            "credentials",
            "private_key",
        ],
    )
    def test_default_sensitive_patterns(self, key: str) -> None:
        stats = _collector()
        stats.set_value(key, "sensitive-value")
        assert stats.get_stats_snapshot() == {key: "[REDACTED]"}

    def test_sensitive_keys_are_redacted_at_any_level(self) -> None:
        stats = _collector()
        stats.set_value(
            "headers", {"Authorization": "Bearer abc", "Accept": "text/html"}
        )
        stats.set_value("jar", {"name": "sid", "value": "qwerty"})
        assert stats.get_stats_snapshot() == {
            "headers": {"Authorization": "[REDACTED]", "Accept": "text/html"},
            "jar": {"name": "sid", "value": "qwerty"},
        }

    def test_a_matching_container_is_redacted_as_a_whole(self) -> None:
        stats = _collector()
        stats.set_value("cookies", {"name": "sid", "value": "qwerty"})
        stats.set_value("nested", {"inner": {"cookies": {"a": "b"}}})
        assert stats.get_stats_snapshot() == {
            "cookies": "[REDACTED]",
            "nested": {"inner": {"cookies": "[REDACTED]"}},
        }

    def test_redacted_value_is_never_touched(self) -> None:
        stats = _collector()
        stats.set_value("token", Trap())
        stats.set_value("nested", {"password": Trap()})
        assert stats.get_stats_snapshot() == {
            "token": "[REDACTED]",
            "nested": {"password": "[REDACTED]"},
        }

    def test_extra_sensitive_keys(self) -> None:
        stats = _collector(STATS_SNAPSHOT_SENSITIVE_KEYS=["internal-*"])
        stats.set_value("internal-id", 1)
        stats.set_value("normal", 2)
        assert stats.get_stats_snapshot() == {
            "internal-id": "[REDACTED]",
            "normal": 2,
        }
        assert stats.get_stats_snapshot(sensitive_keys=["normal"]) == {
            "internal-id": "[REDACTED]",
            "normal": "[REDACTED]",
        }

    def test_builtin_patterns_cannot_be_replaced(self) -> None:
        stats = _collector(STATS_SNAPSHOT_SENSITIVE_KEYS=["only-this"])
        stats.set_value("only-this", 1)
        stats.set_value("password", 2)
        expected = {"only-this": "[REDACTED]", "password": "[REDACTED]"}
        assert stats.get_stats_snapshot() == expected
        assert stats.get_stats_snapshot(sensitive_keys=[]) == expected

    def test_include_filters_top_level_keys(self) -> None:
        stats = _collector()
        stats.set_value("downloader/request_count", 1)
        stats.set_value("downloader/response_count", 2)
        stats.set_value("item_scraped_count", 3)
        assert stats.get_stats_snapshot(include=["downloader/*"]) == {
            "downloader/request_count": 1,
            "downloader/response_count": 2,
        }
        assert stats.get_stats_snapshot(include=["ITEM_*"]) == {"item_scraped_count": 3}
        assert stats.get_stats_snapshot(include=[]) == {
            "downloader/request_count": 1,
            "downloader/response_count": 2,
            "item_scraped_count": 3,
        }

    def test_include_setting(self) -> None:
        stats = _collector(STATS_SNAPSHOT_INCLUDE=["a*"])
        stats.set_value("a", 1)
        stats.set_value("b", 2)
        assert stats.get_stats_snapshot() == {"a": 1}
        assert stats.get_stats_snapshot(include=["b"]) == {"b": 2}

    def test_include_does_not_filter_nested_keys(self) -> None:
        stats = _collector()
        stats.set_value("outer", {"inner": 1})
        assert stats.get_stats_snapshot(include=["outer"]) == {"outer": {"inner": 1}}

    def test_blocked_value_is_not_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stats = _collector()
        stats.set_value("token", "super-secret-value")
        stats.set_value("bad", object())
        error = _snapshot_error(stats.get_stats_snapshot)
        assert "super-secret-value" not in str(error)
        assert all(
            "super-secret-value" not in record.getMessage() for record in caplog.records
        )


class TestStatsSnapshotBoundaries:
    @staticmethod
    def _nest(depth: int) -> dict[str, Any]:
        root: dict[str, Any] = {}
        cursor = root
        for _ in range(depth):
            child: dict[str, Any] = {}
            cursor["child"] = child
            cursor = child
        return root

    def test_depth_limit(self) -> None:
        stats = _collector()
        stats.set_value("nested", self._nest(10))
        error = _snapshot_error(lambda: stats.get_stats_snapshot(max_depth=3))
        assert error.reason == "depth"
        assert error.maximum == 3
        assert error.actual == 4
        assert error.path == 'stats["nested"]["child"]["child"]'

    def test_depth_allows_the_exact_limit(self) -> None:
        stats = _collector()
        stats.set_value("nested", {"a": {"b": 1}})
        # stats (1) -> nested (2) -> a (3); the int is a scalar.
        assert stats.get_stats_snapshot(max_depth=3) == {"nested": {"a": {"b": 1}}}
        assert (
            _snapshot_error(lambda: stats.get_stats_snapshot(max_depth=2)).reason
            == "depth"
        )

    def test_node_limit(self) -> None:
        stats = _collector()
        stats.set_value("values", list(range(100)))
        # stats, its key, the list, then two nodes per item.
        error = _snapshot_error(lambda: stats.get_stats_snapshot(max_nodes=10))
        assert error.reason == "nodes"
        assert error.maximum == 10
        assert error.actual == 11
        assert error.path == 'stats["values"][7]'

    def test_node_limit_counts_keys(self) -> None:
        stats = _collector()
        stats.set_value("a", {"b": 1, "c": 2})
        # stats, "a", the nested dict, "b", 1, "c", 2: 7 nodes.
        assert stats.get_stats_snapshot(max_nodes=7) == {"a": {"b": 1, "c": 2}}
        assert (
            _snapshot_error(lambda: stats.get_stats_snapshot(max_nodes=6)).reason
            == "nodes"
        )

    def test_byte_limit_while_copying(self) -> None:
        stats = _collector()
        stats.set_value("values", ["aaaa", "bbbb", "cccc"])
        error = _snapshot_error(lambda: stats.get_stats_snapshot(max_bytes=20))
        assert error.reason == "bytes"
        assert error.maximum == 20
        assert error.actual is not None
        assert error.actual > 20
        assert error.path == 'stats["values"][1]'

    def test_byte_limit_rejects_a_single_string(self) -> None:
        stats = _collector()
        stats.set_value("big", "x" * 10_000)
        error = _snapshot_error(lambda: stats.get_stats_snapshot(max_bytes=100))
        assert error.reason == "bytes"
        assert error.path == 'stats["big"]'

    def test_byte_limit_rejects_a_single_bytes_value(self) -> None:
        stats = _collector()
        stats.set_value("big", os.urandom(10_000))
        error = _snapshot_error(lambda: stats.get_stats_snapshot(max_bytes=100))
        assert error.reason == "bytes"

    def test_byte_limit_rejects_a_huge_integer(self) -> None:
        stats = _collector()
        stats.set_value("big", 10**10_000)
        error = _snapshot_error(lambda: stats.get_stats_snapshot(max_bytes=100))
        assert error.reason == "bytes"

    def test_byte_limit_rejects_a_huge_set(self) -> None:
        stats = _collector()
        stats.set_value("big", set(range(10_000)))
        error = _snapshot_error(lambda: stats.get_stats_snapshot(max_bytes=200))
        assert error.reason == "bytes"

    def test_limit_stops_the_traversal(self) -> None:
        """The limit triggers while copying instead of after a full copy."""
        stats = _collector()
        values = list(range(1_000_000))
        stats.set_value("huge", values)
        error = _snapshot_error(lambda: stats.get_stats_snapshot(max_nodes=100))
        assert error.reason == "nodes"
        # Few nodes were counted: the whole list was not copied first.
        assert error.actual == 101
        assert error.path == 'stats["huge"][97]'

    def test_a_huge_list_is_rejected_without_a_deep_copied_copy(self) -> None:
        stats = _collector()
        values = list(range(1_000_000))
        stats.set_value("huge", values)
        with pytest.raises(StatsSnapshotError):
            stats.get_stats_snapshot(max_nodes=10)
        # The live list was not copied into the failed snapshot.
        assert stats.get_stats()["huge"] is values

    def test_failed_snapshot_leaves_the_collector_unchanged(self) -> None:
        stats = _collector()
        stats.set_value("ok", 1)
        values = list(range(100))
        stats.set_value("bad", values)
        with pytest.raises(StatsSnapshotError):
            stats.get_stats_snapshot(max_nodes=10)
        assert stats.get_stats() == {"ok": 1, "bad": values}
        assert stats.get_stats()["bad"] is values


class TestStatsSnapshotUnsupported:
    @pytest.mark.parametrize(
        "value",
        [
            object(),
            3.0j,
            iter([1]),
            Exception("boom"),
            Trap(),
        ],
    )
    def test_unsupported_values_raise(self, value: Any) -> None:
        stats = _collector()
        stats.set_value("bad", value)
        error = _snapshot_error(stats.get_stats_snapshot)
        assert error.reason == "unsupported"
        assert error.path == 'stats["bad"]'

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_floats_raise(self, value: float) -> None:
        stats = _collector()
        stats.set_value("bad", value)
        error = _snapshot_error(stats.get_stats_snapshot)
        assert error.reason == "non-finite"
        assert error.path == 'stats["bad"]'

    def test_non_string_keys_raise(self) -> None:
        stats = _collector()
        stats.set_value("bad", {1: "one"})
        error = _snapshot_error(stats.get_stats_snapshot)
        assert error.reason == "key"
        assert error.path == 'stats["bad"][<non-string key>]'

    def test_strings_that_are_not_valid_utf8_raise(self) -> None:
        stats = _collector()
        stats.set_value("bad", "x\ud800")
        error = _snapshot_error(stats.get_stats_snapshot)
        assert error.reason == "unsupported"
        assert error.path == 'stats["bad"]'

    def test_lone_surrogate_keys_are_escaped_in_the_path(self) -> None:
        stats = _collector()
        stats.set_value("bad\ud800", 1)
        error = _snapshot_error(stats.get_stats_snapshot)
        assert error.reason == "unsupported"
        # The text is escaped so the message itself stays valid UTF-8.
        assert error.path == 'stats["bad\\ud800"]'

    def test_circular_reference_raises(self) -> None:
        stats = _collector()
        cycle: dict[str, Any] = {"name": "cycle"}
        cycle["self"] = cycle
        stats.set_value("cycle", cycle)
        error = _snapshot_error(stats.get_stats_snapshot)
        assert error.reason == "circular"
        assert error.path == 'stats["cycle"]["self"]'

    def test_circular_list_raises(self) -> None:
        stats = _collector()
        values: list[Any] = []
        values.append(values)
        stats.set_value("cycle", values)
        error = _snapshot_error(stats.get_stats_snapshot)
        assert error.reason == "circular"

    def test_shared_reference_is_not_a_cycle(self) -> None:
        stats = _collector()
        shared = {"x": 1}
        stats.set_value("mapping", {"a": shared, "b": shared})
        assert stats.get_stats_snapshot() == {"mapping": {"a": {"x": 1}, "b": {"x": 1}}}

    def test_subclasses_do_not_run_their_methods(self) -> None:
        class EvilInt(int):
            def __int__(self) -> int:
                raise AssertionError("__int__ called")

            def __str__(self) -> str:
                raise AssertionError("str called")

        class EvilStr(str):  # noqa: SLOT000
            def __str__(self) -> str:
                raise AssertionError("__str__ called")

        stats = _collector()
        stats.set_value("int", EvilInt(5))
        stats.set_value("str", EvilStr("text"))
        assert stats.get_stats_snapshot() == {"int": 5, "str": "text"}

    def test_arbitrary_repr_is_never_called(self) -> None:
        class Evil:
            def __repr__(self) -> str:
                raise AssertionError("repr called")

            def __json__(self) -> Any:
                raise AssertionError("__json__ called")

        stats = _collector()
        stats.set_value("evil", Evil())
        error = _snapshot_error(stats.get_stats_snapshot)
        assert "Evil" in str(error)

    def test_error_contract(self) -> None:
        stats = _collector()
        stats.set_value("bad", object())
        error = _snapshot_error(stats.get_stats_snapshot)
        assert error.path == 'stats["bad"]'
        assert error.reason == "unsupported"
        assert error.maximum is None
        assert error.actual is None
        assert str(error).startswith("Cannot create a stats snapshot at ")


class TestStatsSnapshotIsolation:
    def test_snapshot_does_not_share_containers_with_stats(self) -> None:
        stats = _collector()
        nested = {"inner": [1, 2]}
        stats.set_value("nested", nested)
        snapshot = stats.get_stats_snapshot()
        snapshot["nested"]["inner"].append(3)
        snapshot["new"] = True
        assert nested == {"inner": [1, 2]}
        assert stats.get_stats() == {"nested": {"inner": [1, 2]}}

    def test_repeated_snapshots_do_not_share_containers(self) -> None:
        stats = _collector()
        stats.set_value("nested", {"inner": [1]})
        first = stats.get_stats_snapshot()
        second = stats.get_stats_snapshot()
        assert first is not second
        assert first["nested"] is not second["nested"]
        first["nested"]["inner"].append(2)
        assert second == {"nested": {"inner": [1]}}

    def test_get_stats_is_unchanged(self) -> None:
        stats = _collector()
        nested = {"inner": [1]}
        stats.set_value("nested", nested)
        assert stats.get_stats() is stats.get_stats()
        assert stats.get_stats()["nested"] is nested

    def test_snapshot_does_not_modify_live_stats(self) -> None:
        stats = _collector()
        stats.set_value("token", "secret")
        stats.set_value("nested", {"password": "x"})
        stats.get_stats_snapshot()
        assert stats.get_stats() == {
            "token": "secret",
            "nested": {"password": "x"},
        }

    def test_snapshot_reflects_the_call_time(self) -> None:
        stats = _collector()
        stats.set_value("count", 1)
        first = stats.get_stats_snapshot()
        stats.inc_value("count")
        assert first == {"count": 1}
        assert stats.get_stats_snapshot() == {"count": 2}

    def test_spider_stats_persistence_is_unaffected(self) -> None:
        crawler: Crawler = get_crawler(Spider, {"STATS_DUMP": False})
        stats = MemoryStatsCollector(crawler)
        crawler.spider = crawler._create_spider("foo")
        stats.set_value("count", 7)
        stats.close_spider(reason="finished")
        assert stats.spider_stats == {"foo": {"count": 7}}


class TestStatsSnapshotFile:
    def test_writes_json(self, tmp_path: Path) -> None:
        stats = _collector()
        stats.set_value("count", 1)
        stats.set_value("payload", b"\x00")
        path = tmp_path / "stats.json"
        stats.save_stats_snapshot(path)
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "count": 1,
            "payload": {"__scrapy_bytes_base64__": "AA=="},
        }
        assert list(tmp_path.glob("*.tmp")) == []

    def test_replaces_previous_file(self, tmp_path: Path) -> None:
        path = tmp_path / "stats.json"
        path.write_text('{"old": true}', encoding="utf-8")
        stats = _collector()
        stats.set_value("count", 2)
        stats.save_stats_snapshot(path)
        assert json.loads(path.read_text(encoding="utf-8")) == {"count": 2}

    def test_parameters_are_forwarded(self, tmp_path: Path) -> None:
        stats = _collector()
        stats.set_value("a", 1)
        stats.set_value("b", 2)
        path = tmp_path / "stats.json"
        stats.save_stats_snapshot(path, include=["a"], max_bytes=100)
        assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}

    def test_snapshot_failure_writes_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "stats.json"
        path.write_text('{"old": true}', encoding="utf-8")
        stats = _collector()
        stats.set_value("bad", object())
        with pytest.raises(StatsSnapshotError):
            stats.save_stats_snapshot(path)
        assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
        assert list(tmp_path.glob("*.tmp")) == []

    def test_replace_failure_keeps_previous_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "stats.json"
        path.write_text('{"old": true}', encoding="utf-8")
        stats = _collector()
        stats.set_value("count", 1)

        def fail_replace(self: Path, target: Any) -> None:
            raise OSError("boom")

        monkeypatch.setattr(Path, "replace", fail_replace)
        error = _snapshot_error(lambda: stats.save_stats_snapshot(path))
        assert error.reason == "write"
        assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
        assert list(tmp_path.glob("*.tmp")) == []

    def test_fsync_failure_keeps_previous_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "stats.json"
        path.write_text('{"old": true}', encoding="utf-8")
        stats = _collector()

        def fail_fsync(fd: int) -> None:
            raise OSError("boom")

        monkeypatch.setattr(os, "fsync", fail_fsync)
        error = _snapshot_error(lambda: stats.save_stats_snapshot(path))
        assert error.reason == "write"
        assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
        assert list(tmp_path.glob("*.tmp")) == []

    def test_missing_parent_directory(self, tmp_path: Path) -> None:
        stats = _collector()
        stats.set_value("a", 1)
        path = tmp_path / "missing" / "stats.json"
        error = _snapshot_error(lambda: stats.save_stats_snapshot(path))
        assert error.reason == "write"
        assert not path.exists()

    def test_accepts_a_string_path(self, tmp_path: Path) -> None:
        stats = _collector()
        stats.set_value("a", 1)
        path = tmp_path / "stats.json"
        stats.save_stats_snapshot(str(path))
        assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}

    def test_repeated_exports_are_identical(self, tmp_path: Path) -> None:
        stats = _collector()
        stats.set_value("set", {"b", "a", "c"})
        stats.set_value("nested", {"z": 1, "a": [1, 2]})
        first = tmp_path / "first.json"
        second = tmp_path / "second.json"
        stats.save_stats_snapshot(first)
        stats.save_stats_snapshot(second)
        assert first.read_bytes() == second.read_bytes()

    def test_failed_export_can_be_retried(self, tmp_path: Path) -> None:
        path = tmp_path / "stats.json"
        stats = _collector()
        stats.set_value("bad", object())
        with pytest.raises(StatsSnapshotError):
            stats.save_stats_snapshot(path)
        stats.set_value("bad", 1)
        stats.save_stats_snapshot(path)
        assert json.loads(path.read_text(encoding="utf-8")) == {"bad": 1}


class TestStatsSnapshotSettings:
    @pytest.mark.parametrize(
        "name",
        [
            "STATS_SNAPSHOT_MAX_DEPTH",
            "STATS_SNAPSHOT_MAX_NODES",
            "STATS_SNAPSHOT_MAX_BYTES",
        ],
    )
    @pytest.mark.parametrize("value", [0, -1])
    def test_invalid_setting_limits(self, name: str, value: int) -> None:
        stats = _collector(**{name: value})
        error = _snapshot_error(stats.get_stats_snapshot)
        assert error.reason == "config"

    def test_setting_limits_apply(self) -> None:
        stats = _collector(STATS_SNAPSHOT_MAX_NODES=2)
        stats.set_value("a", 1)
        stats.set_value("b", 2)
        assert _snapshot_error(stats.get_stats_snapshot).reason == "nodes"
        assert stats.get_stats_snapshot(max_nodes=100) == {"a": 1, "b": 2}

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_depth": 0},
            {"max_nodes": -3},
            {"max_bytes": 0},
            {"include": [1]},
            {"sensitive_keys": [object()]},
            # A bare string would otherwise be treated as a sequence of
            # one-character patterns.
            {"include": "downloader/*"},
            {"sensitive_keys": "token"},
        ],
    )
    def test_invalid_arguments(self, kwargs: dict[str, Any]) -> None:
        stats = _collector()
        stats.set_value("a", 1)
        error = _snapshot_error(lambda: stats.get_stats_snapshot(**kwargs))
        assert error.reason == "config"


class TestStatsSnapshotCrossSpider:
    def test_collectors_do_not_share_snapshot_state(self) -> None:
        stats_a = _collector()
        stats_b = _collector()
        stats_a.set_value("which", "a")
        stats_b.set_value("which", "b")
        snapshot_a = stats_a.get_stats_snapshot()
        snapshot_b = stats_b.get_stats_snapshot()
        assert snapshot_a == {"which": "a"}
        assert snapshot_b == {"which": "b"}
        snapshot_a["which"] = "changed"
        assert snapshot_b == {"which": "b"}
        assert stats_b.get_stats() == {"which": "b"}

    def test_stats_of_another_spider_are_not_included(self) -> None:
        crawler: Crawler = get_crawler(Spider, {"STATS_DUMP": False})
        stats = MemoryStatsCollector(crawler)
        crawler.spider = crawler._create_spider("alpha")
        stats.set_value("count", 1)
        stats.close_spider(reason="finished")
        assert stats.spider_stats == {"alpha": {"count": 1}}
        # A new spider gets fresh stats: set_stats() replaces the dict, so
        # the alpha entry keeps its own contents.
        crawler.spider = crawler._create_spider("beta")
        stats.set_stats({})
        stats.set_value("count", 2)
        assert stats.get_stats_snapshot() == {"count": 2}
        assert stats.spider_stats == {"alpha": {"count": 1}}

    def test_error_does_not_poison_the_collector(self) -> None:
        stats = _collector()
        stats.set_value("bad", object())
        with pytest.raises(StatsSnapshotError):
            stats.get_stats_snapshot()
        stats.set_value("bad", 1)
        assert stats.get_stats_snapshot() == {"bad": 1}
