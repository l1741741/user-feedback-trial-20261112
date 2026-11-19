"""
Offline reproduction driver for the reliable schedule-handoff feature.

This module is intentionally importable (its registered tasks keep a stable
name across processes) and is driven from the command line by
``test_schedule_handoff.py``. Every mode runs in its own short-lived process,
which is what makes the crash/restart and dual-scheduler scenarios real
rather than simulated in-thread.

Modes:
    setup          Put N due tasks (plus one future task) in the schedule.
    claim-crash    Claim the due tasks into the handoff area, then exit hard
                   (os._exit) before publishing anything.
    publish-crash  Claim, publish the first task only, then exit hard. This
                   leaves a partially-completed batch on disk.
    drain          Run handoff_scheduled() in a loop until nothing is left,
                   then print queue/schedule/handoff counts and queue task
                   ids. Used as the "restarted scheduler" in crash tests and
                   as both contenders in the dual-scheduler test.
    poison         Insert one un-deserializable blob into the handoff area
                   alongside two valid due tasks, then drain: the bad row
                   must stay parked while the good tasks publish.
    scheduler      Drive the real consumer.Scheduler.loop() once, so the
                   integration path (not just the Huey API) is exercised.

The database path is read from $HANDOFF_DB. Run with ``python -I`` and a
PYTHONPATH pointing at the repository root.
"""
import atexit
import datetime
import os
import sys
import tempfile
import time

from huey import SqliteHuey
from huey.consumer import Scheduler


# Set to True only by the __main__ bootstrap below, marking a real driver
# process (as opposed to a plain import by the test suite). The bootstrap
# signal travels through an environment variable because runpy's
# init_globals are overwritten by the module's own top-level assignment.
_BOOTSTRAPPED = os.environ.get('HANDOFF_DRIVER_BOOTSTRAP') == '1'


class _StopFlag(object):
    def wait(self, timeout=None):
        return True

    def is_set(self):
        return False


def get_huey():
    db = os.environ['HANDOFF_DB']
    _build_registry()
    # Reuse the registry built at module import so tasks serialized in one
    # process can be deserialized in any sibling process, even though each
    # process opens its own database connection.
    huey = SqliteHuey('handoff-driver', filename=db, reliable_schedule=True,
                      utc=False)
    huey._registry = _REGISTRY_HUEY._registry
    return huey


# Tasks must be registered under a stable, importable module name. The huey
# instance is only needed to build the registry entry; the actual database is
# selected per process via HANDOFF_DB. Registration happens inside
# _build_registry() after the __main__ -> package-name bootstrap, otherwise
# the dynamic task class would capture func.__module__ == "__main__".
_reg_fd, _reg_path = tempfile.mkstemp(prefix='handoff-registry-',
                                      suffix='.db')
os.close(_reg_fd)
atexit.register(lambda: os.path.exists(_reg_path) and os.unlink(_reg_path))
_REGISTRY_HUEY = SqliteHuey(
    'handoff-driver-registry', filename=_reg_path, reliable_schedule=True,
    utc=False, results=False)
task_a = None


def _task_a(n):
    return n + 1


def _build_registry():
    global task_a
    if task_a is None:
        wrapper = _REGISTRY_HUEY.task()(_task_a)
        task_class = wrapper.task_class
        # The registry keys tasks by "<task class module>.<name>"; the
        # dynamic class captured func.__module__ ("__main__" during this
        # process's bootstrap pass). Rewrite it to the stable package name
        # and move the registry entry to match, so every driver process
        # resolves the same identifier.
        stable_key = 'huey.tests.handoff_driver.%s' % task_class.__name__
        registry = _REGISTRY_HUEY._registry
        old_key = registry.task_to_string(task_class)
        if old_key != stable_key:
            task_class.__module__ = 'huey.tests.handoff_driver'
            registry._registry.pop(old_key, None)
            registry._registry[stable_key] = task_class
        task_a = wrapper


def _now():
    return datetime.datetime.now()


def _say(message):
    # Explicit flush: these driver processes are harvested over pipes and
    # some modes terminate via os._exit(), which skips buffer flushing.
    sys.stdout.write(message + '\n')
    sys.stdout.flush()


def setup(n=5):
    huey = get_huey()
    now = _now()
    for i in range(n):
        huey.add_schedule(task_a.s(
            i,
            eta=now - datetime.timedelta(seconds=n - i),
            id='due-%d' % i))
    # A future task that no scheduler is allowed to take early.
    huey.add_schedule(task_a.s(
        99,
        eta=now + datetime.timedelta(hours=1),
        id='future-1'))
    _say('scheduled=%d' % huey.scheduled_count())


