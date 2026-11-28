# Packaging notes — InfraSpine-after-stepE.zip

> Prepared by Agent 10 (docs-packaging), branch `agent/docs-packaging`.
> **Updated by Agent 14 (docs-final), branch `agent/docs-final`, 27 Nov 2026:**
> `CHANGES-step-E.md` now has all gap counts filled in from verified agent reports,
> but still carries two **[PENDING: Agent 11/8 result]** markers (full suite +
> Alembic on the merged HEAD) and Gap 3 is still open. **Do not create the final
> zip yet.** The master agent creates it only after all
> 10 agents report green and the gaps are merged into `main` (see
> `REVIEW-CHECKLIST.md`). This file tells the master agent exactly what to include,
> what to exclude, and how to verify the package.
>
> The script `package_after_step_e.py` in this same folder implements this spec.
> It was **written, not run**, by Agent 10. Run it only after the merge gate passes,
> from a clean checkout of the merged `main`, and review its output before sending
> the zip to the owner.

## Output

- File: `InfraSpine-after-stepE.zip`
- Location: `~/workspace/infraspine/InfraSpine-after-stepE.zip`
  (the owner's handover used `C:\Users\10\Downloads\InfraSpine-after-stepE.zip`
  on the PC — that path is the owner's, not this VM's).
- Expected size: roughly the same order as the handover zip (~67 MB). If it is
  wildly larger, something excluded below was included by mistake.
- The zip must contain the repo **plus full git history** (same as the handover
  zip: repo + `.git` + all docs), so the next worker can continue from it.

## What to include

- The merged `main` repo, at its merged commit: `app/`, `frontend/` (source only —
  see `frontend/dist` below), `migrations/`, `tests/`, `scripts/`, `docs/`,
  `docker/`, `evals/`, `integrations/`, `_drafts/` (preserved, as in the handover),
  plus top-level `README.md`, `HANDOVER.md`, `START-HERE.txt`, `AGENTS.md`,
  `Makefile`, `Dockerfile`, `compose*.yaml`, `alembic.ini`, `pytest.ini`,
  `requirements*.txt`, `requirements.lock`, `.env.example`, `.env.docker.example`,
  `.gitignore`, `.dockerignore`.
- `assessment/` — the updated `CHANGES-step-E.md` (final, no `[TO FILL]` left),
  `REVIEW-CHECKLIST.md` (all boxes ticked, with evidence), this packaging file,
  and the packaging script. Also include `master-plan-v2.md` and the other
  `CHANGES-step-*.md` files from the handover, unchanged.
- `HANDOVER-PROMPT.md` (the handover prompt, unchanged) at the top of the package,
  so the next worker starts from the same instructions.
- A `MANIFEST.txt` inside the zip: merged commit id, branch, Alembic head (0103),
  full-suite test counts, frontend test/build results, date/time (Asia/Karachi),
  and the list of the 5 gaps with their commit ids.

## What to exclude — never put these in the zip

- `.venv/` — re-created by the next worker (`pip install -r requirements*.txt`).
- `frontend/node_modules/` and `frontend/dist/` — `node_modules` is re-created with
  `npm ci`; `dist` is built on the box that needs it. **Exception:** the live-push
  package built separately by `assessment/live-push/` *does* include a prebuilt
  `frontend/dist`, because the PC is not required to run `npm`. That is a different
  package from this handover zip — do not mix them up.
- `.env`, `.env.*` (except the `.example` files), any file containing a secret,
  token, password, or credential. Run `scripts/scan_release_secrets.py` before
  zipping (it is in the repo) and fix any hit before proceeding.
- `uploads/`, `data/`, `data/models/` (the embedding model downloads on first run),
  `logs/`, `*.log`, `__pycache__/`, `*.pyc`, `.pytest_cache/`.
- The live folder `C:\Users\10\Downloads\RAG infraspine\infaspine-AI-RAG` and the
  live database — never copied, never included, never touched.
- The old zip `InfraSpine-source-20261125` — must not be used or re-packaged.
- Any `*_test` database dump, any test database contents.
- `.git` objects that are not part of the repo history (no stray worktree state).

## How to build it (master agent, after the merge gate passes)

1. Check out a clean copy of the merged `main` (no uncommitted changes;
   `git status` clean apart from the packaging itself).
2. Confirm the merge commit, the Alembic head (`alembic current` → 0103), and the
   final test counts in `CHANGES-step-E.md`.
3. Run `scripts/scan_release_secrets.py` and resolve any hit.
4. Run the packaging script from the repo root:
   `python3 assessment/package_after_step_e.py --repo . --out ~/workspace/infraspine/InfraSpine-after-stepE.zip`
   (or the equivalent `zip` command in the script's docstring, if Python is not
   available in that shell).
5. Verify the zip: `unzip -l` lists the expected top-level entries; open
   `HANDOVER-PROMPT.md` and `assessment/CHANGES-step-E.md` from inside the zip and
   confirm they are the final versions; confirm no `.env`, `.venv`, `node_modules`,
   `uploads/`, or `data/` entry is present.
6. Record the zip's byte size and SHA-256 in the final report to the owner, with
   the merged commit id.

## After the zip is made

- Send the final report (which gaps are done, files changed, commit ids, exact
  test numbers, migrations, problems solved, anything left) **with** the zip.
- Do **not** live-push from this zip. Live-push is a separate, owner-approved
  step using `assessment/live-push/` (verified DB backup first, changed files
  only, restart via scheduled tasks, health + smoke checks). Rollback is file
  restore; never `alembic downgrade` on live.
