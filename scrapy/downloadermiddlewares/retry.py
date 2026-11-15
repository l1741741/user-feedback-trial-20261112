"""
An extension to retry failed requests that are potentially caused by temporary
problems such as a connection timeout or HTTP 500 error.

You can change the behaviour of this middleware by modifying the scraping settings:
RETRY_TIMES - how many times to retry a failed page
RETRY_HTTP_CODES - which HTTP response codes to retry
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from logging import Logger, getLevelNamesMapping, getLogger
from time import time
from typing import TYPE_CHECKING, Self

from twisted.internet.defer import Deferred

from scrapy import signals
from scrapy.exceptions import NotConfigured
from scrapy.utils.asyncio import _get_running_or_installed_loop, is_asyncio_available
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.defer import maybe_deferred_to_future
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import _load_objects
from scrapy.utils.python import global_object_name
from scrapy.utils.response import response_status_message

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable
    from typing import Any, Protocol
    from urllib.parse import ParseResult

    import scrapy
    from scrapy.crawler import Crawler
    from scrapy.http import Response
    from scrapy.http.request import Request
    from scrapy.settings import BaseSettings
    from scrapy.statscollectors import StatsCollector

    class _Clock(Protocol):
        """Monotonic source of time and one-shot timers."""

        def seconds(self) -> float: ...

        def callLater(
            self,
            delay: float,
            callable: Callable[..., Any],  # noqa: A002 - IReactorTime name
            *args: Any,
            **kw: Any,
        ) -> _DelayedCall: ...

    class _DelayedCall(Protocol):
        """A timer that can be cancelled; cancelling an expired one is a
        no-op."""

        def cancel(self) -> None: ...

        def active(self) -> bool: ...


retry_logger = getLogger(__name__)

# Per-request meta key holding the monotonic time after which a request that
# waits on Retry-After headers must not wait any longer. Copied with the
# request, so the limit applies to a request and all of its retries together.
_RETRY_AFTER_DEADLINE_META_KEY = "_retry_after_deadline"


def get_retry_request(
    request: Request,
    *,
    spider: scrapy.Spider,
    reason: str | Exception | type[Exception] = "unspecified",
    max_retry_times: int | None = None,
    priority_adjust: int | None = None,
    logger: Logger = retry_logger,
    give_up_log_level: int | str | None = None,
    stats_base_key: str = "retry",
) -> Request | None:
    """
    Returns a new :class:`~scrapy.Request` object to retry the specified
    request, or ``None`` if retries of the specified request have been
    exhausted.

    For example, in a :class:`~scrapy.Spider` callback, you could use it as
    follows:

    .. code-block:: python

        def parse(self, response):
            if not response.text:
                new_request_or_none = get_retry_request(
                    response.request,
                    spider=self,
                    reason="empty",
                )
                return new_request_or_none

    *spider* is the :class:`~scrapy.Spider` instance which is asking for the
    retry request. It is used to access the :ref:`settings <topics-settings>`
    and :ref:`stats <topics-stats>`, and to provide extra logging context (see
    :func:`logging.debug`).

    *reason* is a string, an :class:`Exception` subclass or an
    :class:`Exception` object that indicates the reason why the request needs
    to be retried. It is used to name retry stats.

    *max_retry_times* is a number that determines the maximum number of times
    that *request* can be retried. If not specified or ``None``, the number is
    read from the :reqmeta:`max_retry_times` meta key of the request. If the
    :reqmeta:`max_retry_times` meta key is not defined or ``None``, the number
    is read from the :setting:`RETRY_TIMES` setting.

    *priority_adjust* is a number that determines how the priority of the new
    request changes in relation to *request*. If not specified, the number is
    read from the :setting:`RETRY_PRIORITY_ADJUST` setting.

    *logger* is the logging.Logger object to be used when logging messages

    *give_up_log_level* is the :ref:`logging level <levels>` used for the
    message logged when a request exceeds its retries. See
    :setting:`RETRY_GIVE_UP_LOG_LEVEL` for details.

    .. versionadded:: 2.17.0
       The *give_up_log_level* parameter.

    *stats_base_key* is a string to be used as the base key for the
    retry-related job stats
    """
    settings = spider.crawler.settings
    stats = spider.crawler.stats
    retry_times = request.meta.get("retry_times", 0) + 1
    if max_retry_times is None:
        max_retry_times = request.meta.get("max_retry_times")
        if max_retry_times is None:
            max_retry_times = settings.getint("RETRY_TIMES")
    if retry_times <= max_retry_times:
        logger.debug(
            "Retrying %(request)s (failed %(retry_times)d times): %(reason)s",
            {"request": request, "retry_times": retry_times, "reason": reason},
            extra={"spider": spider},
        )
        new_request: Request = request.copy()
        new_request.meta["retry_times"] = retry_times
        new_request.meta["skip_dupefilter_once"] = True
        if priority_adjust is None:
            priority_adjust = settings.getint("RETRY_PRIORITY_ADJUST")
        new_request.priority = request.priority + priority_adjust

        if callable(reason):
            reason = reason()
        if isinstance(reason, Exception):
            reason = global_object_name(reason.__class__)

        stats.inc_value(f"{stats_base_key}/count")
        stats.inc_value(f"{stats_base_key}/reason_count/{reason}")
        return new_request
    if give_up_log_level is None:
        give_up_log_level = settings["RETRY_GIVE_UP_LOG_LEVEL"]
    if isinstance(give_up_log_level, str):
        level = getLevelNamesMapping().get(give_up_log_level)
        if level is None:
            raise ValueError(f"Invalid give-up log level: {give_up_log_level!r}")
        give_up_log_level = level
    stats.inc_value(f"{stats_base_key}/max_reached")
    assert give_up_log_level is not None
    logger.log(
        give_up_log_level,
        "Gave up retrying %(request)s (failed %(retry_times)d times): %(reason)s",
        {"request": request, "retry_times": retry_times, "reason": reason},
        extra={"spider": spider},
    )
    return None


def _parse_retry_after(value: str) -> float | None:
    """Return the delay in seconds asked by a Retry-After header value, or
    ``None`` when the value is neither a non-negative number of seconds nor an
    HTTP date.

    HTTP dates are converted against the wall clock so that clock differences
    between the crawler and the server do not matter. Values outside the range
    a timestamp can hold are rejected instead of raising.
    """
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    # A date without time zone information is not an HTTP date: its offset from
    # the local wall clock would depend on the machine running the crawl.
    if parsed.tzinfo is None:
        return None
    try:
        delay = parsed.timestamp() - time()
    except (OverflowError, OSError, ValueError):
        return None
    if not math.isfinite(delay) or delay < 0:
        return None
    return delay


def _origin_key(request: Request) -> tuple[str, str, int | None]:
    """Identify the origin of *request* by its scheme, normalized host and
    port. Different schemes, hosts or ports never share a wait."""
    parsed: ParseResult = urlparse_cached(request)
    return parsed.scheme.lower(), parsed.hostname or "", parsed.port


def _origin_name(origin: tuple[str, str, int | None]) -> str:
    """Render an origin key as the stable string used in stats."""
    scheme, host, port = origin
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


class _TimerHandle:
    """Adapt an asyncio timer handle to the cancel/active interface the gate
    shares with a Twisted ``IDelayedCall``."""

    def __init__(self, handle: asyncio.TimerHandle) -> None:
        self._handle = handle

    def cancel(self) -> None:
        # TimerHandle.cancel() is safe to call on a timer that already fired.
        self._handle.cancel()

    def active(self) -> bool:
        return not self._handle.cancelled()


class _AsyncioClock:
    """Expose the timing API of :class:`twisted.internet.task.Clock` over an
    asyncio event loop.

    The middleware awaits on the asyncio loop, so a timer scheduled on the
    Twisted reactor would never fire when the two are not driven together. The
    adapter keeps the injected-clock seam used by the tests while scheduling on
    the loop that actually runs the wait.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def seconds(self) -> float:
        return self._loop.time()

    def callLater(
        self,
        delay: float,
        callable: Callable[..., Any],  # noqa: A002 - IReactorTime name
        *args: Any,
        **kw: Any,
    ) -> _TimerHandle:
        # asyncio timers call back with positional arguments only, matching the
        # gate, which never schedules with keyword arguments.
        assert not kw
        return _TimerHandle(self._loop.call_later(delay, callable, *args))


