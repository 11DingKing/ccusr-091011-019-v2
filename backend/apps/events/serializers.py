"""
业务事件序列化器
"""
from rest_framework import serializers
from .models import BusinessEvent


class BusinessEventSerializer(serializers.ModelSerializer):
    """业务事件序列化器"""
    event_type_display = serializers.CharField(source='get_event_type_display', read_only=True)

    class Meta:
        model = BusinessEvent
        fields = [
            'seq', 'event_type', 'event_type_display',
            'goods_id', 'biz_id', 'actor_id', 'payload', 'created_at'
        ]
