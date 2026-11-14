import gc
from logging import INFO
from typing import Any
from unittest.mock import Mock

import pytest

from scrapy import Request, Spider
from scrapy.core.downloader import Slot
from scrapy.crawler import Crawler
from scrapy.exceptions import NotConfigured
from scrapy.extensions.throttle import AutoThrottle
from scrapy.http.response import Response
from scrapy.settings.default_settings import (
    AUTOTHROTTLE_MAX_DELAY,
    AUTOTHROTTLE_START_DELAY,
    DOWNLOAD_DELAY,
)
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler as _get_crawler

UNSET = object()


def get_crawler(
    settings: dict[str, Any] | None = None, spidercls: type[Spider] | None = None
) -> Crawler:
    settings = settings or {}
    settings["AUTOTHROTTLE_ENABLED"] = True
    return _get_crawler(settings_dict=settings, spidercls=spidercls)


def _mock_downloader(crawler: Crawler) -> Mock:
    """Give *crawler* a mock engine, whose downloader AutoThrottle reads."""
    crawler.engine = Mock()
    downloader: Mock = crawler.engine.downloader
    downloader.slots = {}
    return downloader


def _setup(settings: dict[str, Any] | None = None) -> tuple[AutoThrottle, Spider, Mock]:
    crawler = get_crawler(settings)
    at = build_from_crawler(AutoThrottle, crawler)
    downloader = _mock_downloader(crawler)
    spider = DefaultSpider.from_crawler(crawler)
    at._spider_opened(spider)
    return at, spider, downloader


def _add_slot(downloader: Mock, key: str = "foo", delay: float = 1.0) -> Slot:
    slot = Slot(concurrency=8, delay=delay, jitter=0)
    downloader.slots[key] = slot
    return slot


def _download(
    at: AutoThrottle,
    spider: Spider,
    *,
    latency: Any = 1.0,
    slot: str = "foo",
    status: int = 200,
    dont_adjust: bool = False,
) -> None:
    meta: dict[str, Any] = {"download_slot": slot}
    if latency is not UNSET:
        meta["download_latency"] = latency
    if dont_adjust:
        meta["autothrottle_dont_adjust_delay"] = True
    request = Request("https://example.com", meta=meta)
    at._response_downloaded(Response(request.url, status=status), request, spider)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (UNSET, False),
        (False, False),
        (True, True),
    ],
)
def test_enabled(value, expected):
    settings = {}
    if value is not UNSET:
        settings["AUTOTHROTTLE_ENABLED"] = value
    crawler = _get_crawler(settings_dict=settings)
    if expected:
        build_from_crawler(AutoThrottle, crawler)
    else:
        with pytest.raises(NotConfigured):
            build_from_crawler(AutoThrottle, crawler)


@pytest.mark.parametrize(
    "value",
    [
        0.0,
        -1.0,
    ],
)
def test_target_concurrency_invalid(value):
    settings = {"AUTOTHROTTLE_TARGET_CONCURRENCY": value}
    crawler = get_crawler(settings)
    with pytest.raises(NotConfigured):
        build_from_crawler(AutoThrottle, crawler)


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        (UNSET, DOWNLOAD_DELAY),
        (1.0, 1.0),
    ],
)
def test_mindelay_definition(setting, expected):
    settings = {}
    if setting is not UNSET:
        settings["DOWNLOAD_DELAY"] = setting

    crawler = get_crawler(settings)
    at = build_from_crawler(AutoThrottle, crawler)
    _mock_downloader(crawler)
    at._spider_opened(DefaultSpider.from_crawler(crawler))
    assert at.mindelay == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (UNSET, AUTOTHROTTLE_MAX_DELAY),
        (1.0, 1.0),
    ],
)
def test_maxdelay_definition(value, expected):
    settings = {}
    if value is not UNSET:
        settings["AUTOTHROTTLE_MAX_DELAY"] = value
    crawler = get_crawler(settings)
    at = build_from_crawler(AutoThrottle, crawler)
    _mock_downloader(crawler)
    at._spider_opened(DefaultSpider.from_crawler(crawler))
    assert at.maxdelay == expected


