from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from twisted.python.failure import Failure

from scrapy import Request, Spider
from scrapy.core.spidermw import SpiderMiddlewareManager
from scrapy.http import Response
from scrapy.spidermiddlewares.depth import DepthMiddleware
from scrapy.utils.asyncgen import as_async_generator, collect_asyncgen
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Generator

    from scrapy.crawler import Crawler
    from scrapy.statscollectors import StatsCollector


@pytest.fixture
def crawler() -> Crawler:
    return get_crawler(Spider, {"DEPTH_LIMIT": 1, "DEPTH_STATS_VERBOSE": True})


@pytest.fixture
def stats(crawler: Crawler) -> Generator[StatsCollector]:
    crawler.stats.open_spider()

    yield crawler.stats

    crawler.stats.close_spider()


@pytest.fixture
def mw(crawler: Crawler) -> DepthMiddleware:
    return build_from_crawler(DepthMiddleware, crawler)


def make_crawler(**settings: Any) -> Crawler:
    crawler = get_crawler(Spider, {"DEPTH_STATS_VERBOSE": True, **settings})
    crawler.stats.open_spider()
    return crawler


def make_mw(**settings: Any) -> tuple[DepthMiddleware, StatsCollector]:
    crawler = make_crawler(**settings)
    return build_from_crawler(DepthMiddleware, crawler), crawler.stats


def response_for(request: Request) -> Response:
    return Response(request.url, request=request)


def process(
    mw: DepthMiddleware, response: Response | None, requests: list[Request]
) -> list[Request]:
    return list(mw.process_spider_output(response, requests))


async def process_async(
    mw: DepthMiddleware, response: Response | None, requests: list[Request]
) -> list[Request]:
    return await collect_asyncgen(
        mw.process_spider_output_async(response, as_async_generator(requests))
    )


async def process_start(mw: DepthMiddleware, requests: list[Any]) -> list[Any]:
    return await collect_asyncgen(mw.process_start(as_async_generator(requests)))


def test_process_spider_output(mw: DepthMiddleware, stats: StatsCollector) -> None:
    req = Request("http://scrapytest.org")
    resp = Response("http://scrapytest.org")
    resp.request = req
    result = [Request("http://scrapytest.org")]

    out = list(mw.process_spider_output(resp, result))
    assert out == result

    rdc = stats.get_value("request_depth_count/1")
    assert rdc == 1

    req.meta["depth"] = 1

    out2 = list(mw.process_spider_output(resp, result))
    assert not out2

    rdm = stats.get_value("request_depth_max")
    assert rdm == 1


def test_depth_reset(mw: DepthMiddleware, stats: StatsCollector) -> None:
    resp = Response("https://example.com")
    resp.request = Request("https://example.com", meta={"depth": 5})
    result = [Request("https://example.com", meta={"depth_reset": True})]

    out = list(mw.process_spider_output(resp, result))

    assert out == result
    assert out[0].meta["depth"] == 0
    assert "depth_reset" not in out[0].meta
    assert stats.get_value("request_depth_count/0") == 1


def test_process_spider_output_no_response(
    mw: DepthMiddleware, stats: StatsCollector
) -> None:
    result = [Request("https://example.com")]

    out = list(mw.process_spider_output(None, result))
    assert out == result
    assert "depth" not in out[0].meta
    assert stats.get_value("request_depth_count/0") is None


@coroutine_test
async def test_process_spider_output_async_no_response(
    mw: DepthMiddleware, stats: StatsCollector
) -> None:
    result = [Request("https://example.com")]

    out = await collect_asyncgen(
        mw.process_spider_output_async(None, as_async_generator(result))
    )
    assert out == result
    assert "depth" not in out[0].meta
    assert stats.get_value("request_depth_count/0") is None


def test_ignored_logged_once(
    mw: DepthMiddleware, stats: StatsCollector, caplog: pytest.LogCaptureFixture
) -> None:
    resp = Response("http://example.com")
    resp.request = Request("http://example.com")
    resp.meta["depth"] = 1
    result = [Request(f"http://example.com/{i}") for i in range(3)]

    with caplog.at_level("DEBUG", logger="scrapy.spidermiddlewares.depth"):
        assert not list(mw.process_spider_output(resp, result))

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert "http://example.com/0" in messages[0]
    assert stats.get_value("depth/request_ignored_count") == 3


def test_priority_and_non_verbose_stats() -> None:
    crawler = get_crawler(
        Spider,
        {"DEPTH_LIMIT": 0, "DEPTH_STATS_VERBOSE": False, "DEPTH_PRIORITY": 10},
    )
    crawler.stats.open_spider()
    try:
        mw = build_from_crawler(DepthMiddleware, crawler)
        resp = Response("http://toscrape.com")
        resp.request = Request("http://toscrape.com")
        resp.request.meta["depth"] = 2
        out = list(mw.process_spider_output(resp, [Request("http://toscrape.com")]))
        assert len(out) == 1
        # priority is decremented by depth * DEPTH_PRIORITY
        assert out[0].priority == -30
        assert out[0].meta["depth"] == 3
        # non-verbose stats don't track per-depth counts but still track the max
        assert crawler.stats.get_value("request_depth_count/3") is None
        assert crawler.stats.get_value("request_depth_max") == 3
    finally:
        crawler.stats.close_spider()


