from __future__ import annotations

import logging
import os
import pickle
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from scrapy import signals
from scrapy.exceptions import NotConfigured, ScrapyDeprecationWarning
from scrapy.extensions.spiderstate import SpiderState, SpiderStateError
from scrapy.spiders import Spider
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from pathlib import Path


def test_deprecated_methods(tmp_path: Path) -> None:
    crawler = get_crawler(Spider, {"JOBDIR": str(tmp_path)})
    ss = build_from_crawler(SpiderState, crawler)
    spider = Spider(name="default")
    with pytest.warns(ScrapyDeprecationWarning, match="spider_opened"):
        ss.spider_opened(spider)
    with pytest.warns(ScrapyDeprecationWarning, match="spider_closed"):
        ss.spider_closed(spider)


def test_not_configured() -> None:
    crawler = get_crawler(Spider)
    with pytest.raises(NotConfigured):
        build_from_crawler(SpiderState, crawler)


@coroutine_test
async def test_store_load(tmp_path: Path) -> None:
    jobdir = str(tmp_path)
    spider = Spider(name="default")
    dt = datetime.now(tz=UTC)

    crawler = get_crawler(Spider, {"JOBDIR": jobdir})
    ss = build_from_crawler(SpiderState, crawler)
    await ss._spider_opened(spider)
    assert hasattr(spider, "state")
    spider.state["one"] = 1
    spider.state["dt"] = dt
    await ss._spider_closed(spider)

    spider2 = Spider(name="default")
    crawler2 = get_crawler(Spider, {"JOBDIR": jobdir})
    ss2 = build_from_crawler(SpiderState, crawler2)
    await ss2._spider_opened(spider2)
    assert hasattr(spider2, "state")
    assert spider2.state == {"one": 1, "dt": dt}
    await ss2._spider_closed(spider2)


@coroutine_test
async def test_spider_state_loaded_signal_fires(tmp_path: Path) -> None:
    crawler = get_crawler(Spider, {"JOBDIR": str(tmp_path)})
    ss = build_from_crawler(SpiderState, crawler)

    received: list[dict[str, Any]] = []

    def on_loaded(state: dict[str, Any]) -> None:
        received.append(dict(state))

    crawler.signals.connect(on_loaded, signal=signals.spider_state_loaded, weak=False)

    spider = Spider(name="default")
    await ss._spider_opened(spider)

    assert received == [{}]
    assert spider.state == {}


@coroutine_test
async def test_spider_state_saving_signal_fires(tmp_path: Path) -> None:
    crawler = get_crawler(Spider, {"JOBDIR": str(tmp_path)})
    ss = build_from_crawler(SpiderState, crawler)

    spider = Spider(name="default")
    await ss._spider_opened(spider)
    spider.state["key"] = "value"

    saving_calls: list[dict[str, Any]] = []

    def on_saving(state: dict[str, Any]) -> None:
        saving_calls.append(dict(state))

    crawler.signals.connect(on_saving, signal=signals.spider_state_saving, weak=False)
    await ss._spider_closed(spider)

    assert saving_calls == [{"key": "value"}]


def _build_spider_state(
    tmp_path: Path, settings: dict[str, Any] | None = None
) -> SpiderState:
    crawler = get_crawler(Spider, {"JOBDIR": str(tmp_path), **(settings or {})})
    return build_from_crawler(SpiderState, crawler)


def _read_state_file(path: Path) -> Any:
    with path.open("rb") as f:
        return pickle.load(f)


