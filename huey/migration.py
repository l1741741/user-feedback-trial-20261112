"""Offline, transactional migration from FileHuey to SqliteHuey.

This module is intended for small deployments that can stop their consumers
while the backend is switched.  It copies the *raw serialized bytes* out of a
:class:`huey.storage.FileStorage` directory and into the SQLite tables used by
:class:`huey.storage.SqliteStorage`, so task ids, priorities, ETAs, chains and
chord associations are preserved exactly.

Application task functions are never imported or called.  To validate task
envelopes the bytes are decoded with a restricted unpickler that only knows a
small allow-list of primitive types and huey's own ``Message`` container.
Anything else becomes an inert placeholder, so a malicious pickle cannot run
code during validation, preview or import.

The ``sample`` command is the one exception: with ``--app`` it imports the
application module you name and uses the real serializer to show business
arguments.  That requires ``--allow-unsafe-pickle`` and should only be used
against a trusted source tree.

Quick start
===========

Consumers of both the source queue and the target queue must be stopped, and
the source directory must not be written to, for the whole procedure::

    python -m huey.migration check \\
        --source /srv/app/huey-data --destination /srv/app/huey.db \\
        --queue-name app --serialization pickle

    python -m huey.migration preview ... --json

    python -m huey.migration run ... --migration-id 20261120-cutover

    python -m huey.migration verify ...

    python -m huey.migration sample ... --limit 3
    python -m huey.migration sample ... --app myapp.huey --allow-unsafe-pickle

``python -m huey.migration demo`` runs a self-contained terminal demo against
a temporary FileHuey source and SqliteHuey target.

What is migrated
================

=========================  ==================================================
Source                     Destination
=========================  ==================================================
``queue/<pri>-<ms>``       ``task.data`` and ``task.priority``
``schedule/<ms>``          ``schedule.data`` and ``schedule.timestamp``
``results/**``             ``kv.key`` and ``kv.value`` (bytes unchanged)
``counters/md5(key)``      ``counter.key`` and ``counter.value`` (if the key
                           can be reconstructed)
=========================  ==================================================

Task priority is taken from the queue filename, because that is what
FileStorage itself uses to order deliveries; the envelope's own priority is
cross-checked and reported if they disagree.  Insertion order across the whole
queue is preserved, so ``order by priority desc, id`` in SQLite visits tasks in
the same order FileStorage's lexical filename scan would.

Source layout
=============

``queue/``
    ``%04x`` = ``0xffff - priority``, a dash, the enqueue time in milliseconds
    as ``%012x``, and an optional ``.%03d`` collision suffix.

``schedule/``
    ``%012x`` of the ETA in milliseconds, as produced by
    ``FileStorage._timestamp_to_prefix``, plus an optional collision suffix.

``results/``
    A hash tree of ``>I key_length | key | value`` files.  It holds task
    results (keyed by task id, a UUID), application values written with
    ``Huey.put()``, revocations (``r:<id>`` and ``rt:<module.Class>``), locks
    (``<queue>.lock.<name>``), rate-limit windows (``<queue>.rl.<name>.w``)
    and partial chord results (``chord:<cid>:<idx>``).

``counters/``
    Files named ``md5(key)`` containing a decimal integer.  Because the key is
    not retained, a counter is migrated only when its key can be reconstructed
    from stored data.  The rest are listed in the report and left behind.

Policies for data the tool cannot explain
=========================================

``--unrecognized=reject`` (default)
    Refuse the migration and list every key outside the recognized
    namespaces.

``--unrecognized=skip``
    Migrate nothing for those keys and list them in the report and manifest.

``--unrecognized=copy``
    Copy the bytes verbatim into ``kv`` without interpreting them.

``--locks=copy`` (default) / ``--locks=skip``
    Locks held at migration time.  Copying preserves ``TaskLock`` behaviour;
    skipping is appropriate when the old process is gone and its locks are
    stale.
"""

import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
import pickle
import pickletools
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
import urllib.parse
import zlib

from huey.registry import Message
from huey.utils import ChordConfig
from huey.utils import Error
from huey.utils import SKIPPED


MIGRATION_VERSION = 1
SNAPSHOT_VERSION = 1

# Migration metadata lives in its own table so that huey's result store
# (``kv``) is left exactly as the application had it: ``result_count()``,
# ``all_results()`` and ``flush_results()`` behave as before, and no
# migration bookkeeping can be mistaken for an application value.
META_TABLE = 'huey_migration_meta'
META_DDL = ('create table if not exists %s ('
            'queue text not null, key text not null, value text not null, '
            'primary key(queue, key))' % META_TABLE)
META_ID = 'migration_id'
META_FINGERPRINT = 'source_fingerprint'
META_DIGEST = 'integrity_digest'
META_MANIFEST = 'manifest'

QUEUE_DIR = 'queue'
SCHEDULE_DIR = 'schedule'
RESULTS_DIR = 'results'
COUNTERS_DIR = 'counters'
LOCK_FILE = '.lock'

METRICS = ('task', 'schedule', 'result', 'revocation', 'lock',
           'rate_window', 'chord_result', 'chord_counter', 'counter')

KIND_RESULT = 'result'
KIND_REVOCATION = 'revocation'
KIND_LOCK = 'lock'
KIND_RATE_WINDOW = 'rate_window'
KIND_CHORD_RESULT = 'chord_result'
KIND_CHORD_COUNTER = 'chord_counter'
KIND_OPAQUE = 'opaque'

POLICY_REJECT = 'reject'
POLICY_SKIP = 'skip'
POLICY_COPY = 'copy'
UNRECOGNIZED_POLICIES = (POLICY_REJECT, POLICY_SKIP, POLICY_COPY)
LOCK_POLICIES = (POLICY_COPY, POLICY_SKIP)
COUNTER_POLICIES = (POLICY_REJECT, POLICY_SKIP)
SERIALIZATIONS = ('pickle', 'gzip', 'zlib')

__all__ = [
    'MigrationError', 'SourceChanged', 'InvalidOptions', 'UnrecognizedTask',
    'SourceSnapshot', 'Analysis', 'MigrationPlan', 'import_plan',
    'verify_plan', 'sample_records', 'destination_state', 'classify_key',
    'safe_loads', 'decode_message', 'main', 'run_demo',
]


class MigrationError(Exception):
    """Any problem that prevents a safe migration."""


class SourceChanged(MigrationError):
    """The source tree changed while it was being snapshotted."""


class InvalidOptions(MigrationError):
    """Command-line options or configuration are invalid."""


class UnrecognizedTask(MigrationError):
    """A task payload could not be decoded unambiguously."""


# ---------------------------------------------------------------------------
# Serialization configuration.
# ---------------------------------------------------------------------------

def describe_serialization(serialization):
    return {
        'pickle': 'pickle (no compression)',
        'gzip': 'pickle + gzip compression',
        'zlib': 'pickle + zlib compression',
    }[serialization]


def _decompress(serialization, data):
    if serialization == 'gzip':
        return gzip.decompress(data)
    if serialization == 'zlib':
        return zlib.decompress(data)
    return data


def _looks_compressed(data):
    # gzip and zlib both start with a header byte in this set; no pickle
    # protocol 0/1 opcode or protocol 2+ PROTO byte collides with it.
    return bool(data) and (data[0] == 0x1f or data[0] == 0x78)


# ---------------------------------------------------------------------------
# Restricted unpickling.
# ---------------------------------------------------------------------------

class _Opaque(object):
    """Inert placeholder for an object the restricted unpickler won't load.

    Construction arguments and state are ignored, so ``NEWOBJ``/``BUILD``/
    ``REDUCE`` cannot trigger user code.
    """
    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        return object.__new__(cls)

    def __init__(self, *args, **kwargs):
        pass

    def __setstate__(self, state):
        pass

    def __repr__(self):
        return '<opaque>'


class _SafeDateTime(datetime.datetime):
    """``datetime.datetime`` tolerant of out-of-range state in old pickles."""
    __slots__ = ()

    def __new__(cls, *args, **kwargs):
        try:
            return datetime.datetime.__new__(cls, *args, **kwargs)
        except Exception:
            kwargs.pop('tzinfo', None)
            try:
                return datetime.datetime.__new__(cls, *args, **kwargs)
            except Exception:
                return datetime.datetime.__new__(cls, 1970, 1, 1)

    def __reduce__(self):
        return (datetime.datetime, datetime.datetime.__reduce__(self)[1],
                self.tzinfo)


_ALLOWED_GLOBALS = {
    ('huey.registry', 'Message'): Message,
    ('huey.utils', 'ChordConfig'): ChordConfig,
    ('huey.utils', 'Error'): Error,
    ('huey.utils', 'SKIPPED'): lambda: SKIPPED,
    ('builtins', 'tuple'): tuple,
    ('__builtin__', 'tuple'): tuple,
    ('builtins', 'set'): set,
    ('__builtin__', 'set'): set,
    ('builtins', 'frozenset'): frozenset,
    ('__builtin__', 'frozenset'): frozenset,
    ('builtins', 'bytearray'): bytearray,
    ('__builtin__', 'bytearray'): bytearray,
    ('builtins', 'complex'): complex,
    ('__builtin__', 'complex'): complex,
    ('_codecs', 'encode'): lambda value, encoding: value.encode(encoding),
    ('datetime', 'datetime'): _SafeDateTime,
    ('datetime', 'date'): datetime.date,
    ('datetime', 'time'): datetime.time,
    ('datetime', 'timedelta'): datetime.timedelta,
    ('datetime', 'timezone'): datetime.timezone,
}


def _safe_reconstructor(cls, base, state):
    if base is tuple:
        try:
            return tuple.__new__(cls, state)
        except Exception:
            return _Opaque()
    return _Opaque()


class RestrictedUnpickler(pickle.Unpickler):
    """Unpickler that never imports application modules or calls their code.

    Only a fixed allow-list of globals is loaded.  Anything else becomes an
    :class:`_Opaque` placeholder and is recorded.  Persistent ids and
    extension codes are rejected outright.
    """
    def __init__(self, file_obj):
        super(RestrictedUnpickler, self).__init__(file_obj)
        self.unknown = []

    def find_class(self, module, name):
        if module in ('copy_reg', 'copyreg') and name == '_reconstructor':
            return _safe_reconstructor
        key = (module, name)
        if key in _ALLOWED_GLOBALS:
            return _ALLOWED_GLOBALS[key]
        self.unknown.append('%s.%s' % (module, name))
        return _Opaque

    def persistent_load(self, pid):
        raise pickle.UnpicklingError('persistent ids are not supported')

    def get_extension(self, code):
        raise pickle.UnpicklingError('extension codes are not supported')


