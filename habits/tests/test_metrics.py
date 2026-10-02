"""Tests for `habits/analytics/metrics.py`.

Several of these exist specifically to pin bugs the previous implementation in
`habits/views.py` had, so a regression cannot come back unnoticed:

* streaks were computed from a 30-day log slice, capping `current_streak` at 30,
* the `scheduled_days` denominator was off by one,
* `target_count` above one was ignored,
* `PARTIAL` days were invisible to every calculation,
* non-due days broke a chain instead of being transparent,
* the in-progress day read as a miss, zeroing every streak at local midnight.
"""

from django.test import SimpleTestCase

from common.enums import Status, TrendDirection

from ..analytics import metrics
from ..analytics.calendar import weekday_of
from ..analytics.resolver import build_periods, resolve_day_states
from .factories import custom, hit, label, log, schedule, weekly

TODAY = label(2026, 10, 8)
HISTORY = label(2026, 1, 1)


def periods_for(logs_by_day=None, sched=None, start=None, end=TODAY):
  """Resolve `logs_by_day` into scoring periods.

  With no explicit `start`, the range begins at the earliest logged day, so a
  test that only supplies hits does not silently resolve months of leading
  misses in front of them.
  """
  if start is None:
    start = min(logs_by_day) if logs_by_day else HISTORY

  days = resolve_day_states(
    today=TODAY,
    start=start,
    end=end,
    schedule=sched,
    status=Status.ACTIVE,
    logs_by_day=logs_by_day or {},
  )
  return build_periods(days, sched)


