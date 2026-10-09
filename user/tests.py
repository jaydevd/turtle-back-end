import contextlib
import json
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .google_oauth import GoogleOAuthError, read_state, sign_link_state, sign_state
from .models import GoogleIdentity, Role, User


GOOGLE_SETTINGS = {
  'CLIENT_ID': 'client-id-abc',
  'CLIENT_SECRET': 'client-secret-xyz',
  'REDIRECT_URI': 'http://localhost:3000/api/user/auth/google/callback/',
  'SUCCESS_URL': 'http://localhost:3000/auth/google/callback',
  'STATE_MAX_AGE': 600,
}


def google_settings(**overrides):
  """Run a class or a test against a fully configured Google project."""
  settings = {
    'GOOGLE_OAUTH': GOOGLE_SETTINGS,
    'GOOGLE_OAUTH_ENABLED': True,
    'GOOGLE_LINK_EXISTING_ACCOUNTS': True,
  }
  settings.update(overrides)
  return override_settings(**settings)


def google_profile(**overrides):
  """A userinfo payload shaped like Google's."""
  profile = {
    'sub': '107346802199876543210',
    'email': 'grace@example.com',
    'email_verified': True,
    'given_name': 'Grace',
    'family_name': 'Hopper',
  }
  profile.update(overrides)
  return profile


@contextlib.contextmanager
def google_answers(profile=None, exchange_code='access-token', fetch_error=None):
  """Stand in for Google so no test touches the network.

  Defaults to a successful sign-in; pass `profile` to change who Google says the
  user is, or `fetch_error` to make Google fail the exchange.
  """
  with mock.patch(
    'user.google_oauth.exchange_code',
    return_value=exchange_code,
  ), mock.patch(
    'user.google_oauth.fetch_profile',
    side_effect=fetch_error,
    return_value=profile if profile is not None else google_profile(),
  ):
    yield


def query_of(location):
  return parse_qs(urlparse(location).query)


def fragment_of(location):
  return parse_qs(urlparse(location).fragment)


def payload_of(location):
  """The `{tokens, user}` blob the landing page adopts, exactly as the frontend
  reads it back."""
  return json.loads(fragment_of(location)['payload'][0])


@google_settings()
class GoogleAuthStartTestCase(TestCase):
  """Covers GET /api/user/auth/google/start/."""

  def setUp(self):
    self.url = reverse('google_start')
    self.client = APIClient()

  def test_redirects_to_google(self):
    response = self.client.get(self.url)

    self.assertEqual(response.status_code, 302)
    self.assertTrue(
      response['Location'].startswith('https://accounts.google.com/o/oauth2/v2/auth?')
    )

  def test_sends_the_configured_client_and_scope(self):
    response = self.client.get(self.url)

    query = query_of(response['Location'])
    self.assertEqual(query['client_id'], ['client-id-abc'])
    self.assertEqual(query['redirect_uri'], [GOOGLE_SETTINGS['REDIRECT_URI']])
    self.assertEqual(query['response_type'], ['code'])
    self.assertIn('email', query['scope'][0])

  def test_state_is_signed_rather_than_the_raw_destination(self):
    """The destination rides inside a signed blob so the callback cannot be
    pointed somewhere else, and so it survives the proxy hop that changes host."""
    response = self.client.get(self.url, {'next': '/habits/new'})

    state = query_of(response['Location'])['state'][0]
    self.assertNotIn('/habits/new', state)
    self.assertEqual(read_state(state)['next'], '/habits/new')

  def test_absolute_next_is_dropped(self):
    response = self.client.get(self.url, {'next': 'https://evil.example.com'})

    self.assertIsNone(read_state(query_of(response['Location'])['state'][0])['next'])

  @google_settings(GOOGLE_OAUTH_ENABLED=False)
  def test_unconfigured_server_says_so_instead_of_redirecting(self):
    response = self.client.get(self.url)

    self.assertEqual(response.status_code, 302)
    self.assertEqual(query_of(response['Location'])['error'], ['google_unavailable'])


