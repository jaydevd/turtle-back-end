"""Google sign-in: the authorization-code exchange and the user lookup behind it.

Kept out of `views.py` so the views stay a thin translation of HTTP to the
shared `{tokens, user}` payload, and so the network half can be replaced in
tests without touching the resolution logic.

The exchange is deliberately server side. The browser only ever sees a
redirect to Google and a redirect back carrying a pair of SimpleJWT tokens, so
the client secret never enters the frontend and no Google credential has to be
exposed as a `NEXT_PUBLIC_` variable.
"""

from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction

from groups.services import promote_invitations_for

from .models import GoogleIdentity, User

AUTHORIZATION_ENDPOINT = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token'
USERINFO_ENDPOINT = 'https://www.googleapis.com/oauth2/v3/userinfo'

# Salt keeps these blobs from being interchangeable with anything else Django
# signs with the same SECRET_KEY.
STATE_SALT = 'user.google_oauth.state'
STATE_MAX_AGE = 600
HTTP_TIMEOUT = 10


class GoogleOAuthError(Exception):
    """Google refused the exchange, or answered with something unusable."""


class GoogleAccountCollision(Exception):
    """A password account already owns this email and linking is switched off."""

    def __init__(self, email):
        self.email = email
        super().__init__(email)


class GoogleIdentityTaken(Exception):
    """The Google account asked for is already connected to someone else."""

    def __init__(self, sub):
        self.sub = sub
        super().__init__(sub)


def _config():
    return settings.GOOGLE_OAUTH


def is_enabled():
    return bool(getattr(settings, 'GOOGLE_OAUTH_ENABLED', False))


def authorization_url(state):
    """Where to send the browser to ask for consent."""
    config = _config()
    query = urlencode({
        'client_id': config['CLIENT_ID'],
        'redirect_uri': config['REDIRECT_URI'],
        'response_type': 'code',
        # `openid email profile` is the minimum that yields an email; `openid`
        # additionally puts a verifiable id_token in the token response.
        'scope': 'openid email profile',
        'state': state,
        # Always show the account chooser. Without this, returning users are
        # silently signed in as whoever Google happens to think they are, which
        # is surprising on a shared machine and makes signing *out* of one
        # Google account to sign in as another impossible.
        'prompt': 'select_account',
    })
    return f'{AUTHORIZATION_ENDPOINT}?{query}'


def sign_state(next_url):
    """Carry the post-login destination through the round trip.

    Signed rather than stored: the callback arrives through a Next.js proxy on a
    different host from the one that issued it, and a stateless blob needs no
    shared session store or cookie to survive the hop.
    """
    return signing.dumps({'next': next_url}, salt=STATE_SALT, compress=True)


def sign_link_state(user_id, next_url):
    """State for connecting Google to an account that is already signed in.

    The callback cannot be authenticated - it arrives as a bare navigation with
    no Authorization header - so the user it may link to travels inside the
    signed blob instead. Only this server can mint one, and it only does so in
    answer to an authenticated request, which is what makes the blob proof of
    who asked.
    """
    return signing.dumps(
        {'next': next_url, 'link_user': str(user_id)},
        salt=STATE_SALT,
        compress=True,
    )


def read_state(state):
    """Unpack a `state` blob, or return None if it was tampered with or expired."""
    if not state:
        return None
    try:
        payload = signing.loads(
            state,
            salt=STATE_SALT,
            max_age=_config()['STATE_MAX_AGE'],
        )
    except signing.BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def safe_next(raw):
    """Keep the post-login destination on this site.

    This value is interpolated into a redirect Location, so anything that is not
    a rooted path is discarded and the caller falls back to its own default.
    Mirrors the same guard the frontend applies.
    """
    if not isinstance(raw, str) or not raw.startswith('/') or raw.startswith('//'):
        return None
    return raw


