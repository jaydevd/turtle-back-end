"""Tests for group challenges: lifecycle, participation and winner scoring.

Two things are worth saying about how these are set up.

`now` is injected rather than read from the clock. Challenge completion is
defined relative to the end date, and a test that waited for real days to pass
could only ever assert the happy path once. Every helper takes `today` so the
auto-complete boundary can be probed from both sides.

Logs are written per participant *copy*, not onto the template. That is the whole
reason the copy exists, so these tests deliberately log against different copies
and assert that one member's streak never leaks into another's score.
"""

import datetime as dt

from django.urls import reverse
from rest_framework import status as http
from rest_framework.test import APITestCase

from common.enums import ChallengeStatus, DurationType, GroupRole, Status
from common.utils import get_unix_timestamp

from groups.models import Group, GroupMembership
from groups.tests.factories import make_user

from .. import challenges as habit_challenges
from ..analytics import calendar as analytics_calendar
from ..models import ChallengeSubscription, Habit, HabitLog, HabitSchedule, Tag
from .factories import label, log_habit_days, make_habit, make_schedule

UTC = dt.timezone.utc

START = label(2024, 3, 1)
END = label(2024, 3, 7)


def instant(day_label):
  """A real `datetime` at noon UTC on the day a label names."""
  return dt.datetime.fromtimestamp(day_label, tz=UTC) + dt.timedelta(hours=12)


def days(start_label, count, step=1):
  return [
    start_label + offset * analytics_calendar.SECONDS_PER_DAY
    for offset in range(count)
  ]


class ChallengeTestCase(APITestCase):
  """A group with three members, and a DRAFT challenge proposed by one of them."""

  def setUp(self):
    self.author = make_user('author@example.com')
    self.rival = make_user('rival@example.com')
    self.quitter = make_user('quitter@example.com')
    self.outsider = make_user('outsider@example.com')

    self.group = Group.objects.create(name='Morning Crew', owner=self.author)
    GroupMembership.objects.create(
      group=self.group, user=self.author, role=GroupRole.OWNER
    )
    GroupMembership.objects.create(
      group=self.group, user=self.rival, role=GroupRole.MEMBER
    )
    GroupMembership.objects.create(
      group=self.group, user=self.quitter, role=GroupRole.MEMBER
    )

    self.tag = Tag.objects.create(user=self.author, name='Health')
    self.challenge = make_challenge(self.author, self.tag, group=self.group)

  def as_user(self, user):
    self.client.force_authenticate(user)
    return user

  def copy_for(self, challenge, user):
    return Habit.objects.get(challenge_source=challenge, user=user)


def make_challenge(user, tag, group=None, start=START, end=END, rules=None):
  challenge = make_habit(
    user,
    tag,
    name='30-day plank',
    start_date=start,
    end_date=end,
    duration_type=DurationType.FIXED,
  )
  challenge.group = group
  challenge.is_challenge = True
  challenge.challenge_status = ChallengeStatus.DRAFT
  challenge.challenge_rules = rules if rules is not None else ['Hold for one minute.']
  challenge.save()
  make_schedule(challenge, frequency_type='DAILY', target_count=1)
  return challenge


