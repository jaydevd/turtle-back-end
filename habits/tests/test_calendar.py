"""Tests for the IST day-label arithmetic in `habits/analytics/calendar.py`.

The contract these lock down is the one that let existing `HabitLog.date` values
stay valid while the day boundary moved from UTC midnight to IST midnight: a
stored label must still resolve to the same calendar date, and a label must only
roll over at local midnight.
"""

import datetime as dt
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from ..analytics import calendar
from .factories import label

IST = ZoneInfo('Asia/Kolkata')
UTC = dt.timezone.utc


def ist(year, month, day, hour=0, minute=0):
  return dt.datetime(year, month, day, hour, minute, tzinfo=IST)


class DayLabelTestCase(SimpleTestCase):
  def test_label_is_utc_midnight_of_the_local_date(self):
    """Labels stay on the 86400 grid the client and database already use."""
    value = calendar.day_label_for(ist(2026, 10, 1, 14, 30))

    self.assertEqual(value, label(2026, 10, 1))
    self.assertEqual(value % calendar.SECONDS_PER_DAY, 0)

  def test_local_date_uses_ist_not_utc(self):
    self.assertEqual(calendar.local_date(ist(2026, 10, 1, 14, 30)), dt.date(2026, 10, 1))

  def test_stored_label_round_trips_to_the_same_calendar_date(self):
    """The migration-free guarantee: a label written before the IST change is
    still the same calendar day afterwards."""
    for day in [dt.date(2026, 1, 1), dt.date(2026, 6, 15), dt.date(2026, 12, 31)]:
      stored = calendar.epoch_of_utc_midnight(day)
      self.assertEqual(calendar.label_to_date(stored), day)
      self.assertEqual(calendar.day_label_for(stored), stored)

  def test_day_rolls_over_at_ist_midnight_not_utc_midnight(self):
    """The actual bug: between 00:00 and 05:30 IST the old mapping still
    returned the previous day's label."""
    just_after = calendar.day_label_for(ist(2026, 10, 2, 0, 1))
    just_before = calendar.day_label_for(ist(2026, 10, 1, 23, 59))

    self.assertEqual(just_after, label(2026, 10, 2))
    self.assertEqual(just_before, label(2026, 10, 1))
    self.assertEqual(just_after - just_before, calendar.SECONDS_PER_DAY)

  def test_late_utc_instant_belongs_to_the_next_ist_day(self):
    """18:30-24:00 UTC is already the next day in IST, and this is exactly the
    window where the old UTC-based mapping disagreed."""
    instant = dt.datetime(2026, 10, 1, 20, 0, tzinfo=UTC)

    self.assertEqual(calendar.local_date(instant), dt.date(2026, 10, 2))
    self.assertEqual(calendar.day_label_for(instant), label(2026, 10, 2))

  def test_early_utc_instant_belongs_to_the_same_ist_day(self):
    instant = dt.datetime(2026, 10, 2, 0, 30, tzinfo=UTC)

    self.assertEqual(calendar.day_label_for(instant), label(2026, 10, 2))

  def test_today_label_accepts_an_injected_instant(self):
    self.assertEqual(
      calendar.today_label(ist(2026, 10, 1, 9, 0)), label(2026, 10, 1)
    )


class NormaliseLabelTestCase(SimpleTestCase):
  def test_snaps_a_midday_value_onto_the_grid(self):
    """`HabitLog.date` has no midnight constraint, so analytics snaps rather
    than trusting the column."""
    self.assertEqual(
      calendar.normalise_label(label(2026, 10, 1) + 45_000), label(2026, 10, 1)
    )

  def test_passes_through_none(self):
    self.assertIsNone(calendar.normalise_label(None))

  def test_leaves_an_exact_label_alone(self):
    self.assertEqual(calendar.normalise_label(label(2026, 10, 1)), label(2026, 10, 1))


