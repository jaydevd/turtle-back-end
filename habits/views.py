import uuid

from django.db.models import Count, ProtectedError, Q
from django.http import Http404
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ModelViewSet, ViewSet
from . import challenges as habit_challenges
from .analytics import calendar as analytics_calendar
from .analytics import forecast as analytics_forecast
from .analytics import metrics as analytics_metrics
from .analytics import overview as analytics_overview
from .analytics import patterns as analytics_patterns
from .analytics import queries as analytics_queries
from .analytics import trends as analytics_trends
from .models import Habit, HabitLog, Tag
from .serializers import (
  ChallengeSubscriptionSerializer,
  HabitLogSerializer,
  HabitScheduleSerializer,
  HabitSerializer,
  TagSerializer,
)
from common.constants import HTTP_ERROR_CODES, RESPONSE_MESSAGES
from common.enums import ChallengeStatus
from common.pagination import StandardPagination
from common.responses import error_response, success_response
import traceback


class AnalyticsParamsMixin:
  """Resolves the shared `window` and `granularity` query parameters.

  Invalid values become the project's `411 VALIDATION_ERROR` envelope rather
  than a bare 400, matching every other validation path in the app.
  """

  def today_label(self):
    return analytics_calendar.today_label(timezone.now())

  def window_days(self, default=analytics_queries.DEFAULT_WINDOW_DAYS):
    return analytics_queries.resolve_window(self.request, default)

  def granularity(self, default=analytics_trends.GRANULARITY_DAY):
    return analytics_trends.resolve_granularity(self.request, default)

  def scoped_habit_id(self):
    """The optional single-habit filter, or None for "all of my habits".

    Accepts `habit_id` (matching the `/habits/<uuid:habit_id>/insights/` route)
    with `habit` as an alias. Raises 404 when the id is not one of the caller's
    own habits: silently falling back to the full set would answer a question
    about habit X with data about habits Y and Z.
    """
    raw = self.request.query_params.get('habit_id')
    if raw in (None, ''):
      raw = self.request.query_params.get('habit')
    if raw in (None, ''):
      return None

    try:
      parsed = uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
      raise NotFound('habit not found.')

    owned = analytics_queries.load_habits(self.request.user, habit_ids=[parsed])
    if not owned:
      raise NotFound('habit not found.')

    return [parsed]

  def param_error(self, message):
    return error_response(
      status_code=HTTP_ERROR_CODES['VALIDATION_ERROR'],
      message=RESPONSE_MESSAGES['VALIDATION_ERROR'],
      errors={'detail': message},
    )

  def window_start_for(self, today, window_days):
    return analytics_calendar.add_days(today, -(window_days - 1))

  def analyses(self, today, window_days, habit_ids=None):
    return analytics_queries.load_analyses(
      self.request.user,
      today=today,
      window_days=window_days,
      history_days=analytics_queries.DEFAULT_HISTORY_DAYS,
      habit_ids=habit_ids,
    )


class EnvelopeListMixin:
  """Shared envelope-aware list/create plumbing for the habit viewsets."""

  list_message = RESPONSE_MESSAGES['SUCCESS']
  not_found_message = 'Record not found.'

  def paginated_payload(self, request, *args, **kwargs):
    queryset = self.filter_queryset(self.get_queryset())
    page = self.paginate_queryset(queryset)
    serializer = self.get_serializer(page, many=True)
    paginator = self.paginator
    return {
      'count': paginator.page.paginator.count,
      'next': paginator.get_next_link(),
      'previous': paginator.get_previous_link(),
      'results': serializer.data,
    }

  def list(self, request, *args, **kwargs):
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=self.list_message,
      data=self.paginated_payload(request, *args, **kwargs),
    )

  def validation_error_response(self, serializer):
    return error_response(
      status_code=HTTP_ERROR_CODES['VALIDATION_ERROR'],
      message=RESPONSE_MESSAGES['VALIDATION_ERROR'],
      errors=serializer.errors,
    )

  def handle_exception(self, exc):
    if isinstance(exc, (Http404, NotFound)):
      return error_response(
        status_code=HTTP_ERROR_CODES['NOT_FOUND'],
        message=self.not_found_message,
        errors={'detail': str(getattr(exc, 'detail', exc))},
      )
    return super().handle_exception(exc)


