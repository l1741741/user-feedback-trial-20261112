from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha1
from typing import TYPE_CHECKING, Any, Protocol
from weakref import WeakKeyDictionary

import pytest

from scrapy.http import Request
from scrapy.utils.request import (
    VersionedRequestFingerprinter,
    _fingerprint_cache,
    fingerprint,
    request_httprepr,
    request_to_curl,
)
from scrapy.utils.test import get_crawler

if TYPE_CHECKING:
    from collections.abc import Iterable


@pytest.mark.parametrize(
    ("r", "expected"),
    [
        (
            Request("http://www.example.com"),
            b"GET / HTTP/1.1\r\nHost: www.example.com\r\n\r\n",
        ),
        (
            Request("http://www.example.com/some/page.html?arg=1"),
            b"GET /some/page.html?arg=1 HTTP/1.1\r\nHost: www.example.com\r\n\r\n",
        ),
        (
            Request(
                "http://www.example.com",
                method="POST",
                headers={"Content-type": b"text/html"},
                body=b"Some body",
            ),
            b"POST / HTTP/1.1\r\nHost: www.example.com\r\nContent-type: text/html\r\n\r\nSome body",
        ),
        (
            Request("http://www.example.com", headers={"Accept": None}),
            b"GET / HTTP/1.1\r\nHost: www.example.com\r\n\r\n",
        ),
    ],
)
def test_request_httprepr(r: Request, expected: bytes) -> None:
    assert request_httprepr(r) == expected


@pytest.mark.parametrize(
    "r",
    [
        Request("file:///tmp/foo.txt"),
        Request("ftp://localhost/tmp/foo.txt"),
    ],
)
def test_request_httprepr_for_non_http_request(r: Request) -> None:
    # the representation is not important but it must not fail.
    request_httprepr(r)


class _FingerprintFunction(Protocol):
    def __call__(
        self,
        request: Request,
        *,
        include_headers: Iterable[bytes | str] | None = None,
        keep_fragments: bool = False,
    ) -> bytes: ...


