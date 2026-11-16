"""
Download timeout middleware

See documentation in docs/topics/downloader-middleware.rst
"""

from __future__ import annotations

import math
from time import monotonic
from typing import TYPE_CHECKING, Self
from weakref import WeakSet

from scrapy import Request, Spider, signals
from scrapy.exceptions import DownloadDeadlineExceededError
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.deprecate import warn_on_deprecated_spider_attribute

if TYPE_CHECKING:
    from collections.abc import Callable

    from scrapy.crawler import Crawler
    from scrapy.http import Response
    from scrapy.statscollectors import StatsCollector


# Request meta key holding the absolute deadline of a request and of the
# requests derived from it, as a reading of the monotonic clock. Requests copy
# their meta when they are replaced, e.g. on retries and redirects, so the
# whole chain shares the deadline. It is internal: only this middleware writes
# it, and user code must not set it.
_DEADLINE_META_KEY = "_download_deadline"

# Readings at or above this are wall-clock timestamps, such as time.time()
# values, not readings of a monotonic clock, which counts seconds since boot.
# Mixing the two clock domains would give deadlines that never expire, or that
# expired long ago.
_MAX_CLOCK_SECONDS = 1e9

_DEADLINE_META_DESCRIPTION = f"the {_DEADLINE_META_KEY!r} request meta value"


def _invalid_deadline(value: object) -> ValueError:
    return ValueError(
        f"Invalid DOWNLOAD_DEADLINE value {value!r}: expected a finite, "
        f"non-negative number of seconds below {_MAX_CLOCK_SECONDS:g}, i.e. a "
        f"duration on a monotonic clock. Wall-clock timestamps, negative "
        f"numbers, which do not mean an unlimited deadline, and non-numbers "
        f"are rejected."
    )


def _parse_deadline(value: object) -> float:
    """Return the DOWNLOAD_DEADLINE *value* as a number of seconds."""
    # bool is an int subclass, so True would otherwise pass for one second.
    if isinstance(value, bool):
        raise _invalid_deadline(value)
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _invalid_deadline(value) from None
    if not math.isfinite(seconds) or not 0 <= seconds < _MAX_CLOCK_SECONDS:
        raise _invalid_deadline(value)
    return seconds


def _read_clock_seconds(value: object, description: str) -> float:
    """Return *value* as a reading of the monotonic clock."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"Invalid {description} {value!r}: expected a number of seconds on "
            f"a monotonic clock."
        )
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 <= seconds < _MAX_CLOCK_SECONDS:
        raise ValueError(
            f"Invalid {description} {value!r}: expected a reading of a "
            f"monotonic clock, below {_MAX_CLOCK_SECONDS:g} seconds. A "
            f"wall-clock timestamp or a negative value is not one."
        )
    return seconds


def _timeout_seconds(value: object) -> float | None:
    """Return *value*, a per-download timeout, as seconds, or ``None`` when it
    does not limit the download."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


