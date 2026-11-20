"""
Stand-alone publisher used to simulate a process running the *old* release,
before the rotation-aware serializer existed.

It is deliberately independent of ``huey.serializer``'s new envelope code:

* ``legacy`` mode reproduces byte-for-byte the pre-rotation wire format
  (pickle, HMAC-SHA1 over ``sha1(salt + secret)``, ``b':'`` separator, optional
  gzip/zlib applied *after* signing).
* ``envelope`` mode uses the rotation-aware serializer from this checkout, and
  is used to simulate a publisher that has already been upgraded.

Invoked as::

    python rotation_publisher.py '<json-config>'

The JSON config supports:

    db          SQLite filename.
    mode        ``legacy`` or ``envelope``.
    task        ``add``, ``record`` or ``noop``.
    args        list of positional arguments for the task.
    then        optional list of [task, args] for an on_complete successor.
    count       number of copies to enqueue (default 1).
    salt        signing salt (default ``huey``).
    secret      secret for ``legacy`` mode.
    keys        key id -> secret mapping for ``envelope`` mode.
    signing_key key id used to sign in ``envelope`` mode.
    legacy      for ``envelope`` read side; unused when publishing.
    compression ``none``, ``gzip`` or ``zlib``.
    result_for  optional task id; stores a signed result under that id.
    result      value to store when ``result_for`` is set.
"""

import gzip
import hashlib
import hmac
import importlib.util
import json
import os
import pickle
import sys
import zlib


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _load_fixture():
    # Load the shared task module by path so importing it does not execute
    # ``huey.tests.__init__`` (which pulls in every test module). The module
    # name must match the test process exactly, because task identifiers are
    # ``<module>.<function>``.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'rotation_fixture.py')
    spec = importlib.util.spec_from_file_location(
        'huey.tests.rotation_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_signed_bytes(message, secret, salt, compression):
    key = hashlib.sha1(salt.encode('utf8') + secret.encode('utf8')).digest()
    data = pickle.dumps(message)
    signature = hmac.new(key, msg=data,
                         digestmod=hashlib.sha1).hexdigest().encode('utf8')
    data = data + b':' + signature
    if compression == 'gzip':
        data = gzip.compress(data)
    elif compression == 'zlib':
        data = zlib.compress(data)
    return data


def build_message(registry, task_name, args, then=None):
    tasks = registry['tasks']
    if task_name not in tasks:
        raise SystemExit('unknown task %r' % task_name)
    task = tasks[task_name].s(*args)
    if then:
        then_name, then_args = then
        task = task.then(tasks[then_name], *then_args)
    return registry['registry'].create_message(task), task.id


def main(argv):
    if len(argv) != 2:
        raise SystemExit('expected a single JSON argument')
    config = json.loads(argv[1])

    sys.path.insert(0, REPO_ROOT)
    from huey.api import SqliteHuey

    huey = SqliteHuey('huey', filename=config['db'])
    fixture = _load_fixture()
    wrappers = fixture.register(huey)
    tasks = dict(zip(('add', 'record', 'noop'), wrappers))
    registry = {'tasks': tasks, 'registry': huey._registry}

    secret = config.get('secret')
    salt = config.get('salt', 'huey')
    compression = config.get('compression', 'none')
    mode = config['mode']

    if mode == 'envelope':
        sys.path.insert(0, REPO_ROOT)
        from huey.serializer import SignedSerializer
        serializer = SignedSerializer(
            keys=config['keys'],
            signing_key=config['signing_key'],
            salt=salt,
            compression=compression != 'none',
            use_zlib=compression == 'zlib',
            allow_legacy=config.get('legacy', False),
            legacy_key_id=config.get('legacy_key_id'))
        sign = serializer.serialize
    elif mode == 'legacy':
        def sign(value):
            return legacy_signed_bytes(value, secret, salt, compression)
    else:
        raise SystemExit('unknown mode %r' % mode)

    message, task_id = build_message(
        registry, config['task'], config.get('args', ()),
        config.get('then'))

    count = int(config.get('count', 1))
    for _ in range(count):
        huey.storage.enqueue(sign(message), config.get('priority'))

    if 'result_for' in config:
        value = config.get('result')
        huey.storage.put_data(config['result_for'], sign(value),
                              is_result=True)

    print(json.dumps({'task_id': task_id}))


if __name__ == '__main__':
    main(sys.argv)
