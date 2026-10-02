"""Group endpoints: groups, members, join requests, invitations and challenges.

Every queryset here is scoped to the caller - their own groups, their own
membership, their own inbox - so a group they cannot see never reaches the
permission layer and reads as a 404 rather than a 403. The permissions in
`groups.permissions` then narrow the same objects further for the actions that
need an admin.
"""

from django.db import transaction
from django.db.models import Q
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated

from common.constants import HTTP_ERROR_CODES, RESPONSE_MESSAGES
from common.enums import ChallengeStatus, GroupRole, InvitationStatus, JoinRequestStatus
from common.pagination import StandardPagination
from common.responses import error_response, success_response
from common.utils import get_unix_timestamp

from habits import challenges as habit_challenges
from habits.models import Habit
from habits.serializers import HabitSerializer
from habits.views import EnvelopeListMixin

from . import services
from .models import Group, GroupInvitation, GroupJoinRequest, GroupMembership
from .permissions import (
  CanCreateChallenge,
  CanInvite,
  CanSendInvitation,
  IsGroupAdminOrOwner,
  IsGroupMember,
  IsGroupOwner,
)
from .serializers import (
  GroupInvitationSerializer,
  GroupJoinRequestSerializer,
  GroupMembershipSerializer,
  GroupSerializer,
  InvitationAcceptSerializer,
)


def forbidden(message):
  return error_response(
    status_code=HTTP_ERROR_CODES['FORBIDDEN'],
    message=RESPONSE_MESSAGES['FORBIDDEN'],
    errors={'detail': message},
  )


def detail_error(code, message, detail):
  """The envelope shape for an error carrying one sentence rather than a field map."""
  return error_response(status_code=code, message=message, errors={'detail': detail})


def require(request, obj, *permission_classes):
  """Enforce object permissions from inside a view.

  `@action(permission_classes=...)` looks like it does this and does not: DRF
  keeps those kwargs on the action for `reverse` and never applies them while
  dispatching. Passing them to `as_view` does work, but binds them to the whole
  class rather than to one action, so the check is spelled out here instead.
  """
  for permission_class in permission_classes:
    permission = permission_class()
    if not permission.has_object_permission(request, None, obj):
      raise PermissionDenied(permission.message)


def paginated(view, queryset, serializer_class):
  """The `{count, next, previous, results}` block, for endpoints whose payload is
  not the viewset's own serializer."""
  page = view.paginate_queryset(queryset)
  paginator = view.paginator
  return {
    'count': paginator.page.paginator.count,
    'next': paginator.get_next_link(),
    'previous': paginator.get_previous_link(),
    'results': serializer_class(page, many=True).data,
  }


def single_field_error(field, message):
  return serializers.ValidationError({field: message}).detail


class GroupEnvelopeMixin(EnvelopeListMixin):
  """Envelope responses for denied requests too.

  DRF's default handler answers a `PermissionDenied` with a bare `{"detail": ...}`,
  which breaks the `{status, message, data, errors}` shape every other response
  in this API uses. 404 is already covered by `EnvelopeListMixin`.
  """

  def handle_exception(self, exc):
    if isinstance(exc, PermissionDenied):
      return forbidden(str(exc.detail))
    return super().handle_exception(exc)


