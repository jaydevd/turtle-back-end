"""Group challenge lifecycle, participation and winner scoring.

A challenge is a `Habit` flagged `is_challenge`, owned by the member who
proposed it. Subscribing does not write into that shared row - see
`Habit.challenge_source` for why - it gives the member their own copy, so every
participant's days resolve against their own logs and one member's silence never
becomes another member's miss.

Three transitions live here, in the order they happen:

* `subscribe` / `unsubscribe`, while the challenge is DRAFT or ACTIVE,
* `start_challenge`, DRAFT to ACTIVE, by the author or a group admin,
* `refresh_challenge`, ACTIVE to COMPLETED once the final day has elapsed.

`refresh_challenge` is driven from the read paths rather than by a scheduler.
That is deliberate - there is no worker in this project - and it is written to
be a cheap no-op once the challenge has settled, so every read of a completed
challenge costs one indexed read.

Scoring answers the question the feature is built around: who won, judged on
discipline, consistency and adherence to the challenge rules.
"""

from django.db.models import Q

from common.enums import ChallengeStatus, DurationType, GroupRole, Status
from common.utils import get_unix_timestamp

from .analytics import calendar as analytics_calendar
from .analytics import metrics as analytics_metrics
from .analytics import queries as analytics_queries
from .models import ChallengeSubscription, Habit, HabitSchedule

# ---- Challenge configuration ----

MAX_CHALLENGE_RULES = 10

SCORING_VERSION = 'challenge-v1'

SCORE_WEIGHTS = {
  'discipline': 0.40,
  'consistency': 0.40,
  'adherence': 0.20,
}

# A subscriber who barely engaged cannot win, however well they did on the days
# they showed up: one perfect Sunday would otherwise beat a member who logged
# every single day of a month and missed one.
MIN_ENGAGEMENT_RATIO = 0.5

# How much of the consistency component is best-streak coverage against how much
# is freedom from lapses.
CONSISTENCY_STREAK_WEIGHT = 0.6

# Free-text rules cannot be machine-checked yet, so adherence is measured from
# the strictest automatable signal available: the share of due days fully met with
# no partial credit. Stored alongside the score so the figure is never mistaken
# for a literal reading of the rules, and so a structured rule type can replace
# it without changing the payload shape.
ADHERENCE_SOURCE = 'strict_rate_proxy'

TIE_POLICY = 'all_top_scorers_win'


def validate_rules(rules):
  """Normalise challenge rules, or raise ValueError naming the problem."""
  if rules is None:
    return []
  if not isinstance(rules, list):
    raise ValueError('Rules must be a list of strings.')
  if len(rules) > MAX_CHALLENGE_RULES:
    raise ValueError(
      f'A challenge can have at most {MAX_CHALLENGE_RULES} rules.'
    )

  cleaned = []
  for index, rule in enumerate(rules):
    if not isinstance(rule, str) or not rule.strip():
      raise ValueError(f'Rule {index + 1} must be a non-empty string.')
    cleaned.append(rule.strip())

  return cleaned


def validate_challenge_window(duration_type, end_date):
  """A challenge needs a final day, because the winner is read off it."""
  if duration_type != DurationType.FIXED or end_date is None:
    raise ValueError(
      'A challenge must have a fixed duration with an end date.'
    )


# ---- Participant copies ----


def participant_habits(challenge):
  """Every habit carrying this challenge's progress, template included."""
  return Habit.objects.filter(is_deleted=False).filter(
    Q(pk=challenge.pk) | Q(challenge_source_id=challenge.pk)
  )


def build_participant_copy(challenge, user):
  """A member's own copy of `challenge`, with the same schedule and dates."""
  copy = Habit.objects.create(
    user=user,
    name=challenge.name,
    goal=challenge.goal,
    tag_id=challenge.tag_id,
    group=challenge.group,
    start_date=challenge.start_date,
    end_date=challenge.end_date,
    duration_type=challenge.duration_type,
    reminder=challenge.reminder,
    status=Status.ACTIVE,
    color=challenge.color,
    icon=challenge.icon,
    is_challenge=True,
    challenge_status=challenge.challenge_status,
    challenge_rules=list(challenge.challenge_rules or []),
    challenge_source=challenge,
    challenge_started_by=challenge.challenge_started_by,
    challenge_started_at=challenge.challenge_started_at,
    challenge_ended_at=challenge.challenge_ended_at,
  )

  template_schedule = getattr(challenge, 'schedule', None)
  if template_schedule is not None:
    HabitSchedule.objects.create(
      habit=copy,
      frequency_type=template_schedule.frequency_type,
      target_count=template_schedule.target_count,
      weekdays=list(template_schedule.weekdays or []),
      scheduled_time=template_schedule.scheduled_time,
    )

  return copy


