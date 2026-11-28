"""Official Cloud adapter with crash fencing, service-window checks and receipts.

Reuses the existing encrypted inbox, identity mapping and Agent Studio policy.
Uncertain task execution and sends are held for review instead of duplicated.
"""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agent_studio.service import AgentStudioService, create_thread
from app.core.errors import FileRejectedError, FileTooLargeError, InvalidInputError, NotFoundError, PermissionDeniedError
from app.db.agent_studio_models import AgentStudioAgent
from app.db.agent_studio_whatsapp_models import AgentStudioWhatsAppConnection as Connection, AgentStudioWhatsAppContact as Contact, AgentStudioWhatsAppInbound as Inbound
from app.db.models import User
from app.integrations import whatsapp_templates, whatsapp_voice
from app.integrations.models import WhatsAppArtifactDispatch, WhatsAppDispatch as Dispatch
from app.integrations.transport import request as provider_request, TransportError
from app.integrations.whatsapp_approvals import (
    approval_action_hash,
    approval_buttons,
    approval_card_text,
    approval_summary,
    completed_reply,
    decide_from_whatsapp,
    decode_approval_action,
    parse_approval_command,
)
from app.integrations.whatsapp_artifacts import artifact_refs, artifact_reply, deliver_artifacts
from app.services import agent_studio_whatsapp as legacy, audit
from app.services.audit import AuditContext
from app.whatsapp_auto.knowledge_reply import try_auto_reply

def event_values(payload, connection):
    for entry in payload.get("entry", []):
        if not isinstance(entry, dict) or str(entry.get("id")) != connection.business_account_id: continue
        for change in entry.get("changes", []):
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict): continue
            metadata = value.get("metadata")
            if isinstance(metadata, dict) and str(metadata.get("phone_number_id")) == connection.phone_number_id:
                yield value


# Step F slice 6: a message timestamped far in the past is a replay, not
# a late delivery — Meta retries webhooks for hours, never days. Older
# than this, the inbound is failed at intake and never reaches the worker.
STALE_MESSAGE_MAX_AGE = timedelta(days=7)


async def accept(db, connection_id, raw, signature, cipher):
    accepted = await legacy.accept_webhook(db, connection_id, raw, signature, cipher)
    connection = await db.get(Connection, connection_id)
    payload = json.loads(raw)
    for value in event_values(payload, connection):
        for item in value.get("messages", []) if isinstance(value.get("messages"), list) else []:
            if not isinstance(item, dict): continue
            inbound = await db.scalar(select(Inbound).where(Inbound.connection_id == connection_id,
                Inbound.owner_id == connection.owner_id, Inbound.external_message_id == item.get("id")))
            if inbound is None: continue
            now = datetime.now(UTC)
            try:
                incoming = datetime.fromtimestamp(int(item.get("timestamp", "")), UTC)
                if incoming > now + timedelta(minutes=5):
                    # Step F slice 6: a future-dated message just arrived —
                    # the window runs from arrival, not from a clock-skewed
                    # timestamp (previously this stored NULL and the worker
                    # always treated the message as window-expired).
                    incoming = now
            except (ValueError, OverflowError, TypeError, OSError):
                incoming = now
            if incoming < now - STALE_MESSAGE_MAX_AGE:
                # Step F slice 6: reject the replay at intake — no dispatch
                # row, so claim() never picks it up and no run is created.
                inbound.status = "failed"
                inbound.last_error = "stale_message"
                audit.record(db, AuditContext(connection.owner_id, None, None, None),
                             "integration.whatsapp_stale_message", target=str(inbound.id),
                             details={"age_seconds": int((now - incoming).total_seconds())})
                continue
            await db.execute(pg_insert(Dispatch).values(inbound_id=inbound.id, phase="waiting", incoming_at=incoming)
                             .on_conflict_do_nothing(index_elements=["inbound_id"]))
        for receipt in value.get("statuses", []) if isinstance(value.get("statuses"), list) else []:
            if not isinstance(receipt, dict): continue
            artifact = await db.scalar(select(WhatsAppArtifactDispatch).join(Inbound, Inbound.id == WhatsAppArtifactDispatch.inbound_id).where(
                Inbound.connection_id == connection_id, Inbound.owner_id == connection.owner_id,
                Inbound.from_wa_id == str(receipt.get("recipient_id", "")),
                WhatsAppArtifactDispatch.provider_message_id == receipt.get("id"),
            ).with_for_update(of=WhatsAppArtifactDispatch))
            if artifact is not None:
                status = receipt.get("status")
                rank = {None: 0, "accepted": 0, "failed": 1, "sent": 2, "delivered": 3, "read": 4}
                if status in rank and rank[status] > rank.get(artifact.delivery_status, 0):
                    artifact.delivery_status = status
                    artifact.updated_at = datetime.now(UTC)
                    audit.record(db, AuditContext(connection.owner_id, None, None, None), "integration.whatsapp_artifact_receipt",
                                 target=str(artifact.inbound_id), details={"status": status, "artifact": artifact.artifact_key})
                continue
            row = await db.scalar(select(Dispatch).join(Inbound, Inbound.id == Dispatch.inbound_id).where(
                Inbound.connection_id == connection_id, Inbound.owner_id == connection.owner_id,
                Inbound.from_wa_id == str(receipt.get("recipient_id", "")),
                or_(Dispatch.provider_message_id == receipt.get("id"), Dispatch.progress_message_id == receipt.get("id")),
            ).with_for_update(of=Dispatch))
            if row is None: continue
            status = receipt.get("status")
            rank = {None: 0, "accepted": 0, "failed": 1, "sent": 2, "delivered": 3, "read": 4}
            if row.progress_message_id == receipt.get("id"):
                if status in rank and rank[status] > rank.get(row.progress_status, 0):
                    row.progress_status = status; row.progress_at = datetime.now(UTC)
                    updated = True
                else:
                    updated = False
            elif status in rank and rank[status] > rank.get(row.delivery_status, 0):
                row.delivery_status = status; row.receipt_at = datetime.now(UTC)
                updated = True
            else:
                updated = False
            if updated:
                audit.record(db, AuditContext(connection.owner_id, None, None, None), "integration.whatsapp_receipt",
                             target=str(row.inbound_id), details={"status": status, "progress": row.progress_message_id == receipt.get("id")})
    await db.commit()
    return accepted


