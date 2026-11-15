"""
An extension to retry failed requests that are potentially caused by temporary
problems such as a connection timeout or HTTP 500 error.

You can change the behaviour of this middleware by modifying the scraping settings:
RETRY_TIMES - how many times to retry a failed page
RETRY_HTTP_CODES - which HTTP response codes to retry
"""

from __future__ import annotations

import asyncio
import enum
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from logging import Logger, getLevelNamesMapping, getLogger
from typing import TYPE_CHECKING, Any, Self

from twisted.internet.defer import CancelledError as TwistedCancelledError
from twisted.internet.defer import Deferred

from scrapy import signals
from scrapy.exceptions import NotConfigured
from scrapy.utils.asyncio import sleep
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.defer import deferred_from_coro, maybe_deferred_to_future
from scrapy.utils.misc import _load_objects
from scrapy.utils.python import global_object_name
from scrapy.utils.response import response_status_message

if TYPE_CHECKING:
    from collections.abc import Coroutine, Generator

    import scrapy
    from scrapy.crawler import Crawler
    from scrapy.http import Response
    from scrapy.http.request import Request
    from scrapy.settings import BaseSettings

retry_logger = getLogger(__name__)

_RETRY_AFTER_HEADER = b"Retry-After"
# The value is parsed one character at a time, so its length bounds the work
# spent on a header regardless of what the server sent.
_RETRY_AFTER_MAX_LENGTH = 256
_STATS_BASE_KEY = "retry_after"


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


def _parse_retry_after_delay(values: list[bytes], max_delay: float) -> float | None:
    """Return the delay in seconds asked for by a Retry-After header,
    clamped to ``[0, max_delay]``, or ``None`` if no valid single value is
    present.

    Only an ASCII non-negative integer number of seconds and an HTTP date with
    a time zone are accepted, with optional SP/HTAB on either side. A date is
    measured against the current UTC time.
    """
    if len(values) != 1:
        # A repeated header, or a header line that joined several values with
        # commas: Headers stores both as more than one list entry.
        return None
    raw = values[0]
    if not raw or len(raw) > _RETRY_AFTER_MAX_LENGTH:
        return None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    value = text.strip(" \t")
    if not value:
        return None
    if all(ch in "0123456789" for ch in value):
        return min(int(value), max_delay)
    # Anything else must be a single HTTP-date. The strict parser rejects
    # joined integer values ("10, 20"), fractional values and stray whitespace.
    try:
        date = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if date.tzinfo is None or date.tzinfo.utcoffset(date) is None:
        return None
    delay = (date - datetime.now(UTC)).total_seconds()
    return min(max(delay, 0.0), max_delay)


class _WaitOutcome(enum.Enum):
    COMPLETED = "completed"
    INVALID = "invalid"
    CANCELLED = "cancelled"


class _RetryAfterScheduler:
    """Runs the cancellable waits of one :class:`RetryMiddleware`.

    A wait keeps the retry :class:`~scrapy.http.Request` out of the engine
    until the delay asked for by the server has elapsed. Waiting uses the
    event-loop tools of the running project, so the reactor is never blocked
    and the request is scheduled once, when the wait ends.
    """

    def __init__(self, crawler: Crawler, max_delay: float) -> None:
        self._crawler = crawler
        self._max_delay = max_delay
        self._waiting: set[object] = set()
        crawler.signals.connect(self._spider_closed, signal=signals.spider_closed)

    @property
    def pending(self) -> int:
        return len(self._waiting)

    def _spider_closed(self) -> None:
        # Each middleware belongs to a single crawler, so cancelling its own
        # waits never reaches the waits of another crawler.
        for token in self._waiting:
            assert isinstance(token, _RetryAfterWait)
            token.cancel()
        self._waiting.clear()

    def _record(self, key: str) -> None:
        self._crawler.stats.inc_value(f"{_STATS_BASE_KEY}/{key}")

    async def wait(self, response: Response) -> _WaitOutcome:
        """Wait for the Retry-After delay of *response*.

        Return :attr:`_WaitOutcome.COMPLETED` once the wait ends,
        :attr:`_WaitOutcome.INVALID` for a header that cannot be used (the
        request is retried immediately through the regular path) and
        :attr:`_WaitOutcome.CANCELLED` when the wait is cancelled, including by
        the crawler closing: a cancelled wait must never schedule a retry.
        """
        delay = _parse_retry_after_delay(
            response.headers.getlist(_RETRY_AFTER_HEADER), self._max_delay
        )
        if delay is None:
            self._record("invalid")
            return _WaitOutcome.INVALID
        wait = _RetryAfterWait(delay)
        self._waiting.add(wait)
        self._record("accepted")
        try:
            await wait
        except (asyncio.CancelledError, TwistedCancelledError):
            if wait.cancelled_by_scheduler:
                self._record("cancelled")
                return _WaitOutcome.CANCELLED
            # A cancellation coming from outside (e.g. a fast engine stop) must
            # keep propagating: the engine relies on it to drop the output.
            raise
        finally:
            self._waiting.discard(wait)
        self._crawler.stats.inc_value(f"{_STATS_BASE_KEY}/waited_seconds", count=delay)
        return _WaitOutcome.COMPLETED


