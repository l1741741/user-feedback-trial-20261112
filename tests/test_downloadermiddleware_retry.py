from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

import pytest
from twisted.internet.error import ConnectError, ConnectionDone, ConnectionLost

import scrapy.downloadermiddlewares.retry
from scrapy.core.scheduler import Scheduler
from scrapy.downloadermiddlewares.retry import (
    RetryMiddleware,
    _request_origin,
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
from scrapy.settings.default_settings import (
    RETRY_AFTER_ENABLED,
    RETRY_AFTER_MAX_DELAY,
    RETRY_EXCEPTIONS,
)
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

    def test_priority_adjust(self):
        req = Request("http://www.scrapytest.org/503")
        rsp = Response("http://www.scrapytest.org/503", body=b"", status=503)
        req2 = self.mw.process_response(req, rsp)
        assert isinstance(req2, Request)
        assert req2.priority < req.priority

    def test_404(self):
        req = Request("http://www.scrapytest.org/404")
        rsp = Response("http://www.scrapytest.org/404", body=b"", status=404)

        # dont retry 404s
        assert self.mw.process_response(req, rsp) is rsp

    def test_dont_retry(self):
        req = Request("http://www.scrapytest.org/503", meta={"dont_retry": True})
        rsp = Response("http://www.scrapytest.org/503", body=b"", status=503)

        # no retry
        r = self.mw.process_response(req, rsp)
        assert r is rsp

        # Test retry when dont_retry set to False
        req = Request("http://www.scrapytest.org/503", meta={"dont_retry": False})
        rsp = Response("http://www.scrapytest.org/503", body=b"", status=503)

        # first retry
        req2 = self.mw.process_response(req, rsp)
        assert isinstance(req2, Request)
        assert req2.meta["retry_times"] == 1

    def test_dont_retry_exc(self):
        req = Request("http://www.scrapytest.org/503", meta={"dont_retry": True})

        r = self.mw.process_exception(req, CannotResolveHostError())
        assert r is None

    def test_503(self):
        req = Request("http://www.scrapytest.org/503")
        rsp = Response("http://www.scrapytest.org/503", body=b"", status=503)

        # first retry
        req2 = self.mw.process_response(req, rsp)
        assert isinstance(req2, Request)
        assert req2.meta["retry_times"] == 1

        # second retry
        req3 = self.mw.process_response(req2, rsp)
        assert isinstance(req3, Request)
        assert req3.meta["retry_times"] == 2

        # discard it
        assert self.mw.process_response(req3, rsp) is rsp

        assert self.crawler.stats.get_value("retry/max_reached") == 1
        assert (
            self.crawler.stats.get_value("retry/reason_count/503 Service Unavailable")
            == 2
        )
        assert self.crawler.stats.get_value("retry/count") == 2

    def test_give_up_log_level_setting(self, caplog: pytest.LogCaptureFixture) -> None:
        crawler = get_crawler(
            DefaultSpider, settings_dict={"RETRY_GIVE_UP_LOG_LEVEL": "WARNING"}
        )
        crawler.spider = crawler._create_spider()
        mw = build_from_crawler(RetryMiddleware, crawler)
        mw.max_retry_times = 0
        req = Request("http://example.com/503")
        rsp = Response("http://example.com/503", body=b"", status=503)
        with caplog.at_level(logging.WARNING):
            assert mw.process_response(req, rsp) is rsp
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.WARNING,
            f"Gave up retrying {req} (failed 1 times): 503 Service Unavailable",
        ) in caplog.record_tuples

    def test_give_up_log_level_meta(self, caplog: pytest.LogCaptureFixture) -> None:
        self.mw.max_retry_times = 0
        req = Request("http://example.com/503", meta={"give_up_log_level": "WARNING"})
        rsp = Response("http://example.com/503", body=b"", status=503)
        with caplog.at_level(logging.WARNING):
            assert self.mw.process_response(req, rsp) is rsp
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


