from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest

from scrapy import Request, Spider, signals
from scrapy.core.downloader.handlers.base import BaseDownloadHandler
from scrapy.downloadermiddlewares.offsite import (
    _SNAPSHOT_HISTORY_SIZE,
    OffsiteMiddleware,
)
from scrapy.exceptions import CloseSpider, IgnoreRequest
from scrapy.http import HtmlResponse
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from scrapy.crawler import Crawler

UNSET = object()
# The key the middleware uses to record which snapshot a request was
# evaluated with. Kept as a literal so that renaming it is a test failure.
_VERSION_KEY = "offsite_rules_version"


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


def _snapshot_middleware(
    allowed_domains: list[str] | None = None,
) -> tuple[OffsiteMiddleware, Crawler, Spider]:
    crawler = get_crawler(Spider, {"OFFSITE_RULES_SNAPSHOT": True})
    spider = crawler._create_spider(name="a", allowed_domains=allowed_domains)
    crawler.spider = spider
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(spider)
    return mw, crawler, spider


def test_snapshot_setting_disabled_by_default():
    crawler = get_crawler(Spider)
    crawler.spider = crawler._create_spider(name="a", allowed_domains=["a.example"])
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    assert mw.get_rules_version() is None
    with pytest.raises(RuntimeError):
        mw.reload_rules(crawler.spider, ["b.example"])


@pytest.mark.parametrize(
    ("rules", "url", "allowed"),
    [
        # Exact rules do not cover subdomains, unlike allowed_domains.
        (["example.com"], "http://example.com/1", True),
        (["example.com"], "http://www.example.com/1", False),
        (["example.com"], "http://example.com.evil/1", False),
        (["example.com"], "http://notexample.com/1", False),
        # Explicit subdomain rules match subdomains only, not the root.
        (["*.example.com"], "http://www.example.com/1", True),
        (["*.example.com"], "http://a.b.example.com/1", True),
        (["*.example.com"], "http://example.com/1", False),
        (["*.example.com"], "http://example.com.evil/1", False),
        (["*.example.com"], "http://notexample.com/1", False),
        # Case, trailing dot and IDNA are normalized on both sides.
        (["ExAmPle.CoM"], "http://example.com/1", True),
        (["example.com."], "http://example.com/1", True),
        (["example.com"], "http://example.com./1", True),
        (["bücher.example"], "http://xn--bcher-kva.example/1", True),
        (["xn--bcher-kva.example"], "http://bücher.example/1", True),
        (["*.bücher.example"], "http://www.xn--bcher-kva.example/1", True),
        # IPv6 literals are compared in their compressed form.
        (["[2001:db8::1]"], "http://[2001:db8::1]/1", True),
        (["[2001:db8::1]"], "http://[2001:0db8::1]/1", True),
        (["[2001:db8::1]"], "http://[2001:db8::2]/1", False),
        # Ports use the scheme default when the URL omits one.
        (["example.com:8080"], "http://example.com:8080/1", True),
        (["example.com:8080"], "http://example.com/1", False),
        (["example.com:80"], "http://example.com/1", True),
        (["example.com:443"], "https://example.com/1", True),
        (["example.com"], "https://example.com:8443/1", True),
        (["[2001:db8::1]:8080"], "http://[2001:db8::1]:8080/1", True),
        # Several rules: any match allows the request.
        (["a.example", "*.b.example"], "http://x.b.example/1", True),
        (["a.example", "*.b.example"], "http://c.example/1", False),
        # No rules allows everything, matching the default policy.
        ([], "http://anything.example/1", True),
    ],
)
def test_snapshot_rule_matching(rules, url, allowed):
    mw, _, _ = _snapshot_middleware(rules)
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
        "example.com?query=1",
        "example.com#fragment",
        "user@example.com",
        "user:pass@example.com",
        "example..com",
        ".example.com",
        "example.com..",
        "example.com:",
        "example.com:abc",
        "example.com:99999",
        "example.com:0",
        "example.com:-1",
        "exa mple.com",
        " example.com",
        "example.com ",
        "",
        "*",
        "*.",
        "*.example..com",
        "**.example.com",
        "exa*mple.com",
        "::1",
        "[2001:db8::1",
        "[2001:db8::1]x",
        "[not-an-address]",
        "*. [2001:db8::1]",
        # An IP literal cannot have subdomains, with or without brackets.
        "*.[2001:db8::1]",
        "*.127.0.0.1",
        # Host names are limited to 253 characters.
        ".".join(["a" * 63] * 5),
    ],
)
def test_snapshot_invalid_rules(entry):
    mw, _, spider = _snapshot_middleware(["a.example"])
    with pytest.raises(ValueError, match="Invalid offsite rule"):
        mw.reload_rules(spider, [entry])


