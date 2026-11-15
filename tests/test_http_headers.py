import copy
import logging
from collections.abc import Iterator

import pytest

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import Headers
from scrapy.http.headers import REDACTION_PLACEHOLDER


def test_basics() -> None:
    h = Headers({"Content-Type": "text/html", "Content-Length": 1234})
    assert h["Content-Type"]
    assert h["Content-Length"]

    with pytest.raises(KeyError):
        h["Accept"]
    assert h.get("Accept") is None
    assert h.getlist("Accept") == []

    assert h.get("Accept", "*/*") == b"*/*"
    assert h.getlist("Accept", "*/*") == [b"*/*"]
    assert h.getlist("Accept", ["text/html", "images/jpeg"]) == [
        b"text/html",
        b"images/jpeg",
    ]


def test_single_value() -> None:
    h = Headers()
    h["Content-Type"] = "text/html"
    assert h["Content-Type"] == b"text/html"
    assert h.get("Content-Type") == b"text/html"
    assert h.getlist("Content-Type") == [b"text/html"]


def test_multivalue() -> None:
    h = Headers()
    h["X-Forwarded-For"] = hlist = ["ip1", "ip2"]
    assert h["X-Forwarded-For"] == b"ip2"
    assert h.get("X-Forwarded-For") == b"ip2"
    assert h.getlist("X-Forwarded-For") == [b"ip1", b"ip2"]
    assert h.getlist("X-Forwarded-For") is not hlist  # type: ignore[comparison-overlap]


def test_multivalue_for_one_header() -> None:
    h = Headers((("a", "b"), ("a", "c")))
    assert h["a"] == b"c"
    assert h.get("a") == b"c"
    assert h.getlist("a") == [b"b", b"c"]


def test_encode_utf8() -> None:
    h = Headers({"key": "\xa3"}, encoding="utf-8")
    key, val = dict(h.items()).popitem()
    assert isinstance(key, bytes), key
    assert isinstance(val[0], bytes), val[0]
    assert val[0] == b"\xc2\xa3"


def test_encode_latin1() -> None:
    h = Headers({"key": "\xa3"}, encoding="latin1")
    _, val = dict(h.items()).popitem()
    assert val[0] == b"\xa3"


def test_encode_multiple() -> None:
    h = Headers({"key": ["\xa3"]}, encoding="utf-8")
    _, val = dict(h.items()).popitem()
    assert val[0] == b"\xc2\xa3"


def test_key_case_kept() -> None:
    h = Headers({"accept": "text/html", "access_token": "foo"})
    assert sorted(h.keys()) == [b"accept", b"access_token"]


def test_key_case_of_first_spelling_wins() -> None:
    h = Headers({"accept": "a", "Accept": "b"})
    assert h.getlist("ACCEPT") == [b"a", b"b"]
    assert list(h.keys()) == [b"accept"]

    h["ACCEPT"] = "c"
    h.appendlist("aCCept", "d")
    h.update({"ACCEPT": "e"})
    assert list(h.keys()) == [b"accept"]
    assert h.getlist("accept") == [b"e"]

    del h["ACCEPT"]
    h["ACCEPT"] = "f"
    assert list(h.keys()) == [b"ACCEPT"]


def test_delete_and_contains() -> None:
    h = Headers()
    h["Content-Type"] = "text/html"
    assert "Content-Type" in h
    del h["Content-Type"]
    assert "Content-Type" not in h


def test_pop() -> None:
    h = Headers({"Content-Type": "text/html", "X-Forwarded-For": ["ip1", "ip2"]})
    olist = h.getlist("X-Forwarded-For")
    assert h.pop("x-forwarded-for") is olist
    assert olist == [b"ip1", b"ip2"]
    assert list(h.keys()) == [b"Content-Type"]
    assert h.pop("X-Forwarded-For", None) is None
    with pytest.raises(KeyError):
        h.pop("X-Forwarded-For")


def test_setdefault() -> None:
    h = Headers()
    hlist = ["ip1", "ip2"]
    olist = h.setdefault("X-Forwarded-For", hlist)
    assert h.getlist("X-Forwarded-For") is not hlist  # type: ignore[comparison-overlap]
    assert h.getlist("X-Forwarded-For") is olist

    h = Headers()
    olist = h.setdefault("X-Forwarded-For", "ip1")
    assert h.getlist("X-Forwarded-For") == [b"ip1"]
    assert h.getlist("X-Forwarded-For") is olist


