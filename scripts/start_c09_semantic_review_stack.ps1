$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$reviewRoot = Join-Path $repoRoot '.verification-local-review-20261028-c09-semantic'
$database = 'infraspine_c09semanticreview_20261028_test'
$postgresPort = 55928
$apiPort = 63008
$frontendPort = 5179
$pgBin = 'C:\Program Files\PostgreSQL\18\bin'
$psql = Join-Path $pgBin 'psql.exe'
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$vite = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'
$model = Join-Path $repoRoot 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
if (Test-Path -LiteralPath $reviewRoot) { throw 'The C09 review folder already exists; inspect it before any reuse.' }
foreach ($port in @($apiPort, $frontendPort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "Port $port is occupied; no existing process was changed." }
}
if (!(Test-Path -LiteralPath $psql -PathType Leaf) -or !(Test-Path -LiteralPath $python -PathType Leaf) -or !(Test-Path -LiteralPath $vite -PathType Leaf)) { throw 'A required local runtime is unavailable.' }
if (!(Test-Path -LiteralPath (Join-Path $model 'modules.json') -PathType Leaf)) { throw 'The pinned local embedding model is incomplete; no download will be attempted.' }
& (Join-Path $pgBin 'pg_isready.exe') -h 127.0.0.1 -p $postgresPort | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The retained loopback PostgreSQL cluster is unavailable; no cluster operation was attempted.' }
$exists = & $psql -h 127.0.0.1 -p $postgresPort -U postgres -d postgres -qAt -c "SELECT count(*) FROM pg_database WHERE datname='$database'"
if ($LASTEXITCODE -ne 0 -or $exists.Trim() -ne '0') { throw 'The dedicated C09 database exists or could not be checked; preserving it.' }

New-Item -ItemType Directory -Path $reviewRoot | Out-Null
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = [System.Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($owner)
foreach ($sid in @($owner.Value, 'S-1-5-18', 'S-1-5-32-544')) {
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new($sid),
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit),
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $reviewRoot -AclObject $acl
foreach ($folder in @('data', 'logs', 'logs\api', 'evidence', 'tmp')) { New-Item -ItemType Directory -Path (Join-Path $reviewRoot $folder) -Force | Out-Null }

& $psql -h 127.0.0.1 -p $postgresPort -U postgres -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE $database"
if ($LASTEXITCODE -ne 0) { throw 'Could not create the new disposable C09 database.' }

$env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:$postgresPort/$database"
$env:APP_ENV = 'testing'
$keyGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
$keyBytes = New-Object byte[] 32
$keyGenerator.GetBytes($keyBytes)
$env:MASTER_ENCRYPTION_KEY = [Convert]::ToBase64String($keyBytes)
$keyGenerator.Dispose()
[Array]::Clear($keyBytes, 0, $keyBytes.Length)
$env:MASTER_ENCRYPTION_KEY_ID = 'c09-local-review'
$env:SESSION_COOKIE_SECURE = 'false'
$env:LLM_MOCK_MODE = 'true'
$env:PYTHONPATH = $repoRoot
$env:RAG_DATA_DIR = Join-Path $reviewRoot 'data'
$env:RAG_MODELS_DIR = Join-Path $repoRoot 'data\models'
$env:RAG_EMBEDDING_MODEL = $model
$env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:RAG_VECTOR_STORE = 'faiss'
$env:RAG_WORKER_ENABLED = 'false'
$env:RAG_INGESTION_MODE = 'worker'
$env:RAG_INDEX_SYNC_SECONDS = '0'
$env:RAG_USE_DOCLING = 'false'
$env:RAG_PARSE_ISOLATION = 'true'
$env:LOG_DIR = Join-Path $reviewRoot 'logs\api'
$env:TEMP = Join-Path $reviewRoot 'tmp'
$env:TMP = $env:TEMP
$env:INFRASPINE_C09_REVIEW_ROOT = $reviewRoot
$env:INFRASPINE_DEV_API_PROXY_TARGET = "http://127.0.0.1:$apiPort"

Push-Location $repoRoot
try {
    & $python -m alembic upgrade head *> (Join-Path $reviewRoot 'logs\migration.log')
    if ($LASTEXITCODE -ne 0) { throw 'C09 disposable database migration failed; review database retained for inspection.' }
    & $python (Join-Path $repoRoot 'scripts\seed_c09_semantic_review_admin.py') *> (Join-Path $reviewRoot 'logs\seed.log')
    if ($LASTEXITCODE -ne 0) { throw 'C09 synthetic Admin seeding failed; database and review folder retained for inspection.' }
} finally { Pop-Location }

$api = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$apiPort", '--no-access-log') `
    -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $reviewRoot 'logs\api.stdout.log') `
    -RedirectStandardError (Join-Path $reviewRoot 'logs\api.stderr.log')
$vite = Start-Process -FilePath $node -ArgumentList @(('"' + $vite + '"'), '--host', '127.0.0.1', '--strictPort', '--port', "$frontendPort") `
    -WorkingDirectory (Join-Path $repoRoot 'frontend') -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $reviewRoot 'logs\vite.stdout.log') `
    -RedirectStandardError (Join-Path $reviewRoot 'logs\vite.stderr.log')
$state = [ordered]@{
    apiPid = $api.Id
    vitePid = $vite.Id
    apiPort = $apiPort
    frontendPort = $frontendPort
    database = $database
    migration = 'head'
    startedAt = (Get-Date).ToString('o')
}
$state | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $reviewRoot 'stack-state.json') -Encoding utf8
Write-Output 'C09_CURRENT_SOURCE_STACK_STARTED'
