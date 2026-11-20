"""
Terminal evidence for bounded-memory bulk publishing.

Demonstrates, with a generator far larger than anything that could be
materialized comfortably, that:

1. the input generator is pulled incrementally (its side-effect counter stays
   close to the consumed count, never jumps to N up-front);
2. the peak number of in-flight tasks never exceeds the declared window;
3. memory used by buffered task payloads stays bounded regardless of N;
4. the SQLite hard queue limit is never breached by two concurrent producers
   while a slow consumer drains;
5. a stopped consumer and cancellation terminate without deadlock.
"""
import os
import sys
import threading
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from huey import MemoryHuey, SqliteHuey  # noqa: E402

WINDOW = 8
LIMIT = 12
N = 200_000  # Two hundred thousand tasks; never materialized into a list.


def section(title):
    print('\n=== %s ===' % title, flush=True)


# ---------------------------------------------------------------- memory
section('Memory back-pressure (MemoryHuey), N=%d' % N)
h = MemoryHuey(utc=False)


@h.task()
def payload(n):
    # A non-trivial payload so buffering all of them would be obvious.
    return ('x' * 512, n)


pulled = [0]
gen_lock = threading.Lock()


def huge_generator():
    for i in range(N):
        with gen_lock:
            pulled[0] += 1
        yield payload.s(i)


tracemalloc.start()
session = h.bulk_enqueue(huge_generator(), max_in_flight=WINDOW,
                         queue_limit=LIMIT, timeout=60, poll_interval=0.001)

consumed = 0
max_ahead = 0
sample_points = []
for receipt in session:
    consumed += 1
    h.storage.dequeue()  # Slow-ish consumer frees storage capacity.
    if consumed in (1, 1000, 50_000, 100_000, N):
        current, _peak = tracemalloc.get_traced_memory()
        ahead = pulled[0] - consumed
        max_ahead = max(max_ahead, ahead)
        sample_points.append((consumed, pulled[0], ahead,
                              session.peak_in_flight, current // 1024))
session.close()
current, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()

print('consumed=%d generator-pulled=%d' % (consumed, pulled[0]))
print('(consumed, pulled, pull-ahead, peak-in-flight, current-KiB) samples:')
for row in sample_points:
    print('  ', row)
print('peak in-flight observed: %d (window=%d)' %
      (session.peak_in_flight, WINDOW))
print('max generator pull-ahead over consumer: %d' % max_ahead)
print('tracemalloc peak: %.1f MiB for %d tasks (%.1f bytes/task if buffered)'
      % (peak / 1048576, N, peak / N))

assert consumed == N
assert pulled[0] == N
assert session.peak_in_flight <= WINDOW
# The generator never ran away: at most window + limit + a small scheduling
# margin ahead of the consumer at any sampled instant.
assert max_ahead <= WINDOW + LIMIT + 2
# If all N payloads (>=512 bytes each) were buffered, peak would exceed
# ~100 MiB. Bounded buffering keeps it far lower.
assert peak < 50 * 1048576, 'memory grew as if generator was materialized'
print('PASS: memory stays bounded; generator never fully expanded')


# ----------------------------------------------- SQLite hard limit, 2 procs
section('SQLite hard limit under two real producers')
import tempfile  # noqa: E402

db = os.path.join(tempfile.mkdtemp(prefix='evidence-'), 'bulk.db')
huey = SqliteHuey(name='q', filename=db, timeout=15)
other = SqliteHuey(name='q', filename=db, timeout=15)


@huey.task()
def task(n):
    return n


observed = [0]
olock = threading.Lock()
stop = threading.Event()


def consumer():
    while not stop.is_set():
        if huey.storage.dequeue() is None:
            time.sleep(0.0005)
            continue
        with olock:
            sz = huey.storage.queue_size()
            if sz > observed[0]:
                observed[0] = sz


ct = threading.Thread(target=consumer)
ct.start()
results = [[], []]


def produce(storage, idx, count):
    sess = huey.bulk_enqueue(
        (task.s(idx * 100000 + i) for i in range(count)),
        max_in_flight=6, queue_limit=LIMIT, timeout=60, poll_interval=0.001)
    sess.huey.storage = storage
    for r in sess:
        results[idx].append(r)
    sess.close()


t1 = threading.Thread(target=produce, args=(huey.storage, 0, N // 4))
t2 = threading.Thread(target=produce, args=(other.storage, 1, N // 4))
t0 = time.monotonic()
t1.start()
t2.start()
t1.join()
t2.join()
elapsed = time.monotonic() - t0
stop.set()
ct.join()

allr = results[0] + results[1]
print('producers=%d receipts=%d successes=%d elapsed=%.2fs' %
      (2, len(allr), sum(r.ok for r in allr), elapsed))
print('max on-disk queue size: %d (declared hard limit=%d)' %
      (observed[0], LIMIT))
assert len(allr) == N // 2
assert all(r.ok for r in allr)
assert observed[0] <= LIMIT
huey.storage.close()
other.storage.close()
print('PASS: SQLite hard limit never breached under concurrency')


# --------------------------------------------- cancellation / no deadlock
section('Cancellation with a stopped consumer (no deadlock)')
h2 = MemoryHuey(utc=False)


@h2.task()
def t2(n):
    return n


event = threading.Event()
sess = h2.bulk_enqueue((t2.s(i) for i in range(N)), max_in_flight=4,
                       queue_limit=4, cancel_event=event,
                       poll_interval=0.005)
first = next(sess)
time.sleep(0.05)  # Let it wedge against the full, unconsumed queue.
event.set()
t0 = time.monotonic()
sess.close()
dt = time.monotonic() - t0
print('first receipt: %s; close() after cancel returned in %.3fs' %
      (first.status, dt))
assert dt < 5.0
assert not sess._thread.is_alive()
assert sess._slots._value == 4
print('PASS: clean termination, all in-flight control records released')

print('\nALL EVIDENCE CHECKS PASSED', flush=True)
