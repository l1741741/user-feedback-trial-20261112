"""
Extension for processing data before they are exported to feeds.
"""

from bz2 import BZ2File
from gzip import GzipFile
from io import IOBase
from lzma import LZMAFile
from typing import IO, Any, BinaryIO, cast

from scrapy.utils.misc import load_object


class GzipPlugin:
    """
    Compresses received data using `gzip <https://en.wikipedia.org/wiki/Gzip>`_.

    Accepted ``feed_options`` parameters:

    - `gzip_compresslevel`
    - `gzip_mtime`
    - `gzip_filename`

    See :py:class:`gzip.GzipFile` for more info about parameters.
    """

    def __init__(self, file: BinaryIO, feed_options: dict[str, Any]) -> None:
        self.file = file
        self.feed_options = feed_options
        compress_level = self.feed_options.get("gzip_compresslevel", 9)
        mtime = self.feed_options.get("gzip_mtime")
        filename = self.feed_options.get("gzip_filename")
        self.gzipfile = GzipFile(
            fileobj=self.file,
            mode="wb",
            compresslevel=compress_level,
            mtime=mtime,
            filename=filename,
        )

    def write(self, data: bytes) -> int:
        return self.gzipfile.write(data)

    def close(self) -> None:
        self.gzipfile.close()


class Bz2Plugin:
    """
    Compresses received data using `bz2 <https://en.wikipedia.org/wiki/Bzip2>`_.

    Accepted ``feed_options`` parameters:

    - `bz2_compresslevel`

    See :py:class:`bz2.BZ2File` for more info about parameters.
    """

    def __init__(self, file: BinaryIO, feed_options: dict[str, Any]) -> None:
        self.file = file
        self.feed_options = feed_options
        compress_level = self.feed_options.get("bz2_compresslevel", 9)
        self.bz2file = BZ2File(
            filename=self.file, mode="wb", compresslevel=compress_level
        )

    def write(self, data: bytes) -> int:
        return self.bz2file.write(data)

    def close(self) -> None:
        self.bz2file.close()


class LZMAPlugin:
    """
    Compresses received data using `lzma <https://en.wikipedia.org/wiki/Lempel–Ziv–Markov_chain_algorithm>`_.

    Accepted ``feed_options`` parameters:

    - `lzma_format`
    - `lzma_check`
    - `lzma_preset`
    - `lzma_filters`

    See :py:class:`lzma.LZMAFile` for more info about parameters.
    """

    def __init__(self, file: BinaryIO, feed_options: dict[str, Any]) -> None:
        self.file = file
        self.feed_options = feed_options

        format_ = self.feed_options.get("lzma_format")
        check = self.feed_options.get("lzma_check", -1)
        preset = self.feed_options.get("lzma_preset")
        filters = self.feed_options.get("lzma_filters")
        self.lzmafile = LZMAFile(
            filename=self.file,
            mode="wb",
            format=format_,
            check=check,
            preset=preset,
            filters=filters,
        )

    def write(self, data: bytes) -> int:
        return self.lzmafile.write(data)

    def close(self) -> None:
        self.lzmafile.close()


class _PluginTarget:
    """File-like object that plugins receive as their target file.

    Everything is forwarded to the feed file except :meth:`close`, which does
    nothing: closing the target is up to the manager, once every plugin has
    been closed. Plugins that close their target, as the documentation used to
    ask them to, therefore do not close a file that they do not own.
    """

    def __init__(self, file: IO[bytes]) -> None:
        self._file = file

    @property
    def closed(self) -> bool:
        return self._file.closed

    def write(self, data: bytes) -> int:
        return self._file.write(data)

    def tell(self) -> int:
        return self._file.tell()

    def flush(self) -> None:
        self._file.flush()

    def writable(self) -> bool:
        return True

    def close(self) -> None:
        """Do nothing: the feed file is closed by its owner."""

    def __getattr__(self, name: str) -> Any:
        if name == "_file":
            raise AttributeError(name)
        return getattr(self._file, name)


