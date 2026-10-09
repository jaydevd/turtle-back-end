"""API tests for join requests.

The direction is the point of this file: a member *asks* a registered user to
join, and that user alone decides. Every test below that asserts a 403 is
asserting the sender cannot accept on the recipient's behalf, or that a plain
member cannot pull anyone in once the admins have closed that off.
"""

from django.urls import reverse
from rest_framework import status as http
from rest_framework.test import APITestCase

from common.enums import GroupRole, JoinRequestStatus

from ..models import Group, GroupJoinRequest, GroupMembership
from .factories import join, make_group, make_user
from .test_groups_api import GroupApiTestCase, as_id


class JoinRequestTestCase(GroupApiTestCase):
  def send(
    self,
    recipient,
    expect=http.HTTP_201_CREATED,
    group=None,
    sender=None,
    **fields,
  ):
    """Address the request by email, the way the endpoint is written to be
    used - the recipient's account is resolved from the address server side."""
    group = group or self.group
    self.as_user(sender or self.member)
    response = self.client.post(
      reverse(
        'group-join-request-list-create', kwargs={'group_id': str(group.id)}
      ),
      {'email': recipient.email, **fields},
      format='json',
    )
    self.assertEqual(
      response.status_code, expect, msg=f'{response.status_code} {response.data}'
    )
    return response

  def latest(self, group=None):
    group = group or self.group
    return GroupJoinRequest.objects.filter(group=group).order_by('-created_at').first()


class JoinRequestSendingTestCase(JoinRequestTestCase):
  def test_a_member_can_request_that_a_registered_user_joins(self):
    response = self.send(self.outsider, message='Come and train with us.')

    self.assertEqual(response.data['status'], http.HTTP_201_CREATED)
    join_request = self.latest()
    self.assertEqual(join_request.from_user_id, self.member.id)
    self.assertEqual(join_request.to_user_id, self.outsider.id)
    self.assertEqual(join_request.status, JoinRequestStatus.PENDING)
    self.assertEqual(as_id(response.data['data']['to_user']), str(self.outsider.id))
    # The address is the write shape and never comes back; the id is the read one.
    self.assertNotIn('email', response.data['data'])
    self.assertEqual(response.data['data']['to_user_email'], self.outsider.email)

  def test_the_address_is_matched_case_insensitively(self):
    self.as_user(self.member)
    response = self.client.post(
      reverse(
        'group-join-request-list-create', kwargs={'group_id': str(self.group.id)}
      ),
      {'email': 'Outsider@Example.COM'},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_201_CREATED, msg=str(response.data))
    self.assertEqual(self.latest().to_user_id, self.outsider.id)

  def test_an_unregistered_address_is_refused_with_a_pointer_to_invitations(self):
    """The mirror image of the invitation rule, so neither channel can be used
    for the audience the other one exists for."""
    self.as_user(self.member)
    response = self.client.post(
      reverse(
        'group-join-request-list-create', kwargs={'group_id': str(self.group.id)}
      ),
      {'email': 'nobody@example.com'},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_411_LENGTH_REQUIRED, msg=str(response.data))
    self.assertIn('email', response.data['errors'])
    self.assertFalse(GroupJoinRequest.objects.exists())

  def test_the_status_and_the_sender_are_not_client_writable(self):
    """A request that arrives already ACCEPTED would enrol somebody without
    them ever answering."""
    self.send(self.outsider)

    join_request = self.latest()
    self.assertEqual(join_request.status, JoinRequestStatus.PENDING)
    self.assertEqual(join_request.from_user_id, self.member.id)

  def test_you_cannot_request_yourself(self):
    self.send(self.member, expect=http.HTTP_411_LENGTH_REQUIRED)

    self.assertFalse(GroupJoinRequest.objects.filter(group=self.group).exists())

  def test_an_existing_member_cannot_be_requested(self):
    self.send(self.admin, expect=http.HTTP_411_LENGTH_REQUIRED)

    self.assertIn('email', self.client.post(
      reverse('group-join-request-list-create', kwargs={'group_id': str(self.group.id)}),
      {'email': self.admin.email},
      format='json',
    ).data['errors'])

  def test_a_second_pending_request_is_refused(self):
    self.send(self.outsider)
    self.send(self.outsider, expect=http.HTTP_411_LENGTH_REQUIRED)

    self.assertEqual(GroupJoinRequest.objects.filter(group=self.group).count(), 1)

  def test_a_non_member_cannot_send_a_request(self):
    """The outsider is the *target* above, never the sender. Sending is a
    privilege of membership, so this asserts the sender side - and since the
    outsider has no request connecting them to this group, the group itself is
    not addressable for them at all."""
    self.send(self.admin, expect=http.HTTP_404_NOT_FOUND, sender=self.outsider)

    self.assertFalse(GroupJoinRequest.objects.filter(group=self.group).exists())

  def test_admins_can_close_requests_off_for_plain_members(self):
    """`members_can_invite` is the switch the story asks admins to have."""
    self.group.members_can_invite = False
    self.group.save(update_fields=['members_can_invite', 'updated_at'])

    self.send(self.outsider, expect=http.HTTP_403_FORBIDDEN)

    # The admins themselves are never gated by it.
    self.as_user(self.admin)
    response = self.client.post(
      reverse('group-join-request-list-create', kwargs={'group_id': str(self.group.id)}),
      {'email': self.outsider.email},
      format='json',
    )
    self.assertEqual(response.status_code, http.HTTP_201_CREATED, msg=str(response.data))

  def test_join_requests_enabled_off_blocks_plain_members(self):
    """The switch takes requests out of the members' hands, not the group's."""
    self.group.join_requests_enabled = False
    self.group.save(update_fields=['join_requests_enabled', 'updated_at'])

    self.send(self.outsider, expect=http.HTTP_403_FORBIDDEN)

    self.assertFalse(GroupJoinRequest.objects.exists())

  def test_admins_may_still_send_requests_when_the_switch_is_off(self):
    """Whoever flips the switch is not bound by it: it is a setting about what
    the members below them may do."""
    self.group.join_requests_enabled = False
    self.group.save(update_fields=['join_requests_enabled', 'updated_at'])
    second = make_user('second-outsider@example.com')

    for sender, recipient in ((self.admin, self.outsider), (self.owner, second)):
      self.as_user(sender)
      response = self.client.post(
        reverse('group-join-request-list-create', kwargs={'group_id': str(self.group.id)}),
        {'email': recipient.email},
        format='json',
      )
      self.assertEqual(response.status_code, http.HTTP_201_CREATED, msg=str(response.data))

    self.assertEqual(
      GroupJoinRequest.objects.filter(status=JoinRequestStatus.PENDING).count(), 2
    )


