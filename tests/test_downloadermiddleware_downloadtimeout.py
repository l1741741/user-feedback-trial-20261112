from __future__ import annotations

import time
from typing import Any

import pytest

from scrapy.core.downloader.middleware import DownloaderMiddlewareManager
from scrapy.downloadermiddlewares.downloadtimeout import DownloadTimeoutMiddleware
from scrapy.downloadermiddlewares.redirect import RedirectMiddleware
from scrapy.downloadermiddlewares.retry import get_retry_request
from scrapy.exceptions import DownloadCancelledError, DownloadDeadlineExceededError
from scrapy.http import Request, Response
from scrapy.spiders import Spider
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test


def get_request_spider_mw(settings: dict[str, Any] | None = None):
    crawler = get_crawler(Spider, settings)
    spider = crawler._create_spider("foo")
    request = Request("http://scrapytest.org/")
    return request, spider, build_from_crawler(DownloadTimeoutMiddleware, crawler)


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


class FakeClock:
    """A controllable monotonic clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(
        "scrapy.downloadermiddlewares.downloadtimeout.monotonic", fake
    )
    return fake


def test_deadline_disabled_by_default(clock: FakeClock):
    req, spider, mw = get_request_spider_mw()
    mw.spider_opened(spider)
    assert mw.process_request(req) is None
    assert "_download_deadline" not in req.meta
    assert req.meta["download_timeout"] == 180


def test_deadline_first_request(clock: FakeClock):
    req, spider, mw = get_request_spider_mw({"DOWNLOAD_DEADLINE": 10})
    mw.spider_opened(spider)
    clock.advance(5)
    assert mw.process_request(req) is None
    assert req.meta["_download_deadline"] == 15
    # The remaining budget is lower than DOWNLOAD_TIMEOUT (180).
    assert req.meta["download_timeout"] == 10


def test_deadline_string_setting(clock: FakeClock):
    req, spider, mw = get_request_spider_mw({"DOWNLOAD_DEADLINE": "10.5"})
    mw.spider_opened(spider)
    assert mw.process_request(req) is None
    assert req.meta["_download_deadline"] == 10.5
    assert req.meta["download_timeout"] == 10.5


def test_deadline_retry_inherits(clock: FakeClock):
    req, spider, mw = get_request_spider_mw({"DOWNLOAD_DEADLINE": 10})
    mw.spider_opened(spider)
    assert mw.process_request(req) is None
    clock.advance(4)
    retry = get_retry_request(req, spider=spider)
    assert retry is not None
    assert retry.meta["_download_deadline"] == 10
    assert mw.process_request(retry) is None
    assert retry.meta["download_timeout"] == 6


def test_deadline_redirect_inherits(clock: FakeClock):
    crawler = get_crawler(Spider, {"DOWNLOAD_DEADLINE": 10})
    spider = crawler._create_spider("foo")
    crawler.spider = spider
    mw = build_from_crawler(DownloadTimeoutMiddleware, crawler)
    mw.spider_opened(spider)
    redirect_mw = build_from_crawler(RedirectMiddleware, crawler)
    req = Request("http://scrapytest.org/")
    assert mw.process_request(req) is None
    clock.advance(4)
    response = Response(
        req.url, status=302, headers={"Location": "http://scrapytest.org/redirected"}
    )
    redirected = redirect_mw.process_response(req, response)
    assert isinstance(redirected, Request)
    assert redirected.meta["_download_deadline"] == 10
    assert mw.process_request(redirected) is None
    assert redirected.meta["download_timeout"] == 6


def test_deadline_exceeded(clock: FakeClock):
    req, spider, mw = get_request_spider_mw({"DOWNLOAD_DEADLINE": 10})
    mw.spider_opened(spider)
    assert mw.process_request(req) is None
    clock.advance(11)
    retry = get_retry_request(req, spider=spider)
    assert retry is not None
    with pytest.raises(DownloadDeadlineExceededError, match="deadline"):
        mw.process_request(retry)


def test_deadline_independent_requests(clock: FakeClock):
    _, spider, mw = get_request_spider_mw({"DOWNLOAD_DEADLINE": 10})
    mw.spider_opened(spider)
    req1 = Request("http://scrapytest.org/1")
    assert mw.process_request(req1) is None
    assert req1.meta["_download_deadline"] == 10
    clock.advance(8)
    # An independent request gets its own full budget, unaffected by the time
    # the chain of req1 has already used.
    req2 = Request("http://scrapytest.org/2")
    assert mw.process_request(req2) is None
    assert req2.meta["_download_deadline"] == 18
    assert req2.meta["download_timeout"] == 10


def test_deadline_respects_smaller_meta_timeout(clock: FakeClock):
    req, spider, mw = get_request_spider_mw({"DOWNLOAD_DEADLINE": 10})
    mw.spider_opened(spider)
    req.meta["download_timeout"] = 2
    assert mw.process_request(req) is None
    assert req.meta["download_timeout"] == 2


def test_deadline_caps_larger_meta_timeout(clock: FakeClock):
    req, spider, mw = get_request_spider_mw({"DOWNLOAD_DEADLINE": 10})
    mw.spider_opened(spider)
    req.meta["download_timeout"] = 100
    assert mw.process_request(req) is None
    assert req.meta["download_timeout"] == 10


def test_deadline_with_zero_download_timeout(clock: FakeClock):
    req, spider, mw = get_request_spider_mw(
        {"DOWNLOAD_TIMEOUT": 0, "DOWNLOAD_DEADLINE": 10}
    )
    mw.spider_opened(spider)
    clock.advance(4)
    assert mw.process_request(req) is None
    # With DOWNLOAD_TIMEOUT=0 there is no per-download timeout, but the
    # deadline still caps the download at the remaining budget.
    assert req.meta["_download_deadline"] == 14
    assert req.meta["download_timeout"] == 10


def test_deadline_ignores_wall_clock(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    req, spider, mw = get_request_spider_mw({"DOWNLOAD_DEADLINE": 10})
    mw.spider_opened(spider)
    assert mw.process_request(req) is None
    # A wall-clock jump far past the deadline has no effect: only the
    # monotonic clock counts.
    monkeypatch.setattr(time, "time", lambda: 1e15)
    assert mw.process_request(req) is None
    assert req.meta["download_timeout"] == 10
    # And a wall-clock jump into the past does not extend the budget.
    monkeypatch.setattr(time, "time", lambda: 0.0)
    clock.advance(11)
    with pytest.raises(DownloadDeadlineExceededError):
        mw.process_request(req)


@pytest.mark.parametrize("value", ["abc", -1, float("nan")])
def test_invalid_deadline(value: Any):
    crawler = get_crawler(Spider, {"DOWNLOAD_DEADLINE": value})
    with pytest.raises(ValueError, match="DOWNLOAD_DEADLINE"):
        build_from_crawler(DownloadTimeoutMiddleware, crawler)


def test_invalid_deadline_fails_before_crawling():
    # The middleware manager is built when the crawl is set up, before any
    # request is scheduled, so an invalid setting fails the crawl early.
    crawler = get_crawler(Spider, {"DOWNLOAD_DEADLINE": -1})
    with pytest.raises(ValueError, match="DOWNLOAD_DEADLINE"):
        build_from_crawler(DownloaderMiddlewareManager, crawler)


class TestManager:
    """Deadline behavior through the downloader middleware manager."""

    settings_dict = {"DOWNLOAD_DEADLINE": 10}

    def get_mwman(self):
        crawler = get_crawler(Spider, self.settings_dict)
        crawler.spider = crawler._create_spider("foo")
        return crawler, build_from_crawler(DownloaderMiddlewareManager, crawler)

    @coroutine_test
    async def test_deadline_exceeded_no_download(self, clock: FakeClock):
        crawler, mwman = self.get_mwman()
        downloaded: list[Request] = []

        async def download_func(request: Request) -> Response:
            downloaded.append(request)
            return Response(request.url)

        req = Request("http://scrapytest.org/")
        response = await mwman.download_async(download_func, req)
        assert isinstance(response, Response)
        assert downloaded == [req]

        # A request of the same chain whose budget is up fails without a new
        # download being started.
        clock.advance(11)
        spider = crawler.spider
        assert spider is not None
        retry = get_retry_request(req, spider=spider)
        assert retry is not None
        downloaded.clear()
        with pytest.raises(DownloadDeadlineExceededError):
            await mwman.download_async(download_func, retry)
        assert downloaded == []

    @coroutine_test
    async def test_cancelled_download_propagates(self, clock: FakeClock):
        _, mwman = self.get_mwman()

        async def download_func(request: Request) -> Response:
            raise DownloadCancelledError("test cancellation")

        with pytest.raises(DownloadCancelledError):
            await mwman.download_async(
                download_func, Request("http://scrapytest.org/")
            )