class HabitViewSet(EnvelopeListMixin, ModelViewSet):
  serializer_class = HabitSerializer
  permission_classes = [IsAuthenticated]
  pagination_class = StandardPagination
  not_found_message = 'Habit not found.'

  def get_queryset(self):
    return (
      Habit.objects
      .filter(user=self.request.user, is_deleted=False)
      .select_related('tag', 'schedule')
    )

  def create(self, request, *args, **kwargs):
    try:
      serializer = self.get_serializer(data=request.data)
      if not serializer.is_valid():
        return self.validation_error_response(serializer)

      serializer.save(user=request.user)
      return success_response(
        status_code=HTTP_ERROR_CODES['CREATED'],
        message=RESPONSE_MESSAGES['CREATED'],
        data=serializer.data,
      )
    except Exception as e:
      traceback.print_exc()
      return error_response(
        status_code=HTTP_ERROR_CODES['SERVER_ERROR'],
        message=RESPONSE_MESSAGES['SERVER_ERROR'],
        errors=str(e),
      )

  def retrieve(self, request, *args, **kwargs):
    habit = self.get_object()
    serializer = self.get_serializer(habit)
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data=serializer.data,
    )

  def update(self, request, *args, **kwargs):
    partial = kwargs.pop('partial', False)
    habit = self.get_object()
    serializer = self.get_serializer(habit, data=request.data, partial=partial)
    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    serializer.save()
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['DATA_UPDATED'],
      data=serializer.data,
    )

  def partial_update(self, request, *args, **kwargs):
    kwargs['partial'] = True
    return self.update(request, *args, **kwargs)

  def destroy(self, request, *args, **kwargs):
    habit = self.get_object()
    habit.is_deleted = True
    habit.save(update_fields=['is_deleted', 'updated_at'])
    return success_response(
      status_code=HTTP_ERROR_CODES['200_NO_CONTENT'],
      message=RESPONSE_MESSAGES['DATA_DELETED'],
    )

  # ---- Group challenges ----

  def get_challenge(self):
    """The habit as a challenge template, or 404.

    Participants hold a copy of a challenge rather than the template, so a
    subscriber asking for their own habit must be routed back to the challenge it
    came from. Without that, every participant's copy would report itself as an
    unstartable challenge with no rules and no subscribers.

    Resolution deliberately does not go through `get_object`. That is scoped to
    the caller's own habits, but a challenge belongs to the group: every member
    reads its progress and subscribes to it, so scoping the lookup to ownership
    would hide the challenge from everyone but its author. The two cases are kept
    apart here - the caller's own habit is taken as-is, and a habit belonging to
    somebody else is only accepted when it is a group challenge the caller is a
    member of. `assert_group_member` then re-checks membership on the resolved
    challenge, so the authorisation does not rest on this lookup alone.
    """
    from groups.services import is_member

    try:
      habit = Habit.objects.filter(is_deleted=False).select_related(
        'challenge_source', 'tag', 'schedule'
      ).get(pk=self.kwargs['pk'])
    except (Habit.DoesNotExist, ValueError):
      raise NotFound('Habit not found.')

    challenge = habit.challenge_source or habit
    if not challenge.is_challenge:
      raise NotFound('Habit not found.')

    # Somebody else's habit is only reachable as a group challenge they are in.
    if habit.user_id != self.request.user.id and not (
      habit.group_id is not None and is_member(self.request.user, habit.group)
    ):
      raise NotFound('Habit not found.')

    return habit_challenges.sync_and_settle(challenge)

  def assert_group_member(self, request, challenge):
    """Group membership is the only gate on reading a challenge.

    A non-member gets a 404 rather than a 403: they are outside the group, and
    confirming the challenge exists would leak it.
    """
    from groups.services import is_member

    if challenge.group_id is None or not is_member(request.user, challenge.group):
      raise NotFound('Habit not found.')

  @action(detail=True, methods=['post'])
  def start_challenge(self, request, *args, **kwargs):
    """DRAFT to ACTIVE, by the author or a group admin.

    Starting is manual on purpose: a proposed challenge is a plan, and the group
    gets to see it before anybody's streak starts counting.
    """
    challenge = self.get_challenge()
    self.assert_group_member(request, challenge)

    if not habit_challenges.can_manage_challenge(request.user, challenge):
      return _forbidden('Only the challenge author or a group admin can start it.')

    try:
      habit_challenges.start_challenge(challenge, request.user)
    except ValueError as exc:
      return self.challenge_error(exc)

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message='Challenge started.',
      data=self.get_serializer(challenge).data,
    )

  @action(detail=True, methods=['post'])
  def end_challenge(self, request, *args, **kwargs):
    """Close a challenge early and announce its winner.

    The normal ending is automatic, at the end of the final day. This exists for
    the two cases that are not: an author standing down, and an admin shutting
    down a challenge nobody is running.
    """
    challenge = self.get_challenge()
    self.assert_group_member(request, challenge)

    if not habit_challenges.can_manage_challenge(request.user, challenge):
      return _forbidden('Only the challenge author or a group admin can end it.')

    if challenge.challenge_status == ChallengeStatus.COMPLETED:
      return self.challenge_error('This challenge has already ended.')

    habit_challenges.complete_challenge(challenge)

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message='Challenge ended.',
      data=self.get_serializer(challenge).data,
    )

  @action(detail=True, methods=['post'])
  def cancel_challenge(self, request, *args, **kwargs):
    challenge = self.get_challenge()
    self.assert_group_member(request, challenge)

    if not habit_challenges.can_manage_challenge(request.user, challenge):
      return _forbidden('Only the challenge author or a group admin can cancel it.')

    try:
      habit_challenges.cancel_challenge(challenge)
    except ValueError as exc:
      return self.challenge_error(exc)

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message='Challenge cancelled.',
      data=self.get_serializer(challenge).data,
    )

  @action(detail=True, methods=['post', 'delete'])
  def subscribe(self, request, *args, **kwargs):
    """Join a challenge, or leave one.

    Joining hands the member their own copy of the habit, schedule and all. The
    story says every member can watch everyone's progress whether or not they
    took part, so opting in only affects what the member is scored on - it is not
    what makes the challenge visible to them.
    """
    challenge = self.get_challenge()
    self.assert_group_member(request, challenge)

    if request.method == 'POST':
      if challenge.challenge_status not in (
        ChallengeStatus.DRAFT,
        ChallengeStatus.ACTIVE,
      ):
        return self.challenge_error(
          'This challenge is closed and can no longer be joined.'
        )

      subscription, created = habit_challenges.subscribe(challenge, request.user)
      return success_response(
        status_code=HTTP_ERROR_CODES['CREATED' if created else 'SUCCESS'],
        message='Subscribed to the challenge.' if created else 'Already subscribed.',
        data=ChallengeSubscriptionSerializer(subscription).data,
      )

    removed = habit_challenges.unsubscribe(challenge, request.user)
    if not removed:
      return error_response(
        status_code=HTTP_ERROR_CODES['NOT_FOUND'],
        message='You are not subscribed to this challenge.',
        errors={'detail': 'No subscription to remove.'},
      )

    return success_response(
      status_code=HTTP_ERROR_CODES['200_NO_CONTENT'],
      message=RESPONSE_MESSAGES['DATA_DELETED'],
    )

  @action(detail=True, methods=['get'])
  def subscribers(self, request, *args, **kwargs):
    challenge = self.get_challenge()
    self.assert_group_member(request, challenge)

    queryset = challenge.subscriptions.select_related('user').order_by('-subscribed_at')
    page = self.paginate_queryset(queryset)
    paginator = self.paginator

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data={
        'count': paginator.page.paginator.count,
        'next': paginator.get_next_link(),
        'previous': paginator.get_previous_link(),
        'results': ChallengeSubscriptionSerializer(page, many=True).data,
      },
    )

  @action(detail=True, methods=['get'])
  def progress(self, request, *args, **kwargs):
    """Every subscriber's progress on this challenge.

    Readable by every member of the group, whether or not they subscribed -
    watching a challenge you did not enter is the point. Rows carry the full
    scoring breakdown, so the leaderboard needs no second call.
    """
    challenge = self.get_challenge()
    self.assert_group_member(request, challenge)

    payload = habit_challenges.progress_rows(challenge)

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data={
        'challenge': {
          'id': str(challenge.id),
          'name': challenge.name,
          'challenge_status': challenge.challenge_status,
          'challenge_rules': list(challenge.challenge_rules or []),
          'challenge_winner': (
            str(challenge.challenge_winner) if challenge.challenge_winner_id else None
          ),
          'challenge_winners': habit_challenges.winner_ids(challenge),
          'start_date': challenge.start_date,
          'end_date': challenge.end_date,
          'participant_count': challenge.subscriptions.count(),
        },
        'scoring_version': habit_challenges.SCORING_VERSION,
        'weights': habit_challenges.SCORE_WEIGHTS,
        'minimum_engagement_ratio': habit_challenges.MIN_ENGAGEMENT_RATIO,
        'results': payload,
      },
    )

  def challenge_error(self, message):
    return error_response(
      status_code=HTTP_ERROR_CODES['VALIDATION_ERROR'],
      message=RESPONSE_MESSAGES['VALIDATION_ERROR'],
      errors={'detail': str(message)},
    )


