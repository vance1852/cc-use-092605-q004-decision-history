"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import Conflict
from .service import PhotonService


class Handler(BaseHTTPRequestHandler):
    service = PhotonService()
    # 单连接服务在请求线程间串行执行，避免并发的 SQLite 调用
    lock = threading.Lock()

    def do_GET(self):
        with self.lock:
            self._get()

    def do_POST(self):
        with self.lock:
            self._post()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _get(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "service": "photon-fab"})
        if self.path.startswith("/lots/"):
            try:
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                return self._json(200, self.service.get_lot(token, self.path.split("/", 2)[2]))
            except Exception as exc:
                return self._json(400, {"error": str(exc)})
        return self._json(404, {"error": "not found"})

    def _post(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["wafer_count"]))
            if self.path.startswith("/lots/") and self.path.endswith("/measurements"):
                lot_id = self.path.split("/")[2]
                return self._json(201, self.service.add_measurement(token, lot_id, body["wavelength_nm"], body["response"], body.get("noise", 0.0), body["instrument"]))
            if self.path.startswith("/lots/") and self.path.endswith("/analysis"):
                return self._json(200, self.service.analyze(token, self.path.split("/")[2]))
            if self.path.startswith("/lots/") and self.path.endswith("/approvals"):
                lot_id = self.path.split("/")[2]
                return self._json(201, self.service.approve(token, lot_id, body["decision"], body["reason"], body.get("idempotency_key"), body.get("analysis_id")))
            if self.path.startswith("/lots/") and self.path.endswith("/reconsiderations"):
                lot_id = self.path.split("/")[2]
                return self._json(201, self.service.request_reconsideration(token, lot_id, body["analysis_id"], body["reason"], body.get("idempotency_key")))
            if self.path.startswith("/reconsiderations/") and self.path.endswith("/resolve"):
                reconsideration_id = int(self.path.split("/")[2])
                return self._json(201, self.service.resolve_reconsideration(token, reconsideration_id, body["decision"], body["reason"], body.get("idempotency_key")))
            return self._json(404, {"error": "not found"})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except Conflict as exc:
            return self._json(409, {"error": str(exc)})
        except Exception as exc:
            return self._json(400, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = PhotonService(args.database)
    Handler.service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
