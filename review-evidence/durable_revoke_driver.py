"""
Offline reproduction driver for the durable ("persistent") revocation feature.

This module is importable (its registered tasks keep a stable name across
processes) and is driven from the command line by
``test_durable_revoke.py``. Every mode runs in its own short-lived process, so
a "restart" is an actual process exit and two contenders are genuinely
independent OS processes sharing one SQLite file.

The database path is read from $REVOKE_DB. Run with ``python -I`` and a
PYTHONPATH pointing at the repository root.
"""
import atexit
import os
import sys
import tempfile
import threading
import time

from huey import SqliteHuey


def _task_a(n):
    return n + 1


def _failing(n):
    raise ValueError('boom-%s' % n)


# Tasks must be registered under a stable, importable module name. The huey
# instance only exists to build the registry entries; the database used by a
# mode is selected per process via REVOKE_DB. Task wrappers are bound to this
# registry instance, but because task classes are resolved through the shared
# registry, a task built here can be enqueued on any Huey instance that reuses
# the registry.
_reg_fd, _reg_path = tempfile.mkstemp(prefix='revoke-registry-', suffix='.db')
os.close(_reg_fd)
atexit.register(lambda: os.path.exists(_reg_path) and os.unlink(_reg_path))
_REGISTRY_HUEY = SqliteHuey('revoke-driver-registry', filename=_reg_path,
                            durable_revoke=True, utc=False)


def _register(func, name, **settings):
    wrapper = _REGISTRY_HUEY.task(name=name, **settings)(func)
    task_class = wrapper.task_class
    stable_key = 'huey.tests.durable_revoke_driver.%s' % task_class.__name__
    registry = _REGISTRY_HUEY._registry
    old_key = registry.task_to_string(task_class)
    if old_key != stable_key:
        task_class.__module__ = 'huey.tests.durable_revoke_driver'
        registry._registry.pop(old_key, None)
        registry._registry[stable_key] = task_class
    return wrapper


task_a = _register(_task_a, 'task_a')
failing = _register(_failing, 'failing', retries=2, retry_delay=30)


def get_huey():
    huey = SqliteHuey('revoke-driver', filename=os.environ['REVOKE_DB'],
                      durable_revoke=True, utc=False)
    # Reuse the registry built at import so tasks serialized by one process
    # are resolvable in any sibling process, even though each process opens
    # its own database connection.
    huey._registry = _REGISTRY_HUEY._registry
    return huey


def _say(message):
    # These processes are harvested over pipes and some modes exit hard, so
    # always flush explicitly.
    sys.stdout.write(message + '\n')
    sys.stdout.flush()


def setup(n=3):
    """Enqueue N tasks, printing their ids."""
    huey = get_huey()
    ids = [huey.enqueue(task_a.s(i)).id for i in range(int(n))]
    _say('count=%d ids=%s' % (len(ids), ','.join(ids)))


def setup_failing(n=1):
    """Enqueue N failing tasks (retries=2, retry_delay=30) and print ids."""
    huey = get_huey()
    ids = [huey.enqueue(failing.s(i)).id for i in range(int(n))]
    _say('count=%d ids=%s' % (len(ids), ','.join(ids)))


def revoke_ids(*ids):
    huey = get_huey()
    for task_id in ids:
        huey.revoke_by_id(task_id, revoke_once=False)
    _say('revoked=%d' % len(ids))


def restore_ids(*ids):
    huey = get_huey()
    restored = [huey.restore_by_id(task_id) for task_id in ids]
    _say('restored=%s' % (','.join(str(bool(r)) for r in restored)))


def drain():
    """
    Act like a fresh consumer: dequeue and execute everything, reporting what
    actually ran versus what was skipped as revoked. Used after a "restart"
    to prove the persisted revocation outlived the process that created it.
    """
    huey = get_huey()
    ran, skipped = [], []
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        task = huey.dequeue()
        if task is None:
            break
        result = huey.execute(task)
        (skipped if result is None else ran).append(task.id)
    _say('ran=%s skipped=%s queue=%d schedule=%d' % (
        ','.join(sorted(ran)), ','.join(sorted(skipped)),
        huey.pending_count(), huey.scheduled_count()))


