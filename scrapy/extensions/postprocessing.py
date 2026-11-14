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
    - `gzip_reproducible`

    See :py:class:`gzip.GzipFile` for more info about parameters.

    If `gzip_reproducible` is ``True``, non-content metadata in the gzip
    header is pinned: the modification time defaults to ``0`` and the
    original file name to empty, so that exporting the same bytes with the
    same settings produces identical compressed bytes. Explicit
    `gzip_mtime` and `gzip_filename` values take precedence over these
    pinned defaults. Compression is still streaming; the feed is not
    buffered in memory.
    """

    def __init__(self, file: BinaryIO, feed_options: dict[str, Any]) -> None:
        self.file = file
        self.feed_options = feed_options
        compress_level = self.feed_options.get("gzip_compresslevel", 9)
        if self.feed_options.get("gzip_reproducible", False):
            mtime = self.feed_options.get("gzip_mtime", 0)
            filename = self.feed_options.get("gzip_filename", "")
        else:
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

    def flush(self) -> None:
        self.gzipfile.flush()

    def close(self) -> None:
        # Closing the GzipFile writes the trailer and flushes, but does not
        # close the underlying file object, which remains owned by the
        # caller. Closing twice is a no-op.
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


# io.IOBase is subclassed here, so that exporters can use the PostProcessingManager
# instance as a file like writable object. This could be needed by some exporters
# such as CsvItemExporter which wraps the feed storage with io.TextIOWrapper.
class PostProcessingManager(IOBase):
    """
    This will manage and use declared plugins to process data in a
    pipeline-ish way.
    :param plugins: all the declared plugins for the feed
    :type plugins: list
    :param file: final target file where the processed data will be written
    :type file: file like object
    """

    def __init__(
        self, plugins: list[Any], file: IO[bytes], feed_options: dict[str, Any]
    ) -> None:
        self.plugins = self._load_plugins(plugins)
        self.file = file
        self.feed_options = feed_options
        self.head_plugin = self._get_head_plugin()

    def write(self, data: bytes) -> int:
        """
        Uses all the declared plugins to process data first, then writes
        the processed data to target file.
        :param data: data passed to be written to target file
        :type data: bytes
        :return: returns number of bytes written
        :rtype: int
        """
        return cast("int", self.head_plugin.write(data))

    def tell(self) -> int:
        return self.file.tell()

    def close(self) -> None:
        """
        Close the target file along with all the plugins.
        """
        self.head_plugin.close()

    def writable(self) -> bool:
        return True

    def _load_plugins(self, plugins: list[Any]) -> list[Any]:
        return [load_object(plugin) for plugin in plugins]

    def _get_head_plugin(self) -> Any:
        prev = self.file
        for plugin in self.plugins[::-1]:
            prev = plugin(prev, self.feed_options)
        return prev
