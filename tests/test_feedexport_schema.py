from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from scrapy import Field, Item
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test
from tests.utils.feedexport import (
    PARSERS,
    crawl_items,
    csv_header,
    export_by_format,
    unique_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.mockserver.http import MockServer


class SerializedItem(Item):
    foo = Field()
    egg = Field()


@dataclass
class DataClassItem:
    foo: str
    egg: str | None = None


def _schema_feed(
    tmp_path: Path,
    fmt: str = "jsonlines",
    **options: Any,
) -> dict[Any, dict[str, Any]]:
    return {unique_path(tmp_path): {"format": fmt, **options}}


def _rejected_count(crawler: Any) -> Any:
    return crawler.stats.get_value("feedexport/schema_rejected/FileFeedStorage")


class TestSchemaConfig:
    @pytest.mark.parametrize(
        "options",
        [
            {"schema_fields": ["a", "a"]},
            {"schema_fields": []},
            {"schema_fields": ("a", "")},
            {"schema_fields": [1]},
            {"schema_fields": "a"},
            {"schema_fields": {"a": "b"}},
            {"schema_fields": ["a"], "schema_missing": "drop"},
            {"schema_fields": ["a"], "schema_extra": "null"},
            {"schema_fields": ["a"], "schema_missing": "wat"},
            {"schema_fields": ["a"], "schema_extra": "wat"},
        ],
    )
    def test_invalid_config(self, options: dict[str, Any]) -> None:
        settings = {"FEEDS": {"file:///tmp/items.json": {"format": "json", **options}}}
        with pytest.raises(ValueError, match="schema_"):
            get_crawler(settings_dict=settings)

    @pytest.mark.parametrize(
        "options",
        [
            {"schema_fields": ["a", "b"]},
            {"schema_fields": ("a", "b"), "schema_missing": "null"},
            {"schema_fields": ["a"], "schema_extra": "drop"},
            {
                "schema_fields": ["a"],
                "schema_missing": "null",
                "schema_extra": "drop",
            },
        ],
    )
    def test_valid_config(self, options: dict[str, Any]) -> None:
        settings = {"FEEDS": {"file:///tmp/items.json": {"format": "json", **options}}}
        get_crawler(settings_dict=settings)

    def test_policy_without_schema_is_ignored(self) -> None:
        # No schema_fields: the feed keeps its historical behavior even if a
        # policy key is present.
        settings = {
            "FEEDS": {
                "file:///tmp/items.json": {
                    "format": "json",
                    "schema_missing": "null",
                    "schema_extra": "drop",
                }
            }
        }
        get_crawler(settings_dict=settings)


class TestMissingPolicy:
    @pytest.mark.parametrize("fmt", ["jsonlines", "csv"])
    @coroutine_test
    async def test_missing_error_default(
        self, fmt: str, mockserver: MockServer, tmp_path: Path
    ) -> None:
        settings = {"FEEDS": _schema_feed(tmp_path, fmt, schema_fields=["foo", "egg"])}
        data = (
            await export_by_format(
                mockserver,
                [{"foo": "bar1", "egg": "spam1"}, {"foo": "bar2"}],
                settings,
            )
        )[fmt]
        assert data is not None
        if fmt == "csv":
            assert csv_header(data) == ["foo", "egg"]
        assert PARSERS[fmt](data) == [{"foo": "bar1", "egg": "spam1"}]

    @pytest.mark.parametrize("fmt", list(PARSERS))
    @coroutine_test
    async def test_missing_null_fills_values(
        self, fmt: str, mockserver: MockServer, tmp_path: Path
    ) -> None:
        settings = {
            "FEEDS": _schema_feed(
                tmp_path,
                fmt,
                schema_fields=["foo", "egg"],
                schema_missing="null",
                schema_extra="drop",
            )
        }
        data = (
            await export_by_format(
                mockserver,
                [{"foo": "bar1", "egg": "spam1"}, {"foo": "bar2"}],
                settings,
            )
        )[fmt]
        assert data is not None
        rows = PARSERS[fmt](data)
        assert rows[0] == {"foo": "bar1", "egg": "spam1"}
        # CSV cannot represent a null value; the other formats keep the key.
        if fmt == "csv":
            assert rows[1] == {"foo": "bar2"}
        else:
            assert rows[1] == {"foo": "bar2", "egg": None}

    @coroutine_test
    async def test_missing_null_does_not_mutate_item(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        original = SerializedItem({"foo": "bar"})
        settings = {
            "FEEDS": _schema_feed(
                tmp_path,
                "jsonlines",
                schema_fields=["foo", "egg"],
                schema_missing="null",
                schema_extra="drop",
            )
        }
        await crawl_items(mockserver, [original], settings)
        assert dict(original) == {"foo": "bar"}

    @coroutine_test
    async def test_missing_null_overrides_export_empty_fields_kwarg(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        # The null policy must fill missing fields even if the generic
        # export_empty_fields exporter kwarg says otherwise.
        path = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                path: {
                    "format": "jsonlines",
                    "schema_fields": ["foo", "egg"],
                    "schema_missing": "null",
                    "schema_extra": "drop",
                    "item_export_kwargs": {"export_empty_fields": False},
                }
            }
        }
        await crawl_items(mockserver, [{"foo": "bar"}], settings)
        assert PARSERS["jsonlines"](path.read_bytes()) == [{"foo": "bar", "egg": None}]


class TestExtraPolicy:
    @pytest.mark.parametrize("fmt", ["jsonlines", "csv"])
    @coroutine_test
    async def test_extra_error_default(
        self, fmt: str, mockserver: MockServer, tmp_path: Path
    ) -> None:
        settings = {"FEEDS": _schema_feed(tmp_path, fmt, schema_fields=["foo", "egg"])}
        data = (
            await export_by_format(
                mockserver,
                [
                    {"foo": "bar1", "egg": "spam1"},
                    {"foo": "bar2", "egg": "spam2", "unexpected": True},
                    {"foo": "bar3", "egg": "spam3"},
                ],
                settings,
            )
        )[fmt]
        assert data is not None
        assert PARSERS[fmt](data) == [
            {"foo": "bar1", "egg": "spam1"},
            # The rejected item leaves no line, later valid items still export.
            {"foo": "bar3", "egg": "spam3"},
        ]

    @pytest.mark.parametrize("fmt", list(PARSERS))
    @coroutine_test
    async def test_extra_drop(
        self, fmt: str, mockserver: MockServer, tmp_path: Path
    ) -> None:
        settings = {
            "FEEDS": _schema_feed(
                tmp_path,
                fmt,
                schema_fields=["foo", "egg"],
                schema_extra="drop",
            )
        }
        data = (
            await export_by_format(
                mockserver,
                [
                    {"foo": "bar1", "egg": "spam1", "unexpected": True},
                    {"foo": "bar2", "egg": "spam2"},
                ],
                settings,
            )
        )[fmt]
        assert data is not None
        assert PARSERS[fmt](data) == [
            {"foo": "bar1", "egg": "spam1"},
            {"foo": "bar2", "egg": "spam2"},
        ]


class TestSchemaItems:
    @pytest.mark.parametrize("item_cls", [dict, SerializedItem, DataClassItem])
    @coroutine_test
    async def test_item_types(
        self,
        item_cls: type[Any],
        mockserver: MockServer,
        tmp_path: Path,
    ) -> None:
        items: list[Any]
        if item_cls is DataClassItem:
            items = [DataClassItem(foo="bar1", egg="spam1"), DataClassItem(foo="bar2")]
        else:
            items = [
                item_cls({"foo": "bar1", "egg": "spam1"}),
                item_cls({"foo": "bar2"}),
            ]
        settings = {
            "FEEDS": _schema_feed(
                tmp_path,
                "jsonlines",
                schema_fields=["foo", "egg"],
                schema_missing="null",
                schema_extra="drop",
            )
        }
        data = (await export_by_format(mockserver, items, settings))["jsonlines"]
        assert data is not None
        assert PARSERS["jsonlines"](data) == [
            {"foo": "bar1", "egg": "spam1"},
            {"foo": "bar2", "egg": None},
        ]

    @coroutine_test
    async def test_empty_item_rejected(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        path = unique_path(tmp_path)
        settings = {"FEEDS": {path: {"format": "jsonlines", "schema_fields": ["foo"]}}}
        crawler = await crawl_items(mockserver, [{}, {"foo": "bar"}], settings)
        # The empty item is rejected, only the valid item is written.
        assert PARSERS["jsonlines"](path.read_bytes()) == [{"foo": "bar"}]
        assert _rejected_count(crawler) == 1

    @coroutine_test
    async def test_field_serializer_runs_once(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        calls: list[str] = []

        def serializer(value: str) -> str:
            calls.append(value)
            return value.upper()

        class ItemWithSerializer(Item):
            foo = Field(serializer=serializer)
            egg = Field()

        settings = {
            "FEEDS": _schema_feed(
                tmp_path,
                "jsonlines",
                schema_fields=["foo", "egg"],
                schema_missing="null",
                schema_extra="drop",
            )
        }
        data = (
            await export_by_format(
                mockserver,
                [
                    ItemWithSerializer({"foo": "bar", "egg": "spam"}),
                    ItemWithSerializer({"foo": "baz"}),
                ],
                settings,
            )
        )["jsonlines"]
        assert data is not None
        assert PARSERS["jsonlines"](data) == [
            {"foo": "BAR", "egg": "spam"},
            {"foo": "BAZ", "egg": None},
        ]
        # Validation must not invoke the serializer; every value serializes once.
        assert calls == ["bar", "baz"]


class TestSchemaFeeds:
    @coroutine_test
    async def test_feed_isolation(self, mockserver: MockServer, tmp_path: Path) -> None:
        # The strict feed rejects the second item; the permissive feed and the
        # pipeline stats must not be affected.
        strict_path = unique_path(tmp_path)
        permissive_path = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                strict_path: {
                    "format": "jsonlines",
                    "schema_fields": ["foo", "egg"],
                },
                permissive_path: {
                    "format": "jsonlines",
                    "schema_fields": ["foo"],
                    "schema_missing": "null",
                    "schema_extra": "drop",
                },
            }
        }
        crawler = await crawl_items(
            mockserver,
            [
                {"foo": "bar1", "egg": "spam1"},
                {"foo": "bar2"},
            ],
            settings,
        )
        assert PARSERS["jsonlines"](strict_path.read_bytes()) == [
            {"foo": "bar1", "egg": "spam1"}
        ]
        assert PARSERS["jsonlines"](permissive_path.read_bytes()) == [
            {"foo": "bar1"},
            {"foo": "bar2"},
        ]
        assert crawler.stats.get_value("item_scraped_count") == 2
        assert _rejected_count(crawler) == 1

    @coroutine_test
    async def test_drop_scoped_to_feed(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        # The same item is dropped in one feed and fully kept in another.
        dropping_path = unique_path(tmp_path)
        keeping_path = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                dropping_path: {
                    "format": "jsonlines",
                    "schema_fields": ["foo"],
                    "schema_extra": "drop",
                },
                keeping_path: {"format": "jsonlines"},
            }
        }
        await crawl_items(mockserver, [{"foo": "bar", "extra": "kept-here"}], settings)
        assert PARSERS["jsonlines"](dropping_path.read_bytes()) == [{"foo": "bar"}]
        assert PARSERS["jsonlines"](keeping_path.read_bytes()) == [
            {"foo": "bar", "extra": "kept-here"}
        ]

    @coroutine_test
    async def test_rejection_logged(
        self,
        caplog: pytest.LogCaptureFixture,
        mockserver: MockServer,
        tmp_path: Path,
    ) -> None:
        settings = {"FEEDS": _schema_feed(tmp_path, "jsonlines", schema_fields=["foo"])}
        with caplog.at_level(logging.ERROR):
            await crawl_items(mockserver, [{"egg": "spam"}], settings)
        assert "rejected by the schema" in caplog.text
        assert "missing schema field(s): foo" in caplog.text

    @coroutine_test
    async def test_schema_fields_take_precedence(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        settings = {
            "FEED_EXPORT_FIELDS": ["old"],
            "FEEDS": {
                unique_path(tmp_path): {
                    "format": "jsonlines",
                    "fields": ["middle"],
                    "schema_fields": ["foo", "egg"],
                    "schema_missing": "null",
                    "schema_extra": "drop",
                }
            },
        }
        data = (
            await export_by_format(
                mockserver,
                [{"foo": "bar", "egg": "spam", "middle": 1, "old": 2}],
                settings,
            )
        )["jsonlines"]
        assert data is not None
        assert PARSERS["jsonlines"](data) == [{"foo": "bar", "egg": "spam"}]

    @coroutine_test
    async def test_field_order(self, mockserver: MockServer, tmp_path: Path) -> None:
        settings = {
            "FEEDS": _schema_feed(
                tmp_path,
                "jsonlines",
                schema_fields=["egg", "foo"],
                schema_missing="null",
                schema_extra="drop",
            )
        }
        data = (
            await export_by_format(
                mockserver, [{"foo": "bar", "egg": "spam"}], settings
            )
        )["jsonlines"]
        assert data is not None
        assert list(PARSERS["jsonlines"](data)[0]) == ["egg", "foo"]

    @coroutine_test
    async def test_processor_outputs_validated_independently(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        def processor(item: Any):
            yield {"foo": item["foo"]}  # missing egg -> rejected for this feed
            yield {"foo": item["foo"], "egg": item["egg"]}  # valid

        path = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                path: {
                    "format": "jsonlines",
                    "schema_fields": ["foo", "egg"],
                    "item_processor": processor,
                }
            }
        }
        crawler = await crawl_items(mockserver, [{"foo": "a", "egg": "b"}], settings)
        # Rejecting one processor output must not skip the next one.
        assert PARSERS["jsonlines"](path.read_bytes()) == [{"foo": "a", "egg": "b"}]
        assert _rejected_count(crawler) == 1


