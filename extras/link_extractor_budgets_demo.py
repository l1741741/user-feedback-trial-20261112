#!/usr/bin/env python
"""Offline demo of the LxmlLinkExtractor extraction budgets.

Builds a response from a literal HTML string (no network access) and
shows how max_candidates and max_url_bytes bound the processing of
extraction results. The HTML is parsed as usual in every case; the
budgets only limit how many of the extracted candidates are processed
and returned.

Run with:

    python extras/link_extractor_budgets_demo.py
"""

from scrapy.http import HtmlResponse
from scrapy.linkextractors import LinkExtractor

HTML = """
<html><body>
    <a href="/a.html">a</a>
    <a href="/b.html">b</a>
    <a href="/日本語.html">multibyte</a>
    <a href="/c.html">c</a>
    <a href="/d.html">d</a>
</body></html>
"""


def show(title, extractor, response):
    print(
        f"{title} (max_candidates={extractor.max_candidates}, "
        f"max_url_bytes={extractor.max_url_bytes})"
    )
    for link in extractor.extract_links(response):
        print(f"    {link.url!r} ({len(link.url.encode())} bytes)")


def main():
    response = HtmlResponse(
        "http://example.com", body=HTML.encode(), encoding="utf-8"
    )
    show("No budgets", LinkExtractor(), response)
    show("Only the first 3 candidates", LinkExtractor(max_candidates=3), response)
    show("URLs up to 30 bytes", LinkExtractor(max_url_bytes=30), response)


if __name__ == "__main__":
    main()