class TestFingerprint:
    function: _FingerprintFunction = staticmethod(fingerprint)
    cache: (
        WeakKeyDictionary[
            Request,
            dict[tuple[tuple[bytes, ...] | None, bool, bool, str | None], bytes],
        ]
        | WeakKeyDictionary[
            Request,
            dict[tuple[tuple[bytes, ...] | None, bool, bool, str | None], str],
        ]
    ) = _fingerprint_cache
    default_cache_key = (None, False, False, None)
    known_hashes: tuple[tuple[Request, bytes | str, dict[str, Any]], ...] = (
        (
            Request("http://example.org"),
            b"xs\xd7\x0c3uj\x15\xfe\xd7d\x9b\xa9\t\xe0d\xbf\x9cXD",
            {},
        ),
        (
            Request("https://example.org"),
            b"\xc04\x85P,\xaa\x91\x06\xf8t\xb4\xbd*\xd9\xe9\x8a:m\xc3l",
            {},
        ),
        (
            Request("https://example.org?a"),
            b"G\xad\xb8Ck\x19\x1c\xed\x838,\x01\xc4\xde;\xee\xa5\x94a\x0c",
            {},
        ),
        (
            Request("https://example.org?a=b"),
            b"\x024MYb\x8a\xc2\x1e\xbc>\xd6\xac*\xda\x9cF\xc1r\x7f\x17",
            {},
        ),
        (
            Request("https://example.org?a=b&a"),
            b"t+\xe8*\xfb\x84\xe3v\x1a}\x88p\xc0\xccB\xd7\x9d\xfez\x96",
            {},
        ),
        (
            Request("https://example.org?a=b&a=c"),
            b"\xda\x1ec\xd0\x9c\x08s`\xb4\x9b\xe2\xb6R\xf8k\xef\xeaQG\xef",
            {},
        ),
        (
            Request("https://example.org", method="POST"),
            b"\x9d\xcdA\x0fT\x02:\xca\xa0}\x90\xda\x05B\xded\x8aN7\x1d",
            {},
        ),
        (
            Request("https://example.org", body=b"a"),
            b"\xc34z>\xd8\x99\x8b\xda7\x05r\x99I\xa8\xa0x;\xa41_",
            {},
        ),
        (
            Request("https://example.org", method="POST", body=b"a"),
            b"5`\xe2y4\xd0\x9d\xee\xe0\xbatw\x87Q\xe8O\xd78\xfc\xe7",
            {},
        ),
        (
            Request("https://example.org#a", headers={"A": b"B"}),
            b"\xc04\x85P,\xaa\x91\x06\xf8t\xb4\xbd*\xd9\xe9\x8a:m\xc3l",
            {},
        ),
        (
            Request("https://example.org#a", headers={"A": b"B"}),
            b"]\xc7\x1f\xf2\xafG2\xbc\xa4\xfa\x99\n33\xda\x18\x94\x81U.",
            {"include_headers": ["A"]},
        ),
        (
            Request("https://example.org#a", headers={"A": b"B"}),
            b"<\x1a\xeb\x85y\xdeW\xfb\xdcq\x88\xee\xaf\x17\xdd\x0c\xbfH\x18\x1f",
            {"keep_fragments": True},
        ),
        (
            Request("https://example.org#a", headers={"A": b"B"}),
            b"\xc1\xef~\x94\x9bS\xc1\x83\t\xdcz8\x9f\xdc{\x11\x16I.\x11",
            {"include_headers": ["A"], "keep_fragments": True},
        ),
        (
            Request("https://example.org", headers=[("A", b"B"), ("a", b"C")]),
            b"\xf6\xd4\xfe\xb0e\xa4\xde\xab\xfc*\x83o=\xa8\x8a\x8f#\x9e^z",
            {"include_headers": ["A"]},
        ),
        (
            Request("https://example.org", headers={"A": None}),
            b"*\x1f\xf2\xf5a\xc2?\xda9\x11>\xff$\xed\xc2\x81\xa1J(\xae",
            {"include_headers": ["A"]},
        ),
        (
            Request("https://example.org/ab"),
            b"N\xe5l\xb8\x12@iw\xe2\xf3\x1bp\xea\xffp!u\xe2\x8a\xc6",
            {},
        ),
        (
            Request("https://example.org/a", body=b"b"),
            b"_NOv\xbco$6\xfcW\x9f\xb24g\x9f\xbb\xdd\xa82\xc5",
            {},
        ),
    )

    def test_query_string_key_order(self):
        r1 = Request("http://www.example.com/query?id=111&cat=222")
        r2 = Request("http://www.example.com/query?cat=222&id=111")
        assert self.function(r1) == self.function(r1)
        assert self.function(r1) == self.function(r2)

    def test_query_string_key_without_value(self):
        r1 = Request("http://www.example.com/hnnoticiaj1.aspx?78132,199")
        r2 = Request("http://www.example.com/hnnoticiaj1.aspx?78160,199")
        assert self.function(r1) != self.function(r2)

    def test_caching(self):
        r1 = Request("http://www.example.com/hnnoticiaj1.aspx?78160,199")
        assert self.function(r1) == self.cache[r1][self.default_cache_key]

    def test_header(self):
        r1 = Request("http://www.example.com/members/offers.html")
        r2 = Request("http://www.example.com/members/offers.html")
        r2.headers["SESSIONID"] = b"somehash"
        assert self.function(r1) == self.function(r2)

    def test_headers(self):
        r1 = Request("http://www.example.com/")
        r2 = Request("http://www.example.com/")
        r2.headers["Accept-Language"] = b"en"
        r3 = Request("http://www.example.com/")
        r3.headers["Accept-Language"] = b"en"
        r3.headers["SESSIONID"] = b"somehash"

        assert self.function(r1) == self.function(r2) == self.function(r3)

        assert self.function(r1) == self.function(
            r1, include_headers=["Accept-Language"]
        )

        assert self.function(r1) != self.function(
            r2, include_headers=["Accept-Language"]
        )

        assert self.function(
            r3, include_headers=["accept-language", "sessionid"]
        ) == self.function(r3, include_headers=["SESSIONID", "Accept-Language"])

    def test_fragment(self):
        r1 = Request("http://www.example.com/test.html")
        r2 = Request("http://www.example.com/test.html#fragment")
        assert self.function(r1) == self.function(r2)
        assert self.function(r1) == self.function(r1, keep_fragments=True)
        assert self.function(r2) != self.function(r2, keep_fragments=True)
        assert self.function(r1) != self.function(r2, keep_fragments=True)

    def test_verbatim_url(self):
        # verbatim_url requests skip URL canonicalization
        r1 = Request(
            "http://www.example.com/query?a=1&b=2", meta={"verbatim_url": True}
        )
        r2 = Request(
            "http://www.example.com/query?b=2&a=1", meta={"verbatim_url": True}
        )
        assert self.function(r1) != self.function(r2)

        # without verbatim_url, canonicalization makes query-param order irrelevant
        r3 = Request("http://www.example.com/query?a=1&b=2")
        r4 = Request("http://www.example.com/query?b=2&a=1")
        assert self.function(r3) == self.function(r4)

        # with verbatim_url, the fragment is always kept in the fingerprint
        r5 = Request(
            "http://www.example.com/test#fragment", meta={"verbatim_url": True}
        )
        r6 = Request("http://www.example.com/test", meta={"verbatim_url": True})
        assert self.function(r5) != self.function(r6)

        # keep_fragments parameter is ignored for verbatim_url requests
        assert self.function(r5) == self.function(r5, keep_fragments=True)
        assert self.function(r5) == self.function(r5, keep_fragments=False)

    def test_download_handler(self):
        r1 = Request("https://example.com")
        r2 = Request("https://example.com", meta={"download_handler": "playwright"})
        r3 = Request("https://example.com", meta={"download_handler": "http"})
        assert self.function(r1) != self.function(r2)
        assert self.function(r2) != self.function(r3)

        # An ID matching the URL scheme is the handler that would be used
        # anyway, so it does not change the fingerprint.
        r4 = Request("https://example.com", meta={"download_handler": "https"})
        assert self.function(r1) == self.function(r4)

        # IDs are case-sensitive, and URL schemes are lowercased.
        r5 = Request("HTTPS://example.com", meta={"download_handler": "HTTPS"})
        assert self.function(r1) != self.function(r5)

    def test_download_handler_caching(self):
        # The cached fingerprint must account for the handler ID, which can
        # change after the first call.
        r1 = Request("https://example.com")
        fp1 = self.function(r1)
        r1.meta["download_handler"] = "playwright"
        assert self.function(r1) != fp1

    def test_download_handler_and_verbatim_url(self):
        r1 = Request(
            "https://example.com/a b",
            meta={"verbatim_url": True, "download_handler": "playwright"},
        )
        r2 = Request("https://example.com/a b", meta={"verbatim_url": True})
        r3 = Request("https://example.com/a b", meta={"download_handler": "playwright"})
        assert len({self.function(r1), self.function(r2), self.function(r3)}) == 3

    def test_method_and_body(self):
        r1 = Request("http://www.example.com")
        r2 = Request("http://www.example.com", method="POST")
        r3 = Request("http://www.example.com", method="POST", body=b"request body")

        assert self.function(r1) != self.function(r2)
        assert self.function(r2) != self.function(r3)

    def test_request_replace(self):
        # cached fingerprint must be cleared on request copy
        r1 = Request("http://www.example.com")
        fp1 = self.function(r1)
        r2 = r1.replace(url="http://www.example.com/other")
        fp2 = self.function(r2)
        assert fp1 != fp2

    def test_part_separation(self):
        # An old implementation used to serialize request data in a way that
        # would put the body right after the URL.
        r1 = Request("http://www.example.com/foo")
        fp1 = self.function(r1)
        r2 = Request("http://www.example.com/f", body=b"oo")
        fp2 = self.function(r2)
        assert fp1 != fp2

    def test_hashes(self):
        """Test hardcoded hashes, to make sure future changes to not introduce
        backward incompatibilities."""
        actual = [
            self.function(request, **kwargs) for request, _, kwargs in self.known_hashes
        ]
        expected = [_fingerprint for _, _fingerprint, _ in self.known_hashes]
        assert actual == expected