def _pickle_end(data):
    """Offset just past the STOP opcode, or ``None`` when malformed."""
    try:
        for opcode, arg, pos in pickletools.genops(data):
            if opcode.name == 'STOP':
                return pos + 1
    except Exception:
        return None
    return None


def safe_loads(data):
    """Decode *data* without importing arbitrary modules or calling code.

    Returns ``(obj, unknown_globals)``.  ``obj`` may contain :class:`_Opaque`
    where the original pickle referenced an object outside the allow-list.
    Trailing data after the STOp opcode is rejected, since it would make the
    payload ambiguous.
    """
    if not data:
        raise UnrecognizedTask('empty payload')
    end = _pickle_end(data)
    if end is None:
        raise UnrecognizedTask('not a complete pickle (no STOP opcode)')
    if end != len(data):
        raise UnrecognizedTask('trailing data after pickle')
    unpickler = RestrictedUnpickler(io.BytesIO(data))
    obj = unpickler.load()
    return obj, tuple(sorted(set(unpickler.unknown)))


_STRING_OPCODES = ('SHORT_BINUNICODE', 'BINUNICODE', 'UNICODE',
                   'BINUNICODE8', 'SHORT_BINSTRING', 'BINSTRING', 'STRING')


def scan_globals(data):
    """List globals a pickle references, without evaluating it.

    ``pickletools.genops`` is a pure parser: no object construction, no
    imports.  For protocol 4+ pickles, which use ``STACK_GLOBAL``, the module
    and name are recovered from the two string constants that precede it.
    Intended for operators deciding whether ``--app`` is safe.
    """
    names = set()
    recent = []
    try:
        for opcode, arg, pos in pickletools.genops(data):
            if opcode.name == 'GLOBAL':
                names.add(arg)
            elif opcode.name in _STRING_OPCODES and isinstance(arg, str):
                recent.append(arg)
                del recent[:-2]
            elif opcode.name == 'STACK_GLOBAL':
                if len(recent) == 2:
                    names.add('%s.%s' % (recent[0], recent[1]))
                else:
                    names.add('<stack-global>')
                recent = []
            elif opcode.name == 'MEMOIZE':
                pass  # MEMOIZE follows each string without adding a value.
            else:
                recent = []
    except Exception:
        return None
    return sorted(names)


_DANGEROUS_MODULES = ('os', 'posix', 'subprocess', 'shutil', 'socket',
                      'ctypes', 'importlib', 'sys', 'builtins',
                      '__builtin__', 'runpy', 'pty', 'pickle')


def dangerous_globals(names):
    """Which referenced globals look dangerous to unpickle for real."""
    if not names:
        return []
    risky = []
    for name in names:
        module = name.split('.', 1)[0]
        if module in _DANGEROUS_MODULES:
            risky.append(name)
    return risky


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------

def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, default=_json_default)


def _json_default(obj):
    if isinstance(obj, bytes):
        return {'__bytes__': obj.hex()}
    if isinstance(obj, datetime.datetime):
        return {'__datetime__': obj.isoformat()}
    if isinstance(obj, (_Opaque, Message)):
        return {'__opaque__': True}
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=repr)
    return repr(obj)


def _md5_hex(key):
    return hashlib.md5(_as_bytes(key)).hexdigest()


def _as_bytes(value):
    return value if isinstance(value, bytes) else str(value).encode('utf8')


def _as_text(value):
    if isinstance(value, bytes):
        try:
            return value.decode('utf8')
        except UnicodeDecodeError:
            return None
    return None if value is None else str(value)


def _unpack_kv(raw):
    """Parse a FileStorage result file: ``>I key_len | key | value``."""
    if len(raw) < 4:
        return None, None
    key_len, = struct.unpack('>I', raw[:4])
    if key_len == 0 or len(raw) < 4 + key_len:
        return None, None
    return raw[4:4 + key_len], raw[4 + key_len:]


UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
QUEUE_RE = re.compile(
    r'^(?P<pri>[0-9a-f]{4})-(?P<ts>[0-9a-f]{12})(?P<suffix>\.[0-9]{3})?$')
SCHEDULE_RE = re.compile(r'^(?P<ts>[0-9a-f]{12})(?P<suffix>\.[0-9]{3})?$')
COUNTER_RE = re.compile(r'^[0-9a-f]{32}$')
RESULT_FILE_RE = re.compile(r'^(?:[0-9a-f]/)*[0-9a-f]{32}$')


def classify_key(key):
    """Classify a FileStorage key/value key.

    Returns ``result``, ``revocation``, ``lock``, ``rate_window``,
    ``chord_result``, ``chord_counter`` or ``opaque``.  ``opaque`` means the
    key is outside every namespace this tool can explain; such keys require an
    explicit ``--unrecognized`` policy.
    """
    if key.startswith('rt:') or key.startswith('r:'):
        return KIND_REVOCATION
    if key.startswith('chord:'):
        return (KIND_CHORD_RESULT if ':' in key[len('chord:'):]
                else KIND_CHORD_COUNTER)
    if '.lock.' in key:
        return KIND_LOCK
    if '.rl.' in key and key.endswith('.w'):
        return KIND_RATE_WINDOW
    if UUID_RE.match(key):
        return KIND_RESULT
    return KIND_OPAQUE


def infer_queue_names(key_strings):
    """Infer FileStorage's queue name from keys that embed it.

    FileStorage does not record ``Huey.name`` for queue or schedule entries,
    but lock and rate-limit keys do (``<name>.lock.<x>``, ``<name>.rl.<x>.w``).
    """
    names = set()
    for key in key_strings:
        if '.lock.' in key:
            names.add(key.split('.lock.', 1)[0])
        elif '.rl.' in key:
            names.add(key.split('.rl.', 1)[0])
    return names


# ---------------------------------------------------------------------------
# Source snapshot.
# ---------------------------------------------------------------------------

class SourceItem(object):
    __slots__ = ('kind', 'filename', 'data', 'priority', 'eta_ms', 'order')

    def __init__(self, kind, filename, data, priority=None, eta_ms=None,
                 order=0):
        self.kind = kind
        self.filename = filename
        self.data = data
        self.priority = priority
        self.eta_ms = eta_ms
        self.order = order

    @property
    def digest(self):
        return sha256_hex(self.data)


class SourceSnapshot(object):
    """Read-only in-memory snapshot of a FileStorage directory.

    Each file is read twice and the directory listing is taken before and
    after, so a snapshot never mixes bytes from different moments.  Any
    inconsistency raises :class:`SourceChanged`.
    """
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.queue_dir = os.path.join(self.path, QUEUE_DIR)
        self.schedule_dir = os.path.join(self.path, SCHEDULE_DIR)
        self.results_dir = os.path.join(self.path, RESULTS_DIR)
        self.counters_dir = os.path.join(self.path, COUNTERS_DIR)
        self.queue = []
        self.schedule = []
        self.kv = []          # (key_bytes, value_bytes, sha256(file))
        self.counters = []    # (md5_filename, value, sha256(file))
        self.unreadable = []  # (relpath, reason)
        self._take()

    # -- listing ---------------------------------------------------------
    def _listing(self):
        """Map of relative path -> [size, mtime_ns], or [None, error]."""
        listing = {}
        for root, dirs, files in os.walk(self.path):
            rel = os.path.relpath(root, self.path)
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                key = os.path.normpath(os.path.join(rel, name))
                try:
                    stat = os.stat(full)
                except OSError as exc:
                    listing[key] = [None, str(exc)]
                    continue
                listing[key] = [stat.st_size, stat.st_mtime_ns]
        return listing

    def _read(self, full):
        try:
            with open(full, 'rb') as fh:
                return fh.read()
        except OSError as exc:
            return exc

    def _read_stable(self, full):
        first = self._read(full)
        if isinstance(first, OSError):
            return None, str(first)
        second = self._read(full)
        if isinstance(second, OSError):
            return None, str(second)
        if first != second:
            return None, 'file changed while being read'
        return first, None

    def _rel(self, full):
        return os.path.relpath(full, self.path)

    def _take(self):
        if not os.path.isdir(self.path):
            raise InvalidOptions(
                'source path %r is not a directory' % self.path)
        before = self._listing()
        self._read_queue()
        self._read_schedule()
        self._read_results()
        self._read_counters()
        after = self._listing()
        if before != after:
            changed = sorted(
                set(before) ^ set(after) or
                {name for name in before if before[name] != after[name]})
            raise SourceChanged(
                'source changed while snapshotting (%s)' % (
                    ', '.join(changed[:10]) or 'file metadata changed'))
        # The fingerprint tracks names and sizes only. Bytes are covered by
        # per-file digests, so a restore that preserves content but resets
        # mtimes is still the same source, while the within-snapshot check
        # above (which does include mtime) still catches live writers. The
        # .lock file is live FileStorage guard state, not queue data.
        self._listing_digest = sha256_hex(canonical_json(
            [[name, after[name][0]] for name in sorted(after)
             if name != LOCK_FILE]).encode('utf8'))

    # -- readers ---------------------------------------------------------
    def _iter_files(self, directory):
        if not os.path.isdir(directory):
            return
        for name in sorted(os.listdir(directory)):
            full = os.path.join(directory, name)
            if not os.path.isfile(full):
                self.unreadable.append((self._rel(full),
                                        'not a regular file'))
                continue
            if name.endswith('.tmp'):
                self.unreadable.append((self._rel(full),
                                        'unfinished temporary file'))
                continue
            yield name, full

    def _read_queue(self):
        for order, (name, full) in enumerate(self._iter_files(self.queue_dir)):
            match = QUEUE_RE.match(name)
            if not match:
                self.unreadable.append((self._rel(full),
                                        'unexpected queue filename'))
                continue
            data, error = self._read_stable(full)
            if error is not None:
                self.unreadable.append((self._rel(full), error))
                continue
            self.queue.append(SourceItem(
                'task', name, data,
                priority=0xffff - int(match.group('pri'), 16), order=order))

    def _read_schedule(self):
        for order, (name, full) in enumerate(
                self._iter_files(self.schedule_dir)):
            match = SCHEDULE_RE.match(name)
            if not match:
                self.unreadable.append((self._rel(full),
                                        'unexpected schedule filename'))
                continue
            data, error = self._read_stable(full)
            if error is not None:
                self.unreadable.append((self._rel(full), error))
                continue
            self.schedule.append(SourceItem(
                'schedule', name, data,
                eta_ms=int(match.group('ts'), 16), order=order))

    def _read_results(self):
        if not os.path.isdir(self.results_dir):
            return
        for root, dirs, files in os.walk(self.results_dir):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, self.results_dir)
                if name.endswith('.tmp'):
                    self.unreadable.append((self._rel(full),
                                            'unfinished temporary result'))
                    continue
                if not RESULT_FILE_RE.match(rel.replace(os.sep, '/')):
                    self.unreadable.append((self._rel(full),
                                            'unexpected result filename'))
                    continue
                data, error = self._read_stable(full)
                if error is not None:
                    self.unreadable.append((self._rel(full), error))
                    continue
                key, value = _unpack_kv(data)
                if key is None:
                    self.unreadable.append((self._rel(full),
                                            'malformed result file'))
                    continue
                self.kv.append((key, value, sha256_hex(data)))

    def _read_counters(self):
        for name, full in self._iter_files(self.counters_dir):
            if not COUNTER_RE.match(name):
                self.unreadable.append((self._rel(full),
                                        'unexpected counter filename'))
                continue
            data, error = self._read_stable(full)
            if error is not None:
                self.unreadable.append((self._rel(full), error))
                continue
            try:
                value = int(data.decode('ascii').strip())
            except Exception:
                self.unreadable.append((self._rel(full),
                                        'counter is not a decimal integer'))
                continue
            self.counters.append((name, value, sha256_hex(data)))

    # -- fingerprint -----------------------------------------------------
    def assert_unchanged(self):
        """Raise :class:`SourceChanged` if the tree no longer matches.

        Called immediately before the destination transaction begins, so a
        source that was written to after the snapshot is never imported under
        a stale fingerprint.
        """
        try:
            current = SourceSnapshot(self.path)
        except SourceChanged:
            raise
        if current.fingerprint() != self.fingerprint():
            raise SourceChanged(
                'source changed after the snapshot was taken; re-run the '
                'migration once no consumer is writing to it')

    def fingerprint(self):
        """Content fingerprint of everything the migration could move.

        Order-sensitive: queue order is delivery order.
        """
        payload = [
            ['version', SNAPSHOT_VERSION],
            ['queue', [[item.filename, item.priority, item.digest]
                       for item in self.queue]],
            ['schedule', [[item.filename, item.eta_ms, item.digest]
                          for item in self.schedule]],
            ['kv', [[key.decode('utf8', 'backslashreplace'), digest]
                    for key, value, digest in self.kv]],
            ['counters', [[name, value, digest]
                          for name, value, digest in self.counters]],
            ['unreadable', self.unreadable],
            ['listing', self._listing_digest],
        ]
        return sha256_hex(canonical_json(payload).encode('utf8'))


