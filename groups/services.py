"""Group membership rules, shared by the permissions, the views and the tests.

Role capability lives here rather than in each permission class so the API's
answer to "may this user do X?" is computed once. A permission class that
re-derived the rule could drift from the view that also has to enforce it.

Two group settings decide who may pull someone in, and they are deliberately
orthogonal:

* `join_requests_enabled` is the members' side of requests aimed at an
  already-registered user. With it off, members cannot ask anybody and only the
  admins remain able to - the group has taken requests out of the members'
  hands rather than out of its own. It is never a switch on the admins.
* `members_can_invite` decides whether members below the admin tier may initiate
  either channel. Owners and admins are never gated by it.

A member of a group who is not an admin therefore has to be granted both of the
two settings before they can bring anyone in, which is the behaviour the story
asks for: admins decide whether anyone else can.
"""

from django.db.models import Count, IntegerField, OuterRef, Q, Subquery

from common.enums import GroupRole, InvitationStatus, JoinRequestStatus
from common.utils import get_unix_timestamp

from .models import Group, GroupInvitation, GroupJoinRequest, GroupMembership

DEFAULT_INVITATION_TTL_DAYS = 7


def membership_of(user, group):
  """The caller's `GroupMembership` row, or None. One query, cached per pair."""
  if user is None or not getattr(user, 'is_authenticated', False):
    return None
  if group is None:
    return None
  return GroupMembership.objects.filter(group=group, user=user).first()


def role_of(user, group):
  membership = membership_of(user, group)
  return membership.role if membership is not None else None


def is_member(user, group):
  return role_of(user, group) is not None


def is_admin(user, group):
  return role_of(user, group) in (GroupRole.OWNER, GroupRole.ADMIN)


def is_owner(user, group):
  return role_of(user, group) == GroupRole.OWNER


def can_invite(user, group):
  """Whether `user` may initiate a join request or an email invitation."""
  role = role_of(user, group)
  if role in (GroupRole.OWNER, GroupRole.ADMIN):
    return True
  if role is None:
    return False
  return group.members_can_invite


def can_send_join_request(user, group):
  """Whether `user` may request that a registered user join `group`.

  `join_requests_enabled` gates the members and only the members: an admin
  turning it off is saying "nobody below me pulls people in", not "this group
  takes no requests". The admins who set it keep the ability for themselves,
  which is what separates this from the invite-only reading of the switch.
  """
  role = role_of(user, group)
  if role in (GroupRole.OWNER, GroupRole.ADMIN):
    return True
  if role is None:
    return False
  if not group.join_requests_enabled:
    return False
  return group.members_can_invite


def can_create_challenge(user, group):
  """Whether `user` may propose a challenge in `group`."""
  role = role_of(user, group)
  if role in (GroupRole.OWNER, GroupRole.ADMIN):
    return True
  if role is None:
    return False
  return group.anyone_can_create_challenge


def can_manage_roles(user, group):
  """Whether `user` may promote or demote a member."""
  return is_admin(user, group)


# ---- Membership writes ----


def add_member(group, user, role=GroupRole.MEMBER, joined_at=None):
  """Idempotently add `user` to `group`.

  Returns `(membership, created)`. Re-adding an existing member is a no-op rather
  than an error, so accepting a second invitation does not fail on a race the
  caller could do nothing about.
  """
  membership, created = GroupMembership.objects.get_or_create(
    group=group,
    user=user,
    defaults={
      'role': role,
      'joined_at': joined_at if joined_at is not None else get_unix_timestamp(),
    },
  )
  return membership, created


# ---- Join requests ----


def cancel_expired_invitations(group=None):
  """Move `PENDING` invitations past their `expires_at` to `EXPIRED`.

  Expiry is resolved on read rather than by a job, so an invitation that was
  never picked up stops looking valid the moment it lapses. Returns the number
  of rows changed.
  """
  now = get_unix_timestamp()
  queryset = GroupInvitation.objects.filter(
    status=InvitationStatus.PENDING,
    expires_at__lte=now,
  )
  if group is not None:
    queryset = queryset.filter(group=group)

  return queryset.update(status=InvitationStatus.EXPIRED, updated_at=now)


def expire_invitation(invitation, now=None):
  """Flip one invitation to EXPIRED if its deadline has passed.

  Returns True when this call expired it. An invitation without a deadline never
  expires on its own.
  """
  if invitation.status != InvitationStatus.PENDING or invitation.expires_at is None:
    return False

  if (now or get_unix_timestamp()) <= invitation.expires_at:
    return False

  invitation.status = InvitationStatus.EXPIRED
  invitation.responded_at = get_unix_timestamp()
  invitation.save(update_fields=['status', 'responded_at', 'updated_at'])
  return True


