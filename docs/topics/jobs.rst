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
    file, or, with :setting:`DUPEFILTER_JOURNAL`, the
    ``requests.seen.journal`` file (see :ref:`job-dir-dupefilter-journal`).

-   :class:`~scrapy.extensions.spiderstate.SpiderState` creates the
    ``spider.state`` file.

.. _job-dir-dupefilter-journal:

Recoverable dupefilter journal
==============================

By default, :class:`~scrapy.dupefilters.RFPDupeFilter` stores the
fingerprints of seen requests in the ``requests.seen`` file, a sequence of
bare fingerprints with no checksums or commit markers. If the process exits
in the middle of a write, the file can be left with a truncated tail that is
silently dropped on the next run, and there is no way to tell a truncated
tail apart from actual data corruption.

Setting :setting:`DUPEFILTER_JOURNAL` to ``True`` switches
:class:`~scrapy.dupefilters.RFPDupeFilter` to a recoverable journal file,
``requests.seen.journal``, with the following layout:

-   An 8-byte **versioned header**: the 6-byte magic ``b"SRFPJ\x00"``
    followed by a big-endian, 2-byte format version (currently ``1``).

-   A sequence of **records**, one per fingerprint: a big-endian, 2-byte
    fingerprint length, the fingerprint itself, and a 4-byte CRC-32 checksum
    of the length and fingerprint bytes.

The **commit boundary** is the offset right after the last complete record
with a valid checksum. A record becomes committed once it has been fully
written, flushed and fsynced; only then is the fingerprint added to the
in-memory set, so the in-memory set is always a subset of the committed
on-disk records.

On startup, only complete, checksum-valid records are recovered:

-   An **uncommitted or truncated tail** (an incomplete record, or a record
    with a bad checksum followed by no valid record, small enough to be a
    single record) is *quarantined*: its bytes are appended to
    ``requests.seen.journal.orphan`` for forensics, and the journal is
    truncated to the commit boundary. Requests whose fingerprints were only
    in the tail are simply scheduled again.

-   **Mid-file corruption** (an invalid record with a valid record after it,
    or an unparseable tail larger than any single record could be) raises
    :class:`~scrapy.dupefilters.JournalCorruptionError`, which carries the
    file ``path`` and the byte ``offset`` of the first invalid record. The
    original file is left unmodified — corrupt data is never silently
    skipped.

-   A file that is not a journal at all, or uses an unsupported format
    version, raises :class:`~scrapy.dupefilters.JournalFormatError` and is
    left unmodified. An empty journal file is treated as a fresh journal
    (a previous run crashed before writing the header) and re-initialized.

If appending a record fails (write, flush or fsync error),
:meth:`~scrapy.dupefilters.RFPDupeFilter.request_seen` raises
:class:`~scrapy.dupefilters.JournalWriteError` and the fingerprint is *not*
added to the in-memory set, keeping memory and disk consistent: the request
is scheduled again later, which retries the append. The failed append is
rolled back to the commit boundary on a best-effort basis; if the rollback
itself fails, the torn tail is cleaned before the next append or quarantined
on the next open. Closing the filter is idempotent, and a failed close never
truncates the existing journal.

Writing the same job directory from multiple processes at the same time is
not supported.

.. _job-dir-dupefilter-journal-migration:

Migrating from the legacy format
--------------------------------

Journal mode never converts an existing ``requests.seen`` file implicitly:
if one is found when journal mode is enabled,
:class:`~scrapy.dupefilters.JournalFormatError` is raised. To convert
explicitly, keeping the original file as ``requests.seen.legacy.bak``::

    from scrapy.dupefilters import RFPDupeFilter

    RFPDupeFilter.migrate_legacy_journal("/path/to/job/dir")

The migration refuses to overwrite an existing journal or backup file. The
legacy ``requests.seen`` format remains the default and is fully supported;
it is only read or written when :setting:`DUPEFILTER_JOURNAL` is ``False``.