# ---------------------------------------------------------------------------
# Decoding and validating task envelopes.
# ---------------------------------------------------------------------------

def decode_message(data, serialization='pickle'):
    """Decode a task envelope safely.  Returns ``(Message, unknown_globals)``.

    Raises :class:`UnrecognizedTask` with a precise reason otherwise.
    """
    if serialization != 'pickle':
        if not data:
            raise UnrecognizedTask('empty payload')
        if not _looks_compressed(data):
            raise UnrecognizedTask(
                'serialization is %r but the payload is not compressed' %
                serialization)
        try:
            raw = _decompress(serialization, data)
        except Exception as exc:
            raise UnrecognizedTask('cannot decompress payload: %s' % exc)
    else:
        if _looks_compressed(data):
            raise UnrecognizedTask(
                'payload looks compressed but serialization is "pickle"')
        raw = data

    try:
        obj, unknown = safe_loads(raw)
    except Exception as exc:
        raise UnrecognizedTask('cannot unpickle safely: %s' % exc)
    if not isinstance(obj, Message):
        raise UnrecognizedTask(
            'payload is %s, not huey.registry.Message' % type(obj).__name__)
    return obj, unknown


def validate_message(msg, where, depth=0):
    """Validate the envelope fields the migration relies on."""
    if depth > 50:
        raise UnrecognizedTask('%s: task chain is too deep' % where)
    if not isinstance(msg, Message):
        raise UnrecognizedTask('%s: nested payload is not a Message' % where)
    if not isinstance(msg.id, str) or not msg.id:
        raise UnrecognizedTask('%s: missing task id' % where)
    if not isinstance(msg.name, str) or not msg.name:
        raise UnrecognizedTask('%s: missing task name' % where)
    if msg.priority is not None:
        if isinstance(msg.priority, bool) or not isinstance(
                msg.priority, (int, float)):
            raise UnrecognizedTask('%s: invalid priority' % where)
    if msg.eta is not None and not isinstance(msg.eta, datetime.datetime):
        raise UnrecognizedTask('%s: invalid eta' % where)
    if not isinstance(msg.args, tuple):
        raise UnrecognizedTask('%s: args is not a tuple' % where)
    if not isinstance(msg.kwargs, dict):
        raise UnrecognizedTask('%s: kwargs is not a dict' % where)
    if msg.on_complete is not None:
        validate_message(msg.on_complete, where + '.on_complete', depth + 1)
    if msg.on_error is not None:
        validate_message(msg.on_error, where + '.on_error', depth + 1)
    if msg.chord_config is not None:
        chord = msg.chord_config
        # Registry.create_message serializes the config as a 4-tuple
        # (cid, size, idx, callback Message); a ChordConfig may also appear
        # when a task object was pickled directly.
        if isinstance(chord, ChordConfig):
            cid, size, idx, callback = (
                chord.cid, chord.size, chord.idx, chord.callback)
        elif isinstance(chord, (tuple, list)) and len(chord) == 4:
            cid, size, idx, callback = chord
        else:
            raise UnrecognizedTask('%s: invalid chord config' % where)
        if not isinstance(cid, str) or not cid:
            raise UnrecognizedTask('%s: invalid chord id' % where)
        if not isinstance(size, int) or isinstance(size, bool):
            raise UnrecognizedTask('%s: invalid chord size' % where)
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise UnrecognizedTask('%s: invalid chord index' % where)
        if not isinstance(callback, Message):
            raise UnrecognizedTask('%s: invalid chord callback' % where)
        validate_message(callback, where + '.chord.callback', depth + 1)


def _reason(exc):
    if isinstance(exc, MigrationError):
        return str(exc)
    return '%s: %s' % (type(exc).__name__, exc)


def _eta_ms(msg, item, location):
    """Milliseconds for a schedule row, preferring the envelope's eta.

    FileStorage truncates the ETA to milliseconds with local-time helpers,
    which can differ from ``msg.eta.timestamp()`` by up to a second.  The
    envelope value is authoritative; the filename is a fallback and is
    cross-checked while analysing.
    """
    if isinstance(msg.eta, datetime.datetime):
        return int(round(msg.eta.timestamp() * 1000))
    return item.eta_ms



class TaskRecord(object):
    __slots__ = ('item', 'msg', 'unknown', 'location')

    def __init__(self, item, msg, unknown, location):
        self.item = item
        self.msg = msg
        self.unknown = unknown
        self.location = location

    @property
    def eta_ms(self):
        # See _eta_ms(): the envelope's eta is authoritative.
        return _eta_ms(self.msg, self.item, self.location)


# ---------------------------------------------------------------------------
# Analysis: structural view of the source, no business code involved.
# ---------------------------------------------------------------------------

