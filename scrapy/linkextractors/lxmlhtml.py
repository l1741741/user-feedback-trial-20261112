"""
Link extractor based on lxml.html
"""

from __future__ import annotations

import logging
import operator
import re
from collections.abc import Callable, Iterable
from functools import partial
from typing import TYPE_CHECKING, Any, TypeAlias, cast
from urllib.parse import urljoin

from lxml import etree
from parsel.csstranslator import HTMLTranslator
from w3lib.html import strip_html5_whitespace
from w3lib.url import canonicalize_url, safe_url_string

from scrapy.link import Link
from scrapy.linkextractors import IGNORED_EXTENSIONS, _matches
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import arg_to_iter, rel_has_nofollow
from scrapy.utils.python import unique as unique_list
from scrapy.utils.url import url_is_from_any_domain

if TYPE_CHECKING:
    from lxml.html import HtmlElement

    from scrapy import Selector
    from scrapy.http import TextResponse


logger = logging.getLogger(__name__)

# from lxml/src/lxml/html/__init__.py
XHTML_NAMESPACE = "http://www.w3.org/1999/xhtml"

_collect_string_content = etree.XPath("string()")

# A valid width descriptor is a positive integer followed by "w".
_width_descriptor_re = re.compile(r"^\d+w$")
# A valid pixel density descriptor is a non-negative floating-point number
# followed by "x".
_density_descriptor_re = re.compile(r"^(?:\d+\.?\d*|\.\d+)x$")

# HTML attributes separate tokens with ASCII whitespace only.
_ascii_whitespace = frozenset(" \t\n\f\r")

# Default bounds that keep srcset parsing linear even on hostile markup.
_DEFAULT_SRCSET_MAX_CANDIDATES = 100
_DEFAULT_SRCSET_MAX_CHARS = 100_000
_SRCSET_ON_ERROR_SKIP = "skip"
_SRCSET_ON_ERROR_REJECT = "reject"


def _is_ascii_whitespace(char: str) -> bool:
    return char in _ascii_whitespace


def _nons(tag: Any) -> Any:
    if (
        isinstance(tag, str)
        and tag[0] == "{"
        and tag[1 : len(XHTML_NAMESPACE) + 1] == XHTML_NAMESPACE
    ):
        return tag.split("}")[-1]
    return tag


def _identity(x: Any) -> Any:
    return x


def _canonicalize_link_url(link: Link) -> str:
    return canonicalize_url(link.url, keep_fragments=True)


def _has_extension(path: str, extensions: set[str]) -> bool:
    """Return whether *path* ends with any of *extensions*, which all start
    with a dot and are lowercase, ignoring case.
    """
    dot = path.rfind(".")
    while dot != -1:
        if path[dot:].lower() in extensions:
            return True
        dot = path.rfind(".", 0, dot)
    return False


def _name_matches(allowed: set[str], denied: set[str], name: str) -> bool:
    """Return whether a tag or attribute *name* should be considered.

    A name matches when it is allowed and not denied. A name is allowed if it is
    listed in *allowed*, or if *allowed* contains the ``"*"`` wildcard, which
    matches every name. *denied* has precedence over *allowed*.
    """
    if name in denied:
        return False
    return "*" in allowed or name in allowed


def _tokenize_srcset(
    attr_value: str,
) -> list[tuple[str, list[str]]]:
    """Split an HTML ``srcset`` value into ``(url, descriptors)`` pairs.

    Tokenizing follows the HTML parsing model instead of splitting on commas:
    while a URL is being read every comma belongs to it (data URLs contain
    their own commas), and a comma separates candidates only once the
    descriptors section after the URL has started.
    """
    raw_candidates: list[tuple[str, list[str]]] = []
    url_parts: list[str] = []
    descriptors: list[str] = []
    token_parts: list[str] = []
    in_url = True
    url_started = False

    def finish_candidate() -> None:
        url = "".join(url_parts)
        if url or descriptors:
            raw_candidates.append((url, descriptors))

    for char in attr_value:
        if in_url:
            if _is_ascii_whitespace(char):
                # Leading whitespace is padding; whitespace after the URL
                # marks the beginning of the descriptors section.
                if url_started:
                    in_url = False
            else:
                url_parts.append(char)
                url_started = True
            continue
        if _is_ascii_whitespace(char):
            if token_parts:
                descriptors.append("".join(token_parts))
                token_parts = []
            continue
        if char == ",":
            if token_parts:
                descriptors.append("".join(token_parts))
                token_parts = []
            finish_candidate()
            url_parts = []
            descriptors = []
            in_url = True
            url_started = False
            continue
        token_parts.append(char)
    if token_parts:
        descriptors.append("".join(token_parts))
    finish_candidate()
    return raw_candidates


