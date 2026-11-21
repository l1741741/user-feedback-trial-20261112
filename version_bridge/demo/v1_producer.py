"""v1 写入进程：用未改造的 stock Huey 投递原始默认格式消息。

用法::

    python -m version_bridge.demo.v1_producer --db queue.db --log events.log \
        --order o-1 --order o-2
"""

from version_bridge.demo.common import append_event
from version_bridge.demo.common import common_parser
from version_bridge.demo.common import ensure_parent
from version_bridge.demo.common import make_v1_huey
from version_bridge.demo.v1_tasks import bind_v1_tasks


def main(argv=None):
    parser = common_parser('v1 写入进程（原始默认消息格式）')
    parser.add_argument('--order', action='append', required=True,
                        help='订单号，可重复传入')
    args = parser.parse_args(argv)
    ensure_parent(args.db)

    huey = make_v1_huey(args.db)
    process_order = bind_v1_tasks(huey)

    for order_id in args.order:
        # 先构造任务对象再投递，便于记录任务 id（results=False 时
        # enqueue() 不返回结果句柄）。
        task = process_order.s(order_id)
        huey.enqueue(task)
        append_event(args.log, {
            'actor': 'v1-producer',
            'event': 'enqueued',
            'format': 'huey-default-v1',
            'order_id': order_id,
            'task_id': task.id,
        })
    print('v1-producer: enqueued %d order(s) in Huey default format'
          % len(args.order))


if __name__ == '__main__':
    main()
