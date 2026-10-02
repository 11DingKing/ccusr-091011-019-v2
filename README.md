# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 业务事件流接口

`GET /api/events/` 按数据库提交顺序提供入库、放行、冻结、更正四类业务事件，供内部合规工具增量拉取。

- 事件与业务写入在同一事务中落库，`seq` 为持久化的全局单调递增序号（提交顺序），服务重启后接续分配，已发布事件不重排、不复用。
- 请求参数：`cursor`（已消费的最大 seq，默认 0）、`limit`（默认 100，最大 500）、`event_types`（可选，逗号分隔的类型过滤）。
- 响应：`events`（按 seq 升序）、`next_cursor`、`has_more`、`latest_seq`。携带 `next_cursor` 继续拉取即可。
- 权限裁剪（普通用户仅见本人操作的事件）与类型过滤只影响返回内容，`next_cursor` 始终按原始事件流推进，权限变化不会造成遗漏或重排。
- 游标过旧（之后的事件已被保留窗口清理）时返回 `410`，`data` 中含 `min_cursor` / `earliest_seq` / `latest_seq`，携带 `min_cursor` 重新对齐即可恢复。
- 业务侧在 `transaction.atomic()` 内调用 `apps.events.services` 的 `record_stock_in` / `record_release` / `record_freeze` / `record_correction` 记录事件；`purge_events_before(seq)` 用于保留窗口清理。

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
