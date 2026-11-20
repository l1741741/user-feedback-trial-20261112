import copy
import datetime
import pickle

from huey.api import MemoryHuey
from huey.exceptions import ConfigurationError
from huey.exceptions import HueyException
from huey.exceptions import MessageMigrationError
from huey.exceptions import MessageVersionError
from huey.exceptions import TaskAliasError
from huey.registry import Message
from huey.registry import Registry
from huey.registry import VersionedMessage
from huey.schema import MessageSchema
from huey.serializer import Serializer
from huey.tests.base import BaseTestCase


def identity(args, kwargs):
    return (args, kwargs)


class BaseSchemaTestCase(BaseTestCase):
    ETA = datetime.datetime(2030, 1, 1, 12, 30)

    def get_huey(self, **kwargs):
        return MemoryHuey(utc=False, **kwargs)

    def register(self, huey, name):
        """Register an empty task and return its fully-qualified name."""
        def fn(*args, **kwargs):
            return (args, kwargs)
        wrapper = huey.task(name=name)(fn)
        return huey._registry.task_to_string(wrapper.task_class)

    def plain(self, name, args=(), kwargs=None, **extra):
        values = dict(id='id-1', name=name, eta=None, retries=0,
                      retry_delay=0, priority=0, args=args,
                      kwargs=kwargs or {})
        values.update(extra)
        return Message(**values)

    def rolled(self, version, message):
        return Serializer().serialize(VersionedMessage(version, message))


