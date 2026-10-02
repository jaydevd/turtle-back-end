"""Calendar and day-label primitives for the analytics layer.

Every timestamp in this project is Unix epoch seconds and every calendar day is
identified by an integer **day label**. A label is the UTC midnight of the
calendar date it represents, which is exactly what the client sends
(`frontend/src/lib/date.ts: startOfTodayUtc`) and what is already persisted in
`HabitLog.date`. Labels are opaque identifiers, not instants.

What this module changes is the *instant to label* mapping. It used to be the
UTC date, so between 00:00 and 05:30 IST the application still treated the
previous calendar day as "today" and rolled the day over 5.5 hours late.
Labels are now derived from the `APP_TIMEZONE` date, so the day rolls over at
local midnight while every already-stored label stays valid and unmigrated:
`epoch_of_utc_midnight(local_date(stored)) == stored`.

Nothing here reads the wall clock implicitly except `today_label()`, which takes
the instant as an argument. Metric functions receive `today` explicitly so they
remain pure and testable.
"""

import datetime as dt
from zoneinfo import ZoneInfo

APP_TIMEZONE = ZoneInfo('Asia/Kolkata')
SECONDS_PER_DAY = 86_400

MINUTES_PER_DAY = 1_440

WEEKDAY_LABELS = (
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
  'Sunday',
)

WEEKDAY_SHORT_LABELS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def as_datetime(instant):
  """Coerce epoch seconds, a date or a datetime into an aware datetime."""
  if isinstance(instant, dt.datetime):
    if instant.tzinfo is None:
      return instant.replace(tzinfo=dt.timezone.utc)
    return instant
  if isinstance(instant, dt.date):
    return dt.datetime(instant.year, instant.month, instant.day, tzinfo=dt.timezone.utc)
  return dt.datetime.fromtimestamp(int(instant), tz=dt.timezone.utc)


def local_date(instant):
  """The `APP_TIMEZONE` calendar date containing `instant`."""
  return as_datetime(instant).astimezone(APP_TIMEZONE).date()


def epoch_of_utc_midnight(date):
  return int(
    dt.datetime(date.year, date.month, date.day, tzinfo=dt.timezone.utc).timestamp()
  )


def day_label_for(instant):
  """Label of the `APP_TIMEZONE` calendar day containing `instant`."""
  return epoch_of_utc_midnight(local_date(instant))


def today_label(instant=None):
  if instant is None:
    instant = dt.datetime.now(dt.timezone.utc)
  return day_label_for(instant)


def normalise_label(value):
  """Snap an arbitrary stored value onto the day-label grid.

  The client always sends UTC midnight, but `HabitLog.date` carries no database
  constraint, so analytics snaps defensively instead of trusting the column.
  """
  if value is None:
    return None
  return (int(value) // SECONDS_PER_DAY) * SECONDS_PER_DAY


def label_to_date(label):
  return dt.datetime.fromtimestamp(int(label), tz=dt.timezone.utc).date()


def add_days(label, days):
  return int(label) + days * SECONDS_PER_DAY


def days_between(start, end):
  return (int(end) - int(start)) // SECONDS_PER_DAY


def date_range(start, end):
  """Inclusive list of day labels from `start` to `end`.

  Returns an empty list when `end` precedes `start` so callers can treat a
  backwards range as "no days" rather than iterating forever.
  """
  if end < start:
    return []
  return [start + offset * SECONDS_PER_DAY for offset in range(days_between(start, end) + 1)]


def weekday_of(label):
  """0 = Monday ... 6 = Sunday, matching `common.enums.Weekday`."""
  return label_to_date(label).weekday()


def weekday_name(label):
  return WEEKDAY_LABELS[weekday_of(label)]


def week_start(label):
  """Monday-anchored start label, matching the existing WEEKLY logic."""
  return add_days(label, -weekday_of(label))


def month_key(label):
  date = label_to_date(label)
  return f'{date.year:04d}-{date.month:02d}'


def month_start(label):
  date = label_to_date(label)
  return epoch_of_utc_midnight(dt.date(date.year, date.month, 1))


def minutes_of_day(instant):
  """Minutes since local midnight for a `completed_at` style instant."""
  local = as_datetime(instant).astimezone(APP_TIMEZONE)
  return local.hour * 60 + local.minute


def hour_of_day(instant):
  local = as_datetime(instant).astimezone(APP_TIMEZONE)
  return local.hour


def minutes_from_clock(value):
  """Minutes since local midnight for a `TimeField`, "HH:MM" or "HH:MM:SS".

  Returns None when the value cannot be interpreted, which keeps punctuality
  metrics from silently counting a malformed reminder as a mismatch.
  """
  if value is None:
    return None
  if isinstance(value, dt.time):
    return value.hour * 60 + value.minute
  parts = str(value).split(':')
  if len(parts) < 2:
    return None
  try:
    hours = int(parts[0])
    minutes = int(parts[1])
  except (TypeError, ValueError):
    return None
  if not (0 <= hours <= 23 and 0 <= minutes <= 59):
    return None
  return hours * 60 + minutes


def utc_offset_seconds(instant=None):
  """Seconds `APP_TIMEZONE` is ahead of UTC at `instant`."""
  if instant is None:
    instant = dt.datetime.now(dt.timezone.utc)
  local = as_datetime(instant).astimezone(APP_TIMEZONE)
  return int(local.utcoffset().total_seconds())


def seconds_until_day_end(label, now):
  """Seconds left in the local day that carries `label`.

  A label is the UTC midnight of a local calendar date, so the local day runs
  from `label - utc_offset` to `label - utc_offset + SECONDS_PER_DAY`. Negative
  when `now` is already past that day's local midnight, which is the normal
  case for the historical days callers probe during streak scans.
  """
  now_ts = int(as_datetime(now).timestamp())
  day_end = int(label) + SECONDS_PER_DAY - utc_offset_seconds(now)
  return day_end - now_ts