"""
Scrapy extension for collecting scraping stats
"""

from __future__ import annotations

import logging
import pprint
from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any, TypeAlias

from scrapy.utils._stats_snapshot import (
    build_stats_snapshot,
    snapshot_params,
    write_stats_snapshot,
)
from scrapy.utils.decorators import _warn_spider_arg

if TYPE_CHECKING:
    import os
    from collections.abc import Iterator

    from scrapy import Spider
    from scrapy.crawler import Crawler


logger = logging.getLogger(__name__)


StatsT: TypeAlias = dict[str, Any]

_MISSING = object()
_SPIDER_ARG_METHODS = (
    "get_value",
    "get_stats",
    "set_value",
    "set_stats",
    "inc_value",
    "max_value",
    "min_value",
    "clear_stats",
    "open_spider",
    "close_spider",
)


class StatsCollector(MutableMapping[str, Any]):
    """The base stats collector that other stats collectors are based on."""

    __eq__ = object.__eq__
    __hash__ = object.__hash__

    def __init__(self, crawler: Crawler):
        self._dump: bool = crawler.settings.getbool("STATS_DUMP")
        self._stats: StatsT = {}
        self._crawler: Crawler = crawler
        for name in _SPIDER_ARG_METHODS:
            setattr(self, name, _warn_spider_arg(getattr(self, name)))

    def get_value(
        self, key: str, default: Any = None, spider: Spider | None = None
    ) -> Any:
        """Return the value of the *key* stat, or *default* if it is not set."""
        return self._stats.get(key, default)

    def get_stats(self, spider: Spider | None = None) -> StatsT:
        """Return all stats as a dict."""
        return self._stats

    def get_stats_snapshot(
        self,
        *,
        include: list[str] | None = None,
        sensitive_keys: list[str] | None = None,
        max_depth: int | None = None,
        max_nodes: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Return an independent, JSON-compatible copy of the current stats.

        The returned dict is safe to pass to :func:`json.dumps` and shares no
        mutable container with the live stats or with other snapshots, so it
        can be handed to monitoring while the crawl keeps updating its stats.
        Neither the live stats nor the returned copy are ever modified by
        this method, and the method does not run user code such as
        :meth:`object.__repr__`, :func:`iter` or ``__json__`` on stat values.

        Only the top-level stat keys are filtered: *include*, when non-empty,
        is a list of case-insensitive :mod:`fnmatch` patterns and only the
        matching stats are copied. At any mapping level, a key matching a
        sensitive-key pattern is copied as the string ``"[REDACTED]"`` and
        its value is never read. The built-in patterns cover credentials
        (``*auth*``, ``*cookie*``, ``*password*``, ``*secret*``, ``*session*``,
        ``*token*``, ``*api_key*`` and similar); *sensitive_keys* adds more.
        The default values of all parameters come from the
        :setting:`STATS_SNAPSHOT_INCLUDE`, :setting:`STATS_SNAPSHOT_SENSITIVE_KEYS`,
        :setting:`STATS_SNAPSHOT_MAX_DEPTH`, :setting:`STATS_SNAPSHOT_MAX_NODES`
        and :setting:`STATS_SNAPSHOT_MAX_BYTES` settings.

        Values are converted as follows:

        -   ``None``, :class:`bool`, :class:`int`, finite :class:`float` and
            :class:`str` are copied as they are.

        -   :class:`bytes` become a single-key object whose
            ``"__scrapy_bytes_base64__"`` value is the standard Base64
            encoding of the bytes.

        -   :class:`datetime.datetime` objects become ISO 8601 strings. An
            aware datetime keeps its UTC offset; a naive one is assumed to be
            in UTC and gets a ``+00:00`` suffix. :class:`datetime.date` and
            :class:`datetime.time` objects become their :meth:`~object.__str__`
            ISO 8601 form. A :class:`datetime.timedelta` becomes a number of
            seconds.

        -   Mappings keyed by strings become JSON objects, recursively; list
            and tuple values become JSON arrays, recursively. A set or
            frozenset becomes a JSON array sorted by the canonical JSON
            encoding of its items, so repeated snapshots can be compared.

        -   Any other value, a non-finite float, a mapping key that is not a
            string, or a value reached through a circular reference raises
            :exc:`~scrapy.exceptions.StatsSnapshotError`. No partial snapshot
            is returned or logged.

        The copy is bounded: nesting deeper than *max_depth* levels, more
        than *max_nodes* values and mapping keys, or more than *max_bytes*
        UTF-8 bytes of the equivalent JSON raise
        :exc:`~scrapy.exceptions.StatsSnapshotError` as soon as a limit is
        crossed, before the rest of the value is copied.

        .. versionadded:: VERSION
        """
        (
            effective_include,
            effective_sensitive,
            effective_depth,
            effective_nodes,
            effective_bytes,
        ) = snapshot_params(
            self._crawler.settings,
            include=include,
            sensitive_keys=sensitive_keys,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_bytes=max_bytes,
        )
        return build_stats_snapshot(
            self._stats,
            include=effective_include,
            sensitive=effective_sensitive,
            max_depth=effective_depth,
            max_nodes=effective_nodes,
            max_bytes=effective_bytes,
        )

    def save_stats_snapshot(
        self,
        path: str | os.PathLike[str],
        *,
        include: list[str] | None = None,
        sensitive_keys: list[str] | None = None,
        max_depth: int | None = None,
        max_nodes: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        """Write :meth:`get_stats_snapshot` output to *path* atomically.

        The snapshot is produced first; if the parameters are invalid or a
        stat value cannot be copied, nothing is written. The JSON text then
        goes to a temporary file in the same directory, which is flushed and
        synced before replacing *path*, so a reader never sees a partial
        file. If writing fails, *path* keeps its previous contents, if any,
        the temporary file is removed, and
        :exc:`~scrapy.exceptions.StatsSnapshotError` is raised with
        ``reason="write"``.

        .. versionadded:: VERSION
        """
        write_stats_snapshot(
            self.get_stats_snapshot(
                include=include,
                sensitive_keys=sensitive_keys,
                max_depth=max_depth,
                max_nodes=max_nodes,
                max_bytes=max_bytes,
            ),
            path,
        )

    def set_value(self, key: str, value: Any, spider: Spider | None = None) -> None:
        """Set the *key* stat to *value*."""
        self._stats[key] = value

    def set_stats(self, stats: StatsT, spider: Spider | None = None) -> None:
        """Replace all stats with *stats*."""
        self._stats = stats

    def inc_value(
        self,
        key: str,
        count: float = 1,
        start: float = 0,
        spider: Spider | None = None,
    ) -> None:
        """Increment the *key* stat by *count*, or set it to *start* if it is
        not set."""
        d = self._stats
        d[key] = d.setdefault(key, start) + count

    def max_value(self, key: str, value: Any, spider: Spider | None = None) -> None:
        """Set the *key* stat to *value* if it is not set or lower than
        *value*."""
        self._stats[key] = max(self._stats.setdefault(key, value), value)

    def min_value(self, key: str, value: Any, spider: Spider | None = None) -> None:
        """Set the *key* stat to *value* if it is not set or higher than
        *value*."""
        self._stats[key] = min(self._stats.setdefault(key, value), value)

    def clear_stats(self, spider: Spider | None = None) -> None:
        """Clear all stats."""
        self._stats.clear()

    def open_spider(self, spider: Spider | None = None) -> None:
        """Called when the spider is opened."""

    def close_spider(
        self, spider: Spider | None = None, reason: str | None = None
    ) -> None:
        """Called when the spider is closed."""
        if self._dump:
            logger.info(
                f"Dumping Scrapy stats:\n{self}",
                extra={"spider": self._crawler.spider},
            )
        self._persist_stats(self._stats)

    def _persist_stats(self, stats: StatsT) -> None:
        pass

    def __str__(self) -> str:
        return pprint.pformat(self._stats)

    # Components use ``if stats:`` to tell a stats collector from None.
    def __bool__(self) -> bool:
        return True

    def __getitem__(self, key: str) -> Any:
        value = self.get_value(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        self.set_value(key, value)

    def __delitem__(self, key: str) -> None:
        del self._stats[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.get_stats())

    def __len__(self) -> int:
        return len(self.get_stats())


class MemoryStatsCollector(StatsCollector):
    """A simple stats collector that keeps the stats of the last scraping run
    (for each spider) in memory, after they're closed. The stats can be
    accessed through the :attr:`spider_stats` attribute, which is a dict keyed
    by spider name.

    This is the default stats collector used in Scrapy.
    """

    spider_stats: dict[str, StatsT]
    """A dict of dicts (keyed by spider name) containing the stats of the last
    scraping run for each spider."""

    def __init__(self, crawler: Crawler):
        super().__init__(crawler)
        self.spider_stats = {}

    def _persist_stats(self, stats: StatsT) -> None:
        if self._crawler.spider:
            self.spider_stats[self._crawler.spider.name] = stats


class DummyStatsCollector(StatsCollector):
    """A stats collector which does nothing but is very efficient (because it
    does nothing). This stats collector can be set via the
    :setting:`STATS_CLASS` setting, to disable stats collection in order to
    improve performance. However, the performance penalty of stats collection
    is usually marginal compared to other Scrapy workload like parsing pages.
    """

    def get_value(
        self, key: str, default: Any = None, spider: Spider | None = None
    ) -> Any:
        return default

    def set_value(self, key: str, value: Any, spider: Spider | None = None) -> None:
        pass

    def set_stats(self, stats: StatsT, spider: Spider | None = None) -> None:
        pass

    def inc_value(
        self,
        key: str,
        count: float = 1,
        start: float = 0,
        spider: Spider | None = None,
    ) -> None:
        pass

    def max_value(self, key: str, value: Any, spider: Spider | None = None) -> None:
        pass

    def min_value(self, key: str, value: Any, spider: Spider | None = None) -> None:
        pass
