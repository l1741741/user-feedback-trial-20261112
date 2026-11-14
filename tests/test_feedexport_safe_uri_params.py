from __future__ import annotations

from io import BytesIO
from typing import IO, TYPE_CHECKING, Any

import pytest

import scrapy
from scrapy.extensions.feedexport import FeedExporter, apply_safe_uri_params
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test
from tests.utils.feedexport import MyItem

if TYPE_CHECKING:
    from pathlib import Path

    from scrapy.crawler import Crawler
    from tests.mockserver.http import MockServer


class RecordingStorage:
    """Feed storage that records the URIs it is built with and the bytes
    stored through it, without touching the filesystem."""

    supported_modes = frozenset({"append", "create", "overwrite"})
    instances: list[RecordingStorage] = []

    def __init__(self, uri: str, *, feed_options: dict[str, Any] | None = None):
        self.uri = uri
        self.stored: bytes | None = None
        self.instances.append(self)

    def open(self, spider: scrapy.Spider) -> IO[bytes]:
        return BytesIO()

    def store(self, file: IO[bytes]) -> None:
        file.seek(0)
        self.stored = file.read()
        file.close()

    @classmethod
    def stored_uris(cls) -> list[str]:
        # Skip the instance built from the raw URI template when feed
        # support is checked.
        return [i.uri for i in cls.instances if "%(" not in i.uri]


@pytest.fixture(autouse=True)
def _reset_recording_storage():
    RecordingStorage.instances = []


class TestApplySafeUriParams:
    params: dict[str, Any] = {
        "name": "myspider",
        "tenant": "租户/甲%",
        "time": "2020-01-01T00-00-00",
        "batch_id": 2,
        "batch_time": "2020-01-01T00-00-00.123456",
    }

    @pytest.mark.parametrize(
        ("uri_template", "allowed", "expected"),
        [
            # Non-ASCII characters, separators and percent signs in a
            # whitelisted field are encoded into a single path segment.
            (
                "/data/%(tenant)s/feed.json",
                ["tenant"],
                "/data/%E7%A7%9F%E6%88%B7%2F%E7%94%B2%25/feed.json",
            ),
            # Path segments use percent-encoding, query parameters use
            # form-urlencoding (e.g. a space becomes ``+``).
            (
                "/data/%(name)s.json?q=%(name)s",
                ["name"],
                "/data/myspider.json?q=myspider",
            ),
            (
                "file:///o/%(tenant)s.json",
                ["tenant"],
                "file:///o/%E7%A7%9F%E6%88%B7%2F%E7%94%B2%25.json",
            ),
            # Built-in fields are always allowed, with printf formatting.
            ("/data/%(batch_id)05d.json", [], "/data/00002.json"),
            ("/data/%(time)s.json", [], "/data/2020-01-01T00-00-00.json"),
            ("/data/%(batch_time)s.json", [], "/data/2020-01-01T00-00-00.123456.json"),
            # Percent characters elsewhere in the template stay literal.
            ("/100%/%(name)s.json", ["name"], "/100%/myspider.json"),
            (
                "file:///path%20with%20spaces/%(name)s.json",
                ["name"],
                "file:///path%20with%20spaces/myspider.json",
            ),
        ],
    )
    def test_encoding(self, uri_template: str, allowed: list[str], expected: str):
        assert apply_safe_uri_params(uri_template, self.params, allowed) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # A percent-encoded slash in the input is encoded again, so it
            # cannot become a separator after one decoding round.
            ("%2F", "%252F"),
            ("%2e%2e%2f", "%252e%252e%252f"),
            # Separators cannot split the segment.
            ("a/b", "a%2Fb"),
            ("a\\b", "a%5Cb"),
            # A value that looks like a placeholder is inert data.
            ("%(batch_id)d", "%25%28batch_id%29d"),
        ],
    )
    def test_value_encoding(self, value: str, expected: str):
        params = {**self.params, "tenant": value}
        assert (
            apply_safe_uri_params("/data/%(tenant)s.json", params, ["tenant"])
            == f"/data/{expected}.json"
        )

    def test_query_value_encoding(self):
        params = {**self.params, "tenant": "a b&c=d"}
        assert (
            apply_safe_uri_params("/data/f.json?q=%(tenant)s", params, ["tenant"])
            == "/data/f.json?q=a+b%26c%3Dd"
        )

    @pytest.mark.parametrize(
        "uri_template",
        [
            "/data/%(unknown)s.json",  # not whitelisted
            "/data/%(tenant)s/%(other)s.json",  # only tenant is whitelisted
        ],
    )
    def test_unknown_placeholder_rejected(self, uri_template: str):
        with pytest.raises(ValueError, match="safe_uri_params"):
            apply_safe_uri_params(uri_template, self.params, ["tenant"])

    def test_missing_field_rejected(self):
        with pytest.raises(ValueError, match="no value"):
            apply_safe_uri_params("/data/%(tenant)s.json", {"name": "x"}, ["tenant"])

    @pytest.mark.parametrize(
        "value",
        [
            ".",
            "..",
            "https://evil.example.com/x",
            "file:///etc/passwd",
        ],
    )
    def test_dangerous_path_value_rejected(self, value: str):
        params = {**self.params, "tenant": value}
        with pytest.raises(ValueError):
            apply_safe_uri_params("/data/%(tenant)s.json", params, ["tenant"])

    def test_params_not_modified(self):
        params = dict(self.params)
        snapshot = dict(params)
        apply_safe_uri_params("/data/%(tenant)s.json", params, ["tenant"])
        assert params == snapshot