# Configuration


def test_seed_policy_defaults_to_legacy() -> None:
    mw, _ = make_mw()
    assert mw.seed_policy == "legacy"


@pytest.mark.parametrize("value", ["", "Legacy", "ZERO", "keep", 0, 1, 0.0, ["zero"]])
def test_invalid_seed_policy_setting(value: Any) -> None:
    crawler = make_crawler(DEPTH_SEED_POLICY=value)
    with pytest.raises(ValueError, match="Invalid DEPTH_SEED_POLICY"):
        build_from_crawler(DepthMiddleware, crawler)


def test_invalid_seed_policy_argument() -> None:
    crawler = make_crawler()
    with pytest.raises(ValueError, match="Invalid DEPTH_SEED_POLICY"):
        DepthMiddleware(0, crawler.stats, seed_policy="bogus")


# zero


@coroutine_test
async def test_zero_seed_sync_and_async_start() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="zero", DEPTH_PRIORITY=10)
    seed = Request(
        "https://example.com",
        meta={"depth": 5, "foo": "bar"},
        callback=lambda response: None,
        errback=lambda failure: None,
        flags=["flag"],
        cb_kwargs={"key": "value"},
        body=b"body",
        priority=7,
    )

    sync_out = process(mw, None, [seed])
    async_out = await process_async(mw, None, [seed])
    assert sync_out == async_out

    for out in (sync_out, async_out):
        assert len(out) == 1
        seeded = out[0]
        assert seeded is not seed
        assert seeded.meta["depth"] == 0
        assert seeded.meta["foo"] == "bar"
        assert seeded.priority == 7
        assert seeded.callback is seed.callback
        assert seeded.errback is seed.errback
        assert seeded.flags == ["flag"]
        assert seeded.cb_kwargs == {"key": "value"}
        assert seeded.body == b"body"
        # The request owned by the caller is left untouched.
        assert seed.meta["depth"] == 5
        assert seed.priority == 7

    # Both chains returned the same result, counted once.
    assert sync_out[0] is async_out[0]
    assert stats.get_value("request_depth_count/0") == 1
    assert stats.get_value("request_depth_max") == 0


@coroutine_test
async def test_zero_does_not_modify_caller_meta() -> None:
    mw, _ = make_mw(DEPTH_SEED_POLICY="zero")
    meta = {"depth": 4, "nested": {"value": 1}}
    seed = Request("https://example.com", meta=meta)

    out = await process_start(mw, [seed])

    assert out[0].meta["depth"] == 0
    assert out[0].meta["nested"] is meta["nested"]
    assert meta == {"depth": 4, "nested": {"value": 1}}


@coroutine_test
async def test_zero_start_without_depth() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="zero")
    seed = Request("https://example.com")

    out = await process_start(mw, [seed])

    assert out[0].meta["depth"] == 0
    assert stats.get_value("request_depth_count/0") == 1


@coroutine_test
async def test_empty_start() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="zero")

    assert await process_start(mw, []) == []
    assert stats.get_value("request_depth_max") is None
    assert stats.get_value("depth/request_ignored_count") is None


# preserve


@coroutine_test
async def test_preserve_seed_keeps_depth() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="preserve", DEPTH_PRIORITY=10)
    seed = Request(
        "https://example.com",
        meta={"depth": 5},
        callback=lambda response: None,
        errback=lambda failure: None,
        flags=["flag"],
        cb_kwargs={"key": "value"},
        body=b"body",
        priority=7,
    )

    out = await process_start(mw, [seed])

    seeded = out[0]
    assert seeded.meta["depth"] == 5
    # priority is adjusted once, by the whole depth
    assert seeded.priority == 7 - 5 * 10
    assert seeded.callback is seed.callback
    assert seeded.errback is seed.errback
    assert seeded.flags == ["flag"]
    assert seeded.cb_kwargs == {"key": "value"}
    assert seeded.body == b"body"
    assert seed.meta == {"depth": 5}
    assert seed.priority == 7
    assert stats.get_value("request_depth_count/5") == 1
    assert stats.get_value("request_depth_max") == 5


@coroutine_test
async def test_preserve_seed_missing_depth() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="preserve")

    out = await process_start(mw, [Request("https://example.com")])

    assert out[0].meta["depth"] == 0
    assert stats.get_value("request_depth_count/0") == 1


