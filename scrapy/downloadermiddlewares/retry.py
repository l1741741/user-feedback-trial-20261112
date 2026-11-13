"""
An extension to retry failed requests that are potentially caused by temporary
problems such as a connection timeout or HTTP 500 error.

You can change the behaviour of this middleware by modifying the scraping settings:
RETRY_TIMES - how many times to retry a failed page
RETRY_HTTP_CODES - which HTTP response codes to retry
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from logging import Logger, getLevelNamesMapping, getLogger
from time import monotonic as _monotonic
from typing import TYPE_CHECKING, Self

from scrapy.exceptions import NotConfigured
from scrapy.utils.asyncio import sleep as _sleep
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import _load_objects
from scrapy.utils.python import global_object_name
from scrapy.utils.response import response_status_message

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

    import scrapy
    from scrapy.crawler import Crawler
    from scrapy.http import Response
    from scrapy.http.request import Request
    from scrapy.settings import BaseSettings


retry_logger = getLogger(__name__)

_Origin = tuple[str, str, int | None]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _retry_after_seconds(value: bytes) -> float | None:
    """Return the delay requested by a Retry-After header *value*, in seconds.

    Return ``None`` if *value* is not a non-negative integer number of seconds
    or an HTTP date with a time zone.
    """
    try:
        text = value.decode("ascii").strip(" \t")
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    # str.isdigit() also accepts non-ASCII digits, such as Arabic-Indic ones,
    # so check for ASCII digits only.
    if text.isascii() and all("0" <= char <= "9" for char in text):
        return float(text)
    try:
        date = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if date.tzinfo is None:
        return None
    return max(0.0, (date - _utcnow()).total_seconds())


def _request_origin(request: Request) -> _Origin | None:
    """Return the (scheme, host, port) origin of *request*."""
    parsed = urlparse_cached(request)
    if parsed.hostname is None:
        return None
    port = parsed.port
    if port is None:
        port = {"http": 80, "https": 443}.get(parsed.scheme)
    return parsed.scheme, parsed.hostname.lower(), port


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
        retry_after_max_delay = settings.getfloat("RETRY_AFTER_MAX_DELAY")
        if not math.isfinite(retry_after_max_delay) or retry_after_max_delay <= 0:
            raise ValueError(
                "RETRY_AFTER_MAX_DELAY must be a positive finite number, got"
                f" {retry_after_max_delay!r}"
            )
        self.retry_after_max_delay = retry_after_max_delay
        # Monotonic deadlines, so that wall-clock changes cannot end a wait
        # early or extend it indefinitely.
        self._retry_after_deadlines: dict[_Origin, float] = {}

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        o = cls(crawler.settings)
        o.crawler = crawler
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
        reason = response_status_message(response.status)
        if self.retry_after_enabled and self._has_retry_budget(request):
            origin = _request_origin(request)
            if origin is not None:
                result = self._retry_after_response(request, response, reason, origin)
                if result is not None:
                    return result
        return self._retry(request, reason) or response

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

    def _has_retry_budget(self, request: Request) -> bool:
        """Return ``True`` if a retry of *request* would be attempted.

        This mirrors the checks in :func:`get_retry_request` without its side
        effects, so that no backoff is started for a response that cannot be
        retried anyway.
        """
        retry_times: int = request.meta.get("retry_times", 0) + 1
        max_retry_times: int | None = request.meta.get("max_retry_times")
        if max_retry_times is None:
            max_retry_times = self.max_retry_times
        return retry_times <= max_retry_times

    def _retry_after_response(
        self,
        request: Request,
        response: Response,
        reason: str,
        origin: _Origin,
    ) -> Request | Response | Coroutine[Any, Any, Request | Response] | None:
        """Handle the Retry-After logic for a response that may be retried.

        Return a value to return from :meth:`process_response`, or ``None`` to
        fall back to the regular retry path.
        """
        now = _monotonic()
        self._prune_expired_deadlines(now)
        deadline = self._retry_after_deadlines.get(origin)

        values = response.headers.getlist("Retry-After")
        if values:
            if len(values) > 1:
                return None
            delay = _retry_after_seconds(values[0])
            if delay is None:
                return None
            # A zero delay, or a date that has already passed, asks for no
            # wait: retry as before, without touching a window.
            if delay <= 0:
                return None
            # The per-response cap is positive, so this target is in the
            # future. A shorter delay than the registered window does not
            # shorten it; this response then waits for the longer one.
            target = now + min(delay, self.retry_after_max_delay)
            if deadline is None or target > deadline:
                self._retry_after_deadlines[origin] = target
                deadline = target

        if deadline is None or deadline <= now:
            return None
        return self._delayed_retry(origin, request, response, reason)

    async def _delayed_retry(
        self,
        origin: _Origin,
        request: Request,
        response: Response,
        reason: str,
    ) -> Request | Response:
        await self._wait_for_deadline(origin)
        new_request = self._retry(request, reason)
        if new_request is not None:
            self.crawler.stats.inc_value("retry_after/delayed")
            return new_request
        return response

    async def _wait_for_deadline(self, origin: _Origin) -> None:
        """Wait until the origin deadline passes or its window is removed.

        The deadline is read again after every sleep, so a window extended by
        another response keeps this and every other waiter waiting until the
        latest deadline. Nothing is cancelled or cleaned up when this
        coroutine is cancelled: the deadline belongs to the origin, not to
        this waiter.
        """
        while True:
            deadline = self._retry_after_deadlines.get(origin)
            if deadline is None:
                return
            remaining = deadline - _monotonic()
            if remaining <= 0:
                del self._retry_after_deadlines[origin]
                return
            await _sleep(remaining)

    def _prune_expired_deadlines(self, now: float) -> None:
        expired = [
            origin
            for origin, deadline in self._retry_after_deadlines.items()
            if deadline <= now
        ]
        for origin in expired:
            del self._retry_after_deadlines[origin]

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
