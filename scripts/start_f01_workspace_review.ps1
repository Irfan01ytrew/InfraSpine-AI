param(
    [int]$PostgresPort = 55928,
    [int]$ApiPort = 63006,
    [int]$FrontendPort = 5177
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$database = 'infraspine_f01workspace_20261028_test'
$reviewRoot = Join-Path $repoRoot '.verification-local-review-20261028-f01-workspaces'
$fixturePath = Join-Path $reviewRoot 'review-credentials.json'
$dataDirectory = Join-Path $repoRoot '.verification-local-review-20261027-native\f08-workerprocess-pg-isolated-20261027-r1\cluster'
$pgCtl = 'C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe'
$createdb = 'C:\Program Files\PostgreSQL\18\bin\createdb.exe'
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'

if (Test-Path -LiteralPath $reviewRoot) {
    $entries = @(Get-ChildItem -LiteralPath $reviewRoot -Force)
    $unexpected = @($entries | Where-Object { $_.Name -notin @('review-credentials.json','data','logs') -and $_.Name -notmatch '^stack-processes-63006-5177-.*\.json$' })
    if ($unexpected.Count -gt 0) { throw 'The F01 review folder contains unexpected files; inspect it before reuse.' }
    $priorState = @(Get-ChildItem -LiteralPath $reviewRoot -File -Filter 'stack-processes-63006-5177-*.json' -ErrorAction SilentlyContinue)
    $activePriorState = @($priorState | Where-Object { (Get-Content -LiteralPath $_.FullName -Raw).Trim() -ne '{}' })
    if ($activePriorState.Count -gt 0) {
        throw 'A prior F01 review process-state file exists; inspect it before reuse.'
    }
} else {
    New-Item -ItemType Directory -Path $reviewRoot | Out-Null
}
if (-not (Test-Path -LiteralPath $dataDirectory -PathType Container)) { throw 'The retained isolated PostgreSQL data directory is unavailable.' }
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'The project verification Python environment is unavailable.' }
foreach ($port in @($ApiPort,$FrontendPort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "A required isolated review port ($port) is occupied; no process was changed." }
}

$ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$ownerRule = '*{0}:(OI)(CI)F' -f $ownerSid
& icacls.exe $reviewRoot /inheritance:r /grant:r $ownerRule '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'
if ($LASTEXITCODE -ne 0) { throw 'Windows could not protect the new synthetic review folder ACL.' }
$folderAcl = Get-Acl -LiteralPath $reviewRoot
$allowedFolderSids = @($ownerSid,'S-1-5-18','S-1-5-32-544')
$folderRules = @($folderAcl.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier]))
if (-not $folderAcl.AreAccessRulesProtected -or -not $folderRules -or @($folderRules | Where-Object { $_.IdentityReference.Value -notin $allowedFolderSids }).Count -gt 0) {
    throw 'The synthetic review folder ACL is not restricted to the current user, SYSTEM, and local Administrators.'
}