@pytest.mark.parametrize(
    ("depth", "exception"),
    [
        (True, TypeError),
        (False, TypeError),
        (-1, ValueError),
        (-10, ValueError),
        (float("nan"), TypeError),
        (1.0, TypeError),
        ("3", TypeError),
        (b"3", TypeError),
        (None, TypeError),
        ([3], TypeError),
        ({"depth": 3}, TypeError),
    ],
)
@coroutine_test
async def test_preserve_invalid_seed_depth(
    depth: Any, exception: type[Exception]
) -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="preserve")
    seed = Request("https://example.com", meta={"depth": depth})

    with pytest.raises(exception, match="non-negative integer"):
        await process_start(mw, [seed])
    with pytest.raises(exception, match="non-negative integer"):
        process(mw, None, [seed])

    assert stats.get_value("request_depth_max") is None


def test_policies_other_than_preserve_ignore_start_depth() -> None:
    """Only preserve validates or reuses a start depth."""
    seed = Request("https://example.com", meta={"depth": "not a depth"})

    legacy_mw, _ = make_mw(DEPTH_SEED_POLICY="legacy")
    assert process(legacy_mw, None, [seed]) == [seed]
    assert seed.meta == {"depth": "not a depth"}

    zero_mw, _ = make_mw(DEPTH_SEED_POLICY="zero")
    zero_out = process(zero_mw, None, [seed])
    assert zero_out[0].meta["depth"] == 0
    assert seed.meta == {"depth": "not a depth"}


# Children


@coroutine_test
async def test_child_depth_comes_from_response() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="zero", DEPTH_PRIORITY=10)
    parent = Request("https://example.com/parent", meta={"depth": 2})
    response = response_for(parent)
    child = Request(
        "https://example.com/child",
        callback=lambda response: None,
        errback=lambda failure: None,
        flags=["flag"],
        cb_kwargs={"key": "value"},
        body=b"body",
        priority=7,
    )

    out = process(mw, response, [child])

    assert len(out) == 1
    processed = out[0]
    assert processed is not child
    assert processed.meta["depth"] == 3
    assert processed.priority == 7 - 3 * 10
    assert processed.callback is child.callback
    assert processed.errback is child.errback
    assert processed.flags == ["flag"]
    assert processed.cb_kwargs == {"key": "value"}
    assert processed.body == b"body"
    # Neither the child nor the parent is modified.
    assert "depth" not in child.meta
    assert child.priority == 7
    assert parent.meta == {"depth": 2}
    assert stats.get_value("request_depth_count/3") == 1
    assert stats.get_value("request_depth_max") == 3


@coroutine_test
async def test_child_shares_response_meta() -> None:
    mw, _ = make_mw(DEPTH_SEED_POLICY="zero")
    parent = Request("https://example.com/parent", meta={"depth": 1, "shared": "value"})
    response = response_for(parent)
    nested = {"value": 1}
    child = Request("https://example.com/child", meta=dict(response.meta))
    child.meta["nested"] = nested

    out = await process_async(mw, response, [child])

    assert out[0].meta["depth"] == 2
    assert out[0].meta["shared"] == "value"
    assert out[0].meta["nested"] is nested
    assert nested == {"value": 1}
    assert parent.meta == {"depth": 1, "shared": "value"}


@coroutine_test
async def test_child_reuses_parent_request() -> None:
    mw, _ = make_mw(DEPTH_SEED_POLICY="preserve", DEPTH_PRIORITY=10)
    parent = Request(
        "https://example.com/page",
        meta={"depth": 1, "foo": "bar"},
        priority=5,
    )
    response = response_for(parent)

    out = await process_async(mw, response, [parent])

    assert len(out) == 1
    assert out[0] is not parent
    assert out[0].meta["depth"] == 2
    assert out[0].meta["foo"] == "bar"
    assert out[0].priority == 5 - 2 * 10
    assert parent.meta == {"depth": 1, "foo": "bar"}
    assert parent.priority == 5


@coroutine_test
async def test_child_of_processed_request_priority_adjusted_once() -> None:
    """A processed request yielded again from another response gets its
    priority adjusted by the whole new depth, not on top of the old one."""
    mw, _ = make_mw(DEPTH_SEED_POLICY="preserve", DEPTH_PRIORITY=10)
    parent = Request("https://example.com/parent", meta={"depth": 1}, priority=20)
    child = (
        await process_async(
            mw,
            response_for(parent),
            [Request("https://example.com/child", priority=20)],
        )
    )[0]
    assert child.meta["depth"] == 2
    assert child.priority == 0

    # The child becomes the source of a grandchild, both being yielded by the
    # same callback otherwise.
    grandchild = (
        await process_async(
            mw,
            response_for(child),
            [Request("https://example.com/grandchild", priority=20)],
        )
    )[0]
    assert grandchild.meta["depth"] == 3
    assert grandchild.priority == 20 - 3 * 10


@coroutine_test
async def test_child_depth_reset() -> None:
    for policy in ("zero", "preserve"):
        mw, stats = make_mw(DEPTH_SEED_POLICY=policy)
        parent = Request("https://example.com/parent", meta={"depth": 5})
        response = response_for(parent)
        child = Request("https://example.com/child", meta={"depth_reset": True})

        out = await process_async(mw, response, [child])

        assert out[0].meta["depth"] == 0
        assert "depth_reset" not in out[0].meta
        assert child.meta == {"depth_reset": True}
        assert stats.get_value("request_depth_count/0") == 1


