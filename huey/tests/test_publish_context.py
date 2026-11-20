import datetime
import logging
import pickle
import threading

from huey import MemoryHuey
from huey import PublishContext
from huey import ContextLogFilter
from huey import ContextValidationError
from huey import get_current_context
from huey import publish_context
from huey.api import Task
from huey.registry import Message
from huey.serializer import Serializer
from huey.tests.base import BaseTestCase


def ctx(tenant='acme', correlation_id='cid-1', fields=None):
    return PublishContext(tenant, correlation_id=correlation_id, fields=fields)


class TestPublishContextValidation(BaseTestCase):
    def test_basic_construction_and_immutability(self):
        c = ctx(fields={'region': 'us-east'})
        self.assertEqual(c.tenant, 'acme')
        self.assertEqual(c.correlation_id, 'cid-1')
        self.assertEqual(c.fields, {'region': 'us-east'})

        # Immutable.
        self.assertRaises(AttributeError, setattr, c, 'tenant', 'other')

        # The fields accessor returns a copy, mutating it does not leak.
        f = c.fields
        f['region'] = 'tampered'
        self.assertEqual(c.fields['region'], 'us-east')

    def test_correlation_id_auto_generated(self):
        c = PublishContext('acme')
        self.assertTrue(isinstance(c.correlation_id, str))
        self.assertTrue(len(c.correlation_id) == 32)

    def test_invalid_tenant(self):
        self.assertRaises(ContextValidationError, PublishContext, 123)
        self.assertRaises(ContextValidationError, PublishContext, '')
        self.assertRaises(ContextValidationError, PublishContext, 'x' * 65)

    def test_invalid_correlation_id(self):
        self.assertRaises(ContextValidationError, PublishContext, 't',
                          correlation_id=123)
        self.assertRaises(ContextValidationError, PublishContext, 't',
                          correlation_id='')
        self.assertRaises(ContextValidationError, PublishContext, 't',
                          correlation_id='x' * 129)

    def test_fields_must_be_flat_dict_of_strings(self):
        self.assertRaises(ContextValidationError, PublishContext, 't',
                          fields=[('a', 'b')])
        self.assertRaises(ContextValidationError, PublishContext, 't',
                          fields={1: 'v'})
        self.assertRaises(ContextValidationError, PublishContext, 't',
                          fields={'': 'v'})
        # Arbitrary objects are rejected.
        self.assertRaises(ContextValidationError, PublishContext, 't',
                          fields={'obj': object()})
        self.assertRaises(ContextValidationError, PublishContext, 't',
                          fields={'nested': {'a': 1}})

    def test_fields_accept_numbers_and_booleans_as_strings(self):
        c = PublishContext('t', fields={'n': 1, 'f': 1.5, 'b': True})
        self.assertEqual(c.fields, {'n': '1', 'f': '1.5', 'b': 'true'})

    def test_field_limits(self):
        # Too many fields.
        self.assertRaises(ContextValidationError,
                          PublishContext, 't',
                          fields={('k%d' % i): 'v' for i in range(17)})
        # Key too long.
        self.assertRaises(ContextValidationError,
                          PublishContext, 't',
                          fields={'k' * 65: 'v'})
        # Value too long.
        self.assertRaises(ContextValidationError,
                          PublishContext, 't',
                          fields={'k': 'v' * 257})

    def test_sensitive_keys_rejected(self):
        for bad_key in ('password', 'user_password', 'api_key', 'token',
                        'Authorization', 'cookie', 'credential', 'secret',
                        'private_key_hash'):
            self.assertRaises(ContextValidationError,
                              PublishContext, 't',
                              fields={bad_key: 'x'})

    def test_authorization_header_rejected(self):
        for bad_value in ('Bearer abc123', 'Basic dXNlcjpwYXNz',
                         'authorization: Bearer xyz',
                         '  digest blah'):
            self.assertRaises(ContextValidationError,
                              PublishContext, 't',
                              fields={'trace': bad_value})

    def test_repr_and_dict_never_contain_more_than_stored(self):
        c = ctx(fields={'region': 'us'})
        self.assertIn('acme', repr(c))
        self.assertEqual(c.to_dict(),
                         {'correlation_id': 'cid-1', 'tenant': 'acme',
                          'region': 'us'})

    def test_wire_roundtrip(self):
        c = ctx(fields={'region': 'us'})
        restored = PublishContext.from_wire(c.to_wire())
        self.assertEqual(restored, c)

    def test_from_wire_revalidates_untrusted_data(self):
        self.assertRaises(ContextValidationError,
                          PublishContext.from_wire, ('cid', '', {}))
        self.assertRaises(ContextValidationError,
                          PublishContext.from_wire, ('cid', 't', 'nope'))
        self.assertRaises(ContextValidationError,
                          PublishContext.from_wire, ('cid', 't'))


