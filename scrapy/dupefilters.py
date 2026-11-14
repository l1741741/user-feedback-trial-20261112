from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Any, Self
from warnings import warn

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.utils.conf import _job_dir
from scrapy.utils.deprecate import method_is_overridden
from scrapy.utils.python import global_object_name
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

_FINGERPRINT_CONFIG_FILE = "requests.fingerprint.json"


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


def _fingerprinter_config(
    fingerprinter: RequestFingerprinterProtocol,
) -> dict[str, Any]:
    """Return the configuration that fingerprints recorded with the given
    fingerprinter depend on.

    It always includes the fingerprinter class and, for fingerprinters that
    expose a ``_jobdir_state`` attribute, the settings it records. A custom
    fingerprinter without that attribute is identified by its class alone,
    which still detects switching to or from it.
    """
    config: dict[str, Any] = {
        "class": global_object_name(type(fingerprinter)),
    }
    state = getattr(fingerprinter, "_jobdir_state", None)
    if state is not None:
        config["settings"] = state
    return config


def _default_fingerprinter_config() -> dict[str, Any]:
    return _fingerprinter_config(RequestFingerprinter())


def _has_seen_fingerprints(db_path: Path) -> bool:
    """Return whether the dupefilter database at *db_path* has fingerprints."""
    if not db_path.exists():
        return False
    # Read-only, so that merely checking the job directory does not create
    # journal files next to the database.
    with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True) as db:
        try:
            return db.execute("SELECT 1 FROM seen LIMIT 1").fetchone() is not None
        except sqlite3.OperationalError:
            return False


def _check_fingerprinter_config(
    path: str,
    fingerprinter: RequestFingerprinterProtocol,
    *,
    seen: bool,
) -> None:
    """Refuse to reuse fingerprints recorded with a different configuration.

    The configuration is stored next to the seen fingerprints. Without seen
    fingerprints there is nothing to reuse, so the stored configuration is
    simply replaced. A job directory that has seen fingerprints but no stored
    configuration predates this information, so it is only resumable with the
    default configuration, which recorded those fingerprints.
    """
    current = _fingerprinter_config(fingerprinter)
    config_path = Path(path, _FINGERPRINT_CONFIG_FILE)
    if seen:
        if config_path.exists():
            try:
                stored = json.loads(config_path.read_text(encoding="utf-8"))
            except ValueError as exception:
                raise ValueError(
                    f"The job directory {path!r} contains an unreadable request"
                    f" fingerprinter configuration in {config_path.name}."
                ) from exception
            if stored != current:
                raise ValueError(
                    f"The job directory {path!r} was created with the request"
                    f" fingerprinter configuration {stored!r}, but the current"
                    f" configuration is {current!r}. Fingerprints recorded"
                    " under a different configuration cannot be reused. Resume"
                    " the job with the original configuration, or use a new"
                    " JOBDIR."
                )
        else:
            default_config = _default_fingerprinter_config()
            if current != default_config:
                raise ValueError(
                    f"The job directory {path!r} contains fingerprints recorded"
                    " under the default request fingerprinter configuration"
                    f" {default_config!r}, but the current configuration is"
                    f" {current!r}. Fingerprints recorded under a different"
                    " configuration cannot be reused. Resume the job with the"
                    " original configuration, or use a new JOBDIR."
                )
    config_path.write_text(json.dumps(current), encoding="utf-8")


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
    ) -> None:
        self.file = None
        self.fingerprinter: RequestFingerprinterProtocol = (
            fingerprinter or RequestFingerprinter()
        )
        self._fingerprints: set[bytes] = set()
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
            seen_file = Path(path, "requests.seen")
            _check_fingerprinter_config(
                path,
                self.fingerprinter,
                seen=seen_file.exists() and seen_file.stat().st_size > 0,
            )
            self.file = seen_file.open("a+b")
            self.file.seek(0)
            self._fingerprints.update(_read_fingerprints(self.file.read()))

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
        )

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
    ) -> None:
        super().__init__(debug=debug, fingerprinter=fingerprinter)
        if path:
            self._tempdir: str | None = None
            _check_fingerprinter_config(
                path,
                self.fingerprinter,
                seen=_has_seen_fingerprints(Path(path, "requests.seen.db")),
            )
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
