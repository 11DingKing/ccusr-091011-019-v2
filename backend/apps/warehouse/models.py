"""
库房管理模型
"""
from django.db import models
from apps.authentication.models import User


# 业务事件类型：入库、放行、冻结、更正
EVENT_TYPE_STOCK_IN = 'stock_in'
EVENT_TYPE_RELEASE = 'release'
EVENT_TYPE_FREEZE = 'freeze'
EVENT_TYPE_CORRECTION = 'correction'
BUSINESS_EVENT_TYPES = [
    (EVENT_TYPE_STOCK_IN, '入库'),
    (EVENT_TYPE_RELEASE, '放行'),
    (EVENT_TYPE_FREEZE, '冻结'),
    (EVENT_TYPE_CORRECTION, '更正'),
]
BUSINESS_EVENT_TYPE_KEYS = {key for key, _ in BUSINESS_EVENT_TYPES}


class Unit(models.Model):
    """单位模型"""
    name = models.CharField('单位名称', max_length=5, unique=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_units', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_unit'
        verbose_name = '单位'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品类"""
        return self.categories.exists()


class Category(models.Model):
    """品类模型"""
    name = models.CharField('品类名称', max_length=10, unique=True)
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT,
        related_name='categories', verbose_name='单位'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_categories', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_category'
        verbose_name = '品类'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品种"""
        return self.varieties.exists()


class Variety(models.Model):
    """品种模型"""
    name = models.CharField('品种名称', max_length=20)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT,
        related_name='varieties', verbose_name='所属品类'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_varieties', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_variety'
        verbose_name = '品种'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        unique_together = ['category', 'name']
    
    def __str__(self):
        return f"{self.category.name} - {self.name}"
    
    @property
    def is_in_stock(self):
        """是否已入库"""
        return self.goods.exists()
    
    @property
    def unit_name(self):
        """获取单位名称"""
        return self.category.unit.name if self.category and self.category.unit else ''


class Goods(models.Model):
    """货物模型"""
    variety = models.ForeignKey(
        Variety, on_delete=models.CASCADE,
        related_name='goods', verbose_name='所属品种'
    )
    name = models.CharField('货物名称', max_length=200)
    code = models.CharField('货物编码', max_length=50, unique=True)
    specification = models.CharField('规格型号', max_length=200, blank=True)
    quantity = models.DecimalField('库存数量', max_digits=12, decimal_places=2, default=0)
    frozen_quantity = models.DecimalField('冻结数量', max_digits=12, decimal_places=2, default=0)
    warning_threshold = models.DecimalField('预警阈值', max_digits=12, decimal_places=2, default=10)
    location = models.CharField('存放位置', max_length=100, blank=True)
    remark = models.TextField('备注', blank=True)
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_goods'
        verbose_name = '货物'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_warning(self):
        """是否预警"""
        return self.quantity <= self.warning_threshold


class StockIn(models.Model):
    """入库记录模型"""
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_ins', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_in_operations', verbose_name='操作人'
    )
    quantity = models.DecimalField('入库数量', max_digits=12, decimal_places=2)
    batch_no = models.CharField('批次号', max_length=50, blank=True)
    supplier = models.CharField('供应商', max_length=200, blank=True)
    stock_in_time = models.DateTimeField('入库时间', auto_now_add=True)
    remark = models.TextField('备注', blank=True)
    
    class Meta:
        db_table = 'wh_stock_in'
        verbose_name = '入库记录'
        verbose_name_plural = verbose_name
        ordering = ['-stock_in_time']
    
    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"