@pytest.mark.parametrize(
    ("min_setting", "start_setting", "expected"),
    [
        (UNSET, UNSET, AUTOTHROTTLE_START_DELAY),
        (AUTOTHROTTLE_START_DELAY - 1.0, UNSET, AUTOTHROTTLE_START_DELAY),
        (AUTOTHROTTLE_START_DELAY + 1.0, UNSET, AUTOTHROTTLE_START_DELAY + 1.0),
        (UNSET, AUTOTHROTTLE_START_DELAY - 1.0, AUTOTHROTTLE_START_DELAY - 1.0),
        (UNSET, AUTOTHROTTLE_START_DELAY + 1.0, AUTOTHROTTLE_START_DELAY + 1.0),
        (
            AUTOTHROTTLE_START_DELAY + 2.0,
            AUTOTHROTTLE_START_DELAY + 1.0,
            AUTOTHROTTLE_START_DELAY + 2.0,
        ),
        (
            AUTOTHROTTLE_START_DELAY + 1.0,
            AUTOTHROTTLE_START_DELAY + 2.0,
            AUTOTHROTTLE_START_DELAY + 2.0,
        ),
    ],
)
def test_startdelay_definition(min_setting, start_setting, expected):
    settings = {}
    if min_setting is not UNSET:
        settings["DOWNLOAD_DELAY"] = min_setting
    if start_setting is not UNSET:
        settings["AUTOTHROTTLE_START_DELAY"] = start_setting

    crawler = get_crawler(settings)
    at = build_from_crawler(AutoThrottle, crawler)
    downloader = _mock_downloader(crawler)
    at._spider_opened(DefaultSpider.from_crawler(crawler))
    assert downloader._delay == expected


@pytest.mark.parametrize(
    ("meta", "slot"),
    [
        ({}, None),
        ({"download_latency": 1.0}, None),
        ({"download_slot": "foo"}, None),
        ({"download_slot": "foo"}, "foo"),
        ({"download_latency": 1.0, "download_slot": "foo"}, None),
        (
            {
                "download_latency": 1.0,
                "download_slot": "foo",
                "autothrottle_dont_adjust_delay": True,
            },
            "foo",
        ),
    ],
)
def test_skipped(meta, slot, monkeypatch):
    crawler = get_crawler()
    at = build_from_crawler(AutoThrottle, crawler)
    downloader = _mock_downloader(crawler)
    spider = DefaultSpider.from_crawler(crawler)
    at._spider_opened(spider)
    request = Request("https://example.com", meta=meta)

    if slot is not None:
        downloader.slots[slot] = object()
    # Fail instead of adjusting the delay.
    monkeypatch.setattr(at, "_adjust_delay", Mock(side_effect=AssertionError))

    at._response_downloaded(Response("https://example.com"), request, spider)


@pytest.mark.parametrize(
    ("download_latency", "target_concurrency", "slot_delay", "expected"),
    [
        (2.0, 2.0, 1.0, 1.0),
        (1.0, 2.0, 1.0, 0.75),
        (4.0, 2.0, 1.0, 2.0),
        (2.0, 1.0, 1.0, 2.0),
        (2.0, 4.0, 1.0, 0.75),
        (2.0, 2.0, 0.5, 1.0),
        (2.0, 2.0, 2.0, 1.5),
    ],
)
def test_adjustment(download_latency, target_concurrency, slot_delay, expected):
    settings = {"AUTOTHROTTLE_TARGET_CONCURRENCY": target_concurrency}
    crawler = get_crawler(settings)
    at = build_from_crawler(AutoThrottle, crawler)
    downloader = _mock_downloader(crawler)
    spider = DefaultSpider.from_crawler(crawler)
    at._spider_opened(spider)
    meta = {"download_latency": download_latency, "download_slot": "foo"}
    request = Request("https://example.com", meta=meta)
    response = Response(request.url)

    slot = Mock()
    slot.delay = slot_delay
    downloader.slots["foo"] = slot

    at._response_downloaded(response, request, spider)

    assert slot.delay == expected, f"{slot.delay} != {expected}"


@pytest.mark.parametrize(
    ("mindelay", "maxdelay", "expected"),
    [
        (0.5, 2.0, 1.0),
        (0.25, 0.5, 0.5),
        (2.0, 4.0, 2.0),
    ],
)
def test_adjustment_limits(mindelay, maxdelay, expected):
    download_latency, target_concurrency, slot_delay = (2.0, 2.0, 1.0)
    # expected adjustment without limits with these values: 1.0
    settings = {
        "AUTOTHROTTLE_MAX_DELAY": maxdelay,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": target_concurrency,
        "DOWNLOAD_DELAY": mindelay,
    }
    crawler = get_crawler(settings)
    at = build_from_crawler(AutoThrottle, crawler)
    downloader = _mock_downloader(crawler)
    spider = DefaultSpider.from_crawler(crawler)
    at._spider_opened(spider)
    meta = {"download_latency": download_latency, "download_slot": "foo"}
    request = Request("https://example.com", meta=meta)
    response = Response(request.url)

    slot = Mock()
    slot.delay = slot_delay
    downloader.slots["foo"] = slot

    at._response_downloaded(response, request, spider)

    assert slot.delay == expected, f"{slot.delay} != {expected}"


