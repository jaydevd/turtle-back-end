"""Risk and forward-looking estimates.

Everything here is an estimate over a small sample and is reported with that
sample size attached, so the client can decide whether to present a number or
suppress it. Nothing in this module claims certainty; field names say
`probability`, `projection` and `risk` rather than `predicted` or `will`.
"""

from common.enums import DayState, RiskLevel

from .calendar import SECONDS_PER_DAY, seconds_until_day_end, weekday_of
from .metrics import _round, count_states, next_milestone

MIN_SAMPLES_FOR_PREDICTION = 5

STRUGGLING_RATE_THRESHOLD = 40.0

STRUGGLING_MIN_DUE_DAYS = 10

DECAY_THRESHOLD_POINTS = -5.0

HIGH_RISK_PROBABILITY = 0.4

MEDIUM_RISK_PROBABILITY = 0.7

OVERLOAD_RATIO = 1.25

LOAD_SAMPLE_OCCURRENCES = 8

WEEKDAY_WEIGHT = 0.5

RECENCY_WEIGHT = 0.25

OVERALL_WEIGHT = 0.25

RECENCY_DECAY_DAYS = 14


def _weekday_rates(analysis):
  """Recent completion rate per weekday, with sample counts.

  Scoped to the reporting window, not to all of history. A user who kept a
  Tuesday habit for two years and has been flawless for the last month should
  not be told their probability today is 3% because of what they did last year:
  this feeds a forecast about *today*, where recent behaviour is the signal. The
  long-run view is `patterns.weekday_profile`, which stays history-wide.

  Bucketed from the day-level outcomes rather than `window_periods`, because a
  weekly habit's periods span seven days and have no single weekday of their own.
  """
  buckets = {index: {'due': 0, 'hit': 0, 'partial': 0} for index in range(7)}

  periods = analysis.window_periods
  if not periods:
    return {
      index: {'samples': 0, 'rate': None} for index in range(7)
    }

  start, end = periods[0].start, periods[-1].end
  for day in analysis.days_between(start, end):
    if not day.is_due:
      continue
    bucket = buckets[day.weekday]
    bucket['due'] += 1
    if day.state == DayState.HIT:
      bucket['hit'] += 1
    elif day.state == DayState.PARTIAL:
      bucket['partial'] += 1

  rates = {}
  for index, bucket in buckets.items():
    rates[index] = {
      'samples': bucket['due'],
      'rate': (
        (bucket['hit'] + 0.5 * bucket['partial']) / bucket['due']
        if bucket['due']
        else None
      ),
    }
  return rates