def _get_feed_exporter(settings: dict[str, Any]) -> tuple[Crawler, FeedExporter]:
    crawler = get_crawler(settings_dict=settings)
    feed_exporter = build_from_crawler(FeedExporter, crawler)
    return crawler, feed_exporter


def _make_spider(crawler: Crawler, **attributes: Any) -> scrapy.Spider:
    spider = scrapy.Spider.from_crawler(crawler, "tenant_spider")
    for name, value in attributes.items():
        setattr(spider, name, value)
    return spider


class TestSafeUriParamsFeedOption:
    def test_default_templates_unchanged(self):
        # Without the safe_uri_params feed option, placeholders are
        # substituted verbatim, as before.
        settings = {
            "FEEDS": {
                "file:///tmp/%(tenant)s.json": {"format": "json"},
            },
        }
        crawler, feed_exporter = _get_feed_exporter(settings)
        spider = _make_spider(crawler, tenant="a/b%")
        feed_exporter.open_spider(spider)
        assert feed_exporter.slots[0].uri == "file:///tmp/a/b%.json"

    def test_spider_attribute_encoded(self):
        settings = {
            "FEEDS": {
                "file:///tmp/feeds/%(tenant)s/%(batch_id)02d.json": {
                    "format": "json",
                    "safe_uri_params": ["tenant"],
                },
            },
        }
        crawler, feed_exporter = _get_feed_exporter(settings)
        spider = _make_spider(crawler, tenant="租户/甲%")
        feed_exporter.open_spider(spider)
        assert (
            feed_exporter.slots[0].uri
            == "file:///tmp/feeds/%E7%A7%9F%E6%88%B7%2F%E7%94%B2%25/01.json"
        )
        # The spider attribute itself is left untouched.
        assert spider.tenant == "租户/甲%"

    def test_uri_params_callback_kept_and_not_modified(self):
        returned: dict[str, Any] = {}

        def uri_params(params, spider):
            returned.update({**params, "tenant": "a/b"})
            return returned

        settings = {
            "FEEDS": {
                "file:///tmp/%(tenant)s.json": {
                    "format": "json",
                    "safe_uri_params": ["tenant"],
                    "uri_params": uri_params,
                },
            },
        }
        crawler, feed_exporter = _get_feed_exporter(settings)
        spider = _make_spider(crawler)
        feed_exporter.open_spider(spider)
        assert feed_exporter.slots[0].uri == "file:///tmp/a%2Fb.json"
        # The dict returned by the callback was not modified in place.
        assert returned["tenant"] == "a/b"

    def test_error_before_storage_creation(self, tmp_path: Path):
        # A rejected value must prevent the storage from being created, so
        # no directory is created anywhere, inside or outside tmp_path.
        settings = {
            "FEEDS": {
                str(tmp_path / "feeds" / "%(tenant)s" / "out.json"): {
                    "format": "json",
                    "safe_uri_params": ["tenant"],
                },
            },
        }
        crawler, feed_exporter = _get_feed_exporter(settings)
        spider = _make_spider(crawler, tenant="..")
        with pytest.raises(ValueError, match="path segment"):
            feed_exporter.open_spider(spider)
        assert feed_exporter.slots == []
        assert list(tmp_path.iterdir()) == []

    def test_unknown_placeholder_error_before_storage_creation(
        self, tmp_path: Path
    ):
        settings = {
            "FEEDS": {
                str(tmp_path / "feeds" / "%(tenant)s.json"): {
                    "format": "json",
                    "safe_uri_params": [],
                },
            },
        }
        crawler, feed_exporter = _get_feed_exporter(settings)
        spider = _make_spider(crawler, tenant="acme")
        with pytest.raises(ValueError, match="safe_uri_params"):
            feed_exporter.open_spider(spider)
        assert feed_exporter.slots == []
        assert list(tmp_path.iterdir()) == []