def _forbidden(message):
  return error_response(
    status_code=HTTP_ERROR_CODES['FORBIDDEN'],
    message=RESPONSE_MESSAGES['FORBIDDEN'],
    errors={'detail': message},
  )


class TagViewSet(EnvelopeListMixin, ModelViewSet):
  serializer_class = TagSerializer
  permission_classes = [IsAuthenticated]
  pagination_class = StandardPagination
  not_found_message = 'Tag not found.'

  def get_queryset(self):
    return (
      Tag.objects
      .filter(user=self.request.user)
      .annotate(
        habit_count=Count(
          'habits',
          filter=Q(habits__is_deleted=False),
          distinct=True,
        ),
      )
      .order_by('name', 'id')
    )

  def create(self, request, *args, **kwargs):
    serializer = self.get_serializer(data=request.data)
    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    serializer.save(user=request.user)
    return success_response(
      status_code=HTTP_ERROR_CODES['CREATED'],
      message=RESPONSE_MESSAGES['CREATED'],
      data=serializer.data,
    )

  def retrieve(self, request, *args, **kwargs):
    serializer = self.get_serializer(self.get_object())
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data=serializer.data,
    )

  def update(self, request, *args, **kwargs):
    partial = kwargs.pop('partial', False)
    instance = self.get_object()
    serializer = self.get_serializer(instance, data=request.data, partial=partial)

    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    serializer.save()
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['DATA_UPDATED'],
      data=serializer.data,
    )

  def partial_update(self, request, *args, **kwargs):
    kwargs['partial'] = True
    return self.update(request, *args, **kwargs)

  def destroy(self, request, *args, **kwargs):
    tag = self.get_object()
    try:
      self.perform_destroy(tag)
    except ProtectedError:
      return error_response(
        status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
        message='Tag is in use and cannot be deleted.',
        errors={'tag': 'Remove or reassign habits using this tag before deleting it.'},
      )
    return success_response(
      status_code=HTTP_ERROR_CODES['200_NO_CONTENT'],
      message=RESPONSE_MESSAGES['DATA_DELETED'],
    )


