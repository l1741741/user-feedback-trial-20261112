from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

import pytest
from twisted.internet.error import ConnectError, ConnectionDone, ConnectionLost
from twisted.internet.task import Clock

from scrapy.core.scheduler import Scheduler
from scrapy.downloadermiddlewares.retry import (
    RetryMiddleware,
    _AsyncioClock,
    _default_clock,
    _OriginWaitGate,
    _parse_retry_after,
    get_retry_request,
)
from scrapy.exceptions import (
    CannotResolveHostError,
    DownloadConnectionRefusedError,
    DownloadTimeoutError,
    IgnoreRequest,
    ScrapyDeprecationWarning,
)
from scrapy.http import Request, Response
from scrapy.settings.default_settings import RETRY_EXCEPTIONS
from scrapy.spiders import Spider
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.mockserver.http import MockServer


class TestRetry:
    def setup_method(self) -> None:
        self.crawler = get_crawler(DefaultSpider)
        self.crawler.spider = self.crawler._create_spider()
        self.mw: RetryMiddleware = build_from_crawler(RetryMiddleware, self.crawler)
        self.mw.max_retry_times = 2

    @coroutine_test
    async def test_priority_adjust(self):
        req = Request("http://www.scrapytest.org/503")
        rsp = Response("http://www.scrapytest.org/503", body=b"", status=503)
        req2 = await self.mw.process_response(req, rsp)
        assert isinstance(req2, Request)
        assert req2.priority < req.priority

    @coroutine_test
    async def test_404(self):
        req = Request("http://www.scrapytest.org/404")
        rsp = Response("http://www.scrapytest.org/404", body=b"", status=404)

        # dont retry 404s
        assert await self.mw.process_response(req, rsp) is rsp

    @coroutine_test
    async def test_dont_retry(self):
        req = Request("http://www.scrapytest.org/503", meta={"dont_retry": True})
        rsp = Response("http://www.scrapytest.org/503", body=b"", status=503)

        # no retry
        r = await self.mw.process_response(req, rsp)
        assert r is rsp

        # Test retry when dont_retry set to False
        req = Request("http://www.scrapytest.org/503", meta={"dont_retry": False})
        rsp = Response("http://www.scrapytest.org/503", body=b"", status=503)

        # first retry
        req2 = await self.mw.process_response(req, rsp)
        assert isinstance(req2, Request)
        assert req2.meta["retry_times"] == 1

    def test_dont_retry_exc(self):
        req = Request("http://www.scrapytest.org/503", meta={"dont_retry": True})

        r = self.mw.process_exception(req, CannotResolveHostError())
        assert r is None

    @coroutine_test
    async def test_503(self):
        req = Request("http://www.scrapytest.org/503")
        rsp = Response("http://www.scrapytest.org/503", body=b"", status=503)

        # first retry
        req2 = await self.mw.process_response(req, rsp)
        assert isinstance(req2, Request)
        assert req2.meta["retry_times"] == 1

        # second retry
        req3 = await self.mw.process_response(req2, rsp)
        assert isinstance(req3, Request)
        assert req3.meta["retry_times"] == 2

        # discard it
        assert await self.mw.process_response(req3, rsp) is rsp

        assert self.crawler.stats.get_value("retry/max_reached") == 1
        assert (
            self.crawler.stats.get_value("retry/reason_count/503 Service Unavailable")
            == 2
        )
        assert self.crawler.stats.get_value("retry/count") == 2

    @coroutine_test
    async def test_give_up_log_level_setting(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        crawler = get_crawler(
            DefaultSpider, settings_dict={"RETRY_GIVE_UP_LOG_LEVEL": "WARNING"}
        )
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(RetryMiddleware, crawler)
        mw.max_retry_times = 0
        req = Request("http://example.com/503")
        rsp = Response("http://example.com/503", body=b"", status=503)
        with caplog.at_level(logging.WARNING):
            assert await mw.process_response(req, rsp) is rsp
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.WARNING,
            f"Gave up retrying {req} (failed 1 times): 503 Service Unavailable",
        ) in caplog.record_tuples

    @coroutine_test
    async def test_give_up_log_level_meta(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        self.mw.max_retry_times = 0
        req = Request("http://example.com/503", meta={"give_up_log_level": "WARNING"})
        rsp = Response("http://example.com/503", body=b"", status=503)
        with caplog.at_level(logging.WARNING):
            assert await self.mw.process_response(req, rsp) is rsp
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.WARNING,
            f"Gave up retrying {req} (failed 1 times): 503 Service Unavailable",
        ) in caplog.record_tuples

    def test_twistederrors(self):
        exceptions = [
            ConnectError,
            ConnectionDone,
            ConnectionLost,
            DownloadTimeoutError,
            DownloadConnectionRefusedError,
            CannotResolveHostError,
        ]

        for exc in exceptions:
            req = Request(f"http://www.scrapytest.org/{exc.__name__}")
            self._test_retry_exception(req, exc("foo"))

        stats = self.crawler.stats
        assert stats.get_value("retry/max_reached") == len(exceptions)
        assert stats.get_value("retry/count") == len(exceptions) * 2
        assert (
            stats.get_value("retry/reason_count/scrapy.exceptions.DownloadTimeoutError")
            == 2
        )

    def test_exception_to_retry_added(self):
        exc = ValueError
        settings_dict = {
            "RETRY_EXCEPTIONS": [*RETRY_EXCEPTIONS, exc],
        }
        crawler = get_crawler(DefaultSpider, settings_dict=settings_dict)
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(RetryMiddleware, crawler)
        req = Request(f"http://www.scrapytest.org/{exc.__name__}")
        self._test_retry_exception(req, exc("foo"), mw)

    def _test_retry_exception(
        self, req: Request, exception: Exception, mw: RetryMiddleware | None = None
    ) -> None:
        if mw is None:
            mw = self.mw

        # first retry
        req2 = mw.process_exception(req, exception)
        assert isinstance(req2, Request)
        assert req2.meta["retry_times"] == 1

        # second retry
        req3 = mw.process_exception(req2, exception)
        assert isinstance(req3, Request)
        assert req3.meta["retry_times"] == 2

        # discard it
        assert mw.process_exception(req3, exception) is None