def _classify_descriptor(descriptor: str) -> str:
    """Return ``"width"``, ``"density"`` or ``""`` for an invalid token."""
    if _width_descriptor_re.fullmatch(descriptor) and any(
        char != "0" for char in descriptor[:-1]
    ):
        return "width"
    if _density_descriptor_re.fullmatch(descriptor):
        return "density"
    return ""


def _parse_srcset(
    attr_value: str,
    *,
    on_error: str,
    max_candidates: int,
    max_chars: int,
) -> list[tuple[str, str]] | None:
    """Parse an HTML ``srcset`` attribute into ``(url, descriptor)`` pairs.

    Each candidate is either a bare URL or a URL followed by exactly one width
    (``480w``) or pixel density (``2x``) descriptor. Duplicate descriptors and
    candidates mixing both descriptor kinds are treated as conflicts; see
    :func:`_tokenize_srcset` for why a plain comma split is not used.

    With ``on_error="skip"`` invalid candidates are dropped and the remaining
    ones are returned; with ``on_error="reject"`` any invalid candidate makes
    the whole attribute fail and ``None`` is returned. Attributes longer than
    *max_chars* and candidate lists longer than *max_candidates* are also
    rejected, so parsing stays bounded.
    """
    if len(attr_value) > max_chars:
        logger.warning(
            f"Rejecting srcset attribute of {len(attr_value)} characters, "
            f"above the limit of {max_chars}"
        )
        return None

    candidates: list[tuple[str, str]] = []
    seen_widths: set[str] = set()
    seen_densities: set[str] = set()
    has_width = False
    has_density = False
    for url, raw_descriptors in _tokenize_srcset(attr_value):
        descriptor = raw_descriptors[0] if raw_descriptors else ""
        descriptor_kind = (
            _classify_descriptor(descriptor) if len(raw_descriptors) == 1 else ""
        )
        valid = bool(url) and (not raw_descriptors or descriptor_kind != "")
        if valid and descriptor_kind == "width":
            valid = descriptor not in seen_widths and not has_density
        elif valid and descriptor_kind == "density":
            valid = descriptor not in seen_densities and not has_width

        if not valid:
            if on_error == _SRCSET_ON_ERROR_REJECT:
                logger.warning(
                    f"Rejecting srcset attribute containing invalid candidate "
                    f"with URL {url!r}"
                )
                return None
            logger.debug(f"Skipping invalid srcset candidate with URL {url!r}")
            continue

        if descriptor_kind == "width":
            seen_widths.add(descriptor)
            has_width = True
        elif descriptor_kind == "density":
            seen_densities.add(descriptor)
            has_density = True
        candidates.append((url, descriptor))
        if len(candidates) >= max_candidates:
            logger.warning(
                f"Stopping srcset parsing after {max_candidates} candidates, "
                f"the configured limit"
            )
            break
    return candidates


