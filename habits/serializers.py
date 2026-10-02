from rest_framework import serializers
from common.enums import DurationType, FrequencyType, Weekday
from .models import Habit, HabitLog, HabitSchedule, Tag


class TagSerializer(serializers.ModelSerializer):
  habit_count = serializers.SerializerMethodField()

  class Meta:
    model = Tag
    fields = ['id', 'name', 'habit_count']
    read_only_fields = ['id', 'habit_count']

  def get_habit_count(self, obj):
    annotated = getattr(obj, 'habit_count', None)
    if annotated is not None:
      return annotated
    return obj.habits.filter(is_deleted=False).count()


class HabitScheduleSerializer(serializers.ModelSerializer):
  class Meta:
    model = HabitSchedule
    fields = [
      'id',
      'habit',
      'frequency_type',
      'target_count',
      'weekdays',
      'scheduled_time',
      'created_at',
      'updated_at',
    ]
    read_only_fields = ('id', 'habit', 'created_at', 'updated_at')

  def validate(self, attrs):
    frequency_type = attrs.get(
      'frequency_type',
      getattr(self.instance, 'frequency_type', FrequencyType.DAILY),
    )
    weekdays = attrs.get('weekdays', getattr(self.instance, 'weekdays', None))

    if frequency_type == FrequencyType.CUSTOM:
      if not weekdays:
        raise serializers.ValidationError({
          'weekdays': 'Pick at least one day for a custom schedule.'
        })
      if not all(isinstance(day, int) and day in Weekday.values for day in weekdays):
        raise serializers.ValidationError({
          'weekdays': 'Days must be integers between 0 (Monday) and 6 (Sunday).'
        })

    return attrs


class HabitLogSerializer(serializers.ModelSerializer):
  class Meta:
    model = HabitLog
    fields = [
      'id',
      'habit',
      'date',
      'status',
      'completed_count',
      'note',
      'completed_at',
      'created_at',
      'updated_at',
    ]
    read_only_fields = ('id', 'created_at', 'updated_at')
    extra_kwargs = {
      'note': {'required': False, 'allow_blank': True},
      'completed_at': {'required': False},
    }

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    user = self._user()
    if user is not None and 'habit' in self.fields:
      self.fields['habit'].queryset = Habit.objects.filter(
        user=user,
        is_deleted=False,
      )

  def _user(self):
    request = self.context.get('request')
    return getattr(request, 'user', None) if request is not None else None

  def validate(self, attrs):
    habit = attrs.get('habit', getattr(self.instance, 'habit', None))
    if habit is None:
      raise serializers.ValidationError({'habit': 'This field is required.'})

    if habit.user_id != self.context['request'].user.id:
      raise serializers.ValidationError({'habit': 'Habit does not belong to you.'})

    date = attrs.get('date', getattr(self.instance, 'date', None))
    if date is None:
      raise serializers.ValidationError({'date': 'This field is required.'})

    completed_count = attrs.get(
      'completed_count',
      getattr(self.instance, 'completed_count', 0),
    )
    if completed_count < 0:
      raise serializers.ValidationError({
        'completed_count': 'Completed count cannot be negative.'
      })

    return attrs


class HabitSerializer(serializers.ModelSerializer):
  user = serializers.PrimaryKeyRelatedField(read_only=True)
  tag_detail = TagSerializer(source='tag', read_only=True)
  schedule = HabitScheduleSerializer(required=False, allow_null=True)

  class Meta:
    model = Habit
    fields = [
      'id',
      'user',
      'name',
      'goal',
      'reminder',
      'tag',
      'tag_detail',
      'start_date',
      'end_date',
      'duration_type',
      'status',
      'color',
      'icon',
      'schedule',
      'created_at',
      'updated_at',
    ]
    read_only_fields = ('id', 'user', 'tag_detail', 'created_at', 'updated_at')
    extra_kwargs = {
      'status': {'required': False},
      'end_date': {'required': False},
      'icon': {'required': False},
      'color': {'required': False},
      'reminder': {'required': False},
    }

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    request = self.context.get('request')
    user = getattr(request, 'user', None) if request is not None else None
    if user is not None and 'tag' in self.fields:
      self.fields['tag'].queryset = Tag.objects.filter(user=user)

  def get_schedule(self, obj):
    schedule = getattr(obj, 'schedule', None)
    if schedule is None:
      return None
    return HabitScheduleSerializer(schedule).data

  def _apply_schedule(self, habit, schedule_data):
    if schedule_data is None:
      return
    HabitSchedule.objects.update_or_create(habit=habit, defaults=schedule_data)

  def create(self, validated_data):
    schedule_data = validated_data.pop('schedule', None)
    habit = Habit.objects.create(**validated_data)
    self._apply_schedule(habit, schedule_data)
    return habit

  def update(self, instance, validated_data):
    schedule_data = validated_data.pop('schedule', None)
    habit = super().update(instance, validated_data)
    self._apply_schedule(habit, schedule_data)
    return habit

  def validate(self, attrs):
    instance = self.instance
    duration_type = attrs.get(
      'duration_type',
      getattr(instance, 'duration_type', DurationType.INDEFINITE),
    )
    start_date = attrs.get('start_date', getattr(instance, 'start_date', None))
    end_date = attrs.get('end_date', getattr(instance, 'end_date', None))

    if duration_type == DurationType.FIXED:
      if end_date is None:
        raise serializers.ValidationError({
          'end_date': 'End date is required for a fixed-duration habit.'
        })
      if start_date is not None and end_date <= start_date:
        raise serializers.ValidationError({
          'end_date': 'End date must be greater than start date.'
        })

    return attrs