class ChallengeCreationTestCase(ChallengeTestCase):
  def post(self, expect=http.HTTP_201_CREATED, sender=None, **overrides):
    payload = {
      'name': 'Morning Plank',
      'goal': 'Hold for one minute.',
      'tag': str(self.tag.id),
      'duration_type': DurationType.FIXED,
      'start_date': START,
      'end_date': END,
      'challenge_rules': ['Hold for one minute.', 'Every day, no skipping.'],
    }
    payload.update(overrides)

    self.as_user(sender or self.author)
    response = self.client.post(
      reverse(
        'group-challenge-list-create', kwargs={'group_id': str(self.group.id)}
      ),
      payload,
      format='json',
    )
    self.assertEqual(
      response.status_code, expect, msg=f'{response.status_code} {response.data}'
    )
    return response

  def test_proposing_a_challenge_creates_a_draft(self):
    response = self.post()

    data = response.data['data']
    self.assertTrue(data['is_challenge'])
    self.assertEqual(data['challenge_status'], ChallengeStatus.DRAFT)
    challenge = Habit.objects.get(pk=data['id'])
    self.assertEqual(challenge.group_id, self.group.id)
    self.assertEqual(len(challenge.challenge_rules), 2)
    self.assertIsNone(challenge.challenge_started_at)

  def test_the_challenge_flag_is_set_by_being_a_challenge_not_by_having_rules(self):
    """Rules alone do not make a habit a challenge - the endpoint does. So a
    challenge with no rules still carries the flag, and an ordinary habit with no
    rules does not."""
    self.post(challenge_rules=[])

    challenge = Habit.objects.get(name='Morning Plank')
    self.assertTrue(challenge.is_challenge)
    self.assertEqual(challenge.challenge_rules, [])

    plain = make_habit(self.author, self.tag, name='Just a habit')
    self.assertFalse(plain.is_challenge)
    self.assertFalse(plain.is_challenge_template)

  def test_a_challenge_needs_an_end_date(self):
    response = self.post(end_date=None, expect=http.HTTP_411_LENGTH_REQUIRED)

    self.assertIn('end_date', response.data['errors'])

  def test_a_challenge_must_be_fixed_length(self):
    self.post(
      duration_type=DurationType.INDEFINITE, expect=http.HTTP_411_LENGTH_REQUIRED
    )

  def test_too_many_rules_are_refused(self):
    self.post(
      challenge_rules=[f'Rule {index}' for index in range(11)],
      expect=http.HTTP_411_LENGTH_REQUIRED,
    )

  def test_a_blank_rule_is_refused(self):
    self.post(challenge_rules=['fine', '   '], expect=http.HTTP_411_LENGTH_REQUIRED)

  def test_a_non_member_cannot_propose_a_challenge(self):
    self.post(sender=self.outsider, expect=http.HTTP_404_NOT_FOUND)

  def test_admins_can_close_challenge_creation_to_plain_members(self):
    self.group.anyone_can_create_challenge = False
    self.group.save(update_fields=['anyone_can_create_challenge', 'updated_at'])

    # The rival is a plain MEMBER, so the switch closes the door on them.
    self.post(sender=self.rival, expect=http.HTTP_403_FORBIDDEN)

    # The author is the OWNER and keeps the right regardless of the switch.
    self.post(sender=self.author, expect=http.HTTP_201_CREATED)

    self.post(sender=self.quitter, expect=http.HTTP_403_FORBIDDEN)


class ChallengeLifecycleTestCase(ChallengeTestCase):
  def start(self, user=None, expect=http.HTTP_200_OK):
    self.as_user(user or self.author)
    response = self.client.post(
      reverse(
        'habit-start-challenge', kwargs={'pk': str(self.challenge.id)}
      )
    )
    self.assertEqual(
      response.status_code, expect, msg=f'{response.status_code} {response.data}'
    )
    return response

  def test_starting_moves_draft_to_active(self):
    self.start()

    self.challenge.refresh_from_db()
    self.assertEqual(self.challenge.challenge_status, ChallengeStatus.ACTIVE)
    self.assertEqual(self.challenge.challenge_started_by_id, self.author.id)
    self.assertIsNotNone(self.challenge.challenge_started_at)

  def test_starting_twice_is_refused(self):
    self.start()
    self.start(expect=http.HTTP_411_LENGTH_REQUIRED)

  def test_a_plain_member_cannot_start_somebody_elses_challenge(self):
    self.start(user=self.rival, expect=http.HTTP_403_FORBIDDEN)

    self.challenge.refresh_from_db()
    self.assertEqual(self.challenge.challenge_status, ChallengeStatus.DRAFT)

  def test_the_group_admin_may_start_it_for_the_author(self):
    self.group.memberships.filter(user=self.author).update(role=GroupRole.OWNER)
    admin = make_user('admin@example.com')
    GroupMembership.objects.create(
      group=self.group, user=admin, role=GroupRole.ADMIN
    )

    self.start(user=admin)

    self.challenge.refresh_from_db()
    self.assertEqual(self.challenge.challenge_status, ChallengeStatus.ACTIVE)

  def test_starting_pushes_the_state_onto_every_participant_copy(self):
    habit_challenges.subscribe(self.challenge, self.rival)
    copy = self.copy_for(self.challenge, self.rival)
    self.assertEqual(copy.challenge_status, ChallengeStatus.DRAFT)

    self.start()

    copy.refresh_from_db()
    self.assertEqual(copy.challenge_status, ChallengeStatus.ACTIVE)

  def test_cancelling_closes_it_without_a_winner(self):
    self.as_user(self.author)
    response = self.client.post(
      reverse(
        'habit-cancel-challenge', kwargs={'pk': str(self.challenge.id)}
      )
    )

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    self.challenge.refresh_from_db()
    self.assertEqual(self.challenge.challenge_status, ChallengeStatus.CANCELLED)
    self.assertIsNone(self.challenge.challenge_winner)

  def test_a_cancelled_challenge_cannot_be_started(self):
    self.as_user(self.author)
    self.client.post(
      reverse('habit-cancel-challenge', kwargs={'pk': str(self.challenge.id)})
    )
    self.start(expect=http.HTTP_411_LENGTH_REQUIRED)


