import contextlib
import logging
import os
import select
import signal
import sys
import threading
import time

import multiprocessing

try:
    import gevent
    from gevent import Greenlet
    from gevent import monkey
    from gevent.event import Event as GreenEvent
except ImportError:
    Greenlet = GreenEvent = None

from huey.constants import WORKER_GREENLET
from huey.constants import WORKER_PROCESS
from huey.constants import WORKER_THREAD
from huey.constants import WORKER_TYPES
from huey.exceptions import ConfigurationError
from huey.exceptions import ConsumerStartupError
from huey.exceptions import TaskTimeout
from huey.readiness import ProcessReadinessChannel
from huey.readiness import REPORT_ERROR
from huey.readiness import REASON_SHUTDOWN
from huey.readiness import REASON_STARTUP_HOOK
from huey.readiness import REASON_STORAGE
from huey.readiness import REASON_TIMEOUT
from huey.readiness import REASON_UNIT_DIED
from huey.readiness import STATE_FAILED
from huey.readiness import STATE_PENDING
from huey.readiness import STATE_READY
from huey.readiness import ThreadReadinessChannel
from huey.readiness import TERMINAL_STATES
from huey.readiness import error_message as readiness_error
from huey.readiness import ready_message
from huey.readiness import write_readiness_receipt
from huey.utils import noop_context


logger = logging.getLogger('huey.consumer')


class ConsumerStopped(Exception): pass

class WorkerRecycle(Exception): pass


class BaseProcess(object):
    process_name = 'BaseProcess'

    def __init__(self, huey, stop_flag):
        self.huey = huey
        self.stop_flag = stop_flag
        self._logger = self.create_logger()

    def create_logger(self):
        return logging.getLogger('huey.consumer.%s' % self.process_name)

    def initialize(self):
        pass

    def sleep_for_interval(self, start_ts, nseconds):
        """
        Sleep for a given interval with respect to the start timestamp.

        So, if the start timestamp is 1337 and nseconds is 10, the method will
        actually sleep for nseconds - (current_timestamp - start_timestamp). So
        if the current timestamp is 1340, we'll only sleep for 7 seconds (the
        goal being to sleep until 1347, or 1337 + 10).
        """
        sleep_time = nseconds - (time.monotonic() - start_ts)
        if sleep_time <= 0:
            return
        self._logger.debug('Sleeping for %s', sleep_time)
        # Recompute time to sleep to improve accuracy in case the process was
        # pre-empted by the kernel while logging.
        sleep_time = nseconds - (time.monotonic() - start_ts)
        if sleep_time > 0:
            self.stop_flag.wait(sleep_time)

    def loop(self, now=None):
        """
        Process-specific implementation. Called repeatedly for as long as the
        consumer is running. The `now` parameter is currently only used in the
        unit-tests (to avoid monkey-patching datetime / time). Return value is
        ignored, but an unhandled exception will lead to the process exiting.
        """
        raise NotImplementedError

    def shutdown(self):
        """
        Called when process has finished running (e.g., when shutting down).
        """
        pass


class Worker(BaseProcess):
    """
    Worker implementation.

    Will pull tasks from the queue, executing them or adding them to the
    schedule if they are set to run in the future.
    """
    process_name = 'Worker'

    def __init__(self, huey, stop_flag, default_delay, max_delay, backoff,
                 max_tasks=None):
        self.delay = self.default_delay = default_delay
        self.max_delay = max_delay
        self.backoff = backoff
        self.max_tasks = max_tasks
        self.task_count = 0
        super(Worker, self).__init__(huey, stop_flag)

    def initialize(self):
        for name, startup_hook in self.huey._startup.items():
            self._logger.debug('calling startup hook "%s"', name)
            try:
                startup_hook()
            except Exception:
                # A failed startup hook means this worker unit never became
                # ready. Re-raise so the runnable wrapper reports a startup
                # failure instead of silently entering the task loop
                # half-initialized; the wrapper logs the full failure.
                raise

    def shutdown(self):
        for name, shutdown_hook in self.huey._shutdown.items():
            self._logger.debug('calling shutdown hook "%s"', name)
            try:
                shutdown_hook()
            except Exception:
                self._logger.exception('shutdown hook "%s" failed', name)

    def loop(self, now=None):
        task = None
        try:
            task = self.huey.dequeue()
        except Exception:
            self._logger.exception('Error reading from queue')
            self.sleep()
        else:
            if task is not None:
                self.delay = self.default_delay
                self.task_count += 1
                try:
                    self.huey.execute(task, now)
                except Exception:
                    self._logger.exception('Unhandled error during execution '
                                           'of task %s.', task.id)
                if self.max_tasks and self.task_count >= self.max_tasks:
                    self._logger.info('Worker reached max tasks (%d), '
                                      'exiting.', self.max_tasks)
                    raise WorkerRecycle()
            elif not self.huey.storage.blocking:
                self.sleep()

    def sleep(self):
        if self.delay > self.max_delay:
            self.delay = self.max_delay

        self.stop_flag.wait(self.delay)
        self.delay *= self.backoff


