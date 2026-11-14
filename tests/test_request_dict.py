from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from scrapy import Request, Spider
from scrapy.exceptions import RequestDictError
from scrapy.http import FormRequest, JsonRequest, Response, XmlRpcRequest
from scrapy.http.request import NO_CALLBACK
from scrapy.utils.request import register_request_type, request_from_dict

if TYPE_CHECKING:
    from twisted.python.failure import Failure


class CustomRequest(Request):
    pass


@pytest.fixture
def spider() -> MethodsSpider:
    return MethodsSpider()


def _assert_serializes_ok(request: Request, spider: Spider | None = None) -> None:
    d = request.to_dict(spider=spider)
    request2 = request_from_dict(d, spider=spider)
    _assert_same_request(request, request2)


def _assert_same_request(r1: Request, r2: Request) -> None:
    assert r1.__class__ == r2.__class__
    assert r1.url == r2.url
    assert r1.callback == r2.callback
    assert r1.errback == r2.errback
    assert r1.method == r2.method
    assert r1.body == r2.body
    assert r1.headers == r2.headers
    assert r1.cookies == r2.cookies
    assert r1.meta == r2.meta
    assert r1.cb_kwargs == r2.cb_kwargs
    assert r1.encoding == r2.encoding
    assert r1._encoding == r2._encoding
    assert r1.priority == r2.priority
    assert r1.dont_filter == r2.dont_filter
    assert r1.flags == r2.flags
    assert r1.id == r2.id
    assert r1.parent_id == r2.parent_id
    if isinstance(r1, JsonRequest):
        assert isinstance(r2, JsonRequest)
        assert r1.dumps_kwargs == r2.dumps_kwargs


def test_basic() -> None:
    r = Request("http://www.example.com")
    _assert_serializes_ok(r)


def test_no_id() -> None:
    r = Request("http://www.example.com")
    d = r.to_dict()
    del d["id"]
    assert request_from_dict(d).id != r.id


def test_all_attributes(spider: MethodsSpider) -> None:
    r = Request(
        url="http://www.example.com",
        callback=spider.parse_item,
        errback=spider.handle_error,
        method="POST",
        body=b"some body",
        headers={"content-encoding": "text/html; charset=latin-1"},
        cookies={"currency": "руб"},
        encoding="latin-1",
        priority=20,
        meta={"a": "b"},
        cb_kwargs={"k": "v"},
        flags=["testFlag"],
        parent_id=1,
    )
    _assert_serializes_ok(r, spider=spider)


def test_headers() -> None:
    r = Request(
        "http://www.example.com",
        headers=[("X-Multi", "a"), ("x-multi", "b"), ("dnt", "1"), ("Accept", None)],
    )
    headers = r.to_dict()["headers"]
    assert type(headers) is dict
    assert headers == {b"X-Multi": [b"a", b"b"], b"dnt": [b"1"], b"Accept": []}
    headers[b"dnt"].append(b"2")
    assert r.headers.getlist("dnt") == [b"1"]
    _assert_serializes_ok(r)


def test_latin1_body() -> None:
    r = Request("http://www.example.com", body=b"\xa3")
    _assert_serializes_ok(r)


def test_utf8_body() -> None:
    r = Request("http://www.example.com", body=b"\xc2\xa3")
    _assert_serializes_ok(r)


def test_request_class(spider: MethodsSpider) -> None:
    r1 = CustomRequest("http://www.example.com")
    _assert_serializes_ok(r1, spider=spider)
    r2 = JsonRequest("http://www.example.com", dumps_kwargs={"indent": 4})
    _assert_serializes_ok(r2, spider=spider)


def test_callback_serialization(spider: MethodsSpider) -> None:
    r = Request(
        "http://www.example.com",
        callback=spider.parse_item,
        errback=spider.handle_error,
    )
    _assert_serializes_ok(r, spider=spider)


def test_reference_callback_serialization(spider: MethodsSpider) -> None:
    r = Request(
        "http://www.example.com",
        callback=spider.parse_item_reference,  # type: ignore[arg-type,misc]
        errback=spider.handle_error_reference,  # type: ignore[arg-type,misc]
    )
    _assert_serializes_ok(r, spider=spider)
    request_dict = r.to_dict(spider=spider)
    assert request_dict["callback"] == "parse_item_reference"
    assert request_dict["errback"] == "handle_error_reference"


