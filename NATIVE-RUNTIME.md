# 本地运行说明

源码来自 Huey 上游 commit 7ea73730b9b6d413effdd67880970eb3e674974c，保留 MIT 许可。Python 3.12，SQLite 后端不需要额外服务。

原生测试中的文件存储、文件锁及可选 SQL/stats 用例使用了共享绝对临时路径。为允许多个独立工作区同时运行测试，这个基线仅将这些测试夹具目录改为各测试进程独有的临时目录，并在进程正常退出时清理。原生断言、待测生产代码及预期结果未改变。消费者和 SQLite 核心功能没有预先实现题目要求。

可运行 `python runtests.py api consumer crontab serializer signals storage priority immediate`。也可用 pytest 选择相应原生测试文件；缺少 Redis、Postgres、Django、peewee 等可选后端时按原生条件跳过，不将跳过算作通过。多进程行为应另用实际子进程验证，不能由临时目录隔离代替。

持久化撤销（`durable_revoke=True`）的用例位于 `huey/tests/test_durable_revoke.py`，用 `python -m unittest huey.tests.test_durable_revoke` 运行。其中 `OfflineDurableRevokeTests` 通过 `huey/tests/durable_revoke_driver.py` 启动真实子进程，验证进程退出重启后撤销仍然有效、以及在独立进程里撤销与延迟重试并发时的结果，均针对同一个本地 SQLite 文件。
