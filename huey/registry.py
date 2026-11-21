from collections import namedtuple

from huey.exceptions import HueyException
from huey.utils import ChordConfig


Message = namedtuple('Message', ('id', 'name', 'eta', 'retries', 'retry_delay',
                                 'priority', 'args', 'kwargs', 'on_complete',
                                 'on_error', 'expires', 'expires_resolved',
                                 'timeout', 'chord_config', 'retry_backoff'))

# Automatically set missing parameters to None. This is kind-of a hack, but it
# allows us to add new parameters while continuing to be able to handle
# messages enqueued with a smaller-set of arguments.
Message.__new__.__defaults__ = (None,) * len(Message._fields)


class Registry(object):
    def __init__(self):
        self._registry = {}
        self._periodic_tasks = []
        self._aliases = {}

    def task_to_string(self, task_class):
        return '%s.%s' % (task_class.__module__, task_class.__name__)

    def register_task_alias(self, alias, target):
        """
        Register ``alias`` as another name for the task ``target``.

        The target may be a task class, a TaskWrapper, or the canonical task
        name (string). The canonical task does not need to be registered yet,
        which makes it possible to declare aliases before importing the module
        containing the new task.
        """
        if not isinstance(alias, str) or not alias:
            raise ValueError('alias must be a non-empty string')
        target_name = self.task_name(target)
        if alias == target_name:
            raise ValueError('cannot alias a task to itself: "%s"' % alias)
        if alias in self._registry:
            raise ValueError('cannot register alias "%s", a task with that '
                             'name is already registered.' % alias)

        existing = self._aliases.get(alias)
        if existing is not None and existing != target_name:
            raise ValueError('alias "%s" is already mapped to "%s".' %
                             (alias, existing))
        self._aliases[alias] = target_name
        return True

    def unregister_task_alias(self, alias):
        return self._aliases.pop(alias, None) is not None

    @property
    def task_aliases(self):
        return dict(self._aliases)

    def task_name(self, target):
        """Return the canonical name for a task class, wrapper, or name."""
        if isinstance(target, str):
            return target
        task_class = getattr(target, 'task_class', target)
        if not isinstance(task_class, type):
            raise ValueError('alias target must be a task class, task '
                             'wrapper, or task name string.')
        return self.task_to_string(task_class)

    def resolve_name(self, task_str):
        seen = set()
        while task_str in self._aliases:
            if task_str in seen:
                raise HueyException('circular task alias involving "%s"' %
                                    task_str)
            seen.add(task_str)
            task_str = self._aliases[task_str]
        return task_str

    def wire_name_to_string(self, task):
        """
        Return the name used when serializing ``task``.

        A task that was deserialized from a message retains the name it
        arrived under. While that name is still registered as an alias for the
        task, retries and re-enqueues keep using it, so in-flight old-name
        messages remain visible to the migration accounting. Once the alias is
        retired the task is written under its canonical name.
        """
        task_str = self.task_to_string(type(task))
        wire_name = getattr(task, '_wire_name', None)
        if (wire_name and wire_name != task_str and
                self.resolve_name(wire_name) == task_str):
            return wire_name
        return task_str

    def register(self, task_class):
        task_str = self.task_to_string(task_class)
        if task_str in self._registry:
            raise ValueError('Attempting to register a task with the same '
                             'identifier as existing task. Specify a different'
                             ' name= to register this task. "%s"' % task_str)
        if task_str in self._aliases:
            raise ValueError('Attempting to register a task under "%s", which '
                             'is registered as an alias for "%s". Unregister '
                             'the alias first.' % (task_str,
                                                   self._aliases[task_str]))

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
        task_str = self.wire_name_to_string(task)
        if self.resolve_name(task_str) not in self._registry:
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
            task.retry_backoff)

    def create_task(self, message):
        TaskClass = self.string_to_task(self.resolve_name(message.name))

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

        task = TaskClass(
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
            message.retry_backoff)

        # Remember the name the message arrived under. If it is an alias that
        # is still registered, retries are serialized under the same name.
        if message.name != self.task_to_string(TaskClass):
            task._wire_name = message.name
        return task

    @property
    def periodic_tasks(self):
        return [task_class() for task_class in self._periodic_tasks]