def test_private_reference_callback_serialization(spider: MethodsSpider) -> None:
    r = Request(
        "http://www.example.com",
        callback=spider._MethodsSpider__parse_item_reference,  # type: ignore[attr-defined]
        errback=spider._MethodsSpider__handle_error_reference,  # type: ignore[attr-defined]
    )
    _assert_serializes_ok(r, spider=spider)
    request_dict = r.to_dict(spider=spider)
    assert request_dict["callback"] == "_MethodsSpider__parse_item_reference"
    assert request_dict["errback"] == "_MethodsSpider__handle_error_reference"


def test_private_callback_serialization(spider: MethodsSpider) -> None:
    r = Request(
        "http://www.example.com",
        callback=spider._MethodsSpider__parse_item_private,  # type: ignore[attr-defined]
        errback=spider.handle_error,
    )
    _assert_serializes_ok(r, spider=spider)


def test_mixin_private_callback_serialization(spider: MethodsSpider) -> None:
    r = Request(
        "http://www.example.com",
        callback=spider._SpiderMixin__mixin_callback,  # type: ignore[attr-defined]
        errback=spider.handle_error,
    )
    _assert_serializes_ok(r, spider=spider)


def test_delegated_callback_serialization(spider: MethodsSpider) -> None:
    r = Request(
        "http://www.example.com",
        callback=spider.delegated_callback,
        errback=spider.handle_error,
    )
    _assert_serializes_ok(r, spider=spider)


def test_unserializable_callback1(spider: MethodsSpider) -> None:
    r = Request("http://www.example.com", callback=lambda x: x)  # type: ignore[misc]
    with pytest.raises(
        ValueError, match="is not an instance method in: <MethodsSpider"
    ):
        r.to_dict(spider=spider)


def test_unserializable_callback2(spider: MethodsSpider) -> None:
    r = Request("http://www.example.com", callback=spider.parse_item)
    with pytest.raises(ValueError, match="is not an instance method in: None"):
        r.to_dict(spider=None)


def test_unserializable_callback3() -> None:
    """Parser method is removed or replaced dynamically."""

    class MySpider(Spider):
        name = "my_spider"

        def parse(self, response: Response) -> None:
            pass

    spider = MySpider()
    r = Request("http://www.example.com", callback=spider.parse)
    spider.parse = None  # type: ignore[method-assign,assignment]
    with pytest.raises(ValueError, match="is not an instance method in: <MySpider"):
        r.to_dict(spider=spider)


def test_callback_not_available() -> None:
    """Callback method is not available in the spider passed to from_dict"""
    spider = SpiderDelegation()
    r = Request("http://www.example.com", callback=spider.delegated_callback)
    d = r.to_dict(spider=spider)  # type: ignore[arg-type]
    with pytest.raises(
        ValueError, match="Method 'delegated_callback' not found in: <Spider"
    ):
        request_from_dict(d, spider=Spider("foo"))


class SpiderMixin:
    def __mixin_callback(  # pylint: disable=unused-private-member
        self, response: Response
    ) -> None:
        pass


class SpiderDelegation:
    def delegated_callback(self, response: Response) -> None:
        pass


def parse_item(response: Response) -> None:
    pass


def handle_error(failure: Failure) -> None:
    pass


def private_parse_item(response: Response) -> None:
    pass


def private_handle_error(failure: Failure) -> None:
    pass


class MethodsSpider(Spider, SpiderMixin):
    name = "test"
    parse_item_reference = parse_item
    handle_error_reference = handle_error
    __parse_item_reference = private_parse_item
    __handle_error_reference = private_handle_error

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.delegated_callback = SpiderDelegation().delegated_callback

    def parse_item(self, response: Response) -> None:
        pass

    def handle_error(self, failure: Failure) -> None:
        pass

    def __parse_item_private(  # pylint: disable=unused-private-member
        self, response: Response
    ) -> None:
        pass


# Strict, versioned request dictionaries


class _StrictSpider(Spider):
    name = "strict"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.instance_callback = types.MethodType(lambda self, response: None, self)

    def parse_item(self, response: Response) -> None:
        pass

    def handle_error(self, failure: Failure) -> None:
        pass


class _StrictMixin:
    def __mixin_callback(  # pylint: disable=unused-private-member
        self, response: Response
    ) -> None:
        pass


class _StrictSpiderWithMixin(_StrictSpider, _StrictMixin):
    name = "strict_mixin"


class _StrictCustomRequest(Request):
    pass


class _SpyRequest(Request):
    """Request whose constructor records the keyword arguments it receives."""

    constructed: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).constructed.append(kwargs)
        super().__init__(*args, **kwargs)


register_request_type(_StrictCustomRequest)
register_request_type(_SpyRequest, "tests.test_request_dict._SpyRequest")


@pytest.fixture
def strict_spider() -> _StrictSpiderWithMixin:
    return _StrictSpiderWithMixin()