def all_days_hit(start=HISTORY, end=TODAY, sched=None, every=1):
  labels = [
    start + offset * 86400 for offset in range((end - start) // 86400 + 1)
  ]
  return {
    day: hit(1)
    for offset, day in enumerate(labels)
    if offset % every == 0
  }


class RateTestCase(SimpleTestCase):
  def test_all_days_hit_is_a_full_rate(self):
    periods = periods_for(all_days_hit(start=label(2026, 10, 1)))

    self.assertEqual(metrics.completion_rate(periods), 100.0)

  def test_no_days_hit_is_zero(self):
    self.assertEqual(metrics.completion_rate(periods_for(start=label(2026, 10, 1))), 0.0)

  def test_partial_day_is_worth_half(self):
    periods = periods_for(
      {label(2026, 10, 1): hit(1), label(2026, 10, 2): log(Status.PARTIAL, 1)},
      start=label(2026, 10, 1),
      end=label(2026, 10, 2),
    )

    self.assertEqual(metrics.completion_rate(periods), 75.0)

  def test_nothing_scheduled_reports_none_not_zero(self):
    """'You did nothing' and 'you were never asked to do anything' are
    different answers, and conflating them is what produced a 0% tile."""
    # 2026-10-06 is a Tuesday, which the Mon-only custom schedule skips.
    periods = periods_for(
      {},
      sched=custom([0]),
      start=label(2026, 10, 6),
      end=label(2026, 10, 6),
    )

    self.assertIsNone(metrics.completion_rate(periods))
    self.assertIsNone(metrics.strict_rate(periods))

  def test_skipped_days_leave_the_denominator(self):
    """Deliberately skipping is not a failure."""
    periods = periods_for(
      {label(2026, 10, 1): hit(1), label(2026, 10, 2): log(Status.SKIPPED, 0)},
      start=label(2026, 10, 1),
      end=label(2026, 10, 2),
    )

    counts = metrics.count_states(periods)
    self.assertEqual(counts['scored'], 1)
    self.assertEqual(counts['skipped'], 1)
    self.assertEqual(metrics.completion_rate(periods), 100.0)

  def test_strict_rate_ignores_partial_credit(self):
    periods = periods_for(
      {label(2026, 10, 1): hit(1), label(2026, 10, 2): log(Status.PARTIAL, 1)},
      start=label(2026, 10, 1),
      end=label(2026, 10, 2),
    )

    self.assertEqual(metrics.completion_rate(periods), 75.0)
    self.assertEqual(metrics.strict_rate(periods), 50.0)

  def test_target_adherence_exposes_a_reps_shortfall(self):
    """The one number that shows a 3x/day habit falling short on reps, which
    `completion_rate` hides once the client records the final absolute count."""
    daily_three = schedule('DAILY', target_count=3)
    periods = periods_for(
      {label(2026, 10, 1): hit(2), label(2026, 10, 2): hit(3)},
      sched=daily_three,
      start=label(2026, 10, 1),
      end=label(2026, 10, 2),
    )

    self.assertEqual(metrics.completion_rate(periods), 75.0)
    self.assertEqual(metrics.target_adherence(periods), 83.3)


class StreakTestCase(SimpleTestCase):
  def test_current_streak_survives_todays_in_progress_day(self):
    """Without `is_in_progress`, every current streak would read 0 for the first
    24 hours after local midnight."""
    periods = periods_for(
      all_days_hit(start=label(2026, 10, 1), end=label(2026, 10, 7)),
    )

    self.assertEqual(metrics.compute_streaks(periods, TODAY)['current_streak'], 7)

  def test_current_streak_breaks_on_a_missed_yesterday(self):
    periods = periods_for(
      all_days_hit(start=label(2026, 10, 1), end=label(2026, 10, 6)),
    )

    self.assertEqual(metrics.compute_streaks(periods, TODAY)['current_streak'], 0)

  def test_current_streak_ignores_not_due_days(self):
    """Mon/Wed/Fri habits must not lose their streak on Tuesdays.

    The original maths also skipped non-due days, but the window bug meant it
    only ever had 30 days to work with; this pins the behaviour at any depth.
    """
    monday_wednesday_friday = custom([0, 2, 4])
    start = label(2026, 9, 21)  # a Monday
    hit_days = [
      start + offset * 86400
      for offset in range(18)
      if weekday_of(start + offset * 86400) in (0, 2, 4)
    ]
    periods = periods_for(
      {day: hit(1) for day in hit_days},
      sched=monday_wednesday_friday,
      start=start,
    )

    self.assertEqual(
      metrics.compute_streaks(periods, TODAY)['current_streak'], len(hit_days)
    )

  def test_partial_days_do_not_break_a_streak(self):
    periods = periods_for(
      {
        label(2026, 10, 6): hit(1),
        label(2026, 10, 7): hit(1),
        label(2026, 10, 8): log(Status.PARTIAL, 1),
      },
      start=label(2026, 10, 6),
    )

    streaks = metrics.compute_streaks(periods, TODAY)
    self.assertEqual(streaks['current_streak'], 2)
    self.assertEqual(streaks['partial_in_current_streak'], 1)

  def test_skipped_days_do_not_break_a_streak(self):
    periods = periods_for(
      {
        label(2026, 10, 6): hit(1),
        label(2026, 10, 7): hit(1),
        label(2026, 10, 8): log(Status.SKIPPED, 0),
      },
      start=label(2026, 10, 6),
    )

    self.assertEqual(metrics.compute_streaks(periods, TODAY)['current_streak'], 2)

  def test_streaks_exceed_thirty_days(self):
    """The original implementation derived streaks from a 30-day log slice, so
    `current_streak` could never exceed 30."""
    start = label(2026, 6, 1)
    periods = periods_for(all_days_hit(start=start), start=start)

    streaks = metrics.compute_streaks(periods, TODAY)

    self.assertGreater(streaks['current_streak'], 30)
    self.assertEqual(streaks['current_streak'], (TODAY - start) // 86400 + 1)

  def test_best_streak_is_found_beyond_the_window(self):
    """A 40-day run in spring, then nothing recent: the best streak must still
    be 40 even with a 30-day reporting window."""
    spring_start = label(2026, 4, 1)
    spring_end = label(2026, 5, 10)
    logs = all_days_hit(start=spring_start, end=spring_end)
    logs.update(all_days_hit(start=label(2026, 10, 7), end=TODAY))

    periods = periods_for(logs, start=spring_start)
    streaks = metrics.compute_streaks(periods, TODAY)

    self.assertEqual(streaks['best_streak'], (spring_end - spring_start) // 86400 + 1)
    self.assertEqual(streaks['best_streak_start'], spring_start)
    self.assertEqual(streaks['best_streak_end'], spring_end)

  def test_best_streak_start_and_end_are_reported(self):
    start = label(2026, 6, 1)
    periods = periods_for(all_days_hit(start=start), start=start)

    streaks = metrics.compute_streaks(periods, TODAY)

    self.assertEqual(streaks['best_streak_start'], start)
    self.assertEqual(streaks['best_streak_end'], TODAY)

  def test_no_hits_gives_zero_streaks_not_an_error(self):
    streaks = metrics.compute_streaks(periods_for(start=label(2026, 10, 1)), TODAY)

    self.assertEqual(streaks['current_streak'], 0)
    self.assertEqual(streaks['best_streak'], 0)
    self.assertIsNone(streaks['current_streak_start'])

  def test_weekly_streak_counts_weeks_not_days(self):
    three_a_week = weekly(3)
    monday = label(2026, 10, 5)
    logs = {monday + offset * 86400: hit(1) for offset in (0, 1, 2)}

    periods = periods_for(logs, sched=three_a_week, start=monday)

    self.assertEqual(metrics.compute_streaks(periods, TODAY)['current_streak'], 1)


class LapseTestCase(SimpleTestCase):
  def test_contiguous_misses_form_one_run(self):
    """Oct 2-3 is one lapse, not two separate one-day failures."""
    periods = periods_for(
      {
        label(2026, 10, 1): hit(1),
        label(2026, 10, 4): hit(1),
        label(2026, 10, 8): hit(1),
      },
      start=label(2026, 10, 1),
    )

    lapses = metrics.compute_lapses(periods, TODAY)

    self.assertEqual([lapse['length'] for lapse in lapses], [2, 3])
    self.assertEqual(lapses[0]['start'], label(2026, 10, 2))
    self.assertEqual(lapses[0]['end'], label(2026, 10, 3))

  def test_separate_lapses_are_counted_separately(self):
    periods = periods_for(
      {
        label(2026, 10, 1): hit(1),
        label(2026, 10, 3): hit(1),
        label(2026, 10, 6): hit(1),
      },
      start=label(2026, 10, 1),
    )

    lapses = metrics.compute_lapses(periods, TODAY)

    # Oct 2 alone, then Oct 4-5, then Oct 7-8 which runs into today.
    self.assertEqual([lapse['length'] for lapse in lapses], [1, 2, 2])

  def test_a_lapse_covering_today_is_flagged_in_progress(self):
    periods = periods_for(
      {label(2026, 10, 6): hit(1)},
      start=label(2026, 10, 6),
    )

    lapses = metrics.compute_lapses(periods, TODAY)

    self.assertTrue(lapses[-1]['in_progress'])

  def test_a_settled_lapse_is_not_flagged(self):
    periods = periods_for(
      {label(2026, 10, 1): hit(1), label(2026, 10, 8): hit(1)},
      start=label(2026, 10, 1),
    )

    lapses = metrics.compute_lapses(periods, TODAY)

    self.assertFalse(lapses[-1]['in_progress'])


class VolatilityTestCase(SimpleTestCase):
  def test_a_steady_habit_has_low_stddev(self):
    periods = periods_for(all_days_hit(start=label(2026, 10, 1)))

    self.assertEqual(metrics.volatility(periods)['rate_stddev'], 0.0)

  def test_an_erratic_habit_is_less_stable_than_a_steady_one(self):
    """Hitting every third day is far less stable than hitting every day."""
    steady = metrics.volatility(
      periods_for(all_days_hit(start=label(2026, 10, 1)))
    )['rate_stddev']
    erratic = metrics.volatility(
      periods_for(all_days_hit(start=label(2026, 10, 1), every=3))
    )['rate_stddev']

    self.assertGreater(erratic, steady)

  def test_max_gap_days_is_the_longest_run_between_hits(self):
    periods = periods_for(
      {
        label(2026, 10, 1): hit(1),
        label(2026, 10, 8): hit(1),
      },
      start=label(2026, 10, 1),
    )

    self.assertEqual(metrics.volatility(periods)['max_gap_days'], 7)

  def test_no_history_reports_nulls(self):
    self.assertIsNone(metrics.volatility([])['rate_stddev'])


class RecencyTestCase(SimpleTestCase):
  def test_days_since_last_hit(self):
    periods = periods_for(
      {label(2026, 10, 6): hit(1)},
      start=label(2026, 10, 6),
    )

    self.assertEqual(metrics.recency(periods, TODAY)['days_since_last_hit'], 2)
    self.assertEqual(metrics.recency(periods, TODAY)['last_hit_date'], label(2026, 10, 6))

  def test_no_hits_reports_none(self):
    periods = periods_for(start=label(2026, 10, 6))

    self.assertIsNone(metrics.recency(periods, TODAY)['days_since_last_hit'])


class ConsistencyScoreTestCase(SimpleTestCase):
  def test_perfect_habit_scores_near_the_top(self):
    periods = periods_for(all_days_hit(start=label(2026, 10, 1)))

    result = metrics.consistency_score(periods, TODAY)

    self.assertGreater(result['consistency_score'], 90)

  def test_empty_window_scores_zero(self):
    result = metrics.consistency_score([], TODAY)

    self.assertEqual(result['consistency_score'], 0.0)

  def test_components_are_returned_with_the_score(self):
    """A composite score is only trustworthy if the user can see why it landed
    where it did."""
    result = metrics.consistency_score(
      periods_for(all_days_hit(start=label(2026, 10, 1))), TODAY
    )

    self.assertEqual(
      set(result['consistency_components']),
      set(metrics.SCORE_WEIGHTS),
    )
    self.assertEqual(result['scoring_version'], metrics.SCORING_VERSION)

  def test_a_stale_habit_scores_below_a_fresh_one(self):
    stale = metrics.consistency_score(
      periods_for(all_days_hit(start=label(2026, 9, 1), end=label(2026, 9, 5))), TODAY
    )['consistency_score']
    fresh = metrics.consistency_score(
      periods_for(all_days_hit(start=label(2026, 10, 1))), TODAY
    )['consistency_score']

    self.assertLess(stale, fresh)

  def test_all_components_stay_within_zero_and_one(self):
    for logs in (all_days_hit(start=label(2026, 10, 1)), {}):
      components = metrics.consistency_score(
        periods_for(logs, start=label(2026, 10, 1)), TODAY
      )['consistency_components']
      for name, value in components.items():
        self.assertGreaterEqual(value, 0.0, msg=name)
        self.assertLessEqual(value, 1.0, msg=name)


class TrendTestCase(SimpleTestCase):
  def test_improving_stable_and_declining(self):
    self.assertEqual(metrics.trend_direction(20.0), TrendDirection.IMPROVING)
    self.assertEqual(metrics.trend_direction(0.0), TrendDirection.STABLE)
    self.assertEqual(metrics.trend_direction(-30.0), TrendDirection.DECLINING)

  def test_no_comparison_reports_insufficient_data(self):
    self.assertEqual(
      metrics.trend_direction(None), TrendDirection.INSUFFICIENT_DATA
    )


class MilestoneTestCase(SimpleTestCase):
  def test_next_milestone_is_the_nearest_unpassed_threshold(self):
    self.assertEqual(metrics.next_milestone(12)['threshold'], 30)
    self.assertEqual(metrics.next_milestone(30)['threshold'], 50)
    self.assertEqual(metrics.next_milestone(400)['remaining'], 0)

  def test_progress_is_reported_for_the_client_to_render(self):
    self.assertEqual(metrics.next_milestone(15)['remaining'], 15)
    self.assertEqual(metrics.next_milestone(15)['progress'], 0.5)


class BuildHabitMetricsTestCase(SimpleTestCase):
  def test_streaks_come_from_history_and_rates_from_the_window(self):
    """Mixing the two ranges is how a 30-day window once capped every streak."""
    spring_start = label(2026, 4, 1)
    spring_end = label(2026, 5, 10)
    logs = all_days_hit(start=spring_start, end=spring_end)

    history = periods_for(logs, start=spring_start)
    window = periods_for(logs, start=label(2026, 9, 9))

    result = metrics.build_habit_metrics(
      periods=history,
      window_periods=window,
      previous_periods=[],
      today=TODAY,
      total_completions=40,
    )

    self.assertEqual(result['best_streak'], 40)
    self.assertIsNotNone(result['completion_rate'])
    self.assertEqual(result['next_milestone']['completed'], 40)

  def test_rate_delta_compares_against_the_previous_window(self):
    improving = periods_for(all_days_hit(start=label(2026, 9, 9)))
    window = periods_for(all_days_hit(start=label(2026, 10, 1)))

    result = metrics.build_habit_metrics(
      periods=window,
      window_periods=window,
      previous_periods=improving,
      today=TODAY,
    )

    self.assertEqual(result['previous_completion_rate'], 100.0)
    self.assertEqual(result['trend'], TrendDirection.STABLE)

  def test_no_previous_window_leaves_the_delta_null(self):
    window = periods_for(all_days_hit(start=label(2026, 10, 1)))

    result = metrics.build_habit_metrics(
      periods=window, window_periods=window, previous_periods=[], today=TODAY
    )

    self.assertIsNone(result['rate_delta'])
    self.assertEqual(result['trend'], TrendDirection.INSUFFICIENT_DATA)


def calendar_weekday(day_label):
  from ..analytics.calendar import weekday_of

  return weekday_of(day_label)