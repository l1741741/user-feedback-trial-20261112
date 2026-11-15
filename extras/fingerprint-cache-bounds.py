"""Demonstrate the bounded per-request fingerprint combination cache.

Simulates a long-running job that computes the fingerprint of a single,
long-lived request with many different include_headers combinations, and
shows that the per-request cache stays bounded, evicts the least recently
used combination, and is released with the request.

Runs entirely offline (no crawler, no network):

    python extras/fingerprint-cache-bounds.py
"""

import gc
import hashlib
import weakref

from scrapy import Request
from scrapy.utils import request as request_module
from scrapy.utils.request import (
    _FINGERPRINT_CACHE_MAX_SIZE,
    _fingerprint_cache,
    fingerprint,
)


def main() -> None:
    limit = _FINGERPRINT_CACHE_MAX_SIZE
    request = Request("https://example.org/", headers={"A": "1"})
    print(f"Per-request combination limit: {limit}")

    # Try more distinct combinations than the cache can hold.
    for i in range(limit + 3):
        fingerprint(request, include_headers=[f"X-H{i}"])
    size = len(_fingerprint_cache[request])
    print(f"After {limit + 3} distinct combinations: cache size = {size}")
    assert size == limit

    # Combinations equal after case-folding and sorting share one slot.
    fingerprint(request, include_headers=["X-New", "x-h1"])
    before = len(_fingerprint_cache[request])
    fingerprint(request, include_headers=["X-H1", "x-new"])
    after = len(_fingerprint_cache[request])
    print(f"Equivalent case/order combination reuses a slot: {before == after}")
    assert before == after

    # A one-shot generator works and is consumed exactly once.
    gen = (h for h in ["X-Gen"])
    fingerprint(request, include_headers=gen)
    assert list(gen) == []
    print("One-shot generator include_headers consumed once: True")

    # The oldest combination was evicted and is recomputed on demand.
    computations = 0
    real_sha1 = hashlib.sha1

    def counting_sha1(*args, **kwargs):
        nonlocal computations
        computations += 1
        return real_sha1(*args, **kwargs)

    request_module.hashlib.sha1 = counting_sha1
    try:
        fingerprint(request, include_headers=[f"X-H{limit - 1}"])  # cache hit
        assert computations == 0
        fingerprint(request, include_headers=["X-H0"])  # evicted: recomputed
        assert computations == 1
        print("Evicted combination recomputed on demand: True")
    finally:
        request_module.hashlib.sha1 = real_sha1
    assert len(_fingerprint_cache[request]) == limit

    # Cached combinations are released together with the request.
    temporary = Request("https://example.org/tmp")
    fingerprint(temporary, include_headers=["X-H0"])
    entries_before = len(_fingerprint_cache)
    ref = weakref.ref(temporary)
    del temporary
    gc.collect()
    assert ref() is None
    assert len(_fingerprint_cache) == entries_before - 1
    print("Cache entries released when the request is collected: True")


if __name__ == "__main__":
    main()
