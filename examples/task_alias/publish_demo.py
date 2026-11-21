"""STEP 1 of the demo: publish as the old, not-yet-upgraded application."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alias_config
import tasks_old


def main():
    # A normal report the new consumer will run right away.
    r1 = tasks_old.generate_report('2026-11-20', 0)
    # A report that needs three attempts: the first two fail, so it will be
    # retried across the rename.
    r2 = tasks_old.generate_report('2026-11-21', 2)
    # A report scheduled in the near future, so it is stored with a future
    # ETA under the old name and only becomes runnable after the switch.
    r3 = tasks_old.generate_report.schedule(('2026-11-22', 0), delay=6)

    print('enqueued old-name messages:')
    for label, result in (('report-1', r1), ('report-2 (flaky)', r2)):
        print('  %-18s id=%s' % (label, result.id))
    print('  %-18s id=%s (eta=%s)' % ('report-3 (future)', r3.id, r3.task.eta))
    print('messages were written under: %s' % alias_config.OLD_NAME)

    os.makedirs(alias_config.STATE_DIR, exist_ok=True)
    with open(os.path.join(alias_config.STATE_DIR, 'ids.json'), 'w') as fh:
        json.dump({'report-1': r1.id, 'report-2': r2.id, 'report-3': r3.id},
                  fh)
    tasks_old.huey.storage.close()


if __name__ == '__main__':
    main()