@pytest.mark.parametrize(
    ("download_latency", "target_concurrency", "slot_delay", "expected"),
    [
        (2.0, 2.0, 1.0, 1.0),
        (1.0, 2.0, 1.0, 1.0),  # Instead of 0.75
        (4.0, 2.0, 1.0, 2.0),
    ],
)
def test_adjustment_bad_response(
    download_latency, target_concurrency, slot_delay, expected
):
    settings = {"AUTOTHROTTLE_TARGET_CONCURRENCY": target_concurrency}
    crawler = get_crawler(settings)
    at = build_from_crawler(AutoThrottle, crawler)
    downloader = _mock_downloader(crawler)
    spider = DefaultSpider.from_crawler(crawler)
    at._spider_opened(spider)
    meta = {"download_latency": download_latency, "download_slot": "foo"}
    request = Request("https://example.com", meta=meta)
    response = Response(request.url, status=400)

    slot = Mock()
    slot.delay = slot_delay
    downloader.slots["foo"] = slot

    at._response_downloaded(response, request, spider)

    assert slot.delay == expected, f"{slot.delay} != {expected}"


def test_debug(caplog):
    settings = {"AUTOTHROTTLE_DEBUG": True}
    crawler = get_crawler(settings)
    at = build_from_crawler(AutoThrottle, crawler)
    downloader = _mock_downloader(crawler)
    spider = DefaultSpider.from_crawler(crawler)
    at._spider_opened(spider)
    meta = {"download_latency": 1.0, "download_slot": "foo"}
    request = Request("https://example.com", meta=meta)
    response = Response(request.url, body=b"foo")

    slot = Mock()
    slot.delay = 2.0
    slot.transferring = (None, None)
    downloader.slots["foo"] = slot

    caplog.clear()
    with caplog.at_level(INFO):
        at._response_downloaded(response, request, spider)

    assert caplog.record_tuples == [
        (
            "scrapy.extensions.throttle",
            INFO,
            "slot: foo | conc: 2 | delay: 1500 ms (-500) | latency: 1000 ms | size:     3 bytes",
        ),
    ]


def test_debug_disabled(caplog):
    crawler = get_crawler()
    at = build_from_crawler(AutoThrottle, crawler)
    downloader = _mock_downloader(crawler)
    spider = DefaultSpider.from_crawler(crawler)
    at._spider_opened(spider)
    meta = {"download_latency": 1.0, "download_slot": "foo"}
    request = Request("https://example.com", meta=meta)
    response = Response(request.url, body=b"foo")

    slot = Mock()
    slot.delay = 2.0
    slot.transferring = (None, None)
    downloader.slots["foo"] = slot

    caplog.clear()
    with caplog.at_level(INFO):
        at._response_downloaded(response, request, spider)

    assert caplog.record_tuples == []


def _window_setup(size: int, **settings: Any):
    merged = {"AUTOTHROTTLE_LATENCY_WINDOW": size}
    merged.setdefault("AUTOTHROTTLE_TARGET_CONCURRENCY", 2.0)
    merged.update(settings)
    return _setup(merged)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (UNSET, None),
        (None, None),
        (1, 1),
        (7, 7),
        ("5", 5),
    ],
)
def test_latency_window_valid(value, expected):
    settings = {}
    if value is not UNSET:
        settings["AUTOTHROTTLE_LATENCY_WINDOW"] = value
    crawler = get_crawler(settings)
    at = build_from_crawler(AutoThrottle, crawler)
    assert at._latency_window == expected


@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        True,
        False,
        1.0,
        2.5,
        "0",
        "-3",
        "1.5",
        "abc",
        "",
    ],
)
def test_latency_window_invalid(value):
    """Invalid values abort the crawler startup instead of disabling throttling."""
    settings = {"AUTOTHROTTLE_LATENCY_WINDOW": value}
    with pytest.raises(ValueError, match="AUTOTHROTTLE_LATENCY_WINDOW"):
        get_crawler(settings)


def test_latency_window_default_uses_last_latency():
    """Without a window the latest latency alone drives the delay, as before."""
    at, spider, downloader = _setup({"AUTOTHROTTLE_TARGET_CONCURRENCY": 2.0})
    slot = _add_slot(downloader, delay=1.0)

    _download(at, spider, latency=10.0)
    assert slot.delay == 5.0
    _download(at, spider, latency=1.0)
    assert slot.delay == 2.75
    assert len(at._latencies) == 0


