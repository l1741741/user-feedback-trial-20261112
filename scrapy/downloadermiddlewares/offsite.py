from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
from collections import OrderedDict
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, Self, cast
from weakref import WeakKeyDictionary

from scrapy import Request, Spider, signals
from scrapy.exceptions import CloseSpider, IgnoreRequest
from scrapy.utils.decorators import _warn_spider_arg
from scrapy.utils.httpobj import urlparse_cached

if TYPE_CHECKING:
    from urllib.parse import ParseResult

    from scrapy.crawler import Crawler
    from scrapy.statscollectors import StatsCollector


logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"ftp": 21, "http": 80, "https": 443, "ws": 80, "wss": 443}
# Request.meta key where the middleware records which rule snapshot a request
# was evaluated with. The middleware sets it; user code must not.
_RULES_VERSION_META_KEY = "offsite_rules_version"
# Part of every version identifier, so that changing how identifiers are
# computed cannot silently reuse the identifiers of older snapshots.
_VERSION_FORMAT = "offsite-rules-1"
# Recent snapshots are kept so that requests stamped before a reload keep
# being evaluated with the rules they were admitted under. Older identifiers
# become unknown and their requests are filtered.
_SNAPSHOT_HISTORY_SIZE = 4


@dataclass(frozen=True)
class _Rule:
    """A normalized offsite rule."""

    host: str
    port: int | None
    subdomains: bool

    def __str__(self) -> str:
        # IPv6 addresses are kept in brackets, as in URLs and rule entries.
        host = f"[{self.host}]" if ":" in self.host else self.host
        prefix = "*." if self.subdomains else ""
        port = f":{self.port}" if self.port is not None else ""
        return f"{prefix}{host}{port}"


@dataclass(frozen=True)
class _RuleSnapshot:
    """An immutable set of rules and the identifier of that set."""

    version: str
    rules: tuple[_Rule, ...]


class _SpiderRuleSnapshots:
    """The published snapshots of the rules of one spider."""

    def __init__(self) -> None:
        self.history: OrderedDict[str, _RuleSnapshot] = OrderedDict()
        self.current: _RuleSnapshot | None = None

    def publish(self, snapshot: _RuleSnapshot) -> None:
        if snapshot.version in self.history:
            self.history.move_to_end(snapshot.version)
            self.current = self.history[snapshot.version]
            return
        self.history[snapshot.version] = snapshot
        while len(self.history) > _SNAPSHOT_HISTORY_SIZE:
            self.history.popitem(last=False)
        self.current = snapshot


def _invalid_rule(entry: object, reason: str) -> ValueError:
    return ValueError(f"Invalid offsite rule {entry!r}: {reason}")


def _parse_port(value: str, entry: object) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise _invalid_rule(entry, f"{value!r} is not a valid port")
    port = int(value)
    if not 0 < port <= 65535:
        raise _invalid_rule(entry, f"{value!r} is not a valid port")
    return port


def _normalize_dns_name(host: str, entry: object) -> str:
    # A single trailing dot marks the root and is not part of the name.
    host = host.removesuffix(".")
    if any(not label for label in host.split(".")):
        raise _invalid_rule(entry, "host names must not contain empty labels")
    try:
        normalized = host.lower().encode("idna").decode("ascii")
    except UnicodeError:
        raise _invalid_rule(entry, "the host name is not a valid domain name") from None
    for label in normalized.split("."):
        if not re.fullmatch(r"[a-z0-9_-]+", label):
            raise _invalid_rule(entry, "the host name has invalid characters")
        if label.startswith("-") or label.endswith("-"):
            raise _invalid_rule(entry, "a host name label starts or ends with '-'")
    if len(normalized) > 253:
        raise _invalid_rule(entry, "the host name is too long")
    return normalized