def test_has_key() -> None:
    h = Headers({"Content-Type": "text/html"})
    with pytest.warns(ScrapyDeprecationWarning, match="has_key"):
        assert h.has_key("content-type")


def test_fromkeys() -> None:
    with pytest.warns(ScrapyDeprecationWarning, match="fromkeys"):
        h = Headers.fromkeys(("Content-Type", "Content-Length"), "value")
    assert h.getlist("Content-Type") == [b"value"]
    assert h.getlist("Content-Length") == [b"value"]


def test_iterables() -> None:
    idict = {"Content-Type": "text/html", "X-Forwarded-For": ["ip1", "ip2"]}

    h = Headers(idict)
    assert dict(h.items()) == {
        b"Content-Type": [b"text/html"],
        b"X-Forwarded-For": [b"ip1", b"ip2"],
    }
    assert sorted(h.keys()) == [b"Content-Type", b"X-Forwarded-For"]
    assert sorted(h.items()) == [
        (b"Content-Type", [b"text/html"]),
        (b"X-Forwarded-For", [b"ip1", b"ip2"]),
    ]
    assert set(h.values()) == {b"ip2", b"text/html"}


def test_update() -> None:
    h = Headers()
    h.update({"Content-Type": "text/html", "X-Forwarded-For": ["ip1", "ip2"]})
    assert h.getlist("Content-Type") == [b"text/html"]
    assert h.getlist("X-Forwarded-For") == [b"ip1", b"ip2"]


def test_copy() -> None:
    h1 = Headers({"header1": ["value1", "value2"]})
    h2 = copy.copy(h1)
    assert h1 == h2
    assert h1.getlist("header1") == h2.getlist("header1")
    assert h1.getlist("header1") is not h2.getlist("header1")
    assert isinstance(h2, Headers)


def test_appendlist() -> None:
    h1 = Headers({"header1": "value1"})
    h1.appendlist("header1", "value3")
    assert h1.getlist("header1") == [b"value1", b"value3"]

    h1 = Headers()
    h1.appendlist("header1", "value1")
    h1.appendlist("header1", "value3")
    assert h1.getlist("header1") == [b"value1", b"value3"]


def test_setlist() -> None:
    h1 = Headers({"header1": "value1"})
    assert h1.getlist("header1") == [b"value1"]
    with pytest.warns(ScrapyDeprecationWarning, match="setlist"):
        h1.setlist("header1", [b"value2", b"value3"])
    assert h1.getlist("header1") == [b"value2", b"value3"]


def test_setlistdefault() -> None:
    h1 = Headers({"header1": "value1"})
    with pytest.warns(ScrapyDeprecationWarning, match="setlistdefault"):
        h1.setlistdefault("header1", ["value2", "value3"])
    with pytest.warns(ScrapyDeprecationWarning, match="setlistdefault"):
        h1.setlistdefault("header2", ["value2", "value3"])
    assert h1.getlist("header1") == [b"value1"]
    assert h1.getlist("header2") == [b"value2", b"value3"]


def test_none_value() -> None:
    h1 = Headers()
    h1["foo"] = "bar"
    h1["foo"] = None
    h1.setdefault("foo", "bar")
    assert h1["foo"] is None
    assert h1.get("foo") is None
    assert h1.get("foo", "baz") == b"baz"
    assert h1.getlist("foo") == []


def test_int_value() -> None:
    h1 = Headers({"hey": 5})
    h1["foo"] = 1
    h1.setdefault("bar", 2)
    h1["buz"] = [1, "dos", 3]
    assert h1.getlist("foo") == [b"1"]
    assert h1.getlist("bar") == [b"2"]
    assert h1.getlist("buz") == [b"1", b"dos", b"3"]
    assert h1.getlist("hey") == [b"5"]


def test_invalid_value() -> None:
    with pytest.raises(TypeError, match="Unsupported value type"):
        Headers({"foo": object()})
    with pytest.raises(TypeError, match="Unsupported value type"):
        Headers()["foo"] = object()
    with pytest.raises(TypeError, match="Unsupported value type"):
        Headers().setdefault("foo", object())
    with pytest.raises(TypeError, match="Unsupported value type"):
        Headers()["foo"] = [object()]


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([], b""),
        ([("X-Removed", None)], b""),
        (
            [
                ("Content-Type", "text/html"),
                ("set-cookie", "a=1"),
                ("X-Removed", None),
                ("Set-Cookie", "b=2"),
            ],
            b"Content-Type: text/html\r\nset-cookie: a=1\r\nset-cookie: b=2",
        ),
    ],
)
def test_to_string(headers: list[tuple[str, str | None]], expected: bytes) -> None:
    assert Headers(headers).to_string() == expected


