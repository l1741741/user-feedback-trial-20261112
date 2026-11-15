from __future__ import annotations

import copy
from typing import Any

import pytest

from scrapy import Request
from scrapy.http import Headers, JsonRequest
from scrapy.utils.request import fingerprint


class _Untouchable:
    """An object whose copy protocol must never run."""

    def __init__(self) -> None:
        self.copies = 0

    def __copy__(self) -> Any:
        self.copies += 1
        raise AssertionError("__copy__ must not be called")

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        self.copies += 1
        raise AssertionError("__deepcopy__ must not be called")

    def __reduce__(self) -> Any:
        raise AssertionError("__reduce__ must not be called")


def _private_request(**kwargs: Any) -> Request:
    params: dict[str, Any] = {
        "url": "https://example.com/page",
        "headers": {
            "Authorization": "Basic c2VjcmV0",
            "Cookie": "sid=abc",
            "Proxy-Authorization": "Basic cHJveHk=",
            "X-Trace": "1",
        },
        "meta": {
            "api_token": "secret",
            "nested": {"values": [1, {"deep": 2}]},
            "keep": "value",
        },
        "cb_kwargs": {"session": "s", "shared": [1, 2]},
    }
    params.update(kwargs)
    return Request(**params)


SAME_ORIGIN_URLS = [
    "https://example.com/other",
    "https://EXAMPLE.COM/other",
    "https://example.com:443/other",
    "https://example.com./other",
    "https://user:password@example.com/other",
    "/other",
    "other",
    "?q=1",
]

CROSS_ORIGIN_URLS = [
    "http://example.com/other",
    "https://example.org/other",
    "https://sub.example.com/other",
    "https://example.com:8443/other",
    "https://example.com:80/other",
    "//example.org/other",
]


@pytest.mark.parametrize(
    ("url", "same"),
    [
        ("https://example.com/other", True),
        ("https://example.org/other", False),
    ],
    ids=["same-origin", "cross-origin"],
)
def test_every_attribute_is_copied(url: str, same: bool) -> None:
    def callback(response: Any) -> None:
        pass

    def errback(failure: Any) -> None:
        pass

    request = Request(
        "https://example.com/page",
        callback=callback,
        errback=errback,
        method="POST",
        headers={"Authorization": "Bearer x", "X-Trace": "1"},
        body=b"payload",
        cookies={"sid": "abc"},
        meta={"api_token": "secret", "keep": "value"},
        encoding="latin-1",
        priority=7,
        dont_filter=True,
        flags=["flag"],
        cb_kwargs={"session": "s", "keep": "value"},
        parent_id=42,
    )
    clone = request.copy_for_origin(
        url, private_meta=["api_token"], private_cb_kwargs=["session"]
    )

    assert clone.url == url
    assert clone.callback is callback
    assert clone.errback is errback
    assert clone.method == "POST"
    assert clone.body == b"payload"
    assert clone.encoding == "latin-1"
    assert clone.priority == 7
    assert clone.dont_filter is True
    assert clone.flags == ["flag"]
    assert clone.flags is not request.flags
    assert clone.id != request.id
    assert clone.parent_id == request.id

    expected_keys = {
        "meta",
        "cb_kwargs",
        "headers",
        "cookies",
        "url",
        "parent_id",
    }
    for attribute in Request.attributes:
        if attribute in expected_keys:
            continue
        assert getattr(clone, attribute) == getattr(request, attribute), attribute

    if same:
        assert clone.meta == request.meta
        assert clone.cb_kwargs == request.cb_kwargs
        assert dict(clone.headers) == dict(request.headers)
        assert clone.cookies == request.cookies
    else:
        assert clone.meta == {"keep": "value"}
        assert clone.cb_kwargs == {"keep": "value"}
        assert set(clone.headers) == {b"X-Trace"}
        assert clone.cookies == {}


@pytest.mark.parametrize("url", SAME_ORIGIN_URLS)
def test_same_origin_keeps_private_fields(url: str) -> None:
    request = _private_request()
    clone = request.copy_for_origin(
        url,
        private_meta=["api_token", "nested"],
        private_cb_kwargs=["session"],
    )
    assert clone.meta == request.meta
    assert clone.cb_kwargs == request.cb_kwargs
    assert clone.headers == request.headers
    assert clone.url.endswith("/other") or clone.url.endswith("?q=1")


