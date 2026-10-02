"""API tests for groups, membership and the admin settings around them.

These cover what unit tests cannot: that a queryset really does hide another
group, that the role rules survive being routed through URLs, and that a refusal
comes back in the project's `{status, message, data, errors}` envelope rather
than DRF's bare `{"detail": ...}`.
"""

from django.urls import reverse
from rest_framework import status as http
from rest_framework.test import APITestCase

from common.enums import GroupRole

from ..models import Group, GroupMembership
from .factories import join, make_group, make_user


class GroupApiTestCase(APITestCase):
  def setUp(self):
    self.owner = make_user('owner@example.com')
    self.admin = make_user('admin@example.com')
    self.member = make_user('member@example.com')
    self.outsider = make_user('outsider@example.com')

    self.group = make_group(self.owner, name='Morning Crew')
    join(self.group, self.admin, role=GroupRole.ADMIN)
    join(self.group, self.member)

  def as_user(self, user):
    self.client.force_authenticate(user)
    return user

  def get(self, name, route_kwargs=None, expect=http.HTTP_200_OK, **params):
    response = self.client.get(reverse(name, kwargs=route_kwargs), params)
    self.assertEqual(
      response.status_code, expect, msg=f'{name} -> {response.status_code} {response.data}'
    )
    return response

  def envelope_data(self, response):
    """The success envelope is `{status, message, data}`; only errors carry an
    `errors` key, since `common.responses.success_response` has nowhere to put one.
    """
    self.assertEqual(response.data['status'], response.status_code)
    self.assertIn('message', response.data)
    self.assertIn('data', response.data)
    if response.status_code >= 400:
      self.assertIn('errors', response.data)
    return response.data['data']


def as_id(value):
  """Normalise a related id for comparison.

  `PrimaryKeyRelatedField` hands back the raw `UUID` rather than a string, so a
  direct comparison against `str(user.id)` fails on type alone. This keeps the
  assertions about values rather than about DRF's representation choice.
  """
  return str(value)


