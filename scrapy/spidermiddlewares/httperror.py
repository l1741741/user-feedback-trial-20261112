"""
HttpError Spider Middleware

See documentation in docs/topics/spider-middleware.rst
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Self

from scrapy.exceptions import IgnoreRequest
from scrapy.utils.decorators import _warn_spider_arg

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from scrapy import Spider
    from scrapy.crawler import Crawler
    from scrapy.http import Response
    from scrapy.settings import BaseSettings


logger = logging.getLogger(__name__)


def _merge_status_ranges(
    ranges: Iterable[Sequence[int]], source: str
) -> list[tuple[int, int]]:
    """Validate *ranges* and return them sorted and merged.

    Every range must be a ``(start, end)`` pair of integers (bools excluded)
    with ``100 <= start <= end <= 599``. Invalid values raise
    :exc:`ValueError` instead of being silently ignored.
    """
    validated: list[tuple[int, int]] = []
    for r in ranges:
        if not isinstance(r, (list, tuple)) or len(r) != 2:
            raise ValueError(
                f"Invalid HTTP status range {r!r} in {source}: "
                "expected a (start, end) pair"
            )
        start, end = r
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
        ):
            raise ValueError(
                f"Invalid HTTP status range {r!r} in {source}: "
                "start and end must be integers"
            )
        if not 100 <= start <= end <= 599:
            raise ValueError(
                f"Invalid HTTP status range {r!r} in {source}: "
                "expected 100 <= start <= end <= 599"
            )
        validated.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(validated):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class HttpError(IgnoreRequest):
    """Raised by :class:`HttpErrorMiddleware` for a response whose status code
    is not successful and that the spider does not handle itself.
    """

    def __init__(self, response: Response, *args: object, **kwargs: object):
        self.response: Response = response
        """The response that was filtered out."""
        super().__init__(*args, **kwargs)


class HttpErrorMiddleware:
    crawler: Crawler

    def __init__(self, settings: BaseSettings):
        self.handle_httpstatus_all: bool = settings.getbool("HTTPERROR_ALLOW_ALL")
        self.handle_httpstatus_list: list[int] = settings.getlist(
            "HTTPERROR_ALLOWED_CODES"
        )
        self.handle_httpstatus_ranges: list[tuple[int, int]] = _merge_status_ranges(
            settings.getlist("HTTPERROR_ALLOWED_STATUS_RANGES"),
            "the HTTPERROR_ALLOWED_STATUS_RANGES setting",
        )

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        o = cls(crawler.settings)
        o.crawler = crawler
        return o

    @_warn_spider_arg
    def process_spider_input(
        self, response: Response, spider: Spider | None = None
    ) -> None:
        if 200 <= response.status < 300:  # common case
            return
        meta = response.meta
        if meta.get("handle_httpstatus_all", False):
            return
        if "handle_httpstatus_list" in meta or "handle_httpstatus_ranges" in meta:
            allowed_statuses = meta.get("handle_httpstatus_list", ())
            allowed_ranges = _merge_status_ranges(
                meta.get("handle_httpstatus_ranges", ()),
                "the handle_httpstatus_ranges request meta key",
            )
        elif self.handle_httpstatus_all:
            return
        else:
            allowed_statuses = getattr(
                self.crawler.spider,
                "handle_httpstatus_list",
                self.handle_httpstatus_list,
            )
            spider_ranges = getattr(
                self.crawler.spider, "handle_httpstatus_ranges", None
            )
            if spider_ranges is None:
                allowed_ranges = self.handle_httpstatus_ranges
            else:
                allowed_ranges = _merge_status_ranges(
                    spider_ranges, "the handle_httpstatus_ranges spider attribute"
                )
        if response.status in allowed_statuses:
            return
        if any(start <= response.status <= end for start, end in allowed_ranges):
            return
        raise HttpError(response, "Ignoring non-200 response")

    @_warn_spider_arg
    def process_spider_exception(
        self, response: Response, exception: Exception, spider: Spider | None = None
    ) -> Iterable[Any] | None:
        if isinstance(exception, HttpError):
            stats = self.crawler.stats
            stats.inc_value("httperror/response_ignored_count")
            stats.inc_value(
                f"httperror/response_ignored_status_count/{response.status}"
            )
            logger.info(
                "Ignoring response %(response)r: HTTP status code is not handled or not allowed",
                {"response": response},
                extra={"spider": self.crawler.spider},
            )
            return ()
        return None
