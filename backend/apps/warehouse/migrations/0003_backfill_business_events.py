"""回填历史入库/已放行记录为业务事件，并初始化发号器。"""
from decimal import Decimal

from django.db import migrations


def build_stock_in_events(apps):
    BusinessEvent = apps.get_model('warehouse', 'BusinessEvent')
    StockIn = apps.get_model('warehouse', 'StockIn')
    rows = (
        StockIn.objects.select_related('goods', 'operator')
        .order_by('stock_in_time', 'id')
    )
    events = []
    for row in rows:
        events.append({
            'sort_key': (row.stock_in_time, 0, row.id),
            'event_type': 'stock_in',
            'source_table': 'wh_stock_in',
            'source_id': row.id,
            'goods': row.goods,
            'operator': row.operator,
            'payload': {
                'goods_id': row.goods_id,
                'goods_code': row.goods.code if row.goods else None,
                'goods_name': row.goods.name if row.goods else None,
                'quantity': str(row.quantity),
                'batch_no': row.batch_no or '',
                'supplier': row.supplier or '',
                'remark': row.remark or '',
                'backfilled': True,
            },
        })
    return events


def build_release_events(apps):
    StockOut = apps.get_model('warehouse', 'StockOut')
    rows = (
        StockOut.objects.filter(status='completed')
        .select_related('goods', 'operator')
        .order_by('stock_out_time', 'created_at', 'id')
    )
    events = []
    for row in rows:
        sort_ts = row.stock_out_time or row.created_at
        events.append({
            'sort_key': (sort_ts, 1, row.id),
            'event_type': 'release',
            'source_table': 'wh_stock_out',
            'source_id': row.id,
            'goods': row.goods,
            'operator': row.operator,
            'payload': {
                'goods_id': row.goods_id,
                'goods_code': row.goods.code if row.goods else None,
                'goods_name': row.goods.name if row.goods else None,
                'quantity': str(row.quantity),
                'receiver': row.receiver,
                'receiver_dept': row.receiver_dept or '',
                'remark': row.remark or '',
                'backfilled': True,
            },
        })
    return events


def backfill_events(apps, schema_editor):
    BusinessEvent = apps.get_model('warehouse', 'BusinessEvent')
    EventSequenceCounter = apps.get_model('warehouse', 'EventSequenceCounter')

    # 全新库或已经回填过（发号器已存在）时不重复回填。
    if EventSequenceCounter.objects.filter(pk=1).exists():
        return

    event_specs = build_stock_in_events(apps) + build_release_events(apps)
    event_specs.sort(key=lambda item: item['sort_key'])

    sequence = 0
    to_create = []
    for spec in event_specs:
        sequence += 1
        to_create.append(BusinessEvent(
            sequence=sequence,
            event_type=spec['event_type'],
            source_table=spec['source_table'],
            source_id=spec['source_id'],
            goods=spec['goods'],
            operator=spec['operator'],
            payload=spec['payload'],
        ))
    if to_create:
        BusinessEvent.objects.bulk_create(to_create, batch_size=500)

    EventSequenceCounter.objects.create(pk=1, last_sequence=sequence)


def clear_events(apps, schema_editor):
    BusinessEvent = apps.get_model('warehouse', 'BusinessEvent')
    EventSequenceCounter = apps.get_model('warehouse', 'EventSequenceCounter')
    BusinessEvent.objects.all().delete()
    EventSequenceCounter.objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('warehouse', '0002_eventsequencecounter_goods_frozen_quantity_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_events, clear_events),
    ]
