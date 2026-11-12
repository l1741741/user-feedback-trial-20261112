"""Live demonstration of the atomic filesystem HTTP cache storage.

Run with the project environment, e.g.:

    .venv/bin/python -I demo/httpcache_atomic_demo.py

It exercises, against a real temporary directory and in both plain and
HTTPCACHE_GZIP modes:

1. A normal cache hit after a store.
2. A cache miss after the on-disk entry is corrupted.
3. The previous complete response still being served after a new write fails.
"""

from __future__ import annotations

import gzip
import shutil
import tempfile
from pathlib import Path

from scrapy.extensions.httpcache import FilesystemCacheStorage
from scrapy.http import Request, Response
from scrapy.settings import Settings
from scrapy.spiders import Spider


def build_storage(cache_dir: str, *, use_gzip: bool) -> FilesystemCacheStorage:
    settings = Settings(
        {
            "HTTPCACHE_DIR": cache_dir,
            "HTTPCACHE_GZIP": use_gzip,
            "HTTPCACHE_EXPIRATION_SECS": 0,
        }
    )
    storage = FilesystemCacheStorage(settings)
    spider = Spider("demo-spider")
    storage._fingerprinter = _Fingerprinter()
    return storage


class _Fingerprinter:
    def fingerprint(self, request: Request) -> bytes:
        import hashlib

        return hashlib.sha1(request.url.encode()).digest()


def open_entry_generation(storage: FilesystemCacheStorage, request: Request) -> Path:
    entry_dir = Path(storage._get_request_path(_spider, request))
    pointer = entry_dir / storage._POINTER_FILENAME
    return entry_dir / pointer.read_text(encoding="utf-8").strip()


_spider = Spider("demo-spider")


def run(use_gzip: bool) -> None:
    mode = "HTTPCACHE_GZIP" if use_gzip else "plain"
    print(f"\n=== {mode} mode ===")
    cache_dir = tempfile.mkdtemp(prefix=f"httpcache-demo-{mode}-")
    storage = build_storage(cache_dir, use_gzip=use_gzip)
    request = Request("http://cache.example.com/resource")
    response = Response(
        request.url,
        headers={"Content-Type": "text/html"},
        body=b"first complete body",
        status=200,
    )
    try:
        # 1. Normal hit.
        assert storage.retrieve_response(_spider, request) is None
        storage.store_response(_spider, request, response)
        hit = storage.retrieve_response(_spider, request)
        assert hit is not None
        print(f"[1] normal hit: status={hit.status} body={hit.body!r}")

        # 2. Corrupt the body on disk, then read: it must be a logged miss,
        #    not an exception reaching the caller.
        generation = open_entry_generation(storage, request)
        body_path = generation / "response_body"
        if use_gzip:
            body_path.write_bytes(body_path.read_bytes()[:-8])
        else:
            body_path.unlink()
        damaged_miss = storage.retrieve_response(_spider, request)
        assert damaged_miss is None
        print("[2] corrupted entry -> safe cache miss (no exception raised)")

        # 3. Store again, then make the next write fail halfway. The previous
        #    complete response must remain readable.
        storage.store_response(_spider, request, response)
        real_open = storage._open

        def failing_open(path, mode="rb", *args, **kwargs):
            if "w" in mode and Path(path).name == "response_body":
                raise OSError("simulated disk full during write")
            return real_open(path, mode, *args, **kwargs)

        storage._open = failing_open
        try:
            storage.store_response(
                _spider,
                request,
                response.replace(body=b"this body is never published"),
            )
        except OSError as exc:
            print(f"[3] injected write failure: {exc}")
        finally:
            storage._open = real_open
        recovered = storage.retrieve_response(_spider, request)
        assert recovered is not None
        assert recovered.body == b"first complete body"
        print(f"[3] previous response still served: body={recovered.body!r}")
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


if __name__ == "__main__":
    run(use_gzip=False)
    run(use_gzip=True)
    print("\nAll demonstration assertions passed.")
