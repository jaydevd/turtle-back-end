"""Shared fixtures for the analytics tests.

Two kinds of test live in this package:

* pure unit tests over `DayOutcome` / `Period`, which need no database because
  `today` is always injected rather than read from the clock,
* API tests over the insight endpoints, which build real habits and logs.

`label()` builds a day label for a given date. Labels are the UTC midnight of the
date, which is what the client sends and what the resolver reads, so these are
directly comparable with the values stored in `HabitLog.date`.
"""

import datetime as dt

from ..analytics import calendar
from ..models import Habit, HabitLog, HabitSchedule, Tag

UTC = dt.timezone.utc


def label(year, month, day):
  return calendar.epoch_of_utc_midnight(dt.date(year, month, day))


def schedule(frequency_type, target_count=1, weekdays=None):
  """A stand-in for `HabitSchedule` that is duck-typed like the real model."""
  return _Schedule(frequency_type, target_count, weekdays or [])


def weekly(target_count=1):
  return schedule('WEEKLY', target_count)


def custom(weekdays, target_count=1):
  return schedule('CUSTOM', target_count, weekdays)


def log(status, completed_count=0):
  return {'status': status, 'completed_count': completed_count}


def hit(completed_count=1):
  return log('COMPLETED', completed_count)


class _Schedule:
  def __init__(self, frequency_type, target_count, weekdays):
    self.frequency_type = frequency_type
    self.target_count = target_count
    self.weekdays = weekdays
    self.scheduled_time = None


def states_by_date(outcomes):
  return {outcome.date: outcome.state for outcome in outcomes}


def make_tag(user, name='Health'):
  return Tag.objects.create(user=user, name=name)


def make_habit(
  user,
  tag,
  name='Read',
  start_date=None,
  end_date=None,
  status='ACTIVE',
  duration_type='INDEFINITE',
):
  return Habit.objects.create(
    user=user,
    name=name,
    tag=tag,
    start_date=start_date if start_date is not None else label(2024, 1, 1),
    end_date=end_date,
    status=status,
    duration_type=duration_type,
  )


def make_schedule(habit, frequency_type='DAILY', target_count=1, weekdays=None):
  return HabitSchedule.objects.create(
    habit=habit,
    frequency_type=frequency_type,
    target_count=target_count,
    weekdays=weekdays or [],
  )


def make_log(habit, day_label, status='COMPLETED', completed_count=1, completed_at=None):
  return HabitLog.objects.create(
    habit=habit,
    date=day_label,
    status=status,
    completed_count=completed_count,
    completed_at=completed_at,
  )


def log_habit_days(habit, start_label, day_labels, status='COMPLETED', completed_count=1):
  """Write one log per day label, skipping days that already have one."""
  written = []
  for day in day_labels:
    written.append(
      make_log(habit, day, status=status, completed_count=completed_count)
    )
  return written


def consecutive_days(end_label, count):
  """`count` day labels ending at `end_label`, inclusive."""
  return [end_label - (count - 1 - offset) * calendar.SECONDS_PER_DAY for offset in range(count)]