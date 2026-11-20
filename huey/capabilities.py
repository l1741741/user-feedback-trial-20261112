"""
Capability-based routing helpers.

A task may declare a set of *requirements* (the capabilities a consumer must
provide in order to run it). A consumer advertises its own *capabilities*. A
consumer is eligible for a task when every requirement of the task is among
the consumer's capabilities.

A task without requirements (``None``) is eligible for every consumer, which
keeps tasks enqueued by older releases working unchanged after upgrade.

The helpers in this module are deliberately backend-agnostic: the in-memory
storage and the SQL storages share the same matching semantics.
"""
import json


def normalize_requirements(requirements):
    """Normalize a task's capability requirements into a sorted tuple.

    Accepts a single string, any iterable of strings, or ``None``. Returns
    ``None`` for an empty/``None`` value so that unlabeled tasks stay
    unlabeled rather than being stored as an empty list.
    """
    if requirements is None:
        return None
    if isinstance(requirements, str):
        requirements = (requirements,)
    normalized = tuple(sorted(set(requirements)))
    return normalized or None


def normalize_capabilities(capabilities):
    """Normalize a consumer's capabilities into a frozenset.

    ``None`` means the consumer declares no special capability and may only
    process unlabeled tasks.
    """
    if capabilities is None:
        return frozenset()
    if isinstance(capabilities, str):
        capabilities = (capabilities,)
    return frozenset(capabilities)


def can_handle(capabilities, requirements):
    """Return whether a consumer with the given capabilities may take a task
    with the given (normalized) requirements.

    Unlabeled tasks (``requirements is None``) are always eligible.
    """
    if requirements is None:
        return True
    if capabilities is None:
        return False
    return frozenset(requirements).issubset(frozenset(capabilities))


def encode_requirements(requirements):
    """Encode normalized requirements for storage in a text column.

    Returns ``None`` for unlabeled tasks (stored as SQL NULL) so that rows
    written by older releases match the same representation.
    """
    if requirements is None:
        return None
    return json.dumps(tuple(requirements), separators=(',', ':'))


def decode_requirements(value):
    """Decode a requirements value read back from storage."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('utf-8')
    decoded = json.loads(value)
    if not decoded:
        return None
    return tuple(decoded)


def resolve_chain_requirements(task):
    """Materialize the effective requirements of a task and every task in
    its ``on_complete`` and ``on_error`` chains.

    Each task's requirements are taken from, in order of precedence:

    1. An explicit per-instance ``requirements`` value given at enqueue time
       (``task.requirements`` is not the sentinel ``UNSPECIFIED``).
    2. The task class default declared on the decorator
       (``default_requirements``).
    3. The requirements resolved for the preceding task in the same chain --
       but only when this task itself has no explicit value and no class
       default, so a predecessor can never overwrite a successor's explicit
       choice.
    4. ``None``, i.e. an unlabeled task any consumer may run.

    The walk is repeated for each chain root, so a task reused in several
    chains is resolved independently per chain. The resolved values are
    written back onto the task instances; afterwards the ordinary
    serialize/schedule/retry paths carry the requirements without any
    special-casing.
    """
    def walk(root, inherited=None):
        current = root
        while current is not None:
            explicit = getattr(current, 'requirements', UNSPECIFIED)
            if explicit is not UNSPECIFIED:
                effective = normalize_requirements(explicit)
            else:
                class_default = getattr(current, 'default_requirements', None)
                if class_default is not None:
                    effective = normalize_requirements(class_default)
                else:
                    effective = inherited
            current.requirements = effective
            inherited = effective
            current = current.on_complete

    walk(task)
    if task.on_error is not None:
        # The error chain inherits from the task that owns the on_error
        # handler, but each handler's own explicit choice still wins.
        walk(task.on_error, inherited=task.requirements)
    return task


class _Unspecified(object):
    """Sentinel distinguishing "no per-instance requirements argument" from
    an explicit ``None`` (which itself normalizes to an unlabeled task)."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(_Unspecified, cls).__new__(cls)
        return cls._instance

    def __repr__(self):
        return 'UNSPECIFIED'

    def __bool__(self):
        return False


UNSPECIFIED = _Unspecified()