def test_to_unicode_dict_undecodable() -> None:
    h = Headers({b"Public-Key-Pins": b'pin-sha256=\x94"a"', b"X-\xff": b"ok"})
    with pytest.warns(ScrapyDeprecationWarning, match="to_unicode_dict"):
        unicode_dict = h.to_unicode_dict()
    assert unicode_dict == {
        "Public-Key-Pins": 'pin-sha256=�"a"',
        "X-�": "ok",
    }


def test_to_tuple_list_undecodable() -> None:
    h = Headers({b"Public-Key-Pins": b'pin-sha256=\x94"a"', b"X-\xff": b"ok"})
    assert h.to_tuple_list() == [
        ("Public-Key-Pins", 'pin-sha256=�"a"'),
        ("X-�", "ok"),
    ]


SECRET = b"super-secret-token"


def make_real_headers() -> Headers:
    # Mirrors headers an RPA downloader might capture from a real exchange.
    return Headers(
        {
            "Host": "tickets.example",
            "Authorization": f"Bearer {SECRET.decode()}",
            "proxy-authorization": "Basic dXNlcjpwYXNz",
            "X-Api-Key": "key-12345",
            "Content-Type": "application/json",
            "Accept": ["text/html", "application/json"],
            "Cookie": (
                "sessionid=topsecret; sid=abc; theme=dark; token=e=quals=embedded"
            ),
            "Set-Cookie": [
                "sessionid=topsecret; Path=/; HttpOnly",
                "theme=dark; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT",
                "sid=abc",
            ],
            "X-Empty": None,
        }
    )


def test_redacted_copy_masks_default_fields_case_insensitively() -> None:
    h = make_real_headers()
    copy_ = h.redacted_copy()
    assert copy_.getlist("Authorization") == [REDACTION_PLACEHOLDER]
    assert copy_.getlist("PROXY-AUTHORIZATION") == [REDACTION_PLACEHOLDER]
    # Non-sensitive fields are untouched.
    assert copy_.getlist("Content-Type") == [b"application/json"]
    assert copy_.getlist("Accept") == [b"text/html", b"application/json"]
    assert copy_.get("Host") == b"tickets.example"


def test_redacted_copy_masks_caller_fields_case_insensitively() -> None:
    h = make_real_headers()
    copy_ = h.redacted_copy(fields=["x-api-key", b"X-API-KEY"])
    assert copy_.getlist("X-Api-Key") == [REDACTION_PLACEHOLDER]
    assert "key-12345" not in str(copy_.to_string())


@pytest.mark.parametrize(
    "field",
    ["Authorization", "authorization", b"Proxy-Authorization", b"AUTHORIZATION"],
)
def test_redacted_copy_default_field_name_variants(field: str | bytes) -> None:
    h = Headers([(field, SECRET)])
    assert h.redacted_copy().getlist(field) == [REDACTION_PLACEHOLDER]


def test_redacted_copy_does_not_modify_source() -> None:
    h = make_real_headers()
    before = dict(h.items())
    h.redacted_copy(fields=["X-Api-Key"], allowed_cookies=["theme"])
    assert dict(h.items()) == before
    assert h.get("Authorization") == b"Bearer " + SECRET


def test_redacted_copy_does_not_share_value_lists() -> None:
    h = make_real_headers()
    copy_ = h.redacted_copy()
    for key in h:
        assert h.getlist(key) is not copy_.getlist(key)
    # Mutating the copy cannot reach the source and vice versa.
    copy_.appendlist("Content-Type", "mutated")
    assert h.getlist("Content-Type") == [b"application/json"]
    h.appendlist("Accept", "mutated")
    assert copy_.getlist("Accept") == [b"text/html", b"application/json"]


def test_redacted_copy_keeps_allowed_cookies_only() -> None:
    h = make_real_headers()
    copy_ = h.redacted_copy(allowed_cookies=["theme", "sid"])
    cookie = copy_.get("Cookie")
    assert cookie is not None
    assert b"sessionid=" + REDACTION_PLACEHOLDER in cookie
    assert b"sid=abc" in cookie
    assert b"theme=dark" in cookie
    assert b"token=" + REDACTION_PLACEHOLDER in cookie
    assert SECRET not in cookie
    assert b"topsecret" not in cookie