def _default_clock() -> _Clock:
    if is_asyncio_available():
        return _AsyncioClock(_get_running_or_installed_loop())
    from twisted.internet import reactor

    return reactor


@dataclass(slots=True, eq=False)
class _OriginWait:
    """Shared state of the requests waiting on one origin.

    The single delayed call fires once for every waiter at the current
    deadline. Extending the deadline reschedules it instead of adding another
    timer, so one origin holds at most one timer no matter how many requests
    wait.
    """

    deadline: float
    waiters: list[Deferred[None]] = field(default_factory=list)
    call: _DelayedCall | None = None


class _OriginWaitGate:
    """Per-origin wait behind Retry-After headers of 429 responses.

    A wait extends the shared deadline of its origin when the requested one is
    later, and never shortens a wait already in progress. Origins identified by
    different scheme, host or port are fully independent.
    """

    def __init__(self, clock: _Clock, stats: StatsCollector) -> None:
        self._clock = clock
        self._stats = stats
        self._waits: dict[tuple[str, str, int | None], _OriginWait] = {}

    def now(self) -> float:
        return float(self._clock.seconds())

    def extend_deadline(
        self, origin: tuple[str, str, int | None], deadline: float
    ) -> _OriginWait:
        """Set the deadline of *origin* to the later of *deadline* and its
        current one, rescheduling the shared timer when it moves later."""
        wait = self._waits.get(origin)
        if wait is None:
            wait = _OriginWait(deadline=deadline)
            self._waits[origin] = wait
            wait.call = self._clock.callLater(
                deadline - self._clock.seconds(), self._release, origin
            )
        elif deadline > wait.deadline:
            wait.deadline = deadline
            assert wait.call is not None
            wait.call.cancel()
            wait.call = self._clock.callLater(
                deadline - self._clock.seconds(), self._release, origin
            )
        return wait

    async def wait(self, wait: _OriginWait) -> None:
        """Wait until the shared deadline of *wait*.

        Cancelling the request that awaits removes only its waiter; the shared
        timer keeps running for the other waiters of the origin.
        """

        def remove_waiter(waiter: Deferred[None]) -> None:
            if waiter in wait.waiters:
                wait.waiters.remove(waiter)

        waiter: Deferred[None] = Deferred(remove_waiter)
        wait.waiters.append(waiter)
        try:
            await maybe_deferred_to_future(waiter)
        except BaseException:
            # Cancellation of the wrapping future cancels the Deferred, whose
            # canceller removes only this waiter.
            self._stats.inc_value("retry/wait/cancelled")
            raise

    def _release(self, origin: tuple[str, str, int | None]) -> None:
        wait = self._waits.pop(origin, None)
        if wait is None:
            return
        wait.call = None
        while wait.waiters:
            wait.waiters.pop(0).callback(None)

    def close(self) -> None:
        """Release every waiter and cancel every timer, e.g. on spider close,
        so that neither timers nor request references remain."""
        for wait in self._waits.values():
            if wait.call is not None and wait.call.active():
                wait.call.cancel()
            wait.call = None
            while wait.waiters:
                wait.waiters.pop(0).callback(None)
        self._waits.clear()


