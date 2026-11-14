"""
This module implements the TextResponse class which adds encoding handling and
discovering (through HTTP headers) to base Response class.

See documentation in docs/topics/request-response.rst
"""

from __future__ import annotations

import json
import warnings
import weakref
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Literal, cast
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
from scrapy.http.response import Response
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


class StrictJSONError(ValueError):
    """Base class for failures of strict JSON decoding in
    :meth:`TextResponse.json`.

    Like :class:`json.JSONDecodeError`, it is a :class:`ValueError`, so code
    catching ``ValueError`` keeps working.
    """


class DuplicateJSONKeyError(StrictJSONError):
    r"""A single JSON object contains the same key more than once.

    *path* is the JSONPath-like location of the offending object (``$`` is the
    document root, ``$.items[0].price`` a nested location), and *key* is the
    duplicated key. Keys decoded from different escapes, e.g. ``"a\u0062"``
    and ``"ab"``, count as the same key because the parser sees one decoded
    string.
    """

    def __init__(self, path: str, key: str):
        self.path = path
        self.key = key
        super().__init__(f"Duplicate JSON key {key!r} in object at {path}")


class JSONBudgetExceededError(StrictJSONError):
    """A strict JSON document is deeper or larger than the configured budget.

    *kind* is ``"depth"`` or ``"nodes"``.
    """

    def __init__(self, kind: str, budget: int, path: str):
        self.kind = kind
        self.budget = budget
        self.path = path
        super().__init__(f"JSON {kind} budget of {budget} exceeded at {path}")


class _StrictJSONObject(dict):
    """Dict marking an object built from a JSON ``{...}`` fragment.

    The marker lets :func:`_validate_strict_json` locate the object's path
    after decoding and carries the keys repeated inside that one object. A
    parallel list shared by all markers keeps the violating keys reachable
    even if a user hook replaces the marker with another object type.
    """

    def __init__(
        self,
        pairs: list[tuple[str, Any]],
        duplicates: list[str],
        violations: list[str],
    ):
        super().__init__(pairs)
        self.duplicates = duplicates
        violations.extend(duplicates)


def _strict_object_pairs_hook(
    pairs: list[tuple[str, Any]],
    violations: list[str],
    user_object_pairs_hook: Callable[[list[tuple[str, Any]]], Any] | None = None,
    user_object_hook: Callable[[dict[str, Any]], Any] | None = None,
) -> Any:
    seen: set[str] = set()
    duplicates: list[str] = []
    for key, _ in pairs:
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    strict_object = _StrictJSONObject(pairs, duplicates, violations)
    # User hooks still run, in the same order json.loads would call them:
    # object_pairs_hook takes precedence over object_hook.
    if user_object_pairs_hook is not None:
        return user_object_pairs_hook(list(strict_object.items()))
    if user_object_hook is not None:
        return user_object_hook(dict(strict_object))
    return strict_object


def _reject_json_constant(value: str) -> Any:
    raise StrictJSONError(f"{value} is not valid JSON")


def _format_json_path(segments: tuple[str | int, ...]) -> str:
    path = "$"
    for segment in segments:
        if isinstance(segment, int):
            path += f"[{segment}]"
        else:
            path += f".{segment}"
    return path


def _check_depth_budget(max_depth: int | None, container_depth: int, path: str) -> None:
    if max_depth is not None and container_depth > max_depth:
        raise JSONBudgetExceededError("depth", max_depth, path)


