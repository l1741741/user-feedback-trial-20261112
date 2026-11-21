# partition_store 的进程内测试：分页语义、游标稳定性、清理失效与只读约束。
# 真正的双进程场景见 test_partition_store_multiproc.py。
import base64
import shutil
import sqlite3
import tempfile
import threading
import unittest

from huey.contrib.partition_store import CursorExpired
from huey.contrib.partition_store import PartitionWriter
from huey.contrib.partition_store import ReadOnlyPager


class PartitionStoreTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix='partition-store-%d-' % __import__('os').getpid())
        self.db_path = self.tmp_dir + '/records.db'
        self.writer = PartitionWriter(self.db_path)
        self.pager = ReadOnlyPager(self.db_path)
        self.warehouse = 'wh-1'

    def tearDown(self):
        self.pager.close()
        self.writer.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def seed(self, spec):
        for partition, count in spec.items():
            for i in range(1, count + 1):
                self.writer.append(
                    self.warehouse, partition,
                    ('%s-%06d' % (partition, i)).encode('utf-8'))

    def read_all(self, page_size):
        """用一个全新游标读完所有页，返回 (键列表, 负载列表)。"""
        token = self.pager.open_cursor(self.warehouse)
        keys, payloads = [], []
        while True:
            page = self.pager.read_page(token, page_size)
            for partition, seq, payload in page.rows:
                keys.append((partition, seq))
                payloads.append(payload)
            if page.next_cursor is None:
                return keys, payloads
            token = page.next_cursor


class TestPagingSemantics(PartitionStoreTestBase):
    def test_empty_warehouse(self):
        token = self.pager.open_cursor('does-not-exist')
        page = self.pager.read_page(token, 10)
        self.assertEqual(page.rows, [])
        self.assertIsNone(page.next_cursor)

    def test_stable_order_and_page_boundaries(self):
        self.seed({'p1': 5, 'p2': 4, 'p3': 6})
        token = self.pager.open_cursor(self.warehouse)

        page1 = self.pager.read_page(token, page_size=4)
        self.assertEqual([(p, s) for p, s, _ in page1.rows],
                         [('p1', 1), ('p1', 2), ('p1', 3), ('p1', 4)])
        self.assertIsNotNone(page1.next_cursor)

        page2 = self.pager.read_page(page1.next_cursor, page_size=4)
        self.assertEqual([(p, s) for p, s, _ in page2.rows],
                         [('p1', 5), ('p2', 1), ('p2', 2), ('p2', 3)])

        # 继续翻完，最后一页不足 page_size 且 next_cursor 为 None。
        token = page2.next_cursor
        all_rows = list(page1.rows) + list(page2.rows)
        while True:
            page = self.pager.read_page(token, 4)
            all_rows.extend(page.rows)
            if page.next_cursor is None:
                self.assertLessEqual(len(page.rows), 4)
                break
            token = page.next_cursor

        keys = [(p, s) for p, s, _ in all_rows]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), 15)
        self.assertEqual(len(set(keys)), 15)
        # 负载原样读回。
        self.assertEqual(all_rows[0][2], b'p1-000001')
        self.assertEqual(all_rows[-1][2], b'p3-000006')

    def test_full_pages_until_exhausted(self):
        # 总数恰好是页大小整数倍时，不能多吐一个空页也不能提前结束。
        self.seed({'p1': 6, 'p2': 6})
        keys, _ = self.read_all(page_size=4)
        self.assertEqual(len(keys), 12)
        self.assertEqual(keys, sorted(keys))

    def test_repeated_page_read_is_identical(self):
        self.seed({'p1': 6, 'p2': 6})
        token = self.pager.open_cursor(self.warehouse)
        first = self.pager.read_page(token, 4)

        # 两次分页之间其他分区高写入：游标位置不变，重读结果必须逐行一致。
        for i in range(50):
            self.writer.append(self.warehouse, 'p2',
                               ('extra-%d' % i).encode('utf-8'))
        again = self.pager.read_page(token, 4)
        self.assertEqual(
            [(p, s, bytes(x)) for p, s, x in again.rows],
            [(p, s, bytes(x)) for p, s, x in first.rows])
        self.assertEqual(again.next_cursor, first.next_cursor)

    def test_concurrent_appends_keep_order(self):
        # 另一线程持续向“游标之后”的分区追加，分页过程不能报错、不能乱序。
        self.seed({'p1': 20, 'p2': 20})
        token = self.pager.open_cursor(self.warehouse)

        stop = threading.Event()

        def writer_loop():
            i = 0
            while not stop.is_set():
                self.writer.append(self.warehouse, 'p2',
                                   ('bg-%d' % i).encode('utf-8'))
                i += 1

        thread = threading.Thread(target=writer_loop)
        thread.start()
        try:
            seen = []
            while True:
                page = self.pager.read_page(token, 7)
                seen.extend((p, s) for p, s, _ in page.rows)
                if page.next_cursor is None:
                    break
                token = page.next_cursor
        finally:
            stop.set()
            thread.join()

        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(seen), len(set(seen)))
        # 初始 40 条全部在视图内且顺序正确。
        baseline = ([('p1', i) for i in range(1, 21)] +
                    [('p2', i) for i in range(1, 21)])
        self.assertTrue(set(baseline).issubset(set(seen)))

    def test_new_partition_not_visible_to_old_cursor(self):
        # 打开游标后才新建的分区被分区集合冻结挡在旧视图之外；
        # 已可见分区的追加走 keyset 语义（见其他用例），不会失效游标。
        self.seed({'p1': 3})
        token = self.pager.open_cursor(self.warehouse)
        self.writer.append(self.warehouse, 'p0-new', b'latecomer')
        page = self.pager.read_page(token, 10)
        self.assertEqual([p for p, _, _ in page.rows], ['p1'] * 3)
        self.assertIsNone(page.next_cursor)

        # 重开游标后新分区出现在新的稳定视图里（按分区名排序在最前）。
        fresh = self.pager.open_cursor(self.warehouse)
        page = self.pager.read_page(fresh, 10)
        self.assertEqual(page.rows[0][0], 'p0-new')

    def test_appends_to_visible_partition_keep_cursor_valid(self):
        # 已可见分区的尾部追加不失效游标：初始行稳定，追加行按 keyset 接续。
        self.seed({'p1': 4})
        token = self.pager.open_cursor(self.warehouse)
        page1 = self.pager.read_page(token, 2)
        self.assertEqual([s for _, s, _ in page1.rows], [1, 2])

        for i in range(3):
            self.writer.append(self.warehouse, 'p1',
                               ('tail-%d' % i).encode('utf-8'))
        page2 = self.pager.read_page(page1.next_cursor, 10)
        self.assertEqual([s for _, s, _ in page2.rows], [3, 4, 5, 6, 7])
        self.assertIsNone(page2.next_cursor)