class DownloadTimeoutMiddleware:
    """Set the per-download timeout of requests, and, when the
    :setting:`DOWNLOAD_DEADLINE` setting is set, limit the total time that the
    downloads of a request and of the requests derived from it may take.

    Without :setting:`DOWNLOAD_DEADLINE`, the middleware writes nothing but
    the ``download_timeout`` request meta key, as before.
    """

    def __init__(
        self,
        timeout: float = 180,
        deadline: float | None = None,
        *,
        clock: Callable[[], float] | None = None,
        stats: StatsCollector | None = None,
    ):
        self._timeout: float = timeout
        self._deadline: float | None = (
            None if deadline is None else _parse_deadline(deadline)
        )
        # Until a clock is passed, monotonic() is looked up on every reading,
        # so that tests can replace the default one.
        self._clock: Callable[[], float] | None = clock
        self._stats: StatsCollector | None = stats
        # Requests already counted as expired, so that checking the same
        # request again, e.g. when it is queued and again right before its
        # download, does not count it twice.
        self._expired: WeakSet[Request] = WeakSet()

    @classmethod
    def from_crawler(
        cls, crawler: Crawler, *, clock: Callable[[], float] | None = None
    ) -> Self:
        raw_deadline = crawler.settings.get("DOWNLOAD_DEADLINE")
        # An invalid value raises here, when the crawl engine is built and
        # before any request is scheduled.
        o = cls(
            crawler.settings.getfloat("DOWNLOAD_TIMEOUT"),
            None if raw_deadline is None else _parse_deadline(raw_deadline),
            clock=clock,
            stats=crawler.stats,
        )
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(o.spider_closed, signal=signals.spider_closed)
        return o

    def spider_opened(self, spider: Spider) -> None:
        if hasattr(spider, "download_timeout"):  # pragma: no cover
            warn_on_deprecated_spider_attribute("download_timeout", "DOWNLOAD_TIMEOUT")
        self._timeout = getattr(spider, "download_timeout", self._timeout)
        self._expired.clear()

    def spider_closed(self) -> None:
        """Drop the state kept for the crawl."""
        self._expired.clear()

    def _now(self) -> float:
        """Return the current reading of the monotonic clock."""
        clock = monotonic if self._clock is None else self._clock
        return _read_clock_seconds(clock(), "download deadline clock reading")

    @_warn_spider_arg
    def process_request(
        self, request: Request, spider: Spider | None = None
    ) -> Request | Response | None:
        if self._timeout:
            request.meta.setdefault("download_timeout", self._timeout)
        if self._deadline is not None:
            self._apply_deadline(request, self._ensure_deadline(request))
        return None

    def _check_download_deadline(self, request: Request) -> None:
        """Cap the download of *request* at its remaining budget.

        The downloader calls this right before the download handler runs, so
        that the time that the request spent waiting for its turn is accounted
        for and no connection is opened once the deadline is up.
        """
        if self._deadline is None:
            return
        if _DEADLINE_META_KEY in request.meta:
            self._apply_deadline(request, self._read_deadline(request))

    def _apply_deadline(self, request: Request, deadline: float) -> None:
        remaining = deadline - self._now()
        if remaining <= 0:
            self._count_expired(request)
            raise DownloadDeadlineExceededError(
                f"The total download deadline of {request} expired "
                f"{-remaining:g} seconds ago."
            )
        timeout = _timeout_seconds(request.meta.get("download_timeout"))
        # Keep the lower of the per-download timeout and the remaining budget,
        # whether the per-download timeout comes from the setting, the
        # download_timeout request meta key, or an earlier check.
        if timeout is None or remaining < timeout:
            request.meta["download_timeout"] = remaining

    def _read_deadline(self, request: Request) -> float:
        """Return the absolute deadline of *request* from its meta, rejecting
        values stamped by another process or clock."""
        assert self._deadline is not None
        deadline = _read_clock_seconds(
            request.meta[_DEADLINE_META_KEY], _DEADLINE_META_DESCRIPTION
        )
        # A deadline is stamped at most this budget ahead, and time only takes
        # away from it, so a later one comes from another clock, e.g. the
        # monotonic clock of the process that wrote a disk queue. Rounding
        # makes the difference of the stamping round trip slightly over the
        # budget, which is tolerated.
        slack = max(1e-6, self._deadline * 1e-9)
        if deadline - self._now() > self._deadline + slack:
            raise ValueError(
                f"Invalid {_DEADLINE_META_KEY!r} request meta value "
                f"{request.meta[_DEADLINE_META_KEY]!r}: it is further into the "
                f"future than the DOWNLOAD_DEADLINE budget of "
                f"{self._deadline:g} seconds, so it was not stamped by this "
                f"crawl."
            )
        return deadline

    def _ensure_deadline(self, request: Request) -> float:
        """Return the absolute deadline of *request*, stamping a new one on
        the first request of a chain."""
        if _DEADLINE_META_KEY in request.meta:
            return self._read_deadline(request)
        assert self._deadline is not None
        deadline = self._now() + self._deadline
        request.meta[_DEADLINE_META_KEY] = deadline
        return deadline

    def _count_expired(self, request: Request) -> None:
        if self._stats is None or request in self._expired:
            return
        self._expired.add(request)
        self._stats.inc_value("download_deadline/exceeded")
