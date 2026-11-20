"""
Support for quarantining task messages that cannot be decoded or executed.

The quarantine keeps the raw message bytes and a small amount of *safely
derived* metadata (queue, source, task id/name, eta, priority and an error
classification) so that a bad message can be inspected, replayed or discarded
later, without having to execute the untrusted pickle.

This module contains the record objects, message validation helpers and a
small command-line interface. The actual persistence is implemented by the
Sqlite storage backend.
"""
import argparse
import datetime
import hashlib
import pickletools
import sys

from huey.exceptions import TaskNotRegistered
from huey.registry import Message
from huey.utils import load_class


# Error classifications stored alongside every quarantine record.
ERROR_DESERIALIZE = 'deserialize-error'
ERROR_SIGNATURE = 'signature-invalid'
ERROR_UNREGISTERED = 'unregistered-task'
ERROR_INVALID = 'invalid-message'

ERROR_CLASSES = (ERROR_DESERIALIZE, ERROR_SIGNATURE, ERROR_UNREGISTERED,
                 ERROR_INVALID)


def classify_deserialize_error(exc):
    """
    Return a (classification, message) 2-tuple for an exception raised while
    deserializing a message.

    The exception text itself is intentionally discarded: serializer errors may
    embed raw message fragments, argument values or HMAC signatures. Callers
    only get a stable classification plus a short, safe description.
    """
    try:
        text = str(exc)
    except Exception:
        text = ''

    if 'Signature' in text and 'mismatch' in text:
        return ERROR_SIGNATURE, 'message signature verification failed'
    elif 'Separator' in text and 'not found' in text:
        return ERROR_SIGNATURE, 'signed message separator missing'
    return ERROR_DESERIALIZE, type(exc).__name__


def describe_replay_error(exc):
    """Return a safe, human-readable error for a failed replay."""
    if isinstance(exc, TaskNotRegistered):
        return '%s: %s' % (ERROR_UNREGISTERED, exc.task_name)
    elif isinstance(exc, ValueError):
        error_class, error = classify_deserialize_error(exc)
        return '%s: %s' % (error_class, error)
    return '%s: %s' % (ERROR_DESERIALIZE, type(exc).__name__)


def validate_message(message):
    """
    Perform cheap structural validation of a decoded message.

    Raises ``ValueError`` with a safe description (no argument values) if the
    message cannot be a task message. The registry's ``create_task()`` remains
    responsible for validating nested callbacks and task signatures.
    """
    if not isinstance(message, Message):
        raise ValueError('message is not a task Message')
    # ``id`` and ``args`` / ``kwargs`` may be None for messages written by
    # older Huey versions (the registry/task layer supplies defaults), so only
    # reject values that the registry cannot work with at all.
    if message.id is not None and not isinstance(message.id, str):
        raise ValueError('message id is invalid')
    if not isinstance(message.name, str) or not message.name:
        raise ValueError('message task name is invalid')
    if message.args is not None and not isinstance(message.args, tuple):
        raise ValueError('message args are invalid')
    if message.kwargs is not None and not isinstance(message.kwargs, dict):
        raise ValueError('message kwargs are invalid')
    if message.eta is not None and not isinstance(message.eta,
                                                  datetime.datetime):
        raise ValueError('message eta is invalid')
    if message.priority is not None and (
            isinstance(message.priority, bool) or
            not isinstance(message.priority, (int, float))):
        raise ValueError('message priority is invalid')
    if message.retries is not None and not isinstance(message.retries, int):
        raise ValueError('message retries are invalid')
    if message.timeout is not None and (
            isinstance(message.timeout, bool) or
            not isinstance(message.timeout, (int, float))):
        raise ValueError('message timeout is invalid')
    if message.chord_config is not None and not isinstance(
            message.chord_config, tuple):
        raise ValueError('message chord config is invalid')


def message_metadata(message):
    """
    Extract the metadata needed by a quarantine record from a decoded message,
    ignoring any attribute that is missing or has an unexpected type.
    """
    metadata = {}
    if not isinstance(message, Message):
        return metadata

    if isinstance(message.id, str) and message.id:
        metadata['task_id'] = message.id
    if isinstance(message.name, str) and message.name:
        metadata['task_name'] = message.name
    if isinstance(message.eta, datetime.datetime):
        metadata['eta'] = message.eta.isoformat(sep=' ')
    if (isinstance(message.priority, (int, float)) and
            not isinstance(message.priority, bool)):
        metadata['priority'] = message.priority
    return metadata