class TestContextVar(BaseTestCase):
    def test_publish_context_sets_and_restores(self):
        self.assertIsNone(get_current_context())
        c1 = ctx('t1', 'c1')
        c2 = ctx('t2', 'c2')
        with publish_context(c1):
            self.assertIs(get_current_context(), c1)
            with publish_context(c2):
                self.assertIs(get_current_context(), c2)
            # Inner exit restores the outer value, not None.
            self.assertIs(get_current_context(), c1)
        self.assertIsNone(get_current_context())

    def test_publish_context_requires_instance(self):
        self.assertRaises(ContextValidationError, publish_context, {'tenant': 'x'})

    def test_thread_isolation(self):
        # ContextVars are not copied into plain threading.Thread workers, so
        # a worker starts with no context and its bindings never leak back to
        # the caller thread.
        c = ctx('thread-main', 'cid-main')
        seen = {}

        def worker():
            seen['inside'] = get_current_context()
            with publish_context(ctx('thread-worker', 'cid-w')):
                seen['bound'] = get_current_context().tenant
            seen['after'] = get_current_context()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        self.assertIsNone(seen['inside'])
        self.assertEqual(seen['bound'], 'thread-worker')
        self.assertIsNone(seen['after'])
        # The worker never touched this thread's context.
        self.assertIsNone(get_current_context())

        # And when the caller thread does bind a context, it stays local to it.
        with publish_context(c):
            t = threading.Thread(target=worker)
            t.start()
            t.join()
            self.assertIs(get_current_context(), c)
        self.assertIsNone(get_current_context())