class TestMaxRetryTimes:
    invalid_url = "http://www.scrapytest.org/invalid_url"

    def get_middleware(self, settings: dict[str, Any] | None = None) -> RetryMiddleware:
        crawler = get_crawler(DefaultSpider, settings or {})
        crawler.spider = crawler._create_spider()
        return build_from_crawler(RetryMiddleware, crawler)

    def test_with_settings_zero(self):
        max_retry_times = 0
        settings = {"RETRY_TIMES": max_retry_times}
        middleware = self.get_middleware(settings)
        req = Request(self.invalid_url)
        self._test_retry(
            req,
            CannotResolveHostError("foo"),
            max_retry_times,
            middleware=middleware,
        )

    def test_with_metakey_zero(self):
        max_retry_times = 0
        middleware = self.get_middleware()
        meta = {"max_retry_times": max_retry_times}
        req = Request(self.invalid_url, meta=meta)
        self._test_retry(
            req,
            CannotResolveHostError("foo"),
            max_retry_times,
            middleware=middleware,
        )

    def test_without_metakey(self):
        max_retry_times = 5
        settings = {"RETRY_TIMES": max_retry_times}
        middleware = self.get_middleware(settings)
        req = Request(self.invalid_url)
        self._test_retry(
            req,
            CannotResolveHostError("foo"),
            max_retry_times,
            middleware=middleware,
        )

    def test_with_metakey_greater(self):
        meta_max_retry_times = 3
        middleware_max_retry_times = 2

        req1 = Request(self.invalid_url, meta={"max_retry_times": meta_max_retry_times})
        req2 = Request(self.invalid_url)

        settings = {"RETRY_TIMES": middleware_max_retry_times}
        middleware = self.get_middleware(settings)

        self._test_retry(
            req1,
            CannotResolveHostError("foo"),
            meta_max_retry_times,
            middleware=middleware,
        )
        self._test_retry(
            req2,
            CannotResolveHostError("foo"),
            middleware_max_retry_times,
            middleware=middleware,
        )

    def test_with_metakey_lesser(self):
        meta_max_retry_times = 4
        middleware_max_retry_times = 5

        req1 = Request(self.invalid_url, meta={"max_retry_times": meta_max_retry_times})
        req2 = Request(self.invalid_url)

        settings = {"RETRY_TIMES": middleware_max_retry_times}
        middleware = self.get_middleware(settings)

        self._test_retry(
            req1,
            CannotResolveHostError("foo"),
            meta_max_retry_times,
            middleware=middleware,
        )
        self._test_retry(
            req2,
            CannotResolveHostError("foo"),
            middleware_max_retry_times,
            middleware=middleware,
        )

    def test_with_dont_retry(self):
        max_retry_times = 4
        middleware = self.get_middleware()
        meta = {
            "max_retry_times": max_retry_times,
            "dont_retry": True,
        }
        req = Request(self.invalid_url, meta=meta)
        self._test_retry(
            req,
            CannotResolveHostError("foo"),
            0,
            middleware=middleware,
        )

    def _test_retry(
        self,
        req: Request,
        exception: Exception,
        max_retry_times: int,
        middleware: RetryMiddleware,
    ) -> None:
        for _ in range(max_retry_times):
            result = middleware.process_exception(req, exception)
            assert isinstance(result, Request)
            req = result

        # discard it
        assert middleware.process_exception(req, exception) is None


