"""
This module provides some useful functions for working with
scrapy.Request objects
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Protocol, Self
from urllib.parse import unquote_to_bytes, urlunparse
from weakref import WeakKeyDictionary

from w3lib.url import canonicalize_url

from scrapy import Request, Spider
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import load_object
from scrapy.utils.python import to_bytes

if TYPE_CHECKING:
    from collections.abc import Iterable

    from scrapy.crawler import Crawler
    from scrapy.http.request import CookiesT, VerboseCookie

logger = logging.getLogger(__name__)


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _decode_query_name_rule(name: str) -> bytes:
    """Decode a query parameter name rule, rejecting malformed encoding.

    Rules are names, not query strings, so a ``+`` in a rule stands for the
    literal plus sign, which a URL only needs to escape as ``%2B``.
    """
    i = 0
    while i < len(name):
        if name[i] != "%":
            i += 1
            continue
        if (
            i + 2 >= len(name)
            or name[i + 1] not in _HEX_DIGITS
            or name[i + 2] not in _HEX_DIGITS
        ):
            raise ValueError(
                f"Invalid query parameter name rule {name!r}:"
                f" {name[i : i + 3]!r} is not a valid percent escape."
            )
        i += 3
    raw_name = unquote_to_bytes(name)
    try:
        raw_name.decode()
    except UnicodeDecodeError:
        raise ValueError(
            f"Invalid query parameter name rule {name!r}:"
            " it does not decode to valid UTF-8."
        ) from None
    return raw_name


def _decode_url_query_name(name: str) -> bytes:
    """Decode a URL query parameter name the way form data is decoded.

    ``+`` means a space, which is how canonical URLs spell spaces in the
    query. Percent escapes are decoded to raw bytes, so names that are not
    valid percent encoding or not valid UTF-8 decode to bytes that no rule
    can match.
    """
    return unquote_to_bytes(name.replace("+", " "))


def _decode_query_name_rules(names: Iterable[str], *, source: str) -> tuple[bytes, ...]:
    if isinstance(names, (str, bytes)):
        raise TypeError(f"{source} must be a list of strings, got {names!r}.")
    rules: set[bytes] = set()
    for name in names:
        if not isinstance(name, str):
            raise TypeError(f"{source} must contain only strings, got {name!r}.")
        rules.add(_decode_query_name_rule(name))
    return tuple(sorted(rules))


def _filter_query_params(url: str, ignored: tuple[bytes, ...]) -> str:
    """Drop query parameters whose decoded name is in *ignored*.

    Parameters are checked one at a time, so every occurrence of an ignored
    name goes away while other parameters keep their original spelling.
    Only the query part is rebuilt; the rest of the URL is copied verbatim.
    A URL with no matching parameter is returned unchanged.
    """
    fragment_start = url.find("#")
    if fragment_start < 0:
        url_without_fragment = url
        fragment = ""
    else:
        url_without_fragment = url[:fragment_start]
        fragment = url[fragment_start:]
    query_start = url_without_fragment.find("?")
    if query_start < 0:
        return url
    ignored_set = set(ignored)
    kept: list[str] = []
    removed = False
    for field in url_without_fragment[query_start + 1 :].split("&"):
        name, _, _ = field.partition("=")
        if field and _decode_url_query_name(name) in ignored_set:
            removed = True
            continue
        kept.append(field)
    if not removed:
        return url
    new_query = "&".join(kept)
    if new_query:
        return f"{url_without_fragment[: query_start + 1]}{new_query}{fragment}"
    return f"{url_without_fragment[:query_start]}{fragment}"


_fingerprint_cache: WeakKeyDictionary[
    Request,
    dict[
        tuple[tuple[bytes, ...] | None, bool, bool, str | None, tuple[bytes, ...]],
        bytes,
    ],
] = WeakKeyDictionary()


def fingerprint(
    request: Request,
    *,
    include_headers: Iterable[bytes | str] | None = None,
    keep_fragments: bool = False,
    ignore_query_params: Iterable[str] | None = None,
) -> bytes:
    """
    Return the request fingerprint.

    The request fingerprint is a hash that uniquely identifies the resource the
    request points to. For example, take the following two urls:
    ``http://www.example.com/query?id=111&cat=222``,
    ``http://www.example.com/query?cat=222&id=111``.

    Even though those are two different URLs both point to the same resource
    and are equivalent (i.e. they should return the same response).

    Another example are cookies used to store session ids. Suppose the
    following page is only accessible to authenticated users:
    ``http://www.example.com/members/offers.html``.

    Lots of sites use a cookie to store the session id, which adds a random
    component to the HTTP Request and thus should be ignored when calculating
    the fingerprint.

    For this reason, request headers are ignored by default when calculating
    the fingerprint. If you want to include specific headers use the
    include_headers argument, which is a list of Request headers to include.

    Also, servers usually ignore fragments in urls when handling requests,
    so they are also ignored by default when calculating the fingerprint.
    If you want to include them, set the keep_fragments argument to True
    (for instance when handling requests with a headless browser).

    keep_fragments is not a substitute for rendering JavaScript to reach
    content loaded based on the URL fragment; see
    :ref:`topics-dynamic-content` for that instead.

    *ignore_query_params* is a list of URL query parameter names that are left
    out of the fingerprint, which is useful for tracking parameters. Names
    are matched exactly and case-sensitively against the decoded name of each
    URL query parameter, and every occurrence of a matching name is dropped.
    Only the query part of the URL is affected, and the request itself is
    never modified. Rules are names, so a ``+`` is a literal plus sign, while
    a space in the URL may be spelled either ``+`` or ``%20``.
    """
    processed_ignore_query_params = (
        ()
        if ignore_query_params is None
        else _decode_query_name_rules(ignore_query_params, source="ignore_query_params")
    )
    return _fingerprint(
        request,
        include_headers=include_headers,
        keep_fragments=keep_fragments,
        ignore_query_params=processed_ignore_query_params,
    )


def _fingerprint(
    request: Request,
    *,
    include_headers: Iterable[bytes | str] | None = None,
    keep_fragments: bool = False,
    ignore_query_params: tuple[bytes, ...] = (),
) -> bytes:
    """Build the fingerprint, with query parameter rules already decoded."""
    processed_include_headers: tuple[bytes, ...] | None = None
    if include_headers:
        processed_include_headers = tuple(
            to_bytes(h.lower()) for h in sorted(include_headers)
        )
    verbatim_url = bool(request.meta.get("verbatim_url"))
    effective_keep_fragments = keep_fragments and not verbatim_url
    # A handler ID matching the URL scheme is the one that would be used
    # anyway, so it is left out to keep such fingerprints unchanged.
    handler_id: str | None = request.meta.get("download_handler")
    if handler_id is not None and handler_id == urlparse_cached(request).scheme:
        handler_id = None
    cache = _fingerprint_cache.setdefault(request, {})
    cache_key: tuple[
        tuple[bytes, ...] | None, bool, bool, str | None, tuple[bytes, ...]
    ] = (
        processed_include_headers,
        effective_keep_fragments,
        verbatim_url,
        handler_id,
        ignore_query_params,
    )
    if cache_key not in cache:
        # To decode bytes reliably (JSON does not support bytes), regardless of
        # character encoding, we use bytes.hex()
        headers: dict[str, list[str]] = {}
        if processed_include_headers:
            for header in processed_include_headers:
                if header in request.headers:
                    headers[header.hex()] = [
                        header_value.hex()
                        for header_value in request.headers.getlist(header)
                    ]
        url = request.url
        if ignore_query_params:
            url = _filter_query_params(url, ignore_query_params)
        if not verbatim_url:
            url = canonicalize_url(url, keep_fragments=keep_fragments)
        fingerprint_data = {
            "method": request.method,
            "url": url,
            "body": (request.body or b"").hex(),
            "headers": headers,
        }
        if handler_id is not None:
            fingerprint_data["download_handler"] = handler_id
        fingerprint_json = json.dumps(fingerprint_data, sort_keys=True)
        cache[cache_key] = hashlib.sha1(  # noqa: S324
            fingerprint_json.encode()
        ).digest()
    return cache[cache_key]


class RequestFingerprinterProtocol(Protocol):
    def fingerprint(self, request: Request) -> bytes: ...


class RequestFingerprinter:
    """Default fingerprinter.

    .. versionchanged:: 2.20.0
       :reqmeta:`download_handler` is taken into account.

    .. versionchanged:: VERSION
       :setting:`REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS` is taken into
       account.

    It takes into account a canonical version
    (:func:`w3lib.url.canonicalize_url`) of :attr:`request.url
    <scrapy.Request.url>` and the values of :attr:`request.method
    <scrapy.Request.method>` and :attr:`request.body
    <scrapy.Request.body>`, unless :reqmeta:`verbatim_url` is true for that
    request. It also takes into account :reqmeta:`download_handler` when it
    does not match the URL scheme. It then generates an `SHA1
    <https://en.wikipedia.org/wiki/SHA-1>`_ hash.

    :setting:`REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS` removes the listed
    URL query parameters from the URL before it is canonicalized, without
    changing the request itself.
    """

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler)

    def __init__(self, crawler: Crawler | None = None):
        configured = (
            crawler.settings.getlist("REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS")
            if crawler is not None
            else []
        )
        self._ignore_query_params = _decode_query_name_rules(
            configured,
            source="REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMS",
        )
        # Stored in the job directory to detect configuration changes. Hex
        # avoids depending on a text encoding for arbitrary parameter names.
        self._jobdir_state = {
            "ignore_query_params": [name.hex() for name in self._ignore_query_params]
        }
        self._fingerprint = _fingerprint

    def fingerprint(self, request: Request) -> bytes:
        return self._fingerprint(request, ignore_query_params=self._ignore_query_params)


def request_httprepr(request: Request) -> bytes:
    """Return the raw HTTP representation (as bytes) of the given request.
    This is provided only for reference since it's not the actual stream of
    bytes that will be send when performing the request (that's controlled
    by Twisted).
    """
    parsed = urlparse_cached(request)
    path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    s = f"{request.method} {path} HTTP/1.1\r\n".encode()
    s += f"Host: {parsed.hostname or ''}\r\n".encode()
    if headers := request.headers.to_string():
        s += headers + b"\r\n"
    s += b"\r\n"
    s += request.body
    return s


def referer_str(request: Request) -> str | None:
    """Return Referer HTTP header suitable for logging."""
    referrer = request.headers.get("Referer")
    if referrer is None:
        return referrer
    return referrer.decode(errors="replace")


def request_from_dict(d: dict[str, Any], *, spider: Spider | None = None) -> Request:
    """Create a :class:`~scrapy.Request` object from a dict.

    If a spider is given, it will try to resolve the callbacks looking at the
    spider for methods with the same name.
    """
    request_cls: type[Request] = load_object(d["_class"]) if "_class" in d else Request
    kwargs = {key: value for key, value in d.items() if key in request_cls.attributes}
    if d.get("callback") and spider:
        kwargs["callback"] = _get_method(spider, d["callback"])
    if d.get("errback") and spider:
        kwargs["errback"] = _get_method(spider, d["errback"])
    request = request_cls(**kwargs)
    if "id" in d:
        request._id = d["id"]
    return request


def _get_method(obj: Any, name: object) -> Any:
    """Helper function for request_from_dict"""
    name = str(name)
    try:
        return getattr(obj, name)
    except AttributeError:
        raise ValueError(f"Method {name!r} not found in: {obj!r}") from None


def _to_verbose_cookies(cookies: CookiesT) -> list[VerboseCookie]:
    """Return a list of verbose cookies from ``request.cookies``.

    The list of dicts form is returned as is, the dict one is converted first.
    """
    if isinstance(cookies, dict):
        return [{"name": k, "value": v} for k, v in cookies.items()]
    return cookies


def _decode_cookie(cookie: VerboseCookie, request: Request) -> dict[str, str] | None:
    """Return a dict with non-flag verbose cookie values converted to strings.

    ``name``, ``value``, ``path``, ``domain`` are included, ``secure`` isn't.
    """

    decoded = {}
    for key in ("name", "value", "path", "domain"):
        value = cookie.get(key)
        if value is None:
            if key in {"name", "value"}:
                logger.warning(
                    f"Invalid cookie found in request {request}:"
                    f" {cookie} ('{key}' is missing)"
                )
                return None
            continue
        if isinstance(value, (bool, float, int, str)):
            decoded[key] = str(value)
        else:
            assert isinstance(value, bytes)
            try:
                decoded[key] = value.decode()
            except UnicodeDecodeError:
                logger.warning(
                    f"Non UTF-8 encoded cookie found in request {request}: {cookie}",
                )
                decoded[key] = value.decode("latin1", errors="replace")
    return decoded


def request_to_curl(request: Request) -> str:
    """
    Converts a :class:`~scrapy.Request` object to a curl command.

    :param request: Request object to be converted
    :return: string containing the curl command
    """
    method = request.method

    data = f"--data-raw '{request.body.decode()}'" if request.body else ""

    header_args: list[str] = []
    for name, values in request.headers.items():
        str_name = name.decode(errors="replace")
        if not values:
            # "Remove an internal header by giving a replacement without
            # content on the right side of the colon, as in: -H "Host:"."
            # (which is how Scrapy handles empty headers)
            header_args.append(f"-H '{str_name}:'")
        for value in values:
            str_value = value.decode(errors="replace")
            header_args.append(f"-H '{str_name}: {str_value}'")
    headers = " ".join(header_args)

    url = request.url

    cookie_list: list[VerboseCookie] = _to_verbose_cookies(request.cookies)
    pairs = [
        f"{decoded['name']}={decoded['value']}"
        for c in cookie_list
        if (decoded := _decode_cookie(c, request)) is not None
    ]
    cookies = f"--cookie '{'; '.join(pairs)}'" if pairs else ""

    curl_cmd = f"curl -X {method} {url} {data} {headers} {cookies}".strip()
    return " ".join(curl_cmd.split())
