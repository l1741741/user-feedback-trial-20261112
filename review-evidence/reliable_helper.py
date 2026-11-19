"""
Helper process used by the reliable-mode subprocess tests.

This script runs as a real, separate process so tests exercise process death,
lease takeover, renewal, and retry handoff across processes rather than only
in-process threads.

Usage::

    python -m huey.tests.reliable_helper <mode> <db> <lease_seconds>

Modes:

``consume``
    Run a consumer until signalled. Writes task markers to ``<db>.log`` and
    its pid to ``<db>.pid``.

``crash``
    Claim one delivery, announce the claim, then ``os._exit()`` without
    acknowledging -- simulating a consumer killed mid-task.

``exit-after-claim``
    Like ``crash`` but exits cleanly (``sys.exit``) after writing the claim.

``reclaim``
    Claim whatever is currently available, log what it sees, and ack it.
"""
import json
import os
import sys
import time


def build_huey(db):
    from huey import SqliteHuey
    from huey.tests import reliable_tasks
    huey = SqliteHuey('rel', filename=db, utc=False)
    reliable_tasks.configure(huey, db + '.log')
    return huey


def consumer_options(lease_seconds):
    return dict(workers=1, reliable=True, lease_seconds=lease_seconds,
                initial_delay=0.01, max_delay=0.02, check_worker_health=False,
                periodic=False)


def main(argv):
    mode, db, lease_seconds = argv[1], argv[2], float(argv[3])
    huey = build_huey(db)

    if mode == 'consume':
        with open(db + '.pid', 'w') as fh:
            fh.write(str(os.getpid()))
        huey.create_consumer(**consumer_options(lease_seconds)).run()
        return

    if mode in ('crash', 'exit-after-claim'):
        delivery = huey.claim_lease(lease_seconds)
        record = {'claimed': delivery is not None}
        if delivery is not None:
            record.update(task=delivery.task.id, attempt=delivery.attempt,
                          ticket=delivery.ticket)
        with open(db + '.claim', 'w') as fh:
            json.dump(record, fh)
        if mode == 'crash':
            os._exit(17)
        sys.exit(0)

    if mode == 'reclaim':
        claimed = []
        while True:
            delivery = huey.claim_lease(lease_seconds)
            if delivery is None:
                break
            claimed.append({'task': delivery.task.id,
                            'attempt': delivery.attempt})
            delivery.ack()
        with open(db + '.reclaim', 'w') as fh:
            json.dump(claimed, fh)
        return

    raise SystemExit('unknown mode %r' % mode)


if __name__ == '__main__':
    main(sys.argv)
