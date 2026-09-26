"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import ServiceError
from .service import PhotonService


class Handler(BaseHTTPRequestHandler):
    service = PhotonService()
    db_lock = threading.Lock()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "service": "photon-fab"})
        if self.path.startswith("/lots/"):
            try:
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                with self.db_lock:
                    return self._json(200, self.service.get_lot(token, self.path.split("/", 2)[2]))
            except Exception as exc:
                return self._json(400, {"error": str(exc)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        with self.db_lock:
            return self._post_locked()

    def _post_locked(self):
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw) if raw.strip() else {}
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            idempotency_key = body.get("idempotency_key") or self.headers.get("Idempotency-Key")
            parts = self.path.strip("/").split("/")
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["wafer_count"]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                return self._json(201, self.service.add_measurement(token, parts[1], body["wavelength_nm"], body["response"], body.get("noise", 0.0), body["instrument"]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
                return self._json(200, self.service.analyze(token, parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "approvals":
                return self._json(201, self.service.approve(token, parts[1], body["decision"], body["reason"], body.get("analysis_version"), idempotency_key))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "reconsiderations":
                return self._json(201, self.service.request_reconsideration(token, parts[1], body.get("analysis_version"), body["reason"], idempotency_key))
            if len(parts) == 5 and parts[0] == "lots" and parts[2] == "reconsiderations" and parts[4] == "resolve":
                return self._json(200, self.service.resolve_reconsideration(token, parts[3], body["decision"], body["reason"], idempotency_key))
            return self._json(404, {"error": "not found"})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except ServiceError as exc:
            return self._json(exc.status, {"error": str(exc), "code": exc.code})
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