class GroupViewSet(GroupEnvelopeMixin, viewsets.ModelViewSet):
  """`/api/user/groups/` - the groups the caller owns or belongs to.

  Creating a group also writes the owner's `OWNER` membership row. Both happen in
  one transaction: a group whose owner is not a member is a group nobody but the
  owner can administer, and that half-written pair is exactly what a caller would
  be left holding if the second insert failed.
  """

  serializer_class = GroupSerializer
  permission_classes = [IsAuthenticated]
  pagination_class = StandardPagination
  not_found_message = 'Group not found.'

  def get_queryset(self):
    return services.with_member_count(
      services.visible_groups(self.request.user).select_related('owner')
    )

  def get_object(self):
    """404 for a group the caller cannot see, so ids are not enumerable.

    The id arrives as `pk` on the detail route and `group_id` on the nested ones,
    so both spellings are accepted rather than needing a second accessor.
    """
    group_id = self.kwargs.get('pk') or self.kwargs.get('group_id')
    try:
      group = self.get_queryset().get(pk=group_id)
    except (Group.DoesNotExist, ValueError):
      raise NotFound('Group not found.')
    require(self.request, group, IsGroupMember)
    return group

  def create(self, request, *args, **kwargs):
    serializer = self.get_serializer(data=request.data)
    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    with transaction.atomic():
      group = serializer.save(owner=request.user)
      services.add_member(group, request.user, role=GroupRole.OWNER)

    payload = self.get_serializer(self.get_queryset().get(pk=group.pk)).data
    return success_response(
      status_code=HTTP_ERROR_CODES['CREATED'],
      message=RESPONSE_MESSAGES['CREATED'],
      data=payload,
    )

  def update(self, request, *args, **kwargs):
    return self._write(request, partial=False, *args, **kwargs)

  def partial_update(self, request, *args, **kwargs):
    return self._write(request, partial=True, *args, **kwargs)

  def _write(self, request, partial, *args, **kwargs):
    instance = self.get_object()
    require(request, instance, IsGroupAdminOrOwner)

    serializer = self.get_serializer(instance, data=request.data, partial=partial)
    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    serializer.save()
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['DATA_UPDATED'],
      data=serializer.data,
    )

  def destroy(self, request, *args, **kwargs):
    """Soft delete.

    Memberships go, the group row stays. Nothing is cascaded away: the row and its
    history are what a moderation trail would need, and soft-deleting the habits
    inside it already takes the group out of every reachable queryset. Running
    challenges are cancelled rather than left ACTIVE over a group nobody can open.
    """
    group = self.get_object()
    require(request, group, IsGroupOwner)

    group.is_deleted = True
    group.save(update_fields=['is_deleted', 'updated_at'])

    GroupMembership.objects.filter(group=group).delete()
    Habit.objects.filter(group=group, is_deleted=False).exclude(
      challenge_status=ChallengeStatus.CANCELLED
    ).update(challenge_status=ChallengeStatus.CANCELLED)

    return success_response(
      status_code=HTTP_ERROR_CODES['200_NO_CONTENT'],
      message=RESPONSE_MESSAGES['DATA_DELETED'],
    )

  @action(detail=True, methods=['get'])
  def members(self, request, *args, **kwargs):
    """The roster, visible to every member. A group is small and its membership
    is not private to the people who run it."""
    group = self.get_object()
    queryset = (
      GroupMembership.objects.filter(group=group)
      .select_related('user')
      .order_by('-joined_at')
    )
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data=paginated(self, queryset, GroupMembershipSerializer),
    )

  @action(
    detail=True,
    methods=['patch', 'delete'],
    url_path=r'members/(?P<member_id>[0-9a-fA-F-]+)',
  )
  def member(self, request, member_id=None, *args, **kwargs):
    """Change a member's role, or remove them.

    Admins may only move people between `ADMIN` and `MEMBER`: the owner tier
    belongs to the owner, and an admin cannot demote a peer or evict them. The
    owner may set any role except their own, which keeps a group from being left
    ownerless by a well-meaning promotion.
    """
    group = self.get_object()
    require(request, group, IsGroupAdminOrOwner)
    actor_role = services.role_of(request.user, group)

    membership = (
      GroupMembership.objects.filter(group=group, id=member_id)
      .select_related('user')
      .first()
    )
    if membership is None:
      raise NotFound('Member not found.')
    if membership.role == GroupRole.OWNER:
      return forbidden('The group owner cannot be modified.')

    if request.method == 'DELETE':
      if actor_role == GroupRole.ADMIN and membership.role == GroupRole.ADMIN:
        return forbidden('Only the group owner can remove another admin.')
      membership.delete()
      return success_response(
        status_code=HTTP_ERROR_CODES['200_NO_CONTENT'],
        message=RESPONSE_MESSAGES['DATA_DELETED'],
      )

    if actor_role == GroupRole.ADMIN and membership.role == GroupRole.ADMIN:
      return forbidden('Only the group owner can change another admin.')

    serializer = GroupMembershipSerializer(
      membership, data=request.data, partial=True
    )
    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    if serializer.validated_data.get('role') == GroupRole.OWNER:
      return forbidden('Ownership is handed over with transfer-ownership, not by role.')

    serializer.save()
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['DATA_UPDATED'],
      data=serializer.data,
    )

  @action(detail=True, methods=['post'])
  def transfer_ownership(self, request, *args, **kwargs):
    """Hand the group to another member.

    Owner-only, and deliberately not a role edit: the old owner drops to `ADMIN`
    and the new one gains every admin right, so a mistaken call hands the group
    over entirely. That makes it worth its own endpoint rather than a `PATCH` on
    the membership.
    """
    group = self.get_object()
    require(request, group, IsGroupOwner)

    target_id = request.data.get('user')
    if not target_id:
      return self.validation_error_response(
        single_field_error('user', 'This field is required.')
      )

    target = GroupMembership.objects.filter(group=group, user_id=target_id).first()
    if target is None:
      raise NotFound('Member not found.')
    if target.role == GroupRole.OWNER:
      return forbidden('This member already owns the group.')

    current = services.membership_of(request.user, group)
    with transaction.atomic():
      current.role = GroupRole.ADMIN
      current.save(update_fields=['role', 'updated_at'])
      target.role = GroupRole.OWNER
      target.save(update_fields=['role', 'updated_at'])
      group.owner = target.user
      group.save(update_fields=['owner', 'updated_at'])

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['DATA_UPDATED'],
      data=GroupMembershipSerializer(target).data,
    )


