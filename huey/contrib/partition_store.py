# 分区记录仓的只读分页快照。
#
# 业务场景：运维分页查看某仓的处理记录（按分区组织），后台随时可能对分区
# 做清理/追加。本模块只依赖标准库 sqlite3，提供：
#   * PartitionWriter：建表、追加记录、清理/删除分区（会推进分区 generation）；
#   * ReadOnlyPager：只读连接上的 keyset 分页，游标为自包含的不透明令牌，
#     不依赖任何服务端会话，因此两个进程可以通过传递令牌共享同一游标；
#   * CursorExpired：游标锚点所在分区在游标打开后被清理（generation 推进），
#     历史游标无法恢复，调用方应重新 open_cursor() 获取新的稳定视图。
#
# 稳定性保证：
#   1. 每一页在独立的只读事务内读取，WAL 模式下读不阻塞写、写不阻塞读；
#   2. 游标记录打开时刻可见的分区集合，之后新建的分区不会插入到该视图中间；
#   3. 严格 (partition, seq) 升序 keyset 翻页，同一游标重读同一页结果一致，
#      其他分区高写入只可能在视图尾部追加新行，不会移动或重排既有行；
#   4. 已可见分区的追加采用标准 keyset 语义：锚点之后的新行可被后续页读到，
#      游标不会因此失效；打开时已存在的行顺序与完整性保持稳定；
#   5. 仅当锚点分区被清理（generation 推进）导致继续位置不可恢复时，
#      才抛出 CursorExpired。

from collections import namedtuple
import base64
import json
import sqlite3
import threading


CURSOR_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5000


class CursorExpired(Exception):
    """历史游标无法恢复：锚点分区在游标打开后被清理。

    调用方应当放弃该游标，重新调用 :meth:`ReadOnlyPager.open_cursor`
    获取新的游标，并从第一页开始读取新的稳定视图。
    """


# 一页结果：rows 为 (partition, seq, payload) 三元组列表；next_cursor 为
# 下一页的不透明令牌，没有更多数据时为 None。
Page = namedtuple('Page', ('rows', 'next_cursor'))


def _encode_cursor(data):
    raw = json.dumps(data, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii')


def _decode_cursor(token):
    try:
        raw = base64.urlsafe_b64decode(token.encode('ascii'))
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorExpired('游标令牌损坏或无法解析。') from exc
    if not isinstance(data, dict) or data.get('v') != CURSOR_VERSION:
        raise CursorExpired('游标令牌版本不受支持。')
    return data


def _connect(path, read_only, busy_timeout_ms):
    # check_same_thread=False：允许调用方在多线程/多执行区间共享连接；
    # 写侧并发由 BEGIN IMMEDIATE 事务与 SQLite 文件锁串行化。
    # isolation_level=None：关闭 Python sqlite3 的隐式事务，全部事务
    # 边界由本模块显式 BEGIN/COMMIT 控制，避免嵌套事务错误。
    if read_only:
        uri = 'file:%s?mode=ro' % path
        conn = sqlite3.connect(uri, uri=True,
                               timeout=busy_timeout_ms / 1000.0,
                               check_same_thread=False,
                               isolation_level=None)
        # 双保险：即便有人误写 SQL，连接层面也拒绝任何变更。
        conn.execute('PRAGMA query_only = ON')
    else:
        conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000.0,
                               check_same_thread=False,
                               isolation_level=None)
        # WAL 让读连接与写连接互不阻塞，是“清理不打断分页”的前提。
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = %d' % busy_timeout_ms)
    return conn


