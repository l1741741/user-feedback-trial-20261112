from __future__ import annotations

from typing import Any

import pytest

from scrapy.downloadermiddlewares.httpauth import HttpAuthMiddleware
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import Request
from scrapy.spiders import Spider
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler


def _build_mw(settings: dict[str, Any], spider: Spider) -> HttpAuthMiddleware:
    mw = build_from_crawler(HttpAuthMiddleware, get_crawler(settings_dict=settings))
    if getattr(spider, "http_user", "") or getattr(spider, "http_pass", ""):
        with pytest.warns(ScrapyDeprecationWarning, match="spider attributes"):
            mw.spider_opened(spider)
    else:
        mw.spider_opened(spider)
    return mw


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        # Baseline
        ({}, None),
        # Settings.
        # HTTPAUTH_DOMAIN=None allows any domain.
        (
            {"settings": {"HTTPAUTH_USER": "cu", "HTTPAUTH_DOMAIN": None}},
            b"Basic Y3U6",
        ),
        (
            {"settings": {"HTTPAUTH_PASS": "cp", "HTTPAUTH_DOMAIN": None}},
            b"Basic OmNw",
        ),
        (
            {
                "settings": {
                    "HTTPAUTH_USER": "cu",
                    "HTTPAUTH_PASS": "cp",
                    "HTTPAUTH_DOMAIN": None,
                }
            },
            b"Basic Y3U6Y3A=",
        ),
        # HTTPAUTH_DOMAIN=domain allows only that domain and subdomains.
        (
            {"settings": {"HTTPAUTH_USER": "cu", "HTTPAUTH_DOMAIN": "a.example"}},
            b"Basic Y3U6",
        ),
        (
            {
                "url": "https://s.a.example/a",
                "settings": {"HTTPAUTH_USER": "cu", "HTTPAUTH_DOMAIN": "a.example"},
            },
            b"Basic Y3U6",
        ),
        (
            {"settings": {"HTTPAUTH_USER": "cu", "HTTPAUTH_DOMAIN": "b.example"}},
            None,
        ),
        # HTTPAUTH_DOMAIN must be set if HTTPAUTH_USER or HTTPAUTH_PASS are.
        ({"settings": {"HTTPAUTH_USER": "cu"}}, ValueError),
        ({"settings": {"HTTPAUTH_PASS": "cp"}}, ValueError),
        # Spider attributes.
        # http_auth_domain=None allows any domain.
        (
            {"spider_attributes": {"http_user": "su", "http_auth_domain": None}},
            b"Basic c3U6",
        ),
        (
            {"spider_attributes": {"http_pass": "sp", "http_auth_domain": None}},
            b"Basic OnNw",
        ),
        (
            {
                "spider_attributes": {
                    "http_user": "su",
                    "http_pass": "sp",
                    "http_auth_domain": None,
                }
            },
            b"Basic c3U6c3A=",
        ),
        # http_auth_domain=domain allows only that domain and subdomains.
        (
            {"spider_attributes": {"http_user": "su", "http_auth_domain": "a.example"}},
            b"Basic c3U6",
        ),
        (
            {
                "url": "https://s.a.example/a",
                "spider_attributes": {
                    "http_user": "su",
                    "http_auth_domain": "a.example",
                },
            },
            b"Basic c3U6",
        ),
        (
            {"spider_attributes": {"http_user": "su", "http_auth_domain": "b.example"}},
            None,
        ),
        # http_auth_domain must be defined if http_user or http_pass are.
        ({"spider_attributes": {"http_user": "su"}}, AttributeError),
        # Spider attributes take priority over settings.
        (
            {
                "settings": {
                    "HTTPAUTH_USER": "cu",
                    "HTTPAUTH_PASS": "cp",
                    "HTTPAUTH_DOMAIN": None,
                },
                "spider_attributes": {"http_user": "su", "http_auth_domain": None},
            },
            b"Basic c3U6",
        ),
        # Request.meta.
        ({"meta": {"http_user": "mu"}}, b"Basic bXU6"),
        ({"meta": {"http_pass": "mp"}}, b"Basic Om1w"),
        ({"meta": {"http_user": "mu", "http_pass": "mp"}}, b"Basic bXU6bXA="),
        # Request.meta["http_auth_domain"]=domain allows only that domain and
        # its subdomains.
        (
            {"meta": {"http_user": "mu", "http_auth_domain": "a.example"}},
            b"Basic bXU6",
        ),
        (
            {
                "url": "https://s.a.example/a",
                "meta": {"http_user": "mu", "http_auth_domain": "a.example"},
            },
            b"Basic bXU6",
        ),
        ({"meta": {"http_user": "mu", "http_auth_domain": "b.example"}}, None),
        # Without http_auth_domain, credentials from Request.meta are limited
        # to the origin of the request where they were first seen.
        #
        # Note: _auth_origin is not meant to be set by users, it is set the
        # first time a request is processed by the middleware. See
        # test_origin_setdefault.
        (
            {"meta": {"_auth_origin": "https://a.example", "http_user": "mu"}},
            b"Basic bXU6",
        ),
        (
            {
                "url": "https://a.example:443",
                "meta": {"_auth_origin": "https://a.example", "http_user": "mu"},
            },
            b"Basic bXU6",
        ),
        (
            {
                "url": "https://s.a.example",
                "meta": {"_auth_origin": "https://a.example", "http_user": "mu"},
            },
            None,
        ),
        ({"meta": {"_auth_origin": "http://a.example", "http_user": "mu"}}, None),
        ({"meta": {"_auth_origin": "https://a.example:1", "http_user": "mu"}}, None),
        ({"meta": {"_auth_origin": "https://b.example", "http_user": "mu"}}, None),
        # http_auth_domain takes priority over _auth_origin.
        (
            {
                "meta": {
                    "_auth_origin": "https://b.example",
                    "http_user": "mu",
                    "http_auth_domain": "a.example",
                }
            },
            b"Basic bXU6",
        ),
        # Takes priority over settings and spider attributes.
        (
            {
                "meta": {"http_user": "mu"},
                "settings": {
                    "HTTPAUTH_USER": "cu",
                    "HTTPAUTH_PASS": "cp",
                    "HTTPAUTH_DOMAIN": None,
                },
            },
            b"Basic bXU6",
        ),
        (
            {
                "meta": {"http_user": "mu"},
                "spider_attributes": {
                    "http_user": "su",
                    "http_pass": "sp",
                    "http_auth_domain": None,
                },
            },
            b"Basic bXU6",
        ),
        # If the Authorization header is set, it is not modified.
        (
            {
                "headers": {"Authorization": "a"},
                "settings": {"HTTPAUTH_USER": "cu", "HTTPAUTH_DOMAIN": None},
            },
            b"a",
        ),
        (
            {
                "headers": {"Authorization": "a"},
                "spider_attributes": {"http_user": "su", "http_auth_domain": None},
            },
            b"a",
        ),
        ({"headers": {"Authorization": "a"}, "meta": {"http_user": "mu"}}, b"a"),
        # If a non-HTTP request is received, nothing is done.
        (
            {
                "url": "ftp://example.com",
                "spider_attributes": {"http_user": "su", "http_auth_domain": None},
            },
            None,
        ),
        ({"url": "s3://example.com", "meta": {"http_user": "mu"}}, None),
    ],
)
def test_main(config: dict[str, Any], expected: bytes | type[Exception] | None) -> None:
    url = config.get("url", "https://a.example")
    headers = config.get("headers", {})
    meta = config.get("meta", {})
    settings = config.get("settings", {})
    spider_attributes = config.get("spider_attributes", {})

    class TestSpider(Spider):
        pass

    for k, v in spider_attributes.items():
        setattr(TestSpider, k, v)

    spider = TestSpider("foo")

    if isinstance(expected, type) and issubclass(expected, Exception):
        with pytest.raises(expected):
            _build_mw(settings, spider)
        return

    mw = _build_mw(settings, spider)
    request = Request(url, headers=headers, meta=meta)
    assert mw.process_request(request) is None
    if expected is None:
        assert "Authorization" not in request.headers
    else:
        assert request.headers["Authorization"] == expected, repr(
            request.headers["Authorization"]
        )


