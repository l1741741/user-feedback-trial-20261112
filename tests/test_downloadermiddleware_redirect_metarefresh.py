from __future__ import annotations

import logging
from itertools import chain
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from scrapy.downloadermiddlewares.redirect import (
    _STRICT_PREFIX_SIZE,
    MetaRefreshMiddleware,
)
from scrapy.exceptions import NotConfigured, ScrapyDeprecationWarning
from scrapy.http import HtmlResponse, Request, Response
from scrapy.spiders import Spider
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.bases.redirect import TestRedirectBase
from tests.utils.redirect import (
    HTTP_SCHEMES,
    NON_HTTP_SCHEMES,
    REDIRECT_SCHEME_CASES,
    SCHEME_PARAMS,
)

if TYPE_CHECKING:
    from scrapy.settings import BaseSettings


def meta_refresh_body(url: str, interval: int = 5) -> bytes:
    html = f"""<html><head><meta http-equiv="refresh" content="{interval};url={url}"/></head></html>"""
    return html.encode()


class TestMetaRefreshMiddleware(TestRedirectBase):
    mwcls = MetaRefreshMiddleware
    reason = "meta refresh"

    def setup_method(self):
        crawler = get_crawler(Spider)
        self.mw = build_from_crawler(self.mwcls, crawler)

    def _body(
        self, interval: int = 5, url: str = "http://example.org/newpage"
    ) -> bytes:
        return meta_refresh_body(url, interval)

    def get_response(
        self, request: Request, location: str, status: int = 302
    ) -> Response:
        return HtmlResponse(request.url, body=self._body(url=location))

    def test_meta_refresh(self):
        req = Request(url="http://example.org")
        rsp = HtmlResponse(req.url, body=self._body())
        req2 = self.mw.process_response(req, rsp)
        assert isinstance(req2, Request)
        assert req2.url == "http://example.org/newpage"

    def test_meta_refresh_with_high_interval(self):
        # meta-refresh with high intervals don't trigger redirects
        req = Request(url="http://example.org")
        rsp = HtmlResponse(
            url="http://example.org", body=self._body(interval=1000), encoding="utf-8"
        )
        rsp2 = self.mw.process_response(req, rsp)
        assert rsp is rsp2

    def test_meta_refresh_trough_posted_request(self):
        req = Request(
            url="http://example.org",
            method="POST",
            body="test",
            headers={"Content-Type": "text/plain", "Content-length": "4"},
        )
        rsp = HtmlResponse(req.url, body=self._body())
        req2 = self.mw.process_response(req, rsp)

        assert isinstance(req2, Request)
        assert req2.url == "http://example.org/newpage"
        assert req2.method == "GET"
        assert "Content-Type" not in req2.headers, (
            "Content-Type header must not be present in redirected request"
        )
        assert "Content-Length" not in req2.headers, (
            "Content-Length header must not be present in redirected request"
        )
        assert not req2.body, f"Redirected body must be empty, not {req2.body!r}"

    def test_ignore_tags_default(self):
        req = Request(url="http://example.org")
        body = (
            """<noscript><meta http-equiv="refresh" """
            """content="0;URL='http://example.org/newpage'"></noscript>"""
        )
        rsp = HtmlResponse(req.url, body=body.encode())
        response = self.mw.process_response(req, rsp)
        assert isinstance(response, Response)

    def test_scripting_false(self):
        crawler = get_crawler(Spider, {"METAREFRESH_SCRIPTING": False})
        mw = build_from_crawler(MetaRefreshMiddleware, crawler)
        req = Request(url="http://example.org")
        body = b"<noscript>" + self._body(interval=0) + b"</noscript>"
        req2 = mw.process_response(req, HtmlResponse(req.url, body=body))
        assert isinstance(req2, Request)
        assert req2.url == "http://example.org/newpage"

    @pytest.mark.parametrize("content", ["0", "0;url=#x", "0;url=http://EXAMPLE.org/"])
    def test_refresh_self(self, content: str) -> None:
        req = Request(url="http://example.org")
        body = f'<meta http-equiv="refresh" content="{content}">'.encode()
        rsp = HtmlResponse(req.url, body=body)
        assert self.mw.process_response(req, rsp) is rsp

    def test_maxdelay_shortest(self):
        req = Request(url="http://example.org")
        body = self._body(interval=1000, url="/slow") + self._body(
            interval=1, url="/fast"
        )
        req2 = self.mw.process_response(req, HtmlResponse(req.url, body=body))
        assert isinstance(req2, Request)
        assert req2.url == "http://example.org/fast"


