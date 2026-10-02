"""API tests for the analytics endpoints.

These cover the parts unit tests cannot: URL routing (a literal prefix placed
below `<uuid:pk>` silently 404s), the `{status, message, data}` envelope, the
`?window=` / `?habit_id=` contract, and tenant isolation.
"""

from django.urls import reverse
from rest_framework import status as http
from rest_framework.test import APITestCase

from common.enums import Status

from .factories import (
  consecutive_days,
  label,
  make_habit,
  make_log,
  make_schedule,
  make_tag,
)

TODAY = label(2026, 10, 8)


def today_label():
  """The current IST day, read the same way the views read it."""
  from ..analytics.calendar import today_label as _today

  return _today()


class AnalyticsApiTestCase(APITestCase):
  def setUp(self):
    self.user = self.make_user('reader@example.com')
    self.other = self.make_user('stranger@example.com')
    self.tag = make_tag(self.user)
    self.today = today_label()
    self.client.force_authenticate(self.user)

  def make_user(self, email):
    from user.models import User

    return User.objects.create_user(email=email, password='pw-for-tests-9')

  def make_active_habit(self, name='Read', target_count=1, frequency_type='DAILY'):
    habit = make_habit(self.user, self.tag, name=name, start_date=label(2024, 1, 1))
    make_schedule(habit, frequency_type=frequency_type, target_count=target_count)
    return habit

  def log_days(self, habit, count, end=None, status=Status.COMPLETED, completed_count=1):
    """Log `count` consecutive days ending at `end` (default: today)."""
    end = self.today if end is None else end
    for day in consecutive_days(end, count):
      make_log(
        habit, day, status=status, completed_count=completed_count
      )

  def get(self, name, **params):
    response = self.client.get(reverse(name), params)
    self.assertEqual(
      response.status_code, http.HTTP_200_OK, msg=f'{name} -> {response.data}'
    )
    self.assertIn('status', response.data)
    self.assertIn('message', response.data)
    self.assertIn('data', response.data)
    self.assertEqual(response.data['status'], http.HTTP_200_OK)
    return response.data['data']


