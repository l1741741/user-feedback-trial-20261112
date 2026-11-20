"""Shared task module for multi-process evidence.

Imported (not defined in __main__) so the task registry name is identical in
the spawned child processes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from huey import SqliteHuey  # noqa: E402

huey = SqliteHuey(name='mpq', filename=os.devnull, create_tables=False)


@huey.task()
def mp_task(n):
    return n