def test_redacted_copy_cookie_name_is_case_sensitive() -> None:
    h = Headers({"Cookie": "sid=keep; SID=mask; sidprefix=hidden"})
    copy_ = h.redacted_copy(allowed_cookies=["sid"])
    cookie = copy_.get("Cookie")
    assert cookie == (
        b"sid=keep; SID="
        + REDACTION_PLACEHOLDER
        + b"; sidprefix="
        + REDACTION_PLACEHOLDER
    )


def test_redacted_copy_does_not_match_cookie_name_prefixes() -> None:
    h = Headers({"Cookie": "sid=1; sidxxx=2; xsidx=3"})
    copy_ = h.redacted_copy(allowed_cookies=["sid"])
    cookie = copy_.get("Cookie")
    assert cookie == (
        b"sid=1; sidxxx=" + REDACTION_PLACEHOLDER + b"; xsidx=" + REDACTION_PLACEHOLDER
    )


def test_redacted_copy_keeps_embedded_equals_in_cookie_value() -> None:
    h = Headers({"Cookie": "token=a=b=c; other=x"})
    copy_ = h.redacted_copy(allowed_cookies=["token"])
    assert copy_.get("Cookie") == b"token=a=b=c; other=" + REDACTION_PLACEHOLDER


def test_redacted_copy_cookie_whitelist_keeps_non_ascii_values() -> None:
    h = Headers({"Cookie": b"sid=caf\xc3\xa9; other=x"})
    copy_ = h.redacted_copy(allowed_cookies=[b"sid"])
    # Non-ASCII bytes in a whitelisted cookie's value are preserved verbatim.
    assert copy_.get("Cookie") == b"sid=caf\xc3\xa9; other=" + REDACTION_PLACEHOLDER


def test_redacted_copy_non_ascii_cookie_name_is_fully_masked() -> None:
    # Cookie names are RFC 9110 tokens, so non-ASCII bytes make the whole
    # Cookie header unparseable and it is masked without leaking the bytes.
    h = Headers({"Cookie": b"caf\xc3\xa9=latte; sid=x"})
    copy_ = h.redacted_copy(allowed_cookies=[b"caf\xc3\xa9", "sid"])
    assert copy_.getlist("Cookie") == [REDACTION_PLACEHOLDER]
    assert b"latte" not in copy_.to_string()


def test_redacted_copy_unparseable_cookie_is_fully_masked() -> None:
    # No "=" in a cookie pair.
    h = Headers({"Cookie": b"broken; sid=keep"})
    copy_ = h.redacted_copy(allowed_cookies=["sid"])
    assert copy_.getlist("Cookie") == [REDACTION_PLACEHOLDER]
    assert b"broken" not in copy_.to_string()

    # Empty cookie name.
    h = Headers({"Cookie": b"=noname; sid=keep"})
    copy_ = h.redacted_copy(allowed_cookies=["sid"])
    assert copy_.getlist("Cookie") == [REDACTION_PLACEHOLDER]
    assert b"noname" not in copy_.to_string()

    # Cookie pairs must be separated by "; " exactly; a lone ";" cannot be
    # split into token names, so the whole value is masked conservatively.
    h = Headers({"Cookie": b"sid=keep;other=hide"})
    copy_ = h.redacted_copy(allowed_cookies=["sid"])
    assert copy_.getlist("Cookie") == [REDACTION_PLACEHOLDER]
    assert b"hide" not in copy_.to_string()


def test_redacted_copy_cookie_smuggling_through_whitelisted_name() -> None:
    # A missing space would make "session" part of the whitelisted sid
    # cookie's value; the whole header must be masked instead.
    h = Headers({"Cookie": b"sid=keep;session=stolen"})
    copy_ = h.redacted_copy(allowed_cookies=["sid"])
    assert copy_.getlist("Cookie") == [REDACTION_PLACEHOLDER]
    assert b"stolen" not in copy_.to_string()
    assert b"keep" not in copy_.to_string()


def test_redacted_copy_double_semicolon_cookie_is_fully_masked() -> None:
    h = Headers({"Cookie": b"sid=1;; other=2"})
    copy_ = h.redacted_copy(allowed_cookies=["sid", "other"])
    assert copy_.getlist("Cookie") == [REDACTION_PLACEHOLDER]