async def claim(sessionmaker):
    async with sessionmaker() as db:
        now = datetime.now(UTC)
        stale = (await db.scalars(select(Inbound).where(Inbound.status == "processing", Inbound.claimed_at < now - timedelta(minutes=10))
                                 .with_for_update(skip_locked=True).limit(50))).all()
        for inbound in stale:
            inbound.status = "failed"; inbound.last_error = "execution_or_send_interrupted"
            dispatch = await db.get(Dispatch, inbound.id)
            if dispatch: dispatch.phase = "uncertain"; dispatch.claim_token = None
        inbound = await db.scalar(select(Inbound).join(Dispatch, Dispatch.inbound_id == Inbound.id).where(
            Inbound.status == "queued", Inbound.next_attempt_at <= now, Inbound.attempts < 5,
            Dispatch.phase.in_(("waiting", "prepared", "artifacts", "final_prepared")),
        ).order_by(Inbound.created_at).with_for_update(of=Inbound, skip_locked=True).limit(1))
        if inbound is None: await db.commit(); return None
        dispatch = await db.get(Dispatch, inbound.id)
        inbound.status = "processing"; inbound.claimed_at = now; inbound.attempts += 1
        dispatch.claim_token = uuid.uuid4()
        if not inbound.reply_ciphertext: dispatch.phase = "executing"
        identity = inbound.id, dispatch.claim_token
        await db.commit()
        return identity


async def locked(db, identity):
    inbound = await db.scalar(select(Inbound).where(Inbound.id == identity[0]).with_for_update().execution_options(populate_existing=True))
    dispatch = await db.get(Dispatch, identity[0], populate_existing=True)
    if not inbound or not dispatch or inbound.status != "processing" or dispatch.claim_token != identity[1] or inbound.claimed_at < datetime.now(UTC) - timedelta(minutes=10):
        return None, None
    return inbound, dispatch