@google_settings()
class GoogleAuthCallbackTestCase(TestCase):
  """Covers GET /api/user/auth/google/callback/.

  Each test drives the view the way Google does - a GET carrying `code` and
  `state` - with only the two outbound HTTP calls replaced.
  """

  def setUp(self):
    self.url = reverse('google_callback')
    self.client = APIClient()

  def _callback(self, **params):
    return self.client.get(self.url, params)

  def _sign_in(self, profile=None, next_url=None, **kwargs):
    """Complete a whole sign-in and return the redirect response."""
    with google_answers(profile=profile, **kwargs):
      return self._callback(code='auth-code', state=sign_state(next_url))

  def test_new_google_user_is_created_and_handed_tokens(self):
    response = self._sign_in()

    self.assertEqual(response.status_code, 302)

    user = User.objects.get(email='grace@example.com')
    self.assertEqual(user.first_name, 'Grace')
    self.assertEqual(user.last_name, 'Hopper')
    # No password, and an *unusable* one rather than a blank one, so password
    # sign-in can never be tricked into accepting this account.
    self.assertFalse(user.has_usable_password())

    payload = payload_of(response['Location'])
    self.assertTrue(RefreshToken(payload['tokens']['refresh']).access_token)

  def test_the_fragment_carries_the_same_payload_password_sign_in_returns(self):
    """One shape for every way of becoming authenticated, so the frontend has a
    single funnel and no profile call to rebuild the user from a token claim."""
    payload = payload_of(self._sign_in()['Location'])

    self.assertEqual(sorted(payload), ['tokens', 'user'])
    self.assertEqual(sorted(payload['tokens']), ['access', 'refresh'])
    self.assertEqual(payload['user']['email'], 'grace@example.com')
    self.assertEqual(payload['user']['first_name'], 'Grace')
    self.assertFalse(payload['user']['has_password'])

  def test_tokens_travel_in_the_fragment_never_the_query(self):
    """A fragment is not sent to any server, which is what keeps the tokens out
    of the proxy's and Django's access logs."""
    location = self._sign_in()['Location']

    self.assertIn('#payload=', location)
    self.assertNotIn('access=', urlparse(location).query)
    self.assertNotIn('refresh=', urlparse(location).query)

  def test_creates_a_google_identity_row(self):
    self._sign_in()

    identity = GoogleIdentity.objects.get(sub='107346802199876543210')
    self.assertEqual(identity.user.email, 'grace@example.com')
    self.assertEqual(identity.email, 'grace@example.com')

  def test_promotes_an_invitation_standing_in_for_the_new_account(self):
    """An email invitation only ever goes to an address with no account, so a
    user arriving through Google is exactly the case it was issued for."""
    from common.enums import InvitationStatus, JoinRequestStatus
    from groups.models import Group, GroupInvitation, GroupJoinRequest

    owner = User.objects.create_user(email='owner@example.com', password='pw-for-tests-9')
    group = Group.objects.create(name='Morning Crew', owner=owner)
    GroupInvitation.objects.create(
      group=group,
      invited_by=owner,
      email='grace@example.com',
      status=InvitationStatus.PENDING,
    )

    self._sign_in()

    join_request = GroupJoinRequest.objects.get(
      group=group,
      to_user__email='grace@example.com',
    )
    # Still only a request - nobody is enrolled without accepting.
    self.assertEqual(join_request.status, JoinRequestStatus.PENDING)
    self.assertFalse(group.memberships.filter(user__email='grace@example.com').exists())

  def test_returning_google_user_reuses_the_same_account(self):
    self._sign_in()
    self._sign_in()

    self.assertEqual(User.objects.filter(email='grace@example.com').count(), 1)
    self.assertEqual(GoogleIdentity.objects.count(), 1)

  def test_matching_is_on_the_sub_not_the_email(self):
    """Google's account id outlives its email address, so an account repointed
    at a new address must keep following its owner."""
    self._sign_in()

    self._sign_in(profile=google_profile(email='grace.hopper@navy.mil'))

    self.assertEqual(User.objects.count(), 1)
    self.assertEqual(GoogleIdentity.objects.get().email, 'grace.hopper@navy.mil')

  def test_existing_password_account_is_adopted_by_email(self):
    existing = User.objects.create_user(
      email='grace@example.com',
      password='pw-for-tests-9',
      first_name='Grace',
      last_name='Murray',
    )

    self._sign_in()

    self.assertEqual(User.objects.count(), 1)
    # Google is one source of a name, not the owner of it: a display-name change
    # must not silently undo what the user typed by hand.
    existing.refresh_from_db()
    self.assertEqual(existing.first_name, 'Grace')
    self.assertEqual(existing.last_name, 'Murray')
    self.assertTrue(existing.has_usable_password())

  def test_blank_names_are_filled_in_from_google(self):
    User.objects.create_user(email='grace@example.com', password='pw-for-tests-9')

    self._sign_in()

    user = User.objects.get(email='grace@example.com')
    self.assertEqual(user.first_name, 'Grace')
    self.assertEqual(user.last_name, 'Hopper')

  @google_settings(GOOGLE_LINK_EXISTING_ACCOUNTS=False)
  def test_with_linking_off_a_collision_points_at_password_sign_in(self):
    User.objects.create_user(email='grace@example.com', password='pw-for-tests-9')

    response = self._sign_in()

    self.assertEqual(query_of(response['Location'])['error'], ['account_exists'])
    self.assertEqual(User.objects.count(), 1)
    self.assertEqual(GoogleIdentity.objects.count(), 0)

  def test_a_unique_collision_is_retried_once(self):
    """Two first-time logins for the same address can overlap. The loser has to
    re-run and link to the row the winner committed rather than surface a 500."""
    from django.db import IntegrityError

    from . import google_oauth
    from .google_oauth import verified_profile

    User.objects.create_user(email='grace@example.com', password='pw-for-tests-9')

    real_resolve = google_oauth._resolve_once
    calls = []

    def collide_once(claims):
      calls.append(1)
      if len(calls) == 1:
        raise IntegrityError('duplicate key value violates unique constraint')
      return real_resolve(claims)

    with mock.patch.object(google_oauth, '_resolve_once', side_effect=collide_once):
      user, created = google_oauth.resolve_user(verified_profile(google_profile()))

    self.assertEqual(len(calls), 2)
    self.assertFalse(created)
    self.assertEqual(User.objects.count(), 1)
    self.assertEqual(GoogleIdentity.objects.get().user, user)

  def test_unverified_email_is_refused(self):
    response = self._sign_in(profile=google_profile(email_verified=False))

    self.assertEqual(query_of(response['Location'])['error'], ['sign_in_failed'])
    self.assertEqual(User.objects.count(), 0)

  def test_inactive_user_is_not_signed_in(self):
    self._sign_in()
    User.objects.filter(email='grace@example.com').update(is_active=False)

    response = self._sign_in()

    self.assertEqual(query_of(response['Location'])['error'], ['account_inactive'])

  def test_declining_consent_comes_back_as_access_denied(self):
    response = self._callback(error='access_denied')

    self.assertEqual(response.status_code, 302)
    self.assertEqual(query_of(response['Location'])['error'], ['access_denied'])
    self.assertEqual(User.objects.count(), 0)

  def test_missing_state_is_rejected(self):
    response = self._callback(code='auth-code')

    self.assertEqual(response.status_code, 400)
    self.assertIn('errors', response.data)

  def test_forged_state_is_rejected(self):
    response = self._callback(code='auth-code', state='not-a-signed-blob')

    self.assertEqual(response.status_code, 400)
    self.assertEqual(User.objects.count(), 0)

  def test_expired_state_is_rejected(self):
    expired = sign_state('/dashboard')
    with google_settings(GOOGLE_OAUTH={**GOOGLE_SETTINGS, 'STATE_MAX_AGE': -1}):
      response = self._callback(code='auth-code', state=expired)

    self.assertEqual(response.status_code, 400)

  def test_missing_code_is_rejected(self):
    response = self._callback(state=sign_state(None))

    self.assertEqual(response.status_code, 400)

  def test_a_failed_exchange_creates_nothing(self):
    response = self._sign_in(fetch_error=GoogleOAuthError('Google said no.'))

    self.assertEqual(query_of(response['Location'])['error'], ['sign_in_failed'])
    self.assertEqual(User.objects.count(), 0)

  def test_next_is_carried_through_to_the_landing_page(self):
    response = self._sign_in(next_url='/habits/new')

    self.assertEqual(query_of(response['Location'])['next'], ['/habits/new'])

  def test_next_defaults_to_the_dashboard(self):
    response = self._sign_in()

    self.assertEqual(query_of(response['Location'])['next'], ['/dashboard'])


