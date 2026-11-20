#!/usr/bin/env python3
"""
Offline reproduction for tenant-fair scheduling on SqliteHuey.

This script is self-contained and only needs the stdlib sqlite3 backend that
ships with Python. It writes its database into a fresh temporary directory and
exercises the scenarios from BUSINESS_CASE.md:

  1. Default-off compatibility: without tenant_fair the queue is strict
     priority/FIFO, exactly like unmodified Huey.
  2. Continuous enqueue: a big tenant floods high-priority tasks while small
     tenants keep receiving bounded turns.
  3. High/low priority crossing: priority still decides order *within* a
     tenant but can never be used to jump another tenant's turn.
  4. Multiple concurrent consumers sharing one database file: all workers
     obey the same persisted rotation (no per-process round-robin lists).
  5. Restart: a brand-new Huey instance reopens the database and continues
     from the persisted rotation without bias.
  6. Delayed tasks only join the rotation once they become executable.
  7. Retries / task chains / chords keep an explicit tenant lineage.

Run it with the repository's interpreter, e.g.:
    .venv/bin/python scripts/offline_tenant_fair_repro.py

Exit code is non-zero if any invariant is violated.
"""
import datetime
import os
import sqlite3
import sys
import tempfile
import threading
from queue import Queue

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from huey import SqliteHuey  # noqa: E402
from huey.api import chord  # noqa: E402


failures = []


