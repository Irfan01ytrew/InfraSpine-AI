import pytest

from app.core.config import get_settings
from app.rag.parsers.base import ParseError
from app.rag.parsers.health import run_parser_health_check
from tests.conftest import USER_EMAIL, login


def test_parser_info_reports_supported_formats_and_selection(admin_client):
    response = admin_client.get("/api/admin/document-parsers")

    assert response.status_code == 200
    body = response.json()
    parsers = {item["file_format"]: item for item in body["formats"]}
    assert set(parsers) == {"pdf", "docx", "xlsx", "pptx", "txt", "csv"}
    assert {item["extension"] for item in parsers.values()} == {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".csv"}
    assert parsers["pdf"]["fallback_parser"] == "Built-in PDF parser (pypdf)"
    assert parsers["docx"]["fallback_parser"] == "Built-in Word parser (python-docx)"
    assert parsers["pptx"]["fallback_parser"] == "Built-in PowerPoint parser (python-pptx)"
    assert parsers["xlsx"]["fallback_parser"] is None
    assert all(isinstance(item["active_available"], bool) for item in parsers.values())


def test_parser_info_and_health_tests_are_admin_only(client):
    assert client.get("/api/admin/document-parsers").status_code == 401
    assert client.post("/api/admin/document-parsers/txt/test").status_code == 401

    login(client, USER_EMAIL)
    assert client.get("/api/admin/document-parsers").status_code == 403
    assert client.post("/api/admin/document-parsers/txt/test").status_code == 403


def test_txt_parser_health_check_uses_the_isolated_parser(admin_client):
    response = admin_client.post("/api/admin/document-parsers/txt/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["file_format"] == "txt"
    assert body["parser_used"] == "InfraSpine text parser"
    assert body["latency_ms"] >= 0
    assert body["chunk_count"] == 1
    assert body["error"] is None


def test_docx_parser_health_check_reports_the_selected_docling_or_fallback_parser(admin_client):
    info = admin_client.get("/api/admin/document-parsers").json()
    response = admin_client.post("/api/admin/document-parsers/docx/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    if info["docling_enabled"]:
        assert body["parser_used"] in {"Docling", "Built-in Word parser (python-docx)"}
    else:
        assert body["parser_used"] == "Built-in Word parser (python-docx)"
    assert body["chunk_count"] >= 1


def test_parser_health_failure_returns_safe_actionable_error(admin_client, monkeypatch):
    def fail_before_spawn(*_args, **_kwargs):
        raise ParseError("processing_timeout", "Synthetic parser timeout fixture.")

    monkeypatch.setattr("app.rag.isolation.run_isolated", fail_before_spawn)

    response = admin_client.post("/api/admin/document-parsers/txt/test")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["parser_used"] == "Not reached"
    assert body["error"] == "Synthetic parser timeout fixture."


@pytest.mark.parametrize("file_format", ["pdf", "docx", "xlsx", "pptx", "txt", "csv"])
def test_small_builtin_health_fixtures_parse_in_isolation(file_format):
    settings = get_settings().model_copy(update={"rag_use_docling": False})

    result = run_parser_health_check(file_format, settings)

    assert result["ok"] is True, result
    assert result["parser_used"] != "Not reached"
    assert result["chunk_count"] >= 1
