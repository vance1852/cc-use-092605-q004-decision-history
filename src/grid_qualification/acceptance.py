"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json
import os
import tempfile

from .service import PhotonService


def run() -> dict:
    """演示暂缓到放行的完整审批时间线，并验证服务重启后仍能还原全过程。"""
    with tempfile.TemporaryDirectory() as directory:
        database = os.path.join(directory, "grid.sqlite3")
        service = PhotonService(database)
        service.bootstrap_admin()
        service.auth.create_user("quality", "quality-review-1", "quality")
        admin = service.auth.login("admin", "photon-admin")
        quality = service.auth.login("quality", "quality-review-1")
        service.create_lot(admin, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
        for wavelength, response in ((450, .71), (520, .93), (650, .84)):
            service.add_measurement(admin, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
        first = service.analyze(admin, "LOT-DEMO")
        service.approve(admin, "LOT-DEMO", "hold", "awaiting quality review", idempotency_key="demo-hold")
        # 补充检测产生新的分析版本，结论变更创建复议事项并由另一名有权人员处理
        service.add_measurement(admin, "LOT-DEMO", 700, .88, .01, "spectrometer-1")
        second = service.analyze(admin, "LOT-DEMO")
        reconsideration = service.request_reconsideration(
            admin, "LOT-DEMO", second["analysis_id"], "supplementary measurement available", idempotency_key="demo-reconsider"
        )
        service.resolve_reconsideration(
            quality, reconsideration["reconsideration_id"], "release", "verified by second reviewer", idempotency_key="demo-release"
        )
        service.close()
        # 模拟服务重启：重新打开同一数据库后必须还原暂缓到放行的全过程
        restored = PhotonService(database)
        try:
            token = restored.auth.login("admin", "photon-admin")
            lot = restored.get_lot(token, "LOT-DEMO")
            events = len(restored.audit(token, "LOT-DEMO"))
        finally:
            restored.close()
    timeline = [row["decision"] for row in lot["approvals"]]
    ok = (
        timeline == ["hold", "release"]
        and lot["current_decision"]["decision"] == "release"
        and lot["approvals"][0]["analysis_id"] == first["analysis_id"]
        and lot["approvals"][1]["analysis_id"] == second["analysis_id"]
        and lot["approvals"][1]["reconsideration_id"] == reconsideration["reconsideration_id"]
        and lot["reconsiderations"][0]["status"] == "resolved"
    )
    return {
        "status": "ok" if ok else "failed",
        "lot": lot["lot_id"],
        "peak": first["spectrum"]["peak_wavelength_nm"],
        "events": events,
        "current_decision": lot["current_decision"]["decision"],
        "timeline": timeline,
        "analysis_versions": [row["analysis_id"] for row in lot["approvals"]],
        "reconsiderations": len(lot["reconsiderations"]),
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
