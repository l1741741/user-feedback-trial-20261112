class HueyException(Exception): pass
class ConfigurationError(HueyException): pass
class TaskLockedException(HueyException): pass
class ResultTimeout(HueyException): pass
class TaskTimeout(HueyException): pass


class MessageSchemaError(HueyException):
    """
    Base class for errors raised while interpreting a versioned task message.
    """
    pass


class MessageVersionError(MessageSchemaError):
    """
    Raised when a message advertises a version the consumer cannot read.

    The error deliberately exposes only the task name, task id, version and
    field path. Task arguments and keyword arguments are never included in the
    exception text, so rejecting a message cannot leak its payload into logs.
    """
    def __init__(self, version, task_name=None, task_id=None, path=(),
                 supported=None, reason='unsupported message version'):
        self.version = version
        self.task_name = task_name
        self.task_id = task_id
        self.path = tuple(path or ())
        self.supported = supported
        self.reason = reason
        super(MessageVersionError, self).__init__(version, task_name, task_id)

    def __str__(self):
        parts = ['%s: version=%r' % (self.reason, self.version)]
        if self.task_name is not None:
            parts.append('task=%s' % (self.task_name,))
        if self.task_id is not None:
            parts.append('id=%s' % (self.task_id,))
        if self.path:
            parts.append('path=%s' % ('.'.join(self.path),))
        if self.supported is not None:
            versions = sorted(self.supported)
            parts.append('supported=%s' % (
                ','.join(str(v) for v in versions),))
        return ' '.join(parts)


class TaskAliasError(MessageSchemaError):
    """
    Raised for conflicting, cyclic or unresolved task-name aliases.
    """
    def __init__(self, msg, aliases=None):
        self.aliases = list(aliases or ())
        super(TaskAliasError, self).__init__(msg)


class MessageMigrationError(MessageSchemaError):
    """
    Raised when an old message cannot be safely converted to the current
    message version.

    If a migration function raises, the original exception is attached as
    ``__cause__`` but its arguments are not copied into this exception's text.
    """
    def __init__(self, task_name, task_id=None, from_version=None,
                 to_version=None, path=(), reason='migration failed'):
        self.task_name = task_name
        self.task_id = task_id
        self.from_version = from_version
        self.to_version = to_version
        self.path = tuple(path or ())
        self.reason = reason
        super(MessageMigrationError, self).__init__(
            task_name, task_id, from_version, to_version)

    def __str__(self):
        parts = ['%s for task=%s' % (self.reason, self.task_name)]
        if self.task_id is not None:
            parts.append('id=%s' % (self.task_id,))
        if self.from_version is not None or self.to_version is not None:
            parts.append('from_version=%r to_version=%r' % (
                self.from_version, self.to_version))
        if self.path:
            parts.append('path=%s' % ('.'.join(self.path),))
        return ' '.join(parts)

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
