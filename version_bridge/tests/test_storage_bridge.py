"""兼容存储与桥接门面的单元测试（真实 SQLite 文件，进程内验证）。"""

import os
import shutil
import tempfile
import unittest

from huey.api import SqliteHuey
from huey.storage import SqliteStorage

from version_bridge.bridge import VersionBridgeHuey
from version_bridge.envelope import VERSION_1
from version_bridge.envelope import VERSION_2
from version_bridge.envelope import is_envelope
from version_bridge.storage import VersionedSqliteStorage


class StorageBridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='vbridge-unit-')
        self.db_path = os.path.join(self.workdir, 'queue.db')

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def make_huey(self):
        return VersionBridgeHuey('orders', filename=self.db_path,
                                 results=False, utc=False)

    def bind_tasks(self, huey):
        @huey.task(name='process_order')
        def process_order(order_id, channel='legacy-default'):
            return (order_id, channel)
        return process_order

    # ---- 存储层：版本标注与格式 ------------------------------------------------

    def test_v1_uses_raw_default_bytes_v2_uses_envelope(self):
        storage = VersionedSqliteStorage('orders', filename=self.db_path)
        storage.enqueue(b'raw-bytes-v1')
        storage.enqueue(b'raw-bytes-v2', version=VERSION_2)
        items = [bytes(row) for row in storage.enqueued_items()]
        self.assertEqual(items[0], b'raw-bytes-v1')
        self.assertFalse(is_envelope(items[0]))
        self.assertTrue(is_envelope(items[1]))
        self.assertEqual(storage.count_by_version(), {1: 1, 2: 1})
        storage.close()

    def test_native_dequeue_is_version_agnostic_drop_in(self):
        # 父类原生 dequeue() 不看版本，行为与 stock SqliteStorage 一致。
        storage = VersionedSqliteStorage('orders', filename=self.db_path)
        storage.enqueue(b'v1-a')
        storage.enqueue(b'v2-a', version=VERSION_2)
        self.assertEqual(bytes(storage.dequeue()), b'v1-a')
        self.assertTrue(is_envelope(bytes(storage.dequeue())))
        self.assertIsNone(storage.dequeue())
        storage.close()

    def test_shared_schema_with_stock_sqlite_storage(self):
        # VersionedSqliteStorage 与 stock SqliteStorage 打开同一个库文件，
        # 表结构完全复用，二者可以互读互写。
        stock = SqliteStorage('orders', filename=self.db_path)
        stock.enqueue(b'from-stock')
        stock.close()
        bridge_storage = VersionedSqliteStorage(
            'orders', filename=self.db_path)
        result = bridge_storage.dequeue_accepted({VERSION_1})
        self.assertEqual(bytes(result.data), b'from-stock')
        self.assertEqual(result.version, VERSION_1)
        self.assertEqual(result.skipped, 0)
        bridge_storage.close()

    # ---- 兼容入口：跳过保留、继续取后续 ----------------------------------------

    def test_compat_dequeue_skips_v2_and_keeps_going(self):
        storage = VersionedSqliteStorage('orders', filename=self.db_path)
        # 交错入队：v1, v2, v2, v1。
        storage.enqueue(b'v1-1')
        storage.enqueue(b'v2-1', version=VERSION_2)
        storage.enqueue(b'v2-2', version=VERSION_2)
        storage.enqueue(b'v1-2')

        first = storage.dequeue_accepted({VERSION_1})
        self.assertEqual(bytes(first.data), b'v1-1')
        self.assertEqual(first.skipped, 0)

        # 第二次：跳过两条 v2（保留），取到后面的 v1-2。
        second = storage.dequeue_accepted({VERSION_1})
        self.assertEqual(bytes(second.data), b'v1-2')
        self.assertEqual(second.version, VERSION_1)
        self.assertEqual(second.skipped, 2)

        # 队列中只剩被保留的两条 v2。
        self.assertEqual(storage.count_by_version(), {2: 2})
        empty = storage.dequeue_accepted({VERSION_1})
        self.assertIsNone(empty.data)
        self.assertEqual(empty.skipped, 2)

        # 新版声明接受两种版本：先取到最早的 v2-1。
        upgraded = storage.dequeue_accepted({VERSION_1, VERSION_2})
        self.assertEqual(upgraded.version, VERSION_2)
        self.assertEqual(upgraded.skipped, 0)
        storage.close()

    def test_retained_messages_keep_original_order(self):
        storage = VersionedSqliteStorage('orders', filename=self.db_path)
        for idx in range(3):
            storage.enqueue(('v2-%d' % idx).encode(), version=VERSION_2)
        storage.enqueue(b'v1-tail')
        result = storage.dequeue_accepted({VERSION_1})
        self.assertEqual(bytes(result.data), b'v1-tail')
        versions = storage.enqueued_versions()
        self.assertEqual([v for _, v in versions], [2, 2, 2])
        storage.close()

    def test_scan_limit_bounds_single_poll_but_retains_correctness(self):
        storage = VersionedSqliteStorage(
            'orders', filename=self.db_path, default_scan_limit=2)
        storage.enqueue(b'v2-1', version=VERSION_2)
        storage.enqueue(b'v2-2', version=VERSION_2)
        storage.enqueue(b'v1-1')
        # 首轮只扫前两行，都是 v2，取不到，但消息全部保留。
        first = storage.dequeue_accepted({VERSION_1}, scan_limit=2)
        self.assertIsNone(first.data)
        self.assertEqual(first.skipped, 2)
        self.assertEqual(storage.queue_size(), 3)
        # 下一轮放宽扫描窗口即可取到 v1-1，且 v2 仍保留。
        second = storage.dequeue_accepted({VERSION_1}, scan_limit=10)
        self.assertEqual(bytes(second.data), b'v1-1')
        self.assertEqual(storage.count_by_version(), {2: 2})
        storage.close()

    def test_priority_ordering_matches_native_semantics(self):
        storage = VersionedSqliteStorage('orders', filename=self.db_path)
        storage.enqueue(b'v1-low', priority=1)
        storage.enqueue(b'v2-blocker', priority=10, version=VERSION_2)
        storage.enqueue(b'v1-high', priority=5)
        # 高优先级 v2 被跳过保留后，按 priority desc, id 取到 v1-high。
        result = storage.dequeue_accepted({VERSION_1})
        self.assertEqual(bytes(result.data), b'v1-high')
        self.assertEqual(result.skipped, 1)
        storage.close()

    # ---- 高层门面：端到端任务与迁移钩子 ----------------------------------------

    def test_huey_enqueue_versioned_and_dequeue_accepted(self):
        huey = self.make_huey()
        process_order = self.bind_tasks(huey)

        v1_task = process_order.s('o-1')
        huey.enqueue_versioned(v1_task, version=VERSION_1)
        v2_task = process_order.s('o-2', channel='web')
        huey.enqueue_versioned(v2_task, version=VERSION_2)

        # 旧消费者视角：只拿到 v1。
        old = huey.dequeue_accepted({VERSION_1})
        self.assertEqual(old.version, VERSION_1)
        self.assertEqual(old.task.args, ('o-1',))
        self.assertEqual(huey.backlog_counts(), {2: 1})

        # 新消费者视角：取到 v2，channel 原样保留。
        new = huey.dequeue_accepted({VERSION_1, VERSION_2})
        self.assertEqual(new.version, VERSION_2)
        self.assertEqual(new.task.kwargs, {'channel': 'web'})
        value = huey.execute(new.task)
        self.assertEqual(value, ('o-2', 'web'))

    def test_migrator_fills_new_field_on_old_message(self):
        huey = self.make_huey()
        process_order = self.bind_tasks(huey)
        huey.enqueue_versioned(process_order.s('o-9'), version=VERSION_1)

        calls = []

        def migrator(task, from_version):
            self.assertEqual(from_version, VERSION_1)
            # 与 demo v2 消费者一致：以位置参数补齐 channel 默认值。
            if len(task.args) < 2:
                task.args = task.args + ('legacy-default',)
            calls.append(task.id)

        dequeued = huey.dequeue_accepted(
            {VERSION_1, VERSION_2}, migrator=migrator)
        self.assertEqual(len(calls), 1)
        self.assertEqual(dequeued.task.args,
                         ('o-9', 'legacy-default'))
        self.assertEqual(huey.execute(dequeued.task),
                         ('o-9', 'legacy-default'))

    def test_migrator_not_called_for_current_version(self):
        huey = self.make_huey()
        process_order = self.bind_tasks(huey)
        huey.enqueue_versioned(
            process_order.s('o-9', channel='app'), version=VERSION_2)

        def migrator(task, from_version):
            raise AssertionError('当前版本消息不应触发迁移钩子')

        dequeued = huey.dequeue_accepted(
            {VERSION_1, VERSION_2}, migrator=migrator)
        self.assertEqual(dequeued.version, VERSION_2)

    def test_empty_accepted_versions_rejected(self):
        huey = self.make_huey()
        with self.assertRaises(ValueError):
            huey.dequeue_accepted(set())

    def test_stock_huey_producer_bridge_consumer_interop(self):
        # stock Huey（未接入本包）生产的默认格式消息，桥接门面可直接消费。
        stock = SqliteHuey('orders', filename=self.db_path, results=False,
                           utc=False)

        @stock.task(name='process_order')
        def stock_task(order_id):
            return order_id

        stock_task('s-1')
        stock.storage.close()

        huey = self.make_huey()
        self.bind_tasks(huey)
        raw = huey.raw_queue_bytes()
        self.assertEqual(len(raw), 1)
        self.assertFalse(is_envelope(raw[0]))
        dequeued = huey.dequeue_accepted({VERSION_1, VERSION_2})
        self.assertEqual(dequeued.version, VERSION_1)
        self.assertEqual(dequeued.task.args, ('s-1',))


if __name__ == '__main__':
    unittest.main()
