#!/usr/bin/env python3
"""
Multi-process verification for tenant-fair scheduling.

Threads share the SqliteStorage object and its in-process lock; real consumer
processes do not. This script forks worker processes that each open their own
SqliteHuey connection against the same database file, then dequeue
concurrently. Fairness must hold because the rotation lives in the
``tenant_state`` database table, not in any process memory.

Run:
    .venv/bin/python -I scripts/offline_tenant_fair_multiprocess.py
"""
import multiprocessing as mp
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from huey import SqliteHuey  # noqa: E402


TENANTS = ('alpha', 'beta', 'gamma', 'delta')
PER_TENANT = 25
N_WORKERS = 4


def worker(db_path, queue):
    # Each process builds a completely independent storage connection and
    # re-registers the task class, mirroring a consumer re-importing modules.
    huey = SqliteHuey(name='mp', filename=db_path, timeout=30,
                      tenant_fair=True)

    @huey.task(name='shared_task')
    def shared_task(n):
        return n

    consumed = []
    while True:
        try:
            data = huey.storage.dequeue()
        except Exception as exc:  # pragma: no cover - surfaced as failure.
            queue.put(('error', repr(exc)))
            break
        if data is None:
            break
        task = huey.deserialize_task(data)
        consumed.append(task.tenant)
    huey.storage.close()
    queue.put(('ok', consumed))


def main():
    tmp = tempfile.mkdtemp(prefix='tenant-fair-mp-')
    db_path = os.path.join(tmp, 'mp.db')

    publisher = SqliteHuey(name='mp', filename=db_path, timeout=30,
                           tenant_fair=True)

    @publisher.task(name='shared_task')
    def task(n):
        return n

    for i in range(PER_TENANT):
        for tenant in TENANTS:
            task((tenant, i), tenant=tenant,
                 priority=(i % 7))  # Mixed priorities.

    ctx = mp.get_context('fork')
    queue = ctx.Queue()
    procs = [ctx.Process(target=worker, args=(db_path, queue))
             for _ in range(N_WORKERS)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        if p.is_alive():
            p.terminate()
            print('FAIL: worker %s timed out' % p.pid)
            return 1

    sequence, errors = [], []
    while not queue.empty():
        kind, payload = queue.get()
        if kind == 'error':
            errors.append(payload)
        else:
            sequence.extend(payload)

    if errors:
        print('FAIL: worker errors: %s' % errors)
        return 1

    expected = PER_TENANT * len(TENANTS)
    if len(sequence) != expected:
        print('FAIL: consumed %s of %s tasks' % (len(sequence), expected))
        return 1

    counts = {t: sequence.count(t) for t in TENANTS}
    if any(count != PER_TENANT for count in counts.values()):
        print('FAIL: uneven tenant shares: %s' % counts)
        return 1

    # While all four tenants are active, no process may serve the same tenant
    # twice in a row. Check the prefix where every tenant still has backlog.
    head = sequence[:len(TENANTS) * 2]
    for i in range(1, len(head)):
        if head[i] == head[i - 1]:
            print('FAIL: repeated tenant %r at index %s in %s'
                  % (head[i], i, head))
            return 1

    leftovers = publisher.storage.sql(
        'select count(*) from tenant_state', results=True)[0][0]
    publisher.storage.close()
    if leftovers:
        print('FAIL: stale rotation rows after drain: %s' % leftovers)
        return 1

    print('PASS: %d independent processes consumed %d tasks fairly'
          % (N_WORKERS, expected))
    print('      per-tenant counts: %s' % counts)
    print('      observed rotation head: %s' % head)
    return 0


if __name__ == '__main__':
    sys.exit(main())
