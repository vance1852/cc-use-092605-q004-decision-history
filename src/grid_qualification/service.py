"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import json
import sqlite3
import uuid

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .errors import Conflict, InvalidState
from .storage import canonical_json, connect, content_digest, event, transaction, utcnow

DECISION_STATUS = {"release": "released", "hold": "hold", "reject": "rejected"}


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "photon-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, wafer_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if wafer_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)", (lot_id, product, process_rev, wafer_count, "engineering", actor.user_id, now, now))
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        return self._lot_view(lot_id)

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(wavelength_nm), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "wavelength_nm": wavelength_nm})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def analyze(self, token: str, lot_id: str) -> dict:
        actor = self.auth.require(token, "analyze")
        rows = self.db.execute("SELECT wavelength_nm,response FROM measurements WHERE lot_id=? ORDER BY wavelength_nm", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        summary = summarize_spectrum([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(self._lot_view(lot_id)["wafer_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        result = {"lot_id": lot_id, "spectrum": summary.__dict__, "yield": rates, "response_ci": ci}
        digest = content_digest(result)
        with transaction(self.db):
            latest = self.db.execute("SELECT version,result_sha256 FROM analyses WHERE lot_id=? ORDER BY version DESC LIMIT 1", (lot_id,)).fetchone()
            if latest and latest["result_sha256"] == digest:
                version = latest["version"]
            else:
                version = (latest["version"] if latest else 0) + 1
                self.db.execute("INSERT INTO analyses VALUES(?,?,?,?,?,?)", (lot_id, version, canonical_json(result), digest, actor.user_id, utcnow()))
                event(self.db, lot_id, "analyzed", actor.user_id, {"version": version, "result_sha256": digest})
        return {**result, "analysis_version": version}

    def approve(self, token: str, lot_id: str, decision: str, reason: str, analysis_version: int | None = None, idempotency_key: str | None = None) -> dict:
        actor = self.auth.require(token, "approve")
        self._validate_decision(decision, reason)
        request = {"lot_id": lot_id, "decision": decision, "reason": reason, "analysis_version": analysis_version}
        replay = self._replay(f"approve:{lot_id}", idempotency_key, request)
        if replay is not None:
            return replay
        try:
            with transaction(self.db):
                self._append_opinion(actor.user_id, lot_id, decision, reason, analysis_version, None)
                response = self._lot_view(lot_id)
                self._record_idempotency(f"approve:{lot_id}", idempotency_key, request, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("操作编号冲突") from exc
        return response

    def request_reconsideration(self, token: str, lot_id: str, analysis_version: int | None, reason: str, idempotency_key: str | None = None) -> dict:
        actor = self.auth.require(token, "approve")
        if not reason.strip():
            raise ValueError("reason is required")
        request = {"lot_id": lot_id, "analysis_version": analysis_version, "reason": reason}
        replay = self._replay(f"reconsider:{lot_id}", idempotency_key, request)
        if replay is not None:
            return replay
        try:
            with transaction(self.db):
                if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                    raise KeyError(lot_id)
                current = self._current_opinion(lot_id)
                if current is None:
                    raise InvalidState("批次尚无生效结论，无需复议")
                version = self._resolve_analysis_version(lot_id, analysis_version)
                if version <= current["analysis_version"]:
                    raise InvalidState("复议须针对更新的分析版本")
                reconsideration_id = uuid.uuid4().hex
                self.db.execute(
                    "INSERT INTO reconsiderations(reconsideration_id,lot_id,analysis_version,reason,requested_by,status,created_at) VALUES(?,?,?,?,?,'open',?)",
                    (reconsideration_id, lot_id, version, reason, actor.user_id, utcnow()),
                )
                event(self.db, lot_id, "reconsideration.requested", actor.user_id, {"reconsideration_id": reconsideration_id, "analysis_version": version, "reason": reason})
                response = self._reconsideration_view(reconsideration_id)
                self._record_idempotency(f"reconsider:{lot_id}", idempotency_key, request, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("操作编号冲突") from exc
        return response

    def resolve_reconsideration(self, token: str, reconsideration_id: str, decision: str, reason: str, idempotency_key: str | None = None) -> dict:
        actor = self.auth.require(token, "approve")
        self._validate_decision(decision, reason)
        request = {"reconsideration_id": reconsideration_id, "decision": decision, "reason": reason}
        replay = self._replay(f"resolve:{reconsideration_id}", idempotency_key, request)
        if replay is not None:
            return replay
        try:
            with transaction(self.db):
                record = self.db.execute("SELECT * FROM reconsiderations WHERE reconsideration_id=?", (reconsideration_id,)).fetchone()
                if not record:
                    raise KeyError(reconsideration_id)
                if record["status"] != "open":
                    raise InvalidState("复议事项已处理")
                if record["requested_by"] == actor.user_id:
                    raise PermissionError("复议须由另一名有权人员处理")
                opinion_id = self._append_opinion(actor.user_id, record["lot_id"], decision, reason, record["analysis_version"], reconsideration_id)
                self.db.execute(
                    "UPDATE reconsiderations SET status='resolved',resolved_by=?,resolved_at=?,opinion_id=? WHERE reconsideration_id=?",
                    (actor.user_id, utcnow(), opinion_id, reconsideration_id),
                )
                event(self.db, record["lot_id"], "reconsideration.resolved", actor.user_id, {"reconsideration_id": reconsideration_id, "opinion_id": opinion_id, "decision": decision})
                response = self._lot_view(record["lot_id"])
                self._record_idempotency(f"resolve:{reconsideration_id}", idempotency_key, request, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("操作编号冲突") from exc
        return response

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]

    @staticmethod
    def _validate_decision(decision: str, reason: str) -> None:
        if decision not in DECISION_STATUS or not reason.strip():
            raise ValueError("decision and reason are required")

    def _append_opinion(self, reviewer: str, lot_id: str, decision: str, reason: str, analysis_version: int | None, reconsideration_id: str | None) -> str:
        if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
            raise KeyError(lot_id)
        version = self._resolve_analysis_version(lot_id, analysis_version)
        current = self._current_opinion(lot_id)
        if reconsideration_id is None and current is not None and current["decision"] != decision:
            raise InvalidState("结论变更须通过复议事项处理")
        opinion_id = uuid.uuid4().hex
        now = utcnow()
        self.db.execute(
            "INSERT INTO opinions(opinion_id,lot_id,reviewer,decision,reason,analysis_version,reconsideration_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (opinion_id, lot_id, reviewer, decision, reason, version, reconsideration_id, now),
        )
        self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (DECISION_STATUS[decision], now, lot_id))
        event(self.db, lot_id, "approval", reviewer, {"opinion_id": opinion_id, "decision": decision, "reason": reason, "analysis_version": version, "reconsideration_id": reconsideration_id})
        return opinion_id

    def _resolve_analysis_version(self, lot_id: str, analysis_version: int | None) -> int:
        if analysis_version is None:
            row = self.db.execute("SELECT MAX(version) AS version FROM analyses WHERE lot_id=?", (lot_id,)).fetchone()
            if not row or row["version"] is None:
                raise InvalidState("批次没有可引用的分析版本")
            return row["version"]
        if not self.db.execute("SELECT 1 FROM analyses WHERE lot_id=? AND version=?", (lot_id, analysis_version)).fetchone():
            raise ValueError("analysis version does not exist")
        return int(analysis_version)

    def _current_opinion(self, lot_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM opinions WHERE lot_id=? ORDER BY seq DESC LIMIT 1", (lot_id,)).fetchone()

    def _replay(self, scope: str, key: str | None, request: dict) -> dict | None:
        if not key:
            return None
        row = self.db.execute("SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?", (scope, key)).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != content_digest(request):
            raise Conflict("同一操作编号对应了不同的请求内容")
        return json.loads(row["response_json"])

    def _record_idempotency(self, scope: str, key: str | None, request: dict, response: dict) -> None:
        if not key:
            return
        self.db.execute(
            "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) VALUES(?,?,?,?,?)",
            (scope, key, content_digest(request), canonical_json(response), utcnow()),
        )

    def _lot_view(self, lot_id: str) -> dict:
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        opinions = [
            {
                "opinion_id": r["opinion_id"],
                "reviewer": r["reviewer"],
                "decision": r["decision"],
                "reason": r["reason"],
                "analysis_version": r["analysis_version"],
                "reconsideration_id": r["reconsideration_id"],
                "created_at": r["created_at"],
            }
            for r in self.db.execute("SELECT * FROM opinions WHERE lot_id=? ORDER BY seq", (lot_id,))
        ]
        reconsiderations = [self._reconsideration_view(r["reconsideration_id"]) for r in self.db.execute("SELECT reconsideration_id FROM reconsiderations WHERE lot_id=? ORDER BY seq", (lot_id,))]
        return {
            **dict(row),
            "current_conclusion": opinions[-1] if opinions else None,
            "opinions": opinions,
            "reconsiderations": reconsiderations,
        }

    def _reconsideration_view(self, reconsideration_id: str) -> dict:
        r = self.db.execute("SELECT * FROM reconsiderations WHERE reconsideration_id=?", (reconsideration_id,)).fetchone()
        return {
            "reconsideration_id": r["reconsideration_id"],
            "lot_id": r["lot_id"],
            "analysis_version": r["analysis_version"],
            "reason": r["reason"],
            "requested_by": r["requested_by"],
            "status": r["status"],
            "resolved_by": r["resolved_by"],
            "resolved_at": r["resolved_at"],
            "opinion_id": r["opinion_id"],
            "created_at": r["created_at"],
        }
