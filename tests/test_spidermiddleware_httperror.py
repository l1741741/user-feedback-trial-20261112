from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from scrapy.http import Request, Response
from scrapy.spidermiddlewares.httperror import HttpError, HttpErrorMiddleware
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler
from tests.spiders import MockServerSpider
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from tests.mockserver.http import MockServer


class _HttpErrorSpider(MockServerSpider):
    name = "httperror"
    bypass_status_codes: set[int] = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        assert self.mockserver
        self.start_urls = [
            self.mockserver.url("/status?n=200"),
            self.mockserver.url("/status?n=404"),
            self.mockserver.url("/status?n=402"),
            self.mockserver.url("/status?n=500"),
        ]
        self.failed = set()
        self.skipped = set()
        self.parsed = set()

    async def start(self):
        for url in self.start_urls:
            yield Request(url, self.parse, errback=self.on_error)

    def parse(self, response: Response) -> None:
        self.parsed.add(response.url[-3:])

    def on_error(self, failure):
        if isinstance(failure.value, HttpError):
            response = failure.value.response
            if response.status in self.bypass_status_codes:
                self.skipped.add(response.url[-3:])
                return self.parse(response)

        # it assumes there is a response attached to failure
        self.failed.add(failure.value.response.url[-3:])
        return failure


req = Request("http://scrapytest.org")


def _response(request: Request, status_code: int) -> Response:
    return Response(request.url, status=status_code, request=request)


@pytest.fixture
def res200() -> Response:
    return _response(req, 200)


@pytest.fixture
def res402() -> Response:
    return _response(req, 402)


@pytest.fixture
def res404() -> Response:
    return _response(req, 404)


class TestHttpErrorMiddleware:
    @pytest.fixture
    def mw(self) -> HttpErrorMiddleware:
        crawler = get_crawler(DefaultSpider)
        crawler.spider = crawler._create_spider()
        return build_from_crawler(HttpErrorMiddleware, crawler)

    def test_process_spider_input(
        self, mw: HttpErrorMiddleware, res200: Response, res404: Response
    ) -> None:
        mw.process_spider_input(res200)
        with pytest.raises(HttpError):
            mw.process_spider_input(res404)

    def test_process_spider_exception(
        self, mw: HttpErrorMiddleware, res404: Response
    ) -> None:
        assert mw.process_spider_exception(res404, HttpError(res404)) == ()
        assert mw.process_spider_exception(res404, Exception()) is None

    def test_handle_httpstatus_list(
        self, mw: HttpErrorMiddleware, res404: Response
    ) -> None:
        request = Request(
            "http://scrapytest.org", meta={"handle_httpstatus_list": [404]}
        )
        res = _response(request, 404)
        mw.process_spider_input(res)

        assert mw.crawler.spider
        mw.crawler.spider.handle_httpstatus_list = [404]  # type: ignore[attr-defined]
        mw.process_spider_input(res404)


class TestHttpErrorMiddlewareSettings:
    """Similar test, but with settings"""

    @pytest.fixture
    def mw(self) -> HttpErrorMiddleware:
        crawler = get_crawler(DefaultSpider, {"HTTPERROR_ALLOWED_CODES": (402,)})
        crawler.spider = crawler._create_spider()
        return build_from_crawler(HttpErrorMiddleware, crawler)

    def test_process_spider_input(
        self,
        mw: HttpErrorMiddleware,
        res200: Response,
        res402: Response,
        res404: Response,
    ) -> None:
        mw.process_spider_input(res200)
        with pytest.raises(HttpError):
            mw.process_spider_input(res404)
        mw.process_spider_input(res402)

    def test_meta_overrides_settings(self, mw: HttpErrorMiddleware) -> None:
        request = Request(
            "http://scrapytest.org", meta={"handle_httpstatus_list": [404]}
        )
        res404 = _response(request, 404)
        res402 = _response(request, 402)

        mw.process_spider_input(res404)
        with pytest.raises(HttpError):
            mw.process_spider_input(res402)

    def test_spider_override_settings(
        self, mw: HttpErrorMiddleware, res402: Response, res404: Response
    ) -> None:
        assert mw.crawler.spider
        mw.crawler.spider.handle_httpstatus_list = [404]  # type: ignore[attr-defined]
        mw.process_spider_input(res404)
        with pytest.raises(HttpError):
            mw.process_spider_input(res402)


