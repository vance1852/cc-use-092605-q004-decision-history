"""芯片批次和测量记录的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 wafer_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 wavelength_nm REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
CREATE TABLE IF NOT EXISTS analyses(
 lot_id TEXT NOT NULL, version INTEGER NOT NULL,
 result_json TEXT NOT NULL, result_sha256 TEXT NOT NULL,
 created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(lot_id,version));
CREATE TABLE IF NOT EXISTS opinions(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 opinion_id TEXT NOT NULL UNIQUE, lot_id TEXT NOT NULL,
 reviewer TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
 analysis_version INTEGER NOT NULL, reconsideration_id TEXT,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reconsiderations(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 reconsideration_id TEXT NOT NULL UNIQUE, lot_id TEXT NOT NULL,
 analysis_version INTEGER NOT NULL, reason TEXT NOT NULL,
 requested_by TEXT NOT NULL, status TEXT NOT NULL,
 resolved_by TEXT, resolved_at TEXT, opinion_id TEXT,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency_keys(
 scope TEXT NOT NULL, key TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(scope,key));
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    db.commit()
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute("INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)", (lot_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()))


def canonical_json(value: object) -> str:
    """生成跨重放一致的紧凑 JSON 文本。"""

    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def content_digest(value: object) -> str:
    """计算规范化请求内容的摘要，用于识别同一操作编号下的内容变化。"""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
