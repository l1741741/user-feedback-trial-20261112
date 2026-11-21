# 双进程验证的 worker 入口。
#
# 由 test_partition_store_multiproc.py 通过 subprocess 以独立 OS 进程启动，
# 自身不做任何断言，只把结果写成 JSON。读端与写端是不同的 Python 进程，
# 真正经过 SQLite 的文件锁 / WAL 并发路径。
#
# 用 `python -I` 运行；通过 --repo 把仓库根目录注入 sys.path，避免依赖
# 当前工作目录或环境变量。
"""
subcommands:
  seed     DB WAREHOUSE SPEC_JSON     按 {"partition": count} 造数
  append   DB WAREHOUSE PARTITION N   追加 N 条负载
  clean    DB WAREHOUSE PARTITION     清理分区，输出删除行数
  drop     DB WAREHOUSE PARTITION     删除分区
  readpage DB WAREHOUSE TOKEN SIZE    读一页，输出 {rows, next_cursor, error}
  open     DB WAREHOUSE               输出新游标令牌
  readall  DB WAREHOUSE SIZE          独立打开游标读完全部页
"""
import argparse
import base64
import json
import sys


def _import_partition_store(repo):
    if repo:
        sys.path.insert(0, repo)
    from huey.contrib.partition_store import CursorExpired  # noqa: F401
    from huey.contrib.partition_store import PartitionWriter
    from huey.contrib.partition_store import ReadOnlyPager
    return PartitionWriter, ReadOnlyPager, CursorExpired


def _payload(partition, seq):
    return base64.b64encode(
        ('%s|%08d' % (partition, seq)).encode('utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', default='')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('seed')
    p.add_argument('db'); p.add_argument('warehouse')
    p.add_argument('spec_json')

    p = sub.add_parser('append')
    p.add_argument('db'); p.add_argument('warehouse')
    p.add_argument('partition'); p.add_argument('count', type=int)

    p = sub.add_parser('clean')
    p.add_argument('db'); p.add_argument('warehouse'); p.add_argument('partition')

    p = sub.add_parser('drop')
    p.add_argument('db'); p.add_argument('warehouse'); p.add_argument('partition')

    p = sub.add_parser('readpage')
    p.add_argument('db'); p.add_argument('warehouse')
    p.add_argument('token'); p.add_argument('size', type=int)

    p = sub.add_parser('open')
    p.add_argument('db'); p.add_argument('warehouse')

    p = sub.add_parser('readall')
    p.add_argument('db'); p.add_argument('warehouse'); p.add_argument('size', type=int)

    args = parser.parse_args(argv)
    PartitionWriter, ReadOnlyPager, CursorExpired = _import_partition_store(
        args.repo)

    if args.cmd in ('seed', 'append', 'clean', 'drop'):
        writer = PartitionWriter(args.db)
        try:
            if args.cmd == 'seed':
                spec = json.loads(args.spec_json)
                for partition, count in sorted(spec.items()):
                    for seq in range(1, count + 1):
                        writer.append(args.warehouse, partition,
                                      _payload(partition, seq))
                print(json.dumps({'ok': True}))
            elif args.cmd == 'append':
                for seq in range(1, args.count + 1):
                    writer.append(args.warehouse, args.partition,
                                  _payload(args.partition + '+append', seq))
                print(json.dumps({'ok': True, 'count': args.count}))
            elif args.cmd == 'clean':
                deleted = writer.clean_partition(args.warehouse,
                                                 args.partition)
                print(json.dumps({'ok': True, 'deleted': deleted}))
            else:
                deleted = writer.drop_partition(args.warehouse,
                                                args.partition)
                print(json.dumps({'ok': True, 'deleted': deleted}))
        finally:
            writer.close()
        return 0

    pager = ReadOnlyPager(args.db)
    try:
        if args.cmd == 'open':
            print(json.dumps({'token': pager.open_cursor(args.warehouse)}))
        elif args.cmd == 'readpage':
            try:
                page = pager.read_page(args.token, args.size)
                print(json.dumps({
                    'rows': [[p, s, base64.b64encode(payload).decode('ascii')]
                             for p, s, payload in page.rows],
                    'next_cursor': page.next_cursor,
                }))
            except CursorExpired as exc:
                print(json.dumps({'error': 'cursor_expired',
                                  'message': str(exc)}))
                return 3
        else:  # readall
            token = pager.open_cursor(args.warehouse)
            all_rows, pages = [], 0
            while True:
                page = pager.read_page(token, args.size)
                pages += 1
                all_rows.extend([p, s,
                                 base64.b64encode(payload).decode('ascii')]
                                for p, s, payload in page.rows)
                if page.next_cursor is None:
                    break
                token = page.next_cursor
            print(json.dumps({'rows': all_rows, 'pages': pages}))
    finally:
        pager.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
