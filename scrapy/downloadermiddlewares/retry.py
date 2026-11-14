"""
An extension to retry failed requests that are potentially caused by temporary
problems such as a connection timeout or HTTP 500 error.

You can change the behaviour of this middleware by modifying the scraping settings:
RETRY_TIMES - how many times to retry a failed page
RETRY_HTTP_CODES - which HTTP response codes to retry
"""

from __future__ import annotations

import math
import time
from email.utils import parsedate_to_datetime
from logging import Logger, getLevelNamesMapping, getLogger
from typing import TYPE_CHECKING, Any, Self

from twisted.internet.defer import Deferred

from scrapy import signals
from scrapy.exceptions import DownloadCancelledError, NotConfigured
from scrapy.utils.asyncio import CallLaterResult, call_later
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.defer import maybe_deferred_to_future
from scrapy.utils.misc import _load_objects
from scrapy.utils.python import global_object_name, to_unicode
from scrapy.utils.response import response_status_message

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    import scrapy
    from scrapy.crawler import Crawler
    from scrapy.http import Response
    from scrapy.http.request import Request
    from scrapy.settings import BaseSettings


retry_logger = getLogger(__name__)

# Status codes for which a Retry-After response header is defined (RFC 9110).
_RETRY_AFTER_STATUSES = frozenset({429, 503})
# The header field is a single line; anything much longer than an HTTP date is
# not a value worth parsing.
_RETRY_AFTER_MAX_HEADER_LENGTH = 256


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


class _PendingRetryDelay:
    """A non-blocking wait scheduled for one retried request.

    The timer and the shutdown cleanup settle the wait at most once: a timer
    that fires after the wait was cancelled can neither schedule the request
    nor fire the deferred twice.
    """

    def __init__(
        self,
        delay: float,
        deferred: Deferred[None],
        call_later_function: Callable[..., CallLaterResult],
    ) -> None:
        self.deferred = deferred
        self._settled = False
        self._timer: CallLaterResult = call_later_function(delay, self._fire)

    def _fire(self) -> None:
        if self._settled:
            return
        self._settled = True
        self.deferred.callback(None)

    def cancel(self) -> None:
        """Cancel the timer and fail the wait so its request is not retried."""
        if self._settled:
            return
        self._settled = True
        self._timer.cancel()
        if not self.deferred.called:
            self.deferred.errback(DownloadCancelledError("Retry-After wait cancelled"))