class PartitionWriter(object):
    """分区记录仓的写端：建表、追加、清理。

    连接按线程隔离：SQLite 一个连接同一时刻只能承载一个事务，多线程共享
    会破坏事务状态；同库多连接之间的写并发由文件锁与 BEGIN IMMEDIATE
    串行化。
    """

    def __init__(self, path, busy_timeout_ms=DEFAULT_BUSY_TIMEOUT_MS):
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._all_conns = []
        self._all_conns_lock = threading.Lock()
        self._create_schema()

    @property
    def conn(self):
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = _connect(self.path, read_only=False,
                            busy_timeout_ms=self.busy_timeout_ms)
            self._local.conn = conn
            with self._all_conns_lock:
                self._all_conns.append(conn)
        return conn

    def _create_schema(self):
        self.conn.executescript(
            'CREATE TABLE IF NOT EXISTS process_record ('
            '  warehouse TEXT NOT NULL,'
            '  partition TEXT NOT NULL,'
            '  seq INTEGER NOT NULL,'
            '  payload BLOB NOT NULL,'
            '  PRIMARY KEY (warehouse, partition, seq)'
            ') WITHOUT ROWID;'
            'CREATE TABLE IF NOT EXISTS partition_meta ('
            '  warehouse TEXT NOT NULL,'
            '  partition TEXT NOT NULL,'
            '  generation INTEGER NOT NULL,'
            '  dropped INTEGER NOT NULL DEFAULT 0,'
            '  PRIMARY KEY (warehouse, partition)'
            ') WITHOUT ROWID;')
        self.conn.commit()

    def close(self):
        with self._all_conns_lock:
            for conn in self._all_conns:
                conn.close()
            self._all_conns = []
        self._local = threading.local()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def append(self, warehouse, partition, payload):
        """向指定分区追加一条记录，返回该记录在分区内的单调序号。

        BEGIN IMMEDIATE 把“取序号 + 插入”串行化，多进程并发追加也不会
        产生重复 seq。分区元数据不存在时自动登记（generation=0）；向已
        dropped 的分区追加会隐式复活该分区并推进 generation，使旧游标失效。
        """
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError('payload 必须是 bytes。')
        conn = self.conn
        conn.execute('BEGIN IMMEDIATE')
        try:
            # 登记分区；若分区此前被 drop（dropped=1），复活时必须推进
            # generation，否则仍指向该分区的旧游标会误判锚点有效。
            conn.execute(
                'INSERT INTO partition_meta (warehouse, partition, generation,'
                ' dropped) VALUES (?, ?, 0, 0) '
                'ON CONFLICT(warehouse, partition) DO UPDATE SET '
                'generation = partition_meta.generation + partition_meta.dropped, '
                'dropped = 0',
                (warehouse, partition))
            row = conn.execute(
                'SELECT COALESCE(MAX(seq), 0) + 1 FROM process_record '
                'WHERE warehouse = ? AND partition = ?',
                (warehouse, partition)).fetchone()
            seq = row[0]
            conn.execute(
                'INSERT INTO process_record '
                '(warehouse, partition, seq, payload) VALUES (?, ?, ?, ?)',
                (warehouse, partition, seq, bytes(payload)))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return seq

    def reset_partition(self, warehouse, partition):
        """重新登记一个被删除的分区（generation 递增，旧游标一律失效）。"""
        self.conn.execute(
            'INSERT INTO partition_meta (warehouse, partition, generation,'
            ' dropped) VALUES (?, ?, 1, 0) '
            'ON CONFLICT(warehouse, partition) DO UPDATE SET '
            'generation = partition_meta.generation + 1, dropped = 0',
            (warehouse, partition))
        self.conn.commit()

    def clean_partition(self, warehouse, partition):
        """清理一个分区的全部记录并推进其 generation。

        返回被删除的记录数。generation 推进是游标失效的判定依据：清理后
        仍指向该分区的历史游标继续翻页时会收到 CursorExpired。
        """
        conn = self.conn
        conn.execute('BEGIN IMMEDIATE')
        try:
            cur = conn.execute(
                'DELETE FROM process_record '
                'WHERE warehouse = ? AND partition = ?',
                (warehouse, partition))
            deleted = cur.rowcount
            cur = conn.execute(
                'UPDATE partition_meta SET generation = generation + 1 '
                'WHERE warehouse = ? AND partition = ?',
                (warehouse, partition))
            if cur.rowcount == 0:
                # 分区从未写入过：登记一个已清理的空分区，保证 generation
                # 语义对任何后到的游标仍然成立。
                conn.execute(
                    'INSERT INTO partition_meta (warehouse, partition, '
                    'generation, dropped) VALUES (?, ?, 1, 0)',
                    (warehouse, partition))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return deleted

    def drop_partition(self, warehouse, partition):
        """删除分区：记录清空、元数据标记 dropped 并推进 generation。"""
        conn = self.conn
        conn.execute('BEGIN IMMEDIATE')
        try:
            cur = conn.execute(
                'DELETE FROM process_record '
                'WHERE warehouse = ? AND partition = ?',
                (warehouse, partition))
            deleted = cur.rowcount
            conn.execute(
                'INSERT INTO partition_meta (warehouse, partition, '
                'generation, dropped) VALUES (?, ?, 1, 1) '
                'ON CONFLICT(warehouse, partition) DO UPDATE SET '
                'generation = partition_meta.generation + 1, dropped = 1',
                (warehouse, partition))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return deleted

    def list_partitions(self, warehouse):
        rows = self.conn.execute(
            'SELECT partition FROM partition_meta '
            'WHERE warehouse = ? AND dropped = 0 ORDER BY partition',
            (warehouse,)).fetchall()
        return [r[0] for r in rows]


