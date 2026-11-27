"""The local-only processing switch blocks external content before model clients are built."""

import asyncio
import base64
from types import SimpleNamespace
import uuid

import pytest

from app.core.config import Settings
from app.core.errors import LLMUnavailableError
from app.providers.registry import get_provider
from app.services.audit import AuditContext
from app.services import llm_runtime
from app.services.llm_runtime import LLMRuntime, model_options
from app.services.llm import ProviderVerificationError
from app.services.provider_admin import ProviderAdminService
from app.services.processing_policy import model_allowed_for_content


def settings(*, local_only: bool, trusted: str = "") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://test:test@localhost/infraspine_test",
        master_encryption_key=base64.b64encode(b"x" * 32).decode("ascii"),
        llm_local_only=local_only,
        provider_trusted_private_origins=trusted,
    )


def config(provider: str, model: str, base_url: str | None, *, active: bool = False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        version=1,
        provider=provider,
        model=model,
        base_url=base_url,
        display_name=provider,
        settings={},
        is_active=active,
        created_at=None,
    )


def test_local_only_requires_exact_trusted_private_custom_origin():
    local = settings(local_only=True, trusted="http://localhost:11434")
    assert not model_allowed_for_content("deepseek", None, local)
    assert model_allowed_for_content("custom-ollama", "http://localhost:11434/v1", local)
    assert not model_allowed_for_content("custom-ollama", "http://localhost:11435/v1", local)
    assert not model_allowed_for_content("custom-public", "https://llm.example.com/v1", local)


def test_configured_mode_preserves_cloud_and_custom_provider_choices():
    configured = settings(local_only=False)
    assert model_allowed_for_content("deepseek", None, configured)
    assert model_allowed_for_content("custom-public", "https://llm.example.com/v1", configured)


def test_local_only_filters_model_picker_and_refuses_before_credential_decryption(monkeypatch):
    local = settings(local_only=True, trusted="http://localhost:11434")
    built_in = config("deepseek", "deepseek-flash", None, active=True)
    allowed_custom = config("custom-ollama", "qwen-local", "http://localhost:11434/v1")
    untrusted_custom = config("custom-public", "remote", "https://llm.example.com/v1")

    class Result:
        def scalars(self):
            return [built_in, allowed_custom, untrusted_custom]

    class OptionsDb:
        async def execute(self, _query):
            return Result()

    options = asyncio.run(model_options(OptionsDb(), local))
    assert [(option["provider"], option["model"]) for option in options] == [("custom-ollama", "qwen-local")]
    assert all("endpoint" not in option and "api_key" not in option for option in options)

    credential = SimpleNamespace(id=uuid.uuid4())

    class ClientResult:
        def first(self):
            return built_in, credential

    class ClientDb:
        async def execute(self, _query):
            return ClientResult()

    def forbid_decryption(*_args, **_kwargs):
        pytest.fail("A cloud credential was decrypted under local-only mode.")

    monkeypatch.setattr(llm_runtime, "decrypt_credential", forbid_decryption)
    runtime = LLMRuntime(local, cipher=None)
    with pytest.raises(LLMUnavailableError, match="local-only"):
        asyncio.run(runtime.get_client(ClientDb()))


def test_local_only_blocks_environment_evaluation_before_building_client(monkeypatch):
    local = settings(local_only=True)
    monkeypatch.setenv("EVAL_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("EVAL_LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("EVAL_LLM_API_KEY", "synthetic-never-real")
    monkeypatch.delenv("EVAL_LLM_BASE_URL", raising=False)

    def forbid_build(*_args, **_kwargs):
        pytest.fail("An environment-configured cloud provider was built under local-only mode.")

    monkeypatch.setattr(llm_runtime, "build_client", forbid_build)
    from app.rag.evaluation import gate

    with pytest.raises(gate.GateError, match="local-only.*not scored"):
        asyncio.run(gate._llm(local, "env"))


def test_local_only_blocks_admin_provider_verification_before_network(monkeypatch):
    local = settings(local_only=True)
    service = ProviderAdminService(None, local, None, AuditContext(None, "admin@example.invalid", None, "test"))
    definition = get_provider("deepseek")
    assert definition is not None

    def forbid_build(*_args, **_kwargs):
        pytest.fail("Admin provider verification attempted outbound access under local-only mode.")

    monkeypatch.setattr("app.services.provider_admin.build_client", forbid_build)
    with pytest.raises(ProviderVerificationError) as rejected:
        asyncio.run(service._verify(definition, "synthetic-never-real", definition.default_model, {}))
    assert rejected.value.reason == "unsafe_endpoint"
    assert "local-only" in (rejected.value.detail or "")