@google_settings()
class GoogleLinkStartTestCase(TestCase):
  """Covers POST /api/user/auth/google/link/start/.

  Unlike the sign-in start, this one answers with a URL instead of redirecting:
  the caller is an XHR, and a navigation here would arrive with no Authorization
  header to say whose account is asking to be connected.
  """

  def setUp(self):
    self.url = reverse('google_link_start')
    self.client = APIClient()
    self.user = User.objects.create_user(email='ada@example.com', password='pw-for-tests-9')

  def _authenticate(self, user=None):
    user = user or self.user
    self.client.credentials(
      HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}'
    )

  def _state_from(self, response):
    return query_of(response.data['data']['url'])['state'][0]

  def test_requires_authentication(self):
    response = self.client.post(self.url, {}, format='json')

    self.assertEqual(response.status_code, 401)

  def test_returns_the_url_to_google(self):
    self._authenticate()

    response = self.client.post(self.url, {}, format='json')

    self.assertEqual(response.status_code, 200)
    self.assertTrue(
      response.data['data']['url'].startswith('https://accounts.google.com/o/oauth2/v2/auth?')
    )

  def test_state_names_the_signed_in_account(self):
    """The callback has no Authorization header of its own, so who may be linked
    has to be proven by a blob only this server can mint - and only mints in
    answer to an authenticated request."""
    self._authenticate()

    response = self.client.post(self.url, {'next': '/settings'}, format='json')
    state = read_state(self._state_from(response))

    self.assertEqual(state['link_user'], str(self.user.pk))
    self.assertEqual(state['next'], '/settings')

  def test_a_different_caller_links_their_own_account(self):
    other = User.objects.create_user(email='grace@example.com', password='pw-for-tests-9')
    self._authenticate(other)

    response = self.client.post(self.url, {}, format='json')

    self.assertEqual(read_state(self._state_from(response))['link_user'], str(other.pk))

  def test_next_defaults_to_settings(self):
    self._authenticate()

    response = self.client.post(self.url, {}, format='json')

    self.assertEqual(read_state(self._state_from(response))['next'], '/settings')

  def test_absolute_next_is_dropped(self):
    """The destination is interpolated into a redirect, so it stays on this site."""
    self._authenticate()

    response = self.client.post(self.url, {'next': 'https://evil.example.com'}, format='json')

    self.assertEqual(read_state(self._state_from(response))['next'], '/settings')

  @google_settings(GOOGLE_OAUTH_ENABLED=False)
  def test_unconfigured_server_says_so_instead_of_sending_the_browser(self):
    self._authenticate()

    response = self.client.post(self.url, {}, format='json')

    self.assertEqual(response.status_code, 400)
    self.assertIn('not configured', response.data['errors'])


