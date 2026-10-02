"""Fixtures for the group tests.

Groups are usually built through the API rather than by calling
`Group.objects.create`, because creating a group is itself an operation that does
two things - writes the row *and* claims an `OWNER` membership for the creator.
Hand-building it would let a test set up a state the API never produces.

`make_group` is the exception, for tests that need several groups without caring
how they came about.
"""

from common.enums import GroupRole

from ..models import Group, GroupMembership


def make_user(email, password='pw-for-tests-9'):
  from user.models import User

  return User.objects.create_user(email=email, password=password)


def join(group, user, role=GroupRole.MEMBER):
  return GroupMembership.objects.create(group=group, user=user, role=role)


def make_group(owner, name='Morning Crew', **fields):
  group = Group.objects.create(name=name, owner=owner, **fields)
  join(group, owner, role=GroupRole.OWNER)
  return group