class Scheduler(BaseProcess):
    """
    Scheduler handles enqueueing tasks when they are scheduled to execute. Note
    that the scheduler does not actually execute any tasks, but simply enqueues
    them so that they can be picked up by the worker processes.

    If periodic tasks are enabled, the scheduler will wake up every 60 seconds
    to enqueue any periodic tasks that should be run.
    """
    periodic_task_seconds = 60
    process_name = 'Scheduler'

    def __init__(self, huey, stop_flag, interval, periodic):
        super(Scheduler, self).__init__(huey, stop_flag)
        self.interval = max(min(interval, 60), 1)

        self.periodic = periodic
        self._next_loop = time.monotonic()
        self._next_periodic = time.monotonic()

    def loop(self, now=None):
        current = self._next_loop
        self._next_loop += self.interval
        if self._next_loop < time.monotonic():
            self._logger.debug('scheduler skipping iteration to avoid race.')
            self._next_loop = time.monotonic()
            return

        try:
            task_list = self.huey.read_schedule(now)
        except Exception:
            self._logger.exception('Error reading schedule.')
        else:
            for task in task_list:
                self._logger.debug('Enqueueing %s', task)
                try:
                    self.huey.enqueue(task)
                except Exception:
                    self._logger.exception('Error enqueueing %s.', task)

        if self.periodic and self._next_periodic <= time.monotonic():
            # Skip checks missed during a stall (e.g. suspend) to avoid dupes.
            while self._next_periodic <= time.monotonic():
                self._next_periodic += self.periodic_task_seconds
            self.enqueue_periodic_tasks(now)

        self.sleep_for_interval(current, self.interval)

    def enqueue_periodic_tasks(self, now):
        self._logger.debug('Checking periodic tasks')
        for task in self.huey.read_periodic(now):
            self._logger.info('Enqueueing periodic task %s.', task)
            try:
                self.huey.enqueue(task)
            except Exception:
                self._logger.exception('Error enqueueing periodic task %s.',
                                       task)


class ForkSafeFlag(object):
    # Flag for forked worker processes. The problem w/mp.Event is that the
    # process holding it can be killed while it's held, which causes the
    # shutdown to hang.
    def __init__(self):
        self._r, self._w = os.pipe()

    def set(self):
        if not self.is_set():
            os.write(self._w, b'x')

    def is_set(self):
        return self.wait(0)

    def wait(self, timeout=None):
        poller = select.poll()
        poller.register(self._r, select.POLLIN)
        return bool(poller.poll(None if timeout is None else timeout * 1000))


class Environment(object):
    """
    Provide a common interface to the supported concurrent environments.
    """
    forks = False

    def __init__(self, graceful=signal.SIGINT, interrupt=signal.SIGTERM):
        self.graceful = graceful
        self.interrupt = interrupt

    def get_stop_flag(self):
        raise NotImplementedError

    def get_readiness_channel(self):
        # Threads and greenlets share the process and use a plain queue.
        return ThreadReadinessChannel()

    def create_process(self, runnable, name):
        raise NotImplementedError

    def init_child(self, process):
        pass

    def is_alive(self, proc):
        raise NotImplementedError

    def timeout(self, seconds):
        return noop_context()

    def interrupt_workers(self, procs):
        pass


class ThreadEnvironment(Environment):
    def get_stop_flag(self):
        return threading.Event()

    def create_process(self, runnable, name):
        t = threading.Thread(target=runnable, name=name)
        t.daemon = True
        return t

    def is_alive(self, proc):
        return proc.is_alive()


class GreenletEnvironment(Environment):
    def __init__(self, *args):
        if Greenlet is None:
            raise ImportError('Could not import gevent - is it installed?')
        if not monkey.is_module_patched('socket'):
            logger.warning('Gevent monkey-patch has not been applied, this '
                           'may result in incorrect or unpredictable '
                           'behavior.')
        super().__init__(*args)

    def get_stop_flag(self):
        return GreenEvent()

    def create_process(self, runnable, name):
        def run_wrapper():
            try:
                gevent.sleep()
                runnable()
            except KeyboardInterrupt:
                pass
        return Greenlet(run=run_wrapper)

    def is_alive(self, proc):
        return not proc.dead

    @contextlib.contextmanager
    def timeout(self, seconds):
        timer = gevent.Timeout(seconds, TaskTimeout('timeout (%ss)' % seconds))
        timer.start()
        try:
            yield
        finally:
            timer.cancel()

    def interrupt_workers(self, procs):
        # Interrupt worker greenlets that are blocked on I/O.
        gevent.killall(procs, KeyboardInterrupt)


