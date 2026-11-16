"""
Depth Spider Middleware

See documentation in docs/topics/spider-middleware.rst
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Self
from weakref import WeakKeyDictionary, WeakSet

from scrapy.spidermiddlewares.base import BaseSpiderMiddleware
from scrapy.utils.decorators import _warn_spider_arg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from scrapy import Request, Spider
    from scrapy.crawler import Crawler
    from scrapy.http import Response
    from scrapy.statscollectors import StatsCollector


logger = logging.getLogger(__name__)

_SEED_POLICIES = ("legacy", "zero", "preserve")


class DepthMiddleware(BaseSpiderMiddleware):
    """Track the depth of each request within the site being scraped, setting
    ``request.meta["depth"]`` on the requests that it processes.

    .. versionchanged:: VERSION
       Added the :setting:`DEPTH_SEED_POLICY` setting. With values other than
       its default, ``"legacy"``, the depth and priority adjustment of a
       request are applied to a copy of it, and the depth of a response is
       not written into the :attr:`~scrapy.Request.meta` of its request.

    The depth of a request produced by a response is the depth of that
    response plus 1. The :setting:`DEPTH_SEED_POLICY` setting decides the
    depth of :ref:`start requests <start-requests>`, which have no source
    response.

    It can be used to limit the maximum depth to scrape, control request
    priority based on their depth, and things like that, through the
    :setting:`DEPTH_LIMIT`, :setting:`DEPTH_STATS_VERBOSE` and
    :setting:`DEPTH_PRIORITY` settings.

    .. reqmeta:: depth_reset

    depth_reset
    -----------

    .. versionadded:: 2.18.0

    :attr:`~scrapy.Request.meta` key that, set to ``True``, gives a request
    depth 0 instead of the depth of its source response plus 1, e.g. to keep
    :setting:`DEPTH_LIMIT` from applying across a domain change.
    """

    crawler: Crawler

    def __init__(  # pylint: disable=super-init-not-called
        self,
        maxdepth: int,
        stats: StatsCollector,
        verbose_stats: bool = False,
        prio: int = 1,
        seed_policy: str = "legacy",
    ):
        if seed_policy not in _SEED_POLICIES:
            expected = ", ".join(repr(policy) for policy in _SEED_POLICIES)
            raise ValueError(
                f"Invalid DEPTH_SEED_POLICY value {seed_policy!r}:"
                f" expected one of {expected}"
            )
        self.maxdepth = maxdepth
        self.stats = stats
        self.verbose_stats = verbose_stats
        self.prio = prio
        self.seed_policy = seed_policy
        self._ignored_logged = False
        # Results for the requests that this middleware has already processed,
        # keyed by object identity: start requests, which have no response, by
        # themselves, and the rest by response and then request. A request
        # that reaches this middleware more than once, e.g. through both the
        # start and the spider output chains, then keeps the same result
        # instead of being deepened, adjusted or rejected twice.
        self._start_results: WeakKeyDictionary[Request, Request | None] = (
            WeakKeyDictionary()
        )
        self._output_results: WeakKeyDictionary[
            Response, WeakKeyDictionary[Request, Request | None]
        ] = WeakKeyDictionary()
        # The requests that this middleware already produced, per source
        # response (None for start requests). One of them reaching this
        # middleware again from the same source is a compatibility path
        # delivering already processed output, so it keeps its depth and
        # adjusted priority. From a different source it is a request that the
        # spider yielded again, and it gets the depth of that source.
        self._start_produced: WeakSet[Request] = WeakSet()
        self._output_produced: WeakKeyDictionary[Response, WeakSet[Request]] = (
            WeakKeyDictionary()
        )
        # Responses without a depth that were already counted at depth 0.
        self._uncounted_responses: WeakSet[Response] = WeakSet()
        # Priority that a request had before this middleware adjusted it, by
        # object identity. A request that is yielded again, e.g.
        # ``yield response.request``, then gets its priority adjusted by its
        # whole depth once, instead of accumulating adjustments.
        self._base_priorities: WeakKeyDictionary[Request, int] = WeakKeyDictionary()

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        settings = crawler.settings
        o = cls(
            settings.getint("DEPTH_LIMIT"),
            crawler.stats,
            settings.getbool("DEPTH_STATS_VERBOSE"),
            settings.getint("DEPTH_PRIORITY"),
            settings.get("DEPTH_SEED_POLICY"),
        )
        o.crawler = crawler
        return o

    @_warn_spider_arg
    def process_spider_output(
        self,
        response: Response | None,
        result: Iterable[Any],
        spider: Spider | None = None,
    ) -> Iterable[Any]:
        if response is not None:
            self._init_depth(response)
        yield from super().process_spider_output(response, result)

    @_warn_spider_arg
    async def process_spider_output_async(
        self,
        response: Response | None,
        result: AsyncIterator[Any],
        spider: Spider | None = None,
    ) -> AsyncIterator[Any]:
        if response is not None:
            self._init_depth(response)
        async for o in super().process_spider_output_async(response, result):
            yield o

    def _init_depth(self, response: Response) -> None:
        if self.seed_policy == "legacy":
            # base case (depth=0)
            if "depth" not in response.meta:
                response.meta["depth"] = 0
                if self.verbose_stats:
                    self.stats.inc_value("request_depth_count/0")
            return
        meta = self._response_meta(response)
        if meta is not None and "depth" in meta:
            return
        if response in self._uncounted_responses:
            # Already counted, e.g. the output is being processed again after
            # a spider middleware recovered from an exception.
            return
        # The request of the response is left untouched. A response without a
        # depth belongs to a request that did not go through this middleware,
        # and is counted as a depth-0 request, as under the legacy policy.
        self._uncounted_responses.add(response)
        if self.verbose_stats:
            self.stats.inc_value("request_depth_count/0")

    def get_processed_request(
        self, request: Request, response: Response | None
    ) -> Request | None:
        if self.seed_policy == "legacy":
            return self._get_processed_request_legacy(request, response)
        if response is None:
            return self._get_processed_start_request(request)
        produced = self._output_produced.get(response)
        if produced is not None and request in produced:
            return request
        results = self._output_results.setdefault(response, WeakKeyDictionary())
        try:
            return results[request]
        except KeyError:
            pass
        result = self._process_copy(request, self._child_depth(response, request))
        results[request] = result
        if result is not None:
            self._output_produced.setdefault(response, WeakSet()).add(result)
        return result

    def _get_processed_request_legacy(
        self, request: Request, response: Response | None
    ) -> Request | None:
        # Consumed here so that it cannot reach response.meta and, from there,
        # spread to further requests through a meta copy.
        depth_reset = request.meta.pop("depth_reset", False)
        if response is None:
            # start requests
            return request
        depth = 0 if depth_reset else response.meta["depth"] + 1
        request.meta["depth"] = depth
        if self.prio:
            request.priority -= depth * self.prio
        return self._process_depth(request, depth)

    def _get_processed_start_request(self, request: Request) -> Request | None:
        if request in self._start_produced:
            # Already processed by this middleware, e.g. because a
            # compatibility path delivers the start output to it again.
            return request
        try:
            return self._start_results[request]
        except KeyError:
            pass
        if self.seed_policy == "zero":
            depth = 0
        else:
            depth = self._validated_depth(
                request.meta.get("depth", 0), request, "meta['depth']"
            )
        result = self._process_copy(request, depth)
        self._start_results[request] = result
        if result is not None:
            self._start_produced.add(result)
        return result

    def _child_depth(self, response: Response, request: Request) -> int:
        if request.meta.get("depth_reset"):
            return 0
        # A response without a depth belongs to a request that did not go
        # through this middleware; it is treated as a depth-0 response, like
        # _init_depth() does under the legacy policy.
        meta = self._response_meta(response)
        parent_depth = 0 if meta is None else meta.get("depth", 0)
        return (
            self._validated_depth(
                parent_depth, request, "meta['depth'] of the response"
            )
            + 1
        )

    @staticmethod
    def _response_meta(response: Response) -> dict[str, Any] | None:
        """Return the meta of *response*, or None if it is not tied to a
        request and therefore has none."""
        try:
            return response.meta
        except AttributeError:
            return None

    def _validated_depth(self, depth: Any, request: Request, source: str) -> int:
        message = (
            f"DEPTH_SEED_POLICY={self.seed_policy!r} requires"
            f" Request.{source} to be a non-negative integer or missing,"
            f" got {depth!r} for {request}"
        )
        if isinstance(depth, bool) or not isinstance(depth, int):
            raise TypeError(message)
        if depth < 0:
            raise ValueError(message)
        return depth

    def _process_copy(self, request: Request, depth: int) -> Request | None:
        # A copy keeps the depth and the priority adjustment from reaching a
        # request object that the spider owns, that a callback reused, or
        # whose meta it shares.
        meta = dict(request.meta)
        # depth_reset applies to the request that carries it, not to its
        # children, so the copy drops it and it cannot spread to further
        # requests through a meta copy.
        meta.pop("depth_reset", None)
        meta["depth"] = depth
        # parent_id is passed explicitly because replace() otherwise sets it
        # to the id of the request being copied.
        copy = request.replace(meta=meta, parent_id=request.parent_id)
        if self.prio:
            base_priority = self._base_priorities.get(request, request.priority)
            copy.priority = base_priority - depth * self.prio
            self._base_priorities[copy] = base_priority
        return self._process_depth(copy, depth)

    def _process_depth(self, request: Request, depth: int) -> Request | None:
        if self.maxdepth and depth > self.maxdepth:
            if not self._ignored_logged:
                logger.debug(
                    f"Ignoring link (depth > {self.maxdepth}): {request.url}"
                    " - no more ignored links will be shown",
                    extra={"spider": self.crawler.spider},
                )
                self._ignored_logged = True
            self.stats.inc_value("depth/request_ignored_count")
            return None
        if self.verbose_stats:
            self.stats.inc_value(f"request_depth_count/{depth}")
        self.stats.max_value("request_depth_max", depth)
        return request
