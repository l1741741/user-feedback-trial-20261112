import datetime
import os
import threading
import unittest

from huey.api import MemoryHuey
from huey.api import SqliteHuey
from huey.exceptions import HueyException
from huey.exceptions import TaskAliasInUse
from huey.tests import TEST_TMP_DIR
from huey.tests.base import BaseTestCase


def wire_names(huey):
    return [huey.serializer.deserialize(data).name
            for data in huey.storage.enqueued_items()]


def scheduled_wire_names(huey):
    return [huey.serializer.deserialize(data).name
            for data in huey.storage.scheduled_items()]


class AliasTestCase(BaseTestCase):
    def get_huey(self):
        return MemoryHuey(utc=False)

    def old_publisher(self, retries=0):
        """A Huey whose only knowledge of the task is the old name."""
        huey = MemoryHuey('alias-queue', utc=False)

        def generate_report(*args, **kwargs):
            return ('old', args, kwargs)
        wrapper = huey.task(retries=retries, name='generate_report')(
            generate_report)
        old_name = huey._registry.task_to_string(wrapper.task_class)
        return huey, wrapper, old_name

    def new_consumer(self, old_name, retries=0, storage=None, func=None):
        """A Huey that knows the renamed task and maps the old name to it."""
        huey = MemoryHuey('alias-queue', utc=False)
        if storage is not None:
            huey.storage = storage

        if func is None:
            def func(*args, **kwargs):
                return ('new', args, kwargs)
        wrapper = huey.task(retries=retries, name='generate_report_v2')(func)
        huey.register_task_alias(old_name, wrapper.task_class)
        return huey, wrapper


class TestRegistryAliases(AliasTestCase):
    def test_alias_resolves_to_target(self):
        @self.huey.task()
        def new_report():
            return 'new'

        old_name = 'reports.generate_report'
        self.huey.register_task_alias(old_name, new_report.task_class)
        target = self.huey._registry.task_to_string(new_report.task_class)
        self.assertEqual(self.huey.task_aliases(), {old_name: target})
        self.assertEqual(self.huey._registry.resolve_name(old_name), target)

    def test_alias_already_registered_task_rejected(self):
        @self.huey.task()
        def existing():
            pass

        @self.huey.task()
        def target():
            pass

        existing_name = self.huey._registry.task_to_string(
            existing.task_class)
        self.assertRaises(ValueError, self.huey.register_task_alias,
                          existing_name, target)

    def test_alias_self_rejected(self):
        @self.huey.task()
        def target():
            pass

        name = self.huey._registry.task_to_string(target.task_class)
        self.assertRaises(ValueError, self.huey.register_task_alias,
                          name, target)

    def test_alias_remapped_to_different_target_rejected(self):
        @self.huey.task()
        def target1():
            pass

        @self.huey.task()
        def target2():
            pass

        self.huey.register_task_alias('old.name', target1)
        # Idempotent re-registration is fine.
        self.assertTrue(self.huey.register_task_alias('old.name', target1))
        self.assertRaises(ValueError, self.huey.register_task_alias,
                          'old.name', target2)

    def test_register_task_under_alias_rejected(self):
        @self.huey.task()
        def target():
            pass

        self.huey.register_task_alias('a.b', target)

        def new_task():
            pass
        new_task.__module__ = 'a'
        new_task.__name__ = 'b'
        self.assertRaises(ValueError, self.huey.task(), new_task)

    def test_alias_target_may_be_declared_later(self):
        # Aliases resolve through the target name, so the new task can be
        # imported after the alias has been declared.
        self.huey.register_task_alias('a.b.old', 'a.b.new')
        self.assertEqual(self.huey._registry.resolve_name('a.b.old'), 'a.b.new')

        def new_task():
            pass
        new_task.__module__ = 'a.b'
        new_task.__name__ = 'new'
        wrapper = self.huey.task()(new_task)
        self.assertEqual(self.huey._registry.task_to_string(
            wrapper.task_class), 'a.b.new')
        self.assertIs(self.huey._registry.string_to_task(
            self.huey._registry.resolve_name('a.b.old')), wrapper.task_class)

    def test_circular_alias_rejected(self):
        self.huey.register_task_alias('a', 'b')
        self.huey.register_task_alias('b', 'a')
        self.assertRaises(HueyException, self.huey._registry.resolve_name, 'a')


