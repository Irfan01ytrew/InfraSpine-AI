from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.legacy_browser import LegacyBrowserPolicyError, allowed_origins, validate_target_url


def settings(**overrides) -> Settings:
    values = {
        "database_url": SecretStr("postgresql+asyncpg://test:test@127.0.0.1/test"),
        "master_encryption_key": SecretStr("test-key-not-used"),
        "rag_legacy_browser_enabled": True,
        "rag_legacy_browser_allowed_origins": "http://127.0.0.1:63120,https://docs.example.test",
        "rag_legacy_browser_executable_path": Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        "rag_legacy_browser_allow_private_networks": False,
    }
    values.update(overrides)
    return Settings.model_construct(**values)


def test_origins_are_exact_and_canonicalized():
    config = settings(rag_legacy_browser_allowed_origins="https://docs.example.test/, https://docs.example.test:443")
    assert allowed_origins(config) == {"https://docs.example.test"}
    assert validate_target_url("https://docs.example.test/guide", config) == "https://docs.example.test/guide"


@pytest.mark.parametrize(
    "target,code",
    [
        ("https://not-allowed.example/", "origin_not_allowed"),
        ("https://docs.example.test.evil/", "origin_not_allowed"),
        ("https" + "://" + "user:secret" + "@" + "docs.example.test/", "invalid_url"),
        ("https://docs.example.test/guide?token=secret", "invalid_url"),
        ("https://docs.example.test/guide#fragment", "invalid_url"),
        ("http://docs.example.test/", "origin_not_allowed"),
        ("file:///C:/private.txt", "invalid_url"),
        ("https://docs.example.test/\\\\evil", "invalid_url"),
    ],
)
def test_target_url_policy_rejects_unsafe_urls(target, code):
    with pytest.raises(LegacyBrowserPolicyError) as error:
        validate_target_url(target, settings())
    assert error.value.code == code


def test_private_non_loopback_requires_a_second_operator_opt_in():
    config = settings(rag_legacy_browser_allowed_origins="http://10.20.0.8")
    with pytest.raises(LegacyBrowserPolicyError, match="https_required"):
        validate_target_url("http://10.20.0.8/", config)
    config = settings(
        rag_legacy_browser_allowed_origins="http://10.20.0.8",
        rag_legacy_browser_allow_private_networks=True,
    )
    assert validate_target_url("http://10.20.0.8/", config) == "http://10.20.0.8/"


def test_browser_approval_table_is_additive_and_does_not_store_capture_text():
    from app.db.base import Base
    from app.db.models import LegacyBrowserRun

    assert LegacyBrowserRun.__tablename__ == "legacy_browser_runs"
    assert "text" not in Base.metadata.tables[LegacyBrowserRun.__tablename__].columns
    assert "content" not in Base.metadata.tables[LegacyBrowserRun.__tablename__].columns
