from __future__ import annotations

import pickle
import re
from typing import Any

import pytest
from w3lib.url import canonicalize_url

from scrapy.http import HtmlResponse, TextResponse, XmlResponse
from scrapy.link import Link
from scrapy.linkextractors import lxmlhtml
from scrapy.linkextractors.lxmlhtml import (
    LxmlLinkExtractor,
    LxmlParserLinkExtractor,
    _parse_srcset,
)
from tests import get_testdata


def _parse(value: str, **kwargs: Any):
    kwargs.setdefault("on_error", "skip")
    kwargs.setdefault("max_candidates", 100)
    kwargs.setdefault("max_chars", 100_000)
    return _parse_srcset(value, **kwargs)


class TestLxmlLinkExtractor:
    def setup_method(self):
        body = get_testdata("link_extractor", "linkextractor.html")
        self.response = HtmlResponse(url="http://example.com/index", body=body)

    def test_urls_type(self):
        """Test that the resulting urls are str objects"""
        lx = LxmlLinkExtractor()
        assert all(
            isinstance(link.url, str) for link in lx.extract_links(self.response)
        )

    def test_extract_all_links(self):
        lx = LxmlLinkExtractor()
        page4_url = "http://example.com/page%204.html"

        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html#foo",
                text="sample 3 repetition with fragment",
            ),
            Link(url="http://www.google.com/something", text=""),
            Link(url="http://example.com/innertag.html", text="inner tag"),
            Link(url=page4_url, text="href with whitespaces"),
        ]

    def test_extract_filter_allow(self):
        lx = LxmlLinkExtractor(allow=("sample",))
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html#foo",
                text="sample 3 repetition with fragment",
            ),
        ]

    def test_extract_filter_allow_with_duplicates(self):
        lx = LxmlLinkExtractor(allow=("sample",), unique=False)
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html",
                text="sample 3 repetition",
            ),
            Link(
                url="http://example.com/sample3.html",
                text="sample 3 repetition",
            ),
            Link(
                url="http://example.com/sample3.html#foo",
                text="sample 3 repetition with fragment",
            ),
        ]

    def test_extract_filter_allow_with_duplicates_canonicalize(self):
        lx = LxmlLinkExtractor(allow=("sample",), unique=False, canonicalize=True)
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html",
                text="sample 3 repetition",
            ),
            Link(
                url="http://example.com/sample3.html",
                text="sample 3 repetition",
            ),
            Link(
                url="http://example.com/sample3.html",
                text="sample 3 repetition with fragment",
            ),
        ]

    def test_extract_filter_allow_no_duplicates_canonicalize(self):
        lx = LxmlLinkExtractor(allow=("sample",), unique=True, canonicalize=True)
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
        ]

    def test_extract_filter_allow_and_deny(self):
        lx = LxmlLinkExtractor(allow=("sample",), deny=("3",))
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
        ]

    def test_extract_filter_allowed_domains(self):
        lx = LxmlLinkExtractor(allow_domains=("google.com",))
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://www.google.com/something", text=""),
        ]

    def test_extraction_using_single_values(self):
        """Test the extractor's behaviour among different situations"""

        lx = LxmlLinkExtractor(allow="sample")
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html#foo",
                text="sample 3 repetition with fragment",
            ),
        ]

        lx = LxmlLinkExtractor(allow="sample", deny="3")
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
        ]

        lx = LxmlLinkExtractor(allow_domains="google.com")
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://www.google.com/something", text=""),
        ]

        lx = LxmlLinkExtractor(deny_domains="example.com")
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://www.google.com/something", text=""),
        ]

    def test_nofollow(self):
        """Test the extractor's behaviour for links with rel='nofollow'"""

        html = b"""<html><head><title>Page title</title></head>
        <body>
        <div class='links'>
        <p><a href="/about.html">About us</a></p>
        </div>
        <div>
        <p><a href="/follow.html">Follow this link</a></p>
        </div>
        <div>
        <p><a href="/nofollow.html" rel="nofollow">Dont follow this one</a></p>
        </div>
        <div>
        <p><a href="/nofollow2.html" rel="blah">Choose to follow or not</a></p>
        </div>
        <div>
        <p><a href="http://google.com/something" rel="external nofollow">External link not to follow</a></p>
        </div>
        </body></html>"""
        response = HtmlResponse("http://example.org/somepage/index.html", body=html)

        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(url="http://example.org/about.html", text="About us"),
            Link(url="http://example.org/follow.html", text="Follow this link"),
            Link(
                url="http://example.org/nofollow.html",
                text="Dont follow this one",
                nofollow=True,
            ),
            Link(
                url="http://example.org/nofollow2.html",
                text="Choose to follow or not",
            ),
            Link(
                url="http://google.com/something",
                text="External link not to follow",
                nofollow=True,
            ),
        ]

    def test_matches(self):
        url1 = "http://lotsofstuff.com/stuff1/index"
        url2 = "http://evenmorestuff.com/uglystuff/index"

        lx = LxmlLinkExtractor(allow=(r"stuff1",))
        assert lx.matches(url1)
        assert not lx.matches(url2)

        lx = LxmlLinkExtractor(deny=(r"uglystuff",))
        assert lx.matches(url1)
        assert not lx.matches(url2)

        lx = LxmlLinkExtractor(allow_domains=("evenmorestuff.com",))
        assert not lx.matches(url1)
        assert lx.matches(url2)

        lx = LxmlLinkExtractor(deny_domains=("lotsofstuff.com",))
        assert not lx.matches(url1)
        assert lx.matches(url2)

        lx = LxmlLinkExtractor(
            allow=["blah1"],
            deny=["blah2"],
            allow_domains=["blah1.com"],
            deny_domains=["blah2.com"],
        )
        assert lx.matches("http://blah1.com/blah1")
        assert not lx.matches("http://blah1.com/blah2")
        assert not lx.matches("http://blah2.com/blah1")
        assert not lx.matches("http://blah2.com/blah2")

    def test_restrict_xpaths(self):
        lx = LxmlLinkExtractor(restrict_xpaths=('//div[@id="subwrapper"]',))
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
        ]

    def test_restrict_xpaths_encoding(self):
        """Test restrict_xpaths with encodings"""
        html = b"""<html><head><title>Page title</title></head>
        <body><p><a href="item/12.html">Item 12</a></p>
        <div class='links'>
        <p><a href="/about.html">About us\xa3</a></p>
        </div>
        <div>
        <p><a href="/nofollow.html">This shouldn't be followed</a></p>
        </div>
        </body></html>"""
        response = HtmlResponse(
            "http://example.org/somepage/index.html",
            body=html,
            encoding="windows-1252",
        )

        lx = LxmlLinkExtractor(restrict_xpaths="//div[@class='links']")
        assert lx.extract_links(response) == [
            Link(url="http://example.org/about.html", text="About us\xa3")
        ]

    def test_restrict_xpaths_with_html_entities(self):
        html = b'<html><body><p><a href="/&hearts;/you?c=&euro;">text</a></p></body></html>'
        response = HtmlResponse(
            "http://example.org/somepage/index.html",
            body=html,
            encoding="iso8859-15",
        )
        links = LxmlLinkExtractor(restrict_xpaths="//p").extract_links(response)
        assert links == [
            Link(url="http://example.org/%E2%99%A5/you?c=%A4", text="text")
        ]

    def test_restrict_xpaths_concat_in_handle_data(self):
        """html entities cause SGMLParser to call handle_data hook twice"""
        body = b"""<html><body><div><a href="/foo">&gt;\xbe\xa9&lt;\xb6\xab</a></body></html>"""
        response = HtmlResponse("http://example.org", body=body, encoding="gb18030")
        lx = LxmlLinkExtractor(restrict_xpaths="//div")
        assert lx.extract_links(response) == [
            Link(
                url="http://example.org/foo",
                text=">\u4eac<\u4e1c",
                fragment="",
                nofollow=False,
            )
        ]

    def test_restrict_css(self):
        lx = LxmlLinkExtractor(restrict_css=("#subwrapper a",))
        assert lx.extract_links(self.response) == [
            Link(url="http://example.com/sample2.html", text="sample 2")
        ]

    def test_restrict_css_and_restrict_xpaths_together(self):
        lx = LxmlLinkExtractor(
            restrict_xpaths=('//div[@id="subwrapper"]',),
            restrict_css=("#subwrapper + a",),
        )
        assert list(lx.extract_links(self.response)) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
        ]

    def test_area_tag_with_unicode_present(self):
        body = b"""<html><body>\xbe\xa9<map><area href="http://example.org/foo" /></map></body></html>"""
        response = HtmlResponse("http://example.org", body=body, encoding="utf-8")
        lx = LxmlLinkExtractor()
        lx.extract_links(response)
        lx.extract_links(response)
        lx.extract_links(response)
        assert lx.extract_links(response) == [
            Link(
                url="http://example.org/foo",
                text="",
                fragment="",
                nofollow=False,
            )
        ]

    def test_encoded_url(self):
        body = b"""<html><body><div><a href="?page=2">BinB</a></body></html>"""
        response = HtmlResponse("http://known.fm/AC%2FDC/", body=body, encoding="utf8")
        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(
                url="http://known.fm/AC%2FDC/?page=2",
                text="BinB",
                fragment="",
                nofollow=False,
            ),
        ]

    def test_encoded_url_in_restricted_xpath(self):
        body = b"""<html><body><div><a href="?page=2">BinB</a></body></html>"""
        response = HtmlResponse("http://known.fm/AC%2FDC/", body=body, encoding="utf8")
        lx = LxmlLinkExtractor(restrict_xpaths="//div")
        assert lx.extract_links(response) == [
            Link(
                url="http://known.fm/AC%2FDC/?page=2",
                text="BinB",
                fragment="",
                nofollow=False,
            ),
        ]

    def test_ignored_extensions(self):
        # jpg is ignored by default
        html = b"""<a href="page.html">asd</a> and <a href="photo.jpg">"""
        response = HtmlResponse("http://example.org/", body=html)
        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(url="http://example.org/page.html", text="asd"),
        ]

        # override denied extensions
        lx = LxmlLinkExtractor(deny_extensions=["html"])
        assert lx.extract_links(response) == [
            Link(url="http://example.org/photo.jpg"),
        ]

    @pytest.mark.parametrize(
        ("path", "denied"),
        [
            ("/file.tar.gz", True),
            ("/FILE.TAR.GZ", True),
            ("/file.gz", False),
            ("/file.tar.gz/page", False),
            ("/file", False),
        ],
    )
    def test_ignored_multi_part_extension(self, path: str, denied: bool) -> None:
        html = f'<a href="{path}">x</a>'.encode()
        response = HtmlResponse("http://example.org/", body=html)
        lx = LxmlLinkExtractor(deny_extensions=["tar.gz"])
        assert (not lx.extract_links(response)) is denied

    def test_process_value(self):
        """Test restrict_xpaths with encodings"""
        html = b"""
<a href="javascript:goToPage('../other/page.html','photo','width=600,height=540,scrollbars'); return false">Text</a>
<a href="/about.html">About us</a>
        """
        response = HtmlResponse(
            "http://example.org/somepage/index.html",
            body=html,
            encoding="windows-1252",
        )

        def process_value(value):
            m = re.search(r"javascript:goToPage\('(.*?)'", value)
            return m.group(1) if m else None

        lx = LxmlLinkExtractor(process_value=process_value)
        assert lx.extract_links(response) == [
            Link(url="http://example.org/other/page.html", text="Text")
        ]

    def test_base_url_with_restrict_xpaths(self):
        html = b"""<html><head><title>Page title</title><base href="http://otherdomain.com/base/" /></head>
        <body><p><a href="item/12.html">Item 12</a></p>
        </body></html>"""
        response = HtmlResponse("http://example.org/somepage/index.html", body=html)
        lx = LxmlLinkExtractor(restrict_xpaths="//p")
        assert lx.extract_links(response) == [
            Link(url="http://otherdomain.com/base/item/12.html", text="Item 12")
        ]

    def test_attrs(self):
        lx = LxmlLinkExtractor(attrs="href")
        page4_url = "http://example.com/page%204.html"

        assert lx.extract_links(self.response) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html#foo",
                text="sample 3 repetition with fragment",
            ),
            Link(url="http://www.google.com/something", text=""),
            Link(url="http://example.com/innertag.html", text="inner tag"),
            Link(url=page4_url, text="href with whitespaces"),
        ]

        lx = LxmlLinkExtractor(
            attrs=("href", "src"), tags=("a", "area", "img"), deny_extensions=()
        )
        assert lx.extract_links(self.response) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample2.jpg", text=""),
            Link(url="http://example.com/sample3.html", text="sample 3 text"),
            Link(
                url="http://example.com/sample3.html#foo",
                text="sample 3 repetition with fragment",
            ),
            Link(url="http://www.google.com/something", text=""),
            Link(url="http://example.com/innertag.html", text="inner tag"),
            Link(url=page4_url, text="href with whitespaces"),
        ]

        lx = LxmlLinkExtractor(attrs=None)
        assert lx.extract_links(self.response) == []

    def test_tags(self):
        html = (
            b'<html><area href="sample1.html"></area>'
            b'<a href="sample2.html">sample 2</a><img src="sample2.jpg"/></html>'
        )
        response = HtmlResponse("http://example.com/index.html", body=html)

        lx = LxmlLinkExtractor(tags=None)
        assert lx.extract_links(response) == []

        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(url="http://example.com/sample1.html", text=""),
            Link(url="http://example.com/sample2.html", text="sample 2"),
        ]

        lx = LxmlLinkExtractor(tags="area")
        assert lx.extract_links(response) == [
            Link(url="http://example.com/sample1.html", text=""),
        ]

        lx = LxmlLinkExtractor(tags="a")
        assert lx.extract_links(response) == [
            Link(url="http://example.com/sample2.html", text="sample 2"),
        ]

        lx = LxmlLinkExtractor(
            tags=("a", "img"), attrs=("href", "src"), deny_extensions=()
        )
        assert lx.extract_links(response) == [
            Link(url="http://example.com/sample2.html", text="sample 2"),
            Link(url="http://example.com/sample2.jpg", text=""),
        ]

    def test_tags_attrs(self):
        html = b"""
        <html><body>
        <div id="item1" data-url="get?id=1"><a href="#">Item 1</a></div>
        <div id="item2" data-url="get?id=2"><a href="#">Item 2</a></div>
        </body></html>
        """
        response = HtmlResponse("http://example.com/index.html", body=html)

        lx = LxmlLinkExtractor(tags="div", attrs="data-url")
        assert lx.extract_links(response) == [
            Link(
                url="http://example.com/get?id=1",
                text="Item 1",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://example.com/get?id=2",
                text="Item 2",
                fragment="",
                nofollow=False,
            ),
        ]

        lx = LxmlLinkExtractor(tags=("div",), attrs=("data-url",))
        assert lx.extract_links(response) == [
            Link(
                url="http://example.com/get?id=1",
                text="Item 1",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://example.com/get?id=2",
                text="Item 2",
                fragment="",
                nofollow=False,
            ),
        ]

    def test_tags_attrs_wildcard_and_deny(self):
        html = b"""
        <html><body>
        <a href="a.html">a</a>
        <div data-url="div.html">div</div>
        <span href="span.html">span</span>
        <p data-url="p.html">p</p>
        </body></html>
        """
        response = HtmlResponse("http://example.com/index.html", body=html)

        def urls(**kwargs: Any) -> list[str]:
            lx = LxmlLinkExtractor(**kwargs)
            return [link.url for link in lx.extract_links(response)]

        # Default behavior is unchanged: only the listed tags and attributes.
        assert urls() == ["http://example.com/a.html"]

        # "*" as a tag matches every tag.
        assert urls(tags="*") == [
            "http://example.com/a.html",
            "http://example.com/span.html",
        ]

        # "*" as an attribute matches every attribute.
        assert urls(tags="*", attrs="*") == [
            "http://example.com/a.html",
            "http://example.com/div.html",
            "http://example.com/span.html",
            "http://example.com/p.html",
        ]

        # deny_tags excludes tags from the wildcard.
        assert urls(tags="*", attrs="*", deny_tags="div") == [
            "http://example.com/a.html",
            "http://example.com/span.html",
            "http://example.com/p.html",
        ]

        # deny_attrs excludes attributes from the wildcard.
        assert urls(tags="*", attrs="*", deny_attrs="data-url") == [
            "http://example.com/a.html",
            "http://example.com/span.html",
        ]

        # deny_tags also applies when tags are listed explicitly.
        assert urls(tags=("a", "span"), attrs="*", deny_tags="span") == [
            "http://example.com/a.html",
        ]

        # The wildcard for one parameter is independent of the other.
        assert urls(tags="a", attrs="*") == ["http://example.com/a.html"]

    def test_xhtml(self):
        xhtml = b"""
<?xml version="1.0"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN"
    "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
<head>
    <title>XHTML document title</title>
</head>
<body>
    <div class='links'>
    <p><a href="/about.html">About us</a></p>
    </div>
    <div>
    <p><a href="/follow.html">Follow this link</a></p>
    </div>
    <div>
    <p><a href="/nofollow.html" rel="nofollow">Dont follow this one</a></p>
    </div>
    <div>
    <p><a href="/nofollow2.html" rel="blah">Choose to follow or not</a></p>
    </div>
    <div>
    <p><a href="http://google.com/something" rel="external nofollow">External link not to follow</a></p>
    </div>
</body>
</html>
        """

        response: TextResponse = HtmlResponse(
            "http://example.com/index.xhtml", body=xhtml
        )

        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(
                url="http://example.com/about.html",
                text="About us",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://example.com/follow.html",
                text="Follow this link",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://example.com/nofollow.html",
                text="Dont follow this one",
                fragment="",
                nofollow=True,
            ),
            Link(
                url="http://example.com/nofollow2.html",
                text="Choose to follow or not",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://google.com/something",
                text="External link not to follow",
                nofollow=True,
            ),
        ]

        response = XmlResponse("http://example.com/index.xhtml", body=xhtml)

        lx = LxmlLinkExtractor()
        assert lx.extract_links(response) == [
            Link(
                url="http://example.com/about.html",
                text="About us",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://example.com/follow.html",
                text="Follow this link",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://example.com/nofollow.html",
                text="Dont follow this one",
                fragment="",
                nofollow=True,
            ),
            Link(
                url="http://example.com/nofollow2.html",
                text="Choose to follow or not",
                fragment="",
                nofollow=False,
            ),
            Link(
                url="http://google.com/something",
                text="External link not to follow",
                nofollow=True,
            ),
        ]

    def test_link_wrong_href(self):
        html = b"""
        <a href="http://example.org/item1.html">Item 1</a>
        <a href="http://[example.org/item2.html">Item 2</a>
        <a href="http://example.org/item3.html">Item 3</a>
        """
        response = HtmlResponse("http://example.org/index.html", body=html)
        lx = LxmlLinkExtractor()
        assert list(lx.extract_links(response)) == [
            Link(
                url="http://example.org/item1.html",
                text="Item 1",
                nofollow=False,
            ),
            Link(
                url="http://example.org/item3.html",
                text="Item 3",
                nofollow=False,
            ),
        ]

    @pytest.mark.parametrize(
        ("response_url", "extra_urls"),
        [
            ("http://example.com/index.html", []),
            ("https://example.com/index.html", []),
            ("ftp://example.com/index.html", ["ftp://example.com/d"]),
            ("file:///tmp/index.html", ["file:///etc/passwd"]),
        ],
    )
    def test_default_schemes(self, response_url: str, extra_urls: list[str]) -> None:
        body = b"""
        <html><body>
        <a href="http://example.com/a">a</a>
        <a href="https://example.com/b">b</a>
        <a href="file:///etc/passwd">c</a>
        <a href="ftp://example.com/d">d</a>
        <a href="s3://bucket/e">e</a>
        <a href="data:text/plain,f">f</a>
        </body></html>"""
        response = HtmlResponse(response_url, body=body)
        assert [link.url for link in LxmlLinkExtractor().extract_links(response)] == [
            "http://example.com/a",
            "https://example.com/b",
            *extra_urls,
        ]

    def test_schemes(self):
        body = b"""
        <html><body>
        <a href="page.html">Page</a>
        <a href="data:text/plain,Data">Data</a>
        <a href="mailto:someone@example.com">Mail</a>
        </body></html>"""
        response = HtmlResponse("http://www.example.com/index.html", body=body)

        lx = LxmlLinkExtractor(schemes="mailto")
        assert lx.extract_links(response) == [
            Link(url="mailto:someone@example.com", text="Mail"),
        ]

        lx = LxmlLinkExtractor(schemes=())
        assert lx.extract_links(response) == [
            Link(url="http://www.example.com/page.html", text="Page"),
            Link(url="data:text/plain,Data", text="Data"),
            Link(url="mailto:someone@example.com", text="Mail"),
        ]

    def test_pickle_extractor(self):
        lx = LxmlLinkExtractor()
        assert isinstance(pickle.loads(pickle.dumps(lx)), LxmlLinkExtractor)

    def test_link_extractor_aggregation(self):
        """When a parameter like restrict_css is used, the underlying
        implementation calls its internal link extractor once per selector
        matching the specified restrictions, and then aggregates the
        extracted links.

        Test that aggregation respects the unique and canonicalize
        parameters.
        """
        # unique=True (default), canonicalize=False (default)
        lx = LxmlLinkExtractor(restrict_css=("div",))
        response = HtmlResponse(
            "https://example.com",
            body=b"""
                <div>
                    <a href="/a">a1</a>
                    <a href="/b?a=1&b=2">b1</a>
                </div>
                <div>
                    <a href="/a">a2</a>
                    <a href="/b?b=2&a=1">b2</a>
                </div>
            """,
        )
        actual = lx.extract_links(response)
        assert actual == [
            Link(url="https://example.com/a", text="a1"),
            Link(url="https://example.com/b?a=1&b=2", text="b1"),
            Link(url="https://example.com/b?b=2&a=1", text="b2"),
        ]

        # unique=True (default), canonicalize=True
        lx = LxmlLinkExtractor(restrict_css=("div",), canonicalize=True)
        response = HtmlResponse(
            "https://example.com",
            body=b"""
                <div>
                    <a href="/a">a1</a>
                    <a href="/b?a=1&b=2">b1</a>
                </div>
                <div>
                    <a href="/a">a2</a>
                    <a href="/b?b=2&a=1">b2</a>
                </div>
            """,
        )
        actual = lx.extract_links(response)
        assert actual == [
            Link(url="https://example.com/a", text="a1"),
            Link(url="https://example.com/b?a=1&b=2", text="b1"),
        ]

        # unique=False, canonicalize=False (default)
        lx = LxmlLinkExtractor(restrict_css=("div",), unique=False)
        response = HtmlResponse(
            "https://example.com",
            body=b"""
                <div>
                    <a href="/a">a1</a>
                    <a href="/b?a=1&b=2">b1</a>
                </div>
                <div>
                    <a href="/a">a2</a>
                    <a href="/b?b=2&a=1">b2</a>
                </div>
            """,
        )
        actual = lx.extract_links(response)
        assert actual == [
            Link(url="https://example.com/a", text="a1"),
            Link(url="https://example.com/b?a=1&b=2", text="b1"),
            Link(url="https://example.com/a", text="a2"),
            Link(url="https://example.com/b?b=2&a=1", text="b2"),
        ]

        # unique=False, canonicalize=True
        lx = LxmlLinkExtractor(restrict_css=("div",), unique=False, canonicalize=True)
        response = HtmlResponse(
            "https://example.com",
            body=b"""
                <div>
                    <a href="/a">a1</a>
                    <a href="/b?a=1&b=2">b1</a>
                </div>
                <div>
                    <a href="/a">a2</a>
                    <a href="/b?b=2&a=1">b2</a>
                </div>
            """,
        )
        actual = lx.extract_links(response)
        assert actual == [
            Link(url="https://example.com/a", text="a1"),
            Link(url="https://example.com/b?a=1&b=2", text="b1"),
            Link(url="https://example.com/a", text="a2"),
            Link(url="https://example.com/b?a=1&b=2", text="b2"),
        ]

    def test_canonicalize_once_per_link(self, monkeypatch):
        calls = []

        def counting_canonicalize_url(url, *args, **kwargs):
            calls.append(url)
            return canonicalize_url(url, *args, **kwargs)

        monkeypatch.setattr(lxmlhtml, "canonicalize_url", counting_canonicalize_url)
        response = HtmlResponse(
            "https://example.com",
            body=b"".join(b'<a href="/p?b=2&a=1#f%d">x</a>' % i for i in range(10)),
        )
        lx = LxmlLinkExtractor(canonicalize=True)
        assert lx.extract_links(response) == [
            Link(url="https://example.com/p?a=1&b=2", text="x")
        ]
        assert len(calls) == 10

    def test_link_restrict_text(self):
        html = b"""
        <a href="http://example.org/item1.html">Pic of a cat</a>
        <a href="http://example.org/item2.html">Pic of a dog</a>
        <a href="http://example.org/item3.html">Pic of a cow</a>
        """
        response = HtmlResponse("http://example.org/index.html", body=html)
        # Simple text inclusion test
        lx = LxmlLinkExtractor(restrict_text="dog")
        assert list(lx.extract_links(response)) == [
            Link(
                url="http://example.org/item2.html",
                text="Pic of a dog",
                nofollow=False,
            ),
        ]
        # Unique regex test
        lx = LxmlLinkExtractor(restrict_text=r"of.*dog")
        assert list(lx.extract_links(response)) == [
            Link(
                url="http://example.org/item2.html",
                text="Pic of a dog",
                nofollow=False,
            ),
        ]
        # Multiple regex test
        lx = LxmlLinkExtractor(restrict_text=[r"of.*dog", r"of.*cat"])
        assert list(lx.extract_links(response)) == [
            Link(
                url="http://example.org/item1.html",
                text="Pic of a cat",
                nofollow=False,
            ),
            Link(
                url="http://example.org/item2.html",
                text="Pic of a dog",
                nofollow=False,
            ),
        ]

    def test_skip_bad_links(self):
        html = b"""
        <a href="http://example.org:non-port">Why would you do this?</a>
        <a href="http://example.org/item2.html">Good Link</a>
        <a href="http://example.org/item3.html">Good Link 2</a>
        """
        response = HtmlResponse("http://example.org/index.html", body=html)
        lx = LxmlLinkExtractor()
        assert list(lx.extract_links(response)) == [
            Link(
                url="http://example.org/item2.html",
                text="Good Link",
                nofollow=False,
            ),
            Link(
                url="http://example.org/item3.html",
                text="Good Link 2",
                nofollow=False,
            ),
        ]

    def test_link_allowed_is_false_with_empty_url(self):
        bad_link = Link("")
        assert not LxmlLinkExtractor()._link_allowed(bad_link, {"http", "https"})

    def test_link_allowed_is_false_with_bad_url_prefix(self):
        bad_link = Link("htp://should_be_http.example")
        assert not LxmlLinkExtractor()._link_allowed(bad_link, {"http", "https"})

    def test_link_allowed_is_false_with_missing_url_prefix(self):
        bad_link = Link("should_have_prefix.example")
        assert not LxmlLinkExtractor()._link_allowed(bad_link, {"http", "https"})


