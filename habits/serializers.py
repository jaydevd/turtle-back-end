from rest_framework import serializers
from common.enums import ChallengeStatus, DurationType, FrequencyType, Weekday
from . import challenges as challenge_rules
from .models import Habit, HabitLog, HabitSchedule, ChallengeSubscription, Tag


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
      if not isinstance(weekdays, list) or not all(
        isinstance(day, int) and day in Weekday.values for day in weekdays
      ):
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
  challenge_winners = serializers.SerializerMethodField()
  participant_count = serializers.SerializerMethodField()

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
      # Group challenge block. Read-only apart from `challenge_rules`, which the
      # author may edit until the challenge starts. `group` and `is_challenge`
      # are deliberately not writable here: a habit only becomes a challenge by
      # being proposed inside a group, and that endpoint sets both.
      'group',
      'is_challenge',
      'challenge_status',
      'challenge_rules',
      'challenge_source',
      'challenge_started_by',
      'challenge_started_at',
      'challenge_ended_at',
      'challenge_winner',
      'challenge_winners',
      'challenge_winner_score',
      'participant_count',
      'created_at',
      'updated_at',
    ]
    read_only_fields = (
      'id',
      'user',
      'tag_detail',
      'group',
      'is_challenge',
      'challenge_status',
      'challenge_source',
      'challenge_started_by',
      'challenge_started_at',
      'challenge_ended_at',
      'challenge_winner',
      'challenge_winners',
      'challenge_winner_score',
      'participant_count',
      'created_at',
      'updated_at',
    )
    extra_kwargs = {
      'status': {'required': False},
      'end_date': {'required': False},
      'icon': {'required': False},
      'color': {'required': False},
      'reminder': {'required': False},
      'challenge_rules': {'required': False},
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

  def get_challenge_winners(self, obj):
    """Every user announced as a winner, ties included.

    `challenge_winner` alone cannot express a tie, and the rule is that tied
    participants are all announced, so the full set comes out of the stored
    scoring payload.
    """
    return challenge_rules.winner_ids(obj)

  def get_participant_count(self, obj):
    if not obj.is_challenge:
      return 0
    annotated = getattr(obj, 'participant_count', None)
    if annotated is not None:
      return annotated
    return obj.subscriptions.count()

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

  def _is_challenge(self):
    """Whether this payload is being read or written as a challenge.

    True for an existing challenge, or when the caller declared itself one - the
    group challenge endpoint sets `context['challenge']` because at create time
    there is no instance to inspect yet.
    """
    if self.context.get('challenge'):
      return True
    return bool(self.instance is not None and self.instance.is_challenge)

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

    if not self._is_challenge():
      return attrs

    if 'challenge_rules' in attrs:
      if instance is not None and instance.challenge_status != ChallengeStatus.DRAFT:
        raise serializers.ValidationError({
          'challenge_rules': 'Rules cannot be changed once the challenge has started.'
        })
      try:
        attrs['challenge_rules'] = challenge_rules.validate_rules(
          attrs['challenge_rules']
        )
      except ValueError as exc:
        raise serializers.ValidationError({'challenge_rules': str(exc)})

    try:
      challenge_rules.validate_challenge_window(duration_type, end_date)
    except ValueError as exc:
      raise serializers.ValidationError({'end_date': str(exc)})

    return attrs


class ChallengeSubscriptionSerializer(serializers.ModelSerializer):
  """One participant's place in a challenge, plus the habit carrying their
  progress.

  `habit_id` is what a client needs to log against. Subscribing hands the member
  their own copy of the challenge, and it is that copy which holds their logs -
  so the field is part of the response rather than something the client works
  out from `challenge`.
  """

  user_email = serializers.EmailField(source='user.email', read_only=True)
  habit_id = serializers.SerializerMethodField()

  class Meta:
    model = ChallengeSubscription
    fields = [
      'id',
      'challenge',
      'challenge_name',
      'user',
      'user_email',
      'habit_id',
      'subscribed_at',
    ]
    read_only_fields = (
      'id',
      'challenge',
      'challenge_name',
      'user',
      'user_email',
      'habit_id',
      'subscribed_at',
    )

  challenge_name = serializers.CharField(source='challenge.name', read_only=True)

  def get_habit_id(self, obj):
    """The member's copy of the challenge, or the challenge itself for its author.

    Returns None only if the copy was soft-deleted behind the subscription, which
    the client should treat as "not participating" rather than a crash.
    """
    if obj.challenge.user_id == obj.user_id:
      return str(obj.challenge_id)

    copy = (
      Habit.objects.filter(challenge_source_id=obj.challenge_id, user_id=obj.user_id)
      .order_by('-created_at')
      .first()
    )
    return str(copy.id) if copy is not None else None