@pytest.mark.parametrize("entry", [None, 1, b"example.com"])
def test_snapshot_invalid_rule_types(entry):
    mw, _, spider = _snapshot_middleware(["a.example"])
    with pytest.raises(TypeError, match="Offsite rules must be strings"):
        mw.reload_rules(spider, ["b.example", entry])


def test_snapshot_rules_must_be_a_collection():
    mw, _, spider = _snapshot_middleware(["a.example"])
    with pytest.raises(TypeError, match="iterable of strings"):
        mw.reload_rules(spider, "example.com")


def test_snapshot_invalid_rules_abort_the_spider_opening():
    crawler = get_crawler(Spider, {"OFFSITE_RULES_SNAPSHOT": True})
    spider = crawler._create_spider(name="a", allowed_domains=["http://bad"])
    crawler.spider = spider
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    with pytest.raises(CloseSpider) as excinfo:
        mw.spider_opened(spider)
    assert excinfo.value.reason == "offsite_rules_invalid"
    assert excinfo.value.error is True


def test_snapshot_freezes_the_spider_attribute():
    spider_cls = DomainSpider
    crawler = get_crawler(spider_cls, {"OFFSITE_RULES_SNAPSHOT": True})
    spider = spider_cls.from_crawler(crawler, allowed_domains=["a.example"])
    crawler.spider = spider
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(spider)

    assert mw.process_request(Request("https://a.example")) is None
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://b.example"))

    # The external list changes, but the frozen snapshot does not.
    spider.allowed_domains.append("b.example")
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://b.example"))
    spider.allowed_domains.clear()
    assert mw.process_request(Request("https://a.example")) is None


def test_snapshot_external_list_change_does_not_affect_old_requests():
    crawler = get_crawler(DomainSpider, {"OFFSITE_RULES_SNAPSHOT": True})
    spider = DomainSpider.from_crawler(crawler, allowed_domains=["a.example"])
    crawler.spider = spider
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(spider)

    first = Request("https://a.example/1")
    assert mw.process_request(first) is None
    version = first.meta["offsite_rules_version"]
    assert version == mw.get_rules_version()

    spider.allowed_domains[:] = ["b.example"]
    # The old request keeps the version it was stamped with and is evaluated
    # again with the same snapshot.
    assert mw.process_request(first) is None
    # A new request is evaluated with the frozen snapshot too, not the list.
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://b.example/2"))


def test_snapshot_reload_publishes_new_rules():
    mw, _, spider = _snapshot_middleware(["a.example"])
    old_version = mw.get_rules_version(spider)
    assert old_version is not None

    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://b.example/1"))

    new_version = mw.reload_rules(spider, ["b.example"])
    assert new_version != old_version
    assert mw.get_rules_version(spider) == new_version

    assert mw.process_request(Request("https://b.example/2")) is None
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://a.example/2"))


def test_snapshot_version_is_rule_order_independent():
    mw, _, spider = _snapshot_middleware(["a.example", "b.example"])
    version = mw.get_rules_version(spider)
    assert mw.reload_rules(spider, ["b.example", "a.example"]) == version
    assert mw.reload_rules(spider, ["b.example", "a.example", "a.example"]) == version


def test_snapshot_reload_uses_spider_attribute_by_default():
    crawler = get_crawler(DomainSpider, {"OFFSITE_RULES_SNAPSHOT": True})
    spider = DomainSpider.from_crawler(crawler, allowed_domains=["a.example"])
    crawler.spider = spider
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(spider)

    spider.allowed_domains[:] = ["b.example"]
    mw.reload_rules(spider)
    assert mw.process_request(Request("https://b.example")) is None
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://a.example"))


def test_snapshot_reload_failure_keeps_the_old_rules():
    mw, _, spider = _snapshot_middleware(["a.example"])
    old_version = mw.get_rules_version(spider)

    with pytest.raises(ValueError, match="Invalid offsite rule"):
        mw.reload_rules(spider, ["b.example", "http://bad.example"])

    assert mw.get_rules_version(spider) == old_version
    assert mw.process_request(Request("https://a.example")) is None
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://b.example"))


