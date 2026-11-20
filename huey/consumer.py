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
from huey.exceptions import TaskTimeout
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
                 max_tasks=None, drain_lock=None):
        self.delay = self.default_delay = default_delay
        self.max_delay = max_delay
        self.backoff = backoff
        self.max_tasks = max_tasks
        self.task_count = 0
        # Lock shared with the consumer (thread/greenlet modes only) that
        # makes the admit-or-requeue decision atomic with respect to the
        # start of a drain. None in process mode, where the stop-flag is
        # the only channel from the parent.
        self.drain_lock = drain_lock
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
        try:
            task = self.huey.dequeue()
        except Exception:
            self._logger.exception('Error reading from queue')
            self.sleep()
        else:
            if task is not None:
                self.delay = self.default_delay
                if not self._admit_task(task):
                    return
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

    def _admit_task(self, task):
        """
        Decide whether a dequeued task may begin executing.

        Once the consumer begins stopping or draining, no new task
        functions may start. Because dequeue() already removed the message
        from storage (none of the backends provide an ack/nack protocol),
        an unadmitted task is put back on the queue with its priority so
        it is not silently lost; a subsequent consumer will run it.

        Returns True when the task should execute.
        """
        if self.drain_lock is not None:
            # Thread/greenlet mode: holding the drain lock orders the
            # admission decision against the start of a drain, so a task
            # is either admitted before the drain began (and will be
            # waited on) or re-enqueued.
            with self.drain_lock:
                admitted = not self.stop_flag.is_set()
        else:
            # Process mode: the stop-flag pipe is the only channel from
            # the parent, so admission is bounded by this check.
            admitted = not self.stop_flag.is_set()

        if admitted:
            return True

        self._logger.info('Consumer is stopping, re-enqueueing %s.', task)
        try:
            self.huey.storage.enqueue(
                self.huey.serialize_task(task), task.priority)
        except Exception:
            self._logger.exception('Error re-enqueueing %s.', task)
        return False

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


class DrainReport(object):
    """
    Outcome of a drain request, returned by DrainResult.wait().

    ``drained`` is only ever True when every worker and the scheduler have
    actually stopped and no tasks remain in flight -- it is never reported
    while background threads/processes are still running. ``resolved`` is
    False when the wait() call's own timeout elapsed before the drain
    finished; such a report is a snapshot, not a final result.
    """
    def __init__(self, drained, pending=(), resolved=True, message=''):
        self.drained = drained
        self.pending = tuple(pending)  # Ids of tasks still in flight.
        self.resolved = resolved
        self.message = message

    def __repr__(self):
        return ('DrainReport(drained=%s, resolved=%s, pending=%s)' %
                (self.drained, self.resolved, list(self.pending)))


