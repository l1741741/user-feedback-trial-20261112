"""
Cross-process contention tests for the SQLite persistent rate limiter.

The in-process tests above use injected clocks and a single connection; they
cannot prove that two *operating-system processes* crossing a window boundary
at the same instant agree on one quota. These tests spawn real subprocesses
that open the same SQLite database file independently and hammer one limiter
concurrently, then assert the total number of grants never exceeds the limit
for any window.
"""
from huey.tests import TEST_TMP_DIR

import multiprocessing
import os
import sqlite3
import time
import unittest


# Module-level workers so the "spawn" start method can pickle them.
def _worker_grant(db_path, queue_name, limiter, limit, per, count, ready,
                  start):
    from huey import SqliteHuey
    huey = SqliteHuey(queue_name, filename=db_path, utc=False)
    rl = huey.rate_limit(limiter, limit=limit, per=per, persistent=True)
    ready.set()
    start.wait()
    for _ in range(count):
        try:
            rl.acquire()
        except Exception:
            pass
    huey.storage.close()


def _worker_boundary(db_path, queue_name, limiter, limit, per, base_window,
                     ready, start, phase2):
    # Phase 1: every process competes inside the *same* current window.
    # Phase 2 (after a barrier in the parent): one process opens the next
    # window, then every process fires a late old-window request that must be
    # rejected and must not reset the newer window.
    from huey import SqliteHuey
    huey = SqliteHuey(queue_name, filename=db_path, utc=False)
    store = huey.storage
    ready.set()
    start.wait()

    # Phase 1: two same-window attempts per process.
    store.rate_limit_add(limiter, base_window, limit, per, base_window + 1)
    store.rate_limit_add(limiter, base_window, limit, per, base_window + 2)

    # Wait until the parent has let one process establish the next window.
    phase2.wait()

    # Phase 2: a late, older-window request that must never reset the count.
    store.rate_limit_add(limiter, base_window, limit, per, base_window + 3)
    huey.storage.close()


def _worker_open_next_window(db_path, queue_name, limiter, limit, per,
                             base_window, ready, start, phase2):
    from huey import SqliteHuey
    huey = SqliteHuey(queue_name, filename=db_path, utc=False)
    store = huey.storage
    ready.set()
    start.wait()
    store.rate_limit_add(limiter, base_window, limit, per, base_window + 1)
    # Establish the next window, then release every other process.
    store.rate_limit_add(limiter, base_window + per, limit, per,
                         base_window + per + 1)
    phase2.set()
    huey.storage.close()


@unittest.skipIf(sqlite3 is None, 'requires sqlite3')
class TestSqliteMultiprocessRateLimit(unittest.TestCase):
    def setUp(self):
        self.db_file = os.path.join(
            TEST_TMP_DIR, 'huey-rl-mp-%s.db' % os.getpid())
        # Initialize schema before spawning workers.
        from huey import SqliteHuey
        huey = SqliteHuey('mp', filename=self.db_file, utc=False)
        huey.storage.close()

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            path = self.db_file + suffix
            if os.path.exists(path):
                os.unlink(path)

    def _launch(self, target, args, nproc):
        ctx = multiprocessing.get_context('spawn')
        start = ctx.Event()
        readies = [ctx.Event() for _ in range(nproc)]
        procs = [ctx.Process(target=target,
                             args=args + (readies[i], start))
                 for i in range(nproc)]
        for p in procs:
            p.start()
        # Wait until every worker has opened its connection and is parked.
        for event in readies:
            self.assertTrue(event.wait(15))
        start.set()
        for p in procs:
            p.join(30)
            self.assertEqual(p.exitcode, 0)

    def test_concurrent_grants_never_exceed_limit_per_window(self):
        nproc, per_proc, limit, per = 6, 25, 5, 30
        self._launch(
            _worker_grant,
            (self.db_file, 'mp', 'shared-api', limit, per, per_proc),
            nproc)

        # All processes share one wall clock and one database. Whatever window
        # they landed in, at most `limit` grants may exist for it.
        conn = sqlite3.connect(self.db_file)
        try:
            rows = conn.execute(
                'select window, count from rate_limit where key = ?',
                ('shared-api',)).fetchall()
        finally:
            conn.close()
        self.assertTrue(rows)
        for window, count in rows:
            self.assertLessEqual(
                count, limit,
                'window %s received %d grants (> limit %d)' % (
                    window, count, limit))
        # And the row count stays bounded: one row per limiter.
        self.assertEqual(len(rows), 1)

    def test_concurrent_boundary_crossing_keeps_one_count_per_window(self):
        nproc, limit, per = 5, 2, 60
        base_window = (int(time.time()) // per) * per
        ctx = multiprocessing.get_context('spawn')
        start = ctx.Event()
        phase2 = ctx.Event()
        readies = [ctx.Event() for _ in range(nproc + 1)]

        # One opener establishes the next window; nproc others then fire late
        # old-window requests concurrently.
        opener = ctx.Process(
            target=_worker_open_next_window,
            args=(self.db_file, 'mp', 'boundary-api', limit, per,
                  base_window, readies[0], start, phase2))
        others = [ctx.Process(
            target=_worker_boundary,
            args=(self.db_file, 'mp', 'boundary-api', limit, per,
                  base_window, readies[i + 1], start, phase2))
            for i in range(nproc)]
        procs = [opener] + others
        for p in procs:
            p.start()
        for event in readies:
            self.assertTrue(event.wait(15))
        start.set()
        for p in procs:
            p.join(30)
            self.assertEqual(p.exitcode, 0)

        conn = sqlite3.connect(self.db_file)
        try:
            rows = dict(conn.execute(
                'select window, count from rate_limit where key = ?',
                ('boundary-api',)).fetchall())
        finally:
            conn.close()

        # The limiter keeps one row that always points at the newest window.
        # After the opener established the next window, every late old-window
        # request was rejected and could not roll that row backwards.
        self.assertEqual(len(rows), 1)
        window, count = next(iter(rows.items()))
        self.assertEqual(window, float(base_window + per))
        self.assertEqual(count, 1)  # Only the opener's single new-window slot.

    def test_distinct_queues_sharing_database_are_isolated(self):
        nproc, per_proc, limit, per = 4, 15, 2, 30
        for queue in ('queue-a', 'queue-b'):
            self._launch(
                _worker_grant,
                (self.db_file, queue, 'same-name', limit, per, per_proc),
                nproc)

        conn = sqlite3.connect(self.db_file)
        try:
            rows = conn.execute(
                'select queue, count from rate_limit where key = ?',
                ('same-name',)).fetchall()
        finally:
            conn.close()
        queues = {q: c for q, c in rows}
        self.assertEqual(set(queues), {'queue-a', 'queue-b'})
        for count in queues.values():
            self.assertLessEqual(count, limit)
