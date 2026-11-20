"""
Persistent, multi-process safe fixed-window rate limiting.

Unlike :class:`huey.api.RateLimit`, which coordinates a window using separate
counter/window keys and is only safe within a single process, the
:class:`PersistentRateLimit` delegates the entire read-window / roll-over /
increment operation to the storage backend, where it runs in one database
transaction. Multiple consumer processes sharing one database therefore agree
on one window and one count.

Only the SQLite storage backends implement the required storage API (the
in-memory backend implements the same semantics for tests). The default
in-process :class:`~huey.api.RateLimit` and the default ``Huey.rate_limit()``
behavior are unchanged.
"""
import contextlib
import contextvars
import time
from functools import wraps

from huey.exceptions import ConfigurationError
from huey.exceptions import RateLimitExceeded


# Logical clock for deterministic testing. Production code never sets it, so
# the limiter falls back to time.time(). Huey.execute(timestamp=...) binds the
# execution timestamp here so a simulated window boundary behaves correctly.
_current_time = contextvars.ContextVar('huey_rate_limit_now', default=None)


def get_current_time():
    value = _current_time.get()
    return time.time() if value is None else value


@contextlib.contextmanager
def use_time(now):
    """Bind a logical (unix) timestamp for rate-limit decisions inside."""
    token = _current_time.set(now)
    try:
        yield
    finally:
        _current_time.reset(token)


class PersistentRateLimit(object):
    """
    Fixed-window rate limiter backed by durable, transactional storage.

    :param huey: the :class:`~huey.api.Huey` instance whose storage holds the
        limiter state. State is partitioned by queue name automatically, so
        distinct queues sharing one database never affect one another.
    :param name: logical limiter name, unique within the queue.
    :param limit: maximum number of acquisitions permitted per window.
    :param per: window length in seconds.
    :param retry: when ``True`` (the default) a rejected task is scheduled to
        run again when the window opens, without consuming any of the task's
        ordinary failure-retry attempts. When ``False`` the task is only
        retried if it itself declares retries.
    """
    def __init__(self, huey, name, limit, per, retry=True):
        self._huey = huey
        self._name = name
        self._limit = limit
        self._per = per
        self._retry = retry
        storage = huey.storage
        # Only backends that override the base storage API support this.
        if type(storage).rate_limit_add.__qualname__.startswith(
                'BaseStorage.'):
            raise ConfigurationError(
                'Storage backend %r does not implement the persistent '
                'rate-limit API. Use a SQLite-backed Huey (SqliteHuey) or '
                'MemoryHuey for tests.' % type(storage).__name__)

    def _current_window(self, now):
        # Fixed windows aligned to the unix epoch, matching RateLimit.
        return (int(now) // self._per) * self._per

    def acquire(self, now=None):
        """
        Reserve one slot in the current window.

        :raises RateLimitExceeded: when no slot is available. The exception's
            ``delay`` is the time remaining until the window opens and its
            ``window_boundary`` flag is set, which keeps the task out of the
            queue until then and preserves its failure-retry budget.
        """
        if now is None:
            now = get_current_time()
        window = self._current_window(now)
        result = self._huey.storage.rate_limit_add(
            self._name, window, self._limit, self._per, now)
        if not result.allowed:
            raise RateLimitExceeded(
                self._name, result.retry_after, retry=self._retry,
                window_boundary=True)
        return result

    def reset(self):
        """Clear all persisted state for this limiter."""
        self._huey.storage.rate_limit_reset(self._name)

    def current_usage(self, now=None):
        """Return the count stored for the current window (0 if unknown)."""
        if now is None:
            now = get_current_time()
        row = self._huey.storage.rate_limit_peek(self._name)
        if row is None:
            return 0
        if row['window'] + row['per'] <= now:
            # The persisted row belongs to a closed window.
            return 0
        return row['count']

    def __call__(self, fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            with self:
                return fn(*args, **kwargs)
        return inner

    def __enter__(self):
        self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass
