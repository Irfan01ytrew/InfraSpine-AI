import hashlib
import uuid
from decimal import Decimal

from sqlalchemy import text

from tests.conftest import ADMIN_EMAIL, USER_EMAIL, USER2_EMAIL, login, run_db


def _seed_source(filename: str, content: str, *, visibility: str = "shared", group_id: str | None = None):
    """Seed the minimal ready source state; ingestion is covered by its own worker tests."""
    async def seed(connection):
        owner_id = (
            await connection.execute(text("SELECT id FROM users WHERE email=:email"), {"email": ADMIN_EMAIL})
        ).scalar_one()
        document_uuid = uuid.uuid4()
        document_id = (
            await connection.execute(
                text(
                    "INSERT INTO documents (id, owner_id, visibility, filename, file_format, storage_key, sha256, "
                    "size_bytes, version, status, chunk_count, indexed_version, indexed_filename, page_count) "
                    "VALUES (:document_id, :owner_id, :visibility, :filename, 'pdf', :storage_key, :sha256, "
                    "128, 1, 'ready', "
                    "1, 1, :filename, 1) RETURNING id"
                ),
                {
                    "document_id": document_uuid,
                    "owner_id": owner_id,
                    "visibility": visibility,
                    "filename": filename,
                    "storage_key": uuid.uuid4().hex,
                    "sha256": hashlib.sha256(filename.encode()).hexdigest(),
                },
            )
        ).scalar_one()
        chunk = (
            await connection.execute(
                text(
                    "INSERT INTO document_chunks (document_id, chunk_index, page_number, content, embedding) "
                    "VALUES (:document_id, 0, 1, :content, :embedding) RETURNING id, content"
                ),
                {"document_id": document_id, "content": content, "embedding": bytes(4)},
            )
        ).one()
        if group_id is not None:
            await connection.execute(
                text(
                    "INSERT INTO resource_acl (resource_type, document_id, principal_group_id, origin, created_by) "
                    "VALUES ('document', :document_id, :group_id, 'manual', :owner_id)"
                ),
                {"document_id": document_id, "group_id": group_id, "owner_id": owner_id},
            )
        return {"id": str(document_id)}, chunk

    return run_db(seed)


def _ready_shared_source(client, filename: str):
    login(client, ADMIN_EMAIL)
    return _seed_source(
        filename,
        "2023 revenue was 100.00 units. 2024 revenue was 150.00 units.",
    )


def _payload(document_id: str, excerpt: str, *, title: str = "Revenue what-if") -> dict:
    return {
        "title": title,
        "metric": "Revenue",
        "unit": "USD",
        "period_unit": "year",
        "observations": [
            {"period": "2023", "value": "100.00", "document_id": document_id, "excerpt": excerpt},
            {"period": "2024", "value": "150.00", "document_id": document_id, "excerpt": excerpt},
        ],
        "growth_rate_pct": "10",
        "additive_adjustment": "2",
        "horizon_periods": 2,
        "assumption_notes": "Synthetic scenario assumption for test coverage.",
    }


def test_forecast_math_is_persisted_reproducibly_and_private_to_owner(client):
    source, chunk = _ready_shared_source(client, "forecast-source.pdf")
    payload = _payload(str(source["id"]), chunk.content)
    login(client, USER_EMAIL)

    created = client.post("/api/forecasts", json=payload)
    assert created.status_code == 201, created.text
    scenario = created.json()
    assert scenario["model_version"] == "linear-growth-v1"
    assert scenario["input_digest"] and len(scenario["input_digest"]) == 64
    assert [item["period"] for item in scenario["projections"]] == ["2025", "2026"]
    assert [Decimal(str(item["value"])) for item in scenario["projections"]] == [Decimal("167.000000"), Decimal("185.700000")]
    assert scenario["observations"][0]["citation"]["document_id"] == source["id"]
    assert scenario["observations"][0]["citation"]["page_number"] == 1
    assert scenario["observations"][0]["citation"]["excerpt"] == chunk.content
    assert scenario["assumptions"]["growth_rate_pct"] == "10"
    assert scenario["assumptions"]["additive_adjustment"] == "2"

    repeated = client.post("/api/forecasts", json=payload)
    assert repeated.status_code == 201 and repeated.json()["input_digest"] == scenario["input_digest"]
    assert client.get("/api/forecasts").json()["items"][0]["observation_count"] == 2
    assert client.get(f"/api/forecasts/{scenario['id']}").json()["id"] == scenario["id"]

    client.cookies.clear()
    login(client, USER2_EMAIL)
    assert client.get("/api/forecasts").json()["items"] == []
    assert client.get(f"/api/forecasts/{scenario['id']}").status_code == 404
    async def read_audit_rows(connection):
        result = await connection.execute(
            text("SELECT details FROM audit_logs WHERE action='forecast.scenario_created'")
        )
        return result.scalars().all()

    audit_rows = run_db(read_audit_rows)
    assert len(audit_rows) == 2
    assert all("excerpt" not in str(details) and "150.00" not in str(details) for details in audit_rows)


