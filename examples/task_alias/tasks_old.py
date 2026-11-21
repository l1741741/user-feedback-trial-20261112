"""The OLD publisher.

This module is deployed before the rename and knows only the old task name.
It is also the module a stale process would still be importing while the new
consumers are already rolling out.
"""
import datetime
import os
import sys

from huey import SqliteHuey

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alias_config


def _ledger_path():
    return os.path.join(alias_config.STATE_DIR, 'attempts.log')


huey = SqliteHuey(alias_config.QUEUE_NAME, filename=alias_config.DB_PATH,
                  utc=False)


@huey.task(retries=alias_config.RETRIES,
           retry_delay=alias_config.RETRY_DELAY)
def generate_report(day, fail_times=0):
    """
    The legacy daily-report task. ``fail_times`` makes the first attempts fail
    so the demo can show a retry crossing the rename boundary.
    """
    os.makedirs(alias_config.STATE_DIR, exist_ok=True)
    attempts = 0
    with open(_ledger_path(), 'a') as fh:
        fh.write('old-publisher:%s\n' % day)

    attempts_path = os.path.join(alias_config.STATE_DIR,
                                 'attempts-%s' % day)
    if os.path.exists(attempts_path):
        with open(attempts_path) as fh:
            attempts = int(fh.read().strip() or 0)
    attempts += 1
    with open(attempts_path, 'w') as fh:
        fh.write(str(attempts))

    if attempts <= fail_times:
        raise RuntimeError('report data for %s not ready (attempt %d)' %
                           (day, attempts))
    return 'report:%s' % day


def publish(days, fail_time_map=None):
    fail_time_map = fail_time_map or {}
    results = []
    for day in days:
        results.append(generate_report(day, fail_time_map.get(day, 0)))
    return results


def publish_scheduled(day, delay=30, fail_times=0):
    return generate_report.schedule((day, fail_times), delay=delay)


if __name__ == '__main__':
    # Usage: python tasks_old.py 2026-11-20 [2026-11-21 ...]
    days = sys.argv[1:] or ['2026-11-20']
    results = publish(days)
    for day, result in zip(days, results):
        print('enqueued old-name report for %s as %s' % (day, result.id))