@coroutine_test
async def test_start_depth_reset() -> None:
    """``depth_reset`` only applies to child requests."""
    for policy, expected in (("zero", 0), ("preserve", 4)):
        mw, _ = make_mw(DEPTH_SEED_POLICY=policy)
        seed = Request("https://example.com", meta={"depth": 4, "depth_reset": True})

        out = await process_start(mw, [seed])

        # The seed depth follows the policy, and depth_reset does not reach
        # the copy.
        assert out[0].meta["depth"] == expected
        assert "depth_reset" not in out[0].meta
        assert seed.meta == {"depth": 4, "depth_reset": True}


@coroutine_test
async def test_child_without_response_depth() -> None:
    mw, _ = make_mw(DEPTH_SEED_POLICY="zero")
    response = Response("https://example.com")
    child = Request("https://example.com/child")

    out = await process_async(mw, response, [child])

    assert out[0].meta["depth"] == 1


@pytest.mark.parametrize("depth", [True, -1, float("nan"), 2.5, "2", None, [2]])
@coroutine_test
async def test_child_invalid_response_depth(depth: Any) -> None:
    mw, _ = make_mw(DEPTH_SEED_POLICY="zero")
    parent = Request("https://example.com/parent", meta={"depth": depth})
    response = response_for(parent)

    with pytest.raises((TypeError, ValueError), match="non-negative integer"):
        await process_async(mw, response, [Request("https://example.com/child")])


# Priority


@pytest.mark.parametrize(
    ("prio", "depth", "expected"),
    [
        (10, 3, -30),
        (-10, 3, 30),
        (0, 3, 0),
        (1, 0, 0),
    ],
)
@coroutine_test
async def test_priority_adjustment_sign(prio: int, depth: int, expected: int) -> None:
    mw, _ = make_mw(DEPTH_SEED_POLICY="preserve", DEPTH_PRIORITY=prio)
    seed = Request("https://example.com", meta={"depth": depth}, priority=0)

    out = await process_start(mw, [seed])

    assert out[0].priority == expected


# DEPTH_LIMIT


def test_legacy_depth_limit_boundary() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="legacy", DEPTH_LIMIT=2)
    parent = Request("https://example.com/parent", meta={"depth": 1})
    response = response_for(parent)
    at_limit = process(mw, response, [Request("https://example.com/a")])
    assert at_limit[0].meta["depth"] == 2

    deeper_response = response_for(
        Request("https://example.com/parent", meta={"depth": 2})
    )
    assert not process(mw, deeper_response, [Request("https://example.com/b")])
    assert stats.get_value("depth/request_ignored_count") == 1
    assert stats.get_value("request_depth_count/2") == 1
    assert stats.get_value("request_depth_max") == 2


@coroutine_test
async def test_zero_depth_limit_boundary() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="zero", DEPTH_LIMIT=2)
    seed = Request("https://example.com/0")
    seeded = (await process_start(mw, [seed]))[0]
    assert seeded.meta["depth"] == 0

    first = process(mw, response_for(seeded), [Request("https://example.com/1")])
    assert first[0].meta["depth"] == 1
    second = process(mw, response_for(first[0]), [Request("https://example.com/2")])
    assert second[0].meta["depth"] == 2
    # depth 3 is the first one over the limit
    assert not process(mw, response_for(second[0]), [Request("https://example.com/3")])
    assert stats.get_value("depth/request_ignored_count") == 1


@coroutine_test
async def test_preserve_depth_limit_boundary() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="preserve", DEPTH_LIMIT=2)
    # The seed at the limit is accepted.
    seeded = (
        await process_start(
            mw, [Request("https://example.com/seed", meta={"depth": 2})]
        )
    )[0]
    assert seeded.meta["depth"] == 2
    # A child of it exceeds the limit.
    assert not process(mw, response_for(seeded), [Request("https://example.com/child")])
    assert stats.get_value("depth/request_ignored_count") == 1


@coroutine_test
async def test_legacy_depth_limit_does_not_apply_to_seeds() -> None:
    """Legacy never assigns a depth or counts a start request, so it cannot
    reject one either."""
    mw, stats = make_mw(DEPTH_SEED_POLICY="legacy", DEPTH_LIMIT=2)
    seed = Request("https://example.com", meta={"depth": 5})

    out = await process_start(mw, [seed])

    assert out == [seed]
    assert out[0].meta["depth"] == 5
    assert stats.get_value("depth/request_ignored_count") is None
    assert stats.get_value("request_depth_max") is None


@coroutine_test
async def test_zero_depth_limit_applies_to_children_only() -> None:
    """Under zero a seed is always at depth 0, so the limit cannot reject it."""
    mw, stats = make_mw(DEPTH_SEED_POLICY="zero", DEPTH_LIMIT=0)

    out = await process_start(mw, [Request("https://example.com", meta={"depth": 9})])

    assert len(out) == 1
    assert out[0].meta["depth"] == 0
    assert stats.get_value("depth/request_ignored_count") is None


