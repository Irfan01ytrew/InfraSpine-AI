param(
    [string]$ReviewRoot = '.verification-local-review-20261027-native',
    [string]$FixturePath = '',
    [string]$Url = 'http://127.0.0.1:5175/admin/',
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
$uri = [Uri]$Url
if ($uri.Scheme -ne 'http' -or $uri.Host -notin @('127.0.0.1', 'localhost', '::1')) { throw 'F17 acceptance permits loopback HTTP only.' }
if ($DatabaseName -ne 'infraspine_ai_native_review' -or $PostgresPort -ne 55621 -or $ApiPort -ne 63004 -or $FrontendPort -ne 5175) {
    throw 'F17 acceptance is restricted to the existing synthetic review database and loopback ports.'
}
if (!(Test-Path -LiteralPath $fixturePath -PathType Leaf)) { throw 'The protected synthetic QA role fixture is unavailable.' }
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
$node = (Get-Command node.exe -ErrorAction Stop).Source
foreach ($required in @($postgresData, $pgCtl, $psql, $python)) {
    if (!(Test-Path -LiteralPath $required)) { throw 'An existing isolated PostgreSQL or repository verification dependency is unavailable.' }
}
& $pgCtl status -D $postgresData *> $null
if ($LASTEXITCODE -ne 0) { throw 'The existing isolated PostgreSQL cluster is not running; this runner does not start or create clusters.' }
foreach ($port in @($PostgresPort, $ApiPort, $FrontendPort)) {
    if (-not (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -eq '127.0.0.1' })) {
        throw "The existing loopback service on port $port is unavailable; no service was started."
    }
}
try {
    $apiHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 10
    if ($apiHealth.status -ne 'ready') { throw 'The current-source API is not ready.' }
    $frontResponse = Invoke-WebRequest -Uri $Url -TimeoutSec 10
    if ([int]$frontResponse.StatusCode -ne 200) { throw 'The review UI did not return HTTP 200.' }
} catch { throw 'The existing synthetic review app failed its read-only readiness check.' }

$migration = & $psql "postgresql://postgres@127.0.0.1:$PostgresPort/$DatabaseName" -X -v ON_ERROR_STOP=1 -A -t -c "SELECT current_database() || '|' || version_num FROM alembic_version"
if ($LASTEXITCODE -ne 0 -or -not ($migration -match '^infraspine_ai_native_review\|0052_user_totp_mfa$')) {
    throw 'The existing synthetic review database is not at the expected migration 0052; no migrations were applied.'
}

$qaTemp = Join-Path $reviewRoot 'qa-temp'
New-Item -ItemType Directory -Path $qaTemp -Force | Out-Null
$policyPdf = Join-Path $qaTemp 'f17-policy-source-current.pdf'
if (!(Test-Path -LiteralPath $policyPdf -PathType Leaf)) {
    $env:INFRASPINE_F17_POLICY_PDF = $policyPdf
    try {
        & $python (Join-Path $PSScriptRoot 'create_local_f17_policy_pdf.py')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the one-page synthetic F17 source.' }
    } finally { Remove-Item Env:INFRASPINE_F17_POLICY_PDF -ErrorAction SilentlyContinue }
}

$evidenceRoot = Join-Path $reviewRoot 'f17-current-review-evidence'
$runId = Get-Date -Format 'yyyyMMddTHHmmssfff'
$statePath = Join-Path $evidenceRoot ("f17-state-$runId.json")
$env:INFRASPINE_F17_URL = $Url
$env:INFRASPINE_F17_FIXTURE = $fixturePath
$env:INFRASPINE_F17_ROOT = $reviewRoot
$env:INFRASPINE_F17_PDF = $policyPdf
$env:INFRASPINE_F17_STATE = $statePath
try {
    & $node (Join-Path $PSScriptRoot 'browser_f17_current_review_acceptance.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'F17 authenticated browser acceptance failed; only the synthetic local review records were involved.' }
    if (!(Test-Path -LiteralPath $statePath -PathType Leaf)) { throw 'F17 browser evidence state was not written.' }
    $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    foreach ($id in @($state.exception_id, $state.process_id, $state.policy_entry_id)) {
        $parsed = [guid]::Empty
        if (-not [guid]::TryParse([string]$id, [ref]$parsed)) { throw 'F17 persisted-state evidence contained an invalid record identifier.' }
    }
    $sql = "SELECT status || '|' || (SELECT count(*) FROM knowledge_policy_exception_events e WHERE e.exception_id = x.id)::text FROM knowledge_policy_exceptions x WHERE x.id = '$($state.exception_id)'"
    $dbRow = & $psql "postgresql://postgres@127.0.0.1:$PostgresPort/$DatabaseName" -X -v ON_ERROR_STOP=1 -A -t -c $sql
    if ($LASTEXITCODE -ne 0 -or $dbRow.Trim() -ne 'resolved|4') { throw 'F17 final PostgreSQL status/event readback did not match the resolved four-event lifecycle.' }
    Write-Output 'LOCAL_F17_POSTGRES_READBACK=PASS status=resolved event_count=4'
    Write-Output "F17 evidence: $evidenceRoot"
} finally {
    foreach ($name in @('INFRASPINE_F17_URL', 'INFRASPINE_F17_FIXTURE', 'INFRASPINE_F17_ROOT', 'INFRASPINE_F17_PDF', 'INFRASPINE_F17_STATE')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}
