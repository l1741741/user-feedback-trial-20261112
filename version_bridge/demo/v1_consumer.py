"""v1 取件进程（旧业务消费者）：兼容入口只取 v1，v2 专属投递原样保留。

轮询同一个 SQLite 队列：取到 v1 就执行；队首是 v2 信封消息时跳过并继续
扫描后续可处理项，直到队列中没有任何 v1 消息为止。用法::

    python -m version_bridge.demo.v1_consumer --db queue.db --log events.log \
        [--max-wait 5.0] [--interval 0.05]
"""

import time

from version_bridge.bridge import VersionBridgeHuey
from version_bridge.demo.common import append_event
from version_bridge.demo.common import common_parser
from version_bridge.demo.envelope_constants import VERSION_1
from version_bridge.demo.v1_tasks import bind_v1_tasks


def main(argv=None):
    parser = common_parser('v1 取件进程（只处理 v1，保留 v2）')
    parser.add_argument('--max-wait', type=float, default=5.0,
                        help='连续空闲多久后退出（秒）')
    parser.add_argument('--interval', type=float, default=0.05,
                        help='轮询间隔（秒）')
    args = parser.parse_args(argv)

    # 旧版进程同样基于 VersionedSqliteStorage，但只声明接受 {v1}，
    # 从而获得"跳过并保留 v2"的兼容取件能力。
    huey = VersionBridgeHuey('orders', filename=args.db, results=False,
                             utc=False)
    bind_v1_tasks(huey)

    deadline = None
    processed = 0
    # 同一批保留项在等待后续 v1 期间会被每轮轮询重复扫描，只在保留
    # 集合变化时记录一次，避免事件日志被轮询噪音淹没。
    last_retained = -1
    while True:
        dequeued = huey.dequeue_accepted({VERSION_1})
        if dequeued.task is None:
            if dequeued.skipped and dequeued.skipped != last_retained:
                # 队首有 v2 专属投递被保留；记录后继续等待后续 v1 消息。
                last_retained = dequeued.skipped
                append_event(args.log, {
                    'actor': 'v1-consumer',
                    'event': 'retained-incompatible',
                    'retained_versions': [2] * dequeued.skipped,
                    'skipped': dequeued.skipped,
                })
            if deadline is None:
                deadline = time.monotonic() + args.max_wait
            elif time.monotonic() >= deadline:
                break
            time.sleep(args.interval)
            continue

        deadline = None
        last_retained = -1
        task = dequeued.task
        value = huey.execute(task)
        processed += 1
        append_event(args.log, {
            'actor': 'v1-consumer',
            'event': 'processed',
            'order_id': task.args[0],
            'message_version': dequeued.version,
            'result': value,
            'skipped_before': dequeued.skipped,
            'task_id': task.id,
        })

    append_event(args.log, {
        'actor': 'v1-consumer',
        'event': 'exit',
        'processed': processed,
        'backlog': huey.backlog_counts(),
    })
    print('v1-consumer: processed %d v1 message(s), incompatible v2 '
          'message(s) left in queue' % processed)


if __name__ == '__main__':
    main()
