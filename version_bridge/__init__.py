"""新旧消费者版本衔接（version bridge）。

本包在不修改上游 Huey 代码的前提下，为同一任务队列上并行运行的两个业务
代码版本提供可选的迁移衔接：

* v1（旧业务消费者）：投递保持 Huey 原始默认消息格式（裸序列化字节）。
* v2（新业务消费者）：投递在原始字节外多包一层带版本号的二进制信封，
  语义上对应新版多带的一项业务字段（``channel``）。
* :mod:`version_bridge.storage` 提供兼容取件入口：旧消费者只取 v1，
  遇到 v2 专属投递时原样保留，并继续取后续可处理项；切换后新版消费者
  可以读取保留项，同时也能处理旧版投递（回退安全）。
"""

from version_bridge.envelope import ENVELOPE_HEADER_SIZE
from version_bridge.envelope import ENVELOPE_MAGIC
from version_bridge.envelope import VERSION_1
from version_bridge.envelope import VERSION_2
from version_bridge.envelope import VERSION_LATEST
from version_bridge.envelope import MessageEnvelope
from version_bridge.envelope import envelope_version
from version_bridge.envelope import is_envelope
from version_bridge.envelope import unwrap_envelope
from version_bridge.envelope import wrap_envelope
from version_bridge.storage import VersionedSqliteStorage
from version_bridge.bridge import VersionBridgeHuey

__all__ = [
    'ENVELOPE_HEADER_SIZE',
    'ENVELOPE_MAGIC',
    'MessageEnvelope',
    'VERSION_1',
    'VERSION_2',
    'VERSION_LATEST',
    'VersionBridgeHuey',
    'VersionedSqliteStorage',
    'envelope_version',
    'is_envelope',
    'unwrap_envelope',
    'wrap_envelope',
]
