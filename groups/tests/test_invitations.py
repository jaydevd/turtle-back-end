"""API tests for email invitations.

An invitation is a promise made to an address, not to an account. Two invariants
matter: it cannot be minted for somebody who already has one (the join-request
channel exists for that), and it cannot enrol anybody on its own. Sending one
writes a pending join request against the address; registering links that request
to the new account, and the newcomer still has to accept it.
"""

from unittest import mock

from common.utils import get_unix_timestamp
from django.core import mail
from django.urls import reverse
from rest_framework import status as http
from rest_framework.test import APITestCase

from common.enums import InvitationStatus, JoinRequestStatus

from ..models import Group, GroupInvitation, GroupJoinRequest, GroupMembership
from .factories import join, make_group, make_user
from .test_groups_api import GroupApiTestCase, as_id


class InvitationSendingTestCase(GroupApiTestCase):
  def invite(self, email, expect=http.HTTP_201_CREATED, sender=None, **fields):
    self.as_user(sender or self.member)
    response = self.client.post(
      reverse(
        'group-invitation-list-create', kwargs={'group_id': str(self.group.id)}
      ),
      {'email': email, **fields},
      format='json',
    )
    self.assertEqual(
      response.status_code, expect, msg=f'{response.status_code} {response.data}'
    )
    return response

  def test_an_invitation_carries_a_token_and_a_link(self):
    response = self.invite('newcomer@example.com')

    data = response.data['data']
    invitation = GroupInvitation.objects.get(pk=data['id'])
    self.assertEqual(invitation.invited_by_id, self.member.id)
    self.assertEqual(invitation.status, InvitationStatus.PENDING)
    self.assertTrue(invitation.token)
    self.assertEqual(data['token'], str(invitation.token))

  def test_creating_an_invitation_emails_the_invitee_a_branded_link(self):
    self.invite('newcomer@example.com')

    self.assertEqual(len(mail.outbox), 1)
    message = mail.outbox[0]
    self.assertEqual(message.to, ['newcomer@example.com'])
    self.assertIn(self.group.name, message.subject)
    self.assertIn('/signup?email=', message.body)

    html, mimetype = message.alternatives[0]
    self.assertEqual(mimetype, 'text/html')
    self.assertIn('Turtle', html)
    # The link points at sign-up with the invited address filled in, so the
    # account they create is the one the waiting request is addressed to.
    self.assertIn('newcomer%40example.com', html)

  def test_an_invitation_writes_the_pending_join_request_immediately(self):
    """The email is the nudge; the request is the thing they answer. It is
    written at send time so it is already there the moment they register."""
    self.invite('newcomer@example.com')

    request = GroupJoinRequest.objects.get(
      group=self.group, to_email__iexact='newcomer@example.com'
    )
    self.assertEqual(request.from_user_id, self.member.id)
    self.assertIsNone(request.to_user)
    self.assertEqual(request.status, JoinRequestStatus.PENDING)

  def test_the_inviter_sees_the_request_in_their_sent_list(self):
    self.invite('newcomer@example.com')

    self.as_user(self.member)
    data = self.envelope_data(
      self.get(
        'group-join-request-list-create',
        route_kwargs={'group_id': str(self.group.id)},
        scope='outgoing',
      )
    )

    self.assertEqual(data['count'], 1)
    self.assertEqual(data['results'][0]['to_user_email'], 'newcomer@example.com')

  def test_a_failed_send_does_not_fail_the_invitation(self):
    """A mail outage must not undo the request: the invitation is stored and the
    pending request is written either way, so the newcomer who reaches the
    platform by any route still finds something waiting."""
    with mock.patch(
      'groups.views.send_invitation_email', side_effect=OSError('smtp is down')
    ):
      response = self.invite('newcomer@example.com')

    self.assertEqual(response.status_code, http.HTTP_201_CREATED)
    self.assertTrue(
      GroupInvitation.objects.filter(email='newcomer@example.com').exists()
    )
    self.assertTrue(
      GroupJoinRequest.objects.filter(
        group=self.group, to_email__iexact='newcomer@example.com'
      ).exists()
    )

  def test_the_token_is_read_only_so_only_the_server_mints_it(self):
    response = self.invite('newcomer@example.com', token='forged-token')

    invitation = GroupInvitation.objects.get(pk=response.data['data']['id'])
    self.assertNotEqual(invitation.token, 'forged-token')

  def test_a_registered_address_is_refused_with_a_pointer_to_join_requests(self):
    response = self.invite('member@example.com', expect=http.HTTP_411_LENGTH_REQUIRED)

    self.assertIn('email', response.data['errors'])
    self.assertFalse(
      GroupInvitation.objects.filter(email__iexact='member@example.com').exists()
    )

  def test_the_address_check_is_case_insensitive(self):
    self.invite('Newcomer@Example.COM')

    self.assertTrue(
      GroupInvitation.objects.filter(email='newcomer@example.com').exists()
    )

  def test_a_second_pending_invitation_to_the_same_address_is_refused(self):
    self.invite('newcomer@example.com')
    self.invite('newcomer@example.com', expect=http.HTTP_411_LENGTH_REQUIRED)

    self.assertEqual(GroupInvitation.objects.count(), 1)

  def test_an_expiry_in_the_past_is_refused(self):
    self.invite(
      'newcomer@example.com',
      expect=http.HTTP_411_LENGTH_REQUIRED,
      expires_at=get_unix_timestamp() - 60,
    )

  def test_members_may_invite_by_default(self):
    self.invite('newcomer@example.com')

    self.assertEqual(GroupInvitation.objects.count(), 1)

  def test_admins_can_close_invitations_off_for_plain_members(self):
    self.group.members_can_invite = False
    self.group.save(update_fields=['members_can_invite', 'updated_at'])

    self.invite('newcomer@example.com', expect=http.HTTP_403_FORBIDDEN)

    # The switch does not touch the admins.
    self.invite('newcomer@example.com', sender=self.admin)
    self.assertEqual(GroupInvitation.objects.count(), 1)

  def test_a_non_member_cannot_invite(self):
    stranger = make_user('stranger@example.com')
    self.invite(
      'newcomer@example.com', expect=http.HTTP_404_NOT_FOUND, sender=stranger
    )

    self.assertFalse(GroupInvitation.objects.exists())

  def test_the_inviter_list_is_scoped_to_the_group(self):
    self.invite('newcomer@example.com')

    self.as_user(self.admin)
    data = self.envelope_data(
      self.get('group-invitation-list-create', {'group_id': str(self.group.id)})
    )

    self.assertEqual(data['count'], 1)
    self.assertEqual(data['results'][0]['email'], 'newcomer@example.com')

  def test_an_inviter_can_revoke_their_pending_invitation(self):
    response = self.invite('newcomer@example.com')

    self.as_user(self.member)
    revoked = self.client.delete(
      reverse(
        'group-invitation-detail',
        kwargs={
          'group_id': str(self.group.id),
          'pk': response.data['data']['id'],
        },
      )
    )

    self.assertEqual(revoked.status_code, http.HTTP_204_NO_CONTENT)
    response_invitation = GroupInvitation.objects.get(
      pk=response.data['data']['id']
    )
    self.assertEqual(response_invitation.status, InvitationStatus.REVOKED)

  def test_revoking_an_invitation_withdraws_its_request(self):
    response = self.invite('newcomer@example.com')

    self.as_user(self.member)
    self.client.delete(
      reverse(
        'group-invitation-detail',
        kwargs={
          'group_id': str(self.group.id),
          'pk': response.data['data']['id'],
        },
      )
    )

    request = GroupJoinRequest.objects.get(
      group=self.group, to_email__iexact='newcomer@example.com'
    )
    self.assertEqual(request.status, JoinRequestStatus.CANCELLED)


