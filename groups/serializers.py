"""Serializers for groups, memberships, join requests and invitations.

Two conventions are carried over from `habits.serializers`:

* the queryset on a related field is narrowed in `__init__` from the request
  user, so a payload cannot name an id outside the caller's reach, and
* validation failures raise `serializers.ValidationError` keyed by field, which
  the views render as the project's `411 VALIDATION_ERROR` envelope.

Narrowing the queryset is not the same as authorising. A related id that exists
but is out of scope must read as "not found", and the views collapse that into a
404 by scoping their own querysets.
"""

from django.contrib.auth import get_user_model
from rest_framework import serializers

from common.enums import InvitationStatus, JoinRequestStatus
from common.utils import get_unix_timestamp

from . import services
from .models import Group, GroupInvitation, GroupJoinRequest, GroupMembership


def _request_user(serializer):
  request = serializer.context.get('request')
  return getattr(request, 'user', None) if request is not None else None


class GroupSerializer(serializers.ModelSerializer):
  """Group detail, including the caller's own role so the client knows what it
  may offer the user without a second round trip."""

  member_count = serializers.SerializerMethodField()
  my_role = serializers.SerializerMethodField()

  class Meta:
    model = Group
    fields = [
      'id',
      'name',
      'description',
      'owner',
      'owner_email',
      'is_private',
      'join_requests_enabled',
      'members_can_invite',
      'anyone_can_create_challenge',
      'member_count',
      'my_role',
      'created_at',
      'updated_at',
    ]
    read_only_fields = ('id', 'owner', 'owner_email', 'created_at', 'updated_at')
    extra_kwargs = {
      'name': {'required': True},
      'description': {'required': False},
      'is_private': {'required': False},
      'join_requests_enabled': {'required': False},
      'members_can_invite': {'required': False},
      'anyone_can_create_challenge': {'required': False},
    }

  owner_email = serializers.EmailField(source='owner.email', read_only=True)

  def get_member_count(self, obj):
    annotated = getattr(obj, 'member_count', None)
    if annotated is not None:
      return annotated
    return obj.memberships.count()

  def get_my_role(self, obj):
    return services.role_of(_request_user(self), obj)

  def validate_name(self, value):
    name = (value or '').strip()
    if not name:
      raise serializers.ValidationError('Group name cannot be empty.')
    return name

  def validate(self, attrs):
    if self.instance is not None and not services.is_admin(
      _request_user(self), self.instance
    ):
      raise serializers.ValidationError({
        'detail': 'Only group admins can update group settings.'
      })
    return attrs


class GroupMembershipSerializer(serializers.ModelSerializer):
  """A member row. Read-only here; role changes go through the members endpoint
  so the promotion rules live in one place instead of in a generic update."""

  user_email = serializers.EmailField(source='user.email', read_only=True)
  display_name = serializers.SerializerMethodField()

  class Meta:
    model = GroupMembership
    fields = [
      'id',
      'group',
      'user',
      'user_email',
      'display_name',
      'role',
      'joined_at',
      'created_at',
      'updated_at',
    ]
    read_only_fields = ('id', 'group', 'user', 'user_email', 'display_name', 'joined_at', 'created_at', 'updated_at')
    extra_kwargs = {'role': {'required': False}}

  def get_display_name(self, obj):
    full_name = f"{obj.user.first_name} {obj.user.last_name}".strip()
    return full_name or obj.user.email

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    if 'group' in self.fields:
      self.fields['group'].queryset = Group.objects.filter(is_deleted=False)


