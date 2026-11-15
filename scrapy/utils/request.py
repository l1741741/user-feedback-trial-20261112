"""
This module provides some useful functions for working with
scrapy.Request objects
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol, Self
from urllib.parse import urlsplit, urlunparse, urlunsplit
from weakref import WeakKeyDictionary

from w3lib.url import canonicalize_url

from scrapy import Request, Spider
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import load_object
from scrapy.utils.python import to_bytes

if TYPE_CHECKING:
    from scrapy.crawler import Crawler
    from scrapy.http.request import CookiesT, VerboseCookie

logger = logging.getLogger(__name__)


_fingerprint_cache: WeakKeyDictionary[
    Request, dict[tuple[tuple[bytes, ...] | None, bool, bool, str | None], bytes]
] = WeakKeyDictionary()


def fingerprint(
    request: Request,
    *,
    include_headers: Iterable[bytes | str] | None = None,
    keep_fragments: bool = False,
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
    """
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
    cache_key = (
        processed_include_headers,
        effective_keep_fragments,
        verbatim_url,
        handler_id,
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
        if verbatim_url:
            url = request.url
        else:
            url = canonicalize_url(request.url, keep_fragments=keep_fragments)
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

    It takes into account a canonical version
    (:func:`w3lib.url.canonicalize_url`) of :attr:`request.url
    <scrapy.Request.url>` and the values of :attr:`request.method
    <scrapy.Request.method>` and :attr:`request.body
    <scrapy.Request.body>`, unless :reqmeta:`verbatim_url` is true for that
    request. It also takes into account :reqmeta:`download_handler` when it
    does not match the URL scheme. It then generates an `SHA1
    <https://en.wikipedia.org/wiki/SHA-1>`_ hash.
    """

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler)

    def __init__(self, crawler: Crawler | None = None):
        self._fingerprint = fingerprint

    def fingerprint(self, request: Request) -> bytes:
        return self._fingerprint(request)


def _parse_query_pairs(query: str) -> tuple[tuple[str, str, bool], ...]:
    """Split a query string into ``(key, value, has_equal_sign)`` triples.

    The text is split on the ``&`` separator without decoding percent-escapes,
    so repeated keys, empty values and the difference between a key without a
    value (``a``) and a key with an empty value (``a=``) are all preserved.
    """
    if not query:
        return ()
    pairs: list[tuple[str, str, bool]] = []
    for segment in query.split("&"):
        key, sep, value = segment.partition("=")
        pairs.append((key, value, bool(sep)))
    return tuple(pairs)


def _frame(buff: bytearray, field: bytes) -> None:
    """Append a length-prefixed field, so adjacent fields cannot be confused."""
    buff.extend(len(field).to_bytes(4, "big"))
    buff.extend(field)


def _validate_header_names(
    include_headers: Iterable[bytes | str] | None,
) -> tuple[bytes, ...]:
    if include_headers is None:
        return ()
    if isinstance(include_headers, (str, bytes)):
        raise TypeError(
            "include_headers must be an iterable of header names, not a single string"
        )
    if not isinstance(include_headers, Iterable):
        raise TypeError("include_headers must be an iterable of header names or None")
    names: set[bytes] = set()
    for header in include_headers:
        if not isinstance(header, (str, bytes)):
            raise TypeError(
                "include_headers entries must be str or bytes, got"
                f" {type(header).__name__}"
            )
        name = to_bytes(header).lower()
        if not name:
            raise ValueError("include_headers entries must not be empty")
        names.add(name)
    return tuple(sorted(names))


def _validate_query_names(
    names: Iterable[str] | None, setting_name: str
) -> tuple[str, ...]:
    if names is None:
        return ()
    if isinstance(names, str) or not isinstance(names, Iterable):
        raise TypeError(f"{setting_name} must be an iterable of strings or None")
    validated: set[str] = set()
    for name in names:
        if not isinstance(name, str):
            raise TypeError(
                f"{setting_name} entries must be str, got {type(name).__name__}"
            )
        if not name:
            raise ValueError(f"{setting_name} entries must not be empty")
        validated.add(name)
    return tuple(sorted(validated))


class VersionedRequestFingerprinter:
    """Configurable, versioned request fingerprinter.

    .. versionadded:: VERSION

    Unlike :class:`RequestFingerprinter`, this fingerprinter does not
    canonicalize the URL. It builds each fingerprint from explicit, framed
    components: the request method and body, the URL split into its parts,
    selected request headers, and a version marker. That makes two behaviors
    configurable:

    - *include_headers* names the headers that participate in the fingerprint,
      matched case-insensitively. This distinguishes requests that return
      different content for the same URL depending on headers such as
      ``Accept-Language`` or ``X-Tenant``.
    - *ignore_query_parameters* and *ignore_query_parameter_namespaces* drop
      tracking query parameters by name. Names are matched after parsing the
      query into key/value pairs, so a name only matches itself or, for a
      namespace, a key that starts with the namespace followed by more
      characters; percent-escaped text and other keys are never touched.

    Query parameters keep their original order, including repeated keys, and
    empty values keep their meaning. Every field is prefixed with its length,
    so concatenated fields cannot be ambiguous.

    The fingerprint is a 32-byte SHA-256 digest, so it never collides with the
    20-byte SHA-1 digests of :class:`RequestFingerprinter`; fingerprints
    stored by the duplicate filter or the HTTP cache from a previous
    configuration therefore cannot be mistaken for new ones. The same is true
    when the framing :data:`VERSION` or *namespace* change: changing
    *namespace* is the intended way to separate crawls or configurations on
    purpose.

    The configuration is copied and validated in the constructor, so it
    cannot be mutated through the objects passed by the caller later. Each
    instance keeps its own cache keyed by request, and caching only stores
    successful results. Requests are never modified and header values are
    never logged.
    """

    #: Prefix of every fingerprint input. Bump it if this framing changes.
    VERSION = 1
    _MAGIC = b"SP"

    def __init__(
        self,
        crawler: Crawler | None = None,
        *,
        include_headers: Iterable[bytes | str] | None = None,
        ignore_query_parameters: Iterable[str] | None = None,
        ignore_query_parameter_namespaces: Iterable[str] | None = None,
        namespace: str = "",
    ) -> None:
        if not isinstance(namespace, str):
            raise TypeError("namespace must be a string")
        self._include_headers = _validate_header_names(include_headers)
        self._ignore_query_parameters = _validate_query_names(
            ignore_query_parameters, "ignore_query_parameters"
        )
        self._ignore_query_parameter_namespaces = _validate_query_names(
            ignore_query_parameter_namespaces,
            "ignore_query_parameter_namespaces",
        )
        self._namespace = namespace
        self._cache: WeakKeyDictionary[Request, bytes] = WeakKeyDictionary()

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        settings = crawler.settings
        return cls(
            crawler,
            include_headers=settings.getlist("REQUEST_FINGERPRINTER_INCLUDE_HEADERS")
            or None,
            ignore_query_parameters=settings.getlist(
                "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMETERS"
            )
            or None,
            ignore_query_parameter_namespaces=settings.getlist(
                "REQUEST_FINGERPRINTER_IGNORE_QUERY_PARAMETER_NAMESPACES"
            )
            or None,
            namespace=settings.get("REQUEST_FINGERPRINTER_NAMESPACE", ""),
        )

    @property
    def include_headers(self) -> tuple[bytes, ...]:
        """Names of the headers that participate in fingerprints."""
        return self._include_headers

    @property
    def ignore_query_parameters(self) -> tuple[str, ...]:
        """Query parameter names excluded from fingerprints."""
        return self._ignore_query_parameters

    @property
    def ignore_query_parameter_namespaces(self) -> tuple[str, ...]:
        """Query parameter name prefixes excluded from fingerprints."""
        return self._ignore_query_parameter_namespaces

    @property
    def namespace(self) -> str:
        """Partition label mixed into every fingerprint input."""
        return self._namespace

    def _filter_query(self, query: str) -> str:
        exact = set(self._ignore_query_parameters)
        namespaces = tuple(self._ignore_query_parameter_namespaces)
        kept: list[str] = []
        for key, value, has_equal_sign in _parse_query_pairs(query):
            if key in exact or any(
                key.startswith(prefix) and len(key) > len(prefix)
                for prefix in namespaces
            ):
                continue
            kept.append(f"{key}={value}" if has_equal_sign else key)
        return "&".join(kept)

    def fingerprint(self, request: Request) -> bytes:
        cached = self._cache.get(request)
        if cached is not None:
            return cached

        parts = urlsplit(request.url)
        query = self._filter_query(parts.query)
        filtered_url = urlunsplit(parts._replace(query=query, fragment=""))

        buff = bytearray()
        buff.extend(self._MAGIC)
        buff.extend(self.VERSION.to_bytes(2, "big"))
        _frame(buff, self._namespace.encode())
        _frame(buff, to_bytes(request.method))
        _frame(buff, filtered_url.encode())
        _frame(buff, request.body or b"")
        for header in self._include_headers:
            if header in request.headers:
                framed_values = b"".join(
                    len(value).to_bytes(4, "big") + value
                    for value in request.headers.getlist(header)
                )
                _frame(buff, header)
                _frame(buff, framed_values)

        digest = hashlib.sha256(buff).digest()
        self._cache[request] = digest
        return digest


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