class StatsEndpointTestCase(AnalyticsApiTestCase):
  def test_preserves_the_existing_response_contract(self):
    """The habit cards already consume these keys, so none may disappear."""
    habit = self.make_active_habit()
    self.log_days(habit, 5)

    rows = self.get('habit-stats')['results']

    self.assertEqual(len(rows), 1)
    row = rows[0]
    for key in (
      'id', 'name', 'color', 'icon', 'tag', 'status', 'current_streak',
      'best_streak', 'total_completions', 'completion_rate', 'scheduled_days',
      'logged_days', 'due_today',
    ):
      self.assertIn(key, row)
    self.assertEqual(row['id'], str(habit.id))

  def test_adds_the_new_analytics_keys(self):
    self.make_active_habit()

    row = self.get('habit-stats')['results'][0]

    for key in (
      'consistency_score', 'consistency_components', 'scoring_version', 'strict_rate',
      'target_adherence', 'score_unit', 'daily_target', 'hit_days', 'partial_days',
      'missed_days', 'skipped_days', 'scored_days', 'previous_completion_rate',
      'rate_delta', 'trend', 'current_streak_start', 'best_streak_start',
      'best_streak_end', 'partial_in_current_streak', 'days_since_last_hit',
      'last_hit_date', 'volatility', 'lapse_count', 'longest_lapse',
      'next_milestone',
    ):
      self.assertIn(key, row)

  def test_reports_the_requested_window(self):
    self.make_active_habit()

    seven = self.get('habit-stats', window=7)
    thirty = self.get('habit-stats', window=30)

    self.assertEqual(seven['window_days'], 7)
    self.assertEqual(thirty['window_days'], 30)
    self.assertLessEqual(seven['results'][0]['scheduled_days'], 7)
    self.assertLessEqual(thirty['results'][0]['scheduled_days'], 30)

  def test_logged_days_never_exceeds_the_window(self):
    """`logged_days` and `scheduled_days` share a denominator basis. A habit
    logged for years used to report a `logged_days` far past the window."""
    habit = self.make_active_habit()
    self.log_days(habit, 400)

    row = self.get('habit-stats', window=30)['results'][0]

    self.assertLessEqual(row['logged_days'], row['scheduled_days'])

  def test_current_streak_survives_the_unfinished_today(self):
    self.log_days(self.make_active_habit(), 5)

    row = self.get('habit-stats')['results'][0]

    self.assertEqual(row['current_streak'], 5)

  def test_a_multi_reps_target_is_scored_on_reps(self):
    habit = self.make_active_habit(target_count=3)
    self.log_days(habit, 3, completed_count=3)
    make_log(habit, self.today - 3 * 86400, status=Status.PARTIAL, completed_count=1)

    row = self.get('habit-stats', window=7)['results'][0]

    self.assertEqual(row['daily_target'], 3)
    self.assertEqual(row['scheduled_days'], 7)
    self.assertEqual(row['hit_days'], 3)
    self.assertEqual(row['partial_days'], 1)
    self.assertLess(row['completion_rate'], 100.0)
    self.assertLess(row['target_adherence'], 100.0)

  def test_a_weekly_habit_reports_the_week_as_the_score_unit(self):
    self.make_active_habit(frequency_type='WEEKLY', target_count=3)

    row = self.get('habit-stats', window=30)['results'][0]

    self.assertEqual(row['score_unit'], 'WEEK')
    self.assertLessEqual(row['scored_days'], 6)

  def test_skipped_days_are_not_misses(self):
    """Deliberately skipping is not a failure: it leaves the denominator
    instead of counting against the rate."""
    habit = self.make_active_habit()
    self.log_days(habit, 7, end=self.today - 86400)
    make_log(habit, self.today, status=Status.SKIPPED, completed_count=0)

    row = self.get('habit-stats', window=7)['results'][0]

    self.assertEqual(row['skipped_days'], 1)
    self.assertEqual(row['missed_days'], 0)
    self.assertEqual(row['hit_days'], 6)
    self.assertEqual(row['scheduled_days'], 6)
    self.assertEqual(row['completion_rate'], 100.0)

  def test_a_habit_with_no_logs_reports_zero_not_a_crash(self):
    self.make_active_habit()

    row = self.get('habit-stats')['results'][0]

    self.assertEqual(row['completion_rate'], 0.0)
    self.assertEqual(row['current_streak'], 0)
    self.assertEqual(row['consistency_score'], 0.0)

  def test_a_completed_habit_is_still_listed_but_stops_scoring(self):
    """The dashboard filters to ACTIVE client-side, so `/stats/` keeps listing
    retired habits; the resolver just stops asking for them."""
    habit = self.make_active_habit()
    self.log_days(habit, 5)
    habit.status = Status.COMPLETED
    habit.save()

    row = self.get('habit-stats')['results'][0]

    self.assertEqual(row['status'], Status.COMPLETED)
    self.assertIsNone(row['completion_rate'])

  def test_a_soft_deleted_habit_is_excluded(self):
    habit = self.make_active_habit()
    habit.is_deleted = True
    habit.save()

    self.assertEqual(self.get('habit-stats')['results'], [])

  def test_another_users_habits_are_never_returned(self):
    self.make_active_habit(name='Mine')

    self.client.force_authenticate(self.other)
    self.assertEqual(self.get('habit-stats')['results'], [])

  def test_anonymous_access_is_rejected(self):
    self.make_active_habit()
    self.client.force_authenticate(None)

    response = self.client.get(reverse('habit-stats'))

    self.assertIn(
      response.status_code, (http.HTTP_401_UNAUTHORIZED, http.HTTP_403_FORBIDDEN)
    )

  def test_an_unsupported_window_is_rejected_with_411(self):
    self.make_active_habit()

    response = self.client.get(reverse('habit-stats'), {'window': 13})

    self.assertEqual(response.status_code, http.HTTP_411_LENGTH_REQUIRED)


