"""业务规则测试：授权选择、幂等、撤销、并发不超额。"""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client():
    app = create_app(db_path=":memory:", seed_demo=False)
    with TestClient(app) as c:
        yield c


def make_license(client, **overrides):
    body = {
        "film_title": "山月",
        "venue": "1号厅",
        "valid_from": "2026-10-01T00:00:00Z",
        "valid_to": "2026-10-08T23:59:59Z",
        "max_sessions": 2,
    }
    body.update(overrides)
    resp = client.post("/licenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def verify(client, key=None, **overrides):
    body = {
        "film_title": "山月",
        "venue": "1号厅",
        "start_time": "2026-10-05T12:00:00Z",
        "idempotency_key": key or f"key-{uuid.uuid4()}",
    }
    body.update(overrides)
    return client.post("/verifications", json=body)


# ---------- 授权与核验 ----------

def test_approve_and_consume_quota(client):
    lic = make_license(client, max_sessions=2)
    r1 = verify(client)
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["result"] == "approved"
    assert d1["license_id"] == lic["id"]
    assert d1["remaining_quota"] == 1
    assert d1["reject_reason"] is None

    d2 = verify(client).json()
    assert d2["remaining_quota"] == 0

    # 第三场超额 -> 拒绝
    d3 = verify(client).json()
    assert d3["result"] == "rejected"
    assert d3["reject_reason"] == "QUOTA_EXHAUSTED"
    assert d3["license_id"] is None


def test_earliest_expiry_license_consumed_first(client):
    early = make_license(client, valid_to="2026-10-08T23:59:59Z", max_sessions=1)
    late = make_license(client, valid_to="2026-10-31T23:59:59Z", max_sessions=5)

    d1 = verify(client).json()
    assert d1["result"] == "approved"
    assert d1["license_id"] == early["id"]  # 最早到期优先

    # 早到期的额度用尽后，才消耗晚到期的
    d2 = verify(client).json()
    assert d2["license_id"] == late["id"]
    assert d2["remaining_quota"] == 4


def test_reject_reasons(client):
    make_license(client, valid_from="2026-10-01T00:00:00Z", valid_to="2026-10-08T23:59:59Z")

    # 完全没有授权
    d = verify(client, film_title="不存在的片", venue="9号厅").json()
    assert d["result"] == "rejected"
    assert d["reject_reason"] == "NO_LICENSE"
    assert d["reject_message"]

    # 时间不在任何授权窗口内
    d = verify(client, start_time="2026-12-01T12:00:00Z").json()
    assert d["reject_reason"] == "TIME_OUT_OF_RANGE"


def test_invalid_time_format_rejected(client):
    resp = verify(client, start_time="not-a-time")
    assert resp.status_code == 422


# ---------- 幂等 ----------

def test_idempotent_retry_returns_same_record(client):
    make_license(client, max_sessions=1)
    key = "order-20261004-0001"
    r1 = verify(client, key=key)
    r2 = verify(client, key=key)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()
    assert r2.headers.get("X-Idempotent-Replay") == "true"

    # 重试不重复消耗额度：下一把新键应被拒绝
    d = verify(client).json()
    assert d["result"] == "rejected"
    assert d["reject_reason"] == "QUOTA_EXHAUSTED"


def test_idempotency_conflict_on_different_payload(client):
    make_license(client)
    key = "order-20261004-0002"
    assert verify(client, key=key).status_code == 200
    resp = verify(client, key=key, start_time="2026-10-06T12:00:00Z")
    assert resp.status_code == 409
    assert resp.json()["code"] == "IDEMPOTENCY_CONFLICT"


# ---------- 撤销 ----------

def test_revocation_blocks_future_but_keeps_history(client):
    lic = make_license(client, max_sessions=2)
    approved = verify(client).json()
    assert approved["result"] == "approved"

    resp = client.post(f"/licenses/{lic['id']}/revoke", json={"reason": "版权方撤回"})
    assert resp.status_code == 200
    assert resp.json()["already_revoked"] is False

    # 撤销后新核验被拒绝
    d = verify(client).json()
    assert d["result"] == "rejected"
    assert d["reject_reason"] == "LICENSE_REVOKED"

    # 已核准记录保留且可追溯
    kept = client.get(f"/verifications/{approved['idempotency_key']}")
    assert kept.status_code == 200
    assert kept.json()["result"] == "approved"
    assert kept.json()["license_id"] == lic["id"]

    # 重复撤销幂等
    again = client.post(f"/licenses/{lic['id']}/revoke", json={"reason": "x"})
    assert again.json()["already_revoked"] is True

    # 授权详情体现撤销状态
    view = client.get(f"/licenses/{lic['id']}").json()
    assert view["revoked"] is True
    assert view["used_sessions"] == 1


def test_revoke_unknown_license_404(client):
    resp = client.post("/licenses/999/revoke", json={})
    assert resp.status_code == 404


# ---------- 并发 ----------

def test_concurrent_verifications_never_exceed_quota(client):
    quota = 3
    make_license(client, max_sessions=quota)
    total = 20

    def do_verify(i):
        # 每个线程独立 client，模拟真实并发请求
        with TestClient(client.app) as c:
            return c.post("/verifications", json={
                "film_title": "山月",
                "venue": "1号厅",
                "start_time": "2026-10-05T12:00:00Z",
                "idempotency_key": f"concurrent-{i}",
            }).json()

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(do_verify, range(total)))

    approved = [r for r in results if r["result"] == "approved"]
    rejected = [r for r in results if r["result"] == "rejected"]
    assert len(approved) == quota
    assert len(rejected) == total - quota
    assert all(r["reject_reason"] == "QUOTA_EXHAUSTED" for r in rejected)
    # 剩余额度序列应精确为 2,1,0，无超额
    assert sorted(r["remaining_quota"] for r in approved) == [0, 1, 2]

    lic = client.get("/licenses/1").json()
    assert lic["used_sessions"] == quota
    assert lic["remaining_sessions"] == 0
