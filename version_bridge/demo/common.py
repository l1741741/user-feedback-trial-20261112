"""演示场景共享工具：进程入口参数、事件日志与队列构造。

业务背景：订单处理任务 ``process_order``。
* v1 消息只带 ``order_id``；
* v2 消息多带一项业务字段 ``channel``（下单渠道）。

两个代码版本是各自独立的操作系统进程，通过同一个 SQLite 队列文件
交换消息，事件以 JSON Lines 追加写入同一个日志文件，便于驱动脚本
核对"谁写入、谁取走"。
"""

import argparse
import json
import os

from huey.api import SqliteHuey

from version_bridge.bridge import VersionBridgeHuey

QUEUE_NAME = 'orders'
# 旧版任务在两个代码版本中保持同名，模拟同一业务任务的跨版本演进。
TASK_NAME = 'process_order'
# v1 消息没有 channel，新版读取保留的 v1 投递时补此默认值（迁移衔接）。
DEFAULT_CHANNEL = 'legacy-default'


def make_v1_huey(db_path):
    """旧版进程使用未改造的 stock Huey，消息即 Huey 原始默认格式。"""
    return SqliteHuey(QUEUE_NAME, filename=db_path, results=False,
                      utc=False)


def make_v2_huey(db_path):
    """新版进程使用版本桥接门面，v2 投递带版本信封。"""
    return VersionBridgeHuey(QUEUE_NAME, filename=db_path, results=False,
                             utc=False)


def append_event(log_path, event):
    """以 O_APPEND 原子追加一行 JSON（单行远小于 PIPE_BUF）。"""
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + '\n'
    with open(log_path, 'a', encoding='utf-8') as fh:
        fh.write(line)


def common_parser(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--db', required=True, help='共享 SQLite 队列文件路径')
    parser.add_argument('--log', required=True, help='JSON Lines 事件日志路径')
    return parser


def ensure_parent(path):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)


def define_process_order(huey, executor):
    """注册两版本共享的 process_order 任务。

    任务类必须在同一个模块路径下注册（Huey 注册键为
    ``module.name``），这样 v1/v2 两个进程投递的消息才能互相反序列化。
    两个代码版本的差异体现在各自绑定的执行函数签名上：v1 的 executor
    只接收 order_id，v2 的还接收 channel。
    """
    @huey.task(name=TASK_NAME)
    def process_order(*args, **kwargs):
        return executor(*args, **kwargs)

    return process_order
