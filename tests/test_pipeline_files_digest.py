from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import tempfile
import time
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from scrapy.http import Request, Response
from scrapy.item import Field, Item
from scrapy.pipelines.files import (
    DigestVerificationError,
    FSFilesStore,
    FilesPipeline,
    _MalformedDigestError,
    _parse_content_md5_header,
    _parse_digest_header,
)
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler

from .utils.decorators import coroutine_test
from .utils.media_pipelines import DUMMY_SPIDER_INFO, mocked_download_func


BODY = b"the quick brown fox jumps over the lazy dog"
SHA256_HEX = hashlib.sha256(BODY).hexdigest()
MD5_HEX = hashlib.md5(BODY).hexdigest()
SHA256_B64 = base64.b64encode(bytes.fromhex(SHA256_HEX)).decode()
MD5_B64 = base64.b64encode(bytes.fromhex(MD5_HEX)).decode()
OTHER_SHA256_HEX = hashlib.sha256(b"different bytes").hexdigest()
OTHER_SHA256_B64 = base64.b64encode(bytes.fromhex(OTHER_SHA256_HEX)).decode()


class DigestItem(Item):
    file_urls = Field()
    files = Field()


def _item(*urls: str) -> DigestItem:
    item = DigestItem()
    item["file_urls"] = list(urls)
    return item


def _request(
    url: str,
    *,
    body: bytes = BODY,
    headers: dict[str, str] | None = None,
    flags: list[str] | None = None,
    status: int = 200,
) -> Request:
    return Request(
        url,
        meta={
            "response": Response(
                url, body=body, headers=headers or {}, flags=flags, status=status
            )
        },
    )


class TestDigestHeaderParsing:
    def test_digest_rfc9530_colon_form(self) -> None:
        assert _parse_digest_header(f"sha-256=:{SHA256_B64}:") == [
            ("sha-256", bytes.fromhex(SHA256_HEX))
        ]

    def test_digest_rfc3230_bare_base64_form(self) -> None:
        assert _parse_digest_header(f"sha-256={SHA256_B64}") == [
            ("sha-256", bytes.fromhex(SHA256_HEX))
        ]

    def test_digest_several_algorithms(self) -> None:
        parsed = _parse_digest_header(
            f"sha-256={SHA256_B64}, md5={MD5_B64}"
        )
        assert parsed == [
            ("sha-256", bytes.fromhex(SHA256_HEX)),
            ("md5", bytes.fromhex(MD5_HEX)),
        ]

    def test_digest_algorithm_names_are_case_insensitive(self) -> None:
        assert _parse_digest_header(f"SHA-256={SHA256_B64}") == [
            ("sha-256", bytes.fromhex(SHA256_HEX))
        ]

    def test_digest_whitespace_around_members(self) -> None:
        parsed = _parse_digest_header(
            f"  sha-256 = {SHA256_B64} ,  md5 = {MD5_B64} "
        )
        assert [algorithm for algorithm, _ in parsed] == ["sha-256", "md5"]

    def test_content_md5(self) -> None:
        assert _parse_content_md5_header(MD5_B64) == (
            "md5",
            bytes.fromhex(MD5_HEX),
        )

    @pytest.mark.parametrize(
        "value",
        [
            "not-base64!!!",
            "sha-256=:not-base64:",
            "sha-256=",
            "sha-256=:abc:",  # valid base64, wrong length
            MD5_B64,  # 16 bytes instead of 32
            "sha-256",  # missing '='
            "sha-256=",
            "",
            "weird-algo=aaaa",
            "sha-256=,md5=x",
        ],
    )
    def test_digest_malformed(self, value: str) -> None:
        with pytest.raises(_MalformedDigestError):
            _parse_digest_header(value)

    @pytest.mark.parametrize(
        "value",
        [
            "not-base64!!!",
            "",
            f"{MD5_B64},{MD5_B64}",  # Content-MD5 holds a single digest
            SHA256_B64,  # 32 bytes instead of 16
        ],
    )
    def test_content_md5_malformed(self, value: str) -> None:
        with pytest.raises(_MalformedDigestError):
            _parse_content_md5_header(value)

    def test_digest_unsupported_algorithm_is_an_explicit_error(self) -> None:
        with pytest.raises(_MalformedDigestError, match="unsupported"):
            _parse_digest_header("sha-1=qwek")


