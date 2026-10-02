from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from .serializers import *
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model
from common.responses import success_response, error_response
from common.constants import HTTP_ERROR_CODES, RESPONSE_MESSAGES
from common.utils import get_unix_timestamp
from groups.services import promote_invitations_for
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
