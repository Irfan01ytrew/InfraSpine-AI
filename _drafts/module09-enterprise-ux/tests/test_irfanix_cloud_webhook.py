import hashlib
import hmac
import json
import uuid

from sqlalchemy import func, insert, select
from fastapi.testclient import TestClient

from app.db.agent_studio_models import AgentStudioAgent
from app.db.agent_studio_whatsapp_models import AgentStudioWhatsAppConnection, AgentStudioWhatsAppInbound
from app.db.models import User
from app.main import app
from app.services import agent_studio_whatsapp
from app.services.agent_studio_whatsapp import MAX_WEBHOOK_BYTES, _seal
from tests.conftest import ADMIN_EMAIL, run_db


def test_cloud_webhook_bounds_and_authenticates_the_inbound_queue(monkeypatch):
    async def disable_outbound_worker(*_args):
        return

    monkeypatch.setattr(agent_studio_whatsapp, "run_whatsapp_agent_worker", disable_outbound_worker)
    with TestClient(app) as client:
        check_webhook(client)


def check_webhook(client):
    connection = uuid.uuid4()
    secret = "synthetic-webhook-signing-secret"

    async def seed(db):
        owner = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        agent = uuid.uuid4()
        await db.execute(insert(AgentStudioAgent).values(
            id=agent, owner_id=owner, name="Cloud webhook test", instructions="Test fixture",
        ))
        encrypted = {}
        for key in ("access_token", "app_secret", "verify_token"):
            value = _seal(app.state.cipher, key, connection, secret)
            for part in ("ciphertext", "nonce", "key_id"):
                encrypted[f"{key}_{part}"] = getattr(value, part)
        await db.execute(insert(AgentStudioWhatsAppConnection).values(
            id=connection, owner_id=owner, agent_id=agent, enabled=True,
            business_account_id="100000", phone_number_id="200000", allowed_senders=["30000000"],
            **encrypted,
        ))

    run_db(seed)
    url = f"/api/agent-studio/whatsapp/webhook/{connection}"

    def submit(*, account="100000", number="200000", sender="30000000", signed=True):
        data = json.dumps({"entry": [{"id": account, "changes": [{"value": {
            "metadata": {"phone_number_id": number},
            "messages": [{"id": "wamid.synthetic", "from": sender, "type": "text", "text": {"body": "hello"}}],
        }}]}]}).encode()
        signature = hmac.new(secret.encode(), data, hashlib.sha256).hexdigest() if signed else "0" * 64
        return client.post(url, content=data, headers={"X-Hub-Signature-256": f"sha256={signature}"})

    def queued():
        return run_db(lambda db: db.scalar(select(func.count()).select_from(
            AgentStudioWhatsAppInbound,
        ).where(AgentStudioWhatsAppInbound.connection_id == connection)))

    assert submit(signed=False).status_code == 422
    assert client.post(url, content=b" " * (MAX_WEBHOOK_BYTES + 1)).status_code == 422
    assert submit(account="999999").status_code == 200
    assert submit(number="999999").status_code == 200
    assert submit(sender="99999999").status_code == 200
    assert queued() == 0
    assert submit().status_code == submit().status_code == 200
    assert queued() == 1
