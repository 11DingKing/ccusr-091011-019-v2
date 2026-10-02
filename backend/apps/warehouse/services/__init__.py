"""
业务事件发布与按提交顺序读取的服务层。

设计要点：
1. 所有事件共用一张 wh_business_event 表，sequence 是全局稠密递增序号，
   在业务写事务内分配，与业务记录同提交同回滚。
2. 发号采用“先 UPDATE 自增、再读回”，而不是“先 SELECT MAX 再 INSERT”：
   - PostgreSQL/MySQL：UPDATE 持有行锁，并发事务在锁上排队，提交后读到新值；
   - SQLite：不支持 SELECT FOR UPDATE，但全库同一时刻只有一个写事务，
     UPDATE 计数器会在写锁上排队（配合 settings 中的 timeout 与外层重试）。
   因此同一毫秒内提交的多个事务也能得到互不相同、按提交顺序递增的序号。
3. 序号与事件负载一旦提交即不可变，不依赖应用时钟，也不依赖任何内存状态，
   服务重启后继续拉取不会重排已发布事件。
4. 读取侧的权限/类型裁剪只做“过滤”，永远不重新编号；游标是全局绝对序号，
   边界（最早/最新序号）按全表计算，不受权限裁剪影响。
"""
import time
from decimal import Decimal

from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F, Max, Min
from django.utils import timezone

from ..models import (
    BusinessEvent, EventSequenceCounter, StockCorrection, StockFreeze, StockIn,
    StockOut,
    EVENT_TYPE_STOCK_IN, EVENT_TYPE_RELEASE, EVENT_TYPE_FREEZE,
    EVENT_TYPE_CORRECTION,
)

COUNTER_ID = 1
# SQLite 在写锁竞争或提交升级锁时可能抛 "database is locked"，整事务重试。
MAX_WRITE_RETRIES = 20


class BusinessError(ValueError):
    """业务校验错误，视图层映射为 400。"""


class _IdempotencyConflict(Exception):
    """同幂等键事件已被并发事务提交：必须整体回滚后重试。"""


class StaleCursor(Exception):
    """游标已落在可保留窗口之外。"""

    def __init__(self, cursor, earliest_sequence, latest_sequence):
        self.cursor = cursor
        self.earliest_sequence = earliest_sequence
        self.latest_sequence = latest_sequence
        super().__init__(f"cursor {cursor} is older than {earliest_sequence}")


# ==================== 发号 ====================

def _allocate_sequence():
    """分配下一个事件序号。必须在事务内调用。

    UPDATE 先于任何对计数器的 SELECT：自增在数据库侧完成，
    避免“读后写”在并发下拿到相同序号。
    """
    updated = EventSequenceCounter.objects.filter(pk=COUNTER_ID).update(
        last_sequence=F('last_sequence') + 1
    )
    if not updated:
        # 兜底：发号器行尚未初始化（正常由迁移创建）。
        EventSequenceCounter.objects.create(pk=COUNTER_ID, last_sequence=1)
    return EventSequenceCounter.objects.get(pk=COUNTER_ID).last_sequence


def _run_tx_with_retry(work):
    """在事务中执行 work；锁冲突或同键并发时整体重试。

    重试保证“业务写 + 事件写”同在一次提交中：同幂等键的并发输家整事务
    回滚后重试，入口预检命中赢家已提交的事件，不再重复应用业务变更。
    """
    last_error = None
    for attempt in range(MAX_WRITE_RETRIES):
        try:
            with transaction.atomic():
                return work()
        except _IdempotencyConflict:
            # 重试事务：预检将命中既有事件。
            last_error = None
            continue
        except OperationalError as exc:
            last_error = exc
            if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
                raise
            time.sleep(min(0.05 * (attempt + 1), 0.5))
    if last_error is not None:
        raise last_error
    raise RuntimeError('unreachable retry state')


