param(
    [string]$ReviewRoot = '',
    [string]$FixturePath = '',
    [string]$Url = 'http://127.0.0.1:5175/admin/',
    [switch]$ResumeLatest
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
if ($ResumeLatest) {
    $latestPo = Get-ChildItem -LiteralPath $reviewRoot -Filter 'financial-po-*.pdf' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latestPo) { throw 'No prior synthetic purchase-order PDF is available to resume.' }
    $poPdf = $latestPo.FullName
    if ($poPdf -notmatch 'financial-po-(.+)\.pdf$') { throw 'The prior synthetic purchase-order filename is invalid.' }
    $formPdf = Join-Path $reviewRoot "financial-form-$($Matches[1]).pdf"
    if (!(Test-Path -LiteralPath $formPdf -PathType Leaf)) { throw 'The paired synthetic generic-form PDF is unavailable.' }
} else {
    $poPdf = Join-Path $reviewRoot "financial-po-$stamp.pdf"
    $formPdf = Join-Path $reviewRoot "financial-form-$stamp.pdf"
    $env:INFRASPINE_FINANCIAL_PO_PDF = $poPdf
    $env:INFRASPINE_FINANCIAL_GENERIC_FORM_PDF = $formPdf
    try {
        & $python (Join-Path $PSScriptRoot 'create_local_financial_additional_forms_pdfs.py')
        if ($LASTEXITCODE -ne 0) { throw 'Synthetic purchase-order and generic-form PDF generation failed.' }
    } finally {
        Remove-Item Env:INFRASPINE_FINANCIAL_PO_PDF,Env:INFRASPINE_FINANCIAL_GENERIC_FORM_PDF -ErrorAction SilentlyContinue
    }
}

$env:INFRASPINE_FINANCIAL_REVIEW_URL = $Url
$env:INFRASPINE_FINANCIAL_REVIEW_ROOT = $reviewRoot
$env:INFRASPINE_FINANCIAL_PO_PDF = $poPdf
$env:INFRASPINE_FINANCIAL_GENERIC_FORM_PDF = $formPdf
$env:INFRASPINE_FINANCIAL_REVIEW_ADMIN_EMAIL = [string]$admin.email
$env:INFRASPINE_FINANCIAL_REVIEW_ADMIN_PASSWORD = [string]$admin.password
try {
    & $node (Join-Path $PSScriptRoot 'browser_financial_additional_forms_acceptance.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Focused additional financial-form browser acceptance failed; inspect only redacted run results and protected synthetic logs.' }
} finally {
    foreach ($name in @(
        'INFRASPINE_FINANCIAL_REVIEW_URL', 'INFRASPINE_FINANCIAL_REVIEW_ROOT',
        'INFRASPINE_FINANCIAL_PO_PDF', 'INFRASPINE_FINANCIAL_GENERIC_FORM_PDF',
        'INFRASPINE_FINANCIAL_REVIEW_ADMIN_EMAIL', 'INFRASPINE_FINANCIAL_REVIEW_ADMIN_PASSWORD'
    )) { Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue }
    if ($null -ne $fixture) { $fixture.admin.password = $null }
    if ($null -ne $admin) { $admin.password = $null }
}

$fixtureAclAfter = Get-Acl -LiteralPath $fixturePath
if (-not $fixtureAclAfter.AreAccessRulesProtected) { throw 'The synthetic credentials file lost its protected ACL.' }
