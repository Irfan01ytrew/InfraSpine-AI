from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import insert, text

from app.db.models import KnowledgeProcess, KnowledgeProcessPolicyLink
from tests.conftest import ADMIN_EMAIL, USER_EMAIL, login, run_db
from tests.test_access_control import set_access
from tests.test_knowledge_conflicts import _grant_reviewers
from tests.test_policy_impact import _published_policy


def test_process_map_pagination_rechecks_live_source_access(client):
    _grant_reviewers(client)
    login(client, ADMIN_EMAIL)
    document_id, entry_id = _published_policy(client)
    login(client, USER_EMAIL)
    owner_id = run_db(lambda conn: conn.scalar(text("SELECT id FROM users WHERE email=:email"), {"email": USER_EMAIL}))
    policy_id = UUID(entry_id)
    start = datetime.now(UTC)

    async def seed(conn):
        processes = []
        links = []
        for index in range(52):
            process_id = uuid4()
            created_at = start - timedelta(seconds=index)
            processes.append({"id": process_id, "name": f"Pagination process {index:02d}", "description": "Synthetic scale row.",
                "scope_group_id": None, "owner_user_id": owner_id, "created_by": owner_id, "created_at": created_at,
                "updated_at": created_at})
            links.append({"id": uuid4(), "process_id": process_id, "entry_id": policy_id, "linked_by": owner_id,
                "created_at": created_at, "updated_at": created_at})
        await conn.execute(insert(KnowledgeProcess), processes)
        await conn.execute(insert(KnowledgeProcessPolicyLink), links)
    run_db(seed)

    first = client.get("/api/governance/processes")
    assert first.status_code == 200, first.text
    page = first.json()
    assert len(page["items"]) == 50
    assert page["items"][0]["name"] == "Pagination process 00"
    assert page["next_cursor"]
    assert client.get("/api/governance/processes", params={"cursor": "x" * 161}).status_code == 422

    login(client, ADMIN_EMAIL)
    assert set_access(client, document_id, "restricted", users=[ADMIN_EMAIL]).status_code == 200
    login(client, USER_EMAIL)
    next_page = client.get("/api/governance/processes", params={"cursor": page["next_cursor"]})
    assert next_page.status_code == 200, next_page.text
    assert next_page.json() == {"items": [], "next_cursor": None}