@pytest.mark.parametrize(
    ("meta", "url", "output_value"),
    [
        ({}, "https://example.com/a", None),
        ({"http_user": "a", "_auth_origin": "foo"}, "https://example.com/a", "foo"),
        ({"http_user": "a"}, "https://example.com/a", "https://example.com"),
        ({"http_user": "a"}, "http://example.com/a", "http://example.com"),
        ({"http_user": "a"}, "https://example.com:443/a", "https://example.com"),
        ({"http_user": "a"}, "http://example.com:80/a", "http://example.com"),
        ({"http_user": "a"}, "https://example.com:80/a", "https://example.com:80"),
        ({"http_user": "a"}, "http://example.com:443/a", "http://example.com:443"),
        ({"http_user": "a"}, "https://example.com:1234/a", "https://example.com:1234"),
        ({"http_user": "a"}, "http://example.com:1234/a", "http://example.com:1234"),
        # No origin is tracked when the allowed domain is explicit.
        (
            {"http_user": "a", "http_auth_domain": "example.com"},
            "https://example.com/a",
            None,
        ),
    ],
)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "a"}])
def test_origin_setdefault(
    meta: dict[str, Any], url: str, output_value: str | None, headers: dict[str, str]
) -> None:
    """When request.meta is used for authorization, an _auth_origin meta key is
    defined on the request if not defined already."""
    mw = build_from_crawler(HttpAuthMiddleware, get_crawler())
    request = Request(url, headers=headers, meta=meta)
    assert mw.process_request(request) is None
    if output_value is None:
        assert "_auth_origin" not in request.meta
    else:
        assert request.meta["_auth_origin"] == output_value


