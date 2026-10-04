#!/usr/bin/env bash
# 端到端调用示例：录入授权 -> 核验 -> 幂等重试 -> 冲突 -> 撤销 -> 追溯
# 用法: ./scripts/demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
JSON='Content-Type: application/json'

echo "== 0. 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 1. 录入授权（影片《山月》/ 1号厅，额度 2）=="
curl -s -X POST "$BASE/licenses" -H "$JSON" -d '{
  "film_title": "山月",
  "venue": "1号厅",
  "valid_from": "2026-10-01T00:00:00Z",
  "valid_to": "2026-10-08T23:59:59Z",
  "max_sessions": 2
}'; echo

echo "== 2. 提交待核验场次（幂等键 fest-0001）=="
curl -s -X POST "$BASE/verifications" -H "$JSON" -d '{
  "film_title": "山月",
  "venue": "1号厅",
  "start_time": "2026-10-05T19:30:00+08:00",
  "idempotency_key": "fest-0001"
}'; echo

echo "== 3. 同一幂等键重试（返回原结果，响应头带 X-Idempotent-Replay: true）=="
curl -s -i -X POST "$BASE/verifications" -H "$JSON" -d '{
  "film_title": "山月",
  "venue": "1号厅",
  "start_time": "2026-10-05T19:30:00+08:00",
  "idempotency_key": "fest-0001"
}' | grep -Ei '^(HTTP|X-Idempotent)|"result"'; echo

echo "== 4. 同一幂等键但内容变化（应返回 409 冲突）=="
curl -s -X POST "$BASE/verifications" -H "$JSON" -d '{
  "film_title": "山月",
  "venue": "1号厅",
  "start_time": "2026-10-06T19:30:00+08:00",
  "idempotency_key": "fest-0001"
}'; echo

echo "== 5. 撤销授权 #1（仅阻止后续核验）=="
curl -s -X POST "$BASE/licenses/1/revoke" -H "$JSON" -d '{"reason": "版权方撤回"}'; echo

echo "== 6. 撤销后再核验（应被拒绝 LICENSE_REVOKED）=="
curl -s -X POST "$BASE/verifications" -H "$JSON" -d '{
  "film_title": "山月",
  "venue": "1号厅",
  "start_time": "2026-10-05T19:30:00+08:00",
  "idempotency_key": "fest-0002"
}'; echo

echo "== 7. 已核准场次仍可追溯 =="
curl -s "$BASE/verifications/fest-0001"; echo

echo "== 8. 授权列表（含已用/剩余额度与撤销状态）=="
curl -s "$BASE/licenses"; echo