class TestDigestVerificationPipeline:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        self.tempdir = str(tmp_path)
        self.pipeline = self._create_pipeline(FilesPipeline)

    def _create_pipeline(
        self,
        pipeline_cls: type[FilesPipeline] = FilesPipeline,
        settings: dict[str, Any] | None = None,
    ) -> FilesPipeline:
        crawler = get_crawler(
            DefaultSpider,
            {
                "FILES_STORE": self.tempdir,
                "FILES_VERIFY_DIGEST": True,
                **(settings or {}),
            },
        )
        crawler.spider = crawler._create_spider()
        crawler.engine = mock.MagicMock(download_async=mocked_download_func)
        pipeline = build_from_crawler(pipeline_cls, crawler)
        pipeline.open_spider()
        return pipeline

    def _fresh_pipeline(self) -> FilesPipeline:
        """A new pipeline over the same store directory, modelling a new
        crawl run: the in-memory per-spider media cache starts empty while
        the on-disk files (and their digest sidecars) are kept."""
        return self._create_pipeline(FilesPipeline)

    # -- successful verification ------------------------------------------------

    @pytest.mark.parametrize(
        ("headers", "expected_algorithm", "expected_hex"),
        [
            (
                {"Digest": f"sha-256=:{SHA256_B64}:"},
                "sha-256",
                SHA256_HEX,
            ),
            ({"Digest": f"sha-256={SHA256_B64}"}, "sha-256", SHA256_HEX),
            ({"Content-MD5": MD5_B64}, "md5", MD5_HEX),
        ],
    )
    @coroutine_test
    async def test_verification_success(
        self, headers: dict[str, str], expected_algorithm: str, expected_hex: str
    ) -> None:
        url = f"http://example.com/{expected_algorithm}.bin"
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[_request(url, headers=headers)],
        ):
            result = await self.pipeline.process_item(_item(url))
        assert len(result["files"]) == 1
        info = result["files"][0]
        assert info["status"] == "downloaded"
        assert info["digests"] == {expected_algorithm: expected_hex}
        stored = Path(self.tempdir) / info["path"]
        assert stored.read_bytes() == BODY
        # The verified digests are persisted next to the file.
        sidecar = stored.with_name(stored.name + FSFilesStore.DIGEST_SUFFIX)
        assert json.loads(sidecar.read_text()) == {
            "digests": {expected_algorithm: expected_hex}
        }
        # No half-written file or sidecar is left behind.
        assert list(stored.parent.glob("*.tmp")) == []

    @coroutine_test
    async def test_both_algorithms_are_all_verified(self) -> None:
        url = "http://example.com/both.bin"
        headers = {
            "Digest": f"sha-256={SHA256_B64}, md5={MD5_B64}",
        }
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[_request(url, headers=headers)],
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"][0]["digests"] == {
            "sha-256": SHA256_HEX,
            "md5": MD5_HEX,
        }

    @coroutine_test
    async def test_digest_and_content_md5_cross_check(self) -> None:
        url = "http://example.com/cross.bin"
        headers = {
            "Digest": f"sha-256={SHA256_B64}",
            "Content-MD5": MD5_B64,
        }
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[_request(url, headers=headers)],
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"][0]["digests"] == {
            "sha-256": SHA256_HEX,
            "md5": MD5_HEX,
        }

    @coroutine_test
    async def test_default_checksum_is_still_computed(self) -> None:
        url = "http://example.com/checksum.bin"
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(url, headers={"Digest": f"sha-256={SHA256_B64}"})
            ],
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"][0]["checksum"] == MD5_HEX

    # -- explicit failures ------------------------------------------------------

    @coroutine_test
    async def test_mismatch_fails_and_does_not_store_anything(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        url = "http://example.com/tampered.bin"
        with (
            caplog.at_level(logging.WARNING),
            mock.patch.object(
                FilesPipeline,
                "get_media_requests",
                return_value=[
                    _request(
                        url,
                        body=BODY,
                        headers={"Digest": f"sha-256={OTHER_SHA256_B64}"},
                    )
                ],
            ),
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"] == []
        assert "digest-error" in caplog.text
        assert "sha-256 digest mismatch" in caplog.text
        # Nothing was written under the store at all.
        assert list(Path(self.tempdir).rglob("*")) == [] or all(
            path.is_dir() for path in Path(self.tempdir).rglob("*")
        )
        assert list(Path(self.tempdir).rglob("*.tmp")) == []

    @coroutine_test
    async def test_missing_digest_headers_is_a_failure(self) -> None:
        url = "http://example.com/no-digest.bin"
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[_request(url)],
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"] == []

    @coroutine_test
    async def test_malformed_base64_is_an_explicit_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        url = "http://example.com/malformed.bin"
        with (
            caplog.at_level(logging.WARNING),
            mock.patch.object(
                FilesPipeline,
                "get_media_requests",
                return_value=[
                    _request(
                        url, headers={"Content-MD5": "@@@not-base64@@@"}
                    )
                ],
            ),
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"] == []
        assert "invalid base64" in caplog.text

    @coroutine_test
    async def test_unsupported_algorithm_only_is_a_failure(self) -> None:
        url = "http://example.com/unsupported.bin"
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(url, headers={"Digest": "sha-1=qweqwe"})
            ],
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"] == []

    @coroutine_test
    async def test_one_matching_but_one_failing_algorithm_fails(self) -> None:
        """All advertised supported algorithms must match; a single mismatch
        can never be compensated by another correct digest."""
        url = "http://example.com/partial.bin"
        headers = {
            "Digest": f"sha-256={OTHER_SHA256_B64}, md5={MD5_B64}",
        }
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[_request(url, headers=headers)],
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"] == []

    @coroutine_test
    async def test_conflicting_cross_header_digests_fail(self) -> None:
        url = "http://example.com/conflict.bin"
        headers = {
            "Digest": f"md5={base64.b64encode(b'X' * 16).decode()}",
            "Content-MD5": MD5_B64,
        }
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[_request(url, headers=headers)],
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"] == []

    @coroutine_test
    async def test_failure_raises_digest_verification_error_directly(self) -> None:
        pipeline = self.pipeline
        request = _request(
            "http://example.com/direct.bin",
            headers={"Digest": f"sha-256={OTHER_SHA256_B64}"},
        )
        response = request.meta["response"]
        with pytest.raises(DigestVerificationError):
            pipeline._verify_response_digest(response, request)

    # -- existing files are never replaced --------------------------------------

    @coroutine_test
    async def test_failure_does_not_replace_an_existing_file(self) -> None:
        url = "http://example.com/keep.bin"
        # First, store a verified file.
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(url, headers={"Digest": f"sha-256={SHA256_B64}"})
            ],
        ):
            good = await self.pipeline.process_item(_item(url))
        path = Path(self.tempdir) / good["files"][0]["path"]
        sidecar = path.with_name(path.name + FSFilesStore.DIGEST_SUFFIX)
        assert path.read_bytes() == BODY

        # In a later crawl run, a re-download whose body does not match its
        # own digest must leave the previously stored file (and sidecar)
        # byte-identical. A fresh pipeline is used to bypass the in-memory
        # per-spider media cache, exactly like a new run would; FILES_EXPIRES
        # 0 forces a re-download instead of an up-to-date skip.
        next_pipeline = self._create_pipeline(
            FilesPipeline, {"FILES_EXPIRES": 0}
        )
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(
                    url,
                    body=b"corrupted new body",
                    headers={"Digest": f"sha-256={OTHER_SHA256_B64}"},
                )
            ],
        ):
            bad = await next_pipeline.process_item(_item(url))
        assert bad["files"] == []
        assert path.read_bytes() == BODY
        assert json.loads(sidecar.read_text())["digests"] == {
            "sha-256": SHA256_HEX
        }
        assert list(path.parent.glob("*.tmp")) == []

    # -- cache (uptodate) behaviour ---------------------------------------------

    @coroutine_test
    async def test_verified_sidecar_allows_uptodate_skip(self) -> None:
        url = "http://example.com/cached.bin"
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(url, headers={"Digest": f"sha-256={SHA256_B64}"})
            ],
        ):
            first = await self.pipeline.process_item(_item(url))
        assert first["files"][0]["status"] == "downloaded"

        # A later crawl run reuses the file: the mocked response would fail
        # verification, so an "uptodate" result proves no re-download
        # happened. A fresh pipeline models the new run (empty media cache).
        next_pipeline = self._fresh_pipeline()
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(
                    url,
                    body=b"would never verify",
                    headers={"Digest": f"sha-256={OTHER_SHA256_B64}"},
                )
            ],
        ):
            second = await next_pipeline.process_item(_item(url))
        assert second["files"][0]["status"] == "uptodate"
        assert second["files"][0]["digests"] == {"sha-256": SHA256_HEX}

    @coroutine_test
    async def test_cached_file_without_digest_metadata_is_redownloaded(
        self,
    ) -> None:
        url = "http://example.com/legacy.bin"
        # Simulate a file stored before FILES_VERIFY_DIGEST existed: no
        # sidecar, only the file itself.
        store = self.pipeline.store
        assert isinstance(store, FSFilesStore)
        path = self.pipeline.file_path(Request(url))
        store.persist_file(path, BytesIO(BODY), DUMMY_SPIDER_INFO)
        absolute = store._get_filesystem_path(path)
        assert not absolute.with_name(
            absolute.name + FSFilesStore.DIGEST_SUFFIX
        ).exists()

        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(url, headers={"Digest": f"sha-256={SHA256_B64}"})
            ],
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"][0]["status"] == "downloaded"
        assert result["files"][0]["digests"] == {"sha-256": SHA256_HEX}

    @coroutine_test
    async def test_corrupt_sidecar_forces_redownload(self) -> None:
        url = "http://example.com/corrupt-sidecar.bin"
        store = self.pipeline.store
        assert isinstance(store, FSFilesStore)
        path = self.pipeline.file_path(Request(url))
        store.persist_file(path, BytesIO(BODY), DUMMY_SPIDER_INFO)
        absolute = store._get_filesystem_path(path)
        absolute.with_name(
            absolute.name + FSFilesStore.DIGEST_SUFFIX
        ).write_text("{not valid json", encoding="utf-8")

        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(url, headers={"Digest": f"sha-256={SHA256_B64}"})
            ],
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"][0]["status"] == "downloaded"

    @coroutine_test
    async def test_stat_without_digests_forces_download_even_if_fresh(
        self,
    ) -> None:
        """A custom/stat result that lacks verifiable digest metadata can
        never satisfy the cache-skip requirement in verify mode."""
        url = "http://example.com/nostatdigest.bin"
        with (
            mock.patch.object(
                FSFilesStore,
                "stat_file",
                return_value={
                    "checksum": MD5_HEX,
                    "last_modified": time.time(),
                },
            ),
            mock.patch.object(
                FilesPipeline,
                "get_media_requests",
                return_value=[
                    _request(
                        url, headers={"Digest": f"sha-256={SHA256_B64}"}
                    )
                ],
            ),
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"][0]["status"] == "downloaded"

    # -- retry semantics --------------------------------------------------------

    @coroutine_test
    async def test_failure_is_not_cached_as_success_and_retries_download(
        self,
    ) -> None:
        url = "http://example.com/retry.bin"
        failing_request = _request(
            url, headers={"Digest": f"sha-256={OTHER_SHA256_B64}"}
        )
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[failing_request],
        ):
            first = await self.pipeline.process_item(_item(url))
        assert first["files"] == []
        # The failed download wrote nothing to the store.
        assert [
            path
            for path in Path(self.tempdir).rglob("*")
            if path.is_file()
        ] == []

        # A new crawl run (fresh pipeline, same store) gets a fresh download
        # attempt, which now succeeds: the failure wrote no success metadata.
        next_pipeline = self._fresh_pipeline()
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(url, headers={"Digest": f"sha-256={SHA256_B64}"})
            ],
        ):
            second = await next_pipeline.process_item(_item(url))
        assert second["files"][0]["status"] == "downloaded"
        assert second["files"][0]["digests"] == {"sha-256": SHA256_HEX}

    # -- mode is opt-in ---------------------------------------------------------

    @coroutine_test
    async def test_disabled_mode_ignores_digest_headers(self) -> None:
        pipeline = self._create_pipeline(
            FilesPipeline, {"FILES_VERIFY_DIGEST": False}
        )
        url = "http://example.com/unverified.bin"
        # A wrong digest is completely ignored when the mode is off.
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(
                    url, headers={"Digest": f"sha-256={OTHER_SHA256_B64}"}
                )
            ],
        ):
            result = await pipeline.process_item(_item(url))
        assert result["files"][0]["status"] == "downloaded"
        assert "digests" not in result["files"][0]
        stored = Path(self.tempdir) / result["files"][0]["path"]
        assert not stored.with_name(
            stored.name + FSFilesStore.DIGEST_SUFFIX
        ).exists()

    def test_default_setting_is_off(self, tmp_path: Path) -> None:
        crawler = get_crawler(DefaultSpider, {"FILES_STORE": str(tmp_path)})
        pipeline = build_from_crawler(FilesPipeline, crawler)
        assert pipeline.verify_digest is False

    def test_subclass_specific_setting_prefix(self, tmp_path: Path) -> None:
        """The setting honors the pipeline-class prefix, like the other
        FILES_* settings."""

        class MyFilesPipeline(FilesPipeline):
            pass

        crawler = get_crawler(
            DefaultSpider,
            {
                "FILES_STORE": str(tmp_path),
                "MYFILESPIPELINE_FILES_VERIFY_DIGEST": True,
            },
        )
        pipeline = build_from_crawler(MyFilesPipeline, crawler)
        assert pipeline.verify_digest is True

    @coroutine_test
    async def test_stat_file_error_falls_back_to_download(self) -> None:
        """A broken storage (stat_file raising) must not crash the pipeline;
        it falls back to a fresh, verified download."""
        url = "http://example.com/broken-stat.bin"
        with (
            mock.patch.object(
                FSFilesStore,
                "stat_file",
                side_effect=OSError("injected stat failure"),
            ),
            mock.patch.object(
                FilesPipeline,
                "get_media_requests",
                return_value=[
                    _request(
                        url, headers={"Digest": f"sha-256={SHA256_B64}"}
                    )
                ],
            ),
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"][0]["status"] == "downloaded"
        assert result["files"][0]["digests"] == {"sha-256": SHA256_HEX}

    @coroutine_test
    async def test_persist_failure_cleans_temp_and_reports_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fault injection at the storage layer: a write failure after a
        successful digest check still fails the item, leaves no temporary
        file and writes no success metadata."""
        url = "http://example.com/disk-fail.bin"

        def failing_fdopen(fd: int, *args: Any, **kwargs: Any):
            os.close(fd)
            raise OSError("injected disk failure")

        monkeypatch.setattr(os, "fdopen", failing_fdopen)
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(url, headers={"Digest": f"sha-256={SHA256_B64}"})
            ],
        ):
            result = await self.pipeline.process_item(_item(url))
        assert result["files"] == []
        assert list(Path(self.tempdir).rglob("*.tmp")) == []
        # No sidecar either: the success metadata write never happened.
        assert not list(Path(self.tempdir).rglob("*.scrapy-digests.json"))

    # -- extension points preserved ---------------------------------------------

    @coroutine_test
    async def test_custom_file_downloaded_extension_point_is_kept(self) -> None:
        """A subclass overriding file_downloaded() stays in full control; the
        pipeline does not invent digest metadata the subclass did not
        produce."""

        class CustomPipeline(FilesPipeline):
            def file_downloaded(
                self, response, request, info, *, item=None
            ) -> str:
                # Deliberately does no digest verification.
                buf = BytesIO(response.body)
                path = self.file_path(
                    request, response=response, info=info, item=item
                )
                self.store.persist_file(path, buf, info)
                return hashlib.md5(response.body).hexdigest()

        pipeline = self._create_pipeline(CustomPipeline)
        url = "http://example.com/custom.bin"
        with mock.patch.object(
            CustomPipeline,
            "get_media_requests",
            return_value=[
                _request(
                    url, headers={"Digest": f"sha-256={OTHER_SHA256_B64}"}
                )
            ],
        ):
            result = await pipeline.process_item(_item(url))
        # The subclass decides; no "digests" key is fabricated by the base
        # class, and the (wrong) header does not cause a failure.
        assert result["files"][0]["status"] == "downloaded"
        assert "digests" not in result["files"][0]

    @coroutine_test
    async def test_item_completed_contract_unchanged(self) -> None:
        url = "http://example.com/item-completed.bin"
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(url, headers={"Digest": f"sha-256={SHA256_B64}"})
            ],
        ):
            result = await self.pipeline.process_item(_item(url))
        assert set(result["files"][0]) >= {
            "url",
            "path",
            "checksum",
            "status",
        }


class TestFSFilesStoreAtomicity:
    @pytest.fixture(autouse=True)
    def _store(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.store = FSFilesStore(str(tmp_path))

    def test_persist_uses_atomic_replace(self) -> None:
        path = "full/file.bin"
        self.store.persist_file(path, BytesIO(BODY), DUMMY_SPIDER_INFO)
        absolute = self.store._get_filesystem_path(path)
        assert absolute.read_bytes() == BODY
        assert list(absolute.parent.glob("*.tmp")) == []

    def test_persist_replaces_existing_file_atomically(self) -> None:
        path = "full/file.bin"
        absolute = self.store._get_filesystem_path(path)
        self.store.persist_file(path, BytesIO(b"old content"), DUMMY_SPIDER_INFO)
        inode_before = absolute.stat().st_ino

        self.store.persist_file(path, BytesIO(BODY), DUMMY_SPIDER_INFO)

        assert absolute.read_bytes() == BODY
        assert list(absolute.parent.glob("*.tmp")) == []
        # os.replace() reuses the directory entry rather than truncating in
        # place.
        assert absolute.stat().st_ino != inode_before or os.name == "nt"

    def test_failed_write_keeps_existing_file_and_cleans_temp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = "full/file.bin"
        absolute = self.store._get_filesystem_path(path)
        self.store.persist_file(path, BytesIO(b"old content"), DUMMY_SPIDER_INFO)

        real_fdopen = os.fdopen

        def failing_fdopen(fd: int, *args: Any, **kwargs: Any):
            os.close(fd)
            raise OSError("injected disk failure")

        monkeypatch.setattr(os, "fdopen", failing_fdopen)
        with pytest.raises(OSError, match="injected disk failure"):
            self.store.persist_file(path, BytesIO(BODY), DUMMY_SPIDER_INFO)
        monkeypatch.setattr(os, "fdopen", real_fdopen)

        assert absolute.read_bytes() == b"old content"
        assert list(absolute.parent.glob("*.tmp")) == []

    def test_failed_replace_keeps_existing_file_and_cleans_temp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = "full/file.bin"
        absolute = self.store._get_filesystem_path(path)
        self.store.persist_file(path, BytesIO(b"old content"), DUMMY_SPIDER_INFO)

        def failing_replace(src: Any, dst: Any) -> None:
            raise OSError("injected replace failure")

        monkeypatch.setattr(os, "replace", failing_replace)
        with pytest.raises(OSError, match="injected replace failure"):
            self.store.persist_file(path, BytesIO(BODY), DUMMY_SPIDER_INFO)

        assert absolute.read_bytes() == b"old content"
        assert list(absolute.parent.glob("*.tmp")) == []

    def test_concurrent_persists_use_distinct_temp_files(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        path = "full/file.bin"
        absolute = self.store._get_filesystem_path(path)
        names: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
            fd, name = real_mkstemp(*args, **kwargs)
            names.append(name)
            return fd, name

        with (
            mock.patch.object(
                tempfile, "mkstemp", side_effect=tracking_mkstemp
            ),
            ThreadPoolExecutor(max_workers=8) as executor,
        ):
            futures = [
                executor.submit(
                    self.store.persist_file,
                    path,
                    BytesIO(f"content-{i}".encode()),
                    DUMMY_SPIDER_INFO,
                )
                for i in range(8)
            ]
            for future in futures:
                future.result()  # propagate any exception
        # Every concurrent call got its own unique temporary file...
        assert len(names) == 8
        assert len(set(names)) == 8
        # ...and none of them survived, while the destination exists once.
        assert list(absolute.parent.glob("*.tmp")) == []
        assert absolute.exists()

    def test_digest_sidecar_roundtrip(self) -> None:
        path = "full/file.bin"
        absolute = self.store._get_filesystem_path(path)
        self.store.persist_file(
            path,
            BytesIO(BODY),
            DUMMY_SPIDER_INFO,
            meta={"digests": {"sha-256": SHA256_HEX}},
        )
        stat = self.store.stat_file(path, DUMMY_SPIDER_INFO)
        assert stat["digests"] == {"sha-256": SHA256_HEX}
        assert stat["checksum"] == MD5_HEX

    def test_stat_ignores_corrupt_sidecar(self) -> None:
        path = "full/file.bin"
        absolute = self.store._get_filesystem_path(path)
        self.store.persist_file(path, BytesIO(BODY), DUMMY_SPIDER_INFO)
        absolute.with_name(
            absolute.name + FSFilesStore.DIGEST_SUFFIX
        ).write_text("not json", encoding="utf-8")
        stat = self.store.stat_file(path, DUMMY_SPIDER_INFO)
        assert "digests" not in stat
        assert stat["checksum"] == MD5_HEX


class TestDigestVerificationConcurrency:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        crawler = get_crawler(
            DefaultSpider,
            {"FILES_STORE": str(tmp_path), "FILES_VERIFY_DIGEST": True},
        )
        crawler.spider = crawler._create_spider()
        crawler.engine = mock.MagicMock(download_async=mocked_download_func)
        self.pipeline = build_from_crawler(FilesPipeline, crawler)
        self.pipeline.open_spider()

    @coroutine_test
    async def test_concurrent_downloads_to_distinct_paths_isolate_temp_files(
        self,
    ) -> None:
        # Distinct URLs map to distinct final paths; each concurrent download
        # owns its own temporary file and none is left behind.
        urls = [f"http://example.com/parallel-{i}.bin" for i in range(6)]
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            side_effect=lambda item, info: [
                _request(
                    item["file_urls"][0],
                    headers={"Digest": f"sha-256={SHA256_B64}"},
                )
            ],
        ):
            results = await asyncio.gather(
                *[self.pipeline.process_item(_item(url)) for url in urls]
            )
        stored_paths: set[str] = set()
        for url, result in zip(urls, results, strict=True):
            assert len(result["files"]) == 1
            stored_paths.add(result["files"][0]["path"])
            stored = self.tmp_path / result["files"][0]["path"]
            assert stored.read_bytes() == BODY
        assert len(stored_paths) == 6
        assert list(self.tmp_path.rglob("*.tmp")) == []

    @coroutine_test
    async def test_identical_concurrent_requests_share_one_download(
        self,
    ) -> None:
        # The media pipeline deduplicates identical requests by fingerprint,
        # so concurrent items requesting the same URL never overwrite each
        # other's temporary files: exactly one download reaches the store.
        url = "http://example.com/shared.bin"
        with (
            mock.patch.object(
                FilesPipeline,
                "get_media_requests",
                return_value=[
                    _request(
                        url, headers={"Digest": f"sha-256={SHA256_B64}"}
                    )
                ],
            ),
            mock.patch.object(
                FSFilesStore,
                "persist_file",
                wraps=self.pipeline.store.persist_file,
            ) as wrapped,
        ):
            await asyncio.gather(
                *[self.pipeline.process_item(_item(url)) for _ in range(6)]
            )
        assert wrapped.call_count == 1
        assert list(self.tmp_path.rglob("*.tmp")) == []
        final = self.tmp_path / self.pipeline.file_path(Request(url))
        assert final.read_bytes() == BODY

    @coroutine_test
    async def test_one_failing_concurrent_request_does_not_delete_others_temp(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed request only cleans up the temporary file it owns."""
        good_url = "http://example.com/good.bin"
        bad_url = "http://example.com/bad.bin"
        created: list[Path] = []
        real_mkstemp = tempfile.mkstemp

        def tracking_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
            fd, name = real_mkstemp(*args, **kwargs)
            created.append(Path(name))
            return fd, name

        monkeypatch.setattr(
            tempfile, "mkstemp", tracking_mkstemp
        )
        good = _request(
            good_url, headers={"Digest": f"sha-256={SHA256_B64}"}
        )
        bad = _request(
            bad_url, headers={"Digest": f"sha-256={OTHER_SHA256_B64}"}
        )
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            side_effect=lambda item, info: [good, bad],
        ):
            result = await self.pipeline.process_item(_item(good_url, bad_url))
        assert len(result["files"]) == 1
        assert result["files"][0]["url"] == good_url
        # All temporary files (from both the good and the bad attempt) are
        # gone, each request having removed only its own.
        assert all(not tmp.exists() for tmp in created)
        assert list(self.tmp_path.rglob("*.tmp")) == []
        # The good file's content and digest sidecar survived.
        good_path = self.tmp_path / self.pipeline.file_path(Request(good_url))
        assert good_path.read_bytes() == BODY
        assert good_path.with_name(
            good_path.name + FSFilesStore.DIGEST_SUFFIX
        ).exists()


