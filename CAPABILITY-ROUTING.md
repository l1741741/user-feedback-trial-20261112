# 按消费者能力路由任务（capability routing）

## 要解决的问题

两类部署共用一个 SqliteHuey 队列：装了字体包的机器能生成 PDF，普通机器只跑
CSV。改造前普通机器领到 PDF 任务只会失败重试，还会挡住后面的 CSV。改造后：

- 任务发布时可以声明**能力要求** `requirements`；
- 消费者启动时声明自己具备的**能力集合** `capabilities`；
- 消费者只领取“要求集合 ⊆ 自身能力集合”的任务；
- 没有声明要求的旧任务（无标签）任何消费者都能领取，升级无需迁移数据。

## 数据模型与匹配规则

- 能力用字符串标识（如 `'pdf'`、`'fonts'`）。任务的 `requirements` 在内部
  归一化为排序元组（如 `('fonts', 'pdf')`），消费者的 `capabilities` 归一化为
  `frozenset`。
- 匹配规则（`huey/capabilities.py::can_handle`）：
  - `requirements is None`（无标签任务）→ 任何消费者都可领取；
  - 否则要求集合必须是消费者能力集合的子集，缺一不可。
- SQLite `task` 表新增可空列 `requirements`（JSON 文本，无标签为 SQL NULL）。
  老数据库打开时自动执行一次幂等的 `ALTER TABLE ... ADD COLUMN`
  （`SqliteStorage.initialize_schema`），旧行保持 NULL，语义就是“无标签”。
- 延迟队列表 `schedule` 不新增列：能力要求随任务消息（pickle 的 `Message`）
  一起序列化存储，到期重新排队、失败重试时原样带回。

### 出队的顺序与并发安全

`SqliteStorage.dequeue(capabilities)` 在原来的单个 `begin exclusive`
事务内完成“按 `priority desc, id` 顺序扫描 → 首个匹配行 → 删除”：

- 不匹配的行原地保留，不影响同一消费者可处理的其他积压；
- 匹配仍在**可处理子集内**遵守优先级、同级按入队先后（id 递增）；
- 扫描与删除在同一排他事务中，并发消费者不可能把同一行移出队列两次。

## 任务链中每一项的能力从哪里来

链指 `task.then(...)` 形成的 `on_complete` 链。每一项的有效要求按以下优先级
解析（`huey/capabilities.py::resolve_chain_requirements`，在 `Huey.enqueue`
序列化前对整条链执行一次）：

1. **投递时显式指定**：`pdf_task.s(..., requirements='fonts')` 或
   `wrapper.schedule(..., requirements=...)` 给出的实例级参数，优先级最高；
2. **装饰器类级默认**：`@huey.task(requirements='pdf')` 写入任务类的
   `default_requirements`；
3. **链中前一项的有效要求**：仅当本任务既没有实例级指定、也没有类级默认时
   才继承；
4. 以上都没有则为 `None`（无标签，任何消费者可运行）。

因此**前一项的设置永远不会覆盖后继的明确指定**：后继只要在 1 或 2 任一级别
显式声明，就以自身声明为准。解析在两个收口点执行：`Huey.enqueue` 入口与
`Huey.serialize_task`（后者覆盖 chord 回调、`on_error` 处理器等不经过
`enqueue` 直接序列化的路径），且解析幂等、可重复调用。解析结果写回各任务
实例后，链上每个消息都自带要求，后续调度、重试、反序列化不再需要特殊处理。
`on_error` 链从拥有该处理器的任务继承要求，但处理器自身的显式指定同样优先。
`on_complete` 与 `on_error` 是两条独立的链，各自解析、互不影响。

### 三态语义与序列化

任务的 requirements 在内存中有三种状态：显式元组、显式 `None`（无标签）、
`UNSPECIFIED` 哨兵（未指定，等待链解析）。哨兵不能直接 pickle，registry
序列化时将其替换为私有标记字符串，反序列化时还原；旧版本 15 字段消息缺少该
字段，namedtuple 默认值为 `None`，语义就是显式无标签。写入存储 `requirements`
列的永远只有解析后的元组（JSON）或 NULL，标记字符串不会落库。

