from __future__ import annotations

import asyncio
import errno
import logging
import os
import pickle
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from scrapy import signals
from scrapy.exceptions import NotConfigured, ScrapyDeprecationWarning
from scrapy.extensions import spiderstate
from scrapy.extensions.spiderstate import SpiderState
from scrapy.spiders import Spider
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import IO


_STATE_NAME = "spider.state"
_OLD_STATE = {"old": True}


def _jobdir(tmp_path: Path, name: str = "job") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    return directory


def _spiderstate(jobdir: Path) -> SpiderState:
    crawler = get_crawler(Spider, {"JOBDIR": str(jobdir)})
    return build_from_crawler(SpiderState, crawler)


def _spider_with(state: dict[str, Any] | None = None) -> Spider:
    spider = Spider(name="default")
    spider.state = {} if state is None else state
    return spider


def _save_old_state(jobdir: Path, state: dict[str, Any] | None = None) -> bytes:
    """Write a state file and return its exact bytes."""
    data = pickle.dumps(_OLD_STATE if state is None else state, protocol=4)
    (jobdir / _STATE_NAME).write_bytes(data)
    return data


def _read_state_bytes(jobdir: Path) -> bytes:
    return (jobdir / _STATE_NAME).read_bytes()


def _temp_names(jobdir: Path) -> list[str]:
    return sorted(
        entry.name
        for entry in jobdir.iterdir()
        if entry.name.startswith(f".{_STATE_NAME}.")
    )


class _FaultyFile:
    """Wrap a real file and fail on one operation."""

    def __init__(self, f: IO[bytes], fail_at: str):
        self._f = f
        self.fail_at = fail_at

    @property
    def closed(self) -> bool:
        return bool(self._f.closed)

    def write(self, data: bytes) -> int:
        if self.fail_at == "write":
            raise OSError(errno.EIO, "simulated write failure")
        return self._f.write(data)

    def flush(self) -> None:
        if self.fail_at == "flush":
            raise OSError(errno.EIO, "simulated flush failure")
        self._f.flush()

    def fileno(self) -> int:
        return self._f.fileno()

    def close(self) -> None:
        if self.fail_at == "close":
            # Close first, so that this failure does not also leak the file.
            self._f.close()
            raise OSError(errno.EIO, "simulated close failure")
        self._f.close()