class TestNonLocalStorageContract:
    """Cloud stores keep their upload semantics; digest verification happens
    on the downloaded bytes before any upload, and their stat results carry no
    verifiable digest metadata, so verify mode never skips re-downloads."""

    @coroutine_test
    async def test_verification_runs_before_cloud_upload(self, tmp_path: Path) -> None:
        crawler = get_crawler(
            DefaultSpider,
            {"FILES_STORE": str(tmp_path), "FILES_VERIFY_DIGEST": True},
        )
        crawler.spider = crawler._create_spider()
        crawler.engine = mock.MagicMock(download_async=mocked_download_func)
        pipeline = build_from_crawler(FilesPipeline, crawler)
        pipeline.open_spider()

        cloud_store = mock.MagicMock()
        cloud_store.checksum_algorithm = "md5"
        # A cloud store cannot report verifiable digest metadata.
        cloud_store.stat_file = mock.Mock(return_value={})
        pipeline.store = cloud_store

        url = "http://example.com/cloud-ok.bin"
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(url, headers={"Digest": f"sha-256={SHA256_B64}"})
            ],
        ):
            result = await pipeline.process_item(_item(url))
        assert result["files"][0]["digests"] == {"sha-256": SHA256_HEX}
        # The verified digests are handed to the store as metadata rather than
        # relying on any local rename semantics.
        _, kwargs = cloud_store.persist_file.call_args
        assert kwargs["meta"] == {"digests": {"sha-256": SHA256_HEX}}

    @coroutine_test
    async def test_cloud_upload_never_happens_on_mismatch(
        self, tmp_path: Path
    ) -> None:
        crawler = get_crawler(
            DefaultSpider,
            {"FILES_STORE": str(tmp_path), "FILES_VERIFY_DIGEST": True},
        )
        crawler.spider = crawler._create_spider()
        crawler.engine = mock.MagicMock(download_async=mocked_download_func)
        pipeline = build_from_crawler(FilesPipeline, crawler)
        pipeline.open_spider()

        cloud_store = mock.MagicMock()
        cloud_store.checksum_algorithm = "md5"
        cloud_store.stat_file = mock.Mock(return_value={})
        pipeline.store = cloud_store

        url = "http://example.com/cloud-bad.bin"
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(
                    url, headers={"Digest": f"sha-256={OTHER_SHA256_B64}"}
                )
            ],
        ):
            result = await pipeline.process_item(_item(url))
        assert result["files"] == []
        cloud_store.persist_file.assert_not_called()

    @coroutine_test
    async def test_cloud_store_without_digest_metadata_never_skips(
        self, tmp_path: Path
    ) -> None:
        crawler = get_crawler(
            DefaultSpider,
            {"FILES_STORE": str(tmp_path), "FILES_VERIFY_DIGEST": True},
        )
        crawler.spider = crawler._create_spider()
        crawler.engine = mock.MagicMock(download_async=mocked_download_func)
        pipeline = build_from_crawler(FilesPipeline, crawler)
        pipeline.open_spider()

        cloud_store = mock.MagicMock()
        cloud_store.checksum_algorithm = "md5"
        # Fresh last_modified and a checksum, but no digest metadata: not
        # enough to skip in verify mode.
        cloud_store.stat_file = mock.Mock(
            return_value={"checksum": MD5_HEX, "last_modified": time.time()}
        )
        pipeline.store = cloud_store

        url = "http://example.com/cloud-redownload.bin"
        with mock.patch.object(
            FilesPipeline,
            "get_media_requests",
            return_value=[
                _request(url, headers={"Digest": f"sha-256={SHA256_B64}"})
            ],
        ):
            result = await pipeline.process_item(_item(url))
        assert result["files"][0]["status"] == "downloaded"
        cloud_store.persist_file.assert_called_once()