class TestCleanupAndExpiry(PartitionStoreTestBase):
    def test_clean_anchor_partition_expires_cursor(self):
        self.seed({'p1': 10, 'p2': 10})
        token = self.pager.open_cursor(self.warehouse)
        page1 = self.pager.read_page(token, 4)  # 锚点落在 p1。
        self.assertEqual(page1.rows[-1][0], 'p1')

        deleted = self.writer.clean_partition(self.warehouse, 'p1')
        self.assertEqual(deleted, 10)

        with self.assertRaises(CursorExpired):
            self.pager.read_page(page1.next_cursor, 4)

        # 重开游标得到新的稳定视图：只剩 p2，可完整读完。
        fresh = self.pager.open_cursor(self.warehouse)
        page = self.pager.read_page(fresh, 100)
        self.assertEqual([p for p, _, _ in page.rows], ['p2'] * 10)
        self.assertIsNone(page.next_cursor)

    def test_clean_other_partition_does_not_expire(self):
        # 清理游标之后才会读到的分区：锚点仍有效，被清理分区直接跳过。
        self.seed({'p1': 4, 'p2': 8, 'p3': 4})
        token = self.pager.open_cursor(self.warehouse)
        page1 = self.pager.read_page(token, 4)  # 锚点 (p1, 4)。
        self.writer.clean_partition(self.warehouse, 'p2')

        page2 = self.pager.read_page(page1.next_cursor, 100)
        self.assertEqual([p for p, _, _ in page2.rows], ['p3'] * 4)
        self.assertIsNone(page2.next_cursor)

    def test_clean_partition_behind_anchor_skipped(self):
        # 游标已经越过 p1（锚点落在 p2）；此时再清理身后的 p1 不影响翻页。
        self.seed({'p1': 4, 'p2': 4, 'p3': 2})
        token = self.pager.open_cursor(self.warehouse)
        page1 = self.pager.read_page(token, 5)
        self.assertEqual(page1.rows[-1][0], 'p2')
        self.writer.clean_partition(self.warehouse, 'p1')
        page2 = self.pager.read_page(page1.next_cursor, 10)
        keys = [(p, s) for p, s, _ in page2.rows]
        self.assertEqual(keys,
                         [('p2', 2), ('p2', 3), ('p2', 4),
                          ('p3', 1), ('p3', 2)])

    def test_drop_and_recreate_partition_expires_cursor(self):
        self.seed({'p1': 6})
        token = self.pager.open_cursor(self.warehouse)
        page1 = self.pager.read_page(token, 3)
        self.writer.drop_partition(self.warehouse, 'p1')
        with self.assertRaises(CursorExpired):
            self.pager.read_page(page1.next_cursor, 3)

        # 同名分区重新登记后 generation 已推进，旧游标依旧失效。
        self.writer.reset_partition(self.warehouse, 'p1')
        self.writer.append(self.warehouse, 'p1', b'reborn')
        with self.assertRaises(CursorExpired):
            self.pager.read_page(page1.next_cursor, 3)

        fresh = self.pager.open_cursor(self.warehouse)
        page = self.pager.read_page(fresh, 10)
        self.assertEqual([(p, s) for p, s, _ in page.rows],
                         [('p1', 1)])

    def test_append_revives_dropped_partition_and_expires_cursor(self):
        # drop 后直接 append 复活分区：generation 必须推进，旧游标仍失效，
        # 防止复活后的新数据被旧游标当成原分区继续读取。
        self.seed({'p1': 4, 'p2': 2})
        token = self.pager.open_cursor(self.warehouse)
        page1 = self.pager.read_page(token, 4)  # 锚点 (p1, 4)。
        self.assertEqual(self.writer.drop_partition(self.warehouse, 'p1'), 4)

        # 直接向已 dropped 的分区追加，隐式复活且 generation 推进。
        new_seq = self.writer.append(self.warehouse, 'p1', b'resurrected')
        self.assertEqual(new_seq, 1)
        with self.assertRaises(CursorExpired):
            self.pager.read_page(page1.next_cursor, 10)

        # 新游标看到复活后的干净视图：p1 只有新行，p2 保持原样。
        fresh = self.pager.open_cursor(self.warehouse)
        page = self.pager.read_page(fresh, 10)
        self.assertEqual([(p, s) for p, s, _ in page.rows],
                         [('p1', 1), ('p2', 1), ('p2', 2)])
        self.assertEqual(page.rows[0][2], b'resurrected')

    def test_tampered_cursor_rejected(self):
        good = self.pager.open_cursor(self.warehouse)
        for bad in ('', 'not-a-token', good[:-2] + 'xx',
                    base64.urlsafe_b64encode(b'{"v": 99}').decode()):
            with self.assertRaises(CursorExpired):
                self.pager.read_page(bad, 4)

    def test_cursor_from_other_db_path_rejected(self):
        other_dir = tempfile.mkdtemp(prefix='partition-store-other-')
        try:
            other_writer = PartitionWriter(other_dir + '/r.db')
            other_pager = ReadOnlyPager(other_dir + '/r.db')
            # 不同仓库名：本库元数据中查不到该锚点分区，按失效处理。
            for i in range(5):
                other_writer.append('other-warehouse', 'p1',
                                    ('row-%d' % i).encode())
            foreign = other_pager.open_cursor('other-warehouse')
            page = other_pager.read_page(foreign, 4)  # 推进到非首页锚点。
            self.assertTrue(page.next_cursor)
            with self.assertRaises(CursorExpired):
                self.pager.read_page(page.next_cursor, 4)
            other_pager.close()
            other_writer.close()
        finally:
            shutil.rmtree(other_dir, ignore_errors=True)


class TestConnectionConstraints(PartitionStoreTestBase):
    def test_reader_connection_is_query_only(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.pager.conn.execute(
                'INSERT INTO process_record VALUES (?, ?, ?, ?)',
                ('x', 'y', 1, b'z'))

    def test_concurrent_appends_get_unique_seq(self):
        errors = []

        def append_many(partition, n):
            try:
                for i in range(n):
                    self.writer.append(self.warehouse, partition,
                                       ('%s-%d' % (partition, i)).encode())
            except Exception as exc:  # pragma: no cover - 失败时暴露
                errors.append(exc)

        threads = [threading.Thread(target=append_many,
                                    args=('p%d' % t, 30))
                   for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        keys, _ = self.read_all(page_size=16)
        self.assertEqual(len(keys), 120)
        self.assertEqual(len(set(keys)), 120)

    def test_bad_page_size(self):
        token = self.pager.open_cursor(self.warehouse)
        with self.assertRaises(ValueError):
            self.pager.read_page(token, 0)


if __name__ == '__main__':
    unittest.main()
