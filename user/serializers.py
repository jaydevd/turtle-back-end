from rest_framework import serializers
from .models import User
from django.contrib.auth import get_user_model, authenticate
from rest_framework_simplejwt.tokens import RefreshToken
import traceback

User = get_user_model()

class UserSerializer(serializers.ModelSerializer):
  class Meta:
    model = User
    fields = [
      'id',
      'email',
      'first_name',
      'last_name',
      'user_role',
      'is_deleted',
      'created_at'
    ]

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
