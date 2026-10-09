from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from .serializers import *
from . import google_oauth
from .models import GoogleIdentity
from rest_framework_simplejwt.tokens import RefreshToken
from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponseRedirect
from common.responses import success_response, error_response
from common.constants import HTTP_ERROR_CODES, RESPONSE_MESSAGES
from common.utils import get_unix_timestamp
from groups.services import promote_invitations_for
from urllib.parse import urlencode
import json
import traceback

User = get_user_model()


def _auth_payload(user, refresh, message):
  return {
    "tokens": {
      "access": str(refresh.access_token),
      "refresh": str(refresh),
    },
    "user": UserSerializer(user).data,
  }


def _google_landing_url(**params):
  """The frontend page that receives every outcome of a Google sign in.

  Built from configuration rather than from `request.get_host()`: the callback
  arrives through the Next.js proxy, so the host Django sees is its own and not
  the one the browser is on. Returns None when nothing is configured, which is
  the only case where the caller falls back to a JSON envelope.
  """
  base = settings.GOOGLE_OAUTH.get('SUCCESS_URL')
  if not base:
    return None
  return f"{base}?{urlencode(params)}"


def _google_success(user, next_url):
  """Hand a freshly minted session to the landing page.

  The whole `{tokens, user}` payload rides in the URL fragment rather than the
  query string. A fragment is never sent to a server, so the tokens stay out of
  the Next proxy's access log, out of Django's, and out of the Referer header of
  anything the landing page loads afterwards. It also means the frontend adopts
  the session through exactly the same path as a password sign-in, with no
  profile call to reconstruct the user from a token claim.

  The landing page scrubs the fragment with `history.replaceState` before it
  reads anything else.
  """
  location = _google_landing_url(status='ok', next=next_url or '/dashboard')
  if location is None:
    # Only reachable by hitting the callback with no landing page configured,
    # which means an API client rather than a browser.
    return error_response(
      status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
      message=RESPONSE_MESSAGES['BAD_REQUEST'],
      errors='Google sign in is not configured on this server.',
    )

  refresh = RefreshToken.for_user(user)
  payload = json.dumps(_auth_payload(user, refresh, ''), separators=(',', ':'))
  return HttpResponseRedirect(f"{location}#{urlencode({'payload': payload})}")


def _google_failure(reason, **extra):
  """Send the browser back to the frontend with a reason it can show.

  Falls back to a JSON envelope only when no landing page is configured, which
  is also the only way an API client rather than a browser reaches here.
  """
  location = _google_landing_url(error=reason, **extra)
  if location is not None:
    return HttpResponseRedirect(location)

  return error_response(
    status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
    message=RESPONSE_MESSAGES['BAD_REQUEST'],
    errors=reason,
  )


def _google_unavailable():
  """No Google project on this server, so there is nowhere to send the browser."""
  if settings.GOOGLE_OAUTH.get('SUCCESS_URL'):
    return HttpResponseRedirect(_google_landing_url(error='google_unavailable'))

  return error_response(
    status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
    message=RESPONSE_MESSAGES['BAD_REQUEST'],
    errors='Google sign in is not configured on this server.',
  )


def _google_link_success(next_url):
  """Tell Settings the connection was made and send the browser back there.

  No tokens ride along, unlike a sign in: the browser already holds a session
  and nothing about it changed. All the landing page needs is the yes.
  """
  location = _google_landing_url(link='connected', next=next_url)
  if location is None:
    return error_response(
      status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
      message=RESPONSE_MESSAGES['BAD_REQUEST'],
      errors='Google connection is not configured on this server.',
    )

  return HttpResponseRedirect(location)


def _google_link_failure(reason, next_url):
  """Send the browser back to Settings with a reason it can show.

  The landing page forwards this to the page the user was on, so a cancelled or
  failed connection reads as a message next to the button that started it rather
  than as a dead end.
  """
  location = _google_landing_url(link='failed', reason=reason, next=next_url)
  if location is not None:
    return HttpResponseRedirect(location)

  return error_response(
    status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
    message=RESPONSE_MESSAGES['BAD_REQUEST'],
    errors=reason,
  )


