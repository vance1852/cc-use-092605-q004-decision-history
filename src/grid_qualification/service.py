"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .errors import Conflict
from .storage import connect, event, transaction, utcnow

ALGORITHM_VERSION = "spectrum-1.0"
DECISION_STATUS = {"release": "released", "hold": "hold", "reject": "rejected"}


def _digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def close(self) -> None:
        self.db.close()

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
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        lot = dict(row)
        approvals = [
            dict(r)
            for r in self.db.execute(
                "SELECT a.*, an.input_sha256 AS analysis_sha256 FROM approvals a"
                " LEFT JOIN analyses an ON an.analysis_id=a.analysis_id"
                " WHERE a.lot_id=? ORDER BY a.approval_id",
                (lot_id,),
            ).fetchall()
        ]
        reconsiderations = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM reconsiderations WHERE lot_id=? ORDER BY reconsideration_id",
                (lot_id,),
            ).fetchall()
        ]
        # 批次查询同时给出当前生效结论、各次意见引用的证据版本以及复议关联
        lot["current_decision"] = approvals[-1] if approvals else None
        lot["approvals"] = approvals
        lot["reconsiderations"] = reconsiderations
        return lot

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(wavelength_nm), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "wavelength_nm": wavelength_nm})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def _ensure_analysis(self, lot_id: str, actor_id: str) -> tuple[int, str, dict]:
        """为当前测点集合落库（或复用）一个分析版本，作为审批意见引用的证据版本。"""
        rows = self.db.execute(
            "SELECT measurement_id,wavelength_nm,response,noise,instrument,operator,measured_at"
            " FROM measurements WHERE lot_id=? ORDER BY wavelength_nm,measurement_id",
            (lot_id,),
        ).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        summary = summarize_spectrum([r["wavelength_nm"] for r in rows], [r["response"] for r in rows])
        wafer_count = self.db.execute(
            "SELECT wafer_count FROM chip_lots WHERE lot_id=?", (lot_id,)
        ).fetchone()["wafer_count"]
        rates = yield_rate(wafer_count, sum(1 for r in rows if r["response"] >= 0.8), 0)
        ci = confidence_interval([r["response"] for r in rows])
        result = {"lot_id": lot_id, "spectrum": summary.__dict__, "yield": rates, "response_ci": ci}
        input_sha256 = _digest([dict(r) for r in rows])
        self.db.execute(
            "INSERT OR IGNORE INTO analyses(lot_id,input_sha256,algorithm_version,result_json,created_by,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (lot_id, input_sha256, ALGORITHM_VERSION, json.dumps(result, sort_keys=True), actor_id, utcnow()),
        )
        analysis_id = self.db.execute(
            "SELECT analysis_id FROM analyses WHERE lot_id=? AND input_sha256=?",
            (lot_id, input_sha256),
        ).fetchone()["analysis_id"]
        return analysis_id, input_sha256, result

    def analyze(self, token: str, lot_id: str) -> dict:
        actor = self.auth.require(token, "analyze")
        with transaction(self.db):
            analysis_id, input_sha256, result = self._ensure_analysis(lot_id, actor.user_id)
        return {**result, "analysis_id": analysis_id, "input_sha256": input_sha256}

    def _analysis_for_lot(self, lot_id: str, analysis_id: int) -> None:
        if not self.db.execute(
            "SELECT 1 FROM analyses WHERE lot_id=? AND analysis_id=?", (lot_id, analysis_id)
        ).fetchone():
            raise KeyError(analysis_id)

    def _current_approval(self, lot_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM approvals WHERE lot_id=? ORDER BY approval_id DESC LIMIT 1", (lot_id,)
        ).fetchone()

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict | None:
        row = self.db.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("idempotency key was reused with different content")
        return json.loads(row["response_json"])

    def _store_idempotent(self, scope: str, key: str, request_digest: str, response: dict) -> None:
        self.db.execute(
            "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) VALUES(?,?,?,?,?)",
            (scope, key, request_digest, json.dumps(response, sort_keys=True), utcnow()),
        )

    def approve(
        self,
        token: str,
        lot_id: str,
        decision: str,
        reason: str,
        idempotency_key: str | None = None,
        analysis_id: int | None = None,
    ) -> dict:
        """追加一条审批意见；已落库的意见只增不改，结论变更必须走复议流程。"""
        actor = self.auth.require(token, "approve")
        if decision not in DECISION_STATUS or not reason.strip():
            raise ValueError("decision and reason are required")
        scope = f"approval:{lot_id}"
        request_digest = _digest({
            "action": "approve",
            "lot_id": lot_id,
            "reviewer": actor.user_id,
            "decision": decision,
            "reason": reason,
            "analysis_id": analysis_id,
        })
        if idempotency_key is not None:
            replay = self._idempotent_response(scope, idempotency_key, request_digest)
            if replay is not None:
                return replay
        try:
            with transaction(self.db):
                if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                    raise KeyError(lot_id)
                current = self._current_approval(lot_id)
                if current is not None and current["decision"] != decision:
                    raise ValueError("conclusion changes must go through a reconsideration resolved by another authorized reviewer")
                if analysis_id is None:
                    analysis_id, _, _ = self._ensure_analysis(lot_id, actor.user_id)
                else:
                    self._analysis_for_lot(lot_id, analysis_id)
                now = utcnow()
                cursor = self.db.execute(
                    "INSERT INTO approvals(lot_id,reviewer,decision,reason,analysis_id,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (lot_id, actor.user_id, decision, reason, analysis_id, now),
                )
                approval_id = cursor.lastrowid
                self.db.execute(
                    "UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?",
                    (DECISION_STATUS[decision], now, lot_id),
                )
                event(self.db, lot_id, "approval", actor.user_id, {
                    "approval_id": approval_id,
                    "decision": decision,
                    "reason": reason,
                    "analysis_id": analysis_id,
                })
                response = self.get_lot(token, lot_id)
                if idempotency_key is not None:
                    self._store_idempotent(scope, idempotency_key, request_digest, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("approval conflicts with an existing record") from exc
        return response

    def request_reconsideration(
        self,
        token: str,
        lot_id: str,
        analysis_id: int,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """补充检测后若要改变结论，先创建复议事项并锁定新的分析版本。"""
        actor = self.auth.require(token, "approve")
        if not reason.strip():
            raise ValueError("reason is required")
        scope = f"reconsideration:{lot_id}"
        request_digest = _digest({
            "action": "request_reconsideration",
            "lot_id": lot_id,
            "requester": actor.user_id,
            "analysis_id": analysis_id,
            "reason": reason,
        })
        if idempotency_key is not None:
            replay = self._idempotent_response(scope, idempotency_key, request_digest)
            if replay is not None:
                return replay
        try:
            with transaction(self.db):
                if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                    raise KeyError(lot_id)
                self._analysis_for_lot(lot_id, analysis_id)
                current = self._current_approval(lot_id)
                if current is None:
                    raise ValueError("lot has no effective decision to reconsider")
                if current["analysis_id"] == analysis_id:
                    raise ValueError("reconsideration requires a new analysis version")
                if self.db.execute(
                    "SELECT 1 FROM reconsiderations WHERE lot_id=? AND status='open'", (lot_id,)
                ).fetchone():
                    raise ValueError("an open reconsideration already exists for this lot")
                cursor = self.db.execute(
                    "INSERT INTO reconsiderations(lot_id,analysis_id,reason,requested_by,requested_at,status)"
                    " VALUES(?,?,?,?,?,'open')",
                    (lot_id, analysis_id, reason, actor.user_id, utcnow()),
                )
                reconsideration_id = cursor.lastrowid
                event(self.db, lot_id, "reconsideration_requested", actor.user_id, {
                    "reconsideration_id": reconsideration_id,
                    "analysis_id": analysis_id,
                    "reason": reason,
                })
                response = dict(self.db.execute(
                    "SELECT * FROM reconsiderations WHERE reconsideration_id=?", (reconsideration_id,)
                ).fetchone())
                if idempotency_key is not None:
                    self._store_idempotent(scope, idempotency_key, request_digest, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("reconsideration conflicts with an existing record") from exc
        return response

    def resolve_reconsideration(
        self,
        token: str,
        reconsideration_id: int,
        decision: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """由另一名有权人员针对复议锁定的新分析版本作出结论，并追加审批意见。"""
        actor = self.auth.require(token, "approve")
        if decision not in DECISION_STATUS or not reason.strip():
            raise ValueError("decision and reason are required")
        scope = f"resolution:{reconsideration_id}"
        request_digest = _digest({
            "action": "resolve_reconsideration",
            "reconsideration_id": reconsideration_id,
            "reviewer": actor.user_id,
            "decision": decision,
            "reason": reason,
        })
        if idempotency_key is not None:
            replay = self._idempotent_response(scope, idempotency_key, request_digest)
            if replay is not None:
                return replay
        try:
            with transaction(self.db):
                rec = self.db.execute(
                    "SELECT * FROM reconsiderations WHERE reconsideration_id=?", (reconsideration_id,)
                ).fetchone()
                if rec is None:
                    raise KeyError(reconsideration_id)
                if rec["status"] != "open":
                    raise ValueError("reconsideration is already resolved")
                if rec["requested_by"] == actor.user_id:
                    raise PermissionError("reconsideration must be resolved by another authorized reviewer")
                lot_id = rec["lot_id"]
                now = utcnow()
                cursor = self.db.execute(
                    "INSERT INTO approvals(lot_id,reviewer,decision,reason,analysis_id,reconsideration_id,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (lot_id, actor.user_id, decision, reason, rec["analysis_id"], reconsideration_id, now),
                )
                approval_id = cursor.lastrowid
                self.db.execute(
                    "UPDATE reconsiderations SET status='resolved',resolved_by=?,resolved_at=?,approval_id=?"
                    " WHERE reconsideration_id=?",
                    (actor.user_id, now, approval_id, reconsideration_id),
                )
                self.db.execute(
                    "UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?",
                    (DECISION_STATUS[decision], now, lot_id),
                )
                event(self.db, lot_id, "approval", actor.user_id, {
                    "approval_id": approval_id,
                    "decision": decision,
                    "reason": reason,
                    "analysis_id": rec["analysis_id"],
                    "reconsideration_id": reconsideration_id,
                })
                event(self.db, lot_id, "reconsideration_resolved", actor.user_id, {
                    "reconsideration_id": reconsideration_id,
                    "approval_id": approval_id,
                    "decision": decision,
                })
                response = self.get_lot(token, lot_id)
                if idempotency_key is not None:
                    self._store_idempotent(scope, idempotency_key, request_digest, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("resolution conflicts with an existing record") from exc
        return response

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
