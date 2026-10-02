"""Tests for `habits/analytics/resolver.py`.

`resolve_day_states` is the primitive every reported number derives from, so
these tests focus on the classification rules that were previously applied
inconsistently across four different call sites: inferred misses, partial
credit, `target_count` handling, WEEKLY granularity, and the fact that a day
before a habit started - or after it ended - is not a miss.
"""

from django.test import SimpleTestCase

from common.enums import DayState, ScoreUnit, Status

from ..analytics import calendar
from ..analytics.resolver import (
  build_periods,
  day_target_for,
  is_due_on,
  is_lifecycle_active,
  resolve_day_states,
  score_unit_for,
  target_for,
)
from .factories import custom, hit, label, log, schedule, states_by_date, weekly

TODAY = label(2026, 10, 8)
WEEK_BACK = label(2026, 10, 1)


def resolve(start=WEEK_BACK, **kwargs):
  kwargs.setdefault('today', TODAY)
  return resolve_day_states(start=start, **kwargs)


class ScoreUnitTestCase(SimpleTestCase):
  def test_weekly_scores_per_week(self):
    self.assertEqual(score_unit_for(weekly(3)), ScoreUnit.WEEK)

  def test_daily_and_custom_score_per_day(self):
    self.assertEqual(score_unit_for(schedule('DAILY')), ScoreUnit.DAY)
    self.assertEqual(score_unit_for(custom([0, 2, 4])), ScoreUnit.DAY)

  def test_missing_schedule_scores_per_day(self):
    self.assertEqual(score_unit_for(None), ScoreUnit.DAY)

  def test_weekly_period_target_is_the_weekly_count(self):
    self.assertEqual(target_for(weekly(3)), 3)

  def test_weekly_day_target_is_one(self):
    """Applying the weekly target per day would demand all three reps land on
    the same date and mark every other day of the week a miss."""
    self.assertEqual(day_target_for(weekly(3)), 1)

  def test_daily_day_target_matches_its_period_target(self):
    self.assertEqual(day_target_for(schedule('DAILY', 3)), 3)


class IsDueOnTestCase(SimpleTestCase):
  def test_no_schedule_is_always_due(self):
    self.assertTrue(is_due_on(None, label(2026, 10, 8)))

  def test_daily_is_always_due(self):
    self.assertTrue(is_due_on(schedule('DAILY'), label(2026, 10, 8)))

  def test_weekly_is_always_due(self):
    """A weekly habit has no fixed days: any day counts towards its target."""
    self.assertTrue(is_due_on(weekly(3), label(2026, 10, 8)))

  def test_custom_only_on_listed_weekdays(self):
    monday_wednesday_friday = custom([0, 2, 4])

    self.assertTrue(is_due_on(monday_wednesday_friday, label(2026, 9, 28)))
    self.assertFalse(is_due_on(monday_wednesday_friday, label(2026, 9, 29)))

  def test_custom_tolerates_an_empty_weekday_list(self):
    self.assertFalse(is_due_on(custom([]), label(2026, 10, 8)))


class LifecycleTestCase(SimpleTestCase):
  def test_active_habit_within_bounds(self):
    self.assertTrue(
      is_lifecycle_active(Status.ACTIVE, label(2026, 1, 1), None, TODAY)
    )

  def test_day_before_start_date_is_not_due(self):
    self.assertFalse(
      is_lifecycle_active(Status.ACTIVE, label(2026, 10, 2), None, label(2026, 10, 1))
    )

  def test_day_after_end_date_is_not_due(self):
    self.assertFalse(
      is_lifecycle_active(Status.ACTIVE, label(2026, 1, 1), label(2026, 9, 1), TODAY)
    )

  def test_archived_habit_is_never_due(self):
    """Archived and expired habits previously kept accruing streaks because no
    streak code ever read `Habit.status`, `start_date` or `end_date`."""
    for status in (Status.COMPLETED, Status.SKIPPED, Status.PARTIAL, Status.MISSED):
      self.assertFalse(
        is_lifecycle_active(status, None, None, TODAY), msg=status
      )


