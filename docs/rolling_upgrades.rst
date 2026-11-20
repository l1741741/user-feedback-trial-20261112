.. _rolling-upgrades:

Versioned messages and rolling upgrades
=======================================

Huey's task queue is usually upgraded in the middle of traffic. A web process
starts writing tasks with new code while consumers are still running the old
release, and a consumer may be restarted while tasks written by the previous
release are still in the queue. Task functions also move between modules as
code is reorganized.

By default this is handled the same way it always has been: the message is a
pickled tuple whose missing trailing fields default to ``None``, so adding a
field to the message is backwards compatible, and a renamed task raises
``HueyException`` in the consumer.

For deployments where the *payload* also changes - a task is renamed, its
arguments change shape, or a new release must refuse messages it cannot
interpret - Huey offers an **optional** versioned message contract.

None of this is enabled by default. Existing :py:class:`Serializer` and
:py:class:`Registry` usage continues to work unchanged unless a
``message_schema`` is passed to :py:class:`Huey`.

Enabling the contract
---------------------

.. code-block:: python

    from huey import RedisHuey
    from huey.schema import MessageSchema

    schema = MessageSchema(version=2)
    huey = RedisHuey('my-app', message_schema=schema)

``version`` is the version this process writes and, without conversion, reads.
Version ``1`` is the message format that predates versioning. Messages that
carry no version are assumed to be at ``legacy_version``, which defaults to
``version`` - so simply passing ``MessageSchema(version=1)`` changes nothing
on the wire.

The schema must be shared by every producer and consumer that talks to the
same queue.

Task renames
------------

A task is identified by its fully-qualified ``module.Class`` name. When a task
moves, add an explicit alias so old messages keep working:

.. code-block:: python

    schema = MessageSchema(
        version=2,
        aliases={
            'myapp.tasks.send_report_v1': 'myapp.reports.send_report',
        })

Rules for aliases:

* Both sides must be fully-qualified names. Aliases are matched **exactly**
  against the full name; the short function name is never used to guess a
  match. Two tasks that merely share a function name do not alias each other.
* Alias chains are followed (``a`` -> ``b`` -> ``c``).
* Registering two different targets for the same old name raises
  :py:class:`~huey.exceptions.TaskAliasError`.
* A cycle raises :py:class:`~huey.exceptions.TaskAliasError`.
* An alias source that is itself a registered task name is ambiguous and is
  rejected.
* Conflicts and cycles are reported when a task is registered and again before
  the consumer starts, so they surface **before** any queued message runs.

The alias target must resolve to a task that is registered on the consumer.

Argument conversion
-------------------

When the shape of a task's arguments changes, register a migration. Only
functions you register are ever run - there is no guessing based on the
argument names.

.. code-block:: python

    def migrate_report_args(args, kwargs):
        # Version 1 passed (account_id, start, end); version 2 passes a
        # single request dict.
        account_id, start, end = args
        return (({'account_id': account_id, 'start': start, 'end': end},),
                kwargs)

    schema = MessageSchema(
        version=2,
        legacy_version=1,
        aliases={'myapp.tasks.send_report': 'myapp.reports.send_report'},
        migrations={
            'myapp.reports.send_report': [
                (1, 2, migrate_report_args),
            ],
        })

A migration function is called as ``migration(args, kwargs)`` and **must**
return a ``(args, kwargs)`` tuple. It receives deep copies of both, so it may
modify them in place without touching the shared message object. Running the
same old message through the conversion twice produces the same result.

Constraints enforced at configuration time:

* Every migration moves strictly forward in versions, and never past the
  current ``version``.
* Chains may span several versions (``1 -> 2`` and ``2 -> 3``), but each task
  must have a complete path from every older version it accepts up to the
  current version.
* A migration for a task that is not registered is rejected before the
  consumer starts.
* Migrations are registered under the *canonical* (new) name, not an alias
  source.

If a migration raises, or returns something that is not a ``(tuple, dict)``
pair, the message is rejected with
:py:class:`~huey.exceptions.MessageMigrationError` and **no task is
constructed**, so a half-converted task is never executed. The original
exception is chained as ``__cause__`` for debugging, but the error text never
includes the argument values.