class TestTaskContextEnqueue(BaseTestCase):
    def test_explicit_context_serializes_through_queue(self):
        @self.huey.task()
        def task_a():
            return get_current_context().tenant

        c = ctx('tenant-a', 'cid-a')
        r = task_a(task_context=c)

        # The context travels via the serialized message, not via globals.
        dequeued = self.huey.dequeue()
        self.assertEqual(dequeued.task_context, c)
        self.assertEqual(self.huey.execute(dequeued), 'tenant-a')

        # Context is restored after execution.
        self.assertIsNone(get_current_context())

    def test_ambient_context_attached_on_enqueue_without_mutating_template(self):
        @self.huey.task()
        def task_a():
            return get_current_context().tenant

        signature = task_a.s()
        self.assertIsNone(signature.task_context)

        with publish_context(ctx('ambient-tenant', 'cid-amb')):
            r = self.huey.enqueue(signature)

        # The caller's template object is untouched.
        self.assertIsNone(signature.task_context)

        dequeued = self.huey.dequeue()
        self.assertEqual(dequeued.task_context.tenant, 'ambient-tenant')

    def test_explicit_overrides_ambient(self):
        @self.huey.task()
        def task_a():
            return get_current_context().tenant

        with publish_context(ctx('ambient', 'cid-amb')):
            task_a(task_context=ctx('explicit', 'cid-exp'))

        dequeued = self.huey.dequeue()
        self.assertEqual(dequeued.task_context.tenant, 'explicit')

    def test_invalid_context_rejected_before_enqueue(self):
        @self.huey.task()
        def task_a():
            pass

        # Non-PublishContext values are rejected by Task.__init__.
        self.assertRaises(ContextValidationError, task_a,
                          task_context={'tenant': 'not-a-context'})
        self.assertRaises(ContextValidationError, task_a,
                          task_context=('tuple', 'not', 'valid'))
        # Bad PublishContext values are rejected at construction time.
        self.assertRaises(ContextValidationError, PublishContext, tenant='')
        self.assertRaises(ContextValidationError, PublishContext,
                          tenant='t', fields={'password': 'x'})
        # Nothing was enqueued.
        self.assertEqual(len(self.huey), 0)

    def test_task_init_rejects_bad_context_type(self):
        class MyTask(Task):
            pass
        self.assertRaises(ContextValidationError, MyTask, task_context=object())

    def test_legacy_context_true_semantics_preserved(self):
        state = {}

        @self.huey.task(context=True)
        def task_c(n, task=None):
            state['id'] = task.id
            state['ctx'] = get_current_context()
            return (n, task.id)

        c = ctx('legacy', 'cid-legacy')
        task_c(3, task_context=c)
        dequeued = self.huey.dequeue()
        self.assertEqual(self.huey.execute(dequeued), (3, dequeued.id))
        self.assertEqual(state['id'], dequeued.id)
        self.assertEqual(state['ctx'], c)
        # The injected task kwarg must still be stripped before re-serializing.
        self.assertEqual(dequeued.kwargs, {})
        self.assertIsNone(get_current_context())

    def test_two_tenants_interleaved_no_residue(self):
        order = []

        @self.huey.task()
        def log_tenant():
            current = get_current_context()
            order.append((current.tenant, current.correlation_id))
            return current.tenant

        log_tenant(task_context=ctx('t1', 'r1'))
        log_tenant(task_context=ctx('t2', 'r2'))
        log_tenant(task_context=ctx('t1', 'r1'))
        log_tenant(task_context=ctx('t2', 'r2'))

        results = []
        for _ in range(4):
            task = self.huey.dequeue()
            results.append(self.huey.execute(task))
            # After every task, the thread-local context must be empty.
            self.assertIsNone(get_current_context())

        self.assertEqual(order, [('t1', 'r1'), ('t2', 'r2'),
                                 ('t1', 'r1'), ('t2', 'r2')])
        self.assertEqual(results, ['t1', 't2', 't1', 't2'])

    def test_context_restored_after_exception(self):
        errors = []

        @self.huey.task()
        def boom():
            raise ValueError('explode')

        @self.huey.signal('error')
        def on_error(sig, task, exc):
            errors.append(exc)

        boom(task_context=ctx('failing', 'cid-fail'))
        task = self.huey.dequeue()
        # Huey._execute handles the exception itself (error signal + result
        # store) rather than propagating it.
        self.assertIsNone(self.huey.execute(task))
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertIsNone(get_current_context())

    def test_context_visible_inside_function_and_clean_after(self):
        seen = []

        @self.huey.task()
        def reader():
            seen.append(get_current_context())

        reader(task_context=ctx('t', 'c'))
        self.huey.execute(self.huey.dequeue())
        self.assertEqual(seen[0].tenant, 't')
        self.assertIsNone(get_current_context())