@pytest.mark.parametrize("url", CROSS_ORIGIN_URLS)
def test_cross_origin_drops_private_fields(url: str) -> None:
    request = _private_request()
    clone = request.copy_for_origin(
        url,
        private_meta=["api_token", "nested"],
        private_cb_kwargs=["session"],
    )
    assert clone.meta == {"keep": "value"}
    assert clone.cb_kwargs == {"shared": [1, 2]}
    assert "api_token" not in clone.meta
    assert "nested" not in clone.meta
    assert "session" not in clone.cb_kwargs


@pytest.mark.parametrize(
    "header", [b"Authorization", b"Cookie", b"Proxy-Authorization"]
)
def test_cross_origin_drops_credentials(header: bytes) -> None:
    request = _private_request()
    clone = request.copy_for_origin("https://example.org/page")
    assert header not in clone.headers
    assert set(clone.headers) == {b"X-Trace"}


def test_same_origin_keeps_credentials() -> None:
    request = _private_request()
    clone = request.copy_for_origin("/other")
    assert clone.headers == request.headers
    assert clone.headers is not request.headers


def test_keep_credentials_is_separate_option() -> None:
    request = _private_request()
    clone = request.copy_for_origin(
        "https://example.org/page",
        private_meta=["api_token"],
        keep_credentials=True,
    )
    assert clone.headers == request.headers
    assert "api_token" not in clone.meta


@pytest.mark.parametrize("value", ["true", "false", "1", 1, 0, None, ["x"], object()])
def test_keep_credentials_requires_bool(value: Any) -> None:
    request = _private_request()
    with pytest.raises(TypeError, match="keep_credentials must be a bool"):
        request.copy_for_origin("https://example.org/page", keep_credentials=value)
    assert request.meta["api_token"] == "secret"
    assert b"Authorization" in request.headers


def test_private_names_require_iterable_of_str() -> None:
    request = _private_request()
    with pytest.raises(TypeError, match="private_meta must be an iterable"):
        request.copy_for_origin("https://example.org/page", private_meta="api_token")
    with pytest.raises(TypeError, match="private_meta field names must be str"):
        request.copy_for_origin(
            "https://example.org/page",
            private_meta=[b"x"],  # type: ignore[list-item]
        )
    with pytest.raises(TypeError, match="private_cb_kwargs field names must be str"):
        request.copy_for_origin(
            "https://example.org/page",
            private_cb_kwargs=[1],  # type: ignore[list-item]
        )
    with pytest.raises(TypeError, match="private_cb_kwargs must be an iterable"):
        request.copy_for_origin(
            "https://example.org/page",
            private_cb_kwargs=3,  # type: ignore[arg-type]
        )


def test_invalid_url_does_not_modify_original() -> None:
    request = _private_request()
    headers_before = copy.deepcopy(dict(request.headers.items()))
    meta_before = copy.deepcopy(request.meta)
    with pytest.raises(TypeError):
        request.copy_for_origin(123)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Port out of range"):
        request.copy_for_origin("https://example.com:70000/page")
    assert copy.deepcopy(dict(request.headers.items())) == headers_before
    assert request.meta == meta_before
    assert request.url == "https://example.com/page"


def test_invalid_container_types_raise() -> None:
    request = _private_request()
    with pytest.raises(TypeError, match="meta must be a mapping"):
        request.copy_for_origin("https://example.org/page", meta=["x", "y"])
    with pytest.raises(TypeError, match="cb_kwargs must be a mapping"):
        request.copy_for_origin("https://example.org/page", cb_kwargs="x")
    with pytest.raises(TypeError):
        request.copy_for_origin("https://example.org/page", headers=3)
    assert request.meta["api_token"] == "secret"
    assert b"Cookie" in request.headers


def test_url_must_be_str() -> None:
    with pytest.raises(TypeError, match="Request url must be str"):
        _private_request().copy_for_origin(None)  # type: ignore[arg-type]