def _validate_strict_json(
    value: Any,
    violations: list[str],
    *,
    max_depth: int | None,
    max_nodes: int | None,
) -> Any:
    """Check the decoded document for duplicate keys and budget limits, and
    return it with the internal marker dicts replaced by plain dicts.

    The walk uses an explicit stack so a deeply nested document cannot exhaust
    the Python call stack before the depth budget is reported.
    """
    # Frames are (node, parent, slot, path segments, depth). *parent* and
    # *slot* describe where to write the node back after it is processed; the
    # root has none. A node is counted once, when it is pushed onto the stack.
    root: Any = value
    stack: list[tuple[Any, Any, Any, tuple[str | int, ...], int]] = [
        (value, None, None, (), 0)
    ]
    node_count = 0
    while stack:
        node, parent, slot, segments, depth = stack.pop()
        node_count += 1
        if max_nodes is not None and node_count > max_nodes:
            raise JSONBudgetExceededError(
                "nodes", max_nodes, _format_json_path(segments)
            )
        container_depth = depth + 1
        if isinstance(node, dict):
            path = _format_json_path(segments)
            _check_depth_budget(max_depth, container_depth, path)
            if isinstance(node, _StrictJSONObject):
                if node.duplicates:
                    raise DuplicateJSONKeyError(path, node.duplicates[0])
                # Drop the marker type so the caller only sees plain dicts.
                plain = dict(node)
                if parent is None:
                    root = plain
                else:
                    parent[slot] = plain
                node = plain
            for key, child in node.items():
                stack.append((child, node, key, (*segments, key), container_depth))
        elif isinstance(node, list):
            _check_depth_budget(max_depth, container_depth, _format_json_path(segments))
            # Push in reverse so element 0 is processed first; the stored
            # index is the real array index regardless of visit order.
            for reverse_index, child in enumerate(reversed(node)):
                index = len(node) - 1 - reverse_index
                stack.append((child, node, index, (*segments, index), container_depth))
    # A user hook rebuilt every offending object, removing the markers that
    # would have located their paths. The violating keys were still collected
    # while parsing.
    if violations:
        raise DuplicateJSONKeyError("$", violations[0])
    return root