def _mw(
    settings: dict[str, Any] | None = None,
    spidercls: type[Spider] | None = None,
) -> HttpAuthMiddleware:
    return build_from_crawler(
        HttpAuthMiddleware,
        get_crawler(spidercls or Spider, settings_dict=settings or {}),
    )


def _open(mw: HttpAuthMiddleware, spidercls: type[Spider] | None = None) -> None:
    spider = (spidercls or Spider)("foo")
    if getattr(spider, "http_user", "") or getattr(spider, "http_pass", ""):
        with pytest.warns(ScrapyDeprecationWarning, match="spider attributes"):
            mw.spider_opened(spider)
    else:
        mw.spider_opened(spider)


def _spider(**attributes: Any) -> type[Spider]:
    return type("TestSpider", (Spider,), {"name": "test", **attributes})


@pytest.mark.parametrize(
    ("origins", "url", "expected"),
    [
        # Scheme and port are part of the origin.
        (["https://a.example"], "https://a.example", b"Basic Y3U6"),
        (["https://a.example"], "https://a.example:443", b"Basic Y3U6"),
        (["https://a.example:443"], "https://a.example", b"Basic Y3U6"),
        (["https://a.example"], "https://a.example:8443", None),
        (["https://a.example:8443"], "https://a.example:8443", b"Basic Y3U6"),
        (["https://a.example:8443"], "https://a.example", None),
        (["https://a.example"], "http://a.example", None),
        (["http://a.example"], "https://a.example", None),
        (["http://a.example"], "http://a.example:80", b"Basic Y3U6"),
        (["http://a.example:80"], "https://a.example", None),
        (["https://a.example"], "http://a.example:443", None),
        # Hosts match exactly: no subdomains, prefixes or suffixes.
        (["https://a.example"], "https://b.a.example", None),
        (["https://a.example"], "https://a.example.evil.example", None),
        (["https://a.example"], "https://aa.example", None),
        (["https://a.example"], "https://evil.example/?host=a.example", None),
        # Host normalization.
        (["https://A.EXAMPLE"], "https://a.example", b"Basic Y3U6"),
        (["https://a.example"], "https://A.EXAMPLE", b"Basic Y3U6"),
        (["https://a.example."], "https://a.example", b"Basic Y3U6"),
        (["https://a.example"], "https://a.example.", b"Basic Y3U6"),
        # IPv6 addresses.
        (["http://[2001:db8::1]"], "http://[2001:db8::1]", b"Basic Y3U6"),
        (["http://[2001:DB8::1]"], "http://[2001:0db8:0:0:0:0:0:1]", b"Basic Y3U6"),
        (["http://[2001:db8::1]"], "http://[2001:db8::2]", None),
        (["http://[2001:db8::1]:8080"], "http://[2001:db8::1]:8080", b"Basic Y3U6"),
        (["http://[2001:db8::1]:8080"], "http://[2001:db8::1]", None),
        # Internationalized domain names.
        (["https://bücher.example"], "https://xn--bcher-kva.example", b"Basic Y3U6"),
        (["https://XN--BCHER-KVA.example"], "https://bücher.example", b"Basic Y3U6"),
        (["https://bücher.example"], "https://bucher.example", None),
        # Several entries.
        (
            ["http://a.example", "https://b.example:8443"],
            "https://b.example:8443",
            b"Basic Y3U6",
        ),
        (["http://a.example", "https://b.example:8443"], "https://a.example", None),
        # The path, the query and the fragment are not part of the origin.
        (["https://a.example"], "https://a.example/path?q=1#frag", b"Basic Y3U6"),
        # Non-HTTP requests are left alone.
        (["https://a.example"], "ftp://a.example", None),
    ],
)
def test_origins_matching(
    origins: list[str], url: str, expected: bytes | None
) -> None:
    mw = _mw({"HTTPAUTH_USER": "cu", "HTTPAUTH_ORIGINS": origins})
    _open(mw)
    request = Request(url)
    assert mw.process_request(request) is None
    assert request.headers.get("Authorization") == expected


