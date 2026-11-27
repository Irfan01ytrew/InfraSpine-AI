"""Custom-provider endpoint security: credentials bound to their verified origin, no stored-key reuse on a
changed endpoint, SSRF policy (internal targets, redirects, DNS rebinding) and the legacy-key upgrade.

The real OpenAI SDK and HTTP stack run end to end. Only DNS (`endpoint_security.resolve_host`) and the
socket layer (`endpoint_security._inner_backend`) are replaced by an in-memory network, so every test can
see exactly which address was contacted, with which TLS server name and which Authorization header.
"""

import asyncio
import base64
import ipaddress
import json
import logging
import socket
import uuid
from datetime import UTC, datetime

import httpcore2
import httpx2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.crypto import CredentialCipher, DecryptionError, EncryptedValue, credential_aad
from app.db.session import test_engine_options as _test_engine_options
from app.providers import endpoint_security
from app.providers.endpoint_security import (
    EndpointPolicy,
    UnsafeEndpointError,
    blocked_reason,
    guarded_http_client,
    normalize_base_url,
    parse_trusted_origins,
)
from app.services.provider_admin import rebind_legacy_credentials
from tests.conftest import TEST_DATABASE_URL, TEST_MASTER_KEY, VALID_KEY_1, run_db

KEY_A = 'sk' + '-custom-endpoint-key-aaaa-1111'
KEY_B = 'sk' + '-custom-endpoint-key-bbbb-2222'
PROVIDER_ID = "custom-acme-llm-acme-1"
PROVIDER = f"/api/admin/providers/{PROVIDER_ID}"
PUBLIC_IP = "93.184.215.14"
PUBLIC_IP_2 = "151.101.1.69"
ATTACKER_IP = "45.33.32.156"
CIPHER = CredentialCipher(base64.b64decode(TEST_MASTER_KEY), "v1")


# ------------------------------------------------------------------ in-memory network


class FakeNetwork:
    def __init__(self) -> None:
        self.dns: dict[str, object] = {}  # host -> list of IPs, or a callable returning one
        self.servers: dict[str, object] = {}  # ip -> handler(request) -> (status, headers, body)
        self.connections: list[tuple[str, int]] = []
        self.requests: list[dict] = []

    async def resolve(self, host: str, port: int):
        answer = self.dns.get(host)
        if answer is None:
            raise socket.gaierror("no such host")
        if callable(answer):
            answer = answer()
        return [ipaddress.ip_address(a) for a in answer]

    def requests_to(self, ip: str) -> list[dict]:
        return [r for r in self.requests if r["ip"] == ip]

    def authorizations(self) -> set[tuple[str, str]]:
        return {(r["ip"], r["headers"].get("authorization", "")) for r in self.requests}


class _Stream(httpcore2.AsyncNetworkStream):
    def __init__(self, net: FakeNetwork, ip: str, handler) -> None:
        self._net, self._ip, self._handler = net, ip, handler
        self._incoming, self._outgoing, self._sni = b"", b"", None

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._incoming += buffer
        head, sep, body = self._incoming.partition(b"\r\n\r\n")
        if not sep:
            return
        lines = head.decode("latin-1").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:])}
        if len(body) < int(headers.get("content-length", "0")):
            return
        request = {"ip": self._ip, "sni": self._sni, "method": method, "path": path, "headers": headers, "body": body}
        self._net.requests.append(request)
        status, extra, payload = self._handler(request)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        response_headers = {"content-type": "application/json", "content-length": str(len(data)), "connection": "close"}
        response_headers.update(extra)
        self._outgoing = (
            f"HTTP/1.1 {status} X\r\n".encode()
            + "".join(f"{k}: {v}\r\n" for k, v in response_headers.items()).encode()
            + b"\r\n"
            + data
        )
        self._incoming = b""

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        data, self._outgoing = self._outgoing[:max_bytes], self._outgoing[max_bytes:]
        return data

    async def aclose(self) -> None:
        return None

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._sni = server_hostname
        return self

    def get_extra_info(self, info: str):
        return None


class _Backend(httpcore2.AsyncNetworkBackend):
    def __init__(self, net: FakeNetwork) -> None:
        self._net = net

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self._net.connections.append((host, port))
        handler = self._net.servers.get(host)
        if handler is None:
            raise httpcore2.ConnectError("unreachable")
        return _Stream(self._net, host, handler)

    async def sleep(self, seconds: float) -> None:
        return None


