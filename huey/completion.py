"""Durable task-completion events.

This module defines the event contract shared by the SQLite storage layer, the
public :class:`~huey.api.Huey` pull/ack methods, and local receiver
implementations.

Completion events are deliberately *not* signals.  Signals are process-local
and are gone once the consumer exits.  Completion events are written to the
same SQLite database as the task result, in the same transaction, so a process
that exits after a task finishes cannot leave a stored result without the
matching event (or vice versa).
"""
import dataclasses
import json
import sys
import threading


# Bump this whenever the payload schema changes.  Consumers should reject (or
# explicitly handle) an unknown version rather than silently misinterpreting a
# newer payload.
COMPLETION_EVENT_VERSION = 1

# Hard upper bound used when no explicit maximum is configured.  The payload
# builder trims optional fields and finally falls back to a minimal payload so
# an event can always be recorded without exceeding the bound.
DEFAULT_MAX_PAYLOAD_BYTES = 16 * 1024
MIN_MAX_PAYLOAD_BYTES = 256

# Terminal states.  Exactly one terminal event is expected per task execution
# attempt that reaches a terminal outcome.
COMPLETION_SUCCEEDED = 'succeeded'
COMPLETION_FAILED = 'failed'
COMPLETION_CANCELED = 'canceled'
COMPLETION_REVOKED = 'revoked'
COMPLETION_EXPIRED = 'expired'

# Non-terminal state.  A retrying attempt is reported so consumers can observe
# it, but it must never be mistaken for a final completion.
COMPLETION_RETRYING = 'retrying'

TERMINAL_STATUSES = frozenset((
    COMPLETION_SUCCEEDED,
    COMPLETION_FAILED,
    COMPLETION_CANCELED,
    COMPLETION_REVOKED,
    COMPLETION_EXPIRED,
))
NONTERMINAL_STATUSES = frozenset((COMPLETION_RETRYING,))
ALL_STATUSES = TERMINAL_STATUSES | NONTERMINAL_STATUSES


def build_payload(status, task_id, task_name, occurred_at, retries=0,
                  error=None, truncation_note=None,
                  max_bytes=DEFAULT_MAX_PAYLOAD_BYTES):
    """Build a versioned, size-bounded JSON payload.

    The payload never includes task arguments/kwargs and never includes a raw
    traceback, so secrets passed to tasks are not copied into the event log.
    ``error`` should already be a short, de-identified string (for example the
    result of ``build_error_summary``).

    If the payload is too large, optional fields are dropped in order of
    decreasing sensitivity/size, ending with a minimal payload that must fit.
    An explicit ``truncated`` flag tells consumers data was dropped.
    """
    base = {
        'version': COMPLETION_EVENT_VERSION,
        'status': status,
        'final': status in TERMINAL_STATUSES,
        'task_id': task_id,
        'task_name': task_name,
        'occurred_at': occurred_at,
        'retries': retries,
    }
    optional = {}
    if error is not None:
        optional['error'] = error
    if truncation_note is not None:
        optional['truncation_note'] = truncation_note

    def encode(obj):
        return json.dumps(obj, sort_keys=True, separators=(',', ':'))
    def size(obj):
        return len(encode(obj).encode('utf8'))

    payload = dict(base, **optional)
    if size(payload) <= max_bytes:
        return encode(payload).encode('utf8')

    truncated = dict(base, truncated=True)
    # Drop optional fields first, then trim remaining strings.
    for key in ('truncation_note', 'error'):
        optional.pop(key, None)
        candidate = dict(base, **optional, truncated=True)
        if size(candidate) <= max_bytes:
            return encode(candidate).encode('utf8')

    for key in ('task_name', 'task_id'):
        if key in truncated and isinstance(truncated[key], str):
            while size(truncated) > max_bytes and truncated[key]:
                truncated[key] = truncated[key][:-1]
            if size(truncated) <= max_bytes:
                return encode(truncated).encode('utf8')

    # Last resort: no optional fields, shortened identifiers.
    minimal = {
        'version': COMPLETION_EVENT_VERSION,
        'status': status,
        'final': status in TERMINAL_STATUSES,
        'occurred_at': occurred_at,
        'truncated': True,
    }
    if size(minimal) > max_bytes:
        # A bound smaller than the literal minimum cannot be honored; this is
        # a configuration error, not a task error.
        raise ValueError('max_bytes is too small for a completion event')
    return encode(minimal).encode('utf8')


def build_error_summary(exception, include_message=False, max_chars=500):
    """Return a short, non-secret representation of an exception.

    By default only the exception type is included.  ``str(exception)`` may
    contain user data or credentials and is excluded unless explicitly
    requested with ``include_message=True``.  The traceback and the task
    arguments are never included.
    """
    summary = type(exception).__name__
    if include_message:
        try:
            text = str(exception)
        except Exception:
            text = ''
        text = ' '.join(text.split())
        if text:
            summary = '%s: %s' % (summary, text)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + '...'
    return summary


