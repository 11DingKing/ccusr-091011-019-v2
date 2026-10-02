from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='BusinessEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('seq', models.BigIntegerField(db_index=True, unique=True, verbose_name='事件序号')),
                ('event_type', models.CharField(choices=[('stock_in', '入库'), ('release', '放行'), ('freeze', '冻结'), ('correction', '更正')], db_index=True, max_length=20, verbose_name='事件类型')),
                ('goods_id', models.BigIntegerField(blank=True, db_index=True, null=True, verbose_name='货物ID')),
                ('biz_id', models.CharField(blank=True, max_length=100, verbose_name='业务单据标识')),
                ('actor_id', models.BigIntegerField(blank=True, db_index=True, null=True, verbose_name='操作人ID')),
                ('payload', models.JSONField(default=dict, verbose_name='事件快照')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
            ],
            options={
                'verbose_name': '业务事件',
                'verbose_name_plural': '业务事件',
                'db_table': 'evt_business_event',
                'ordering': ['seq'],
            },
        ),
        migrations.CreateModel(
            name='EventSequence',
            fields=[
                ('name', models.CharField(max_length=50, primary_key=True, serialize=False, verbose_name='序列名称')),
                ('value', models.BigIntegerField(default=0, verbose_name='当前序号')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
            ],
            options={
                'verbose_name': '事件序号',
                'verbose_name_plural': '事件序号',
                'db_table': 'evt_sequence',
            },
        ),
    ]
