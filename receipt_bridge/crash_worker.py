"""崩溃注入子进程：在指定阶段以 ``os._exit`` 退出，模拟进程崩溃。

用法：

    python -m receipt_bridge.crash_worker <jobs.json> <batch_id> <阶段> [消费数]

阶段：

- ``before_publish``：保存批次请求后、发布前退出；
- ``before_ledger``：发布并补登记、消费部分作业后、写账前退出；
- ``none``：完整流程（对照用）。

数据库路径由环境变量 ``RECEIPT_BRIDGE_QUEUE_DB`` /
``RECEIPT_BRIDGE_LEDGER_DB`` 指定（见 receipt_bridge.app）。
"""
import json
import os
import sys

from receipt_bridge.adapter import ReceiptBridge
from receipt_bridge.app import LEDGER_DB, export_job, huey


def consume_jobs(count):
    """从队列消费若干作业并执行（结果写回队列库），返回任务 ID。"""
    consumed = []
    for _ in range(count):
        task = huey.dequeue()
        if task is None:
            break
        huey.execute(task)
        consumed.append(task.id)
    return consumed


def main(argv):
    jobs_path, batch_id, stage = argv[1:4]
    consume = int(argv[4]) if len(argv) > 4 else 0
    with open(jobs_path) as fh:
        jobs = json.load(fh)

    bridge = ReceiptBridge(huey, LEDGER_DB, export_job)
    bridge.save_batch_request(batch_id, jobs)
    if stage == 'before_publish':
        os._exit(1)  # 发布前退出：财务库只有批次请求
    bridge.publish_batch(batch_id)
    if consume:
        consume_jobs(consume)
    if stage == 'before_ledger':
        os._exit(1)  # 写账前退出：已补登记，部分作业已消费
    bridge.write_ledger(batch_id)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