class StockOut(models.Model):
    """出库记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
        ('completed', '已完成'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_outs', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_out_operations', verbose_name='操作人'
    )
    receiver = models.CharField('领用人', max_length=100)
    receiver_dept = models.CharField('领用部门', max_length=100, blank=True)
    quantity = models.DecimalField('出库数量', max_digits=12, decimal_places=2)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    stock_out_time = models.DateTimeField('出库时间', null=True, blank=True)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_stock_out'
        verbose_name = '出库记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"


class Warning(models.Model):
    """预警记录模型"""
    TYPE_CHOICES = [
        ('low_stock', '库存不足'),
        ('expiring', '即将过期'),
        ('expired', '已过期'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='warnings', verbose_name='货物'
    )
    type = models.CharField('预警类型', max_length=20, choices=TYPE_CHOICES)
    message = models.TextField('预警信息')
    is_read = models.BooleanField('是否已读', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_warning'
        verbose_name = '预警记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.get_type_display()}"


class Approval(models.Model):
    """审批记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
    ]
    
    stock_out = models.ForeignKey(
        StockOut, on_delete=models.CASCADE,
        related_name='approvals', verbose_name='出库记录'
    )
    approver = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='approvals', verbose_name='审批人'
    )
    status = models.CharField('审批状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    remark = models.TextField('审批意见', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_approval'
        verbose_name = '审批记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.stock_out} - {self.get_status_display()}"


class StockFreeze(models.Model):
    """冻结记录模型（对在库物资的冻结/解冻操作）"""
    ACTION_CHOICES = [
        ('freeze', '冻结'),
        ('unfreeze', '解冻'),
    ]

    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='freezes', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='freeze_operations', verbose_name='操作人'
    )
    action = models.CharField('操作类型', max_length=20, choices=ACTION_CHOICES)
    quantity = models.DecimalField('冻结数量', max_digits=12, decimal_places=2)
    reason = models.CharField('冻结原因', max_length=200, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_stock_freeze'
        verbose_name = '冻结记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.goods.name} - {self.get_action_display()} - {self.quantity}"


class StockCorrection(models.Model):
    """库存更正记录模型（盘盈盘亏等对库存数量的更正）"""
    REASON_CHOICES = [
        ('check', '盘点更正'),
        ('damage', '损毁核销'),
        ('system', '系统订正'),
        ('other', '其他'),
    ]

    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='corrections', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='correction_operations', verbose_name='操作人'
    )
    quantity_before = models.DecimalField('更正前数量', max_digits=12, decimal_places=2)
    quantity_after = models.DecimalField('更正后数量', max_digits=12, decimal_places=2)
    reason = models.CharField('更正原因', max_length=20, choices=REASON_CHOICES, default='check')
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_stock_correction'
        verbose_name = '库存更正记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.goods.name}: {self.quantity_before} -> {self.quantity_after}"


class BusinessEvent(models.Model):
    """按数据库提交顺序发布的业务事件。

    sequence 为全局稠密递增序号，在事务内分配并与业务变更同事务提交，
    因此可见顺序即提交顺序，不依赖应用时钟（同毫秒提交也可稳定排序），
    服务重启后已发布事件的序号也不会重排。
    """

    sequence = models.BigIntegerField('事件序号', primary_key=True)
    event_type = models.CharField('事件类型', max_length=20, choices=BUSINESS_EVENT_TYPES)
    source_table = models.CharField('来源表', max_length=50)
    source_id = models.BigIntegerField('来源记录ID')
    goods = models.ForeignKey(
        Goods, on_delete=models.PROTECT,
        related_name='business_events', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='business_events', verbose_name='操作人'
    )
    payload = models.JSONField('事件快照', default=dict)
    idempotency_key = models.CharField(
        '幂等键', max_length=100, unique=True, null=True, blank=True
    )
    created_at = models.DateTimeField('入库时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_business_event'
        verbose_name = '业务事件'
        verbose_name_plural = verbose_name
        ordering = ['sequence']
        indexes = [
            models.Index(fields=['event_type', 'sequence'], name='idx_event_type_seq'),
            models.Index(fields=['source_table', 'source_id'], name='idx_event_source'),
        ]

    def __str__(self):
        return f"#{self.sequence} {self.event_type}"


class EventSequenceCounter(models.Model):
    """事件序号发号器（单行表，id 恒为 1）。

    通过行锁串行化序号分配：拿到锁的事务顺序即提交顺序，
    事务回滚时序号一并回滚，保证 sequence 稠密且无空洞。
    """

    last_sequence = models.BigIntegerField('已分配最大序号', default=0)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_event_seq_counter'
        verbose_name = '事件序号发号器'
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"counter@{self.last_sequence}"
