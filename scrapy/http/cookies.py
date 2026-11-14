from __future__ import annotations

import itertools
import re
import time
from http.cookiejar import Cookie, CookiePolicy, DefaultCookiePolicy
from http.cookiejar import CookieJar as _CookieJar
from typing import TYPE_CHECKING, Self, cast

from scrapy.utils.httpobj import urlparse_cached

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from scrapy import Request
    from scrapy.http import Response


# Defined in the http.cookiejar module, but undocumented:
# https://github.com/python/cpython/blob/v3.9.0/Lib/http/cookiejar.py#L527
IPV4_RE = re.compile(r"\.\d+$", re.ASCII)

# Stands in for the creation time of RFC 6265. A counter, unlike a clock,
# never gives the same value to two cookies.
_creation_indexes = itertools.count()


class _CreationOrderCookieJar(_CookieJar):
    """Cookie jar that lists cookies in the ``Cookie`` header by descending
    path length, then by ascending creation time, as :rfc:`6265#section-5.4`
    requires.
    """

    def set_cookie(self, cookie: Cookie) -> None:
        try:
            # A cookie that replaces another one keeps its creation time.
            old_cookie = self._cookies[cookie.domain][cookie.path][cookie.name]  # type: ignore[attr-defined]
        except KeyError:
            cookie._creation_index = next(_creation_indexes)  # type: ignore[attr-defined]
        else:
            cookie._creation_index = getattr(old_cookie, "_creation_index", -1)  # type: ignore[attr-defined]
        super().set_cookie(cookie)

    def _cookie_attrs(self, cookies: list[Cookie]) -> list[str]:
        # super() then sorts by path length, and its sort is stable.
        cookies.sort(key=lambda c: getattr(c, "_creation_index", -1))
        return super()._cookie_attrs(cookies)  # type: ignore[misc,no-any-return]


class _QuotaCookieJar(_CreationOrderCookieJar):
    """Cookie jar that enforces optional cookie count and byte size quotas.

    The byte size of a cookie is the sum of the UTF-8 encoded lengths of
    its stored name, value, domain and path. When storing a cookie would
    exceed a quota, expired cookies are discarded first and then cookies
    are evicted in least-recently-used order. A cookie that on its own
    exceeds the byte quota is rejected and reported through *on_reject*.
    """

    def __init__(
        self,
        policy: CookiePolicy | None = None,
        max_count: int | None = None,
        max_bytes: int | None = None,
        on_reject: Callable[[Cookie], None] | None = None,
    ):
        super().__init__(policy)
        self.max_count: int | None = max_count
        self.max_bytes: int | None = max_bytes
        self.on_reject: Callable[[Cookie], None] | None = on_reject
        self._count: int = 0
        self._bytes: int = 0
        self._use_counter = itertools.count()
        self._last_used: dict[tuple[str, str, str], int] = {}

    @staticmethod
    def _cookie_size(cookie: Cookie) -> int:
        size = (
            len(cookie.name.encode("utf-8"))
            + len(cookie.domain.encode("utf-8"))
            + len(cookie.path.encode("utf-8"))
        )
        if cookie.value:
            size += len(cookie.value.encode("utf-8"))
        return size

    def _over_quota(self) -> bool:
        return (
            self.max_count is not None and self._count > self.max_count
        ) or (self.max_bytes is not None and self._bytes > self.max_bytes)

    def set_cookie(self, cookie: Cookie) -> None:
        key = (cookie.domain, cookie.path, cookie.name)
        size = self._cookie_size(cookie)
        if (self.max_bytes is not None and size > self.max_bytes) or (
            self.max_count is not None and self.max_count < 1
        ):
            if self.on_reject is not None:
                self.on_reject(cookie)
            return
        try:
            old = self._cookies[cookie.domain][cookie.path][cookie.name]  # type: ignore[attr-defined]
        except KeyError:
            self._count += 1
        else:
            self._bytes -= self._cookie_size(old)
        super().set_cookie(cookie)
        self._bytes += size
        self._last_used[key] = next(self._use_counter)
        if not self._over_quota():
            return
        self.clear_expired_cookies()
        while self._over_quota():
            # The cookie just stored has the most recent use stamp, so the
            # victim is always another cookie.
            victim = min(self._last_used, key=lambda k: self._last_used[k])
            self.clear(*victim)

    def note_used(self, cookies: list[Cookie]) -> None:
        """Mark *cookies* as used now, for least-recently-used eviction."""
        for cookie in cookies:
            key = (cookie.domain, cookie.path, cookie.name)
            self._last_used[key] = next(self._use_counter)

    def clear(
        self,
        domain: str | None = None,
        path: str | None = None,
        name: str | None = None,
    ) -> None:
        super().clear(domain, path, name)
        self._resync()

    def clear_expired_cookies(self) -> None:
        super().clear_expired_cookies()
        self._resync()

    def clear_session_cookies(self) -> None:
        super().clear_session_cookies()
        self._resync()

    def _resync(self) -> None:
        """Recompute quota usage and drop use stamps of removed cookies."""
        self._count = 0
        self._bytes = 0
        keys = set()
        for cookie in self:
            self._count += 1
            self._bytes += self._cookie_size(cookie)
            keys.add((cookie.domain, cookie.path, cookie.name))
        for key in list(self._last_used):
            if key not in keys:
                del self._last_used[key]