async def bound_identity(db, inbound):
    connection = await db.scalar(select(Connection).where(Connection.id == inbound.connection_id,
        Connection.owner_id == inbound.owner_id, Connection.enabled.is_(True)).with_for_update())
    if connection is None:
        raise TransportError("identity_revoked")
    from app.platform.channels import resolve_whatsapp_sender, sync_allowed_senders
    await sync_allowed_senders(db, owner_id=connection.owner_id, channel="whatsapp_cloud",
                               allowed=list(connection.allowed_senders or []))
    try:
        principal = await resolve_whatsapp_sender(
            db, channel="whatsapp_cloud", sender=inbound.from_wa_id,
            allowed=list(connection.allowed_senders or []), owner_id=connection.owner_id,
        )
    except PermissionDeniedError:
        raise TransportError("identity_revoked") from None
    user = await db.scalar(select(User).where(User.id == principal.user_id, User.is_active.is_(True)).with_for_update())
    if user is None:
        raise TransportError("identity_revoked")
    agent = await db.scalar(select(AgentStudioAgent).where(AgentStudioAgent.id == connection.agent_id,
        AgentStudioAgent.owner_id == inbound.owner_id, AgentStudioAgent.enabled.is_(True)))
    if agent is None: raise TransportError("agent_unavailable")
    return connection, user, agent


async def send_text(connection, inbound, cipher, text):
    response = await provider_request(
        "https://graph.facebook.com/" + connection.api_version,
        "/" + connection.phone_number_id + "/messages",
        method="POST",
        write=True,
        token=legacy._secret(connection, cipher, "access_token"),
        payload={
            "messaging_product": "whatsapp",
            "to": inbound.from_wa_id,
            "type": "text",
            "text": {"preview_url": False, "body": text.strip()[:4000]},
        },
    )
    try:
        provider_id = response.body["messages"][0]["id"]
        if not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 160:
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise TransportError("missing_message_identifier", ambiguous=True) from None
    return provider_id


async def send_interactive(connection, inbound, cipher, body, buttons):
    """Send a Meta Cloud API interactive reply-button message.

    Step F slice 5: approval cards go out as interactive buttons on Cloud
    (``body`` ≤ 1024 chars, button ids ≤ 256, titles ≤ 20). Same provider-id
    extraction and error mapping as :func:`send_text`.
    """
    response = await provider_request(
        "https://graph.facebook.com/" + connection.api_version,
        "/" + connection.phone_number_id + "/messages",
        method="POST",
        write=True,
        token=legacy._secret(connection, cipher, "access_token"),
        payload={
            "messaging_product": "whatsapp",
            "to": inbound.from_wa_id,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body.strip()[:1024]},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": button_id, "title": title}}
                    for button_id, title in buttons
                ]},
            },
        },
    )
    try:
        provider_id = response.body["messages"][0]["id"]
        if not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 160:
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise TransportError("missing_message_identifier", ambiguous=True) from None
    return provider_id


async def send_template(connection, inbound, cipher, template_name, language, parameters):
    """Send a Meta Cloud API template message (Step F slice 6).

    Only templates from the catalogue may be sent — the name, language
    and parameter count are validated before any network call. Same
    provider-id extraction and error mapping as :func:`send_text`.
    """
    whatsapp_templates.validate_template(template_name, language, parameters)
    response = await provider_request(
        "https://graph.facebook.com/" + connection.api_version,
        "/" + connection.phone_number_id + "/messages",
        method="POST",
        write=True,
        token=legacy._secret(connection, cipher, "access_token"),
        payload={
            "messaging_product": "whatsapp",
            "to": inbound.from_wa_id,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": [
                    {"type": "body", "parameters": [
                        {"type": "text", "text": parameter} for parameter in parameters
                    ]},
                ],
            },
        },
    )
    try:
        provider_id = response.body["messages"][0]["id"]
        if not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 160:
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise TransportError("missing_message_identifier", ambiguous=True) from None
    return provider_id


