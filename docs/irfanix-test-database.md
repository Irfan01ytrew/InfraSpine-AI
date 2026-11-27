# Isolated IRFANIX regression database

Run from the primary InfraSpine checkout with the development Python dependencies installed. On Windows, use the local PostgreSQL binaries. The test runner creates a fresh UTF-8 cluster on loopback and a regular database owner for the disposable `infraspine_ai_test` database. It passes the ephemeral URL only to pytest. Alembic migrations and fixture resets run within that test cluster. The production role does not need `CREATEDB`.

```powershell
.\.venv\Scripts\python.exe scripts/irfanix_postgres_tests.py --pg-bin 'C:\Program Files\PostgreSQL\18\bin' -- -q tests/test_rag_units.py tests/unit/test_agent_action_capabilities.py
```

On this host, port 15432 passed the regression run. Add `--port 15432` before `--` to select it explicitly if it is free. The default selects an available loopback port. A reserved or concurrently taken port fails at startup. Retry with a different port without modifying Windows reservations or touching the server on port 5432.

The runner stops PostgreSQL and removes its temporary cluster on normal exit. If the parent is forcibly killed, check the new `irfanix-pg-test-*` directory in the invoking user's temporary directory and inspect the PID and port in `cluster/postmaster.pid`. Verify the path and process belong to that test run before using `pg_ctl -D <verified-test-cluster> -m fast -w stop`. Preserve logs if needed to investigate. Never use a configured live database URL as `TEST_DATABASE_URL`.

Verified on Windows with PostgreSQL 18.6 on port 15432. Both selected suites completed with 129 passing tests.

Run the existing synthetic multilingual retrieval comparison against a separate disposable evaluation database:

```powershell
.\.venv\Scripts\python.exe scripts/irfanix_postgres_tests.py --pg-bin 'C:\Program Files\PostgreSQL\18\bin' --retrieval-eval
```

The benchmark reads locally cached embedding weights, uses a temporary FAISS index and writes `evals/reports/multilingual-comparison.json`. Its retrieval scores do not measure answer correctness or the rate of false refusals.
