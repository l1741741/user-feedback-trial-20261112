"""
Read-only, consistent-snapshot diagnostics for ``SqliteHuey`` databases.

This module lets an operator inspect the backlog, schedule, results and
control state (revocations, locks, counters) of a huey sqlite database from
the terminal, without consuming anything:

* The database is opened read-only (``mode=ro``) with ``query_only`` set, so
  no tables are ever created and no rows are ever modified or deleted. A
  missing database is reported, never created. (Note: sqlite itself may
  materialize empty ``-wal``/``-shm`` sidecar files when opening a cold
  WAL-mode database read-only; these contain no data and are removed by
  sqlite on the next clean close.)
* All counts and entries are collected inside a single read transaction, so
  the report is a consistent snapshot of one point in time, even while
  consumers are enqueueing, dequeueing and storing results.
* Task messages are never unpickled. Task ids and names are extracted by
  scanning pickle opcodes with :mod:`pickletools`, which executes nothing.
  Task arguments and result values are never displayed.

The primary entry points are :func:`inspect_database`, which returns a
JSON-serializable report dictionary with stable field names, and
:func:`format_report`, which renders that report for the terminal. The
``huey_diag`` command-line tool wraps both.
"""

import datetime
import os
import pickletools
import re
import sqlite3
import urllib.parse
import zlib

REPORT_VERSION = 1

STATUS_OK = 'ok'
STATUS_MISSING = 'missing'
STATUS_LOCKED = 'locked'
STATUS_NO_SCHEMA = 'no-schema'
STATUS_LEGACY_SCHEMA = 'legacy-schema'
STATUS_UNREADABLE = 'unreadable'

#: Exit codes used by the ``huey_diag`` command-line tool.
EXIT_CODES = {
    STATUS_OK: 0,
    STATUS_MISSING: 2,
    STATUS_LOCKED: 3,
    STATUS_NO_SCHEMA: 4,
    STATUS_UNREADABLE: 5,
    STATUS_LEGACY_SCHEMA: 0,  # Report was still produced.
}

# Never attempt to decode a task message larger than this many bytes.
MAX_MESSAGE_BYTES = 65536

# Cap decompression output so a corrupt or hostile message cannot balloon.
MAX_DECOMPRESSED_BYTES = 4 * 1024 * 1024

# Tables of the current SqliteStorage schema and the columns diagnostics
# relies on. Anything else is treated as a legacy layout.
_EXPECTED_COLUMNS = {
    'task': ('id', 'queue', 'data', 'priority'),
    'schedule': ('id', 'queue', 'data', 'timestamp'),
    'kv': ('queue', 'key', 'value'),
    'counter': ('queue', 'key', 'value'),
}

_TASK_ID_RE = re.compile(
    '^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$|^[0-9a-f]{32}$')
_STRING_OPS = frozenset(
    ('SHORT_BINUNICODE', 'BINUNICODE', 'BINUNICODE8', 'UNICODE'))
_BYTES_OPS = frozenset(
    ('SHORT_BINBYTES', 'BINBYTES', 'BINBYTES8', 'BYTEARRAY8'))
_SIGNED_RE = re.compile(b':[0-9a-f]{40}$')


def _decode_error(info, category, detail):
    info['status'] = 'invalid'
    info['category'] = category
    info['detail'] = detail
    return info


def _decompress(data, wbits):
    obj = zlib.decompressobj(wbits)
    out = obj.decompress(data, MAX_DECOMPRESSED_BYTES + 1)
    if obj.unconsumed_tail or len(out) > MAX_DECOMPRESSED_BYTES:
        raise ValueError('decompressed message exceeds %d bytes'
                         % MAX_DECOMPRESSED_BYTES)
    return out


def _scan_pickle_strings(raw):
    """
    Walk the pickle opcode stream without executing it, collecting string
    constants in order. Raises ValueError with the byte offset when the
    stream cannot be parsed.
    """
    strings = []
    pos = 0
    try:
        for opcode, arg, pos in pickletools.genops(raw):
            if opcode.name in _STRING_OPS:
                strings.append(arg)
    except Exception as exc:
        raise ValueError('%s at byte %d' % (str(exc)[:80] or
                                            type(exc).__name__, pos))
    return strings


