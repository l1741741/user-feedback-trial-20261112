#!/usr/bin/env python
"""
Terminal reproduction for the persistent, multi-process rate limiter.

Run from the repository root with the project virtualenv:

    python examples/rate_limit_persistent/repro.py

The script uses only the standard library plus Huey itself. It creates a
SqliteHuey database in a temporary directory and demonstrates, in real
operating-system processes:

  1. Fixed-window quota: exactly `limit` acquisitions succeed per window.
  2. Multi-process contention at one instant: N processes hammer one limiter
     concurrently, yet the window never grants more than `limit` slots.
  3. Window roll-over: a fresh window restores the quota.
  4. Late old-window requests (clock roll-back / stragglers) are rejected and
     can never reset a newer window's count.
  5. Rejected requests do not grow the stored row without bound.
  6. Different queues sharing one database are isolated.

Pass --keep to leave the SQLite database on disk for manual inspection.
"""
import argparse
import multiprocessing
import os
import sqlite3
import tempfile
import time


def _worker(db_path, queue, name, limit, per, attempts, ready, start):
    # Imported inside the worker so the "spawn" start method stays self-
    # contained on every platform.
    from huey import SqliteHuey
    huey = SqliteHuey(queue, filename=db_path, utc=False)
    rl = huey.rate_limit(name, limit=limit, per=per, persistent=True)
    ready.set()
    start.wait()
    granted = 0
    for _ in range(attempts):
        try:
            rl.acquire()
            granted += 1
        except Exception:
            pass
    huey.storage.close()
    print('  worker pid=%d granted=%d' % (os.getpid(), granted))


def _spawn(db_path, queue, name, limit, per, attempts, nproc):
    ctx = multiprocessing.get_context('spawn')
    start = ctx.Event()
    readies = [ctx.Event() for _ in range(nproc)]
    procs = [ctx.Process(target=_worker,
                         args=(db_path, queue, name, limit, per, attempts,
                               readies[i], start))
             for i in range(nproc)]
    for p in procs:
        p.start()
    for event in readies:
        event.wait(15)
    start.set()  # Release every process at the same instant.
    for p in procs:
        p.join(30)
        if p.exitcode != 0:
            raise RuntimeError('worker failed: %s' % p.exitcode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keep', action='store_true',
                        help='keep the SQLite database instead of deleting it')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--per', type=int, default=60)
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix='huey-rl-repro-')
    db_path = os.path.join(workdir, 'huey.db')

    # Create the schema before spawning workers.
    from huey import SqliteHuey
    SqliteHuey('demo', filename=db_path, utc=False).storage.close()

    print('SQLite database: %s' % db_path)
    print('limit=%d per=%ds workers=%d' % (args.limit, args.per,
                                           args.workers))

    print('\n[1] Multi-process contention in a single window')
    _spawn(db_path, 'demo', 'third-party-api', args.limit, args.per,
           attempts=20, nproc=args.workers)
    conn = sqlite3.connect(db_path)
    window, count = conn.execute(
        'select window, count from rate_limit where key = ?',
        ('third-party-api',)).fetchone()
    print('  stored row: window=%s count=%d (limit=%d)' % (
        window, count, args.limit))
    assert count == args.limit, 'window over-granted: %d > %d' % (
        count, args.limit)
    assert conn.execute(
        'select count(*) from rate_limit where key = ?',
        ('third-party-api',)).fetchone()[0] == 1, 'row grew without bound'

    print('\n[2] Window roll-over restores the quota')
    next_window = window + args.per
    huey = SqliteHuey('demo', filename=db_path, utc=False)
    result = huey.storage.rate_limit_add(
        'third-party-api', next_window, args.limit, args.per,
        now=next_window + 1)
    print('  first grant in new window allowed=%s count=%d' % (
        result.allowed, result.count))
    assert result.allowed and result.count == 1

    print('\n[3] A late old-window request cannot reset the new window')
    stale = huey.storage.rate_limit_add(
        'third-party-api', window, args.limit, args.per,
        now=next_window + 2)
    row = huey.storage.rate_limit_peek('third-party-api')
    print('  stale request allowed=%s retry_after=%.1fs; row window=%s '
          'count=%d' % (stale.allowed, stale.retry_after, row['window'],
                        row['count']))
    assert not stale.allowed
    assert row['window'] == next_window and row['count'] == 1

    print('\n[4] Repeated rejections leave the count unchanged')
    # Fill the new window up to the limit first (one slot already used).
    for _ in range(args.limit - 1):
        huey.storage.rate_limit_add('third-party-api', next_window,
                                    args.limit, args.per, now=next_window + 3)
    row = huey.storage.rate_limit_peek('third-party-api')
    assert row['count'] == args.limit
    # Every further request is rejected and cannot grow the row.
    for _ in range(50):
        huey.storage.rate_limit_add('third-party-api', next_window,
                                    args.limit, args.per, now=next_window + 4)
    row = huey.storage.rate_limit_peek('third-party-api')
    print('  after filling the window and 50 more denials: count=%d '
          '(bounded at limit)' % row['count'])
    assert row['count'] == args.limit

    print('\n[5] Queue isolation in a shared database')
    other = SqliteHuey('other-queue', filename=db_path, utc=False)
    isolated = other.storage.rate_limit_add(
        'third-party-api', next_window, args.limit, args.per,
        now=next_window + 1)
    cur = sqlite3.connect(db_path)
    queues = [q for q, in cur.execute(
        'select distinct queue from rate_limit where key = ?',
        ('third-party-api',)).fetchall()]
    cur.close()
    print('  same limiter name in another queue allowed=%s; queues=%s' % (
        isolated.allowed, sorted(queues)))
    assert isolated.allowed and set(queues) == {'demo', 'other-queue'}

    huey.storage.close()
    other.storage.close()
    conn.close()

    if args.keep:
        print('\nDatabase kept at: %s' % db_path)
    else:
        for suffix in ('', '-wal', '-shm'):
            path = db_path + suffix
            if os.path.exists(path):
                os.unlink(path)
        os.rmdir(workdir)
        print('\nCleaned up temporary database.')

    print('\nALL CHECKS PASSED')


if __name__ == '__main__':
    main()