@pytest.fixture(autouse=True)
def opened_temp_files(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Record the temporary files and their file descriptors, and check after
    the test that the extension released both."""
    files: list[Any] = []
    descriptors: list[int] = []
    original = spiderstate._open_temp_file

    def open_temp_file(state_path: Path) -> tuple[Any, Path]:
        f, temp_path = original(state_path)
        files.append(f)
        descriptors.append(f.fileno())
        return f, temp_path

    monkeypatch.setattr(spiderstate, "_open_temp_file", open_temp_file)
    yield files
    assert [f.closed for f in files] == [True] * len(files)
    for fd in descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)


@pytest.fixture
def jobdir(tmp_path: Path) -> Path:
    return _jobdir(tmp_path)


def _fail_temp_file_stage(monkeypatch: pytest.MonkeyPatch, fail_at: str) -> None:
    """Make the temporary file fail on *fail_at*."""
    original = spiderstate._open_temp_file

    def open_temp_file(state_path: Path) -> tuple[Any, Path]:
        f, temp_path = original(state_path)
        return _FaultyFile(f, fail_at), temp_path

    monkeypatch.setattr(spiderstate, "_open_temp_file", open_temp_file)


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


def test_new_file(jobdir: Path) -> None:
    spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 1}))

    assert _temp_names(jobdir) == []
    assert pickle.loads(_read_state_bytes(jobdir)) == {"new": 1}


def test_overwrite_old_file(jobdir: Path) -> None:
    old_bytes = _save_old_state(jobdir)
    spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 2}))

    assert _temp_names(jobdir) == []
    new_bytes = _read_state_bytes(jobdir)
    assert pickle.loads(new_bytes) == {"new": 2}
    assert new_bytes != old_bytes


def test_temp_file_is_in_jobdir_and_unique(
    jobdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replaced: list[Path] = []
    real_replace = os.replace

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        replaced.append(Path(src))
        real_replace(src, dst)

    monkeypatch.setattr("scrapy.extensions.spiderstate.os.replace", replace)
    ss = spiderstate.SpiderState(str(jobdir))
    for value in (1, 2):
        ss._persist_state(_spider_with({"n": value}))

    assert len(replaced) == 2
    assert all(path.parent == jobdir for path in replaced)
    assert all(path.name.startswith(f".{_STATE_NAME}.") for path in replaced)
    assert replaced[0] != replaced[1]


def test_temp_name_collision(jobdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    names = iter(["colliding"])
    monkeypatch.setattr(
        "scrapy.extensions.spiderstate.secrets.token_hex",
        lambda _n: next(names, "free-name"),
    )
    colliding = jobdir / f".{_STATE_NAME}.colliding.tmp"
    colliding.write_bytes(b"not ours")
    spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 1}))

    assert colliding.read_bytes() == b"not ours"
    assert pickle.loads(_read_state_bytes(jobdir)) == {"new": 1}


def test_temp_name_always_colliding(
    jobdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_bytes = _save_old_state(jobdir)
    monkeypatch.setattr(
        "scrapy.extensions.spiderstate.secrets.token_hex", lambda _n: "taken"
    )
    taken = jobdir / f".{_STATE_NAME}.taken.tmp"
    taken.write_bytes(b"not ours")
    spider = _spider_with({"new": 1})

    with pytest.raises(OSError, match="Cannot create a temporary file"):
        spiderstate.SpiderState(str(jobdir))._persist_state(spider)

    assert _read_state_bytes(jobdir) == old_bytes
    assert taken.read_bytes() == b"not ours"
    assert spider.state == {"new": 1}


@pytest.mark.parametrize("fail_at", ["write", "flush", "close"])
def test_temp_file_io_failure(
    jobdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_temp_files: list[Any],
    fail_at: str,
) -> None:
    old_bytes = _save_old_state(jobdir)
    _fail_temp_file_stage(monkeypatch, fail_at)
    state = {"new": 1}
    spider = _spider_with(state)

    with pytest.raises(OSError, match=f"simulated {fail_at} failure"):
        spiderstate.SpiderState(str(jobdir))._persist_state(spider)

    assert _read_state_bytes(jobdir) == old_bytes
    assert _temp_names(jobdir) == []
    assert spider.state is state
    assert len(opened_temp_files) == 1


def test_serialization_failure(jobdir: Path) -> None:
    old_bytes = _save_old_state(jobdir)

    class Unserializable:
        def __reduce__(self) -> Any:
            raise TypeError("cannot pickle this")

    state = {"bad": Unserializable()}
    spider = _spider_with(state)

    with pytest.raises(TypeError, match="cannot pickle this"):
        spiderstate.SpiderState(str(jobdir))._persist_state(spider)

    assert _read_state_bytes(jobdir) == old_bytes
    assert _temp_names(jobdir) == []
    assert spider.state is state


def test_temp_fsync_failure(jobdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_bytes = _save_old_state(jobdir)
    real_fsync = os.fsync
    calls = 0

    def fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "simulated fsync failure")
        real_fsync(fd)

    monkeypatch.setattr("scrapy.extensions.spiderstate.os.fsync", fsync)
    state = {"new": 1}
    spider = _spider_with(state)

    with pytest.raises(OSError, match="simulated fsync failure"):
        spiderstate.SpiderState(str(jobdir))._persist_state(spider)

    assert _read_state_bytes(jobdir) == old_bytes
    assert _temp_names(jobdir) == []
    assert spider.state is state


def test_replace_failure(jobdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_bytes = _save_old_state(jobdir)

    def replace(_src: Any, _dst: Any) -> None:
        raise OSError(errno.ENOSPC, "simulated replace failure")

    monkeypatch.setattr("scrapy.extensions.spiderstate.os.replace", replace)
    state = {"new": 1}
    spider = _spider_with(state)

    with pytest.raises(OSError, match="simulated replace failure"):
        spiderstate.SpiderState(str(jobdir))._persist_state(spider)

    assert _read_state_bytes(jobdir) == old_bytes
    assert _temp_names(jobdir) == []
    assert spider.state is state


def test_replace_completed_before_failure(
    jobdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new state is already visible if os.replace() completed, even when
    the call then reports an error."""
    real_replace = os.replace

    def replace(src: Any, dst: Any) -> None:
        real_replace(src, dst)
        raise OSError(errno.EIO, "simulated late replace failure")

    monkeypatch.setattr("scrapy.extensions.spiderstate.os.replace", replace)

    with pytest.raises(OSError, match="simulated late replace failure"):
        spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 1}))

    assert pickle.loads(_read_state_bytes(jobdir)) == {"new": 1}
    assert _temp_names(jobdir) == []