class RetryMiddleware:
    crawler: Crawler

    def __init__(
        self,
        settings: BaseSettings,
        *,
        wall_clock: Callable[[], float] = time.time,
        call_later_function: Callable[..., CallLaterResult] = call_later,
    ) -> None:
        if not settings.getbool("RETRY_ENABLED"):
            raise NotConfigured
        self.max_retry_times = settings.getint("RETRY_TIMES")
        self.retry_http_codes = {int(x) for x in settings.getlist("RETRY_HTTP_CODES")}
        self.priority_adjust = settings.getint("RETRY_PRIORITY_ADJUST")
        self.give_up_log_level = settings["RETRY_GIVE_UP_LOG_LEVEL"]
        self.exceptions_to_retry = _load_objects(settings.getlist("RETRY_EXCEPTIONS"))
        self.retry_after_enabled = settings.getbool("RETRY_AFTER_ENABLED")
        # Keep the parsed value around to reject non-finite settings too.
        self.retry_after_max_delay = settings.getfloat("RETRY_AFTER_MAX_DELAY")
        if self.retry_after_enabled and (
            not math.isfinite(self.retry_after_max_delay)
            or self.retry_after_max_delay <= 0
        ):
            raise ValueError(
                "RETRY_AFTER_MAX_DELAY must be a finite positive number when"
                f" RETRY_AFTER_ENABLED is True, got {self.retry_after_max_delay!r}"
            )
        self._wall_clock = wall_clock
        self._call_later = call_later_function
        self._pending_delays: set[_PendingRetryDelay] = set()

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        o = cls(crawler.settings)
        o.crawler = crawler
        crawler.signals.connect(o.spider_closed, signal=signals.spider_closed)
        return o

    def spider_closed(self, spider: scrapy.Spider) -> None:
        # A timer that fires after this must not schedule any request: every
        # pending wait fails instead, so the awaiting download gets cancelled
        # without a retry being counted.
        for pending in list(self._pending_delays):
            pending.cancel()
        self._pending_delays.clear()

    @_warn_spider_arg
    def process_response(
        self,
        request: Request,
        response: Response,
        spider: scrapy.Spider | None = None,
    ) -> Request | Response | Coroutine[Any, Any, Request | Response]:
        if request.meta.get("dont_retry", False):
            return response
        if response.status in self.retry_http_codes:
            reason = response_status_message(response.status)
            delay = self._get_retry_after_delay(response)
            # A request with no retries left must not wait before giving up.
            if delay is not None and self._can_retry(request):
                return self._retry_after_response(request, reason, delay)
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

    def _can_retry(self, request: Request) -> bool:
        """Whether at least one retry is still available for *request*.

        Mirrors the budget logic of :func:`get_retry_request` without
        counting a retry, so a wait is only scheduled for requests that the
        regular retry path will be able to reschedule.
        """
        max_retry_times = request.meta.get("max_retry_times", self.max_retry_times)
        retry_times = request.meta.get("retry_times", 0) + 1
        return retry_times <= max_retry_times

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

    def _get_retry_after_delay(self, response: Response) -> float | None:
        """Parse the Retry-After header of *response* into a non-negative
        number of seconds, or return ``None`` when the retry must not wait.

        The header is only honored for 429 and 503 responses when
        :setting:`RETRY_AFTER_ENABLED` is set. Integer seconds and HTTP-date
        values are supported. Missing, empty, overlong, malformed or
        unparseable values fall back to an immediate retry. A negative
        delta-seconds value, and an HTTP date in the past, mean no wait.
        A valid delay above :setting:`RETRY_AFTER_MAX_DELAY` is truncated.
        """
        if (
            not self.retry_after_enabled
            or response.status not in _RETRY_AFTER_STATUSES
        ):
            return None
        raw_value = response.headers.get("Retry-After")
        if raw_value is None:
            return None
        value = to_unicode(raw_value, encoding="latin-1").strip()
        if not value or len(value) > _RETRY_AFTER_MAX_HEADER_LENGTH:
            self._record_invalid_retry_after(value)
            return None
        delay = self._parse_retry_after_value(value)
        if delay is None:
            self._record_invalid_retry_after(value)
            return None
        if delay < 0:
            self.crawler.stats.inc_value("retry/retry_after/ignored")
            retry_logger.debug(
                f"Ignoring a Retry-After delay of {delay} seconds for"
                " a response in the past"
            )
            return None
        if delay > self.retry_after_max_delay:
            self.crawler.stats.inc_value("retry/retry_after/truncated")
            retry_logger.debug(
                f"Truncating a Retry-After delay of {delay} seconds to"
                f" {self.retry_after_max_delay} seconds"
            )
            delay = self.retry_after_max_delay
        return delay

    def _parse_retry_after_value(self, value: str) -> float | None:
        # delta-seconds: an optional sign followed by digits only.
        if value.lstrip("-").isdigit():
            return float(int(value))
        try:
            retry_date = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if retry_date.tzinfo is None:
            # RFC 9110 HTTP dates always carry a time zone (GMT).
            return None
        return retry_date.timestamp() - self._wall_clock()

    def _record_invalid_retry_after(self, value: str) -> None:
        self.crawler.stats.inc_value("retry/retry_after/invalid")
        retry_logger.debug(f"Ignoring a malformed Retry-After header: {value!r}")

    async def _retry_after_response(
        self,
        request: Request,
        reason: str | Exception | type[Exception],
        delay: float,
    ) -> Request | Response:
        pending = _PendingRetryDelay(delay, Deferred(), self._call_later)
        self._pending_delays.add(pending)
        try:
            await maybe_deferred_to_future(pending.deferred)
        finally:
            self._pending_delays.discard(pending)
        # The retry request, its priority adjustment and retry_times are all
        # produced by the regular retry path once the wait is over.
        self.crawler.stats.inc_value("retry/retry_after/delayed")
        return self._retry(request, reason) or response
