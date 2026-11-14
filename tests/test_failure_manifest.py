from __future__ import annotations

import json
import logging
import threading

from twisted.python.failure import Failure

from scrapy.exceptions import DropItem
from scrapy.failuremanifest import generate_replay_plan, read_manifest
from scrapy.http import Request, Response
from scrapy.item import Field, Item
from scrapy.spidermiddlewares.httperror import HttpError
from scrapy.spiders import Spider
from scrapy.utils.test import get_crawler


class ProductItem(Item):
    name = Field()


def make_formatter(tmp_path, batch_id="batch-1", **overrides):
    settings = {
        "CRAWL_FAILURE_MANIFEST": {
            "path": str(tmp_path / "manifest.jsonl"),
            "batch_id": batch_id,
        },
        **overrides,
    }
    crawler = get_crawler(settings_dict=settings)
    formatter = crawler.logformatter
    spider = Spider.from_crawler(crawler, "shop")
    return formatter, spider, tmp_path / "manifest.jsonl"


def download_fail(formatter, spider, url, retry_times=0, method="GET", body=b""):
    request = Request(url, method=method, body=body)
    request.meta["retry_times"] = retry_times
    formatter.download_error(Failure(ValueError("boom")), request, spider)
    return request


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def failure_lines(path):
    return [r for r in read_lines(path) if r.get("record_type") == "failure"]


class TestDisabledByDefault:
    def test_no_recorder_without_setting(self):
        crawler = get_crawler()
        formatter = crawler.logformatter
        assert formatter._failure_manifest is None

    def test_log_methods_still_work(self):
        crawler = get_crawler()
        formatter = crawler.logformatter
        spider = Spider.from_crawler(crawler, "default")
        request = Request("http://example.com/")
        logkws = formatter.download_error(Failure(ValueError("x")), request, spider)
        assert logkws["level"] == logging.ERROR

    def test_no_file_created(self, tmp_path):
        crawler = get_crawler()
        formatter = crawler.logformatter
        spider = Spider.from_crawler(crawler, "default")
        request = Request("http://example.com/")
        formatter.download_error(Failure(ValueError("x")), request, spider)
        assert list(tmp_path.iterdir()) == []


