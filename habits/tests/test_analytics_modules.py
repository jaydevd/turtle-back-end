"""Tests for `patterns`, `trends`, `forecast` and `overview`.

The API tests prove each endpoint is wired up and shaped correctly; these pin
the arithmetic underneath, using `load_analyses` so they run against real rows
rather than hand-built stand-ins.
"""

import datetime as dt

from django.test import TestCase

from common.enums import RiskLevel, Status

from ..analytics import forecast, overview, patterns, trends
from ..analytics.calendar import SECONDS_PER_DAY, today_label
from ..analytics.queries import load_analyses
from .factories import (
  label,
  make_habit,
  make_log,
  make_schedule,
  make_tag,
)

TODAY = today_label()
WINDOW = 30


def window_start(days=WINDOW):
  return TODAY - (days - 1) * SECONDS_PER_DAY


class AnalyticsFixture(TestCase):
  """Builds habits through the real model layer and resolves them."""

  def setUp(self):
    from user.models import User

    self.user = User.objects.create_user(
      email='analytics@example.com', password='pw-for-tests-9'
    )
    self.tag = make_tag(self.user)
    self.today = today_label()

  def habit(
    self,
    name='Read',
    target_count=1,
    frequency_type='DAILY',
    weekdays=None,
    start_date=None,
  ):
    habit = make_habit(
      self.user,
      self.tag,
      name=name,
      start_date=start_date if start_date is not None else label(2024, 1, 1),
    )
    make_schedule(
      habit,
      frequency_type=frequency_type,
      target_count=target_count,
      weekdays=weekdays,
    )
    return habit

  def log_run(self, habit, count, end=None, status=Status.COMPLETED, completed_count=1):
    end = self.today if end is None else end
    for offset in range(count):
      make_log(
        habit,
        end - offset * SECONDS_PER_DAY,
        status=status,
        completed_count=completed_count,
      )

  def analyses(self, window_days=WINDOW):
    return load_analyses(
      self.user, today=self.today, window_days=window_days, history_days=730
    )


class WeekdayPatternTestCase(AnalyticsFixture):
  def test_weakest_weekday_is_found(self):
    """Never log Tuesdays, always log Mondays.

    `weekday_profile` deliberately reads the whole history - "you always miss
    Mondays" is a long-run claim - so the habit starts two months ago rather
    than in 2024.
    """
    habit = self.habit(start_date=self.today - 60 * SECONDS_PER_DAY)
    mondays = [
      day
      for day in (self.today - offset * SECONDS_PER_DAY for offset in range(56))
      if dt.datetime.fromtimestamp(day, dt.timezone.utc).weekday() == 0
    ]
    for day in mondays:
      make_log(habit, day)

    analysis = self.analyses()[0]
    profile = patterns.weekday_profile(analysis)

    self.assertEqual(profile[0]['weekday'], 0)
    self.assertGreater(profile[0]['completion_rate'], 0)
    # A daily habit has plenty of samples on every weekday, so Tuesday is
    # reliable - it is just at 0%. `weekday_extremes` is what refuses to call
    # a 0% weekday the "best" day.
    self.assertEqual(profile[1]['completion_rate'], 0.0)
    self.assertTrue(profile[0]['reliable'])
    self.assertTrue(profile[1]['reliable'])

    extremes = patterns.weekday_extremes(profile)
    self.assertEqual(extremes['best_weekday'], 0)
    self.assertEqual(extremes['worst_weekday'], 1)

  def test_best_and_worst_weekday_skip_thin_samples(self):
    """A rate computed from a single sample is not a habit, it is noise."""
    self.log_run(self.habit(start_date=self.today - SECONDS_PER_DAY), 1)

    extremes = patterns.weekday_extremes(patterns.weekday_profile(self.analyses()[0]))

    self.assertIsNone(extremes['best_weekday'])
    self.assertIsNone(extremes['worst_weekday'])

  def test_best_and_worst_weekday_appear_once_there_is_history(self):
    self.log_run(self.habit(start_date=self.today - 60 * SECONDS_PER_DAY), 30)

    extremes = patterns.weekday_extremes(patterns.weekday_profile(self.analyses()[0]))

    self.assertIsNotNone(extremes['best_weekday'])
    self.assertIsNotNone(extremes['worst_weekday'])

  def test_no_patterns_for_a_habit_with_no_history(self):
    self.habit()

    report = patterns.build_pattern_report(self.analyses()[0])

    self.assertIsNone(report['best_weekday'])
    self.assertIsNone(report['hour_of_day']['peak_hour'])


