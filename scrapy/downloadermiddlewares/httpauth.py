"""
HTTP basic auth downloader middleware

See documentation in docs/topics/downloader-middleware.rst
"""

from __future__ import annotations

import functools
import ipaddress
import warnings
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Self, TypeAlias
from urllib.parse import urlsplit

from w3lib.http import basic_auth_header
from w3lib.url import safe_url_string

from scrapy import Request, Spider, signals
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.settings import SETTINGS_PRIORITIES
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.url import url_is_from_any_domain

if TYPE_CHECKING:
    from scrapy.crawler import Crawler
    from scrapy.http import Response


_DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
}

# Scheme, canonical host and port.
_Origin: TypeAlias = tuple[str, str, int]


@functools.lru_cache(maxsize=1024)
def _normalize_host(host: str) -> str:
    """Return *host* in the canonical form used for origin comparisons.

    The result is lower-case ASCII, with internationalized domain names
    converted to punycode and IPv6 addresses compressed. Trailing dots, which
    mark a fully qualified domain name, are dropped.
    """
    if ":" in host:
        return ipaddress.IPv6Address(host).compressed
    if any(ch in host for ch in "/@?#\\"):
        raise ValueError(f"invalid host: {host!r}")
    parsed = urlsplit(safe_url_string(f"http://{host}/"))
    # Hosts that would make the serialization end the authority early were
    # rejected above, so the path cannot grow.
    if parsed.path != "/" or parsed.query or parsed.fragment:  # pragma: no cover
        raise ValueError(f"invalid host: {host!r}")
    normalized = (parsed.hostname or "").removesuffix(".")
    if not normalized or normalized.startswith("."):
        raise ValueError(f"invalid host: {host!r}")
    return normalized


def _parse_origin(item: object, source: str, index: int) -> _Origin:
    where = f"{source}, item {index}"
    if not isinstance(item, str):
        raise ValueError(f"{where} must be a string, got {item!r}.")
    if not item or item != item.strip() or any(ch.isspace() for ch in item):
        raise ValueError(f"{where} is not an absolute origin: {item!r}.")
    try:
        parsed = urlsplit(item)
    except ValueError as exc:
        raise ValueError(f"{where} is not a valid origin: {item!r}.") from exc
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError(f"{where} must use the http or https scheme: {item!r}.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{where} must not contain user information: {item!r}.")
    if parsed.path or parsed.query or parsed.fragment or "?" in item or "#" in item:
        raise ValueError(
            f"{where} must not contain a path, a query or a fragment: {item!r}."
        )
    if not parsed.hostname:
        raise ValueError(f"{where} is missing a host: {item!r}.")
    if parsed.netloc.endswith(":"):
        raise ValueError(f"{where} has an empty port: {item!r}.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{where} has an invalid port: {item!r}.") from exc
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    elif not 0 < port < 65536:
        raise ValueError(f"{where} has an invalid port: {item!r}.")
    try:
        host = _normalize_host(parsed.hostname)
    except ValueError as exc:
        raise ValueError(f"{where} has an invalid host: {item!r}.") from exc
    return scheme, host, port


def _parse_origins(value: object, source: str) -> frozenset[_Origin]:
    """Return the origins in *value* as a set of normalized tuples.

    *value* is not modified. Invalid values raise :exc:`ValueError`.
    """
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        raise ValueError(f"{source} must be a list of origins, got {value!r}.")
    return frozenset(
        _parse_origin(item, source, index) for index, item in enumerate(value)
    )


def _request_origin(request: Request) -> _Origin | None:
    """Return the normalized origin of *request*, if it has a valid one."""
    parsed_url = urlparse_cached(request)
    scheme = parsed_url.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    try:
        host = parsed_url.hostname
        if not host:
            return None
        port = parsed_url.port
        if port is None:
            port = _DEFAULT_PORTS[scheme]
        return scheme, _normalize_host(host), port
    except ValueError:
        return None


def _origin(request: Request) -> str:
    parsed_url = urlparse_cached(request)
    scheme = parsed_url.scheme
    netloc = (
        parsed_url.netloc
        if parsed_url.port != _DEFAULT_PORTS[scheme]
        else parsed_url.hostname
    )
    return f"{scheme}://{netloc}"


def _setdefault_auth_origin(request: Request) -> str:
    origin: str | None = request.meta.get("_auth_origin")
    if origin:
        return origin
    origin = _origin(request)
    request.meta["_auth_origin"] = origin
    return origin


def _spider_origins(crawler: Crawler) -> object | None:
    """Return the ``http_auth_origins`` value configured on the spider.

    Prefer the spider instance, which may define the attribute in
    ``__init__()``, and fall back to its class, which is all that is available
    when the middleware is built before the spider.
    """
    spider = crawler.spider
    if spider is not None:
        origins = getattr(spider, "http_auth_origins", None)
        if origins is not None:
            return origins
    return getattr(crawler.spidercls, "http_auth_origins", None)


