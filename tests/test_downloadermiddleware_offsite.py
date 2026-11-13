import re
from typing import Any

import pytest

from scrapy import Request, Spider
from scrapy.crawler import Crawler
from scrapy.downloadermiddlewares.offsite import OffsiteMiddleware
from scrapy.exceptions import IgnoreRequest
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler

UNSET = object()


@pytest.mark.parametrize(
    ("allowed_domain", "url", "allowed"),
    [
        ("example.com", "http://example.com/1", True),
        ("example.com", "http://example.org/1", False),
        ("example.com", "http://sub.example.com/1", True),
        ("sub.example.com", "http://sub.example.com/1", True),
        ("sub.example.com", "http://example.com/1", False),
        ("example.com", "http://example.com:8000/1", True),
        ("example.com", "http://example.org/example.com", False),
        ("example.com", "http://example.org/foo.example.com", False),
        ("example.com", "http://example.com.example", False),
        ("a.example", "http://nota.example", False),
        ("b.a.example", "http://notb.a.example", False),
    ],
)
def test_process_request_domain_filtering(allowed_domain, url, allowed):
    crawler = get_crawler(Spider)
    crawler.spider = crawler._create_spider(name="a", allowed_domains=[allowed_domain])
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    request = Request(url)
    if allowed:
        assert mw.process_request(request) is None
    else:
        with pytest.raises(IgnoreRequest):
            mw.process_request(request)


@pytest.mark.parametrize(
    ("value", "filtered"),
    [
        (UNSET, True),
        (None, True),
        (False, True),
        (True, False),
    ],
)
def test_process_request_dont_filter(value, filtered):
    crawler = get_crawler(Spider)
    crawler.spider = crawler._create_spider(name="a", allowed_domains=["a.example"])
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    kwargs: dict[str, Any] = {}
    if value is not UNSET:
        kwargs["dont_filter"] = value
    request = Request("https://b.example", **kwargs)
    if filtered:
        with pytest.raises(IgnoreRequest):
            mw.process_request(request)
    else:
        assert mw.process_request(request) is None


@pytest.mark.parametrize(
    ("allow_offsite", "dont_filter", "filtered"),
    [
        (True, UNSET, False),
        (True, None, False),
        (True, False, False),
        (True, True, False),
        (False, UNSET, True),
        (False, None, True),
        (False, False, True),
        (False, True, False),
    ],
)
def test_process_request_allow_offsite(allow_offsite, dont_filter, filtered):
    crawler = get_crawler(Spider)
    crawler.spider = crawler._create_spider(name="a", allowed_domains=["a.example"])
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    kwargs: dict[str, Any] = {"meta": {}}
    if allow_offsite is not UNSET:
        kwargs["meta"]["allow_offsite"] = allow_offsite
    if dont_filter is not UNSET:
        kwargs["dont_filter"] = dont_filter
    request = Request("https://b.example", **kwargs)
    if filtered:
        with pytest.raises(IgnoreRequest):
            mw.process_request(request)
    else:
        assert mw.process_request(request) is None


@pytest.mark.parametrize(
    "value",
    [
        UNSET,
        None,
        [],
    ],
)
def test_process_request_no_allowed_domains(value):
    crawler = get_crawler(Spider)
    kwargs: dict[str, Any] = {}
    if value is not UNSET:
        kwargs["allowed_domains"] = value
    crawler.spider = crawler._create_spider(name="a", **kwargs)
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    request = Request("https://example.com")
    assert mw.process_request(request) is None


def test_process_request_invalid_domains():
    crawler = get_crawler(Spider)
    allowed_domains = ["a.example", None, "http:////b.example", "//c.example"]
    crawler.spider = crawler._create_spider(name="a", allowed_domains=allowed_domains)
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    request = Request("https://a.example")
    assert mw.process_request(request) is None
    for letter in ("b", "c"):
        request = Request(f"https://{letter}.example")
        with pytest.raises(IgnoreRequest):
            mw.process_request(request)