class OverviewEndpointTestCase(AnalyticsApiTestCase):
  def test_aggregates_across_habits(self):
    first = self.make_active_habit(name='Read')
    second = self.make_active_habit(name='Run')
    self.log_days(first, 3)
    self.log_days(second, 2)

    data = self.get('habit-insights-overview', window=7)

    self.assertEqual(data['window_days'], 7)
    self.assertEqual(data['habit_count'], 2)
    self.assertIsNotNone(data['completion_rate'])
    self.assertEqual(data['due_days'], 7)
    self.assertEqual(len(data['top_habits']) + len(data['bottom_habits']), 2)

  def test_compares_against_the_previous_window(self):
    habit = self.make_active_habit()
    self.log_days(habit, 3)

    data = self.get('habit-insights-overview', window=7)

    self.assertIsNotNone(data['previous_completion_rate'])
    self.assertIn('rate_delta', data)

  def test_empty_for_a_user_with_no_habits(self):
    data = self.get('habit-insights-overview')

    self.assertEqual(data['habit_count'], 0)
    self.assertIsNone(data['completion_rate'])

  def test_rejects_a_bad_window(self):
    response = self.client.get(reverse('habit-insights-overview'), {'window': 0})

    self.assertEqual(response.status_code, http.HTTP_411_LENGTH_REQUIRED)


class PatternsEndpointTestCase(AnalyticsApiTestCase):
  def test_returns_weekday_and_hour_profiles(self):
    habit = self.make_active_habit()
    self.log_days(habit, 30)

    data = self.get('habit-insights-patterns', habit_id=habit.id)

    self.assertEqual(len(data['weekday_profile']), 7)
    self.assertEqual(len(data['habits']), 1)
    report = data['habits'][0]
    self.assertGreater(len(report['hour_of_day']), 0)
    hours = report['hour_of_day']['buckets']
    self.assertEqual(len(hours), 24)
    self.assertEqual([bucket['hour'] for bucket in hours], list(range(24)))
    self.assertIn('best_weekday', report)
    self.assertIn('punctuality', report)
    self.assertIn('monthly_profile', report)

  def test_aggregates_across_habits_when_no_habit_is_given(self):
    first = self.make_active_habit(name='Read')
    second = self.make_active_habit(name='Run')
    self.log_days(first, 20)
    self.log_days(second, 20)

    data = self.get('habit-insights-patterns')

    self.assertEqual(len(data['weekday_profile']), 7)
    self.assertEqual(len(data['habits']), 2)
    self.assertEqual(len(data['tag_profile']), 1)

  def test_an_unknown_habit_id_is_a_404(self):
    self.make_active_habit()

    response = self.client.get(
      reverse('habit-insights-patterns'),
      {'habit_id': '00000000-0000-4000-8000-000000000000'},
    )

    self.assertEqual(response.status_code, http.HTTP_404_NOT_FOUND)

  def test_another_users_habit_id_is_a_404(self):
    """Not a 403: the habit simply is not part of this user's world."""
    self.make_active_habit()

    response = self.client.get(
      reverse('habit-insights-patterns'),
      {'habit_id': self.other_habit_id()},
    )

    self.assertEqual(response.status_code, http.HTTP_404_NOT_FOUND)

  def other_habit_id(self):
    other_tag = make_tag(self.other, name='Theirs')
    return make_habit(self.other, other_tag, start_date=label(2024, 1, 1)).id


class TrendEndpointTestCase(AnalyticsApiTestCase):
  def test_daily_series_covers_the_whole_window_without_gaps(self):
    self.log_days(self.make_active_habit(), 10)

    data = self.get('habit-insights-trend', window=14)

    self.assertEqual(data['granularity'], 'day')
    self.assertEqual(len(data['combined']), 14)
    dates = [row['date'] for row in data['combined']]
    self.assertEqual(dates, sorted(dates))
    self.assertEqual(data['habits'][0]['granularity'], 'day')

  def test_weekly_granularity_is_accepted(self):
    self.log_days(self.make_active_habit(), 40)

    data = self.get('habit-insights-trend', window=90, granularity='week')

    self.assertEqual(data['granularity'], 'week')
    self.assertGreater(len(data['habits'][0]['weekly']), 0)
    self.assertGreater(len(data['habits'][0]['daily']), 0)

  def test_an_unknown_granularity_is_rejected(self):
    response = self.client.get(
      reverse('habit-insights-trend'), {'granularity': 'FORTNIGHT'}
    )

    self.assertEqual(response.status_code, http.HTTP_411_LENGTH_REQUIRED)

  def test_heatmap_covers_the_trailing_year(self):
    self.log_days(self.make_active_habit(), 20)

    data = self.get('habit-insights-trend')

    heatmap = data['habits'][0]['heatmap']
    self.assertEqual(len(heatmap), 730)
    self.assertEqual(heatmap[-1]['date'], self.today)
    dates = [row['date'] for row in heatmap]
    self.assertEqual(dates, sorted(dates))
    self.assertEqual(
      len({row['weekday'] for row in heatmap}), 7, msg='all seven weekdays present'
    )