@coroutine_test
async def test_rejected_seed_is_not_output() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="preserve", DEPTH_LIMIT=2)

    assert (
        await process_start(mw, [Request("https://example.com", meta={"depth": 3})])
        == []
    )
    assert stats.get_value("depth/request_ignored_count") == 1
    assert stats.get_value("request_depth_count/3") is None
    assert stats.get_value("request_depth_max") is None


@coroutine_test
async def test_rejected_request_counted_once() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="preserve", DEPTH_LIMIT=1)
    parent = Request("https://example.com/parent", meta={"depth": 1})
    response = response_for(parent)
    rejected = Request("https://example.com/rejected")

    out = await process_async(mw, response, [rejected, rejected])

    assert out == []
    assert stats.get_value("depth/request_ignored_count") == 1


# Stats


@coroutine_test
async def test_verbose_stats() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="preserve", DEPTH_STATS_VERBOSE=True)
    seed = (
        await process_start(mw, [Request("https://example.com", meta={"depth": 2})])
    )[0]
    child = (
        await process_async(mw, response_for(seed), [Request("https://example.com/c")])
    )[0]

    assert stats.get_value("request_depth_count/2") == 1
    assert stats.get_value("request_depth_count/3") == 1
    assert stats.get_value("request_depth_max") == 3
    # The response of a processed request is not counted again.
    await process_async(mw, response_for(seed), [Request("https://example.com/d")])
    assert stats.get_value("request_depth_count/0") is None
    assert child.meta["depth"] == 3


@coroutine_test
async def test_non_verbose_stats() -> None:
    mw, stats = make_mw(
        DEPTH_SEED_POLICY="zero", DEPTH_STATS_VERBOSE=False, DEPTH_PRIORITY=10
    )
    seed = (await process_start(mw, [Request("https://example.com")]))[0]
    await process_async(
        mw, response_for(seed), [Request("https://example.com/c", priority=5)]
    )

    assert stats.get_value("request_depth_count/0") is None
    assert stats.get_value("request_depth_count/1") is None
    assert stats.get_value("request_depth_max") == 1


# Isolation


@coroutine_test
async def test_crawlers_are_independent() -> None:
    zero_mw, zero_stats = make_mw(DEPTH_SEED_POLICY="zero")
    preserve_mw, preserve_stats = make_mw(DEPTH_SEED_POLICY="preserve")
    seed = Request("https://example.com", meta={"depth": 3})

    zero_out = await process_start(zero_mw, [seed])
    preserve_out = await process_start(preserve_mw, [seed])

    assert zero_out[0].meta["depth"] == 0
    assert preserve_out[0].meta["depth"] == 3
    assert zero_stats.get_value("request_depth_count/0") == 1
    assert zero_stats.get_value("request_depth_max") == 0
    assert preserve_stats.get_value("request_depth_count/3") == 1
    assert preserve_stats.get_value("request_depth_max") == 3


@coroutine_test
async def test_concurrent_crawls_keep_depths_apart() -> None:
    """Two crawlers sharing the process do not affect each other's depths."""

    class SharedSpider(Spider):
        name = "shared_spider"

        def __init__(self, seed_depth: int, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.seed_depth = seed_depth
            self.observed: list[Any] = []

        async def start(self) -> AsyncIterator[Any]:
            yield Request(
                "data:text/plain,seed",
                callback=self.parse,
                meta={"depth": self.seed_depth},
            )

        def parse(self, response: Response) -> Any:
            self.observed.append(response.meta.get("depth"))
            if len(self.observed) < 2:
                yield Request("data:text/plain,child", callback=self.parse)

    async def crawl(policy: str, seed_depth: int) -> tuple[list[Any], Crawler]:
        crawler = get_crawler(
            SharedSpider, {"DEPTH_SEED_POLICY": policy, "DEPTH_STATS_VERBOSE": True}
        )
        await crawler.crawl_async(seed_depth=seed_depth)
        spider = crawler.spider
        assert isinstance(spider, SharedSpider)
        return spider.observed, crawler

    (
        (zero_observed, zero_crawler),
        (preserve_observed, preserve_crawler),
    ) = await asyncio.gather(
        crawl("zero", 7),
        crawl("preserve", 7),
    )

    assert zero_observed == [0, 1]
    assert preserve_observed == [7, 8]
    assert zero_crawler.stats.get_value("request_depth_max") == 1
    assert preserve_crawler.stats.get_value("request_depth_max") == 8


@coroutine_test
async def test_seeds_are_independent() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="preserve", DEPTH_PRIORITY=1)
    seeds = [
        Request("https://example.com/a", meta={"depth": 0}, priority=10),
        Request("https://example.com/b", meta={"depth": 2}, priority=10),
    ]

    out = await process_start(mw, list(seeds))

    assert [request.meta["depth"] for request in out] == [0, 2]
    assert [request.priority for request in out] == [10, 8]
    assert stats.get_value("request_depth_count/0") == 1
    assert stats.get_value("request_depth_count/2") == 1
    assert stats.get_value("request_depth_max") == 2


