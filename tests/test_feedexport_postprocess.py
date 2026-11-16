from __future__ import annotations

import asyncio
import bz2
import gzip
import lzma
import marshal
import pickle
import sys
import tempfile
from io import BytesIO
from typing import IO, TYPE_CHECKING, Any, cast

import pytest

import scrapy
from scrapy import signals
from scrapy.extensions.feedexport import FeedExporter
from scrapy.extensions.postprocessing import PostProcessingManager
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test
from tests.utils.feedexport import export_by_format, export_by_path, unique_path

if TYPE_CHECKING:
    from pathlib import Path

    from tests.mockserver.http import MockServer


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


class _Calls:
    """Record the calls that the offline tests below observe, in order."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def __call__(self, name: str) -> None:
        self.names.append(name)


class _Recorder:
    """File-like object that forwards to an in-memory file and records
    writes and closes."""

    def __init__(self, calls: _Calls, name: str = "file") -> None:
        self.calls = calls
        self.name = name
        self.stream = BytesIO()
        self.close_count = 0

    def write(self, data: bytes) -> int:
        self.calls(f"{self.name}.write")
        return self.stream.write(data)

    def tell(self) -> int:
        return self.stream.tell()

    def flush(self) -> None:
        self.calls(f"{self.name}.flush")

    def close(self) -> None:
        self.close_count += 1
        self.calls(f"{self.name}.close")
        self.stream.close()

    @property
    def closed(self) -> bool:
        return self.stream.closed


class _BytesRecorder(_Recorder):
    """Recorder that keeps its content readable after it is closed."""

    def __init__(self, calls: _Calls, name: str = "file") -> None:
        super().__init__(calls, name)
        self.value = b""

    def write(self, data: bytes) -> int:
        written = super().write(data)
        self.value += bytes(data)
        return written


def _plugin(
    calls: _Calls,
    name: str,
    *,
    char: bytes = b"",
    fail_init: bool = False,
    fail_close: bool = False,
) -> type:
    """Return a plugin class that records its calls, optionally failing when
    it is initialized or closed.

    Like the built-in plugins, it does not close the target file that it
    receives, which belongs to the plugin that follows it or to the manager.
    """

    class Plugin:
        def __init__(self, file: IO[bytes], feed_options: dict[str, Any]) -> None:
            calls(f"{name}.init")
            if fail_init:
                raise RuntimeError(f"{name} init")
            self.file = file
            self.char = char

        def write(self, data: bytes) -> int:
            calls(f"{name}.write")
            return self.file.write(data) + self.file.write(self.char)

        def close(self) -> None:
            calls(f"{name}.close")
            if fail_close:
                raise RuntimeError(f"{name} close")

    Plugin.__name__ = name
    return Plugin


class TestPostProcessingManagerLifecycle:
    """Offline tests of the manager, using in-memory files and plugins."""

    @staticmethod
    def _manager(
        plugin_classes: list[Any],
        calls: _Calls,
        file: Any = None,
        **kwargs: Any,
    ) -> tuple[PostProcessingManager, Any]:
        file = file if file is not None else _Recorder(calls)
        return (
            PostProcessingManager(
                plugin_classes, cast("IO[bytes]", file), feed_options={}, **kwargs
            ),
            file,
        )

    def test_state_transitions(self) -> None:
        calls = _Calls()
        manager, file = self._manager([_plugin(calls, "a")], calls)
        assert not manager.closed

        manager.write(b"x")
        manager.close()
        assert manager.closed
        assert file.close_count == 1, "the manager owns the file by default"

    def test_write_while_closing(self) -> None:
        """A plugin that closes cannot write to the chain any more, because
        the manager is already closing it."""
        calls = _Calls()
        errors: list[Exception] = []

        class Plugin:
            def __init__(self, file, feed_options):
                self.file = file

            def write(self, data):
                return self.file.write(data)

            def close(self):
                try:
                    manager.write(b"late")
                except ValueError as exc:
                    errors.append(exc)

        manager = PostProcessingManager(
            [Plugin], cast("IO[bytes]", _Recorder(calls)), {}
        )
        manager.close()
        assert len(errors) == 1
        assert "closing" in str(errors[0])

    def test_close_order_is_head_to_tail(self) -> None:
        calls = _Calls()
        # The first plugin of the list is the head of the chain, the one that
        # receives writes, so it must be closed first for its tail to reach
        # the socket.
        manager, file = self._manager(
            [_plugin(calls, "head"), _plugin(calls, "tail")], calls
        )
        manager.close()
        assert calls.names == [
            "tail.init",
            "head.init",
            "head.close",
            "tail.close",
            "file.close",
        ]
        assert file.closed

    def test_plugins_do_not_close_the_file_they_receive(self) -> None:
        """A plugin that closes its target must not close the feed file, which
        it does not own."""
        calls = _Calls()
        manager, file = self._manager([_plugin(calls, "a")], calls)
        manager.head_plugin.file.close()  # what GzipPlugin used to do
        manager.head_plugin.file.close()
        assert not file.closed
        assert file.close_count == 0
        manager.close()
        assert file.close_count == 1

    def test_foreign_file_is_left_open(self) -> None:
        calls = _Calls()
        manager, file = self._manager([_plugin(calls, "a")], calls, _owns_file=False)
        manager.write(b"x")
        manager.close()
        assert manager.closed
        assert not file.closed
        assert file.close_count == 0

    def test_second_plugin_init_failure(self) -> None:
        """Plugins are initialized from the tail to the head, so the plugin
        built first is the one that fails here."""
        calls = _Calls()
        with pytest.raises(RuntimeError, match="a init"):
            PostProcessingManager(
                [_plugin(calls, "a", fail_init=True), _plugin(calls, "b")],
                cast("IO[bytes]", _Recorder(calls)),
                {},
            )
        # The plugin that was constructed before the failure is closed, and
        # the file is closed because the manager owns it.
        assert calls.names == ["b.init", "a.init", "b.close", "file.close"]

    def test_first_plugin_init_failure(self) -> None:
        calls = _Calls()
        with pytest.raises(RuntimeError, match="b init"):
            PostProcessingManager(
                [_plugin(calls, "a"), _plugin(calls, "b", fail_init=True)],
                cast("IO[bytes]", _Recorder(calls)),
                {},
            )
        assert calls.names == ["b.init", "file.close"]

    def test_init_failure_does_not_close_foreign_file(self) -> None:
        calls = _Calls()
        file = _Recorder(calls)
        with pytest.raises(RuntimeError, match="b init"):
            PostProcessingManager(
                [_plugin(calls, "a"), _plugin(calls, "b", fail_init=True)],
                cast("IO[bytes]", file),
                {},
                _owns_file=False,
            )
        assert not file.closed
        assert file.close_count == 0

    def test_init_failure_keeps_the_init_error_over_cleanup_errors(self) -> None:
        calls = _Calls()

        class File(_Recorder):
            def close(self):
                self.close_count += 1
                calls("file.close")
                raise OSError("file close")

        with pytest.raises(RuntimeError, match="a init") as info:
            PostProcessingManager(
                [
                    _plugin(calls, "a", fail_init=True),
                    _plugin(calls, "b", fail_close=True),
                ],
                cast("IO[bytes]", File(calls)),
                {},
            )
        # The error of the plugin that could not be initialized stays the one
        # that is raised, and the cleanup errors are attached to it.
        assert calls.names == ["b.init", "a.init", "b.close", "file.close"]
        assert info.value.__notes__ == [
            "Also failed to close plugin b: RuntimeError('b close')",
            "Also failed to close the feed file: OSError('file close')",
        ]

    def test_first_plugin_close_failure(self) -> None:
        calls = _Calls()
        manager, file = self._manager(
            [_plugin(calls, "a", fail_close=True), _plugin(calls, "b")], calls
        )
        with pytest.raises(RuntimeError, match="a close") as info:
            manager.close()
        # The remaining plugin and the file are still closed.
        assert calls.names[-3:] == ["a.close", "b.close", "file.close"]
        assert file.closed
        assert manager.closed
        assert not hasattr(info.value, "__notes__")

    def test_middle_plugin_close_failure(self) -> None:
        calls = _Calls()
        manager, file = self._manager(
            [
                _plugin(calls, "a"),
                _plugin(calls, "b", fail_close=True),
                _plugin(calls, "c"),
            ],
            calls,
        )
        with pytest.raises(RuntimeError, match="b close"):
            manager.close()
        assert calls.names[-4:] == ["a.close", "b.close", "c.close", "file.close"]
        assert file.closed

    def test_file_close_failure(self) -> None:
        calls = _Calls()

        class File(_Recorder):
            def close(self):
                self.close_count += 1
                calls("file.close")
                raise OSError("file close")

        file = File(calls)
        manager, _ = self._manager([_plugin(calls, "a")], calls, file)
        with pytest.raises(OSError, match="file close"):
            manager.close()
        assert manager.closed

    def test_all_close_failures_keep_the_first_one(self) -> None:
        calls = _Calls()

        class File(_Recorder):
            def close(self):
                self.close_count += 1
                calls("file.close")
                raise OSError("file close")

        file = File(calls)
        manager, _ = self._manager(
            [
                _plugin(calls, "a", fail_close=True),
                _plugin(calls, "b", fail_close=True),
            ],
            calls,
            file,
        )
        with pytest.raises(RuntimeError, match="a close") as info:
            manager.close()
        # Every close was attempted, in order.
        assert calls.names[-3:] == ["a.close", "b.close", "file.close"]
        notes = info.value.__notes__
        assert notes == [
            "Also failed to close plugin b: RuntimeError('b close')",
            "Also failed to close the feed file: OSError('file close')",
        ]

    def test_repeated_close_does_not_close_again(self) -> None:
        calls = _Calls()
        manager, file = self._manager([_plugin(calls, "a")], calls)
        manager.close()
        calls.names.clear()
        manager.close()
        manager.close()
        assert calls.names == []
        assert file.close_count == 1

    def test_repeated_close_does_not_write_the_tail_twice(self) -> None:
        """Closing a compression plugin writes the end of the compressed
        stream, so closing twice would corrupt it."""
        calls = _Calls()
        file = _BytesRecorder(calls)
        manager = PostProcessingManager(
            [
                "scrapy.extensions.postprocessing.GzipPlugin",
                "scrapy.extensions.postprocessing.Bz2Plugin",
            ],
            cast("IO[bytes]", file),
            {},
        )
        manager.write(b"foo\r\nbar\r\n")
        manager.close()
        compressed = file.value
        manager.close()
        assert file.value == compressed
        # The first plugin of the list writes into the second one, so the file
        # holds the bz2 stream that wraps the gzip stream.
        assert gzip.decompress(bz2.decompress(compressed)) == b"foo\r\nbar\r\n"

    def test_repeated_close_after_failure_raises_nothing(self) -> None:
        calls = _Calls()
        manager, file = self._manager(
            [_plugin(calls, "a", fail_close=True), _plugin(calls, "b")], calls
        )
        with pytest.raises(RuntimeError, match="a close"):
            manager.close()
        calls.names.clear()
        manager.close()
        assert calls.names == []
        assert file.close_count == 1

    def test_write_after_close(self) -> None:
        calls = _Calls()
        manager, _ = self._manager([_plugin(calls, "a")], calls)
        manager.close()
        with pytest.raises(ValueError, match="Cannot write to a closed"):
            manager.write(b"x")
        assert not manager.writable()
        assert "a.write" not in calls.names

    def test_write_after_failed_close(self) -> None:
        calls = _Calls()
        manager, _ = self._manager([_plugin(calls, "a", fail_close=True)], calls)
        with pytest.raises(RuntimeError):
            manager.close()
        with pytest.raises(ValueError, match="Cannot write to a failed"):
            manager.write(b"x")

    @pytest.mark.parametrize("plugin_name", ["GzipPlugin", "Bz2Plugin", "LZMAPlugin"])
    def test_real_compression_round_trip(self, plugin_name: str) -> None:
        calls = _Calls()
        plugin = f"scrapy.extensions.postprocessing.{plugin_name}"
        file = _BytesRecorder(calls)
        manager = PostProcessingManager([plugin], cast("IO[bytes]", file), {})
        manager.write(b"foo\r\nbar\r\n")
        manager.close()
        decompress = {
            "GzipPlugin": gzip.decompress,
            "Bz2Plugin": bz2.decompress,
            "LZMAPlugin": lzma.decompress,
        }[plugin_name]
        assert decompress(file.value) == b"foo\r\nbar\r\n"

    def test_owned_file_is_closed_only_after_the_plugins(self) -> None:
        """Compression plugins write their tail while closing, so the file
        must still be open and writable at that point."""
        calls = _Calls()
        file = _BytesRecorder(calls)
        manager = PostProcessingManager(
            ["scrapy.extensions.postprocessing.GzipPlugin"],
            cast("IO[bytes]", file),
            {},
        )
        manager.write(b"data")
        manager.close()
        assert calls.names.count("file.close") == 1
        assert calls.names[-1] == "file.close"
        assert calls.names[:-1].count("file.write") > 1
        assert gzip.decompress(file.value) == b"data"


class _MemoryFeedStorage:
    """Feed storage that keeps the feed in memory, so that a test can read it
    after the feed is stored."""

    contents: dict[str, bytes] = {}
    stored: list[str] = []
    opened: list[IO[bytes]] = []

    def __init__(self, uri: str, *, feed_options: dict[str, Any] | None = None):
        self.uri = uri
        self.fail = (feed_options or {}).get("fail_store", False)

    def _open(self) -> IO[bytes]:
        file = BytesIO()
        type(self).opened.append(file)
        return file

    def _store(self, file: IO[bytes]) -> None:
        type(self).stored.append(self.uri)
        try:
            file.seek(0)
            type(self).contents[self.uri] = file.read()
            if self.fail:
                raise OSError("store failed")
        finally:
            file.close()

    def open(self, spider: scrapy.Spider) -> IO[bytes]:
        return self._open()

    def store(self, file: IO[bytes]) -> None:
        self._store(file)

    @classmethod
    def reset(cls) -> None:
        cls.contents = {}
        cls.stored = []
        cls.opened = []


class _AsyncMemoryFeedStorage(_MemoryFeedStorage):
    """Memory feed storage whose ``open()`` and ``store()`` are coroutine
    functions."""

    async def open(self, spider: scrapy.Spider) -> IO[bytes]:  # type: ignore[override]
        await asyncio.sleep(0)
        return self._open()

    async def store(self, file: IO[bytes]) -> None:  # type: ignore[override]
        await asyncio.sleep(0)
        self._store(file)


class _FailingClosePlugin:
    """Plugin whose ``close()`` method raises, so that the compressed tail is
    never written."""

    def __init__(self, file, feed_options):
        self.file = file
        self.feed_options = feed_options

    def write(self, data):
        return self.file.write(data)

    def close(self):
        raise OSError("plugin close failed")


class _FailingInitPlugin:
    def __init__(self, file, feed_options):
        raise RuntimeError("plugin init failed")


class TestFeedExporterPostProcessingClose:
    """Tests of how FeedExporter handles the post-processing close, with feeds
    stored in memory instead of on a remote backend."""

    items = [{"foo": "bar"}]

    @staticmethod
    async def _export(settings: dict[str, Any]) -> Any:
        crawler = get_crawler(scrapy.Spider, settings)
        exporter = build_from_crawler(FeedExporter, crawler)
        spider = scrapy.Spider.from_crawler(crawler, "default")
        exporter.open_spider(spider)
        for item in TestFeedExporterPostProcessingClose.items:
            await exporter.item_scraped(item, spider)
        await exporter.close_spider(spider)
        return crawler.stats.get_stats()

    @pytest.fixture(autouse=True)
    def _reset_storage(self) -> None:
        _MemoryFeedStorage.reset()
        _AsyncMemoryFeedStorage.reset()

    @staticmethod
    def _settings(*feeds: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        return {
            "FEEDS": dict(feeds),
            "FEED_STORAGES": {
                "file": _MemoryFeedStorage,
                "async": _AsyncMemoryFeedStorage,
            },
        }

    @coroutine_test
    async def test_success_counts_after_a_complete_close(self) -> None:
        stats = await self._export(
            self._settings(
                (
                    "async://feed1",
                    {
                        "format": "jsonlines",
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.GzipPlugin",
                        ],
                    },
                )
            )
        )
        assert stats["feedexport/success_count/_AsyncMemoryFeedStorage"] == 1
        assert gzip.decompress(_AsyncMemoryFeedStorage.contents["async://feed1"]) == (
            b'{"foo": "bar"}\n'
        )

    @coroutine_test
    async def test_plugin_close_failure_fails_the_feed(self) -> None:
        stats = await self._export(
            self._settings(
                (
                    "async://feed1",
                    {
                        "format": "jsonlines",
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.GzipPlugin",
                            _FailingClosePlugin,
                        ],
                    },
                )
            )
        )
        assert stats["feedexport/failed_count/_AsyncMemoryFeedStorage"] == 1
        assert "feedexport/success_count/_AsyncMemoryFeedStorage" not in stats
        # The feed was never handed over to the storage.
        assert _AsyncMemoryFeedStorage.stored == []

    @coroutine_test
    async def test_plugin_init_failure_fails_the_feed(self) -> None:
        stats = await self._export(
            self._settings(
                (
                    "async://feed1",
                    {
                        "format": "jsonlines",
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.GzipPlugin",
                            _FailingInitPlugin,
                        ],
                    },
                )
            )
        )
        assert stats["feedexport/failed_count/_AsyncMemoryFeedStorage"] == 1
        assert "feedexport/success_count/_AsyncMemoryFeedStorage" not in stats
        assert _AsyncMemoryFeedStorage.stored == []

    @coroutine_test
    async def test_async_store_failure_fails_the_feed(self) -> None:
        stats = await self._export(
            self._settings(
                (
                    "async://feed1",
                    {
                        "format": "jsonlines",
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.GzipPlugin",
                        ],
                        "fail_store": True,
                    },
                )
            )
        )
        assert stats["feedexport/failed_count/_AsyncMemoryFeedStorage"] == 1
        assert "feedexport/success_count/_AsyncMemoryFeedStorage" not in stats

    @coroutine_test
    async def test_one_feed_failure_does_not_block_the_other(self) -> None:
        """A feed whose plugin fails to close must not prevent the other feeds
        from being stored, and their decompressed content must be unchanged."""
        stats = await self._export(
            self._settings(
                (
                    "async://broken",
                    {
                        "format": "jsonlines",
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.GzipPlugin",
                            _FailingClosePlugin,
                        ],
                    },
                ),
                (
                    "async://gz",
                    {
                        "format": "jsonlines",
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.GzipPlugin",
                        ],
                    },
                ),
                (
                    "async://bz2",
                    {
                        "format": "jsonlines",
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.Bz2Plugin",
                        ],
                    },
                ),
                (
                    "async://lzma",
                    {
                        "format": "jsonlines",
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.LZMAPlugin",
                        ],
                    },
                ),
                ("async://plain", {"format": "jsonlines"}),
            )
        )
        assert stats["feedexport/failed_count/_AsyncMemoryFeedStorage"] == 1
        assert stats["feedexport/success_count/_AsyncMemoryFeedStorage"] == 4
        assert gzip.decompress(_AsyncMemoryFeedStorage.contents["async://gz"]) == (
            b'{"foo": "bar"}\n'
        )
        assert bz2.decompress(_AsyncMemoryFeedStorage.contents["async://bz2"]) == (
            b'{"foo": "bar"}\n'
        )
        assert lzma.decompress(_AsyncMemoryFeedStorage.contents["async://lzma"]) == (
            b'{"foo": "bar"}\n'
        )
        assert _AsyncMemoryFeedStorage.contents["async://plain"] == b'{"foo": "bar"}\n'

    @coroutine_test
    async def test_init_failure_does_not_block_the_other_feed(self) -> None:
        """A feed whose plugin fails to initialize must not stop the items from
        reaching the other feeds."""
        stats = await self._export(
            self._settings(
                (
                    "async://broken",
                    {
                        "format": "jsonlines",
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.GzipPlugin",
                            _FailingInitPlugin,
                        ],
                    },
                ),
                (
                    "async://gz",
                    {
                        "format": "jsonlines",
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.GzipPlugin",
                        ],
                    },
                ),
            )
        )
        assert stats["feedexport/failed_count/_AsyncMemoryFeedStorage"] == 1
        assert stats["feedexport/success_count/_AsyncMemoryFeedStorage"] == 1
        assert gzip.decompress(_AsyncMemoryFeedStorage.contents["async://gz"]) == (
            b'{"foo": "bar"}\n'
        )
        assert "async://broken" not in _AsyncMemoryFeedStorage.contents

    @coroutine_test
    async def test_storage_failure_that_leaves_the_file_open(self) -> None:
        """The exporter releases the file when the storage does not, so that a
        failed feed does not leak a file descriptor."""

        class Storage(_MemoryFeedStorage):
            def store(self, file: IO[bytes]) -> None:
                # Unlike a well-behaved storage, this one fails without
                # closing the file.
                type(self).stored.append(self.uri)
                raise OSError("store failed")

        crawler = get_crawler(
            scrapy.Spider,
            {
                "FEEDS": {
                    "async://feed1": {
                        "format": "jsonlines",
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.GzipPlugin",
                        ],
                    },
                },
                "FEED_STORAGES": {"async": Storage},
            },
        )
        exporter = build_from_crawler(FeedExporter, crawler)
        spider = scrapy.Spider.from_crawler(crawler, "default")
        exporter.open_spider(spider)
        await exporter.item_scraped(self.items[0], spider)
        await exporter.close_spider(spider)
        stats = crawler.stats.get_stats()
        assert stats["feedexport/failed_count/Storage"] == 1
        file = Storage.opened[0]
        assert file.closed

    @coroutine_test
    async def test_stdout_failed_feed_keeps_stdout_open(self) -> None:
        """A failed feed must not close a standard stream, which the rest of
        the process still writes to."""
        stdout = sys.stdout.buffer
        crawler = get_crawler(
            scrapy.Spider,
            {
                "FEEDS": {
                    "stdout:": {
                        "format": "jsonlines",
                        "postprocessing": [_FailingClosePlugin],
                    },
                },
            },
        )
        exporter = build_from_crawler(FeedExporter, crawler)
        spider = scrapy.Spider.from_crawler(crawler, "default")
        exporter.open_spider(spider)
        await exporter.item_scraped(self.items[0], spider)
        await exporter.close_spider(spider)
        assert not stdout.closed
        assert crawler.stats.get_value("feedexport/failed_count/StdoutFeedStorage") == 1

    @coroutine_test
    async def test_failure_sends_the_slot_closed_signal(self) -> None:
        crawler = get_crawler(
            scrapy.Spider,
            self._settings(
                (
                    "async://feed1",
                    {
                        "format": "jsonlines",
                        "postprocessing": [_FailingClosePlugin],
                    },
                )
            ),
        )
        exporter = build_from_crawler(FeedExporter, crawler)
        spider = scrapy.Spider.from_crawler(crawler, "default")
        closed_slots = []

        def handler(slot):
            closed_slots.append(slot)

        crawler.signals.connect(handler, signal=signals.feed_slot_closed)
        exporter.open_spider(spider)
        await exporter.item_scraped(self.items[0], spider)
        await exporter.close_spider(spider)
        assert len(closed_slots) == 1

    @coroutine_test
    async def test_failed_feed_is_retried_on_the_next_batch(self) -> None:
        """The failure of one batch does not prevent the next batch from being
        stored."""
        crawler = get_crawler(
            scrapy.Spider,
            self._settings(
                (
                    "async://feed-%(batch_id)d",
                    {
                        "format": "jsonlines",
                        "batch_item_count": 1,
                        "postprocessing": [
                            "scrapy.extensions.postprocessing.GzipPlugin",
                        ],
                    },
                )
            ),
        )
        exporter = build_from_crawler(FeedExporter, crawler)
        spider = scrapy.Spider.from_crawler(crawler, "default")
        exporter.open_spider(spider)
        for item in ({"foo": "bar1"}, {"foo": "bar2"}):
            await exporter.item_scraped(item, spider)
        await exporter.close_spider(spider)
        stats = crawler.stats.get_stats()
        assert stats["feedexport/success_count/_AsyncMemoryFeedStorage"] == 2
        assert gzip.decompress(_AsyncMemoryFeedStorage.contents["async://feed-1"]) == (
            b'{"foo": "bar1"}\n'
        )
        assert gzip.decompress(_AsyncMemoryFeedStorage.contents["async://feed-2"]) == (
            b'{"foo": "bar2"}\n'
        )
