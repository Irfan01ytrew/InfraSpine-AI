"""Seed a fresh synthetic C16 cloud-connector review database and protected Admin fixture."""

from __future__ import annotations

import json
import os
import secrets
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.security import hash_password
from app.db.models import AccessGroup, User


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    fixture_path = Path(os.environ["C16_CREDENTIAL_FIXTURE"])
    data_dir = Path(os.environ["RAG_DATA_DIR"])
    admin = {"email": "c16-admin@example.test", "password": secrets.token_urlsafe(32)}
    admin_id = uuid.uuid4()
    department_id = uuid.uuid4()

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                User.__table__.insert().values(
                    id=admin_id,
                    email=admin["email"],
                    role="admin",
                    password_hash=hash_password(admin["password"]),
                    is_active=True,
                )
            )
            await connection.execute(
                AccessGroup.__table__.insert().values(
                    id=department_id,
                    name="Synthetic Engineering",
                    kind="department",
                    source="local",
                    created_by=admin_id,
                    lifecycle_status="active",
                )
            )
    finally:
        await engine.dispose()

    fixture_path.write_text(json.dumps({"admin": admin}, separators=(",", ":")), encoding="utf-8")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "company-profile.json").write_text(
        json.dumps(
            {
                "company_name": "Synthetic C16 Review",
                "industry": "General",
                "brand_name": "InfraSpine QA",
                "primary_color": "#145da0",
                "setup_complete": True,
                "template_id": None,
                "template_version": None,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    print("C16_REVIEW_SEED=READY")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
