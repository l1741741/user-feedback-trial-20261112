from __future__ import annotations

import json
from hashlib import sha1
from typing import TYPE_CHECKING, Any, Protocol
from weakref import WeakKeyDictionary

import pytest

from scrapy.http import Request
from scrapy.utils.request import (
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
        ignore_query_params: Iterable[str] | None = None,
    ) -> bytes: ...


class TestFingerprint:
    function: _FingerprintFunction = staticmethod(fingerprint)
    cache: (
        WeakKeyDictionary[
            Request,
            dict[
                tuple[
                    tuple[bytes, ...] | None, bool, bool, str | None, tuple[bytes, ...]
                ],
                bytes,
            ],
        ]
        | WeakKeyDictionary[
            Request,
            dict[
                tuple[
                    tuple[bytes, ...] | None, bool, bool, str | None, tuple[bytes, ...]
                ],
                str,
            ],
        ]
    ) = _fingerprint_cache
    default_cache_key = (None, False, False, None, ())
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

    def test_ignore_query_params_drops_only_listed_names(self):
        url = "https://example.com/p?a=1&b=2&c=3"
        r1 = Request(url)
        r2 = Request("https://example.com/p?b=2&c=3")
        assert self.function(r1, ignore_query_params=["a"]) == self.function(r2)

    def test_ignore_query_params_empty_list_keeps_fingerprint(self):
        r1 = Request("https://example.com/p?a=1&b=2")
        assert self.function(r1) == self.function(r1, ignore_query_params=[])

    def test_ignore_query_params_is_case_sensitive(self):
        r1 = Request("https://example.com/p?A=1&b=2")
        r2 = Request("https://example.com/p?b=2")
        assert self.function(r1, ignore_query_params=["a"]) == self.function(
            r1, ignore_query_params=[]
        )
        assert self.function(r1, ignore_query_params=["A"]) == self.function(r2)

    def test_ignore_query_params_does_not_match_substrings(self):
        r1 = Request("https://example.com/p?a=1&aa=2&a-b=3&xa=4")
        r2 = Request("https://example.com/p?aa=2&a-b=3&xa=4")
        assert self.function(r1, ignore_query_params=["a"]) == self.function(r2)

    def test_ignore_query_params_does_not_touch_path(self):
        r1 = Request("https://example.com/a?x=1")
        r2 = Request("https://example.com/b?x=1")
        assert self.function(r1, ignore_query_params=["a"]) != self.function(
            r2, ignore_query_params=["a"]
        )

    def test_ignore_query_params_does_not_touch_fragment(self):
        r1 = Request("https://example.com/p?x=1#x=2")
        r2 = Request("https://example.com/p#x=2")
        assert self.function(
            r1, keep_fragments=True, ignore_query_params=["x"]
        ) == self.function(r2, keep_fragments=True)
        r3 = Request("https://example.com/p?x=1#y=2")
        assert self.function(
            r1, keep_fragments=True, ignore_query_params=["x"]
        ) != self.function(r3, keep_fragments=True, ignore_query_params=["x"])

    def test_ignore_query_params_repeated_names(self):
        with_dupes = Request("https://example.com/p?a=1&a=2&b=3")
        without = Request("https://example.com/p?b=3")
        assert self.function(with_dupes, ignore_query_params=["a"]) == self.function(
            without
        )

    @pytest.mark.parametrize(
        ("url", "without_url"),
        [
            # No equals sign.
            ("https://example.com/p?a&b=2", "https://example.com/p?b=2"),
            # Empty value.
            ("https://example.com/p?a=&b=2", "https://example.com/p?b=2"),
            # Percent-encoded and plus-encoded names.
            (
                "https://example.com/p?a%20b=1&a+c=2&b=3",
                "https://example.com/p?b=3",
            ),
            # Percent-encoded equals sign inside a value.
            (
                "https://example.com/p?a=1%3D2&b=3",
                "https://example.com/p?b=3",
            ),
        ],
    )
    def test_ignore_query_params_forms(self, url, without_url):
        rules = ["a", "a b", "a c"]
        assert self.function(Request(url), ignore_query_params=rules) == self.function(
            Request(without_url), ignore_query_params=rules
        )

    def test_ignore_query_params_plus_and_space(self):
        # A plus in a rule is a literal plus, only spelled %2B in the URL.
        literal_plus = Request("https://example.com/p?a%2Bb=1&c=2")
        space_in_url = Request("https://example.com/p?a+b=1&c=2")
        space_encoded = Request("https://example.com/p?a%20b=1&c=2")
        without = Request("https://example.com/p?c=2")

        assert self.function(
            literal_plus, ignore_query_params=["a+b"]
        ) == self.function(without, ignore_query_params=["a+b"])
        assert self.function(
            space_in_url, ignore_query_params=["a b"]
        ) == self.function(without, ignore_query_params=["a b"])
        assert self.function(
            space_encoded, ignore_query_params=["a b"]
        ) == self.function(without, ignore_query_params=["a b"])
        assert self.function(
            literal_plus, ignore_query_params=["a b"]
        ) != self.function(without, ignore_query_params=["a b"])

    @pytest.mark.parametrize(
        ("url", "rule"),
        [
            # A space in the URL is spelled + or %20, and either spelling of
            # the rule names the same parameter.
            ("https://example.com/p?a b=1&c=2", "a b"),
            ("https://example.com/p?a b=1&c=2", "a%20b"),
            ("https://example.com/p?a+b=1&c=2", "a b"),
            ("https://example.com/p?a+b=1&c=2", "a%20b"),
            ("https://example.com/p?a%20b=1&c=2", "a b"),
            ("https://example.com/p?a%20b=1&c=2", "a%20b"),
            # A plus in the URL is %2B, and only a literal plus rule names it.
            ("https://example.com/p?a%2Bb=1&c=2", "a+b"),
            ("https://example.com/p?a%2Bb=1&c=2", "a%2Bb"),
        ],
    )
    def test_ignore_query_params_percent_encoding(self, url, rule):
        without = Request("https://example.com/p?c=2")
        assert self.function(Request(url), ignore_query_params=[rule]) == self.function(
            without, ignore_query_params=[rule]
        )

    @pytest.mark.parametrize(
        ("url", "rule"),
        [
            # Neither spelling names the other parameter.
            ("https://example.com/p?a%2Bb=1&c=2", "a b"),
            ("https://example.com/p?a%2Bb=1&c=2", "a%20b"),
            ("https://example.com/p?a b=1&c=2", "a+b"),
            ("https://example.com/p?a+b=1&c=2", "a%2Bb"),
        ],
    )
    def test_ignore_query_params_percent_encoding_no_match(self, url, rule):
        without = Request("https://example.com/p?c=2")
        assert self.function(Request(url), ignore_query_params=[rule]) != self.function(
            without, ignore_query_params=[rule]
        )

    @pytest.mark.parametrize(
        ("rule", "exception"),
        [
            ("a", TypeError),
            ([b"a"], TypeError),
            ([1], TypeError),
            (["%"], ValueError),
            (["%2"], ValueError),
            (["%zz"], ValueError),
            (["%a"], ValueError),
            (["%ff"], ValueError),
        ],
    )
    def test_ignore_query_params_invalid_rules(self, rule, exception):
        request = Request("https://example.com/p?a=1")
        with pytest.raises(exception):
            self.function(request, ignore_query_params=rule)

    def test_ignore_query_params_does_not_modify_request(self):
        request = Request(
            "https://example.com/p?a=1&b=2#fragment",
            method="POST",
            body=b"body",
            headers={"X-ID": "1"},
            meta={"verbatim_url": True},
        )
        url = request.url
        headers = dict(request.headers)
        meta = dict(request.meta)
        body = request.body
        self.function(request, ignore_query_params=["a"])
        assert request.url == url
        assert dict(request.headers) == headers
        assert dict(request.meta) == meta
        assert request.body == body

    def test_ignore_query_params_empty_name_rule(self):
        with_empty_name = Request("https://example.com/p?=1&a=2")
        without = Request("https://example.com/p?a=2")
        assert self.function(
            with_empty_name, ignore_query_params=[""]
        ) == self.function(without, ignore_query_params=[""])

    def test_ignore_query_params_no_match_keeps_fingerprint(self):
        r1 = Request("https://example.com/p?a=1&b=2&empty=&noequal")
        assert self.function(r1, ignore_query_params=["c"]) == self.function(r1)

    def test_ignore_query_params_no_query_keeps_fingerprint(self):
        r1 = Request("https://example.com/p?q=1#a=2")
        assert self.function(
            r1, keep_fragments=True, ignore_query_params=["q"]
        ) == self.function(
            Request("https://example.com/p#a=2"),
            keep_fragments=True,
        )

    def test_ignore_query_params_keeps_method_and_body_apart(self):
        get = Request("https://example.com/p?a=1")
        post = Request("https://example.com/p?a=1", method="POST")
        post_body = Request("https://example.com/p?a=1", method="POST", body=b"1")
        assert self.function(get, ignore_query_params=["a"]) != self.function(
            post, ignore_query_params=["a"]
        )
        assert self.function(post, ignore_query_params=["a"]) != self.function(
            post_body, ignore_query_params=["a"]
        )

    def test_ignore_query_params_keeps_canonicalization(self):
        r1 = Request("https://example.com/p?b=2&a=1&utm=tracking")
        r2 = Request("https://example.com/p?utm=other&a=1&b=2")
        assert self.function(r1, ignore_query_params=["utm"]) == self.function(
            r2, ignore_query_params=["utm"]
        )

    def test_ignore_query_params_cache_isolation(self):
        request = Request("https://example.com/p?a=1&b=2")
        no_rules = self.function(request)
        rules_a = self.function(request, ignore_query_params=["a"])
        rules_b = self.function(request, ignore_query_params=["b"])
        assert len({no_rules, rules_a, rules_b}) == 3
        assert self.function(request) == no_rules
        assert self.function(request, ignore_query_params=["a"]) == rules_a
        assert self.function(request, ignore_query_params=["b"]) == rules_b

    def test_ignore_query_params_cache_is_stable(self):
        request = Request("https://example.com/p?a=1&b=2")
        first = self.function(request, ignore_query_params=["a", "b"])
        assert self.function(request, ignore_query_params=["b", "a"]) == first
        assert self.function(request, ignore_query_params=["a", "a", "b"]) == first
        assert self.function(request, ignore_query_params=["a", "a"]) == self.function(
            request, ignore_query_params=["a"]
        )

    def test_ignore_query_params_and_verbatim_url(self):
        r1 = Request("https://example.com/p?b=2&a=1", meta={"verbatim_url": True})
        r2 = Request("https://example.com/p?b=2", meta={"verbatim_url": True})
        assert self.function(r1, ignore_query_params=["a"]) == self.function(
            r2, ignore_query_params=["a"]
        )