class Analysis(object):
    def __init__(self, snapshot, serialization='pickle'):
        self.snapshot = snapshot
        self.serialization = serialization
        self.tasks = []             # queue TaskRecord, in delivery order
        self.scheduled = []         # schedule TaskRecord, soonest first
        self.bad_tasks = []         # (location, reason)
        self.bad_kv = []            # (key, reason)
        self.bad_counters = []      # (md5, value)
        self.task_names = set()
        self.task_ids = {}
        self.unknown_globals = set()
        self.records = []           # (kind, key, value, digest)
        self.kind_counts = {}
        self.opaque_keys = []
        self.revocation_keys = []
        self.lock_keys = []
        self.rate_keys = []
        self.chord_result_keys = []
        self.chord_counter_keys = []
        self.result_keys = []
        self.counter_values = {}
        self.warnings = []
        self.inferred_queue_names = set()
        self._analyse()

    # -- tasks -----------------------------------------------------------
    def _add_task(self, records, item, location):
        try:
            msg, unknown = decode_message(item.data, self.serialization)
            validate_message(msg, location)
        except Exception as exc:
            self.bad_tasks.append((location, _reason(exc)))
            return
        if msg.id in self.task_ids:
            self.bad_tasks.append(
                (location, 'duplicate task id %r (also at %s)' %
                 (msg.id, self.task_ids[msg.id])))
            return
        self.task_ids[msg.id] = location
        records.append(TaskRecord(item, msg, unknown, location))
        self.task_names.add(msg.name)
        self.unknown_globals.update(unknown)
        self._check_priority(item, msg, location)

    def _check_priority(self, item, msg, location):
        if item.priority is None:
            return
        expected = 0 if msg.priority is None else int(msg.priority)
        if expected != item.priority:
            self.warnings.append(
                '%s: envelope priority %r does not match queue filename '
                'priority %d; the filename (delivery order) wins' % (
                    location, msg.priority, item.priority))

    def _analyse_tasks(self):
        for item in self.snapshot.queue:
            self._add_task(self.tasks, item, QUEUE_DIR + '/' + item.filename)
        for item in self.snapshot.schedule:
            self._add_task(self.scheduled, item,
                           SCHEDULE_DIR + '/' + item.filename)
        for record in self.scheduled:
            if record.msg.eta is None:
                continue
            expected_ms = int(round(record.msg.eta.timestamp() * 1000))
            # FileStorage encodes the prefix with local-time helpers, which
            # can drift by a second; differences beyond that are reported.
            if abs(expected_ms - record.item.eta_ms) > 1000:
                self.warnings.append(
                    '%s: envelope eta %s does not match schedule filename '
                    'eta (%s); the envelope eta is used' % (
                        record.location, record.msg.eta,
                        datetime.datetime.fromtimestamp(
                            record.item.eta_ms / 1000.0)))

    # -- key/value -------------------------------------------------------
    def _decode_value(self, value):
        try:
            if self.serialization != 'pickle':
                if not _looks_compressed(value):
                    return False
                value = _decompress(self.serialization, value)
            safe_loads(value)
        except Exception:
            return False
        return True

    def _analyse_kv(self):
        seen = set()
        key_strings = []
        for key, value, digest in self.snapshot.kv:
            try:
                key_string = key.decode('utf8')
            except UnicodeDecodeError:
                self.bad_kv.append((key[:40], 'key is not valid UTF-8'))
                continue
            key_strings.append(key_string)
            if key_string in seen:
                self.bad_kv.append((key_string, 'duplicate key'))
                continue
            seen.add(key_string)
            kind = classify_key(key_string)
            if kind is None:
                self.bad_kv.append((key_string, 'reserved migration key'))
                continue
            if kind in (KIND_RESULT, KIND_CHORD_RESULT):
                if not self._decode_value(value):
                    self.bad_kv.append(
                        (key_string,
                         'value cannot be decoded with the given '
                         'serialization settings'))
                    continue
            self.records.append((kind, key_string, value, digest))
            self.kind_counts[kind] = self.kind_counts.get(kind, 0) + 1
            if kind == KIND_RESULT:
                self.result_keys.append(key_string)
            elif kind == KIND_REVOCATION:
                self.revocation_keys.append(key_string)
            elif kind == KIND_LOCK:
                self.lock_keys.append(key_string)
            elif kind == KIND_RATE_WINDOW:
                self.rate_keys.append(key_string)
            elif kind == KIND_CHORD_RESULT:
                self.chord_result_keys.append(key_string)
            elif kind == KIND_CHORD_COUNTER:
                self.chord_counter_keys.append(key_string)
            elif kind == KIND_OPAQUE:
                self.opaque_keys.append(key_string)
        self.inferred_queue_names = infer_queue_names(key_strings)

    # -- counters --------------------------------------------------------
    def _analyse_counters(self):
        by_md5 = {name: value for name, value, digest in
                  self.snapshot.counters}
        for key in self.chord_counter_keys:
            digest = _md5_hex(key)
            if digest in by_md5:
                self.counter_values[key] = by_md5[digest]
        for key in self.chord_result_keys:
            counter_key = 'chord:%s' % key[len('chord:'):].split(':', 1)[0]
            digest = _md5_hex(counter_key)
            if digest in by_md5:
                self.counter_values[counter_key] = by_md5[digest]
        for key in self.rate_keys:
            counter_key = key[:-2]  # Strip the trailing ".w".
            digest = _md5_hex(counter_key)
            if digest in by_md5:
                self.counter_values[counter_key] = by_md5[digest]
        mapped = {_md5_hex(key) for key in self.counter_values}
        for name, value, digest in self.snapshot.counters:
            if name not in mapped:
                self.bad_counters.append((name, value))

    # -- public ----------------------------------------------------------
    def _analyse(self):
        self._analyse_tasks()
        self._analyse_kv()
        self._analyse_counters()

    def counters_for_migration(self):
        return dict(sorted(self.counter_values.items()))

    def expected_metrics(self):
        metrics = {kind: 0 for kind in METRICS}
        metrics['task'] = len(self.tasks)
        metrics['schedule'] = len(self.scheduled)
        for kind in (KIND_RESULT, KIND_REVOCATION, KIND_LOCK,
                     KIND_RATE_WINDOW, KIND_CHORD_RESULT, KIND_CHORD_COUNTER):
            metrics[kind] = self.kind_counts.get(kind, 0)
        metrics['counter'] = len(self.counter_values)
        return metrics


# ---------------------------------------------------------------------------
# Destination inspection.
# ---------------------------------------------------------------------------

def sqlite_readonly_uri(path):
    return 'file:%s?mode=ro&immutable=1' % urllib.parse.quote(
        os.path.abspath(path))


def _zero_byte_destination_is_safe(path):
    """True if a zero-byte file was left by an aborted import.

    An earlier attempt can fail after ``begin`` + ``create table`` but before
    ``commit``; SQLite then removes the tables but the empty file remains.
    Only treat it as reusable if there is no migration metadata in it and it
    contains no bytes at all.  Any other file is reported, not overwritten.
    """
    try:
        return os.path.getsize(path) == 0
    except OSError:
        return False


def destination_state(path, queue_name=None):
    """Inspect an existing SQLite destination without modifying it.

    Opens read-only with ``immutable=1`` so SQLite does not create ``-wal`` or
    ``-shm`` files.  If sidecars already exist the database is assumed to be
    in use and is reported as such without being opened.
    """
    path = os.path.abspath(path)
    state = {
        'path': path,
        'exists': os.path.exists(path),
        'size': os.path.getsize(path) if os.path.exists(path) else None,
        'empty_file': False,
        'in_use': False,
        'tables': [],
        'counts': {},
        'strict_fifo': None,
        'journal_mode': None,
        'marker': None,
        'fingerprint': None,
        'digest': None,
        'meta_queues': [],
        'reason': None,
        'fresh': False,
    }
    if not state['exists']:
        state['fresh'] = True
        state['reason'] = 'destination does not exist'
        return state
    if state['size'] == 0:
        state['fresh'] = True
        state['empty_file'] = True
        state['reason'] = 'destination is a zero-byte file'
        return state

    sidecars = [suffix for suffix in ('-wal', '-shm')
                if os.path.exists(path + suffix)]
    if sidecars:
        state['in_use'] = True
        state['reason'] = (
            'destination has SQLite sidecar files (%s); stop consumers and '
            'close the database before migrating' %
            ', '.join('*' + suffix for suffix in sidecars))
        return state

    try:
        conn = sqlite3.connect(sqlite_readonly_uri(path), uri=True)
    except sqlite3.Error as exc:
        state['reason'] = 'cannot open destination read-only: %s' % exc
        return state
    try:
        state['tables'] = [row[0] for row in conn.execute(
            "select name from sqlite_master where type='table' "
            "order by name")]
        state['journal_mode'] = conn.execute(
            'pragma journal_mode').fetchone()[0]
        for table in ('task', 'schedule', 'kv', 'counter'):
            if table in state['tables']:
                state['counts'][table] = conn.execute(
                    'select count(*) from %s' % table).fetchone()[0]
        if 'task' in state['tables']:
            row = conn.execute(
                "select sql from sqlite_master where type='table' "
                "and name='task'").fetchone()
            state['strict_fifo'] = bool(
                row and row[0] and 'AUTOINCREMENT' in row[0].upper())
        if META_TABLE in state['tables']:
            rows = conn.execute(
                'select queue, key, value from %s order by queue, key' %
                META_TABLE).fetchall()
            by_queue = {}
            for row_queue, key, value in rows:
                by_queue.setdefault(row_queue, {})[key] = value
            state['meta_queues'] = sorted(by_queue)
            if queue_name is not None:
                meta = by_queue.get(queue_name, {})
            elif len(by_queue) == 1:
                meta = next(iter(by_queue.values()))
            else:
                meta = {}
            state['marker'] = _as_text(meta.get(META_ID))
            state['fingerprint'] = _as_text(meta.get(META_FINGERPRINT))
            state['digest'] = _as_text(meta.get(META_DIGEST))
            state['meta'] = meta
        state['fresh'] = False
    except sqlite3.Error as exc:
        state['reason'] = 'cannot inspect destination: %s' % exc
    finally:
        conn.close()
    return state


# ---------------------------------------------------------------------------
# Schema statements.
# ---------------------------------------------------------------------------

def schema_statements(strict_fifo=False):
    """The DDL SqliteStorage would run, plus the migration metadata table.

    The metadata table is separate from ``kv`` on purpose: huey's result store
    must keep behaving exactly as the application expects.
    """
    from huey.storage import SqliteStorage
    statements = list(SqliteStorage.ddl)
    if strict_fifo:
        # SqliteStorage replaces the "task" table DDL at index 3.
        statements[3] = SqliteStorage.table_task.replace(
            'primary key', 'primary key autoincrement')
    statements.append(META_DDL)
    return statements


_REQUIRED_SCHEMA = {
    'task': ('id', 'queue', 'data', 'priority'),
    'schedule': ('id', 'queue', 'data', 'timestamp'),
    'kv': ('queue', 'key', 'value'),
    'counter': ('queue', 'key', 'value'),
}


def check_schema(conn, strict_fifo=False):
    for table, columns in _REQUIRED_SCHEMA.items():
        rows = conn.execute('pragma table_info(%s)' % table).fetchall()
        if not rows:
            raise MigrationError('destination is missing table %r' % table)
        present = {row[1] for row in rows}
        missing = set(columns) - present
        if missing:
            raise MigrationError('table %r is missing columns %s' % (
                table, ', '.join(sorted(missing))))
    if strict_fifo:
        row = conn.execute(
            "select sql from sqlite_master where type='table' "
            "and name='task'").fetchone()
        if not (row and row[0] and 'AUTOINCREMENT' in row[0].upper()):
            raise MigrationError(
                'destination task table does not use AUTOINCREMENT, so '
                '--strict-fifo cannot be honoured; recreate the destination '
                'or drop --strict-fifo')


def create_schema(conn, strict_fifo=False):
    for statement in schema_statements(strict_fifo):
        conn.execute(statement)


# ---------------------------------------------------------------------------
# Migration plan.
# ---------------------------------------------------------------------------

def _priority(value):
    return float(value) if value is not None else 0.0


def build_digest_payload(queue_name, serialization, source_fingerprint,
                         tasks, schedule, kv, counters):
    """Canonical content description shared by source and destination.

    ``tasks`` and ``schedule`` are ordered lists; ``kv`` and ``counters`` are
    sorted so that filesystem walk order does not affect the digest.
    """
    return {
        'version': MIGRATION_VERSION,
        'queue_name': queue_name,
        'serialization': serialization,
        'source_fingerprint': source_fingerprint,
        'tasks': sorted(tasks),
        'schedule': sorted(schedule),
        'kv': sorted(kv),
        'counters': sorted(counters),
    }


def digest_of(payload):
    return sha256_hex(canonical_json(payload).encode('utf8'))