_MAX_OPS = 500
_MEMO_OPS = frozenset(('BINPUT', 'MEMOIZE', 'PUT', 'LONG_BINPUT'))


def _scan_ops(data):
    """
    Parse the pickle opcode stream without executing it.

    ``pickletools.genops()`` only decodes opcodes and never imports or calls
    anything, so this is safe to run against untrusted data. Returns an empty
    list if the stream cannot be parsed.
    """
    ops = []
    try:
        for i, (op, arg, pos) in enumerate(pickletools.genops(data)):
            ops.append((op.name, arg))
            if i >= _MAX_OPS:
                return []
            if op.name == 'STOP':
                break
    except Exception:
        return []
    return ops


def _stack_global(ops, idx):
    # STACK_GLOBAL builds its name from the two preceding unicode strings,
    # which may be separated by memoization opcodes.
    strings = []
    jdx = idx - 1
    while jdx >= 0 and len(strings) < 2:
        name, arg = ops[jdx]
        if name in _MEMO_OPS:
            jdx -= 1
        elif name in ('UNICODE', 'BINUNICODE', 'SHORT_BINUNICODE'):
            strings.append(arg)
            jdx -= 1
        else:
            return None
    if len(strings) != 2:
        return None
    return '%s %s' % (strings[1], strings[0])


def _global_at(ops, idx):
    name, arg = ops[idx]
    if name == 'GLOBAL':
        return arg
    if name == 'STACK_GLOBAL':
        return _stack_global(ops, idx)
    return None


def _message_global_indices(ops):
    return [idx for idx in range(len(ops))
            if _global_at(ops, idx) in ('huey.registry Message',
                                        'huey.registry.Message')]


def _datetime_from_encoded(raw):
    """
    Decode the 10-byte state produced by ``datetime.__reduce__``.

    The layout is: two big-endian year bytes, month, day, hour, minute,
    second as single bytes, then three big-endian microsecond bytes.
    """
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 10:
        return None
    try:
        year = int.from_bytes(raw[0:2], 'big')
        month, day, hour, minute, second = raw[2:7]
        microsecond = int.from_bytes(raw[7:10], 'big')
        return datetime.datetime(year, month, day, hour, minute, second,
                                 microsecond)
    except Exception:
        return None


def _decode_datetime(ops, idx):
    """
    Decode an eta field starting at ``idx`` and return (datetime, next_idx).

    A datetime value is a ``datetime`` global followed (within a few opcodes)
    by its encoded state, so the datetime global is located by scanning
    forward a short distance. This handles the protocol 0-2 form (a
    ``_codecs.encode`` REDUCE wrapped in a datetime REDUCE) and the protocol
    3+ form (a ``datetime`` REDUCE over a raw byte string). Returns
    (None, idx) if the value is not a plain datetime.
    """
    global_idx = None
    for jdx in range(idx, min(idx + 8, len(ops))):
        if _global_at(ops, jdx) == 'datetime datetime':
            global_idx = jdx
            break
    if global_idx is None:
        return None, idx

    # Find the first string/bytes literal after the datetime global.
    for jdx in range(global_idx + 1, min(global_idx + 10, len(ops))):
        name, arg = ops[jdx]
        if name in ('BINBYTES', 'SHORT_BINBYTES', 'BINBYTES8'):
            dt = _datetime_from_encoded(arg)
        elif name in ('UNICODE', 'BINUNICODE', 'SHORT_BINUNICODE') and \
                isinstance(arg, str):
            try:
                dt = _datetime_from_encoded(arg.encode('latin1'))
            except Exception:
                dt = None
        else:
            continue
        if dt is None:
            return None, idx
        k = jdx + 1
        while k < len(ops) and ops[k][0] in (
                'TUPLE', 'TUPLE1', 'TUPLE2', 'TUPLE3', 'REDUCE') + \
                tuple(_MEMO_OPS):
            k += 1
        return dt, k
    return None, idx


def _is_global_start(ops, idx):
    """Return True if ``idx`` begins a global reference (GLOBAL/STACK_GLOBAL)."""
    name = ops[idx][0]
    if name == 'GLOBAL':
        return True
    if name != 'UNICODE' and name not in ('BINUNICODE',
                                          'SHORT_BINUNICODE'):
        return False
    # STACK_GLOBAL is preceded by two unicode strings; this is the first.
    jdx = idx + 1
    while jdx < len(ops) and ops[jdx][0] in _MEMO_OPS:
        jdx += 1
    if jdx >= len(ops) or ops[jdx][0] not in (
            'UNICODE', 'BINUNICODE', 'SHORT_BINUNICODE'):
        return False
    jdx += 1
    while jdx < len(ops) and ops[jdx][0] in _MEMO_OPS:
        jdx += 1
    return jdx < len(ops) and ops[jdx][0] == 'STACK_GLOBAL'