class TestMetaRefreshMiddlewareStrict(TestRedirectBase):
    mwcls = MetaRefreshMiddleware
    reason = "meta refresh"

    def setup_method(self):
        crawler = get_crawler(Spider, {"METAREFRESH_STRICT": True})
        self.mw = build_from_crawler(self.mwcls, crawler)

    def get_response(
        self, request: Request, location: str, status: int = 302
    ) -> Response:
        return HtmlResponse(request.url, body=meta_refresh_body(location))

    def _process(
        self,
        body: bytes = b"",
        *,
        url: str = "http://example.org",
        headers: dict[str, str] | None = None,
    ) -> tuple[Request, HtmlResponse, Request | Response]:
        req = Request(url)
        rsp = HtmlResponse(url, body=body, headers=headers)
        return req, rsp, self.mw.process_response(req, rsp)

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("0;url=http://example.org/a", "http://example.org/a"),
            # Semicolons inside a URL, quoted or not, are URL data.
            ("0; url='http://example.org/a;b'", "http://example.org/a;b"),
            ("0; URL=&quot;http://example.org/a;b&quot;", "http://example.org/a;b"),
            ("0; url=http://example.org/a;b", "http://example.org/a;b"),
            # The url= prefix is optional and case-insensitive.
            ("5;http://example.org/a", "http://example.org/a"),
            (" 0 ; Url = http://example.org/a ", "http://example.org/a"),
            ("0.5;url=http://example.org/a", "http://example.org/a"),
            (".5;url=http://example.org/a", "http://example.org/a"),
            ("5.;url=http://example.org/a", "http://example.org/a"),
        ],
    )
    def test_content_parsing(self, content: str, expected: str) -> None:
        body = f'<meta http-equiv="refresh" content="{content}">'.encode()
        _, _, result = self._process(body)
        assert isinstance(result, Request)
        assert result.url == expected

    @pytest.mark.parametrize(
        "content",
        [
            "abc;url=http://example.org/a",  # non-numeric delay
            "-1;url=http://example.org/a",  # negative delay
            "1e2;url=http://example.org/a",  # exponent notation
            "0 http://example.org/a",  # missing semicolon
            "0;url='http://example.org/a",  # unclosed quote
            "0;url='http://example.org/a' x",  # junk after the closing quote
            f"{'9' * 400};url=http://example.org/a",  # non-finite delay
        ],
    )
    def test_malformed_content(self, content: str) -> None:
        body = f'<meta http-equiv="refresh" content="{content}">'.encode()
        _, rsp, result = self._process(body)
        assert result is rsp

    def test_case_insensitive_markup(self) -> None:
        body = b'<META HTTP-EQUIV="Refresh" CONTENT="0;URL=http://example.org/a">'
        _, _, result = self._process(body)
        assert isinstance(result, Request)
        assert result.url == "http://example.org/a"

    @pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
    def test_bom(self, encoding: str) -> None:
        html = '\ufeff<meta http-equiv="refresh" content="0;url=http://example.org/a">'
        _, _, result = self._process(html.encode(encoding))
        assert isinstance(result, Request)
        assert result.url == "http://example.org/a"

    def test_first_valid_tag_wins(self) -> None:
        body = (
            b'<meta http-equiv="refresh" content="0;url=http://example.org/first">'
            b'<meta http-equiv="refresh" content="0;url=http://example.org/second">'
        )
        _, _, result = self._process(body)
        assert isinstance(result, Request)
        assert result.url == "http://example.org/first"

    def test_malformed_tag_skipped(self) -> None:
        body = (
            b'<meta http-equiv="refresh" content="not-a-refresh">'
            b'<meta http-equiv="refresh" content="0;url=http://example.org/second">'
        )
        _, _, result = self._process(body)
        assert isinstance(result, Request)
        assert result.url == "http://example.org/second"

    def test_tag_beyond_prefix_ignored(self) -> None:
        body = b" " * _STRICT_PREFIX_SIZE + meta_refresh_body("http://example.org/a", 0)
        _, rsp, result = self._process(body)
        assert result is rsp

    def test_tag_cut_by_prefix_ignored(self) -> None:
        tag = b'<meta http-equiv="refresh" content="0;url=http://example.org/a">'
        body = b" " * (_STRICT_PREFIX_SIZE - 10) + tag
        _, rsp, result = self._process(body)
        assert result is rsp

    def test_refresh_header(self) -> None:
        _, _, result = self._process(headers={"Refresh": "0; url=http://example.org/a"})
        assert isinstance(result, Request)
        assert result.url == "http://example.org/a"

    def test_refresh_header_precedence(self) -> None:
        _, _, result = self._process(
            meta_refresh_body("http://example.org/tag", 0),
            headers={"Refresh": "0; url=http://example.org/header"},
        )
        assert isinstance(result, Request)
        assert result.url == "http://example.org/header"

    def test_malformed_refresh_header_falls_back_to_tag(self) -> None:
        _, _, result = self._process(
            meta_refresh_body("http://example.org/tag", 0),
            headers={"Refresh": "not-a-refresh"},
        )
        assert isinstance(result, Request)
        assert result.url == "http://example.org/tag"

    def test_refresh_header_empty_url_wins_over_tag(self) -> None:
        _, rsp, result = self._process(
            meta_refresh_body("http://example.org/tag", 0),
            headers={"Refresh": "5"},
        )
        assert result is rsp

    def test_refresh_header_relative_url_ignores_base_tag(self) -> None:
        _, _, result = self._process(
            b'<base href="http://other.example/sub/">',
            headers={"Refresh": "0; url=/header"},
        )
        assert isinstance(result, Request)
        assert result.url == "http://example.org/header"

    @pytest.mark.parametrize(
        ("base", "target", "expected"),
        [
            ("http://example.org/sub/", "page", "http://example.org/sub/page"),
            ("/sub/", "page", "http://example.org/sub/page"),
            ("http://example.org/sub/", "?q=1", "http://example.org/sub/?q=1"),
        ],
    )
    def test_relative_url_resolved_against_base(
        self, base: str, target: str, expected: str
    ) -> None:
        body = (
            f'<base href="{base}"><meta http-equiv="refresh" content="0;url={target}">'
        ).encode()
        _, _, result = self._process(body)
        assert isinstance(result, Request)
        assert result.url == expected

    @pytest.mark.parametrize("content", ["5", "5;url=", "5;url=''"])
    def test_empty_url(self, content: str) -> None:
        body = f'<meta http-equiv="refresh" content="{content}">'.encode()
        _, rsp, result = self._process(body)
        assert result is rsp

    @pytest.mark.parametrize(
        "content",
        ["0;url=http://example.org", "0;url=/", "0;url=http://EXAMPLE.org/"],
    )
    def test_self_url(self, content: str) -> None:
        body = f'<meta http-equiv="refresh" content="{content}">'.encode()
        _, rsp, result = self._process(body)
        assert result is rsp

    @pytest.mark.parametrize(
        "target",
        [
            "javascript:alert(1)",
            "ftp://example.org/a",
            "data:text/html,x",
            "https://",
            "http://[::1",
        ],
    )
    def test_unsupported_scheme(self, target: str) -> None:
        body = f'<meta http-equiv="refresh" content="0;url={target}">'.encode()
        _, rsp, result = self._process(body)
        assert result is rsp

    @pytest.mark.parametrize("interval", [100, 1000])
    def test_delay_not_below_maxdelay(self, interval: int) -> None:
        _, rsp, result = self._process(
            meta_refresh_body("http://example.org/a", interval)
        )
        assert result is rsp

    @pytest.mark.parametrize(
        "body",
        [
            b'<!-- <meta http-equiv="refresh" content="0;url=http://example.org/a"> -->',
            (
                b'<script>var s = \'<meta http-equiv="refresh" '
                b'content="0;url=http://example.org/a">\';</script>'
            ),
            (
                b'<template><meta http-equiv="refresh" '
                b'content="0;url=http://example.org/a"></template>'
            ),
            (
                b'<template><noscript><meta http-equiv="refresh" '
                b'content="0;url=http://example.org/a"></noscript></template>'
            ),
            (
                b'<noscript><meta http-equiv="refresh" '
                b'content="0;url=http://example.org/a"></noscript>'
            ),
            b'<p>meta http-equiv="refresh" content="0;url=http://example.org/a"</p>',
            # Meta tags without http-equiv="refresh" or without content are
            # not refresh tags.
            b'<meta content="0;url=http://example.org/a">',
            b'<meta http-equiv="refresh">',
            b'<meta http-equiv="content-type" content="0;url=http://example.org/a">',
        ],
    )
    def test_ignored_contexts(self, body: bytes) -> None:
        _, rsp, result = self._process(body)
        assert result is rsp

    @pytest.mark.parametrize(
        "base",
        [
            "<base>",
            '<base href="">',
            '<base href="http://[::1">',
        ],
    )
    def test_invalid_base_ignored(self, base: str) -> None:
        body = (f'{base}<meta http-equiv="refresh" content="0;url=/a">').encode()
        _, _, result = self._process(body)
        assert isinstance(result, Request)
        assert result.url == "http://example.org/a"

    def test_first_base_wins(self) -> None:
        body = (
            b'<base href="http://example.org/first/">'
            b'<base href="http://example.org/second/">'
            b'<meta http-equiv="refresh" content="0;url=a">'
        )
        _, _, result = self._process(body)
        assert isinstance(result, Request)
        assert result.url == "http://example.org/first/a"

    def test_noscript_scripting_disabled(self) -> None:
        crawler = get_crawler(
            Spider, {"METAREFRESH_STRICT": True, "METAREFRESH_SCRIPTING": False}
        )
        mw = build_from_crawler(MetaRefreshMiddleware, crawler)
        req = Request("http://example.org")
        body = (
            b'<noscript><meta http-equiv="refresh" '
            b'content="0;url=http://example.org/a"></noscript>'
        )
        result = mw.process_response(req, HtmlResponse(req.url, body=body))
        assert isinstance(result, Request)
        assert result.url == "http://example.org/a"

    def test_repeated_processing_does_not_stack_redirect_times(self) -> None:
        req = Request("http://example.org")
        rsp = HtmlResponse(req.url, body=meta_refresh_body("http://example.org/a", 0))
        first = self.mw.process_response(req, rsp)
        second = self.mw.process_response(req, rsp)
        assert isinstance(first, Request)
        assert isinstance(second, Request)
        assert first.meta["redirect_times"] == 1
        assert second.meta["redirect_times"] == 1

    def test_original_response_preserved(self) -> None:
        req = Request("http://example.org")
        rsp = HtmlResponse(req.url, body=b"<p>no refresh</p>", headers={"X-Test": "1"})
        body, headers = rsp.body, rsp.headers
        result = self.mw.process_response(req, rsp)
        assert result is rsp
        assert rsp.body is body
        assert rsp.headers is headers
        assert "redirect_times" not in req.meta

    def test_requests_do_not_share_parse_results(self) -> None:
        req1 = Request("http://example.org/1")
        rsp1 = HtmlResponse(req1.url, body=meta_refresh_body("http://example.org/a", 0))
        req2 = Request("http://example.org/2")
        rsp2 = HtmlResponse(req2.url, body=meta_refresh_body("http://example.org/b", 0))
        result1 = self.mw.process_response(req1, rsp1)
        result2 = self.mw.process_response(req2, rsp2)
        assert isinstance(result1, Request)
        assert isinstance(result2, Request)
        assert result1.url == "http://example.org/a"
        assert result2.url == "http://example.org/b"