class TestSchemaConfiguration(BaseSchemaTestCase):
    def test_disabled_by_default(self):
        self.assertTrue(self.huey.message_schema is None)
        self.assertTrue(self.huey._registry.message_schema is None)

    def test_invalid_version(self):
        for version in (0, -1, '2', 2.0, True):
            self.assertRaises(ConfigurationError, MessageSchema, version)

    def test_legacy_version_must_not_exceed_current(self):
        self.assertRaises(ConfigurationError, MessageSchema, version=1,
                          legacy_version=2)

    def test_bare_alias_names_rejected(self):
        # Short names are never matched, so they are not accepted as either
        # side of an alias.
        for old, new in (('send_report', 'app.tasks.send_report'),
                         ('app.tasks.send_report', 'send_report')):
            self.assertRaises(
                ConfigurationError, MessageSchema, version=2,
                aliases={old: new})

    def test_alias_conflict_rejected(self):
        schema = MessageSchema(version=2)
        schema.add_alias('app.v1.task', 'app.task')
        # Re-registering the same mapping is idempotent.
        schema.add_alias('app.v1.task', 'app.task')
        self.assertRaises(TaskAliasError, schema.add_alias,
                          'app.v1.task', 'app.other')

    def test_alias_cycle_rejected(self):
        schema = MessageSchema(version=2)
        schema.add_alias('a.task', 'b.task')
        self.assertRaises(TaskAliasError, schema.add_alias, 'b.task', 'a.task')
        # The rejected alias is rolled back, leaving a usable schema.
        self.assertEqual(schema.aliases, {'a.task': 'b.task'})
        self.assertTrue(schema.resolve_name('b.task') == 'b.task')
        self.assertTrue(schema.validate())

    def test_self_alias_rejected(self):
        self.assertRaises(TaskAliasError, MessageSchema, version=2,
                          aliases={'app.task': 'app.task'})

    def test_alias_chain(self):
        schema = MessageSchema(version=2, aliases={
            'app.v1.task': 'app.v2.task',
            'app.v2.task': 'app.task'})
        self.assertEqual(schema.resolve_name('app.v1.task'), 'app.task')
        self.assertEqual(schema.resolve_name('app.v2.task'), 'app.task')
        self.assertEqual(schema.resolve_name('app.task'), 'app.task')
        # Short names are never matched.
        self.assertEqual(schema.resolve_name('task'), 'task')

    def test_alias_source_cannot_have_migrations(self):
        self.assertRaises(
            ConfigurationError, MessageSchema, version=2,
            aliases={'app.old': 'app.new'},
            migrations={'app.old': [(1, 2, identity)]})

    def test_migration_validation(self):
        self.assertRaises(ConfigurationError, MessageSchema, version=2,
                          migrations={'app.t': [(2, 1, identity)]})
        self.assertRaises(ConfigurationError, MessageSchema, version=2,
                          migrations={'app.t': [(1, 3, identity)]})
        self.assertRaises(ConfigurationError, MessageSchema, version=2,
                          migrations={'app.t': [(1, 2, 'not-callable')]})
        self.assertRaises(ConfigurationError, MessageSchema, version=2,
                          migrations={'t': [(1, 2, identity)]})
        self.assertRaises(ConfigurationError, MessageSchema, version=2,
                          migrations={'app.t': [('1', 2, identity)]})

    def test_migration_chain_must_be_complete(self):
        self.assertRaises(
            ConfigurationError, MessageSchema, version=3,
            migrations={'app.t': [(1, 2, identity)]})

    def test_migration_chain_resolves(self):
        def m1(args, kwargs):
            return (args, dict(kwargs, step=1))
        def m2(args, kwargs):
            return (args, dict(kwargs, step=2))
        schema = MessageSchema(version=3, migrations={
            'app.t': [(1, 2, m1), (2, 3, m2)]})
        args, kwargs = schema.migrate('app.t', 1, (1,), {})
        self.assertEqual(args, (1,))
        self.assertEqual(kwargs, {'step': 2})

    def test_conflicting_migration_rejected(self):
        other = lambda a, k: (a, k)
        schema = MessageSchema(version=2)
        schema.add_migration('app.t', 1, 2, identity)
        schema.add_migration('app.t', 1, 2, identity)  # idempotent
        self.assertRaises(ConfigurationError, schema.add_migration,
                          'app.t', 1, 2, other)

    def test_compatibility_check_with_registry(self):
        class BadRegistry(object):
            pass

        schema = MessageSchema(version=2, aliases={'app.old': 'app.new'})
        self.assertRaises(ConfigurationError, schema.validate_registry,
                          BadRegistry())

        registry = Registry()
        # Aliased target is not registered yet.
        self.assertRaises(TaskAliasError, schema.validate_registry, registry)

        registry._registry['app.new'] = object
        self.assertTrue(schema.validate_registry(registry))

    def test_alias_source_that_is_registered_is_rejected(self):
        huey = self.get_huey()
        target = self.register(huey, 'target')
        source = self.register(huey, 'source')
        schema = MessageSchema(version=2, aliases={source: target})
        self.assertRaises(TaskAliasError, schema.validate_registry,
                          huey._registry)

    def test_alias_target_must_resolve_to_registered_task(self):
        huey = self.get_huey()
        target = self.register(huey, 'target')
        # Alias points at an unregistered intermediate name.
        schema = MessageSchema(
            version=2, aliases={'app.legacy': 'app.missing'})
        huey._registry.message_schema = schema
        huey.message_schema = schema
        self.assertRaises(TaskAliasError, schema.validate_registry,
                          huey._registry)

    def test_migration_for_unregistered_task_rejected(self):
        schema = MessageSchema(version=2,
                               migrations={'app.missing': [(1, 2, identity)]})
        self.assertRaises(ConfigurationError, schema.validate_registry,
                          Registry())

    def test_non_schema_argument_rejected(self):
        class Fake(object):
            version = 1

        self.assertRaises(ConfigurationError, MemoryHuey,
                          message_schema=Fake())

    def test_serializer_without_schema_support_rejected(self):
        class OldSerializer(Serializer):
            supports_message_schema = False

        self.assertRaises(ConfigurationError, MemoryHuey,
                          message_schema=MessageSchema(version=1),
                          serializer=OldSerializer())

    def test_custom_serializer_allowed_when_opted_in(self):
        class NewSerializer(Serializer):
            pass

        huey = MemoryHuey(message_schema=MessageSchema(version=2),
                          serializer=NewSerializer())
        self.assertTrue(huey.message_schema is not None)

    def test_custom_registry_rejected_when_schema_enabled(self):
        class CustomHuey(MemoryHuey):
            def __init__(self, **kwargs):
                self.before = True
                super(CustomHuey, self).__init__(**kwargs)
                self._registry = object()

        huey = CustomHuey(utc=False, message_schema=MessageSchema(version=2))
        self.assertRaises(ConfigurationError, huey.validate_message_schema)

    def test_serializer_that_cannot_roundtrip_versioned_message_rejected(self):
        class LossySerializer(Serializer):
            # Claims support but mangles a versioned wrapper.
            def _deserialize(self, data):
                obj = super(LossySerializer, self)._deserialize(data)
                if isinstance(obj, VersionedMessage):
                    return Message(*obj.message)
                return obj

        self.assertRaises(ConfigurationError, MemoryHuey,
                          message_schema=MessageSchema(version=2),
                          serializer=LossySerializer())

    def test_consumer_start_validates_schema(self):
        schema = MessageSchema(version=2, aliases={'app.old': 'app.new'})
        huey = MemoryHuey(utc=False, message_schema=schema)
        consumer = huey.create_consumer(initial_delay=0.001,
                                        check_worker_health=False)
        self.assertRaises(TaskAliasError, consumer.start)


