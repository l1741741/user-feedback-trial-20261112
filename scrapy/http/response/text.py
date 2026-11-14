"""
This module implements the TextResponse class which adds encoding handling and
discovering (through HTTP headers) to base Response class.

See documentation in docs/topics/request-response.rst
"""

from __future__ import annotations

import json
import re
import warnings
import weakref
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Literal, Self, cast, overload
from urllib.parse import urljoin

import frostwork
import parsel
from w3lib.encoding import (
    html_body_declared_encoding,
    html_encoding,
    html_to_unicode,
    http_content_type_encoding,
    read_bom,
    resolve_encoding,
)
from w3lib.html import strip_html5_whitespace

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http.response import Response, ResponseTypeVar
from scrapy.utils.python import memoizemethod_noargs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from twisted.python.failure import Failure

    from scrapy.http.request import CallbackT, CookiesT, Request
    from scrapy.link import Link
    from scrapy.selector import Selector, SelectorList


class _Unset(Enum):
    UNSET = auto()


_UNSET = _Unset.UNSET

# In strict mode the encoding declared inside the body is only looked for in
# the first META_PREFIX_SIZE bytes, the limit of the prescan of the HTML
# standard, so a huge body without an early declaration is never scanned in
# full.
META_PREFIX_SIZE = 4096

# Charset labels are token or quoted-string values, made of word characters
# and hyphens, the characters w3lib accepts in an encoding label.
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]*?charset\s*=\s*['"]?\s*([\w-]+)""",
    re.IGNORECASE,
)
_XML_CHARSET_RE = re.compile(
    rb"""<\?xml[^>]*?encoding\s*=\s*['"]([\w-]+)['"]""",
    re.IGNORECASE,
)
# A declaration inside an HTML comment is not a declaration, as in browsers.
_COMMENT_RE = re.compile(rb"<!--.*?(-->|$)", re.DOTALL)


class ResponseEncodingError(LookupError):
    """Raised by a :class:`TextResponse` created with ``strict_encoding=True``
    when its encoding cannot be resolved the way the constructor was asked to.

    .. versionadded:: VERSION

    *source* names where the unsupported encoding came from: ``"encoding"``
    for the *encoding* argument, ``"bom"`` for a byte order mark,
    ``"http"`` for the charset of the Content-Type header and ``"meta"`` for
    the charset declared by a ``<meta>`` tag or an XML declaration.

    Invalid bytes never raise this error; they raise the standard
    :exc:`UnicodeDecodeError` when *encoding_errors* is ``"strict"``.
    """

    def __init__(self, source: str, label: str, *, reason: str):
        self.source = source
        """Where the encoding came from."""
        self.label = label
        """The encoding label that could not be used, or the two labels of an
        incompatible pair, joined with ``" != "``."""
        self.reason = reason
        super().__init__(f"{source} encoding {label!r}: {reason}")


@dataclass(frozen=True)
class MetaRefresh:
    """Meta refresh of a :class:`~scrapy.http.TextResponse`, as
    :attr:`TextResponse.meta_refresh <scrapy.http.TextResponse.meta_refresh>`
    reports it.

    .. versionadded:: 2.20.0
    """

    delay: float
    """Seconds to wait before the refresh."""

    url: str
    """Absolute URL to load."""


def _is_browser_encoding(encoding: str) -> bool:
    resolved = resolve_encoding(encoding)
    # "replacement" is what response.encoding reports for labels that browsers
    # decode as a single U+FFFD, e.g. a Content-Type charset of hz.
    return resolved is not None and (
        resolved != "replacement" or encoding == "replacement"
    )


