from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from scrapy import Request, signals
from scrapy.core.downloader import Downloader
from scrapy.core.downloader.handlers.datauri import DataURIDownloadHandler
from scrapy.core.downloader.middleware import DownloaderMiddlewareManager
from scrapy.downloadermiddlewares.downloadtimeout import DownloadTimeoutMiddleware
from scrapy.downloadermiddlewares.redirect import RedirectMiddleware
from scrapy.downloadermiddlewares.retry import get_retry_request
from scrapy.exceptions import DownloadCancelledError, DownloadDeadlineExceededError
from scrapy.http import Response
from scrapy.spiders import Spider
from scrapy.utils.defer import maybe_deferred_to_future
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import Callable

    from scrapy.crawler import Crawler


class FakeClock:
    """A controllable monotonic clock."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def get_request_spider_mw(settings: dict[str, Any] | None = None):
    crawler = get_crawler(Spider, settings)
    spider = crawler._create_spider("foo")
    request = Request("http://scrapytest.org/")
    return request, spider, build_from_crawler(DownloadTimeoutMiddleware, crawler)


def build_mw(
    settings: dict[str, Any] | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[Crawler, DownloadTimeoutMiddleware]:
    crawler = get_crawler(Spider, settings)
    mw = build_from_crawler(DownloadTimeoutMiddleware, crawler, clock=clock)
    mw.spider_opened(crawler._create_spider("foo"))
    return crawler, mw


def test_default_download_timeout():
    req, spider, mw = get_request_spider_mw()
    mw.spider_opened(spider)
    assert mw.process_request(req) is None
    assert req.meta.get("download_timeout") == 180


def test_string_download_timeout():
    req, spider, mw = get_request_spider_mw({"DOWNLOAD_TIMEOUT": "20.1"})
    mw.spider_opened(spider)
    assert mw.process_request(req) is None
    assert req.meta.get("download_timeout") == 20.1


def test_setting_has_download_timeout():
    req, spider, mw = get_request_spider_mw({"DOWNLOAD_TIMEOUT": 2})
    mw.spider_opened(spider)
    assert mw.process_request(req) is None
    assert req.meta.get("download_timeout") == 2


def test_request_has_download_timeout():
    req, spider, mw = get_request_spider_mw({"DOWNLOAD_TIMEOUT": 2})
    mw.spider_opened(spider)
    req.meta["download_timeout"] = 1
    assert mw.process_request(req) is None
    assert req.meta.get("download_timeout") == 1


def test_zero_download_timeout():
    req, spider, mw = get_request_spider_mw({"DOWNLOAD_TIMEOUT": 0})
    mw.spider_opened(spider)
    assert mw.process_request(req) is None
    assert req.meta.get("download_timeout") is None


class TestDeadlineValidation:
    @pytest.mark.parametrize(
        "value",
        [
            -1,
            -0.5,
            "abc",
            "",
            [],
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            False,
            1e9,
            1_700_000_000,
            1_700_000_000.5,
        ],
    )
    def test_invalid_setting(self, value: Any) -> None:
        """Negative values do not mean unlimited, and wall-clock timestamps,
        booleans and non-numbers are rejected before the crawl starts."""
        crawler = get_crawler(Spider, {"DOWNLOAD_DEADLINE": value})
        with pytest.raises(ValueError, match="DOWNLOAD_DEADLINE"):
            build_from_crawler(DownloadTimeoutMiddleware, crawler)

    @pytest.mark.parametrize(
        ("value", "expected"), [(0, 0.0), (10, 10.0), ("7.5", 7.5)]
    )
    def test_valid_setting(self, value: Any, expected: float) -> None:
        crawler = get_crawler(Spider, {"DOWNLOAD_DEADLINE": value})
        mw = build_from_crawler(DownloadTimeoutMiddleware, crawler)
        assert mw._deadline == expected

    def test_invalid_setting_fails_middleware_manager(self) -> None:
        crawler = get_crawler(Spider, {"DOWNLOAD_DEADLINE": -1})
        with pytest.raises(ValueError, match="DOWNLOAD_DEADLINE"):
            build_from_crawler(DownloaderMiddlewareManager, crawler)

    @pytest.mark.parametrize(
        "value", [1_700_000_000, -1, True, float("nan"), "1700000000"]
    )
    def test_invalid_absolute_meta(self, value: Any, clock: FakeClock) -> None:
        """A deadline in another clock domain is rejected instead of becoming
        a deadline that never expires or that expired long ago."""
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        req = Request("http://example.com", meta={"_download_deadline": value})
        with pytest.raises(ValueError, match="_download_deadline"):
            mw.process_request(req)

    def test_deadline_beyond_budget_rejected(self, clock: FakeClock) -> None:
        """A deadline that no budget of this crawl could have produced, e.g.
        one restored from a disk queue written by another process, is rejected
        instead of extending the budget."""
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        req = Request("http://example.com", meta={"_download_deadline": 20.0})
        with pytest.raises(ValueError, match="further into the future"):
            mw.process_request(req)

    @pytest.mark.parametrize("budget", [0.1, 1, 10, 180, 86400])
    def test_stamped_deadline_accepted(self, budget: float) -> None:
        """A deadline stamped by this crawl is accepted even where float
        rounding of the budget makes it look slightly too far ahead."""
        clock = FakeClock(now=1e8)
        _, mw = build_mw({"DOWNLOAD_DEADLINE": budget}, clock)
        req = Request("http://example.com")
        assert mw.process_request(req) is None
        assert mw.process_request(req) is None

    @pytest.mark.parametrize(
        "value", [1_700_000_000, -1, True, float("nan"), "abc", None]
    )
    def test_invalid_clock_domain(self, value: Any) -> None:
        def bad_clock() -> Any:
            return value

        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, bad_clock)
        with pytest.raises(ValueError, match="clock reading"):
            mw.process_request(Request("http://example.com"))


class TestDeadline:
    def test_disabled_by_default(self, clock: FakeClock) -> None:
        _, mw = build_mw(clock=clock)
        req = Request("http://example.com")
        assert mw.process_request(req) is None
        assert "_download_deadline" not in req.meta
        assert req.meta["download_timeout"] == 180

    def test_stamps_absolute_deadline(self, clock: FakeClock) -> None:
        clock.now = 100.0
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        req = Request("http://example.com")
        assert mw.process_request(req) is None
        assert req.meta["_download_deadline"] == 110.0
        assert req.meta["download_timeout"] == 10

    def test_lower_than_download_timeout(self, clock: FakeClock) -> None:
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10, "DOWNLOAD_TIMEOUT": 4}, clock)
        req = Request("http://example.com")
        assert mw.process_request(req) is None
        assert req.meta["download_timeout"] == 4

    def test_zero_download_timeout(self, clock: FakeClock) -> None:
        """DOWNLOAD_TIMEOUT=0 means no per-download timeout, so the deadline
        still caps the download."""
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10, "DOWNLOAD_TIMEOUT": 0}, clock)
        req = Request("http://example.com")
        assert mw.process_request(req) is None
        assert req.meta["download_timeout"] == 10

    def test_zero_budget(self, clock: FakeClock) -> None:
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 0}, clock)
        with pytest.raises(DownloadDeadlineExceededError):
            mw.process_request(Request("http://example.com"))

    @pytest.mark.parametrize(
        ("meta_timeout", "expected"),
        [
            (1, 1),
            (100, 10),
            ("2.5", 10),
            (0, 10),
            (-1, 10),
            (float("nan"), 10),
            (None, 10),
            (True, 10),
        ],
    )
    def test_minimum_with_meta_timeout(
        self, meta_timeout: Any, expected: Any, clock: FakeClock
    ) -> None:
        """The lower of the download_timeout request meta key and the remaining
        budget applies; a value that does not limit the download does not win
        over the budget."""
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        req = Request("http://example.com", meta={"download_timeout": meta_timeout})
        assert mw.process_request(req) is None
        assert req.meta["download_timeout"] == expected

    def test_retry_inherits_deadline(self, clock: FakeClock) -> None:
        crawler, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        spider = crawler._create_spider("foo")
        req = Request("http://example.com")
        assert mw.process_request(req) is None
        clock.advance(4)
        retry = get_retry_request(req, spider=spider)
        assert retry is not None
        assert retry.meta["_download_deadline"] == 10
        assert mw.process_request(retry) is None
        assert retry.meta["download_timeout"] == 6
        # The request the retry derives from is left as it was.
        assert req.meta["download_timeout"] == 10

    def test_redirect_inherits_deadline(self, clock: FakeClock) -> None:
        crawler, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        redirect_mw = build_from_crawler(RedirectMiddleware, crawler)
        req = Request("http://example.com/a")
        assert mw.process_request(req) is None
        clock.advance(4)
        response = Response(
            req.url, status=302, headers={"Location": "http://other.example/b"}
        )
        redirected = redirect_mw.process_response(req, response)
        assert isinstance(redirected, Request)
        # A redirect to another origin still keeps the deadline.
        assert mw.process_request(redirected) is None
        assert redirected.meta["_download_deadline"] == 10
        assert redirected.meta["download_timeout"] == 6

    def test_exceeded(self, clock: FakeClock) -> None:
        crawler, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        spider = crawler._create_spider("foo")
        req = Request("http://example.com")
        assert mw.process_request(req) is None
        clock.advance(11)
        retry = get_retry_request(req, spider=spider)
        assert retry is not None
        with pytest.raises(DownloadDeadlineExceededError, match="deadline"):
            mw.process_request(retry)

    def test_independent_chains(self, clock: FakeClock) -> None:
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        first = Request("http://example.com/1")
        assert mw.process_request(first) is None
        clock.advance(8)
        second = Request("http://example.com/2")
        assert mw.process_request(second) is None
        assert second.meta["_download_deadline"] == 18
        assert second.meta["download_timeout"] == 10

    def test_sibling_requests_get_own_deadline(self, clock: FakeClock) -> None:
        """Following a response builds requests with their own meta, so the
        time already spent by the request of the response is not counted."""
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        parent = Request("http://example.com")
        response = Response(parent.url, request=parent)
        assert mw.process_request(parent) is None
        clock.advance(5)
        followed = response.follow("/child")
        assert mw.process_request(followed) is None
        assert followed.meta["_download_deadline"] == 15

    def test_check_download_deadline_caps_remaining(self, clock: FakeClock) -> None:
        """The check right before the download accounts for queue time."""
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        req = Request("http://example.com")
        assert mw.process_request(req) is None
        clock.advance(7)
        mw._check_download_deadline(req)
        assert req.meta["download_timeout"] == 3

    def test_check_download_deadline_expires(self, clock: FakeClock) -> None:
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        req = Request("http://example.com")
        assert mw.process_request(req) is None
        clock.advance(10)
        with pytest.raises(DownloadDeadlineExceededError):
            mw._check_download_deadline(req)

    def test_repeated_processing_keeps_deadline(self, clock: FakeClock) -> None:
        """Processing the same request again, e.g. after it is rescheduled,
        does not restart its countdown."""
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        req = Request("http://example.com")
        assert mw.process_request(req) is None
        clock.advance(6)
        assert mw.process_request(req) is None
        assert req.meta["_download_deadline"] == 10
        assert req.meta["download_timeout"] == 4

    def test_retry_wait_counts_against_deadline(self, clock: FakeClock) -> None:
        """Time spent waiting before a retry is downloaded is counted."""
        crawler, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        spider = crawler._create_spider("foo")
        req = Request("http://example.com")
        assert mw.process_request(req) is None
        retry = get_retry_request(req, spider=spider)
        assert retry is not None
        # The retry is scheduled, waits, and only then reaches the downloader.
        clock.advance(9)
        assert mw.process_request(retry) is None
        assert retry.meta["download_timeout"] == 1

    def test_check_download_deadline_without_stamp(self, clock: FakeClock) -> None:
        """A request that skipped process_request() is left alone."""
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        req = Request("http://example.com")
        mw._check_download_deadline(req)
        assert "download_timeout" not in req.meta

    def test_repeated_checks_count_once(self, clock: FakeClock) -> None:
        """Repeated checks of the same request do not inflate the stat."""
        crawler, mw = build_mw({"DOWNLOAD_DEADLINE": 0}, clock)
        req = Request("http://example.com")
        with pytest.raises(DownloadDeadlineExceededError):
            mw.process_request(req)
        clock.advance(5)
        for _ in range(3):
            with pytest.raises(DownloadDeadlineExceededError):
                mw._check_download_deadline(req)
        assert crawler.stats is not None
        assert crawler.stats.get_value("download_deadline/exceeded") == 1

    def test_no_stats(self, clock: FakeClock) -> None:
        """Without a stats collector, expired requests are still rejected."""
        mw = DownloadTimeoutMiddleware(deadline=0, clock=clock)
        with pytest.raises(DownloadDeadlineExceededError):
            mw.process_request(Request("http://example.com"))

    def test_spider_closed_clears_state(self, clock: FakeClock) -> None:
        crawler, mw = build_mw({"DOWNLOAD_DEADLINE": 0}, clock)
        req = Request("http://example.com")
        with pytest.raises(DownloadDeadlineExceededError):
            mw.process_request(req)
        assert len(mw._expired) == 1
        mw.spider_closed()
        assert not mw._expired
        assert crawler.stats is not None
        assert crawler.stats.get_value("download_deadline/exceeded") == 1

    def test_another_chain_not_affected(self, clock: FakeClock) -> None:
        """One expired chain does not change the budget of another one."""
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        expired = Request("http://example.com/expired")
        assert mw.process_request(expired) is None
        clock.advance(11)
        other = Request("http://example.com/other")
        assert mw.process_request(other) is None
        assert other.meta["download_timeout"] == 10

    def test_copy_branches_share_deadline(self, clock: FakeClock) -> None:
        """Every copy of a request keeps the deadline of the chain, and
        processing one branch does not change another."""
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        req = Request("http://example.com")
        assert mw.process_request(req) is None
        left, right = req.copy(), req.copy()
        clock.advance(3)
        assert mw.process_request(left) is None
        assert left.meta["download_timeout"] == 7
        clock.advance(3)
        assert mw.process_request(right) is None
        assert right.meta["download_timeout"] == 4
        assert left.meta["download_timeout"] == 7
        assert left.meta["_download_deadline"] == 10
        assert right.meta["_download_deadline"] == 10

    @pytest.mark.parametrize("meta", [{}, {"download_timeout": 1}])
    def test_does_not_change_unrelated_requests(
        self, meta: dict[str, Any], clock: FakeClock
    ) -> None:
        """Processing a request does not change the meta of a request that is
        built from it but does not inherit its meta, e.g. one built with
        Response.follow()."""
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        parent = Request("http://example.com", meta=meta)
        response = Response(parent.url, request=parent)
        assert mw.process_request(parent) is None
        clock.advance(5)
        child = response.follow("/child", meta=dict(meta))
        assert mw.process_request(child) is None
        assert parent.meta["_download_deadline"] == 10
        assert child.meta["_download_deadline"] == 15
        assert parent.meta.get("download_timeout") == meta.get("download_timeout", 10)

    def test_does_not_change_spider_settings(self, clock: FakeClock) -> None:
        crawler = get_crawler(Spider, {"DOWNLOAD_DEADLINE": 10})
        spider = crawler._create_spider("foo")
        mw = build_from_crawler(DownloadTimeoutMiddleware, crawler, clock=clock)
        mw.spider_opened(spider)
        mw.process_request(Request("http://example.com"))
        assert not hasattr(spider, "download_deadline")
        assert crawler.settings.get("DOWNLOAD_DEADLINE") == 10

    def test_header_and_body_unchanged(self, clock: FakeClock) -> None:
        _, mw = build_mw({"DOWNLOAD_DEADLINE": 10}, clock)
        req = Request(
            "http://example.com",
            method="POST",
            headers={"X-Test": "1"},
            body=b"payload",
            cookies={"a": "b"},
            cb_kwargs={"k": "v"},
        )
        headers = dict(req.headers)
        cookies = req.cookies
        assert mw.process_request(req) is None
        assert dict(req.headers) == headers
        assert req.body == b"payload"
        assert req.method == "POST"
        assert req.cb_kwargs == {"k": "v"}
        assert req.cookies == cookies


class TestThroughManager:
    """Deadline behavior through the downloader middleware manager and a fake
    download function."""

    def get_mwman(
        self, clock: FakeClock, settings: dict[str, Any] | None = None
    ) -> tuple[Crawler, DownloaderMiddlewareManager]:
        crawler = get_crawler(Spider, settings or {"DOWNLOAD_DEADLINE": 10})
        crawler.spider = crawler._create_spider("foo")
        mw = build_from_crawler(DownloadTimeoutMiddleware, crawler, clock=clock)
        mw.spider_opened(crawler.spider)
        mwman = DownloaderMiddlewareManager(mw, crawler=crawler)
        return crawler, mwman

    @coroutine_test
    async def test_expired_chain_does_not_download(self, clock: FakeClock) -> None:
        crawler, mwman = self.get_mwman(clock)
        downloaded: list[Request] = []

        async def download_func(request: Request) -> Response:
            downloaded.append(request)
            return Response(request.url)

        req = Request("http://example.com")
        assert isinstance(await mwman.download_async(download_func, req), Response)
        clock.advance(11)
        assert crawler.spider is not None
        retry = get_retry_request(req, spider=crawler.spider)
        assert retry is not None
        downloaded.clear()
        with pytest.raises(DownloadDeadlineExceededError):
            await mwman.download_async(download_func, retry)
        assert downloaded == []

    @coroutine_test
    async def test_queue_time_expires_before_download(self, clock: FakeClock) -> None:
        """Time spent after process_request(), e.g. waiting for a slot, is
        accounted for before the download function runs."""
        _, mwman = self.get_mwman(clock)

        async def download_func(request: Request) -> Response:
            pytest.fail("the download should not start")

        async def wait(request: Request) -> None:
            clock.advance(11)

        mwman.methods["process_request"].append(wait)
        with pytest.raises(DownloadDeadlineExceededError):
            await mwman.download_async(download_func, Request("http://example.com"))

    @coroutine_test
    async def test_remaining_caps_timeout_before_download(
        self, clock: FakeClock
    ) -> None:
        _, mwman = self.get_mwman(clock)
        seen: list[float | None] = []

        async def download_func(request: Request) -> Response:
            seen.append(request.meta.get("download_timeout"))
            return Response(request.url)

        async def wait(request: Request) -> None:
            clock.advance(4)

        mwman.methods["process_request"].append(wait)
        await mwman.download_async(download_func, Request("http://example.com"))
        assert seen == [6.0]

    @coroutine_test
    async def test_cache_hit_does_not_extend(self, clock: FakeClock) -> None:
        """A response served without a download, e.g. from the cache, neither
        resets nor extends the deadline of the chain."""
        _, mwman = self.get_mwman(clock)
        downloaded: list[Request] = []

        async def download_func(request: Request) -> Response:
            downloaded.append(request)
            return Response(request.url)

        req = Request("http://example.com")
        assert isinstance(await mwman.download_async(download_func, req), Response)
        clock.advance(4)

        async def cached(request: Request) -> Response:
            return Response(request.url, flags=["cached"])

        # A process_request() that returns a response, e.g. HttpCacheMiddleware
        # on a cache hit, skips the download and its pre-download check.
        mwman.methods["process_request"].append(cached)
        retry = req.copy()
        assert isinstance(await mwman.download_async(download_func, retry), Response)
        assert downloaded == [req]
        assert retry.meta["_download_deadline"] == 10
        # The next download of the chain still measures from the same deadline.
        timeout_mw = next(
            mw for mw in mwman.middlewares if isinstance(mw, DownloadTimeoutMiddleware)
        )
        assert timeout_mw.process_request(retry) is None
        assert retry.meta["download_timeout"] == 6

    @coroutine_test
    async def test_cancelled_download_does_not_extend(self, clock: FakeClock) -> None:
        """Cancelling a download does not change the deadline, so the next
        request of the chain is still capped by it."""
        _, mwman = self.get_mwman(clock)

        async def cancelled(request: Request) -> Response:
            clock.advance(5)
            raise DownloadCancelledError("test cancellation")

        req = Request("http://example.com")
        with pytest.raises(DownloadCancelledError):
            await mwman.download_async(cancelled, req)
        assert req.meta["_download_deadline"] == 10
        timeout_mw = next(
            mw for mw in mwman.middlewares if isinstance(mw, DownloadTimeoutMiddleware)
        )
        timeout_mw._check_download_deadline(req)
        assert req.meta["download_timeout"] == 5

    @coroutine_test
    async def test_disabled_by_default_through_manager(self) -> None:
        crawler = get_crawler(Spider)
        crawler.spider = crawler._create_spider("foo")
        mwman = build_from_crawler(DownloaderMiddlewareManager, crawler)
        downloaded: list[Request] = []

        async def download_func(request: Request) -> Response:
            downloaded.append(request)
            return Response(request.url)

        req = Request("http://example.com")
        await mwman.download_async(download_func, req)
        assert downloaded == [req]
        assert "_download_deadline" not in req.meta


class _CountingHandler(DataURIDownloadHandler):
    def __init__(self, crawler: Crawler):
        super().__init__(crawler)
        self.downloads: list[str] = []

    async def download_request(self, request: Request) -> Response:
        self.downloads.append(request.url)
        return await super().download_request(request)


class _WaitingMiddleware:
    """Stands for time spent after process_request() but before a download,
    e.g. waiting in a slot queue."""

    def __init__(self, clock: FakeClock, seconds: float) -> None:
        self._clock = clock
        self._seconds = seconds

    def process_request(self, request: Request) -> None:
        self._clock.advance(self._seconds)


class TestThroughDownloader:
    """Deadline behavior through the downloader and a fake download handler."""

    @coroutine_test
    async def test_expired_before_connection(
        self, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The downloader builds the middleware itself, so replace the default
        # clock that the middleware reads.
        monkeypatch.setattr(
            "scrapy.downloadermiddlewares.downloadtimeout.monotonic", clock
        )
        crawler = get_crawler(
            DefaultSpider,
            {"DOWNLOAD_DEADLINE": 10, "DOWNLOAD_HANDLERS": {"data": _CountingHandler}},
        )
        crawler.spider = crawler._create_spider()
        downloader = Downloader(crawler)
        # Runs after the deadline check of process_request(), standing for the
        # time that a request spends waiting for its turn.
        downloader.middleware.methods["process_request"].append(
            _WaitingMiddleware(clock, 11).process_request
        )
        handler = downloader.handlers._get_handler("data")
        assert isinstance(handler, _CountingHandler)
        try:
            with pytest.raises(DownloadDeadlineExceededError):
                await maybe_deferred_to_future(downloader.fetch(Request("data:,")))
            # The download handler never ran for the expired request.
            assert handler.downloads == []
        finally:
            downloader.close()
            await downloader.handlers._close()

    @coroutine_test
    async def test_queue_time_caps_timeout(
        self, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The time that a request spends queued lowers the timeout that the
        handler sees, without the deadline being reset."""
        monkeypatch.setattr(
            "scrapy.downloadermiddlewares.downloadtimeout.monotonic", clock
        )
        seen: list[float | None] = []

        class Handler(DataURIDownloadHandler):
            async def download_request(self, request: Request) -> Response:
                seen.append(request.meta.get("download_timeout"))
                return await super().download_request(request)

        crawler = get_crawler(
            DefaultSpider,
            {"DOWNLOAD_DEADLINE": 10, "DOWNLOAD_HANDLERS": {"data": Handler}},
        )
        crawler.spider = crawler._create_spider()
        downloader = Downloader(crawler)
        req = Request("data:,")
        try:
            await maybe_deferred_to_future(downloader.fetch(req))
            clock.advance(4)
            await maybe_deferred_to_future(downloader.fetch(req))
            assert seen == [10, 6]
        finally:
            downloader.close()
            await downloader.handlers._close()

    @coroutine_test
    async def test_close_clears_stat_state(
        self, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "scrapy.downloadermiddlewares.downloadtimeout.monotonic", clock
        )
        crawler = get_crawler(DefaultSpider, {"DOWNLOAD_DEADLINE": 0})
        crawler.spider = crawler._create_spider()
        downloader = Downloader(crawler)
        timeout_mw = next(
            mw
            for mw in downloader.middleware.middlewares
            if isinstance(mw, DownloadTimeoutMiddleware)
        )
        try:
            with pytest.raises(DownloadDeadlineExceededError):
                await maybe_deferred_to_future(downloader.fetch(Request("data:,")))
            assert crawler.stats is not None
            assert crawler.stats.get_value("download_deadline/exceeded") == 1
            assert timeout_mw._expired
            crawler.signals.send_catch_log(
                signal=signals.spider_closed,
                spider=crawler.spider,
                reason="finished",
                error=False,
            )
            assert not timeout_mw._expired
        finally:
            downloader.close()
            await downloader.handlers._close()
