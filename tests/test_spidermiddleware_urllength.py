from __future__ import annotations

from logging import INFO
from typing import TYPE_CHECKING, Any

import pytest

from scrapy.exceptions import NotConfigured
from scrapy.http import Request, Response
from scrapy.spidermiddlewares.urllength import UrlLengthMeasure, UrlLengthMiddleware
from scrapy.spiders import Spider
from scrapy.utils.asyncgen import as_async_generator, collect_asyncgen
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from scrapy.crawler import Crawler
    from scrapy.statscollectors import StatsCollector


maxlength = 25
response = Response("http://scrapytest.org")
short_url_req = Request("http://scrapytest.org/")
long_url_req = Request("http://scrapytest.org/this_is_a_long_url")
reqs: list[Request] = [short_url_req, long_url_req]


def make_mw(
    settings: dict[str, Any] | None = None, *, measure: str | None = None
) -> UrlLengthMiddleware:
    merged: dict[str, Any] = {"URLLENGTH_LIMIT": maxlength}
    if measure is not None:
        merged["URLLENGTH_LIMIT_MEASURE"] = measure
    if settings:
        merged.update(settings)
    crawler = get_crawler(Spider, merged)
    return build_from_crawler(UrlLengthMiddleware, crawler)


@pytest.fixture
def crawler() -> Crawler:
    return get_crawler(Spider, {"URLLENGTH_LIMIT": maxlength})


@pytest.fixture
def stats(crawler: Crawler) -> StatsCollector:
    return crawler.stats


@pytest.fixture
def mw(crawler: Crawler) -> UrlLengthMiddleware:
    return build_from_crawler(UrlLengthMiddleware, crawler)


def process_spider_output(mw: UrlLengthMiddleware) -> list[Request]:
    return list(mw.process_spider_output(response, reqs))


def test_middleware_works(mw: UrlLengthMiddleware) -> None:
    assert process_spider_output(mw) == [short_url_req]
    assert mw.measure is UrlLengthMeasure.CHARS


def test_not_configured_without_limit() -> None:
    crawler = get_crawler(Spider, {"URLLENGTH_LIMIT": 0})
    with pytest.raises(NotConfigured):
        build_from_crawler(UrlLengthMiddleware, crawler)


