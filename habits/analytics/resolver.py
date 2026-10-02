"""Turn raw `HabitLog` rows into resolved per-day outcomes.

`resolve_day_states` is the single source of truth for "how did this habit do
on this day". The previous implementation answered that question four different
ways across `compute_streaks`, the `scheduled_days` denominator, `is_due_today`
and the dashboard, which is why the numbers disagreed with each other. Every
metric in this package now derives from the output of this module.

Three classes of day are excluded from every denominator:

* not due - outside the habit's schedule, before its `start_date`, after its
  `end_date`, or the habit is no longer ACTIVE,
* pending - the day has not happened yet,
* skipped - the user deliberately skipped it, which is not a failure.

A past due day with no `HabitLog` row at all resolves to `MISSED`. Nothing in
the codebase ever writes `Status.MISSED`, so misses have to be inferred.
"""

from dataclasses import dataclass

from common.enums import DayState, FrequencyType, ScoreUnit, Status

from .calendar import date_range, days_between, normalise_label, week_start, weekday_of

CREDIT_HIT = 1.0
CREDIT_PARTIAL = 0.5
CREDIT_MISSED = 0.0


@dataclass(frozen=True)
class DayOutcome:
  """Resolved outcome for one habit on one calendar day."""

  date: int
  weekday: int
  is_due: bool
  state: str
  target: int
  completed_count: int
  is_future: bool

  @property
  def credit(self):
    if self.state == DayState.HIT:
      return CREDIT_HIT
    if self.state == DayState.PARTIAL:
      return CREDIT_PARTIAL
    if self.state == DayState.MISSED:
      return CREDIT_MISSED
    return None

  @property
  def counts_towards_rate(self):
    """Whether this day belongs in a rate denominator."""
    return self.credit is not None

  def as_dict(self):
    return {
      'date': self.date,
      'weekday': self.weekday,
      'is_due': self.is_due,
      'state': self.state,
      'target': self.target,
      'completed_count': self.completed_count,
      'is_future': self.is_future,
    }


@dataclass(frozen=True)
class Period:
  """A scoring unit for one habit: a single day, or a Monday-anchored week."""

  start: int
  end: int
  state: str
  due_days: int
  target: int
  completed_total: int
  weekday: int
  is_partial: bool = False

  @property
  def length(self):
    return days_between(self.start, self.end) + 1

  @property
  def credit(self):
    if self.state == DayState.HIT:
      return CREDIT_HIT
    if self.state == DayState.PARTIAL:
      return CREDIT_PARTIAL
    if self.state == DayState.MISSED:
      return CREDIT_MISSED
    return None

  @property
  def counts_towards_rate(self):
    return self.credit is not None

  def as_dict(self):
    return {
      'start': self.start,
      'end': self.end,
      'state': self.state,
      'due_days': self.due_days,
      'target': self.target,
      'completed_total': self.completed_total,
      'is_partial': self.is_partial,
    }


def score_unit_for(schedule):
  """WEEKLY targets apply per week; everything else applies per day."""
  if schedule is None:
    return ScoreUnit.DAY
  if schedule.frequency_type == FrequencyType.WEEKLY:
    return ScoreUnit.WEEK
  return ScoreUnit.DAY


def target_for(schedule):
  """Reps required by the scoring period: per day, or per week for WEEKLY."""
  if schedule is None:
    return 1
  return max(1, schedule.target_count or 1)


def day_target_for(schedule):
  """Reps required within a single day.

  A WEEKLY habit needs `target_count` reps *across* the week, so applying that
  number per day would demand all three reps land on the same date and mark
  every other day of the week a miss.
  """
  if schedule is None:
    return 1
  if schedule.frequency_type == FrequencyType.WEEKLY:
    return 1
  return max(1, schedule.target_count or 1)


def is_due_on(schedule, label):
  """Whether the schedule marks `label` as a scheduled day."""
  if schedule is None:
    return True
  if schedule.frequency_type == FrequencyType.CUSTOM:
    return weekday_of(label) in (schedule.weekdays or [])
  return True


def is_lifecycle_active(status, start_date, end_date, label):
  """Whether the habit was meant to be running on `label`.

  The original streak maths never looked at `Habit.status`, `start_date` or
  `end_date`, so archived and expired habits kept accruing streaks and counted
  as missed on days before they were even created.
  """
  if status is not None and status != Status.ACTIVE:
    return False
  if start_date is not None and label < start_date:
    return False
  if end_date is not None and label > end_date:
    return False
  return True


