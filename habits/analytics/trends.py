"""Time-series payloads for charts and the heatmap grid.

The frontend currently builds its heatmap from whatever logs it happens to have
loaded (`frontend/src/components/ui/Heatmap.tsx`), which cannot account for
days that were scheduled but never logged. These series come from the resolver
instead, so a missed day is scored as a miss rather than being absent.
"""

from common.enums import DayState

from .calendar import SECONDS_PER_DAY, week_start
from .metrics import _round

GRANULARITY_DAY = 'day'
GRANULARITY_WEEK = 'week'

ALLOWED_GRANULARITIES = (GRANULARITY_DAY, GRANULARITY_WEEK)


def resolve_granularity(request, default=GRANULARITY_DAY):
  raw = request.query_params.get('granularity') if request is not None else None
  if raw in (None, ''):
    return default
  if raw not in ALLOWED_GRANULARITIES:
    raise ValueError(f'granularity must be one of {list(ALLOWED_GRANULARITIES)}.')
  return raw


def daily_series(analysis, start=None, end=None):
  """One row per calendar day.

  `score` is the day's credit (1.0 hit, 0.5 partial, 0.0 missed, null when the
  day was not due), which is the value a heatmap cell needs. Non-due days are
  still emitted with `is_due: false` so the grid can render a neutral cell
  instead of a hole.
  """
  rows = []
  for day in analysis.days:
    if start is not None and day.date < start:
      continue
    if end is not None and day.date > end:
      continue

    row = day.as_dict()
    row['score'] = day.credit
    row['logged'] = bool(analysis.completed_at_for(day.date)) or day.completed_count > 0
    rows.append(row)

  return rows


def aggregate_series(day_rows, granularity=GRANULARITY_DAY):
  """Roll day rows into day or week buckets.

  Weekly buckets are Monday-anchored, matching `ScoreUnit.WEEK` and the existing
  `is_due_today` week window, so a weekly habit and a weekly chart bucket always
  line up.
  """
  if granularity == GRANULARITY_DAY:
    return [_day_bucket(row) for row in day_rows]

  grouped = []
  for row in day_rows:
    start = week_start(row['date'])
    if not grouped or grouped[-1]['start'] != start:
      grouped.append(
        {
          'start': start,
          'end': start,
          'due_days': 0,
          'hit_days': 0,
          'partial_days': 0,
          'missed_days': 0,
          'skipped_days': 0,
          'credit_total': 0.0,
          'scored_days': 0,
        }
      )
    grouped[-1]['end'] = row['date']
    _accumulate(grouped[-1], row)

  return [_finalise(bucket, granularity) for bucket in grouped]


def _day_bucket(row):
  bucket = {
    'start': row['date'],
    'end': row['date'],
    'due_days': 0,
    'hit_days': 0,
    'partial_days': 0,
    'missed_days': 0,
    'skipped_days': 0,
    'credit_total': 0.0,
    'scored_days': 0,
  }
  _accumulate(bucket, row)
  finalised = _finalise(bucket, GRANULARITY_DAY)
  finalised['weekday'] = row['weekday']
  return finalised


def _accumulate(bucket, row):
  if row['is_due']:
    bucket['due_days'] += 1
  if row['state'] == DayState.HIT:
    bucket['hit_days'] += 1
  elif row['state'] == DayState.PARTIAL:
    bucket['partial_days'] += 1
  elif row['state'] == DayState.MISSED:
    bucket['missed_days'] += 1
  elif row['state'] == DayState.SKIPPED:
    bucket['skipped_days'] += 1

  if row['score'] is not None:
    bucket['credit_total'] += row['score']
    bucket['scored_days'] += 1


def _finalise(bucket, granularity):
  scored = bucket['scored_days']
  return {
    'start': bucket['start'],
    'end': bucket['end'],
    'granularity': granularity,
    'days': (bucket['end'] - bucket['start']) // SECONDS_PER_DAY + 1,
    'due_days': bucket['due_days'],
    'hit_days': bucket['hit_days'],
    'partial_days': bucket['partial_days'],
    'missed_days': bucket['missed_days'],
    'skipped_days': bucket['skipped_days'],
    'completion_rate': _round(bucket['credit_total'] / scored * 100) if scored else None,
  }


