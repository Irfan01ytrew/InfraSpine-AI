"""SMTP safety tests that do not load the project .env or need PostgreSQL."""

import base64
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.services import personal_task_email


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://synthetic:synthetic@127.0.0.1/unused_test",
        "master_encryption_key": base64.b64encode(b"k" * 32).decode(),
        "app_env": "testing",
        "personal_task_email_enabled": True,
        "personal_task_smtp_host": "mail.synthetic.invalid",
        "personal_task_smtp_port": 587,
        "personal_task_smtp_security": "starttls",
        "personal_task_smtp_username": "synthetic-user",
        "personal_task_smtp_password": SecretStr("synthetic-password"),
        "personal_task_email_from": "reminders@synthetic.invalid",
    }
    values.update(overrides)
    return Settings(**values)


def test_deadline_email_uses_starttls_auth_stable_id_and_no_task_content(monkeypatch):
    captured = {}

    class FakeSmtp:
        def __init__(self, host, port, timeout):
            captured.update(host=host, port=port, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def ehlo(self):
            captured.setdefault("ehlo", 0)
            captured["ehlo"] += 1

        def starttls(self, *, context):
            captured["tls_context"] = context

        def login(self, username, password):
            captured["auth"] = (username, password)

        def send_message(self, message):
            captured["message"] = message

    monkeypatch.setattr(personal_task_email.smtplib, "SMTP", FakeSmtp)
    reminder_id = uuid4()
    personal_task_email.send_personal_task_reminder_email(
        _settings(), "employee@synthetic.invalid", reminder_id
    )

    message = captured["message"]
    assert captured["host"] == "mail.synthetic.invalid" and captured["port"] == 587
    assert captured["ehlo"] == 2 and captured["tls_context"] is not None
    assert captured["auth"] == ("synthetic-user", "synthetic-password")
    assert message["To"] == "employee@synthetic.invalid"
    assert message["Message-ID"] == f"<personal-task-reminder-{reminder_id}@infraspine.local>"
    assert "task details" in message.get_content()
    assert "synthetic-password" not in message.as_string()


def test_deadline_email_ssl_transport_and_disabled_delivery(monkeypatch):
    calls = []

    class FakeSmtpSsl:
        def __init__(self, host, port, timeout, context):
            calls.append((host, port, timeout, context))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def send_message(self, message):
            calls.append(message)

    monkeypatch.setattr(personal_task_email.smtplib, "SMTP_SSL", FakeSmtpSsl)
    settings = _settings(personal_task_smtp_security="ssl", personal_task_smtp_username="", personal_task_smtp_password=None)
    personal_task_email.send_personal_task_reminder_email(settings, "employee@synthetic.invalid", uuid4())
    assert len(calls) == 2 and calls[0][3] is not None

    disabled = _settings(personal_task_email_enabled=False)
    personal_task_email.send_personal_task_reminder_email(disabled, "employee@synthetic.invalid", uuid4())
    assert len(calls) == 2


def test_email_transport_requires_complete_configuration():
    with pytest.raises(ValidationError, match="PERSONAL_TASK_SMTP_HOST"):
        _settings(personal_task_smtp_host="")
    with pytest.raises(ValidationError, match="both PERSONAL_TASK_SMTP_USERNAME"):
        _settings(personal_task_smtp_username="", personal_task_smtp_password=SecretStr("orphaned"))
    with pytest.raises(ValueError, match="control characters"):
        personal_task_email.build_personal_task_reminder_message(
            "reminders@synthetic.invalid", "employee@synthetic.invalid\r\nBcc: other@invalid", uuid4()
        )