@google_settings()
class GoogleLinkCallbackTestCase(TestCase):
  """Covers the connect half of GET /api/user/auth/google/callback/.

  Google only ever redirects to the one registered URI, so connecting comes back
  through the sign-in endpoint and is told apart by the signed state, which
  names the account to link instead of carrying only a destination.
  """

  def setUp(self):
    self.url = reverse('google_callback')
    self.client = APIClient()
    self.user = User.objects.create_user(
      email='ada@example.com',
      password='pw-for-tests-9',
      first_name='Ada',
    )

  def _connect(self, user=None, next_url='/settings', **kwargs):
    """Drive the callback the way Google does after a consent screen."""
    user = user or self.user
    with google_answers(**kwargs):
      return self.client.get(
        self.url,
        {'code': 'auth-code', 'state': sign_link_state(user.pk, next_url)},
      )

  def test_lands_back_on_settings_saying_connected(self):
    response = self._connect()

    self.assertEqual(response.status_code, 302)
    query = query_of(response['Location'])
    self.assertEqual(query['link'], ['connected'])
    self.assertEqual(query['next'], ['/settings'])

  def test_no_tokens_are_minted(self):
    """Nothing about the session changed, so unlike a sign in there is no
    `{tokens, user}` payload to hand over - and no credential in the URL."""
    location = self._connect()['Location']

    self.assertNotIn('payload', fragment_of(location))
    self.assertNotIn('access=', urlparse(location).query)

  def test_links_to_the_signed_in_account_whatever_google_reports(self):
    """The caller proved who they are before leaving for Google, so a Google
    account registered under a different address still lands on this account
    rather than finding or creating another one by email."""
    response = self._connect(profile=google_profile(email='ada.newton@example.org'))

    self.assertEqual(query_of(response['Location'])['link'], ['connected'])
    self.assertEqual(User.objects.count(), 1)

    identity = GoogleIdentity.objects.get()
    self.assertEqual(identity.user, self.user)
    self.assertEqual(identity.email, 'ada.newton@example.org')

  def test_blank_names_are_filled_in_from_google(self):
    self.user.first_name = ''
    self.user.save(update_fields=['first_name'])

    self._connect()

    self.user.refresh_from_db()
    self.assertEqual(self.user.first_name, 'Grace')

  def test_connecting_a_second_google_account_replaces_the_first(self):
    self._connect()
    self._connect(profile=google_profile(sub='999999999999999999999'))

    self.assertEqual(GoogleIdentity.objects.count(), 1)
    self.assertEqual(GoogleIdentity.objects.get().sub, '999999999999999999999')

  def test_reconnecting_the_same_account_is_idempotent(self):
    self._connect()
    self._connect()

    self.assertEqual(GoogleIdentity.objects.count(), 1)
    self.assertEqual(GoogleIdentity.objects.get().user, self.user)

  def test_a_google_account_already_connected_to_someone_else_is_refused(self):
    """`sub` is unique across the table: one Google account maps to exactly one
    user, so the second claim on it loses."""
    other = User.objects.create_user(email='grace@example.com', password='pw-for-tests-9')
    GoogleIdentity.objects.create(
      user=other,
      sub='107346802199876543210',
      email='grace@example.com',
    )

    response = self._connect()

    query = query_of(response['Location'])
    self.assertEqual(query['link'], ['failed'])
    self.assertEqual(query['reason'], ['google_taken'])
    self.assertFalse(GoogleIdentity.objects.filter(user=self.user).exists())
    self.assertEqual(GoogleIdentity.objects.get().user, other)

  def test_declining_consent_reports_access_denied(self):
    response = self.client.get(
      self.url,
      {'error': 'access_denied', 'state': sign_link_state(self.user.pk, '/settings')},
    )

    query = query_of(response['Location'])
    self.assertEqual(query['link'], ['failed'])
    self.assertEqual(query['reason'], ['access_denied'])
    self.assertEqual(GoogleIdentity.objects.count(), 0)

  def test_a_failed_exchange_creates_nothing(self):
    response = self._connect(fetch_error=GoogleOAuthError('Google said no.'))

    query = query_of(response['Location'])
    self.assertEqual(query['link'], ['failed'])
    self.assertEqual(query['reason'], ['link_failed'])
    self.assertEqual(GoogleIdentity.objects.count(), 0)

  def test_a_missing_code_creates_nothing(self):
    response = self.client.get(self.url, {'state': sign_link_state(self.user.pk, '/settings')})

    query = query_of(response['Location'])
    self.assertEqual(query['link'], ['failed'])
    self.assertEqual(query['reason'], ['link_failed'])
    self.assertEqual(GoogleIdentity.objects.count(), 0)

  def test_an_account_that_no_longer_exists_is_refused(self):
    """Signed out or removed while Google had the tab: the state still verifies,
    but there is no longer anyone it may attach to."""
    state = sign_link_state(self.user.pk, '/settings')
    self.user.delete()

    with google_answers():
      response = self.client.get(self.url, {'code': 'auth-code', 'state': state})

    query = query_of(response['Location'])
    self.assertEqual(query['link'], ['failed'])
    self.assertEqual(query['reason'], ['link_failed'])
    self.assertEqual(GoogleIdentity.objects.count(), 0)

  def test_a_forged_state_is_rejected(self):
    response = self.client.get(self.url, {'code': 'auth-code', 'state': 'not-a-signed-blob'})

    self.assertEqual(response.status_code, 400)
    self.assertEqual(GoogleIdentity.objects.count(), 0)