class ChallengeAutoCompleteTestCase(ChallengeTestCase):
  """The end date is a due day, so completion must land strictly after it."""

  def refresh(self, now, expect_completed, status=None):
    completed = habit_challenges.refresh_challenge(self.challenge, now=now)
    self.challenge.refresh_from_db()
    self.assertEqual(completed, expect_completed)
    if status is not None:
      self.assertEqual(self.challenge.challenge_status, status)

  def test_an_active_challenge_does_not_complete_on_its_final_day(self):
    habit_challenges.start_challenge(self.challenge, self.author)

    self.refresh(instant(END), False, ChallengeStatus.ACTIVE)

  def test_it_completes_the_day_after_the_final_day(self):
    habit_challenges.start_challenge(self.challenge, self.author)

    self.refresh(
      instant(END + analytics_calendar.SECONDS_PER_DAY), True, ChallengeStatus.COMPLETED
    )

  def test_it_does_not_complete_before_the_window_closes(self):
    habit_challenges.start_challenge(self.challenge, self.author)

    self.refresh(instant(START), False, ChallengeStatus.ACTIVE)

  def test_a_draft_challenge_does_not_complete_on_its_own(self):
    """Auto-completion belongs to started challenges. A proposal nobody ran is
    the author's to cancel, not something that quietly finishes itself."""
    self.refresh(
      instant(END + analytics_calendar.SECONDS_PER_DAY * 3),
      False,
      ChallengeStatus.DRAFT,
    )

  def test_completing_records_the_winner_and_a_snapshot(self):
    habit_challenges.start_challenge(self.challenge, self.author)
    habit_challenges.subscribe(self.challenge, self.author)
    habit_challenges.subscribe(self.challenge, self.rival)
    log_habit_days(self.challenge, START, days(START, 7))
    log_habit_days(self.copy_for(self.challenge, self.rival), START, days(START, 7))

    self.refresh(
      instant(END + analytics_calendar.SECONDS_PER_DAY), True, ChallengeStatus.COMPLETED
    )

    payload = self.challenge.challenge_winner_score
    self.assertEqual(payload['scoring_version'], habit_challenges.SCORING_VERSION)
    self.assertEqual(payload['tie_policy'], habit_challenges.TIE_POLICY)
    self.assertEqual(payload['rules'], self.challenge.challenge_rules)
    self.assertEqual(
      str(self.challenge.challenge_winner_id), str(self.author.id)
    )

  def test_completing_leaves_the_habit_itself_active(self):
    """`resolve_day_states` treats a non-ACTIVE habit as due on no day at all, so
    completing the habit would erase the days the winner is computed from."""
    habit_challenges.start_challenge(self.challenge, self.author)

    self.refresh(
      instant(END + analytics_calendar.SECONDS_PER_DAY), True, ChallengeStatus.COMPLETED
    )

    self.assertEqual(self.challenge.status, Status.ACTIVE)

  def test_a_read_settles_a_challenge_whose_window_has_passed(self):
    """There is no scheduler in this project, so the read path is what runs the
    transition. The fixture window is in the past, so reading settles it."""
    habit_challenges.start_challenge(self.challenge, self.author)
    self.assertEqual(
      habit_challenges.refresh_challenge(self.challenge, now=instant(START)),
      False,
    )

    self.as_user(self.author)
    self.client.get(
      reverse('habit-challenge-progress', kwargs={'pk': str(self.challenge.id)})
    )

    self.challenge.refresh_from_db()
    self.assertEqual(self.challenge.challenge_status, ChallengeStatus.COMPLETED)
    self.assertIsNotNone(self.challenge.challenge_ended_at)

  def test_a_read_leaves_a_live_challenge_alone(self):
    """The other half of the same guard: settling must not fire early just
    because somebody looked."""
    live = make_challenge(
      self.author,
      self.tag,
      start=label(2030, 1, 1),
      end=label(2030, 1, 7),
    )
    habit_challenges.start_challenge(live, self.author)

    self.as_user(self.author)
    self.client.get(
      reverse('habit-challenge-progress', kwargs={'pk': str(live.id)})
    )

    live.refresh_from_db()
    self.assertEqual(live.challenge_status, ChallengeStatus.ACTIVE)
    self.assertIsNone(live.challenge_ended_at)


