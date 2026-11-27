"""Gap 5 (step E): the workflow engine worker is on by default.

The kill switch is WORKFLOW_ENGINE_WORKER_ENABLED=false. Unset means on,
except APP_ENV=testing (tests must not drain work as a side effect of
starting the app). Both deployment surfaces must agree: the PC wrapper
(run-worker.ps1, which starts `python -m app.worker`) and the Docker stacks
(compose.yaml / compose.onprem.yaml, whose worker service runs the same
command) must start the engine unless the kill switch is set.

These are config/deployment checks only; they do not need a database.
"""

import base64
from pathlib import Path

import pytest

from app.core.config import Settings

KEY = b"k" * 32
ROOT = Path(__file__).resolve().parent.parent


def _settings(monkeypatch, **overrides):
    # Production requires secure cookies; the test harness runs with
    # SESSION_COOKIE_SECURE=false, so pin it on here (unrelated to the
    # worker flag). Callers that need the env flag unset must delenv it
    # themselves before calling this helper.
    values = {
        "database_url": "postgresql+asyncpg://u@localhost/db",
        "master_encryption_key": base64.b64encode(KEY).decode(),
        "session_cookie_secure": True,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def _unset_flag(monkeypatch):
    monkeypatch.delenv("WORKFLOW_ENGINE_WORKER_ENABLED", raising=False)


@pytest.mark.parametrize("app_env", ["development", "staging", "production"])
def test_worker_on_by_default_outside_testing(monkeypatch, app_env):
    _unset_flag(monkeypatch)
    settings = _settings(monkeypatch, app_env=app_env)
    assert settings.workflow_engine_worker_enabled is None
    assert settings.workflow_engine_on is True


def test_worker_off_by_default_in_testing(monkeypatch):
    _unset_flag(monkeypatch)
    settings = _settings(monkeypatch, app_env="testing")
    assert settings.workflow_engine_worker_enabled is None
    assert settings.workflow_engine_on is False


@pytest.mark.parametrize("raw", ["false", "False", "FALSE", "0"])
def test_kill_switch_false_turns_worker_off(monkeypatch, raw):
    monkeypatch.setenv("WORKFLOW_ENGINE_WORKER_ENABLED", raw)
    settings = _settings(monkeypatch, app_env="production")
    assert settings.workflow_engine_worker_enabled is False
    assert settings.workflow_engine_on is False


@pytest.mark.parametrize("raw", ["true", "True", "TRUE", "1"])
def test_explicit_true_keeps_worker_on(monkeypatch, raw):
    monkeypatch.setenv("WORKFLOW_ENGINE_WORKER_ENABLED", raw)
    settings = _settings(monkeypatch, app_env="testing")
    assert settings.workflow_engine_worker_enabled is True
    assert settings.workflow_engine_on is True


def test_explicit_field_value_is_the_kill_switch(monkeypatch):
    assert _settings(monkeypatch, app_env="production", workflow_engine_worker_enabled=False).workflow_engine_on is False
    assert _settings(monkeypatch, app_env="testing", workflow_engine_worker_enabled=True).workflow_engine_on is True


def _active_env_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_env_example_enables_worker():
    assert "WORKFLOW_ENGINE_WORKER_ENABLED=true" in _active_env_lines(ROOT / ".env.example")


def test_docker_env_example_enables_worker():
    assert "WORKFLOW_ENGINE_WORKER_ENABLED=true" in _active_env_lines(ROOT / ".env.docker.example")


@pytest.mark.parametrize("compose_file", ["compose.yaml", "compose.onprem.yaml"])
def test_compose_stacks_enable_worker_by_default(compose_file):
    text = (ROOT / compose_file).read_text(encoding="utf-8")
    assert "WORKFLOW_ENGINE_WORKER_ENABLED: ${WORKFLOW_ENGINE_WORKER_ENABLED:-true}" in text


def test_run_worker_ps1_starts_engine_by_default_and_honours_kill_switch():
    text = (ROOT / "scripts" / "ops" / "windows" / "run-worker.ps1").read_text(encoding="utf-8")
    assert "app.worker" in text
    assert "WORKFLOW_ENGINE_WORKER_ENABLED=false" in text
    assert "ON (default" in text
    assert "OFF (WORKFLOW_ENGINE_WORKER_ENABLED=false)" in text


def test_docker_worker_starts_app_worker():
    text = (ROOT / "docker" / "api-entrypoint.sh").read_text(encoding="utf-8")
    assert "python -m app.worker" in text