def invitation_default_expiry():
  return get_unix_timestamp() + DEFAULT_INVITATION_TTL_DAYS * 86_400


def accept_join_request(join_request):
  """PENDING to ACCEPTED, adding the recipient to the group.

  The recipient is the only party who can accept. Whoever sent the request has no
  say over it - a member pulling a colleague in is asking, not admitting.
  """
  if join_request.status != JoinRequestStatus.PENDING:
    raise ValueError('This request has already been answered.')

  now = get_unix_timestamp()
  add_member(join_request.group, join_request.to_user, joined_at=now)

  join_request.status = JoinRequestStatus.ACCEPTED
  join_request.responded_at = now
  join_request.save(update_fields=['status', 'responded_at', 'updated_at'])

  # The invitation that led here, if any, is now spent. An invitation is only
  # ever created for an unregistered address, so a promoted invitation is the
  # one and only place a request can arrive from outside the platform.
  GroupInvitation.objects.filter(
    group=join_request.group,
    invited_by=join_request.from_user,
    email=join_request.to_user.email,
    status=InvitationStatus.PENDING,
  ).update(
    status=InvitationStatus.ACCEPTED,
    accepted_by=join_request.to_user,
    responded_at=now,
    updated_at=now,
  )

  return join_request


def reject_join_request(join_request):
  if join_request.status != JoinRequestStatus.PENDING:
    raise ValueError('This request has already been answered.')

  join_request.status = JoinRequestStatus.REJECTED
  join_request.responded_at = get_unix_timestamp()
  join_request.save(update_fields=['status', 'responded_at', 'updated_at'])
  return join_request


def cancel_join_request(join_request):
  if join_request.status != JoinRequestStatus.PENDING:
    raise ValueError('This request has already been answered.')

  join_request.status = JoinRequestStatus.CANCELLED
  join_request.responded_at = get_unix_timestamp()
  join_request.save(update_fields=['status', 'responded_at', 'updated_at'])
  return join_request


def promote_invitations_for(user):
  """Turn `PENDING` invitations addressed to `user` into join requests.

  Called once, at registration. An invitation is only ever created for an address
  with no account, so its arrival at this function means the address has just
  become a platform user and the request it stood in for can finally be made -
  the point at which the story says the request is raised.

  Nothing is joined here. The newcomer gets a PENDING request like anybody else
  and has to accept it, so the invitation never auto-enrols anyone.
  """
  now = get_unix_timestamp()
  invitations = list(
    GroupInvitation.objects.select_related('group', 'invited_by').filter(
      email__iexact=user.email,
      status=InvitationStatus.PENDING,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
  )

  created = []
  for invitation in invitations:
    if GroupMembership.objects.filter(
      group=invitation.group, user=user
    ).exists():
      continue
    if GroupJoinRequest.objects.filter(
      group=invitation.group,
      from_user=invitation.invited_by,
      to_user=user,
      status=JoinRequestStatus.PENDING,
    ).exists():
      continue

    created.append(
      GroupJoinRequest.objects.create(
        group=invitation.group,
        from_user=invitation.invited_by,
        to_user=user,
        message=f'{invitation.invited_by.email} invited you to join this group.',
      )
    )

  return created


def visible_groups(user):
  """Groups the caller owns or belongs to, soft-deleted ones excluded.

  A group creator is always given an `OWNER` membership row, so the owner branch
  is belt and braces for rows written before that invariant held.
  """
  if user is None or not getattr(user, 'is_authenticated', False):
    return Group.objects.none()

  return Group.objects.filter(is_deleted=False).filter(
    Q(owner=user) | Q(memberships__user=user)
  ).distinct()


def with_member_count(queryset):
  """Annotate the roster size onto a `visible_groups` queryset.

  Deliberately a subquery rather than `Count('memberships')`. `visible_groups`
  filters through `memberships`, and an aggregate on the same join counts only
  the rows surviving that filter - which is exactly one per group, the caller's
  own row - so every group would report the same size no matter how many members
  it has. A subquery counts the table instead.
  """
  counts = (
    GroupMembership.objects.filter(group=OuterRef('pk'))
    .values('group')
    .annotate(total=Count('id'))
    .values('total')[:1]
  )
  return queryset.annotate(
    member_count=Subquery(counts, output_field=IntegerField())
  )