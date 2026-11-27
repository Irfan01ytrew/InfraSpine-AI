"""C06 audio ingestion acceptance on the live-AI review stack (scripts/start_live_review_stack.py).

Synthetic speech WAV → Admin API upload → one-shot local document worker (local faster-whisper)
→ ready/searchable with a time locator → authorized byte-range preview → Employee access check.
With `--ask`, also asks the configured live AI provider and checks the answer cites the audio's
time range (never accepts an uncited answer). Evidence is written without secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from start_live_review_stack import DATABASE, REPO, ROOT, VITE_PORT, database_url, review_runtime  # noqa: E402

BASE = f"http://127.0.0.1:{VITE_PORT}"
CSRF = {"X-Requested-With": "acceptance"}
WAV = REPO / ".verification-local-review-20261028-c11-sqlite-ui/data/c06-synthetic-maintenance.wav"
QUESTION = "Which ticket number was reviewed in the maintenance recording?"


def login(email: str) -> httpx.Client:
    accounts = json.loads((ROOT / "credentials-fixture.json").read_text(encoding="utf-8"))
    client = httpx.Client(base_url=BASE, headers=CSRF, timeout=180)
    response = client.post("/api/auth/login", json={"email": email, "password": accounts[email]["password"]})
    response.raise_for_status()
    return client


async def chunks(document_id: str) -> list[dict]:
    url = database_url().replace("postgresql+asyncpg", "postgresql")
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch("SELECT chunk_index, content, locator FROM document_chunks WHERE document_id = $1 ORDER BY chunk_index", document_id)
    finally:
        await conn.close()
    return [{"chunk_index": r["chunk_index"], "content": r["content"], "locator": json.loads(r["locator"]) if r["locator"] else None} for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ask", action="store_true", help="also ask the configured live AI provider")
    args = parser.parse_args()
    evidence: dict = {"review_url": f"{BASE}/admin/", "database": DATABASE, "llm": "live provider" if args.ask else "not called"}

    admin = login("admin@livereview.test")
    data = WAV.read_bytes()
    evidence["submitted_sha256"] = hashlib.sha256(data).hexdigest()
    upload = admin.post("/api/rag/documents", files={"file": (WAV.name, data, "audio/wav")}, data={"visibility": "shared"})
    evidence["upload_http"] = upload.status_code
    upload.raise_for_status()
    doc = upload.json()
    document_id = doc["id"]
    evidence.update(document_id=document_id, queued_status=doc["status"], file_format=doc["file_format"])

    started = time.monotonic()
    worker = subprocess.run([sys.executable, "-m", "app.worker", "--once", "--concurrency", "1"], cwd=REPO, env=review_runtime(database_url()), capture_output=True, text=True, timeout=900)
    evidence["worker"] = {"exit_code": worker.returncode, "seconds": round(time.monotonic() - started, 1)}

    for _ in range(30):
        state = admin.get(f"/api/rag/documents/{document_id}").json()
        if state["status"] in ("ready", "failed") and state.get("searchable", state["status"] == "ready"):
            break
        time.sleep(2)
    evidence["final_state"] = {k: state.get(k) for k in ("status", "attempts", "chunk_count", "searchable", "error_code")}

    rows = asyncio.run(chunks(document_id))
    evidence["chunks"] = [{"chunk_index": r["chunk_index"], "locator": r["locator"], "transcript_excerpt": r["content"][:240]} for r in rows]

    preview = admin.get(f"/api/rag/documents/{document_id}/media", headers={"Range": "bytes=0-63"})
    evidence["original_preview"] = {"status": preview.status_code, "type": preview.headers.get("content-type"), "range": preview.headers.get("content-range"), "bytes": len(preview.content)}

    employee = login("employee@livereview.test")
    listed = [d["id"] for d in employee.get("/api/rag/documents").json().get("documents", [])]
    evidence["employee_sees_shared_audio"] = document_id in listed

    if args.ask:
        answer = admin.post("/api/rag/chat", json={"message": QUESTION})
        body = answer.json() if answer.headers.get("content-type", "").startswith("application/json") else {}
        citations = body.get("citations", [])
        evidence["answer"] = {
            "status": answer.status_code,
            "provider": body.get("provider"),
            "model": body.get("model"),
            "insufficient_evidence": body.get("insufficient_evidence"),
            "cited_audio": any(c.get("document_id") == document_id for c in citations),
            "citation_locators": [c.get("locator") for c in citations if c.get("document_id") == document_id],
            "answer_excerpt": (body.get("answer") or "")[:400],
        }

    out = ROOT / "evidence" / ("c06-live-audio-acceptance.json" if args.ask else "c06-local-audio-ingestion.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, ensure_ascii=False))
    ok = evidence["final_state"]["status"] == "ready" and rows and preview.status_code == 206
    if args.ask:
        ok = ok and evidence["answer"]["status"] == 200 and evidence["answer"]["cited_audio"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