class DrainResult(object):
    """
    Waitable completion status for a consumer drain request.

    Created once per consumer by Consumer.drain(); repeated drain() calls
    return the same object, so multiple requesters and any number of
    waiters all observe a single, consistent outcome. The final report is
    resolved exactly once, even if a shutdown hook misbehaves.
    """
    def __init__(self, consumer, timeout):
        self._consumer = consumer
        self._timeout = timeout
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._report = None
        self._monitor_started = False
        self._monitor = threading.Thread(target=self._run_monitor,
                                         name='DrainMonitor')
        self._monitor.daemon = True

    def start_monitor(self):
        with self._lock:
            if not self._monitor_started:
                self._monitor_started = True
                self._monitor.start()

    @property
    def done(self):
        return self._done.is_set()

    @property
    def report(self):
        """The final DrainReport, or None if the drain has not resolved."""
        with self._lock:
            return self._report

    def wait(self, timeout=None):
        """
        Wait for the drain to resolve and return a DrainReport.

        ``timeout`` only bounds this caller's patience; it does not change
        the drain itself. If it elapses first, an unresolved snapshot is
        returned (``resolved=False``) -- never a success report.
        """
        self._done.wait(timeout)
        report = self.report
        if report is not None:
            return report
        pending = self._pending_tasks()
        return DrainReport(
            drained=False, resolved=False, pending=pending,
            message='drain still in progress (%d task(s) in flight%s); '
                    'wait again, or call Consumer.stop() to force '
                    'shutdown' % (len(pending),
                                  ': %s' % ', '.join(pending) if pending
                                  else ''))

    def _pending_tasks(self):
        # Best-effort snapshot of the in-flight set, which workers mutate
        # concurrently. Only meaningful for in-process (thread/greenlet)
        # workers; with process workers the parent's set is always empty
        # and liveness of the worker processes is the source of truth.
        tasks_in_flight = self._consumer.huey._tasks_in_flight
        for _ in range(3):
            try:
                return sorted(str(task.id) for task in tasks_in_flight)
            except RuntimeError:
                continue
        return []

    def _run_monitor(self):
        consumer = self._consumer
        deadline = None
        if self._timeout is not None:
            deadline = time.monotonic() + self._timeout
        try:
            started = self._await_startup(consumer, deadline)
            if started:
                timeout = None if deadline is None else max(
                    deadline - time.monotonic(), 0)
                stopped = consumer._join_workers(timeout)
            else:
                # The consumer never started: nothing is running.
                stopped = True
        except Exception:
            logger.exception('Drain monitor failed while waiting for '
                             'workers to stop.')
            stopped = False
        pending = self._pending_tasks()
        drained = stopped and not pending
        if drained:
            message = ('consumer drained: all workers stopped, no tasks '
                       'in flight')
        else:
            message = (
                'drain incomplete: %d task(s) still in flight%s; call '
                'Consumer.stop() to force shutdown' % (
                    len(pending),
                    ': %s' % ', '.join(pending) if pending else ''))
        report = DrainReport(drained=drained, pending=pending,
                             message=message)
        with self._lock:
            self._report = report
        self._done.set()
        if drained:
            logger.info('%s.', message)
        else:
            logger.warning('%s.', message)

    def _await_startup(self, consumer, deadline):
        # A drain may be requested before or while the consumer starts.
        # Workers can only be joined once they exist, so wait for start()
        # to finish (bounded by the drain deadline, if any).
        while not consumer._started.is_set():
            if deadline is None:
                consumer._started.wait()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                consumer._started.wait(min(remaining, 0.1))
        return True


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

        # Drain-shutdown state. The drain lock also serializes worker task
        # admission against the start of a drain (thread/greenlet modes).
        self._drain_lock = threading.Lock()
        self._drain_result = None
        self._started = threading.Event()

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
        return self.worker_class(
            huey=self.huey,
            stop_flag=self.stop_flag,
            default_delay=self.default_delay,
            max_delay=self.max_delay,
            backoff=self.backoff,
            max_tasks=self.max_tasks,
            # A threading lock cannot synchronize across fork(), so process
            # workers rely on the stop-flag check alone (see _admit_task).
            drain_lock=None if self.environment.forks else self._drain_lock)

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
                except Exception:
                    # A failing shutdown hook must not prevent the process
                    # from exiting (drain/stop waiters rely on it).
                    self._logger.exception('Error in shutdown of %s.', name)
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
        for _, worker_process in self.worker_threads:
            worker_process.start()

        # Finally set the signal handlers for main process.
        self._set_signal_handlers()

        # Unblock a drain monitor that is waiting for start-up to finish.
        self._started.set()

    def drain(self, timeout=None):
        """
        Begin draining the consumer and return a waitable DrainResult.

        Draining stops workers from picking up new tasks -- a task that
        was dequeued but not yet started is put back on the queue rather
        than dropped -- and lets tasks already in flight finish normally,
        including their results, successors and cleanup hooks, before the
        workers and scheduler exit.

        ``timeout`` bounds the total time to wait for in-flight tasks and
        defaults to the consumer's ``shutdown_timeout`` (None means wait
        indefinitely). If the timeout elapses first, the report returned
        by DrainResult.wait() indicates which tasks remain and that
        Consumer.stop() may be used to force shutdown; success is never
        reported while tasks are still running.

        Supported for the "thread" and "process" worker types. With
        process workers, a task dequeued concurrently with drain() may be
        admitted if the worker checked the stop-flag before the drain
        began; the drain still waits for it to finish.

        May be called multiple times and from any thread: all callers
        share a single drain and observe the same result.
        """
        if timeout is None:
            timeout = self.shutdown_timeout
        with self._drain_lock:
            if self._drain_result is not None:
                return self._drain_result
            self._logger.info('Draining consumer: no new tasks will '
                              'start, waiting for in-flight tasks.')
            self.stop_flag.set()
            result = DrainResult(self, timeout)
            self._drain_result = result
            result.start_monitor()
            return result

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
                success = self._join_workers(self.shutdown_timeout)
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

    def _join_workers(self, timeout=None):
        deadline = None
        if timeout is not None:
            deadline = time.monotonic() + timeout
        # Re-read the process list until it is stable: a health-check that
        # raced with the stop request may have replaced a worker.
        seen = set()
        while True:
            procs = [p for _, p in self.worker_threads] + [self.scheduler]
            new_procs = [p for p in procs if id(p) not in seen]
            if not new_procs:
                break
            seen.update(id(p) for p in new_procs)
            for proc in new_procs:
                if deadline is None:
                    proc.join()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    proc.join(remaining)
            if deadline is not None and time.monotonic() >= deadline:
                break
        procs = [p for _, p in self.worker_threads] + [self.scheduler]
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

        if self._drain_result is not None:
            # A drain was requested: wait for in-flight tasks to finish
            # before reporting interrupted tasks and exiting, so tasks
            # that complete normally are not misreported as interrupted.
            report = self._drain_result.wait()
            if not report.drained:
                self._logger.warning('Exiting with drain incomplete: %s',
                                     report.message)

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
        if self.stop_flag.is_set():
            # Stopping or draining: never start replacement workers.
            self._logger.debug('Stop-flag set, skipping health check.')
            return True
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
