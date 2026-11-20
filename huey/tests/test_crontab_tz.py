"""
Tests for IANA-timezone periodic scheduling and explicit DST handling.

All times are controlled datetimes (zoneinfo on fixed, well-known US DST
transition dates); nothing here reads the network or changes the system
clock. Multi-process behavior (real restarts, dual schedulers) lives in
``test_tz_periodic_multiprocess.py`` and the ``scripts/`` reproducer.
"""
import datetime
import unittest
from zoneinfo import ZoneInfo

from huey import FOLD_ONCE
from huey import FOLD_TWICE
from huey import GAP_NEXT_VALID
from huey import GAP_SKIP
from huey import MemoryHuey
from huey import SqliteHuey
from huey import crontab
from huey import tzcrontab
from huey.consumer import Consumer
from huey.crontab_tz import LocalCronSchedule
from huey.crontab_tz import classify_walltime
from huey.exceptions import ConfigurationError
from huey.tests.base import BaseTestCase


NY = 'America/New_York'
UTC = datetime.timezone.utc

# US DST transition dates are fixed by statute and present in the tzdata
# shipped with the interpreter: 2026 spring-forward is Mar 8 02:00 local and
# fall-back is Nov 1 02:00 local.
SPRING = datetime.datetime(2026, 3, 8)
FALL = datetime.datetime(2026, 11, 1)


def utc(y, mo, d, h=0, mi=0, s=0):
    return datetime.datetime(y, mo, d, h, mi, s)