class ProcessEnvironment(Environment):
    forks = True

    def __init__(self, *args):
        super().__init__(*args)
        # Runnables are closures over live objects and can't be pickled.
        try:
            self.mp = multiprocessing.get_context('fork')
        except ValueError:
            raise ConfigurationError(
                'The "process" worker type requires the fork() start-method '
                'and cannot be used on this platform.')

    def get_stop_flag(self):
        return ForkSafeFlag()

    def get_readiness_channel(self):
        # Per-unit pipes: no helper thread is inherited by the forked
        # children, and a killed worker cannot wedge the parent's reader.
        return ProcessReadinessChannel()

    def create_process(self, runnable, name):
        p = self.mp.Process(target=runnable, name=name)
        p.daemon = True
        return p

    def init_child(self, process):
        def interrupt(signum, frame):
            if process.huey._tasks_in_flight:
                # Interrupt if the task is currently running. Otherwise it
                # should exit through the stop flag.
                raise KeyboardInterrupt
        signal.signal(self.graceful, signal.SIG_IGN)
        signal.signal(self.interrupt, interrupt)
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def is_alive(self, proc):
        return proc.is_alive()

    @contextlib.contextmanager
    def timeout(self, seconds):
        def _handle_alrm(signum, frame):
            raise TaskTimeout('timeout (%ss)' % seconds)

        orig = signal.signal(signal.SIGALRM, _handle_alrm)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)  # Cancel any pending alarm.
            signal.signal(signal.SIGALRM, orig)

    def interrupt_workers(self, procs):
        # The mp exit handler terminates children w/SIGTERM, which they
        # ignore when it is the graceful signal.
        for proc in procs:
            if proc.is_alive():
                try:
                    os.kill(proc.pid, self.interrupt)
                except OSError:
                    pass
        # Brief join so the exit handler cannot interrupt mid-cleanup.
        deadline = time.monotonic() + 1
        for proc in procs:
            proc.join(max(deadline - time.monotonic(), 0))


WORKER_TO_ENVIRONMENT = {
    WORKER_THREAD: ThreadEnvironment,
    WORKER_GREENLET: GreenletEnvironment,
    'gevent': GreenletEnvironment,
    WORKER_PROCESS: ProcessEnvironment,
}