class GroupJoinRequestViewSet(GroupEnvelopeMixin, viewsets.ModelViewSet):
  """`/groups/<group_id>/join-requests/` - asking a registered user to join.

  The direction is the Instagram one: a member sends a request to somebody, and
  that somebody decides. Accepting is therefore restricted to the recipient,
  which is what separates this from an invite the sender controls outright.
  """

  serializer_class = GroupJoinRequestSerializer
  permission_classes = [IsAuthenticated]
  pagination_class = StandardPagination
  http_method_names = ['get', 'post', 'delete']
  not_found_message = 'Join request not found.'

  def get_group(self):
    """Resolve the group, tolerating a caller who is not in it yet.

    The recipient of a request is by definition usually not a member, so a
    strict `visible_groups` lookup would 404 them at the exact moment they were
    asked to join. The concession is narrow: a non-member reaches the group only
    if a request actually connects them to it. Anyone else asking about this
    group here gets the same 404 they would get from the groups endpoint, so the
    join-request routes cannot be used to probe which group ids exist.
    """
    try:
      group = Group.objects.filter(is_deleted=False).get(
        pk=self.kwargs['group_id']
      )
    except (Group.DoesNotExist, ValueError):
      raise NotFound('Group not found.')

    if services.is_member(self.request.user, group):
      return group

    if GroupJoinRequest.objects.filter(
      group=group,
    ).filter(
      Q(from_user=self.request.user) | Q(to_user=self.request.user)
    ).exists():
      return group

    raise NotFound('Group not found.')

  def get_serializer_context(self):
    """Hand the serializer the group the URL already selected.

    Without it the serializer has no group to validate against, since `group` is
    read-only and never appears in the payload.
    """
    context = super().get_serializer_context()
    context['group'] = self.get_group()
    return context

  def get_queryset(self):
    """Only ever the caller's own requests.

    This is the guard that makes the relaxed `get_group` safe: a non-member can
    name a group in the URL but sees nothing from it unless a request actually
    connects them to it. Admins widen it to the whole log.
    """
    group = self.get_group()
    queryset = GroupJoinRequest.objects.filter(group=group).select_related(
      'from_user', 'to_user'
    )

    if services.is_admin(self.request.user, group):
      pass  # Admins see the full log.
    else:
      queryset = queryset.filter(
        Q(from_user=self.request.user) | Q(to_user=self.request.user)
      )

    scope = self.request.query_params.get('scope')
    if scope == 'incoming':
      return queryset.filter(to_user=self.request.user)
    if scope == 'outgoing':
      return queryset.filter(from_user=self.request.user)
    if scope == 'pending':
      return queryset.filter(status=JoinRequestStatus.PENDING)
    return queryset

  def get_object(self):
    try:
      return self.get_queryset().get(pk=self.kwargs['pk'])
    except (GroupJoinRequest.DoesNotExist, ValueError):
      raise NotFound('Join request not found.')

  def create(self, request, *args, **kwargs):
    group = self.get_group()
    require(request, group, CanInvite)

    serializer = self.get_serializer(data=request.data)
    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    serializer.save(group=group, from_user=request.user)
    return success_response(
      status_code=HTTP_ERROR_CODES['CREATED'],
      message=RESPONSE_MESSAGES['CREATED'],
      data=serializer.data,
    )

  def list(self, request, *args, **kwargs):
    """`get_queryset` already draws the line between an admin's full log and
    everybody else's own requests."""
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data=self.paginated_payload(self.get_queryset()),
    )

  def retrieve(self, request, *args, **kwargs):
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data=self.get_serializer(self.get_object()).data,
    )

  @action(detail=True, methods=['post'])
  def accept(self, request, *args, **kwargs):
    join_request = self.get_object()
    if join_request.to_user_id != request.user.id:
      return forbidden('Only the recipient can accept this request.')
    return self._respond(services.accept_join_request, 'Request accepted.')

  @action(detail=True, methods=['post'])
  def reject(self, request, *args, **kwargs):
    join_request = self.get_object()
    if join_request.to_user_id != request.user.id:
      return forbidden('Only the recipient can reject this request.')
    return self._respond(services.reject_join_request, 'Request rejected.')

  def destroy(self, request, *args, **kwargs):
    """Withdraw a request. Sender only.

    Admins are deliberately not given this. The recipient already has `reject`
    for a request they do not want, and letting a third party retire a pending
    request would quietly remove an offer the target never got to answer.
    """
    join_request = self.get_object()
    if join_request.from_user_id != request.user.id:
      return forbidden('Only the sender can withdraw this request.')

    services.cancel_join_request(join_request)
    return success_response(
      status_code=HTTP_ERROR_CODES['200_NO_CONTENT'],
      message=RESPONSE_MESSAGES['DATA_DELETED'],
    )

  def _respond(self, handler, message):
    join_request = self.get_object()
    try:
      handler(join_request)
    except ValueError as exc:
      return detail_error(
        HTTP_ERROR_CODES['VALIDATION_ERROR'],
        RESPONSE_MESSAGES['VALIDATION_ERROR'],
        str(exc),
      )

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=message,
      data=self.get_serializer(join_request).data,
    )


