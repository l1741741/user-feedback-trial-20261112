"""
Task definitions shared between the legacy publisher process and the test
consumer.

Both processes import this module, so ``Registry.task_to_string`` produces the
same identifiers on both sides. Tasks are registered against the Huey instance
passed by the caller.
"""


def add(a, b):
    return a + b


def record(value):
    return value


def noop():
    return None


def register(huey):
    """Register the fixture tasks on ``huey`` and return their wrappers."""
    return (huey.task()(add),
            huey.task()(record),
            huey.task()(noop))
