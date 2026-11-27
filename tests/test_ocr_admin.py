from app.core.config import get_settings
from app.rag.ocr_health import ocr_engine_status, run_ocr_health_check
from tests.conftest import USER_EMAIL, login


def test_ocr_status_reports_the_local_rapidocr_configuration(admin_client):
    response = admin_client.get("/api/admin/ocr-engine")

    assert response.status_code == 200
    body = response.json()
    assert body["engine_name"] == "RapidOCR (ONNX Runtime)"
    assert body["enabled"] is True
    assert body["runtime_available"] is True
    assert body["status"] == "ready"
    assert body["docling_available"] is True
    assert body["rapidocr_available"] is True
    assert body["pytorch_available"] is True
    assert (body["backend"], body["device"]) == ("onnxruntime", "cpu")
    assert body["full_page_ocr_enabled"] is True
    assert body["orientation_classifier_enabled"] is False
    assert body["configured_languages"] == ["ch (RapidOCR default)"]
    assert body["capabilities"]


def test_ocr_status_and_health_require_admin(client):
    assert client.get("/api/admin/ocr-engine").status_code == 401
    assert client.post("/api/admin/ocr-engine/test").status_code == 401

    login(client, USER_EMAIL)
    assert client.get("/api/admin/ocr-engine").status_code == 403
    assert client.post("/api/admin/ocr-engine/test").status_code == 403


def test_ocr_health_test_recognizes_a_local_scanned_pdf(admin_client):
    response = admin_client.post("/api/admin/ocr-engine/test")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    assert body["engine_name"] == "RapidOCR (ONNX Runtime)"
    assert body["parser_used"] == "RapidOCR"
    assert body["latency_ms"] > 0
    assert body["page_count"] == 1
    assert body["chunk_count"] >= 1
    assert body["recognized_characters"] > 0
    assert body["error"] is None


def test_ocr_health_failure_is_safe_and_returns_a_result(admin_client, monkeypatch):
    from app.rag.isolation import ProcessingCrashed

    def fail_before_spawn(*_args, **_kwargs):
        raise ProcessingCrashed(-1)

    monkeypatch.setattr("app.rag.isolation.run_isolated", fail_before_spawn)
    response = admin_client.post("/api/admin/ocr-engine/test")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["parser_used"] == "Not run"
    assert body["error"] == "The isolated OCR process exited before returning a result."
    assert "fixture.pdf" not in response.text


def test_ocr_status_and_health_report_disabled_without_starting_parser(monkeypatch):
    def should_not_run(*_args, **_kwargs):
        raise AssertionError("The disabled OCR runtime must not start a parser process.")

    monkeypatch.setattr("app.rag.isolation.run_isolated", should_not_run)
    settings = get_settings().model_copy(update={"rag_ocr_enabled": False})

    status = ocr_engine_status(settings)
    result = run_ocr_health_check(settings)

    assert status.enabled is False and status.status == "disabled"
    assert result["ok"] is False
    assert result["parser_used"] == "Not run"
    assert result["error"] == "OCR is disabled by RAG_OCR_ENABLED."


def test_ocr_status_and_health_report_missing_runtime_without_starting_parser(monkeypatch):
    def only_rapidocr_missing(name: str) -> bool:
        return name != "rapidocr"

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("An unavailable OCR runtime must not start a parser process.")

    monkeypatch.setattr("app.rag.ocr_health._module_available", only_rapidocr_missing)
    monkeypatch.setattr("app.rag.isolation.run_isolated", should_not_run)
    settings = get_settings()

    status = ocr_engine_status(settings)
    result = run_ocr_health_check(settings)

    assert status.runtime_available is False and status.status == "unavailable"
    assert result["ok"] is False
    assert result["parser_used"] == "Not run"
    assert result["error"] == "RapidOCR, ONNX Runtime, or PDFium is unavailable."