async def send_template_once(db, user, connection, inbound, cipher, template_name, language, parameters, *, key, baileys_sessions=None):
    """Send a template message exactly once (Step F slice 6).

    The same ``dispatch_once`` + failover adapter as
    :func:`send_text_once`: Cloud sends the structured template; a Baileys
    leg (primary by the owner's path setting, or failover) sends the
    rendered text, which needs no template — a normal WhatsApp account has
    no 24-hour window. The same idempotency key covers both legs.
    """
    from app.platform.access import principal_for
    from app.platform.approvals import ActionSpec
    from app.platform.side_effects import dispatch_once, org_scope_id

    p = principal_for(user, channel="whatsapp_cloud")
    scope_id = await org_scope_id(db)
    action = ActionSpec("whatsapp.send", scope_id,
                        {"to": inbound.from_wa_id, "inbound_id": str(inbound.id), "key": key,
                         "kind": "template", "template": template_name},
                        subject=f"whatsapp:{inbound.id}")

    async def dispatch():
        from app.integrations.whatsapp_failover import (
            BaileysTransport,
            CloudTransport,
            get_path_setting,
            live_baileys_session,
            phone_to_jid,
            send_with_failover,
        )

        setting = await get_path_setting(db, inbound.owner_id)
        cloud = CloudTransport(connection, inbound, cipher)
        session = await live_baileys_session(db, baileys_sessions, inbound.owner_id)
        baileys = BaileysTransport(session) if session is not None else None

        async def on_failover(from_path, to_path, reason, failover_key, *, outcome, fallback_code=None):
            audit.record(db, AuditContext(inbound.owner_id, None, None, None), "integration.whatsapp_failover",
                         outcome=outcome, target=failover_key,
                         details={"from_path": from_path, "to_path": to_path, "reason": reason,
                                  "fallback_code": fallback_code})

        ref, _used = await send_with_failover(
            setting=setting, own_channel="cloud",
            to={"cloud": inbound.from_wa_id, "baileys": phone_to_jid(inbound.from_wa_id)},
            text=whatsapp_templates.render_template_text(template_name, parameters),
            key=key, cloud=cloud, baileys=baileys, on_failover=on_failover,
            template=(template_name, language, parameters),
        )
        return ref

    try:
        result = await dispatch_once(db, p, key, action, duty="channel.reply", dispatch=dispatch)
    except (NotFoundError, PermissionDeniedError) as exc:
        raise TransportError("identity_revoked") from exc
    if result.status == "failed" or not result.external_ref:
        raise TransportError("missing_message_identifier", ambiguous=True)
    return result.external_ref


async def send_text_once(db, user, connection, inbound, cipher, text, *, key, baileys_sessions=None):
    """Send a WhatsApp Cloud API text exactly once (retries replay the stored provider id).

    Step F slice 3: the send goes through the failover adapter — Cloud first
    (or the owner's path setting), Baileys once when Cloud fails with an
    error that confirmed nothing. The adapter runs inside ``dispatch_once``,
    so the same idempotency key covers both legs and the reply is never
    sent twice.
    """
    from app.platform.access import principal_for
    from app.platform.approvals import ActionSpec
    from app.platform.side_effects import dispatch_once, org_scope_id

    p = principal_for(user, channel="whatsapp_cloud")
    scope_id = await org_scope_id(db)
    action = ActionSpec("whatsapp.send", scope_id, {"to": inbound.from_wa_id, "inbound_id": str(inbound.id), "key": key},
                        subject=f"whatsapp:{inbound.id}")

    async def dispatch():
        from app.integrations.whatsapp_failover import (
            BaileysTransport,
            CloudTransport,
            get_path_setting,
            live_baileys_session,
            phone_to_jid,
            send_with_failover,
        )

        setting = await get_path_setting(db, inbound.owner_id)
        cloud = CloudTransport(connection, inbound, cipher)
        session = await live_baileys_session(db, baileys_sessions, inbound.owner_id)
        baileys = BaileysTransport(session) if session is not None else None

        async def on_failover(from_path, to_path, reason, failover_key, *, outcome, fallback_code=None):
            audit.record(db, AuditContext(inbound.owner_id, None, None, None), "integration.whatsapp_failover",
                         outcome=outcome, target=failover_key,
                         details={"from_path": from_path, "to_path": to_path, "reason": reason,
                                  "fallback_code": fallback_code})

        ref, _used = await send_with_failover(
            setting=setting, own_channel="cloud",
            to={"cloud": inbound.from_wa_id, "baileys": phone_to_jid(inbound.from_wa_id)},
            text=text, key=key, cloud=cloud, baileys=baileys, on_failover=on_failover,
        )
        return ref

    try:
        result = await dispatch_once(db, p, key, action, duty="channel.reply", dispatch=dispatch)
    except (NotFoundError, PermissionDeniedError) as exc:
        raise TransportError("identity_revoked") from exc
    if result.status == "failed" or not result.external_ref:
        raise TransportError("missing_message_identifier", ambiguous=True)
    return result.external_ref