class TestGetRetryRequest:
    @staticmethod
    def get_spider(settings: dict[str, Any] | None = None) -> Spider:
        crawler = get_crawler(Spider, settings or {})
        return crawler._create_spider("foo")

    def test_basic_usage(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.DEBUG):
            new_request = get_retry_request(
                request,
                spider=spider,
            )
        assert isinstance(new_request, Request)
        assert new_request != request
        assert new_request.meta["skip_dupefilter_once"] is True
        expected_retry_times = 1
        assert new_request.meta["retry_times"] == expected_retry_times
        assert new_request.priority == -1
        expected_reason = "unspecified"
        for stat in ("retry/count", f"retry/reason_count/{expected_reason}"):
            assert spider.crawler.stats.get_value(stat) == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_max_retries_reached(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        max_retry_times = 0
        with caplog.at_level(logging.DEBUG):
            new_request = get_retry_request(
                request,
                spider=spider,
                max_retry_times=max_retry_times,
            )
        assert new_request is None
        assert spider.crawler.stats.get_value("retry/max_reached") == 1
        failure_count = max_retry_times + 1
        expected_reason = "unspecified"
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.ERROR,
            (
                f"Gave up retrying {request} (failed {failure_count} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_one_retry(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.DEBUG):
            new_request = get_retry_request(
                request,
                spider=spider,
                max_retry_times=1,
            )
        assert isinstance(new_request, Request)
        assert new_request != request
        assert new_request.meta["skip_dupefilter_once"] is True
        expected_retry_times = 1
        assert new_request.meta["retry_times"] == expected_retry_times
        assert new_request.priority == -1
        expected_reason = "unspecified"
        for stat in ("retry/count", f"retry/reason_count/{expected_reason}"):
            assert spider.crawler.stats.get_value(stat) == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_two_retries(self, caplog: pytest.LogCaptureFixture) -> None:
        spider = self.get_spider()
        request = Request("https://example.com")
        new_request = request
        max_retry_times = 2
        for index in range(max_retry_times):
            caplog.clear()
            with caplog.at_level(logging.DEBUG):
                new_request = cast(
                    "Request",
                    get_retry_request(
                        new_request,
                        spider=spider,
                        max_retry_times=max_retry_times,
                    ),
                )
            assert isinstance(new_request, Request)
            assert new_request != request
            assert new_request.meta["skip_dupefilter_once"] is True
            expected_retry_times = index + 1
            assert new_request.meta["retry_times"] == expected_retry_times
            assert new_request.priority == -expected_retry_times
            expected_reason = "unspecified"
            for stat in ("retry/count", f"retry/reason_count/{expected_reason}"):
                value = spider.crawler.stats.get_value(stat)
                assert value == expected_retry_times
            assert (
                "scrapy.downloadermiddlewares.retry",
                logging.DEBUG,
                (
                    f"Retrying {request} (failed {expected_retry_times} times): "
                    f"{expected_reason}"
                ),
            ) in caplog.record_tuples

        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            new_request = cast(
                "Request",
                get_retry_request(
                    new_request,
                    spider=spider,
                    max_retry_times=max_retry_times,
                ),
            )
        assert new_request is None
        assert spider.crawler.stats.get_value("retry/max_reached") == 1
        failure_count = max_retry_times + 1
        expected_reason = "unspecified"
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.ERROR,
            (
                f"Gave up retrying {request} (failed {failure_count} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_no_spider(self):
        request = Request("https://example.com")
        with pytest.raises(TypeError):
            get_retry_request(request)  # type: ignore[call-arg]  # pylint: disable=missing-kwoa

    def test_max_retry_times_setting(self):
        max_retry_times = 0
        spider = self.get_spider({"RETRY_TIMES": max_retry_times})
        request = Request("https://example.com")
        new_request = get_retry_request(
            request,
            spider=spider,
        )
        assert new_request is None

    def test_max_retry_times_meta(self):
        max_retry_times = 0
        spider = self.get_spider({"RETRY_TIMES": max_retry_times + 1})
        meta = {"max_retry_times": max_retry_times}
        request = Request("https://example.com", meta=meta)
        new_request = get_retry_request(
            request,
            spider=spider,
        )
        assert new_request is None

    def test_max_retry_times_argument(self):
        max_retry_times = 0
        spider = self.get_spider({"RETRY_TIMES": max_retry_times + 1})
        meta = {"max_retry_times": max_retry_times + 1}
        request = Request("https://example.com", meta=meta)
        new_request = get_retry_request(
            request,
            spider=spider,
            max_retry_times=max_retry_times,
        )
        assert new_request is None

    def test_priority_adjust_setting(self):
        priority_adjust = 1
        spider = self.get_spider({"RETRY_PRIORITY_ADJUST": priority_adjust})
        request = Request("https://example.com")
        new_request = get_retry_request(
            request,
            spider=spider,
        )
        assert new_request
        assert new_request.priority == priority_adjust

    def test_priority_adjust_argument(self):
        priority_adjust = 1
        spider = self.get_spider({"RETRY_PRIORITY_ADJUST": priority_adjust + 1})
        request = Request("https://example.com")
        new_request = get_retry_request(
            request,
            spider=spider,
            priority_adjust=priority_adjust,
        )
        assert new_request
        assert new_request.priority == priority_adjust

    def test_log_extra_retry_success(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
            )
        assert any(getattr(r, "spider", None) is spider for r in caplog.records)

    def test_log_extra_retries_exceeded(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                max_retry_times=0,
            )
        assert any(getattr(r, "spider", None) is spider for r in caplog.records)

    def test_reason_string(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = "because"
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                reason=expected_reason,
            )
        expected_retry_times = 1
        for stat in ("retry/count", f"retry/reason_count/{expected_reason}"):
            assert spider.crawler.stats.get_value(stat) == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_reason_builtin_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = NotImplementedError()
        expected_reason_string = "builtins.NotImplementedError"
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                reason=expected_reason,
            )
        expected_retry_times = 1
        stat = spider.crawler.stats.get_value(
            f"retry/reason_count/{expected_reason_string}"
        )
        assert stat == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_reason_builtin_exception_class(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = NotImplementedError
        expected_reason_string = "builtins.NotImplementedError"
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                reason=expected_reason,
            )
        expected_retry_times = 1
        stat = spider.crawler.stats.get_value(
            f"retry/reason_count/{expected_reason_string}"
        )
        assert stat == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_reason_custom_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = IgnoreRequest()
        expected_reason_string = "scrapy.exceptions.IgnoreRequest"
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                reason=expected_reason,
            )
        expected_retry_times = 1
        stat = spider.crawler.stats.get_value(
            f"retry/reason_count/{expected_reason_string}"
        )
        assert stat == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_reason_custom_exception_class(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = IgnoreRequest
        expected_reason_string = "scrapy.exceptions.IgnoreRequest"
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                reason=expected_reason,
            )
        expected_retry_times = 1
        stat = spider.crawler.stats.get_value(
            f"retry/reason_count/{expected_reason_string}"
        )
        assert stat == 1
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.DEBUG,
            (
                f"Retrying {request} (failed {expected_retry_times} times): "
                f"{expected_reason}"
            ),
        ) in caplog.record_tuples

    def test_custom_logger(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = logging.getLogger("custom-logger")
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = "because"
        with caplog.at_level(logging.DEBUG):
            get_retry_request(
                request,
                spider=spider,
                reason=expected_reason,
                logger=logger,
            )
        assert (
            "custom-logger",
            logging.DEBUG,
            f"Retrying {request} (failed 1 times): {expected_reason}",
        ) in caplog.record_tuples

    def test_give_up_log_level_default(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.ERROR):
            get_retry_request(
                request,
                spider=spider,
                max_retry_times=0,
            )
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.ERROR,
            f"Gave up retrying {request} (failed 1 times): unspecified",
        ) in caplog.record_tuples

    def test_give_up_log_level_argument_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.WARNING):
            get_retry_request(
                request,
                spider=spider,
                max_retry_times=0,
                give_up_log_level="WARNING",
            )
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.WARNING,
            f"Gave up retrying {request} (failed 1 times): unspecified",
        ) in caplog.record_tuples

    def test_give_up_log_level_argument_number(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = Request("https://example.com")
        spider = self.get_spider()
        with caplog.at_level(logging.WARNING):
            get_retry_request(
                request,
                spider=spider,
                max_retry_times=0,
                give_up_log_level=logging.WARNING,
            )
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.WARNING,
            f"Gave up retrying {request} (failed 1 times): unspecified",
        ) in caplog.record_tuples

    def test_give_up_log_level_setting(self, caplog: pytest.LogCaptureFixture) -> None:
        request = Request("https://example.com")
        spider = self.get_spider({"RETRY_GIVE_UP_LOG_LEVEL": "WARNING"})
        with caplog.at_level(logging.WARNING):
            get_retry_request(
                request,
                spider=spider,
                max_retry_times=0,
            )
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.WARNING,
            f"Gave up retrying {request} (failed 1 times): unspecified",
        ) in caplog.record_tuples

    def test_give_up_log_level_invalid(self):
        request = Request("https://example.com")
        spider = self.get_spider()
        with pytest.raises(ValueError, match="Invalid give-up log level"):
            get_retry_request(
                request,
                spider=spider,
                max_retry_times=0,
                give_up_log_level="NOT_A_LEVEL",
            )

    def test_custom_stats_key(self):
        request = Request("https://example.com")
        spider = self.get_spider()
        expected_reason = "because"
        stats_key = "custom_retry"
        get_retry_request(
            request,
            spider=spider,
            reason=expected_reason,
            stats_base_key=stats_key,
        )
        for stat in (
            f"{stats_key}/count",
            f"{stats_key}/reason_count/{expected_reason}",
        ):
            assert spider.crawler.stats.get_value(stat) == 1