class GoogleDisconnectTestCase(TestCase):
  """Covers DELETE /api/user/auth/google/disconnect/."""

  def setUp(self):
    self.url = reverse('google_disconnect')
    self.client = APIClient()
    self.user = User.objects.create_user(email='ada@example.com', password='pw-for-tests-9')
    self.identity = GoogleIdentity.objects.create(
      user=self.user,
      sub='107346802199876543210',
      email='ada@gmail.com',
    )

  def _authenticate(self, user=None):
    user = user or self.user
    self.client.credentials(
      HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}'
    )

  def test_removes_the_link_and_reports_it(self):
    """The response is the refreshed user, so the client learns
    `google_connected` has flipped without a second profile call."""
    self._authenticate()

    response = self.client.delete(self.url)

    self.assertEqual(response.status_code, 200)
    self.assertFalse(response.data['data']['google_connected'])
    self.assertIsNone(response.data['data']['google_email'])
    self.assertFalse(GoogleIdentity.objects.filter(pk=self.identity.pk).exists())

  def test_only_the_callers_own_link_can_be_removed(self):
    other = User.objects.create_user(email='grace@example.com', password='pw-for-tests-9')
    other_identity = GoogleIdentity.objects.create(
      user=other,
      sub='999999999999999999999',
      email='grace@example.com',
    )
    self._authenticate()

    self.client.delete(self.url)

    self.assertTrue(GoogleIdentity.objects.filter(pk=other_identity.pk).exists())

  def test_password_sign_in_still_works_afterwards(self):
    self._authenticate()
    self.client.delete(self.url)

    self.client.credentials()
    response = self.client.post(
      reverse('log_in'),
      {'email': 'ada@example.com', 'password': 'pw-for-tests-9'},
      format='json',
    )

    self.assertEqual(response.status_code, 200)

  def test_an_account_that_would_be_locked_out_may_not_disconnect(self):
    """An account with no password has no other way back in, so the link is the
    only key it owns."""
    google_user = User.objects.create_user(email='grace@example.com', password=None)
    GoogleIdentity.objects.create(
      user=google_user,
      sub='999999999999999999999',
      email='grace@example.com',
    )
    self._authenticate(google_user)

    response = self.client.delete(self.url)

    self.assertEqual(response.status_code, 400)
    self.assertIn('Set a password first', response.data['errors'])
    self.assertTrue(GoogleIdentity.objects.filter(user=google_user).exists())

  def test_nothing_connected_is_a_bad_request(self):
    self._authenticate(User.objects.create_user(email='ada@newton.ac.uk', password='pw-for-tests-9'))

    response = self.client.delete(self.url)

    self.assertEqual(response.status_code, 400)
    self.assertIn('No Google account is connected', response.data['errors'])

  def test_requires_authentication(self):
    response = self.client.delete(self.url)

    self.assertEqual(response.status_code, 401)


