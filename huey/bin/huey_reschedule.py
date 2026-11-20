#!/usr/bin/env python
"""
Command-line entrypoint for adjusting queued / scheduled tasks in place.

Unlike ``Result.reschedule`` (which revokes a task and enqueues a replacement
with a new task id), these commands rewrite the existing delivery and keep the
task id, so the original :py:class:`Result` handle still refers to it.

Examples::

    # Move a task that is scheduled for later to run as soon as possible.
    huey_reschedule myapp.huey --id 0d4f... --delay 0

    # Push a queued task back by ten minutes and raise its priority.
    huey_reschedule myapp.huey --id 0d4f... --delay 600 --priority 10

    # Show what would happen, without touching the queue.
    huey_reschedule myapp.huey --id 0d4f... --eta 2026-11-21T09:00:00 --dry-run

    # Adjust several tasks, reporting per-id failures.
    huey_reschedule myapp.huey --id a,b,c --delta 300

Only deliveries that are still in the queue or schedule can be adjusted. If a
worker has already claimed the task, the command reports that it can no longer
be adjusted. A ``--operation-id`` makes a retried command safe: replaying the
same id returns the earlier outcome instead of moving the task again.
"""
import json
import optparse
import sys

from huey.exceptions import HueyException
from huey.reschedule import UNCHANGED
from huey.reschedule import normalize_eta
from huey.utils import load_class


def err(s):
    sys.stderr.write('\033[91m%s\033[0m\n' % s)


def load_huey(path):
    try:
        return load_class(path)
    except Exception:
        import os
        cur_dir = os.getcwd()
        if cur_dir not in sys.path:
            sys.path.insert(0, cur_dir)
            return load_huey(path)
        err('Error importing %s' % path)
        raise


def get_parser():
    parser = optparse.OptionParser(
        usage='%prog path.to.huey_instance --id TASK_ID[,TASK_ID...] '
              '[options]',
        description='Adjust queued or scheduled tasks in place, preserving '
                    'their task ids.')
    parser.add_option('-i', '--id', dest='task_ids', metavar='TASK_ID',
                      help='task id(s) to adjust, comma-separated.')
    parser.add_option('-e', '--eta', dest='eta', metavar='ISO8601',
                      help='new absolute execution time. A time in the past '
                           '(or now) moves the task to the front of the '
                           'queue.')
    parser.add_option('-d', '--delay', dest='delay', metavar='SECONDS',
                      type='float',
                      help='run again after this many seconds (0 = now).')
    parser.add_option('-p', '--priority', dest='priority', metavar='PRIORITY',
                      type='float', help='new priority (higher runs first).')
    parser.add_option('-o', '--operation-id', dest='operation_id',
                      metavar='ID',
                      help='stable identifier for this adjustment. Reusing it '
                           'returns the earlier outcome instead of applying '
                           'the change twice.')
    parser.add_option('-n', '--dry-run', dest='dry_run', action='store_true',
                      default=False,
                      help='preview each adjustment without modifying the '
                           'queue or schedule.')
    parser.add_option('-j', '--json', dest='json_output', action='store_true',
                      default=False, help='emit machine-readable JSON.')
    return parser


def _format_preview(p):
    if p.reschedulable:
        where = 'queue (now)' if p.location == 'queue' else (
            'schedule (eta=%s)' % p.eta.isoformat() if p.eta else 'schedule')
        priority = '' if p.priority is None else ' priority=%s' % p.priority
        return '%s: OK -> %s%s' % (p.task_id, where, priority)
    return '%s: NOT RESCHEDULABLE (%s)' % (p.task_id, p.reason)


def _format_batch(batch):
    lines = []
    for r in batch.results:
        lines.append('%s: %s -> %s priority=%s eta=%s' % (
            r.task_id, r.status, r.location,
            '' if r.priority is None else r.priority,
            '' if r.eta is None else r.eta.isoformat()))
    for e in batch.errors:
        reason = getattr(e.error, 'reason', None)
        label = reason or type(e.error).__name__
        lines.append('%s: FAILED (%s) %s' % (e.task_id, label, e.error))
    return lines


def main(argv=None):
    parser = get_parser()
    options, args = parser.parse_args(argv)
    if not args:
        err('Error: missing import path to `Huey` instance')
        err('Example: huey_reschedule app.queue.huey_instance --id ... --eta ...')
        return 1
    if not options.task_ids:
        err('Error: --id is required')
        return 1
    if options.eta is not None and options.delay is not None:
        err('Error: specify only one of --eta / --delay')
        return 1
    if (options.eta is None and options.delay is None and
            options.priority is None):
        err('Error: specify --eta, --delay or --priority')
        return 1

    try:
        huey_instance = load_huey(args[0])
    except Exception:
        return 1

    task_ids = [t.strip() for t in options.task_ids.split(',') if t.strip()]
    if not task_ids:
        err('Error: --id is empty')
        return 1
    if len(task_ids) > 1 and options.operation_id:
        err('Error: --operation-id applies to a single task id only; use '
            'separate invocations if these are distinct changes')
        return 1

    eta = delay = None
    try:
        if options.eta is not None:
            eta = _parse_datetime(options.eta)
        else:
            delay = options.delay
        # Validate once up front so bad time/priority values are rejected
        # before any task is touched.
        normalize_eta(huey_instance, eta, delay)
    except ValueError as exc:
        err('Error: %s' % exc)
        return 1

    priority = options.priority

    if options.dry_run:
        previews = []
        failed = False
        for task_id in task_ids:
            try:
                previews.append(huey_instance.preview_reschedule(
                    task_id, eta=eta, delay=delay, priority=priority))
            except HueyException as exc:
                failed = True
                if options.json_output:
                    previews.append({'task_id': task_id, 'error': str(exc)})
                else:
                    err('%s: ERROR %s' % (task_id, exc))
        if options.json_output:
            print(json.dumps([p.as_dict() if hasattr(p, 'as_dict') else p
                              for p in previews], indent=2, default=str))
        else:
            for p in previews:
                if hasattr(p, 'reschedulable'):
                    print(_format_preview(p))
        return 2 if failed else 0

    if len(task_ids) == 1:
        try:
            result = huey_instance.reschedule(
                task_ids[0], eta=eta, delay=delay, priority=priority,
                operation_id=options.operation_id)
        except HueyException as exc:
            if options.json_output:
                print(json.dumps({'error': str(exc)}, indent=2))
            else:
                err('Error: %s' % exc)
            return 2
        if options.json_output:
            print(json.dumps(result.as_dict(), indent=2, default=str))
        else:
            print('%s: %s -> %s (priority=%s, eta=%s)' % (
                result.task_id, result.status, result.location,
                '' if result.priority is None else result.priority,
                '' if result.eta is None else result.eta.isoformat()))
        return 0

    batch = huey_instance.reschedule_many(
        task_ids, eta=eta, delay=delay, priority=priority,
        operation_id_prefix=options.operation_id)
    if options.json_output:
        print(json.dumps(batch.as_dict(), indent=2, default=str))
    else:
        for line in _format_batch(batch):
            print(line)
    return 0 if batch.ok() else 2


def _parse_datetime(value):
    import datetime
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError('invalid --eta value %r (want ISO-8601, e.g. '
                         '2026-11-21T09:00:00)' % value)


if __name__ == '__main__':
    sys.exit(main())
