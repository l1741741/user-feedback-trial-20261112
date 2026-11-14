from __future__ import annotations

import gzip
import re
from datetime import UTC, datetime
from io import BytesIO
from logging import WARNING
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from scrapy import signals
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import HtmlResponse, Request, Response, TextResponse, XmlResponse
from scrapy.spiders import SitemapSpider
from scrapy.spiders.sitemap import iterloc
from scrapy.utils.test import get_crawler
from tests import tests_datadir
from tests.spiders import RawResponseSpider
from tests.utils.bases.spider import TestSpiderBase
from tests.utils.crawl import crawl_items
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from tests.mockserver.http import MockServer


class RawSitemapSpider(RawResponseSpider):
    """Feeds :meth:`raw_body` to :class:`~scrapy.spiders.SitemapSpider` as a
    sitemap, so that it is fetched and followed through a regular crawl.

    Subclasses build the document in :meth:`raw_body`, typically using
    :attr:`mockserver` to point ``<loc>`` entries at real endpoints.
    """

    content_type = "application/xml"

    async def start(self):
        self.sitemap_urls = [self.raw_url]
        async for request in super().start():
            yield request


class TestSitemapSpider(TestSpiderBase):
    spider_class = SitemapSpider

    BODY = b"SITEMAP"
    f = BytesIO()
    g = gzip.GzipFile(fileobj=f, mode="w+b")
    g.write(BODY)
    g.close()
    GZBODY = f.getvalue()

    def assertSitemapBody(self, response: Response, body: bytes | None) -> None:
        crawler = get_crawler()
        spider = self.spider_class.from_crawler(crawler, "example.com")
        assert spider._get_sitemap_body(response) == body

    def test_get_sitemap_body(self):
        r: Response = XmlResponse(url="http://www.example.com/", body=self.BODY)
        self.assertSitemapBody(r, self.BODY)

        r = HtmlResponse(url="http://www.example.com/", body=self.BODY)
        self.assertSitemapBody(r, None)

        r = Response(url="http://www.example.com/favicon.ico", body=self.BODY)
        self.assertSitemapBody(r, None)

        r = XmlResponse(url="http://www.example.com/", body=b"")
        self.assertSitemapBody(r, b"")

    def test_get_sitemap_body_xml_content_type(self):
        r = TextResponse(
            url="http://www.example.com/sitemap",
            body=self.BODY,
            headers={"Content-Type": "application/xml"},
        )
        self.assertSitemapBody(r, self.BODY)

        r = TextResponse(
            url="http://www.example.com/sitemap",
            body=self.BODY,
            headers={"Content-Type": "text/html"},
        )
        self.assertSitemapBody(r, None)

    def test_get_sitemap_body_xml_body(self):
        body = b'<?xml version="1.0" encoding="UTF-8"?><urlset></urlset>'
        r = TextResponse(url="http://www.example.com/sitemap", body=body)
        self.assertSitemapBody(r, body)

    def test_get_sitemap_body_xml_file_url(self):
        r = Response(url="file:///tmp/sitemap.rss", body=self.BODY)
        self.assertSitemapBody(r, self.BODY)

    def test_get_sitemap_body_gzip_headers(self):
        r = Response(
            url="http://www.example.com/sitemap",
            body=self.GZBODY,
            headers={"content-type": "application/gzip"},
            request=Request("http://www.example.com/sitemap"),
        )
        self.assertSitemapBody(r, self.BODY)

    def test_get_sitemap_body_xml_url(self):
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=self.BODY)
        self.assertSitemapBody(r, self.BODY)

    def test_get_sitemap_body_xml_url_compressed(self):
        r = Response(
            url="http://www.example.com/sitemap.xml.gz",
            body=self.GZBODY,
            request=Request("http://www.example.com/sitemap"),
        )
        self.assertSitemapBody(r, self.BODY)

        # .xml.gz but body decoded by HttpCompression middleware already
        r = Response(url="http://www.example.com/sitemap.xml.gz", body=self.BODY)
        self.assertSitemapBody(r, self.BODY)

    def test_get_sitemap_urls_from_robotstxt(self):
        robots = b"""# Sitemap files
Sitemap: http://example.com/sitemap.xml
Sitemap: http://example.com/sitemap-product-index.xml
Sitemap: HTTP://example.com/sitemap-uppercase.xml
Sitemap: /sitemap-relative-url.xml
"""

        r = TextResponse(url="http://www.example.com/robots.txt", body=robots)
        spider = self.spider_class("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://example.com/sitemap.xml",
            "http://example.com/sitemap-product-index.xml",
            "http://example.com/sitemap-uppercase.xml",
            "http://www.example.com/sitemap-relative-url.xml",
        ]

    def test_get_sitemap_urls_from_robotstxt_invalid_utf8_urls(self):
        robots = (
            b"User-agent: *\n"
            b"Sitemap: http://example.com/\xff.xml\n"
            b"Sitemap: http://example.com/ok.xml\n"
        )

        r = TextResponse(url="http://www.example.com/robots.txt", body=robots)
        spider = self.spider_class("example.com")

        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://example.com/.xml",
            "http://example.com/ok.xml",
        ]

    def test_get_sitemap_urls_from_robots_parsed_signal(self):
        crawler = get_crawler(self.spider_class, settings_dict={"ROBOTSTXT_OBEY": True})
        spider = self.spider_class.from_crawler(crawler, "example.com")

        class FakeRobotParser:
            def sitemaps(self):
                return [
                    "http://example.com/sitemap.xml",  # already found below
                    "/sitemap-from-parser.xml",  # only reported through the signal
                ]

        crawler.signals.send_catch_log(
            signal=signals.robots_parsed,
            robotparser=FakeRobotParser(),
            request=Request("http://www.example.com/robots.txt"),
            response=TextResponse("http://www.example.com/robots.txt"),
        )

        robots = b"Sitemap: http://example.com/sitemap.xml\n"
        r = TextResponse(url="http://www.example.com/robots.txt", body=robots)
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://example.com/sitemap.xml",
            "http://www.example.com/sitemap-from-parser.xml",
        ]

    def test_robots_parsed_signal_not_connected_without_robotstxt_obey(self):
        crawler = get_crawler(self.spider_class)
        spider = self.spider_class.from_crawler(crawler, "example.com")

        crawler.signals.send_catch_log(
            signal=signals.robots_parsed,
            robotparser=None,
            request=Request("http://www.example.com/robots.txt"),
            response=TextResponse("http://www.example.com/robots.txt"),
        )

        assert spider._robots_sitemaps == {}

    def test_robots_parsed_signal_schedules_sitemaps_for_skipped_netloc(self):
        calls = []

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            def sitemap_request(self, loc, callback, entry, *, source, response):
                calls.append((loc, entry, source, response))
                return super().sitemap_request(
                    loc, callback, entry, source=source, response=response
                )

        crawler = get_crawler(_Spider, settings_dict={"ROBOTSTXT_OBEY": True})
        spider = _Spider.from_crawler(crawler, "example.com")
        crawler.engine = mock.MagicMock()
        spider._signal_only_netlocs.add("www.example.com")

        class FakeRobotParser:
            def sitemaps(self):
                return ["/sitemap.xml"]

        robots_response = TextResponse("http://www.example.com/robots.txt")
        # request is whatever other request to that host triggered the fetch,
        # not necessarily a request for robots.txt itself.
        crawler.signals.send_catch_log(
            signal=signals.robots_parsed,
            robotparser=FakeRobotParser(),
            request=Request("http://www.example.com/some/page"),
            response=robots_response,
        )

        crawler.engine.crawl.assert_called_once()
        scheduled = crawler.engine.crawl.call_args[0][0]
        assert scheduled.url == "http://www.example.com/sitemap.xml"
        assert scheduled.callback == spider._parse_sitemap  # pylint: disable=comparison-with-callable
        url = "http://www.example.com/sitemap.xml"
        assert calls == [(url, {"loc": url}, "robotstxt", robots_response)]
        assert spider._signal_only_netlocs == set()
        assert spider._robots_sitemaps == {}

    def test_sitemap_follow_robots_parsed_signal(self):
        class _FollowSpider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_follow = [r"1\.xml"]

        crawler = get_crawler(_FollowSpider, settings_dict={"ROBOTSTXT_OBEY": True})
        spider = _FollowSpider.from_crawler(crawler, "example.com")
        crawler.engine = mock.MagicMock()
        spider._signal_only_netlocs.add("www.example.com")

        class FakeRobotParser:
            def sitemaps(self):
                return ["/sitemap1.xml", "/sitemap2.xml"]

        crawler.signals.send_catch_log(
            signal=signals.robots_parsed,
            robotparser=FakeRobotParser(),
            request=Request("http://www.example.com/some/page"),
            response=TextResponse("http://www.example.com/robots.txt"),
        )

        crawler.engine.crawl.assert_called_once()
        scheduled = crawler.engine.crawl.call_args[0][0]
        assert scheduled.url == "http://www.example.com/sitemap1.xml"

    def test_alternate_url_locs(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/english/</loc>
            <xhtml:link rel="canonical"
                href="http://www.example.com/canonical/"/>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/deutsch/"/>
            <xhtml:link rel="alternate" hreflang="de-ch"
                href="http://www.example.com/schweiz-deutsch/"/>
            <xhtml:link rel="alternate" hreflang="it"
                href="http://www.example.com/italiano/"/>
            <xhtml:link rel="alternate" hreflang="it"/><!-- wrong tag without href -->
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)
        spider = self.spider_class("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/english/"
        ]

        spider.sitemap_alternate_links = True
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/english/",
            "http://www.example.com/deutsch/",
            "http://www.example.com/schweiz-deutsch/",
            "http://www.example.com/italiano/",
        ]

    def test_relative_and_protocol_relative_locs(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>//www.example.com/protocol-relative/</loc></url>
        <url><loc>/relative/</loc></url>
    </urlset>"""
        r = TextResponse(url="https://www.example.com/sitemap.xml", body=sitemap)
        spider = self.spider_class("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "https://www.example.com/protocol-relative/",
            "https://www.example.com/relative/",
        ]

    def test_sitemap_filter(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/english/</loc>
            <lastmod>2010-01-01</lastmod>
        </url>
        <url>
            <loc>http://www.example.com/portuguese/</loc>
            <lastmod>2005-01-01</lastmod>
        </url>
    </urlset>"""

        class FilteredSitemapSpider(self.spider_class):  # type: ignore[name-defined,misc]
            def sitemap_filter(self, entries):
                for entry in entries:
                    date_time = datetime.strptime(entry["lastmod"], "%Y-%m-%d").replace(
                        tzinfo=UTC
                    )
                    if date_time.year > 2008:
                        yield entry

        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)
        spider = self.spider_class("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/english/",
            "http://www.example.com/portuguese/",
        ]

        spider = FilteredSitemapSpider("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/english/"
        ]

    def test_sitemap_filter_with_alternate_links(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/english/article_1/</loc>
            <lastmod>2010-01-01</lastmod>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/deutsch/article_1/"/>
        </url>
        <url>
            <loc>http://www.example.com/english/article_2/</loc>
            <lastmod>2015-01-01</lastmod>
        </url>
    </urlset>"""

        class FilteredSitemapSpider(self.spider_class):  # type: ignore[name-defined,misc]
            def sitemap_filter(self, entries):
                for entry in entries:
                    alternate_links = entry.get("alternate", ())
                    for link in alternate_links:
                        if "/deutsch/" in link:
                            entry["loc"] = link
                            yield entry

        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)
        spider = self.spider_class("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/english/article_1/",
            "http://www.example.com/english/article_2/",
        ]

        spider = FilteredSitemapSpider("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/deutsch/article_1/"
        ]

    def test_sitemapindex_filter(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap>
            <loc>http://www.example.com/sitemap1.xml</loc>
            <lastmod>2004-01-01T20:00:00+00:00</lastmod>
        </sitemap>
        <sitemap>
            <loc>http://www.example.com/sitemap2.xml</loc>
            <lastmod>2005-01-01</lastmod>
        </sitemap>
    </sitemapindex>"""

        class FilteredSitemapSpider(self.spider_class):  # type: ignore[name-defined,misc]
            def sitemap_filter(self, entries):
                for entry in entries:
                    date_time = datetime.strptime(
                        entry["lastmod"].split("T")[0], "%Y-%m-%d"
                    ).replace(tzinfo=UTC)
                    if date_time.year > 2004:
                        yield entry

        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)
        spider = self.spider_class("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/sitemap1.xml",
            "http://www.example.com/sitemap2.xml",
        ]

        spider = FilteredSitemapSpider("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "http://www.example.com/sitemap2.xml"
        ]

    @pytest.mark.parametrize(
        ("rule", "result"),
        [(r"english", ["http://www.example.com/english/"]), (r"nonexistent", [])],
    )
    def test_sitemap_filter_with_rule(self, rule: str, result: list[str]):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>http://www.example.com/english/</loc></url>
        <url><loc>http://www.example.com/portuguese/</loc></url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _RuleSpider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_rules = [(rule, "parse")]

        spider = _RuleSpider("example.com")
        urls = [req.url for req in spider._parse_sitemap(r)]
        assert urls == result

    @coroutine_test
    async def test_sitemap_rules_with_callable(self, mockserver: MockServer):
        # A sitemap_rules entry may hold a callable instead of a method name.
        def parse_item(response):
            yield {"url": response.url}

        class _Spider(RawSitemapSpider, self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_rules = [("", parse_item)]

            def raw_body(self):
                assert self.mockserver
                loc = self.mockserver.url("/text")
                return (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{loc}</loc></url>"
                    "</urlset>"
                )

        items, _ = await crawl_items(_Spider, mockserver)
        assert items == [{"url": mockserver.url("/text")}]

    @coroutine_test
    async def test_sitemap_empty_loc(self, mockserver: MockServer):
        class _Spider(RawSitemapSpider, self.spider_class):  # type: ignore[name-defined,misc]
            def parse(self, response):
                yield {"url": response.url}

            def raw_body(self):
                assert self.mockserver
                loc = self.mockserver.url("/text")
                return (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    "<url><loc></loc></url>"
                    f"<url><loc>{loc}</loc></url>"
                    "</urlset>"
                )

        items, _ = await crawl_items(_Spider, mockserver)
        assert items == [{"url": mockserver.url("/text")}]

    @coroutine_test
    async def test_robots_txt_sitemap_comment(self, mockserver: MockServer):
        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            name = "robots_txt_sitemap_comment"
            sitemap_urls = [mockserver.url("/static/sitemap-comments/robots.txt")]

            def parse(self, response):
                yield {"url": response.url}

        items, _ = await crawl_items(_Spider, mockserver)
        assert items == [{"url": mockserver.url("/text")}]

    @coroutine_test
    async def test_robots_txt_requested_once(self, mockserver: MockServer):
        """robots.txt should be downloaded only once, even though both
        SitemapSpider and RobotsTxtMiddleware need its content: the spider
        requests it directly to look for sitemaps, while the middleware
        downloads it to know what the spider is allowed to crawl.
        """
        robots_url = mockserver.url("/robots.txt")

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            name = "robots_txt_requested_once"
            custom_settings = {"ROBOTSTXT_OBEY": True}
            sitemap_urls = [robots_url]

            @classmethod
            def from_crawler(cls, crawler, *args, **kwargs):
                spider = super().from_crawler(crawler, *args, **kwargs)
                spider.robots_txt_requests = []
                crawler.signals.connect(
                    spider._track_downloader_hit,
                    signals.request_reached_downloader,
                )
                return spider

            def _track_downloader_hit(self, request, spider):
                if request.url == robots_url:
                    self.robots_txt_requests.append(request)

        _, crawler = await crawl_items(_Spider, mockserver)
        assert isinstance(crawler.spider, _Spider)
        assert len(crawler.spider.robots_txt_requests) == 1

    @coroutine_test
    async def test_start_skips_robots_url_covered_by_another_sitemap_url(self):
        crawler = get_crawler(self.spider_class, settings_dict={"ROBOTSTXT_OBEY": True})
        spider = self.spider_class.from_crawler(
            crawler,
            "example.com",
            sitemap_urls=[
                "http://www.example.com/sitemap.xml",
                "http://www.example.com/robots.txt",
                "http://other.example.com/robots.txt",
            ],
        )

        urls = [request.url async for request in spider.start()]

        assert urls == [
            "http://www.example.com/sitemap.xml",
            "http://other.example.com/robots.txt",
        ]
        assert spider._signal_only_netlocs == {"www.example.com"}

    @coroutine_test
    async def test_robots_txt_requested_once_when_other_sitemap_url_goes_first(
        self, mockserver: MockServer
    ):
        """When another sitemap_urls entry targets the same host, the spider
        should not also request robots.txt itself: RobotsTxtMiddleware's own
        robots.txt request, triggered by that other entry, covers it.
        """
        robots_url = mockserver.url("/robots.txt")
        other_url = mockserver.url("/text")

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            name = "robots_txt_requested_once_other_first"
            custom_settings = {"ROBOTSTXT_OBEY": True}
            sitemap_urls = [other_url, robots_url]

            @classmethod
            def from_crawler(cls, crawler, *args, **kwargs):
                spider = super().from_crawler(crawler, *args, **kwargs)
                spider.robots_txt_requests = []
                crawler.signals.connect(
                    spider._track_downloader_hit,
                    signals.request_reached_downloader,
                )
                return spider

            def _track_downloader_hit(self, request, spider):
                if request.url == robots_url:
                    self.robots_txt_requests.append(request)

        _, crawler = await crawl_items(_Spider, mockserver)
        assert isinstance(crawler.spider, _Spider)
        assert len(crawler.spider.robots_txt_requests) == 1

    def test_parse_sitemap_empty_body(self, caplog: pytest.LogCaptureFixture) -> None:
        r = XmlResponse(url="http://www.example.com/sitemap.xml", body=b"")
        spider = self.spider_class("example.com")

        caplog.clear()
        with caplog.at_level(WARNING):
            results = list(spider._parse_sitemap(r))

        assert not results

        assert caplog.record_tuples == [
            (
                "scrapy.spiders.sitemap",
                WARNING,
                "Ignoring invalid sitemap: <200 http://www.example.com/sitemap.xml>",
            )
        ]

    def test_parse_sitemap_not_sitemap(self):
        body = b"""<?xml version="1.0" encoding="UTF-8"?>
    <some attr="string">
        <tag><tag3>sometext</tag3></tag>
        <tag2><tag4>sometext2</tag4></tag2>
    </some>"""
        r = XmlResponse(url="http://www.example.com/random.xml", body=body)
        spider = self.spider_class("example.com")

        results = list(spider._parse_sitemap(r))

        assert not results

    @pytest.mark.parametrize(
        ("follow", "result"),
        [
            (r"1.xml", ["http://www.example.com/sitemap1.xml"]),
            (re.compile(r"sitemap\d"), ["http://www.example.com/sitemap1.xml"]),
            (r"nonexistent", []),
        ],
    )
    def test_sitemap_follow(self, follow, result):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap>
            <loc>http://www.example.com/sitemap1.xml</loc>
        </sitemap>
    </sitemapindex>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _FollowSpider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_follow = [follow]

        spider = _FollowSpider("example.com")
        urls = [req.url for req in spider._parse_sitemap(r)]
        assert urls == result

    @pytest.mark.parametrize(
        ("follow", "result"),
        [
            (r"1.xml", ["http://www.example.com/sitemap1.xml"]),
            (re.compile(r"sitemap\d"), ["http://www.example.com/sitemap1.xml"]),
            (r"nonexistent", []),
        ],
    )
    def test_sitemap_follow_robotstxt(self, follow, result):
        robots = b"Sitemap: http://www.example.com/sitemap1.xml\n"
        r = TextResponse(url="http://www.example.com/robots.txt", body=robots)

        class _FollowSpider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_follow = [follow]

        spider = _FollowSpider("example.com")
        urls = [req.url for req in spider._parse_sitemap(r)]
        assert urls == result

    @coroutine_test
    async def test_sitemap_request(self, mockserver: MockServer):
        class _Spider(RawSitemapSpider, self.spider_class):  # type: ignore[name-defined,misc]
            def sitemap_request(self, loc, callback, entry, *, source, response):
                return Request(loc, callback, meta={"lastmod": entry["lastmod"]})

            def parse(self, response):
                yield {"url": response.url, "lastmod": response.meta["lastmod"]}

            def raw_body(self):
                assert self.mockserver
                loc = self.mockserver.url("/text")
                return (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{loc}</loc><lastmod>2005-01-01</lastmod></url>"
                    "</urlset>"
                )

        items, _ = await crawl_items(_Spider, mockserver)
        assert items == [{"url": mockserver.url("/text"), "lastmod": "2005-01-01"}]

    @pytest.mark.parametrize(
        ("url", "body", "expected"),
        [
            (
                "http://www.example.com/robots.txt",
                b"Sitemap: http://www.example.com/sitemap1.xml",
                [
                    (
                        "http://www.example.com/sitemap1.xml",
                        "_parse_sitemap",
                        {"loc": "http://www.example.com/sitemap1.xml"},
                        "robotstxt",
                    )
                ],
            ),
            (
                "http://www.example.com/sitemap.xml",
                b"""<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap>
            <loc>http://www.example.com/sitemap1.xml</loc>
            <lastmod>2005-01-01</lastmod>
        </sitemap>
    </sitemapindex>""",
                [
                    (
                        "http://www.example.com/sitemap1.xml",
                        "_parse_sitemap",
                        {
                            "loc": "http://www.example.com/sitemap1.xml",
                            "lastmod": "2005-01-01",
                        },
                        "sitemapindex",
                    )
                ],
            ),
            (
                "http://www.example.com/sitemap.xml",
                b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url>
            <loc>http://www.example.com/english/</loc>
            <lastmod>2005-01-01</lastmod>
        </url>
    </urlset>""",
                [
                    (
                        "http://www.example.com/english/",
                        "parse",
                        {
                            "loc": "http://www.example.com/english/",
                            "lastmod": "2005-01-01",
                        },
                        "urlset",
                    )
                ],
            ),
        ],
        ids=["robotstxt", "sitemapindex", "urlset"],
    )
    def test_sitemap_request_source(
        self,
        url: str,
        body: bytes,
        expected: list[tuple[str, str, dict[str, str], str]],
    ):
        response = TextResponse(url=url, body=body)
        calls = []

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            def sitemap_request(self, loc, callback, entry, *, source, response):
                calls.append((loc, callback.__name__, entry, source, response))
                return super().sitemap_request(
                    loc, callback, entry, source=source, response=response
                )

        spider = _Spider("example.com")
        requests = list(spider._parse_sitemap(response))

        assert [request.url for request in requests] == [loc for loc, *_ in expected]
        assert calls == [(*item, response) for item in expected]

    def test_sitemap_request_for_alternate_links(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/english/</loc>
            <xhtml:link rel="alternate" hreflang="de" href="http://www.example.com/deutsch/"/>
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_alternate_links = True

            def sitemap_request(self, loc, callback, entry, *, source, response):
                return Request(loc, callback, meta={"entry_loc": entry["loc"]})

        spider = _Spider("example.com")
        requests = list(spider._parse_sitemap(r))

        assert [request.meta["entry_loc"] for request in requests] == [
            "http://www.example.com/english/",
            "http://www.example.com/english/",
        ]

    @pytest.mark.parametrize(
        ("languages", "expected_url", "expected_label", "expected_match"),
        [
            # Exact matches are case-insensitive and never conflate en with
            # en-US.
            (["en-US"], "http://www.example.com/en-us/", "en-US", "exact"),
            (["en-us"], "http://www.example.com/en-us/", "en-US", "exact"),
            (["en"], "http://www.example.com/en/", "en", "exact"),
            # Primary language fallback: regionless en first, then any en-* tag.
            (["en-CA"], "http://www.example.com/en/", "en", "primary"),
            (["de-AT"], "http://www.example.com/de/", "de", "primary"),
            (["fr-FR"], "http://www.example.com/fr-ca/", "fr-CA", "primary"),
            # Preferences are considered in order; the second one is used when
            # the first one has no candidate at all.
            (["ja", "en-US"], "http://www.example.com/en-us/", "en-US", "exact"),
            # x-default is an explicit fallback after every preference failed.
            (["ja"], "http://www.example.com/default/", "x-default", "x-default"),
        ],
    )
    def test_sitemap_languages_priority(
        self,
        languages: list[str],
        expected_url: str,
        expected_label: str | None,
        expected_match: str,
    ):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/loc/</loc>
            <xhtml:link rel="alternate" hreflang="en"
                href="http://www.example.com/en/"/>
            <xhtml:link rel="alternate" hreflang="en-US"
                href="http://www.example.com/en-us/"/>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/de/"/>
            <xhtml:link rel="alternate" hreflang="fr-CA"
                href="http://www.example.com/fr-ca/"/>
            <xhtml:link rel="alternate" hreflang="x-default"
                href="http://www.example.com/default/"/>
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = languages

        spider = _Spider("example.com")
        requests = list(spider._parse_sitemap(r))

        assert len(requests) == 1
        request = requests[0]
        assert request.url == expected_url
        assert request.callback == spider.parse
        assert request.meta["sitemap_language"] == expected_label
        assert request.meta["sitemap_language_match"] == expected_match

    def test_sitemap_languages_string_attribute(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/loc/</loc>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/de/"/>
            <xhtml:link rel="alternate" hreflang="en"
                href="http://www.example.com/en/"/>
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = "de"

        spider = _Spider("example.com")
        assert spider._language_preferences == ["de"]
        requests = list(spider._parse_sitemap(r))
        assert [request.url for request in requests] == ["http://www.example.com/de/"]

    def test_sitemap_languages_order_independence(self):
        """Shuffling the order of unrelated candidates never changes the
        selected one; only the configured priorities decide."""
        template = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/loc/</loc>
            {links}
        </url>
    </urlset>"""
        links = [
            (b"en", b"http://www.example.com/en/"),
            (b"en-US", b"http://www.example.com/en-us/"),
            (b"de", b"http://www.example.com/de/"),
            (b"x-default", b"http://www.example.com/default/"),
        ]

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = ["en-US"]

        results = set()
        for order in ((0, 1, 2, 3), (3, 2, 1, 0), (2, 0, 3, 1), (1, 3, 0, 2)):
            link_xml = b"".join(
                b'<xhtml:link rel="alternate" hreflang="'
                + links[i][0]
                + b'" href="'
                + links[i][1]
                + b'"/>'
                for i in order
            )
            r = TextResponse(
                url="http://www.example.com/sitemap.xml",
                body=template.replace(b"{links}", link_xml),
            )
            spider = _Spider("example.com")
            requests = list(spider._parse_sitemap(r))
            assert len(requests) == 1
            results.add(
                (
                    requests[0].url,
                    requests[0].meta["sitemap_language"],
                    requests[0].meta["sitemap_language_match"],
                )
            )
        assert results == {("http://www.example.com/en-us/", "en-US", "exact")}

    def test_sitemap_languages_duplicate_url_single_request(self):
        """The same URL selected by several entries produces one request, and
        the first entry wins the metadata."""
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/a/</loc>
            <xhtml:link rel="alternate" hreflang="en"
                href="http://www.example.com/shared/"/>
        </url>
        <url>
            <loc>http://www.example.com/b/</loc>
            <xhtml:link rel="alternate" hreflang="en"
                href="http://www.example.com/shared/"/>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/de/"/>
        </url>
        <url>
            <loc>http://www.example.com/c/</loc>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/de/"/>
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = ["en", "de"]

        spider = _Spider("example.com")
        requests = list(spider._parse_sitemap(r))

        assert [request.url for request in requests] == [
            "http://www.example.com/shared/",
            "http://www.example.com/de/",
        ]
        assert requests[0].meta["sitemap_language"] == "en"
        assert requests[0].meta["sitemap_language_match"] == "exact"

    def test_sitemap_languages_ignores_alternate_links_setting(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/loc/</loc>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/de/"/>
            <xhtml:link rel="alternate" hreflang="en"
                href="http://www.example.com/en/"/>
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_alternate_links = True
            sitemap_languages = ["en"]

        spider = _Spider("example.com")
        requests = list(spider._parse_sitemap(r))
        assert [request.url for request in requests] == ["http://www.example.com/en/"]

    def test_sitemap_languages_missing_hreflang_and_invalid_labels(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Alternates without a usable hreflang label can never be selected;
        invalid labels are reported instead of being treated as languages."""
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/loc/</loc>
            <xhtml:link rel="alternate"
                href="http://www.example.com/no-label/"/>
            <xhtml:link rel="alternate" hreflang="not a tag!"
                href="http://www.example.com/invalid/"/>
            <xhtml:link rel="alternate" hreflang="x-default"
                href="http://www.example.com/default/"/>
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = ["en"]

        spider = _Spider("example.com")
        caplog.clear()
        with caplog.at_level("DEBUG", logger="scrapy.spiders.sitemap"):
            requests = list(spider._parse_sitemap(r))

        assert len(requests) == 1
        assert requests[0].url == "http://www.example.com/default/"
        assert requests[0].meta["sitemap_language"] == "x-default"
        assert requests[0].meta["sitemap_language_match"] == "x-default"
        assert "invalid hreflang label 'not a tag!'" in caplog.text
        assert "http://www.example.com/no-label/" not in [
            request.url for request in requests
        ]

    def test_sitemap_languages_invalid_preference_warning(
        self, caplog: pytest.LogCaptureFixture
    ):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/loc/</loc>
            <xhtml:link rel="alternate" hreflang="en"
                href="http://www.example.com/en/"/>
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = ["", "bad tag", "x-default", "en", "EN"]

        caplog.clear()
        with caplog.at_level(WARNING, logger="scrapy.spiders.sitemap"):
            spider = _Spider("example.com")

        assert spider._language_preferences == ["en"]
        warnings_text = caplog.text
        assert "Ignoring invalid language tag ''" in warnings_text
        assert "Ignoring invalid language tag 'bad tag'" in warnings_text
        assert "'x-default'" in warnings_text
        # Duplicate preferences (after normalization) are collapsed.
        assert warnings_text.count("Ignoring invalid language tag") == 2

        requests = list(spider._parse_sitemap(r))
        assert [request.url for request in requests] == ["http://www.example.com/en/"]

    def test_sitemap_languages_all_preferences_invalid_falls_back_to_loc(
        self, caplog: pytest.LogCaptureFixture
    ):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>http://www.example.com/loc/</loc></url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = ["nope!"]

        caplog.clear()
        with caplog.at_level(WARNING, logger="scrapy.spiders.sitemap"):
            spider = _Spider("example.com")
        assert spider._language_preferences == []

        requests = list(spider._parse_sitemap(r))
        assert len(requests) == 1
        assert requests[0].url == "http://www.example.com/loc/"
        assert requests[0].meta["sitemap_language"] is None
        assert requests[0].meta["sitemap_language_match"] == "loc"

    def test_sitemap_languages_respects_sitemap_rules(self):
        """The selected URL still goes through sitemap_rules, and the request
        keeps the matching callback."""
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/page/1</loc>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/product/1"/>
            <xhtml:link rel="alternate" hreflang="en"
                href="http://www.example.com/page/1"/>
        </url>
        <url>
            <loc>http://www.example.com/page/2</loc>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/other/2"/>
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        def parse_product(response):
            yield {"url": response.url}

        class _ProductSpider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = ["de"]
            sitemap_rules = [("/product/", parse_product)]

        product_spider = _ProductSpider("example.com")
        product_requests = list(product_spider._parse_sitemap(r))
        assert [request.url for request in product_requests] == [
            "http://www.example.com/product/1"
        ]
        assert product_requests[0].callback is parse_product
        assert product_requests[0].meta["sitemap_language"] == "de"
        assert product_requests[0].meta["sitemap_language_match"] == "exact"

        class _PageSpider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = ["de"]
            sitemap_rules = [("/page/", "parse")]

        page_spider = _PageSpider("example.com")
        # The selected German URLs match no /page/ rule, so nothing is yielded.
        assert list(page_spider._parse_sitemap(r)) == []

    def test_sitemap_languages_does_not_modify_entries(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/loc/</loc>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/de/"/>
            <xhtml:link rel="alternate" hreflang="en"
                href="http://www.example.com/en/"/>
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)
        seen_entries = []

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = ["en"]

            def sitemap_filter(self, entries):
                for entry in entries:
                    seen_entries.append(entry)
                    yield entry

            def sitemap_request(self, loc, callback, entry, *, source, response):
                # The entry handed to sitemap_request is the untouched entry.
                assert entry["loc"] == "http://www.example.com/loc/"
                assert entry["alternate"] == [
                    "http://www.example.com/de/",
                    "http://www.example.com/en/",
                ]
                return super().sitemap_request(
                    loc, callback, entry, source=source, response=response
                )

        spider = _Spider("example.com")
        requests = list(spider._parse_sitemap(r))

        assert [request.url for request in requests] == ["http://www.example.com/en/"]
        assert seen_entries[0]["loc"] == "http://www.example.com/loc/"
        assert seen_entries[0]["alternate_hreflang"] == ["de", "en"]

    def test_sitemap_languages_relative_urls_joined_before_dedup(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>/shared/</loc>
            <xhtml:link rel="alternate" hreflang="en" href="/shared/"/>
            <xhtml:link rel="alternate" hreflang="de" href="/de/"/>
        </url>
        <url>
            <loc>/other/</loc>
            <xhtml:link rel="alternate" hreflang="en" href="/shared/"/>
        </url>
    </urlset>"""
        r = TextResponse(url="https://www.example.com/sitemap.xml", body=sitemap)

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = ["en", "de"]

        spider = _Spider("example.com")
        requests = list(spider._parse_sitemap(r))

        # The relative loc and alternate resolve to the same absolute URL:
        # only one request is produced for it.
        assert [request.url for request in requests] == [
            "https://www.example.com/shared/"
        ]

    def test_sitemap_languages_repeated_iteration_is_stable(self):
        """Parsing the same sitemap twice yields identical, independent
        request lists; selection state never leaks between iterations."""
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/loc/</loc>
            <xhtml:link rel="alternate" hreflang="en"
                href="http://www.example.com/shared/"/>
        </url>
        <url>
            <loc>http://www.example.com/other/</loc>
            <xhtml:link rel="alternate" hreflang="en"
                href="http://www.example.com/shared/"/>
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = ["en"]

        spider = _Spider("example.com")
        first = list(spider._parse_sitemap(r))
        second = list(spider._parse_sitemap(r))

        expected = [
            (
                "http://www.example.com/shared/",
                "en",
                "exact",
            )
        ]
        for requests in (first, second):
            assert [
                (
                    request.url,
                    request.meta["sitemap_language"],
                    request.meta["sitemap_language_match"],
                )
                for request in requests
            ] == expected
        assert first[0] is not second[0]

    def test_sitemap_languages_skips_entry_without_loc(self):
        """A custom sitemap_filter may yield entries without a loc; those are
        skipped rather than crashing language selection."""
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/loc/</loc>
            <xhtml:link rel="alternate" hreflang="en"
                href="http://www.example.com/en/"/>
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = ["en"]

            def sitemap_filter(self, entries):
                yield {"lastmod": "2010-01-01"}  # entry without a loc
                yield from entries

        spider = _Spider("example.com")
        requests = list(spider._parse_sitemap(r))
        assert [request.url for request in requests] == ["http://www.example.com/en/"]

    def test_sitemap_languages_default_behavior_unchanged(self):
        """Without sitemap_languages, requests carry no language metadata and
        the alternate setting keeps its original behavior."""
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:xhtml="http://www.w3.org/1999/xhtml">
        <url>
            <loc>http://www.example.com/loc/</loc>
            <xhtml:link rel="alternate" hreflang="de"
                href="http://www.example.com/de/"/>
        </url>
    </urlset>"""
        r = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)

        spider = self.spider_class("example.com")
        assert spider._language_preferences is None
        requests = list(spider._parse_sitemap(r))
        assert [request.url for request in requests] == ["http://www.example.com/loc/"]
        assert "sitemap_language" not in requests[0].meta
        assert "sitemap_language_match" not in requests[0].meta

    @coroutine_test
    async def test_sitemap_languages_crawl_meta(self, mockserver: MockServer):
        class _Spider(RawSitemapSpider, self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_languages = ["en-GB", "de"]

            def parse(self, response):
                yield {
                    "url": response.url,
                    "language": response.meta["sitemap_language"],
                    "match": response.meta["sitemap_language_match"],
                }

            def raw_body(self):
                assert self.mockserver
                en = self.mockserver.url("/en")
                en_gb = self.mockserver.url("/en-gb")
                de = self.mockserver.url("/de")
                loc = self.mockserver.url("/loc")
                return (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                    'xmlns:xhtml="http://www.w3.org/1999/xhtml">'
                    f"<url><loc>{loc}</loc>"
                    f'<xhtml:link rel="alternate" hreflang="en" href="{en}"/>'
                    f'<xhtml:link rel="alternate" hreflang="en-GB" href="{en_gb}"/>'
                    f'<xhtml:link rel="alternate" hreflang="de" href="{de}"/>'
                    "</url></urlset>"
                )

        items, _ = await crawl_items(_Spider, mockserver)
        assert items == [
            {
                "url": mockserver.url("/en-gb"),
                "language": "en-GB",
                "match": "exact",
            }
        ]

    def test_compression_bomb_setting(self):
        settings = {"DOWNLOAD_MAXSIZE": 10_000_000}
        crawler = get_crawler(settings_dict=settings)
        spider = self.spider_class.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(url="https://example.com")
        response = Response(url="https://example.com", body=body, request=request)
        assert spider._get_sitemap_body(response) is None

    @pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
    def test_compression_bomb_spider_attr(self):
        class DownloadMaxSizeSpider(self.spider_class):  # type: ignore[name-defined,misc]
            download_maxsize = 10_000_000

        crawler = get_crawler()
        spider = DownloadMaxSizeSpider.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(url="https://example.com")
        response = Response(url="https://example.com", body=body, request=request)
        assert spider._get_sitemap_body(response) is None

    def test_compression_bomb_request_meta(self):
        crawler = get_crawler()
        spider = self.spider_class.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(
            url="https://example.com", meta={"download_maxsize": 10_000_000}
        )
        response = Response(url="https://example.com", body=body, request=request)
        assert spider._get_sitemap_body(response) is None

    def test_download_warnsize_setting(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = {"DOWNLOAD_WARNSIZE": 10_000_000}
        crawler = get_crawler(settings_dict=settings)
        spider = self.spider_class.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(url="https://example.com")
        response = Response(url="https://example.com", body=body, request=request)
        caplog.clear()
        with caplog.at_level(WARNING, logger="scrapy.spiders.sitemap"):
            spider._get_sitemap_body(response)
        assert caplog.record_tuples == [
            (
                "scrapy.spiders.sitemap",
                WARNING,
                (
                    "<200 https://example.com> body size after decompression "
                    "(11511612 B) is larger than the download warning size "
                    "(10000000 B)."
                ),
            ),
        ]

    @pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
    def test_download_warnsize_spider_attr(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class DownloadWarnSizeSpider(self.spider_class):  # type: ignore[name-defined,misc]
            download_warnsize = 10_000_000

        crawler = get_crawler()
        spider = DownloadWarnSizeSpider.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(
            url="https://example.com", meta={"download_warnsize": 10_000_000}
        )
        response = Response(url="https://example.com", body=body, request=request)
        caplog.clear()
        with caplog.at_level(WARNING, logger="scrapy.spiders.sitemap"):
            spider._get_sitemap_body(response)
        assert caplog.record_tuples == [
            (
                "scrapy.spiders.sitemap",
                WARNING,
                (
                    "<200 https://example.com> body size after decompression "
                    "(11511612 B) is larger than the download warning size "
                    "(10000000 B)."
                ),
            ),
        ]

    def test_download_warnsize_setting_ignored_with_request_maxsize(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = {"DOWNLOAD_WARNSIZE": 10_000_000}
        crawler = get_crawler(settings_dict=settings)
        spider = self.spider_class.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(
            url="https://example.com", meta={"download_maxsize": 20_000_000}
        )
        response = Response(url="https://example.com", body=body, request=request)
        caplog.clear()
        with caplog.at_level(WARNING, logger="scrapy.spiders.sitemap"):
            spider._get_sitemap_body(response)
        assert not caplog.records

    def test_download_warnsize_request_meta(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        crawler = get_crawler()
        spider = self.spider_class.from_crawler(crawler, "example.com")
        body_path = Path(tests_datadir, "compressed", "bomb-gzip.bin")
        body = body_path.read_bytes()
        request = Request(
            url="https://example.com", meta={"download_warnsize": 10_000_000}
        )
        response = Response(url="https://example.com", body=body, request=request)
        caplog.clear()
        with caplog.at_level(WARNING, logger="scrapy.spiders.sitemap"):
            spider._get_sitemap_body(response)
        assert caplog.record_tuples == [
            (
                "scrapy.spiders.sitemap",
                WARNING,
                (
                    "<200 https://example.com> body size after decompression "
                    "(11511612 B) is larger than the download warning size "
                    "(10000000 B)."
                ),
            ),
        ]

    @coroutine_test
    async def test_sitemap_urls(self):
        class TestSpider(self.spider_class):  # type: ignore[name-defined,misc]
            name = "test"
            sitemap_urls = ["https://toscrape.com/sitemap.xml"]

        crawler = get_crawler(TestSpider)
        spider = TestSpider.from_crawler(crawler)
        requests = [request async for request in spider.start()]

        assert len(requests) == 1
        request = requests[0]
        assert request.url == "https://toscrape.com/sitemap.xml"
        assert request.dont_filter is False
        assert request.callback == spider._parse_sitemap  # pylint: disable=comparison-with-callable

    @pytest.mark.parametrize(
        ("schemes", "expected"),
        [
            (None, ["http"]),
            ((), ["file", "http"]),
            (["file"], ["file"]),
        ],
    )
    @coroutine_test
    async def test_sitemap_schemes_urlset(
        self,
        mockserver: MockServer,
        tmp_path: Path,
        schemes: list[str] | None,
        expected: list[str],
    ) -> None:
        page = tmp_path / "page.html"
        page.write_text("<html></html>")
        urls = {"file": page.as_uri(), "http": mockserver.url("/text")}

        class _Spider(RawSitemapSpider, self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_schemes = schemes

            def parse(self, response):
                yield {"url": response.url}

            def raw_body(self):
                return (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{urls['file']}</loc></url>"
                    f"<url><loc>{urls['http']}</loc></url>"
                    "</urlset>"
                )

        items, _ = await crawl_items(_Spider, mockserver)
        assert sorted(item["url"] for item in items) == sorted(
            urls[scheme] for scheme in expected
        )

    @pytest.mark.parametrize(("schemes", "followed"), [(None, False), ((), True)])
    @coroutine_test
    async def test_sitemap_schemes_sitemapindex(
        self,
        mockserver: MockServer,
        tmp_path: Path,
        schemes: list[str] | None,
        followed: bool,
    ) -> None:
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<url><loc>{mockserver.url('/text')}</loc></url>"
            "</urlset>"
        )

        class _Spider(RawSitemapSpider, self.spider_class):  # type: ignore[name-defined,misc]
            sitemap_schemes = schemes

            def parse(self, response):
                yield {"url": response.url}

            def raw_body(self):
                return (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<sitemap><loc>{sitemap.as_uri()}</loc></sitemap>"
                    "</sitemapindex>"
                )

        items, _ = await crawl_items(_Spider, mockserver)
        assert items == ([{"url": mockserver.url("/text")}] if followed else [])

    @coroutine_test
    async def test_sitemap_schemes_source_scheme(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        page = tmp_path / "page.html"
        page.write_text("<html></html>")
        sitemap = tmp_path / "sitemap.xml"
        sitemap.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<url><loc>{page.as_uri()}</loc></url>"
            "</urlset>"
        )

        class _Spider(self.spider_class):  # type: ignore[name-defined,misc]
            name = "test"
            sitemap_urls = [sitemap.as_uri()]

            def parse(self, response):
                yield {"url": response.url}

        items, _ = await crawl_items(_Spider, mockserver)
        assert items == [{"url": page.as_uri()}]

    def test_sitemap_schemes_robotstxt(self):
        robots = (
            b"Sitemap: file:///etc/sitemap.xml\n"
            b"Sitemap: ftp://example.com/sitemap.xml\n"
            b"Sitemap: https://example.com/sitemap.xml\n"
        )
        r = TextResponse(url="http://www.example.com/robots.txt", body=robots)
        spider = self.spider_class("example.com")
        assert [req.url for req in spider._parse_sitemap(r)] == [
            "https://example.com/sitemap.xml",
        ]

    def test_sitemap_schemes_robots_parsed_signal(self):
        crawler = get_crawler(self.spider_class, settings_dict={"ROBOTSTXT_OBEY": True})
        spider = self.spider_class.from_crawler(crawler, "example.com")
        crawler.engine = mock.MagicMock()
        spider._signal_only_netlocs.add("www.example.com")

        class FakeRobotParser:
            def sitemaps(self):
                return ["file:///etc/sitemap.xml", "/sitemap.xml"]

        crawler.signals.send_catch_log(
            signal=signals.robots_parsed,
            robotparser=FakeRobotParser(),
            request=Request("http://www.example.com/some/page"),
            response=TextResponse("http://www.example.com/robots.txt"),
        )

        crawler.engine.crawl.assert_called_once()
        scheduled = crawler.engine.crawl.call_args[0][0]
        assert scheduled.url == "http://www.example.com/sitemap.xml"


def test_iterloc_deprecated():
    entries = [{"loc": "http://www.example.com/"}]

    with pytest.warns(ScrapyDeprecationWarning, match="iterloc"):
        locs = list(iterloc(entries))

    assert locs == ["http://www.example.com/"]