def _publish_event(event_type, source_table, source_id, goods, operator, payload,
                   idempotency_key=None):
    """在当前事务内创建业务事件并分配序号。返回 (event, created)。"""
    if idempotency_key:
        existing = BusinessEvent.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            return existing, False

    sequence = _allocate_sequence()
    # 并发同幂等键：预检与插入之间可能已有同行提交（PostgreSQL 等多版本库）。
    # 用 savepoint 接住唯一约束冲突，确认同键事件已存在后抛出
    # _IdempotencyConflict，使整个业务事务回滚（避免“业务生效但无事件”），
    # 再由 _run_tx_with_retry 整事务重试并在入口预检命中既有事件。
    sid = transaction.savepoint()
    try:
        event = BusinessEvent.objects.create(
            sequence=sequence,
            event_type=event_type,
            source_table=source_table,
            source_id=source_id,
            goods=goods,
            operator=operator,
            payload=payload,
            idempotency_key=idempotency_key,
        )
    except IntegrityError:
        transaction.savepoint_rollback(sid)
        if not idempotency_key:
            raise
        existing = BusinessEvent.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existing is None:
            raise
        raise _IdempotencyConflict(idempotency_key)
    else:
        transaction.savepoint_commit(sid)
    return event, True


# ==================== 四类业务命令（业务写与事件发布同事务） ====================

def _goods_payload(goods, stock_after=None, **extra):
    payload = {
        'goods_id': goods.id,
        'goods_code': goods.code,
        'goods_name': goods.name,
    }
    if stock_after is not None:
        payload['stock_after'] = str(stock_after)
    payload.update(extra)
    return payload


def _check_positive_quantity(quantity):
    if quantity is None or quantity <= Decimal('0'):
        raise BusinessError('数量必须大于0')
    return quantity


def record_stock_in(goods, operator, quantity, batch_no='', supplier='', remark='',
                    idempotency_key=None):
    """入库：登记入库单、增加库存、发布 stock_in 事件（同一事务）。"""
    quantity = _check_positive_quantity(quantity)

    def work():
        if idempotency_key:
            duplicate = BusinessEvent.objects.filter(
                idempotency_key=idempotency_key
            ).first()
            if duplicate is not None:
                return duplicate, False

        record = StockIn.objects.create(
            goods=goods, operator=operator, quantity=quantity,
            batch_no=batch_no or '', supplier=supplier or '', remark=remark or '',
        )
        goods.refresh_from_db()
        goods.quantity = F('quantity') + quantity
        goods.save(update_fields=['quantity'])
        goods.refresh_from_db()

        event, created = _publish_event(
            EVENT_TYPE_STOCK_IN, 'wh_stock_in', record.id, goods, operator,
            _goods_payload(
                goods, stock_after=goods.quantity,
                quantity=str(quantity), batch_no=batch_no or '',
                supplier=supplier or '', remark=remark or '',
            ),
            idempotency_key=idempotency_key,
        )
        return event, created

    return _run_tx_with_retry(work)


def record_release(goods, operator, quantity, receiver, receiver_dept='', remark='',
                   idempotency_key=None):
    """放行（出库）：登记已完成出库单、扣减可用库存、发布 release 事件。"""
    quantity = _check_positive_quantity(quantity)
    if not receiver:
        raise BusinessError('领用人不能为空')

    def work():
        if idempotency_key:
            duplicate = BusinessEvent.objects.filter(
                idempotency_key=idempotency_key
            ).first()
            if duplicate is not None:
                return duplicate, False

        goods.refresh_from_db()
        available = goods.quantity - goods.frozen_quantity
        if quantity > available:
            raise BusinessError(
                f'可放行库存不足：当前可用 {available}，申请放行 {quantity}'
            )

        record = StockOut.objects.create(
            goods=goods, operator=operator, receiver=receiver,
            receiver_dept=receiver_dept or '', quantity=quantity,
            status='completed', stock_out_time=timezone.now(), remark=remark or '',
        )
        goods.quantity = F('quantity') - quantity
        goods.save(update_fields=['quantity'])
        goods.refresh_from_db()

        event, created = _publish_event(
            EVENT_TYPE_RELEASE, 'wh_stock_out', record.id, goods, operator,
            _goods_payload(
                goods, stock_after=goods.quantity,
                quantity=str(quantity), receiver=receiver,
                receiver_dept=receiver_dept or '', remark=remark or '',
            ),
            idempotency_key=idempotency_key,
        )
        return event, created

    return _run_tx_with_retry(work)


