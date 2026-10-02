"""Rates, streaks, lapses and the consistency score.

Everything here consumes `DayOutcome` / `Period` objects from
`habits.analytics.resolver` and returns plain dictionaries, so the views can
serialise them without a translation layer. No function reads the clock:
`today` is always passed in.
"""

from common.enums import DayState, TrendDirection

from .calendar import days_between

SCORING_VERSION = 'v1'

SCORE_WEIGHTS = {
  'adherence': 0.50,
  'regularity': 0.20,
  'persistence': 0.15,
  'recency': 0.15,
}

MILESTONE_THRESHOLDS = (30, 50, 100, 200, 365)

RECENCY_HALF_LIFE_DAYS = 7

DECLINE_THRESHOLD_POINTS = -15.0

STABLE_BAND_POINTS = 5.0

TRANSPARENT_STATES = (DayState.NOT_DUE, DayState.SKIPPED, DayState.PENDING)


def _ratio(numerator, denominator):
  if not denominator:
    return None
  return numerator / denominator


def _percent(numerator, denominator, places=1):
  """Ratio as a percentage, or None when the denominator is empty.

  Guards the multiply: `_ratio` returns None for "nothing was scheduled", and
  `None * 100` is a TypeError rather than the None the caller asked for.
  """
  ratio = _ratio(numerator, denominator)
  return _round(ratio * 100) if ratio is not None else None


def _round(value, places=1):
  if value is None:
    return None
  return round(value, places)


def count_states(periods):
  """Tally outcomes and split out which ones belong in a denominator."""
  tally = {
    DayState.HIT: 0,
    DayState.PARTIAL: 0,
    DayState.MISSED: 0,
    DayState.SKIPPED: 0,
    DayState.NOT_DUE: 0,
    DayState.PENDING: 0,
  }
  credit_total = 0.0
  scored = 0

  for period in periods:
    tally[period.state] = tally.get(period.state, 0) + 1
    if period.counts_towards_rate:
      credit_total += period.credit
      scored += 1

  return {
    'hit': tally[DayState.HIT],
    'partial': tally[DayState.PARTIAL],
    'missed': tally[DayState.MISSED],
    'skipped': tally[DayState.SKIPPED],
    'not_due': tally[DayState.NOT_DUE],
    'pending': tally[DayState.PENDING],
    'scored': scored,
    'credit_total': credit_total,
  }


def completion_rate(periods):
  """Credit-weighted adherence: a partial period is worth half.

  Returns None rather than 0.0 when nothing was scheduled, because "you did
  nothing" and "you were never asked to do anything" are different answers.
  """
  counts = count_states(periods)
  return _percent(counts['credit_total'], counts['scored'])


def strict_rate(periods):
  """Adherence with no partial credit: hits over scored periods only."""
  counts = count_states(periods)
  return _percent(counts['hit'], counts['scored'])


def target_adherence(periods):
  """Reps delivered against reps required, for habits with a target above one.

  A 3x/day habit the client records as 3 reps on a good day and 2 on a bad day
  scores 100% on `completion_rate` only if the shortfall was never written;
  `target_adherence` exposes it either way.
  """
  required = sum(period.target for period in periods if period.counts_towards_rate)
  delivered = sum(
    period.completed_total for period in periods if period.counts_towards_rate
  )
  if not required:
    return None
  return _round(min(delivered / required, 1.0) * 100)


def _ordered(periods):
  return sorted(periods, key=lambda period: (period.start, period.end))


def _scoring(periods):
  """Only the periods that carry a credit, which is what rates are built on."""
  return [period for period in _ordered(periods) if period.counts_towards_rate]


def is_in_progress(period, today):
  """A period covering today that has not been satisfied yet.

  The day is not over, so an unscored period covering today must not read as a
  miss. Without this every current streak would drop to zero at local midnight
  and rebuild itself over the following 24 hours.
  """
  return period.state == DayState.MISSED and period.start <= today <= period.end


