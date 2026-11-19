"""Task definitions shared by the multi-process batch tests.

Both the publisher and the consumer register the same task classes under the
same ``module.name`` keys, which is what the registry requires.
"""
from huey import SqliteHuey


def build(filename):
    huey = SqliteHuey(filename=filename, utc=False)

    @huey.task()
    def add(a, b):
        return a + b

    @huey.task()
    def total(values):
        return sum(values)

    return huey, add, total
