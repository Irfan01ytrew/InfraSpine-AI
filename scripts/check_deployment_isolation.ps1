[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$compose = Join-Path $repo 'compose.onprem.yaml'
$docker = Get-Command docker.exe -ErrorAction SilentlyContinue
if (-not $docker) { throw 'Docker Compose CLI is required for this config-only check.' }

$suffix = [guid]::NewGuid().ToString('N')
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) "infraspine-isolation-$suffix"
New-Item -ItemType Directory -Path $tempRoot | Out-Null

function New-RandomHex([int]$bytes = 32) {
    $buffer = New-Object byte[] $bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return [BitConverter]::ToString($buffer).Replace('-', '').ToLowerInvariant()
}

function New-RandomBase64([int]$bytes = 32) {
    $buffer = New-Object byte[] $bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($buffer)
}

function New-TestConfig([string]$name, [int]$port, [string]$subnet) {
    $root = Join-Path $tempRoot $name
    $dirs = @{
        postgres = Join-Path $root 'postgres'
        data = Join-Path $root 'data'
        logs = Join-Path $root 'logs'
        backups = Join-Path $root 'backups'
        qdrant = Join-Path $root 'qdrant'
        imports = Join-Path $root 'imports'
    }
    $user = "user_$name"
    $db = "db_$name"
    $dbPassword = New-RandomHex
    $masterKey = New-RandomBase64
    $qdrantKey = New-RandomHex
    $rows = @(
        "POSTGRES_DB=$db",
        "POSTGRES_USER=$user",
        "POSTGRES_PASSWORD=$dbPassword",
        "DATABASE_URL=postgresql+asyncpg://${user}:${dbPassword}@postgres:5432/${db}",
        "MASTER_ENCRYPTION_KEY=$masterKey",
        'MASTER_ENCRYPTION_KEY_ID=v1',
        'LICENSE_PUBLIC_KEY=',
        'LICENSE_ENFORCEMENT_ENABLED=false',
        "RAG_QDRANT_API_KEY=$qdrantKey",
        "INFRASPINE_NETWORK_SUBNET=$subnet",
        "INFRASPINE_API_PORT=$port",
        "INFRASPINE_POSTGRES_DIR=$($dirs.postgres)",
        "INFRASPINE_DATA_DIR=$($dirs.data)",
        "INFRASPINE_LOG_DIR=$($dirs.logs)",
        "INFRASPINE_BACKUP_DIR=$($dirs.backups)",
        "INFRASPINE_QDRANT_DIR=$($dirs.qdrant)",
        "INFRASPINE_IMPORT_DIR=$($dirs.imports)"
    )
    $envPath = Join-Path $tempRoot "$name.env"
    [IO.File]::WriteAllLines($envPath, $rows, [Text.UTF8Encoding]::new($false))
    return [pscustomobject]@{ Name = $name; Project = "isolation_$($suffix)_$name"; Env = $envPath; Paths = $dirs; Database = $db; DbPassword = $dbPassword; MasterKey = $masterKey; QdrantKey = $qdrantKey; Port = $port; Subnet = $subnet }
}

function Read-Compose([pscustomobject]$test) {
    $jsonLines = & $docker.Source compose --project-name $test.Project --env-file $test.Env --file $compose --profile qdrant config --format json 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Compose config failed for synthetic project $($test.Name)." }
    try { return ($jsonLines -join [Environment]::NewLine) | ConvertFrom-Json }
    catch { throw "Compose config was not valid JSON for synthetic project $($test.Name)." }
}

try {
    $first = New-TestConfig 'customer_a' 18080 '10.252.1.0/24'
    $second = New-TestConfig 'customer_b' 18081 '10.252.2.0/24'
    $a = Read-Compose $first
    $b = Read-Compose $second

    if ($a.services.api.build.context -ne $b.services.api.build.context) {
        throw 'The two synthetic customer projects do not use the same build context.'
    }

    $targets = @{
        postgres = '/var/lib/postgresql'
        data = '/var/lib/infraspine/data'
        logs = '/var/lib/infraspine/logs'
        backups = '/var/lib/infraspine/backups'
        qdrant = '/qdrant/storage'
    }
    foreach ($targetName in $targets.Keys) {
        $target = $targets[$targetName]
        $serviceName = if ($targetName -eq 'postgres') { 'postgres' } elseif ($targetName -eq 'qdrant') { 'qdrant' } else { 'api' }
        $mountA = @($a.services.$serviceName.volumes | Where-Object { $_.target -eq $target })
        $mountB = @($b.services.$serviceName.volumes | Where-Object { $_.target -eq $target })
        if ($mountA.Count -ne 1 -or $mountB.Count -ne 1 -or $mountA[0].source -eq $mountB[0].source) {
            throw "Customer storage for '$targetName' is missing or shared."
        }
    }

    $portA = [string]$a.services.api.ports[0].published
    $portB = [string]$b.services.api.ports[0].published
    if ($portA -eq $portB -or $portA -ne [string]$first.Port -or $portB -ne [string]$second.Port) {
        throw 'Synthetic customer API ports are missing or collide.'
    }
    if ($a.networks.infraspine.ipam.config[0].subnet -ne $first.Subnet -or
        $b.networks.infraspine.ipam.config[0].subnet -ne $second.Subnet -or
        $first.Subnet -eq $second.Subnet) {
        throw 'Synthetic customer Docker network subnets are missing or shared.'
    }
    if ($first.Database -eq $second.Database -or $first.DbPassword -eq $second.DbPassword -or
        $first.MasterKey -eq $second.MasterKey -or $first.QdrantKey -eq $second.QdrantKey) {
        throw 'Synthetic customer database names or installation secrets are shared.'
    }

    Write-Host 'PASS: two distinct synthetic Compose projects use the same code/build context.'
    Write-Host 'PASS: database, data, log, backup and Qdrant mounts; API ports; and generated secrets are isolated.'
    Write-Host 'SCOPE: config-only validation; no containers, database, API or worker were started.'
}
finally {
    $fullTemp = [IO.Path]::GetFullPath($tempRoot).TrimEnd('\')
    $tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    $leaf = Split-Path -Leaf $fullTemp
    if ($fullTemp.StartsWith("$tempBase\", [StringComparison]::OrdinalIgnoreCase) -and
        $leaf -match '^infraspine-isolation-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $fullTemp -Recurse -Force
    } else {
        throw 'Temporary isolation-test cleanup refused because the resolved path did not match its generated temp directory.'
    }
}