class TestDownloadFailureRecording:
    def test_terminal_failure_recorded_with_final_retry_times(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        download_fail(formatter, spider, "http://example.com/p1", retry_times=2)
        formatter._failure_manifest.close()
        (record,) = failure_lines(path)
        assert record["stage"] == "download"
        assert record["url"] == "http://example.com/p1"
        assert record["method"] == "GET"
        assert record["retry_times"] == 2
        assert record["error_class"] == "ValueError"
        assert record["spider"] == "shop"
        assert record["batch_id"] == "batch-1"
        assert record["replayable"] is True
        assert record["occurrences"] == 1

    def test_successful_crawl_leaves_no_record(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        request = Request("http://example.com/p1")
        response = Response("http://example.com/p1", request=request)
        formatter.crawled(request, response, spider)
        formatter._failure_manifest.close()
        assert failure_lines(path) == []

    def test_log_output_unchanged_when_enabled(self, tmp_path):
        formatter, spider, _ = make_formatter(tmp_path)
        crawler = get_crawler()
        plain = crawler.logformatter
        plain_spider = Spider.from_crawler(crawler, "shop")
        failure = Failure(ValueError("boom"))
        request = Request("http://example.com/p1")
        got = formatter.download_error(failure, request, spider)
        want = plain.download_error(failure, request, plain_spider)
        assert got["level"] == want["level"]
        assert got["msg"] == want["msg"]
        assert got["args"] == want["args"]
        formatter._failure_manifest.close()


class TestFingerprintMerging:
    def test_duplicate_terminal_failures_merge(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        download_fail(formatter, spider, "http://example.com/p1", retry_times=1)
        download_fail(formatter, spider, "http://example.com/p1", retry_times=2)
        formatter._failure_manifest.close()
        records, _, _ = read_manifest(path)
        (record,) = records
        assert record["occurrences"] == 2
        assert record["retry_times"] == 2

    def test_different_fingerprints_not_merged(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        download_fail(formatter, spider, "http://example.com/p1")
        download_fail(
            formatter, spider, "http://example.com/p1", method="POST", body=b"a=1"
        )
        download_fail(formatter, spider, "http://example.com/p2")
        formatter._failure_manifest.close()
        records, _, _ = read_manifest(path)
        assert len(records) == 3

    def test_success_elsewhere_does_not_swallow_failure(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        ok_request = Request("http://example.com/ok")
        formatter.crawled(
            ok_request, Response("http://example.com/ok", request=ok_request), spider
        )
        download_fail(formatter, spider, "http://example.com/bad")
        formatter._failure_manifest.close()
        records, _, _ = read_manifest(path)
        assert [r["url"] for r in records] == ["http://example.com/bad"]


class TestCallbackContext:
    def test_serializable_cb_kwargs_recorded(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        request = Request(
            "http://example.com/p1",
            callback=spider.parse,
            cb_kwargs={"page": 3, "tags": ["a", "b"]},
        )
        formatter.download_error(Failure(ValueError("x")), request, spider)
        formatter._failure_manifest.close()
        (record,) = failure_lines(path)
        assert record["callback"] == "parse"
        assert record["cb_kwargs"] == {"page": 3, "tags": ["a", "b"]}
        assert record["replayable"] is True

    def test_non_serializable_cb_kwargs_marked_not_replayable(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        request = Request(
            "http://example.com/p1",
            callback=spider.parse,
            cb_kwargs={"session": object()},
        )
        formatter.download_error(Failure(ValueError("x")), request, spider)
        formatter._failure_manifest.close()
        (record,) = failure_lines(path)
        assert record["replayable"] is False
        assert record["cb_kwargs"] is None
        assert "not JSON-serializable" in record["non_replayable_reason"]
        # the reason must not smuggle a repr of the value in as JSON
        assert "object at 0x" not in json.dumps(record)

    def test_lambda_callback_marked_not_replayable(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        request = Request("http://example.com/p1", callback=lambda r: None)
        formatter.download_error(Failure(ValueError("x")), request, spider)
        formatter._failure_manifest.close()
        (record,) = failure_lines(path)
        assert record["replayable"] is False
        assert record["callback"] is None


class TestStagesAndExceptions:
    def test_parse_failure_with_http_status(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        request = Request("http://example.com/p1")
        response = Response("http://example.com/p1", status=200, request=request)
        formatter.spider_error(Failure(KeyError("price")), request, response, spider)
        formatter._failure_manifest.close()
        (record,) = failure_lines(path)
        assert record["stage"] == "parse"
        assert record["error_class"] == "KeyError"
        assert record["http_status"] == 200
        assert record["url_fetched"] is True

    def test_spider_error_on_download_failure_stays_download_stage(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        request = Request("http://example.com/p1")
        formatter.spider_error(
            Failure(ValueError("x")), request, Failure(ValueError("x")), spider
        )
        formatter._failure_manifest.close()
        (record,) = failure_lines(path)
        assert record["stage"] == "download"

    def test_http_error_status_recorded(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        request = Request("http://example.com/p1")
        response = Response("http://example.com/p1", status=503, request=request)
        formatter.download_error(Failure(HttpError(response)), request, spider)
        formatter._failure_manifest.close()
        (record,) = failure_lines(path)
        assert record["error_class"] == "HttpError"
        assert record["http_status"] == 503


class TestItemDropped:
    def test_dropped_item_not_a_url_miss(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        request = Request("http://example.com/p1")
        response = Response("http://example.com/p1", status=200, request=request)
        formatter.dropped(
            ProductItem(name="x"), DropItem("missing price"), response, spider
        )
        formatter._failure_manifest.close()
        (record,) = failure_lines(path)
        assert record["stage"] == "item_dropped"
        assert record["url_fetched"] is True
        assert record["url"] == "http://example.com/p1"
        assert record["drop_reason"] == "missing price"
        assert record["replayable"] is False
        plan = generate_replay_plan(path, batch_id="batch-1")
        assert plan["requests"] == []
        assert plan["counts"]["items_dropped_excluded"] == 1
        assert plan["counts"]["url_misses"] == 0

    def test_dropped_item_without_response(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        formatter.dropped(ProductItem(name="x"), DropItem("bad"), None, spider)
        formatter._failure_manifest.close()
        (record,) = failure_lines(path)
        assert record["stage"] == "item_dropped"
        assert record["url_fetched"] is False
        assert record["url"] is None


class TestConcurrency:
    def test_concurrent_writes_do_not_interleave(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        recorder = formatter._failure_manifest

        def worker(n):
            for i in range(50):
                download_fail(
                    formatter, spider, f"http://example.com/t{n}/{i}", retry_times=i
                )

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        recorder.close()
        lines = path.read_text().splitlines()
        assert len(lines) == 8 * 50 + 1  # failures + batch_start
        for line in lines:
            json.loads(line)  # every line is complete, valid JSON


class TestReopenAndCorruption:
    def test_partial_tail_recovered_on_reopen(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        download_fail(formatter, spider, "http://example.com/p1")
        formatter._failure_manifest.close()
        # simulate a torn write from a crashed run
        with path.open("ab") as f:
            f.write(b'{"schema": 1, "bat')
        formatter2, spider2, _ = make_formatter(tmp_path, batch_id="batch-1")
        download_fail(formatter2, spider2, "http://example.com/p2")
        formatter2._failure_manifest.close()
        # every line in the manifest is valid again
        records = read_lines(path)
        urls = [r.get("url") for r in records if r.get("record_type") == "failure"]
        assert urls == ["http://example.com/p1", "http://example.com/p2"]
        # the partial tail was preserved, not silently dropped
        backup = path.with_name("manifest.jsonl.partial-tail")
        assert backup.exists()
        assert b'{"schema": 1, "bat' in backup.read_bytes()

    def test_corrupt_middle_line_warns_and_file_preserved(self, tmp_path, caplog):
        formatter, spider, path = make_formatter(tmp_path)
        download_fail(formatter, spider, "http://example.com/p1")
        download_fail(formatter, spider, "http://example.com/p2")
        formatter._failure_manifest.close()
        original = path.read_bytes()
        lines = original.splitlines(keepends=True)
        lines.insert(1, b"this is not json\n")
        path.write_bytes(b"".join(lines))
        corrupted = path.read_bytes()
        with caplog.at_level(logging.WARNING):
            plan = generate_replay_plan(path, batch_id="batch-1")
        assert plan["counts"]["corrupt_lines"] == 1
        assert plan["counts"]["corrupt_line_numbers"] == [2]
        assert plan["request_count"] == 2
        assert "corrupt line 2" in caplog.text
        assert path.read_bytes() == corrupted  # read-only: original preserved


class TestWriteFailure:
    def test_write_error_does_not_interrupt_crawl(self, tmp_path):
        formatter, spider, _path = make_formatter(tmp_path)
        recorder = formatter._failure_manifest

        def broken_write(_line):
            raise OSError("disk full")

        recorder._writer._file.write = broken_write
        # must not raise
        download_fail(formatter, spider, "http://example.com/p1")
        assert recorder._writer.failed_writes == 1
        recorder._writer._file.close()  # close the real file object directly
        recorder._writer._closed = True

    def test_unwritable_path_disables_recording(self, tmp_path, caplog):
        settings = {
            "CRAWL_FAILURE_MANIFEST": {
                "path": str(tmp_path / "missing" / "dir" / "m.jsonl"),
                "batch_id": "b",
            }
        }
        crawler = get_crawler(settings_dict=settings)
        formatter = crawler.logformatter
        # parent dirs are created automatically, so this succeeds...
        assert formatter._failure_manifest is not None
        formatter._failure_manifest.close()
        # ...but a path that is a directory cannot be opened
        settings["CRAWL_FAILURE_MANIFEST"] = {"path": str(tmp_path), "batch_id": "b"}
        crawler2 = get_crawler(settings_dict=settings)
        with caplog.at_level(logging.WARNING):
            formatter2 = crawler2.logformatter
        assert formatter2._failure_manifest is None
        assert "failure recording is disabled" in caplog.text


class TestBatchIsolationAndPlan:
    def test_batches_are_isolated(self, tmp_path):
        f1, s1, path = make_formatter(tmp_path, batch_id="batch-1")
        download_fail(f1, s1, "http://example.com/a")
        f1._failure_manifest.close()
        f2, s2, _ = make_formatter(tmp_path, batch_id="batch-2")
        download_fail(f2, s2, "http://example.com/b")
        f2._failure_manifest.close()
        plan1 = generate_replay_plan(path, batch_id="batch-1")
        plan2 = generate_replay_plan(path, batch_id="batch-2")
        assert [r["url"] for r in plan1["requests"]] == ["http://example.com/a"]
        assert [r["url"] for r in plan2["requests"]] == ["http://example.com/b"]
        assert plan2["known_batches"] == ["batch-1", "batch-2"]

    def test_default_batch_is_latest(self, tmp_path):
        f1, s1, path = make_formatter(tmp_path, batch_id="batch-1")
        download_fail(f1, s1, "http://example.com/a")
        f1._failure_manifest.close()
        f2, s2, _ = make_formatter(tmp_path, batch_id="batch-2")
        download_fail(f2, s2, "http://example.com/b")
        f2._failure_manifest.close()
        plan = generate_replay_plan(path)
        assert plan["batch_id"] == "batch-2"

    def test_replay_plan_end_to_end(self, tmp_path):
        formatter, spider, path = make_formatter(tmp_path)
        download_fail(formatter, spider, "http://example.com/p1", retry_times=2)
        request = Request(
            "http://example.com/p2",
            callback=spider.parse,
            cb_kwargs={"session": object()},
        )
        formatter.download_error(Failure(ValueError("x")), request, spider)
        ok = Request("http://example.com/p3")
        formatter.dropped(
            ProductItem(name="x"),
            DropItem("no price"),
            Response("http://example.com/p3", request=ok),
            spider,
        )
        formatter._failure_manifest.close()
        out = tmp_path / "next_batch.json"
        plan = generate_replay_plan(path, batch_id="batch-1", output_path=out)
        assert plan["request_count"] == 1
        (entry,) = plan["requests"]
        assert entry["url"] == "http://example.com/p1"
        assert entry["retry_times"] == 2
        assert entry["stage"] == "download"
        assert len(plan["non_replayable"]) == 1
        assert plan["non_replayable"][0]["url"] == "http://example.com/p2"
        counts = plan["counts"]
        assert counts["download_failures"] == 2
        assert counts["replayable_urls"] == 1
        assert counts["non_replayable_urls"] == 1
        assert counts["items_dropped_excluded"] == 1
        on_disk = json.loads(out.read_text())
        assert on_disk == plan
