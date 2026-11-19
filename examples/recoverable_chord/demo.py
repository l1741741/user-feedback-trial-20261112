#!/usr/bin/env python
"""
Terminal reproduction for the recoverable SQLite chord.

Scenarios
---------

seed        Enqueue a four-document chord (one member retries once) and exit,
            leaving the work entirely in the SQLite database.

consume     Run a real multi-process consumer until the queue drains, then
            print the callback output.

crash       Enqueue a chord and move it to the "ready" state WITHOUT
            enqueueing the callback -- the on-disk state left behind when the
            last worker dies at the join. The callback is missing but no
            evidence is lost.

recover     Open the database in a fresh process, run chord_sweep(), and
            start a consumer so the recovered callback is executed. Prove
            that repeated sweeps never publish a second copy.

race        Two independent worker processes drain the same chord at once;
            exactly one callback delivery lands in the queue.

status      Print the raw chord tables for inspection.

Usage (from the repository root)::

    python -m examples.recoverable_chord.demo seed
    python -m examples.recoverable_chord.demo crash
    python -m examples.recoverable_chord.demo recover
    python -m examples.recoverable_chord.demo race
    python -m examples.recoverable_chord.demo status
"""
import multiprocessing
import os
import sys
import time

from huey import chord
from huey.consumer import WORKER_PROCESS

# ``demo_tasks`` is importable both as a package module and as a standalone
# script next to this file.
try:
    from examples.recoverable_chord.demo_tasks import DEMO_DIR
    from examples.recoverable_chord.demo_tasks import DB_PATH
    from examples.recoverable_chord.demo_tasks import convert
    from examples.recoverable_chord.demo_tasks import flaky_convert
    from examples.recoverable_chord.demo_tasks import huey
    from examples.recoverable_chord.demo_tasks import submit_batch
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from demo_tasks import DEMO_DIR  # type: ignore
    from demo_tasks import DB_PATH  # type: ignore
    from demo_tasks import convert  # type: ignore
    from demo_tasks import flaky_convert  # type: ignore
    from demo_tasks import huey  # type: ignore
    from demo_tasks import submit_batch  # type: ignore


def _reset():
    # Close the connection first: deleting the file out from under an open
    # WAL connection would leave the next writes landing in a schema-less
    # file. Reinitialize afterwards so this process holds a fresh database.
    huey.storage.close()
    for suffix in ('', '-wal', '-shm'):
        path = DB_PATH + suffix
        if os.path.exists(path):
            os.unlink(path)
    for name in os.listdir(DEMO_DIR):
        if name.startswith(('attempt-', 'submitted')):
            os.unlink(os.path.join(DEMO_DIR, name))
    huey.storage = huey.create_storage()


def _print_chord_rows(label):
    print('\n== %s ==' % label)
    rows = huey.storage.sql(
        'select cid, size, state, updated from chord order by cid',
        results=True)
    for cid, size, state, updated in rows:
        print('chord %s size=%s state=%s' % (cid[:8], size, state))
        members = huey.storage.chord_members(cid)
        results = huey.storage.sql(
            'select idx, length(value) from chord_result where cid=? '
            'order by idx', (cid,), results=True)
        print('  members: %s' % members)
        print('  reported slots: %s' % results)


def cmd_seed():
    _reset()
    result = huey.enqueue(chord([
        convert.s('doc1'),
        flaky_convert.s('doc2'),
        convert.s('doc3'),
        convert.s('doc4'),
    ], submit_batch.s()))
    print('Enqueued chord %s with 4 members; callback task %s' % (
        result.callback.id[:8], result.callback.id))
    _print_chord_rows('Durable state after enqueue (process exits now)')
    print('Queue depth: %s. No worker was started in this process.'
          % huey.pending_count())
    huey.storage.close()


