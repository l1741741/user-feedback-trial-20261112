"""
Multi-process evidence: separate OS processes publish through the same
on-disk SQLite queue while another process consumes, proving the hard limit
holds across real connection/process boundaries (not merely threads).
"""
import multiprocessing as mp
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from huey import SqliteHuey, BULK_SUCCESS  # noqa: E402

LIMIT = 8
N_PER_PROCESS = 200


def publisher(db, worker_id, queue):
    from mp_tasks import mp_task
    huey = SqliteHuey(name='mpq', filename=db, timeout=15)
    # Each process has its own registry; register the shared task class.
    huey._registry.register(mp_task.task_class)
    session = huey.bulk_enqueue(
        (mp_task.s(worker_id * 100000 + i) for i in range(N_PER_PROCESS)),
        max_in_flight=4, queue_limit=LIMIT, timeout=60,
        poll_interval=0.002)
    statuses = {}
    for receipt in session:
        statuses[receipt.status] = statuses.get(receipt.status, 0) + 1
    session.close()
    huey.storage.close()
    queue.put((worker_id, statuses))


def consumer(db, duration, queue):
    from mp_tasks import mp_task
    huey = SqliteHuey(name='mpq', filename=db, timeout=15)
    huey._registry.register(mp_task.task_class)
    deadline = time.monotonic() + duration
    max_seen = 0
    dequeued = 0
    while time.monotonic() < deadline:
        if huey.storage.dequeue() is None:
            time.sleep(0.001)
            continue
        dequeued += 1
        size = huey.storage.queue_size()
        if size > max_seen:
            max_seen = size
    huey.storage.close()
    queue.put(('consumer', dequeued, max_seen))


if __name__ == '__main__':
    mp.set_start_method('spawn', force=True)
    db = os.path.join(tempfile.mkdtemp(prefix='mp-bulk-'), 'mp.db')
    ctx_q = mp.Queue()

    consumer_proc = mp.Process(target=consumer, args=(db, 12.0, ctx_q))
    consumer_proc.start()
    time.sleep(0.5)

    procs = [mp.Process(target=publisher, args=(db, wid, ctx_q))
             for wid in range(2)]
    t0 = time.monotonic()
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    elapsed = time.monotonic() - t0
    consumer_proc.join(20)

    results = []
    while not ctx_q.empty():
        results.append(ctx_q.get())

    pub = sorted(r for r in results if r[0] in (0, 1))
    con = [r for r in results if r[0] == 'consumer'][0]
    total_ok = sum(s.get(BULK_SUCCESS, 0) for _, s in pub)

    print('publisher statuses:', pub)
    print('consumer dequeued:', con[1], 'max queue size seen:', con[2],
          'limit:', LIMIT)
    print('elapsed: %.2fs' % elapsed)
    assert total_ok == 2 * N_PER_PROCESS, pub
    assert con[2] <= LIMIT, 'HARD LIMIT BREACHED ACROSS PROCESSES'
    print('PASS: SQLite hard limit held across separate OS processes')