def _strict_round_trip(
    request: Request, strict_spider: _StrictSpiderWithMixin
) -> Request:
    """Serialize, persist as JSON text, reload, and validate."""
    d = request.to_dict(spider=strict_spider, strict=True)
    persisted = json.loads(json.dumps(d))
    return request_from_dict(persisted, spider=strict_spider, strict=True)


@pytest.mark.parametrize(
    "req",
    [
        Request("http://www.example.com"),
        Request("http://www.example.com/a%20b?q=%D1%80%D1%83%D0%B1"),
        Request("https://www.example.com", method="POST", body=b"\x00\xff\xfe"),
        Request(
            "http://www.example.com",
            headers=[
                (b"X-Multi", b"a"),
                (b"X-Multi", b"\xff\x00"),
                (b"accept", None),
            ],
        ),
        Request(
            "http://www.example.com",
            cookies={b"name": b"\xff", "other": "value", "flag": True},
            encoding="latin-1",
            priority=-5,
            dont_filter=True,
            flags=["flag-a", "flag-b"],
            parent_id=1,
        ),
        Request(
            "http://www.example.com",
            meta={
                "str": "text",
                "bytes": b"\x00\xff",
                "tuple": (1, "two", (b"three",)),
                "dict": {"a": [1, 2.5, None, True]},
            },
            cb_kwargs={"key": [b"\xff", ("nested",)]},
        ),
        JsonRequest(
            "http://www.example.com",
            body=b'{"a": 1}',
            meta={"dumps_used": True},
        ),
        _StrictCustomRequest(
            "http://www.example.com",
            meta={"custom": b"payload"},
            headers={b"X-Custom": b"value"},
        ),
    ],
)
def test_strict_round_trip(
    req: Request, strict_spider: _StrictSpiderWithMixin
) -> None:
    request2 = _strict_round_trip(req, spider)
    _assert_same_request(req, request2)
    assert request2.url == req.url


def test_strict_schema_fields(strict_spider: _StrictSpiderWithMixin) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    assert d["schema_version"] == 1
    assert d["request_type"] == "scrapy.Request"
    assert set(d) == {
        "schema_version",
        "request_type",
        "id",
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
        "attributes",
    }
    json.dumps(d)


def test_strict_bytes_round_trip(strict_spider: _StrictSpiderWithMixin) -> None:
    body = bytes(range(256))
    header_value = bytes(range(256))
    r = Request(
        "http://www.example.com",
        method="POST",
        body=body,
        headers={b"X-Bytes": [header_value, b""]},
        meta={"bytes": body},
        cb_kwargs={"bytes": body},
        cookies={b"k": header_value},
    )
    r2 = _strict_round_trip(r, strict_spider)
    assert r2.body == body
    assert r2.headers.getlist(b"X-Bytes") == [header_value, b""]
    assert r2.meta["bytes"] == body
    assert r2.cb_kwargs["bytes"] == body
    assert r2.cookies[b"k"] == header_value


def test_strict_callback_references(strict_spider: _StrictSpiderWithMixin) -> None:
    for callback in (
        strict_spider.parse_item,
        strict_spider.handle_error,
        strict_spider._StrictMixin__mixin_callback,  # type: ignore[attr-defined]
        strict_spider.instance_callback,
        None,
    ):
        r = Request("http://www.example.com", callback=callback)
        d = r.to_dict(spider=strict_spider, strict=True)
        r2 = request_from_dict(
            json.loads(json.dumps(d)), spider=strict_spider, strict=True
        )
        if callback is None:
            assert r2.callback is None
        else:
            assert callable(r2.callback)
            assert r2.callback == callback


def test_strict_no_callback(strict_spider: _StrictSpiderWithMixin) -> None:
    r = Request("http://www.example.com", callback=NO_CALLBACK)
    d = r.to_dict(spider=strict_spider, strict=True)
    assert d["callback"] == _tagged("no_callback")
    r2 = request_from_dict(json.loads(json.dumps(d)), strict=True)
    assert r2.callback is NO_CALLBACK


def test_strict_delegated_method_callback(
    strict_spider: _StrictSpiderWithMixin,
) -> None:
    class SpiderDelegation:
        def delegated_callback(self, response: Response) -> None:
            pass

    strict_spider.delegated = SpiderDelegation().delegated_callback
    r = Request("http://www.example.com", callback=strict_spider.delegated)
    d = r.to_dict(spider=strict_spider, strict=True)
    assert d["callback"] == "delegated"
    r2 = request_from_dict(
        json.loads(json.dumps(d)), spider=strict_spider, strict=True
    )
    assert r2.callback.__func__ is SpiderDelegation.delegated_callback


