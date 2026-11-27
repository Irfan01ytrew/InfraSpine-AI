"""Login and issue a RAG query against the isolated smoke database. No secrets logged."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
PASSWORD = os.environ["SMOKE_PASSWORD"]


def call(path: str, data=None, cookie: str | None = None):
    headers = {"Content-Type": "application/json", "Accept": "application/json", "X-Requested-With": "InfraSpine"}
    if cookie:
        headers["Cookie"] = cookie
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers=headers,
        method="POST" if body else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode() or "null"
            return resp.status, json.loads(raw), resp.headers.get("Set-Cookie")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), None


def main() -> int:
    status, payload, cookie = call("/api/auth/login", {"email": "smoke@example.test", "password": PASSWORD})
    if status != 200:
        print("login_failed status=", status)
        return 1
    cookie_header = cookie.split(";", 1)[0] if cookie else ""
    status, me, _ = call("/api/auth/me", cookie=cookie_header)
    if status != 200:
        print("me_failed status=", status)
        return 1
    print("login_ok")
    status, answer, _ = call(
        "/api/rag/query",
        {"question": "What is InfraSpine?"},
        cookie=cookie_header,
    )
    print("rag_status=", status)
    if status >= 500:
        print("rag_failed")
        return 1
    print("rag_smoke_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
