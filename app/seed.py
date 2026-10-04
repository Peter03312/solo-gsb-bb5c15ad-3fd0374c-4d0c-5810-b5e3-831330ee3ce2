"""演示数据：仅在数据库为空且 SEED_DEMO_DATA=true 时写入，便于开箱体验。"""

from __future__ import annotations

from .db import Database
from .models import normalize_dt
from datetime import datetime, timezone
from . import service

DEMO_LICENSES = [
    # 同一影片同一场馆两份授权，用于演示"最早到期优先消耗"
    {"film_title": "山月", "venue": "1号厅",
     "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-08T23:59:59Z",
     "max_sessions": 2},
    {"film_title": "山月", "venue": "1号厅",
     "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-31T23:59:59Z",
     "max_sessions": 5},
    {"film_title": "海边的电台", "venue": "2号厅",
     "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-15T23:59:59Z",
     "max_sessions": 3},
]


def seed_demo_data(db: Database) -> bool:
    """数据库为空时写入演示授权，返回是否执行了写入。"""
    if db.query_one("SELECT id FROM licenses LIMIT 1") is not None:
        return False
    for lic in DEMO_LICENSES:
        service.create_license(
            db,
            film_title=lic["film_title"],
            venue=lic["venue"],
            valid_from=normalize_dt(datetime.fromisoformat(lic["valid_from"].replace("Z", "+00:00"))),
            valid_to=normalize_dt(datetime.fromisoformat(lic["valid_to"].replace("Z", "+00:00"))),
            max_sessions=lic["max_sessions"],
        )
    return True