def test_strict_no_callback_sentinel_forged(
    strict_spider: _StrictSpiderWithMixin,
) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    d["callback"] = _tagged("no_callback", extra=1)
    with pytest.raises(RequestDictError, match="callback:"):
        request_from_dict(d, spider=strict_spider, strict=True)


def test_strict_classmethod_callback(strict_spider: _StrictSpiderWithMixin) -> None:
    class WithClassMethod(Spider):
        name = "with_classmethod"

        @classmethod
        def parse_item(cls, response: Response) -> None:
            pass

    spider = WithClassMethod()
    r = Request("http://www.example.com", callback=spider.parse_item)
    d = r.to_dict(spider=spider, strict=True)
    assert d["callback"] == "parse_item"
    r2 = request_from_dict(
        json.loads(json.dumps(d)), spider=spider, strict=True
    )
    assert r2.callback is not None


def test_strict_errback_reference(strict_spider: _StrictSpiderWithMixin) -> None:
    r = Request("http://www.example.com", errback=strict_spider.handle_error)
    r2 = _strict_round_trip(r, strict_spider)
    assert isinstance(r2.errback, types.MethodType)
    assert r2.errback.__func__ is _StrictSpider.handle_error


@pytest.mark.parametrize(
    "callback",
    [
        lambda response: None,
        staticmethod(lambda response: None).__func__,
    ],
    ids=["lambda", "function"],
)
def test_strict_unserializable_callback(
    strict_spider: _StrictSpiderWithMixin, callback: Any
) -> None:
    r = Request("http://www.example.com", callback=callback)
    with pytest.raises(RequestDictError, match="callback:"):
        r.to_dict(spider=strict_spider, strict=True)


def test_strict_callback_requires_spider() -> None:
    spider = _StrictSpiderWithMixin()
    r = Request("http://www.example.com", callback=spider.parse_item)
    with pytest.raises(RequestDictError, match="callback:"):
        r.to_dict(strict=True)


def test_strict_callback_of_another_object(
    strict_spider: _StrictSpiderWithMixin,
) -> None:
    class Other:
        def callback(self, response: Response) -> None:
            pass

    r = Request("http://www.example.com", callback=Other().callback)
    with pytest.raises(RequestDictError, match="callback:"):
        r.to_dict(spider=strict_spider, strict=True)


@pytest.mark.parametrize(
    "name",
    [
        "__class__",
        "__init__",
        "os.system",
        "subprocess",
        "a b",
        "",
        1,
        b"parse",
    ],
)
def test_strict_malicious_callback_reference(
    strict_spider: _StrictSpiderWithMixin, name: Any
) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    d["callback"] = name
    with pytest.raises(RequestDictError, match="callback:"):
        request_from_dict(d, spider=strict_spider, strict=True)


@pytest.mark.parametrize(
    "name",
    ["missing", "some_attribute", "__class__"],
)
def test_strict_callback_not_on_spider(
    strict_spider: _StrictSpiderWithMixin, name: str
) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    d["callback"] = name
    with pytest.raises(RequestDictError, match="callback:"):
        request_from_dict(d, spider=strict_spider, strict=True)


def test_strict_callback_without_spider(strict_spider: _StrictSpiderWithMixin) -> None:
    d = Request(
        "http://www.example.com", callback=strict_spider.parse_item
    ).to_dict(spider=strict_spider, strict=True)
    with pytest.raises(RequestDictError, match="callback:"):
        request_from_dict(d, strict=True)




def test_strict_unknown_version(strict_spider: _StrictSpiderWithMixin) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    for version in (0, 2, 99, -1):
        d["schema_version"] = version
        with pytest.raises(
            RequestDictError, match="schema_version: unsupported schema version"
        ):
            request_from_dict(d, spider=strict_spider, strict=True)


@pytest.mark.parametrize("version", ["1", 1.0, None, True])
def test_strict_invalid_version_type(
    strict_spider: _StrictSpiderWithMixin, version: Any
) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    d["schema_version"] = version
    with pytest.raises(RequestDictError, match="schema_version:"):
        request_from_dict(d, spider=strict_spider, strict=True)


def test_strict_missing_version(strict_spider: _StrictSpiderWithMixin) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    del d["schema_version"]
    with pytest.raises(RequestDictError, match="schema_version:"):
        request_from_dict(d, spider=strict_spider, strict=True)


def test_strict_unknown_request_type(strict_spider: _StrictSpiderWithMixin) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    for request_type in (
        "os.system",
        "scrapy.Requests",
        "tests.test_request_dict.CustomRequest",
        "scrapy.http.Request",
    ):
        d["request_type"] = request_type
        with pytest.raises(RequestDictError, match="request_type:"):
            request_from_dict(d, spider=strict_spider, strict=True)


