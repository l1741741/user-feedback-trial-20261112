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

Extracting srcset candidates
----------------------------

An ``srcset`` attribute holds several image candidates separated by commas,
where each candidate is a URL optionally followed by a width (``480w``) or
pixel density (``2x``) descriptor. A plain split on commas is not enough:
data URLs contain commas of their own, so the extractor parses candidates with
the HTML tokenizing rules instead. Pass the attributes to treat as candidate
lists through *srcset_attrs*; every valid candidate becomes its own
:class:`~scrapy.link.Link`, its descriptor is left out of the URL and the
attribute name is available as the link's ``source_attr``.

.. code-block:: python

    extractor = LxmlLinkExtractor(
        tags="img",
        attrs=(),
        srcset_attrs="srcset",
        deny_extensions=(),
    )

By default an invalid candidate is skipped and the rest of the attribute is
still extracted. Set *srcset_on_error* to ``"reject"`` to ignore the whole
attribute instead. Parsing is bounded by *srcset_max_candidates* and
*srcset_max_chars*, so an oversized attribute cannot turn extraction into an
unbounded loop. Relative candidates are resolved against the response and its
``<base>`` tag, and the usual allow, deny, domain and scheme filters apply to
each candidate.

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
