from __future__ import annotations

import logging
from collections import deque
from math import isfinite
from statistics import median
from typing import TYPE_CHECKING, Self
from weakref import WeakKeyDictionary

from scrapy import Request, Spider, signals
from scrapy.exceptions import NotConfigured
from scrapy.utils.response import _body_size

if TYPE_CHECKING:
    from scrapy.core.downloader import Slot
    from scrapy.crawler import Crawler
    from scrapy.http import Response
    from scrapy.settings import BaseSettings


logger = logging.getLogger(__name__)


def _is_valid_latency(latency: object) -> bool:
    """Return whether *latency* can be used to compute a target delay."""
    return (
        isinstance(latency, (int, float))
        and not isinstance(latency, bool)
        and isfinite(latency)
        and latency >= 0
    )


def _latency_window_size(settings: BaseSettings) -> int | None:
    """Return :setting:`AUTOTHROTTLE_LATENCY_WINDOW` as a positive integer, or
    ``None`` when the window is disabled."""
    value = settings.get("AUTOTHROTTLE_LATENCY_WINDOW")
    if value is None:
        return None
    size: int | None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        size = None
    else:
        # A string is accepted because command-line and environment settings
        # arrive as strings, as long as it spells a whole number.
        try:
            size = int(value)
        except ValueError:
            size = None
    if size is None or size <= 0:
        raise ValueError(
            f"AUTOTHROTTLE_LATENCY_WINDOW ({value!r}) must be a positive integer."
        )
    return size


class AutoThrottle:
    def __init__(self, crawler: Crawler):
        self.crawler: Crawler = crawler
        if not crawler.settings.getbool("AUTOTHROTTLE_ENABLED"):
            raise NotConfigured

        self.debug: bool = crawler.settings.getbool("AUTOTHROTTLE_DEBUG")
        self.target_concurrency: float = crawler.settings.getfloat(
            "AUTOTHROTTLE_TARGET_CONCURRENCY"
        )
        if self.target_concurrency <= 0.0:
            raise NotConfigured(
                f"AUTOTHROTTLE_TARGET_CONCURRENCY "
                f"({self.target_concurrency!r}) must be higher than 0."
            )
        self._latency_window: int | None = _latency_window_size(crawler.settings)
        self._latencies: WeakKeyDictionary[Slot, deque[float]] = WeakKeyDictionary()
        crawler.signals.connect(self._spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(self._spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(
            self._response_downloaded, signal=signals.response_downloaded
        )

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler)

    def _spider_opened(self, spider: Spider) -> None:
        self.mindelay = self._min_delay()
        self.maxdelay = self._max_delay()
        self.crawler.engine.downloader._delay = self._start_delay()

    def _spider_closed(self, spider: Spider) -> None:
        self._latencies.clear()

    def _min_delay(self) -> float:
        return self.crawler.settings.getfloat("DOWNLOAD_DELAY")

    def _max_delay(self) -> float:
        return self.crawler.settings.getfloat("AUTOTHROTTLE_MAX_DELAY")

    def _start_delay(self) -> float:
        return max(
            self.mindelay, self.crawler.settings.getfloat("AUTOTHROTTLE_START_DELAY")
        )

    def _response_downloaded(
        self, response: Response, request: Request, spider: Spider
    ) -> None:
        key, slot = self._get_slot(request, spider)
        latency = request.meta.get("download_latency")
        if (
            latency is None
            or slot is None
            or request.meta.get("autothrottle_dont_adjust_delay", False) is True
        ):
            return
        effective_latency = latency
        if self._latency_window is not None:
            if not _is_valid_latency(latency):
                return
            latencies = self._slot_latencies(slot)
            latencies.append(latency)
            effective_latency = median(latencies)

        olddelay = slot.delay
        self._adjust_delay(slot, effective_latency, response)
        if self.debug:
            diff = slot.delay - olddelay
            size = _body_size(response)
            conc = len(slot.transferring)
            logger.info(
                "slot: %(slot)s | conc:%(concurrency)2d | "
                "delay:%(delay)5d ms (%(delaydiff)+d) | "
                "latency:%(latency)5d ms | size:%(size)6d bytes",
                {
                    "slot": key,
                    "concurrency": conc,
                    "delay": slot.delay * 1000,
                    "delaydiff": diff * 1000,
                    "latency": effective_latency * 1000,
                    "size": size,
                },
                extra={"spider": spider},
            )

    def _get_slot(
        self, request: Request, spider: Spider
    ) -> tuple[str | None, Slot | None]:
        key: str | None = request.meta.get("download_slot")
        if key is None:
            return None, None
        return key, self.crawler.engine.downloader.slots.get(key)

    def _slot_latencies(self, slot: Slot) -> deque[float]:
        """Return the bounded latency window of *slot*, creating it if needed."""
        assert self._latency_window is not None
        latencies = self._latencies.get(slot)
        if latencies is None:
            latencies = deque(maxlen=self._latency_window)
            self._latencies[slot] = latencies
        return latencies

    def _adjust_delay(self, slot: Slot, latency: float, response: Response) -> None:
        """Define delay adjustment policy"""

        # If a server needs `latency` seconds to respond then
        # we should send a request each `latency/N` seconds
        # to have N requests processed in parallel
        target_delay = latency / self.target_concurrency

        # Adjust the delay to make it closer to target_delay
        new_delay = (slot.delay + target_delay) / 2.0

        # If target delay is bigger than old delay, then use it instead of mean.
        # It works better with problematic sites.
        new_delay = max(target_delay, new_delay)

        # Make sure self.mindelay <= new_delay <= self.maxdelay
        new_delay = min(max(self.mindelay, new_delay), self.maxdelay)

        # Dont adjust delay if response status != 200 and new delay is smaller
        # than old one, as error pages (and redirections) are usually small and
        # so tend to reduce latency, thus provoking a positive feedback by
        # reducing delay instead of increase.
        if response.status != 200 and new_delay <= slot.delay:
            return

        slot.delay = new_delay