def test_unremovable_temp_file(
    jobdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    old_bytes = _save_old_state(jobdir)
    _fail_temp_file_stage(monkeypatch, "flush")
    real_unlink = Path.unlink

    def unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.parent == jobdir:
            raise PermissionError(errno.EACCES, "simulated unlink failure")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(OSError, match="simulated flush failure"),
    ):
        spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 1}))

    assert _read_state_bytes(jobdir) == old_bytes
    assert _temp_names(jobdir) != []
    assert "simulated unlink failure" in caplog.text


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX permissions that apply to the test user",
)
def test_unwritable_jobdir(jobdir: Path) -> None:
    old_bytes = _save_old_state(jobdir)
    jobdir.chmod(0o500)
    state = {"new": 1}
    spider = _spider_with(state)
    try:
        with pytest.raises(PermissionError):
            spiderstate.SpiderState(str(jobdir))._persist_state(spider)
    finally:
        jobdir.chmod(0o700)

    assert _read_state_bytes(jobdir) == old_bytes
    assert _temp_names(jobdir) == []
    assert spider.state is state


@coroutine_test
async def test_repeated_close_leaves_no_temp_file(jobdir: Path) -> None:
    ss = _spiderstate(jobdir)
    spider = _spider_with({"n": 1})
    await ss._spider_closed(spider)
    await ss._spider_closed(spider)

    assert _temp_names(jobdir) == []
    assert pickle.loads(_read_state_bytes(jobdir)) == {"n": 1}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX umask")
@pytest.mark.parametrize(("umask", "mode"), [(0o027, 0o640), (0o022, 0o644)])
def test_permissions_follow_umask(jobdir: Path, umask: int, mode: int) -> None:
    """The new file gets the permissions that umask gives to any other new
    file, rather than the permissions of the file it replaces."""
    _save_old_state(jobdir)
    (jobdir / _STATE_NAME).chmod(0o600)
    old_umask = os.umask(umask)
    try:
        spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 1}))
    finally:
        os.umask(old_umask)

    assert (jobdir / _STATE_NAME).stat().st_mode & 0o777 == mode


def test_different_jobdirs(tmp_path: Path) -> None:
    first = _jobdir(tmp_path, "first")
    second = _jobdir(tmp_path, "second")
    for directory, value in ((first, 1), (second, 2)):
        spiderstate.SpiderState(str(directory))._persist_state(
            _spider_with({"dir": value})
        )

    assert pickle.loads(_read_state_bytes(first)) == {"dir": 1}
    assert pickle.loads(_read_state_bytes(second)) == {"dir": 2}
    assert _temp_names(first) == []
    assert _temp_names(second) == []


