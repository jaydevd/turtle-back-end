"""Pattern discovery: when and where a habit actually succeeds.

These are the features that answer "what should I change about my routine" -
which weekday reliably fails, which hour of the day the user actually performs
the habit at, and how habits differ by tag.

Hour-of-day and punctuality metrics depend on `HabitLog.completed_at`, which the
client used to leave null. `HabitLogViewSet.create` now defaults it, so these
populate from new check-ins onward, but historical rows stay empty and the
sample counts are reported so the client can hide an empty chart rather than
draw a misleading one.
"""

from common.enums import DayState

from .calendar import (
  WEEKDAY_LABELS,
  WEEKDAY_SHORT_LABELS,
  hour_of_day,
  month_key,
  minutes_from_clock,
  minutes_of_day,
)
from .metrics import _round

MIN_WEEKDAY_SAMPLES = 2

PUNCTUALITY_TOLERANCE_MINUTES = 60


def weekday_profile(analysis):
  """Per-weekday hit and miss tallies.

  Answers "you always miss Mondays". Non-due weekdays report `due_days: 0`,
  which is the normal case for a Mon/Wed/Fri custom schedule on a Tuesday, so
  the client should treat a zero-sample weekday as absent rather than as 0%.
  """
  buckets = [
    {
      'weekday': index,
      'name': WEEKDAY_LABELS[index],
      'short_name': WEEKDAY_SHORT_LABELS[index],
      'due_days': 0,
      'hit_days': 0,
      'partial_days': 0,
      'missed_days': 0,
      'skipped_days': 0,
    }
    for index in range(7)
  ]

  for day in analysis.days:
    if day.state in (DayState.NOT_DUE, DayState.PENDING):
      continue
    bucket = buckets[day.weekday]
    if day.is_due:
      bucket['due_days'] += 1
    if day.state == DayState.HIT:
      bucket['hit_days'] += 1
    elif day.state == DayState.PARTIAL:
      bucket['partial_days'] += 1
    elif day.state == DayState.MISSED:
      bucket['missed_days'] += 1
    elif day.state == DayState.SKIPPED:
      bucket['skipped_days'] += 1

  for bucket in buckets:
    scored = (
      bucket['due_days'] - bucket['skipped_days']
    )
    bucket['completion_rate'] = (
      _round((bucket['hit_days'] + 0.5 * bucket['partial_days']) / scored * 100)
      if scored > 0
      else None
    )
    bucket['miss_rate'] = (
      _round(bucket['missed_days'] / scored * 100) if scored > 0 else None
    )
    bucket['reliable'] = (
      scored >= MIN_WEEKDAY_SAMPLES and bucket['completion_rate'] is not None
    )

  return buckets


def weekday_extremes(profile):
  """Best and worst weekdays, ignoring weekdays with too little history.

  A 0% weekday is a legitimate *worst* - "you always miss Mondays" is the most
  useful thing this module says - but it is never a *best*. With every weekday
  at 0% there is nothing to rank, so both come back empty rather than naming
  Monday as the winner at 0%.
  """
  reliable = [bucket for bucket in profile if bucket['reliable']]
  if not reliable or not any((bucket['completion_rate'] or 0) > 0 for bucket in reliable):
    return {'best_weekday': None, 'worst_weekday': None}

  best = max(reliable, key=lambda bucket: (bucket['completion_rate'], -bucket['weekday']))
  worst = min(reliable, key=lambda bucket: (bucket['completion_rate'], bucket['weekday']))

  return {
    'best_weekday': best['weekday'],
    'best_weekday_name': best['name'],
    'best_weekday_rate': best['completion_rate'],
    'worst_weekday': worst['weekday'],
    'worst_weekday_name': worst['name'],
    'worst_weekday_rate': worst['completion_rate'],
    'weekday_spread': _round(best['completion_rate'] - worst['completion_rate']),
  }


def hour_of_day_profile(analysis):
  """Distribution of completions across the 24 local hours.

  `completed_at` is the only sub-day signal in the schema, so this is the
  entire basis for "you do this best at 7am". `sample_count` lets the client
  decide whether it has enough data to say anything.
  """
  buckets = [{'hour': hour, 'completions': 0} for hour in range(24)]

  for day in analysis.days:
    if day.state != DayState.HIT:
      continue
    completed_at = analysis.completed_at_for(day.date)
    if completed_at is None:
      continue
    buckets[hour_of_day(completed_at)]['completions'] += 1

  total = sum(bucket['completions'] for bucket in buckets)
  for bucket in buckets:
    bucket['share'] = (
      _round(bucket['completions'] / total, 3) if total else None
    )

  ranked = sorted(buckets, key=lambda bucket: (-bucket['completions'], bucket['hour']))
  peak = next((bucket for bucket in ranked if bucket['completions']), None)

  return {
    'buckets': buckets,
    'sample_count': total,
    'peak_hour': peak['hour'] if peak else None,
    'peak_share': peak['share'] if peak else None,
  }