def test_strict_request_type_not_str(strict_spider: _StrictSpiderWithMixin) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    d["request_type"] = Request
    with pytest.raises(RequestDictError, match="request_type:"):
        request_from_dict(d, spider=strict_spider, strict=True)


@pytest.mark.parametrize(
    "field",
    [
        "callback",
        "cookies",
        "dont_filter",
        "encoding",
        "errback",
        "flags",
        "headers",
        "meta",
        "method",
        "parent_id",
        "priority",
        "url",
        "schema_version",
        "request_type",
    ],
)
def test_strict_missing_field(
    strict_spider: _StrictSpiderWithMixin, field: str
) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    del d[field]
    with pytest.raises(RequestDictError, match=f"{re.escape(field)}:"):
        request_from_dict(d, spider=strict_spider, strict=True)


def test_strict_unknown_fields(strict_spider: _StrictSpiderWithMixin) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    for field in ("unknown", "_class", "data", "params", "formdata"):
        d[field] = "value"
        with pytest.raises(
            RequestDictError, match=f"{re.escape(field)}: unknown field"
        ):
            request_from_dict(d, spider=strict_spider, strict=True)
        del d[field]


def test_strict_legacy_class_field_rejected(
    strict_spider: _StrictSpiderWithMixin,
) -> None:
    legacy = _StrictCustomRequest("http://www.example.com").to_dict()
    assert legacy["_class"] == (
        "tests.test_request_dict._StrictCustomRequest"
    )
    with pytest.raises(RequestDictError, match="_class: unknown field"):
        request_from_dict(legacy, spider=strict_spider, strict=True)


def test_strict_non_dict_input(strict_spider: _StrictSpiderWithMixin) -> None:
    for value in ([], (), "text", None, 1):
        with pytest.raises(
            RequestDictError, match=re.escape("<request>: expected a dict")
        ):
            request_from_dict(  # type: ignore[arg-type]
                value, spider=strict_spider, strict=True
            )


@pytest.mark.parametrize(
    "field, value, path",
    [
        ("url", "", "url"),
        ("url", 1, "url"),
        ("url", "http://www.example.com/a b", "url"),
        ("url", "example.com", "url"),
        ("url", "http://www.example.com/\ud800", "url"),
        ("method", "", "method"),
        ("method", "get", "method"),
        ("method", None, "method"),
        ("encoding", "no-such-codec", "encoding"),
        ("encoding", 1, "encoding"),
        ("body", "not base64!", "body"),
        ("headers", {}, "headers"),
        ("headers", [[1, []]], "headers[0][0]"),
        ("headers", [["not base64!", []]], "headers[0][0]"),
        ("headers", [["YQ=="], []], "headers[0]"),
        ("meta", [], "meta"),
        ("cb_kwargs", "text", "cb_kwargs"),
        ("flags", "text", "flags"),
        ("flags", [1], "flags[0]"),
        ("priority", True, "priority"),
        ("priority", "1", "priority"),
        ("dont_filter", 1, "dont_filter"),
        ("parent_id", True, "parent_id"),
        ("parent_id", "1", "parent_id"),
        ("id", True, "id"),
        ("id", "1", "id"),
    ],
)
def test_strict_invalid_fields(
    strict_spider: _StrictSpiderWithMixin, field: str, value: Any, path: str
) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    d[field] = value
    with pytest.raises(
        RequestDictError, match=f"{re.escape(path)}:"
    ) as exc_info:
        request_from_dict(d, spider=strict_spider, strict=True)
    assert str(exc_info.value).startswith(f"{path}:")


@pytest.mark.parametrize(
    "meta, path",
    [
        ({"a": object()}, "meta['a']"),
        ({"a": {1, 2}}, "meta['a']"),
        ({"a": {b"k": "v"}}, "meta['a'][b'k']"),
        ({"a": float("nan")}, "meta['a']"),
        ({"a": (1, object())}, "meta['a'][1]"),
    ],
)
def test_strict_unserializable_meta(
    strict_spider: _StrictSpiderWithMixin, meta: dict[str, Any], path: str
) -> None:
    r = Request("http://www.example.com")
    r.meta.update(meta)
    with pytest.raises(
        RequestDictError,
        match=re.escape(path) + ":",
    ):
        r.to_dict(spider=strict_spider, strict=True)


