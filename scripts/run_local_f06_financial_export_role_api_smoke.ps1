param(
    [string]$ReviewRoot = '.verification-local-review-20261027-native',
    [string]$FixturePath = '',
    [ValidateRange(1024, 65535)][int]$PostgresPort = 55621,
    [ValidateRange(1024, 65535)][int]$ApiPort = 63004,
    [ValidateRange(1024, 65535)][int]$FrontendPort = 5175,
    [string]$DatabaseName = 'infraspine_ai_native_review'
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not [IO.Path]::IsPathRooted($ReviewRoot)) { $ReviewRoot = Join-Path $repoRoot $ReviewRoot }
$reviewRoot = [IO.Path]::GetFullPath($ReviewRoot)
if ([string]::IsNullOrWhiteSpace($FixturePath)) { $FixturePath = Join-Path $reviewRoot 'qa-users.json' }
elseif (-not [IO.Path]::IsPathRooted($FixturePath)) { $FixturePath = Join-Path $repoRoot $FixturePath }
$fixturePath = [IO.Path]::GetFullPath($FixturePath)
if ($DatabaseName -ne 'infraspine_ai_native_review' -or $PostgresPort -ne 55621 -or $ApiPort -ne 63004 -or $FrontendPort -ne 5175) {
    throw 'F06 role smoke is restricted to the existing synthetic loopback review target.'
}
if (!(Test-Path -LiteralPath $fixturePath -PathType Leaf)) { throw 'The protected synthetic role fixture is unavailable.' }
$fixtureAcl = Get-Acl -LiteralPath $fixturePath
if (-not $fixtureAcl.AreAccessRulesProtected) { throw 'The synthetic role fixture ACL is not protected from inheritance.' }
$allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
$fixtureRules = @($fixtureAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
if (-not $fixtureRules -or @($fixtureRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
    throw 'The synthetic role fixture ACL is broader than this user, SYSTEM and local Administrators.'
}

$postgresData = Join-Path $repoRoot '.verification-voice-native-pg-20261027\data'
$pgCtl = 'C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe'
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
foreach ($required in @($postgresData, $pgCtl, $psql, $python)) {
    if (!(Test-Path -LiteralPath $required)) { throw 'The retained synthetic PostgreSQL or Python 3.12 runtime is unavailable.' }
}
& $pgCtl status -D $postgresData *> $null
if ($LASTEXITCODE -ne 0) { throw 'The retained PostgreSQL cluster is not running; this runner never starts clusters.' }
foreach ($port in @($PostgresPort, $ApiPort, $FrontendPort)) {
    if (-not (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -eq '127.0.0.1' })) {
        throw "The existing loopback service on port $port is unavailable; no service was started."
    }
}
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 10
    if ($health.status -ne 'ready') { throw 'The current synthetic API is not ready.' }
    $page = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort/admin/" -TimeoutSec 10
    if ([int]$page.StatusCode -ne 200) { throw 'The current synthetic UI is not ready.' }
} catch { throw 'The existing synthetic review stack failed its read-only preflight.' }
$migration = & $psql "postgresql://postgres@127.0.0.1:$PostgresPort/$DatabaseName" -X -v ON_ERROR_STOP=1 -A -t -c "SELECT current_database() || '|' || version_num FROM alembic_version"
if ($LASTEXITCODE -ne 0 -or -not ($migration -match '^infraspine_ai_native_review\|0052_user_totp_mfa$')) {
    throw 'The existing synthetic review database is not at migration 0052; no migration was run.'
}

$evidenceRoot = Join-Path $reviewRoot 'f06-financial-export-role-evidence'
$statePath = Join-Path $evidenceRoot ("f06-role-api-smoke-$(Get-Date -Format 'yyyyMMddTHHmmssfff').json")
$names = @('DATABASE_URL', 'APP_ENV', 'MASTER_ENCRYPTION_KEY', 'MASTER_ENCRYPTION_KEY_ID', 'SESSION_COOKIE_SECURE',
          'LLM_MOCK_MODE', 'RAG_WORKER_ENABLED', 'LOG_DIR', 'RAG_DATA_DIR', 'INFRASPINE_F06_ROLE_FIXTURE', 'INFRASPINE_F06_ROLE_EVIDENCE')
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    # Explicit passwordless loopback DSN for the synthetic PostgreSQL cluster; never read or use .env DATABASE_URL.
    $env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$DatabaseName"
    $env:APP_ENV = 'testing'
    $env:MASTER_ENCRYPTION_KEY = [Convert]::ToBase64String([byte[]](65..96))
    $env:MASTER_ENCRYPTION_KEY_ID = 'f06-smoke'
    $env:SESSION_COOKIE_SECURE = 'false'
    $env:LLM_MOCK_MODE = 'true'
    $env:RAG_WORKER_ENABLED = 'false'
    $env:LOG_DIR = ''
    $env:RAG_DATA_DIR = Join-Path $evidenceRoot 'isolated-unused-rag-data'
    $env:INFRASPINE_F06_ROLE_FIXTURE = $fixturePath
    $env:INFRASPINE_F06_ROLE_EVIDENCE = $statePath
    & $python -B -m scripts.f06_financial_export_role_api_smoke
    if ($LASTEXITCODE -ne 0) { throw 'Current-source F06 API role smoke failed; the existing review database was not migrated or fixture-reset.' }
    if (!(Test-Path -LiteralPath $statePath -PathType Leaf)) { throw 'F06 safe role-smoke evidence file was not written.' }
    Write-Output "F06 role API evidence: $evidenceRoot"
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
}
