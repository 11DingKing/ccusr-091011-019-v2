"""业务事件流测试：提交顺序、游标分页、裁剪单调性、过旧游标恢复、重启稳定性。"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from django.apps import apps as django_apps
from django.db import connections
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import (
    BusinessEvent, EventSequenceCounter, Goods, StockIn,
)
from apps.warehouse.services import (
    record_correction, record_freeze, record_release, record_stock_in,
    read_events, StaleCursor,
)

from apps.warehouse.tests import WarehouseFixture


class CommandPublishTest(WarehouseFixture):
    """四个命令端到端发布事件。"""

    def path(self, name):
        return f"/api/commands/{name}/"

    def test_stock_in_publishes_dense_event_and_updates_stock(self):
        response = self.client.post(
            self.path("stock-in"),
            {"goods": self.goods.id, "quantity": "3.00", "batch_no": "B-1"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["sequence"], 1)
        self.assertEqual(data["event_type"], "stock_in")
        self.assertEqual(data["payload"]["quantity"], "3.00")
        self.assertEqual(data["payload"]["stock_after"], "15.00")
        self.assertEqual(data["goods_code"], "DEV-001")

        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("15"))
        self.assertEqual(StockIn.objects.count(), 1)

    def test_freeze_blocks_available_stock_and_release_respects_it(self):
        record_stock_in(self.goods, self.user, Decimal("10"))  # seq 1, stock 22
        ok = self.client.post(
            self.path("freeze"),
            {"goods": self.goods.id, "action": "freeze", "quantity": "20", "reason": "涉案封存"},
            format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.content)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.frozen_quantity, Decimal("20"))

        # 可用仅 2，放行 3 必须失败且不得发布事件。
        blocked = self.client.post(
            self.path("release"),
            {"goods": self.goods.id, "quantity": "3", "receiver": "办案员甲"},
            format="json",
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(BusinessEvent.objects.filter(event_type="release").count(), 0)

        released = self.client.post(
            self.path("release"),
            {"goods": self.goods.id, "quantity": "2", "receiver": "办案员甲"},
            format="json",
        )
        self.assertEqual(released.status_code, 200, released.content)
        self.assertEqual(released.json()["data"]["event_type"], "release")

    def test_unfreeze_over_frozen_is_rejected(self):
        response = self.client.post(
            self.path("freeze"),
            {"goods": self.goods.id, "action": "unfreeze", "quantity": "1"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(BusinessEvent.objects.count(), 0)

    def test_correction_records_before_and_after(self):
        response = self.client.post(
            self.path("correction"),
            {"goods": self.goods.id, "quantity_after": "9.5", "reason": "check", "remark": "盘亏"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]["payload"]
        self.assertEqual(payload["quantity_before"], "12.00")
        self.assertEqual(payload["quantity_after"], "9.50")
        self.assertEqual(payload["delta"], "-2.50")

    def test_negative_quantity_rejected(self):
        response = self.client.post(
            self.path("stock-in"), {"goods": self.goods.id, "quantity": "-1"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(BusinessEvent.objects.count(), 0)

    def test_idempotency_key_duplicates_command(self):
        body = {"goods": self.goods.id, "quantity": "4", "idempotency_key": "idem-001"}
        first = self.client.post(self.path("stock-in"), body, format="json")
        second = self.client.post(self.path("stock-in"), body, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["data"]["sequence"], second.json()["data"]["sequence"])
        self.assertEqual(BusinessEvent.objects.count(), 1)
        self.assertEqual(StockIn.objects.count(), 1)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("16"))

    def test_unknown_goods_returns_404(self):
        response = self.client.post(
            self.path("stock-in"), {"goods": 99999, "quantity": "1"}, format="json"
        )
        self.assertEqual(response.status_code, 404)


class SameMillisecondOrderingTest(WarehouseFixture):
    """sequence 由提交顺序决定，与事件入库时间戳是否相同无关。"""

    def test_events_with_identical_timestamp_keep_sequence_order(self):
        # 连续快速发布，时间戳可能落在同一毫秒；游标顺序必须严格按 sequence。
        for _ in range(6):
            record_stock_in(self.goods, self.user, Decimal("1"))
        events, _, _, _ = read_events(cursor=0, limit=100)
        sequences = [e.sequence for e in events]
        self.assertEqual(sequences, list(range(1, 7)))
        # 即便人为把 created_at 改成相同值，读取顺序依旧由 sequence 决定。
        BusinessEvent.objects.update(created_at=events[0].created_at)
        events2, _, _, _ = read_events(cursor=0, limit=100)
        self.assertEqual([e.sequence for e in events2], list(range(1, 7)))


class CursorPaginationTest(WarehouseFixture):
    def setUp(self):
        super().setUp()
        record_stock_in(self.goods, self.user, Decimal("1"))    # 1 stock_in
        record_release(self.goods, self.user, Decimal("1"), "甲")  # 2 release
        record_stock_in(self.goods, self.user, Decimal("1"))    # 3 stock_in
        record_freeze(self.goods, self.user, "freeze", Decimal("1"))  # 4 freeze
        record_correction(self.goods, self.user, Decimal("11"))  # 5 correction
        record_stock_in(self.goods, self.user, Decimal("1"))    # 6 stock_in

    def test_walk_all_pages_without_gap_or_duplicate(self):
        seen = []
        cursor = 0
        for _ in range(10):
            events, cursor, has_more, _ = read_events(cursor=cursor, limit=2)
            seen.extend(e.sequence for e in events)
            if not has_more:
                break
        self.assertEqual(seen, [1, 2, 3, 4, 5, 6])

    def test_resume_after_last_returns_empty_without_reorder(self):
        events, next_cursor, has_more, _ = read_events(cursor=6, limit=10)
        self.assertEqual(events, [])
        self.assertEqual(next_cursor, 6)
        self.assertFalse(has_more)

    def test_type_filter_keeps_global_monotonic_cursor(self):
        # 只看入库：可见序号为 1、3、6，但游标仍是全局绝对序号。
        page1, cursor1, has_more1, boundary = read_events(
            cursor=0, limit=2, event_types=["stock_in"]
        )
        self.assertEqual([e.sequence for e in page1], [1, 3])
        self.assertEqual(cursor1, 3)
        self.assertTrue(has_more1)
        # 边界永远是全局的，不受类型裁剪影响。
        self.assertEqual(boundary, (1, 6))

        page2, cursor2, has_more2, _ = read_events(
            cursor=cursor1, limit=2, event_types=["stock_in"]
        )
        self.assertEqual([e.sequence for e in page2], [6])
        self.assertEqual(cursor2, 6)
        self.assertFalse(has_more2)

        # 用过滤得到的游标回到全量流，中间被跳过的 4、5 不会重复出现，也不回退。
        events, cursor3, has_more3, _ = read_events(cursor=cursor2, limit=10)
        self.assertEqual(events, [])
        self.assertEqual(cursor3, 6)

    def test_stream_api_pagination_and_filter(self):
        response = self.client.get("/api/events/?limit=2&event_type=stock_in")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual([e["sequence"] for e in data["events"]], [1, 3])
        self.assertEqual(data["next_cursor"], 3)
        self.assertTrue(data["has_more"])
        self.assertEqual(data["earliest_sequence"], 1)
        self.assertEqual(data["latest_sequence"], 6)

    def test_stream_rejects_bad_params(self):
        self.assertEqual(self.client.get("/api/events/?cursor=-1").status_code, 400)
        self.assertEqual(self.client.get("/api/events/?limit=0").status_code, 400)
        self.assertEqual(self.client.get("/api/events/?limit=1001").status_code, 400)
        self.assertEqual(
            self.client.get("/api/events/?event_type=unknown").status_code, 400
        )

    def test_stream_requires_authentication(self):
        self.assertEqual(APIClient().get("/api/events/").status_code, 401)


class PermissionTrimTest(WarehouseFixture):
    def setUp(self):
        super().setUp()
        self.other = User.objects.create_user("clerk", "testpass123", role="user")
        # admin 与普通用户的事件交错提交。
        record_stock_in(self.goods, self.user, Decimal("1"))     # seq 1 admin
        record_stock_in(self.goods, self.other, Decimal("1"))    # seq 2 clerk
        record_freeze(self.goods, self.user, "freeze", Decimal("1"))  # seq 3 admin
        record_stock_in(self.goods, self.other, Decimal("1"))    # seq 4 clerk

    def test_trimming_only_hides_rows_without_renumbering(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.other)}")

        response = client.get("/api/events/?limit=10")
        data = response.json()["data"]
        # 普通用户只看到自己的事件，但序号保持全局编号，不重新计数。
        self.assertEqual([e["sequence"] for e in data["events"]], [2, 4])
        self.assertEqual(data["next_cursor"], 4)
        # 边界仍是全局边界，不受权限裁剪影响。
        self.assertEqual(data["earliest_sequence"], 1)
        self.assertEqual(data["latest_sequence"], 4)

    def test_cursor_into_hidden_gap_resumes_forward_only(self):
        # clerk 持有游标 1（seq1 是管理员事件，对其不可见），下一条只能是 seq2。
        events, next_cursor, _, _ = read_events(
            cursor=1, limit=10, operator_id=self.other.id
        )
        self.assertEqual([e.sequence for e in events], [2, 4])
        self.assertGreaterEqual(next_cursor, 1)

    def test_admin_sees_all_events(self):
        response = self.client.get("/api/events/?limit=10")
        sequences = [e["sequence"] for e in response.json()["data"]["events"]]
        self.assertEqual(sequences, [1, 2, 3, 4])


class StaleCursorTest(WarehouseFixture):
    def test_stale_cursor_raises_with_recoverable_boundary(self):
        for _ in range(5):
            record_stock_in(self.goods, self.user, Decimal("1"))
        # 模拟保留窗口清理：最旧事件已被物理清除。
        BusinessEvent.objects.filter(sequence__lte=2).delete()

        with self.assertRaises(StaleCursor) as ctx:
            read_events(cursor=0, limit=10)
        self.assertEqual(ctx.exception.earliest_sequence, 3)
        self.assertEqual(ctx.exception.latest_sequence, 5)

    def test_stale_cursor_api_returns_410_and_resume_hint(self):
        for _ in range(5):
            record_stock_in(self.goods, self.user, Decimal("1"))
        BusinessEvent.objects.filter(sequence__lte=2).delete()

        stale = self.client.get("/api/events/?cursor=0")
        self.assertEqual(stale.status_code, 410)
        data = stale.json()["data"]
        self.assertEqual(data["earliest_sequence"], 3)
        self.assertEqual(data["latest_sequence"], 5)
        self.assertEqual(data["resume_cursor"], 2)

        # 携带边界游标即可恢复，且不重排：从 3 开始顺序读取。
        resumed = self.client.get(f"/api/events/?cursor={data['resume_cursor']}&limit=10")
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(
            [e["sequence"] for e in resumed.json()["data"]["events"]], [3, 4, 5]
        )

    def test_cursor_at_retained_first_event_is_not_stale(self):
        for _ in range(3):
            record_stock_in(self.goods, self.user, Decimal("1"))
        BusinessEvent.objects.filter(sequence__lte=1).delete()
        # cursor=1 是 earliest-1 恢复点：下一条正好是最早保留事件 2。
        events, _, _, _ = read_events(cursor=1, limit=10)
        self.assertEqual([e.sequence for e in events], [2, 3])


class BackfillMigrationTest(TestCase):
    """0003 回填迁移：历史单据补事件，发号器从回填最大值继续，且只回填一次。"""

    def test_backfill_assigns_sequences_and_counter_continues(self):
        from importlib import import_module
        mod = import_module("apps.warehouse.migrations.0003_backfill_business_events")
        from apps.warehouse.models import Unit, Category, Variety

        user = User.objects.create_user("bf-user", "testpass123", role="admin")
        unit_obj = Unit.objects.create(name="件", created_by=user)
        category = Category.objects.create(name="类", unit=unit_obj, created_by=user)
        variety = Variety.objects.create(name="种", category=category, created_by=user)
        g1 = Goods.objects.create(variety=variety, name="物一", code="BF-1")
        g2 = Goods.objects.create(variety=variety, name="物二", code="BF-2")
        StockIn.objects.create(goods=g1, operator=user, quantity=Decimal("1"))
        StockIn.objects.create(goods=g2, operator=user, quantity=Decimal("2"))

        # 测试库建表时 0003 已跑过（空数据，发号器 last_sequence=0）。
        # 删除发号器以还原“尚未回填”的状态，再直接执行回填函数。
        EventSequenceCounter.objects.filter(pk=1).delete()
        BusinessEvent.objects.all().delete()

        class FakeSchema:
            pass

        mod.backfill_events(django_apps, FakeSchema())
        # 回填幂等：再次执行不产生重复事件。
        mod.backfill_events(django_apps, FakeSchema())

        self.assertEqual(BusinessEvent.objects.count(), 2)
        self.assertEqual(
            list(BusinessEvent.objects.order_by("sequence").values_list("sequence", flat=True)),
            [1, 2],
        )
        self.assertEqual(EventSequenceCounter.objects.get(pk=1).last_sequence, 2)

        # 新发布事件序号紧接回填最大值。
        record_stock_in(g1, user, Decimal("3"))
        self.assertEqual(EventSequenceCounter.objects.get(pk=1).last_sequence, 3)
        self.assertEqual(BusinessEvent.objects.get(sequence=3).event_type, "stock_in")


class RestartStabilityTest(TestCase):
    """服务重启（仅清进程内状态）后继续拉取，已发布事件不重排。"""

    def test_sequence_survives_process_restart(self):
        user = User.objects.create_user("rs-user", "testpass123", role="admin")
        from apps.warehouse.models import Unit, Category, Variety
        unit = Unit.objects.create(name="件", created_by=user)
        category = Category.objects.create(name="类", unit=unit, created_by=user)
        variety = Variety.objects.create(name="种", category=category, created_by=user)
        goods = Goods.objects.create(variety=variety, name="物", code="RS-1")

        for _ in range(3):
            record_stock_in(goods, user, Decimal("1"))
        before = list(
            BusinessEvent.objects.order_by("sequence").values_list(
                "sequence", "event_type", "source_id"
            )
        )

        # 服务层不持有任何内存序号状态：关闭数据库连接并重新装配即等价于重启。
        connections.close_all()
        django_setup_again()

        after = list(
            BusinessEvent.objects.order_by("sequence").values_list(
                "sequence", "event_type", "source_id"
            )
        )
        self.assertEqual(before, after)

        # 重启后继续拉取与继续发布：游标不回退，新序号为 4。
        events, next_cursor, has_more, _ = read_events(cursor=3, limit=10)
        self.assertEqual(events, [])
        self.assertEqual(next_cursor, 3)
        event, _ = record_stock_in(goods, user, Decimal("1"))
        self.assertEqual(event.sequence, 4)
        events, _, _, _ = read_events(cursor=3, limit=10)
        self.assertEqual([e.sequence for e in events], [4])


def django_setup_again():
    """重新连接数据库，模拟新进程从持久化状态恢复。"""
    from django.db import connection
    connection.ensure_connection()


class ConcurrentAllocationTest(TransactionTestCase):
    """并发提交下发号稠密、唯一、按提交顺序递增（SQLite 单写 + 锁重试）。"""

    def setUp(self):
        self.user = User.objects.create_user("cc-user", "testpass123", role="admin")
        from apps.warehouse.models import Unit, Category, Variety
        self.unit = Unit.objects.create(name="件", created_by=self.user)
        self.category = Category.objects.create(name="类", unit=self.unit, created_by=self.user)
        self.variety = Variety.objects.create(name="种", category=self.category, created_by=self.user)
        self.goods = Goods.objects.create(variety=self.variety, name="物", code="CC-1")

    def test_concurrent_events_get_dense_unique_sequences(self):
        def publish(i):
            # 每个线程使用独立连接。
            record_stock_in(self.goods, self.user, Decimal("1"),
                            idempotency_key=f"cc-{i}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(publish, range(20)))

        sequences = list(
            BusinessEvent.objects.order_by("sequence").values_list("sequence", flat=True)
        )
        self.assertEqual(len(sequences), 20)
        self.assertEqual(sequences, list(range(1, 21)))
        self.assertEqual(EventSequenceCounter.objects.get(pk=1).last_sequence, 20)

    def test_concurrent_same_idempotency_key_publishes_once(self):
        def publish(_):
            record_stock_in(self.goods, self.user, Decimal("1"),
                            idempotency_key="same-key")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(publish, range(12)))

        self.assertEqual(BusinessEvent.objects.count(), 1)
        self.assertEqual(StockIn.objects.count(), 1)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.quantity, Decimal("1"))
        self.assertEqual(EventSequenceCounter.objects.get(pk=1).last_sequence, 1)