class TestSchemaBatches:
    @coroutine_test
    async def test_schema_stable_across_batches(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        feed_dir = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                feed_dir / "batch_%(batch_id)d.jsonl": {
                    "format": "jsonlines",
                    "schema_fields": ["foo", "egg"],
                    "schema_missing": "null",
                    "schema_extra": "drop",
                    "batch_item_count": 2,
                }
            }
        }
        await crawl_items(
            mockserver,
            [
                {"foo": "a", "egg": "1", "extra": "x"},
                {"foo": "b"},
                {"foo": "c", "egg": "3"},
                {"foo": "d", "egg": "4", "extra": "y"},
            ],
            settings,
        )
        batches = sorted(feed_dir.iterdir())
        assert [p.name for p in batches] == ["batch_1.jsonl", "batch_2.jsonl"]
        assert PARSERS["jsonlines"](batches[0].read_bytes()) == [
            {"foo": "a", "egg": "1"},
            {"foo": "b", "egg": None},
        ]
        assert PARSERS["jsonlines"](batches[1].read_bytes()) == [
            {"foo": "c", "egg": "3"},
            {"foo": "d", "egg": "4"},
        ]

    @coroutine_test
    async def test_rejected_item_does_not_fill_batch(
        self, mockserver: MockServer, tmp_path: Path
    ) -> None:
        # Rejected items count nothing, so only accepted items fill batches and
        # contribute to the stored item counts.
        feed_dir = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                feed_dir / "batch_%(batch_id)d.jsonl": {
                    "format": "jsonlines",
                    "schema_fields": ["foo"],
                    "batch_item_count": 1,
                }
            }
        }
        crawler = await crawl_items(
            mockserver,
            [{"egg": "bad"}, {"foo": "good1"}, {"foo": "good2"}],
            settings,
        )
        batches = sorted(feed_dir.iterdir())
        assert [p.name for p in batches] == ["batch_1.jsonl", "batch_2.jsonl"]
        assert [PARSERS["jsonlines"](p.read_bytes()) for p in batches] == [
            [{"foo": "good1"}],
            [{"foo": "good2"}],
        ]
        assert crawler.stats.get_value("feedexport/item_count/FileFeedStorage") == 2
        assert _rejected_count(crawler) == 1


