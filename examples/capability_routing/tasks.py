"""
Local report "services" for the capability-routing demonstration.

No external report service is installed: these plain functions only record
which kind of consumer ran them and return a small payload, so the routing
behavior can be observed end-to-end against a temporary SQLite queue. They
are turned into Huey tasks inside demo.py.
"""
import threading


# Shared, thread-safe log of every task execution. The demo script reads this
# to prove which consumer handled which delivery.
run_log = []
_log_lock = threading.Lock()

# State for the flaky task, keyed by task argument.
_attempts = {}


def record(kind, consumer, name):
    entry = '%s:%s@%s' % (kind, name, consumer)
    with _log_lock:
        run_log.append(entry)
    return entry


def csv_report(name):
    """Plain CSV export -- no special capability required."""
    return record('csv', 'csv-worker', name)


def pdf_report(name):
    """PDF export -- requires a machine with the font packages."""
    return record('pdf', 'pdf-worker', name)


def archive_report(name):
    """Mixed export -- needs both the CSV and PDF toolchains."""
    return record('archive', 'archive-capable-worker', name)


def flaky_report(name):
    """Fails the first time to demonstrate retry preserves requirements."""
    with _log_lock:
        attempts = _attempts.get(name, 0) + 1
        _attempts[name] = attempts
    if attempts < 2:
        raise RuntimeError('simulated transient failure on attempt %d' %
                           attempts)
    return record('flaky-ok', 'pdf-worker', name)