class WalltimeClassificationTest(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo(NY)

    def test_normal_walltime(self):
        kind, _, _ = classify_walltime(
            datetime.datetime(2026, 6, 1, 2, 30), self.tz)
        self.assertEqual(kind, 'normal')

    def test_spring_forward_gap(self):
        # 02:00-02:59 on Mar 8 2026 does not exist in New York.
        for minute in (0, 15, 30, 59):
            kind, dt0, dt1 = classify_walltime(
                datetime.datetime(2026, 3, 8, 2, minute), self.tz)
            self.assertEqual(kind, 'gap')
            # fold=0 resolves to a strictly later UTC instant than fold=1.
            self.assertGreater(
                dt0.astimezone(UTC), dt1.astimezone(UTC))

    def test_fall_back_fold(self):
        # 01:00-01:59 on Nov 1 2026 happens twice (EDT then EST).
        for minute in (0, 15, 30, 59):
            kind, dt0, dt1 = classify_walltime(
                datetime.datetime(2026, 11, 1, 1, minute), self.tz)
            self.assertEqual(kind, 'fold')
            self.assertLess(dt0.astimezone(UTC), dt1.astimezone(UTC))
            self.assertEqual(dt0.utcoffset(), datetime.timedelta(hours=-4))
            self.assertEqual(dt1.utcoffset(), datetime.timedelta(hours=-5))

    def test_offsets_on_transition_day(self):
        kind, dt0, dt1 = classify_walltime(
            datetime.datetime(2026, 3, 8, 2, 30), self.tz)
        self.assertEqual(kind, 'gap')
        self.assertEqual(dt0.utcoffset(), datetime.timedelta(hours=-5))
        self.assertEqual(dt1.utcoffset(), datetime.timedelta(hours=-4))


class TzcrontabConfigurationTest(unittest.TestCase):
    def test_requires_explicit_timezone(self):
        with self.assertRaises(ConfigurationError):
            tzcrontab(minute='0')

    def test_invalid_timezone_name(self):
        with self.assertRaises(ConfigurationError):
            tzcrontab(minute='0', timezone='Not/AZone')

    def test_invalid_timezone_type(self):
        with self.assertRaises(ConfigurationError):
            tzcrontab(minute='0', timezone=1234)

    def test_invalid_gap_policy(self):
        with self.assertRaises(ConfigurationError):
            tzcrontab(minute='0', timezone=NY, gap_policy='bogus')

    def test_invalid_fold_policy(self):
        with self.assertRaises(ConfigurationError):
            tzcrontab(minute='0', timezone=NY, fold_policy='bogus')

    def test_invalid_catchup(self):
        for bad in (0, 59, -1, 'soon'):
            with self.assertRaises(ConfigurationError):
                tzcrontab(minute='0', timezone=NY, catchup=bad)

    def test_invalid_cron_fields_still_reported(self):
        # The underlying crontab parser keeps its own strict validation.
        with self.assertRaises(ValueError):
            tzcrontab(minute='61', timezone=NY, strict=True)

    def test_zoneinfo_instance_accepted(self):
        sched = tzcrontab(minute='0', timezone=ZoneInfo(NY))
        self.assertIsInstance(sched, LocalCronSchedule)
        self.assertEqual(sched.timezone_name, NY)

    def test_fingerprint_changes_with_plan(self):
        base = tzcrontab(minute='0', hour='2', timezone=NY)
        self.assertEqual(
            base.fingerprint,
            tzcrontab(minute='0', hour='2', timezone=NY).fingerprint)
        changed = (
            tzcrontab(minute='30', hour='2', timezone=NY),
            tzcrontab(minute='0', hour='3', timezone=NY),
            tzcrontab(minute='0', hour='2', timezone='America/Chicago'),
            tzcrontab(minute='0', hour='2', timezone=NY,
                      gap_policy=GAP_NEXT_VALID),
            tzcrontab(minute='0', hour='2', timezone=NY,
                      fold_policy=FOLD_TWICE),
        )
        for other in changed:
            self.assertNotEqual(base.fingerprint, other.fingerprint)

    def test_callable_validator_wallclock_and_aware(self):
        sched = tzcrontab(minute='30', hour='2', timezone=NY)
        # Naive input is read as schedule-local wall time.
        self.assertTrue(sched(datetime.datetime(2026, 6, 1, 2, 30)))
        self.assertFalse(sched(datetime.datetime(2026, 6, 1, 2, 31)))
        # Aware input is converted into the schedule zone first.
        # 06:30Z == 02:30 EDT in June.
        self.assertTrue(sched(datetime.datetime(2026, 6, 1, 6, 30,
                                                 tzinfo=UTC)))


class OccurrenceResolutionTest(unittest.TestCase):
    def test_normal_day_daily_schedule(self):
        sched = tzcrontab(minute='30', hour='2', timezone=NY)
        # June 1, 02:30 NY (EDT, -4) == 06:30 UTC.
        got = list(sched.occurrences(utc(2026, 6, 1, 6, 0),
                                     utc(2026, 6, 1, 6, 31)))
        self.assertEqual(got, [(utc(2026, 6, 1, 6, 30),
                                '20260601T063000#0', 0)])

    def test_window_is_half_open_and_closed(self):
        sched = tzcrontab(minute='0', timezone=NY)
        # 00:00 EDT on Jun 1 == 04:00Z.
        target = utc(2026, 6, 1, 4, 0)
        # Window ending exactly at the target instant includes it.
        self.assertEqual(
            [o[0] for o in sched.occurrences(utc(2026, 6, 1, 3, 59),
                                             target)], [target])
        # A later window that no longer covers that instant excludes it.
        self.assertEqual(
            list(sched.occurrences(utc(2026, 6, 1, 4, 1),
                                   utc(2026, 6, 1, 4, 59))), [])

    def test_spring_gap_skip(self):
        sched = tzcrontab(minute='30', hour='2', timezone=NY,
                          gap_policy=GAP_SKIP)
        # Tick after the would-be occurrence; skip policy emits nothing for
        # Mar 8 (Mar 7 and Mar 9 occurrences are outside this window).
        got = list(sched.occurrences(utc(2026, 3, 8, 7, 0),
                                     utc(2026, 3, 8, 7, 31)))
        self.assertEqual(got, [])

    def test_spring_gap_next_valid(self):
        sched = tzcrontab(minute='30', hour='2', timezone=NY,
                          gap_policy=GAP_NEXT_VALID)
        # 02:30 (missing) -> first valid instant at/after it is 03:30 EDT ==
        # 07:30 UTC.
        got = list(sched.occurrences(utc(2026, 3, 8, 7, 0),
                                     utc(2026, 3, 8, 7, 31)))
        self.assertEqual(got, [(utc(2026, 3, 8, 7, 30),
                                '20260308T073000#0', 0)])

    def test_spring_gap_next_valid_at_gap_start(self):
        sched = tzcrontab(minute='0', hour='2', timezone=NY,
                          gap_policy=GAP_NEXT_VALID)
        # 02:00 missing -> 03:00 EDT == 07:00 UTC.
        got = list(sched.occurrences(utc(2026, 3, 8, 6, 59),
                                     utc(2026, 3, 8, 7, 1)))
        self.assertEqual([o[0] for o in got], [utc(2026, 3, 8, 7, 0)])

    def test_fall_fold_once(self):
        sched = tzcrontab(minute='30', hour='1', timezone=NY,
                          fold_policy=FOLD_ONCE)
        # Tick after both halves; only the first (05:30Z) is scheduled but it
        # is outside the 15-minute window here, so widen via small catchup
        # override to inspect both candidates in one window.
        sched.catchup = 3600
        got = list(sched.occurrences(utc(2026, 11, 1, 5, 0),
                                     utc(2026, 11, 1, 6, 31)))
        self.assertEqual(got, [(utc(2026, 11, 1, 5, 30),
                                '20261101T053000#0', 0)])

    def test_fall_fold_twice_distinct_identities(self):
        sched = tzcrontab(minute='30', hour='1', timezone=NY,
                          fold_policy=FOLD_TWICE)
        sched.catchup = 3600
        got = list(sched.occurrences(utc(2026, 11, 1, 5, 0),
                                     utc(2026, 11, 1, 6, 31)))
        self.assertEqual(got, [
            (utc(2026, 11, 1, 5, 30), '20261101T053000#0', 0),
            (utc(2026, 11, 1, 6, 30), '20261101T063000#1', 1),
        ])
        # The execution identities differ, which is what lets the task body
        # tell the two runs apart.
        self.assertNotEqual(got[0][1], got[1][1])

    def test_fold_two_separate_ticks(self):
        # With default 900s catchup, normal minute ticking delivers each fold
        # half on its own tick and never duplicates.
        sched = tzcrontab(minute='30', hour='1', timezone=NY,
                          fold_policy=FOLD_TWICE)
        first = list(sched.occurrences(utc(2026, 11, 1, 5, 29),
                                       utc(2026, 11, 1, 5, 31)))
        second = list(sched.occurrences(utc(2026, 11, 1, 6, 29),
                                        utc(2026, 11, 1, 6, 31)))
        self.assertEqual([o[2] for o in first], [0])
        self.assertEqual([o[2] for o in second], [1])

    def test_hourly_schedule_across_spring_gap(self):
        sched = tzcrontab(minute='0', timezone=NY, gap_policy=GAP_SKIP)
        # All minute-0 instants from 00:00 through 05:00 local on Mar 8; the
        # 02:00 one is skipped, 03:00 local == 07:00Z.
        got = list(sched.occurrences(utc(2026, 3, 8, 4, 59),
                                     utc(2026, 3, 8, 9, 1)))
        utc_hours = [o[0] for o in got]
        # 00:00 EST(05Z), 01:00 EST(06Z), [02:00 skipped],
        # 03:00 EDT(07Z), 04:00 EDT(08Z), 05:00 EDT(09Z).
        self.assertEqual(utc_hours, [
            utc(2026, 3, 8, 5), utc(2026, 3, 8, 6),
            utc(2026, 3, 8, 7), utc(2026, 3, 8, 8),
            utc(2026, 3, 8, 9)])

    def test_aware_bounds_normalized(self):
        sched = tzcrontab(minute='30', hour='2', timezone=NY)
        aware_got = list(sched.occurrences(
            utc(2026, 6, 1, 6, 0).replace(tzinfo=UTC),
            utc(2026, 6, 1, 6, 31).replace(tzinfo=UTC)))
        naive_got = list(sched.occurrences(utc(2026, 6, 1, 6, 0),
                                           utc(2026, 6, 1, 6, 31)))
        self.assertEqual(aware_got, naive_got)


class HueyEnqueueDueTest(BaseTestCase):
    def get_huey(self):
        return MemoryHuey(utc=True)

    def _daily(self, **kwargs):
        @self.huey.periodic_task(
            tzcrontab(minute='30', hour='2', timezone=NY, **kwargs))
        def ops():
            pass
        return ops

    def test_delivery_and_dedup_on_repeat_tick(self):
        self._daily()
        now = utc(2026, 6, 1, 6, 31)
        first = self.huey.enqueue_due_periodic(now)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].occurrence_id, '20260601T063000#0')
        # Re-checking the exact same minute never re-delivers.
        self.assertEqual(self.huey.enqueue_due_periodic(now), [])
        self.assertEqual(self.huey.pending_count(), 1)

    def test_catchup_window_delivers_recent_miss(self):
        self._daily()
        # Consumer paused ~10 minutes then ticks once: occurrence at 06:30Z
        # is still inside the default 900s window.
        enq = self.huey.enqueue_due_periodic(utc(2026, 6, 1, 6, 40))
        self.assertEqual(len(enq), 1)
        self.assertEqual(self.huey.enqueue_due_periodic(
            utc(2026, 6, 1, 6, 40)), [])

    def test_occurrence_outside_catchup_is_dropped(self):
        self._daily()
        # A tick 20 minutes after the occurrence: older than the 15-minute
        # catch-up window, so no delivery (explicit expiry, not silent retry).
        self.assertEqual(self.huey.enqueue_due_periodic(
            utc(2026, 6, 1, 6, 50)), [])
        self.assertEqual(self.huey.pending_count(), 0)

    def test_custom_catchup_window(self):
        @self.huey.periodic_task(tzcrontab(
            minute='30', hour='2', timezone=NY, catchup=3600))
        def ops_hour():
            pass
        # 30 minutes late: outside default window but inside the custom one.
        enq = self.huey.enqueue_due_periodic(utc(2026, 6, 1, 7, 0))
        self.assertEqual(len(enq), 1)

    def test_aware_timestamp_matches_naive(self):
        self._daily()
        enq = self.huey.enqueue_due_periodic(
            utc(2026, 6, 1, 6, 31).replace(tzinfo=UTC))
        self.assertEqual(len(enq), 1)
        self.assertEqual(self.huey.enqueue_due_periodic(
            utc(2026, 6, 1, 6, 31)), [])

    def test_fold_twice_execution_identity_available_to_task(self):
        seen = []

        @self.huey.periodic_task(tzcrontab(
            minute='30', hour='1', timezone=NY, fold_policy=FOLD_TWICE,
            catchup=3700), context=True)
        def ops(task=None):
            seen.append(task.occurrence_id)

        # One tick whose catch-up window (half-open start) spans both fold
        # halves at 05:30Z (fold#0) and 06:30Z (fold#1).
        enq = self.huey.enqueue_due_periodic(utc(2026, 11, 1, 6, 30, 1))
        self.assertEqual(len(enq), 2)
        for task in enq:
            self.huey.execute(task)
        self.assertEqual(sorted(seen),
                         ['20261101T053000#0', '20261101T063000#1'])

    def test_legacy_crontab_path_is_partitioned(self):
        @self.huey.periodic_task(crontab(minute='*'))
        def legacy():
            pass
        self._daily()
        # The legacy validator path must not see tz tasks and vice versa.
        legacy_tasks = self.huey.read_periodic(utc(2026, 6, 1, 6, 31))
        self.assertEqual([t.name for t in legacy_tasks], ['legacy'])
        self.assertEqual(
            [type(t).__name__ for t in self.huey.local_periodic_tasks()],
            ['ops'])

    def test_plan_change_starts_new_dedup_namespace(self):
        @self.huey.periodic_task(tzcrontab(
            minute='30', hour='2', timezone=NY))
        def ops():
            pass
        tick = utc(2026, 6, 1, 6, 31)
        self.assertEqual(len(self.huey.enqueue_due_periodic(tick)), 1)
        self.assertEqual(self.huey.enqueue_due_periodic(tick), [])

        cls = type(self.huey.local_periodic_tasks()[0])
        self.huey._registry.unregister(cls)

        @self.huey.periodic_task(tzcrontab(
            minute='0', hour='2', timezone=NY))
        def ops():  # noqa: F811 - same name, new plan
            pass
        new_tick = utc(2026, 6, 1, 6, 1)  # 02:00 NY occurrence
        enq = self.huey.enqueue_due_periodic(new_tick)
        self.assertEqual(len(enq), 1)
        self.assertEqual(enq[0].occurrence_id, '20260601T060000#0')

    def test_task_rename_isolates_claims(self):
        @self.huey.periodic_task(tzcrontab(
            minute='30', hour='2', timezone=NY), name='ops_a')
        def ops():
            pass
        tick = utc(2026, 6, 1, 6, 31)
        self.assertEqual(len(self.huey.enqueue_due_periodic(tick)), 1)

        cls = type(self.huey.local_periodic_tasks()[0])
        self.huey._registry.unregister(cls)

        @self.huey.periodic_task(tzcrontab(
            minute='30', hour='2', timezone=NY), name='ops_b')
        def ops2():
            pass
        # Same occurrence minute, but a new task name -> fresh claim.
        self.assertEqual(len(self.huey.enqueue_due_periodic(tick)), 1)