class TextResponse(Response):
    attributes: tuple[str, ...] = (
        *Response.attributes,
        "encoding",
        "strict_encoding",
        "encoding_errors",
    )
    __slots__ = (
        "_base_url",
        "_cached_benc",
        "_cached_decoded_json",
        "_cached_selector",
        "_cached_strict",
        "_cached_ubody",
        "_encoding",
        "_encoding_errors",
        "_meta_refresh",
        "_strict_encoding",
    )

    def __init__(
        self,
        *args: Any,
        strict_encoding: bool = False,
        encoding_errors: Literal["strict", "replace"] | None = None,
        **kwargs: Any,
    ):
        self._encoding: str | None = kwargs.pop("encoding", None)
        if self._encoding is not None and not _is_browser_encoding(self._encoding):
            warnings.warn(
                f"Passing encoding={self._encoding!r} to {type(self).__name__} "
                f"is deprecated, because web browsers do not support that "
                f"encoding, and it will raise ValueError in a future version "
                f"of Scrapy. To decode the response body with it, use "
                f"response.body.decode({self._encoding!r}) instead.",
                ScrapyDeprecationWarning,
                stacklevel=2,
            )
        if encoding_errors is not None and encoding_errors not in {
            "strict",
            "replace",
        }:
            raise ValueError(
                "encoding_errors must be 'strict' or 'replace', got "
                f"{encoding_errors!r}"
            )
        self._strict_encoding: bool = strict_encoding
        self._encoding_errors: Literal["strict", "replace"] = encoding_errors or (
            "strict" if strict_encoding else "replace"
        )
        self._cached_benc: str | None = None
        self._cached_ubody: str | None = None
        self._cached_strict: tuple[str, int] | None = None
        self._cached_selector: Selector | None = None
        self._cached_decoded_json: object = _UNSET
        self._base_url: str | None = None
        self._meta_refresh: MetaRefresh | Literal[_Unset.UNSET] | None = _UNSET
        super().__init__(*args, **kwargs)

    @property
    def strict_encoding(self) -> bool:
        """Whether encoding conflicts and unknown encodings raise
        :class:`ResponseEncodingError` instead of being handled the way
        browsers do.

        .. versionadded:: VERSION
        """
        return self._strict_encoding

    @property
    def encoding_errors(self) -> Literal["strict", "replace"]:
        """How :attr:`text` treats bytes that the resolved encoding cannot
        decode: ``"strict"`` raises :exc:`UnicodeDecodeError`, ``"replace"``
        inserts U+FFFD.

        .. versionadded:: VERSION
        """
        return self._encoding_errors

    @property
    def _explicit_encoding(self) -> str | None:
        """The *encoding* argument passed to the constructor, before any
        resolution. :meth:`replace` copies it instead of the resolved
        :attr:`encoding`, so a response keeps inferring its encoding when no
        explicit encoding was given."""
        return self._encoding

    @overload
    def replace(
        self, *args: Any, cls: type[ResponseTypeVar], **kwargs: Any
    ) -> ResponseTypeVar: ...

    @overload
    def replace(self, *args: Any, cls: None = None, **kwargs: Any) -> Self: ...

    def replace(
        self, *args: Any, cls: type[Response] | None = None, **kwargs: Any
    ) -> Response:
        if not self._strict_encoding and not kwargs.get("strict_encoding"):
            # The resolved encoding has always been part of the copied
            # attributes, including when no encoding argument was given.
            return super().replace(*args, cls=cls, **kwargs)
        # In strict mode reading the public encoding property can raise, and a
        # resolved encoding must not be frozen into a response whose encoding
        # was only inferred. Copy the constructor inputs instead.
        for attr in self.attributes:
            if attr == "encoding":
                kwargs.setdefault(attr, self._explicit_encoding)
            elif attr == "strict_encoding":
                kwargs.setdefault(attr, self.strict_encoding)
            elif attr == "encoding_errors":
                kwargs.setdefault(attr, self.encoding_errors)
            else:
                kwargs.setdefault(attr, getattr(self, attr))
        if cls is None:
            cls = self.__class__
        return cls(*args, **kwargs)

    def _set_body(self, body: str | bytes | None) -> None:
        self._body: bytes = b""  # used by encoding detection
        if isinstance(body, str):
            if self._encoding is None:
                raise TypeError(
                    "Cannot convert unicode body - "
                    f"{type(self).__name__} has no encoding"
                )
            self._body = body.encode(self._encoding)
            # Keep body as text only if decoding _body gives it back
            # unchanged. html_to_unicode() may resolve the encoding to
            # another codec (e.g. latin-1 to cp1252) and strips a BOM.
            if resolve_encoding(self._encoding) == "utf-8" and not body.startswith(
                "\ufeff"
            ):
                self._cached_ubody = body
        else:
            super()._set_body(body)

    @property
    def encoding(self) -> str:
        if self._strict_encoding:
            return self._strict_encoding_resolution()[0]
        return self._declared_encoding() or self._body_inferred_encoding()

    def _declared_encoding(self) -> str | None:
        return (
            self._encoding
            or self._bom_encoding()
            or self._headers_encoding()
            or self._body_declared_encoding()
        )

    def _strict_encoding_resolution(self) -> tuple[str, int]:
        """Resolve the encoding and the length of a BOM to strip, following
        the documented strict-mode priority and validating every source."""
        if self._cached_strict is None:
            self._cached_strict = self._resolve_strict_encoding()
        return self._cached_strict

    def _resolve_strict_encoding(self) -> tuple[str, int]:
        bom_enc, bom_length = self._bom_with_length()
        if self._encoding is not None:
            resolved = self._resolve_strict_label(self._encoding, "encoding")
            if bom_enc is not None and bom_enc != resolved:
                raise ResponseEncodingError(
                    "bom",
                    f"{self._encoding} != {bom_enc}",
                    reason=(
                        "the encoding argument is incompatible with the byte "
                        "order mark of the body"
                    ),
                )
            return resolved, bom_length
        if bom_enc is not None:
            return bom_enc, bom_length
        header_label = self._raw_header_charset()
        if header_label is not None:
            return (
                self._resolve_strict_label(header_label, "http"),
                0,
            )
        meta_label, meta_source = self._raw_meta_charset()
        if meta_label is not None:
            return self._resolve_strict_label(meta_label, meta_source), 0
        # No source named an encoding; UTF-8 is the documented default.
        return "utf-8", 0

    @staticmethod
    def _resolve_strict_label(label: str, source: str) -> str:
        resolved = resolve_encoding(label)
        if resolved is None or (resolved == "replacement" and label != "replacement"):
            raise ResponseEncodingError(
                source,
                label,
                reason="the label names no supported encoding",
            )
        return resolved

    def _bom_with_length(self) -> tuple[str | None, int]:
        from frostwork.encoding import bom_encoding  # noqa: PLC0415

        bom = bom_encoding(self.body)
        if bom is None:
            return None, 0
        name, length = bom
        return cast("str", resolve_encoding(name)), length

    def _raw_header_charset(self) -> str | None:
        content_type = self.headers.get(b"Content-Type")
        if content_type is None:
            return None
        match = re.search(
            r"(?:^|;)[ \t]*charset=(?:\"([\w-]+)\"|([\w-]+))",
            content_type.decode("latin-1"),
            re.IGNORECASE,
        )
        if match is None:
            return None
        return match.group(1) or match.group(2)

    def _raw_meta_charset(self) -> tuple[str | None, str]:
        """Return the raw charset label declared in the bounded body prefix
        and its source, without resolving it, so an unknown label can be
        reported with the exact bytes the document used."""
        prefix = _COMMENT_RE.sub(b"", self.body[:META_PREFIX_SIZE])
        # An XML declaration is only recognized at the start of the document.
        match = _XML_CHARSET_RE.match(prefix)
        if match is not None:
            return match.group(1).decode("latin-1"), "meta"
        match = _META_CHARSET_RE.search(prefix)
        if match is not None:
            return match.group(1).decode("latin-1"), "meta"
        return None, "meta"

    def json(self) -> Any:
        """Deserialize a JSON document to a Python object.

        .. versionchanged:: 2.18.0
           Bodies that cannot be decoded as UTF-8, UTF-16 or UTF-32, as the
           JSON specification requires, are now decoded using
           :attr:`TextResponse.encoding` instead of raising
           :exc:`UnicodeDecodeError`.

        The result is cached after the first call.
        """
        if self._cached_decoded_json is _UNSET:
            try:
                self._cached_decoded_json = json.loads(self.body)
            except UnicodeDecodeError:
                self._cached_decoded_json = json.loads(self.text)
        return self._cached_decoded_json

    @property
    def text(self) -> str:
        """Body as unicode"""
        if self._cached_ubody is None:
            if self._strict_encoding:
                self._cached_ubody = self._strict_text()
            elif (
                self._encoding is not None
                and not _is_browser_encoding(self._encoding)
                and not self._bom_encoding()
            ):
                with suppress(LookupError):
                    self._cached_ubody = self.body.decode(self._encoding, "replace")
                if self._cached_ubody is None:
                    charset = f"charset={self.encoding}"
                    self._cached_ubody = html_to_unicode(charset, self.body)[1]
            else:
                charset = f"charset={self.encoding}"
                self._cached_ubody = html_to_unicode(charset, self.body)[1]
        return self._cached_ubody

    def _strict_text(self) -> str:
        encoding, bom_length = self._strict_encoding_resolution()
        return self.body[bom_length:].decode(encoding, self._encoding_errors)

    def urljoin(self, url: str) -> str:
        """Join this Response's url with a possible relative url to form an
        absolute interpretation of the latter."""
        return urljoin(self.base_url, url)

    @property
    def base_url(self) -> str:
        """Absolute URL that relative URLs of the response are relative to.

        .. versionadded:: 2.20.0

        It is the ``href`` of the first ``<base>`` element that has one,
        resolved against :attr:`~Response.url`, or :attr:`~Response.url`
        itself, serialized like a web browser does, e.g. with ``/`` as empty
        path. Setting it overrides the value read from the body.
        """
        if self._base_url is None:
            self._read_html_metadata()
        assert self._base_url is not None
        return self._base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._base_url = value

    @property
    def meta_refresh(self) -> MetaRefresh | None:
        """:class:`~scrapy.http.response.text.MetaRefresh` that a web browser
        with JavaScript enabled would follow, or ``None``.

        .. versionadded:: 2.20.0

        Setting it overrides the value read from the body.
        """
        if self._meta_refresh is _UNSET:
            self._read_html_metadata()
        assert self._meta_refresh is not _UNSET
        return self._meta_refresh

    @meta_refresh.setter
    def meta_refresh(self, value: MetaRefresh | tuple[float, str] | None) -> None:
        if value is None or isinstance(value, MetaRefresh):
            self._meta_refresh = value
        else:
            self._meta_refresh = MetaRefresh(*value)

    def _read_html_metadata(self) -> None:
        metadata = frostwork.navigation(
            self.body,
            self.url,
            self.encoding,
            base_url=self._base_url is None,
            meta_refresh=self._meta_refresh is _UNSET,
        )
        if self._base_url is None:
            self._base_url = metadata.base_url
        if self._meta_refresh is _UNSET:
            self.meta_refresh = metadata.meta_refresh

    @memoizemethod_noargs
    def _headers_encoding(self) -> str | None:
        content_type = self.headers.get(b"Content-Type") or b""
        return http_content_type_encoding(content_type.decode("latin-1"))

    def _body_inferred_encoding(self) -> str:
        if self._cached_benc is None:
            content_type = self.headers.get(b"Content-Type") or b""
            self._cached_benc = html_encoding(
                content_type.decode("latin-1"), self.body, url=self.url
            )
        return self._cached_benc

    @memoizemethod_noargs
    def _body_declared_encoding(self) -> str | None:
        return html_body_declared_encoding(self.body)

    @memoizemethod_noargs
    def _bom_encoding(self) -> str | None:
        return read_bom(self.body)[0]

    @property
    def selector(self) -> Selector:
        if self._cached_selector is None:
            # circular import
            from scrapy.selector import Selector  # noqa: PLC0415

            selector = Selector(self)
            # The weak reference lets the response be freed as soon as nothing
            # else refers to it.
            selector._response = weakref.ref(self)
            self._cached_selector = selector
        return self._cached_selector

    def jmespath(self, query: str, **kwargs: Any) -> SelectorList:
        return cast("SelectorList", self.selector.jmespath(query, **kwargs))

    def xpath(self, query: str, **kwargs: Any) -> SelectorList:
        return cast("SelectorList", self.selector.xpath(query, **kwargs))

    def css(self, query: str) -> SelectorList:
        return cast("SelectorList", self.selector.css(query))

    def follow(
        self,
        url: str | Link | parsel.Selector,
        callback: CallbackT | None = None,
        method: str = "GET",
        headers: Mapping[str, Any]
        | Mapping[bytes, Any]
        | Iterable[tuple[str | bytes, Any]]
        | None = None,
        body: bytes | str | None = None,
        cookies: CookiesT | None = None,
        meta: dict[str, Any] | None = None,
        encoding: str | None = None,
        priority: int = 0,
        dont_filter: bool = False,
        errback: Callable[[Failure], Any] | None = None,
        cb_kwargs: dict[str, Any] | None = None,
        flags: list[str] | None = None,
    ) -> Request:
        """
        Return a :class:`~.Request` instance to follow a link ``url``.
        It accepts the same arguments as ``Request.__init__()`` method,
        but ``url`` can be not only an absolute URL, but also

        * a relative URL
        * a :class:`~scrapy.link.Link` object, e.g. the result of
          :ref:`topics-link-extractors`
        * a :class:`~scrapy.Selector` object for a ``<link>`` or ``<a>`` element, e.g.
          ``response.css('a.my_link')[0]``
        * an attribute :class:`~scrapy.Selector` (not SelectorList), e.g.
          ``response.css('a::attr(href)')[0]`` or
          ``response.xpath('//img/@src')[0]``

        See :ref:`response-follow-example` for usage examples.
        """
        if isinstance(url, parsel.Selector):
            url = _url_from_selector(url)
        elif isinstance(url, parsel.SelectorList):
            raise ValueError("SelectorList is not supported")
        encoding = self.encoding if encoding is None else encoding
        return super().follow(
            url=url,
            callback=callback,
            method=method,
            headers=headers,
            body=body,
            cookies=cookies,
            meta=meta,
            encoding=encoding,
            priority=priority,
            dont_filter=dont_filter,
            errback=errback,
            cb_kwargs=cb_kwargs,
            flags=flags,
        )

    def follow_all(
        self,
        urls: Iterable[str | Link] | parsel.SelectorList[Any] | None = None,
        callback: CallbackT | None = None,
        method: str = "GET",
        headers: Mapping[str, Any]
        | Mapping[bytes, Any]
        | Iterable[tuple[str | bytes, Any]]
        | None = None,
        body: bytes | str | None = None,
        cookies: CookiesT | None = None,
        meta: dict[str, Any] | None = None,
        encoding: str | None = None,
        priority: int = 0,
        dont_filter: bool = False,
        errback: Callable[[Failure], Any] | None = None,
        cb_kwargs: dict[str, Any] | None = None,
        flags: list[str] | None = None,
        css: str | None = None,
        xpath: str | None = None,
    ) -> Iterable[Request]:
        """
        A generator that produces :class:`~.Request` instances to follow all
        links in ``urls``. It accepts the same arguments as the :class:`~.Request`'s
        ``__init__()`` method, except that each ``urls`` element does not need to be
        an absolute URL, it can be any of the following:

        * a relative URL
        * a :class:`~scrapy.link.Link` object, e.g. the result of
          :ref:`topics-link-extractors`
        * a :class:`~scrapy.Selector` object for a ``<link>`` or ``<a>`` element, e.g.
          ``response.css('a.my_link')[0]``
        * an attribute :class:`~scrapy.Selector` (not SelectorList), e.g.
          ``response.css('a::attr(href)')[0]`` or
          ``response.xpath('//img/@src')[0]``

        In addition, ``css`` and ``xpath`` arguments are accepted to perform the link extraction
        within the ``follow_all()`` method (only one of ``urls``, ``css`` and ``xpath`` is accepted).

        Note that when passing a ``SelectorList`` as argument for the ``urls`` parameter or
        using the ``css`` or ``xpath`` parameters, this method will not produce requests for
        selectors from which links cannot be obtained (for instance, anchor tags without an
        ``href`` attribute)

        .. seealso:: :meth:`.Response.follow_all`, for a caution about mutable
            *meta* and *cb_kwargs* values.
        """
        arguments = [x for x in (urls, css, xpath) if x is not None]
        if len(arguments) != 1:
            raise ValueError(
                "Please supply exactly one of the following arguments: urls, css, xpath"
            )
        if not urls:
            if css:
                urls = self.css(css)
            if xpath:
                urls = self.xpath(xpath)
        if isinstance(urls, parsel.SelectorList):
            selectors = urls
            urls = []
            for sel in selectors:
                with suppress(_InvalidSelector):
                    urls.append(_url_from_selector(sel))
        return super().follow_all(
            urls=cast("Iterable[str | Link]", urls),
            callback=callback,
            method=method,
            headers=headers,
            body=body,
            cookies=cookies,
            meta=meta,
            encoding=encoding,
            priority=priority,
            dont_filter=dont_filter,
            errback=errback,
            cb_kwargs=cb_kwargs,
            flags=flags,
        )


class _InvalidSelector(ValueError):
    """
    Raised when a URL cannot be obtained from a Selector
    """


def _url_from_selector(sel: parsel.Selector) -> str:
    if isinstance(sel.root, str):
        # e.g. ::attr(href) result
        return strip_html5_whitespace(sel.root)
    if not hasattr(sel.root, "tag"):
        raise _InvalidSelector(f"Unsupported selector: {sel}")
    if sel.root.tag not in {"a", "link"}:
        raise _InvalidSelector(
            f"Only <a> and <link> elements are supported; got <{sel.root.tag}>"
        )
    href = sel.root.get("href")
    if href is None:
        raise _InvalidSelector(f"<{sel.root.tag}> element has no href attribute: {sel}")
    return strip_html5_whitespace(href)
