"""
Stronger SQLite hard-limit evidence: instrument the actual insert commit
point (not a concurrent observer) and record the post-insert queue size for
every enqueue, proving no committed row ever pushes the queue past the limit.
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from huey import SqliteHuey  # noqa: E402

LIMIT = 10
N_PER = 300
db = os.path.join(tempfile.mkdtemp(prefix='hardlimit-'), 'h.db')
huey = SqliteHuey(name='q', filename=db, timeout=15)
other = SqliteHuey(name='q', filename=db, timeout=15)

post_insert_sizes = []
instrument_lock = threading.Lock()
orig = SqliteHuey.storage_class.enqueue_if_space


def instrumented(self, data, priority=None, limit=None):
    accepted = orig(self, data, priority, limit)
    if accepted:
        # Reads the durable size after the insert transaction committed.
        size = self.queue_size()
        with instrument_lock:
            post_insert_sizes.append(size)
    return accepted


SqliteHuey.storage_class.enqueue_if_space = instrumented


@huey.task()
def task(n):
    return n


stop = threading.Event()


def consumer():
    while not stop.is_set():
        if huey.storage.dequeue() is None:
            time.sleep(0.0005)


ct = threading.Thread(target=consumer)
ct.start()
results = [[], []]


def produce(storage, idx):
    sess = huey.bulk_enqueue(
        (task.s(idx * 10000 + i) for i in range(N_PER)),
        max_in_flight=4, queue_limit=LIMIT, timeout=30,
        poll_interval=0.001)
    sess.huey.storage = storage
    for r in sess:
        results[idx].append(r)
    sess.close()


t1 = threading.Thread(target=produce, args=(huey.storage, 0))
t2 = threading.Thread(target=produce, args=(other.storage, 1))
t1.start()
t2.start()
t1.join()
t2.join()
stop.set()
ct.join()

total = len(results[0]) + len(results[1])
violations = [s for s in post_insert_sizes if s > LIMIT]
print('committed inserts measured:', len(post_insert_sizes),
      'expected:', total)
print('max post-insert queue size:', max(post_insert_sizes),
      'hard limit:', LIMIT)
print('violations (> limit):', len(violations))
assert len(post_insert_sizes) == total
assert not violations, 'HARD LIMIT BREACHED: %r' % violations[:5]
assert max(post_insert_sizes) <= LIMIT
huey.storage.close()
other.storage.close()
print('PASS: every committed insert kept the queue within the hard limit')
