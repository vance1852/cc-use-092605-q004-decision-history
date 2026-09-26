from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from grid_qualification.errors import Conflict
from grid_qualification.service import PhotonService


class GridQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService()
        self.service.bootstrap_admin()
        self.service.auth.create_user("quality-b", "quality-pass-2", "quality")
        self.service.auth.create_user("tech", "engineer-pass-1", "engineer")
        self.service.auth.create_user("ops", "operator-pass-1", "operator")
        self.admin = self.service.auth.login("admin", "photon-admin")
        self.quality = self.service.auth.login("quality-b", "quality-pass-2")
        self.engineer = self.service.auth.login("tech", "engineer-pass-1")
        self.operator = self.service.auth.login("ops", "operator-pass-1")
        self.service.create_lot(self.admin, "LOT-1", "power module", "P1.0", 10)
        for wavelength, response in ((450, .71), (520, .93), (650, .84)):
            self.service.add_measurement(self.admin, "LOT-1", wavelength, response, .01, "spectrometer-1")

    def tearDown(self) -> None:
        self.service.close()

    def _supplement_and_reanalyze(self) -> dict:
        self.service.add_measurement(self.admin, "LOT-1", 700, .88, .01, "spectrometer-1")
        return self.service.analyze(self.admin, "LOT-1")

    def test_same_reviewer_opinions_are_appended_not_overwritten(self) -> None:
        self.service.analyze(self.admin, "LOT-1")
        self.service.approve(self.admin, "LOT-1", "hold", "first look")
        self.service.approve(self.admin, "LOT-1", "hold", "second look")
        lot = self.service.get_lot(self.admin, "LOT-1")
        self.assertEqual(len(lot["approvals"]), 2)
        self.assertEqual({row["reviewer"] for row in lot["approvals"]}, {"admin"})
        self.assertNotEqual(lot["approvals"][0]["approval_id"], lot["approvals"][1]["approval_id"])
        self.assertEqual(lot["approvals"][0]["reason"], "first look")
        self.assertEqual(lot["current_decision"]["reason"], "second look")

    def test_direct_conclusion_change_is_rejected(self) -> None:
        self.service.analyze(self.admin, "LOT-1")
        self.service.approve(self.admin, "LOT-1", "hold", "hold it")
        self._supplement_and_reanalyze()
        with self.assertRaises(ValueError):
            self.service.approve(self.quality, "LOT-1", "release", "try direct release")
        with self.assertRaises(ValueError):
            self.service.approve(self.admin, "LOT-1", "release", "same reviewer too")
        lot = self.service.get_lot(self.admin, "LOT-1")
        self.assertEqual(len(lot["approvals"]), 1)
        self.assertEqual(lot["current_decision"]["decision"], "hold")
        self.assertEqual(lot["status"], "hold")

    def test_hold_to_release_timeline_keeps_evidence_versions_and_linkage(self) -> None:
        first = self.service.analyze(self.admin, "LOT-1")
        self.service.approve(self.admin, "LOT-1", "hold", "vibration anomaly", idempotency_key="h-1")
        second = self._supplement_and_reanalyze()
        self.assertNotEqual(first["analysis_id"], second["analysis_id"])
        rec = self.service.request_reconsideration(
            self.admin, "LOT-1", second["analysis_id"], "supplementary test done", idempotency_key="r-1"
        )
        lot = self.service.resolve_reconsideration(
            self.quality, rec["reconsideration_id"], "release", "verified by qa-b", idempotency_key="s-1"
        )
        self.assertEqual(lot["status"], "released")
        self.assertEqual(lot["current_decision"]["decision"], "release")
        self.assertEqual(lot["current_decision"]["reviewer"], "quality-b")
        self.assertEqual([row["decision"] for row in lot["approvals"]], ["hold", "release"])
        self.assertEqual(lot["approvals"][0]["analysis_id"], first["analysis_id"])
        self.assertEqual(lot["approvals"][0]["analysis_sha256"], first["input_sha256"])
        self.assertEqual(lot["approvals"][1]["analysis_id"], second["analysis_id"])
        self.assertEqual(lot["approvals"][1]["reconsideration_id"], rec["reconsideration_id"])
        self.assertEqual(lot["reconsiderations"][0]["requested_by"], "admin")
        self.assertEqual(lot["reconsiderations"][0]["resolved_by"], "quality-b")
        self.assertEqual(lot["reconsiderations"][0]["status"], "resolved")
        self.assertEqual(lot["reconsiderations"][0]["approval_id"], lot["approvals"][1]["approval_id"])
        event_types = [row["event_type"] for row in self.service.audit(self.admin, "LOT-1")]
        self.assertIn("approval", event_types)
        self.assertIn("reconsideration_requested", event_types)
        self.assertIn("reconsideration_resolved", event_types)

    def test_reconsideration_guards(self) -> None:
        analysis = self.service.analyze(self.admin, "LOT-1")
        with self.assertRaises(ValueError):
            self.service.request_reconsideration(self.admin, "LOT-1", analysis["analysis_id"], "too early")
        self.service.approve(self.admin, "LOT-1", "hold", "hold it")
        with self.assertRaises(ValueError):
            self.service.request_reconsideration(self.admin, "LOT-1", analysis["analysis_id"], "same version")
        new_analysis = self._supplement_and_reanalyze()
        rec = self.service.request_reconsideration(self.admin, "LOT-1", new_analysis["analysis_id"], "new evidence")
        with self.assertRaises(ValueError):
            self.service.request_reconsideration(self.quality, "LOT-1", new_analysis["analysis_id"], "duplicate")
        with self.assertRaises(PermissionError):
            self.service.resolve_reconsideration(self.admin, rec["reconsideration_id"], "release", "self serve")
        with self.assertRaises(PermissionError):
            self.service.resolve_reconsideration(self.engineer, rec["reconsideration_id"], "release", "no permission")
        done = self.service.resolve_reconsideration(self.quality, rec["reconsideration_id"], "release", "verified")
        self.assertEqual(done["current_decision"]["decision"], "release")
        with self.assertRaises(ValueError):
            self.service.resolve_reconsideration(self.quality, rec["reconsideration_id"], "release", "again")

    def test_idempotent_replay_returns_original_and_change_is_rejected(self) -> None:
        self.service.analyze(self.admin, "LOT-1")
        first = self.service.approve(self.admin, "LOT-1", "hold", "awaiting review", idempotency_key="k-1")
        replay = self.service.approve(self.admin, "LOT-1", "hold", "awaiting review", idempotency_key="k-1")
        self.assertEqual(first, replay)
        self.assertEqual(len(first["approvals"]), 1)
        with self.assertRaises(Conflict):
            self.service.approve(self.admin, "LOT-1", "hold", "different reason", idempotency_key="k-1")
        with self.assertRaises(Conflict):
            self.service.approve(self.admin, "LOT-1", "reject", "different decision", idempotency_key="k-1")
        lot = self.service.get_lot(self.admin, "LOT-1")
        self.assertEqual(len(lot["approvals"]), 1)

    def test_reconsideration_idempotency(self) -> None:
        self.service.analyze(self.admin, "LOT-1")
        self.service.approve(self.admin, "LOT-1", "hold", "hold it", idempotency_key="a-1")
        analysis = self._supplement_and_reanalyze()
        rec1 = self.service.request_reconsideration(self.admin, "LOT-1", analysis["analysis_id"], "new evidence", idempotency_key="r-1")
        rec2 = self.service.request_reconsideration(self.admin, "LOT-1", analysis["analysis_id"], "new evidence", idempotency_key="r-1")
        self.assertEqual(rec1, rec2)
        with self.assertRaises(Conflict):
            self.service.request_reconsideration(self.admin, "LOT-1", analysis["analysis_id"], "changed", idempotency_key="r-1")
        res1 = self.service.resolve_reconsideration(self.quality, rec1["reconsideration_id"], "release", "ok", idempotency_key="s-1")
        res2 = self.service.resolve_reconsideration(self.quality, rec1["reconsideration_id"], "release", "ok", idempotency_key="s-1")
        self.assertEqual(res1, res2)
        with self.assertRaises(Conflict):
            self.service.resolve_reconsideration(self.quality, rec1["reconsideration_id"], "reject", "ok", idempotency_key="s-1")
        lot = self.service.get_lot(self.admin, "LOT-1")
        self.assertEqual(len(lot["approvals"]), 2)
        self.assertEqual(len(lot["reconsiderations"]), 1)

    def test_existing_import_analyze_and_permission_behavior(self) -> None:
        result = self.service.analyze(self.admin, "LOT-1")
        self.assertEqual(result["spectrum"]["peak_wavelength_nm"], 520.0)
        self.assertEqual(result["yield"]["yield"], 0.2)
        again = self.service.analyze(self.admin, "LOT-1")
        self.assertEqual(result["analysis_id"], again["analysis_id"])
        self.assertEqual(result["input_sha256"], again["input_sha256"])
        imported = self.service.add_measurement(self.operator, "LOT-1", 700, .88, .01, "spectrometer-1")
        self.assertIn("measurement_id", imported)
        newer = self.service.analyze(self.admin, "LOT-1")
        self.assertNotEqual(result["analysis_id"], newer["analysis_id"])
        with self.assertRaises(PermissionError):
            self.service.approve(self.operator, "LOT-1", "hold", "no permission")
        with self.assertRaises(PermissionError):
            self.service.approve(self.engineer, "LOT-1", "hold", "no permission")
        with self.assertRaises(PermissionError):
            self.service.analyze(self.operator, "LOT-1")
        with self.assertRaises(PermissionError):
            self.service.get_lot("bad-token", "LOT-1")
        self.service.create_lot(self.admin, "LOT-2", "power module", "P1.0", 5)
        for wavelength, response in ((450, .71), (520, .93)):
            self.service.add_measurement(self.admin, "LOT-2", wavelength, response, .01, "spectrometer-1")
        with self.assertRaises(ValueError):
            self.service.analyze(self.admin, "LOT-2")

    def test_approve_records_current_data_version_without_explicit_analyze(self) -> None:
        lot = self.service.approve(self.admin, "LOT-1", "hold", "direct")
        self.assertIsNotNone(lot["current_decision"]["analysis_id"])
        self.assertIsNotNone(lot["current_decision"]["analysis_sha256"])
        analysis = self.service.analyze(self.admin, "LOT-1")
        explicit = self.service.approve(self.quality, "LOT-1", "hold", "explicit", analysis_id=analysis["analysis_id"])
        self.assertEqual(explicit["current_decision"]["analysis_id"], analysis["analysis_id"])
        with self.assertRaises(KeyError):
            self.service.approve(self.quality, "LOT-1", "hold", "bad ref", analysis_id=9999)
        with self.assertRaises(KeyError):
            self.service.approve(self.admin, "LOT-MISSING", "hold", "no such lot")

    def test_lot_without_opinions_has_empty_timeline(self) -> None:
        lot = self.service.get_lot(self.admin, "LOT-1")
        self.assertIsNone(lot["current_decision"])
        self.assertEqual(lot["approvals"], [])
        self.assertEqual(lot["reconsiderations"], [])

    def test_restart_restores_full_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "grid.sqlite3")
            service = PhotonService(path)
            service.bootstrap_admin()
            service.auth.create_user("quality-b", "quality-pass-2", "quality")
            admin = service.auth.login("admin", "photon-admin")
            service.create_lot(admin, "LOT-R", "module", "P1", 10)
            for wavelength, response in ((450, .71), (520, .93), (650, .84)):
                service.add_measurement(admin, "LOT-R", wavelength, response, .01, "spectrometer-1")
            first = service.analyze(admin, "LOT-R")
            service.approve(admin, "LOT-R", "hold", "vibration anomaly", idempotency_key="hold-1")
            service.add_measurement(admin, "LOT-R", 700, .88, .01, "spectrometer-1")
            second = service.analyze(admin, "LOT-R")
            rec = service.request_reconsideration(admin, "LOT-R", second["analysis_id"], "supplementary data", idempotency_key="rec-1")
            quality = service.auth.login("quality-b", "quality-pass-2")
            service.resolve_reconsideration(quality, rec["reconsideration_id"], "release", "verified", idempotency_key="rel-1")
            service.close()

            restored = PhotonService(path)
            try:
                token = restored.auth.login("admin", "photon-admin")
                lot = restored.get_lot(token, "LOT-R")
                self.assertEqual([row["decision"] for row in lot["approvals"]], ["hold", "release"])
                self.assertEqual(lot["current_decision"]["decision"], "release")
                self.assertEqual(lot["status"], "released")
                self.assertEqual(lot["approvals"][0]["analysis_id"], first["analysis_id"])
                self.assertEqual(lot["approvals"][0]["analysis_sha256"], first["input_sha256"])
                self.assertEqual(lot["approvals"][1]["analysis_id"], second["analysis_id"])
                self.assertEqual(lot["approvals"][1]["reconsideration_id"], rec["reconsideration_id"])
                self.assertEqual(lot["reconsiderations"][0]["status"], "resolved")
                self.assertEqual(lot["reconsiderations"][0]["approval_id"], lot["approvals"][1]["approval_id"])
                replay = restored.approve(token, "LOT-R", "hold", "vibration anomaly", idempotency_key="hold-1")
                # 重启后重试仍返回首次落库的原记录，且不追加新意见
                self.assertEqual(replay["current_decision"]["decision"], "hold")
                self.assertEqual(len(replay["approvals"]), 1)
                self.assertEqual(len(restored.get_lot(token, "LOT-R")["approvals"]), 2)
            finally:
                restored.close()

    def test_legacy_approvals_are_migrated_into_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "grid.sqlite3")
            legacy = sqlite3.connect(path)
            legacy.executescript("""
                CREATE TABLE chip_lots(
                 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
                 wafer_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE approvals(
                 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
                 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
            """)
            legacy.execute(
                "INSERT INTO chip_lots VALUES('LOT-OLD','module','P0',5,'hold','ops','2026-09-20T00:00:00+00:00','2026-09-20T00:00:00+00:00')"
            )
            legacy.execute(
                "INSERT INTO approvals VALUES('LOT-OLD','qa-lead','hold','vibration anomaly','2026-09-21T00:00:00+00:00')"
            )
            legacy.commit()
            legacy.close()

            service = PhotonService(path)
            try:
                service.bootstrap_admin()
                token = service.auth.login("admin", "photon-admin")
                lot = service.get_lot(token, "LOT-OLD")
                self.assertEqual(len(lot["approvals"]), 1)
                self.assertEqual(lot["approvals"][0]["decision"], "hold")
                self.assertEqual(lot["approvals"][0]["reviewer"], "qa-lead")
                self.assertIsNone(lot["approvals"][0]["analysis_id"])
                self.assertEqual(lot["current_decision"]["decision"], "hold")
                with self.assertRaises(ValueError):
                    service.approve(token, "LOT-OLD", "release", "override attempt")
                for wavelength, response in ((450, .71), (520, .93), (650, .84)):
                    service.add_measurement(token, "LOT-OLD", wavelength, response, .01, "spectrometer-1")
                analysis = service.analyze(token, "LOT-OLD")
                service.auth.create_user("qa-2", "quality-pass-2", "quality")
                rec = service.request_reconsideration(token, "LOT-OLD", analysis["analysis_id"], "new evidence")
                other = service.auth.login("qa-2", "quality-pass-2")
                updated = service.resolve_reconsideration(other, rec["reconsideration_id"], "release", "verified")
                self.assertEqual([row["decision"] for row in updated["approvals"]], ["hold", "release"])
                self.assertEqual(updated["current_decision"]["decision"], "release")
                tables = {row[0] for row in service.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertNotIn("approvals_legacy", tables)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
