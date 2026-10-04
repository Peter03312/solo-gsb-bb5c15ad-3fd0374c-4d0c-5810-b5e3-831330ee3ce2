# 电影节放映许可核验服务

基于 **Python + FastAPI + SQLite** 的放映许可核验服务：录入影片放映授权（场馆、有效时段、场次上限、撤销记录），对待核验场次按**业务幂等键**返回准入结果、命中授权、剩余额度与拒绝原因。

## 功能特性

- **授权管理**：录入影片 / 场馆 / 有效时段 / 场次上限；查询列表与剩余额度；撤销授权并记录原因。
- **场次核验**：提交影片、场馆、开始时间与幂等键，返回 `approved / rejected`、命中授权 ID、剩余额度、拒绝原因（代码 + 中文说明）。
- **授权选择**：多份授权同时有效时只消耗一份额度，**优先消耗最早到期且有余额**的授权。
- **并发安全**：核验-占额-落库在 `BEGIN IMMEDIATE` 事务内完成，并发核验不会超额（有测试覆盖）。
- **幂等**：同一幂等键重试原样返回首次结果（响应头 `X-Idempotent-Replay: true`）；同键不同内容返回 **409 冲突**。
- **撤销语义**：撤销仅阻止后续核验，已核准场次保留且可按幂等键 / 授权 ID 追溯。
- **明确拒绝**：时间格式非法返回 422；无授权 / 时间不在窗口 / 授权已撤销 / 额度用尽分别返回对应拒绝原因。

## 快速开始（Docker Compose 一键启动，含初始化）

```bash
docker compose up --build -d
```

- 服务地址：**http://localhost:8000**
- 交互式 API 文档：**http://localhost:8000/docs**
- 首次启动自动完成 SQLite 建表初始化；Compose 默认 `SEED_DEMO_DATA=true`，会写入 3 条演示授权（《山月》1号厅 两份、《海边的电台》2号厅 一份），可直接体验。
- 数据持久化在命名卷 `festival-data` 中。

停止并清理：

```bash
docker compose down        # 保留数据卷
docker compose down -v     # 连同数据一起删除
```

## 本地开发

```bash
pip install -r requirements-dev.txt

# 启动服务（默认 ./data/festival.db，端口 8000）
uvicorn app.main:app --reload --port 8000

# 运行测试（含并发不超额用例）
pytest -q

# 端到端调用示例（需服务已启动）
./scripts/demo.sh http://localhost:8000
```

## 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DB_PATH` | 容器内 `/data/festival.db`，本地 `./data/festival.db` | SQLite 数据库文件路径 |
| `SEED_DEMO_DATA` | `false`（Compose 中为 `true`） | 数据库为空时写入演示授权 |
| `PORT` | `8000` | 容器内监听端口（Dockerfile CMD 使用） |

对外端口由 Compose `ports` 映射控制，默认 `8000:8000`；改左侧宿主机端口即可，例如 `"8080:8000"` 后访问 `http://localhost:8080`。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/licenses` | 录入放映授权 |
| GET | `/licenses` | 授权列表（含已用 / 剩余额度、撤销状态），支持 `film_title`、`venue` 过滤 |
| GET | `/licenses/{id}` | 授权详情 |
| POST | `/licenses/{id}/revoke` | 撤销授权（幂等，重复撤销返回 `already_revoked: true`） |
| POST | `/verifications` | 核验放映场次（幂等） |
| GET | `/verifications` | 核验记录列表，支持 `film_title`、`venue`、`license_id`、`result` 过滤 |
| GET | `/verifications/{idempotency_key}` | 按幂等键查询核验记录 |

## 调用示例

### 1. 录入授权

```bash
curl -X POST http://localhost:8000/licenses \
  -H 'Content-Type: application/json' \
  -d '{
    "film_title": "山月",
    "venue": "1号厅",
    "valid_from": "2026-10-01T00:00:00Z",
    "valid_to": "2026-10-08T23:59:59Z",
    "max_sessions": 2
  }'
```

```json
{"id": 1, "film_title": "山月", "venue": "1号厅", "max_sessions": 2,
 "used_sessions": 0, "remaining_sessions": 2, "revoked": false, "...": "..."}
