#!/usr/bin/env python
"""
``huey_diag`` - read-only diagnostics for a SqliteHuey database.

Prints a consistent-snapshot summary of the queue backlog, schedule,
results and control state (revocations, locks, counters) without consuming
or modifying anything. See :mod:`huey.diagnostics` for details.
"""

import argparse
import json
import sys

from huey.diagnostics import EXIT_CODES
from huey.diagnostics import format_report
from huey.diagnostics import inspect_database


def get_option_parser():
    parser = argparse.ArgumentParser(
        prog='huey_diag',
        description='Read-only, consistent-snapshot diagnostics for a '
                    'SqliteHuey database. Never creates, claims or deletes '
                    'anything; task arguments and result values are never '
                    'displayed.')
    parser.add_argument('database',
                        help='path to the sqlite database file')
    parser.add_argument('-q', '--queue', default='huey',
                        help='queue name to inspect (default: huey); only '
                             'this queue is shown when several share one '
                             'database file')
    parser.add_argument('-n', '--limit', type=int, default=20,
                        help='maximum entries listed per section (default: '
                             '20); counts always cover the whole queue')
    parser.add_argument('--offset', type=int, default=0,
                        help='skip this many entries (stable ordering); the '
                             'page shares the snapshot of the counts')
    parser.add_argument('--timeout', type=float, default=5.0,
                        help='seconds to wait on a locked database before '
                             'reporting it (default: 5.0)')
    parser.add_argument('--json', action='store_true', dest='as_json',
                        help='emit the machine-readable report (stable '
                             'field names) instead of text')
    return parser


def diag_main(argv=None):
    parser = get_option_parser()
    options = parser.parse_args(argv)

    report = inspect_database(
        options.database,
        queue=options.queue,
        limit=options.limit,
        offset=options.offset,
        timeout=options.timeout)

    if options.as_json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        sys.stdout.write('\n')
    else:
        sys.stdout.write(format_report(report))
        sys.stdout.write('\n')

    return EXIT_CODES.get(report['status'], 5)


if __name__ == '__main__':
    sys.exit(diag_main())
