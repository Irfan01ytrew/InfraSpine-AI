"""Collaboration routes for Slack and Microsoft Teams bots."""

import json
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.api.deps import AppSettings
from app.services import collaboration

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.get("/status")
async def integration_status(settings: AppSettings) -> dict[str, Any]:
    """Return status of communication platform integrations."""
    return {
        "slack": {
            "enabled": settings.slack_enabled,
            "has_signing_secret": bool(settings.slack_signing_secret.get_secret_value()),
            "permission_scoped_answers_available": False,
        },
        "teams": {
            "enabled": settings.teams_enabled,
            "has_app_id": bool(settings.teams_app_id),
            "bot_framework_authentication_available": False,
        },
    }


@router.post("/slack/events")
async def slack_events(
    request: Request,
    settings: AppSettings,
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
) -> Any:
    """Handle Slack Event API events including URL verification and bot mentions."""
    if not settings.slack_enabled or not settings.slack_signing_secret.get_secret_value():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Slack integration is not configured")
    body_bytes = await request.body()
    if len(body_bytes) > 1_048_576:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Slack event is too large")
    secret = settings.slack_signing_secret.get_secret_value()
    if not collaboration.verify_slack_signature(
        body_bytes,
        x_slack_request_timestamp or "",
        x_slack_signature or "",
        secret,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Slack signature")

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload")

    # Slack's URL-verification challenge is signed too; verify before returning it.
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    event = payload.get("event", {})
    event_type = event.get("type")

    if event_type in ("app_mention", "message"):
        # A verified webhook still needs a mapped local identity and ACL-filtered RAG call.
        # Acknowledge it without inventing an answer or claiming a document was searched.
        return {"status": "received", "answer_available": False}

    return {"status": "ok"}


@router.post(
    "/slack/command",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/x-www-form-urlencoded": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "text": {"type": "string"},
                            "user_id": {"type": "string"},
                            "user_name": {"type": "string"},
                        },
                        "required": ["command"],
                    }
                }
            }
        }
    },
)
async def slack_command(
    request: Request,
    settings: AppSettings,
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
) -> Any:
    """Handle Slack slash command (e.g. /ask policy details)."""
    if not settings.slack_enabled or not settings.slack_signing_secret.get_secret_value():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Slack integration is not configured")
    body_bytes = await request.body()
    if len(body_bytes) > 65_536:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Slack command is too large")
    secret = settings.slack_signing_secret.get_secret_value()
    if not collaboration.verify_slack_signature(
        body_bytes,
        x_slack_request_timestamp or "",
        x_slack_signature or "",
        secret,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Slack signature")

    form_data = await request.form()
    text = str(form_data.get("text", ""))
    if not text.strip():
        return {
            "response_type": "ephemeral",
            "text": "Please provide a question after the command, e.g. `/ask What is the refund policy?`",
        }

    return collaboration.slack_search_unavailable_response()


@router.post("/teams/messages")
async def teams_messages(
    settings: AppSettings,
) -> Any:
    """Handle Microsoft Teams Bot Framework activity JSON."""
    if not settings.teams_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Teams integration is not configured")
    # Do not trust an arbitrary HTTP caller as a Teams bot. Bot Framework JWT validation and
    # permission-scoped identity mapping are not implemented yet, so refuse to process activity.
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Teams webhook authentication and permission-scoped retrieval are unavailable; activity was not processed",
    )