class TestRequestFingerprinter:
    def test_fingerprint(self):
        crawler = get_crawler()
        request = Request("https://example.com")
        assert crawler.request_fingerprinter.fingerprint(request) == fingerprint(
            request
        )


class TestCustomRequestFingerprinter:
    def test_include_headers(self):
        class RequestFingerprinter:
            def fingerprint(self, request):
                return fingerprint(request, include_headers=["X-ID"])

        settings = {
            "REQUEST_FINGERPRINTER_CLASS": RequestFingerprinter,
        }
        crawler = get_crawler(settings_dict=settings)

        r1 = Request("http://www.example.com", headers={"X-ID": "1"})
        fp1 = crawler.request_fingerprinter.fingerprint(r1)
        r2 = Request("http://www.example.com", headers={"X-ID": "2"})
        fp2 = crawler.request_fingerprinter.fingerprint(r2)
        assert fp1 != fp2

    def test_dont_canonicalize(self):
        class RequestFingerprinter:
            cache: WeakKeyDictionary[Request, bytes] = WeakKeyDictionary()

            def fingerprint(self, request: Request) -> bytes:
                if request not in self.cache:
                    fp = sha1()
                    fp.update(request.url.encode())
                    self.cache[request] = fp.digest()
                return self.cache[request]

        settings = {
            "REQUEST_FINGERPRINTER_CLASS": RequestFingerprinter,
        }
        crawler = get_crawler(settings_dict=settings)

        r1 = Request("http://www.example.com?a=1&a=2")
        fp1 = crawler.request_fingerprinter.fingerprint(r1)
        r2 = Request("http://www.example.com?a=2&a=1")
        fp2 = crawler.request_fingerprinter.fingerprint(r2)
        assert fp1 != fp2

    def test_meta(self):
        class RequestFingerprinter:
            def fingerprint(self, request):
                if "fingerprint" in request.meta:
                    return request.meta["fingerprint"]
                return fingerprint(request)

        settings = {
            "REQUEST_FINGERPRINTER_CLASS": RequestFingerprinter,
        }
        crawler = get_crawler(settings_dict=settings)

        r1 = Request("http://www.example.com")
        fp1 = crawler.request_fingerprinter.fingerprint(r1)
        r2 = Request("http://www.example.com", meta={"fingerprint": "a"})
        fp2 = crawler.request_fingerprinter.fingerprint(r2)
        r3 = Request("http://www.example.com", meta={"fingerprint": "a"})
        fp3 = crawler.request_fingerprinter.fingerprint(r3)
        r4 = Request("http://www.example.com", meta={"fingerprint": "b"})
        fp4 = crawler.request_fingerprinter.fingerprint(r4)
        assert fp1 != fp2
        assert fp1 != fp4
        assert fp2 != fp4
        assert fp2 == fp3

    def test_from_crawler(self):
        class RequestFingerprinter:
            @classmethod
            def from_crawler(cls, crawler):
                return cls(crawler)

            def __init__(self, crawler):
                self._fingerprint = crawler.settings["FINGERPRINT"]

            def fingerprint(self, request):
                return self._fingerprint

        settings = {
            "REQUEST_FINGERPRINTER_CLASS": RequestFingerprinter,
            "FINGERPRINT": b"fingerprint",
        }
        crawler = get_crawler(settings_dict=settings)

        request = Request("http://www.example.com")
        fingerprint = crawler.request_fingerprinter.fingerprint(request)
        assert fingerprint == settings["FINGERPRINT"]