def punctuality(analysis, tolerance_minutes=PUNCTUALITY_TOLERANCE_MINUTES):
  """How close completions land to the habit's scheduled time.

  Compares against `HabitSchedule.scheduled_time` and falls back to
  `Habit.reminder`, which are overlapping concepts in the schema and currently
  both display-only metadata.
  """
  reference = minutes_from_clock(
    analysis.schedule.scheduled_time if analysis.schedule else None
  )
  if reference is None:
    reference = minutes_from_clock(analysis.habit.reminder)

  if reference is None:
    return {
      'available': False,
      'reference_minutes': None,
      'sample_count': 0,
      'on_time_count': 0,
      'on_time_rate': None,
      'average_offset_minutes': None,
    }

  on_time = 0
  offsets = []
  for day in analysis.days:
    if day.state not in (DayState.HIT, DayState.PARTIAL) or day.completed_count <= 0:
      continue
    completed_at = analysis.completed_at_for(day.date)
    if completed_at is None:
      continue
    offset = minutes_of_day(completed_at) - reference
    offsets.append(offset)
    if abs(offset) <= tolerance_minutes:
      on_time += 1

  return {
    'available': True,
    'reference_minutes': reference,
    'tolerance_minutes': tolerance_minutes,
    'sample_count': len(offsets),
    'on_time_count': on_time,
    'on_time_rate': _round(on_time / len(offsets) * 100) if offsets else None,
    'average_offset_minutes': (
      _round(sum(offsets) / len(offsets)) if offsets else None
    ),
  }


def monthly_profile(analysis):
  """Calendar-month rollup, for the trend endpoint and long-range charts."""
  buckets = {}

  for day in analysis.days:
    if day.state in (DayState.NOT_DUE, DayState.PENDING):
      continue
    key = month_key(day.date)
    bucket = buckets.setdefault(
      key, {'month': key, 'due_days': 0, 'hit_days': 0, 'partial_days': 0, 'missed_days': 0}
    )
    if day.is_due:
      bucket['due_days'] += 1
    if day.state == DayState.HIT:
      bucket['hit_days'] += 1
    elif day.state == DayState.PARTIAL:
      bucket['partial_days'] += 1
    elif day.state == DayState.MISSED:
      bucket['missed_days'] += 1

  ordered = [buckets[key] for key in sorted(buckets)]

  for bucket in ordered:
    scored = bucket['due_days']
    bucket['completion_rate'] = (
      _round((bucket['hit_days'] + 0.5 * bucket['partial_days']) / scored * 100)
      if scored
      else None
    )

  return ordered


def tag_profile(analyses):
  """Roll habits up by tag, which is the only grouping key the schema has."""
  buckets = {}

  for analysis in analyses:
    tag = analysis.habit.tag
    bucket = buckets.setdefault(
      str(tag.id),
      {
        'tag_id': str(tag.id),
        'tag_name': tag.name,
        'habit_ids': [],
        'due_days': 0,
        'hit_days': 0,
        'partial_days': 0,
        'missed_days': 0,
        'skipped_days': 0,
      },
    )

    bucket['habit_ids'].append(str(analysis.habit.id))
    for day in analysis.days:
      if not day.is_due:
        continue
      if day.state == DayState.HIT:
        bucket['due_days'] += 1
        bucket['hit_days'] += 1
      elif day.state == DayState.PARTIAL:
        bucket['due_days'] += 1
        bucket['partial_days'] += 1
      elif day.state == DayState.MISSED:
        bucket['due_days'] += 1
        bucket['missed_days'] += 1
      elif day.state == DayState.SKIPPED:
        bucket['skipped_days'] += 1

  rows = []
  for bucket in buckets.values():
    scored = bucket['due_days'] - bucket['skipped_days']
    rows.append(
      {
        'tag_id': bucket['tag_id'],
        'tag_name': bucket['tag_name'],
        'habit_ids': bucket['habit_ids'],
        'habit_count': len(bucket['habit_ids']),
        'due_days': bucket['due_days'],
        'hit_days': bucket['hit_days'],
        'partial_days': bucket['partial_days'],
        'missed_days': bucket['missed_days'],
        'skipped_days': bucket['skipped_days'],
        'completion_rate': (
          _round((bucket['hit_days'] + 0.5 * bucket['partial_days']) / scored * 100)
          if scored > 0
          else None
        ),
      }
    )

  return sorted(rows, key=lambda row: (-(row['completion_rate'] or -1), row['tag_name']))


def build_pattern_report(analysis):
  """Every pattern metric for one habit."""
  profile = weekday_profile(analysis)
  return {
    'weekday_profile': profile,
    **weekday_extremes(profile),
    'hour_of_day': hour_of_day_profile(analysis),
    'punctuality': punctuality(analysis),
    'monthly_profile': monthly_profile(analysis),
  }


def habit_by_weekday_rates(analyses):
  """Per-weekday completion rate across all habits, for the overview.

  This is the aggregate answer to "which day of my week do I lose habits",
    which is not the same as any single habit's weekday profile.
  """
  buckets = [
    {
      'weekday': index,
      'name': WEEKDAY_LABELS[index],
      'due_days': 0,
      'hit_days': 0,
      'partial_days': 0,
      'missed_days': 0,
      'habit_ids': [],
    }
    for index in range(7)
  ]

  for analysis in analyses:
    for day in analysis.days:
      if not day.is_due:
        continue
      bucket = buckets[day.weekday]
      bucket['due_days'] += 1
      if day.state == DayState.HIT:
        bucket['hit_days'] += 1
      elif day.state == DayState.PARTIAL:
        bucket['partial_days'] += 1
      elif day.state == DayState.MISSED:
        bucket['missed_days'] += 1

  for bucket in buckets:
    scored = bucket['due_days']
    bucket['completion_rate'] = (
      _round((bucket['hit_days'] + 0.5 * bucket['partial_days']) / scored * 100)
      if scored
      else None
    )
    bucket['habit_ids'] = sorted(
      {
        str(analysis.habit.id)
        for analysis in analyses
        if any(day.is_due and day.weekday == bucket['weekday'] for day in analysis.days)
      }
    )

  return buckets
