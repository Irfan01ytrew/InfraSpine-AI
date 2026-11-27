"""Seed a new isolated A01 MFA review database without emitting synthetic credentials."""

import argparse
import base64
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select

from app.core.crypto import CredentialCipher
from app.core.security import hash_password, hash_session_token, new_session_token
from app.core.totp import new_totp_secret
from app.db.models import ROLE_ADMIN, ROLE_EMPLOYEE, User, UserMfaFactor, UserSession
from app.db.session import create_engine, create_sessionmaker


async def seed(fixture_path: Path, old_session_path: Path) -> None:
    source = json.loads(fixture_path.read_text(encoding="utf-8"))
    admin = source["admin"]
    employee = next(item for item in source["employees"] if item["email"] == "f20-browser-requester@synthetic.test")
    engine = create_engine(os.environ["DATABASE_URL"], pool_size=1, max_overflow=0)
    sessions = create_sessionmaker(engine)
    session_token = new_session_token()
    master_key = base64.b64decode(os.environ["MASTER_ENCRYPTION_KEY"], validate=True)
    cipher = CredentialCipher(master_key, os.environ["MASTER_ENCRYPTION_KEY_ID"])
    try:
        async with sessions() as db:
            count = await db.scalar(select(func.count()).select_from(User))
            if count:
                raise RuntimeError("Refusing to seed a database that already contains users.")
            db.add_all(
                [
                    User(
                        email=admin["email"],
                        password_hash=hash_password(admin["password"]),
                        role=ROLE_ADMIN,
                        is_active=True,
                    ),
                    User(
                        email=employee["email"],
                        password_hash=hash_password(employee["password"]),
                        role=ROLE_EMPLOYEE,
                        is_active=True,
                    ),
                ]
            )
            old = User(
                email="f20-a01-prepolicy-session@synthetic.test",
                password_hash=hash_password(new_session_token()),
                role=ROLE_EMPLOYEE,
                is_active=True,
            )
            db.add(old)
            await db.flush()
            encrypted = cipher.encrypt(new_totp_secret(), associated_data=f"infraspine:user-mfa:v1:{old.id}".encode("ascii"))
            db.add(
                UserMfaFactor(
                    user_id=old.id,
                    secret_ciphertext=encrypted.ciphertext,
                    secret_nonce=encrypted.nonce,
                    key_id=encrypted.key_id,
                    enabled_at=datetime.now(UTC),
                )
            )
            db.add(
                UserSession(
                    user_id=old.id,
                    token_hash=hash_session_token(session_token),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    mfa_verified_at=None,
                )
            )
            await db.commit()
        old_session_path.parent.mkdir(parents=True, exist_ok=True)
        old_session_path.write_text(
            json.dumps({"email": "f20-a01-prepolicy-session@synthetic.test", "session_token": session_token}) + "\n",
            encoding="utf-8",
        )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-fixture", required=True, type=Path)
    parser.add_argument("--old-session-output", required=True, type=Path)
    args = parser.parse_args()
    if not args.credential_fixture.is_file():
        raise SystemExit("Protected synthetic credential fixture is unavailable.")
    if args.old_session_output.exists():
        raise SystemExit("Refusing to overwrite the synthetic old-session fixture.")
    asyncio.run(seed(args.credential_fixture, args.old_session_output))
    print("A01 synthetic accounts seeded; pre-policy session token stored in the protected review folder.")


if __name__ == "__main__":
    main()