@pytest.mark.parametrize(
    "origins",
    [
        "https://a.example",
        b"https://a.example",
        {"https://a.example": None},
        [1],
        [None],
        [b"https://a.example"],
        [""],
        [" "],
        [" a.example"],
        ["a.example"],
        ["//a.example"],
        ["ftp://a.example"],
        ["file:///a.example"],
        ["https://user:pass@a.example"],
        ["https://user@a.example"],
        ["https://a.example/"],
        ["https://a.example/path"],
        ["https://a.example?x=1"],
        ["https://a.example#frag"],
        ["https://a.example:0"],
        ["https://a.example:65536"],
        ["https://a.example:abc"],
        ["https://a.example:"],
        ["https://"],
        ["https:///a.example"],
        ["https://a.example\\path"],
        ["https://.example"],
        ["http://a.example", "https://b.example:8443/x"],
    ],
)
def test_origins_invalid(origins: Any) -> None:
    with pytest.raises(ValueError):
        _mw({"HTTPAUTH_ORIGINS": origins})


def test_origins_satisfy_the_credentials_requirement() -> None:
    mw = _mw({"HTTPAUTH_USER": "cu", "HTTPAUTH_ORIGINS": ["https://a.example"]})
    _open(mw)
    request = Request("https://a.example")
    mw.process_request(request)
    assert request.headers["Authorization"] == b"Basic Y3U6"


def test_origins_empty_disable_authentication() -> None:
    mw = _mw({"HTTPAUTH_USER": "cu", "HTTPAUTH_ORIGINS": []})
    _open(mw)
    request = Request("https://a.example")
    assert mw.process_request(request) is None
    assert "Authorization" not in request.headers


def test_origins_spider_attribute_satisfy_the_credentials_requirement() -> None:
    spidercls = _spider(http_user="su", http_auth_origins=["https://a.example"])
    mw = _mw(spidercls=spidercls)
    _open(mw, spidercls)
    request = Request("https://a.example")
    mw.process_request(request)
    assert request.headers["Authorization"] == b"Basic c3U6"


def test_origins_invalid_settings_fail_before_the_first_request() -> None:
    with pytest.raises(ValueError, match="HTTPAUTH_ORIGINS"):
        _mw({"HTTPAUTH_ORIGINS": ["https://a.example/path"]})


def test_origins_invalid_spider_attribute_fail_before_the_first_request() -> None:
    with pytest.raises(ValueError, match="http_auth_origins"):
        _mw(spidercls=_spider(http_auth_origins=["a.example"]))


def test_origins_do_not_override_an_explicit_header() -> None:
    mw = _mw({"HTTPAUTH_USER": "cu", "HTTPAUTH_ORIGINS": ["https://a.example"]})
    _open(mw)
    request = Request("https://a.example", headers={"Authorization": "explicit"})
    mw.process_request(request)
    assert request.headers["Authorization"] == b"explicit"


def test_origins_leave_other_requests_alone() -> None:
    mw = _mw({"HTTPAUTH_USER": "cu", "HTTPAUTH_ORIGINS": ["https://a.example"]})
    _open(mw)
    other = Request("https://b.example", headers={"Authorization": "other"})
    mw.process_request(other)
    assert other.headers["Authorization"] == b"other"
    no_auth = Request("https://b.example")
    mw.process_request(no_auth)
    assert "Authorization" not in no_auth.headers


def test_origins_do_not_modify_the_configured_list() -> None:
    origins = ["https://A.EXAMPLE.", "http://[2001:DB8::1]:8080"]
    mw = _mw({"HTTPAUTH_USER": "cu", "HTTPAUTH_ORIGINS": origins})
    _open(mw)
    mw.process_request(Request("https://a.example"))
    mw.process_request(Request("http://[2001:db8::1]:8080"))
    assert origins == ["https://A.EXAMPLE.", "http://[2001:DB8::1]:8080"]


def test_origins_judge_every_request_independently() -> None:
    """A request sent after a cross-origin redirect is judged by its own
    origin, not by the origin of the request seen before it."""
    mw = _mw({"HTTPAUTH_USER": "cu", "HTTPAUTH_ORIGINS": ["https://a.example"]})
    _open(mw)
    for url, expected in [
        ("https://a.example", b"Basic Y3U6"),
        ("https://b.example", None),
        ("https://a.example", b"Basic Y3U6"),
        ("https://c.example", None),
    ]:
        request = Request(url)
        mw.process_request(request)
        if expected is None:
            assert "Authorization" not in request.headers, url
        else:
            assert request.headers["Authorization"] == expected, url


