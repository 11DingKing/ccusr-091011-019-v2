"""
业务事件流视图
"""
import logging

from django.db.models import Max, Min
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.response import error_response, success_response
from .models import BusinessEvent
from .serializers import BusinessEventSerializer
from .services import user_can_view

logger = logging.getLogger('apps')


class EventFeedView(APIView):
    """业务事件流接口（按数据库提交顺序读取）

    - cursor 表示"已消费的最大 seq"，本接口按 seq 升序返回其后的事件；
    - next_cursor 按扫描到的原始事件推进：权限裁剪与类型过滤只影响
      返回内容，不影响游标单调性，权限变化不会造成遗漏或重排；
    - 游标过旧（之后的事件已被保留窗口清理）时返回 410 与可恢复边界，
      消费者携带 min_cursor 重新对齐即可继续。
    """
    permission_classes = [IsAuthenticated]

    DEFAULT_LIMIT = 100
    MAX_LIMIT = 500

    def get(self, request):
        cursor = self._parse_int(request.query_params.get('cursor'), default=0)
        if cursor is None or cursor < 0:
            return error_response(message='cursor 必须是非负整数')

        limit = self._parse_int(request.query_params.get('limit'), default=self.DEFAULT_LIMIT)
        if limit is None or limit < 1:
            return error_response(message='limit 必须是正整数')
        limit = min(limit, self.MAX_LIMIT)

        type_filter = [t.strip() for t in
                       request.query_params.get('event_types', '').split(',') if t.strip()]
        invalid = [t for t in type_filter if t not in BusinessEvent.TYPE_VALUES]
        if invalid:
            return error_response(message=f'未知事件类型: {",".join(invalid)}')

        bounds = BusinessEvent.objects.aggregate(first=Min('seq'), latest=Max('seq'))
        earliest_seq = bounds['first']
        latest_seq = bounds['latest'] or 0

        # 过旧游标：之后已有事件被清理，无法保证不遗漏，返回可恢复边界
        if earliest_seq is not None and cursor < earliest_seq - 1:
            logger.warning(
                f"User {request.user.username} cursor {cursor} expired, "
                f"earliest available seq is {earliest_seq}"
            )
            return error_response(
                message='游标已过期，请从 min_cursor 重新对齐',
                code=410,
                data={
                    'reason': 'cursor_expired',
                    'min_cursor': earliest_seq - 1,
                    'earliest_seq': earliest_seq,
                    'latest_seq': latest_seq,
                })

        # 多取一条用于判断是否还有后续事件
        queryset = BusinessEvent.objects.filter(seq__gt=cursor).order_by('seq')
        scanned = list(queryset[:limit + 1])
        has_more = len(scanned) > limit
        page = scanned[:limit]
        next_cursor = page[-1].seq if page else cursor

        # 权限裁剪与类型过滤只裁剪返回内容，游标仍按原始事件流推进
        visible = [
            event for event in page
            if user_can_view(request.user, event)
            and (not type_filter or event.event_type in type_filter)
        ]

        return success_response(data={
            'events': BusinessEventSerializer(visible, many=True).data,
            'next_cursor': next_cursor,
            'has_more': has_more,
            'latest_seq': latest_seq,
        })

    @staticmethod
    def _parse_int(raw, default):
        if raw is None or raw == '':
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
