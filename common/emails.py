"""Branded transactional email.

Every message the product sends is rendered from `templates/emails/` and sent as
multipart: an HTML body for clients that draw it, with a plain-text twin that
still carries a working link for the ones that do not. The palette in the
templates mirrors `frontend/src/theme/tokens.ts` (the light scheme) so a message
looks like the app it came from.
"""

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

# The product's voice, kept in one place so a rename does not have to chase down
# every template and subject line.
BRAND_NAME = 'Turtle'
BRAND_TAGLINE = 'Build consistency, one day at a time.'


def brand_context():
  """The values every template can rely on, regardless of which email it is."""
  frontend_url = settings.FRONTEND_URL.rstrip('/')
  return {
    'brand_name': BRAND_NAME,
    'brand_tagline': BRAND_TAGLINE,
    'frontend_url': frontend_url,
    'logo_url': f'{frontend_url}/brand/logo.png',
  }


def send_templated_email(*, template, subject, to, context):
  """Render `emails/<template>.{html,txt}` and send it to one address.

  Returns the number of messages sent (Django's 0/1). Delivery errors are raised
  rather than swallowed, so the caller decides whether a mail outage should fail
  the request it came from.
  """
  merge_context = {**brand_context(), **context}
  text_body = render_to_string(f'emails/{template}.txt', merge_context)
  html_body = render_to_string(f'emails/{template}.html', merge_context)

  message = EmailMultiAlternatives(
    subject=subject,
    body=text_body,
    from_email=settings.DEFAULT_FROM_EMAIL,
    to=[to],
  )
  message.attach_alternative(html_body, 'text/html')
  return message.send(fail_silently=False)
