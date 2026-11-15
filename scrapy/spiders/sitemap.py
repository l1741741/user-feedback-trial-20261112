from __future__ import annotations

import logging
import re
import warnings

# Iterable is needed at the run time for the SitemapSpider._parse_sitemap() annotation
from collections.abc import AsyncIterator, Iterable, Sequence  # noqa: TC003
from typing import TYPE_CHECKING, Any, Literal, Self, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

from w3lib.url import safe_url_string

from scrapy import signals
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import Request, Response, XmlResponse
from scrapy.responsetypes import responsetypes
from scrapy.spiders import Spider
from scrapy.utils._compression import (
    _DecompressionMaxSizeExceeded,
    gunzip,
    gzip_magic_number,
)
from scrapy.utils._sitemap import Sitemap, sitemap_urls_from_robots
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.url import strip_url

if TYPE_CHECKING:
    from scrapy.crawler import Crawler
    from scrapy.http.request import CallbackT
    from scrapy.robotstxt import RobotParser

logger = logging.getLogger(__name__)

# Request meta key holding the sitemap index depth of a request. Root sitemap
# requests do not set it, and are treated as depth 0.
_DEPTH_META_KEY = "_sitemap_index_depth"


class SitemapSpider(Spider):
    """Spider that crawls a site by discovering its URLs using `sitemaps
    <https://www.sitemaps.org/index.html>`_.

    It supports nested sitemaps and discovering sitemap URLs from `robots.txt
    <https://www.robotstxt.org/>`_.
    """

    sitemap_urls: Sequence[str] = ()
    """URLs pointing to the sitemaps whose URLs you want to crawl.

    You can also point to a `robots.txt <https://www.robotstxt.org/>`_ and it
    will be parsed to extract sitemap URLs from it.
    """

    sitemap_rules: Sequence[tuple[re.Pattern[str] | str, str | CallbackT]] = [
        ("", "parse")
    ]
    """``(regex, callback)`` tuples where:

    -   ``regex`` is a regular expression to match URLs extracted from
        sitemaps. ``regex`` can be either a str or a compiled regex object.

    -   ``callback`` is the callback to use for processing the URLs that match
        the regular expression. ``callback`` can be a string (indicating the
        name of a spider method) or a callable.

    For example:

    .. code-block:: python

        sitemap_rules = [("/product/", "parse_product")]

    Rules are applied in order, and only the first one that matches will be
    used.

    The default value makes all URLs found in sitemaps be processed with the
    :meth:`~scrapy.Spider.parse` callback.
    """

    sitemap_follow: Sequence[re.Pattern[str] | str] = [""]
    """Regexes of sitemap URLs that should be followed: those linked from
    `sitemap index files <https://www.sitemaps.org/protocol.html#index>`_, and
    those declared through the ``Sitemap`` directive of a :file:`robots.txt`
    file.

    By default, all sitemaps are followed.
    """

    sitemap_alternate_links: bool = False
    """Specifies if alternate links for one ``url`` should be followed. These are
    links for the same website in another language passed within the same
    ``url`` block.

    For example:

    .. code-block:: xml

        <url>
            <loc>http://example.com/</loc>
            <xhtml:link rel="alternate" hreflang="de" href="http://example.com/de"/>
        </url>

    When enabled, this would retrieve both URLs. When disabled, only
    ``http://example.com/`` would be retrieved.
    """

    sitemap_schemes: Sequence[str] | None = None
    """URL schemes that the URLs found in sitemaps and :file:`robots.txt` files
    may use.

    .. versionadded:: 2.20.0

    If ``None``, they may only use ``http``, ``https`` or the scheme of the
    sitemap or :file:`robots.txt` file they come from. Use ``()`` to follow
    URLs regardless of their scheme.
    """

    sitemap_max_depth: int | None = None
    """Maximum depth of the sitemaps to download.

    .. versionadded:: VERSION

    The URLs of :attr:`sitemap_urls` and the sitemaps declared in
    :file:`robots.txt` files are at depth 0. Each sitemap linked from a
    `sitemap index file
    <https://www.sitemaps.org/protocol.html#index>`_ is at the depth of the
    index plus 1.

    A sitemap at the maximum depth is still downloaded and parsed. Only the
    sitemaps that it links to, which would be one level deeper, are ignored,
    along with everything below them in the same branch. Other branches are
    not affected.

    Only sitemap URLs count towards the depth: the page URLs that sitemaps
    list are crawled no matter how deep the sitemap that lists them is.

    ``None`` means no limit. Other values must be non-negative integers, or
    the spider raises an exception at initialization time.
    """

    sitemap_deduplicate: bool = False
    """Whether to avoid downloading the same sitemap more than once.

    .. versionadded:: VERSION

    When enabled, a sitemap URL is registered right before its request is
    returned, keyed by its absolute URL without the fragment. Relative URLs
    are resolved against the response they come from first. The query string
    is compared as is, and URLs of different schemes are never considered the
    same.

    A sitemap whose URL is already registered is not downloaded again, which
    prevents repeated sitemap indexes from being scheduled, e.g. when
    :file:`robots.txt` and a sitemap index both list the same sitemap, or when
    sitemap indexes reference each other in a cycle. Only sitemap URLs are
    registered: the page URLs that sitemaps list are always scheduled.

    Registration happens after :meth:`sitemap_request` returns, so a failing
    override never prevents a later attempt from scheduling the same URL.

    The registration is kept per spider instance, and sitemap URLs requested
    with :attr:`sitemap_urls`, discovered in :file:`robots.txt` files, or
    discovered through the :signal:`robots_parsed` signal share it.
    """

    _max_size: int
    _warn_size: int
    _obey_robotstxt: bool

    @classmethod
    def from_crawler(cls, crawler: Crawler, *args: Any, **kwargs: Any) -> Self:
        spider = super().from_crawler(crawler, *args, **kwargs)
        spider._max_size = getattr(
            spider, "download_maxsize", spider.settings.getint("DOWNLOAD_MAXSIZE")
        )
        spider._warn_size = getattr(
            spider, "download_warnsize", spider.settings.getint("DOWNLOAD_WARNSIZE")
        )
        spider._obey_robotstxt = crawler.settings.getbool("ROBOTSTXT_OBEY")
        if spider._obey_robotstxt:
            crawler.signals.connect(spider._robots_parsed, signals.robots_parsed)
        return spider

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._cbs: list[tuple[re.Pattern[str], CallbackT]] = []
        for r, c in self.sitemap_rules:
            if isinstance(c, str):
                c = cast("CallbackT", getattr(self, c))  # noqa: PLW2901
            self._cbs.append((regex(r), c))
        self._follow: list[re.Pattern[str]] = [regex(x) for x in self.sitemap_follow]
        self._robots_sitemaps: dict[str, list[str]] = {}
        self._signal_only_netlocs: set[str] = set()
        self._max_depth = _validate_max_depth(self.sitemap_max_depth)
        self._deduplicate = self.sitemap_deduplicate
        self._visited_sitemap_urls: set[str] = set()

    def _sitemap_url_key(self, url: str) -> str | None:
        """Return the key that identifies the sitemap at *url*, or ``None``
        when *url* cannot be parsed.

        The key is the absolute URL without its fragment, normalized the same
        way as the URL of the request built from it, so that two spellings of
        the same URL get the same key.

        Every other component is kept as is. In particular, the order and the
        values of the query parameters are preserved, and the scheme is, so
        that the same path over ``http`` and ``https`` gets different keys.
        """
        try:
            normalized: str = safe_url_string(url)
            parts = urlsplit(normalized)
        except ValueError:
            return None
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))

    def _log_url(self, url: str) -> str:
        """Return *url* without credentials or fragment, for logging."""
        try:
            return strip_url(url)
        except ValueError:
            return "<invalid URL>"

    def _already_requested(self, url: str) -> bool:
        if not self._deduplicate:
            return False
        key = self._sitemap_url_key(url)
        if key is None or key not in self._visited_sitemap_urls:
            return False
        logger.debug(
            f"Ignoring sitemap, already requested: {self._log_url(url)}",
            extra={"spider": self},
        )
        return True

    def _mark_requested(self, url: str) -> None:
        if not self._deduplicate:
            return
        key = self._sitemap_url_key(url)
        if key is not None:
            self._visited_sitemap_urls.add(key)

    def _get_sitemap_request(
        self,
        loc: str,
        entry: dict[str, Any],
        *,
        source: Literal["robotstxt", "sitemapindex"],
        response: Response,
        depth: int,
    ) -> Request | None:
        """Return the request for the sitemap *loc*, or ``None`` when *loc* is
        deeper than :attr:`sitemap_max_depth` or was already requested.

        The URL is only registered once :meth:`sitemap_request` has returned,
        so an exception raised by an override does not prevent the same URL
        from being requested later, e.g. from another entry of the same
        sitemap.

        The depth of a sitemap linked from an index is recorded on a copy of
        the request returned by :meth:`sitemap_request`, so that neither
        *entry*, *response* nor request metadata shared between requests is
        changed. Root sitemaps are at the default depth, and keep their
        request as returned. Request metadata is only touched when
        :attr:`sitemap_max_depth` sets a limit.
        """
        if self._max_depth is not None and depth > self._max_depth:
            logger.debug(
                f"Ignoring sitemap at depth {depth}, above "
                f"sitemap_max_depth={self._max_depth}: {self._log_url(loc)}",
                extra={"spider": self},
            )
            return None
        if self._already_requested(loc):
            return None
        request = self.sitemap_request(
            loc, self._parse_sitemap, entry, source=source, response=response
        )
        if self._max_depth is not None and depth:
            request = request.replace(
                meta={**request.meta, _DEPTH_META_KEY: depth},
                parent_id=request.parent_id,
            )
        self._mark_requested(loc)
        return request

    def _iter_sitemap_requests(
        self,
        entries: Iterable[tuple[str, dict[str, Any]]],
        *,
        source: Literal["robotstxt", "sitemapindex"],
        response: Response,
        depth: int,
    ) -> Iterable[Request]:
        for loc, entry in entries:
            request = self._get_sitemap_request(
                loc, entry, source=source, response=response, depth=depth
            )
            if request is not None:
                yield request

    def _response_sitemap_depth(self, response: Response) -> int:
        """Return the depth of the sitemap that *response* contains."""
        if response.request is None:
            return 0
        return cast("int", response.request.meta.get(_DEPTH_META_KEY, 0))

    def _robots_parsed(
        self, robotparser: RobotParser, request: Request, response: Response
    ) -> None:
        netloc = urlparse_cached(request).netloc
        urls = [urljoin(request.url, url) for url in robotparser.sitemaps()]
        if netloc in self._signal_only_netlocs:
            # No request for this host's robots.txt was sent from start(), so
            # _parse_sitemap() will never run for it; schedule its sitemaps
            # directly instead of stashing them for a response that never arrives.
            self._signal_only_netlocs.discard(netloc)
            for url in urls:
                if self._scheme_allowed(url, response.url) and any(
                    x.search(url) for x in self._follow
                ):
                    sitemap_request = self._get_sitemap_request(
                        url,
                        {"loc": url},
                        source="robotstxt",
                        response=response,
                        depth=0,
                    )
                    if sitemap_request is not None:
                        self.crawler.engine.crawl(sitemap_request)
        else:
            self._robots_sitemaps[netloc] = urls

    async def start(self) -> AsyncIterator[Any]:
        sitemap_netlocs = {
            urlparse_cached(url).netloc
            for url in self.sitemap_urls
            if not url.endswith("/robots.txt")
        }
        for url in self.sitemap_urls:
            if self._already_requested(url):
                continue
            netloc = urlparse_cached(url).netloc
            if (
                self._obey_robotstxt
                and url.endswith("/robots.txt")
                and netloc in sitemap_netlocs
            ):
                # RobotsTxtMiddleware will fetch and parse this robots.txt
                # anyway, to check the other sitemap_urls requests to the same
                # host, so rely on the robots_parsed signal for it instead of
                # requesting it a second time here.
                self._signal_only_netlocs.add(netloc)
                self._mark_requested(url)
                continue
            request = Request(url, self._parse_sitemap)
            self._mark_requested(url)
            yield request

    def sitemap_filter(
        self, entries: Iterable[dict[str, Any]]
    ) -> Iterable[dict[str, Any]]:
        """Yield the sitemap entries from *entries* that should be processed.

        Override it to select sitemap entries based on their attributes. For
        example, given the following sitemap entry:

        .. code-block:: xml

            <url>
                <loc>http://example.com/</loc>
                <lastmod>2005-01-01</lastmod>
            </url>

        You can filter entries by date as follows:

        .. code-block:: python

            from datetime import datetime
            from scrapy.spiders import SitemapSpider


            class FilteredSitemapSpider(SitemapSpider):
                name = "filtered_sitemap_spider"
                allowed_domains = ["example.com"]
                sitemap_urls = ["http://example.com/sitemap.xml"]

                def sitemap_filter(self, entries):
                    for entry in entries:
                        date_time = datetime.strptime(entry["lastmod"], "%Y-%m-%d")
                        if date_time.year >= 2005:
                            yield entry

        This would retrieve only entries modified on 2005 and the following
        years.

        Entries are dict objects extracted from the sitemap document. Usually,
        the key is the tag name and the value is the text inside it.

        It's important to notice that:

        -   as the ``loc`` attribute is required, entries without this tag are
            discarded
        -   alternate links are stored in a list with the key ``alternate``
            (see :attr:`sitemap_alternate_links`)
        -   namespaces are removed, so lxml tags named as ``{namespace}tagname``
            become only ``tagname``

        The default implementation yields all entries, observing other
        attributes and their settings.
        """
        yield from entries

    def sitemap_request(
        self,
        loc: str,
        callback: CallbackT,
        entry: dict[str, Any],
        *,
        source: Literal["robotstxt", "sitemapindex", "urlset"],
        response: Response,
    ) -> Request:
        """Return the request for the *loc* URL of the sitemap *entry*, to be
        parsed with *callback*.

        .. versionadded:: 2.20.0

        Override this method to build those requests differently, e.g. to
        carry sitemap data and request metadata over to *callback*:

        .. code-block:: python

            def sitemap_request(self, loc, callback, entry, *, source, response):
                meta = {
                    "lastmod": entry.get("lastmod"),
                    "site": response.meta.get("site"),
                }
                return Request(loc, callback, meta=meta)

        *entry* is the sitemap entry that *loc* comes from, as described in
        :meth:`sitemap_filter`. When :attr:`sitemap_alternate_links` is
        enabled, the same entry is used for every alternate URL.

        *source* tells where *loc* comes from: ``"urlset"`` for a URL to
        crawl, ``"sitemapindex"`` for a sitemap linked from a sitemap index,
        and ``"robotstxt"`` for a sitemap linked from a :file:`robots.txt`
        file, where *entry* only has ``loc``.

        *response* is the response that *entry* was read from.
        """
        return Request(loc, callback=callback)

    def _parse_sitemap(self, response: Response) -> Iterable[Request]:
        if response.url.endswith("/robots.txt"):
            urls = list(sitemap_urls_from_robots(response.body, base_url=response.url))
            netloc = urlparse_cached(response).netloc
            for url in self._robots_sitemaps.pop(netloc, ()):
                if url not in urls:
                    urls.append(url)
            robots_entries = (
                (url, {"loc": url})
                for url in urls
                if self._scheme_allowed(url, response.url)
                and any(x.search(url) for x in self._follow)
            )
            return self._iter_sitemap_requests(
                robots_entries, source="robotstxt", response=response, depth=0
            )

        body = self._get_sitemap_body(response)
        if not body:
            logger.warning(
                "Ignoring invalid sitemap: %(response)s",
                {"response": response},
                extra={"spider": self},
            )
            return ()

        s = Sitemap(body)

        if s.type not in ("sitemapindex", "urlset"):
            logger.warning(
                "Ignoring invalid sitemap: %(response)s",
                {"response": response},
                extra={"spider": self},
            )
            return ()

        # A redirect may have made the sitemap available under a URL other
        # than the one it was requested with. Register that final URL too, so
        # that an entry pointing back to this sitemap through it does not make
        # the spider download the sitemap again.
        self._mark_requested(response.url)

        if s.type == "sitemapindex":
            depth = self._response_sitemap_depth(response) + 1
            index_entries = list(
                self._get_urls_from_sitemapindex(self.sitemap_filter(s), response.url)
            )
            return self._iter_sitemap_requests(
                index_entries,
                source="sitemapindex",
                response=response,
                depth=depth,
            )

        urlset_entries = list(
            self._get_urls_and_callbacks_from_urlset(
                self.sitemap_filter(s), response.url
            )
        )
        return (
            self.sitemap_request(loc, c, entry, source="urlset", response=response)
            for loc, c, entry in urlset_entries
        )

    def _get_urls_from_sitemapindex(
        self, it: Iterable[dict[str, Any]], base_url: str
    ) -> Iterable[tuple[str, dict[str, Any]]]:
        for loc, entry in _iterlocs(it, self.sitemap_alternate_links):
            loc = urljoin(base_url, loc)  # noqa: PLW2901
            if self._scheme_allowed(loc, base_url) and any(
                x.search(loc) for x in self._follow
            ):
                yield loc, entry

    def _get_urls_and_callbacks_from_urlset(
        self, it: Iterable[dict[str, Any]], base_url: str
    ) -> Iterable[tuple[str, CallbackT, dict[str, Any]]]:
        for loc, entry in _iterlocs(it, self.sitemap_alternate_links):
            loc = urljoin(base_url, loc)  # noqa: PLW2901
            if not self._scheme_allowed(loc, base_url):
                continue
            for r, c in self._cbs:
                if r.search(loc):
                    yield loc, c, entry
                    break

    def _scheme_allowed(self, url: str, source_url: str) -> bool:
        if self.sitemap_schemes is None:
            schemes = {"http", "https", urlparse_cached(source_url).scheme}
        else:
            schemes = set(self.sitemap_schemes)
        return not schemes or urlparse_cached(url).scheme in schemes

    def _get_sitemap_body(self, response: Response) -> bytes | None:
        """Return the sitemap body contained in the given response,
        or None if the response is not a sitemap.
        """
        # Download handlers other than the built-in ones may use their own
        # response classes, so the content type, the URL and the body are taken
        # into account as well.
        if (
            isinstance(response, XmlResponse)
            or responsetypes.from_args(
                headers=response.headers, url=response.url, body=response.body
            )
            is XmlResponse
        ):
            return response.body
        if gzip_magic_number(response):
            uncompressed_size = len(response.body)
            max_size = response.meta.get("download_maxsize", self._max_size)
            # circular import
            from scrapy.utils._download_handlers import get_warnsize  # noqa: PLC0415

            warn_size = get_warnsize(response.meta, self._warn_size)
            try:
                body = gunzip(response.body, max_size=max_size)
            except _DecompressionMaxSizeExceeded:
                return None
            if uncompressed_size < warn_size <= len(body):
                logger.warning(
                    f"{response} body size after decompression ({len(body)} B) "
                    f"is larger than the download warning size ({warn_size} B)."
                )
            return body
        # actual gzipped sitemap files are decompressed above ;
        # if we are here (response body is not gzipped)
        # and have a response for .xml.gz,
        # it usually means that it was already gunzipped
        # by HttpCompression middleware,
        # the HTTP response being sent with "Content-Encoding: gzip"
        # without actually being a .xml.gz file in the first place,
        # merely XML gzip-compressed on the fly,
        # in other word, here, we have plain XML
        if response.url.endswith(".xml") or response.url.endswith(".xml.gz"):
            return response.body
        return None


def _validate_max_depth(value: object) -> int | None:
    if value is None:
        return None
    # bool is a subclass of int, but True and False are not useful depths.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"sitemap_max_depth must be a non-negative integer or None, got {value!r}"
        )
    if value < 0:
        raise ValueError(
            f"sitemap_max_depth must be a non-negative integer or None, got {value}"
        )
    return value


def regex(x: re.Pattern[str] | str) -> re.Pattern[str]:
    if isinstance(x, str):
        return re.compile(x)
    return x


def iterloc(it: Iterable[dict[str, Any]], alt: bool = False) -> Iterable[str]:
    warnings.warn(
        "scrapy.spiders.sitemap.iterloc() is deprecated.",
        ScrapyDeprecationWarning,
        stacklevel=2,
    )
    return (loc for loc, _ in _iterlocs(it, alt))


def _iterlocs(
    it: Iterable[dict[str, Any]], alt: bool = False
) -> Iterable[tuple[str, dict[str, Any]]]:
    for d in it:
        if loc := d["loc"]:
            yield loc, d

        # Also consider alternate URLs (xhtml:link rel="alternate")
        if alt and (alt_list := d.get("alternate")):
            for loc in alt_list:
                yield loc, d
