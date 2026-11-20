"""
Tests for the explicit error-redaction policy.

These tests use only *fake* secrets (synthetic tokens, passwords and signed
URLs generated inside the test process). No real credentials are ever used.

Guarantee scope (what is asserted here):
* Huey's own public boundaries -- log records emitted by ``huey`` /
  ``huey.consumer`` loggers, the ``SIGNAL_ERROR`` signal, the persisted error
  result (``error`` + ``traceback`` metadata) and the exception delivered to
  ``on_error`` handlers -- are scrubbed consistently.
* The raw exception object used for retry bookkeeping and the post-execute
  hook argument is not mutated, and the business function / retry condition /
  actual call arguments are never rewritten.
* Redaction failures, recursive exception chains, unprintable objects and
  binary arguments degrade to a safe fallback without breaking task
  finalization.
* Sanitized errors survive serialization and can be read back from a real
  storage backend by a separate process.

Explicitly NOT guaranteed (outside the project's control):
* Anything application code writes itself to its own loggers, files or
  external systems (e.g. a task that logs the secret before raising).
* The textual contents of source files referenced by traceback frames and
  any third-party library logging.
* stdout/stderr written by worker processes outside Huey's logging handlers.
"""
import logging
import multiprocessing
import os
import pickle
import shutil
import sys
import tempfile
import time
import unittest

from huey.api import MemoryHuey
from huey.api import SqliteHuey
from huey.exceptions import TaskException
from huey.redaction import ErrorRedactor
from huey.redaction import RedactedError
from huey.redaction import RedactionFailedError
from huey.redaction import REDACTION_PLACEHOLDER
from huey.tests.base import BaseTestCase


# Synthetic secrets -- fake values only.
FAKE_TOKEN = 'fake-token-abcdef123456'
FAKE_PASSWORD = 'fake-password-supersecret'
FAKE_BYTE_SECRET = b'fake-byte-secret-987654'
FAKE_SIGNED_URL = (
    'https://files.example.com/download/report.pdf'
    '?Signature=zzzSIGNATUREzzz&Expires=1700000000&page=7')
FAKE_URL_WITH_CREDENTIALS = (
    'https://svc-user:%s@api.example.internal/v1/object?id=42' % FAKE_PASSWORD)


def make_redactor(**kwargs):
    defaults = dict(
        secrets=(FAKE_TOKEN, FAKE_PASSWORD, FAKE_BYTE_SECRET),
        sensitive_fields=('password', 'token', 'authorization'),
        sensitive_url_params=('Signature',))
    defaults.update(kwargs)
    return ErrorRedactor(**defaults)


class RedactedHuey(MemoryHuey):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('error_redactor', make_redactor())
        super(RedactedHuey, self).__init__(*args, **kwargs)


class _LogCapture(logging.Handler):
    def __init__(self):
        super(_LogCapture, self).__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.name, record.levelname,
                             record.getMessage()))

    def all_text(self):
        return '\n'.join(text for _, _, text in self.records)


@unittest.skipUnless(sys.platform != 'win32', 'requires fork()')
class RedactionBaseTestCase(BaseTestCase):
    def get_huey(self):
        return RedactedHuey(utc=False)

    def setUp(self):
        super(RedactionBaseTestCase, self).setUp()
        self.logs = _LogCapture()
        self.root = logging.getLogger('huey')
        self.root.addHandler(self.logs)
        self.addCleanup(self.root.removeHandler, self.logs)

    def assertNoSecret(self, text):
        self.assertIsInstance(text, str)
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn(FAKE_PASSWORD, text)
        self.assertNotIn('zzzSIGNATUREzzz', text)
        self.assertNotIn('svc-user', text)
        # Byte secret rendered as text must never appear either.
        self.assertNotIn(FAKE_BYTE_SECRET.decode('utf-8'), text)

    def assertNoSecretLeak(self, text):
        # Alias kept for readability at call sites that assert on log output.
        self.assertNoSecret(text)


