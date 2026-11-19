# 本地运行说明

源码来自 Huey 上游 commit 7ea73730b9b6d413effdd67880970eb3e674974c，保留 MIT 许可。Python 3.12，SQLite 后端不需要额外服务。

原生测试中的文件存储、文件锁及可选 SQL/stats 用例使用了共享绝对临时路径。为允许多个独立工作区同时运行测试，这个基线仅将这些测试夹具目录改为各测试进程独有的临时目录，并在进程正常退出时清理。原生断言、待测生产代码及预期结果未改变。消费者和 SQLite 核心功能没有预先实现题目要求。

可运行 `python runtests.py api consumer crontab serializer signals storage priority immediate`。也可用 pytest 选择相应原生测试文件；缺少 Redis、Postgres、Django、peewee 等可选后端时按原生条件跳过，不将跳过算作通过。多进程行为应另用实际子进程验证，不能由临时目录隔离代替。

## 可靠消费模式（新增）

默认关闭的租约/确认消费模式位于 `huey/storage.py`（SQLite 租约 API）、
`huey/api.py`（`Delivery`、`Huey.claim_lease`、`execute(..., delivery=)`）和
`huey/consumer.py`（`reliable`/`lease_seconds` 参数、续租、收尾）。新增测试：

* `python runtests.py reliable` —— 存储层租约语义、续租、围栏、重试交接、
  关闭行为（进程内）。
* `python runtests.py reliable_subprocess` —— 通过 `huey.tests.reliable_helper`
  启动真实子进程，覆盖真实退出、租约到期接管、跨进程长任务续租、重试交接、
  优雅/强制关闭。两者都可用 pytest 单独运行。

操作文档见 `docs/reliable.rst`，其中列出所有仍可能产生合法重复执行的场景。
