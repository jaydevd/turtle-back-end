"""Routes for `/api/user/groups/`.

Written out explicitly rather than through a router, matching `habits/urls.py`:
the literal-prefixed routes have to be declared above the `<uuid:...>` catch-alls
or the uuid converter swallows them, and that ordering is easier to see written
down than to configure.

Members, join requests, invitations and challenges are all nested under a group.
The literal `join-requests/`, `invitations/` and `challenges/` segments below sit
above `<uuid:group_id>` deliberately, for the same reason the habit routes do it.
The first two are the caller's own inbox rather than a group's.
"""

from django.urls import path

from .views import (
  GroupChallengeViewSet,
  GroupInvitationViewSet,
  GroupJoinRequestViewSet,
  GroupViewSet,
  MyInvitationViewSet,
  MyJoinRequestViewSet,
)

group_list = GroupViewSet.as_view({'get': 'list', 'post': 'create'})
group_detail = GroupViewSet.as_view({
  'get': 'retrieve',
  'put': 'update',
  'patch': 'partial_update',
  'delete': 'destroy',
})
group_members = GroupViewSet.as_view({'get': 'members'})
group_member = GroupViewSet.as_view({'patch': 'member', 'delete': 'member'})
group_transfer_ownership = GroupViewSet.as_view({'post': 'transfer_ownership'})

join_request_list = GroupJoinRequestViewSet.as_view({'get': 'list', 'post': 'create'})
join_request_detail = GroupJoinRequestViewSet.as_view({
  'get': 'retrieve',
  'delete': 'destroy',
})
join_request_accept = GroupJoinRequestViewSet.as_view({'post': 'accept'})
join_request_reject = GroupJoinRequestViewSet.as_view({'post': 'reject'})

group_invitation_list = GroupInvitationViewSet.as_view({'get': 'list', 'post': 'create'})
group_invitation_detail = GroupInvitationViewSet.as_view({
  'get': 'retrieve',
  'delete': 'destroy',
})

my_invitations = MyInvitationViewSet.as_view({'get': 'list'})
my_invitation_accept = MyInvitationViewSet.as_view({'post': 'accept'})

my_join_requests = MyJoinRequestViewSet.as_view({'get': 'list'})

group_challenge_list = GroupChallengeViewSet.as_view({'get': 'list', 'post': 'create'})
group_challenge_detail = GroupChallengeViewSet.as_view({'get': 'retrieve'})

urlpatterns = [
  # The caller's own inbox, in both directions. Above `<uuid:group_id>` for the
  # same reason the invitations are: a recipient has no group id to work from.
  path('join-requests/', my_join_requests, name='group-my-join-requests'),

  # The caller's own invitations. Above `<uuid:group_id>` so the literal segment
  # is matched before the uuid converter gets a chance to.
  path('invitations/', my_invitations, name='group-my-invitations'),
  path('invitations/accept/', my_invitation_accept, name='group-invitation-accept'),

  path('', group_list, name='group-list-create'),
  path('<uuid:pk>/', group_detail, name='group-detail'),
  path('<uuid:group_id>/members/', group_members, name='group-members'),
  path('<uuid:group_id>/members/<uuid:member_id>/', group_member, name='group-member-detail'),
  path(
    '<uuid:group_id>/transfer-ownership/',
    group_transfer_ownership,
    name='group-transfer-ownership',
  ),
  path(
    '<uuid:group_id>/join-requests/',
    join_request_list,
    name='group-join-request-list-create',
  ),
  path(
    '<uuid:group_id>/join-requests/<uuid:pk>/',
    join_request_detail,
    name='group-join-request-detail',
  ),
  path(
    '<uuid:group_id>/join-requests/<uuid:pk>/accept/',
    join_request_accept,
    name='group-join-request-accept',
  ),
  path(
    '<uuid:group_id>/join-requests/<uuid:pk>/reject/',
    join_request_reject,
    name='group-join-request-reject',
  ),
  path(
    '<uuid:group_id>/invitations/',
    group_invitation_list,
    name='group-invitation-list-create',
  ),
  path(
    '<uuid:group_id>/invitations/<uuid:pk>/',
    group_invitation_detail,
    name='group-invitation-detail',
  ),
  path(
    '<uuid:group_id>/challenges/',
    group_challenge_list,
    name='group-challenge-list-create',
  ),
  path(
    '<uuid:group_id>/challenges/<uuid:pk>/',
    group_challenge_detail,
    name='group-challenge-detail',
  ),
]