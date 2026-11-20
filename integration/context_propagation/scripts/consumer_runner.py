"""
消费者进程：运行真实的 Huey Consumer（线程 worker），从 SQLite 队列消费任务。

进程结束前检查工作线程上没有残留任何发布上下文，并把检查结果写入结果文件。
"""
import logging
import os
import sys
import time

_REPO_ROOT = os.environ['HUEY_REPO_ROOT']
_SCRIPT_DIR = os.environ.get('HUEY_SCRIPTS_DIR') or \
        os.path.dirname(os.path.abspath(__file__))
for path in (_REPO_ROOT, _SCRIPT_DIR):
    if path and path not in sys.path:
        sys.path.insert(0, path)

from huey import ContextLogFilter  # noqa: E402
from huey import get_current_context  # noqa: E402
from huey.consumer import Consumer  # noqa: E402

from integration_tasks import huey  # noqa: E402


def _record(line):
    with open(os.environ['HUEY_CTX_RESULT'], 'a', encoding='utf-8') as fh:
        fh.write(line + '\n')


def main():
    log_path = os.environ['HUEY_CTX_LOG']

    # Attach the context log filter to the huey loggers so every task log
    # line carries its tenant / correlation id.
    log_filter = ContextLogFilter()
    formatter = logging.Formatter(
        '%(levelname)s [tenant=%(tenant)s cid=%(correlation_id)s] %(message)s')
    handler = logging.FileHandler(log_path)
    handler.setFormatter(formatter)
    handler.addFilter(log_filter)
    root = logging.getLogger('huey')
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for name in ('huey', 'integration.tasks'):
        logging.getLogger(name).addFilter(log_filter)

    # Two worker threads, so interleaving is real. The worker type can be
    # overridden via HUEY_CTX_WORKER (thread / process / greenlet).
    worker_type = os.environ.get('HUEY_CTX_WORKER', 'thread')
    consumer = Consumer(huey, workers=2, initial_delay=0.01, max_delay=0.05,
                        scheduler_interval=1, periodic=False,
                        check_worker_health=False,
                        worker_type=worker_type)
    consumer.start()
    try:
        # Wait until the queue is drained (publisher enqueued a fixed set).
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if huey.pending_count() == 0 and huey.scheduled_count() == 0:
                # Give in-flight tasks a moment to finish their bookkeeping.
                time.sleep(0.3)
                if huey.pending_count() == 0:
                    break
            time.sleep(0.1)
        else:
            raise RuntimeError('consumer did not drain the queue in time')
    finally:
        consumer.stop(graceful=True)

    # Main thread never executes tasks, so its context must be empty.
    if get_current_context() is not None:
        _record('RESIDUE main-thread context leaked: %r' %
                (get_current_context(),))
    else:
        _record('residue-check main-thread clean')

    _record('consumer-stopped')


if __name__ == '__main__':
    main()
