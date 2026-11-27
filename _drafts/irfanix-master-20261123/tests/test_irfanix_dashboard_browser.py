"""Authenticated Chromium UI acceptance against an isolated PostgreSQL-backed app.

Explicit opt-in because this starts loopback HTTP and headless Chrome. Uses the
suite's synthetic accounts and deterministic planner; no production credentials.
"""
import asyncio
import os
from pathlib import Path
import re
import socket

import pytest
from sqlalchemy import func, select

from app.agent_studio import queue
from app.core.config import get_settings
from app.db.agent_studio_models import AgentStudioMemory
from app.main import app
from tests.conftest import USER_EMAIL, PASSWORD, run_db
from tests.test_irfanix_durable_queue import setup_task, planner


@pytest.mark.skipif(os.environ.get("IRFANIX_BROWSER_ACCEPTANCE") != "1", reason="Explicit isolated browser acceptance")
def test_dashboard_submit_review_complete_filter_cancel_and_mobile(client, monkeypatch):
    from playwright.sync_api import sync_playwright, expect
    import uvicorn

    planner(monkeypatch)
    agent, _, _ = setup_task(client, approvals=["remember"])
    evidence = Path(".irfanix-cycle/evidence")
    evidence.mkdir(parents=True, exist_ok=True)

    async def start():
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        stop = asyncio.Event()
        worker_task = asyncio.create_task(queue.run_worker(app.state.sessionmaker, get_settings(),
            app.state.cipher, app.state.llm_runtime, app.state.rag, stop))
        for _ in range(100):
            if server.started:
                return server, server_task, stop, worker_task, listener.getsockname()[1]
            await asyncio.sleep(0.05)
        raise RuntimeError("Isolated browser server did not start")

    server, server_task, stop, worker_task, port = client.portal.call(start)

    async def stop_worker():
        stop.set()
        await asyncio.wait_for(worker_task, 15)

    async def shutdown():
        await stop_worker()
        server.should_exit = True
        await asyncio.wait_for(server_task, 15)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe", headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.set_default_timeout(20000)
                page.goto(f"http://127.0.0.1:{port}/admin/?view=agent-studio")
                page.locator('form.login-form input[name="email"]').fill(USER_EMAIL)
                page.locator('form.login-form input[name="password"]').fill(PASSWORD)
                page.locator('form.login-form input[name="password"]').press("Enter")
                expect(page.locator(".shell")).to_be_visible()
                page.goto(f"http://127.0.0.1:{port}/admin/?view=agent-studio")
                page.get_by_role("button", name=re.compile(r"^Queue acceptance")).click()
                page.get_by_placeholder(re.compile("Research three current options")).fill("Remember browser verification preference")
                page.get_by_role("button", name="Run agent", exact=True).click()
                expect(page.get_by_role("button", name="Approve and continue", exact=True)).to_be_visible(timeout=30000)
                page.get_by_text("Review exact action", exact=True).click()
                expect(page.locator("pre").filter(has_text="queue-preference")).to_be_visible()
                page.screenshot(path=str(evidence / "approval-desktop.png"), full_page=True)
                page.get_by_role("button", name="Approve and continue", exact=True).click()
                expect(page.get_by_text("Saved preference verified in the tool result.", exact=True)).to_be_visible(timeout=30000)
                page.get_by_role("tab", name=re.compile("Task monitor")).click()
                page.get_by_label("Filter task status").select_option("completed")
                page.get_by_label("Search agent tasks").fill("browser verification")
                expect(page.get_by_text("1 matching tasks", exact=True)).to_be_visible(timeout=10000)
                page.get_by_label("Search agent tasks").fill("no-match-sentinel")
                expect(page.get_by_text("No matching tasks", exact=True)).to_be_visible()
                page.get_by_label("Search agent tasks").fill("")
                page.screenshot(path=str(evidence / "monitor-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
                page.screenshot(path=str(evidence / "monitor-mobile.png"), full_page=True)
                page.set_viewport_size({"width": 1440, "height": 1000})
                client.portal.call(stop_worker)
                page.get_by_role("tab", name=re.compile("Agents & workspace")).click()
                page.get_by_placeholder(re.compile("Research three current options")).fill("Cancel this queued task")
                page.get_by_role("button", name="Run agent", exact=True).click()
                expect(page.get_by_role("button", name="Cancel task", exact=True)).to_be_visible()
                page.get_by_role("button", name="Cancel task", exact=True).click()
                expect(page.get_by_text("Task cancelled.", exact=True)).to_be_visible()
                assert run_db(lambda c: c.scalar(select(func.count()).select_from(AgentStudioMemory))) == 1
            finally:
                browser.close()
    finally:
        client.portal.call(shutdown)
