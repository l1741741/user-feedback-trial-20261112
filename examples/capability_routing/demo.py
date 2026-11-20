#!/usr/bin/env python
"""
Capability-based routing demonstration on SqliteHuey.

Scenario walk-through (all against a fresh temporary SQLite database, with
real Huey Consumer instances and local handler functions):

  1. PDF machine offline: PDF-capable tasks stay queued while a plain CSV
     consumer drains the unlabeled CSV backlog.
  2. PDF machine comes online: a consumer with the "pdf" capability picks up
     the waiting PDF tasks, honoring priority then FIFO within its eligible
     subset.
  3. Competing consumers: two consumers sharing a capability race for the
     same labeled backlog; every delivery is processed exactly once.
  4. Upgrade compatibility: an unlabeled task written the way the old release
     wrote them (NULL requirements row, old Message without the field) is
     still executed by any consumer.

Run with the repository on PYTHONPATH, e.g.::

    PYTHONPATH=. python -I examples/capability_routing/demo.py
"""
import logging
import os
import sqlite3
import sys
import tempfile
import time

# Make the in-repo huey package importable when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
# Local handlers live next to this script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from huey import SqliteHuey  # noqa: E402
from huey.consumer import Consumer  # noqa: E402
from huey.registry import Message  # noqa: E402
import tasks as report_tasks  # noqa: E402


logging.getLogger('huey').disabled = True


def banner(title):
    print('\n' + '=' * 72)
    print(title)
    print('=' * 72)


