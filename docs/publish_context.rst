# 发布上下文（租户标识与请求关联 ID）

多个业务方共用同一个消费者时，需要把 **请求关联 ID（correlation id）** 与
**租户标识（tenant）** 一路带到任务执行、重试和回调日志里。Huey 提供了显式的
*发布上下文（publish context）*：上下文作为任务消息的一部分被序列化，随队列
（SQLite / Redis / ...）传递到消费者进程，**不依赖发布进程中的任何全局变量**。

相关 API 位于 :mod:`huey.context`，并从顶层包直接导出：

```python
from huey import PublishContext, publish_context, get_current_context
from huey import ContextLogFilter, ContextValidationError
```

> 注意：本特性与 ``@huey.task(context=True)`` 的 ``context`` 参数 **完全无关**。
> 后者的语义是“把 Task 实例本身作为 ``task`` 关键字参数传给任务函数”，该语义
> 保持原样不变（详见文末“兼容性”一节）。

## 基本用法

### 显式发布（推荐）

构造一个不可变的 :class:`~huey.context.PublishContext`，通过框架保留关键字
``task_context`` 附加到单次任务调用上：

```python
from huey import PublishContext

ctx = PublishContext(tenant='acme', correlation_id='req-001')

# 只作用于这一次调用
process_order(order_id, task_context=ctx)
```

任务函数内部通过 :func:`~huey.context.get_current_context` 读取：

```python
@huey.task()
def process_order(order_id):
    ctx = get_current_context()
    logger.info('processing order %s', order_id)  # 见下文日志过滤器
    return (ctx.tenant, ctx.correlation_id)
```

``task_context`` 也可以用在签名（``.s()``）、``schedule()`` 以及
``.then()/.error()`` 的后继任务上：

```python
process_order.s(order_id, task_context=ctx)
process_order.schedule((order_id,), delay=60, task_context=ctx)
process_order.s(order_id, task_context=ctx).then(send_receipt)
```

### 环境上下文（publish_context 块）

在一段代码内统一发布多个任务时，可以用
:class:`~huey.context.publish_context` 上下文管理器设置“环境上下文”，块内发布
且 **没有显式上下文** 的任务会自动携带它：

```python
with publish_context(PublishContext(tenant='acme', correlation_id='req-001')):
    validate_order(order_id)          # 自动携带
    charge_card(card_id)              # 自动携带
    send_receipt(task_context=other)  # 显式上下文优先
# 退出块后精确恢复进入前的值（包括 None），不会残留
```

环境上下文基于 :mod:`contextvars` 实现，按线程 / 协程隔离，支持嵌套。

## 受控结构与大小限制

上下文只接受 **可控的结构和大小**，所有校验在 ``PublishContext`` 构造时完成，
非法输入抛出 :class:`~huey.context.ContextValidationError`，任务在进入队列之前
就被拒绝（fail fast）：

* ``tenant``：必填、非空字符串，最长 64 字符；
* ``correlation_id``：非空字符串，最长 128 字符；省略时自动生成 UUID；
* ``fields``：可选的扁平 ``dict``，最多 16 项；键为字符串（≤64 字符），值只允许
  字符串 / 数字 / 布尔（数字与布尔统一转成字符串，单值 ≤256 字符），不接受嵌套
  结构或任意对象；序列化总量上限 4096 字节。

敏感信息一律拒绝，不能进入日志上下文：

* 键名（小写匹配）包含 ``password``、``passwd``、``secret``、``token``、
  ``authorization``、``auth``、``api_key``、``cookie``、``credential``、
  ``private_key`` 等；
* 值形如原始 Authorization 头（``Bearer ...``、``Basic ...``、
  ``Digest ...``、``Authorization: ...``）。

```python
PublishContext('acme', fields={'password': 'hunter2'})
# -> ContextValidationError

PublishContext('acme', fields={'trace': 'Bearer eyJ...'})
# -> ContextValidationError
```

## 执行期生命周期与清理

* 任务在消费者侧 **执行前**，其自带上下文被绑定到当前工作线程 / 协程；
* 业务函数（包括框架的执行、异常、重试与收尾日志）运行期间可通过
  ``get_current_context()`` 读取；