class TestTaskAliasMigration(AliasTestCase):
    def test_old_message_executes_new_task_keeping_id_and_result(self):
        publisher, old_task, old_name = self.old_publisher()
        result = old_task(1, 2, key='value')
        self.assertEqual(wire_names(publisher), [old_name])

        consumer, new_task = self.new_consumer(old_name,
                                               storage=publisher.storage)
        task = consumer.dequeue()
        self.assertIsInstance(task, new_task.task_class)
        self.assertEqual(task.id, result.id)
        self.assertEqual(task._wire_name, old_name)
        self.assertEqual(consumer.execute(task), ('new', (1, 2),
                                                   {'key': 'value'}))
        # The result is stored under the original task ID.
        self.assertEqual(consumer.result(result.id), ('new', (1, 2),
                                                      {'key': 'value'}))

    def test_retries_keep_old_wire_name_and_remaining_count(self):
        state = {'calls': 0}

        def flaky(n):
            state['calls'] += 1
            if state['calls'] < 3:
                raise ValueError('boom')
            return ('ok', n)

        publisher = MemoryHuey('alias-queue', utc=False)
        old_task = publisher.task(retries=2, name='flaky')(flaky)
        old_name = publisher._registry.task_to_string(old_task.task_class)
        result = old_task(42)

        consumer, new_task = self.new_consumer(old_name, retries=2,
                                               storage=publisher.storage,
                                               func=flaky)
        task = consumer.dequeue()
        self.assertEqual(task.retries, 2)
        self.assertEqual(task._wire_name, old_name)

        consumer.execute(task)
        self.assertEqual(wire_names(consumer), [old_name])
        self.assertEqual(consumer.pending()[0].retries, 1)

        consumer.execute(consumer.dequeue())
        self.assertEqual(wire_names(consumer), [old_name])
        self.assertEqual(consumer.pending()[0].retries, 0)

        self.assertEqual(consumer.execute(consumer.dequeue()), ('ok', 42))
        self.assertEqual(consumer.result(result.id), ('ok', 42))
        self.assertEqual(consumer.task_alias_status(old_name)['total'], 0)

    def test_retirement_requires_empty_backlog(self):
        # A retry is still queued under the old name after the first failure;
        # the alias cannot be retired yet, and force-retiring would strand it.
        state = {'calls': 0}

        def flaky():
            state['calls'] += 1
            if state['calls'] < 2:
                raise ValueError('boom')
            return 'ok'

        publisher = MemoryHuey('alias-queue', utc=False)
        old_task = publisher.task(retries=2, name='flaky')(flaky)
        old_name = publisher._registry.task_to_string(old_task.task_class)
        old_task()

        consumer, new_task = self.new_consumer(old_name, retries=2,
                                               storage=publisher.storage,
                                               func=flaky)
        consumer.execute(consumer.dequeue())  # Fails, retry stays old-name.
        self.assertEqual(wire_names(consumer), [old_name])
        self.assertRaises(TaskAliasInUse,
                          consumer.unregister_task_alias, old_name)

        # The retry is processed before retirement.
        self.assertEqual(consumer.execute(consumer.dequeue()), 'ok')
        self.assertTrue(consumer.unregister_task_alias(old_name))

        # New messages are published under the new name and still work.
        r = new_task()
        self.assertEqual(wire_names(consumer), [
            consumer._registry.task_to_string(new_task.task_class)])
        self.assertEqual(consumer.execute(consumer.dequeue()), 'ok')
        self.assertEqual(consumer.result(r.id), 'ok')

    def test_alias_counts_pipeline_references(self):
        publisher, old_task, old_name = self.old_publisher()
        # A pipeline whose completion handler is also the old task. The single
        # queued message references the old name twice, but it is one message
        # that would be stranded.
        head = old_task.s().then(old_task)
        publisher.enqueue(head)

        consumer, new_task = self.new_consumer(old_name,
                                               storage=publisher.storage)
        status = consumer.task_alias_status(old_name)
        self.assertEqual(status['queue'], 1)
        self.assertRaises(TaskAliasInUse,
                          consumer.unregister_task_alias, old_name)

    def test_unknown_old_name_without_alias_still_fails(self):
        publisher, old_task, old_name = self.old_publisher()
        old_task()

        consumer, new_task = self.new_consumer(old_name,
                                               storage=publisher.storage)
        consumer.unregister_task_alias(old_name, force=True)
        self.assertRaises(HueyException, consumer.dequeue)

    def test_alias_keeps_expiration_and_pipeline_metadata(self):
        publisher = MemoryHuey('alias-queue', utc=False)

        def generate_report():
            return 'old-report'

        def after_report(r):
            return 'after:%s' % r

        old_report = publisher.task(expires=30, name='generate_report')(
            generate_report)
        old_after = publisher.task(name='after_report')(after_report)
        old_name = publisher._registry.task_to_string(old_report.task_class)
        head = old_report.s().then(old_after)
        result_group = publisher.enqueue(head)
        result = result_group._results[0]

        consumer = MemoryHuey('alias-queue', utc=False)
        consumer.storage = publisher.storage
        new_report = consumer.task(name='generate_report_v2')(
            lambda: 'new-report')
        consumer.task(name='after_report')(lambda r: 'after:%s' % r)
        consumer.register_task_alias(old_name, new_report.task_class)

        task = consumer.dequeue()
        self.assertEqual(task.id, result.id)
        self.assertIsNotNone(task.expires_resolved)
        self.assertIsNotNone(task.on_complete)
        self.assertEqual(consumer.execute(task), 'new-report')
        # on_complete is enqueued under its own name and still executes.
        self.assertEqual(consumer.execute(consumer.dequeue()),
                         'after:new-report')