class _RetryAfterWait:
    """A cancellable wait built on the project's async tools.

    The sleep coroutine is wrapped through
    :func:`~scrapy.utils.defer.deferred_from_coro`, which drives it with an
    asyncio future when an asyncio reactor runs and with a plain
    :class:`~twisted.internet.defer.Deferred` otherwise. Awaiting goes through
    :func:`~scrapy.utils.defer.maybe_deferred_to_future`, so the same object
    works in a coroutine under every supported reactor.
    """

    def __init__(self, delay: float) -> None:
        self.cancelled_by_scheduler = False
        self._deferred: Deferred[None] = deferred_from_coro(sleep(delay))

    def __await__(self) -> Generator[Any, None, None]:
        return maybe_deferred_to_future(self._deferred).__await__()

    def cancel(self) -> None:
        # Mark the cancellation as ours so the scheduler can swallow it; a
        # cancellation of the surrounding download task is not marked and keeps
        # propagating to the engine.
        self.cancelled_by_scheduler = True
        self._deferred.cancel()


class RetryMiddleware:
    crawler: Crawler

    def __init__(self, settings: BaseSettings):
        if not settings.getbool("RETRY_ENABLED"):
            raise NotConfigured
        self.max_retry_times = settings.getint("RETRY_TIMES")
        self.retry_http_codes = {int(x) for x in settings.getlist("RETRY_HTTP_CODES")}
        self.priority_adjust = settings.getint("RETRY_PRIORITY_ADJUST")
        self.give_up_log_level = settings["RETRY_GIVE_UP_LOG_LEVEL"]
        self.exceptions_to_retry = _load_objects(settings.getlist("RETRY_EXCEPTIONS"))
        self.retry_after_enabled = settings.getbool("RETRY_AFTER_ENABLED")
        self.retry_after_max_delay: Any = self._get_max_delay(settings)
        self._retry_after: _RetryAfterScheduler | None = None

    @staticmethod
    def _get_max_delay(settings: BaseSettings) -> Any:
        max_delay = settings.get("RETRY_AFTER_MAX_DELAY")
        if not settings.getbool("RETRY_AFTER_ENABLED"):
            # The value is unused while the feature is off, so it is neither
            # validated nor converted: enabling the feature re-reads it anyway.
            return max_delay
        if isinstance(max_delay, bool) or not isinstance(max_delay, (int, float)):
            raise TypeError(
                "RETRY_AFTER_MAX_DELAY must be a non-negative finite number,"
                f" got {max_delay!r}"
            )
        if max_delay < 0 or not math.isfinite(max_delay):
            raise ValueError(
                "RETRY_AFTER_MAX_DELAY must be a non-negative finite number,"
                f" got {max_delay!r}"
            )
        return float(max_delay)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        o = cls(crawler.settings)
        o.crawler = crawler
        if o.retry_after_enabled:
            o._retry_after = _RetryAfterScheduler(crawler, o.retry_after_max_delay)
        return o

    @_warn_spider_arg
    def process_response(
        self,
        request: Request,
        response: Response,
        spider: scrapy.Spider | None = None,
    ) -> Request | Response | Coroutine[Any, Any, Request | Response]:
        if request.meta.get("dont_retry", False):
            return response
        if response.status not in self.retry_http_codes:
            return response
        # A wait is only started for a response that still has a retry left,
        # so an exhausted request never parks before being given up on.
        if (
            self._retry_after is not None
            and _has_retry_after(response)
            and self._can_retry(request)
        ):
            return self._retry_after_response(request, response)
        return (
            self._retry(request, response_status_message(response.status)) or response
        )

    async def _retry_after_response(
        self, request: Request, response: Response
    ) -> Request | Response:
        assert self._retry_after is not None
        outcome = await self._retry_after.wait(response)
        if outcome is _WaitOutcome.CANCELLED:
            # The crawler is closing or the request was cancelled: nothing may
            # be scheduled anymore.
            return response
        reason = response_status_message(response.status)
        return self._retry(request, reason) or response

    def _can_retry(self, request: Request) -> bool:
        """Whether *request* still has a retry available, matching the budget
        logic of :func:`get_retry_request` without consuming it."""
        max_retry_times = request.meta.get("max_retry_times")
        if max_retry_times is None:
            max_retry_times = self.max_retry_times
        return bool(request.meta.get("retry_times", 0) + 1 <= max_retry_times)

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


def _has_retry_after(response: Response) -> bool:
    return bool(response.headers.getlist(_RETRY_AFTER_HEADER))
