"""
This module provides some useful functions for working with
scrapy.Request objects
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import inspect
import json
import logging
import math
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, Self
from urllib.parse import urlunparse
from weakref import WeakKeyDictionary

from w3lib.url import canonicalize_url, safe_url_string

from scrapy import Request, Spider
from scrapy.exceptions import RequestDictError
from scrapy.http import FormRequest, JsonRequest, XmlRpcRequest
from scrapy.http.request import NO_CALLBACK
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import load_object
from scrapy.utils.python import to_bytes

if TYPE_CHECKING:
    from collections.abc import Iterable

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


def request_from_dict(
    d: dict[str, Any], *, spider: Spider | None = None, strict: bool = False
) -> Request:
    """Create a :class:`~scrapy.Request` object from a dict.

    If a spider is given, it will try to resolve the callbacks looking at the
    spider for methods with the same name.

    .. versionchanged:: VERSION
       Added the *strict* parameter.

    If *strict* is true, *d* must be a versioned request dictionary as
    returned by :meth:`Request.to_dict <scrapy.Request.to_dict>` with
    ``strict=True``. It is validated in full before the request is built:

    -   The schema version must be the supported one, and the request type
        must have been registered with :func:`register_request_type`.

    -   Fields must be of the expected type, and unknown fields are rejected.

    -   Callback and errback names must resolve to methods of *spider*.

    Errors are raised as :class:`~scrapy.exceptions.RequestDictError` and
    carry the path of the offending field, e.g. ``meta['a']``.
    """
    if strict:
        return _request_from_dict_strict(d, spider=spider)
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


# Strict, versioned request dictionaries.
#
# The legacy dictionaries produced by Request.to_dict() can name any class and
# do not validate their values before Request is built. The strict format below
# is validated in full before the Request class -- and so any user-defined
# subclass constructor -- is called. It is also JSON-safe: bytes are
# base64-encoded and tuples or dicts with non-string keys use explicit tags, so
# json.dumps()/json.loads() round-trips are lossless.

_REQUEST_DICT_SCHEMA_VERSION = 1
_REQUEST_DICT_TAG = "__scrapy_request_dict__"
_BASE_REQUEST_ATTRIBUTES = frozenset(Request.attributes)
_REQUEST_DICT_FIELDS = (
    "schema_version",
    "request_type",
    "url",
    "method",
    "headers",
    "body",
    "meta",
    "cb_kwargs",
    "flags",
    "cookies",
    "encoding",
    "priority",
    "dont_filter",
    "parent_id",
    "callback",
    "errback",
)
_REQUEST_DICT_OPTIONAL_FIELDS = frozenset({"id", "attributes"})
_REQUEST_DICT_TYPES: dict[str, type[Request]] = {}
_REQUEST_CLASS_NAMES: dict[type[Request], str] = {}


def _fail(path: str, message: str) -> NoReturn:
    raise RequestDictError(f"{path}: {message}")


def _check_request_type_name(name: object, *, path: str) -> str:
    if not isinstance(name, str):
        _fail(path, f"expected str, got {type(name).__name__}")
    if not name or name != name.strip() or any(c.isspace() for c in name):
        _fail(path, "expected a non-empty name without whitespace")
    return name


def _default_request_type_name(cls: type[Request]) -> str:
    qualname = cls.__qualname__
    if "<locals>" in qualname:
        _fail(
            "request_type",
            f"{cls!r} is defined inside a function and cannot be restored in "
            "another process; register it with an explicit name",
        )
    return f"{cls.__module__}.{qualname}"


def register_request_type(cls: type[Request], name: str | None = None) -> str:
    """Register a :class:`~scrapy.Request` subclass for strict request
    dictionaries.

    .. versionadded:: VERSION

    :func:`request_from_dict` only restores registered classes when *strict*
    is true; no class is ever loaded from the dictionary itself. Register the
    class in every process that reads the dictionaries, at import time.

    *cls* is the subclass, and *name* is the value stored in the
    ``request_type`` field. By default the name is the module and qualified
    name of the class. Returns the registered name.
    """
    if not isinstance(cls, type) or not issubclass(cls, Request):
        raise RequestDictError(
            f"request_type: expected a Request subclass, got {cls!r}"
        )
    if name is None:
        name = _default_request_type_name(cls)
    name = _check_request_type_name(name, path="request_type")

    registered_name = _REQUEST_CLASS_NAMES.get(cls)
    if registered_name is not None and registered_name != name:
        raise RequestDictError(
            f"request_type: {cls!r} is already registered as {registered_name!r}"
        )
    registered_cls = _REQUEST_DICT_TYPES.get(name)
    if registered_cls is not None and registered_cls is not cls:
        raise RequestDictError(
            f"request_type: {name!r} is already registered for {registered_cls!r}"
        )

    _REQUEST_DICT_TYPES[name] = cls
    _REQUEST_CLASS_NAMES[cls] = name
    return name


def _register_builtin_request_types() -> None:
    register_request_type(Request, "scrapy.Request")
    register_request_type(FormRequest, "scrapy.FormRequest")
    register_request_type(JsonRequest, "scrapy.JsonRequest")
    register_request_type(XmlRpcRequest, "scrapy.XmlRpcRequest")


_register_builtin_request_types()


def _request_type_name(cls: type[Request]) -> str:
    name = _REQUEST_CLASS_NAMES.get(cls)
    if name is None:
        _fail(
            "request_type",
            f"{cls.__module__}.{cls.__qualname__} is not registered; call "
            "scrapy.utils.request.register_request_type() with it first",
        )
    return name


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: object, path: str) -> bytes:
    if not isinstance(value, str):
        _fail(path, f"expected a base64 str, got {type(value).__name__}")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as ex:
        _fail(path, f"invalid base64 data ({ex})")


def _encode_dict(value: dict[Any, Any], path: str, *, str_keys: bool) -> Any:
    for key in value:
        if not isinstance(key, (str, bytes)):
            _fail(
                f"{path}.key",
                f"dictionary keys must be str or bytes, got {type(key).__name__}",
            )
        if str_keys and not isinstance(key, str):
            _fail(f"{path}[{key!r}]", "dictionary keys must be str")
    if _REQUEST_DICT_TAG not in value and all(isinstance(key, str) for key in value):
        return {
            key: _encode_value(item, f"{path}[{key!r}]", str_keys=str_keys)
            for key, item in value.items()
        }
    # Non-string keys cannot be JSON object keys, and a user dict may contain
    # the tag key; encode both as explicit key/value pairs.
    return {
        _REQUEST_DICT_TAG: "dict",
        "items": [
            [
                _encode_value(key, f"{path}.key", str_keys=False),
                _encode_value(item, f"{path}[{key!r}]", str_keys=str_keys),
            ]
            for key, item in value.items()
        ],
    }


def _encode_value(value: Any, path: str, *, str_keys: bool) -> Any:
    """Return a JSON-safe copy of *value*, tagging non-JSON types."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(path, f"cannot serialize non-finite float {value!r}")
        return value
    if isinstance(value, bytes):
        return {_REQUEST_DICT_TAG: "bytes", "data": _b64encode(value)}
    if isinstance(value, list):
        return [
            _encode_value(item, f"{path}[{index}]", str_keys=str_keys)
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        return {
            _REQUEST_DICT_TAG: "tuple",
            "items": [
                _encode_value(item, f"{path}[{index}]", str_keys=str_keys)
                for index, item in enumerate(value)
            ],
        }
    if isinstance(value, dict):
        return _encode_dict(value, path, str_keys=str_keys)
    _fail(
        path,
        f"cannot serialize object of type {type(value).__name__}; supported "
        "types are None, bool, int, finite float, str, bytes, list, tuple, "
        "and dict",
    )


def _decode_tagged(
    value: dict[str, Any], path: str, *, str_keys: bool
) -> Any:
    tag = value[_REQUEST_DICT_TAG]
    if tag == "bytes":
        if set(value) != {_REQUEST_DICT_TAG, "data"}:
            _fail(path, "invalid encoded bytes value")
        return _b64decode(value["data"], path)
    if tag not in {"tuple", "dict"}:
        _fail(path, f"unknown encoded value tag {tag!r}")
    if set(value) != {_REQUEST_DICT_TAG, "items"}:
        _fail(path, f"invalid encoded {tag} value")
    items = value["items"]
    if not isinstance(items, list):
        _fail(path, f"invalid encoded {tag} value")
    if tag == "tuple":
        return tuple(
            _decode_value(item, f"{path}[{index}]", str_keys=str_keys)
            for index, item in enumerate(items)
        )
    return _decode_key_value_pairs(items, path, str_keys=str_keys)


def _decode_key_value_pairs(
    items: list[Any], path: str, *, str_keys: bool
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for index, item in enumerate(items):
        item_path = f"{path}.items[{index}]"
        if not isinstance(item, list) or len(item) != 2:
            _fail(item_path, "expected a [key, value] pair")
        key = _decode_value(item[0], f"{item_path}[0]", str_keys=False)
        if not isinstance(key, (str, bytes)):
            _fail(f"{item_path}[0]", "expected a str or bytes key")
        if str_keys and not isinstance(key, str):
            _fail(f"{item_path}[0]", "expected a str key")
        if key in result:
            _fail(f"{item_path}[0]", f"duplicate key {key!r}")
        result[key] = _decode_value(item[1], f"{item_path}[1]", str_keys=str_keys)
    return result


def _decode_value(value: Any, path: str, *, str_keys: bool) -> Any:
    """Validate and decode a value produced by :func:`_encode_value`."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(path, f"non-finite float {value!r} is not supported")
        return value
    if isinstance(value, list):
        return [
            _decode_value(item, f"{path}[{index}]", str_keys=str_keys)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        if _REQUEST_DICT_TAG in value:
            return _decode_tagged(value, path, str_keys=str_keys)
        if not all(isinstance(key, str) for key in value):
            _fail(path, "expected a dict with str keys")
        return {
            key: _decode_value(item, f"{path}[{key!r}]", str_keys=str_keys)
            for key, item in value.items()
        }
    _fail(path, f"unsupported value of type {type(value).__name__}")


def _is_valid_method_name(name: str) -> bool:
    # Dotted names could reference objects outside the spider, and dunder
    # names expose implementation details such as __class__.
    return name.isidentifier() and not (name.startswith("__") and name.endswith("__"))


def _callback_names(spider: Spider) -> list[str]:
    """Return candidate attribute names defined on *spider*.

    Instance attributes are listed before class attributes, and the names come
    from the definitions rather than from attribute access, so no property or
    ``__getattr__`` code runs.
    """
    names: dict[str, None] = dict.fromkeys(getattr(spider, "__dict__", {}))
    for cls in type(spider).__mro__:
        for name in vars(cls):
            names.setdefault(name, None)
    return list(names)


def _resolve_static(spider: Spider, name: str) -> Any:
    """Return ``getattr(spider, name)`` without running property code."""
    static = inspect.getattr_static(spider, name)
    if isinstance(static, (staticmethod, classmethod)):
        return static.__get__(spider, type(spider))
    instance_vars = getattr(spider, "__dict__", {})
    if name in instance_vars:
        # Instance attributes, including bound methods, are not descriptors.
        return instance_vars[name]
    if inspect.isfunction(static):
        return static.__get__(spider, type(spider))
    return static


def _is_method_like(value: Any) -> bool:
    return inspect.isfunction(value) or inspect.ismethod(value)


def _same_callable(first: Any, second: Any) -> bool:
    if first is second:
        return True
    if inspect.ismethod(first) and inspect.ismethod(second):
        return first.__self__ is second.__self__ and first.__func__ is second.__func__
    return False


def _encode_callback(
    spider: Spider | None, callback: Any, path: str
) -> str | dict[str, str] | None:
    if callback is None:
        return None
    if callback is NO_CALLBACK:
        return {_REQUEST_DICT_TAG: "no_callback"}
    if not _is_method_like(callback):
        _fail(
            path,
            "expected None, NO_CALLBACK, or a method of the spider passed to "
            "to_dict(); other callables cannot be referenced safely",
        )
    if spider is None:
        _fail(path, "a spider is required to reference a callback by name")
    for name in _callback_names(spider):
        if not _is_valid_method_name(name):
            continue
        try:
            resolved = _resolve_static(spider, name)
        except AttributeError:
            continue
        if _is_method_like(resolved) and _same_callable(resolved, callback):
            return name
    _fail(path, f"cannot find {callback!r} among the methods of the spider")


def _decode_callback(spider: Spider | None, value: Any, path: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        if set(value) == {_REQUEST_DICT_TAG}:
            if value[_REQUEST_DICT_TAG] == "no_callback":
                return NO_CALLBACK
        _fail(path, "invalid callback reference")
    if not isinstance(value, str):
        _fail(path, f"expected None or a str, got {type(value).__name__}")
    if not _is_valid_method_name(value):
        _fail(path, f"invalid method name {value!r}")
    if spider is None:
        _fail(path, "cannot resolve a callback without a spider")
    try:
        static_value = inspect.getattr_static(spider, value)
    except AttributeError:
        _fail(path, f"method {value!r} not found in {spider!r}")
    if isinstance(static_value, property):
        _fail(path, f"{value!r} is a property, not a method")
    resolved = _resolve_static(spider, value)
    if not _is_method_like(resolved):
        _fail(path, f"{value!r} is not a method of {spider!r}")
    return resolved


def _encode_headers(headers: Any) -> list[list[Any]]:
    return [
        [_b64encode(name), [_b64encode(value) for value in values]]
        for name, values in headers.items()
    ]


def _decode_headers(value: Any) -> list[tuple[bytes, list[bytes]]]:
    if not isinstance(value, list):
        _fail("headers", f"expected a list, got {type(value).__name__}")
    result: list[tuple[bytes, list[bytes]]] = []
    seen: set[bytes] = set()
    for index, item in enumerate(value):
        name, values = _decode_header(item, index)
        lowered = name.lower()
        if lowered in seen:
            _fail(f"headers[{index}][0]", f"duplicate header name {name!r}")
        seen.add(lowered)
        result.append((name, values))
    return result


def _decode_header(item: Any, index: int) -> tuple[bytes, list[bytes]]:
    path = f"headers[{index}]"
    if not isinstance(item, list) or len(item) != 2:
        _fail(path, "expected a [name, values] pair")
    name = _b64decode(item[0], f"{path}[0]")
    if not name:
        _fail(f"{path}[0]", "header name must not be empty")
    if not isinstance(item[1], list):
        _fail(f"{path}[1]", "expected a list of header values")
    values = [
        _b64decode(raw_value, f"{path}[1][{value_index}]")
        for value_index, raw_value in enumerate(item[1])
    ]
    return name, values


def _check_cookie_scalar(value: Any, path: str) -> None:
    if not isinstance(value, (bool, int, float, str, bytes)):
        _fail(
            path,
            "cookie values must be bool, int, float, str, or bytes, got "
            f"{type(value).__name__}",
        )


def _validate_verbose_cookies(value: list[Any]) -> None:
    for index, item in enumerate(value):
        path = f"cookies[{index}]"
        if not isinstance(item, dict):
            _fail(path, f"expected a dict, got {type(item).__name__}")
        for key, cookie_value in item.items():
            if not isinstance(key, str):
                _fail(f"{path}.key", "cookie attribute names must be str")
            if key == "secure":
                if not isinstance(cookie_value, bool):
                    _fail(
                        f"{path}[{key!r}]",
                        f"expected bool, got {type(cookie_value).__name__}",
                    )
            else:
                _check_cookie_scalar(cookie_value, f"{path}[{key!r}]")


def _validate_cookies(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, (str, bytes)):
                _fail(f"cookies[{key!r}].key", "cookie names must be str or bytes")
            _check_cookie_scalar(item, f"cookies[{key!r}]")
        return
    if isinstance(value, list):
        _validate_verbose_cookies(value)
        return
    _fail("cookies", f"expected a dict or a list, got {type(value).__name__}")


def _validate_url(url: str, encoding: str, meta: dict[str, Any]) -> None:
    if meta.get("verbatim_url"):
        final_url = url
    else:
        try:
            final_url = safe_url_string(url, encoding)
        except (UnicodeError, ValueError) as ex:
            _fail("url", f"cannot encode URL {url!r}: {ex}")
        if final_url != url:
            _fail(
                "url",
                f"URL would be silently rewritten to {final_url!r}; "
                "store the normalized URL",
            )
    if (
        "://" not in final_url
        and not final_url.startswith("about:")
        and not final_url.startswith("data:")
    ):
        _fail("url", f"missing scheme in {final_url!r}")


def _check_int(value: Any, path: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        expected = "None or int" if optional else "int"
        _fail(path, f"expected {expected}, got {type(value).__name__}")
    return value


def _check_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, f"expected bool, got {type(value).__name__}")
    return value


def _check_str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        _fail(path, f"expected str, got {type(value).__name__}")
    return value


def _validate_string_fields(d: dict[str, Any]) -> tuple[str, str, str]:
    encoding = _check_str(d["encoding"], "encoding")
    try:
        codecs.lookup(encoding)
    except LookupError:
        _fail("encoding", f"unknown codec {encoding!r}")

    url = _check_str(d["url"], "url")
    if not url:
        _fail("url", "must not be empty")

    method = _check_str(d["method"], "method")
    if not method:
        _fail("method", "must not be empty")
    if method != method.upper():
        _fail("method", f"expected an uppercase HTTP method, got {method!r}")
    return url, method, encoding


def _validate_flags(value: Any) -> list[str]:
    if not isinstance(value, list):
        _fail("flags", f"expected a list, got {type(value).__name__}")
    for index, flag in enumerate(value):
        _check_str(flag, f"flags[{index}]")
    return value


def _validate_fields(d: dict[str, Any]) -> dict[str, Any]:
    """Validate and decode every field, without instantiating anything."""
    url, method, encoding = _validate_string_fields(d)
    flags = _validate_flags(d["flags"])

    meta = _decode_value(d["meta"], "meta", str_keys=True)
    if not isinstance(meta, dict):
        _fail("meta", f"expected a dict, got {type(meta).__name__}")
    _validate_url(url, encoding, meta)

    cb_kwargs = _decode_value(d["cb_kwargs"], "cb_kwargs", str_keys=True)
    if not isinstance(cb_kwargs, dict):
        _fail("cb_kwargs", f"expected a dict, got {type(cb_kwargs).__name__}")

    cookies = _decode_value(d["cookies"], "cookies", str_keys=False)
    _validate_cookies(cookies)

    request_id = None
    if "id" in d:
        request_id = _check_int(d["id"], "id")

    return {
        "url": url,
        "method": method,
        "headers": _decode_headers(d["headers"]),
        "body": _b64decode(d["body"], "body"),
        "meta": meta,
        "cb_kwargs": cb_kwargs,
        "flags": flags,
        "cookies": cookies,
        "encoding": encoding,
        "priority": _check_int(d["priority"], "priority"),
        "dont_filter": _check_bool(d["dont_filter"], "dont_filter"),
        "parent_id": _check_int(d["parent_id"], "parent_id", optional=True),
        "id": request_id,
    }


def _validate_request_for_encoding(request: Request) -> None:
    """Reject request values that would produce an unrestorable dictionary.

    These are the values that Request accepts as is, but that the strict
    format cannot validate back on restore.
    """
    try:
        codecs.lookup(request.encoding)
    except LookupError:
        _fail("encoding", f"unknown codec {request.encoding!r}")
    _validate_url(request.url, request.encoding, request.meta)
    _validate_flags(request.flags)
    _validate_cookies(request.cookies)


def _request_to_dict_strict(
    request: Request, *, spider: Spider | None = None
) -> dict[str, Any]:
    request_type = _request_type_name(type(request))
    _validate_request_for_encoding(request)
    d: dict[str, Any] = {
        "schema_version": _REQUEST_DICT_SCHEMA_VERSION,
        "request_type": request_type,
        "id": request.id,
        "url": request.url,
        "method": request.method,
        "headers": _encode_headers(request.headers),
        "body": _b64encode(request.body),
        "meta": _encode_value(request.meta, "meta", str_keys=True),
        "cb_kwargs": _encode_value(request.cb_kwargs, "cb_kwargs", str_keys=True),
        "flags": _encode_value(request.flags, "flags", str_keys=False),
        "cookies": _encode_value(request.cookies, "cookies", str_keys=False),
        "encoding": request.encoding,
        "priority": request.priority,
        "dont_filter": request.dont_filter,
        "parent_id": request.parent_id,
        "callback": _encode_callback(spider, request.callback, "callback"),
        "errback": _encode_callback(spider, request.errback, "errback"),
    }
    d["attributes"] = {
        attr: _encode_value(
            getattr(request, attr),
            f"attributes[{attr!r}]",
            str_keys=False,
        )
        for attr in _extra_attributes(type(request))
    }
    return d


def _extra_attributes(cls: type[Request]) -> list[str]:
    return [
        attr for attr in cls.attributes if attr not in _BASE_REQUEST_ATTRIBUTES
    ]


def _check_dict_structure(d: dict[Any, Any]) -> type[Request]:
    """Validate the field names, schema version, and request type of *d*."""
    allowed = set(_REQUEST_DICT_FIELDS) | _REQUEST_DICT_OPTIONAL_FIELDS
    unknown = sorted(set(d) - allowed, key=str)
    if unknown:
        _fail(str(unknown[0]), "unknown field")

    for name in _REQUEST_DICT_FIELDS:
        if name not in d:
            _fail(name, "missing required field")

    schema_version = d["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        _fail("schema_version", f"expected int, got {type(schema_version).__name__}")
    if schema_version != _REQUEST_DICT_SCHEMA_VERSION:
        _fail(
            "schema_version",
            f"unsupported schema version {schema_version}; supported versions: "
            f"{_REQUEST_DICT_SCHEMA_VERSION}",
        )

    request_type = _check_str(d["request_type"], "request_type")
    request_cls = _REQUEST_DICT_TYPES.get(request_type)
    if request_cls is None:
        _fail(
            "request_type",
            f"unknown request type {request_type!r}; register it with "
            "scrapy.utils.request.register_request_type()",
        )
    return request_cls


def _decode_extra_attributes(
    d: dict[str, Any], request_cls: type[Request]
) -> dict[str, Any]:
    extra_attributes = _extra_attributes(request_cls)
    raw_attributes = d.get("attributes")
    if not extra_attributes:
        if raw_attributes not in (None, {}):
            _fail("attributes", "unexpected for this request type")
        return {}
    if not isinstance(raw_attributes, dict):
        _fail("attributes", "missing required field")
    missing = sorted(set(extra_attributes) - set(raw_attributes))
    if missing:
        _fail(f"attributes[{missing[0]!r}]", "missing required field")
    extra = sorted(set(raw_attributes) - set(extra_attributes), key=str)
    if extra:
        _fail(f"attributes[{extra[0]!r}]", "unknown field")
    return {
        attr: _decode_value(
            raw_attributes[attr], f"attributes[{attr!r}]", str_keys=False
        )
        for attr in extra_attributes
    }


def _request_from_dict_strict(
    d: object, *, spider: Spider | None = None
) -> Request:
    if not isinstance(d, dict):
        _fail("<request>", f"expected a dict, got {type(d).__name__}")
    request_cls = _check_dict_structure(d)
    values = _validate_fields(d)
    attributes = _decode_extra_attributes(d, request_cls)
    callback = _decode_callback(spider, d["callback"], "callback")
    errback = _decode_callback(spider, d["errback"], "errback")

    kwargs: dict[str, Any] = {
        "url": values["url"],
        "callback": callback,
        "method": values["method"],
        "headers": values["headers"],
        "body": values["body"],
        "cookies": values["cookies"],
        "meta": values["meta"],
        "encoding": values["encoding"],
        "priority": values["priority"],
        "dont_filter": values["dont_filter"],
        "errback": errback,
        "flags": values["flags"],
        "cb_kwargs": values["cb_kwargs"],
        "parent_id": values["parent_id"],
        **attributes,
    }
    # Everything above is validation, and nothing has been instantiated. The
    # input dict, its nested values, and the spider are left untouched.
    request = request_cls(**kwargs)
    if values["id"] is not None:
        request._id = values["id"]
    return request


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
