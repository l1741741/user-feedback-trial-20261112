"""demo 子包内统一引用的版本号常量（转自 version_bridge 顶层）。"""

from version_bridge.envelope import VERSION_1
from version_bridge.envelope import VERSION_2
from version_bridge.envelope import VERSION_LATEST

__all__ = ['VERSION_1', 'VERSION_2', 'VERSION_LATEST']
