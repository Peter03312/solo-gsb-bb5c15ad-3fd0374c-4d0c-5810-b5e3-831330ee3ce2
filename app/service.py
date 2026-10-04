"""核心业务逻辑：授权管理与放映核验。"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from .db import Database
from .models import utcnow_iso

# 拒绝原因代码
REASON_NO_LICENSE = "NO_LICENSE"                # 影片+场馆完全无授权
REASON_TIME_OUT_OF_RANGE = "TIME_OUT_OF_RANGE"  # 有授权但均不覆盖该场次时间
REASON_LICENSE_REVOKED = "LICENSE_REVOKED"      # 覆盖该时段的授权均已撤销
REASON_QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"      # 覆盖该时段的授权额度均已用尽

REASON_MESSAGES = {
    REASON_NO_LICENSE: "该影片在该场馆没有任何授权记录",
    REASON_TIME_OUT_OF_RANGE: "场次开始时间不在任何授权的有效时段内",
    REASON_LICENSE_REVOKED: "覆盖该时段的授权均已被撤销",
    REASON_QUOTA_EXHAUSTED: "覆盖该时段的授权场次额度均已用尽",
}


class IdempotencyConflictError(Exception):
    """同一幂等键提交了不同内容的请求。"""

    def __init__(self, key: str):
        super().__init__(f"幂等键 {key!r} 已存在，但请求内容不一致")
        self.key = key


class LicenseNotFoundError(Exception):
    pass


def _request_hash(film_title: str, venue: str, start_time_iso: str) -> str:
    payload = json.dumps(
        {"film_title": film_title, "venue": venue, "start_time": start_time_iso},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------- 授权 ----------

def create_license(db: Database, *, film_title: str, venue: str,
                   valid_from: str, valid_to: str, max_sessions: int) -> dict:
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO licenses (film_title, venue, valid_from, valid_to, max_sessions, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (film_title, venue, valid_from, valid_to, max_sessions, utcnow_iso()),
        )
        row = conn.execute("SELECT * FROM licenses WHERE id = ?", (cur.lastrowid,)).fetchone()
    return license_view(db, row["id"])


def license_view(db: Database, license_id: int) -> dict:
    row = db.query_one("SELECT * FROM licenses WHERE id = ?", (license_id,))
    if row is None:
        raise LicenseNotFoundError(license_id)
    used = db.query_one(
        "SELECT COUNT(*) AS c FROM verifications WHERE license_id = ? AND result = 'approved'",
        (license_id,),
    )["c"]
    revocation = db.query_one(
        "SELECT * FROM revocations WHERE license_id = ? ORDER BY id DESC LIMIT 1",
        (license_id,),
    )
    return {
        "id": row["id"],
        "film_title": row["film_title"],
        "venue": row["venue"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "max_sessions": row["max_sessions"],
        "used_sessions": used,
        "remaining_sessions": max(row["max_sessions"] - used, 0),
        "revoked": revocation is not None,
        "revoked_at": revocation["revoked_at"] if revocation else None,
        "revoke_reason": revocation["reason"] if revocation else None,
        "created_at": row["created_at"],
    }


def list_licenses(db: Database, film_title: Optional[str] = None,
                  venue: Optional[str] = None) -> list[dict]:
    sql = "SELECT id FROM licenses WHERE 1=1"
    params: list = []
    if film_title:
        sql += " AND film_title = ?"
        params.append(film_title)
    if venue:
        sql += " AND venue = ?"
        params.append(venue)
    sql += " ORDER BY id"
    return [license_view(db, r["id"]) for r in db.query(sql, params)]


def revoke_license(db: Database, license_id: int, reason: Optional[str]) -> dict:
    """撤销授权：只影响后续核验，已核准的场次记录保留可追溯。"""
    with db.transaction() as conn:
        lic = conn.execute("SELECT id FROM licenses WHERE id = ?", (license_id,)).fetchone()
        if lic is None:
            raise LicenseNotFoundError(license_id)
        existing = conn.execute(
            "SELECT * FROM revocations WHERE license_id = ? ORDER BY id DESC LIMIT 1",
            (license_id,),
        ).fetchone()
        if existing is not None:
            return {
                "id": existing["id"],
                "license_id": license_id,
                "reason": existing["reason"],
                "revoked_at": existing["revoked_at"],
                "already_revoked": True,
            }
        cur = conn.execute(
            "INSERT INTO revocations (license_id, reason, revoked_at) VALUES (?, ?, ?)",
            (license_id, reason, utcnow_iso()),
        )
        row = conn.execute("SELECT * FROM revocations WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {
            "id": row["id"],
            "license_id": license_id,
            "reason": row["reason"],
            "revoked_at": row["revoked_at"],
            "already_revoked": False,
        }


# ---------- 核验 ----------

def verify_screening(db: Database, *, film_title: str, venue: str,
                     start_time_iso: str, idempotency_key: str) -> tuple[dict, bool]:
    """核验一个待放映场次。

    返回 (核验记录, 是否幂等重放)。整个 "查重 -> 选授权 -> 占额度 -> 落库"
    在单个 BEGIN IMMEDIATE 事务中完成，并发下不会超额。
    """
    req_hash = _request_hash(film_title, venue, start_time_iso)
    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM verifications WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != req_hash:
                raise IdempotencyConflictError(idempotency_key)
            return _verification_dict(existing), True  # 幂等重放，原样返回

        result = "rejected"
        reject_reason: Optional[str] = None
        chosen_license_id: Optional[int] = None
        remaining_quota: Optional[int] = None

        licenses = conn.execute(
            "SELECT * FROM licenses WHERE film_title = ? AND venue = ? ORDER BY id",
            (film_title, venue),
        ).fetchall()

        if not licenses:
            reject_reason = REASON_NO_LICENSE
        else:
            covering = [
                lic for lic in licenses
                if lic["valid_from"] <= start_time_iso <= lic["valid_to"]
            ]
            if not covering:
                reject_reason = REASON_TIME_OUT_OF_RANGE
            else:
                revoked_ids = {
                    r["license_id"]
                    for r in conn.execute(
                        "SELECT license_id FROM revocations WHERE license_id IN (%s)"
                        % ",".join("?" * len(covering)),
                        [lic["id"] for lic in covering],
                    ).fetchall()
                }
                active = [lic for lic in covering if lic["id"] not in revoked_ids]
                if not active:
                    reject_reason = REASON_LICENSE_REVOKED
                else:
                    # 多份授权同时有效时，优先消耗"最早到期且有余额"的那份
                    active.sort(key=lambda lic: (lic["valid_to"], lic["id"]))
                    for lic in active:
                        used = conn.execute(
                            "SELECT COUNT(*) AS c FROM verifications"
                            " WHERE license_id = ? AND result = 'approved'",
                            (lic["id"],),
                        ).fetchone()["c"]
                        if used < lic["max_sessions"]:
                            result = "approved"
                            chosen_license_id = lic["id"]
                            remaining_quota = lic["max_sessions"] - used - 1
                            reject_reason = None
                            break
                    else:
                        reject_reason = REASON_QUOTA_EXHAUSTED

        conn.execute(
            "INSERT INTO verifications"
            " (idempotency_key, request_hash, film_title, venue, start_time,"
            "  result, license_id, remaining_quota, reject_reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (idempotency_key, req_hash, film_title, venue, start_time_iso,
             result, chosen_license_id, remaining_quota, reject_reason, utcnow_iso()),
        )
        row = conn.execute(
            "SELECT * FROM verifications WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return _verification_dict(row), False


def _verification_dict(row) -> dict:
    reason = row["reject_reason"]
    return {
        "id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "film_title": row["film_title"],
        "venue": row["venue"],
        "start_time": row["start_time"],
        "result": row["result"],
        "license_id": row["license_id"],
        "remaining_quota": row["remaining_quota"],
        "reject_reason": reason,
        "reject_message": REASON_MESSAGES.get(reason) if reason else None,
        "created_at": row["created_at"],
    }


def get_verification(db: Database, idempotency_key: str) -> Optional[dict]:
    row = db.query_one(
        "SELECT * FROM verifications WHERE idempotency_key = ?", (idempotency_key,)
    )
    return _verification_dict(row) if row else None


def list_verifications(db: Database, *, film_title: Optional[str] = None,
                       venue: Optional[str] = None, license_id: Optional[int] = None,
                       result: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM verifications WHERE 1=1"
    params: list = []
    if film_title:
        sql += " AND film_title = ?"
        params.append(film_title)
    if venue:
        sql += " AND venue = ?"
        params.append(venue)
    if license_id is not None:
        sql += " AND license_id = ?"
        params.append(license_id)
    if result:
        sql += " AND result = ?"
        params.append(result)
    sql += " ORDER BY id"
    return [_verification_dict(r) for r in db.query(sql, params)]