# Create your views here.
class SignUp(APIView):

  permission_classes = [AllowAny]

  def post(self, request):

    serializer = SignUpSerializer(data=request.data)
    print("serializer initialized")

    if not serializer.is_valid():
      return error_response(
        status_code=HTTP_ERROR_CODES['VALIDATION_ERROR'],
        message=RESPONSE_MESSAGES['VALIDATION_ERROR'],
        errors=serializer.errors,
      )

    print("data validation successfull")
    print("creating the data entry in database")

    try:

      user = serializer.save()

      print('user: ', user)
      print("data saved in database")

      refresh = RefreshToken.for_user(user)
      print('refresh: ', refresh)

      # An email invitation is only ever issued to an address with no account, so
      # reaching this point means the address has just become a platform user and
      # the request the invitation stood in for can finally be made. Nobody is
      # joined here - each invitation becomes a PENDING join request the
      # newcomer still has to accept.
      promote_invitations_for(user)

      return success_response(
        status_code=HTTP_ERROR_CODES['SUCCESS'],
        message='user signed up successfully',
        data=_auth_payload(user, refresh, 'user signed up successfully'),
      )

    except Exception:
      print("Error when registering the user.")
      traceback.print_exc()
      return error_response(
        status_code=HTTP_ERROR_CODES['SERVER_ERROR'],
        message=RESPONSE_MESSAGES['SERVER_ERROR'],
        errors='Unable to create the account.',
      )


class LogIn(APIView):

  permission_classes = [AllowAny]

  def post(self, request):
    serializer = LogInSerializer(data=request.data)

    if not serializer.is_valid():
      return error_response(
        status_code=HTTP_ERROR_CODES['VALIDATION_ERROR'],
        message=RESPONSE_MESSAGES['VALIDATION_ERROR'],
        errors=serializer.errors,
      )

    try:
      user = serializer.validated_data['user']
      refresh = RefreshToken.for_user(user)
    except Exception:
      print("Server Error from Log in view")
      traceback.print_exc()
      return error_response(
        status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
        message=RESPONSE_MESSAGES['BAD_REQUEST'],
        errors='Invalid email or password.',
      )

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message="Log in successful",
      data=_auth_payload(user, refresh, "Log in successful"),
    )


class LogOut(APIView):
  permission_classes = [IsAuthenticated]

  def post(self, request):
    print('request.data: ', request.data)
    serializer = LogOutSerializer(data=request.data)

    if not serializer.is_valid():
      return error_response(
        status_code=HTTP_ERROR_CODES['VALIDATION_ERROR'],
        message=RESPONSE_MESSAGES['VALIDATION_ERROR'],
        errors=serializer.errors,
      )

    try:
      refresh = RefreshToken(serializer.validated_data['refresh'])
      print('refresh: ', refresh)
    except Exception:
      print("Error occured while blacklisting the user token")
      traceback.print_exc()
      return error_response(
        status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
        message=RESPONSE_MESSAGES['BAD_REQUEST'],
        errors='Something went wrong',
      )

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message="user logged out successfully",
      data=None,
    )


class GoogleAuthStart(APIView):
  """Hand the browser to Google to collect consent.

  The only entry point is a navigation, never an XHR: the answer is a redirect,
  so the client cannot read it and does not need to.
  """

  permission_classes = [AllowAny]

  def get(self, request):
    next_url = google_oauth.safe_next(request.query_params.get('next'))

    if not google_oauth.is_enabled():
      return _google_unavailable()

    # `state` carries the destination the user was headed for and proves the
    # callback belongs to a start we issued.
    state = google_oauth.sign_state(next_url)

    return HttpResponseRedirect(google_oauth.authorization_url(state))