class TextResponse(Response):
    attributes: tuple[str, ...] = (*Response.attributes, "encoding")
    __slots__ = (
        "_base_url",
        "_cached_benc",
        "_cached_decoded_json",
        "_cached_selector",
        "_cached_strict_json",
        "_cached_ubody",
        "_encoding",
        "_meta_refresh",
    )

    def __init__(self, *args: Any, **kwargs: Any):
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
        self._cached_benc: str | None = None
        self._cached_ubody: str | None = None
        self._cached_selector: Selector | None = None
        self._cached_decoded_json: object = _UNSET
        self._cached_strict_json: dict[tuple[Any, ...], object] = {}
        self._base_url: str | None = None
        self._meta_refresh: MetaRefresh | Literal[_Unset.UNSET] | None = _UNSET
        super().__init__(*args, **kwargs)

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
        return self._declared_encoding() or self._body_inferred_encoding()

    def _declared_encoding(self) -> str | None:
        return (
            self._encoding
            or self._bom_encoding()
            or self._headers_encoding()
            or self._body_declared_encoding()
        )

    def json(
        self,
        *,
        strict: bool = False,
        max_depth: int | None = None,
        max_nodes: int | None = None,
        **kwargs: Any,
    ) -> Any:
        r"""Deserialize a JSON document to a Python object.

        .. versionchanged:: 2.18.0
           Bodies that cannot be decoded as UTF-8, UTF-16 or UTF-32, as the
           JSON specification requires, are now decoded using
           :attr:`TextResponse.encoding` instead of raising
           :exc:`UnicodeDecodeError`.

        .. versionchanged:: 2.20.0
           Added *strict*, *max_depth* and *max_nodes*.

        The result is cached after the first call. The strict cache and the
        default cache are separate, and every distinct combination of strict
        parameters and the extra keyword arguments gets its own cache entry, so
        calls with different parsing options never reuse each other's result.

        With *strict* enabled, the body must be JSON as specified by RFC 8259:
        it is decoded directly from the bytes as UTF-8, UTF-16 or UTF-32
        without falling back to :attr:`TextResponse.encoding`, the literals
        ``NaN``, ``Infinity`` and ``-Infinity`` are rejected, and an object
        that repeats a key at any nesting level raises
        :class:`DuplicateJSONKeyError` naming the key and its path. Same-named
        keys in different objects, including objects inside the same array,
        are allowed; keys written with different escapes, e.g. ``"a\u0062"``
        and ``"ab"``, are the same key once decoded.

        *max_depth* limits the nesting of objects and arrays, and *max_nodes*
        limits the total number of JSON values; exceeding either raises
        :class:`JSONBudgetExceededError`. Both are ignored when *strict* is
        false, matching the default lenient behavior.

        Further keyword arguments are passed to :func:`json.loads`. A
        user-supplied *parse_constant*, *object_pairs_hook* or *object_hook*
        is wrapped rather than replaced, so the strict checks still run and
        the user hook keeps its effect. If an object hook replaces the strict
        marker with another object type, its path can no longer be located and
        a duplicate inside it is reported at the document root.
        """
        if not strict:
            if max_depth is not None or max_nodes is not None:
                raise ValueError("max_depth and max_nodes only apply with strict=True")
            if self._cached_decoded_json is _UNSET:
                try:
                    self._cached_decoded_json = json.loads(self.body, **kwargs)
                except UnicodeDecodeError:
                    self._cached_decoded_json = json.loads(self.text, **kwargs)
            return self._cached_decoded_json

        if (max_depth is not None and max_depth < 0) or (
            max_nodes is not None and max_nodes <= 0
        ):
            raise ValueError("max_depth must be >= 0 and max_nodes must be > 0")
        cache_key = (max_depth, max_nodes, tuple(sorted(kwargs.items())))
        if cache_key not in self._cached_strict_json:
            self._cached_strict_json[cache_key] = self._decode_strict_json(
                max_depth=max_depth, max_nodes=max_nodes, **kwargs
            )
        return self._cached_strict_json[cache_key]

    def _decode_strict_json(
        self,
        *,
        max_depth: int | None,
        max_nodes: int | None,
        **kwargs: Any,
    ) -> Any:
        # RFC 8259 only allows UTF-8/16/32; never fall back to the response's
        # declared encoding, which can turn malformed bytes into replacement
        # characters and hide data loss.
        parse_kwargs = dict(kwargs)
        violations: list[str] = []
        if "parse_constant" not in parse_kwargs:
            parse_kwargs["parse_constant"] = _reject_json_constant
        if (
            "object_pairs_hook" not in parse_kwargs
            and "object_hook" not in parse_kwargs
        ):
            parse_kwargs["object_pairs_hook"] = lambda pairs: _strict_object_pairs_hook(
                pairs, violations
            )
        elif "object_pairs_hook" in parse_kwargs:
            user_pairs_hook = parse_kwargs.pop("object_pairs_hook")
            parse_kwargs["object_pairs_hook"] = lambda pairs, hook=user_pairs_hook: (
                _strict_object_pairs_hook(
                    pairs,
                    violations,
                    user_object_pairs_hook=hook,
                )
            )
        else:
            user_object_hook = parse_kwargs.pop("object_hook")
            parse_kwargs["object_pairs_hook"] = lambda pairs, hook=user_object_hook: (
                _strict_object_pairs_hook(
                    pairs,
                    violations,
                    user_object_hook=hook,
                )
            )
        decoded = json.loads(self.body, **parse_kwargs)
        return _validate_strict_json(
            decoded, violations, max_depth=max_depth, max_nodes=max_nodes
        )

    @property
    def text(self) -> str:
        """Body as unicode"""
        if self._cached_ubody is None:
            if (
                self._encoding is not None
                and not _is_browser_encoding(self._encoding)
                and not self._bom_encoding()
            ):
                with suppress(LookupError):
                    self._cached_ubody = self.body.decode(self._encoding, "replace")
            if self._cached_ubody is None:
                charset = f"charset={self.encoding}"
                self._cached_ubody = html_to_unicode(charset, self.body)[1]
        return self._cached_ubody

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