class GroupJoinRequestSerializer(serializers.ModelSerializer):
  """A request for a registered user to join the group.

  The write shape is an **address**, not an id: `email` names the account to ask
  and is resolved to `to_user` during validation, so nobody has to go hunting
  for a UUID. `to_user` stays in the read shape because every list and detail
  payload needs it, but as a read-only field it can no longer be written.

  `from_user` is always the caller and `status` is always PENDING on create: the
  recipient alone decides the outcome, so neither can be set by the sender.
  """

  from_user_email = serializers.EmailField(source='from_user.email', read_only=True)
  to_user_email = serializers.EmailField(source='to_user.email', read_only=True)
  group_name = serializers.CharField(source='group.name', read_only=True)
  email = serializers.EmailField(write_only=True, required=True)

  class Meta:
    model = GroupJoinRequest
    fields = [
      'id',
      'group',
      'group_name',
      'from_user',
      'from_user_email',
      'to_user',
      'to_user_email',
      'email',
      'status',
      'message',
      'responded_at',
      'created_at',
      'updated_at',
    ]
    read_only_fields = (
      'id',
      'group',
      'group_name',
      'from_user',
      'from_user_email',
      'to_user',
      'to_user_email',
      'status',
      'responded_at',
      'created_at',
      'updated_at',
    )
    extra_kwargs = {
      'message': {'required': False, 'allow_blank': True},
    }

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    if 'group' in self.fields:
      self.fields['group'].queryset = Group.objects.filter(is_deleted=False)

  def validate_email(self, value):
    return (value or '').strip().lower()

  def validate(self, attrs):
    # `group` is read-only, so DRF strips it out of the payload before this
    # runs. It travels in the context instead, which also means the client
    # cannot name a group other than the one the URL already selected.
    group = self.context.get('group') or getattr(self.instance, 'group', None)
    # Popped rather than read: the row stores `to_user`, so the address must not
    # travel on into `objects.create` and be mistaken for a model field.
    email = attrs.pop('email', None)
    requester = _request_user(self)

    if group is None:
      raise serializers.ValidationError({'group': 'This field is required.'})
    if not email:
      raise serializers.ValidationError({'email': 'This field is required.'})

    to_user = get_user_model().objects.filter(
      email__iexact=email, is_deleted=False
    ).first()
    if to_user is None:
      # The mirror image of the invitation rule: that channel is for addresses
      # with no account, this one is for addresses that have one.
      raise serializers.ValidationError({
        'email': 'No account uses this address. Send them an email invitation instead.'
      })

    if requester is None:
      attrs['to_user'] = to_user
      return attrs

    if to_user.id == requester.id:
      raise serializers.ValidationError({
        'email': 'You cannot send a join request to yourself.'
      })

    if not services.is_member(requester, group):
      raise serializers.ValidationError({
        'group': 'You are not a member of this group.'
      })

    if services.membership_of(to_user, group) is not None:
      raise serializers.ValidationError({
        'email': 'This user is already a member of the group.'
      })

    if GroupJoinRequest.objects.filter(
      group=group,
      from_user=requester,
      to_user=to_user,
      status=JoinRequestStatus.PENDING,
    ).exists():
      raise serializers.ValidationError({
        'email': 'A pending join request to this address already exists.'
      })

    # Read-only fields are dropped before `validate` runs, so the resolved
    # account is put back here - it lands in `validated_data` and reaches
    # `create` like any other value.
    attrs['to_user'] = to_user
    return attrs


class GroupInvitationSerializer(serializers.ModelSerializer):
  """An invitation sent to an address that has no account yet.

  The `token` is exposed because it is the whole point of the row: it is the
  `/invite?token=` link the inviter passes on by hand. It is read-only, so it
  can only ever be minted by the server.
  """

  invited_by_email = serializers.EmailField(source='invited_by.email', read_only=True)
  group_name = serializers.CharField(source='group.name', read_only=True)

  class Meta:
    model = GroupInvitation
    fields = [
      'id',
      'group',
      'group_name',
      'invited_by',
      'invited_by_email',
      'email',
      'token',
      'status',
      'expires_at',
      'accepted_by',
      'responded_at',
      'created_at',
      'updated_at',
    ]
    read_only_fields = (
      'id',
      'group',
      'group_name',
      'invited_by',
      'invited_by_email',
      'token',
      'status',
      'accepted_by',
      'responded_at',
      'created_at',
      'updated_at',
    )
    extra_kwargs = {
      'email': {'required': True},
      'expires_at': {'required': False},
    }

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    if 'group' in self.fields:
      self.fields['group'].queryset = Group.objects.filter(is_deleted=False)

  def validate_email(self, value):
    email = (value or '').strip().lower()

    # The story scopes email invitations to people who are not on the platform
    # yet; an existing member is reached with a join request instead. Matching
    # is case-insensitive because `User.email` is normalised on the way in and
    # the inviter may well type the address with a different casing.
    if get_user_model().objects.filter(email__iexact=email).exists():
      raise serializers.ValidationError(
        'This address already belongs to a registered user. Send them a join '
        'request instead.'
      )
    return email

  def validate(self, attrs):
    group = self.context.get('group') or getattr(self.instance, 'group', None)
    email = attrs.get('email', getattr(self.instance, 'email', None))
    inviter = _request_user(self)

    if group is None:
      raise serializers.ValidationError({'group': 'This field is required.'})
    if email is None:
      raise serializers.ValidationError({'email': 'This field is required.'})
    if inviter is not None and not services.is_member(inviter, group):
      raise serializers.ValidationError({
        'group': 'You are not a member of this group.'
      })

    if GroupInvitation.objects.filter(
      group=group,
      email=email,
      status=InvitationStatus.PENDING,
    ).exists():
      raise serializers.ValidationError({
        'email': 'A pending invitation to this address already exists.'
      })

    expires_at = attrs.get('expires_at', getattr(self.instance, 'expires_at', None))
    if expires_at is not None and expires_at <= get_unix_timestamp():
      raise serializers.ValidationError({
        'expires_at': 'Expiry must be in the future.'
      })

    return attrs


class InvitationAcceptSerializer(serializers.Serializer):
  """Write shape for claiming an invitation by its token."""

  token = serializers.UUIDField(required=True)

  def validate_token(self, value):
    invitation = GroupInvitation.objects.filter(token=value).first()
    if invitation is None:
      raise serializers.ValidationError('This invitation link is not valid.')

    if services.expire_invitation(invitation):
      raise serializers.ValidationError('This invitation link has expired.')

    if invitation.status != InvitationStatus.PENDING:
      raise serializers.ValidationError(
        f'This invitation is already {invitation.status}.'
      )

    claimant = _request_user(self)
    if claimant is not None and invitation.email.lower() != claimant.email.lower():
      raise serializers.ValidationError(
        'This invitation was sent to a different email address.'
      )

    self.context['invitation'] = invitation
    return value