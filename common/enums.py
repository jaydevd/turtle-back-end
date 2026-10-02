from django.db import models

class DurationType(models.TextChoices):
  INDEFINITE = "INDEFINITE", "Indefinite"
  FIXED = "FIXED", "Fixed"

class FrequencyType(models.TextChoices):
  DAILY = "DAILY", "Daily"
  WEEKLY = "WEEKLY", "Weekly"
  CUSTOM = "CUSTOM", "Custom"

class Status(models.TextChoices):
  ACTIVE = "ACTIVE", "Active"
  COMPLETED = "COMPLETED", "Completed"
  PARTIAL = "PARTIAL", "Partial"
  MISSED = "MISSED", "Missed"
  SKIPPED = "SKIPPED", "Skipped"

class Weekday(models.IntegerChoices):
  MONDAY = 0, "Monday"
  TUESDAY = 1, "Tuesday"
  WEDNESDAY = 2, "Wednesday"
  THURSDAY = 3, "Thursday"
  FRIDAY = 4, "Friday"
  SATURDAY = 5, "Saturday"
  SUNDAY = 6, "Sunday"

class DayState(models.TextChoices):
  """Resolved outcome of one calendar day for one habit.

  Derived, never stored. `NOT_DUE` covers days outside a habit's schedule or
  lifecycle, `PENDING` covers days that have not happened yet. Both are excluded
  from every rate denominator, as is `SKIPPED` because deliberately skipping is
  not a failure.
  """
  HIT = "HIT", "Hit"
  PARTIAL = "PARTIAL", "Partial"
  MISSED = "MISSED", "Missed"
  SKIPPED = "SKIPPED", "Skipped"
  NOT_DUE = "NOT_DUE", "Not due"
  PENDING = "PENDING", "Pending"

class ScoreUnit(models.TextChoices):
  """Granularity a habit is scored at.

  WEEKLY schedules mean "hit target_count on any days this week", so their
  target applies to a week rather than to a day and they are scored per week to
  keep the denominator honest.
  """
  DAY = "DAY", "Day"
  WEEK = "WEEK", "Week"

class TrendDirection(models.TextChoices):
  IMPROVING = "IMPROVING", "Improving"
  STABLE = "STABLE", "Stable"
  DECLINING = "DECLINING", "Declining"
  INSUFFICIENT_DATA = "INSUFFICIENT_DATA", "Insufficient data"

class RiskLevel(models.TextChoices):
  LOW = "LOW", "Low"
  MEDIUM = "MEDIUM", "Medium"
  HIGH = "HIGH", "High"


# ---- Group and Challenge enums ----


class GroupRole(models.TextChoices):
  OWNER = "owner", "Owner"
  ADMIN = "admin", "Admin"
  MEMBER = "member", "Member"


class JoinRequestStatus(models.TextChoices):
  PENDING = "pending", "Pending"
  ACCEPTED = "accepted", "Accepted"
  REJECTED = "rejected", "Rejected"
  CANCELLED = "cancelled", "Cancelled"


class InvitationStatus(models.TextChoices):
  PENDING = "pending", "Pending"
  ACCEPTED = "accepted", "Accepted"
  EXPIRED = "expired", "Expired"
  REVOKED = "revoked", "Revoked"


class ChallengeStatus(models.TextChoices):
  DRAFT = "draft", "Draft"
  ACTIVE = "active", "Active"
  COMPLETED = "completed", "Completed"
  CANCELLED = "cancelled", "Cancelled"
