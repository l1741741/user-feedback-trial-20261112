# 租户公平调度设计与配置说明

本文档说明本基线在 Huey 上游 commit `7ea73730` 之上新增的**租户公平调度**（tenant-fair scheduling）。对应英文文档见 `docs/guide.rst` 的 "Tenant-fair scheduling" 一节。

## 1. 解决的问题

多个租户共用一个 `SqliteHuey` 队列时，原有调度严格按 `priority desc, id` 取任务。大租户只要持续提交高优先级任务，其他租户的任务就会无限期排在后面；即使没有优先级，持续的大量小任务也会让新入队的小租户任务长期等待。

## 2. 配置方式（默认关闭）

公平调度是存储层开关，**默认 `False`**，不传该参数时队列行为与改动前完全一致：

```python
from huey import SqliteHuey

huey = SqliteHuey(filename='queue.db', tenant_fair=True)
```

支持的后端：

| 后端 | 是否支持 | 说明 |
| --- | --- | --- |
| `SqliteHuey` / `CySqliteHuey` | 是 | 轮转状态持久化在数据库 `tenant_state` 表 |
| `MemoryHuey`（含 immediate 模式） | 是 | 轮转状态在进程内，由同一把锁保护 |
| Redis / Postgres / File 等 | 否 | 传 `tenant_fair=True` 直接抛 `ConfigurationError`，不会静默忽略 |

发布端通过稳定的非空字符串租户键标识任务，可逐次调用指定，也可在装饰器上声明默认值：

```python
@huey.task()
def ingest(tenant_id, payload): ...

ingest('acme', payload, tenant='acme')          # 逐次指定

@huey.task(tenant='acme')
def acme_report(payload): ...

acme_report(payload, tenant='globex')           # 显式调用覆盖装饰器默认
acme_report(payload, tenant='')                 # ValueError：非法键
```

**未指定租户的任务的兼容行为**：

- 功能关闭时：`tenant` 仍随任务序列化保存，但对出队顺序没有任何影响，队列保持原来的优先级/FIFO 语义。
- 功能开启时：未指定租户的任务统一进入一个显式的**默认桶**（存储层用空字符串标识，反序列化后任务的 `tenant` 仍为 `None`），与其他租户一样参与轮转，不会绕过公平机制。

## 3. 调度算法与公平边界

每次 dequeue 在 SQLite 的一个 `begin exclusive` 事务内完成以下步骤：

1. 查出当前 `task` 表中**有可执行任务**的租户集合；
2. 集合中尚未登记的租户，以当前最大轮转位置之后的新位置登记；
3. 选取轮转位置最小（最久未被服务）的租户；
4. 在该租户内部按 `priority desc, id`（即优先级 + 同级 FIFO）取一条任务并删除；
5. 把该租户的轮转位置推进到全局队尾；
6. 若该租户已无剩余任务，从 `tenant_state` 表删除其登记行。

由此得到的保证：

- **有限等待**：只要一个租户还有可执行任务，任何其他活跃租户都不可能在它之前连续获得第二次服务；大租户无法靠任务数量或优先级霸占调度。
- **租户内部顺序不变**：优先级高的先执行，同级保持 FIFO。
- **空租户不耗轮次**：租户排干后立即退出轮转；之后重新入队时按新位置排到队尾，既不会空转，也不会保留历史偏置。
- **多消费者共享同一份状态**：轮转状态只存在于数据库表，不存在任何消费者进程/线程私有的轮排列表。选租户、取任务、推进状态在同一事务内，事务回滚（例如领取失败、数据库锁冲突）时状态不会被推进。

适用单位与边界（务必注意）：

- 公平的单位是**任务的派发（dequeue）顺序**，即任务被交给 worker 的次序。
- **不保证任务耗时公平或墙钟吞吐公平**：慢任务占用 worker 的时间不会被抢占，公平调度不会中断正在运行的任务，也不限制单租户任务的执行时长。
- 延迟（ETA/delay）任务在调度器提升进可执行 `task` 表之前，对公平轮转完全不可见，不会因为"未来的任务"占用任何租户的轮次。
- 轮转位置是单调递增的 64 位整数，SQLite 整数空间足以长期运行；位置不连续（有删除行）不影响比较语义。

## 4. 重试、任务链与 chord 的归属规则

租户归属是任务序列化消息中的显式字段（`Message.tenant`），系统内**不存在线程局部或请求局部的租户上下文**，因此请求 A 的租户不可能泄漏到请求 B。

| 派生方式 | 租户规则 |
| --- | --- |
| 重试（`retries` / `RetryTask` / 限流重试） | 重试的是同一个任务对象，租户原样保留 |
| 任务链 `task.then(...)`（`on_complete`） | 子任务未显式指定租户时继承来源任务的租户；显式指定则以显式为准。每一跳入队时再次应用该规则 |
| 错误处理 `task.error(...)`（`on_error`） | 同上，复制出的 handler 与来源任务同租户 |
| chord 成员 | 各成员保留自己发布时的租户 |
| chord 回调 | 回调是独立任务，使用它自己发布时的租户；未指定则进默认桶 |
| `Result.reschedule(...)` | 保留原任务的租户 |

## 5. 重启、排空与失败的安全性

- **进程重启**：`tenant_state` 与队列在同一个数据库文件中。新进程打开同一文件即可看到完整轮转状态，无需恢复流程。
- **租户排空后重新入队**：排干时登记行被删除；重新入队会以新的最大位置登记，不会恢复陈旧偏置。
- **某次领取失败**：全部状态变更都在排他事务内，异常时整体回滚，任务仍在队列中、轮转位置不变，worker 下次轮询重试即可。
- **旧数据库迁移**：打开功能前由旧版本 Huey 创建的数据库缺少 `task.tenant` 列，初始化时自动执行 `alter table ... add column`（带默认空串），历史消息可正常读取并归入默认桶，同时补齐新索引。
- **清空队列**：`flush_queue()` 同时清空 `tenant_state`，避免残留派生状态。

## 6. 验证方式

### 单元测试

```shell
.venv/bin/python -m pytest huey/tests/test_tenant_fair.py -q
```

覆盖：默认关闭的严格优先级行为、轮转、洪泛防饿死、租户内优先级/FIFO、默认桶、空租户不耗轮次、延迟任务到期才参与、重试归属、链/chord 归属、非法租户键、序列化往返、重启状态延续、多线程共享轮转、领取失败状态完好、旧库迁移。

### 离线复现脚本（无需任何外部服务）

```shell
# 持续入队、高低优先级交叉、重启、延迟任务、重试/链/chord 谱系
.venv/bin/python -I scripts/offline_tenant_fair_repro.py

# 4 个独立 OS 进程、各自独立连接同一数据库文件的真实多进程验证
.venv/bin/python -I scripts/offline_tenant_fair_multiprocess.py
```

多进程脚本中的每个 worker 都新建自己的 `SqliteHuey` 连接并重新注册任务类（等价于真实消费者重新导入任务模块），用于证明公平状态不依赖进程内内存。

### 原生回归

```shell
.venv/bin/python runtests.py
```

唯一的既有 error（`test_wrappers` 因环境未安装 redis 而报 `ConfigurationError`）在未改动的基线上同样存在，与本特性无关。