class MigrationPlan(object):
    def __init__(self, source, destination, queue_name,
                 serialization='pickle', migration_id=None,
                 unrecognized=POLICY_REJECT, locks=POLICY_COPY,
                 strict_fifo=False, counters=POLICY_REJECT):
        if serialization not in SERIALIZATIONS:
            raise InvalidOptions('serialization must be one of %s' %
                                 ', '.join(SERIALIZATIONS))
        if unrecognized not in UNRECOGNIZED_POLICIES:
            raise InvalidOptions('--unrecognized must be one of %s' %
                                 ', '.join(UNRECOGNIZED_POLICIES))
        if locks not in LOCK_POLICIES:
            raise InvalidOptions('--locks must be one of %s' %
                                 ', '.join(LOCK_POLICIES))
        if counters not in COUNTER_POLICIES:
            raise InvalidOptions('--counters must be one of %s' %
                                 ', '.join(COUNTER_POLICIES))
        if not queue_name or not isinstance(queue_name, str):
            raise InvalidOptions('queue name must be a non-empty string')

        self.source_path = os.path.abspath(source)
        self.destination_path = os.path.abspath(destination)
        self.queue_name = queue_name
        self.serialization = serialization
        self.unrecognized = unrecognized
        self.locks = locks
        self.counters = counters
        self.strict_fifo = bool(strict_fifo)
        self.blockers = []
        self.warnings = []

        self.snapshot = SourceSnapshot(self.source_path)
        self.analysis = Analysis(self.snapshot, serialization)
        self.source_fingerprint = self.snapshot.fingerprint()
        self.migration_id = migration_id or (
            'file-to-sqlite-%s-%s' % (
                time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()),
                self.source_fingerprint[:12]))
        self.destination = destination_state(self.destination_path,
                                             self.queue_name)
        self._evaluate()
        self.integrity_digest = self._compute_integrity_digest()

    # -- evaluation ------------------------------------------------------
    def _evaluate(self):
        analysis = self.analysis
        self._evaluate_queue_identity()
        self._evaluate_serialization()
        self._evaluate_unknown_data()
        self._evaluate_destination()
        self.warnings.extend(analysis.warnings)

    def _evaluate_queue_identity(self):
        names = set(self.analysis.inferred_queue_names)
        self.queue_identity = {
            'name': self.queue_name,
            'evidence': 'command line',
        }
        if len(names) > 1:
            self.blockers.append(
                'lock/rate-limit keys imply multiple queue names (%s); the '
                'source is ambiguous' % ', '.join(sorted(names)))
        elif names and self.queue_name not in names:
            self.blockers.append(
                'queue name %r does not match the name recorded in source '
                'lock/rate-limit keys (%s)' % (
                    self.queue_name, ', '.join(sorted(names))))
        elif names:
            self.queue_identity['evidence'] = 'lock/rate-limit keys'
        else:
            self.warnings.append(
                'FileStorage does not record the queue name for queue or '
                'schedule entries; %r was taken from the command line and '
                'is not independently verifiable in this source' %
                self.queue_name)

    def _evaluate_serialization(self):
        sample = (self.snapshot.queue[:1] or self.snapshot.schedule[:1])
        if not sample:
            return
        looks = _looks_compressed(sample[0].data)
        if self.serialization == 'pickle' and looks:
            self.blockers.append(
                'source payloads look compressed; --serialization pickle is '
                'wrong (use gzip or zlib)')
        elif self.serialization != 'pickle' and not looks:
            self.blockers.append(
                'source payloads are not compressed; --serialization %s is '
                'wrong' % self.serialization)

    def _evaluate_unknown_data(self):
        analysis = self.analysis
        for location, reason in analysis.bad_tasks:
            self.blockers.append(
                'undecodable task %s: %s' % (location, reason))
        for key, reason in analysis.bad_kv:
            self.blockers.append(
                'undecodable record %r: %s' % (key, reason))
        for rel, reason in self.snapshot.unreadable:
            self.blockers.append(
                'unreadable file %s: %s' % (rel, reason))

        listed = ', '.join(repr(key) for key in analysis.opaque_keys[:10])
        if analysis.opaque_keys:
            if self.unrecognized == POLICY_REJECT:
                self.blockers.append(
                    '%d key(s) are outside the recognized result/revocation/'
                    'lock/rate/chord namespaces: %s; choose '
                    '--unrecognized=skip or --unrecognized=copy' % (
                        len(analysis.opaque_keys), listed))
            elif self.unrecognized == POLICY_SKIP:
                self.warnings.append(
                    '%d unexplained key(s) will stay in the source: %s' % (
                        len(analysis.opaque_keys), listed))
        if analysis.bad_counters:
            listed = ', '.join(name for name, value in
                               analysis.bad_counters[:10])
            if self.counters == POLICY_REJECT:
                self.blockers.append(
                    '%d counter file(s) cannot be attributed to a key: '
                    'FileStorage stores counters as md5(key) only, and no '
                    'chord/rate-limit key in this source hashes to them: %s. '
                    'Inspect them, or pass --counters=skip to leave them in '
                    'the source (they are then listed in the manifest).' % (
                        len(analysis.bad_counters), listed))
            else:
                self.warnings.append(
                    '%d unattributable counter file(s) will stay in the '
                    'source: %s' % (len(analysis.bad_counters), listed))
        if analysis.lock_keys and self.locks == POLICY_SKIP:
            self.warnings.append(
                '%d lock key(s) will not be migrated: %s' % (
                    len(analysis.lock_keys),
                    ', '.join(analysis.lock_keys[:10])))
        if os.path.exists(os.path.join(self.source_path, LOCK_FILE)):
            self.warnings.append(
                'source lock file %s is not migrated; it only guards live '
                'FileStorage access' % LOCK_FILE)

    def _evaluate_destination(self):
        dest = self.destination
        if dest['in_use']:
            self.blockers.append(dest['reason'])
            return
        if dest['fresh']:
            return
        if dest['reason'] and not dest['tables']:
            self.blockers.append(dest['reason'])
            return
        missing = set(_REQUIRED_SCHEMA) - set(dest['tables'])
        if missing:
            self.blockers.append(
                'destination is missing tables: %s' % ', '.join(
                    sorted(missing)))
            return
        if META_TABLE not in dest['tables']:
            non_empty = {table: count for table, count in
                         dest['counts'].items() if count}
            if non_empty or dest['marker']:
                self.blockers.append(
                    'destination already contains rows (%s) but was not '
                    'created by this migration tool; use a fresh database' %
                    ', '.join('%s=%d' % (table, count) for table, count in
                              sorted(non_empty.items())))
            else:
                self.warnings.append(
                    'destination is an empty SqliteHuey database; the '
                    'migration metadata table will be added inside the '
                    'import transaction')
        if self.strict_fifo and dest['strict_fifo'] is False:
            self.blockers.append(
                'destination task table does not use AUTOINCREMENT (strict '
                'FIFO); recreate the destination or drop --strict-fifo')

        marker = dest['marker']
        if not marker and dest['meta_queues']:
            self.blockers.append(
                'destination holds migration metadata for another queue '
                '(%s); refusing to mix queues in one database' % ', '.join(
                    repr(name) for name in dest['meta_queues']))
            return
        if marker and marker != self.migration_id:
            self.blockers.append(
                'destination already holds migration %r; refusing to mix '
                'migrations' % marker)
            return
        if marker == self.migration_id:
            if dest['fingerprint'] != self.source_fingerprint:
                self.blockers.append(
                    'migration id %r is already recorded in the destination '
                    'for a different source snapshot (recorded %s, current '
                    '%s). Refusing to treat them as one snapshot; use a new '
                    '--migration-id to migrate the new source.' % (
                        self.migration_id,
                        str(dest['fingerprint'])[:12],
                        self.source_fingerprint[:12]))
            return

        non_empty = {table: count for table, count in dest['counts'].items()
                     if count}
        if non_empty:
            self.blockers.append(
                'destination is not empty: %s' % ', '.join(
                    '%s=%d' % (table, count)
                    for table, count in sorted(non_empty.items())))

    # -- derived ---------------------------------------------------------
    def is_noop(self):
        """True when this exact migration is already in the destination.

        Deliberately independent of ``blockers``: verification knows the
        destination is already the migration's own output, so it can check
        its content without re-importing anything.
        """
        dest = self.destination
        return bool(
            not dest['fresh'] and
            dest['marker'] == self.migration_id and
            dest['fingerprint'] == self.source_fingerprint)

    def expected_metrics(self):
        return self.analysis.expected_metrics()

    def migrate_kv_records(self):
        """Yield ``(kind, key, value)`` for every key/value to be written."""
        for kind, key, value, digest in self.analysis.records:
            if kind == KIND_OPAQUE and self.unrecognized != POLICY_COPY:
                continue
            if kind == KIND_LOCK and self.locks == POLICY_SKIP:
                continue
            yield kind, key, value

    def _digest_payload(self, tasks, schedule, kv, counters):
        return build_digest_payload(
            self.queue_name, self.serialization, self.source_fingerprint,
            tasks, schedule, kv, counters)

    def source_digest_payload(self):
        tasks = [[sha256_hex(record.item.data),
                  _priority(record.item.priority)]
                 for record in self.analysis.tasks]
        schedule = [[sha256_hex(record.item.data), record.eta_ms]
                    for record in self.analysis.scheduled]
        kv = [[key, sha256_hex(value)] for kind, key, value
              in self.migrate_kv_records()]
        counters = [[key, value] for key, value in
                    self.analysis.counters_for_migration().items()]
        return self._digest_payload(tasks, schedule, kv, counters)

    def _compute_integrity_digest(self):
        return digest_of(self.source_digest_payload())

    # -- reporting -------------------------------------------------------
    def summary(self):
        analysis = self.analysis
        return {
            'ok': not self.blockers,
            'migration_id': self.migration_id,
            'queue_identity': self.queue_identity,
            'serialization': describe_serialization(self.serialization),
            'strict_fifo': self.strict_fifo,
            'source': self.source_path,
            'destination': self.destination_path,
            'source_fingerprint': self.source_fingerprint,
            'integrity_digest': self.integrity_digest,
            'policies': {'unrecognized': self.unrecognized,
                         'locks': self.locks,
                         'counters': self.counters},
            'counts': self.expected_metrics(),
            'unexplained_count': len(analysis.opaque_keys),
            'task_names': sorted(analysis.task_names),
            'skipped': {
                'unexplained_keys': (
                    analysis.opaque_keys
                    if self.unrecognized == POLICY_SKIP else []),
                'locks': (analysis.lock_keys
                          if self.locks == POLICY_SKIP else []),
                'counters': [{'md5': name, 'value': value}
                             for name, value in analysis.bad_counters],
            },
            'unrecognized_globals': sorted(analysis.unknown_globals),
            'blockers': list(self.blockers),
            'warnings': list(self.warnings),
            'destination_state': self.destination,
            'noop': self.is_noop(),
        }


