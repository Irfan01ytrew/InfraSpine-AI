$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$root=Join-Path $repo '.verification-local-review-20261027-native\f15-pagination-browser-4a62'
if(Test-Path -LiteralPath $root){throw 'F15 review folder exists; inspect before reuse'}
foreach($port in @(5180,63009)){if(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue){throw 'F15 port occupied'}}
$frontendEnv=@(Get-ChildItem (Join-Path $repo 'frontend') -Force -File -Filter '.env*' -ErrorAction SilentlyContinue);if($frontendEnv.Count){throw 'Frontend local env files block Vite start'}
New-Item -ItemType Directory -Path $root | Out-Null
$owner=[Security.Principal.WindowsIdentity]::GetCurrent().User;$acl=[System.Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($true,$false);$acl.SetOwner($owner)
foreach($sid in @($owner.Value,'S-1-5-18','S-1-5-32-544')){$rule=[System.Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),[System.Security.AccessControl.FileSystemRights]::FullControl,([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow);[void]$acl.AddAccessRule($rule)}
Set-Acl -LiteralPath $root -AclObject $acl
foreach($d in @('api-cwd','logs','logs\api')){New-Item -ItemType Directory -Path (Join-Path $root $d) | Out-Null}
$python=Join-Path $repo '.venv-verify-312\Scripts\python.exe';$node=(Get-Command node.exe).Source;$vite=Join-Path $repo 'frontend\node_modules\vite\bin\vite.js'
$env:DATABASE_URL='postgresql+asyncpg://postgres@127.0.0.1:55888/infraspine_f15page_2c71_test';$env:APP_ENV='testing';$env:LLM_MOCK_MODE='true';$env:MASTER_ENCRYPTION_KEY=[Convert]::ToBase64String([byte[]](1..32|ForEach-Object{107}));$env:MASTER_ENCRYPTION_KEY_ID='v1';$env:SESSION_COOKIE_SECURE='false';$env:RAG_DATA_DIR=Join-Path $root 'data';$env:RAG_WORKER_ENABLED='false';$env:RAG_INGESTION_MODE='worker';$env:RAG_INDEX_SYNC_SECONDS='0';$env:LOG_DIR=Join-Path $root 'logs\api';$env:PYTHONPATH=$repo;$env:TEMP=Join-Path $repo '.verification-local-review-20261027-native\f14-sla-browser-27b1\tmp';$env:TMP=$env:TEMP
$api=Start-Process $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','63009','--no-access-log') -WorkingDirectory (Join-Path $root 'api-cwd') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root 'logs\api-out.log') -RedirectStandardError (Join-Path $root 'logs\api-err.log')
$env:INFRASPINE_DEV_API_PROXY_TARGET='http://127.0.0.1:63009';$viteProc=Start-Process $node -ArgumentList @(('"'+$vite+'"'),'--host','127.0.0.1','--strictPort','--port','5180') -WorkingDirectory (Join-Path $repo 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root 'logs\vite-out.log') -RedirectStandardError (Join-Path $root 'logs\vite-err.log')
@{apiPid=$api.Id;vitePid=$viteProc.Id;apiPort=63009;frontendPort=5180;database='infraspine_f15page_2c71_test';migration='0055_workflow_sla_trigger';reviewUrl='http://127.0.0.1:5180/admin/'}|ConvertTo-Json|Set-Content (Join-Path $root 'state.json')
while($true){Start-Sleep -Seconds 5}
