"""The emails the account flow sends."""

from common.emails import BRAND_NAME, send_templated_email


def send_password_reset_email(user, *, reset_url, expires_hours):
  """Send the reset link to `user`.

  `reset_url` is built by the caller because only it knows how the token was
  minted; this function only owns the copy and the branding.
  """
  return send_templated_email(
    template='password_reset',
    subject=f'Reset your {BRAND_NAME} password',
    to=user.email,
    context={
      'reset_url': reset_url,
      'expires_hours': expires_hours,
    },
  )
