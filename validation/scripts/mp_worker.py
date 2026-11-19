"""Multi-process persistence check for keyspace isolation (SqliteHuey).

This script is intentionally invoked as a fresh subprocess so that every
phase runs against a newly-created Huey instance and a freshly-opened SQLite
connection - it proves isolation state survives a real process restart, not
just an in-process storage object.

Usage:
    python -I mp_worker.py <db_path> <phase>
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from huey import SqliteHuey  # noqa: E402
from huey.api import Task  # noqa: E402
from huey.constants import EmptyData  # noqa: E402

KEY = 'shared-id'


def make_huey(db_path, name='q1'):
    return SqliteHuey(name, filename=db_path, utc=False,
                      keyspace_isolation=True)


def phase_write(db_path):
    h = make_huey(db_path)
    h.put(KEY, 'business-data')
    h.put_result(KEY, 'result-data')
    h.revoke_by_id(KEY)
    h.lock_task(KEY).acquire()
    rl = h.rate_limit(KEY, 5, 60)
    rl.acquire()
    rl.acquire()
    # A class-level revocation with a deliberately colliding literal.
    @h.task()
    def collide_task():
        return True
    collide_task.revoke()
    h.storage.close()
    print('write-ok')


def phase_verify(db_path):
    # Fresh process, fresh connection: all control state must have survived.
    h = make_huey(db_path)
    assert h.get(KEY, peek=True) == 'business-data'
    assert h.result(KEY, preserve=True) == 'result-data'
    assert h.is_revoked(Task(id=KEY)), 'single-task revoke lost on restart'
    assert h.is_locked(KEY), 'lock lost on restart'
    rl = h.rate_limit(KEY, 5, 60)
    assert rl.current_usage() == 2, 'rate-limit window lost on restart'

    # A second queue on the same file keeps its own keyspaces.
    other = make_huey(db_path, name='q2')
    assert other.get(KEY) is None
    assert other.result(KEY) is None
    assert not other.is_revoked(Task(id=KEY))
    assert not other.is_locked(KEY)
    other.put(KEY, 'other-business')
    other.storage.close()

    # Flushing results must not resurrect/clear any control state.
    h.storage.flush_results()
    assert h.is_revoked(Task(id=KEY))
    assert h.is_locked(KEY)
    assert rl.current_usage() == 2
    assert h.get(KEY, peek=True) == 'business-data'
    h.storage.close()
    print('verify-ok')


def phase_mutate(db_path):
    # Another restart: restore/release still work on the persisted state.
    h = make_huey(db_path)
    assert h.restore_by_id(KEY) is True
    assert h.restore_by_id(KEY) is False
    assert not h.is_revoked(Task(id=KEY))
    lock = h.lock_task(KEY)
    assert lock.clear() is True
    assert not h.is_locked(KEY)
    h.rate_limit(KEY, 5, 60).reset()

    other = make_huey(db_path, name='q2')
    assert other.get(KEY, peek=True) == 'other-business'
    other.storage.close()
    h.storage.close()
    print('mutate-ok')


def main():
    db_path, phase = sys.argv[1], sys.argv[2]
    {
        'write': phase_write,
        'verify': phase_verify,
        'mutate': phase_mutate,
    }[phase](db_path)


if __name__ == '__main__':
    main()
