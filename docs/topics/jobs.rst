.. _topics-jobs:

=================================
Jobs: pausing and resuming crawls
=================================

Sometimes, for big sites, it's desirable to pause crawls and be able to resume
them later.

Scrapy supports this functionality out of the box through the following
facilities:

* a scheduler that persists scheduled requests on disk

* a duplicates filter that persists visited requests on disk

* an extension that keeps some spider state (key/value pairs) persistent
  between batches

.. _job-dir:

Job directory
=============

To enable persistence support, define a *job directory* through the
:setting:`JOBDIR` setting.

The job directory will store all required data to keep the state of a *single*
job (i.e. a spider run), so that if stopped cleanly, it can be resumed later.

.. warning:: This directory must *not* be shared by different spiders, or even
    different jobs of the same spider. See :ref:`job-dir-spider-name`.

.. warning:: Treat the job directory with the same security care as your
    Scrapy project source code. Do not point ``JOBDIR`` to a path that
    untrusted parties can write to.

See also :ref:`job-dir-contents`.

How to use it
=============

To start a spider with persistence support enabled, run it like this::

    scrapy crawl somespider -s JOBDIR=crawls/somespider-1

Then you can stop the spider safely at any time (by pressing Ctrl-C or sending
a signal), and resume it later by issuing the same command::

    scrapy crawl somespider -s JOBDIR=crawls/somespider-1

.. _job-dir-spider-name:

Deriving the job directory from the spider name
===============================================

One way to keep job directories apart is to build the path in
:meth:`~scrapy.Spider.update_settings`, so that a group of spiders shares the
same path except for the spider name:

.. code-block:: python

    from pathlib import Path

    from scrapy import Spider


    class BaseSpider(Spider):
        @classmethod
        def update_settings(cls, settings):
            super().update_settings(settings)
            settings.set("JOBDIR", str(Path("crawls", cls.name)), priority="spider")


.. _topics-keeping-persistent-state-between-batches:

Keeping persistent state between batches
========================================

Sometimes you'll want to keep persistent spider state between pause/resume
batches. Use the ``spider.state`` attribute for that. It should be a dict.
There's :ref:`a built-in extension <topics-extensions-ref-spiderstate>` that
takes care of serializing, storing and loading that attribute from the job
directory when the spider starts and stops.

Here's an example of a callback that uses the spider state (other spider code
is omitted for brevity):

.. code-block:: python

    def parse_item(self, response):
        # parse item here
        self.state["items_count"] = self.state.get("items_count", 0) + 1

Persistence gotchas
===================

There are a few things to keep in mind if you want to use Scrapy's persistence
support:

Pause limitations
-----------------

Job pausing and resuming is only supported when the spider is paused by
stopping it cleanly. Forced, sudden or otherwise unclean shutdown can lead to
data corruption in the job directory, which may prevent the spider from
resuming correctly.

.. _job-dir-state-writes:

Spider state writes
-------------------

The :ref:`spider state extension <topics-extensions-ref-spiderstate>` writes
the new state to a temporary file in the job directory, flushes it to disk,
and then atomically replaces the ``spider.state`` file with it.

This gives the state file the following guarantees:

-   If serialization, the write, the flush, or the replacement fails, the
    ``spider.state`` file is never left truncated or partially written: it
    keeps either its previous contents or, if the replacement had already
    completed, the new contents. The extension cleans up its temporary file
    and re-raises the original exception.
-   The temporary file name is unique to the instance that creates it, so
    several crawls that share a job directory use different temporary files.
-   If a temporary file cannot be removed after a failure, a warning names it
    so that it can be deleted by hand. It is never loaded.
-   The new file gets the permissions that the current process's umask gives
    to any other new file, and its contents come only from the serialized
    ``spider.state`` value.

That guarantee ends at the replacement. After the replacement completes, the
job directory holds only the new state: the extension keeps no copy of the
previous state in memory, in a backup file, or anywhere else.

The extension also syncs the job directory after the replacement, so that the
replacement survives a power loss. Some platforms do not allow syncing a
directory. There, the replacement is still atomic for any process that keeps
running, and a power loss right after it may leave either version of the file.

The serialization format is unchanged: the file holds a ``pickle`` stream with
protocol 4, and an existing ``spider.state`` file is loaded the same way as
before.

Diagnosing disk failures
------------------------

Failures are reported in two ways:

-   Failures before the replacement raise an exception. The crawl logs it when
    it closes the spider, and the state file keeps its previous contents.
