[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [string]$PythonPath = '',
    [int]$BenchmarkVectors = 10000,
    [string]$BackendTestSelection = '',
    [switch]$HostBackendTests
)

$ErrorActionPreference = 'Stop'
if ($BenchmarkVectors -lt 0) { throw 'BenchmarkVectors must be 0 (skip benchmarks) or a positive vector count.' }
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $repo
try {
    $gitRoot = (& git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or (Resolve-Path $gitRoot).Path -ne $repo) {
        throw 'Run this script from the repository checkout; Git root did not match the script location.'
    }
    Write-Host "OS: $([System.Runtime.InteropServices.RuntimeInformation]::OSDescription)"
    Write-Host "Shell: PowerShell $($PSVersionTable.PSVersion)"
    Write-Host "Repository: $repo"

    $venv = Join-Path $repo '.venv-verify-312'
    $python = Join-Path $venv 'Scripts\python.exe'
    $pythonInfo = $null
    $createdVenv = $false
    if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
        if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
            throw "The explicit Python interpreter path does not exist: $PythonPath"
        }
        $python = (Resolve-Path -LiteralPath $PythonPath).Path
        try {
            $candidateInfo = @(& $python -c 'import sys; print(sys.executable); print(sys.version.split()[0])' 2>$null)
            if ($LASTEXITCODE -eq 0 -and $candidateInfo.Count -ge 2 -and $candidateInfo[-1] -like '3.12.*') {
                $pythonInfo = $candidateInfo
            }
        }
        catch {
            $pythonInfo = $null
        }
        if ($null -eq $pythonInfo) {
            throw 'The explicit Python interpreter must be a working Python 3.12 executable.'
        }
    }
    if ($null -eq $pythonInfo -and (Test-Path -LiteralPath $python)) {
        try {
            $candidateInfo = @(& $python -c 'import sys; print(sys.executable); print(sys.version.split()[0])' 2>$null)
            if ($LASTEXITCODE -eq 0 -and $candidateInfo.Count -ge 2 -and $candidateInfo[-1] -like '3.12.*') {
                $pythonInfo = $candidateInfo
            }
        }
        catch {
            $pythonInfo = $null
        }
    }
    if ($null -eq $pythonInfo) {
        if (Test-Path -LiteralPath $venv) {
            # Preserve the existing/stale environment byte-for-byte and bootstrap in a new sibling directory.
            $suffix = [guid]::NewGuid().ToString('N').Substring(0, 8)
            $venv = Join-Path $repo ".venv-verify-312-fresh-$suffix"
            $python = Join-Path $venv 'Scripts\python.exe'
            Write-Host 'The existing verification environment is not a working Python 3.12 environment; preserving it.'
        }
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        $launcherSupports312 = $false
        if ($launcher) {
            $registeredPython = & $launcher.Source -0p 2>$null
            $launcherSupports312 = ($registeredPython -match '(?m)3\.12(?:\s|$)')
        }
        if ($launcherSupports312) {
            & $launcher.Source -3.12 -m venv $venv
        }
        else {
            $python312 = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
            $python312Available = $false
            try { $python312Available = Test-Path -LiteralPath $python312 -ErrorAction Stop }
            catch { Write-Host 'The conventional per-user Python 3.12 path could not be inspected from this runtime.' }
            if (-not $python312Available) {
                throw 'Python 3.12 is not discoverable through the launcher and its conventional per-user path is not accessible. Install/register Python 3.12 with the Windows py launcher, then rerun.'
            }
            & $python312 -m venv $venv
        }
        if ($LASTEXITCODE -ne 0) { throw "Could not create a fresh Python 3.12 environment at $venv. The existing environment was preserved." }
        $createdVenv = $true
        $pythonInfo = @(& $python -c 'import sys; print(sys.executable); print(sys.version.split()[0])')
        if ($LASTEXITCODE -ne 0 -or $pythonInfo.Count -lt 2 -or $pythonInfo[-1] -notlike '3.12.*') {
            throw "The new verification environment did not start as Python 3.12. It was preserved for inspection: $python"
        }
    }
    Write-Host "Interpreter: $($pythonInfo[0]) (Python $($pythonInfo[1]))"

    if (-not $SkipInstall -or $createdVenv) {
        & $python -m pip install --index-url https://download.pytorch.org/whl/cpu `
            --extra-index-url https://pypi.org/simple torch==2.14.1
        if ($LASTEXITCODE -ne 0) { throw 'CPU PyTorch install failed.' }
        & $python -m pip install -r requirements.txt -r requirements-dev.txt -r requirements-media.txt `
            -r requirements-document-tables.txt -r requirements-reporting.txt
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; no existing environment was replaced.' }
    }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'pip check reported inconsistent dependencies.' }
    & $python scripts/scan_release_secrets.py --root $repo
    if ($LASTEXITCODE -ne 0) { throw 'Release-source secret scan found a credential-shaped value. Review only the reported file and line; the scanner never prints matched content.' }

    $baseTemp = Join-Path $env:TEMP ('infraspine-verify-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $baseTemp | Out-Null
    $serviceTests = @(
        'tests/test_database_safety.py', 'tests/test_media_ingestion.py',
        'tests/test_vector_store_benchmark.py', 'tests/test_qdrant_store.py',
        'tests/test_crypto_and_settings.py', 'tests/test_gemini_grounding.py',
        'tests/test_rag_units.py'
    )
    & $python -m pytest --noconftest -q @serviceTests --basetemp $baseTemp
    $unitExit = $LASTEXITCODE
    Remove-Item -LiteralPath $baseTemp -Recurse -Force -ErrorAction SilentlyContinue
    if ($unitExit -ne 0) { throw 'Service-independent backend tests failed.' }

    $docker = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $docker) { throw 'Docker CLI was not found. Start Docker Desktop and install its Compose plugin, then rerun.' }
    Write-Host "Docker context: $(& $docker.Source context show)"
    $engine = & $docker.Source info --format '{{.ServerVersion}}' 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Docker engine is unreachable:'
        Write-Host ($engine -join [Environment]::NewLine)
        throw 'Start Docker Desktop and wait for its engine to be ready. No Docker reset or volume cleanup was attempted.'
    }
    Write-Host "Docker engine: $engine"

    $compose = Join-Path $repo 'compose.test.yaml'
    $project = 'infraspine-verify-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
    $suffix = [guid]::NewGuid().ToString('N').Substring(0, 12)
    function Get-FreeLoopbackPort {
        $listener = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
        $listener.Start()
        try { return ([Net.IPEndPoint]$listener.LocalEndpoint).Port }
        finally { $listener.Stop() }
    }
    $names = @('VERIFY_DB_NAME','VERIFY_DB_USER','VERIFY_DB_PASSWORD','VERIFY_POSTGRES_PORT',
        'VERIFY_DB_GUARD','VERIFY_QDRANT_API_KEY','VERIFY_QDRANT_PORT','DATABASE_URL','TEST_DATABASE_URL',
        'QDRANT_TEST_URL','QDRANT_TEST_API_KEY','QDRANT_TEST_CONTAINER','APP_ENV','INFASPINE_TEST_SCHEMA',
        'TEST_DATABASE_ALLOW_SHARED_SCHEMA')
    $previous = @{}
    foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
    $composeAttempted = $false
    try {
        $dbName = "infraspine_verify_${suffix}_test"
        $dbUser = "iv${suffix}"
        $randomBytes = New-Object byte[] 32
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($randomBytes) } finally { $rng.Dispose() }
        $dbPassword = ([BitConverter]::ToString($randomBytes)).Replace('-', '')
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($randomBytes) } finally { $rng.Dispose() }
        $qdrantKey = ([BitConverter]::ToString($randomBytes)).Replace('-', '')
        $pgPort = Get-FreeLoopbackPort
        do { $qdrantPort = Get-FreeLoopbackPort } while ($qdrantPort -eq $pgPort)
    $names += 'VERIFY_DOCKER_NETWORK'
    $previous['VERIFY_DOCKER_NETWORK'] = [Environment]::GetEnvironmentVariable('VERIFY_DOCKER_NETWORK', 'Process')
    $testNetwork = "$project-test-network"
    $networkCreated = $false
    for ($attempt = 0; $attempt -lt 128 -and -not $networkCreated; $attempt++) {
        $subnetOctet = Get-Random -Minimum 0 -Maximum 256
        $subnet = "10.240.$subnetOctet.0/24"
        # Docker reports an expected subnet collision on stderr. With
        # ErrorActionPreference=Stop, PowerShell 5.1 promotes that stream to a
        # terminating error before the next candidate can be tried.
        try {
            $networkResult = & $docker.Source network create --driver bridge --subnet $subnet `
                --label "com.infraspine.verify.project=$project" $testNetwork 2>&1
            $networkCreated = $LASTEXITCODE -eq 0
        }
        catch {
            $networkCreated = $false
        }
    }
    if (-not $networkCreated) { throw 'Could not allocate a unique isolated test network in 10.240.0.0/16.' }
        $env:VERIFY_DB_NAME = $dbName
        $env:VERIFY_DB_USER = $dbUser
        $env:VERIFY_DB_PASSWORD = $dbPassword
        $env:VERIFY_DB_GUARD = "infraspine_verify_${suffix}_guard"
        $env:VERIFY_POSTGRES_PORT = "$pgPort"
        $env:VERIFY_QDRANT_API_KEY = $qdrantKey
        $env:VERIFY_QDRANT_PORT = "$qdrantPort"
    $env:VERIFY_DOCKER_NETWORK = $testNetwork
        $env:DATABASE_URL = "postgresql+asyncpg://${dbUser}:${dbPassword}@127.0.0.1:${pgPort}/$($env:VERIFY_DB_GUARD)"
        $env:TEST_DATABASE_URL = "postgresql+asyncpg://${dbUser}:${dbPassword}@127.0.0.1:${pgPort}/${dbName}"
        $env:QDRANT_TEST_URL = "http://127.0.0.1:${qdrantPort}"
        $env:QDRANT_TEST_API_KEY = $qdrantKey

        $composeAttempted = $true
        & $docker.Source compose -p $project -f $compose up -d --wait
        if ($LASTEXITCODE -ne 0) { throw 'Isolated PostgreSQL/Qdrant services did not become healthy.' }
        $pgBinding = (& $docker.Source compose -p $project -f $compose port postgres 5432).Trim()
        if ($LASTEXITCODE -ne 0 -or $pgBinding -notmatch '^127\.0\.0\.1:\d+$') {
            throw 'PostgreSQL test service did not publish its expected loopback-only port.'
        }
        $qdrantBinding = (& $docker.Source compose -p $project -f $compose port qdrant 6333).Trim()
        if ($LASTEXITCODE -ne 0 -or $qdrantBinding -notmatch '^127\.0\.0\.1:\d+$') {
            throw 'Qdrant test service did not publish its expected loopback-only port; no Qdrant request was sent.'
        }
        Write-Host "Isolated test service endpoints: PostgreSQL $pgBinding; Qdrant $qdrantBinding"
        $env:QDRANT_TEST_URL = "http://$qdrantBinding"
        $env:QDRANT_TEST_CONTAINER = (& $docker.Source compose -p $project -f $compose ps -q qdrant).Trim()
        $env:APP_ENV = 'testing'
        $env:INFASPINE_TEST_SCHEMA = ''
        $env:TEST_DATABASE_ALLOW_SHARED_SCHEMA = ''

        $null = & $python -m pytest --collect-only -q
        if ($LASTEXITCODE -ne 0) { throw 'Backend test collection failed.' }
        & $python -m pytest --noconftest -q tests/test_qdrant_server.py --basetemp (Join-Path $env:TEMP ($project + '-qdrant'))
        if ($LASTEXITCODE -ne 0) { throw 'Actual-server Qdrant integration test failed.' }
        if ($BenchmarkVectors -gt 0) {
            & $python scripts/benchmark_qdrant_acl.py --vectors $BenchmarkVectors --dimension 384 --queries 20 --concurrency 1
            if ($LASTEXITCODE -ne 0) { throw 'Actual-server Qdrant ACL benchmark failed.' }
        }

        if ($HostBackendTests) {
            $backendTests = @('-m', 'pytest', '-q', '--tb=short', '--basetemp',
                (Join-Path $env:TEMP ($project + '-backend')))
            if ($BackendTestSelection) { $backendTests += ($BackendTestSelection -split ';') }
            else { $backendTests += 'tests' }
            & $python @backendTests
            if ($LASTEXITCODE -ne 0) { throw 'Backend test suite failed against the isolated host test services.' }
        }
        else {
            $backendTests = @('pytest', '-q', '--tb=short', '--basetemp', '/tmp/infraspine-pytest')
            if ($BackendTestSelection) { $backendTests += ($BackendTestSelection -split ';') }
            else { $backendTests += 'tests' }
            & $docker.Source compose --profile verification -p $project -f $compose run --build --rm backend-test @backendTests
            if ($LASTEXITCODE -ne 0) { throw 'Full backend test suite failed in the isolated Linux test container.' }
        }

        if ($BenchmarkVectors -gt 0) {
            & $python scripts/benchmark_vector_store.py --vectors $BenchmarkVectors --dimension 384 --queries 10
            if ($LASTEXITCODE -ne 0) { throw 'Synthetic FAISS benchmark failed.' }
        }
        Write-Host "Qdrant test container: ${project}-qdrant-1"
        Write-Host 'Isolated service volumes are retained; only this run’s containers and labeled network are removed.'
    }
    finally {
        if ($composeAttempted) {
            & $docker.Source compose -p $project -f $compose down --remove-orphans | Out-Null
        }
        if ($networkCreated) {
            $networkDetails = & $docker.Source network inspect $testNetwork 2>$null | ConvertFrom-Json
            $owner = if ($networkDetails) { $networkDetails[0].Labels['com.infraspine.verify.project'] } else { $null }
            if ($LASTEXITCODE -eq 0 -and $owner -eq $project) {
                & $docker.Source network rm $testNetwork | Out-Null
            }
        }
        foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    }
}
finally {
    Pop-Location
}