class TestTaskAliasStatus(AliasTestCase):
    def setUp(self):
        super(TestTaskAliasStatus, self).setUp()
        self.publisher, self.old_wrapper, self.old_name = (
            self.old_publisher())
        self.consumer, self.new_wrapper = self.new_consumer(
            self.old_name, storage=self.publisher.storage)

    def test_status_counts_queue_schedule_and_inflight(self):
        self.old_wrapper()
        # A task added to the schedule directly, as a worker would for a
        # future-dated message.
        self.consumer.add_schedule(
            self.old_wrapper.s(eta=self.consumer._get_timestamp() +
                               datetime.timedelta(days=1)))
        status = self.consumer.task_alias_status(self.old_name)
        self.assertTrue(status['registered'])
        self.assertEqual(status['queue'], 1)
        self.assertEqual(status['schedule'], 1)
        self.assertEqual(status['inflight'], 0)
        self.assertEqual(status['total'], 2)

        task = self.consumer.dequeue()
        status = self.consumer.task_alias_status(self.old_name)
        self.assertEqual(status['queue'], 0)
        self.assertEqual(status['inflight'], 1)
        self.assertEqual(status['total'], 2)

        self.consumer.execute(task)
        status = self.consumer.task_alias_status(self.old_name)
        self.assertEqual(status['inflight'], 0)
        self.assertEqual(status['schedule'], 1)

    def test_unregister_refuses_while_backlog_exists(self):
        self.old_wrapper()
        self.assertRaises(TaskAliasInUse,
                          self.consumer.unregister_task_alias, self.old_name)
        self.assertIn(self.old_name, self.consumer.task_aliases())

    def test_unregister_refuses_while_inflight(self):
        self.old_wrapper()
        task = self.consumer.dequeue()  # Claimed, not finished.
        try:
            self.assertRaises(TaskAliasInUse,
                              self.consumer.unregister_task_alias,
                              self.old_name)
        finally:
            self.consumer.execute(task)

    def test_unregister_succeeds_when_drained(self):
        self.old_wrapper()
        self.consumer.execute(self.consumer.dequeue())
        self.assertTrue(self.consumer.unregister_task_alias(self.old_name))
        self.assertNotIn(self.old_name, self.consumer.task_aliases())
        self.assertFalse(self.consumer.unregister_task_alias(self.old_name))

    def test_force_unregister(self):
        self.old_wrapper()
        self.assertTrue(self.consumer.unregister_task_alias(self.old_name,
                                                            force=True))
        self.assertNotIn(self.old_name, self.consumer.task_aliases())

    def test_wait_for_alias(self):
        self.old_wrapper()
        timer = threading.Timer(
            0.05, lambda: self.consumer.execute(self.consumer.dequeue()))
        timer.start()
        self.assertTrue(self.consumer.wait_for_task_alias(self.old_name,
                                                         timeout=2,
                                                         interval=0.01))
        timer.join()


