"""Verify one C16 synthetic connector row is encrypted and AAD-bound in the C11 clone."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncpg
import psutil
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.crypto import CredentialCipher, EncryptedValue


REVIEW_ROOT = ROOT / ".verification-local-review-20261028-c11-sqlite-ui"
EXPECTED_DATABASE = "infraspine_c11sqliteui_20261028_test"


async def main(connector_id: str) -> dict[str, object]:
    state = json.loads((REVIEW_ROOT / "stack-state.json").read_text(encoding="utf-8-sig"))
    if state.get("review_database") != EXPECTED_DATABASE or state.get("migration") != "0061_persist_cloud_connectors":
        raise RuntimeError("C11 synthetic review identity is unexpected.")
    runtime = psutil.Process(int(state["api_pid"])).environ()
    if runtime.get("APP_ENV") != "testing" or runtime.get("RAG_WORKER_ENABLED", "").casefold() != "false":
        raise RuntimeError("The saved runtime is not the worker-disabled C11 review process.")
    url = make_url(runtime.get("DATABASE_URL", ""))
    if url.database != EXPECTED_DATABASE or url.host not in {"localhost", "127.0.0.1", "::1"} or not url.port:
        raise RuntimeError("The C16 verifier refused a database outside the exact loopback C11 clone.")
    os.environ.update(runtime)
    settings = Settings()
    connection = await asyncpg.connect(
        host=url.host, port=url.port, user=url.username, password=url.password,
        database=url.database, timeout=8,
    )
    try:
        revision = await connection.fetchval("SELECT version_num FROM alembic_version LIMIT 1")
        row = await connection.fetchrow(
            "SELECT id, credential_ciphertext, credential_nonce, credential_key_id FROM cloud_connector_configs WHERE id = $1",
            connector_id,
        )
        if revision != "0061_persist_cloud_connectors" or row is None:
            raise RuntimeError("The connector row or expected migration was not present in the C11 database.")
        cipher = CredentialCipher(settings.master_key_bytes, settings.master_encryption_key_id)
        decoded = json.loads(
            cipher.decrypt(
                EncryptedValue(bytes(row["credential_ciphertext"]), bytes(row["credential_nonce"]), row["credential_key_id"]),
                associated_data=f"infraspine:cloud-connector:{row['id']}".encode(),
            )
        )
        ciphertext = bytes(row["credential_ciphertext"])
        checks = {
            "ciphertext_present": len(ciphertext) > 32,
            "nonce_is_96_bit": len(bytes(row["credential_nonce"])) == 12,
            "plaintext_sentinels_absent": b"C16_ONLY_SYNTHETIC_" not in ciphertext,
            "aad_bound_synthetic_credentials_decrypt": (
                decoded.get("client_id") == "synthetic-client-id"
                and str(decoded.get("client_secret", "")).startswith("C16_ONLY_SYNTHETIC_SECRET_")
                and str(decoded.get("refresh_token", "")).startswith("C16_ONLY_SYNTHETIC_REFRESH_")
            ),
        }
        if not all(checks.values()):
            raise RuntimeError("The synthetic connector ciphertext verification failed.")
        return {"database": EXPECTED_DATABASE, "migration": revision, **checks}
    finally:
        await connection.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Pass only the synthetic connector UUID.")
    try:
        print(json.dumps(asyncio.run(main(sys.argv[1]))))
    except Exception as exc:
        print(f"C16 synthetic ciphertext verification failed safely: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