class LxmlParserLinkExtractor:
    def __init__(
        self,
        tag: str | Callable[[str], bool] = "a",
        attr: str | Callable[[str], bool] = "href",
        process: Callable[[Any], Any] | None = None,
        unique: bool = False,
        strip: bool = True,
        canonicalized: bool = False,
        *,
        srcset_attrs: Iterable[str] = (),
        srcset_on_error: str = _SRCSET_ON_ERROR_SKIP,
        srcset_max_candidates: int = _DEFAULT_SRCSET_MAX_CANDIDATES,
        srcset_max_chars: int = _DEFAULT_SRCSET_MAX_CHARS,
    ):
        # mypy doesn't infer types for operator.* and also for partial()
        self.scan_tag: Callable[[str], bool] = (
            tag
            if callable(tag)
            else cast("Callable[[str], bool]", partial(operator.eq, tag))
        )
        self.scan_attr: Callable[[str], bool] = (
            attr
            if callable(attr)
            else cast("Callable[[str], bool]", partial(operator.eq, attr))
        )
        self.process_attr: Callable[[Any], Any] = (
            process if callable(process) else _identity
        )
        self.unique: bool = unique
        self.strip: bool = strip
        self.link_key: Callable[[Link], str] = (
            cast("Callable[[Link], str]", operator.attrgetter("url"))
            if canonicalized
            else _canonicalize_link_url
        )
        self.srcset_attrs: frozenset[str] = frozenset(srcset_attrs)
        self.srcset_on_error: str = srcset_on_error
        self.srcset_max_candidates: int = srcset_max_candidates
        self.srcset_max_chars: int = srcset_max_chars

    def _build_link(
        self,
        el: HtmlElement,
        attr_val: str,
        response_url: str,
        response_encoding: str,
        base_url: str,
        source_attr: str | None = None,
    ) -> Link | None:
        """Resolve and normalize one attribute value into a Link."""
        # pseudo lxml.html.HtmlElement.make_links_absolute(base_url)
        try:
            if self.strip:
                attr_val = strip_html5_whitespace(attr_val)
            attr_val = urljoin(base_url, attr_val)
        except ValueError:
            return None  # skipping bogus links
        url = self.process_attr(attr_val)
        if url is None:
            return None
        try:
            url = safe_url_string(url, encoding=response_encoding)
        except ValueError:
            logger.debug(f"Skipping extraction of link with bad URL {url!r}")
            return None

        # to fix relative links after process_value
        url = urljoin(response_url, url)
        return Link(
            url,
            _collect_string_content(el) or "",
            nofollow=rel_has_nofollow(el.get("rel")),
            source_attr=source_attr,
        )

    def _extract_links(
        self,
        selector: Selector,
        response_url: str,
        response_encoding: str,
        base_url: str,
    ) -> list[Link]:
        links: list[Link] = []
        # hacky way to get the underlying lxml parsed document
        for el in selector.root.iter(etree.Element):
            if not self.scan_tag(_nons(el.tag)):
                continue
            for attr_name, attr_val in el.attrib.items():
                if attr_name in self.srcset_attrs:
                    candidates = _parse_srcset(
                        attr_val,
                        on_error=self.srcset_on_error,
                        max_candidates=self.srcset_max_candidates,
                        max_chars=self.srcset_max_chars,
                    )
                    if candidates is None:
                        continue
                    for candidate_url, _descriptor in candidates:
                        link = self._build_link(
                            el,
                            candidate_url,
                            response_url,
                            response_encoding,
                            base_url,
                            source_attr=attr_name,
                        )
                        if link is not None:
                            links.append(link)
                    continue
                if not self.scan_attr(attr_name):
                    continue
                link = self._build_link(
                    el,
                    attr_val,
                    response_url,
                    response_encoding,
                    base_url,
                )
                if link is not None:
                    links.append(link)
        return self._deduplicate_if_needed(links)

    def extract_links(self, response: TextResponse) -> list[Link]:
        base_url = response.base_url
        return self._extract_links(
            response.selector, response.url, response.encoding, base_url
        )

    def _process_links(self, links: list[Link]) -> list[Link]:
        """Normalize and filter extracted links

        The subclass should override it if necessary
        """
        return self._deduplicate_if_needed(links)

    def _deduplicate_if_needed(self, links: list[Link]) -> list[Link]:
        if self.unique:
            return unique_list(links, key=self.link_key)
        return links


_Regex: TypeAlias = str | re.Pattern[str]
_RegexOrSeveral: TypeAlias = _Regex | Iterable[_Regex]


