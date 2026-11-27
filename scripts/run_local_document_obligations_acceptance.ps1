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
    throw 'The obligation browser acceptance only permits loopback HTTP.'
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
$employee = @($fixture.employees | Where-Object { $_.role -eq 'employee' } | Select-Object -Last 1)
if ([string]::IsNullOrWhiteSpace([string]$admin.email) -or [string]::IsNullOrWhiteSpace([string]$admin.password) -or
    -not $employee -or [string]::IsNullOrWhiteSpace([string]$employee[0].email) -or [string]::IsNullOrWhiteSpace([string]$employee[0].password)) {
    throw 'The protected synthetic Admin/Employee accounts are incomplete.'
}

$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
if ($ResumeLatest) {
    $latestV1 = Get-ChildItem -LiteralPath $reviewRoot -Filter 'obligation-review-*-v1.pdf' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latestV1) { throw 'No prior synthetic obligation-review contract is available to resume.' }
    $v1Pdf = $latestV1.FullName
    $v2Pdf = $v1Pdf -replace '-v1\.pdf$', '-v2.pdf'
    if (!(Test-Path -LiteralPath $v2Pdf -PathType Leaf)) { throw 'The prior synthetic obligation-review replacement PDF is unavailable.' }
} else {
    $v1Pdf = Join-Path $reviewRoot "obligation-review-$stamp-v1.pdf"
    $v2Pdf = Join-Path $reviewRoot "obligation-review-$stamp-v2.pdf"
    $env:INFRASPINE_OBLIGATION_REVIEW_V1_PDF = $v1Pdf
    $env:INFRASPINE_OBLIGATION_REVIEW_V2_PDF = $v2Pdf
    try {
        & $python (Join-Path $PSScriptRoot 'create_local_obligation_review_pdfs.py')
        if ($LASTEXITCODE -ne 0) { throw 'Synthetic obligation review PDF generation failed.' }
    } finally {
        Remove-Item Env:INFRASPINE_OBLIGATION_REVIEW_V1_PDF,Env:INFRASPINE_OBLIGATION_REVIEW_V2_PDF -ErrorAction SilentlyContinue
    }
}

$env:INFRASPINE_OBLIGATION_REVIEW_URL = $Url
$env:INFRASPINE_OBLIGATION_REVIEW_ROOT = $reviewRoot
$env:INFRASPINE_OBLIGATION_REVIEW_V1_PDF = $v1Pdf
$env:INFRASPINE_OBLIGATION_REVIEW_V2_PDF = $v2Pdf
$env:INFRASPINE_OBLIGATION_REVIEW_ADMIN_EMAIL = [string]$admin.email
$env:INFRASPINE_OBLIGATION_REVIEW_ADMIN_PASSWORD = [string]$admin.password
$env:INFRASPINE_OBLIGATION_REVIEW_EMPLOYEE_EMAIL = [string]$employee[0].email
$env:INFRASPINE_OBLIGATION_REVIEW_EMPLOYEE_PASSWORD = [string]$employee[0].password
try {
    & $node (Join-Path $PSScriptRoot 'browser_document_obligations_acceptance.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Focused document-obligation browser acceptance failed; inspect only redacted run results and protected synthetic logs.' }
} finally {
    foreach ($name in @(
        'INFRASPINE_OBLIGATION_REVIEW_URL', 'INFRASPINE_OBLIGATION_REVIEW_ROOT',
        'INFRASPINE_OBLIGATION_REVIEW_V1_PDF', 'INFRASPINE_OBLIGATION_REVIEW_V2_PDF',
        'INFRASPINE_OBLIGATION_REVIEW_ADMIN_EMAIL', 'INFRASPINE_OBLIGATION_REVIEW_ADMIN_PASSWORD',
        'INFRASPINE_OBLIGATION_REVIEW_EMPLOYEE_EMAIL', 'INFRASPINE_OBLIGATION_REVIEW_EMPLOYEE_PASSWORD'
    )) { Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue }
    if ($null -ne $fixture) {
        $fixture.admin.password = $null
        foreach ($account in @($fixture.employees)) { $account.password = $null }
    }
    if ($null -ne $admin) { $admin.password = $null }
    if ($null -ne $employee -and $employee.Count -gt 0) { $employee[0].password = $null }
}

$fixtureAclAfter = Get-Acl -LiteralPath $fixturePath
if (-not $fixtureAclAfter.AreAccessRulesProtected) { throw 'The synthetic credentials file lost its protected ACL.' }
