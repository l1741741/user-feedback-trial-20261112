"""
This is a middleware to respect robots.txt policies. To activate it you must
enable this middleware and enable the ROBOTSTXT_OBEY setting.

"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from twisted.internet.defer import CancelledError, Deferred

from scrapy import signals
from scrapy.exceptions import CloseSpider, IgnoreRequest, NotConfigured
from scrapy.http import Request, Response
from scrapy.http.request import NO_CALLBACK
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.defer import deferred_from_coro, maybe_deferred_to_future
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import build_from_crawler, load_object

if TYPE_CHECKING:
    from collections.abc import Callable

    from scrapy import Spider
    from scrapy.crawler import Crawler
    from scrapy.robotstxt import RobotParser
    from scrapy.statscollectors import StatsCollector


logger = logging.getLogger(__name__)


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _robots_origin(request: Request) -> tuple[str, str, int]:
    """Return the normalized origin of *request* as a
    ``(scheme, hostname, port)`` tuple.

    The scheme and the hostname are lower-cased, an omitted or default port
    collapses onto the scheme's default port (80 for HTTP, 443 for HTTPS), so
    requests that share an origin share their robots.txt rules while different
    schemes, hostnames or non-default ports stay isolated.
    """
    parsed = urlparse_cached(request)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None:
        port = _DEFAULT_PORTS.get(scheme, 80)
    return scheme, hostname, port


def _robots_url(origin: tuple[str, str, int]) -> str:
    """Build the canonical robots.txt URL for *origin*.

    Unlike string-concatenating the request's netloc, this never carries
    userinfo (user name and password), a query string or a fragment, and keeps
    IPv6 addresses wrapped in brackets.
    """
    scheme, hostname, port = origin
    if ":" in hostname:
        host = f"[{hostname}]"
    else:
        host = hostname
    default_port = _DEFAULT_PORTS.get(scheme, 80)
    if port == default_port:
        return f"{scheme}://{host}/robots.txt"
    return f"{scheme}://{host}:{port}/robots.txt"


@dataclass
class _CachedRobots:
    """A completed robots.txt lookup in the bounded cache."""

    # None means the download failed and the result is negatively cached.
    parser: RobotParser | None
    # monotonic timestamp at which the entry expires
    expires_at: float


@dataclass
class _RobotsFetch:
    """A single shared robots.txt download and parse for one origin.

    Every caller to the same origin while this is in flight gets its own
    :class:`~twisted.internet.defer.Deferred` waiter, so cancelling one page
    request (and its waiter) never cancels the shared download or the other
    waiters.
    """

    origin: tuple[str, str, int]
    robots_url: str
    # True when an explicit robots.txt page request is doing the download, in
    # which case process_response()/process_exception() own its completion.
    external: bool = False
    waiters: list[Deferred[RobotParser | None]] = field(default_factory=list)

    def add_waiter(self) -> Deferred[RobotParser | None]:
        # The no-op canceller makes waiter cancellation local: Deferred.cancel()
        # fires this waiter with CancelledError without touching the shared
        # fetch or the other waiters, and Deferred.asFuture()'s cancellation
        # hook therefore cannot propagate up to the shared download either.
        waiter: Deferred[RobotParser | None] = Deferred(lambda d: None)
        self.waiters.append(waiter)
        return waiter

    def fire(self, parser: RobotParser | None) -> None:
        waiters, self.waiters = self.waiters, []
        for waiter in waiters:
            if not waiter.called:
                waiter.callback(parser)


class RobotsTxtMiddleware:
    """This middleware filters out requests forbidden by the robots.txt
    exclusion standard.

    To make sure Scrapy respects robots.txt make sure the middleware is enabled
    and the :setting:`ROBOTSTXT_OBEY` setting is enabled.

    The :setting:`ROBOTSTXT_USER_AGENT` setting can be used to specify the
    user agent string to use for matching in the robots.txt_ file. If it
    is ``None``, the User-Agent header you are sending with the request or the
    :setting:`USER_AGENT` setting (in that order) will be used for determining
    the user agent to use in the robots.txt_ file.

    This middleware has to be combined with a robots.txt_ parser.

    Scrapy ships with support for the following robots.txt_ parsers:

    * :ref:`Protego <protego-parser>` (default)
    * :ref:`RobotFileParser <python-robotfileparser>`
    * :ref:`Robotexclusionrulesparser <rerp-parser>`

    You can change the robots.txt_ parser with the :setting:`ROBOTSTXT_PARSER`
    setting. Or you can also :ref:`implement support for a new parser
    <support-for-new-robots-parser>`.

    Parsed rules are cached per origin (scheme, host and port). The cache only
    holds completed results and is bounded by
    :setting:`ROBOTSTXT_CACHE_MAXSIZE`; in-flight downloads are shared by every
    request to the same origin and are never evicted. Successful results are
    reused for :setting:`ROBOTSTXT_CACHE_TTL` seconds, while download failures,
    which keep the fail-open behavior, are only negatively cached for
    :setting:`ROBOTSTXT_FAILURE_TTL` seconds.

    If no :ref:`start request <start-requests>` can be crawled, and robots.txt
    rules denied at least one of them, the crawl stops with the
    ``robotstxt_denied`` :stat:`finish_reason`, as long as
    :class:`~scrapy.spidermiddlewares.start.StartSpiderMiddleware` is enabled.
    """

    DOWNLOAD_PRIORITY: int = 1000

    def __init__(self, crawler: Crawler):
        if not crawler.settings.getbool("ROBOTSTXT_OBEY"):
            raise NotConfigured
        self._start_request_crawled = False
        self._start_request_denied = False
        self._default_useragent: str = crawler.settings["USER_AGENT"]
        self._robotstxt_useragent: str | None = crawler.settings["ROBOTSTXT_USER_AGENT"]
        self.crawler: Crawler = crawler
        self._stats: StatsCollector = crawler.stats
        self._cache_maxsize: int = self._read_positive_int_setting(
            crawler, "ROBOTSTXT_CACHE_MAXSIZE", 256
        )
        self._cache_ttl: float = self._read_ttl_setting(
            crawler, "ROBOTSTXT_CACHE_TTL", 3600.0
        )
        self._failure_ttl: float = self._read_ttl_setting(
            crawler, "ROBOTSTXT_FAILURE_TTL", 30.0
        )
        # Only completed lookups live here, most recently used last. In-flight
        # downloads have a separate store and are never evicted from it.
        self._cache: OrderedDict[tuple[str, str, int], _CachedRobots] = (
            OrderedDict()
        )
        self._inflight: dict[tuple[str, str, int], _RobotsFetch] = {}
        # Indirection over the monotonic clock so TTL behavior can be tested
        # deterministically without sleeping.
        self._monotonic: Callable[[], float] = time.monotonic
        self._parserimpl: type[RobotParser] = load_object(
            crawler.settings.get("ROBOTSTXT_PARSER")
        )

        # check if parser dependencies are met, this should throw an error otherwise.
        build_from_crawler(self._parserimpl, self.crawler, b"")

        crawler.signals.connect(
            self._response_received, signal=signals.response_received
        )
        crawler.signals.connect(self._spider_idle, signal=signals.spider_idle)

    @staticmethod
    def _read_positive_int_setting(
        crawler: Crawler, name: str, default: int
    ) -> int:
        value = crawler.settings.getint(name, default)
        if value <= 0:
            logger.warning(
                "Ignoring invalid %(name)s=%(value)r: it must be a positive "
                "integer, using %(default)d instead.",
                {"name": name, "value": value, "default": default},
                extra={"spider": crawler.spider},
            )
            return default
        return value

    @staticmethod
    def _read_ttl_setting(
        crawler: Crawler, name: str, default: float
    ) -> float:
        value = crawler.settings.getfloat(name, default)
        if value < 0:
            logger.warning(
                "Ignoring invalid %(name)s=%(value)r: it must be 0 or a "
                "positive number, using %(default)s instead.",
                {"name": name, "value": value, "default": default},
                extra={"spider": crawler.spider},
            )
            return default
        return value

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler)

    def _response_received(self, request: Request) -> None:
        if request.meta.get("is_start_request"):
            self._start_request_crawled = True

    def _spider_idle(self) -> None:
        if self._start_request_crawled or not self._start_request_denied:
            return
        logger.error(
            "Stopping the crawl: no start request could be crawled, and at "
            "least one of them was rejected based on robots.txt rules. See "
            "https://docs.scrapy.org/en/latest/topics/downloader-middleware.html#topics-dlmw-robots"
        )
        raise CloseSpider("robotstxt_denied")

    @_warn_spider_arg
    async def process_request(
        self, request: Request, spider: Spider | None = None
    ) -> None:
        if request.meta.get("dont_obey_robotstxt"):
            return
        if request.url.startswith("data:") or request.url.startswith("file:"):
            return
        origin = _robots_origin(request)
        if (
            origin not in self._inflight
            and self._is_robotstxt_request(request)
        ):
            # Let this request download normally and use its own response as
            # this origin's robots.txt, instead of triggering a second,
            # separate download of the same URL.
            fetch = _RobotsFetch(origin, request.url, external=True)
            self._inflight[origin] = fetch
            self._stats.inc_value("robotstxt/request_count")
            return
        rp = await self.robot_parser(request, origin)
        self.process_request_2(rp, request)

    @_warn_spider_arg
    async def process_response(
        self, request: Request, response: Response, spider: Spider | None = None
    ) -> Response:
        origin = _robots_origin(request)
        fetch = self._inflight.get(origin)
        if (
            fetch is not None
            and fetch.external
            and self._is_robotstxt_request(request)
        ):
            await self._parse_robots(response, fetch, request)
        return response

    @_warn_spider_arg
    async def process_exception(
        self, request: Request, exception: Exception, spider: Spider | None = None
    ) -> None:
        origin = _robots_origin(request)
        fetch = self._inflight.get(origin)
        if (
            fetch is not None
            and fetch.external
            and self._is_robotstxt_request(request)
        ):
            self._robots_error(exception, fetch, request)

    @staticmethod
    def _is_robotstxt_request(request: Request) -> bool:
        # robot_parser() handles its own internal robots.txt request directly,
        # without going through process_response()/process_exception().
        if request.meta.get("dont_obey_robotstxt"):
            return False
        return request.url == _robots_url(_robots_origin(request))

    def process_request_2(self, rp: RobotParser | None, request: Request) -> None:
        if rp is None:
            return

        useragent: str | bytes | None = self._robotstxt_useragent
        if not useragent:
            useragent = request.headers.get(b"User-Agent", self._default_useragent)
            assert useragent is not None
        if not rp.allowed(request.url, useragent):
            logger.debug(
                "Forbidden by robots.txt: %(request)s",
                {"request": request},
                extra={"spider": self.crawler.spider},
            )
            self._stats.inc_value("robotstxt/forbidden")
            if request.meta.get("is_start_request"):
                self._start_request_denied = True
            raise IgnoreRequest("Forbidden by robots.txt")

    async def robot_parser(
        self,
        request: Request,
        origin: tuple[str, str, int] | None = None,
    ) -> RobotParser | None:
        if origin is None:
            origin = _robots_origin(request)

        cached = self._cache.get(origin)
        if cached is not None:
            if cached.expires_at > self._monotonic():
                self._cache.move_to_end(origin)
                return cached.parser
            # Expired entries are dropped lazily; they are completed results, so
            # evicting them never touches an in-flight download.
            del self._cache[origin]

        fetch = self._inflight.get(origin)
        if fetch is None:
            fetch = _RobotsFetch(origin, _robots_url(origin))
            self._inflight[origin] = fetch
            self._stats.inc_value("robotstxt/request_count")
            driver = deferred_from_coro(self._download_robots(fetch, request))
            # The waiter Deferreds, not this driver, deliver the result to the
            # callers. The driver never raises in normal operation: download
            # failures are handled in _download_robots(). A CancelledError only
            # reaches here when the event loop shuts down, in which case the
            # whole loop is going away; swallow it so it can't surface as an
            # unhandled Deferred error.
            driver.addErrback(self._swallow_driver_cancellation)
        # Otherwise an existing fetch owns the download, either an internal one
        # or the explicit robots.txt page request, whose response comes back
        # through process_response()/process_exception(). This caller only
        # waits. The waiter's no-op canceller makes a cancellation local to this
        # caller: it can neither cancel the shared download nor the other
        # waiters.
        waiter = fetch.add_waiter()
        return await maybe_deferred_to_future(waiter)

    @staticmethod
    def _swallow_driver_cancellation(failure: Any) -> None:
        failure.trap(CancelledError)

    def _build_robots_request(
        self, fetch: _RobotsFetch, request: Request
    ) -> Request:
        meta: dict[str, Any] = {
            "dont_obey_robotstxt": True,
            "is_robotstxt_request": True,
        }
        if "download_handler" in request.meta:
            meta["download_handler"] = request.meta["download_handler"]
        # A fresh Request: no cookies, no headers (no Cookie or Authorization
        # from the page request), no userinfo, query string or fragment.
        return Request(
            fetch.robots_url,
            priority=self.DOWNLOAD_PRIORITY,
            meta=meta,
            callback=NO_CALLBACK,
        )

    async def _download_robots(
        self, fetch: _RobotsFetch, request: Request
    ) -> None:
        robotsreq = self._build_robots_request(fetch, request)
        try:
            response = await self.crawler.engine.download_async(robotsreq)
            await self._parse_robots(response, fetch, request)
        except Exception as e:
            self._robots_error(e, fetch, request)
        except BaseException:
            # e.g. the event loop shutting down: still release every waiter and
            # clear the in-flight slot so nothing can stay pending forever.
            self._finish_fetch(fetch, None, cache=False)
            raise

    async def _parse_robots(
        self,
        response: Response,
        fetch: _RobotsFetch,
        request: Request,
    ) -> None:
        self._stats.inc_value("robotstxt/response_count")
        self._stats.inc_value(f"robotstxt/response_status_count/{response.status}")
        rp = build_from_crawler(self._parserimpl, self.crawler, response.body)
        await self.crawler.signals.send_catch_log_async(
            signal=signals.robots_parsed,
            robotparser=rp,
            request=request,
            response=response,
        )
        self._finish_fetch(fetch, rp, cache=self._cache_ttl > 0)

    def _robots_error(
        self, exc: Exception, fetch: _RobotsFetch, request: Request
    ) -> None:
        if not isinstance(exc, IgnoreRequest):
            logger.error(
                f"Error downloading {request}",
                exc_info=exc,
                extra={"spider": self.crawler.spider},
            )
            key = f"robotstxt/exception_count/{type(exc)}"
            self._stats.inc_value(key)
        # Fail open, and only negatively cache for the (shorter) failure TTL.
        self._finish_fetch(fetch, None, cache=self._failure_ttl > 0)

    def _finish_fetch(
        self,
        fetch: _RobotsFetch,
        parser: RobotParser | None,
        *,
        cache: bool,
    ) -> None:
        # A fetch object is only ever finished once, and only its own
        # completion can replace its in-flight slot, so a late callback of an
        # old download can never overwrite a newer cache entry.
        if self._inflight.get(fetch.origin) is not fetch:
            return
        del self._inflight[fetch.origin]
        if cache:
            ttl = self._cache_ttl if parser is not None else self._failure_ttl
            entry = _CachedRobots(
                parser=parser,
                expires_at=self._monotonic() + ttl,
            )
            self._store_cached(fetch.origin, entry)
        fetch.fire(parser)

    def _store_cached(
        self,
        origin: tuple[str, str, int],
        entry: _CachedRobots,
    ) -> None:
        self._cache[origin] = entry
        self._cache.move_to_end(origin)
        while len(self._cache) > self._cache_maxsize:
            self._cache.popitem(last=False)
