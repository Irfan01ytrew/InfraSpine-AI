"""ASGI transport for the Step H load harness (slice 4, bounded).

Drives the real FastAPI app in-process through ``httpx.ASGITransport`` —
no network, no server process, no credentials on disk. One
``httpx.AsyncClient`` per virtual user, so each user keeps its own session
cookie; every user logs in once (lazily, on the first step that needs a
session) and reuses that session for the rest of the pass.

Steps (the mix the 1,000-user qualification will use at scale):

* ``health`` — ``GET /health`` (no auth; the app's cheapest read).
* ``login`` — ``POST /api/auth/login`` (the auth burst of the real scenario).
* ``documents`` — ``GET /api/knowledge`` (a permission-checked document read).
* ``founder_note`` — ``POST /api/founder-voice/note`` (the full founder
  intake path: parse, seal, mirror onto the unified engine). The transcript
  carries the user index, so each virtual user files a *different* command;
  the same words from two users must never collide on one action hash.

Any HTTP status >= 400 raises, which the harness counts as one error.
A step name the transport does not know raises ``ValueError`` immediately —
a misspelled scenario must fail loudly, never silently pass.
"""

from __future__ import annotations

import httpx

from app.qa.load_harness import LoadStep

FOUNDER_NOTE_TRANSCRIPT = "mera is mahine ka kharcha chart dikhao"


class AsgiTransport:
    """Load transport over the real app, in-process."""

    def __init__(self, app, *, email: str, password: str, scope_id: str,
                 base_url: str = 'http://test') -> None:
        self._app = app
        self._email = email
        self._password = password
        self._scope_id = scope_id
        self._base_url = base_url
        self._clients: dict[int, httpx.AsyncClient] = {}
        self._logged_in: set[int] = set()

    def _client(self, user_index: int) -> httpx.AsyncClient:
        client = self._clients.get(user_index)
        if client is None:
            # The app requires the X-Requested-With header on state-changing
            # requests (CSRF); conftest's TestClient sends the same header.
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._app), base_url=self._base_url,
                headers={'X-Requested-With': 'test'})
            self._clients[user_index] = client
        return client

    async def _login(self, user_index: int) -> None:
        if user_index in self._logged_in:
            return
        response = await self._client(user_index).post(
            '/api/auth/login', json={'email': self._email, 'password': self._password})
        if response.status_code != 200:
            raise RuntimeError(f'login -> HTTP {response.status_code}: {response.text[:200]}')
        self._logged_in.add(user_index)

    async def __call__(self, step: LoadStep, user_index: int) -> None:
        client = self._client(user_index)
        if step.name == 'health':
            response = await client.get('/health')
        elif step.name == 'login':
            response = await client.post(
                '/api/auth/login', json={'email': self._email, 'password': self._password})
            if response.status_code == 200:
                self._logged_in.add(user_index)
        elif step.name == 'documents':
            await self._login(user_index)
            response = await client.get('/api/knowledge')
        elif step.name == 'founder_note':
            await self._login(user_index)
            response = await client.post('/api/founder-voice/note', json={
                'scope_id': self._scope_id,
                'transcript': f'{FOUNDER_NOTE_TRANSCRIPT} user {user_index}'})
        else:
            raise ValueError(f'unknown load step: {step.name}')
        if response.status_code >= 400:
            raise RuntimeError(
                f'{step.name} -> HTTP {response.status_code}: {response.text[:200]}')

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        self._logged_in.clear()