def _recency_factor(analysis, today):
  """Nudge toward habits touched recently.

  Keeps a habit the user has not logged in weeks from carrying a stale
  probability into today's forecast.
  """
  last = analysis.metric('last_hit_date')
  if last is None:
    return 0.0
  gap = max(0, (today - last) // SECONDS_PER_DAY)
  return max(0.0, 0.5 ** (gap / RECENCY_DECAY_DAYS))


def completion_probability_today(analysis, today):
  """Blended estimate that the habit is satisfied today.

  Weekday rate dominates because the day-of-week pattern is the strongest
  signal the schema supports. The weekday component is skipped when it has too
  few samples and its weight is redistributed, so a thin history never produces
  a confidently wrong number.
  """
  overall_rate = (analysis.metric('completion_rate') or 0) / 100

  rates = _weekday_rates(analysis)
  weekday = rates[weekday_of(today)]

  components = {'overall': overall_rate}
  weights = {'overall': OVERALL_WEIGHT}

  if weekday['rate'] is not None and weekday['samples'] >= MIN_SAMPLES_FOR_PREDICTION:
    components['weekday'] = weekday['rate']
    weights['weekday'] = WEEKDAY_WEIGHT

  components['recency'] = _recency_factor(analysis, today)
  weights['recency'] = RECENCY_WEIGHT

  total_weight = sum(weights.values())
  probability = sum(
    components[name] * weights[name] for name in weights
  ) / total_weight

  samples = {
    'overall': count_states(analysis.window_periods)['scored'],
    'weekday': weekday['samples'],
  }

  return {
    'habit_id': str(analysis.habit_id),
    'habit_name': analysis.habit.name,
    'probability': _round(probability, 3),
    'components': {name: _round(value, 3) for name, value in components.items()},
    'weights': {name: _round(weight / total_weight, 3) for name, weight in weights.items()},
    'samples': samples,
    'sufficient_data': samples['overall'] >= MIN_SAMPLES_FOR_PREDICTION,
    'risk': risk_level(probability),
  }


def risk_level(probability):
  if probability < HIGH_RISK_PROBABILITY:
    return RiskLevel.HIGH
  if probability < MEDIUM_RISK_PROBABILITY:
    return RiskLevel.MEDIUM
  return RiskLevel.LOW


def streaks_at_risk(analyses, today, now=None):
  """Habits with a live streak that today could still break.

  The in-progress-day rule from `metrics.is_in_progress` is what keeps this
  honest: a habit is only at risk once today's own target is unmet, not simply
  because its streak has not been updated yet this morning.
  """
  rows = []

  for analysis in analyses:
    streak = analysis.metric('current_streak') or 0
    if streak <= 0 or not analysis.is_outstanding():
      continue

    rows.append(
      {
        'habit_id': str(analysis.habit_id),
        'habit_name': analysis.habit.name,
        'current_streak': streak,
        'current_streak_start': analysis.metric('current_streak_start'),
        'seconds_remaining_in_day': (
          seconds_until_day_end(today, now) if now is not None else None
        ),
      }
    )

  return sorted(rows, key=lambda row: -row['current_streak'])


def decaying_habits(analyses):
  """Habits losing ground against their own previous window."""
  rows = []

  for analysis in analyses:
    delta = analysis.metric('rate_delta')
    if delta is None or delta > DECAY_THRESHOLD_POINTS:
      continue

    rows.append(
      {
        'habit_id': str(analysis.habit_id),
        'habit_name': analysis.habit.name,
        'completion_rate': analysis.metric('completion_rate'),
        'previous_completion_rate': analysis.metric('previous_completion_rate'),
        'rate_delta': delta,
        'trend': analysis.metric('trend'),
        'window_days': analysis.window_days,
      }
    )

  return sorted(rows, key=lambda row: row['rate_delta'])


def struggling_habits(analyses):
  """Habits with enough history to judge and a rate too low to sustain.

  The candidate set for the "drop it or shrink the target" conversation.
  """
  rows = []

  for analysis in analyses:
    counts = count_states(analysis.window_periods)
    rate = analysis.metric('completion_rate')

    if counts['scored'] < STRUGGLING_MIN_DUE_DAYS:
      continue
    if rate is None or rate > STRUGGLING_RATE_THRESHOLD:
      continue

    rows.append(
      {
        'habit_id': str(analysis.habit_id),
        'habit_name': analysis.habit.name,
        'completion_rate': rate,
        'scored_days': counts['scored'],
        'missed_periods': counts['missed'],
        'lapse_count': analysis.metric('lapse_count'),
        'longest_lapse': analysis.metric('longest_lapse'),
        'daily_target': analysis.day_target,
        'next_milestone': next_milestone(analysis.total_completions),
      }
    )

  return sorted(rows, key=lambda row: (row['completion_rate'], -row['scored_days']))


def load_projection(analyses, today):
  """Today's outstanding load against the user's typical throughput.

  Compares how many habits are outstanding right now with how many they have
  historically completed on this weekday. A ratio above `OVERLOAD_RATIO` is the
  "you have 7 due and you usually finish 3" signal.
  """
  outstanding_ids = sorted(
    str(analysis.habit_id)
    for analysis in analyses
    if analysis.is_outstanding()
  )

  hit_dates = {
    str(analysis.habit_id): {
      day.date for day in analysis.days if day.state == DayState.HIT
    }
    for analysis in analyses
  }

  sampled_totals = []
  for offset in range(1, LOAD_SAMPLE_OCCURRENCES + 1):
    label = today - offset * 7 * SECONDS_PER_DAY
    sampled_totals.append(
      sum(1 for dates in hit_dates.values() if label in dates)
    )

  typical_average = (
    sum(sampled_totals) / len(sampled_totals) if sampled_totals else 0.0
  )
  due_now = len(outstanding_ids)
  ratio = (due_now / typical_average) if typical_average > 0 else None

  return {
    'outstanding_count': due_now,
    'outstanding_habit_ids': outstanding_ids,
    'typical_completions_same_weekday': _round(typical_average, 2),
    'sample_occurrences': len(sampled_totals),
    'overload_ratio': _round(ratio, 2),
    'overloaded': bool(ratio and ratio > OVERLOAD_RATIO),
    'risk': risk_level(1 - min(1.0, ratio or 1.0)),
  }


def build_risk_report(analyses, today, now=None):
  """Everything the risk endpoint returns, across every habit in one pass."""
  return {
    'streaks_at_risk': streaks_at_risk(analyses, today, now=now),
    'completion_probabilities': [
      completion_probability_today(analysis, today) for analysis in analyses
    ],
    'decaying_habits': decaying_habits(analyses),
    'struggling_habits': struggling_habits(analyses),
    'load_projection': load_projection(analyses, today),
  }


def summarise_risk(report):
  """Headline counts, so a client can badge a screen without walking the rows."""
  return {
    'streaks_at_risk': len(report['streaks_at_risk']),
    'high_risk_habits': sum(
      1
      for row in report['completion_probabilities']
      if row['risk'] == RiskLevel.HIGH
    ),
    'decaying_habits': len(report['decaying_habits']),
    'struggling_habits': len(report['struggling_habits']),
    'overloaded': report['load_projection']['overloaded'],
  }