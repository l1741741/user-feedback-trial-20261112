"""
Self-contained fixture module for the real-subprocess readiness tests.

It is run as ``python -I -m huey.tests.readiness_fixture`` with the
repository root supplied through PYTHONPATH, so it only relies on the
stdlib and the huey package itself.

Scenario selection is environment-driven (HUEY_TEST_* variables): the
subprocess inherits a clean environment plus those variables, and no
credential-bearing command-line arguments are ever parsed.
"""
import multiprocessing
import os
import sys
import tempfile
import threading
import time

from huey import SqliteHuey
from huey.exceptions import ConsumerStartupError


# The consumer is assigned in main() and read from the startup hooks.
CONSUMER = None


def unit_name():
    """Name the consumer assigned to the running unit."""
    proc = multiprocessing.current_process()
    if proc.name != 'MainProcess':
        return proc.name
    return threading.current_thread().name


def wait_for_stop(timeout=30):
    """Cooperative startup block that honors the consumer stop flag."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if CONSUMER is not None and CONSUMER.stop_flag.is_set():
            return
        time.sleep(0.02)


def log(msg):
    sys.stderr.write('[fixture] %s\n' % msg)
    sys.stderr.flush()


def main():
    global CONSUMER

    scenario = os.environ.get('HUEY_TEST_SCENARIO', 'success')
    worker_type = os.environ.get('HUEY_TEST_WORKER_TYPE', 'thread')
    workers = int(os.environ.get('HUEY_TEST_WORKERS', '2'))
    startup_timeout = float(os.environ.get('HUEY_TEST_STARTUP_TIMEOUT', '10'))
    receipt_path = os.environ.get('HUEY_TEST_READINESS_FILE') or None
    fail_worker = os.environ.get('HUEY_TEST_FAIL_WORKER', 'Worker-2')

    db_dir = tempfile.mkdtemp(prefix='huey-readiness-db-')
    filename = os.path.join(db_dir, 'huey.db')

    huey = SqliteHuey('readiness-fixture', filename=filename)

    if scenario == 'storage_missing_dir':
        # The database opens fine at construction time, then becomes
        # unavailable: the consumer's startup preflight (a storage probe
        # before any unit launches) must reject startup. This mirrors a
        # backend going away between import and consumer start without the
        # fixture itself crashing while building the Huey instance.
        class UnavailableStorage(huey.storage.__class__):
            def queue_size(self):
                raise OSError('storage backend is unavailable')

        huey.storage = UnavailableStorage(name=huey.storage.name,
                                          filename=filename)

    @huey.task()
    def add(a, b):
        return a + b

    if scenario in ('hook_failure', 'partial_failure'):
        @huey.on_startup()
        def failing_hook():
            # Only the selected unit fails; its siblings get far enough to
            # report ready, exercising partial-success-then-failure cleanup.
            if unit_name() == fail_worker:
                raise RuntimeError('fixture startup hook failure in %s'
                                   % fail_worker)

    if scenario in ('hook_block', 'shutdown_before_ready'):
        @huey.on_startup()
        def blocking_hook():
            wait_for_stop()

    if scenario == 'worker_exit':
        # Die immediately, without ever sending a readiness report. Raising
        # SystemExit inside a startup hook terminates the unit in both models:
        # the worker thread ends (thread mode) or the forked process exits
        # non-zero (process mode), and the readiness liveness check reports
        # "unit_died".
        @huey.on_startup()
        def exiting_hook():
            if unit_name() == 'Worker-1':
                raise SystemExit(23)

    CONSUMER = huey.create_consumer(
        workers=workers,
        worker_type=worker_type,
        startup_timeout=startup_timeout,
        readiness_file=receipt_path,
        initial_delay=0.05,
        max_delay=0.05,
        check_worker_health=True,
        health_check_interval=1,
        # The supervisor in the tests stops us with SIGTERM; make TERM the
        # graceful signal so a ready consumer exits cleanly with code 0.
        graceful_signal='TERM',
        shutdown_timeout=5)
    CONSUMER.start()

    try:
        CONSUMER.wait_until_ready()
    except ConsumerStartupError as exc:
        log('startup failed: %s %s' % (exc.reason, exc.unit or ''))
        return 1

    if scenario == 'success':
        # A ready consumer must actually be able to process work before we
        # hand control to the consumer's own main loop. Running the real
        # loop (instead of parking in signal.pause()) is also what lets the
        # supervisor's SIGTERM be serviced for a graceful code-0 exit.
        assert add(40, 2).get(blocking=True, timeout=10) == 42
        log('ready and task processed; entering consumer main loop')
        return CONSUMER.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