class TestHttpErrorMiddlewareHandleAll:
    @pytest.fixture
    def mw(self) -> HttpErrorMiddleware:
        crawler = get_crawler(DefaultSpider, {"HTTPERROR_ALLOW_ALL": True})
        crawler.spider = crawler._create_spider()
        return build_from_crawler(HttpErrorMiddleware, crawler)

    def test_process_spider_input(
        self,
        mw: HttpErrorMiddleware,
        res200: Response,
        res404: Response,
    ) -> None:
        mw.process_spider_input(res200)
        mw.process_spider_input(res404)

    def test_meta_overrides_settings(self, mw: HttpErrorMiddleware) -> None:
        request = Request(
            "http://scrapytest.org", meta={"handle_httpstatus_list": [404]}
        )
        res404 = _response(request, 404)
        res402 = _response(request, 402)

        mw.process_spider_input(res404)
        with pytest.raises(HttpError):
            mw.process_spider_input(res402)

    def test_httperror_allow_all_false(self) -> None:
        crawler = get_crawler(_HttpErrorSpider)
        mw = build_from_crawler(HttpErrorMiddleware, crawler)
        request_httpstatus_false = Request(
            "http://scrapytest.org", meta={"handle_httpstatus_all": False}
        )
        request_httpstatus_true = Request(
            "http://scrapytest.org", meta={"handle_httpstatus_all": True}
        )
        res404 = _response(request_httpstatus_false, 404)
        res402 = _response(request_httpstatus_true, 402)

        with pytest.raises(HttpError):
            mw.process_spider_input(res404)
        mw.process_spider_input(res402)


