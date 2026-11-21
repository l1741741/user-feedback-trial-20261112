"""CLI used by the demo to inspect or retire the legacy task-name alias.

Runs in its own process so that its view of the shared SQLite database is
independent from the consumer's.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tasks_new


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--retire', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--result', metavar='TASK_ID')
    args = parser.parse_args()

    if args.result:
        raw = tasks_new.huey.get_raw(args.result, peek=True)
        value = tasks_new.huey.serializer.deserialize(raw)
        print(json.dumps({'id': args.result, 'result': value}))
        return

    if args.retire:
        try:
            removed = tasks_new.retire_alias(force=args.force)
        except Exception as exc:
            print(json.dumps({
                'retired': False,
                'error_type': type(exc).__name__,
                'error': str(exc),
                'status': tasks_new.alias_status(),
            }))
            return 2
        print(json.dumps({'retired': removed,
                          'status': tasks_new.alias_status()}))
        return

    print(json.dumps(tasks_new.alias_status(), indent=2))


if __name__ == '__main__':
    sys.exit(main() or 0)
