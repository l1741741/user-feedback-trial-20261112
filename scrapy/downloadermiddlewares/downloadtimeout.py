"""
Download timeout middleware

See documentation in docs/topics/downloader-middleware.rst
"""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING, Self

from scrapy import Request, Spider, signals
from scrapy.exceptions import DownloadDeadlineExceededError
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.deprecate import warn_on_deprecated_spider_attribute

if TYPE_CHECKING:
    from scrapy.crawler import Crawler
    from scrapy.http import Response


# Request.meta key holding the total download deadline of a request chain, as
# a monotonic timestamp. Retries and redirects copy their meta from the
# request they derive from, so the whole chain shares the deadline stamped on
# the first request.
_DEADLINE_META_KEY = "_download_deadline"


class DownloadTimeoutMiddleware:
    def __init__(self, timeout: float = 180, deadline: float = 0):
        self._timeout: float = timeout
        self._deadline: float = deadline

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        try:
            deadline = crawler.settings.getfloat("DOWNLOAD_DEADLINE")
        except (TypeError, ValueError):
            deadline = -1.0
        if not deadline >= 0:
            raise ValueError(
                f"Invalid DOWNLOAD_DEADLINE value: "
                f"{crawler.settings['DOWNLOAD_DEADLINE']!r}, it must be a "
                f"non-negative number of seconds."
            ) from None
        o = cls(crawler.settings.getfloat("DOWNLOAD_TIMEOUT"), deadline)
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        return o

    def spider_opened(self, spider: Spider) -> None:
        if hasattr(spider, "download_timeout"):  # pragma: no cover
            warn_on_deprecated_spider_attribute("download_timeout", "DOWNLOAD_TIMEOUT")
        self._timeout = getattr(spider, "download_timeout", self._timeout)

    @_warn_spider_arg
    def process_request(
        self, request: Request, spider: Spider | None = None
    ) -> Request | Response | None:
        if self._timeout:
            request.meta.setdefault("download_timeout", self._timeout)
        if self._deadline:
            self._apply_deadline(request)
        return None

    def _apply_deadline(self, request: Request) -> None:
        now = monotonic()
        deadline = request.meta.get(_DEADLINE_META_KEY)
        if deadline is None:
            deadline = now + self._deadline
            request.meta[_DEADLINE_META_KEY] = deadline
        remaining = deadline - now
        if remaining <= 0:
            raise DownloadDeadlineExceededError(
                f"The total download deadline of {self._deadline:g} seconds "
                f"has been exceeded for {request}."
            )
        timeout = request.meta.get("download_timeout")
        if timeout is None or remaining < timeout:
            request.meta["download_timeout"] = remaining
