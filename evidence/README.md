# 有界批量发布的终端运行证据（evidence）

本目录的脚本独立于 `huey` 包，可直接用仓库虚拟环境运行，用于给出题目要求的
**内存有界**与**终端运行**证据。脚本只依赖标准库与本仓库的 `huey` 包。

运行方式（在仓库根目录）：

```sh
.venv/bin/python -I evidence/evidence_bounded.py
.venv/bin/python -I evidence/evidence_hardlimit.py
.venv/bin/python -I evidence/evidence_multiproc.py
```

三个脚本分别证明：

1. `evidence_bounded.py`
   - 20 万个任务的一次性生成器**不会被展开到内存**：用带副作用计数的生成器，
     断言生成器领先消费者的数量、峰值在途数都不超过声明窗口；用
     `tracemalloc` 证明缓冲内存与 N 无关。
   - 两个真实线程生产者 + 慢消费者下，SQLite 队列大小不突破硬上限。
   - 消费者停止后取消/关闭不会死锁，控制记录（在途信号量）全部释放。
2. `evidence_hardlimit.py`
   - 在**插入提交点**插桩，记录每次成功插入后的真实队列大小，断言 600 次
     提交插入没有任何一次让队列超过硬上限。
3. `evidence_multiproc.py`
   - 用独立 **OS 进程**（`spawn`）发布到同一个 SQLite 文件、另一个进程消费，
     证明硬上限在真实进程/连接边界下仍然成立（线程隔离无法替代这一点）。

正式单元测试见 `huey/tests/test_bulk_enqueue.py`，可通过
`.venv/bin/python -I runtests.py bulk_enqueue` 运行。