@dataclasses.dataclass(frozen=True)
class CompletionEvent:
    """A completion event claimed by a receiver.

    ``lease_token`` must be supplied when acknowledging the event.  It is a
    capability scoped to this particular claim: once the lease expires and
    another receiver claims the same ``event_id``, the old token can no longer
    acknowledge (and therefore can no longer delete) the event.
    """
    event_id: str
    queue: str
    task_id: str
    task_name: str
    status: str
    final: bool
    result_available: bool
    occurred_at: float
    payload: bytes
    lease_token: str
    lease_expires: float

    @property
    def data(self):
        """Decode the versioned JSON payload."""
        return json.loads(self.payload.decode('utf8'))

    @property
    def version(self):
        return self.data.get('version')

    def to_dict(self):
        data = self.data
        data.update(event_id=self.event_id,
                    queue=self.queue,
                    task_id=self.task_id,
                    status=self.status,
                    final=self.final,
                    result_available=self.result_available)
        return data


class LocalCompletionReceiver(object):
    """Small, dependency-free pull/ack receiver for local use.

    The receiver is intentionally not tied to a ticket system.  Callers provide
    a handler; the event is acknowledged only after the handler returns
    successfully.  A process that crashes before acknowledging leaves the event
    available for redelivery (at-least-once).

    Example::

        receiver = LocalCompletionReceiver(huey)

        def handle(event):
            print(event.event_id, event.status, event.data)

        receiver.process_once(handle)
    """
    def __init__(self, huey, limit=10, lease_seconds=None, owner=None):
        self.huey = huey
        self.limit = limit
        self.lease_seconds = lease_seconds
        self.owner = owner or 'receiver:%s' % threading.get_ident()

    def pull(self):
        return self.huey.pull_completion_events(
            limit=self.limit,
            lease_seconds=self.lease_seconds,
            owner=self.owner)

    def ack(self, event):
        return self.huey.ack_completion_event(event.event_id,
                                              event.lease_token)

    def process_once(self, handler):
        """Pull one batch, run ``handler`` for each event, then acknowledge.

        If ``handler`` raises, that event is left unacknowledged and will be
        redelivered after its lease expires.  Returning normally (even if the
        acknowledgement itself races with a lease takeover) does not block the
        receiver.
        """
        processed = 0
        for event in self.pull():
            handler(event)
            self.ack(event)
            processed += 1
        return processed

    def run(self, handler, interval=1.0, stop_event=None):
        """Poll forever (or until ``stop_event`` is set)."""
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            processed = self.process_once(handler)
            if not processed:
                stop_event.wait(interval)


def _stdout_handler(event):
    # Print one JSON object per line and flush, so a supervising test process
    # can observe delivery without a ticket-service dependency.
    sys.stdout.write(json.dumps(event.to_dict(), sort_keys=True) + '\n')
    sys.stdout.flush()


def receiver_main(argv=None):
    """Run a standalone local receiver process.

    Usage: ``python -m huey.completion_receiver app.module.huey_instance [--once]``

    The receiver acknowledges each event only after it has been printed.  A
    crash between printing and acknowledging causes redelivery, which the
    stable ``event_id`` lets a consumer de-duplicate.
    """
    import argparse
    import logging
    import os
    import signal

    from huey.utils import load_class

    parser = argparse.ArgumentParser(
        description='Local pull/ack receiver for durable completion events.')
    parser.add_argument('huey_path', help='import path to a Huey instance')
    parser.add_argument('--once', action='store_true',
                        help='pull one batch and exit')
    parser.add_argument('--drain', action='store_true',
                        help='pull batches until no unleased events remain, '
                             'then exit')
    parser.add_argument('--interval', type=float, default=0.5,
                        help='poll interval in seconds')
    parser.add_argument('--limit', type=int, default=10,
                        help='events per pull batch')
    parser.add_argument('--lease-seconds', type=float, default=30.0,
                        help='lease duration for claimed events')
    parser.add_argument('--owner', default=None,
                        help='receiver identity recorded on leases')
    parser.add_argument('--no-ack', action='store_true',
                        help='print events but do not acknowledge them '
                             '(simulates a receiver crash before ack)')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO)

    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    huey = load_class(args.huey_path)
    if not huey.completion_events:
        sys.stderr.write('Huey instance "%s" does not have '
                         'completion_events enabled.\n' % args.huey_path)
        return 2

    receiver = LocalCompletionReceiver(
        huey, limit=args.limit, lease_seconds=args.lease_seconds,
        owner=args.owner)

    if args.no_ack:
        for event in receiver.pull():
            _stdout_handler(event)
        return 0

    if args.once:
        receiver.process_once(_stdout_handler)
        return 0

    if args.drain:
        while receiver.process_once(_stdout_handler):
            pass
        return 0

    stop_event = threading.Event()

    def handle_stop(signum, frame):
        stop_event.set()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handle_stop)

    receiver.run(_stdout_handler, interval=args.interval,
                 stop_event=stop_event)
    return 0


if __name__ == '__main__':
    sys.exit(receiver_main())