def subscribe(challenge, user):
  """Join `challenge`, materialising the member's copy of it.

  Idempotent on `ChallengeSubscription`'s unique constraint: re-subscribing
  returns the existing row rather than a second copy of the habit.
  """
  subscription = ChallengeSubscription.objects.filter(
    challenge=challenge, user=user
  ).first()
  if subscription is not None:
    return subscription, False

  subscription = ChallengeSubscription.objects.create(
    challenge=challenge, user=user
  )

  # The author participates through the template itself, so only subscribers who
  # are not the author need their own row.
  if challenge.user_id != user.id:
    build_participant_copy(challenge, user)

  return subscription, True


def unsubscribe(challenge, user):
  """Leave `challenge`, discarding the member's copy and its logs."""
  deleted, _ = ChallengeSubscription.objects.filter(
    challenge=challenge, user=user
  ).delete()

  Habit.objects.filter(challenge_source=challenge, user=user).update(
    is_deleted=True
  )

  return bool(deleted)


def sync_challenge_copies(challenge):
  """Push the template's lifecycle fields onto every copy.

  Used when the challenge starts or ends: subscribers must not be left logging
  against a habit that still reads DRAFT, and their completion state has to move
  in step with the challenge they belong to.
  """
  return Habit.objects.filter(challenge_source=challenge).update(
    challenge_status=challenge.challenge_status,
    challenge_ended_at=challenge.challenge_ended_at,
    challenge_started_at=challenge.challenge_started_at,
    challenge_started_by=challenge.challenge_started_by,
    updated_at=get_unix_timestamp(),
  )


# ---- Lifecycle ----


def challenge_window(challenge):
  """`(start_label, end_label)` for the challenge, both normalised."""
  return (
    analytics_calendar.normalise_label(challenge.start_date),
    analytics_calendar.normalise_label(challenge.end_date),
  )


def today_label(now=None):
  if now is None:
    return analytics_calendar.today_label()
  return analytics_calendar.day_label_for(now)


def refresh_challenge(challenge, now=None):
  """Complete an ACTIVE challenge whose final day has passed.

  Returns True when this call is the one that settled it. The comparison is
  strictly greater than the end label because the end label is itself a due day:
  completing on it would reject the final day's check-in, which is exactly the
  day the winner is decided on.
  """
  if challenge.challenge_status != ChallengeStatus.ACTIVE:
    return False

  _, end = challenge_window(challenge)
  if end is None:
    return False

  if today_label(now) <= end:
    return False

  complete_challenge(challenge)
  return True


def start_challenge(challenge, actor):
  """DRAFT to ACTIVE. `actor` is the author or a group admin."""
  if not challenge.is_challenge_template:
    raise ValueError('Only the challenge itself can be started.')
  if challenge.challenge_status != ChallengeStatus.DRAFT:
    raise ValueError('This challenge has already been started.')

  _, end = challenge_window(challenge)
  validate_challenge_window(challenge.duration_type, end)

  challenge.challenge_status = ChallengeStatus.ACTIVE
  challenge.challenge_started_by = actor
  challenge.challenge_started_at = get_unix_timestamp()
  challenge.save(update_fields=[
    'challenge_status',
    'challenge_started_by',
    'challenge_started_at',
    'updated_at',
  ])

  sync_challenge_copies(challenge)
  return challenge


