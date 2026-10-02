from django.contrib import admin

from .models import Group, GroupInvitation, GroupJoinRequest, GroupMembership


class GroupMembershipInline(admin.TabularInline):
  model = GroupMembership
  extra = 0
  raw_id_fields = ('user',)


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
  list_display = ('name', 'owner', 'is_private', 'join_requests_enabled', 'members_can_invite', 'anyone_can_create_challenge', 'is_deleted', 'created_at')
  list_filter = ('is_private', 'join_requests_enabled', 'members_can_invite', 'anyone_can_create_challenge', 'is_deleted')
  search_fields = ('name', 'owner__email')
  raw_id_fields = ('owner',)
  inlines = (GroupMembershipInline,)


@admin.register(GroupMembership)
class GroupMembershipAdmin(admin.ModelAdmin):
  list_display = ('group', 'user', 'role', 'joined_at')
  list_filter = ('role',)
  search_fields = ('group__name', 'user__email')
  raw_id_fields = ('group', 'user')


@admin.register(GroupJoinRequest)
class GroupJoinRequestAdmin(admin.ModelAdmin):
  list_display = ('group', 'from_user', 'to_user', 'status', 'created_at')
  list_filter = ('status',)
  search_fields = ('group__name', 'from_user__email', 'to_user__email')
  raw_id_fields = ('group', 'from_user', 'to_user')


@admin.register(GroupInvitation)
class GroupInvitationAdmin(admin.ModelAdmin):
  list_display = ('group', 'email', 'invited_by', 'status', 'expires_at', 'created_at')
  list_filter = ('status',)
  search_fields = ('group__name', 'email', 'invited_by__email')
  raw_id_fields = ('group', 'invited_by', 'accepted_by')