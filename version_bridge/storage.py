"""带版本感知的 SQLite 存储，提供旧消费者的兼容取件入口。

队列表结构完全复用 Huey 的 ``SqliteStorage``（``task(queue, data, priority)``），
区别只在存取策略：

* :meth:`VersionedSqliteStorage.enqueue` 支持在入队时标注业务版本，v2
  消息以信封字节存储，v1 消息保持 Huey 原始默认字节。
* :meth:`VersionedSqliteStorage.dequeue_accepted` 是兼容入口：在一条
  ``BEGIN EXCLUSIVE`` 事务内按 Huey 原生顺序（priority 降序、id 升序）
  扫描，跳过当前消费者不接受的版本（跳过项原样保留），原子删除并返回
  第一条可处理消息。
* 父类原生 :meth:`dequeue` 行为不变（不看版本、取队首），保证本存储对
  既有 Huey 代码仍是可直接替换的 drop-in 实现。
"""

from collections import namedtuple

from huey.storage import SqliteStorage

from version_bridge.envelope import ENVELOPE_HEADER_SIZE
from version_bridge.envelope import VERSION_1
from version_bridge.envelope import envelope_version
from version_bridge.envelope import is_envelope
from version_bridge.envelope import wrap_envelope


# 兼容取件结果：data 为原始行字节（信封消息仍带信封），version 为业务
# 版本号，skipped 为本次扫描中被跳过的不可处理消息条数。
CompatDequeue = namedtuple('CompatDequeue', ('data', 'version', 'skipped'))

# 单次兼容取件最多扫描多少行，避免队列头部堆积大量不可处理消息时，
# 一次轮询长时间持锁。被截断不影响正确性：下一轮询继续从队首扫描。
DEFAULT_SCAN_LIMIT = 1000


class VersionedSqliteStorage(SqliteStorage):
    def __init__(self, *args, default_scan_limit=DEFAULT_SCAN_LIMIT, **kwargs):
        self.default_scan_limit = default_scan_limit
        super(VersionedSqliteStorage, self).__init__(*args, **kwargs)

    def enqueue(self, data, priority=None, version=None):
        """入队一条消息。

        ``version`` 为 None 或 1 时按 Huey 原始默认格式入队；为 2（或更高）
        时先包一层版本信封。不传 ``version`` 时与父类签名完全兼容。
        """
        if version is not None and version != VERSION_1:
            data = wrap_envelope(data, version=version)
        super(VersionedSqliteStorage, self).enqueue(data, priority=priority)

    def dequeue_accepted(self, accepted_versions, scan_limit=None):
        """兼容取件入口：只取并删除接受版本中的第一条消息。

        :param accepted_versions: 可处理的业务版本号集合，如 ``{1}`` 或
            ``{1, 2}``。
        :param scan_limit: 单次扫描行数上限，默认取实例配置。
        :return: :class:`CompatDequeue`；队列中没有可处理消息时
            ``data`` 为 None（被跳过的消息仍保留在队列中）。

        扫描与删除在同一条排他事务内完成，多个消费者进程并发取件时，
        一条消息要么被其中一个消费者原子取走，要么继续保留，不会出现
        两个进程取到同一条、或跳过项被误删的情况。
        """
        accepted = set(accepted_versions)
        if not accepted:
            raise ValueError('accepted_versions must not be empty')
        limit = self.default_scan_limit if scan_limit is None else scan_limit
        name = self.name

        # 与父类 dequeue() 相同的快速预检：队列为空时不申请排他锁。
        if not self.sql('select 1 from task where queue = ? limit 1',
                        (name,), results=True):
            return CompatDequeue(None, None, 0)

        with self.db(commit=True) as curs:
            curs.execute(
                'select id, data from task where queue = ? '
                'order by priority desc, id limit ?', (name, limit))
            rows = curs.fetchall()
            skipped = 0
            for task_id, data in rows:
                raw = bytes(data)
                version = envelope_version(raw)
                if version not in accepted:
                    skipped += 1
                    continue
                curs.execute('delete from task where id = ?', (task_id,))
                if curs.rowcount == 1:
                    return CompatDequeue(raw, version, skipped)
                # 行已被并发事务删除（SQLite 排他锁下理论上不会发生），
                # 继续看下一条可处理项。
            return CompatDequeue(None, None, skipped)

    def enqueued_versions(self, limit=None):
        """非破坏性列出队列中消息的（行 id, 业务版本号），保持取件顺序。"""
        sql = ('select id, data from task where queue = ? '
               'order by priority desc, id')
        params = [self.name]
        if limit is not None:
            sql += ' limit ?'
            params.append(limit)
        return [(task_id, envelope_version(bytes(data)))
                for task_id, data in self.sql(sql, tuple(params), results=True)]

    def count_by_version(self):
        """按业务版本号统计队列积压，返回 ``{version: count}``。"""
        counts = {}
        for _, version in self.enqueued_versions():
            counts[version] = counts.get(version, 0) + 1
        return counts

    def is_envelope_message(self, data):
        """供上层判断原始行字节是否为信封消息。"""
        return is_envelope(data)

    def header_overhead(self):
        """v2 信封相对原始格式多出的头部字节数，供文档/测试引用。"""
        return ENVELOPE_HEADER_SIZE