def _leftover_tmp_files(tmp_path: Path) -> list[Path]:
    return [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


@coroutine_test
async def test_atomic_store_load(tmp_path: Path) -> None:
    jobdir = str(tmp_path)
    settings = {"SPIDER_STATE_ATOMIC": True}
    spider = Spider(name="default")
    dt = datetime.now(tz=UTC)

    ss = _build_spider_state(tmp_path, settings)
    await ss._spider_opened(spider)
    assert spider.state == {}
    spider.state["one"] = 1
    spider.state["dt"] = dt
    await ss._spider_closed(spider)

    assert _read_state_file(tmp_path / "spider.state") == {"one": 1, "dt": dt}
    assert _leftover_tmp_files(tmp_path) == []

    spider2 = Spider(name="default")
    crawler2 = get_crawler(Spider, {"JOBDIR": jobdir, **settings})
    ss2 = build_from_crawler(SpiderState, crawler2)
    await ss2._spider_opened(spider2)
    assert spider2.state == {"one": 1, "dt": dt}


@coroutine_test
async def test_atomic_pickle_failure_preserves_previous(tmp_path: Path) -> None:
    settings = {"SPIDER_STATE_ATOMIC": True}
    spider = Spider(name="default")

    ss = _build_spider_state(tmp_path, settings)
    await ss._spider_opened(spider)
    spider.state["one"] = 1
    await ss._spider_closed(spider)
    original_bytes = (tmp_path / "spider.state").read_bytes()

    spider.state["bad"] = lambda: None  # not picklable
    with pytest.raises((pickle.PicklingError, AttributeError)):
        await ss._spider_closed(spider)

    assert (tmp_path / "spider.state").read_bytes() == original_bytes
    assert _read_state_file(tmp_path / "spider.state") == {"one": 1}
    assert _leftover_tmp_files(tmp_path) == []


@coroutine_test
async def test_atomic_write_failure_preserves_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = {"SPIDER_STATE_ATOMIC": True}
    spider = Spider(name="default")

    ss = _build_spider_state(tmp_path, settings)
    await ss._spider_opened(spider)
    spider.state["one"] = 1
    await ss._spider_closed(spider)
    original_bytes = (tmp_path / "spider.state").read_bytes()

    def failing_fsync(fd: int) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    spider.state["one"] = 2
    with pytest.raises(OSError, match="simulated disk failure"):
        await ss._spider_closed(spider)

    assert (tmp_path / "spider.state").read_bytes() == original_bytes
    assert _leftover_tmp_files(tmp_path) == []


@coroutine_test
async def test_atomic_replace_failure_preserves_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = {"SPIDER_STATE_ATOMIC": True}
    spider = Spider(name="default")

    ss = _build_spider_state(tmp_path, settings)
    await ss._spider_opened(spider)
    spider.state["one"] = 1
    await ss._spider_closed(spider)
    original_bytes = (tmp_path / "spider.state").read_bytes()

    def failing_replace(src: str, dst: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    spider.state["one"] = 2
    with pytest.raises(OSError, match="simulated replace failure"):
        await ss._spider_closed(spider)

    assert (tmp_path / "spider.state").read_bytes() == original_bytes
    assert _leftover_tmp_files(tmp_path) == []


@coroutine_test
async def test_backup_saved_on_close(tmp_path: Path) -> None:
    settings = {"SPIDER_STATE_ATOMIC": True, "SPIDER_STATE_BACKUP": True}
    spider = Spider(name="default")

    ss = _build_spider_state(tmp_path, settings)
    await ss._spider_opened(spider)
    spider.state["run"] = 1
    await ss._spider_closed(spider)

    # No previous state existed, so no backup is created on the first save.
    assert not (tmp_path / "spider.state.bak").exists()

    spider.state["run"] = 2
    await ss._spider_closed(spider)

    assert _read_state_file(tmp_path / "spider.state") == {"run": 2}
    assert _read_state_file(tmp_path / "spider.state.bak") == {"run": 1}
    assert _leftover_tmp_files(tmp_path) == []


@coroutine_test
async def test_backup_recovers_corrupt_main(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = {"SPIDER_STATE_ATOMIC": True, "SPIDER_STATE_BACKUP": True}
    spider = Spider(name="default")

    ss = _build_spider_state(tmp_path, settings)
    await ss._spider_opened(spider)
    spider.state["run"] = 1
    await ss._spider_closed(spider)
    spider.state["run"] = 2
    await ss._spider_closed(spider)

    (tmp_path / "spider.state").write_bytes(b"not a pickle")
    backup_bytes = (tmp_path / "spider.state.bak").read_bytes()

    spider2 = Spider(name="default")
    ss2 = _build_spider_state(tmp_path, settings)
    with caplog.at_level(logging.WARNING, logger="scrapy.extensions.spiderstate"):
        await ss2._spider_opened(spider2)

    assert spider2.state == {"run": 1}
    assert "spider.state" in caplog.text
    assert "spider.state.bak" in caplog.text
    # Loading must not modify the files on disk.
    assert (tmp_path / "spider.state").read_bytes() == b"not a pickle"
    assert (tmp_path / "spider.state.bak").read_bytes() == backup_bytes


@coroutine_test
async def test_backup_and_main_invalid_raises(tmp_path: Path) -> None:
    settings = {"SPIDER_STATE_ATOMIC": True, "SPIDER_STATE_BACKUP": True}
    spider = Spider(name="default")

    ss = _build_spider_state(tmp_path, settings)
    await ss._spider_opened(spider)
    spider.state["run"] = 1
    await ss._spider_closed(spider)

    (tmp_path / "spider.state").write_bytes(b"not a pickle")
    (tmp_path / "spider.state.bak").write_bytes(b"also not a pickle")

    spider2 = Spider(name="default")
    ss2 = _build_spider_state(tmp_path, settings)
    with pytest.raises(SpiderStateError, match="spider.state"):
        await ss2._spider_opened(spider2)

    # Both files are left untouched for manual inspection.
    assert (tmp_path / "spider.state").read_bytes() == b"not a pickle"
    assert (tmp_path / "spider.state.bak").read_bytes() == b"also not a pickle"


@coroutine_test
async def test_backup_non_dict_not_used(tmp_path: Path) -> None:
    settings = {"SPIDER_STATE_BACKUP": True}
    spider = Spider(name="default")

    ss = _build_spider_state(tmp_path, settings)
    await ss._spider_opened(spider)
    spider.state["run"] = 1
    await ss._spider_closed(spider)

    (tmp_path / "spider.state").write_bytes(b"not a pickle")
    with (tmp_path / "spider.state.bak").open("wb") as f:
        pickle.dump(["not", "a", "dict"], f, protocol=4)

    spider2 = Spider(name="default")
    ss2 = _build_spider_state(tmp_path, settings)
    with pytest.raises(SpiderStateError):
        await ss2._spider_opened(spider2)


@coroutine_test
async def test_corrupt_main_not_backed_up(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = {"SPIDER_STATE_ATOMIC": True, "SPIDER_STATE_BACKUP": True}
    spider = Spider(name="default")

    ss = _build_spider_state(tmp_path, settings)
    await ss._spider_opened(spider)
    spider.state["run"] = 1
    await ss._spider_closed(spider)
    spider.state["run"] = 2
    await ss._spider_closed(spider)
    backup_bytes = (tmp_path / "spider.state.bak").read_bytes()

    # A corrupt main file must not replace the last valid backup.
    (tmp_path / "spider.state").write_bytes(b"not a pickle")
    spider.state["run"] = 3
    with caplog.at_level(logging.WARNING, logger="scrapy.extensions.spiderstate"):
        await ss._spider_closed(spider)

    assert "Not updating spider state backup" in caplog.text
    assert (tmp_path / "spider.state.bak").read_bytes() == backup_bytes
    assert _read_state_file(tmp_path / "spider.state") == {"run": 3}


@coroutine_test
async def test_default_mode_writes_no_backup_or_tmp_files(tmp_path: Path) -> None:
    spider = Spider(name="default")
    ss = _build_spider_state(tmp_path)
    await ss._spider_opened(spider)
    spider.state["one"] = 1
    await ss._spider_closed(spider)

    assert _read_state_file(tmp_path / "spider.state") == {"one": 1}
    assert not (tmp_path / "spider.state.bak").exists()
    assert _leftover_tmp_files(tmp_path) == []


@coroutine_test
async def test_deprecated_methods_with_atomic_and_backup(tmp_path: Path) -> None:
    settings = {"SPIDER_STATE_ATOMIC": True, "SPIDER_STATE_BACKUP": True}
    ss = _build_spider_state(tmp_path, settings)
    spider = Spider(name="default")
    with pytest.warns(ScrapyDeprecationWarning, match="spider_opened"):
        ss.spider_opened(spider)
    assert spider.state == {}
    spider.state["one"] = 1
    with pytest.warns(ScrapyDeprecationWarning, match="spider_closed"):
        ss.spider_closed(spider)
    assert _read_state_file(tmp_path / "spider.state") == {"one": 1}
    assert _leftover_tmp_files(tmp_path) == []
