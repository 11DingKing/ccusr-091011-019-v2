"""
业务事件流URL配置
"""
from django.urls import path
from .views import EventFeedView

urlpatterns = [
    path('events/', EventFeedView.as_view(), name='event-feed'),
]
