from collections import namedtuple

from huey.context import PublishContext
from huey.exceptions import HueyException
from huey.utils import ChordConfig


Message = namedtuple('Message', ('id', 'name', 'eta', 'retries', 'retry_delay',
                                 'priority', 'args', 'kwargs', 'on_complete',
                                 'on_error', 'expires', 'expires_resolved',
                                 'timeout', 'chord_config', 'retry_backoff',
                                 'task_context'))

# Automatically set missing parameters to None. This is kind-of a hack, but it
# allows us to add new parameters while continuing to be able to handle
# messages enqueued with a smaller-set of arguments.
Message.__new__.__defaults__ = (None,) * len(Message._fields)


class Registry(object):
    def __init__(self):
        self._registry = {}
        self._periodic_tasks = []

    def task_to_string(self, task_class):
        return '%s.%s' % (task_class.__module__, task_class.__name__)

    def register(self, task_class):
        task_str = self.task_to_string(task_class)
        if task_str in self._registry:
            raise ValueError('Attempting to register a task with the same '
                             'identifier as existing task. Specify a different'
                             ' name= to register this task. "%s"' % task_str)

        self._registry[task_str] = task_class
        if hasattr(task_class, 'validate_datetime'):
            self._periodic_tasks.append(task_class)
        return True

    def unregister(self, task_class):
        task_str = self.task_to_string(task_class)
        if task_str not in self._registry:
            return False

        del self._registry[task_str]
        if hasattr(task_class, 'validate_datetime'):
            self._periodic_tasks = [t for t in self._periodic_tasks
                                    if t is not task_class]
        return True

    def string_to_task(self, task_str):
        if task_str not in self._registry:
            raise HueyException('%s not found in TaskRegistry' % task_str)
        return self._registry[task_str]

    def create_message(self, task):
        task_str = self.task_to_string(type(task))
        if task_str not in self._registry:
            raise HueyException('%s not found in TaskRegistry' % task_str)

        # Remove an injected context-task instance before serializing.
        if task.kwargs and task.kwargs.get('task') is task:
            task.kwargs.pop('task')

        on_complete = None
        if task.on_complete is not None:
            on_complete = self.create_message(task.on_complete)

        on_error = None
        if task.on_error is not None:
            on_error = self.create_message(task.on_error)

        chord_config = None
        if task.chord_config is not None:
            chord_config = (
                task.chord_config.cid,
                task.chord_config.size,
                task.chord_config.idx,
                self.create_message(task.chord_config.callback))

        # The publish context is stored on the task as a PublishContext value
        # object and travels over the wire as a plain (correlation_id, tenant,
        # fields) tuple so that custom serializers only have to handle
        # built-in types.
        task_context = (task.task_context.to_wire()
                        if task.task_context is not None else None)

        return Message(
            task.id,
            task_str,
            task.eta,
            task.retries,
            task.retry_delay,
            task.priority,
            task.args,
            task.kwargs,
            on_complete,
            on_error,
            task.expires,
            task.expires_resolved,
            task.timeout,
            chord_config,
            task.retry_backoff,
            task_context)

    def create_task(self, message):
        TaskClass = self.string_to_task(message.name)

        on_complete = None
        if message.on_complete is not None:
            on_complete = self.create_task(message.on_complete)

        on_error = None
        if message.on_error is not None:
            on_error = self.create_task(message.on_error)

        chord_config = None
        if message.chord_config is not None:
            cid, size, idx, cb_ser = message.chord_config
            callback = self.create_task(cb_ser)
            chord_config = ChordConfig(cid, size, idx, callback)

        # Re-validate on the consumer side: data coming back from the queue
        # cannot be assumed to satisfy the publish-time constraints.
        task_context = None
        if message.task_context is not None:
            task_context = PublishContext.from_wire(message.task_context)

        return TaskClass(
            message.args,
            message.kwargs,
            message.id,
            message.eta,
            message.retries,
            message.retry_delay,
            message.priority,
            message.expires,
            on_complete,
            on_error,
            message.expires_resolved,
            message.timeout,
            chord_config,
            message.retry_backoff,
            task_context=task_context)

    @property
    def periodic_tasks(self):
        return [task_class() for task_class in self._periodic_tasks]
