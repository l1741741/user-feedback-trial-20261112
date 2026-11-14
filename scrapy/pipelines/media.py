from __future__ import annotations

import asyncio
import functools
import logging
import warnings
from abc import ABC, abstractmethod
from collections import OrderedDict, defaultdict
from contextlib import suppress
from traceback import clear_frames
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    NotRequired,
    Self,
    TypeAlias,
    TypedDict,
    cast,
)

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


#: Request meta key under which :class:`~scrapy.pipelines.files.FilesPipeline`
#: (and subclasses, such as :class:`~scrapy.pipelines.images.ImagesPipeline`)
#: store the URL exactly as it appeared in the item's URL list, before it was
#: passed to :class:`~scrapy.http.Request` and percent-encoded. When
#: :setting:`MEDIA_RESULTS_BY_SOURCE_INDEX` is enabled, the value is delivered
#: back as the ``source_url`` field of every result record.
MEDIA_SOURCE_URL_META_KEY: str = "media_source_url"


class FileInfo(TypedDict):
    url: str
    path: str
    checksum: str | None
    status: str


class SourceFileInfo(TypedDict):
    """A successful media result aligned with one position of the input URL
    list.

    Delivered (as the second element of a ``(True, record)`` tuple) only when
    :setting:`MEDIA_RESULTS_BY_SOURCE_INDEX` is enabled.
    """

    #: Position of the source URL in the list returned by
    #: :meth:`~MediaPipeline.get_media_requests` (0-based).
    source_index: int
    #: URL exactly as provided by the spider before :class:`~scrapy.http.Request`
    #: construction and URL percent-encoding.
    source_url: str
    #: URL of the :class:`~scrapy.http.Request` that produced the result, i.e.
    #: the source URL after Request URL normalization. Redirects do not change
    #: this field.
    url: str
    path: str
    checksum: str | None
    status: str
    #: Present only when a custom pipeline's request errback recovered with a
    #: non-dict value; built-in pipelines always provide path/checksum/status.
    value: NotRequired[Any]


class MediaErrorInfo(TypedDict):
    """The explicit failure description of one source position.

    Delivered (as the second element of a ``(False, record)`` tuple) only when
    :setting:`MEDIA_RESULTS_BY_SOURCE_INDEX` is enabled. Failures are kept in
    place instead of being dropped, so later indices never shift.
    """

    source_index: int
    source_url: str
    #: Request URL after Request URL normalization, when a request existed.
    url: str | None
    #: Dotted class name of the exception that caused the failure, e.g.
    #: ``"scrapy.pipelines.media.FileException"``.
    error_type: str
    #: String representation of that exception.
    error_message: str


SourceMediaResult: TypeAlias = SourceFileInfo | MediaErrorInfo


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


class _MediaPipelineClosed(FileException):
    """Raised internally for source positions whose media request is still in
    flight when the pipeline is closed, so that they are delivered as explicit
    failure records instead of hanging forever."""


def _media_request_filtered(failure: Failure) -> bool:
    return isinstance(failure.value, _MediaRequestFiltered)


class _SourcePosition:
    """One input position of a media request list.

    Each position gets its own object, even when several positions (of the same
    item or of different items) share a request fingerprint, so per-item
    result containers never share mutable state.
    """

    __slots__ = ("index", "request", "source_url")

    def __init__(self, index: int, request: Request, source_url: str) -> None:
        self.index: int = index
        self.request: Request = request
        self.source_url: str = source_url