class TestHttpErrorMiddlewareStatusRanges:
    @pytest.fixture
    def mw(self) -> HttpErrorMiddleware:
        crawler = get_crawler(
            DefaultSpider, {"HTTPERROR_ALLOWED_STATUS_RANGES": [(400, 499)]}
        )
        crawler.spider = crawler._create_spider()
        return build_from_crawler(HttpErrorMiddleware, crawler)

    def test_process_spider_input(self, mw: HttpErrorMiddleware) -> None:
        mw.process_spider_input(_response(Request("https://example.com"), 200))
        mw.process_spider_input(_response(Request("https://example.com"), 400))
        mw.process_spider_input(_response(Request("https://example.com"), 404))
        mw.process_spider_input(_response(Request("https://example.com"), 499))
        with pytest.raises(HttpError):
            mw.process_spider_input(_response(Request("https://example.com"), 399))
        with pytest.raises(HttpError):
            mw.process_spider_input(_response(Request("https://example.com"), 500))

    def test_single_status_range(self) -> None:
        crawler = get_crawler(
            DefaultSpider, {"HTTPERROR_ALLOWED_STATUS_RANGES": [(404, 404)]}
        )
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(HttpErrorMiddleware, crawler)
        mw.process_spider_input(_response(Request("https://example.com"), 404))
        with pytest.raises(HttpError):
            mw.process_spider_input(_response(Request("https://example.com"), 403))
        with pytest.raises(HttpError):
            mw.process_spider_input(_response(Request("https://example.com"), 405))

    @pytest.mark.parametrize(
        "ranges",
        [
            [(400, 450), (451, 499)],  # adjacent
            [(400, 460), (450, 499)],  # overlapping
            [(400, 499), (400, 499)],  # duplicate
            [(450, 499), (400, 449)],  # unordered
        ],
    )
    def test_ranges_are_merged(self, ranges: list[tuple[int, int]]) -> None:
        crawler = get_crawler(
            DefaultSpider, {"HTTPERROR_ALLOWED_STATUS_RANGES": ranges}
        )
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(HttpErrorMiddleware, crawler)
        assert mw.handle_httpstatus_ranges == [(400, 499)]
        mw.process_spider_input(_response(Request("https://example.com"), 455))
        with pytest.raises(HttpError):
            mw.process_spider_input(_response(Request("https://example.com"), 500))

    @pytest.mark.parametrize(
        "ranges",
        [
            [(100, 100)],
            [(599, 599)],
            [(100, 599)],
        ],
    )
    def test_boundary_ranges_are_valid(self, ranges: list[tuple[int, int]]) -> None:
        crawler = get_crawler(
            DefaultSpider, {"HTTPERROR_ALLOWED_STATUS_RANGES": ranges}
        )
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(HttpErrorMiddleware, crawler)
        assert mw.handle_httpstatus_ranges == ranges

    @pytest.mark.parametrize(
        "ranges",
        [
            [(99, 200)],
            [(100, 600)],
            [(0, 99)],
            [(600, 699)],
            [(500, 400)],  # start > end
            [(True, 500)],  # bool is not a valid endpoint
            [(100, False)],
            [(100.0, 200)],
            [("400", "499")],
            [(404,)],  # not a pair
            [(400, 404, 500)],
            [404],
        ],
    )
    def test_invalid_settings_ranges_raise(self, ranges: list) -> None:
        crawler = get_crawler(
            DefaultSpider, {"HTTPERROR_ALLOWED_STATUS_RANGES": ranges}
        )
        crawler.spider = crawler._create_spider()
        with pytest.raises(ValueError, match="HTTPERROR_ALLOWED_STATUS_RANGES"):
            build_from_crawler(HttpErrorMiddleware, crawler)

    @pytest.mark.parametrize(
        "ranges",
        [
            [(99, 200)],
            [(100, 600)],
            [(500, 400)],
            [(True, 500)],
            [(100.0, 200)],
            [("400", "499")],
            [(404,)],
            [404],
        ],
    )
    def test_invalid_meta_ranges_raise(self, ranges: list) -> None:
        crawler = get_crawler(DefaultSpider)
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(HttpErrorMiddleware, crawler)
        request = Request(
            "https://example.com", meta={"handle_httpstatus_ranges": ranges}
        )
        with pytest.raises(ValueError, match="handle_httpstatus_ranges"):
            mw.process_spider_input(_response(request, 404))

    def test_meta_overrides_settings(self, mw: HttpErrorMiddleware) -> None:
        request = Request(
            "https://example.com", meta={"handle_httpstatus_ranges": [(500, 599)]}
        )
        mw.process_spider_input(_response(request, 500))
        # the setting allowing 4xx no longer applies to this request
        with pytest.raises(HttpError):
            mw.process_spider_input(_response(request, 404))

    def test_meta_empty_ranges_do_not_fall_back(self, mw: HttpErrorMiddleware) -> None:
        request = Request("https://example.com", meta={"handle_httpstatus_ranges": []})
        with pytest.raises(HttpError):
            mw.process_spider_input(_response(request, 404))

    def test_meta_empty_list_does_not_fall_back(self) -> None:
        crawler = get_crawler(DefaultSpider, {"HTTPERROR_ALLOWED_CODES": [404]})
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(HttpErrorMiddleware, crawler)
        request = Request("https://example.com", meta={"handle_httpstatus_list": []})
        with pytest.raises(HttpError):
            mw.process_spider_input(_response(request, 404))

    def test_meta_list_and_ranges_combine(self) -> None:
        crawler = get_crawler(DefaultSpider)
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(HttpErrorMiddleware, crawler)
        request = Request(
            "https://example.com",
            meta={
                "handle_httpstatus_list": [402],
                "handle_httpstatus_ranges": [(500, 599)],
            },
        )
        mw.process_spider_input(_response(request, 402))
        mw.process_spider_input(_response(request, 503))
        with pytest.raises(HttpError):
            mw.process_spider_input(_response(request, 404))

    def test_spider_attribute_overrides_settings(self, mw: HttpErrorMiddleware) -> None:
        assert mw.crawler.spider
        mw.crawler.spider.handle_httpstatus_ranges = [(500, 599)]  # type: ignore[attr-defined]
        mw.process_spider_input(_response(Request("https://example.com"), 500))
        with pytest.raises(HttpError):
            mw.process_spider_input(_response(Request("https://example.com"), 404))

    def test_invalid_spider_attribute_raises(self, mw: HttpErrorMiddleware) -> None:
        assert mw.crawler.spider
        mw.crawler.spider.handle_httpstatus_ranges = [(99, 200)]  # type: ignore[attr-defined]
        with pytest.raises(ValueError, match="handle_httpstatus_ranges"):
            mw.process_spider_input(_response(Request("https://example.com"), 404))

    def test_requests_do_not_share_policy(self) -> None:
        crawler = get_crawler(DefaultSpider)
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(HttpErrorMiddleware, crawler)
        request_with_meta = Request(
            "https://example.com", meta={"handle_httpstatus_ranges": [(404, 404)]}
        )
        mw.process_spider_input(_response(request_with_meta, 404))
        # a later request without meta must not inherit the previous policy
        plain_request = Request("https://example.com")
        with pytest.raises(HttpError):
            mw.process_spider_input(_response(plain_request, 404))

    def test_inputs_are_not_mutated(self, mw: HttpErrorMiddleware) -> None:
        meta = {"handle_httpstatus_ranges": [(450, 499), (400, 449)]}
        request = Request("https://example.com", meta=meta)
        mw.process_spider_input(_response(request, 404))
        assert request.meta == {"handle_httpstatus_ranges": [(450, 499), (400, 449)]}
        assert meta == {"handle_httpstatus_ranges": [(450, 499), (400, 449)]}
        assert mw.handle_httpstatus_ranges == [(400, 499)]

    def test_rejection_is_counted_once(self, mw: HttpErrorMiddleware) -> None:
        response = _response(Request("https://example.com"), 500)
        with pytest.raises(HttpError):
            mw.process_spider_input(response)
        try:
            mw.process_spider_input(response)
        except HttpError as exc:
            assert mw.process_spider_exception(response, exc) == ()
        get_value = mw.crawler.stats.get_value
        assert get_value("httperror/response_ignored_count") == 1
        assert get_value("httperror/response_ignored_status_count/500") == 1


