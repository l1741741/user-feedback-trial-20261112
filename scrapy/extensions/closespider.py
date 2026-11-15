"""CloseSpider is an extension that forces spiders to be closed after certain
conditions are met.

See documentation in docs/topics/extensions.rst
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Self

from twisted.python.failure import Failure

from scrapy import Request, Spider, signals
from scrapy.exceptions import NotConfigured
from scrapy.utils.asyncio import (
    AsyncioLoopingCall,
    CallLaterResult,
    call_later,
    create_looping_call,
)
from scrapy.utils.defer import _schedule_coro

if TYPE_CHECKING:
    from twisted.internet.task import LoopingCall

    from scrapy.crawler import Crawler
    from scrapy.http import Response


logger = logging.getLogger(__name__)


class CloseSpider:
    """Close a spider when any of the conditions enabled through the
    ``CLOSESPIDER_*`` settings is met.

    Only the first condition that the extension processes asks the engine to
    close the spider; it locks the closing reason and the error flag, which
    later conditions cannot change. See :ref:`closespider-close-reason` for
    which reason ends up being used.
    """

    def __init__(self, crawler: Crawler):
        self.crawler: Crawler = crawler

        # The closing reason and error flag of the first condition met. They
        # are set together with _close_requested, so that later conditions do
        # not change them.
        self.close_reason: str | None = None
        self._close_error: bool = False
        # The exception raised while asking the engine to close the spider.
        self.close_failure: Failure | None = None
        self._close_requested: bool = False
        self._closed: bool = False

        # for CLOSESPIDER_TIMEOUT
        self.task: CallLaterResult | None = None

        # for CLOSESPIDER_TIMEOUT_NO_ITEM
        self.task_no_item: AsyncioLoopingCall[[], None] | LoopingCall | None = None

        self.close_on: dict[str, Any] = {
            "timeout": crawler.settings.getfloat("CLOSESPIDER_TIMEOUT"),
            "itemcount": crawler.settings.getint("CLOSESPIDER_ITEMCOUNT"),
            "pagecount": crawler.settings.getint("CLOSESPIDER_PAGECOUNT"),
            "errorcount": crawler.settings.getint("CLOSESPIDER_ERRORCOUNT"),
            "timeout_no_item": crawler.settings.getint("CLOSESPIDER_TIMEOUT_NO_ITEM"),
            "pagecount_no_item": crawler.settings.getint(
                "CLOSESPIDER_PAGECOUNT_NO_ITEM"
            ),
        }

        if not any(self.close_on.values()):
            raise NotConfigured

        self.counter: defaultdict[str, int] = defaultdict(int)

        if self.close_on.get("errorcount"):
            crawler.signals.connect(self.error_count, signal=signals.spider_error)
        if self.close_on.get("pagecount") or self.close_on.get("pagecount_no_item"):
            crawler.signals.connect(self.page_count, signal=signals.response_received)
        if self.close_on.get("timeout"):
            crawler.signals.connect(self.spider_opened, signal=signals.spider_opened)
        if self.close_on.get("itemcount") or self.close_on.get("pagecount_no_item"):
            crawler.signals.connect(self.item_scraped, signal=signals.item_scraped)
        if self.close_on.get("timeout_no_item"):
            self.timeout_no_item: int = self.close_on["timeout_no_item"]
            self.items_in_period: int = 0
            crawler.signals.connect(
                self.spider_opened_no_item, signal=signals.spider_opened
            )
            crawler.signals.connect(
                self.item_scraped_no_item, signal=signals.item_scraped
            )

        crawler.signals.connect(self.spider_closed, signal=signals.spider_closed)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler)

    def error_count(self, failure: Failure, response: Response, spider: Spider) -> None:
        self.counter["errorcount"] += 1
        if self.counter["errorcount"] == self.close_on["errorcount"]:
            self._close_spider("closespider_errorcount", error=True)

    def page_count(self, response: Response, request: Request, spider: Spider) -> None:
        self.counter["pagecount"] += 1
        self.counter["pagecount_since_last_item"] += 1
        if self.counter["pagecount"] == self.close_on["pagecount"]:
            self._close_spider("closespider_pagecount")
            return
        if self.close_on["pagecount_no_item"] and (
            self.counter["pagecount_since_last_item"]
            >= self.close_on["pagecount_no_item"]
        ):
            self._close_spider("closespider_pagecount_no_item")

    def spider_opened(self, spider: Spider) -> None:
        self.task = call_later(
            self.close_on["timeout"], self._close_spider, "closespider_timeout"
        )

    def item_scraped(self, item: Any, spider: Spider) -> None:
        self.counter["itemcount"] += 1
        self.counter["pagecount_since_last_item"] = 0
        if self.counter["itemcount"] == self.close_on["itemcount"]:
            self._close_spider("closespider_itemcount")

    def spider_closed(self, spider: Spider) -> None:
        self._closed = True

        if self.task:
            self.task.cancel()
            self.task = None

        if self.task_no_item:
            if self.task_no_item.running:  # pragma: no branch
                self.task_no_item.stop()
            self.task_no_item = None

    def spider_opened_no_item(self, spider: Spider) -> None:
        self.task_no_item = create_looping_call(self._count_items_produced)
        self.task_no_item.start(self.timeout_no_item, now=False)

        logger.info(
            f"Spider will stop when no items are produced after "
            f"{self.timeout_no_item} seconds."
        )

    def item_scraped_no_item(self, item: Any, spider: Spider) -> None:
        self.items_in_period += 1

    def _count_items_produced(self) -> None:
        # A call may still be queued when the spider_closed handler stopped
        # the looping call, or when another condition already asked to close
        # the spider.
        if self._close_requested or self._closed:
            return
        if self.items_in_period >= 1:
            self.items_in_period = 0
        else:
            logger.info(
                f"Closing spider since no items were produced in the last "
                f"{self.timeout_no_item} seconds."
            )
            self._close_spider("closespider_timeout_no_item")

    def _close_spider(self, reason: str, *, error: bool = False) -> None:
        # The first condition that gets here locks the reason and the error
        # flag of this crawler, and is the only one that asks the engine to
        # close the spider. Callbacks that were queued before the spider was
        # closed, by this or any other component, do nothing.
        if self._close_requested or self._closed:
            return
        self._close_requested = True
        self.close_reason = reason
        self._close_error = error
        _schedule_coro(self._close_spider_async(reason, error=error))

    async def _close_spider_async(self, reason: str, *, error: bool = False) -> None:
        # Another component may have closed the spider after this close was
        # scheduled but before the loop ran it.
        if self._closed:
            return
        try:
            await self.crawler.engine.close_spider_async(reason=reason, error=error)
        except Exception:
            # Keep the latch set: retrying could loop forever. The failure is
            # logged, reported with the spider_error signal, and stored so
            # that it can be inspected later from self.close_failure.
            self.close_failure = Failure()
            logger.exception(
                f"Error while closing the spider ({reason})",
                extra={"spider": self.crawler.spider},
            )
            self.crawler.signals.send_catch_log(
                signal=signals.spider_error,
                failure=self.close_failure,
                response=None,
                spider=self.crawler.spider,
            )