class ChallengeSubscriptionTestCase(ChallengeTestCase):
  def subscribe(self, user=None, expect=http.HTTP_201_CREATED, method='post'):
    self.as_user(user or self.rival)
    url = reverse(
      'habit-challenge-subscribe', kwargs={'pk': str(self.challenge.id)}
    )
    response = self.client.post(url) if method == 'post' else self.client.delete(url)
    self.assertEqual(
      response.status_code, expect, msg=f'{response.status_code} {response.data}'
    )
    return response

  def test_subscribing_creates_a_subscription(self):
    self.subscribe()

    subscription = ChallengeSubscription.objects.get(
      challenge=self.challenge, user=self.rival
    )
    self.assertIsNotNone(subscription.subscribed_at)

  def test_subscribing_hands_over_a_personal_copy(self):
    self.subscribe()

    copy = self.copy_for(self.challenge, self.rival)
    self.assertEqual(copy.name, self.challenge.name)
    self.assertEqual(copy.challenge_source_id, self.challenge.id)
    self.assertEqual(copy.start_date, self.challenge.start_date)
    self.assertEqual(copy.end_date, self.challenge.end_date)

  def test_the_copy_carries_the_schedule_so_the_days_are_due(self):
    self.subscribe()

    copy = self.copy_for(self.challenge, self.rival)
    self.assertEqual(copy.schedule.frequency_type, 'DAILY')
    self.assertEqual(copy.schedule.target_count, 1)

  def test_the_copy_keeps_the_rules_readable(self):
    self.subscribe()

    copy = self.copy_for(self.challenge, self.rival)
    self.assertEqual(copy.challenge_rules, self.challenge.challenge_rules)

  def test_subscribing_twice_does_not_duplicate_anything(self):
    self.subscribe()
    self.subscribe(expect=http.HTTP_200_OK)

    self.assertEqual(
      ChallengeSubscription.objects.filter(
        challenge=self.challenge, user=self.rival
      ).count(),
      1,
    )
    self.assertEqual(
      Habit.objects.filter(challenge_source=self.challenge, user=self.rival).count(),
      1,
    )

  def test_the_author_participates_through_the_template(self):
    """The author already owns a habit carrying the challenge; giving them a
    second copy would split their own streak in two."""
    self.subscribe(user=self.author)

    self.assertFalse(
      Habit.objects.filter(challenge_source=self.challenge, user=self.author).exists()
    )

  def test_leaving_discards_the_copy(self):
    self.subscribe()

    self.subscribe(method='delete', expect=http.HTTP_204_NO_CONTENT)

    copy = Habit.objects.get(challenge_source=self.challenge, user=self.rival)
    self.assertTrue(copy.is_deleted)
    self.assertFalse(
      ChallengeSubscription.objects.filter(
        challenge=self.challenge, user=self.rival
      ).exists()
    )

  def test_leaving_when_not_subscribed_is_a_404(self):
    self.subscribe(method='delete', expect=http.HTTP_404_NOT_FOUND)

  def test_a_closed_challenge_can_no_long_be_joined(self):
    self.as_user(self.author)
    self.client.post(
      reverse('habit-cancel-challenge', kwargs={'pk': str(self.challenge.id)})
    )

    self.subscribe(expect=http.HTTP_411_LENGTH_REQUIRED)

  def test_a_non_member_cannot_subscribe(self):
    self.subscribe(user=self.outsider, expect=http.HTTP_404_NOT_FOUND)

    self.assertFalse(ChallengeSubscription.objects.exists())

  def test_the_subscriber_list_is_readable_by_any_group_member(self):
    self.subscribe()

    self.as_user(self.quitter)
    response = self.client.get(
      reverse(
        'habit-challenge-subscribers', kwargs={'pk': str(self.challenge.id)}
      )
    )

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    self.assertEqual(response.data['data']['count'], 1)
    self.assertEqual(
      response.data['data']['results'][0]['user_email'], 'rival@example.com'
    )

  def test_the_subscriber_list_is_closed_to_non_members(self):
    self.as_user(self.outsider)
    response = self.client.get(
      reverse(
        'habit-challenge-subscribers', kwargs={'pk': str(self.challenge.id)}
      )
    )

    self.assertEqual(response.status_code, http.HTTP_404_NOT_FOUND, msg=str(response.data))


