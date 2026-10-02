from django.db import models
from common.utils import get_unix_timestamp
# from django_mongodb_backend import fields
import uuid
from .managers import CustomUserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

class Role(models.TextChoices):
    ADMIN = "admin", "Admin"
    MANAGER = "manager", "Manager"
    USER = "user", "User"

# Create your models here.
class User(AbstractBaseUser, PermissionsMixin):

    id = models.UUIDField(default=uuid.uuid4, primary_key=True, editable=False)
    email = models.EmailField(unique=True)

    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)

    created_at = models.BigIntegerField(default=get_unix_timestamp, editable=False)
    updated_at = models.BigIntegerField(default=get_unix_timestamp)

    is_staff = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    user_role = models.CharField(choices=Role.choices, default=Role.USER)

    objects = CustomUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # email & password already required

    def __str__(self):
        return self.email