def complete_challenge(challenge):
  """ACTIVE or DRAFT to COMPLETED, with the winner scored and stored.

  `Habit.status` is deliberately left alone. `resolve_day_states` treats any
  habit whose status is not ACTIVE as not due on every day, so completing the
  habit as well would erase the very days the winner is computed from. The
  challenge lifecycle lives in `challenge_status` for exactly this reason.
  """
  if challenge.challenge_status == ChallengeStatus.COMPLETED:
    return challenge

  challenge.challenge_status = ChallengeStatus.COMPLETED
  challenge.challenge_ended_at = get_unix_timestamp()

  payload = build_winner_payload(challenge)
  winners = payload.get('winners') or []
  # Ties are all announced as winners; `challenge_winner` keeps the first so the
  # column stays a single reference, and the full set lives in the score JSON.
  #
  # The id has to be wrapped in a User. The scorer reports `user_id` as a string
  # because it serialises to JSON, but assigning that straight to the FK raises
  # "must be a User instance" - and only on the completion path, so it would sit
  # unnoticed until the first challenge actually ended.
  if winners:
    challenge.challenge_winner_id = winners[0]['user_id']
  else:
    challenge.challenge_winner = None
  challenge.challenge_winner_score = payload

  challenge.save(update_fields=[
    'challenge_status',
    'challenge_ended_at',
    'challenge_winner',
    'challenge_winner_score',
    'updated_at',
  ])

  sync_challenge_copies(challenge)
  return challenge


def cancel_challenge(challenge):
  if challenge.challenge_status in (
    ChallengeStatus.COMPLETED,
    ChallengeStatus.CANCELLED,
  ):
    raise ValueError('This challenge is already closed.')

  challenge.challenge_status = ChallengeStatus.CANCELLED
  challenge.challenge_ended_at = get_unix_timestamp()
  challenge.save(update_fields=[
    'challenge_status',
    'challenge_ended_at',
    'updated_at',
  ])

  sync_challenge_copies(challenge)
  return challenge


def winner_ids(challenge):
  """Every user id announced as a winner, ties included."""
  payload = challenge.challenge_winner_score or {}
  return [row['user_id'] for row in payload.get('winners') or []]


def is_group_admin(user, group):
  """Whether `user` may administer `group`. Shares one definition with the
  permission classes, so the API and the challenge actions cannot disagree."""
  if user is None or not user.is_authenticated or group is None:
    return False

  from groups.models import GroupMembership

  membership = GroupMembership.objects.filter(group=group, user=user).first()
  if membership is None:
    return False

  return membership.role in (GroupRole.OWNER, GroupRole.ADMIN)


def can_manage_challenge(user, challenge):
  """The author starts and ends their challenge; group admins can stand in."""
  if challenge is None:
    return False
  if challenge.user_id == getattr(user, 'id', None):
    return True
  return is_group_admin(user, challenge.group)


# ---- Scoring ----


def load_challenge_analyses(challenge, now=None):
  """`HabitAnalysis` per subscriber, keyed by habit id.

  Bounded to the challenge's own window rather than the caller's analytics
  window: a challenge is scored over exactly the days it ran, and a 30-day default
  would silently drop month-long challenges off the leaderboard.
  """
  habits = list(
    participant_habits(challenge).select_related('tag', 'schedule')
  )
  if not habits:
    return {}

  start, end = challenge_window(challenge)
  today = today_label(now)
  resolved_today = end if (end is not None and today > end) else today

  if start is None:
    start = resolved_today

  history_days = max(
    1, analytics_calendar.days_between(start, resolved_today) + 1
  )
  logs = analytics_queries.load_logs(habits, start, resolved_today)
  totals = analytics_queries.load_total_hits(habits)

  return {
    habit.id: analytics_queries.analyse_habit(
      habit=habit,
      schedule=getattr(habit, 'schedule', None),
      today=resolved_today,
      window_days=history_days,
      history_days=history_days,
      logs_by_day=logs.get(habit.id, {}),
      total_completions=totals.get(habit.id, 0),
    )
    for habit in habits
  }


def _consistency_component(periods, scored_days, today):
  """Best-streak coverage weighted against freedom from lapses.

  A member who never breaks a run scores full marks; one who keeps restarting
  does not, however many days they eventually banked.
  """
  if not scored_days:
    return 0.0

  streaks = analytics_metrics.compute_streaks(periods, today)
  lapses = analytics_metrics.compute_lapses(periods, today)

  coverage = min(1.0, streaks['best_streak'] / scored_days)
  recovery = max(0.0, 1.0 - (len(lapses) / scored_days))

  return round(
    100.0
    * (
      CONSISTENCY_STREAK_WEIGHT * coverage
      + (1 - CONSISTENCY_STREAK_WEIGHT) * recovery
    ),
    2,
  )


