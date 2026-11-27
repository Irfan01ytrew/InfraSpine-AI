"""Tests for Slack and Microsoft Teams Bot integrations."""

import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from app.main import app
from app.services.collaboration import (
    format_slack_rag_response,
    format_teams_adaptive_card,
    slack_search_unavailable_response,
    verify_slack_signature,
)


def _generate_slack_sig(body: bytes, timestamp: int, secret: str) -> str:
    sig_basestring = f"v0:{timestamp}:".encode("ascii") + body
    return "v0=" + hmac.new(secret.encode("ascii"), sig_basestring, hashlib.sha256).hexdigest()


def test_slack_signature_verification():
    secret = "test_signing_secret_123"
    body = b'{"event":{"type":"app_mention","text":"hello"}}'
    now = int(time.time())

    # Valid
    sig = _generate_slack_sig(body, now, secret)
    assert verify_slack_signature(body, str(now), sig, secret) is True

    # Tampered body
    assert verify_slack_signature(b"tampered", str(now), sig, secret) is False

    # Expired timestamp (>300s)
    old = now - 305
    old_sig = _generate_slack_sig(body, old, secret)
    assert verify_slack_signature(body, str(old), old_sig, secret) is False

    arabic_digits = str(now).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
    assert verify_slack_signature(body, arabic_digits, "v0=" + "0" * 64, secret) is False
    assert verify_slack_signature(body, str(now), "v0=" + "a" * 63 + "é", secret) is False
    assert verify_slack_signature(body, "9" * 100, "v0=" + "0" * 64, secret) is False


def test_slack_and_teams_formatting():
    # Slack formatting
    citations = [{"filename": "Employee_Handbook.pdf", "page_number": 4}]
    slack_resp = format_slack_rag_response("You get 25 days PTO.", citations)
    assert slack_resp["response_type"] == "in_channel"
    assert len(slack_resp["blocks"]) == 2
    assert "Employee_Handbook.pdf" in json.dumps(slack_resp["blocks"])

    # Teams formatting
    teams_resp = format_teams_adaptive_card("You get 25 days PTO.", citations)
    card = teams_resp["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == "1.5"
    assert len(card["body"]) >= 2

    unavailable = slack_search_unavailable_response()
    assert unavailable["response_type"] == "ephemeral"
    assert "No sources were searched" in unavailable["text"]
    assert "citations" not in unavailable


def test_collaboration_api_routes():
    client = TestClient(app)

    # 1. Integration status
    resp = client.get("/api/integrations/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "slack" in data and "teams" in data

    # 2. Slack URL verification challenge
    resp = client.post(
        "/api/integrations/slack/events",
        json={"type": "url_verification", "challenge": "challenge_token_abc"},
    )
    assert resp.status_code == 503

    # 3. Slack slash command
    resp = client.post(
        "/api/integrations/slack/command",
        data={"command": "/ask", "text": "What is company policy?", "user_name": "developer"},
    )
    assert resp.status_code == 503

    # 4. Teams messages
    resp = client.post(
        "/api/integrations/teams/messages",
        json={
            "type": "message",
            "text": "What is the policy?",
            "from": {"id": "teams_id_1", "name": "Sarah"},
        },
    )
    assert resp.status_code == 503
