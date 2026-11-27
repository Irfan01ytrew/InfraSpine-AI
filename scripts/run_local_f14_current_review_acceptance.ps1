param(
    [string]$ReviewRoot = '.verification-local-review-20261027-native',
    [string]$FixturePath = '',
    [string]$Url = 'http://127.0.0.1:5175/admin/',
    [ValidateRange(1024, 65535)][int]$ApiPort = 63004
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not [IO.Path]::IsPathRooted($ReviewRoot)) { $ReviewRoot = Join-Path $repoRoot $ReviewRoot }
$reviewRoot = [IO.Path]::GetFullPath($ReviewRoot)
if ([string]::IsNullOrWhiteSpace($FixturePath)) { $FixturePath = Join-Path $reviewRoot 'qa-users.json' }
elseif (-not [IO.Path]::IsPathRooted($FixturePath)) { $FixturePath = Join-Path $repoRoot $FixturePath }
$fixturePath = [IO.Path]::GetFullPath($FixturePath)
$uri = [Uri]$Url
if ($uri.Scheme -ne 'http' -or $uri.Host -notin @('127.0.0.1', 'localhost', '::1')) { throw 'F14 review acceptance permits loopback HTTP only.' }
if (!(Test-Path -LiteralPath $fixturePath -PathType Leaf)) { throw 'The protected synthetic QA fixture is unavailable.' }
$fixtureAcl = Get-Acl -LiteralPath $fixturePath
if (-not $fixtureAcl.AreAccessRulesProtected) { throw 'The synthetic credentials ACL is not protected from inheritance.' }
$allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
$fixtureRules = @($fixtureAcl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
if (-not $fixtureRules -or @($fixtureRules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
    throw 'The synthetic credentials ACL is broader than this user, SYSTEM and local Administrators.'
}
if (-not (Get-NetTCPConnection -State Listen -LocalPort $ApiPort -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -eq '127.0.0.1' })) {
    throw 'The existing isolated loopback API is unavailable; no service was started.'
}
$node = (Get-Command node.exe -ErrorAction Stop).Source
$env:INFRASPINE_F14_CURRENT_URL = $Url
$env:INFRASPINE_F14_FIXTURE = $fixturePath
$env:INFRASPINE_F14_REVIEW_ROOT = $reviewRoot
try {
    & $node (Join-Path $PSScriptRoot 'browser_f14_current_review_acceptance.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Current-source F14 acceptance failed; only synthetic local QA requests/workflow were involved.' }
} finally {
    Remove-Item Env:INFRASPINE_F14_CURRENT_URL,Env:INFRASPINE_F14_FIXTURE,Env:INFRASPINE_F14_REVIEW_ROOT -ErrorAction SilentlyContinue
}