class ChallengeProgressTestCase(ChallengeTestCase):
  def setUp(self):
    super().setUp()
    habit_challenges.start_challenge(self.challenge, self.author)
    for user in (self.author, self.rival, self.quitter):
      habit_challenges.subscribe(self.challenge, user)

  def progress(self, user, expect=http.HTTP_200_OK):
    self.as_user(user)
    response = self.client.get(
      reverse('habit-challenge-progress', kwargs={'pk': str(self.challenge.id)})
    )
    self.assertEqual(
      response.status_code, expect, msg=f'{response.status_code} {response.data}'
    )
    return response.data['data']

  def test_every_group_member_may_read_progress_without_subscribing(self):
    """Watching is separate from taking part - the story asks for both."""
    fresh = make_user('watcher@example.com')
    GroupMembership.objects.create(group=self.group, user=fresh)

    data = self.progress(fresh)

    self.assertEqual(data['challenge']['participant_count'], 3)
    self.assertEqual(len(data['results']), 3)

  def test_progress_is_scoped_to_the_challenges_own_window(self):
    data = self.progress(self.author)

    row = next(
      r for r in data['results'] if r['user_email'] == 'rival@example.com'
    )
    self.assertEqual(row['engagement']['scored_days'], 7)

  def test_a_non_member_cannot_read_progress(self):
    self.progress(self.outsider, expect=http.HTTP_404_NOT_FOUND)

  def test_one_members_logs_do_not_reach_anothers_score(self):
    """The reason participants hold copies. A perfect week logged against the
    author's habit must not show up as the rival's streak."""
    log_habit_days(self.challenge, START, days(START, 7))
    log_habit_days(self.copy_for(self.challenge, self.rival), START, days(START, 7))

    data = self.progress(self.author)

    by_email = {row['user_email']: row for row in data['results']}
    self.assertEqual(by_email['author@example.com']['evidence']['hit_days'], 7)
    self.assertEqual(by_email['rival@example.com']['evidence']['hit_days'], 7)
    self.assertEqual(by_email['quitter@example.com']['evidence']['hit_days'], 0)

  def test_a_partial_day_scores_below_a_full_one(self):
    log_habit_days(self.challenge, START, days(START, 7))
    partial_copy = self.copy_for(self.challenge, self.rival)
    HabitLog.objects.create(
      habit=partial_copy,
      date=START,
      status='PARTIAL',
      completed_count=0,
    )

    data = self.progress(self.author)

    by_email = {row['user_email']: row for row in data['results']}
    self.assertGreater(
      by_email['author@example.com']['total'], by_email['rival@example.com']['total']
    )