def record_freeze(goods, operator, action, quantity, reason='', idempotency_key=None):
    """冻结/解冻：登记冻结记录、调整冻结数量、发布 freeze 事件。"""
    quantity = _check_positive_quantity(quantity)
    if action not in ('freeze', 'unfreeze'):
        raise BusinessError('冻结操作类型无效')

    def work():
        if idempotency_key:
            duplicate = BusinessEvent.objects.filter(
                idempotency_key=idempotency_key
            ).first()
            if duplicate is not None:
                return duplicate, False

        goods.refresh_from_db()
        if action == 'freeze':
            available = goods.quantity - goods.frozen_quantity
            if quantity > available:
                raise BusinessError(
                    f'可冻结库存不足：当前可用 {available}，申请冻结 {quantity}'
                )
            goods.frozen_quantity = F('frozen_quantity') + quantity
        else:
            if quantity > goods.frozen_quantity:
                raise BusinessError(
                    f'解冻数量不能大于已冻结数量 {goods.frozen_quantity}'
                )
            goods.frozen_quantity = F('frozen_quantity') - quantity
        goods.save(update_fields=['frozen_quantity'])
        goods.refresh_from_db()

        record = StockFreeze.objects.create(
            goods=goods, operator=operator, action=action,
            quantity=quantity, reason=reason or '',
        )

        event, created = _publish_event(
            EVENT_TYPE_FREEZE, 'wh_stock_freeze', record.id, goods, operator,
            _goods_payload(
                goods,
                action=action, quantity=str(quantity), reason=reason or '',
                frozen_after=str(goods.frozen_quantity),
                available_after=str(goods.quantity - goods.frozen_quantity),
            ),
            idempotency_key=idempotency_key,
        )
        return event, created

    return _run_tx_with_retry(work)


def record_correction(goods, operator, quantity_after, reason='check', remark='',
                      idempotency_key=None):
    """库存更正：登记前后数量、直接订正库存、发布 correction 事件。"""
    if quantity_after is None or quantity_after < Decimal('0'):
        raise BusinessError('更正后数量不能为负')
    if reason not in dict(StockCorrection.REASON_CHOICES):
        raise BusinessError('更正原因无效')

    def work():
        if idempotency_key:
            duplicate = BusinessEvent.objects.filter(
                idempotency_key=idempotency_key
            ).first()
            if duplicate is not None:
                return duplicate, False

        goods.refresh_from_db()
        quantity_before = goods.quantity

        record = StockCorrection.objects.create(
            goods=goods, operator=operator,
            quantity_before=quantity_before, quantity_after=quantity_after,
            reason=reason, remark=remark or '',
        )
        goods.quantity = quantity_after
        goods.save(update_fields=['quantity'])

        event, created = _publish_event(
            EVENT_TYPE_CORRECTION, 'wh_stock_correction', record.id, goods, operator,
            _goods_payload(
                goods, stock_after=quantity_after,
                quantity_before=str(quantity_before),
                quantity_after=str(quantity_after),
                delta=str(quantity_after - quantity_before),
                reason=reason, remark=remark or '',
            ),
            idempotency_key=idempotency_key,
        )
        return event, created

    return _run_tx_with_retry(work)


# ==================== 按提交顺序读取（游标分页） ====================

def get_event_boundary():
    """全局事件边界，不受任何权限/类型裁剪影响。无事件时返回 (None, None)。"""
    agg = BusinessEvent.objects.aggregate(
        earliest=Min('sequence'), latest=Max('sequence')
    )
    return agg['earliest'], agg['latest']


def read_events(cursor, limit, event_types=None, operator_id=None):
    """按提交顺序读取一页事件。

    - cursor：消费者已确认的最大序号（首页传 0 或不传）；
    - event_types/operator_id 只做行级过滤，绝不参与编号，因此游标保持单调；
    - 返回 (events, next_cursor, has_more, (earliest, latest))。
    - 游标早于全局最早序号时抛 StaleCursor，携带可恢复边界。
    """
    earliest, latest = get_event_boundary()

    # 过期判定：cursor 与保留窗口起点之间存在“被清除的空洞”，即
    # cursor 的下一条事件序号 cursor+1 已经小于 earliest，说明消费者
    # 必然漏掉过事件，必须显式对齐。cursor == earliest-1 时下一条正好
    # 是 earliest，连续无空洞，是合法的恢复点（故用严格小于）。
    # 未发生裁剪（earliest=1）时，cursor=0 即从头订阅，不算过期。
    if earliest is not None and cursor < earliest - 1:
        raise StaleCursor(cursor, earliest, latest)

    queryset = BusinessEvent.objects.filter(sequence__gt=cursor)
    if event_types:
        queryset = queryset.filter(event_type__in=event_types)
    if operator_id is not None:
        queryset = queryset.filter(operator_id=operator_id)

    queryset = queryset.select_related('goods', 'operator').order_by('sequence')
    rows = list(queryset[:limit + 1])
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = page[-1].sequence if page else cursor
    return page, next_cursor, has_more, (earliest, latest)
