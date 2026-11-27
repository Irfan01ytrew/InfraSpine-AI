"""Bounded concurrency measurement against the live-AI review API (scripts/start_live_review_stack.py).

Measures what one API process on this machine sustains for many simultaneous employees:
  * a burst of concurrent sign-ins (Argon2 password hashing is deliberately expensive), then
  * waves of authenticated read traffic (profile, document library, conversation list).
LLM chat is not included (it depends on the external provider's latency and quota).
Results are evidence for this host and configuration only, not for target hardware.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1] / ".verification-live-review-20261029"
API = "http://127.0.0.1:63011"  # override with --api
HEADERS = {"X-Requested-With": "load-test"}
READS = ("/api/auth/me", "/api/rag/documents", "/api/conversations")


def summarize(latencies: list[float], errors: int, seconds: float) -> dict:
    ordered = sorted(latencies)
    pct = lambda q: round(ordered[min(len(ordered) - 1, int(q * len(ordered)))] * 1000) if ordered else None
    return {
        "requests": len(latencies) + errors, "errors": errors, "seconds": round(seconds, 2),
        "throughput_rps": round((len(latencies) + errors) / seconds, 1) if seconds else None,
        "p50_ms": pct(0.50), "p95_ms": pct(0.95), "p99_ms": pct(0.99), "max_ms": pct(1.0),
        "mean_ms": round(statistics.fmean(ordered) * 1000) if ordered else None,
    }


async def login_burst(api: str, email: str, password: str, sessions: int) -> tuple[list[httpx.AsyncClient], dict]:
    clients = [httpx.AsyncClient(base_url=api, headers=HEADERS, timeout=120) for _ in range(sessions)]
    latencies, errors = [], 0

    async def one(client: httpx.AsyncClient) -> None:
        nonlocal errors
        start = time.perf_counter()
        try:
            response = await client.post("/api/auth/login", json={"email": email, "password": password})
            if response.status_code == 200:
                latencies.append(time.perf_counter() - start)
            else:
                errors += 1
        except httpx.HTTPError:
            errors += 1

    began = time.perf_counter()
    await asyncio.gather(*(one(c) for c in clients))
    return clients, summarize(latencies, errors, time.perf_counter() - began)


async def read_wave(clients: list[httpx.AsyncClient], users: int, requests_per_user: int) -> dict:
    latencies, errors, statuses = [], 0, {}

    async def user(index: int) -> None:
        nonlocal errors
        client = clients[index % len(clients)]
        for step in range(requests_per_user):
            path = READS[(index + step) % len(READS)]
            start = time.perf_counter()
            try:
                response = await client.get(path)
                statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
                if response.status_code == 200:
                    latencies.append(time.perf_counter() - start)
                else:
                    errors += 1
            except httpx.HTTPError:
                errors += 1

    began = time.perf_counter()
    await asyncio.gather(*(user(i) for i in range(users)))
    result = summarize(latencies, errors, time.perf_counter() - began)
    result.update(concurrent_users=users, requests_per_user=requests_per_user, statuses=statuses)
    return result


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=50, help="concurrent sign-ins")
    parser.add_argument("--waves", default="100,250,500,1000", help="concurrent users per read wave")
    parser.add_argument("--requests-per-user", type=int, default=3)
    parser.add_argument("--api", default=API, help="API base URL")
    parser.add_argument("--label", default="single-process", help="evidence file suffix")
    args = parser.parse_args()
    account = json.loads((ROOT / "credentials-fixture.json").read_text(encoding="utf-8"))["employee@livereview.test"]
    clients, login = await login_burst(args.api, "employee@livereview.test", account["password"], args.sessions)
    evidence = {"api": args.api, "label": args.label, "host_note": "uvicorn API, document worker disabled, synthetic DB on local PostgreSQL 18 (max_connections=40)", "login_burst": dict(login, sessions=args.sessions)}
    live = [c for c in clients if c.cookies]
    evidence["waves"] = []
    for users in (int(x) for x in args.waves.split(",")):
        if not live:
            break
        evidence["waves"].append(await read_wave(live, users, args.requests_per_user))
    for client in clients:
        await client.aclose()
    out = ROOT / "evidence" / f"load-test-review-stack-{args.label}.json"
    out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