class TestRequestFingerprinter:
    def test_fingerprint(self):
        crawler = get_crawler()
        request = Request("https://example.com")
        assert crawler.request_fingerprinter.fingerprint(request) == fingerprint(
            request
        )

    def test_default_ignores_nothing(self):
        crawler = get_crawler()
        r1 = Request("https://example.com/p?a=1&b=2")
        r2 = Request("https://example.com/p?b=2")
        assert crawler.request_fingerprinter.fingerprint(
            r1
        ) != crawler.request_fingerprinter.fingerprint(r2)

    @pytest.mark.parametrize(
        "configured",
        [
            ["a"],
            '["a"]',
            "a",
        ],
    )
    def test_ignore_query_params(self, configured):
        crawler = get_crawler(
            settings_dict={
                "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS": configured,
            }
        )
        r1 = Request("https://example.com/p?a=1&b=2")
        r2 = Request("https://example.com/p?b=2")
        assert crawler.request_fingerprinter.fingerprint(
            r1
        ) == crawler.request_fingerprinter.fingerprint(r2)

    def test_ignore_query_params_does_not_affect_verbatim_request(self):
        crawler = get_crawler(
            settings_dict={"REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS": ["a"]}
        )
        r1 = Request("https://example.com/p?a=1&b=2", meta={"verbatim_url": True})
        r2 = Request("https://example.com/p?b=2", meta={"verbatim_url": True})
        assert crawler.request_fingerprinter.fingerprint(
            r1
        ) == crawler.request_fingerprinter.fingerprint(r2)

    def test_custom_fingerprinter_not_affected_by_setting(self):
        class CustomFingerprinter:
            def fingerprint(self, request):
                return fingerprint(request)

        crawler = get_crawler(
            settings_dict={
                "REQUEST_FINGERPRINTER_CLASS": CustomFingerprinter,
                "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS": ["a"],
            }
        )
        r1 = Request("https://example.com/p?a=1&b=2")
        r2 = Request("https://example.com/p?b=2")
        assert crawler.request_fingerprinter.fingerprint(
            r1
        ) != crawler.request_fingerprinter.fingerprint(r2)

    @pytest.mark.parametrize(
        ("configured", "exception"),
        [
            ([1], TypeError),
            ([b"a"], TypeError),
            (["%zz"], ValueError),
            (["%2"], ValueError),
            (["%80"], ValueError),
        ],
    )
    def test_ignore_query_params_invalid(self, configured, exception):
        with pytest.raises(exception):
            get_crawler(
                settings_dict={
                    "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS": configured,
                }
            )

    def test_ignore_query_params_empty_list(self):
        crawler = get_crawler(
            settings_dict={"REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS": []}
        )
        request = Request("https://example.com/p?a=1")
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