class TestTaskAliasAcrossProcesses(unittest.TestCase):
    """Old publisher, new consumer, and one SQLite database."""

    def setUp(self):
        self.db_file = os.path.join(TEST_TMP_DIR, 'alias-%s.db' % id(self))

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            path = self.db_file + suffix
            if os.path.exists(path):
                os.unlink(path)

    def old_huey(self):
        return SqliteHuey('report', filename=self.db_file, utc=False)

    def new_huey(self):
        return SqliteHuey('report', filename=self.db_file, utc=False)

    def make_old_publisher(self):
        huey = self.old_huey()

        def generate_report(day):
            return ('old', day)
        wrapper = huey.task(retries=2, name='generate_report')(
            generate_report)
        old_name = huey._registry.task_to_string(wrapper.task_class)
        return huey, wrapper, old_name

    def make_new_consumer(self, old_name):
        huey = self.new_huey()

        def generate_report_v2(day):
            return ('new', day)
        wrapper = huey.task(retries=2, name='generate_report_v2')(
            generate_report_v2)
        huey.register_task_alias(old_name, wrapper.task_class)
        return huey, wrapper

    def test_handoff_and_retirement(self):
        publisher, old_wrapper, old_name = self.make_old_publisher()
        r1 = old_wrapper(1)
        r2 = old_wrapper.schedule((2,), delay=60)
        publisher.storage.close()

        consumer, new_wrapper = self.make_new_consumer(old_name)
        status = consumer.task_alias_status(old_name)
        self.assertEqual(status['queue'], 2)  # Scheduled is still enqueued.

        # A worker picks two messages up: one runs, one is future-dated and
        # goes to the schedule.
        started = consumer.dequeue()
        future = consumer.dequeue()
        first, second = sorted((started.id, future.id))
        self.assertEqual({started.id, future.id}, {r1.id, r2.id})
        future_task = future if future.id == r2.id else started
        ready_task = started if future.id == r2.id else future
        self.assertEqual(consumer.execute(future_task), None)  # Scheduled.
        self.assertEqual(scheduled_wire_names(consumer), [old_name])
        self.assertEqual(consumer.task_alias_status(old_name)['inflight'], 1)

        # Another handle to the same database sees the claim, even before its
        # own task registry has been populated.
        other = self.new_huey()
        other.register_task_alias(old_name, new_wrapper.task_class)
        self.assertEqual(other.task_alias_status(old_name)['inflight'], 1)
        other.storage.close()

        # When the schedule is due, the scheduler hands the message back to
        # the queue under the name it was stored with, and it runs through
        # the alias, keeping its ID.
        self.assertEqual(consumer.execute(ready_task), ('new', 1))
        self.assertEqual(consumer.result(r1.id), ('new', 1))

        due = consumer.scheduled()[0].eta
        due_messages = consumer.storage.read_schedule(due)
        self.assertEqual(len(due_messages), 1)
        consumer.storage.enqueue(due_messages[0])
        scheduled = consumer.dequeue()
        self.assertEqual(scheduled.id, r2.id)
        self.assertEqual(scheduled._wire_name, old_name)
        self.assertEqual(consumer.execute(scheduled, due), ('new', 2))
        self.assertEqual(consumer.result(r2.id), ('new', 2))

        self.assertEqual(consumer.task_alias_status(old_name)['total'], 0)
        self.assertTrue(consumer.unregister_task_alias(old_name))
        self.assertEqual(consumer.task_aliases(), {})
        consumer.storage.close()

    def test_retry_round_trip_through_database(self):
        state = {'calls': 0}

        def flaky(day):
            state['calls'] += 1
            if state['calls'] < 3:
                raise ValueError('boom')
            return ('new', day)

        publisher = self.old_huey()

        def old_flaky(day):
            return ('old', day)
        old_wrapper = publisher.task(retries=2, name='flaky')(old_flaky)
        old_name = publisher._registry.task_to_string(old_wrapper.task_class)
        result = old_wrapper(7)
        publisher.storage.close()

        consumer = self.new_huey()
        new_wrapper = consumer.task(retries=2, name='flaky_v2')(flaky)
        consumer.register_task_alias(old_name, new_wrapper.task_class)

        # Every attempt fails until the third, which succeeds. Each attempt
        # keeps its old wire name and its remaining retry count.
        for expected_retries in (2, 1, 0):
            task = consumer.dequeue()
            self.assertEqual(task.retries, expected_retries)
            self.assertEqual(task._wire_name, old_name)
            consumer.execute(task)
            self.assertEqual(wire_names(consumer),
                             [old_name] * min(expected_retries, 1))
        self.assertEqual(consumer.result(result.id), ('new', 7))
        self.assertEqual(consumer.task_alias_status(old_name)['total'], 0)
        consumer.storage.close()


if __name__ == '__main__':
    unittest.main()