def test_refresh_header_ignored_without_strict() -> None:
    crawler = get_crawler(Spider)
    mw = build_from_crawler(MetaRefreshMiddleware, crawler)
    req = Request("http://example.org")
    rsp = HtmlResponse(
        req.url, body=b"", headers={"Refresh": "0; url=http://example.org/a"}
    )
    assert mw.process_response(req, rsp) is rsp


NOSCRIPT_REFRESH = (
    b"<noscript>" + meta_refresh_body("http://example.org/newpage", 0) + b"</noscript>"
)


def _process_noscript_refresh(mw: MetaRefreshMiddleware) -> Request | Response:
    req = Request(url="http://example.org")
    return mw.process_response(req, HtmlResponse(req.url, body=NOSCRIPT_REFRESH))


@pytest.mark.parametrize(
    ("ignore_tags", "scripting"),
    [
        (["script", "noscript"], True),
        (["noscript", "template"], True),
        (["script"], False),
        ([], False),
    ],
)
def test_ignore_tags(ignore_tags: list[str], scripting: bool) -> None:
    crawler = get_crawler(Spider, {"METAREFRESH_IGNORE_TAGS": ignore_tags})
    with pytest.warns(
        ScrapyDeprecationWarning, match=f"METAREFRESH_SCRIPTING = {scripting}"
    ):
        mw = build_from_crawler(MetaRefreshMiddleware, crawler)
    assert isinstance(_process_noscript_refresh(mw), Request) is not scripting


