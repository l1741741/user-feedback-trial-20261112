"""Tests for the per-feed item filter audit stats and the
FEED_FILTER_ERROR_POLICY setting."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import pytest
from w3lib.url import file_uri_to_path

from scrapy.extensions.feedexport import ItemFilter
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test
from tests.utils.feedexport import crawl_items, path_to_url, unique_path

if TYPE_CHECKING:
    from scrapy import Spider
    from tests.mockserver.http import MockServer


class NoSkipFilter(ItemFilter):
    """Rejects items whose ``foo`` field is ``"skip"``."""

    def accepts(self, item: Any) -> bool:
        return bool(item["foo"] != "skip")


class StatefulFilter(ItemFilter):
    """Accepts the first two items it sees, recording instance and call
    counts to prove that a single instance judges every item."""

    instances = 0
    calls = 0

    def __init__(self, feed_options: dict[str, Any] | None) -> None:
        super().__init__(feed_options)
        type(self).instances += 1
        self.seen = 0

    def accepts(self, item: Any) -> bool:
        type(self).calls += 1
        self.seen += 1
        return self.seen <= 2


class BoomFilter(ItemFilter):
    """Raises on items whose ``foo`` field is ``"boom"``."""

    def accepts(self, item: Any) -> bool:
        if item["foo"] == "boom":
            raise ValueError("simulated filter failure")
        return True


class NonBoolFilter(ItemFilter):
    """Returns a non-bool for items whose ``foo`` field is ``"weird"``."""

    def accepts(self, item: Any) -> bool:
        if item["foo"] == "weird":
            return "yes"  # type: ignore[return-value]
        return True


class CountingFilter(ItemFilter):
    """Accepts everything, recording how many items it judged."""

    calls = 0

    def accepts(self, item: Any) -> bool:
        type(self).calls += 1
        return True


class FailingAsyncStorage:
    """Storage whose coroutine ``store()`` always fails."""

    def __init__(self, uri: str, *, feed_options: dict[str, Any] | None = None):
        self.path = Path(file_uri_to_path(uri))

    async def open(self, spider: Spider) -> IO[bytes]:
        return self.path.open("wb")

    async def store(self, file: IO[bytes]) -> None:
        file.close()
        raise OSError("simulated storage failure")


def _jl(path: Path) -> list[bytes]:
    """Return the lines of the JSON lines feed at *path*."""
    return path.read_bytes().splitlines()


@coroutine_test
async def test_filter_stats_two_feeds(mockserver: MockServer, tmp_path: Path) -> None:
    items = [{"foo": "bar1"}, {"foo": "skip"}, {"foo": "bar3"}]
    feed_all = unique_path(tmp_path)
    feed_filtered = unique_path(tmp_path)
    settings = {
        "FEEDS": {
            feed_all: {"format": "jsonlines"},
            feed_filtered: {"format": "jsonlines", "item_filter": NoSkipFilter},
        },
    }
    crawler = await crawl_items(mockserver, items, settings)

    assert crawler.stats.get_value("feedexport/filter/accepted/0") == 3
    assert crawler.stats.get_value("feedexport/filter/rejected/0") == 0
    assert crawler.stats.get_value("feedexport/filter/errors/0") == 0
    assert crawler.stats.get_value("feedexport/filter/accepted/1") == 2
    assert crawler.stats.get_value("feedexport/filter/rejected/1") == 1
    assert crawler.stats.get_value("feedexport/filter/errors/1") == 0

    assert _jl(feed_all) == [
        b'{"foo": "bar1"}',
        b'{"foo": "skip"}',
        b'{"foo": "bar3"}',
    ]
    assert _jl(feed_filtered) == [b'{"foo": "bar1"}', b'{"foo": "bar3"}']


@coroutine_test
async def test_filter_stats_accumulate_across_batches(
    mockserver: MockServer, tmp_path: Path
) -> None:
    StatefulFilter.instances = 0
    StatefulFilter.calls = 0
    items = [{"foo": f"bar{i}"} for i in range(1, 6)]
    feed_dir = unique_path(tmp_path)
    settings = {
        "FEEDS": {
            f"{feed_dir}/batch-%(batch_id)d.jl": {
                "format": "jsonlines",
                "item_filter": StatefulFilter,
                "batch_item_count": 1,
            },
        },
    }
    crawler = await crawl_items(mockserver, items, settings)

    # One judgment per item, by a single filter instance reused across
    # batches; the counts are not reset when a batch is switched.
    assert StatefulFilter.instances == 1
    assert StatefulFilter.calls == 5
    assert crawler.stats.get_value("feedexport/filter/accepted/0") == 2
    assert crawler.stats.get_value("feedexport/filter/rejected/0") == 3
    assert crawler.stats.get_value("feedexport/filter/errors/0") == 0

    batches = sorted(feed_dir.iterdir())
    assert [batch.read_bytes() for batch in batches] == [
        b'{"foo": "bar1"}\n',
        b'{"foo": "bar2"}\n',
    ]


@coroutine_test
async def test_filter_error_raise(
    caplog: pytest.LogCaptureFixture, mockserver: MockServer, tmp_path: Path
) -> None:
    items = [{"foo": "bar1"}, {"foo": "boom"}, {"foo": "bar3"}]
    feed_raising = unique_path(tmp_path)
    feed_all = unique_path(tmp_path)
    settings = {
        "FEEDS": {
            feed_raising: {"format": "jsonlines", "item_filter": BoomFilter},
            feed_all: {"format": "jsonlines"},
        },
    }
    with caplog.at_level(logging.ERROR):
        crawler = await crawl_items(mockserver, items, settings)

    assert crawler.stats.get_value("feedexport/filter/errors/0") == 1
    assert crawler.stats.get_value("feedexport/filter/accepted/0") == 2
    # The exception propagates, so later feeds do not see that item.
    assert crawler.stats.get_value("feedexport/filter/accepted/1") == 2
    assert "simulated filter failure" in caplog.text
    assert _jl(feed_raising) == [b'{"foo": "bar1"}', b'{"foo": "bar3"}']
    assert _jl(feed_all) == [b'{"foo": "bar1"}', b'{"foo": "bar3"}']


@coroutine_test
async def test_filter_error_skip(
    caplog: pytest.LogCaptureFixture, mockserver: MockServer, tmp_path: Path
) -> None:
    items = [{"foo": "bar1"}, {"foo": "boom"}, {"foo": "bar3"}]
    feed_raising = unique_path(tmp_path)
    feed_all = unique_path(tmp_path)
    settings = {
        "FEED_FILTER_ERROR_POLICY": "skip",
        "FEEDS": {
            feed_raising: {"format": "jsonlines", "item_filter": BoomFilter},
            feed_all: {"format": "jsonlines"},
        },
    }
    with caplog.at_level(logging.ERROR):
        crawler = await crawl_items(mockserver, items, settings)

    assert crawler.stats.get_value("feedexport/filter/errors/0") == 1
    assert crawler.stats.get_value("feedexport/filter/accepted/0") == 2
    # Skipping only affects the failing feed; the other feed gets every item.
    assert crawler.stats.get_value("feedexport/filter/accepted/1") == 3
    assert "simulated filter failure" in caplog.text
    assert _jl(feed_raising) == [b'{"foo": "bar1"}', b'{"foo": "bar3"}']
    assert _jl(feed_all) == [
        b'{"foo": "bar1"}',
        b'{"foo": "boom"}',
        b'{"foo": "bar3"}',
    ]


@pytest.mark.parametrize("policy", ["raise", "skip"])
@coroutine_test
async def test_filter_non_bool_return(
    policy: str,
    caplog: pytest.LogCaptureFixture,
    mockserver: MockServer,
    tmp_path: Path,
) -> None:
    items = [{"foo": "bar1"}, {"foo": "weird"}, {"foo": "bar3"}]
    feed_nonbool = unique_path(tmp_path)
    feed_all = unique_path(tmp_path)
    settings = {
        "FEED_FILTER_ERROR_POLICY": policy,
        "FEEDS": {
            feed_nonbool: {"format": "jsonlines", "item_filter": NonBoolFilter},
            feed_all: {"format": "jsonlines"},
        },
    }
    with caplog.at_level(logging.ERROR):
        crawler = await crawl_items(mockserver, items, settings)

    assert crawler.stats.get_value("feedexport/filter/errors/0") == 1
    assert crawler.stats.get_value("feedexport/filter/accepted/0") == 2
    assert "must return a bool" in caplog.text
    assert _jl(feed_nonbool) == [b'{"foo": "bar1"}', b'{"foo": "bar3"}']
    if policy == "raise":
        # The TypeError propagates, so later feeds do not see that item.
        assert crawler.stats.get_value("feedexport/filter/accepted/1") == 2
        assert _jl(feed_all) == [b'{"foo": "bar1"}', b'{"foo": "bar3"}']
    else:
        assert crawler.stats.get_value("feedexport/filter/accepted/1") == 3
        assert _jl(feed_all) == [
            b'{"foo": "bar1"}',
            b'{"foo": "weird"}',
            b'{"foo": "bar3"}',
        ]


@coroutine_test
async def test_filter_error_policy_feed_override(
    caplog: pytest.LogCaptureFixture, mockserver: MockServer, tmp_path: Path
) -> None:
    items = [{"foo": "boom"}, {"foo": "bar2"}]
    feed_skip = unique_path(tmp_path)
    feed_raise = unique_path(tmp_path)
    settings = {
        "FEED_FILTER_ERROR_POLICY": "skip",
        "FEEDS": {
            feed_skip: {"format": "jsonlines", "item_filter": BoomFilter},
            feed_raise: {
                "format": "jsonlines",
                "item_filter": BoomFilter,
                "filter_error_policy": "raise",
            },
        },
    }
    with caplog.at_level(logging.ERROR):
        crawler = await crawl_items(mockserver, items, settings)

    # Feed 0 inherits the skip policy, feed 1 overrides it with raise.
    assert crawler.stats.get_value("feedexport/filter/errors/0") == 1
    assert crawler.stats.get_value("feedexport/filter/errors/1") == 1
    assert crawler.stats.get_value("feedexport/filter/accepted/0") == 1
    assert crawler.stats.get_value("feedexport/filter/accepted/1") == 1
    assert "simulated filter failure" in caplog.text
    assert _jl(feed_skip) == [b'{"foo": "bar2"}']
    assert _jl(feed_raise) == [b'{"foo": "bar2"}']


@pytest.mark.parametrize("option", ["setting", "feed_option"])
def test_invalid_filter_error_policy(option: str, tmp_path: Path) -> None:
    feed = unique_path(tmp_path)
    feed_options: dict[str, Any] = {"format": "jsonlines"}
    settings: dict[str, Any] = {"FEEDS": {feed: feed_options}}
    if option == "feed_option":
        feed_options["filter_error_policy"] = "ignore"
    else:
        settings["FEED_FILTER_ERROR_POLICY"] = "ignore"

    with pytest.raises(ValueError, match="Invalid filter_error_policy"):
        get_crawler(settings_dict=settings)
    assert list(tmp_path.iterdir()) == []


@coroutine_test
async def test_filter_rejection_keeps_empty_feed_conventions(
    mockserver: MockServer, tmp_path: Path
) -> None:
    items = [{"foo": "skip"}, {"foo": "skip"}]
    feed_empty = unique_path(tmp_path)
    feed_absent = unique_path(tmp_path)
    settings = {
        "FEEDS": {
            feed_empty: {
                "format": "jsonlines",
                "item_filter": NoSkipFilter,
                "store_empty": True,
            },
            feed_absent: {
                "format": "jsonlines",
                "item_filter": NoSkipFilter,
                "store_empty": False,
            },
        },
    }
    crawler = await crawl_items(mockserver, items, settings)

    for feed_id in (0, 1):
        assert crawler.stats.get_value(f"feedexport/filter/accepted/{feed_id}") == 0
        assert crawler.stats.get_value(f"feedexport/filter/rejected/{feed_id}") == 2
    # Rejected items do not count as exported items.
    assert crawler.stats.get_value("feedexport/item_count/FileFeedStorage", 0) == 0
    # The empty-feed conventions are unchanged.
    assert feed_empty.read_bytes() == b""
    assert not feed_absent.exists()


@coroutine_test
async def test_filter_errors_do_not_create_empty_batches(
    mockserver: MockServer, tmp_path: Path
) -> None:
    items = [{"foo": "boom"}, {"foo": "boom"}]
    feed = unique_path(tmp_path)
    settings = {
        "FEED_FILTER_ERROR_POLICY": "skip",
        "FEEDS": {
            feed: {
                "format": "jsonlines",
                "item_filter": BoomFilter,
                "store_empty": False,
            },
        },
    }
    crawler = await crawl_items(mockserver, items, settings)

    assert crawler.stats.get_value("feedexport/filter/errors/0") == 2
    assert crawler.stats.get_value("feedexport/filter/accepted/0") == 0
    assert crawler.stats.get_value("feedexport/item_count/FileFeedStorage", 0) == 0
    assert not feed.exists()


@coroutine_test
async def test_async_storage_failure_keeps_filter_stats(
    caplog: pytest.LogCaptureFixture, mockserver: MockServer, tmp_path: Path
) -> None:
    CountingFilter.calls = 0
    items = [{"foo": "bar1"}, {"foo": "bar2"}]
    feed = unique_path(tmp_path)
    settings = {
        "FEEDS": {
            path_to_url(feed): {"format": "jsonlines", "item_filter": CountingFilter},
        },
        "FEED_STORAGES": {"file": FailingAsyncStorage},
    }
    with caplog.at_level(logging.ERROR):
        crawler = await crawl_items(mockserver, items, settings)

    assert crawler.stats.get_value("feedexport/filter/accepted/0") == 2
    assert crawler.stats.get_value("feedexport/filter/errors/0") == 0
    # The storage failure does not re-run the filter.
    assert CountingFilter.calls == 2
    assert crawler.stats.get_value("feedexport/failed_count/FailingAsyncStorage") == 1
    assert (
        crawler.stats.get_value("feedexport/success_count/FailingAsyncStorage", 0) == 0
    )
    assert "simulated storage failure" in caplog.text
