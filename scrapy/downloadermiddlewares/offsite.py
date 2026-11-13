from __future__ import annotations

import ipaddress
import logging
import re
from typing import TYPE_CHECKING, NamedTuple, Self

from scrapy import Request, Spider, signals
from scrapy.exceptions import IgnoreRequest
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.httpobj import urlparse_cached

if TYPE_CHECKING:
    from scrapy.crawler import Crawler
    from scrapy.statscollectors import StatsCollector


logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"ftp": 21, "http": 80, "https": 443, "ws": 80, "wss": 443}


class _AllowedHost(NamedTuple):
    host: str
    port: int | None
    subdomains: bool


def _normalize_host(host: str) -> str:
    host = host.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return ip.compressed
    return host.encode("idna").decode("ascii")


def _parse_allowed_host(entry: str) -> _AllowedHost:
    if not isinstance(entry, str):
        raise ValueError(f"allowed_hosts entries must be strings, got {entry!r}")
    original = entry
    entry = entry.strip()
    subdomains = entry.startswith("*.")
    if subdomains:
        entry = entry[2:]
    if not entry:
        raise ValueError(f"Invalid allowed_hosts entry {original!r}: empty host")
    if any(c in entry for c in "/@?#") or any(c.isspace() for c in entry):
        raise ValueError(
            f"Invalid allowed_hosts entry {original!r}: entries must not "
            "include a scheme, path, query, fragment or user info"
        )
    port: int | None = None
    if entry.startswith("["):
        host, closing, rest = entry[1:].partition("]")
        if not closing:
            raise ValueError(
                f"Invalid allowed_hosts entry {original!r}: missing closing "
                "bracket in IPv6 address"
            )
        if rest:
            if not rest.startswith(":"):
                raise ValueError(
                    f"Invalid allowed_hosts entry {original!r}: unexpected "
                    f"{rest!r} after IPv6 address"
                )
            port = _parse_port(rest[1:], original)
    else:
        if entry.count(":") > 1:
            raise ValueError(
                f"Invalid allowed_hosts entry {original!r}: IPv6 addresses "
                "must be enclosed in brackets"
            )
        host, colon, port_str = entry.partition(":")
        if colon:
            port = _parse_port(port_str, original)
    if not host or "*" in host:
        raise ValueError(
            f"Invalid allowed_hosts entry {original!r}: empty or wildcard host"
        )
    try:
        host = _normalize_host(host)
    except UnicodeError:
        raise ValueError(
            f"Invalid allowed_hosts entry {original!r}: invalid host name"
        ) from None
    return _AllowedHost(host, port, subdomains)


def _parse_port(value: str, entry: str) -> int:
    if not value.isascii() or not value.isdigit() or int(value) > 65535:
        raise ValueError(
            f"Invalid allowed_hosts entry {entry!r}: invalid port {value!r}"
        )
    return int(value)


