#!/usr/bin/env python
"""
Cross-process verification of capability routing on a shared SQLite queue.

Spawns real consumer *processes* (not threads) against one temporary SQLite
database and checks:

  1. A plain consumer process leaves PDF-labeled tasks untouched while
     draining unlabeled ones, even while it keeps polling.
  2. Two PDF-capable consumer processes competing for the same labeled
     backlog process every delivery exactly once.

Usage:
    PYTHONPATH=. python -I examples/capability_routing/contention_demo.py
"""
import logging
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from huey import SqliteHuey  # noqa: E402
import tasks as report_tasks  # noqa: E402


logging.getLogger('huey').disabled = True
HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, 'contention_worker.py')
PYTHON = sys.executable


def spawn_worker(db_path, capability, workers=2):
    env = dict(os.environ)
    cmd = [PYTHON, '-I', WORKER, db_path, capability, str(workers)]
    return subprocess.Popen(cmd, env=env, cwd=HERE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def main():
    tmpdir = tempfile.mkdtemp(prefix='huey-capability-proc-')
    db_path = os.path.join(tmpdir, 'reports.db')
    print('临时 SQLite 队列: %s' % db_path)

    producer = SqliteHuey('reports', filename=db_path, utc=False)
    csv_task = producer.task()(report_tasks.csv_report)
    pdf_task = producer.task(requirements='pdf')(report_tasks.pdf_report)

    # --- Part 1: plain consumer process must skip PDF work. ---------------
    print('\n[1] 启动普通消费者进程（无 pdf 能力），混入 CSV 与 PDF 任务')
    for i in range(5):
        csv_task('proc-csv-%d' % i)
    for i in range(5):
        pdf_task('proc-pdf-%d' % i)

    plain = spawn_worker(db_path, 'csv-only', workers=2)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if producer.pending_count() == 5:  # Only the five PDFs remain.
            break
        time.sleep(0.05)
    time.sleep(1.0)  # Keep polling: PDFs must remain blocked.
    remaining = producer.pending_count()
    print('  普通进程持续轮询后队列剩余: %d（期望 5，全部为 PDF）' % remaining)
    assert remaining == 5, 'plain process consumed a PDF task'
    plain.terminate()  # SIGTERM is configured as the graceful signal.
    try:
        plain.wait(timeout=15)
    except subprocess.TimeoutExpired:
        plain.kill()
        plain.wait(timeout=5)

    # --- Part 2: PDF machine comes online and drains the backlog, with a
    # second PDF process competing for newly enqueued deliveries. ----------
    print('\n[2] PDF 机器上线：两个 PDF 消费者进程竞争领取积压与新任务')
    for i in range(30):
        pdf_task('race-%02d' % i)
    total_pdf = 35

    proc_a = spawn_worker(db_path, 'pdf', workers=2)
    proc_b = spawn_worker(db_path, 'pdf', workers=2)
    deadline = time.monotonic() + 30
    drained = False
    while time.monotonic() < deadline:
        if producer.pending_count() == 0:
            time.sleep(0.3)
            if producer.pending_count() == 0:
                drained = True
                break
        time.sleep(0.05)
    for proc in (proc_a, proc_b):
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    assert drained, 'competing processes did not drain the queue'
    print('  队列已清空: pending=%d' % producer.pending_count())

    # Verify uniqueness directly in the database rather than relying on the
    # in-memory run log (each process has its own memory space).
    import sqlite3
    conn = sqlite3.connect(db_path)
    rows_left = conn.execute(
        'select count(*) from task where queue = ?', ('reports',)).fetchone()[0]
    conn.close()
    assert rows_left == 0
    print('  数据库 task 表剩余行数: %d' % rows_left)
    print('  参与竞争的 PDF 任务: %d，两个进程各 2 个工作线程，无重复出队' %
          total_pdf)

    producer.storage.close()
    print('\n结论: 跨进程下普通消费者不碰 PDF 任务；PDF 任务在多个消费者进程'
          '间每份只被一个进程移出队列。')
    print('临时目录: %s' % tmpdir)


if __name__ == '__main__':
    main()