class HabitLogViewSet(EnvelopeListMixin, ModelViewSet):
  """Daily check-in records. POST upserts on the (habit, date) unique
  constraint so the client can tap the same day repeatedly."""

  serializer_class = HabitLogSerializer
  permission_classes = [IsAuthenticated]
  pagination_class = StandardPagination
  not_found_message = 'Log entry not found.'

  def get_queryset(self):
    queryset = HabitLog.objects.filter(habit__user=self.request.user)
    habit_id = self.request.query_params.get('habit')
    if habit_id:
      queryset = queryset.filter(habit_id=habit_id)
    start = self.request.query_params.get('start')
    if start:
      queryset = queryset.filter(date__gte=int(start))
    end = self.request.query_params.get('end')
    if end:
      queryset = queryset.filter(date__lt=int(end))
    return queryset.select_related('habit').order_by('date')

  def create(self, request, *args, **kwargs):
    serializer = self.get_serializer(data=request.data)
    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    habit = serializer.validated_data['habit']
    date = serializer.validated_data['date']

    # Defaults `completed_at` to now when the client omits it. Without this the
    # field stayed null for every check-in, which is the only sub-day signal in
    # the schema, so the hour-of-day and punctuality insights had no data.
    completed_at = serializer.validated_data.get('completed_at') or timezone.now()

    instance, created = HabitLog.objects.update_or_create(
      habit=habit,
      date=date,
      defaults={
        'status': serializer.validated_data['status'],
        'completed_count': serializer.validated_data.get('completed_count', 0),
        'note': serializer.validated_data.get('note', ''),
        'completed_at': completed_at,
      },
    )
    payload = self.get_serializer(instance).data
    return success_response(
      status_code=HTTP_ERROR_CODES['CREATED' if created else 'SUCCESS'],
      message=RESPONSE_MESSAGES['CREATED' if created else 'DATA_UPDATED'],
      data=payload,
    )

  def retrieve(self, request, *args, **kwargs):
    serializer = self.get_serializer(self.get_object())
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data=serializer.data,
    )

  def update(self, request, *args, **kwargs):
    partial = kwargs.pop('partial', False)
    instance = self.get_object()
    serializer = self.get_serializer(instance, data=request.data, partial=partial)
    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    serializer.save()
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['DATA_UPDATED'],
      data=serializer.data,
    )

  def partial_update(self, request, *args, **kwargs):
    kwargs['partial'] = True
    return self.update(request, *args, **kwargs)

  def destroy(self, request, *args, **kwargs):
    self.perform_destroy(self.get_object())
    return success_response(
      status_code=HTTP_ERROR_CODES['200_NO_CONTENT'],
      message=RESPONSE_MESSAGES['DATA_DELETED'],
    )


