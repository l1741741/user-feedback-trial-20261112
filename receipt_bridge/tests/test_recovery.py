"""跨库登记的崩溃恢复测试。

用独立子进程分别在发布前、写账前退出（os._exit 模拟崩溃），再由新进程
执行恢复，最后在父进程复核队列库与财务库的记录。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from huey.serializer import Serializer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
QUEUE_NAME = 'receipt-bridge'


class ReceiptBridgeRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='receipt-bridge-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.queue_db = os.path.join(self.tmp, 'queue.db')
        self.ledger_db = os.path.join(self.tmp, 'ledger.db')
        self.env = dict(os.environ)
        self.env['RECEIPT_BRIDGE_QUEUE_DB'] = self.queue_db
        self.env['RECEIPT_BRIDGE_LEDGER_DB'] = self.ledger_db
        self.env['PYTHONPATH'] = os.pathsep.join(
            [REPO_ROOT, self.env.get('PYTHONPATH', '')])
        self.serializer = Serializer()

    # -- 子进程 --

    def run_worker(self, batch_id, jobs, stage, consume=0):
        jobs_path = os.path.join(self.tmp, '%s-jobs.json' % batch_id)
        with open(jobs_path, 'w') as fh:
            json.dump(jobs, fh)
        return subprocess.run(
            [sys.executable, '-m', 'receipt_bridge.crash_worker',
             jobs_path, batch_id, stage, str(consume)],
            cwd=REPO_ROOT, env=self.env, capture_output=True, text=True)

    def run_recovery(self):
        proc = subprocess.run(
            [sys.executable, '-m', 'receipt_bridge.recover'],
            cwd=REPO_ROOT, env=self.env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    # -- 复核两个库 --

    def fetch_one(self, db, sql, params=()):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def fetch_batch(self, batch_id):
        return self.fetch_one(
            self.ledger_db,
            'select * from batch_request where batch_id = ?', (batch_id,))

    def fetch_ledger(self, batch_id):
        return self.fetch_one(
            self.ledger_db,
            'select * from ledger_entry where batch_id = ?', (batch_id,))

    def ledger_entry_count(self):
        row = self.fetch_one(
            self.ledger_db, 'select count(*) as n from ledger_entry')
        return row['n']

    def queued_messages(self):
        conn = sqlite3.connect(self.queue_db)
        try:
            rows = conn.execute(
                'select data from task where queue = ? order by id',
                (QUEUE_NAME,)).fetchall()
        finally:
            conn.close()
        return [self.serializer.deserialize(bytes(data)) for data, in rows]

    def stored_results(self):
        conn = sqlite3.connect(self.queue_db)
        try:
            rows = conn.execute(
                'select key, value from kv where queue = ?',
                (QUEUE_NAME,)).fetchall()
        finally:
            conn.close()
        return {key: self.serializer.deserialize(bytes(value))
                for key, value in rows}

    # -- 测试 --

    def test_crash_before_publish_recovers_by_republish(self):
        jobs = [{'job_id': 'a1', 'amount': 10},
                {'job_id': 'a2', 'amount': 20}]
        proc = self.run_worker('batch-a', jobs, 'before_publish')
        self.assertNotEqual(proc.returncode, 0)

        # 崩溃后：财务库只有批次请求，队列为空，无账务记录。
        batch = self.fetch_batch('batch-a')
        self.assertEqual(batch['status'], 'pending')
        self.assertIsNone(batch['task_ids'])
        self.assertEqual(self.queued_messages(), [])
        self.assertIsNone(self.fetch_ledger('batch-a'))

        summary = self.run_recovery()
        self.assertEqual(summary['republished'], ['batch-a'])
        self.assertEqual(summary['recorded'], ['batch-a'])
        self.assertEqual(summary['conflict'], [])
        self.assertEqual(summary['errors'], [])

        # 恢复后：按批次请求重新发布并补登记，每个作业在队列中恰好一次。
        batch = self.fetch_batch('batch-a')
        self.assertEqual(batch['status'], 'recorded')
        registered = json.loads(batch['task_ids'])
        self.assertEqual(len(registered), 2)
        messages = self.queued_messages()
        self.assertEqual(sorted(m.args[1] for m in messages), ['a1', 'a2'])
        self.assertEqual(sorted(m.id for m in messages), sorted(registered))

        ledger = self.fetch_ledger('batch-a')
        self.assertEqual(json.loads(ledger['task_ids']), registered)
        self.assertEqual(ledger['published_count'], 2)
        self.assertEqual(ledger['consumed_count'], 0)
        self.assertEqual(ledger['pending_count'], 2)

    def test_crash_before_ledger_with_consumed_jobs(self):
        jobs = [{'job_id': 'b1', 'amount': 10},
                {'job_id': 'b2', 'amount': 20},
                {'job_id': 'b3', 'amount': 30}]
        proc = self.run_worker('batch-b', jobs, 'before_ledger', consume=1)
        self.assertNotEqual(proc.returncode, 0)

        # 崩溃后：已补登记，1 个作业已消费（结果在队列库），无账务记录。
        batch = self.fetch_batch('batch-b')
        self.assertEqual(batch['status'], 'published')
        registered = json.loads(batch['task_ids'])
        self.assertEqual(len(registered), 3)
        queued_before = self.queued_messages()
        self.assertEqual(len(queued_before), 2)
        results_before = self.stored_results()
        self.assertEqual(len(results_before), 1)
        self.assertIsNone(self.fetch_ledger('batch-b'))

        summary = self.run_recovery()
        self.assertEqual(summary['republished'], [])  # 不重复发布
        self.assertEqual(summary['recorded'], ['batch-b'])
        self.assertEqual(summary['errors'], [])

        # 恢复须找到原任务 ID 并补记一次。
        ledger = self.fetch_ledger('batch-b')
        self.assertEqual(json.loads(ledger['task_ids']), registered)
        self.assertEqual(ledger['published_count'], 3)
        self.assertEqual(ledger['consumed_count'], 1)
        self.assertEqual(ledger['pending_count'], 2)
        self.assertEqual(self.fetch_batch('batch-b')['status'], 'recorded')

        # 队列与结果保持原样：没有重复发布，已消费结果未丢失。
        self.assertEqual([m.id for m in self.queued_messages()],
                         [m.id for m in queued_before])
        self.assertEqual(self.stored_results(), results_before)

        # 恢复幂等：再次恢复不重复记帐。
        summary = self.run_recovery()
        self.assertEqual(summary['recorded'], [])
        self.assertEqual(summary['errors'], [])
        self.assertEqual(self.ledger_entry_count(), 1)
        self.assertEqual(json.loads(self.fetch_ledger('batch-b')['task_ids']),
                         registered)

    def test_conflict_batch_kept_and_others_recover(self):
        jobs_c = [{'job_id': 'c1', 'amount': 5}]
        jobs_d = [{'job_id': 'd1', 'amount': 6},
                  {'job_id': 'd2', 'amount': 7}]
        self.assertNotEqual(
            self.run_worker('batch-c', jobs_c, 'before_ledger').returncode, 0)
        self.assertNotEqual(
            self.run_worker('batch-d', jobs_d, 'before_publish').returncode, 0)

        # 冲突：财务库已存在 batch-c 的原账务结果（如人工调整）。
        conn = sqlite3.connect(self.ledger_db)
        conn.execute(
            'insert into ledger_entry (batch_id, task_ids, published_count,'
            ' consumed_count, pending_count, detail, created_at)'
            " values ('batch-c', '[\"manual-task-id\"]', 1, 0, 1,"
            " '人工调整', '2026-11-22T00:00:00+00:00')")
        conn.commit()
        conn.close()

        summary = self.run_recovery()
        self.assertEqual(summary['conflict'], ['batch-c'])
        self.assertEqual(summary['republished'], ['batch-d'])
        self.assertEqual(summary['recorded'], ['batch-d'])
        self.assertEqual(summary['errors'], [])

        # 冲突批次保留原结果。
        ledger_c = self.fetch_ledger('batch-c')
        self.assertEqual(ledger_c['detail'], '人工调整')
        self.assertEqual(json.loads(ledger_c['task_ids']), ['manual-task-id'])
        self.assertEqual(self.fetch_batch('batch-c')['status'], 'conflict')

        # 其他待办继续恢复：batch-d 重新发布并补记一次。
        batch_d = self.fetch_batch('batch-d')
        self.assertEqual(batch_d['status'], 'recorded')
        registered_d = json.loads(batch_d['task_ids'])
        ledger_d = self.fetch_ledger('batch-d')
        self.assertEqual(json.loads(ledger_d['task_ids']), registered_d)
        self.assertEqual(ledger_d['pending_count'], 2)

        # 队列复核：batch-c 的 1 个作业 + batch-d 的 2 个作业，各一次。
        messages = self.queued_messages()
        self.assertEqual(
            sorted((m.args[0], m.args[1]) for m in messages),
            [('batch-c', 'c1'), ('batch-d', 'd1'), ('batch-d', 'd2')])

    def test_happy_path_worker_completes(self):
        jobs = [{'job_id': 'e1', 'amount': 1}]
        proc = self.run_worker('batch-e', jobs, 'none')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.fetch_batch('batch-e')['status'], 'recorded')
        self.assertIsNotNone(self.fetch_ledger('batch-e'))
        # 无待办，恢复为空操作。
        summary = self.run_recovery()
        self.assertEqual(summary,
                         {'republished': [], 'recorded': [], 'conflict': [],
                          'errors': []})


if __name__ == '__main__':
    unittest.main()
