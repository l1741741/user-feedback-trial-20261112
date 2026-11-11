from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from scrapy.core.scheduler import Scheduler
from scrapy.dupefilters import (
    BaseDupeFilter,
    DiskDupeFilter,
    RFPDupeFilter,
    _read_fingerprints,
)
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.http import Request
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.request import RequestFingerprinter
from scrapy.utils.test import get_crawler
from tests.spiders import SimpleSpider

if TYPE_CHECKING:
    from scrapy.crawler import Crawler


def _record(*fingerprints: bytes) -> bytes:
    return b"".join(
        len(fingerprint).to_bytes(2, "big") + fingerprint
        for fingerprint in fingerprints
    )


def _fingerprint(request: Request) -> bytes:
    return RequestFingerprinter().fingerprint(request)


def _migration_state(jobdir: Path) -> tuple[int, bool]:
    """Return the number of imported rows and whether the migration is marked
    as completed."""
    db_path = Path(jobdir, "requests.seen.db")
    if not db_path.exists():
        return 0, False
    db = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "seen" in tables:
            row = db.execute("SELECT COUNT(*) FROM seen").fetchone()
            rows = 0 if row is None else row[0]
        else:
            rows = 0
        migrated = False
        if "metadata" in tables:
            migrated = bool(
                db.execute(
                    "SELECT 1 FROM metadata WHERE key = ?",
                    ("requests.seen.migrated",),
                ).fetchone()
            )
    finally:
        db.close()
    return rows, migrated


def _storage_state(jobdir: Path) -> tuple[bytes | None, int, bool]:
    seen_path = Path(jobdir, "requests.seen")
    seen = seen_path.read_bytes() if seen_path.exists() else None
    rows, migrated = _migration_state(jobdir)
    return seen, rows, migrated


