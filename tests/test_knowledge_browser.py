"""Opt-in authenticated Chromium acceptance using the disposable PostgreSQL test database.

Build frontend first, then run with KNOWLEDGE_BROWSER_ACCEPTANCE=1 through
scripts/irfanix_postgres_tests.py. Provider/embedding doubles make UI checks deterministic;
these assertions do not measure model quality. No live accounts or data are used.
"""
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import text

from app.main import app
from tests.conftest import ADMIN_EMAIL, PASSWORD, USER_EMAIL, USER2_EMAIL, run_db
from tests.rag_helpers import activate_provider, make_pdf, ready

pytestmark = pytest.mark.skipif(os.environ.get("KNOWLEDGE_BROWSER_ACCEPTANCE") != "1", reason="Opt-in local browser acceptance")
ROOT = Path(__file__).resolve().parents[1]


def test_authenticated_knowledge_workspace(client, fake_provider, tmp_path):
    assert (ROOT / "frontend/dist/index.html").is_file(), "Build frontend before browser acceptance."
    activate_provider(client)
    profile = client.put("/api/admin/deployment/company", json={
        "company_name": "Synthetic Knowledge QA", "industry": "Engineering",
        "brand_name": "InfraSpine QA", "primary_color": "#146B61", "setup_complete": True,
    })
    assert profile.status_code == 200, profile.text
    ready(client, "knowledge-acceptance.pdf", make_pdf("Refund policy: customers can request a refund within 30 days by contacting billing support."), visibility="shared")
    run_db(lambda conn: conn.execute(text("UPDATE users SET role = 'manager' WHERE email = :email"), {"email": USER2_EMAIL}))
    fake_provider.rag_reply = lambda message: (
        "Refund requests are accepted within 30 days [S1]."
        if "SOURCES" in message else
        "Here is a Python example.\n\n```python\ndef add(a, b):\n    return a + b\n```\n\n| Input | Result |\n| --- | ---: |\n| 2 + 3 | 5 |\n\n**Explanation:** addition combines two numbers."
    )
    fixture = tmp_path / "browser-fixture.json"
    fixture.write_text(json.dumps({"accounts": [
        {"role": "admin", "email": ADMIN_EMAIL, "password": PASSWORD},
        {"role": "manager", "email": USER2_EMAIL, "password": PASSWORD},
        {"role": "employee", "email": USER_EMAIL, "password": PASSWORD},
    ]}), encoding="utf-8")
    evidence = ROOT / ".verification-knowledge-mission" / "browser"
    evidence.mkdir(parents=True, exist_ok=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    async def serve():
        await server.serve(sockets=[sock])

    # Run in the TestClient's loop, so application pools/locks never cross event loops.
    future = client.portal.start_task_soon(serve)
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline and not future.done():
            time.sleep(0.05)
        assert server.started, "Synthetic browser API did not start."
        env = {**os.environ, "KNOWLEDGE_BROWSER_FIXTURE": str(fixture), "KNOWLEDGE_BROWSER_EVIDENCE": str(evidence)}
        result = subprocess.run(
            ["node", "scripts/knowledge_browser_acceptance.mjs", f"http://127.0.0.1:{port}/admin/"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=240,
        )
        # Script output contains only checks, never credentials, cookies or model prompts.
        assert result.returncode == 0, (result.stdout + result.stderr)[-7000:]
        report = json.loads((evidence / "report.json").read_text(encoding="utf-8"))
        assert report["passed"] and len(report["roles"]) == 3
    finally:
        server.should_exit = True
        try:
            future.result(timeout=15)
        finally:
            sock.close()
            fixture.unlink(missing_ok=True)
