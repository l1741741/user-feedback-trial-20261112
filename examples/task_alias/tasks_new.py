"""The NEW consumer.

The report task has been renamed. The old name is registered as a migration
alias so that messages published by ``tasks_old`` keep working until operators
retire the alias.
"""
import os
import sys
import threading
import time

from huey import SqliteHuey

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alias_config


def _attempts_path(day):
    return os.path.join(alias_config.STATE_DIR, 'attempts-%s' % day)


huey = SqliteHuey(alias_config.QUEUE_NAME, filename=alias_config.DB_PATH,
                  utc=False)


@huey.task(retries=alias_config.RETRIES,
           retry_delay=alias_config.RETRY_DELAY)
def generate_report(day, fail_times=0):
    """The renamed (new) implementation of the daily report."""
    os.makedirs(alias_config.STATE_DIR, exist_ok=True)
    with open(os.path.join(alias_config.STATE_DIR, 'attempts.log'),
              'a') as fh:
        fh.write('new-consumer:%s\n' % day)

    attempts = 0
    if os.path.exists(_attempts_path(day)):
        with open(_attempts_path(day)) as fh:
            attempts = int(fh.read().strip() or 0)
    attempts += 1
    with open(_attempts_path(day), 'w') as fh:
        fh.write(str(attempts))

    if attempts <= fail_times:
        # Spend a moment working, so an operator polling the status sees this
        # message as in-flight rather than only as a queue entry.
        time.sleep(0.6)
        raise RuntimeError('report data for %s not ready (attempt %d)' %
                           (day, attempts))
    return 'report:%s' % day


# Migration entry-point: messages addressed to the old name are executed by
# the new task, keeping their ID, result and remaining retries.
#
# Operators remove this registration (a code deploy) only after
# ``task_alias_status`` reports no queued, scheduled or in-flight old-name
# messages. The environment variable lets the demo show the post-retirement
# deployment without maintaining a second module.
ALIAS_RETIRED = bool(os.environ.get('HUEY_ALIAS_RETIRED'))
if not ALIAS_RETIRED:
    huey.register_task_alias(alias_config.OLD_NAME,
                             generate_report.task_class)


def alias_status():
    return huey.task_alias_status(alias_config.OLD_NAME)


def retire_alias(force=False):
    return huey.unregister_task_alias(alias_config.OLD_NAME, force=force)


if __name__ == '__main__':
    import json
    print(json.dumps(alias_status(), indent=2))