@pytest.mark.parametrize(
    ("allowed_domain", "url", "allowed"),
    [
        ("example.com", "http://example.com/1", True),
        ("example.com", "http://example.org/1", False),
        ("example.com", "http://sub.example.com/1", True),
        ("sub.example.com", "http://sub.example.com/1", True),
        ("sub.example.com", "http://example.com/1", False),
        ("example.com", "http://example.com:8000/1", True),
        ("example.com", "http://example.org/example.com", False),
        ("example.com", "http://example.org/foo.example.com", False),
        ("example.com", "http://example.com.example", False),
        ("a.example", "http://nota.example", False),
        ("b.a.example", "http://notb.a.example", False),
    ],
)
def test_request_scheduled_domain_filtering(allowed_domain, url, allowed):
    crawler = get_crawler(Spider)
    crawler.spider = crawler._create_spider(name="a", allowed_domains=[allowed_domain])
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    request = Request(url)
    if allowed:
        mw.request_scheduled(request, crawler.spider)
    else:
        with pytest.raises(IgnoreRequest):
            mw.request_scheduled(request, crawler.spider)


@pytest.mark.parametrize(
    ("value", "filtered"),
    [
        (UNSET, True),
        (None, True),
        (False, True),
        (True, False),
    ],
)
def test_request_scheduled_dont_filter(value, filtered):
    crawler = get_crawler(Spider)
    crawler.spider = crawler._create_spider(name="a", allowed_domains=["a.example"])
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    kwargs: dict[str, Any] = {}
    if value is not UNSET:
        kwargs["dont_filter"] = value
    request = Request("https://b.example", **kwargs)
    if filtered:
        with pytest.raises(IgnoreRequest):
            mw.request_scheduled(request, crawler.spider)
    else:
        mw.request_scheduled(request, crawler.spider)


@pytest.mark.parametrize(
    "value",
    [
        UNSET,
        None,
        [],
    ],
)
def test_request_scheduled_no_allowed_domains(value):
    crawler = get_crawler(Spider)
    kwargs: dict[str, Any] = {}
    if value is not UNSET:
        kwargs["allowed_domains"] = value
    crawler.spider = crawler._create_spider(name="a", **kwargs)
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    request = Request("https://example.com")
    mw.request_scheduled(request, crawler.spider)


def test_request_scheduled_invalid_domains():
    crawler = get_crawler(Spider)
    allowed_domains = ["a.example", None, "http:////b.example", "//c.example"]
    crawler.spider = crawler._create_spider(name="a", allowed_domains=allowed_domains)
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    request = Request("https://a.example")
    mw.request_scheduled(request, crawler.spider)
    for letter in ("b", "c"):
        request = Request(f"https://{letter}.example")
        with pytest.raises(IgnoreRequest):
            mw.request_scheduled(request, crawler.spider)


def test_repeated_offsite_domain():
    crawler = get_crawler(Spider)
    crawler.spider = crawler._create_spider(name="a", allowed_domains=["example.com"])
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    req1 = Request("http://other.org/1")
    req2 = Request("http://other.org/2")
    with pytest.raises(IgnoreRequest):
        mw.process_request(req1)
    assert "other.org" in mw.domains_seen
    assert crawler.stats.get_value("offsite/domains") == 1
    assert crawler.stats.get_value("offsite/filtered") == 1
    with pytest.raises(IgnoreRequest):
        mw.process_request(req2)
    assert crawler.stats.get_value("offsite/domains") == 1  # not incremented again
    assert crawler.stats.get_value("offsite/filtered") == 2