def _parse_ipv6_rule(entry: object, host_entry: str, subdomains: bool) -> _Rule:
    closing = host_entry.find("]")
    if closing < 0:
        raise _invalid_rule(entry, "the IPv6 address is missing its closing ']'")
    if subdomains:
        raise _invalid_rule(entry, "subdomain rules cannot apply to an IP address")
    rest = host_entry[closing + 1 :]
    if rest and not rest.startswith(":"):
        raise _invalid_rule(entry, f"unexpected {rest!r} after the IPv6 address")
    port = _parse_port(rest[1:], entry) if rest else None
    try:
        host = ipaddress.IPv6Address(host_entry[1:closing]).compressed
    except ValueError:
        raise _invalid_rule(
            entry, "the bracketed address is not a valid IPv6 address"
        ) from None
    return _Rule(host, port, False)


def _parse_host_rule(entry: object, host_entry: str, subdomains: bool) -> _Rule:
    if host_entry.count(":") > 1:
        raise _invalid_rule(entry, "IPv6 addresses must be enclosed in brackets")
    host, colon, port_text = host_entry.partition(":")
    if not host:
        raise _invalid_rule(entry, "the rule has no host")
    port = _parse_port(port_text, entry) if colon else None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return _Rule(_normalize_dns_name(host, entry), port, subdomains)
    if subdomains:
        raise _invalid_rule(entry, "subdomain rules cannot apply to an IP address")
    return _Rule(address.compressed, port, False)


def _parse_rule(entry: object) -> _Rule:
    if not isinstance(entry, str):
        raise TypeError(
            f"Offsite rules must be strings, got {type(entry).__name__}: {entry!r}"
        )
    if not entry:
        raise _invalid_rule(entry, "the rule is empty")
    if entry != entry.strip() or any(char.isspace() for char in entry):
        raise _invalid_rule(entry, "the rule must not contain whitespace")
    if "://" in entry:
        raise _invalid_rule(entry, "the rule must not include a scheme")
    if any(char in entry for char in "/@?#"):
        raise _invalid_rule(
            entry, "the rule must not include a path, query, fragment or user info"
        )
    subdomains = entry.startswith("*.")
    host_entry = entry[2:] if subdomains else entry
    if subdomains and not host_entry:
        raise _invalid_rule(entry, "the rule has no host after '*.'")
    if not subdomains and "*" in entry:
        raise _invalid_rule(
            entry, "only a leading '*.' subdomain wildcard is supported"
        )
    if host_entry.startswith("["):
        return _parse_ipv6_rule(entry, host_entry, subdomains)
    return _parse_host_rule(entry, host_entry, subdomains)


def _compile_snapshot(rules: Iterable[str]) -> _RuleSnapshot:
    """Validate *rules* and return the snapshot they describe.

    Every rule is parsed before the snapshot is returned, so a failure leaves
    no partial snapshot behind. Rules are deduplicated and sorted, which makes
    the version identifier independent of the rule order.
    """
    if isinstance(rules, str | bytes | bytearray) or not isinstance(rules, Iterable):
        raise TypeError(
            f"Offsite rules must be an iterable of strings, got {type(rules).__name__}"
        )
    parsed = {_parse_rule(entry) for entry in rules}
    ordered = tuple(sorted(parsed, key=str))
    payload = "\n".join(str(rule) for rule in ordered)
    digest = hashlib.sha256(f"{_VERSION_FORMAT}\n{payload}".encode()).hexdigest()
    return _RuleSnapshot(digest, ordered)


def _spider_rules(spider: Spider) -> Iterable[str]:
    rules: object = getattr(spider, "allowed_domains", None)
    if rules is None:
        return ()
    if isinstance(rules, str | bytes):
        raise TypeError(
            "Spider.allowed_domains must be a collection of strings, not a single"
            f" {type(rules).__name__}"
        )
    return cast("Iterable[str]", rules)


def _normalize_request_host(host: str) -> str:
    """Return the canonical form of a request host name.

    Letter case and a trailing dot are ignored, internationalized names are
    returned in their IDNA form, and IP addresses in their compressed form.
    A host that cannot be normalized raises :exc:`ValueError` or
    :exc:`UnicodeError`.
    """
    if not host:
        raise ValueError("empty host")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    return host.removesuffix(".").lower().encode("idna").decode("ascii")


