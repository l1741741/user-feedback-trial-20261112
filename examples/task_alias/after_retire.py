"""Demonstrate the retired mapping actually rejects stale old-name messages.

Called at the end of the demo, after the alias has been retired. A stale
producer -- for example an old deployment that has not been upgraded yet --
writes one more message under the old name. A new consumer (which no longer
has the mapping) cannot deserialize it, which is the intended failure mode
once operators have confirmed the migration is complete.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# This process represents the deployment after the mapping was closed: the
# alias registration has been removed from the code.
os.environ['HUEY_ALIAS_RETIRED'] = '1'
import tasks_new
import tasks_old


def main():
    # A stale old-style publisher still writes the old name.
    tasks_old.generate_report('2026-11-23', 0)
    tasks_old.huey.storage.close()

    try:
        tasks_new.huey.dequeue()
    except Exception as exc:
        print('dequeue with retired alias raised %s: %s' %
              (type(exc).__name__, exc))
        tasks_new.huey.storage.close()
        return 0
    print('ERROR: stale message was deserialized without an alias')
    tasks_new.huey.storage.close()
    return 1


if __name__ == '__main__':
    sys.exit(main())