# ---------------------------------------------------------------------------
# Import.
# ---------------------------------------------------------------------------

class _AlreadyImported(Exception):
    pass


def import_plan(plan):
    """Apply *plan* to the destination in one SQLite transaction.

    Either the whole snapshot is committed or nothing is.  Schema creation,
    tasks, schedule entries, key/value rows, counters and the manifest marker
    all share one transaction, so an interrupted import leaves no partially
    consumable batch behind.
    """
    if plan.blockers:
        raise MigrationError('; '.join(plan.blockers))
    if plan.is_noop():
        return {
            'status': 'already-imported',
            'migration_id': plan.migration_id,
            'integrity_digest': plan.integrity_digest,
            'counts': plan.expected_metrics(),
        }

    parent = os.path.dirname(plan.destination_path) or '.'
    if not os.path.isdir(parent):
        raise InvalidOptions(
            'destination directory %r does not exist' % parent)

    # Refuse to import a snapshot that the source has since outgrown.
    plan.snapshot.assert_unchanged()
    plan.destination = destination_state(plan.destination_path,
                                         plan.queue_name)
    if plan.destination['in_use']:
        raise MigrationError(plan.destination['reason'])
    created = plan.destination['fresh']
    if not created and plan.destination['empty_file']:
        # A zero-byte file left by an aborted earlier attempt. It carries no
        # data; reuse it as if it were new, inside the transaction.
        if not _zero_byte_destination_is_safe(plan.destination_path):
            raise MigrationError(
                'destination %r is not empty and is not a usable SQLite '
                'database' % plan.destination_path)
        created = True
    conn = sqlite3.connect(plan.destination_path, timeout=5,
                           isolation_level=None)
    try:
        conn.execute('begin immediate')
        try:
            if created:
                create_schema(conn, plan.strict_fifo)
            else:
                conn.execute(META_DDL)
            check_schema(conn, plan.strict_fifo)
            _recheck_empty(conn, plan)
            counts = _write_plan(conn, plan)
        except _AlreadyImported:
            conn.execute('rollback')
            return {
                'status': 'already-imported',
                'migration_id': plan.migration_id,
                'integrity_digest': plan.integrity_digest,
                'counts': plan.expected_metrics(),
            }
        except BaseException:
            conn.execute('rollback')
            raise
        conn.execute('commit')
    finally:
        conn.close()
    return {
        'status': 'imported',
        'created': created,
        'migration_id': plan.migration_id,
        'integrity_digest': plan.integrity_digest,
        'counts': counts,
    }


def _recheck_empty(conn, plan):
    """Re-check inside the transaction, to close the inspect/write race."""
    marker = _meta_get(conn, plan.queue_name, META_ID)
    if marker and marker != plan.migration_id:
        raise MigrationError(
            'destination was populated concurrently by migration %r' % marker)
    if marker == plan.migration_id:
        if _meta_get(conn, plan.queue_name, META_FINGERPRINT) != \
                plan.source_fingerprint:
            raise MigrationError(
                'destination was populated concurrently by a different '
                'snapshot of migration %r' % plan.migration_id)
        raise _AlreadyImported()
    for table in ('task', 'schedule', 'kv', 'counter'):
        if conn.execute('select 1 from %s limit 1' % table).fetchone():
            raise MigrationError(
                'destination was populated concurrently (%s is not empty)' %
                table)


def _meta_get(conn, queue_name, key):
    try:
        row = conn.execute(
            'select value from %s where queue = ? and key = ?' % META_TABLE,
            (queue_name, key)).fetchone()
    except sqlite3.Error:
        return None
    return _as_text(row[0]) if row else None


def _meta_put(conn, queue_name, key, value):
    conn.execute(
        'insert or replace into %s (queue, key, value) values (?, ?, ?)' %
        META_TABLE, (queue_name, key, value))


def _write_plan(conn, plan):
    analysis = plan.analysis
    name = plan.queue_name
    counts = {metric: 0 for metric in METRICS}

    base_id = conn.execute(
        'select coalesce(max(id), 0) from task').fetchone()[0]
    for offset, record in enumerate(analysis.tasks, 1):
        conn.execute(
            'insert into task (id, queue, data, priority) '
            'values (?, ?, ?, ?)',
            (base_id + offset, name, sqlite3.Binary(record.item.data),
             _priority(record.item.priority)))
        counts['task'] += 1

    for record in analysis.scheduled:
        conn.execute(
            'insert into schedule (queue, data, timestamp) '
            'values (?, ?, ?)',
            (name, sqlite3.Binary(record.item.data),
             record.eta_ms / 1000.0))
        counts['schedule'] += 1

    for kind, key, value in plan.migrate_kv_records():
        conn.execute(
            'insert or replace into kv (queue, key, value) values (?, ?, ?)',
            (name, key, sqlite3.Binary(value)))
        if kind in counts:
            counts[kind] += 1

    for key, value in analysis.counters_for_migration().items():
        conn.execute(
            'insert or replace into counter (queue, key, value) '
            'values (?, ?, ?)', (name, key, value))
        counts['counter'] += 1

    manifest = {
        'version': MIGRATION_VERSION,
        'migration_id': plan.migration_id,
        'queue_name': name,
        'queue_identity': plan.queue_identity,
        'serialization': plan.serialization,
        'strict_fifo': plan.strict_fifo,
        'source_path': plan.source_path,
        'source_fingerprint': plan.source_fingerprint,
        'integrity_digest': plan.integrity_digest,
        'policies': {'unrecognized': plan.unrecognized,
                     'locks': plan.locks,
                     'counters': plan.counters},
        'counts': counts,
        'skipped': {
            'unexplained_keys': (
                analysis.opaque_keys
                if plan.unrecognized == POLICY_SKIP else []),
            'locks': (analysis.lock_keys
                      if plan.locks == POLICY_SKIP else []),
            'counters': [{'md5': name, 'value': value}
                         for name, value in analysis.bad_counters],
        },
        'imported_at': datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
    }
    for key, value in ((META_ID, plan.migration_id),
                       (META_FINGERPRINT, plan.source_fingerprint),
                       (META_DIGEST, plan.integrity_digest),
                       (META_MANIFEST, canonical_json(manifest))):
        _meta_put(conn, name, key, value)
    return counts


# ---------------------------------------------------------------------------
# Verification.
# ---------------------------------------------------------------------------

def verify_plan(plan):
    """Compare the destination with the source snapshot, read-only.

    Row bytes, priorities, ETAs, counters and keys are compared directly, and
    an integrity digest is recomputed from the destination rows and compared
    with both the source-side digest and the manifest.  The destination is
    opened read-only and never written.
    """
    path = plan.destination_path
    if not os.path.exists(path):
        return {'ok': False, 'problems': ['destination does not exist'],
                'counts': {}, 'digest': None, 'manifest_digest': None}
    sidecars = [suffix for suffix in ('-wal', '-shm')
                if os.path.exists(path + suffix)]
    if sidecars:
        return {'ok': False,
                'problems': ['destination is open (SQLite sidecar files are '
                             'present); stop consumers before verifying'],
                'counts': {}, 'digest': None, 'manifest_digest': None}

    problems = []
    conn = sqlite3.connect(sqlite_readonly_uri(path), uri=True)
    try:
        check_schema(conn, False)
        if META_TABLE not in [row[0] for row in conn.execute(
                "select name from sqlite_master where type='table'")]:
            problems.append('destination has no migration metadata table')
        marker = _meta_get(conn, plan.queue_name, META_ID)
        fingerprint = _meta_get(conn, plan.queue_name, META_FINGERPRINT)
        recorded = _meta_get(conn, plan.queue_name, META_DIGEST)
        if marker != plan.migration_id:
            problems.append('migration marker missing or different')
        if fingerprint != plan.source_fingerprint:
            problems.append('source fingerprint does not match the manifest')
        if recorded != plan.integrity_digest:
            problems.append(
                'manifest integrity digest %s does not match the current '
                'source digest %s' % (str(recorded)[:12],
                                      plan.integrity_digest[:12]))

        task_rows = conn.execute(
            'select data, priority from task where queue = ? '
            'order by priority desc, id', (plan.queue_name,)).fetchall()
        expected_tasks = plan.analysis.tasks
        if len(task_rows) != len(expected_tasks):
            problems.append('task rows %d != expected %d' % (
                len(task_rows), len(expected_tasks)))
        else:
            for index, (row, record) in enumerate(zip(task_rows,
                                                      expected_tasks)):
                if bytes(row[0]) != record.item.data:
                    problems.append('task #%d bytes differ' % index)
                    break
                if float(row[1]) != _priority(record.item.priority):
                    problems.append('task #%d priority differs' % index)
                    break

        schedule_rows = conn.execute(
            'select data, timestamp from schedule where queue = ? '
            'order by timestamp, id', (plan.queue_name,)).fetchall()
        expected_schedule = plan.analysis.scheduled
        if len(schedule_rows) != len(expected_schedule):
            problems.append('schedule rows %d != expected %d' % (
                len(schedule_rows), len(expected_schedule)))
        else:
            for index, (row, record) in enumerate(zip(schedule_rows,
                                                      expected_schedule)):
                if bytes(row[0]) != record.item.data:
                    problems.append('schedule #%d bytes differ' % index)
                    break
                if int(round(float(row[1]) * 1000)) != record.eta_ms:
                    problems.append('schedule #%d ETA differs' % index)
                    break

        kv_rows = dict(conn.execute(
            'select key, value from kv where queue = ?',
            (plan.queue_name,)).fetchall())
        expected_kv = list(plan.migrate_kv_records())
        for kind, key, value in expected_kv:
            stored = kv_rows.get(key)
            if stored is None:
                problems.append('missing key %r' % key)
            elif bytes(stored) != value:
                problems.append('value differs for %r' % key)

        counter_rows = dict(conn.execute(
            'select key, value from counter where queue = ?',
            (plan.queue_name,)).fetchall())
        expected_counters = plan.analysis.counters_for_migration()
        for key, value in expected_counters.items():
            if counter_rows.get(key) != value:
                problems.append('counter %r differs' % key)

        destination_payload = build_digest_payload(
            plan.queue_name, plan.serialization, fingerprint,
            [[sha256_hex(bytes(row[0])), float(row[1])]
             for row in task_rows],
            [[sha256_hex(bytes(row[0])), int(round(float(row[1]) * 1000))]
             for row in schedule_rows],
            [[key, sha256_hex(bytes(value))] for key, value in kv_rows.items()],
            [[key, value] for key, value in counter_rows.items()])
        destination_digest = digest_of(destination_payload)
        if destination_digest != plan.integrity_digest:
            problems.append(
                'destination integrity digest %s does not match the source '
                'digest %s' % (destination_digest[:12],
                               plan.integrity_digest[:12]))

        counts = {metric: plan.expected_metrics()[metric] for metric in METRICS}
        counts['destination_task_rows'] = len(task_rows)
        counts['destination_schedule_rows'] = len(schedule_rows)
        counts['destination_kv_rows'] = len(kv_rows)
        counts['destination_counter_rows'] = len(counter_rows)
    except MigrationError as exc:
        problems.append(str(exc))
        destination_digest = None
        counts = {}
    finally:
        conn.close()
    return {'ok': not problems, 'problems': problems,
            'digest': destination_digest,
            'manifest_digest': plan.integrity_digest,
            'counts': counts}