class _RemoveNoscriptAddon:
    def update_settings(self, settings: BaseSettings) -> None:
        settings.remove_from_list("METAREFRESH_IGNORE_TAGS", "noscript")


def test_ignore_tags_removed_from_list() -> None:
    crawler = get_crawler(Spider, {"ADDONS": {_RemoveNoscriptAddon: 0}})
    with pytest.warns(ScrapyDeprecationWarning, match="METAREFRESH_SCRIPTING = False"):
        mw = build_from_crawler(MetaRefreshMiddleware, crawler)
    assert isinstance(_process_noscript_refresh(mw), Request)


def test_ignore_tags_unsupported() -> None:
    crawler = get_crawler(Spider, {"METAREFRESH_IGNORE_TAGS": ["noscript", "div"]})
    with pytest.raises(ValueError, match="METAREFRESH_SCRIPTING"):
        build_from_crawler(MetaRefreshMiddleware, crawler)


class _IgnoreNoTagsAddon:
    def update_settings(self, settings: BaseSettings) -> None:
        settings.set("METAREFRESH_IGNORE_TAGS", [], priority="addon")


def test_ignore_tags_overridden() -> None:
    crawler = get_crawler(
        Spider,
        {"ADDONS": {_IgnoreNoTagsAddon: 0}, "METAREFRESH_SCRIPTING": True},
    )
    mw = build_from_crawler(MetaRefreshMiddleware, crawler)
    assert isinstance(_process_noscript_refresh(mw), Response)