# Compatibility bridges


@coroutine_test
async def test_start_request_not_deepened_twice_through_output() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="preserve", DEPTH_PRIORITY=1)
    seed = Request("https://example.com", meta={"depth": 2}, priority=5)

    start_out = await process_start(mw, [seed])
    processed = start_out[0]
    assert processed.meta["depth"] == 2
    assert processed.priority == 3

    # A compatibility bridge may feed the processed start request again,
    # through the spider output chain with no response.
    again = process(mw, None, [processed])
    assert again == [processed]
    assert again[0].meta["depth"] == 2
    assert again[0].priority == 3
    assert stats.get_value("request_depth_count/2") == 1
    assert stats.get_value("request_depth_max") == 2


@coroutine_test
async def test_output_processed_once_across_sync_and_async() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="zero", DEPTH_PRIORITY=1)
    parent = Request("https://example.com/parent", meta={"depth": 1})
    response = response_for(parent)
    child = Request("https://example.com/child", priority=4)

    sync_out = process(mw, response, [child])
    async_out = await process_async(mw, response, [child])

    assert sync_out == async_out
    assert sync_out[0] is async_out[0]
    assert sync_out[0].meta["depth"] == 2
    assert sync_out[0].priority == 2
    assert stats.get_value("request_depth_count/2") == 1
    assert stats.get_value("request_depth_max") == 2


@coroutine_test
async def test_start_request_deepened_once_with_response_none_across_chains() -> None:
    mw, stats = make_mw(DEPTH_SEED_POLICY="zero")

    seed = Request("https://example.com")
    sync_out = process(mw, None, [seed])
    async_out = await process_async(mw, None, [seed])
    start_out = await process_start(mw, [seed])

    assert sync_out == async_out == start_out
    assert start_out[0].meta["depth"] == 0
    assert stats.get_value("request_depth_count/0") == 1
    assert stats.get_value("request_depth_max") == 0


# Real middleware chain


def build_manager(
    spidercls: type[Spider] = Spider, **settings: Any
) -> tuple[SpiderMiddlewareManager, Crawler]:
    crawler = get_crawler(
        spidercls,
        {
            "SPIDER_MIDDLEWARES_BASE": {},
            "SPIDER_MIDDLEWARES": {
                "scrapy.spidermiddlewares.depth.DepthMiddleware": 900
            },
            **settings,
        },
    )
    crawler.spider = crawler._create_spider("depth_chain")
    crawler.stats.open_spider()
    return build_from_crawler(SpiderMiddlewareManager, crawler), crawler


@coroutine_test
async def test_manager_start_output_bridge_is_idempotent() -> None:
    """The start output fed again through the output chain is not deepened,
    adjusted, or counted a second time."""
    for policy, expected_priority in (("zero", 8), ("preserve", 5)):
        mwman, crawler = build_manager(
            DEPTH_SEED_POLICY=policy, DEPTH_PRIORITY=1, DEPTH_STATS_VERBOSE=True
        )
        seed = Request("https://example.com", meta={"depth": 3}, priority=8)

        start_out = await collect_asyncgen(
            mwman.methods["process_start"][0](as_async_generator([seed]))
        )
        # Simulate a compatibility bridge that also delivers the processed
        # start request through the spider output chain.
        bridged = await collect_asyncgen(
            mwman.methods["process_spider_output"][0](
                None, as_async_generator(start_out)
            )
        )

        processed = start_out[0]
        assert isinstance(processed, Request)
        assert bridged == [processed]
        assert bridged[0] is processed
        expected_depth = 0 if policy == "zero" else 3
        assert processed.meta["depth"] == expected_depth
        assert processed.priority == expected_priority
        key = f"request_depth_count/{expected_depth}"
        assert crawler.stats.get_value(key) == 1
        assert crawler.stats.get_value("request_depth_max") == expected_depth


class StartSpiderWithSeeds(Spider):
    name = "start_seeds"

    seed_meta: dict[str, Any] = {}
    seed_priority = 0

    async def start(self) -> AsyncIterator[Any]:
        yield Request(
            "https://example.com/seed",
            meta=dict(self.seed_meta),
            priority=self.seed_priority,
        )
        yield {"not": "a request"}

    def parse(self, response: Response) -> Any:
        yield Request("https://example.com/child", priority=10)
        yield {"not": "a request"}


