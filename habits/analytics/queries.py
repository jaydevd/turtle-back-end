"""The only module in the analytics package that touches the database.

Everything above it works on plain dictionaries and dataclasses, which keeps the
metric functions pure and testable without a test database. The query budget per
request is fixed regardless of how many habits the user owns:

* one query for habits, with `tag` and `schedule` pre-selected,
* one query for logs over the reporting range,
* one query for all-time `COMPLETED` counts, feeding the milestone tracker.

Streak history is bounded by `history_days`. A daily habit logged for five years
would otherwise pull thousands of rows per request on a serverless database, and
`best_streak` would claim to be all-time while silently truncating. The bound is
reported back to the client so the number stays honest.
"""

from django.db.models import Count

from common.enums import DayState, ScoreUnit, Status

from ..models import Habit, HabitLog
from .calendar import add_days, normalise_label, week_start
from .metrics import build_habit_metrics
from .resolver import (
  build_periods,
  day_target_for,
  is_due_on,
  resolve_day_states,
  score_unit_for,
  target_for,
)

DEFAULT_WINDOW_DAYS = 30

DEFAULT_HISTORY_DAYS = 730

ALLOWED_WINDOWS = (7, 14, 30, 90, 180, 365)


def resolve_window(request, default=DEFAULT_WINDOW_DAYS):
  """Read and validate the `window` query parameter.

  Raises ValueError with a caller-facing message so the view can turn it into the
  project's `411 VALIDATION_ERROR` envelope.
  """
  raw = request.query_params.get('window') if request is not None else None
  if raw in (None, ''):
    return default

  try:
    window = int(raw)
  except (TypeError, ValueError):
    raise ValueError(f'window must be one of {list(ALLOWED_WINDOWS)}.')

  if window not in ALLOWED_WINDOWS:
    raise ValueError(f'window must be one of {list(ALLOWED_WINDOWS)}.')

  return window


def load_habits(user, habit_ids=None):
  queryset = Habit.objects.filter(user=user, is_deleted=False).select_related(
    'tag', 'schedule'
  )
  if habit_ids is not None:
    queryset = queryset.filter(id__in=habit_ids)
  return list(queryset)


def load_logs(habits, start_label, end_label):
  """Logs grouped by habit then day label.

  Uses `.values()` so no model is instantiated, and groups in Python rather than
  issuing a query per habit.
  """
  habit_ids = [habit.id for habit in habits]
  if not habit_ids:
    return {}

  rows = (
    HabitLog.objects.filter(
      habit_id__in=habit_ids,
      date__gte=start_label,
      date__lte=end_label,
    )
    .values('habit_id', 'date', 'status', 'completed_count', 'completed_at')
  )

  grouped = {habit.id: {} for habit in habits}
  for row in rows:
    grouped[row['habit_id']][normalise_label(row['date'])] = {
      'status': row['status'],
      'completed_count': row['completed_count'],
      'completed_at': row['completed_at'],
    }
  return grouped


def load_total_hits(habits):
  """All-time `COMPLETED` count per habit."""
  habit_ids = [habit.id for habit in habits]
  if not habit_ids:
    return {}

  rows = (
    HabitLog.objects.filter(
      habit_id__in=habit_ids,
      status=Status.COMPLETED,
    )
    .values_list('habit_id')
    .annotate(total=Count('id'))
  )
  return dict(rows)


