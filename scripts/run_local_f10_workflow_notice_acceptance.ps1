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
if ($uri.Scheme -ne 'http' -or $uri.Host -notin @('127.0.0.1', 'localhost', '::1')) { throw 'F10 notice acceptance permits loopback HTTP only.' }
if ($DatabaseName -ne 'infraspine_ai_native_review' -or $PostgresPort -ne 55621 -or $ApiPort -ne 63004 -or $FrontendPort -ne 5175) {
    throw 'F10 notice acceptance is restricted to the existing synthetic review database and loopback ports.'
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
$node = (Get-Command node.exe -ErrorAction Stop).Source
foreach ($required in @($postgresData, $pgCtl, $psql)) {
    if (!(Test-Path -LiteralPath $required)) { throw 'The retained synthetic PostgreSQL runtime is unavailable.' }
}
& $pgCtl status -D $postgresData *> $null
if ($LASTEXITCODE -ne 0) { throw 'The retained PostgreSQL cluster is not running; this acceptance runner never creates or starts clusters.' }
foreach ($port in @($PostgresPort, $ApiPort, $FrontendPort)) {
    if (-not (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -eq '127.0.0.1' })) {
        throw "The existing loopback service on port $port is unavailable; no service was started."
    }
}
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 10
    if ($health.status -ne 'ready') { throw 'The current-source API is not ready.' }
    $page = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 10
    if ([int]$page.StatusCode -ne 200) { throw 'The review UI did not return HTTP 200.' }
} catch { throw 'The existing synthetic review app failed its read-only readiness check.' }
$migration = & $psql "postgresql://postgres@127.0.0.1:$PostgresPort/$DatabaseName" -X -v ON_ERROR_STOP=1 -A -t -c "SELECT current_database() || '|' || version_num FROM alembic_version"
if ($LASTEXITCODE -ne 0 -or -not ($migration -match '^infraspine_ai_native_review\|0052_user_totp_mfa$')) {
    throw 'The retained synthetic review database is not at migration 0052; no migration was run.'
}

$evidenceRoot = Join-Path $reviewRoot 'f10-workflow-notice-evidence'
$runId = Get-Date -Format 'yyyyMMddTHHmmssfff'
$statePath = Join-Path $evidenceRoot ("f10-workflow-notice-state-$runId.json")
$env:INFRASPINE_F10_NOTICE_URL = $Url
$env:INFRASPINE_F10_NOTICE_FIXTURE = $fixturePath
$env:INFRASPINE_F10_NOTICE_ROOT = $reviewRoot
$env:INFRASPINE_F10_NOTICE_STATE = $statePath
try {
    & $node (Join-Path $PSScriptRoot 'browser_f10_workflow_notice_acceptance.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Current-source F10 workflow notice acceptance failed; only synthetic loopback records were involved.' }
    if (!(Test-Path -LiteralPath $statePath -PathType Leaf)) { throw 'F10 workflow notification state evidence was not written.' }
    $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    $uuidFields = @('run_id', 'approval_notification_id', 'manager_notification_id', 'employee_notification_id', 'task_id')
    foreach ($field in $uuidFields) {
        $parsed = [guid]::Empty
        if (-not [guid]::TryParse([string]$state.$field, [ref]$parsed)) { throw "F10 state evidence has an invalid $field." }
    }
    $ids = @($state.approval_notification_id, $state.manager_notification_id, $state.employee_notification_id) -join "','"
    $sql = "SELECT notification_type || '|' || (read_at IS NOT NULL)::text FROM workflow_notifications WHERE id IN ('$ids') ORDER BY notification_type"
    $rows = @(& $psql "postgresql://postgres@127.0.0.1:$PostgresPort/$DatabaseName" -X -v ON_ERROR_STOP=1 -A -t -c $sql)
    if ($LASTEXITCODE -ne 0) { throw 'F10 notification read-receipt PostgreSQL readback failed.' }
    $actual = @($rows | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $expected = @('approval_required|true', 'run_approved|true', 'task_assigned|true')
    if ($actual.Count -ne $expected.Count -or (@($actual | Sort-Object) -join ',') -ne (@($expected | Sort-Object) -join ',')) {
        throw 'F10 PostgreSQL readback did not show all three expected notices with persisted read receipts.'
    }
    $runRow = & $psql "postgresql://postgres@127.0.0.1:$PostgresPort/$DatabaseName" -X -v ON_ERROR_STOP=1 -A -t -c "SELECT r.status || '|' || t.status FROM automation_runs r JOIN automation_tasks t ON t.run_id = r.id WHERE r.id = '$($state.run_id)'"
    if ($LASTEXITCODE -ne 0 -or $runRow.Trim() -ne 'completed|open') { throw 'F10 approved run/task state did not match PostgreSQL.' }
    Write-Output 'LOCAL_F10_POSTGRES_APPROVAL_AND_READ_RECEIPTS=PASS approval_required|run_approved|task_assigned all_read=true'
    Write-Output 'LOCAL_F10_POSTGRES_APPROVED_RUN_TASK=PASS run=completed task=open'
    Write-Output "F10 workflow notice evidence: $evidenceRoot"
} finally {
    foreach ($name in @('INFRASPINE_F10_NOTICE_URL', 'INFRASPINE_F10_NOTICE_FIXTURE', 'INFRASPINE_F10_NOTICE_ROOT', 'INFRASPINE_F10_NOTICE_STATE')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}
