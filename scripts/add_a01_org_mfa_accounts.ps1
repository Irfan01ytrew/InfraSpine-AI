param([ValidateRange(1024,65535)][int]$PostgresPort=55888)

$ErrorActionPreference='Stop'
$repoRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtimeRoot=Join-Path $repoRoot '.verification-local-review-20261027-native\a01-org-mfa-current-20261027'
$fixture=Join-Path $runtimeRoot 'review-credentials.json'
$python=Join-Path $repoRoot '.venv-verify-312\Scripts\python.exe'
$database='infraspine_a01orgmfa_20261027_test'
if (!(Test-Path -LiteralPath $runtimeRoot -PathType Container)) { throw 'A01 runtime folder is unavailable.' }
if (Test-Path -LiteralPath $fixture) { throw 'A01 credential fixture already exists and will not be overwritten.' }
$folderAcl=Get-Acl -LiteralPath $runtimeRoot
if (!$folderAcl.AreAccessRulesProtected) { throw 'A01 runtime folder ACL is not protected.' }
$env:DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:$PostgresPort/$database"
$env:APP_ENV='testing';$env:DATABASE_POOL_SIZE='1';$env:DATABASE_POOL_MAX_OVERFLOW='0'
try {
    & $python (Join-Path $PSScriptRoot 'add_a01_org_mfa_accounts.py') $fixture
    if ($LASTEXITCODE -ne 0) { throw 'A01 synthetic account creation failed.' }
} finally {
    Remove-Item Env:DATABASE_URL,Env:APP_ENV,Env:DATABASE_POOL_SIZE,Env:DATABASE_POOL_MAX_OVERFLOW -ErrorAction SilentlyContinue
}
$fileAcl=Get-Acl -LiteralPath $fixture
$fileAcl.SetAccessRuleProtection($true,$false)
foreach ($rule in @($fileAcl.Access)) { [void]$fileAcl.RemoveAccessRuleSpecific($rule) }
foreach ($principal in @(
    [Security.Principal.WindowsIdentity]::GetCurrent().User,
    [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
    [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
)) {
    $fileAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $principal,[Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.InheritanceFlags]::None,[Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    ))
}
Set-Acl -LiteralPath $fixture -AclObject $fileAcl
$confirmed=Get-Acl -LiteralPath $fixture
if (!$confirmed.AreAccessRulesProtected) { throw 'A01 credential fixture ACL protection did not persist.' }
Write-Output 'Two new synthetic accounts added; credential fixture ACL verified for this user, SYSTEM and local Administrators.'