def test_should_follow_override():
    class RootOnlyOffsiteMiddleware(OffsiteMiddleware):
        def should_follow(self, request: Request, spider: Spider) -> bool:
            allowed_domains: list[str] = getattr(spider, "allowed_domains", [])
            return urlparse_cached(request).hostname in allowed_domains

    crawler = get_crawler(Spider)
    crawler.spider = crawler._create_spider(name="a", allowed_domains=["example.com"])
    mw = build_from_crawler(RootOnlyOffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    assert mw.process_request(Request("https://example.com/1")) is None
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://www.example.com/1"))


def test_ignore_request_reason():
    crawler = get_crawler(Spider)
    crawler.spider = crawler._create_spider(name="a", allowed_domains=["example.com"])
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    request = Request("http://other.org/1")
    with pytest.raises(
        IgnoreRequest, match=re.escape("Filtered offsite request to 'other.org'")
    ):
        mw.process_request(request)


class DomainSpider(Spider):
    name = "a"
    allowed_domains: list[str]


def test_dynamic_allowed_domains():
    crawler = get_crawler(DomainSpider)
    spider = DomainSpider.from_crawler(crawler, allowed_domains=["a.example"])
    crawler.spider = spider
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(spider)

    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://b.example"))

    spider.allowed_domains.append("b.example")
    assert mw.process_request(Request("https://b.example")) is None

    spider.allowed_domains.remove("a.example")
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://a.example"))


def test_dynamic_allowed_domains_caching():
    calls = 0

    class TrackingMiddleware(OffsiteMiddleware):
        def get_host_regex(self, spider: Spider) -> re.Pattern[str]:
            nonlocal calls
            calls += 1
            return super().get_host_regex(spider)

    crawler = get_crawler(DomainSpider)
    spider = DomainSpider.from_crawler(crawler, allowed_domains=["a.example"])
    crawler.spider = spider
    mw = build_from_crawler(TrackingMiddleware, crawler)
    mw.spider_opened(spider)

    for _ in range(3):
        mw.process_request(Request("https://a.example"))
    assert calls == 1

    spider.allowed_domains.append("b.example")
    assert mw.process_request(Request("https://b.example")) is None
    assert calls == 2


def _build_strict_mw(
    allowed_hosts: list[Any], allowed_domains: Any = UNSET
) -> tuple[OffsiteMiddleware, Crawler]:
    crawler = get_crawler(Spider)
    kwargs: dict[str, Any] = {"allowed_hosts": allowed_hosts}
    if allowed_domains is not UNSET:
        kwargs["allowed_domains"] = allowed_domains
    crawler.spider = crawler._create_spider(name="a", **kwargs)
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(crawler.spider)
    return mw, crawler


@pytest.mark.parametrize(
    ("allowed_hosts", "url", "allowed"),
    [
        # exact host matching, no implicit subdomains
        (["example.com"], "http://example.com/1", True),
        (["example.com"], "http://www.example.com/1", False),
        (["example.com"], "http://example.org/1", False),
        (["example.com"], "http://notexample.com/1", False),
        (["example.com"], "http://example.com.b.example/1", False),
        # entries without a port allow any port and scheme
        (["example.com"], "http://example.com:8080/1", True),
        (["example.com"], "https://example.com/1", True),
        (["example.com"], "nonstandard://example.com/1", True),
        # explicit subdomain rules
        (["*.example.com"], "http://example.com/1", True),
        (["*.example.com"], "http://www.example.com/1", True),
        (["*.example.com"], "http://a.b.example.com/1", True),
        (["*.example.com"], "http://notexample.com/1", False),
        (["*.example.com"], "http://example.com.b.example/1", False),
        (["*.example.com"], "http://example.org/1", False),
        # case and trailing dot normalization
        (["ExAmPle.CoM"], "http://example.com/1", True),
        (["example.com"], "http://EXAMPLE.COM/1", True),
        (["example.com."], "http://example.com/1", True),
        (["example.com"], "http://example.com./1", True),
        # ports, including default ports of the URL scheme
        (["example.com:8080"], "http://example.com:8080/1", True),
        (["example.com:8080"], "http://example.com:8081/1", False),
        (["example.com:8080"], "http://example.com/1", False),
        (["example.com:80"], "http://example.com/1", True),
        (["example.com:80"], "http://example.com:80/1", True),
        (["example.com:80"], "https://example.com/1", False),
        (["example.com:443"], "https://example.com/1", True),
        (["example.com:443"], "http://example.com/1", False),
        (["example.com:21"], "ftp://example.com/1", True),
        (["example.com:80"], "nonstandard://example.com/1", False),
        (["*.example.com:8080"], "http://sub.example.com:8080/1", True),
        (["*.example.com:8080"], "http://sub.example.com/1", False),
        # internationalized domain names
        (["bücher.example"], "http://xn--bcher-kva.example/1", True),
        (["xn--bcher-kva.example"], "http://bücher.example/1", True),
        (["bücher.example"], "http://bücher.example/1", True),
        (["*.bücher.example"], "http://www.xn--bcher-kva.example/1", True),
        # IP literals
        (["127.0.0.1"], "http://127.0.0.1/1", True),
        (["127.0.0.1:8080"], "http://127.0.0.1:8080/1", True),
        (["127.0.0.1:8080"], "http://127.0.0.1/1", False),
        (["[::1]"], "http://[::1]/1", True),
        (["[::1]"], "http://[0:0:0:0:0:0:0:1]/1", True),
        (["[0:0:0:0:0:0:0:1]"], "http://[::1]/1", True),
        (["[::1]:8080"], "http://[::1]:8080/1", True),
        (["[::1]:8080"], "http://[::1]/1", False),
        (["[::1]"], "http://[::2]/1", False),
        # multiple entries
        (["a.example", "b.example:8080"], "http://b.example:8080/1", True),
        (["a.example", "b.example:8080"], "http://b.example/1", False),
        (["a.example", "b.example:8080"], "http://a.example/1", True),
    ],
)
def test_strict_allowed_hosts(allowed_hosts, url, allowed):
    mw, _ = _build_strict_mw(allowed_hosts)
    request = Request(url)
    if allowed:
        assert mw.process_request(request) is None
    else:
        with pytest.raises(IgnoreRequest):
            mw.process_request(request)


@pytest.mark.parametrize(
    "entry",
    [
        "http://example.com",
        "//example.com",
        "example.com/path",
        "user@example.com",
        "user:pass@example.com",
        "example.com?query=1",
        "example.com#fragment",
        "example.com:abc",
        "example.com:99999",
        "example.com:",
        "example.com:²",
        "::1",
        "[::1",
        "[::1]x",
        "",
        "   ",
        "*",
        "*.",
        "exa mple.com",
        "a" * 64,
        "example..com",
        None,
        123,
    ],
)
def test_strict_allowed_hosts_invalid(entry):
    crawler = get_crawler(Spider)
    crawler.spider = crawler._create_spider(name="a", allowed_hosts=[entry])
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    with pytest.raises(ValueError, match="allowed_hosts"):
        mw.spider_opened(crawler.spider)


def test_strict_allowed_hosts_precedence():
    mw, _ = _build_strict_mw(["b.example"], allowed_domains=["a.example"])
    assert mw.process_request(Request("https://b.example")) is None
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://a.example"))


def test_empty_allowed_hosts_falls_back_to_allowed_domains():
    mw, _ = _build_strict_mw([], allowed_domains=["a.example"])
    assert mw.process_request(Request("https://sub.a.example")) is None
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://b.example"))


def test_strict_allow_offsite_and_dont_filter():
    mw, _ = _build_strict_mw(["a.example"])
    request = Request("https://b.example", dont_filter=True)
    assert mw.process_request(request) is None
    request = Request("https://b.example", meta={"allow_offsite": True})
    assert mw.process_request(request) is None


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/some/file",  # no host name
        f"http://{'a' * 64}.example/",  # host name too long for IDNA
    ],
)
def test_strict_allowed_hosts_invalid_url_host(url):
    mw, _ = _build_strict_mw(["example.com"])
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request(url))


