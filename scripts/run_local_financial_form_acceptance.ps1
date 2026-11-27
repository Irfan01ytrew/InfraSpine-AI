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
$python = Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction Stop).Source

$uri = [Uri]$Url
if ($uri.Scheme -ne 'http' -or $uri.Host -notin @('127.0.0.1', 'localhost', '::1')) {
    throw 'The financial-form browser acceptance only permits loopback HTTP.'
}
if (!(Test-Path -LiteralPath $fixturePath -PathType Leaf)) { throw 'The protected synthetic review fixture is unavailable.' }
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw 'The repository verification Python environment is unavailable.' }
$fixtureAcl = Get-Acl -LiteralPath $fixturePath
if (-not $fixtureAcl.AreAccessRulesProtected) { throw 'The synthetic credentials file is not protected from inheritance.' }
$allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
$fixtureRules = @($fixtureAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
if (-not $fixtureRules -or @($fixtureRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
    throw 'The synthetic credentials ACL is broader than this user, SYSTEM, and local Administrators.'
}
$fixture = Get-Content -LiteralPath $fixturePath -Raw | ConvertFrom-Json
$admin = $fixture.admin
if ([string]::IsNullOrWhiteSpace([string]$admin.email) -or [string]::IsNullOrWhiteSpace([string]$admin.password)) {
    throw 'The protected synthetic Admin account is incomplete.'
}

$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$pdfPath = Join-Path $reviewRoot "financial-review-$stamp.pdf"
$env:INFRASPINE_FINANCIAL_REVIEW_PDF = $pdfPath
$env:INFRASPINE_FINANCIAL_REVIEW_ROOT = $reviewRoot
try {
    & $python (Join-Path $PSScriptRoot 'create_local_financial_review_pdf.py')
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic financial review PDF generation failed.' }
} finally {
    Remove-Item Env:INFRASPINE_FINANCIAL_REVIEW_PDF -ErrorAction SilentlyContinue
}

$env:INFRASPINE_FINANCIAL_REVIEW_URL = $Url
$env:INFRASPINE_FINANCIAL_REVIEW_PDF = $pdfPath
$env:INFRASPINE_FINANCIAL_REVIEW_ROOT = $reviewRoot
$env:INFRASPINE_FINANCIAL_REVIEW_ADMIN_EMAIL = [string]$admin.email
$env:INFRASPINE_FINANCIAL_REVIEW_ADMIN_PASSWORD = [string]$admin.password
try {
    & $node (Join-Path $PSScriptRoot 'browser_financial_form_acceptance.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Focused financial-form browser acceptance failed; inspect only redacted run results and protected synthetic logs.' }
} finally {
    foreach ($name in @(
        'INFRASPINE_FINANCIAL_REVIEW_URL', 'INFRASPINE_FINANCIAL_REVIEW_PDF', 'INFRASPINE_FINANCIAL_REVIEW_ROOT',
        'INFRASPINE_FINANCIAL_REVIEW_ADMIN_EMAIL', 'INFRASPINE_FINANCIAL_REVIEW_ADMIN_PASSWORD'
    )) { Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue }
    if ($null -ne $fixture) { $fixture.admin.password = $null }
    if ($null -ne $admin) { $admin.password = $null }
}

$fixtureAclAfter = Get-Acl -LiteralPath $fixturePath
if (-not $fixtureAclAfter.AreAccessRulesProtected) { throw 'The synthetic credentials file lost its protected ACL.' }