class GoogleAuthCallback(APIView):
  """Complete the exchange and hand the session to the frontend.

  Google redirects here, so this view answers with redirects rather than the
  usual envelope. It also takes the return leg of a connect from Settings -
  there is only one registered redirect URI - which is told apart by its state
  and answers with an outcome instead of a session.
  """

  permission_classes = [AllowAny]

  def get(self, request):
    params = request.query_params
    state = google_oauth.read_state(params.get('state'))

    # A connect from Settings comes back through this same endpoint - Google
    # only ever redirects to the one registered URI - and answers in a different
    # currency: it has no session to hand over, only a yes or no for Settings.
    if state is not None and state.get('link_user'):
      return self._complete_link(params, state)

    # Pressing "Cancel" on the consent screen is an error redirect with no code.
    if params.get('error'):
      return _google_failure('access_denied')

    if state is None:
      return error_response(
        status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
        message=RESPONSE_MESSAGES['BAD_REQUEST'],
        errors='That sign in request has expired. Please try again.',
      )

    code = params.get('code')
    if not code:
      return error_response(
        status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
        message=RESPONSE_MESSAGES['BAD_REQUEST'],
        errors='Google did not return an authorization code.',
      )

    try:
      access_token = google_oauth.exchange_code(code)
      claims = google_oauth.verified_profile(google_oauth.fetch_profile(access_token))
      user, created = google_oauth.resolve_user(claims)
    except google_oauth.GoogleAccountCollision:
      # GOOGLE_LINK_EXISTING_ACCOUNTS is off, so this address already belongs to
      # a password account that Google has no proof of.
      return _google_failure('account_exists')
    except google_oauth.GoogleOAuthError:
      traceback.print_exc()
      return _google_failure('sign_in_failed')

    # `is_active` is the only gate password sign-in applies, so the two paths
    # cannot disagree about who gets in. `is_deleted` is a declared flag that
    # nothing in the project enforces.
    if not user.is_active:
      return _google_failure('account_inactive')

    print(f"google sign in: user={user.email} created={created}")

    return _google_success(user, google_oauth.safe_next(state.get('next')))

  def _complete_link(self, params, state):
    """Second half of a connect started from Settings.

    The signed state names the account to link to, so this path never looks a
    user up by email and never creates one: it either attaches the Google
    account to the caller or explains why it could not.
    """
    next_url = google_oauth.safe_next(state.get('next')) or '/settings'

    # Pressing "Cancel" on the consent screen is an error redirect with no code.
    if params.get('error'):
      return _google_link_failure('access_denied', next_url)

    if not params.get('code'):
      return _google_link_failure('link_failed', next_url)

    user = User.objects.filter(pk=state['link_user'], is_active=True).first()
    if user is None:
      # The account was signed out or deactivated while Google had the tab.
      return _google_link_failure('link_failed', next_url)

    try:
      access_token = google_oauth.exchange_code(params.get('code'))
      claims = google_oauth.verified_profile(google_oauth.fetch_profile(access_token))
      google_oauth.link_identity(user, claims)
    except google_oauth.GoogleIdentityTaken:
      return _google_link_failure('google_taken', next_url)
    except google_oauth.GoogleOAuthError:
      traceback.print_exc()
      return _google_link_failure('link_failed', next_url)

    print(f"google connect: user={user.email} sub={claims['sub']}")

    return _google_link_success(next_url)


class GoogleLinkStart(APIView):
  """Hand the browser to Google to collect consent for this signed-in account.

  Answers with a URL instead of redirecting, because the caller is an XHR: a
  navigation here would arrive with no Authorization header, and the endpoint
  would have no idea whose account to link.
  """

  permission_classes = [IsAuthenticated]

  def post(self, request):
    if not google_oauth.is_enabled():
      reason = 'Google sign in is not configured on this server.'
      return error_response(
        status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
        message=reason,
        errors=reason,
      )

    raw_next = request.data.get('next') if hasattr(request.data, 'get') else None
    next_url = google_oauth.safe_next(raw_next) or '/settings'
    state = google_oauth.sign_link_state(request.user.pk, next_url)

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message='google authorization url ready',
      data={'url': google_oauth.authorization_url(state)},
    )