def test_origins_do_not_leak_between_spiders() -> None:
    spider_a = _spider(http_user="su", http_auth_origins=["https://a.example"])
    mw = _mw(spidercls=spider_a)
    _open(mw, spider_a)
    request = Request("https://a.example")
    mw.process_request(request)
    assert request.headers["Authorization"] == b"Basic c3U6"

    _open(mw, _spider())
    for url in ("https://a.example", "https://b.example"):
        request = Request(url)
        mw.process_request(request)
        assert "Authorization" not in request.headers, url


def test_origins_spider_attribute_overrides_the_settings() -> None:
    mw = _mw({"HTTPAUTH_USER": "cu", "HTTPAUTH_ORIGINS": ["https://a.example"]})
    _open(mw, _spider(http_auth_origins=["https://b.example"]))
    request = Request("https://b.example")
    mw.process_request(request)
    assert request.headers["Authorization"] == b"Basic Y3U6"
    _open(mw, _spider())
    request = Request("https://a.example")
    mw.process_request(request)
    assert request.headers["Authorization"] == b"Basic Y3U6"


def test_origins_spider_custom_settings() -> None:
    class CustomSettingsSpider(Spider):
        name = "custom"
        custom_settings = {
            "HTTPAUTH_USER": "cu",
            "HTTPAUTH_ORIGINS": ["https://a.example:8443"],
        }

    mw = _mw(spidercls=CustomSettingsSpider)
    _open(mw, CustomSettingsSpider)
    request = Request("https://a.example:8443")
    mw.process_request(request)
    assert request.headers["Authorization"] == b"Basic Y3U6"
    request = Request("https://a.example")
    mw.process_request(request)
    assert "Authorization" not in request.headers


@pytest.mark.parametrize(
    ("meta", "url", "expected"),
    [
        ({"_auth_origin": "https://b.example"}, "https://a.example", b"Basic Y3U6"),
        ({"_auth_origin": "https://b.example"}, "https://b.example", None),
        ({"_auth_origin": "https://a.example"}, "https://a.example", b"Basic Y3U6"),
        ({"_auth_origin": "https://b.example"}, "https://a.example:8443", None),
    ],
)
def test_origins_ignore_the_shared_last_origin(
    meta: dict[str, Any], url: str, expected: bytes | None
) -> None:
    """Origins matching does not use the _auth_origin meta key that the
    middleware sets for meta-level credentials."""
    mw = _mw({"HTTPAUTH_USER": "cu", "HTTPAUTH_ORIGINS": ["https://a.example"]})
    _open(mw)
    request = Request(url, meta=meta)
    mw.process_request(request)
    assert request.headers.get("Authorization") == expected


def test_origins_spider_instance_attribute() -> None:
    class InstanceSpider(Spider):
        name = "instance"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.http_user = "iu"
            self.http_auth_origins = ["https://a.example"]

    crawler = get_crawler(InstanceSpider)
    crawler.spider = InstanceSpider("foo")  # type: ignore[assignment]
    mw = build_from_crawler(HttpAuthMiddleware, crawler)
    _open(mw, InstanceSpider)
    request = Request("https://a.example")
    mw.process_request(request)
    assert request.headers["Authorization"] == b"Basic aXU6"
    request = Request("https://b.example")
    mw.process_request(request)
    assert "Authorization" not in request.headers


def test_origins_empty_spider_list_disables_settings_origins() -> None:
    spidercls = _spider(http_user="su", http_auth_origins=[])
    mw = _mw(
        {"HTTPAUTH_USER": "cu", "HTTPAUTH_ORIGINS": ["https://a.example"]},
        spidercls=spidercls,
    )
    _open(mw, spidercls)
    request = Request("https://a.example")
    mw.process_request(request)
    assert "Authorization" not in request.headers


def test_origins_take_precedence_over_the_domain_setting() -> None:
    mw = _mw(
        {
            "HTTPAUTH_USER": "cu",
            "HTTPAUTH_DOMAIN": "a.example",
            "HTTPAUTH_ORIGINS": ["https://a.example:8443"],
        }
    )
    _open(mw)
    request = Request("https://a.example")
    mw.process_request(request)
    assert "Authorization" not in request.headers
    request = Request("https://a.example:8443")
    mw.process_request(request)
    assert request.headers["Authorization"] == b"Basic Y3U6"
