from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

import pytest

from scrapy.exceptions import NotConfigured
from scrapy.extensions import feedexport
from scrapy.extensions.feedexport import (
    FeedExporter,
    FileFeedStorage,
    S3FeedStorage,
    StdoutFeedStorage,
    _atomic_write_bytes,
    feed_digest_manifest_path,
    hash_stored_file,
    publish_feed_digest,
)
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test
from tests.utils.feedexport import crawl_items, export_by_path, unique_path

if TYPE_CHECKING:
    from tests.mockserver.http import MockServer


MANIFEST_SUFFIX = ".manifest.json"


def _manifest_path(feed_path: Path) -> Path:
    return feed_path.with_name(feed_path.name + MANIFEST_SUFFIX)


def _load_manifest(feed_path: Path) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        json.loads(_manifest_path(feed_path).read_text(encoding="utf-8")),
    )


def _assert_manifest_matches(
    feed_path: Path, *, fmt: str, item_count: int, batch_id: int
) -> dict[str, Any]:
    """Check that the manifest of *feed_path* describes its actual stored
    bytes, which are read back independently."""
    manifest = _load_manifest(feed_path)
    stored = feed_path.read_bytes()
    assert manifest == {
        "algorithm": "sha256",
        "batch_id": batch_id,
        "byte_count": len(stored),
        "digest": hashlib.sha256(stored).hexdigest(),
        "file_name": feed_path.name,
        "format": fmt,
        "item_count": item_count,
    }
    # The manifest is itself a standalone JSON document.
    assert manifest["byte_count"] > 0 or item_count == 0
    return manifest