@coroutine_test
async def test_manager_start_chain() -> None:
    mwman, crawler = build_manager(
        StartSpiderWithSeeds,
        DEPTH_SEED_POLICY="preserve",
        DEPTH_PRIORITY=1,
        DEPTH_STATS_VERBOSE=True,
    )
    crawler.spider.seed_meta = {"depth": 4}
    crawler.spider.seed_priority = 7

    start = await mwman.process_start()
    assert start is not None
    out = await collect_asyncgen(start)

    assert len(out) == 2
    assert out[1] == {"not": "a request"}
    seed = out[0]
    assert isinstance(seed, Request)
    assert seed.meta["depth"] == 4
    assert seed.priority == 3
    assert crawler.stats.get_value("request_depth_count/4") == 1
    assert crawler.stats.get_value("request_depth_max") == 4


@coroutine_test
async def test_manager_output_chain() -> None:
    mwman, crawler = build_manager(
        DEPTH_SEED_POLICY="preserve", DEPTH_PRIORITY=1, DEPTH_STATS_VERBOSE=True
    )
    parent = Request("https://example.com/seed", meta={"depth": 4})
    response = response_for(parent)

    async def callback(
        failing_response: Response | Failure, request: Request
    ) -> list[Any]:
        return [Request("https://example.com/child", priority=10), {"item": 1}]

    output = await mwman.scrape_response_async(callback, response, parent)
    out = await collect_asyncgen(output)

    assert len(out) == 2
    assert out[1] == {"item": 1}
    child = out[0]
    assert isinstance(child, Request)
    assert child.meta["depth"] == 5
    assert child.priority == 5
    # The request of the response is untouched.
    assert parent.meta == {"depth": 4}
    assert crawler.stats.get_value("request_depth_count/5") == 1
    assert crawler.stats.get_value("request_depth_max") == 5


@coroutine_test
async def test_manager_failure_chain_has_no_response() -> None:
    mwman, crawler = build_manager(
        DEPTH_SEED_POLICY="zero", DEPTH_PRIORITY=1, DEPTH_STATS_VERBOSE=True
    )
    request = Request("https://example.com/seed")
    failure = Failure(Exception("boom"))

    async def errback(failed: Failure, failed_request: Request) -> list[Any]:
        return [Request("https://example.com/from-errback", priority=10)]

    output = await mwman._scrape_failure_async(errback, failure, request)
    out = await collect_asyncgen(output)

    assert len(out) == 1
    child = out[0]
    assert isinstance(child, Request)
    # Errback output has no source response, so it gets a start depth.
    assert child.meta["depth"] == 0
    assert child.priority == 10
    assert crawler.stats.get_value("request_depth_count/0") == 1
    assert crawler.stats.get_value("request_depth_max") == 0


# Real crawl


