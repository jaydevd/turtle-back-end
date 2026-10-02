from django.urls import path
from .views import (
  HabitDashboardViewSet,
  HabitDetailInsightsViewSet,
  HabitLogViewSet,
  HabitOverviewViewSet,
  HabitPatternsViewSet,
  HabitRiskViewSet,
  HabitScheduleViewSet,
  HabitStatsViewSet,
  HabitTrendViewSet,
  HabitViewSet,
  TagViewSet,
)

habit_list = HabitViewSet.as_view({'get': 'list', 'post': 'create'})
habit_create = HabitViewSet.as_view({'post': 'create'})
habit_detail = HabitViewSet.as_view({
    'get': 'retrieve',
    'put': 'update',
    'patch': 'partial_update',
    'delete': 'destroy',
})
tag_list = TagViewSet.as_view({'get': 'list', 'post': 'create'})
tag_detail = TagViewSet.as_view({
    'get': 'retrieve',
    'put': 'update',
    'patch': 'partial_update',
    'delete': 'destroy',
})
habit_log_list = HabitLogViewSet.as_view({'get': 'list', 'post': 'create'})
habit_log_detail = HabitLogViewSet.as_view({
    'get': 'retrieve',
    'put': 'update',
    'patch': 'partial_update',
    'delete': 'destroy',
})
habit_stats = HabitStatsViewSet.as_view({'get': 'list'})
habit_dashboard = HabitDashboardViewSet.as_view({'get': 'list'})
habit_overview = HabitOverviewViewSet.as_view({'get': 'list'})
habit_patterns = HabitPatternsViewSet.as_view({'get': 'list'})
habit_trend = HabitTrendViewSet.as_view({'get': 'list'})
habit_risk = HabitRiskViewSet.as_view({'get': 'list'})
habit_detail_insights = HabitDetailInsightsViewSet.as_view({'get': 'list'})
habit_schedule = HabitScheduleViewSet.as_view({
    'get': 'retrieve',
    'post': 'create',
    'put': 'update',
    'patch': 'partial_update',
})

# Literal-prefixed analytics routes must stay ABOVE `<uuid:pk>`, otherwise the
# uuid converter swallows `insights/` and the request 404s.
urlpatterns = [
  path('', habit_list, name='habit-list-create'),
  path('create/', habit_create, name='habit-create-legacy'),
  path('tags/', tag_list, name='tag-list-create'),
  path('tags/<uuid:pk>/', tag_detail, name='tag-detail'),
  path('logs/', habit_log_list, name='habit-log-list-create'),
  path('logs/<uuid:pk>/', habit_log_detail, name='habit-log-detail'),
  path('stats/', habit_stats, name='habit-stats'),
  path('dashboard/', habit_dashboard, name='habit-dashboard'),
  path('insights/overview/', habit_overview, name='habit-insights-overview'),
  path('insights/patterns/', habit_patterns, name='habit-insights-patterns'),
  path('insights/trend/', habit_trend, name='habit-insights-trend'),
  path('insights/risk/', habit_risk, name='habit-insights-risk'),
  path('<uuid:habit_id>/schedule/', habit_schedule, name='habit-schedule'),
  path('<uuid:habit_id>/insights/', habit_detail_insights, name='habit-insights-detail'),
  path('<uuid:pk>/', habit_detail, name='habit-detail'),
]