class JoinRequestResponseTestCase(JoinRequestTestCase):
  def setUp(self):
    super().setUp()
    self.send(self.outsider)
    self.join_request = self.latest()

  def detail_url(self, action=''):
    kwargs = {
      'group_id': str(self.group.id),
      'pk': str(self.join_request.id),
    }
    name = 'group-join-request-detail'
    if action:
      name = f'group-join-request-{action}'
    return reverse(name, kwargs=kwargs)

  def test_only_the_recipient_can_accept(self):
    self.as_user(self.member)
    response = self.client.post(self.detail_url('accept'))

    self.assertEqual(response.status_code, http.HTTP_403_FORBIDDEN, msg=str(response.data))
    self.assertFalse(GroupMembership.objects.filter(group=self.group, user=self.outsider).exists())

  def test_the_recipient_accepting_adds_them_to_the_group(self):
    self.as_user(self.outsider)
    response = self.client.post(self.detail_url('accept'))

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    self.join_request.refresh_from_db()
    self.assertEqual(self.join_request.status, JoinRequestStatus.ACCEPTED)
    self.assertIsNotNone(self.join_request.responded_at)
    membership = GroupMembership.objects.get(group=self.group, user=self.outsider)
    self.assertEqual(membership.role, GroupRole.MEMBER)

  def test_only_the_recipient_can_reject(self):
    self.as_user(self.member)
    response = self.client.post(self.detail_url('reject'))

    self.assertEqual(response.status_code, http.HTTP_403_FORBIDDEN, msg=str(response.data))
    self.join_request.refresh_from_db()
    self.assertEqual(self.join_request.status, JoinRequestStatus.PENDING)

  def test_the_recipient_can_reject(self):
    self.as_user(self.outsider)
    response = self.client.post(self.detail_url('reject'))

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    self.join_request.refresh_from_db()
    self.assertEqual(self.join_request.status, JoinRequestStatus.REJECTED)
    self.assertFalse(GroupMembership.objects.filter(group=self.group, user=self.outsider).exists())

  def test_answering_twice_is_a_411(self):
    self.as_user(self.outsider)
    self.client.post(self.detail_url('accept'))
    response = self.client.post(self.detail_url('accept'))

    self.assertEqual(response.status_code, http.HTTP_411_LENGTH_REQUIRED, msg=str(response.data))

  def test_the_sender_can_withdraw_their_own_request(self):
    self.as_user(self.member)
    response = self.client.delete(self.detail_url())

    self.assertEqual(response.status_code, http.HTTP_204_NO_CONTENT)
    self.join_request.refresh_from_db()
    self.assertEqual(self.join_request.status, JoinRequestStatus.CANCELLED)

  def test_another_member_cannot_withdraw_someone_elses_request(self):
    self.as_user(self.admin)
    response = self.client.delete(self.detail_url())

    self.assertEqual(response.status_code, http.HTTP_403_FORBIDDEN, msg=str(response.data))
    self.join_request.refresh_from_db()
    self.assertEqual(self.join_request.status, JoinRequestStatus.PENDING)

  def test_a_stranger_cannot_even_see_the_request(self):
    stranger = make_user('stranger@example.com')
    self.as_user(stranger)

    self.get('group-join-request-list-create', {'group_id': str(self.group.id)}, expect=http.HTTP_404_NOT_FOUND)
    response = self.client.post(self.detail_url('accept'))
    self.assertEqual(response.status_code, http.HTTP_404_NOT_FOUND, msg=str(response.data))