class HabitAnalysis:
  """Resolved days and periods for one habit, ready for the metric functions.

  Holds three overlapping ranges so callers pick the right one without
  re-querying:

  * `periods` - bounded history for streaks, lapses and recency,
  * `window_periods` - the reporting window behind every rate,
  * `previous_periods` - the equally long window immediately before it, which is
    what makes `rate_delta` a real comparison rather than a guess.
  """

  __slots__ = (
    'habit',
    'schedule',
    'today',
    'window_days',
    'history_days',
    'days',
    'periods',
    'window_periods',
    'previous_periods',
    'total_completions',
    'logs_by_day',
    'metrics',
    'day_index',
  )

  def __init__(self, **fields):
    for name in self.__slots__:
      setattr(self, name, fields.get(name))

  @property
  def habit_id(self):
    return self.habit.id

  @property
  def start_date(self):
    return normalise_label(self.habit.start_date)

  @property
  def end_date(self):
    return normalise_label(self.habit.end_date)

  @property
  def period_target(self):
    return target_for(self.schedule)

  @property
  def day_target(self):
    return day_target_for(self.schedule)

  @property
  def score_unit(self):
    return score_unit_for(self.schedule)

  @property
  def target(self):
    """Reps due today, which is what a check-in control needs."""
    return self.day_target

  def today_outcome(self):
    return self.day_index.get(self.today)

  def outcome_on(self, label):
    return self.day_index.get(label)

  def current_period(self):
    for period in self.periods:
      if period.start <= self.today <= period.end:
        return period
    return None

  def is_outstanding(self):
    """Whether the habit still needs attention today.

    DAILY and CUSTOM habits are done once today's own target is met. WEEKLY
    habits stay outstanding until the week's target is met, which is the rule the
    old `is_due_today` applied with one `COUNT` query per weekly habit; here it
    is read off the already-resolved periods.
    """
    day = self.today_outcome()
    if day is None or not day.is_due:
      return False

    if self.score_unit == ScoreUnit.WEEK:
      period = self.current_period()
      return period is not None and period.state != DayState.HIT

    return day.state != DayState.HIT

  def days_between(self, start, end):
    return [day for day in self.days if start <= day.date <= end]

  def metric(self, key, default=None):
    return (self.metrics or {}).get(key, default)

  def completed_at_for(self, day_label):
    return (self.logs_by_day or {}).get(day_label, {}).get('completed_at')


def analyse_habit(
  *,
  habit,
  schedule,
  today,
  window_days,
  history_days,
  logs_by_day,
  total_completions=0,
):
  """Resolve one habit across its history, window and previous window.

  For WEEKLY habits every boundary is snapped back to the Monday of its week.
  Without that, a window starting mid-week would produce a truncated opening
  bucket that gets scored MISSED on reps the user may well have completed in the
  days before the window began, showing up as a phantom lapse.
  """
  habit_start = normalise_label(habit.start_date)
  habit_end = normalise_label(habit.end_date)
  weekly = score_unit_for(schedule) == ScoreUnit.WEEK

  def snap(label):
    return week_start(label) if weekly else label

  history_start = snap(add_days(today, -(history_days - 1)))
  if habit_start is not None:
    history_start = max(history_start, habit_start)

  window_start = snap(add_days(today, -(window_days - 1)))
  previous_end = add_days(window_start, -1)
  previous_start = snap(add_days(previous_end, -(window_days - 1)))

  def resolve(start, end):
    days = resolve_day_states(
      today=today,
      start=start,
      end=end,
      schedule=schedule,
      status=habit.status,
      habit_start_date=habit_start,
      habit_end_date=habit_end,
      logs_by_day=logs_by_day,
    )
    return days, build_periods(days, schedule)

  history_days_resolved, history_periods = resolve(history_start, today)
  _, window_periods = resolve(window_start, today)

  if previous_start < history_start:
    previous_periods = []
  else:
    _, previous_periods = resolve(previous_start, previous_end)

  return HabitAnalysis(
    habit=habit,
    schedule=schedule,
    today=today,
    window_days=window_days,
    history_days=history_days,
    days=history_days_resolved,
    periods=history_periods,
    window_periods=window_periods,
    previous_periods=previous_periods,
    total_completions=total_completions,
    logs_by_day=logs_by_day,
    day_index={day.date: day for day in history_days_resolved},
    metrics=build_habit_metrics(
      periods=history_periods,
      window_periods=window_periods,
      previous_periods=previous_periods,
      today=today,
      total_completions=total_completions,
    ),
  )


def load_analyses(
  user,
  *,
  today,
  window_days=DEFAULT_WINDOW_DAYS,
  history_days=DEFAULT_HISTORY_DAYS,
  habit_ids=None,
):
  """Build a `HabitAnalysis` for every habit the user owns."""
  habits = load_habits(user, habit_ids=habit_ids)
  if not habits:
    return []

  history_start = add_days(today, -(history_days - 1))
  window_start = add_days(today, -(window_days - 1))
  logs = load_logs(habits, min(history_start, window_start), today)
  totals = load_total_hits(habits)

  return [
    analyse_habit(
      habit=habit,
      schedule=getattr(habit, 'schedule', None),
      today=today,
      window_days=window_days,
      history_days=history_days,
      logs_by_day=logs.get(habit.id, {}),
      total_completions=totals.get(habit.id, 0),
    )
    for habit in habits
  ]


def scheduled_on(analysis, label):
  """Public re-export so views need not import the resolver directly."""
  return is_due_on(analysis.schedule, label)