class HourOfDayTestCase(AnalyticsFixture):
  def test_peak_hour_comes_from_completed_at(self):
    habit = self.habit()
    # 09:30 IST == 04:00 UTC.
    for offset in range(5):
      make_log(
        habit,
        self.today - offset * SECONDS_PER_DAY,
        completed_at=dt.datetime(2026, 10, 1, 4, 0, tzinfo=dt.timezone.utc),
      )

    report = patterns.hour_of_day_profile(self.analyses()[0])

    self.assertEqual(report['peak_hour'], 9)  # 04:00 UTC is 09:30 IST
    self.assertEqual(report['sample_count'], 5)

  def test_logs_without_a_timestamp_do_not_corrupt_the_profile(self):
    """`completed_at` predates this work and is nullable, so older rows have
    none. They must be skipped, not bucketed as hour zero."""
    habit = self.habit()
    self.log_run(habit, 5)
    make_log(
      habit,
      self.today - 10 * SECONDS_PER_DAY,
      completed_at=None,
    )

    report = patterns.hour_of_day_profile(self.analyses()[0])

    self.assertIsNone(report['peak_hour'])
    self.assertEqual(report['sample_count'], 0)


class PunctualityTestCase(AnalyticsFixture):
  def test_ontime_share_is_measured_against_the_reminder(self):
    """`scheduled_time` is the only sub-day signal on the schedule."""
    habit = self.habit()
    habit.schedule.scheduled_time = dt.time(7, 0)
    habit.schedule.save()
    for offset in range(4):
      make_log(
        habit,
        self.today - offset * SECONDS_PER_DAY,
        completed_at=dt.datetime(2026, 10, 1, 1, 45, tzinfo=dt.timezone.utc),  # 07:15 IST
      )
    make_log(
      habit,
      self.today - 10 * SECONDS_PER_DAY,
      completed_at=dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc),  # 17:30 IST
    )

    report = patterns.punctuality(self.analyses()[0])

    self.assertTrue(report['available'])
    self.assertEqual(report['sample_count'], 5)
    self.assertEqual(report['on_time_count'], 4)
    self.assertEqual(report['on_time_rate'], 80.0)

  def test_no_reminder_means_no_punctuality_claim(self):
    self.log_run(self.habit(), 5)

    report = patterns.punctuality(self.analyses()[0])

    self.assertFalse(report['available'])
    self.assertIsNone(report['on_time_rate'])


class MonthlyPatternTestCase(AnalyticsFixture):
  def test_a_best_month_is_reported(self):
    self.log_run(self.habit(), 120)

    report = patterns.monthly_profile(self.analyses()[0])

    # One bucket per calendar month the history covers, not a fixed 12.
    self.assertGreaterEqual(len(report), 12)
    self.assertIn('month', report[0])
    self.assertIn('completion_rate', report[0])
    months = [row['month'] for row in report]
    self.assertEqual(months, sorted(months))


class TagPatternTestCase(AnalyticsFixture):
  def test_habits_are_rolled_up_per_tag(self):
    self.log_run(self.habit(name='Read'), 10)
    other_tag = make_tag(self.user, name='Mind')
    second = make_habit(self.user, other_tag, name='Meditate', start_date=label(2024, 1, 1))
    make_schedule(second)
    self.log_run(second, 5)

    rows = patterns.tag_profile(self.analyses())

    by_name = {row['tag_name']: row for row in rows}
    self.assertEqual(set(by_name), {'Health', 'Mind'})
    self.assertEqual(by_name['Health']['habit_count'], 1)
    self.assertEqual(by_name['Mind']['hit_days'], 5)