class TestSchemaVersions(BaseSchemaTestCase):
    def test_default_messages_remain_legacy(self):
        huey = self.get_huey(message_schema=MessageSchema(version=1))

        @huey.task()
        def add(a, b):
            return a + b

        message = huey._registry.create_message(add.s(1, 2))
        self.assertIsInstance(message, Message)
        self.assertFalse(isinstance(message, VersionedMessage))

    def test_message_field_order_is_unchanged(self):
        self.assertEqual(Message._fields, (
            'id', 'name', 'eta', 'retries', 'retry_delay', 'priority',
            'args', 'kwargs', 'on_complete', 'on_error', 'expires',
            'expires_resolved', 'timeout', 'chord_config', 'retry_backoff'))

    def test_versioned_message_wrapper(self):
        huey = self.get_huey(message_schema=MessageSchema(version=2))

        @huey.task()
        def add(a, b):
            return a + b

        message = huey._registry.create_message(add.s(1, 2))
        self.assertIsInstance(message, VersionedMessage)
        self.assertEqual(message.schema_version, 2)
        self.assertEqual(message.message.args, (1, 2))

        task = huey.deserialize_task(Serializer().serialize(message))
        self.assertEqual(task.args, (1, 2))
        self.assertEqual(task.execute(), 3)

    def test_old_consumer_can_read_legacy_messages(self):
        huey = self.get_huey(message_schema=MessageSchema(version=2))

        @huey.task(name='add')
        def add(a, b):
            return a + b

        name = huey._registry.task_to_string(add.task_class)
        # A message still in the queue, written before the upgrade: plain
        # Message with the 15-field wire format.
        task = huey.deserialize_task(
            Serializer().serialize(self.plain(name, (1, 2))))
        self.assertEqual(task.args, (1, 2))

    def test_legacy_message_pickles_to_pre_versioning_payload(self):
        huey = self.get_huey(message_schema=MessageSchema(version=2))
        name = self.register(huey, 'add')

        # Recreate the exact class a pre-versioning consumer would import.
        from collections import namedtuple
        import huey.registry as registry_module
        LegacyMessage = namedtuple('Message', Message._fields)
        LegacyMessage.__new__.__defaults__ = (None,) * len(Message._fields)
        LegacyMessage.__module__ = 'huey.registry'
        current = registry_module.Message
        registry_module.Message = LegacyMessage
        try:
            legacy_payload = pickle.dumps(
                LegacyMessage('id', name, None, 0, 0, 0, (1, 2), {},
                              None, None, None, None, None, None, None))
        finally:
            registry_module.Message = current

        # A modern consumer reads the legacy payload without special-casing.
        task = huey.deserialize_task(legacy_payload)
        self.assertEqual(task.args, (1, 2))

    def test_old_consumer_rejects_versioned_payload(self):
        # A pre-versioning consumer has neither VersionedMessage nor the
        # version-checking code: it cannot load the payload at all.
        import huey.registry as registry_module
        from collections import namedtuple
        LegacyMessage = namedtuple('Message', Message._fields)
        LegacyMessage.__new__.__defaults__ = (None,) * len(Message._fields)
        LegacyMessage.__module__ = 'huey.registry'

        payload = self.rolled(2, self.plain('app.add', (1, 2)))
        current_message = registry_module.Message
        current_versioned = registry_module.VersionedMessage
        registry_module.Message = LegacyMessage
        del registry_module.VersionedMessage
        try:
            with self.assertRaises(Exception):
                pickle.loads(payload)
        finally:
            registry_module.Message = current_message
            registry_module.VersionedMessage = current_versioned

    def test_future_version_rejected_without_payload(self):
        huey = self.get_huey(message_schema=MessageSchema(version=2))

        @huey.task(name='add')
        def add(a, b):
            return a + b

        name = huey._registry.task_to_string(add.task_class)
        payload = self.rolled(99, self.plain(
            name, ('s3cret', 'p@ss'), id='task-abc'))
        with self.assertRaises(MessageVersionError) as ctx:
            huey.deserialize_task(payload)
        exc = ctx.exception
        self.assertEqual(exc.version, 99)
        # Locatable: names the task, the id and the supported versions...
        self.assertEqual(exc.task_name, name)
        self.assertEqual(exc.task_id, 'task-abc')
        self.assertEqual(exc.supported, {2})
        # ...without leaking the arguments.
        self.assertNotIn('s3cret', str(exc))
        self.assertNotIn('p@ss', str(exc))

    def test_future_version_in_nested_message(self):
        huey = self.get_huey(message_schema=MessageSchema(version=2))

        @huey.task(name='parent')
        def parent(x):
            pass

        @huey.task(name='child')
        def child(y):
            pass

        pname = huey._registry.task_to_string(parent.task_class)
        cname = huey._registry.task_to_string(child.task_class)
        payload = self.rolled(2, self.plain(
            pname, (1,), on_complete=VersionedMessage(
                99, self.plain(cname, (2,), id='child-id'))))
        with self.assertRaises(MessageVersionError) as ctx:
            huey.deserialize_task(payload)
        self.assertEqual(ctx.exception.path, ('on_complete',))
        self.assertEqual(ctx.exception.task_id, 'child-id')

    def test_versioned_message_without_schema_rejected(self):
        huey = self.get_huey()

        @huey.task()
        def add(a, b):
            return a + b

        name = huey._registry.task_to_string(add.task_class)
        payload = self.rolled(2, self.plain(name, (1, 2)))
        with self.assertRaises(MessageVersionError) as ctx:
            huey.deserialize_task(payload)
        self.assertEqual(ctx.exception.version, 2)
        self.assertIn('not enabled', str(ctx.exception))

    def test_malformed_version(self):
        huey = self.get_huey(message_schema=MessageSchema(version=2))

        @huey.task()
        def add(a, b):
            return a + b

        name = huey._registry.task_to_string(add.task_class)
        for bad in ('2', 0, -1, 1.5, True):
            payload = self.rolled(bad, self.plain(name, (1, 2)))
            self.assertRaises(MessageVersionError, huey.deserialize_task,
                              payload)

    def test_legacy_version_used_for_unversioned_messages(self):
        def migrate(args, kwargs):
            return ((args[0] * 10,), kwargs)

        huey = self.get_huey(message_schema=MessageSchema(version=2,
                                                          legacy_version=1))

        @huey.task(name='scale')
        def scale(a):
            return a

        name = huey._registry.task_to_string(scale.task_class)
        huey.message_schema.add_migration(name, 1, 2, migrate)
        task = huey.deserialize_task(
            Serializer().serialize(self.plain(name, (2,))))
        self.assertEqual(task.args, (20,))

    def test_legacy_message_skips_migration_when_shape_unchanged(self):
        # legacy_version == version means old messages are already current.
        huey = self.get_huey(message_schema=MessageSchema(version=2,
                                                          legacy_version=2))

        @huey.task(name='scale')
        def scale(a):
            return a

        name = huey._registry.task_to_string(scale.task_class)
        task = huey.deserialize_task(
            Serializer().serialize(self.plain(name, (2,))))
        self.assertEqual(task.args, (2,))

    def test_check_version_supported_set(self):
        schema = MessageSchema(version=3, migrations={
            'app.t': [(1, 2, identity), (2, 3, identity)]})
        self.assertEqual(schema.supported_versions('app.t'), {1, 2, 3})
        self.assertEqual(schema.check_version(1, 'app.t'), 1)
        self.assertRaises(MessageVersionError, schema.check_version, 4,
                          'app.t')


