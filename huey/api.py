import copy
import datetime
import hashlib
import inspect
import itertools
import logging
import pickle
import re
import time
import traceback
import uuid
import warnings

from collections import deque
from functools import partial
from functools import wraps

from huey import signals as S
from huey.constants import EmptyData
from huey.consumer import Consumer
from huey.exceptions import BatchConflictError
from huey.exceptions import BatchIdConflictError
from huey.exceptions import BatchPublishError
from huey.exceptions import CancelExecution
from huey.exceptions import ConfigurationError
from huey.exceptions import RateLimitExceeded
from huey.exceptions import RETRYING_EXCEPTIONS
from huey.exceptions import ResultTimeout
from huey.exceptions import RetryTask
from huey.exceptions import TaskException
from huey.exceptions import TaskLockedException
from huey.exceptions import TaskTimeout
from huey.registry import Registry
from huey.serializer import Serializer
from huey.storage import BlackHoleStorage
from huey.storage import CySqliteStorage
from huey.storage import FileStorage
from huey.storage import MemoryStorage
from huey.storage import PostgresStorage
from huey.storage import PriorityRedisExpireStorage
from huey.storage import PriorityRedisStorage
from huey.storage import RedisExpireStorage
from huey.storage import RedisStorage
from huey.storage import SqliteStorage
from huey.utils import ChordConfig
from huey.utils import Error
from huey.utils import SKIPPED
from huey.utils import noop_context
from huey.utils import normalize_expire_time
from huey.utils import normalize_time
from huey.utils import utcnow


logger = logging.getLogger('huey')
_sentinel = object()


def _canonical_recipe(recipe, index):
    """
    Replace task ids in a result recipe with their position in the batch's
    traversal order, yielding a fingerprint input that is stable across
    processes (huey-generated ids are random).
    """
    kind = recipe[0]
    if kind == 'none':
        return ('none',)
    elif kind == 'result':
        return ('result', index[recipe[1]])
    elif kind == 'group':
        return ('group', tuple(_canonical_recipe(r, index)
                               for r in recipe[1]))
    elif kind == 'items':
        return ('items', tuple(_canonical_recipe(r, index)
                               for r in recipe[1]))
    elif kind == 'pipeline':
        return ('pipeline', tuple(_canonical_recipe(r, index)
                                  for r in recipe[1]))
    elif kind == 'chord':
        _, members, callback, pipeline = recipe
        return ('chord',
                tuple(_canonical_recipe(r, index) for r in members),
                _canonical_recipe(callback, index),
                (None if pipeline is None else
                 _canonical_recipe(pipeline, index)))
    raise ValueError('unknown recipe %r' % (recipe,))


def _sort_key(value):
    # Content-derived ordering that is stable across processes: pickle bytes
    # for objects that support it, a repr fallback for the rest. Never
    # compare the objects themselves (they may not be orderable).
    try:
        return pickle.dumps(value, protocol=4)
    except Exception:
        return repr(value).encode('utf-8', 'replace')


def _canonical(value):
    """
    Normalize container ordering so a content fingerprint does not depend on
    hash randomization (sets) or dict insertion order across processes.

    Collections are reduced to sequences of sort keys, so elements whose sort
    key ties are interchangeable in the fingerprint and cannot make it depend
    on iteration order.
    """
    if isinstance(value, dict):
        items = [(_sort_key(_canonical(k)), _sort_key(_canonical(v)),
                  _canonical(v))
                 for k, v in value.items()]
        # Sort by both keys so tied keys cannot make the result depend on
        # insertion order; a full tie means the pairs are identical.
        items.sort(key=lambda i: (i[0], i[1]))
        return (type(value).__name__, tuple((k, v) for k, _, v in items))
    elif isinstance(value, (set, frozenset)):
        return (type(value).__name__,
                tuple(sorted(_sort_key(_canonical(v)) for v in value)))
    elif isinstance(value, (list, tuple)):
        return (type(value).__name__, tuple(_canonical(v) for v in value))
    return value


class _BatchPlan(object):
    def __init__(self):
        self.entries = []       # (serialized task data, priority) pairs.
        self.task_ids = []      # every task id in every staged graph.
        self.roots = []         # visible task of each queued entry.
        self.emitted = []       # task objects to signal after commit.
        self._seen_objects = set()
        self._seen_ids = set()

    def reserve(self, task):
        """
        Record every id reachable from ``task``, so that the storage can
        reject collisions before writing anything. Shared references are
        visited once; two distinct tasks with the same id are an error.
        """
        if task is None or id(task) in self._seen_objects:
            return
        self._seen_objects.add(id(task))
        if task.id in self._seen_ids:
            raise BatchIdConflictError(
                'Task id %r appears more than once in the batch.' % task.id)
        self._seen_ids.add(task.id)
        self.task_ids.append(task.id)
        self.reserve(task.on_complete)
        self.reserve(task.on_error)
        if task.chord_config is not None:
            self.reserve(task.chord_config.callback)

    def add(self, huey, task, data):
        if task.id in self._seen_ids:
            raise BatchIdConflictError(
                'Task id %r appears more than once in the batch.' % task.id)
        self.reserve(task)
        self.entries.append((data, task.priority))
        self.roots.append(task)
        self.emitted.append(task)

    def fingerprint(self, huey, recipe):
        identities = tuple(huey._task_identity(root) for root in self.roots)
        index = {}
        for i, task_id in enumerate(self.task_ids):
            index.setdefault(task_id, i)
        shape = _canonical_recipe(recipe, index)
        # Protocol 4 is fixed so that independently-built batches agree
        # regardless of the interpreter's default pickle protocol.
        try:
            payload = pickle.dumps((_canonical(identities),
                                    _canonical(shape)), protocol=4)
        except Exception as exc:
            raise BatchPublishError(
                'Cannot compute a stable fingerprint for this batch; all '
                'task arguments must be picklable: %s' % exc) from exc
        return hashlib.sha256(payload).hexdigest()


