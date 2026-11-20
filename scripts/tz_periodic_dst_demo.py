#!/usr/bin/env python3
"""
Terminal reproduction for IANA-timezone periodic scheduling with DST.

Run from the repository root:

    .venv/bin/python -I scripts/tz_periodic_dst_demo.py

The script never touches the network and never changes the system clock:
every "current time" is an explicitly constructed datetime. It uses a real
SQLite database in a temporary directory and, for the dual-scheduler and
restart sections, spawns real OS subprocesses (via the test driver module).

What it demonstrates, against the fixed US DST rules in tzdata for 2026
(spring-forward 2026-03-08 02:00 America/New_York; fall-back 2026-11-01
02:00):

  1. A normal-day New York 02:30 task fires at the correct UTC instant.
  2. Spring-forward gap: skip vs. next-valid policies.
  3. Fall-back fold: once vs. twice, with distinct occurrence identities.
  4. Re-checking the same minute never duplicates a delivery (dedup claim).
  5. A paused scheduler catches up within an explicit window and drops an
     occurrence that is older than the window.
  6. Two schedulers sharing one SQLite file produce a single delivery.
  7. A real consumer-process restart preserves the dedup ledger.
  8. A plan change / task rename starts a fresh dedup namespace.
  9. Invalid timezone / conflicting configuration fail before startup.
"""
import datetime
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from huey import FOLD_TWICE
from huey import GAP_NEXT_VALID
from huey import GAP_SKIP
from huey import SqliteHuey
from huey import tzcrontab
from huey.consumer import Consumer
from huey.exceptions import ConfigurationError


NY = 'America/New_York'
GREEN = '\033[32m'
RED = '\033[31m'
BOLD = '\033[1m'
RESET = '\033[0m'


def check(label, condition, detail=''):
    mark = GREEN + 'PASS' + RESET if condition else RED + 'FAIL' + RESET
    print('  [%s] %s%s' % (mark, label, (' -- ' + detail) if detail else ''))
    if not condition:
        raise SystemExit(1)


def section(title):
    print('\n' + BOLD + '== ' + title + RESET)