def describe_message(data):
    """
    Safely extract the task id and task name from a serialized huey task
    message. The message is never unpickled; opcodes are only scanned.

    :param bytes data: raw message as stored in the task/schedule tables.
    :return: dict with stable keys: ``status`` ("ok", "too-large",
        "unparsed" or "invalid"), ``category`` (None, "decompress",
        "pickle-parse" or "layout"), ``detail``, ``compressed``, ``signed``,
        ``task_id`` and ``task_name``.
    """
    info = {'status': 'ok', 'category': None, 'detail': None,
            'compressed': None, 'signed': None,
            'task_id': None, 'task_name': None}

    data = bytes(data)
    if len(data) > MAX_MESSAGE_BYTES:
        return _decode_error(info, 'too-large',
                             'message exceeds %d bytes' % MAX_MESSAGE_BYTES)

    raw = data
    if raw[:2] == b'\x1f\x8b':
        info['compressed'] = 'gzip'
        try:
            raw = _decompress(raw, 16 + zlib.MAX_WBITS)
        except Exception as exc:
            return _decode_error(info, 'decompress', str(exc))
    elif raw[:1] == b'\x78':
        info['compressed'] = 'zlib'
        try:
            raw = _decompress(raw, zlib.MAX_WBITS)
        except Exception as exc:
            return _decode_error(info, 'decompress', str(exc))

    # A SignedSerializer message is "<pickle>:<40 hex chars>". The opcode
    # stream stops at STOP, so trailing bytes would otherwise be ignored
    # silently. The signature cannot be verified here (the secret is
    # unknown), but the payload can still be scanned after stripping it.
    match = _SIGNED_RE.search(raw)
    if match:
        info['signed'] = 'unverified'
        raw = raw[:match.start()]

    try:
        strings = _scan_pickle_strings(raw)
    except ValueError as exc:
        return _decode_error(info, 'pickle-parse', str(exc))

    # In a huey message the first uuid-shaped string is the task id and the
    # string right after it is the registered task name. Nested on_complete /
    # on_error messages appear later, so the outermost task is found first.
    for i, value in enumerate(strings):
        if _TASK_ID_RE.match(value):
            info['task_id'] = value
            if i + 1 < len(strings):
                info['task_name'] = strings[i + 1]
            return info

    info['status'] = 'unparsed'
    info['category'] = 'layout'
    info['detail'] = 'no task id found in pickle stream'
    return info


def _decode_pickle_datetime(data):
    # A naive datetime pickles to a 10-byte big-endian reduction argument.
    if len(data) != 10:
        return None
    try:
        return datetime.datetime(
            (data[0] << 8) | data[1], data[2], data[3],
            data[4], data[5], data[6],
            (data[7] << 16) | (data[8] << 8) | data[9])
    except ValueError:
        return None


def describe_revocation(data):
    """
    Safely describe a serialized revocation value ``(revoke_until,
    revoke_once)`` without unpickling it.

    :return: dict with stable keys: ``status``, ``category``, ``detail``,
        ``once`` (bool or None) and ``until`` (ISO timestamp or None).
    """
    info = {'status': 'ok', 'category': None, 'detail': None,
            'once': None, 'until': None}

    data = bytes(data)
    if len(data) > MAX_MESSAGE_BYTES:
        return _decode_error(info, 'too-large',
                             'value exceeds %d bytes' % MAX_MESSAGE_BYTES)
    raw = data
    if raw[:2] == b'\x1f\x8b' or raw[:1] == b'\x78':
        try:
            raw = _decompress(raw, 31 if raw[:2] == b'\x1f\x8b' else 15)
        except Exception as exc:
            return _decode_error(info, 'decompress', str(exc))
    match = _SIGNED_RE.search(raw)
    if match:
        raw = raw[:match.start()]

    try:
        ops = list(pickletools.genops(raw))
    except Exception as exc:
        return _decode_error(info, 'pickle-parse', str(exc))

    for opcode, arg, _ in ops:
        if opcode.name == 'NEWTRUE':
            info['once'] = True
        elif opcode.name == 'NEWFALSE':
            info['once'] = False
        elif opcode.name in _BYTES_OPS and isinstance(arg, bytes):
            dt = _decode_pickle_datetime(arg)
            if dt is not None:
                info['until'] = dt.isoformat()
    return info


