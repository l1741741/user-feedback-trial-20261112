from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import warnings
from abc import ABC, abstractmethod
from collections import OrderedDict, defaultdict
from traceback import clear_frames
from typing import TYPE_CHECKING, Any, Literal, Self, TypeAlias, TypedDict, cast
from urllib.parse import urlsplit

from itemadapter import ItemAdapter
from twisted.internet.defer import Deferred, DeferredList
from twisted.python.failure import Failure

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http.request import NO_CALLBACK, Request
from scrapy.utils._datatypes import SequenceExclude
from scrapy.utils._deps_compat import TWISTED_FAILURE_HAS_STACK
from scrapy.utils.asyncio import is_asyncio_available
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.defer import (
    _process_pending_io,
    deferred_from_coro,
    ensure_awaitable,
    maybe_deferred_to_future,
)
from scrapy.utils.log import failure_to_exc_info
from scrapy.utils.misc import arg_to_iter
from scrapy.utils.python import global_object_name

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from scrapy import Spider
    from scrapy.crawler import Crawler
    from scrapy.http import Response
    from scrapy.settings import Settings
    from scrapy.utils.request import RequestFingerprinterProtocol


class FileInfo(TypedDict):
    url: str
    path: str
    checksum: str | None
    status: str


FileInfoOrError: TypeAlias = (
    tuple[Literal[True], FileInfo] | tuple[Literal[False], Failure]
)

logger = logging.getLogger(__name__)


class FileException(Exception):
    """General media error exception"""


class _MediaRequestFiltered(FileException):
    """Raised internally by media pipelines when a media request is filtered
    out (e.g. as an offsite request) instead of being downloaded.

    It is a subclass of :exc:`FileException` for backward compatibility, but
    unlike an actual download error it is logged at the ``DEBUG`` level and
    without a traceback, since filtering a request is expected behavior rather
    than an error.
    """


def _media_request_filtered(failure: Failure) -> bool:
    return isinstance(failure.value, _MediaRequestFiltered)


#: Request meta key used to pass the HTTP status of a failed media response
#: to the safe diagnostics machinery, without keeping the response object.
_SAFE_DIAGNOSTICS_STATUS_META_KEY: str = "_media_safe_diagnostics_http_status"

_SAFE_DIAGNOSTICS_EVENT: str = "media_download_failed"

#: URL default ports that are omitted from normalized origins.
_SAFE_URL_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443, "ftp": 21}

#: Maximum nesting depth accepted in item identifier values.
_SAFE_IDENTIFIER_MAX_DEPTH: int = 8

_MISSING: Any = object()


def _safe_url_origin_and_path(url: str) -> tuple[str | None, str | None]:
    """Return ``(origin, path)`` for *url*, stripped of userinfo, query and
    fragment, so that signed URLs and embedded credentials are not logged.

    The origin is normalized (lowercase scheme and host, default port
    dropped). If the URL has no scheme or host, or cannot be parsed, the
    affected parts are ``None``; the raw URL is never returned.
    """
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        origin: str | None = None
        if scheme and hostname:
            try:
                port = parts.port
            except ValueError:
                port = None
            origin = f"{scheme}://{hostname}"
            if port is not None and _SAFE_URL_DEFAULT_PORTS.get(scheme) != port:
                origin = f"{origin}:{port}"
        path: str | None = parts.path or ("/" if origin else None)
        return origin, path
    except Exception:
        return None, None