class _RetryThenRedirectSpider(Spider):
    """Retries two requests whose retries redirect to an already-seen URL."""

    name = "retry_then_redirect"
    mockserver: MockServer

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen_parsed = 0

    async def start(self) -> AsyncIterator[Any]:
        seen = self.mockserver.url("/status?n=200")
        yield Request(seen)
        for n in range(2):
            url = self.mockserver.url(
                "/redirect-to?" + urlencode({"goto": seen, "n": n})
            )
            yield Request(
                url, meta={"dont_redirect": True, "handle_httpstatus_list": [302]}
            )

    def parse(self, response: Response) -> Request | None:
        if response.status == 200:
            self.seen_parsed += 1
            return None
        assert response.request is not None
        request = get_retry_request(response.request, spider=self)
        assert request is not None
        del request.meta["dont_redirect"]
        del request.meta["handle_httpstatus_list"]
        return request


class _LegacyScheduler(Scheduler):
    supports_skip_dupefilter_once = False


class _OverridingScheduler(Scheduler):
    def enqueue_request(self, request: Request) -> bool:
        if not request.dont_filter and self.df.request_seen(request):
            self.df.log(request, self.spider)
            return False
        self._mqpush(request)
        return True


@coroutine_test
async def test_retry_redirect_dupefilter(mockserver: MockServer) -> None:
    crawler = get_crawler(_RetryThenRedirectSpider)
    await crawler.crawl_async(mockserver=mockserver)
    assert crawler.spider is not None
    assert cast("_RetryThenRedirectSpider", crawler.spider).seen_parsed == 1
    assert crawler.stats is not None
    assert crawler.stats.get_value("dupefilter/filtered") == 2


@pytest.mark.parametrize("scheduler", [_LegacyScheduler, _OverridingScheduler])
@coroutine_test
async def test_retry_redirect_dupefilter_legacy_scheduler(
    mockserver: MockServer, scheduler: type[Scheduler]
) -> None:
    crawler = get_crawler(_RetryThenRedirectSpider, {"SCHEDULER": scheduler})
    with pytest.warns(
        ScrapyDeprecationWarning, match="supports_skip_dupefilter_once"
    ) as record:
        await crawler.crawl_async(mockserver=mockserver)
    assert sum("supports_skip_dupefilter_once" in str(w.message) for w in record) == 1
    assert crawler.spider is not None
    assert cast("_RetryThenRedirectSpider", crawler.spider).seen_parsed == 3


class TestParseRetryAfter:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("0", 0.0),
            ("120", 120.0),
            (" 30 ", 30.0),
        ],
    )
    def test_delay_seconds(self, value: str, expected: float) -> None:
        assert _parse_retry_after(value) == expected

    def test_http_date(self) -> None:
        delay = _parse_retry_after(
            format_datetime(datetime.now(UTC) + timedelta(seconds=60))
        )
        assert delay is not None
        assert 59 <= delay <= 61

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "not a number",
            "-5",
            "1.5",
            "12a",
            "Wed, 21 Oct 2015 07:28:00",  # naive date, no time zone
            "Wed, 99 Foo 9999 00:00:00 GMT",
        ],
    )
    def test_invalid(self, value: str) -> None:
        assert _parse_retry_after(value) is None

    def test_past_date(self) -> None:
        value = format_datetime(datetime.now(UTC) - timedelta(seconds=60))
        assert _parse_retry_after(value) is None


