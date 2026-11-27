param(
    [string]$ReviewRoot = '.verification-local-review-20261027-native',
    [string]$FixturePath = '',
    [string]$Url = 'http://127.0.0.1:5175/admin/',
    [ValidateRange(1024, 65535)][int]$PostgresPort = 55621,
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
if ($uri.Scheme -ne 'http' -or $uri.Host -notin @('127.0.0.1', 'localhost', '::1')) { throw 'F10 review acceptance permits loopback HTTP only.' }
if ($DatabaseName -ne 'infraspine_ai_native_review') { throw 'F10 review acceptance is restricted to the named disposable synthetic database.' }
if (!(Test-Path -LiteralPath $fixturePath -PathType Leaf)) { throw 'The protected synthetic QA role fixture is unavailable.' }
$fixtureAcl = Get-Acl -LiteralPath $fixturePath
if (-not $fixtureAcl.AreAccessRulesProtected) { throw 'The synthetic role fixture is not protected from ACL inheritance.' }
$allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
$fixtureRules = @($fixtureAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
if (-not $fixtureRules -or @($fixtureRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
    throw 'The synthetic role fixture ACL is broader than this user, SYSTEM and local Administrators.'
}
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw 'The repository verification Python environment is unavailable.' }
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $PostgresPort -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -eq '127.0.0.1' })
if (-not $listeners) { throw 'The existing isolated loopback PostgreSQL listener is unavailable; no cluster was started.' }
$env:INFRASPINE_F10_CURRENT_URL = $Url
$env:INFRASPINE_F10_FIXTURE = $fixturePath
$env:INFRASPINE_F10_REVIEW_ROOT = $reviewRoot
$env:INFRASPINE_F10_PYTHON = $python
$env:DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$DatabaseName"
try {
    & $node (Join-Path $PSScriptRoot 'browser_f10_current_review_acceptance.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Current-source F10 reminder browser acceptance failed; no live data or Docker recovery was touched.' }
} finally {
    foreach ($name in @('INFRASPINE_F10_CURRENT_URL', 'INFRASPINE_F10_FIXTURE', 'INFRASPINE_F10_REVIEW_ROOT', 'INFRASPINE_F10_PYTHON', 'DATABASE_URL')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}