def test_strict_repeated_offsite_domain():
    mw, crawler = _build_strict_mw(["example.com"])
    req1 = Request("http://other.org/1")
    req2 = Request("http://other.org/2")
    with pytest.raises(IgnoreRequest):
        mw.process_request(req1)
    assert "other.org" in mw.domains_seen
    assert crawler.stats.get_value("offsite/domains") == 1
    assert crawler.stats.get_value("offsite/filtered") == 1
    with pytest.raises(IgnoreRequest):
        mw.process_request(req2)
    assert crawler.stats.get_value("offsite/domains") == 1  # not incremented again
    assert crawler.stats.get_value("offsite/filtered") == 2


class HostsSpider(Spider):
    name = "a"
    allowed_hosts: list[str]


def test_dynamic_allowed_hosts():
    crawler = get_crawler(HostsSpider)
    spider = HostsSpider.from_crawler(crawler, allowed_hosts=["a.example"])
    crawler.spider = spider
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(spider)

    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://b.example"))

    spider.allowed_hosts.append("b.example")
    assert mw.process_request(Request("https://b.example")) is None

    spider.allowed_hosts.remove("a.example")
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://a.example"))


def test_dynamic_allowed_hosts_fallback():
    crawler = get_crawler(HostsSpider)
    spider = HostsSpider.from_crawler(
        crawler, allowed_hosts=["a.example"], allowed_domains=["b.example"]
    )
    crawler.spider = spider
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(spider)

    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://b.example"))

    spider.allowed_hosts.clear()
    assert mw.process_request(Request("https://b.example")) is None
    assert mw.process_request(Request("https://sub.b.example")) is None
