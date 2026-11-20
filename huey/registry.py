from collections import namedtuple

from huey.exceptions import HueyException
from huey.exceptions import MessageVersionError
from huey.utils import ChordConfig


# The message format written by Huey releases that predate the versioned
# message contract. It is deliberately kept unchanged: while a schema stays at
# version 1, producers keep writing exactly this payload, so consumers that
# predate versioning can still read the queue.
Message = namedtuple('Message', ('id', 'name', 'eta', 'retries', 'retry_delay',
                                 'priority', 'args', 'kwargs', 'on_complete',
                                 'on_error', 'expires', 'expires_resolved',
                                 'timeout', 'chord_config', 'retry_backoff'))

# Automatically set missing parameters to None. This is kind-of a hack, but it
# allows us to add new parameters while continuing to be able to handle
# messages enqueued with a smaller-set of arguments.
Message.__new__.__defaults__ = (None,) * len(Message._fields)


# Wraps a Message (and, recursively, any nested pipeline message) when the
# producer uses a message schema version that differs from the legacy version.
# A consumer that does not know about versions cannot unpickle this class, so
# it refuses the message instead of silently running it with defaults.
VersionedMessage = namedtuple('VersionedMessage',
                              ('schema_version', 'message'))


class Registry(object):
    def __init__(self, message_schema=None):
        self._registry = {}
        self._periodic_tasks = []
        self.message_schema = message_schema
        # Incremented whenever the set of registered tasks changes, so callers
        # can cache a successful schema/registry validation.
        self.generation = 0

    def task_to_string(self, task_class):
        return '%s.%s' % (task_class.__module__, task_class.__name__)

    def register(self, task_class):
        task_str = self.task_to_string(task_class)
        if task_str in self._registry:
            raise ValueError('Attempting to register a task with the same '
                             'identifier as existing task. Specify a different'
                             ' name= to register this task. "%s"' % task_str)

        self._registry[task_str] = task_class
        self.generation += 1
        if hasattr(task_class, 'validate_datetime'):
            self._periodic_tasks.append(task_class)
        # Surface alias conflicts/cycles as soon as a task is registered. The
        # alias target does not have to exist yet, because the application
        # controls import order; full registry membership is checked before
        # anything is consumed.
        if self.message_schema is not None:
            self.message_schema.validate()
        return True

    def unregister(self, task_class):
        task_str = self.task_to_string(task_class)
        if task_str not in self._registry:
            return False

        del self._registry[task_str]
        self.generation += 1
        if hasattr(task_class, 'validate_datetime'):
            self._periodic_tasks = [t for t in self._periodic_tasks
                                    if t is not task_class]
        return True

    def string_to_task(self, task_str):
        if task_str not in self._registry:
            raise HueyException('%s not found in TaskRegistry' % task_str)
        return self._registry[task_str]

    def resolve_task_name(self, task_str):
        """
        Resolve a task name through the configured aliases.

        Without a schema, the name is returned unchanged. Aliases are only
        matched against the full ``"module.Class"`` name - short function
        names are never used.
        """
        if self.message_schema is None:
            return task_str
        return self.message_schema.resolve_name(task_str)

    def _serialize_version(self):
        schema = self.message_schema
        if schema is None or schema.version <= 1:
            # Version 1 is the legacy payload: keep writing exactly the old
            # format so un-upgraded consumers are not broken unnecessarily.
            # Once the version advances, every message (including nested
            # pipeline messages) is stamped so old readers can reject it.
            return None
        return schema.version

    def _maybe_versioned(self, message, version):
        if version is None:
            return message
        return VersionedMessage(version, message)

    def create_message(self, task):
        task_str = self.task_to_string(type(task))
        if task_str not in self._registry:
            raise HueyException('%s not found in TaskRegistry' % task_str)

        # Remove an injected context-task instance before serializing.
        if task.kwargs and task.kwargs.get('task') is task:
            task.kwargs.pop('task')

        version = self._serialize_version()

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

        message = Message(
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
        return self._maybe_versioned(message, version)

    def _unwrap_message(self, message, path):
        """
        Return ``(version, plain_message)`` for a message read from the queue.

        A missing version means the message predates versioning (or the schema
        has not bumped its version). A message carrying a version when no
        schema is configured is refused, never executed with defaults.
        """
        if isinstance(message, VersionedMessage):
            version = message.schema_version
            inner = message.message
            if not isinstance(inner, Message):
                raise MessageVersionError(
                    version, getattr(inner, 'name', None),
                    getattr(inner, 'id', None), path, supported=(),
                    reason='malformed versioned message')
        elif isinstance(message, Message):
            version, inner = None, message
        else:
            raise MessageVersionError(
                None, getattr(message, 'name', None),
                getattr(message, 'id', None), path, supported=(),
                reason='unknown message type %r' % type(message).__name__)

        if self.message_schema is None:
            if version is not None:
                raise MessageVersionError(
                    version, inner.name, inner.id, path, supported=(),
                    reason='message versioning is not enabled')
            return None, inner

        version = self.message_schema.check_version(
            version, inner.name, inner.id, path)
        return version, inner

    def create_task(self, message, path=()):
        """
        Rebuild a task instance from a message, applying aliases and the
        caller-registered argument migrations to this message and to every
        nested pipeline message.

        ``path`` identifies the location of this message within a pipeline
        (``on_complete`` / ``on_error`` / ``chord_config``) so that version
        errors can be located precisely.
        """
        version, message = self._unwrap_message(message, path)

        task_name = self.resolve_task_name(message.name)
        # Resolve the task before running a migration, so an unknown alias
        # target fails without invoking any conversion function.
        TaskClass = self.string_to_task(task_name)

        args, kwargs = message.args, message.kwargs
        if self.message_schema is not None:
            # Migrations run before the task is constructed, so a failed
            # conversion can never leave a half-migrated task to execute.
            args, kwargs = self.message_schema.migrate(
                task_name, version, message.args, message.kwargs,
                message.id, path)

        on_complete = None
        if message.on_complete is not None:
            on_complete = self.create_task(message.on_complete,
                                           path + ('on_complete',))

        on_error = None
        if message.on_error is not None:
            on_error = self.create_task(message.on_error,
                                        path + ('on_error',))

        chord_config = None
        if message.chord_config is not None:
            cid, size, idx, cb_ser = message.chord_config
            callback = self.create_task(cb_ser,
                                        path + ('chord_config', 'callback'))
            chord_config = ChordConfig(cid, size, idx, callback)

        return TaskClass(
            args,
            kwargs,
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

    @property
    def periodic_tasks(self):
        return [task_class() for task_class in self._periodic_tasks]