class RiskEndpointTestCase(AnalyticsApiTestCase):
  def test_flags_a_live_streak_that_today_could_still_break(self):
    """Logged through yesterday: the streak is intact, so today is the day it
    dies if the user does nothing."""
    habit = self.make_active_habit()
    self.log_days(habit, 30, end=self.today - 86400)

    data = self.get('habit-insights-risk')

    self.assertEqual(len(data['completion_probabilities']), 1)
    row = data['completion_probabilities'][0]
    self.assertIsNotNone(row['probability'])
    self.assertIn(row['risk'], {'LOW', 'MEDIUM', 'HIGH'})
    self.assertEqual(len(data['streaks_at_risk']), 1)
    self.assertEqual(data['streaks_at_risk'][0]['current_streak'], 30)

  def test_a_settled_streak_is_not_at_risk(self):
    """The in-progress-day rule: a streak already updated today is safe, not
    'at risk' just because the day is young."""
    self.log_days(self.make_active_habit(), 30)

    self.assertEqual(self.get('habit-insights-risk')['streaks_at_risk'], [])

  def test_a_fresh_hit_is_low_risk(self):
    self.log_days(self.make_active_habit(), 30)

    row = self.get('habit-insights-risk')['completion_probabilities'][0]

    self.assertEqual(row['risk'], 'LOW')
    self.assertGreater(row['probability'], 0.9)

  def test_a_dead_habit_is_high_risk(self):
    self.log_days(self.make_active_habit(), 30, end=self.today - 60 * 86400)

    row = self.get('habit-insights-risk')['completion_probabilities'][0]

    self.assertEqual(row['risk'], 'HIGH')

  def test_load_projection_sums_the_open_habits(self):
    first = self.make_active_habit(name='Read')
    second = self.make_active_habit(name='Run')
    self.log_days(first, 10)
    self.log_days(second, 9, end=self.today - 86400)

    data = self.get('habit-insights-risk')

    self.assertEqual(data['load_projection']['outstanding_count'], 1)
    self.assertEqual(data['load_projection']['outstanding_habit_ids'], [str(second.id)])
    self.assertIn('overloaded', data['load_projection'])
    self.assertEqual(data['summary']['high_risk_habits'], 0)


class DetailInsightsEndpointTestCase(AnalyticsApiTestCase):
  def test_routes_below_the_uuid_converter_still_resolve(self):
    """`insights/` must not be swallowed by `<uuid:pk>`."""
    habit = self.make_active_habit()
    self.log_days(habit, 4)

    url = reverse('habit-insights-detail', kwargs={'habit_id': habit.id})
    response = self.client.get(url)

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    data = response.data['data']
    self.assertEqual(data['habit']['id'], str(habit.id))
    for key in ('metrics', 'patterns', 'trends', 'risk', 'schedule'):
      self.assertIn(key, data)
    self.assertEqual(data['timezone'], 'Asia/Kolkata')

  def test_another_users_habit_is_a_404(self):
    self.make_active_habit()

    url = reverse(
      'habit-insights-detail', kwargs={'habit_id': self.other_habit_id()}
    )
    response = self.client.get(url)

    self.assertEqual(response.status_code, http.HTTP_404_NOT_FOUND)

  def other_habit_id(self):
    other_tag = make_tag(self.other, name='Theirs')
    return make_habit(self.other, other_tag, start_date=label(2024, 1, 1)).id


class DashboardEndpointTestCase(AnalyticsApiTestCase):
  def test_habit_ids_are_serialised_as_uuids_not_ints(self):
    """The dashboard renders habit ids as keys; an int would break lookup."""
    self.make_active_habit()

    data = self.get('habit-dashboard')

    self.assertEqual(len(data['habits']), 1)
    for habit in data['habits']:
      self.assertIsInstance(habit['id'], str)
