from __future__ import annotations

import gzip
import re
import time
from datetime import UTC, date, datetime, timedelta, timezone
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
from scrapy.spiders.sitemap import _parse_lastmod_value, iterloc
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


URLSET_WITH_LASTMOD = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
    <url>
        <loc>http://www.example.com/old/</loc>
        <lastmod>2025-01-01T00:00:00Z</lastmod>
    </url>
    <url>
        <loc>http://www.example.com/tokyo/</loc>
        <lastmod>2025-06-01T09:00:00+09:00</lastmod>
    </url>
    <url>
        <loc>http://www.example.com/utc-midnight/</loc>
        <lastmod>2025-06-01T00:00:00Z</lastmod>
    </url>
    <url>
        <loc>http://www.example.com/date-only/</loc>
        <lastmod>2025-06-02</lastmod>
    </url>
    <url>
        <loc>http://www.example.com/new/</loc>
        <lastmod>2025-07-01T12:00:00+02:00</lastmod>
    </url>
    <url>
        <loc>http://www.example.com/missing/</loc>
    </url>
    <url>
        <loc>http://www.example.com/empty/</loc>
        <lastmod></lastmod>
    </url>
    <url>
        <loc>http://www.example.com/invalid/</loc>
        <lastmod>not-a-date</lastmod>
    </url>
    <url>
        <loc>http://www.example.com/impossible-date/</loc>
        <lastmod>2025-02-30</lastmod>
    </url>
    <url>
        <loc>http://www.example.com/naive/</loc>
        <lastmod>2025-06-15T12:00:00</lastmod>
    </url>
    <url>
        <loc>http://www.example.com/alternate/</loc>
        <lastmod>2024-01-01T00:00:00Z</lastmod>
        <xhtml:link rel="alternate" hreflang="de"
            href="http://www.example.com/deutsch/"/>
    </url>
</urlset>"""

SITEMAPINDEX_WITH_LASTMOD = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <sitemap>
        <loc>http://www.example.com/old-sitemap.xml</loc>
        <lastmod>2025-01-01T00:00:00Z</lastmod>
    </sitemap>
    <sitemap>
        <loc>http://www.example.com/new-sitemap.xml</loc>
        <lastmod>2025-07-01T00:00:00Z</lastmod>
    </sitemap>
    <sitemap>
        <loc>http://www.example.com/missing-sitemap.xml</loc>
    </sitemap>
</sitemapindex>"""


def _parse_urlset(
    spider: SitemapSpider, body: bytes = URLSET_WITH_LASTMOD
) -> list[str]:
    response = TextResponse(url="http://www.example.com/sitemap.xml", body=body)
    return [request.url for request in spider._parse_sitemap(response)]