def openai_server(valid_keys: set[str], models=("acme-1",)):
    """A minimal OpenAI-compatible API: model listing and chat completions."""

    def handler(request):
        if request["headers"].get("authorization", "").removeprefix("Bearer ") not in valid_keys:
            return 401, {}, {"error": {"message": "invalid key"}}
        if request["method"] == "GET" and request["path"].endswith("/models"):
            return 200, {}, {"object": "list", "data": [{"id": m, "object": "model", "created": 0, "owned_by": "t"} for m in models]}
        if request["method"] == "POST" and request["path"].endswith("/chat/completions"):
            key = request["headers"]["authorization"][-4:]
            reply = f"served-by={request['ip']} key={key}"
            return 200, {}, {
                "id": "c1", "object": "chat.completion", "created": 0, "model": models[0],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        return 404, {}, {"error": {"message": "not found"}}

    return handler


@pytest.fixture
def net(monkeypatch):
    network = FakeNetwork()
    monkeypatch.setattr(endpoint_security, "resolve_host", network.resolve)
    monkeypatch.setattr(endpoint_security, "_inner_backend", lambda: _Backend(network))
    network.dns["llm.example.com"] = [PUBLIC_IP]
    network.servers[PUBLIC_IP] = openai_server({KEY_A})
    network.dns["evil.example.net"] = [ATTACKER_IP]
    network.servers[ATTACKER_IP] = openai_server({KEY_A, KEY_B})  # would accept anything it is sent
    return network


@pytest.fixture
def trusted_origins(monkeypatch):
    def set_policy(value: str) -> None:
        monkeypatch.setattr(get_settings(), "provider_trusted_private_origins", value)

    return set_policy


# ------------------------------------------------------------------ helpers


def create(client, url="https://llm.example.com/v1", key=KEY_A):
    return client.post(
        "/api/admin/providers/custom",
        json={"display_name": "Acme LLM", "base_url": url, "model": "acme-1", "api_key": key},
    )


def state(client) -> dict:
    return client.get(PROVIDER).json()


def update(client, **body):
    current = state(client)
    payload = {"model": current["model"], "settings": current["settings"], "expected_version": current["version"], **body}
    return client.put(PROVIDER, json=payload)


def activate(client):
    return client.post(f"{PROVIDER}/activate", json={"expected_version": state(client)["version"]})


def credentials() -> list:
    return run_db(
        lambda c: c.execute(
            text(
                "SELECT id, status, ciphertext, nonce, key_id, endpoint_origin FROM provider_credentials "
                "ORDER BY created_at"
            )
        )
    ).all()


def audit_rows() -> list:
    return run_db(lambda c: c.execute(text("SELECT action, outcome, details::text FROM audit_logs ORDER BY id"))).all()


def decrypt(row, provider=PROVIDER_ID, origin=None) -> str:
    return CIPHER.decrypt(
        EncryptedValue(bytes(row.ciphertext), bytes(row.nonce), row.key_id),
        associated_data=credential_aad(row.id, provider, origin),
    )


# ------------------------------------------------------------------ valid endpoints and binding


def test_valid_endpoint_pins_the_checked_address_and_binds_the_key(admin_client, net):
    response = create(admin_client)
    assert response.status_code == 200, response.text
    assert response.json()["base_url"] == "https://llm.example.com/v1"

    [verify] = net.requests
    assert (verify["ip"], verify["sni"], verify["headers"]["host"]) == (PUBLIC_IP, "llm.example.com", "llm.example.com")
    assert verify["path"] == "/v1/models" and verify["headers"]["authorization"] == f"Bearer {KEY_A}"

    [row] = credentials()
    assert row.endpoint_origin == "https://llm.example.com:443"
    assert decrypt(row, origin=row.endpoint_origin) == KEY_A
    with pytest.raises(DecryptionError):  # the ciphertext is useless without the origin binding
        decrypt(row)
    with pytest.raises(DecryptionError):
        decrypt(row, origin="https://evil.example.net:443")

    assert activate(admin_client).status_code == 200
    reply = admin_client.post("/api/ai/chat", json={"message": "hello"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["reply"] == f"served-by={PUBLIC_IP} key={KEY_A[-4:]}"
    assert net.authorizations() == {(PUBLIC_IP, f"Bearer {KEY_A}")}


def test_url_normalization_is_canonical():
    endpoint = normalize_base_url("  HTTPS://LLM.Example.COM.:443/v1/  ")
    assert (endpoint.base_url, endpoint.origin) == ("https://llm.example.com/v1", "https://llm.example.com:443")
    v6 = normalize_base_url("https://[2606:4700:0::1111]:8443/v1")
    assert (v6.base_url, v6.origin) == ("https://[2606:4700::1111]:8443/v1", "https://[2606:4700::1111]:8443")
    assert normalize_base_url("https://bücher.example/v1").host == "xn--bcher-kva.example"
    for bad in ("https://llm.example.com\\@evil", "https://llm.example.com/v1/%2e%2e/x", "ftp://llm.example.com",
                "https://llm.example.com:0/", "https://llm.example.com/v1#frag", "https://llm.example.com/?"):
        with pytest.raises(UnsafeEndpointError):
            normalize_base_url(bad)
    assert parse_trusted_origins(" http://localhost:11434 , https://LLM.corp:8443 ") == {
        "http://localhost:11434", "https://llm.corp:8443"
    }
    with pytest.raises(ValueError):
        parse_trusted_origins("http://localhost:11434/v1")


# ------------------------------------------------------------------ stored-key reuse


def test_stored_key_is_never_sent_to_a_changed_endpoint(admin_client, net):
    assert create(admin_client).status_code == 200
    before = state(admin_client)

    tested = admin_client.post(f"{PROVIDER}/test", json={"base_url": "https://evil.example.net/v1"})
    assert tested.status_code == 422 and "API key" in tested.json()["error"]["message"]
    for body in (
        {"base_url": "https://evil.example.net/v1"},  # new host
        {"base_url": "https://llm.example.com/v2"},  # same origin, different path: still a new endpoint
        {"base_url": "https://llm.example.com:8443/v1"},  # new port
    ):
        assert update(admin_client, **body).status_code == 422
    assert update(admin_client, api_key=KEY_B).status_code == 422  # a key without an endpoint change
    assert update(admin_client, base_url="https://LLM.example.com/v1/").status_code == 200  # same endpoint, canonical
    assert state(admin_client)["base_url"] == before["base_url"]
    assert net.requests_to(ATTACKER_IP) == [] and ATTACKER_IP not in [ip for ip, _ in net.connections]

    stored = admin_client.post(f"{PROVIDER}/test", json={}).json()
    assert stored["ok"] is True and stored["tested"] == "pending_key"
    assert net.authorizations() == {(PUBLIC_IP, f"Bearer {KEY_A}")}


def test_a_tampered_endpoint_cannot_decrypt_the_stored_key(admin_client, net):
    """Even a direct database edit of base_url cannot redirect the stored key: decryption is origin-bound."""
    assert create(admin_client).status_code == 200
    run_db(lambda c: c.execute(text("UPDATE provider_configs SET base_url = 'https://evil.example.net/v1'")))
    response = admin_client.post(f"{PROVIDER}/test", json={})
    assert response.status_code == 409 and response.json()["error"]["code"] == "credential_unreadable"
    assert activate(admin_client).status_code == 409
    assert net.requests_to(ATTACKER_IP) == []


def test_endpoint_change_requires_a_new_key_which_replaces_the_bound_ones(admin_client, net, caplog):
    caplog.set_level(logging.DEBUG)
    net.dns["llm2.example.org"] = [PUBLIC_IP_2]
    net.servers[PUBLIC_IP_2] = openai_server({KEY_B})
    assert create(admin_client).status_code == 200
    assert activate(admin_client).status_code == 200
    allowed = admin_client.put(
        f"{PROVIDER}/image-sharing", json={"authorized": True, "expected_version": state(admin_client)["version"]}
    )
    assert allowed.status_code == 200 and allowed.json()["image_sharing_authorized"]

    rejected = update(admin_client, base_url="https://llm2.example.org/v1", api_key='sk' + '-not-valid-for-llm2-000000')
    assert rejected.status_code == 400 and rejected.json()["error"]["details"]["reason"] == "invalid_credentials"
    assert state(admin_client)["base_url"] == "https://llm.example.com/v1"
    assert [r.status for r in credentials()] == ["active"]

    changed = update(admin_client, base_url="https://llm2.example.org/v1", api_key=KEY_B)
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["base_url"] == "https://llm2.example.org/v1" and body["is_active"] is True
    assert body["active_credential"]["key_hint"] == KEY_B[-4:] and body["pending_credential"] is None
    assert body["image_sharing_authorized"] is False  # images were authorized for the previous endpoint only

    old, new = credentials()
    assert old.status == "retired" and old.ciphertext is None and old.nonce is None  # shredded
    assert new.status == "active" and new.endpoint_origin == "https://llm2.example.org:443"
    assert decrypt(new, origin=new.endpoint_origin) == KEY_B

    reply = admin_client.post("/api/ai/chat", json={"message": "hello"}).json()
    assert reply["reply"] == f"served-by={PUBLIC_IP_2} key={KEY_B[-4:]}"
    assert (PUBLIC_IP_2, f"Bearer {KEY_A}") not in net.authorizations()  # the old key never went to the new host

    rows = audit_rows()
    [changed_entry] = [json.loads(d) for a, o, d in rows if a == "provider.endpoint_changed"]
    assert changed_entry["previous_origin"] == "https://llm.example.com:443"
    assert changed_entry["origin"] == "https://llm2.example.org:443"
    assert changed_entry["retired_credential_ids"] == [str(old.id)]
    for secret in (KEY_A, KEY_B):
        assert all(secret not in d for _, _, d in rows)
        assert secret not in caplog.text


def test_endpoint_change_on_an_inactive_provider_stages_the_key_and_switching_still_works(admin_client, net, fake_provider):
    net.dns["llm2.example.org"] = [PUBLIC_IP_2]
    net.servers[PUBLIC_IP_2] = openai_server({KEY_B})
    assert create(admin_client).status_code == 200
    assert update(admin_client, base_url="https://llm2.example.org/v1", api_key=KEY_B).status_code == 200
    body = state(admin_client)
    assert body["is_active"] is False and body["pending_credential"]["key_hint"] == KEY_B[-4:]
    assert [r.status for r in credentials()] == ["retired", "pending"]

    # Built-in provider switching is unchanged: DeepSeek active, then back to the custom endpoint.
    deepseek = "/api/admin/providers/deepseek"
    admin_client.put(deepseek, json={"model": "deepseek-flash", "settings": {}, "expected_version": 0})
    admin_client.post(f"{deepseek}/credentials", json={"api_key": VALID_KEY_1, "expected_version": 1})
    assert admin_client.post(f"{deepseek}/activate", json={"expected_version": 2}).status_code == 200
    assert admin_client.post("/api/ai/chat", json={"message": "hi"}).json()["reply"].startswith("key=1111")
    assert activate(admin_client).status_code == 200
    assert admin_client.get(deepseek).json()["is_active"] is False
    assert admin_client.post("/api/ai/chat", json={"message": "hi"}).json()["reply"] == (
        f"served-by={PUBLIC_IP_2} key={KEY_B[-4:]}"
    )


# ------------------------------------------------------------------ SSRF


@pytest.mark.parametrize(
    ("url", "answers"),
    [
        ("http://llm.example.com/v1", None),  # plain HTTP to a public host
        ("https://127.0.0.1/v1", None),
        ("https://localhost/v1", ["127.0.0.1"]),
        ("https://169.254.169.254/latest", None),  # cloud metadata
        ("https://100.100.100.200/", None),  # Alibaba metadata (shared address space)
        ("https://10.0.0.5/v1", None),
        ("https://[::1]/v1", None),
        ("https://[::ffff:127.0.0.1]/v1", None),  # IPv4-mapped loopback
        ("https://[64:ff9b::a9fe:a9fe]/", None),  # NAT64 of 169.254.169.254
        ("https://2130706433/v1", None),  # decimal 127.0.0.1
        ("https://0x7f.1/v1", None),
        ('https' + '://' + 'user:pw' + '@' + 'llm.example.com/v1', None),
        ("https://llm.example.com/v1?x=1", None),
        ("https://llm.example.com/v1/../admin", None),
        ("https://metadata.google.internal/", ["169.254.169.254"]),
        ("https://meta.example.com/v1", ["169.254.169.254"]),  # public name, metadata address
        ("https://mixed.example.com/v1", [PUBLIC_IP, "10.1.2.3"]),  # one internal answer rejects all
        ("https://v6.example.com/v1", ["fd00:ec2::254"]),  # AWS IPv6 metadata (unique local)
        ("https://nowhere.example.com/v1", None),  # does not resolve
    ],
)
def test_internal_and_invalid_targets_are_rejected_before_any_connection(admin_client, net, url, answers):
    host = url.split("//", 1)[1].split("/", 1)[0].split("@")[-1]
    if answers is not None:
        net.dns[host] = answers
    response = create(admin_client, url=url)
    assert response.status_code == 422, (url, response.text)
    assert net.connections == [] and net.requests == []
    assert credentials() == []
    assert ("provider.endpoint_rejected", "failure") in [(a, o) for a, o, _ in audit_rows()]


def test_dns_rebinding_between_validation_and_connection_is_blocked(admin_client, net):
    answers = iter([[PUBLIC_IP], ["10.0.0.7"], ["10.0.0.7"]])
    net.dns["rebind.example.com"] = lambda: next(answers)
    net.servers["10.0.0.7"] = openai_server({KEY_A})
    response = create(admin_client, url="https://rebind.example.com/v1")
    assert response.status_code == 400 and response.json()["error"]["details"]["reason"] == "unsafe_endpoint"
    assert net.connections == [] and credentials() == []


def test_dns_rebinding_after_saving_is_blocked_on_every_connection(admin_client, net):
    assert create(admin_client).status_code == 200
    net.dns["llm.example.com"] = ["169.254.169.254"]
    net.servers["169.254.169.254"] = openai_server({KEY_A})
    result = admin_client.post(f"{PROVIDER}/test", json={}).json()
    assert result["ok"] is False and result["reason"] == "unsafe_endpoint"
    assert activate(admin_client).status_code == 400
    assert [ip for ip, _ in net.connections] == [PUBLIC_IP]  # only the original verification


def test_redirects_are_not_followed(admin_client, net):
    def redirect(request):
        return 307, {"location": f"https://evil.example.net{request['path']}"}, b""

    net.servers[PUBLIC_IP] = redirect
    response = create(admin_client)
    assert response.status_code == 400 and response.json()["error"]["details"]["reason"] == "redirect_refused"
    assert net.requests_to(ATTACKER_IP) == [] and [ip for ip, _ in net.connections] == [PUBLIC_IP]


def test_guarded_client_refuses_any_other_origin(net):
    endpoint = normalize_base_url("https://llm.example.com/v1")

    async def attempt(url):
        async with guarded_http_client(endpoint, EndpointPolicy()) as http:
            return await http.get(url, headers={"Authorization": f"Bearer {KEY_A}"})

    for url in ("https://evil.example.net/v1/models", "http://llm.example.com/v1/models", "https://llm.example.com:8443/"):
        with pytest.raises(httpx2.ConnectError) as caught:
            asyncio.run(attempt(url))
        assert blocked_reason(caught.value) is not None
    assert net.connections == []
    assert asyncio.run(attempt("https://llm.example.com/v1/models")).status_code == 200


def test_private_endpoints_need_an_explicit_deployment_policy(admin_client, net, trusted_origins):
    net.dns["localhost"] = ["127.0.0.1"]
    net.servers["127.0.0.1"] = openai_server({KEY_A})
    assert create(admin_client, url="http://localhost:11434/v1").status_code == 422
    assert net.connections == []

    trusted_origins("http://localhost:11434")
    assert create(admin_client, url="http://localhost:8080/v1").status_code == 422  # only the exact origin
    response = create(admin_client, url="http://localhost:11434/v1")
    assert response.status_code == 200, response.text
    assert net.connections == [("127.0.0.1", 11434)] and net.requests[0]["sni"] is None

    trusted_origins("https://169.254.169.254, http://public.example.com")
    net.dns["public.example.com"] = [PUBLIC_IP]
    assert create(admin_client, url="https://169.254.169.254/").status_code == 422  # metadata never allowed
    assert create(admin_client, url="http://public.example.com/v1").status_code == 422  # HTTP only to private


def test_local_only_transport_rejects_public_resolution_even_for_trusted_origin(net):
    public = normalize_base_url("https://llm.example.com/v1")
    net.dns["llm.example.com"] = [PUBLIC_IP]
    policy = EndpointPolicy(frozenset({public.origin}), private_only=True)

    async def send_to(endpoint):
        async with guarded_http_client(endpoint, policy) as http:
            return await http.get(endpoint.base_url + "/models")

    with pytest.raises(httpx2.ConnectError) as rejected:
        asyncio.run(send_to(public))
    assert blocked_reason(rejected.value) is not None
    assert net.connections == []

    local = normalize_base_url("http://localhost:11434/v1")
    net.dns["localhost"] = ["127.0.0.1"]
    # The guarded transport deliberately adds no authorization header by itself;
    # this is only a reachability assertion, so accept the empty test credential.
    net.servers["127.0.0.1"] = openai_server({""})
    local_policy = EndpointPolicy(frozenset({local.origin}), private_only=True)

    async def send_to_local():
        async with guarded_http_client(local, local_policy) as http:
            return await http.get(local.base_url + "/models")

    assert asyncio.run(send_to_local()).status_code == 200
    assert net.connections == [("127.0.0.1", 11434)]


# ------------------------------------------------------------------ migration of existing credentials


def _insert_legacy(conn_rows: list[tuple]) -> None:
    async def insert(conn):
        for config_id, provider, base_url, credential_id, ciphertext, nonce in conn_rows:
            await conn.execute(
                text(
                    "INSERT INTO provider_configs (id, provider, model, display_name, base_url, settings) "
                    "VALUES (:id, :provider, 'acme-1', 'Legacy', :base_url, "
                    "'{\"max_tokens\": 4096, \"temperature\": 1.0}'::jsonb)"
                ),
                {"id": config_id, "provider": provider, "base_url": base_url},
            )
            await conn.execute(
                text(
                    "INSERT INTO provider_credentials (id, provider_config_id, status, ciphertext, nonce, key_id, "
                    "key_hint, verified_at) VALUES (:id, :config, 'pending', :ct, :nonce, 'v1', 'abcd', :now)"
                ),
                {"id": credential_id, "config": config_id, "ct": ciphertext, "nonce": nonce, "now": datetime.now(UTC)},
            )

    run_db(insert)


def _rebind() -> int:
    async def run():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
        try:
            return await rebind_legacy_credentials(async_sessionmaker(engine, expire_on_commit=False), CIPHER)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_existing_credentials_are_rebound_to_their_origin(admin_client, net):
    good_id, bad_id = uuid.uuid4(), uuid.uuid4()
    legacy = CIPHER.encrypt(KEY_A, associated_data=credential_aad(good_id, PROVIDER_ID))  # pre-binding (v1) form
    _insert_legacy([
        (uuid.uuid4(), PROVIDER_ID, "https://LLM.Example.com/v1/", good_id, legacy.ciphertext, legacy.nonce),
        (uuid.uuid4(), "custom-broken", "https://llm.example.com/v1", bad_id, b"garbage-ciphertext", b"0" * 12),
    ])

    assert _rebind() == 1
    rows = {r.id: r for r in credentials()}
    good, bad = rows[good_id], rows[bad_id]
    assert good.endpoint_origin == "https://llm.example.com:443" and bytes(good.ciphertext) != legacy.ciphertext
    assert decrypt(good, origin=good.endpoint_origin) == KEY_A
    with pytest.raises(DecryptionError):
        decrypt(good)  # the legacy binding no longer works
    assert bad.endpoint_origin is None and bytes(bad.ciphertext) == b"garbage-ciphertext"  # left for re-entry
    assert _rebind() == 0  # idempotent
    rebound = [json.loads(d) for a, _, d in audit_rows() if a == "provider.credential_rebound"]
    assert rebound == [{"credential_id": str(good_id), "origin": "https://llm.example.com:443"}]
    assert all(KEY_A not in d for _, _, d in audit_rows())

    result = admin_client.post(f"{PROVIDER}/test", json={}).json()  # the rebound key keeps working
    assert result["ok"] is True and net.authorizations() == {(PUBLIC_IP, f"Bearer {KEY_A}")}


def test_downgrade_shreds_origin_bound_credentials_and_upgrade_restores_the_schema(admin_client, net):
    assert create(admin_client).status_code == 200
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    command.downgrade(cfg, "0008")
    try:
        [row] = run_db(lambda c: c.execute(text("SELECT status, ciphertext, nonce FROM provider_credentials"))).all()
        assert (row.status, row.ciphertext, row.nonce) == ("retired", None, None)
    finally:
        command.upgrade(cfg, "head")
    assert [r.endpoint_origin for r in credentials()] == [None]