@pytest.mark.parametrize(
    SCHEME_PARAMS,
    [
        *REDIRECT_SCHEME_CASES,
        # data/file/ftp/s3/foo → * does not redirect
        *(
            (
                f"{input_scheme}://example.com/a",
                f"{output_scheme}://example.com/b",
                None,
            )
            for input_scheme in NON_HTTP_SCHEMES
            for output_scheme in chain(HTTP_SCHEMES, NON_HTTP_SCHEMES)
        ),
        # data/file/ftp/s3/foo → relative does not redirect
        *(
            (
                f"{scheme}://example.com/a",
                location,
                None,
            )
            for scheme in NON_HTTP_SCHEMES
            for location in ("//example.com/b", "/b")
        ),
    ],
)
def test_meta_refresh_schemes(url, location, target):
    crawler = get_crawler(Spider)
    mw = build_from_crawler(MetaRefreshMiddleware, crawler)
    request = Request(url)
    response = HtmlResponse(url, body=meta_refresh_body(location))
    redirect = mw.process_response(request, response)
    if target is None:
        assert redirect == response
    else:
        assert isinstance(redirect, Request)


def test_warning_meta_refresh_middleware(caplog):
    crawler = get_crawler()
    crawler.get_spider_middleware = MagicMock(  # type: ignore[method-assign]
        return_value=None
    )
    mw = build_from_crawler(MetaRefreshMiddleware, crawler)
    with caplog.at_level(logging.WARNING):
        mw._spider_opened()
    assert (
        "scrapy.downloadermiddlewares.redirect.MetaRefreshMiddleware found no "
        "scrapy.spidermiddlewares.referer.RefererMiddleware"
    ) in caplog.text
    assert (
        "enable scrapy.spidermiddlewares.referer.RefererMiddleware (or a subclass)"
        in caplog.text
    )
    assert (
        "replace scrapy.downloadermiddlewares.redirect.MetaRefreshMiddleware "
        "with a subclass that overrides the handle_referer() method"
    ) in caplog.text


def test_not_configured():
    crawler = get_crawler(Spider, {"METAREFRESH_ENABLED": False})
    with pytest.raises(NotConfigured):
        build_from_crawler(MetaRefreshMiddleware, crawler)