def test_snapshot_reload_failure_does_not_publish_partial_rules():
    mw, _, spider = _snapshot_middleware(["a.example"])
    with pytest.raises(TypeError):
        mw.reload_rules(spider, ["b.example", None])  # type: ignore[list-item]
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("https://b.example"))
    assert mw.process_request(Request("https://a.example")) is None


def test_snapshot_interleaved_reload():
    mw, _, spider = _snapshot_middleware(["a.example"])
    # Stamp a request under the first snapshot, then reload.
    stale = Request("https://a.example/stale")
    assert mw.process_request(stale) is None
    stale_version = stale.meta["offsite_rules_version"]

    mw.reload_rules(spider, ["*.b.example"])
    fresh = Request("https://x.b.example/fresh")
    assert mw.process_request(fresh) is None
    fresh_version = fresh.meta["offsite_rules_version"]
    assert fresh_version != stale_version

    # The stale request keeps being evaluated with its own snapshot.
    assert mw.process_request(stale) is None
    assert stale.meta["offsite_rules_version"] == stale_version
    # A request stamped with the old version for a host of the new rules is
    # still evaluated with the old rules.
    with pytest.raises(IgnoreRequest):
        mw.process_request(
            Request("https://x.b.example/old", meta={_VERSION_KEY: stale_version})
        )


def test_snapshot_unknown_version_is_rejected():
    mw, _, _ = _snapshot_middleware(["a.example"])
    request = Request("https://a.example", meta={_VERSION_KEY: "unknown"})
    with pytest.raises(IgnoreRequest, match="unknown offsite rules version"):
        mw.process_request(request)


@pytest.mark.parametrize("version", [None, 1, ["x"], ""])
def test_snapshot_non_string_version_is_rejected(version):
    mw, _, _ = _snapshot_middleware(["a.example"])
    request = Request("https://a.example", meta={_VERSION_KEY: version})
    with pytest.raises(IgnoreRequest, match="unknown offsite rules version"):
        mw.process_request(request)


def test_snapshot_version_never_grants_access_by_itself():
    mw, _, spider = _snapshot_middleware(["a.example"])
    version = mw.get_rules_version(spider)
    assert version is not None
    request = Request("https://b.example", meta={_VERSION_KEY: version})
    with pytest.raises(IgnoreRequest):
        mw.process_request(request)


def test_snapshot_version_does_not_bypass_rules_through_should_follow():
    class AllowAll(OffsiteMiddleware):
        def should_follow(self, request: Request, spider: Spider) -> bool:
            return True

    crawler = get_crawler(Spider, {"OFFSITE_RULES_SNAPSHOT": True})
    spider = crawler._create_spider(name="a", allowed_domains=["a.example"])
    crawler.spider = spider
    mw = build_from_crawler(AllowAll, crawler)
    mw.spider_opened(spider)
    # A custom policy allows known-version requests...
    assert mw.process_request(Request("https://b.example")) is None
    # ...but an unknown version is still rejected before the policy runs.
    with pytest.raises(IgnoreRequest, match="unknown offsite rules version"):
        mw.process_request(Request("https://b.example", meta={_VERSION_KEY: "gone"}))


def test_snapshot_does_not_treat_arbitrary_meta_as_exception():
    mw, _, _ = _snapshot_middleware(["a.example"])
    request = Request("https://b.example", meta={"allow_offsite_": True})
    with pytest.raises(IgnoreRequest):
        mw.process_request(request)


@pytest.mark.parametrize(
    ("kwargs", "filtered"),
    [
        ({"dont_filter": True}, False),
        ({"meta": {"allow_offsite": True}}, False),
        ({"meta": {"allow_offsite": False}}, True),
        ({"meta": {"allow_offsite": "yes"}}, False),
    ],
)
def test_snapshot_bypass_priority(kwargs, filtered):
    mw, _, _ = _snapshot_middleware(["a.example"])
    request = Request("https://b.example", **kwargs)
    if filtered:
        with pytest.raises(IgnoreRequest):
            mw.process_request(request)
    else:
        assert mw.process_request(request) is None
        # Bypassed requests are not stamped.
        assert _VERSION_KEY not in request.meta


