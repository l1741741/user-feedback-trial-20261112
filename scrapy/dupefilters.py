from __future__ import annotations

import logging
import sqlite3
from io import BytesIO
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Any, Self
from warnings import warn

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
    from typing import BinaryIO

    from twisted.internet.defer import Deferred

    from scrapy.crawler import Crawler
    from scrapy.http.request import Request
    from scrapy.spiders import Spider


_SIZE_BYTES = 2
_FINGERPRINT_ID_FILENAME = "requests.seen.id"
_REQUESTS_SEEN_MIGRATED = "requests.seen.migrated"


class _RequestsSeenCorruptionError(ValueError):
    """The ``requests.seen`` file contains a complete zero-length record."""


class _RequestsSeenReader:
    """Reads fingerprints from a ``requests.seen`` file.

    Iteration stops at a truncated final record, which an unclean shutdown can
    leave behind, and exposes the offset up to which the file is valid. A
    complete zero-length record is corruption and raises instead of being
    skipped, since skipping it would misalign every record that follows.
    """

    def __init__(self, file: BinaryIO, path: Path | None = None) -> None:
        self._file = file
        self._path = path
        self.valid_end = 0

    def __iter__(self) -> Iterator[bytes]:
        self._file.seek(0)
        while True:
            offset = self._file.tell()
            header = self._file.read(_SIZE_BYTES)
            if len(header) < _SIZE_BYTES:
                return
            size = int.from_bytes(header, "big")
            if not size:
                raise _RequestsSeenCorruptionError(
                    f"{self._path or 'requests.seen'} is corrupt:"
                    f" zero-length fingerprint record at offset {offset}"
                )
            fingerprint = self._file.read(size)
            if len(fingerprint) < size:
                return
            self.valid_end = self._file.tell()
            yield fingerprint


def _read_fingerprints(data: bytes) -> Iterator[bytes]:
    """Yield the complete fingerprints stored in *data*."""
    yield from _RequestsSeenReader(BytesIO(data))


def _read_fingerprint_id(path: Path) -> str | None:
    try:
        value = Path(path, _FINGERPRINT_ID_FILENAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return value.strip() or None


def _write_fingerprint_id(path: Path, fingerprint_id: str) -> None:
    id_path = Path(path, _FINGERPRINT_ID_FILENAME)
    # Write to a temporary file first so that a crash cannot leave a partially
    # written ID behind, which would otherwise be taken as the recorded one.
    tmp_path = id_path.with_name(f"{id_path.name}.tmp")
    tmp_path.write_text(fingerprint_id, encoding="utf-8")
    tmp_path.replace(id_path)


def _check_fingerprint_id(path: Path, fingerprint_id: str | None) -> str | None:
    recorded = _read_fingerprint_id(path)
    if recorded is None:
        if fingerprint_id is not None:
            _write_fingerprint_id(path, fingerprint_id)
        return fingerprint_id
    if fingerprint_id is not None and fingerprint_id != recorded:
        raise ValueError(
            "JOBDIR_FINGERPRINTER_ID mismatch:"
            f" the job directory {str(path)!r} recorded fingerprint ID"
            f" {recorded!r}, which does not match {fingerprint_id!r}"
        )
    return recorded


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
    """

    def __init__(
        self,
        path: str | None = None,
        debug: bool = False,
        *,
        fingerprinter: RequestFingerprinterProtocol | None = None,
        fingerprint_id: str | None = None,
    ) -> None:
        self.file = None
        self.fingerprinter: RequestFingerprinterProtocol = (
            fingerprinter or RequestFingerprinter()
        )
        self._fingerprints: set[bytes] = set()
        self._fingerprint_id = fingerprint_id
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
            self._fingerprint_id = _check_fingerprint_id(Path(path), fingerprint_id)
            seen_path = Path(path, "requests.seen")
            self.file = seen_path.open("a+b")
            try:
                reader = _RequestsSeenReader(self.file, seen_path)
                self._fingerprints.update(reader)
                if reader.valid_end != self.file.tell():
                    # Drop a record left incomplete by an unclean shutdown so
                    # that the next record is written at a valid offset.
                    self.file.seek(reader.valid_end)
                    self.file.truncate()
            except BaseException:
                self.file.close()
                self.file = None
                raise

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
        kwargs: dict[str, Any] = {
            "fingerprinter": crawler.request_fingerprinter,
        }
        fingerprint_id = crawler.settings.get("JOBDIR_FINGERPRINTER_ID")
        if fingerprint_id is not None:
            kwargs["fingerprint_id"] = str(fingerprint_id)
        return cls(_job_dir(crawler.settings), debug, **kwargs)

    def request_seen(self, request: Request) -> bool:
        fp = self._fingerprint(request)
        if fp in self._fingerprints:
            return True
        self._fingerprints.add(fp)
        if self.file:
            self.file.write(len(fp).to_bytes(_SIZE_BYTES, "big") + fp)
        return False

    def _fingerprint(self, request: Request) -> bytes:
        if self._legacy_fingerprint:
            return bytes.fromhex(self.request_fingerprint(request))
        return self.fingerprinter.fingerprint(request)

    def request_fingerprint(self, request: Request) -> str:
        """Returns a string that uniquely identifies the specified request."""
        return self.fingerprinter.fingerprint(request).hex()

    def close(self, reason: str) -> None:
        if self.file:
            self.file.close()

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
        fingerprint_id: str | None = None,
    ) -> None:
        super().__init__(
            debug=debug,
            fingerprinter=fingerprinter,
            fingerprint_id=fingerprint_id,
        )
        if path:
            self._tempdir: str | None = None
            self._fingerprint_id = _check_fingerprint_id(Path(path), fingerprint_id)
            seen_path: Path | None = Path(path, "requests.seen")
            db_path = Path(path, "requests.seen.db")
        else:
            self._tempdir = path = mkdtemp()
            seen_path = None
            db_path = Path(path, "requests.seen.db")
        self._db = sqlite3.connect(db_path)
        try:
            self._db.execute("PRAGMA journal_mode=WAL").close()
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS seen (fingerprint BLOB PRIMARY KEY)"
                " WITHOUT ROWID"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,"
                " value TEXT NOT NULL) WITHOUT ROWID"
            )
            self._db.commit()
            if seen_path is not None:
                self._migrate_requests_seen(seen_path)
        except BaseException:
            self._db.close()
            raise
        self._sync_every = 0
        self._changes = 0

    def _migrate_requests_seen(self, seen_path: Path) -> None:
        """Import the binary ``requests.seen`` file into SQLite, once.

        Records are streamed and inserted in a single transaction together
        with the completion marker, so an interrupted migration leaves neither
        imported rows nor a marker behind and can be retried.
        """
        if not seen_path.exists() or self._requests_seen_migrated():
            return
        with seen_path.open("rb") as file:
            reader = _RequestsSeenReader(file, seen_path)
            with self._db:
                for fingerprint in reader:
                    self._db.execute(
                        "INSERT OR IGNORE INTO seen (fingerprint) VALUES (?)",
                        (fingerprint,),
                    )
                self._db.execute(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)",
                    (_REQUESTS_SEEN_MIGRATED, "1"),
                )

    def _requests_seen_migrated(self) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM metadata WHERE key = ?", (_REQUESTS_SEEN_MIGRATED,)
        ).fetchone()
        return row is not None

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
