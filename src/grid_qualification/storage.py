"""芯片批次、测量记录和审批时间线的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

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
CREATE TABLE IF NOT EXISTS analyses(
 analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 input_sha256 TEXT NOT NULL, algorithm_version TEXT NOT NULL,
 result_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(lot_id,input_sha256));
CREATE TABLE IF NOT EXISTS approvals(
 approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 reviewer TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('release','hold','reject')),
 reason TEXT NOT NULL,
 analysis_id INTEGER REFERENCES analyses(analysis_id),
 reconsideration_id INTEGER REFERENCES reconsiderations(reconsideration_id),
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS approvals_lot_timeline ON approvals(lot_id,approval_id);
CREATE TABLE IF NOT EXISTS reconsiderations(
 reconsideration_id INTEGER PRIMARY KEY AUTOINCREMENT,
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 analysis_id INTEGER NOT NULL REFERENCES analyses(analysis_id),
 reason TEXT NOT NULL, requested_by TEXT NOT NULL, requested_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','resolved')),
 resolved_by TEXT, resolved_at TEXT,
 approval_id INTEGER REFERENCES approvals(approval_id));
CREATE UNIQUE INDEX IF NOT EXISTS one_open_reconsideration_per_lot
 ON reconsiderations(lot_id) WHERE status='open';
CREATE TABLE IF NOT EXISTS idempotency_keys(
 scope TEXT NOT NULL, key TEXT NOT NULL,
 request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(scope,key));
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_legacy_approvals(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='approvals'"
    ).fetchone()
    if row is None:
        return False
    columns = {r[1] for r in db.execute("PRAGMA table_info(approvals)").fetchall()}
    return "approval_id" not in columns


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # check_same_thread=False：HTTP 入口在工作线程中复用同一连接，并由 api 层的锁串行化
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    legacy = _has_legacy_approvals(db)
    if legacy:
        # 旧结构按 (lot_id,reviewer) 覆盖已落库意见，先改名再迁入仅追加的新结构
        db.execute("ALTER TABLE approvals RENAME TO approvals_legacy")
    db.executescript(SCHEMA)
    if legacy:
        with transaction(db):
            db.execute(
                "INSERT INTO approvals(lot_id,reviewer,decision,reason,created_at)"
                " SELECT lot_id,reviewer,decision,reason,created_at FROM approvals_legacy"
            )
            db.execute("DROP TABLE approvals_legacy")
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
