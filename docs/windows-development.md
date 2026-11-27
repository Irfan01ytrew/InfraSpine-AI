# Windows development and verification

The native Windows Docling PDF pipeline can terminate its parser process with an access violation. The application
therefore routes Windows PDF ingestion through the built-in parser while retaining Docling for DOCX/PPTX. The parser
catalog reports the active path. Run PDF table/image extraction acceptance inside the Linux container, which is the
supported Docling PDF runtime; Windows tests for that path are explicitly skipped.

Use an isolated Python environment for verification. These commands preserve an existing `.venv` and invoke the
selected interpreter explicitly:

```powershell
$python312 = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
& $python312 -m venv .venv-verify-312
.\.venv-verify-312\Scripts\python.exe -m pip install --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple torch==2.14.1
.\.venv-verify-312\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt -r requirements-media.txt -r requirements-document-tables.txt -r requirements-reporting.txt
.\.venv-verify-312\Scripts\python.exe -m pip check
```

Ensure `.env` points `DATABASE_URL` at the application database, then create a dedicated PostgreSQL test database
once, separate from the application database:

```powershell
createdb --username postgres infraspine_ai_test
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://infraspine_app:<url-encoded-password>@localhost:5432/infraspine_ai_test'
```

Then run focused checks followed by the complete backend suite:

```powershell
.\.venv-verify-312\Scripts\python.exe -m pytest tests/test_database_safety.py tests/test_media_ingestion.py tests/test_gemini_grounding.py -q
.\.venv-verify-312\Scripts\python.exe -m pytest -q
.\.venv-verify-312\Scripts\python.exe -m pip check
Remove-Item Env:TEST_DATABASE_URL
```

The integration-test harness rejects a test URL that resolves to the configured application database, even if its
database name ends in `_test`. Downgrade/upgrade and cleanup commands are confined to the separate test database.
The same-database development-schema mode requires an explicit fresh `infraspine_test_<12 lowercase hex>` schema,
`TEST_DATABASE_ALLOW_SHARED_SCHEMA=1`, and `APP_ENV=development`.

Frontend checks use the locked npm dependencies:

```powershell
cd frontend
npm ci
npm test -- --run
npm run build
cd ..
```

This project uses PostgreSQL integration tests. Do not run `pytest` until `TEST_DATABASE_URL` identifies the
dedicated test database; do not point it at a production or application database. Never run `alembic downgrade`
or `pytest` against the application's configured database.
