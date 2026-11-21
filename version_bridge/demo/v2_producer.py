"""v2 写入进程：投递带版本信封、含 channel 业务字段的新格式消息。

用法::

    python -m version_bridge.demo.v2_producer --db queue.db --log events.log \
        --order o-3 --channel web --order o-4 --channel mobile
"""

from version_bridge.demo.common import append_event
from version_bridge.demo.common import common_parser
from version_bridge.demo.common import ensure_parent
from version_bridge.demo.common import make_v2_huey
from version_bridge.demo.envelope_constants import VERSION_2
from version_bridge.demo.v2_tasks import bind_v2_tasks


def main(argv=None):
    parser = common_parser('v2 写入进程（版本信封 + channel 字段）')
    parser.add_argument('--order', action='append', required=True,
                        help='订单号，可重复传入')
    parser.add_argument('--channel', action='append', required=True,
                        help='与 --order 一一对应的下单渠道')
    args = parser.parse_args(argv)
    if len(args.order) != len(args.channel):
        parser.error('--order 与 --channel 数量必须一致')
    ensure_parent(args.db)

    huey = make_v2_huey(args.db)
    process_order = bind_v2_tasks(huey)

    for order_id, channel in zip(args.order, args.channel):
        task = process_order.s(order_id, channel)
        # 显式按 v2 投递：存储层在 Huey 原始字节外包版本信封。
        huey.enqueue_versioned(task, version=VERSION_2)
        append_event(args.log, {
            'actor': 'v2-producer',
            'event': 'enqueued',
            'format': 'version-envelope-v2',
            'order_id': order_id,
            'channel': channel,
            'task_id': task.id,
        })
    print('v2-producer: enqueued %d order(s) in version-2 envelope format'
          % len(args.order))


if __name__ == '__main__':
    main()
