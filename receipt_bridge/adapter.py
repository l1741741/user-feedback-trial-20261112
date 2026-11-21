"""本地适配器：队列接受作业后向独立财务库登记。

提交流程（两个库分别提交，不跨库事务）：

1. ``save_batch_request()`` —— 先把批次请求存入财务库并提交；
2. ``publish_batch()`` —— 用现有队列逐个发布作业（队列库由 Huey 的
   SqliteStorage 自行提交），再把任务 ID 补登记到财务库并提交；
3. ``write_ledger()`` —— 按登记的任务 ID 写账，同一批次只补记一次。

恢复语义（``recover()``）：

- 发布前退出：批次请求仍在财务库，恢复时按请求重新发布并补登记；
- 写账前退出（部分作业已消费）：从补登记记录找到原任务 ID（登记缺失时
  扫描队列兜底），只补记一次账务，不重复发布、不重复记帐；
- 冲突批次保留原结果，其他待办继续恢复。
"""
import json
import sqlite3
from datetime import datetime, timezone

STATUS_PENDING = 'pending'      # 已保存批次请求，尚未发布
STATUS_PUBLISHED = 'published'  # 已发布并补登记，尚未写账
STATUS_RECORDED = 'recorded'    # 已写账
STATUS_CONFLICT = 'conflict'    # 写账冲突，保留原结果

SCHEMA = (
    'create table if not exists batch_request ('
    ' batch_id text primary key,'
    ' payload text not null,'
    ' status text not null,'
    ' task_ids text,'
    ' created_at text not null,'
    ' updated_at text not null)',
    'create table if not exists ledger_entry ('
    ' batch_id text primary key,'
    ' task_ids text not null,'
    ' published_count integer not null,'
    ' consumed_count integer not null,'
    ' pending_count integer not null,'
    ' detail text,'
    ' created_at text not null)',
)


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


