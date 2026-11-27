"""Real headless-browser adversarial smoke against a disposable loopback fixture."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.legacy_browser import capture_readonly


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen_cookies: list[str] = []
    mutations = 0
    blocked_external_hits = 0
    script = ""

    def log_message(self, *_args):
        return

    def do_GET(self):
        self.__class__.seen_cookies.append(self.headers.get("Cookie", ""))
        if self.path == "/":
            body = (
                '<!doctype html><html><head><title>Safe review</title></head><body>'
                f'<script src="/review.js"></script><a href="/guide">Exact Guide</a></body></html>'
            ).encode()
            self.send_response(200)
            self.send_header("Set-Cookie", "must_not_be_forwarded=secret; Path=/")
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/review.js":
            body = self.__class__.script.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
        elif self.path == "/guide":
            body = b"<html><head><title>Guide</title></head><body>Approved guide content.</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/large":
            body = b"x" * (2 * 1024 * 1024 + 1)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path.startswith("/asset-"):
            body = b"asset"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        else:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.__class__.mutations += 1
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


class ExternalFixtureHandler(FixtureHandler):
    def do_GET(self):
        self.__class__.blocked_external_hits += 1
        super().do_GET()


def _settings(origin: str, *, max_requests: int = 40) -> Settings:
    return Settings.model_construct(
        database_url=SecretStr("postgresql+asyncpg://test:test@127.0.0.1/test"),
        master_encryption_key=SecretStr("test-key-not-used"),
        rag_legacy_browser_enabled=True,
        rag_legacy_browser_allowed_origins=origin,
        rag_legacy_browser_executable_path=Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        rag_legacy_browser_timeout_seconds=25,
        rag_legacy_browser_max_requests=max_requests,
        rag_legacy_browser_max_response_bytes=8 * 1024 * 1024,
        rag_legacy_browser_max_result_chars=12_000,
        rag_legacy_browser_allow_private_networks=False,
    )


@pytest.mark.skipif(not Path("C:/Program Files/Google/Chrome/Application/chrome.exe").is_file(), reason="local Chrome is unavailable")
def test_real_browser_is_cookie_free_get_only_and_origin_bounded():
    FixtureHandler.seen_cookies = []
    FixtureHandler.mutations = 0
    FixtureHandler.blocked_external_hits = 0
    with ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler) as internal:
        with ThreadingHTTPServer(("127.0.0.1", 0), ExternalFixtureHandler) as external:
            FixtureHandler.script = (
                "document.body.insertAdjacentText('beforeend', document.cookie || 'NO_COOKIE');"
                "fetch('/mutate', {method:'POST', body:'ignored'});"
                f"fetch('http://localhost:{external.server_port}/probe');"
            )
            worker = threading.Thread(target=internal.serve_forever, daemon=True)
            external_worker = threading.Thread(target=external.serve_forever, daemon=True)
            worker.start()
            external_worker.start()
            try:
                origin = f"http://127.0.0.1:{internal.server_port}"
                result = asyncio.run(
                    capture_readonly(
                        _settings(origin),
                        f"{origin}/",
                        ["Exact Guide"],
                    )
                )
            finally:
                internal.shutdown()
                external.shutdown()
                worker.join(timeout=2)
                external_worker.join(timeout=2)

    assert [page.label for page in result.pages] == ["Start page", "Exact Guide"]
    assert "NO_COOKIE" in result.pages[0].text
    assert "Approved guide content." in result.pages[1].text
    assert result.request_count <= 40
    assert result.response_bytes < 8 * 1024 * 1024
    assert FixtureHandler.mutations == 0
    assert ExternalFixtureHandler.blocked_external_hits == 0
    assert all(cookie == "" for cookie in FixtureHandler.seen_cookies)


@pytest.mark.skipif(not Path("C:/Program Files/Google/Chrome/Application/chrome.exe").is_file(), reason="local Chrome is unavailable")
def test_real_browser_fails_closed_on_response_size_and_request_budgets():
    with ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            with pytest.raises(Exception, match="response_too_large"):
                asyncio.run(capture_readonly(_settings(origin), f"{origin}/large", []))

            FixtureHandler.script = "".join(f"fetch('/asset-{index}');" for index in range(12))
            with pytest.raises(Exception, match="request_budget_exceeded"):
                asyncio.run(capture_readonly(_settings(origin, max_requests=5), f"{origin}/", []))
        finally:
            server.shutdown()
            worker.join(timeout=2)