class TestContentEncodingContract:
    """The digest is always checked against the exact bytes the pipeline
    stores (``response.body``). If compression middleware decoded the body,
    the Content-Encoding header is gone, so the pipeline cannot mistake an
    encoded-bytes digest for a match against decoded bytes."""

    def _pipeline(self, tmp_path: Path) -> FilesPipeline:
        crawler = get_crawler(
            DefaultSpider,
            {"FILES_STORE": str(tmp_path), "FILES_VERIFY_DIGEST": True},
        )
        crawler.spider = crawler._create_spider()
        return build_from_crawler(FilesPipeline, crawler)

    def test_verification_uses_the_stored_body_bytes(self, tmp_path: Path) -> None:
        pipeline = self._pipeline(tmp_path)
        request = Request("http://example.com/plain.bin")
        # A response the compression middleware would leave untouched: the
        # digest matches the body the pipeline will actually persist.
        response = Response(
            "http://example.com/plain.bin",
            body=BODY,
            headers={"Digest": f"sha-256={SHA256_B64}"},
        )
        digests = pipeline._verify_response_digest(response, request)
        assert digests == {"sha-256": SHA256_HEX}

    def test_digest_of_other_representation_cannot_match(
        self, tmp_path: Path
    ) -> None:
        pipeline = self._pipeline(tmp_path)
        request = Request("http://example.com/decoded.bin")
        # Simulate the post-middleware state: decoded body, no
        # Content-Encoding header, but a server digest computed over some
        # other byte representation. It must fail rather than claim success.
        response = Response(
            "http://example.com/decoded.bin",
            body=BODY,
            headers={"Digest": f"sha-256={OTHER_SHA256_B64}"},
        )
        assert "Content-Encoding" not in response.headers
        with pytest.raises(DigestVerificationError):
            pipeline._verify_response_digest(response, request)

    def test_content_md5_checked_against_body_when_header_present(
        self, tmp_path: Path
    ) -> None:
        pipeline = self._pipeline(tmp_path)
        request = Request("http://example.com/md5.bin")
        # An undecoded representation still carrying Content-Encoding: the
        # only bytes available to the pipeline are response.body, and that is
        # what the contract checks.
        response = Response(
            "http://example.com/md5.bin",
            body=BODY,
            headers={
                "Content-Encoding": "identity",
                "Content-MD5": MD5_B64,
            },
        )
        assert pipeline._verify_response_digest(response, request) == {
            "md5": MD5_HEX
        }