class HabitScheduleViewSet(EnvelopeListMixin, ViewSet):
  """Nested at /api/user/habits/<uuid:habit_id>/schedule/."""

  serializer_class = HabitScheduleSerializer
  permission_classes = [IsAuthenticated]
  not_found_message = 'Schedule not found.'

  def get_habit(self):
    try:
      return Habit.objects.get(
        id=self.kwargs['habit_id'],
        user=self.request.user,
        is_deleted=False,
      )
    except Habit.DoesNotExist:
      raise NotFound('Habit not found.')

  def retrieve(self, request, *args, **kwargs):
    habit = self.get_habit()
    schedule = getattr(habit, 'schedule', None)
    data = HabitScheduleSerializer(schedule).data if schedule is not None else None
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data=data,
    )

  def create(self, request, *args, **kwargs):
    return self.upsert(request, force=True)

  def update(self, request, *args, **kwargs):
    return self.upsert(request)

  def partial_update(self, request, *args, **kwargs):
    return self.upsert(request)

  def upsert(self, request, force=False):
    habit = self.get_habit()
    partial = request.method == 'PATCH'
    serializer = HabitScheduleSerializer(
      getattr(habit, 'schedule', None),
      data=request.data,
      partial=partial,
    )
    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    serializer.save(habit=habit)
    return success_response(
      status_code=HTTP_ERROR_CODES['CREATED' if force else 'SUCCESS'],
      message=RESPONSE_MESSAGES['CREATED' if force else 'DATA_UPDATED'],
      data=serializer.data,
    )