```

### 2. 核验场次（带时区的时间会自动归一化为 UTC）

```bash
curl -X POST http://localhost:8000/verifications \
  -H 'Content-Type: application/json' \
  -d '{
    "film_title": "山月",
    "venue": "1号厅",
    "start_time": "2026-10-05T19:30:00+08:00",
    "idempotency_key": "fest-0001"
  }'
```

```json
{"id": 1, "idempotency_key": "fest-0001", "result": "approved",
 "license_id": 1, "remaining_quota": 1, "reject_reason": null, "...": "..."}
```

### 3. 幂等重试与冲突

```bash
# 同样内容重试 -> 200，返回首次结果，响应头 X-Idempotent-Replay: true
curl -i -X POST http://localhost:8000/verifications -H 'Content-Type: application/json' \
  -d '{"film_title":"山月","venue":"1号厅","start_time":"2026-10-05T19:30:00+08:00","idempotency_key":"fest-0001"}'

# 同一幂等键但内容变化 -> 409
curl -X POST http://localhost:8000/verifications -H 'Content-Type: application/json' \
  -d '{"film_title":"山月","venue":"1号厅","start_time":"2026-10-06T19:30:00+08:00","idempotency_key":"fest-0001"}'
# {"detail":"幂等键 'fest-0001' 已存在，但请求内容不一致","code":"IDEMPOTENCY_CONFLICT", ...}
```

### 4. 撤销授权与追溯

```bash
# 撤销（仅阻止后续核验）
curl -X POST http://localhost:8000/licenses/1/revoke \
  -H 'Content-Type: application/json' -d '{"reason": "版权方撤回"}'

# 撤销后新核验被拒绝
curl -X POST http://localhost:8000/verifications -H 'Content-Type: application/json' \
  -d '{"film_title":"山月","venue":"1号厅","start_time":"2026-10-05T19:30:00+08:00","idempotency_key":"fest-0002"}'
# {"result":"rejected","reject_reason":"LICENSE_REVOKED","reject_message":"覆盖该时段的授权均已被撤销", ...}

# 已核准场次保留可追溯
curl http://localhost:8000/verifications/fest-0001
```

## 拒绝原因代码

| 代码 | 含义 |
|---|---|
| `NO_LICENSE` | 该影片在该场馆没有任何授权记录 |
| `TIME_OUT_OF_RANGE` | 场次开始时间不在任何授权的有效时段内 |
| `LICENSE_REVOKED` | 覆盖该时段的授权均已被撤销 |
| `QUOTA_EXHAUSTED` | 覆盖该时段的授权场次额度均已用尽 |

时间格式无法解析时由请求校验直接返回 **422**。

## 设计说明

- **并发不超额**：核验的 "查重 → 选授权 → 占额度 → 落库" 整体包裹在进程锁 + SQLite `BEGIN IMMEDIATE` 事务中；`idempotency_key` 有唯一约束兜底。`tests/test_api.py::test_concurrent_verifications_never_exceed_quota` 以 20 并发抢 3 额度验证恰好 3 笔核准。
- **授权选择**：候选授权按 `(valid_to, id)` 升序遍历，命中第一份未撤销且剩余额度 > 0 的授权，即"最早到期且有余额优先"，单次核验只消耗一份额度。
- **幂等实现**：请求内容（影片 / 场馆 / 归一化后的开始时间）计算 SHA-256 指纹随记录存储；同键同内容重放原记录，同键不同内容报 409。
- **可追溯**：核验记录（含拒绝记录）永久保留，撤销只写入 `revocations` 表，不回溯已核准场次。

## 项目结构

```
app/
  main.py      # FastAPI 路由与装配（create_app）
  service.py   # 核验 / 授权 / 撤销核心业务逻辑
  db.py        # SQLite 连接、schema 初始化、写事务
  models.py    # Pydantic 模型与时间归一化
  seed.py      # 演示数据（SEED_DEMO_DATA=true 时写入）
tests/test_api.py  # 9 个业务规则测试
scripts/demo.sh    # 端到端 curl 示例
Dockerfile / docker-compose.yml
```
