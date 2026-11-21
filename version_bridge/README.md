# 新旧消费者版本衔接（version bridge）

对应 `BUSINESS_CASE.md` 的场景：任务积压由旧业务消费者（v1）和新版（v2）
并行处理，新版投递多带一项业务字段；两版之间提供**可选的**迁移衔接。

## 设计

消息在同一个 Huey SQLite 队列（`task` 表，结构不变）中混排，靠字节前缀
区分版本：

| 消息 | 线上字节格式 | 说明 |
|------|--------------|------|
| v1 | Huey 原始默认格式（裸 pickle 字节） | 未改造的 stock Huey 可直接生产/消费 |
| v2 | `HUEYVB01` 魔数 + 定长头（含版本号、负载长度）+ Huey 原始字节 | 仅多一层信封，Huey 自身的序列化内容不变 |

魔数首字节 `H`(0x48) 与 pickle（0x80）、gzip（0x1f）、zlib（0x78）的
首字节均不冲突，仅凭前缀即可无损区分（见 `envelope.py`）。

兼容取件入口 `VersionedSqliteStorage.dequeue_accepted(accepted_versions)`
在**一条 `BEGIN EXCLUSIVE` 事务**内按 Huey 原生顺序（priority 降序、
id 升序）扫描：跳过当前消费者不接受的版本（原样保留，不删除），原子
删除并返回第一条可处理消息。扫描行数有上限（默认 1000），避免队首
堆积大量不可处理消息时单次轮询长时间持锁；截断不影响正确性。

高层门面 `VersionBridgeHuey`（继承上游 `Huey`，注册表/序列化器全部沿用）：

- `enqueue_versioned(task, version=)`：v1 走原始默认字节，v2 走信封；
- `dequeue_accepted(versions, migrator=)`：反序列化并返回任务，取到
  旧版本消息时可选调用迁移钩子就地补齐新版字段（demo 中给 v1 消息补
  `channel='legacy-default'`）；
- 不调用这些入口时与普通 `SqliteHuey` 行为完全一致，父类原生
  `dequeue()` 仍是不看版本的 drop-in 实现。

## 模块

```
version_bridge/
├── envelope.py                  # 信封格式：包装/拆封/版本识别/损坏校验
├── storage.py                   # VersionedSqliteStorage：兼容取件入口
├── bridge.py                    # VersionBridgeHuey：版本化投递 + 迁移钩子
├── demo/
│   ├── common.py                # 共享任务注册、队列构造、JSONL 事件日志
│   ├── v1_tasks.py / v2_tasks.py      # 两个代码版本的业务执行函数
│   ├── v1_producer.py           # v1 写入进程（stock Huey，原始默认格式）
│   ├── v2_producer.py           # v2 写入进程（信封 + channel 字段）
│   ├── v1_consumer.py           # v1 取件进程（只取 v1，保留 v2 并继续）
│   ├── v2_consumer.py           # v2 取件进程（v1/v2 通吃，v1 经迁移补齐）
│   └── run_scenarios.py         # 三个端到端场景的子进程驱动
└── tests/                       # 24 个测试（含真实子进程与并发验证）
```

## 运行

```bash
# 全部测试（单元 + 子进程场景 + 多进程并发原子性）
.venv/bin/python -I -m unittest discover -s version_bridge/tests -p 'test_*.py'

# 可读的端到端场景演示（实际子进程，独立临时库）
.venv/bin/python -I -m version_bridge.demo.run_scenarios
```

## 三个场景与业务要求的对应

1. **升级**（`run_scenarios upgrade`）：v1 消费者与 v2 写入并行。
   v1 取走 o-1/o-2；v2 的 o-3/o-4 入队后被 v1 跳过并**保留**；随后
   v1 写入的 o-5 仍被 v1 正常取走（证明跳过 v2 后继续取后续可处理项）。
   切换 v2 消费者后，保留的 o-3/o-4 被读出，`channel` 字段原样保留。
2. **回退**（`run_scenarios rollback`）：v2 已产生 n-1/n-2 积压，
   旧版恢复投递 n-3/n-4。v1 消费者只清空 v1 部分，v2 积压在库中
   原样保留（事件日志 `backlog: {2: 2}`）；v2 重新上线后两类都能处理。
3. **原有默认消息格式**（`run_scenarios default_format`）：v1 全程
   使用未改造的 stock Huey，库中字节不带任何信封；stock 原生
   `dequeue()` 与兼容入口都能读取；v2 读取时迁移钩子补齐 channel。

## 并发安全

`test_concurrent_dequeue.py` 启动 3 个 v1 + 2 个 v2 消费者进程并行
取 22 条交错消息，断言：每条消息恰好处理一次（无重复、无丢失）、
v1 消费者绝不会取到 v2 消息、队列最终清空。排他事务保证跨进程原子性。