# ---------------------------------------------------------------------------
# Sampling (explicit).
# ---------------------------------------------------------------------------

def sample_records(plan, limit=5, app_huey=None):
    """Return a small sample of tasks for human inspection.

    Without ``app_huey`` only the safely decoded envelope is shown.  With it,
    the application serializer and registry reconstruct real task objects and
    their business arguments.  The caller is responsible for requiring an
    explicit opt-in before passing ``app_huey``.
    """
    rows = []
    for kind, records in (('queue', plan.analysis.tasks),
                          ('schedule', plan.analysis.scheduled)):
        for record in records[:limit]:
            row = {
                'kind': kind,
                'filename': record.item.filename,
                'id': record.msg.id,
                'name': record.msg.name,
                'eta': (record.msg.eta.isoformat()
                        if record.msg.eta else None),
                'priority': record.item.priority,
                'envelope_priority': record.msg.priority,
                'retries': record.msg.retries,
                'chained': record.msg.on_complete is not None,
                'has_error_handler': record.msg.on_error is not None,
                'chord_cid': _chord_cid(record.msg),
                'opaque_globals': list(record.unknown),
                'referenced_globals': scan_globals(record.item.data),
            }
            if app_huey is not None:
                try:
                    task = app_huey.deserialize_task(record.item.data)
                    row['args'] = repr(task.args)
                    row['kwargs'] = repr(task.kwargs)
                    row['task_class'] = '%s.%s' % (
                        type(task).__module__, type(task).__name__)
                except Exception as exc:
                    row['args'] = row['kwargs'] = None
                    row['task_class'] = None
                    row['error'] = '%s: %s' % (type(exc).__name__, exc)
            else:
                row['args'] = repr(tuple(
                    _safe_repr(arg) for arg in record.msg.args))
                row['kwargs'] = repr({key: _safe_repr(value) for key, value
                                      in record.msg.kwargs.items()})
                row['task_class'] = None
            rows.append(row)
    return rows


def _chord_cid(msg):
    chord = msg.chord_config
    if chord is None:
        return None
    if isinstance(chord, ChordConfig):
        return chord.cid
    try:
        return chord[0]
    except Exception:
        return None


def _safe_repr(value, depth=0):
    if depth > 3:
        return '...'
    if isinstance(value, _Opaque):
        return '<opaque>'
    if isinstance(value, Message):
        return '<Message %s>' % (value.id,)
    if isinstance(value, (tuple, list)):
        return type(value)(_safe_repr(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return {key: _safe_repr(item, depth + 1)
                for key, item in value.items()}
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return value
    return '<%s>' % type(value).__name__


# ---------------------------------------------------------------------------
# Terminal rendering.
# ---------------------------------------------------------------------------

def render(report, as_json=False):
    """Print a report as JSON (``--json``) or a short human summary."""
    if as_json:
        print(canonical_json(report))
        return
    lines = []
    status = 'OK' if report.get('ok') else 'BLOCKED'
    lines.append('Status:      %s' % status)
    labels = (('migration_id', 'Migration'), ('queue_name', 'Queue'),
              ('serialization', 'Serializer'), ('status', 'Result'),
              ('source', 'Source'), ('destination', 'Destination'),
              ('source_fingerprint', 'Source hash'),
              ('integrity_digest', 'Integrity hash'))
    for key, label in labels:
        if key in report:
            lines.append('%-12s %s' % (label + ':', report[key]))
    if report.get('strict_fifo'):
        lines.append('FIFO:        strict (task ids AUTOINCREMENT)')
    identity = report.get('queue_identity')
    if identity:
        lines.append('Identity:    %r via %s' % (
            identity['name'], identity['evidence']))
    counts = report.get('counts')
    if counts:
        source_counts = {name: value for name, value in counts.items()
                         if not name.startswith('destination_')}
        if source_counts:
            lines.append('Counts:      %s' % ' '.join(
                '%s=%s' % (name, value)
                for name, value in sorted(source_counts.items())))
        destination_counts = {
            name[len('destination_'):-len('_rows')]: value
            for name, value in counts.items()
            if name.startswith('destination_')}
        if destination_counts:
            lines.append('Destination: %s' % ' '.join(
                '%s=%s' % (name, value)
                for name, value in sorted(destination_counts.items())))
    skipped = report.get('skipped')
    if skipped:
        parts = ['%s=%d' % (name, len(value))
                 for name, value in sorted(skipped.items()) if value]
        if parts:
            lines.append('Not moved:   %s' % ' '.join(parts))
    for name in ('blockers', 'problems', 'warnings'):
        for item in report.get(name, []):
            lines.append('%s: %s' % (name[:-1].capitalize(), item))
    samples = report.get('samples')
    if samples:
        for row in samples:
            lines.append('')
            lines.append('%s %s  id=%s  name=%s' % (
                row['kind'], row['filename'], row['id'], row['name']))
            lines.append('  eta=%s priority=%s retries=%s chained=%s '
                         'chord=%s' % (
                             row['eta'], row['priority'], row['retries'],
                             row['chained'], row['chord_cid']))
            lines.append('  args=%s' % (row['args'],))
            lines.append('  kwargs=%s' % (row['kwargs'],))
            if row.get('error'):
                lines.append('  error=%s' % row['error'])
            if row.get('opaque_globals'):
                lines.append('  opaque=%s' % ', '.join(
                    row['opaque_globals']))
            if row['referenced_globals']:
                lines.append('  globals=%s' % ', '.join(
                    row['referenced_globals'][:8]))
    print('\n'.join(lines))


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog='python -m huey.migration',
        description='Offline FileHuey -> SqliteHuey migration tool. Stop '
                    'consumers for both queues before running this.')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--source', required=True,
                        help='FileHuey base directory (never modified)')
    common.add_argument('--destination', required=True,
                        help='SqliteHuey database filename')
    common.add_argument('--queue-name', required=True,
                        help='Huey instance name for the queue')
    common.add_argument('--serialization', default='pickle',
                        choices=SERIALIZATIONS,
                        help='serializer configuration used by the source')
    common.add_argument('--migration-id', default=None,
                        help='stable identifier for this migration')
    common.add_argument('--unrecognized', default=POLICY_REJECT,
                        choices=UNRECOGNIZED_POLICIES,
                        help='policy for keys the tool cannot classify')
    common.add_argument('--locks', default=POLICY_COPY,
                        choices=LOCK_POLICIES,
                        help='policy for lock keys')
    common.add_argument('--counters', default=POLICY_REJECT,
                        choices=COUNTER_POLICIES,
                        help='policy for counter files whose key cannot be '
                             'reconstructed')
    common.add_argument('--strict-fifo', action='store_true',
                        help='create the task table with AUTOINCREMENT ids, '
                             'matching SqliteHuey(strict_fifo=True)')
    common.add_argument('--json', action='store_true',
                        help='emit a machine-readable report')

    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('check', parents=[common],
                   help='validate source and destination (no writes)')
    sub.add_parser('preview', parents=[common],
                   help='show counts, policies and conflicts (no writes)')
    sub.add_parser('run', parents=[common],
                   help='import the snapshot in a single transaction')
    sub.add_parser('verify', parents=[common],
                   help='compare destination with source (read-only)')

    sample = sub.add_parser('sample', parents=[common],
                            help='decode and print a few task payloads')
    sample.add_argument('--limit', type=int, default=5,
                        help='tasks per queue to show (default 5)')
    sample.add_argument('--app', default=None,
                        help='import path of the application Huey instance, '
                             'e.g. "myapp.huey"')
    sample.add_argument('--allow-unsafe-pickle', action='store_true',
                        help='required with --app: runs the real serializer '
                             'on business arguments')

    demo = sub.add_parser('demo',
                          help='run a self-contained terminal demonstration')
    demo.add_argument('--keep', action='store_true',
                      help='keep the temporary demo directory')
    return parser


def plan_from_args(args):
    return MigrationPlan(
        source=args.source,
        destination=args.destination,
        queue_name=args.queue_name,
        serialization=args.serialization,
        migration_id=args.migration_id,
        unrecognized=args.unrecognized,
        locks=args.locks,
        counters=args.counters,
        strict_fifo=args.strict_fifo)


