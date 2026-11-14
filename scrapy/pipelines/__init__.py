"""
Item pipeline

See documentation in docs/topics/item-pipeline.rst
"""

from __future__ import annotations

import asyncio
import warnings
from collections import deque
from typing import TYPE_CHECKING, Any, Protocol, cast

from twisted.internet.defer import Deferred, DeferredList, FirstError

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.middleware import MiddlewareManager
from scrapy.utils.asyncio import is_asyncio_available
from scrapy.utils.conf import build_component_list
from scrapy.utils.defer import _maybeDeferred_coro, deferred_from_coro, ensure_awaitable
from scrapy.utils.python import global_object_name

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable

    from twisted.python.failure import Failure

    from scrapy import Spider
    from scrapy.crawler import Crawler
    from scrapy.settings import Settings


class ItemProcessorProtocol(Protocol):
    """Protocol for item processor implementations.

    See :setting:`ITEM_PROCESSOR`.
    """

    async def open_spider_async(self) -> None:
        """Get the item processor ready to process items."""

    async def process_item_async(self, item: Any) -> Any:
        """Return the processed *item*, or raise
        :exc:`~scrapy.exceptions.DropItem` to drop it."""

    async def close_spider_async(self) -> None:
        """Release any resource that the item processor is using."""


class ItemPipelineManager(MiddlewareManager):
    component_name = "item pipeline"

    def __init__(self, *middlewares: Any, crawler: Crawler | None = None) -> None:
        super().__init__(*middlewares, crawler=crawler)
        max_pending: int | None = None
        if crawler is not None:
            raw = crawler.settings.get("ITEM_PIPELINE_MAX_PENDING")
            if raw is not None:
                try:
                    max_pending = int(raw)
                except (TypeError, ValueError):
                    max_pending = 0
                if max_pending < 1:
                    raise ValueError(
                        "ITEM_PIPELINE_MAX_PENDING must be a positive integer,"
                        f" got {raw!r}."
                    )
        self._max_pending = max_pending
        self._pending_count = 0
        self._pending_waiters: deque[asyncio.Future[None]] = deque()
        self._drained: asyncio.Future[None] | None = None
        self._closing = False

    @classmethod
    def _get_mwlist_from_settings(cls, settings: Settings) -> list[Any]:
        return build_component_list(
            settings.get_component_priority_dict_with_base("ITEM_PIPELINES")
        )

    def _add_middleware(self, mw: Any) -> None:
        if hasattr(mw, "open_spider"):
            self.methods["open_spider"].append(mw.open_spider)
            self._check_mw_method_spider_arg(mw.open_spider)
        if hasattr(mw, "close_spider"):
            self.methods["close_spider"].appendleft(mw.close_spider)
            self._check_mw_method_spider_arg(mw.close_spider)
        if hasattr(mw, "process_item"):
            self.methods["process_item"].append(mw.process_item)
            self._check_mw_method_spider_arg(mw.process_item)

    def process_item(self, item: Any, spider: Spider) -> Deferred[Any]:
        warnings.warn(
            f"{global_object_name(type(self))}.process_item() is deprecated, use process_item_async() instead.",
            category=ScrapyDeprecationWarning,
            stacklevel=2,
        )
        self._set_compat_spider(spider)
        return deferred_from_coro(self.process_item_async(item))

    async def process_item_async(self, item: Any) -> Any:
        if self._max_pending is None:
            return await self._process_chain(
                "process_item", item, add_spider=True, warn_deferred=True
            )
        await self._acquire_pending_slot()
        try:
            return await self._process_chain(
                "process_item", item, add_spider=True, warn_deferred=True
            )
        finally:
            self._release_pending_slot()

    async def _acquire_pending_slot(self) -> None:
        """Wait until fewer than :setting:`ITEM_PIPELINE_MAX_PENDING` items
        are being processed, in FIFO order."""
        assert self._max_pending is not None  # typing
        if self._closing:
            raise RuntimeError(
                "The item pipeline is closed and cannot process more items."
            )
        if not self._pending_waiters and self._pending_count < self._max_pending:
            self._pending_count += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        self._pending_waiters.append(waiter)
        try:
            await waiter
        except BaseException:
            if waiter.cancelled():
                # Cancelled while queued, before a slot was granted.
                try:
                    self._pending_waiters.remove(waiter)
                except ValueError:
                    pass
            elif waiter.exception() is not None:
                # Failed by close_spider_async(), no slot was granted.
                pass
            else:
                # A slot was granted but the waiting coroutine was cancelled
                # before resuming: hand the slot to the next waiter.
                self._release_pending_slot()
            raise

    def _release_pending_slot(self) -> None:
        while self._pending_waiters:
            waiter = self._pending_waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return
        self._pending_count -= 1
        if (
            self._pending_count == 0
            and self._drained is not None
            and not self._drained.done()
        ):
            self._drained.set_result(None)

    def _get_dfd(
        self,
        method: Callable[..., Coroutine[Any, Any, None] | Deferred[None] | None],
    ) -> Deferred[None]:
        if method in self._mw_methods_requiring_spider:
            return _maybeDeferred_coro(method, True, self._spider)
        return _maybeDeferred_coro(method, True)

    @staticmethod
    def _eb(failure: Failure) -> Failure:
        assert isinstance(failure.value, FirstError)
        return failure.value.subFailure

    def _process_parallel_dfd(self, methodname: str) -> Deferred[list[None]]:
        methods = cast(
            "Iterable[Callable[..., Coroutine[Any, Any, None] | Deferred[None] | None]]",
            self.methods[methodname],
        )

        dfds = [self._get_dfd(m) for m in methods]
        d: Deferred[list[tuple[bool, None]]] = DeferredList(
            dfds, fireOnOneErrback=True, consumeErrors=True
        )
        d2: Deferred[list[None]] = d.addCallback(lambda r: [x[1] for x in r])

        d2.addErrback(self._eb)
        return d2

    def get_awaitable(
        self,
        method: Callable[..., Coroutine[Any, Any, None] | Deferred[None] | None],
    ) -> Awaitable[None]:
        if method in self._mw_methods_requiring_spider:
            result = method(self._spider)
        else:
            result = method()
        return ensure_awaitable(result, _warn=global_object_name(method))

    async def _process_parallel_asyncio(self, methodname: str) -> list[None]:
        methods = cast(
            "Iterable[Callable[..., Coroutine[Any, Any, None] | Deferred[None] | None]]",
            self.methods[methodname],
        )
        if not methods:
            return []

        awaitables = [self.get_awaitable(m) for m in methods]
        await asyncio.gather(*awaitables)
        return [None for _ in methods]

    async def _process_parallel(self, methodname: str) -> list[None]:
        if is_asyncio_available():
            return await self._process_parallel_asyncio(methodname)
        return await self._process_parallel_dfd(methodname)

    def open_spider(self, spider: Spider) -> Deferred[list[None]]:
        warnings.warn(
            f"{global_object_name(type(self))}.open_spider() is deprecated, use open_spider_async() instead.",
            category=ScrapyDeprecationWarning,
            stacklevel=2,
        )
        self._set_compat_spider(spider)
        return deferred_from_coro(self._process_parallel("open_spider"))

    async def open_spider_async(self) -> None:
        self._closing = False
        await self._process_parallel("open_spider")

    def close_spider(self, spider: Spider) -> Deferred[list[None]]:
        warnings.warn(
            f"{global_object_name(type(self))}.close_spider() is deprecated, use close_spider_async() instead.",
            category=ScrapyDeprecationWarning,
            stacklevel=2,
        )
        self._set_compat_spider(spider)
        return deferred_from_coro(self._process_parallel("close_spider"))

    async def close_spider_async(self) -> None:
        if self._max_pending is not None:
            self._closing = True
            # Items waiting for a slot must not enter the pipeline once it
            # is closed.
            while self._pending_waiters:
                waiter = self._pending_waiters.popleft()
                if not waiter.done():
                    waiter.set_exception(
                        RuntimeError(
                            "The item pipeline was closed before this item"
                            " could be processed."
                        )
                    )
            # Items already in the pipeline finish processing first.
            if self._pending_count:
                self._drained = asyncio.get_running_loop().create_future()
                await self._drained
        await self._process_parallel("close_spider")