class TestVersionedRequestFingerprinter:
    def _fingerprinter(self, **kwargs: Any) -> VersionedRequestFingerprinter:
        return VersionedRequestFingerprinter(**kwargs)

    def test_fixed_vector_default(self) -> None:
        fp = self._fingerprinter()
        assert fp.fingerprint(Request("https://example.com/")).hex() == (
            "77abceee40e7b2b072b3d5fdc68dfb3a36a1daafd3cc43e911478a7fac3692a6"
        )

    def test_fixed_vector_configured(self) -> None:
        fp = self._fingerprinter(
            include_headers=["Accept-Language", "X-Tenant"],
            ignore_query_parameters=["utm_source"],
            ignore_query_parameter_namespaces=["track"],
            namespace="shop-eu",
        )
        request = Request(
            "https://example.com/p?a=1&utm_source=x&tracking=z",
            method="POST",
            body=b"body",
            headers={"Accept-Language": "zh-CN", "X-Tenant": "acme"},
        )
        assert fp.fingerprint(request).hex() == (
            "41eaabc573fc9542d66464c6fd9b4570700c72091d7f308d10b4d58aedb6a038"
        )

    def test_digest_length_differs_from_legacy(self) -> None:
        request = Request("https://example.com/")
        assert len(self._fingerprinter().fingerprint(request)) == 32
        assert len(fingerprint(request)) == 20
        assert self._fingerprinter().fingerprint(request) != fingerprint(request)

    def test_deterministic(self) -> None:
        fp = self._fingerprinter(include_headers=["X-Tenant"])
        request = Request("https://example.com/", headers={"X-Tenant": "a"})
        assert fp.fingerprint(request) == fp.fingerprint(request)

    @pytest.mark.parametrize(
        ("url_a", "url_b"),
        [
            # tracking query parameters are ignored
            (
                "https://example.com/p?a=1&utm_source=x",
                "https://example.com/p?a=1",
            ),
            # ignored namespaces remove keys that extend the prefix, anywhere
            (
                "https://example.com/p?tracking=1&a=2",
                "https://example.com/p?a=2",
            ),
            # dropping an ignored trailing parameter leaves the same query
            (
                "https://example.com/p?a=1&utm_source=",
                "https://example.com/p?a=1",
            ),
            # fragments never participate
            ("https://example.com/p#frag", "https://example.com/p"),
        ],
    )
    def test_equivalent_requests(self, url_a: str, url_b: str) -> None:
        fp = self._fingerprinter(
            ignore_query_parameters=["utm_source"],
            ignore_query_parameter_namespaces=["track"],
        )
        assert fp.fingerprint(Request(url_a)) == fp.fingerprint(Request(url_b))

    @pytest.mark.parametrize(
        ("url_a", "url_b"),
        [
            # a namespace is a prefix rule, so the exact name is kept
            (
                "https://example.com/p?track=1",
                "https://example.com/p",
            ),
            # percent-escapes are parsed as text, never string-replaced
            (
                "https://example.com/p?a=%3D%26",
                "https://example.com/p?a=%3d%26",
            ),
            # repeated keys keep their multiplicity
            (
                "https://example.com/p?a=1&a=1",
                "https://example.com/p?a=1",
            ),
            # a key without a value differs from a key with an empty value
            ("https://example.com/p?a", "https://example.com/p?a="),
            # parameter order is meaningful
            (
                "https://example.com/p?a=1&b=2",
                "https://example.com/p?b=2&a=1",
            ),
            # a semicolon is part of the value, not a separator, matching
            # urllib.parse and w3lib.url behavior
            (
                "https://example.com/p?a=1;b=2",
                "https://example.com/p?a=1&b=2",
            ),
        ],
    )
    def test_distinct_requests(self, url_a: str, url_b: str) -> None:
        fp = self._fingerprinter(
            ignore_query_parameters=["utm_source"],
            ignore_query_parameter_namespaces=["track"],
        )
        assert fp.fingerprint(Request(url_a)) != fp.fingerprint(Request(url_b))

    def test_ignored_parameter_does_not_match_longer_key(self) -> None:
        # exact-name ignoring must not delete keys that merely start with it
        fp = self._fingerprinter(ignore_query_parameters=["id"])
        assert fp.fingerprint(Request("https://example.com/p?id=1")) == fp.fingerprint(
            Request("https://example.com/p")
        )
        assert fp.fingerprint(
            Request("https://example.com/p?idempotent=1")
        ) != fp.fingerprint(Request("https://example.com/p"))

    def test_ignored_parameters_keep_empty_value_semantics(self) -> None:
        fp = self._fingerprinter(ignore_query_parameters=["utm"])
        # both forms of the ignored key disappear, the surviving key stays
        assert (
            fp.fingerprint(Request("https://example.com/p?a=1&utm"))
            == fp.fingerprint(Request("https://example.com/p?utm=&a=1"))
            == fp.fingerprint(Request("https://example.com/p?a=1"))
        )

    def test_header_case_insensitive_selection(self) -> None:
        fp = self._fingerprinter(include_headers=["Accept-Language"])
        lower = Request("https://example.com/", headers={"accept-language": "en"})
        upper = Request("https://example.com/", headers={"Accept-Language": "en"})
        assert fp.fingerprint(lower) == fp.fingerprint(upper)
        other = Request("https://example.com/", headers={"Accept-Language": "fr"})
        assert fp.fingerprint(lower) != fp.fingerprint(other)

    def test_unselected_header_ignored(self) -> None:
        fp = self._fingerprinter(include_headers=["X-Tenant"])
        without = Request("https://example.com/")
        with_token = Request(
            "https://example.com/", headers={"Authorization": "Bearer secret"}
        )
        assert fp.fingerprint(without) == fp.fingerprint(with_token)

    def test_missing_and_empty_header_distinguished(self) -> None:
        fp = self._fingerprinter(include_headers=["Accept-Language"])
        missing = Request("https://example.com/")
        empty = Request("https://example.com/", headers={"Accept-Language": None})
        assert fp.fingerprint(missing) != fp.fingerprint(empty)

    def test_multi_value_header_order(self) -> None:
        fp = self._fingerprinter(include_headers=["X-Tenant"])
        first = Request(
            "https://example.com/",
            headers=[("X-Tenant", b"a"), ("x-tenant", b"b")],
        )
        reversed_order = Request(
            "https://example.com/",
            headers=[("X-Tenant", b"b"), ("x-tenant", b"a")],
        )
        same = Request(
            "https://example.com/",
            headers=[("X-Tenant", b"a"), ("x-tenant", b"b")],
        )
        assert fp.fingerprint(first) != fp.fingerprint(reversed_order)
        assert fp.fingerprint(first) == fp.fingerprint(same)

    def test_framing_prevents_field_boundary_collision(self) -> None:
        fp = self._fingerprinter()
        url_only = Request("https://example.com/ab")
        split_across_fields = Request("https://example.com/a", body=b"b")
        assert fp.fingerprint(url_only) != fp.fingerprint(split_across_fields)

    def test_method_and_post_body(self) -> None:
        fp = self._fingerprinter()
        get = Request("https://example.com/")
        post_empty = Request("https://example.com/", method="POST")
        post_body = Request("https://example.com/", method="POST", body="中文".encode())
        post_other = Request(
            "https://example.com/", method="POST", body="中文x".encode()
        )
        fingerprints = {
            fp.fingerprint(get),
            fp.fingerprint(post_empty),
            fp.fingerprint(post_body),
            fp.fingerprint(post_other),
        }
        assert len(fingerprints) == 4

    def test_idna_and_chinese_urls(self) -> None:
        fp = self._fingerprinter()
        # Scrapy encodes the unicode host to punycode when building the request
        unicode_host = Request("https://例え.jp/")
        punycode_host = Request("https://xn--r8jz45g.jp/")
        assert unicode_host.url == punycode_host.url
        assert fp.fingerprint(unicode_host) == fp.fingerprint(punycode_host)
        # distinct percent-encoded Chinese paths stay distinct
        product_one = Request("https://example.example/产品/1")
        product_two = Request("https://example.example/产品/2")
        assert fp.fingerprint(product_one) != fp.fingerprint(product_two)

    def test_namespace_separates_fingerprints(self) -> None:
        request = Request("https://example.com/")
        first = self._fingerprinter(namespace="tenant-a")
        second = self._fingerprinter(namespace="tenant-b")
        assert first.fingerprint(request) != second.fingerprint(request)

    def test_config_snapshot_isolated_from_caller(self) -> None:
        headers = ["X-Tenant"]
        ignored = ["utm_source"]
        namespaces = ["track"]
        fp = self._fingerprinter(
            include_headers=headers,
            ignore_query_parameters=ignored,
            ignore_query_parameter_namespaces=namespaces,
        )
        headers.append("X-Evil")
        ignored.append("evil")
        namespaces.append("evilns")
        assert fp.include_headers == (b"x-tenant",)
        assert fp.ignore_query_parameters == ("utm_source",)
        assert fp.ignore_query_parameter_namespaces == ("track",)

    def test_exposed_config_is_immutable(self) -> None:
        fp = self._fingerprinter(include_headers=["X-Tenant"])
        with pytest.raises(AttributeError):
            fp.include_headers.append(b"x")  # type: ignore[attr-defined]

    def test_request_is_not_mutated(self) -> None:
        fp = self._fingerprinter(
            include_headers=["X-Tenant"],
            ignore_query_parameters=["utm_source"],
        )
        request = Request(
            "https://example.com/p?a=1&utm_source=x",
            method="POST",
            body=b"body",
            headers={"X-Tenant": "acme"},
        )
        snapshot = (
            request.url,
            request.body,
            request.headers.copy(),
            dict(request.meta),
        )
        fp.fingerprint(request)
        assert (
            request.url,
            request.body,
            request.headers.copy(),
            dict(request.meta),
        ) == snapshot

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"include_headers": "X-Tenant"},
            {"include_headers": b"X-Tenant"},
            {"include_headers": [""]},
            {"include_headers": [1]},
            {"ignore_query_parameters": "utm_source"},
            {"ignore_query_parameters": [""]},
            {"ignore_query_parameters": [b"utm_source"]},
            {"ignore_query_parameter_namespaces": [""]},
            {"ignore_query_parameter_namespaces": [1]},
            {"namespace": b"tenant"},
        ],
    )
    def test_invalid_config_rejected(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises((TypeError, ValueError)):
            self._fingerprinter(**kwargs)

    def test_invalid_config_rejected_before_requests(self) -> None:
        with pytest.raises(TypeError):
            get_crawler(
                settings_dict={
                    "REQUEST_FINGERPRINTER_CLASS": (
                        "scrapy.utils.request.VersionedRequestFingerprinter"
                    ),
                    "REQUEST_FINGERPRINTER_INCLUDE_HEADERS": [1],
                }
            )

    def test_error_is_not_cached(self) -> None:
        class BrokenUrlRequest:
            method = "GET"
            body = b""
            headers: dict[Any, Any] = {}

            @property
            def url(self) -> str:
                raise RuntimeError("url unavailable")

        fp = self._fingerprinter()
        with pytest.raises(RuntimeError, match="url unavailable"):
            fp.fingerprint(BrokenUrlRequest())  # type: ignore[arg-type]
        assert len(fp._cache) == 0

    def test_instances_have_independent_caches(self) -> None:
        first = self._fingerprinter(include_headers=["X-Tenant"])
        second = self._fingerprinter(include_headers=["X-Tenant"])
        request = Request("https://example.com/", headers={"X-Tenant": "a"})
        first.fingerprint(request)
        assert request in first._cache
        assert request not in second._cache

    def test_does_not_use_legacy_cache(self) -> None:
        request = Request("https://example.com/legacy")
        self._fingerprinter().fingerprint(request)
        assert request not in _fingerprint_cache

    def test_parallel_instances_equivalent_and_distinct(self) -> None:
        request = Request(
            "https://example.com/p?a=1&utm_source=x",
            headers={"Accept-Language": "zh-CN", "X-Tenant": "acme"},
        )
        shared = {
            "include_headers": ["Accept-Language", "x-tenant"],
            "ignore_query_parameters": ["utm_source"],
            "namespace": "shop-eu",
        }
        instances = [self._fingerprinter(**shared) for _ in range(4)]
        fingerprints = {fp.fingerprint(request) for fp in instances}
        assert len(fingerprints) == 1

        other_namespace = self._fingerprinter(**{**shared, "namespace": "other"})
        assert other_namespace.fingerprint(request) not in fingerprints

    def test_concurrent_fingerprints_are_stable(self) -> None:
        # Many threads hammering independent instances of the same
        # configuration must agree, while each instance only caches its own
        # work; shared request objects must not be mutated along the way.
        fingerprinter = self._fingerprinter(
            include_headers=["Accept-Language", "X-Tenant"],
            ignore_query_parameters=["utm_source"],
        )
        requests = [
            Request(
                f"https://example.com/p/{i}?a={i}&utm_source=track",
                headers={"Accept-Language": "zh-CN", "X-Tenant": str(i % 3)},
            )
            for i in range(32)
        ]
        expected = {request: fingerprinter.fingerprint(request) for request in requests}
        instances = [
            self._fingerprinter(
                include_headers=["Accept-Language", "X-Tenant"],
                ignore_query_parameters=["utm_source"],
            )
            for _ in range(8)
        ]
        snapshots = [
            (request.url, request.body, request.headers.copy()) for request in requests
        ]

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(fp.fingerprint, request)
                for fp in instances
                for request in requests
            ]
            results = [future.result() for future in futures]

        assert set(results) == set(expected.values())
        for request, digest in expected.items():
            assert all(fp.fingerprint(request) == digest for fp in instances)
        assert [
            (request.url, request.body, request.headers.copy()) for request in requests
        ] == snapshots

    def test_from_crawler_settings(self) -> None:
        crawler = get_crawler(
            settings_dict={
                "REQUEST_FINGERPRINTER_CLASS": (
                    "scrapy.utils.request.VersionedRequestFingerprinter"
                ),
                "REQUEST_FINGERPRINTER_INCLUDE_HEADERS": [
                    "Accept-Language",
                    "x-tenant",
                ],
                "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMETERS": ["utm_source"],
                "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMETER_NAMESPACES": ["track"],
                "REQUEST_FINGERPRINTER_NAMESPACE": "shop-eu",
            }
        )
        fingerprinter = crawler.request_fingerprinter
        assert isinstance(fingerprinter, VersionedRequestFingerprinter)
        assert fingerprinter.include_headers == (
            b"accept-language",
            b"x-tenant",
        )
        assert fingerprinter.ignore_query_parameters == ("utm_source",)
        assert fingerprinter.ignore_query_parameter_namespaces == ("track",)
        assert fingerprinter.namespace == "shop-eu"

        request = Request(
            "https://example.com/p?a=1&utm_source=9&tracking=z",
            headers={"X-TENANT": "acme"},
        )
        direct = VersionedRequestFingerprinter(
            include_headers=["Accept-Language", "x-tenant"],
            ignore_query_parameters=["utm_source"],
            ignore_query_parameter_namespaces=["track"],
            namespace="shop-eu",
        )
        assert fingerprinter.fingerprint(request) == direct.fingerprint(request)