class TestInheritance(BaseTestCase):
    def test_pipeline_then_inherits_parent_context(self):
        @self.huey.task()
        def parent():
            return 1

        @self.huey.task()
        def child(value):
            return (value, get_current_context().correlation_id)

        pipeline = parent.s(task_context=ctx('p', 'cid-parent')).then(child)
        self.huey.enqueue(pipeline)

        head = self.huey.dequeue()
        self.assertEqual(head.task_context.correlation_id, 'cid-parent')
        self.huey.execute(head)

        successor = self.huey.dequeue()
        self.assertEqual(successor.task_context.correlation_id, 'cid-parent')
        self.assertEqual(self.huey.execute(successor), (1, 'cid-parent'))
        self.assertIsNone(get_current_context())

    def test_explicit_successor_context_overrides_inheritance(self):
        @self.huey.task()
        def parent():
            return 1

        @self.huey.task()
        def child(value):
            return get_current_context().correlation_id

        pipeline = (parent.s(task_context=ctx('p', 'cid-parent'))
                    .then(child, task_context=ctx('c', 'cid-child')))
        self.huey.enqueue(pipeline)

        head = self.huey.dequeue()
        self.huey.execute(head)
        successor = self.huey.dequeue()
        self.assertEqual(successor.task_context.correlation_id, 'cid-child')

    def test_error_successor_inherits_context(self):
        @self.huey.task(retries=0)
        def boom():
            raise RuntimeError('boom')

        @self.huey.task()
        def on_err(exc):
            return get_current_context().tenant

        pipeline = boom.s(task_context=ctx('tenant-err', 'cid-err'))
        pipeline.error(on_err)
        self.huey.enqueue(pipeline)

        head = self.huey.dequeue()
        self.huey.execute(head)

        handler = self.huey.dequeue()
        self.assertEqual(handler.task_context.tenant, 'tenant-err')
        self.assertEqual(self.huey.execute(handler), 'tenant-err')

    def test_group_members_independent_templates_not_mutated(self):
        @self.huey.task()
        def task_a():
            return get_current_context().tenant

        s1 = task_a.s()
        s2 = task_a.s()
        from huey.api import group
        with publish_context(ctx('grp', 'cid-grp')):
            self.huey.enqueue(group([s1, s2]))

        # Both members got stamped via copies; originals are clean.
        self.assertIsNone(s1.task_context)
        self.assertIsNone(s2.task_context)
        tenants = []
        for _ in range(2):
            t = self.huey.dequeue()
            tenants.append(self.huey.execute(t))
        self.assertEqual(sorted(tenants), ['grp', 'grp'])

    def test_group_explicit_override_one_branch_only(self):
        @self.huey.task()
        def task_a():
            return get_current_context().tenant

        from huey.api import group
        s1 = task_a.s(task_context=ctx('explicit', 'cid-e'))
        s2 = task_a.s()
        with publish_context(ctx('ambient', 'cid-a')):
            self.huey.enqueue(group([s1, s2]))

        tenants = sorted(self.huey.execute(self.huey.dequeue()) for _ in range(2))
        self.assertEqual(tenants, ['ambient', 'explicit'])

    def test_chord_callback_does_not_inherit_member_context(self):
        @self.huey.task()
        def member(x):
            return x

        @self.huey.task()
        def callback(values):
            current = get_current_context()
            return (values, current.tenant if current else None)

        from huey.api import chord
        chord_obj = chord([member.s(1, task_context=ctx('m1', 'c1')),
                           member.s(2, task_context=ctx('m2', 'c2'))],
                          callback)
        self.huey.enqueue(chord_obj)

        # Execute the two members.
        for _ in range(2):
            t = self.huey.dequeue()
            self.huey.execute(t)

        cb = self.huey.dequeue()
        # No explicit context on the callback, and it must not have inherited.
        self.assertIsNone(cb.task_context)
        values, tenant = self.huey.execute(cb)
        self.assertEqual(sorted(values), [1, 2])
        self.assertIsNone(tenant)

    def test_chord_callback_keeps_explicit_context(self):
        @self.huey.task()
        def member(x):
            return x

        @self.huey.task()
        def callback(values):
            return get_current_context().tenant

        from huey.api import chord
        chord_obj = chord([member.s(1, task_context=ctx('m', 'cm'))],
                          callback.s(task_context=ctx('cb', 'ccb')))
        self.huey.enqueue(chord_obj)
        self.huey.execute(self.huey.dequeue())
        cb = self.huey.dequeue()
        self.assertEqual(cb.task_context.tenant, 'cb')
        self.assertEqual(self.huey.execute(cb), 'cb')

    def test_empty_chord_callback_skips_ambient_context(self):
        @self.huey.task()
        def callback(values):
            current = get_current_context()
            return current.tenant if current else None

        from huey.api import chord
        with publish_context(ctx('ambient', 'cid-amb')):
            self.huey.enqueue(chord([], callback))
        cb = self.huey.dequeue()
        self.assertIsNone(cb.task_context)
        self.assertIsNone(self.huey.execute(cb))

    def test_retry_with_delay_keeps_context_through_schedule(self):
        attempts = []

        @self.huey.task(retries=1, retry_delay=60)
        def scheduled_flaky():
            attempts.append(get_current_context().correlation_id)
            if len(attempts) < 2:
                raise RuntimeError('retry me later')
            return 'done'

        scheduled_flaky(task_context=ctx('delayed', 'cid-delayed'))
        first = self.huey.dequeue()
        self.huey.execute(first)

        # The retry is parked in the schedule, context must survive.
        self.assertEqual(len(self.huey), 0)
        scheduled = self.huey.read_schedule(
            self.huey._get_timestamp() + datetime.timedelta(seconds=120))
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0].task_context.correlation_id,
                         'cid-delayed')

    def test_signal_receivers_see_task_context(self):
        seen = []

        @self.huey.signal('complete')
        def on_complete(sig, task):
            seen.append((sig, get_current_context().correlation_id))

        @self.huey.task()
        def task_a():
            return 1

        task_a(task_context=ctx('sig', 'cid-sig'))
        self.huey.execute(self.huey.dequeue())
        self.assertEqual(seen, [('complete', 'cid-sig')])
        # Receiver runs inside the bound lifecycle; afterwards it is reset.
        self.assertIsNone(get_current_context())

    def test_retry_preserves_context(self):
        attempts = []

        @self.huey.task(retries=2)
        def flaky():
            attempts.append(get_current_context().correlation_id)
            if len(attempts) < 3:
                raise RuntimeError('not yet')
            return 'ok'

        flaky(task_context=ctx('retry-tenant', 'cid-retry'))

        # First attempt + two requeues, all carrying the same context.
        for _ in range(3):
            t = self.huey.dequeue()
            self.assertEqual(t.task_context.correlation_id, 'cid-retry')
            try:
                result = self.huey.execute(t)
            except RuntimeError:
                result = None
        self.assertEqual(result, 'ok')
        self.assertEqual(attempts, ['cid-retry'] * 3)
        self.assertIsNone(get_current_context())

    def test_dynamic_child_task_inherits_running_context(self):
        @self.huey.task()
        def child():
            return get_current_context().correlation_id

        @self.huey.task()
        def parent():
            # Enqueued while the parent's context is bound.
            r = child()
            return r.id

        parent(task_context=ctx('dyn', 'cid-dyn'))
        head = self.huey.dequeue()
        self.huey.execute(head)

        child_task = self.huey.dequeue()
        self.assertEqual(child_task.task_context.correlation_id, 'cid-dyn')

    def test_dynamic_child_can_override(self):
        @self.huey.task()
        def child():
            return get_current_context().correlation_id

        @self.huey.task()
        def parent():
            child(task_context=ctx('override', 'cid-override'))

        parent(task_context=ctx('dyn', 'cid-dyn'))
        self.huey.execute(self.huey.dequeue())
        child_task = self.huey.dequeue()
        self.assertEqual(child_task.task_context.correlation_id, 'cid-override')

    def test_schedule_with_task_context(self):
        @self.huey.task()
        def task_a():
            return get_current_context().tenant

        eta = datetime.datetime.now() + datetime.timedelta(seconds=60)
        task_a.schedule(eta=eta, task_context=ctx('sched', 'cid-sched'))

        # A future-dated task is first enqueued, then moved into the schedule
        # store when the worker processes it.
        queued = self.huey.dequeue()
        self.assertEqual(queued.task_context.tenant, 'sched')
        self.huey.execute(queued)
        scheduled = self.huey.read_schedule(eta + datetime.timedelta(seconds=1))
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0].task_context.tenant, 'sched')

    def test_reschedule_preserves_context(self):
        @self.huey.task()
        def task_a():
            return get_current_context().tenant

        result = task_a(task_context=ctx('resched', 'cid-resched'))
        new_result = result.reschedule()
        self.assertEqual(new_result.task.task_context,
                         result.task.task_context)