* 业务结束 **或抛错后**，通过 ContextVar token 恢复进入前的值。同一工作线程随后
  执行另一个租户的任务时不会有任何残留；
* 嵌套的 immediate 执行（任务内部再以 immediate 模式执行子任务）退出时恢复外层
  任务的上下文；异步任务与 immediate 任务互不污染。

## 继承规则

| 场景 | 规则 |
| --- | --- |
| 普通任务 | 使用显式 ``task_context``；否则继承环境上下文；都没有则为 ``None`` |
| 链 / 管道（``.then()``） | 后继任务继承父任务上下文，除非后继自带显式上下文 |
| 错误后继（``.error()``） | 同链规则：继承失败任务的上下文，显式上下文优先 |
| 重试（retries / RetryTask） | 同一个任务对象重新入队，上下文在所有尝试间保持不变 |
| group | 每个成员独立解析；显式覆盖只作用于该成员，不影响兄弟任务 |
| chord 回调 | 回调聚合所有成员，**不** 继承成员执行时的上下文；回调自带的显式上下文仍然生效 |
| 任务内动态发布的子任务 | 自动继承正在执行任务的上下文；显式 ``task_context`` 可覆盖 |
| 周期任务 | 发布时没有环境上下文即不带上下文（``None``） |

### 显式覆盖只作用于当前分支

环境上下文在入队时通过 **浅拷贝** 附加到任务副本上，调用方手里的签名 / 模板
对象不会被修改，共享模板与兄弟任务不受影响：

```python
signature = my_task.s()
with publish_context(ctx):
    huey.enqueue(signature)
assert signature.task_context is None  # 原对象保持干净
```

## 日志集成

:class:`~huey.context.ContextLogFilter` 把当前上下文注入每条日志记录，可直接在
格式化字符串中使用 ``tenant`` 与 ``correlation_id``（无上下文时为 ``-``）：

```python
import logging
from huey import ContextLogFilter

handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s [tenant=%(tenant)s cid=%(correlation_id)s] '
    '%(name)s: %(message)s'))
handler.addFilter(ContextLogFilter())
logging.getLogger('huey').addHandler(handler)
```

过滤器对异常完全免疫：日志格式化或注入过程中的任何错误都被吞掉，**不会阻断任务
收尾**，也不会让后续任务的记录贴上错误的关联 ID。

## 序列化与队列传递

* 任务消息（``registry.Message``）新增末尾字段 ``task_context``，线上传输格式为
  普通元组 ``(correlation_id, tenant, fields)``，默认 pickle 序列化器、
  ``SignedSerializer`` 以及压缩选项均可直接处理；
* 消费端反序列化时会 **重新执行完整校验**，队列中损坏 / 越界的数据在任务执行前
  抛出 ``ContextValidationError``；
* 旧版本写入的 15 字段消息缺少该字段时按 ``None`` 处理，保持向后兼容。

## 兼容性边界

### 原 ``context=True`` 语义不变

``@huey.task(context=True)`` 仍然表示把 Task 实例注入为 ``task`` 关键字参数。
两套机制可以同时使用，互不干扰。

### 自定义 Task / TaskWrapper

``Task.__init__`` 新增 **末尾关键字参数** ``task_context=None``，位置参数顺序
未变。自定义 Task 子类若覆盖 ``__init__``，需要转发该关键字（建议使用
``**kwargs`` 透传给 ``super().__init__``），否则该任务无法携带发布上下文。
直接覆盖 ``execute()`` 时，上下文绑定由 ``Huey.execute()`` 在外层完成，无需子类
自行管理。

### 自定义 Serializer

线上传输的上下文只包含 ``str`` / ``tuple`` / ``dict`` 内建类型，自定义序列化器
只需能处理这些类型与 ``Message`` namedtuple 即可，无需认识
``PublishContext`` 类。若自行实现消息封装，请保留 ``task_context`` 字段（缺失时
为 ``None``）。

### 保留关键字

``task_context`` 是框架级保留关键字，不能同时作为任务函数自身的关键字参数名。
