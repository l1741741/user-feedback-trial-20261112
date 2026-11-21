"""队列与财务库的运行时配置。

队列库与财务库是两个独立的 SQLite 文件，分别提交。子进程通过环境变量
指定路径：

- ``RECEIPT_BRIDGE_QUEUE_DB``：Huey 队列库（含结果存储）；
- ``RECEIPT_BRIDGE_LEDGER_DB``：独立财务库（批次请求 / 登记 / 账务）。
"""
import os

from huey import SqliteHuey

QUEUE_DB = os.environ.get('RECEIPT_BRIDGE_QUEUE_DB',
                          os.path.abspath('receipt-bridge-queue.db'))
LEDGER_DB = os.environ.get('RECEIPT_BRIDGE_LEDGER_DB',
                           os.path.abspath('receipt-bridge-ledger.db'))

huey = SqliteHuey('receipt-bridge', filename=QUEUE_DB)


@huey.task()
def export_job(batch_id, job_id, amount):
    """对账导出作业：被消费后结果写回队列库的结果存储。"""
    return {'batch_id': batch_id, 'job_id': job_id, 'amount': amount}