class _FakeClock:
    """Wall and monotonic clocks that only move when the test moves them."""

    def __init__(self) -> None:
        self.wall = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        self.monotonic = 1000.0
        self.waits: list[tuple[float, asyncio.Event]] = []

    def utcnow(self) -> datetime:
        return self.wall

    async def sleep(self, seconds: float) -> None:
        event = asyncio.Event()
        self.waits.append((seconds, event))
        await event.wait()

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += timedelta(seconds=seconds)
        waits, self.waits = self.waits, []
        for _, event in waits:
            event.set()

    @property
    def requested(self) -> list[float]:
        return [seconds for seconds, _ in self.waits]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    module = scrapy.downloadermiddlewares.retry
    monkeypatch.setattr(module, "_monotonic", lambda: fake.monotonic)
    monkeypatch.setattr(module, "_utcnow", fake.utcnow)
    monkeypatch.setattr(module, "_sleep", fake.sleep)
    return fake


async def _settle() -> None:
    """Let every coroutine that is ready run."""
    for _ in range(20):
        await asyncio.sleep(0)


def _build_retry_middleware(
    settings: dict[str, Any] | None = None,
) -> RetryMiddleware:
    crawler = get_crawler(DefaultSpider, settings or {})
    crawler.spider = crawler._create_spider()
    return build_from_crawler(RetryMiddleware, crawler)


def _retry_after_ready(
    settings: dict[str, Any] | None = None,
) -> RetryMiddleware:
    return _build_retry_middleware({"RETRY_AFTER_ENABLED": True, **(settings or {})})


def _response(
    url: str,
    status: int = 503,
    retry_after: Any = None,
    headers: dict[str, Any] | None = None,
) -> Response:
    all_headers: dict[str, Any] = dict(headers or {})
    if retry_after is not None:
        all_headers["Retry-After"] = retry_after
    return Response(url, status=status, headers=all_headers)


_HTTP_DATE_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _http_date(offset: float = 0, tz: timezone | None = UTC) -> str:
    """Return an HTTP date *offset* seconds from the fake wall clock."""
    return format_datetime(
        _HTTP_DATE_BASE + timedelta(seconds=offset), usegmt=tz is UTC
    )


async def _awaited(func: Any, *args: Any) -> Any:
    result = func(*args)
    if isinstance(result, (Request, Response)):
        return result
    return await result


def _start(func: Any, *args: Any) -> asyncio.Task[Any]:
    """Call *func*, whether it returns an awaitable or not, as a task."""
    return asyncio.ensure_future(_awaited(func, *args))


async def _drive(clock: _FakeClock, result: Any) -> Any:
    """Await *result*, moving the fake clock past every sleep it takes."""
    if isinstance(result, (Request, Response)):
        return result
    task = asyncio.ensure_future(result)
    await _settle()
    while not task.done():
        assert clock.waits, "the middleware is neither done nor sleeping"
        clock.advance(clock.waits[0][0])
        await _settle()
    return task.result()


def test_default_settings() -> None:
    assert RETRY_AFTER_ENABLED is False
    assert RETRY_AFTER_MAX_DELAY == 120.0


class TestRetryAfterDisabled:
    def test_synchronous_retry(self) -> None:
        mw = _build_retry_middleware()
        request = Request("http://example.com/")
        result = mw.process_response(request, _response(request.url, retry_after="1"))
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert mw._retry_after_deadlines == {}
        assert mw.crawler.stats.get_value("retry_after/delayed") is None

    def test_synchronous_non_retry(self) -> None:
        mw = _build_retry_middleware()
        request = Request("http://example.com/")
        response = _response(request.url, status=404, retry_after="1")
        assert mw.process_response(request, response) is response
        assert mw._retry_after_deadlines == {}