def test_strict_unserializable_cb_kwargs(strict_spider: _StrictSpiderWithMixin) -> None:
    r = Request("http://www.example.com")
    r.cb_kwargs["a"] = object()
    with pytest.raises(RequestDictError, match=re.escape("cb_kwargs['a']:")):
        r.to_dict(spider=strict_spider, strict=True)


_TAG = "__scrapy_request_dict__"


def _tagged(kind: str, **kwargs: Any) -> dict[str, Any]:
    return {_TAG: kind, **kwargs}


@pytest.mark.parametrize(
    "cookies",
    [
        {"name": object()},
        {"name": [b"value"]},
        [{"name": "n", "value": "v", "secure": "yes"}],
        [{b"name": b"n", "value": b"v"}],
        [["name", "value"]],
        [{"name": "n", "value": "v", "secure": 1}],
    ],
    ids=[
        "object-value",
        "list-value",
        "secure-not-bool",
        "bytes-attribute-name",
        "not-a-dict",
        "secure-int",
    ],
)
def test_strict_unserializable_cookies(
    strict_spider: _StrictSpiderWithMixin, cookies: Any
) -> None:
    r = Request("http://www.example.com")
    r.cookies = cookies
    with pytest.raises(RequestDictError, match="cookies"):
        r.to_dict(spider=strict_spider, strict=True)


@pytest.mark.parametrize(
    "cookies",
    [
        "text",
        {"name": [_tagged("bytes", data="dmFsdWU=")]},
        [["name", "value"]],
        [{"name": "n", "value": "v", "secure": "yes"}],
        [{"name": "n", "value": "v", "secure": 1}],
    ],
)
def test_strict_invalid_cookies_in_dict(
    strict_spider: _StrictSpiderWithMixin, cookies: Any
) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    d["cookies"] = cookies
    with pytest.raises(RequestDictError, match="cookies"):
        request_from_dict(d, spider=strict_spider, strict=True)


@pytest.mark.parametrize(
    "cookies",
    [
        {"name": "value", "flag": True},
        {b"name": b"\xff"},
        [{"name": "name", "value": "value", "secure": True}],
        {},
        [],
    ],
)
def test_strict_valid_cookies(
    strict_spider: _StrictSpiderWithMixin, cookies: Any
) -> None:
    r = Request("http://www.example.com", cookies=cookies)
    r2 = _strict_round_trip(r, strict_spider)
    assert r2.cookies == r.cookies


def test_strict_validation_before_construction(
    strict_spider: _StrictSpiderWithMixin,
) -> None:
    _SpyRequest.constructed.clear()
    r = _SpyRequest("http://www.example.com")
    d = r.to_dict(spider=strict_spider, strict=True)
    _SpyRequest.constructed.clear()

    d["priority"] = "1"
    with pytest.raises(RequestDictError):
        request_from_dict(d, spider=strict_spider, strict=True)
    assert _SpyRequest.constructed == []


def test_strict_does_not_mutate_input_or_spider(
    strict_spider: _StrictSpiderWithMixin,
) -> None:
    nested = {"deep": [1, b"x", (b"y",)]}
    r = _SpyRequest(
        "http://www.example.com",
        meta={"nested": nested},
        cb_kwargs={"data": [1]},
        flags=["flag"],
        headers={b"X-Test": [b"value"]},
        cookies={b"cookie": b"value"},
    )
    d = r.to_dict(spider=strict_spider, strict=True)
    snapshot = copy.deepcopy(d)
    _SpyRequest.constructed.clear()

    r2 = request_from_dict(d, spider=strict_spider, strict=True)
    assert d == snapshot
    assert nested == {"deep": [1, b"x", (b"y",)]}
    assert "meta" not in strict_spider.__dict__

    # Mutations of the restored object must not reach the dict or the spider.
    r2.headers.getlist(b"X-Test").append(b"value2")
    r2.meta["nested"]["deep"].append(2)
    r2.flags.append("flag2")
    r2.cb_kwargs["data"].append(2)
    assert d == snapshot
    assert nested == {"deep": [1, b"x", (b"y",)]}
    assert r2.meta["nested"] is not nested


def test_strict_restored_objects_do_not_share_containers(
    strict_spider: _StrictSpiderWithMixin,
) -> None:
    d = Request(
        "http://www.example.com",
        meta={"nested": {"items": [1]}},
        cb_kwargs={"items": [1]},
        flags=["flag"],
        headers={b"X-Test": [b"value"]},
        cookies={b"cookie": b"value"},
    ).to_dict(spider=strict_spider, strict=True)
    first = request_from_dict(
        json.loads(json.dumps(d)), spider=strict_spider, strict=True
    )
    second = request_from_dict(
        json.loads(json.dumps(d)), spider=strict_spider, strict=True
    )

    first.meta["nested"]["items"].append(2)
    first.cb_kwargs["items"].append(2)
    first.flags.append("flag2")
    first.headers.getlist(b"X-Test").append(b"value2")

    assert second.meta == {"nested": {"items": [1]}}
    assert second.cb_kwargs == {"items": [1]}
    assert second.flags == ["flag"]
    assert second.headers.getlist(b"X-Test") == [b"value"]
    assert first.meta is not second.meta
    assert first.headers is not second.headers
    assert first.flags is not second.flags
    assert first.cb_kwargs is not second.cb_kwargs