class DayResolutionTestCase(SimpleTestCase):
  def test_completed_log_resolves_to_hit(self):
    states = states_by_date(
      resolve(logs_by_day={TODAY: hit(1)})
    )
    self.assertEqual(states[TODAY], DayState.HIT)

  def test_due_day_with_no_log_resolves_to_missed(self):
    """Nothing in the codebase ever writes `Status.MISSED`, so every miss in
    the product is inferred from the absence of a log row."""
    states = states_by_date(resolve())
    self.assertEqual(states[TODAY], DayState.MISSED)

  def test_explicit_missed_log_resolves_to_missed(self):
    states = states_by_date(resolve(logs_by_day={TODAY: log(Status.MISSED, 0)}))
    self.assertEqual(states[TODAY], DayState.MISSED)

  def test_skipped_day_is_excluded_from_the_denominator(self):
    """Deliberately skipping is not a failure."""
    states = states_by_date(resolve(logs_by_day={TODAY: log(Status.SKIPPED, 0)}))
    self.assertEqual(states[TODAY], DayState.SKIPPED)

  def test_partial_log_with_reps_resolves_to_partial(self):
    states = states_by_date(resolve(logs_by_day={TODAY: log(Status.PARTIAL, 2)}))
    self.assertEqual(states[TODAY], DayState.PARTIAL)

  def test_partial_log_with_zero_reps_resolves_to_missed(self):
    states = states_by_date(resolve(logs_by_day={TODAY: log(Status.PARTIAL, 0)}))
    self.assertEqual(states[TODAY], DayState.MISSED)

  def test_future_day_is_pending_not_missed(self):
    future = label(2026, 10, 20)
    states = states_by_date(resolve(start=label(2026, 10, 8), end=future))

    self.assertEqual(states[future], DayState.PENDING)

  def test_day_before_habit_start_is_not_due_not_missed(self):
    states = states_by_date(
      resolve(habit_start_date=label(2026, 10, 5))
    )

    self.assertEqual(states[label(2026, 10, 1)], DayState.NOT_DUE)
    self.assertEqual(states[label(2026, 10, 6)], DayState.MISSED)

  def test_day_after_habit_end_is_not_due(self):
    states = states_by_date(
      resolve(habit_end_date=label(2026, 10, 5))
    )

    self.assertEqual(states[label(2026, 10, 6)], DayState.NOT_DUE)

  def test_archived_habit_resolves_every_day_as_not_due(self):
    states = states_by_date(resolve(status=Status.COMPLETED))

    self.assertEqual(set(states.values()), {DayState.NOT_DUE})

  def test_unscheduled_weekday_is_not_due(self):
    monday_wednesday_friday = custom([0, 2, 4])
    states = states_by_date(resolve(schedule=monday_wednesday_friday))

    self.assertEqual(states[label(2026, 10, 6)], DayState.NOT_DUE)  # Tuesday
    self.assertEqual(states[label(2026, 10, 7)], DayState.MISSED)  # Wednesday

  def test_backwards_range_resolves_to_nothing(self):
    self.assertEqual(resolve(start=TODAY, end=WEEK_BACK), [])


class TargetHandlingTestCase(SimpleTestCase):
  def test_daily_target_of_three_needs_three_reps_for_a_hit(self):
    """`target_count` was previously ignored by both streaks and
    `completion_rate`, so a 3x/day habit with two reps scored a full hit."""
    daily_three = schedule('DAILY', target_count=3)

    self.assertEqual(
      states_by_date(resolve(schedule=daily_three, logs_by_day={TODAY: hit(2)}))[TODAY],
      DayState.PARTIAL,
    )
    self.assertEqual(
      states_by_date(resolve(schedule=daily_three, logs_by_day={TODAY: hit(3)}))[TODAY],
      DayState.HIT,
    )

  def test_daily_target_of_one_is_a_hit_with_a_single_rep(self):
    self.assertEqual(
      states_by_date(resolve(logs_by_day={TODAY: hit(1)}))[TODAY], DayState.HIT
    )

  def test_weekly_habit_hits_when_a_single_day_clears_one_rep(self):
    """The weekly target of three is spread across the week, so any one day
    doing the habit is a hit for that day."""
    states = states_by_date(
      resolve(schedule=weekly(3), logs_by_day={TODAY: hit(1)})
    )
    self.assertEqual(states[TODAY], DayState.HIT)


