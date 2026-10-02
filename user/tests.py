from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Role, User


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