def test_relative_target_resolution() -> None:
    request = _private_request(url="https://example.com/a/b/c?q=1")
    assert request.copy_for_origin("d").url == "https://example.com/a/b/d"
    assert request.copy_for_origin("../d").url == "https://example.com/a/d"
    assert request.copy_for_origin("/d").url == "https://example.com/d"
    assert request.copy_for_origin("//other.example/d").url == (
        "https://other.example/d"
    )
    assert request.copy_for_origin("?q=2").url == "https://example.com/a/b/c?q=2"


def test_relative_target_keeps_fragment() -> None:
    request = _private_request(url="https://example.com/a#frag")
    assert request.copy_for_origin("#other").url == "https://example.com/a#other"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("http://[::1]/a", "http://[0:0:0:0:0:0:0:1]/b"),
        ("http://[::1]:8080/a", "http://[0:0:0:0:0:0:0:1]:8080/b"),
        ("http://[::1]/a", "http://[::1]:80/b"),
        ("http://[::ffff:192.168.0.1]/a", "http://[0:0:0:0:0:ffff:c0a8:1]/b"),
    ],
)
def test_ipv6_same_origin(source: str, target: str) -> None:
    request = _private_request(url=source)
    clone = request.copy_for_origin(target, private_meta=["api_token"])
    assert "api_token" in clone.meta
    assert b"Cookie" in clone.headers
    assert clone.url == target


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("http://[::1]/a", "http://[::2]/b"),
        ("http://[::1]/a", "http://[::1]:8080/b"),
        ("http://[::1]/a", "https://[::1]/b"),
        ("http://[::1]/a", "http://127.0.0.1/b"),
    ],
)
def test_ipv6_cross_origin(source: str, target: str) -> None:
    request = _private_request(url=source)
    clone = request.copy_for_origin(target, private_meta=["api_token"])
    assert "api_token" not in clone.meta
    assert b"Cookie" not in clone.headers


@pytest.mark.parametrize(
    ("source", "target", "same"),
    [
        ("http://example.com/a", "http://example.com:80/b", True),
        ("https://example.com/a", "https://example.com:443/b", True),
        ("https://example.com/a", "https://example.com:8443/b", False),
        ("http://example.com/a", "http://example.com:0/b", False),
        ("http://example.com/a", "https://example.com/b", False),
        ("http://bücher.example/a", "http://xn--bcher-kva.example/b", True),
        ("http://BÜCHER.example/a", "http://bücher.example/b", True),
        ("http://example.com./a", "http://example.com/b", True),
        ("http://user:secret@example.com/a", "http://example.com/b", True),
        ("http://example.com/a", "http://user:secret@example.com/b", True),
        ("s3://bucket/a", "s3://bucket/b", False),
        ("about:blank", "about:blank", False),
    ],
)
def test_default_ports_idna_and_userinfo(source: str, target: str, same: bool) -> None:
    request = _private_request(url=source)
    clone = request.copy_for_origin(target, private_meta=["api_token"])
    assert ("api_token" in clone.meta) is same


def test_multiple_copies_are_independent() -> None:
    request = _private_request()
    first = request.copy_for_origin(
        "https://example.com/first",
        private_meta=["nested"],
        private_cb_kwargs=["shared"],
    )
    second = first.copy_for_origin(
        "https://example.org/second",
        private_meta=["nested"],
        private_cb_kwargs=["shared"],
    )
    third = request.copy_for_origin(
        "https://example.com/third",
        private_meta=["nested"],
        private_cb_kwargs=["shared"],
    )

    assert first.parent_id == request.id
    assert second.parent_id == first.id
    assert third.parent_id == request.id
    assert len({request.id, first.id, second.id, third.id}) == 4

    # The cross-origin copy in the middle does not affect the original or the
    # other copies.
    assert "nested" in first.meta
    assert "nested" not in second.meta
    assert "nested" in third.meta
    assert request.meta["nested"] == {"values": [1, {"deep": 2}]}
    assert b"Cookie" in first.headers
    assert b"Cookie" not in second.headers
    assert b"Cookie" in third.headers