class ChallengeScoringTestCase(ChallengeTestCase):
  """Direct calls into the scorer, so the arithmetic can be asserted exactly."""

  def setUp(self):
    super().setUp()
    habit_challenges.start_challenge(self.challenge, self.author)

  def run_scorer(self):
    return habit_challenges.build_winner_payload(
      self.challenge, now=instant(END)
    )

  def row_for(self, payload, user):
    return next(
      row for row in payload['leaderboard'] if row['user_id'] == str(user.id)
    )

  def test_a_clean_week_beats_an_empty_one(self):
    habit_challenges.subscribe(self.challenge, self.author)
    habit_challenges.subscribe(self.challenge, self.rival)
    log_habit_days(self.challenge, START, days(START, 7))

    payload = self.run_scorer()

    winner = payload['winners'][0]
    self.assertEqual(winner['user_email'], 'author@example.com')
    self.assertEqual(winner['total'], 100.0)

  def test_a_tie_announces_everyone_at_the_top(self):
    habit_challenges.subscribe(self.challenge, self.author)
    habit_challenges.subscribe(self.challenge, self.rival)
    log_habit_days(self.challenge, START, days(START, 7))
    log_habit_days(self.copy_for(self.challenge, self.rival), START, days(START, 7))

    payload = self.run_scorer()

    self.assertEqual(len(payload['winners']), 2)
    self.assertEqual(
      {winner['user_email'] for winner in payload['winners']},
      {'author@example.com', 'rival@example.com'},
    )
    self.assertEqual(payload['winner_total'], 100.0)

  def test_a_subscriber_below_the_engagement_floor_cannot_win(self):
    """The rule the story asks for: opting out of participation, or drifting, must
    not be able to take the prize on a handful of good days."""
    habit_challenges.subscribe(self.challenge, self.author)
    habit_challenges.subscribe(self.challenge, self.rival)
    log_habit_days(self.challenge, START, days(START, 7))

    # The rival shows up for one day out of seven, under the 0.5 floor.
    HabitLog.objects.create(
      habit=self.copy_for(self.challenge, self.rival),
      date=START,
      status='COMPLETED',
      completed_count=1,
    )

    payload = self.run_scorer()

    rival = self.row_for(payload, self.rival)
    self.assertFalse(rival['eligible'])
    self.assertEqual(rival['participation'], 'below_minimum_engagement')
    self.assertLess(rival['engagement']['ratio'], 0.5)
    self.assertEqual(len(payload['winners']), 1)
    self.assertEqual(payload['winners'][0]['user_email'], 'author@example.com')

  def test_the_floor_is_inclusive_at_exactly_half(self):
    habit_challenges.subscribe(self.challenge, self.author)
    habit_challenges.subscribe(self.challenge, self.rival)
    log_habit_days(self.challenge, START, days(START, 7))

    # 3 of 7 days is 0.428..., so 4 of 7 is the first ratio that clears 0.5.
    HabitLog.objects.create(
      habit=self.copy_for(self.challenge, self.rival), date=START, status='COMPLETED'
    )
    for day in days(START, 3):
      HabitLog.objects.create(
        habit=self.copy_for(self.challenge, self.rival),
        date=day + analytics_calendar.SECONDS_PER_DAY,
        status='COMPLETED',
      )

    payload = self.run_scorer()

    self.assertTrue(self.row_for(payload, self.rival)['eligible'])

  def test_a_broken_run_scores_below_an_unbroken_one(self):
    habit_challenges.subscribe(self.challenge, self.author)
    habit_challenges.subscribe(self.challenge, self.rival)
    log_habit_days(self.challenge, START, days(START, 7))

    rival_copy = self.copy_for(self.challenge, self.rival)
    for day in days(START, 3):
      HabitLog.objects.create(habit=rival_copy, date=day, status='COMPLETED')
    for day in days(START + 4 * analytics_calendar.SECONDS_PER_DAY, 3):
      HabitLog.objects.create(habit=rival_copy, date=day, status='COMPLETED')

    payload = self.run_scorer()

    author = self.row_for(payload, self.author)
    rival = self.row_for(payload, self.rival)
    self.assertEqual(author['evidence']['lapse_count'], 0)
    self.assertGreater(author['evidence']['best_streak'], rival['evidence']['best_streak'])
    self.assertGreater(author['total'], rival['total'])

  def test_the_snapshot_records_how_it_was_scored(self):
    """A score is only meaningful next to the weights that produced it."""
    habit_challenges.subscribe(self.challenge, self.author)
    log_habit_days(self.challenge, START, days(START, 7))

    payload = self.run_scorer()

    self.assertEqual(payload['weights'], habit_challenges.SCORE_WEIGHTS)
    self.assertEqual(
      payload['adherence_source'], habit_challenges.ADHERENCE_SOURCE
    )
    self.assertEqual(
      payload['minimum_engagement_ratio'], habit_challenges.MIN_ENGAGEMENT_RATIO
    )
    self.assertEqual(payload['rules'], self.challenge.challenge_rules)
    self.assertEqual(payload['rules_count'], 1)

  def test_the_adherence_figure_is_labelled_as_a_proxy(self):
    """Free-text rules cannot be machine-checked, so the number is a stand-in and
    has to say so rather than passing itself off as a rules verdict."""
    habit_challenges.subscribe(self.challenge, self.author)
    log_habit_days(self.challenge, START, days(START, 7))

    payload = self.run_scorer()

    self.assertEqual(payload['adherence_source'], 'strict_rate_proxy')
    self.assertIn('adherence', payload['winners'][0]['components'])

  def test_every_participant_appears_on_the_board(self):
    habit_challenges.subscribe(self.challenge, self.author)
    habit_challenges.subscribe(self.challenge, self.rival)

    payload = self.run_scorer()

    self.assertEqual(len(payload['leaderboard']), 2)
    self.assertEqual(
      {row['user_email'] for row in payload['leaderboard']},
      {'author@example.com', 'rival@example.com'},
    )

  def test_a_challenge_nobody_joined_has_no_winner(self):
    payload = self.run_scorer()

    self.assertEqual(payload['winners'], [])
    self.assertIsNone(payload['winner_total'])