def claim_crash():
    huey = get_huey()
    rows = huey.storage.claim_scheduled(_now())
    # Hard exit: no finally blocks, no flushing, simulating SIGKILL / power
    # loss between claim and publish.
    sys.stderr.write('claimed=%d\n' % len(rows))
    sys.stderr.flush()
    os._exit(0)


def publish_crash():
    huey = get_huey()
    rows = huey.storage.claim_scheduled(_now())
    if not rows:
        os._exit(2)
    handoff_id, data = rows[0]
    ok = huey.storage.publish_scheduled(handoff_id, data, 0.0)
    sys.stderr.write('published_one=%s handoff_left=%d\n' %
                     (ok, huey.storage.handoff_size()))
    sys.stderr.flush()
    # Die before publishing the rest / recording anything else.
    os._exit(0)


def drain():
    huey = get_huey()
    deadline = time.monotonic() + 10.0
    rounds = 0
    while time.monotonic() < deadline:
        published = huey.handoff_scheduled(_now())
        rounds += 1
        # Stop once nothing is parked in the handoff area and the last
        # publication was a no-op. Future-dated schedule rows are expected
        # to remain and must not keep this loop spinning.
        if not published and huey.storage.handoff_size() == 0:
            break
        time.sleep(0.05)
    ids = sorted(task.id for task in huey.pending())
    _say('rounds=%d queue=%d schedule=%d handoff=%d ids=%s' % (
        rounds,
        huey.pending_count(),
        huey.scheduled_count(),
        huey.storage.handoff_size(),
        ','.join(ids)))


def poison():
    huey = get_huey()
    now = _now()
    huey.add_schedule(task_a.s(1, eta=now, id='good-1'))
    huey.add_schedule(task_a.s(2, eta=now, id='good-2'))
    # A handoff row whose payload cannot be deserialized by any process.
    huey.storage.sql(
        'insert into schedule_handoff (queue, data, attempts, created_at) '
        'values (?, ?, 0, ?)',
        (huey.name, b'\x80\x04this-is-not-a-valid-huey-message', time.time()),
        commit=True)
    n = huey.handoff_scheduled(now)
    parked = huey.storage.sql(
        'select attempts from schedule_handoff where queue = ?',
        (huey.name,), results=True)
    ids = sorted(task.id for task in huey.pending())
    _say('published=%d handoff_attempts=%s queue_ids=%s' % (
        n, parked, ','.join(ids)))


def scheduler():
    # Exercise the actual Scheduler.loop() integration point.
    huey = get_huey()
    sched = Scheduler(huey, _StopFlag(), interval=1, periodic=False)
    sched.loop(_now())
    ids = sorted(task.id for task in huey.pending())
    _say('queue=%d schedule=%d handoff=%d ids=%s' % (
        huey.pending_count(),
        huey.scheduled_count(),
        huey.storage.handoff_size(),
        ','.join(ids)))


MODES = {
    'setup': setup,
    'claim-crash': claim_crash,
    'publish-crash': publish_crash,
    'drain': drain,
    'poison': poison,
    'scheduler': scheduler,
}


def main(argv):
    if len(argv) < 2 or argv[1] not in MODES:
        sys.stderr.write('usage: HANDOFF_DB=... python handoff_driver.py %s\n'
                         % '|'.join(sorted(MODES)))
        return 64
    MODES[argv[1]](*argv[2:])
    return 0


# Both ``python -m huey.tests.handoff_driver`` and running the file by path
# make runpy set this module's __name__ to "__main__", which would change the
# registered task class name and break deserialization in sibling processes.
# Re-run ourselves under the real package name so task identifiers stay
# identical across every driver process. On re-entry runpy executes the same
# module globals with __name__ set to the package name; the _BOOTSTRAPPED
# flag (set by the bootstrap call) distinguishes a driver process from a
# plain package import performed by the test suite.
if __name__ == '__main__':
    import runpy
    os.environ['HANDOFF_DRIVER_BOOTSTRAP'] = '1'
    runpy.run_module('huey.tests.handoff_driver',
                     run_name='huey.tests.handoff_driver',
                     alter_sys=True)
    sys.exit(0)

_build_registry()

if _BOOTSTRAPPED:
    sys.exit(main(sys.argv))