class Huey(object):
    """
    Huey executes tasks by exposing function decorators that cause the function
    call to be enqueued for execution by a separate consumer process.

    :param name: a name for the task queue, e.g. your application's name.
    :param bool results: whether to store task results.
    :param bool store_none: whether to store ``None`` in the result store.
    :param bool store_intermediate_errors: when a task fails but has retries
        remaining, store the intermediate exception in the result store and run
        any ``on_error`` handler. When ``False``, the error is withheld until
        the task's retries are exhausted. Defaults to ``True`` for backwards
        compatibility.
    :param bool utc: use UTC internally by converting from local time.
    :param bool immediate: useful for debugging; causes tasks to be executed
        synchronously in the application.
    :param Serializer serializer: serializer implementation for tasks and
        result data. The default implementation uses pickle.
    :param bool compression: compress tasks and result data (gzip by default).
    :param bool use_zlib: use zlib for compression instead of gzip.
    :param bool immediate_use_memory: automatically switch to a local in-memory
        storage backend when immediate-mode is enabled.
    :param storage_kwargs: arbitrary keyword arguments that will be passed to
        the storage backend for additional configuration.

    Example usage::

        from huey import RedisHuey

        # Create a huey instance.
        huey = RedisHuey('my-app')

        @huey.task()
        def add_numbers(a, b):
            return a + b

        @huey.periodic_task(crontab(minute='0', hour='2'))
        def nightly_report():
            generate_nightly_report()
    """
    storage_class = None

    def __init__(self, name='huey', results=True, store_none=False, utc=True,
                 immediate=False, serializer=None, compression=False,
                 use_zlib=False, immediate_use_memory=True, storage_class=None,
                 store_intermediate_errors=True, **storage_kwargs):

        self.name = name
        self.results = results
        self.store_none = store_none
        self.store_intermediate_errors = store_intermediate_errors
        self.utc = utc
        self._immediate = immediate
        self.immediate_use_memory = immediate_use_memory
        if serializer is None:
            serializer = Serializer(compression, use_zlib=use_zlib)
        self.serializer = serializer

        # Initialize storage.
        self.storage_kwargs = storage_kwargs
        if storage_class is not None:
            self.storage_class = storage_class
        self.storage = self.create_storage()

        # Allow overriding the default TaskWrapper implementation.
        self.task_wrapper_class = self.get_task_wrapper_class()

        self._locks = set()
        self._pre_execute = {}
        self._post_execute = {}
        self._startup = {}
        self._shutdown = {}
        self._registry = Registry()
        self._signal = S.Signal()
        self._tasks_in_flight = set()
        self._timeout_handler = None  # This is consumer-specific.

    def get_task_wrapper_class(self):
        return TaskWrapper

    def create_storage(self):
        # When using immediate mode, the default behavior is to use an
        # in-memory broker rather than a live one like Redis or Sqlite, however
        # this can be overridden by specifying "immediate_use_memory=False"
        # when initializing Huey.
        if self._immediate and self.immediate_use_memory:
            return self.get_immediate_storage()

        return self.get_storage(**self.storage_kwargs)

    def get_immediate_storage(self):
        return MemoryStorage(self.name)

    def get_storage(self, **kwargs):
        if self.storage_class is None:
            warnings.warn('storage_class not specified when initializing '
                          'huey, will default to RedisStorage.')
            Storage = RedisStorage
        else:
            Storage = self.storage_class
        return Storage(self.name, **kwargs)

    @property
    def immediate(self):
        return self._immediate

    @immediate.setter
    def immediate(self, value):
        if self._immediate != value:
            self._immediate = value
            # If we are using different storage engines for immediate-mode
            # versus normal mode, we need to recreate the storage engine.
            if self.immediate_use_memory:
                self.storage = self.create_storage()

    def create_consumer(self, **options):
        return Consumer(self, **options)

    def task(self, retries=0, retry_delay=0, retry_backoff=0, priority=None,
             context=False, name=None, expires=None, timeout=None, **kwargs):
        TaskWrapper = self.task_wrapper_class
        def decorator(func):
            return TaskWrapper(
                self,
                func.func if isinstance(func, TaskWrapper) else func,
                context=context,
                name=name,
                default_retries=retries,
                default_retry_delay=retry_delay,
                default_retry_backoff=retry_backoff,
                default_priority=priority,
                default_expires=expires,
                default_timeout=timeout,
                **kwargs)
        return decorator

    def periodic_task(self, validate_datetime, retries=0, retry_delay=0,
                      retry_backoff=0, priority=None, context=False, name=None,
                      expires=None, timeout=None, **kwargs):
        TaskWrapper = self.task_wrapper_class
        def decorator(func):
            def method_validate(self, timestamp):
                return validate_datetime(timestamp)

            return TaskWrapper(
                self,
                func.func if isinstance(func, TaskWrapper) else func,
                context=context,
                name=name,
                default_retries=retries,
                default_retry_delay=retry_delay,
                default_retry_backoff=retry_backoff,
                default_priority=priority,
                default_expires=expires,
                default_timeout=timeout,
                validate_datetime=method_validate,
                task_base=PeriodicTask,
                **kwargs)

        return decorator

    def context_task(self, obj, as_argument=False, **kwargs):
        def context_decorator(fn):
            @wraps(fn)
            def inner(*a, **k):
                with obj as ctx:
                    if as_argument:
                        return fn(ctx, *a, **k)
                    else:
                        return fn(*a, **k)
            return inner
        def task_decorator(func):
            return self.task(**kwargs)(context_decorator(func))
        return task_decorator

    def pre_execute(self, name=None):
        def decorator(fn):
            self._pre_execute[name or fn.__name__] = fn
            return fn
        return decorator

    def unregister_pre_execute(self, name):
        if not isinstance(name, str):
            # Assume we were given the function itself.
            name = name.__name__
        return self._pre_execute.pop(name, None) is not None

    def post_execute(self, name=None):
        def decorator(fn):
            self._post_execute[name or fn.__name__] = fn
            return fn
        return decorator

    def unregister_post_execute(self, name):
        if not isinstance(name, str):
            # Assume we were given the function itself.
            name = name.__name__
        return self._post_execute.pop(name, None) is not None

    def on_startup(self, name=None):
        def decorator(fn):
            self._startup[name or fn.__name__] = fn
            return fn
        return decorator

    def unregister_on_startup(self, name):
        if not isinstance(name, str):
            # Assume we were given the function itself.
            name = name.__name__
        return self._startup.pop(name, None) is not None

    def on_shutdown(self, name=None):
        def decorator(fn):
            self._shutdown[name or fn.__name__] = fn
            return fn
        return decorator

    def unregister_on_shutdown(self, name=None):
        if not isinstance(name, str):
            # Assume we were given the function itself.
            name = name.__name__
        return self._shutdown.pop(name, None) is not None

    def notify_interrupted_tasks(self):
        while self._tasks_in_flight:
            try:
                task = self._tasks_in_flight.pop()
            except KeyError:
                break  # Task might have raced and finished.
            self._emit(S.SIGNAL_INTERRUPTED, task)

    def signal(self, *signals):
        def decorator(fn):
            self._signal.connect(fn, *signals)
            return fn
        return decorator

    def disconnect_signal(self, receiver, *signals):
        self._signal.disconnect(receiver, *signals)

    def _emit(self, signal, task, *args, **kwargs):
        self._signal.send(signal, task, *args, **kwargs)

    def serialize_task(self, task):
        message = self._registry.create_message(task)
        return self.serializer.serialize(message)

    def deserialize_task(self, data):
        message = self.serializer.deserialize(data)
        return self._registry.create_task(message)

    def enqueue(self, task):
        if isinstance(task, group):
            return ResultGroup([self.enqueue(t) for t in task.tasks])
        elif isinstance(task, chord):
            return self._enqueue_chord(task)

        # Resolve the expiration time when the task is enqueued.
        if task.expires:
            task.resolve_expires(self.utc)

        self._emit(S.SIGNAL_ENQUEUED, task)

        if self._immediate:
            self.execute(task)
        else:
            self.storage.enqueue(self.serialize_task(task), task.priority)

        if not self.results:
            return

        results = self._pipeline_results(task)
        return ResultGroup(results) if len(results) > 1 else results[0]

    def _enqueue_chord(self, chord_obj):
        cid = str(uuid.uuid4())
        size = len(chord_obj.tasks)
        results = []
        for i, task in enumerate(chord_obj.tasks):
            if isinstance(task, group):
                raise ValueError('cannot use `group` as a chord member - '
                                 'use .then() to convert to a `chord` first.')

            config = ChordConfig(cid, size, i, chord_obj.callback)
            results.append(self._enqueue_chord_member(task, config))

        if not size:
            callback = copy.copy(chord_obj.callback)
            callback.extend_data(([],))
            self.enqueue(callback)

        pipeline = self._pipeline_results(chord_obj.callback)
        return ChordResult(results, pipeline[0],
                           ResultGroup(pipeline) if len(pipeline) > 1 else None)

    def _enqueue_chord_member(self, task, config):
        if isinstance(task, chord):
            head = task.callback
        else:
            head = task

        tail = head
        while tail.on_complete is not None:
            tail = tail.on_complete
        tail.chord_config = config

        if isinstance(task, chord):
            self._enqueue_chord(task)
        else:
            self.enqueue(head)

        return Result(self, tail)

    def _pipeline_results(self, task):
        results = []
        while task is not None:
            results.append(Result(self, task))
            task = task.on_complete
        return results

    # -- Atomic batch publishing ------------------------------------------
    #
    # Batch publishing collects every task that would normally be written by
    # repeated calls to enqueue() and writes them in a single SQLite
    # transaction (see SqliteStorage.publish_batch). Graphs are planned
    # without touching the database, so an unserializable member or a bad
    # chord fails before anything is written.

    def publish_batch(self, items, key=None):
        """
        Atomically enqueue a group of related tasks.

        ``items`` is an iterable whose members are task signatures, ``group``
        and ``chord`` instances, or a single such value. Members may contain
        pipelines, and groups and chords may be nested. Either every member
        becomes visible to consumers at once, or -- if serialization, an id
        conflict or a database write fails -- none of them do.

        :param items: iterable of tasks/groups/chords (see above).
        :param str key: optional stable business identifier. Retrying the same
            key with the same content returns the handles produced by the
            first successful publish without writing anything again. The same
            key with different content raises :py:class:`BatchConflictError`.
        :return: a :py:class:`BatchResult` with one handle per input item, in
            input order.

        This is only supported by storage backends that can write a batch
        atomically (currently :py:class:`SqliteStorage`, and therefore
        :py:class:`SqliteHuey`). Other backends -- including
        :py:class:`MemoryHuey` and :py:class:`CySqliteHuey` -- raise
        :py:class:`BatchPublishError`; ordinary :py:meth:`enqueue` keeps its
        usual per-task behavior everywhere.

        Signals are emitted only after the transaction commits, and only for
        an actually-created batch. A failing signal receiver is logged and
        cannot roll the batch back.
        """
        if not getattr(self.storage, 'supports_batch_publish', False):
            raise BatchPublishError(
                'Atomic batch publishing is not supported by %s.' %
                type(self.storage).__name__)
        if self._immediate:
            raise BatchPublishError(
                'Atomic batch publishing is not available in immediate '
                'mode, which executes tasks synchronously instead of '
                'publishing them to a queue.')
        if key is not None and (not isinstance(key, str) or not key):
            raise TypeError('key must be a non-empty string.')
        if isinstance(items, (Task, group, chord)):
            items = (items,)
        else:
            items = tuple(items)

        plan = _BatchPlan()
        recipes = tuple(self._plan_node(item, plan) for item in items)
        recipe = ('items', recipes)

        if key is None:
            fingerprint = payload = None
        else:
            fingerprint = plan.fingerprint(self, recipe)
            payload = self.serializer.serialize(recipe)

        created, stored = self.storage.publish_batch(
            plan.entries,
            batch_key=key,
            fingerprint=fingerprint,
            payload=payload,
            task_ids=plan.task_ids)

        if not created:
            # A previous attempt committed. Return its result handles, do not
            # re-publish and do not emit signals.
            return self._batch_result(self.serializer.deserialize(stored))

        # Only now that the transaction committed is the batch announced.
        # Signal.send() already logs (and swallows) receiver errors, so a
        # failing subscriber cannot make the caller think the batch failed.
        for task in plan.emitted:
            self._emit(S.SIGNAL_ENQUEUED, task)

        return self._batch_result(recipe)

    def get_batch(self, key):
        """
        Return the :py:class:`BatchResult` stored for a published batch key,
        or ``None`` if no batch has committed under that key.

        This is how a caller that never saw the outcome of
        :py:meth:`publish_batch` (e.g. it crashed, or the response was lost)
        determines whether the batch was committed, possibly from another
        process.
        """
        if not getattr(self.storage, 'supports_batch_publish', False):
            raise BatchPublishError(
                'Atomic batch publishing is not supported by %s.' %
                type(self.storage).__name__)
        if not isinstance(key, str) or not key:
            raise TypeError('key must be a non-empty string.')
        payload = self.storage.get_batch(key)
        if payload is EmptyData:
            return None
        return self._batch_result(self.serializer.deserialize(payload))

    def _batch_result(self, recipe):
        return BatchResult(self, self._handles_from_recipe(recipe))

    def _handles_from_recipe(self, recipe):
        kind = recipe[0]
        if kind == 'none':
            return None
        elif kind == 'result':
            return Result(self, Task(id=recipe[1]))
        elif kind == 'group':
            return ResultGroup([self._handles_from_recipe(r)
                                for r in recipe[1]])
        elif kind == 'items':
            return [self._handles_from_recipe(r) for r in recipe[1]]
        elif kind == 'pipeline':
            return ResultGroup([self._handles_from_recipe(r)
                                for r in recipe[1]])
        elif kind == 'chord':
            _, members, callback, pipeline = recipe
            return ChordResult(
                [self._handles_from_recipe(r) for r in members],
                self._handles_from_recipe(callback),
                (None if pipeline is None else
                 self._handles_from_recipe(pipeline)))
        raise ValueError('unknown recipe %r' % (recipe,))

    def _plan_node(self, item, plan):
        if isinstance(item, group):
            recipes = tuple(self._plan_node(t, plan) for t in item.tasks)
            return ('group', recipes)
        elif isinstance(item, chord):
            return self._plan_chord(item, plan)
        elif isinstance(item, Task):
            self._prepare_entry(item)
            plan.add(self, item, self.serialize_task(item))
            if not self.results:
                return ('none',)
            return self._recipe_for_pipeline(item)
        raise TypeError('Cannot publish %r in a batch.' % (item,))

    def _plan_chord(self, chord_obj, plan):
        cid = str(uuid.uuid4())
        size = len(chord_obj.tasks)
        members = []
        for i, task in enumerate(chord_obj.tasks):
            if isinstance(task, group):
                raise ValueError('cannot use `group` as a chord member - '
                                 'use .then() to convert to a `chord` first.')
            config = ChordConfig(cid, size, i, chord_obj.callback)
            members.append(self._plan_chord_member(task, config, plan))

        # The callback is enqueued by the consumer when the chord completes,
        # so for a non-empty chord it is not written now -- but its id (and
        # pipeline) must still be reserved and included in the batch's
        # identity so a later batch cannot claim it. An empty chord enqueues
        # the callback immediately, like Huey.enqueue does.
        if size:
            plan.reserve(chord_obj.callback)
        else:
            callback = copy.copy(chord_obj.callback)
            callback.extend_data(([],))
            self._prepare_entry(callback)
            plan.add(self, callback, self.serialize_task(callback))

        pipeline = self._pipeline_results(chord_obj.callback)
        callback_recipe = ('result', chord_obj.callback.id)
        pipeline_recipe = None
        if len(pipeline) > 1:
            pipeline_recipe = ('pipeline', tuple(
                ('result', r.id) for r in pipeline))

        return ('chord', tuple(members), callback_recipe, pipeline_recipe)

    def _plan_chord_member(self, task, config, plan):
        if isinstance(task, chord):
            head = task.callback
        else:
            head = task

        tail = head
        while tail.on_complete is not None:
            tail = tail.on_complete
        tail.chord_config = config

        if isinstance(task, chord):
            self._plan_chord(task, plan)
        else:
            self._prepare_entry(head)
            plan.add(self, head, self.serialize_task(head))
        return ('result', tail.id)

    def _recipe_for_pipeline(self, task):
        results = self._pipeline_results(task)
        if len(results) > 1:
            return ('pipeline', tuple(('result', r.id) for r in results))
        return ('result', task.id)

    def _prepare_entry(self, task):
        # Mirrors Huey.enqueue: expiration is resolved when the task is
        # actually written. Successors in a pipeline are resolved later,
        # when the consumer re-enqueues them.
        if task.expires:
            task.resolve_expires(self.utc)

    def _task_identity(self, task):
        """
        Logical content of a task graph, used for idempotent batch retries.

        Task ids generated by huey are random, so they are not part of the
        identity. An eta may be: one derived from a relative delay is
        resolved against the retry's own clock, so it is compared as
        "scheduled", while an absolute eta is compared exactly. Ids
        explicitly supplied by the caller are part of the identity, since
        they define the result handles the caller expects.
        """
        kwargs = dict(task.kwargs)
        if kwargs.get('task') is task:
            kwargs.pop('task')  # Injected context-task argument.

        if task.eta is None:
            eta = None
        elif task._eta_relative:
            eta = ('relative',)
        else:
            eta = ('absolute', task.eta)

        chord_config = None
        if task.chord_config is not None:
            cc = task.chord_config
            # The chord id is generated per publish attempt, so it is not
            # part of the batch's logical content; membership and callback
            # are.
            chord_config = ('chord', cc.size, cc.idx,
                            self._task_identity(cc.callback))

        return (
            self._registry.task_to_string(type(task)),
            (task.id if task._id_provided else None),
            task.args,
            kwargs,
            eta,
            task.retries,
            task.retry_delay,
            task.retry_backoff,
            task.priority,
            task.expires,
            task.timeout,
            (None if task.on_complete is None else
             self._task_identity(task.on_complete)),
            (None if task.on_error is None else
             self._task_identity(task.on_error)),
            chord_config)

    def dequeue(self):
        data = self.storage.dequeue()
        if data is not None:
            return self.deserialize_task(data)

    def put(self, key, data):
        return self.storage.put_data(key, self.serializer.serialize(data))

    def put_result(self, key, data):
        return self.storage.put_data(key, self.serializer.serialize(data),
                                     is_result=True)

    def put_if_empty(self, key, data, ttl=None):
        return self.storage.put_if_empty(key, self.serializer.serialize(data),
                                         ttl)

    def get_raw(self, key, peek=False):
        if peek:
            return self.storage.peek_data(key)
        else:
            return self.storage.pop_data(key)

    def get(self, key, peek=False):
        data = self.get_raw(key, peek)
        if data is not EmptyData:
            return self.serializer.deserialize(data)

    def delete(self, key):
        return self.storage.delete_data(key)

    def _timeout_context(self, task):
        if not task.timeout or self._timeout_handler is None:
            return noop_context()

        return self._timeout_handler(task.timeout)

    def _get_timestamp(self):
        return (utcnow() if self.utc else
                datetime.datetime.now())

    def execute(self, task, timestamp=None):
        if timestamp is None:
            timestamp = self._get_timestamp()

        if not self.ready_to_run(task, timestamp):
            self.add_schedule(task)
        elif self.is_revoked(task, timestamp, False):
            logger.warning('Task %s was revoked, not executing', task)
            self._emit(S.SIGNAL_REVOKED, task)
            self._abort_chord_member(task, SKIPPED)
        elif task.expires_resolved and task.expires_resolved < timestamp:
            logger.info('Task %s expired, not executing.', task)
            self._emit(S.SIGNAL_EXPIRED, task)
            self._abort_chord_member(task, SKIPPED)
        else:
            logger.info('Executing %s', task)
            self._emit(S.SIGNAL_EXECUTING, task)
            return self._execute(task, timestamp)

    def _execute(self, task, timestamp):
        if self._pre_execute:
            try:
                self._run_pre_execute(task)
            except CancelExecution:
                self._emit(S.SIGNAL_CANCELED, task)
                self._abort_chord_member(task, SKIPPED)
                return

        start = time.monotonic()
        exception = None
        retry_eta = None
        task_value = None

        if task.timeout:
            task._deadline = start + task.timeout

        try:
            self._tasks_in_flight.add(task)
            try:
                with self._timeout_context(task):
                    task_value = task.execute()
            finally:
                # notify_interrupted_tasks() may have already popped it.
                self._tasks_in_flight.discard(task)
                duration = time.monotonic() - start
        except TaskTimeout as exc:
            logger.warning('Task %s timed out after %ss.', task.id,
                           task.timeout)
            exception = exc
            self._emit(S.SIGNAL_TIMEOUT, task)
        except RateLimitExceeded as exc:
            delay = task.retry_delay or exc.delay
            if exc.retry or task.retries:
                logger.info('Task %s rate-limited on "%s", retrying in %s',
                            task.id, exc.key, delay)
                retry_eta = normalize_time(None, delay, self.utc)
                if exc.retry:
                    task.retries += 1
            else:
                logger.info('Task %s rate-limited on "%s"', task.id, exc.key)
            exception = exc
            self._emit(S.SIGNAL_RATE_LIMITED, task)
        except TaskLockedException as exc:
            logger.warning('Task %s not run, %s.', task.id, exc)
            exception = exc
            self._emit(S.SIGNAL_LOCKED, task)
        except RetryTask as exc:
            logger.info('Task %s raised RetryTask, retrying.', task.id)
            task.retries += 1
            if exc.eta or exc.delay is not None:
                retry_eta = normalize_time(exc.eta, exc.delay, self.utc)
            exception = exc
        except CancelExecution as exc:
            if exc.retry or (exc.retry is None and task.retries):
                task.retries = max(task.retries, 1)
                msg = '(task will be retried)'
            else:
                task.retries = 0
                msg = '(aborted, will not be retried)'
            logger.warning('Task %s raised CancelExecution %s.', task.id, msg)
            self._emit(S.SIGNAL_CANCELED, task)
            exception = exc
        except KeyboardInterrupt:
            logger.warning('Received exit signal, %s did not finish.', task.id)
            self._emit(S.SIGNAL_INTERRUPTED, task)
            return
        except Exception as exc:
            logger.exception('Unhandled exception in task %s.', task.id)
            exception = exc
            self._emit(S.SIGNAL_ERROR, task, exc)
        else:
            logger.info('%s executed in %0.3fs', task, duration)

        # Clear the flag if this instance of the task was revoked after it
        # began executing. Expiring storages implement pop_data as a peek.
        if not isinstance(task, PeriodicTask):
            self.delete(task.revoke_id)

        store = self.results and not isinstance(task, PeriodicTask)
        if exception is None:
            if store and (task_value is not None or self.store_none):
                self.put_result(task.id, task_value)
            self._run_post_execute(task, task_value, exception)
            self._emit(S.SIGNAL_COMPLETE, task)
            if task.on_complete:
                task.on_complete.extend_data(task_value)
                self.enqueue(task.on_complete)
            if task.chord_config is not None:
                self._check_chord(task.chord_config, task_value)
        else:
            final = not task.retries
            surface = final or (self.store_intermediate_errors and
                                not isinstance(exception, RETRYING_EXCEPTIONS))
            if surface:
                error = Error(self.build_error_result(task, exception))
                next_task = task if store else None
                while next_task is not None:
                    self.put_result(next_task.id, error)
                    next_task = next_task.on_complete if final else None
            self._run_post_execute(task, task_value, exception)
            if surface and task.on_error:
                # Copy, as on_error is shared across retries.
                handler = copy.copy(task.on_error)
                handler.extend_data(exception)
                self.enqueue(handler)
            if final:
                self._abort_chord_member(task, error)
            else:
                self._emit(S.SIGNAL_RETRYING, task)
                self._requeue_task(task, self._get_timestamp(), retry_eta)

        return task_value

    def _abort_chord_member(self, task, value):
        # The rest of the pipeline won't run, so report to its chord here.
        while task is not None:
            if task.chord_config is not None:
                return self._check_chord(task.chord_config, value)
            task = task.on_complete

    def _check_chord(self, cc, value):
        chord_key = 'chord:%s' % cc.cid
        result_key = 'chord:%s:%s' % (cc.cid, cc.idx)
        self.put_result(result_key, value)

        if self.storage.incr(chord_key) == cc.size:
            self.storage.delete_counter(chord_key)

            # Expiring storages implement pop_data as a peek.
            results = []
            for idx in range(cc.size):
                key = 'chord:%s:%s' % (cc.cid, idx)
                results.append(self.get(key, peek=True))
                self.delete(key)

            callback = cc.callback
            callback.extend_data((results,))
            self.enqueue(callback)

    def _requeue_task(self, task, timestamp, retry_eta=None):
        task.retries -= 1
        logger.info('Requeueing %s, %s retries', task.id, task.retries)
        if retry_eta is not None:
            task.eta = retry_eta
            self.add_schedule(task)
        elif task.retry_delay:
            delay = datetime.timedelta(seconds=task.retry_delay)
            task.eta = timestamp + delay
            if task.retry_backoff:
                task.retry_delay *= task.retry_backoff
            self.add_schedule(task)
        else:
            self.enqueue(task)

    def _run_pre_execute(self, task):
        for name, callback in self._pre_execute.items():
            logger.debug('Pre-execute hook %s for %s.', name, task)
            try:
                callback(task)
            except CancelExecution:
                logger.warning('Task %s cancelled by %s (pre-execute).',
                               task, name)
                raise
            except Exception:
                logger.exception('Unhandled exception calling pre-execute '
                                 'hook %s for %s.', name, task)

    def _run_post_execute(self, task, task_value, exception):
        for name, callback in self._post_execute.items():
            logger.debug('Post-execute hook %s for %s.', name, task)
            try:
                callback(task, task_value, exception)
            except Exception:
                logger.exception('Unhandled exception calling post-execute '
                                 'hook %s for %s.', name, task)

    def build_error_result(self, task, exception):
        tb = ''.join(traceback.format_exception(
            type(exception), exception, exception.__traceback__))
        if isinstance(exception, TaskException):
            error = exception.metadata.get('error') or repr(exception)
        else:
            error = repr(exception)

        return {
            'error': error,
            'retries': task.retries,
            'traceback': tb,
            'task_id': task.id,
        }

    def _task_key(self, task_class, key):
        if isinstance(task_class, TaskWrapper):
            task_class = task_class.task_class
        return ':'.join((key, self._registry.task_to_string(task_class)))

    def revoke_all(self, task_class, revoke_until=None, revoke_once=False):
        if revoke_until is not None:
            revoke_until = normalize_time(revoke_until, utc=self.utc)
        self.put(self._task_key(task_class, 'rt'), (revoke_until, revoke_once))

    def restore_all(self, task_class):
        return self.delete(self._task_key(task_class, 'rt'))

    def revoke(self, task, revoke_until=None, revoke_once=False):
        if revoke_until is not None:
            revoke_until = normalize_time(revoke_until, utc=self.utc)
        self.put(task.revoke_id, (revoke_until, revoke_once))

    def restore(self, task):
        # Return value indicates whether the task was in fact revoked.
        return self.delete(task.revoke_id)

    def revoke_by_id(self, id, revoke_until=None, revoke_once=False):
        return self.revoke(Task(id=id), revoke_until, revoke_once)

    def restore_by_id(self, id):
        return self.restore(Task(id=id))

    def _check_revoked(self, data, timestamp=None, peek=True):
        """
        Given raw revocation data, returns a 2-tuple indicating:

        1. Is task revoked?
        2. Should task be restored?
        """
        if data is EmptyData:
            return False, False

        revoke_until, revoke_once = self.serializer.deserialize(data)
        if revoke_until is not None and timestamp is None:
            timestamp = self._get_timestamp()

        if revoke_once:
            # This task *was* revoked for one run, but now it should be
            # restored to normal execution (unless we are just peeking).
            return True, not peek
        elif revoke_until is not None and revoke_until <= timestamp:
            # Task is no longer revoked and can be restored.
            return False, not peek
        else:
            # Task is still revoked. Do not restore.
            return True, False

    def _revoke_keys(self, task):
        if isinstance(task, TaskWrapper) or inspect.isclass(task):
            return [self._task_key(task, 'rt')]
        if isinstance(task, Result):
            task = task.task
        if isinstance(task, Task):
            return [task.revoke_id, self._task_key(type(task), 'rt')]
        return [Task(id=task).revoke_id]

    def is_revoked(self, task, timestamp=None, peek=True):
        keys = self._revoke_keys(task)
        data = self.storage.peek_many(keys)
        for key in keys:
            revoked, restore = self._check_revoked(
                data.get(key, EmptyData), timestamp, peek)
            if restore:
                self.delete(key)
            if revoked:
                return True
        return False

    def add_schedule(self, task):
        data = self.serialize_task(task)
        eta = task.eta or datetime.datetime.fromtimestamp(0)
        self.storage.add_to_schedule(data, eta)
        logger.info('Added task %s to schedule, eta %s', task.id, eta)
        self._emit(S.SIGNAL_SCHEDULED, task)

    def read_schedule(self, timestamp=None):
        if timestamp is None:
            timestamp = self._get_timestamp()
        return self._deserialize_all(self.storage.read_schedule(timestamp))

    def _deserialize_all(self, messages):
        accum = []
        for msg in messages:
            try:
                accum.append(self.deserialize_task(msg))
            except Exception:
                logger.exception('Unable to deserialize task.')
        return accum

    def read_periodic(self, timestamp):
        if timestamp is None:
            timestamp = self._get_timestamp()
        return [task for task in self._registry.periodic_tasks
                if task.validate_datetime(timestamp)]

    def ready_to_run(self, task, timestamp=None):
        if timestamp is None:
            timestamp = self._get_timestamp()
        return task.eta is None or task.eta <= timestamp

    def pending(self, limit=None):
        return self._deserialize_all(self.storage.enqueued_items(limit))

    def pending_count(self):
        return self.storage.queue_size()

    def scheduled(self, limit=None):
        return self._deserialize_all(self.storage.scheduled_items(limit))

    def scheduled_count(self):
        return self.storage.schedule_size()

    def all_results(self):
        return self.storage.result_items()

    def result_count(self):
        return self.storage.result_store_size()

    def __bool__(self):
        return True

    def __len__(self):
        return self.pending_count()

    def flush(self):
        self.storage.flush_all()

    def lock_task(self, lock_name, ttl=None):
        return TaskLock(self, lock_name, ttl)

    def is_locked(self, lock_name):
        return TaskLock(self, lock_name).is_locked()

    def flush_locks(self, *names):
        flushed = set()
        locks = self._locks
        if names:
            lock_template = '%s.lock.%%s' % self.name
            named_locks = (lock_template % name.strip() for name in names)
            locks = itertools.chain(locks, named_locks)

        for lock_key in locks:
            if self.delete(lock_key):
                flushed.add(lock_key.split('.lock.', 1)[-1])

        return flushed

    def rate_limit(self, name, limit, per, retry=True):
        return RateLimit(self, name, limit, per, retry=retry)

    def _result_handle(self, task):
        return Result(self, task)

    def result(self, id, blocking=False, timeout=None, backoff=1.15,
               max_delay=1.0, revoke_on_timeout=False, preserve=False):
        task_result = Result(self, Task(id=id))
        return task_result.get(
            blocking=blocking,
            timeout=timeout,
            backoff=backoff,
            max_delay=max_delay,
            revoke_on_timeout=revoke_on_timeout,
            preserve=preserve)


