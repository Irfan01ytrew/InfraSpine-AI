param(
    [string]$ReviewRoot = '',
    [string]$FixturePath = '',
    [string]$Url = 'http://127.0.0.1:5175/admin/'
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($ReviewRoot)) {
    $ReviewRoot = Join-Path $repoRoot '.verification-local-review-20261027-native'
} elseif (-not [IO.Path]::IsPathRooted($ReviewRoot)) {
    $ReviewRoot = Join-Path $repoRoot $ReviewRoot
}
$reviewRoot = [IO.Path]::GetFullPath($ReviewRoot)
if ([string]::IsNullOrWhiteSpace($FixturePath)) {
    $FixturePath = Join-Path $reviewRoot 'qa-users.json'
} elseif (-not [IO.Path]::IsPathRooted($FixturePath)) {
    $FixturePath = Join-Path $repoRoot $FixturePath
}
$fixturePath = [IO.Path]::GetFullPath($FixturePath)
$node = (Get-Command node.exe -ErrorAction Stop).Source

if (!(Test-Path -LiteralPath $fixturePath -PathType Leaf)) { throw 'The protected synthetic review fixture is unavailable.' }
$fixtureAcl = Get-Acl -LiteralPath $fixturePath
if (-not $fixtureAcl.AreAccessRulesProtected) { throw 'The synthetic credentials file is not protected from inheritance.' }
$allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
$fixtureRules = @($fixtureAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
if (-not $fixtureRules -or @($fixtureRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
    throw 'The synthetic credentials ACL is broader than this user, SYSTEM, and local Administrators.'
}
$fixture = Get-Content -LiteralPath $fixturePath -Raw | ConvertFrom-Json
$admin = $fixture.admin
$manager = @($fixture.managers | Where-Object { $_.role -eq 'manager' } | Select-Object -Last 1)
if ([string]::IsNullOrWhiteSpace([string]$admin.email) -or [string]::IsNullOrWhiteSpace([string]$admin.password) -or
    -not $manager -or [string]::IsNullOrWhiteSpace([string]$manager[0].email) -or [string]::IsNullOrWhiteSpace([string]$manager[0].password)) {
    throw 'The protected Admin/Manager synthetic accounts are incomplete.'
}

$env:INFRASPINE_PROPOSAL_REVIEW_URL = $Url
$env:INFRASPINE_PROPOSAL_REVIEW_ROOT = $reviewRoot
$env:INFRASPINE_PROPOSAL_REVIEW_ADMIN_EMAIL = [string]$admin.email
$env:INFRASPINE_PROPOSAL_REVIEW_ADMIN_PASSWORD = [string]$admin.password
$env:INFRASPINE_PROPOSAL_REVIEW_MANAGER_EMAIL = [string]$manager[0].email
$env:INFRASPINE_PROPOSAL_REVIEW_MANAGER_PASSWORD = [string]$manager[0].password
try {
    & $node (Join-Path $PSScriptRoot 'browser_proposal_coverage_acceptance.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Focused proposal coverage browser acceptance failed; inspect only the redacted run result and protected synthetic logs.' }
} finally {
    foreach ($name in @(
        'INFRASPINE_PROPOSAL_REVIEW_URL', 'INFRASPINE_PROPOSAL_REVIEW_ROOT',
        'INFRASPINE_PROPOSAL_REVIEW_ADMIN_EMAIL', 'INFRASPINE_PROPOSAL_REVIEW_ADMIN_PASSWORD',
        'INFRASPINE_PROPOSAL_REVIEW_MANAGER_EMAIL', 'INFRASPINE_PROPOSAL_REVIEW_MANAGER_PASSWORD'
    )) { Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue }
    if ($null -ne $fixture) { $fixture.admin.password = $null }
    if ($null -ne $admin) { $admin.password = $null }
    if ($null -ne $manager -and $manager.Count -gt 0) { $manager[0].password = $null }
}

$fixtureAclAfter = Get-Acl -LiteralPath $fixturePath
if (-not $fixtureAclAfter.AreAccessRulesProtected) { throw 'The synthetic credentials file lost its protected ACL.' }