class RetryMiddleware:
    crawler: Crawler

    def __init__(
        self,
        settings: BaseSettings,
        stats: StatsCollector,
        *,
        clock: _Clock | None = None,
    ) -> None:
        if not settings.getbool("RETRY_ENABLED"):
            raise NotConfigured
        self.max_retry_times = settings.getint("RETRY_TIMES")
        self.retry_http_codes = {int(x) for x in settings.getlist("RETRY_HTTP_CODES")}
        self.priority_adjust = settings.getint("RETRY_PRIORITY_ADJUST")
        self.give_up_log_level = settings["RETRY_GIVE_UP_LOG_LEVEL"]
        self.exceptions_to_retry = _load_objects(settings.getlist("RETRY_EXCEPTIONS"))
        self._retry_after_max_wait = settings.getfloat("RETRY_RETRY_AFTER_MAX_WAIT")
        self._retry_after_max_total_wait = settings.getfloat(
            "RETRY_RETRY_AFTER_MAX_TOTAL_WAIT"
        )
        self._stats = stats
        self._gate: _OriginWaitGate | None = None
        if settings.getbool("RETRY_RETRY_AFTER_ENABLED"):
            self._gate = _OriginWaitGate(clock or _default_clock(), stats)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        o = cls(crawler.settings, crawler.stats)
        o.crawler = crawler
        if o._gate is not None:
            crawler.signals.connect(o.spider_closed, signal=signals.spider_closed)
        return o

    def spider_closed(self) -> None:
        if self._gate is not None:
            self._gate.close()

    @_warn_spider_arg
    async def process_response(
        self,
        request: Request,
        response: Response,
        spider: scrapy.Spider | None = None,
    ) -> Request | Response:
        if request.meta.get("dont_retry", False):
            return response
        if response.status in self.retry_http_codes:
            reason = response_status_message(response.status)
            if response.status == 429 and self._gate is not None:
                await self._wait_retry_after(request, response)
            return self._retry(request, reason) or response
        return response

    @_warn_spider_arg
    def process_exception(
        self,
        request: Request,
        exception: Exception,
        spider: scrapy.Spider | None = None,
    ) -> Request | Response | None:
        if isinstance(exception, self.exceptions_to_retry) and not request.meta.get(
            "dont_retry", False
        ):
            return self._retry(request, exception)
        return None

    async def _wait_retry_after(self, request: Request, response: Response) -> None:
        """Hold *request* behind the per-origin gate of its Retry-After header.

        A missing or malformed header does not hold the request. The wait is
        cut to the per-request total deadline and the maximum single wait; a
        wait cut to nothing skips the gate entirely.
        """
        assert self._gate is not None
        header = response.headers.get(b"Retry-After")
        if header is None:
            return
        try:
            requested = _parse_retry_after(header.decode("ascii"))
        except UnicodeDecodeError:
            requested = None
        if requested is None:
            self._stats.inc_value("retry/wait/invalid_header")
            return
        now = self._gate.now()
        deadline = now + requested
        if self._retry_after_max_wait >= 0:
            deadline = min(deadline, now + self._retry_after_max_wait)
        total_deadline = request.meta.get(_RETRY_AFTER_DEADLINE_META_KEY)
        if total_deadline is not None:
            # The total budget only limits waits after the first one: the
            # first wait starts the budget and is bounded by the per-wait cap.
            deadline = min(deadline, float(total_deadline))
        if deadline <= now:
            # A zero Retry-After asks for an immediate retry, not a capped
            # wait; only a positive wait cut to nothing reaches a limit.
            if requested > 0:
                self._stats.inc_value("retry/wait/limit_reached")
            return
        if total_deadline is None and self._retry_after_max_total_wait >= 0:
            # Stamp only when the request really enters a wait: the total
            # budget must not start while nothing is being waited out. The
            # stamp is copied into retries, so it spans the request life.
            request.meta[_RETRY_AFTER_DEADLINE_META_KEY] = (
                now + self._retry_after_max_total_wait
            )
        origin = _origin_key(request)
        wait = self._gate.extend_deadline(origin, deadline)
        if wait.deadline > deadline:
            self._stats.inc_value("retry/wait/extended")
        self._stats.inc_value("retry/wait/count")
        self._stats.inc_value(f"retry/wait/origin_count/{_origin_name(origin)}")
        await self._gate.wait(wait)

    def _retry(
        self, request: Request, reason: str | Exception | type[Exception]
    ) -> Request | None:
        max_retry_times = request.meta.get("max_retry_times", self.max_retry_times)
        priority_adjust = request.meta.get("priority_adjust", self.priority_adjust)
        give_up_log_level = request.meta.get(
            "give_up_log_level", self.give_up_log_level
        )
        assert self.crawler.spider
        return get_retry_request(
            request,
            reason=reason,
            spider=self.crawler.spider,
            max_retry_times=max_retry_times,
            priority_adjust=priority_adjust,
            give_up_log_level=give_up_log_level,
        )
