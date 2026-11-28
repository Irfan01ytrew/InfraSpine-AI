"""Step G slice 6: graph merge identities — pure logic, no database."""

import uuid

import pytest

from app.continuity.graph_merge import seed_idempotency_key, seed_items, unified_key


def test_unified_key_forms():
    user_id, group_id, node_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert unified_key("employee", user_id=user_id) == f"person:{user_id}"
    assert unified_key("department", access_group_id=group_id) == f"department:{group_id}"
    assert unified_key("system", node_id=node_id) == f"continuity:{node_id}"
    assert unified_key("process", node_id=node_id) == f"continuity:{node_id}"
    with pytest.raises(ValueError):
        unified_key("employee")
    with pytest.raises(ValueError):
        unified_key("department")
    with pytest.raises(ValueError):
        unified_key("system")


def test_seed_items_deterministic_and_sorted():
    departments = [
        {"id": uuid.uuid4(), "name": "Finance", "access_group_id": uuid.uuid4()},
        {"id": uuid.uuid4(), "name": "IT", "access_group_id": uuid.uuid4()},
    ]
    persons = [
        {"user_id": uuid.uuid4(), "name": "b@example.com", "node_id": uuid.uuid4(), "department_id": None},
        {"user_id": uuid.uuid4(), "name": "a@example.com", "node_id": uuid.uuid4(), "department_id": None},
    ]
    first = seed_items(departments, persons)
    second = seed_items(list(reversed(departments)), list(reversed(persons)))
    assert first == second
    assert [item["kind"] for item in first] == ["department", "department", "person", "person"]
    assert first[0]["key"].startswith("department:")
    assert first[2]["key"].startswith("person:")
    assert all(item["quote"] for item in first)


def test_seed_idempotency_key_changes_with_content():
    tenant = uuid.uuid4()
    items = [{"key": "person:1"}, {"key": "department:2"}]
    assert seed_idempotency_key(tenant, items) == seed_idempotency_key(tenant, items)
    assert seed_idempotency_key(tenant, items) != seed_idempotency_key(tenant, items[:1])
    assert seed_idempotency_key(tenant, items) != seed_idempotency_key(uuid.uuid4(), items)