class ReceiptBridge(object):
    """导出发布器与独立财务库之间的本地适配器。"""

    def __init__(self, huey, ledger_path, task):
        self.huey = huey
        self.ledger_path = ledger_path
        self.task = task
        with self._connect() as conn:
            for stmt in SCHEMA:
                conn.execute(stmt)
            conn.commit()

    def _connect(self):
        conn = sqlite3.connect(self.ledger_path)
        conn.row_factory = sqlite3.Row
        return conn

    # -- 查询辅助 --

    def get_batch(self, batch_id):
        with self._connect() as conn:
            row = conn.execute(
                'select * from batch_request where batch_id = ?',
                (batch_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_ledger_entry(self, batch_id):
        with self._connect() as conn:
            row = conn.execute(
                'select * from ledger_entry where batch_id = ?',
                (batch_id,)).fetchone()
        return dict(row) if row is not None else None

    def _require_batch(self, batch_id):
        batch = self.get_batch(batch_id)
        if batch is None:
            raise KeyError('批次不存在：%s' % batch_id)
        return batch

    def _queued_jobs(self, batch_id):
        """扫描队列库，返回该批次仍在排队的作业：{job_id: 原任务 ID}。"""
        queued = {}
        for data in self.huey.storage.enqueued_items():
            task = self.huey.deserialize_task(bytes(data))
            if len(task.args) >= 2 and task.args[0] == batch_id:
                queued[task.args[1]] = task.id
        return queued

    # -- 第一步：保存批次请求（财务库提交） --

    def save_batch_request(self, batch_id, jobs):
        payload = json.dumps({'jobs': list(jobs)}, sort_keys=True)
        now = _utcnow()
        with self._connect() as conn:
            conn.execute(
                'insert into batch_request (batch_id, payload, status,'
                ' created_at, updated_at) values (?, ?, ?, ?, ?)'
                ' on conflict(batch_id) do nothing',
                (batch_id, payload, STATUS_PENDING, now, now))
            conn.commit()
        return self.get_batch(batch_id)

    # -- 第二步：用现有队列发布并补登记（两个库分别提交） --

    def publish_batch(self, batch_id):
        batch = self._require_batch(batch_id)
        if batch['status'] != STATUS_PENDING and batch['task_ids']:
            return json.loads(batch['task_ids'])  # 幂等：已补登记

        jobs = json.loads(batch['payload'])['jobs']
        queued = self._queued_jobs(batch_id)  # 崩溃恢复：队列里已有的作业
        task_ids = []
        for job in jobs:
            job_id = job['job_id']
            if job_id in queued:
                task_ids.append(queued[job_id])  # 找到原任务 ID，不重复发布
                continue
            task = self.task.s(batch_id, job_id, job['amount'])
            self.huey.enqueue(task)  # 队列库提交
            task_ids.append(task.id)

        with self._connect() as conn:  # 补登记，财务库提交
            conn.execute(
                'update batch_request set status = ?, task_ids = ?,'
                ' updated_at = ? where batch_id = ?',
                (STATUS_PUBLISHED, json.dumps(task_ids), _utcnow(), batch_id))
            conn.commit()
        return task_ids

    # -- 第三步：写账（同一批次只补记一次） --

    def _find_task_ids(self, batch):
        """找到原任务 ID：优先补登记记录，缺失时扫描队列兜底。"""
        task_ids = (list(json.loads(batch['task_ids']))
                    if batch['task_ids'] else [])
        if not task_ids:
            # 发布与补登记之间崩溃：扫描队列找回已入队作业的原任务 ID。
            queued = self._queued_jobs(batch['batch_id'])
            task_ids = [queued[job_id] for job_id in sorted(queued)]
        if not task_ids:
            raise ValueError('批次尚未发布，无任务 ID：%s' % batch['batch_id'])
        return task_ids

    def write_ledger(self, batch_id):
        batch = self._require_batch(batch_id)
        task_ids = self._find_task_ids(batch)
        queued_ids = set(self._queued_jobs(batch_id).values())
        pending = [tid for tid in task_ids if tid in queued_ids]
        consumed = [tid for tid in task_ids if tid not in queued_ids]
        now = _utcnow()
        with self._connect() as conn:
            curs = conn.execute(
                'insert into ledger_entry (batch_id, task_ids,'
                ' published_count, consumed_count, pending_count, detail,'
                ' created_at) values (?, ?, ?, ?, ?, ?, ?)'
                ' on conflict(batch_id) do nothing',
                (batch_id, json.dumps(task_ids), len(task_ids),
                 len(consumed), len(pending), 'recorded by receipt-bridge',
                 now))
            if curs.rowcount == 1:
                conn.execute(
                    'update batch_request set status = ?, updated_at = ?'
                    ' where batch_id = ?', (STATUS_RECORDED, now, batch_id))
                conn.commit()
                return STATUS_RECORDED

            existing = conn.execute(
                'select task_ids from ledger_entry where batch_id = ?',
                (batch_id,)).fetchone()
            if json.loads(existing['task_ids']) == task_ids:
                # 本批次此前已记过同一结果：补记一次的幂等保证。
                conn.execute(
                    'update batch_request set status = ?, updated_at = ?'
                    ' where batch_id = ?', (STATUS_RECORDED, now, batch_id))
                conn.commit()
                return STATUS_RECORDED

            # 冲突批次：保留原结果，仅标记状态。
            conn.execute(
                'update batch_request set status = ?, updated_at = ?'
                ' where batch_id = ?', (STATUS_CONFLICT, now, batch_id))
            conn.commit()
            return STATUS_CONFLICT

    # -- 完整提交流程 --

    def submit_batch(self, batch_id, jobs):
        self.save_batch_request(batch_id, jobs)
        task_ids = self.publish_batch(batch_id)
        outcome = self.write_ledger(batch_id)
        return task_ids, outcome

    # -- 恢复 --

    def recover(self):
        """恢复所有待办批次；单批失败或冲突不影响其他批次。"""
        summary = {'republished': [], 'recorded': [], 'conflict': [],
                   'errors': []}
        with self._connect() as conn:
            rows = conn.execute(
                'select batch_id, status from batch_request'
                ' where status in (?, ?) order by created_at, batch_id',
                (STATUS_PENDING, STATUS_PUBLISHED)).fetchall()
        for row in rows:
            batch_id = row['batch_id']
            try:
                if row['status'] == STATUS_PENDING:
                    self.publish_batch(batch_id)
                    summary['republished'].append(batch_id)
                outcome = self.write_ledger(batch_id)
                summary[outcome].append(batch_id)
            except Exception as exc:  # 其他待办继续恢复
                summary['errors'].append(
                    {'batch_id': batch_id, 'error': repr(exc)})
        return summary
