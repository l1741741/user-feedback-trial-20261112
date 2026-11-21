"""v2 取件进程（新业务消费者）：同时处理 v1 与 v2，旧消息经迁移钩子补齐字段。

取到 v1 保留项时，``migrator`` 给任务补上新版业务字段 ``channel`` 的
默认值，使 v2 业务函数始终收到完整参数；取到 v2 时直接处理。用法::

    python -m version_bridge.demo.v2_consumer --db queue.db --log events.log \
        [--max-wait 5.0] [--interval 0.05]
"""

import time

from version_bridge.bridge import VersionBridgeHuey
from version_bridge.demo.common import DEFAULT_CHANNEL
from version_bridge.demo.common import append_event
from version_bridge.demo.common import common_parser
from version_bridge.demo.envelope_constants import VERSION_1
from version_bridge.demo.envelope_constants import VERSION_2
from version_bridge.demo.v2_tasks import bind_v2_tasks


def make_migrator(log_path):
    """迁移钩子：把旧版任务就地升级到新版任务的参数形态。"""

    def migrate(task, from_version):
        # v1 任务只带 order_id（args 长度 1，kwargs 为空），v2 执行函数
        # 第二个位置参数 channel 在旧消息中缺失，追加默认值补齐。
        if len(task.args) < 2 and 'channel' not in task.kwargs:
            task.args = task.args + (DEFAULT_CHANNEL,)
        else:
            task.kwargs = dict(task.kwargs)
            task.kwargs.setdefault('channel', DEFAULT_CHANNEL)
        append_event(log_path, {
            'actor': 'v2-consumer',
            'event': 'migrated',
            'from_version': from_version,
            'to_version': VERSION_2,
            'order_id': task.args[0],
            'filled_default': {'channel': DEFAULT_CHANNEL},
            'task_id': task.id,
        })

    return migrate


def channel_of(task):
    """channel 可能以位置参数或关键字参数传递，两种形态都能取到。"""
    if len(task.args) >= 2:
        return task.args[1]
    return task.kwargs.get('channel')


def main(argv=None):
    parser = common_parser('v2 取件进程（处理 v1/v2，v1 经迁移补齐字段）')
    parser.add_argument('--max-wait', type=float, default=5.0,
                        help='连续空闲多久后退出（秒）')
    parser.add_argument('--interval', type=float, default=0.05,
                        help='轮询间隔（秒）')
    args = parser.parse_args(argv)

    huey = VersionBridgeHuey('orders', filename=args.db, results=False,
                             utc=False)
    bind_v2_tasks(huey)
    migrator = make_migrator(args.log)

    deadline = None
    processed = 0
    while True:
        # 新版声明同时接受两种版本：升级后可以读 v1 消费者保留的积压，
        # 回退场景也不会因格式问题停摆。
        dequeued = huey.dequeue_accepted({VERSION_1, VERSION_2},
                                         migrator=migrator)
        if dequeued.task is None:
            if deadline is None:
                deadline = time.monotonic() + args.max_wait
            elif time.monotonic() >= deadline:
                break
            time.sleep(args.interval)
            continue

        deadline = None
        task = dequeued.task
        value = huey.execute(task)
        processed += 1
        append_event(args.log, {
            'actor': 'v2-consumer',
            'event': 'processed',
            'order_id': task.args[0],
            'channel': channel_of(task),
            'message_version': dequeued.version,
            'result': value,
            'task_id': task.id,
        })

    append_event(args.log, {
        'actor': 'v2-consumer',
        'event': 'exit',
        'processed': processed,
        'backlog': huey.backlog_counts(),
    })
    print('v2-consumer: processed %d message(s) (v1 retained items and v2)'
          % processed)


if __name__ == '__main__':
    main()