def revoke_and_exit(task_id):
    """Revoke one task id, then exit hard -- the classic crash/restart."""
    huey = get_huey()
    huey.revoke_by_id(task_id, revoke_once=False)
    sys.stderr.write('revoked=%s\n' % task_id)
    sys.stderr.flush()
    os._exit(0)


def retry_while_revoked(task_id):
    """
    Execute a failing task while revoking it after the business function has
    already raised -- the exact window the feature must close. The retry that
    would normally be scheduled must be suppressed.
    """
    huey = get_huey()
    task = huey.dequeue()
    if task is None or task.id != task_id:
        sys.stderr.write('expected %s got %s\n' % (
            task_id, None if task is None else task.id))
        os._exit(2)

    original = task.execute

    def execute_and_revoke():
        try:
            return original()
        finally:
            huey.revoke_by_id(task_id, revoke_once=False)

    task.execute = execute_and_revoke
    huey.execute(task)
    _say('queue=%d schedule=%d' % (huey.pending_count(),
                                   huey.scheduled_count()))


def race_revoke_and_retry(task_id):
    """
    A controllable race: a worker thread runs the failing task and pauses
    between the business function raising and the worker settling. While it is
    paused the main thread revokes the instance; then the worker resumes and
    attempts to schedule its retry. Because the revocation check and the
    delivery write share one SQLite transaction, the retry is suppressed.
    """
    huey = get_huey()
    task = huey.dequeue()
    if task is None or task.id != task_id:
        sys.stderr.write('expected %s got %s\n' % (
            task_id, None if task is None else task.id))
        os._exit(2)

    business_done = threading.Event()
    may_settle = threading.Event()

    original = task.execute

    def gated_execute():
        try:
            return original()
        finally:
            # The business function has raised; pause before the worker
            # settles (schedules the retry).
            business_done.set()
            may_settle.wait(5.0)

    task.execute = gated_execute

    worker = threading.Thread(target=huey.execute, args=(task,), daemon=True)
    worker.start()
    business_done.wait(5.0)
    huey.revoke_by_id(task_id, revoke_once=False)
    may_settle.set()
    worker.join(5.0)

    _say('queue=%d schedule=%d' % (huey.pending_count(),
                                   huey.scheduled_count()))


MODES = {
    'setup': setup,
    'setup-failing': setup_failing,
    'revoke-ids': revoke_ids,
    'restore-ids': restore_ids,
    'drain': drain,
    'revoke-and-exit': revoke_and_exit,
    'retry-while-revoked': retry_while_revoked,
    'race-revoke-and-retry': race_revoke_and_retry,
}


def main(argv):
    if len(argv) < 2 or argv[1] not in MODES:
        sys.stderr.write('usage: REVOKE_DB=... python durable_revoke_driver.py '
                         '%s\n' % '|'.join(sorted(MODES)))
        return 64
    MODES[argv[1]](*argv[2:])
    return 0


# Both ``python -m huey.tests.durable_revoke_driver`` and running the file by
# path set __name__ to "__main__", which would change the registered task
# names and break cross-process deserialization. Re-run ourselves under the
# real package name; the environment flag distinguishes a driver process from
# a plain package import performed by the test suite.
if __name__ == '__main__':
    import runpy
    os.environ['REVOKE_DRIVER_BOOTSTRAP'] = '1'
    runpy.run_module('huey.tests.durable_revoke_driver',
                     run_name='huey.tests.durable_revoke_driver',
                     alter_sys=True)
    sys.exit(0)

if os.environ.get('REVOKE_DRIVER_BOOTSTRAP') == '1':
    sys.exit(main(sys.argv))
