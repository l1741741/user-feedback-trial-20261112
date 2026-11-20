"""
Supporting types and validation for in-place task rescheduling.

See :py:meth:`Huey.reschedule` and :py:meth:`Huey.reschedule_many`. Unlike
:py:meth:`Result.reschedule` (which revokes the original task and enqueues a
replacement with a new task id), rescheduling in place adjusts the existing
queued or scheduled delivery, preserving the task id and therefore the
association with any :py:class:`Result` handle.
"""
import datetime
import math

from huey.utils import normalize_time


# Sentinel used to mean "the caller did not ask to change the execution time".
UNCHANGED = object()


def validate_priority(priority):
    """
    Validate and normalize a priority value. ``None`` means "leave the
    existing priority unchanged".
    """
    if priority is None:
        return None
    if isinstance(priority, bool) or not isinstance(priority, (int, float)):
        raise ValueError('priority must be a number, got %r' % (priority,))
    value = float(priority)
    if math.isnan(value) or math.isinf(value):
        raise ValueError('priority must be a finite number, got %r' %
                         (priority,))
    return value


def normalize_eta(huey, eta=None, delay=None):
    """
    Resolve the requested ``eta`` / ``delay`` to a naive datetime using the
    Huey instance's timezone convention.

    Returns :py:data:`UNCHANGED` when neither was supplied. Raises ValueError
    for invalid or contradictory input, before anything is modified.
    """
    if eta is not None and delay is not None:
        raise ValueError('specify either eta or delay, not both')
    if eta is None and delay is None:
        return UNCHANGED

    if delay is not None:
        if isinstance(delay, datetime.timedelta):
            seconds = delay.total_seconds()
        elif isinstance(delay, bool) or not isinstance(delay, (int, float)):
            raise ValueError('delay must be seconds or a timedelta, got %r' %
                             (delay,))
        else:
            seconds = float(delay)
        if math.isnan(seconds) or math.isinf(seconds):
            raise ValueError('delay must be a finite number, got %r' %
                             (delay,))
        if seconds < 0:
            raise ValueError('delay must be zero or positive, got %r' %
                             (delay,))
        return normalize_time(delay=seconds, utc=huey.utc)

    if not isinstance(eta, datetime.datetime):
        raise ValueError('eta must be a datetime, got %r' % (eta,))
    return normalize_time(eta=eta, utc=huey.utc)


def validate_operation_id(operation_id):
    if operation_id is None:
        return None
    if not isinstance(operation_id, str):
        raise ValueError('operation_id must be a string')
    if not operation_id:
        raise ValueError('operation_id must not be empty')
    return operation_id


class RescheduleResult(object):
    """
    Outcome of a single :py:meth:`Huey.reschedule` call.

    :ivar str task_id: the task id, unchanged.
    :ivar str status: one of ``'rescheduled'`` (same storage location),
        ``'queued'`` (moved from the schedule to the queue), ``'scheduled'``
        (moved from the queue to the schedule), or ``'replayed'`` (the
        operation id had already been applied and the recorded outcome is
        returned).
    :ivar str location: ``'queue'`` or ``'schedule'``.
    :ivar eta: the new execution time (datetime) or ``None`` for queued.
    :ivar float priority: the effective queue priority.
    :ivar str operation_id: the operation id, if one was used.
    :ivar list previous: the delivery snapshots that were replaced.
    :ivar list deliveries: the delivery snapshots that now exist.
    """
    __slots__ = ('task_id', 'status', 'location', 'eta', 'priority',
                 'operation_id', 'previous', 'deliveries', 'task')

    def __init__(self, task_id, status, location, eta, priority,
                 operation_id=None, previous=None, deliveries=None, task=None):
        self.task_id = task_id
        self.status = status
        self.location = location
        self.eta = eta
        self.priority = priority
        self.operation_id = operation_id
        self.previous = previous or []
        self.deliveries = deliveries or []
        self.task = task

    def as_dict(self):
        return {
            'task_id': self.task_id,
            'status': self.status,
            'location': self.location,
            'eta': self.eta.isoformat() if self.eta is not None else None,
            'priority': self.priority,
            'operation_id': self.operation_id,
        }

    def __repr__(self):
        return '<RescheduleResult %s %s at %s (%s)>' % (
            self.task_id, self.status, self.location,
            self.eta.isoformat() if self.eta is not None else 'now')


class ReschedulePreview(object):
    """
    Non-destructive description of what :py:meth:`Huey.reschedule` would do.
    """
    __slots__ = ('task_id', 'reschedulable', 'reason', 'previous', 'location',
                 'eta', 'priority', 'task_name')

    def __init__(self, task_id, reschedulable, reason, previous, location,
                 eta, priority, task_name=None):
        self.task_id = task_id
        self.reschedulable = reschedulable
        self.reason = reason
        self.previous = previous
        self.location = location
        self.eta = eta
        self.priority = priority
        self.task_name = task_name

    def as_dict(self):
        return {
            'task_id': self.task_id,
            'reschedulable': self.reschedulable,
            'reason': self.reason,
            'task_name': self.task_name,
            'current': [public_delivery(d) for d in self.previous],
            'new_location': self.location,
            'new_eta': self.eta.isoformat() if self.eta is not None else None,
            'new_priority': self.priority,
        }

    def __repr__(self):
        if self.reschedulable:
            suffix = '-> %s @ %s' % (
                self.location,
                self.eta.isoformat() if self.eta is not None else 'now')
        else:
            suffix = 'not reschedulable (%s)' % self.reason
        return '<ReschedulePreview %s %s>' % (self.task_id, suffix)


class RescheduleItemError(object):
    """
    A per-item failure from :py:meth:`Huey.reschedule_many`. Other items in the
    batch may still have been applied.
    """
    __slots__ = ('task_id', 'error')

    def __init__(self, task_id, error):
        self.task_id = task_id
        self.error = error

    def as_dict(self):
        return {
            'task_id': self.task_id,
            'error': type(self.error).__name__,
            'reason': getattr(self.error, 'reason', None),
            'message': str(self.error),
        }

    def __repr__(self):
        return '<RescheduleItemError %s %r>' % (self.task_id, self.error)


class RescheduleBatch(object):
    """
    Outcome of :py:meth:`Huey.reschedule_many`.

    Each item is applied in its own transaction. A failure for one item does
    not roll back items that already succeeded. ``results`` and ``errors``
    together account for every requested task id.
    """
    __slots__ = ('results', 'errors')

    def __init__(self, results=None, errors=None):
        self.results = results or []
        self.errors = errors or []

    def __iter__(self):
        return iter(self.results)

    def __len__(self):
        return len(self.results)

    def ok(self):
        return not self.errors

    def as_dict(self):
        return {
            'results': [r.as_dict() for r in self.results],
            'errors': [e.as_dict() for e in self.errors],
        }

    def __repr__(self):
        return '<RescheduleBatch %s ok, %s failed>' % (
            len(self.results), len(self.errors))


def public_delivery(delivery):
    """
    Return a JSON-friendly view of a delivery snapshot (datetime -> ISO string).
    """
    return {
        'task_id': delivery['task_id'],
        'location': delivery['location'],
        'priority': delivery.get('priority'),
        'eta': (delivery['eta'].isoformat()
                if delivery.get('eta') is not None else None),
    }
