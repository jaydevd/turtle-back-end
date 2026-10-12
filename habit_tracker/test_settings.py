"""Settings for the test suite.

`habit_tracker.settings` points at the Neon PostgreSQL instance, which means a
plain `manage.py test` would try to create `test_neondb` against the production
database. Nothing in this project uses PostgreSQL-specific features, so the
suite runs against an in-memory SQLite database instead:

    python manage.py test --settings=habit_tracker.test_settings
"""

from .settings import *  # noqa: F401,F403

DATABASES = {
  'default': {
    'ENGINE': 'django.db.backends.sqlite3',
    'NAME': ':memory:',
  }
}

PASSWORD_HASHERS = [
  'django.contrib.auth.hashers.MD5PasswordHasher',
]

# Never let the suite reach a real mail server. The settings default is SMTP, and
# a few flows (password reset, group invitations) send mail as part of their
# normal work, so the in-memory outbox is what a test should observe.
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'