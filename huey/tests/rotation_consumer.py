"""
Stand-alone consumer used to prove that a rotation-aware reader drains a
backlog written by other processes.

It imports only the public Huey API and the shared task fixture, drains the
queue, executes each task and records the outcome as JSON on stdout. Rejected
messages are reported by classification; the process exits non-zero if any
message was rejected when ``strict`` is set.

Invoked as::

    python rotation_consumer.py '<json-config>'

Config keys:

    db           SQLite filename.
    keys         key id -> secret mapping for the reader.
    signing_key  key id used if this consumer also enqueues (usually unused).
    salt         signing salt (default ``huey``).
    allow_legacy accept unversioned messages.
    legacy_key_id key id that verifies unversioned messages.
    compression  ``none``, ``gzip`` or ``zlib``.
    limit        maximum tasks to drain (default 100).
    strict       exit non-zero if any message is rejected.
"""

import importlib.util
import json
import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _load_fixture():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'rotation_fixture.py')
    spec = importlib.util.spec_from_file_location(
        'huey.tests.rotation_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv):
    if len(argv) != 2:
        raise SystemExit('expected a single JSON argument')
    config = json.loads(argv[1])

    sys.path.insert(0, REPO_ROOT)
    from huey.api import SqliteHuey
    from huey.exceptions import MessageRejected
    from huey.serializer import SignedSerializer

    serializer_config = {
        'keys': config['keys'],
        'signing_key': config.get('signing_key') or next(iter(config['keys'])),
        'salt': config.get('salt', 'huey'),
        'compression': config.get('compression', 'none') != 'none',
        'use_zlib': config.get('compression') == 'zlib',
    }
    if config.get('allow_legacy'):
        serializer_config['allow_legacy'] = True
        serializer_config['legacy_key_id'] = config['legacy_key_id']

    huey = SqliteHuey('huey', filename=config['db'], utc=False,
                      serializer=SignedSerializer(**serializer_config))
    _load_fixture().register(huey)

    executed, rejected = [], []
    for _ in range(int(config.get('limit', 100))):
        if not huey.pending_count():
            break
        try:
            task = huey.dequeue()
        except MessageRejected as exc:
            rejected.append(exc.reason)
            continue
        if task is None:
            break
        executed.append(huey.execute(task))

    print(json.dumps({'executed': executed, 'rejected': rejected}))
    if rejected and config.get('strict'):
        sys.exit(2)


if __name__ == '__main__':
    main(sys.argv)
