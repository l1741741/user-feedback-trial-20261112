import contextlib
import logging
import os
import select
import signal
import sys
import threading
import time

from collections import namedtuple

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


class WorkerExit(object):
    """
    The reason a worker (or scheduler) process/thread exited. RECYCLED is a
    planned recycle after reaching ``max_tasks``, CRASHED an unexpected
    failure, and STOPPED / INTERRUPTED a deliberate consumer shutdown.
    """
    STOPPED = 'stopped'
    RECYCLED = 'recycled'
    CRASHED = 'crashed'
    INTERRUPTED = 'interrupted'


# Process exit-codes used to communicate the exit reason back to the parent
# when workers run in their own processes (where the parent's Worker objects
# do not reflect what happened in the child).
EXIT_CODE_RECYCLED = 3
EXIT_CODE_CRASHED = 4


# A point in a worker's lifecycle ("start" / "exit"). ``reason`` is one of
# the WorkerExit constants for "exit" events. The task counters are only
# available when the worker shares memory with the consumer (thread and
# greenlet workers); for forked workers the child reports them in its own
# log output and they are recorded as None here.
WorkerEvent = namedtuple('WorkerEvent', (
    'event', 'name', 'generation', 'reason', 'task_count',
    'tasks_completed', 'tasks_requeued', 'timestamp'))


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
        # Lifecycle state for this worker generation. `task_count` drives
        # max_tasks and counts every dequeued task; `tasks_completed` only
        # counts tasks whose chain finished (success or final failure),
        # while `tasks_requeued` counts executions that were requeued (e.g.
        # a retry) and will be finished by whichever generation picks the
        # task back up.
        self.generation = 0
        self.tasks_completed = 0
        self.tasks_requeued = 0
        self.exit_reason = None
        self._shutdown_hooks_run = set()
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
            if name in self._shutdown_hooks_run:
                continue  # Never re-run a hook that already completed.
            self._logger.debug('calling shutdown hook "%s"', name)
            try:
                shutdown_hook()
            except Exception:
                # Log and continue so the remaining hooks still run.
                self._logger.exception('shutdown hook "%s" failed', name)
            self._shutdown_hooks_run.add(name)

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
                else:
                    # A task that was requeued (e.g. for a retry) has not
                    # finished its chain and must not be counted as
                    # completed, even if an intermediate error was stored.
                    if getattr(task, '_chain_finished', True):
                        self.tasks_completed += 1
                    else:
                        self.tasks_requeued += 1
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

    def exit_process(self, reason):
        """
        Called at the very end of a process/thread runnable with the
        WorkerExit reason. Only the process environment needs to act here
        (communicating the reason to the parent via the exit-code); shared
        memory environments record the reason on the worker itself.
        """


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

    def exit_process(self, reason):
        # Communicate the exit reason to the parent via the exit-code so it
        # can distinguish a planned recycle from a crash or a shutdown.
        if reason == WorkerExit.RECYCLED:
            sys.exit(EXIT_CODE_RECYCLED)
        elif reason == WorkerExit.CRASHED:
            sys.exit(EXIT_CODE_CRASHED)


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
                 graceful_signal='INT'):

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

        if max_tasks and not check_worker_health:
            raise ConfigurationError('max_tasks requires check_worker_health '
                                     'be enabled.')

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

        # Lifecycle tracking for worker generations. A worker that reaches
        # max_tasks (or crashes) is replaced by a new generation once it has
        # fully exited; every start/exit is recorded in `_lifecycle_events`.
        # `_stopping` ensures no new generation is spawned once shutdown has
        # begun, and the lock serializes health-check replacements against
        # stop() so a late stop cannot signal a brand-new generation.
        self._stopping = False
        self._worker_generation = 0
        self._lifecycle_events = []
        self._lifecycle_lock = threading.RLock()
        self._exits_recorded = set()

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
        self._worker_generation += 1
        worker = self.worker_class(
            huey=self.huey,
            stop_flag=self.stop_flag,
            default_delay=self.default_delay,
            max_delay=self.max_delay,
            backoff=self.backoff,
            max_tasks=self.max_tasks)
        worker.generation = self._worker_generation
        return worker

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
            reason = WorkerExit.STOPPED
            try:
                process.initialize()
                while not self.stop_flag.is_set():
                    process.loop()
            except KeyboardInterrupt:
                reason = WorkerExit.INTERRUPTED
            except WorkerRecycle:
                reason = WorkerExit.RECYCLED
                self._logger.info('Process %s recycling (max tasks).', name)
            except:
                reason = WorkerExit.CRASHED
                self._logger.exception('Process %s died!', name)
            try:
                process.shutdown()
            except KeyboardInterrupt:
                # Retry once so the remaining hooks still run; hooks that
                # already completed are not executed again.
                try:
                    process.shutdown()
                except KeyboardInterrupt:
                    pass
            process.exit_reason = reason
            if isinstance(process, Worker):
                self._logger.info(
                    'Worker %s (generation %s) exited: reason=%s tasks=%d '
                    'completed=%d requeued=%d', name, process.generation,
                    reason, process.task_count, process.tasks_completed,
                    process.tasks_requeued)
            self.environment.exit_process(reason)
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

        msg = ['The following commands are available:']
        for command in self.huey._registry._registry:
            msg.append('+ %s' % command)

        self._logger.info('\n'.join(msg))

        # Forked children must not share the parent's storage connection.
        if self.environment.forks:
            self.huey.storage.close()

        # Start the scheduler and workers.
        self.scheduler.start()
        for i, (worker, worker_process) in enumerate(self.worker_threads):
            worker_process.start()
            self._record_worker_event('start', worker, 'Worker-%d' % (i + 1))

        # Finally set the signal handlers for main process.
        self._set_signal_handlers()

    def stop(self, graceful=False):
        """
        Set the stop-flag.

        If `graceful=True`, this method blocks until the workers to finish
        executing any tasks they might be currently working on, or until
        `shutdown_timeout` elapses.
        """
        # From this point on no new worker generation may be spawned.
        self._stopping = True
        self.stop_flag.set()
        if graceful:
            self._logger.info('Shutting down gracefully...')
            try:
                success = self._join_workers()
            except KeyboardInterrupt:
                self._logger.info('Received request to shut down now.')
                self._restart = False
                self._interrupt_workers()
                self._record_worker_exits()
                return
            if success:
                self._logger.info('All workers have stopped.')
                self._record_worker_exits()
                return
            self._logger.warning('Shutdown timeout of %ss elapsed, '
                                 'interrupting workers.',
                                 self.shutdown_timeout)
        self._interrupt_workers()
        self._record_worker_exits()
        self._logger.info('Shutting down')

    def _interrupt_workers(self):
        # Snapshot the current generation under the lifecycle lock: a worker
        # that is being recycled concurrently is either fully replaced before
        # this snapshot (and the new generation is correctly interrupted) or
        # the health check observes `_stopping` and spawns nothing.
        with self._lifecycle_lock:
            procs = [p for _, p in self.worker_threads]
        self.environment.interrupt_workers(procs)

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

    @property
    def lifecycle_events(self):
        """
        A snapshot of the recorded worker lifecycle events (WorkerEvent
        namedtuples), in the order they occurred.
        """
        with self._lifecycle_lock:
            return list(self._lifecycle_events)

    def _record_worker_event(self, event, worker, name, reason=None):
        if self.environment.forks:
            # Task counters are maintained in the child and reported in its
            # own log output; they are not visible to the parent.
            task_count = tasks_completed = tasks_requeued = None
        else:
            task_count = worker.task_count
            tasks_completed = worker.tasks_completed
            tasks_requeued = worker.tasks_requeued
        record = WorkerEvent(
            event=event, name=name, generation=worker.generation,
            reason=reason, task_count=task_count,
            tasks_completed=tasks_completed, tasks_requeued=tasks_requeued,
            timestamp=time.time())
        with self._lifecycle_lock:
            self._lifecycle_events.append(record)

    def _worker_exit_reason(self, worker, worker_t):
        # Shared-memory environments record the reason on the worker itself;
        # for forked workers it is communicated via the process exit-code.
        reason = getattr(worker, 'exit_reason', None)
        if reason is not None:
            return reason
        exitcode = getattr(worker_t, 'exitcode', None)
        if exitcode is None:
            return None  # Never started.
        if exitcode == 0:
            return WorkerExit.STOPPED
        elif exitcode == EXIT_CODE_RECYCLED:
            return WorkerExit.RECYCLED
        return WorkerExit.CRASHED

    def _record_worker_exit(self, worker, worker_t, name):
        # Keep a reference to the process object (rather than its id()) so
        # that a recycled object's reused id cannot mask a new generation.
        if worker_t in self._exits_recorded:
            return
        reason = self._worker_exit_reason(worker, worker_t)
        if reason is None:
            return
        self._exits_recorded.add(worker_t)
        self._record_worker_event('exit', worker, name, reason)

    @staticmethod
    def _reap(proc):
        # Join a defunct process/thread to reap it. Processes and threads
        # expose `ident` once started; greenlets need no reaping.
        if getattr(proc, 'ident', None) is not None:
            proc.join()

    def _record_worker_exits(self):
        for i, (worker, worker_t) in enumerate(self.worker_threads):
            if not self.environment.is_alive(worker_t):
                self._record_worker_exit(worker, worker_t,
                                         'Worker-%d' % (i + 1))

    def check_worker_health(self):
        """
        Check the health of the worker processes. Workers that have exited
        (whether by planned recycle or by crashing) are replaced with a new
        generation -- but never after the consumer has begun shutting down.
        """
        if self._stopping or self.stop_flag.is_set():
            self._logger.debug('Consumer is stopping, not restarting '
                               'workers.')
            return True

        self._logger.debug('Checking worker health.')
        workers = []
        restart_occurred = False
        with self._lifecycle_lock:
            if self._stopping or self.stop_flag.is_set():
                return True
            for i, (worker, worker_t) in enumerate(self.worker_threads):
                if not self.environment.is_alive(worker_t):
                    # The old generation has fully exited; only now do we
                    # bring up its replacement.
                    self._reap(worker_t)
                    name = 'Worker-%d' % (i + 1)
                    self._record_worker_exit(worker, worker_t, name)
                    reason = self._worker_exit_reason(worker, worker_t)
                    if reason == WorkerExit.CRASHED:
                        self._logger.warning('Worker %d died, restarting.',
                                             i + 1)
                    else:
                        self._logger.info('Worker %d exited (%s), '
                                          'restarting.', i + 1, reason)
                    worker = self._create_worker()
                    worker_t = self._create_process(worker, name)
                    worker_t.start()
                    self._record_worker_event('start', worker, name)
                    restart_occurred = True
                workers.append((worker, worker_t))

            if restart_occurred:
                self.worker_threads = workers
            else:
                self._logger.debug('Workers are up and running.')

            if not self.environment.is_alive(self.scheduler):
                self._logger.warning('Scheduler died, restarting.')
                self._reap(self.scheduler)
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