class InvitationClaimTestCase(GroupApiTestCase):
  def setUp(self):
    super().setUp()
    # The newcomer does not exist yet - that is the precondition for an email
    # invitation at all. They are registered after the link is minted, standing
    # in for the signup that would really have happened in between.
    self.other = make_user('other@example.com')
    self.as_user(self.member)
    response = self.client.post(
      reverse(
        'group-invitation-list-create', kwargs={'group_id': str(self.group.id)}
      ),
      {'email': 'newcomer@example.com'},
      format='json',
    )
    self.assertEqual(response.status_code, http.HTTP_201_CREATED)
    self.invitation = GroupInvitation.objects.get(pk=response.data['data']['id'])

    from user.models import User

    self.newcomer = User.objects.create_user(
      email='newcomer@example.com', password='pw-for-tests-9'
    )

  def claim(self, expect=http.HTTP_201_CREATED, token=None, user=None):
    """201 on the first claim because a membership row is created; 200 if the
    caller was already in the group by some other route."""
    self.as_user(user or self.newcomer)
    response = self.client.post(
      reverse('group-invitation-accept'),
      {'token': str(token or self.invitation.token)},
      format='json',
    )
    self.assertEqual(
      response.status_code, expect, msg=f'{response.status_code} {response.data}'
    )
    return response

  def test_claiming_adds_the_newcomer_as_a_member(self):
    self.claim()

    self.invitation.refresh_from_db()
    self.assertEqual(self.invitation.status, InvitationStatus.ACCEPTED)
    self.assertEqual(self.invitation.accepted_by_id, self.newcomer.id)
    self.assertIsNotNone(self.invitation.responded_at)
    membership = GroupMembership.objects.get(
      group=self.group, user=self.newcomer
    )
    self.assertEqual(membership.role, 'member')

  def test_the_token_must_belong_to_the_callers_address(self):
    """Handing the link around is the one thing the token cannot stop, but
    claiming still has to be done by the address it was sent to."""
    self.claim(expect=http.HTTP_411_LENGTH_REQUIRED, user=self.other)

    self.invitation.refresh_from_db()
    self.assertEqual(self.invitation.status, InvitationStatus.PENDING)
    self.assertFalse(
      GroupMembership.objects.filter(group=self.group, user=self.other).exists()
    )

  def test_a_claimed_invitation_cannot_be_reused(self):
    self.claim()
    self.claim(expect=http.HTTP_411_LENGTH_REQUIRED)

    self.assertEqual(GroupMembership.objects.filter(group=self.group).count(), 4)

  def test_an_unknown_token_is_refused(self):
    self.claim(expect=http.HTTP_411_LENGTH_REQUIRED, token='11111111-1111-1111-1111-111111111111')

  def test_a_malformed_token_is_refused(self):
    self.as_user(self.newcomer)
    response = self.client.post(
      reverse('group-invitation-accept'), {'token': 'not-a-uuid'}, format='json'
    )

    self.assertEqual(response.status_code, http.HTTP_411_LENGTH_REQUIRED, msg=str(response.data))

  def test_an_expired_invitation_cannot_be_claimed(self):
    self.invitation.expires_at = get_unix_timestamp() - 1
    self.invitation.save(update_fields=['expires_at', 'updated_at'])

    self.claim(expect=http.HTTP_411_LENGTH_REQUIRED)

    self.invitation.refresh_from_db()
    self.assertEqual(self.invitation.status, InvitationStatus.EXPIRED)
    self.assertFalse(
      GroupMembership.objects.filter(group=self.group, user=self.newcomer).exists()
    )

  def test_a_revoked_invitation_cannot_be_claimed(self):
    self.invitation.status = InvitationStatus.REVOKED
    self.invitation.save(update_fields=['status', 'updated_at'])

    self.claim(expect=http.HTTP_411_LENGTH_REQUIRED)

    self.assertFalse(
      GroupMembership.objects.filter(group=self.group, user=self.newcomer).exists()
    )

  def test_an_expiry_in_the_past_is_resolved_on_read(self):
    """No background sweep job runs, so a lapsed link has to be caught when it is
    looked at - otherwise it keeps reading as usable until somebody tries it."""
    self.invitation.expires_at = get_unix_timestamp() - 1
    self.invitation.save(update_fields=['expires_at', 'updated_at'])

    self.as_user(self.member)
    self.get('group-invitation-list-create', {'group_id': str(self.group.id)})

    self.invitation.refresh_from_db()
    self.assertEqual(self.invitation.status, InvitationStatus.EXPIRED)

  def test_the_newcomers_own_inbox_lists_the_invitation(self):
    self.as_user(self.newcomer)
    data = self.envelope_data(self.get('group-my-invitations'))

    self.assertEqual(data['count'], 1)
    self.assertEqual(data['results'][0]['group_name'], 'Morning Crew')
    self.assertEqual(as_id(data['results'][0]['group']), str(self.group.id))