class HttpAuthMiddleware:
    """Set Basic HTTP Authorization header."""

    def __init__(self) -> None:
        # Configuration in effect for the open spider, reset in
        # spider_opened() so that a spider never reuses another spider's
        # credentials or origins.
        self._auth: bytes | None = None
        self._domain: str | None = None
        self._origins: frozenset[_Origin] | None = None
        # Configuration from the settings, which every spider starts from.
        self._settings_auth: bytes | None = None
        self._settings_domain: str | None = None
        self._settings_origins: frozenset[_Origin] | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        o = cls()
        origins = crawler.settings.get("HTTPAUTH_ORIGINS")
        if origins is not None:
            o._settings_origins = _parse_origins(origins, "HTTPAUTH_ORIGINS")
        # Validate the spider configuration here, where an error stops the
        # crawl, instead of in spider_opened(), where the engine only logs it.
        spider_origins = _spider_origins(crawler)
        if spider_origins is not None:
            _parse_origins(spider_origins, "http_auth_origins")
        usr = crawler.settings.get("HTTPAUTH_USER", "")
        pwd = crawler.settings.get("HTTPAUTH_PASS", "")
        if usr or pwd:
            domain_priority = crawler.settings.getpriority("HTTPAUTH_DOMAIN") or 0
            # Settings credentials apply to every spider, so a spider attribute
            # cannot restrict them.
            if (
                domain_priority <= SETTINGS_PRIORITIES["default"]
                and o._settings_origins is None
            ):
                raise ValueError(
                    "HTTPAUTH_DOMAIN or HTTPAUTH_ORIGINS must be set when "
                    "HTTPAUTH_USER or HTTPAUTH_PASS is configured. Set HTTPAUTH_DOMAIN "
                    "to a domain (e.g. 'example.com') or HTTPAUTH_ORIGINS to a list of "
                    "origins (e.g. ['https://example.com']) to restrict credentials to "
                    "them, or set HTTPAUTH_DOMAIN to None to send credentials with all "
                    "requests."
                )
            o._settings_auth = basic_auth_header(usr, pwd)
            o._settings_domain = crawler.settings.get("HTTPAUTH_DOMAIN")
        o._reset()
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        return o

    def _reset(self) -> None:
        self._auth = self._settings_auth
        self._domain = self._settings_domain
        self._origins = self._settings_origins

    def spider_opened(self, spider: Spider) -> None:
        # Start from the settings so that no configuration of a previously
        # opened spider can leak into this one.
        self._reset()
        spider_origins = getattr(spider, "http_auth_origins", None)
        if spider_origins is not None:
            self._origins = _parse_origins(spider_origins, "http_auth_origins")
            self._domain = None
        usr = getattr(spider, "http_user", "")
        pwd = getattr(spider, "http_pass", "")
        if usr or pwd:
            warnings.warn(
                "Use the HTTPAUTH_USER, HTTPAUTH_PASS, HTTPAUTH_DOMAIN, and "
                "HTTPAUTH_ORIGINS settings instead of the http_user, http_pass, "
                "and http_auth_domain spider attributes. Support for the spider "
                "attributes will be removed in a future version of Scrapy.",
                category=ScrapyDeprecationWarning,
                stacklevel=2,
            )
            self._auth = basic_auth_header(usr, pwd)
            if spider_origins is None:
                if hasattr(spider, "http_auth_domain"):
                    self._domain = spider.http_auth_domain  # type: ignore[attr-defined]
                    self._origins = None
                elif self._origins is None:
                    # Deprecated spider credentials still require an explicit
                    # restriction, as they did before HTTPAUTH_ORIGINS.
                    raise AttributeError(
                        "http_auth_domain or http_auth_origins must be defined if "
                        "http_user or http_pass are."
                    )

    @_warn_spider_arg
    def process_request(
        self, request: Request, spider: Spider | None = None
    ) -> Request | Response | None:
        if urlparse_cached(request).scheme not in _DEFAULT_PORTS:
            return None
        # Per-request meta overrides
        usr = request.meta.get("http_user", "")
        pwd = request.meta.get("http_pass", "")
        if usr or pwd:
            domain = request.meta.get("http_auth_domain")
            allowed = (
                url_is_from_any_domain(request, [domain])
                if domain
                else _setdefault_auth_origin(request) == _origin(request)
            )
            if allowed:
                request.headers.setdefault(
                    b"Authorization", basic_auth_header(usr, pwd)
                )
            return None
        # Middleware-level auth
        if not self._auth:
            return None
        if self._origins is not None:
            if _request_origin(request) in self._origins:
                request.headers.setdefault(b"Authorization", self._auth)
        elif not self._domain or url_is_from_any_domain(request, [self._domain]):
            request.headers.setdefault(b"Authorization", self._auth)
        return None