def _worker_loop(max_tasks):
    # Re-import inside the child so a spawn-style start would work too.
    try:
        from examples.recoverable_chord.demo_tasks import huey as h
    except ImportError:
        from demo_tasks import huey as h  # type: ignore
    executed = 0
    idle = None
    while executed < max_tasks:
        task = h.dequeue()
        if task is None:
            if idle is None:
                idle = time.time()
            elif time.time() - idle > 15:
                break
            time.sleep(0.02)
            continue
        idle = None
        h.execute(task)
        executed += 1
    h.storage.close()


def cmd_consume():
    ctx = multiprocessing.get_context('fork')
    procs = [ctx.Process(target=_worker_loop, args=(6,)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    output = os.path.join(DEMO_DIR, 'submitted.txt')
    print('\nCallback output file:')
    with open(output) as fh:
        for line in fh:
            print('  ' + line.strip())
    _print_chord_rows('State after consumption')
    print('Queue depth: %s' % huey.pending_count())
    huey.storage.close()


def cmd_crash():
    _reset()
    result = huey.enqueue(chord([
        convert.s('crash1'),
        convert.s('crash2'),
        convert.s('crash3'),
    ], submit_batch.s()))
    cid = huey.storage.chord_open_cids()[0]
    ser = huey.serializer
    for idx, doc in enumerate(('crash1', 'crash2', 'crash3')):
        huey.storage.chord_report(
            cid, idx, ser.serialize('converted:%s' % doc))
    print('All three members reported; chord state is now: %s'
          % huey.storage.chord_state(cid))
    # Simulate SIGKILL at this exact instant: discard the member messages
    # and never run the delivery transaction.
    huey.storage.flush_queue()
    _print_chord_rows('Simulated crash: ready chord, callback NOT enqueued')
    print('Queue depth: %s -- the callback would be lost with the legacy '
          'counter implementation.' % huey.pending_count())
    huey.storage.close()


def cmd_recover():
    before = huey.pending_count()
    print('Queue depth on startup: %s' % before)
    first = huey.chord_sweep(grace=0, retention=3600)
    second = huey.chord_sweep(grace=0, retention=3600)
    third = huey.chord_sweep(grace=0, retention=3600)
    print('sweep #1: %s' % first)
    print('sweep #2: %s (no duplicate delivery)' % second)
    print('sweep #3: %s (still no duplicate)' % third)
    print('Queue depth after sweeps: %s' % huey.pending_count())

    ctx = multiprocessing.get_context('fork')
    procs = [ctx.Process(target=_worker_loop, args=(2,)) for _ in range(1)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    output = os.path.join(DEMO_DIR, 'submitted.txt')
    print('\nRecovered callback output:')
    with open(output) as fh:
        for line in fh:
            print('  ' + line.strip())
    _print_chord_rows('State after recovery and execution')
    huey.storage.close()


def cmd_race():
    _reset()
    huey.enqueue(chord([
        convert.s('r1'), convert.s('r2'),
        convert.s('r3'), convert.s('r4'),
    ], submit_batch.s()))
    # Five tasks total: four members + one callback.
    ctx = multiprocessing.get_context('fork')
    procs = [ctx.Process(target=_worker_loop, args=(5,)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    output = os.path.join(DEMO_DIR, 'submitted.txt')
    lines = open(output).read().splitlines()
    print('\nCallback invocations: %s (must be exactly 1)' % len(lines))
    for line in lines:
        print('  ' + line)
    assert len(lines) == 1, 'duplicate callback delivery!'
    huey.storage.close()


def cmd_status():
    _print_chord_rows('Raw durable chord tables')
    print('queue size: %s, schedule size: %s' % (
        huey.pending_count(), huey.scheduled_count()))
    huey.storage.close()


COMMANDS = {
    'seed': cmd_seed,
    'consume': cmd_consume,
    'crash': cmd_crash,
    'recover': cmd_recover,
    'race': cmd_race,
    'status': cmd_status,
}


def main(argv):
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print(__doc__)
        return 1
    COMMANDS[argv[1]]()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