def _load_app(path):
    # Importing the application is an explicit operator action and is only
    # done for "sample --app".
    from huey.utils import load_class
    return load_class(path)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == 'demo':
        return run_demo(keep=args.keep)

    try:
        plan = plan_from_args(args)
    except MigrationError as exc:
        render({'ok': False, 'blockers': [str(exc)]}, args.json)
        return 2

    if args.command == 'check':
        report = plan.summary()
        render(report, args.json)
        return 0 if report['ok'] else 1

    if args.command == 'preview':
        report = plan.summary()
        report['would_write'] = {
            'destination': plan.destination_path,
            'counts': plan.expected_metrics(),
            'noop': plan.is_noop(),
        }
        render(report, args.json)
        return 0 if report['ok'] else 1

    if args.command == 'run':
        if plan.blockers:
            render({'ok': False, 'migration_id': plan.migration_id,
                    'blockers': plan.blockers}, args.json)
            return 1
        try:
            result = import_plan(plan)
        except MigrationError as exc:
            render({'ok': False, 'migration_id': plan.migration_id,
                    'blockers': [str(exc)]}, args.json)
            return 1
        verification = verify_plan(plan)
        report = {
            'ok': verification['ok'],
            'status': result['status'],
            'migration_id': plan.migration_id,
            'source_fingerprint': plan.source_fingerprint,
            'integrity_digest': plan.integrity_digest,
            'counts': result['counts'],
            'problems': verification['problems'],
            'warnings': plan.warnings,
        }
        render(report, args.json)
        return 0 if verification['ok'] else 1

    if args.command == 'verify':
        if plan.blockers:
            # verify compares a fresh read of the source with the destination,
            # so it requires the same quiescent source as run.
            render({'ok': False, 'migration_id': plan.migration_id,
                    'blockers': plan.blockers}, args.json)
            return 1
        result = verify_plan(plan)
        report = {
            'ok': result['ok'],
            'migration_id': plan.migration_id,
            'integrity_digest': result['digest'],
            'counts': result['counts'],
            'problems': result['problems'],
        }
        render(report, args.json)
        return 0 if result['ok'] else 1

    if args.command == 'sample':
        app_huey = None
        if args.app:
            if not args.allow_unsafe_pickle:
                render({'ok': False, 'blockers': [
                    '--app requires --allow-unsafe-pickle because it runs '
                    'the real unpickler on business arguments']}, args.json)
                return 2
            try:
                app_huey = _load_app(args.app)
            except Exception as exc:
                render({'ok': False, 'blockers': [
                    'cannot import %r: %s' % (args.app, exc)]}, args.json)
                return 2
        report = {
            'ok': True,
            'migration_id': plan.migration_id,
            'queue_name': plan.queue_name,
            'samples': sample_records(plan, args.limit, app_huey),
        }
        render(report, args.json)
        return 0

    parser.error('unknown command')


# ---------------------------------------------------------------------------
# Demo.
# ---------------------------------------------------------------------------

def run_demo(keep=False):
    """Self-contained terminal demonstration using temporary directories."""
    from huey import FileHuey, SqliteHuey, chord
    from huey.consumer import Consumer

    root = tempfile.mkdtemp(prefix='huey-migration-demo-')
    source = os.path.join(root, 'file-source')
    database = os.path.join(root, 'sqlite-target.db')
    name = 'demo'
    print('Demo directory: %s' % root)
    print()
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)
            print('   FAIL: %s' % message)

    try:
        def add_impl(a, b):
            return a + b

        def total_impl(*values):
            # Called directly with a tuple by chords, and as a chained
            # callback with the previous return value appended.
            if len(values) == 1 and isinstance(values[0], (list, tuple)):
                values = tuple(values[0])
            return sum(values)

        # The same functions are registered on both instances, so the task
        # names in the serialized envelopes resolve on the target too.
        source_huey = FileHuey(name, path=source, use_thread_lock=True)
        add = source_huey.task()(add_impl)
        total = source_huey.task()(total_impl)

        result_1 = add(1, 2)
        result_2 = add(10, 20)
        source_huey.enqueue(add.s(3, 4).then(total.s()))
        # Huey.enqueue() always writes to the queue, even for a future eta;
        # only the consumer's scheduler writes to schedule/. Mirror that.
        scheduled = add.s(5, 6)
        scheduled.eta = (datetime.datetime.now(datetime.timezone.utc)
                         .replace(tzinfo=None) +
                         datetime.timedelta(hours=1))
        source_huey.add_schedule(scheduled)
        chord_result = source_huey.enqueue(
            chord([add.s(7, 8), add.s(9, 10)], total))
        source_huey.put_result('11111111-1111-1111-1111-111111111111',
                               {'done': 1})
        source_huey.revoke_by_id('22222222-2222-2222-2222-222222222222')
        source_huey.lock_task('demo-lock').acquire()
        source_huey.put_result('chord:demo-chord:0', 'partial')
        source_huey.storage.incr('chord:demo-chord')
        source_huey.storage.close()

        print('1) Source created (FileHuey at %s)' % source)
        for sub in (QUEUE_DIR, SCHEDULE_DIR, RESULTS_DIR, COUNTERS_DIR):
            count = 0
            path = os.path.join(source, sub)
            for _, _, files in os.walk(path):
                count += len(files)
            print('   %-9s %d file(s)' % (sub, count))
        print()

        print('2) Preview (source and destination must not change)')
        before = _tree_digest(source)
        plan = MigrationPlan(source, database, name,
                             migration_id='demo-cutover')
        print('   status: %s' % ('OK' if not plan.blockers else 'BLOCKED'))
        print('   queue identity: %r (%s)' % (
            plan.queue_name, plan.queue_identity['evidence']))
        print('   counts: %s' % canonical_json(plan.expected_metrics()))
        print('   source fingerprint: %s' % plan.source_fingerprint[:16])
        print('   integrity digest:   %s' % plan.integrity_digest[:16])
        for warning in plan.warnings:
            print('   warning: %s' % warning)
        check(_tree_digest(source) == before, 'preview modified the source')
        check(not os.path.exists(database),
              'preview created the destination')
        print()

        print('3) Interrupted import (transaction rollback)')
        partial_path = os.path.join(root, 'partial.db')
        partial = MigrationPlan(source, partial_path, name,
                                migration_id='demo-partial')
        try:
            _import_then_fail(partial, after_tasks=2)
        except RuntimeError as exc:
            print('   injected failure: %s' % exc)
        state = destination_state(partial_path)
        print('   destination task rows after failure: %d' % (
            state['counts'].get('task', 0)))
        print('   destination marker: %r' % state['marker'])
        check(state['counts'].get('task', 0) == 0,
              'a failed import left consumable tasks behind')
        check(not state['marker'],
              'a failed import left a migration marker behind')
        print()

        print('4) Import')
        result = import_plan(plan)
        print('   status: %s' % result['status'])
        print('   counts: %s' % canonical_json(result['counts']))
        verification = verify_plan(plan)
        print('   verify: %s' % ('OK' if verification['ok'] else 'FAILED'))
        print('   destination digest: %s' % str(
            verification['digest'])[:16])
        for problem in verification['problems']:
            print('   problem: %s' % problem)
        check(verification['ok'], 'verification failed after import')
        print()

        print('5) Idempotent retry (same source, same migration id)')
        again = import_plan(plan)
        print('   status: %s' % again['status'])
        source_digest_after_import = _tree_digest(source)
        target = SqliteHuey(name, filename=database)
        target.task()(add_impl)
        target.task()(total_impl)
        task_count = target.pending_count()
        scheduled_count = target.scheduled_count()
        print('   queued tasks: %d' % task_count)
        print('   scheduled:   %d' % scheduled_count)
        check(source_digest_after_import == before,
              'check/preview/run modified the source tree')
        check(again['status'] == 'already-imported',
              'retry was not recognised as already imported')
        check(task_count == plan.expected_metrics()['task'],
              'retry changed the number of queued deliveries')
        print()

        print('6) Resume consumption against SQLite')
        consumer = Consumer(target, workers=1, periodic=False,
                            initial_delay=0.01, max_delay=0.05,
                            check_worker_health=False)
        consumer.start()
        try:
            deadline = time.time() + 15
            while target.pending_count() and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.2)
        finally:
            consumer.stop(graceful=True)
        print('   %s -> %r' % (result_1.id, target.result(
            result_1.id, blocking=True, timeout=5, preserve=True)))
        print('   %s -> %r' % (result_2.id, target.result(
            result_2.id, blocking=True, timeout=5, preserve=True)))
        print('   scheduled task %s still pending: %s' % (
            scheduled.id, target.scheduled_count() == 1))
        print('   chord callback %s' % chord_result.callback.id)
        print('   lock still held: %s' % target.is_locked('demo-lock'))
        print('   revocation still present: %s' % target.is_revoked(
            '22222222-2222-2222-2222-222222222222'))
        print('   application value preserved: %r' % target.result(
            '11111111-1111-1111-1111-111111111111', preserve=True))
        check(target.result(result_1.id, blocking=True, timeout=5,
                            preserve=True) == 3,
              'result bytes or the chain did not survive')
        check(target.result(result_2.id, blocking=True, timeout=5,
                            preserve=True) == 30,
              'second result did not survive')
        check(target.scheduled_count() == 1, 'scheduled task was lost')
        check(target.is_locked('demo-lock'), 'lock state was lost')
        check(target.is_revoked('22222222-2222-2222-2222-222222222222'),
              'revocation was lost')
        target.storage.close()
        print()

        print('7) Conflict: same migration id, changed source')
        add(100, 200)
        changed = MigrationPlan(source, database, name,
                                migration_id='demo-cutover')
        for blocker in changed.blockers:
            print('   blocked: %s' % blocker)
        check(bool(changed.blockers),
              'a changed source reused the same migration id without error')
        print()

        print('8) Conflict: different migration id, populated destination')
        other = MigrationPlan(source, database, name,
                              migration_id='demo-other')
        for blocker in other.blockers:
            print('   blocked: %s' % blocker)
        check(bool(other.blockers),
              'a populated destination accepted a second migration')
        print()

        print('9) Source retained for rollback')
        print('   source unchanged by check/preview/run: %s' % (
            source_digest_after_import == before))
        print('   (step 7 deliberately mutated the source)')
        print('   rollback = point consumers back at %s' % source)
        print()

        if failures:
            print('Demo completed with %d failure(s):' % len(failures))
            for failure in failures:
                print(' - %s' % failure)
            return 1
        print('Demo complete: all checks passed.')
        return 0
    finally:
        if keep:
            print('Kept demo directory: %s' % root)
        else:
            shutil.rmtree(root, ignore_errors=True)


def _import_then_fail(plan, after_tasks=2):
    """Write a few rows then raise, to show rollback. Demo use only."""
    if plan.blockers:
        raise MigrationError('; '.join(plan.blockers))
    conn = sqlite3.connect(plan.destination_path, isolation_level=None)
    try:
        conn.execute('begin immediate')
        try:
            create_schema(conn, plan.strict_fifo)
            for offset, record in enumerate(plan.analysis.tasks, 1):
                conn.execute(
                    'insert into task (id, queue, data, priority) '
                    'values (?, ?, ?, ?)',
                    (offset, plan.queue_name,
                     sqlite3.Binary(record.item.data),
                     _priority(record.item.priority)))
                if offset >= after_tasks:
                    raise RuntimeError('simulated crash mid-import')
            conn.execute('commit')
        except RuntimeError:
            conn.execute('rollback')
            raise
    finally:
        conn.close()


def _tree_digest(path):
    items = []
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            with open(full, 'rb') as fh:
                items.append([os.path.relpath(full, path),
                              sha256_hex(fh.read())])
    return sha256_hex(canonical_json(items).encode('utf8'))


if __name__ == '__main__':
    sys.exit(main())