def test_read_fingerprints() -> None:
    assert list(_read_fingerprints(_record(b"a", b"b"))) == [b"a", b"b"]
    assert list(_read_fingerprints(_record(b"a") + b"\x00")) == [b"a"]
    with pytest.raises(ValueError, match="zero-length fingerprint record"):
        list(_read_fingerprints(b"\x00\x00"))


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

    def test_df_from_crawler_keeps_old_init_signature(self, tmp_path: Path) -> None:
        class OldInitDupeFilter(RFPDupeFilter):
            def __init__(self, path=None, debug=False, *, fingerprinter=None):
                super().__init__(
                    path=path, debug=debug, fingerprinter=fingerprinter
                )

        dupefilter = _get_dupefilter(
            settings={
                "DUPEFILTER_CLASS": OldInitDupeFilter,
                "JOBDIR": str(tmp_path),
            }
        )
        assert not dupefilter.request_seen(Request("https://example.com/1"))
        dupefilter.close("finished")

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

    @pytest.mark.parametrize("truncation", ["header", "payload"])
    def test_truncated_tail_is_removed_before_appending(
        self, tmp_path: Path, truncation: str
    ) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        r3 = Request("https://example.com/3")
        fp1 = _fingerprint(r1)
        fp2 = _fingerprint(r2)
        fp3 = _fingerprint(r3)

        dupefilter = self._get_dupefilter(JOBDIR=str(tmp_path))
        assert not dupefilter.request_seen(r1)
        assert not dupefilter.request_seen(r2)
        dupefilter.close("finished")

        seen_path = Path(tmp_path, "requests.seen")
        if truncation == "header":
            data = _record(fp1, fp2)[: 2 + len(fp1) + 1]
        else:
            data = _record(fp1, fp2)[:-1]
        seen_path.write_bytes(data)

        resumed = self._get_dupefilter(JOBDIR=str(tmp_path))
        assert resumed.request_seen(r1)
        assert not resumed.request_seen(r2)
        assert not resumed.request_seen(r3)
        resumed.close("finished")

        assert seen_path.read_bytes() == _record(fp1, fp2, fp3)

        restarted = self._get_dupefilter(JOBDIR=str(tmp_path))
        assert restarted.request_seen(r1)
        assert restarted.request_seen(r2)
        assert restarted.request_seen(r3)
        restarted.close("finished")

    def test_zero_length_record_is_corruption(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        fp1 = _fingerprint(r1)
        fp2 = _fingerprint(r2)
        seen_path = Path(tmp_path, "requests.seen")
        # A follow-up record shows that the zero-length record is not skipped.
        corrupted = _record(fp1) + b"\x00\x00" + _record(fp2)
        seen_path.write_bytes(corrupted)

        with pytest.raises(ValueError, match="zero-length fingerprint record"):
            self._get_dupefilter(JOBDIR=str(tmp_path))

        assert seen_path.read_bytes() == corrupted

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

    def test_temporary_database_removed_with_fingerprint_id(self) -> None:
        dupefilter = self._get_dupefilter(JOBDIR_FINGERPRINTER_ID="id-1")
        assert isinstance(dupefilter, DiskDupeFilter)
        assert dupefilter._tempdir
        tempdir = Path(dupefilter._tempdir)
        assert tempdir.exists()
        # A fingerprint ID only applies to a persistent job directory.
        assert not Path(tempdir, "requests.seen.id").exists()

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

    def test_migrates_requests_seen(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        r3 = Request("https://example.com/3")
        fp1 = _fingerprint(r1)
        fp2 = _fingerprint(r2)
        seen_path = Path(tmp_path, "requests.seen")
        data = _record(fp1, fp2)
        seen_path.write_bytes(data)

        dupefilter = self._get_dupefilter(JOBDIR=str(tmp_path))
        assert dupefilter.request_seen(r1)
        assert dupefilter.request_seen(r2)
        assert not dupefilter.request_seen(r3)
        dupefilter.close("finished")

        assert seen_path.read_bytes() == data
        rows, migrated = _migration_state(tmp_path)
        assert rows == 3
        assert migrated

    def test_migration_keeps_one_row_per_fingerprint(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        fp1 = _fingerprint(r1)
        fp2 = _fingerprint(r2)
        seen_path = Path(tmp_path, "requests.seen")
        seen_path.write_bytes(_record(fp1, fp1, fp2))

        dupefilter = self._get_dupefilter(JOBDIR=str(tmp_path))
        assert dupefilter.request_seen(r1)
        assert dupefilter.request_seen(r2)
        dupefilter.close("finished")

        rows, _ = _migration_state(tmp_path)
        assert rows == 2

    def test_migration_runs_only_once(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        fp1 = _fingerprint(r1)
        fp2 = _fingerprint(r2)
        seen_path = Path(tmp_path, "requests.seen")
        seen_path.write_bytes(_record(fp1))

        dupefilter = self._get_dupefilter(JOBDIR=str(tmp_path))
        assert dupefilter.request_seen(r1)
        dupefilter.close("finished")
        assert _migration_state(tmp_path) == (1, True)

        seen_path.write_bytes(_record(fp1, fp2))
        resumed = self._get_dupefilter(JOBDIR=str(tmp_path))
        assert resumed.request_seen(r1)
        # The completion marker prevents importing the appended record.
        assert not resumed.request_seen(r2)
        resumed.close("finished")
        assert _migration_state(tmp_path) == (2, True)

    @pytest.mark.parametrize("truncation", ["header", "payload"])
    def test_migration_ignores_truncated_tail(
        self, tmp_path: Path, truncation: str
    ) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        fp1 = _fingerprint(r1)
        fp2 = _fingerprint(r2)
        seen_path = Path(tmp_path, "requests.seen")
        if truncation == "header":
            data = _record(fp1) + b"\x00"
        else:
            data = _record(fp1, fp2)[:-1]
        seen_path.write_bytes(data)

        dupefilter = self._get_dupefilter(JOBDIR=str(tmp_path))
        assert dupefilter.request_seen(r1)
        assert not dupefilter.request_seen(r2)
        dupefilter.close("finished")

        assert seen_path.read_bytes() == data
        assert _migration_state(tmp_path) == (2, True)

    def test_migration_zero_length_fails_and_can_retry(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        fp1 = _fingerprint(r1)
        fp2 = _fingerprint(r2)
        seen_path = Path(tmp_path, "requests.seen")
        seen_path.write_bytes(_record(fp1, fp2) + b"\x00\x00")

        with pytest.raises(ValueError, match="zero-length fingerprint record"):
            self._get_dupefilter(JOBDIR=str(tmp_path))

        # The whole import was rolled back, marker included.
        assert _migration_state(tmp_path) == (0, False)

        seen_path.write_bytes(_record(fp1, fp2))
        resumed = self._get_dupefilter(JOBDIR=str(tmp_path))
        assert resumed.request_seen(r1)
        assert resumed.request_seen(r2)
        resumed.close("finished")
        assert _migration_state(tmp_path) == (2, True)

    def test_migration_interruption_rolls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapy import dupefilters as dupefilters_module

        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        fp1 = _fingerprint(r1)
        fp2 = _fingerprint(r2)
        seen_path = Path(tmp_path, "requests.seen")
        seen_path.write_bytes(_record(fp1, fp2))

        real_reader = dupefilters_module._RequestsSeenReader

        class InterruptingReader:
            def __init__(self, file: Any, path: Path | None = None) -> None:
                self._reader = real_reader(file, path)

            def __iter__(self) -> Any:
                for index, fingerprint in enumerate(self._reader):
                    if index == 1:
                        raise RuntimeError("interrupted")
                    yield fingerprint

        monkeypatch.setattr(
            dupefilters_module, "_RequestsSeenReader", InterruptingReader
        )
        with pytest.raises(RuntimeError, match="interrupted"):
            self._get_dupefilter(JOBDIR=str(tmp_path))
        assert _migration_state(tmp_path) == (0, False)

        monkeypatch.setattr(dupefilters_module, "_RequestsSeenReader", real_reader)
        resumed = self._get_dupefilter(JOBDIR=str(tmp_path))
        assert resumed.request_seen(r1)
        assert resumed.request_seen(r2)
        resumed.close("finished")
        assert _migration_state(tmp_path) == (2, True)

    def test_migrates_rfp_seen_with_first_fingerprint_id(
        self, tmp_path: Path
    ) -> None:
        r1 = Request("https://example.com/1")
        dupefilter = _get_dupefilter(
            settings={
                "DUPEFILTER_CLASS": RFPDupeFilter,
                "JOBDIR": str(tmp_path),
            }
        )
        assert not dupefilter.request_seen(r1)
        dupefilter.close("finished")

        resumed = self._get_dupefilter(
            JOBDIR=str(tmp_path), JOBDIR_FINGERPRINTER_ID="id-1"
        )
        assert resumed.request_seen(r1)
        resumed.close("finished")

        with pytest.raises(ValueError, match="JOBDIR_FINGERPRINTER_ID mismatch"):
            self._get_dupefilter(
                JOBDIR=str(tmp_path), JOBDIR_FINGERPRINTER_ID="id-2"
            )

    def test_migrates_rfp_seen_with_same_fingerprint_id(
        self, tmp_path: Path
    ) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")
        dupefilter = _get_dupefilter(
            settings={
                "DUPEFILTER_CLASS": RFPDupeFilter,
                "JOBDIR": str(tmp_path),
                "JOBDIR_FINGERPRINTER_ID": "id-1",
            }
        )
        assert not dupefilter.request_seen(r1)
        dupefilter.close("finished")

        resumed = self._get_dupefilter(
            JOBDIR=str(tmp_path), JOBDIR_FINGERPRINTER_ID="id-1"
        )
        assert resumed.request_seen(r1)
        assert not resumed.request_seen(r2)
        resumed.close("finished")

        with pytest.raises(ValueError, match="JOBDIR_FINGERPRINTER_ID mismatch"):
            self._get_dupefilter(
                JOBDIR=str(tmp_path), JOBDIR_FINGERPRINTER_ID="id-2"
            )

    def test_mismatch_does_not_create_database(self, tmp_path: Path) -> None:
        r1 = Request("https://example.com/1")
        dupefilter = _get_dupefilter(
            settings={
                "DUPEFILTER_CLASS": RFPDupeFilter,
                "JOBDIR": str(tmp_path),
                "JOBDIR_FINGERPRINTER_ID": "id-1",
            }
        )
        assert not dupefilter.request_seen(r1)
        dupefilter.close("finished")
        before = _storage_state(tmp_path)

        with pytest.raises(ValueError, match="JOBDIR_FINGERPRINTER_ID mismatch"):
            self._get_dupefilter(
                JOBDIR=str(tmp_path), JOBDIR_FINGERPRINTER_ID="id-2"
            )

        assert not Path(tmp_path, "requests.seen.db").exists()
        assert _storage_state(tmp_path) == before


@pytest.mark.parametrize("dupefilter_class", [RFPDupeFilter, DiskDupeFilter])
class TestFingerprintId:
    def _get(
        self,
        tmp_path: Path,
        dupefilter_class: type[BaseDupeFilter],
        fingerprint_id: str | None,
    ) -> BaseDupeFilter:
        settings: dict[str, Any] = {
            "DUPEFILTER_CLASS": dupefilter_class,
            "JOBDIR": str(tmp_path),
        }
        if fingerprint_id is not None:
            settings["JOBDIR_FINGERPRINTER_ID"] = fingerprint_id
        return _get_dupefilter(settings=settings)

    def test_same_id_resumes(
        self, tmp_path: Path, dupefilter_class: type[BaseDupeFilter]
    ) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")

        dupefilter = self._get(tmp_path, dupefilter_class, "id-1")
        assert not dupefilter.request_seen(r1)
        dupefilter.close("finished")

        resumed = self._get(tmp_path, dupefilter_class, "id-1")
        assert resumed.request_seen(r1)
        assert not resumed.request_seen(r2)
        resumed.close("finished")

    def test_mismatched_id_is_rejected_before_changes(
        self, tmp_path: Path, dupefilter_class: type[BaseDupeFilter]
    ) -> None:
        r1 = Request("https://example.com/1")

        dupefilter = self._get(tmp_path, dupefilter_class, "id-1")
        assert not dupefilter.request_seen(r1)
        dupefilter.close("finished")
        before = _storage_state(tmp_path)

        with pytest.raises(ValueError, match="JOBDIR_FINGERPRINTER_ID mismatch"):
            self._get(tmp_path, dupefilter_class, "id-2")

        assert _storage_state(tmp_path) == before

    def test_first_explicit_id_takes_over_existing_data(
        self, tmp_path: Path, dupefilter_class: type[BaseDupeFilter]
    ) -> None:
        r1 = Request("https://example.com/1")
        r2 = Request("https://example.com/2")

        dupefilter = self._get(tmp_path, dupefilter_class, None)
        assert not dupefilter.request_seen(r1)
        dupefilter.close("finished")

        resumed = self._get(tmp_path, dupefilter_class, "id-1")
        assert resumed.request_seen(r1)
        assert not resumed.request_seen(r2)
        resumed.close("finished")

        with pytest.raises(ValueError, match="JOBDIR_FINGERPRINTER_ID mismatch"):
            self._get(tmp_path, dupefilter_class, "id-2")

    def test_absent_id_keeps_compatibility(
        self, tmp_path: Path, dupefilter_class: type[BaseDupeFilter]
    ) -> None:
        r1 = Request("https://example.com/1")

        dupefilter = self._get(tmp_path, dupefilter_class, "id-1")
        assert not dupefilter.request_seen(r1)
        dupefilter.close("finished")

        resumed = self._get(tmp_path, dupefilter_class, None)
        assert resumed.request_seen(r1)
        resumed.close("finished")


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
