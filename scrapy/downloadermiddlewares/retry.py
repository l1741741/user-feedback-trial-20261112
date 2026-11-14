"""
An extension to retry failed requests that are potentially caused by temporary
problems such as a connection timeout or HTTP 500 error.

You can change the behaviour of this middleware by modifying the scraping settings:
RETRY_TIMES - how many times to retry a failed page
RETRY_HTTP_CODES - which HTTP response codes to retry
"""

from __future__ import annotations

import math
from email.utils import parsedate_to_datetime
from logging import Logger, getLevelNamesMapping, getLogger
from typing import TYPE_CHECKING, Protocol, Self, cast

from scrapy.exceptions import NotConfigured
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.defer import maybe_deferred_to_future
from scrapy.utils.misc import _load_objects
from scrapy.utils.python import global_object_name
from scrapy.utils.response import response_status_message

if TYPE_CHECKING:
    from collections.abc import Callable

    from twisted.internet.base import DelayedCall
    from twisted.internet.defer import Deferred

    import scrapy
    from scrapy.crawler import Crawler
    from scrapy.http import Response
    from scrapy.http.request import Request
    from scrapy.settings import BaseSettings


retry_logger = getLogger(__name__)

# Largest delay the scheduling code is willing to hand to a clock. Anything
# above it is treated as an overflow rather than a plausible wait time.
_MAX_SCHEDULED_DELAY = float(2**31 - 1)


class _Clock(Protocol):
    """Time source and delayed-call scheduler used by the middleware.

    The running reactor implements this protocol, and tests inject a
    :class:`twisted.internet.task.Clock` instead so that waiting never blocks.
    """

    def seconds(self) -> float: ...

    def callLater(
        self,
        delay: float,
        callable: Callable[..., object],  # noqa: A002
        *args: object,
    ) -> DelayedCall: ...


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


def _parse_http_date(value: bytes) -> float | None:
    """Parse an HTTP-date into epoch seconds, or ``None`` when it is not a
    valid date carrying time zone information."""
    try:
        parsed = parsedate_to_datetime(value.decode("latin-1"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _retry_after_delay(response: Response, now: float) -> float | None:
    """Return the wait in seconds asked by a Retry-After header.

    ``None`` means the header is absent or cannot be honored: more than one
    header, a blank or non-numeric value that is not an HTTP date, a negative
    or non-finite number, or an overflow. An HTTP-date in the past, including
    one made to look past by a skewed server ``Date`` header, resolves to
    zero.
    """
    values = response.headers.getlist("Retry-After")
    if len(values) != 1:
        return None
    raw_value = values[0].strip()
    if not raw_value:
        return None

    if raw_value.isdigit():
        try:
            seconds = float(int(raw_value))
        except OverflowError:
            return None
        if not math.isfinite(seconds) or seconds > _MAX_SCHEDULED_DELAY:
            return None
        return seconds

    retry_after = _parse_http_date(raw_value)
    if retry_after is None:
        return None
    # Convert the HTTP date against the server's own clock when the response
    # carries a valid Date header, so clock skew does not inflate the wait.
    date_values = response.headers.getlist("Date")
    if len(date_values) == 1:
        server_time = _parse_http_date(date_values[0])
        if server_time is not None:
            return max(0.0, retry_after - server_time)
    return max(0.0, retry_after - now)


class RetryMiddleware:
    crawler: Crawler

    def __init__(
        self,
        settings: BaseSettings,
        *,
        clock: _Clock | None = None,
    ):
        if not settings.getbool("RETRY_ENABLED"):
            raise NotConfigured
        self.max_retry_times = settings.getint("RETRY_TIMES")
        self.retry_http_codes = {int(x) for x in settings.getlist("RETRY_HTTP_CODES")}
        self.priority_adjust = settings.getint("RETRY_PRIORITY_ADJUST")
        self.give_up_log_level = settings["RETRY_GIVE_UP_LOG_LEVEL"]
        self.exceptions_to_retry = _load_objects(settings.getlist("RETRY_EXCEPTIONS"))
        self.retry_after_enabled = settings.getbool("RETRY_AFTER_ENABLED")
        # A negative value disables the budget, meaning any finite delay may
        # be scheduled.
        self.retry_after_max_delay = settings.getfloat("RETRY_AFTER_MAX_DELAY")
        if clock is None:
            from twisted.internet import reactor

            clock = cast("_Clock", reactor)
        self._clock = clock

    @classmethod
    def from_crawler(cls, crawler: Crawler, *, clock: _Clock | None = None) -> Self:
        o = cls(crawler.settings, clock=clock)
        o.crawler = crawler
        return o

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
            retry_request = self._retry(request, reason)
            if retry_request is None:
                return response
            delay = self._retry_after_delay(response)
            if delay is None:
                return retry_request
            if not self._fits_schedule(request, delay):
                return response
            await self._wait(retry_request, response, delay)
            return retry_request
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

    def _retry_after_delay(self, response: Response) -> float | None:
        """Delay in seconds mandated by Retry-After, or ``None`` to retry now."""
        if not self.retry_after_enabled or response.status != 429:
            return None
        return _retry_after_delay(response, self._clock.seconds())

    def _fits_schedule(self, request: Request, delay: float) -> bool:
        """Whether *delay* stays within both the wait budget and the request's
        absolute deadline."""
        if not math.isfinite(delay) or delay < 0.0:
            return False
        max_delay = request.meta.get(
            "retry_after_max_delay", self.retry_after_max_delay
        )
        if max_delay is not None and 0.0 <= max_delay < delay:
            return False
        deadline = request.meta.get("download_deadline")
        return deadline is None or delay <= float(deadline) - self._clock.seconds()

    async def _wait(
        self, retry_request: Request, response: Response, delay: float
    ) -> None:
        """Wait *delay* seconds without blocking the reactor.

        The wait is driven by the injected clock through a deferred that the
        clock settles, so cancelling the surrounding coroutine cancels the
        delayed call instead of leaving it pending.
        """
        from twisted.internet.defer import CancelledError, Deferred  # noqa: PLC0415

        def cancel_wait(deferred: Deferred[None]) -> None:
            if delayed_call.active():
                delayed_call.cancel()
            deferred.errback(
                CancelledError(
                    f"Retry of {retry_request.url} cancelled while waiting "
                    f"{delay:.3f}s for Retry-After"
                )
            )

        pending: Deferred[None] = Deferred(cancel_wait)
        delayed_call = self._clock.callLater(delay, pending.callback, None)
        retry_logger.debug(
            f"Waiting {delay:.3f}s before retrying {retry_request} "
            f"(Retry-After on {response.status})"
        )
        await maybe_deferred_to_future(pending)