-   Failures after the replacement, or while removing a temporary file, cannot
    undo the replacement. They are logged at ``WARNING`` level by the
    ``scrapy.extensions.spiderstate`` logger and do not stop the crawl.

An unsupported directory sync is expected on some platforms, so it is logged
at ``DEBUG`` level. Enable ``DEBUG`` logging for that logger to see it.

To find out what state a job will resume from, inspect the ``spider.state``
file: it holds the state of the last crawl that saved successfully. Leftover
``.spider.state.*.tmp`` files only mean that the extension could not remove
one of its temporary files; they are safe to delete.

Scrapy version changes
----------------------

The contents of a job directory are an implementation detail of the Scrapy
version that wrote them. A job must be resumed with the same Scrapy version
that paused it; after upgrading or downgrading Scrapy, start a new job with a
new job directory.

Feed exports
------------

When a job is resumed, the :ref:`feeds <topics-feed-exports>` of the previous
run already exist, so their :ref:`mode <feed-mode>` determines what happens to
the items of the resumed run: ``"append"`` adds them to those feeds,
``"overwrite"`` replaces the items of the previous run with them, and
``"create"`` does not write them anywhere.

Cookie expiration
-----------------

Cookies may expire, so if you don't resume your spider quickly, the scheduled
requests may no longer work. This won't be an issue if your spider doesn't rely
on cookies.

.. _request-serialization:

Request serialization
---------------------

For persistence to work, :class:`~scrapy.Request` objects must be
serializable with :mod:`pickle`, except for the :ref:`callback
<callbacks>` and :ref:`errback
<errbacks>` values passed to their ``__init__``
method, which must be methods of the running :class:`~scrapy.Spider` class.

Requests that cannot be serialized are kept in memory only: they are still
sent, but they are lost when the crawl is paused.

If you wish to log the requests that couldn't be serialized, you can set the
:setting:`SCHEDULER_DEBUG` setting to ``True`` in the project's settings page.
It is ``False`` by default.

.. note:: Because requests are serialized with :mod:`pickle`, the objects you
    store on a request, such as the values of its
    :attr:`~scrapy.Request.cb_kwargs` and :attr:`~scrapy.Request.meta`
    dictionaries, are deep-copied when the request is written to and later read
    back from the job directory. As a result, the callback receives a *copy* of
    those objects rather than the original ones, and changes made to the copy are
    not reflected in the original object. Keep this in mind if you rely on
    sharing mutable state through ``cb_kwargs`` or ``meta``.

.. _job-dir-contents:

Job directory contents
======================

The contents of a job directory depend on the components used during the job.
Components known to write in the job directory include the :ref:`scheduler
<topics-scheduler>` and the :class:`~scrapy.extensions.spiderstate.SpiderState`
extension. See the reference documentation of the corresponding components for
details.

For example, with default settings, the job directory may look like this:

.. code-block:: none

    ├── requests.queue
    |   ├── active.json
    |   └── {hostname}-{hash}
    |       └── {priority}{s?}
    |           ├── q{00000}
    |           └── info.json
    ├── requests.seen
    └── spider.state

Where:

-   :class:`~scrapy.core.scheduler.Scheduler` creates the ``requests.queue/``
    directory and the ``active.json`` file, the latter containing the state
    data of :class:`~scrapy.pqueues.DownloaderAwarePriorityQueue`, written
    when the job stops and, with :setting:`JOBDIR_SYNC_EVERY`, as it runs.

-   :class:`~scrapy.pqueues.DownloaderAwarePriorityQueue` creates the
    ``{hostname}-{hash}`` directories.

-   :class:`~scrapy.pqueues.ScrapyPriorityQueue` creates the ``{priority}{s?}``
    directories.

-   :class:`scrapy.squeues.PickleFifoDiskQueue`, a subclass of
    :class:`queuelib.FifoDiskQueue` that uses :mod:`pickle` to serialize
    :class:`dict` representations of :class:`scrapy.Request` objects, creates
    the ``info.json`` and ``q{00000}`` files.

-   :class:`~scrapy.dupefilters.RFPDupeFilter` creates the ``requests.seen``
    file.

-   :class:`~scrapy.extensions.spiderstate.SpiderState` creates the
    ``spider.state`` file, and may leave a ``.spider.state.{random}.tmp`` file
    behind if it cannot remove it (see :ref:`job-dir-state-writes`).