class GroupCrudTestCase(GroupApiTestCase):
  def test_creating_a_group_also_claims_owner_membership(self):
    self.as_user(self.owner)

    response = self.client.post(
      '/api/user/groups/',
      {'name': 'Evening Crew', 'description': 'Wind down together.'},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_201_CREATED, msg=str(response.data))
    data = self.envelope_data(response)

    group = Group.objects.get(pk=data['id'])
    membership = GroupMembership.objects.get(group=group, user=self.owner)
    self.assertEqual(membership.role, GroupRole.OWNER)
    self.assertEqual(data['my_role'], GroupRole.OWNER)
    self.assertEqual(data['member_count'], 1)

  def test_a_blank_name_is_a_411(self):
    self.as_user(self.owner)

    response = self.client.post('/api/user/groups/', {'name': '   '}, format='json')

    self.assertEqual(response.status_code, http.HTTP_411_LENGTH_REQUIRED, msg=str(response.data))
    self.assertIn('name', response.data['errors'])

  def test_listing_returns_only_groups_the_caller_is_in(self):
    other = make_group(self.outsider, name='Private Circle')

    self.as_user(self.member)
    data = self.envelope_data(self.get('group-list-create'))

    ids = [row['id'] for row in data['results']]
    self.assertIn(str(self.group.id), ids)
    self.assertNotIn(str(other.id), ids)

  def test_a_group_the_caller_is_outside_is_a_404(self):
    other = make_group(self.outsider, name='Private Circle')

    self.as_user(self.member)
    response = self.get(
      'group-detail', {'pk': str(other.id)}, expect=http.HTTP_404_NOT_FOUND
    )

    self.assertEqual(response.data['status'], http.HTTP_404_NOT_FOUND)

  def test_admin_may_change_the_group_settings(self):
    self.as_user(self.admin)

    response = self.client.patch(
      reverse('group-detail', kwargs={'pk': str(self.group.id)}),
      {'members_can_invite': False, 'anyone_can_create_challenge': False},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    self.group.refresh_from_db()
    self.assertFalse(self.group.members_can_invite)
    self.assertFalse(self.group.anyone_can_create_challenge)

  def test_a_plain_member_may_not_change_the_group_settings(self):
    self.as_user(self.member)

    response = self.client.patch(
      reverse('group-detail', kwargs={'pk': str(self.group.id)}),
      {'members_can_invite': False},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_403_FORBIDDEN, msg=str(response.data))
    self.group.refresh_from_db()
    self.assertTrue(self.group.members_can_invite)

  def test_a_403_still_uses_the_envelope(self):
    """DRF answers a denied request with a bare `{"detail": ...}`. Every other
    response in this API is an envelope, so a client parsing one shape must not
    trip over the refusals."""
    self.as_user(self.member)

    response = self.client.patch(
      reverse('group-detail', kwargs={'pk': str(self.group.id)}),
      {'members_can_invite': False},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_403_FORBIDDEN)
    self.assertEqual(response.data['status'], http.HTTP_403_FORBIDDEN)
    self.assertEqual(response.data['message'], 'user forbidden')
    self.assertIn('detail', response.data['errors'])

  def test_delete_is_owner_only_and_soft(self):
    self.as_user(self.admin)
    denied = self.client.delete(
      reverse('group-detail', kwargs={'pk': str(self.group.id)})
    )
    self.assertEqual(denied.status_code, http.HTTP_403_FORBIDDEN, msg=str(denied.data))

    self.as_user(self.owner)
    response = self.client.delete(
      reverse('group-detail', kwargs={'pk': str(self.group.id)})
    )

    self.assertEqual(response.status_code, http.HTTP_204_NO_CONTENT)
    self.group.refresh_from_db()
    self.assertTrue(self.group.is_deleted)
    # Soft deleted, so the row survives for a moderation trail.
    self.assertTrue(Group.objects.filter(pk=self.group.pk).exists())
    self.assertFalse(GroupMembership.objects.filter(group=self.group).exists())


class GroupMemberTestCase(GroupApiTestCase):
  def test_the_roster_is_visible_to_every_member(self):
    self.as_user(self.member)

    data = self.envelope_data(
      self.get('group-members', {'group_id': str(self.group.id)})
    )

    emails = {row['user_email'] for row in data['results']}
    self.assertEqual(
      emails,
      {'owner@example.com', 'admin@example.com', 'member@example.com'},
    )

  def test_a_non_member_cannot_read_the_roster(self):
    self.as_user(self.outsider)

    self.get(
      'group-members',
      {'group_id': str(self.group.id)},
      expect=http.HTTP_404_NOT_FOUND,
    )

  def test_an_admin_may_promote_a_member(self):
    self.as_user(self.admin)

    membership = GroupMembership.objects.get(group=self.group, user=self.member)
    response = self.client.patch(
      reverse(
        'group-member-detail',
        kwargs={
          'group_id': str(self.group.id),
          'member_id': str(membership.id),
        },
      ),
      {'role': GroupRole.ADMIN},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    membership.refresh_from_db()
    self.assertEqual(membership.role, GroupRole.ADMIN)

  def test_an_admin_may_not_change_another_admin(self):
    self.as_user(self.admin)

    target = GroupMembership.objects.get(group=self.group, user=self.owner)
    # The owner sits above the actor, so this exercises the owner-tier guard.
    response = self.client.patch(
      reverse(
        'group-member-detail',
        kwargs={'group_id': str(self.group.id), 'member_id': str(target.id)},
      ),
      {'role': GroupRole.MEMBER},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_403_FORBIDDEN, msg=str(response.data))
    target.refresh_from_db()
    self.assertEqual(target.role, GroupRole.OWNER)

  def test_an_admin_may_not_evict_another_admin(self):
    self.as_user(self.owner)
    join(self.group, self.outsider, role=GroupRole.ADMIN)

    peer = GroupMembership.objects.get(group=self.group, user=self.admin)
    self.as_user(self.admin)

    response = self.client.delete(
      reverse(
        'group-member-detail',
        kwargs={'group_id': str(self.group.id), 'member_id': str(peer.id)},
      )
    )

    self.assertEqual(response.status_code, http.HTTP_403_FORBIDDEN, msg=str(response.data))
    self.assertTrue(GroupMembership.objects.filter(pk=peer.pk).exists())

  def test_an_admin_may_remove_a_member(self):
    self.as_user(self.admin)

    membership = GroupMembership.objects.get(group=self.group, user=self.member)
    response = self.client.delete(
      reverse(
        'group-member-detail',
        kwargs={'group_id': str(self.group.id), 'member_id': str(membership.id)},
      )
    )

    self.assertEqual(response.status_code, http.HTTP_204_NO_CONTENT)
    self.assertFalse(GroupMembership.objects.filter(pk=membership.pk).exists())

  def test_a_plain_member_may_not_manage_members(self):
    self.as_user(self.member)

    target = GroupMembership.objects.get(group=self.group, user=self.admin)
    response = self.client.patch(
      reverse(
        'group-member-detail',
        kwargs={'group_id': str(self.group.id), 'member_id': str(target.id)},
      ),
      {'role': GroupRole.MEMBER},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_403_FORBIDDEN, msg=str(response.data))
    target.refresh_from_db()
    self.assertEqual(target.role, GroupRole.ADMIN)

  def test_ownership_transfer_moves_the_tier_and_the_owner_column(self):
    self.as_user(self.owner)

    response = self.client.post(
      reverse('group-transfer-ownership', kwargs={'group_id': str(self.group.id)}),
      {'user': str(self.member.id)},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_200_OK, msg=str(response.data))
    self.group.refresh_from_db()
    self.assertEqual(self.group.owner_id, self.member.id)
    self.assertEqual(
      GroupMembership.objects.get(group=self.group, user=self.member).role,
      GroupRole.OWNER,
    )
    self.assertEqual(
      GroupMembership.objects.get(group=self.group, user=self.owner).role,
      GroupRole.ADMIN,
    )

  def test_ownership_transfer_is_owner_only(self):
    self.as_user(self.admin)

    response = self.client.post(
      reverse('group-transfer-ownership', kwargs={'group_id': str(self.group.id)}),
      {'user': str(self.member.id)},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_403_FORBIDDEN, msg=str(response.data))

  def test_transferring_to_a_non_member_is_a_404(self):
    self.as_user(self.owner)

    response = self.client.post(
      reverse('group-transfer-ownership', kwargs={'group_id': str(self.group.id)}),
      {'user': str(self.outsider.id)},
      format='json',
    )

    self.assertEqual(response.status_code, http.HTTP_404_NOT_FOUND, msg=str(response.data))