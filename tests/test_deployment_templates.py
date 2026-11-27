"""Customer-neutral industry templates create only unassigned local department groups."""

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db


def test_industry_template_catalog_is_admin_only_and_supports_multiple_sectors(client):
    assert client.get("/api/admin/deployment/templates").status_code == 401

    login(client, ADMIN_EMAIL)
    response = client.get("/api/admin/deployment/templates")
    assert response.status_code == 200
    templates = {item["template_id"]: item for item in response.json()}
    assert {"general", "consulting", "real_estate", "healthcare", "education", "manufacturing", "finance"} <= set(templates)
    # Sectors beyond the original seven share the same engine; no industry is hard-coded elsewhere.
    assert {"retail", "logistics", "legal", "government", "ngo", "hospitality", "construction", "banking"} <= set(templates)
    assert len(templates) >= 25
    assert templates["healthcare"]["version"] == "1.1.0"
    assert all(item["default_roles"] for item in templates.values())
    assert templates["finance"]["default_departments"]
    assert all(item["knowledge_areas"] for item in templates.values())


def test_template_application_is_idempotent_and_never_assigns_people_or_documents(admin_client):
    template = next(
        item for item in admin_client.get("/api/admin/deployment/templates").json()
        if item["template_id"] == "manufacturing"
    )

    first = admin_client.post(f"/api/admin/deployment/templates/{template['template_id']}/apply")
    assert first.status_code == 200, first.text
    applied = first.json()
    assert applied["version"] == template["version"]
    assert set(applied["created_departments"]) == set(template["default_departments"])
    assert applied["existing_departments"] == []
    assert set(applied["created_roles"]) == set(template["default_roles"])  # ready-made custom roles, unassigned

    second = admin_client.post(f"/api/admin/deployment/templates/{template['template_id']}/apply")
    assert second.status_code == 200, second.text
    assert second.json()["created_departments"] == []
    assert set(second.json()["existing_departments"]) == set(template["default_departments"])
    assert second.json()["created_roles"] == [] and set(second.json()["existing_roles"]) == set(template["default_roles"])
    assert admin_client.get("/api/admin/role-template-assignments").json()["assignments"] == []

    groups = admin_client.get("/api/admin/access/groups").json()["groups"]
    by_name = {group["name"]: group for group in groups}
    for name in template["default_departments"]:
        assert by_name[name]["kind"] == "department"
        assert by_name[name]["source"] == "local"
        assert by_name[name]["member_count"] == 0

    audit_count = run_db(
        lambda conn: conn.scalar(
            text("SELECT count(*) FROM audit_logs WHERE action = 'deployment.industry_template_applied' AND target = :target"),
            {"target": template["template_id"]},
        )
    )
    assert audit_count == 2


def test_profile_persists_template_version_and_rejects_unknown_catalog_version(admin_client, client):
    payload = {
        "company_name": "Synthetic Example Co",
        "industry": "Manufacturing operations",
        "brand_name": "Example Knowledge",
        "primary_color": "#245A72",
        "setup_complete": True,
        "template_id": "manufacturing",
        "template_version": "1.0.0",
    }
    saved = admin_client.put("/api/admin/deployment/company", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["template_id"] == "manufacturing"
    assert saved.json()["template_version"] == "1.0.0"
    assert client.get("/api/deployment/profile").json()["industry"] == "Manufacturing operations"

    invalid = admin_client.put(
        "/api/admin/deployment/company",
        json={**payload, "template_version": "99.0.0"},
    )
    assert invalid.status_code == 422
    assert client.get("/api/deployment/profile").json()["template_version"] == "1.0.0"


def test_only_admin_can_apply_industry_templates(client):
    login(client, ADMIN_EMAIL)
    templates = client.get("/api/admin/deployment/templates").json()
    assert templates
    login(client, USER_EMAIL)
    denied = client.post("/api/admin/deployment/templates/general/apply")
    assert denied.status_code == 403


def test_every_library_role_passes_role_validation_and_profiles_keep_older_template_versions(admin_client):
    from app.schemas.roles import CreateRoleTemplateRequest
    from app.services.deployment_templates import validate_profile_template
    from app.services.role_library import ROLE_LIBRARY

    for role in ROLE_LIBRARY.values():
        CreateRoleTemplateRequest.model_validate(role)  # same validation as the Admin API
    assert {row["key"] for row in admin_client.get("/api/admin/role-library").json()["roles"]} == set(ROLE_LIBRARY)
    validate_profile_template("consulting", "1.0.0")  # profiles saved before roles were added remain valid