class TestHttpErrorMiddlewareIntegrational:
    @coroutine_test
    async def test_middleware_works(self, mockserver: MockServer) -> None:
        crawler = get_crawler(_HttpErrorSpider)
        await crawler.crawl_async(mockserver=mockserver)
        assert isinstance(crawler.spider, _HttpErrorSpider)
        assert not crawler.spider.skipped
        assert crawler.spider.parsed == {"200"}
        assert crawler.spider.failed == {"404", "402", "500"}

        get_value = crawler.stats.get_value
        assert get_value("httperror/response_ignored_count") == 3
        assert get_value("httperror/response_ignored_status_count/404") == 1
        assert get_value("httperror/response_ignored_status_count/402") == 1
        assert get_value("httperror/response_ignored_status_count/500") == 1

    @coroutine_test
    async def test_logging(
        self, caplog: pytest.LogCaptureFixture, mockserver: MockServer
    ) -> None:
        crawler = get_crawler(_HttpErrorSpider)
        with caplog.at_level(logging.INFO):
            await crawler.crawl_async(mockserver=mockserver, bypass_status_codes={402})
        assert isinstance(crawler.spider, _HttpErrorSpider)
        assert crawler.spider.parsed == {"200", "402"}
        assert crawler.spider.skipped == {"402"}
        assert crawler.spider.failed == {"404", "500"}

        assert "Ignoring response <404" in caplog.text
        assert "Ignoring response <500" in caplog.text
        assert "Ignoring response <200" not in caplog.text
        assert "Ignoring response <402" not in caplog.text

    @coroutine_test
    async def test_logging_level(
        self, caplog: pytest.LogCaptureFixture, mockserver: MockServer
    ) -> None:
        # HttpError logs ignored responses with level INFO
        crawler = get_crawler(_HttpErrorSpider)
        with caplog.at_level(logging.INFO):
            await crawler.crawl_async(mockserver=mockserver)
        assert isinstance(crawler.spider, _HttpErrorSpider)
        assert crawler.spider.parsed == {"200"}
        assert crawler.spider.failed == {"404", "402", "500"}

        assert "Ignoring response <402" in caplog.text
        assert "Ignoring response <404" in caplog.text
        assert "Ignoring response <500" in caplog.text
        assert "Ignoring response <200" not in caplog.text

        # with level WARNING, we shouldn't capture anything from HttpError
        caplog.clear()
        crawler = get_crawler(_HttpErrorSpider)
        with caplog.at_level(logging.WARNING):
            await crawler.crawl_async(mockserver=mockserver)
        assert isinstance(crawler.spider, _HttpErrorSpider)
        assert crawler.spider.parsed == {"200"}
        assert crawler.spider.failed == {"404", "402", "500"}

        assert "Ignoring response <402" not in caplog.text
        assert "Ignoring response <404" not in caplog.text
        assert "Ignoring response <500" not in caplog.text
        assert "Ignoring response <200" not in caplog.text
