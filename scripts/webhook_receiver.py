"""Local test receiver for webhooks on the review stack (http://127.0.0.1:63099/hooks/infraspine).

Records each call's headers and raw body (base64) to .verification-live-review-20261029/logs/webhook-receiver.jsonl
and serves the recorded calls at GET /calls (CORS-open for the local admin UI) so a signature can be checked in the
browser. Answers 500 while the file `fail` exists next to the log, to exercise retries. Loopback only; test use only.

    python scripts/webhook_receiver.py
"""

from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOGS = Path(__file__).resolve().parents[1] / ".verification-live-review-20261029" / "logs"
RECORD = LOGS / "webhook-receiver.jsonl"
FAIL_FLAG = LOGS / "webhook-receiver.fail"
MAX_BODY = 256 * 1024


class Handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:5182")

    def do_POST(self) -> None:  # noqa: N802
        length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
        body = self.rfile.read(length)
        status = 500 if FAIL_FLAG.exists() else 200
        entry = {"path": self.path, "status": status, "headers": {k: v for k, v in self.headers.items() if k.lower().startswith("x-infraspine") or k.lower() in ("content-type", "user-agent")},
                 "body_b64": base64.b64encode(body).decode()}
        with RECORD.open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry) + "\n")
        self.send_response(status)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/calls":
            self.send_response(404)
            self.end_headers()
            return
        calls = [json.loads(line) for line in RECORD.read_text(encoding="utf-8").splitlines()] if RECORD.exists() else []
        payload = json.dumps(calls[-50:]).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # quiet
        pass


if __name__ == "__main__":
    LOGS.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", 63099), Handler).serve_forever()