class DateArithmeticTestCase(SimpleTestCase):
  def test_add_days_moves_exactly_one_day(self):
    self.assertEqual(
      calendar.add_days(label(2026, 1, 31), 1), label(2026, 2, 1)
    )

  def test_date_range_is_inclusive(self):
    start = label(2026, 10, 1)
    end = label(2026, 10, 4)

    self.assertEqual(
      calendar.date_range(start, end),
      [label(2026, 10, 1), label(2026, 10, 2), label(2026, 10, 3), label(2026, 10, 4)],
    )

  def test_date_range_is_empty_when_backwards(self):
    self.assertEqual(calendar.date_range(label(2026, 10, 4), label(2026, 10, 1)), [])

  def test_days_between(self):
    self.assertEqual(
      calendar.days_between(label(2026, 10, 1), label(2026, 10, 4)), 3
    )


class WeekdayTestCase(SimpleTestCase):
  def test_weekday_matches_the_project_enum(self):
    """`common.enums.Weekday` is Monday=0..Sunday=6, and CUSTOM schedules
    compare against it directly."""
    self.assertEqual(calendar.weekday_of(label(2026, 9, 28)), 0)  # Monday
    self.assertEqual(calendar.weekday_of(label(2026, 10, 4)), 6)  # Sunday

  def test_week_start_is_monday_anchored(self):
    for day in range(28, 5):
      value = label(2026, 9, day) if day >= 28 else label(2026, 10, day)
      start = calendar.week_start(value)
      self.assertEqual(calendar.weekday_of(start), 0)
      self.assertLessEqual(start, value)

  def test_week_start_of_sunday_is_the_previous_monday(self):
    self.assertEqual(calendar.week_start(label(2026, 10, 4)), label(2026, 9, 28))


class MonthTestCase(SimpleTestCase):
  def test_month_key(self):
    self.assertEqual(calendar.month_key(label(2026, 10, 15)), '2026-10')
    self.assertEqual(calendar.month_key(label(2026, 1, 1)), '2026-01')

  def test_month_start(self):
    self.assertEqual(calendar.month_start(label(2026, 10, 15)), label(2026, 10, 1))


class LocalTimeTestCase(SimpleTestCase):
  def test_hour_of_day_buckets_in_ist(self):
    """07:00 IST is 01:30 UTC, so a UTC-based bucket would be wrong by six."""
    instant = dt.datetime(2026, 10, 1, 1, 30, tzinfo=UTC)

    self.assertEqual(calendar.hour_of_day(instant), 7)

  def test_minutes_of_day(self):
    instant = dt.datetime(2026, 10, 1, 3, 45, tzinfo=UTC)

    self.assertEqual(calendar.minutes_of_day(instant), 9 * 60 + 15)

  def test_minutes_from_clock_parses_both_shapes(self):
    self.assertEqual(calendar.minutes_from_clock('07:30:00'), 450)
    self.assertEqual(calendar.minutes_from_clock('07:30'), 450)
    self.assertEqual(calendar.minutes_from_clock(dt.time(7, 30)), 450)

  def test_minutes_from_clock_rejects_junk(self):
    """A malformed reminder must not silently count as a punctuality miss."""
    self.assertIsNone(calendar.minutes_from_clock('not-a-time'))
    self.assertIsNone(calendar.minutes_from_clock('99:99'))
    self.assertIsNone(calendar.minutes_from_clock(None))
    self.assertIsNone(calendar.minutes_from_clock(''))


class DayEndTestCase(SimpleTestCase):
  def test_counts_down_to_local_midnight(self):
    now = ist(2026, 10, 1, 22, 0)
    today = calendar.day_label_for(now)

    remaining = calendar.seconds_until_day_end(today, now)

    self.assertEqual(remaining, 2 * 3600)

  def test_is_zero_at_local_midnight(self):
    now = ist(2026, 10, 2, 0, 0)
    today = calendar.day_label_for(now)

    self.assertEqual(calendar.seconds_until_day_end(today, now), 86400)

  def test_is_negative_for_a_historical_day(self):
    now = ist(2026, 10, 1, 12, 0)
    yesterday = calendar.add_days(calendar.day_label_for(now), -1)

    self.assertLess(calendar.seconds_until_day_end(yesterday, now), 0)