def heatmap(analysis, start=None, end=None):
  """GitHub-style grid: one cell per day with the day's completion score.

  Days that were not due are omitted rather than emitted with a null score, which
  halves the payload for a Mon/Wed/Fri habit and leaves the client free to leave
  gaps.
  """
  cells = []
  for day in analysis.days:
    if start is not None and day.date < start:
      continue
    if end is not None and day.date > end:
      continue
    if not day.is_due:
      continue

    cells.append(
      {
        'date': day.date,
        'weekday': day.weekday,
        'state': day.state,
        'score': day.credit,
        'target': day.target,
        'completed_count': day.completed_count,
      }
    )

  return cells


def build_trend_report(analysis, granularity=GRANULARITY_DAY):
  """Daily and weekly series plus the heatmap grid for one habit."""
  rows = daily_series(analysis)
  return {
    'granularity': granularity,
    'score_unit': analysis.score_unit,
    'daily': [_day_bucket(row) for row in rows],
    'weekly': aggregate_series(rows, GRANULARITY_WEEK),
    'heatmap': heatmap(analysis),
  }


def user_daily_series(analyses, start, end, granularity=GRANULARITY_DAY):
  """Combined day buckets across every habit.

  `due_count` and `hit_count` per day are what the dashboard-level chart needs:
  a day with three habits due and two completed scores 0.67, not 1.0.
  """
  merged = {}
  for analysis in analyses:
    for day in analysis.days:
      if day.date < start or day.date > end:
        continue
      if not day.is_due:
        continue
      bucket = merged.setdefault(
        day.date,
        {
          'date': day.date,
          'weekday': day.weekday,
          'due_count': 0,
          'hit_count': 0,
          'partial_count': 0,
          'missed_count': 0,
          'credit_total': 0.0,
          'habit_ids': [],
        },
      )
      bucket['due_count'] += 1
      if day.state == DayState.HIT:
        bucket['hit_count'] += 1
      elif day.state == DayState.PARTIAL:
        bucket['partial_count'] += 1
      elif day.state == DayState.MISSED:
        bucket['missed_count'] += 1
      bucket['credit_total'] += day.credit
      bucket['habit_ids'].append(str(analysis.habit.id))

  rows = []
  for label in sorted(merged):
    bucket = merged[label]
    rows.append(
      {
        'date': bucket['date'],
        'weekday': bucket['weekday'],
        'due_count': bucket['due_count'],
        'hit_count': bucket['hit_count'],
        'partial_count': bucket['partial_count'],
        'missed_count': bucket['missed_count'],
        'score': _round(bucket['credit_total'] / bucket['due_count'], 3),
        'perfect': bucket['hit_count'] == bucket['due_count'],
        'habit_ids': bucket['habit_ids'],
      }
    )

  if granularity == GRANULARITY_WEEK:
    return _roll_user_rows_weekly(rows)

  return rows


def _roll_user_rows_weekly(rows):
  grouped = {}
  for row in rows:
    start = week_start(row['date'])
    bucket = grouped.setdefault(
      start,
      {
        'start': start,
        'end': start,
        'due_count': 0,
        'hit_count': 0,
        'partial_count': 0,
        'missed_count': 0,
        'credit_total': 0.0,
        'perfect_days': 0,
        'days': 0,
      },
    )
    bucket['end'] = row['date']
    bucket['days'] += 1
    bucket['due_count'] += row['due_count']
    bucket['hit_count'] += row['hit_count']
    bucket['partial_count'] += row['partial_count']
    bucket['missed_count'] += row['missed_count']
    bucket['credit_total'] += row['due_count'] * row['score']
    if row['perfect']:
      bucket['perfect_days'] += 1

  return [
    {
      'start': bucket['start'],
      'end': bucket['end'],
      'granularity': GRANULARITY_WEEK,
      'days': bucket['days'],
      'due_count': bucket['due_count'],
      'hit_count': bucket['hit_count'],
      'partial_count': bucket['partial_count'],
      'missed_count': bucket['missed_count'],
      'perfect_days': bucket['perfect_days'],
      'score': (
        _round(bucket['credit_total'] / bucket['due_count'], 3)
        if bucket['due_count']
        else None
      ),
    }
    for _, bucket in sorted(grouped.items())
  ]