class LxmlLinkExtractor:
    r"""LxmlLinkExtractor is the recommended link extractor with handy filtering
    options. It is implemented using lxml's robust HTMLParser.

    :param allow: a single regular expression (or list of regular expressions)
        that the (absolute) URLs must match in order to be extracted. If not
        given (or empty), it will match all links.
    :type allow: str or list

    :param deny: a single regular expression (or list of regular expressions)
        that the (absolute) URLs must match in order to be excluded (i.e. not
        extracted). It has precedence over the ``allow`` parameter. If not
        given (or empty), it won't exclude any links.
    :type deny: str or list

    :param allow_domains: a single value or a list of strings containing
        domains that will be considered for extracting the links.
    :type allow_domains: str or list

    :param deny_domains: a single value or a list of strings containing
        domains that won't be considered for extracting the links.
    :type deny_domains: str or list

    :param deny_extensions: a single value or list of strings containing
        extensions that should be ignored when extracting links.
        If not given, it will default to
        :data:`scrapy.linkextractors.IGNORED_EXTENSIONS`.
    :type deny_extensions: list

    :param schemes: a single value or list of strings containing the URL
        schemes that extracted links may use. If not given, links may only use
        ``http``, ``https`` or the scheme of the response. Use ``()`` to
        extract links regardless of their scheme.

        .. versionadded:: 2.20.0
    :type schemes: str or list

    :param restrict_xpaths: an XPath (or list of XPaths) that defines regions
        inside the response where links should be extracted. If given, only the
        text selected by those XPaths will be scanned for links.
    :type restrict_xpaths: str or list

    :param restrict_css: a CSS selector (or list of selectors) that defines
        regions inside the response where links should be extracted. It has the
        same behaviour as ``restrict_xpaths``.
    :type restrict_css: str or list

    :param restrict_text: a single regular expression (or list of regular
        expressions) that the link's text must match in order to be extracted.
        If not given (or empty), it will match all links. If a list of regular
        expressions is given, the link will be extracted if it matches at least
        one.
    :type restrict_text: str or list

    :param tags: a tag or a list of tags to consider when extracting links.
        Defaults to ``('a', 'area')``. Use ``'*'`` to consider every tag.
    :type tags: str or list

    :param attrs: an attribute or list of attributes that should be considered
        when looking for links to extract (only for those tags specified in the
        ``tags`` parameter). Defaults to ``('href',)``. Use ``'*'`` to consider
        every attribute.
    :type attrs: list

    :param deny_tags: a tag or a list of tags that should not be considered when
        extracting links. It has precedence over the ``tags`` parameter, so it
        can be combined with ``tags='*'`` to consider every tag except a few.
        Defaults to ``()`` (no tag is excluded).

        .. versionadded:: 2.17.0
    :type deny_tags: str or list

    :param deny_attrs: an attribute or a list of attributes that should not be
        considered when looking for links to extract. It has precedence over the
        ``attrs`` parameter, so it can be combined with ``attrs='*'`` to consider
        every attribute except a few. Defaults to ``()`` (no attribute is
        excluded).

        .. versionadded:: 2.17.0
    :type deny_attrs: str or list

    :param canonicalize: canonicalize each extracted URL (using
        w3lib.url.canonicalize_url). Defaults to ``False``. Note that
        canonicalize_url is meant for duplicate checking; it can change the URL
        visible at the server side, so the response can be different for
        requests with canonicalized and raw URLs. If you're using LinkExtractor
        to follow links, it is more robust to keep the default
        ``canonicalize=False``.
    :type canonicalize: bool

    :param unique: whether duplicate filtering should be applied to extracted
        links.
    :type unique: bool

    :param process_value: a function that receives each value extracted from
        the tag and attributes scanned and can modify the value and return a
        new one, or return ``None`` to ignore the link altogether. If not
        given, ``process_value`` defaults to ``lambda x: x``.

        .. highlight:: html

        For example, to extract links from this code::

            <a href="javascript:goToPage('../other/page.html'); return false">Link text</a>

        .. highlight:: python

        You can use the following function in ``process_value``:

        .. code-block:: python

            def process_value(value):
                m = re.search(r"javascript:goToPage\('(.*?)'", value)
                if m:
                    return m.group(1)

        ``process_value`` is called before the filtering parameters, such as
        ``allow`` and ``deny``, which match the value that it returns. To drop
        links based on their final URL, use the ``process_links`` parameter of
        :class:`~scrapy.spiders.Rule`, which only receives links that those
        parameters kept.

    :type process_value: collections.abc.Callable

    :param strip: whether to strip whitespaces from extracted attributes.
        According to the HTML5 standard, leading and trailing whitespaces must
        be stripped from ``href`` attributes of ``<a>``, ``<area>`` and many
        other elements, the ``src`` attribute of ``<img>`` and ``<iframe>``
        elements, etc., so LinkExtractor strips space characters by default.
        Set ``strip=False`` to turn it off (for example, if you're extracting
        URLs from elements or attributes that allow leading or trailing
        whitespaces).
    :type strip: bool

    :param srcset_attrs: an attribute or list of attributes whose values are
        parsed as HTML ``srcset`` candidate lists, such as ``srcset`` and
        ``imagesrcset``. Each valid candidate is extracted as a separate
        :class:`~scrapy.link.Link`, with its descriptor (``480w`` or ``2x``)
        left out of the URL and the attribute name stored in the link's
        ``source_attr``. Commas inside data URLs are kept intact. Defaults to
        ``()``, in which case attributes are treated as single URLs as before.

        .. versionadded:: VERSION
    :type srcset_attrs: str or list

    :param srcset_on_error: what to do when a ``srcset`` attribute contains an
        invalid candidate (a malformed descriptor, a duplicate descriptor or a
        mix of width and density descriptors). With ``"skip"`` (the default)
        invalid candidates are skipped and the remaining ones are extracted;
        with ``"reject"`` the whole attribute is ignored. Each decision is
        written to the logger.

        .. versionadded:: VERSION
    :type srcset_on_error: str

    :param srcset_max_candidates: maximum number of candidates extracted from
        one ``srcset`` attribute. Once reached, parsing of that attribute stops
        and a warning is logged. Defaults to 100.

        .. versionadded:: VERSION
    :type srcset_max_candidates: int

    :param srcset_max_chars: maximum length, in characters, of a ``srcset``
        attribute that is parsed. Longer attributes are rejected and a warning
        is logged, so an oversized attribute cannot turn parsing into an
        unbounded loop. Defaults to 100000.

        .. versionadded:: VERSION
    :type srcset_max_chars: int
    """

    _csstranslator = HTMLTranslator()

    def __init__(
        self,
        allow: _RegexOrSeveral = (),
        deny: _RegexOrSeveral = (),
        allow_domains: str | Iterable[str] = (),
        deny_domains: str | Iterable[str] = (),
        restrict_xpaths: str | Iterable[str] = (),
        tags: str | Iterable[str] | None = ("a", "area"),
        attrs: str | Iterable[str] | None = ("href",),
        canonicalize: bool = False,
        unique: bool = True,
        process_value: Callable[[Any], Any] | None = None,
        deny_extensions: str | Iterable[str] | None = None,
        restrict_css: str | Iterable[str] = (),
        strip: bool = True,
        restrict_text: _RegexOrSeveral | None = None,
        deny_tags: str | Iterable[str] = (),
        deny_attrs: str | Iterable[str] = (),
        schemes: str | Iterable[str] | None = None,
        *,
        srcset_attrs: str | Iterable[str] | None = None,
        srcset_on_error: str = _SRCSET_ON_ERROR_SKIP,
        srcset_max_candidates: int = _DEFAULT_SRCSET_MAX_CANDIDATES,
        srcset_max_chars: int = _DEFAULT_SRCSET_MAX_CHARS,
    ):
        if srcset_on_error not in (
            _SRCSET_ON_ERROR_SKIP,
            _SRCSET_ON_ERROR_REJECT,
        ):
            raise ValueError(
                f"srcset_on_error must be 'skip' or 'reject', got {srcset_on_error!r}"
            )
        if srcset_max_candidates < 1:
            raise ValueError(
                "srcset_max_candidates must be a positive integer, "
                f"got {srcset_max_candidates!r}"
            )
        if srcset_max_chars < 1:
            raise ValueError(
                f"srcset_max_chars must be a positive integer, got {srcset_max_chars!r}"
            )
        srcset_attr_set = frozenset(arg_to_iter(srcset_attrs))
        tags, attrs = set(arg_to_iter(tags)), set(arg_to_iter(attrs))
        deny_tags, deny_attrs = (
            set(arg_to_iter(deny_tags)),
            set(arg_to_iter(deny_attrs)),
        )
        self.link_extractor = LxmlParserLinkExtractor(
            tag=partial(_name_matches, tags, deny_tags),
            attr=partial(_name_matches, attrs, deny_attrs),
            unique=unique,
            process=process_value,
            strip=strip,
            canonicalized=True,
            srcset_attrs=srcset_attr_set,
            srcset_on_error=srcset_on_error,
            srcset_max_candidates=srcset_max_candidates,
            srcset_max_chars=srcset_max_chars,
        )
        self.srcset_attrs: frozenset[str] = srcset_attr_set
        self.allow_res: list[re.Pattern[str]] = self._compile_regexes(allow)
        self.deny_res: list[re.Pattern[str]] = self._compile_regexes(deny)

        self.allow_domains: set[str] = set(arg_to_iter(allow_domains))
        self.deny_domains: set[str] = set(arg_to_iter(deny_domains))

        self.restrict_xpaths: tuple[str, ...] = tuple(arg_to_iter(restrict_xpaths))
        self.restrict_xpaths += tuple(
            map(self._csstranslator.css_to_xpath, arg_to_iter(restrict_css))
        )

        if deny_extensions is None:
            deny_extensions = IGNORED_EXTENSIONS
        self.canonicalize: bool = canonicalize
        self.deny_extensions: set[str] = {"." + e for e in arg_to_iter(deny_extensions)}
        self.schemes: set[str] | None = (
            None if schemes is None else set(arg_to_iter(schemes))
        )
        self.restrict_text: list[re.Pattern[str]] = self._compile_regexes(restrict_text)

    @staticmethod
    def _compile_regexes(value: _RegexOrSeveral | None) -> list[re.Pattern[str]]:
        return [
            x if isinstance(x, re.Pattern) else re.compile(x)
            for x in arg_to_iter(value)
        ]

    def _link_allowed(self, link: Link, schemes: set[str]) -> bool:
        parsed_url = urlparse_cached(link.url)
        if schemes and parsed_url.scheme not in schemes:
            return False
        if self.allow_res and not _matches(link.url, self.allow_res):
            return False
        if self.deny_res and _matches(link.url, self.deny_res):
            return False
        if self.allow_domains and not url_is_from_any_domain(
            parsed_url, self.allow_domains
        ):
            return False
        if self.deny_domains and url_is_from_any_domain(parsed_url, self.deny_domains):
            return False
        if self.deny_extensions and _has_extension(
            parsed_url.path, self.deny_extensions
        ):
            return False
        return not self.restrict_text or _matches(link.text, self.restrict_text)

    def matches(self, url: str) -> bool:
        if self.allow_domains and not url_is_from_any_domain(url, self.allow_domains):
            return False
        if self.deny_domains and url_is_from_any_domain(url, self.deny_domains):
            return False

        allowed = (
            (regex.search(url) for regex in self.allow_res)
            if self.allow_res
            else [True]
        )
        denied = (regex.search(url) for regex in self.deny_res) if self.deny_res else ()
        return any(allowed) and not any(denied)

    def _process_links(self, links: list[Link], schemes: set[str]) -> list[Link]:
        links = [x for x in links if self._link_allowed(x, schemes)]
        if self.canonicalize:
            for link in links:
                link.url = canonicalize_url(link.url)
            return self.link_extractor._process_links(links)
        return links

    def _extract_links(self, *args: Any, **kwargs: Any) -> list[Link]:
        return self.link_extractor._extract_links(*args, **kwargs)

    def extract_links(self, response: TextResponse) -> list[Link]:
        """Returns a list of :class:`~scrapy.link.Link` objects from the
        specified :class:`response <scrapy.http.Response>`.

        Only links that match the settings passed to the ``__init__`` method of
        the link extractor are returned.

        Duplicate links are omitted if the ``unique`` attribute is set to ``True``,
        otherwise they are returned.
        """
        base_url = response.base_url
        if self.restrict_xpaths:
            docs = [
                subdoc for x in self.restrict_xpaths for subdoc in response.xpath(x)
            ]
        else:
            docs = [response.selector]
        schemes = (
            {"http", "https", urlparse_cached(response).scheme}
            if self.schemes is None
            else self.schemes
        )
        all_links = []
        for doc in docs:
            links = self._extract_links(doc, response.url, response.encoding, base_url)
            all_links.extend(self._process_links(links, schemes))
        if self.link_extractor.unique and len(docs) > 1:
            return unique_list(all_links, key=self.link_extractor.link_key)
        return all_links
