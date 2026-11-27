$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$fixturePath = Join-Path $repoRoot '.verification-local-review-20261026\qa-users.json'
$browserScript = Join-Path $PSScriptRoot 'browser_mfa_acceptance.mjs'
$node = (Get-Command node.exe -ErrorAction Stop).Source
if (-not (Test-Path -LiteralPath $fixturePath -PathType Leaf)) { throw 'The protected synthetic Admin fixture is unavailable.' }

$acl = Get-Acl -LiteralPath $fixturePath
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$allowedSids = @($currentSid, 'S-1-5-18', 'S-1-5-32-544')
$rules = @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
if (-not $acl.AreAccessRulesProtected -or -not $rules -or
    @($rules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
    throw 'The synthetic Admin fixture ACL is broader than the current user, SYSTEM, and local Administrators.'
}

$fixture = Get-Content -LiteralPath $fixturePath -Raw | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace([string]$fixture.admin.email) -or
    [string]::IsNullOrWhiteSpace([string]$fixture.admin.password)) {
    throw 'The protected synthetic Admin fixture is incomplete.'
}

$env:INFRASPINE_MFA_REVIEW_EMAIL = [string]$fixture.admin.email
$env:INFRASPINE_MFA_REVIEW_PASSWORD = [string]$fixture.admin.password
$env:INFRASPINE_MFA_REVIEW_URL = 'http://127.0.0.1:5174/admin/'
try {
    & $node $browserScript
    if ($LASTEXITCODE -ne 0) { throw 'Current-source MFA browser acceptance failed; review only the redacted status output.' }
}
finally {
    foreach ($name in @('INFRASPINE_MFA_REVIEW_EMAIL', 'INFRASPINE_MFA_REVIEW_PASSWORD', 'INFRASPINE_MFA_REVIEW_URL')) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
}

$aclAfter = Get-Acl -LiteralPath $fixturePath
if (-not $aclAfter.AreAccessRulesProtected) { throw 'The synthetic Admin fixture ACL lost protection during browser acceptance.' }