class TestImmediateMode(BaseTestCase):
    def get_huey(self):
        return MemoryHuey(immediate=True, utc=False)

    def test_immediate_context_set_and_restored(self):
        @self.huey.task()
        def task_a():
            return get_current_context().tenant

        r = task_a(task_context=ctx('imm', 'cid-imm'))
        self.assertEqual(r.get(), 'imm')
        self.assertIsNone(get_current_context())

    def test_nested_immediate_restores_outer_context(self):
        @self.huey.task()
        def inner():
            return get_current_context().correlation_id

        seen = {}

        @self.huey.task()
        def outer():
            seen['outer'] = get_current_context().correlation_id
            # Nested immediate execution with its own explicit context.
            inner(task_context=ctx('inner-tenant', 'cid-inner'))
            seen['after_inner'] = get_current_context().correlation_id

        outer(task_context=ctx('outer-tenant', 'cid-outer'))
        self.assertEqual(seen, {'outer': 'cid-outer',
                                'after_inner': 'cid-outer'})
        self.assertIsNone(get_current_context())

    def test_nested_immediate_ambient_inherits_outer(self):
        @self.huey.task()
        def inner():
            return get_current_context().correlation_id

        @self.huey.task()
        def outer():
            r = inner()  # No explicit context: inherit the running one.
            return r.get()

        r = outer(task_context=ctx('o', 'cid-o'))
        self.assertEqual(r.get(), 'cid-o')
        self.assertIsNone(get_current_context())


