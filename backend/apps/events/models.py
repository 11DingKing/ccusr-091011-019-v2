"""
业务事件流模型

事件与业务写入在同一数据库事务中落库，序号(seq)由计数器行在事务内分配，
计数器行的写锁持有到事务结束，因此 seq 的顺序即数据库提交顺序。
事件行只增不改，序号持久化于数据库，服务重启后继续分配，
已发布事件不会重排、序号不会复用。
"""
from django.db import models


class EventSequence(models.Model):
    """事件序号计数器（单行），与业务事件在同一事务中递增"""

    BUSINESS_EVENT = 'business_event'

    name = models.CharField('序列名称', max_length=50, primary_key=True)
    value = models.BigIntegerField('当前序号', default=0)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'evt_sequence'
        verbose_name = '事件序号'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.name}={self.value}"


class BusinessEvent(models.Model):
    """业务事件（只增不改的提交日志）"""

    TYPE_STOCK_IN = 'stock_in'
    TYPE_RELEASE = 'release'
    TYPE_FREEZE = 'freeze'
    TYPE_CORRECTION = 'correction'
    TYPE_CHOICES = [
        (TYPE_STOCK_IN, '入库'),
        (TYPE_RELEASE, '放行'),
        (TYPE_FREEZE, '冻结'),
        (TYPE_CORRECTION, '更正'),
    ]
    TYPE_VALUES = {choice for choice, _ in TYPE_CHOICES}

    seq = models.BigIntegerField('事件序号', unique=True, db_index=True)
    event_type = models.CharField('事件类型', max_length=20, choices=TYPE_CHOICES, db_index=True)
    goods_id = models.BigIntegerField('货物ID', null=True, blank=True, db_index=True)
    biz_id = models.CharField('业务单据标识', max_length=100, blank=True)
    actor_id = models.BigIntegerField('操作人ID', null=True, blank=True, db_index=True)
    payload = models.JSONField('事件快照', default=dict)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'evt_business_event'
        verbose_name = '业务事件'
        verbose_name_plural = verbose_name
        ordering = ['seq']

    def __str__(self):
        return f"#{self.seq} {self.get_event_type_display()}"