def test_strict_unregistered_subclass(strict_spider: _StrictSpiderWithMixin) -> None:
    class Unregistered(Request):
        pass

    r = Unregistered("http://www.example.com")
    with pytest.raises(RequestDictError, match="request_type:"):
        r.to_dict(spider=strict_spider, strict=True)


def test_strict_registered_subclass(strict_spider: _StrictSpiderWithMixin) -> None:
    r = _StrictCustomRequest(
        "http://www.example.com", meta={"a": b"b"}, headers={b"X": b"y"}
    )
    d = r.to_dict(spider=strict_spider, strict=True)
    assert d["request_type"] == "tests.test_request_dict._StrictCustomRequest"
    r2 = request_from_dict(
        json.loads(json.dumps(d)), spider=strict_spider, strict=True
    )
    assert type(r2) is _StrictCustomRequest
    _assert_same_request(r, r2)


def test_strict_subclass_extra_attributes(
    strict_spider: _StrictSpiderWithMixin,
) -> None:
    r = JsonRequest("http://www.example.com", dumps_kwargs={"indent": 4})
    d = r.to_dict(spider=strict_spider, strict=True)
    assert d["request_type"] == "scrapy.JsonRequest"
    assert "dumps_kwargs" not in d
    assert d["attributes"] == {
        "dumps_kwargs": {
            "indent": 4,
            "sort_keys": True,
        }
    }
    r2 = request_from_dict(
        json.loads(json.dumps(d)), spider=strict_spider, strict=True
    )
    assert isinstance(r2, JsonRequest)
    assert r2.dumps_kwargs == r.dumps_kwargs


