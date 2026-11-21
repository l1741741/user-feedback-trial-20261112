"""v2（新版）业务逻辑：process_order 增加 channel 业务字段。

任务类在 :mod:`version_bridge.demo.common` 中与 v1 同名同模块注册，
差异只在执行函数多处理一项 channel 字段。
"""


def execute_v2(order_id, channel):
    # 新版多处理一项业务字段：下单渠道。
    return {'processed_by': 'v2', 'order_id': order_id,
            'channel': channel}


def bind_v2_tasks(huey):
    from version_bridge.demo.common import define_process_order
    return define_process_order(huey, execute_v2)
