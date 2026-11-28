# Step I — Key Rotation Runbook

Owner-facing document. Two rotations live here: the **master encryption key**
(encrypts stored provider credentials) and the **database credential exposed
in older docs**. They are independent — do them in either order, but never
skip the backup step of the one you are doing.

**Golden rules**

1. Never print, paste, or commit a key or password. Keys move through the
   environment (`.env` on the PC, secret store on a host), never through
   chat, screenshots, or Git.
2. Never delete the old key until the verification step for that rotation
   reads zero stale items. The old key is the only thing that can decrypt
   anything the sweep missed.
3. Take a backup first. Every time. Rotation is reversible only because the
   backup exists.

---

## 1. What is sealed, and where

| Store | Sealed with | Rotation support |
| --- | --- | --- |
| `provider_credentials` (LLM keys, WhatsApp token, …) | `MASTER_ENCRYPTION_KEY` | **Supported** — `app/security/key_rotation.py: rotate_provider_credentials()` + `scripts/rotate_master_key.py` |
| Integration sealed payloads (`{"ct","iv","kid"}`) | same key | Manual — `reencrypt_sealed_json()` per payload |
| Workflow sealed payloads | same key | Manual — same helper |
| Agent-run sealed task/result | same key | Manual — re-encrypt only if a run must stay readable; otherwise they age out with run retention |
| Agent Studio Baileys payloads | same key | Manual — same helper |

Code: `app/core/crypto.py` (`RotatingCredentialCipher`),
`app/security/key_rotation.py` (`SEALED_STORES`, sweep, helper).

---

## 2. Master-key rotation (owner does steps 2.1–2.4; the app does the rest)

### 2.1 Back up first

On the live PC, stop the API and worker, then copy the live folder and dump
the database (same procedure as the live-push README):

- Copy `C:\Users\10\Downloads\RAG infraspine\infaspine-AI-RAG` to
  `infaspine-AI-RAG-backup-<date>`.
- `pg_dump` the live database to
  `C:\Users\10\Downloads\infraspine-db-backup-<date>.dump`.

Do not proceed without both.

### 2.2 Generate the new key

On the PC, in PowerShell (never in chat):

```powershell
python -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Copy the output — that is the new `MASTER_ENCRYPTION_KEY`. It must decode to
exactly 32 bytes; the app refuses to start otherwise.

### 2.3 Deploy both keys, new one current

In the live `.env` (the file itself is never committed, never shared):

```ini
MASTER_ENCRYPTION_KEY=<new key from 2.2>
MASTER_ENCRYPTION_KEY_ID=v2
MASTER_ENCRYPTION_KEY_PREVIOUS=<old key, exactly as it was before>
MASTER_ENCRYPTION_KEY_PREVIOUS_ID=v1
```

Notes:

- `MASTER_ENCRYPTION_KEY_ID` must differ from the previous id, and the
  previous id must differ from the current one. The app refuses to start if
  the pair is inconsistent (previous key without previous id, or ids equal).
- The old key stays in the file as `MASTER_ENCRYPTION_KEY_PREVIOUS` — this
  is what keeps every stored credential decryptable while the sweep runs.
  Nothing is undecryptable at any point.
- Restart the API and worker. The app now decrypts with either key and
  encrypts only with the new one. Verify the app starts and `/health`
  answers before continuing.

### 2.4 Run the sweep, then verify

From the live folder (venv active), dry-run first, then the real sweep:

```powershell
# Dry run: counts only, changes nothing
.venv\Scripts\python.exe scripts\rotate_master_key.py --dry-run

