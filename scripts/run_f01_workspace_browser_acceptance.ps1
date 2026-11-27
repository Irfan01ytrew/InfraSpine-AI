param([string]$ReviewRoot = '')

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($ReviewRoot)) {
    $ReviewRoot = Join-Path $repoRoot '.verification-local-review-20261028-f01-workspaces'
} elseif (-not [IO.Path]::IsPathRooted($ReviewRoot)) {
    $ReviewRoot = Join-Path $repoRoot $ReviewRoot
}
$reviewRoot = [IO.Path]::GetFullPath($ReviewRoot)
$fixturePath = Join-Path $reviewRoot 'review-credentials.json'
$statePath = Join-Path $reviewRoot 'stack-state.json'
if (-not (Test-Path -LiteralPath $fixturePath -PathType Leaf) -or -not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    throw 'The protected F01 synthetic review fixture and current-source stack state are required.'
}
$fixtureAcl = Get-Acl -LiteralPath $fixturePath
if (-not $fixtureAcl.AreAccessRulesProtected) { throw 'The F01 synthetic credential fixture ACL is not protected.' }
$allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
$fixtureRules = @($fixtureAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
if (-not $fixtureRules -or @($fixtureRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
    throw 'The F01 synthetic fixture ACL is broader than the current user, SYSTEM, and local Administrators.'
}
$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if ($state.reviewUrl -ne 'http://127.0.0.1:5177/admin/' -or $state.database -ne 'infraspine_f01workspace_20261028_test') {
    throw 'The browser runner is pinned to the F01 isolated current-source review stack.'
}
$apiHealth = Invoke-WebRequest -UseBasicParsing -Uri "$($state.apiUrl)/health/ready" -TimeoutSec 5
$uiHealth = Invoke-WebRequest -UseBasicParsing -Uri $state.reviewUrl -TimeoutSec 5
if ($apiHealth.StatusCode -ne 200 -or $uiHealth.StatusCode -ne 200) { throw 'The F01 API/UI health checks did not pass.' }

$node = (Get-Command node.exe -ErrorAction Stop).Source
$env:INFRASPINE_F01_REVIEW_URL = $state.reviewUrl
$env:INFRASPINE_F01_CREDENTIAL_FIXTURE = $fixturePath
$env:INFRASPINE_F01_REVIEW_ROOT = $reviewRoot
try {
    & $node (Join-Path $PSScriptRoot 'browser_f01_workspace_hierarchy_acceptance.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'F01 current-source workspace browser acceptance failed.' }
} finally {
    foreach ($name in @('INFRASPINE_F01_REVIEW_URL','INFRASPINE_F01_CREDENTIAL_FIXTURE','INFRASPINE_F01_REVIEW_ROOT')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}