class ChallengeVisibilityTestCase(ChallengeTestCase):
  """The challenge belongs to the group, not only to its author."""

  def test_a_plain_member_may_read_the_challenge_it_did_not_author(self):
    self.as_user(self.quitter)
    response = self.client.get(
      reverse('habit-challenge-progress', kwargs={'pk': str(self.challenge.id)})
    )

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))

  def test_an_outsider_gets_a_404_not_a_403(self):
    """403 would confirm the habit exists; they are outside the group entirely."""
    self.as_user(self.outsider)
    response = self.client.get(
      reverse('habit-challenge-progress', kwargs={'pk': str(self.challenge.id)})
    )

    self.assertEqual(response.status_code, http.HTTP_404_NOT_FOUND, msg=str(response.data))

  def test_a_participants_own_copy_routes_back_to_the_challenge(self):
    habit_challenges.subscribe(self.challenge, self.rival)
    copy = self.copy_for(self.challenge, self.rival)

    self.as_user(self.rival)
    response = self.client.get(
      reverse('habit-challenge-progress', kwargs={'pk': str(copy.id)})
    )

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    self.assertEqual(response.data['data']['challenge']['id'], str(self.challenge.id))

  def test_a_private_habit_stays_private_even_to_a_group_member(self):
    """The widening is for challenges only. An ordinary habit belonging to
    somebody else must stay out of reach through this path."""
    private = make_habit(self.author, self.tag, name='Private journal')

    self.as_user(self.quitter)
    response = self.client.get(
      reverse('habit-challenge-progress', kwargs={'pk': str(private.id)})
    )

    self.assertEqual(response.status_code, http.HTTP_404_NOT_FOUND, msg=str(response.data))

  def test_the_group_challenge_list_shows_the_challenge_to_any_member(self):
    self.as_user(self.quitter)
    response = self.client.get(
      reverse(
        'group-challenge-list-create', kwargs={'group_id': str(self.group.id)}
      )
    )

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    self.assertEqual(response.data['data']['count'], 1)