def _sanitize_identifier_value(value: Any, depth: int = 0) -> Any:
    """Return a JSON-encodable, deterministic representation of *value*.

    Only built-in JSON-native types are kept as is. ``bytes`` become
    ``{"type": "bytes", "length": N}`` and anything else becomes a
    ``"<unencodable:TypeName>"`` placeholder. Custom serialization hooks of
    arbitrary objects (``__json__``, ``__repr__``, ...) are never called.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "<unencodable:float>"
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value)}
    if depth >= _SAFE_IDENTIFIER_MAX_DEPTH:
        return f"<unencodable:{type(value).__name__}>"
    if isinstance(value, (list, tuple)):
        return [_sanitize_identifier_value(v, depth + 1) for v in value]
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            return f"<unencodable:{type(value).__name__}>"
        return {k: _sanitize_identifier_value(v, depth + 1) for k, v in value.items()}
    return f"<unencodable:{type(value).__name__}>"


def _exception_http_status(exc: BaseException) -> int | None:
    """Return the HTTP status carried by *exc* (e.g. through a ``response``
    attribute), or ``None``. The response object itself is never kept."""
    try:
        status = getattr(getattr(exc, "response", None), "status", None)
    except Exception:
        return None
    return status if isinstance(status, int) else None


class MediaPipeline(ABC):
    LOG_FAILED_RESULTS: bool = True

    class SpiderInfo:
        def __init__(self, spider: Spider):
            self.spider: Spider = spider
            self.downloading: set[bytes] = set()
            self.downloaded: OrderedDict[bytes, FileInfo | Failure] = OrderedDict()
            self.waiting: defaultdict[bytes, list[Deferred[FileInfo]]] = defaultdict(
                list
            )
            self.safe_diagnostics_contexts: OrderedDict[bytes, dict[str, Any]] = (
                OrderedDict()
            )

    def __init__(
        self,
        download_func: None = None,
        *,
        crawler: Crawler,
    ):
        if download_func is not None:  # pragma: no cover
            warnings.warn(
                "The download_func argument of MediaPipeline.__init__() is ignored"
                " and will be removed in a future Scrapy version.",
                category=ScrapyDeprecationWarning,
                stacklevel=2,
            )
        self.crawler: Crawler = crawler
        self._fingerprinter: RequestFingerprinterProtocol = (
            crawler.request_fingerprinter
        )

        settings = crawler.settings
        resolve = functools.partial(
            self._key_for_pipe, base_class_name="MediaPipeline", settings=settings
        )
        self._redirect_http_codes: set[int] = {
            int(x) for x in settings.getlist("REDIRECT_HTTP_CODES")
        }
        self.allow_redirects: bool = settings.getbool(
            resolve("MEDIA_ALLOW_REDIRECTS"), False
        )
        self._handle_statuses(self.allow_redirects)
        self._cache_size: int = settings.getint(resolve("MEDIA_CACHE_SIZE"))
        self._safe_diagnostics_enabled: bool = settings.getbool(
            resolve("MEDIA_SAFE_DIAGNOSTICS"), False
        )
        self._safe_diagnostics_item_fields: tuple[str, ...] = tuple(
            settings.getlist(resolve("MEDIA_SAFE_DIAGNOSTICS_ITEM_FIELDS"))
        )

    def _handle_statuses(self, allow_redirects: bool) -> None:
        self.handle_httpstatus_list = None
        if allow_redirects:
            # Statuses that RedirectMiddleware may redirect, i.e. those from
            # REDIRECT_HTTP_CODES, are left for it to handle.
            self.handle_httpstatus_list = SequenceExclude(
                {*range(300, 400), *self._redirect_http_codes}
            )

    def _key_for_pipe(
        self,
        key: str,
        base_class_name: str | None = None,
        settings: Settings | None = None,
    ) -> str:
        class_name = self.__class__.__name__
        formatted_key = f"{class_name.upper()}_{key}"
        if (
            not base_class_name
            or class_name == base_class_name
            or (settings and not settings.get(formatted_key))
        ):
            return key
        return formatted_key

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler=crawler)

    @_warn_spider_arg
    def open_spider(self, spider: Spider | None = None) -> None:
        assert self.crawler.spider
        self.spiderinfo = self.SpiderInfo(self.crawler.spider)

    @_warn_spider_arg
    async def process_item(self, item: Any, spider: Spider | None = None) -> Any:
        info = self.spiderinfo
        requests = arg_to_iter(self.get_media_requests(item, info))
        coros = [self._process_request(r, info, item) for r in requests]
        results: list[FileInfoOrError] = []
        if coros:
            if is_asyncio_available():
                results_asyncio = await asyncio.gather(*coros, return_exceptions=True)
                for res in results_asyncio:
                    if isinstance(res, BaseException):
                        results.append((False, Failure(res)))
                    else:
                        results.append((True, res))
            else:
                results = await cast(
                    "Deferred[list[FileInfoOrError]]",
                    DeferredList(
                        (deferred_from_coro(coro) for coro in coros), consumeErrors=True
                    ),
                )
        return await ensure_awaitable(self.item_completed(results, item, info))

    async def _process_request(
        self, request: Request, info: SpiderInfo, item: Any
    ) -> FileInfo:
        fp = self._fingerprinter.fingerprint(request)

        eb: Callable[[Failure], FileInfo] | None = request.errback
        request.callback = NO_CALLBACK
        request.errback = None

        # Return cached result if request was already seen
        if fp in info.downloaded:
            await _process_pending_io()
            info.downloaded.move_to_end(fp)
            cached_result = info.downloaded[fp]
            if isinstance(cached_result, Failure):
                if self._safe_diagnostics_enabled and not _media_request_filtered(
                    cached_result
                ):
                    self._emit_safe_diagnostics(request, item, info, cached_result)
                if eb:
                    return eb(cached_result)
                cached_result.raiseException()
            return cached_result

        # Otherwise, wait for result
        wad: Deferred[FileInfo] = Deferred()
        if self._safe_diagnostics_enabled:
            wad.addErrback(self._safe_diagnostics_errback, request, item, info)
        if eb:
            wad.addErrback(eb)
        info.waiting[fp].append(wad)

        # Check if request is downloading right now to avoid doing it twice
        if fp in info.downloading:
            return await maybe_deferred_to_future(wad)

        # Download request checking media_to_download hook output first
        info.downloading.add(fp)
        await _process_pending_io()
        result: FileInfo | Failure
        stage = "media_to_download"
        try:
            file_info: FileInfo | None = await ensure_awaitable(
                self.media_to_download(request, info, item=item)
            )
            if file_info:
                # got a result without downloading
                result = file_info
            else:
                # download the result
                stage = "download"
                result = await self._check_media_to_download(request, info, item=item)
        except Exception:
            result = Failure()
            if not _media_request_filtered(result):
                if self._safe_diagnostics_enabled:
                    info.safe_diagnostics_contexts[fp] = (
                        self._build_safe_diagnostics_context(request, result, stage)
                    )
                logger.exception(result)
        await _process_pending_io()
        self._cache_result_and_execute_waiters(result, fp, info)
        return await maybe_deferred_to_future(wad)  # it must return wad at last

    def _modify_media_request(self, request: Request) -> None:
        if self.handle_httpstatus_list:
            request.meta["handle_httpstatus_list"] = self.handle_httpstatus_list
        else:
            request.meta["handle_httpstatus_all"] = True

    async def _check_media_to_download(
        self, request: Request, info: SpiderInfo, item: Any
    ) -> FileInfo:
        try:
            self._modify_media_request(request)
            response = await self.crawler.engine.download_async(request)
            return await ensure_awaitable(
                self.media_downloaded(response, request, info, item=item)
            )
        except Exception:
            failure = self.media_failed(Failure(), request, info)
            if isinstance(failure, Failure):
                warnings.warn(
                    f"{global_object_name(self.media_failed)} returned a Failure instance."
                    f" This is deprecated, please raise an exception instead, e.g. via failure.raiseException().",
                    category=ScrapyDeprecationWarning,
                    stacklevel=2,
                )
                failure.raiseException()

    def _cache_result_and_execute_waiters(
        self, result: FileInfo | Failure, fp: bytes, info: SpiderInfo
    ) -> None:
        if isinstance(result, Failure):
            # minimize cached information for failure
            exception: BaseException | None = result.value
            while exception is not None:
                clear_frames(exception.__traceback__)
                exception = exception.__context__
            result.cleanFailure()
            result.frames.clear()
            if TWISTED_FAILURE_HAS_STACK:
                result.stack.clear()

        info.downloading.remove(fp)
        info.downloaded[fp] = result  # cache result
        evicted_fp: bytes | None = None
        if 0 <= self._cache_size < len(info.downloaded):
            evicted_fp, _ = info.downloaded.popitem(last=False)
        for wad in info.waiting.pop(fp):
            if isinstance(result, Failure):
                wad.errback(result)
            else:
                wad.callback(result)
        if evicted_fp is not None:
            # Drop the diagnostics context of the evicted result only after
            # the waiters ran: with MEDIA_CACHE_SIZE=0 the result itself is
            # evicted above, but its waiters still need the context.
            info.safe_diagnostics_contexts.pop(evicted_fp, None)

    # Safe failure diagnostics (see the MEDIA_SAFE_DIAGNOSTICS setting)

    def _safe_diagnostics_errback(
        self, failure: Failure, request: Request, item: Any, info: SpiderInfo
    ) -> Failure:
        if not _media_request_filtered(failure):
            self._emit_safe_diagnostics(request, item, info, failure)
        return failure

    def _build_safe_diagnostics_context(
        self, request: Request, failure: Failure, stage: str
    ) -> dict[str, Any]:
        """Build the request-scoped, item-independent part of a diagnostics
        event. It contains no item data, so it can be shared by all items
        waiting on the same download."""
        origin, path = _safe_url_origin_and_path(request.url)
        status: Any = request.meta.get(_SAFE_DIAGNOSTICS_STATUS_META_KEY)
        exc = failure.value
        if not isinstance(status, int):
            status = _exception_http_status(exc) if exc is not None else None
        return {
            "source": stage,
            "url_origin": origin,
            "url_path": path,
            "http_status": status,
            "exception_class": (
                f"{type(exc).__module__}.{type(exc).__qualname__}"
                if exc is not None
                else None
            ),
        }

    def _safe_diagnostics_item_identifiers(self, item: Any) -> dict[str, Any]:
        """Return the whitelisted business identifiers of *item*, sanitized
        to JSON-native values. Never raises."""
        if not self._safe_diagnostics_item_fields:
            return {}
        try:
            adapter = ItemAdapter(item)
        except Exception:
            return {}
        identifiers: dict[str, Any] = {}
        for field in self._safe_diagnostics_item_fields:
            try:
                value = adapter.get(field, _MISSING)
            except Exception:
                value = _MISSING
            if value is _MISSING:
                continue
            identifiers[field] = _sanitize_identifier_value(value)
        return identifiers

    def safe_diagnostics_url_source(
        self, request: Request, item: Any
    ) -> tuple[str | None, int | None]:
        """Return the ``(field name, index)`` of the item field that
        *request*'s URL comes from, for safe diagnostics events.

        The base implementation returns ``(None, None)``;
        :class:`~scrapy.pipelines.files.FilesPipeline` maps URLs back to its
        ``files_urls_field``. Override it if your ``get_media_requests()``
        builds requests from other fields.
        """
        return None, None

    def safe_diagnostics_emit(self, event: dict[str, Any], info: SpiderInfo) -> None:
        """Sink for safe diagnostics events. The default implementation logs
        the event as a JSON object at the ``ERROR`` level; override it to
        send events elsewhere (e.g. to a structured collector).

        Exceptions raised here are logged (without event contents) and never
        affect download error propagation.
        """
        logger.error(
            "Media download failed (safe diagnostics): %s",
            json.dumps(event, ensure_ascii=False, sort_keys=True),
            extra={"spider": info.spider, "safe_diagnostics_event": event},
        )

    def _emit_safe_diagnostics(
        self, request: Request, item: Any, info: SpiderInfo, failure: Failure
    ) -> None:
        """Build and emit a safe diagnostics event for a failed download.

        Never raises: a diagnostics failure must not swallow the download
        exception, and diagnostics data must not leak secrets, so neither the
        event nor exception reprs are included in fallback log messages.
        """
        try:
            context = info.safe_diagnostics_contexts.get(
                self._fingerprinter.fingerprint(request)
            )
            if context is None:
                context = self._build_safe_diagnostics_context(
                    request, failure, "unknown"
                )
            try:
                field, field_index = self.safe_diagnostics_url_source(request, item)
            except Exception:
                field, field_index = None, None
            event: dict[str, Any] = {
                "event": _SAFE_DIAGNOSTICS_EVENT,
                "spider": getattr(info.spider, "name", None),
                "item": self._safe_diagnostics_item_identifiers(item),
                "field": field,
                "field_index": field_index,
                **context,
            }
        except Exception:
            logger.warning(
                "Failed to build a safe diagnostics event for a failed media download.",
                extra={"spider": info.spider},
            )
            return
        try:
            self.safe_diagnostics_emit(event, info)
        except Exception as exc:
            logger.warning(
                "Safe diagnostics sink failed with %s; the event was discarded.",
                type(exc).__name__,
                extra={"spider": info.spider},
            )

    # Overridable Interface
    @abstractmethod
    def media_to_download(
        self, request: Request, info: SpiderInfo, *, item: Any = None
    ) -> Deferred[FileInfo | None] | None:
        """Check request before starting download"""
        raise NotImplementedError

    @abstractmethod
    def get_media_requests(self, item: Any, info: SpiderInfo) -> list[Request]:
        """Returns the media requests to download"""
        raise NotImplementedError

    @abstractmethod
    def media_downloaded(
        self,
        response: Response,
        request: Request,
        info: SpiderInfo,
        *,
        item: Any = None,
    ) -> FileInfo | Awaitable[FileInfo]:
        """Handler for success downloads"""
        raise NotImplementedError

    @abstractmethod
    def media_failed(
        self, failure: Failure, request: Request, info: SpiderInfo
    ) -> Failure:
        """Handler for failed downloads"""
        raise NotImplementedError

    def item_completed(
        self, results: list[FileInfoOrError], item: Any, info: SpiderInfo
    ) -> Any:
        """Called per item when all media requests has been processed"""
        if self.LOG_FAILED_RESULTS:
            for ok, value in results:
                if not ok:
                    assert isinstance(value, Failure)
                    if _media_request_filtered(value):
                        continue
                    logger.error(
                        "%(class)s found errors processing %(item)s",
                        {"class": self.__class__.__name__, "item": item},
                        exc_info=failure_to_exc_info(value),
                        extra={"spider": info.spider},
                    )
        return item

    @abstractmethod
    def file_path(
        self,
        request: Request,
        response: Response | None = None,
        info: SpiderInfo | None = None,
        *,
        item: Any = None,
    ) -> str:
        """Returns the path where downloaded media should be stored"""
        raise NotImplementedError