def _parse_fields(ops, start):
    # Positional fields: id, name, eta, retries, retry_delay, priority.
    fields = []
    idx = start
    for _ in range(6):
        if idx >= len(ops):
            break
        name, arg = ops[idx]
        if _is_global_start(ops, idx):
            # A global value, e.g. the datetime eta (or an unexpected object).
            dt, next_idx = _decode_datetime(ops, idx)
            if dt is None:
                break
            fields.append(dt)
            idx = next_idx
        elif name in ('UNICODE', 'BINUNICODE', 'SHORT_BINUNICODE'):
            fields.append(arg)
            idx += 1
        elif name in ('BININT', 'BININT1', 'BININT2', 'INT'):
            fields.append(arg)
            idx += 1
        elif name == 'NONE':
            fields.append(None)
            idx += 1
        else:
            # Anything else (dict, tuple, etc.) starts the remaining fields.
            break
        while idx < len(ops) and ops[idx][0] in _MEMO_OPS:
            idx += 1
    return fields


def scan_message_metadata(data):
    """
    Best-effort, execution-free extraction of message metadata.

    Returns a dict with any of ``task_id``, ``task_name``, ``eta`` and
    ``priority`` that can be recovered purely by reading pickle opcodes. Task
    arguments, kwargs and callbacks are never read, and nothing is un-pickled.
    Returns an empty dict if the stream cannot be understood.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return {}
    if isinstance(data, memoryview):
        data = data.tobytes()
    ops = _scan_ops(data)
    if not ops:
        return {}

    # A message may be represented by several tuple layouts depending on the
    # pickle protocol (e.g. the protocol 0/1 __reconstructor wrapper). Try
    # every argument-tuple start following a Message reference and use the
    # first whose leading fields look like a message id and task name.
    fields = None
    for global_idx in _message_global_indices(ops):
        for mark_idx in range(global_idx + 1, len(ops)):
            if ops[mark_idx][0] == 'STOP':
                break
            if ops[mark_idx][0] != 'MARK':
                continue
            candidate = _parse_fields(ops, mark_idx + 1)
            if (len(candidate) >= 2 and isinstance(candidate[0], str) and
                    candidate[0] and isinstance(candidate[1], str) and
                    candidate[1]):
                fields = candidate
                break
        if fields is not None:
            break
    if not fields:
        return {}

    metadata = {}
    if isinstance(fields[0], str) and fields[0]:
        metadata['task_id'] = fields[0]
    if len(fields) > 1 and isinstance(fields[1], str) and fields[1]:
        metadata['task_name'] = fields[1]
    if len(fields) > 2 and isinstance(fields[2], datetime.datetime):
        metadata['eta'] = fields[2].isoformat(sep=' ')
    if len(fields) > 5 and isinstance(fields[5], (int, float)) and \
            not isinstance(fields[5], bool):
        metadata['priority'] = fields[5]
    return metadata


class QuarantineRecord(object):
    """
    Read-only view of a quarantined message.

    The raw bytes are exposed as :py:attr:`data` for callers that explicitly
    need them, but are deliberately excluded from the repr and from the
    command-line output so that inspecting a record never prints task
    arguments or signature material.
    """
    __slots__ = ('id', 'queue', 'source', 'error_class', 'error', 'task_id',
                 'task_name', 'eta', 'priority', 'created', 'created_ts',
                 'data', 'size', 'digest')

    def __init__(self, id, queue, source, error_class, error, task_id,
                 task_name, eta, priority, created_ts, data=None, size=None):
        self.id = id
        self.queue = queue
        self.source = source
        self.error_class = error_class
        self.error = error
        self.task_id = task_id
        self.task_name = task_name
        self.eta = eta
        self.priority = priority
        self.created_ts = created_ts
        self.created = datetime.datetime.fromtimestamp(created_ts)
        self.data = data
        if data is None:
            self.size = size
            self.digest = None
        else:
            self.size = len(data)
            self.digest = hashlib.sha256(data).hexdigest()[:16]

    @property
    def has_data(self):
        return self.data is not None

    def __repr__(self):
        return ('<QuarantineRecord id=%s queue=%r source=%s class=%s '
                'task=%s size=%s created=%s>' % (
                    self.id, self.queue, self.source, self.error_class,
                    self.task_name or '-', self.size, self.created))

    __str__ = __repr__


class ReplayResult(object):
    """Outcome of an explicit attempt to replay a quarantined message."""
    __slots__ = ('id', 'success', 'destination', 'task_id', 'priority',
                 'error')

    def __init__(self, id, success, destination=None, task_id=None,
                 priority=None, error=None):
        self.id = id
        self.success = success
        self.destination = destination
        self.task_id = task_id
        self.priority = priority
        self.error = error

    def __bool__(self):
        return self.success

    def __repr__(self):
        if self.success:
            return ('<ReplayResult id=%s success destination=%s task=%s '
                    'priority=%s>' % (self.id, self.destination,
                                      self.task_id, self.priority))
        return '<ReplayResult id=%s failed: %s>' % (self.id, self.error)

    __str__ = __repr__


def format_record(record):
    """One-line, argument-free summary of a quarantine record."""
    parts = ['id=%s' % record.id,
             'queue=%r' % record.queue,
             'source=%s' % record.source,
             'class=%s' % record.error_class]
    if record.task_name:
        parts.append('task=%s' % record.task_name)
    if record.task_id:
        parts.append('task_id=%s' % record.task_id)
    if record.eta:
        parts.append('eta=%s' % record.eta)
    if record.priority is not None:
        parts.append('priority=%s' % record.priority)
    parts.append('size=%s' % record.size)
    parts.append('created=%s' % record.created.strftime('%Y-%m-%d %H:%M:%S'))
    parts.append('error=%r' % record.error)
    return ' '.join(parts)


def _load_huey(path):
    try:
        return load_class(path)
    except Exception:
        import os
        cur_dir = os.getcwd()
        if cur_dir not in sys.path:
            sys.path.insert(0, cur_dir)
            return load_class(path)
        raise


def _build_parser():
    parser = argparse.ArgumentParser(
        prog='huey_quarantine',
        description='Inspect, replay or discard quarantined Huey messages.')
    parser.add_argument('command', nargs='?', default='list',
                        choices=('list', 'show', 'replay', 'discard'),
                        help='action to perform (default: list)')
    parser.add_argument('record_id', nargs='?', type=int,
                        help='quarantine record id')
    parser.add_argument('--limit', type=int, default=None,
                        help='maximum number of records to list')
    return parser


def main(argv=None, huey=None, stream=None):
    """
    Command-line entry point.

    ``python -m huey.quarantine path.to.huey_instance list|show|replay|discard``

    A Huey instance may also be supplied directly (used by the test-suite), in
    which case the import path is omitted from ``argv``.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if stream is None:
        stream = sys.stdout

    if huey is None:
        if not argv:
            stream.write('Error: missing import path to Huey instance.\n')
            return 2
        path = argv.pop(0)
        try:
            huey = _load_huey(path)
        except Exception as exc:
            stream.write('Error importing %s: %s\n' % (path, type(exc).__name__))
            return 2

    parser = _build_parser()
    options = parser.parse_args(argv)

    if not getattr(huey, 'quarantine_enabled', False):
        stream.write('Error: quarantine is not enabled for this Huey '
                     'instance (pass quarantine=True to the storage).\n')
        return 2

    command = options.command
    if command == 'list':
        records = huey.quarantined(limit=options.limit)
        if not records:
            stream.write('No quarantined messages.\n')
        for record in records:
            stream.write(format_record(record) + '\n')
        return 0

    if options.record_id is None:
        stream.write('Error: %s requires a record id.\n' % command)
        return 2

    record_id = options.record_id
    if command == 'show':
        record = huey.get_quarantined(record_id)
        if record is None:
            stream.write('No quarantined message with id=%s.\n' % record_id)
            return 1
        stream.write(format_record(record) + '\n')
        return 0

    if command == 'replay':
        result = huey.replay_quarantined(record_id)
        if result.success:
            stream.write('Replayed id=%s -> %s (task=%s priority=%s).\n' % (
                record_id, result.destination, result.task_id,
                result.priority))
            return 0
        stream.write('Replay failed id=%s: %s\n' % (record_id, result.error))
        return 1

    # command == 'discard'
    if huey.discard_quarantined(record_id):
        stream.write('Discarded id=%s.\n' % record_id)
        return 0
    stream.write('No quarantined message with id=%s.\n' % record_id)
    return 1


if __name__ == '__main__':
    sys.exit(main())