def test_latency_window_ignores_outlier():
    """With a window, a single spike does not move the delay."""
    at, spider, downloader = _window_setup(5)
    slot = _add_slot(downloader, delay=0.0)

    for _ in range(4):
        _download(at, spider, latency=2.0)
    assert slot.delay == 1.0
    _download(at, spider, latency=100.0)
    assert slot.delay == 1.0
    # The spike stays in the window until the next response pushes it out.
    assert list(at._latencies[slot]) == [2.0, 2.0, 2.0, 2.0, 100.0]
    _download(at, spider, latency=2.0)
    assert list(at._latencies[slot]) == [2.0, 2.0, 2.0, 100.0, 2.0]


@pytest.mark.parametrize(
    ("latencies", "expected_delays"),
    [
        # The delay is reset before each response, so it equals the target
        # delay computed from the window median: the median / 2, unless the
        # mean with the reset delay is higher.
        ((2.0,), (1.0,)),
        ((2.0, 4.0), (1.0, 1.5)),
        ((6.0, 1.0, 2.0), (3.0, 1.75, 1.0)),
        # median (2, 6) is 4, target 2, and the mean with the reset delay is 1
        ((8.0, 6.0, 1.0, 2.0), (4.0, 3.5, 3.0, 2.0)),
    ],
)
def test_latency_window_median(latencies, expected_delays):
    """Odd and even windows both use the median latency."""
    at, spider, downloader = _window_setup(max(len(latencies), 1))
    slot = _add_slot(downloader, delay=0.0)

    for latency, expected_delay in zip(latencies, expected_delays, strict=True):
        slot.delay = 0.0
        _download(at, spider, latency=latency)
        assert slot.delay == expected_delay

    assert list(at._latencies[slot]) == list(latencies)


def test_latency_window_bounded():
    """Only the most recent samples are kept."""
    at, spider, downloader = _window_setup(2)
    slot = _add_slot(downloader, delay=0.0)

    for latency in (2.0, 4.0, 6.0, 8.0):
        slot.delay = 0.0
        _download(at, spider, latency=latency)
        assert len(at._latencies[slot]) <= 2

    assert list(at._latencies[slot]) == [6.0, 8.0]


def test_latency_window_limits():
    """The window median is still capped by the min and max delays."""
    at, spider, downloader = _window_setup(
        2, DOWNLOAD_DELAY=2.0, AUTOTHROTTLE_START_DELAY=2.0, AUTOTHROTTLE_MAX_DELAY=4.0
    )
    slot = _add_slot(downloader, delay=2.0)

    _download(at, spider, latency=0.0)
    assert slot.delay == 2.0
    for _ in range(2):
        _download(at, spider, latency=100.0)
    assert slot.delay == 4.0


def test_latency_window_bad_response():
    """A non-200 response cannot lower the delay through the window."""
    at, spider, downloader = _window_setup(3)
    slot = _add_slot(downloader, delay=5.0)

    _download(at, spider, latency=6.0)
    _download(at, spider, latency=6.0)
    assert slot.delay == 3.5
    assert list(at._latencies[slot]) == [6.0, 6.0]

    # Median 6 -> target 3 < 3.5, so the error response leaves the delay alone.
    _download(at, spider, latency=100.0, status=400)
    assert slot.delay == 3.5
    assert list(at._latencies[slot]) == [6.0, 6.0, 100.0]

    # A 200 response applies the median (6) again, and keeps lowering it.
    _download(at, spider, latency=6.0)
    assert slot.delay == 3.25

    # Errors still raise the delay when the window median does.
    for _ in range(5):
        _download(at, spider, latency=100.0, status=500)
    assert slot.delay == 50.0


def test_latency_window_missing_latency():
    """A response without a latency does not fill the window."""
    at, spider, downloader = _window_setup(3)
    slot = _add_slot(downloader, delay=0.0)
    _download(at, spider, latency=2.0)

    _download(at, spider, latency=UNSET)

    assert list(at._latencies[slot]) == [2.0]
    assert slot.delay == 1.0


def test_latency_window_zero_latency():
    """Zero is a valid latency and can lower the delay."""
    at, spider, downloader = _window_setup(2)
    slot = _add_slot(downloader, delay=2.0)

    _download(at, spider, latency=0.0)
    assert list(at._latencies[slot]) == [0.0]
    assert slot.delay == 1.0
    _download(at, spider, latency=0.0)
    assert slot.delay == 0.5


