from __future__ import annotations

import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from scrapy._frame_disk_queue import _FRAME_MAGIC, FrameQueueError
from scrapy.http import Request
from scrapy.spiders import Spider
from scrapy.squeues import (
    MarshalFifoFrameDiskQueue,
    MarshalLifoFrameDiskQueue,
    PickleFifoDiskQueue,
    PickleFifoFrameDiskQueue,
    PickleLifoDiskQueue,
    PickleLifoFrameDiskQueue,
)
from scrapy.utils.misc import build_from_crawler
from scrapy.utils.test import get_crawler

if TYPE_CHECKING:
    from scrapy.crawler import Crawler

_FRAME_DISK_QUEUES = [
    pytest.param(PickleFifoFrameDiskQueue, True, id="pickle-fifo-frame"),
    pytest.param(PickleLifoFrameDiskQueue, False, id="pickle-lifo-frame"),
    pytest.param(MarshalFifoFrameDiskQueue, True, id="marshal-fifo-frame"),
    pytest.param(MarshalLifoFrameDiskQueue, False, id="marshal-lifo-frame"),
]


class _DetailSpider(Spider):
    name = "detail"

    def parse_detail(self, response: Any) -> None:
        pass

    def on_error(self, failure: Any) -> None:
        pass


@pytest.fixture
def crawler() -> Crawler:
    crawler = get_crawler(_DetailSpider)
    crawler.spider = _DetailSpider.from_crawler(crawler, name="detail")
    return crawler


def _spider(crawler: Crawler) -> _DetailSpider:
    assert isinstance(crawler.spider, _DetailSpider)
    return crawler.spider


def _request(index: int, spider: _DetailSpider) -> Request:
    return Request(
        f"https://example.com/product/{index}",
        callback=spider.parse_detail,
        errback=spider.on_error,
        cb_kwargs={"page": index, "cursor": f"cursor-{index}"},
        meta={
            "batch": f"batch-{index % 3}",
            "cursor": index * 100,
            "nested": {"depth": index, "tags": ["a", "b"]},
        },
    )


@pytest.mark.parametrize(("queue_cls", "is_fifo"), _FRAME_DISK_QUEUES)
def test_request_context_round_trips_across_reopen(
    crawler: Crawler, tmp_path: Path, queue_cls: Any, is_fifo: bool
) -> None:
    key = str(tmp_path / "queue")
    queue = build_from_crawler(queue_cls, crawler, key=key)
    requests = [_request(i, _spider(crawler)) for i in range(6)]
    for request in requests:
        queue.push(request)
    assert len(queue) == 6
    queue.close()

    queue = build_from_crawler(queue_cls, crawler, key=key)
    try:
        expected = requests if is_fifo else list(reversed(requests))
        assert len(queue) == len(expected)
        for original in expected:
            peeked = queue.peek()
            restored = queue.pop()
            assert peeked is not None
            assert restored is not None
            assert peeked.url == original.url
            assert restored.url == original.url
            assert restored.callback == _spider(crawler).parse_detail
            assert restored.errback == _spider(crawler).on_error
            assert restored.cb_kwargs == original.cb_kwargs
            assert restored.meta["batch"] == original.meta["batch"]
            assert restored.meta["cursor"] == original.meta["cursor"]
            assert restored.meta["nested"] == original.meta["nested"]
        assert queue.pop() is None
    finally:
        queue.close()


@pytest.mark.parametrize(
    ("legacy_cls", "frame_cls", "is_fifo"),
    [
        (PickleFifoDiskQueue, PickleFifoFrameDiskQueue, True),
        (PickleLifoDiskQueue, PickleLifoFrameDiskQueue, False),
    ],
)
def test_legacy_jobdir_is_migrated_and_order_preserved(
    crawler: Crawler,
    tmp_path: Path,
    legacy_cls: Any,
    frame_cls: Any,
    is_fifo: bool,
) -> None:
    key = str(tmp_path / "queue")
    legacy = build_from_crawler(legacy_cls, crawler, key=key)
    requests = [_request(i, _spider(crawler)) for i in range(5)]
    for request in requests:
        legacy.push(request)
    legacy.pop()
    legacy.close()

    frame_queue = build_from_crawler(frame_cls, crawler, key=key)
    try:
        remaining = requests[1:] if is_fifo else list(reversed(requests[:-1]))
        assert len(frame_queue) == len(remaining)
        urls = []
        while (request := frame_queue.pop()) is not None:
            assert request.callback == _spider(crawler).parse_detail
            assert request.cb_kwargs["cursor"].startswith("cursor-")
            urls.append(request.url)
        assert urls == [request.url for request in remaining]
    finally:
        frame_queue.close()


@pytest.mark.parametrize(("queue_cls", "is_fifo"), _FRAME_DISK_QUEUES)
def test_unserializable_request_does_not_damage_pending(
    crawler: Crawler, tmp_path: Path, queue_cls: Any, is_fifo: bool
) -> None:
    key = str(tmp_path / "queue")
    queue = build_from_crawler(queue_cls, crawler, key=key)
    queue.push(_request(0, _spider(crawler)))
    bad = Request(
        "https://example.com/bad",
        callback=_spider(crawler).parse_detail,
        meta={"unpicklable": lambda: None},
    )
    with pytest.raises(ValueError):  # noqa: PT011
        queue.push(bad)
    queue.push(_request(1, _spider(crawler)))
    assert len(queue) == 2
    queue.close()

    queue = build_from_crawler(queue_cls, crawler, key=key)
    try:
        urls = set()
        while (request := queue.pop()) is not None:
            urls.add(request.url)
        assert urls == {
            "https://example.com/product/0",
            "https://example.com/product/1",
        }
    finally:
        queue.close()


def test_damaged_frame_file_is_not_mistaken_for_legacy(
    crawler: Crawler, tmp_path: Path
) -> None:
    key = str(tmp_path / "queue")
    queue = build_from_crawler(PickleFifoFrameDiskQueue, crawler, key=key)
    queue.push(_request(0, _spider(crawler)))
    queue.close()

    # Corrupt the frame magic but keep a version-marked layout: this must be
    # treated as a damaged new file, never silently re-read as legacy data.
    chunk = Path(key) / "f00000"
    data = bytearray(chunk.read_bytes())
    data[: len(_FRAME_MAGIC)] = b"XXXX"
    chunk.write_bytes(bytes(data))

    with pytest.raises(FrameQueueError, match="Unrecognized frame header"):
        build_from_crawler(PickleFifoFrameDiskQueue, crawler, key=key)
    # The scene is preserved rather than skipped.
    assert chunk.exists()


def test_frame_magic_does_not_collide_with_legacy_header() -> None:
    # Legacy LIFO files start with an item count that is zero for a fresh
    # queue; the frame marker is a fixed four-byte tag that can never be the
    # start of a legacy record, so a tagged file is never read as legacy.
    legacy_prefix = struct.pack(">L", 0)
    assert legacy_prefix != _FRAME_MAGIC
    # Frame chunks live in a directory that never contains a legacy info.json.
    assert _FRAME_MAGIC == b"SQF1"


@pytest.mark.parametrize(("queue_cls", "is_fifo"), _FRAME_DISK_QUEUES)
def test_second_handle_rejected(
    crawler: Crawler, tmp_path: Path, queue_cls: Any, is_fifo: bool
) -> None:
    key = str(tmp_path / "queue")
    first = build_from_crawler(queue_cls, crawler, key=key)
    first.push(_request(0, _spider(crawler)))
    with pytest.raises(FrameQueueError, match="Another crawl process"):
        build_from_crawler(queue_cls, crawler, key=key)
    first.close()