def test_redacted_copy_set_cookie_keeps_multi_value_boundaries() -> None:
    h = make_real_headers()
    copy_ = h.redacted_copy(allowed_cookies=["theme", "sid"])
    values = copy_.getlist("Set-Cookie")
    # One entry per original Set-Cookie line; Expires commas never split one.
    assert values == [
        REDACTION_PLACEHOLDER,
        b"theme=dark; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT",
        b"sid=abc",
    ]
    assert b"topsecret" not in b"".join(values)


def test_redacted_copy_set_cookie_unparseable_is_fully_masked() -> None:
    h = Headers(
        {
            "Set-Cookie": [
                b"good=1",
                b"no-equals-sign",
                b"=empty-name; Path=/",
                b"bad name=2",
            ]
        }
    )
    copy_ = h.redacted_copy(allowed_cookies=["good"])
    assert copy_.getlist("Set-Cookie") == [
        b"good=1",
        REDACTION_PLACEHOLDER,
        REDACTION_PLACEHOLDER,
        REDACTION_PLACEHOLDER,
    ]
    assert b"no-equals-sign" not in copy_.to_string()
    assert b"empty-name" not in copy_.to_string()


def test_redacted_copy_empty_values_and_empty_lists() -> None:
    h = Headers({"X-Empty": None, "Cookie": None, "Set-Cookie": []})
    copy_ = h.redacted_copy()
    # A header set to None is kept as an empty value list in the copy.
    assert copy_.getlist("X-Empty") == []
    assert copy_.get("X-Empty") is None
    assert b"X-Empty" in dict(copy_.items())
    assert copy_.getlist("Cookie") == []
    assert copy_.getlist("Set-Cookie") == []
    # Empty lists carry no values, so nothing of them reaches the wire form.
    assert copy_.to_string() == b""
    assert h.redacted_copy(strategy="remove").getlist("X-Empty") == []


def test_redacted_copy_repeated_fields_keep_placeholder_count() -> None:
    h = Headers({"Authorization": ["one", "two", "three"]})
    assert h.redacted_copy().getlist("Authorization") == [
        REDACTION_PLACEHOLDER,
        REDACTION_PLACEHOLDER,
        REDACTION_PLACEHOLDER,
    ]


def test_redacted_copy_is_idempotent() -> None:
    h = make_real_headers()
    once = h.redacted_copy(fields=["X-Api-Key"], allowed_cookies=["theme", "sid"])
    twice = once.redacted_copy(fields=["X-Api-Key"], allowed_cookies=["theme", "sid"])
    assert dict(once.items()) == dict(twice.items())
    # Also idempotent under a different whitelist and strategy.
    thrice = twice.redacted_copy(allowed_cookies=[], strategy="remove")
    again = thrice.redacted_copy(allowed_cookies=[], strategy="remove")
    assert dict(thrice.items()) == dict(again.items())


def test_redacted_copy_remove_strategy_drops_sensitive_data() -> None:
    h = make_real_headers()
    copy_ = h.redacted_copy(
        fields=["X-Api-Key"],
        allowed_cookies=["theme"],
        strategy="remove",
    )
    assert "Authorization" not in copy_
    assert "Proxy-Authorization" not in copy_
    assert "X-Api-Key" not in copy_
    cookie = copy_.get("Cookie")
    assert cookie == b"theme=dark"
    assert copy_.getlist("Set-Cookie") == [
        b"theme=dark; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT",
    ]
    assert b"topsecret" not in copy_.to_string()


def test_redacted_copy_remove_strategy_drops_unparseable_cookies() -> None:
    h = Headers({"Cookie": b"broken; sid=1", "Set-Cookie": [b"good=1", b"junk"]})
    copy_ = h.redacted_copy(allowed_cookies=["good", "sid"], strategy="remove")
    assert "Cookie" not in copy_
    assert copy_.getlist("Set-Cookie") == [b"good=1"]


def test_redacted_copy_unknown_strategy_raises() -> None:
    h = make_real_headers()
    with pytest.raises(ValueError, match="strategy"):
        h.redacted_copy(strategy="delete")  # type: ignore[arg-type]
    assert dict(h.items()) == dict(make_real_headers().items())


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"fields": ["X-Header\r"]}, "CR or LF"),
        ({"fields": [b"X-Header\nInjected: 1"]}, "CR or LF"),
        ({"allowed_cookies": ["sid\r"]}, "CR or LF"),
        ({"fields": [""]}, "empty"),
        ({"allowed_cookies": [b""]}, "empty"),
        ({"fields": [123]}, "str or bytes"),
        ({"allowed_cookies": [object()]}, "str or bytes"),
    ],
)
def test_redacted_copy_validates_parameters(
    kwargs: dict[str, object], match: str
) -> None:
    h = make_real_headers()
    with pytest.raises((ValueError, TypeError), match=match) as exc_info:
        h.redacted_copy(**kwargs)  # type: ignore[arg-type]
    # Error text never embeds header values or parameter contents.
    assert SECRET not in str(exc_info.value).encode()
    assert b"Injected" not in str(exc_info.value).encode()
    assert dict(h.items()) == dict(make_real_headers().items())