class SetPasswordTestCase(TestCase):
  """Covers POST /api/user/auth/set-password/."""

  def setUp(self):
    self.url = reverse('set_password')
    self.client = APIClient()

    # A Google account arrives with no usable password at all.
    self.google_user = User.objects.create_user(
      email='grace@example.com',
      password=None,
      first_name='Grace',
    )
    self.password_user = User.objects.create_user(
      email='ada@example.com',
      password='pw-for-tests-9',
    )

  def _authenticate(self, user):
    self.client.credentials(
      HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}'
    )

  def test_account_without_a_password_sets_one_without_confirming(self):
    """There is nothing to confirm, and the access token already proves who is
    asking."""
    self._authenticate(self.google_user)

    response = self.client.post(
      self.url,
      {'new_password': 'compilers-1979'},
      format='json',
    )

    self.assertEqual(response.status_code, 200)
    self.assertTrue(response.data['data']['has_password'])
    self.google_user.refresh_from_db()
    self.assertTrue(self.google_user.check_password('compilers-1979'))

  def test_existing_account_must_confirm_its_password(self):
    self._authenticate(self.password_user)

    response = self.client.post(
      self.url,
      {'new_password': 'coBOL-1959'},
      format='json',
    )

    self.assertEqual(response.status_code, 411)
    self.assertIn('current_password', response.data['errors'])
    self.password_user.refresh_from_db()
    self.assertTrue(self.password_user.check_password('pw-for-tests-9'))

  def test_wrong_current_password_is_rejected(self):
    self._authenticate(self.password_user)

    response = self.client.post(
      self.url,
      {'new_password': 'coBOL-1959', 'current_password': 'not-my-password'},
      format='json',
    )

    self.assertEqual(response.status_code, 411)
    self.assertIn('current_password', response.data['errors'])

  def test_correct_current_password_replaces_it(self):
    self._authenticate(self.password_user)

    response = self.client.post(
      self.url,
      {'new_password': 'coBOL-1959', 'current_password': 'pw-for-tests-9'},
      format='json',
    )

    self.assertEqual(response.status_code, 200)
    self.password_user.refresh_from_db()
    self.assertTrue(self.password_user.check_password('coBOL-1959'))

  def test_weak_password_is_rejected_by_the_shared_validators(self):
    self._authenticate(self.google_user)

    response = self.client.post(self.url, {'new_password': 'abc'}, format='json')

    self.assertEqual(response.status_code, 411)
    self.assertIn('new_password', response.data['errors'])

  def test_password_never_comes_back(self):
    self._authenticate(self.google_user)

    response = self.client.post(
      self.url,
      {'new_password': 'compilers-1979'},
      format='json',
    )

    self.assertNotIn('password', response.data['data'])

  def test_bumps_updated_at(self):
    self._authenticate(self.google_user)
    before = self.google_user.updated_at

    self.client.post(self.url, {'new_password': 'compilers-1979'}, format='json')

    self.google_user.refresh_from_db()
    self.assertGreaterEqual(self.google_user.updated_at, before)

  def test_a_google_account_can_now_sign_in_with_a_password(self):
    """The whole point of the endpoint: an account that had none gets one."""
    self._authenticate(self.google_user)
    self.client.post(self.url, {'new_password': 'compilers-1979'}, format='json')

    self.client.credentials()
    response = self.client.post(
      reverse('log_in'),
      {'email': 'grace@example.com', 'password': 'compilers-1979'},
      format='json',
    )

    self.assertEqual(response.status_code, 200)
    self.assertIn('access', response.data['data']['tokens'])

  def test_requires_authentication(self):
    response = self.client.post(
      self.url,
      {'new_password': 'compilers-1979'},
      format='json',
    )

    self.assertEqual(response.status_code, 401)