class PeriodBuildingTestCase(SimpleTestCase):
  def test_daily_habit_produces_one_period_per_day(self):
    days = resolve()
    periods = build_periods(days, schedule('DAILY'))

    self.assertEqual(len(periods), len(days))

  def test_weekly_habit_collapses_into_monday_anchored_weeks(self):
    """Oct 1 2026 is a Thursday, so a raw 8-day window spans two weeks, the
    first of which is truncated. `queries.analyse_habit` snaps the start back to
    Monday; this test pins the raw grouping behaviour."""
    days = resolve(schedule=weekly(3))
    periods = build_periods(days, weekly(3))

    self.assertEqual(len(periods), 2)
    self.assertEqual(periods[0].start, label(2026, 10, 1))
    self.assertEqual(periods[0].end, label(2026, 10, 4))
    self.assertEqual(periods[1].start, label(2026, 10, 5))

  def test_a_full_week_bucket_is_not_partial(self):
    days = resolve(start=label(2026, 9, 28), end=label(2026, 10, 4), schedule=weekly(3))
    periods = build_periods(days, weekly(3))

    self.assertEqual(len(periods), 1)
    self.assertFalse(periods[0].is_partial)

  def test_a_truncated_week_bucket_is_flagged_partial(self):
    """So the client can tell an incomplete week from a failed one."""
    days = resolve(schedule=weekly(3))
    periods = build_periods(days, weekly(3))

    self.assertTrue(periods[0].is_partial)
    self.assertTrue(periods[1].is_partial)

  def test_a_monday_aligned_weekly_window_produces_full_buckets(self):
    days = resolve(
      start=label(2026, 9, 28), end=label(2026, 10, 13), schedule=weekly(3)
    )
    periods = build_periods(days, weekly(3))

    self.assertEqual(len(periods), 3)
    self.assertEqual(calendar.weekday_of(periods[0].start), 0)
    self.assertEqual(periods[0].length, 7)
    self.assertFalse(periods[0].is_partial)
    self.assertFalse(periods[1].is_partial)
    self.assertTrue(periods[2].is_partial)

  def test_weekly_period_is_partial_until_the_weekly_target_is_reached(self):
    days = resolve(schedule=weekly(3), logs_by_day={label(2026, 10, 1): hit(1)})
    periods = build_periods(days, weekly(3))

    first = periods[0]
    self.assertEqual(first.state, DayState.PARTIAL)
    self.assertEqual(first.completed_total, 1)
    self.assertEqual(first.target, 3)

  def test_weekly_period_hits_once_the_weekly_target_is_reached(self):
    days = resolve(
      schedule=weekly(3),
      logs_by_day={label(2026, 10, 1): hit(1), label(2026, 10, 2): hit(1), label(2026, 10, 3): hit(1)},
    )
    periods = build_periods(days, weekly(3))

    self.assertEqual(periods[0].state, DayState.HIT)

  def test_weekly_period_is_not_due_when_nothing_was_scheduled(self):
    days = resolve(
      schedule=weekly(3),
      habit_start_date=label(2026, 10, 6),
    )
    periods = build_periods(days, weekly(3))

    self.assertEqual(periods[0].state, DayState.NOT_DUE)

  def test_weekly_period_reports_the_missed_week_as_missed(self):
    days = resolve(schedule=weekly(3))
    periods = build_periods(days, weekly(3))

    self.assertEqual(periods[0].state, DayState.MISSED)

  def test_period_credit_weights(self):
    monday_wednesday_friday = custom([0, 2, 4])
    days = resolve(
      schedule=monday_wednesday_friday,
      logs_by_day={label(2026, 10, 7): hit(1)},
    )
    periods = build_periods(days, monday_wednesday_friday)

    credits = {period.state: period.credit for period in periods}
    self.assertEqual(credits[DayState.HIT], 1.0)
    self.assertEqual(credits[DayState.MISSED], 0.0)
    self.assertIsNone(credits[DayState.NOT_DUE])
    self.assertFalse(periods[0].counts_towards_rate)


class LogNormalisationTestCase(SimpleTestCase):
  def test_midday_log_snaps_onto_the_same_day(self):
    """`HabitLog.date` has no midnight constraint, so a mid-day value must not
    silently split one calendar day across two labels."""
    midday = TODAY + (14 * 3600)

    states = states_by_date(resolve(logs_by_day={midday: hit(1)}))

    self.assertEqual(states[TODAY], DayState.HIT)
    self.assertNotIn(midday, states)