class TestSitemapSpiderLastmodCutoff:
    def test_disabled_by_default_processes_every_entry(self):
        spider = SitemapSpider("example.com")
        assert spider._lastmod_cutoff is None

        urls = _parse_urlset(spider)

        assert urls == [
            "http://www.example.com/old/",
            "http://www.example.com/tokyo/",
            "http://www.example.com/utc-midnight/",
            "http://www.example.com/date-only/",
            "http://www.example.com/new/",
            "http://www.example.com/missing/",
            "http://www.example.com/empty/",
            "http://www.example.com/invalid/",
            "http://www.example.com/impossible-date/",
            "http://www.example.com/naive/",
            "http://www.example.com/alternate/",
        ]

    @pytest.mark.parametrize(
        "cutoff",
        [
            "2025-06-01T00:00:00Z",
            datetime(2025, 6, 1, tzinfo=UTC),
            datetime(2025, 6, 1, 2, 0, tzinfo=timezone(timedelta(hours=2))),
            "2025-06-01",
            date(2025, 6, 1),
        ],
        ids=["string-utc", "datetime-utc", "datetime-offset", "string-date", "date"],
    )
    def test_cutoff_inputs_are_normalized_to_utc(self, cutoff):
        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = cutoff
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        spider = _Spider("example.com")

        assert spider._lastmod_cutoff == datetime(2025, 6, 1, tzinfo=UTC)
        assert spider._lastmod_cutoff.tzinfo == UTC

    def test_default_policies_include_missing_invalid_naive_and_exclude_boundary(self):
        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"

        urls = _parse_urlset(_Spider("example.com"))

        # The boundary entry (2025-06-01T00:00:00Z) and older entries are out,
        # the Tokyo entry is kept (09:00+09:00 == 00:00 UTC, so it is also the
        # boundary), missing/empty/invalid/naive entries are included.
        assert urls == [
            "http://www.example.com/date-only/",
            "http://www.example.com/new/",
            "http://www.example.com/missing/",
            "http://www.example.com/empty/",
            "http://www.example.com/invalid/",
            "http://www.example.com/impossible-date/",
            "http://www.example.com/naive/",
        ]

    def test_boundary_include_keeps_entries_equal_to_cutoff(self):
        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_boundary = "include"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        urls = _parse_urlset(_Spider("example.com"))

        assert urls == [
            "http://www.example.com/tokyo/",
            "http://www.example.com/utc-midnight/",
            "http://www.example.com/date-only/",
            "http://www.example.com/new/",
            "http://www.example.com/naive/",
        ]

    def test_missing_policy_exclude_drops_entries_without_lastmod(self):
        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        urls = _parse_urlset(_Spider("example.com"))

        assert urls == [
            "http://www.example.com/date-only/",
            "http://www.example.com/new/",
            "http://www.example.com/naive/",
        ]

    def test_invalid_policy_exclude_drops_unparseable_entries(self):
        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_invalid = "exclude"

        urls = _parse_urlset(_Spider("example.com"))

        assert "http://www.example.com/invalid/" not in urls
        assert "http://www.example.com/impossible-date/" not in urls
        # Missing entries are governed by the missing policy, still included.
        assert "http://www.example.com/missing/" in urls
        assert "http://www.example.com/empty/" in urls

    def test_naive_policy_reject_routes_naive_entries_to_invalid_policy(self):
        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_naive = "reject"
            sitemap_lastmod_invalid = "exclude"
            sitemap_lastmod_missing = "exclude"

        urls = _parse_urlset(_Spider("example.com"))

        assert "http://www.example.com/naive/" not in urls

    def test_naive_policy_reject_with_invalid_include_keeps_naive_entries(self):
        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_naive = "reject"
            sitemap_lastmod_invalid = "include"
            sitemap_lastmod_missing = "exclude"

        urls = _parse_urlset(_Spider("example.com"))

        assert "http://www.example.com/naive/" in urls

    def test_naive_value_is_read_as_utc_not_local_time(self):
        # A naive value has no offset to resolve; reading it as UTC gives the
        # 12:00 UTC instant regardless of the host time zone, which a cutoff at
        # 11:00 UTC must therefore accept.
        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-15T11:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        urls = _parse_urlset(_Spider("example.com"))

        assert "http://www.example.com/naive/" in urls

    @pytest.mark.skipif(
        not hasattr(time, "tzset"), reason="time.tzset() is required to change TZ"
    )
    def test_results_do_not_depend_on_host_time_zone(self, monkeypatch):
        def urls_in_time_zone(tz_name: str) -> list[str]:
            monkeypatch.setenv("TZ", tz_name)
            time.tzset()
            try:

                class _Spider(SitemapSpider):
                    sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
                    sitemap_lastmod_missing = "exclude"
                    sitemap_lastmod_invalid = "exclude"

                return _parse_urlset(_Spider("example.com"))
            finally:
                monkeypatch.undo()
                time.tzset()

        expected = urls_in_time_zone("UTC")
        assert expected
        assert expected == urls_in_time_zone("America/Los_Angeles")
        assert expected == urls_in_time_zone("Asia/Tokyo")

    def test_offsets_are_compared_as_instants_across_time_zones(self):
        # 2025-06-01T09:00:00+09:00 == 2025-06-01T00:00:00Z: the Tokyo entry
        # sits exactly on the cutoff regardless of string ordering.
        class _ExcludeBoundary(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        tokyo = "http://www.example.com/tokyo/"
        assert tokyo not in _parse_urlset(_ExcludeBoundary("example.com"))

        class _IncludeBoundary(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_boundary = "include"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        assert tokyo in _parse_urlset(_IncludeBoundary("example.com"))

    def test_dst_spring_forward_offset_is_compared_correctly(self):
        # US DST starts 2025-03-09: 03:30 in New York does not exist in local
        # wall time, but with an explicit offset the instant is unambiguous.
        # -05:00 (standard) 03:30 == 08:30 UTC; -04:00 (daylight) == 07:30 UTC.
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url>
        <loc>http://www.example.com/standard-time/</loc>
        <lastmod>2025-03-09T03:30:00-05:00</lastmod>
    </url>
    <url>
        <loc>http://www.example.com/daylight-time/</loc>
        <lastmod>2025-03-09T03:30:00-04:00</lastmod>
    </url>
</urlset>"""

        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-03-09T08:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        urls = _parse_urlset(_Spider("example.com"), body=sitemap)

        assert urls == ["http://www.example.com/standard-time/"]

    def test_dst_fall_back_same_wall_time_different_instants(self):
        # US DST ends 2025-11-02: 01:30 happens twice, distinguished only by
        # the offset. The -04:00 reading is the earlier UTC instant.
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url>
        <loc>http://www.example.com/first-1-30/</loc>
        <lastmod>2025-11-02T01:30:00-04:00</lastmod>
    </url>
    <url>
        <loc>http://www.example.com/second-1-30/</loc>
        <lastmod>2025-11-02T01:30:00-05:00</lastmod>
    </url>
</urlset>"""

        # Cutoff at 06:00 UTC: first 01:30 (-04:00) is 05:30 UTC (before),
        # second 01:30 (-05:00) is 06:30 UTC (after).
        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-11-02T06:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        urls = _parse_urlset(_Spider("example.com"), body=sitemap)

        assert urls == ["http://www.example.com/second-1-30/"]

    def test_date_only_entry_is_midnight_utc(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url>
        <loc>http://www.example.com/day/</loc>
        <lastmod>2025-06-02</lastmod>
    </url>
</urlset>"""

        class _AtMidnight(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-02T00:00:00Z"
            sitemap_lastmod_missing = "exclude"

        assert _parse_urlset(_AtMidnight("example.com"), body=sitemap) == []

        class _BeforeMidnight(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T23:59:59Z"
            sitemap_lastmod_missing = "exclude"

        assert _parse_urlset(_BeforeMidnight("example.com"), body=sitemap) == [
            "http://www.example.com/day/"
        ]

    def test_alternate_links_are_gated_by_the_same_lastmod(self):
        class _Spider(SitemapSpider):
            sitemap_alternate_links = True
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        urls = _parse_urlset(_Spider("example.com"))

        assert "http://www.example.com/alternate/" not in urls
        assert "http://www.example.com/deutsch/" not in urls

    def test_sitemap_index_lastmod_only_gates_sub_sitemap_traversal(self):
        response = TextResponse(
            url="http://www.example.com/index.xml", body=SITEMAPINDEX_WITH_LASTMOD
        )

        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        urls = [
            request.url for request in _Spider("example.com")._parse_sitemap(response)
        ]

        assert urls == ["http://www.example.com/new-sitemap.xml"]

    def test_sitemap_index_missing_policy_keeps_sub_sitemap_by_default(self):
        response = TextResponse(
            url="http://www.example.com/index.xml", body=SITEMAPINDEX_WITH_LASTMOD
        )

        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"

        urls = [
            request.url for request in _Spider("example.com")._parse_sitemap(response)
        ]

        assert "http://www.example.com/missing-sitemap.xml" in urls
        assert "http://www.example.com/new-sitemap.xml" in urls
        assert "http://www.example.com/old-sitemap.xml" not in urls

    def test_sitemap_index_lastmod_is_not_applied_to_child_urls(self, monkeypatch):
        # The index's fresh lastmod must not be inherited as the lastmod of
        # the URLs inside the sub-sitemap it points to.
        index = TextResponse(
            url="http://www.example.com/index.xml",
            body=b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <sitemap>
        <loc>http://www.example.com/child.xml</loc>
        <lastmod>2025-07-01T00:00:00Z</lastmod>
    </sitemap>
</sitemapindex>""",
        )
        child = TextResponse(
            url="http://www.example.com/child.xml",
            body=b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url>
        <loc>http://www.example.com/old-page/</loc>
        <lastmod>2025-01-01T00:00:00Z</lastmod>
    </url>
</urlset>""",
        )

        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        spider = _Spider("example.com")
        index_requests = list(spider._parse_sitemap(index))
        assert [request.url for request in index_requests] == [
            "http://www.example.com/child.xml"
        ]
        assert [request.url for request in spider._parse_sitemap(child)] == []

    def test_cutoff_runs_after_sitemap_filter_and_keeps_its_order(self):
        seen: list[str] = []

        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

            def sitemap_filter(self, entries):
                for entry in entries:
                    seen.append(entry["loc"])
                    # The user filter keeps an entry the cutoff would drop.
                    if entry["loc"].endswith("/old/") or entry["loc"].endswith("/new/"):
                        yield entry

        urls = _parse_urlset(_Spider("example.com"))

        # The user filter saw every entry first; its survivors then go through
        # the cutoff, which still removes the old one.
        assert seen[0] == "http://www.example.com/old/"
        assert urls == ["http://www.example.com/new/"]

    def test_sitemap_rules_still_apply_after_cutoff(self):
        sitemap = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url>
        <loc>http://www.example.com/product/new</loc>
        <lastmod>2025-07-01T00:00:00Z</lastmod>
    </url>
    <url>
        <loc>http://www.example.com/other/new</loc>
        <lastmod>2025-07-01T00:00:00Z</lastmod>
    </url>
    <url>
        <loc>http://www.example.com/product/old</loc>
        <lastmod>2025-01-01T00:00:00Z</lastmod>
    </url>
</urlset>"""

        class _Spider(SitemapSpider):
            sitemap_rules = [("/product/", "parse")]
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        response = TextResponse(url="http://www.example.com/sitemap.xml", body=sitemap)
        spider = _Spider("example.com")
        requests = list(spider._parse_sitemap(response))

        assert [request.url for request in requests] == [
            "http://www.example.com/product/new"
        ]
        assert requests[0].callback == spider.parse

    def test_entries_and_xml_are_not_mutated(self):
        body = URLSET_WITH_LASTMOD
        response = TextResponse(url="http://www.example.com/sitemap.xml", body=body)

        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        list(_Spider("example.com")._parse_sitemap(response))

        assert response.body == body

    def test_spider_instances_do_not_share_cutoff_state(self):
        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

        first = _Spider("example.com")
        second = _Spider("example.com")

        assert first._lastmod_cutoff is not second._lastmod_cutoff
        assert first._lastmod_cutoff == second._lastmod_cutoff

        second.sitemap_lastmod_cutoff = "2026-01-01T00:00:00Z"
        second._lastmod_cutoff = _parse_lastmod_value(
            second.sitemap_lastmod_cutoff, naive_policy="utc"
        )

        assert first._lastmod_cutoff == datetime(2025, 6, 1, tzinfo=UTC)
        assert second._lastmod_cutoff == datetime(2026, 1, 1, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("attributes", "match"),
        [
            ({"sitemap_lastmod_missing": "maybe"}, "sitemap_lastmod_missing"),
            ({"sitemap_lastmod_invalid": "maybe"}, "sitemap_lastmod_invalid"),
            ({"sitemap_lastmod_naive": "maybe"}, "sitemap_lastmod_naive"),
            ({"sitemap_lastmod_boundary": "maybe"}, "sitemap_lastmod_boundary"),
        ],
    )
    def test_invalid_policy_value_fails_at_initialization(self, attributes, match):
        with pytest.raises(ValueError, match=match):
            SitemapSpider("example.com", **attributes)

    @pytest.mark.parametrize(
        "cutoff",
        [
            "not-a-date",
            "2025-02-30",
            "2025-1-1",
            "2025-06-01T00:00:00",  # naive string under the reject policy
            123,
        ],
        ids=["garbage", "impossible-date", "non-padded-date", "naive-rejected", "int"],
    )
    def test_invalid_cutoff_fails_at_initialization(self, cutoff):
        kwargs: dict[str, object] = {"sitemap_lastmod_cutoff": cutoff}
        if cutoff == "2025-06-01T00:00:00":
            kwargs["sitemap_lastmod_naive"] = "reject"
        with pytest.raises(ValueError, match="sitemap_lastmod_cutoff"):
            SitemapSpider("example.com", **kwargs)

    def test_naive_cutoff_datetime_is_accepted_as_utc_by_default(self):
        class _Spider(SitemapSpider):
            sitemap_lastmod_cutoff = datetime(2025, 6, 1, 0, 0)

        spider = _Spider("example.com")
        assert spider._lastmod_cutoff == datetime(2025, 6, 1, tzinfo=UTC)

    def test_naive_cutoff_datetime_fails_under_reject_policy(self):
        with pytest.raises(ValueError, match="sitemap_lastmod_cutoff"):
            SitemapSpider(
                "example.com",
                sitemap_lastmod_cutoff=datetime(2025, 6, 1),
                sitemap_lastmod_naive="reject",
            )

    def test_cutoff_accepted_through_from_crawler(self):
        crawler = get_crawler(
            SitemapSpider,
            settings_dict={},
        )
        spider = SitemapSpider.from_crawler(
            crawler,
            "example.com",
            sitemap_lastmod_cutoff="2025-06-01T00:00:00Z",
            sitemap_lastmod_missing="exclude",
        )
        assert spider._lastmod_cutoff == datetime(2025, 6, 1, tzinfo=UTC)

    @coroutine_test
    async def test_cutoff_through_full_crawl(self, mockserver: MockServer):
        kept = mockserver.url("/kept")
        dropped = mockserver.url("/dropped")

        class _Spider(RawSitemapSpider, SitemapSpider):  # type: ignore[misc]
            sitemap_lastmod_cutoff = "2025-06-01T00:00:00Z"
            sitemap_lastmod_missing = "exclude"
            sitemap_lastmod_invalid = "exclude"

            def parse(self, response):
                yield {"url": response.url}

            def raw_body(self):
                assert self.mockserver
                return (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{dropped}</loc>"
                    "<lastmod>2025-01-01T00:00:00Z</lastmod></url>"
                    f"<url><loc>{kept}</loc>"
                    "<lastmod>2025-07-01T00:00:00Z</lastmod></url>"
                    "</urlset>"
                )

        items, _ = await crawl_items(_Spider, mockserver)
        assert items == [{"url": kept}]

    @coroutine_test
    async def test_cutoff_failure_stops_crawl_before_requests(
        self, mockserver: MockServer
    ):
        class _Spider(RawSitemapSpider, SitemapSpider):  # type: ignore[misc]
            sitemap_lastmod_cutoff = "not-a-date"

            def parse(self, response):
                yield {"url": response.url}

            def raw_body(self):
                return ""

        crawler = get_crawler(_Spider)
        with pytest.raises(ValueError, match="sitemap_lastmod_cutoff"):
            await crawler.crawl_async(mockserver=mockserver)
        assert crawler.stats.get_value("downloader/request_count", 0) == 0


class TestParseLastmodValue:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2025-06-01", datetime(2025, 6, 1, tzinfo=UTC)),
            ("2025-06-01T00:00:00Z", datetime(2025, 6, 1, tzinfo=UTC)),
            (
                "2025-06-01T09:00:00+09:00",
                datetime(2025, 6, 1, 0, 0, tzinfo=UTC),
            ),
            (
                "2025-07-01T12:00:00+02:00",
                datetime(2025, 7, 1, 10, 0, tzinfo=UTC),
            ),
            (
                "2025-03-09T03:30:00-05:00",
                datetime(2025, 3, 9, 8, 30, tzinfo=UTC),
            ),
            (
                "2025-11-02T01:30:00-04:00",
                datetime(2025, 11, 2, 5, 30, tzinfo=UTC),
            ),
            (date(2025, 6, 1), datetime(2025, 6, 1, tzinfo=UTC)),
            (
                datetime(2025, 6, 1, 9, tzinfo=timezone(timedelta(hours=9))),
                datetime(2025, 6, 1, 0, 0, tzinfo=UTC),
            ),
        ],
    )
    def test_valid_values_convert_to_utc(self, value, expected):
        parsed = _parse_lastmod_value(value, naive_policy="utc")
        assert parsed is not None
        assert parsed == expected
        assert parsed.tzinfo == UTC

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-date",
            "2025-02-30",
            "2025-1-1",
            "2025/06/01",
            "01-06-2025",
            "",
        ],
    )
    def test_invalid_strings_return_none(self, value):
        assert _parse_lastmod_value(value, naive_policy="utc") is None

    def test_naive_string_read_as_utc_by_default(self):
        parsed = _parse_lastmod_value("2025-06-15T12:00:00", naive_policy="utc")
        assert parsed == datetime(2025, 6, 15, 12, tzinfo=UTC)

    def test_naive_string_rejected(self):
        assert (
            _parse_lastmod_value("2025-06-15T12:00:00", naive_policy="reject") is None
        )

    def test_naive_datetime_read_as_utc_by_default(self):
        parsed = _parse_lastmod_value(datetime(2025, 6, 15, 12), naive_policy="utc")
        assert parsed == datetime(2025, 6, 15, 12, tzinfo=UTC)

    def test_naive_datetime_rejected(self):
        assert (
            _parse_lastmod_value(datetime(2025, 6, 15, 12), naive_policy="reject")
            is None
        )

    def test_bare_date_is_never_rejected_by_naive_policy(self):
        assert _parse_lastmod_value("2025-06-15", naive_policy="reject") == datetime(
            2025, 6, 15, tzinfo=UTC
        )

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError, match="str, date or datetime"):
            _parse_lastmod_value(123, naive_policy="utc")  # type: ignore[arg-type]

    def test_datetime_subclass_of_date_is_treated_as_datetime(self):
        value = datetime(2025, 6, 1, 0, 0, tzinfo=UTC)
        assert isinstance(value, date)
        assert _parse_lastmod_value(value, naive_policy="utc") == value

    def test_repeated_parsing_is_stable(self):
        value = "2025-11-02T01:30:00-05:00"
        first = _parse_lastmod_value(value, naive_policy="utc")
        for _ in range(5):
            assert _parse_lastmod_value(value, naive_policy="utc") == first
