from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from scrapy import Spider
from scrapy.exporters import JsonLinesItemExporter
from scrapy.extensions.feedexport import FeedExporter, ItemFilter
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test
from tests.utils.feedexport import (
    MyItem,
    MyItem2,
    crawl_items,
    export_by_format,
    export_by_path,
    path_to_url,
    printf_escape,
    unique_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.mockserver.http import MockServer


@coroutine_test
async def test_export_based_on_item_classes(
    mockserver: MockServer, tmp_path: Path
) -> None:
    items = [
        MyItem({"foo": "bar1", "egg": "spam1"}),
        MyItem2({"hello": "world2", "foo": "bar2"}),
        {"hello": "world3", "egg": "spam3"},
    ]

    formats = {
        "csv": b"foo,egg,baz\r\nbar1,spam1,\r\n",
        "json": b'[\n{"foo": "bar2", "hello": "world2"}\n]',
        "jsonlines": (
            b'{"foo": "bar1", "egg": "spam1"}\n{"foo": "bar2", "hello": "world2"}\n'
        ),
        "xml": (
            b'<?xml version="1.0" encoding="utf-8"?>\n<items>\n<item>'
            b"<foo>bar1</foo><egg>spam1</egg></item>\n<item><foo>"
            b"bar2</foo><hello>world2</hello></item>\n<item><hello>world3"
            b"</hello><egg>spam3</egg></item>\n</items>"
        ),
    }

    settings = {
        "FEEDS": {
            unique_path(tmp_path): {
                "format": "csv",
                "item_classes": [MyItem],
            },
            unique_path(tmp_path): {
                "format": "json",
                "item_classes": [MyItem2],
            },
            unique_path(tmp_path): {
                "format": "jsonlines",
                "item_classes": [MyItem, MyItem2],
            },
            unique_path(tmp_path): {
                "format": "xml",
            },
        },
    }

    data = await export_by_format(mockserver, items, settings)
    for fmt, expected in formats.items():
        assert data[fmt] == expected


@coroutine_test
async def test_export_based_on_custom_filters(
    mockserver: MockServer, tmp_path: Path
) -> None:
    items = [
        MyItem({"foo": "bar1", "egg": "spam1"}),
        MyItem2({"hello": "world2", "foo": "bar2"}),
        {"hello": "world3", "egg": "spam3"},
    ]

    class CustomFilter1:
        def __init__(self, feed_options):
            pass

        def accepts(self, item):
            return isinstance(item, MyItem)

    class CustomFilter2(ItemFilter):
        def accepts(self, item):
            return "foo" in item.fields

    class CustomFilter3(ItemFilter):
        def accepts(self, item):
            return (
                isinstance(item, tuple(self.item_classes)) and item["foo"] == "bar1"  # type: ignore[index]
            )

    formats = {
        "json": b'[\n{"foo": "bar1", "egg": "spam1"}\n]',
        "xml": (
            b'<?xml version="1.0" encoding="utf-8"?>\n<items>\n<item>'
            b"<foo>bar1</foo><egg>spam1</egg></item>\n<item><foo>"
            b"bar2</foo><hello>world2</hello></item>\n</items>"
        ),
        "jsonlines": b'{"foo": "bar1", "egg": "spam1"}\n',
    }

    settings = {
        "FEEDS": {
            unique_path(tmp_path): {
                "format": "json",
                "item_filter": CustomFilter1,
            },
            unique_path(tmp_path): {
                "format": "xml",
                "item_filter": CustomFilter2,
            },
            unique_path(tmp_path): {
                "format": "jsonlines",
                "item_classes": [MyItem, MyItem2],
                "item_filter": CustomFilter3,
            },
        },
    }

    data = await export_by_format(mockserver, items, settings)
    for fmt, expected in formats.items():
        assert data[fmt] == expected


class TestItemFilter:
    def test_no_feed_options(self):
        item_filter = ItemFilter(None)
        assert item_filter.item_classes == ()
        assert item_filter.accepts(MyItem({"foo": "bar"}))


def split_foo(item):
    for value in item["foo"].split(","):
        yield {"foo": value}


def drop_item(item):
    return []


@coroutine_test
async def test_export_based_on_item_processors(
    mockserver: MockServer, tmp_path: Path
) -> None:
    items = [
        MyItem({"foo": "bar1,bar2"}),
        {"foo": "bar3"},
    ]

    formats = {
        "jsonlines": b'{"foo": "bar1"}\n{"foo": "bar2"}\n{"foo": "bar3"}\n',
        "json": b'[\n{"foo": "bar1"},\n{"foo": "bar2"},\n{"foo": "bar3"}\n]',
        "xml": (
            b'<?xml version="1.0" encoding="utf-8"?>\n<items>\n'
            b"<item><foo>bar1</foo></item>\n<item><foo>bar2</foo></item>\n</items>"
        ),
        "csv": b"",
    }

    settings = {
        "FEEDS": {
            unique_path(tmp_path): {
                "format": "jsonlines",
                "item_processor": split_foo,
            },
            unique_path(tmp_path): {
                "format": "json",
                "item_processor": "tests.test_feedexport_filters_processors.split_foo",
            },
            unique_path(tmp_path): {
                "format": "xml",
                "item_classes": [MyItem],
                "item_processor": split_foo,
            },
            unique_path(tmp_path): {
                "format": "csv",
                "item_processor": drop_item,
            },
        },
    }

    data = await export_by_format(mockserver, items, settings)
    for fmt, expected in formats.items():
        assert data[fmt] == expected


@coroutine_test
async def test_item_processor_stats(mockserver: MockServer, tmp_path: Path) -> None:
    settings = {
        "FEEDS": {
            path_to_url(unique_path(tmp_path)): {
                "format": "jsonlines",
                "item_processor": split_foo,
            },
        },
    }
    crawler = await crawl_items(mockserver, [{"foo": "bar1,bar2"}], settings)

    assert crawler.stats.get_value("item_scraped_count") == 1
    assert crawler.stats.get_value("feedexport/item_count/FileFeedStorage") == 2


# ---------------------------------------------------------------------------
# Ordering, isolation and strictness contract of FeedExporter
# ---------------------------------------------------------------------------

#: Records (kind, feed name, item["foo"]) for every filter and serializer
#: call, so tests can assert call order and call counts directly instead of
#: relying on log output.
EVENT_LOG: list[tuple[str, str, Any]] = []


class OrderRecordingFilter:
    """Filter that records every accepts() call and rejects given values."""

    def __init__(self, feed_options):
        self.name = feed_options["feed_name"]
        self.reject = set(feed_options.get("reject") or ())

    def accepts(self, item):
        EVENT_LOG.append(("filter", self.name, item.get("foo")))
        return item.get("foo") not in self.reject


class OrderRecordingExporter(JsonLinesItemExporter):
    """JsonLines exporter that records every export_item() call."""

    def __init__(self, file, feed_name=None, **kwargs):
        super().__init__(file, **kwargs)
        self.name = feed_name

    def export_item(self, item):
        EVENT_LOG.append(("serialize", self.name, item.get("foo")))
        return super().export_item(item)


def _order_settings(path_a: Path, path_b: Path, **feed_a_extra: Any) -> dict[str, Any]:
    return {
        "FEEDS": {
            path_a: {
                "format": "recording",
                "feed_name": "a",
                "item_filter": OrderRecordingFilter,
                "item_export_kwargs": {"feed_name": "a"},
                **feed_a_extra,
            },
            path_b: {
                "format": "recording",
                "feed_name": "b",
                "item_filter": OrderRecordingFilter,
                "item_export_kwargs": {"feed_name": "b"},
            },
        },
        "FEED_EXPORTERS": {"recording": OrderRecordingExporter},
    }


@coroutine_test
async def test_filter_runs_before_serializer_for_each_feed(
    mockserver: MockServer, tmp_path: Path
) -> None:
    """Each feed filters first and only serializes items it accepted."""
    EVENT_LOG.clear()
    path_a, path_b = unique_path(tmp_path), unique_path(tmp_path)
    items = [{"foo": "keep1"}, {"foo": "drop"}, {"foo": "keep2"}]
    settings = _order_settings(path_a, path_b, reject=["drop"])

    data = await export_by_path(mockserver, items, settings)

    assert EVENT_LOG == [
        ("filter", "a", "keep1"),
        ("serialize", "a", "keep1"),
        ("filter", "b", "keep1"),
        ("serialize", "b", "keep1"),
        ("filter", "a", "drop"),
        ("filter", "b", "drop"),
        ("serialize", "b", "drop"),
        ("filter", "a", "keep2"),
        ("serialize", "a", "keep2"),
        ("filter", "b", "keep2"),
        ("serialize", "b", "keep2"),
    ]
    assert data[path_a] == b'{"foo": "keep1"}\n{"foo": "keep2"}\n'
    assert data[path_b] == b'{"foo": "keep1"}\n{"foo": "drop"}\n{"foo": "keep2"}\n'


@coroutine_test
async def test_rejection_has_no_side_effects(
    mockserver: MockServer, tmp_path: Path
) -> None:
    """A rejected item is not serialized, not counted and creates no file."""
    EVENT_LOG.clear()
    path_a, path_b = unique_path(tmp_path), unique_path(tmp_path)
    items = [{"foo": "x"}, {"foo": "y"}]
    settings = _order_settings(path_a, path_b, reject=["x", "y"], store_empty=False)

    crawler = await crawl_items(mockserver, items, settings)

    # The serializer of feed "a" was never called.
    assert EVENT_LOG == [
        ("filter", "a", "x"),
        ("filter", "b", "x"),
        ("serialize", "b", "x"),
        ("filter", "a", "y"),
        ("filter", "b", "y"),
        ("serialize", "b", "y"),
    ]
    # No output file was created for the fully-rejecting feed.
    assert not path_a.exists()
    # Only actually exported items are counted.
    assert crawler.stats.get_value("feedexport/item_count/FileFeedStorage") == 2


class MutatingFilter:
    def __init__(self, feed_options):
        pass

    def accepts(self, item):
        item["foo"] = "FILTER_MUTATED"
        item["tags"].append("filter")
        return True


def mutating_processor(item):
    item["foo"] = "PROCESSOR_MUTATED"
    item["tags"].append("processor")
    return [item]


class MutatingExporter(JsonLinesItemExporter):
    def export_item(self, item):
        item["foo"] = "SERIALIZER_MUTATED"
        item["tags"].append("serializer")
        return super().export_item(item)


@coroutine_test
async def test_feeds_do_not_share_mutated_values(
    mockserver: MockServer, tmp_path: Path
) -> None:
    """In-place changes by one feed's filter, processor and serializer must
    not leak into other feeds or into the original item."""
    path_a, path_b = unique_path(tmp_path), unique_path(tmp_path)
    item = {"foo": "bar", "tags": ["orig"]}
    settings = {
        "FEEDS": {
            path_a: {
                "format": "mutating",
                "item_filter": MutatingFilter,
                "item_processor": mutating_processor,
            },
            path_b: {"format": "jsonlines"},
        },
        "FEED_EXPORTERS": {"mutating": MutatingExporter},
    }

    data = await export_by_path(mockserver, [item], settings)

    assert json.loads(data[path_a]) == {
        "foo": "SERIALIZER_MUTATED",
        "tags": ["orig", "filter", "processor", "serializer"],
    }
    assert json.loads(data[path_b]) == {"foo": "bar", "tags": ["orig"]}
    assert item == {"foo": "bar", "tags": ["orig"]}


class FailingExporter(JsonLinesItemExporter):
    def export_item(self, item):
        raise RuntimeError("boom")


@coroutine_test
async def test_failing_serializer_does_not_affect_other_feeds(
    mockserver: MockServer, tmp_path: Path
) -> None:
    """An exception in one feed's serializer skips the item for that feed
    only; other feeds still export it."""
    path_a, path_b = unique_path(tmp_path), unique_path(tmp_path)
    settings = {
        "FEEDS": {
            path_a: {"format": "failing"},
            path_b: {"format": "jsonlines"},
        },
        "FEED_EXPORTERS": {"failing": FailingExporter},
    }

    data = await export_by_path(mockserver, [{"foo": "bar"}], settings)

    assert json.loads(data[path_b]) == {"foo": "bar"}
    assert data.get(path_a) in (None, b"")


class RaisingFilter:
    def __init__(self, feed_options):
        pass

    def accepts(self, item):
        raise ValueError("filter boom")


@coroutine_test
async def test_failing_filter_does_not_affect_other_feeds(
    mockserver: MockServer, tmp_path: Path
) -> None:
    """An exception in one feed's filter (default mode) rejects the item for
    that feed only; other feeds still export it."""
    path_a, path_b = unique_path(tmp_path), unique_path(tmp_path)
    settings = {
        "FEEDS": {
            path_a: {
                "format": "jsonlines",
                "item_filter": RaisingFilter,
                "store_empty": False,
            },
            path_b: {"format": "jsonlines"},
        },
    }

    data = await export_by_path(mockserver, [{"foo": "bar"}], settings)

    assert json.loads(data[path_b]) == {"foo": "bar"}
    assert not path_a.exists()


@coroutine_test
async def test_duplicate_items_exported_to_each_feed(
    mockserver: MockServer, tmp_path: Path
) -> None:
    """The same item object scraped twice is exported twice by every feed."""
    path_a, path_b = unique_path(tmp_path), unique_path(tmp_path)
    item = {"foo": "bar"}
    settings = {
        "FEEDS": {
            path_a: {"format": "jsonlines"},
            path_b: {"format": "jsonlines"},
        },
    }

    data = await export_by_path(mockserver, [item, item], settings)

    assert data[path_a] == b'{"foo": "bar"}\n{"foo": "bar"}\n'
    assert data[path_b] == b'{"foo": "bar"}\n{"foo": "bar"}\n'


class EvenFilter:
    def __init__(self, feed_options):
        pass

    def accepts(self, item):
        return item["n"] % 2 == 0


@coroutine_test
async def test_batch_item_count_counts_only_exported_items(
    mockserver: MockServer, tmp_path: Path
) -> None:
    """Rejected items do not advance the batch counter of a feed."""
    batch_dir = tmp_path / "batches"
    settings = {
        "FEEDS": {
            batch_dir / "batch-%(batch_id)d.jl": {
                "format": "jsonlines",
                "batch_item_count": 2,
                "item_filter": EvenFilter,
            },
        },
    }

    await crawl_items(mockserver, [{"n": i} for i in range(6)], settings)

    batches = sorted(batch_dir.iterdir())
    assert [b.name for b in batches] == ["batch-1.jl", "batch-2.jl"]
    assert [json.loads(line) for line in batches[0].read_bytes().splitlines()] == [
        {"n": 0},
        {"n": 2},
    ]
    assert [json.loads(line) for line in batches[1].read_bytes().splitlines()] == [
        {"n": 4}
    ]


def _build_feed_exporter(settings: dict[str, Any]) -> tuple[FeedExporter, Spider]:
    crawler = get_crawler(Spider, settings)
    exporter = build_from_crawler(FeedExporter, crawler)
    spider = Spider.from_crawler(crawler, "default")
    exporter.open_spider(spider)
    return exporter, spider


class NonBoolFilter:
    def __init__(self, feed_options):
        pass

    def accepts(self, item):
        return 1


class AsyncFilter:
    def __init__(self, feed_options):
        pass

    async def accepts(self, item):
        return True


@coroutine_test
async def test_default_mode_accepts_truthy_filter_result(
    mockserver: MockServer, tmp_path: Path
) -> None:
    """Default mode keeps supporting custom filters returning truthy values."""
    path = unique_path(tmp_path)
    settings = {
        "FEEDS": {
            path: {"format": "jsonlines", "item_filter": NonBoolFilter},
        },
    }

    data = await export_by_path(mockserver, [{"foo": "bar"}], settings)

    assert json.loads(data[path]) == {"foo": "bar"}


@coroutine_test
async def test_strict_mode_rejects_non_bool_filter_result(tmp_path: Path) -> None:
    exporter, spider = _build_feed_exporter(
        {
            "FEED_EXPORT_FILTER_STRICT": True,
            "FEEDS": {
                printf_escape(path_to_url(unique_path(tmp_path))): {
                    "format": "jsonlines",
                    "item_filter": NonBoolFilter,
                },
            },
        }
    )

    with pytest.raises(TypeError, match="must return a bool"):
        await exporter.item_scraped({"foo": "bar"}, spider)


@pytest.mark.filterwarnings("error::RuntimeWarning")
@coroutine_test
async def test_strict_mode_rejects_awaitable_filter_result(tmp_path: Path) -> None:
    """An unawaited coroutine is rejected, not treated as True, and closed."""
    exporter, spider = _build_feed_exporter(
        {
            "FEED_EXPORT_FILTER_STRICT": True,
            "FEEDS": {
                printf_escape(path_to_url(unique_path(tmp_path))): {
                    "format": "jsonlines",
                    "item_filter": AsyncFilter,
                },
            },
        }
    )

    with pytest.raises(TypeError, match="awaitable"):
        await exporter.item_scraped({"foo": "bar"}, spider)


@coroutine_test
async def test_strict_mode_propagates_filter_exception(tmp_path: Path) -> None:
    exporter, spider = _build_feed_exporter(
        {
            "FEED_EXPORT_FILTER_STRICT": True,
            "FEEDS": {
                printf_escape(path_to_url(unique_path(tmp_path))): {
                    "format": "jsonlines",
                    "item_filter": RaisingFilter,
                },
            },
        }
    )

    with pytest.raises(ValueError, match="filter boom"):
        await exporter.item_scraped({"foo": "bar"}, spider)


@coroutine_test
async def test_close_spider_finalizes_feeds(tmp_path: Path) -> None:
    """close_spider stores accepted items of every feed and finalizes files."""
    EVENT_LOG.clear()
    path_a, path_b = unique_path(tmp_path), unique_path(tmp_path)
    exporter, spider = _build_feed_exporter(
        {
            "FEEDS": {
                printf_escape(path_to_url(path_a)): {
                    "format": "jsonlines",
                    "item_filter": OrderRecordingFilter,
                    "feed_name": "a",
                    "reject": ["drop"],
                },
                printf_escape(path_to_url(path_b)): {"format": "jsonlines"},
            },
        }
    )

    await exporter.item_scraped({"foo": "keep"}, spider)
    await exporter.item_scraped({"foo": "drop"}, spider)
    await exporter.close_spider(spider)

    assert path_a.read_bytes() == b'{"foo": "keep"}\n'
    assert path_b.read_bytes() == b'{"foo": "keep"}\n{"foo": "drop"}\n'