def _decode_entry(data, size):
    # ``data`` may have been truncated by the SQL substr(); use the true
    # blob size to decide whether decoding is even attempted.
    if size > MAX_MESSAGE_BYTES:
        return {'status': 'invalid', 'category': 'too-large',
                'detail': 'message exceeds %d bytes' % MAX_MESSAGE_BYTES,
                'compressed': None, 'signed': None,
                'task_id': None, 'task_name': None}
    return describe_message(data)


def _file_identity(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _classify_schema(conn):
    """
    :return: (present, legacy) - sets of table names. ``present`` tables
        match the current schema; ``legacy`` tables exist but have an older
        layout (e.g. no queue or priority column).
    """
    found = {row[0] for row in conn.execute(
        "select name from sqlite_master where type = 'table'")}
    present, legacy = set(), set()
    for table, expected in _EXPECTED_COLUMNS.items():
        if table not in found:
            continue
        columns = {row[1] for row in conn.execute(
            'pragma table_info(%s)' % table)}
        if set(expected) <= columns:
            present.add(table)
        else:
            legacy.add(table)
    return present, legacy


def _kv_kind_case():
    return ("case when key like 'rt:%' then 'class-revocation' "
            "when key like 'r:%' then 'revocation' "
            "when key like '%.lock.%' then 'lock' "
            "else 'result' end")


def _empty_section(offset, limit):
    return {'count': None, 'offset': offset, 'limit': limit, 'returned': 0,
            'queue_filtered': None, 'entries': []}


def inspect_database(filename, queue='huey', limit=20, offset=0, timeout=5.0):
    """
    Collect a read-only, consistent-snapshot diagnostic report for the
    SqliteHuey database at ``filename``.

    :param str filename: path to the sqlite database file.
    :param str queue: only this queue's rows are reported (multiple huey
        queues may share one database file).
    :param int limit: maximum number of entries listed per section. Counts
        always reflect the whole queue, regardless of the limit.
    :param int offset: skip this many entries (stable ordering). Counts and
        the returned page come from the same snapshot; separate invocations
        are separate snapshots.
    :param float timeout: seconds to wait if the database is locked by a
        writer before reporting a "locked" status.
    :return: JSON-serializable dict. See ``REPORT_VERSION``; top-level keys
        are stable: version, status, detail, warnings, database, queue,
        snapshot, summary, pending, scheduled, results, control.
    """
    limit = max(0, int(limit))
    offset = max(0, int(offset))
    path = os.path.abspath(filename)

    report = {
        'version': REPORT_VERSION,
        'status': STATUS_OK,
        'detail': None,
        'warnings': [],
        'database': {'path': path, 'size_bytes': None, 'journal_mode': None},
        'queue': queue,
        'snapshot': {
            'mode': 'single-read-transaction',
            'consistent': False,
            'note': ('counts and entries in this report come from one read '
                     'transaction; separate invocations are separate '
                     'snapshots'),
        },
        'summary': {'pending': None, 'scheduled': None, 'results': None,
                    'revocations': None, 'locks': None, 'counters': None},
        'pending': _empty_section(offset, limit),
        'scheduled': _empty_section(offset, limit),
        'results': _empty_section(offset, limit),
        'control': {'revocations': [], 'locks': [], 'counters': [],
                    'queue_filtered': None},
    }

    identity_before = _file_identity(path)
    if identity_before is None:
        report['status'] = STATUS_MISSING
        report['detail'] = ('database file does not exist; nothing was '
                            'created or modified')
        return report

    conn = None
    try:
        report['database']['size_bytes'] = os.stat(path).st_size
        uri = 'file:%s?mode=ro' % urllib.parse.quote(path)
        conn = sqlite3.connect(uri, uri=True, timeout=timeout,
                               check_same_thread=False)
        conn.isolation_level = None  # Autocommit; we manage the read txn.
        conn.execute('pragma query_only=on')
        report['database']['journal_mode'] = conn.execute(
            'pragma journal_mode').fetchone()[0]

        # Everything below runs inside one read transaction, giving a
        # single consistent point-in-time view of all tables.
        conn.execute('begin')
        try:
            _collect(conn, report, queue, limit, offset)
        finally:
            conn.rollback()
        report['snapshot']['consistent'] = True
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if 'locked' in message:
            report['status'] = STATUS_LOCKED
            report['detail'] = ('database is locked by another writer; gave '
                                'up after %.1fs' % timeout)
        else:
            report['status'] = STATUS_UNREADABLE
            report['detail'] = message[:200]
    except sqlite3.DatabaseError as exc:
        report['status'] = STATUS_UNREADABLE
        report['detail'] = str(exc)[:200]
    finally:
        if conn is not None:
            conn.close()

    identity_after = _file_identity(path)
    if identity_after != identity_before:
        if identity_after is None:
            report['warnings'].append(
                'database file was removed during inspection')
        else:
            report['warnings'].append(
                'database file was replaced during inspection; report '
                'reflects the original file')

    return report


def _collect(conn, report, queue, limit, offset):
    present, legacy = _classify_schema(conn)
    warnings = report['warnings']

    if not present and not legacy:
        report['status'] = STATUS_NO_SCHEMA
        report['detail'] = ('database has no huey tables (task, schedule, '
                            'kv, counter); nothing was created')
        return

    if legacy:
        report['status'] = STATUS_LEGACY_SCHEMA
        report['detail'] = ('legacy schema: %s; counts are not filtered by '
                            'queue' % ', '.join(
                                'table "%s" has an old layout' % t
                                for t in sorted(legacy)))
        warnings.append('legacy schema detected; showing rows for all '
                        'queues combined')

    for table in _EXPECTED_COLUMNS:
        if table not in present and table not in legacy:
            warnings.append('table "%s" not found; section unavailable'
                            % table)

    def where(table):
        # Returns (where_sql, params, queue_filtered).
        if table in present:
            return ' where queue = ?', (queue,), True
        return '', (), False

    # -- pending tasks ---------------------------------------------------
    if 'task' in present or 'task' in legacy:
        sql_where, params, filtered = where('task')
        has_priority = 'task' in present
        prio_col = 'priority, ' if has_priority else 'null, '
        order = 'priority desc, id' if has_priority else 'id'
        section = report['pending']
        section['queue_filtered'] = filtered
        section['count'] = conn.execute(
            'select count(*) from task' + sql_where, params).fetchone()[0]
        rows = conn.execute(
            'select id, %slength(data), substr(data, 1, %d) from task%s '
            'order by %s limit ? offset ?'
            % (prio_col, MAX_MESSAGE_BYTES, sql_where, order),
            params + (limit, offset)).fetchall()
        for i, (row_id, priority, size, data) in enumerate(rows):
            decode = _decode_entry(data, size)
            section['entries'].append({
                'position': offset + i,
                'row_id': row_id,
                'priority': priority if has_priority else None,
                'bytes': size,
                'task_id': decode['task_id'],
                'task_name': decode['task_name'],
                'decode': {k: decode[k] for k in
                           ('status', 'category', 'detail', 'compressed',
                            'signed')},
            })
        section['returned'] = len(section['entries'])
        report['summary']['pending'] = section['count']

    # -- scheduled tasks ---------------------------------------------------
    if 'schedule' in present or 'schedule' in legacy:
        sql_where, params, filtered = where('schedule')
        section = report['scheduled']
        section['queue_filtered'] = filtered
        section['count'] = conn.execute(
            'select count(*) from schedule' + sql_where,
            params).fetchone()[0]
        rows = conn.execute(
            'select id, timestamp, length(data), substr(data, 1, %d) '
            'from schedule%s order by timestamp, id limit ? offset ?'
            % (MAX_MESSAGE_BYTES, sql_where),
            params + (limit, offset)).fetchall()
        for i, (row_id, ts, size, data) in enumerate(rows):
            decode = _decode_entry(data, size)
            section['entries'].append({
                'position': offset + i,
                'row_id': row_id,
                'timestamp': ts,
                'eta_utc': datetime.datetime.fromtimestamp(
                    ts, datetime.timezone.utc).isoformat(),
                'bytes': size,
                'task_id': decode['task_id'],
                'task_name': decode['task_name'],
                'decode': {k: decode[k] for k in
                           ('status', 'category', 'detail', 'compressed',
                            'signed')},
            })
        section['returned'] = len(section['entries'])
        report['summary']['scheduled'] = section['count']

    # -- key/value rows: results, revocations, locks -----------------------
    if 'kv' in present or 'kv' in legacy:
        sql_where, params, filtered = where('kv')
        kind_case = _kv_kind_case()
        kind_counts = dict(conn.execute(
            'select %s, count(*) from kv%s group by 1'
            % (kind_case, sql_where), params).fetchall())
        results_count = kind_counts.get('result', 0)
        report['summary']['results'] = results_count
        report['summary']['revocations'] = (
            kind_counts.get('revocation', 0)
            + kind_counts.get('class-revocation', 0))
        report['summary']['locks'] = kind_counts.get('lock', 0)

        section = report['results']
        section['queue_filtered'] = filtered
        section['count'] = results_count
        rows = conn.execute(
            "select key, length(value) from kv%s %s key not like 'r:%%' "
            "and key not like 'rt:%%' and key not like '%%.lock.%%' "
            "order by key limit ? offset ?"
            % (sql_where, 'and' if filtered else 'where'),
            params + (limit, offset)).fetchall()
        section['entries'] = [{'key': key, 'bytes': size}
                              for key, size in rows]
        section['returned'] = len(rows)

        report['control']['queue_filtered'] = filtered
        rows = conn.execute(
            "select key, length(value), substr(value, 1, %d) from kv%s "
            "%s (key like 'r:%%' or key like 'rt:%%') "
            "order by key limit ?"
            % (MAX_MESSAGE_BYTES, sql_where,
               'and' if filtered else 'where'),
            params + (limit,)).fetchall()
        for key, size, value in rows:
            decode = (describe_revocation(value)
                      if size <= MAX_MESSAGE_BYTES else
                      {'status': 'invalid', 'category': 'too-large',
                       'detail': 'value exceeds %d bytes'
                                 % MAX_MESSAGE_BYTES,
                       'once': None, 'until': None})
            report['control']['revocations'].append({
                'key': key,
                'kind': 'task-class' if key.startswith('rt:') else 'task',
                'bytes': size,
                'once': decode['once'],
                'until': decode['until'],
                'decode': {k: decode[k] for k in
                           ('status', 'category', 'detail')},
            })

        rows = conn.execute(
            "select key, length(value) from kv%s %s key like '%%.lock.%%' "
            "and key not like 'r:%%' order by key limit ?"
            % (sql_where, 'and' if filtered else 'where'),
            params + (limit,)).fetchall()
        report['control']['locks'] = [
            {'key': key, 'name': key.split('.lock.', 1)[-1], 'bytes': size}
            for key, size in rows]

    # -- counters ----------------------------------------------------------
    if 'counter' in present or 'counter' in legacy:
        sql_where, params, _ = where('counter')
        count = conn.execute(
            'select count(*) from counter' + sql_where,
            params).fetchone()[0]
        report['summary']['counters'] = count
        rows = conn.execute(
            'select key, value from counter%s order by key limit ?'
            % sql_where, params + (limit,)).fetchall()
        report['control']['counters'] = [
            {'key': key, 'value': value} for key, value in rows]


def _fmt_bytes(size):
    if size is None:
        return '?'
    if size < 1024:
        return '%d B' % size
    if size < 1024 * 1024:
        return '%.1f KiB' % (size / 1024.0)
    return '%.1f MiB' % (size / (1024.0 * 1024.0))


def _safe(value, max_len=60):
    """
    Make an arbitrary stored string safe for the terminal: escape
    non-printable characters and cap the length.
    """
    text = str(value)
    text = ''.join(c if (c.isprintable() and c not in '\x1b\x07') else '?'
                   for c in text)
    if len(text) > max_len:
        text = text[:max_len - 3] + '...'
    return text


def _task_label(entry):
    decode = entry['decode']
    if decode['status'] != 'ok':
        detail = decode['detail'] or decode['category'] or 'unreadable'
        return '<%s: %s>' % (decode['status'], _safe(detail, 40))
    name = _safe(entry['task_name']) if entry['task_name'] else '<no name>'
    if entry['task_id']:
        return '%s [%s]' % (name, _safe(entry['task_id'], 40))
    return name


def format_report(report):
    """
    Render a report produced by :func:`inspect_database` as plain text for
    the terminal. Output is bounded by the report's entry limits; message
    contents, task arguments and result values are never included.
    """
    lines = []
    add = lines.append
    db = report['database']

    add('huey sqlite diagnostics (report v%d)' % report['version'])
    if db['size_bytes'] is not None:
        add('database: %s (%s, journal=%s)' % (
            db['path'], _fmt_bytes(db['size_bytes']),
            db['journal_mode'] or '?'))
    else:
        add('database: %s' % db['path'])
    add('queue:    %s' % _safe(report['queue']))
    add('status:   %s' % report['status'])
    if report['detail']:
        add('detail:   %s' % _safe(report['detail'], 200))
    if report['snapshot']['consistent']:
        add('snapshot: consistent (%s)' % report['snapshot']['mode'])
    else:
        add('snapshot: no consistent snapshot available')

    if report['status'] in (STATUS_OK, STATUS_LEGACY_SCHEMA):
        _format_sections(report, add)

    for warning in report['warnings']:
        add('warning:  %s' % _safe(warning, 200))
    add('note: task arguments and result values are never displayed')
    return '\n'.join(lines)


def _format_sections(report, add):
    summary = report['summary']

    def section_header(name, section):
        count = section['count']
        if count is None:
            add('%s: unavailable' % name)
            return False
        extra = ''
        if section['queue_filtered'] is False:
            extra = ', all queues (legacy schema)'
        add('%s: %d (showing %d from offset %d%s)' % (
            name, count, section['returned'], section['offset'], extra))
        return True

    if section_header('pending', report['pending']):
        for entry in report['pending']['entries']:
            prio = entry['priority']
            add('  %4d row=%-6s prio=%-4s %9s  %s' % (
                entry['position'], entry['row_id'],
                prio if prio is not None else '-',
                _fmt_bytes(entry['bytes']), _task_label(entry)))

    if section_header('scheduled', report['scheduled']):
        for entry in report['scheduled']['entries']:
            add('  %4d row=%-6s eta=%s  %9s  %s' % (
                entry['position'], entry['row_id'], entry['eta_utc'],
                _fmt_bytes(entry['bytes']), _task_label(entry)))

    if section_header('results', report['results']):
        for entry in report['results']['entries']:
            add('  %s  %s' % (_safe(entry['key'], 48),
                              _fmt_bytes(entry['bytes'])))

    control = report['control']
    if any(summary[k] is not None
           for k in ('revocations', 'locks', 'counters')):
        add('control: revocations=%s locks=%s counters=%s' % (
            summary['revocations'], summary['locks'], summary['counters']))
        for entry in control['revocations']:
            when = ('once' if entry['once'] else
                    ('until %s' % entry['until'] if entry['until']
                     else 'indefinite'))
            if entry['decode']['status'] != 'ok':
                when = '<%s: %s>' % (entry['decode']['status'],
                                     _safe(entry['decode']['detail'] or '',
                                           40))
            add('  revocation %-40s %-10s %s' % (
                _safe(entry['key'], 40), entry['kind'], when))
        for entry in control['locks']:
            add('  lock       %-40s (%s)' % (_safe(entry['key'], 40),
                                             _safe(entry['name'], 40)))
        for entry in control['counters']:
            add('  counter    %-40s %s' % (_safe(entry['key'], 40),
                                           entry['value']))