class OffsiteMiddleware:
    """Filter out requests for URLs outside the domains covered by the spider.

    .. versionadded:: 2.11.2

    A request is allowed if its host name is in the
    :attr:`~scrapy.Spider.allowed_domains` attribute of the spider, or is a
    subdomain of one of those domains. E.g. ``www.example.org`` also allows
    ``bob.www.example.org``, but neither ``www2.example.org`` nor
    ``example.org``. See :meth:`should_follow` to use a different policy.

    If the spider does not define :attr:`~scrapy.Spider.allowed_domains`, or
    the attribute is empty, every request is allowed.

    .. versionchanged:: VERSION
       Added support for the ``allowed_hosts`` spider attribute, described
       below.

    If the spider defines a non-empty ``allowed_hosts`` attribute, a strict
    allow list is used instead of :attr:`~scrapy.Spider.allowed_domains`.
    Each entry of ``allowed_hosts`` is a host name or IP literal with an
    optional port, and an optional ``*.`` prefix that also allows
    subdomains:

    .. code-block:: python

        allowed_hosts = [
            "example.com",        # only example.com, any port
            "api.example.com:8443",  # only api.example.com on port 8443
            "*.example.org",      # example.org and its subdomains
            "[2001:db8::1]:8080",  # an IPv6 literal
        ]

    A request is allowed if its host name matches an entry and, when the
    entry includes a port, the request port matches it. A request URL
    without an explicit port uses the default port of its scheme (e.g. 80
    for ``http``, 443 for ``https``). Entries without a port allow any
    port. ``*.example.org`` matches ``example.org`` and
    ``www.example.org``, but not ``notexample.org`` or
    ``example.org.example.com``.

    Host names are normalized before matching: letter case and trailing
    dots are ignored, internationalized domain names are compared in their
    IDNA encoding, and IP literals are compared in their canonical form.
    Entries that include a scheme, path, query, fragment or user info, an
    invalid port, or an unbracketed IPv6 address raise :exc:`ValueError`.

    Filtered requests are logged as follows::

        DEBUG: Filtered offsite request to 'offsite.example': <GET http://offsite.example/some/page.html>

    Only the first request filtered for a given domain is logged, to keep the
    log readable.

    .. reqmeta:: allow_offsite

    allow_offsite
    -------------

    Requests with the ``allow_offsite`` :attr:`~scrapy.Request.meta` key set to
    ``True``, or with :attr:`~scrapy.Request.dont_filter` set to ``True``, are
    allowed regardless of their host name.
    """

    crawler: Crawler
    host_regex: re.Pattern[str]

    def __init__(self, stats: StatsCollector):
        self.stats = stats
        self.domains_seen: set[str] = set()
        self._allowed_domains: list[str] | None = None
        self._allowed_hosts: list[str] | None = None
        self._allowed_hosts_rules: list[_AllowedHost] | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        o = cls(crawler.stats)
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(o.request_scheduled, signal=signals.request_scheduled)
        o.crawler = crawler
        return o

    def spider_opened(self, spider: Spider) -> None:
        self._update_host_regex(spider)

    def _update_host_regex(self, spider: Spider) -> None:
        allowed_domains = list(getattr(spider, "allowed_domains", None) or [])
        allowed_hosts = getattr(spider, "allowed_hosts", None)
        if allowed_hosts is not None:
            allowed_hosts = list(allowed_hosts)
        if (
            allowed_domains == self._allowed_domains
            and allowed_hosts == self._allowed_hosts
        ):
            return
        self._allowed_domains = allowed_domains
        self._allowed_hosts = allowed_hosts
        self.host_regex = self.get_host_regex(spider)
        if allowed_hosts:
            self._allowed_hosts_rules = [
                _parse_allowed_host(entry) for entry in allowed_hosts
            ]
        else:
            self._allowed_hosts_rules = None

    def request_scheduled(self, request: Request, spider: Spider) -> None:
        self.process_request(request)

    @_warn_spider_arg
    def process_request(self, request: Request, spider: Spider | None = None) -> None:
        assert self.crawler.spider
        if (
            request.dont_filter
            or request.meta.get("allow_offsite")
            or self.should_follow(request, self.crawler.spider)
        ):
            return
        domain = urlparse_cached(request).hostname
        if domain and domain not in self.domains_seen:
            self.domains_seen.add(domain)
            logger.debug(
                "Filtered offsite request to %(domain)r: %(request)s",
                {"domain": domain, "request": request},
                extra={"spider": self.crawler.spider},
            )
            self.stats.inc_value("offsite/domains")
        self.stats.inc_value("offsite/filtered")
        raise IgnoreRequest(f"Filtered offsite request to {domain!r}")

    def should_follow(self, request: Request, spider: Spider) -> bool:
        """Return ``True`` if *request* is on site, ``False`` if it must be
        filtered out.

        Override this method to implement a different offsite policy. For
        example, to allow the domains in
        :attr:`~scrapy.Spider.allowed_domains` but none of their subdomains:

        .. code-block:: python

            from scrapy.downloadermiddlewares.offsite import OffsiteMiddleware
            from scrapy.utils.httpobj import urlparse_cached


            class RootOnlyOffsiteMiddleware(OffsiteMiddleware):
                def should_follow(self, request, spider):
                    return urlparse_cached(request).hostname in spider.allowed_domains
        """
        self._update_host_regex(spider)
        if self._allowed_hosts_rules is not None:
            return self._should_follow_allowed_hosts(request)
        regex = self.host_regex
        # hostname can be None for wrong urls (like javascript links)
        host = urlparse_cached(request).hostname or ""
        return bool(regex.search(host))

    def _should_follow_allowed_hosts(self, request: Request) -> bool:
        parsed = urlparse_cached(request)
        if not parsed.hostname:
            return False
        try:
            host = _normalize_host(parsed.hostname)
        except UnicodeError:
            return False
        port = parsed.port
        if port is None:
            port = _DEFAULT_PORTS.get(parsed.scheme)
        assert self._allowed_hosts_rules is not None
        for rule in self._allowed_hosts_rules:
            if rule.port is not None and rule.port != port:
                continue
            if host == rule.host or (
                rule.subdomains and host.endswith(f".{rule.host}")
            ):
                return True
        return False

    def get_host_regex(self, spider: Spider) -> re.Pattern[str]:
        allowed_domains = getattr(spider, "allowed_domains", None)
        if not allowed_domains:
            return re.compile("")  # allow all by default
        domains = []
        for domain in allowed_domains:
            if domain is None:
                continue
            domains.append(re.escape(domain))
        regex = rf"^(.*\.)?({'|'.join(domains)})$"
        return re.compile(regex)
