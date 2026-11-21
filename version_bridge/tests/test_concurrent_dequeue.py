"""并发原子性测试：多个消费者进程并行取件，验证兼容入口的事务安全性。

SQLite 的 ``BEGIN EXCLUSIVE`` 事务必须保证：同一条消息不会被两个进程
同时取走，跳过保留的消息不会丢失或被误删。用真实子进程制造并发压力，
而不是同进程内的顺序夹具。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


V1_COUNT = 12
V2_COUNT = 10
V1_CONSUMERS = 3
V2_CONSUMERS = 2

# 子进程用 ``python -c 'from <module> import ...'`` 导入本模块，必须使用
# 完整包限定名——unittest discover 下模块 __name__ 不含包前缀，不能直接用。
THIS_MODULE = 'version_bridge.tests.test_concurrent_dequeue'


def write_backlog(db_path, log_path):
    """单个子进程：批量写入交错的 v1/v2 消息。"""
    from version_bridge.bridge import VersionBridgeHuey
    from version_bridge.demo.common import define_process_order
    from version_bridge.envelope import VERSION_1
    from version_bridge.envelope import VERSION_2

    huey = VersionBridgeHuey('orders', filename=db_path, results=False,
                             utc=False)
    process_order = define_process_order(
        huey, lambda order_id, channel=None: (order_id, channel))
    for idx in range(V1_COUNT):
        huey.enqueue_versioned(
            process_order.s('v1-%02d' % idx), version=VERSION_1)
    for idx in range(V2_COUNT):
        huey.enqueue_versioned(
            process_order.s('v2-%02d' % idx, 'web'), version=VERSION_2)


def consume(db_path, log_path, accepted, tag, max_wait):
    """单个消费者子进程：按声明版本集合循环取件并执行。"""
    import time

    from version_bridge.bridge import VersionBridgeHuey
    from version_bridge.demo.common import define_process_order
    from version_bridge.demo.common import append_event
    from version_bridge.envelope import VERSION_1
    from version_bridge.envelope import VERSION_2

    huey = VersionBridgeHuey('orders', filename=db_path, results=False,
                             utc=False)
    define_process_order(
        huey, lambda order_id, channel=None: (order_id, channel))
    accepted_versions = {VERSION_1: {VERSION_1},
                         VERSION_2: {VERSION_1, VERSION_2}}[accepted]
    deadline = time.monotonic() + max_wait
    while True:
        dequeued = huey.dequeue_accepted(accepted_versions)
        if dequeued.task is None:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
            continue
        huey.execute(dequeued.task)
        append_event(log_path, {
            'actor': tag,
            'event': 'processed',
            'order_id': dequeued.task.args[0],
            'message_version': dequeued.version,
        })
        deadline = time.monotonic() + max_wait


def run_in_subprocess(target, *args):
    """按 ``(模块名, 函数名)`` 在独立子进程内调用目标函数。"""
    module, func_name = target
    cmd = [sys.executable, '-I', '-c',
           'from %s import %s as f; f(*%r)'
           % (module, func_name, args)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError('subprocess failed:\n%s' % proc.stderr)


class ConcurrentDequeueTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='vbridge-concurrent-')
        self.db_path = os.path.join(self.workdir, 'queue.db')
        self.log_path = os.path.join(self.workdir, 'events.log')

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_parallel_consumers_no_duplicates_no_loss(self):
        run_in_subprocess(
            (THIS_MODULE, 'write_backlog'), self.db_path, self.log_path)

        producers = []
        for idx in range(V1_CONSUMERS):
            producers.append(subprocess.Popen([
                sys.executable, '-I', '-c',
                'from %s import consume; consume(%r, %r, %d, %r, 8.0)'
                % (THIS_MODULE, self.db_path, self.log_path,
                   1, 'v1-consumer-%d' % idx)]))
        for idx in range(V2_CONSUMERS):
            producers.append(subprocess.Popen([
                sys.executable, '-I', '-c',
                'from %s import consume; consume(%r, %r, %d, %r, 8.0)'
                % (THIS_MODULE, self.db_path, self.log_path,
                   2, 'v2-consumer-%d' % idx)]))
        for proc in producers:
            proc.wait(timeout=60)
            self.assertEqual(proc.returncode, 0)

        with open(self.log_path, encoding='utf-8') as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        processed = [e for e in events if e['event'] == 'processed']

        # 1) 每条消息恰好被处理一次：无重复、无丢失。
        order_ids = [e['order_id'] for e in processed]
        self.assertEqual(len(order_ids), V1_COUNT + V2_COUNT)
        self.assertEqual(len(set(order_ids)), len(order_ids))
        expected = {'v1-%02d' % i for i in range(V1_COUNT)}
        expected |= {'v2-%02d' % i for i in range(V2_COUNT)}
        self.assertEqual(set(order_ids), expected)

        # 2) 版本不串：v1 消费者只能处理 v1；v2 消息全部由 v2 消费者处理。
        for event in processed:
            if event['actor'].startswith('v1-consumer'):
                self.assertEqual(event['message_version'], 1)
            else:
                self.assertIn(event['message_version'], (1, 2))
        v2_handled = [e for e in processed
                      if e['actor'].startswith('v2-consumer')]
        self.assertEqual(
            sorted(e['order_id'] for e in v2_handled
                   if e['message_version'] == 2),
            ['v2-%02d' % i for i in range(V2_COUNT)])

        # 3) 队列最终清空，没有消息被遗留或误删到不可达状态。
        from huey.storage import SqliteStorage
        storage = SqliteStorage('orders', filename=self.db_path)
        try:
            self.assertEqual(storage.queue_size(), 0)
        finally:
            storage.close()


if __name__ == '__main__':
    unittest.main()
