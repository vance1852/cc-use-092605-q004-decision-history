"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .errors import Conflict, InvalidState
from .service import PhotonService


def run() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        database = str(Path(tmp) / "grid_qualification.db")
        service = PhotonService(database)
        service.bootstrap_admin()
        service.auth.create_user("quality-a", "quality-a-pass", "quality")
        service.auth.create_user("quality-b", "quality-b-pass", "quality")
        admin = service.auth.login("admin", "photon-admin")
        service.create_lot(admin, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
        for wavelength, response in ((450, .71), (520, .93), (650, .84)):
            service.add_measurement(admin, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
        first = service.analyze(admin, "LOT-DEMO")
        qa = service.auth.login("quality-a", "quality-a-pass")
        service.approve(qa, "LOT-DEMO", "hold", "振动测点异常，暂缓投运", analysis_version=first["analysis_version"], idempotency_key="op-hold-001")

        # 模拟服务重启：同一数据库文件重新打开后，暂缓意见必须仍在。
        service = PhotonService(database)
        qa = service.auth.login("quality-a", "quality-a-pass")
        qb = service.auth.login("quality-b", "quality-b-pass")
        replayed = service.approve(qa, "LOT-DEMO", "hold", "振动测点异常，暂缓投运", analysis_version=first["analysis_version"], idempotency_key="op-hold-001")
        assert len(replayed["opinions"]) == 1, "客户端重试必须返回原记录而不是新增意见"
        try:
            service.approve(qa, "LOT-DEMO", "release", "同一编号但结论不同", idempotency_key="op-hold-001")
        except Conflict:
            pass
        else:
            raise AssertionError("编号相同但内容变化必须被拒绝")

        # 补充检测产生新的分析版本；结论变更必须走复议并由另一名有权人员处理。
        for wavelength, response in ((480, .82), (610, .88)):
            service.add_measurement(admin, "LOT-DEMO", wavelength, response, .01, "spectrometer-2")
        second = service.analyze(admin, "LOT-DEMO")
        assert second["analysis_version"] > first["analysis_version"]
        try:
            service.approve(qb, "LOT-DEMO", "release", "绕过复议直接放行")
        except InvalidState:
            pass
        else:
            raise AssertionError("未经复议不得直接改变结论")
        reconsideration = service.request_reconsideration(qb, "LOT-DEMO", second["analysis_version"], "补充检测完成，申请复核暂缓结论", idempotency_key="op-recon-001")
        try:
            service.resolve_reconsideration(qb, reconsideration["reconsideration_id"], "release", "自己复议自己")
        except PermissionError:
            pass
        else:
            raise AssertionError("复议不得由提请人自行处理")
        service.resolve_reconsideration(qa, reconsideration["reconsideration_id"], "release", "新分析版本确认异常消除，准予投运", idempotency_key="op-release-001")

        # 再次模拟重启：暂缓到放行的全过程必须可以还原。
        service = PhotonService(database)
        admin = service.auth.login("admin", "photon-admin")
        lot = service.get_lot(admin, "LOT-DEMO")
        assert [o["decision"] for o in lot["opinions"]] == ["hold", "release"]
        assert lot["opinions"][0]["analysis_version"] == first["analysis_version"]
        assert lot["opinions"][1]["analysis_version"] == second["analysis_version"]
        assert lot["current_conclusion"]["decision"] == "release"
        assert lot["reconsiderations"][0]["status"] == "resolved"
        assert lot["reconsiderations"][0]["opinion_id"] == lot["opinions"][1]["opinion_id"]
        return {
            "status": "ok",
            "lot": lot["lot_id"],
            "peak": first["spectrum"]["peak_wavelength_nm"],
            "events": len(service.audit(admin, "LOT-DEMO")),
            "opinions": [o["decision"] for o in lot["opinions"]],
            "current": lot["current_conclusion"]["decision"],
        }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
