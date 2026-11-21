"""End-to-end demo: rename a task name without dropping old messages.

Scenario
--------
1. The OLD publisher (``tasks_old``) enqueues reports under the old name
   ``tasks_old.generate_report``: one immediate, one scheduled in the future,
   and one configured to fail twice (so it will retry).
2. The NEW consumer (``tasks_new``) is started in a separate process. It has
   the renamed task ``tasks_new.generate_report`` registered, plus an alias
   mapping the old name onto it.
3. The consumer runs. Old-name messages execute through the new task. The
   failing message retries: while a worker holds it (in-flight), then from the
   queue, still under the old name until the alias is retired.
4. We poll ``task_alias_status``. The in-flight claim cannot be seen by simply
   counting the queue, so retiring while it is held must fail.
5. Once drained, the alias is retired.

Run with::

    python run_demo.py

The script uses a fresh SQLite database in a temporary directory, so it is
safe to run repeatedly.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def run(script, *args, env, expected=True):
    result = subprocess.run([sys.executable, script] + list(args),
                            cwd=BASE_DIR, env=env, text=True,
                            capture_output=True)
    if expected and result.returncode != 0:
        raise SystemExit('command failed: %s\nstdout:\n%s\nstderr:\n%s' % (
            ' '.join([script] + list(args)), result.stdout, result.stderr))
    return result


def json_cmd(script, *args, env):
    result = run(script, *args, env=env, expected=False)
    return result.returncode, json.loads(result.stdout)


def step(number, title):
    print('\n' + '=' * 70)
    print('STEP %s: %s' % (number, title))
    print('=' * 70)


def main():
    workdir = tempfile.mkdtemp(prefix='huey-alias-demo-')
    env = dict(os.environ,
               HUEY_ALIAS_DB=os.path.join(workdir, 'reports.db'),
               HUEY_ALIAS_STATE=os.path.join(workdir, 'state'),
               PYTHONPATH=BASE_DIR)
    consumer = None
    try:
        step(1, 'OLD publisher enqueues under the old task name')
        out = run('publish_demo.py', env=env)
        print(out.stdout.rstrip())
        ids = json.load(open(os.path.join(env['HUEY_ALIAS_STATE'],
                                          'ids.json')))

        step(2, 'NEW consumer process starts with the alias registered')
        consumer = subprocess.Popen(
            [sys.executable, 'consume.py', '--timeout', '30'],
            cwd=BASE_DIR, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        # Poll while the consumer works. Catch the moment a message is
        # claimed but not finished, and try to retire the alias then.
        inflight_sample = None
        retire_while_inflight = None
        for _ in range(400):
            _, status = json_cmd('status_cli.py', '--status', env=env)
            if status['inflight'] and inflight_sample is None:
                inflight_sample = status
                _, retire_while_inflight = json_cmd('status_cli.py',
                                                    '--retire', env=env)
            if consumer.poll() is not None:
                break
            time.sleep(0.05)

        out, _ = consumer.communicate(timeout=40)
        for line in out.splitlines():
            if line.startswith(('Huey consumer', 'Shutting down',
                                'All workers')):
                print(line)

        step(3, 'Retries crossed the rename')
        state_dir = env['HUEY_ALIAS_STATE']
        ledgers = {}
        for name in os.listdir(state_dir):
            if name.startswith('attempts-'):
                with open(os.path.join(state_dir, name)) as fh:
                    ledgers[name[len('attempts-'):]] = int(fh.read() or 0)
        for day in sorted(ledgers):
            print('  %s: executed %d time(s) as the NEW task' %
                  (day, ledgers[day]))
        assert ledgers.get('2026-11-21') == 3, ledgers  # 2 failures + success

        step(4, 'In-flight accounting blocks retirement')
        print('status while a worker held an old-name message:')
        print(json.dumps(inflight_sample, indent=2))
        print('\nretire attempt at that moment:')
        print(json.dumps(retire_while_inflight, indent=2))

        step(5, 'Backlog drained; retire the alias')
        _, final_status = json_cmd('status_cli.py', '--status', env=env)
        print(json.dumps(final_status, indent=2))
        _, retire = json_cmd('status_cli.py', '--retire', env=env)
        print('\nretire result:')
        print(json.dumps(retire, indent=2))

        step(6, 'Old-name messages kept their identity')
        for label, task_id in sorted(ids.items()):
            _, record = json_cmd('status_cli.py', '--result', task_id,
                                 env=env)
            print('  %-9s %s -> %s' % (label, task_id, record['result']))

        step(7, 'After retirement, stale old-name messages are rejected')
        out = run('after_retire.py', env=env)
        print(out.stdout.rstrip())

        # Assertions so the demo fails loudly if behavior regresses.
        assert inflight_sample and inflight_sample['inflight'] == 1
        assert retire_while_inflight['retired'] is False
        assert retire_while_inflight['error_type'] == 'TaskAliasInUse'
        assert final_status['total'] == 0, final_status
        assert retire['retired'] is True
        print('\nDEMO OK: rename, in-flight retry accounting and retirement '
              'behaved as expected.')
        return 0
    finally:
        if consumer is not None and consumer.poll() is None:
            consumer.kill()
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
