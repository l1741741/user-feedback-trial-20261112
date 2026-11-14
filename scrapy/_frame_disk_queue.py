from __future__ import annotations

import hashlib
import json
import os
import struct
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, cast

from queuelib import queue

if os.name == "nt":  # pragma: no cover - exercised only on Windows
    import msvcrt  # pylint: disable=import-error
else:
    import fcntl

if TYPE_CHECKING:
    from os import PathLike

# Every frame starts with this fixed marker. Legacy queues never write it:
# the FIFO layout marks its directory with info.json and names chunks q*, while
# the LIFO layout begins with a 4-byte big-endian item count, so the marker
# plus the directory/file layout together tell the two formats apart.
_FRAME_MAGIC = b"SQF1"
_FRAME_VERSION = 1
_FRAME_HEADER_FORMAT = ">4sLQ"
_FRAME_HEADER_SIZE = struct.calcsize(_FRAME_HEADER_FORMAT)
_FRAME_DIGEST_SIZE = 16
_FRAME_TRAILER = b"CMT"
_FRAME_TRAILER_SIZE = len(_FRAME_TRAILER)
_FRAME_META_SIZE = _FRAME_HEADER_SIZE + _FRAME_DIGEST_SIZE + _FRAME_TRAILER_SIZE
_FIFO_CHUNK_PREFIX = "f"
_FIFO_STATE_FILE = "frames.json"
_LIFO_DATA_SUFFIX = ".frames"
_LOCK_SUFFIX = ".lock"
_MIGRATION_SUFFIX = ".migrate"
_DEFAULT_CHUNKSIZE = 100_000


class FrameQueueError(Exception):
    """Raised when a frame queue on disk cannot be read safely.

    The queue directory is left untouched so the crawl can be inspected and
    resumed manually instead of silently skipping requests.
    """


def _encode_frame(payload: bytes) -> bytes:
    digest = hashlib.blake2b(payload, digest_size=_FRAME_DIGEST_SIZE).digest()
    return (
        struct.pack(
            _FRAME_HEADER_FORMAT,
            _FRAME_MAGIC,
            _FRAME_VERSION,
            len(payload),
        )
        + digest
        + payload
        + _FRAME_TRAILER
    )


def _read_frame(stream: BinaryIO) -> tuple[bytes, bool]:
    """Read one frame from *stream* at its current offset.

    Return the payload and whether the frame was only partially present. A
    partial frame is only tolerated at the end of the storage, where an
    interrupted write can leave a torn frame behind.
    """
    source = getattr(stream, "name", repr(stream))
    start = stream.tell()
    header = stream.read(_FRAME_HEADER_SIZE)
    if not header:
        return b"", True
    if len(header) < _FRAME_HEADER_SIZE:
        return b"", True
    magic, version, length = struct.unpack(_FRAME_HEADER_FORMAT, header)
    if magic != _FRAME_MAGIC or version != _FRAME_VERSION:
        raise FrameQueueError(
            f"Unrecognized frame header {magic!r} in {source!r} at offset {start}"
        )
    body = stream.read(_FRAME_DIGEST_SIZE + length + _FRAME_TRAILER_SIZE)
    if len(body) < _FRAME_DIGEST_SIZE + _FRAME_TRAILER_SIZE:
        return b"", True
    if len(body) < _FRAME_DIGEST_SIZE + length + _FRAME_TRAILER_SIZE:
        return b"", True
    digest = body[:_FRAME_DIGEST_SIZE]
    payload = body[_FRAME_DIGEST_SIZE : _FRAME_DIGEST_SIZE + length]
    trailer = body[_FRAME_DIGEST_SIZE + length :]
    expected = hashlib.blake2b(payload, digest_size=_FRAME_DIGEST_SIZE).digest()
    if digest != expected or trailer != _FRAME_TRAILER:
        raise FrameQueueError(
            f"Corrupt frame in {source!r} at offset {start}: "
            "checksum or commit marker mismatch"
        )
    return payload, False