async def send_interactive_once(db, user, connection, inbound, cipher, body, buttons, *, key, baileys_sessions=None):
    """Send an approval card exactly once: interactive buttons on Cloud, plain text elsewhere.

    Step F slice 5: the same ``dispatch_once`` + failover adapter as
    :func:`send_text_once`. Cloud sends the interactive message; a Baileys
    leg (primary by the owner's path setting, or failover when Cloud fails
    with an eligible error) sends the same card as text, whose
    APPROVE/REJECT commands carry the same action hash.
    """
    from app.platform.access import principal_for
    from app.platform.approvals import ActionSpec
    from app.platform.side_effects import dispatch_once, org_scope_id

    p = principal_for(user, channel="whatsapp_cloud")
    scope_id = await org_scope_id(db)
    action = ActionSpec("whatsapp.send", scope_id, {"to": inbound.from_wa_id, "inbound_id": str(inbound.id), "key": key},
                        subject=f"whatsapp:{inbound.id}")

    async def dispatch():
        from app.integrations.whatsapp_failover import (
            BaileysTransport,
            CloudTransport,
            get_path_setting,
            live_baileys_session,
            phone_to_jid,
            send_with_failover,
        )

        setting = await get_path_setting(db, inbound.owner_id)
        cloud = CloudTransport(connection, inbound, cipher)
        session = await live_baileys_session(db, baileys_sessions, inbound.owner_id)
        baileys = BaileysTransport(session) if session is not None else None

        async def on_failover(from_path, to_path, reason, failover_key, *, outcome, fallback_code=None):
            audit.record(db, AuditContext(inbound.owner_id, None, None, None), "integration.whatsapp_failover",
                         outcome=outcome, target=failover_key,
                         details={"from_path": from_path, "to_path": to_path, "reason": reason,
                                  "fallback_code": fallback_code})

        ref, _used = await send_with_failover(
            setting=setting, own_channel="cloud",
            to={"cloud": inbound.from_wa_id, "baileys": phone_to_jid(inbound.from_wa_id)},
            text=body, key=key, cloud=cloud, baileys=baileys, on_failover=on_failover,
            interactive=(body, buttons),
        )
        return ref

    try:
        result = await dispatch_once(db, p, key, action, duty="channel.reply", dispatch=dispatch)
    except (NotFoundError, PermissionDeniedError) as exc:
        raise TransportError("identity_revoked") from exc
    if result.status == "failed" or not result.external_ref:
        raise TransportError("missing_message_identifier", ambiguous=True)
    return result.external_ref


async def _transcribe_inbound_voice(db, inbound, connection, cipher, runtime):
    """Transcribe a Cloud audio inbound; returns the transcript or None.

    None means the user gets a "[NEEDS INPUT]" reply (no crash, no retry):
    a missing media reference, oversized audio, an unsupported format, no
    speech, or no transcriber available. The staged audio is always removed.
    """
    try:
        media_id = legacy._open(cipher, "inbound", inbound.id, inbound.text_ciphertext, inbound.text_nonce, inbound.text_key_id)
        audio_bytes, suffix = await whatsapp_voice.download_cloud_audio(
            media_id, api_version=connection.api_version,
            access_token=legacy._secret(connection, cipher, "access_token"),
        )
        transcript, source = await whatsapp_voice.transcribe_voice_note(audio_bytes, suffix, runtime=runtime, db=db)
    except (FileTooLargeError, FileRejectedError, InvalidInputError, TransportError) as exc:
        audit.record(db, AuditContext(inbound.owner_id, None, None, None), "integration.whatsapp_voice_failed",
                     target=str(inbound.id), details={"error": getattr(exc, "code", type(exc).__name__)})
        return None
    except Exception as exc:
        # A corrupt sealed reference or an unexpected download failure is
        # still just an unusable voice note, never a worker crash.
        audit.record(db, AuditContext(inbound.owner_id, None, None, None), "integration.whatsapp_voice_failed",
                     target=str(inbound.id), details={"error": type(exc).__name__})
        return None
    audit.record(db, AuditContext(inbound.owner_id, None, None, None), "integration.whatsapp_voice_transcribed",
                 target=str(inbound.id), details={"source": source, "bytes": len(audio_bytes)})
    return transcript


