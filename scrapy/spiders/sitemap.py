from __future__ import annotations

import logging
import re
import warnings

# Iterable is needed at the run time for the SitemapSpider._parse_sitemap() annotation
from collections.abc import AsyncIterator, Iterable, Sequence  # noqa: TC003
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal, Self, cast
from urllib.parse import urljoin

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

if TYPE_CHECKING:
    from scrapy.crawler import Crawler
    from scrapy.http.request import CallbackT
    from scrapy.robotstxt import RobotParser

logger = logging.getLogger(__name__)

# Lastmod values carry an explicit UTC offset or are dates; the only naive
# values accepted are interpreted as UTC, so the local time zone never matters.
_LastmodCutoffInput = str | date | datetime
_LastmodMissingPolicy = Literal["include", "exclude"]
_LastmodInvalidPolicy = Literal["include", "exclude"]
_LastmodNaivePolicy = Literal["utc", "reject"]
_LastmodBoundaryPolicy = Literal["include", "exclude"]
_LASTMOD_POLICIES = ("include", "exclude")
_LASTMOD_NAIVE_POLICIES = ("utc", "reject")
# A W3C date is exactly four-digit year, two-digit month and two-digit day.
_W3C_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


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

    sitemap_lastmod_cutoff: _LastmodCutoffInput | None = None
    """Only process sitemap entries whose ``lastmod`` is after this point in
    time.

    .. versionadded:: VERSION

    It accepts a :class:`datetime.datetime` (use a timezone-aware value), a
    :class:`datetime.date`, or a string in one of the `W3C Datetime
    <https://www.w3.org/TR/NOTE-datetime>`_ forms allowed for ``lastmod``:
    a date (``YYYY-MM-DD``) or a date and time, with an explicit time zone
    offset (``YYYY-MM-DDThh:mm:ss+TZD``, where ``TZD`` is ``Z`` or an offset
    such as ``+02:00``). Dates are compared as midnight UTC, and every other
    value is converted to UTC before comparing, so the local time zone is
    never used.

    When ``None``, the default, every entry is processed regardless of its
    ``lastmod``.

    The ``lastmod`` of a sitemap index entry only decides whether that
    sub-sitemap is traversed; it is not applied to the URLs inside it.

    Entries that lack a usable ``lastmod``, and entries whose ``lastmod``
    equals the cutoff, are handled by :attr:`sitemap_lastmod_missing`,
    :attr:`sitemap_lastmod_invalid`, :attr:`sitemap_lastmod_naive` and
    :attr:`sitemap_lastmod_boundary`.
    """

    sitemap_lastmod_missing: _LastmodMissingPolicy = "include"
    """What to do with entries that have no ``lastmod`` tag, or an empty one,
    when :attr:`sitemap_lastmod_cutoff` is set: ``"include"`` (the default)
    processes them, ``"exclude"`` skips them.

    .. versionadded:: VERSION
    """

    sitemap_lastmod_invalid: _LastmodInvalidPolicy = "include"
    """What to do with entries whose ``lastmod`` cannot be parsed when
    :attr:`sitemap_lastmod_cutoff` is set: ``"include"`` (the default)
    processes them, ``"exclude"`` skips them.

    .. versionadded:: VERSION
    """

    sitemap_lastmod_naive: _LastmodNaivePolicy = "utc"
    """How to read a ``lastmod`` without a time zone offset (other than a bare
    date) when :attr:`sitemap_lastmod_cutoff` is set: ``"utc"`` (the default)
    reads it as UTC, ``"reject"`` treats it like an invalid value and applies
    :attr:`sitemap_lastmod_invalid`.

    .. versionadded:: VERSION

    A bare date (``YYYY-MM-DD``) is always read as midnight UTC.
    """

    sitemap_lastmod_boundary: _LastmodBoundaryPolicy = "exclude"
    """What to do with entries whose ``lastmod`` equals
    :attr:`sitemap_lastmod_cutoff`: ``"exclude"`` (the default) only processes
    entries strictly after the cutoff, ``"include"`` also processes entries at
    the cutoff.

    .. versionadded:: VERSION
    """

    _max_size: int
    _warn_size: int
    _obey_robotstxt: bool
    _lastmod_cutoff: datetime | None

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
        self._lastmod_cutoff = self._build_lastmod_cutoff()

    def _build_lastmod_cutoff(self) -> datetime | None:
        """Validate the lastmod policy attributes and return the cutoff as a
        timezone-aware UTC datetime, or ``None`` when no cutoff is set."""
        for attr, choices in (
            ("sitemap_lastmod_missing", _LASTMOD_POLICIES),
            ("sitemap_lastmod_invalid", _LASTMOD_POLICIES),
            ("sitemap_lastmod_naive", _LASTMOD_NAIVE_POLICIES),
            ("sitemap_lastmod_boundary", _LASTMOD_POLICIES),
        ):
            value = getattr(self, attr)
            if value not in choices:
                msg = f"{attr} must be one of {', '.join(choices)!r}, got {value!r}."
                raise ValueError(msg)

        cutoff = self.sitemap_lastmod_cutoff
        if cutoff is None:
            return None
        try:
            parsed = _parse_lastmod_value(
                cutoff, naive_policy=self.sitemap_lastmod_naive
            )
        except (TypeError, ValueError) as exc:
            msg = f"Invalid sitemap_lastmod_cutoff value {cutoff!r}: {exc}"
            raise ValueError(msg) from exc
        if parsed is None:
            msg = f"Invalid sitemap_lastmod_cutoff value {cutoff!r}."
            raise ValueError(msg)
        return parsed

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
                    self.crawler.engine.crawl(
                        self.sitemap_request(
                            url,
                            self._parse_sitemap,
                            {"loc": url},
                            source="robotstxt",
                            response=response,
                        )
                    )
        else:
            self._robots_sitemaps[netloc] = urls

    async def start(self) -> AsyncIterator[Any]:
        sitemap_netlocs = {
            urlparse_cached(url).netloc
            for url in self.sitemap_urls
            if not url.endswith("/robots.txt")
        }
        for url in self.sitemap_urls:
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
                continue
            yield Request(url, self._parse_sitemap)

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

    def _filter_lastmod(
        self, entries: Iterable[dict[str, Any]]
    ) -> Iterable[dict[str, Any]]:
        """Yield the entries from *entries* accepted by the
        :attr:`sitemap_lastmod_cutoff` policy.

        Runs after :meth:`sitemap_filter` so an overriding filter keeps
        precedence, and only reads entries: neither the entry dicts nor the
        sitemap document are modified.
        """
        cutoff = self._lastmod_cutoff
        if cutoff is None:
            yield from entries
            return

        for entry in entries:
            lastmod = entry.get("lastmod")
            if not lastmod:
                if self.sitemap_lastmod_missing == "include":
                    yield entry
                continue

            parsed = _parse_lastmod_value(
                lastmod, naive_policy=self.sitemap_lastmod_naive
            )
            if parsed is None:
                if self.sitemap_lastmod_invalid == "include":
                    yield entry
                continue

            if self.sitemap_lastmod_boundary == "include":
                accepted = parsed >= cutoff
            else:
                accepted = parsed > cutoff
            if accepted:
                yield entry

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
            return (
                self.sitemap_request(
                    url,
                    self._parse_sitemap,
                    {"loc": url},
                    source="robotstxt",
                    response=response,
                )
                for url in urls
                if self._scheme_allowed(url, response.url)
                and any(x.search(url) for x in self._follow)
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

        if s.type == "sitemapindex":
            index_entries = list(
                self._get_urls_from_sitemapindex(
                    self._filter_lastmod(self.sitemap_filter(s)), response.url
                )
            )
            return (
                self.sitemap_request(
                    loc,
                    self._parse_sitemap,
                    entry,
                    source="sitemapindex",
                    response=response,
                )
                for loc, entry in index_entries
            )

        if s.type == "urlset":
            urlset_entries = list(
                self._get_urls_and_callbacks_from_urlset(
                    self._filter_lastmod(self.sitemap_filter(s)), response.url
                )
            )
            return (
                self.sitemap_request(loc, c, entry, source="urlset", response=response)
                for loc, c, entry in urlset_entries
            )

        logger.warning(
            "Ignoring invalid sitemap: %(response)s",
            {"response": response},
            extra={"spider": self},
        )

        return ()

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


def regex(x: re.Pattern[str] | str) -> re.Pattern[str]:
    if isinstance(x, str):
        return re.compile(x)
    return x


def _parse_lastmod_value(
    value: _LastmodCutoffInput, *, naive_policy: _LastmodNaivePolicy
) -> datetime | None:
    """Parse a ``lastmod`` value, or a configured cutoff, into a
    timezone-aware UTC :class:`~datetime.datetime`.

    Return ``None`` when *value* is a string that is not a valid W3C date or
    timezone-aware datetime. Raise :exc:`TypeError` or :exc:`ValueError` for
    other unsupported inputs, so that a bad cutoff fails while a bad entry can
    be handled by :attr:`SitemapSpider.sitemap_lastmod_invalid`.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        # A date covers a whole day and is read as its start in UTC.
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif isinstance(value, str):
        if _W3C_DATE_RE.fullmatch(value):
            try:
                day = date.fromisoformat(value)
            except ValueError:
                return None
            return datetime(day.year, day.month, day.day, tzinfo=UTC)
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        msg = f"expected a str, date or datetime, got {type(value).__name__}"
        raise TypeError(msg)

    if parsed.tzinfo is None:
        if naive_policy == "reject":
            return None
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
