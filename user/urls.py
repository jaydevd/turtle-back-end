from django.urls import path
from .views import *

urlpatterns = [
  path('auth/sign-up/', SignUp.as_view()),
  path('auth/log-in/', LogIn.as_view()),
  path('auth/log-out/', LogOut.as_view()),
  path('profile/', Profile.as_view(), name='profile')
]