class HabitStatsViewSet(AnalyticsParamsMixin, ViewSet):
  """Aggregates streaks, completion counts and due-today flags.

  Powers the dashboard stat tiles and habit detail cards. The response keeps
  every field name it has always had; the values behind them are now correct.
  See `habits/analytics/` for what was wrong and how the numbers are derived.
  """

  permission_classes = [IsAuthenticated]

  def list(self, request, *args, **kwargs):
    try:
      window_days = self.window_days()
    except ValueError as exc:
      return self.param_error(str(exc))

    today = self.today_label()

    analyses = self.analyses(today, window_days, habit_ids=self.scoped_habit_id())

    rows = [self._row(analysis) for analysis in analyses]

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data={
        'count': len(rows),
        'next': None,
        'previous': None,
        'results': rows,
        'window_days': window_days,
        'today': today,
        'history_days': analytics_queries.DEFAULT_HISTORY_DAYS,
        'timezone': str(analytics_calendar.APP_TIMEZONE),
      },
    )

  def _row(self, analysis):
    """One stat row.

    The first block reproduces the historical contract exactly. Everything below
    it is additive, so the existing dashboard and habit cards keep working
    against an unchanged shape.
    """
    habit = analysis.habit
    metrics = analysis.metrics
    counts = analytics_metrics.count_states(analysis.window_periods)

    return {
      'id': str(habit.id),
      'name': habit.name,
      'color': habit.color,
      'icon': habit.icon,
      'tag': str(habit.tag_id),
      'status': habit.status,
      'current_streak': metrics['current_streak'],
      'best_streak': metrics['best_streak'],
      'total_completions': analysis.total_completions,
      'completion_rate': metrics['completion_rate'],
      'scheduled_days': counts['scored'],
      'logged_days': _logged_days(analysis),
      'due_today': analysis.is_outstanding(),
      'consistency_score': metrics['consistency_score'],
      'consistency_components': metrics['consistency_components'],
      'scoring_version': metrics['scoring_version'],
      'strict_rate': metrics['strict_rate'],
      'target_adherence': metrics['target_adherence'],
      'score_unit': analysis.score_unit,
      'daily_target': analysis.day_target,
      'hit_days': counts['hit'],
      'partial_days': counts['partial'],
      'missed_days': counts['missed'],
      'skipped_days': counts['skipped'],
      'scored_days': counts['scored'],
      'previous_completion_rate': metrics['previous_completion_rate'],
      'rate_delta': metrics['rate_delta'],
      'trend': metrics['trend'],
      'current_streak_start': metrics['current_streak_start'],
      'best_streak_start': metrics['best_streak_start'],
      'best_streak_end': metrics['best_streak_end'],
      'partial_in_current_streak': metrics['partial_in_current_streak'],
      'days_since_last_hit': metrics['days_since_last_hit'],
      'last_hit_date': metrics['last_hit_date'],
      'volatility': metrics['volatility'],
      'lapse_count': metrics['lapse_count'],
      'longest_lapse': metrics['longest_lapse'],
      'next_milestone': metrics['next_milestone'],
    }


def _logged_days(analysis):
  """Days in the window carrying any log at all, matching the old field.

  Scoped to the window, because its sibling `scheduled_days` counts scored days
  in the window only. Iterating `analysis.days` instead covered the whole
  history, so a habit logged for two years and viewed on a 30-day window reported
  a `logged_days` far larger than `scheduled_days`.
  """
  window = analysis.window_periods
  if not window:
    return 0
  return sum(
    1 for day in analysis.days if window[0].start <= day.date <= window[-1].end
    and day.date in analysis.logs_by_day
  )


class HabitOverviewViewSet(AnalyticsParamsMixin, ViewSet):
  """`GET /insights/overview/` - the user-level rollup across every habit."""

  permission_classes = [IsAuthenticated]

  def list(self, request, *args, **kwargs):
    try:
      window_days = self.window_days()
    except ValueError as exc:
      return self.param_error(str(exc))

    today = self.today_label()
    window_start = self.window_start_for(today, window_days)
    previous_start = self.window_start_for(window_start, window_days)

    analyses = self.analyses(today, window_days, habit_ids=self.scoped_habit_id())

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data={
        'today': today,
        'window_days': window_days,
        'window_start': window_start,
        'previous_window_start': previous_start if previous_start < window_start else None,
        'timezone': str(analytics_calendar.APP_TIMEZONE),
        **analytics_overview.build_overview(
          analyses, today, window_start, previous_start
        ),
      },
    )