class JoinRequestListingTestCase(JoinRequestTestCase):
  def test_admins_see_the_whole_request_log(self):
    self.send(self.outsider)

    self.as_user(self.admin)
    data = self.envelope_data(
      self.get(
        'group-join-request-list-create', {'group_id': str(self.group.id)}
      )
    )

    self.assertEqual(data['count'], 1)
    self.assertEqual(
      as_id(data['results'][0]['to_user']), str(self.outsider.id)
    )

  def test_a_plain_member_only_sees_their_own(self):
    other = make_user('another-sender@example.com')
    join(self.group, other)
    self.as_user(other)
    self.client.post(
      reverse('group-join-request-list-create', kwargs={'group_id': str(self.group.id)}),
      {'email': self.outsider.email},
      format='json',
    )

    self.as_user(self.member)
    data = self.envelope_data(
      self.get(
        'group-join-request-list-create', {'group_id': str(self.group.id)}
      )
    )

    self.assertEqual(data['count'], 0)

  def test_the_recipient_can_filter_to_incoming(self):
    self.send(self.outsider)

    self.as_user(self.outsider)
    data = self.envelope_data(
      self.get(
        'group-join-request-list-create',
        {'group_id': str(self.group.id)},
        scope='incoming',
      )
    )

    self.assertEqual(data['count'], 1)


class JoinRequestInboxTestCase(JoinRequestTestCase):
  """The recipient's own inbox.

  A recipient is not in `visible_groups`, so they cannot reach the group-scoped
  route without already knowing the group id. This is the route that does not
  need one, and without it a request addressed to somebody is invisible to them
  until they are handed a link by hand.
  """

  def test_the_recipient_sees_the_request_without_a_group_id(self):
    self.send(self.outsider, message='Come and train with us.')

    self.as_user(self.outsider)
    data = self.envelope_data(self.get('group-my-join-requests', scope='incoming'))

    self.assertEqual(data['count'], 1)
    row = data['results'][0]
    self.assertEqual(as_id(row['group']), str(self.group.id))
    self.assertEqual(row['group_name'], 'Morning Crew')
    self.assertEqual(row['from_user_email'], self.member.email)
    self.assertEqual(row['status'], JoinRequestStatus.PENDING)

  def test_the_sender_can_follow_their_own_request_from_the_same_route(self):
    self.send(self.outsider)

    self.as_user(self.member)
    data = self.envelope_data(self.get('group-my-join-requests', scope='outgoing'))

    self.assertEqual(data['count'], 1)
    self.assertEqual(as_id(data['results'][0]['to_user']), str(self.outsider.id))

  def test_the_inbox_only_returns_rows_that_name_the_caller(self):
    self.send(self.outsider)
    stranger = make_user('stranger@example.com')

    self.as_user(stranger)
    data = self.envelope_data(self.get('group-my-join-requests'))

    self.assertEqual(data['count'], 0)

  def test_the_inbox_never_leaks_a_request_from_another_pair(self):
    """Two people can be talking about the same group without either of them
    being party to the other's conversation."""
    other = make_user('another-sender@example.com')
    join(self.group, other)
    self.send(self.outsider, sender=other)

    self.as_user(self.member)
    data = self.envelope_data(self.get('group-my-join-requests'))

    self.assertEqual(data['count'], 0)

  def test_the_ids_in_the_payload_are_enough_to_answer_the_request(self):
    """The recipient's whole path is: read the inbox, then post to the
    group-scoped accept route with the two ids it just handed over."""
    self.send(self.outsider)

    self.as_user(self.outsider)
    data = self.envelope_data(self.get('group-my-join-requests', scope='incoming'))
    row = data['results'][0]

    response = self.client.post(
      reverse(
        'group-join-request-accept',
        kwargs={'group_id': row['group'], 'pk': row['id']},
      )
    )

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    self.assertTrue(
      GroupMembership.objects.filter(group=self.group, user=self.outsider).exists()
    )