class GroupInvitationViewSet(GroupEnvelopeMixin, viewsets.ModelViewSet):
  """`/groups/<group_id>/invitations/` - inviting an address with no account.

  `POST /groups/invitations/accept/` is the far end of the link and the only way
  an invitation becomes a membership. Nothing here joins anybody: the address has
  to register first, and then still has to claim it.
  """

  serializer_class = GroupInvitationSerializer
  permission_classes = [IsAuthenticated]
  pagination_class = StandardPagination
  http_method_names = ['get', 'post', 'delete']
  not_found_message = 'Invitation not found.'

  def get_group(self):
    try:
      group = services.visible_groups(self.request.user).get(
        pk=self.kwargs['group_id']
      )
    except (Group.DoesNotExist, ValueError):
      raise NotFound('Group not found.')
    require(self.request, group, IsGroupMember)
    return group

  def get_serializer_context(self):
    """Hand the serializer the group the URL already selected.

    `group` is read-only, so it never reaches `validate()` from the payload.
    """
    context = super().get_serializer_context()
    context['group'] = self.get_group()
    return context

  def get_queryset(self):
    group = self.get_group()
    # Resolving expiry on read means a lapsed link stops looking usable without a
    # job to sweep it.
    services.cancel_expired_invitations(group)
    return GroupInvitation.objects.filter(group=group).select_related(
      'invited_by', 'group'
    )

  def get_object(self):
    try:
      return self.get_queryset().get(pk=self.kwargs['pk'])
    except (GroupInvitation.DoesNotExist, ValueError):
      raise NotFound('Invitation not found.')

  def create(self, request, *args, **kwargs):
    group = self.get_group()
    require(request, group, CanSendInvitation)

    payload = dict(request.data)
    if not payload.get('expires_at'):
      payload['expires_at'] = services.invitation_default_expiry()

    serializer = self.get_serializer(data=payload)
    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    serializer.save(group=group, invited_by=request.user)
    return success_response(
      status_code=HTTP_ERROR_CODES['CREATED'],
      message=RESPONSE_MESSAGES['CREATED'],
      data=serializer.data,
    )

  def retrieve(self, request, *args, **kwargs):
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data=self.get_serializer(self.get_object()).data,
    )

  def destroy(self, request, *args, **kwargs):
    """Revoke a pending invitation. One already answered is left alone."""
    invitation = self.get_object()
    if invitation.status != InvitationStatus.PENDING:
      return detail_error(
        HTTP_ERROR_CODES['VALIDATION_ERROR'],
        RESPONSE_MESSAGES['VALIDATION_ERROR'],
        'This invitation has already been answered.',
      )

    invitation.status = InvitationStatus.REVOKED
    invitation.responded_at = get_unix_timestamp()
    invitation.save(update_fields=['status', 'responded_at', 'updated_at'])

    return success_response(
      status_code=HTTP_ERROR_CODES['200_NO_CONTENT'],
      message=RESPONSE_MESSAGES['DATA_DELETED'],
    )