def check(label, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print('  [%s] %s%s' % (status, label, (' -- ' + detail) if detail else ''))
    if not condition:
        failures.append(label)


def build_huey(path, tenant_fair):
    return SqliteHuey(name='repro', filename=path, timeout=10,
                      tenant_fair=tenant_fair)


def scenario_default_off(tmp):
    print('\n[1] Default-off behavior is unchanged priority/FIFO')
    db = os.path.join(tmp, 'plain.db')
    huey = build_huey(db, tenant_fair=False)

    @huey.task()
    def task(n):
        return n

    for i in range(3):
        task(('alpha', i), tenant='alpha')
    task(('beta-hi',), tenant='beta', priority=10)

    order = [huey.dequeue().args for _ in range(4)]
    check('high-priority beta task dequeued first',
          order[0] == (('beta-hi',),), str(order))
    check('remaining tasks follow FIFO',
          order[1:] == [(('alpha', 0),), (('alpha', 1),), (('alpha', 2),)],
          str(order))
    check('no tenant_state table usage required',
          huey.storage.sql('select count(*) from tenant_state',
                           results=True)[0][0] == 0)
    huey.storage.close()


def scenario_continuous_enqueue(tmp):
    print('\n[2] Continuous enqueue: flooding tenant cannot starve others')
    db = os.path.join(tmp, 'fair.db')
    huey = build_huey(db, tenant_fair=True)

    @huey.task()
    def task(n):
        return n

    # Big tenant keeps a deep backlog of high-priority tasks.
    for i in range(12):
        task(('big', i), tenant='big', priority=9)
    task(('small', 0), tenant='small')
    task(('small', 1), tenant='small')

    tenants = []
    for _ in range(6):
        tenants.append(huey.dequeue().tenant)
    check('rotation alternates while both tenants are active',
          tenants == ['big', 'small', 'big', 'small', 'big', 'big'],
          str(tenants))
    # small drained after two turns; big now owns the remainder (8 tasks).
    rest = [huey.dequeue().tenant for _ in range(8)]
    check('remainder all belongs to the flooding tenant',
          set(rest) == {'big'}, str(rest))
    check('queue fully drained', huey.dequeue() is None)
    check('rotation table empty after drain',
          huey.storage.sql('select count(*) from tenant_state',
                           results=True)[0][0] == 0)
    huey.storage.close()


def scenario_priority_within_tenant(tmp):
    print('\n[3] Priority only orders tasks inside the same tenant')
    db = os.path.join(tmp, 'priority.db')
    huey = build_huey(db, tenant_fair=True)

    @huey.task()
    def task(n):
        return n

    task(('a-low',), tenant='alpha')
    task(('a-hi',), tenant='alpha', priority=10)
    task(('b-normal',), tenant='beta')

    first = huey.dequeue()
    check('alpha opens rotation and picks its high-priority task',
          first.tenant == 'alpha' and first.args == (('a-hi',),))
    second = huey.dequeue()
    check('beta gets its turn despite alpha priority',
          second.tenant == 'beta' and second.args == (('b-normal',),))
    third = huey.dequeue()
    check('alpha low-priority task served last',
          third.tenant == 'alpha' and third.args == (('a-low',),))
    huey.storage.close()


def scenario_concurrent_consumers(tmp):
    print('\n[4] Concurrent consumers share one persisted fair state')
    db = os.path.join(tmp, 'concurrent.db')
    huey = build_huey(db, tenant_fair=True)

    @huey.task()
    def task(n):
        return n

    per_tenant = 50
    for i in range(per_tenant):
        task(('alpha', i), tenant='alpha', priority=5)
        task(('beta', i), tenant='beta')
        task(('gamma', i), tenant='gamma')

    # Independent storage connections, as separate consumer processes would
    # have. They deliberately do not share any in-memory rotation object.
    connections = [
        SqliteHuey(name='repro', filename=db, timeout=10, tenant_fair=True)
        for _ in range(4)]
    results = Queue()

    def consume(storage):
        while True:
            data = storage.storage.dequeue()
            if data is None:
                return
            task_obj = huey.deserialize_task(data)
            results.put(task_obj.tenant)

    threads = [threading.Thread(target=consume, args=(conn,))
               for conn in connections]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    for conn in connections:
        conn.storage.close()

    sequence = [results.get() for _ in range(results.qsize())]
    check('every task consumed exactly once',
          len(sequence) == per_tenant * 3, str(len(sequence)))
    counts = {name: sequence.count(name) for name in ('alpha', 'beta', 'gamma')}
    check('each tenant received its full share',
          counts == {'alpha': per_tenant, 'beta': per_tenant,
                     'gamma': per_tenant}, str(counts))
    # While three tenants are active the same tenant can never repeat.
    repeats = [i for i in range(1, len(sequence))
               if sequence[i] == sequence[i - 1]]
    check('no back-to-back same-tenant claims while all are active',
          not repeats, 'repeat indices: %s' % repeats[:5])
    check('rotation table empty once drained',
          huey.storage.sql('select count(*) from tenant_state',
                           results=True)[0][0] == 0)
    huey.storage.close()


def scenario_restart(tmp):
    print('\n[5] Restart resumes from persisted rotation without bias')
    db = os.path.join(tmp, 'restart.db')
    huey = build_huey(db, tenant_fair=True)

    @huey.task()
    def task_a(n):
        return n

    @huey.task()
    def task_b(n):
        return n

    task_a(('a1',), tenant='alpha')
    task_b(('b1',), tenant='beta')
    assert huey.dequeue().tenant == 'alpha'
    assert huey.dequeue().tenant == 'beta'
    huey.storage.close()

    # Simulate a process restart: fresh object, same database file.
    reborn = SqliteHuey(name='repro', filename=db, timeout=10,
                        tenant_fair=True)

    @reborn.task(name='task_a')
    def task_a2(n):
        return n

    @reborn.task(name='task_b')
    def task_b2(n):
        return n

    task_a2(('a2',), tenant='alpha')
    task_b2(('b2',), tenant='beta')
    order = [reborn.dequeue().tenant, reborn.dequeue().tenant]
    check('restarted consumer still rotates fairly', order == ['alpha', 'beta'],
          str(order))
    reborn.storage.close()


def scenario_delayed(tmp):
    print('\n[6] Delayed tasks enter the rotation only when executable')
    db = os.path.join(tmp, 'delayed.db')
    huey = build_huey(db, tenant_fair=True)

    @huey.task()
    def task(n):
        return n

    now = huey._get_timestamp()
    task.schedule(('late',), delay=60, tenant='alpha')
    task(('now',), tenant='beta')

    early = huey.dequeue()
    huey.execute(early, timestamp=now)  # Parks the not-due task.
    check('not-due task moved to schedule table',
          huey.scheduled_count() == 1 and huey.pending_count() == 1)
    ready = huey.dequeue()
    check('executable beta task is served while alpha waits',
          ready.tenant == 'beta')
    check('nothing else executable', huey.dequeue() is None)

    due = now + datetime.timedelta(seconds=61)
    promoted = huey.read_schedule(timestamp=due)
    check('scheduled task keeps its tenant after promotion',
          len(promoted) == 1 and promoted[0].tenant == 'alpha')
    for t in promoted:
        huey.enqueue(t)
    check('promoted task now participates in the queue',
          huey.dequeue().tenant == 'alpha')
    huey.storage.close()


def scenario_lineage(tmp):
    print('\n[7] Retries, chains and chords keep explicit tenant lineage')
    db = os.path.join(tmp, 'lineage.db')
    huey = build_huey(db, tenant_fair=True)

    @huey.task(retries=1)
    def flaky(n):
        raise ValueError('boom once')

    @huey.task()
    def add(n):
        return n

    @huey.task()
    def callback(results):
        return results

    # Retry keeps the origin tenant.
    result = flaky(('a',), tenant='alpha')
    add(('b',), tenant='beta')
    first = huey.dequeue()
    check('first claim is alpha', first.tenant == 'alpha')
    try:
        huey.execute(first)
    except Exception:
        pass
    other = huey.dequeue()
    retry = huey.dequeue()
    check('retry retains origin tenant',
          other.tenant == 'beta' and retry.id == result.id
          and retry.tenant == 'alpha')
    try:
        huey.execute(retry)
    except Exception:
        pass

    # Pipeline children inherit the origin tenant; later plain tasks do not.
    pipeline = add.s(('root',), tenant='alpha').then(add, ('child',))
    huey.enqueue(pipeline)
    root = huey.dequeue()
    huey.execute(root)
    child = huey.dequeue()
    check('on_complete child inherits tenant',
          child.tenant == 'alpha' and child.args == (('child',), 'root'))
    add(('plain',))
    plain = huey.dequeue()
    check('no context leak into an unrelated publish', plain.tenant is None)

    # Chord members keep their own tenants.
    c = chord([add.s(('m1',), tenant='alpha'),
               add.s(('m2',), tenant='beta')],
              callback.s(('cb',)))
    huey.enqueue(c)
    member_tenants = sorted(huey.dequeue().tenant for _ in range(2))
    check('chord members preserve independent tenants',
          member_tenants == ['alpha', 'beta'], str(member_tenants))
    huey.storage.close()


def main():
    tmp = tempfile.mkdtemp(prefix='tenant-fair-repro-')
    print('Using temporary database directory: %s' % tmp)
    try:
        scenario_default_off(tmp)
        scenario_continuous_enqueue(tmp)
        scenario_priority_within_tenant(tmp)
        scenario_concurrent_consumers(tmp)
        scenario_restart(tmp)
        scenario_delayed(tmp)
        scenario_lineage(tmp)
    finally:
        # Keep the directory on failure for inspection.
        if not failures:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    print('\n' + '=' * 60)
    if failures:
        print('REPRO FAILED: %d invariant(s) violated' % len(failures))
        for label in failures:
            print('  - %s' % label)
        return 1
    print('ALL SCENARIOS PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
