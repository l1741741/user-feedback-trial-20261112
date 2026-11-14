from __future__ import annotations

import logging
import re
import warnings

# Iterable is needed at the run time for the SitemapSpider._parse_sitemap() annotation
from collections.abc import AsyncIterator, Iterable, Sequence  # noqa: TC003
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

# Reserved hreflang value that does not name a language:
# https://developers.google.com/search/docs/specialty/international/localized-versions
_X_DEFAULT = "x-default"

# Main syntax of a BCP 47 language tag, including private-use tags such as
# "x-pig-latin". x-default is handled separately as it is not a language.
_LANGUAGE_TAG_RE = re.compile(r"(?:[a-zA-Z]{2,8}|x)(?:-[a-zA-Z0-9]{1,8})*")

_LANGUAGE_MATCH_EXACT = "exact"
_LANGUAGE_MATCH_PRIMARY = "primary"
_LANGUAGE_MATCH_X_DEFAULT = _X_DEFAULT
_LANGUAGE_MATCH_LOC = "loc"


def _normalize_language_tag(tag: str) -> str | None:
    """Return the lowercased *tag* if it is a well-formed BCP 47 language
    tag, otherwise ``None``."""
    tag = tag.strip()
    if not _LANGUAGE_TAG_RE.fullmatch(tag):
        return None
    return tag.lower()


def _iter_language_candidates(
    entry: dict[str, Any],
) -> Iterable[tuple[str, str | None, str | None]]:
    """Yield ``(url, raw_label, normalized_label)`` for the loc URL and every
    alternate link of *entry*, in document order.

    The loc URL has no label. Alternate links without a usable hreflang label
    have a ``None`` normalized label and can never match a language
    preference."""
    yield entry["loc"], None, None
    alternate = entry.get("alternate", ())
    labels = entry.get("alternate_hreflang", ())
    for index, url in enumerate(alternate):
        label = labels[index] if index < len(labels) else None
        if label:
            normalized = _normalize_language_tag(label)
            if normalized is None:
                logger.debug(
                    f"Ignoring alternate link {url!r} with invalid hreflang "
                    f"label {label!r}"
                )
        else:
            normalized = None
        yield url, label, normalized


