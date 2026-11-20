class HueyException(Exception): pass
class ConfigurationError(HueyException): pass
class TaskLockedException(HueyException): pass
class ResultTimeout(HueyException): pass
class TaskTimeout(HueyException): pass


class RescheduleError(HueyException): pass
class RescheduleNotSupported(RescheduleError): pass

class TaskNotReschedulable(RescheduleError):
    """
    Raised when a task delivery can no longer be adjusted, because it is not
    present in the queue or schedule for this queue (it was already claimed by
    a worker, already ran, or never existed), or because more than one delivery
    exists for the requested task id.

    ``reason`` is one of ``'not_found'`` or ``'ambiguous'``.
    """
    def __init__(self, task_id, reason='not_found'):
        self.task_id = task_id
        self.reason = reason
        super(TaskNotReschedulable, self).__init__(
            'task %s cannot be rescheduled (%s)' % (task_id, reason))

class RescheduleConflict(RescheduleError):
    """
    Raised when an operation id was already used to reschedule a different
    task. Operation ids are unique per queue and per task.
    """
    def __init__(self, operation_id, task_id, existing_task_id):
        self.operation_id = operation_id
        self.task_id = task_id
        self.existing_task_id = existing_task_id
        super(RescheduleConflict, self).__init__(
            'operation "%s" was already used for task %s, not %s' % (
                operation_id, existing_task_id, task_id))

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
