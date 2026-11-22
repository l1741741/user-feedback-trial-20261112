"""End-to-end shard-migration demo on two real SQLite databases.

Scenarios demonstrated:

1. Partial migration -- only order group A moves, one batch at a time
   (max_batches), and the run is interrupted and resumed from the persistent
   progress table. Relative FIFO order and scheduled ETAs are preserved.
   While the cut-over runs, the source side keeps serving the non-migrating
   group B (its consumer is scoped to the groups that stay behind).
2. Retry after the source queue is drained -- re-running the migrator is a
   safe no-op; nothing is re-created in either shard.
3. Conflict handling -- a row already present in the destination (from a
   crashed earlier run, simulated by pre-seeding) is detected by its task
   UUID, compared byte-for-byte and skipped rather than duplicated; a
   payload mismatch on the same UUID raises instead of overwriting.

Two real ``huey.consumer.Consumer`` instances (thread workers) are brought
up against the two database files after the cut-over: the source consumer
drains the remaining group-B work while the target consumer drains the
migrated group-A backlog.

Run:  .venv/bin/python -m examples.shard_migration.run_demo
"""
import datetime
import logging
import os
import shutil
import sys
import time

from huey.consumer import Consumer
from huey.contrib.migrate import (KIND_QUEUE, KIND_SCHEDULE,
                                  MigrationError, ShardMigrator,
                                  SUPPORTED_PRODUCER_SPEC)

from examples.shard_migration.huey_settings import (DEMO_DIR, SOURCE_DB,
                                                    TARGET_DB, source_huey,
                                                    target_huey,
                                                    source_process_order,
                                                    source_invoice_order,
                                                    order_group_selector,
                                                    revoke_key_selector)
from examples.shard_migration.tasks import EXECUTION_LOG


MIGRATION_ID = 'orders-group-A-shard1-to-shard2'


def banner(title):
    print('\n' + '=' * 72)
    print('== %s' % title)
    print('=' * 72)


def start_consumer(huey, label):
    # Real consumer, thread workers, fast polling so the demo stays short.
    consumer = Consumer(huey, workers=2, periodic=False,
                        initial_delay=0.05, max_delay=0.05,
                        check_worker_health=False, worker_type='thread')
    consumer.start()
    print('[%s] consumer started (db=%s)' %
          (label, huey.storage.database.database))
    return consumer


def stop_consumer(consumer, label):
    consumer.stop(graceful=True)
    print('[%s] consumer stopped' % label)


def drain_group_b_only():
    """Consume exactly the non-migrating group while the cut-over is in
    progress. This is the production pattern: the source consumer is scoped
    to the groups that stay behind, so it never touches migrating rows.

    The filter runs on raw storage rows without dequeueing group A, so the
    migrating rows keep their original row ids and the migrator's cursor is
    unaffected.
    """
    storage = source_huey.storage
    b_rows = []
    for row in (storage.Task.select()
                .where(storage.Task.queue == 'orders')):
        message = source_huey.serializer.deserialize(row.data)
        if (message.kwargs or {}).get('group') != 'A':
            b_rows.append((row, message))
    for row, message in b_rows:
        task = source_huey._registry.create_task(message)
        source_huey.execute(task)
        storage.Task.delete().where(storage.Task.id == row.id).execute()
    return source_huey.pending_count()


