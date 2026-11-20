"""
Optional versioned message contract for rolling deployments.

Huey already tolerates messages serialized by older versions of a task class,
because :class:`huey.registry.Message` defaults missing fields to ``None``.
That only works while the *shape* of a message does not change. This module
adds an explicit, opt-in contract on top of that behavior:

* each message (including nested ``on_complete`` / ``on_error`` / chord
  callback messages) can carry a ``schema_version``;
* readers can map old task names to new ones with explicit aliases;
* readers can convert old arguments with caller-registered migrations;
* readers reject unknown versions instead of guessing.

Nothing here is enabled unless a :class:`MessageSchema` is passed to the
``Huey`` constructor, so existing serializers and task registries keep working
unchanged by default.
"""
import copy

from huey.exceptions import ConfigurationError
from huey.exceptions import MessageMigrationError
from huey.exceptions import MessageVersionError
from huey.exceptions import TaskAliasError


def _is_version(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_name(value, what):
    if not isinstance(value, str) or not value:
        raise ConfigurationError('%s must be a non-empty string, got %r.' % (
            what, value))
    # Task names are always "module.Class". Rejecting bare names makes it
    # impossible to match tasks by guessing their short function name.
    if '.' not in value:
        raise ConfigurationError(
            '%s must be a fully-qualified "module.Class" name, got %r. '
            'Task names are never matched on their short name.' % (
                what, value))


class MessageSchema(object):
    """
    Declare the current message version, old task-name aliases and the
    migrations that convert old task arguments to their current form.

    :param int version: the version that this process writes and can read
        without conversion. Must be >= 1.
    :param int legacy_version: the version assumed for messages that carry no
        ``schema_version`` (i.e. messages written before versioning was
        enabled, or by a producer that predates this version bump). Defaults
        to ``version``, which means "an unversioned message already matches
        the current contract" - this keeps messages that were in the queue
        before an upgrade readable without any migration. Set it to an older
        version when the payload shape changed, and register a migration for
        every task whose arguments must be converted.
    :param dict aliases: mapping of ``old.name`` to ``new.name``. Aliases are
        matched exactly against the full task name; short names are never
        used. Alias chains are followed, but cycles and conflicts are
        rejected.
    :param dict migrations: mapping of canonical task name to a list of
        ``(from_version, to_version, function)`` tuples. ``function`` is
        called as ``function(args, kwargs)`` and must return ``(args, kwargs)``.

    Example::

        schema = MessageSchema(
            version=2,
            aliases={
                'myapp.tasks.send_report_v1': 'myapp.tasks.send_report',
            },
            migrations={
                'myapp.tasks.send_report': [
                    (1, 2, migrate_report_args),
                ],
            })

        huey = RedisHuey('my-app', message_schema=schema)
    """
    def __init__(self, version=1, legacy_version=None, aliases=None,
                 migrations=None):
        if not _is_version(version) or version < 1:
            raise ConfigurationError(
                'MessageSchema version must be an integer >= 1, got %r.' % (
                    version,))
        if legacy_version is None:
            legacy_version = version
        if not _is_version(legacy_version) or legacy_version < 1:
            raise ConfigurationError(
                'MessageSchema legacy_version must be an integer >= 1, '
                'got %r.' % (legacy_version,))
        if legacy_version > version:
            raise ConfigurationError(
                'MessageSchema legacy_version (%s) cannot be greater than '
                'the current version (%s).' % (legacy_version, version))

        self.version = version
        self.legacy_version = legacy_version
        self._aliases = {}
        self._migrations = {}
        # Bumped on every mutation, so callers can cache validation results.
        self.revision = 0

        for old_name, new_name in (aliases or {}).items():
            self.add_alias(old_name, new_name)
        for task_name, steps in (migrations or {}).items():
            for step in steps:
                self.add_migration(task_name, *step)
        self.validate()

    # -- Registration ------------------------------------------------------

    def add_alias(self, old_name, new_name):
        """
        Register an explicit alias from an old task name to a new task name.

        Re-registering the same pair is a no-op. Re-registering the same old
        name with a different target raises :py:class:`TaskAliasError`.
        """
        _check_name(old_name, 'Alias source')
        _check_name(new_name, 'Alias target')
        if old_name == new_name:
            raise TaskAliasError(
                'Alias maps %r to itself, which is a cycle.' % (old_name,),
                [old_name])

        existing = self._aliases.get(old_name)
        if existing is not None and existing != new_name:
            raise TaskAliasError(
                'Conflicting aliases for %r: %r and %r.' % (
                    old_name, existing, new_name), [old_name])
        if old_name in self._migrations:
            raise ConfigurationError(
                'Task %r is an alias source and also has argument migrations. '
                'Migrations must be registered under the canonical name.' % (
                    old_name,))

        previous = self._aliases.get(old_name)
        self._aliases[old_name] = new_name
        self.revision += 1
        try:
            self._check_alias_cycles()
        except TaskAliasError:
            # Do not leave a rejected alias in the graph: a caller that
            # catches the error must not end up with a cyclic schema.
            if previous is None:
                del self._aliases[old_name]
            else:
                self._aliases[old_name] = previous
            self.revision += 1
            raise

    def add_migration(self, task_name, from_version, to_version, func):
        """
        Register a migration step for the given canonical task name.

        ``func(args, kwargs)`` must return ``(args, kwargs)``. It receives
        deep copies, so it may modify them in place. Only registered
        migrations are ever run.

        Re-registering the identical step is a no-op. A conflicting step
        (same task and source version, different target or function) raises
        :py:class:`ConfigurationError`.
        """
        _check_name(task_name, 'Migration task name')
        if not _is_version(from_version) or not _is_version(to_version):
            raise ConfigurationError(
                'Migration versions must be integers, got %r -> %r.' % (
                    from_version, to_version))
        if to_version <= from_version:
            raise ConfigurationError(
                'Migration for %r must move forward in versions, got %s -> '
                '%s.' % (task_name, from_version, to_version))
        if to_version > self.version:
            raise ConfigurationError(
                'Migration for %r targets version %s, which is newer than '
                'the current version %s.' % (
                    task_name, to_version, self.version))
        if not callable(func):
            raise ConfigurationError(
                'Migration for %r must be callable.' % (task_name,))
        if task_name in self._aliases:
            raise ConfigurationError(
                'Task %r is an alias source and cannot define migrations. '
                'Register migrations under the canonical name.' % (
                    task_name,))

        key = (task_name, from_version)
        existing = self._migrations.get(key)
        if existing is not None:
            if existing[0] == to_version and existing[1] is func:
                return
            raise ConfigurationError(
                'Conflicting migrations for %r from version %s: %r -> %s and '
                '%r -> %s.' % (task_name, from_version, existing[1],
                               existing[0], func, to_version))

        self._migrations[key] = (to_version, func)
        self.revision += 1
        # Chains are validated as a whole in validate(); validating each step
        # in isolation would reject a legitimate multi-step chain before its
        # later steps have been registered.

    # -- Validation --------------------------------------------------------

    def _check_alias_cycles(self):
        for start in self._aliases:
            seen = []
            current = start
            while current in self._aliases:
                if current in seen:
                    cycle = seen[seen.index(current):]
                    raise TaskAliasError(
                        'Task-name alias cycle detected: %s' % (
                            ' -> '.join(cycle + [current]),), cycle)
                seen.append(current)
                current = self._aliases[current]

    def _check_migration_chains(self):
        for (task_name, from_version) in sorted(self._migrations):
            seen = set()
            current = from_version
            while current != self.version:
                if current in seen:
                    raise ConfigurationError(
                        'Migration cycle detected for %r at version %s.' % (
                            task_name, current))
                seen.add(current)
                step = self._migrations.get((task_name, current))
                if step is None:
                    raise ConfigurationError(
                        'Migration chain for %r is incomplete: version '
                        '%s has no migration to the current version %s.' %
                        (task_name, current, self.version))
                current = step[0]

    def validate(self):
        """
        Validate the alias graph and migration chains. Raises
        :py:class:`TaskAliasError` or :py:class:`ConfigurationError` on
        conflicts, cycles and incomplete migration chains.
        """
        self._check_alias_cycles()
        self._check_migration_chains()
        return True

    def validate_registry_object(self, registry):
        """
        Check that a registry implementation can participate in the versioned
        message contract. Called when the Huey instance is created, so an
        incompatible custom registry is rejected at configuration time.
        """
        registered = getattr(registry, '_registry', None)
        if not isinstance(registered, dict):
            raise ConfigurationError(
                'MessageSchema requires a Huey Registry object with a '
                '"_registry" mapping of task names to task classes, got %r.' % (
                    registry,))
        for method in ('create_message', 'create_task', 'resolve_task_name'):
            if not callable(getattr(registry, method, None)):
                raise ConfigurationError(
                    'MessageSchema requires the registry to implement %r.' % (
                        method,))
        return True

    def validate_registry(self, registry):
        """
        Validate aliases and migrations against the tasks that are actually
        registered. Called before a consumer starts, so configuration errors
        surface before any task is executed.
        """
        self.validate()
        self.validate_registry_object(registry)

        registered = registry._registry

        for old_name, new_name in self._aliases.items():
            if old_name in registered:
                raise TaskAliasError(
                    'Alias source %r is also a registered task. Registering '
                    'and aliasing the same name is ambiguous.' % (old_name,),
                    [old_name])
            canonical = self.resolve_name(new_name)
            if canonical not in registered:
                raise TaskAliasError(
                    'Alias %r -> %r resolves to %r, which is not a registered '
                    'task.' % (old_name, new_name, canonical),
                    [old_name, new_name])

        for (task_name, from_version) in self._migrations:
            if task_name not in registered:
                raise ConfigurationError(
                    'Migration defined for task %r, which is not registered.' %
                    (task_name,))

        return True

    # -- Lookups -----------------------------------------------------------

    @property
    def aliases(self):
        return dict(self._aliases)

    @property
    def migrations(self):
        return dict(self._migrations)

    def resolve_name(self, name):
        """
        Return the canonical task name, following explicit aliases.
        """
        seen = []
        current = name
        while current in self._aliases:
            if current in seen:
                cycle = seen[seen.index(current):]
                raise TaskAliasError(
                    'Task-name alias cycle detected: %s' % (
                        ' -> '.join(cycle + [current]),), cycle)
            seen.append(current)
            current = self._aliases[current]
        return current

    def supported_versions(self, task_name=None):
        """Return the set of message versions this schema can read."""
        versions = {self.version}
        for (name, from_version) in self._migrations:
            if task_name is not None and name != task_name:
                continue
            current = from_version
            seen = set()
            while current != self.version and current not in seen:
                seen.add(current)
                versions.add(current)
                step = self._migrations.get((name, current))
                if step is None:
                    break
                current = step[0]
        return versions

    def check_version(self, version, task_name=None, task_id=None, path=()):
        """
        Normalize and validate a message version before doing any other work.

        Returns the effective version. Raises :py:class:`MessageVersionError`
        for future or malformed versions. The error never contains task
        arguments.
        """
        if version is None:
            version = self.legacy_version
        elif not _is_version(version):
            raise MessageVersionError(
                version, task_name, task_id, path,
                self.supported_versions(task_name),
                reason='invalid message version')
        elif version < 1:
            raise MessageVersionError(
                version, task_name, task_id, path,
                self.supported_versions(task_name),
                reason='invalid message version')
        elif version > self.version:
            raise MessageVersionError(
                version, task_name, task_id, path,
                self.supported_versions(task_name),
                reason='unsupported future message version')
        return version

    def migrate(self, task_name, version, args, kwargs, task_id=None, path=()):
        """
        Convert ``args`` / ``kwargs`` for the given canonical task name from
        ``version`` to the current version using registered migrations.

        The input payload is never modified: migration functions receive deep
        copies. Running this twice on the same message therefore produces the
        same result. Raises :py:class:`MessageMigrationError` if a registered
        migration fails or returns an unusable value, and
        :py:class:`MessageVersionError` if no migration path exists.
        """
        version = self.check_version(version, task_name, task_id, path)
        if version == self.version:
            return args, kwargs

        current = version
        seen = set()
        # Copy before the first conversion so shared message payloads are not
        # polluted, even if a migration mutates its argument in place.
        args = copy.deepcopy(args)
        kwargs = copy.deepcopy(kwargs)
        while current != self.version:
            if current in seen:
                raise MessageMigrationError(
                    task_name, task_id, version, self.version, path,
                    'migration cycle detected')
            seen.add(current)

            step = self._migrations.get((task_name, current))
            if step is None:
                raise MessageVersionError(
                    current, task_name, task_id, path,
                    self.supported_versions(task_name),
                    reason='no migration path from message version')
            to_version, func = step

            try:
                result = func(args, kwargs)
            except Exception as exc:
                # Do not continue with a partially converted task. The cause
                # is chained for debugging; the error text stays payload-free.
                raise MessageMigrationError(
                    task_name, task_id, current, to_version, path,
                    'migration function raised %s' % type(exc).__name__) \
                    from exc

            if not isinstance(result, (tuple, list)) or len(result) != 2:
                raise MessageMigrationError(
                    task_name, task_id, current, to_version, path,
                    'migration must return a 2-tuple (args, kwargs), got %s' %
                    type(result).__name__)
            args, kwargs = result
            if isinstance(args, list):
                args = tuple(args)
            elif not isinstance(args, tuple):
                raise MessageMigrationError(
                    task_name, task_id, current, to_version, path,
                    'migration returned args of type %s, expected tuple' %
                    type(args).__name__)
            if kwargs is None:
                kwargs = {}
            elif not isinstance(kwargs, dict):
                raise MessageMigrationError(
                    task_name, task_id, current, to_version, path,
                    'migration returned kwargs of type %s, expected dict' %
                    type(kwargs).__name__)
            current = to_version

        return args, kwargs


def ensure_schema_object(message_schema):
    """
    Validate the value passed as ``Huey(message_schema=...)``.
    """
    if message_schema is None:
        return None
    if not isinstance(message_schema, MessageSchema):
        raise ConfigurationError(
            'message_schema must be a MessageSchema instance, got %r.' % (
                message_schema,))
    return message_schema