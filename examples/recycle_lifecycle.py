#!/usr/bin/env python
"""
Terminal reproduction for the max_tasks worker-recycle lifecycle.

Run from the repository root (no services needed, SQLite backend):

    python examples/recycle_lifecycle.py thread
    python examples/recycle_lifecycle.py process

Each run enqueues 7 tasks with max_tasks=3, so the worker is recycled
twice and a third generation is started. The script prints, for every
generation: the startup/shutdown hook calls (with the pid/thread that ran
them), the per-generation task counters, and the recorded lifecycle events
distinguishing "recycled" from "stopped". It exits non-zero if any task
result is wrong, if a database handle was reused across processes, or if
worker processes/threads are left behind after shutdown.
"""
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from huey import SqliteHuey
from huey.consumer import ConsumerStopped


def main(worker_type):
    tmp_dir = tempfile.mkdtemp(prefix='huey-recycle-demo-')
    db = os.path.join(tmp_dir, 'demo.db')
    hook_log = os.path.join(tmp_dir, 'hooks.log')
    huey = SqliteHuey('recycle-demo', filename=db)

    def record_hook(kind):
        # Hooks run in the worker, which may be a forked process, so they
        # report through the filesystem.
        with open(hook_log, 'a') as fh:
            fh.write('%s %d %d\n' % (kind, os.getpid(),
                                     threading.get_ident()))

    @huey.on_startup()
    def startup():
        record_hook('startup')

    @huey.on_shutdown()
    def shutdown():
        record_hook('shutdown')

    @huey.task()
    def work(n):
        return (n * 2, os.getpid(), huey.storage._conn_pid)

    results = [work(i) for i in range(7)]
    consumer = huey.create_consumer(
        workers=1, periodic=False, worker_type=worker_type,
        initial_delay=0.01, max_delay=0.01, max_tasks=3,
        check_worker_health=True, health_check_interval=0.1,
        shutdown_timeout=10)
    consumer.start()

    def run_loop():
        health_check_ts = time.monotonic()
        while True:
            try:
                health_check_ts = consumer.loop(health_check_ts)
            except ConsumerStopped:
                break
        huey.notify_interrupted_tasks()

    loop_t = threading.Thread(target=run_loop)
    loop_t.start()
    failures = []
    try:
        out = [r.get(blocking=True, timeout=30) for r in results]
        deadline = time.monotonic() + 10
        while (len([e for e in consumer.lifecycle_events
                    if e.event == 'start']) < 3
               and time.monotonic() < deadline):
            time.sleep(0.05)
    finally:
        consumer.stop(graceful=True)
        loop_t.join(10)

    print('task results (n, value, worker_pid, conn_pid):')
    for (n, value), (val, pid, conn_pid) in zip(enumerate(results), out):
        ok = val == n * 2
        ok = ok and conn_pid == pid  # No cross-process handle reuse.
        if not ok:
            failures.append('task %d: value=%s pid=%s conn_pid=%s'
                            % (n, val, pid, conn_pid))
        print('  n=%d value=%s worker_pid=%d conn_pid=%d %s'
              % (n, val, pid, conn_pid, '' if ok else 'MISMATCH'))

    print('\nhooks (hook, pid, thread):')
    hooks = []
    if os.path.exists(hook_log):
        with open(hook_log) as fh:
            hooks = [line.split() for line in fh.read().splitlines()]
    for kind, pid, tid in hooks:
        print('  %s pid=%s tid=%s' % (kind, pid, tid))
    n_startup = sum(1 for h in hooks if h[0] == 'startup')
    n_shutdown = sum(1 for h in hooks if h[0] == 'shutdown')
    if n_startup != 3 or n_shutdown != 3:
        failures.append('expected 3 startup + 3 shutdown hook calls, got '
                        '%d/%d' % (n_startup, n_shutdown))

    print('\nlifecycle events:')
    for e in consumer.lifecycle_events:
        print('  %-5s %s gen=%d reason=%s tasks=%s completed=%s requeued=%s'
              % (e.event, e.name, e.generation, e.reason, e.task_count,
                 e.tasks_completed, e.tasks_requeued))
    events = consumer.lifecycle_events
    starts = [e for e in events if e.event == 'start']
    exits = [e for e in events if e.event == 'exit']
    recycled = [e for e in exits if e.reason == 'recycled']
    if len(starts) != 3 or len(exits) != 3 or len(recycled) != 2:
        failures.append('expected 3 starts / 3 exits / 2 recycled, got '
                        '%d/%d/%d' % (len(starts), len(exits), len(recycled)))

    leftovers = [t for t in threading.enumerate()
                 if t.name.startswith(('Worker-', 'Scheduler'))]
    if leftovers:
        failures.append('leftover threads: %s' % leftovers)
    for _, proc in consumer.worker_threads:
        if consumer.environment.is_alive(proc):
            failures.append('leftover worker process: %s' % proc)
    if consumer.environment.is_alive(consumer.scheduler):
        failures.append('scheduler still alive')

    shutil.rmtree(tmp_dir, ignore_errors=True)
    if loop_t.is_alive():
        failures.append('consumer main loop did not exit')
    if failures:
        print('\nFAILURES:')
        for failure in failures:
            print('  - %s' % failure)
        return 1
    print('\nOK: 7 tasks, 3 generations, 2 recycled, hooks balanced, '
          'no leftovers.')
    return 0


if __name__ == '__main__':
    worker_type = sys.argv[1] if len(sys.argv) > 1 else 'thread'
    if worker_type not in ('thread', 'process'):
        print('usage: python examples/recycle_lifecycle.py [thread|process]')
        sys.exit(2)
    sys.exit(main(worker_type))
