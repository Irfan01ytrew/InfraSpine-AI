import pytest

from tests.conftest import USER_EMAIL, login


@pytest.mark.parametrize(
    "format,content_type",
    [
        ("csv", "text/csv; charset=utf-8"),
        ("pdf", "application/pdf"),
        ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ],
)
def test_admin_can_download_usage_report_in_real_office_formats(admin_client, format, content_type):
    response = admin_client.get(f"/api/admin/reports/usage/export?days=30&format={format}")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == content_type
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].endswith(f".{format}\"")
    assert response.content


def test_report_export_is_admin_only(client):
    assert client.get("/api/admin/reports/usage/export?format=pdf").status_code == 401
    login(client, USER_EMAIL)
    assert client.get("/api/admin/reports/usage/export?format=pdf").status_code == 403


def test_report_export_validates_parameters_for_admin(admin_client):
    assert admin_client.get("/api/admin/reports/usage/export?format=exe").status_code == 422
    assert admin_client.get("/api/admin/reports/usage/export?days=0&format=pdf").status_code == 422