class TestDigestAfterCompressionMiddleware:
    """End-to-end contract: after HttpCompressionMiddleware decodes a body,
    verification runs against the decoded bytes and cannot be satisfied by a
    digest computed over the encoded representation."""

    def _pipeline(self, tmp_path: Path) -> FilesPipeline:
        crawler = get_crawler(
            DefaultSpider,
            {"FILES_STORE": str(tmp_path), "FILES_VERIFY_DIGEST": True},
        )
        crawler.spider = crawler._create_spider()
        return build_from_crawler(FilesPipeline, crawler)

    @coroutine_test
    async def test_decoded_body_requires_digest_of_decoded_bytes(
        self, tmp_path: Path
    ) -> None:
        from gzip import GzipFile

        from scrapy.downloadermiddlewares.httpcompression import (
            HttpCompressionMiddleware,
        )
        from scrapy.utils.misc import build_from_crawler as _build

        pipeline = self._pipeline(tmp_path)
        middleware = _build(HttpCompressionMiddleware, get_crawler())

        raw_buf = BytesIO()
        with GzipFile(fileobj=raw_buf, mode="wb") as gz:
            gz.write(BODY)
        encoded = raw_buf.getvalue()

        request = Request("http://example.com/encoded.bin")
        encoded_response = Response(
            "http://example.com/encoded.bin",
            body=encoded,
            headers={
                "Content-Encoding": "gzip",
                # A digest of the *encoded* bytes, as a naive server would send.
                "Digest": (
                    "sha-256="
                    + base64.b64encode(hashlib.sha256(encoded).digest()).decode()
                ),
            },
        )
        decoded = await middleware.process_response(request, encoded_response)
        # The middleware decoded the body and dropped Content-Encoding.
        assert decoded.body == BODY
        assert "Content-Encoding" not in decoded.headers

        # The encoded-bytes digest cannot validate the decoded body.
        with pytest.raises(DigestVerificationError):
            pipeline._verify_response_digest(decoded, request)

        # A digest of the decoded body (as a conforming server would provide
        # after the representation is the decoded one) verifies cleanly.
        good_response = decoded.replace(
            headers={
                **decoded.headers,
                "Digest": f"sha-256={SHA256_B64}",
            }
        )
        assert pipeline._verify_response_digest(good_response, request) == {
            "sha-256": SHA256_HEX
        }
