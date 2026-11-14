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

.. _topics-jobs-frame-queues:

Frame disk queues
-----------------

The regular disk queue types
(``scrapy.squeues.PickleFifoDiskQueue``,
``scrapy.squeues.PickleLifoDiskQueue`` and their ``Marshal`` variants) store
each request as one length-prefixed record with no checksum. If a process is
killed while the next request is being written, that record can be left half
written; on resume, the queue may then fail to read that request and the
following ones, or report a different queue length than what is stored.

The frame disk queue types avoid this. Each request is stored as a frame that
starts with a fixed marker and version, carries the payload length and a
checksum, and ends with a commit marker. The frame is synced before it becomes
visible to the scheduler, so a request is either fully stored or not stored at
all. The available frame types are
``scrapy.squeues.PickleFifoFrameDiskQueue``,
``scrapy.squeues.PickleLifoFrameDiskQueue``,
``scrapy.squeues.MarshalFifoFrameDiskQueue`` and
``scrapy.squeues.MarshalLifoFrameDiskQueue``. They are opt-in through
:setting:`SCHEDULER_DISK_QUEUE` (and :setting:`SCHEDULER_START_DISK_QUEUE`
when separate start request queues are used); the default queue types keep
their original on-disk format.

Resuming a crawl
~~~~~~~~~~~~~~~~

When a frame queue is opened, it validates every frame it still owns:

- If the only unreadable data is a partially written frame at the very end of
  the storage, that frame was never committed and is discarded. The requests
  committed before it are all still available, in the same FIFO or LIFO order.

- If a frame in the middle of the storage is missing, truncated or has a bad
  checksum, opening the queue raises an error and leaves every file in the job
  directory untouched. The crawl does not skip past the damaged request, which
  could silently drop product pages; inspect and repair or restart the job from
  that job directory instead.

A job directory created by a regular disk queue is opened and migrated
automatically the first time a frame queue of the same order (FIFO or LIFO) is
pointed at it. Frame files carry the marker and version described above, so a
damaged frame file is reported as a frame error rather than misread as the
older length-prefixed format. A job must still be resumed with the same Scrapy
version, as described under :ref:`job-dir-scrapy-version-changes` below.

Rolling back a failed write
~~~~~~~~~~~~~~~~~~~~~~~~~~~

A write, flush or sync can fail because the disk is full or returns an error.
In a frame queue the bytes of the unfinished frame are truncated back to the
last committed frame before the error is propagated, so the failed request is
not counted in the queue length and cannot be popped. The next successful
push appends a complete frame after the intact data. Requests that could not
be serialized are still kept only in memory, exactly as with the regular
queues; they never reach the job directory and therefore cannot corrupt it.

Only one crawl process may open a given frame queue directory. An advisory lock
file is taken before any write and a second process opening the same directory
fails fast instead of appending interleaved frames. Closing a queue more than
once is harmless, and an empty queue removes its data and lock files.

Pause limitations
-----------------

Job pausing and resuming is only supported when the spider is paused by
stopping it cleanly. Forced, sudden or otherwise unclean shutdown can lead to
data corruption in the job directory, which may prevent the spider from
resuming correctly.

.. _job-dir-scrapy-version-changes:

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
    ``spider.state`` file.
