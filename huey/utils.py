from collections import namedtuple
import calendar
import contextlib
import datetime
import math
import os
import sys
import threading
import time
import warnings
try:
    import fcntl
except ImportError:
    fcntl = None


if sys.version_info < (3, 12):
    utcnow = datetime.datetime.utcnow
else:
    def utcnow():
        return (datetime.datetime
                .now(datetime.timezone.utc)
                .replace(tzinfo=None))


Error = namedtuple('Error', ('metadata',))


class _Skipped(object):
    def __repr__(self):
        return 'SKIPPED'
    def __reduce__(self):
        return 'SKIPPED'

SKIPPED = _Skipped()

ChordConfig = namedtuple('ChordConfig', ('cid', 'size', 'idx', 'callback'))


def load_class(s):
    path, klass = s.rsplit('.', 1)
    __import__(path)
    mod = sys.modules[path]
    return getattr(mod, klass)


def is_naive(dt):
    """
    Determines if a given datetime.datetime is naive.
    The concept is defined in Python's docs:
    http://docs.python.org/library/datetime.html#datetime.tzinfo
    Assuming value.tzinfo is either None or a proper datetime.tzinfo,
    value.utcoffset() implements the appropriate logic.
    """
    return dt.utcoffset() is None


def make_naive(dt):
    """
    Makes an aware datetime.datetime naive in local time zone.
    """
    tt = dt.utctimetuple()
    ts = calendar.timegm(tt)
    local_tt = time.localtime(ts)
    return datetime.datetime(*local_tt[:6])


def aware_to_utc(dt):
    """
    Converts an aware datetime.datetime in UTC time zone.
    """
    return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def local_to_utc(dt):
    """
    Converts a naive local datetime.datetime in UTC time zone.
    """
    return datetime.datetime(*time.gmtime(time.mktime(dt.timetuple()))[:6])


def normalize_expire_time(expires, utc=True):
    if isinstance(expires, datetime.datetime):
        return normalize_time(eta=expires, utc=utc)
    return normalize_time(delay=expires, utc=utc)


def validate_duration_seconds(name, value):
    """
    Validate a duration expressed as a number of seconds (int/float) or as a
    ``datetime.timedelta``, returning the equivalent number of seconds.

    Booleans and other types are rejected with a ``TypeError``; negative and
    non-finite (inf/nan) values are rejected with a ``ValueError``. Zero is a
    valid duration.
    """
    if isinstance(value, bool) or \
            not isinstance(value, (int, float, datetime.timedelta)):
        raise TypeError('%s must be a number of seconds or a timedelta, '
                        'got %r' % (name, value))
    if isinstance(value, datetime.timedelta):
        seconds = value.total_seconds()
    else:
        seconds = value
    if not math.isfinite(seconds):
        raise ValueError('%s must be a finite number of seconds, '
                         'got %r' % (name, value))
    if seconds < 0:
        raise ValueError('%s must not be negative, got %r' % (name, value))
    return seconds


def validate_retry_deadline(value):
    """
    Validate a retry deadline: ``None``, an absolute ``datetime``, or a
    duration (seconds / ``timedelta``) relative to the first enqueue.
    """
    if value is None or isinstance(value, datetime.datetime):
        return
    validate_duration_seconds('retry_deadline', value)


def validate_retry_backoff_max(value):
    """Validate a backoff ceiling, returning seconds (or ``None``)."""
    if value is None:
        return None
    return validate_duration_seconds('retry_backoff_max', value)


def validate_retry_jitter(value):
    """
    Validate a jitter setting, returning the normalized value: ``None``
    (disabled), ``True`` (full jitter), or a non-negative number of seconds
    of additive jitter.
    """
    if value is None or value is False:
        return None
    if value is True:
        return True
    return validate_duration_seconds('retry_jitter', value)


def normalize_time(eta=None, delay=None, utc=True):
    if (delay is None) == (eta is None):
        raise ValueError('Specify either an eta (datetime) or delay (seconds)')
    elif delay is not None:
        method = (utc and utcnow or
                  datetime.datetime.now)
        if not isinstance(delay, datetime.timedelta):
            delay = datetime.timedelta(seconds=delay)
        return method() + delay
    elif not isinstance(eta, datetime.datetime):
        raise ValueError('eta must be a datetime instance')
    else:
        has_tz = not is_naive(eta)
        if utc:
            if not has_tz:
                eta = local_to_utc(eta)
            else:
                eta = aware_to_utc(eta)
        elif has_tz:
            # Convert TZ-aware into naive localtime.
            eta = make_naive(eta)
        return eta


def encode(s):
    if isinstance(s, bytes):
        return s
    elif isinstance(s, str):
        return s.encode('utf8')
    elif s is not None:
        return str(s).encode('utf8')


class FileLock(object):
    def __init__(self, filename):
        if fcntl is None:
            warnings.warn('FileLock not supported on this platform. Please '
                          'use a different storage implementation.')
        self.filename = filename
        # Each thread flocks its own fd. A shared fd is clobbered under
        # thread contention, leaking the flock and deadlocking every process.
        self._local = threading.local()

        dirname = os.path.dirname(filename)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

    def acquire(self):
        flags = os.O_CREAT | os.O_TRUNC | os.O_RDWR
        self._local.fd = fd = os.open(self.filename, flags)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)

    def release(self):
        fd = getattr(self._local, 'fd', None)
        if fd is not None:
            self._local.fd = None
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


@contextlib.contextmanager
def noop_context():
    yield