class GoogleDisconnect(APIView):
  """Remove Google from the caller's own account.

  Operates on `request.user`, so there is no cross-user access to authorise: the
  only row that can be deleted is the one belonging to whoever holds the token.
  """

  permission_classes = [IsAuthenticated]

  def delete(self, request):
    user = request.user
    identity = GoogleIdentity.objects.filter(user=user).first()

    if identity is None:
      reason = 'No Google account is connected to this account.'
      return error_response(
        status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
        message=reason,
        errors=reason,
      )

    # This row may be the only way the account can be signed into at all, so
    # dropping it would lock the user out of their own account.
    if not user.has_usable_password():
      reason = 'Set a password first, otherwise there would be no way to sign in.'
      return error_response(
        status_code=HTTP_ERROR_CODES['BAD_REQUEST'],
        message=reason,
        errors=reason,
      )

    try:
      identity.delete()
      user.updated_at = get_unix_timestamp()
      user.save(update_fields=['updated_at'])
    except Exception:
      print("Error when disconnecting the Google account.")
      traceback.print_exc()
      return error_response(
        status_code=HTTP_ERROR_CODES['SERVER_ERROR'],
        message=RESPONSE_MESSAGES['SERVER_ERROR'],
        errors='Unable to disconnect the Google account.',
      )

    # The response is the refreshed user, so the client learns
    # `google_connected` has flipped without a second profile call.
    user.refresh_from_db()
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message='Google account disconnected',
      data=UserSerializer(user).data,
    )



class SetPassword(APIView):
  """Set or replace the caller's own password.

  An account created through Google arrives here with an unusable password, so
  this is the only way it can ever sign in with a password too.
  """

  permission_classes = [IsAuthenticated]

  def post(self, request):
    serializer = SetPasswordSerializer(data=request.data, context={'request': request})

    if not serializer.is_valid():
      return error_response(
        status_code=HTTP_ERROR_CODES['VALIDATION_ERROR'],
        message=RESPONSE_MESSAGES['VALIDATION_ERROR'],
        errors=serializer.errors,
      )

    user = request.user

    try:
      # set_password writes the hash; the timestamp is bumped alongside it so the
      # profile's "last changed" stays honest, matching the PATCH profile path.
      user.set_password(serializer.validated_data['new_password'])
      user.updated_at = get_unix_timestamp()
      user.save(update_fields=['password', 'updated_at'])
    except Exception:
      print("Error when setting the user password.")
      traceback.print_exc()
      return error_response(
        status_code=HTTP_ERROR_CODES['SERVER_ERROR'],
        message=RESPONSE_MESSAGES['SERVER_ERROR'],
        errors='Unable to set the password.',
      )

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message='password updated successfully',
      data=UserSerializer(user).data,
    )


class Profile(APIView):
  """Read and update the authenticated user's own profile.

  Operates on `request.user` rather than a URL-supplied pk, so there is no
  cross-user access to authorise.
  """

  permission_classes = [IsAuthenticated]

  def get(self, request):
    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['SUCCESS'],
      data=UserSerializer(request.user).data,
    )

  def patch(self, request):
    user = request.user
    serializer = ProfileUpdateSerializer(user, data=request.data, partial=True)

    if not serializer.is_valid():
      return error_response(
        status_code=HTTP_ERROR_CODES['VALIDATION_ERROR'],
        message=RESPONSE_MESSAGES['VALIDATION_ERROR'],
        errors=serializer.errors,
      )

    try:
      serializer.save(updated_at=get_unix_timestamp())
    except Exception:
      print("Error when updating the user profile.")
      traceback.print_exc()
      return error_response(
        status_code=HTTP_ERROR_CODES['SERVER_ERROR'],
        message=RESPONSE_MESSAGES['SERVER_ERROR'],
        errors='Unable to update the profile.',
      )

    user.refresh_from_db()

    return success_response(
      status_code=HTTP_ERROR_CODES['SUCCESS'],
      message=RESPONSE_MESSAGES['DATA_UPDATED'],
      data=UserSerializer(user).data,
    )
