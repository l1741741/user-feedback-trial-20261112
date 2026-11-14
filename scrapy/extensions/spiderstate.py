from __future__ import annotations

import logging
import os
import pickle
import tempfile
import warnings
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from scrapy import Spider, signals
from scrapy.exceptions import NotConfigured, ScrapyDeprecationWarning
from scrapy.utils.conf import _job_dir

if TYPE_CHECKING:
    from collections.abc import Callable, BinaryIO

    from scrapy.crawler import Crawler

logger = logging.getLogger(__name__)


class SpiderStateError(Exception):
    """Raised when the spider state cannot be loaded or saved safely."""


class SpiderState:
    """Store and load spider state during a scraping job"""

    def __init__(
        self,
        jobdir: str | None = None,
        *,
        atomic: bool = False,
        backup: bool = False,
    ):
        self.jobdir: str | None = jobdir
        self.atomic: bool = atomic
        self.backup: bool = backup
        self.crawler: Crawler | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        jobdir = _job_dir(crawler.settings)
        if not jobdir:
            raise NotConfigured

        obj = cls(
            jobdir,
            atomic=crawler.settings.getbool("SPIDER_STATE_ATOMIC"),
            backup=crawler.settings.getbool("SPIDER_STATE_BACKUP"),
        )
        obj.crawler = crawler
        crawler.signals.connect(obj._spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(obj._spider_opened, signal=signals.spider_opened)
        return obj

    def _read_state_file(self, path: Path) -> dict[str, Any] | None:
        """Return the state stored in *path*, or ``None`` if it is missing or
        cannot be fully deserialized into a dict."""
        try:
            with path.open("rb") as f:
                state = pickle.load(f)  # noqa: S301
        except Exception:
            return None
        if not isinstance(state, dict):
            return None
        return state

    def _load_state(self, spider: Spider) -> None:
        path = Path(self.statefn)
        if not self.backup:
            if path.exists():
                with path.open("rb") as f:
                    spider.state = pickle.load(f)  # noqa: S301
            else:
                spider.state = {}
            return
        if path.exists():
            state = self._read_state_file(path)
            if state is not None:
                spider.state = state
                return
            backup_state = self._read_state_file(Path(self.backupfn))
            if backup_state is not None:
                logger.warning(
                    "Spider state file %s is invalid, restored the previous "
                    "valid state from backup %s.",
                    path,
                    self.backupfn,
                )
                spider.state = backup_state
                return
            raise SpiderStateError(
                f"Spider state file {path} cannot be deserialized into a "
                f"dict and backup {self.backupfn} is missing or invalid. "
                "Both files were left untouched; fix or remove them before "
                "restarting the job."
            )
        spider.state = {}

    def _persist_state(self, spider: Spider) -> None:
        assert hasattr(spider, "state")
        if self.backup:
            self._update_backup()
        if self.atomic:
            self._atomic_write(
                Path(self.statefn),
                lambda f: pickle.dump(spider.state, f, protocol=4),
            )
            return
        with Path(self.statefn).open("wb") as f:
            pickle.dump(spider.state, f, protocol=4)

    def _update_backup(self) -> None:
        """Save the current valid state file as the backup, if any."""
        src = Path(self.statefn)
        if not src.exists():
            return
        state = self._read_state_file(src)
        if state is None:
            logger.warning(
                "Not updating spider state backup %s: existing state file "
                "%s cannot be deserialized into a dict.",
                self.backupfn,
                src,
            )
            return
        self._atomic_write(
            Path(self.backupfn), lambda f: pickle.dump(state, f, protocol=4)
        )

    def _atomic_write(
        self, path: Path, write_fn: Callable[[BinaryIO], None]
    ) -> None:
        """Write *path* through *write_fn* atomically.

        Serialize into a temporary file in the same directory, flush and
        fsync it, then atomically replace *path*. On any failure the
        temporary file is removed and the original file is left untouched.
        """
        fd, tmpname = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as f:
                write_fn(f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmpname, path)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmpname)
            raise
        self._fsync_dir(path.parent)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        """Best-effort fsync of a directory, so that a file replacement in
        it is durable."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

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

    @property
    def backupfn(self) -> str:
        assert self.jobdir
        return str(Path(self.jobdir, "spider.state.bak"))