def compute_streaks(periods, today):
  """Current and best run of consecutive satisfied scoring periods.

  Transparent periods - not due, deliberately skipped, or the in-progress day or
  week - neither extend nor break a run, which is what keeps a Mon/Wed/Fri
  streak intact across its off days. Partial periods are also transparent, so a
  streak survives a near-miss; `partial_in_current_streak` counts them.

  The caller must pass periods spanning the full history being scanned. The
  previous implementation derived streaks from a 30-day log slice, which capped
  `current_streak` at 30 and under-reported `best_streak` for anyone with older
  history.
  """
  ordered = _ordered(periods)

  current = 0
  partial_in_current = 0
  current_start = None

  for period in reversed(ordered):
    if period.state in TRANSPARENT_STATES or is_in_progress(period, today):
      continue
    if period.state == DayState.HIT:
      current += 1
      if current_start is None:
        current_start = period.start
    elif period.state == DayState.PARTIAL:
      partial_in_current += 1
    else:
      break

  best = 0
  best_start = None
  best_end = None
  running = 0
  running_start = None

  for period in ordered:
    transparent = period.state in TRANSPARENT_STATES or is_in_progress(period, today)

    if period.state == DayState.HIT:
      if running == 0:
        running_start = period.start
      running += 1
      if running > best:
        best = running
        best_start = running_start
        best_end = period.end
    elif transparent:
      continue
    else:
      running = 0
      running_start = None

  return {
    'current_streak': current,
    'current_streak_start': current_start,
    'best_streak': best,
    'best_streak_start': best_start,
    'best_streak_end': best_end,
    'partial_in_current_streak': partial_in_current,
    'history_periods': len(ordered),
  }


def compute_lapses(periods, today):
  """Contiguous runs of missed scoring periods.

  This is the recovery question the old stats could not answer: not just how
  often a streak broke, but how long the habit then took to get going again. A
  lapse still covering today is flagged `in_progress`, which is how the client
  can show a break in progress rather than a settled one.
  """
  lapses = []
  running = None

  for period in _ordered(periods):
    if period.state == DayState.MISSED:
      if running is None:
        running = {
          'start': period.start,
          'end': period.end,
          'length': 0,
          'missed_periods': 0,
          'in_progress': False,
        }
      running['end'] = period.end
      running['length'] += 1
      running['missed_periods'] += 1
      running['in_progress'] = is_in_progress(period, today)
    elif running is not None:
      lapses.append(running)
      running = None

  if running is not None:
    lapses.append(running)

  return lapses


def volatility(periods):
  """Stability of the completion rate, from rolling windows and gaps.

  Distinguishes a habit that reliably lands at 70% from one swinging between
  30% and 100%, and surfaces the longest run of days between satisfactions.
  """
  scoring = _scoring(periods)
  if not scoring:
    return {
      'rate_stddev': None,
      'rate_range': None,
      'max_gap_days': None,
      'samples': 0,
    }

  hits = [period.end for period in scoring if period.state == DayState.HIT]
  gaps = [days_between(previous, current) for previous, current in zip(hits, hits[1:])]

  window_rates = [
    sum(1 for period in scoring[index: index + 7] if period.state == DayState.HIT)
    / len(scoring[index: index + 7])
    for index in range(len(scoring))
  ]
  mean = sum(window_rates) / len(window_rates)
  variance = sum((rate - mean) ** 2 for rate in window_rates) / len(window_rates)

  return {
    'rate_stddev': _round(variance**0.5, 3),
    'rate_range': _round(max(window_rates) - min(window_rates), 3),
    'max_gap_days': max(gaps) if gaps else 0,
    'samples': len(window_rates),
  }


def recency(periods, today):
  """How recently the habit was last satisfied.

  For a WEEKLY habit a satisfied period ends on the last day of its week, so the
  gap is measured from that end rather than from the period start.
  """
  hits = [
    period.end for period in _scoring(periods) if period.state == DayState.HIT
  ]
  if not hits:
    return {'days_since_last_hit': None, 'last_hit_date': None}

  last = hits[-1]
  return {
    'days_since_last_hit': max(0, days_between(last, today)),
    'last_hit_date': last,
  }


def _regularity(periods):
  """Penalise bursty logging: one hit, then nothing for a month.

  Measures how evenly hits are spread across the observed span rather than how
  many there are, so habit volume does not distort the component.
  """
  scoring = _scoring(periods)
  if not scoring:
    # Nothing was scheduled, so there is no evidence of regularity to award.
    # Returning 1.0 here handed a brand-new habit 20 free points.
    return 0.0
  if len(scoring) == 1:
    return 1.0 if scoring[0].state == DayState.HIT else 0.0

  span = days_between(scoring[0].start, scoring[-1].end) + 1
  if span <= 0:
    return 1.0

  hits = [period.end for period in scoring if period.state == DayState.HIT]
  if len(hits) < 2:
    return 0.0

  gaps = [days_between(previous, current) for previous, current in zip(hits, hits[1:])]
  mean_gap = sum(gaps) / len(gaps)
  variance = sum((gap - mean_gap) ** 2 for gap in gaps) / len(gaps)

  return max(0.0, min(1.0, 1.0 - (variance**0.5 / span)))


def _recency_component(periods, today):
  """Exponential decay over the days since the last satisfaction."""
  days = recency(periods, today)['days_since_last_hit']
  if days is None:
    return 0.0
  return max(0.0, 0.5 ** (days / RECENCY_HALF_LIFE_DAYS))