def resolve_day_state(
  *,
  label,
  today,
  schedule=None,
  status=Status.ACTIVE,
  habit_start_date=None,
  habit_end_date=None,
  log=None,
):
  """Resolve a single day label. See the module docstring for the rules."""
  target = day_target_for(schedule)
  is_future = label > today
  due = (
    not is_future
    and is_lifecycle_active(status, habit_start_date, habit_end_date, label)
    and is_due_on(schedule, label)
  )

  if is_future:
    state, completed, due_flag = DayState.PENDING, 0, False
  elif not due:
    state, completed, due_flag = DayState.NOT_DUE, 0, False
  elif log is None:
    state, completed, due_flag = DayState.MISSED, 0, True
  elif log['status'] == Status.SKIPPED:
    state, completed, due_flag = DayState.SKIPPED, log.get('completed_count') or 0, True
  else:
    completed = log.get('completed_count') or 0
    if log['status'] == Status.COMPLETED and completed >= target:
      state = DayState.HIT
    elif completed > 0:
      state = DayState.PARTIAL
    else:
      state = DayState.MISSED
    due_flag = True

  return DayOutcome(
    date=label,
    weekday=weekday_of(label),
    is_due=due_flag,
    state=state,
    target=target,
    completed_count=completed,
    is_future=is_future,
  )


def normalise_logs(logs_by_day):
  """Re-key a log mapping onto normalised day labels.

  `HabitLog.date` carries no midnight constraint at the database level, so a
  client posting a mid-day timestamp would otherwise silently split one
  calendar day across two labels.
  """
  if not logs_by_day:
    return {}
  return {normalise_label(label): log for label, log in logs_by_day.items()}


def resolve_day_states(
  *,
  today,
  start,
  end=None,
  schedule=None,
  status=Status.ACTIVE,
  habit_start_date=None,
  habit_end_date=None,
  logs_by_day=None,
):
  """Resolve every day label in `[start, end]`, defaulting `end` to `today`.

  `logs_by_day` maps a day label to `{'status': ..., 'completed_count': ...}`.
  """
  if end is None:
    end = today
  logs = normalise_logs(logs_by_day)
  return [
    resolve_day_state(
      label=label,
      today=today,
      schedule=schedule,
      status=status,
      habit_start_date=habit_start_date,
      habit_end_date=habit_end_date,
      log=logs.get(label),
    )
    for label in date_range(start, end)
  ]


def build_periods(day_outcomes, schedule=None):
  """Collapse resolved days into the granularity the habit is scored at.

  WEEKLY habits get one period per ISO week whose target is the weekly
  `target_count`, so a habit due every day but only needing three reps a week
  is not scored as seven separate failures.
  """
  if score_unit_for(schedule) == ScoreUnit.DAY:
    return [_period_from_day(day) for day in day_outcomes]
  return _build_weekly_periods(day_outcomes, schedule)


def _period_from_day(day):
  return Period(
    start=day.date,
    end=day.date,
    state=day.state,
    due_days=1 if day.is_due else 0,
    target=day.target,
    completed_total=day.completed_count,
    weekday=day.weekday,
  )


def _build_weekly_periods(days, schedule):
  """Group resolved days into Monday-anchored weeks.

  A bucket is flagged `is_partial` when it does not span the full seven days:
  the trailing week, because `today` cuts it short, and the leading week if the
  caller started mid-week. `queries.analyse_habit` snaps boundaries back to the
  Monday for weekly habits, so in practice only the trailing week is partial -
  and it is also the one `metrics.is_in_progress` treats as still running.
  """
  target = target_for(schedule)
  grouped = []
  for day in days:
    if not grouped or week_start(day.date) != week_start(grouped[-1][0].date):
      grouped.append([])
    grouped[-1].append(day)
  return [_weekly_period(bucket, target) for bucket in grouped]


def _weekly_period(days, target):
  due_days = [day for day in days if day.is_due]
  completed_total = sum(day.completed_count for day in due_days)

  if not due_days:
    state = DayState.NOT_DUE
  elif completed_total >= target:
    state = DayState.HIT
  elif completed_total > 0:
    state = DayState.PARTIAL
  elif any(day.state == DayState.SKIPPED for day in due_days):
    state = DayState.SKIPPED
  else:
    state = DayState.MISSED

  return Period(
    start=days[0].date,
    end=days[-1].date,
    state=state,
    due_days=len(due_days),
    target=target,
    completed_total=completed_total,
    weekday=days[-1].weekday,
    is_partial=days[0].date != week_start(days[0].date) or len(days) < 7,
  )