def test_logging(
    stats: StatsCollector, mw: UrlLengthMiddleware, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(INFO):
        process_spider_output(mw)
    ric = stats.get_value("urllength/request_ignored_count")
    assert ric == 1
    assert f"Ignoring link (url chars length {len(long_url_req.url)} >" in caplog.text


# --- measurement on local Request objects --------------------------------


@pytest.mark.parametrize(
    ("url", "kwargs", "expected"),
    [
        # ASCII: characters and bytes coincide
        ("http://example.com/path", {}, 23),
        # multibyte characters, kept verbatim: 1 char != 1 byte
        (
            "http://example.com/路径?q=中文",
            {"meta": {"verbatim_url": True}},
            26,
        ),
        # IDNA host gets punycode-encoded at Request build time
        ("http://例え.jp/path", {}, 26),
        # IDNA host kept as unicode with verbatim_url
        (
            "http://例え.jp/path",
            {"meta": {"verbatim_url": True}},
            17,
        ),
        # IPv6 authority with brackets and port
        ("http://[2001:db8::1]:8080/a", {}, 27),
        # repeated percent escapes stay untouched
        ("http://example.com/%25%25x", {}, 26),
        (
            "http://example.com/%25%25/中文",
            {"meta": {"verbatim_url": True}},
            28,
        ),
        # fragment counts towards the character length of request.url
        ("http://example.com/p#frag", {}, 25),
        (
            "http://example.com/p#中文",
            {"meta": {"verbatim_url": True}},
            23,
        ),
    ],
)
def test_char_measure(url: str, kwargs: dict[str, Any], expected: int) -> None:
    mw = make_mw()
    request = Request(url, **kwargs)
    assert mw._get_length(request) == expected == len(request.url)


@pytest.mark.parametrize(
    ("url", "kwargs", "expected"),
    [
        ("http://example.com/path", {}, 23),
        # 8 multibyte chars in path/query add 2 extra bytes each
        (
            "http://example.com/路径?q=中文",
            {"meta": {"verbatim_url": True}},
            34,
        ),
        # already punycode: ASCII, same as character count
        ("http://例え.jp/path", {}, 26),
        # unicode authority kept verbatim: 例え is 6 bytes vs 2 chars
        (
            "http://例え.jp/path",
            {"meta": {"verbatim_url": True}},
            21,
        ),
        ("http://[2001:db8::1]:8080/a", {}, 27),
        ("http://example.com/%25%25x", {}, 26),
        (
            "http://example.com/%25%25/中文",
            {"meta": {"verbatim_url": True}},
            32,
        ),
        # the fragment is never part of the request target
        ("http://example.com/p#frag", {}, 20),
        (
            "http://example.com/p#中文",
            {"meta": {"verbatim_url": True}},
            20,
        ),
    ],
)
def test_byte_measure(url: str, kwargs: dict[str, Any], expected: int) -> None:
    mw = make_mw(measure="bytes")
    request = Request(url, **kwargs)
    assert mw._get_length(request) == expected


def test_measure_does_not_mutate_request() -> None:
    mw = make_mw(measure="bytes")
    request = Request(
        "http://example.com/%25%25/中文",
        meta={"verbatim_url": True},
    )
    original_url = request.url
    mw._get_length(request)
    # already-escaped parts and the whole URL must be left exactly as built
    assert request.url == original_url
    assert "%25%25" in request.url


def test_two_modes_same_object() -> None:
    # 26 chars, 34 bytes: chars mode accepts at limit 26, byte mode rejects
    request = Request(
        "http://example.com/路径?q=中文",
        meta={"verbatim_url": True},
    )
    assert (
        make_mw({"URLLENGTH_LIMIT": 26}, measure="chars").get_processed_request(
            request, None
        )
        is request
    )
    assert (
        make_mw({"URLLENGTH_LIMIT": 26}, measure="bytes").get_processed_request(
            request, None
        )
        is None
    )
    # measuring never rewrites the shared request URL
    assert request.url == "http://example.com/路径?q=中文"


@pytest.mark.parametrize("measure", ["chars", "bytes"])
def test_exact_boundary(measure: str) -> None:
    mw = make_mw({"URLLENGTH_LIMIT": 23}, measure=measure)
    request = Request("http://example.com/path")  # 23 chars and 23 bytes
    assert mw.get_processed_request(request, None) is request

    over = Request("http://example.com/pathx")  # 24
    assert mw.get_processed_request(over, None) is None


def test_byte_boundary_with_multibyte() -> None:
    request = Request(
        "http://example.com/路径?q=中文",
        meta={"verbatim_url": True},
    )  # 26 chars, 34 bytes
    assert (
        make_mw({"URLLENGTH_LIMIT": 26}, measure="chars").get_processed_request(
            request, None
        )
        is request
    )
    assert (
        make_mw({"URLLENGTH_LIMIT": 26}, measure="bytes").get_processed_request(
            request, None
        )
        is None
    )
    assert (
        make_mw({"URLLENGTH_LIMIT": 34}, measure="bytes").get_processed_request(
            request, None
        )
        is request
    )


def test_fragment_not_counted_as_bytes() -> None:
    mw = make_mw({"URLLENGTH_LIMIT": 20}, measure="bytes")
    request = Request("http://example.com/p#frag")
    assert mw.get_processed_request(request, None) is request
    # chars mode sees 25 characters and rejects the same request
    mw_chars = make_mw({"URLLENGTH_LIMIT": 20}, measure="chars")
    assert mw_chars.get_processed_request(request, None) is None


# --- configuration validation ---------------------------------------------


@pytest.mark.parametrize("measure", ["chars", "bytes"])
def test_valid_measure_settings(measure: str) -> None:
    mw = make_mw(measure=measure)
    assert mw.measure == UrlLengthMeasure(measure)


def test_negative_limit_rejected() -> None:
    crawler = get_crawler(Spider, {"URLLENGTH_LIMIT": -1})
    with pytest.raises(ValueError, match="non-negative integer"):
        build_from_crawler(UrlLengthMiddleware, crawler)


@pytest.mark.parametrize("bad_limit", [1.5, True, "25", None])
def test_non_integer_limit_rejected(bad_limit: object) -> None:
    crawler = get_crawler(Spider, {"URLLENGTH_LIMIT": bad_limit})
    with pytest.raises((ValueError, NotConfigured)):
        build_from_crawler(UrlLengthMiddleware, crawler)


@pytest.mark.parametrize("bad_measure", ["words", 1, True, b"chars"])
def test_invalid_measure_value_rejected(bad_measure: object) -> None:
    crawler = get_crawler(
        Spider,
        {"URLLENGTH_LIMIT": maxlength, "URLLENGTH_LIMIT_MEASURE": bad_measure},
    )
    with pytest.raises(ValueError, match="URLLENGTH_LIMIT_MEASURE"):
        build_from_crawler(UrlLengthMiddleware, crawler)


def test_none_measure_falls_back_to_default() -> None:
    # Settings.get() treats None as unset and returns the setting default
    crawler = get_crawler(
        Spider,
        {"URLLENGTH_LIMIT": maxlength, "URLLENGTH_LIMIT_MEASURE": None},
    )
    mw = build_from_crawler(UrlLengthMiddleware, crawler)
    assert mw.measure is UrlLengthMeasure.CHARS


def test_zero_limit_disables_middleware() -> None:
    crawler = get_crawler(Spider, {"URLLENGTH_LIMIT": 0})
    with pytest.raises(NotConfigured):
        build_from_crawler(UrlLengthMiddleware, crawler)


# --- stats and logging -----------------------------------------------------


def test_stats_are_locatable_and_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mw = make_mw(measure="bytes")
    request = Request("http://example.com/p?token=secret123#frag")

    with caplog.at_level(INFO):
        assert mw.get_processed_request(request, None) is None

    stats = mw.crawler.stats
    assert stats.get_value("urllength/request_ignored_count") == 1
    assert stats.get_value("urllength/bytes/request_ignored_count") == 1
    assert stats.get_value("urllength/bytes/max_ignored_length") == len(
        b"http://example.com/p?token=secret123"
    )
    # the sensitive query and the fragment never reach the log
    assert "secret123" not in caplog.text
    assert "token" not in caplog.text
    assert "frag" not in caplog.text
    assert "http://example.com/p" in caplog.text


def test_char_mode_stats_keys() -> None:
    mw = make_mw(measure="chars")
    request = Request("http://example.com/" + "x" * 30)
    assert mw.get_processed_request(request, None) is None
    stats = mw.crawler.stats
    assert stats.get_value("urllength/request_ignored_count") == 1
    assert stats.get_value("urllength/chars/request_ignored_count") == 1
    assert stats.get_value("urllength/chars/max_ignored_length") == len(request.url)


# --- sync and async iteration ----------------------------------------------


def _mixed_output() -> list[object]:
    good = Request("http://scrapytest.org/")
    bad = Request("http://scrapytest.org/" + "x" * 40)
    also_good = Request("http://scrapytest.org/ok")
    item = {"name": "item"}
    return [good, bad, also_good, item]


def test_sync_iteration_keeps_later_items() -> None:
    mw = make_mw()
    out = list(mw.process_spider_output(response, _mixed_output()))
    urls = [o.url for o in out if isinstance(o, Request)]
    assert urls == [
        "http://scrapytest.org/",
        "http://scrapytest.org/ok",
    ]
    assert {"name": "item"} in out
    assert mw.crawler.stats.get_value("urllength/request_ignored_count") == 1


@coroutine_test
async def test_async_iteration_keeps_later_items() -> None:
    mw = make_mw(measure="bytes")
    out = await collect_asyncgen(
        mw.process_spider_output_async(response, as_async_generator(_mixed_output()))
    )
    urls = [o.url for o in out if isinstance(o, Request)]
    assert urls == [
        "http://scrapytest.org/",
        "http://scrapytest.org/ok",
    ]
    assert {"name": "item"} in out
    assert mw.crawler.stats.get_value("urllength/request_ignored_count") == 1
