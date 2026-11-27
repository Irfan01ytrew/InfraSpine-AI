import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models import AccessGroup, AccessGroupMember
from app.services.operational_health import (
    calculate_token_cost,
    get_department_quotas,
    get_operational_dashboard,
    get_provider_costs,
    set_department_quota,
)


def test_calculate_token_cost():
    # Gemini 1.5 Pro: prompt $0.00125/1k, completion $0.00500/1k
    cost = calculate_token_cost("gemini", "gemini-1.5-pro", 10_000, 2_000)
    assert cost == round(10 * 0.00125 + 2 * 0.00500, 4)

    # GPT-4o: prompt $0.00250/1k, completion $0.01000/1k
    cost_gpt = calculate_token_cost("openai", "gpt-4o", 20_000, 5_000)
    assert cost_gpt == round(20 * 0.00250 + 5 * 0.01000, 4)

    # Zero / None handling
    assert calculate_token_cost(None, None, 1000, 1000) == 0.0
    assert calculate_token_cost("gemini", None, 0, 0) == 0.0


def test_department_quotas_and_alert_generation():
    async def _run():
        dept_id = uuid.uuid4()
        user_id = uuid.uuid4()

        mock_dept = MagicMock(spec=AccessGroup)
        mock_dept.id = dept_id
        mock_dept.name = "Engineering"
        mock_dept.kind = "department"

        # Configure custom quota: 100,000 tokens, 80% threshold
        set_department_quota(dept_id, monthly_token_quota=100_000, alert_threshold_percent=80.0)

        # Mock DB session returns
        db = AsyncMock()

        # 1. Dept list
        mock_dept_result = MagicMock()
        mock_dept_result.scalars.return_value.all.return_value = [mock_dept]

        # 2. User traces: user consumed 85,000 prompt + 5,000 completion = 90,000 tokens (90%)
        mock_traces_result = MagicMock()
        mock_traces_result.all.return_value = [(user_id, 10, 85_000, 5_000)]

        # 3. Memberships
        mock_members_result = MagicMock()
        mock_members_result.all.return_value = [(dept_id, user_id)]

        db.execute.side_effect = [mock_dept_result, mock_traces_result, mock_members_result]

        quotas, alerts = await get_department_quotas(db, days=30)

        assert len(quotas) == 1
        assert quotas[0].department_name == "Engineering"
        assert quotas[0].token_count == 90_000
        assert quotas[0].utilization_percent == 90.0
        assert quotas[0].is_alert_triggered is True
        assert quotas[0].alert_severity == "warning"

        assert len(alerts) == 1
        assert alerts[0].severity == "warning"
        assert "reached 90.0% of monthly token quota" in alerts[0].message

    asyncio.run(_run())


def test_department_quotas_critical_overage():
    async def _run():
        dept_id = uuid.uuid4()
        user_id = uuid.uuid4()

        mock_dept = MagicMock(spec=AccessGroup)
        mock_dept.id = dept_id
        mock_dept.name = "Finance"
        mock_dept.kind = "department"

        # Configure custom quota: 50,000 tokens, 75% threshold
        set_department_quota(dept_id, monthly_token_quota=50_000, alert_threshold_percent=75.0)

        db = AsyncMock()
        mock_dept_result = MagicMock()
        mock_dept_result.scalars.return_value.all.return_value = [mock_dept]

        # User consumed 60,000 tokens (120% utilization)
        mock_traces_result = MagicMock()
        mock_traces_result.all.return_value = [(user_id, 25, 40_000, 20_000)]

        mock_members_result = MagicMock()
        mock_members_result.all.return_value = [(dept_id, user_id)]

        db.execute.side_effect = [mock_dept_result, mock_traces_result, mock_members_result]

        quotas, alerts = await get_department_quotas(db, days=30)

        assert len(quotas) == 1
        assert quotas[0].utilization_percent == 120.0
        assert quotas[0].alert_severity == "critical"
        assert len(alerts) == 1
        assert alerts[0].severity == "critical"
        assert "exceeded monthly token quota" in alerts[0].message

    asyncio.run(_run())


def test_admin_operational_health_endpoints(admin_client):
    dept_id = str(uuid.uuid4())

    # 1. Update department quota
    put_res = admin_client.put(
        f"/api/admin/reports/quotas/{dept_id}",
        json={"monthly_token_quota": 500000, "alert_threshold_percent": 85.0},
    )
    assert put_res.status_code == 200, put_res.text
    assert put_res.json()["status"] == "ok"

    # 2. Get operational health dashboard
    get_res = admin_client.get("/api/admin/reports/operational-health?days=30")
    assert get_res.status_code == 200, get_res.text
    data = get_res.json()
    assert "system_status" in data
    assert "total_spend_usd" in data
    assert "total_tokens_used" in data
    assert "department_quotas" in data
    assert "provider_costs" in data

    # 3. Get quotas list
    quotas_res = admin_client.get("/api/admin/reports/quotas?days=30")
    assert quotas_res.status_code == 200, quotas_res.text
    assert isinstance(quotas_res.json(), list)


def test_operational_health_auth_required(client):
    res = client.get("/api/admin/reports/operational-health")
    assert res.status_code == 401
