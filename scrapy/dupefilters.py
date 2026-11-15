from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Any, Self
from warnings import warn
from zlib import crc32

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.utils.conf import _job_dir
from scrapy.utils.deprecate import method_is_overridden
from scrapy.utils.request import (
    RequestFingerprinter,
    RequestFingerprinterProtocol,
    referer_str,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator

    from twisted.internet.defer import Deferred

    from scrapy.crawler import Crawler
    from scrapy.http.request import Request
    from scrapy.spiders import Spider


_SIZE_BYTES = 2
_CRC_BYTES = 4

_JOURNAL_MAGIC = b"SRFPJ\x00"
_JOURNAL_VERSION = 1
_JOURNAL_HEADER = _JOURNAL_MAGIC + _JOURNAL_VERSION.to_bytes(2, "big")
_JOURNAL_FILENAME = "requests.seen.journal"
_ORPHAN_FILENAME = "requests.seen.journal.orphan"
_LEGACY_FILENAME = "requests.seen"
_LEGACY_BACKUP_FILENAME = "requests.seen.legacy.bak"
_MAX_FINGERPRINT_SIZE = (1 << (_SIZE_BYTES * 8)) - 1
_MAX_RECORD_SIZE = _SIZE_BYTES + _MAX_FINGERPRINT_SIZE + _CRC_BYTES


class DupeFilterJournalError(Exception):
    """Base class for :class:`RFPDupeFilter` recoverable-journal errors."""


class JournalFormatError(DupeFilterJournalError):
    """The journal file is not a supported journal, or a legacy
    ``requests.seen`` file is present that requires :ref:`explicit migration
    <job-dir-dupefilter-journal>`.

    The original file is never modified when this is raised.
    """

    def __init__(self, message: str, *, path: Path):
        super().__init__(message)
        self.path = path


class JournalCorruptionError(DupeFilterJournalError):
    """A record in the middle of the journal failed validation while valid
    data exists beyond it.

    The original file is left untouched. :attr:`offset` is the byte offset of
    the first invalid record.
    """

    def __init__(self, message: str, *, path: Path, offset: int):
        super().__init__(message)
        self.path = path
        self.offset = offset


class JournalWriteError(DupeFilterJournalError):
    """Appending a record to the journal failed.

    The fingerprint was *not* added to the in-memory set, so a later
    :meth:`RFPDupeFilter.request_seen` call for the same request returns
    ``False`` and retries the append. The on-disk journal may contain a torn
    tail record, which is quarantined on the next open; already-committed
    records are never truncated.
    """


def _read_fingerprints(data: bytes) -> Iterator[bytes]:
    pos = 0
    while pos + _SIZE_BYTES <= len(data):
        size = int.from_bytes(data[pos : pos + _SIZE_BYTES], "big")
        pos += _SIZE_BYTES
        fingerprint = data[pos : pos + size]
        if len(fingerprint) < size:
            # Truncated by an unclean shutdown.
            return
        yield fingerprint
        pos += size


def _journal_record(fingerprint: bytes) -> bytes:
    if not fingerprint:
        raise ValueError("Cannot journal an empty fingerprint.")
    if len(fingerprint) > _MAX_FINGERPRINT_SIZE:
        raise ValueError(
            f"Cannot journal a fingerprint of {len(fingerprint)} bytes, "
            f"the maximum is {_MAX_FINGERPRINT_SIZE}."
        )
    body = len(fingerprint).to_bytes(_SIZE_BYTES, "big") + fingerprint
    return body + crc32(body).to_bytes(_CRC_BYTES, "big")


def _valid_record_at(data: bytes, pos: int) -> bool:
    """Whether a complete, checksum-valid record starts at *pos*."""
    if pos + _SIZE_BYTES > len(data):
        return False
    size = int.from_bytes(data[pos : pos + _SIZE_BYTES], "big")
    end = pos + _SIZE_BYTES + size + _CRC_BYTES
    if not size or end > len(data):
        return False
    body = data[pos : end - _CRC_BYTES]
    return crc32(body) == int.from_bytes(data[end - _CRC_BYTES : end], "big")


def _find_valid_record(data: bytes, start: int) -> int | None:
    """Offset of the first valid record at or after *start*, or ``None``."""
    for pos in range(start, len(data)):
        if _valid_record_at(data, pos):
            return pos
    return None


def _read_journal(data: bytes, *, path: Path) -> tuple[list[bytes], int]:
    """Parse journal *data*, including the header.

    Return the valid fingerprints and the *commit boundary*: the offset of
    the first byte after the last complete, checksum-valid record. Bytes past
    the commit boundary are an uncommitted or truncated tail that the caller
    must isolate.

    Raise :class:`JournalFormatError` if the header is missing or unsupported,
    and :class:`JournalCorruptionError` if an invalid record is followed by a
    valid one, or the unparseable tail is larger than a single record could
    be (mid-file corruption).
    """
    if len(data) < len(_JOURNAL_HEADER) or not data.startswith(_JOURNAL_MAGIC):
        raise JournalFormatError(
            f"{path} is not a dupefilter journal file (bad magic).",
            path=path,
        )
    version = int.from_bytes(data[len(_JOURNAL_MAGIC) : len(_JOURNAL_HEADER)], "big")
    if version != _JOURNAL_VERSION:
        raise JournalFormatError(
            f"{path} uses unsupported journal version {version}, "
            f"this Scrapy version supports version {_JOURNAL_VERSION}.",
            path=path,
        )
    fingerprints: list[bytes] = []
    pos = len(_JOURNAL_HEADER)
    while pos < len(data):
        if _valid_record_at(data, pos):
            size = int.from_bytes(data[pos : pos + _SIZE_BYTES], "big")
            fingerprints.append(data[pos + _SIZE_BYTES : pos + _SIZE_BYTES + size])
            pos += _SIZE_BYTES + size + _CRC_BYTES
            continue
        tail_size = len(data) - pos
        if tail_size > _MAX_RECORD_SIZE or _find_valid_record(data, pos + 1):
            raise JournalCorruptionError(
                f"Invalid record at offset {pos} in {path} with valid-looking "
                f"data beyond it; the file was left unmodified.",
                path=path,
                offset=pos,
            )
        break
    return fingerprints, pos


def _init_journal_file(path: Path) -> None:
    """Write the journal header to *path* (a new or empty file) and sync it."""
    with path.open("wb") as file:
        file.write(_JOURNAL_HEADER)
        file.flush()
        os.fsync(file.fileno())


class BaseDupeFilter:
    """Dummy duplicate request filtering class (:setting:`DUPEFILTER_CLASS`)
    that does not filter out any request."""

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls()

    def request_seen(self, request: Request) -> bool:
        return False

    def open(self) -> Coroutine[Any, Any, None] | Deferred[None] | None:
        pass

    def close(self, reason: str) -> Coroutine[Any, Any, None] | Deferred[None] | None:
        pass

    def log(self, request: Request, spider: Spider) -> None:
        """Log that a request has been filtered"""
        warn(
            "Calling BaseDupeFilter.log() is deprecated.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )


class RFPDupeFilter(BaseDupeFilter):
    """Duplicate request filtering class (:setting:`DUPEFILTER_CLASS`) that
    filters out requests with the canonical
    (:func:`w3lib.url.canonicalize_url`) :attr:`~scrapy.http.Request.url`,
    :attr:`~scrapy.http.Request.method` and :attr:`~scrapy.http.Request.body`.

    Job directory contents
    ======================

    .. warning:: The files that this class generates in the :ref:`job directory
        <job-dir>` are an implementation detail, and may change without a
        warning in a future version of Scrapy. Do not rely on the following
        information for anything other than debugging purposes.

    When using :setting:`JOBDIR`, seen fingerprints are tracked in a binary
    file named :file:`requests.seen` in the :ref:`job directory <job-dir>`,
    where each fingerprint is stored as its big-endian, 2-byte length followed
    by the fingerprint itself.

    With :setting:`DUPEFILTER_JOURNAL` enabled, a recoverable journal file
    named :file:`requests.seen.journal` is used instead. See
    :ref:`job-dir-dupefilter-journal` for the file format, the recovery rules
    and the migration path from the legacy format.
    """

    def __init__(
        self,
        path: str | None = None,
        debug: bool = False,
        *,
        fingerprinter: RequestFingerprinterProtocol | None = None,
        journal: bool = False,
    ) -> None:
        self.file = None
        self.fingerprinter: RequestFingerprinterProtocol = (
            fingerprinter or RequestFingerprinter()
        )
        self._fingerprints: set[bytes] = set()
        self._journal = journal
        self._journal_path: Path | None = None
        self._journal_dirty_from: int | None = None
        self.logdupes = True
        self.debug = debug
        self.logger = logging.getLogger(__name__)
        self._legacy_fingerprint = method_is_overridden(
            type(self), RFPDupeFilter, "request_fingerprint"
        )
        if self._legacy_fingerprint:
            warn(
                "Overriding RFPDupeFilter.request_fingerprint() is deprecated,"
                " set the REQUEST_FINGERPRINTER_CLASS setting instead.",
                ScrapyDeprecationWarning,
                stacklevel=2,
            )
        if path:
            if journal:
                self._open_journal(Path(path))
            else:
                self.file = Path(path, _LEGACY_FILENAME).open("a+b")
                self.file.seek(0)
                self._fingerprints.update(_read_fingerprints(self.file.read()))

    def _open_journal(self, jobdir: Path) -> None:
        journal_path = jobdir / _JOURNAL_FILENAME
        if not journal_path.exists():
            legacy_path = jobdir / _LEGACY_FILENAME
            if legacy_path.exists():
                raise JournalFormatError(
                    f"Found legacy {legacy_path} but journal mode is enabled "
                    f"and no {_JOURNAL_FILENAME} exists. Journal mode never "
                    f"converts the legacy format implicitly; run "
                    f"RFPDupeFilter.migrate_legacy_journal({str(jobdir)!r}) "
                    f"to convert it explicitly.",
                    path=legacy_path,
                )
            _init_journal_file(journal_path)
        else:
            data = journal_path.read_bytes()
            if data:
                fingerprints, committed_end = _read_journal(data, path=journal_path)
                self._fingerprints.update(fingerprints)
                if committed_end < len(data):
                    self._quarantine_tail(journal_path, data, committed_end)
            else:
                # Empty file: a previous run crashed before writing the
                # header. Initialize it as a fresh journal.
                _init_journal_file(journal_path)
        self._journal_path = journal_path
        self.file = journal_path.open("a+b")

    def _quarantine_tail(
        self, journal_path: Path, data: bytes, committed_end: int
    ) -> None:
        """Isolate the uncommitted or truncated tail of the journal.

        Append the bytes past the commit boundary to
        :file:`requests.seen.journal.orphan` and truncate the journal to the
        commit boundary, so that later appends start from a valid record
        boundary. Committed records are never touched.
        """
        orphan_path = journal_path.with_name(_ORPHAN_FILENAME)
        tail = data[committed_end:]
        with orphan_path.open("ab") as orphan:
            orphan.write(tail)
            orphan.flush()
            os.fsync(orphan.fileno())
        with journal_path.open("r+b") as journal:
            journal.truncate(committed_end)
            journal.flush()
            os.fsync(journal.fileno())
        self.logger.warning(
            "Isolated %d uncommitted or truncated bytes from %s into %s "
            "and truncated the journal to its commit boundary (%d bytes).",
            len(tail),
            journal_path,
            orphan_path,
            committed_end,
        )

    def _journal_append(self, fingerprint: bytes) -> None:
        """Append *fingerprint* to the journal and sync it to disk.

        Raise :class:`JournalWriteError` on any write, flush or fsync
        failure, before the fingerprint reaches the in-memory set. A failed
        append is rolled back to the commit boundary on a best-effort basis,
        so that the journal only ever contains valid records; if the rollback
        itself fails, the torn tail is cleaned before the next append or
        quarantined on the next open.
        """
        assert self._journal_path is not None
        record = _journal_record(fingerprint)
        start: int | None = None
        try:
            if self._journal_dirty_from is not None:
                # A previous append failed and its rollback did not complete;
                # remove the torn tail before appending new records.
                self.file.truncate(self._journal_dirty_from)
                self._journal_dirty_from = None
            self.file.seek(0, os.SEEK_END)
            start = self.file.tell()
            self.file.write(record)
            self.file.flush()
            os.fsync(self.file.fileno())
        except OSError as exc:
            if start is not None:
                self._journal_dirty_from = start
                self._rollback_append(start)
            raise JournalWriteError(
                f"Could not append a fingerprint to {self._journal_path}: "
                f"{exc}. The fingerprint was not added to the in-memory set, "
                f"so the request will be rescheduled."
            ) from exc

    def _rollback_append(self, start: int) -> None:
        """Best-effort truncation of a torn append back to the commit
        boundary at *start*. Only bytes written by the failed append are
        removed; committed records are never truncated."""
        try:
            self.file.truncate(start)
            self.file.flush()
            os.fsync(self.file.fileno())
        except OSError:
            self.logger.warning(
                "Could not roll back a torn append to %s at offset %d; the "
                "torn tail will be removed before the next append or "
                "quarantined on the next open.",
                self._journal_path,
                start,
            )
        else:
            self._journal_dirty_from = None

    @classmethod
    def migrate_legacy_journal(cls, path: str) -> Path:
        """Convert a legacy :file:`requests.seen` file in the job directory
        *path* into the recoverable journal format
        (:setting:`DUPEFILTER_JOURNAL`), and return the journal path.

        The legacy file is kept, renamed to
        :file:`requests.seen.legacy.bak`. Raise :class:`JournalFormatError`
        if there is no legacy file to convert, or if a journal or backup
        file already exists (nothing is overwritten).
        """
        jobdir = Path(path)
        legacy_path = jobdir / _LEGACY_FILENAME
        journal_path = jobdir / _JOURNAL_FILENAME
        backup_path = jobdir / _LEGACY_BACKUP_FILENAME
        if not legacy_path.exists():
            raise JournalFormatError(
                f"No legacy {_LEGACY_FILENAME} file found in {jobdir}.",
                path=legacy_path,
            )
        if journal_path.exists():
            raise JournalFormatError(
                f"{journal_path} already exists; refusing to overwrite it.",
                path=journal_path,
            )
        if backup_path.exists():
            raise JournalFormatError(
                f"{backup_path} already exists; refusing to overwrite the "
                f"previous backup.",
                path=backup_path,
            )
        fingerprints = list(_read_fingerprints(legacy_path.read_bytes()))
        tmp_path = jobdir / (_JOURNAL_FILENAME + ".tmp")
        with tmp_path.open("wb") as tmp:
            tmp.write(_JOURNAL_HEADER)
            for fingerprint in fingerprints:
                tmp.write(_journal_record(fingerprint))
            tmp.flush()
            os.fsync(tmp.fileno())
        tmp_path.replace(journal_path)
        legacy_path.rename(backup_path)
        return journal_path

    @property
    def fingerprints(self) -> frozenset[str]:
        warn(
            "RFPDupeFilter.fingerprints is deprecated.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return frozenset(fp.hex() for fp in self._fingerprints)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        debug = crawler.settings.getbool("DUPEFILTER_DEBUG")
        return cls(
            _job_dir(crawler.settings),
            debug,
            fingerprinter=crawler.request_fingerprinter,
            journal=crawler.settings.getbool("DUPEFILTER_JOURNAL"),
        )

    def request_seen(self, request: Request) -> bool:
        fp = self._fingerprint(request)
        if fp in self._fingerprints:
            return True
        if self.file is not None:
            if self._journal:
                # Durably append first: the fingerprint only enters the
                # in-memory set once it is committed to the journal, so on
                # failure both stay consistent and this request is simply
                # rescheduled later.
                self._journal_append(fp)
            else:
                self.file.write(len(fp).to_bytes(_SIZE_BYTES, "big") + fp)
        self._fingerprints.add(fp)
        return False

    def _fingerprint(self, request: Request) -> bytes:
        if self._legacy_fingerprint:
            return bytes.fromhex(self.request_fingerprint(request))
        return self.fingerprinter.fingerprint(request)

    def request_fingerprint(self, request: Request) -> str:
        """Returns a string that uniquely identifies the specified request."""
        return self.fingerprinter.fingerprint(request).hex()

    def close(self, reason: str) -> None:
        if self.file is None:
            # Idempotent: closing an already-closed filter is a no-op.
            return
        # If flushing, syncing or closing fails, self.file stays set so that
        # close() can be retried; the existing log is never truncated here.
        if self._journal:
            self.file.flush()
            os.fsync(self.file.fileno())
        self.file.close()
        self.file = None

    def log(self, request: Request, spider: Spider) -> None:
        if self.debug:
            msg = "Filtered duplicate request: %(request)s (referer: %(referer)s)"
            args = {"request": request, "referer": referer_str(request)}
            self.logger.debug(msg, args, extra={"spider": spider})
        elif self.logdupes:
            msg = (
                "Filtered duplicate request: %(request)s"
                " - no more duplicates will be shown"
                " (see DUPEFILTER_DEBUG to show all duplicates)"
            )
            self.logger.debug(msg, {"request": request}, extra={"spider": spider})
            self.logdupes = False

        spider.crawler.stats.inc_value("dupefilter/filtered")


class DiskDupeFilter(RFPDupeFilter):
    """Duplicate request filtering class (:setting:`DUPEFILTER_CLASS`) that
    keeps seen request fingerprints on disk, in an SQLite database, instead of
    in memory, trading some speed for a much lower memory usage on crawls that
    send a large number of requests.

    .. versionadded:: 2.20.0

    Job directory contents
    ======================

    .. warning:: The files that this class generates in the :ref:`job directory
        <job-dir>` are an implementation detail, and may change without a
        warning in a future version of Scrapy. Do not rely on the following
        information for anything other than debugging purposes.

    When using :setting:`JOBDIR`, seen fingerprints are tracked in an SQLite
    database named :file:`requests.seen.db` in the :ref:`job directory
    <job-dir>`.
    """

    def __init__(
        self,
        path: str | None = None,
        debug: bool = False,
        *,
        fingerprinter: RequestFingerprinterProtocol | None = None,
        journal: bool = False,
    ) -> None:
        super().__init__(debug=debug, fingerprinter=fingerprinter)
        if journal:
            self.logger.warning(
                "DUPEFILTER_JOURNAL has no effect on DiskDupeFilter, which "
                "already keeps fingerprints in a WAL-mode SQLite database."
            )
        if path:
            self._tempdir: str | None = None
        else:
            self._tempdir = path = mkdtemp()
        self._db = sqlite3.connect(Path(path, "requests.seen.db"))
        self._db.execute("PRAGMA journal_mode=WAL").close()
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS seen (fingerprint BLOB PRIMARY KEY)"
            " WITHOUT ROWID"
        )
        self._sync_every = 0
        self._changes = 0

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        dupefilter = super().from_crawler(crawler)
        dupefilter._sync_every = crawler.settings.getint("JOBDIR_SYNC_EVERY")
        return dupefilter

    def request_seen(self, request: Request) -> bool:
        fp = self._fingerprint(request)
        cursor = self._db.execute("INSERT OR IGNORE INTO seen VALUES (?)", (fp,))
        if not cursor.rowcount:
            return True
        if self._sync_every:
            self._changes += 1
            if self._changes >= self._sync_every:
                self._changes = 0
                self._db.commit()
        return False

    def close(self, reason: str) -> None:
        self._db.commit()
        self._db.close()
        if self._tempdir:
            rmtree(self._tempdir)
