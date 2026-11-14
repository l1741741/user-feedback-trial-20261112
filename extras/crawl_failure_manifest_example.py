#!/usr/bin/env python3
"""Local example: record crawl failures and build a next-batch URL plan.

Simulates a finished crawl batch (download failures, a parse failure with
non-restorable callback context, and an item rejected by a pipeline), then
generates the read-only replay plan that operations would hand to the next
crawl batch.

Run from the repository root:

    python extras/crawl_failure_manifest_example.py

Everything is written under a temporary directory; no network access and no
real crawl happens.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from twisted.python.failure import Failure

from scrapy.exceptions import DropItem
from scrapy.failuremanifest import generate_replay_plan
from scrapy.http import Request, Response
from scrapy.item import Field, Item
from scrapy.spiders import Spider
from scrapy.utils.test import get_crawler


class ProductItem(Item):
    name = Field()


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="failure-manifest-example-"))
    manifest_path = workdir / "failures.jsonl"
    plan_path = workdir / "next_batch_plan.json"

    crawler = get_crawler(
        settings_dict={
            "CRAWL_FAILURE_MANIFEST": {
                "path": str(manifest_path),
                "batch_id": "batch-2026-11-14",
            }
        }
    )
    formatter = crawler.logformatter
    spider = Spider.from_crawler(crawler, "shop")

    # 1. A product page whose download failed terminally after 2 retries.
    request = Request(
        "https://shop.example.com/products/1",
        callback=spider.parse,
        cb_kwargs={"category": "shoes"},
    )
    request.meta["retry_times"] = 2
    formatter.download_error(Failure(TimeoutError("timed out")), request, spider)

    # 2. The same URL failing again (duplicate terminal signal): merged into
    #    a single manifest entry with occurrences=2.
    retry = Request(
        "https://shop.example.com/products/1",
        callback=spider.parse,
        cb_kwargs={"category": "shoes"},
    )
    retry.meta["retry_times"] = 2
    formatter.download_error(Failure(TimeoutError("timed out")), retry, spider)

    # 3. A parse callback failure: the page was fetched but parsing crashed.
    parse_request = Request("https://shop.example.com/products/2")
    response = Response(
        "https://shop.example.com/products/2", status=200, request=parse_request
    )
    formatter.spider_error(Failure(KeyError("price")), parse_request, response, spider)

    # 4. A failure whose callback context cannot be restored as JSON: kept in
    #    the manifest, but marked non-replayable with the reason.
    weird = Request(
        "https://shop.example.com/products/3",
        callback=spider.parse,
        cb_kwargs={"session": object()},
    )
    formatter.download_error(Failure(ValueError("boom")), weird, spider)

    # 5. An item rejected by a pipeline: NOT a URL miss, excluded from the
    #    replay plan (counted separately).
    ok_request = Request("https://shop.example.com/products/4")
    ok_response = Response(
        "https://shop.example.com/products/4", status=200, request=ok_request
    )
    formatter.dropped(
        ProductItem(name="Sneaker"), DropItem("missing price"), ok_response, spider
    )

    formatter._failure_manifest.close()  # normally fired by spider_closed

    plan = generate_replay_plan(
        manifest_path, batch_id="batch-2026-11-14", output_path=plan_path
    )

    print(f"manifest: {manifest_path}")
    print(f"plan:     {plan_path}\n")
    print("next-batch URLs:")
    for entry in plan["requests"]:
        print(
            f"  {entry['method']} {entry['url']} (stage={entry['stage']}, "
            f"retry_times={entry['retry_times']}, occurrences={entry['occurrences']})"
        )
    print("\ncounts:")
    print(json.dumps(plan["counts"], indent=2, sort_keys=True))
    print("\nnon-replayable (need manual handling):")
    for entry in plan["non_replayable"]:
        print(f"  {entry['url']}: {entry['reason']}")


if __name__ == "__main__":
    main()
