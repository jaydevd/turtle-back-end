"""User-level rollups across every habit.

Answers the questions a single-habit view cannot: am I getting better overall,
which weekday do I lose the most habits, and on how many days did I actually
finish everything I was supposed to.

A user-level "did I do my habits today" rollup is inherently day-shaped, so
`WEEKLY` habits contribute one period per day here even though their own scoring
stays weekly in `resolver.build_periods`. The two views answer different
questions and are not expected to agree on a weekly habit.
"""

from common.enums import DayState

from .calendar import SECONDS_PER_DAY, weekday_of
from .metrics import (
  SCORE_WEIGHTS,
  SCORING_VERSION,
  _round,
  completion_rate,
  compute_streaks,
  consistency_score,
  count_states,
  next_milestone,
)
from .patterns import habit_by_weekday_rates, tag_profile
from .resolver import Period


def _state_for(bucket, scored):
  if scored <= 0:
    return DayState.SKIPPED
  if bucket['hit'] == scored:
    return DayState.HIT
  if bucket['hit'] + bucket['partial'] == scored:
    return DayState.PARTIAL
  return DayState.MISSED


def user_periods_by_day(analyses, start, end):
  """Day-unit periods across all habits, for days in `[start, end]`."""
  by_date = {}

  for analysis in analyses:
    for label, day in analysis.day_index.items():
      if label < start or label > end or not day.is_due:
        continue
      bucket = by_date.setdefault(
        label,
        {'due': 0, 'hit': 0, 'partial': 0, 'missed': 0, 'skipped': 0},
      )
      bucket['due'] += 1
      if day.state == DayState.HIT:
        bucket['hit'] += 1
      elif day.state == DayState.PARTIAL:
        bucket['partial'] += 1
      elif day.state == DayState.MISSED:
        bucket['missed'] += 1
      elif day.state == DayState.SKIPPED:
        bucket['skipped'] += 1

  periods = []
  for label in sorted(by_date):
    bucket = by_date[label]
    scored = bucket['due'] - bucket['skipped']
    periods.append(
      Period(
        start=label,
        end=label,
        state=_state_for(bucket, scored),
        due_days=bucket['due'],
        target=1,
        completed_total=bucket['hit'],
        weekday=weekday_of(label),
      )
    )
  return periods


def _day_totals(analyses, label):
  due = hit = partial = missed = skipped = 0

  for analysis in analyses:
    day = analysis.day_index.get(label)
    if day is None or not day.is_due:
      continue
    due += 1
    if day.state == DayState.HIT:
      hit += 1
    elif day.state == DayState.PARTIAL:
      partial += 1
    elif day.state == DayState.MISSED:
      missed += 1
    elif day.state == DayState.SKIPPED:
      skipped += 1

  return {
    'date': label,
    'due': due,
    'hit': hit,
    'partial': partial,
    'missed': missed,
    'skipped': skipped,
    'scored': due - skipped,
    'active': hit > 0 or partial > 0,
    'perfect': due > 0 and due - skipped == hit,
  }


def activity_counts(analyses, start, end):
  """Days with any activity, fully completed days, and days where nothing landed."""
  active_days = 0
  perfect_days = 0
  zero_days = 0

  for label in _labels(start, end):
    totals = _day_totals(analyses, label)
    if totals['due'] == 0:
      continue
    if totals['perfect']:
      perfect_days += 1
      active_days += 1
    elif totals['active']:
      active_days += 1
    else:
      zero_days += 1

  return {
    'days_considered': len(_labels(start, end)),
    'active_days': active_days,
    'perfect_days': perfect_days,
    'zero_days': zero_days,
  }