class MediaPipeline(ABC):
    LOG_FAILED_RESULTS: bool = True

    #: Name used for the stats of this pipeline. Concrete built-in pipelines
    #: override it (``"file"`` / ``"image"``).
    MEDIA_NAME: str = "media"

    class SpiderInfo:
        def __init__(self, spider: Spider):
            self.spider: Spider = spider
            self.downloading: set[bytes] = set()
            self.downloaded: OrderedDict[bytes, FileInfo | Failure] = OrderedDict()
            self.waiting: defaultdict[bytes, list[Deferred[FileInfo]]] = defaultdict(
                list
            )
            #: Source positions of requests that have been registered but have
            #: not received their result yet. Only populated when
            #: MEDIA_RESULTS_BY_SOURCE_INDEX is enabled.
            self.inflight: set[_SourcePosition] = set()
            #: Maps a request fingerprint to the asyncio tasks of the positions
            #: currently awaiting it, so close_spider() can unblock a position
            #: parked on a shielded download without cancelling the download.
            self.position_tasks: dict[bytes, list[asyncio.Task[Any]]] = defaultdict(
                list
            )
            #: The shielded tasks that perform the one actual download per
            #: fingerprint, kept as references so they never become orphaned
            #: tasks and so close_spider() can let them finish naturally.
            self.download_tasks: set[asyncio.Task[None]] = set()
            #: Set while close_spider() releases the in-flight positions.
            self.closing: bool = False

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
        self.results_by_source_index: bool = settings.getbool(
            resolve("MEDIA_RESULTS_BY_SOURCE_INDEX"), False
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
        if self.results_by_source_index:
            return await self._process_item_by_source_index(requests, item, info)
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

    async def _process_item_by_source_index(
        self,
        requests: list[Request],
        item: Any,
        info: SpiderInfo,
    ) -> Any:
        """Process *requests* keeping one result per input position.

        Results are placed back at the index of the request they belong to, so
        neither out-of-order completion nor dropped failures can shift the
        indices. Requests with the same fingerprint are still merged for the
        actual download; every position merely gets its own record describing
        the shared outcome.
        """
        records: list[tuple[bool, SourceMediaResult] | None] = [None] * len(requests)
        coros = [
            self._process_source_position(
                _SourcePosition(index, request, self.get_media_source_url(request)),
                info,
                item,
                records,
            )
            for index, request in enumerate(requests)
        ]
        if coros:
            if is_asyncio_available():
                await asyncio.gather(*coros)
            else:
                await cast(
                    "Deferred[list[Any]]",
                    DeferredList(
                        (deferred_from_coro(coro) for coro in coros), consumeErrors=True
                    ),
                )
        # Every position must have been filled, success or failure, regardless
        # of the order in which downloads completed.
        results = cast("list[tuple[bool, SourceMediaResult]]", records)
        return await ensure_awaitable(self.item_completed(results, item, info))

    async def _process_source_position(
        self,
        position: _SourcePosition,
        info: SpiderInfo,
        item: Any,
        records: list[tuple[bool, SourceMediaResult] | None],
    ) -> None:
        """Resolve one :class:`_SourcePosition` and write its record back at
        its own index, never appending in completion order."""
        fp = self._fingerprinter.fingerprint(position.request)
        current_task = asyncio.current_task()
        info.inflight.add(position)
        if current_task is not None:
            info.position_tasks[fp].append(current_task)
        try:
            try:
                result = await self._process_request(
                    position.request, info, item
                )
                record: SourceMediaResult = self._build_source_success(
                    position, result
                )
                ok = True
            except asyncio.CancelledError:
                # close_spider() cancels the wait of a position parked on a
                # shielded download; the position's own barrier was already
                # failed with the closing error, so surface that as a record.
                if not info.closing:
                    raise
                record = self._build_source_error(
                    position,
                    Failure(_MediaPipelineClosed("media pipeline is closing")),
                )
                ok = False
            except BaseException as exc:  # noqa: BLE001 - any failure is a record
                record = self._build_source_error(position, Failure(exc))
                ok = False
            records[position.index] = (ok, record)
            self.inc_source_stats("ok" if ok else "failed")
        finally:
            info.inflight.discard(position)
            if current_task is not None:
                with suppress(ValueError):
                    info.position_tasks.get(fp, []).remove(current_task)

    def _build_source_success(
        self, position: _SourcePosition, result: FileInfo | Any
    ) -> SourceFileInfo:
        """Turn the value returned for *position* into a source-aligned
        success record.

        Built-in pipelines return :class:`FileInfo` dicts; custom pipelines
        may return arbitrary values from request errbacks, which are wrapped
        instead of being dropped.
        """
        if isinstance(result, dict):
            # Keep any extra keys an errback recovery may have added, only
            # overriding the fields that describe the source position.
            record = SourceFileInfo(
                **{
                    **result,
                    "source_index": position.index,
                    "source_url": position.source_url,
                    "url": result.get("url") or position.request.url,
                    "path": result.get("path", ""),
                    "checksum": result.get("checksum"),
                    "status": result.get("status", ""),
                }
            )
            return record
        return SourceFileInfo(
            source_index=position.index,
            source_url=position.source_url,
            url=position.request.url,
            path="",
            checksum=None,
            status="",
            value=result,
        )

    def _build_source_error(
        self, position: _SourcePosition, failure: Failure
    ) -> MediaErrorInfo:
        value = failure.value
        return MediaErrorInfo(
            source_index=position.index,
            source_url=position.source_url,
            url=position.request.url,
            error_type=global_object_name(type(value)),
            error_message=str(value),
        )

    def get_media_source_url(self, request: Request) -> str:
        """Return the URL of *request* as it was provided by the spider,
        before :class:`~scrapy.http.Request` normalized it.

        Built-in pipelines store that URL under
        :data:`MEDIA_SOURCE_URL_META_KEY` when building their requests; custom
        pipelines can override this method to provide their own source URL.
        """
        source_url = request.meta.get(MEDIA_SOURCE_URL_META_KEY)
        if isinstance(source_url, str):
            return source_url
        return request.url

    def inc_source_stats(self, status: Literal["ok", "failed"]) -> None:
        """Count results by *source position*, independently of the download
        stats that count actual downloader/store activity."""
        stats = self.crawler.stats
        stats.inc_value(f"{self.MEDIA_NAME}_source_count")
        stats.inc_value(f"{self.MEDIA_NAME}_source_status_count/{status}")

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
                if eb:
                    return eb(cached_result)
                cached_result.raiseException()
            return cached_result

        # Otherwise, wait for result. wad is registered with SpiderInfo and is
        # fired by _cache_result_and_execute_waiters(); waiter is a per-position
        # barrier whose no-op canceller stops an asyncio cancellation of this
        # position from propagating to wad and, through it, to the shared
        # download or to the other waiters.
        wad: Deferred[FileInfo] = Deferred()
        if eb:
            wad.addErrback(eb)
        waiter: Deferred[FileInfo] = Deferred(lambda _d: None)
        wad.chainDeferred(waiter)
        info.waiting[fp].append(wad)

        # Check if request is downloading right now to avoid doing it twice
        if fp in info.downloading:
            try:
                return await maybe_deferred_to_future(waiter)
            finally:
                # Drop only this position's Deferred so a later result cannot
                # fire the detached barrier, and leave the other waiters alone.
                with suppress(ValueError):
                    info.waiting.get(fp, []).remove(wad)

        # Download request checking media_to_download hook output first
        info.downloading.add(fp)
        if is_asyncio_available():
            # Shield the shared download from the cancellation of this
            # position's coroutine: cancelling one waiter must neither cancel
            # the actual download nor fail the other waiters. The inner task
            # is tracked explicitly so it is never orphaned, and it always runs
            # to completion and caches its outcome.
            download_task = asyncio.ensure_future(
                self._download_and_cache_result(request, info, item, fp)
            )
            info.download_tasks.add(download_task)
            download_task.add_done_callback(info.download_tasks.discard)
            try:
                await asyncio.shield(download_task)
            except asyncio.CancelledError:
                raise
        else:  # pragma: no cover - non-asyncio reactors are deprecated
            await _process_pending_io()
            result: FileInfo | Failure
            try:
                file_info: FileInfo | None = await ensure_awaitable(
                    self.media_to_download(request, info, item=item)
                )
                if file_info:
                    # got a result without downloading
                    result = file_info
                else:
                    # download the result
                    result = await self._check_media_to_download(
                        request, info, item=item
                    )
            except Exception:
                result = Failure()
                if not _media_request_filtered(result):
                    logger.exception(result)
            await _process_pending_io()
            self._cache_result_and_execute_waiters(result, fp, info)
        try:
            # it must return the waiter at last
            return await maybe_deferred_to_future(waiter)
        finally:
            with suppress(ValueError):
                info.waiting.get(fp, []).remove(wad)

    async def _download_and_cache_result(
        self, request: Request, info: SpiderInfo, item: Any, fp: bytes
    ) -> None:
        """Perform the one actual download for a fingerprint and cache its
        outcome, waking up every waiting position when done."""
        await _process_pending_io()
        result: FileInfo | Failure
        try:
            file_info: FileInfo | None = await ensure_awaitable(
                self.media_to_download(request, info, item=item)
            )
            if file_info:
                # got a result without downloading
                result = file_info
            else:
                # download the result
                result = await self._check_media_to_download(request, info, item=item)
        except Exception:
            result = Failure()
            if not _media_request_filtered(result):
                logger.exception(result)
        await _process_pending_io()
        self._cache_result_and_execute_waiters(result, fp, info)

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
        if 0 <= self._cache_size < len(info.downloaded):
            info.downloaded.popitem(last=False)
        # The list may be missing if close_spider() already released the
        # waiters, or if every waiter was cancelled individually.
        for wad in info.waiting.pop(fp, []):
            if isinstance(result, Failure):
                wad.errback(result)
            else:
                wad.callback(result)

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
                if ok:
                    continue
                if isinstance(value, Failure):
                    if _media_request_filtered(value):
                        continue
                    logger.error(
                        "%(class)s found errors processing %(item)s",
                        {"class": self.__class__.__name__, "item": item},
                        exc_info=failure_to_exc_info(value),
                        extra={"spider": info.spider},
                    )
                else:
                    # MEDIA_RESULTS_BY_SOURCE_INDEX mode: failures are explicit
                    # MediaErrorInfo records, not Failure objects.
                    logger.error(
                        "%(class)s found errors processing %(item)s: "
                        "%(error_type)s at source index %(index)d "
                        "(source URL %(source_url)s): %(error_message)s",
                        {
                            "class": self.__class__.__name__,
                            "item": item,
                            "error_type": value["error_type"],
                            "index": value["source_index"],
                            "source_url": value["source_url"],
                            "error_message": value["error_message"],
                        },
                        extra={"spider": info.spider},
                    )
        return item

    @_warn_spider_arg
    async def close_spider(self, spider: Spider | None = None) -> None:
        """Release the source positions still in flight when the spider closes.

        Only relevant when :setting:`MEDIA_RESULTS_BY_SOURCE_INDEX` is enabled.
        Every waiting position receives an explicit failure instead of
        awaiting a result that will never arrive. The shared downloads
        themselves are not cancelled (their shielded inner tasks keep running
        and cache whatever outcome they reach), and each waiting Deferred is
        fired at most once.
        """
        info = self.spiderinfo
        if not self.results_by_source_index or info.closing:
            return
        info.closing = True
        # Each in-flight (non-cached) position has its own registered Deferred
        # in one of the waiting queues. Firing every queue releases the
        # positions that await their barrier directly; positions parked on a
        # shielded download first need their wait task cancelled so they
        # resume, at which point their already-failed barrier gives them the
        # closing failure. This covers positions of several items sharing a
        # fingerprint, and never cancels the shielded download itself.
        for fp, wads in list(info.waiting.items()):
            failure = Failure(_MediaPipelineClosed("media pipeline is closing"))
            for wad in wads:
                wad.errback(failure)
            info.waiting.pop(fp, None)
            for task in list(info.position_tasks.get(fp, [])):
                task.cancel()
            # Give the released coroutines a turn to turn the failure into a
            # record and leave the inflight set through their finally block.
            await _process_pending_io()
        # Positions resolved from the outcome cache (without a waiting
        # Deferred) drain on their own. The bound guards against a scheduling
        # stall instead of looping forever.
        rounds = 0
        while info.inflight and rounds <= len(info.inflight) + 1:
            await _process_pending_io()
            rounds += 1
        # Safety net: nothing should remain here in normal operation.
        info.inflight.clear()
        # The shielded downloads are deliberately not cancelled here: the
        # engine owns their lifecycle and cancels them during shutdown. Do not
        # await them (they may never resolve), but retrieve the result of the
        # ones that already finished so no exception is reported as unhandled.
        for download_task in list(info.download_tasks):
            if download_task.done():
                download_task.exception()
        info.download_tasks.clear()

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