class TrendSeriesTestCase(AnalyticsFixture):
  def test_daily_rows_are_dense_and_ordered(self):
    self.log_run(self.habit(), 10)

    rows = trends.daily_series(self.analyses()[0], window_start(), self.today)

    self.assertEqual(len(rows), WINDOW)
    self.assertEqual([row['date'] for row in rows], sorted(row['date'] for row in rows))

  def test_weekly_rows_sum_the_days_they_cover(self):
    self.log_run(self.habit(), 30)

    raw = trends.daily_series(self.analyses()[0], window_start(), self.today)
    daily = trends.aggregate_series(raw, trends.GRANULARITY_DAY)
    weekly = trends.aggregate_series(raw, trends.GRANULARITY_WEEK)

    self.assertEqual(len(daily), WINDOW)
    for field in ('due_days', 'hit_days', 'partial_days', 'missed_days'):
      self.assertEqual(
        sum(row[field] for row in weekly),
        sum(row[field] for row in daily),
        msg=field,
      )

  def test_weeks_are_monday_anchored(self):
    """Buckets must line up with the Monday-anchored scoring periods, or a
    weekly habit's chart will not agree with its streak."""
    from ..analytics.calendar import weekday_of

    self.log_run(self.habit(), 30)

    weekly = trends.aggregate_series(
      trends.daily_series(self.analyses()[0], window_start(), self.today),
      trends.GRANULARITY_WEEK,
    )

    for row in weekly:
      self.assertEqual(weekday_of(row['start']), 0, msg=str(row['start']))

  def test_partial_credit_and_score_are_reported(self):
    habit = self.habit()
    self.log_run(habit, 5)
    make_log(habit, self.today - 10 * SECONDS_PER_DAY, status=Status.PARTIAL, completed_count=1)

    rows = trends.daily_series(self.analyses()[0], window_start(), self.today)
    by_date = {row['date']: row for row in rows}
    partial_day = self.today - 10 * SECONDS_PER_DAY

    self.assertEqual(by_date[partial_day]['state'], Status.PARTIAL)
    self.assertEqual(by_date[partial_day]['score'], 0.5)
    self.assertEqual(by_date[self.today]['score'], 1.0)
    self.assertTrue(by_date[self.today]['logged'])

  def test_heatmap_spans_the_trailing_year_without_gaps(self):
    self.log_run(self.habit(), 20)

    heatmap = trends.heatmap(self.analyses()[0])

    self.assertEqual(len(heatmap), 730)
    self.assertEqual(heatmap[-1]['date'], self.today)


class ForecastProbabilityTestCase(AnalyticsFixture):
  def test_a_perfect_recent_record_forecasts_high(self):
    self.log_run(self.habit(), 30)

    probability = forecast.completion_probability_today(self.analyses()[0], self.today)

    self.assertGreater(probability['probability'], 0.9)
    self.assertEqual(probability['risk'], RiskLevel.LOW)

  def test_an_abandoned_habit_forecasts_low(self):
    self.log_run(self.habit(), 30, end=self.today - 60 * SECONDS_PER_DAY)

    probability = forecast.completion_probability_today(self.analyses()[0], self.today)

    self.assertLess(probability['probability'], 0.4)
    self.assertEqual(probability['risk'], RiskLevel.HIGH)

  def test_a_long_history_of_failure_cannot_override_recent_success(self):
    """Regression guard: the weekday component once read all 730 days, so a
    user flawless for a month still forecast at their two-year-old rate."""
    habit = self.habit()
    self.log_run(habit, 30)
    # A scattering of old hits, so the lifetime weekday rates are poor.
    for offset in range(60, 700, 37):
      make_log(habit, self.today - offset * SECONDS_PER_DAY)

    probability = forecast.completion_probability_today(self.analyses()[0], self.today)

    self.assertGreater(probability['probability'], 0.8)

  def test_probability_stays_a_probability(self):
    self.log_run(self.habit(), 10)

    probability = forecast.completion_probability_today(self.analyses()[0], self.today)['probability']

    self.assertGreaterEqual(probability, 0.0)
    self.assertLessEqual(probability, 1.0)

  def test_a_thin_history_reports_insufficient_data(self):
    """Two days of history is not enough to claim a confident forecast."""
    self.log_run(self.habit(start_date=self.today - 2 * SECONDS_PER_DAY), 2)

    probability = forecast.completion_probability_today(self.analyses()[0], self.today)

    self.assertFalse(probability['sufficient_data'])