class TestSchemaAliases(BaseSchemaTestCase):
    def _huey_with_alias(self, version=2, legacy_version=1):
        huey = self.get_huey(message_schema=MessageSchema(
            version=version, legacy_version=legacy_version))
        new_name = self.register(huey, 'add')
        old_name = new_name + '_v1'
        huey.message_schema.add_alias(old_name, new_name)
        return huey, old_name, new_name

    def test_alias_resolution(self):
        huey, old_name, new_name = self._huey_with_alias()
        payload = self.rolled(2, self.plain(old_name, (1, 2)))
        task = huey.deserialize_task(payload)
        self.assertEqual(type(task).__name__, 'add')
        self.assertEqual(task.args, (1, 2))
        self.assertEqual(task.execute(), ((1, 2), {}))

    def test_alias_without_short_name_guessing(self):
        huey, old_name, new_name = self._huey_with_alias()
        # Same short name ('add') in a different module does not match.
        short_name = 'other_module.add'
        payload = self.rolled(2, self.plain(short_name, (1, 2)))
        self.assertRaises(HueyException, huey.deserialize_task, payload)

    def test_alias_chain(self):
        huey = self.get_huey(message_schema=MessageSchema(
            version=2, legacy_version=1))
        new_name = self.register(huey, 'add')
        middle = new_name + '_v2'
        oldest = new_name + '_v1'
        huey.message_schema.add_alias(middle, new_name)
        huey.message_schema.add_alias(oldest, middle)
        task = huey.deserialize_task(
            self.rolled(2, self.plain(oldest, (1, 2))))
        self.assertEqual(type(task).__name__, 'add')

    def test_missing_alias_target(self):
        huey = self.get_huey()
        schema = MessageSchema(version=2, legacy_version=1)
        huey.message_schema = schema
        huey._registry.message_schema = schema
        schema.add_alias('app.old.add', 'app.new.add')
        payload = self.rolled(2, self.plain('app.old.add', (1, 2)))
        with self.assertRaises(HueyException) as ctx:
            huey.deserialize_task(payload)
        self.assertIn('app.new.add', str(ctx.exception))

    def test_alias_and_version_migrate_together(self):
        huey = self.get_huey(message_schema=MessageSchema(
            version=2, legacy_version=1))
        new_name = self.register(huey, 'add')
        old_name = new_name + '_v1'
        huey.message_schema.add_alias(old_name, new_name)

        def migrate(args, kwargs):
            return (args, dict(kwargs, migrated=True))

        huey.message_schema.add_migration(new_name, 1, 2, migrate)
        task = huey.deserialize_task(
            Serializer().serialize(self.plain(old_name, (1,), {'b': 2})))
        self.assertEqual(type(task).__name__, 'add')
        self.assertEqual(task.kwargs, {'b': 2, 'migrated': True})

    def test_nested_pipeline_aliases_and_metadata(self):
        huey = self.get_huey(message_schema=MessageSchema(
            version=2, legacy_version=1))
        parent_name = self.register(huey, 'parent')
        child_name = self.register(huey, 'child')
        old_child = child_name + '_v1'
        huey.message_schema.add_alias(old_child, child_name)

        child = VersionedMessage(2, Message(
            'child-id', old_child, self.ETA, retries=1, retry_delay=5,
            priority=3, args=(1, 2), kwargs={}, timeout=30,
            retry_backoff=2))
        payload = self.rolled(2, self.plain(parent_name, ('x',),
                                            on_complete=child))
        task = huey.deserialize_task(payload)
        self.assertEqual(task.on_complete.name, 'child')
        self.assertEqual(task.on_complete.args, (1, 2))
        # Identity, retry, priority, ETA and timeout are preserved.
        self.assertEqual(task.on_complete.id, 'child-id')
        self.assertEqual(task.on_complete.retries, 1)
        self.assertEqual(task.on_complete.retry_delay, 5)
        self.assertEqual(task.on_complete.priority, 3)
        self.assertEqual(task.on_complete.eta, self.ETA)
        self.assertEqual(task.on_complete.timeout, 30)
        self.assertEqual(task.on_complete.retry_backoff, 2)

    def test_chord_callback_aliases_and_metadata(self):
        huey = self.get_huey(message_schema=MessageSchema(
            version=2, legacy_version=1))
        member_name = self.register(huey, 'member')
        cb_name = self.register(huey, 'cb')
        old_cb = cb_name + '_v1'
        huey.message_schema.add_alias(old_cb, cb_name)

        callback = VersionedMessage(2, Message(
            'cb-id', old_cb, None, retries=2, retry_delay=1, priority=5,
            args=('r',), kwargs={}, timeout=60, retry_backoff=3))
        payload = self.rolled(2, self.plain(
            member_name, (1,), chord_config=('cid', 2, 0, callback)))
        task = huey.deserialize_task(payload)
        cb_task = task.chord_config.callback
        self.assertEqual(cb_task.name, 'cb')
        self.assertEqual(cb_task.id, 'cb-id')
        self.assertEqual(cb_task.retries, 2)
        self.assertEqual(cb_task.priority, 5)
        self.assertEqual(cb_task.timeout, 60)
        self.assertEqual(cb_task.retry_backoff, 3)
        self.assertEqual(cb_task.args, ('r',))

    def test_on_error_alias(self):
        huey = self.get_huey(message_schema=MessageSchema(
            version=2, legacy_version=1))
        worker_name = self.register(huey, 'worker')
        err_name = self.register(huey, 'err')
        old_err = err_name + '_v1'
        huey.message_schema.add_alias(old_err, err_name)

        handler = VersionedMessage(2, Message(
            'e', old_err, None, retries=0, retry_delay=0, priority=0,
            args=('exc', 'orig'), kwargs={}))
        payload = self.rolled(2, self.plain(worker_name, (1,),
                                            on_error=handler))
        task = huey.deserialize_task(payload)
        self.assertEqual(task.on_error.name, 'err')
        self.assertEqual(task.on_error.args, ('exc', 'orig'))
        self.assertEqual(task.on_error.id, 'e')