def test_snapshot_bypass_wins_over_unknown_version():
    mw, _, _ = _snapshot_middleware(["a.example"])
    meta = {_VERSION_KEY: "unknown", "allow_offsite": True}
    assert mw.process_request(Request("https://b.example", meta=meta)) is None
    meta = {_VERSION_KEY: "unknown"}
    assert (
        mw.process_request(Request("https://b.example", meta=meta, dont_filter=True))
        is None
    )


def test_snapshot_spiders_do_not_share_rules():
    crawler = get_crawler(Spider, {"OFFSITE_RULES_SNAPSHOT": True})
    first = crawler._create_spider(name="a", allowed_domains=["a.example"])
    second = crawler._create_spider(name="b", allowed_domains=["b.example"])
    crawler.spider = first
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(first)
    crawler.spider = second
    mw.spider_opened(second)

    first_version = mw.get_rules_version(first)
    second_version = mw.get_rules_version(second)
    assert first_version != second_version

    mw.reload_rules(first, ["*.a.example"])
    assert mw.get_rules_version(second) == second_version

    # Reloading the rules of one spider leaves the snapshot of the other one
    # untouched.
    with pytest.raises(IgnoreRequest):
        mw.process_request(
            Request("https://a.example", meta={_VERSION_KEY: second_version})
        )
    crawler.spider = first
    assert mw.process_request(Request("https://x.a.example")) is None
    with pytest.raises(IgnoreRequest):
        mw.process_request(
            Request("https://b.example", meta={_VERSION_KEY: first_version})
        )


def test_snapshot_version_unknown_to_another_spider():
    crawler = get_crawler(Spider, {"OFFSITE_RULES_SNAPSHOT": True})
    first = crawler._create_spider(name="a", allowed_domains=["a.example"])
    second = crawler._create_spider(name="b", allowed_domains=["b.example"])
    crawler.spider = first
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(first)
    crawler.spider = second
    mw.spider_opened(second)

    assert first.allowed_domains is not second.allowed_domains
    request = Request(
        "https://a.example", meta={_VERSION_KEY: mw.get_rules_version(first)}
    )
    with pytest.raises(IgnoreRequest, match="unknown offsite rules version"):
        mw.process_request(request)


def test_snapshot_counts_normalized_domains():
    mw, crawler, _ = _snapshot_middleware(["a.example"])
    for url in (
        "https://b.example/1",
        "https://B.Example/2",
        "https://b.example./3",
        "https://b.example:8080/4",
    ):
        with pytest.raises(IgnoreRequest):
            mw.process_request(Request(url))
    assert mw.domains_seen == {"b.example"}
    assert crawler.stats.get_value("offsite/domains") == 1
    assert crawler.stats.get_value("offsite/filtered") == 4


def test_snapshot_counts_idn_equivalent_domains_once():
    mw, crawler, _ = _snapshot_middleware(["a.example"])
    for url in (
        "https://bücher.example/1",
        "https://xn--bcher-kva.example/2",
    ):
        with pytest.raises(IgnoreRequest):
            mw.process_request(Request(url))
    assert crawler.stats.get_value("offsite/domains") == 1
    assert crawler.stats.get_value("offsite/filtered") == 2


def test_snapshot_request_scheduled_signal_is_consistent():
    mw, crawler, spider = _snapshot_middleware(["a.example"])
    allowed = Request("https://a.example/1")
    mw.request_scheduled(allowed, spider)
    assert allowed.meta[_VERSION_KEY] == mw.get_rules_version(spider)
    filtered = Request("https://b.example/1")
    with pytest.raises(IgnoreRequest):
        mw.request_scheduled(filtered, spider)
    with pytest.raises(IgnoreRequest):
        mw.process_request(filtered)
    assert crawler.stats.get_value("offsite/filtered") == 2
    assert crawler.stats.get_value("offsite/domains") == 1


def test_snapshot_history_is_bounded():
    mw, _, spider = _snapshot_middleware(["a.example"])
    versions = [mw.get_rules_version(spider)]
    versions.extend(
        mw.reload_rules(spider, [f"host{index}.example"])
        for index in range(_SNAPSHOT_HISTORY_SIZE + 1)
    )
    last_rule = f"host{_SNAPSHOT_HISTORY_SIZE}.example"

    # The oldest snapshot fell out of the retained history.
    oldest = Request("https://a.example", meta={_VERSION_KEY: versions[0]})
    with pytest.raises(IgnoreRequest, match="unknown offsite rules version"):
        mw.process_request(oldest)

    # The most recent one is still known.
    current = Request(f"https://{last_rule}", meta={_VERSION_KEY: versions[-1]})
    assert mw.process_request(current) is None


