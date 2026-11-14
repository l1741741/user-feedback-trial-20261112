from __future__ import annotations

import codecs
import gc
import weakref
from typing import cast
from unittest import mock

import frostwork
import pytest

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import HtmlResponse, TextResponse, XmlResponse
from scrapy.http.response.text import (
    META_PREFIX_SIZE,
    MetaRefresh,
    ResponseEncodingError,
)
from scrapy.selector import Selector
from scrapy.utils.response import response_from_dict
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


class TestStrictEncoding:
    response_class = TextResponse

    def _response(self, **kwargs) -> TextResponse:
        kwargs.setdefault("url", "http://example.com")
        kwargs.setdefault("strict_encoding", True)
        return self.response_class(**kwargs)

    def test_default_mode_unchanged(self) -> None:
        """Without strict_encoding, unknown labels keep being ignored."""
        response = TextResponse(
            "http://example.com",
            headers={"Content-Type": "text/html; charset=UNKNOWN"},
            body=b"\xc2\xa3",
        )
        assert response.strict_encoding is False
        assert response.encoding_errors == "replace"
        assert response.encoding == "utf-8"
        assert response.text == "£"

    def test_strict_defaults(self) -> None:
        response = self._response()
        assert response.strict_encoding is True
        assert response.encoding_errors == "strict"
        assert response.encoding == "utf-8"
        assert response.text == ""

    @pytest.mark.parametrize(
        "value",
        ["ignore", "strict ", ""],
    )
    def test_encoding_errors_validation(self, value: str) -> None:
        with pytest.raises(ValueError, match="encoding_errors must be"):
            TextResponse(
                "http://example.com",
                strict_encoding=True,
                encoding_errors=value,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("value", ["strict", "replace"])
    def test_encoding_errors_accepted(self, value: str) -> None:
        response = self._response(
            body=b"h\xe3", encoding="utf-8", encoding_errors=value
        )
        if value == "replace":
            assert response.text == "h�"
        else:
            with pytest.raises(UnicodeDecodeError):
                response.text

    def test_explicit_encoding_wins(self) -> None:
        response = self._response(
            body=b"<meta charset='gbk'>\xa8D",
            encoding="gbk",
            headers={"Content-Type": "text/html; charset=iso-8859-1"},
        )
        assert response.encoding == "gbk"
        assert response.text == "<meta charset='gbk'>―"

    def test_bom_beats_http_and_meta(self) -> None:
        response = self._response(
            body=codecs.BOM_UTF8 + b"WORD",
            headers={"Content-Type": "text/html; charset=cp1251"},
        )
        assert response.encoding == "utf-8"
        # The BOM is stripped from the decoded text.
        assert response.text == "WORD"

    def test_http_charset_beats_meta(self) -> None:
        response = self._response(
            headers={"Content-Type": "text/html; charset=iso-8859-1"},
            body=b"<meta charset='utf-8'>\xa3",
        )
        assert response.encoding == "cp1252"
        assert response.text == "<meta charset='utf-8'>£"

    @pytest.mark.parametrize(
        ("body", "encoding"),
        [
            (b'<meta charset="utf-8">', "utf-8"),
            (b"<meta charset='gb2312'/>", "gbk"),
            (b"<META CHARSET=ISO-8859-1>", "cp1252"),
            (
                (
                    b'<meta http-equiv="content-type" '
                    b'content="text/html;charset=iso-8859-1">'
                ),
                "cp1252",
            ),
            (b"<?xml version='1.0' encoding='iso-8859-1'?><xml/>", "cp1252"),
        ],
    )
    def test_meta_encoding_used(self, body: bytes, encoding: str) -> None:
        assert self._response(body=body).encoding == encoding

    @pytest.mark.parametrize(
        "body",
        [
            b"<!-- <meta charset='iso-8859-2'> --><meta charset='utf-8'>",
            b"<!--<meta charset='iso-8859-2'>-->",
            # An unknown label hidden in a comment never raises.
            b"<!-- <meta charset='bogus-xyz'> -->",
        ],
    )
    def test_commented_meta_is_ignored(self, body: bytes) -> None:
        assert self._response(body=body).encoding == "utf-8"

    def test_xml_declaration_must_lead(self) -> None:
        response = self._response(
            body=b"<html><?xml version='1.0' encoding='iso-8859-2'?>"
        )
        assert response.encoding == "utf-8"

    def test_unclosed_comment_covers_prefix(self) -> None:
        response = self._response(body=b"<!-- <meta charset='iso-8859-2'>" + b"x" * 100)
        assert response.encoding == "utf-8"

    def test_default_utf8_when_no_source(self) -> None:
        response = self._response(body=b"plain text without any declaration")
        assert response.encoding == "utf-8"
        assert response.text == "plain text without any declaration"

    @pytest.mark.parametrize(
        ("kwargs", "source", "label", "warns"),
        [
            (
                {"encoding": "bogus-xyz"},
                "encoding",
                "bogus-xyz",
                True,
            ),
            (
                {"headers": {"Content-Type": "text/html; charset=bogus-xyz"}},
                "http",
                "bogus-xyz",
                False,
            ),
            (
                {"body": b"<meta charset='bogus-xyz'>"},
                "meta",
                "bogus-xyz",
                False,
            ),
            (
                {"body": b"<?xml version='1.0' encoding='bogus-xyz'?><xml/>"},
                "meta",
                "bogus-xyz",
                False,
            ),
        ],
    )
    def test_unknown_encoding_raises(
        self, kwargs: dict[str, object], source: str, label: str, warns: bool
    ) -> None:
        kwargs.setdefault("body", b"hi")
        if warns:
            with pytest.warns(ScrapyDeprecationWarning, match=repr(label)):
                response = self._response(**kwargs)
        else:
            response = self._response(**kwargs)
        with pytest.raises(ResponseEncodingError) as exc_info:
            response.encoding
        error = exc_info.value
        assert error.source == source
        assert error.label == label
        assert label in str(error)
        assert isinstance(error, LookupError)

    def test_replacement_charset_raises_in_strict_mode(self) -> None:
        response = self._response(
            body=b"~{Dc:C~}",
            headers={"Content-Type": "text/html; charset=hz"},
        )
        with pytest.raises(ResponseEncodingError) as exc_info:
            response.encoding
        assert exc_info.value.source == "http"
        assert exc_info.value.label == "hz"

    def test_explicit_replacement_label_raises(self) -> None:
        with pytest.warns(ScrapyDeprecationWarning, match="'hz'"):
            response = self._response(encoding="hz", body=b"hi")
        with pytest.raises(ResponseEncodingError, match="'hz'"):
            response.encoding

    @pytest.mark.parametrize(
        ("body", "encoding"),
        [
            (codecs.BOM_UTF8 + b"abc", "cp1251"),
            (b"\xff\xfeh\x00i\x00", "utf-16-be"),
            (b"\xfe\xff\x00h\x00i", "utf-16-le"),
        ],
    )
    def test_bom_conflict_raises(self, body: bytes, encoding: str) -> None:
        response = self._response(body=body, encoding=encoding)
        with pytest.raises(ResponseEncodingError) as exc_info:
            response.encoding
        error = exc_info.value
        assert error.source == "bom"
        assert encoding in error.label
        assert "!=" in error.label

    @pytest.mark.parametrize(
        ("body", "encoding"),
        [
            (b"\xff\xfeh\x00i\x00", "utf-16"),
            (b"\xff\xfeh\x00i\x00", "utf-16le"),
            (b"\xfe\xff\x00h\x00i", "utf-16be"),
            (codecs.BOM_UTF8 + b"abc", "utf-8"),
        ],
    )
    def test_compatible_bom_and_explicit_encoding(
        self, body: bytes, encoding: str
    ) -> None:
        response = self._response(body=body, encoding=encoding)
        assert response.text in {"hi", "abc"}

    def test_utf16_byte_order(self) -> None:
        little = self._response(body=b"\xff\xfeh\x00i\x00")
        assert little.encoding == "utf-16-le"
        assert little.text == "hi"
        big = self._response(body=b"\xfe\xff\x00h\x00i")
        assert big.encoding == "utf-16-be"
        assert big.text == "hi"

    def test_utf32_le_bom_reads_as_utf16le(self) -> None:
        # Browsers read a UTF-32LE BOM as a UTF-16LE one; strict mode keeps
        # that behavior, and decoding follows the BOM-named codec.
        body = b"\xff\xfe\x00\x00h\x00\x00\x00i\x00\x00\x00"
        response = self._response(body=body)
        assert response.encoding == "utf-16-le"

    def test_invalid_bytes_strict_raises_unicode_decode_error(self) -> None:
        response = self._response(body=b"PREFIX\xe3\xabSUFFIX", encoding="utf-8")
        with pytest.raises(UnicodeDecodeError) as exc_info:
            response.text
        error = exc_info.value
        assert error.encoding == "utf-8"
        assert error.start == 6
        assert error.end == 8

    def test_invalid_bytes_replace(self) -> None:
        response = self._response(
            body=b"PREFIX\xe3\xabSUFFIX",
            encoding="utf-8",
            encoding_errors="replace",
        )
        assert response.text == "PREFIX�SUFFIX"

    def test_multibyte_truncation_raises(self) -> None:
        response = self._response(body=b"\xa8", encoding="gbk")
        with pytest.raises(UnicodeDecodeError, match="incomplete multibyte"):
            response.text

    def test_truncated_utf16_raises(self) -> None:
        response = self._response(body=b"\xff\xfeh", encoding="utf-16le")
        with pytest.raises(UnicodeDecodeError, match="truncated data"):
            response.text

    def test_text_encoding_selector_consistent(self) -> None:
        response = self._response(body=b"<html><body><p>ok</p></body></html>")
        assert response.encoding == "utf-8"
        first = response.text
        assert response.text is first
        assert response.encoding == "utf-8"
        assert response.selector is response.selector  # pylint: disable=comparison-with-itself
        assert response.xpath("//p/text()").getall() == ["ok"]
        assert response.css("p::text").getall() == ["ok"]

    def test_error_is_stable_across_accesses(self) -> None:
        response = self._response(body=b"\xff\xfeh", encoding="utf-16le")
        for _ in range(3):
            with pytest.raises(UnicodeDecodeError):
                response.text

    def test_replace_body_drops_decode_cache(self) -> None:
        response = self._response(
            body=b"<meta charset='cp1252'>\xa3", encoding="cp1252"
        )
        assert response.text.endswith("£")
        replaced = response.replace(body=b"new body")
        assert replaced.strict_encoding is True
        assert replaced.encoding_errors == "strict"
        assert replaced.text == "new body"
        # The original response keeps its own cached decoding.
        assert response.text.endswith("£")

    def test_replace_body_redecodes_with_new_meta(self) -> None:
        response = self._response(body=b"old", encoding="utf-8")
        new_body = b"<meta charset='gbk'>" + "价".encode("gbk")
        replaced = response.replace(body=new_body, encoding=None)
        assert replaced.encoding == "gbk"
        assert replaced.text == "<meta charset='gbk'>价"

    def test_replace_encoding_drops_decode_cache(self) -> None:
        response = self._response(body=b"\xa3", encoding="cp1252")
        assert response.text == "£"
        replaced = response.replace(encoding="cp1251", body=b"\xa3")
        assert replaced.encoding == "cp1251"
        assert replaced.text == b"\xa3".decode("cp1251")
        assert response.text == "£"

    def test_replace_into_strict_mode(self) -> None:
        response = TextResponse(
            "http://example.com",
            headers={"Content-Type": "text/html; charset=bogus-xyz"},
            body=b"\xc2\xa3",
        )
        assert response.encoding == "utf-8"
        replaced = response.replace(strict_encoding=True)
        # The inferred default encoding is not frozen as an explicit one, so
        # the invalid header charset still surfaces in strict mode.
        assert replaced._explicit_encoding is None
        with pytest.raises(ResponseEncodingError):
            replaced.encoding

    def test_replace_into_strict_mode_keeps_explicit_encoding(self) -> None:
        response = TextResponse(
            "http://example.com",
            encoding="cp1252",
            headers={"Content-Type": "text/html; charset=bogus-xyz"},
            body=b"\xa3",
        )
        replaced = response.replace(strict_encoding=True)
        assert replaced._explicit_encoding == "cp1252"
        assert replaced.encoding == "cp1252"
        assert replaced.text == "£"

    def test_replace_out_of_strict_mode(self) -> None:
        response = self._response(
            headers={"Content-Type": "text/html; charset=bogus-xyz"},
            body=b"\xc2\xa3",
        )
        with pytest.raises(ResponseEncodingError):
            response.encoding
        replaced = response.replace(strict_encoding=False)
        assert replaced.encoding == "utf-8"
        assert replaced.text == "£"

    def test_empty_body(self) -> None:
        assert self._response(body=b"").text == ""
        response = self._response(
            body=b"", headers={"Content-Type": "text/html; charset=cp1252"}
        )
        assert response.encoding == "cp1252"
        assert response.text == ""

    def test_bom_only_body(self) -> None:
        response = self._response(body=codecs.BOM_UTF8)
        assert response.encoding == "utf-8"
        assert response.text == ""

    def test_meta_scan_is_bounded(self) -> None:
        # A declaration past the prefix is not consulted, so a huge body is
        # never scanned in full: resolution falls back to the UTF-8 default.
        late_body = b"x" * (META_PREFIX_SIZE + 1) + b"<meta charset='iso-8859-2'>"
        response = self._response(body=late_body)
        assert response.encoding == "utf-8"
        early_body = b"x" * 100 + b"<meta charset='iso-8859-2'>"
        assert self._response(body=early_body).encoding == "iso8859-2"

    def test_large_body_prefix_is_not_scanned_in_full(self) -> None:
        size = 50_000_000
        response = self._response(body=b"x" * size)
        assert response.encoding == "utf-8"
        # Slicing for the bounded prefix keeps memory use linear in the
        # prefix size, not the body size.
        assert response.body[:META_PREFIX_SIZE] == b"x" * META_PREFIX_SIZE

    def test_non_html_response_uses_same_priority(self) -> None:
        response = self._response(
            body=b"plain \xa3 text",
            headers={"Content-Type": "text/plain; charset=cp1252"},
        )
        assert response.encoding == "cp1252"
        assert response.text == "plain £ text"

    def test_header_value_is_copied(self) -> None:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        response = self._response(body=b"hi", headers=headers)
        assert response.headers is not headers
        response.headers[b"Content-Type"] = b"text/html; charset=cp1252"
        assert headers == {"Content-Type": "text/html; charset=utf-8"}

    def test_body_and_headers_remain_immutable(self) -> None:
        response = self._response(body=b"hi")
        with pytest.raises(AttributeError):
            response.body = b"other"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            response.url = "http://other.example"  # type: ignore[misc]

    def test_str_body_uses_explicit_encoding(self) -> None:
        response = self._response(body="café", encoding="utf-8")
        assert response.body == "café".encode()
        assert response.text == "café"
        assert response.encoding == "utf-8"

    def test_str_body_unknown_encoding_still_warns(self) -> None:
        with pytest.warns(ScrapyDeprecationWarning, match="'cp437'"):
            response = self._response(body="café", encoding="cp437")
        with pytest.raises(ResponseEncodingError):
            response.text

    def test_dict_roundtrip(self) -> None:
        response = self._response(body=b"hi", encoding="utf-8")
        restored = response_from_dict(response.to_dict())
        assert isinstance(restored, TextResponse)
        assert restored.strict_encoding is True
        assert restored.encoding_errors == "strict"
        assert restored.encoding == "utf-8"
        assert restored.text == "hi"


class TestStrictEncodingHtml(TestStrictEncoding):
    response_class = HtmlResponse


class TestStrictEncodingXml(TestStrictEncoding):
    response_class = XmlResponse
