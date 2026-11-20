class HueyException(Exception): pass
class ConfigurationError(HueyException): pass
class TaskNotRegistered(HueyException):
    """
    Raised when reading a task message whose task class is not present in the
    registry, e.g. after a rolling deploy removed or renamed a task.
    """
    def __init__(self, task_name):
        self.task_name = task_name
        super(TaskNotRegistered, self).__init__(
            '%s not found in TaskRegistry' % task_name)
class TaskLockedException(HueyException): pass
class ResultTimeout(HueyException): pass
class TaskTimeout(HueyException): pass

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
