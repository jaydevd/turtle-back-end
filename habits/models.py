from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator

from common.utils import *
from common.enums import FrequencyType, DurationType, Status, Weekday

import uuid

# Create your models here.
# class Habit(models.Model):

#   id=models.UUIDField(default=uuid.uuid4, primary_key=True)
#   name=models.CharField()
#   user=models.ForeignKey(
#     User,
#     on_delete=models.CASCADE,
#     related_name="habit"
#   )
  
#   goal=models.JSONField() # repetations eg. 1 time a day, 140 calories a week, etc.
#   reminder=models.CharField() # remind user to perform the habit at their chosen time
#   tag=models.CharField() # it shows what type of habit it is.
  
#   start_date=models.BigIntegerField()
#   end_date=models.BigIntegerField(null=True, blank=True)
#   habit_duration_type=models.CharField() # eg. never ending / fix timeline

#   created_at=models.BigIntegerField(default=get_unix_timestamp, editable=False)
#   updated_at=models.BigIntegerField(default=get_unix_timestamp)

#   is_deleted=models.BooleanField(default=False)
  
#   def __str__(self):
#     return str(self.id)

# ------------------------------------------------------------------------------

class Tag(models.Model):
  id = models.UUIDField(default=uuid.uuid4, primary_key=True, editable=False)
  name = models.CharField(max_length=100)
  user = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    related_name="tags"
  )

  def __str__(self):
    return self.name

class Habit(models.Model):

  id = models.UUIDField(
    primary_key=True,
    default=uuid.uuid4,
    editable=False
  )

  user = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    related_name="habits"
  )

  name = models.CharField(max_length=255)

  goal = models.TextField(
    blank=True,
    null=True
  )

  tag = models.ForeignKey(
    Tag,
    on_delete=models.PROTECT,
    related_name="habits"
  )

  duration_type = models.CharField(
    max_length=20,
    choices=DurationType.choices,
    default=DurationType.INDEFINITE
  )

  start_date = models.BigIntegerField()

  end_date = models.BigIntegerField(
    blank=True,
    null=True
  )

  reminder = models.TimeField(
    blank=True,
    null=True
  )

  status = models.CharField(
    max_length=20,
    choices=Status.choices,
    default=Status.ACTIVE
  )

  color = models.CharField(
    max_length=20,
    blank=True,
    null=True
  )

  icon = models.CharField(
    max_length=100,
    blank=True,
    null=True
  )

  created_at = models.BigIntegerField(
    default=get_unix_timestamp,
    editable=False
  )

  updated_at = models.BigIntegerField(
    default=get_unix_timestamp
  )

  is_deleted = models.BooleanField(
    default=False
  )

  class Meta:
    ordering = ["-created_at"]
    indexes = [
      models.Index(fields=["user", "status"]),
      models.Index(fields=["user", "start_date"]),
      # Every analytics query filters on `user + is_deleted`, which none of the
      # existing indexes cover.
      models.Index(fields=["user", "is_deleted"], name="habit_user_deleted_idx"),
    ]

  def save(self, *args, **kwargs):
    self.updated_at = get_unix_timestamp()
    super().save(*args, **kwargs)

  def __str__(self):
    return self.name

# ------------------------------------------------------------------------------

class HabitSchedule(models.Model):

  id = models.UUIDField(
    primary_key=True,
    default=uuid.uuid4,
    editable=False,
  )

  habit = models.OneToOneField(
    Habit,
    on_delete=models.CASCADE,
    related_name="schedule",
  )

  frequency_type = models.CharField(
    max_length=20,
    choices=FrequencyType.choices,
    default=FrequencyType.DAILY,
  )

  # How many times the habit should be completed
  # within the schedule period.
  target_count = models.PositiveIntegerField(
    default=1,
    validators=[
        MinValueValidator(1),
    ],
  )

  # Used for CUSTOM schedules.
  #
  # Example:
  # [0, 2, 4]
  #
  # means Monday, Wednesday and Friday.
  weekdays = models.JSONField(
    default=list,
    blank=True,
  )

  # Optional time at which the habit is expected.
  scheduled_time = models.TimeField(
    null=True,
    blank=True,
  )

  created_at = models.BigIntegerField(
    default=get_unix_timestamp,
    editable=False
  )

  updated_at = models.BigIntegerField(
    default=get_unix_timestamp
  )

  class Meta:
    ordering = ["created_at"]

  def save(self, *args, **kwargs):
    self.updated_at = get_unix_timestamp()
    super().save(*args, **kwargs)

  def __str__(self):
    return f"{self.habit.name} schedule"

# ------------------------------------------------------------------------------

class HabitLog(models.Model):

  id = models.UUIDField(
    primary_key=True,
    default=uuid.uuid4,
    editable=False,
  )

  habit = models.ForeignKey(
    Habit,
    on_delete=models.CASCADE,
    related_name="logs",
  )

  # The calendar day this log belongs to.
  date = models.BigIntegerField()

  status = models.CharField(
    max_length=20,
    choices=Status.choices,
  )

  # Useful for habits that have measurable repetitions.
  #
  # Example:
  # Target = 3
  # completed_count = 2
  #
  # This can represent partial completion.
  completed_count = models.PositiveIntegerField(
    default=0,
  )

  # Optional user note for that particular day's activity.
  note = models.TextField(
    blank=True,
  )

  # Actual time at which the habit was completed.
  completed_at = models.DateTimeField(
    null=True,
    blank=True,
  )

  created_at = models.BigIntegerField(
    default=get_unix_timestamp,
    editable=False
  )

  updated_at = models.BigIntegerField(
    default=get_unix_timestamp
  )

  class Meta:
    ordering = ["-date", "-created_at"]

    constraints = [
      models.UniqueConstraint(
        fields=["habit", "date"],
        name="unique_habit_log_per_day",
      ),
    ]

    indexes = [
      models.Index(
        fields=["habit", "date"],
        name="habit_log_habit_date_idx",
      ),
      models.Index(
        fields=["habit", "status"],
        name="habit_log_habit_status_idx",
      ),
    ]

  def save(self, *args, **kwargs):
    self.updated_at = get_unix_timestamp()
    super().save(*args, **kwargs)

  def __str__(self):
    return f"{self.habit.name} - {self.date}"