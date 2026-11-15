"""
This module implements the Request class which is used to represent HTTP
requests in Scrapy.

See documentation in docs/topics/request-response.rst
"""

from __future__ import annotations

import inspect
import ipaddress
import itertools
import secrets
import time
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import (
    TYPE_CHECKING,
    Any,
    Concatenate,
    NamedTuple,
    NoReturn,
    NotRequired,
    Self,
    TypeAlias,
    TypedDict,
    TypeVar,
    overload,
)
from urllib.parse import urljoin, urlsplit

from w3lib.url import safe_url_string

# a workaround for the docs "more than one target found" problem
import scrapy  # noqa: TC001
from scrapy.http.headers import Headers
from scrapy.utils.curl import curl_to_request_kwargs
from scrapy.utils.python import to_bytes
from scrapy.utils.trackref import object_ref

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from twisted.python.failure import Failure

    # circular import
    from scrapy.crawler import Crawler
    from scrapy.http import Response

    CallbackT: TypeAlias = Callable[Concatenate[Response, ...], Any]


# Crawler for the callback, coroutine pipeline method or Spider.start()
# call currently executing, if any. Lets Request.__await__() schedule
# itself without needing a crawler reference passed in explicitly.
_active_crawler: ContextVar[Crawler | None] = ContextVar(
    "_active_crawler", default=None
)

# Whether a download handler's download_request() is currently executing.
# Download handlers must never await a Request or call download_async(),
# so Request.__await__() checks this instead of just relying on whatever
# _active_crawler happens to be set to.
_in_download_handler: ContextVar[bool] = ContextVar(
    "_in_download_handler", default=False
)


@contextmanager
def _active_crawler_scope(crawler: Crawler) -> Generator[None, None, None]:
    """Set _active_crawler to *crawler* for the duration of the block.

    Restores the previous value with set() instead of reset(), and only where
    the value set here is still in place: the block may be exited from an
    unrelated context, e.g. when an abandoned coroutine is garbage-collected,
    where the token from the original set() is invalid and the current value
    belongs to that context.
    """
    previous = _active_crawler.get()
    _active_crawler.set(crawler)
    try:
        yield
    finally:
        if _active_crawler.get() is crawler:
            _active_crawler.set(previous)


@contextmanager
def _in_download_handler_scope() -> Generator[None, None, None]:
    """Set _in_download_handler to True for the duration of the block.

    See _active_crawler_scope() for how the previous value is restored.
    """
    previous = _in_download_handler.get()
    _in_download_handler.set(True)
    try:
        yield
    finally:
        if _in_download_handler.get():
            _in_download_handler.set(previous)


class VerboseCookie(TypedDict):
    name: str | bytes
    value: str | bytes | bool | float | int
    domain: NotRequired[str | bytes]
    path: NotRequired[str | bytes]
    secure: NotRequired[bool]


CookiesT: TypeAlias = (
    dict[str | bytes, str | bytes | bool | float | int] | list[VerboseCookie]
)


RequestTypeVar = TypeVar("RequestTypeVar", bound="Request")