def wait_until(predicate, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def start_consumer(huey, capabilities, workers=2):
    consumer = Consumer(
        huey,
        workers=workers,
        periodic=False,
        initial_delay=0.01,
        max_delay=0.05,
        backoff=1.1,
        scheduler_interval=1,
        check_worker_health=False,
        capabilities=capabilities)
    consumer.start()
    return consumer


def stop_consumer(consumer):
    consumer.stop(graceful=True)


def build_huey(filename):
    huey = SqliteHuey('reports', filename=filename, utc=False)

    # Unlabeled task: any consumer may run it.
    csv_report = huey.task()(report_tasks.csv_report)
    # Labeled at the decorator: requires the pdf capability.
    pdf_report = huey.task(requirements='pdf')(report_tasks.pdf_report)
    # Requires two capabilities at once.
    archive_report = huey.task(
        requirements=('pdf', 'archive'))(report_tasks.archive_report)
    # Labeled task with retries, used to show retries keep the requirements.
    flaky_report = huey.task(
        retries=2, requirements='pdf')(report_tasks.flaky_report)
    return huey, {
        'csv_report': csv_report,
        'pdf_report': pdf_report,
        'archive_report': archive_report,
        'flaky_report': flaky_report,
    }


def scenario_pdf_offline_then_online(huey, t):
    banner('场景 1/2：PDF 机器离线 -> CSV 照常完成 -> PDF 机器上线继续取件')

    t['csv_report']('csv-1')
    t['pdf_report']('pdf-low-1')                  # priority 0
    t['csv_report']('csv-2')
    t['pdf_report']('pdf-high', priority=10)     # priority 10, jumps the line
    t['archive_report']('archive-1')             # needs pdf + archive

    print('入队 5 个任务：2 个 CSV（无标记）、2 个 PDF、1 个 archive(pdf+archive)')
    print('队列深度: %d，调度表: %d' %
          (huey.pending_count(), huey.scheduled_count()))

    # -- Phase 1: only a plain CSV consumer is running. ---------------------
    plain = start_consumer(huey, capabilities=())
    try:
        assert wait_until(lambda: sum(1 for e in report_tasks.run_log
                                       if e.startswith('csv:')) == 2), \
            'CSV backlog was not drained'
        time.sleep(0.3)  # Give it a chance to wrongly touch PDF work.
        print('\n[PDF 离线] 普通消费者运行后:')
        print('  已执行:', [e for e in report_tasks.run_log])
        print('  队列剩余: %d（PDF 任务必须仍在队列中）' % huey.pending_count())
        assert huey.pending_count() == 3, 'plain consumer took a PDF task!'
        assert not any('pdf' in e for e in report_tasks.run_log)
    finally:
        stop_consumer(plain)

    # -- Phase 2: the PDF machine joins. ------------------------------------
    pdf_worker = start_consumer(huey,
                                capabilities=('pdf', 'archive', 'fonts'))
    try:
        assert wait_until(lambda: huey.pending_count() == 0), \
            'PDF-capable consumer did not drain the queue'
        print('\n[PDF 上线] PDF 消费者运行后:')
        print('  已执行:', list(report_tasks.run_log))
        print('  队列剩余: %d' % huey.pending_count())

        # Priority ordering within the PDF-eligible subset: pdf-high first.
        pdf_entries = [e for e in report_tasks.run_log if e.startswith('pdf:')]
        assert pdf_entries[0].startswith('pdf:pdf-high'), pdf_entries
        print('  优先级校验: pdf-high(p=10) 先于 pdf-low(p=0) ->', pdf_entries)
        assert any(e.startswith('archive:archive-1')
                   for e in report_tasks.run_log)
    finally:
        stop_consumer(pdf_worker)


def scenario_competing_consumers(huey, t):
    banner('场景 3：两组具备共同能力的消费者竞争领取，每份投递只执行一次')

    total = 40
    for i in range(total):
        t['pdf_report']('contend-%02d' % i)

    # Two separate Consumer instances sharing the same SQLite file, both
    # advertising the pdf capability.
    consumer_a = start_consumer(huey, capabilities=('pdf',), workers=3)
    consumer_b = start_consumer(huey, capabilities=('pdf',), workers=3)
    try:
        assert wait_until(lambda: huey.pending_count() == 0), \
            'competing consumers did not drain the queue'
    finally:
        stop_consumer(consumer_a)
        stop_consumer(consumer_b)

    handled = [e for e in report_tasks.run_log if 'contend' in e]
    names = sorted(e.split(':', 1)[1] for e in handled)
    print('参与竞争的任务数: %d，实际执行次数: %d，去重后: %d' %
          (total, len(handled), len(set(names))))
    assert len(handled) == total, 'a delivery was lost'
    assert len(set(names)) == total, 'a delivery was processed twice'
    print('结论: 没有重复领取，也没有遗漏。')


def scenario_retry_preserves_requirements(huey, t):
    banner('附加：失败重试保留能力要求')

    t['flaky_report']('retry-1')
    plain = start_consumer(huey, capabilities=())
    pdf_worker = start_consumer(huey, capabilities=('pdf',))
    try:
        assert wait_until(
            lambda: any(e.startswith('flaky-ok:retry-1')
                        for e in report_tasks.run_log)), \
            'flaky task never completed'
    finally:
        stop_consumer(plain)
        stop_consumer(pdf_worker)

    entries = [e for e in report_tasks.run_log if 'retry-1' in e]
    print('重试任务最终执行记录:', entries)
    assert all('pdf-worker' in e for e in entries)
    assert huey.pending_count() == 0
    print('结论: 首次失败后的重试仍只被 PDF 消费者领取。')


def scenario_legacy_unlabeled_task(huey, filename):
    banner('场景 4：升级兼容 —— 旧版本写入的无标签任务照常执行')

    csv_task_class = huey._registry._registry['tasks.csv_report']

    # (a) Raw row with NULL in the requirements column, as the old schema's
    #     rows look after the automatic migration.
    current_blob = huey.serialize_task(csv_task_class(('legacy-null',), {}))
    huey.storage.sql(
        'insert into task (queue, data, priority, requirements) '
        'values (?, ?, ?, ?)',
        (huey.name, current_blob, 0, None), commit=True)

    # (b) A truly old message: the 15-field Message namedtuple has no
    #     requirements field at all.
    old_message = Message(
        'legacy-message-id',
        'tasks.csv_report',
        None, 0, 0, None, ('legacy-msg',), {}, None, None, None, None,
        None, None, None)
    old_blob = huey.serializer.serialize(old_message)
    raw_conn = sqlite3.connect(filename)
    raw_conn.execute('insert into task (queue, data, priority, requirements) '
                     'values (?, ?, ?, ?)',
                     (huey.name, old_blob, 0, None))
    raw_conn.commit()
    raw_conn.close()

    print('写入 2 个旧式无标签任务（NULL requirements / 旧 Message 格式）')
    plain = start_consumer(huey, capabilities=())
    try:
        assert wait_until(
            lambda: sum(1 for e in report_tasks.run_log
                        if 'legacy' in e) == 2), \
            'legacy tasks were not processed'
    finally:
        stop_consumer(plain)

    legacy = sorted(e for e in report_tasks.run_log if 'legacy' in e)
    print('普通消费者执行的遗留任务:', legacy)
    assert len(legacy) == 2
    assert huey.pending_count() == 0
    print('结论: 升级后旧任务无需重新标记，任何消费者都能照常执行。')


def main():
    tmpdir = tempfile.mkdtemp(prefix='huey-capability-demo-')
    filename = os.path.join(tmpdir, 'reports.db')
    print('临时 SQLite 队列: %s' % filename)

    huey, tasks = build_huey(filename)
    try:
        scenario_pdf_offline_then_online(huey, tasks)
        scenario_competing_consumers(huey, tasks)
        scenario_retry_preserves_requirements(huey, tasks)
        scenario_legacy_unlabeled_task(huey, filename)
    finally:
        huey.storage.close()

    banner('全部场景通过')
    print('完整执行顺序 (%d 条):' % len(report_tasks.run_log))
    for entry in report_tasks.run_log:
        print('  -', entry)
    print('\n临时目录: %s（演示结束后可手动删除）' % tmpdir)


if __name__ == '__main__':
    main()