class Task(object):
    default_expires = None
    default_priority = None
    default_retries = 0
    default_retry_delay = 0
    default_retry_backoff = 0
    default_timeout = None

    def __init__(self, args=None, kwargs=None, id=None, eta=None, retries=None,
                 retry_delay=None, priority=None, expires=None,
                 on_complete=None, on_error=None, expires_resolved=None,
                 timeout=None, chord_config=None, retry_backoff=None):
        self.name = type(self).__name__
        self.args = () if args is None else args
        self.kwargs = {} if kwargs is None else kwargs
        # Remember whether the id was supplied by the caller. Batch
        # publishing uses this to build a stable content fingerprint: ids
        # generated by huey are random per process, so they are not part of
        # the batch's logical content. An empty id is treated as absent,
        # matching the ``id or create_id()`` fallback below.
        self._id_provided = bool(id)
        self.id = id or self.create_id()
        self.revoke_id = 'r:%s' % self.id
        self.eta = eta
        # True when eta was computed from a relative delay. Batch identity
        # treats relative etas as "scheduled" rather than as an exact time,
        # because a retry resolves them against a later clock.
        self._eta_relative = False
        self.retries = retries if retries is not None else self.default_retries
        self.retry_delay = retry_delay if retry_delay is not None else \
                self.default_retry_delay
        self.retry_backoff = retry_backoff if retry_backoff is not None else \
                self.default_retry_backoff
        self.priority = priority if priority is not None else \
                self.default_priority
        self.expires = expires if expires is not None else self.default_expires
        self.expires_resolved = expires_resolved
        self.timeout = timeout if timeout is not None else self.default_timeout
        self.chord_config = chord_config
        self._deadline = None

        self.on_complete = on_complete
        self.on_error = on_error

    @property
    def data(self):
        return (self.args, self.kwargs)

    def __repr__(self):
        rep = '%s.%s: %s' % (self.__module__, self.name, self.id)
        if self.eta:
            rep += ' @%s' % self.eta
        if self.expires:
            if self.expires_resolved and self.expires != self.expires_resolved:
                rep += ' exp=%s (%s)' % (self.expires, self.expires_resolved)
            else:
                rep += ' exp=%s' % self.expires
        if self.priority:
            rep += ' p=%s' % self.priority
        if self.retries:
            rep += ' %s retries' % self.retries
        if self.timeout:
            rep += ' timeout=%s' % self.timeout
        if self.on_complete:
            rep += ' -> %s' % self.on_complete
        if self.on_error:
            rep += ', on error %s' % self.on_error
        if self.chord_config:
            rep += ', chord %s' % self.chord_config.cid
        return rep

    def __hash__(self):
        return hash(self.id)

    def create_id(self):
        return str(uuid.uuid4())

    def resolve_expires(self, utc=True):
        if self.expires:
            self.expires_resolved = normalize_expire_time(self.expires, utc)
        return self.expires_resolved

    @property
    def time_remaining(self):
        if self._deadline:
            return max(0, self._deadline - time.monotonic())
        return float('inf')

    @property
    def is_timed_out(self):
        return self._deadline and time.monotonic() >= self._deadline

    def check_timeout(self):
        if self.is_timed_out:
            raise TaskTimeout('timeout %ss' % self.timeout)

    def extend_data(self, data):
        if data is None or data == ():
            return

        if isinstance(data, tuple):
            self.args += data
        elif isinstance(data, dict):
            # XXX: alternate would be self.kwargs.update(data), but this will
            # stomp on user-provided parameters.
            for key, value in data.items():
                self.kwargs.setdefault(key, value)
        else:
            self.args = self.args + (data,)

    def then(self, task, *args, **kwargs):
        if self.on_complete:
            self.on_complete.then(task, *args, **kwargs)
        else:
            self.on_complete = _signature(task, args, kwargs)
        return self

    def error(self, task, *args, **kwargs):
        if self.on_error:
            self.on_error.error(task, *args, **kwargs)
        else:
            self.on_error = _signature(task, args, kwargs)
        return self

    def execute(self):
        # Implementation provided by subclass, see: TaskWrapper.create_task().
        raise NotImplementedError

    def __eq__(self, rhs):
        if not isinstance(rhs, Task):
            return False

        return (
            self.id == rhs.id and
            self.eta == rhs.eta and
            type(self) == type(rhs))


