"""恢复子进程入口：新进程扫描财务库待办批次并恢复。

用法：

    python -m receipt_bridge.recover

恢复摘要以 JSON 打印到标准输出，供调用方复核。
"""
import json
import sys

from receipt_bridge.adapter import ReceiptBridge
from receipt_bridge.app import LEDGER_DB, export_job, huey


def main():
    bridge = ReceiptBridge(huey, LEDGER_DB, export_job)
    summary = bridge.recover()
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2,
              sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