class TestLxmlParserLinkExtractor:
    def test_extract_links(self):
        html = b'<a href="http://example.com/page.html">Link</a>'
        response = HtmlResponse("http://example.com/", body=html)
        lx = LxmlParserLinkExtractor()
        assert lx.extract_links(response) == [
            Link(url="http://example.com/page.html", text="Link", nofollow=False),
        ]

    def test_deduplicates_by_canonical_url(self):
        # With canonicalized=False, the default, URLs are canonicalized to
        # decide whether two extracted links are the same one.
        html = (
            b'<a href="http://example.com/page.html?b=2&amp;a=1">1</a>'
            b'<a href="http://example.com/page.html?a=1&amp;b=2">2</a>'
        )
        response = HtmlResponse("http://example.com/", body=html)
        lx = LxmlParserLinkExtractor(unique=True)
        assert lx.extract_links(response) == [
            Link(url="http://example.com/page.html?b=2&a=1", text="1", nofollow=False),
        ]

    def test_strip_false(self):
        # With strip=False, trailing whitespace on a relative href survives urljoin
        # and is visible to process_value (safe_url_string cleans it up afterward).
        # Here process_value rejects URLs that still carry trailing whitespace,
        # demonstrating the difference from strip=True.
        def reject_trailing_whitespace(url):
            return None if url != url.rstrip() else url

        html = b'<a href="page.html   ">Link</a>'
        response = HtmlResponse("http://example.com/", body=html)

        lx_strip = LxmlParserLinkExtractor(
            strip=True, process=reject_trailing_whitespace
        )
        assert lx_strip.extract_links(response) == [
            Link(url="http://example.com/page.html", text="Link", nofollow=False),
        ]

        lx_no_strip = LxmlParserLinkExtractor(
            strip=False, process=reject_trailing_whitespace
        )
        assert lx_no_strip.extract_links(response) == []


