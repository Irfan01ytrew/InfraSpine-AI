"""Disposable HTTP owner service with durable jobs and an idempotency ledger.

This is controlled fault injection, not an installed production integration.
SQLite holds the independent service's queue; Module 07 always uses PostgreSQL.
"""
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from app.recovery.adapters import Observation


class Lab:
    def __init__(self, directory):
        self.path = str(directory / "owner-service.sqlite")
        self.fail_recovery = False
        with sqlite3.connect(self.path) as db:
            db.executescript("CREATE TABLE services(scope TEXT PRIMARY KEY, healthy INTEGER, provider TEXT); CREATE TABLE jobs(scope TEXT, id INTEGER, status TEXT, PRIMARY KEY(scope,id)); CREATE TABLE effects(scope TEXT, command TEXT, PRIMARY KEY(scope,command));")
        lab = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                scope = payload["scope"]
                with sqlite3.connect(lab.path, timeout=10) as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("INSERT OR IGNORE INTO services VALUES (?,0,'primary')", (scope,))
                    for n in range(3):
                        db.execute("INSERT OR IGNORE INTO jobs VALUES (?,?,'queued')", (scope, n))
                    if self.path == "/execute" and not lab.fail_recovery:
                        db.execute("INSERT OR IGNORE INTO effects VALUES (?,?)", (scope, payload["command"]))
                        db.execute("UPDATE services SET healthy=1, provider=? WHERE scope=?", ("backup" if payload["action"] == "fallback" else "primary", scope))
                        db.execute("UPDATE jobs SET status='completed' WHERE scope=?", (scope,))
                    state = db.execute("SELECT healthy,provider FROM services WHERE scope=?", (scope,)).fetchone()
                    effects = db.execute("SELECT count(*) FROM effects WHERE scope=?", (scope,)).fetchone()[0]
                    jobs = db.execute("SELECT count(*) FROM jobs WHERE scope=? AND status='completed'", (scope,)).fetchone()[0]
                data = json.dumps({"healthy": bool(state[0]), "provider": state[1], "effects": effects, "completed_jobs": jobs}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class LabAdapter:
    actions = frozenset({"probe", "retry", "resume", "reprocess", "fallback", "restart", "compensate", "route_approver"})
    idempotent = True

    def __init__(self, lab):
        self.lab = lab
        self.lose_ack = False

    async def authorize(self, context):
        assert context.tenant_id and context.owner_id and context.resource == "lab"

    async def call(self, context, path, action="probe"):
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            response = await client.post(self.lab.origin + path, json={"scope": f"{context.tenant_id}/{context.owner_id}/{context.resource}", "command": str(context.command_id), "action": action})
            response.raise_for_status()
            return response.json()

    async def execute(self, context, action):
        result = await self.call(context, "/execute", action)
        if self.lose_ack:
            raise TimeoutError("injected lost acknowledgement")
        return result

    async def observe(self, context):
        result = await self.call(context, "/health")
        return Observation(result["healthy"], "lab_health", result)

    async def impact(self, context):
        return {"status": "fixture", "departments": ["dispatch", "operations"], "evidence": "Disposable dependency fixture; production Company Graph not connected"}