class TestSchemaSerializerFailure:
    @coroutine_test
    async def test_serializer_exception_leaves_no_partial_line(
        self,
        caplog: pytest.LogCaptureFixture,
        mockserver: MockServer,
        tmp_path: Path,
    ) -> None:
        def broken_serializer(value: str) -> str:
            raise RuntimeError("boom")

        class BrokenItem(Item):
            foo = Field(serializer=broken_serializer)
            egg = Field()

        path = unique_path(tmp_path)
        settings = {
            "FEEDS": {
                path: {
                    "format": "jsonlines",
                    "schema_fields": ["foo", "egg"],
                    "schema_extra": "drop",
                }
            }
        }
        with caplog.at_level(logging.ERROR):
            crawler = await crawl_items(
                mockserver,
                [
                    BrokenItem({"foo": "bad", "egg": "spam"}),
                    {"foo": "good", "egg": "spam2"},
                ],
                settings,
            )
        # The serializer of the first item raised inside the exporter. The
        # exception is logged by the signal machinery and the failing item
        # leaves no line of its own; later valid items still export.
        assert PARSERS["jsonlines"](path.read_bytes()) == [
            {"foo": "good", "egg": "spam2"}
        ]
        assert crawler.stats.get_value("feedexport/item_count/FileFeedStorage") == 1
        assert "RuntimeError" in caplog.text