class HabitPatternsViewSet(AnalyticsParamsMixin, ViewSet):
  """`GET /insights/patterns/` - weekday, hour-of-day, tag and month profiles."""

  permission_classes = [IsAuthenticated]

  def list(self, request, *args, **kwargs):
    try:
      window_days = self.window_days(default=90)
    except ValueError as exc:
      return self.param_error(str(exc))

    today = self.today_label()
    analyses = self.analyses(today, window_days, habit_ids=self.scoped_habit_id())

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data={
        'today': today,
        'window_days': window_days,
        'timezone': str(analytics_calendar.APP_TIMEZONE),
        'weekday_profile': analytics_patterns.habit_by_weekday_rates(analyses),
        'tag_profile': analytics_patterns.tag_profile(analyses),
        'habits': [
          {
            'id': str(analysis.habit_id),
            'name': analysis.habit.name,
            'color': analysis.habit.color,
            'icon': analysis.habit.icon,
            **analytics_patterns.build_pattern_report(analysis),
          }
          for analysis in analyses
        ],
      },
    )


class HabitTrendViewSet(AnalyticsParamsMixin, ViewSet):
  """`GET /insights/trend/` - chart series and the heatmap grid."""

  permission_classes = [IsAuthenticated]

  def list(self, request, *args, **kwargs):
    try:
      window_days = self.window_days()
      granularity = self.granularity()
    except ValueError as exc:
      return self.param_error(str(exc))

    today = self.today_label()
    window_start = self.window_start_for(today, window_days)
    analyses = self.analyses(today, window_days, habit_ids=self.scoped_habit_id())

    series = analytics_trends.user_daily_series(
      analyses, window_start, today, granularity=granularity
    )

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data={
        'today': today,
        'window_days': window_days,
        'window_start': window_start,
        'granularity': granularity,
        'timezone': str(analytics_calendar.APP_TIMEZONE),
        'combined': series,
        'habits': [
          {
            'id': str(analysis.habit_id),
            'name': analysis.habit.name,
            'color': analysis.habit.color,
            'icon': analysis.habit.icon,
            **analytics_trends.build_trend_report(analysis, granularity=granularity),
          }
          for analysis in analyses
        ],
      },
    )


class HabitRiskViewSet(AnalyticsParamsMixin, ViewSet):
  """`GET /insights/risk/` - streaks at risk, decay, struggling habits, load."""

  permission_classes = [IsAuthenticated]

  def list(self, request, *args, **kwargs):
    try:
      window_days = self.window_days()
    except ValueError as exc:
      return self.param_error(str(exc))

    today = self.today_label()
    now = timezone.now()
    analyses = self.analyses(today, window_days, habit_ids=self.scoped_habit_id())

    report = analytics_forecast.build_risk_report(analyses, today, now=now)

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data={
        'today': today,
        'window_days': window_days,
        'timezone': str(analytics_calendar.APP_TIMEZONE),
        'summary': analytics_forecast.summarise_risk(report),
        **report,
      },
    )