def _window_expired(dispatch) -> bool:
    """True when the 24-hour customer-service window for this inbound is over."""
    return not dispatch.incoming_at or datetime.now(UTC) >= dispatch.incoming_at + timedelta(hours=24)


async def _reopen_expired_window(db, user, connection, inbound, dispatch, cipher, summary, *, baileys_sessions=None):
    """Re-open an expired window with the task-ready template (Step F slice 6).

    The free-form reply cannot go out after 24 hours, so the user gets the
    approved template instead and the inbound completes — the task itself
    is not re-run, and the reply stays sealed on the inbound record. When
    the template send fails, the original window error is raised so the
    existing failure handling applies (no silent loss).
    """
    try:
        provider_id = await send_template_once(
            db, user, connection, inbound, cipher,
            whatsapp_templates.TASK_READY_TEMPLATE, whatsapp_templates.TASK_READY_LANGUAGE,
            [whatsapp_templates.template_summary(summary)],
            key=f"whatsapp:{inbound.id}:template:{whatsapp_templates.TASK_READY_TEMPLATE}",
            baileys_sessions=baileys_sessions,
        )
    except TransportError:
        raise TransportError("messaging_window_expired") from None
    dispatch.provider_message_id = provider_id
    dispatch.phase = "accepted"
    dispatch.delivery_status = "accepted"
    dispatch.claim_token = None
    inbound.status = "completed"
    inbound.claimed_at = None
    inbound.last_error = None
    audit.record(db, AuditContext(inbound.owner_id, None, None, None),
                 "integration.whatsapp_template_reopen", target=str(inbound.id),
                 details={"template": whatsapp_templates.TASK_READY_TEMPLATE})
    await db.commit()