class MyInvitationViewSet(GroupEnvelopeMixin, viewsets.GenericViewSet):
  """`/groups/invitations/` - the caller's own invitations, and claiming one.

  Matching is on the caller's email, which is what makes an invitation issued to
  an unregistered address reachable at all: the row becomes visible the moment its
  owner has an account, and to nobody else.

  `GenericViewSet` rather than `ViewSet` on purpose: `paginate_queryset` and
  `paginator` come from `GenericAPIView`, and a bare `ViewSet` has neither, so the
  listing would raise `AttributeError` on first call.
  """

  serializer_class = GroupInvitationSerializer
  permission_classes = [IsAuthenticated]
  pagination_class = StandardPagination
  not_found_message = 'Invitation not found.'

  def get_queryset(self):
    services.cancel_expired_invitations()
    return GroupInvitation.objects.filter(
      email__iexact=self.request.user.email
    ).select_related('group', 'invited_by')

  def list(self, request, *args, **kwargs):
    queryset = self.get_queryset().filter(status=InvitationStatus.PENDING)
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data=paginated(self, queryset, GroupInvitationSerializer),
    )

  @action(detail=False, methods=['post'], url_path='accept')
  def accept(self, request, *args, **kwargs):
    """Claim an invitation by token and join the group it names.

    Idempotent, because an invite link is the sort of thing people open twice, and
    because a request promoted at registration may already be waiting in the
    recipient's inbox by the time they click the link.
    """
    serializer = InvitationAcceptSerializer(
      data=request.data, context={'request': request}
    )
    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    invitation = serializer.context['invitation']
    membership, created = services.add_member(invitation.group, request.user)

    invitation.status = InvitationStatus.ACCEPTED
    invitation.accepted_by = request.user
    invitation.responded_at = get_unix_timestamp()
    invitation.save(
      update_fields=['status', 'accepted_by', 'responded_at', 'updated_at']
    )

    return success_response(
      status_code=HTTP_ERROR_CODES['CREATED' if created else 'SUCCESS'],
      message='Invitation accepted.',
      data={
        'invitation': GroupInvitationSerializer(invitation).data,
        'membership': GroupMembershipSerializer(membership).data,
      },
    )


