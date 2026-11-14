from __future__ import annotations

import io
import json
import os
import struct
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

import pytest
from queuelib import queue as legacy_queue

from scrapy._frame_disk_queue import (
    _FRAME_MAGIC,
    FrameFifoDiskQueue,
    FrameLifoDiskQueue,
    FrameQueueError,
    _encode_frame,
    _read_frame,
)

if TYPE_CHECKING:
    from collections.abc import Generator


class _FailingFifo(FrameFifoDiskQueue):
    """Frame FIFO queue whose head stream raises a controlled fault."""

    fail_after: int | None = None
    fail_on: str = "write"

    def _write_all(self, stream: Any, data: bytes) -> None:  # type: ignore[override]
        if self.fail_on == "write" and self.fail_after is not None:
            cut = min(len(data), self.fail_after)
            if cut:
                os.write(stream.fileno(), data[:cut])
            raise OSError("injected short write")
        super()._write_all(stream, data)

    def _sync_stream(self, stream: Any) -> None:  # type: ignore[override]
        if self.fail_on == "fsync":
            raise OSError("injected fsync failure")
        super()._sync_stream(stream)


class _FailingLifo(FrameLifoDiskQueue):
    fail_after: int | None = None
    fail_on: str = "write"

    def _write_all(self, stream: Any, data: bytes) -> None:  # type: ignore[override]
        if self.fail_on == "write" and self.fail_after is not None:
            cut = min(len(data), self.fail_after)
            if cut:
                os.write(stream.fileno(), data[:cut])
            raise OSError("injected short write")
        super()._write_all(stream, data)

    def _sync_stream(self, stream: Any) -> None:  # type: ignore[override]
        if self.fail_on == "fsync":
            raise OSError("injected fsync failure")
        super()._sync_stream(stream)


@pytest.fixture
def fifo(tmp_path: Path) -> Generator[FrameFifoDiskQueue]:
    queue = FrameFifoDiskQueue(str(tmp_path / "fifo"), chunksize=3)
    yield queue
    queue.close()


@pytest.fixture
def lifo(tmp_path: Path) -> Generator[FrameLifoDiskQueue]:
    queue = FrameLifoDiskQueue(str(tmp_path / "lifo"))
    yield queue
    queue.close()


# ---------------------------------------------------------------------------
# frame codec
# ---------------------------------------------------------------------------


def test_encode_decode_roundtrip() -> None:
    payload = b"the serialized request"
    frame = _encode_frame(payload)
    assert frame[:4] == _FRAME_MAGIC
    decoded, torn = _read_frame(_io(frame))
    assert not torn
    assert decoded == payload


def test_frame_header_carries_version_and_length() -> None:
    payload = b"x" * 40
    frame = _encode_frame(payload)
    header_size = struct.calcsize(">4sLQ")
    magic, version, length = struct.unpack(">4sLQ", frame[:header_size])
    assert magic == _FRAME_MAGIC
    assert version == 1
    assert length == 40


def test_read_frame_torn_when_truncated() -> None:
    frame = _encode_frame(b"abc")
    for cut in (1, len(frame) - 1, len(frame) - 3):
        _, torn = _read_frame(_io(frame[:cut]))
        assert torn


def test_read_frame_detects_digest_tampering() -> None:
    frame = bytearray(_encode_frame(b"abc"))
    # The digest occupies the bytes right after the fixed-size header.
    header_size = 20
    frame[header_size + 3] ^= 0xFF
    with pytest.raises(FrameQueueError, match="Corrupt frame"):
        _read_frame(_io(bytes(frame)))


def test_read_frame_detects_payload_tampering() -> None:
    frame = bytearray(_encode_frame(b"abc"))
    frame[-4] ^= 0xFF
    with pytest.raises(FrameQueueError):
        _read_frame(_io(bytes(frame)))


def test_read_frame_detects_bad_commit_marker() -> None:
    frame = bytearray(_encode_frame(b"abc"))
    frame[-1] ^= 0xFF
    with pytest.raises(FrameQueueError, match="Corrupt frame"):
        _read_frame(_io(bytes(frame)))


