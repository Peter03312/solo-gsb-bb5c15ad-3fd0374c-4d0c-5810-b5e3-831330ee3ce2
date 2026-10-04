"""Pydantic 请求/响应模型与时间归一化工具。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


def normalize_dt(dt: datetime) -> str:
    """归一化为 UTC 定宽 ISO-8601 字符串（可字典序比较）。naive 时间按 UTC 处理。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def utcnow_iso() -> str:
    return normalize_dt(datetime.now(timezone.utc))


# ---------- 请求模型 ----------

class LicenseCreate(BaseModel):
    film_title: str = Field(min_length=1, max_length=200, description="影片名")
    venue: str = Field(min_length=1, max_length=200, description="场馆/影厅")
    valid_from: datetime = Field(description="授权生效时间（ISO-8601）")
    valid_to: datetime = Field(description="授权失效时间（ISO-8601）")
    max_sessions: int = Field(gt=0, le=1_000_000, description="场次上限")

    @field_validator("valid_to")
    @classmethod
    def _window_must_be_positive(cls, v: datetime, info):
        valid_from = info.data.get("valid_from")
        if valid_from is not None and v <= valid_from:
            raise ValueError("valid_to 必须晚于 valid_from")
        return v


class VerificationCreate(BaseModel):
    film_title: str = Field(min_length=1, max_length=200, description="影片名")
    venue: str = Field(min_length=1, max_length=200, description="场馆/影厅")
    start_time: datetime = Field(description="场次开始时间（ISO-8601）")
    idempotency_key: str = Field(min_length=1, max_length=128, description="业务幂等键")


class RevokeCreate(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500, description="撤销原因")


# ---------- 响应模型 ----------

class LicenseView(BaseModel):
    id: int
    film_title: str
    venue: str
    valid_from: str
    valid_to: str
    max_sessions: int
    used_sessions: int = Field(description="已核准消耗的场次")
    remaining_sessions: int = Field(description="剩余额度")
    revoked: bool
    revoked_at: Optional[str] = None
    revoke_reason: Optional[str] = None
    created_at: str


class VerificationView(BaseModel):
    id: int
    idempotency_key: str
    film_title: str
    venue: str
    start_time: str
    result: Literal["approved", "rejected"]
    license_id: Optional[int] = Field(default=None, description="命中的授权 ID")
    remaining_quota: Optional[int] = Field(default=None, description="核验后剩余额度")
    reject_reason: Optional[str] = Field(default=None, description="拒绝原因代码")
    reject_message: Optional[str] = Field(default=None, description="拒绝原因说明")
    created_at: str


class RevocationView(BaseModel):
    id: int
    license_id: int
    reason: Optional[str]
    revoked_at: str
    already_revoked: bool = False
