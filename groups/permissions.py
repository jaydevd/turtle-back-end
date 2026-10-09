"""Object-level permissions for group scoped resources.

Every class resolves the group off the object it is guarding - `obj.group` for a
habit or a membership, `obj` itself for a group - and then asks `groups.services`
for the answer. The rules live in one place so a permission class and the view
that also enforces them cannot drift apart.

Non-membership reads as a 404, not a 403. Every queryset in this app is scoped
to the caller's own groups, so a group the caller cannot see is already absent
from the result set; a 403 would confirm that the id exists.
"""

from rest_framework import permissions

from . import services


class IsGroupMember(permissions.BasePermission):
  """May read anything belonging to a group the caller belongs to."""

  message = 'You are not a member of this group.'

  def has_object_permission(self, request, view, obj):
    return services.is_member(request.user, _group_of(obj))


class IsGroupAdminOrOwner(permissions.BasePermission):
  """May administer a group: settings, members, requests and invitations."""

  message = 'Only group admins can do this.'

  def has_object_permission(self, request, view, obj):
    return services.is_admin(request.user, _group_of(obj))


class IsGroupOwner(permissions.BasePermission):
  """Owner-only operations, currently transferring ownership."""

  message = 'Only the group owner can do this.'

  def has_object_permission(self, request, view, obj):
    return services.is_owner(request.user, _group_of(obj))


class CanInvite(permissions.BasePermission):
  """May pull a registered user in by request.

  Both switches - `join_requests_enabled` and `members_can_invite` - apply to
  members below the admin tier. Owners and admins are subject to neither.
  """

  message = 'You cannot send join requests for this group.'

  def has_object_permission(self, request, view, obj):
    return services.can_send_join_request(request.user, _group_of(obj))


class CanSendInvitation(permissions.BasePermission):
  """May invite an unregistered address by email."""

  message = 'You cannot invite people to this group.'

  def has_object_permission(self, request, view, obj):
    return services.can_invite(request.user, _group_of(obj))


class CanCreateChallenge(permissions.BasePermission):
  """May propose a challenge in the group.

  Honours `anyone_can_create_challenge`, so a group that wants challenges to come
  from its admins alone sets it to False and every non-admin is refused.
  """

  message = 'Only group admins can create challenges in this group.'

  def has_object_permission(self, request, view, obj):
    return services.can_create_challenge(request.user, _group_of(obj))


class CanManageChallenge(permissions.BasePermission):
  """May start, end or cancel a challenge.

  The author manages their own challenge; group admins can stand in, which is
  what lets an admin shut down a challenge the author abandoned.
  """

  message = 'Only the challenge author or a group admin can do this.'

  def has_object_permission(self, request, view, obj):
    from habits import challenges

    return challenges.can_manage_challenge(request.user, obj)


def _group_of(obj):
  """The group guarding `obj`, whether obj is the group or lives under one."""
  return getattr(obj, 'group', None) or obj