class TestSchemaMigrations(BaseSchemaTestCase):
    def _huey(self, version=2, legacy_version=1):
        return self.get_huey(message_schema=MessageSchema(
            version=version, legacy_version=legacy_version))

    def test_migration_applied(self):
        huey = self._huey()

        @huey.task(name='add')
        def add(a, b, scale=1):
            return (a + b) * scale

        name = huey._registry.task_to_string(add.task_class)

        def migrate(args, kwargs):
            # Old messages sent (a, b) and expected scaling by 10.
            return (args, dict(kwargs, scale=10))

        huey.message_schema.add_migration(name, 1, 2, migrate)
        task = huey.deserialize_task(
            Serializer().serialize(self.plain(name, (1, 2))))
        self.assertEqual(task.kwargs, {'scale': 10})
        self.assertEqual(task.execute(), 30)

    def test_migration_runs_for_each_nested_message(self):
        calls = []
        def migrate(args, kwargs):
            calls.append(args)
            return (args, kwargs)

        huey = self._huey()
        pname = self.register(huey, 'parent')
        cname = self.register(huey, 'child')
        huey.message_schema.add_migration(pname, 1, 2, migrate)
        huey.message_schema.add_migration(cname, 1, 2, migrate)

        payload = self.rolled(1, self.plain(
            pname, (1,), on_complete=VersionedMessage(
                1, self.plain(cname, (2,)))))
        task = huey.deserialize_task(payload)
        self.assertEqual(calls, [(1,), (2,)])
        self.assertEqual(task.on_complete.args, (2,))

    def test_migration_failure_does_not_execute_partial_task(self):
        executed = []

        def bad_migration(args, kwargs):
            raise ValueError('payload detail that must not leak')

        huey = self._huey()

        @huey.task(name='add')
        def add(a, b):
            executed.append((a, b))

        name = huey._registry.task_to_string(add.task_class)
        huey.message_schema.add_migration(name, 1, 2, bad_migration)
        payload = Serializer().serialize(
            self.plain(name, ('secret', 'args')))

        with self.assertRaises(MessageMigrationError) as ctx:
            huey.deserialize_task(payload)
        self.assertEqual(executed, [])
        self.assertNotIn('secret', str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, ValueError)
        self.assertEqual(ctx.exception.task_name, name)
        self.assertEqual(ctx.exception.from_version, 1)
        self.assertEqual(ctx.exception.to_version, 2)

    def test_migration_failure_in_nested_message_aborts_all(self):
        huey = self._huey()
        pname = self.register(huey, 'parent')
        cname = self.register(huey, 'child')

        def ok(args, kwargs):
            return (args, kwargs)

        def bad(args, kwargs):
            raise RuntimeError('boom')

        huey.message_schema.add_migration(pname, 1, 2, ok)
        huey.message_schema.add_migration(cname, 1, 2, bad)
        payload = self.rolled(1, self.plain(
            pname, (1,), on_complete=VersionedMessage(
                1, self.plain(cname, (2,)))))
        with self.assertRaises(MessageMigrationError) as ctx:
            huey.deserialize_task(payload)
        self.assertEqual(ctx.exception.path, ('on_complete',))

    def test_bad_migration_return_value(self):
        huey = self._huey()

        @huey.task(name='add')
        def add(a, b):
            pass

        name = huey._registry.task_to_string(add.task_class)
        payload = Serializer().serialize(self.plain(name, (1, 2)))

        for retval in (None, (1, 2, 3), ('a', 'b'), (1, ())):
            schema = huey.message_schema
            schema._migrations = {}
            schema.add_migration(name, 1, 2, lambda a, k, r=retval: r)
            self.assertRaises(MessageMigrationError, huey.deserialize_task,
                              payload)

    def test_migration_accepts_list_args(self):
        huey = self._huey()

        @huey.task(name='add')
        def add(a, b):
            return a + b

        name = huey._registry.task_to_string(add.task_class)
        huey.message_schema.add_migration(
            name, 1, 2, lambda a, k: ([a[0], a[1]], k))
        task = huey.deserialize_task(
            Serializer().serialize(self.plain(name, (1, 2))))
        self.assertEqual(task.args, (1, 2))

    def test_migration_does_not_pollute_shared_message(self):
        def mutate_in_place(args, kwargs):
            args[0]['value'] += 1
            kwargs['injected'] = True
            return (args, kwargs)

        huey = self._huey()

        @huey.task(name='consume')
        def consume(payload, injected=False):
            return payload['value']

        name = huey._registry.task_to_string(consume.task_class)
        huey.message_schema.add_migration(name, 1, 2, mutate_in_place)

        original = self.plain(name, ({'value': 1},))
        before = copy.deepcopy(original)
        task = huey._registry.create_task(original)
        self.assertEqual(original, before)
        self.assertEqual(task.args, ({'value': 2},))
        self.assertEqual(task.kwargs, {'injected': True})

        # A second pass over the same message yields the same semantics...
        task2 = huey._registry.create_task(original)
        self.assertEqual(task2.args, task.args)
        self.assertEqual(task2.kwargs, task.kwargs)
        # ...and still does not mutate the shared message, even though the
        # migration received a mutable copy twice.
        self.assertEqual(original, before)
        self.assertIsNot(task.args[0], task2.args[0])

    def test_unknown_alias_target_skips_migration(self):
        huey = self._huey()
        # A migration exists for the canonical name, but the alias resolves
        # to an unregistered task, so it must not execute a conversion for a
        # task the consumer cannot run.
        called = []

        @huey.task(name='canonical')
        def canonical(a, b):
            pass

        name = huey._registry.task_to_string(canonical.task_class)
        huey.message_schema.add_migration(
            name, 1, 2, lambda a, k: (called.append(1), (a, k))[1])
        huey.message_schema.add_alias('app.old.canonical', 'app.missing')

        # Call the registry directly (bypassing the pre-consume validation) so
        # this exercises the ordering inside create_task: an unresolvable
        # task must fail without invoking any conversion function.
        message = self.plain('app.old.canonical', (1, 2))
        with self.assertRaises(HueyException):
            huey._registry.create_task(message)
        self.assertEqual(called, [])

    def test_no_migration_path(self):
        huey = self._huey()

        @huey.task(name='add')
        def add(a, b):
            pass

        name = huey._registry.task_to_string(add.task_class)
        payload = Serializer().serialize(self.plain(name, (1, 2)))
        with self.assertRaises(MessageVersionError) as ctx:
            huey.deserialize_task(payload)
        self.assertEqual(ctx.exception.version, 1)
        self.assertNotIn('(1, 2)', str(ctx.exception))

    def test_migrations_only_run_for_registered_conversions(self):
        # A message at the current version is never passed through a
        # migration, even if one exists for older versions.
        called = []
        huey = self._huey()

        @huey.task(name='add')
        def add(a, b):
            return a + b

        name = huey._registry.task_to_string(add.task_class)
        huey.message_schema.add_migration(
            name, 1, 2, lambda a, k: (called.append(1), (a, k))[1])
        task = huey.deserialize_task(
            self.rolled(2, self.plain(name, (1, 2))))
        self.assertEqual(called, [])
        self.assertEqual(task.execute(), 3)

    def test_roundtrip_through_queue_is_stable(self):
        # The same legacy payload serialized, migrated and re-serialized twice
        # must produce identical bytes and identical task semantics.
        huey = self._huey()

        @huey.task(name='add')
        def add(a, b, scale=1):
            return (a + b) * scale

        name = huey._registry.task_to_string(add.task_class)
        huey.message_schema.add_migration(
            name, 1, 2, lambda a, k: (a, dict(k, scale=10)))

        legacy = Serializer().serialize(self.plain(name, (1, 2)))
        task1 = huey.deserialize_task(legacy)
        task2 = huey.deserialize_task(legacy)
        self.assertEqual(task1.args, task2.args)
        self.assertEqual(task1.kwargs, task2.kwargs)
        self.assertEqual(task1.id, task2.id)
        self.assertEqual(
            huey.serialize_task(task1), huey.serialize_task(task2))
        self.assertEqual(task1.execute(), 30)
        self.assertEqual(task2.execute(), 30)

    def test_enqueue_after_upgrade_writes_current_version(self):
        huey = self._huey()

        @huey.task(name='add')
        def add(a, b):
            return a + b

        # Producing after the upgrade stamps the current version, including
        # for the nested on_complete message.
        task = add.s(1, 2)
        task.then(add.s(3, 4))
        raw = huey.serialize_task(task)
        message = Serializer().deserialize(raw)
        self.assertIsInstance(message, VersionedMessage)
        self.assertEqual(message.schema_version, 2)
        self.assertIsInstance(message.message.on_complete, VersionedMessage)
        self.assertEqual(message.message.on_complete.schema_version, 2)

        # And it round-trips back into an executable task.
        restored = huey.deserialize_task(raw)
        self.assertEqual(restored.args, (1, 2))
        self.assertEqual(restored.on_complete.args, (3, 4))
        self.assertEqual(restored.on_complete.execute(), 7)