def active_day_streaks(analyses, start, end, today):
  """Current and best run of days where at least one habit was satisfied.

  Both an `active` and a `perfect` streak are reported. A perfect-day streak
  requires every due habit to be hit, which is close to unreachable for anyone
  tracking more than a handful of habits, and a permanently zero number teaches
  the user nothing.
  """
  labels = _labels(start, end)
  flags = [_has_hit(_day_totals(analyses, label)) for label in labels]

  best = 0
  running = 0
  for hit in flags:
    running = running + 1 if hit else 0
    best = max(best, running)

  current = 0
  for label in reversed(_labels(start, today)):
    if not _has_hit(_day_totals(analyses, label)):
      break
    current += 1

  return {
    'active_day_streak': current,
    'best_active_day_streak': best,
  }


def _has_hit(totals):
  return totals['hit'] > 0


def _labels(start, end):
  if end < start:
    return []
  return [start + offset * SECONDS_PER_DAY for offset in range((end - start) // SECONDS_PER_DAY + 1)]


TOP_HABITS_LIMIT = 5


def _split_ranked(ranked, limit=TOP_HABITS_LIMIT):
  """Split best-first `ranked` into non-overlapping best and worst lists.

  Slicing both ends independently would list every habit twice for any user with
  fewer habits than the limit - once as a win, once as a loss - and the client
  would render the same card in both sections.
  """
  if len(ranked) <= limit:
    return ranked, []

  middle = ranked[limit:-limit]
  return ranked[:limit], list(reversed(middle[-limit:]))


def _habit_row(analysis):
  return {
    'id': str(analysis.habit_id),
    'name': analysis.habit.name,
    'color': analysis.habit.color,
    'icon': analysis.habit.icon,
    'completion_rate': analysis.metric('completion_rate'),
    'current_streak': analysis.metric('current_streak'),
    'best_streak': analysis.metric('best_streak'),
    'consistency_score': analysis.metric('consistency_score'),
    'trend': analysis.metric('trend'),
    'rate_delta': analysis.metric('rate_delta'),
  }


def build_overview(analyses, today, window_start, previous_start=None):
  """The user-level rollup returned by `/insights/overview/`."""
  window_periods = user_periods_by_day(analyses, window_start, today)

  previous_periods = (
    user_periods_by_day(analyses, previous_start, window_start - SECONDS_PER_DAY)
    if previous_start is not None and previous_start < window_start
    else []
  )

  counts = count_states(window_periods)
  rate = completion_rate(window_periods)
  previous_rate = completion_rate(previous_periods) if previous_periods else None

  perfect_streaks = compute_streaks(window_periods, today)

  habit_rows = [_habit_row(analysis) for analysis in analyses]
  ranked = sorted(
    [row for row in habit_rows if row['completion_rate'] is not None],
    key=lambda row: -row['completion_rate'],
  )
  top_habits, bottom_habits = _split_ranked(ranked)

  return {
    'completion_rate': rate,
    'strict_rate': (
      _round(counts['hit'] / counts['scored'] * 100) if counts['scored'] else None
    ),
    'previous_completion_rate': previous_rate,
    'rate_delta': (
      _round(rate - previous_rate)
      if rate is not None and previous_rate is not None
      else None
    ),
    'due_days': counts['scored'],
    'hit_days': counts['hit'],
    'partial_days': counts['partial'],
    'missed_days': counts['missed'],
    'skipped_days': counts['skipped'],
    **consistency_score(window_periods, today),
    **active_day_streaks(analyses, window_start, today, today),
    'perfect_day_streak': perfect_streaks['current_streak'],
    'best_perfect_day_streak': perfect_streaks['best_streak'],
    **activity_counts(analyses, window_start, today),
    'weekday_profile': habit_by_weekday_rates(analyses),
    'tag_profile': tag_profile(analyses),
    'habit_count': len(habit_rows),
    'top_habits': top_habits,
    'bottom_habits': bottom_habits,
    'scoring_weights': SCORE_WEIGHTS,
    'scoring_version': SCORING_VERSION,
    'next_milestone': next_milestone(
      sum(analysis.total_completions for analysis in analyses)
    ),
  }