def test_read_frame_rejects_foreign_magic() -> None:
    frame = bytearray(_encode_frame(b"abc"))
    frame[0] = ord("q")
    with pytest.raises(FrameQueueError, match="Unrecognized frame header"):
        _read_frame(_io(bytes(frame)))


def _io(data: bytes) -> BinaryIO:
    stream = io.BytesIO(data)
    stream.name = "<memory>"
    return stream


# ---------------------------------------------------------------------------
# FIFO/LIFO order, peek and len
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("queue_cls", "is_fifo"),
    [
        (FrameFifoDiskQueue, True),
        (FrameLifoDiskQueue, False),
    ],
)
def test_order_peek_len(tmp_path: Path, queue_cls: Any, is_fifo: bool) -> None:
    queue = queue_cls(str(tmp_path / "q"))
    assert len(queue) == 0
    assert queue.pop() is None
    assert queue.peek() is None
    items = [f"item-{i}".encode() for i in range(6)]
    for item in items:
        queue.push(item)
        assert len(queue) == items.index(item) + 1
    expected = items if is_fifo else list(reversed(items))
    for index, item in enumerate(expected):
        assert queue.peek() == item
        assert queue.pop() == item
        assert len(queue) == len(expected) - index - 1
    assert queue.pop() is None
    assert queue.peek() is None
    queue.close()


def test_fifo_rolls_over_chunks(fifo: FrameFifoDiskQueue) -> None:
    for i in range(7):
        fifo.push(f"r{i}".encode())
    assert sorted(p.name for p in Path(fifo.path).iterdir() if p.name[0] == "f") == [
        "f00000",
        "f00001",
        "f00002",
    ]
    assert [fifo.pop() for _ in range(7)] == [f"r{i}".encode() for i in range(7)]
    # Fully consumed earlier chunks are removed.
    assert fifo.pop() is None


def test_fifo_close_reopen_keeps_order(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    queue = FrameFifoDiskQueue(key, chunksize=2)
    for i in range(5):
        queue.push(f"r{i}".encode())
    assert queue.pop() == b"r0"
    queue.close()

    queue = FrameFifoDiskQueue(key, chunksize=2)
    assert len(queue) == 4
    assert queue.peek() == b"r1"
    assert [queue.pop() for _ in range(4)] == [f"r{i}".encode() for i in range(1, 5)]
    queue.close()


def test_lifo_close_reopen_keeps_order(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    queue = FrameLifoDiskQueue(key)
    for i in range(4):
        queue.push(f"r{i}".encode())
    assert queue.pop() == b"r3"
    queue.close()

    queue = FrameLifoDiskQueue(key)
    assert len(queue) == 3
    assert queue.peek() == b"r2"
    assert [queue.pop() for _ in range(3)] == [b"r2", b"r1", b"r0"]
    queue.close()


# ---------------------------------------------------------------------------
# push fault isolation: uncommitted frames never become visible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fail_on", ["write", "fsync"])
def test_fifo_failed_push_is_invisible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_on: str
) -> None:
    key = str(tmp_path / "q")
    queue = FrameFifoDiskQueue(key, chunksize=10)
    queue.push(b"committed-1")
    queue.close()

    failing = _FailingFifo(key, chunksize=10)
    failing.fail_on = fail_on
    if fail_on == "write":
        failing.fail_after = 5
    monkeypatch.setattr(failing, "fail_on", fail_on)
    with pytest.raises(OSError, match="injected"):
        failing.push(b"never-committed")
    failing.close()

    queue = FrameFifoDiskQueue(key, chunksize=10)
    assert len(queue) == 1
    assert queue.pop() == b"committed-1"
    # A subsequent push appends a clean frame after the rolled-back bytes.
    queue.push(b"committed-2")
    assert queue.pop() == b"committed-2"
    queue.close()


@pytest.mark.parametrize("fail_on", ["write", "fsync"])
def test_lifo_failed_push_is_invisible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_on: str
) -> None:
    key = str(tmp_path / "q")
    queue = FrameLifoDiskQueue(key)
    queue.push(b"committed-1")
    queue.close()

    failing = _FailingLifo(key)
    monkeypatch.setattr(failing, "fail_on", fail_on)
    if fail_on == "write":
        failing.fail_after = 5
    with pytest.raises(OSError, match="injected"):
        failing.push(b"never-committed")
    # The close-time sync may report the same injected fault, but the lock is
    # released either way so a fresh process can reopen the job directory.
    with suppress(OSError):
        failing.close()

    queue = FrameLifoDiskQueue(key)
    assert len(queue) == 1
    assert queue.pop() == b"committed-1"
    queue.push(b"committed-2")
    assert queue.pop() == b"committed-2"
    queue.close()


