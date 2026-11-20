# 本地运行说明

源码来自 Huey 上游 commit 7ea73730b9b6d413effdd67880970eb3e674974c，保留 MIT 许可。Python 3.12，SQLite 后端不需要额外服务。

原生测试中的文件存储、文件锁及可选 SQL/stats 用例使用了共享绝对临时路径。为允许多个独立工作区同时运行测试，这个基线仅将这些测试夹具目录改为各测试进程独有的临时目录，并在进程正常退出时清理。原生断言、待测生产代码及预期结果未改变。消费者和 SQLite 核心功能没有预先实现题目要求。

可运行 `python runtests.py api consumer crontab serializer signals storage priority immediate`。也可用 pytest 选择相应原生测试文件；缺少 Redis、Postgres、Django、peewee 等可选后端时按原生条件跳过，不将跳过算作通过。多进程行为应另用实际子进程验证，不能由临时目录隔离代替。

## 时区周期任务（新增）

IANA 时区调度与夏令时处理在 `huey/crontab_tz.py`（`tzcrontab` / `LocalCronSchedule`），去重投递状态由各存储后端的 periodic claim 实现，SQLite 使用独立的 `periodic_claim` 表做跨进程原子认领。

- 单元测试（zoneinfo 固定 DST 日期、可控时间、无网络、不改系统时钟）：`python runtests.py crontab_tz`
- 多进程测试（真实子进程、双调度者、真实重启、普通跨日）：`python runtests.py tz_periodic_multiprocess`
- 终端复现脚本：`python -I scripts/tz_periodic_dst_demo.py`

多进程测试通过 `huey/tests/_tz_multiprocess_driver.py` 以 `python -I -m` 启动真实解释器进程，并经环境变量注入伪造的当前 UTC 时间，因此不依赖系统时钟也不访问公网。