def _signature(task, args=(), kwargs=None):
    if not isinstance(task, Task):
        return task.s(*args, **(kwargs or {}))
    if args: task.extend_data(args)
    if kwargs: task.extend_data(kwargs)
    return task


class PeriodicTask(Task):
    def validate_datetime(self, timestamp):
        return False


class TaskWrapper(object):
    task_base = Task

    def __init__(self, huey, func, context=False, name=None, task_base=None,
                 **settings):
        self.__doc__ = getattr(func, '__doc__', None)
        self.huey = huey
        self.func = func
        self.context = context
        self.name = name
        self.settings = settings
        if task_base is not None:
            self.task_base = task_base

        # Dynamically create task class and register with Huey instance.
        self.task_class = self.create_task(func, context, name, **settings)
        self.huey._registry.register(self.task_class)

    @property
    def retries(self):
        return self.task_class.default_retries

    @property
    def retry_delay(self):
        return self.task_class.default_retry_delay

    def unregister(self):
        return self.huey._registry.unregister(self.task_class)

    def create_task(self, func, context=False, name=None, **settings):
        if inspect.iscoroutinefunction(func):
            raise ConfigurationError(
                'huey does not support async functions. Wrap the coroutine '
                'with asyncio.run() in a regular function.')

        def execute(self):
            args, kwargs = self.data
            if self.context:
                # Copy, so a retry doesn't serialize the task into its kwargs.
                kwargs = dict(kwargs, task=self)
            return func(*args, **kwargs)

        attrs = {
            'context': context,
            'execute': execute,
            '__module__': func.__module__,
            '__doc__': func.__doc__}
        attrs.update(settings)

        if not name:
            name = func.__name__

        return type(name, (self.task_base,), attrs)

    def is_revoked(self, timestamp=None, peek=True):
        return self.huey.is_revoked(self.task_class, timestamp, peek)

    def revoke(self, revoke_until=None, revoke_once=False):
        self.huey.revoke_all(self.task_class, revoke_until, revoke_once)

    def restore(self):
        return self.huey.restore_all(self.task_class)

    def schedule(self, args=None, kwargs=None, eta=None, delay=None,
                 priority=None, retries=None, retry_delay=None,
                 retry_backoff=None, expires=None, timeout=None, id=None):
        relative = False
        if eta is None and delay is None:
            if isinstance(args, (int, float)):
                delay = args
                relative = True
            elif isinstance(args, datetime.timedelta):
                delay = args.total_seconds()
                relative = True
            elif isinstance(args, datetime.datetime):
                eta = args
            else:
                raise ValueError('schedule() missing required eta= or delay=')
            args = None
        elif delay is not None:
            relative = True

        if kwargs is not None and not isinstance(kwargs, dict):
            raise ValueError('schedule() kwargs argument must be a dict.')

        eta = normalize_time(eta, delay, self.huey.utc)
        task = self.task_class(
            args or (),
            kwargs or {},
            id=id,
            eta=eta,
            retries=retries,
            retry_delay=retry_delay,
            retry_backoff=retry_backoff,
            priority=priority,
            expires=expires,
            timeout=timeout)
        task._eta_relative = relative
        return self.huey.enqueue(task)

    def _apply(self, it):
        return [self.s(*(i if isinstance(i, tuple) else (i,))) for i in it]

    def map(self, it):
        results = [self.huey.enqueue(t) for t in self._apply(it)]
        if self.huey.results:
            return ResultGroup(results)

    def __call__(self, *args, **kwargs):
        return self.huey.enqueue(self.s(*args, **kwargs))

    def call_local(self, *args, **kwargs):
        return self.func(*args, **kwargs)

    def s(self, *args, **kwargs):
        eta = kwargs.pop('eta', None)
        delay = kwargs.pop('delay', None)
        relative = delay is not None
        if delay is not None and isinstance(delay, datetime.timedelta):
            delay = delay.total_seconds()
        if eta is not None or delay is not None:
            eta = normalize_time(eta, delay, self.huey.utc)

        task = self.task_class(args, kwargs,
                               id=kwargs.pop('id', None),
                               eta=eta,
                               retries=kwargs.pop('retries', None),
                               retry_delay=kwargs.pop('retry_delay', None),
                               retry_backoff=kwargs.pop('retry_backoff', None),
                               priority=kwargs.pop('priority', None),
                               expires=kwargs.pop('expires', None),
                               timeout=kwargs.pop('timeout', None))
        task._eta_relative = relative
        return task


