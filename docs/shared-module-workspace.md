# Shared module chat workspace

The canonical shared checkout for Module 01–12 source edits is:

`C:\Users\10\Downloads\RAG infraspine\infaspine-AI-RAG`

Every module chat must make source edits in that checkout when the user expects the changes to appear in the running application. A chat opened from `work\moduleNN-*` can still inspect its module worktree, but must target the canonical checkout for shared edits. Confirm the target with `git -C 'C:\Users\10\Downloads\RAG infraspine\infaspine-AI-RAG' rev-parse --show-toplevel` and inspect its Git status first. Preserve existing edits and migrations. Never copy or overwrite the main checkout from a module worktree wholesale.

Existing module worktrees contain independent in-progress edits. Those edits become visible to the app only after they are reviewed and integrated into the canonical checkout. Keep their work intact until review.

## Live preview

Run `scripts\start_module_live_preview.ps1` from the canonical checkout. It serves the shared frontend at `http://127.0.0.1:5173/admin/` with Vite hot reload and proxies API calls to the existing local API at `http://127.0.0.1:8000`. UI edits in the canonical checkout appear in the browser as they are saved. Python/backend edits require the API process to reload or restart before the running API reflects them. The preview script does not restart the API or run database migrations.

Stop the preview with `scripts\start_module_live_preview.ps1 -Stop`. Browser edits and approval-gated or data-changing API actions still follow normal application authorization.