def test_snapshot_requires_spider_opened():
    crawler = get_crawler(Spider, {"OFFSITE_RULES_SNAPSHOT": True})
    crawler.spider = crawler._create_spider(name="a", allowed_domains=["a.example"])
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    with pytest.raises(RuntimeError, match="not initialized"):
        mw.process_request(Request("https://a.example"))
    assert mw.get_rules_version() is None


def test_snapshot_ipv4_rule_is_exact():
    mw, _, _ = _snapshot_middleware(["127.0.0.1", "10.0.0.0"])
    assert mw.process_request(Request("http://127.0.0.1/1")) is None
    assert mw.process_request(Request("http://10.0.0.0/1")) is None
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("http://127.0.0.2/1"))


def test_snapshot_request_without_host():
    mw, _, _ = _snapshot_middleware(["a.example"])
    # A URL with no host name can never match a rule.
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request("data:text/plain,hi"))


def test_snapshot_request_with_invalid_port():
    mw, _, _ = _snapshot_middleware(["a.example", "b.example:8080"])
    # Request() rejects an out-of-range port unless the URL is verbatim.
    request = Request("http://a.example:99999/1", meta={"verbatim_url": True})
    with pytest.raises(ValueError, match="Port out of range"):
        urlparse_cached(request).port
    # A rule without a port ignores the port, so it still matches.
    assert mw.process_request(request) is None
    # A rule with a port cannot match a request whose port is invalid.
    request = Request("http://b.example:99999/1", meta={"verbatim_url": True})
    with pytest.raises(IgnoreRequest):
        mw.process_request(request)


def test_snapshot_custom_should_follow_is_validated():
    class DenyAll(OffsiteMiddleware):
        def should_follow(self, request: Request, spider: Spider) -> bool:
            return False

    crawler = get_crawler(Spider, {"OFFSITE_RULES_SNAPSHOT": True})
    spider = crawler._create_spider(name="a", allowed_domains=["a.example"])
    crawler.spider = spider
    mw = build_from_crawler(DenyAll, crawler)
    mw.spider_opened(spider)

    # A known version reaches the custom policy, which denies the request.
    request = Request("https://a.example")
    with pytest.raises(IgnoreRequest, match="Filtered offsite request"):
        mw.process_request(request)
    # The request is stamped before the policy runs, so the same verdict is
    # reused if it is evaluated again.
    assert request.meta[_VERSION_KEY] == mw.get_rules_version(spider)
    # An unknown version is rejected before the policy runs.
    with pytest.raises(IgnoreRequest, match="unknown offsite rules version"):
        mw.process_request(Request("https://a.example", meta={_VERSION_KEY: "gone"}))


def test_get_rules_version_defaults_to_the_current_spider():
    crawler = get_crawler(Spider, {"OFFSITE_RULES_SNAPSHOT": True})
    spider = crawler._create_spider(name="a", allowed_domains=["a.example"])
    crawler.spider = spider
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(spider)
    assert mw.get_rules_version() == mw.get_rules_version(spider)


def test_snapshot_spider_attribute_must_be_a_collection():
    crawler = get_crawler(Spider, {"OFFSITE_RULES_SNAPSHOT": True})
    spider = crawler._create_spider(name="a", allowed_domains="a.example")
    crawler.spider = spider
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    with pytest.raises(CloseSpider):
        mw.spider_opened(spider)


def test_snapshot_request_with_a_host_that_cannot_be_normalized():
    mw, crawler, _ = _snapshot_middleware(["a.example"])
    long_label = "a" * 64
    request = Request(f"http://{long_label}.example/1")
    with pytest.raises(IgnoreRequest, match=re.escape(long_label)):
        mw.process_request(request)
    # The raw host name is counted and logged once, like any other domain.
    assert long_label + ".example" in mw.domains_seen
    with pytest.raises(IgnoreRequest):
        mw.process_request(Request(f"http://{long_label}.example/2"))
    assert crawler.stats.get_value("offsite/domains") == 1
    assert crawler.stats.get_value("offsite/filtered") == 2


class _EchoDownloadHandler(BaseDownloadHandler):
    """Return an empty HTML page for any URL, so that a crawl does not need a
    server."""

    async def download_request(self, request: Request) -> HtmlResponse:
        return HtmlResponse(request.url, body=b"<html></html>")