if (-not (Test-Path -LiteralPath $fixturePath -PathType Leaf)) {
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $passwordBytes = New-Object byte[] 32
    $rng.GetBytes($passwordBytes)
    $rng.Dispose()
    $password = [Convert]::ToBase64String($passwordBytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    $fixture = @{ admin = @{ email = 'f01-workspace-admin@synthetic.test'; password = $password } }
    [IO.File]::WriteAllText($fixturePath, ($fixture | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
    $password = $null
    [Array]::Clear($passwordBytes, 0, $passwordBytes.Length)
    $passwordBytes = $null
    $fixture = $null
} else {
    $fixtureAcl = Get-Acl -LiteralPath $fixturePath
    $fixtureData = Get-Content -LiteralPath $fixturePath -Raw | ConvertFrom-Json
    if ([string]$fixtureData.admin.email -ne 'f01-workspace-admin@synthetic.test' -or [string]$fixtureData.admin.password.Length -lt 24) {
        throw 'The retained F01 credential fixture does not match this isolated review.'
    }
    $fixtureData.admin.password = $null
    $fixtureData = $null
}
$fileOwnerRule = '*{0}:F' -f $ownerSid
& icacls.exe $fixturePath /inheritance:r /grant:r $fileOwnerRule '*S-1-5-18:F' '*S-1-5-32-544:F'
if ($LASTEXITCODE -ne 0) { throw 'Windows could not protect the synthetic credential file ACL.' }
$fixtureAcl = Get-Acl -LiteralPath $fixturePath
$fixtureRules = @($fixtureAcl.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier]))
if (-not $fixtureAcl.AreAccessRulesProtected -or -not $fixtureRules -or @($fixtureRules | Where-Object { $_.IdentityReference.Value -notin $allowedFolderSids }).Count -gt 0) {
    throw 'The synthetic credential file ACL is not restricted to the current user, SYSTEM, and local Administrators.'
}

$clusterWasRunning = $false
& $pgCtl -D $dataDirectory status *> $null
if ($LASTEXITCODE -eq 0) {
    $clusterWasRunning = $true
} else {
    & $pgCtl -D $dataDirectory -o "-c listen_addresses=127.0.0.1 -p $PostgresPort -c max_connections=20 -c shared_buffers=16MB -c work_mem=1MB -c maintenance_work_mem=16MB" -l (Join-Path (Split-Path -Parent $dataDirectory) 'postgresql.log') start -w
    if ($LASTEXITCODE -ne 0) { throw 'The retained isolated PostgreSQL cluster did not start; its data was preserved.' }
}

try {
    $env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$database"
    $existing = & $psql -h 127.0.0.1 -p $PostgresPort -U postgres -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$database'"
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the isolated PostgreSQL catalog.' }
    $databaseAlreadyExists = (($existing -join '').Trim() -eq '1')
    if (-not $databaseAlreadyExists) {
        & $createdb -h 127.0.0.1 -p $PostgresPort -U postgres $database
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the new disposable F01 database.' }
    } else {
        $currentMigration = (& $psql -h 127.0.0.1 -p $PostgresPort -U postgres -d $database -tAc 'SELECT version_num FROM alembic_version') -join ''
        if ($LASTEXITCODE -ne 0 -or $currentMigration.Trim() -ne '0059_workspace_hierarchy') { throw 'Existing F01 database is not at the expected migration head; it was preserved for inspection.' }
        $userCount = (& $psql -h 127.0.0.1 -p $PostgresPort -U postgres -d $database -tAc 'SELECT count(*) FROM users') -join ''
        if ($LASTEXITCODE -ne 0 -or $userCount.Trim() -notin @('0','1')) { throw 'Existing F01 database has unexpected accounts; it was preserved for inspection.' }
    }

    $env:APP_ENV = 'testing'
    $env:LLM_MOCK_MODE = 'true'
    $keyRng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $keyBytes = New-Object byte[] 32
    $keyRng.GetBytes($keyBytes)
    $keyRng.Dispose()
    $env:MASTER_ENCRYPTION_KEY = [Convert]::ToBase64String($keyBytes)
    [Array]::Clear($keyBytes, 0, $keyBytes.Length)
    $env:MASTER_ENCRYPTION_KEY_ID = 'f01-workspace-review'
    $env:SESSION_COOKIE_SECURE = 'false'
    $env:RAG_DATA_DIR = Join-Path $reviewRoot 'data'
    $env:RAG_MODELS_DIR = Join-Path $repoRoot 'data\models'
    $env:RAG_EMBEDDING_ALLOW_DOWNLOAD = 'false'
    $env:RAG_EMBEDDING_MODEL = Join-Path $repoRoot 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
    $env:HF_HUB_OFFLINE = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    $env:RAG_VECTOR_STORE = 'faiss'
    $env:RAG_INGESTION_MODE = 'worker'
    $env:RAG_WORKER_ENABLED = 'false'
    $env:INFRASPINE_F01_CREDENTIAL_FIXTURE = $fixturePath
    if (-not $databaseAlreadyExists) {
        & $python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw 'Current-source Alembic upgrade failed; the new database has been retained for inspection.' }
    }
    & $python (Join-Path $PSScriptRoot 'seed_f01_workspace_review.py')
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic F01 Admin seeding failed; the new database and fixture have been retained.' }

    & (Join-Path $PSScriptRoot 'start_local_review_stack.ps1') `
        -PostgresPort $PostgresPort `
        -PostgresDataDirectory $dataDirectory `
        -DatabaseName $database `
        -ReviewRoot $reviewRoot `
        -CredentialFixturePath $fixturePath `
        -ApiPort $ApiPort `
        -FrontendPort $FrontendPort
    if ($LASTEXITCODE -ne 0) { throw 'Current-source local review stack startup failed; isolated data was retained.' }

    $healthy = $false
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 2
            $ui = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort/admin/" -TimeoutSec 2
            if ($health.StatusCode -eq 200 -and $ui.StatusCode -eq 200) { $healthy = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $healthy) { throw 'The new review stack did not pass both health checks within 45 attempts.' }

    $sourceFiles = @(
        'app\services\access_control.py',
        'app\api\routes\access_admin.py',
        'frontend\src\components\WorkspacesPage.tsx',
        'migrations\versions\0059_workspace_hierarchy.py',
        'tests\test_workspaces.py'
    )
    $sourceHashes = @{}
    foreach ($relative in $sourceFiles) { $sourceHashes[$relative] = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $repoRoot $relative)).Hash.ToLowerInvariant() }
    $state = [ordered]@{
        reviewUrl = "http://127.0.0.1:$FrontendPort/admin/"
        apiUrl = "http://127.0.0.1:$ApiPort"
        database = $database
        migration = '0059_workspace_hierarchy'
        gitHead = (& git -C $repoRoot rev-parse HEAD).Trim()
        sourceFilesSha256 = $sourceHashes
        clusterWasRunningBeforeReview = $clusterWasRunning
        startedAt = (Get-Date).ToString('o')
    }
    $state | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $reviewRoot 'stack-state.json') -Encoding utf8
    Write-Output 'F01_CURRENT_SOURCE_API_UI=HEALTHY'
    Write-Output "F01_REVIEW_URL=http://127.0.0.1:$FrontendPort/admin/"
    Write-Output "F01_API_HEALTH=http://127.0.0.1:$ApiPort/health/ready"
    Write-Output "F01_DATABASE=$database; migration=0059_workspace_hierarchy"
} finally {
    foreach ($name in @('DATABASE_URL','APP_ENV','LLM_MOCK_MODE','MASTER_ENCRYPTION_KEY','MASTER_ENCRYPTION_KEY_ID','SESSION_COOKIE_SECURE','RAG_DATA_DIR','RAG_MODELS_DIR','RAG_EMBEDDING_ALLOW_DOWNLOAD','RAG_EMBEDDING_MODEL','HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','RAG_VECTOR_STORE','RAG_INGESTION_MODE','RAG_WORKER_ENABLED','INFRASPINE_F01_CREDENTIAL_FIXTURE')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}
