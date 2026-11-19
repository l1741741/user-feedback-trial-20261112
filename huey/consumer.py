import contextlib
import inspect
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
from huey.exceptions import TaskTimeout
from huey.utils import noop_context


logger = logging.getLogger('huey.consumer')


class ConsumerStopped(Exception): pass

class WorkerRecycle(Exception): pass

WORKER_THREAD_AND_PROCESS = (WORKER_THREAD, WORKER_PROCESS)


class InFlightDeliveries(object):
    """
    Thread-safe registry of deliveries currently being executed by workers.

    Used by reliable mode so that a forced shutdown can abandon unfinished
    deliveries (leaving them for lease-based recovery) instead of
    acknowledging them.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._deliveries = set()

    def add(self, delivery):
        with self._lock:
            self._deliveries.add(delivery)

    def discard(self, delivery):
        with self._lock:
            self._deliveries.discard(delivery)

    def abandon(self):
        with self._lock:
            deliveries = list(self._deliveries)
        abandoned = []
        for delivery in deliveries:
            if delivery.release():
                abandoned.append(delivery)
        return abandoned

    def __len__(self):
        with self._lock:
            return len(self._deliveries)


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
                 max_tasks=None, reliable=False, lease_seconds=60.0,
                 deliveries=None):
        self.delay = self.default_delay = default_delay
        self.max_delay = max_delay
        self.backoff = backoff
        self.max_tasks = max_tasks
        self.reliable = reliable
        self.lease_seconds = lease_seconds
        # Shared in-flight registry so the consumer can abandon unfinished
        # deliveries on an abrupt shutdown. Only used in reliable mode.
        self.deliveries = deliveries
        self.task_count = 0
        super(Worker, self).__init__(huey, stop_flag)

    def initialize(self):
        for name, startup_hook in self.huey._startup.items():
            self._logger.debug('calling startup hook "%s"', name)
            try:
                startup_hook()
            except Exception:
                self._logger.exception('startup hook "%s" failed', name)

    def shutdown(self):
        for name, shutdown_hook in self.huey._shutdown.items():
            self._logger.debug('calling shutdown hook "%s"', name)
            try:
                shutdown_hook()
            except Exception:
                self._logger.exception('shutdown hook "%s" failed', name)

    def loop(self, now=None):
        task = None
        delivery = None
        try:
            if self.reliable:
                delivery = self.huey.claim_lease(self.lease_seconds)
                task = delivery.task if delivery is not None else None
            else:
                task = self.huey.dequeue()
        except Exception:
            self._logger.exception('Error reading from queue')
            self.sleep()
        else:
            if task is not None:
                self.delay = self.default_delay
                self.task_count += 1
                if delivery is not None and self.deliveries is not None:
                    self.deliveries.add(delivery)
                try:
                    if delivery is not None:
                        delivery.start_renewal()
                        self.huey.execute(task, now, delivery=delivery)
                    else:
                        self.huey.execute(task, now)
                except Exception:
                    self._logger.exception('Unhandled error during execution '
                                           'of task %s.', task.id)
                finally:
                    if delivery is not None and self.deliveries is not None:
                        self.deliveries.discard(delivery)
                if self.max_tasks and self.task_count >= self.max_tasks:
                    self._logger.info('Worker reached max tasks (%d), '
                                      'exiting.', self.max_tasks)
                    raise WorkerRecycle()
            elif not self.huey.storage.blocking:
                self.sleep()
        finally:
            # Anything but a committed ack/retry (e.g. a worker killed by the
            # graceful-shutdown interrupt, or an unexpected error) leaves the
            # delivery in place so it is recovered when its lease expires.
            # ``release()`` is a no-op if the delivery was already completed.
            if delivery is not None:
                delivery.release()
                if self.deliveries is not None:
                    self.deliveries.discard(delivery)

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
                 graceful_signal='INT', reliable=False, lease_seconds=60.0):

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
        self.graceful_signal = graceful_signal

        # Opt-in reliable mode: tasks are claimed under a lease and only
        # removed once acknowledged. Supported only by storage backends that
        # implement the leased-delivery API, and rejected up-front otherwise.
        self.reliable = bool(reliable)
        self.lease_seconds = lease_seconds
        self._deliveries = InFlightDeliveries()

        if max_tasks and not check_worker_health:
            raise ConfigurationError('max_tasks requires check_worker_health '
                                     'be enabled.')

        # Ensure that the scheduler runs at an interval between 1 and 60s.
        self.scheduler_interval = max(min(scheduler_interval, 60), 1)
        if 60 % self.scheduler_interval != 0:
            raise ConfigurationError('Scheduler interval must be a factor '
                                     'of 60, e.g. 1, 2, 3, 4, 5, 6, 10, 12...')

        self.worker_type = worker_type  # What process model are we using?

        if self.reliable:
            self._validate_reliable()

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

    def _validate_reliable(self):
        """
        Reject unsupported reliable-mode configurations before any task is
        consumed, rather than silently falling back to at-most-once delivery.
        """
        if not getattr(self.huey.storage, 'supports_leases', False):
            raise ConfigurationError(
                'reliable=True requires a storage backend that supports '
                'leased delivery, such as SqliteHuey or CySqliteHuey. The '
                'configured storage (%s) does not.' %
                type(self.huey.storage).__name__)

        if isinstance(self.lease_seconds, bool) or not isinstance(
                self.lease_seconds, (int, float)):
            raise ConfigurationError('lease_seconds must be a number.')
        if self.lease_seconds <= 0:
            raise ConfigurationError('lease_seconds must be positive.')

        if self.worker_type not in WORKER_THREAD_AND_PROCESS:
            raise ConfigurationError(
                'reliable=True supports the "thread" and "process" worker '
                'types, not "%s". Lease renewal runs in a background thread '
                'and cannot be relied upon with greenlets.' % self.worker_type)

        # A worker may keep polling for new tasks; the lease interval only
        # needs to be meaningful relative to how long a task can run. Guard
        # against absurdly small values that would make renewal a busy-loop.
        if self.lease_seconds < 0.5:
            raise ConfigurationError('lease_seconds must be at least 0.5.')

        # The lease table is created with the other tables by default. Probe
        # it now so a database created with create_tables=False (or managed
        # externally) fails here rather than while consuming tasks.
        try:
            self.huey.storage.lease_count()
        except Exception as exc:
            raise ConfigurationError(
                'reliable=True requires the "lease" table, which could not '
                'be read from the configured storage. Create the schema '
                '(create_tables=True) or add the table before enabling '
                'reliable mode. Original error: %s' % (exc,)) from exc

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
        kwargs = dict(
            huey=self.huey,
            stop_flag=self.stop_flag,
            default_delay=self.default_delay,
            max_delay=self.max_delay,
            backoff=self.backoff,
            max_tasks=self.max_tasks)
        if self._worker_accepts_reliable():
            kwargs.update(
                reliable=self.reliable,
                lease_seconds=self.lease_seconds,
                deliveries=self._deliveries)
        elif self.reliable:
            # Validated in _validate_reliable(), so this should be
            # unreachable; guard against a subclass bypassing it.
            raise ConfigurationError(
                'reliable=True requires a worker class that accepts the '
                '"reliable" keyword argument.')
        return self.worker_class(**kwargs)

    def _worker_accepts_reliable(self):
        try:
            params = inspect.signature(self.worker_class).parameters
        except (TypeError, ValueError):
            return True  # Cannot introspect; assume it is the built-in.
        return ('reliable' in params or
                any(p.kind is inspect.Parameter.VAR_KEYWORD
                    for p in params.values()))

    def _create_scheduler(self):
        return self.scheduler_class(
            huey=self.huey,
            stop_flag=self.stop_flag,
            interval=self.scheduler_interval,
            periodic=self.periodic)

    def _create_process(self, process, name):
        """
        Repeatedly call the `loop()` method of the given process. Unhandled
        exceptions in the `loop()` method will cause the process to terminate.
        """
        def _run():
            self.environment.init_child(process)
            try:
                process.initialize()
                while not self.stop_flag.is_set():
                    process.loop()
            except KeyboardInterrupt:
                pass
            except WorkerRecycle:
                self._logger.info('Process %s restarting (max tasks).', name)
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
        """
        if self.huey.immediate:
            raise ConfigurationError(
                'Consumer cannot be run with Huey instances where immediate '
                'is enabled. Please check your configuration and ensure that '
                '"huey.immediate = False".')

        # Log startup message.
        self._logger.info('Huey consumer started with %s %s, PID %s at %s',
                          self.workers, self.worker_type, os.getpid(),
                          self.huey._get_timestamp())
        self._logger.info('Scheduler runs every %s second(s).',
                          self.scheduler_interval)
        self._logger.info('Periodic tasks are %s.',
                          'enabled' if self.periodic else 'disabled')
        if self.reliable:
            self._logger.info('Reliable delivery is enabled '
                              '(lease=%ss).', self.lease_seconds)

        msg = ['The following commands are available:']
        for command in self.huey._registry._registry:
            msg.append('+ %s' % command)

        self._logger.info('\n'.join(msg))

        # Forked children must not share the parent's storage connection.
        if self.environment.forks:
            self.huey.storage.close()

        # Start the scheduler and workers.
        self.scheduler.start()
        for _, worker_process in self.worker_threads:
            worker_process.start()

        # Finally set the signal handlers for main process.
        self._set_signal_handlers()

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
            if self.reliable:
                self._abandon_in_flight_deliveries()
        self._interrupt_workers()
        self._logger.info('Shutting down')

    def _interrupt_workers(self):
        self.environment.interrupt_workers([p for _, p in self.worker_threads])

    def _abandon_in_flight_deliveries(self):
        # Workers leave unacknowledged deliveries in place themselves; this is
        # only a best-effort sweep for a worker that died without running its
        # cleanup. Deliveries whose task finished normally are already done.
        deliveries = self._deliveries.abandon()
        for delivery in deliveries:
            if delivery.task is not None:
                self._logger.info('Leaving delivery for task %s in place for '
                                  'recovery.', delivery.task.id)

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
        """
        self.start()
        health_check_ts = time.monotonic()

        while True:
            try:
                health_check_ts = self.loop(health_check_ts)
            except ConsumerStopped:
                break

        if self.reliable:
            # Any delivery the workers have not acknowledged is left in place
            # so it can be recovered once its lease expires.
            self._abandon_in_flight_deliveries()

        self.huey.notify_interrupted_tasks()

        if self._restart:
            self._logger.info('Consumer will restart.')
            python = sys.executable
            os.execl(python, python, *sys.argv)
        else:
            self._logger.info('Consumer exiting.')

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
            # Flag to caller that the main consumer loop should shut down.
            raise ConsumerStopped

        if self._health_check and health_check_ts:
            now = time.monotonic()
            if now >= health_check_ts + self._health_check_interval:
                health_check_ts = now
                self.check_worker_health()

        return health_check_ts

    def check_worker_health(self):
        """
        Check the health of the worker processes. Workers that have died will
        be replaced with new workers.
        """
        self._logger.debug('Checking worker health.')
        workers = []
        restart_occurred = False
        for i, (worker, worker_t) in enumerate(self.worker_threads):
            if not self.environment.is_alive(worker_t):
                self._logger.warning('Worker %d died, restarting.', i + 1)
                worker = self._create_worker()
                worker_t = self._create_process(worker, 'Worker-%d' % (i + 1))
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
            self.scheduler = self._create_process(scheduler, 'Scheduler')
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