class TestAsyncioClock:
    @coroutine_test
    async def test_timer_runs_on_running_loop(self) -> None:
        loop = asyncio.get_running_loop()
        clock = _AsyncioClock(loop)
        fired = asyncio.Event()
        handle = clock.callLater(0, fired.set)
        assert handle is not None
        await fired.wait()
        # The clock reads the same monotonic source as the loop it wraps.
        assert abs(clock.seconds() - loop.time()) < 1e-6

    @coroutine_test
    async def test_handle_active_and_cancel(self) -> None:
        loop = asyncio.get_running_loop()
        clock = _AsyncioClock(loop)
        handle = clock.callLater(60, lambda: None)
        assert handle.active()
        handle.cancel()
        assert not handle.active()
        # Cancelling an already cancelled timer is a no-op.
        handle.cancel()

    @coroutine_test
    async def test_gate_close_on_asyncio_clock(self) -> None:
        # The production clock schedules asyncio timers, whose handles expose
        # no Twisted-style active(): closing a pending gate must work anyway.
        crawler = get_crawler(DefaultSpider)
        gate = _OriginWaitGate(_default_clock(), crawler.stats)
        wait = gate.extend_deadline(("http", "example.com", None), 60.0)
        task = asyncio.ensure_future(gate.wait(wait))
        await asyncio.sleep(0)
        assert not task.done()
        gate.close()
        await task
        assert not gate._waits

    @coroutine_test
    async def test_default_clock_uses_loop_when_asyncio_available(self) -> None:
        assert isinstance(_default_clock(), _AsyncioClock)


class TestOriginWaitGate:
    def setup_method(self) -> None:
        self.clock = Clock()
        self.crawler = get_crawler(DefaultSpider)
        self.gate = _OriginWaitGate(self.clock, self.crawler.stats)
        self.origin = ("http", "example.com", None)

    @coroutine_test
    async def test_release_at_deadline(self) -> None:
        wait = self.gate.extend_deadline(self.origin, 10.0)
        task = asyncio.ensure_future(self.gate.wait(wait))
        await asyncio.sleep(0)
        assert not task.done()
        self.clock.advance(9)
        await asyncio.sleep(0)
        assert not task.done()
        self.clock.advance(1)
        await asyncio.sleep(0)
        assert task.done()
        assert not self.gate._waits
        assert not self.clock.getDelayedCalls()

    @coroutine_test
    async def test_extend_moves_deadline_later(self) -> None:
        wait = self.gate.extend_deadline(self.origin, 10.0)
        assert len(self.clock.getDelayedCalls()) == 1
        wait = self.gate.extend_deadline(self.origin, 20.0)
        assert wait.deadline == 20.0
        # Extending reschedules the single shared timer instead of adding one.
        assert len(self.clock.getDelayedCalls()) == 1
        assert self.clock.getDelayedCalls()[0].getTime() == 20.0

        task = asyncio.ensure_future(self.gate.wait(wait))
        await asyncio.sleep(0)
        self.clock.advance(10)
        await asyncio.sleep(0)
        assert not task.done()
        self.clock.advance(10)
        await asyncio.sleep(0)
        assert task.done()

    @coroutine_test
    async def test_earlier_deadline_does_not_shorten(self) -> None:
        wait = self.gate.extend_deadline(self.origin, 20.0)
        wait = self.gate.extend_deadline(self.origin, 10.0)
        assert wait.deadline == 20.0
        assert len(self.clock.getDelayedCalls()) == 1
        assert self.clock.getDelayedCalls()[0].getTime() == 20.0

    @coroutine_test
    async def test_simultaneous_release(self) -> None:
        wait = self.gate.extend_deadline(self.origin, 10.0)
        self.gate.extend_deadline(self.origin, 20.0)
        task1 = asyncio.ensure_future(self.gate.wait(wait))
        task2 = asyncio.ensure_future(self.gate.wait(wait))
        await asyncio.sleep(0)
        assert len(wait.waiters) == 2
        self.clock.advance(20)
        await asyncio.sleep(0)
        assert task1.done()
        assert task2.done()
        assert not self.gate._waits

    @coroutine_test
    async def test_cancel_removes_only_own_waiter(self) -> None:
        wait = self.gate.extend_deadline(self.origin, 10.0)
        task1 = asyncio.ensure_future(self.gate.wait(wait))
        task2 = asyncio.ensure_future(self.gate.wait(wait))
        await asyncio.sleep(0)
        task1.cancel()
        await asyncio.sleep(0.05)
        with pytest.raises(asyncio.CancelledError):
            await task1
        assert len(wait.waiters) == 1
        # The shared timer survives the cancellation.
        assert len(self.clock.getDelayedCalls()) == 1
        self.clock.advance(10)
        await task2
        assert self.crawler.stats.get_value("retry/wait/cancelled") == 1

    @coroutine_test
    async def test_close_releases_all(self) -> None:
        wait1 = self.gate.extend_deadline(self.origin, 10.0)
        other = ("https", "other.example", 8443)
        wait2 = self.gate.extend_deadline(other, 30.0)
        task1 = asyncio.ensure_future(self.gate.wait(wait1))
        task2 = asyncio.ensure_future(self.gate.wait(wait2))
        await asyncio.sleep(0)
        self.gate.close()
        await asyncio.sleep(0)
        assert task1.done()
        assert task2.done()
        assert not self.gate._waits
        assert not self.clock.getDelayedCalls()
        # Closing an empty gate is a no-op.
        self.gate.close()

    @coroutine_test
    async def test_origins_are_independent(self) -> None:
        first = ("http", "first.example", None)
        second = ("http", "second.example", None)
        wait1 = self.gate.extend_deadline(first, 10.0)
        wait2 = self.gate.extend_deadline(second, 20.0)
        task1 = asyncio.ensure_future(self.gate.wait(wait1))
        task2 = asyncio.ensure_future(self.gate.wait(wait2))
        await asyncio.sleep(0)
        self.clock.advance(10)
        await asyncio.sleep(0)
        assert task1.done()
        assert not task2.done()
        self.clock.advance(10)
        await asyncio.sleep(0)
        assert task2.done()

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (("http", "example.com", None), ("https", "example.com", None)),
            (("http", "a.example", None), ("http", "b.example", None)),
            (("http", "example.com", 80), ("http", "example.com", 8080)),
        ],
    )
    def test_origin_keys_never_share(
        self,
        left: tuple[str, str, int | None],
        right: tuple[str, str, int | None],
    ) -> None:
        self.gate.extend_deadline(left, 10.0)
        self.gate.extend_deadline(right, 20.0)
        assert set(self.gate._waits) == {left, right}
        assert len(self.clock.getDelayedCalls()) == 2

    @coroutine_test
    async def test_clock_jump_backwards_keeps_deadline(self) -> None:
        wait = self.gate.extend_deadline(self.origin, 10.0)
        task = asyncio.ensure_future(self.gate.wait(wait))
        await asyncio.sleep(0)
        self.clock.rightNow = 0.0
        self.clock.advance(5)
        await asyncio.sleep(0)
        assert not task.done()
        self.clock.advance(5)
        await asyncio.sleep(0)
        assert task.done()