def test_strict_subclass_extra_attributes_unknown(
    strict_spider: _StrictSpiderWithMixin,
) -> None:
    d = Request("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    d["attributes"] = {"unknown": 1}
    with pytest.raises(RequestDictError, match="attributes:"):
        request_from_dict(d, spider=strict_spider, strict=True)

    d = JsonRequest("http://www.example.com").to_dict(spider=strict_spider, strict=True)
    d["attributes"] = {}
    with pytest.raises(RequestDictError, match="attributes"):
        request_from_dict(d, spider=strict_spider, strict=True)


def test_strict_register_request_type_errors() -> None:
    class UnregisteredA(Request):
        pass

    class UnregisteredB(Request):
        pass

    name = register_request_type(UnregisteredA, "tests.registered_a")
    assert name == "tests.registered_a"
    assert register_request_type(UnregisteredA, "tests.registered_a") == name
    with pytest.raises(RequestDictError, match="expected a Request subclass"):
        register_request_type(object, "tests.not_a_request")  # type: ignore[arg-type]
    with pytest.raises(RequestDictError, match="without whitespace"):
        register_request_type(UnregisteredB, "tests.registered b")
    with pytest.raises(RequestDictError, match="without whitespace"):
        register_request_type(UnregisteredB, "")
    with pytest.raises(RequestDictError, match="expected str"):
        register_request_type(UnregisteredB, 1)  # type: ignore[arg-type]
    with pytest.raises(RequestDictError, match="is defined inside a function"):
        register_request_type(UnregisteredB)


def test_strict_default_registered_types(
    strict_spider: _StrictSpiderWithMixin,
) -> None:
    for cls, name in (
        (Request, "scrapy.Request"),
        (FormRequest, "scrapy.FormRequest"),
        (JsonRequest, "scrapy.JsonRequest"),
        (XmlRpcRequest, "scrapy.XmlRpcRequest"),
    ):
        r = cls("http://www.example.com")
        d = r.to_dict(spider=strict_spider, strict=True)
        assert d["request_type"] == name
        r2 = request_from_dict(
            json.loads(json.dumps(d)), spider=strict_spider, strict=True
        )
        assert type(r2) is cls


def test_strict_local_class(strict_spider: _StrictSpiderWithMixin) -> None:
    class LocalRequest(Request):
        pass

    r = LocalRequest("http://www.example.com")
    with pytest.raises(RequestDictError, match="request_type:"):
        r.to_dict(spider=strict_spider, strict=True)
    register_request_type(LocalRequest, "tests.local_request")
    r2 = request_from_dict(
        json.loads(json.dumps(r.to_dict(spider=strict_spider, strict=True))),
        spider=spider,
        strict=True,
    )
    assert type(r2) is LocalRequest


def test_strict_tag_collision(strict_spider: _StrictSpiderWithMixin) -> None:
    meta = {
        "user": _tagged("not bytes", data="text"),
        "list": [_tagged("tuple", items=[1])],
        "tuple": (_tagged("dict", items=[]),),
    }
    r = Request("http://www.example.com", meta=meta)
    r2 = _strict_round_trip(r, strict_spider)
    assert r2.meta == meta
    assert isinstance(r2.meta["tuple"], tuple)
    assert isinstance(r2.meta["tuple"][0], dict)


def test_strict_verbatim_url(strict_spider: _StrictSpiderWithMixin) -> None:
    url = "http://www.example.com/a b?c=d e"
    r = Request(url, meta={"verbatim_url": True})
    d = r.to_dict(spider=strict_spider, strict=True)
    r2 = request_from_dict(
        json.loads(json.dumps(d)), spider=strict_spider, strict=True
    )
    assert r2.url == url
    assert r2.meta["verbatim_url"] is True


def test_strict_url_without_verbatim(strict_spider: _StrictSpiderWithMixin) -> None:
    r = Request("http://www.example.com/a b")
    d = r.to_dict(spider=strict_spider, strict=True)
    d["url"] = "http://www.example.com/a b"
    with pytest.raises(RequestDictError, match="url:"):
        request_from_dict(d, spider=strict_spider, strict=True)


def test_strict_no_id(strict_spider: _StrictSpiderWithMixin) -> None:
    r = Request("http://www.example.com")
    d = r.to_dict(spider=strict_spider, strict=True)
    del d["id"]
    r2 = request_from_dict(d, spider=strict_spider, strict=True)
    assert r2.id != r.id


def test_strict_dict_is_stable(
    strict_spider: _StrictSpiderWithMixin,
) -> None:
    """Serializing a restored request yields the same dictionary."""
    r = Request(
        "http://www.example.com",
        method="POST",
        body=b"\x00",
        headers={b"A": [b"\xff"]},
        meta={"a": (b"x", 1)},
        cb_kwargs={"a": [1.5]},
        flags=["x"],
        cookies={b"c": b"v"},
        priority=3,
        dont_filter=True,
        parent_id=9,
        callback=strict_spider.parse_item,
    )
    d1 = r.to_dict(spider=strict_spider, strict=True)
    r2 = request_from_dict(
        json.loads(json.dumps(d1)), spider=strict_spider, strict=True
    )
    d2 = r2.to_dict(spider=strict_spider, strict=True)
    assert d1 == d2


def test_strict_default_legacy_compatibility() -> None:
    """The default, legacy interface is unchanged."""
    r = Request("http://www.example.com")
    legacy = r.to_dict()
    assert "schema_version" not in legacy
    assert "request_type" not in legacy
    restored = request_from_dict(legacy)
    assert restored.id == r.id
    assert restored.url == r.url
    assert restored.headers == r.headers


def test_strict_temp_persistence(
    strict_spider: _StrictSpiderWithMixin, tmp_path: Path
) -> None:
    """Round-trip through a file written and read by another process."""

    r = _StrictCustomRequest(
        "http://www.example.com",
        method="POST",
        body=b"\x00\xff",
        headers={b"X-Bytes": [b"\xff\x00"]},
        meta={"nested": {"data": [b"x", (1, "two")]}},
        cb_kwargs={"data": b"value"},
        flags=["a", "b"],
        cookies={b"k": b"v"},
        encoding="latin-1",
        priority=7,
        dont_filter=True,
        parent_id=42,
        callback=strict_spider.parse_item,
        errback=strict_spider.handle_error,
    )
    path = tmp_path / "request.json"
    path.write_text(json.dumps(r.to_dict(spider=strict_spider, strict=True)))

    code = (
        "import json, sys\n"
        "from scrapy.utils.request import request_from_dict\n"
        "from tests.test_request_dict import _StrictSpiderWithMixin\n"
        "spider = _StrictSpiderWithMixin()\n"
        "d = json.load(open(sys.argv[1]))\n"
        "r = request_from_dict(d, spider=spider, strict=True)\n"
        "print(type(r).__name__, r.url, r.body.hex(), r.meta['nested']['data'][1][0])\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "_StrictCustomRequest",
        "http://www.example.com/",
        "00ff",
        "1",
    ]
