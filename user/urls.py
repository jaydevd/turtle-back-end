from django.urls import path
from .views import *

urlpatterns = [
  path('auth/sign-up/', SignUp.as_view(), name='sign_up'),
  path('auth/log-in/', LogIn.as_view(), name='log_in'),
  path('auth/log-out/', LogOut.as_view(), name='log_out'),
  path('auth/set-password/', SetPassword.as_view(), name='set_password'),
  # Both of these answer with redirects rather than a JSON envelope, so they
  # only ever work as a browser navigation.
  path('auth/google/start/', GoogleAuthStart.as_view(), name='google_start'),
  path('auth/google/callback/', GoogleAuthCallback.as_view(), name='google_callback'),
  # Connecting hands back a URL for the caller to navigate to, because the
  # navigation itself carries no Authorization header to say who is asking.
  path('auth/google/link/start/', GoogleLinkStart.as_view(), name='google_link_start'),
  path('auth/google/disconnect/', GoogleDisconnect.as_view(), name='google_disconnect'),
  path('profile/', Profile.as_view(), name='profile')
]