def _request_port(parsed: ParseResult) -> int | None:
    """Return the port of *parsed*, using the scheme default when omitted."""
    try:
        port = parsed.port
    except ValueError:
        # An invalid port cannot match a rule that has one, and does not
        # affect a rule that does not.
        return -1
    if port is not None:
        return port
    return _DEFAULT_PORTS.get(parsed.scheme.lower())


def _snapshot_allows(snapshot: _RuleSnapshot, request: Request) -> bool:
    if not snapshot.rules:
        return True
    parsed = urlparse_cached(request)
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        host = _normalize_request_host(hostname)
    except (UnicodeError, ValueError):
        return False
    port = _request_port(parsed)
    for rule in snapshot.rules:
        if rule.port is not None and rule.port != port:
            continue
        if rule.subdomains:
            if host != rule.host and host.endswith(f".{rule.host}"):
                return True
        elif host == rule.host:
            return True
    return False


class OffsiteMiddleware:
    """Filter out requests for URLs outside the domains covered by the spider.

    .. versionadded:: 2.11.2

    A request is allowed if its host name is in the
    :attr:`~scrapy.Spider.allowed_domains` attribute of the spider, or is a
    subdomain of one of those domains. E.g. ``www.example.org`` also allows
    ``bob.www.example.org``, but neither ``www2.example.org`` nor
    ``example.org``. See :meth:`should_follow` to use a different policy.

    .. versionchanged:: VERSION
       Added :setting:`OFFSITE_RULES_SNAPSHOT`, rule snapshots, versioned
       requests and :meth:`reload_rules`, described below.

    If the spider does not define :attr:`~scrapy.Spider.allowed_domains`, or
    the attribute is empty, every request is allowed.

    Rule snapshots
    --------------

    By default, :attr:`~scrapy.Spider.allowed_domains` is read again for
    every request, so changing it affects requests scheduled afterwards. When
    :setting:`OFFSITE_RULES_SNAPSHOT` is enabled, the rules are instead
    validated and frozen when the spider opens, and later changes to the
    spider attribute have no effect until :meth:`reload_rules` publishes a
    new snapshot.

    In snapshot mode the rules use a stricter syntax than the default
    matching:

    -   ``example.com`` allows requests to exactly ``example.com``.
    -   ``*.example.com`` allows requests to subdomains of ``example.com``,
        such as ``www.example.com``, but not to ``example.com`` itself.
    -   A rule can end with an optional port, e.g. ``example.com:8080`` or
        ``*.example.com:443``, in which case only requests to that port
        match. A request without an explicit port uses the default port of
        its scheme. A rule without a port matches every port.
    -   IPv6 addresses are enclosed in brackets, e.g. ``[2001:db8::1]`` or
        ``[2001:db8::1]:8080``, and match exactly.

    Host names are normalized before matching: letter case and a trailing dot
    are ignored, internationalized domain names are compared in their IDNA
    form, and IPv6 addresses are compared in their compressed form. A rule
    that includes a scheme, a path, a query, a fragment, user info, an empty
    label, a wildcard other than a leading ``*.``, or an invalid port raises
    :exc:`ValueError` when the spider opens or when :meth:`reload_rules` is
    called. No rule is published in that case.

    Each published snapshot has an opaque version identifier, returned by
    :meth:`reload_rules` and :meth:`get_rules_version`. Requests are stamped
    with it in the :reqmeta:`offsite_rules_version` request meta key. The
    identifier is read-only for the middleware: it is added to requests that
    do not carry one, but an identifier a request already carries is never
    replaced, so a request keeps being evaluated with its snapshot even after
    a reload and a batch of requests does not change its verdict halfway
    through. A request carrying an identifier that is unknown, because it was
    never published or because it is older than the retained history, is
    filtered with an explanatory message. The identifier alone never grants
    access: such a request still has to match the rules of its snapshot.

    The identifier is derived from the normalized rules, so the same rules
    always have the same identifier, also in another process. Requests
    persisted in a job directory can therefore resume with the same rules,
    and are filtered with an explanatory message if the rules they were
    admitted under are no longer published.

    Filtered requests are logged as follows::

        DEBUG: Filtered offsite request to 'offsite.example': <GET http://offsite.example/some/page.html>

    Only the first request filtered for a given domain is logged, to keep the
    log readable. In snapshot mode domains are normalized first, so
    equivalent spellings of the same host count and log once.

    .. reqmeta:: allow_offsite

    allow_offsite
    -------------

    Requests with the ``allow_offsite`` :attr:`~scrapy.Request.meta` key set to
    ``True``, or with :attr:`~scrapy.Request.dont_filter` set to ``True``, are
    allowed regardless of their host name. These two checks keep their
    precedence over rule snapshots: they are evaluated before the version
    identifier is looked up, so even a request carrying an unknown identifier
    passes when one of them is set. No other meta key bypasses the rules.
    """

    crawler: Crawler
    host_regex: re.Pattern[str]

    def __init__(self, stats: StatsCollector, *, snapshot: bool = False):
        self.stats = stats
        self.domains_seen: set[str] = set()
        self._allowed_domains: list[str] | None = None
        self._snapshot_mode = snapshot
        self._snapshots: WeakKeyDictionary[Spider, _SpiderRuleSnapshots] = (
            WeakKeyDictionary()
        )

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        o = cls(
            crawler.stats,
            snapshot=crawler.settings.getbool("OFFSITE_RULES_SNAPSHOT"),
        )
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(o.request_scheduled, signal=signals.request_scheduled)
        o.crawler = crawler
        return o

    def spider_opened(self, spider: Spider) -> None:
        if not self._snapshot_mode:
            self._update_host_regex(spider)
            return
        try:
            self._install_rules(spider, _spider_rules(spider))
        except (TypeError, ValueError) as exc:
            # A signal handler cannot abort the crawl by raising, so ask for
            # the spider to be closed as failed instead of letting every
            # request fail later for lack of a snapshot.
            logger.error(
                f"Invalid offsite rules, closing the spider: {exc}",
                extra={"spider": spider},
            )
            raise CloseSpider("offsite_rules_invalid", error=True) from exc

    def _install_rules(self, spider: Spider, rules: Iterable[str]) -> _RuleSnapshot:
        # Compile before touching any state, so that an invalid rule leaves
        # the previously published snapshots exactly as they were.
        snapshot = _compile_snapshot(rules)
        state = self._snapshots.get(spider)
        if state is None:
            state = self._snapshots[spider] = _SpiderRuleSnapshots()
        state.publish(snapshot)
        return snapshot

    def reload_rules(self, spider: Spider, rules: Iterable[str] | None = None) -> str:
        """Validate *rules*, publish them atomically, and return their version
        identifier.

        *rules* is an iterable of rule strings, in the syntax described in the
        class documentation. If ``None``, the current value of the spider's
        :attr:`~scrapy.Spider.allowed_domains` is used.

        All rules are validated before any of them becomes active. If a rule
        is invalid, :exc:`ValueError` (or :exc:`TypeError` for a non-string
        entry) is raised and the current snapshot stays in place; a new
        snapshot is never partially published.

        This method raises :exc:`RuntimeError` unless
        :setting:`OFFSITE_RULES_SNAPSHOT` is enabled.
        """
        if not self._snapshot_mode:
            raise RuntimeError(
                "reload_rules() requires the OFFSITE_RULES_SNAPSHOT setting"
            )
        if rules is None:
            rules = _spider_rules(spider)
        return self._install_rules(spider, rules).version

    def get_rules_version(self, spider: Spider | None = None) -> str | None:
        """Return the version identifier of the current rule snapshot of
        *spider*, which defaults to the spider being crawled.

        Return ``None`` if snapshot mode is disabled or no snapshot has been
        published yet.
        """
        if not self._snapshot_mode:
            return None
        if spider is None:
            if self.crawler.spider is None:
                return None
            spider = self.crawler.spider
        state = self._snapshots.get(spider)
        if state is None or state.current is None:
            return None
        return state.current.version

    def _update_host_regex(self, spider: Spider) -> None:
        allowed_domains = list(getattr(spider, "allowed_domains", None) or [])
        if allowed_domains != self._allowed_domains:
            self._allowed_domains = allowed_domains
            self.host_regex = self.get_host_regex(spider)

    def _state_for(self, spider: Spider) -> _SpiderRuleSnapshots:
        state = self._snapshots.get(spider)
        if state is None or state.current is None:
            raise RuntimeError(
                "Offsite rule snapshots are not initialized for this spider;"
                " OffsiteMiddleware.spider_opened() has not run yet"
            )
        return state

    def _select_snapshot(self, request: Request, spider: Spider) -> _RuleSnapshot:
        """Return the snapshot to evaluate *request* with.

        Stamp *request* with the current version identifier if it does not
        carry one, and reject it if the identifier it carries is unknown or
        is not a string.
        """
        state = self._state_for(spider)
        if _RULES_VERSION_META_KEY not in request.meta:
            assert state.current is not None
            request.meta[_RULES_VERSION_META_KEY] = state.current.version
            return state.current
        version = request.meta[_RULES_VERSION_META_KEY]
        snapshot = state.history.get(version) if isinstance(version, str) else None
        if snapshot is None:
            self._reject(
                request,
                spider,
                detail=f"unknown offsite rules version {version!r}",
            )
        return snapshot

    def request_scheduled(self, request: Request, spider: Spider) -> None:
        self.process_request(request)

    @_warn_spider_arg
    def process_request(self, request: Request, spider: Spider | None = None) -> None:
        assert self.crawler.spider
        current_spider = self.crawler.spider
        if request.dont_filter or request.meta.get("allow_offsite"):
            return
        if self._snapshot_mode:
            # Validate the stamped identifier, and stamp requests that do not
            # carry one, before the policy runs, so that a custom
            # should_follow() cannot make an unknown version pass.
            self._select_snapshot(request, current_spider)
        if not self.should_follow(request, current_spider):
            self._reject(request, current_spider)

    def _reject(
        self, request: Request, spider: Spider, *, detail: str | None = None
    ) -> NoReturn:
        domain = urlparse_cached(request).hostname
        if domain and self._snapshot_mode:
            with suppress(UnicodeError, ValueError):
                domain = _normalize_request_host(domain)
        if domain and domain not in self.domains_seen:
            self.domains_seen.add(domain)
            logger.debug(
                "Filtered offsite request to %(domain)r: %(request)s",
                {"domain": domain, "request": request},
                extra={"spider": spider},
            )
            self.stats.inc_value("offsite/domains")
        self.stats.inc_value("offsite/filtered")
        message = f"Filtered offsite request to {domain!r}"
        if detail:
            message = f"{message}: {detail}"
        raise IgnoreRequest(message)

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

        In snapshot mode the default implementation matches the request
        against the snapshot selected by its :reqmeta:`offsite_rules_version`
        request meta key, or against the current snapshot when the key is
        absent. It raises :exc:`~scrapy.exceptions.IgnoreRequest` for an
        unknown version.

        :meth:`process_request` also stamps the request with the version
        identifier it is evaluated with, and validates it there before
        calling this method.
        """
        if self._snapshot_mode:
            return _snapshot_allows(self._select_snapshot(request, spider), request)
        self._update_host_regex(spider)
        regex = self.host_regex
        # hostname can be None for wrong urls (like javascript links)
        host = urlparse_cached(request).hostname or ""
        return bool(regex.search(host))

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
