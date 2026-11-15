from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

import scrapy.dupefilters
from scrapy.core.scheduler import Scheduler
from scrapy.dupefilters import (
    _JOURNAL_HEADER,
    BaseDupeFilter,
    DiskDupeFilter,
    JournalCorruptionError,
    JournalFormatError,
    JournalWriteError,
    RFPDupeFilter,
    _journal_record,
)
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import Request
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.spiders import SimpleSpider

if TYPE_CHECKING:
    from scrapy.crawler import Crawler


def _get_dupefilter(
    *,
    crawler: Crawler | None = None,
    settings: dict[str, Any] | None = None,
    open_: bool = True,
) -> BaseDupeFilter:
    if crawler is None:
        crawler = get_crawler(settings_dict=settings)
    scheduler = build_from_crawler(Scheduler, crawler)
    dupefilter = scheduler.df
    if open_:
        dupefilter.open()
    return dupefilter


class FromCrawlerRFPDupeFilter(RFPDupeFilter):
    method: str

    @classmethod
    def from_crawler(cls, crawler):
        df = super().from_crawler(crawler)
        df.method = "from_crawler"
        return df


class DirectDupeFilter:
    method = "n/a"


class DupeFilterTestMixin:
    """Tests that every :setting:`DUPEFILTER_CLASS` based on request
    fingerprints must pass, regardless of where it stores them."""

    dupefilter_class: type[BaseDupeFilter]

    def _get_dupefilter(self, **settings: Any) -> BaseDupeFilter:
        return _get_dupefilter(
            settings={"DUPEFILTER_CLASS": self.dupefilter_class, **settings}
        )

    def test_filter(self) -> None:
        dupefilter = self._get_dupefilter()
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        r3 = Request("https://example.com/2")

        assert not dupefilter.request_seen(r1)
        assert dupefilter.request_seen(r1)

        assert not dupefilter.request_seen(r2)
        assert dupefilter.request_seen(r3)

        dupefilter.close("finished")

    def test_jobdir(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")

        dupefilter = self._get_dupefilter(JOBDIR=str(tmp_path))
        assert not dupefilter.request_seen(r1)
        dupefilter.close("finished")

        resumed = self._get_dupefilter(JOBDIR=str(tmp_path))
        assert resumed.request_seen(r1)
        assert not resumed.request_seen(r2)
        resumed.close("finished")

    def test_fingerprinter(self) -> None:
        r1 = Request("https://example.com/index.html")
        r2 = Request("https://example.com/INDEX.html")

        dupefilter = self._get_dupefilter()
        assert not dupefilter.request_seen(r1)
        assert not dupefilter.request_seen(r2)
        dupefilter.close("finished")

        class CaseInsensitiveRequestFingerprinter:
            def fingerprint(self, request: Request) -> bytes:
                return hashlib.sha1(request.url.lower().encode()).digest()

        case_insensitive_dupefilter = self._get_dupefilter(
            REQUEST_FINGERPRINTER_CLASS=CaseInsensitiveRequestFingerprinter
        )
        assert not case_insensitive_dupefilter.request_seen(r1)
        assert case_insensitive_dupefilter.request_seen(r2)
        case_insensitive_dupefilter.close("finished")


class TestRFPDupeFilter(DupeFilterTestMixin):
    dupefilter_class = RFPDupeFilter

    def test_df_from_crawler_scheduler(self):
        settings = {
            "DUPEFILTER_DEBUG": True,
            "DUPEFILTER_CLASS": FromCrawlerRFPDupeFilter,
        }
        crawler = get_crawler(settings_dict=settings)
        scheduler = build_from_crawler(Scheduler, crawler)
        dupefilter = cast("FromCrawlerRFPDupeFilter", scheduler.df)
        assert dupefilter.debug
        assert dupefilter.method == "from_crawler"

    def test_df_direct_scheduler(self):
        settings = {
            "DUPEFILTER_CLASS": DirectDupeFilter,
        }
        crawler = get_crawler(settings_dict=settings)
        scheduler = build_from_crawler(Scheduler, crawler)
        assert cast("DirectDupeFilter", scheduler.df).method == "n/a"

    def test_seenreq_truncated(self):
        r1 = Request("http://scrapytest.org/1")
        r2 = Request("http://scrapytest.org/2")

        path = tempfile.mkdtemp()
        try:
            df = _get_dupefilter(settings={"JOBDIR": path}, open_=False)
            try:
                df.open()
                df.request_seen(r1)
                df.request_seen(r2)
            finally:
                df.close("finished")

            seen_file = Path(path, "requests.seen")
            seen_file.write_bytes(seen_file.read_bytes()[:-1])

            df2 = _get_dupefilter(settings={"JOBDIR": path}, open_=False)
            try:
                df2.open()
                assert df2.request_seen(r1)
                assert not df2.request_seen(r2)
            finally:
                df2.close("finished")
        finally:
            shutil.rmtree(path)

    def test_log(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = {
            "DUPEFILTER_DEBUG": False,
            "DUPEFILTER_CLASS": FromCrawlerRFPDupeFilter,
        }
        crawler = get_crawler(SimpleSpider, settings_dict=settings)
        spider = SimpleSpider.from_crawler(crawler)
        dupefilter = _get_dupefilter(crawler=crawler)

        r1 = Request("http://scrapytest.org/index.html")
        r2 = Request("http://scrapytest.org/index.html")

        with caplog.at_level(logging.DEBUG):
            dupefilter.log(r1, spider)
            dupefilter.log(r2, spider)

        assert crawler.stats.get_value("dupefilter/filtered") == 2
        assert (
            "scrapy.dupefilters",
            logging.DEBUG,
            (
                "Filtered duplicate request: <GET http://scrapytest.org/index.html> - no more"
                " duplicates will be shown (see DUPEFILTER_DEBUG to show all duplicates)"
            ),
        ) in caplog.record_tuples

        dupefilter.close("finished")

    @pytest.mark.parametrize("df", [None, FromCrawlerRFPDupeFilter])
    def test_log_debug(
        self, caplog: pytest.LogCaptureFixture, df: type[BaseDupeFilter] | None
    ) -> None:
        settings: dict[str, Any] = {
            "DUPEFILTER_DEBUG": True,
        }
        if df:
            settings["DUPEFILTER_CLASS"] = df
        crawler = get_crawler(SimpleSpider, settings_dict=settings)
        spider = SimpleSpider.from_crawler(crawler)
        dupefilter = _get_dupefilter(crawler=crawler)

        r1 = Request("http://scrapytest.org/index.html")
        r2 = Request(
            "http://scrapytest.org/index.html",
            headers={"Referer": "http://scrapytest.org/INDEX.html"},
        )

        with caplog.at_level(logging.DEBUG):
            dupefilter.log(r1, spider)
            dupefilter.log(r2, spider)

        assert crawler.stats.get_value("dupefilter/filtered") == 2
        assert (
            "scrapy.dupefilters",
            logging.DEBUG,
            "Filtered duplicate request: <GET http://scrapytest.org/index.html> (referer: None)",
        ) in caplog.record_tuples
        assert (
            "scrapy.dupefilters",
            logging.DEBUG,
            (
                "Filtered duplicate request: <GET http://scrapytest.org/index.html>"
                " (referer: http://scrapytest.org/INDEX.html)"
            ),
        ) in caplog.record_tuples

        dupefilter.close("finished")

    def test_fingerprints_deprecation(self):
        dupefilter = cast("RFPDupeFilter", _get_dupefilter())
        request = Request("http://scrapytest.org/index.html")
        dupefilter.request_seen(request)
        with pytest.warns(
            ScrapyDeprecationWarning,
            match=r"RFPDupeFilter\.fingerprints is deprecated\.",
        ):
            fingerprints = dupefilter.fingerprints
        assert fingerprints == {dupefilter.request_fingerprint(request)}
        dupefilter.close("finished")

    def test_request_fingerprint_override_deprecation(self):
        class LegacyDupeFilter(RFPDupeFilter):
            def request_fingerprint(self, request):
                return hashlib.sha1(request.url.lower().encode()).hexdigest()

        with pytest.warns(
            ScrapyDeprecationWarning,
            match=r"Overriding RFPDupeFilter\.request_fingerprint\(\) is deprecated",
        ):
            dupefilter = _get_dupefilter(
                settings={"DUPEFILTER_CLASS": LegacyDupeFilter}
            )

        assert not dupefilter.request_seen(Request("http://scrapytest.org/index.html"))
        assert dupefilter.request_seen(Request("http://scrapytest.org/INDEX.html"))
        dupefilter.close("finished")


class _TornFile:
    """File wrapper that simulates a short write followed by an I/O error,
    as happens when a process dies or the disk fails mid-append."""

    def __init__(self, real: Any, keep: int, fail_truncate: bool = False):
        self._real = real
        self._keep = keep
        self._fail_truncate = fail_truncate

    def write(self, data: bytes) -> int:
        self._real.write(data[: self._keep])
        self._real.flush()
        raise OSError("simulated short write")

    def truncate(self, size: int | None = None) -> int:
        if self._fail_truncate:
            raise OSError("simulated truncate failure")
        return self._real.truncate(size)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class TestRFPDupeFilterJournal:
    """Tests for the recoverable journal mode of RFPDupeFilter
    (DUPEFILTER_JOURNAL)."""

    def _get_journal_dupefilter(self, path: Path, **kwargs: Any) -> RFPDupeFilter:
        return RFPDupeFilter(path=str(path), journal=True, **kwargs)

    @staticmethod
    def _record(dupefilter: RFPDupeFilter, request: Request) -> bytes:
        return _journal_record(dupefilter.fingerprinter.fingerprint(request))

    def test_setting_enables_journal_mode(self, tmp_path: Path) -> None:
        dupefilter = cast(
            "RFPDupeFilter",
            _get_dupefilter(
                settings={"JOBDIR": str(tmp_path), "DUPEFILTER_JOURNAL": True}
            ),
        )
        assert dupefilter._journal
        journal = tmp_path / "requests.seen.journal"
        assert journal.read_bytes() == _JOURNAL_HEADER
        assert not (tmp_path / "requests.seen").exists()
        dupefilter.close("finished")

    def test_journal_disabled_by_default(self, tmp_path: Path) -> None:
        dupefilter = cast(
            "RFPDupeFilter", _get_dupefilter(settings={"JOBDIR": str(tmp_path)})
        )
        assert not dupefilter._journal
        assert (tmp_path / "requests.seen").exists()
        assert not (tmp_path / "requests.seen.journal").exists()
        dupefilter.close("finished")

    def test_layout_and_reopen(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        r3 = Request("https://example.com/3")

        df = self._get_journal_dupefilter(tmp_path)
        assert not df.request_seen(r1)
        assert not df.request_seen(r2)
        journal = tmp_path / "requests.seen.journal"
        expected = _JOURNAL_HEADER + self._record(df, r1) + self._record(df, r2)
        assert journal.read_bytes() == expected
        df.close("finished")

        resumed = self._get_journal_dupefilter(tmp_path)
        assert resumed.request_seen(r1)
        assert resumed.request_seen(r2)
        assert not resumed.request_seen(r3)
        expected += self._record(resumed, r3)
        assert journal.read_bytes() == expected
        resumed.close("finished")
        assert not (tmp_path / "requests.seen.journal.orphan").exists()

    def test_empty_journal_file_is_initialized(self, tmp_path: Path) -> None:
        journal = tmp_path / "requests.seen.journal"
        journal.touch()
        df = self._get_journal_dupefilter(tmp_path)
        r1 = Request("https://example.com/1")
        assert not df.request_seen(r1)
        assert journal.read_bytes() == _JOURNAL_HEADER + self._record(df, r1)
        df.close("finished")

    def test_header_only_journal_reopens_clean(self, tmp_path: Path) -> None:
        df = self._get_journal_dupefilter(tmp_path)
        df.close("finished")
        resumed = self._get_journal_dupefilter(tmp_path)
        assert not resumed.request_seen(Request("https://example.com/1"))
        resumed.close("finished")
        assert not (tmp_path / "requests.seen.journal.orphan").exists()

    def test_truncated_record_quarantined(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        df = self._get_journal_dupefilter(tmp_path)
        df.request_seen(r1)
        df.request_seen(r2)
        record1 = self._record(df, r1)
        record2 = self._record(df, r2)
        df.close("finished")

        journal = tmp_path / "requests.seen.journal"
        data = journal.read_bytes()
        journal.write_bytes(data[:-3])  # tear off 3 bytes of the last record

        resumed = self._get_journal_dupefilter(tmp_path)
        # The committed record is recovered, the torn one is rescheduled.
        assert resumed.request_seen(r1)
        assert not resumed.request_seen(r2)
        # The torn tail was isolated into the orphan file and the journal was
        # truncated to the commit boundary.
        orphan = tmp_path / "requests.seen.journal.orphan"
        assert orphan.read_bytes() == record2[:-3]
        assert journal.read_bytes() == _JOURNAL_HEADER + record1 + record2
        resumed.close("finished")

    def test_truncated_length_field_quarantined(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        df = self._get_journal_dupefilter(tmp_path)
        df.request_seen(r1)
        record1 = self._record(df, r1)
        df.close("finished")

        journal = tmp_path / "requests.seen.journal"
        with journal.open("ab") as f:
            f.write(b"\x00")  # a single dangling byte of a length field

        resumed = self._get_journal_dupefilter(tmp_path)
        assert resumed.request_seen(r1)
        assert (tmp_path / "requests.seen.journal.orphan").read_bytes() == b"\x00"
        assert journal.read_bytes() == _JOURNAL_HEADER + record1
        resumed.close("finished")

    def test_bad_checksum_tail_quarantined(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        df = self._get_journal_dupefilter(tmp_path)
        df.request_seen(r1)
        df.request_seen(r2)
        record1 = self._record(df, r1)
        record2 = self._record(df, r2)
        df.close("finished")

        journal = tmp_path / "requests.seen.journal"
        data = bytearray(journal.read_bytes())
        data[-5] ^= 0xFF  # flip a fingerprint bit in the last record
        journal.write_bytes(bytes(data))

        resumed = self._get_journal_dupefilter(tmp_path)
        assert resumed.request_seen(r1)
        assert not resumed.request_seen(r2)  # uncommitted: rescheduled
        assert (tmp_path / "requests.seen.journal.orphan").read_bytes() == (
            record2[:-5] + bytes([record2[-5] ^ 0xFF]) + record2[-4:]
        )
        assert journal.read_bytes() == _JOURNAL_HEADER + record1 + record2
        resumed.close("finished")

    def test_mid_file_corruption_raises_and_preserves(self, tmp_path: Path) -> None:
        requests = [Request(f"https://example.com/{i}") for i in (1, 2, 3)]
        df = self._get_journal_dupefilter(tmp_path)
        for request in requests:
            df.request_seen(request)
        record1 = self._record(df, requests[0])
        df.close("finished")

        journal = tmp_path / "requests.seen.journal"
        data = bytearray(journal.read_bytes())
        corrupt_offset = len(_JOURNAL_HEADER) + len(record1)
        data[corrupt_offset + 2] ^= 0xFF  # corrupt the 2nd record's payload
        journal.write_bytes(bytes(data))
        before = journal.read_bytes()

        with pytest.raises(JournalCorruptionError) as excinfo:
            self._get_journal_dupefilter(tmp_path)
        assert excinfo.value.offset == corrupt_offset
        assert excinfo.value.path == journal
        assert str(corrupt_offset) in str(excinfo.value)
        # The original file is preserved, no orphan is created.
        assert journal.read_bytes() == before
        assert not (tmp_path / "requests.seen.journal.orphan").exists()

    def test_oversized_tail_raises_and_preserves(self, tmp_path: Path) -> None:
        df = self._get_journal_dupefilter(tmp_path)
        r1 = Request("https://example.com/1")
        df.request_seen(r1)
        df.close("finished")

        journal = tmp_path / "requests.seen.journal"
        with journal.open("ab") as f:
            f.write(b"\xff" * (70000))  # larger than any single record
        before = journal.read_bytes()

        with pytest.raises(JournalCorruptionError):
            self._get_journal_dupefilter(tmp_path)
        assert journal.read_bytes() == before

    def test_bad_magic_raises_and_preserves(self, tmp_path: Path) -> None:
        journal = tmp_path / "requests.seen.journal"
        journal.write_bytes(b"NOTAJOURNAL" * 4)
        before = journal.read_bytes()
        with pytest.raises(JournalFormatError) as excinfo:
            self._get_journal_dupefilter(tmp_path)
        assert excinfo.value.path == journal
        assert journal.read_bytes() == before

    def test_unsupported_version_raises_and_preserves(self, tmp_path: Path) -> None:
        journal = tmp_path / "requests.seen.journal"
        journal.write_bytes(b"SRFPJ\x00\x00\x63" + b"\x00" * 10)
        before = journal.read_bytes()
        with pytest.raises(JournalFormatError, match="version"):
            self._get_journal_dupefilter(tmp_path)
        assert journal.read_bytes() == before

    def test_legacy_file_requires_explicit_migration(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        legacy_df = RFPDupeFilter(path=str(tmp_path))
        legacy_df.request_seen(r1)
        legacy_df.close("finished")
        legacy_bytes = (tmp_path / "requests.seen").read_bytes()

        # Journal mode never converts the legacy format implicitly.
        with pytest.raises(JournalFormatError, match="migrate_legacy_journal"):
            self._get_journal_dupefilter(tmp_path)
        assert (tmp_path / "requests.seen").read_bytes() == legacy_bytes
        assert not (tmp_path / "requests.seen.journal").exists()

        journal_path = RFPDupeFilter.migrate_legacy_journal(str(tmp_path))
        assert journal_path == tmp_path / "requests.seen.journal"
        # The original file is kept as a backup with identical bytes.
        assert not (tmp_path / "requests.seen").exists()
        assert (tmp_path / "requests.seen.legacy.bak").read_bytes() == legacy_bytes

        df = self._get_journal_dupefilter(tmp_path)
        assert df.request_seen(r1)
        r2 = Request("https://example.com/2")
        assert not df.request_seen(r2)
        df.close("finished")

    def test_migration_refuses_to_overwrite(self, tmp_path: Path) -> None:
        with pytest.raises(JournalFormatError, match="No legacy"):
            RFPDupeFilter.migrate_legacy_journal(str(tmp_path))

        (tmp_path / "requests.seen").write_bytes(b"\x00\x01x")
        (tmp_path / "requests.seen.journal").write_bytes(_JOURNAL_HEADER)
        with pytest.raises(JournalFormatError, match="already exists"):
            RFPDupeFilter.migrate_legacy_journal(str(tmp_path))
        (tmp_path / "requests.seen.journal").unlink()

        (tmp_path / "requests.seen.legacy.bak").write_bytes(b"old backup")
        with pytest.raises(JournalFormatError, match="already exists"):
            RFPDupeFilter.migrate_legacy_journal(str(tmp_path))
        assert (tmp_path / "requests.seen").read_bytes() == b"\x00\x01x"
        assert not (tmp_path / "requests.seen.journal").exists()

    def test_short_write_failure_keeps_memory_and_disk_consistent(
        self, tmp_path: Path
    ) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        df = self._get_journal_dupefilter(tmp_path)
        df.request_seen(r1)
        record1 = self._record(df, r1)
        journal = tmp_path / "requests.seen.journal"

        df.file = _TornFile(df.file, keep=10)  # type: ignore[assignment]
        with pytest.raises(JournalWriteError):
            df.request_seen(r2)
        # The fingerprint did not enter the in-memory set, and the torn
        # append was rolled back: memory and disk are consistent.
        assert journal.read_bytes() == _JOURNAL_HEADER + record1

        # The request is rescheduled on the next call and the append retried.
        df.file = df.file._real
        assert not df.request_seen(r2)
        assert df.request_seen(r2)
        record2 = self._record(df, r2)
        assert journal.read_bytes() == _JOURNAL_HEADER + record1 + record2
        df.close("finished")

        resumed = self._get_journal_dupefilter(tmp_path)
        assert resumed.request_seen(r1)
        assert resumed.request_seen(r2)
        assert not (tmp_path / "requests.seen.journal.orphan").exists()
        resumed.close("finished")

    def test_failed_rollback_is_cleaned_before_next_append(
        self, tmp_path: Path
    ) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        df = self._get_journal_dupefilter(tmp_path)
        df.request_seen(r1)
        record1 = self._record(df, r1)
        journal = tmp_path / "requests.seen.journal"

        # The torn append cannot be rolled back either (failing disk).
        df.file = _TornFile(df.file, keep=10, fail_truncate=True)  # type: ignore[assignment]
        with pytest.raises(JournalWriteError):
            df.request_seen(r2)
        record2 = self._record(df, r2)
        assert journal.read_bytes() == _JOURNAL_HEADER + record1 + record2[:10]

        # The next successful append removes the torn tail first, so the
        # journal only ever contains valid records.
        df.file = df.file._real
        assert not df.request_seen(r2)
        assert journal.read_bytes() == _JOURNAL_HEADER + record1 + record2
        df.close("finished")

        resumed = self._get_journal_dupefilter(tmp_path)
        assert resumed.request_seen(r1)
        assert resumed.request_seen(r2)
        assert not (tmp_path / "requests.seen.journal.orphan").exists()
        resumed.close("finished")

    def test_failed_rollback_tail_quarantined_on_reopen(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        df = self._get_journal_dupefilter(tmp_path)
        df.request_seen(r1)
        record1 = self._record(df, r1)
        journal = tmp_path / "requests.seen.journal"

        # The process "dies" right after the failed append: no retry, no
        # cleanup. The torn tail is quarantined on the next open.
        df.file = _TornFile(df.file, keep=10, fail_truncate=True)  # type: ignore[assignment]
        with pytest.raises(JournalWriteError):
            df.request_seen(r2)
        record2 = self._record(df, r2)
        df.file.close()

        resumed = self._get_journal_dupefilter(tmp_path)
        assert resumed.request_seen(r1)
        assert not resumed.request_seen(r2)  # uncommitted: rescheduled
        assert (tmp_path / "requests.seen.journal.orphan").read_bytes() == record2[:10]
        assert journal.read_bytes() == _JOURNAL_HEADER + record1 + record2
        resumed.close("finished")

    def test_fsync_failure_keeps_memory_and_disk_consistent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r1 = Request("https://example.com/1")
        df = self._get_journal_dupefilter(tmp_path)

        def failing_fsync(fd: int) -> None:
            raise OSError("simulated fsync failure")

        monkeypatch.setattr(scrapy.dupefilters.os, "fsync", failing_fsync)
        with pytest.raises(JournalWriteError):
            df.request_seen(r1)
        monkeypatch.undo()

        # Not in memory: the request is rescheduled and the append retried.
        assert not df.request_seen(r1)
        assert df.request_seen(r1)
        df.close("finished")

        resumed = self._get_journal_dupefilter(tmp_path)
        assert resumed.request_seen(r1)
        resumed.close("finished")

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        df = self._get_journal_dupefilter(tmp_path)
        r1 = Request("https://example.com/1")
        df.request_seen(r1)
        df.close("finished")
        df.close("finished")  # no-op, no error
        assert df.file is None

        resumed = self._get_journal_dupefilter(tmp_path)
        assert resumed.request_seen(r1)
        resumed.close("finished")

    def test_failed_close_does_not_truncate_and_can_be_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        df = self._get_journal_dupefilter(tmp_path)
        r1 = Request("https://example.com/1")
        df.request_seen(r1)
        record1 = self._record(df, r1)

        def failing_fsync(fd: int) -> None:
            raise OSError("simulated fsync failure")

        monkeypatch.setattr(scrapy.dupefilters.os, "fsync", failing_fsync)
        with pytest.raises(OSError, match="simulated fsync failure"):
            df.close("finished")
        monkeypatch.undo()

        # The file is still open for a retry, and the log was not truncated.
        assert df.file is not None
        df.close("finished")
        assert df.file is None
        journal = tmp_path / "requests.seen.journal"
        assert journal.read_bytes() == _JOURNAL_HEADER + record1

    def test_duplicate_request_appends_single_record(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        df = self._get_journal_dupefilter(tmp_path)
        assert not df.request_seen(r1)
        assert df.request_seen(r1)
        journal = tmp_path / "requests.seen.journal"
        assert journal.read_bytes() == _JOURNAL_HEADER + self._record(df, r1)
        df.close("finished")


class TestDiskDupeFilter(DupeFilterTestMixin):
    dupefilter_class = DiskDupeFilter

    def test_temporary_database_removed(self) -> None:
        dupefilter = self._get_dupefilter()
        assert isinstance(dupefilter, DiskDupeFilter)
        assert dupefilter._tempdir
        tempdir = Path(dupefilter._tempdir)
        assert tempdir.exists()

        dupefilter.close("finished")
        assert not tempdir.exists()

    @pytest.mark.parametrize(
        ("sync_every", "committed"),
        [
            (0, [0, 0, 0, 0]),
            (1, [1, 1, 2, 3]),
            (2, [0, 0, 2, 2]),
        ],
    )
    def test_sync_every(
        self, tmp_path: Path, sync_every: int, committed: list[int]
    ) -> None:
        dupefilter = self._get_dupefilter(
            JOBDIR=str(tmp_path), JOBDIR_SYNC_EVERY=sync_every
        )
        # A second connection only sees committed rows.
        reader = sqlite3.connect(tmp_path / "requests.seen.db")
        urls = [f"https://example.com/{i}" for i in (1, 1, 2, 3)]
        for url, expected in zip(urls, committed, strict=True):
            dupefilter.request_seen(Request(url))
            (count,) = reader.execute("SELECT COUNT(*) FROM seen").fetchone()
            assert count == expected
        reader.close()
        dupefilter.close("finished")


class TestBaseDupeFilter:
    def test_log_deprecation(self):
        dupefilter = _get_dupefilter(
            settings={"DUPEFILTER_CLASS": BaseDupeFilter},
        )
        with pytest.warns(
            ScrapyDeprecationWarning,
            match=r"Calling BaseDupeFilter\.log\(\) is deprecated.",
        ):
            dupefilter.log(None, None)  # type: ignore[arg-type]
