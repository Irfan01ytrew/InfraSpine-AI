[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TestPaths
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $repo
$docker = Get-Command docker.exe -ErrorAction Stop
$modelCacheVolume = 'infraspine-verification-model-cache'
$knownVolumes = @(& $docker.Source --context desktop-linux volume ls --format '{{.Name}}')
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Docker volumes before preparing the test model cache.' }
if ($knownVolumes -notcontains $modelCacheVolume) {
    $null = & $docker.Source --context desktop-linux volume create `
        --label 'com.infraspine.verification.cache=public-models' $modelCacheVolume
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the dedicated public model-cache volume.' }
} else {
    $cacheLabels = (& $docker.Source --context desktop-linux volume inspect $modelCacheVolume `
        --format '{{json .Labels}}' | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or
        ($cacheLabels.'com.infraspine.verification.cache' -ne 'public-models' -and
         $cacheLabels.'com.docker.compose.volume' -ne 'verification-model-cache')) {
        throw 'The reserved model-cache volume exists with unexpected labels; refusing to mount it.'
    }
}
$project = 'infraspine-accept-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$suffix = [guid]::NewGuid().ToString('N').Substring(0, 12)
$network = "$project-test-network"
$compose = Join-Path $repo 'compose.test.yaml'
$variables = @(
    'VERIFY_DB_NAME', 'VERIFY_DB_USER', 'VERIFY_DB_PASSWORD', 'VERIFY_POSTGRES_PORT',
    'VERIFY_DB_GUARD', 'VERIFY_QDRANT_API_KEY', 'VERIFY_QDRANT_PORT', 'VERIFY_DOCKER_NETWORK',
    'DATABASE_URL', 'TEST_DATABASE_URL', 'QDRANT_TEST_URL', 'QDRANT_TEST_API_KEY', 'QDRANT_TEST_CONTAINER',
    'APP_ENV', 'INFASPINE_TEST_SCHEMA', 'TEST_DATABASE_ALLOW_SHARED_SCHEMA'
)
$previous = @{}
foreach ($name in $variables) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$networkCreated = $false
$composeAttempted = $false

function New-RandomHex([int]$Bytes) {
    $buffer = New-Object byte[] $Bytes
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
    return ([BitConverter]::ToString($buffer)).Replace('-', '').ToLowerInvariant()
}

function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

try {
    if ((& $docker.Source --context desktop-linux info --format '{{.ServerVersion}}' 2>$null) -notmatch '^\d+\.') {
        throw 'The authorized desktop-linux Docker Engine is unavailable.'
    }
    for ($attempt = 0; $attempt -lt 128 -and -not $networkCreated; $attempt++) {
        $subnet = "10.241.$(Get-Random -Minimum 0 -Maximum 256).0/24"
        try {
            $null = & $docker.Source --context desktop-linux network create --driver bridge --subnet $subnet `
                --label "com.infraspine.accept.project=$project" $network 2>&1
            $networkCreated = $LASTEXITCODE -eq 0
        } catch { $networkCreated = $false }
    }
    if (-not $networkCreated) { throw 'Could not allocate a unique isolated Docker network in 10.241.0.0/16.' }

    $dbUser = "ia$suffix"
    $dbPassword = New-RandomHex 32
    $qdrantKey = New-RandomHex 32
    $pgPort = Get-FreeLoopbackPort
    do { $qdrantPort = Get-FreeLoopbackPort } while ($qdrantPort -eq $pgPort)
    $env:VERIFY_DB_NAME = "infraspine_accept_${suffix}_test"
    $env:VERIFY_DB_USER = $dbUser
    $env:VERIFY_DB_PASSWORD = $dbPassword
    $env:VERIFY_DB_GUARD = "infraspine_accept_${suffix}_guard"
    $env:VERIFY_POSTGRES_PORT = "$pgPort"
    $env:VERIFY_QDRANT_API_KEY = $qdrantKey
    $env:VERIFY_QDRANT_PORT = "$qdrantPort"
    $env:VERIFY_DOCKER_NETWORK = $network
    $env:DATABASE_URL = "postgresql+asyncpg://${dbUser}:${dbPassword}@127.0.0.1:${pgPort}/$($env:VERIFY_DB_GUARD)"
    $env:TEST_DATABASE_URL = "postgresql+asyncpg://${dbUser}:${dbPassword}@127.0.0.1:${pgPort}/$($env:VERIFY_DB_NAME)"
    $env:QDRANT_TEST_URL = "http://127.0.0.1:${qdrantPort}"
    $env:QDRANT_TEST_API_KEY = $qdrantKey
    $env:APP_ENV = 'testing'
    $env:INFASPINE_TEST_SCHEMA = ''
    $env:TEST_DATABASE_ALLOW_SHARED_SCHEMA = ''

    $composeAttempted = $true
    & $docker.Source --context desktop-linux compose -p $project -f $compose up -d --wait
    if ($LASTEXITCODE -ne 0) { throw 'Isolated PostgreSQL/Qdrant services failed readiness.' }
    $env:QDRANT_TEST_CONTAINER = (& $docker.Source --context desktop-linux compose -p $project -f $compose ps -q qdrant).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $env:QDRANT_TEST_CONTAINER) { throw 'Isolated Qdrant container ID was unavailable.' }

    $selectedPaths = @($TestPaths -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if (-not $selectedPaths) { throw 'Specify one or more test paths separated by semicolons.' }
    if ($selectedPaths.Count -eq 1 -and (Test-Path -LiteralPath $selectedPaths[0] -PathType Container)) {
        $selectedDirectory = (Resolve-Path -LiteralPath $selectedPaths[0]).Path
        $testsDirectory = (Resolve-Path -LiteralPath (Join-Path $repo 'tests')).Path
        if ($selectedDirectory -ne $testsDirectory) { throw 'Directory acceptance runs are limited to the repository tests directory.' }
        $selectedPaths = @(Get-ChildItem -LiteralPath $selectedDirectory -Recurse -File -Filter 'test_*.py' |
            Sort-Object FullName |
            ForEach-Object { $_.FullName.Substring($repo.Length + 1).Replace('\', '/') })
    }
    foreach ($testNode in $selectedPaths) {
        $testFile = ($testNode -split '::', 2)[0]
        if (-not (Test-Path -LiteralPath $testFile -PathType Leaf)) { throw "Test file does not exist: $testNode" }
    }
    $pytestArgs = @('python', '-m', 'pytest', '-q', '--tb=long', '--show-capture=no', '--basetemp', '/tmp/infraspine-pytest') + $selectedPaths
    & $docker.Source --context desktop-linux compose --profile verification -p $project -f $compose `
        run --build --rm backend-test @pytestArgs
    if ($LASTEXITCODE -ne 0) { throw 'Focused backend acceptance tests failed.' }
    Write-Host "Focused backend acceptance passed in isolated project $project. Its test-only PostgreSQL/Qdrant volumes are retained."
}
finally {
    if ($composeAttempted) {
        & $docker.Source --context desktop-linux compose -p $project -f $compose down --remove-orphans | Out-Null
    }
    if ($networkCreated) {
        $details = & $docker.Source --context desktop-linux network inspect $network 2>$null | ConvertFrom-Json
        $owner = if ($details) { $details[0].Labels['com.infraspine.accept.project'] } else { $null }
        if ($LASTEXITCODE -eq 0 -and $owner -eq $project) {
            & $docker.Source --context desktop-linux network rm $network | Out-Null
        }
    }
    foreach ($name in $variables) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
    Pop-Location
}