class Consumer(object):
    """
    Consumer sets up and coordinates the execution of the workers and scheduler
    and registers signal handlers.
    """
    # Simplify providing custom implementations. See _create_worker and
    # _create_scheduler if you need more sophisticated overrides.
    worker_class = Worker
    scheduler_class = Scheduler

    def __init__(self, huey, workers=1, periodic=True, initial_delay=0.1,
                 backoff=1.15, max_delay=10.0, scheduler_interval=1,
                 worker_type=WORKER_THREAD, check_worker_health=True,
                 health_check_interval=10, flush_locks=False,
                 extra_locks=None, max_tasks=None, shutdown_timeout=None,
                 graceful_signal='INT', startup_timeout=10.0,
                 readiness_file=None):

        self._logger = logging.getLogger('huey.consumer')
        if huey.immediate:
            self._logger.warning('Consumer initialized with Huey instance '
                                 'that has "immediate" mode enabled. This '
                                 'must be disabled before the consumer can '
                                 'be run.')
        self.huey = huey
        self.workers = workers  # Number of workers.
        self.periodic = periodic  # Enable periodic task scheduler?
        self.default_delay = initial_delay  # Default queue polling interval.
        self.backoff = backoff  # Exponential backoff factor when queue empty.
        self.max_delay = max_delay  # Maximum interval between polling events.
        self.max_tasks = max_tasks  # Max tasks to execute before recycling.
        self.shutdown_timeout = shutdown_timeout
        # Unified bound on startup: every unit must report readiness before
        # this many seconds elapse, otherwise startup fails.
        self.startup_timeout = startup_timeout
        # Optional path for the machine-readable readiness receipt.
        self.readiness_file = readiness_file
        self.graceful_signal = graceful_signal

        if max_tasks and not check_worker_health:
            raise ConfigurationError('max_tasks requires check_worker_health '
                                     'be enabled.')

        if startup_timeout is not None and startup_timeout <= 0:
            raise ConfigurationError('startup_timeout must be a positive '
                                     'number of seconds (or None to wait '
                                     'indefinitely).')

        # Ensure that the scheduler runs at an interval between 1 and 60s.
        self.scheduler_interval = max(min(scheduler_interval, 60), 1)
        if 60 % self.scheduler_interval != 0:
            raise ConfigurationError('Scheduler interval must be a factor '
                                     'of 60, e.g. 1, 2, 3, 4, 5, 6, 10, 12...')

        self.worker_type = worker_type  # What process model are we using?

        # Configure health-check and consumer main-loop attributes.
        self._stop_flag_timeout = 0.1
        self._health_check = check_worker_health
        self._health_check_interval = float(health_check_interval)

        # Signal -> (restart, graceful).
        self._graceful_sig, self._interrupt_sig = (
            (signal.SIGTERM, signal.SIGINT) if graceful_signal == 'TERM'
            else (signal.SIGINT, signal.SIGTERM))
        self._signal_actions = {
            self._interrupt_sig: (False, False),
            self._graceful_sig: (False, True),
        }
        if hasattr(signal, 'SIGHUP'):
            self._signal_actions[signal.SIGHUP] = (True, True)
        self._signum = None
        self._restart = False
        self._graceful = True

        # Create the execution environment helper.
        self.environment = self.get_environment(self.worker_type)
        self.huey._timeout_handler = self.environment.timeout
        self.stop_flag = self.environment.get_stop_flag()

        # Startup readiness coordination. The generation identifies each set
        # of units: it starts at 1 for the units built in __init__ and is
        # bumped on every restart, so readiness messages produced by a
        # previous generation (e.g. a late report from a unit being replaced)
        # can never mark a newer generation ready.
        self._generation = 1
        self._startup_state = STATE_PENDING
        self._startup_error = None
        self._ready_event = threading.Event()
        # Serializes terminal startup transitions so teardown completes
        # before any waiter observes the failed state.
        self._startup_lock = threading.Lock()
        self._readiness_channel = self.environment.get_readiness_channel()
        # unit name -> callable used by the unit to report its readiness.
        self._readiness_senders = {}
        self._readiness_thread = None

        # In the event the consumer was killed while running a task that held
        # a lock, this ensures that all locks are flushed before starting.
        if flush_locks or extra_locks:
            lock_names = extra_locks.split(',') if extra_locks else ()
            self.flush_locks(*lock_names)

        # Create the scheduler process (but don't start it yet).
        scheduler = self._create_scheduler()
        self.scheduler = self._create_process(scheduler, 'Scheduler')

        # Create the worker process(es) (also not started yet).
        self.worker_threads = []
        for i in range(workers):
            worker = self._create_worker()
            process = self._create_process(worker, 'Worker-%d' % (i + 1))

            # The worker threads are stored as [(worker impl, worker_t), ...].
            # The worker impl is not currently referenced in any consumer code,
            # but it is referenced in the test-suite.
            self.worker_threads.append((worker, process))

    @property
    def startup_state(self):
        """Current startup lifecycle state (pending/ready/failed)."""
        return self._startup_state

    @property
    def generation(self):
        """Generation of the current (or most recent) set of units."""
        return self._generation

    def is_ready(self):
        return self._startup_state == STATE_READY

    def flush_locks(self, *names):
        self._logger.debug('Flushing locks before starting up.')
        flushed = self.huey.flush_locks(*names)
        if flushed:
            self._logger.warning('Found stale locks: %s' % (
                ', '.join(key for key in flushed)))

    def get_environment(self, worker_type):
        if worker_type not in WORKER_TO_ENVIRONMENT:
            raise ValueError('worker_type must be one of %s.' %
                             ', '.join(WORKER_TYPES))
        return WORKER_TO_ENVIRONMENT[worker_type](self._graceful_sig,
                                                  self._interrupt_sig)

    def _create_worker(self):
        return self.worker_class(
            huey=self.huey,
            stop_flag=self.stop_flag,
            default_delay=self.default_delay,
            max_delay=self.max_delay,
            backoff=self.backoff,
            max_tasks=self.max_tasks)

    def _create_scheduler(self):
        return self.scheduler_class(
            huey=self.huey,
            stop_flag=self.stop_flag,
            interval=self.scheduler_interval,
            periodic=self.periodic)

    def _create_process(self, process, name, generation=None):
        """
        Repeatedly call the `loop()` method of the given process. Unhandled
        exceptions in the `loop()` method will cause the process to terminate.

        Each unit reports readiness exactly once, after its ``initialize()``
        completes successfully. Startup hook failures (and any other error
        raised by initialization) are reported as errors instead; a unit that
        dies without reporting anything is detected by liveness check.

        Units recreated by the health check after startup pass a generation
        older than the current one, so their re-reports are discarded as
        stale rather than re-entering the readiness state machine.
        """
        report = self._readiness_senders.get(name)
        if report is None:
            # New unit name: allocate its endpoint. Units recreated by the
            # health check keep the same name and reuse their endpoint.
            report = self._readiness_channel.sender_for(name)
            self._readiness_senders[name] = report
        # The generation is captured when the runnable is built, so a unit
        # always reports for the generation it belongs to even when the
        # consumer is restarted in-process later.
        generation = self._generation if generation is None else generation

        def _run():
            # Close inherited readiness descriptors this unit does not own
            # (no-op for the in-process channel). Must run before anything
            # that could block, so a forked unit never pins a sibling's pipe.
            self._readiness_channel.init_child(name)
            self.environment.init_child(process)
            try:
                process.initialize()
            except KeyboardInterrupt:
                return
            except SystemExit:
                # An explicit unit exit (e.g. a startup check that gives up)
                # must actually terminate the unit, not be downgraded into a
                # reported error while the runnable keeps living. Re-raise so
                # the thread ends / the forked process exits, and the
                # readiness liveness check reports "unit_died".
                raise
            except BaseException as exc:
                self._logger.exception('Process %s failed during startup.',
                                       name)
                report(readiness_error(generation, name, exc))
            else:
                report(ready_message(generation, name))
                try:
                    while not self.stop_flag.is_set():
                        process.loop()
                except KeyboardInterrupt:
                    pass
                except WorkerRecycle:
                    self._logger.info('Process %s restarting (max tasks).',
                                      name)
                except:
                    self._logger.exception('Process %s died!', name)
            finally:
                try:
                    process.shutdown()
                except KeyboardInterrupt:
                    pass
        return self.environment.create_process(_run, name)

    def start(self):
        """
        Start all consumer processes and register signal handlers.

        Startup is split from readiness: this method returns once the units
        have been launched; use :meth:`wait_until_ready` (or :meth:`run`,
        which waits automatically) to block until every worker unit and the
        scheduler have actually completed their initialization.
        """
        if self.huey.immediate:
            raise ConfigurationError(
                'Consumer cannot be run with Huey instances where immediate '
                'is enabled. Please check your configuration and ensure that '
                '"huey.immediate = False".')

        if self._readiness_thread is not None:
            # An in-process restart (the SIGHUP path normally execs a fresh
            # process, but programmatic users may call start() again): bump
            # the generation first so every rebuilt runnable captures the new
            # number, then rebuild every unit. Late reports from the old
            # generation can then never mark this generation ready.
            self._generation += 1
            self._reset_for_restart()

        self._startup_state = STATE_PENDING
        self._startup_error = None
        self._ready_event.clear()

        # Log startup message.
        self._logger.info('Huey consumer started with %s %s, PID %s at %s',
                          self.workers, self.worker_type, os.getpid(),
                          self.huey._get_timestamp())
        self._logger.info('Scheduler runs every %s second(s).',
                          self.scheduler_interval)
        self._logger.info('Periodic tasks are %s.',
                          'enabled' if self.periodic else 'disabled')

        msg = ['The following commands are available:']
        for command in self.huey._registry._registry:
            msg.append('+ %s' % command)

        self._logger.info('\n'.join(msg))

        # Verify the storage backend is reachable before any unit is started.
        # A failure here is reported as a locatable startup failure with
        # nothing to clean up yet, instead of producing workers that crash in
        # a health-check restart loop.
        try:
            self.huey.storage.queue_size()
        except Exception as exc:
            error = ConsumerStartupError(
                REASON_STORAGE,
                'storage backend is unavailable at startup: %s' % exc)
            self._startup_state = STATE_FAILED
            self._startup_error = error
            self._logger.error('Consumer startup failed (%s): %s',
                               error.reason, error)
            if self.readiness_file:
                self._write_receipt(error)
            self._ready_event.set()
            raise error

        # Forked children must not share the parent's storage connection.
        if self.environment.forks:
            self.huey.storage.close()

        # Start the scheduler and workers.
        self.scheduler.start()
        for _, worker_process in self.worker_threads:
            worker_process.start()

        # Watch the readiness channel until the full unit set has reported or
        # something fails.
        self._start_readiness_monitor()

        # Finally set the signal handlers for main process.
        self._set_signal_handlers()

    def _reset_for_restart(self):
        """Rebuild stop flag, channel endpoints and units for a new start."""
        if any(self.environment.is_alive(p)
               for p in self._all_processes()):
            self.stop()
        self.stop_flag = self.environment.get_stop_flag()
        # Close the previous generation's pipe ends before replacing the
        # channel, so descriptors do not leak across in-process restarts.
        self._readiness_channel.close()
        # A fresh channel guarantees no message from the previous generation
        # can reach the new readiness monitor.
        self._readiness_channel = self.environment.get_readiness_channel()
        self._readiness_senders = {}
        self._readiness_thread = None

        scheduler = self._create_scheduler()
        self.scheduler = self._create_process(scheduler, 'Scheduler')

        self.worker_threads = []
        for i in range(self.workers):
            worker = self._create_worker()
            process = self._create_process(worker, 'Worker-%d' % (i + 1))
            self.worker_threads.append((worker, process))

    def _all_processes(self):
        return [p for _, p in self.worker_threads] + [self.scheduler]

    def _unit_processes(self):
        units = dict(('Worker-%d' % (i + 1), p)
                     for i, (_, p) in enumerate(self.worker_threads))
        units['Scheduler'] = self.scheduler
        return units

    def _start_readiness_monitor(self):
        self._readiness_thread = threading.Thread(
            target=self._readiness_loop, name='HueyReadiness', daemon=True)
        self._readiness_thread.start()

    def _readiness_loop(self):
        """
        Drive the startup state machine until a terminal state is reached.

        A unit becomes confirmed only with a generation-matching readiness
        message. Messages from an older generation are discarded. A unit that
        dies or reports an error before all units are confirmed fails the
        whole startup; the unified timeout and an early shutdown do the same.

        The monitor only *decides* the outcome. Publication of the terminal
        state and failure cleanup are serialized under a lock, so by the time
        a waiter returns from :meth:`wait_until_ready` every started unit has
        already been reclaimed.
        """
        generation = self._generation
        expected = set(self._unit_processes())
        confirmed = set()
        deadline = (None if self.startup_timeout is None
                    else time.monotonic() + self.startup_timeout)

        while True:
            if self.stop_flag.is_set():
                self._fail_startup(ConsumerStartupError(
                    REASON_SHUTDOWN,
                    'consumer was shut down before readiness was reached.'))
                return

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    pending = sorted(expected - confirmed)
                    self._fail_startup(ConsumerStartupError(
                        REASON_TIMEOUT,
                        'startup timeout of %ss elapsed before units %s '
                        'reported ready.' % (self.startup_timeout, pending),
                        unit=pending[0] if pending else None))
                    return
            else:
                remaining = None

            # Liveness check: a unit that exits before startup completes can
            # never report readiness. This also catches a unit that dies at
            # the same time as its (now stale) message arrives.
            for name, proc in self._unit_processes().items():
                if name not in confirmed and not self.environment.is_alive(proc):
                    self._fail_startup(ConsumerStartupError(
                        REASON_UNIT_DIED,
                        'unit %s exited during startup before reporting '
                        'ready.' % name, unit=name))
                    return

            message = self._readiness_channel.recv(
                timeout=min(0.1, remaining) if remaining is not None else 0.1)
            if message is None:
                continue
            if message.generation != generation:
                # Late report from a previous generation: never let it confirm
                # a unit of the current generation.
                self._logger.warning(
                    'Ignoring readiness message from generation %s (current '
                    'generation is %s).', message.generation, generation)
                continue
            if message.unit not in expected:
                self._logger.warning('Ignoring readiness message from unknown '
                                     'unit %s.', message.unit)
                continue

            if message.status == REPORT_ERROR:
                self._fail_startup(ConsumerStartupError(
                    REASON_STARTUP_HOOK,
                    'unit %s failed its startup initialization: %s' % (
                        message.unit, message.error_message),
                    unit=message.unit, error_type=message.error_type))
                return

            confirmed.add(message.unit)
            self._logger.info('Unit %s reported ready (%d/%d).',
                              message.unit, len(confirmed), len(expected))
            if confirmed == expected:
                # A shutdown requested concurrently with the final report
                # wins: units are already exiting, so this is an early
                # shutdown rather than a unit failure.
                if self.stop_flag.is_set():
                    self._fail_startup(ConsumerStartupError(
                        REASON_SHUTDOWN,
                        'consumer was shut down before readiness was '
                        'reached.'))
                    return
                # Re-check liveness so a unit cannot report ready and die
                # before we publish the ready state.
                dead = [name for name, proc in self._unit_processes().items()
                        if not self.environment.is_alive(proc)]
                if dead:
                    self._fail_startup(ConsumerStartupError(
                        REASON_UNIT_DIED,
                        'unit %s exited immediately after reporting ready.'
                        % ', '.join(sorted(dead)), unit=sorted(dead)[0]))
                    return
                self._succeed_startup()
                return

    def _succeed_startup(self):
        with self._startup_lock:
            if self._startup_state in TERMINAL_STATES:
                return
            self._startup_state = STATE_READY
            self._startup_error = None
            self._logger.info('All %d units reported ready; consumer is '
                              'accepting tasks.', self.workers + 1)
            # Write the receipt before releasing waiters so that any process
            # unblocked by wait_until_ready() is guaranteed the receipt is
            # already on disk.
            if self.readiness_file:
                self._write_receipt(None)
            self._ready_event.set()

    def _fail_startup(self, error):
        """
        Record a terminal startup failure and tear down every unit.

        The whole transition is serialized under a lock. The failure receipt
        is persisted *before* unit cleanup begins and before waiters are
        released, so a process unblocked by :meth:`wait_until_ready` is
        guaranteed to find both the failed state and the on-disk receipt,
        rather than racing the monitor while it is still reclaiming units.
        """
        with self._startup_lock:
            if self._startup_state in TERMINAL_STATES:
                return
            self._startup_state = STATE_FAILED
            self._startup_error = error
            self._logger.error('Consumer startup failed (%s): %s',
                               error.reason, error)
            if self.readiness_file:
                self._write_receipt(error)
            self._teardown_startup()
            # Release waiters only after the receipt is durable and the
            # cleanup has completed.
            self._ready_event.set()

    def _teardown_startup(self):
        """Stop and reclaim every unit started for this generation."""
        try:
            self.stop_flag.set()
        except Exception:
            pass
        # Interrupt units blocked inside startup work so they observe the stop
        # flag instead of holding the teardown hostage.
        try:
            self._interrupt_workers()
        except Exception:
            self._logger.exception('Error interrupting workers during startup '
                                   'failure cleanup.')
        # Wait until every unit has actually exited (the readiness monitor
        # terminates as soon as the failure is recorded, so nothing else is
        # policing the sibling units from this point). Bounded by
        # shutdown_timeout, falling back to a grace period long enough to
        # cover a full unit polling interval.
        grace = self.shutdown_timeout or (self._stop_flag_timeout + 0.5)
        deadline = time.monotonic() + grace
        for proc in self._all_processes():
            if not self.environment.is_alive(proc):
                continue  # Never started or already reaped.
            # Re-check with the remaining budget so a unit that only wakes on
            # its next stop-flag poll is still reclaimed within the deadline.
            while self.environment.is_alive(proc):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                proc.join(min(remaining, self._stop_flag_timeout + 0.1))
        # Hard-reclaim forked units that ignored the graceful path. Threads
        # cannot be killed from outside; their startup code must cooperate with
        # the stop flag (as the worker loop does).
        if self.environment.forks:
            for proc in self._all_processes():
                if proc.is_alive():
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    proc.join(1)
        # Every unit is reclaimed; release the parent-side channel
        # descriptors so they do not leak (the readiness monitor has already
        # reached a terminal state and will not read from them again).
        self._readiness_channel.close()

    def _write_receipt(self, error):
        try:
            write_readiness_receipt(
                self.readiness_file, self._startup_state, self._generation,
                self.worker_type, sorted(self._unit_processes()),
                self.startup_timeout, error)
        except Exception:
            self._logger.exception('Could not write readiness receipt to %s',
                                   self.readiness_file)

    def wait_until_ready(self, timeout=None):
        """
        Block until every worker unit and the scheduler have completed their
        startup initialization.

        :param timeout: maximum seconds to wait. When ``None`` (the default),
            wait indefinitely for the consumer's own authoritative startup
            decision, which is always bounded by the consumer's unified
            ``startup_timeout``. Passing an explicit value adds a caller-side
            bound on top.
        :raises ConsumerStartupError: with a machine-readable ``reason`` if
            startup failed, timed out, or was shut down first.
        :raises RuntimeError: if called before :meth:`start`.
        :returns: the readiness receipt payload (same dictionary written to
            ``readiness_file`` when configured).
        """
        if self._readiness_thread is None:
            raise RuntimeError('Consumer has not been started; call start() '
                               'before wait_until_ready().')
        # Poll instead of one blocking wait so a shutdown signal delivered
        # while startup is still pending is serviced here rather than only
        # after the readiness monitor happens to notice the stop flag.
        poll = self._stop_flag_timeout
        remaining = timeout
        while not self._ready_event.wait(poll if remaining is None
                                         else min(poll, remaining)):
            if remaining is not None:
                remaining -= poll
                if remaining <= 0:
                    break
            if self._signum is not None:
                self.stop(graceful=self._graceful)
        if self._ready_event.is_set():
            if self._startup_state == STATE_READY:
                return self._receipt_payload(None)
            raise self._startup_error
        raise ConsumerStartupError(
            REASON_TIMEOUT,
            'wait_until_ready() timed out after %ss before the consumer '
            'reported ready.' % timeout)

    def _receipt_payload(self, error):
        return {
            'status': self._startup_state,
            'generation': self._generation,
            'worker_type': self.worker_type,
            'units': sorted(self._unit_processes()),
            'startup_timeout': self.startup_timeout,
        }

    def stop(self, graceful=False):
        """
        Set the stop-flag.

        If `graceful=True`, this method blocks until the workers to finish
        executing any tasks they might be currently working on, or until
        `shutdown_timeout` elapses.
        """
        self.stop_flag.set()
        if graceful:
            self._logger.info('Shutting down gracefully...')
            try:
                success = self._join_workers()
            except KeyboardInterrupt:
                self._logger.info('Received request to shut down now.')
                self._restart = False
                self._interrupt_workers()
                return
            if success:
                self._logger.info('All workers have stopped.')
                return
            self._logger.warning('Shutdown timeout of %ss elapsed, '
                                 'interrupting workers.',
                                 self.shutdown_timeout)
        self._interrupt_workers()
        self._logger.info('Shutting down')

    def _interrupt_workers(self):
        self.environment.interrupt_workers([p for _, p in self.worker_threads])

    def _join_workers(self):
        deadline = None
        if self.shutdown_timeout is not None:
            deadline = time.monotonic() + self.shutdown_timeout
        procs = [p for _, p in self.worker_threads] + [self.scheduler]
        for proc in procs:
            if deadline is None:
                proc.join()
            else:
                proc.join(max(deadline - time.monotonic(), 0))
        return not any(self.environment.is_alive(p) for p in procs)

    def run(self):
        """
        Run the consumer.

        Blocks until the consumer is ready (every worker unit and the
        scheduler completed startup) before entering the main loop. If startup
        fails, the already-started units are cleaned up and the failure is
        returned to the caller as an exit code (1) instead of entering the
        health-check restart loop with half of the units missing.

        :returns: 0 after a clean shutdown, 1 when startup failed.
        """
        self.start()
        try:
            self.wait_until_ready()
        except ConsumerStartupError as exc:
            # _fail_startup() already tore the units down and (optionally)
            # wrote the failure receipt.
            self._logger.error('Consumer exiting: startup did not complete.')
            return 1

        health_check_ts = time.monotonic()

        while True:
            try:
                health_check_ts = self.loop(health_check_ts)
            except ConsumerStopped:
                break

        self.huey.notify_interrupted_tasks()

        if self._restart:
            self._logger.info('Consumer will restart.')
            python = sys.executable
            os.execl(python, python, *sys.argv)
        else:
            self._logger.info('Consumer exiting.')
        return 0

    def loop(self, health_check_ts=None):
        try:
            stopped = self.stop_flag.wait(timeout=self._stop_flag_timeout)
        except KeyboardInterrupt:
            self._logger.info('Received SIGINT')
            self.stop(graceful=True)
            stopped = True
        except:
            self._logger.exception('Error in consumer.')
            self.stop()
            stopped = True
        else:
            if self._signum is not None:
                self._logger.info('Received %s',
                                  signal.Signals(self._signum).name)
                self.stop(graceful=self._graceful)
                stopped = True

        if stopped:
            # A shutdown requested while startup is still pending must release
            # every readiness waiter rather than leaving it blocked. This only
            # applies once start() has actually launched units; loop() may be
            # driven directly by tests and embedders without a startup phase.
            if (self._startup_state == STATE_PENDING
                    and self._readiness_thread is not None):
                self._fail_startup(ConsumerStartupError(
                    REASON_SHUTDOWN,
                    'consumer was shut down before readiness was reached.'))
            # Flag to caller that the main consumer loop should shut down.
            raise ConsumerStopped

        if self.is_ready():
            # Units recreated by the health check re-report on their reused
            # endpoint; those messages (and any other late reports) are
            # discarded after startup so the channel cannot fill up. Failures
            # at this stage never rewrite the initial startup state: business
            # task errors and max_tasks recycling are normal operation.
            self._readiness_channel.drain()

        if self._health_check and health_check_ts and self.is_ready():
            # Health checks (and their restart loop) only run after the
            # consumer is ready. Before that, the readiness monitor owns
            # failure handling and fails startup instead of silently
            # rebuilding units.
            now = time.monotonic()
            if now >= health_check_ts + self._health_check_interval:
                health_check_ts = now
                self.check_worker_health()

        return health_check_ts

    def check_worker_health(self):
        """
        Check the health of the worker processes. Workers that have died will
        be replaced with new workers.

        Replacement units belong to the same runtime generation but their
        readiness re-reports are tagged with the previous generation so they
        are treated as stale: post-ready restarts never rewrite the initial
        startup state.
        """
        self._logger.debug('Checking worker health.')
        workers = []
        restart_occurred = False
        for i, (worker, worker_t) in enumerate(self.worker_threads):
            if not self.environment.is_alive(worker_t):
                self._logger.warning('Worker %d died, restarting.', i + 1)
                worker = self._create_worker()
                worker_t = self._create_process(
                    worker, 'Worker-%d' % (i + 1),
                    generation=self._generation - 1)
                worker_t.start()
                restart_occurred = True
            workers.append((worker, worker_t))

        if restart_occurred:
            self.worker_threads = workers
        else:
            self._logger.debug('Workers are up and running.')

        if not self.environment.is_alive(self.scheduler):
            self._logger.warning('Scheduler died, restarting.')
            scheduler = self._create_scheduler()
            self.scheduler = self._create_process(
                scheduler, 'Scheduler', generation=self._generation - 1)
            self.scheduler.start()
        else:
            self._logger.debug('Scheduler is up and running.')

        return not restart_occurred

    def _set_signal_handlers(self):
        for signum in self._signal_actions:
            signal.signal(signum, self._handle_signal)

    def _handle_signal(self, signum, frame):
        self._signum = signum
        self._restart, self._graceful = self._signal_actions[signum]
        if self._graceful and not self._restart:
            # A second graceful signal interrupts the shutdown.
            signal.signal(signum, signal.default_int_handler)