class CookieJar:
    def __init__(
        self,
        policy: CookiePolicy | None = None,
        check_expired_frequency: int = 10000,
        max_count: int | None = None,
        max_bytes: int | None = None,
        on_reject: Callable[[Cookie], None] | None = None,
    ):
        self.policy: CookiePolicy = policy or DefaultCookiePolicy()
        self.jar: _CookieJar
        if max_count is None and max_bytes is None:
            self.jar = _CreationOrderCookieJar(self.policy)
        else:
            self.jar = _QuotaCookieJar(self.policy, max_count, max_bytes, on_reject)
        self.jar._cookies_lock = _DummyLock()  # type: ignore[attr-defined]
        self.check_expired_frequency: int = check_expired_frequency
        self.processed: int = 0

    def extract_cookies(self, response: Response, request: Request) -> None:
        wreq = WrappedRequest(request)
        wrsp = WrappedResponse(response)
        self.jar.extract_cookies(wrsp, wreq)  # type: ignore[arg-type]

    def add_cookie_header(self, request: Request) -> None:
        wreq = WrappedRequest(request)
        self.policy._now = self.jar._now = int(time.time())  # type: ignore[attr-defined]

        # the cookiejar implementation iterates through all domains
        # instead we restrict to potential matches on the domain
        req_host = urlparse_cached(request).hostname
        if not req_host:
            return

        if not IPV4_RE.search(req_host):
            hosts = potential_domain_matches(req_host)
            if "." not in req_host:
                hosts += potential_domain_matches(req_host + ".local")
        else:
            hosts = [req_host, "." + req_host]

        cookies = []
        for host in hosts:
            if host in self.jar._cookies:  # type: ignore[attr-defined]
                cookies.extend(self.jar._cookies_for_domain(host, wreq))  # type: ignore[attr-defined]

        if isinstance(self.jar, _QuotaCookieJar):
            # Only cookies matching this request count as used, so that
            # requests to other domains do not refresh them.
            self.jar.note_used(cookies)

        attrs = self.jar._cookie_attrs(cookies)  # type: ignore[attr-defined]
        if attrs and not wreq.has_header("Cookie"):
            wreq.add_unredirected_header("Cookie", "; ".join(attrs))

        self.processed += 1
        if self.processed % self.check_expired_frequency == 0:
            # This is still quite inefficient for large number of cookies
            self.jar.clear_expired_cookies()

    @property
    def _cookies(self) -> dict[str, dict[str, dict[str, Cookie]]]:
        return self.jar._cookies  # type: ignore[attr-defined,no-any-return]

    def clear_session_cookies(self) -> None:
        return self.jar.clear_session_cookies()

    def clear(
        self,
        domain: str | None = None,
        path: str | None = None,
        name: str | None = None,
    ) -> None:
        self.jar.clear(domain, path, name)

    def __iter__(self) -> Iterator[Cookie]:
        return iter(self.jar)

    def __len__(self) -> int:
        return len(self.jar)

    def set_policy(self, pol: CookiePolicy) -> None:
        self.jar.set_policy(pol)

    def make_cookies(self, response: Response, request: Request) -> Sequence[Cookie]:
        wreq = WrappedRequest(request)
        wrsp = WrappedResponse(response)
        return self.jar.make_cookies(wrsp, wreq)  # type: ignore[arg-type]

    def set_cookie(self, cookie: Cookie) -> None:
        self.jar.set_cookie(cookie)

    def set_cookie_if_ok(self, cookie: Cookie, request: Request) -> None:
        self.jar.set_cookie_if_ok(cookie, WrappedRequest(request))  # type: ignore[arg-type]


def potential_domain_matches(domain: str) -> list[str]:
    """Potential domain matches for a cookie

    >>> potential_domain_matches('www.example.com')
    ['www.example.com', 'example.com', '.www.example.com', '.example.com']

    """
    matches = [domain]
    try:
        start = domain.index(".") + 1
        end = domain.rindex(".")
        while start < end:
            matches.append(domain[start:])
            start = domain.index(".", start) + 1
    except ValueError:
        pass
    return matches + ["." + d for d in matches]


class _DummyLock:
    def acquire(self) -> None:
        pass

    def release(self) -> None:
        pass


class WrappedRequest:
    """Wraps a :class:`scrapy.Request` class with methods defined by
    the :class:`urllib.request.Request` class to interact with
    the :class:`http.cookiejar.CookieJar` class.
    """

    def __init__(self, request: Request):
        self.request = request

    def get_full_url(self) -> str:
        return self.request.url

    def get_host(self) -> str:
        return urlparse_cached(self.request).netloc

    def get_type(self) -> str:
        return urlparse_cached(self.request).scheme

    def is_unverifiable(self) -> bool:
        """Return ``False``, as Scrapy does not track whether the user had the
        option to approve the URL of a request, which is what makes a request
        unverifiable as defined by :rfc:`2965`.
        """
        return False

    @property
    def full_url(self) -> str:
        return self.get_full_url()

    @property
    def host(self) -> str:
        return self.get_host()

    @property
    def type(self) -> str:
        return self.get_type()

    @property
    def unverifiable(self) -> bool:
        return self.is_unverifiable()

    @property
    def origin_req_host(self) -> str:
        return cast("str", urlparse_cached(self.request).hostname)

    def has_header(self, name: str) -> bool:
        return name in self.request.headers

    def get_header(self, name: str, default: str | None = None) -> str | None:
        value = self.request.headers.get(name, default)
        return value.decode(errors="replace") if value is not None else None

    def header_items(self) -> list[tuple[str, list[str]]]:
        return [
            (
                k.decode(errors="replace"),
                [x.decode(errors="replace") for x in v],
            )
            for k, v in self.request.headers.items()
        ]

    def add_unredirected_header(self, name: str, value: str) -> None:
        self.request.headers.appendlist(name, value)


class WrappedResponse:
    def __init__(self, response: Response):
        self.response = response

    def info(self) -> Self:
        return self

    def get_all(self, name: str, default: object = None) -> list[str]:
        return [v.decode(errors="replace") for v in self.response.headers.getlist(name)]
