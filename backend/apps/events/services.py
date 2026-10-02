"""
业务事件写入、权限裁剪与保留窗口清理
"""
from django.db import IntegrityError, connection, transaction

from .models import BusinessEvent, EventSequence


def _allocate_seq():
    """在调用方事务内分配下一个序号。

    计数器行的写锁(select_for_update)持有到事务结束：SQLite 单写者串行提交，
    MVCC 数据库上后分配序号的事务也必须等待先分配者提交，
    因此序号的分配顺序即数据库提交顺序。
    事务回滚时序号一并回滚，不会向已发布事件流留下空洞。
    """
    row = EventSequence.objects.select_for_update().filter(
        name=EventSequence.BUSINESS_EVENT).first()
    if row is None:
        try:
            with transaction.atomic():
                row = EventSequence.objects.create(
                    name=EventSequence.BUSINESS_EVENT, value=0)
        except IntegrityError:
            # 并发事务已创建计数器行，重新加锁读取
            row = EventSequence.objects.select_for_update().get(
                name=EventSequence.BUSINESS_EVENT)
    row.value += 1
    row.save(update_fields=['value'])
    return row.value


def record_event(event_type, *, goods_id=None, biz_id='', actor=None, payload=None):
    """在业务事务内记录一条事件，返回带稳定序号的 BusinessEvent。

    必须与业务写入处于同一个 transaction.atomic() 中：
    业务提交则事件可见，业务回滚则事件消失，二者不存在中间态。
    """
    if event_type not in BusinessEvent.TYPE_VALUES:
        raise ValueError(f'未知事件类型: {event_type}')
    if not connection.in_atomic_block:
        raise RuntimeError('record_event 必须在 transaction.atomic() 内调用')
    seq = _allocate_seq()
    return BusinessEvent.objects.create(
        seq=seq,
        event_type=event_type,
        goods_id=goods_id,
        biz_id=biz_id,
        actor_id=getattr(actor, 'id', None),
        payload=payload or {},
    )


def record_stock_in(stock_in, actor):
    """入库事件"""
    return record_event(
        BusinessEvent.TYPE_STOCK_IN,
        goods_id=stock_in.goods_id,
        biz_id=f'stock_in:{stock_in.pk}',
        actor=actor,
        payload={
            'quantity': str(stock_in.quantity),
            'batch_no': stock_in.batch_no,
            'supplier': stock_in.supplier,
        },
    )


def record_release(stock_out, actor):
    """放行事件（出库审批通过并完成）"""
    return record_event(
        BusinessEvent.TYPE_RELEASE,
        goods_id=stock_out.goods_id,
        biz_id=f'stock_out:{stock_out.pk}',
        actor=actor,
        payload={
            'quantity': str(stock_out.quantity),
            'receiver': stock_out.receiver,
            'receiver_dept': stock_out.receiver_dept,
        },
    )


def record_freeze(goods, actor, reason=''):
    """冻结事件"""
    return record_event(
        BusinessEvent.TYPE_FREEZE,
        goods_id=goods.pk,
        biz_id=f'goods:{goods.pk}',
        actor=actor,
        payload={'reason': reason},
    )


def record_correction(*, goods_id, actor, changes, biz_id=''):
    """更正事件，changes 记录字段前后值，如 {'quantity': ['12', '10']}"""
    return record_event(
        BusinessEvent.TYPE_CORRECTION,
        goods_id=goods_id,
        biz_id=biz_id or f'goods:{goods_id}',
        actor=actor,
        payload={'changes': changes},
    )


def user_can_view(user, event):
    """权限裁剪规则：管理员全量可见，普通用户仅可见本人操作产生的事件"""
    if getattr(user, 'is_admin', False):
        return True
    return event.actor_id is not None and event.actor_id == user.id


def purge_events_before(seq):
    """清理 seq 以下的历史事件（保留窗口）。

    只删除事件行，计数器不回退：已发布序号永不复用。
    清理后持有过旧游标的消费者会在拉取时收到 410 与可恢复边界。
    """
    deleted, _ = BusinessEvent.objects.filter(seq__lt=seq).delete()
    return deleted
