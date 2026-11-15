.. _topics-link-extractors:

===============
Link Extractors
===============

A link extractor is an object that extracts links from responses.

The ``__init__`` method of
:class:`~scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor` takes settings that
determine which links may be extracted. :class:`LxmlLinkExtractor.extract_links
<scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor.extract_links>` returns a
list of matching :class:`~scrapy.link.Link` objects from a
:class:`~scrapy.http.Response` object.

Link extractors are used in :class:`~scrapy.spiders.CrawlSpider` spiders
through a set of :class:`~scrapy.spiders.Rule` objects.

You can also use link extractors in regular spiders. For example, you can
instantiate :class:`LinkExtractor
<scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor>` as a class variable in your
spider and use it from your spider callbacks:

.. code-block:: python

    def parse(self, response):
        for link in self.link_extractor.extract_links(response):
            yield Request(link.url, callback=self.parse)

.. _link-extractor-budgets:

Extraction budgets
==================

Some pages contain a very large number of links, or links with extremely
long URLs, and processing every extracted candidate at once can use a lot
of memory. The *max_candidates* and *max_url_bytes* arguments of
:class:`~scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor` bound that
work.

These budgets constrain the processing of extraction results, not the
parsing of the HTML: the response is parsed as usual, and the budgets
only limit how many of the extracted candidates are processed and
returned.

*max_candidates* caps how many candidate links are taken from the
underlying parser during a single
:meth:`~scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor.extract_links`
call. Every candidate counts as soon as the parser produces it, before
*allow*, *deny* and the other filtering arguments are applied and before
deduplication and canonicalization, so candidates that are later
filtered out still count. Once the budget is spent, extraction stops and
the links gathered so far are returned in their original order.

*max_url_bytes* drops any link whose final absolute URL is longer than
the given number of UTF-8 bytes, and extraction continues with the
remaining links. The URL is measured after it has been made absolute and
percent-encoded, so a non-ASCII character counts as its percent-encoded
bytes, not as a single character.

Each
:meth:`~scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor.extract_links`
call has its own budget. The following example runs offline:

.. code-block:: python

    from scrapy.http import HtmlResponse
    from scrapy.linkextractors import LinkExtractor

    html = """
    <html><body>
        <a href="/a.html">a</a>
        <a href="/b.html">b</a>
        <a href="/c.html">c</a>
    </body></html>
    """
    response = HtmlResponse("http://example.com", body=html.encode())
    extractor = LinkExtractor(max_candidates=2)
    for link in extractor.extract_links(response):
        print(link.url)

It prints::

    http://example.com/a.html
    http://example.com/b.html

.. _topics-link-extractors-ref:

Link extractor reference
========================

The link extractor class is
:class:`scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor`. For convenience it
can also be imported as ``scrapy.linkextractors.LinkExtractor``:

.. code-block:: python

    from scrapy.linkextractors import LinkExtractor

.. autodata:: scrapy.linkextractors.IGNORED_EXTENSIONS

.. autodata:: scrapy.linkextractors.SUPPORTED_SCHEMES

LxmlLinkExtractor
-----------------

.. autoclass:: scrapy.linkextractors.lxmlhtml.LxmlLinkExtractor

    .. automethod:: extract_links

Link
----

.. autoclass:: scrapy.link.Link