class ReadOnlyPager(object):
    """只读分页器：稳定页序、跨进程游标、清理时明确失效。"""

    def __init__(self, path, busy_timeout_ms=DEFAULT_BUSY_TIMEOUT_MS):
        self.path = path
        self.conn = _connect(path, read_only=True,
                             busy_timeout_ms=busy_timeout_ms)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def open_cursor(self, warehouse):
        """打开一个新的稳定视图游标。

        游标快照打开时刻可见（未 dropped）的分区集合及其 generation；
        之后才出现的分区不会被该游标读取，直到调用方重新打开游标。
        """
        rows = self.conn.execute(
            'SELECT partition, generation FROM partition_meta '
            'WHERE warehouse = ? AND dropped = 0 ORDER BY partition',
            (warehouse,)).fetchall()
        partitions = [r[0] for r in rows]
        generations = dict(rows)
        return _encode_cursor({
            'v': CURSOR_VERSION,
            'w': warehouse,
            'p': partitions,
            'g': generations,
            # 锚点：下一页从 (partition, seq) 严格大于该值处开始。
            # 空字符串/0 表示第一页。
            'ap': '',
            'as': 0,
        })

    def read_page(self, token, page_size):
        """读取游标之后的一页。

        每个分页在独立的只读事务（快照）内完成，写入/清理不会阻塞本页。
        锚点分区在游标打开后被清理或删除时抛出 :class:`CursorExpired`。
        """
        if not isinstance(page_size, int) or page_size <= 0:
            raise ValueError('page_size 必须是正整数。')
        data = _decode_cursor(token)
        warehouse = data.get('w')
        partitions = data.get('p')
        anchor_partition = data.get('ap', '')
        anchor_seq = data.get('as', 0)
        if (not isinstance(warehouse, str) or
                not isinstance(partitions, list) or
                not all(isinstance(p, str) for p in partitions) or
                not isinstance(anchor_partition, str) or
                not isinstance(anchor_seq, int)):
            raise CursorExpired('游标令牌内容不合法。')

        conn = self.conn
        # 第一页之前锚点为空；非首页必须确认锚点分区自打开后未被清理。
        if anchor_partition:
            row = conn.execute(
                'SELECT generation FROM partition_meta '
                'WHERE warehouse = ? AND partition = ?',
                (warehouse, anchor_partition)).fetchone()
            opened_generation = data.get('g', {}).get(anchor_partition)
            if row is None or row[0] != opened_generation:
                # 分区被清理/删除（generation 推进或元数据消失），锚点之后
                # 的数据边界已不可恢复——明确通知调用方重开游标。
                raise CursorExpired(
                    '锚点分区 %r 已被清理，历史游标无法恢复，'
                    '请重新打开游标获取新的稳定视图。' % anchor_partition)

        if not partitions:
            return Page([], None)

        # 只在游标打开时可见的分区集合内翻页，防止后建分区插进视图中间。
        placeholders = ','.join('?' for _ in partitions)
        sql = (
            'SELECT partition, seq, payload FROM process_record '
            'WHERE warehouse = ? AND partition IN (%s) '
            'AND (partition > ? OR (partition = ? AND seq > ?)) '
            'ORDER BY partition ASC, seq ASC LIMIT ?' % placeholders)
        params = ([warehouse] + partitions +
                  [anchor_partition, anchor_partition, anchor_seq,
                   page_size + 1])

        conn.execute('BEGIN')
        try:
            fetched = conn.execute(sql, params).fetchall()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        has_more = len(fetched) > page_size
        page_rows = [(p, s, bytes(payload))
                     for p, s, payload in fetched[:page_size]]
        next_token = None
        if has_more and page_rows:
            last_partition, last_seq, _ = page_rows[-1]
            data['ap'] = last_partition
            data['as'] = last_seq
            next_token = _encode_cursor(data)
        return Page(page_rows, next_token)

    def iterate(self, token, page_size):
        """便捷迭代器：逐页产出 :class:`Page`，首页可传 open_cursor 的结果。

        注意：中途发生清理仍会抛出 :class:`CursorExpired`，由调用方决定
        是否重开游标。
        """
        current = token
        while True:
            page = self.read_page(current, page_size)
            yield page
            if page.next_cursor is None:
                return
            current = page.next_cursor