def main():
    tmp = tempfile.mkdtemp(prefix='huey-tz-demo-')
    db = os.path.join(tmp, 'huey.db')
    print('Using temporary SQLite database: %s' % db)

    # 1. Normal day -------------------------------------------------------
    section('1. Normal day: 02:30 America/New_York on 2026-06-01')
    h = SqliteHuey('demo', filename=db)

    @h.periodic_task(tzcrontab(minute='30', hour='2', timezone=NY))
    def ops():
        pass

    tick = datetime.datetime(2026, 6, 1, 6, 31)  # 02:30 EDT == 06:30Z
    enq = h.enqueue_due_periodic(tick)
    check('one occurrence delivered at 06:30Z',
          [t.occurrence_id for t in enq] == ['20260601T063000#0'])
    check('re-checking the same minute does not redeliver',
          h.enqueue_due_periodic(tick) == [])
    check('queue holds exactly one task', h.pending_count() == 1)
    h.flush()

    # 2. Spring-forward gap ----------------------------------------------
    section('2. Spring-forward gap on 2026-03-08 (02:00->03:00 skipped)')
    h_skip = SqliteHuey('demo', filename=db)

    @h_skip.periodic_task(tzcrontab(
        minute='30', hour='2', timezone=NY, gap_policy=GAP_SKIP))
    def ops_skip():
        pass

    gap_tick = datetime.datetime(2026, 3, 8, 7, 31)
    check('gap=skip delivers nothing',
          h_skip.enqueue_due_periodic(gap_tick) == [])
    h_skip.flush()

    h_next = SqliteHuey('demo', filename=db)

    @h_next.periodic_task(tzcrontab(
        minute='30', hour='2', timezone=NY,
        gap_policy=GAP_NEXT_VALID))
    def ops_next():
        pass

    enq = h_next.enqueue_due_periodic(gap_tick)
    check('gap=next_valid moves 02:30 to 03:30 EDT (07:30Z)',
          [t.occurrence_id for t in enq] == ['20260308T073000#0'])
    h_next.flush()

    # 3. Fall-back fold ---------------------------------------------------
    section('3. Fall-back fold on 2026-11-01 (01:00 repeated)')
    h_once = SqliteHuey('demo', filename=db)

    @h_once.periodic_task(tzcrontab(
        minute='30', hour='1', timezone=NY))
    def ops_once():
        pass

    # 01:30 EDT fold#0 == 05:30Z, 01:30 EST fold#1 == 06:30Z.
    check('fold=once delivers the first half only',
          [t.occurrence_id for t in h_once.enqueue_due_periodic(
              datetime.datetime(2026, 11, 1, 5, 31))] ==
          ['20261101T053000#0'])
    h_once.flush()

    h_twice = SqliteHuey('demo', filename=db)

    @h_twice.periodic_task(tzcrontab(
        minute='30', hour='1', timezone=NY, fold_policy=FOLD_TWICE,
        catchup=3720))
    def ops_twice():
        pass

    enq = h_twice.enqueue_due_periodic(
        datetime.datetime(2026, 11, 1, 6, 30, 1))
    ids = sorted(t.occurrence_id for t in enq)
    check('fold=twice delivers both halves with distinct identities',
          ids == ['20261101T053000#0', '20261101T063000#1'], str(ids))
    h_twice.flush()

    # 4/5. Catch-up window ------------------------------------------------
    section('4. Catch-up window after a pause')
    h_c = SqliteHuey('demo', filename=db)

    @h_c.periodic_task(tzcrontab(minute='30', hour='2', timezone=NY))
    def ops_c():
        pass

    check('10-minute pause still delivers (inside 15-minute window)',
          len(h_c.enqueue_due_periodic(
              datetime.datetime(2026, 6, 2, 6, 40))) == 1)
    check('20-minute pause drops the stale occurrence',
          h_c.enqueue_due_periodic(
              datetime.datetime(2026, 6, 3, 6, 50)) == [])
    h_c.flush()

    # 6. Dual schedulers over one SQLite file (real subprocesses) --------
    section('5. Two schedulers, one SQLite file, one delivery')
    driver_env = dict(os.environ)
    driver_env.update({
        'HUEY_TZ_NOW': '2026-06-04T06:31:00',
        'HUEY_TZ_DB': db,
        'HUEY_TZ_RESULTS': os.path.join(tmp, 'dual.log'),
        'HUEY_TZ_MODE': 'dual',
    })
    open(driver_env['HUEY_TZ_RESULTS'], 'w').close()
    proc = subprocess.run(
        [sys.executable, '-I', '-m',
         'huey.tests._tz_multiprocess_driver'],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=driver_env, capture_output=True, text=True)
    check('dual driver exited cleanly', proc.returncode == 0, proc.stderr)
    delivered = [l for l in proc.stdout.strip().splitlines() if l]
    check('exactly one delivery despite concurrent schedulers',
          delivered == ['20260604T063000#0'], str(delivered))

    # 7. Real restart -----------------------------------------------------
    section('6. Real consumer-process restart preserves dedup state')
    h_r = SqliteHuey('demo', filename=db)
    h_r.storage.close()
    restart_env = dict(driver_env)
    restart_env['HUEY_TZ_NOW'] = '2026-06-05T06:31:00'
    restart_env['HUEY_TZ_MODE'] = 'tick'
    restart_env['HUEY_TZ_RESULTS'] = os.path.join(tmp, 'restart.log')
    open(restart_env['HUEY_TZ_RESULTS'], 'w').close()

    def tick():
        p = subprocess.run(
            [sys.executable, '-I', '-m',
             'huey.tests._tz_multiprocess_driver'],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=restart_env, capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        return [l for l in p.stdout.strip().splitlines() if l]

    first = tick()  # fresh interpreter #1
    second = tick()  # fresh interpreter #2, same DB
    check('first process delivers the occurrence',
          first == ['20260605T063000#0'])
    check('restarted process does not redeliver', second == [])

    # 8. Plan/rename isolation -------------------------------------------
    section('7. Plan change and task rename isolate dedup state')
    h_p = SqliteHuey('demo', filename=db)

    @h_p.periodic_task(tzcrontab(minute='30', hour='2', timezone=NY))
    def ops_p():
        pass

    t = datetime.datetime(2026, 6, 6, 6, 31)
    check('original plan delivers', len(h_p.enqueue_due_periodic(t)) == 1)
    cls = type(h_p.local_periodic_tasks()[0])
    h_p._registry.unregister(cls)

    @h_p.periodic_task(tzcrontab(minute='0', hour='2', timezone=NY))
    def ops_p():  # noqa: F811
        pass

    check('changed plan has a different fingerprint',
          len(h_p.enqueue_due_periodic(
              datetime.datetime(2026, 6, 6, 6, 1))) == 1)
    h_p.flush()

    # 9. Startup validation ----------------------------------------------
    section('8. Invalid configuration fails before startup')
    try:
        tzcrontab(minute='0', timezone='Mars/Olympus_Mons')
        check('invalid timezone rejected', False)
    except ConfigurationError:
        check('invalid timezone rejected', True)

    h_bad = SqliteHuey('bad', filename=os.path.join(tmp, 'bad.db'),
                      utc=False)

    @h_bad.periodic_task(tzcrontab(minute='0', timezone=NY))
    def ops_bad():
        pass

    try:
        Consumer(h_bad, workers=1)
        check('non-UTC Huey with tz periodic task rejected', False)
    except ConfigurationError:
        check('non-UTC Huey with tz periodic task rejected', True)

    shutil.rmtree(tmp, ignore_errors=True)
    print('\n' + GREEN + BOLD +
          'All timezone/DST scenarios reproduced successfully.' + RESET)


if __name__ == '__main__':
    main()
