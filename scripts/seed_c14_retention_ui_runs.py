"""Create two expired failed runs using the isolated app's real encryption key."""

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.crypto import CredentialCipher
from app.db.models import AgentProfile, AgentProfileRun, User
from app.db.session import test_engine_options
from app.services.agents import seal_run_value


async def main(profile_id: uuid.UUID) -> None:
    settings = get_settings()
    database = settings.database_url.get_secret_value().rsplit("/", 1)[-1]
    if not database.endswith("_test"):
        raise SystemExit("Refusing to seed a database not named with the _test suffix.")
    fixture = json.loads(Path(os.environ["INFRASPINE_RETENTION_QA_FIXTURE"]).read_text(encoding="utf-8-sig"))
    admin_email = fixture["admin"]["email"]
    cipher = CredentialCipher(settings.master_key_bytes, settings.master_encryption_key_id)
    engine = create_async_engine(settings.database_url.get_secret_value(), **test_engine_options())
    try:
        async with engine.begin() as conn:
            existing_runs = await conn.scalar(select(func.count()).select_from(AgentProfileRun))
            if existing_runs:
                raise SystemExit("Refusing to seed runs into a database that already has run history.")
            profile = await conn.execute(
                select(AgentProfile.id, AgentProfile.version, AgentProfile.department_group_id).where(
                    AgentProfile.id == profile_id
                )
            )
            profile_row = profile.one_or_none()
            if profile_row is None:
                raise SystemExit("Synthetic agent profile was not found.")
            admin_id = await conn.scalar(select(User.id).where(User.email == admin_email, User.role == "admin"))
            if admin_id is None:
                raise SystemExit("Synthetic admin was not found.")
            now = datetime.now(UTC)
            rows = []
            for days_ago in (90, 60):
                run_id = uuid.uuid4()
                encrypted_task = seal_run_value(cipher, run_id, "task", "Synthetic retention QA task")
                requested_at = now - timedelta(days=days_ago)
                rows.append(
                    {
                        "id": run_id,
                        "profile_id": profile_row.id,
                        "profile_version": profile_row.version,
                        "department_group_id": profile_row.department_group_id,
                        "requester_user_id": admin_id,
                        "capability": "knowledge_search",
                        "task_ciphertext": encrypted_task.ciphertext,
                        "task_nonce": encrypted_task.nonce,
                        "task_key_id": encrypted_task.key_id,
                        "status": "failed",
                        "execution_mode": "sync",
                        "attempts": 1,
                        "failure_code": "synthetic_qa_failure",
                        "requested_at": requested_at,
                        "available_at": requested_at,
                        "started_at": requested_at,
                        "finished_at": requested_at,
                    }
                )
            await conn.execute(AgentProfileRun.__table__.insert(), rows)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: seed_c14_retention_ui_runs.py <synthetic-agent-profile-uuid>")
    asyncio.run(main(uuid.UUID(sys.argv[1])))