class TestDigestExports:
    @coroutine_test
    async def test_manifest_published(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_path = unique_path(tmp_path) / "items.json"
        items = [{"foo": "bar"}, {"foo": "baz"}]
        settings = {
            "FEEDS": {
                str(feed_path): {
                    "format": "json",
                    "digest": True,
                },
            },
        }
        await export_by_path(mockserver, items, settings)

        assert feed_path.exists()
        _assert_manifest_matches(
            feed_path, fmt="json", item_count=len(items), batch_id=1
        )

    @coroutine_test
    async def test_manifest_disabled_by_default(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_path = unique_path(tmp_path) / "items.json"
        settings = {
            "FEEDS": {
                str(feed_path): {"format": "json"},
            },
        }
        await export_by_path(mockserver, [{"foo": "bar"}], settings)

        assert feed_path.exists()
        assert not _manifest_path(feed_path).exists()

    @coroutine_test
    async def test_empty_feed_manifest(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_path = unique_path(tmp_path) / "empty.json"
        settings = {
            "FEEDS": {
                str(feed_path): {
                    "format": "json",
                    "digest": True,
                },
            },
            "FEED_STORE_EMPTY": True,
            "FEED_EXPORT_INDENT": None,
        }
        await export_by_path(mockserver, [], settings)

        assert feed_path.read_bytes() == b"[]"
        _assert_manifest_matches(feed_path, fmt="json", item_count=0, batch_id=1)

    @coroutine_test
    async def test_empty_feed_not_stored(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        """Without store_empty, neither the feed nor a manifest is created."""
        feed_path = unique_path(tmp_path) / "empty.json"
        settings = {
            "FEEDS": {
                str(feed_path): {
                    "format": "json",
                    "digest": True,
                },
            },
            "FEED_STORE_EMPTY": False,
        }
        await export_by_path(mockserver, [], settings)

        assert not feed_path.exists()
        assert not _manifest_path(feed_path).exists()

    @coroutine_test
    async def test_batches_get_separate_manifests(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        directory = unique_path(tmp_path)
        items = [{"foo": "bar1"}, {"foo": "bar2"}, {"foo": "bar3"}]
        settings = {
            "FEEDS": {
                str(directory / "%(batch_id)d.json"): {
                    "format": "json",
                    "digest": True,
                },
            },
            "FEED_EXPORT_BATCH_ITEM_COUNT": 2,
        }
        await crawl_items(mockserver, items, settings)

        batch_paths = sorted(
            (
                p
                for p in directory.glob("*.json")
                if not p.name.endswith(MANIFEST_SUFFIX)
            ),
            key=lambda p: int(p.stem),
        )
        assert [p.name for p in batch_paths] == ["1.json", "2.json"]
        assert {p.name + MANIFEST_SUFFIX for p in batch_paths} <= {
            p.name for p in directory.iterdir()
        }
        _assert_manifest_matches(batch_paths[0], fmt="json", item_count=2, batch_id=1)
        _assert_manifest_matches(batch_paths[1], fmt="json", item_count=1, batch_id=2)

    @coroutine_test
    async def test_overwrite_replaces_manifest(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_path = unique_path(tmp_path) / "items.jsonl"
        url = str(feed_path)
        first_settings = {
            "FEEDS": {
                url: {
                    "format": "jsonlines",
                    "digest": True,
                    "mode": "overwrite",
                },
            },
        }
        await export_by_path(mockserver, [{"foo": "old"}], first_settings)
        old_manifest = _load_manifest(feed_path)

        await export_by_path(
            mockserver,
            [{"foo": "new1"}, {"foo": "new2"}],
            first_settings,
        )

        assert old_manifest["item_count"] == 1
        _assert_manifest_matches(feed_path, fmt="jsonlines", item_count=2, batch_id=1)

    @coroutine_test
    async def test_append_manifest_covers_whole_file(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_path = unique_path(tmp_path) / "items.jsonl"
        url = str(feed_path)
        settings = {
            "FEEDS": {
                url: {
                    "format": "jsonlines",
                    "digest": True,
                    "mode": "append",
                },
            },
        }
        await export_by_path(mockserver, [{"foo": "one"}], settings)
        await export_by_path(mockserver, [{"foo": "two"}], settings)

        # The digest covers every byte of the appended file, while the item
        # count only reflects the latest export.
        manifest = _assert_manifest_matches(
            feed_path, fmt="jsonlines", item_count=1, batch_id=1
        )
        assert len(feed_path.read_bytes().splitlines()) == 2
        assert manifest["byte_count"] == len(feed_path.read_bytes())

    @coroutine_test
    async def test_gzip_digest_covers_compressed_bytes(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_path = unique_path(tmp_path) / "items.csv.gz"
        items = [{"foo": "bar"}, {"foo": "baz"}]
        settings = {
            "FEEDS": {
                str(feed_path): {
                    "format": "csv",
                    "digest": True,
                    "postprocessing": ["scrapy.extensions.postprocessing.GzipPlugin"],
                    "gzip_mtime": 0,
                    "gzip_filename": "",
                },
            },
        }
        await export_by_path(mockserver, items, settings)

        compressed = feed_path.read_bytes()
        # The stored file really is gzip data ...
        assert gzip.decompress(compressed)
        # ... and the manifest hashes that compressed output, not the
        # pre-compression items.
        manifest = _load_manifest(feed_path)
        assert manifest["digest"] == hashlib.sha256(compressed).hexdigest()
        assert manifest["byte_count"] == len(compressed)
        assert (
            manifest["digest"]
            != hashlib.sha256(gzip.decompress(compressed)).hexdigest()
        )
        assert manifest["item_count"] == 2

    @coroutine_test
    async def test_store_failure_keeps_previous_manifest(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_path = unique_path(tmp_path) / "items.json"
        url = str(feed_path)
        settings = {
            "FEEDS": {
                url: {
                    "format": "json",
                    "digest": True,
                    "mode": "overwrite",
                },
            },
        }
        await export_by_path(mockserver, [{"foo": "good"}], settings)
        old_manifest_bytes = _manifest_path(feed_path).read_bytes()
        old_manifest = json.loads(old_manifest_bytes)

        def failing_store(file):
            file.close()
            raise OSError("disk on fire")

        with mock.patch.object(FileFeedStorage, "store", side_effect=failing_store):
            crawler = get_crawler(
                settings_dict={**settings, "FEEDS": {url: settings["FEEDS"][url]}}
            )
            await crawler.crawl_async()

        # The previously valid manifest is untouched and no temporary files
        # were left behind.
        assert _manifest_path(feed_path).read_bytes() == old_manifest_bytes
        assert _load_manifest(feed_path) == old_manifest
        assert not [p for p in feed_path.parent.iterdir() if p.name.startswith(".")]

    @coroutine_test
    async def test_readback_failure_keeps_previous_manifest(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_path = unique_path(tmp_path) / "items.json"
        url = str(feed_path)
        settings = {
            "FEEDS": {
                url: {
                    "format": "json",
                    "digest": True,
                    "mode": "overwrite",
                },
            },
        }
        await export_by_path(mockserver, [{"foo": "good"}], settings)
        old_manifest_bytes = _manifest_path(feed_path).read_bytes()

        with mock.patch(
            "scrapy.extensions.feedexport.hash_stored_file",
            side_effect=OSError("cannot read back"),
        ):
            crawler = get_crawler(settings_dict=settings)
            await crawler.crawl_async()

        assert _manifest_path(feed_path).read_bytes() == old_manifest_bytes
        stats = crawler.stats.get_stats()
        assert stats["feedexport/digest_failed/FileFeedStorage"] == 1
        assert "feedexport/digest_count/FileFeedStorage" not in stats
        assert stats["feedexport/success_count/FileFeedStorage"] == 1

    @coroutine_test
    async def test_rename_failure_keeps_previous_manifest(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_path = unique_path(tmp_path) / "items.json"
        url = str(feed_path)
        settings = {
            "FEEDS": {
                url: {
                    "format": "json",
                    "digest": True,
                    "mode": "overwrite",
                },
            },
        }
        await export_by_path(mockserver, [{"foo": "good"}], settings)
        old_manifest_bytes = _manifest_path(feed_path).read_bytes()

        real_replace = os.replace

        def failing_replace(src, dst):
            if str(dst) == str(_manifest_path(feed_path)):
                raise OSError("rename failed")
            return real_replace(src, dst)

        with mock.patch(
            "scrapy.extensions.feedexport.os.replace", side_effect=failing_replace
        ):
            crawler = get_crawler(settings_dict=settings)
            await crawler.crawl_async()

        assert _manifest_path(feed_path).read_bytes() == old_manifest_bytes
        assert crawler.stats.get_value("feedexport/digest_failed/FileFeedStorage") == 1
        # The failed temporary manifest is cleaned up.
        leftovers = [
            p
            for p in feed_path.parent.iterdir()
            if p.name.startswith(f".{_manifest_path(feed_path).name}.")
        ]
        assert leftovers == []

    @coroutine_test
    async def test_success_stats(self, mockserver: MockServer, tmp_path: Path) -> None:
        feed_path = unique_path(tmp_path) / "items.json"
        settings = {
            "FEEDS": {
                str(feed_path): {
                    "format": "json",
                    "digest": True,
                },
            },
        }
        crawler = await crawl_items(mockserver, [{"foo": "bar"}], settings)
        stats = crawler.stats.get_stats()
        assert stats["feedexport/digest_count/FileFeedStorage"] == 1
        assert "feedexport/digest_failed/FileFeedStorage" not in stats

    @coroutine_test
    async def test_feed_digest_setting_fallback(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_path = unique_path(tmp_path) / "items.json"
        settings = {
            "FEED_DIGEST": True,
            "FEEDS": {
                str(feed_path): {"format": "json"},
            },
        }
        await crawl_items(mockserver, [{"foo": "bar"}], settings)
        assert _manifest_path(feed_path).exists()

    @coroutine_test
    async def test_feed_option_overrides_setting(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_path = unique_path(tmp_path) / "items.json"
        settings = {
            "FEED_DIGEST": True,
            "FEEDS": {
                str(feed_path): {
                    "format": "json",
                    "digest": False,
                },
            },
        }
        await crawl_items(mockserver, [{"foo": "bar"}], settings)
        assert not _manifest_path(feed_path).exists()


class TestDigestCapability:
    def test_file_storage_supports_digest(self) -> None:
        assert FileFeedStorage.supports_digest is True

    def test_remote_storage_without_capability_refused(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # boto3 is an optional dependency; emulate a remote storage class
        # without instantiating the real one.
        class FakeRemoteStorage:
            def __init__(self, uri, *, feed_options=None):
                self.uri = uri

        settings = {
            "FEED_STORAGES": {"s3": FakeRemoteStorage},
            "FEEDS": {
                "s3://bucket/items.json": {
                    "format": "json",
                    "digest": True,
                },
            },
        }
        crawler = get_crawler(settings_dict=settings)
        # get_crawler() already tried to build the extensions once, so clear
        # the capability error recorded during crawler initialization and
        # assert the explicit construction logs it exactly once more.
        caplog.clear()
        with caplog.at_level("ERROR"), pytest.raises(NotConfigured):
            FeedExporter(crawler)
        errors = [
            record.getMessage()
            for record in caplog.records
            if record.levelname == "ERROR"
            and "digest feed option" in record.getMessage()
        ]
        assert len(errors) == 1
        assert "does not" in errors[0]
        assert "s3://bucket/items.json" in errors[0]
        assert "FakeRemoteStorage" in errors[0]

    def test_crawler_startup_refuses_unsupported_storage(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The refusal happens while the crawler builds its extensions, so a
        crawl configured with an unsupported digest feed never starts and the
        reason is logged exactly once."""

        class FakeRemoteStorage:
            def __init__(self, uri, *, feed_options=None):
                self.uri = uri

        settings = {
            "FEED_STORAGES": {"s3": FakeRemoteStorage},
            "FEEDS": {
                "s3://bucket/items.json": {
                    "format": "json",
                    "digest": True,
                },
            },
        }
        with caplog.at_level("ERROR"):
            crawler = get_crawler(settings_dict=settings)
        # The FeedExporter extension disabled itself instead of raising out
        # of crawler creation.
        assert not any(
            isinstance(extension, FeedExporter)
            for extension in crawler.extensions.middlewares
        )
        errors = [
            record.getMessage()
            for record in caplog.records
            if "digest feed option" in record.getMessage()
        ]
        assert len(errors) == 1

    def test_stdout_storage_refused(self, caplog: pytest.LogCaptureFixture) -> None:
        settings = {
            "FEEDS": {
                "stdout:": {
                    "format": "json",
                    "digest": True,
                },
            },
        }
        crawler = get_crawler(settings_dict=settings)
        caplog.clear()
        with caplog.at_level("ERROR"), pytest.raises(NotConfigured):
            FeedExporter(crawler)
        assert StdoutFeedStorage.__name__ in caplog.text

    def test_remote_storage_without_digest_option_runs(self) -> None:
        # The capability check only applies when the manifest is requested:
        # remote feeds keep working unchanged by default.
        class FakeRemoteStorage:
            def __init__(self, uri, *, feed_options=None):
                self.uri = uri

        settings = {
            "FEED_STORAGES": {"s3": FakeRemoteStorage},
            "FEEDS": {"s3://bucket/items.json": {"format": "json"}},
        }
        crawler = get_crawler(settings_dict=settings)
        exporter = FeedExporter(crawler)
        assert exporter.feeds["s3://bucket/items.json"]["digest"] is False

    def test_s3_storage_has_no_capability(self) -> None:
        assert not getattr(S3FeedStorage, "supports_digest", False)


class TestDigestHelpers:
    def test_manifest_path(self, tmp_path: Path) -> None:
        assert feed_digest_manifest_path(tmp_path / "a.json") == (
            tmp_path / "a.json.manifest.json"
        )

    def test_hash_stored_file(self, tmp_path: Path) -> None:
        path = tmp_path / "feed"
        content = b"x" * (3 * 1024 * 1024 + 7)
        path.write_bytes(content)
        digest, size = hash_stored_file(path)
        assert digest == hashlib.sha256(content).hexdigest()
        assert size == len(content)

    def test_hash_stored_file_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "feed"
        path.write_bytes(b"")
        digest, size = hash_stored_file(path)
        assert digest == hashlib.sha256(b"").hexdigest()
        assert size == 0

    def test_hash_reads_stored_bytes_not_memory(self, tmp_path: Path) -> None:
        """The hash must come from reading the file back, even if another
        process changes the file after it was written."""
        path = tmp_path / "feed"
        path.write_bytes(b"first version")
        digest_before, _ = hash_stored_file(path)
        path.write_bytes(b"second version, different length")
        digest_after, size_after = hash_stored_file(path)
        assert digest_before != digest_after
        assert digest_after == hashlib.sha256(path.read_bytes()).hexdigest()
        assert size_after == len(path.read_bytes())

    def test_publish_feed_digest(self, tmp_path: Path) -> None:
        feed_path = tmp_path / "items.jsonl"
        feed_path.write_bytes(b'{"a": 1}\n{"a": 2}\n')
        manifest_path = publish_feed_digest(
            feed_path=feed_path,
            format="jsonlines",
            item_count=2,
            batch_id=1,
        )
        assert manifest_path == _manifest_path(feed_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest == {
            "algorithm": "sha256",
            "batch_id": 1,
            "byte_count": feed_path.stat().st_size,
            "digest": hashlib.sha256(feed_path.read_bytes()).hexdigest(),
            "file_name": "items.jsonl",
            "format": "jsonlines",
            "item_count": 2,
        }

    def test_offline_consumer_verification_detects_corruption(
        self, tmp_path: Path
    ) -> None:
        """An offline consumer recomputes the digest of the transferred file
        and compares it with the manifest; a truncated or modified transfer
        is rejected."""
        feed_path = tmp_path / "items.jsonl"
        feed_path.write_bytes(b'{"a": 1}\n{"a": 2}\n')
        publish_feed_digest(
            feed_path=feed_path,
            format="jsonlines",
            item_count=2,
            batch_id=1,
        )

        def verify(received: Path) -> bool:
            manifest = _load_manifest(feed_path)
            digest, size = hash_stored_file(received)
            return (
                digest == manifest["digest"]
                and size == manifest["byte_count"]
                and received.name == manifest["file_name"]
            )

        # An intact transfer verifies.
        received = tmp_path / "copy" / feed_path.name
        received.parent.mkdir()
        received.write_bytes(feed_path.read_bytes())
        assert verify(received)

        # A truncated transfer (incomplete download) is rejected.
        truncated = tmp_path / "truncated" / feed_path.name
        truncated.parent.mkdir()
        truncated.write_bytes(feed_path.read_bytes()[:-3])
        assert not verify(truncated)

        # A byte-altered transfer of the same length is rejected too.
        tampered = tmp_path / "tampered" / feed_path.name
        tampered.parent.mkdir()
        data = bytearray(feed_path.read_bytes())
        data[0] ^= 0xFF
        tampered.write_bytes(data)
        assert not verify(tampered)

    def test_publish_failure_does_not_create_manifest(self, tmp_path: Path) -> None:
        feed_path = tmp_path / "items.jsonl"
        feed_path.write_bytes(b"{}")
        with (
            mock.patch.object(
                feedexport, "hash_stored_file", side_effect=OSError("boom")
            ),
            pytest.raises(OSError, match="boom"),
        ):
            publish_feed_digest(
                feed_path=feed_path,
                format="jsonlines",
                item_count=1,
                batch_id=1,
            )
        assert list(tmp_path.iterdir()) == [feed_path]

    def test_atomic_write_replaces_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_text("old", encoding="utf-8")
        _atomic_write_bytes(path, b"new")
        assert path.read_bytes() == b"new"
        assert not list(tmp_path.glob("*.tmp"))

    def test_atomic_write_failure_cleans_temp_file(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_bytes(b"old")
        with (
            mock.patch(
                "scrapy.extensions.feedexport.os.replace",
                side_effect=OSError("rename failed"),
            ),
            pytest.raises(OSError, match="rename failed"),
        ):
            _atomic_write_bytes(path, b"new")
        assert path.read_bytes() == b"old"
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]

    def test_atomic_write_write_failure_cleans_temp_file(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_bytes(b"old")
        real_named_temporary_file = tempfile.NamedTemporaryFile

        class FailingTempFile:
            def __init__(self, *args: Any, **kwargs: Any):
                self._inner = real_named_temporary_file(*args, **kwargs)
                self.name = self._inner.name

            def write(self, data: bytes) -> int:
                raise OSError("no space left on device")

            def flush(self) -> None:
                self._inner.flush()

            def fileno(self) -> int:
                return self._inner.fileno()

            def close(self) -> None:
                self._inner.close()

        with (
            mock.patch(
                "scrapy.extensions.feedexport.NamedTemporaryFile", FailingTempFile
            ),
            pytest.raises(OSError, match="no space left"),
        ):
            _atomic_write_bytes(path, b"new")
        assert path.read_bytes() == b"old"
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]

    def test_publish_uses_temp_dir_for_verification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Manifest publication must materialize its temporary file in the
        destination directory so the final rename is atomic on the same
        filesystem."""
        feed_path = tmp_path / "items.jsonl"
        feed_path.write_bytes(b"data")
        seen_dirs: list[str] = []
        real_mkstemp = tempfile.NamedTemporaryFile

        def spy_mkstemp(*args: Any, **kwargs: Any) -> Any:
            seen_dirs.append(kwargs.get("dir", ""))
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(
            "scrapy.extensions.feedexport.NamedTemporaryFile", spy_mkstemp
        )
        publish_feed_digest(
            feed_path=feed_path,
            format="jsonlines",
            item_count=1,
            batch_id=1,
        )
        assert seen_dirs
        assert all(Path(d) == tmp_path for d in seen_dirs)