### 要求在生命周期各环节的保留方式

| 环节 | 保留机制 |
| --- | --- |
| 普通入队 | `Huey.enqueue` 把解析后的 `task.requirements` 同时写入消息体和 `task.requirements` 列 |
| 延迟任务（eta/delay） | Huey 的延迟语义是先带 eta 进入普通队列；能力不匹配的消费者在 `dequeue` 阶段就取不到它。匹配的消费者 `execute` 时发现未到期才转入 schedule 表，要求随消息保留；调度器到期重新 `enqueue` 后仍受同一过滤 |
| 失败立即重试 | `_requeue_task` 重新 `enqueue` 同一个任务实例，要求不变 |
| 失败带 `retry_delay` | 任务带 eta 进入 schedule 表，要求随消息保留，到期重新排队后仍受能力过滤 |
| `on_complete` 后继 | 入队前整条链解析完毕，后继消息携带各自的有效要求 |
| `on_error` 处理器 | 从所属任务继承，自身显式指定优先；经 `serialize_task` 收口解析 |
| chord 回调 | `_check_chord` 直接入队，由 `serialize_task` 统一解析，类级默认不丢 |
| `Result.reschedule` | 显式复制原任务的 `requirements` |

## 发布与消费的用法

```python
from huey import SqliteHuey

# 普通机器：不声明能力（或只声明 csv），只会拿到无标签任务
huey_csv = SqliteHuey('reports', filename='queue.db')

# PDF 机器：声明能力
huey_pdf = SqliteHuey('reports', filename='queue.db',
                      capabilities=('pdf', 'fonts'))

@huey_pdf.task()                       # 无标签，任何消费者可跑
def csv_report(name): ...

@huey_pdf.task(requirements='pdf')     # 类级默认要求 pdf
def pdf_report(name): ...

# 单次投递显式指定，覆盖类级默认
pdf_report('special', requirements=('pdf', 'fonts'))
```

消费者进程三种声明能力的方式：

- 构造参数：`huey.create_consumer(capabilities=('pdf',))`；
- Huey 实例级默认：`SqliteHuey(..., capabilities=('pdf',))`，该实例创建的
  消费者自动继承；
- 命令行：`huey_consumer --capabilities pdf,fonts app.huey_instance`。

## 向后兼容

- 旧 `task` 表：启动时自动加列，旧行 NULL = 无标签；
- 旧消息：`Message` 是带默认值的 namedtuple，新字段 `requirements`
  追加在末尾，旧 15 字段消息反序列化得到 `None`；
- 存储接口的新参数（`enqueue(..., requirements=None)`、
  `dequeue(capabilities=None)`）全部带默认值，未传时行为与旧版完全一致；
- Redis/Postgres/File 后端同步更新了方法签名；在这些后端上显式使用能力
  过滤会抛出明确的 `NotImplementedError`，不会静默忽略。能力路由的完整实现
  覆盖 Memory 与 SQLite（含 CySqlite，继承同一实现）。

## 演示与测试

- `examples/capability_routing/demo.py`：临时 SQLite 库 + 真实 Consumer
  线程，演示 PDF 离线、CSV 照常、PDF 上线按优先级取件、同能力竞争领取、
  重试保留要求、旧式无标签任务兼容；
- `examples/capability_routing/contention_demo.py` 与
  `contention_worker.py`：真实**子进程**消费者，验证跨进程下普通消费者不碰
  PDF、两个 PDF 进程竞争时每份投递只出队一次；
- `huey/tests/test_capabilities.py`：21 个单元测试，Memory 与 SQLite
  双后端覆盖路由、优先级/FIFO、多能力子集、延迟与重试保留、链继承规则、
  旧 schema/旧消息兼容、并发去重。

运行：

```sh
PYTHONPATH=. .venv/bin/python -I examples/capability_routing/demo.py
PYTHONPATH=. .venv/bin/python -I examples/capability_routing/contention_demo.py
.venv/bin/python runtests.py capabilities
```