def _select_language_candidate(
    candidates: Sequence[tuple[str, str | None, str | None]],
    preferences: Sequence[str],
) -> tuple[str, str | None, str]:
    """Select one candidate following the configured language *preferences*.

    For every preference, in order, an exact (case-insensitive) hreflang
    match wins first, then a match on the primary language subtag, preferring
    regionless candidates (e.g. ``en`` over ``en-GB`` for preference
    ``en-US``). Only then is the next preference considered. If none match,
    the ``x-default`` alternate is used, and finally the entry loc URL.

    Ties within a tier are resolved by document order, so the result does not
    depend on the order of unrelated elements."""
    for preference in preferences:
        for url, label, normalized in candidates:
            if normalized == preference:
                return url, label, _LANGUAGE_MATCH_EXACT
        primary = preference.split("-", 1)[0]
        for url, label, normalized in candidates:
            if normalized == primary:
                return url, label, _LANGUAGE_MATCH_PRIMARY
        for url, label, normalized in candidates:
            if (
                normalized is not None
                and normalized != _X_DEFAULT
                and normalized.startswith(f"{primary}-")
            ):
                return url, label, _LANGUAGE_MATCH_PRIMARY
    for url, label, normalized in candidates:
        if normalized == _X_DEFAULT:
            return url, label, _LANGUAGE_MATCH_X_DEFAULT
    # The first candidate is always the loc URL, which has no label.
    return candidates[0][0], None, _LANGUAGE_MATCH_LOC


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

    sitemap_languages: str | Sequence[str] | None = None
    """Normalized language tags of the content to crawl, in order of
    preference.

    .. versionadded:: 2.21.0

    When set, each ``url`` block yields a single request, for the candidate
    whose hreflang best matches this list instead of every alternate URL. For
    every configured language, an exact match is considered first (matching is
    case-insensitive, but ``en`` and ``en-US`` are never treated as the same
    tag), then any tag with the same primary language subtag, preferring the
    regionless tag (``en`` before ``en-GB``). Only if no configured language
    matches is the ``x-default`` alternate used, and finally the ``loc`` URL.

    The label of the selected candidate and the way it was matched are kept on
    the request under the ``sitemap_language`` and
    ``sitemap_language_match`` meta keys. Identical URLs selected by several
    entries produce a single request. When this is set,
    :attr:`sitemap_alternate_links` is ignored for ``urlset`` entries.

    A single tag may be given as a string. Invalid tags and ``x-default`` are
    ignored with a warning; they never need to be configured because
    ``x-default`` is already the explicit fallback. When every configured tag
    is invalid, the ``x-default`` and ``loc`` fallbacks still apply.
    """

    sitemap_schemes: Sequence[str] | None = None
    """URL schemes that the URLs found in sitemaps and :file:`robots.txt` files
    may use.

    .. versionadded:: 2.20.0

    If ``None``, they may only use ``http``, ``https`` or the scheme of the
    sitemap or :file:`robots.txt` file they come from. Use ``()`` to follow
    URLs regardless of their scheme.
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
        self._language_preferences = self._get_language_preferences()

    def _get_language_preferences(self) -> Sequence[str] | None:
        """Validate and normalize :attr:`sitemap_languages`.

        Returns ``None`` when language selection is disabled, otherwise the
        normalized tags, which may be an empty sequence if none of the
        configured tags were usable."""
        if self.sitemap_languages is None:
            return None
        tags = (
            (self.sitemap_languages,)
            if isinstance(self.sitemap_languages, str)
            else tuple(self.sitemap_languages)
        )
        preferences: list[str] = []
        for tag in tags:
            if tag.strip().lower() == _X_DEFAULT:
                logger.warning(
                    f"Ignoring {tag!r} in sitemap_languages: x-default is "
                    "already used as the fallback of language selection."
                )
                continue
            normalized = _normalize_language_tag(tag)
            if normalized is None:
                logger.warning(
                    f"Ignoring invalid language tag {tag!r} in sitemap_languages."
                )
                continue
            if normalized not in preferences:
                preferences.append(normalized)
        return preferences

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
            (see :attr:`sitemap_alternate_links`), and their hreflang labels,
            aligned by index with ``alternate`` and ``None`` when missing, in
            a list with the key ``alternate_hreflang`` (see
            :attr:`sitemap_languages`)
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

        When :attr:`sitemap_languages` selects the URL, the returned request
        is also given the ``sitemap_language`` (the raw hreflang label of the
        selected candidate, or ``None`` for the unlabeled loc URL) and
        ``sitemap_language_match`` (``"exact"``, ``"primary"``,
        ``"x-default"`` or ``"loc"``) meta keys.
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
                self._get_urls_from_sitemapindex(self.sitemap_filter(s), response.url)
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
            if self._language_preferences is None:
                urlset_entries = list(
                    self._get_urls_and_callbacks_from_urlset(
                        self.sitemap_filter(s), response.url
                    )
                )
                return (
                    self.sitemap_request(
                        loc, c, entry, source="urlset", response=response
                    )
                    for loc, c, entry in urlset_entries
                )
            language_entries = list(
                self._get_language_urls_and_callbacks_from_urlset(
                    self.sitemap_filter(s), response.url
                )
            )
            return (
                self._language_sitemap_request(
                    loc,
                    c,
                    entry,
                    language,
                    language_match,
                    response=response,
                )
                for loc, c, entry, language, language_match in language_entries
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

    def _get_language_urls_and_callbacks_from_urlset(
        self, it: Iterable[dict[str, Any]], base_url: str
    ) -> Iterable[tuple[str, CallbackT, dict[str, Any], str | None, str]]:
        # Each entry yields at most one loc, and a loc selected again by a
        # later entry is not requested a second time.
        seen_locs: set[str] = set()
        assert self._language_preferences is not None
        for entry in it:
            if not entry.get("loc"):
                continue
            candidates = [
                (urljoin(base_url, url), label, normalized)
                for url, label, normalized in _iter_language_candidates(entry)
            ]
            loc, language, language_match = _select_language_candidate(
                candidates, self._language_preferences
            )
            if language_match != _LANGUAGE_MATCH_EXACT:
                logger.debug(
                    f"No exact language match for {entry['loc']!r}; selected "
                    f"{loc!r} ({language_match}: {language!r})."
                )
            if not self._scheme_allowed(loc, base_url) or loc in seen_locs:
                continue
            seen_locs.add(loc)
            for r, c in self._cbs:
                if r.search(loc):
                    yield loc, c, entry, language, language_match
                    break

    def _language_sitemap_request(
        self,
        loc: str,
        callback: CallbackT,
        entry: dict[str, Any],
        language: str | None,
        language_match: str,
        *,
        response: Response,
    ) -> Request:
        """Build the request for a language-selected *loc* and attach the
        selected language metadata without changing the entry."""
        request = self.sitemap_request(
            loc, callback, entry, source="urlset", response=response
        )
        request.meta.setdefault("sitemap_language", language)
        request.meta.setdefault("sitemap_language_match", language_match)
        return request

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
