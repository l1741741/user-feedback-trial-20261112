from __future__ import annotations

import errno
import logging
import os
import pickle
import secrets
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Self

from scrapy import Spider, signals
from scrapy.exceptions import NotConfigured, ScrapyDeprecationWarning
from scrapy.utils.conf import _job_dir

if TYPE_CHECKING:
    from typing import IO

    from scrapy.crawler import Crawler


logger = logging.getLogger(__name__)

# Errors that mean a platform cannot sync a directory, as opposed to a failed
# sync. Opening a directory with os.open() is not allowed on Windows, which
# reports EACCES or EPERM.
_UNSUPPORTED_DIRECTORY_SYNC_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EINVAL", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "EPERM", None),
    )
    if code is not None
)

_TEMP_FILE_ATTEMPTS = 10


def _open_temp_file(state_path: Path) -> tuple[IO[bytes], Path]:
    """Create an exclusive temporary file next to *state_path*.

    The file is created with the permissions that the current umask gives to
    any other new file, and the random name keeps concurrent instances in the
    same job directory from writing to the same file.
    """
    directory = state_path.parent
    for _ in range(_TEMP_FILE_ATTEMPTS):
        temp_path = directory / f".{state_path.name}.{secrets.token_hex(8)}.tmp"
        try:
            return temp_path.open("xb"), temp_path
        except FileExistsError:
            continue
    raise OSError(f"Cannot create a temporary file in {directory}")


def _discard_temp_file(temp_path: Path) -> None:
    """Remove *temp_path*, reporting failures instead of raising them.

    This runs while another exception is propagating, so it must not hide it.
    """
    try:
        temp_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(f"Cannot remove the temporary file {temp_path}: {exc}")


def _sync_directory(directory: Path) -> None:
    """Flush *directory* so that a completed rename survives a power loss.

    The state file has already been replaced when this runs, so failures are
    logged instead of raised: there is no older version to fall back to.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_SYNC_ERRNOS:
            logger.debug(
                f"Cannot sync {directory} after writing spider state; this "
                f"platform does not support opening a directory to sync it: "
                f"{exc}"
            )
        else:
            logger.warning(
                f"Cannot open {directory} to sync it after writing spider "
                f"state: {exc}. The new state is in place, but the rename may "
                f"not survive a power loss."
            )
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_SYNC_ERRNOS:
            logger.debug(
                f"Cannot sync {directory} after writing spider state; this "
                f"platform does not support syncing directories: {exc}"
            )
        else:
            logger.warning(
                f"Cannot sync {directory} after writing spider state: {exc}. "
                f"The new state is in place, but the rename may not survive a "
                f"power loss."
            )
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            logger.warning(f"Cannot close {directory}: {exc}")


class SpiderState:
    """Store and load spider state during a scraping job"""

    def __init__(self, jobdir: str | None = None):
        self.jobdir: str | None = jobdir
        self.crawler: Crawler | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        jobdir = _job_dir(crawler.settings)
        if not jobdir:
            raise NotConfigured

        obj = cls(jobdir)
        obj.crawler = crawler
        crawler.signals.connect(obj._spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(obj._spider_opened, signal=signals.spider_opened)
        return obj

    def _load_state(self, spider: Spider) -> None:
        if Path(self.statefn).exists():
            with Path(self.statefn).open("rb") as f:
                spider.state = pickle.load(f)  # noqa: S301
        else:
            spider.state = {}

    def _persist_state(self, spider: Spider) -> None:
        """Save ``spider.state`` by replacing the state file atomically.

        Everything is written to a temporary file in the job directory and
        moved over the state file only once it is fully serialized and synced,
        so a failure at any earlier point leaves the previous state file
        untouched.
        """
        assert hasattr(spider, "state")
        state_path = Path(self.statefn)
        f, temp_path = _open_temp_file(state_path)
        try:
            pickle.dump(spider.state, f, protocol=4)
            f.flush()
            os.fsync(f.fileno())
            f.close()
            temp_path.replace(state_path)
        except BaseException:
            if not f.closed:
                try:
                    f.close()
                except BaseException as exc:
                    # The original exception must reach the caller, since it
                    # explains why the state file was not replaced.
                    logger.warning(
                        f"Cannot close the temporary file {temp_path}: {exc}"
                    )
            _discard_temp_file(temp_path)
            raise
        _sync_directory(state_path.parent)

    async def _spider_opened(self, spider: Spider) -> None:
        self._load_state(spider)
        assert self.crawler is not None
        await self.crawler.signals.send_catch_log_async(
            signals.spider_state_loaded,
            state=spider.state,
        )

    async def _spider_closed(self, spider: Spider) -> None:
        assert self.crawler is not None
        await self.crawler.signals.send_catch_log_async(
            signals.spider_state_saving,
            state=spider.state,
        )
        self._persist_state(spider)

    def spider_opened(self, spider: Spider) -> None:
        warnings.warn(
            f"{type(self).__qualname__}.spider_opened() is deprecated, "
            "use the spider_state_loaded signal instead.",
            category=ScrapyDeprecationWarning,
            stacklevel=2,
        )
        self._load_state(spider)
        assert self.crawler is not None
        self.crawler.signals.send_catch_log(
            signals.spider_state_loaded,
            state=spider.state,
        )

    def spider_closed(self, spider: Spider) -> None:
        warnings.warn(
            f"{type(self).__qualname__}.spider_closed() is deprecated, "
            "use the spider_state_saving signal instead.",
            category=ScrapyDeprecationWarning,
            stacklevel=2,
        )
        assert self.crawler is not None
        self.crawler.signals.send_catch_log(
            signals.spider_state_saving,
            state=spider.state,
        )
        self._persist_state(spider)

    @property
    def statefn(self) -> str:
        assert self.jobdir
        return str(Path(self.jobdir, "spider.state"))