class TestParseSrcset:
    """Unit tests for the srcset tokenizer and descriptor validation."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # Bare URL, no descriptors.
            ("/a.png", [("/a.png", "")]),
            # Width and density descriptors.
            ("/a.png 480w", [("/a.png", "480w")]),
            ("/a.png 2x", [("/a.png", "2x")]),
            ("/a.png 1.5x", [("/a.png", "1.5x")]),
            ("/a.png .5x", [("/a.png", ".5x")]),
            # Several candidates separated by comma plus whitespace.
            (
                "/small.png 480w, /large.png 800w",
                [("/small.png", "480w"), ("/large.png", "800w")],
            ),
            # A data URL keeps the commas embedded in its payload, including
            # when further candidates follow after the descriptor.
            (
                "data:image/gif;base64,R0lGOD,l=q,AA== 1x, /b.png 2x",
                [
                    ("data:image/gif;base64,R0lGOD,l=q,AA==", "1x"),
                    ("/b.png", "2x"),
                ],
            ),
            (
                "data:text/plain,hello,world",
                [("data:text/plain,hello,world", "")],
            ),
            # All ASCII whitespace kinds separate tokens and candidates.
            (
                "/a.png\t1x\n, /b.png\f2x\r",
                [("/a.png", "1x"), ("/b.png", "2x")],
            ),
            # Repeated whitespace around the URL and descriptor is padding.
            (
                "/a.png    480w   ,   /b.png  800w",
                [("/a.png", "480w"), ("/b.png", "800w")],
            ),
            # Multibyte characters in URLs are preserved character-wise.
            (
                "/café.png 1x, /naïve.png 2x",
                [("/café.png", "1x"), ("/naïve.png", "2x")],
            ),
            # A comma without preceding whitespace is part of the URL, even
            # when it looks like the plain single-URL separator.
            ("/a.png,/b.png 2x", [("/a.png,/b.png", "2x")]),
            # Trailing comma and whitespace do not create empty candidates.
            ("/a.png 1x,", [("/a.png", "1x")]),
            ("/a.png 1x, ", [("/a.png", "1x")]),
            # Empty or whitespace-only attributes yield no candidate.
            ("", []),
            ("   ", []),
        ],
    )
    def test_valid_values(self, value: str, expected: list[tuple[str, str]]) -> None:
        assert _parse(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "/a.png 0w",  # zero width is not a valid non-negative integer here
            "/a.png 00w",
            "/a.png -1x",  # densities cannot be negative
            "/a.png 100w 2x",  # two descriptors on one candidate
            "/a.png 480w 800w",
            "/a.png foo",  # unknown descriptor
            "/a.png 2X",  # descriptor suffix is case-sensitive
            "/a.png 1h",
        ],
    )
    def test_invalid_candidate_skipped(self, value: str) -> None:
        assert _parse(value) == []

    def test_invalid_candidate_does_not_drop_valid_ones(self) -> None:
        value = "/good.png 1x, /bad.png 0w, /also-good.png 2x"
        assert _parse(value) == [
            ("/good.png", "1x"),
            ("/also-good.png", "2x"),
        ]

    def test_duplicate_width_descriptor_is_conflict(self) -> None:
        value = "/a.png 480w, /b.png 480w"
        assert _parse(value) == [("/a.png", "480w")]

    def test_duplicate_density_descriptor_is_conflict(self) -> None:
        value = "/a.png 1x, /b.png 1x"
        assert _parse(value) == [("/a.png", "1x")]

    def test_mixed_descriptor_kinds_are_conflict(self) -> None:
        # A width after a density, and the reverse order.
        assert _parse("/a.png 1x, /b.png 480w") == [("/a.png", "1x")]
        assert _parse("/a.png 480w, /b.png 1x") == [("/a.png", "480w")]

    def test_reject_mode_returns_none_on_invalid_candidate(self) -> None:
        assert _parse("/a.png 480w, /b.png 480w", on_error="reject") is None
        assert _parse("/a.png 0w", on_error="reject") is None

    def test_reject_mode_keeps_valid_attribute(self) -> None:
        assert _parse("/a.png 480w, /b.png 800w", on_error="reject") == [
            ("/a.png", "480w"),
            ("/b.png", "800w"),
        ]

    def test_skip_mode_logs_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("DEBUG", logger="scrapy.linkextractors.lxmlhtml"):
            _parse("/a.png 0w")
        assert len(caplog.records) == 1
        assert "Skipping invalid srcset candidate" in caplog.records[0].getMessage()

    def test_reject_mode_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="scrapy.linkextractors.lxmlhtml"):
            result = _parse("/a.png 0w", on_error="reject")
        assert result is None
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelname == "WARNING"
        assert "Rejecting srcset attribute" in record.getMessage()

    def test_candidate_limit_stops_parsing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        value = ", ".join(f"/i{n}.png {n + 1}x" for n in range(10))
        with caplog.at_level("WARNING", logger="scrapy.linkextractors.lxmlhtml"):
            result = _parse(value, max_candidates=3)
        assert len(result) == 3
        assert result[0] == ("/i0.png", "1x")
        assert result[-1] == ("/i2.png", "3x")
        assert any(
            "Stopping srcset parsing" in record.getMessage()
            for record in caplog.records
        )

    def test_character_budget_rejects_whole_attribute(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        value = "/a.png 1x, " + "x" * 50
        with caplog.at_level("WARNING", logger="scrapy.linkextractors.lxmlhtml"):
            assert _parse(value, max_chars=10) is None
        assert any(
            "above the limit" in record.getMessage() for record in caplog.records
        )

    def test_character_budget_boundary_is_accepted(self) -> None:
        value = "/a.png 1x"
        assert _parse(value, max_chars=len(value)) == [("/a.png", "1x")]

    def test_parsing_is_linear_on_long_comma_run(self) -> None:
        # A long run of commas inside a bare URL must stay one bounded pass.
        value = "data:text/plain," + "a," * 1000
        result = _parse(value, max_chars=100_000)
        assert result == [(value, "")]


class TestLxmlLinkExtractorSrcset:
    """Integration tests for srcset extraction through the public API."""

    def _response(self, body: bytes) -> HtmlResponse:
        return HtmlResponse("http://example.com/page/index", body=body)

    def _extractor(self, **kwargs: Any) -> LxmlLinkExtractor:
        kwargs.setdefault("tags", "img")
        kwargs.setdefault("attrs", ())
        kwargs.setdefault("srcset_attrs", "srcset")
        kwargs.setdefault("deny_extensions", ())
        return LxmlLinkExtractor(**kwargs)

    def test_disabled_by_default(self) -> None:
        response = self._response(
            b'<img src="/plain.png" srcset="/a.png 480w, /b.png 800w"/>'
        )
        # The default extractor only scans a/area href, so nothing is found.
        assert LxmlLinkExtractor().extract_links(response) == []
        # Even with img scanned, srcset is treated as an unknown attribute.
        lx = LxmlLinkExtractor(tags="img", attrs="src", deny_extensions=())
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/plain.png"
        ]

    def test_extracts_each_candidate_with_source_attr(self) -> None:
        response = self._response(b'<img srcset="/small.png 480w, /large.png 800w"/>')
        assert self._extractor().extract_links(response) == [
            Link(
                "http://example.com/small.png",
                source_attr="srcset",
            ),
            Link(
                "http://example.com/large.png",
                source_attr="srcset",
            ),
        ]

    def test_descriptor_is_not_part_of_url(self) -> None:
        response = self._response(b'<img srcset="http://cdn.example.com/a.png 480w"/>')
        link = self._extractor().extract_links(response)[0]
        assert link.url == "http://cdn.example.com/a.png"
        assert "480w" not in link.url

    def test_relative_urls_resolve_against_base_tag(self) -> None:
        response = HtmlResponse(
            "http://example.com/page/index",
            body=(
                b'<html><head><base href="http://cdn.example.com/assets/"/></head>'
                b'<body><img srcset="small.png 480w, /root.png 800w"/></body></html>'
            ),
        )
        assert [link.url for link in self._extractor().extract_links(response)] == [
            "http://cdn.example.com/assets/small.png",
            "http://cdn.example.com/root.png",
        ]

    def test_data_url_with_commas_is_one_link(self) -> None:
        response = self._response(
            b'<img srcset="data:image/gif;base64,R0lGOD,l=q,AA== 1x, /b.png 2x"/>'
        )
        lx = self._extractor(schemes=())
        assert [link.url for link in lx.extract_links(response)] == [
            "data:image/gif;base64,R0lGOD,l=q,AA==",
            "http://example.com/b.png",
        ]
        assert all(link.source_attr == "srcset" for link in lx.extract_links(response))

    def test_empty_and_whitespace_attributes(self) -> None:
        response = self._response(
            b'<img srcset=""/><img srcset="   "/><img srcset="/a.png 1x"/>'
        )
        assert [link.url for link in self._extractor().extract_links(response)] == [
            "http://example.com/a.png"
        ]

    def test_html_entities_are_decoded_before_parsing(self) -> None:
        # &amp; must be decoded to &, so the parser sees the real URL.
        response = self._response(b'<img srcset="/a.png?x=1&amp;y=2 480w"/>')
        assert self._extractor().extract_links(response) == [
            Link("http://example.com/a.png?x=1&y=2", source_attr="srcset"),
        ]

    def test_multibyte_urls(self) -> None:
        response = self._response(
            '<img srcset="/café.png 1x, /naïve.png 2x"/>'.encode()
        )
        assert [link.url for link in self._extractor().extract_links(response)] == [
            "http://example.com/caf%C3%A9.png",
            "http://example.com/na%C3%AFve.png",
        ]

    def test_skip_mode_keeps_valid_candidates(self) -> None:
        response = self._response(
            b'<img srcset="/good.png 1x, /bad.png 0w, /also.png 2x"/>'
        )
        assert [link.url for link in self._extractor().extract_links(response)] == [
            "http://example.com/good.png",
            "http://example.com/also.png",
        ]

    def test_reject_mode_drops_whole_attribute(self) -> None:
        response = self._response(b'<img srcset="/a.png 480w, /b.png 480w"/>')
        lx = self._extractor(srcset_on_error="reject")
        assert lx.extract_links(response) == []

    def test_reject_mode_keeps_valid_attribute(self) -> None:
        response = self._response(b'<img srcset="/a.png 480w, /b.png 800w"/>')
        lx = self._extractor(srcset_on_error="reject")
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/a.png",
            "http://example.com/b.png",
        ]

    def test_invalid_on_error_value_raises(self) -> None:
        with pytest.raises(ValueError, match="srcset_on_error"):
            LxmlLinkExtractor(srcset_attrs="srcset", srcset_on_error="explode")

    @pytest.mark.parametrize(
        ("limit_name", "limit_value"),
        [
            ("srcset_max_candidates", 0),
            ("srcset_max_candidates", -1),
            ("srcset_max_chars", 0),
        ],
    )
    def test_invalid_limits_raise(self, limit_name: str, limit_value: int) -> None:
        def build() -> LxmlLinkExtractor:
            if limit_name == "srcset_max_candidates":
                return LxmlLinkExtractor(
                    srcset_attrs="srcset", srcset_max_candidates=limit_value
                )
            return LxmlLinkExtractor(
                srcset_attrs="srcset", srcset_max_chars=limit_value
            )

        with pytest.raises(ValueError, match="positive integer"):
            build()

    def test_candidate_limit_is_enforced(self) -> None:
        body = ", ".join(f"/i{n}.png {n + 1}x" for n in range(10)).encode()
        response = self._response(b'<img srcset="' + body + b'"/>')
        lx = self._extractor(srcset_max_candidates=3)
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/i0.png",
            "http://example.com/i1.png",
            "http://example.com/i2.png",
        ]

    def test_character_budget_is_enforced(self) -> None:
        long_value = b"/a.png 1x, " + b"x" * 500
        response = self._response(b'<img srcset="' + long_value + b'"/>')
        lx = self._extractor(srcset_max_chars=10)
        assert lx.extract_links(response) == []

    def test_dedup_removes_repeated_candidate_url(self) -> None:
        response = self._response(b'<img srcset="/dup.png 1x, /dup.png 2x"/>')
        # unique=True (the default) collapses the repeated URL.
        assert [link.url for link in self._extractor().extract_links(response)] == [
            "http://example.com/dup.png"
        ]
        # unique=False keeps both links in document order.
        lx = self._extractor(unique=False)
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/dup.png",
            "http://example.com/dup.png",
        ]

    def test_filters_apply_to_candidates(self) -> None:
        response = self._response(b'<img srcset="/keep-a.png 1x, /drop-b.png 2x"/>')
        lx = self._extractor(allow=(r"keep-a",))
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/keep-a.png"
        ]

    def test_process_value_applies_to_candidates(self) -> None:
        response = self._response(b'<img srcset="/small.png 480w, /large.png 800w"/>')
        lx = self._extractor(
            process_value=lambda value: None if "large" in value else value
        )
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/small.png"
        ]

    def test_single_url_attrs_keep_working_alongside_srcset(self) -> None:
        response = self._response(
            b'<img src="/plain.png" srcset="/a.png 480w, /b.png 800w"/>'
        )
        lx = LxmlLinkExtractor(
            tags="img",
            attrs="src",
            srcset_attrs="srcset",
            deny_extensions=(),
        )
        assert [
            (link.url, link.source_attr) for link in lx.extract_links(response)
        ] == [
            ("http://example.com/plain.png", None),
            ("http://example.com/a.png", "srcset"),
            ("http://example.com/b.png", "srcset"),
        ]

    def test_custom_srcset_attribute_name(self) -> None:
        response = self._response(b'<link imagesrcset="/a.png 480w, /b.png 800w"/>')
        lx = LxmlLinkExtractor(
            tags="link",
            attrs=(),
            srcset_attrs="imagesrcset",
            deny_extensions=(),
        )
        assert [
            (link.url, link.source_attr) for link in lx.extract_links(response)
        ] == [
            ("http://example.com/a.png", "imagesrcset"),
            ("http://example.com/b.png", "imagesrcset"),
        ]

    def test_deny_attrs_only_governs_single_url_attrs(self) -> None:
        # srcset_attrs is a separate, explicit channel: deny_attrs filters the
        # regular single-URL attributes but never suppresses a srcset parse.
        # Listing the same attribute in both channels must not extract it twice.
        response = self._response(
            b'<img src="/plain.png" srcset="/a.png 480w, /b.png 800w"/>'
        )
        lx = LxmlLinkExtractor(
            tags="img",
            attrs="*",
            srcset_attrs="srcset",
            deny_attrs="src",
            deny_extensions=(),
        )
        assert [
            (link.url, link.source_attr) for link in lx.extract_links(response)
        ] == [
            ("http://example.com/a.png", "srcset"),
            ("http://example.com/b.png", "srcset"),
        ]

    def test_dom_and_response_are_not_modified(self) -> None:
        body = b'<img srcset="/a.png 480w, /b.png 800w"/>'
        response = self._response(body)
        original = response.css("img::attr(srcset)").get()
        self._extractor().extract_links(response)
        # The attribute value and the raw body are untouched.
        assert response.css("img::attr(srcset)").get() == original
        assert response.body == body

    def test_several_elements_preserve_document_order(self) -> None:
        response = self._response(b'<img srcset="/a.png 1x"/><img srcset="/b.png 1x"/>')
        assert [link.url for link in self._extractor().extract_links(response)] == [
            "http://example.com/a.png",
            "http://example.com/b.png",
        ]

    def test_restrict_xpaths_limits_srcset_scan(self) -> None:
        response = self._response(
            b'<div id="keep"><img srcset="/a.png 1x"/></div>'
            b'<div id="drop"><img srcset="/b.png 1x"/></div>'
        )
        lx = self._extractor(restrict_xpaths='//div[@id="keep"]')
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/a.png"
        ]