# Request IDs are unique across processes: process start time in milliseconds,
# 48 random bits, and a 40-bit per-process counter.
_request_ids = itertools.count(
    (time.time_ns() // 1_000_000 << 88) | (secrets.randbits(48) << 40)
)

# Origin-bearing schemes and the port used when a URL omits one. Other
# schemes have no origin, see _origin_of_url().
_DEFAULT_PORTS = {"http": 80, "https": 443}

# Header fields that carry site credentials when Scrapy or a component sets
# them automatically. They are dropped from a cross-origin copy unless the
# caller asks to keep them.
_CREDENTIAL_HEADERS: tuple[bytes, ...] = (
    b"Authorization",
    b"Cookie",
    b"Proxy-Authorization",
)


class _Origin(NamedTuple):
    """A normalized URL origin: scheme, IDNA host and effective port."""

    scheme: str
    host: str
    port: int


def _normalized_origin_host(hostname: str) -> str:
    """Return the normalized host of an HTTP(S) origin.

    An IP address becomes its canonical spelling, so equivalent IPv6
    spellings compare equal, and keeps its scope ID. Any other host is
    IDNA-encoded and lower-cased, so every spelling of the same
    internationalized name compares equal, and one final dot, which marks a
    fully qualified name, is dropped."""
    try:
        return str(ipaddress.ip_address(hostname)).lower()
    except ValueError:
        pass
    if hostname.endswith(".") and len(hostname) > 1:
        hostname = hostname[:-1]
    try:
        return hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError(f"Invalid origin host {hostname!r}") from exc


def _origin_of_url(url: str) -> _Origin | None:
    """Return the normalized origin of an absolute HTTP(S) URL, or ``None``
    when the URL has no origin.

    Only ``http`` and ``https`` have an origin, since they are the only
    schemes with a defined default port and a well-defined host comparison.
    A URL of any other scheme, or one without a host, has no origin; callers
    treat that as a different origin.

    The scheme is lower-cased, the host is normalized as described in
    :func:`_normalized_origin_host`, and a missing port becomes the default
    port of the scheme, so ``http://example.com`` and
    ``http://example.com:80`` share an origin. User information, path, query
    and fragment are not part of the origin and are ignored.

    Raise :exc:`ValueError` when *url* has a port outside the valid range."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parsed.hostname:
        return None
    port = parsed.port
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    return _Origin(scheme, _normalized_origin_host(parsed.hostname), port)


def _resolve_target_url(
    source_url: str, target_url: str, *, verbatim: bool = False
) -> str:
    """Return the absolute form of *target_url*, resolving it against
    *source_url* when it is relative.

    With *verbatim*, the URL keeps the spelling it was given, matching the
    :reqmeta:`verbatim_url` behavior of the request it is copied from;
    otherwise it is encoded like any other request URL."""
    if not isinstance(target_url, str):
        raise TypeError(f"Request url must be str, got {type(target_url).__name__}")
    target = target_url if verbatim else safe_url_string(target_url)
    if not urlsplit(target).scheme:
        target = urljoin(source_url, target)
    return target if verbatim else safe_url_string(target)


def _copy_private_names(names: Iterable[str], what: str) -> frozenset[str]:
    """Materialize and validate the names of site-private fields.

    The iterable is consumed eagerly, so a failing iterable raises before
    anything else is read or built. A bare string is rejected because it
    would silently declare its characters."""
    if isinstance(names, (str, bytes)) or not isinstance(names, Iterable):
        raise TypeError(f"{what} must be an iterable of field names")
    result: set[str] = set()
    for name in names:
        if not isinstance(name, str):
            raise TypeError(
                f"{what} field names must be str, got {type(name).__name__}"
            )
        result.add(name)
    return frozenset(result)


def _copy_private_value(value: Any, path: str) -> Any:
    """Return a copy of *value* that shares no mutable container with it.

    Only exact built-in containers are copied: :class:`dict` values,
    :class:`list` and :class:`tuple` items, and :class:`set` and
    :class:`frozenset` themselves. Dict keys and the items of a set or
    frozenset are hashable, hence immutable, and are shared as they are. Any
    other object, including subclasses of those containers, is shared by
    reference, so no user-defined ``__copy__``, ``__deepcopy__`` or
    constructor runs.

    A container reachable through several paths is copied once per path, so
    aliasing between them is not preserved; a container reachable from itself
    raises :exc:`TypeError`."""
    return _copy_private_value_inner(value, path, {})


def _copy_private_value_inner(value: Any, path: str, active: dict[int, str]) -> Any:
    value_type = type(value)
    if value_type not in (dict, list, tuple, set, frozenset):
        return value
    container_id = id(value)
    if container_id in active:
        raise TypeError(f"{path} contains a reference cycle")
    active[container_id] = path
    try:
        if value_type is dict:
            # Keys are hashable, hence immutable, so they never need copying.
            return {
                key: _copy_private_value_inner(item, f"{path}[{key!r}]", active)
                for key, item in value.items()
            }
        if value_type is list:
            return [
                _copy_private_value_inner(item, f"{path}[{index}]", active)
                for index, item in enumerate(value)
            ]
        if value_type is tuple:
            return tuple(
                _copy_private_value_inner(item, f"{path}[{index}]", active)
                for index, item in enumerate(value)
            )
        # The items of a set or frozenset are hashable, hence immutable, so
        # copying the container is enough. copy() does not hash the items
        # again, unlike the set constructor.
        return value.copy()
    finally:
        del active[container_id]


# Marks an argument that the caller did not pass, so that ``None`` can be told
# apart from "not given".
_UNSET: Any = object()


def _copy_container(value: Mapping[str, Any] | None, what: str) -> dict[str, Any]:
    """Return an isolated plain-:class:`dict` copy of a request *meta* or
    *cb_kwargs* mapping.

    Any other value is rejected, so the copy can be pruned without touching
    the caller's object."""
    if not value:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{what} must be a mapping, got {type(value).__name__}")
    copied: dict[str, Any] = _copy_private_value(dict(value), what)
    return copied


def _copy_headers(
    value: Mapping[str, Any]
    | Mapping[bytes, Any]
    | Iterable[tuple[str | bytes, Any]]
    | None,
    encoding: str,
) -> Headers | None:
    """Return an isolated :class:`~scrapy.http.Headers` copy of a request's
    headers, with the same values but new value lists."""
    if not value:
        return None
    return Headers(value, encoding=encoding)


def _validate_keep_credentials(value: bool) -> bool:
    """Return *value* if it is a real :class:`bool`.

    Requiring a :class:`bool` keeps a string such as ``"false"`` from turning
    the option on through its truthiness."""
    if not isinstance(value, bool):
        raise TypeError(
            f"keep_credentials must be a bool, got {type(value).__name__}: {value!r}"
        )
    return value


def NO_CALLBACK(*args: Any, **kwargs: Any) -> NoReturn:
    """When assigned to the ``callback`` parameter of
    :class:`~scrapy.Request`, it indicates that the request is not meant
    to have a spider callback at all.

    For example:

    .. code-block:: python

       Request("https://example.com", callback=NO_CALLBACK)

    This value should be used by :ref:`components <topics-components>` that
    create and handle their own requests, e.g. through
    :meth:`scrapy.core.engine.ExecutionEngine.download`, so that downloader
    middlewares handling such requests can treat them differently from requests
    intended for the :meth:`~scrapy.Spider.parse` callback.
    """
    raise RuntimeError(
        "The NO_CALLBACK callback has been called. This is a special callback "
        "value intended for requests whose callback is never meant to be "
        "called."
    )


class Request(object_ref):
    """Represents an HTTP request, which is usually generated in a Spider and
    executed by the Downloader, thus generating a
    :class:`~scrapy.http.Response`.

    *url* is the URL of the request. If it is invalid, :exc:`ValueError` is
    raised.

    *method* is the HTTP method of the request.

    *headers* are the request headers. Header values can be strings, for
    single-valued headers, or lists, for multi-valued headers. Headers with a
    ``None`` value are not sent.

    .. caution:: The :ref:`cookie middleware <cookies>` ignores cookies set
        through the ``Cookie`` header. Use *cookies* instead.

    *body* is the request body. A string body is encoded with *encoding*.

    *cookies* are the request cookies, as a dict of cookie names and values or
    as a list of dicts with a cookie each. See :ref:`cookies`.

    *encoding* is used to percent-encode *url*, unless the
    :reqmeta:`verbatim_url` request meta key is set, and to encode *body* if it
    is a string.

    *flags* is a list of labels for the request, which are included in log
    messages about it.

    *meta* and *cb_kwargs* are shallow copied into :attr:`meta` and
    :attr:`cb_kwargs`.

    *callback*, *errback*, *priority*, *dont_filter* and *parent_id* set the
    attributes of the same name. Set *parent_id* when building a request on
    behalf of another request, e.g. in a downloader middleware:
    ``Request(url, parent_id=request.id)``.
    """

    __attrs_and_slots = ("callback", "dont_filter", "errback", "method", "priority")
    attributes: tuple[str, ...] = (
        "url",
        "headers",
        "body",
        "cookies",
        "meta",
        "encoding",
        "flags",
        "cb_kwargs",
        "parent_id",
        *__attrs_and_slots,
    )
    """A tuple of :class:`str` objects containing the name of all public
    attributes of the class that are also keyword parameters of the
    ``__init__()`` method.

    Currently used by :meth:`.Request.replace`, :meth:`.Request.to_dict` and
    :func:`~scrapy.utils.request.request_from_dict`.
    """

    __slots__ = (
        "__weakref__",
        "_body",
        "_cb_kwargs",
        "_cookies",
        "_encoding",
        "_flags",
        "_headers",
        "_id",
        "_meta",
        "_url",
        "parent_id",
        *__attrs_and_slots,
    )
    del __attrs_and_slots

    def __init__(
        self,
        url: str,
        callback: CallbackT | None = None,
        method: str = "GET",
        headers: Mapping[str, Any]
        | Mapping[bytes, Any]
        | Iterable[tuple[str | bytes, Any]]
        | None = None,
        body: bytes | str | None = None,
        cookies: CookiesT | None = None,
        meta: dict[str, Any] | None = None,
        encoding: str = "utf-8",
        priority: int = 0,
        dont_filter: bool = False,
        errback: Callable[[Failure], Any] | None = None,
        flags: list[str] | None = None,
        cb_kwargs: dict[str, Any] | None = None,
        parent_id: int | None = None,
    ) -> None:
        self._id: int = next(_request_ids)
        self._encoding: str = encoding  # this one has to be set first
        self.method: str = str(method).upper()
        self._meta: dict[str, Any] | None = dict(meta) if meta else None
        self._set_url(url)
        self._set_body(body)
        if not isinstance(priority, int):
            raise TypeError(f"Request priority not an integer: {priority!r}")

        #: Default: ``0``
        #:
        #: Value that the :ref:`scheduler <topics-scheduler>` may use for
        #: request prioritization.
        #:
        #: Built-in schedulers prioritize requests with a higher priority
        #: value.
        #:
        #: Negative values are allowed.
        self.priority: int = priority

        if not (callable(callback) or callback is None):
            raise TypeError(
                f"callback must be a callable, got {type(callback).__name__}"
            )
        if not (callable(errback) or errback is None):
            raise TypeError(f"errback must be a callable, got {type(errback).__name__}")

        #: :class:`~collections.abc.Callable` to parse the
        #: :class:`~scrapy.http.Response` to this request once received.
        #:
        #: The callable must expect the response as its first parameter, and
        #: support any additional keyword arguments set through
        #: :attr:`cb_kwargs`. See :ref:`writing-callbacks` and
        #: :ref:`callback-output`.
        #:
        #: In addition to an arbitrary callable, the following values are also
        #: supported:
        #:
        #: -   ``None`` (default), which indicates that the
        #:     :meth:`~scrapy.Spider.parse` method of the spider must be used.
        #:
        #: -   :func:`~scrapy.http.request.NO_CALLBACK`.
        #:
        #: If an unhandled exception is raised during request or response
        #: processing, i.e. by a :ref:`spider middleware
        #: <topics-spider-middleware>`, :ref:`downloader middleware
        #: <topics-downloader-middleware>` or download handler
        #: (:setting:`DOWNLOAD_HANDLERS`), :attr:`errback` is called instead.
        #:
        #: .. tip::
        #:     :class:`~scrapy.spidermiddlewares.httperror.HttpErrorMiddleware`
        #:     raises exceptions for non-2xx responses by default, sending them
        #:     to the :attr:`errback` instead.
        #:
        #: .. seealso:: :ref:`callbacks`
        self.callback: CallbackT | None = callback

        #: :class:`~collections.abc.Callable` to handle exceptions raised
        #: during request or response processing.
        #:
        #: The callable must expect a :exc:`~twisted.python.failure.Failure` as
        #: its first parameter.
        #:
        #: .. seealso:: :ref:`errbacks`
        self.errback: Callable[[Failure], Any] | None = errback

        self._cookies: CookiesT | None = cookies or None
        self._headers: Headers | None = (
            Headers(headers, encoding=encoding) if headers else None
        )

        #: Whether this request may be filtered out by :ref:`components
        #: <topics-components>` that support filtering out requests (``False``,
        #: default), or those components should not filter out this request
        #: (``True``).
        #:
        #: The following built-in components check this attribute:
        #:
        #: -   The :ref:`scheduler <topics-scheduler>` uses it to skip
        #:     duplicate request filtering (see
        #:     :setting:`DUPEFILTER_CLASS`). When set to ``True``, the
        #:     request is not checked against the duplicate filter,
        #:     allowing requests that would otherwise be considered duplicates
        #:     to be scheduled multiple times.
        #: -   :class:`~scrapy.downloadermiddlewares.offsite.OffsiteMiddleware`
        #:     uses it to allow requests to domains not in
        #:     :attr:`~scrapy.Spider.allowed_domains`. To skip only the offsite
        #:     filter without affecting other components, consider using the
        #:     :reqmeta:`allow_offsite` request meta key instead.
        #:
        #: Third-party components may also use this attribute to decide whether
        #: to filter out a request.
        #:
        #: When defining the start URLs of a spider through
        #: :attr:`~scrapy.Spider.start_urls`, this attribute is enabled by
        #: default. See :meth:`~scrapy.Spider.start`.
        self.dont_filter: bool = dont_filter

        self._cb_kwargs: dict[str, Any] | None = dict(cb_kwargs) if cb_kwargs else None
        self._flags: list[str] | None = list(flags) if flags else None

        self.parent_id: int | None = parent_id
        """:attr:`id` of the request this request derives from, or ``None``.

        Unless set explicitly, Scrapy sets it to the :attr:`id` of:

        -   The original request, for requests created with :meth:`replace`
            or :meth:`copy`, e.g. redirects and retries.

        -   The request of the response, for requests created with
            :meth:`Response.follow() <scrapy.http.Response.follow>` or
            :meth:`~scrapy.http.Response.follow_all`.

        -   The request whose callback or errback yielded the request.
        """

    @property
    def id(self) -> int:
        """Unique identifier of this request."""
        return self._id

    @property
    def cb_kwargs(self) -> dict[str, Any]:
        if self._cb_kwargs is None:
            self._cb_kwargs = {}
        return self._cb_kwargs

    @property
    def meta(self) -> dict[str, Any]:
        if self._meta is None:
            self._meta = {}
        return self._meta

    @property
    def url(self) -> str:
        return self._url

    def _url_is_verbatim(self) -> bool:
        return bool(self._meta and self._meta.get("verbatim_url"))

    def _set_url(self, url: str) -> None:
        if not isinstance(url, str):
            raise TypeError(f"Request url must be str, got {type(url).__name__}")

        if self._url_is_verbatim():
            self._url = url
        else:
            self._url = safe_url_string(url, self.encoding)

        if (
            "://" not in self._url
            and not self._url.startswith("about:")
            and not self._url.startswith("data:")
        ):
            raise ValueError(f"Missing scheme in request url: {self._url}")

    @property
    def body(self) -> bytes:
        return self._body

    def _set_body(self, body: str | bytes | None) -> None:
        self._body = b"" if not body else to_bytes(body, self.encoding)

    @property
    def encoding(self) -> str:
        return self._encoding

    @property
    def flags(self) -> list[str]:
        if self._flags is None:
            self._flags = []
        return self._flags

    @flags.setter
    def flags(self, value: list[str] | None) -> None:
        self._flags = value

    @property
    def cookies(self) -> CookiesT:
        if self._cookies is None:
            self._cookies = {}
        return self._cookies

    @cookies.setter
    def cookies(self, value: CookiesT | None) -> None:
        self._cookies = value

    @property
    def headers(self) -> Headers:
        if self._headers is None:
            self._headers = Headers(encoding=self.encoding)
        return self._headers

    @headers.setter
    def headers(
        self,
        value: Mapping[str, Any]
        | Mapping[bytes, Any]
        | Iterable[tuple[str | bytes, Any]]
        | None,
    ) -> None:
        if isinstance(value, Headers):
            self._headers = value
        else:
            self._headers = (
                Headers(value, encoding=self.encoding) if value is not None else None
            )

    def __repr__(self) -> str:
        return f"<{self.method} {self.url}>"

    def __await__(self) -> Generator[Any, None, Response]:
        """Send this request and return its response.

        .. versionadded:: 2.20.0

        See :ref:`inline-requests`.
        """
        if _in_download_handler.get():
            raise RuntimeError(
                "Request objects cannot be awaited from a download "
                "handler; see DownloadHandlerProtocol"
            )
        if self.callback not in (None, NO_CALLBACK) or self.errback is not None:
            raise ValueError(
                "Cannot await a Request that has a callback or an errback set"
            )
        crawler = _active_crawler.get()
        if crawler is None:
            raise RuntimeError(
                "Request objects can only be awaited from where coroutine "
                "syntax is supported (see the Scrapy coroutines docs)"
            )
        self.callback = NO_CALLBACK
        return crawler.engine.download_async(self).__await__()

    def copy(self) -> Self:
        return self.replace()

    @overload
    def replace(
        self, *args: Any, cls: type[RequestTypeVar], **kwargs: Any
    ) -> RequestTypeVar: ...

    @overload
    def replace(self, *args: Any, cls: None = None, **kwargs: Any) -> Self: ...

    def replace(
        self, *args: Any, cls: type[Request] | None = None, **kwargs: Any
    ) -> Request:
        """Return a new request with the same attributes as this one, except
        for those given new values as keyword arguments named after
        :attr:`attributes`.

        :attr:`cb_kwargs` and :attr:`meta` are :doc:`shallow copied
        <library/copy>` unless given new values. The new request gets a new
        :attr:`id`, and :attr:`parent_id` defaults to the :attr:`id` of this
        request. *cls* sets the class of the new request.

        See also :ref:`callback-data`.
        """
        kwargs.setdefault("parent_id", self.id)
        for x in self.attributes:
            kwargs.setdefault(x, getattr(self, x))
        if cls is None:
            cls = self.__class__
        return cls(*args, **kwargs)

    def copy_for_origin(
        self,
        url: str,
        *,
        private_meta: Iterable[str] = (),
        private_cb_kwargs: Iterable[str] = (),
        keep_credentials: bool = False,
        **kwargs: Any,
    ) -> Self:
        """Return a copy of this request for the site that *url* points to,
        without carrying site-private data across origins.

        Use it instead of :meth:`replace` when a request is copied to a
        different site, e.g. by a component that forwards requests to a
        mirror or an API host. :meth:`replace` keeps every attribute as is;
        this method additionally drops the data you declare as belonging to
        the site of the original request when the target site differs.

        *url* is the URL of the new request. When it is relative, it is
        resolved against the URL of this request. It is encoded and parsed,
        and its origin is the tuple of the lower-cased scheme, the IDNA host
        and the effective port, where a missing port is the default port of
        the scheme. IPv6 addresses are compared in their canonical spelling,
        so ``http://[0:0:0:0:0:0:0:1]`` and ``http://[::1]`` share an
        origin, and user information takes no part in the comparison. Only
        when that origin is exactly the origin of this request are the
        declared fields kept; otherwise, and also when either URL has no
        origin, they are dropped.

        *private_meta* and *private_cb_kwargs* name the :attr:`meta` and
        :attr:`cb_kwargs` keys that belong to the site of this request, e.g.
        ``private_meta=["api_token"]``. A request copied for the same origin
        keeps them; a request copied for another origin does not have those
        keys at all. Keys you pass in a *meta* or *cb_kwargs* argument follow
        the same rule, so they cannot bring a private field to another origin
        by accident.

        A cross-origin copy also drops the ``Authorization``, ``Cookie`` and
        ``Proxy-Authorization`` headers and the :attr:`cookies` attribute,
        which hold credentials Scrapy and its components set automatically.
        Pass ``keep_credentials=True`` to keep them anyway; this option
        accepts only :class:`bool` values, so a string such as ``"false"``
        cannot turn it on. The :reqmeta:`http_user`, :reqmeta:`http_pass`,
        :reqmeta:`ftp_user`, :reqmeta:`ftp_password`, :reqmeta:`proxy` and
        :reqmeta:`ftp_proxy` keys hold credentials too and are left to the
        caller to declare through *private_meta*.

        :attr:`meta` and :attr:`cb_kwargs` are copied with the policy
        described in :ref:`request-copy-for-origin-nested-copy`, which
        isolates built-in containers without running any method defined by
        the objects they hold. The other attributes, named by
        :attr:`attributes`, are copied as in :meth:`replace`, so the new
        request has a new :attr:`id` and a :attr:`parent_id` of this
        request's :attr:`id`. A keyword argument named after an attribute
        supplies the value for the copy instead, as in :meth:`replace`; for
        *meta*, *cb_kwargs*, *headers* and *cookies* the cross-origin rule
        still applies to the supplied value, so declaring a field private or
        passing a credential header does not keep it on another origin.

        When this method raises, this request is unchanged and no copy is
        returned, so a caller can report the failure and keep using the
        original request.

        .. versionadded:: VERSION

        See also :ref:`request-copy-for-origin-redirects`.
        """
        keep_credentials = _validate_keep_credentials(keep_credentials)
        meta_names = _copy_private_names(private_meta, "private_meta")
        cb_kwargs_names = _copy_private_names(private_cb_kwargs, "private_cb_kwargs")
        target = _resolve_target_url(self.url, url, verbatim=self._url_is_verbatim())

        # Everything that can fail runs before the copy is built, so a
        # failure leaves both this request and the new one untouched.
        source_origin = _origin_of_url(self.url)
        target_origin = _origin_of_url(target)
        same_origin = (
            source_origin is not None
            and target_origin is not None
            and source_origin == target_origin
        )

        meta_arg = kwargs.pop("meta", _UNSET)
        cb_kwargs_arg = kwargs.pop("cb_kwargs", _UNSET)
        headers_arg = kwargs.pop("headers", _UNSET)
        cookies = kwargs.pop("cookies", _UNSET)
        meta = _copy_container(self._meta if meta_arg is _UNSET else meta_arg, "meta")
        cb_kwargs = _copy_container(
            self._cb_kwargs if cb_kwargs_arg is _UNSET else cb_kwargs_arg,
            "cb_kwargs",
        )
        headers = _copy_headers(
            self._headers if headers_arg is _UNSET else headers_arg, self.encoding
        )
        if cookies is _UNSET:
            cookies = self._cookies
        if cookies:
            cookies = _copy_private_value(cookies, "cookies")
        if not same_origin:
            for name in meta_names:
                meta.pop(name, None)
            for name in cb_kwargs_names:
                cb_kwargs.pop(name, None)
            if not keep_credentials:
                if headers is not None:
                    for header in _CREDENTIAL_HEADERS:
                        headers.pop(header, None)
                cookies = None

        kwargs.setdefault("parent_id", self.id)
        kwargs["meta"] = meta
        kwargs["cb_kwargs"] = cb_kwargs
        kwargs["headers"] = headers
        kwargs["cookies"] = cookies
        for x in self.attributes:
            if x != "url":
                kwargs.setdefault(x, getattr(self, x))
        return self.__class__(target, **kwargs)

    @classmethod
    def from_curl(
        cls,
        curl_command: str,
        ignore_unknown_options: bool = True,
        **kwargs: Any,
    ) -> Self:
        """Create a Request object from a string containing a `cURL
        <https://curl.se/>`_ command. It populates the HTTP method, the
        URL, the headers, the cookies and the body. It accepts the same
        arguments as the :class:`Request` class, taking preference and
        overriding the values of the same arguments contained in the cURL
        command.

        Unrecognized options are ignored by default. To raise an error when
        finding unknown options call this method by passing
        ``ignore_unknown_options=False``.

        .. caution:: Using :meth:`from_curl` from :class:`~scrapy.Request`
                     subclasses, such as :class:`~scrapy.http.JsonRequest`, or
                     :class:`~scrapy.http.XmlRpcRequest`, as well as having
                     :ref:`downloader middlewares <topics-downloader-middleware>`
                     and
                     :ref:`spider middlewares <topics-spider-middleware>`
                     enabled, such as
                     :class:`~scrapy.downloadermiddlewares.defaultheaders.DefaultHeadersMiddleware`,
                     :class:`~scrapy.downloadermiddlewares.useragent.UserAgentMiddleware`,
                     or
                     :class:`~scrapy.downloadermiddlewares.httpcompression.HttpCompressionMiddleware`,
                     may modify the :class:`~scrapy.Request` object.

        To translate a cURL command into a Scrapy request,
        you may use `curl2scrapy <https://michael-shub.github.io/curl2scrapy/>`_.
        """
        request_kwargs = curl_to_request_kwargs(curl_command, ignore_unknown_options)
        request_kwargs.update(kwargs)
        return cls(**request_kwargs)

    def to_curl(self) -> str:
        """Return a string with a `cURL <https://curl.se/>`_ command equivalent
        to this request.

        Inverse of :meth:`from_curl`. See also
        :func:`scrapy.utils.request.request_to_curl`.

        .. versionadded:: 2.18.0
        """
        # Imported here to avoid a circular import.
        from scrapy.utils.request import request_to_curl  # noqa: PLC0415

        return request_to_curl(self)

    def to_dict(self, *, spider: scrapy.Spider | None = None) -> dict[str, Any]:
        """Return a dictionary containing the Request's data.

        Use :func:`~scrapy.utils.request.request_from_dict` to convert back into a :class:`~scrapy.Request` object.

        If a spider is given, this method will try to find out the name of the spider methods used as callback
        and errback and include them in the output dict, raising an exception if they cannot be found.
        """
        d = {
            "url": self.url,  # urls are safe (safe_string_url)
            "callback": (
                _find_method(spider, self.callback)
                if callable(self.callback)
                else self.callback
            ),
            "errback": (
                _find_method(spider, self.errback)
                if callable(self.errback)
                else self.errback
            ),
            "headers": {k: list(v) for k, v in self.headers.items()},
            "id": self.id,
        }
        for attr in self.attributes:
            d.setdefault(attr, getattr(self, attr))
        if type(self) is not Request:  # pylint: disable=unidiomatic-typecheck
            d["_class"] = self.__module__ + "." + self.__class__.__name__
        return d


def _find_method(obj: Any, func: Callable[..., Any]) -> str:
    """Helper function for Request.to_dict"""
    # Only instance methods contain ``__func__``
    if obj and hasattr(func, "__func__"):
        members = inspect.getmembers(obj, predicate=inspect.ismethod)
        for name, obj_func in members:
            # We need to use __func__ to access the original function object because instance
            # method objects are generated each time attribute is retrieved from instance.
            #
            # Reference: The standard type hierarchy
            # https://docs.python.org/3/reference/datamodel.html
            if obj_func.__func__ is func.__func__:
                return name
    raise ValueError(f"Function {func} is not an instance method in: {obj!r}")