@coroutine_test
async def test_safe_uri_params_multiple_batches(
    mockserver: MockServer, tmp_path: Path
) -> None:
    items = [MyItem({"foo": f"bar{i}"}) for i in range(3)]
    settings = {
        "FEED_STORAGES": {
            "recording": "tests.test_feedexport_safe_uri_params.RecordingStorage",
        },
        "FEEDS": {
            "recording://bucket/%(tenant)s/%(batch_id)02d.jl": {
                "format": "jsonlines",
                "safe_uri_params": ["tenant"],
                "batch_item_count": 2,
            },
        },
    }

    class TenantSpider(scrapy.Spider):
        name = "tenant_spider"
        tenant = "租户/甲%"
        start_urls = [mockserver.url("/")]

        def parse(self, response):
            yield from items

    crawler = get_crawler(TenantSpider, settings)
    await crawler.crawl_async()

    encoded_tenant = "%E7%A7%9F%E6%88%B7%2F%E7%94%B2%25"
    assert RecordingStorage.stored_uris() == [
        f"recording://bucket/{encoded_tenant}/01.jl",
        f"recording://bucket/{encoded_tenant}/02.jl",
    ]
    batches = [i for i in RecordingStorage.instances if "%(" not in i.uri]
    assert [i.stored for i in batches] == [
        b'{"foo": "bar0"}\n{"foo": "bar1"}\n',
        b'{"foo": "bar2"}\n',
    ]


@coroutine_test
async def test_safe_uri_params_error_creates_no_directory(
    mockserver: MockServer, tmp_path: Path
) -> None:
    items = [MyItem({"foo": "bar"})]
    settings = {
        "FEEDS": {
            str(tmp_path / "feeds" / "%(tenant)s" / "out.jl"): {
                "format": "jsonlines",
                "safe_uri_params": ["tenant"],
            },
        },
    }

    class EvilSpider(scrapy.Spider):
        name = "evil_spider"
        tenant = "https://evil.example.com/x"
        start_urls = [mockserver.url("/")]

        def parse(self, response):
            yield from items

    crawler = get_crawler(EvilSpider, settings)
    await crawler.crawl_async()

    assert list(tmp_path.iterdir()) == []
