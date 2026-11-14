from __future__ import annotations

import codecs
import gc
import json
import weakref
from typing import cast
from unittest import mock

import frostwork
import pytest

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import HtmlResponse, TextResponse, XmlResponse
from scrapy.http.response.text import (
    DuplicateJSONKeyError,
    JSONBudgetExceededError,
    MetaRefresh,
    StrictJSONError,
)
from scrapy.selector import Selector
from tests.utils.bases.http_response import TestResponseBase


class TestTextResponse(TestResponseBase):
    response_class = TextResponse

    def _links_response(self) -> TextResponse:
        return cast("TextResponse", super()._links_response())

    def _links_response_no_href(self) -> TextResponse:
        return cast("TextResponse", super()._links_response_no_href())

    def test_follow_None_encoding(self):
        # unlike the base Response, TextResponse.follow() falls back to the
        # response encoding when encoding is None instead of raising
        r = self.response_class("http://example.com", body=b"hello", encoding="cp1252")
        req = r.follow("foo", encoding=None)
        assert req.encoding == "cp1252"

    def test_replace(self) -> None:
        super().test_replace()
        r1 = self.response_class(
            "http://www.example.com", body="hello", encoding="cp866"
        )
        r2 = r1.replace(url="http://www.example.com/other")
        r3 = r1.replace(url="http://www.example.com/other", encoding="latin1")

        assert isinstance(r2, self.response_class)
        assert r2.url == "http://www.example.com/other"
        assert r2.encoding == "cp866"
        assert r3.url == "http://www.example.com/other"
        assert r3._declared_encoding() == "latin1"

    def test_unicode_url(self):
        # instantiate with unicode url without encoding (should set default encoding)
        resp = self.response_class("http://www.example.com/")
        self._assert_response_encoding(resp, "utf-8")

        # make sure urls are converted to str
        resp = self.response_class(url="http://www.example.com/", encoding="utf-8")
        assert isinstance(resp.url, str)

        resp = self.response_class(
            url="http://www.example.com/price/\xa3", encoding="utf-8"
        )
        assert resp.url == b"http://www.example.com/price/\xc2\xa3".decode()
        resp = self.response_class(
            url="http://www.example.com/price/\xa3", encoding="latin-1"
        )
        assert resp.url == "http://www.example.com/price/\xa3"
        resp = self.response_class(
            "http://www.example.com/price/\xa3",
            headers={"Content-type": ["text/html; charset=utf-8"]},
        )
        assert resp.url == b"http://www.example.com/price/\xc2\xa3".decode()
        resp = self.response_class(
            "http://www.example.com/price/\xa3",
            headers={"Content-type": ["text/html; charset=iso-8859-1"]},
        )
        assert resp.url == "http://www.example.com/price/\xa3"

    def test_unicode_body(self):
        unicode_string = (
            "\u043a\u0438\u0440\u0438\u043b\u043b\u0438\u0447\u0435\u0441\u043a\u0438\u0439 "
            "\u0442\u0435\u043a\u0441\u0442"
        )
        with pytest.raises(TypeError):
            self.response_class("http://www.example.com", body="unicode body")

        original_string = unicode_string.encode("cp1251")
        r1 = self.response_class(
            "http://www.example.com", body=original_string, encoding="cp1251"
        )

        # check response.text
        assert isinstance(r1.text, str)
        assert r1.text == unicode_string

    @pytest.mark.parametrize(
        ("body", "encoding", "text", "reused"),
        [
            ("\xa3\U0001f600", "utf8", "\xa3\U0001f600", True),
            ("\ufeffWORD", "utf-8", "WORD", False),
            ("\x80", "latin-1", "\u20ac", False),
        ],
    )
    def test_str_body_text(
        self, body: str, encoding: str, text: str, reused: bool
    ) -> None:
        r = self.response_class("http://www.example.com", body=body, encoding=encoding)
        assert r.text == text
        assert (r.text is body) is reused

    def test_encoding(self):
        r1 = self.response_class(
            "http://www.example.com",
            body=b"\xc2\xa3",
            headers={"Content-type": ["text/html; charset=utf-8"]},
        )
        r2 = self.response_class(
            "http://www.example.com", encoding="utf-8", body="\xa3"
        )
        r3 = self.response_class(
            "http://www.example.com",
            body=b"\xa3",
            headers={"Content-type": ["text/html; charset=iso-8859-1"]},
        )
        r4 = self.response_class("http://www.example.com", body=b"\xa2\xa3")
        r5 = self.response_class(
            "http://www.example.com",
            body=b"\xc2\xa3",
            headers={"Content-type": ["text/html; charset=None"]},
        )
        r6 = self.response_class(
            "http://www.example.com",
            body=b"\xa8D",
            headers={"Content-type": ["text/html; charset=gb2312"]},
        )
        r7 = self.response_class(
            "http://www.example.com",
            body=b"\xa8D",
            headers={"Content-type": ["text/html; charset=gbk"]},
        )
        r8 = self.response_class(
            "http://www.example.com",
            body=codecs.BOM_UTF8 + b"\xc2\xa3",
            headers={"Content-type": ["text/html; charset=cp1251"]},
        )
        r9 = self.response_class(
            "http://www.example.com",
            body=b"\x80",
            headers={
                "Content-type": [b"application/x-download; filename=\x80dummy.txt"]
            },
        )

        assert r1._headers_encoding() == "utf-8"
        assert r2._headers_encoding() is None
        assert r2._declared_encoding() == "utf-8"
        self._assert_response_encoding(r2, "utf-8")
        assert r3._headers_encoding() == "cp1252"
        assert r3._declared_encoding() == "cp1252"
        assert r4._headers_encoding() is None
        assert r5._headers_encoding() is None
        assert r8._headers_encoding() == "cp1251"
        assert r9._headers_encoding() is None
        assert r8._declared_encoding() == "utf-8"
        assert r9._declared_encoding() is None
        self._assert_response_encoding(r5, "utf-8")
        self._assert_response_encoding(r8, "utf-8")
        assert r4._body_inferred_encoding() is not None
        assert r4._body_inferred_encoding() != "ascii"
        assert r9._body_inferred_encoding() is not None
        assert r9._body_inferred_encoding() != "ascii"
        self._assert_response_values(r1, "utf-8", "\xa3")
        self._assert_response_values(r2, "utf-8", "\xa3")
        self._assert_response_values(r3, "iso-8859-1", "\xa3")
        self._assert_response_values(r6, "gbk", "\u2015")
        self._assert_response_values(r7, "gbk", "\u2015")

        # TextResponse (and subclasses) must be passed a encoding when instantiating with unicode bodies
        with pytest.raises(TypeError):
            self.response_class("http://www.example.com", body="\xa3")

    def test_declared_encoding_invalid(self):
        """Check that unknown declared encodings are ignored"""
        r = self.response_class(
            "http://www.example.com",
            headers={"Content-type": ["text/html; charset=UNKNOWN"]},
            body=b"\xc2\xa3",
        )
        assert r._declared_encoding() is None
        self._assert_response_values(r, "utf-8", "\xa3")

    @pytest.mark.parametrize(
        ("url", "encoding"),
        [("http://www.example.com", "cp932"), ("http://www.example.cn", "gbk")],
    )
    def test_body_inferred_encoding_of_undeclared_legacy_page(
        self, url: str, encoding: str
    ) -> None:
        body = "ICD10 国際疾病分類第10版2013年版 病名マスター".encode("shift-jis")
        r = self.response_class(url, body=body)
        assert r._declared_encoding() is None
        assert r.encoding == encoding
        assert r.text == body.decode(r.encoding)

    def test_utf16(self):
        """Test utf-16 because UnicodeDammit is known to have problems with"""
        body = b"\xff\xfeh\x00i\x00"
        r = self.response_class(
            "http://www.example.com",
            body=body,
            encoding="utf-16",
        )
        assert r.encoding == "utf-16"
        assert r.text == "hi"

    @pytest.mark.parametrize(
        ("encoding", "body", "text"),
        [
            ("cp437", "<p>é", "<p>é"),
            ("hz", b"~{Dc:C~}", "你好"),
            ("foo", b"\xc2\xa3", "£"),
            ("cp437", codecs.BOM_UTF8 + b"\xc2\xa3", "£"),
        ],
    )
    def test_non_browser_encoding(
        self, encoding: str, body: str | bytes, text: str
    ) -> None:
        with pytest.warns(ScrapyDeprecationWarning, match=repr(encoding)):
            r = self.response_class(
                "http://www.example.com", body=body, encoding=encoding
            )
        assert r.encoding == encoding
        assert r.text == text

    def test_replace_replacement_encoding(self) -> None:
        r = self.response_class(
            "http://www.example.com",
            headers={"Content-Type": ["text/html; charset=hz"]},
            body=b"~{Dc:C~}",
        )
        assert r.encoding == "replacement"
        assert r.replace(url="http://www.example.com/other").text == "�"

    def test_invalid_utf8_encoded_body_with_valid_utf8_BOM(self):
        r6 = self.response_class(
            "http://www.example.com",
            headers={"Content-type": ["text/html; charset=utf-8"]},
            body=b"\xef\xbb\xbfWORD\xe3\xab",
        )
        assert r6.encoding == "utf-8"
        assert r6.text == "WORD\ufffd"

    def test_bom_is_removed_from_body(self):
        # Inferring encoding from body also cache decoded body as sideeffect,
        # this test tries to ensure that calling response.encoding and
        # response.text in indistinct order doesn't affect final
        # values for encoding and decoded body.
        url = "http://example.com"
        body = b"\xef\xbb\xbfWORD"
        headers = {"Content-type": ["text/html; charset=utf-8"]}

        # Test response without content-type and BOM encoding
        response = self.response_class(url, body=body)
        assert response.encoding == "utf-8"
        assert response.text == "WORD"
        response = self.response_class(url, body=body)
        assert response.text == "WORD"
        assert response.encoding == "utf-8"

        # Body caching sideeffect isn't triggered when encoding is declared in
        # content-type header but BOM still need to be removed from decoded
        # body
        response = self.response_class(url, headers=headers, body=body)
        assert response.encoding == "utf-8"
        assert response.text == "WORD"
        response = self.response_class(url, headers=headers, body=body)
        assert response.text == "WORD"
        assert response.encoding == "utf-8"

    def test_replace_wrong_encoding(self):
        """Test invalid chars are replaced properly"""
        r = self.response_class(
            "http://www.example.com",
            encoding="utf-8",
            body=b"PREFIX\xe3\xabSUFFIX",
        )
        assert r.text == "PREFIX\ufffdSUFFIX"

        # Do not destroy html tags due to encoding bugs
        r = self.response_class(
            "http://example.com",
            encoding="utf-8",
            body=b"\xf0<span>value</span>",
        )
        assert "<span>value</span>" in r.text, repr(r.text)

    def test_selector(self):
        body = b"<html><head><title>Some page</title><body></body></html>"
        response = self.response_class("http://www.example.com", body=body)

        assert isinstance(response.selector, Selector)
        assert response.selector.type == "html"
        # property is cached
        assert response.selector is response.selector  # pylint: disable=comparison-with-itself
        assert response.selector.response is response

        assert response.selector.xpath("//title/text()").getall() == ["Some page"]
        assert response.selector.css("title::text").getall() == ["Some page"]
        assert response.selector.re("Some (.*)</title>") == ["page"]

    def test_selector_response_refcounting(self):
        response = self.response_class("http://www.example.com", body=b"<a>b</a>")
        selector = response.selector
        ref = weakref.ref(response)
        gc.disable()
        try:
            del response
            assert ref() is None
        finally:
            gc.enable()
        assert selector.response is None

    def test_selector_shortcuts(self):
        body = b"<html><head><title>Some page</title><body></body></html>"
        response = self.response_class("http://www.example.com", body=body)

        assert (
            response.xpath("//title/text()").getall()
            == response.selector.xpath("//title/text()").getall()
        )
        assert (
            response.css("title::text").getall()
            == response.selector.css("title::text").getall()
        )

    def test_selector_shortcuts_kwargs(self):
        body = b'<html><head><title>Some page</title><body><p class="content">A nice paragraph.</p></body></html>'
        response = self.response_class("http://www.example.com", body=body)

        assert (
            response.xpath(
                "normalize-space(//p[@class=$pclass])", pclass="content"
            ).getall()
            == response.xpath('normalize-space(//p[@class="content"])').getall()
        )
        assert (
            response.xpath(
                "//title[count(following::p[@class=$pclass])=$pcount]/text()",
                pclass="content",
                pcount=1,
            ).getall()
            == response.xpath(
                '//title[count(following::p[@class="content"])=1]/text()'
            ).getall()
        )

    def test_urljoin_with_base_url(self):
        body = b'<html><body><base href="https://example.net"></body></html>'
        joined = self.response_class("http://www.example.com", body=body).urljoin(
            "/test"
        )
        absolute = "https://example.net/test"
        assert joined == absolute

        body = b'<html><body><base href="/elsewhere"></body></html>'
        joined = self.response_class("http://www.example.com", body=body).urljoin(
            "test"
        )
        absolute = "http://www.example.com/test"
        assert joined == absolute

        body = b'<html><body><base href="/elsewhere/"></body></html>'
        joined = self.response_class("http://www.example.com", body=body).urljoin(
            "test"
        )
        absolute = "http://www.example.com/elsewhere/test"
        assert joined == absolute

    def test_follow_redirect_ignores_base_url(self):
        """The Location header is resolved against the response URL, not against
        the base URL of the document, which only applies to its own links."""
        body = b'<html><body><base href="https://example.net"></body></html>'
        response = self.response_class(
            "http://www.example.com/dir/index",
            body=body,
            headers={"Location": "/test"},
        )
        assert response.urljoin("/test") == "https://example.net/test"
        assert response.follow_redirect().url == "http://www.example.com/test"

    def test_base_url_serialized(self) -> None:
        response = self.response_class("http://www.EXAMPLE.com", body=b"")
        assert response.base_url == "http://www.example.com/"

    @pytest.mark.parametrize(
        ("body", "base_url"),
        [
            (b"", "http://www.example.com/a/b"),
            (b'<base href="c/">', "http://www.example.com/a/c/"),
            (
                b'<base target="_blank"><base href="/c"><base href="/d">',
                "http://www.example.com/c",
            ),
        ],
    )
    def test_base_url(self, body: bytes, base_url: str) -> None:
        response = self.response_class("http://www.example.com/a/b", body=body)
        assert response.base_url == base_url

    @pytest.mark.parametrize(
        ("body", "meta_refresh"),
        [
            (b"", None),
            (
                b'<base href="/c/"><meta http-equiv="refresh" content="5;url=d">',
                MetaRefresh(5.0, "http://www.example.com/c/d"),
            ),
            (
                (
                    b'<meta http-equiv="refresh" content="2;url=/a">'
                    b'<meta http-equiv="refresh" content="1;url=/b">'
                    b'<meta http-equiv="refresh" content="1;url=/c">'
                    b'<meta http-equiv="refresh" content="3;url=/d">'
                ),
                MetaRefresh(1.0, "http://www.example.com/c"),
            ),
            (
                (
                    b'<noscript><meta http-equiv="refresh" content="0;url=/a">'
                    b"</noscript><template>"
                    b'<meta http-equiv="refresh" content="0;url=/b"></template>'
                    b'<script>document.write(\'<meta http-equiv="refresh" '
                    b'content="0;url=/c">\')</script>'
                ),
                None,
            ),
        ],
    )
    def test_meta_refresh(self, body: bytes, meta_refresh: MetaRefresh | None) -> None:
        response = self.response_class("http://www.example.com", body=body)
        assert response.meta_refresh == meta_refresh

    def test_html_metadata_once(self) -> None:
        body = b'<base href="/a/"><meta http-equiv="refresh" content="0;url=b">'
        response = self.response_class("http://www.example.com", body=body)
        with mock.patch("frostwork.navigation", wraps=frostwork.navigation) as read:
            assert response.base_url == "http://www.example.com/a/"
            assert response.meta_refresh == MetaRefresh(
                0.0, "http://www.example.com/a/b"
            )
        read.assert_called_once()

    def test_html_metadata_set(self) -> None:
        body = b'<base href="/a/"><meta http-equiv="refresh" content="0;url=b">'
        response = self.response_class("http://www.example.com", body=body)
        response.base_url = "http://example.net/"
        assert response.meta_refresh == MetaRefresh(0.0, "http://www.example.com/a/b")
        assert response.base_url == "http://example.net/"
        response.meta_refresh = None
        assert response.meta_refresh is None

        response = self.response_class("http://www.example.com", body=body)
        response.meta_refresh = (1.0, "http://example.net/")
        assert response.base_url == "http://www.example.com/a/"
        assert response.meta_refresh == MetaRefresh(1.0, "http://example.net/")
        meta_refresh = MetaRefresh(2.0, "http://example.org/")
        response.meta_refresh = meta_refresh
        assert response.meta_refresh is meta_refresh

    def test_follow_selector(self):
        resp = self._links_response()
        urls = [
            "http://example.com/sample2.html",
            "http://example.com/sample3.html",
            "http://example.com/sample3.html",
            "http://example.com/sample3.html",
            "http://example.com/sample3.html#foo",
            "http://www.google.com/something",
            "http://example.com/innertag.html",
            "http://example.com/page%204.html",
        ]

        # select <a> elements
        for sellist in [resp.css("a"), resp.xpath("//a")]:
            for sel, url in zip(sellist, urls, strict=True):
                self._assert_followed_url(sel, url, response=resp)

        # select <link> elements
        self._assert_followed_url(
            Selector(text='<link href="foo"></link>').css("link")[0],
            "http://example.com/foo",
            response=resp,
        )

        # href attributes should work
        for sellist in [resp.css("a::attr(href)"), resp.xpath("//a/@href")]:
            for sel, url in zip(sellist, urls, strict=True):
                self._assert_followed_url(sel, url, response=resp)

        # non-a elements are not supported
        with pytest.raises(
            ValueError, match="Only <a> and <link> elements are supported"
        ):
            resp.follow(resp.css("div")[0])

    def test_follow_selector_list(self):
        resp = self._links_response()
        with pytest.raises(ValueError, match="SelectorList"):
            resp.follow(resp.css("a"))  # type: ignore[arg-type]

    def test_follow_selector_invalid(self):
        resp = self._links_response()
        with pytest.raises(ValueError, match="Unsupported"):
            resp.follow(resp.xpath("count(//div)")[0])

    def test_follow_selector_attribute(self):
        resp = self._links_response()
        for src in resp.css("img::attr(src)"):
            self._assert_followed_url(src, "http://example.com/sample2.jpg")

    def test_follow_selector_no_href(self):
        resp = self.response_class(
            url="http://example.com",
            body=b"<html><body><a name=123>click me</a></body></html>",
        )
        with pytest.raises(ValueError, match="no href"):
            resp.follow(resp.css("a")[0])

    def test_follow_whitespace_selector(self):
        resp = self.response_class(
            "http://example.com",
            body=b"""<html><body><a href=" foo\n">click me</a></body></html>""",
        )
        self._assert_followed_url(
            resp.css("a")[0], "http://example.com/foo", response=resp
        )
        self._assert_followed_url(
            resp.css("a::attr(href)")[0],
            "http://example.com/foo",
            response=resp,
        )

    def test_follow_encoding(self):
        resp1 = self.response_class(
            "http://example.com",
            encoding="utf8",
            body='<html><body><a href="foo?привет">click me</a></body></html>'.encode(),
        )
        self._assert_followed_url(
            resp1.css("a")[0],
            "http://example.com/foo?%D0%BF%D1%80%D0%B8%D0%B2%D0%B5%D1%82",
            response=resp1,
            encoding="utf8",
        )

        resp2 = self.response_class(
            "http://example.com",
            encoding="cp1251",
            body='<html><body><a href="foo?привет">click me</a></body></html>'.encode(
                "cp1251"
            ),
        )
        self._assert_followed_url(
            resp2.css("a")[0],
            "http://example.com/foo?%EF%F0%E8%E2%E5%F2",
            response=resp2,
            encoding="cp1251",
        )

    def test_follow_all_css(self):
        expected = [
            "http://example.com/sample3.html",
            "http://example.com/innertag.html",
        ]
        response = self._links_response()
        extracted = [r.url for r in response.follow_all(css='a[href*="example.com"]')]
        assert expected == extracted

    def test_follow_all_css_skip_invalid(self):
        expected = [
            "http://example.com/page/1/",
            "http://example.com/page/3/",
            "http://example.com/page/4/",
        ]
        response = self._links_response_no_href()
        extracted1 = [r.url for r in response.follow_all(css=".pagination a")]
        assert expected == extracted1
        extracted2 = [r.url for r in response.follow_all(response.css(".pagination a"))]
        assert expected == extracted2

    def test_follow_all_xpath(self):
        expected = [
            "http://example.com/sample3.html",
            "http://example.com/innertag.html",
        ]
        response = self._links_response()
        extracted = response.follow_all(xpath='//a[contains(@href, "example.com")]')
        assert expected == [r.url for r in extracted]

    def test_follow_all_xpath_skip_invalid(self):
        expected = [
            "http://example.com/page/1/",
            "http://example.com/page/3/",
            "http://example.com/page/4/",
        ]
        response = self._links_response_no_href()
        extracted1 = [
            r.url for r in response.follow_all(xpath='//div[@id="pagination"]/a')
        ]
        assert expected == extracted1
        extracted2 = [
            r.url
            for r in response.follow_all(response.xpath('//div[@id="pagination"]/a'))
        ]
        assert expected == extracted2

    def test_follow_all_too_many_arguments(self):
        response = self._links_response()
        with pytest.raises(
            ValueError, match="Please supply exactly one of the following arguments"
        ):
            response.follow_all(
                css='a[href*="example.com"]',
                xpath='//a[contains(@href, "example.com")]',
            )

    def test_json_response(self):
        json_body = b"""{"ip": "109.187.217.200"}"""
        json_response = self.response_class("http://www.example.com", body=json_body)
        assert json_response.json() == {"ip": "109.187.217.200"}

        text_body = b"""<html><body>text</body></html>"""
        text_response = self.response_class("http://www.example.com", body=text_body)
        with pytest.raises(
            ValueError, match=r"(Expecting value|Unexpected '<'): line 1"
        ):
            text_response.json()

    def test_json_response_non_utf8(self):
        response = self.response_class(
            "http://www.example.com",
            body='{"message": "café"}'.encode("cp1252"),
            headers={"Content-Type": "application/json"},
        )
        assert response.json() == {"message": "café"}

    def test_json_response_wrong_charset(self):
        response = self.response_class(
            "http://www.example.com",
            body='{"message": "café"}'.encode(),
            headers={"Content-Type": "application/json; charset=iso-8859-1"},
        )
        assert response.json() == {"message": "café"}

    @pytest.mark.parametrize(
        "json_body", [b"""{"ip": "109.187.217.200"}""", b"""null"""]
    )
    def test_cache_json_response(self, json_body: bytes) -> None:
        json_response = self.response_class("http://www.example.com", body=json_body)

        with mock.patch("json.loads") as mock_json:
            for _ in range(2):
                json_response.json()
            mock_json.assert_called_once_with(json_body)

    def test_strict_json_basic(self) -> None:
        response = self.response_class(
            "http://www.example.com", body=b'{"ip": "109.187.217.200"}'
        )
        assert response.json(strict=True) == {"ip": "109.187.217.200"}

    def test_strict_json_returns_builtin_containers(self) -> None:
        body = b'{"a": {"b": 1}, "c": [{"d": 2}, 3]}'
        response = self.response_class("http://www.example.com", body=body)
        result = response.json(strict=True)
        assert type(result) is dict
        assert type(result["a"]) is dict
        assert type(result["c"]) is list
        assert type(result["c"][0]) is dict
        assert result == {"a": {"b": 1}, "c": [{"d": 2}, 3]}

    @pytest.mark.parametrize(
        ("body", "path", "key"),
        [
            (b'{"price": 10, "price": 20}', "$", "price"),
            (b'{"outer": {"cursor": 1, "cursor": 2}}', "$.outer", "cursor"),
            (
                b'{"data": [{"k": 1}, {"k": 2, "k": 3}]}',
                "$.data[1]",
                "k",
            ),
            (b'[{"a": [{"b": 1, "b": 2}]}]', "$[0].a[0]", "b"),
        ],
    )
    def test_strict_json_duplicate_key_path(
        self, body: bytes, path: str, key: str
    ) -> None:
        response = self.response_class("http://www.example.com", body=body)
        with pytest.raises(DuplicateJSONKeyError) as exc_info:
            response.json(strict=True)
        assert exc_info.value.path == path
        assert exc_info.value.key == key
        assert path in str(exc_info.value)
        assert key in str(exc_info.value)
        assert isinstance(exc_info.value, StrictJSONError)

    def test_strict_json_duplicate_key_default_mode_overrides(self) -> None:
        # The default interface keeps json.loads' silent last-value-wins
        # behavior even after a strict call failed on the same response.
        body = b'{"price": 10, "price": 20}'
        response = self.response_class("http://www.example.com", body=body)
        with pytest.raises(DuplicateJSONKeyError):
            response.json(strict=True)
        assert response.json() == {"price": 20}

    @pytest.mark.parametrize(
        "body",
        [
            b'[{"id": 1}, {"id": 2}]',
            b'{"a": {"id": 1}, "b": {"id": 2}}',
            b'[{"id": 1}, [{"id": 2}]]',
        ],
    )
    def test_strict_json_same_key_distinct_objects(self, body: bytes) -> None:
        response = self.response_class("http://www.example.com", body=body)
        response.json(strict=True)  # must not raise

    @pytest.mark.parametrize(
        "body",
        [
            b'{"a\\u0062": 1, "ab": 2}',
            b'{"\\u0041": 1, "A": 2}',
            b'{"\\u00fc": 1, "\\u00FC": 2}',
        ],
    )
    def test_strict_json_escaped_equivalent_keys(self, body: bytes) -> None:
        response = self.response_class("http://www.example.com", body=body)
        with pytest.raises(DuplicateJSONKeyError) as exc_info:
            response.json(strict=True)
        # The reported key is the decoded string, not either raw escape.
        assert "\\" not in exc_info.value.key

    @pytest.mark.parametrize("body", [b"NaN", b"Infinity", b"-Infinity", b"[1, NaN]"])
    def test_strict_json_rejects_constants(self, body: bytes) -> None:
        response = self.response_class("http://www.example.com", body=body)
        with pytest.raises(StrictJSONError):
            response.json(strict=True)

    @pytest.mark.parametrize("body", [b"NaN", b"Infinity", b"-Infinity"])
    def test_json_constants_default_mode_unchanged(self, body: bytes) -> None:
        response = self.response_class("http://www.example.com", body=body)
        response.json()  # lenient default still accepts them

    def test_strict_json_user_parse_constant_takes_precedence(self) -> None:
        response = self.response_class("http://www.example.com", body=b"NaN")
        assert response.json(strict=True, parse_constant=lambda value: None) is None

    def test_strict_json_invalid_encoding_fails(self) -> None:
        body = '{"message": "café"}'.encode("cp1252")
        response = self.response_class(
            "http://www.example.com",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        # Default mode falls back to response.encoding, strict mode must not.
        assert response.json() == {"message": "café"}
        with pytest.raises(UnicodeDecodeError):
            response.json(strict=True)

    @pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
    def test_strict_json_unicode_encodings(self, encoding: str) -> None:
        body = '{"price": 10, "name": "café"}'.encode(encoding)
        response = self.response_class("http://www.example.com", body=body)
        assert response.json(strict=True) == {"price": 10, "name": "café"}

    @pytest.mark.parametrize(
        ("body", "max_depth", "path"),
        [
            (b'{"a": {"b": {"c": 1}}}', 2, "$.a.b"),
            (b"[[[1]]]", 2, "$[0][0]"),
            (b'{"a": 1}', 0, "$"),
            (b"[1]", 0, "$"),
        ],
    )
    def test_strict_json_depth_budget_exceeded(
        self, body: bytes, max_depth: int, path: str
    ) -> None:
        response = self.response_class("http://www.example.com", body=body)
        with pytest.raises(JSONBudgetExceededError) as exc_info:
            response.json(strict=True, max_depth=max_depth)
        assert exc_info.value.kind == "depth"
        assert exc_info.value.budget == max_depth
        assert exc_info.value.path == path

    @pytest.mark.parametrize(
        ("body", "max_depth"),
        [
            (b'{"a": {"b": {"c": 1}}}', 3),
            (b"[[[1]]]", 3),
            (b"1", 0),
            (b"[1]", 1),
            (b'{"a": 1}', 1),
        ],
    )
    def test_strict_json_depth_budget_within_limit(
        self, body: bytes, max_depth: int
    ) -> None:
        response = self.response_class("http://www.example.com", body=body)
        response.json(strict=True, max_depth=max_depth)  # must not raise

    @pytest.mark.parametrize(
        ("body", "max_nodes"),
        [
            (b'{"a": 1, "b": 2}', 2),  # 1 object + 2 scalars = 3 nodes
            (b"[1, 2, 3]", 3),
        ],
    )
    def test_strict_json_node_budget_exceeded(
        self, body: bytes, max_nodes: int
    ) -> None:
        response = self.response_class("http://www.example.com", body=body)
        with pytest.raises(JSONBudgetExceededError) as exc_info:
            response.json(strict=True, max_nodes=max_nodes)
        assert exc_info.value.kind == "nodes"
        assert exc_info.value.budget == max_nodes

    @pytest.mark.parametrize(
        ("body", "max_nodes"),
        [
            (b'{"a": 1, "b": 2}', 3),
            (b"[1, 2, 3]", 4),
            (b"null", 1),
        ],
    )
    def test_strict_json_node_budget_within_limit(
        self, body: bytes, max_nodes: int
    ) -> None:
        response = self.response_class("http://www.example.com", body=body)
        response.json(strict=True, max_nodes=max_nodes)  # must not raise

    @pytest.mark.parametrize(
        "kwargs",
        [{"max_depth": -1}, {"max_nodes": 0}, {"max_depth": -1, "max_nodes": 1}],
    )
    def test_strict_json_invalid_budget_arguments(self, kwargs: dict) -> None:
        response = self.response_class("http://www.example.com", body=b"{}")
        with pytest.raises(ValueError, match=r"max_depth|max_nodes"):
            response.json(strict=True, **kwargs)

    @pytest.mark.parametrize(
        "kwargs", [{"max_depth": 1}, {"max_nodes": 1}, {"max_depth": 1, "max_nodes": 1}]
    )
    def test_json_budget_without_strict_rejected(self, kwargs: dict) -> None:
        response = self.response_class("http://www.example.com", body=b"{}")
        with pytest.raises(ValueError, match="strict"):
            response.json(**kwargs)

    def test_strict_and_default_caches_are_separate(self) -> None:
        response = self.response_class("http://www.example.com", body=b'{"x": 1}')
        with mock.patch("json.loads", wraps=json.loads) as mock_loads:
            for _ in range(2):
                response.json()
            for _ in range(2):
                response.json(strict=True)
        assert mock_loads.call_count == 2
        assert response.json() == {"x": 1}
        assert response.json(strict=True) == {"x": 1}
        assert len(response._cached_strict_json) == 1

    def test_strict_cache_keyed_by_arguments(self) -> None:
        response = self.response_class("http://www.example.com", body=b'{"x": 1}')
        response.json(strict=True)
        response.json(strict=True, max_depth=5)
        response.json(strict=True, parse_int=int)
        assert len(response._cached_strict_json) == 3
        # Repeating the exact same combination adds no entry.
        response.json(strict=True)
        assert len(response._cached_strict_json) == 3

    def test_strict_failure_is_not_cached_as_success(self) -> None:
        body = b'{"a": 1, "a": 2}'
        response = self.response_class("http://www.example.com", body=body)
        for _ in range(2):
            with pytest.raises(DuplicateJSONKeyError):
                response.json(strict=True)
        assert response._cached_strict_json == {}

    def test_json_cache_after_replace_reparses_new_body(self) -> None:
        response = self.response_class("http://www.example.com", body=b'{"a": 1}')
        assert response.json() == {"a": 1}
        assert response.json(strict=True) == {"a": 1}
        replaced = response.replace(body=b'{"b": 2}')
        assert replaced.json() == {"b": 2}
        assert replaced.json(strict=True) == {"b": 2}
        # The original response keeps its own body and cached values.
        assert response.body == b'{"a": 1}'
        assert response.json() == {"a": 1}

    def test_strict_json_does_not_modify_body_or_headers(self) -> None:
        body = b'{"a": 1, "a": 2}'
        response = self.response_class(
            "http://www.example.com",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        headers_before = dict(response.headers)
        with pytest.raises(DuplicateJSONKeyError):
            response.json(strict=True)
        assert response.body == body
        assert dict(response.headers) == headers_before

    def test_strict_json_user_object_pairs_hook_runs_and_duplicates_detected(
        self,
    ) -> None:
        def user_hook(pairs: list) -> dict:
            return {"pairs": pairs}

        response = self.response_class(
            "http://www.example.com", body=b'{"k": 1, "k": 2}'
        )
        with pytest.raises(DuplicateJSONKeyError):
            response.json(strict=True, object_pairs_hook=user_hook)

        clean = self.response_class("http://www.example.com", body=b'{"k": 1}')
        assert clean.json(strict=True, object_pairs_hook=user_hook) == {
            "pairs": [("k", 1)]
        }

    def test_strict_json_user_object_hook_runs(self) -> None:
        def user_hook(obj: dict) -> dict:
            obj["hooked"] = True
            return obj

        response = self.response_class("http://www.example.com", body=b'{"k": 1}')
        assert response.json(strict=True, object_hook=user_hook) == {
            "k": 1,
            "hooked": True,
        }

    def test_strict_json_duplicate_detected_when_hook_replaces_marker(
        self,
    ) -> None:
        # The user hook turns every object into a list, so the offending
        # object's path cannot be located; the key must still be reported.
        def user_hook(obj: dict) -> list:
            return list(obj.items())

        response = self.response_class(
            "http://www.example.com", body=b'{"k": 1, "k": 2}'
        )
        with pytest.raises(DuplicateJSONKeyError) as exc_info:
            response.json(strict=True, object_hook=user_hook)
        assert exc_info.value.key == "k"
        assert exc_info.value.path == "$"

    def test_strict_json_deep_nesting_does_not_recurse(self) -> None:
        # A document far deeper than the interpreter limit is reported through
        # the depth budget instead of crashing with RecursionError.
        depth = 2000
        body = b"[" * depth + b"1" + b"]" * depth
        response = self.response_class("http://www.example.com", body=body)
        with pytest.raises(JSONBudgetExceededError) as exc_info:
            response.json(strict=True, max_depth=100)
        assert exc_info.value.kind == "depth"


class TestHtmlResponse(TestTextResponse):
    response_class = HtmlResponse

    def test_html_encoding(self):
        body = b"""<html><head><title>Some page</title>
        <meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">
        </head><body>Price: \xa3100</body></html>'
        """
        r1 = self.response_class("http://www.example.com", body=body)
        self._assert_response_values(r1, "iso-8859-1", body)

        body = b"""<?xml version="1.0" encoding="iso-8859-1"?>
        <!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
        Price: \xa3100
        """
        r2 = self.response_class("http://www.example.com", body=body)
        self._assert_response_values(r2, "iso-8859-1", body)

        # for conflicting declarations headers must take precedence
        body = b"""<html><head><title>Some page</title>
        <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
        </head><body>Price: \xa3100</body></html>'
        """
        r3 = self.response_class(
            "http://www.example.com",
            body=body,
            headers={"Content-type": ["text/html; charset=iso-8859-1"]},
        )
        self._assert_response_values(r3, "iso-8859-1", body)

        # make sure replace() preserves the encoding of the original response
        body = b"New body \xa3"
        r4 = r3.replace(body=body)
        self._assert_response_values(r4, "iso-8859-1", body)

    def test_html5_meta_charset(self):
        body = b"""<html><head><meta charset="gb2312" /><title>Some page</title><body>bla bla</body>"""
        r1 = self.response_class("http://www.example.com", body=body)
        self._assert_response_values(r1, "gb2312", body)


class TestXmlResponse(TestTextResponse):
    response_class = XmlResponse

    def test_xml_encoding(self):
        body = b"<xml></xml>"
        r1 = self.response_class("http://www.example.com", body=body)
        self._assert_response_values(r1, "utf-8", body)

        body = b"""<?xml version="1.0" encoding="iso-8859-1"?><xml></xml>"""
        r2 = self.response_class("http://www.example.com", body=body)
        self._assert_response_values(r2, "iso-8859-1", body)

        # make sure replace() preserves the explicit encoding passed in the __init__ method
        body = b"""<?xml version="1.0" encoding="iso-8859-1"?><xml></xml>"""
        r3 = self.response_class("http://www.example.com", body=body, encoding="utf-8")
        body2 = b"New body"
        r4 = r3.replace(body=body2)
        self._assert_response_values(r4, "utf-8", body2)

    def test_replace_encoding(self):
        # make sure replace() keeps the previous encoding unless overridden explicitly
        body = b"""<?xml version="1.0" encoding="iso-8859-1"?><xml></xml>"""
        body2 = b"""<?xml version="1.0" encoding="utf-8"?><xml></xml>"""
        r5 = self.response_class("http://www.example.com", body=body)
        r6 = r5.replace(body=body2)
        r7 = r5.replace(body=body2, encoding="utf-8")
        self._assert_response_values(r5, "iso-8859-1", body)
        self._assert_response_values(r6, "iso-8859-1", body2)
        self._assert_response_values(r7, "utf-8", body2)

    def test_selector(self):
        body = b'<?xml version="1.0" encoding="utf-8"?><xml><elem>value</elem></xml>'
        response = self.response_class("http://www.example.com", body=body)

        assert isinstance(response.selector, Selector)
        assert response.selector.type == "xml"
        # property is cached
        assert response.selector is response.selector  # pylint: disable=comparison-with-itself
        assert response.selector.response is response

        assert response.selector.xpath("//elem/text()").getall() == ["value"]

    def test_selector_shortcuts(self):
        body = b'<?xml version="1.0" encoding="utf-8"?><xml><elem>value</elem></xml>'
        response = self.response_class("http://www.example.com", body=body)

        assert (
            response.xpath("//elem/text()").getall()
            == response.selector.xpath("//elem/text()").getall()
        )

    def test_selector_shortcuts_kwargs(self):
        body = b"""<?xml version="1.0" encoding="utf-8"?>
        <xml xmlns:somens="http://scrapy.org">
        <somens:elem>value</somens:elem>
        </xml>"""
        response = self.response_class("http://www.example.com", body=body)

        assert (
            response.xpath(
                "//s:elem/text()", namespaces={"s": "http://scrapy.org"}
            ).getall()
            == response.selector.xpath(
                "//s:elem/text()", namespaces={"s": "http://scrapy.org"}
            ).getall()
        )

        response.selector.register_namespace("s2", "http://scrapy.org")
        assert (
            response.xpath(
                "//s1:elem/text()", namespaces={"s1": "http://scrapy.org"}
            ).getall()
            == response.selector.xpath("//s2:elem/text()").getall()
        )


class CustomResponse(TextResponse):
    attributes = (*TextResponse.attributes, "foo", "bar")

    def __init__(self, *args, **kwargs) -> None:
        self.foo = kwargs.pop("foo", None)
        self.bar = kwargs.pop("bar", None)
        self.lost = kwargs.pop("lost", None)
        super().__init__(*args, **kwargs)


class TestCustomResponse(TestTextResponse):
    response_class = CustomResponse

    def test_copy(self) -> None:
        super().test_copy()
        r1 = self.response_class(
            url="https://example.org",
            status=200,
            foo="foo",
            bar="bar",
            lost="lost",
        )
        r2 = r1.copy()
        assert isinstance(r2, self.response_class)
        assert r1.foo == r2.foo
        assert r1.bar == r2.bar
        assert r1.lost == "lost"
        assert r2.lost is None

    def test_replace(self) -> None:
        super().test_replace()
        r1 = self.response_class(
            url="https://example.org",
            status=200,
            foo="foo",
            bar="bar",
            lost="lost",
        )

        r2 = r1.replace(foo="new-foo", bar="new-bar", lost="new-lost")
        assert isinstance(r2, self.response_class)
        assert r1.foo == "foo"
        assert r1.bar == "bar"
        assert r1.lost == "lost"
        assert r2.foo == "new-foo"
        assert r2.bar == "new-bar"
        assert r2.lost == "new-lost"

        r3 = r1.replace(foo="new-foo", bar="new-bar")
        assert isinstance(r3, self.response_class)
        assert r1.foo == "foo"
        assert r1.bar == "bar"
        assert r1.lost == "lost"
        assert r3.foo == "new-foo"
        assert r3.bar == "new-bar"
        assert r3.lost is None

        r4 = r1.replace(foo="new-foo")
        assert isinstance(r4, self.response_class)
        assert r1.foo == "foo"
        assert r1.bar == "bar"
        assert r1.lost == "lost"
        assert r4.foo == "new-foo"
        assert r4.bar == "bar"
        assert r4.lost is None

        with pytest.raises(
            TypeError,
            match=r"__init__\(\) got an unexpected keyword argument 'unknown'",
        ):
            r1.replace(unknown="unknown")