def test_nested_containers_are_isolated() -> None:
    request = _private_request()
    clone = request.copy_for_origin("/other")

    assert clone.meta is not request.meta
    assert clone.meta["nested"] is not request.meta["nested"]
    assert clone.meta["nested"]["values"] is not request.meta["nested"]["values"]
    assert clone.meta["nested"]["values"][1] is not request.meta["nested"]["values"][1]
    assert clone.cb_kwargs["shared"] is not request.cb_kwargs["shared"]
    assert clone.headers is not request.headers
    assert clone.headers.getlist(b"X-Trace") is not request.headers.getlist(b"X-Trace")

    clone.meta["nested"]["values"][1]["deep"] = 99
    clone.cb_kwargs["shared"].append(3)
    clone.headers[b"X-Trace"] = "2"

    assert request.meta["nested"]["values"][1]["deep"] == 2
    assert request.cb_kwargs["shared"] == [1, 2]
    assert request.headers[b"X-Trace"] == b"1"


def test_nested_containers_tuple_and_set() -> None:
    request = _private_request(
        meta={
            "pair": ([1], {"a": [2]}),
            "items": {1, 2},
        }
    )
    clone = request.copy_for_origin("/other")
    assert clone.meta["pair"] is not request.meta["pair"]
    assert clone.meta["pair"][0] is not request.meta["pair"][0]
    assert clone.meta["pair"][1]["a"] is not request.meta["pair"][1]["a"]
    assert clone.meta["items"] == request.meta["items"]
    assert clone.meta["items"] is not request.meta["items"]


def test_nested_containers_do_not_run_user_methods() -> None:
    untouchable = _Untouchable()
    request = _private_request(
        meta={"token": "secret", "object": untouchable, "nested": [untouchable]},
        cb_kwargs={"object": untouchable},
    )
    clone = request.copy_for_origin("/other")
    assert clone.meta["object"] is untouchable
    assert clone.meta["nested"] == [untouchable]
    assert clone.cb_kwargs["object"] is untouchable
    assert untouchable.copies == 0


def test_nested_container_subclasses_are_shared() -> None:
    class _List(list[int]):
        pass

    value = _List([1, 2])
    request = _private_request(meta={"value": value})
    clone = request.copy_for_origin("/other")
    assert clone.meta["value"] is value


def test_nested_container_cycle_raises_before_copy() -> None:
    cycle: list[Any] = []
    cycle.append(cycle)
    request = _private_request(meta={"api_token": "secret", "cycle": cycle})
    headers_before = copy.deepcopy(dict(request.headers.items()))
    with pytest.raises(TypeError, match="reference cycle"):
        request.copy_for_origin("/other", private_meta=["api_token"])
    assert request.meta["api_token"] == "secret"
    assert request.meta["cycle"] is cycle
    assert copy.deepcopy(dict(request.headers.items())) == headers_before


def test_original_request_is_unchanged() -> None:
    request = _private_request()
    expected_headers = copy.deepcopy(dict(request.headers.items()))
    expected_meta = copy.deepcopy(request._meta)
    expected_cb_kwargs = copy.deepcopy(request.cb_kwargs)
    expected_fingerprint = fingerprint(request)
    expected_url = request.url
    expected_id = request.id

    request.copy_for_origin(
        "https://example.org/page",
        private_meta=["api_token", "nested"],
        private_cb_kwargs=["session"],
    )

    assert copy.deepcopy(dict(request.headers.items())) == expected_headers
    assert request._meta == expected_meta
    assert request.cb_kwargs == expected_cb_kwargs
    assert fingerprint(request) == expected_fingerprint
    assert request.url == expected_url
    assert request.id == expected_id


def test_fingerprint_of_copy_depends_on_url_only() -> None:
    request = _private_request()
    same = request.copy_for_origin("/other", private_meta=["api_token"])
    cross = request.copy_for_origin(
        "https://example.org/other", private_meta=["api_token"]
    )
    assert fingerprint(same) != fingerprint(cross)
    assert fingerprint(cross) != fingerprint(request)


def test_replace_semantics_unchanged() -> None:
    request = _private_request()
    clone = request.replace(url="https://example.org/page")
    assert clone.meta == request.meta
    assert clone.cb_kwargs == request.cb_kwargs
    assert clone.headers == request.headers


