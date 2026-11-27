"""Collaboration platforms service: Slack & Microsoft Teams bot integrations."""

import hashlib
import hmac
import re
import time
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AuthenticationRequiredError, PermissionDeniedError
from app.db.models import User
from app.rag.access import readable_by

_SLACK_TIMESTAMP = re.compile(r"[0-9]{1,16}\Z")
_SLACK_SIGNATURE = re.compile(r"v0=[0-9a-f]{64}\Z")


def verify_slack_signature(
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str,
) -> bool:
    """Verify Slack X-Slack-Signature using HMAC-SHA256 and a 5-minute timestamp freshness window."""
    if (
        not timestamp
        or not signature
        or not signing_secret
        or not _SLACK_TIMESTAMP.fullmatch(timestamp)
        or not _SLACK_SIGNATURE.fullmatch(signature)
    ):
        return False
    req_time = int(timestamp)

    # Guard against replay attacks older than 5 minutes
    if abs(time.time() - req_time) > 300:
        return False

    sig_basestring = f"v0:{timestamp}:".encode("ascii") + body
    computed = "v0=" + hmac.new(
        signing_secret.encode("ascii"),
        sig_basestring,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, signature)


def slack_search_unavailable_response() -> dict[str, str]:
    """Fail closed while Slack identity mapping and permission-scoped retrieval are not wired."""
    return {
        "response_type": "ephemeral",
        "text": "Slack knowledge search is not connected to authenticated, permission-scoped retrieval. No sources were searched.",
    }


def format_slack_rag_response(
    answer: str,
    citations: list[dict[str, Any]],
    insufficient_evidence: bool = False,
) -> dict[str, Any]:
    """Format answer and source citations into Slack Block Kit blocks."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{'Insufficient Evidence' if insufficient_evidence else 'InfraSpine AI Answer'}*\n\n{answer}",
            },
        }
    ]

    if citations and not insufficient_evidence:
        source_texts = []
        for i, c in enumerate(citations, 1):
            fn = c.get("filename", "Document")
            pg = c.get("page_number")
            loc = f" (p. {pg})" if pg else ""
            source_texts.append(f"*[S{i}]* {fn}{loc}")

        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "📌 *Sources:* " + " | ".join(source_texts),
                    }
                ],
            }
        )

    return {
        "response_type": "in_channel",
        "blocks": blocks,
    }


def format_teams_adaptive_card(
    answer: str,
    citations: list[dict[str, Any]],
    insufficient_evidence: bool = False,
) -> dict[str, Any]:
    """Format answer and source citations into Microsoft Teams Adaptive Card 1.5 schema."""
    body_elements: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "size": "Medium",
            "weight": "Bolder",
            "text": "InfraSpine AI" if not insufficient_evidence else "InfraSpine AI — Notice",
            "color": "Default" if not insufficient_evidence else "Attention",
        },
        {
            "type": "TextBlock",
            "text": answer,
            "wrap": True,
        },
    ]

    actions: list[dict[str, Any]] = []

    if citations and not insufficient_evidence:
        fact_items = []
        for i, c in enumerate(citations, 1):
            fn = c.get("filename", "Document")
            pg = c.get("page_number")
            loc = f"Page {pg}" if pg else "Document"
            fact_items.append({"title": f"[S{i}] {fn}", "value": loc})

        body_elements.append(
            {
                "type": "FactSet",
                "facts": fact_items,
            }
        )

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.5",
                    "body": body_elements,
                    "actions": actions,
                },
            }
        ],
    }


async def resolve_collaborator_user(
    db: AsyncSession,
    *,
    email: str | None = None,
    external_id: str | None = None,
) -> User | None:
    """Find the user by email or external ID (e.g. Azure AD Object ID or Slack email)."""
    conditions = []
    if email:
        conditions.append(func.lower(User.email) == email.strip().lower())
    if external_id:
        conditions.append(User.external_id == external_id.strip())

    if not conditions:
        return None

    stmt = select(User).where(or_(*conditions), User.is_active.is_(True))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()