def _discipline_component(periods):
  """Reps delivered against reps required, falling back to the day rate.

  `target_adherence` is the sharper measure but is None for habits whose target
  is one, where it collapses to the same thing `completion_rate` reports.
  """
  adherence = analytics_metrics.target_adherence(periods)
  if adherence is None:
    adherence = analytics_metrics.completion_rate(periods)
  return round(adherence if adherence is not None else 0.0, 2)


def _adherence_component(periods):
  """Full-hit days over due days. See `ADHERENCE_SOURCE`."""
  return round(analytics_metrics.strict_rate(periods) or 0.0, 2)


def score_participant(analysis, logged_days):
  """One row of the leaderboard: components, the weighted total, and evidence."""
  periods = analysis.periods
  counts = analytics_metrics.count_states(periods)
  scored_days = counts['scored']

  discipline = _discipline_component(periods)
  consistency = _consistency_component(periods, scored_days, analysis.today)
  adherence = _adherence_component(periods)

  total = round(
    sum(
      value * SCORE_WEIGHTS[key]
      for key, value in (
        ('discipline', discipline),
        ('consistency', consistency),
        ('adherence', adherence),
      )
    ),
    2,
  )

  engagement_ratio = (
    round(min(1.0, logged_days / scored_days), 3) if scored_days else 0.0
  )
  eligible = engagement_ratio >= MIN_ENGAGEMENT_RATIO

  streaks = analytics_metrics.compute_streaks(periods, analysis.today)

  return {
    'user_id': str(analysis.habit.user_id),
    'user_email': analysis.habit.user.email,
    'participation': 'eligible' if eligible else 'below_minimum_engagement',
    'eligible': eligible,
    'total': total,
    'components': {
      'discipline': discipline,
      'consistency': consistency,
      'adherence': adherence,
    },
    'engagement': {
      'logged_days': logged_days,
      'scored_days': scored_days,
      'ratio': engagement_ratio,
    },
    'evidence': {
      'completion_rate': analytics_metrics.completion_rate(periods),
      'best_streak': streaks['best_streak'],
      'hit_days': counts['hit'],
      'partial_days': counts['partial'],
      'missed_days': counts['missed'],
      'skipped_days': counts['skipped'],
      'lapse_count': len(analytics_metrics.compute_lapses(periods, analysis.today)),
    },
  }


def build_winner_payload(challenge, now=None):
  """The stored result of a completed challenge.

  The whole leaderboard is snapshotted, not just the winner: a score is only
  meaningful against the rules that produced it, and recomputing it later would
  quietly change history if the weights are ever retuned. `SCORING_VERSION`
  travels with the payload for the same reason.
  """
  analyses = load_challenge_analyses(challenge, now=now)

  rows = []
  for habit_id, analysis in analyses.items():
    rows.append(score_participant(analysis, len(analysis.logs_by_day or {})))

  rows.sort(key=lambda row: row['total'], reverse=True)

  eligible = [row for row in rows if row['eligible']]
  winners = []
  if eligible:
    top = eligible[0]['total']
    winners = [row for row in eligible if row['total'] == top]

  return {
    'scoring_version': SCORING_VERSION,
    'weights': SCORE_WEIGHTS,
    'tie_policy': TIE_POLICY,
    'minimum_engagement_ratio': MIN_ENGAGEMENT_RATIO,
    'adherence_source': ADHERENCE_SOURCE,
    'rules': list(challenge.challenge_rules or []),
    'rules_count': len(challenge.challenge_rules or []),
    'winner_total': winners[0]['total'] if winners else None,
    'winners': winners,
    'leaderboard': rows,
  }


def progress_rows(challenge, now=None, include_ineligible=True):
  """Per-subscriber progress, for the group-facing progress endpoint.

  Every subscriber is returned whether or not they meet the winner threshold,
  so a member who quietly stopped taking part is visible rather than simply
  absent from the board.
  """
  payload = build_winner_payload(challenge, now=now)
  rows = payload['leaderboard']
  if include_ineligible:
    return rows
  return [row for row in rows if row['eligible']]


def sync_and_settle(challenge, now=None):
  """`refresh_challenge` plus a re-read, for the caller's response body."""
  if refresh_challenge(challenge, now=now):
    challenge.refresh_from_db()
  return challenge