def exchange_code(code):
    """Trade the one-time authorization code for an access token."""
    config = _config()
    try:
        response = requests.post(
            TOKEN_ENDPOINT,
            data={
                'code': code,
                'client_id': config['CLIENT_ID'],
                'client_secret': config['CLIENT_SECRET'],
                'redirect_uri': config['REDIRECT_URI'],
                'grant_type': 'authorization_code',
            },
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GoogleOAuthError('Could not reach Google to complete the sign in.') from exc

    if response.status_code != 200:
        raise GoogleOAuthError('Google rejected the authorization code.')

    try:
        return response.json()['access_token']
    except (ValueError, KeyError) as exc:
        raise GoogleOAuthError('Google returned an unexpected token response.') from exc


def fetch_profile(access_token):
    """Read the signed-in Google account's identity.

    Called against Google's userinfo endpoint rather than trusting a decoded
    id_token: the access token is opaque here, and the endpoint answers only
    for the account Google actually authenticated.
    """
    try:
        response = requests.get(
            USERINFO_ENDPOINT,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GoogleOAuthError('Could not reach Google to read your account.') from exc

    if response.status_code != 200:
        raise GoogleOAuthError('Google would not confirm the sign in.')

    try:
        profile = response.json()
    except ValueError as exc:
        raise GoogleOAuthError('Google returned an unexpected profile.') from exc

    if not isinstance(profile, dict):
        raise GoogleOAuthError('Google returned an unexpected profile.')

    return profile


def verified_profile(profile):
    """Pull the fields this project trusts out of a userinfo payload.

    `email_verified` is the load-bearing claim. Google asserts it only when it
    has verified the address itself, which is what makes signing a user in on
    the strength of that email defensible.
    """
    email = (profile.get('email') or '').strip().lower()
    sub = (profile.get('sub') or '').strip()

    if not email or not sub:
        raise GoogleOAuthError('Google did not share an email address.')
    if profile.get('email_verified') is not True:
        raise GoogleOAuthError('That Google account has no verified email address.')

    return {
        'sub': sub,
        'email': email,
        'first_name': (profile.get('given_name') or '').strip()[:150],
        'last_name': (profile.get('family_name') or '').strip()[:150],
    }


def resolve_user(claims):
    """Find or create the `User` behind a set of verified Google claims.

    Matched on `sub` first and email second, deliberately. `sub` is the account's
    identity for its lifetime; email is a mutable label on it. Leading with
    `sub` means a returning user keeps their account even if the Google account
    has since been pointed at a different address - whereas leading with email
    would hand that account to whoever owns the new address.
    """
    for attempt in range(2):
        try:
            with transaction.atomic():
                return _resolve_once(claims)
        except IntegrityError:
            if attempt:
                raise
            # Two first-time logins for the same address raced, and the loser
            # tripped the unique constraint on `email`. Re-running finds the row
            # the winner created and links to it instead.
    raise AssertionError('unreachable')


def _resolve_once(claims):
    link_existing = getattr(settings, 'GOOGLE_LINK_EXISTING_ACCOUNTS', True)

    identity = (
        GoogleIdentity.objects
        .select_related('user')
        .filter(sub=claims['sub'])
        .first()
    )

    if identity is not None:
        return _refresh_link(identity.user, claims), False

    user = User.objects.filter(email__iexact=claims['email']).first()

    if user is not None:
        if not link_existing:
            raise GoogleAccountCollision(claims['email'])
        # An existing password account adopting a Google login is a normal
        # outcome, not an error, so there is nothing to promote: the account is
        # already known to the platform.
        return _refresh_link(user, claims), False

    # `password=None` sets an unusable password rather than an empty one, so
    # `LogInSerializer` cannot be tricked into accepting a blank password for
    # this account. The user sets a real one from Settings.
    user = User.objects.create_user(
        email=claims['email'],
        password=None,
        first_name=claims['first_name'],
        last_name=claims['last_name'],
    )

    # Same reason `SignUp` calls this: an invitation only ever goes to an address
    # with no account, so this address becoming a platform user is exactly the
    # moment the invitation it stood in for can be raised.
    promote_invitations_for(user)

    GoogleIdentity.objects.create(
        user=user,
        sub=claims['sub'],
        email=claims['email'],
    )

    return user, True


def link_identity(user, claims):
    """Attach a Google account to a user who is already signed in.

    Deliberately does not go through `resolve_user`. That function decides who
    the account *is* from Google's answer; this one already knows, because the
    caller proved it with an access token before ever leaving for Google. So the
    Google account lands on the signed-in account whatever address it reports,
    and no user is looked up or created from an email.
    """
    if GoogleIdentity.objects.filter(sub=claims['sub']).exclude(user=user).exists():
        raise GoogleIdentityTaken(claims['sub'])

    try:
        with transaction.atomic():
            return _refresh_link(user, claims)
    except IntegrityError as exc:
        # Another account linked the same Google account in between the check
        # above and the write. One Google account maps to exactly one user.
        raise GoogleIdentityTaken(claims['sub']) from exc


def _refresh_link(user, claims):
    """Fill in what Google knows and the account is missing.

    Existing names are never overwritten - the user edits those by hand in
    Settings, and a Google display name change should not silently undo it.
    """
    updates = []
    if not user.first_name and claims['first_name']:
        user.first_name = claims['first_name']
        updates.append('first_name')
    if not user.last_name and claims['last_name']:
        user.last_name = claims['last_name']
        updates.append('last_name')
    if updates:
        user.save(update_fields=updates)

    GoogleIdentity.objects.update_or_create(
        user=user,
        defaults={'sub': claims['sub'], 'email': claims['email']},
    )

    return user