# ---------------------------------------------------------------------------
# torn tail is discarded; middle corruption is an error that keeps the scene
# ---------------------------------------------------------------------------


def test_fifo_torn_tail_dropped_on_reopen(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    queue = FrameFifoDiskQueue(key, chunksize=10)
    queue.push(b"a")
    queue.push(b"b")
    # Simulate a process killed mid-write: bytes without a committed frame.
    queue._headf.write(_FRAME_MAGIC + b"\x00\x00\x00\x01partial")
    queue._headf.flush()
    queue.close()

    queue = FrameFifoDiskQueue(key, chunksize=10)
    assert len(queue) == 2
    assert [queue.pop(), queue.pop()] == [b"a", b"b"]
    queue.push(b"c")
    assert queue.pop() == b"c"
    queue.close()


def test_lifo_torn_tail_dropped_on_reopen(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    queue = FrameLifoDiskQueue(key)
    queue.push(b"a")
    queue.push(b"b")
    queue._stream.write(_FRAME_MAGIC + b"\x00\x00\x00\x01partial")
    queue._stream.flush()
    queue.close()

    queue = FrameLifoDiskQueue(key)
    assert len(queue) == 2
    assert [queue.pop(), queue.pop()] == [b"b", b"a"]
    queue.close()


def _corrupt_middle(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))


def _simulate_kill(queue: FrameFifoDiskQueue | FrameLifoDiskQueue) -> None:
    """Mimic a killed process: close the open files and drop the lock without
    writing the close-time state, so the pop position is not persisted."""
    if isinstance(queue, FrameFifoDiskQueue):
        queue._headf.close()
        queue._tailf.close()
    else:
        queue._stream.close()
    queue._lock.release()
    queue._opened = False


def test_fifo_unclean_stop_replays_surviving_frames(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    queue = FrameFifoDiskQueue(key, chunksize=2)
    for index in range(3):
        queue.push(f"r{index}".encode())
    for _ in range(3):
        queue.pop()
    _simulate_kill(queue)

    # The consumed first chunk was deleted; the surviving head chunk is
    # replayed on restart because the pop position died with the process,
    # which is at-least-once rather than a silent loss.
    reopened = FrameFifoDiskQueue(key, chunksize=2)
    assert Path(key, "f00000").exists() is False
    assert reopened.pop() == b"r2"
    assert reopened.pop() is None
    reopened.push(b"fresh")
    assert reopened.pop() == b"fresh"
    reopened.close()


def test_fifo_clean_close_never_replays(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    queue = FrameFifoDiskQueue(key, chunksize=2)
    for index in range(3):
        queue.push(f"r{index}".encode())
    for _ in range(3):
        queue.pop()
    queue.close()

    reopened = FrameFifoDiskQueue(key, chunksize=2)
    assert len(reopened) == 0
    assert reopened.pop() is None
    reopened.close()


def test_lifo_unclean_stop_drops_only_torn_tail(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    queue = FrameLifoDiskQueue(key)
    queue.push(b"a")
    queue.push(b"b")
    assert queue.pop() == b"b"
    _simulate_kill(queue)

    reopened = FrameLifoDiskQueue(key)
    # The committed frame "a" is still present and is delivered again rather
    # than being lost.
    assert reopened.pop() == b"a"
    assert reopened.pop() is None
    reopened.close()


def test_fifo_middle_corruption_raises_and_keeps_files(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    queue = FrameFifoDiskQueue(key)
    queue.push(b"a")
    queue.push(b"b")
    queue.push(b"c")
    queue.close()

    chunk = Path(key) / "f00000"
    _corrupt_middle(chunk)
    with pytest.raises(FrameQueueError):
        FrameFifoDiskQueue(key)
    # The scene is preserved for manual recovery; nothing was skipped.
    assert chunk.exists()


def test_lifo_middle_corruption_raises_and_keeps_files(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    queue = FrameLifoDiskQueue(key)
    queue.push(b"a")
    queue.push(b"b")
    queue.close()

    data_file = tmp_path / "q.frames"
    _corrupt_middle(data_file)
    with pytest.raises(FrameQueueError):
        FrameLifoDiskQueue(key)
    assert data_file.exists()


def test_fifo_state_size_mismatch_raises(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    queue = FrameFifoDiskQueue(key)
    queue.push(b"a")
    queue.close()
    state_path = Path(key) / "frames.json"
    state = json.loads(state_path.read_text())
    state["size"] = 99
    state_path.write_text(json.dumps(state))
    with pytest.raises(FrameQueueError, match="records 99"):
        FrameFifoDiskQueue(key)
    assert state_path.exists()


def test_fifo_corrupt_state_raises_and_keeps_scene(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    queue = FrameFifoDiskQueue(key)
    queue.push(b"a")
    queue.close()
    state_path = Path(key) / "frames.json"
    state_path.write_text("{broken")
    with pytest.raises(FrameQueueError, match="Unreadable queue state"):
        FrameFifoDiskQueue(key)
    assert state_path.exists()


def test_fifo_migration_removes_legacy_files(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    legacy = legacy_queue.FifoDiskQueue(key)
    legacy.push(b"old")
    legacy.close()
    assert (Path(key) / "info.json").exists()

    frame_queue = FrameFifoDiskQueue(key)
    try:
        assert frame_queue.pop() == b"old"
        assert not (Path(key) / "info.json").exists()
        assert not list(Path(key).glob("q*"))
    finally:
        frame_queue.close()


def test_lifo_migration_replaces_legacy_file(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    legacy = legacy_queue.LifoDiskQueue(key)
    legacy.push(b"old")
    legacy.close()
    assert Path(key).is_file()

    frame_queue = FrameLifoDiskQueue(key)
    try:
        assert frame_queue.pop() == b"old"
        assert Path(key).is_dir()
        assert (tmp_path / "q.frames").exists()
    finally:
        frame_queue.close()


# ---------------------------------------------------------------------------
# locking, idempotent close, empty cleanup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("queue_cls", [FrameFifoDiskQueue, FrameLifoDiskQueue])
def test_second_process_rejected_before_writes(tmp_path: Path, queue_cls: Any) -> None:
    key = str(tmp_path / "q")
    first = queue_cls(key)
    first.push(b"a")
    with pytest.raises(FrameQueueError, match="Another crawl process"):
        queue_cls(key)
    first.close()
    # Once released the same job directory can be opened again.
    second = queue_cls(key)
    assert second.pop() == b"a"
    second.close()


@pytest.mark.parametrize("queue_cls", [FrameFifoDiskQueue, FrameLifoDiskQueue])
def test_close_is_idempotent(tmp_path: Path, queue_cls: Any) -> None:
    key = str(tmp_path / "q")
    queue = queue_cls(key)
    queue.push(b"a")
    queue.close()
    queue.close()
    assert len(queue) == 1


@pytest.mark.parametrize("queue_cls", [FrameFifoDiskQueue, FrameLifoDiskQueue])
def test_empty_queue_removes_its_files(tmp_path: Path, queue_cls: Any) -> None:
    key = tmp_path / "q"
    queue = queue_cls(str(key))
    queue.close()
    assert not key.exists()
    assert not (tmp_path / "q.lock").exists()


def test_failed_push_not_counted_after_close(tmp_path: Path) -> None:
    key = str(tmp_path / "q")
    queue = _FailingFifo(key, chunksize=10)
    queue.push(b"ok")
    queue.fail_on = "write"
    queue.fail_after = 3
    with pytest.raises(OSError, match="injected"):
        queue.push(b"bad")
    assert len(queue) == 1
    queue.close()
    reopened = FrameFifoDiskQueue(key, chunksize=10)
    assert len(reopened) == 1
    reopened.close()
