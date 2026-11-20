"""
Consumer startup readiness coordination.

A running consumer process is not the same as a *ready* consumer: every worker
unit must complete its own initialization (notably the ``on_startup`` hooks)
and the scheduler must be running before the consumer can honestly claim it
can process work. This module provides the pieces the consumer uses to track
that state:

* :class:`ReadinessMessage` — a generation-tagged report a unit sends once its
  initialization finishes. The generation tag lets a restarting consumer
  reject readiness reports produced by the previous generation of units.
* :class:`ThreadReadinessChannel` / :class:`ProcessReadinessChannel` — the
  transport used by thread/greenlet and forked-process environments. The
  process channel is built from per-unit pipes so that, unlike
  ``multiprocessing.Queue``, no helper thread exists in the parent before
  ``fork()`` and a killed child cannot wedge the parent's reader.
* :func:`write_readiness_receipt` — the machine-readable, credential-free
  receipt written for deployment scripts.

Only readiness state lives here. The state machine driving it lives on the
:class:`~huey.consumer.Consumer`.
"""
import json
import multiprocessing
import os
import queue as queue_module
import select
import tempfile
from collections import namedtuple


# Consumer startup lifecycle states.
STATE_PENDING = 'pending'
STATE_READY = 'ready'
STATE_FAILED = 'failed'

# Terminal states, i.e. states after which waiters must be released.
TERMINAL_STATES = (STATE_READY, STATE_FAILED)

# Machine-readable failure reasons carried by ConsumerStartupError and the
# readiness receipt. Keep this vocabulary stable: deployment scripts branch
# on these strings.
REASON_STARTUP_HOOK = 'startup_hook'
REASON_UNIT_DIED = 'unit_died'
REASON_STORAGE = 'storage_unavailable'
REASON_TIMEOUT = 'timeout'
REASON_SHUTDOWN = 'shutdown'

# Unit report outcomes.
REPORT_READY = 'ready'
REPORT_ERROR = 'error'


class ReadinessMessage(namedtuple('ReadinessMessage',
                                 ('generation', 'unit', 'status',
                                  'error_type', 'error_message'))):
    """
    A single readiness report sent by a worker unit.

    :param int generation: consumer generation that owned the sending unit.
    :param str unit: name of the unit, e.g. ``"Worker-1"`` or ``"Scheduler"``.
    :param str status: :data:`REPORT_READY` or :data:`REPORT_ERROR`.
    :param error_type: dotted exception type name, or ``None``.
    :param error_message: exception message text, or ``None``. Intentionally
        limited to ``type + message``; tracebacks go to the logs and never to
        a receipt.
    """
    __slots__ = ()


def ready_message(generation, unit):
    return ReadinessMessage(generation, unit, REPORT_READY, None, None)


def error_message(generation, unit, exc):
    return ReadinessMessage(
        generation, unit, REPORT_ERROR,
        '%s.%s' % (type(exc).__module__, type(exc).__name__),
        str(exc))


class ThreadReadinessChannel(object):
    """
    Readiness channel for in-process units (thread and greenlet environments).

    A plain :class:`queue.Queue` shared by every unit: all units live in the
    same process and the queue is a thread-safe rendez-vous.
    """
    def __init__(self):
        self._queue = queue_module.Queue()

    def sender_for(self, name):
        return self._queue.put

    def recv(self, timeout=0.1):
        try:
            return self._queue.get(block=True, timeout=timeout)
        except queue_module.Empty:
            return None

    def drain(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue_module.Empty:
                return

    def close(self):
        # A queue.Queue holds no OS descriptors; nothing to release.
        pass

    def init_child(self, name):
        # In-process units share the queue and need no descriptor cleanup.
        pass


class ProcessReadinessChannel(object):
    """
    Readiness channel for forked-process units.

    Each unit gets its own pipe write-end; the parent keeps every read-end and
    polls them with ``select``. Pipes (unlike ``multiprocessing.Queue``)
    require no background thread, so there is nothing to inherit across
    ``fork()``, and a worker that is killed can never block the parent.

    Because ``fork()`` duplicates every open file descriptor, each child
    inherits *all* write-ends. :meth:`init_child` closes every write-end the
    child does not own (and the read-ends, which only the parent uses), so a
    pipe is fully closed once its owning unit exits and the parent can rely
    on EOF/poll semantics instead of a descriptor kept alive by a sibling.
    """
    def __init__(self):
        self._readers = {}   # name -> read connection
        self._writers = {}   # name -> write connection
        self._poller = select.poll()

    def sender_for(self, name):
        r, w = multiprocessing.Pipe(duplex=False)
        self._readers[name] = r
        self._writers[name] = w
        self._poller.register(r.fileno(), select.POLLIN)

        def send(message):
            try:
                w.send(message)
            except (BrokenPipeError, OSError):
                # The parent is gone (e.g. hard shutdown); nothing useful can
                # be done from inside the unit.
                pass
        return send

    def init_child(self, name):
        """
        Run inside a forked unit right after it starts. Keep only the write
        end belonging to this unit; close every inherited read-end and every
        sibling's write-end so descriptor lifetime matches unit lifetime.
        """
        for unit, writer in self._writers.items():
            if unit != name:
                try:
                    writer.close()
                except OSError:
                    pass
        for reader in self._readers.values():
            try:
                reader.close()
            except OSError:
                pass

    def _readies(self):
        for name, conn in list(self._readers.items()):
            try:
                while conn.poll():
                    yield conn.recv()
            except EOFError:
                # All write-ends closed: the unit exited without reporting.
                continue
            except OSError:
                continue

    def recv(self, timeout=0.1):
        try:
            events = self._poller.poll(timeout * 1000)
        except InterruptedError:
            return None
        if not events:
            return None
        for _fd, _event in events:
            for message in self._readies():
                return message
        return None

    def drain(self):
        for _ in self._readies():
            pass

    def close(self):
        """Release every pipe end held by the parent process."""
        for conn in list(self._readers.values()):
            try:
                conn.close()
            except OSError:
                pass
        for conn in list(self._writers.values()):
            try:
                conn.close()
            except OSError:
                pass
        self._readers.clear()
        self._writers.clear()


def write_readiness_receipt(path, state, generation, worker_type, units,
                            startup_timeout, error=None):
    """
    Write the machine-readable readiness receipt atomically.

    The payload is a fixed whitelist of non-sensitive operational fields. It
    never contains command-line arguments, storage DSNs, credentials, task
    payloads or tracebacks. The file is written to a temporary file in the
    same directory and renamed into place so a deployment script polling the
    path never observes a partially-written document.

    :param error: :class:`~huey.exceptions.ConsumerStartupError` or ``None``.
    """
    payload = {
        'status': state,
        'generation': generation,
        'worker_type': worker_type,
        'units': list(units),
        'startup_timeout': startup_timeout,
    }
    if error is not None:
        payload['error'] = {
            'reason': error.reason,
            'unit': error.unit,
            'error_type': error.error_type,
            'message': str(error),
        }

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix='.huey-readiness-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return payload
