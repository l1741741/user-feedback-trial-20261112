class HueyException(Exception): pass
class ConfigurationError(HueyException): pass
class TaskLockedException(HueyException): pass
class ResultTimeout(HueyException): pass
class TaskTimeout(HueyException): pass


class MessageRejected(HueyException, ValueError):
    """
    Raised when a serialized message cannot be authenticated or decoded.

    It subclasses ``ValueError`` for compatibility with the original
    ``SignedSerializer``, which documented a ``ValueError`` on tampering, and
    ``HueyException`` so it can be handled alongside other Huey errors.

    ``reason`` is a stable, non-sensitive classification suitable for logs.
    The exception string never contains key material, the raw message, or
    task arguments.
    """
    def __init__(self, reason):
        self.reason = reason
        super(MessageRejected, self).__init__(reason)

    def __str__(self):
        return self.reason


class MessageAuthenticationError(MessageRejected):
    """A message failed key lookup or signature verification."""


class MessageFormatError(MessageRejected):
    """A message was structurally invalid or could not be decoded."""

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