def consistency_score(periods, today):
  """A single 0-100 consistency number plus the components behind it.

  The components are returned alongside the score on purpose: a composite score
  is only useful if the user can see why it landed where it did. Weights live in
  `SCORE_WEIGHTS` and the version in `SCORING_VERSION` so they can be retuned
  without a breaking API change.
  """
  counts = count_states(periods)

  components = {
    'adherence': _round(_ratio(counts['credit_total'], counts['scored']), 3),
    'regularity': _round(_regularity(periods), 3),
    'persistence': _round(_persistence_component(periods, today), 3),
    'recency': _round(_recency_component(periods, today), 3),
  }

  # A component that could not be computed contributes nothing rather than
  # blowing up: a brand-new or fully abandoned habit must still return a score.
  score = sum((components[key] or 0.0) * weight for key, weight in SCORE_WEIGHTS.items())

  return {
    'consistency_score': _round(score * 100),
    'consistency_components': components,
    'scoring_version': SCORING_VERSION,
  }


def _persistence_component(periods, today):
  streaks = compute_streaks(periods, today)
  best = streaks['best_streak']
  if not best:
    return 0.0
  return streaks['current_streak'] / best


def trend_direction(delta):
  """Classify a window-over-window rate change."""
  if delta is None:
    return TrendDirection.INSUFFICIENT_DATA
  if delta <= DECLINE_THRESHOLD_POINTS:
    return TrendDirection.DECLINING
  if delta >= STABLE_BAND_POINTS:
    return TrendDirection.IMPROVING
  return TrendDirection.STABLE


def next_milestone(total_completions):
  """Nearest unpassed milestone, as raw inputs the client can render.

  Counts all-time `COMPLETED` log rows rather than resolved hit periods, so the
  figure is exact and unbounded by `history_days`. Returns the threshold, the
  progress towards it and how many remain, not a formatted string, so the client
  owns the wording.
  """
  for threshold in MILESTONE_THRESHOLDS:
    if total_completions < threshold:
      return {
        'threshold': threshold,
        'completed': total_completions,
        'remaining': threshold - total_completions,
        'progress': _round(total_completions / threshold, 3),
      }
  return {
    'threshold': MILESTONE_THRESHOLDS[-1],
    'completed': total_completions,
    'remaining': 0,
    'progress': 1.0,
  }


def summarise_periods(periods, today):
  """The full metric bundle for one habit over one set of scoring periods."""
  counts = count_states(periods)

  return {
    'completion_rate': completion_rate(periods),
    'strict_rate': strict_rate(periods),
    'target_adherence': target_adherence(periods),
    'hit_periods': counts['hit'],
    'partial_periods': counts['partial'],
    'missed_periods': counts['missed'],
    'skipped_periods': counts['skipped'],
    'not_due_periods': counts['not_due'],
    'pending_periods': counts['pending'],
    'scored_periods': counts['scored'],
    **compute_streaks(periods, today),
    'volatility': volatility(periods),
    **recency(periods, today),
    **consistency_score(periods, today),
  }


def build_habit_metrics(
  *,
  periods,
  window_periods,
  previous_periods=None,
  today,
  total_completions=0,
):
  """Combine all-time and window metrics into one habit row.

  Streaks, lapses, recency, volatility and milestones come from `periods`, which
  must span the full history; rates, trend and the consistency score come from
  `window_periods`. Mixing the two would reintroduce the bug where a 30-day
  window silently capped every streak.
  """
  metrics = summarise_periods(window_periods, today)
  all_time = summarise_periods(periods, today)
  lapses = compute_lapses(periods, today)

  metrics.update(
    {
      'current_streak': all_time['current_streak'],
      'current_streak_start': all_time['current_streak_start'],
      'partial_in_current_streak': all_time['partial_in_current_streak'],
      'best_streak': all_time['best_streak'],
      'best_streak_start': all_time['best_streak_start'],
      'best_streak_end': all_time['best_streak_end'],
      'history_periods': all_time['history_periods'],
      'days_since_last_hit': all_time['days_since_last_hit'],
      'last_hit_date': all_time['last_hit_date'],
      'lapse_count': len(lapses),
      'longest_lapse': max((lapse['length'] for lapse in lapses), default=0),
      'next_milestone': next_milestone(total_completions),
    }
  )

  metrics['previous_completion_rate'] = (
    completion_rate(previous_periods) if previous_periods else None
  )
  metrics['rate_delta'] = _delta(
    metrics['completion_rate'], metrics['previous_completion_rate']
  )
  metrics['trend'] = trend_direction(metrics['rate_delta'])

  return metrics


def _delta(current, previous):
  if current is None or previous is None:
    return None
  return _round(current - previous)