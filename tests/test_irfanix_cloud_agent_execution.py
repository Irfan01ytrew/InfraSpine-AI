import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from fastapi.testclient import TestClient

from app.agent_studio.service import AgentStudioService
from app.db.agent_studio_models import AgentStudioAgent
from app.db.agent_studio_whatsapp_models import AgentStudioWhatsAppConnection, AgentStudioWhatsAppInbound
from app.db.models import User
from app.integrations import whatsapp as cloud_whatsapp
from app.integrations.models import WhatsAppDispatch
from app.integrations import whatsapp_artifacts
from app.main import app
from app.services.agent_studio_whatsapp import _seal
from app.core.config import get_settings
from app.db.session import test_engine_options as _test_engine_options
from app.platform.channels import link_identity
from tests.conftest import ADMIN_EMAIL, TEST_DATABASE_URL, run_db


def test_official_cloud_webhook_runs_agent_and_delivers_status_and_result(monkeypatch, clean_tables):
    connection_id = uuid.uuid4()
    secret = "synthetic-cloud-agent-secret"
    outgoing = []
    executed = []

    # The lifespan starts the worker with baileys_sessions=... (slice 3);
    # the stub must accept that keyword.
    async def disable_background_worker(*_args, **_kwargs):
        return

    async def fake_provider_request(_base, path, *, payload=None, files=None, **_kwargs):
        if path.endswith("/media"):
            assert files["file"][0] == "infraspine-chat-response.pdf"
            return SimpleNamespace(body={"id": "media.out.1"})
        outgoing.append(payload)
        return SimpleNamespace(body={"messages": [{"id": f"wamid.out.{len(outgoing)}"}]})

    async def fake_start_run(self, agent_id, thread_id, prompt, *, idempotency_key=None, execute_inline=False):
        assert execute_inline is True
        assert idempotency_key.startswith("whatsapp-cloud:")
        executed.append((agent_id, thread_id, prompt))
        return SimpleNamespace(status="completed"), {
            "answer": "The verified synthetic task is complete. [Report](/api/exports/download/synthetic-token?format=pdf)"
        }, None

    async def fake_load_file(*_args):
        return whatsapp_artifacts.ArtifactFile("infraspine-chat-response.pdf", "application/pdf", b"synthetic pdf")

    monkeypatch.setattr(cloud_whatsapp, "run_whatsapp_agent_worker", disable_background_worker)
    monkeypatch.setattr(cloud_whatsapp, "provider_request", fake_provider_request)
    monkeypatch.setattr(AgentStudioService, "start_run", fake_start_run)
    monkeypatch.setattr(whatsapp_artifacts, "provider_request", fake_provider_request)
    monkeypatch.setattr(whatsapp_artifacts, "_load_file", fake_load_file)

    with TestClient(app) as client:
        async def seed(db):
            owner = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
            agent_id = uuid.uuid4()
            await db.execute(insert(AgentStudioAgent).values(
                id=agent_id,
                owner_id=owner,
                name="Cloud E2E agent",
                instructions="Verify the synthetic task using the supplied test fixture.",
                tool_permissions=[],
                approval_tools=[],
                delegate_agent_ids=[],
            ))
            encrypted = {}
            for key in ("access_token", "app_secret", "verify_token"):
                value = _seal(app.state.cipher, key, connection_id, secret)
                for part in ("ciphertext", "nonce", "key_id"):
                    encrypted[f"{key}_{part}"] = getattr(value, part)
            await db.execute(insert(AgentStudioWhatsAppConnection).values(
                id=connection_id,
                owner_id=owner,
                agent_id=agent_id,
                enabled=True,
                business_account_id="100001",
                phone_number_id="200001",
                allowed_senders=["30000001"],
                **encrypted,
            ))
            return agent_id

        agent_id = run_db(seed)

        # Slice 2: an allow-listed sender acts only with a verified identity.
        # Link the sender the way the unified-intake tests do, or the worker
        # refuses the inbound as identity_revoked and no run ever starts.
        async def link_sender():
            engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **_test_engine_options())
            try:
                sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
                async with sessionmaker() as session:
                    owner_id = await session.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
                    await link_identity(session, owner_id, "whatsapp_cloud", "30000001",
                                        method="whatsapp:confirmed")
                    await session.commit()
            finally:
                await engine.dispose()

        asyncio.run(link_sender())
        url = f"/api/agent-studio/whatsapp/webhook/{connection_id}"
        body = json.dumps({"entry": [{"id": "100001", "changes": [{"value": {
            "metadata": {"phone_number_id": "200001"},
            "messages": [{
                "id": "wamid.e2e",
                "from": "30000001",
                "timestamp": str(int(datetime.now(UTC).timestamp())),
                "type": "text",
                "text": {"body": "Run this synthetic check"},
            }],
        }}]}]}).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        response = client.post(url, content=body, headers={"X-Hub-Signature-256": f"sha256={signature}"})
        assert response.status_code == 200, response.text

        identity = client.portal.call(cloud_whatsapp.claim, app.state.sessionmaker)
        assert identity is not None
        client.portal.call(
            cloud_whatsapp.process,
            app.state.sessionmaker,
            identity,
            get_settings(),
            app.state.cipher,
            app.state.llm_runtime,
            app.state.rag,
        )

        async def load_result(db):
            inbound_id = await db.scalar(select(AgentStudioWhatsAppInbound.id).where(
                AgentStudioWhatsAppInbound.connection_id == connection_id
            ))
            status = await db.scalar(select(AgentStudioWhatsAppInbound.status).where(
                AgentStudioWhatsAppInbound.id == inbound_id
            ))
            dispatch = (await db.execute(select(
                WhatsAppDispatch.progress_message_id,
                WhatsAppDispatch.provider_message_id,
                WhatsAppDispatch.delivery_status,
            ).where(WhatsAppDispatch.inbound_id == inbound_id))).one()
            artifact = (await db.execute(select(
                whatsapp_artifacts.WhatsAppArtifactDispatch.status,
                whatsapp_artifacts.WhatsAppArtifactDispatch.provider_message_id,
            ).where(whatsapp_artifacts.WhatsAppArtifactDispatch.inbound_id == inbound_id))).one()
            return status, dispatch.progress_message_id, dispatch.provider_message_id, dispatch.delivery_status, artifact

        status, progress_id, result_id, delivery_status, artifact = run_db(load_result)
        assert executed and executed[0][0] == agent_id and executed[0][2] == "Run this synthetic check"
        assert status == "completed"
        assert len(outgoing) == 3
        assert outgoing[0]["text"]["body"].startswith("Request received.")
        assert outgoing[1]["type"] == "document"
        # Step F slice 5: the run completes inline (no execute_once row), so
        # the artifact status claims no success marker without verification.
        assert outgoing[2]["text"]["body"].startswith("Agent task completed.")
        assert not outgoing[2]["text"]["body"].startswith("[SUCCESS]")
        assert "verified synthetic task is complete" in outgoing[2]["text"]["body"]
        assert "WhatsApp accepted 1 output file(s) for delivery" in outgoing[2]["text"]["body"]
        assert "synthetic-token" not in outgoing[2]["text"]["body"]
        assert progress_id == "wamid.out.1"
        assert result_id == "wamid.out.3"
        assert delivery_status == "accepted"
        assert artifact.status == "accepted"
        assert artifact.provider_message_id == "wamid.out.2"
