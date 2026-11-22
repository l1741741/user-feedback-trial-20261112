# SQLite 分片迁移演示（订单组 A 从 shard1 迁往 shard2）

扩容时，一个订单组的待办要从一个 SQLite 库迁往另一个 SQLite 库，而其他组
继续在源库上被消费。本目录用 **两个真实数据库 + 两个真实 Huey 消费者**
演示可选迁移入口 `huey.contrib.migrate.ShardMigrator` 的完整行为。

## 运行

```bash
.venv/bin/python -m examples.shard_migration.run_demo
```

命令行入口（同样支持断点续跑）：

```bash
.venv/bin/python -m huey.bin.migrate_shard \
  --source examples.shard_migration.huey_settings:source_huey \
  --destination examples.shard_migration.huey_settings:target_huey \
  --selector examples.shard_migration.huey_settings:order_group_selector \
  --migration-id orders-group-A-to-shard2 \
  --batch-size 2 --kinds queue,schedule
```

## 发布方版本兼容性

迁移工具读取的线上消息是 `huey.registry.Message` 具名元组的 pickle。该格式
自 **Huey 2.0.0** 引入，之后只通过追加字段（`Message.__new__.__defaults__`）
演进，新旧版本互可反序列化；`task/schedule/kv/counter` 关系表结构自
SqlStorage 发布以来保持稳定。因此支持的发布方（生产者）版本为：

> **Huey >= 2.0.0, < 4.0.0**（常量 `SUPPORTED_PRODUCER_SPEC`，本基线在
> 3.5.4 上测试）。

约束：两侧 Huey 实例必须使用相同的 Serializer 配置（压缩开关、签名
secret/salt）；队列名必须一致。工具启动时会做序列化器往返自检。

## 文件

- `tasks.py` — 订单任务定义与执行日志；业务身份是 `(order_id, group)`。
- `huey_settings.py` — 两个真实 SQLite 库（`runtime/source.db`、
  `runtime/target.db`）上的两个 SqlHuey 实例，以及组 A 选择器。
- `run_demo.py` — 三个场景的端到端编排。

## 演示场景

1. **部分迁移 + 中断续跑**：`max_batches=1` 只迁移一批后模拟操作员退出；
   新建迁移器实例，仅凭目标库 `huey_migration_progress` 表中的持久进度续跑。
   断言：组 A 队列消息与 ETA 计划消息全部迁移，ETA 原样保留；目标侧出队
   顺序与原始投递顺序一致（FIFO）；组 B 一行未动，全程可继续消费。
2. **源队列清空后的重试**：再次运行迁移器，扫描/选择/迁移均为 0，不产生
   任何重复行——已迁移项不会再生成。
3. **冲突处理**：预先在目标库植入同一任务 UUID 的消息，模拟"上一轮已写入
   但未及删除源行就崩溃"。迁移器按业务身份（任务 UUID，而非各库自增的
   行 id）判重：负载一致则跳过插入并清理源行；负载不一致则抛出
   `MigrationError` 拒绝覆盖。

## 持久进度与幂等

- 进度表 `huey_migration_progress(migration_id, kind, last_row_id, migrated,
  updated_at)` 位于**目标库**，按 `(migration_id, kind)` 唯一。
- 扫描游标沿单调的插入序行 id 推进，高水位对每一行（无论是否选中）更新，
  因此跳过的行不会遮蔽后续行。
- 目标侧写入先于源侧删除；崩溃后重跑由 UUID 判重兜底，保证 at-least-once
  且不重复投递。
