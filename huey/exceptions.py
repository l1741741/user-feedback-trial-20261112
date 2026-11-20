class HueyException(Exception): pass
class ConfigurationError(HueyException): pass
class TaskLockedException(HueyException): pass
class ResultTimeout(HueyException): pass
class TaskTimeout(HueyException): pass

class ConsumerStartupError(HueyException):
    """
    The consumer failed to become ready: a worker unit (including its startup
    hooks) or the scheduler died or reported an error during startup, the
    storage backend was unavailable, the unified startup timeout elapsed, or
    the consumer was shut down before readiness was reached.

    ``reason`` is one of the machine-readable constants in
    :mod:`huey.readiness` (``REASON_*``) so that deployment tooling can branch
    on the failure without parsing log messages.
    """
    def __init__(self, reason, message, unit=None, error_type=None):
        self.reason = reason
        self.unit = unit
        self.error_type = error_type
        super(ConsumerStartupError, self).__init__(message)

    def __str__(self):
        return self.args[0]

class RateLimitExceeded(HueyException):
    def __init__(self, key, delay, retry=True):
        self.key, self.delay, self.retry = key, delay, retry
        super(RateLimitExceeded, self).__init__(key, delay, retry)

    def __str__(self):
        if self.retry:
            return 'Rate limit exceeded on "%s", retry in %0.1fs' % (
                self.key, self.delay)
        return 'Rate limit exceeded on "%s"' % self.key

class CancelExecution(Exception):
    def __init__(self, msg=None, retry=None):
        self.retry = retry
        args = () if msg is None else (msg,)
        super(CancelExecution, self).__init__(*args)
class RetryTask(Exception):
    def __init__(self, msg=None, eta=None, delay=None, *args, **kwargs):
        self.eta, self.delay = eta, delay
        super(RetryTask, self).__init__(msg, *args, **kwargs)
class TaskException(Exception):
    def __init__(self, metadata=None, *args):
        self.metadata = metadata or {}
        super(TaskException, self).__init__(*args)

    def __str__(self):
        return self.metadata.get('error') or 'unknown error'


# Not failures while a retry is pending.
RETRYING_EXCEPTIONS = (RetryTask, RateLimitExceeded, TaskLockedException)
