# 发布上下文多进程集成测试

单元测试（`huey/tests/test_publish_context.py`）在单进程内验证上下文校验、继承
与清理。本目录用 **真实的独立子进程** 复现完整链路，证明上下文随消息序列化经过
SQLite 队列跨进程传递，而不是依赖发布进程内的全局变量。

## 组成

* `scripts/integration_tasks.py` —— 发布进程与消费者进程共同导入的任务定义
  （两租户交错、失败重试、嵌套动态子任务、清理探针）；
* `scripts/publisher.py` —— 发布进程，把任务写入 SQLite 队列；
* `scripts/consumer_runner.py` —— 消费者进程，运行真实 `Consumer`（默认线程
  worker，可用 `HUEY_CTX_WORKER=process` 切换为 fork 进程 worker），并把带
  租户 / 关联 ID 的日志写入文件；
* `test_process_integration.py` —— unittest 驱动，编排两个子进程并校验结果。

## 运行

```shell
# 在仓库根目录
.venv/bin/python -I integration/context_propagation/test_process_integration.py -v
```

子进程均以 `python -I`（隔离模式）启动，脚本目录与数据目录分离，路径全部通过
环境变量 / 绝对参数传递，临时数据在 `tearDown` 中清理。

## 覆盖的断言

1. 两个租户（tenant-a / tenant-b）交错执行，每条记录保持自己的关联 ID，不串号；
2. 失败任务重试 3 次，所有尝试与最终成功日志共享同一关联 ID；
3. 嵌套任务内动态发布的子任务继承父上下文；显式覆盖只作用于子任务分支；
4. `publish_context()` 环境上下文被序列化并在消费者侧还原；
5. 消费者日志每条任务记录都带正确的 `tenant` / `correlation_id`，后续任务不会
   贴上一个任务的关联 ID；
6. 消费结束后无线程 / 进程上下文残留，SQLite 队列被完全清空。