class TestSerialization(BaseTestCase):
    def test_message_roundtrip_through_default_serializer(self):
        @self.huey.task()
        def task_a():
            pass

        c = ctx('ser', 'cid-ser', {'region': 'eu'})
        task = task_a.s(1, x=2, task_context=c)
        blob = self.huey.serialize_task(task)
        restored = self.huey.deserialize_task(blob)
        self.assertEqual(restored.task_context, c)

    def test_message_backwards_compatible_without_context(self):
        # A legacy 15-field message must deserialize with task_context=None.
        legacy = Message('id', 'name', None, 0, 0, None, (), {}, None, None,
                         None, None, None, None, None)
        self.assertIsNone(legacy.task_context)

    def test_pickle_of_context(self):
        c = ctx('p', 'cid-p', {'k': 'v'})
        restored = pickle.loads(pickle.dumps(c))
        self.assertEqual(restored, c)

    def test_signed_serializer_roundtrip(self):
        from huey.serializer import SignedSerializer
        huey = MemoryHuey(serializer=SignedSerializer(secret='s3cr3t'))

        @huey.task()
        def task_a():
            pass

        task = task_a.s(task_context=ctx('signed', 'cid-signed'))
        restored = huey.deserialize_task(huey.serialize_task(task))
        self.assertEqual(restored.task_context.tenant, 'signed')

    def test_custom_task_subclass_accepts_context_kwarg(self):
        # Custom Task subclasses must accept the task_context keyword. This
        # documents the compatibility boundary: overriding __init__ requires
        # forwarding the keyword.
        class CustomTask(Task):
            def __init__(self, *args, **kwargs):
                marker = kwargs.pop('custom_marker', None)
                super(CustomTask, self).__init__(*args, **kwargs)
                self.custom_marker = marker

        class CustomWrapper(object):
            # Minimal stand-in mimicking TaskWrapper's task-class creation.
            def __call__(self):
                return CustomTask(task_context=ctx('sub', 'cid-sub'),
                                  custom_marker='m')

        instance = CustomWrapper()()
        self.assertEqual(instance.task_context.tenant, 'sub')
        self.assertEqual(instance.custom_marker, 'm')