Reading old messages
--------------------

When ``legacy_version`` is lower than ``version``, every task that still needs
conversion must have a migration. A consumer that encounters an old message it
has no migration path for raises :py:class:`~huey.exceptions.MessageVersionError`
rather than running the task against the new signature.

When ``legacy_version`` equals ``version`` (the default), unversioned messages
are treated as already current and are read directly.

Refusing newer messages
-----------------------

A producer that writes a version higher than the consumer's ``version`` is
ahead of the consumer. A consumer refuses such a message instead of running it
with default values. The error is locatable without exposing the payload:

.. code-block:: text

    unsupported future message version: version=99 task=myapp.tasks.report
    id=5f0c... path=on_complete supported=2

The task name, task id, version and nested path are included. Task arguments
and keyword arguments are never included, so rejecting a message cannot leak
payload values into your logs.

Consumers that predate versioning do not know the versioned wrapper at all and
fail to unpickle it; they therefore also refuse the message rather than
silently running it. This is why a version bump is only stamped on messages
once ``version`` advances past ``1``: an un-upgraded consumer keeps working
with messages written before the bump.

Pipelines, chords and error handlers
------------------------------------

Aliases and migrations apply to every message in a pipeline, not just the
top-level task:

* ``on_complete`` (``task.then(...)``) successors,
* ``on_error`` (``task.error(...)``) handlers,
* chord member callbacks (``group(...).then(...)``).

Each nested message carries its own version, and the original task id, retry
count, retry delay, priority, ETA and timeout are preserved exactly. When a
nested message is refused, the error's ``path`` tells you where it was found:
``on_complete``, ``on_error``, ``chord_config.callback``.

A representative deployment
---------------------------

#. **Before the upgrade.** Producers and consumers run the old release and
   write version-1 messages.

#. **Deploy producers first** with ``version=2`` and the alias and migration
   registrations. They keep reading version-1 messages from the queue (if
   ``legacy_version=1`` and the migrations cover them) and start writing
   version-2 messages.

#. **Deploy consumers.** Old consumers that are still running see version-2
   messages and refuse them - they are not executed with the new signature
   against the old code. New consumers read both version 1 and version 2.

#. **After every consumer is upgraded,** the alias and old migration entries
   can be removed in a later release, once the queue contains no messages that
   need them.

Because a rejected message is removed from the queue by the consumer that
refuses it, drain in-flight messages before removing migrations, or run
consumers in a controlled order as above.

Custom serializers and registries
---------------------------------

The default pickle serializer and :py:class:`Registry` support the contract
without any changes. If you pass your own serializer, Huey verifies it can
round-trip a versioned message when ``message_schema`` is enabled, and raises
:py:class:`~huey.exceptions.ConfigurationError` at construction time if it
cannot. A custom serializer may declare ``supports_message_schema = False`` to
opt out explicitly and receive the same early error.

Custom registries are rejected when a schema is enabled unless they expose a
``_registry`` mapping and implement ``create_message``, ``create_task`` and
``resolve_task_name`` - a partial registry would otherwise fail mid-consumption.

API reference
-------------

.. py:class:: huey.schema.MessageSchema(version=1, legacy_version=None, aliases=None, migrations=None)

    :param int version: message version written and read without conversion.
    :param int legacy_version: version assumed for messages without a version.
        Defaults to ``version``.
    :param dict aliases: mapping of old fully-qualified task name to new name.
    :param dict migrations: mapping of canonical task name to a list of
        ``(from_version, to_version, function)`` tuples.

    .. py:method:: add_alias(old_name, new_name)

    .. py:method:: add_migration(task_name, from_version, to_version, func)

    .. py:method:: resolve_name(name)

        Follow aliases to the canonical name.

    .. py:method:: migrate(task_name, version, args, kwargs, task_id=None, path=())

        Convert arguments to the current version using registered migrations
        only. Never mutates its inputs.

    .. py:method:: validate()

        Raise on alias conflicts/cycles or incomplete migration chains.

    .. py:method:: validate_registry(registry)

        Validate the schema against the tasks the consumer actually
        registered. Called automatically before consuming begins.