class StartupValidationTest(BaseTestCase):
    def get_huey(self):
        return MemoryHuey(utc=True)

    def test_consumer_rejects_non_utc_huey(self):
        huey = MemoryHuey(utc=False)

        @huey.periodic_task(tzcrontab(minute='0', timezone=NY))
        def ops():
            pass
        with self.assertRaises(ConfigurationError):
            Consumer(huey, workers=1)

    def test_consumer_accepts_valid_configuration(self):
        @self.huey.periodic_task(tzcrontab(minute='0', timezone=NY))
        def ops():
            pass
        consumer = Consumer(self.huey, workers=1)
        self.assertIsNotNone(consumer)

    def test_decorator_rejects_bad_catchup(self):
        with self.assertRaises(ConfigurationError):
            @self.huey.periodic_task(tzcrontab(
                minute='0', timezone=NY, catchup=30))
            def ops():
                pass


class SqliteClaimPersistenceTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix='huey-tz-test-')
        self.path = '%s/huey.db' % self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _huey(self, name='tz-sqlite'):
        return SqliteHuey(name, filename=self.path)

    def test_claim_survives_new_huey_instance(self):
        # Simulate a consumer restart: a brand new Huey/Storage object over
        # the same database file must not redeliver a claimed occurrence.
        h1 = self._huey()

        @h1.periodic_task(tzcrontab(minute='30', hour='2', timezone=NY))
        def ops():
            pass
        tick = utc(2026, 6, 1, 6, 31)
        self.assertEqual(len(h1.enqueue_due_periodic(tick)), 1)
        h1.storage.close()

        h2 = self._huey()

        @h2.periodic_task(tzcrontab(minute='30', hour='2', timezone=NY))
        def ops():  # noqa: F811
            pass
        self.assertEqual(h2.enqueue_due_periodic(tick), [])
        h2.storage.close()

    def test_pruning_removes_stale_claims(self):
        h = self._huey()
        self.assertTrue(h.storage.claim_periodic('pc:old', b'1'))
        # Manually back-date the claim row.
        with h.storage.db(commit=True) as curs:
            curs.execute(
                'update periodic_claim set created = ? where key = ?',
                (1.0, 'pc:old'))
        self.assertTrue(h.storage.claim_periodic('pc:fresh', b'1'))
        h.storage.prune_periodic_claims(cutoff=100.0)
        self.assertFalse(self._claim_exists(h, 'pc:old'))
        self.assertTrue(self._claim_exists(h, 'pc:fresh'))
        h.storage.close()

    @staticmethod
    def _claim_exists(huey, key):
        rows = huey.storage.sql(
            'select 1 from periodic_claim where queue = ? and key = ?',
            (huey.name, key), results=True)
        return bool(rows)

    def test_flush_clears_claims(self):
        h = self._huey()
        self.assertTrue(h.storage.claim_periodic('pc:x', b'1'))
        h.storage.flush_periodic_claims()
        self.assertFalse(self._claim_exists(h, 'pc:x'))
        h.storage.close()


if __name__ == '__main__':
    unittest.main()
