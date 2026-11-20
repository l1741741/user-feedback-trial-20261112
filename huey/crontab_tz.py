"""
Timezone-aware periodic scheduling with explicit DST semantics.

This module provides :func:`tzcrontab`, an IANA-timezone-aware counterpart to
:func:`huey.api.crontab`. Schedules are evaluated against wall-clock time in
the requested zone (e.g. ``America/New_York``) while every other part of Huey
keeps using UTC -- no process-level ``TZ`` variable is involved, so queues that
must remain UTC-based are unaffected.

Two DST edge cases are handled explicitly instead of relying on platform
behavior:

* **Non-existent local time** (spring-forward gap). Two policies are offered:
  ``GAP_SKIP`` drops the occurrence, ``GAP_NEXT_VALID`` resolves it to the
  first valid wall-clock instant at or after the requested time.
* **Repeated local time** (fall-back fold). ``FOLD_ONCE`` runs a single
  occurrence (the first, pre-transition instant), ``FOLD_TWICE`` runs both,
  and the two executions carry distinct occurrence identities (fold 0/1).

All computed instants are returned as naive UTC datetimes, matching Huey's
internal representation when ``Huey(utc=True)`` (the default).
"""
import calendar
import datetime
import hashlib
from zoneinfo import ZoneInfo

from huey.api import crontab
from huey.exceptions import ConfigurationError


# Policy for local wall-clock times that do not exist (spring-forward gap).
GAP_SKIP = 'skip'
GAP_NEXT_VALID = 'next_valid'
GAP_POLICIES = (GAP_SKIP, GAP_NEXT_VALID)

# Policy for local wall-clock times that occur twice (fall-back fold).
FOLD_ONCE = 'once'
FOLD_TWICE = 'twice'
FOLD_POLICIES = (FOLD_ONCE, FOLD_TWICE)

# Default window (seconds) within which a missed occurrence is still delivered
# after a consumer pause or restart. Must be at least one scheduler minute.
DEFAULT_CATCHUP_SECONDS = 15 * 60

# Retention for dedup claim rows. Long enough to survive any realistic
# downtime, short enough to keep the table small.
CLAIM_RETENTION_SECONDS = 7 * 24 * 3600


def get_timezone(tz):
    """Resolve an IANA timezone name/object to a ``ZoneInfo`` instance."""
    if isinstance(tz, ZoneInfo):
        return tz
    if not isinstance(tz, str) or not tz:
        raise ConfigurationError(
            'Timezone must be an IANA timezone name (e.g. "America/New_York") '
            'or a zoneinfo.ZoneInfo instance, got %r.' % (tz,))
    try:
        return ZoneInfo(tz)
    except Exception as exc:
        raise ConfigurationError(
            'Invalid IANA timezone %r: %s' % (tz, exc))


