"""Create isolated synthetic C04 transfer fixtures without printing credentials."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.security import hash_password
from app.db.models import (
    AccessGroup,
    AccessGroupMember,
    DepartmentManager,
    Document,
    DocumentChunk,
    KnowledgeEntry,
    KnowledgeEvidence,
    KnowledgeRevision,
    ResourceAcl,
    User,
)
from app.db.session import create_engine, create_sessionmaker


def account(email: str, role: str) -> tuple[User, dict[str, str]]:
    password = secrets.token_urlsafe(24)
    return User(email=email, password_hash=hash_password(password), role=role, is_active=True), {
        "email": email,
        "password": password,
    }


async def seed(credentials_path: Path, state_path: Path) -> None:
    if credentials_path.exists() or state_path.exists():
        raise RuntimeError("Refusing to overwrite an existing C04 review fixture.")

    admin, admin_credentials = account("c04-review-admin@synthetic.test", "admin")
    steward, steward_credentials = account("c04-review-steward@synthetic.test", "manager")
    current, current_credentials = account("c04-departing-active@synthetic.test", "employee")
    legacy, legacy_credentials = account("c04-departing-inactive@synthetic.test", "employee")

    engine = create_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    sessions = create_sessionmaker(engine)
    try:
        async with sessions() as db:
            db.add_all([admin, steward, current, legacy])
            await db.flush()

            group = AccessGroup(name="C04 Transfer Operations", kind="department", created_by=admin.id)
            db.add(group)
            await db.flush()
            db.add_all(
                [
                    AccessGroupMember(group_id=group.id, user_id=steward.id, added_by=admin.id),
                    AccessGroupMember(group_id=group.id, user_id=current.id, added_by=admin.id),
                    AccessGroupMember(group_id=group.id, user_id=legacy.id, added_by=admin.id),
                    DepartmentManager(group_id=group.id, manager_user_id=steward.id, assigned_by=admin.id),
                ]
            )
            fixtures: list[dict[str, str]] = []
            for departing, category, label in (
                (current, "financial", "active departure"),
                (legacy, "policy", "inactive recovery"),
            ):
                quote = f"Synthetic C04 evidence for {label}; approval record 8472."
                document_id = uuid.uuid4()
                file_sha = hashlib.sha256(quote.encode("utf-8")).hexdigest()
                filename = f"c04-{label.replace(' ', '-')}.txt"
                document = Document(
                    id=document_id,
                    owner_id=departing.id,
                    visibility="restricted",
                    filename=filename,
                    file_format="txt",
                    storage_key=secrets.token_hex(32),
                    sha256=file_sha,
                    size_bytes=len(quote.encode("utf-8")),
                    version=1,
                    status="ready",
                    page_count=1,
                    chunk_count=1,
                    indexed_version=1,
                    indexed_filename=filename,
                )
                db.add(document)
                await db.flush()
                chunk = DocumentChunk(
                    document_id=document.id,
                    chunk_index=0,
                    page_number=1,
                    locator=None,
                    content=quote,
                    embedding=b"\x00\x00\x00\x00",
                )
                db.add(chunk)
                await db.flush()
                db.add(ResourceAcl(
                    resource_type="document",
                    document_id=document.id,
                    principal_group_id=group.id,
                    origin="manual",
                    created_by=admin.id,
                ))

                entry = KnowledgeEntry(author_user_id=departing.id, owner_user_id=departing.id)
                db.add(entry)
                await db.flush()
                revision = KnowledgeRevision(
                    entry_id=entry.id,
                    author_user_id=departing.id,
                    revision_number=1,
                    statement=f"Synthetic {label} knowledge remains under {category} stewardship.",
                    category=category,
                    visibility="restricted",
                    scope_group_id=group.id,
                    # Company-knowledge custody is based on the immutable revision's visibility
                    # and scope; publication itself remains guarded by the review workflow.
                    status="draft",
                )
                db.add(revision)
                await db.flush()
                entry.current_revision_id = revision.id
                db.add(KnowledgeEvidence(
                    revision_id=revision.id,
                    document_id=document.id,
                    document_key=document.id,
                    document_version=1,
                    document_sha256=file_sha,
                    filename=filename,
                    chunk_id=chunk.id,
                    chunk_key=chunk.id,
                    page_number=1,
                    start_offset=0,
                    end_offset=len(quote),
                    quote=quote,
                    prefix="",
                    suffix="",
                ))
                fixtures.append({
                    "departing_email": departing.email,
                    "entry_id": str(entry.id),
                    "document_id": str(document.id),
                    "category": category,
                })

            # Preserve the legacy-owner recovery scenario as a historical state created
            # before deactivation. The normal offboarding workflow transfers custody first.
            legacy.is_active = False
            await db.commit()
            state = {
                "database": os.environ.get("C04_REVIEW_DATABASE", ""),
                "migration": "0057_org_mfa_session_proof",
                "group_id": str(group.id),
                "admin_id": str(admin.id),
                "steward_id": str(steward.id),
                "active_departing_id": str(current.id),
                "inactive_departing_id": str(legacy.id),
                "knowledge": fixtures,
            }

        credentials = {
            "admin": admin_credentials,
            "managers": [steward_credentials],
            "employees": [current_credentials, legacy_credentials],
        }
        credentials_path.parent.mkdir(parents=True, exist_ok=True)
        credentials_path.write_text(json.dumps(credentials, indent=2) + "\n", encoding="utf-8")
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        for item in credentials["managers"] + credentials["employees"]:
            item["password"] = ""
        credentials["admin"]["password"] = ""
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(seed(args.credentials, args.state))
    print("C04 synthetic review users, scoped source evidence and transfer fixtures seeded.")


if __name__ == "__main__":
    main()
