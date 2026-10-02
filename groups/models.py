import uuid

from django.conf import settings
from django.db import models

from common.enums import GroupRole, JoinRequestStatus, InvitationStatus
from common.utils import get_unix_timestamp


class Group(models.Model):
  id = models.UUIDField(
    primary_key=True,
    default=uuid.uuid4,
    editable=False
  )
  name = models.CharField(max_length=100)
  description = models.TextField(blank=True)
  owner = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    related_name="owned_groups"
  )
  is_private = models.BooleanField(default=False)
  join_requests_enabled = models.BooleanField(default=True)
  members_can_invite = models.BooleanField(default=True)
  anyone_can_create_challenge = models.BooleanField(default=True)
  is_deleted = models.BooleanField(default=False)
  created_at = models.BigIntegerField(
    default=get_unix_timestamp,
    editable=False
  )
  updated_at = models.BigIntegerField(
    default=get_unix_timestamp
  )

  class Meta:
    ordering = ["-created_at"]
    indexes = [
      models.Index(fields=["owner"], name="group_owner_idx"),
      models.Index(fields=["is_deleted"], name="group_deleted_idx"),
    ]

  def save(self, *args, **kwargs):
    self.updated_at = get_unix_timestamp()
    super().save(*args, **kwargs)

  def __str__(self):
    return self.name


class GroupMembership(models.Model):
  id = models.UUIDField(
    primary_key=True,
    default=uuid.uuid4,
    editable=False
  )
  group = models.ForeignKey(
    Group,
    on_delete=models.CASCADE,
    related_name="memberships"
  )
  user = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    related_name="group_memberships"
  )
  role = models.CharField(
    max_length=20,
    choices=GroupRole.choices,
    default=GroupRole.MEMBER
  )
  joined_at = models.BigIntegerField(default=get_unix_timestamp)
  created_at = models.BigIntegerField(
    default=get_unix_timestamp,
    editable=False
  )
  updated_at = models.BigIntegerField(
    default=get_unix_timestamp
  )

  class Meta:
    unique_together = ("group", "user")
    ordering = ["-joined_at"]
    indexes = [
      models.Index(fields=["group", "role"], name="gm_group_role_idx"),
      models.Index(fields=["user"], name="gm_user_idx"),
    ]

  def save(self, *args, **kwargs):
    self.updated_at = get_unix_timestamp()
    super().save(*args, **kwargs)


class GroupJoinRequest(models.Model):
  id = models.UUIDField(
    primary_key=True,
    default=uuid.uuid4,
    editable=False
  )
  group = models.ForeignKey(
    Group,
    on_delete=models.CASCADE,
    related_name="join_requests"
  )
  from_user = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    related_name="sent_join_requests"
  )
  to_user = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    related_name="received_join_requests"
  )
  status = models.CharField(
    max_length=20,
    choices=JoinRequestStatus.choices,
    default=JoinRequestStatus.PENDING
  )
  message = models.TextField(blank=True)
  responded_at = models.BigIntegerField(null=True, blank=True)
  created_at = models.BigIntegerField(
    default=get_unix_timestamp,
    editable=False
  )
  updated_at = models.BigIntegerField(
    default=get_unix_timestamp
  )

  class Meta:
    unique_together = ("group", "from_user", "to_user", "status")
    ordering = ["-created_at"]
    indexes = [
      models.Index(fields=["group", "status"], name="gjr_group_status_idx"),
      models.Index(fields=["to_user", "status"], name="gjr_to_status_idx"),
      models.Index(fields=["from_user", "status"], name="gjr_from_status_idx"),
    ]

  def save(self, *args, **kwargs):
    self.updated_at = get_unix_timestamp()
    super().save(*args, **kwargs)


class GroupInvitation(models.Model):
  id = models.UUIDField(
    primary_key=True,
    default=uuid.uuid4,
    editable=False
  )
  group = models.ForeignKey(
    Group,
    on_delete=models.CASCADE,
    related_name="invitations"
  )
  invited_by = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    related_name="sent_invitations"
  )
  email = models.EmailField()
  token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
  status = models.CharField(
    max_length=20,
    choices=InvitationStatus.choices,
    default=InvitationStatus.PENDING
  )
  expires_at = models.BigIntegerField(null=True, blank=True)
  accepted_by = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.SET_NULL,
    null=True, blank=True,
    related_name="accepted_invitations"
  )
  responded_at = models.BigIntegerField(null=True, blank=True)
  created_at = models.BigIntegerField(
    default=get_unix_timestamp,
    editable=False
  )
  updated_at = models.BigIntegerField(
    default=get_unix_timestamp
  )

  class Meta:
    ordering = ["-created_at"]
    indexes = [
      models.Index(fields=["email", "status"], name="gi_email_status_idx"),
      models.Index(fields=["token"], name="gi_token_idx"),
      models.Index(fields=["group", "status"], name="gi_group_status_idx"),
    ]

  def save(self, *args, **kwargs):
    self.updated_at = get_unix_timestamp()
    super().save(*args, **kwargs)
