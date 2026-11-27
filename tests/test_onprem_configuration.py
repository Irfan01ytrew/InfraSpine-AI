"""Safety and reproducibility checks for the customer Compose template."""

from pathlib import Path

import yaml


REPOSITORY = Path(__file__).resolve().parents[1]


def test_onprem_models_are_local_only_by_default_and_images_are_digest_pinned():
    compose = yaml.safe_load((REPOSITORY / "compose.onprem.yaml").read_text(encoding="utf-8"))
    api_environment = compose["services"]["api"]["environment"]
    worker_environment = compose["services"]["worker"]["environment"]

    expected_worker_environment = {
        **api_environment,
        "RAG_DATABASE_ANALYSIS_URL": "",
        "RAG_DATABASE_ANALYSIS_CATALOG": "",
        "RAG_DATABASE_ANALYSIS_DEPARTMENT_GRANTS": "",
        "RAG_DATABASE_ANALYSIS_SQLITE_PATH": "",
    }
    assert worker_environment == expected_worker_environment
    assert api_environment["RAG_EMBEDDING_ALLOW_DOWNLOAD"] == "${RAG_EMBEDDING_ALLOW_DOWNLOAD:-false}"
    assert api_environment["HF_HUB_OFFLINE"] == "${HF_HUB_OFFLINE:-true}"
    assert api_environment["TRANSFORMERS_OFFLINE"] == "${TRANSFORMERS_OFFLINE:-true}"
    assert api_environment["HF_HOME"] == "/var/lib/infraspine/data/models/huggingface"

    postgres = compose["services"]["postgres"]["image"]
    qdrant = compose["services"]["qdrant"]["image"]
    assert postgres.startswith("postgres:18.6-alpine@sha256:")
    assert qdrant.startswith("qdrant/qdrant:v1.19.0@sha256:")
    assert len(postgres.rsplit(":", 1)[1]) == 64
    assert len(qdrant.rsplit(":", 1)[1]) == 64


def test_application_build_bases_are_digest_pinned():
    dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM node:22-bookworm-slim@sha256:" in dockerfile
    assert "FROM python:3.12-slim@sha256:" in dockerfile


def test_onprem_service_logs_are_bounded():
    compose = yaml.safe_load((REPOSITORY / "compose.onprem.yaml").read_text(encoding="utf-8"))
    expected = {"driver": "local", "options": {"max-size": "10m", "max-file": "3"}}

    assert compose["x-default-logging"] == expected
    assert all(service["logging"] == expected for service in compose["services"].values())


def test_worker_healthcheck_allows_measured_cold_start_and_model_diagnostic():
    compose = yaml.safe_load((REPOSITORY / "compose.onprem.yaml").read_text(encoding="utf-8"))
    healthcheck = compose["services"]["worker"]["healthcheck"]

    assert healthcheck["test"] == ["CMD", "python", "-m", "app.worker", "--check"]
    assert healthcheck["timeout"] == "120s"
    assert healthcheck["start_period"] == "180s"