def test_write_order(jobdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The state file is only replaced once the temporary file has been fully
    written and synced."""
    events: list[str] = []
    original_open = spiderstate._open_temp_file
    temp_fds: list[int] = []

    class RecordingFile:
        def __init__(self, f: IO[bytes]):
            self._f = f

        @property
        def closed(self) -> bool:
            return bool(self._f.closed)

        def write(self, data: bytes) -> int:
            events.append("write")
            return self._f.write(data)

        def flush(self) -> None:
            events.append("flush")
            self._f.flush()

        def fileno(self) -> int:
            return self._f.fileno()

        def close(self) -> None:
            self._f.close()

    def open_temp_file(state_path: Path) -> tuple[Any, Path]:
        f, temp_path = original_open(state_path)
        temp_fds.append(f.fileno())
        return RecordingFile(f), temp_path

    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if fd in temp_fds:
            events.append("fsync")
        real_fsync(fd)

    real_replace = Path.replace

    def replace(self: Path, target: Any) -> Path:
        events.append("replace")
        assert _temp_names(jobdir)
        return real_replace(self, target)

    monkeypatch.setattr(spiderstate, "_open_temp_file", open_temp_file)
    monkeypatch.setattr("scrapy.extensions.spiderstate.os.fsync", fsync)
    monkeypatch.setattr(Path, "replace", replace)
    spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 1}))

    assert events.count("flush") == 1
    assert events.count("replace") == 1
    assert events.index("flush") < events.index("fsync") < events.index("replace")
    assert "write" in events[: events.index("flush")]
    assert _temp_names(jobdir) == []


def test_directory_synced_after_replace(
    jobdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_replace = os.replace
    real_fsync = os.fsync
    real_open = os.open

    def replace(src: Any, dst: Any) -> None:
        real_replace(src, dst)
        events.append("replace")

    def fsync(fd: int) -> None:
        if fd in directory_fds:
            events.append("directory fsync")
        real_fsync(fd)

    def open_dir(path: Any, flags: int) -> int:
        fd = real_open(path, flags)
        if Path(path) == jobdir:
            directory_fds.append(fd)
        return fd

    directory_fds: list[int] = []
    monkeypatch.setattr("scrapy.extensions.spiderstate.os.replace", replace)
    monkeypatch.setattr("scrapy.extensions.spiderstate.os.fsync", fsync)
    monkeypatch.setattr("scrapy.extensions.spiderstate.os.open", open_dir)
    spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 1}))

    assert events == ["replace", "directory fsync"]


@pytest.mark.parametrize("errno_value", [errno.EACCES, errno.EPERM, errno.EINVAL])
def test_unsupported_directory_open(
    jobdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    errno_value: int,
) -> None:
    real_open = os.open

    def open_dir(path: Any, flags: int) -> int:
        if Path(path) == jobdir:
            raise OSError(errno_value, "directory open unsupported")
        return real_open(path, flags)

    monkeypatch.setattr("scrapy.extensions.spiderstate.os.open", open_dir)
    with caplog.at_level(logging.DEBUG, logger=spiderstate.__name__):
        spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 1}))

    assert pickle.loads(_read_state_bytes(jobdir)) == {"new": 1}
    assert _temp_names(jobdir) == []
    assert "does not support opening a directory" in caplog.text
    assert "may not survive a power loss" not in caplog.text


def test_unsupported_directory_fsync(
    jobdir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_fsync = os.fsync
    calls = 0

    def fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        real_fsync(fd)

    monkeypatch.setattr("scrapy.extensions.spiderstate.os.fsync", fsync)
    with caplog.at_level(logging.DEBUG, logger=spiderstate.__name__):
        spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 1}))

    assert pickle.loads(_read_state_bytes(jobdir)) == {"new": 1}
    assert _temp_names(jobdir) == []
    assert "does not support syncing directories" in caplog.text
    assert "may not survive a power loss" not in caplog.text


def test_directory_open_failure_is_logged(
    jobdir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_open = os.open

    def open_dir(path: Any, flags: int) -> int:
        if Path(path) == jobdir:
            raise OSError(errno.EMFILE, "simulated directory open failure")
        return real_open(path, flags)

    monkeypatch.setattr("scrapy.extensions.spiderstate.os.open", open_dir)
    with caplog.at_level(logging.WARNING, logger=spiderstate.__name__):
        spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 1}))

    assert pickle.loads(_read_state_bytes(jobdir)) == {"new": 1}
    assert "simulated directory open failure" in caplog.text
    assert "may not survive a power loss" in caplog.text


def test_directory_fsync_failure_is_logged(
    jobdir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_fsync = os.fsync
    calls = 0

    def fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "simulated directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr("scrapy.extensions.spiderstate.os.fsync", fsync)
    with caplog.at_level(logging.WARNING, logger=spiderstate.__name__):
        spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 1}))

    assert pickle.loads(_read_state_bytes(jobdir)) == {"new": 1}
    assert "simulated directory fsync failure" in caplog.text
    assert "may not survive a power loss" in caplog.text


def test_directory_close_failure_is_logged(
    jobdir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_open = os.open
    real_close = os.close
    directory_fds: list[int] = []

    def open_dir(path: Any, flags: int) -> int:
        fd = real_open(path, flags)
        if Path(path) == jobdir:
            directory_fds.append(fd)
        return fd

    def close(fd: int) -> None:
        if fd in directory_fds:
            real_close(fd)
            raise OSError(errno.EIO, "simulated directory close failure")
        real_close(fd)

    monkeypatch.setattr("scrapy.extensions.spiderstate.os.open", open_dir)
    monkeypatch.setattr("scrapy.extensions.spiderstate.os.close", close)
    with caplog.at_level(logging.WARNING, logger=spiderstate.__name__):
        spiderstate.SpiderState(str(jobdir))._persist_state(_spider_with({"new": 1}))

    assert pickle.loads(_read_state_bytes(jobdir)) == {"new": 1}
    assert "simulated directory close failure" in caplog.text


@coroutine_test
async def test_signal_order(jobdir: Path) -> None:
    """State is loaded before it is sent, and saved only after every saving
    handler has finished."""
    crawler = get_crawler(Spider, {"JOBDIR": str(jobdir)})
    ss = build_from_crawler(SpiderState, crawler)
    events: list[str] = []

    def on_loaded(state: dict[str, Any]) -> None:
        events.append("loaded")
        state["loaded"] = True

    def on_saving(state: dict[str, Any]) -> None:
        events.append("saving")

    crawler.signals.connect(on_loaded, signal=signals.spider_state_loaded, weak=False)
    crawler.signals.connect(on_saving, signal=signals.spider_state_saving, weak=False)

    spider = _spider_with()
    await ss._spider_opened(spider)
    assert events == ["loaded"]
    assert spider.state == {"loaded": True}

    await ss._spider_closed(spider)
    assert events == ["loaded", "saving"]
    assert pickle.loads(_read_state_bytes(jobdir)) == {"loaded": True}


@coroutine_test
async def test_async_saving_handler_finishes_before_save(jobdir: Path) -> None:
    crawler = get_crawler(Spider, {"JOBDIR": str(jobdir)})
    ss = build_from_crawler(SpiderState, crawler)
    events: list[str] = []

    async def on_saving(state: dict[str, Any]) -> None:
        events.append("start")
        await _yield_control()
        state["async"] = True
        events.append("end")

    crawler.signals.connect(on_saving, signal=signals.spider_state_saving, weak=False)

    spider = _spider_with()
    await ss._spider_opened(spider)
    await ss._spider_closed(spider)

    assert events == ["start", "end"]
    assert pickle.loads(_read_state_bytes(jobdir)) == {"async": True}


async def _yield_control() -> None:
    await asyncio.sleep(0)


@coroutine_test
async def test_saving_handler_failure_keeps_signal_behavior(
    jobdir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing handler is logged and does not prevent the save."""
    crawler = get_crawler(Spider, {"JOBDIR": str(jobdir)})
    ss = build_from_crawler(SpiderState, crawler)

    async def on_saving(state: dict[str, Any]) -> None:
        raise RuntimeError("handler failed")

    crawler.signals.connect(on_saving, signal=signals.spider_state_saving, weak=False)

    spider = _spider_with({"key": "value"})
    with caplog.at_level(logging.ERROR):
        await ss._spider_closed(spider)

    assert pickle.loads(_read_state_bytes(jobdir)) == {"key": "value"}
    assert "handler failed" in caplog.text


@coroutine_test
async def test_loaded_handler_failure_keeps_signal_behavior(
    jobdir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing handler is logged and does not prevent the state from
    loading."""
    crawler = get_crawler(Spider, {"JOBDIR": str(jobdir)})
    ss = build_from_crawler(SpiderState, crawler)
    _save_old_state(jobdir, {"loaded": 1})

    def on_loaded(state: dict[str, Any]) -> None:
        raise RuntimeError("handler failed")

    crawler.signals.connect(on_loaded, signal=signals.spider_state_loaded, weak=False)

    spider = _spider_with()
    with caplog.at_level(logging.ERROR):
        await ss._spider_opened(spider)

    assert spider.state == {"loaded": 1}
    assert "handler failed" in caplog.text


def test_deprecated_entry_is_atomic(
    jobdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deprecated synchronous entry point goes through the same atomic
    implementation."""
    old_bytes = _save_old_state(jobdir)
    crawler = get_crawler(Spider, {"JOBDIR": str(jobdir)})
    ss = build_from_crawler(SpiderState, crawler)

    def replace(_src: Any, _dst: Any) -> None:
        raise OSError(errno.ENOSPC, "simulated replace failure")

    monkeypatch.setattr("scrapy.extensions.spiderstate.os.replace", replace)
    spider = _spider_with({"new": 1})
    with (
        pytest.warns(ScrapyDeprecationWarning, match="spider_closed"),
        pytest.raises(OSError, match="simulated replace failure"),
    ):
        ss.spider_closed(spider)

    assert _read_state_bytes(jobdir) == old_bytes
    assert _temp_names(jobdir) == []
