# receipt_bridge：对账导出的跨库登记

队列接受作业后，导出发布器还要往独立财务库登记。本包提供本地适配器
`ReceiptBridge`，按以下顺序提交（两个库分别提交，不跨库事务）：

1. `save_batch_request()` —— 先把批次请求存入财务库并提交；
2. `publish_batch()` —— 用现有 Huey 队列逐个发布作业（队列库由 Huey
   自行提交），再把任务 ID 补登记到财务库并提交；
3. `write_ledger()` —— 按登记的任务 ID 写账，同一批次只补记一次
   （`ledger_entry.batch_id` 唯一约束保证幂等）。

## 恢复语义（`recover()`）

- **发布前退出**：批次请求仍在财务库（`pending`），恢复时按请求重新发布
  并补登记；已入队的作业按 `job_id` 找回原任务 ID，不重复发布。
- **写账前退出（部分作业已消费）**：从补登记记录找到原任务 ID（登记缺失
  时扫描队列兜底），只补记一次账务；已消费作业按队列剩余情况计入
  `consumed_count` / `pending_count`。
- **冲突批次保留原结果**：`ledger_entry` 已存在不同结果时不覆盖，批次
  标记为 `conflict`；其他待办批次继续恢复，单批失败互不影响。

## 运行验证

```sh
python -m unittest receipt_bridge.tests.test_recovery -v
```

测试用独立子进程分别在发布前、写账前以 `os._exit` 退出模拟崩溃，再由
新进程执行 `python -m receipt_bridge.recover` 恢复，父进程复核队列库与
财务库记录。子进程的数据库路径通过环境变量 `RECEIPT_BRIDGE_QUEUE_DB` /
`RECEIPT_BRIDGE_LEDGER_DB` 指定（见 `receipt_bridge/app.py`）。