class DepthRecordingSpider(Spider):
    name = "depth_recording"

    def __init__(
        self,
        url: str,
        pages: int,
        seed_meta: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.start_url = url
        self.pages = pages
        self.seed_meta = seed_meta or {}
        self.depths: list[tuple[str, Any]] = []
        self.priorities: list[tuple[str, int]] = []

    async def start(self) -> AsyncIterator[Any]:
        yield Request(
            self.start_url,
            callback=self.parse,
            cb_kwargs={"page": 0},
            meta=self.seed_meta,
        )

    def parse(self, response: Response, page: int) -> Any:
        self.depths.append((response.url, response.meta.get("depth")))
        assert response.request is not None
        self.priorities.append((response.url, response.request.priority))
        if page + 1 < self.pages:
            yield Request(
                f"data:text/plain,page-{page + 1}",
                callback=self.parse,
                cb_kwargs={"page": page + 1},
                priority=10,
            )


async def crawl_depths(
    policy: str, *, seed_depth: int | None = None, pages: int = 3
) -> tuple[list[tuple[str, Any]], list[int], Crawler]:
    settings = {"DEPTH_SEED_POLICY": policy}
    crawler = get_crawler(DepthRecordingSpider, settings)
    seed_meta = {} if seed_depth is None else {"depth": seed_depth}
    await crawler.crawl_async(
        url="data:text/plain,page-0",
        pages=pages,
        seed_meta=seed_meta,
    )
    spider = crawler.spider
    assert isinstance(spider, DepthRecordingSpider)
    return spider.depths, [priority for _, priority in spider.priorities], crawler


@pytest.mark.parametrize(
    ("policy", "seed_depth", "expected"),
    [
        ("legacy", None, [0, 1, 2]),
        # Legacy does not touch the depth of start requests, so its children
        # start from the depth that the seed carried.
        ("legacy", 5, [5, 6, 7]),
        ("zero", 5, [0, 1, 2]),
        ("preserve", 5, [5, 6, 7]),
        ("preserve", 1, [1, 2, 3]),
    ],
)
@coroutine_test
async def test_crawl_depths(
    policy: str, seed_depth: int | None, expected: list[int]
) -> None:
    depths, _, crawler = await crawl_depths(policy, seed_depth=seed_depth)
    assert [depth for _, depth in depths] == expected
    assert crawler.stats.get_value("request_depth_max") == expected[-1]


@coroutine_test
async def test_crawl_depth_limit() -> None:
    class LimitedSpider(Spider):
        name = "limited_depth"

        async def start(self) -> AsyncIterator[Any]:
            yield Request("data:text/plain,0", callback=self.parse)

        def parse(self, response: Response) -> Any:
            assert response.request is not None
            page = int(response.url.rsplit(",", 1)[1])
            yield Request(f"data:text/plain,{page + 1}", callback=self.parse)

    crawler = get_crawler(
        LimitedSpider,
        {
            "DEPTH_SEED_POLICY": "zero",
            "DEPTH_LIMIT": 2,
            "DEPTH_STATS_VERBOSE": True,
        },
    )
    await crawler.crawl_async()
    assert crawler.stats.get_value("downloader/request_count") == 3
    assert crawler.stats.get_value("depth/request_ignored_count") == 1
    assert crawler.stats.get_value("request_depth_max") == 2


@coroutine_test
async def test_crawl_empty_start() -> None:
    class EmptySpider(Spider):
        name = "empty_depth"

        async def start(self) -> AsyncIterator[Any]:
            return
            yield

    crawler = get_crawler(EmptySpider, {"DEPTH_SEED_POLICY": "preserve"})
    await crawler.crawl_async()
    assert crawler.stats.get_value("finish_reason") == "finished"
    assert crawler.stats.get_value("request_depth_max") is None


@coroutine_test
async def test_crawl_multilevel_callbacks_with_shared_meta() -> None:
    """A callback that shares response.meta with its children and one that
    yields the request it received do not affect the request of the response
    or the depths derived from it."""

    class SharingSpider(Spider):
        name = "sharing_meta"

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.observed: list[tuple[int, Any, int, list[Any]]] = []
            self.shared_list: list[Any] = ["from-seed"]

        async def start(self) -> AsyncIterator[Any]:
            yield Request(
                "data:text/plain,level-0",
                callback=self.parse,
                meta={"depth": 3, "shared": self.shared_list},
            )

        def parse(self, response: Response) -> Any:
            assert response.request is not None
            level = int(response.url.rsplit("-", 1)[1])
            self.observed.append(
                (
                    level,
                    response.meta.get("depth"),
                    response.request.priority,
                    list(response.meta["shared"]),
                )
            )
            if len(self.observed) >= 3:
                return
            if level == 0:
                # Copy the response meta into the next request, so that both
                # requests share the nested list.
                yield Request(
                    f"data:text/plain,level-{level + 1}",
                    callback=self.parse,
                    meta=dict(response.meta),
                    priority=100,
                )
            else:
                # Reuse the request that produced this response, visiting the
                # same URL again.
                reused = response.request
                reused.dont_filter = True
                reused.priority = 100
                yield reused

    crawler = get_crawler(
        SharingSpider,
        {
            "DEPTH_SEED_POLICY": "preserve",
            "DEPTH_PRIORITY": 10,
            "DEPTH_STATS_VERBOSE": True,
        },
    )
    await crawler.crawl_async()
    spider = crawler.spider
    assert isinstance(spider, SharingSpider)

    # The third visit happens because the callback of the second response
    # yields its own request. As a child of that response, it gets its depth
    # plus 1, without modifying the original request or its meta.
    assert [level for level, _, _, _ in spider.observed] == [0, 1, 1]
    assert [depth for _, depth, _, _ in spider.observed] == [3, 4, 5]
    # Children start from the priority that the callback sets, so they are
    # adjusted by their whole depth once.
    assert [priority for _, _, priority, _ in spider.observed] == [-30, 60, 50]
    # The shared nested value is never replaced or extended.
    assert all(shared == ["from-seed"] for _, _, _, shared in spider.observed)
    assert spider.shared_list == ["from-seed"]
    assert crawler.stats.get_value("request_depth_count/3") == 1
    assert crawler.stats.get_value("request_depth_count/4") == 1
    assert crawler.stats.get_value("request_depth_count/5") == 1
    assert crawler.stats.get_value("request_depth_max") == 5


@coroutine_test
async def test_crawl_seed_objects_are_not_modified() -> None:
    class SeedSpider(Spider):
        name = "seed_objects"

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.seed: Request | None = None
            self.observed: list[Any] = []

        async def start(self) -> AsyncIterator[Any]:
            self.seed = Request(
                "data:text/plain,seed",
                callback=self.parse,
                meta={"depth": 2, "marker": "seed"},
                priority=10,
            )
            yield self.seed

        def parse(self, response: Response) -> None:
            self.observed.append(response.meta.get("depth"))

    for policy in ("zero", "preserve"):
        crawler = get_crawler(
            SeedSpider,
            {"DEPTH_SEED_POLICY": policy, "DEPTH_PRIORITY": 10},
        )
        await crawler.crawl_async()
        spider = crawler.spider
        assert isinstance(spider, SeedSpider)
        assert spider.seed is not None
        assert spider.seed.meta == {"depth": 2, "marker": "seed"}
        assert spider.seed.priority == 10
        expected = 0 if policy == "zero" else 2
        assert spider.observed == [expected]
        assert expected == 0 or crawler.stats.get_value("request_depth_max") == 2