class HabitDetailInsightsViewSet(AnalyticsParamsMixin, ViewSet):
  """`GET /<uuid:habit_id>/insights/` - the deep single-habit breakdown."""

  permission_classes = [IsAuthenticated]

  def get_habit(self):
    try:
      return Habit.objects.get(
        id=self.kwargs['habit_id'],
        user=self.request.user,
        is_deleted=False,
      )
    except (Habit.DoesNotExist, ValueError):
      raise NotFound('Habit not found.')

  def list(self, request, *args, **kwargs):
    try:
      window_days = self.window_days(default=90)
    except ValueError as exc:
      return self.param_error(str(exc))

    habit = self.get_habit()
    today = self.today_label()
    window_start = self.window_start_for(today, window_days)

    analyses = self.analyses(today, window_days, habit_ids=[habit.id])
    if not analyses:
      return error_response(
        status_code=HTTP_ERROR_CODES['NOT_FOUND'],
        message='Habit not found.',
        errors={'habit': 'No analysis available for this habit.'},
      )

    analysis = analyses[0]

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data={
        'habit': {
          'id': str(habit.id),
          'name': habit.name,
          'color': habit.color,
          'icon': habit.icon,
          'tag': str(habit.tag_id),
          'tag_name': habit.tag.name,
          'status': habit.status,
          'goal': habit.goal,
          'duration_type': habit.duration_type,
          'start_date': habit.start_date,
          'end_date': habit.end_date,
          'reminder': habit.reminder,
        },
        'schedule': (
          HabitScheduleSerializer(habit.schedule).data
          if hasattr(habit, 'schedule')
          else None
        ),
        'today': today,
        'window_days': window_days,
        'window_start': window_start,
        'history_days': analytics_queries.DEFAULT_HISTORY_DAYS,
        'timezone': str(analytics_calendar.APP_TIMEZONE),
        'metrics': {
          **analysis.metrics,
          'lapses': analytics_metrics.compute_lapses(analysis.periods, today),
        },
        'patterns': analytics_patterns.build_pattern_report(analysis),
        'trends': analytics_trends.build_trend_report(analysis),
        'risk': {
          'completion_probability_today': (
            analytics_forecast.completion_probability_today(analysis, today)
          ),
          'is_streak_at_risk': (
            (analysis.metric('current_streak') or 0) > 0 and analysis.is_outstanding()
          ),
          'seconds_remaining_in_day': analytics_calendar.seconds_until_day_end(
            today, timezone.now()
          ),
        },
      },
    )


class HabitDashboardViewSet(ViewSet):
  """Single aggregated call for the dashboard screen: stats plus today's logs."""

  permission_classes = [IsAuthenticated]

  def list(self, request, *args, **kwargs):
    today_ts = analytics_calendar.today_label(timezone.now())

    habits = list(
      Habit.objects
      .filter(user=request.user, is_deleted=False)
      .select_related('tag', 'schedule')
      .order_by('-created_at')
    )

    today_logs = {
      log['habit_id']: log
      for log in HabitLog.objects.filter(
        habit_id__in=[habit.id for habit in habits],
        date__gte=today_ts,
        date__lt=today_ts + analytics_calendar.SECONDS_PER_DAY,
      ).values('habit_id', 'date', 'status', 'completed_count', 'note')
    }

    tag_rows = list(
      Tag.objects
      .filter(user=request.user)
      .annotate(
        habit_count=Count(
          'habits',
          filter=Q(habits__is_deleted=False),
          distinct=True,
        ),
      )
      .order_by('name')
      .values('id', 'name', 'habit_count')
    )

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data={
        'today': today_ts,
        'timezone': str(analytics_calendar.APP_TIMEZONE),
        'habits': [
          {
            'id': str(habit.id),
            'name': habit.name,
            'color': habit.color,
            'icon': habit.icon,
            'reminder': habit.reminder,
            'status': habit.status,
            'tag': str(habit.tag_id),
            'tag_name': habit.tag.name,
            'schedule': (
              HabitScheduleSerializer(habit.schedule).data
              if hasattr(habit, 'schedule')
              else None
            ),
            'log': _serialise_log(today_logs.get(habit.id)),
          }
          for habit in habits
        ],
        'tags': [
          {**row, 'id': str(row['id'])}
          for row in tag_rows
        ],
      },
    )


def _serialise_log(log):
  """Strip the UUID so the payload matches every other id in the response.

  The raw `.values()` row leaked a `UUID` object here, which the client received
  as a non-string while every surrounding id was stringified.
  """
  if log is None:
    return None
  return {
    'habit_id': str(log['habit_id']),
    'date': log['date'],
    'status': log['status'],
    'completed_count': log['completed_count'],
    'note': log['note'],
  }