class TestRequestToCurl:
    def _test_request(
        self, request_object: Request, expected_curl_command: str
    ) -> None:
        curl_command = request_to_curl(request_object)
        assert curl_command == expected_curl_command

    def test_get(self) -> None:
        request_object = Request("https://www.example.com")
        expected_curl_command = "curl -X GET https://www.example.com/"
        self._test_request(request_object, expected_curl_command)

    def test_post(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            'curl -X POST https://www.httpbin.org/post --data-raw \'{"foo": "bar"}\''
        )
        self._test_request(request_object, expected_curl_command)

    def test_headers(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            "curl -X POST https://www.httpbin.org/post"
            ' --data-raw \'{"foo": "bar"}\''
            " -H 'Content-Type: application/json' -H 'Accept: application/json'"
        )
        self._test_request(request_object, expected_curl_command)

    @pytest.mark.parametrize(
        ("headers", "expected_args"),
        [
            ({"X-Multi": ["a", "b"]}, "-H 'X-Multi: a' -H 'X-Multi: b'"),
            (
                {"User-Agent": None, "Accept": "*/*"},
                "-H 'User-Agent:' -H 'Accept: */*'",
            ),
            ({"X-Latin": b"caf\xe9"}, "-H 'X-Latin: caf\ufffd'"),
        ],
    )
    def test_header_values(self, headers: dict[str, Any], expected_args: str) -> None:
        request_object = Request("https://www.example.com", headers=headers)
        expected_curl_command = f"curl -X GET https://www.example.com/ {expected_args}"
        self._test_request(request_object, expected_curl_command)

    def test_headers_from_curl(self) -> None:
        request_object = Request(
            "https://www.example.com",
            headers={"User-Agent": None, "X-Multi": ["a", "b"]},
        )
        request_object2 = Request.from_curl(request_object.to_curl())
        assert request_object2.headers == request_object.headers

    def test_cookies_dict(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            cookies={"foo": "bar"},
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            "curl -X POST https://www.httpbin.org/post"
            " --data-raw '{\"foo\": \"bar\"}' --cookie 'foo=bar'"
        )
        self._test_request(request_object, expected_curl_command)

    def test_cookies_dict_bytes(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            cookies={b"foo": b"bar"},
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            "curl -X POST https://www.httpbin.org/post"
            " --data-raw '{\"foo\": \"bar\"}' --cookie 'foo=bar'"
        )
        self._test_request(request_object, expected_curl_command)

    def test_cookies_list_verbose(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            cookies=[
                {
                    "name": b"foo",
                    "value": b"bar",
                    "domain": "example.com",
                    "path": "/",
                    "secure": True,
                }
            ],
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            "curl -X POST https://www.httpbin.org/post"
            " --data-raw '{\"foo\": \"bar\"}' --cookie 'foo=bar'"
        )
        self._test_request(request_object, expected_curl_command)

    def test_cookies_list_verbose_non_string_value(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            cookies=[
                {
                    "name": "foo",
                    "value": 1,
                    "domain": "example.com",
                    "path": "/",
                    "secure": True,
                }
            ],
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            "curl -X POST https://www.httpbin.org/post"
            " --data-raw '{\"foo\": \"bar\"}' --cookie 'foo=1'"
        )
        self._test_request(request_object, expected_curl_command)

    def test_request_to_curl_method(self) -> None:
        request_object = Request(
            "https://www.httpbin.org/post",
            method="POST",
            body=json.dumps({"foo": "bar"}),
        )
        expected_curl_command = (
            'curl -X POST https://www.httpbin.org/post --data-raw \'{"foo": "bar"}\''
        )
        assert request_object.to_curl() == expected_curl_command
