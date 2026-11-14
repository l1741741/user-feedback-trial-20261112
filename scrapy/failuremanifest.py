"""Optional crawl failure manifest.

When the :setting:`CRAWL_FAILURE_MANIFEST` setting is enabled, terminal crawl
failures (download errors, spider callback errors and items rejected by item
pipelines) are recorded into a local JSONL manifest so that operators can
hand the missed URLs to the next crawl batch instead of grepping text logs.

The manifest is written by :class:`FailureManifestRecorder`, which is driven
from :class:`scrapy.logformatter.LogFormatter` methods, i.e. it only sees
*terminal* events: intermediate retries handled by the retry middleware never
reach the log formatter, so they never appear in the manifest. Within one
crawl batch, repeated terminal failures of the same request are merged by
request fingerprint (see :func:`scrapy.utils.request.fingerprint`).

The module also provides :func:`generate_replay_plan`, a read-only helper
that turns a manifest into a next-batch URL plan. It never modifies the
manifest and never schedules or sends any requests itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from twisted.python.failure import Failure

from scrapy import signals
from scrapy.http import Request, Response
from scrapy.utils.request import fingerprint

if TYPE_CHECKING:
    from scrapy.crawler import Crawler
    from scrapy.spiders import Spider

logger = logging.getLogger(__name__)

SETTING_NAME = "CRAWL_FAILURE_MANIFEST"
SCHEMA_VERSION = 1

STAGE_DOWNLOAD = "download"
STAGE_PARSE = "parse"
STAGE_ITEM_DROPPED = "item_dropped"

#: Stages that mean the URL content was not (fully) turned into items and
#: therefore belongs into a next-batch URL plan.
URL_MISS_STAGES = (STAGE_DOWNLOAD, STAGE_PARSE)

DEFAULT_MANIFEST_PATH = "crawl_failure_manifest.jsonl"


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _default_batch_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{os.urandom(4).hex()}"


def _parse_config(config: Any) -> tuple[Path, str]:
    """Turn a ``CRAWL_FAILURE_MANIFEST`` value into ``(path, batch_id)``."""
    if isinstance(config, (str, os.PathLike)):
        return Path(config), _default_batch_id()
    if isinstance(config, dict):
        path = config.get("path")
        if not path:
            raise ValueError(f"{SETTING_NAME} requires a non-empty 'path'")
        return Path(path), str(config.get("batch_id") or _default_batch_id())
    if config is True:
        return Path(DEFAULT_MANIFEST_PATH), _default_batch_id()
    raise ValueError(f"unsupported {SETTING_NAME} value: {config!r}")


class _JsonlWriter:
    """Append-only JSONL writer.

    *   A lock serializes concurrent writes so lines never interleave.
    *   Every line is flushed immediately; :meth:`close` flushes again.
    *   On open, a truncated tail line (e.g. from a crashed run) is preserved
        into a ``<path>.partial-tail`` sidecar and removed from the manifest
        so that appends resume on a clean line boundary.
    *   Write failures are logged and counted, never raised: a broken
        manifest must not interrupt the crawl.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lines_written = 0
        self.failed_writes = 0
        self._lock = threading.Lock()
        self._closed = False
        self._recover_partial_tail()
        self._file = self.path.open("a", encoding="utf-8", newline="\n")

    def _recover_partial_tail(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return  # does not exist yet
        if size == 0:
            return
        data = self.path.read_bytes()
        if data.endswith(b"\n"):
            return
        last_newline = data.rfind(b"\n")
        tail = data[last_newline + 1 :]
        backup = self.path.with_name(self.path.name + ".partial-tail")
        with backup.open("ab") as backup_file:
            backup_file.write(tail + b"\n")
        with self.path.open("r+b") as manifest_file:
            manifest_file.truncate(last_newline + 1)
        logger.warning(
            "Crawl failure manifest %s ended with a partial line; preserved "
            "%d bytes into %s and resumed appending after the last complete "
            "line.",
            self.path,
            len(tail),
            backup,
        )

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            if self._closed:
                self.failed_writes += 1
                logger.warning(
                    "Crawl failure manifest %s is already closed; dropping record.",
                    self.path,
                )
                return
            try:
                self._file.write(line)
                self._file.flush()
                self.lines_written += 1
            except OSError as exc:
                self.failed_writes += 1
                logger.warning(
                    "Crawl failure manifest write to %s failed (%s); "
                    "crawling continues.",
                    self.path,
                    exc,
                )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._file.flush()
                self._file.close()
            except OSError as exc:
                logger.warning(
                    "Crawl failure manifest %s failed to close cleanly: %s",
                    self.path,
                    exc,
                )


class FailureManifestRecorder:
    """Records terminal crawl failures into a JSONL manifest.

    Instances are created per crawl batch through :meth:`from_crawler` (which
    returns ``None`` when :setting:`CRAWL_FAILURE_MANIFEST` is disabled) and
    are driven from :class:`scrapy.logformatter.LogFormatter` methods. All
    ``record_*`` methods are best-effort: they never raise, so recording can
    never break a crawl.
    """

    def __init__(self, path: Path, batch_id: str):
        self.batch_id = batch_id
        self._writer = _JsonlWriter(path)
        self._entries: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._writer.write(
            {
                "schema": SCHEMA_VERSION,
                "record_type": "batch_start",
                "batch_id": self.batch_id,
                "ts": _utcnow_iso(),
            }
        )

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> FailureManifestRecorder | None:
        config = crawler.settings.get(SETTING_NAME)
        if not config:
            return None
        try:
            path, batch_id = _parse_config(config)
            path.parent.mkdir(parents=True, exist_ok=True)
            recorder = cls(path, batch_id)
        except Exception as exc:
            logger.warning(
                "%s is set but the manifest cannot be initialized (%s); "
                "failure recording is disabled for this crawl.",
                SETTING_NAME,
                exc,
            )
            return None
        crawler.signals.connect(recorder.close, signal=signals.spider_closed)
        logger.info("Recording crawl failures to %s (batch %s)", path, batch_id)
        return recorder

    # -- recording API (called from LogFormatter methods) --

    def record_download_error(
        self, failure: Failure, request: Request, spider: Spider
    ) -> None:
        """Record a terminal download failure of *request*."""
        try:
            exc = failure.value if isinstance(failure, Failure) else failure
            self._record_request_failure(
                STAGE_DOWNLOAD, exc, request, spider, response=None
            )
        except Exception as exc:
            logger.warning("Failed to record download error in manifest: %s", exc)

    def record_spider_error(
        self,
        failure: Failure,
        request: Request,
        response: Response | Failure | None,
        spider: Spider,
    ) -> None:
        """Record an exception raised by a spider callback or errback."""
        try:
            exc = failure.value if isinstance(failure, Failure) else failure
            # If the callback input itself was a download Failure, the URL
            # content never reached the spider: that is a download-stage miss.
            stage = STAGE_PARSE if isinstance(response, Response) else STAGE_DOWNLOAD
            self._record_request_failure(stage, exc, request, spider, response)
        except Exception as exc:
            logger.warning("Failed to record spider error in manifest: %s", exc)

    def record_item_dropped(
        self,
        item: Any,
        exception: BaseException,
        response: Response | Failure | None,
        spider: Spider,
    ) -> None:
        """Record an item rejected by an item pipeline.

        A dropped item is *not* a URL miss: the page was fetched and parsed.
        It is recorded under its own stage so replay plans can exclude it.
        """
        try:
            url_fetched = isinstance(response, Response)
            request = response.request if url_fetched else None
            if request is not None:
                fp = fingerprint(request).hex()
                url, method = request.url, request.method
                retry_times = int(request.meta.get("retry_times", 0))
            else:
                fp = hashlib.sha1(  # noqa: S324
                    f"item-dropped|{item.__class__.__module__}."
                    f"{item.__class__.__qualname__}|"
                    f"{exception.__class__.__name__}|{exception}".encode()
                ).hexdigest()
                url, method, retry_times = None, None, 0
            if url_fetched:
                reason = (
                    "item was rejected by an item pipeline after the URL was "
                    "fetched and parsed; it is not a URL crawl miss"
                )
            else:
                reason = (
                    "item was rejected by an item pipeline; the originating "
                    "response is unavailable, so this is not a URL crawl miss"
                )
            record: dict[str, Any] = {
                "schema": SCHEMA_VERSION,
                "record_type": "failure",
                "batch_id": self.batch_id,
                "ts": _utcnow_iso(),
                "spider": getattr(spider, "name", None),
                "stage": STAGE_ITEM_DROPPED,
                "fingerprint": fp,
                "url": url,
                "method": method,
                "callback": None,
                "cb_kwargs": None,
                "replayable": False,
                "non_replayable_reason": reason,
                "retry_times": retry_times,
                "error_class": exception.__class__.__name__,
                "http_status": response.status if url_fetched else None,
                "url_fetched": url_fetched,
                "item_class": f"{item.__class__.__module__}.{item.__class__.__qualname__}",
                "drop_reason": str(exception),
                "occurrences": 1,
            }
            self._merge_and_write(STAGE_ITEM_DROPPED, fp, record)
        except Exception as exc:
            logger.warning("Failed to record dropped item in manifest: %s", exc)

    def close(self, spider: Spider | None = None) -> None:
        """Flush and close the manifest. Connected to ``spider_closed``."""
        self._writer.close()

    # -- internals --

    def _record_request_failure(
        self,
        stage: str,
        exc: BaseException,
        request: Request,
        spider: Spider,
        response: Response | Failure | None,
    ) -> None:
        fp = fingerprint(request).hex()
        callback, cb_kwargs, problems = self._request_context(request)
        http_status = None
        if isinstance(response, Response):
            http_status = response.status
        else:
            exc_response = getattr(exc, "response", None)
            if isinstance(exc_response, Response):
                http_status = exc_response.status
        record: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "record_type": "failure",
            "batch_id": self.batch_id,
            "ts": _utcnow_iso(),
            "spider": getattr(spider, "name", None),
            "stage": stage,
            "fingerprint": fp,
            "url": request.url,
            "method": request.method,
            "callback": callback,
            "cb_kwargs": cb_kwargs,
            "replayable": not problems,
            "non_replayable_reason": "; ".join(problems) or None,
            "retry_times": int(request.meta.get("retry_times", 0)),
            "error_class": exc.__class__.__name__ if exc is not None else None,
            "http_status": http_status,
            "url_fetched": stage == STAGE_PARSE,
            "occurrences": 1,
        }
        self._merge_and_write(stage, fp, record)

    @staticmethod
    def _request_context(
        request: Request,
    ) -> tuple[str | None, dict[str, Any] | None, list[str]]:
        """Extract callback name and JSON-safe cb_kwargs from *request*.

        Context that cannot be faithfully restored is reported through the
        returned problem list; the corresponding fields are set to ``None``
        instead of being faked with ``repr()``.
        """
        problems: list[str] = []
        callback_name: str | None = None
        if request.callback is not None:
            callback_name = getattr(request.callback, "__name__", None)
            if not callback_name or callback_name == "<lambda>":
                problems.append(
                    "request callback cannot be referenced by a spider method name"
                )
                callback_name = None
        cb_kwargs: dict[str, Any] | None = dict(request.cb_kwargs)
        try:
            json.dumps(cb_kwargs)
        except (TypeError, ValueError) as exc:
            problems.append(f"cb_kwargs are not JSON-serializable: {exc}")
            cb_kwargs = None
        return callback_name, cb_kwargs, problems

    def _merge_and_write(self, stage: str, fp: str, record: dict[str, Any]) -> None:
        """Merge *record* into the entry for ``(stage, fp)`` and append it.

        Repeated terminal failures of the same request within this batch
        update a single logical entry (bumping ``occurrences`` and keeping
        the final ``retry_times``); the appended line supersedes earlier
        lines for the same key when the manifest is read back.
        """
        with self._lock:
            key = (stage, fp)
            existing = self._entries.get(key)
            if existing is not None:
                record["occurrences"] = existing["occurrences"] + 1
                record["retry_times"] = max(
                    existing["retry_times"], record["retry_times"]
                )
            self._entries[key] = record
            self._writer.write(record)


