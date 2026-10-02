from django.contrib import admin

from .models import ChallengeSubscription, Habit, HabitLog, HabitSchedule, Tag


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
  list_display = ('name', 'user')
  search_fields = ('name', 'user__email')
  raw_id_fields = ('user',)


class HabitScheduleInline(admin.StackedInline):
  model = HabitSchedule
  extra = 0


@admin.register(Habit)
class HabitAdmin(admin.ModelAdmin):
  list_display = ('name', 'user', 'group', 'is_challenge', 'challenge_status', 'status', 'start_date', 'end_date', 'is_deleted', 'created_at')
  list_filter = ('is_challenge', 'challenge_status', 'status', 'duration_type', 'is_deleted')
  search_fields = ('name', 'user__email', 'group__name')
  raw_id_fields = ('user', 'tag', 'group', 'challenge_source', 'challenge_started_by', 'challenge_winner')
  inlines = (HabitScheduleInline,)


@admin.register(HabitLog)
class HabitLogAdmin(admin.ModelAdmin):
  list_display = ('habit', 'date', 'status', 'completed_count')
  list_filter = ('status',)
  raw_id_fields = ('habit',)


@admin.register(ChallengeSubscription)
class ChallengeSubscriptionAdmin(admin.ModelAdmin):
  list_display = ('challenge', 'user', 'subscribed_at')
  search_fields = ('challenge__name', 'user__email')
  raw_id_fields = ('challenge', 'user')