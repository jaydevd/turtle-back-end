"""The emails the group flow sends."""

from datetime import datetime, timezone

from django.conf import settings
from django.utils.http import urlencode

from common.emails import BRAND_NAME, send_templated_email


def _expires_on(timestamp):
  """Turn the invitation's unix deadline into a date the template can print.

  `None` when the invitation never lapses, which the template reads as its cue
  to skip the deadline line.
  """
  if not timestamp:
    return None
  return datetime.fromtimestamp(timestamp, tz=timezone.utc).date()


def send_invitation_email(invitation):
  """Email an invitee the sign-up link that leads to `invitation`.

  Invitations are addressed to people who have no account yet, so this is the
  one message that reaches someone before they have ever signed in. It points at
  the sign-up screen rather than a claim page: the join request is already
  waiting, and it is matched to them by email the moment they register.
  """
  frontend_url = settings.FRONTEND_URL.rstrip('/')
  signup_url = f"{frontend_url}/signup?{urlencode({'email': invitation.email})}"

  return send_templated_email(
    template='invitation',
    subject=f'{invitation.invited_by.email} invited you to join {invitation.group.name} on {BRAND_NAME}',
    to=invitation.email,
    context={
      'signup_url': signup_url,
      'group_name': invitation.group.name,
      'inviter_email': invitation.invited_by.email,
      'expires_on': _expires_on(invitation.expires_at),
    },
  )