async def process(sessionmaker, identity, settings, cipher, runtime, rag, baileys_sessions=None):
    error = None
    try:
        async with sessionmaker() as db:
            inbound, dispatch = await locked(db, identity)
            if inbound is None: return
            connection, user, agent = await bound_identity(db, inbound)
            if _window_expired(dispatch):
                await _reopen_expired_window(db, user, connection, inbound, dispatch, cipher, None,
                                             baileys_sessions=baileys_sessions)
                return
            if inbound.reply_ciphertext:
                reply = legacy._open(cipher, "reply", inbound.id, inbound.reply_ciphertext, inbound.reply_nonce, inbound.reply_key_id)
                message = None
            elif inbound.message_type == "audio":
                # Step F slice 4: the sealed text is the Meta media id, not
                # user text. A transcript becomes the task (marked as machine
                # text); anything unusable gets a "[NEEDS INPUT]" reply and
                # the inbound still completes.
                transcript = await _transcribe_inbound_voice(db, inbound, connection, cipher, runtime)
                if transcript is None:
                    reply = "[NEEDS INPUT] I could not understand that voice note. Please resend your request as text, or send the voice note again."
                    message = None
                else:
                    inbound.message_type = "voice"
                    await db.commit()
                    message = whatsapp_voice.mark_machine_text(transcript)
            elif inbound.message_type != "text":
                reply = "[NEEDS INPUT] This connection currently accepts text tasks. Please resend your request as text or contact a human operator for this attachment."
                message = None
            else:
                message = legacy._open(cipher, "inbound", inbound.id, inbound.text_ciphertext, inbound.text_nonce, inbound.text_key_id)
            # Step F slice 6: a spoken approval arrives as a marked
            # transcript — strip the "[Voice note transcript] " marker
            # before parsing, so the decision is recognized; the task text
            # itself keeps its marker. This check sits after the
            # text/audio branches (not inside the text branch) so it
            # applies to spoken approvals too, and it still runs BEFORE
            # the AE auto-reply and the task path below.
            approval_cmd = parse_approval_command(
                whatsapp_voice.strip_machine_text_prefix(message)) if isinstance(message, str) else None
            if approval_cmd is not None:
                # Step F slice 5: an approval decision is not a task —
                # check the precise command first so it is never
                # answered as a Knowledge question or run as an agent
                # task. Decide it through the Agent Studio service and
                # reply; no agent run, no progress message.
                decision, approval_id, presented_hash = approval_cmd
                reply = await decide_from_whatsapp(
                    db, principal_user_id=user.id, approval_id=approval_id, decision=decision,
                    presented_hash=presented_hash, cipher=cipher, settings=settings,
                    runtime=runtime, rag=rag, channel="whatsapp_cloud",
                )
                message = None
            if message is not None and inbound.message_type == "text":
                # Step AE: try the customer auto-reply first (a Knowledge
                # answer from the customer-visible documents, or lead
                # capture). A reply here skips the progress message and
                # the Agent Studio task run; anything else falls through
                # to the task path below, unchanged.
                auto_reply = await try_auto_reply(db, user, settings, runtime, rag, inbound, message)
                if auto_reply is not None:
                    reply = auto_reply
                    message = None
            run = None
            approval_card = None
            if message is not None:
                if dispatch.progress_message_id is None:
                    dispatch.phase = "progress_sending"
                    await db.commit()
                    progress_id = await send_text_once(
                        db,
                        user,
                        connection,
                        inbound,
                        cipher,
                        "Request received. The agent is working on it; I will send the result here.",
                        key=f"whatsapp:{inbound.id}:progress",
                        baileys_sessions=baileys_sessions,
                    )
                    inbound, dispatch = await locked(db, identity)
                    if inbound is None: return
                    dispatch.progress_message_id = progress_id
                    dispatch.progress_status = "accepted"
                    dispatch.progress_at = datetime.now(UTC)
                    dispatch.phase = "executing"
                    await db.commit()
                contact = await db.scalar(select(Contact).where(Contact.connection_id == connection.id,
                    Contact.owner_id == user.id, Contact.wa_id == inbound.from_wa_id))
                if contact is None:
                    thread = await create_thread(db, user, cipher, agent.id, "WhatsApp · " + inbound.from_wa_id[-4:])
                    await db.execute(pg_insert(Contact).values(id=uuid.uuid4(), connection_id=connection.id, owner_id=user.id,
                        wa_id=inbound.from_wa_id, thread_id=thread.id).on_conflict_do_nothing(index_elements=["connection_id", "wa_id"]))
                    await db.commit()
                    contact = await db.scalar(select(Contact).where(Contact.connection_id == connection.id,
                        Contact.owner_id == user.id, Contact.wa_id == inbound.from_wa_id))
                run, state, pending_approval = await AgentStudioService(db, user, settings, cipher, runtime, rag).start_run(
                    agent.id,
                    contact.thread_id,
                    message,
                    idempotency_key=f"whatsapp-cloud:{inbound.id}",
                    execute_inline=True,
                )
                answer = str(state.get("answer") or "").strip()
                if run.status == "awaiting_approval":
                    if pending_approval is not None:
                        # Step F slice 5: send the approval card on this
                        # channel, bound to the exact action hash.
                        from app.platform.side_effects import org_scope_id

                        scope_id = await org_scope_id(db)
                        action_hash = approval_action_hash(cipher, pending_approval, scope_id)
                        action = decode_approval_action(cipher, pending_approval)
                        reply = approval_card_text(
                            tool_name=pending_approval.tool_name,
                            summary=approval_summary(pending_approval.tool_name, action),
                            approval_id=pending_approval.id, action_hash=action_hash,
                        )
                        approval_card = (pending_approval.id, action_hash)
                    else:
                        reply = "[REQUIRES APPROVAL] This task is waiting for approval in InfraSpine AI → AI Agents → Approvals & execution log."
                elif run.status == "completed" and answer:
                    # Step F slice 5: "Done (verified)" only when the
                    # execute_once read-back verified the attempt.
                    reply = await completed_reply(db, run, answer)
                elif run.status == "failed":
                    reply = "[FAILED] " + (answer or "The agent could not verify a completed result. Check the AI Agents execution log.")
                else:
                    reply = "[IN PROGRESS] This task is still queued or running. Check the AI Agents execution log for updates."
            inbound, dispatch = await locked(db, identity)
            if inbound is None: return
            connection, _, _ = await bound_identity(db, inbound)
            if _window_expired(dispatch):
                # Step F slice 6: the window closed while the agent ran —
                # the reply cannot go out as free-form text, so re-open
                # with the template (the reply stays sealed on the record).
                await _reopen_expired_window(db, user, connection, inbound, dispatch, cipher, reply,
                                             baileys_sessions=baileys_sessions)
                return
            if dispatch.phase not in {"artifacts", "final_prepared"}:
                encrypted = legacy._seal(cipher, "reply", inbound.id, reply[:12000])
                inbound.reply_ciphertext, inbound.reply_nonce, inbound.reply_key_id = encrypted.ciphertext, encrypted.nonce, encrypted.key_id
                dispatch.phase = "artifacts" if run is not None and run.status == "completed" and artifact_refs(reply) else "final_prepared"
                await db.commit()
            if dispatch.phase == "artifacts":
                results = await deliver_artifacts(db, connection, inbound, user, settings, cipher, reply)
                reply = artifact_reply(reply, results)
                inbound, dispatch = await locked(db, identity)
                if inbound is None: return
                encrypted = legacy._seal(cipher, "reply", inbound.id, reply[:12000])
                inbound.reply_ciphertext, inbound.reply_nonce, inbound.reply_key_id = encrypted.ciphertext, encrypted.nonce, encrypted.key_id
                dispatch.phase = "final_prepared"
                await db.commit()
            inbound, dispatch = await locked(db, identity)
            if inbound is None: return
            connection, _, _ = await bound_identity(db, inbound)
            dispatch.phase = "final_sending"
            await db.commit()
            inbound, dispatch = await locked(db, identity)
            if inbound is None: return
            connection, _, _ = await bound_identity(db, inbound)
            if approval_card is not None:
                # Step F slice 5: interactive buttons on Cloud, card text on
                # Baileys (failover adapter). The text card is the fallback
                # when the interactive send fails with an eligible error.
                provider_id = await send_interactive_once(
                    db, user, connection, inbound, cipher, reply,
                    approval_buttons(approval_card[0], approval_card[1]),
                    key=f"whatsapp:{inbound.id}:approval-card",
                    baileys_sessions=baileys_sessions,
                )
            else:
                provider_id = await send_text_once(db, user, connection, inbound, cipher, reply,
                                                    key=f"whatsapp:{inbound.id}:final",
                                                    baileys_sessions=baileys_sessions)
            dispatch.provider_message_id = provider_id; dispatch.phase = "accepted"; dispatch.delivery_status = "accepted"
            dispatch.claim_token = None; inbound.status = "completed"; inbound.claimed_at = None; inbound.last_error = None
            audit.record(db, AuditContext(inbound.owner_id, None, None, None), "integration.whatsapp_accepted", target=str(inbound.id))
            await db.commit()
    except TransportError as exc: error = exc
    except Exception: error = TransportError("execution_or_send_interrupted", ambiguous=True)
    if error:
        async with sessionmaker() as db:
            inbound, dispatch = await locked(db, identity)
            if inbound is None: return
            retry = (
                error.code == "rate_limited"
                and inbound.attempts < 5
                and (inbound.reply_ciphertext is not None or dispatch.progress_message_id is None)
            )
            inbound.status = "queued" if retry else "failed"
            inbound.last_error = error.code; inbound.claimed_at = None
            inbound.next_attempt_at = datetime.now(UTC) + timedelta(seconds=max(30, error.retry_after))
            if retry:
                dispatch.phase = "final_prepared" if dispatch.phase == "final_sending" else (
                    dispatch.phase if dispatch.phase == "artifacts" else "prepared"
                )
            else:
                dispatch.phase = "uncertain" if error.ambiguous else "failed"
            dispatch.claim_token = None
            audit.record(db, AuditContext(inbound.owner_id, None, None, None), "integration.whatsapp_failed", target=str(inbound.id),
                         details={"error": error.code, "phase": dispatch.phase})
            await db.commit()


async def run_whatsapp_agent_worker(sessionmaker, settings, cipher, runtime, rag, stop, baileys_sessions=None):
    while not stop.is_set():
        try:
            identity = await claim(sessionmaker)
            if identity:
                await process(sessionmaker, identity, settings, cipher, runtime, rag, baileys_sessions)
                continue
        except asyncio.CancelledError: raise
        except Exception: pass  # no raw payloads or credential-bearing exceptions in logs
        try: await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError: pass