def test_redacted_copy_consumes_failing_iterators_without_partial_result() -> None:
    h = make_real_headers()

    def exploding() -> Iterator[str]:
        yield "X-Api-Key"
        raise RuntimeError("iterator exploded")

    with pytest.raises(RuntimeError, match="exploded"):
        h.redacted_copy(fields=exploding())
    # The source is intact and nothing redacted was returned or stored.
    assert dict(h.items()) == dict(make_real_headers().items())


def test_redacted_copy_bytes_and_non_ascii_values_pass_through() -> None:
    h = Headers(
        [
            (b"X-Binary", b"\x00\x01\xff raw"),
            ("X-Utf8", "café"),
            ("Cookie", b"raw=\xff\xfe; ok=1"),
        ]
    )
    copy_ = h.redacted_copy(allowed_cookies=["ok"])
    # Non-sensitive field values, including arbitrary bytes, are copied as is.
    assert copy_.getlist("X-Binary") == [b"\x00\x01\xff raw"]
    assert copy_.getlist("X-Utf8") == ["café".encode()]
    # The non-ASCII bytes live in a non-whitelisted cookie value and never
    # leak: the value is masked while the ASCII cookie name is retained.
    cookie = copy_.get("Cookie")
    assert cookie == b"raw=" + REDACTION_PLACEHOLDER + b"; ok=1"
    assert b"\xff\xfe" not in cookie


def test_redacted_copy_explicit_cookie_field_overrides_whitelist() -> None:
    h = Headers({"Cookie": "sid=keep; other=hide"})
    copy_ = h.redacted_copy(fields=["cookie"], allowed_cookies=["sid"])
    assert copy_.getlist("Cookie") == [REDACTION_PLACEHOLDER]
    assert b"keep" not in copy_.to_string()


def test_redacted_copy_multiple_cookie_values_are_independent() -> None:
    h = Headers({"Cookie": ["sid=1; bad=2", b"no-equals", "theme=dark"]})
    copy_ = h.redacted_copy(allowed_cookies=["sid", "theme"])
    # Each Cookie header value is parsed on its own; the unparseable one is
    # masked wholesale without affecting the parseable values.
    assert copy_.getlist("Cookie") == [
        b"sid=1; bad=" + REDACTION_PLACEHOLDER,
        REDACTION_PLACEHOLDER,
        b"theme=dark",
    ]


def test_redacted_copy_on_empty_headers() -> None:
    copy_ = Headers().redacted_copy()
    assert isinstance(copy_, Headers)
    assert len(copy_) == 0


def test_redacted_copy_preserves_subclass_and_encoding() -> None:
    class MyHeaders(Headers):
        pass

    copy_ = MyHeaders(encoding="latin1").redacted_copy()
    assert isinstance(copy_, MyHeaders)
    assert copy_.encoding == "latin1"


def test_redacted_copy_validates_strategy_before_iterating_parameters() -> None:
    class Exploding:
        def __iter__(self) -> Iterator[str]:
            raise AssertionError("parameters must not be consumed")

    with pytest.raises(ValueError, match="strategy"):
        Headers().redacted_copy(fields=Exploding(), strategy="nope")  # type: ignore[arg-type]


def test_redacted_copy_output_is_safe_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    h = make_real_headers()
    caplog.set_level(logging.DEBUG)
    safe = h.redacted_copy(fields=["X-Api-Key"], allowed_cookies=["theme", "sid"])
    logger = logging.getLogger("test.headers.redaction")
    logger.debug("captured headers: %s", safe.to_string())
    logged = caplog.text
    assert SECRET.decode() not in logged
    assert "topsecret" not in logged
    assert "dXNlcjpwYXNz" not in logged
    assert "key-12345" not in logged
    assert "e=quals=embedded" not in logged
    assert REDACTION_PLACEHOLDER.decode() in logged
    assert "theme=dark" in logged
