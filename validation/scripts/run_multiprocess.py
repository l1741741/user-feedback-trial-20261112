"""Driver: run the keyspace-isolation worker across real process restarts.

Each phase is a separate ``python -I`` subprocess with a fresh interpreter and
a fresh SQLite connection, so persisted control-state is verified across real
restarts rather than within one process. Business data lives under
validation/data, never next to the scripts.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, 'mp_worker.py')
PYTHON = sys.executable


def main():
    data_dir = tempfile.mkdtemp(prefix='huey-keyspace-')
    db_path = os.path.join(data_dir, 'shared.db')
    for phase in ('write', 'verify', 'mutate'):
        proc = subprocess.run(
            [PYTHON, '-I', WORKER, db_path, phase],
            capture_output=True, text=True)
        if proc.returncode != 0:
            print('phase %s FAILED' % phase)
            print(proc.stdout)
            print(proc.stderr)
            sys.exit(1)
        print('phase %-6s -> %s' % (phase, proc.stdout.strip()))
    print('MULTI-PROCESS RESTART ISOLATION OK')


if __name__ == '__main__':
    main()