def _to_utc_naive(dt):
    """Normalize any datetime to naive UTC.

    Naive datetimes are assumed to already be UTC (Huey's internal convention);
    aware datetimes are converted.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def utc_epoch_seconds(dt_naive_utc):
    """Deterministic UTC epoch seconds that never depend on system TZ."""
    return calendar.timegm(dt_naive_utc.utctimetuple())


def classify_walltime(wall, tz):
    """
    Classify a naive wall-clock datetime in ``tz``.

    Returns a 3-tuple ``(kind, dt_fold0, dt_fold1)`` where ``kind`` is one of
    ``'normal'``, ``'gap'`` (non-existent) or ``'fold'`` (ambiguous) and the
    datetimes are timezone-aware variants for fold=0 and fold=1.
    """
    dt0 = wall.replace(tzinfo=tz, fold=0)
    dt1 = wall.replace(tzinfo=tz, fold=1)
    off0 = dt0.utcoffset()
    off1 = dt1.utcoffset()
    if off0 == off1:
        kind = 'normal'
    elif dt0.astimezone(datetime.timezone.utc) > \
            dt1.astimezone(datetime.timezone.utc):
        # fold=0 maps to a *later* UTC instant than fold=1: the wall time is
        # inside the spring-forward gap and never actually happens.
        kind = 'gap'
    else:
        # fold=0 maps to an earlier UTC instant: the wall time is repeated.
        kind = 'fold'
    return kind, dt0, dt1


class LocalCronSchedule(object):
    """
    A crontab-style schedule interpreted in a fixed IANA timezone.

    Instances are created via :func:`tzcrontab`. They are callable validators
    (like the object returned by :func:`huey.api.crontab`) for wall-clock
    datetimes, and additionally expose :meth:`occurrences` which resolves the
    DST-correct UTC instants due inside a UTC time window.
    """
    def __init__(self, minute, hour, day, month, day_of_week, tz,
                 gap_policy=GAP_SKIP, fold_policy=FOLD_ONCE, strict=False,
                 catchup=DEFAULT_CATCHUP_SECONDS):
        if gap_policy not in GAP_POLICIES:
            raise ConfigurationError(
                'gap_policy must be one of %s, got %r.' %
                (GAP_POLICIES, gap_policy))
        if fold_policy not in FOLD_POLICIES:
            raise ConfigurationError(
                'fold_policy must be one of %s, got %r.' %
                (FOLD_POLICIES, fold_policy))
        if not isinstance(catchup, (int, float)) or catchup < 60:
            raise ConfigurationError(
                'catchup must be a number of seconds >= 60, got %r.' %
                (catchup,))

        self.tz = get_timezone(tz)
        self.gap_policy = gap_policy
        self.fold_policy = fold_policy
        self.catchup = int(catchup)

        # Reuse upstream crontab parsing/validation unchanged so plain cron
        # semantics stay byte-for-byte compatible.
        self._validate = crontab(minute=minute, hour=hour, day=day,
                                 month=month, day_of_week=day_of_week,
                                 strict=strict)
        self._cron_args = (minute, hour, day, month, day_of_week)
        self._fingerprint = self._build_fingerprint()

    @property
    def timezone_name(self):
        return str(self.tz)

    @property
    def fingerprint(self):
        """Opaque token identifying this exact plan.

        Changing any cron field, the timezone or a DST policy changes the
        fingerprint, so dedup state from a previous plan can never suppress an
        occurrence of the new plan.
        """
        return self._fingerprint

    def _build_fingerprint(self):
        raw = '|'.join((
            'v1',
            self.timezone_name,
            self.gap_policy,
            self.fold_policy,
            repr(self._cron_args),
        ))
        return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]

    def validate_wallclock(self, wall_naive):
        """Validate a naive wall-clock datetime expressed in ``self.tz``."""
        return self._validate(wall_naive)

    def __call__(self, timestamp):
        """Crontab-compatible hook: validate an aware or naive datetime.

        Naive datetimes are interpreted in the schedule's own timezone.
        """
        if timestamp.tzinfo is not None:
            wall = timestamp.astimezone(self.tz).replace(tzinfo=None)
        else:
            wall = timestamp
        return self._validate(wall)

    def occurrences(self, window_start_utc, window_end_utc):
        """
        Yield ``(utc_naive_dt, occurrence_id, fold)`` for every scheduled
        occurrence whose UTC instant lies in ``(window_start_utc,
        window_end_utc]``.

        Both bounds are naive UTC datetimes. ``occurrence_id`` is a stable
        string unique per planned occurrence (including the two halves of a
        repeated hour), and ``fold`` is 0 or 1.
        """
        start = _to_utc_naive(window_start_utc)
        end = _to_utc_naive(window_end_utc)
        if end <= start:
            return

        # Scan local wall-clock minutes beginning at midnight (00:00) of the
        # local day containing the window start. Cron fields never address
        # anything finer than a minute or coarser than a month, so starting at
        # the local day boundary guarantees every wall minute that could map
        # into the UTC window is visited -- including minutes inside a
        # spring-forward gap that wall time has already jumped past at tick
        # time. The scan extends two hours past the window end's wall time to
        # cover next_valid gap shifts and fall-back folds; candidates whose
        # resolved UTC instant lies outside the UTC window are filtered at
        # emit time. Catch-up windows are at most 24 hours, so scanning the
        # current plus previous local day covers every case.
        wall_at_start = (start.replace(tzinfo=datetime.timezone.utc)
                         .astimezone(self.tz).replace(tzinfo=None))
        cursor = datetime.datetime.combine(
            wall_at_start.date() - datetime.timedelta(days=1),
            datetime.time.min)
        wall_end = (end.replace(tzinfo=datetime.timezone.utc)
                    .astimezone(self.tz).replace(tzinfo=None)
                    + datetime.timedelta(hours=2))
        emitted = set()

        while cursor <= wall_end:
            if self._validate(cursor.replace(tzinfo=None)):
                kind, dt0, dt1 = classify_walltime(cursor, self.tz)
                candidates = []
                if kind == 'gap':
                    if self.gap_policy == GAP_NEXT_VALID:
                        # The requested wall time never happens. Resolve to
                        # the first valid wall instant at or after it: shift
                        # the wall time forward by the gap size (old offset
                        # minus new offset, e.g. one hour) and re-localize.
                        gap_size = abs(dt0.utcoffset() - dt1.utcoffset())
                        next_wall = (cursor.replace(tzinfo=None) +
                                     gap_size).replace(tzinfo=self.tz)
                        # Every gap minute resolves to a distinct post-gap
                        # instant; the emitted-set still guards duplicates.
                        candidates = [(next_wall, 0)]
                elif kind == 'fold':
                    if self.fold_policy == FOLD_TWICE:
                        candidates = [(dt0, 0), (dt1, 1)]
                    else:
                        # Default to the first (pre-transition) occurrence.
                        candidates = [(dt0, 0)]
                else:
                    candidates = [(dt0, 0)]

                for aware_dt, fold in candidates:
                    utc_dt = aware_dt.astimezone(
                        datetime.timezone.utc).replace(tzinfo=None)
                    if not (start < utc_dt <= end):
                        continue
                    occurrence_id = self.occurrence_id(utc_dt, fold)
                    if occurrence_id in emitted:
                        continue
                    emitted.add(occurrence_id)
                    yield utc_dt, occurrence_id, fold

            cursor += datetime.timedelta(minutes=1)

    def occurrence_id(self, utc_naive_dt, fold=0):
        """Stable identity of one planned occurrence."""
        return '%s#%d' % (
            utc_naive_dt.strftime('%Y%m%dT%H%M%S'), int(bool(fold)))


def tzcrontab(minute='*', hour='*', day='*', month='*', day_of_week='*',
              timezone=None, gap_policy=GAP_SKIP, fold_policy=FOLD_ONCE,
              strict=False, catchup=DEFAULT_CATCHUP_SECONDS):
    """
    Build a :class:`LocalCronSchedule` from crontab fields plus an IANA zone.

    :param timezone: IANA name (e.g. ``"America/New_York"``) or a
        ``zoneinfo.ZoneInfo`` instance. Required -- there is no implicit
        process timezone.
    :param gap_policy: ``GAP_SKIP`` (default) or ``GAP_NEXT_VALID``.
    :param fold_policy: ``FOLD_ONCE`` (default) or ``FOLD_TWICE``.
    :param catchup: seconds after a missed occurrence during which it is still
        delivered after a consumer pause/restart (default 900). Must be >= 60.
    :param strict: forwarded to the underlying crontab parser.

    Invalid timezone names or conflicting policies raise
    :class:`~huey.exceptions.ConfigurationError` at construction time, i.e.
    before the consumer starts.
    """
    if timezone is None:
        raise ConfigurationError(
            'tzcrontab() requires an explicit IANA timezone=. Evaluate the '
            'schedule in UTC with crontab() if that is what you want.')
    return LocalCronSchedule(minute, hour, day, month, day_of_week, timezone,
                             gap_policy=gap_policy, fold_policy=fold_policy,
                             strict=strict, catchup=catchup)