class TaskLock(object):
    """
    Utilize the Storage key/value APIs to implement simple locking. For more
    details see :py:meth:`Huey.lock_task`.
    """
    def __init__(self, huey, name, ttl=None):
        self._huey = huey
        self._name = name
        self._ttl = ttl
        self._key = '%s.lock.%s' % (self._huey.name, self._name)
        self._huey._locks.add(self._key)

    def is_locked(self):
        return self._huey.storage.has_data_for_key(self._key)
    locked = is_locked

    def __call__(self, fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            with self:
                return fn(*args, **kwargs)
        return inner

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._huey.delete(self._key)

    def acquire(self):
        if not self._huey.put_if_empty(self._key, '1', self._ttl):
            raise TaskLockedException('unable to acquire lock %s' % self._name)
        return True

    def clear(self):
        return self._huey.delete(self._key)
    release = clear


class RateLimit(object):
    """
    Utilize the Storage API counter to implement fixed window rate-limit.
    """
    def __init__(self, huey, name, limit, per, retry=True):
        self._huey = huey
        self._name = name
        self._limit = limit
        self._per = per
        self._retry = retry
        self._counter_key = '%s.rl.%s' % (self._huey.name, self._name)
        self._window_key = '%s.rl.%s.w' % (self._huey.name, self._name)

    def _current_window(self, now):
        return int(now) // self._per

    def _time_until_reset(self, now):
        window_end = (self._current_window(now) + 1) * self._per
        return window_end - now

    def reset(self):
        self._huey.delete(self._window_key)
        self._huey.storage.delete_counter(self._counter_key)

    def acquire(self):
        now = time.time()
        window = self._current_window(now)

        stored = self._huey.get(self._window_key, peek=True)
        if stored != window:
            self._huey.storage.delete_counter(self._counter_key)
            self._huey.put(self._window_key, window)

        count = self._huey.storage.incr(self._counter_key)
        if count > self._limit:
            ttr = self._time_until_reset(now)
            raise RateLimitExceeded(self._name, ttr, retry=self._retry)

    def current_usage(self):
        return self._huey.storage.incr(self._counter_key, 0)

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


class group(object):
    def __init__(self, tasks):
        self.tasks = tasks

    def then(self, task, *args, **kwargs):
        return chord(self.tasks, _signature(task, args, kwargs))

    def error(self, *args, **kwargs):
        for task in self.tasks:
            task.error(*args, **kwargs)
        return self


class chord(object):
    def __init__(self, tasks, callback):
        self.tasks = tasks
        self.callback = _signature(callback)

    def then(self, task, *args, **kwargs):
        self.callback.then(task, *args, **kwargs)
        return self

    def error(self, task, *args, **kwargs):
        self.callback.error(task, *args, **kwargs)
        return self


class Result(object):
    """
    Wrapper around task result data. When a task is executed, an instance of
    ``Result`` is returned to provide access to the return value.

    To retrieve the task's result value, you can simply call the wrapper::

        @huey.task()
        def my_task(a, b):
            return a + b

        result = my_task(1, 2)

        # After a moment, when the consumer has executed the task and put
        # the result in the result storage, we can retrieve the value.
        print result()  # Prints 3

        # If you want to block until the result is ready, you can pass
        # blocking=True. We'll also specify a 4 second timeout so we don't
        # block forever if the consumer goes down:
        result2 = my_task(2, 3)
        print(result(blocking=True, timeout=4))
    """
    def __init__(self, huey, task):
        self.huey = huey
        self.task = task
        self.revoke_id = task.revoke_id
        self._result = EmptyData

    def __repr__(self):
        return '<Result: task %s>' % self.id

    @property
    def id(self):
        return self.task.id

    def __call__(self, *args, **kwargs):
        return self.get(*args, **kwargs)

    def is_ready(self):
        return self._get() is not EmptyData

    def _get(self, preserve=False):
        task_id = self.id
        if self._result is EmptyData:
            res = self.huey.get_raw(task_id, peek=preserve)

            if res is not EmptyData:
                self._result = self.huey.serializer.deserialize(res)
                return self._result
            else:
                return res
        else:
            return self._result

    def get_raw_result(self, blocking=False, timeout=None, backoff=1.15,
                       max_delay=1.0, revoke_on_timeout=False, preserve=False):
        res = self._get(preserve)
        if res is not EmptyData:
            return res
        elif not blocking:
            return

        if self.huey.storage.wait_result(self.id, timeout, backoff, max_delay):
            res = self._get(preserve)
            if res is not EmptyData:
                return res

        if revoke_on_timeout:
            self.revoke()
        raise ResultTimeout('timed out waiting for result')

    def get(self, blocking=False, timeout=None, backoff=1.15, max_delay=1.0,
            revoke_on_timeout=False, preserve=False):
        result = self.get_raw_result(blocking, timeout, backoff, max_delay,
                                     revoke_on_timeout, preserve)
        if result is not None and isinstance(result, Error):
            raise TaskException(result.metadata)
        return result

    def is_revoked(self):
        return self.huey.is_revoked(self.task, peek=True)

    def revoke(self, revoke_once=True):
        self.huey.revoke(self.task, revoke_once=revoke_once)

    def restore(self):
        return self.huey.restore(self.task)

    def reschedule(self, eta=None, delay=None, expires=None, priority=None,
                   preserve_pipeline=True):
        # Rescheduling works by revoking the currently-scheduled task (nothing
        # is done to check if the task has already run, however). Then the
        # original task's data is used to enqueue a new task with a new task ID
        # and execution_time.
        self.revoke()
        if eta is not None or delay is not None:
            eta = normalize_time(eta, delay, self.huey.utc)
        if preserve_pipeline:
            on_complete = self.task.on_complete
            on_error = self.task.on_error
        else:
            on_complete = on_error = None

        task = type(self.task)(
            self.task.args,
            self.task.kwargs,
            eta=eta,
            retries=self.task.retries,
            retry_delay=self.task.retry_delay,
            retry_backoff=self.task.retry_backoff,
            priority=priority if priority is not None else self.task.priority,
            expires=expires if expires is not None else self.task.expires,
            timeout=self.task.timeout,
            chord_config=self.task.chord_config,
            on_complete=on_complete,
            on_error=on_error)
        return self.huey.enqueue(task)

    def reset(self):
        self._result = EmptyData


class BatchResult(object):
    """
    Handles produced by :py:meth:`Huey.publish_batch`.

    ``members`` mirrors the input: one handle per published item, in input
    order. For an empty batch ``members`` is an empty tuple.
    """
    def __init__(self, huey, members):
        self.huey = huey
        self.members = tuple(members)

    def __len__(self):
        return len(self.members)

    def __iter__(self):
        return iter(self.members)

    def __getitem__(self, idx):
        return self.members[idx]

    def get(self, *args, **kwargs):
        # With results disabled, a member is None, matching enqueue().
        return [None if member is None else member.get(*args, **kwargs)
                for member in self.members]
    __call__ = get

    def __repr__(self):
        return '<BatchResult: %s members>' % len(self.members)


class ResultGroup(object):
    def __init__(self, results):
        self._results = results

    def get(self, *args, **kwargs):
        return [result.get(*args, **kwargs) for result in self._results]
    __call__ = get

    def __getitem__(self, idx):
        return self._results[idx].get(True)
    def __iter__(self):
        return iter(self._results)
    def __len__(self):
        return len(self._results)
    def as_completed(self, backoff=1.15, max_delay=1.0):
        res = deque(self._results)
        delay = {r.id: 0. for r in res}
        while res:
            r = res.popleft()
            if delay[r.id]:
                time.sleep(delay[r.id])
            if r._get() is EmptyData:
                res.append(r)
                delay[r.id] = min((delay[r.id] or 0.1) * backoff, max_delay)
            else:
                yield r.get()


class ChordResult(object):
    def __init__(self, results, callback_result, pipeline=None):
        self.results = ResultGroup(results)
        self.callback = callback_result
        self.pipeline_results = pipeline

    def get(self, *args, **kwargs):
        return self.callback.get(*args, **kwargs)
    __call__ = get

    def reset(self):
        self.callback.reset()


dash_re = re.compile(r'(\d+)-(\d+)(?:/(\d+))?')
every_re = re.compile(r'\*\/(\d+)')


def crontab(minute='*', hour='*', day='*', month='*', day_of_week='*', strict=False):
    """
    Convert a "crontab"-style set of parameters into a test function that will
    return True when the given datetime matches the parameters set forth in
    the crontab.

    For day-of-week, 0=Sunday and 6=Saturday.

    Acceptable inputs:
    * = every distinct value
    */n = run every "n" times, i.e. hours='*/4' == 0, 4, 8, 12, 16, 20
    m-n = run every time m..n
    m,n = run on m and n

    The strict parameter will cause crontab to raise a ValueError if an input
    does not match a supported crontab input format. This provides backwards
    compatibility.
    """
    validation = (
        ('m', month, range(1, 13)),
        ('d', day, range(1, 32)),
        ('w', day_of_week, range(8)), # 0-6, but also 7 for Sunday.
        ('H', hour, range(24)),
        ('M', minute, range(60))
    )
    cron_settings = []

    for (date_str, value, acceptable) in validation:
        settings = set([])

        if isinstance(value, int):
            value = str(value)

        for piece in value.split(','):
            if piece == '*':
                settings.update(acceptable)
                continue

            if piece.isdigit():
                piece = int(piece)
                if piece not in acceptable:
                    raise ValueError('%d is not a valid input' % piece)
                elif date_str == 'w':
                    piece %= 7
                settings.add(piece)
                continue

            dash_match = dash_re.fullmatch(piece)
            if dash_match:
                lhs, rhs, step = dash_match.groups()
                lhs, rhs, step = int(lhs), int(rhs), int(step or 1)
                if lhs not in acceptable or rhs not in acceptable:
                    raise ValueError('%s is not a valid input' % piece)
                if lhs <= rhs:
                    values = list(range(lhs, rhs + 1))
                else:
                    hi = 6 if date_str == 'w' else acceptable[-1]
                    values = (list(range(lhs, hi + 1)) +
                              list(range(acceptable[0], rhs + 1)))
                values = values[::step]
                if date_str == 'w':
                    values = [v % 7 for v in values]
                settings.update(values)
                continue

            # Handle stuff like */3, */6.
            every_match = every_re.fullmatch(piece)
            if every_match:
                if date_str == 'w':
                    raise ValueError('Cannot perform this kind of matching'
                                     ' on day-of-week.')
                interval = int(every_match.groups()[0])
                settings.update(acceptable[::interval])
                continue

            # Older versions of Huey would, at this point, ignore the unmatched piece.
            if strict:
                raise ValueError('%s is not a valid input' % piece)

        if not settings:
            raise ValueError('%s matches no values' % value)
        cron_settings.append(sorted(list(settings)))

    def validate_date(timestamp):
        _, m, d, H, M, _, w, _, _ = timestamp.timetuple()

        # fix the weekday to be sunday=0
        w = (w + 1) % 7

        for (date_piece, selection) in zip((m, d, w, H, M), cron_settings):
            if date_piece not in selection:
                return False

        return True

    return validate_date


# Convenience helpers.
crontab.hourly = partial(crontab, minute='0')
crontab.daily = partial(crontab, minute='0', hour='0')


# Convenience wrappers for the various storage implementations.
class BlackHoleHuey(Huey):
    storage_class = BlackHoleStorage

class MemoryHuey(Huey):
    storage_class = MemoryStorage

class SqliteHuey(Huey):
    storage_class = SqliteStorage

class CySqliteHuey(Huey):
    storage_class = CySqliteStorage

class PostgresHuey(Huey):
    storage_class = PostgresStorage

class RedisHuey(Huey):
    storage_class = RedisStorage

class RedisExpireHuey(RedisHuey):
    storage_class = RedisExpireStorage

class PriorityRedisHuey(RedisHuey):
    storage_class = PriorityRedisStorage

class PriorityRedisExpireHuey(RedisHuey):
    storage_class = PriorityRedisExpireStorage

class FileHuey(Huey):
    storage_class = FileStorage