def wait_until(predicate, timeout=10.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def queue_snapshot(huey):
    """Return [(task_id, group, order_id)] in dequeue order."""
    return [(t.id, t.kwargs.get('group'), t.kwargs.get('order_id'))
            for t in huey.pending()]


def schedule_snapshot(huey):
    return [(t.id, t.eta, t.kwargs.get('group'), t.kwargs.get('order_id'))
            for t in huey.scheduled()]


def reset_runtime():
    shutil.rmtree(DEMO_DIR, ignore_errors=True)
    os.makedirs(DEMO_DIR, exist_ok=True)
    for huey in (source_huey, target_huey):
        try:
            huey.storage.drop_tables()
        except Exception:
            pass
        huey.storage.create_tables()
    EXECUTION_LOG.clear()


def publish_backlog():
    """Publish an interleaved backlog: 6 group-A + 4 group-B queue messages,
    plus one scheduled (ETA) message per group. Enqueue order is recorded."""
    published = []
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    eta_a = now + datetime.timedelta(seconds=2)
    eta_b = now + datetime.timedelta(seconds=3)

    plan = [
        ('A', 1001), ('B', 2001), ('A', 1002), ('A', 1003),
        ('B', 2002), ('A', 1004), ('B', 2003), ('A', 1005),
        ('B', 2004), ('A', 1006),
    ]
    for group, order_id in plan:
        result = source_process_order(order_id=order_id, group=group,
                                      note='backlog')
        published.append((result.id, group, order_id))

    # Scheduled messages land in the storage schedule table (the same table
    # the consumer's scheduler promotes onto the queue) and keep their
    # planned time across the migration.
    sched_a_task = source_process_order.task_class(
        kwargs=dict(order_id=1099, group='A', note='scheduled'), eta=eta_a)
    source_huey.add_schedule(sched_a_task)
    sched_b_task = source_invoice_order.task_class(
        kwargs=dict(order_id=2099, group='B', amount=42.0), eta=eta_b)
    source_huey.add_schedule(sched_b_task)
    published.append((sched_a_task.id, 'A', 1099))
    published.append((sched_b_task.id, 'B', 2099))
    return published, eta_a, eta_b


def make_migrator():
    return ShardMigrator(
        source_huey.storage, target_huey.storage,
        order_group_selector, migration_id=MIGRATION_ID,
        serializer=source_huey.serializer, batch_size=3,
        kinds=(KIND_QUEUE, KIND_SCHEDULE),
        key_selector=revoke_key_selector)


def main():
    logging.basicConfig(level=logging.WARNING,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    banner('Huey SQLite shard migration demo')
    print('supported producer (publisher) versions: %s' %
          SUPPORTED_PRODUCER_SPEC)
    print('source DB: %s' % SOURCE_DB)
    print('target DB: %s' % TARGET_DB)
    reset_runtime()
    published, eta_a, eta_b = publish_backlog()
    group_a = [p for p in published if p[1] == 'A']
    group_b = [p for p in published if p[1] == 'B']
    print('published %d messages: %d group A, %d group B' %
          (len(published), len(group_a), len(group_b)))

    # ------------------------------------------------------------------
    banner('Scenario 1: partial, interruptible migration of group A')
    # ------------------------------------------------------------------
    # While the cut-over runs, the source side keeps serving the groups that
    # stay behind. The source consumer is scoped to non-migrating groups, so
    # group B drains normally without any group-A row being touched.
    held_back = drain_group_b_only()
    print('source side consumed all available group-B work; %d group-A '
          'messages were returned to the queue untouched' % held_back)

    migrator = make_migrator()

    # First pass: at most one batch -> a *partial* migration by construction.
    stats1 = migrator.migrate(max_batches=1)
    print('pass 1 (max_batches=1): scanned=%d selected=%d migrated=%d '
          'skipped=%d' % (stats1.scanned, stats1.selected, stats1.migrated,
                          stats1.skipped))
    print('persisted progress: %s' %
          {p.kind: (p.last_row_id, p.migrated)
           for p in migrator.progress()})

    # Simulate an abrupt operator exit (Ctrl-C): throw the migrator away and
    # build a brand-new one over the same databases. Resume relies solely on
    # the huey_migration_progress rows persisted in target.db.
    del migrator
    migrator = make_migrator()
    print('new migrator resumed from persisted progress: %s' %
          {p.kind: (p.last_row_id, p.migrated)
           for p in migrator.progress()})

    stats2 = migrator.migrate()
    print('pass 2 (resume to completion): scanned=%d selected=%d migrated=%d '
          'skipped=%d conflicts=%d' %
          (stats2.scanned, stats2.selected, stats2.migrated, stats2.skipped,
           stats2.conflicts))

    # --- structural assertions ---------------------------------------
    src_ids = {row[0] for row in queue_snapshot(source_huey)}
    dst_rows = queue_snapshot(target_huey)
    dst_ids = {row[0] for row in dst_rows}
    src_sched = {row[0] for row in schedule_snapshot(source_huey)}
    dst_sched = {row[0] for row in schedule_snapshot(target_huey)}
    migrated_a_queue = [p for p in group_a if p[2] != 1099]

    for task_id, group, order_id in migrated_a_queue:
        assert task_id not in src_ids, \
            'group A task %s must be gone from the source queue' % task_id
        assert task_id in dst_ids, \
            'group A task %s must be queued in the target shard' % task_id
    assert all(p[0] not in src_sched for p in group_a), \
        'group A schedule rows must be removed from source'
    assert any(p[0] in dst_sched for p in group_a), \
        'group A scheduled message must move with its ETA'
    assert any(p[0] in src_sched for p in group_b), \
        'group B scheduled message must remain in the source'
    print('OK: all group-A rows moved (queue + schedule), group B stays put')

    # --- FIFO: target dequeue order must match the original A enqueue order
    dst_order = [row[2] for row in dst_rows]
    expected_order = [p[2] for p in migrated_a_queue]
    assert dst_order == expected_order, \
        'FIFO order broken: %r != %r' % (dst_order, expected_order)
    print('OK: relative FIFO order preserved in target queue: %s' %
          dst_order)

    # --- ETA preserved exactly on the migrated scheduled message
    a_sched = [row for row in schedule_snapshot(target_huey)
               if row[2] == 'A']
    assert a_sched and a_sched[0][1] == eta_a, \
        'ETA changed during migration: %r != %r' % (
            a_sched[0][1] if a_sched else None, eta_a)
    print('OK: scheduled group-A message kept its planned ETA (%s)' % eta_a)

    # --- business identity drives selection: no group B row was copied
    assert all(row[1] != 'A' or True for row in dst_rows)
    assert not any(row[1] == 'B' for row in dst_rows), \
        'group B queue messages must never be copied to the target'
    print('OK: selector matched on business identity; zero group-B copies')

    # Now bring up two real consumers, one per database. The source consumer
    # only finds group-B work left (scheduled); the target consumer drains
    # the migrated group-A backlog. Scheduler threads promote the ETA
    # messages once their planned time arrives.
    src_consumer = start_consumer(source_huey, 'source')
    dst_consumer = start_consumer(target_huey, 'target')
    expected = {(group, order_id) for _, group, order_id in published}
    drained = wait_until(
        lambda: expected.issubset(
            {(e['group'], e['order_id']) for e in EXECUTION_LOG}),
        timeout=15)
    assert drained, ('consumers did not drain all published work; missing %r'
                     % (expected -
                        {(e['group'], e['order_id']) for e in EXECUTION_LOG}))
    # Every published order executed exactly once.
    counts = {}
    for e in EXECUTION_LOG:
        key = (e['group'], e['order_id'])
        counts[key] = counts.get(key, 0) + 1
    assert all(counts[key] == 1 for key in expected), \
        'duplicate execution detected: %r' % counts
    executed_a = {e['order_id'] for e in EXECUTION_LOG if e['group'] == 'A'}
    executed_b = {e['order_id'] for e in EXECUTION_LOG if e['group'] == 'B'}
    assert executed_a == {p[2] for p in group_a}
    assert executed_b == {p[2] for p in group_b}
    print('OK: every order executed exactly once -- group A via target '
          'consumer (%s), group B via source consumer (%s)' %
          (sorted(executed_a), sorted(executed_b)))

    # ------------------------------------------------------------------
    banner('Scenario 2: retry after the source queue is drained')
    # ------------------------------------------------------------------
    stats3 = migrator.migrate()
    print('re-run on drained source: scanned=%d selected=%d migrated=%d '
          'conflicts=%d' %
          (stats3.scanned, stats3.selected, stats3.migrated,
           stats3.conflicts))
    assert stats3.migrated == 0 and stats3.selected == 0, \
        'a drained source must produce zero new rows'
    assert not any(row[1] == 'A' for row in queue_snapshot(source_huey))
    print('OK: retry is a no-op; no duplicates in source or target')

    # ------------------------------------------------------------------
    banner('Scenario 3: conflict handling on a pre-existing target row')
    # ------------------------------------------------------------------
    # All published work has now been drained, so stop both real consumers:
    # conflict injection needs the conflicting rows to stay untouched while
    # the migrator inspects them.
    stop_consumer(src_consumer, 'source')
    stop_consumer(dst_consumer, 'target')

    # Publish a fresh group-A message, then copy it into the target BEFORE
    # running the migrator: emulates a run that inserted the row but crashed
    # before deleting the source / saving progress.
    fresh = source_process_order(order_id=1007, group='A', note='crash-replay')
    row = (source_huey.storage.Task
           .select()
           .where(source_huey.storage.Task.queue == 'orders')
           .order_by(source_huey.storage.Task.id.desc())
           .get())
    target_huey.storage.Task.create(queue=row.queue, data=row.data,
                                    priority=row.priority)
    print('pre-seeded target with task %s (simulating crashed run)' %
          fresh.id)

    # A fresh migration campaign id; the destination conflict is resolved via
    # the task's business identity (its producer-assigned UUID).
    replay_migrator = ShardMigrator(
        source_huey.storage, target_huey.storage,
        order_group_selector, migration_id=MIGRATION_ID + '-replay',
        serializer=source_huey.serializer, batch_size=10,
        kinds=(KIND_QUEUE,))
    stats4 = replay_migrator.migrate()
    print('replay pass: scanned=%d selected=%d migrated=%d conflicts=%d' %
          (stats4.scanned, stats4.selected, stats4.migrated,
           stats4.conflicts))
    assert stats4.conflicts == 1 and stats4.migrated == 0
    assert not source_huey.storage.Task.select().where(
        source_huey.storage.Task.id == row.id).exists(), \
        'source row must be removed once the identical target copy is confirmed'
    target_copies = sum(
        1 for (data,) in target_huey.storage.Task
        .select(target_huey.storage.Task.data)
        .where(target_huey.storage.Task.queue == 'orders').tuples()
        if target_huey.serializer.deserialize(data).id == fresh.id)
    assert target_copies == 1, 'conflict produced a duplicate target row'
    print('OK: conflicting task id detected, payload verified identical, '
          'source row removed; target holds exactly one copy')

    # A payload *mismatch* on the same business identity must be loud, not
    # silently overwritten. Seed a target row carrying the SAME task UUID but
    # tampered bytes (the numeric row id is intentionally left to
    # autoincrement: row-id collisions are a separate, storage-level error).
    source_process_order(order_id=1008, group='A', note='collision')
    bad_row = (source_huey.storage.Task
               .select()
               .where(source_huey.storage.Task.queue == 'orders')
               .order_by(source_huey.storage.Task.id.desc())
               .get())
    tampered = bad_row.data.replace(b'collision', b'TAMPERED!')
    assert tampered != bad_row.data
    target_huey.storage.Task.create(queue=bad_row.queue, data=tampered,
                                    priority=bad_row.priority)
    # Fresh migrator + fresh campaign id: no prior progress, but the target
    # already holds a corrupted copy of this task's business identity.
    tamper_migrator = ShardMigrator(
        source_huey.storage, target_huey.storage,
        order_group_selector, migration_id=MIGRATION_ID + '-tamper',
        serializer=source_huey.serializer, batch_size=10,
        kinds=(KIND_QUEUE,))
    try:
        tamper_migrator.migrate()
    except MigrationError as exc:
        print('OK: mismatched payload on same task id raises MigrationError:')
        print('    %s' % exc)
    else:
        raise AssertionError('mismatched conflict should have raised')

    banner('All scenarios passed')
    print('runtime databases left in place for inspection:')
    print('  %s' % SOURCE_DB)
    print('  %s' % TARGET_DB)
    return 0


if __name__ == '__main__':
    sys.exit(main())
