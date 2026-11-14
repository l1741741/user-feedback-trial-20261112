from __future__ import annotations

import bz2
import gzip
import lzma
import marshal
import os
import pickle
import tempfile
import time
from io import BytesIO
from typing import IO, TYPE_CHECKING
from urllib.parse import urljoin

import pytest

from scrapy.extensions.postprocessing import GzipPlugin, PostProcessingManager
from tests.utils.decorators import coroutine_test
from tests.utils.feedexport import (
    crawl_items,
    export_by_format,
    export_by_path,
    unique_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.mockserver.http import MockServer


class NamedBytesIO(BytesIO):
    """BytesIO with a ``name`` attribute, like a real file object."""

    def __init__(self, name: str = ""):
        super().__init__()
        self.name = name


class FailingStream:
    """Wraps a binary stream and raises on write once ``fail`` is set."""

    def __init__(self, wrapped: BytesIO):
        self.wrapped = wrapped
        self.fail = False

    def write(self, data: bytes) -> int:
        if self.fail:
            raise OSError("simulated downstream write failure")
        return self.wrapped.write(data)

    def flush(self) -> None:
        self.wrapped.flush()


def gzip_header(data: bytes) -> dict:
    return {
        "magic": data[:2],
        "compression_method": data[2],
        "flags": data[3],
        "mtime": int.from_bytes(data[4:8], "little"),
    }


class TestGzipPluginReproducible:
    items = [{"foo": "bar"}]
    expected = b"foo\r\nbar\r\n"

    def make_plugin(self, stream=None, **feed_options):
        return GzipPlugin(stream if stream is not None else BytesIO(), feed_options)

    def compress(self, data_chunks, **feed_options) -> bytes:
        stream = BytesIO()
        plugin = self.make_plugin(stream, **feed_options)
        for chunk in data_chunks:
            plugin.write(chunk)
        plugin.close()
        return stream.getvalue()

    def test_same_content_same_bytes(self):
        first = self.compress([self.expected], gzip_reproducible=True)
        second = self.compress([self.expected], gzip_reproducible=True)
        assert first == second

    def test_header_metadata_pinned(self):
        data = self.compress([self.expected], gzip_reproducible=True)
        header = gzip_header(data)
        assert header["magic"] == b"\x1f\x8b"
        assert header["compression_method"] == 8
        assert header["mtime"] == 0
        assert not header["flags"] & 0x08  # no FNAME field

    def test_roundtrip_decompression(self):
        data = self.compress([self.expected], gzip_reproducible=True)
        assert gzip.decompress(data) == self.expected

    def test_different_content_differs(self):
        first = self.compress([b"foo"], gzip_reproducible=True)
        second = self.compress([b"bar"], gzip_reproducible=True)
        assert first != second
        assert gzip.decompress(first) == b"foo"
        assert gzip.decompress(second) == b"bar"

    def test_chunked_and_oneshot_writes_match(self):
        chunks = [self.expected[:3], self.expected[3:5], self.expected[5:]]
        chunked = self.compress(chunks, gzip_reproducible=True)
        oneshot = self.compress([self.expected], gzip_reproducible=True)
        assert chunked == oneshot
        assert gzip.decompress(chunked) == self.expected

    def test_empty_feed(self):
        data = self.compress([], gzip_reproducible=True)
        assert gzip_header(data)["mtime"] == 0
        assert gzip.decompress(data) == b""

    def test_compresslevel_still_respected(self):
        data = self.compress(
            [self.expected], gzip_reproducible=True, gzip_compresslevel=1
        )
        reference_stream = BytesIO()
        with gzip.GzipFile(
            fileobj=reference_stream, mode="wb", compresslevel=1, mtime=0, filename=""
        ) as reference_file:
            reference_file.write(self.expected)
        assert data == reference_stream.getvalue()
        assert data != self.compress(
            [self.expected], gzip_reproducible=True, gzip_compresslevel=9
        )

    def test_explicit_mtime_and_filename_override_pinned_defaults(self):
        data = self.compress(
            [self.expected],
            gzip_reproducible=True,
            gzip_mtime=123456789,
            gzip_filename="FILE1",
        )
        header = gzip_header(data)
        assert header["mtime"] == 123456789
        assert header["flags"] & 0x08
        assert b"FILE1\x00" in data
        assert gzip.decompress(data) == self.expected

    def test_default_behavior_not_pinned(self):
        stream = NamedBytesIO(name="/tmp/feed.csv")
        plugin = self.make_plugin(stream)
        before = int(time.time())
        plugin.write(self.expected)
        plugin.close()
        after = int(time.time())
        data = stream.getvalue()
        header = gzip_header(data)
        assert before <= header["mtime"] <= after
        assert header["flags"] & 0x08  # FNAME taken from the file object name
        assert b"feed.csv\x00" in data
        assert gzip.decompress(data) == self.expected

    def test_close_does_not_close_underlying_stream(self):
        stream = BytesIO()
        plugin = self.make_plugin(stream, gzip_reproducible=True)
        plugin.write(self.expected)
        plugin.close()
        assert not stream.closed
        assert gzip.decompress(stream.getvalue()) == self.expected

    def test_repeated_close(self):
        stream = BytesIO()
        plugin = self.make_plugin(stream, gzip_reproducible=True)
        plugin.write(self.expected)
        plugin.close()
        data = stream.getvalue()
        plugin.close()
        assert stream.getvalue() == data
        assert gzip.decompress(data) == self.expected

    def test_flush_then_close(self):
        stream = BytesIO()
        plugin = self.make_plugin(stream, gzip_reproducible=True)
        plugin.write(self.expected)
        plugin.flush()
        # data is streamed, not buffered in the plugin
        assert len(stream.getvalue()) > 0
        plugin.close()
        assert gzip.decompress(stream.getvalue()) == self.expected

    def test_downstream_write_error_propagates_on_flush(self):
        failing = FailingStream(BytesIO())
        plugin = self.make_plugin(failing, gzip_reproducible=True)
        # small writes sit in the gzip buffer, the failure surfaces on flush
        plugin.write(self.expected)
        failing.fail = True
        with pytest.raises(OSError, match="simulated downstream write failure"):
            plugin.flush()

    def test_downstream_write_error_propagates_on_large_write(self):
        failing = FailingStream(BytesIO())
        plugin = self.make_plugin(failing, gzip_reproducible=True)
        failing.fail = True
        # a payload larger than the gzip write buffer is written through
        with pytest.raises(OSError, match="simulated downstream write failure"):
            plugin.write(os.urandom(1024 * 1024))

    def test_cross_instance_isolation(self):
        stream_a, stream_b = BytesIO(), BytesIO()
        plugin_a = self.make_plugin(stream_a, gzip_reproducible=True)
        plugin_b = self.make_plugin(stream_b, gzip_reproducible=True)
        plugin_a.write(b"aaa")
        plugin_b.write(b"bbb")
        plugin_a.write(b"ccc")
        plugin_b.write(b"ddd")
        plugin_a.close()
        plugin_b.close()
        assert gzip.decompress(stream_a.getvalue()) == b"aaaccc"
        assert gzip.decompress(stream_b.getvalue()) == b"bbbddd"
        assert stream_a.getvalue() != stream_b.getvalue()


class TestFeedPostProcessedExports:
    items = [{"foo": "bar"}]
    expected = b"foo\r\nbar\r\n"

    class MyPlugin1:
        def __init__(self, file, feed_options):
            self.file = file
            self.feed_options = feed_options
            self.char = self.feed_options.get("plugin1_char", b"")

        def write(self, data):
            written_count = self.file.write(data)
            written_count += self.file.write(self.char)
            return written_count

        def close(self):
            self.file.close()

    def get_gzip_compressed(
        self,
        data: bytes,
        compresslevel: int = 9,
        mtime: int = 0,
        filename: str = "",
    ) -> bytes:
        data_stream = BytesIO()
        gzipf = gzip.GzipFile(
            fileobj=data_stream,
            filename=filename,
            mtime=mtime,
            compresslevel=compresslevel,
            mode="wb",
        )
        gzipf.write(data)
        gzipf.close()
        data_stream.seek(0)
        return data_stream.read()

    def test_tell_reports_target_file_position(self):
        """Exporters that wrap the file they get, e.g. through
        :class:`io.TextIOWrapper`, need it to report a position."""
        file = BytesIO()
        manager = PostProcessingManager([self.MyPlugin1], file, {})
        assert manager.tell() == 0
        manager.write(b"foo")
        assert manager.tell() == file.tell() == 3

    @coroutine_test
    async def test_gzip_plugin(self, mockserver: MockServer, tmp_path: Path) -> None:
        filename = tmp_path / "gzip_file"

        settings = {
            "FEEDS": {
                filename: {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.GzipPlugin"],
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)
        try:
            gzip.decompress(data[filename])
        except OSError:
            pytest.fail("Received invalid gzip data.")

    @coroutine_test
    async def test_gzip_plugin_compresslevel(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        filename_to_compressed = {
            tmp_path / "compresslevel_0": self.get_gzip_compressed(
                self.expected, compresslevel=0
            ),
            tmp_path / "compresslevel_9": self.get_gzip_compressed(
                self.expected, compresslevel=9
            ),
        }

        settings = {
            "FEEDS": {
                tmp_path / "compresslevel_0": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.GzipPlugin"],
                    "gzip_compresslevel": 0,
                    "gzip_mtime": 0,
                    "gzip_filename": "",
                },
                tmp_path / "compresslevel_9": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.GzipPlugin"],
                    "gzip_compresslevel": 9,
                    "gzip_mtime": 0,
                    "gzip_filename": "",
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)

        for filename, compressed in filename_to_compressed.items():
            result = gzip.decompress(data[filename])
            assert compressed == data[filename]
            assert result == self.expected

    @coroutine_test
    async def test_gzip_plugin_mtime(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        filename_to_compressed = {
            tmp_path / "mtime_123": self.get_gzip_compressed(self.expected, mtime=123),
            tmp_path / "mtime_123456789": self.get_gzip_compressed(
                self.expected, mtime=123456789
            ),
        }

        settings = {
            "FEEDS": {
                tmp_path / "mtime_123": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.GzipPlugin"],
                    "gzip_mtime": 123,
                    "gzip_filename": "",
                },
                tmp_path / "mtime_123456789": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.GzipPlugin"],
                    "gzip_mtime": 123456789,
                    "gzip_filename": "",
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)

        for filename, compressed in filename_to_compressed.items():
            result = gzip.decompress(data[filename])
            assert compressed == data[filename]
            assert result == self.expected

    @coroutine_test
    async def test_gzip_plugin_filename(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        filename_to_compressed = {
            tmp_path / "filename_FILE1": self.get_gzip_compressed(
                self.expected, filename="FILE1"
            ),
            tmp_path / "filename_FILE2": self.get_gzip_compressed(
                self.expected, filename="FILE2"
            ),
        }

        settings = {
            "FEEDS": {
                tmp_path / "filename_FILE1": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.GzipPlugin"],
                    "gzip_mtime": 0,
                    "gzip_filename": "FILE1",
                },
                tmp_path / "filename_FILE2": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.GzipPlugin"],
                    "gzip_mtime": 0,
                    "gzip_filename": "FILE2",
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)

        for filename, compressed in filename_to_compressed.items():
            result = gzip.decompress(data[filename])
            assert compressed == data[filename]
            assert result == self.expected

    @coroutine_test
    async def test_gzip_plugin_reproducible_batched_exports(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        items = [{"foo": f"value{i}"} for i in range(4)]

        async def export_once(dirname: str) -> list[bytes]:
            feed_dir = tmp_path / dirname
            feed_dir.mkdir()
            feed_path = feed_dir / "feed_%(batch_id)02d"
            settings = {
                "FEEDS": {
                    urljoin("file:", str(feed_path)): {
                        "format": "jsonlines",
                        "batch_item_count": 2,
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.GzipPlugin"
                        ],
                        "gzip_reproducible": True,
                    },
                },
            }
            await crawl_items(mockserver, items, settings)
            return [p.read_bytes() for p in sorted(feed_dir.iterdir())]

        first = await export_once("run1")
        second = await export_once("run2")

        assert len(first) == 2
        # same items, settings and batching produce identical compressed bytes
        assert first == second
        # batches hold different content and remain distinguishable
        assert first[0] != first[1]
        assert gzip.decompress(first[0]) == b'{"foo": "value0"}\n{"foo": "value1"}\n'
        assert gzip.decompress(first[1]) == b'{"foo": "value2"}\n{"foo": "value3"}\n'

    @coroutine_test
    async def test_lzma_plugin(self, mockserver: MockServer, tmp_path: Path) -> None:
        filename = tmp_path / "lzma_file"

        settings = {
            "FEEDS": {
                filename: {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.LZMAPlugin"],
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)
        try:
            lzma.decompress(data[filename])
        except lzma.LZMAError:
            pytest.fail("Received invalid lzma data.")

    @coroutine_test
    async def test_lzma_plugin_format(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        filename_to_compressed = {
            tmp_path / "format_FORMAT_XZ": lzma.compress(
                self.expected, format=lzma.FORMAT_XZ
            ),
            tmp_path / "format_FORMAT_ALONE": lzma.compress(
                self.expected, format=lzma.FORMAT_ALONE
            ),
        }

        settings = {
            "FEEDS": {
                tmp_path / "format_FORMAT_XZ": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.LZMAPlugin"],
                    "lzma_format": lzma.FORMAT_XZ,
                },
                tmp_path / "format_FORMAT_ALONE": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.LZMAPlugin"],
                    "lzma_format": lzma.FORMAT_ALONE,
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)

        for filename, compressed in filename_to_compressed.items():
            result = lzma.decompress(data[filename])
            assert compressed == data[filename]
            assert result == self.expected

    @coroutine_test
    async def test_lzma_plugin_check(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        filename_to_compressed = {
            tmp_path / "check_CHECK_NONE": lzma.compress(
                self.expected, check=lzma.CHECK_NONE
            ),
            tmp_path / "CHECK_SHA256": lzma.compress(
                self.expected, check=lzma.CHECK_SHA256
            ),
        }

        settings = {
            "FEEDS": {
                tmp_path / "check_CHECK_NONE": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.LZMAPlugin"],
                    "lzma_check": lzma.CHECK_NONE,
                },
                tmp_path / "CHECK_SHA256": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.LZMAPlugin"],
                    "lzma_check": lzma.CHECK_SHA256,
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)

        for filename, compressed in filename_to_compressed.items():
            result = lzma.decompress(data[filename])
            assert compressed == data[filename]
            assert result == self.expected

    @coroutine_test
    async def test_lzma_plugin_preset(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        filename_to_compressed = {
            tmp_path / "preset_PRESET_0": lzma.compress(self.expected, preset=0),
            tmp_path / "preset_PRESET_9": lzma.compress(self.expected, preset=9),
        }

        settings = {
            "FEEDS": {
                tmp_path / "preset_PRESET_0": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.LZMAPlugin"],
                    "lzma_preset": 0,
                },
                tmp_path / "preset_PRESET_9": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.LZMAPlugin"],
                    "lzma_preset": 9,
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)

        for filename, compressed in filename_to_compressed.items():
            result = lzma.decompress(data[filename])
            assert compressed == data[filename]
            assert result == self.expected

    @coroutine_test
    async def test_lzma_plugin_filters(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        filters = [{"id": lzma.FILTER_LZMA2}]
        compressed = lzma.compress(self.expected, filters=filters)
        filename = tmp_path / "filters"

        settings = {
            "FEEDS": {
                filename: {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.LZMAPlugin"],
                    "lzma_filters": filters,
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)
        assert compressed == data[filename]
        result = lzma.decompress(data[filename])
        assert result == self.expected

    @coroutine_test
    async def test_bz2_plugin(self, mockserver: MockServer, tmp_path: Path) -> None:
        filename = tmp_path / "bz2_file"

        settings = {
            "FEEDS": {
                filename: {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.Bz2Plugin"],
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)
        try:
            bz2.decompress(data[filename])
        except OSError:
            pytest.fail("Received invalid bz2 data.")

    @coroutine_test
    async def test_bz2_plugin_compresslevel(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        filename_to_compressed = {
            tmp_path / "compresslevel_1": bz2.compress(self.expected, compresslevel=1),
            tmp_path / "compresslevel_9": bz2.compress(self.expected, compresslevel=9),
        }

        settings = {
            "FEEDS": {
                tmp_path / "compresslevel_1": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.Bz2Plugin"],
                    "bz2_compresslevel": 1,
                },
                tmp_path / "compresslevel_9": {
                    "format": "csv",
                    "postprocessing": ["scrapy.extensions.postprocessing.Bz2Plugin"],
                    "bz2_compresslevel": 9,
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)

        for filename, compressed in filename_to_compressed.items():
            result = bz2.decompress(data[filename])
            assert compressed == data[filename]
            assert result == self.expected

    @coroutine_test
    async def test_custom_plugin(self, mockserver: MockServer, tmp_path: Path) -> None:
        filename = tmp_path / "csv_file"

        settings = {
            "FEEDS": {
                filename: {
                    "format": "csv",
                    "postprocessing": [self.MyPlugin1],
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)
        assert data[filename] == self.expected

    @coroutine_test
    async def test_custom_plugin_with_parameter(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        expected = b"foo\r\n\nbar\r\n\n"
        filename = tmp_path / "newline"

        settings = {
            "FEEDS": {
                filename: {
                    "format": "csv",
                    "postprocessing": [self.MyPlugin1],
                    "plugin1_char": b"\n",
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)
        assert data[filename] == expected

    @coroutine_test
    async def test_custom_plugin_with_compression(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        expected = b"foo\r\n\nbar\r\n\n"

        filename_to_decompressor = {
            tmp_path / "bz2": bz2.decompress,
            tmp_path / "lzma": lzma.decompress,
            tmp_path / "gzip": gzip.decompress,
        }

        settings = {
            "FEEDS": {
                tmp_path / "bz2": {
                    "format": "csv",
                    "postprocessing": [
                        self.MyPlugin1,
                        "scrapy.extensions.postprocessing.Bz2Plugin",
                    ],
                    "plugin1_char": b"\n",
                },
                tmp_path / "lzma": {
                    "format": "csv",
                    "postprocessing": [
                        self.MyPlugin1,
                        "scrapy.extensions.postprocessing.LZMAPlugin",
                    ],
                    "plugin1_char": b"\n",
                },
                tmp_path / "gzip": {
                    "format": "csv",
                    "postprocessing": [
                        self.MyPlugin1,
                        "scrapy.extensions.postprocessing.GzipPlugin",
                    ],
                    "plugin1_char": b"\n",
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)

        for filename, decompressor in filename_to_decompressor.items():
            result = decompressor(data[filename])
            assert result == expected

    @coroutine_test
    async def test_exports_compatibility_with_postproc(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        filename_to_expected = {
            tmp_path / "csv": b"foo\r\nbar\r\n",
            tmp_path / "json": b'[\n{"foo": "bar"}\n]',
            tmp_path / "jsonlines": b'{"foo": "bar"}\n',
            tmp_path / "xml": b'<?xml version="1.0" encoding="utf-8"?>\n'
            b"<items>\n<item><foo>bar</foo></item>\n</items>",
        }

        settings = {
            "FEEDS": {
                tmp_path / "csv": {
                    "format": "csv",
                    "postprocessing": [self.MyPlugin1],
                    # empty plugin to activate postprocessing.PostProcessingManager
                },
                tmp_path / "json": {
                    "format": "json",
                    "postprocessing": [self.MyPlugin1],
                },
                tmp_path / "jsonlines": {
                    "format": "jsonlines",
                    "postprocessing": [self.MyPlugin1],
                },
                tmp_path / "xml": {
                    "format": "xml",
                    "postprocessing": [self.MyPlugin1],
                },
                tmp_path / "marshal": {
                    "format": "marshal",
                    "postprocessing": [self.MyPlugin1],
                },
                tmp_path / "pickle": {
                    "format": "pickle",
                    "postprocessing": [self.MyPlugin1],
                },
            },
        }

        data = await export_by_path(mockserver, self.items, settings)

        for path, expected in filename_to_expected.items():
            assert data[path] == expected
        assert pickle.loads(data[tmp_path / "pickle"]) == self.items[0]
        assert marshal.loads(data[tmp_path / "marshal"]) == self.items[0]


@coroutine_test
async def test_storage_file_no_postprocessing(
    mockserver: MockServer, tmp_path: Path
) -> None:
    class Storage:
        open_file: IO[bytes]
        store_file: IO[bytes]

        def __init__(self, uri, *, feed_options=None):
            pass

        def open(self, spider):
            Storage.open_file = tempfile.NamedTemporaryFile(prefix="feed-")
            return Storage.open_file

        def store(self, file):
            Storage.store_file = file
            file.close()

    settings = {
        "FEEDS": {unique_path(tmp_path): {"format": "jsonlines"}},
        "FEED_STORAGES": {"file": Storage},
    }
    await export_by_format(mockserver, [], settings)
    assert Storage.open_file is Storage.store_file


@coroutine_test
async def test_storage_file_postprocessing(
    mockserver: MockServer, tmp_path: Path
) -> None:
    class Storage:
        open_file: IO[bytes]
        store_file: IO[bytes]
        file_was_closed: bool

        def __init__(self, uri, *, feed_options=None):
            pass

        def open(self, spider):
            Storage.open_file = tempfile.NamedTemporaryFile(prefix="feed-")
            return Storage.open_file

        def store(self, file):
            Storage.store_file = file
            Storage.file_was_closed = file.closed
            file.close()

    settings = {
        "FEEDS": {
            unique_path(tmp_path): {
                "format": "jsonlines",
                "postprocessing": [
                    "scrapy.extensions.postprocessing.GzipPlugin",
                ],
            },
        },
        "FEED_STORAGES": {"file": Storage},
    }
    await export_by_format(mockserver, [], settings)
    assert Storage.open_file is Storage.store_file
    assert not Storage.file_was_closed
