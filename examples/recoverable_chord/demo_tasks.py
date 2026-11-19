"""
Task definitions shared by the recoverable-chord terminal demo.

Importable as a module (``python -m examples.recoverable_chord.demo_tasks``)
so that producer, consumer and recovery subprocesses all resolve the same
task names through the registry.
"""
import os

from huey import SqliteHuey


DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('CHORD_DEMO_DB',
                         os.path.join(DEMO_DIR, 'demo.db'))

huey = SqliteHuey('doc-batch', filename=DB_PATH, durable_chord=True,
                  utc=False, timeout=10)


@huey.task()
def convert(document):
    """A parallel document-conversion member."""
    return 'converted:%s' % document


@huey.task(retries=1)
def flaky_convert(document):
    """Fails on the first attempt, succeeds on the retry."""
    marker = os.path.join(DEMO_DIR, 'attempt-%s' % document)
    if not os.path.exists(marker):
        open(marker, 'w').close()
        raise RuntimeError('transient failure for %s' % document)
    return 'converted:%s' % document


@huey.task()
def submit_batch(results):
    """The chord callback: commits the batch result."""
    path = os.path.join(DEMO_DIR, 'submitted.txt')
    with open(path, 'a') as fh:
        fh.write(repr(results) + '\n')
    return 'submitted %s documents' % len(results)