class RiskBucketsTestCase(AnalyticsFixture):
  def test_risk_thresholds_are_inclusive_at_the_right_edges(self):
    self.assertEqual(forecast.risk_level(0.0), RiskLevel.HIGH)
    self.assertEqual(forecast.risk_level(0.39), RiskLevel.HIGH)
    self.assertEqual(forecast.risk_level(0.4), RiskLevel.MEDIUM)
    self.assertEqual(forecast.risk_level(0.69), RiskLevel.MEDIUM)
    self.assertEqual(forecast.risk_level(0.7), RiskLevel.LOW)
    self.assertEqual(forecast.risk_level(1.0), RiskLevel.LOW)

  def test_a_habit_going_quiet_appears_in_decaying(self):
    """Decay is measured against the habit's own previous window, so the run
    has to sit entirely in that window: the last 30 days are now empty."""
    self.log_run(self.habit(), 40, end=self.today - 31 * SECONDS_PER_DAY)

    analyses = self.analyses()
    decaying = forecast.decaying_habits(analyses)

    self.assertEqual(len(decaying), 1)
    self.assertEqual(decaying[0]['completion_rate'], 0.0)
    self.assertGreater(decaying[0]['previous_completion_rate'], 90)
    self.assertLess(decaying[0]['rate_delta'], 0)

  def test_load_projection_counts_only_unfinished_habits(self):
    done = self.habit(name='Read')
    pending = self.habit(name='Run')
    self.log_run(done, 30)
    self.log_run(pending, 29, end=self.today - SECONDS_PER_DAY)

    projection = forecast.load_projection(self.analyses(), self.today)

    self.assertEqual(projection['outstanding_count'], 1)
    self.assertFalse(projection['overloaded'])

  def test_summary_totals_match_the_lists(self):
    self.log_run(self.habit(), 30, end=self.today - 60 * SECONDS_PER_DAY)
    self.log_run(self.habit(name='Run'), 30)

    report = forecast.build_risk_report(self.analyses(), self.today)
    summary = forecast.summarise_risk(report)

    self.assertEqual(summary['decaying_habits'], len(report['decaying_habits']))
    self.assertEqual(summary['streaks_at_risk'], len(report['streaks_at_risk']))
    self.assertEqual(
      summary['high_risk_habits'],
      len([row for row in report['completion_probabilities'] if row['risk'] == RiskLevel.HIGH]),
    )


class OverviewTotalsTestCase(AnalyticsFixture):
  def test_a_day_every_habit_hits_is_a_perfect_day(self):
    first = self.habit(name='Read')
    second = self.habit(name='Run')
    self.log_run(first, 10)
    self.log_run(second, 10)

    data = overview.build_overview(self.analyses(), self.today, window_start())

    self.assertEqual(data['perfect_days'], 10)
    self.assertEqual(data['active_days'], 10)
    self.assertEqual(data['zero_days'], 20)
    self.assertEqual(data['habit_count'], 2)

  def test_a_zero_day_counts_as_active_but_not_perfect(self):
    """The distinction the overview exists to draw: you showed up, you just
    did not finish everything."""
    first = self.habit(name='Read')
    second = self.habit(name='Run')
    self.log_run(first, 10)
    self.log_run(second, 5, end=self.today - 5 * SECONDS_PER_DAY)

    data = overview.build_overview(self.analyses(), self.today, window_start())

    self.assertEqual(data['perfect_days'], 5)
    self.assertEqual(data['active_days'], 10)
    self.assertEqual(data['zero_days'], 20)

  def test_top_and_bottom_never_double_count_a_habit(self):
    """With fewer habits than the limit, slicing both ends would list every
    habit twice - once as a win, once as a loss."""
    first = self.habit(name='Read')
    second = self.habit(name='Run')
    self.log_run(first, 30)
    self.log_run(second, 10)

    data = overview.build_overview(self.analyses(), self.today, window_start())

    top = [row['id'] for row in data['top_habits']]
    bottom = [row['id'] for row in data['bottom_habits']]
    self.assertEqual(set(top) & set(bottom), set())
    self.assertEqual(len(top) + len(bottom), data['habit_count'])

  def test_trend_direction_is_reported_against_the_previous_window(self):
    # Solid in the current window, nothing at all in the one before it.
    self.log_run(self.habit(name='Read'), 20)

    data = overview.build_overview(
      self.analyses(),
      self.today,
      window_start(),
      previous_start=window_start() - WINDOW * SECONDS_PER_DAY,
    )

    self.assertEqual(data['previous_completion_rate'], 0.0)
    self.assertGreater(data['completion_rate'], 0.0)
    self.assertGreater(data['rate_delta'], 0)

  def test_empty_account(self):
    data = overview.build_overview([], self.today, window_start())

    self.assertEqual(data['habit_count'], 0)
    self.assertIsNone(data['completion_rate'])
    self.assertEqual(data['top_habits'], [])