class TestRedactionUnit(unittest.TestCase):
    """Direct unit tests for the redaction policy itself."""

    def setUp(self):
        self.redactor = make_redactor()

    def test_literal_secret_replaced_everywhere(self):
        out = self.redactor.sanitize_text(
            'auth header token=%s done' % FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertIn(REDACTION_PLACEHOLDER, out)
        self.assertIn('auth header', out)

    def test_sensitive_field_value_redacted_key_retained(self):
        out = self.redactor.sanitize_text(
            'login failed password="%s" x=1' % FAKE_PASSWORD)
        self.assertNotIn(FAKE_PASSWORD, out)
        self.assertIn('password=', out)
        # Non-sensitive neighboring content survives.
        self.assertIn('x=1', out)

    def test_url_userinfo_and_query_redacted(self):
        out = self.redactor.sanitize_text(
            'fetching %s then %s' % (FAKE_URL_WITH_CREDENTIALS,
                                     FAKE_SIGNED_URL))
        self.assertNoSecret(out)
        # Structural URL information is preserved for debugging.
        self.assertIn('api.example.internal', out)
        self.assertIn('/v1/object', out)
        self.assertIn('id=42', out)
        self.assertIn('files.example.com', out)
        self.assertIn('page=7', out)
        self.assertIn('Expires=1700000000', out)

    def test_default_sensitive_query_params(self):
        # Even without explicit configuration, auth-related query params go.
        redactor = ErrorRedactor()
        out = redactor.sanitize_text(
            'https://h.test/p?api_key=LEAK&access_token=LEAK2&ok=1')
        self.assertNotIn('LEAK', out)
        self.assertIn('ok=1', out)

    def test_exception_args_redacted(self):
        exc = ValueError('bad token %s' % FAKE_TOKEN)
        red = self.redactor.redact_exception(exc)
        self.assertIsInstance(red, RedactedError)
        self.assertEqual(red.original_type, 'builtins.ValueError')
        self.assertNoSecret(str(red))
        self.assertIn('ValueError', repr(red))

    def test_custom_exception_string_redacted(self):
        class CustomError(Exception):
            def __str__(self):
                return 'CustomError detail password=%s' % FAKE_PASSWORD

        red = self.redactor.redact_exception(CustomError())
        self.assertNoSecret(str(red))
        self.assertIn('CustomError', red.original_type)

    def test_nested_cause_and_context(self):
        try:
            try:
                raise KeyError(FAKE_TOKEN)
            except Exception as inner:
                raise RuntimeError('outer %s' % FAKE_PASSWORD) from inner
        except Exception as exc:
            red = self.redactor.redact_exception(exc)
        text = str(red)
        self.assertNoSecret(text)
        self.assertIn('caused by', text)
        self.assertIn('KeyError', text)
        # Outer type is preserved on the redacted object's type metadata and
        # repr, while the message carries the sanitized content.
        self.assertEqual(red.original_type, 'builtins.RuntimeError')
        self.assertIn('RuntimeError', repr(red))
        self.assertEqual(red.details['exception_chain'][0],
                         'builtins.RuntimeError')

    def test_recursive_exception_chain(self):
        loop = ValueError('loop %s' % FAKE_TOKEN)
        loop.__cause__ = loop
        loop.__context__ = loop
        red = self.redactor.redact_exception(loop)
        self.assertNoSecret(str(red))
        # No infinite recursion: the self-reference terminates and the chain
        # metadata records the type without looping.
        self.assertEqual(red.details['exception_chain'],
                         ['builtins.ValueError', '<recursive exception>'])

    def test_binary_argument(self):
        red = self.redactor.redact_exception(
            ValueError(b'prefix ' + FAKE_BYTE_SECRET + b' suffix'))
        self.assertNoSecret(str(red))
        # Clean binary data is represented by length, never rendered raw.
        clean = self.redactor.redact_exception(ValueError(b'\x00\x01\xff'))
        self.assertIn('bytes len=3', str(clean))

    def test_unprintable_object_argument(self):
        class Unprintable(object):
            def __str__(self):
                raise RuntimeError('cannot print')
            def __repr__(self):
                raise RuntimeError('cannot repr')

        red = self.redactor.redact_exception(
            ValueError(Unprintable(), FAKE_TOKEN))
        self.assertNoSecret(str(red))

    def test_dict_and_sequence_arguments(self):
        red = self.redactor.redact_exception(Exception({
            'password': FAKE_PASSWORD,
            'nested': {'token': FAKE_TOKEN, 'ok': 'fine'},
            'items': [FAKE_TOKEN, 'plain']}))
        text = str(red)
        self.assertNoSecret(text)
        self.assertIn('plain', text)

    def test_pickle_roundtrip_drops_original(self):
        exc = ValueError('secret %s' % FAKE_TOKEN)
        red = self.redactor.redact_exception(exc)
        self.assertIs(red.__original_exception__, exc)
        restored = pickle.loads(pickle.dumps(red))
        self.assertFalse(hasattr(restored, '__original_exception__'))
        self.assertNoSecret(str(restored))
        self.assertEqual(restored.original_type, 'builtins.ValueError')

    def test_redactor_failure_falls_back_safely(self):
        class BrokenRedactor(ErrorRedactor):
            def _render_exception(self, exc, seen=None, depth=0):
                raise RuntimeError('redactor blew up')

        red = BrokenRedactor(secrets=(FAKE_TOKEN,)).redact_exception(
            ValueError(FAKE_TOKEN))
        self.assertIsInstance(red, RedactionFailedError)
        self.assertNoSecret(str(red))
        # Type name retained even in the fallback.
        self.assertEqual(red.original_type, 'builtins.ValueError')
        # Fallback itself is picklable.
        pickle.loads(pickle.dumps(red))

    def test_idempotent(self):
        once = self.redactor.sanitize_text('x %s y' % FAKE_TOKEN)
        twice = self.redactor.sanitize_text(once)
        self.assertEqual(once, twice)

    def assertNoSecret(self, text):
        for secret in (FAKE_TOKEN, FAKE_PASSWORD, 'zzzSIGNATUREzzz',
                       'svc-user', FAKE_BYTE_SECRET.decode('utf-8')):
            self.assertNotIn(secret, text)


class TestRedactedTaskFailures(RedactionBaseTestCase):
    """End-to-end behavior through Huey's actual execution machinery."""

    def test_final_error_result_is_redacted(self):
        @self.huey.task()
        def fetch(url):
            raise RuntimeError('failed GET %s token=%s' %
                               (url, FAKE_TOKEN))

        result = fetch(FAKE_SIGNED_URL)
        self.assertTrue(self.execute_next() is None)

        exc = self.trap_exception(result)
        self.assertIsInstance(exc, TaskException)
        metadata = exc.metadata
        # Debug info retained.
        self.assertEqual(metadata['task_id'], result.id)
        self.assertEqual(metadata['retries'], 0)
        self.assertIn('RuntimeError', metadata['error'])
        self.assertIn('RuntimeError', metadata['traceback'])
        # Secrets gone from every persisted field.
        self.assertNoSecret(metadata['error'])
        self.assertNoSecret(metadata['traceback'])
        # Code location preserved in the redacted traceback.
        self.assertIn('test_redaction.py', metadata['traceback'])
        self.assertNoSecret(self.logs.all_text())

    def test_retry_then_final_error(self):
        attempts = [0]

        @self.huey.task(retries=2)
        def flaky():
            attempts[0] += 1
            raise ValueError('attempt failed pw=%s' % FAKE_PASSWORD)

        result = flaky()
        # Three failed attempts (initial + two retries), then exhausted.
        for _ in range(3):
            self.assertTrue(self.execute_next() is None)
        self.assertEqual(attempts, [3])
        self.assertEqual(len(self.huey), 0)

        exc = self.trap_exception(result)
        self.assertEqual(exc.metadata['retries'], 0)
        self.assertNoSecret(exc.metadata['error'])
        self.assertNoSecret(exc.metadata['traceback'])
        self.assertNoSecret(self.logs.all_text())

    def test_intermediate_error_also_redacted(self):
        @self.huey.task(retries=1)
        def flaky():
            raise ValueError('boom %s' % FAKE_TOKEN)

        result = flaky()
        self.assertTrue(self.execute_next() is None)  # Fails, retry queued.
        # The intermediate error stored on the first attempt is redacted.
        intermediate = result.get_raw_result(preserve=True)
        self.assertNoSecret(intermediate.metadata['error'])
        self.assertNoSecret(intermediate.metadata['traceback'])

        self.assertTrue(self.execute_next() is None)  # Final failure.
        result.reset()
        exc = self.trap_exception(result)
        self.assertNoSecret(exc.metadata['error'])

    def test_retry_then_success_leaves_no_error(self):
        # When retries eventually succeed, no redacted error is retained and
        # the real success value is stored unchanged.
        state = {'attempts': 0}

        @self.huey.task(retries=2)
        def flaky():
            state['attempts'] += 1
            if state['attempts'] < 3:
                raise ValueError('attempt %s %s' %
                                 (state['attempts'], FAKE_TOKEN))
            return 'success-value'

        result = flaky()
        # Two failed attempts (each re-enqueued), then the third succeeds.
        self.assertTrue(self.execute_next() is None)
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(self.execute_next(), 'success-value')

        self.assertEqual(result.get(), 'success-value')
        self.assertEqual(self.huey.result_count(), 0)
        self.assertEqual(len(self.huey), 0)
        # The intermediate failure logs were nonetheless scrubbed.
        self.assertNoSecret(self.logs.all_text())
        self.assertIn('line', self.logs.all_text())

    def test_business_function_and_arguments_not_rewritten(self):
        seen = {}

        @self.huey.task(retries=1)
        def use_secret(token, url):
            seen['token'] = token
            seen['url'] = url
            raise RuntimeError('using %s' % token)

        result = use_secret(FAKE_TOKEN, url=FAKE_SIGNED_URL)
        # Before execution, the queued message still carries the real
        # arguments (redaction applies to error output, not invocation).
        queued_task = self.huey.dequeue()
        self.assertEqual(queued_task.args, (FAKE_TOKEN,))
        self.assertEqual(queued_task.kwargs, {'url': FAKE_SIGNED_URL})

        # Re-enqueue the untouched task and run both attempts.
        self.huey.enqueue(queued_task)
        self.assertTrue(self.execute_next() is None)
        # The task function received the intact, original arguments.
        self.assertEqual(seen['token'], FAKE_TOKEN)
        self.assertEqual(seen['url'], FAKE_SIGNED_URL)
        self.assertTrue(self.execute_next() is None)

        exc = self.trap_exception(result)
        self.assertNoSecret(exc.metadata['error'])

    def test_on_error_handler_receives_redacted_exception(self):
        received = []

        @self.huey.task()
        def failing():
            raise RuntimeError('callback secret %s' % FAKE_PASSWORD)

        @self.huey.task()
        def error_handler(err):
            received.append(err)
            return 'handled'

        task = failing.s().error(error_handler)
        self.huey.enqueue(task)
        self.assertTrue(self.execute_next() is None)  # Failing task.
        self.assertEqual(len(self.huey), 1)
        self.assertTrue(self.execute_next() == 'handled')  # Error handler.

        self.assertEqual(len(received), 1)
        err = received[0]
        self.assertIsInstance(err, RedactedError)
        self.assertNoSecret(repr(err))
        self.assertEqual(err.original_type, 'builtins.RuntimeError')

    def test_error_callback_matches_result_policy(self):
        received = []

        @self.huey.task()
        def failing():
            raise ValueError('shared %s' % FAKE_TOKEN)

        @self.huey.task()
        def error_handler(err):
            received.append(str(err))

        result = self.huey.enqueue(failing.s().error(error_handler))
        self.assertTrue(self.execute_next() is None)
        self.assertTrue(self.execute_next() is None)

        exc = self.trap_exception(result)
        # The handler's view and the stored result expose the same boundary.
        self.assertNoSecret(received[0])
        self.assertIn(REDACTION_PLACEHOLDER, received[0])
        self.assertNoSecret(exc.metadata['error'])

    def test_signal_error_receives_redacted_exception(self):
        import huey.signals as S
        signals_seen = []

        @self.huey.signal(S.SIGNAL_ERROR)
        def on_error_signal(signal, task, exc):
            signals_seen.append(exc)

        @self.huey.task()
        def failing():
            raise RuntimeError('signal leak %s' % FAKE_TOKEN)

        failing()
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(len(signals_seen), 1)
        self.assertIsInstance(signals_seen[0], RedactedError)
        self.assertNoSecret(str(signals_seen[0]))

    def test_post_execute_hook_keeps_original_exception(self):
        hooks = []

        @self.huey.post_execute()
        def hook(task, value, exception):
            hooks.append(exception)

        @self.huey.task()
        def failing():
            raise ValueError('hook %s' % FAKE_TOKEN)

        failing()
        self.assertTrue(self.execute_next() is None)
        self.assertEqual(len(hooks), 1)
        # In-process extension hook deliberately keeps the raw exception.
        self.assertIsInstance(hooks[0], ValueError)
        self.assertNotIsInstance(hooks[0], RedactedError)

    def test_original_exception_not_mutated(self):
        captured = {}

        @self.huey.task()
        def failing():
            err = ValueError('immutable %s' % FAKE_TOKEN)
            captured['err'] = err
            raise err

        failing()
        self.assertTrue(self.execute_next() is None)
        # The live exception object retains its original, unredacted args.
        self.assertIn(FAKE_TOKEN, str(captured['err']))
        self.assertNotIsInstance(captured['err'], RedactedError)

    def test_url_credentials_in_exception_redacted(self):
        @self.huey.task()
        def fetch():
            raise RuntimeError('cannot reach %s' %
                               FAKE_URL_WITH_CREDENTIALS)

        result = fetch()
        self.assertTrue(self.execute_next() is None)
        exc = self.trap_exception(result)
        self.assertNoSecret(exc.metadata['error'])
        # Host and path remain available for diagnosis.
        self.assertIn('api.example.internal', exc.metadata['error'])
        self.assertIn('/v1/object', exc.metadata['error'])

    def test_unprintable_and_binary_in_real_task(self):
        class Weird(object):
            def __repr__(self):
                raise RuntimeError('nope')

        @self.huey.task()
        def failing():
            raise ValueError(Weird(), FAKE_BYTE_SECRET)

        result = failing()
        # Must finalize normally (error stored, task drained) despite weird
        # arguments.
        self.assertTrue(self.execute_next() is None)
        exc = self.trap_exception(result)
        self.assertNoSecret(exc.metadata['error'])
        self.assertNoSecret(exc.metadata['traceback'])

    def test_recursive_exception_in_real_task(self):
        @self.huey.task()
        def failing():
            err = ValueError('cycle %s' % FAKE_TOKEN)
            err.__context__ = err
            raise err

        result = failing()
        self.assertTrue(self.execute_next() is None)
        exc = self.trap_exception(result)
        self.assertNoSecret(exc.metadata['error'])
        self.assertNoSecret(exc.metadata['traceback'])

    def test_broken_redactor_does_not_break_finalization(self):
        class BrokenRedactor(ErrorRedactor):
            def redact_exception(self, exc):
                raise RuntimeError('redactor totally broken')

        huey = MemoryHuey(utc=False, error_redactor=BrokenRedactor(
            secrets=(FAKE_TOKEN,)))

        @huey.task()
        def failing():
            raise ValueError('x %s' % FAKE_TOKEN)

        result = failing()
        # Task finalization (result storage, queue drain) still completes.
        self.assertTrue(huey.execute(huey.dequeue()) is None)
        self.assertEqual(len(huey), 0)
        exc_obj = None
        try:
            result.get()
        except TaskException as e:
            exc_obj = e
        self.assertIsNotNone(exc_obj)
        self.assertNotIn(FAKE_TOKEN, exc_obj.metadata['error'])
        self.assertIn('ValueError', exc_obj.metadata['error'])


class TestImmediateModeRedaction(unittest.TestCase):
    """Redaction also applies when tasks run synchronously in immediate mode."""

    def test_immediate_error_is_redacted(self):
        huey = MemoryHuey(utc=False, immediate=True,
                          error_redactor=make_redactor())

        @huey.task()
        def failing():
            raise RuntimeError('immediate %s' % FAKE_TOKEN)

        result = failing()  # Runs inline via enqueue -> execute.
        with self.assertRaises(TaskException) as cm:
            result.get()
        self.assertNotIn(FAKE_TOKEN, cm.exception.metadata['error'])
        self.assertIn('RuntimeError', cm.exception.metadata['error'])


class TestConsumerLoggingRedaction(RedactionBaseTestCase):
    """The real consumer worker path must not log raw exception text."""

    def test_worker_logs_are_redacted(self):
        @self.huey.task()
        def failing():
            raise RuntimeError('worker leak %s' % FAKE_PASSWORD)

        with self.consumer_context(workers=2):
            failing()
            # Wait for the result to land.
            deadline = time.monotonic() + 10
            while (time.monotonic() < deadline and
                   self.huey.result_count() == 0):
                time.sleep(0.05)

        self.assertGreaterEqual(self.huey.result_count(), 1)
        self.assertNoSecret(self.logs.all_text())
        # The redacted log still identifies the exception category.
        self.assertIn('RuntimeError', self.logs.all_text())


class TestDefaultBehaviorUnchanged(BaseTestCase):
    """Without a redactor configured, behavior is byte-for-byte compatible."""

    def test_no_redactor_passes_raw_exception_through(self):
        @self.huey.task()
        def failing():
            raise ValueError('raw message')

        result = failing()
        self.assertTrue(self.execute_next() is None)
        exc = self.trap_exception(result)
        # No redactor: the raw default repr/exception text is unchanged.
        self.assertEqual(exc.metadata['error'], "ValueError('raw message')")
        self.assertIn("ValueError('raw message')",
                      exc.metadata['traceback'])


def _mp_build_huey(db_path, queue_name):
    # Both parent and worker build equivalent instances against the same
    # SQLite file, including the same redaction policy. The module-level task
    # class below is registered on each instance by name.
    huey = SqliteHuey(queue_name, filename=db_path,
                      error_redactor=make_redactor())
    huey._registry.register(mp_cross_process_task.task_class)
    return huey


def _mp_task_body():
    raise RuntimeError('mp failure %s %s' % (FAKE_TOKEN, FAKE_SIGNED_URL))


# A placeholder huey only used to construct the module-level task class, so
# the forked worker's task registry contains an identically-named task. The
# storage backend is irrelevant here, so use in-memory storage to avoid
# creating a file at import time.
_mp_bootstrap = MemoryHuey('mpredact-bootstrap')
mp_cross_process_task = _mp_bootstrap.task(name='mp_cross_process_task')(
    _mp_task_body)


def _multiprocess_worker(db_path, queue_name, ready_counter):
    """Runs in a separate process: consume the queued task, store result."""
    huey = _mp_build_huey(db_path, queue_name)
    consumer = huey.create_consumer(
        workers=1, worker_type='thread',
        initial_delay=0.001, max_delay=0.001,
        check_worker_health=False, periodic=False)
    consumer.start()
    try:
        deadline = time.monotonic() + 15
        while huey.pending_count() and time.monotonic() < deadline:
            time.sleep(0.05)
        # Give the worker a moment to flush the error result.
        time.sleep(0.3)
    finally:
        consumer.stop(graceful=True)
    ready_counter.append(1)


class TestMultiprocessRedaction(unittest.TestCase):
    """A separate process must read back only sanitized data."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, 'mp-huey.db')

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @unittest.skipIf(sys.platform == 'win32', 'requires fork()')
    def test_separate_process_reads_redacted_result(self):
        huey = _mp_build_huey(self.db_path, 'mpredact')
        enqueued = huey.enqueue(mp_cross_process_task.s())
        task_id = enqueued.id

        ctx = multiprocessing.get_context('fork')
        manager = ctx.Manager()
        ready = manager.list()
        proc = ctx.Process(
            target=_multiprocess_worker,
            args=(self.db_path, 'mpredact', ready))
        proc.start()
        proc.join(timeout=30)
        self.assertEqual(proc.exitcode, 0)
        self.assertEqual(list(ready), [1])

        # Read the persisted error from a *fresh* Huey/connection in this
        # process, proving the bytes at rest contain no secret.
        reader = SqliteHuey('mpredact', filename=self.db_path)
        raw = reader.storage.peek_data(task_id)
        from huey.constants import EmptyData
        self.assertIsNot(raw, EmptyData)
        # The serialized blob itself must not contain the secret bytes.
        self.assertNotIn(FAKE_TOKEN.encode('utf-8'), raw)
        self.assertNotIn(b'zzzSIGNATUREzzz', raw)

        from huey.api import Result, Task
        handle = Result(reader, Task(id=task_id))
        metadata = self.trap_metadata(handle)
        self.assertNotIn(FAKE_TOKEN, metadata['error'])
        self.assertNotIn('zzzSIGNATUREzzz', metadata['traceback'])
        self.assertIn('RuntimeError', metadata['error'])
        self.assertEqual(metadata['task_id'], task_id)

    @staticmethod
    def trap_metadata(result_handle):
        try:
            result_handle.get()
        except TaskException as exc:
            return exc.metadata
        raise AssertionError('expected a TaskException error result')


class TestSerializationFailureModes(unittest.TestCase):
    """Redaction must hold even when normal serialization would struggle."""

    def setUp(self):
        self.redactor = make_redactor()

    def test_redacted_exception_is_picklable_with_live_original(self):
        class LiveError(Exception):
            def __init__(self):
                super(LiveError, self).__init__('live %s' % FAKE_TOKEN)
                self.unpicklable = lambda: None  # Cannot pickle a lambda.

        red = self.redactor.redact_exception(LiveError())
        # The redacted object itself pickles even though the original cannot.
        restored = pickle.loads(pickle.dumps(red))
        self.assertNotIn(FAKE_TOKEN, str(restored))

    def test_traceback_text_redaction_never_raises(self):
        from huey.redaction import redact_traceback_text

        class ExplodingRedactor(ErrorRedactor):
            def sanitize_text(self, text):
                raise RuntimeError('boom')

        out = redact_traceback_text(
            ExplodingRedactor(secrets=(FAKE_TOKEN,)),
            'some traceback %s' % FAKE_TOKEN)
        # Falls back to a generic marker rather than raising or leaking.
        self.assertNotIn(FAKE_TOKEN, out)

    def test_sanitized_error_result_dict_is_fully_picklable(self):
        huey = MemoryHuey(utc=False, error_redactor=self.redactor)

        class LiveError(Exception):
            def __init__(self):
                super(LiveError, self).__init__('dict %s' % FAKE_TOKEN)
                self.lock = multiprocessing.Lock()  # Not always picklable.

        err = LiveError()
        try:
            raise err
        except LiveError:
            public = huey.redact_exception(err)
            built = huey.build_error_result(
                type('T', (), {'id': 't1', 'retries': 0})(), public)
        # The whole error metadata dict round-trips through the serializer.
        blob = huey.serializer.serialize(built)
        restored = huey.serializer.deserialize(blob)
        self.assertNotIn(FAKE_TOKEN, restored['error'])
        self.assertNotIn(FAKE_TOKEN, restored['traceback'])


if __name__ == '__main__':
    unittest.main()
