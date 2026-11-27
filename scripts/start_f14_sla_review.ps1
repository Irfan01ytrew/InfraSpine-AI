$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$root=Join-Path $repo '.verification-local-review-20261027-native\f14-sla-browser-27b1'
if(Test-Path -LiteralPath $root){ throw 'F14 isolated folder already exists' }
foreach($p in @(5179,63008)){if(Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue){throw 'F14 port occupied'}}
New-Item -ItemType Directory -Path $root | Out-Null
$owner=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=[System.Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($true,$false);$acl.SetOwner($owner)
foreach($sid in @($owner.Value,'S-1-5-18','S-1-5-32-544')){ $rule=[System.Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),[System.Security.AccessControl.FileSystemRights]::FullControl,([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow);[void]$acl.AddAccessRule($rule) }
Set-Acl -LiteralPath $root -AclObject $acl
foreach($d in @('api-cwd','data','logs','logs\api','logs\worker','evidence','tmp')){New-Item -ItemType Directory -Path (Join-Path $root $d) -Force | Out-Null}
$psql='C:\Program Files\PostgreSQL\18\bin\psql.exe'
$db='infraspine_f14browser_27b1_test'
$exists=& $psql 'postgresql://postgres@127.0.0.1:55888/postgres' -X -v ON_ERROR_STOP=1 -A -t -c "SELECT count(*) FROM pg_database WHERE datname='$db'"
if($LASTEXITCODE -ne 0 -or $exists.Trim() -ne '0'){throw 'F14 database exists or clone unavailable'}
& $psql 'postgresql://postgres@127.0.0.1:55888/postgres' -X -v ON_ERROR_STOP=1 -c "CREATE DATABASE $db"
if($LASTEXITCODE -ne 0){throw 'DB create failed'}
$python=Join-Path $repo '.venv-verify-312\Scripts\python.exe'
$env:DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:55888/$db"
$env:APP_ENV='testing';$env:MASTER_ENCRYPTION_KEY=[Convert]::ToBase64String([byte[]](1..32|ForEach-Object{107}));$env:MASTER_ENCRYPTION_KEY_ID='v1';$env:PYTHONPATH=$repo
$env:TEMP=Join-Path $root 'tmp';$env:TMP=$env:TEMP
Push-Location (Join-Path $repo '.verification-local-review-20261027-native\f10-test-runner-c5eeed7eca')
try { & $python -m alembic upgrade head; if($LASTEXITCODE -ne 0){throw 'Migration failed'} } finally {Pop-Location}
$env:F14_FIXTURE=Join-Path $root 'credentials.json'
Push-Location (Join-Path $root 'api-cwd')
try {& $python (Join-Path $repo 'scripts\seed_f14_sla_review.py');if($LASTEXITCODE -ne 0){throw 'Seed failed'}} finally {Pop-Location}
$env:LLM_MOCK_MODE='true';$env:SESSION_COOKIE_SECURE='false';$env:RAG_DATA_DIR=Join-Path $root 'data';$env:RAG_MODELS_DIR=Join-Path $repo 'data\models';$env:RAG_EMBEDDING_ALLOW_DOWNLOAD='false';$env:RAG_EMBEDDING_MODEL=Join-Path $repo 'data\models\models--sentence-transformers--all-MiniLM-L6-v2\snapshots\1110a243fdf4706b3f48f1d95db1a4f5529b4d41';$env:HF_HUB_OFFLINE='1';$env:TRANSFORMERS_OFFLINE='1';$env:RAG_VECTOR_STORE='faiss';$env:RAG_INGESTION_MODE='worker';$env:RAG_WORKER_ENABLED='false';$env:RAG_INDEX_SYNC_SECONDS='0';$env:RAG_USE_DOCLING='false';$env:RAG_PARSE_ISOLATION='true';$env:LOG_DIR=Join-Path $root 'logs\api'
$api=Start-Process $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','63008','--no-access-log') -WorkingDirectory (Join-Path $root 'api-cwd') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root 'logs\api-out.log') -RedirectStandardError (Join-Path $root 'logs\api-err.log')
$env:RAG_WORKER_ENABLED='true';$env:LOG_DIR=Join-Path $root 'logs\worker'
$worker=Start-Process $python -ArgumentList @('-m','app.worker','--concurrency','1') -WorkingDirectory (Join-Path $root 'api-cwd') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root 'logs\worker-out.log') -RedirectStandardError (Join-Path $root 'logs\worker-err.log')
$env:INFRASPINE_DEV_API_PROXY_TARGET='http://127.0.0.1:63008'
$node=(Get-Command node.exe).Source;$viteEntry=Join-Path $repo 'frontend\node_modules\vite\bin\vite.js'
$vite=Start-Process $node -ArgumentList @(('"'+$viteEntry+'"'),'--host','127.0.0.1','--strictPort','--port','5179') -WorkingDirectory (Join-Path $repo 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root 'logs\vite-out.log') -RedirectStandardError (Join-Path $root 'logs\vite-err.log')
@{apiPid=$api.Id;workerPid=$worker.Id;vitePid=$vite.Id;database=$db;migration='0055_workflow_sla_trigger'}|ConvertTo-Json|Set-Content (Join-Path $root 'state.json')
Write-Output 'F14_STACK_STARTED'
while($true){Start-Sleep -Seconds 5}
