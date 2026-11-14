from __future__ import annotations

import pickle
import re
from typing import Any

import pytest
from w3lib.url import canonicalize_url

from scrapy.http import HtmlResponse, TextResponse, XmlResponse
from scrapy.link import Link
from scrapy.linkextractors import lxmlhtml
from scrapy.linkextractors.lxmlhtml import LxmlLinkExtractor, LxmlParserLinkExtractor
from tests import get_testdata


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

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            # Case, trailing dot and the default port normalize to one origin.
            (
                ["HTTP://EXAMPLE.COM.", "http://example.com:80"],
                {"http://example.com"},
            ),
            # IDNA and non-default ports are normalized but kept distinct.
            (
                ["http://bücher.example", "http://example.com:8080"],
                {"http://xn--bcher-kva.example", "http://example.com:8080"},
            ),
            # IPv6 literals are bracketed and compressed.
            (["http://[2001:DB8::1]:8080"], {"http://[2001:db8::1]:8080"}),
            # An empty list is kept as an empty set and allows nothing.
            ([], set()),
            # A single value is accepted like a list.
            ("http://example.com", {"http://example.com"}),
        ],
    )
    def test_allowed_origins_normalized(
        self, configured: Any, expected: set[str]
    ) -> None:
        lx = LxmlLinkExtractor(allowed_origins=configured)
        assert lx.allowed_origins == expected

    @pytest.mark.parametrize(
        "origin",
        [
            "ftp://example.com",
            "http://user:pass@example.com",
            "http://example.com/",
            "http://example.com/path",
            "http://example.com?q=1",
            "http://example.com#frag",
            "http://example.com:65536",
            "http://[bad]",
        ],
    )
    def test_allowed_origins_invalid(self, origin: str) -> None:
        with pytest.raises(ValueError, match="Invalid origin|Invalid host"):
            LxmlLinkExtractor(allowed_origins=[origin])

    def test_allowed_origins_filters_links(self) -> None:
        html = b"""<html><body>
        <a href="http://example.com/same">same host</a>
        <a href="https://example.com/cross-scheme">cross scheme</a>
        <a href="http://example.com:8080/port">non-default port</a>
        <a href="http://example.com:80/default-port">explicit default port</a>
        <a href="http://notexample.com/similar">similar host</a>
        <a href="http://other.example.org/other">other host</a>
        <a href="ftp://example.com/ftp">ftp scheme</a>
        </body></html>"""
        response = HtmlResponse("https://response.example/page", body=html)
        lx = LxmlLinkExtractor(allowed_origins=["http://example.com"])
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/same",
            "http://example.com:80/default-port",
        ]

    def test_allowed_origins_multiple(self) -> None:
        html = b"""<html><body>
        <a href="http://example.com/a">a</a>
        <a href="https://secure.example.org/b">b</a>
        <a href="http://[2001:db8::1]:9000/c">c</a>
        <a href="http://else.example.net/d">d</a>
        </body></html>"""
        response = HtmlResponse("http://response.example/", body=html)
        lx = LxmlLinkExtractor(
            allowed_origins=[
                "http://example.com",
                "https://secure.example.org",
                "http://[2001:db8::1]:9000",
            ]
        )
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/a",
            "https://secure.example.org/b",
            "http://[2001:db8::1]:9000/c",
        ]

    def test_allowed_origins_empty_allows_nothing(self) -> None:
        html = b'<a href="http://example.com/a">a</a><a href="/b">b</a>'
        response = HtmlResponse("http://example.com/index.html", body=html)
        assert LxmlLinkExtractor(allowed_origins=[]).extract_links(response) == []

    def test_allowed_origins_uses_base_tag_for_relative_links(self) -> None:
        # Relative hrefs are resolved against the <base> tag, not the response
        # URL, and the origin of the resolved URL decides.
        html = b"""<html><head>
        <base href="http://base.example:8080/root/" />
        </head><body>
        <a href="page.html">base relative</a>
        <a href="/absolute.html">base absolute</a>
        <a href="http://example.com/external">external</a>
        </body></html>"""
        response = HtmlResponse("https://response.example/index", body=html)

        lx = LxmlLinkExtractor(
            allowed_origins=["http://base.example:8080", "http://example.com"]
        )
        assert [link.url for link in lx.extract_links(response)] == [
            "http://base.example:8080/root/page.html",
            "http://base.example:8080/absolute.html",
            "http://example.com/external",
        ]

        other = LxmlLinkExtractor(allowed_origins=["https://response.example"])
        assert other.extract_links(response) == []

    def test_allowed_origins_without_base_tag_uses_response_url(self) -> None:
        html = b'<a href="page.html">relative</a><a href="/root/x">absolute</a>'
        response = HtmlResponse("https://response.example:8443/index", body=html)
        lx = LxmlLinkExtractor(allowed_origins=["https://response.example:8443"])
        assert [link.url for link in lx.extract_links(response)] == [
            "https://response.example:8443/page.html",
            "https://response.example:8443/root/x",
        ]

    def test_allowed_origins_keeps_text_nofollow_fragment(self) -> None:
        html = b"""<html><body>
        <a href="/a#section" rel="nofollow">followed text</a>
        <a href="http://example.com/b">plain</a>
        </body></html>"""
        response = HtmlResponse("http://example.com/index", body=html)
        lx = LxmlLinkExtractor(allowed_origins=["http://example.com"])
        assert lx.extract_links(response) == [
            Link(
                url="http://example.com/a#section",
                text="followed text",
                nofollow=True,
            ),
            Link(url="http://example.com/b", text="plain"),
        ]

    def test_allowed_origins_with_canonicalize(self) -> None:
        html = b'<a href="/p?b=2&amp;a=1">x</a><a href="http://other.example/p">y</a>'
        response = HtmlResponse("http://example.com/", body=html)
        lx = LxmlLinkExtractor(
            allowed_origins=["http://example.com"], canonicalize=True
        )
        assert lx.extract_links(response) == [
            Link(url="http://example.com/p?a=1&b=2", text="x")
        ]

    def test_allowed_origins_combines_with_other_filters(self) -> None:
        html = b"""<html><body>
        <a href="http://example.com/keep.html">keep</a>
        <a href="http://example.com/skip.jpg">denied extension</a>
        <a href="http://example.com/deny.html">denied regex</a>
        <a href="http://other.example/keep.html">other origin</a>
        </body></html>"""
        response = HtmlResponse("http://response.example/", body=html)
        lx = LxmlLinkExtractor(
            allowed_origins=["http://example.com"], deny=("deny",)
        )
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/keep.html"
        ]

    def test_allowed_origins_with_restrict_selectors(self) -> None:
        html = b"""<html><body>
        <div id="in"><a href="http://example.com/in">in</a></div>
        <div id="out"><a href="http://example.com/out">out</a></div>
        </body></html>"""
        response = HtmlResponse("http://response.example/", body=html)
        lx = LxmlLinkExtractor(
            allowed_origins=["http://example.com"],
            restrict_xpaths='//div[@id="in"]',
        )
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/in"
        ]

    def test_allowed_origins_idna_and_ipv6(self) -> None:
        html = b"""<html><head>
        <base href="http://xn--bcher-kva.example/" />
        </head><body>
        <a href="/idn">IDNA host through the base tag</a>
        <a href="http://[2001:DB8::1]/v6">IPv6 host</a>
        <a href="http://[2001:db8::1]:80/default">IPv6 default port</a>
        </body></html>"""
        response = HtmlResponse("http://response.example/", body=html)
        lx = LxmlLinkExtractor(
            allowed_origins=[
                "http://bücher.example",
                "http://[2001:db8::1]",
            ]
        )
        assert [link.url for link in lx.extract_links(response)] == [
            "http://xn--bcher-kva.example/idn",
            "http://[2001:db8::1]/v6",
            "http://[2001:db8::1]:80/default",
        ]

    def test_allowed_origins_with_process_value_none(self) -> None:
        def drop_first(value: str) -> str | None:
            return None if value.endswith("drop.html") else value

        html = b"""<html><body>
        <a href="http://example.com/drop.html">dropped</a>
        <a href="http://example.com/keep.html">kept</a>
        </body></html>"""
        response = HtmlResponse("http://response.example/", body=html)
        lx = LxmlLinkExtractor(
            allowed_origins=["http://example.com"], process_value=drop_first
        )
        assert [link.url for link in lx.extract_links(response)] == [
            "http://example.com/keep.html"
        ]

    def test_allowed_origins_repeated_calls_are_independent(self) -> None:
        first = HtmlResponse(
            "http://response.example/",
            body=b'<a href="http://example.com/a">a</a>',
        )
        second = HtmlResponse(
            "http://response.example/",
            body=b'<a href="http://example.com/b">b</a>',
        )
        lx = LxmlLinkExtractor(allowed_origins=["http://example.com"])
        assert [link.url for link in lx.extract_links(first)] == [
            "http://example.com/a"
        ]
        # The deduplication state of the first call must not leak into this one.
        assert [link.url for link in lx.extract_links(second)] == [
            "http://example.com/b"
        ]

    def test_allowed_origins_does_not_mutate_inputs(self) -> None:
        html = b'<a href="http://example.com/a">a</a>'
        response = HtmlResponse("http://response.example/", body=html)
        configured = ["http://example.com"]
        lx = LxmlLinkExtractor(allowed_origins=configured)
        lx.extract_links(response)
        assert configured == ["http://example.com"]
        assert response.url == "http://response.example/"

    def test_allowed_origins_default_disables_origin_filtering(self) -> None:
        html = b"""<html><body>
        <a href="http://example.com/a">a</a>
        <a href="https://other.example.org/b">b</a>
        <a href="/relative">c</a>
        </body></html>"""
        response = HtmlResponse("http://response.example/", body=html)
        lx = LxmlLinkExtractor()
        assert lx.allowed_origins is None
        assert len(lx.extract_links(response)) == 3


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