class ProfileTestCase(TestCase):
  """Covers GET/PATCH /api/user/profile/.

  The endpoint resolves the user from the access token, so every test
  authenticates explicitly rather than passing a pk.
  """

  def setUp(self):
    self.url = reverse('profile')
    self.client = APIClient()

    self.user = self._create_user(
      email='ada@example.com',
      first_name='Ada',
      last_name='Lovelace',
    )

  def _create_user(self, email, first_name='', last_name=''):
    return User.objects.create_user(
      email=email,
      password='correct-horse-battery',
      first_name=first_name,
      last_name=last_name,
    )

  def _authenticate(self, user):
    self.client.credentials(
      HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}'
    )

  def test_get_returns_callers_own_profile(self):
    self._authenticate(self.user)

    response = self.client.get(self.url)

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.data['status'], 200)
    self.assertEqual(response.data['data']['email'], 'ada@example.com')
    self.assertEqual(response.data['data']['first_name'], 'Ada')
    self.assertEqual(response.data['data']['last_name'], 'Lovelace')

  def test_get_does_not_leak_password(self):
    self._authenticate(self.user)

    response = self.client.get(self.url)

    self.assertNotIn('password', response.data['data'])

  def test_get_reports_whether_a_password_is_set(self):
    """Settings keys the "set a password" form off this."""
    self._authenticate(self.user)
    password_account = self.client.get(self.url).data['data']['has_password']

    self.google_user = User.objects.create_user(
      email='grace@example.com',
      password=None,
    )
    self._authenticate(self.google_user)
    google_account = self.client.get(self.url).data['data']['has_password']

    self.assertTrue(password_account)
    self.assertFalse(google_account)

  def test_get_reports_whether_google_is_connected(self):
    """Settings keys the connect/disconnect choice off this."""
    self._authenticate(self.user)
    unconnected = self.client.get(self.url).data['data']

    GoogleIdentity.objects.create(
      user=self.user,
      sub='107346802199876543210',
      email='ada@gmail.com',
    )
    connected = self.client.get(self.url).data['data']

    self.assertFalse(unconnected['google_connected'])
    self.assertIsNone(unconnected['google_email'])
    self.assertTrue(connected['google_connected'])
    self.assertEqual(connected['google_email'], 'ada@gmail.com')

  def test_patch_updates_both_names(self):
    self._authenticate(self.user)

    response = self.client.patch(
      self.url,
      {'first_name': 'Grace', 'last_name': 'Hopper'},
      format='json',
    )

    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.data['message'], 'data updated successfully')

    self.user.refresh_from_db()
    self.assertEqual(self.user.first_name, 'Grace')
    self.assertEqual(self.user.last_name, 'Hopper')

  def test_patch_leaves_omitted_field_untouched(self):
    self._authenticate(self.user)

    self.client.patch(self.url, {'last_name': 'Byron'}, format='json')

    self.user.refresh_from_db()
    self.assertEqual(self.user.first_name, 'Ada')
    self.assertEqual(self.user.last_name, 'Byron')

  def test_patch_allows_clearing_a_name(self):
    self._authenticate(self.user)

    response = self.client.patch(self.url, {'first_name': ''}, format='json')

    self.assertEqual(response.status_code, 200)
    self.user.refresh_from_db()
    self.assertEqual(self.user.first_name, '')

  def test_patch_trims_whitespace(self):
    self._authenticate(self.user)

    self.client.patch(self.url, {'first_name': '  Ada  '}, format='json')

    self.user.refresh_from_db()
    self.assertEqual(self.user.first_name, 'Ada')

  def test_patch_rejects_oversize_name(self):
    self._authenticate(self.user)

    response = self.client.patch(
      self.url,
      {'first_name': 'x' * 151},
      format='json',
    )

    self.assertEqual(response.status_code, 411)
    self.assertIn('first_name', response.data['errors'])

    self.user.refresh_from_db()
    self.assertEqual(self.user.first_name, 'Ada')

  def test_patch_bumps_updated_at(self):
    self._authenticate(self.user)
    before = self.user.updated_at

    self.client.patch(self.url, {'first_name': 'Grace'}, format='json')

    self.user.refresh_from_db()
    self.assertGreaterEqual(self.user.updated_at, before)

  def test_patch_cannot_change_privileged_fields(self):
    """The allowlist in ProfileUpdateSerializer is the authorization
    boundary: privilege fields must survive even if sent."""
    self._authenticate(self.user)

    response = self.client.patch(
      self.url,
      {
        'first_name': 'Grace',
        'email': 'attacker@example.com',
        'is_staff': True,
        'user_role': Role.ADMIN,
        'is_deleted': True,
      },
      format='json',
    )

    self.assertEqual(response.status_code, 200)

    self.user.refresh_from_db()
    self.assertEqual(self.user.email, 'ada@example.com')
    self.assertFalse(self.user.is_staff)
    self.assertEqual(self.user.user_role, Role.USER)
    self.assertFalse(self.user.is_deleted)

  def test_get_requires_authentication(self):
    response = self.client.get(self.url)

    self.assertEqual(response.status_code, 401)

  def test_patch_requires_authentication(self):
    response = self.client.patch(self.url, {'first_name': 'Grace'}, format='json')

    self.assertEqual(response.status_code, 401)