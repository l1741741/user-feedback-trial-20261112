"""v1（旧版）业务逻辑：process_order 只接收 order_id。

任务类本身在 :mod:`version_bridge.demo.common` 中统一定义，保证两个
代码版本的 Huey 注册键一致；这里只提供旧版的执行函数与绑定入口。
"""


def execute_v1(order_id):
    # 旧版只认识 order_id 一个业务字段。
    return {'processed_by': 'v1', 'order_id': order_id}


def bind_v1_tasks(huey):
    from version_bridge.demo.common import define_process_order
    return define_process_order(huey, execute_v1)
