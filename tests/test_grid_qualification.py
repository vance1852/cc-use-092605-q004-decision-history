from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from grid_qualification.errors import Conflict, InvalidState
from grid_qualification.service import PhotonService


class GridQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.service.auth.create_user("eng", "engineer-pass", "engineer")
        self.service.auth.create_user("qa", "quality-a-pass", "quality")
        self.service.auth.create_user("qb", "quality-b-pass", "quality")
        self.eng = self.service.auth.login("eng", "engineer-pass")
        self.qa = self.service.auth.login("qa", "quality-a-pass")
        self.qb = self.service.auth.login("qb", "quality-b-pass")
        self.service.create_lot(self.eng, "LOT-1", "WTG-6MW", "P1.0", 12)
        for wavelength, response in ((450, 0.71), (520, 0.93), (650, 0.84)):
            self.service.add_measurement(self.eng, "LOT-1", wavelength, response, 0.01, "vibration-1")
        self.v1 = self.service.analyze(self.eng, "LOT-1")["analysis_version"]

    def _supplementary_analysis(self) -> int:
        for wavelength, response in ((480, 0.82), (610, 0.88)):
            self.service.add_measurement(self.eng, "LOT-1", wavelength, response, 0.01, "vibration-2")
        return self.service.analyze(self.eng, "LOT-1")["analysis_version"]

    def test_hold_then_release_keeps_full_timeline(self) -> None:
        self.service.approve(self.qa, "LOT-1", "hold", "振动测点异常，暂缓", analysis_version=self.v1)
        v2 = self._supplementary_analysis()
        reconsideration = self.service.request_reconsideration(self.qa, "LOT-1", v2, "补充检测完成")
        self.service.resolve_reconsideration(self.qb, reconsideration["reconsideration_id"], "release", "异常消除，准予投运")
        lot = self.service.get_lot(self.eng, "LOT-1")
        self.assertEqual(lot["status"], "released")
        self.assertEqual(lot["current_conclusion"]["decision"], "release")
        self.assertEqual([o["decision"] for o in lot["opinions"]], ["hold", "release"])
        hold, release = lot["opinions"]
        self.assertEqual(hold["analysis_version"], self.v1)
        self.assertEqual(hold["reviewer"], "qa")
        self.assertIsNone(hold["reconsideration_id"])
        self.assertEqual(release["analysis_version"], v2)
        self.assertEqual(release["reconsideration_id"], reconsideration["reconsideration_id"])
        record = lot["reconsiderations"][0]
        self.assertEqual(record["status"], "resolved")
        self.assertEqual(record["requested_by"], "qa")
        self.assertEqual(record["resolved_by"], "qb")
        self.assertEqual(record["opinion_id"], release["opinion_id"])
        self.assertEqual(record["analysis_version"], v2)

    def test_same_reviewer_cannot_overwrite_persisted_opinion(self) -> None:
        self.service.approve(self.qa, "LOT-1", "hold", "暂缓", analysis_version=self.v1)
        with self.assertRaises(InvalidState):
            self.service.approve(self.qa, "LOT-1", "release", "同一人直接改结论")
        with self.assertRaises(InvalidState):
            self.service.approve(self.qb, "LOT-1", "release", "未复议直接改结论")
        reaffirmed = self.service.approve(self.qa, "LOT-1", "hold", "维持暂缓")
        self.assertEqual([o["decision"] for o in reaffirmed["opinions"]], ["hold", "hold"])
        first, second = reaffirmed["opinions"]
        self.assertNotEqual(first["opinion_id"], second["opinion_id"])
        self.assertEqual(first["reason"], "暂缓")

    def test_reconsideration_requires_newer_analysis_and_other_approver(self) -> None:
        self.service.approve(self.qa, "LOT-1", "hold", "暂缓", analysis_version=self.v1)
        with self.assertRaises(InvalidState):
            self.service.request_reconsideration(self.qa, "LOT-1", self.v1, "仍是旧版本")
        v2 = self._supplementary_analysis()
        reconsideration = self.service.request_reconsideration(self.qa, "LOT-1", v2, "申请复核")
        with self.assertRaises(PermissionError):
            self.service.resolve_reconsideration(self.qa, reconsideration["reconsideration_id"], "release", "自己处理")
        with self.assertRaises(PermissionError):
            self.service.request_reconsideration(self.eng, "LOT-1", v2, "无权人员")
        self.service.resolve_reconsideration(self.qb, reconsideration["reconsideration_id"], "release", "复核通过")
        with self.assertRaises(InvalidState):
            self.service.resolve_reconsideration(self.qb, reconsideration["reconsideration_id"], "hold", "重复处理")

    def test_reconsideration_needs_existing_conclusion(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.request_reconsideration(self.qa, "LOT-1", self.v1, "尚无论断")

    def test_approve_requires_analysis_evidence(self) -> None:
        self.service.create_lot(self.eng, "LOT-2", "WTG-8MW", "P1.0", 8)
        with self.assertRaises(InvalidState):
            self.service.approve(self.qa, "LOT-2", "hold", "没有分析版本")
        with self.assertRaises(ValueError):
            self.service.approve(self.qa, "LOT-1", "hold", "版本不存在", analysis_version=99)

    def test_idempotent_replay_returns_original_and_conflict_rejected(self) -> None:
        first = self.service.approve(self.qa, "LOT-1", "hold", "暂缓", analysis_version=self.v1, idempotency_key="op-1")
        second = self.service.approve(self.qa, "LOT-1", "hold", "暂缓", analysis_version=self.v1, idempotency_key="op-1")
        self.assertEqual(first, second)
        self.assertEqual(len(second["opinions"]), 1)
        with self.assertRaises(Conflict):
            self.service.approve(self.qa, "LOT-1", "hold", "同一编号内容变了", analysis_version=self.v1, idempotency_key="op-1")
        self.assertEqual(len(self.service.get_lot(self.eng, "LOT-1")["opinions"]), 1)

    def test_reconsideration_flow_idempotency(self) -> None:
        self.service.approve(self.qa, "LOT-1", "hold", "暂缓", analysis_version=self.v1)
        v2 = self._supplementary_analysis()
        created = self.service.request_reconsideration(self.qa, "LOT-1", v2, "申请复核", idempotency_key="re-1")
        replayed = self.service.request_reconsideration(self.qa, "LOT-1", v2, "申请复核", idempotency_key="re-1")
        self.assertEqual(created, replayed)
        with self.assertRaises(Conflict):
            self.service.request_reconsideration(self.qa, "LOT-1", v2, "编号相同理由不同", idempotency_key="re-1")
        resolved = self.service.resolve_reconsideration(self.qb, created["reconsideration_id"], "release", "放行", idempotency_key="rs-1")
        replayed = self.service.resolve_reconsideration(self.qb, created["reconsideration_id"], "release", "放行", idempotency_key="rs-1")
        self.assertEqual(resolved, replayed)
        with self.assertRaises(Conflict):
            self.service.resolve_reconsideration(self.qb, created["reconsideration_id"], "reject", "编号相同结论不同", idempotency_key="rs-1")
        lot = self.service.get_lot(self.eng, "LOT-1")
        self.assertEqual(len(lot["opinions"]), 2)
        self.assertEqual(len(lot["reconsiderations"]), 1)

    def test_analysis_version_reused_when_result_unchanged(self) -> None:
        again = self.service.analyze(self.eng, "LOT-1")
        self.assertEqual(again["analysis_version"], self.v1)
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM analyses").fetchone()[0], 1)
        v2 = self._supplementary_analysis()
        self.assertEqual(v2, self.v1 + 1)

    def test_existing_behaviour_preserved(self) -> None:
        result = self.service.analyze(self.eng, "LOT-1")
        for key in ("lot_id", "spectrum", "yield", "response_ci"):
            self.assertIn(key, result)
        with self.assertRaises(ValueError):
            self.service.analyze(self.eng, "LOT-2")
        with self.assertRaises(PermissionError):
            self.service.approve(self.eng, "LOT-1", "hold", "无审批权限")
        with self.assertRaises(PermissionError):
            self.service.resolve_reconsideration(self.eng, "missing", "release", "无审批权限")
        with self.assertRaises(KeyError):
            self.service.get_lot(self.eng, "LOT-404")
        with self.assertRaises(ValueError):
            self.service.approve(self.qa, "LOT-1", "unknown", "非法结论")

    def test_restart_restores_hold_to_release_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = str(Path(tmp) / "grid.db")
            service = PhotonService(database)
            service.bootstrap_admin()
            service.auth.create_user("qa", "quality-a-pass", "quality")
            service.auth.create_user("qb", "quality-b-pass", "quality")
            admin = service.auth.login("admin", "photon-admin")
            service.create_lot(admin, "LOT-9", "WTG-6MW", "P1.0", 10)
            for wavelength, response in ((450, 0.71), (520, 0.93), (650, 0.84)):
                service.add_measurement(admin, "LOT-9", wavelength, response, 0.01, "vibration-1")
            v1 = service.analyze(admin, "LOT-9")["analysis_version"]
            qa = service.auth.login("qa", "quality-a-pass")
            service.approve(qa, "LOT-9", "hold", "振动异常，暂缓", analysis_version=v1, idempotency_key="k-hold")

            service = PhotonService(database)
            qa = service.auth.login("qa", "quality-a-pass")
            qb = service.auth.login("qb", "quality-b-pass")
            lot = service.get_lot(qa, "LOT-9")
            self.assertEqual([o["decision"] for o in lot["opinions"]], ["hold"])
            replayed = service.approve(qa, "LOT-9", "hold", "振动异常，暂缓", analysis_version=v1, idempotency_key="k-hold")
            self.assertEqual(replayed["opinions"][0]["opinion_id"], lot["opinions"][0]["opinion_id"])
            for wavelength, response in ((480, 0.82), (610, 0.88)):
                service.add_measurement(qa, "LOT-9", wavelength, response, 0.01, "vibration-2")
            v2 = service.analyze(qa, "LOT-9")["analysis_version"]
            reconsideration = service.request_reconsideration(qa, "LOT-9", v2, "补充检测完成")
            service.resolve_reconsideration(qb, reconsideration["reconsideration_id"], "release", "准予投运")

            service = PhotonService(database)
            qa = service.auth.login("qa", "quality-a-pass")
            lot = service.get_lot(qa, "LOT-9")
            self.assertEqual([o["decision"] for o in lot["opinions"]], ["hold", "release"])
            self.assertEqual([o["analysis_version"] for o in lot["opinions"]], [v1, v2])
            self.assertEqual(lot["current_conclusion"]["decision"], "release")
            self.assertEqual(lot["reconsiderations"][0]["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