def test_forecast_rejects_unverifiable_values_and_nonverbatim_quotes(client):
    source, chunk = _ready_shared_source(client, "forecast-evidence.pdf")
    login(client, USER_EMAIL)
    valid = _payload(str(source["id"]), chunk.content)

    bad_value = {**valid, "observations": [dict(row) for row in valid["observations"]]}
    bad_value["observations"][0]["value"] = "999"
    assert client.post("/api/forecasts", json=bad_value).status_code == 422

    bad_quote = {**valid, "observations": [dict(row) for row in valid["observations"]]}
    bad_quote["observations"][0]["excerpt"] = "prefix " + chunk.content
    assert client.post("/api/forecasts", json=bad_quote).status_code == 422

    bad_period = {**valid, "observations": [dict(row) for row in valid["observations"]]}
    bad_period["observations"][0]["period"] = "2022"
    assert client.post("/api/forecasts", json=bad_period).status_code == 422

    bad_assumption = {**valid, "growth_rate_pct": "501"}
    assert client.post("/api/forecasts", json=bad_assumption).status_code == 422


def test_forecast_citation_stops_working_after_department_membership_revocation(client):
    login(client, ADMIN_EMAIL)
    group = client.post("/api/admin/access/groups", json={"name": "Forecast Department", "kind": "department"})
    assert group.status_code == 201, group.text
    group_id = group.json()["id"]
    assert client.post(f"/api/admin/access/groups/{group_id}/members", json={"email": USER_EMAIL}).status_code == 204
    document, chunk = _seed_source(
        "forecast-restricted.pdf",
        "2023 cost was 80.00 units. 2024 cost was 90.00 units.",
        visibility="restricted",
        group_id=group_id,
    )
    payload = _payload(str(document["id"]), chunk.content, title="Department cost case")
    payload["metric"] = "Cost"
    payload["observations"][0]["value"] = "80.00"
    payload["observations"][1]["value"] = "90.00"

    client.cookies.clear()
    login(client, USER_EMAIL)
    created = client.post("/api/forecasts", json=payload)
    assert created.status_code == 201, created.text
    scenario_id = created.json()["id"]

    client.cookies.clear()
    login(client, ADMIN_EMAIL)
    member = client.get(f"/api/admin/access/groups/{group_id}/members").json()["members"]
    user_id = next(item["user_id"] for item in member if item["email"] == USER_EMAIL)
    assert client.delete(f"/api/admin/access/groups/{group_id}/members/{user_id}").status_code == 204
    client.cookies.clear()
    login(client, USER_EMAIL)
    assert client.get(f"/api/forecasts/{scenario_id}").status_code == 404
    assert client.get("/api/forecasts").json()["items"] == []


def test_projection_calculation_uses_bounded_decimal_assumptions():
    from app.rag.forecast import calculate_projection

    result = calculate_projection(
        [("2024-Q3", Decimal("100")), ("2024-Q4", Decimal("200"))],
        growth_rate_pct=Decimal("5"),
        additive_adjustment=Decimal("0.25"),
        horizon_periods=2,
        period_unit="quarter",
    )
    assert result == [
        {"period": "2025-Q1", "value": "210.250000"},
        {"period": "2025-Q2", "value": "221.012500"},
    ]
