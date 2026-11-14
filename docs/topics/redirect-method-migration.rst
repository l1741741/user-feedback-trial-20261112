.. _redirect-method-migration:

Redirect method transition policy
=================================

.. versionadded:: VERSION

This page describes how to migrate to the strict redirect method transition
policy, enabled through the :setting:`REDIRECT_STRICT_METHOD` setting of
:class:`~scrapy.downloadermiddlewares.redirect.RedirectMiddleware`.

Background
----------

The HTTP standard asks user agents to keep the method and the body of a
request when following 301 (Moved Permanently) and 302 (Found) responses.
Early web browsers instead turned ``POST`` requests into bodyless ``GET``
requests on those responses, and later standards kept that behavior for
compatibility. Scrapy follows the browser behavior by default.

The stricter, standard-compliant behavior has always been used for 307
(Temporary Redirect) and 308 (Permanent Redirect) responses, which were added
to the HTTP standard specifically to preserve the method and the body.

What changes
------------

Set :setting:`REDIRECT_STRICT_METHOD` to ``True`` to make 301 and 302
responses preserve the method and the body of the original request, the same
way 307 and 308 responses already do:

.. code-block:: python

    REDIRECT_STRICT_METHOD = True

The table below shows, for each status code, whether a ``POST`` request with
a body keeps its method and its body:

.. list-table::
   :header-rows: 1

   * - Status code
     - Default policy
     - Strict policy
   * - 301, 302
     - Followed with a bodyless ``GET`` request
     - Followed with the original ``POST`` request and its body
   * - 303
     - Followed with a bodyless ``GET`` request
     - Same as the default policy
   * - 307, 308
     - Original ``POST`` request and body kept
     - Same as the default policy
   * - Other codes in :setting:`REDIRECT_HTTP_CODES`
     - Followed like 303 responses
     - Same as the default policy

Custom methods, such as ``PUT``, ``DELETE`` or ``PATCH``, are not affected by
the setting: 301 and 302 responses already keep them under the default
policy. ``GET`` and ``HEAD`` requests keep their method under both policies.

Requests that become bodyless ``GET`` requests lose the headers that describe
the original request body, namely ``Content-Type``, ``Content-Length``,
``Content-Encoding``, ``Content-Language`` and
``Content-Location``. Redirects that keep the body keep those headers with it.

The policy does not change any other redirect behavior. The
:reqmeta:`dont_redirect` request meta key and the
``handle_httpstatus_list`` and ``handle_httpstatus_all`` keys still bypass the
middleware entirely, :setting:`REDIRECT_MAX_TIMES` still limits the number of
redirects (a chain that reaches the limit is still aborted with
:exc:`~scrapy.exceptions.IgnoreRequest`), and the ``Referer``, ``Cookie`` and
``Authorization`` header rules are applied in the same order as before.

Migrating
---------

1. Enable the setting and run your crawl against a target site you control,
   or inspect requests through :ref:`topics-debug`. A server that expects a
   ``GET`` request after a 301 or 302 response will now receive the original
   ``POST`` request, including its body, which is the behavior the setting is
   meant to produce.

2. If a specific redirect must keep the old behavior, do not enable the
   setting globally. Instead, handle that response in your spider through the
   ``handle_httpstatus_list`` spider attribute or :attr:`Request.meta
   <scrapy.Request.meta>` key, and issue the follow-up ``GET`` request
   yourself, for example with
   :meth:`Response.follow_redirect() <scrapy.http.Response.follow_redirect>`.

3. Spiders that rely on ``redirect_urls`` or ``redirect_reasons`` request
   metadata do not need changes: the metadata records the same URLs and
   status codes under both policies.