# io.IOBase is subclassed here, so that exporters can use the PostProcessingManager
# instance as a file like writable object. This could be needed by some exporters
# such as CsvItemExporter which wraps the feed storage with io.TextIOWrapper.
class PostProcessingManager(IOBase):
    """Manage the post-processing plugins of a feed.

    Plugins are initialized from the last one in *plugins* to the first one,
    since each plugin receives the output of the plugin that follows it. The
    first plugin is therefore the head of the chain, the one that receives
    writes, and *file* is its tail.

    Plugins never close their target. The manager owns *file* and closes it
    after all the plugins, as the last step of :meth:`close`, unless it is
    told otherwise: a manager that does not own *file* leaves it open on every
    path, including initialization and close failures, so that whoever created
    it can still read and close it. The feed exporter uses that mode, since
    the feed storage reads *file* to deliver the feed and closes it
    afterwards.

    Internally the manager goes through these states:

    -   ``"open"``: it accepts writes.
    -   ``"closing"``: :meth:`close` is closing the plugins, so writes are
        rejected. This state is only observable from a plugin ``close()``
        method.
    -   ``"closed"``: every plugin, and the file if owned, was closed
        successfully.
    -   ``"failed"``: closing raised, or a plugin failed to initialize.

    :attr:`closed` is ``True`` in the ``"closed"`` and ``"failed"`` states, as
    for any :class:`io.IOBase` subclass.

    :meth:`close` runs once. It closes every plugin that was successfully
    initialized, from the head of the chain to the tail, and then *file* if
    the manager owns it. A plugin whose ``close()`` method raises does not
    stop the remaining plugins from being closed: all of them are attempted,
    and the first exception is raised, with the following ones attached to it
    as notes. Later calls to :meth:`close` close nothing and raise nothing.

    If a plugin raises while it is being initialized, the plugins initialized
    before it are closed with the same rules, and the initialization error is
    raised with any close error attached to it.
    """

    _OPEN = "open"
    _CLOSING = "closing"
    _CLOSED = "closed"
    _FAILED = "failed"

    def __init__(
        self,
        plugins: list[Any],
        file: IO[bytes],
        feed_options: dict[str, Any],
        *,
        _owns_file: bool = True,
    ) -> None:
        self.file = file
        self.feed_options = feed_options
        self._owns_file = _owns_file
        self._state = self._OPEN
        self._target = _PluginTarget(file)
        self._initialized: list[Any] = []
        try:
            self.plugins = self._load_plugins(plugins)
            self.head_plugin = self._initialize_plugins()
        except Exception as exc:
            self._state = self._FAILED
            self._add_notes(exc, self._close_resources())
            raise

    @property
    def closed(self) -> bool:
        return self._state in {self._CLOSED, self._FAILED}

    def write(self, data: bytes) -> int:
        """
        Uses all the declared plugins to process data first, then writes
        the processed data to target file.
        :param data: data passed to be written to target file
        :type data: bytes
        :return: returns number of bytes written
        :rtype: int
        """
        if self._state != self._OPEN:
            raise ValueError(f"Cannot write to a {self._state} PostProcessingManager")
        return cast("int", self.head_plugin.write(data))

    def tell(self) -> int:
        return self.file.tell()

    def close(self) -> None:
        """
        Close the initialized plugins, from the head of the chain to the tail,
        and then the file if this manager owns it.

        If closing raises, the first exception is raised, with the other ones
        attached to it as notes.
        """
        if self._state != self._OPEN:
            return
        self._state = self._CLOSING
        failures = self._close_resources()
        if failures:
            self._state = self._FAILED
            self._add_notes(failures[0][1], failures)
            raise failures[0][1]
        self._state = self._CLOSED

    def writable(self) -> bool:
        return self._state == self._OPEN

    def _load_plugins(self, plugins: list[Any]) -> list[Any]:
        return [load_object(plugin) for plugin in plugins]

    def _initialize_plugins(self) -> Any:
        prev: Any = self._target
        for plugin in reversed(self.plugins):
            prev = plugin(prev, self.feed_options)
            self._initialized.append(prev)
        return prev

    def _close_resources(self) -> list[tuple[str, BaseException]]:
        """Close the initialized plugins, and the file if it is owned, and
        return the failures in the order in which they happened."""
        failures: list[tuple[str, BaseException]] = []
        for plugin in reversed(self._initialized):
            try:
                plugin.close()
            except Exception as exc:
                failures.append((f"plugin {type(plugin).__name__}", exc))
        if self._owns_file:
            try:
                self.file.close()
            except Exception as exc:
                failures.append(("the feed file", exc))
        return failures

    @staticmethod
    def _add_notes(
        primary: BaseException, failures: list[tuple[str, BaseException]]
    ) -> None:
        """Keep *primary* as the error that is raised, and describe the other
        failures in the order in which they happened."""
        for name, exc in failures:
            if exc is not primary:
                primary.add_note(f"Also failed to close {name}: {exc!r}")
