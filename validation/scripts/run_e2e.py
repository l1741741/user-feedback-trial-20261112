"""End-to-end offline validation of keyspace isolation.

Covers, all through public APIs only:

* the identical literal used as business key, result id, single-task and
  whole-class revoke id, lock name and rate-limit name on both MemoryHuey
  and SqliteHuey;
* two queues sharing one SQLite file;
* a database containing legacy (unmarked) values and both legacy policies;
* multi-process restarts (delegated to run_multiprocess.py);
* rejection of storage backends that do not support isolation;
* a default-mode regression check proving the legacy shared layout is
  unchanged.

Run with the repository's interpreter:  python -I validation/scripts/run_e2e.py
"""
import os
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from huey import MemoryHuey  # noqa: E402
from huey import SqliteHuey  # noqa: E402
from huey.api import Huey  # noqa: E402
from huey.api import Task  # noqa: E402
from huey.api import chord  # noqa: E402
from huey.api import group  # noqa: E402
from huey.constants import EmptyData  # noqa: E402
from huey.exceptions import ConfigurationError  # noqa: E402
from huey.exceptions import RateLimitExceeded  # noqa: E402
from huey.exceptions import TaskLockedException  # noqa: E402
from huey.storage import BlackHoleStorage  # noqa: E402

KEY = 'order-42'  # A deliberate, valid-looking colliding business id.


def check(cond, label):
    if not cond:
        raise AssertionError('FAILED: %s' % label)
    print('  ok - %s' % label)


def validate_collision_contract(huey_factory, label):
    print('collision contract (%s)' % label)
    h = huey_factory()

    h.put(KEY, 'business-payload')
    h.put_result(KEY, 'result-payload')
    h.revoke_by_id(KEY)
    h.lock_task(KEY).acquire()
    rl = h.rate_limit(KEY, limit=1, per=60)
    rl.acquire()

    @h.task()
    def order_task():
        return True
    h.revoke_all(order_task)

    check(h.get(KEY, peek=True) == 'business-payload', 'business read')
    check(h.result(KEY, preserve=True) == 'result-payload', 'result read')
    check(h.is_revoked(Task(id=KEY)), 'single-task revoke active')
    check(order_task.is_revoked(), 'whole-class revoke active')
    check(h.is_locked(KEY), 'lock active')
    try:
        rl.acquire()
    except RateLimitExceeded:
        check(True, 'rate-limit window enforced')
    else:
        raise AssertionError('rate limit not enforced')
    # The rejected acquire still increments the counter; remember the value so
    # we can prove the window (counter + window marker) survives a result
    # flush rather than asserting a hard-coded number.
    usage = rl.current_usage()

    # Business and result access cannot read or delete control-state.
    check(h.delete(KEY), 'business delete returns True')
    check(h.is_locked(KEY), 'lock survives business delete')
    check(h.is_revoked(Task(id=KEY)), 'revoke survives business delete')
    # The result plane is untouched by the business delete.
    check(h.result(KEY, preserve=True) == 'result-payload',
          'result plane independent of business')
    check(h.get(KEY) is None, 'business value was actually removed')
    h.delete_result(KEY)

    # Re-create a result and prove result flush leaves control intact.
    h.put_result(KEY, 'again')
    h.storage.flush_results()
    check(h.is_locked(KEY), 'lock survives flush_results')
    check(h.is_revoked(Task(id=KEY)), 'revoke survives flush_results')
    check(rl.current_usage() == usage,
          'rate-limit window survives flush_results')
    check(h.result(KEY) is None, 'result actually flushed')
    check(h.storage.control_store_size() >= 3, 'control store introspection')

    # Control flush leaves business and result planes alone.
    h.put(KEY, 'biz2')
    h.put_result(KEY, 'res2')
    h.storage.flush_control()
    check(not h.is_locked(KEY), 'lock cleared by flush_control')
    check(not h.is_revoked(Task(id=KEY)), 'revoke cleared by flush_control')
    check(h.get(KEY, peek=True) == 'biz2', 'business survives flush_control')
    check(h.result(KEY, preserve=True) == 'res2',
          'result survives flush_control')
    h.storage.close()


def validate_public_workflows(huey_factory, label):
    print('public workflows (%s)' % label)
    h = huey_factory()

    @h.task()
    def add(a, b):
        return a + b

    # Chain via Result handles.
    r1, r2 = h.enqueue(add.s(1, 2).then(add.s(10)))
    h.execute(h.dequeue())
    h.execute(h.dequeue())
    check(r1.get() == 3 and r2.get() == 13, 'task chain + Result handles')

    # Group / ResultGroup.
    rg = h.enqueue(group([add.s(i, i) for i in range(3)]))
    while len(h):
        h.execute(h.dequeue())
    check(rg.get() == [0, 2, 4], 'group + ResultGroup')

    # Chord.
    @h.task()
    def total(values):
        return sum(values)
    cr = h.enqueue(chord([add.s(i, 1) for i in range(3)], total.s()))
    for _ in range(3):
        h.execute(h.dequeue())
    h.execute(h.dequeue())
    check(cr.get() == (1 + 2 + 3), 'chord callback result')

    # revoke/restore around real execution.
    @h.task()
    def work():
        return 'done'
    res = work()
    res.revoke()
    check(res.is_revoked(), 'result revoke visible')
    check(h.execute(h.dequeue()) is None, 'revoked task skipped')
    res2 = work()
    res2.restore()
    h.execute(h.dequeue())
    check(res2.get() == 'done', 'restored task executes')
    h.storage.close()