class _ReloadingSpider(Spider):
    name = "reloading"
    allowed_domains = ["a.example"]
    offsite_middleware: OffsiteMiddleware

    async def start(self):
        yield Request("http://a.example/start", callback=self.parse_start)

    async def parse_start(self, response):
        # The inline requests below are sent one after the other, so the
        # batches before and after the reload are strictly interleaved.
        # The rules in place when the spider opened allow a.example only.
        assert (await Request("http://a.example/first")).status == 200
        with pytest.raises(IgnoreRequest, match="Filtered offsite request"):
            await Request("http://b.example/first")

        self.offsite_middleware.reload_rules(self, ["b.example"])
        assert self.offsite_middleware.get_rules_version(self) is not None

        assert (await Request("http://b.example/second")).status == 200
        with pytest.raises(IgnoreRequest, match="Filtered offsite request"):
            await Request("http://a.example/second")


@coroutine_test
async def test_snapshot_interleaved_reload_during_a_crawl() -> None:
    crawler = get_crawler(
        _ReloadingSpider,
        {
            "OFFSITE_RULES_SNAPSHOT": True,
            "DOWNLOAD_HANDLERS": {
                "http": _EchoDownloadHandler,
                "https": _EchoDownloadHandler,
            },
        },
    )
    middleware: list[OffsiteMiddleware] = []

    def find_middleware(spider: Spider) -> None:
        assert crawler.engine is not None
        (mw,) = (
            found
            for found in crawler.engine.downloader.middleware.middlewares
            if isinstance(found, OffsiteMiddleware)
        )
        spider.offsite_middleware = mw  # type: ignore[attr-defined]
        middleware.append(mw)

    crawler.signals.connect(find_middleware, signal=signals.spider_opened)
    await crawler.crawl_async()

    # One request of each batch was filtered.
    assert crawler.stats.get_value("offsite/filtered") == 2
    assert crawler.stats.get_value("offsite/domains") == 2
    (mw,) = middleware
    assert mw.domains_seen == {"a.example", "b.example"}


def test_snapshot_unknown_version_counts_in_stats():
    mw, crawler, _ = _snapshot_middleware(["a.example"])
    for url in ("https://a.example/1", "https://A.Example/2"):
        request = Request(url, meta={_VERSION_KEY: "gone"})
        with pytest.raises(IgnoreRequest, match="unknown offsite rules version"):
            mw.process_request(request)
    assert mw.domains_seen == {"a.example"}
    assert crawler.stats.get_value("offsite/domains") == 1
    assert crawler.stats.get_value("offsite/filtered") == 2


def test_snapshot_allowed_request_meta_is_stamped_once():
    mw, _, spider = _snapshot_middleware(["a.example"])
    request = Request("https://a.example/1")
    assert _VERSION_KEY not in request.meta
    assert mw.process_request(request) is None
    version = request.meta[_VERSION_KEY]
    assert version == mw.get_rules_version(spider)
    # A later check reuses the stamped identifier instead of replacing it.
    mw.reload_rules(spider, ["h.example"])
    assert mw.process_request(request) is None
    assert request.meta[_VERSION_KEY] == version


def test_snapshot_spider_without_allowed_domains():
    crawler = get_crawler(Spider, {"OFFSITE_RULES_SNAPSHOT": True})
    spider = crawler._create_spider(name="a")
    crawler.spider = spider
    mw = build_from_crawler(OffsiteMiddleware, crawler)
    mw.spider_opened(spider)
    assert mw.get_rules_version(spider) is not None
    # An empty set of rules allows every request, like the default policy.
    assert mw.process_request(Request("https://anything.example/1")) is None


class _InvalidRulesSpider(Spider):
    name = "invalid-rules"
    allowed_domains = ["http://example.com"]
    custom_settings = {"OFFSITE_RULES_SNAPSHOT": True}

    async def start(self):
        yield Request("http://example.com/1")


@coroutine_test
async def test_snapshot_invalid_rules_fail_the_crawl() -> None:
    crawler = get_crawler(_InvalidRulesSpider)
    await crawler.crawl_async()
    # The spider is closed as failed instead of running without rules.
    assert crawler.stats.get_value("finish_reason") == "offsite_rules_invalid"
    assert crawler.stats.get_value("log_count/ERROR") == 1
