"""Analytics and insight layer for habit history.

Layering, outermost first:

* `queries` - the only module that touches the database; builds `HabitAnalysis`
  objects that pair a habit's resolved days and periods with its metrics,
* `overview` / `forecast` / `patterns` / `trends` - report builders that turn a
  set of analyses into response payloads,
* `metrics` - rates, streaks, lapses, volatility and the consistency score,
* `resolver` - the primitive every other module depends on, turning raw
  `HabitLog` rows into resolved per-day outcomes and per-period scores,
* `calendar` - IST constants and day-label arithmetic.

Nothing below `queries` reads the clock or the database: `today` is always passed
in, which is what keeps the metric functions unit-testable without a test
database.
"""

from .calendar import (
  APP_TIMEZONE,
  SECONDS_PER_DAY,
  WEEKDAY_LABELS,
  date_range,
  day_label_for,
  label_to_date,
  today_label,
  weekday_of,
  week_start,
)
from .metrics import (
  SCORING_VERSION,
  SCORE_WEIGHTS,
  build_habit_metrics,
  completion_rate,
  compute_lapses,
  compute_streaks,
  consistency_score,
  strict_rate,
  target_adherence,
  trend_direction,
  volatility,
)
from .resolver import (
  DayOutcome,
  Period,
  build_periods,
  day_target_for,
  is_due_on,
  resolve_day_states,
  score_unit_for,
  target_for,
)

__all__ = [
  'APP_TIMEZONE',
  'DayOutcome',
  'Period',
  'SCORE_WEIGHTS',
  'SCORING_VERSION',
  'SECONDS_PER_DAY',
  'WEEKDAY_LABELS',
  'build_habit_metrics',
  'build_periods',
  'completion_rate',
  'compute_lapses',
  'compute_streaks',
  'consistency_score',
  'date_range',
  'day_label_for',
  'day_target_for',
  'is_due_on',
  'label_to_date',
  'resolve_day_states',
  'score_unit_for',
  'strict_rate',
  'target_adherence',
  'target_for',
  'today_label',
  'trend_direction',
  'volatility',
  'week_start',
  'weekday_of',
]