def _sync_directory(path: Path) -> None:
    """Make file creation, replacement and deletion in *path* durable."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some platforms (e.g. Windows) do not support fsync on directories.
        pass
    finally:
        os.close(descriptor)


class _FileLock:
    """An advisory exclusive lock backed by a file next to the queue data."""

    def __init__(self, path: Path):
        self._path = path
        self._descriptor: int | None = None

    def acquire(self) -> None:
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":  # pragma: no cover - exercised only on Windows
                try:
                    # msvcrt only exists on Windows, so its attributes are
                    # unknown to type checkers on other platforms.
                    msvcrt.locking(  # type: ignore[attr-defined]
                        descriptor,
                        msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                        1,
                    )
                except OSError as ex:
                    raise FrameQueueError(
                        f"Another crawl process is already using {self._path.parent}"
                    ) from ex
            else:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as ex:
                    raise FrameQueueError(
                        f"Another crawl process is already using {self._path.parent}"
                    ) from ex
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        if self._descriptor is None:
            return
        descriptor = self._descriptor
        self._descriptor = None
        if os.name != "nt":
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        else:  # pragma: no cover - exercised only on Windows
            with suppress(OSError):
                msvcrt.locking(  # type: ignore[attr-defined]
                    descriptor,
                    msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                    1,
                )
        os.close(descriptor)


class _BaseFrameDiskQueue:
    """Shared open, locking and migration logic for frame disk queues."""

    def __init__(self, path: str | PathLike[str]) -> None:
        self.path = str(path)
        self._directory = Path(self.path)
        self._lock = _FileLock(
            self._directory.parent / f"{self._directory.name}{_LOCK_SUFFIX}"
        )
        self._opened = True
        self._lock.acquire()
        try:
            self._init_storage()
        except BaseException:
            self._lock.release()
            raise

    def _init_storage(self) -> None:
        raise NotImplementedError

    @staticmethod
    def _sync_stream(stream: BinaryIO) -> None:
        """Flush a write all the way to disk. Overridable in tests to inject
        an fsync failure without losing the rollback behavior."""
        os.fsync(stream.fileno())

    @staticmethod
    def _write_all(stream: BinaryIO, data: bytes) -> None:
        """Write every byte of *data* or raise, so a partial OS write cannot
        be mistaken for a committed frame."""
        view = memoryview(data)
        while view:
            written = stream.write(view)
            if not written:
                raise FrameQueueError(
                    f"Short write to {getattr(stream, 'name', stream)!r}"
                )
            view = view[written:]

    def close(self) -> None:
        if not self._opened:
            return
        self._opened = False
        try:
            self._close_storage()
        finally:
            # Releasing the lock even when the final sync fails keeps a later
            # process able to recover the queue instead of seeing it locked.
            self._lock.release()
        if len(self) == 0:
            self._cleanup()

    def _close_storage(self) -> None:
        raise NotImplementedError

    def __len__(self) -> int:
        raise NotImplementedError

    def _cleanup(self) -> None:
        if self._directory.is_dir():
            for child in self._directory.glob("*"):
                with suppress(OSError):
                    child.unlink()
        with suppress(OSError):
            self._directory.rmdir()
        with suppress(OSError):
            self._lock._path.unlink()


class _FrameMigrationSink:
    """Write frames into hidden chunk files before a legacy queue is removed."""

    def __init__(self, directory: Path, chunksize: int):
        self._directory = directory
        self._chunksize = chunksize
        self._number = 0
        self._count = 0
        self._staging: list[Path] = []
        directory.mkdir(parents=True, exist_ok=True)
        self._stream = self._open()

    def _staging_path(self, number: int) -> Path:
        return self._directory / f".{_FIFO_CHUNK_PREFIX}{number:05d}{_MIGRATION_SUFFIX}"

    def _open(self) -> BinaryIO:
        path = self._staging_path(self._number)
        self._staging.append(path)
        return cast("BinaryIO", path.open("ab+"))

    def write_frame(self, frame: bytes) -> None:
        self._stream.write(frame)
        self._count += 1
        if self._count == self._chunksize:
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._stream.close()
            self._number += 1
            self._count = 0
            self._stream = self._open()

    def commit(self) -> None:
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        for number, staging in enumerate(self._staging):
            staging.replace(self._directory / f"{_FIFO_CHUNK_PREFIX}{number:05d}")
        _sync_directory(self._directory)


class FrameFifoDiskQueue(_BaseFrameDiskQueue):
    """Persistent FIFO queue storing each item as an atomic checked frame.

    Frames are appended to chunk files and become visible to :meth:`pop` and
    :meth:`__len__` only once the whole frame, including its commit marker, has
    been written and synced. A frame torn by an interrupted write is discarded
    on the next open when it is the last frame of the last chunk; damage in the
    middle of the storage raises :class:`FrameQueueError` and keeps every file
    in place.
    """

    def __init__(
        self, path: str | PathLike[str], chunksize: int = _DEFAULT_CHUNKSIZE
    ) -> None:
        self.chunksize = chunksize
        self._tailnum: int
        self._tail_offset: int
        self._headnum: int
        self._head_count: int
        self._size: int
        self._state_present: bool
        super().__init__(path)

    def _chunk_path(self, number: int) -> Path:
        return self._directory / f"{_FIFO_CHUNK_PREFIX}{number:05d}"

    def _state_path(self) -> Path:
        return self._directory / _FIFO_STATE_FILE

    def _chunk_numbers(self) -> list[int]:
        return sorted(
            int(path.name[1:])
            for path in self._directory.glob(f"{_FIFO_CHUNK_PREFIX}[0-9]*")
        )

    def _init_storage(self) -> None:
        legacy_info = self._directory / "info.json"
        if legacy_info.is_file() and not self._chunk_numbers():
            self._migrate_legacy()
        else:
            self._directory.mkdir(parents=True, exist_ok=True)
        self._load_state()
        self._scan_pending()
        # With no frames pending, the recorded tail chunk may have been
        # removed after being fully consumed. Reopen the head chunk as the
        # read position instead of a tail file that no longer exists.
        self._headf = self._open_chunk(self._headnum, "ab+")
        if not self._chunk_path(self._tailnum).exists():
            self._tailnum = self._headnum
            self._tail_offset = 0
        self._tailf = self._open_chunk(self._tailnum, "rb")
        self._tailf.seek(self._tail_offset)

    def _migrate_legacy(self) -> None:
        # The legacy FIFO layout (info.json and q* chunks) shares this
        # directory. Drain it into a sibling staging directory first, then let
        # the legacy queue remove its own empty directory before moving the
        # frame chunks into a recreated directory at the same path.
        staging = self._directory.parent / f".{self._directory.name}.migrate"
        legacy = queue.FifoDiskQueue(self.path)
        sink = _FrameMigrationSink(staging, self.chunksize)
        try:
            while True:
                item = legacy.pop()
                if item is None:
                    break
                sink.write_frame(_encode_frame(item))
            sink.commit()
        finally:
            legacy.close()
        self._directory.mkdir(parents=True, exist_ok=True)
        for chunk in staging.glob(f"{_FIFO_CHUNK_PREFIX}[0-9]*"):
            chunk.replace(self._directory / chunk.name)
        staging.rmdir()

    def _load_state(self) -> None:
        state_path = self._state_path()
        if state_path.exists():
            try:
                state = cast("dict[str, Any]", json.loads(state_path.read_text()))
            except json.JSONDecodeError as ex:
                raise FrameQueueError(
                    f"Unreadable queue state {state_path}: {ex.msg}"
                ) from ex
            self._tailnum = cast("int", state["tail"][0])
            self._tail_offset = cast("int", state["tail"][1])
            self._size = cast("int", state["size"])
            self._state_present = True
        else:
            # Without a state the previous process never closed cleanly, so
            # every frame still on disk is pending: pops died with the process.
            self._tailnum = 0
            self._tail_offset = 0
            self._size = 0
            self._state_present = False

    def _scan_pending(self) -> int:
        """Validate frames from the read position onward.

        Only a frame torn by an interrupted write at the very end of the last
        chunk is discarded; any other unreadable data raises, keeping the
        queue directory intact for manual inspection.
        """
        numbers = self._chunk_numbers()
        if not numbers:
            self._headnum = 0
            self._head_count = 0
            self._tailnum = 0
            self._tail_offset = 0
            self._size = 0
            return 0
        if self._state_present and self._tailnum not in numbers:
            raise FrameQueueError(
                f"State of {self._directory} points at missing chunk {self._tailnum}"
            )
        self._headnum = numbers[-1]
        pending = 0
        for number in numbers:
            if number < self._tailnum:
                continue
            path = self._chunk_path(number)
            with path.open("rb+") as stream:
                if number == self._tailnum:
                    stream.seek(self._tail_offset)
                while True:
                    start = stream.tell()
                    header = stream.read(_FRAME_HEADER_SIZE)
                    if not header:
                        # Clean end of a fully consumed chunk, not a torn
                        # frame: there are no uncommitted bytes left here.
                        break
                    if len(header) < _FRAME_HEADER_SIZE:
                        self._reject_or_truncate_torn(stream, path, number, start)
                        break
                    stream.seek(start)
                    _, torn = _read_frame(stream)
                    if not torn:
                        pending += 1
                        continue
                    self._reject_or_truncate_torn(stream, path, number, start)
                    break
        if self._state_present:
            if pending != self._size:
                raise FrameQueueError(
                    f"State of {self._directory} records {self._size} pending "
                    f"requests but {pending} frames are readable"
                )
        else:
            self._size = pending
        self._head_count = self._count_frames(self._chunk_path(self._headnum))
        return pending

    def _reject_or_truncate_torn(
        self,
        stream: BinaryIO,
        path: Path,
        number: int,
        start: int,
    ) -> None:
        # Only the final frame of the head chunk may be an interrupted
        # append; torn data anywhere else means real corruption.
        if number != self._headnum:
            raise FrameQueueError(f"Torn frame in the middle of {path!r}")
        stream.seek(start)
        stream.truncate()

    @staticmethod
    def _count_frames(path: Path) -> int:
        count = 0
        with path.open("rb") as stream:
            while True:
                _, torn = _read_frame(stream)
                if torn:
                    return count
                count += 1

    def _open_chunk(self, number: int, mode: str = "rb") -> BinaryIO:
        return cast("BinaryIO", self._chunk_path(number).open(mode))

    def push(self, string: bytes) -> None:
        if not isinstance(string, bytes):
            raise TypeError(f"Unsupported type: {type(string).__name__}")
        frame = _encode_frame(string)
        offset = self._headf.tell()
        try:
            self._write_all(self._headf, frame)
            self._headf.flush()
            self._sync_stream(self._headf)
        except BaseException:
            # Roll the uncommitted bytes back so the failed push stays
            # invisible and a later push still appends a complete frame.
            self._headf.seek(offset)
            self._headf.truncate()
            raise
        self._head_count += 1
        self._size += 1
        if self._head_count == self.chunksize:
            self._headf.close()
            _sync_directory(self._directory)
            self._headnum += 1
            self._head_count = 0
            self._headf = self._open_chunk(self._headnum, "ab+")

    def pop(self) -> bytes | None:
        if self._size == 0:
            return None
        payload, torn = _read_frame(self._tailf)
        if torn:
            raise FrameQueueError(
                f"Pending frame at the read position of {self._tailf.name!r} "
                "is incomplete"
            )
        self._tail_offset = self._tailf.tell()
        self._size -= 1
        chunk_exhausted = (
            self._tail_offset == self._chunk_path(self._tailnum).stat().st_size
        )
        if chunk_exhausted and self._tailnum < self._headnum:
            self._tailf.close()
            self._chunk_path(self._tailnum).unlink()
            self._tailnum += 1
            self._tail_offset = 0
            self._tailf = self._open_chunk(self._tailnum, "rb")
            _sync_directory(self._directory)
        return payload

    def peek(self) -> bytes | None:
        if self._size == 0:
            return None
        position = self._tailf.tell()
        payload, torn = _read_frame(self._tailf)
        if torn:
            raise FrameQueueError(
                f"Pending frame at the read position of {self._tailf.name!r} "
                "is incomplete"
            )
        self._tailf.seek(position)
        return payload

    def _save_state(self) -> None:
        state = {
            "version": _FRAME_VERSION,
            "chunksize": self.chunksize,
            "size": self._size,
            "tail": [self._tailnum, self._tail_offset],
            "head": [self._headnum, self._head_count],
        }
        state_path = self._state_path()
        tmp = state_path.with_name(f"{_FIFO_STATE_FILE}.tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(state_path)
        _sync_directory(self._directory)

    def _close_storage(self) -> None:
        self._headf.close()
        self._tailf.close()
        self._save_state()

    def __len__(self) -> int:
        return self._size


class FrameLifoDiskQueue(_BaseFrameDiskQueue):
    """Persistent LIFO queue storing each item as an atomic checked frame.

    New frames are appended to one data file and popped from its end, so a
    push that fails before the commit marker is written can be truncated away
    without touching any frame that was already visible.
    """

    def _data_path(self) -> Path:
        return self._directory.parent / f"{self._directory.name}{_LIFO_DATA_SUFFIX}"

    def _init_storage(self) -> None:
        data_path = self._data_path()
        legacy_path = Path(self.path)
        # A legacy LIFO queue keeps its data in a file at this path; a frame
        # queue keeps a directory here instead.
        if legacy_path.is_file() and not data_path.exists():
            self._migrate_legacy()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._size = 0
        self._offsets: list[int] = []
        if data_path.exists():
            self._scan(data_path)
        self._stream = (
            data_path.open("rb+") if data_path.exists() else data_path.open("wb+")
        )
        self._stream.seek(0, os.SEEK_END)

    def _migrate_legacy(self) -> None:
        legacy_path = Path(self.path)
        data_path = self._data_path()
        staging = data_path.with_name(data_path.name + _MIGRATION_SUFFIX)
        # Read the legacy LIFO file in forward order: it stores records from
        # oldest to newest as item bytes followed by a 4-byte big-endian
        # length. Draining it through pop() would reverse that order, so parse
        # the raw layout instead to keep LIFO semantics after migration.
        items = self._read_legacy_lifo(legacy_path)
        try:
            with staging.open("wb") as out:
                for item in items:
                    out.write(_encode_frame(item))
                out.flush()
                os.fsync(out.fileno())
            staging.replace(data_path)
            _sync_directory(data_path.parent)
        finally:
            if staging.exists():
                staging.unlink()
        # The legacy file is fully drained; remove it so the frame directory
        # can take over the path.
        if not legacy_path.exists():
            return
        if legacy_path.is_file():
            legacy_path.unlink()
        else:
            raise FrameQueueError(
                f"Cannot migrate legacy queue at {legacy_path}: path is not a file"
            )

    @staticmethod
    def _read_legacy_lifo(path: Path) -> list[bytes]:
        size_size = queue.LifoDiskQueue.SIZE_SIZE
        size_format = queue.LifoDiskQueue.SIZE_FORMAT
        raw = path.read_bytes()
        (stored_size,) = struct.unpack(size_format, raw[:size_size])
        body = raw[size_size:]
        parsed: list[bytes] = []
        end = len(body)
        for _ in range(stored_size):
            if end < size_size:
                raise FrameQueueError(
                    f"Legacy LIFO queue {path!r} is shorter than its records"
                )
            (length,) = struct.unpack(size_format, body[end - size_size : end])
            start = end - size_size - length
            if start < 0:
                raise FrameQueueError(
                    f"Record length {length} exceeds remaining data in {path!r}"
                )
            parsed.append(body[start : end - size_size])
            end = start
        parsed.reverse()
        if end != 0:
            raise FrameQueueError(f"Unread bytes remain in legacy queue {path!r}")
        return parsed

    def _scan(self, data_path: Path) -> None:
        offsets: list[int] = []
        with data_path.open("rb+") as stream:
            while True:
                start = stream.tell()
                header = stream.read(_FRAME_HEADER_SIZE)
                if not header:
                    # Clean end of the data file.
                    break
                stream.seek(start)
                _, torn = _read_frame(stream)
                if not torn:
                    offsets.append(start)
                    continue
                # Any bytes that do not form one complete, committed frame
                # must be the interrupted append at the very end; drop only
                # that tail.
                stream.seek(start)
                stream.truncate()
                break
        self._offsets = offsets
        self._size = len(offsets)

    def push(self, string: bytes) -> None:
        if not isinstance(string, bytes):
            raise TypeError(f"Unsupported type: {type(string).__name__}")
        frame = _encode_frame(string)
        offset = self._stream.tell()
        try:
            self._write_all(self._stream, frame)
            self._stream.flush()
            self._sync_stream(self._stream)
        except BaseException:
            self._stream.seek(offset)
            self._stream.truncate()
            raise
        self._offsets.append(offset)
        self._size += 1

    def pop(self) -> bytes | None:
        if self._size == 0:
            return None
        offset = self._offsets.pop()
        self._stream.seek(offset)
        payload, torn = _read_frame(self._stream)
        if torn:
            raise FrameQueueError(
                f"Pending frame at offset {offset} of {self._stream.name!r} "
                "is incomplete"
            )
        self._stream.seek(offset)
        self._stream.truncate()
        self._stream.seek(0, os.SEEK_END)
        self._size -= 1
        return payload

    def peek(self) -> bytes | None:
        if self._size == 0:
            return None
        self._stream.seek(self._offsets[-1])
        payload, torn = _read_frame(self._stream)
        if torn:
            raise FrameQueueError(
                f"Pending frame at offset {self._offsets[-1]} of "
                f"{self._stream.name!r} is incomplete"
            )
        self._stream.seek(0, os.SEEK_END)
        return payload

    def _close_storage(self) -> None:
        try:
            self._stream.flush()
            self._sync_stream(self._stream)
        finally:
            # A failing final sync must not keep the descriptor and lock open;
            # every committed frame was already synced when it was pushed.
            self._stream.close()

    def _cleanup(self) -> None:
        with suppress(OSError):
            self._data_path().unlink()
        super()._cleanup()

    def __len__(self) -> int:
        return self._size
