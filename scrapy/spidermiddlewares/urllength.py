"""
Url Length Spider Middleware

See documentation in docs/topics/spider-middleware.rst
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Self
from urllib.parse import urlunparse

from scrapy.exceptions import NotConfigured
from scrapy.spidermiddlewares.base import BaseSpiderMiddleware
from scrapy.utils.httpobj import urlparse_cached

if TYPE_CHECKING:
    from scrapy.crawler import Crawler
    from scrapy.http import Request, Response


logger = logging.getLogger(__name__)


class UrlLengthMeasure(StrEnum):
    """How the length of a request URL is measured.

    .. versionadded:: VERSION
    """

    CHARS = "chars"
    BYTES = "bytes"


class UrlLengthMiddleware(BaseSpiderMiddleware):
    """Filter out requests whose URL exceeds :setting:`URLLENGTH_LIMIT`.

    The length is measured either in characters of
    :attr:`~scrapy.Request.url` or, when :setting:`URLLENGTH_LIMIT_MEASURE`
    is ``"bytes"``, in UTF-8 bytes of the request target sent over the wire.
    The fragment never counts towards the byte length because it is kept by
    the client and is not transmitted.

    .. versionchanged:: VERSION
       The :setting:`URLLENGTH_LIMIT_MEASURE` setting was added.
    """

    crawler: Crawler

    def __init__(  # pylint: disable=super-init-not-called
        self, maxlength: int, *, measure: UrlLengthMeasure = UrlLengthMeasure.CHARS
    ):
        self.maxlength: int = maxlength
        self.measure: UrlLengthMeasure = measure

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        settings = crawler.settings
        raw_maxlength = settings.get("URLLENGTH_LIMIT", 0)
        if (
            isinstance(raw_maxlength, bool)
            or not isinstance(raw_maxlength, int)
            or raw_maxlength < 0
        ):
            raise ValueError("URLLENGTH_LIMIT must be a non-negative integer")
        if not raw_maxlength:
            raise NotConfigured
        raw_measure = settings.get(
            "URLLENGTH_LIMIT_MEASURE", UrlLengthMeasure.CHARS.value
        )
        allowed = ", ".join(m.value for m in UrlLengthMeasure)
        if not isinstance(raw_measure, str):
            raise ValueError(
                f"Invalid URLLENGTH_LIMIT_MEASURE value: {raw_measure!r}. "
                f"Allowed values: {allowed}"
            )
        try:
            measure = UrlLengthMeasure(raw_measure)
        except ValueError as exc:
            raise ValueError(
                f"Invalid URLLENGTH_LIMIT_MEASURE value: {exc.args[0]!r}. "
                f"Allowed values: {allowed}"
            ) from exc
        o = cls(raw_maxlength, measure=measure)
        o.crawler = crawler
        return o

    def _get_length(self, request: Request) -> int:
        if self.measure is UrlLengthMeasure.CHARS:
            return len(request.url)
        parsed = urlparse_cached(request)
        # The fragment is kept by the client and never sent as part of the
        # request target, so it cannot count towards its on-wire size.
        target = urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, "")
        )
        return len(target.encode("utf-8"))

    def get_processed_request(
        self, request: Request, response: Response | None
    ) -> Request | None:
        length = self._get_length(request)
        if length <= self.maxlength:
            return request
        parsed = urlparse_cached(request)
        # Keep the query out of the logs: it may carry credentials or tokens.
        logged_url = urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", "")
        )
        logger.info(
            "Ignoring link (url %(measure)s length %(length)d > "
            "%(maxlength)d): %(url)s ",
            {
                "measure": self.measure.value,
                "length": length,
                "maxlength": self.maxlength,
                "url": logged_url,
            },
            extra={"spider": self.crawler.spider},
        )
        stats = self.crawler.stats
        stats.inc_value("urllength/request_ignored_count")
        stats.inc_value(f"urllength/{self.measure.value}/request_ignored_count")
        stats.max_value(f"urllength/{self.measure.value}/max_ignored_length", length)
        return None
