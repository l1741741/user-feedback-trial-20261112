"""Huey 高层门面：版本化投递 + 兼容取件 + 可选迁移钩子。

``VersionBridgeHuey`` 继承上游 ``Huey``，注册表、任务包装器、序列化器等
机制全部沿用，只增加与版本衔接相关的入口；不调用这些入口时，它与普通
``SqliteHuey`` 行为一致（包括原始默认消息格式）。
"""

from collections import namedtuple

from huey import signals as S
from huey.api import Huey
from huey.api import Result
from huey.api import ResultGroup

from version_bridge.envelope import VERSION_1
from version_bridge.envelope import VERSION_LATEST
from version_bridge.envelope import unwrap_envelope
from version_bridge.storage import VersionedSqliteStorage


# 兼容取件的高层结果：task 为反序列化后的 Huey 任务（可能已过迁移钩子），
# version 为消息的业务版本号，skipped 为本次取件扫描中被跳过保留的条数。
DequeuedTask = namedtuple('DequeuedTask', ('task', 'version', 'skipped'))


class VersionBridgeHuey(Huey):
    storage_class = VersionedSqliteStorage

    def enqueue_versioned(self, task, version=VERSION_LATEST):
        """按指定业务版本投递任务。

        v1 走 Huey 原始默认字节；v2 由存储层包上版本信封。其余逻辑
        （过期时间解析、信号、结果句柄、immediate 模式）与
        :meth:`Huey.enqueue` 保持一致。
        """
        if task.expires:
            task.resolve_expires(self.utc)

        self._emit(S.SIGNAL_ENQUEUED, task)

        if self._immediate:
            self.execute(task)
        else:
            self.storage.enqueue(self.serialize_task(task),
                                 task.priority, version=version)

        if not self.results:
            return

        results = self._pipeline_results(task)
        return ResultGroup(results) if len(results) > 1 else results[0]

    def dequeue_accepted(self, accepted_versions, migrator=None,
                         scan_limit=None):
        """兼容取件入口的高层封装，返回反序列化后的任务。

        :param accepted_versions: 本消费者可处理的业务版本集合。旧版消费
            者传 ``{VERSION_1}``，新版传 ``{VERSION_1, VERSION_2}``。
        :param migrator: 可选迁移钩子 ``migrator(task, from_version)``，
            当取到低于当前最新版本的旧投递时调用，就地补齐新版业务字段
            （如给 v1 消息补默认 ``channel``），使新版代码无需为两种
            格式写分支。
        :return: :class:`DequeuedTask`；没有可处理消息时 ``task`` 为 None，
            被跳过的消息仍保留在队列中。
        """
        result = self.storage.dequeue_accepted(
            accepted_versions, scan_limit=scan_limit)
        if result.data is None:
            return DequeuedTask(None, None, result.skipped)

        envelope = unwrap_envelope(result.data)
        task = self.deserialize_task(envelope.payload)
        if migrator is not None and envelope.version < VERSION_LATEST:
            migrator(task, envelope.version)
        return DequeuedTask(task, envelope.version, result.skipped)

    def backlog_counts(self):
        """按业务版本统计当前积压，例如 ``{1: 3, 2: 5}``。"""
        return self.storage.count_by_version()

    def raw_queue_bytes(self, limit=None):
        """非破坏性读取队列原始行字节（保留信封），供测试核对线上格式。"""
        return [bytes(item) for item in
                self.storage.enqueued_items(limit=limit)]

    def queue_versions(self, limit=None):
        """非破坏性列出队列中消息的（行 id, 业务版本号），保持取件顺序。"""
        return self.storage.enqueued_versions(limit=limit)