class TestRetryAfterOrigin:
    def test_without_hostname(self) -> None:
        request = Request("file:///tmp/x")
        assert _request_origin(request) is None

        mw = _retry_after_ready()
        response = _response(request.url, retry_after="10")
        result = mw.process_response(request, response)
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert mw._retry_after_deadlines == {}

    def test_default_ports(self) -> None:
        for url, expected in (
            ("http://example.com/", ("http", "example.com", 80)),
            ("https://example.com/", ("https", "example.com", 443)),
            ("http://example.com:8080/", ("http", "example.com", 8080)),
            ("http://EXAMPLE.com/", ("http", "example.com", 80)),
        ):
            request = Request(url)
            assert _request_origin(request) == expected

    def test_explicit_default_port_matches_implicit(self) -> None:
        assert _request_origin(Request("http://example.com:80/")) == _request_origin(
            Request("http://example.com/")
        )
        assert _request_origin(Request("https://example.com:443/")) == _request_origin(
            Request("https://example.com/")
        )


@pytest.mark.only_asyncio
class TestRetryAfter:
    """Tests using the fake clock need the asyncio event loop."""

    ORIGIN = ("http", "example.com", 80)

    @coroutine_test
    async def test_seconds(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        result = await _drive(
            clock,
            mw.process_response(request, _response(request.url, retry_after="2")),
        )
        assert isinstance(result, Request)
        assert result.meta["retry_times"] == 1
        assert result.meta["skip_dupefilter_once"] is True
        assert result.priority == request.priority - 1
        assert mw.crawler.stats.get_value("retry_after/delayed") == 1
        assert mw.crawler.stats.get_value("retry/count") == 1
        assert (
            mw.crawler.stats.get_value("retry/reason_count/503 Service Unavailable")
            == 1
        )
        assert mw._retry_after_deadlines == {}
        assert clock.monotonic == 1002.0

    @coroutine_test
    async def test_whitespace(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        result = await _drive(
            clock,
            mw.process_response(request, _response(request.url, retry_after=" \t1 \t")),
        )
        assert isinstance(result, Request)
        assert clock.monotonic == 1001.0

    @coroutine_test
    async def test_http_date(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        result = await _drive(
            clock,
            mw.process_response(
                request, _response(request.url, retry_after=_http_date(3))
            ),
        )
        assert isinstance(result, Request)
        assert clock.monotonic == 1003.0

    @coroutine_test
    async def test_http_date_with_non_utc_timezone(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        tz = timezone(timedelta(hours=2))
        date = format_datetime(datetime(2026, 1, 1, 14, 0, 1, tzinfo=tz))
        request = Request("http://example.com/")
        result = await _drive(
            clock,
            mw.process_response(request, _response(request.url, retry_after=date)),
        )
        assert isinstance(result, Request)
        assert clock.monotonic == 1001.0

    @coroutine_test
    async def test_expired_date(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        result = await _drive(
            clock,
            mw.process_response(
                request, _response(request.url, retry_after=_http_date(-60))
            ),
        )
        assert isinstance(result, Request)
        assert clock.monotonic == 1000.0
        assert mw._retry_after_deadlines == {}
        assert mw.crawler.stats.get_value("retry_after/delayed") is None

    @coroutine_test
    async def test_zero_delay(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        result = await _drive(
            clock,
            mw.process_response(request, _response(request.url, retry_after="0")),
        )
        assert isinstance(result, Request)
        assert clock.monotonic == 1000.0
        assert mw._retry_after_deadlines == {}
        assert mw.crawler.stats.get_value("retry_after/delayed") is None

    @coroutine_test
    async def test_zero_delay_with_existing_window(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        mw._retry_after_deadlines[self.ORIGIN] = 1100.0
        request = Request("http://example.com/")
        result = await _drive(
            clock,
            mw.process_response(request, _response(request.url, retry_after="0")),
        )
        # A zero delay never waits, and does not modify the origin window.
        assert isinstance(result, Request)
        assert clock.requested == []
        assert mw._retry_after_deadlines == {self.ORIGIN: 1100.0}
        assert mw.crawler.stats.get_value("retry_after/delayed") is None

    @coroutine_test
    async def test_expired_date_with_existing_window(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        mw._retry_after_deadlines[self.ORIGIN] = 900.0
        request = Request("http://example.com/")
        result = await _drive(
            clock,
            mw.process_response(
                request, _response(request.url, retry_after=_http_date(-60))
            ),
        )
        # An expired window is pruned and an expired date adds nothing.
        assert isinstance(result, Request)
        assert clock.requested == []
        assert mw._retry_after_deadlines == {}

    @coroutine_test
    async def test_short_header_within_existing_window(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        first = Request("http://example.com/")
        _start(mw.process_response, first, _response(first.url, retry_after="5"))
        await _settle()

        request = Request("http://example.com/")
        task = _start(
            mw.process_response, request, _response(request.url, retry_after="1")
        )
        await _settle()
        # The longer registered window wins and both responses wait for it.
        assert clock.requested == [5.0, 5.0]
        await _drive(clock, task)
        assert clock.monotonic == 1005.0
        assert mw._retry_after_deadlines == {}

    @coroutine_test
    async def test_max_delay(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready({"RETRY_AFTER_MAX_DELAY": 1.5})
        request = Request("http://example.com/")
        await _drive(
            clock,
            mw.process_response(request, _response(request.url, retry_after="3600")),
        )
        assert clock.monotonic == 1001.5

    @coroutine_test
    async def test_max_delay_fractional(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready({"RETRY_AFTER_MAX_DELAY": "0.25"})
        request = Request("http://example.com/")
        await _drive(
            clock,
            mw.process_response(request, _response(request.url, retry_after="10")),
        )
        assert clock.monotonic == 1000.25

    @coroutine_test
    async def test_max_delay_caps_http_date(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready({"RETRY_AFTER_MAX_DELAY": 2})
        request = Request("http://example.com/")
        await _drive(
            clock,
            mw.process_response(
                request, _response(request.url, retry_after=_http_date(3600))
            ),
        )
        assert clock.monotonic == 1002.0

    @pytest.mark.parametrize(
        "value",
        [
            b"-1",
            b"+1",
            b"1.5",
            b"1e3",
            b"nan",
            b"inf",
            b"",
            b"   ",
            b"abc",
            b"Wed, 21 Oct 2015 07:28:00",  # HTTP date without a time zone
            b"1, 2",
            b"\xff",
        ],
    )
    @coroutine_test
    async def test_invalid_header(self, clock: _FakeClock, value: bytes) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        result = await _drive(
            clock,
            mw.process_response(request, _response(request.url, retry_after=value)),
        )
        assert isinstance(result, Request)
        assert clock.monotonic == 1000.0
        assert mw._retry_after_deadlines == {}
        assert mw.crawler.stats.get_value("retry_after/delayed") is None
        assert mw.crawler.stats.get_value("retry/count") == 1

    @coroutine_test
    async def test_multiple_headers(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        response = _response(request.url, headers={"Retry-After": ["1", "2"]})
        result = await _drive(clock, mw.process_response(request, response))
        assert isinstance(result, Request)
        assert clock.monotonic == 1000.0
        assert mw._retry_after_deadlines == {}

    def test_dont_retry(self) -> None:
        mw = _retry_after_ready()
        mw._retry_after_deadlines[self.ORIGIN] = 1100.0
        request = Request("http://example.com/", meta={"dont_retry": True})
        response = _response(request.url, retry_after="10")
        assert mw.process_response(request, response) is response
        # Neither a new window nor a change to the existing one.
        assert mw._retry_after_deadlines == {self.ORIGIN: 1100.0}

    @pytest.mark.parametrize("status", [200, 404, 400])
    def test_not_retryable_status(self, status: int) -> None:
        mw = _retry_after_ready()
        mw._retry_after_deadlines[self.ORIGIN] = 1100.0
        request = Request("http://example.com/")
        response = _response(request.url, status=status, retry_after="10")
        assert mw.process_response(request, response) is response
        assert mw._retry_after_deadlines == {self.ORIGIN: 1100.0}

    def test_no_budget(self) -> None:
        mw = _retry_after_ready({"RETRY_TIMES": 0})
        mw._retry_after_deadlines[self.ORIGIN] = 1100.0
        request = Request("http://example.com/")
        response = _response(request.url, retry_after="10")
        assert mw.process_response(request, response) is response
        assert mw._retry_after_deadlines == {self.ORIGIN: 1100.0}
        assert mw.crawler.stats.get_value("retry/max_reached") == 1
        assert mw.crawler.stats.get_value("retry_after/delayed") is None

    def test_no_budget_from_meta(self) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/", meta={"retry_times": 2})
        response = _response(request.url, retry_after="10")
        assert mw.process_response(request, response) is response
        assert mw._retry_after_deadlines == {}

    def test_exception_does_not_wait(self) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        result = mw.process_exception(request, CannotResolveHostError())
        assert isinstance(result, Request)
        assert mw._retry_after_deadlines == {}
        assert mw.crawler.stats.get_value("retry_after/delayed") is None

    @coroutine_test
    async def test_original_request_unchanged(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        request = Request(
            "http://example.com/",
            method="POST",
            body=b"payload",
            headers={"X-Test": "1"},
            meta={"custom": "value"},
            cb_kwargs={"a": 1},
        )
        original = request.copy()
        result = await _drive(
            clock,
            mw.process_response(request, _response(request.url, retry_after="1")),
        )
        assert isinstance(result, Request)
        for attribute in (
            "method",
            "body",
            "headers",
            "callback",
            "cb_kwargs",
            "meta",
        ):
            assert getattr(request, attribute) == getattr(original, attribute)
        assert result.body == original.body
        assert result.headers == original.headers
        assert result.callback is original.callback
        assert result.cb_kwargs == original.cb_kwargs
        assert result.meta["custom"] == "value"

    @coroutine_test
    async def test_other_origins_unaffected(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        first = Request("http://example.com/")
        _start(mw.process_response, first, _response(first.url, retry_after="5"))
        await _settle()
        assert self.ORIGIN in mw._retry_after_deadlines

        for url in (
            "http://other.example.com/",
            "https://example.com/",
            "http://example.com:8080/",
        ):
            other = Request(url)
            result = mw.process_response(other, _response(other.url))
            assert isinstance(result, Request)
        assert set(mw._retry_after_deadlines) == {self.ORIGIN}

    @coroutine_test
    async def test_same_origin_shares_window(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        first = Request("http://example.com/a")
        _start(mw.process_response, first, _response(first.url, retry_after="2"))
        await _settle()

        second = Request("http://EXAMPLE.com/b")
        task = _start(mw.process_response, second, _response(second.url))
        await _settle()
        # The second response has no header of its own and waits for the
        # remaining part of the window that the first response registered.
        assert clock.requested == [2.0, 2.0]
        await _drive(clock, task)
        assert clock.monotonic == 1002.0
        assert mw.crawler.stats.get_value("retry_after/delayed") == 2

    @coroutine_test
    async def test_window_extends_but_never_shrinks(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        first = Request("http://example.com/")
        _start(mw.process_response, first, _response(first.url, retry_after="5"))
        await _settle()
        assert mw._retry_after_deadlines[self.ORIGIN] == 1005.0

        shorter = Request("http://example.com/")
        _start(mw.process_response, shorter, _response(shorter.url, retry_after="1"))
        await _settle()
        assert mw._retry_after_deadlines[self.ORIGIN] == 1005.0

        longer = Request("http://example.com/")
        _start(mw.process_response, longer, _response(longer.url, retry_after="9"))
        await _settle()
        assert mw._retry_after_deadlines[self.ORIGIN] == 1009.0

    @coroutine_test
    async def test_waiter_follows_extension(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        first = Request("http://example.com/a")
        waiter = _start(
            mw.process_response, first, _response(first.url, retry_after="1")
        )
        await _settle()
        assert clock.requested == [1.0]

        extender = Request("http://example.com/b")
        _start(mw.process_response, extender, _response(extender.url, retry_after="3"))
        await _settle()
        assert mw._retry_after_deadlines[self.ORIGIN] == 1003.0

        # Wake the first waiter at its original deadline. It must keep
        # waiting for the extension instead of being released.
        clock.advance(1.0)
        await _settle()
        assert not waiter.done()
        assert clock.requested == [2.0, 2.0]
        assert mw.crawler.stats.get_value("retry_after/delayed") is None

        clock.advance(2.0)
        await _settle()
        assert waiter.done()
        assert isinstance(waiter.result(), Request)

    @coroutine_test
    async def test_cancelled_wait_keeps_window(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        task = _start(
            mw.process_response, request, _response(request.url, retry_after="4")
        )
        await _settle()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The window belongs to the origin, not to the cancelled waiter.
        assert self.ORIGIN in mw._retry_after_deadlines
        assert mw.crawler.stats.get_value("retry_after/delayed") is None
        assert mw.crawler.stats.get_value("retry/count") is None
        assert mw.crawler.stats.get_value("retry/max_reached") is None

        other = Request("http://example.com/other")
        result = await _drive(clock, mw.process_response(other, _response(other.url)))
        assert isinstance(result, Request)
        assert clock.monotonic == 1004.0
        assert mw._retry_after_deadlines == {}

    @coroutine_test
    async def test_wall_clock_jump(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        task = _start(
            mw.process_response,
            request,
            _response(request.url, retry_after=_http_date(10)),
        )
        await _settle()
        assert clock.requested == [10.0]
        assert mw._retry_after_deadlines[self.ORIGIN] == 1010.0

        # The deadline is monotonic: a wall-clock jump neither ends the wait
        # early nor extends it.
        clock.wall += timedelta(hours=1)
        await _settle()
        assert not task.done()
        assert mw._retry_after_deadlines[self.ORIGIN] == 1010.0

        clock.wall -= timedelta(hours=2)
        await _settle()
        assert not task.done()
        assert mw._retry_after_deadlines[self.ORIGIN] == 1010.0

        clock.advance(10.0)
        await _settle()
        assert task.done()
        assert isinstance(task.result(), Request)

    @coroutine_test
    async def test_expired_window_cleaned_up(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        _start(mw.process_response, request, _response(request.url, retry_after="1"))
        await _settle()
        assert mw._retry_after_deadlines == {self.ORIGIN: 1001.0}

        clock.advance(2.0)
        await _settle()
        assert self.ORIGIN not in mw._retry_after_deadlines

        result = mw.process_response(request, _response(request.url))
        assert isinstance(result, Request)
        assert clock.requested == []

    @coroutine_test
    async def test_expired_window_pruned_on_new_response(
        self, clock: _FakeClock
    ) -> None:
        mw = _retry_after_ready()
        other_origin = ("http", "other.example.com", 80)
        mw._retry_after_deadlines[self.ORIGIN] = 1001.0
        mw._retry_after_deadlines[other_origin] = 1100.0
        clock.advance(2.0)

        # Processing a response for the expired origin prunes it without
        # touching the origin that still has a window.
        request = Request("http://example.com/")
        result = mw.process_response(request, _response(request.url))
        assert isinstance(result, Request)
        assert clock.requested == []
        assert mw._retry_after_deadlines == {other_origin: 1100.0}

    @pytest.mark.parametrize(
        "value", [0, -1, float("nan"), float("inf"), float("-inf")]
    )
    def test_max_delay_validation(self, value: float) -> None:
        with pytest.raises(ValueError, match="RETRY_AFTER_MAX_DELAY"):
            _retry_after_ready({"RETRY_AFTER_MAX_DELAY": value})

    def test_max_delay_validated_when_disabled(self) -> None:
        with pytest.raises(ValueError, match="RETRY_AFTER_MAX_DELAY"):
            _build_retry_middleware({"RETRY_AFTER_MAX_DELAY": -1})

    @coroutine_test
    async def test_meta_preserved(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready({"RETRY_PRIORITY_ADJUST": -3})
        request = Request(
            "http://example.com/",
            meta={"max_retry_times": 4, "priority_adjust": 7},
            priority=10,
        )
        result = await _drive(
            clock,
            mw.process_response(request, _response(request.url, retry_after="1")),
        )
        assert isinstance(result, Request)
        assert result.meta["max_retry_times"] == 4
        assert result.priority == 17

    @coroutine_test
    async def test_give_up_log_level_meta_no_wait(
        self, clock: _FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mw = _retry_after_ready()
        mw.max_retry_times = 0
        request = Request("http://example.com/", meta={"give_up_log_level": "WARNING"})
        response = _response(request.url, retry_after="1")
        with caplog.at_level(logging.WARNING):
            result = await _drive(clock, mw.process_response(request, response))
        assert result is response
        assert clock.requested == []
        assert mw._retry_after_deadlines == {}
        assert (
            "scrapy.downloadermiddlewares.retry",
            logging.WARNING,
            f"Gave up retrying {request} (failed 1 times): 503 Service Unavailable",
        ) in caplog.record_tuples

    @coroutine_test
    async def test_budget_lost_during_wait(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        response = _response(request.url, retry_after="1")
        task = _start(mw.process_response, request, response)
        await _settle()
        assert clock.requested == [1.0]

        # The retry budget is checked again when the wait ends.
        mw.max_retry_times = 0
        clock.advance(1.0)
        await _settle()
        assert task.done()
        assert task.result() is response
        assert mw.crawler.stats.get_value("retry_after/delayed") is None
        assert mw.crawler.stats.get_value("retry/max_reached") == 1

    @coroutine_test
    async def test_statistic_only_for_returned_retries(self, clock: _FakeClock) -> None:
        mw = _retry_after_ready()
        request = Request("http://example.com/")
        response = _response(request.url, retry_after="1")

        task = _start(mw.process_response, request, response)
        await _settle()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert mw.crawler.stats.get_value("retry_after/delayed") is None

        await _drive(clock, mw.process_response(request, response))
        assert mw.crawler.stats.get_value("retry_after/delayed") == 1
        assert mw.crawler.stats.get_value("retry/count") == 1


class _RetryAfterSpider(Spider):
    """Requests three pages that each answer 503 once with ``Retry-After``."""

    name = "retry_after"
    mockserver: MockServer

    async def start(self) -> AsyncIterator[Request]:
        for path in ("/retry-after?a", "/retry-after?b", "/retry-after?c"):
            yield Request(self.mockserver.url(path))


@coroutine_test
async def test_retry_after_crawl(mockserver: MockServer) -> None:
    crawler = get_crawler(
        _RetryAfterSpider,
        {
            "RETRY_AFTER_ENABLED": True,
            # The server asks for 1 second; cap it to keep the test short.
            "RETRY_AFTER_MAX_DELAY": 0.2,
        },
    )
    started = time.monotonic()
    await crawler.crawl_async(mockserver=mockserver)
    elapsed = time.monotonic() - started

    assert crawler.stats is not None
    assert crawler.stats.get_value("retry/count") == 3
    assert crawler.stats.get_value("retry_after/delayed") == 3
    assert crawler.stats.get_value("finish_reason") == "finished"
    # The three requests share one 0.2-second window and wait concurrently,
    # instead of stalling the whole crawl for 0.6 seconds.
    assert elapsed < 0.55


@pytest.mark.only_asyncio
@coroutine_test
async def test_http_date_against_real_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP dates are compared with the real UTC wall clock."""
    module = scrapy.downloadermiddlewares.retry
    fake = _FakeClock()
    monkeypatch.setattr(module, "_monotonic", lambda: fake.monotonic)
    monkeypatch.setattr(module, "_sleep", fake.sleep)
    mw = _retry_after_ready()
    request = Request("http://example.com/")
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=5), usegmt=True)
    task = _start(
        mw.process_response, request, _response(request.url, retry_after=date)
    )
    await _settle()
    assert 4 <= fake.requested[0] <= 6
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
