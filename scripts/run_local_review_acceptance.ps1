param(
    [string]$ReviewRoot = '',
    [string]$FixturePath = '',
    [string]$Url = 'http://127.0.0.1:5173/admin/'
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($ReviewRoot)) {
    $ReviewRoot = Join-Path $repoRoot '.verification-local-review-20261026'
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
$browserCandidates = @(
    'C:\Program Files\Google\Chrome\Application\chrome.exe',
    'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
    (Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe')
)
$browser = $browserCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $browser) { throw 'Chrome or Edge is unavailable for the local review.' }
if (-not (Test-Path -LiteralPath $fixturePath -PathType Leaf)) { throw 'The protected synthetic Admin fixture is unavailable.' }

$fixtureAcl = Get-Acl -LiteralPath $fixturePath
if (-not $fixtureAcl.AreAccessRulesProtected) { throw 'The synthetic credentials file is not protected from inheritance.' }
$allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
$fixtureRules = @($fixtureAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
if (-not $fixtureRules -or @($fixtureRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
    throw 'The synthetic credentials file ACL is broader than this user, SYSTEM, and local Administrators.'
}

$fixture = Get-Content -LiteralPath $fixturePath -Raw | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace([string]$fixture.admin.email) -or [string]::IsNullOrWhiteSpace([string]$fixture.admin.password)) {
    throw 'The protected synthetic Admin account is incomplete.'
}
$stackStatePath = Join-Path $reviewRoot 'stack-state.json'
if (Test-Path -LiteralPath $stackStatePath) {
    $stackState = Get-Content -LiteralPath $stackStatePath -Raw | ConvertFrom-Json
    if ($stackState.reviewUrl -ne $Url) { throw 'The requested URL does not match the saved current-source stack state.' }
}

$stamp = Get-Date -Format 'yyyyMMddTHHmmssfff'
$profile = Join-Path $reviewRoot "chrome-profile-$stamp"
$pdfPath = Join-Path $reviewRoot "review-evidence-$stamp.pdf"
$env:INFRASPINE_LOCAL_REVIEW_ROOT = $reviewRoot
$env:INFRASPINE_LOCAL_REVIEW_PROFILE = $profile
$env:INFRASPINE_LOCAL_REVIEW_PDF = $pdfPath
$env:INFRASPINE_LOCAL_REVIEW_FIXTURE = $fixturePath
$env:INFRASPINE_LOCAL_REVIEW_ADMIN_EMAIL = [string]$fixture.admin.email
$env:INFRASPINE_LOCAL_REVIEW_ADMIN_PASSWORD = [string]$fixture.admin.password
$env:INFRASPINE_LOCAL_REVIEW_URL = $Url
if ($null -ne $stackState) {
    $env:INFRASPINE_LOCAL_REVIEW_DATABASE = [string]$stackState.database
    $env:INFRASPINE_LOCAL_REVIEW_MIGRATION = [string]$stackState.migration
    $env:INFRASPINE_LOCAL_REVIEW_SOURCE_HEAD = [string]$stackState.gitHead
}

$failed = $false
try {
    & $python (Join-Path $PSScriptRoot 'create_local_review_pdf.py')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the new synthetic PDF acceptance fixture.' }
    & $node (Join-Path $PSScriptRoot 'browser_local_review_acceptance.mjs')
    if ($LASTEXITCODE -ne 0) { $failed = $true }
}
finally {
    foreach ($name in @(
        'INFRASPINE_LOCAL_REVIEW_ADMIN_EMAIL', 'INFRASPINE_LOCAL_REVIEW_ADMIN_PASSWORD',
        'INFRASPINE_LOCAL_REVIEW_FIXTURE', 'INFRASPINE_LOCAL_REVIEW_PDF',
        'INFRASPINE_LOCAL_REVIEW_PROFILE', 'INFRASPINE_LOCAL_REVIEW_ROOT', 'INFRASPINE_LOCAL_REVIEW_URL',
        'INFRASPINE_LOCAL_REVIEW_DATABASE', 'INFRASPINE_LOCAL_REVIEW_MIGRATION', 'INFRASPINE_LOCAL_REVIEW_SOURCE_HEAD'
    )) { Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue }
}
if ($failed) { throw 'The current-source local review browser acceptance failed; inspect the redacted step output and review logs.' }

$fixtureAclAfter = Get-Acl -LiteralPath $fixturePath
if (-not $fixtureAclAfter.AreAccessRulesProtected) { throw 'The credentials file lost its protected ACL during acceptance.' }

# Reopen the accepted synthetic session without a debugging endpoint so the owner can inspect it safely.
$chrome = Start-Process -FilePath $browser -ArgumentList @(
    "--user-data-dir=$profile", '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    '--disable-sync', '--disable-background-networking', '--new-window', $Url
) -PassThru
Write-Output 'The verified Admin session is open in a normal local browser window.'
Write-Output "Profile and session data are retained inside the protected QA folder: $profile"
