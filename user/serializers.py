from rest_framework import serializers
from .models import GoogleIdentity, User
from django.contrib.auth import get_user_model, authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework_simplejwt.tokens import RefreshToken
import traceback

User = get_user_model()

class UserSerializer(serializers.ModelSerializer):
  """The caller's own account.

  Only ever serialized for the authenticated user - the group serializers carry
  their own member shape - so `has_password` is safe to expose here. It is what
  lets Settings show a "set a password" form to an account created through
  Google, which has an unusable password until one is set. `google_connected`
  and `google_email` are the other half of that screen: they decide between
  offering a "connect to Google" action and offering to disconnect one.
  """

  has_password = serializers.SerializerMethodField()
  google_connected = serializers.SerializerMethodField()
  google_email = serializers.SerializerMethodField()

  class Meta:
    model = User
    fields = [
      'id',
      'email',
      'first_name',
      'last_name',
      'user_role',
      'is_deleted',
      'has_password',
      'google_connected',
      'google_email',
      'created_at'
    ]

  def get_has_password(self, obj):
    return obj.has_usable_password()

  def get_google_connected(self, obj):
    return self._google_identity(obj) is not None

  def get_google_email(self, obj):
    identity = self._google_identity(obj)
    return identity.email if identity is not None else None

  @staticmethod
  def _google_identity(obj):
    """The account's Google link, or None.

    The forward one-to-one raises `RelatedObjectDoesNotExist` - an
    AttributeError subclass - for an account that has never connected, which
    `getattr` folds into None. Django caches the miss on the instance, so the
    two fields above cost one query between them.
    """
    return getattr(obj, 'google_identity', None)

class ProfileUpdateSerializer(serializers.ModelSerializer):
  """Write shape for the caller's own profile.

  Deliberately separate from UserSerializer: this is an allowlist of the two
  fields a user may change on themselves. Fields like user_role, is_deleted and
  email are absent, so they cannot be mass-assigned through this endpoint.
  """

  class Meta:
    model = User
    fields = ['first_name', 'last_name']
    extra_kwargs = {
      'first_name': {'required': False, 'allow_blank': True},
      'last_name': {'required': False, 'allow_blank': True},
    }

class SignUpSerializer(serializers.ModelSerializer):

  class Meta:
    model = User
    fields = ['email', 'password', 'first_name', 'last_name']

  def create(self, validated_data):
    return User.objects.create_user(**validated_data)

class LogInSerializer(serializers.Serializer):

  email = serializers.EmailField()
  password = serializers.CharField(write_only=True, trim_whitespace=False)
  default_error_messages = {
    'invalid_credentials': 'Invalid email or password.',
    'inactive': 'This account is inactive.',
  }

  def validate(self, attrs):
    email = User.objects.normalize_email(attrs["email"]).lower()
    password = attrs["password"]

    user = authenticate(
        request=self.context.get("request"),
        username=email,
        password=password,
    )

    print('user: ', user)
    
    if not user:
        self.fail("invalid_credentials")
    if not user.is_active:
        self.fail("inactive")

    attrs["user"] = user

    return attrs

class LogOutSerializer(serializers.Serializer):
  refresh = serializers.CharField()

  def validate_refresh(self, value):
    try:
      print("serializer - validating refresh token from the serializer class.")
      RefreshToken(value)
      print("serializer - token validation completed.")
      return value
    except:
      print("Unable to revoke the user token")
      traceback.print_exc()
      return value


def validate_password_strength(password, user=None):
  """Run a candidate password through the validators in AUTH_PASSWORD_VALIDATORS.

  Returns the list of human-readable problems, which DRF renders for us. Used by
  `SetPasswordSerializer` so a password set after signing in with Google is held
  to exactly the same rules as one set at sign-up, rather than a looser copy.
  The user is passed so the similarity check has their email and names to compare
  against.
  """
  try:
    validate_password(password, user=user)
  except DjangoValidationError as exc:
    return list(exc.messages)
  return []


class SetPasswordSerializer(serializers.Serializer):
  """Set or replace the caller's own password.

  An account created through Google has no usable password at all, so there is
  nothing to confirm and `current_password` is optional. An account that already
  has one must prove it first, otherwise any stolen access token could take the
  password over permanently - the token would outlive the session it was stolen
  in.
  """

  new_password = serializers.CharField(write_only=True, trim_whitespace=False)
  current_password = serializers.CharField(
    write_only=True,
    required=False,
    allow_blank=True,
    default='',
  )

  default_error_messages = {
    'current_password_required': 'Enter your current password.',
    'current_password_incorrect': 'That is not your current password.',
  }

  def _reject_current_password(self, code):
    """Keyed to the field so the client can put the message on the input rather
    than in a form-wide banner."""
    raise serializers.ValidationError({
      'current_password': [self.error_messages[code]],
    })

  def validate(self, attrs):
    user = self.context['request'].user

    if user.has_usable_password():
      if not attrs.get('current_password'):
        self._reject_current_password('current_password_required')
      if not user.check_password(attrs['current_password']):
        self._reject_current_password('current_password_incorrect')

    problems = validate_password_strength(attrs['new_password'], user=user)
    if problems:
      raise serializers.ValidationError({'new_password': problems})

    return attrs