class TestConsumerContextPropagation(BaseTestCase):
    """Same-process Consumer with real worker threads: interleaved tenants."""

    def test_consumer_interleaves_tenants_and_cleans_up(self):
        import time
        from huey.consumer import Consumer

        records = []
        barrier = threading.Event()

        @self.huey.task()
        def tagged(tenant, cid, wait_for_peer=False):
            current = get_current_context()
            records.append((tenant, cid, current.tenant,
                            current.correlation_id))
            if wait_for_peer:
                # Hold the context while the other worker runs another tenant,
                # proving bindings are per-thread and do not cross.
                barrier.wait(timeout=5)
            return current.tenant

        consumer = Consumer(self.huey, workers=2, initial_delay=0.001,
                            max_delay=0.005, periodic=False,
                            check_worker_health=False)
        consumer.start()
        try:
            tagged('t1', 'r1', True, task_context=ctx('t1', 'r1'))
            tagged('t2', 'r2', False, task_context=ctx('t2', 'r2'))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and len(records) < 2:
                time.sleep(0.02)
            barrier.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and self.huey.pending_count():
                time.sleep(0.02)
        finally:
            consumer.stop(graceful=True)

        self.assertEqual(len(records), 2)
        # Each record's bound context matches its own task, never the peer's.
        for expected_tenant, expected_cid, got_tenant, got_cid in records:
            self.assertEqual(expected_tenant, got_tenant)
            self.assertEqual(expected_cid, got_cid)
        self.assertIsNone(get_current_context())


class TestLogFilter(BaseTestCase):
    def _record(self, msg='hello'):
        return logging.LogRecord(
            name='huey', level=logging.INFO, pathname='', lineno=0,
            msg=msg, args=(), exc_info=None)

    def test_filter_injects_context_attributes(self):
        f = ContextLogFilter()
        with publish_context(ctx('log-tenant', 'cid-log', {'region': 'us'})):
            record = self._record()
            self.assertTrue(f.filter(record))
            self.assertEqual(record.tenant, 'log-tenant')
            self.assertEqual(record.correlation_id, 'cid-log')
            self.assertEqual(record.region, 'us')

    def test_filter_placeholder_without_context(self):
        f = ContextLogFilter()
        record = self._record()
        f.filter(record)
        self.assertEqual(record.tenant, '-')
        self.assertEqual(record.correlation_id, '-')

    def test_filter_never_raises(self):
        from huey.context import current_context

        # A context whose accessors blow up must not be able to interrupt task
        # finalization: the filter swallows the error and still returns True.
        class Exploding(PublishContext):
            @property
            def fields(self):
                raise RuntimeError('boom')

        f = ContextLogFilter()
        token = current_context.set(Exploding('e', correlation_id='c'))
        try:
            record = self._record()
            self.assertTrue(f.filter(record) is True)
            self.assertEqual(record.correlation_id, 'c')
        finally:
            current_context.reset(token)

    def test_filter_survives_broken_record(self):
        from huey.context import current_context

        class BrokenRecord(logging.LogRecord):
            def __setattr__(self, name, value):
                if name in ('tenant', 'correlation_id'):
                    raise RuntimeError('record is broken')
                super(BrokenRecord, self).__setattr__(name, value)

        f = ContextLogFilter()
        token = current_context.set(ctx('t', 'c'))
        try:
            record = BrokenRecord(
                name='huey', level=logging.INFO, pathname='', lineno=0,
                msg='x', args=(), exc_info=None)
            # Even a broken record cannot make the filter raise.
            self.assertTrue(f.filter(record) is True)
        finally:
            current_context.reset(token)

    def test_log_records_not_cross_contaminated(self):
        f = ContextLogFilter()
        r1 = self._record()
        with publish_context(ctx('t1', 'cid-1')):
            f.filter(r1)
        r2 = self._record()
        # No ambient context here.
        f.filter(r2)
        self.assertEqual(r1.correlation_id, 'cid-1')
        self.assertEqual(r2.correlation_id, '-')