# Real sweep: re-encrypts up to 100 stale credentials per run
.venv\Scripts\python.exe scripts\rotate_master_key.py --confirm
```

Repeat `--confirm` until the dry run reports `stale: 0`. Each run audits
`provider.credential_reencrypted` per credential (check the audit log if a
run reports skipped rows — a skipped row was left exactly as it was; it is
never deleted or guessed at).

**Verification (must read 0 before step 2.5):**

```sql
-- in psql on the live database
SELECT count(*) FROM provider_credentials
WHERE status IN ('active','pending') AND key_id <> 'v2';
```

Expected: `0`. Also open Settings → Providers in the admin UI and confirm a
provider still loads (it decrypts a stored credential on read).

### 2.5 Drop the retired key

Only after 2.4 reads 0:

```ini
# remove both lines from .env, then restart
MASTER_ENCRYPTION_KEY_PREVIOUS=
MASTER_ENCRYPTION_KEY_PREVIOUS_ID=
```

Restart and re-check `/health` and one provider read. Rotation is done.
Keep the backup from 2.1 for one week.

### Rollback

- **Before the sweep ran:** restore the old `.env` (old key as
  `MASTER_ENCRYPTION_KEY`, old id, no `PREVIOUS` lines) and restart. Nothing
  was re-encrypted, so nothing changes.
- **After the sweep ran (partly or fully):** do NOT just restore the old
  `.env` — rows already re-encrypted under the new key would become
  undecryptable. Instead restore the database backup from 2.1, then restore
  the old `.env`, and restart. (Alternatively, keep the new key current with
  the old key as `PREVIOUS` and re-run the sweep in the other direction —
  ask the Master Agent; do not improvise.)
- **If the app refuses to start** after editing `.env`: the validator found
  an inconsistent pair. Read the startup error, fix the pair, restart. The
  database is untouched by a failed start.

---

## 3. Database credential rotation (owner only — no code can do this)

The database credential that appears in older docs is exposed and must be
rotated by the owner. The app cannot rotate it: the credential lives in
PostgreSQL and in the connection string, not in app code.

### 3.1 Create the new credential

In `psql` as a superuser, on the live database host:

```sql
-- pick a fresh username if the old role name itself is the exposed part;
-- otherwise keep the role and only change its password.
CREATE ROLE infraspine_app_new LOGIN PASSWORD '<strong random password>';
GRANT CONNECT ON DATABASE infraspine TO infraspine_app_new;
GRANT USAGE ON SCHEMA public TO infraspine_app_new;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO infraspine_app_new;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
  TO infraspine_app_new;
-- repeat the GRANTs for every schema the app uses, then:
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO infraspine_app_new;
```

Generate the password the same way as §2.2 (32 random bytes, base64).
Record which role the app currently uses first — check the live `.env`
`DATABASE_URL` before changing anything.

### 3.2 Point the app at the new credential

Stop the API and worker. In the live `.env`, replace the user/password in
`DATABASE_URL` with the new role/password. Nothing else changes.
Restart the API and worker and check `/health` and one login.

### 3.3 Verify, then revoke the old credential

```sql
-- no sessions should remain on the old role (check first):
SELECT pid, usename, application_name, state
FROM pg_stat_activity WHERE usename = '<old role>';

-- only when the old role shows zero sessions:
REVOKE CONNECT ON DATABASE infraspine FROM <old role>;
ALTER ROLE <old role> NOLOGIN;
-- after one week with no incident:
DROP ROLE <old role>;
```

### Rollback

Restore the old `DATABASE_URL` in `.env` and restart (the old role still
exists until step 3.3 drops it). If the old role was already dropped,
restore from the backup taken before step 3.1.

---

## 4. Checklist (copy into the owner message when rotation is done)

- [ ] Backup taken (folder + database dump) before starting
- [ ] New master key generated (32 bytes, base64) — never shared in chat
- [ ] Both keys deployed, new one current, app restarted, `/health` OK
- [ ] Sweep dry-run → `stale: 0` (repeat `--confirm` until it is)
- [ ] SQL verification: 0 active/pending credentials under the old key id
- [ ] Provider read verified in the admin UI
- [ ] Retired key removed from `.env`, app restarted, `/health` OK
- [ ] DB credential: new role created, app pointed at it, old role revoked
- [ ] Backup kept for one week
