"""Master-key rotation sweep (Step I, slice 5).

Re-encrypts provider credentials sealed under a retired master key under the
current key. See docs/step-i-key-rotation-runbook.md for the full procedure.

Usage (from the repo root, venv active, live .env in place)::

    python scripts/rotate_master_key.py --dry-run    # counts only
    python scripts/rotate_master_key.py --confirm    # re-encrypts (limit 100)

Safety:
- ``--dry-run`` (the default) changes nothing.
- The sweep never prints a secret, never deletes a row, and leaves any row it
  cannot decrypt exactly as it was (reported as skipped).
- It refuses to run unless a previous key is configured: without
  ``MASTER_ENCRYPTION_KEY_PREVIOUS`` there is nothing to rotate from, and
  running it would be a no-op that hides a misconfigured deploy.
- Run it only after the backup step in the runbook, and only with the new
  key deployed as current (``MASTER_ENCRYPTION_KEY`` + new
  ``MASTER_ENCRYPTION_KEY_ID``) and the old key as previous.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# The script lives in scripts/; make the repo root importable so `app`
# resolves no matter where the interpreter was launched from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings, get_settings
from app.core.crypto import rotating_cipher_from_settings
from app.security.key_rotation import (
    count_stale_provider_credentials,
    rotate_provider_credentials,
)
from app.db.session import create_engine, create_sessionmaker


async def _run(*, confirm: bool, limit: int) -> int:
    settings: Settings = get_settings()
    if not settings.master_encryption_key_previous:
        print(
            "REFUSED: MASTER_ENCRYPTION_KEY_PREVIOUS is not set. "
            "Deploy the new key as current with the old key as previous "
            "(runbook section 2.3) before running the sweep."
        )
        return 2
    cipher = rotating_cipher_from_settings(settings)
    engine = create_engine(settings.database_url.get_secret_value())
    sessionmaker = create_sessionmaker(engine)
    async with sessionmaker() as db:
        stale = await count_stale_provider_credentials(db, cipher.key_id)
        print(f"current_key_id={cipher.key_id} stale_credentials={stale}")
        if not confirm:
            print("Dry run only — nothing changed. Re-run with --confirm to rotate.")
            return 0
        if stale == 0:
            print("Nothing to rotate.")
            return 0
        result = await rotate_provider_credentials(db, cipher, limit=limit)
        print(
            f"reencrypted={result.reencrypted} "
            f"skipped={len(result.skipped)} "
            f"failed={len(result.failed)}"
        )
        if result.failed:
            for item in result.failed:
                print(f"  skipped credential {item['id']}: {item['reason']}")
            print(
                "Some rows could not be decrypted and were left as-is. "
                "Investigate before dropping the retired key."
            )
            return 1
        remaining = await count_stale_provider_credentials(db, cipher.key_id)
        print(f"remaining_stale={remaining}")
        if remaining:
            print("More stale rows remain — run again (limit applies per run).")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count stale credentials without changing anything (default).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually re-encrypt stale credentials.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max credentials to re-encrypt in one run (default 100).",
    )
    args = parser.parse_args(argv)
    if args.dry_run and args.confirm:
        parser.error("--dry-run and --confirm are mutually exclusive")
    return asyncio.run(_run(confirm=args.confirm, limit=args.limit))


if __name__ == "__main__":
    sys.exit(main())