@pytest.mark.parametrize(
    "latency",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        -1.0,
        -0.001,
        "1.0",
        True,
        [1.0],
        UNSET,
    ],
)
def test_latency_window_invalid_latency(latency):
    """Invalid latencies neither fill the window nor change the delay."""
    at, spider, downloader = _window_setup(3)
    slot = _add_slot(downloader, delay=0.0)
    _download(at, spider, latency=2.0)
    assert list(at._latencies[slot]) == [2.0]
    assert slot.delay == 1.0

    _download(at, spider, latency=latency)

    assert list(at._latencies[slot]) == [2.0]
    assert slot.delay == 1.0


def test_latency_window_dont_adjust():
    """Requests that skip the adjustment do not fill the window either."""
    at, spider, downloader = _window_setup(3)
    slot = _add_slot(downloader, delay=0.0)
    _download(at, spider, latency=2.0)
    assert list(at._latencies[slot]) == [2.0]
    assert slot.delay == 1.0

    _download(at, spider, latency=100.0, dont_adjust=True)

    assert list(at._latencies[slot]) == [2.0]
    assert slot.delay == 1.0


def test_latency_window_independent_slots():
    """Each download slot has its own window and delay."""
    at, spider, downloader = _window_setup(3)
    foo = _add_slot(downloader, "foo", delay=0.0)
    bar = _add_slot(downloader, "bar", delay=0.0)

    _download(at, spider, latency=2.0, slot="foo")
    _download(at, spider, latency=2.0, slot="foo")
    _download(at, spider, latency=10.0, slot="bar")

    assert list(at._latencies[foo]) == [2.0, 2.0]
    assert list(at._latencies[bar]) == [10.0]
    assert foo.delay == 1.0
    assert bar.delay == 5.0


def test_latency_window_spider_closed():
    """Closing the spider drops every window."""
    at, spider, downloader = _window_setup(3)
    _add_slot(downloader, "foo", delay=0.0)
    _add_slot(downloader, "bar", delay=0.0)
    _download(at, spider, latency=2.0, slot="foo")
    _download(at, spider, latency=2.0, slot="bar")
    assert len(at._latencies) == 2

    at._spider_closed(spider)

    assert len(at._latencies) == 0


def test_latency_window_slot_removed():
    """A window goes away with its download slot."""
    at, spider, downloader = _window_setup(3)
    slot = _add_slot(downloader, delay=0.0)
    _download(at, spider, latency=2.0)
    assert len(at._latencies) == 1

    del downloader.slots["foo"]
    del slot
    gc.collect()

    assert len(at._latencies) == 0


def test_latency_window_reused_slot_name():
    """A new slot with the name of a removed one starts with an empty window."""
    at, spider, downloader = _window_setup(3)
    old = _add_slot(downloader, delay=0.0)
    _download(at, spider, latency=2.0)
    _download(at, spider, latency=2.0)
    assert list(at._latencies[old]) == [2.0, 2.0]

    new = _add_slot(downloader, delay=0.0)
    assert new is not old
    _download(at, spider, latency=8.0)

    assert list(at._latencies[new]) == [8.0]
    assert new.delay == 4.0
    assert list(at._latencies[old]) == [2.0, 2.0]


def test_latency_window_per_crawler():
    """Windows are not shared between crawlers."""
    at1, spider1, downloader1 = _window_setup(3)
    at2, spider2, downloader2 = _window_setup(3)
    slot1 = _add_slot(downloader1, delay=0.0)
    slot2 = _add_slot(downloader2, delay=0.0)

    _download(at1, spider1, latency=2.0)
    _download(at1, spider1, latency=2.0)
    _download(at2, spider2, latency=10.0)

    assert at1._latencies is not at2._latencies
    assert list(at1._latencies[slot1]) == [2.0, 2.0]
    assert list(at2._latencies[slot2]) == [10.0]


def test_latency_window_debug_logs_median(caplog):
    """The debug line reports the median latency used for the adjustment."""
    at, spider, downloader = _window_setup(3, AUTOTHROTTLE_DEBUG=True)
    slot = _add_slot(downloader, delay=2.0)
    slot.transferring = (None, None)

    _download(at, spider, latency=2.0)
    caplog.clear()
    with caplog.at_level(INFO):
        _download(at, spider, latency=100.0)

    # Median of (2, 100) is 51 -> target 25.5, so the delay jumps to it.
    assert caplog.record_tuples == [
        (
            "scrapy.extensions.throttle",
            INFO,
            "slot: foo | conc: 2 | delay:25500 ms (+24000) | latency:51000 ms | size:     0 bytes",
        ),
    ]