class SignupPromotionTestCase(APITestCase):
  """The invitation is minted before the address has an account, so it has to be
  carried across registration. What it becomes is a request, not a membership."""

  def setUp(self):
    self.inviter = make_user('inviter@example.com')
    self.group = make_group(self.inviter, name='Morning Crew')
    self.client.force_authenticate(self.inviter)
    response = self.client.post(
      reverse(
        'group-invitation-list-create', kwargs={'group_id': str(self.group.id)}
      ),
      {'email': 'newcomer@example.com'},
      format='json',
    )
    self.assertEqual(response.status_code, http.HTTP_201_CREATED)

  def sign_up(self, email='newcomer@example.com'):
    # `user/urls.py` registers this path without a name, so it is spelled out.
    return self.client.post(
      '/api/user/auth/sign-up/',
      {'email': email, 'password': 'pw-for-tests-9'},
      format='json',
    )

  def test_registering_converts_the_invitation_into_a_join_request(self):
    response = self.sign_up()

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    from user.models import User

    newcomer = User.objects.get(email='newcomer@example.com')
    join_request = GroupJoinRequest.objects.get(
      group=self.group, to_user=newcomer
    )
    self.assertEqual(join_request.from_user_id, self.inviter.id)
    self.assertEqual(join_request.status, JoinRequestStatus.PENDING)

  def test_registering_does_not_join_the_group(self):
    """The whole point of routing through a request. Signup must not enrol
    anybody on the strength of an email match alone."""
    self.sign_up()

    from user.models import User

    newcomer = User.objects.get(email='newcomer@example.com')
    self.assertFalse(
      GroupMembership.objects.filter(group=self.group, user=newcomer).exists()
    )

  def test_the_newcomer_still_has_to_accept(self):
    self.sign_up()

    from user.models import User

    newcomer = User.objects.get(email='newcomer@example.com')
    join_request = GroupJoinRequest.objects.get(
      group=self.group, to_user=newcomer
    )
    self.client.force_authenticate(newcomer)
    response = self.client.post(
      reverse(
        'group-join-request-accept',
        kwargs={
          'group_id': str(self.group.id),
          'pk': str(join_request.id),
        },
      )
    )

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    self.assertTrue(
      GroupMembership.objects.filter(group=self.group, user=newcomer).exists()
    )

  def test_the_invitation_is_still_claimable_after_promotion(self):
    """Promotion creates a request; it does not consume the link. The address can
    also take the faster route and claim it outright."""
    self.sign_up()
    invitation = GroupInvitation.objects.get(group=self.group)

    from user.models import User

    newcomer = User.objects.get(email='newcomer@example.com')
    self.client.force_authenticate(newcomer)
    response = self.client.post(
      reverse('group-invitation-accept'),
      {'token': str(invitation.token)},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_201_CREATED, msg=str(response.data))
    self.assertTrue(
      GroupMembership.objects.filter(group=self.group, user=newcomer).exists()
    )

  def test_claiming_the_link_twice_does_not_duplicate_membership(self):
    self.sign_up()
    invitation = GroupInvitation.objects.get(group=self.group)

    from user.models import User

    newcomer = User.objects.get(email='newcomer@example.com')
    self.client.force_authenticate(newcomer)
    self.client.post(
      reverse('group-invitation-accept'),
      {'token': str(invitation.token)},
      format='json',
    )

    self.assertEqual(
      GroupMembership.objects.filter(group=self.group, user=newcomer).count(), 1
    )

  def test_an_expired_invitation_is_not_promoted(self):
    invitation = GroupInvitation.objects.get(group=self.group)
    invitation.expires_at = get_unix_timestamp() - 1
    invitation.save(update_fields=['expires_at', 'updated_at'])

    self.sign_up()

    # The request written at send time lapses with the invitation: nothing is
    # left pending for the group and nothing was linked to the newcomer.
    self.assertFalse(
      GroupJoinRequest.objects.filter(
        group=self.group, status=JoinRequestStatus.PENDING
      ).exists()
    )
    self.assertFalse(GroupJoinRequest.objects.filter(to_user__isnull=False).exists())

  def test_signing_up_someone_else_is_unaffected(self):
    self.sign_up(email='someone-else@example.com')

    # The pending request stays on the invited address, unlinked. Signing up an
    # unrelated address must not claim somebody else's request.
    self.assertFalse(GroupJoinRequest.objects.filter(to_user__isnull=False).exists())
    self.assertTrue(
      GroupJoinRequest.objects.filter(
        to_email__iexact='newcomer@example.com',
        to_user__isnull=True,
        status=JoinRequestStatus.PENDING,
      ).exists()
    )

  def test_the_newcomer_finds_the_request_in_their_own_inbox(self):
    """The point of writing the request at send time: after registering, the
    invitee sees something waiting without needing the emailed link."""
    self.sign_up()

    from user.models import User

    newcomer = User.objects.get(email='newcomer@example.com')
    self.client.force_authenticate(newcomer)
    response = self.client.get(
      reverse('group-my-join-requests'), {'scope': 'incoming'}
    )

    payload = response.data['data']
    self.assertEqual(payload['count'], 1)
    self.assertEqual(as_id(payload['results'][0]['group']), str(self.group.id))
    self.assertEqual(payload['results'][0]['to_user_email'], 'newcomer@example.com')
    self.assertEqual(payload['results'][0]['status'], 'pending')
