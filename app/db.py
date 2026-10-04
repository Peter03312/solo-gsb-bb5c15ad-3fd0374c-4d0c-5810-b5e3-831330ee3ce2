"""SQLite 数据访问层。

设计要点：
- 单连接 + 进程级写锁，配合 ``BEGIN IMMEDIATE`` 事务，保证
  "查额度 -> 占额度 -> 写记录" 这一串操作的原子性，并发核验不会超额。
- 所有时间统一归一化为 UTC ISO-8601 定宽字符串存储，可直接按字典序比较。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    film_title    TEXT NOT NULL,
    venue         TEXT NOT NULL,
    valid_from    TEXT NOT NULL,          -- UTC ISO-8601
    valid_to      TEXT NOT NULL,          -- UTC ISO-8601
    max_sessions  INTEGER NOT NULL CHECK (max_sessions > 0),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revocations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id  INTEGER NOT NULL REFERENCES licenses (id),
    reason      TEXT,
    revoked_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revocations_license ON revocations (license_id);

CREATE TABLE IF NOT EXISTS verifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,  -- 业务幂等键
    request_hash    TEXT NOT NULL,         -- 请求内容指纹，用于冲突检测
    film_title      TEXT NOT NULL,
    venue           TEXT NOT NULL,
    start_time      TEXT NOT NULL,         -- UTC ISO-8601
    result          TEXT NOT NULL CHECK (result IN ('approved', 'rejected')),
    license_id      INTEGER REFERENCES licenses (id),  -- 命中的授权（拒绝时为空）
    remaining_quota INTEGER,               -- 核验后该授权剩余额度
    reject_reason   TEXT,                  -- 拒绝原因代码
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_license ON verifications (license_id);
CREATE INDEX IF NOT EXISTS idx_verifications_film ON verifications (film_title, venue);
"""


class Database:
    """线程安全的 SQLite 封装（单连接，写操作串行化）。"""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：进程锁 + BEGIN IMMEDIATE，确保核验-占额-落库原子完成。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