def validate_two_queues(db_path):
    print('two queues sharing one SQLite file')
    a = SqliteHuey('qa', filename=db_path, keyspace_isolation=True)
    b = SqliteHuey('qb', filename=db_path, keyspace_isolation=True)
    for h, tag in ((a, 'A'), (b, 'B')):
        h.put(KEY, 'biz-%s' % tag)
        h.put_result(KEY, 'res-%s' % tag)
        h.revoke_by_id(KEY)
        h.lock_task(KEY).acquire()
    check(a.get(KEY, peek=True) == 'biz-A' and
          b.get(KEY, peek=True) == 'biz-B', 'business planes separated')
    check(a.result(KEY, preserve=True) == 'res-A' and
          b.result(KEY, preserve=True) == 'res-B', 'result planes separated')
    a.storage.flush_results()
    check(a.result(KEY) is None and
          b.result(KEY, preserve=True) == 'res-B', 'flush scoped to one queue')
    check(b.is_revoked(Task(id=KEY)) and b.is_locked(KEY),
          'other queue control-state untouched')
    a.storage.close()
    b.storage.close()


def validate_legacy_database(db_path):
    print('legacy database policies')
    legacy = SqliteHuey('ql', filename=db_path)
    # Use distinct literals so every write survives in the legacy shared
    # keyspace (identical literals would overwrite each other - exactly the
    # collision isolation is designed to remove).
    legacy.put('legacy-biz', 'old-business')
    legacy.put_result('legacy-task', 'old-result')
    legacy.revoke_by_id('legacy-task')
    legacy.storage.close()

    # Default policy: refuse, preserve everything.
    try:
        SqliteHuey('ql', filename=db_path, keyspace_isolation=True)
    except ConfigurationError:
        check(True, 'error policy refuses ambiguous legacy rows')
    else:
        raise AssertionError('expected ConfigurationError')

    import sqlite3
    conn = sqlite3.connect(db_path)
    check(conn.execute('select count(*) from kv').fetchone()[0] == 3,
          'legacy rows preserved after refusal')
    conn.close()

    # Quarantine policy: archive, never reinterpret, then run cleanly.
    h = SqliteHuey('ql', filename=db_path, keyspace_isolation=True,
                   legacy_policy='quarantine')
    check(h.storage.quarantine_info() == 3, 'rows quarantined verbatim')
    check(h.storage.result_store_size() == 0 and
          h.storage.control_store_size() == 0 and
          h.storage.business_store_size() == 0, 'isolated store starts empty')
    h.put(KEY, 'new-business')
    check(h.get(KEY, peek=True) == 'new-business', 'new writes work after '
          'quarantine')
    h.storage.close()


def validate_unsupported_backend():
    print('unsupported storage rejection')
    try:
        Huey('x', storage_class=BlackHoleStorage, keyspace_isolation=True)
    except ConfigurationError:
        check(True, 'BlackHole storage rejected before any write')
    else:
        raise AssertionError('expected ConfigurationError')

    class LegacyStorage(BlackHoleStorage):
        def __init__(self, name):
            super(LegacyStorage, self).__init__(name)
    h = Huey('x', storage_class=LegacyStorage)  # default mode still works
    check(not h.keyspace_isolation, 'legacy custom storage works by default')


def validate_default_mode_regression(db_path):
    print('default-mode regression (shared keyspace preserved)')
    h = SqliteHuey('qd', filename=db_path)
    check(not h.storage.keyspace_isolation, 'isolation defaults to off')
    h.put(KEY, 'first')
    h.put_result(KEY, 'second')  # Same physical key in legacy layout.
    check(h.result(KEY, preserve=True) == 'second',
          'legacy overwrite behavior unchanged')
    # Lock and revoke still share the store as they always did.
    h.revoke_by_id(KEY)
    check(h.is_revoked(Task(id=KEY)), 'legacy revoke works')
    h.storage.close()

    mem = MemoryHuey('qm')
    check(not mem.storage.keyspace_isolation, 'memory isolation defaults off')


def main():
    tmp = tempfile.mkdtemp(prefix='huey-e2e-')
    validate_collision_contract(
        lambda: MemoryHuey('m', utc=False, keyspace_isolation=True), 'memory')
    validate_collision_contract(
        lambda: SqliteHuey('s', filename=os.path.join(tmp, 'iso.db'),
                          utc=False, keyspace_isolation=True), 'sqlite')
    validate_public_workflows(
        lambda: MemoryHuey('mw', utc=False, keyspace_isolation=True), 'memory')
    validate_public_workflows(
        lambda: SqliteHuey('sw', filename=os.path.join(tmp, 'wf.db'),
                          utc=False, keyspace_isolation=True), 'sqlite')
    validate_two_queues(os.path.join(tmp, 'shared.db'))
    validate_legacy_database(os.path.join(tmp, 'legacy.db'))
    validate_unsupported_backend()
    validate_default_mode_regression(os.path.join(tmp, 'default.db'))

    driver = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'run_multiprocess.py')
    proc = subprocess.run([sys.executable, '-I', driver],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise AssertionError('multi-process validation failed')
    print(proc.stdout.strip())
    print('\nALL E2E VALIDATION PASSED')


if __name__ == '__main__':
    main()