def _response(
    url: str = "http://example.com/429",
    retry_after: str | bytes | None = None,
) -> Response:
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return Response(url, body=b"", status=429, headers=headers)


class TestRetryAfterMiddleware:
    def _middleware(self, settings: dict[str, Any] | None = None) -> RetryMiddleware:
        config = {"RETRY_RETRY_AFTER_ENABLED": True}
        if settings:
            config.update(settings)
        crawler = get_crawler(DefaultSpider, config)
        crawler.spider = crawler._create_spider()
        mw = RetryMiddleware(crawler.settings, crawler.stats, clock=Clock())
        mw.crawler = crawler
        return mw

    @staticmethod
    def _clock(mw: RetryMiddleware) -> Clock:
        assert mw._gate is not None
        return cast("Clock", mw._gate._clock)

    @coroutine_test
    async def test_disabled_by_default(self) -> None:
        crawler = get_crawler(DefaultSpider)
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(RetryMiddleware, crawler)
        assert mw._gate is None
        req = Request("http://example.com/429")
        result = await mw.process_response(req, _response())
        assert isinstance(result, Request)
        assert crawler.stats.get_value("retry/wait/count") is None

    @coroutine_test
    async def test_wait_then_retry(self) -> None:
        mw = self._middleware()
        clock = self._clock(mw)
        req = Request("http://example.com/429")
        task = asyncio.ensure_future(
            mw.process_response(req, _response(retry_after="10"))
        )
        await asyncio.sleep(0)
        assert not task.done()
        clock.advance(10)
        result = await task
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert mw.crawler.stats.get_value("retry/wait/count") == 1
        assert mw.crawler.stats.get_value("retry/count") == 1
        # The gate state is cleaned up once the wait ends.
        assert not mw._gate._waits  # type: ignore[union-attr]
        assert not clock.getDelayedCalls()

    @coroutine_test
    async def test_extension_shared_between_requests(self) -> None:
        mw = self._middleware()
        clock = self._clock(mw)
        req1 = Request("http://example.com/a")
        req2 = Request("http://example.com/b")
        task1 = asyncio.ensure_future(
            mw.process_response(req1, _response(req1.url, retry_after="20"))
        )
        await asyncio.sleep(0)
        # The second request asks for less but joins the later shared
        # deadline, so its own wait is extended by the gate.
        task2 = asyncio.ensure_future(
            mw.process_response(req2, _response(req2.url, retry_after="10"))
        )
        await asyncio.sleep(0)
        assert mw.crawler.stats.get_value("retry/wait/extended") == 1
        clock.advance(10)
        await asyncio.sleep(0)
        assert not task1.done()
        assert not task2.done()
        clock.advance(10)
        result1, result2 = await asyncio.gather(task1, task2)
        assert isinstance(result1, Request)
        assert isinstance(result2, Request)

    @coroutine_test
    async def test_cancel_does_not_retry_or_count(self) -> None:
        mw = self._middleware()
        clock = self._clock(mw)
        req = Request("http://example.com/429")
        task = asyncio.ensure_future(
            mw.process_response(req, _response(retry_after="10"))
        )
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.05)
        with pytest.raises(asyncio.CancelledError):
            await task
        # No retry request was produced, so no retry stat was counted.
        assert mw.crawler.stats.get_value("retry/count") is None
        assert mw.crawler.stats.get_value("retry/wait/cancelled") == 1
        # Cancelling removes only the waiter: the shared origin timer stays
        # until its deadline, then cleans the gate entry up by itself.
        assert len(clock.getDelayedCalls()) == 1
        assert clock.getDelayedCalls()[0].active()
        clock.advance(10)
        assert not clock.getDelayedCalls()
        assert not mw._gate._waits  # type: ignore[union-attr]

    @coroutine_test
    async def test_spider_closed_cancels_wait(self) -> None:
        mw = self._middleware()
        clock = self._clock(mw)
        req = Request("http://example.com/429")
        task = asyncio.ensure_future(
            mw.process_response(req, _response(retry_after="10"))
        )
        await asyncio.sleep(0)
        mw.spider_closed()
        result = await task
        # Released by the close, the request still retries normally.
        assert isinstance(result, Request)
        assert not clock.getDelayedCalls()
        assert not mw._gate._waits  # type: ignore[union-attr]

    @coroutine_test
    async def test_cross_origin_waits_are_independent(self) -> None:
        mw = self._middleware()
        clock = self._clock(mw)
        req1 = Request("http://first.example/429")
        req2 = Request("http://second.example/429")
        task1 = asyncio.ensure_future(
            mw.process_response(req1, _response(req1.url, retry_after="10"))
        )
        task2 = asyncio.ensure_future(
            mw.process_response(req2, _response(req2.url, retry_after="20"))
        )
        await asyncio.sleep(0)
        clock.advance(10)
        result1 = await task1
        assert isinstance(result1, Request)
        assert not task2.done()
        clock.advance(10)
        result2 = await task2
        assert isinstance(result2, Request)

    @coroutine_test
    async def test_per_request_and_per_origin_counts(self) -> None:
        mw = self._middleware()
        clock = self._clock(mw)
        stats = mw.crawler.stats
        # Two requests of one origin, one of another, with distinct deadlines.
        sequence = [
            (Request("http://first.example/a"), "10"),
            (Request("https://second.example:8443/b"), "5"),
            (Request("http://first.example/c"), "20"),
        ]
        tasks = [
            asyncio.ensure_future(
                mw.process_response(req, _response(req.url, retry_after=after))
            )
            for req, after in sequence
        ]
        await asyncio.sleep(0)
        # Per-origin counts accumulate independently of the shared deadlines.
        assert stats.get_value("retry/wait/origin_count/http://first.example") == 2
        assert (
            stats.get_value("retry/wait/origin_count/https://second.example:8443") == 1
        )
        # The per-request total matches the sum of the per-origin counts.
        assert stats.get_value("retry/wait/count") == 3
        clock.advance(20)
        results = await asyncio.gather(*tasks)
        assert all(isinstance(r, Request) for r in results)

    @pytest.mark.parametrize(
        ("left_url", "right_url"),
        [
            ("http://example.com/a", "https://example.com/a"),
            ("http://example.com:80/a", "http://example.com:8080/a"),
            ("http://a.example/a", "http://b.example/a"),
        ],
    )
    @coroutine_test
    async def test_origin_identity(self, left_url: str, right_url: str) -> None:
        mw = self._middleware()
        clock = self._clock(mw)
        task1 = asyncio.ensure_future(
            mw.process_response(
                Request(left_url), _response(left_url, retry_after="20")
            )
        )
        await asyncio.sleep(0)
        task2 = asyncio.ensure_future(
            mw.process_response(
                Request(right_url), _response(right_url, retry_after="10")
            )
        )
        await asyncio.sleep(0)
        # Neither origin extends the other: both keep their own deadline.
        assert mw.crawler.stats.get_value("retry/wait/extended") is None
        clock.advance(10)
        result2 = await task2
        assert isinstance(result2, Request)
        assert not task1.done()
        clock.advance(10)
        result1 = await task1
        assert isinstance(result1, Request)

    @pytest.mark.parametrize("bad_header", ["not a number", "-5", b"\xff\xfe"])
    @coroutine_test
    async def test_invalid_header_does_not_block(self, bad_header: str | bytes) -> None:
        mw = self._middleware()
        req = Request("http://example.com/429")
        result = await mw.process_response(req, _response(retry_after=bad_header))
        assert isinstance(result, Request)
        assert mw.crawler.stats.get_value("retry/wait/invalid_header") == 1
        assert mw.crawler.stats.get_value("retry/count") == 1
        assert not mw._gate._waits  # type: ignore[union-attr]

    @coroutine_test
    async def test_missing_header_does_not_wait_or_count(self) -> None:
        mw = self._middleware()
        req = Request("http://example.com/429")
        result = await mw.process_response(req, _response())
        assert isinstance(result, Request)
        assert mw.crawler.stats.get_value("retry/wait/count") is None
        assert mw.crawler.stats.get_value("retry/wait/invalid_header") is None

    @coroutine_test
    async def test_max_wait_caps_single_wait(self) -> None:
        mw = self._middleware({"RETRY_RETRY_AFTER_MAX_WAIT": 5})
        clock = self._clock(mw)
        req = Request("http://example.com/429")
        task = asyncio.ensure_future(
            mw.process_response(req, _response(retry_after="100"))
        )
        await asyncio.sleep(0)
        clock.advance(5)
        result = await task
        assert isinstance(result, Request)
        assert mw.crawler.stats.get_value("retry/wait/limit_reached") is None

    @coroutine_test
    async def test_disabled_limits_keep_full_wait(self) -> None:
        mw = self._middleware(
            {
                "RETRY_RETRY_AFTER_MAX_WAIT": -1,
                "RETRY_RETRY_AFTER_MAX_TOTAL_WAIT": -1,
            }
        )
        clock = self._clock(mw)
        req = Request("http://example.com/429")
        task = asyncio.ensure_future(
            mw.process_response(req, _response(retry_after="100"))
        )
        await asyncio.sleep(0)
        # Neither cap applies: the full requested wait is scheduled, and no
        # per-request deadline is stamped.
        assert clock.getDelayedCalls()[0].getTime() == 100.0
        assert "_retry_after_deadline" not in req.meta
        clock.advance(100)
        result = await task
        assert isinstance(result, Request)

    @coroutine_test
    async def test_negative_retry_after_does_not_wait(self) -> None:
        mw = self._middleware()
        req = Request("http://example.com/429")
        # A past HTTP date yields a negative delay and is treated as invalid.
        http_date = format_datetime(datetime.now(UTC) - timedelta(seconds=30))
        result = await mw.process_response(req, _response(retry_after=http_date))
        assert isinstance(result, Request)
        assert mw.crawler.stats.get_value("retry/wait/invalid_header") == 1
        assert not mw._gate._waits  # type: ignore[union-attr]

    @coroutine_test
    async def test_max_total_wait_spans_retries(self) -> None:
        mw = self._middleware(
            {
                "RETRY_RETRY_AFTER_MAX_TOTAL_WAIT": 15,
                "RETRY_RETRY_AFTER_MAX_WAIT": 10,
                "RETRY_TIMES": 5,
            }
        )
        clock = self._clock(mw)

        req = Request("http://example.com/429")
        task = asyncio.ensure_future(
            mw.process_response(req, _response(retry_after="10"))
        )
        await asyncio.sleep(0)
        clock.advance(10)
        req = cast("Request", await task)
        assert req.meta["_retry_after_deadline"] == 15.0

        # The second 429 asks for another 10s, but only 5s of the total budget
        # remain, so the wait is cut to the total deadline.
        task = asyncio.ensure_future(
            mw.process_response(req, _response(retry_after="10"))
        )
        await asyncio.sleep(0)
        assert len(clock.getDelayedCalls()) == 1
        assert clock.getDelayedCalls()[0].getTime() == 15.0
        clock.advance(5)
        result = await task
        assert isinstance(result, Request)

    @coroutine_test
    async def test_total_budget_exhausted_skips_gate(self) -> None:
        # A total budget of 0 still allows the first wait, which is bounded by
        # the per-wait cap; every later wait then has no budget left.
        mw = self._middleware(
            {
                "RETRY_RETRY_AFTER_MAX_TOTAL_WAIT": 0,
                "RETRY_RETRY_AFTER_MAX_WAIT": 10,
                "RETRY_TIMES": 5,
            }
        )
        clock = self._clock(mw)

        req = Request("http://example.com/429")
        task = asyncio.ensure_future(
            mw.process_response(req, _response(retry_after="100"))
        )
        await asyncio.sleep(0)
        assert clock.getDelayedCalls()[0].getTime() == 10.0
        clock.advance(10)
        req = cast("Request", await task)
        # The total budget started empty: the stamp is the first wait's start.
        assert req.meta["_retry_after_deadline"] == 0.0

        # No total budget remains: the second wait skips the gate entirely.
        result = await mw.process_response(req, _response(retry_after="100"))
        assert isinstance(result, Request)
        assert mw.crawler.stats.get_value("retry/wait/limit_reached") == 1
        assert not mw._gate._waits  # type: ignore[union-attr]

    @coroutine_test
    async def test_max_retries_intersects_and_returns_response(self) -> None:
        mw = self._middleware({"RETRY_TIMES": 0})
        clock = self._clock(mw)
        req = Request("http://example.com/429")
        task = asyncio.ensure_future(
            mw.process_response(req, _response(retry_after="10"))
        )
        await asyncio.sleep(0)
        clock.advance(10)
        response = await task
        # Retries exhausted: the real 429 response comes back.
        assert isinstance(response, Response)
        assert response.status == 429
        assert mw.crawler.stats.get_value("retry/max_reached") == 1
        assert mw.crawler.stats.get_value("retry/count") is None

    @coroutine_test
    async def test_stats_counted_once_per_actual_retry(self) -> None:
        mw = self._middleware()
        req = Request("http://example.com/429")
        response = _response(retry_after="0")
        result1 = await mw.process_response(req, response)
        assert isinstance(result1, Request)
        # Processing the same response object again must not add another wait
        # stat, and each call still produces its own single retry count.
        result2 = await mw.process_response(req, response)
        assert isinstance(result2, Request)
        assert mw.crawler.stats.get_value("retry/count") == 2
        assert mw.crawler.stats.get_value("retry/wait/count") is None
        # A zero header is an immediate retry, not a capped wait.
        assert mw.crawler.stats.get_value("retry/wait/limit_reached") is None
        assert mw.crawler.stats.get_value("retry/wait/invalid_header") is None

    @coroutine_test
    async def test_positive_wait_capped_to_zero_counts_limit(self) -> None:
        mw = self._middleware(
            {"RETRY_RETRY_AFTER_MAX_TOTAL_WAIT": 0, "RETRY_RETRY_AFTER_MAX_WAIT": 10}
        )
        clock = self._clock(mw)
        req = Request("http://example.com/429")
        first = asyncio.ensure_future(
            mw.process_response(req, _response(retry_after="5"))
        )
        await asyncio.sleep(0)
        clock.advance(5)
        req = cast("Request", await first)
        # The total budget is spent: a later positive wait is cut to nothing.
        result = await mw.process_response(req, _response(retry_after="5"))
        assert isinstance(result, Request)
        assert mw.crawler.stats.get_value("retry/wait/limit_reached") == 1
        assert not mw._gate._waits  # type: ignore[union-attr]

    @coroutine_test
    async def test_request_meta_and_headers_not_polluted(self) -> None:
        mw = self._middleware()
        clock = self._clock(mw)
        original_meta = dict(req_meta := {"custom": "value"})
        req = Request("http://example.com/429", meta=req_meta)
        original_headers = dict(req.headers)
        task = asyncio.ensure_future(
            mw.process_response(req, _response(retry_after="10"))
        )
        await asyncio.sleep(0)
        # The gate bookkeeping stays off the user-visible request headers and
        # only adds the private deadline key to the meta copy.
        assert dict(req.headers) == original_headers
        clock.advance(10)
        result = await task
        assert isinstance(result, Request)
        assert result.meta["custom"] == original_meta["custom"]
        assert result.url == req.url

    @coroutine_test
    async def test_http_date_header(self) -> None:
        mw = self._middleware()
        clock = self._clock(mw)
        http_date = format_datetime(datetime.now(UTC) + timedelta(seconds=10))
        req = Request("http://example.com/429")
        task = asyncio.ensure_future(
            mw.process_response(req, _response(retry_after=http_date))
        )
        await asyncio.sleep(0)
        assert not task.done()
        clock.advance(10.1)
        result = await task
        assert isinstance(result, Request)