def test_custom_attributes_are_copied() -> None:
    class SubRequest(Request):
        attributes = (*Request.attributes, "extra")

        def __init__(self, *args: Any, extra: str | None = None, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.extra = extra

    request = SubRequest(
        "https://example.com/page",
        extra="value",
        meta={"api_token": "secret"},
    )
    clone = request.copy_for_origin(
        "https://example.org/page", private_meta=["api_token"]
    )
    assert isinstance(clone, SubRequest)
    assert clone.extra == "value"
    assert "api_token" not in clone.meta


def test_json_request_keeps_dumps_kwargs() -> None:
    request = JsonRequest("https://example.com/page", data={"a": 1})
    clone = request.copy_for_origin("https://example.org/page")
    assert isinstance(clone, JsonRequest)
    assert clone.body == request.body
    assert clone.headers["Content-Type"] == b"application/json"


def test_verbatim_url() -> None:
    request = Request(
        "HTTPS://Example.COM/page",
        meta={"verbatim_url": True, "api_token": "secret"},
        headers={"Cookie": "sid=abc"},
    )
    same = request.copy_for_origin("/other", private_meta=["api_token"])
    assert "api_token" in same.meta
    assert b"Cookie" in same.headers

    cross = request.copy_for_origin(
        "https://example.org/other", private_meta=["api_token"]
    )
    assert "api_token" not in cross.meta
    assert b"Cookie" not in cross.headers


def test_kwargs_override_attributes() -> None:
    request = _private_request()
    clone = request.copy_for_origin(
        "https://example.com/other",
        method="POST",
        body=b"data",
        meta={"other": 1},
        cb_kwargs={"other": 2},
        headers={"X-New": "1"},
    )
    assert clone.method == "POST"
    assert clone.body == b"data"
    assert clone.meta == {"other": 1}
    assert clone.cb_kwargs == {"other": 2}
    assert set(clone.headers) == {b"X-New"}
    assert clone.headers[b"X-New"] == b"1"


def test_explicit_containers_still_drop_credentials_cross_origin() -> None:
    request = _private_request()
    clone = request.copy_for_origin(
        "https://example.org/page",
        headers={"Authorization": "Bearer other", "X-New": "1"},
        cookies={"sid": "abc"},
    )
    assert set(clone.headers) == {b"X-New"}
    assert clone.cookies == {}


def test_no_headers_no_meta() -> None:
    request = Request("https://example.com/page")
    clone = request.copy_for_origin("https://example.org/page")
    assert clone.meta == {}
    assert clone.cb_kwargs == {}
    assert clone.headers == Headers()


def test_redirect_chain_contract() -> None:
    """A chain of cross-origin hops stays clean, and returning to the source
    origin does not restore what an earlier hop dropped."""
    request = _private_request()

    hop_one = request.copy_for_origin(
        "https://api.example.org/v1", private_meta=["api_token"]
    )
    assert set(hop_one.headers) == {b"X-Trace"}
    assert "api_token" not in hop_one.meta
    assert hop_one.cb_kwargs == request.cb_kwargs

    hop_two = hop_one.copy_for_origin(
        "https://api.example.org/v2", private_meta=["api_token"]
    )
    assert "api_token" not in hop_two.meta
    assert set(hop_two.headers) == {b"X-Trace"}

    back = hop_two.copy_for_origin("https://example.com/back")
    assert "api_token" not in back.meta
    assert b"Cookie" not in back.headers
    assert request.meta["api_token"] == "secret"
    assert b"Cookie" in request.headers


def test_cross_origin_drops_cookies_attribute() -> None:
    request = _private_request(cookies={"sid": "abc"})
    same = request.copy_for_origin("/other")
    assert same.cookies == {"sid": "abc"}
    assert same.cookies is not request.cookies
    cross = request.copy_for_origin("https://example.org/page")
    assert cross.cookies == {}
    assert request.cookies == {"sid": "abc"}


def test_keep_credentials_keeps_cookies_attribute() -> None:
    request = _private_request(cookies={"sid": "abc"})
    cross = request.copy_for_origin("https://example.org/page", keep_credentials=True)
    assert cross.cookies == {"sid": "abc"}


def test_none_private_names() -> None:
    request = _private_request()
    clone = request.copy_for_origin(
        "https://example.org/page", private_meta=(), private_cb_kwargs=()
    )
    assert clone.meta == request.meta
    assert clone.cb_kwargs == request.cb_kwargs
    assert b"Cookie" not in clone.headers
