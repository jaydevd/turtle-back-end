from django.utils import timezone

def get_unix_timestamp():
  return int(timezone.now().timestamp())
