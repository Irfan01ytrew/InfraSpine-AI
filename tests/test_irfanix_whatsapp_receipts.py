import json
import uuid

from sqlalchemy import insert, select

from app.db.agent_studio_baileys_models import BaileysConnection, BaileysMessage
from app.db.agent_studio_models import AgentStudioAgent
from app.db.models import User
from app.core.config import get_settings
from app.main import app
from app.services import agent_studio_baileys
from app.services.agent_studio_baileys import BridgeSession, record_receipt, seal
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db


def test_whatsapp_receipts_are_scoped_to_the_reply_and_never_regress(client, monkeypatch):
    monkeypatch.setattr(agent_studio_baileys, "bridge_available", lambda: False)
    async def seed(conn):
        connections = []
        for email in (ADMIN_EMAIL, USER_EMAIL):
            owner = await conn.scalar(select(User.id).where(User.email == email))
            agent, connection = uuid.uuid4(), uuid.uuid4()
            await conn.execute(insert(AgentStudioAgent).values(
                id=agent, owner_id=owner, name="Receipt test", instructions="Test fixture",
            ))
            await conn.execute(insert(BaileysConnection).values(id=connection, owner_id=owner, agent_id=agent))
            connections.append(connection)
        reply_id, pending_id = uuid.uuid4(), uuid.uuid4()
        for mid, status, reply in ((reply_id, "sending", "sealed"), (pending_id, "queued", None)):
            await conn.execute(insert(BaileysMessage).values(
                id=mid, connection_id=connections[0], external_id=mid.hex,
                payload_sealed="sealed", reply_sealed=reply, status=status,
            ))
        return (*connections, reply_id, pending_id)

    sender, other, reply, pending = run_db(seed)

    async def verify_updates(conn):
        async def status(mid):
            return await conn.scalar(select(BaileysMessage.receipt_status).where(BaileysMessage.id == mid))

        await record_receipt(conn, other, reply.hex.upper(), 4)
        await record_receipt(conn, sender, pending.hex.upper(), 4)
        await record_receipt(conn, sender, reply.hex.upper(), 5)
        assert await status(reply) is None and await status(pending) is None
        await record_receipt(conn, sender, reply.hex.upper(), 3)
        await record_receipt(conn, sender, reply.hex.upper(), 2)
        assert await status(reply) == 3
        await record_receipt(conn, sender, reply.hex.upper(), 4)
        assert await status(reply) == 4

    run_db(verify_updates)
    login(client, USER_EMAIL)
    cancel_url = f"/api/agent-studio/whatsapp-qr/commands/{pending}/cancel"
    assert client.post(cancel_url).status_code == 404
    assert client.get("/api/agent-studio/whatsapp-qr").json()["connection"]["latest_command"] is None


    login(client, ADMIN_EMAIL)
    assert client.post(f"/api/agent-studio/whatsapp-qr/commands/{reply}/cancel").status_code == 409
    assert client.post(cancel_url).status_code == 204
    assert client.post(cancel_url).status_code == 204
    assert run_db(lambda conn: conn.scalar(
        select(BaileysMessage.status).where(BaileysMessage.id == pending),
    )) == "cancelled"
    run_db(lambda conn: conn.execute(BaileysMessage.__table__.delete().where(BaileysMessage.id == pending)))
    response = client.get("/api/agent-studio/whatsapp-qr")
    assert response.status_code == 200
    command = response.json()["connection"]["latest_command"]
    assert set(command) == {"id", "status", "receipt", "received_at"}
    assert command["receipt"] == "read"
    login(client, USER_EMAIL)
    assert client.get("/api/agent-studio/whatsapp-qr").json()["connection"]["latest_command"] is None

def test_reply_prepared_during_disconnect_delivers_without_rerunning(client, monkeypatch):
    monkeypatch.setattr(agent_studio_baileys, "bridge_available", lambda: False)
    connection_id, mid = uuid.uuid4(), uuid.uuid4()
    executions = []
    deliveries = []

    def diagnostics():
        executions.append(True)
        return "Measured diagnostics"

    monkeypatch.setattr(agent_studio_baileys, "windows_host_status", diagnostics)

    async def seed(db):
        owner = await db.scalar(select(User.id).where(User.email == ADMIN_EMAIL))
        agent = uuid.uuid4()
        await db.execute(insert(AgentStudioAgent).values(
            id=agent, owner_id=owner, name="IRFANIX", instructions="Test fixture",
        ))
        await db.execute(insert(BaileysConnection).values(id=connection_id, owner_id=owner, agent_id=agent))
        await db.execute(insert(BaileysMessage).values(
            id=mid, connection_id=connection_id, external_id=mid.hex, status="processing",
            payload_sealed=seal(app.state.cipher, f"message:{mid}", json.dumps({
                "text": "laptop status", "jid": "10000000@s.whatsapp.net",
            })),
        ))

    run_db(seed)

    async def run():
        bridge = BridgeSession(app, get_settings(), connection_id)

        async def send_reply(delivery_id, _jid, reply, *, timeout):
            deliveries.append((delivery_id, reply))
            return True

        bridge.send_reply = send_reply
        await bridge.execute(mid)
        async with app.state.sessionmaker() as db:
            assert (await db.get(BaileysMessage, mid)).status == "ready"
        assert deliveries == []
        bridge.online.set()
        await bridge.execute(mid)
        async with app.state.sessionmaker() as db:
            assert (await db.get(BaileysMessage, mid)).status == "completed"

    client.portal.call(run)
    assert len(executions) == 1
    assert len(deliveries) == 1 and deliveries[0][0] == mid.hex.upper()
    assert "Measured diagnostics" in deliveries[0][1]
