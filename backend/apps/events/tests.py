"""
业务事件流测试
"""
from decimal import Decimal

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import Category, Goods, StockIn, StockOut, Unit, Variety
from . import services
from .models import BusinessEvent


class EventFixture(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('event-admin', 'testpass123', role='admin')
        self.operator = User.objects.create_user('event-user', 'testpass123', role='user')
        self.client = APIClient()
        self.auth_as(self.admin)
        self.unit = Unit.objects.create(name='件', created_by=self.admin)
        self.category = Category.objects.create(name='受控器材', unit=self.unit, created_by=self.admin)
        self.variety = Variety.objects.create(name='记录终端', category=self.category, created_by=self.admin)
        self.goods = Goods.objects.create(
            variety=self.variety,
            name='执法记录终端',
            code='DEV-001',
            quantity=Decimal('12'),
            warning_threshold=Decimal('5'),
        )

    def auth_as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {generate_token(user)}')

    def make_stock_in(self, actor, quantity='1'):
        """一次真实入库事务：业务记录与事件在同一事务中提交"""
        with transaction.atomic():
            record = StockIn.objects.create(
                goods=self.goods, operator=actor, quantity=Decimal(quantity))
            event = services.record_stock_in(record, actor)
        return record, event

    def make_release(self, actor, quantity='1'):
        """一次真实放行事务"""
        with transaction.atomic():
            record = StockOut.objects.create(
                goods=self.goods, operator=actor, receiver='保管员',
                quantity=Decimal(quantity), status='completed')
            event = services.record_release(record, actor)
        return record, event

    def drain(self, limit=10, **params):
        """携带游标翻页直到 has_more=False，返回 (事件seq列表, next_cursor轨迹)"""
        seqs, cursors = [], []
        cursor = 0
        for _ in range(100):
            response = self.client.get('/api/events/', {'cursor': cursor, 'limit': limit, **params})
            self.assertEqual(response.status_code, 200)
            data = response.json()['data']
            seqs.extend(e['seq'] for e in data['events'])
            cursors.append(data['next_cursor'])
            cursor = data['next_cursor']
            if not data['has_more']:
                break
        else:
            self.fail('翻页超过100次仍未结束')
        return seqs, cursors


class EventFeedTest(EventFixture):
    def test_same_millisecond_events_keep_commit_order(self):
        """同一毫秒提交的事件不遗漏、不重复、按提交顺序返回"""
        for _ in range(30):
            self.make_stock_in(self.admin)
        # 强制所有事件时间戳完全相同，证明排序不依赖时间
        BusinessEvent.objects.update(created_at=timezone.now())

        response = self.client.get('/api/events/', {'cursor': 0, 'limit': 100})
        seqs = [e['seq'] for e in response.json()['data']['events']]
        self.assertEqual(seqs, list(range(1, 31)))
        self.assertEqual(len(set(seqs)), 30)

    def test_cursor_pagination_has_no_gap_or_duplicate(self):
        """游标翻页完整覆盖事件流，无遗漏无重复"""
        for _ in range(25):
            self.make_stock_in(self.admin)

        seqs, cursors = self.drain(limit=10)
        self.assertEqual(seqs, list(range(1, 26)))
        self.assertEqual(cursors, [10, 20, 25])

        # 追平后继续拉取：空页且游标不动
        data = self.client.get('/api/events/', {'cursor': 25, 'limit': 10}).json()['data']
        self.assertEqual(data['events'], [])
        self.assertEqual(data['next_cursor'], 25)
        self.assertFalse(data['has_more'])
        self.assertEqual(data['latest_seq'], 25)

    def test_all_four_event_types_share_one_stable_sequence(self):
        """入库、放行、冻结、更正共享同一条稳定递增序号流"""
        self.make_stock_in(self.admin)
        self.make_release(self.admin)
        with transaction.atomic():
            self.goods.is_active = False
            self.goods.save(update_fields=['is_active'])
            services.record_freeze(self.goods, self.admin, reason='监管冻结')
        with transaction.atomic():
            services.record_correction(
                goods_id=self.goods.id, actor=self.admin,
                changes={'quantity': ['12', '10']})

        events = self.client.get('/api/events/', {'cursor': 0, 'limit': 10}).json()['data']['events']
        self.assertEqual(
            [e['event_type'] for e in events],
            ['stock_in', 'release', 'freeze', 'correction'])
        self.assertEqual([e['seq'] for e in events], [1, 2, 3, 4])

    def test_permission_trimming_keeps_cursor_monotonic(self):
        """权限裁剪不改变游标推进，权限变化也不造成重排"""
        self.make_stock_in(self.admin)      # seq 1（普通用户不可见）
        self.make_stock_in(self.operator)   # seq 2
        self.make_stock_in(self.admin)      # seq 3
        self.make_stock_in(self.operator)   # seq 4
        self.make_stock_in(self.admin)      # seq 5

        # 普通用户只见本人事件，但游标轨迹与管理员全量一致
        self.auth_as(self.operator)
        seqs, cursors = self.drain(limit=2)
        self.assertEqual(seqs, [2, 4])
        self.assertEqual(cursors, [2, 4, 5])

        self.auth_as(self.admin)
        admin_seqs, admin_cursors = self.drain(limit=2)
        self.assertEqual(admin_seqs, [1, 2, 3, 4, 5])
        self.assertEqual(admin_cursors, cursors)

        # 权限中途提升：从旧游标继续拉取，序号仍单调递增、不重排
        self.operator.role = 'admin'
        self.operator.save(update_fields=['role'])
        self.auth_as(self.operator)
        data = self.client.get('/api/events/', {'cursor': 2, 'limit': 10}).json()['data']
        self.assertEqual([e['seq'] for e in data['events']], [3, 4, 5])
        self.assertEqual(data['next_cursor'], 5)

    def test_stale_cursor_returns_recoverable_boundary(self):
        """过旧游标返回 410 与可恢复边界，携带 min_cursor 可继续对齐"""
        for _ in range(10):
            self.make_stock_in(self.admin)
        services.purge_events_before(6)  # 清理 seq 1..5

        response = self.client.get('/api/events/', {'cursor': 2})
        self.assertEqual(response.status_code, 410)
        body = response.json()
        self.assertFalse(body['success'])
        self.assertEqual(body['data']['reason'], 'cursor_expired')
        self.assertEqual(body['data']['min_cursor'], 5)
        self.assertEqual(body['data']['earliest_seq'], 6)
        self.assertEqual(body['data']['latest_seq'], 10)

        # 携带边界游标恢复拉取
        data = self.client.get(
            '/api/events/', {'cursor': body['data']['min_cursor']}).json()['data']
        self.assertEqual([e['seq'] for e in data['events']], [6, 7, 8, 9, 10])

        # 恰好衔接保留窗口的游标不算过期
        self.assertEqual(self.client.get('/api/events/', {'cursor': 5}).status_code, 200)

    def test_restart_keeps_published_order_and_continues_sequence(self):
        """重启后已发布事件不重排，序号接续分配不复用"""
        for _ in range(5):
            self.make_stock_in(self.admin)
        before = self.client.get('/api/events/', {'cursor': 0, 'limit': 100}).json()['data']

        connection.close()  # 模拟服务重启：丢弃全部连接与进程内状态

        self.make_stock_in(self.admin)  # 重启后的新事件
        after = self.client.get('/api/events/', {'cursor': 0, 'limit': 100}).json()['data']

        # 已发布事件逐条一致，新事件序号接续
        self.assertEqual(after['events'][:5], before['events'])
        self.assertEqual(after['events'][5]['seq'], before['latest_seq'] + 1)
        # 旧游标重放结果与重启前完全相同
        replay = self.client.get('/api/events/', {'cursor': 2, 'limit': 3}).json()['data']
        self.assertEqual([e['seq'] for e in replay['events']], [3, 4, 5])

    def test_rolled_back_transaction_publishes_nothing(self):
        """业务回滚则事件消失，且不会向已发布流留下序号空洞"""
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                record = StockIn.objects.create(
                    goods=self.goods, operator=self.admin, quantity=Decimal('1'))
                services.record_stock_in(record, self.admin)
                raise RuntimeError('业务失败回滚')

        self.assertEqual(BusinessEvent.objects.count(), 0)
        self.assertEqual(StockIn.objects.count(), 0)

        # 回滚不消耗已发布序号：下一个事件仍从 1 开始
        _, event = self.make_stock_in(self.admin)
        self.assertEqual(event.seq, 1)

    def test_type_filter_does_not_move_cursor(self):
        """类型过滤只裁剪返回内容，游标仍按原始事件流推进"""
        self.make_stock_in(self.admin)   # seq 1
        self.make_release(self.admin)    # seq 2
        self.make_stock_in(self.admin)   # seq 3

        plain = self.client.get('/api/events/', {'cursor': 0, 'limit': 10}).json()['data']
        filtered = self.client.get(
            '/api/events/', {'cursor': 0, 'limit': 10, 'event_types': 'stock_in'}).json()['data']

        self.assertEqual([e['seq'] for e in filtered['events']], [1, 3])
        self.assertEqual(filtered['next_cursor'], plain['next_cursor'])

    def test_requires_authentication(self):
        self.assertEqual(APIClient().get('/api/events/').status_code, 401)

    def test_rejects_invalid_params(self):
        self.assertEqual(self.client.get('/api/events/', {'cursor': 'abc'}).status_code, 400)
        self.assertEqual(self.client.get('/api/events/', {'cursor': -1}).status_code, 400)
        self.assertEqual(self.client.get('/api/events/', {'limit': 0}).status_code, 400)
        self.assertEqual(self.client.get('/api/events/', {'limit': 'x'}).status_code, 400)
        self.assertEqual(self.client.get('/api/events/', {'event_types': 'bogus'}).status_code, 400)


class RecordEventContractTest(TransactionTestCase):
    """record_event 的事务契约（TestCase 自带事务，需用 TransactionTestCase 验证）"""

    def test_record_event_requires_atomic_block(self):
        user = User.objects.create_user('contract-user', 'testpass123', role='admin')
        with self.assertRaises(RuntimeError):
            services.record_event(BusinessEvent.TYPE_FREEZE, goods_id=1, actor=user)

        with transaction.atomic():
            event = services.record_event(BusinessEvent.TYPE_FREEZE, goods_id=1, actor=user)
        self.assertEqual(event.seq, 1)

    def test_unknown_event_type_rejected(self):
        with self.assertRaises(ValueError):
            with transaction.atomic():
                services.record_event('unknown_type')