def read_manifest(
    path: str | os.PathLike[str],
) -> tuple[list[dict[str, Any]], list[int], list[str]]:
    """Read a manifest, merging duplicate entries.

    Returns ``(records, corrupt_line_numbers, batch_ids)``. Corrupt lines
    (e.g. a torn write in the middle of the file) are reported by line
    number and skipped; the file itself is left untouched. For each
    ``(batch_id, stage, fingerprint)`` key the last complete line wins.
    """
    records: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    order: list[tuple[Any, Any, Any]] = []
    corrupt: list[int] = []
    batches: list[str] = []
    with Path(path).open(encoding="utf-8") as manifest_file:
        for lineno, raw_line in enumerate(manifest_file, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                corrupt.append(lineno)
                logger.warning(
                    "Crawl failure manifest %s: skipping corrupt line %d "
                    "(original file left unchanged).",
                    path,
                    lineno,
                )
                continue
            batch_id = record.get("batch_id")
            if batch_id is not None and batch_id not in batches:
                batches.append(batch_id)
            if record.get("record_type") != "failure":
                continue
            key = (batch_id, record.get("stage"), record.get("fingerprint"))
            if key not in records:
                order.append(key)
            records[key] = record
    return [records[key] for key in order], corrupt, batches


def generate_replay_plan(
    manifest_path: str | os.PathLike[str],
    *,
    batch_id: str | None = None,
    output_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Build a read-only next-batch URL plan from a failure manifest.

    The manifest is only read, never modified. No requests are scheduled.
    Only URL-level misses (download and parse failures) become plan entries;
    dropped items are counted but excluded, since their URLs were fetched.
    Non-replayable entries are listed separately with their reasons.

    *batch_id* defaults to the most recent batch found in the manifest.
    When *output_path* is given, the plan is also written there as JSON.
    """
    records, corrupt, batches = read_manifest(manifest_path)
    if batch_id is None:
        batch_id = batches[-1] if batches else None
    batch_records = [r for r in records if r.get("batch_id") == batch_id]

    misses = [r for r in batch_records if r.get("stage") in URL_MISS_STAGES]
    dropped = [r for r in batch_records if r.get("stage") == STAGE_ITEM_DROPPED]
    replayable = [r for r in misses if r.get("replayable")]
    non_replayable = [r for r in misses if not r.get("replayable")]

    plan: dict[str, Any] = {
        "plan_type": "crawl-failure-replay",
        "schema": SCHEMA_VERSION,
        "source_manifest": str(manifest_path),
        "batch_id": batch_id,
        "known_batches": batches,
        "request_count": len(replayable),
        "requests": [
            {
                "url": r["url"],
                "method": r["method"],
                "callback": r["callback"],
                "cb_kwargs": r["cb_kwargs"],
                "fingerprint": r["fingerprint"],
                "stage": r["stage"],
                "retry_times": r["retry_times"],
                "error_class": r["error_class"],
                "http_status": r["http_status"],
                "occurrences": r["occurrences"],
            }
            for r in replayable
        ],
        "non_replayable": [
            {
                "url": r["url"],
                "stage": r["stage"],
                "fingerprint": r["fingerprint"],
                "reason": r["non_replayable_reason"],
            }
            for r in non_replayable
        ],
        "counts": {
            "manifest_records": len(batch_records),
            "download_failures": sum(1 for r in misses if r["stage"] == STAGE_DOWNLOAD),
            "parse_failures": sum(1 for r in misses if r["stage"] == STAGE_PARSE),
            "url_misses": len(misses),
            "replayable_urls": len(replayable),
            "non_replayable_urls": len(non_replayable),
            "items_dropped_excluded": len(dropped),
            "corrupt_lines": len(corrupt),
            "corrupt_line_numbers": corrupt,
        },
    }
    if output_path is not None:
        with Path(output_path).open("w", encoding="utf-8") as plan_file:
            json.dump(plan, plan_file, ensure_ascii=False, indent=2, sort_keys=True)
            plan_file.write("\n")
    return plan