class GroupChallengeViewSet(GroupEnvelopeMixin, viewsets.ModelViewSet):
  """`/groups/<group_id>/challenges/` - challenges proposed inside a group.

  A challenge is a `Habit`, so it reuses the schedule, the logs and the whole
  analytics stack rather than duplicating them. This viewset exists because the
  habit endpoints scope strictly to the caller's own rows, and a group member has
  to be able to see a challenge they did not create.
  """

  serializer_class = HabitSerializer
  permission_classes = [IsAuthenticated]
  pagination_class = StandardPagination
  http_method_names = ['get', 'post']
  not_found_message = 'Challenge not found.'

  def get_group(self):
    try:
      group = services.visible_groups(self.request.user).get(
        pk=self.kwargs['group_id']
      )
    except (Group.DoesNotExist, ValueError):
      raise NotFound('Group not found.')
    require(self.request, group, IsGroupMember)
    return group

  def group_challenges(self):
    """Every challenge template in the group.

    Templates only: a member's participation copy is an implementation detail of
    their own progress, and listing those here would show the same challenge once
    per participant.
    """
    group = self.get_group()
    return Habit.objects.filter(
      group=group,
      is_challenge=True,
      is_deleted=False,
      challenge_source__isnull=True,
    ).select_related('tag', 'schedule')

  def get_queryset(self):
    queryset = self.group_challenges().order_by('-created_at')

    challenge_status = self.request.query_params.get('challenge_status')
    if challenge_status:
      queryset = queryset.filter(challenge_status=challenge_status)
    return queryset

  def get_object(self):
    self.get_group()
    try:
      challenge = self.group_challenges().get(pk=self.kwargs['pk'])
    except (Habit.DoesNotExist, ValueError):
      raise NotFound('Challenge not found.')
    return habit_challenges.sync_and_settle(challenge)

  def list(self, request, *args, **kwargs):
    """Settle anything whose final day has passed before answering.

    Completion is driven from the read path because there is no scheduler in this
    project. Doing it before serialising is what makes a challenge read as
    COMPLETED with a winner the day after it ends, rather than reading ACTIVE
    forever because no job ever came along.
    """
    for challenge in self.group_challenges().filter(
      challenge_status=ChallengeStatus.ACTIVE
    ):
      habit_challenges.refresh_challenge(challenge)

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data=self.paginated_payload(self.get_queryset()),
    )

  def retrieve(self, request, *args, **kwargs):
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data=self.get_serializer(self.get_object()).data,
    )

  def get_serializer_context(self):
    """Tell the habit serializer it is being written as a challenge.

    At create time there is no instance to inspect, so the flag has to travel in
    the context. Without it the challenge rules and the fixed-duration
    requirement would both be skipped on the one payload that most needs them.
    """
    context = super().get_serializer_context()
    context['challenge'] = True
    return context

  def create(self, request, *args, **kwargs):
    group = self.get_group()
    require(request, group, CanCreateChallenge)

    serializer = self.get_serializer(data=request.data)
    if not serializer.is_valid():
      return self.validation_error_response(serializer)

    # `group` and `is_challenge` are read-only on the serializer: a habit only
    # becomes a challenge by being proposed inside a group, and that has to be
    # this endpoint rather than a field anybody can set on any habit.
    challenge = serializer.save(user=request.user, group=group, is_challenge=True)

    # The author is a participant from the moment they propose it, logging
    # against the challenge row itself rather than a copy of it.
    habit_challenges.subscribe(challenge, request.user)

    return success_response(
      status_code=HTTP_ERROR_CODES['CREATED'],
      message=RESPONSE_MESSAGES['CREATED'],
      data=self.get_serializer(challenge).data,
    )