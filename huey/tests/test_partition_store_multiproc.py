# 双进程验证：读端与写端是不同 OS 进程，经 SQLite 文件锁/WAL 真实并发。
#
# 覆盖题目要求的三个场景：
#   1. 清理夹在两次分页之间 —— 历史游标明确失效，重开得到新稳定视图；
#   2. 跨分区追加 —— 其他分区高写入不影响当前游标，视图顺序稳定；
#   3. 重复页读回 —— 同一游标重读同一页，结果逐字节一致。
#
# worker 子进程以 `python -I` 运行（隔离模式），仓库根目录通过 --repo 注入，
# 不依赖调用方的当前目录或环境变量。
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))))
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'mp_partition_worker.py')
WAREHOUSE = 'wh-ops'


class TwoProcessPartitionTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(
            prefix='partition-mp-%d-' % os.getpid())
        self.db_path = os.path.join(self.tmp_dir, 'records.db')

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def run_worker(self, *args, expect_rc=0, timeout=30):
        """以独立 OS 进程运行 worker，返回解析后的 JSON 结果。"""
        cmd_args = [str(arg) for arg in args]
        proc = subprocess.run(
            [sys.executable, '-I', WORKER, '--repo', REPO_ROOT, *cmd_args],
            capture_output=True, text=True, timeout=timeout)
        if proc.returncode != expect_rc:
            self.fail('worker %s 退出码 %s（期望 %s）\nstdout:%s\nstderr:%s'
                      % (args[:1], proc.returncode, expect_rc,
                         proc.stdout, proc.stderr))
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            self.fail('worker 输出无法解析: %r\nstderr:%s'
                      % (proc.stdout, proc.stderr))

    def seed(self, spec):
        self.run_worker('seed', self.db_path, WAREHOUSE, json.dumps(spec))

    def open_cursor(self):
        return self.run_worker('open', self.db_path, WAREHOUSE)['token']

    def read_page(self, token, size):
        return self.run_worker('readpage', self.db_path, WAREHOUSE,
                               token, str(size))
    def test_cleanup_between_two_pages(self):
        # 场景 1：清理恰好夹在两次分页之间，游标锚点所在分区被清。
        self.seed({'p1': 20, 'p2': 20, 'p3': 20})

        # 读端进程 A：第一页，锚点落在 p1。
        token = self.open_cursor()
        page1 = self.read_page(token, 8)
        self.assertEqual(len(page1['rows']), 8)
        self.assertTrue(all(r[0] == 'p1' for r in page1['rows']))
        saved_token = page1['next_cursor']
        self.assertIsNotNone(saved_token)

        # 写端进程 B：在两次分页之间清理 p1（真实的独立进程执行）。
        result = self.run_worker('clean', self.db_path, WAREHOUSE, 'p1')
        self.assertEqual(result['deleted'], 20)

        # 读端进程 A'：同一历史游标继续翻页，必须收到明确的游标失效。
        expired = self.run_worker('readpage', self.db_path, WAREHOUSE,
                                  saved_token, '8', expect_rc=3)
        self.assertEqual(expired['error'], 'cursor_expired')
        self.assertIn('p1', expired['message'])

        # 调用方重开游标：新的稳定视图只含 p2/p3，可完整读完不重不漏。
        fresh = self.open_cursor()
        rows = []
        current = fresh
        while True:
            page = self.read_page(current, 8)
            rows.extend((r[0], r[1]) for r in page['rows'])
            if page['next_cursor'] is None:
                break
            current = page['next_cursor']
        self.assertEqual(len(rows), 40)
        self.assertEqual(len(set(rows)), 40)
        self.assertEqual(rows, sorted(rows))
        self.assertTrue(all(p in ('p2', 'p3') for p, _ in rows))

    def test_cross_partition_appends_do_not_disturb_cursor(self):
        # 场景 2：分页期间另一进程持续向其他分区高写入并新建分区。
        self.seed({'p1': 15, 'p2': 15})
        token = self.open_cursor()

        # 写端进程 B：向已有的 p2 大量追加，并新建游标打开时不存在的 p9。
        self.run_worker('append', self.db_path, WAREHOUSE, 'p2', 40)
        self.run_worker('append', self.db_path, WAREHOUSE, 'p9', 25)

        # 旧游标全程不失效、不乱序、不重复；打开时已存在的 30 条一条不少。
        seen, current = [], token
        while True:
            page = self.read_page(current, 6)
            self.assertNotIn('error', page)
            seen.extend((r[0], r[1]) for r in page['rows'])
            if page['next_cursor'] is None:
                break
            current = page['next_cursor']

        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(seen), len(set(seen)))
        # keyset 语义：已可见分区 p2 的追加可能出现在尾部（游标不失效），
        # 但打开时存在的初始 30 条必须完整、顺序稳定。
        baseline = ([('p1', i) for i in range(1, 16)] +
                    [('p2', i) for i in range(1, 16)])
        self.assertTrue(set(baseline).issubset(set(seen)))
        # 打开时存在的初始 30 条必须完整、顺序稳定。
        self.assertEqual(
            sorted(s for p, s in seen if p == 'p2' and s <= 15),
            list(range(1, 16)))
        # 打开后才新建的 p9 被分区集合冻结挡在旧视图之外。
        self.assertFalse(any(p == 'p9' for p, _ in seen))

        # 重开游标：新视图包含全部追加与新分区，跨分区顺序严格稳定。
        fresh_rows = self.run_worker('readall', self.db_path, WAREHOUSE,
                                     '7')
        keys = [(r[0], r[1]) for r in fresh_rows['rows']]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys, sorted(keys))
        self.assertIn(('p9', 1), keys)
        self.assertEqual(len(keys), 15 + 55 + 25)

    def test_repeated_page_readback_under_writes(self):
        # 场景 3：同一游标重读同一页，期间另一进程持续写入，结果逐字节一致。
        self.seed({'p1': 12, 'p2': 12})
        token = self.open_cursor()

        first = self.read_page(token, 5)
        first_cursor = first['next_cursor']

        # 写端进程 B 在两次读取之间高写入（跨 p1、p2 两个分区）。
        self.run_worker('append', self.db_path, WAREHOUSE, 'p1', 30)
        self.run_worker('append', self.db_path, WAREHOUSE, 'p2', 30)

        again = self.read_page(token, 5)
        self.assertEqual(again['rows'], first['rows'])
        self.assertEqual(again['next_cursor'], first_cursor)

        # 再读一页并重复，确认不是只在首页成立；然后用同一游标完整走完：
        # 打开时存在的 24 条初始记录不重不漏不乱序（追加行允许出现在尾部，
        # 但不影响初始视图的稳定性）。
        second = self.read_page(first_cursor, 5)
        second_repeat = self.read_page(first_cursor, 5)
        self.assertEqual(second_repeat['rows'], second['rows'])
        self.assertEqual(second_repeat['next_cursor'], second['next_cursor'])

        current, keys = token, []
        while True:
            page = self.read_page(current, 5)
            keys.extend((r[0], r[1]) for r in page['rows'])
            if page['next_cursor'] is None:
                break
            current = page['next_cursor']
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), len(set(keys)))
        baseline = ([('p1', i) for i in range(1, 13)] +
                    [('p2', i) for i in range(1, 13)])
        self.assertTrue(set(baseline).issubset(set(keys)))
        self.assertEqual(len(keys), 24 + 60)

    def test_worker_processes_survive_contention(self):
        # 附加压力：写端进程在读端分页窗口内反复清理“非锚点”分区，
        # 游标不应失效，锁等待由 busy_timeout 兜底，不出现数据库级错误。
        self.seed({'p1': 10, 'p2': 10, 'p3': 10})
        token = self.open_cursor()
        page1 = self.read_page(token, 10)  # 锚点在 p1 末尾。

        # 独立进程清理 p3，再清理 p2（均为锚点之后的分区）。
        self.run_worker('clean', self.db_path, WAREHOUSE, 'p3')
        self.run_worker('clean', self.db_path, WAREHOUSE, 'p2')

        page2 = self.read_page(page1['next_cursor'], 100)
        self.assertEqual([r[0] for r in page2['rows']], [])
        # p1 已在第一页读完，p2/p3 被清理：视图自然耗尽。
        self.assertIsNone(page2['next_cursor'])


if __name__ == '__main__':
    unittest.main()
