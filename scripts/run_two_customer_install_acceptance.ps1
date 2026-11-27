[CmdletBinding()]
param(
    [string]$Image = 'infraspine-ai:acceptance-20261024-aclfix-r2'
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$compose = Join-Path $repo 'compose.onprem.yaml'
$docker = (Get-Command docker.exe -ErrorAction Stop).Source
$node = (Get-Command node.exe -ErrorAction Stop).Source
$suffix = [guid]::NewGuid().ToString('N')
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) "infraspine-customer-accept-$suffix"
New-Item -ItemType Directory -Path $tempRoot | Out-Null
$started = [System.Collections.Generic.List[object]]::new()
$previousFixture = [Environment]::GetEnvironmentVariable('INFRASPINE_CUSTOMER_INSTALL_FIXTURE', 'Process')
$previousFirstRunFixture = [Environment]::GetEnvironmentVariable('INFRASPINE_FIRST_RUN_FIXTURE', 'Process')

function Protect-TestRoot([string]$Path) {
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $sid, [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit,
        [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow
    )
    $acl.SetAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function New-RandomHex([int]$Bytes = 32) {
    $buffer = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($buffer)).Replace('-', '').ToLowerInvariant()
}

function New-RandomBase64([int]$Bytes = 32) {
    $buffer = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($buffer)
}

function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try { return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port }
    finally { $listener.Stop() }
}

function Get-IPv4NetworkRange([string]$Cidr) {
    $parts = $Cidr.Split('/')
    if ($parts.Count -ne 2) { return $null }
    try {
        $bytes = [Net.IPAddress]::Parse($parts[0]).GetAddressBytes()
        $prefix = [int]$parts[1]
    } catch { return $null }
    if ($bytes.Length -ne 4 -or $prefix -lt 0 -or $prefix -gt 32) { return $null }
    $address = [uint64]$bytes[0] * 16777216 + [uint64]$bytes[1] * 65536 + [uint64]$bytes[2] * 256 + [uint64]$bytes[3]
    $size = [uint64][math]::Pow(2, 32 - $prefix)
    $start = $address - ($address % $size)
    return [pscustomobject]@{ Start = $start; End = ($start + $size - 1) }
}

function Get-ExistingDockerSubnets {
    $ids = @(& $docker --context desktop-linux network ls -q)
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect existing Docker networks.' }
    $subnets = [System.Collections.Generic.List[string]]::new()
    foreach ($id in $ids) {
        $found = @(& $docker --context desktop-linux network inspect --format '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' $id 2>$null)
        if ($LASTEXITCODE -ne 0) { throw 'Could not inspect existing Docker network subnets.' }
        foreach ($subnet in $found) { if ($subnet.Trim()) { $subnets.Add($subnet.Trim()) } }
    }
    return ,$subnets
}

function New-UniqueDockerSubnet([System.Collections.Generic.List[string]]$Reserved) {
    $existingRanges = @($Reserved | ForEach-Object { Get-IPv4NetworkRange $_ } | Where-Object { $_ })
    foreach ($secondOctet in 243..250) {
        foreach ($attempt in 1..256) {
            $thirdOctet = Get-Random -Minimum 0 -Maximum 256
            $candidate = "10.$secondOctet.$thirdOctet.0/24"
            $range = Get-IPv4NetworkRange $candidate
            $overlap = @($existingRanges | Where-Object { $_.Start -le $range.End -and $range.Start -le $_.End }).Count -gt 0
            if (-not $overlap) { $Reserved.Add($candidate); return $candidate }
        }
    }
    throw 'Could not allocate a unique synthetic Docker subnet in 10.243.0.0/16 through 10.250.0.0/16.'
}

function Invoke-Docker([string[]]$Arguments, [string]$FailureMessage) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $docker @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($exitCode -ne 0) {
        $text = ($output | ForEach-Object { $_.ToString() }) -join "`n"
        $category = if ($text -match 'address pools have been fully subnetted') { 'NETWORK_SUBNET_EXHAUSTED' }
            elseif ($text -match 'password authentication failed') { 'DATABASE_AUTHENTICATION' }
            elseif ($text -match 'could not translate host name|name or service not known') { 'DATABASE_DNS' }
            elseif ($text -match 'connection refused|no response') { 'SERVICE_NOT_READY' }
            elseif ($text -match 'not found|no such image') { 'IMAGE_OR_RESOURCE_NOT_FOUND' }
            elseif ($text -match 'unhealthy|health check') { 'HEALTHCHECK_FAILED' }
            else { 'DOCKER_OPERATION_FAILED' }
        Write-Host "SYNTHETIC_ACCEPTANCE_DIAGNOSTIC=$category"
        throw $FailureMessage
    }
}

function Invoke-DockerWithInput([string[]]$Arguments, [string]$InputLine, [string]$FailureMessage) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $InputLine | & $docker @Arguments 2>&1 | Out-Null
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($exitCode -ne 0) { throw $FailureMessage }
}

function New-Customer([string]$Name, [string]$Label, [string]$PublicKey, [string]$Subnet) {
    $root = Join-Path $tempRoot $Name
    $paths = @{
        postgres = Join-Path $root 'postgres'
        data = Join-Path $root 'data'
        logs = Join-Path $root 'logs'
        backups = Join-Path $root 'backups'
        qdrant = Join-Path $root 'qdrant'
        imports = Join-Path $root 'imports'
    }
    foreach ($path in $paths.Values) { New-Item -ItemType Directory -Path $path -Force | Out-Null }
    $db = "infraspine_${Name}_${suffix.Substring(0, 8)}"
    $user = "app_${Name}_${suffix.Substring(0, 8)}"
    $password = New-RandomHex
    $email = "admin-${Name}-${suffix.Substring(0, 8)}@qa.invalid"
    $adminPassword = New-RandomHex 24
    $qdrantKey = New-RandomHex
    $masterKey = New-RandomBase64
    $port = Get-FreeLoopbackPort
    $project = "infraspine_${Name}_$suffix"
    $envPath = Join-Path $tempRoot "$Name.env"
    $rows = @(
        "INFRASPINE_IMAGE=$Image",
        "POSTGRES_DB=$db",
        "POSTGRES_USER=$user",
        "POSTGRES_PASSWORD=$password",
        "DATABASE_URL=postgresql+asyncpg://${user}:${password}@postgres:5432/${db}",
        "MASTER_ENCRYPTION_KEY=$masterKey",
        "MASTER_ENCRYPTION_KEY_ID=key-$Name-$($suffix.Substring(0, 8))",
        "LICENSE_PUBLIC_KEY=$PublicKey",
        'LICENSE_ENFORCEMENT_ENABLED=true',
        'RAG_VECTOR_STORE=qdrant',
        'RAG_QDRANT_URL=http://qdrant:6333',
        "RAG_QDRANT_API_KEY=$qdrantKey",
        "INFRASPINE_NETWORK_SUBNET=$Subnet",
        'RAG_EMBEDDING_ALLOW_DOWNLOAD=false',
        "INFRASPINE_API_PORT=$port",
        "INFRASPINE_POSTGRES_DIR=$($paths.postgres)",
        "INFRASPINE_DATA_DIR=$($paths.data)",
        "INFRASPINE_LOG_DIR=$($paths.logs)",
        "INFRASPINE_BACKUP_DIR=$($paths.backups)",
        "INFRASPINE_QDRANT_DIR=$($paths.qdrant)",
        "INFRASPINE_IMPORT_DIR=$($paths.imports)"
    )
    [IO.File]::WriteAllLines($envPath, $rows, [Text.UTF8Encoding]::new($false))
    return [pscustomobject]@{
        Name = $Name; Label = $Label; Project = $project; Env = $envPath; Paths = $paths
        Database = $db; User = $user; DatabasePassword = $password; MasterKey = $masterKey
        QdrantKey = $qdrantKey; Port = $port; Subnet = $Subnet; Email = $email; AdminPassword = $adminPassword
        BaseUrl = "http://127.0.0.1:$port/"
    }
}

function Get-ComposeArgs($Customer) {
    return @('--context', 'desktop-linux', 'compose', '--project-name', $Customer.Project,
        '--env-file', $Customer.Env, '--file', $compose, '--profile', 'qdrant')
}

function Wait-Ready($Customer) {
    $deadline = [DateTime]::UtcNow.AddMinutes(3)
    do {
        try {
            $ready = Invoke-RestMethod -Uri ($Customer.BaseUrl + 'health/ready') -TimeoutSec 3
            if ($ready.status -eq 'ready' -and $ready.database -eq 'ok') { return }
        } catch { }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Synthetic customer $($Customer.Name) did not become ready."
}

function Invoke-QdrantMarker($Customer, [string]$Marker, [string]$Forbidden) {
    $code = 'import os,sys; from qdrant_client import QdrantClient,models; c=QdrantClient(url=os.environ["RAG_QDRANT_URL"],api_key=os.environ["RAG_QDRANT_API_KEY"]); n={x.name for x in c.get_collections().collections}; own=sys.argv[2]; forbidden=sys.argv[3]; c.create_collection(collection_name=own,vectors_config=models.VectorParams(size=4,distance=models.Distance.COSINE)) if own not in n else None; n={x.name for x in c.get_collections().collections}; assert own in n and forbidden not in n; print("QDRANT_ISOLATION=PASS")'
    $encodedCode = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($code))
    $decodeCode = 'exec(__import__(chr(98)+chr(97)+chr(115)+chr(101)+chr(54)+chr(52)).b64decode(__import__(chr(115)+chr(121)+chr(115)).argv[1]))'
    $base = Get-ComposeArgs $Customer
    Invoke-Docker ($base + @('exec', '-T', 'api', 'python', '-c', $decodeCode, $encodedCode, $Marker, $Forbidden)) 'Qdrant isolation assertion failed.'
}

try {
    Protect-TestRoot $tempRoot
    $imageId = & $docker --context desktop-linux image inspect --format '{{.Id}}' $Image 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $imageId) { throw 'The selected local application image is unavailable.' }

    $nodeCode = 'const c=require("node:crypto");const k=c.generateKeyPairSync("ed25519");const p=k.publicKey.export({type:"spki",format:"der"}).subarray(-32).toString("base64");console.log(JSON.stringify({public_key:p,private_key_pem:k.privateKey.export({type:"pkcs8",format:"pem"})}))'
    $encodedNodeCode = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($nodeCode))
    $keyResult = (& $node -e 'eval(Buffer.from(process.argv[1],String.fromCharCode(98,97,115,101,54,52)).toString())' $encodedNodeCode | ConvertFrom-Json)
    if (-not $keyResult.public_key -or -not $keyResult.private_key_pem) { throw 'Could not create the in-memory synthetic license issuer.' }

    $reservedSubnets = Get-ExistingDockerSubnets
    $a = New-Customer 'customer_a' 'Synthetic Customer A' $keyResult.public_key (New-UniqueDockerSubnet $reservedSubnets)
    $b = New-Customer 'customer_b' 'Synthetic Customer B' $keyResult.public_key (New-UniqueDockerSubnet $reservedSubnets)
    $aEmployeeEmail = "employee-$($suffix.Substring(0, 8))@qa.invalid"
    $aEmployeePassword = New-RandomHex 24
    if ($a.Port -eq $b.Port -or $a.Database -eq $b.Database -or $a.MasterKey -eq $b.MasterKey -or
        $a.QdrantKey -eq $b.QdrantKey -or $a.DatabasePassword -eq $b.DatabasePassword) {
        throw 'Generated customer settings were not unique.'
    }
    $fixture = [pscustomobject]@{
        issuer_private_key_pem = $keyResult.private_key_pem
        customer_a = [pscustomobject]@{ base_url = $a.BaseUrl; email = $a.Email; password = $a.AdminPassword; employee_email = $aEmployeeEmail; name = $a.Label; brand = 'Customer A QA'; industry = 'Manufacturing'; primary_color = '#25638A' }
        customer_b = [pscustomobject]@{ base_url = $b.BaseUrl; email = $b.Email; password = $b.AdminPassword; name = $b.Label; brand = 'Customer B QA'; industry = 'Education'; primary_color = '#7556A8' }
    }
    $fixturePath = Join-Path $tempRoot 'customer-install-fixture.json'
    $fixture | ConvertTo-Json -Depth 8 -Compress | Set-Content -LiteralPath $fixturePath -Encoding utf8
    $fixtureAcl = Get-Acl -LiteralPath $fixturePath
    $fixtureAcl.SetAccessRuleProtection($true, $false)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $fixtureAcl.SetAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $sid, [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow))
    Set-Acl -LiteralPath $fixturePath -AclObject $fixtureAcl

    foreach ($customer in @($a, $b)) {
        $base = Get-ComposeArgs $customer
        $started.Add($customer)
        Invoke-Docker ($base + @('up', '-d', '--no-build', '--wait', 'postgres', 'qdrant')) "Database/vector startup failed for synthetic $($customer.Name)."
    }
    foreach ($customer in @($a, $b)) {
        $base = Get-ComposeArgs $customer
        Invoke-Docker ($base + @('run', '--rm', '--no-deps', 'api', 'alembic', 'upgrade', 'head')) "Fresh database migration failed for synthetic $($customer.Name)."
    }
    foreach ($customer in @($a, $b)) {
        $base = Get-ComposeArgs $customer
        Invoke-Docker ($base + @('up', '-d', '--no-build', '--wait', 'api', 'worker')) "Application startup failed for synthetic $($customer.Name)."
        Wait-Ready $customer
        Invoke-Docker ($base + @('exec', '-T', 'worker', 'python', '-m', 'app.worker', '--check')) "Worker readiness failed for synthetic $($customer.Name)."
        $bootstrapArgs = $base + @('exec', '-T', 'api', 'python', '-m', 'app.cli', 'create-user', '--email', $customer.Email, '--role', 'admin', '--password-stdin')
        Invoke-DockerWithInput $bootstrapArgs $customer.AdminPassword "Synthetic administrator bootstrap failed for $($customer.Name)."
    }

    $employeeArgs = (Get-ComposeArgs $a) + @('exec', '-T', 'api', 'python', '-m', 'app.cli', 'create-user', '--email', $aEmployeeEmail, '--role', 'employee', '--password-stdin')
    Invoke-DockerWithInput $employeeArgs $aEmployeePassword 'Synthetic Employee bootstrap failed for Customer A.'
    $firstRunFixturePath = Join-Path $tempRoot 'first-run-browser-fixture.json'
    $firstRunFixture = [pscustomobject]@{
        admin = [pscustomobject]@{ email = $a.Email; password = $a.AdminPassword }
        employee = [pscustomobject]@{ email = $aEmployeeEmail; password = $aEmployeePassword }
        company = [pscustomobject]@{ company_name = $a.Label; industry = 'Manufacturing'; brand_name = 'Customer A QA'; primary_color = '#25638A'; setup_complete = $true }
    }
    $firstRunFixture | ConvertTo-Json -Depth 8 -Compress | Set-Content -LiteralPath $firstRunFixturePath -Encoding utf8
    $firstRunAcl = Get-Acl -LiteralPath $firstRunFixturePath
    $firstRunAcl.SetAccessRuleProtection($true, $false)
    $firstRunAcl.SetAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent().User,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow))
    Set-Acl -LiteralPath $firstRunFixturePath -AclObject $firstRunAcl
    $env:INFRASPINE_FIRST_RUN_FIXTURE = $firstRunFixturePath
    & $node (Join-Path $PSScriptRoot 'browser_first_run_setup_acceptance.mjs') $a.BaseUrl
    if ($LASTEXITCODE -ne 0) { throw 'Fresh-install first-run browser acceptance failed.' }

    $env:INFRASPINE_CUSTOMER_INSTALL_FIXTURE = $fixturePath
    & $node (Join-Path $PSScriptRoot 'verify_two_customer_installations.mjs')
    if ($LASTEXITCODE -ne 0) { throw 'Authenticated two-customer API/setup/licensing isolation checks failed.' }

    $markerA = "synthetic-customer-a-$suffix"
    $markerB = "synthetic-customer-b-$suffix"
    Invoke-QdrantMarker $a $markerA $markerB
    Invoke-QdrantMarker $b $markerB $markerA
    [IO.File]::WriteAllText((Join-Path $a.Paths.data "$markerA.txt"), 'synthetic only')
    [IO.File]::WriteAllText((Join-Path $a.Paths.backups "$markerA.txt"), 'synthetic only')
    [IO.File]::WriteAllText((Join-Path $b.Paths.data "$markerB.txt"), 'synthetic only')
    [IO.File]::WriteAllText((Join-Path $b.Paths.backups "$markerB.txt"), 'synthetic only')
    if (-not (Test-Path (Join-Path $a.Paths.data "$markerA.txt")) -or
        (Test-Path (Join-Path $b.Paths.data "$markerA.txt")) -or
        -not (Test-Path (Join-Path $b.Paths.data "$markerB.txt")) -or
        (Test-Path (Join-Path $a.Paths.data "$markerB.txt")) -or
        (Test-Path (Join-Path $b.Paths.backups "$markerA.txt")) -or
        (Test-Path (Join-Path $a.Paths.backups "$markerB.txt"))) {
        throw 'Synthetic customer file/backup marker crossed installation paths.'
    }

    Write-Host "CUSTOMER_INSTALLS_SAME_IMAGE=PASS image=$Image image_id=$imageId"
    Write-Host 'CUSTOMER_POSTGRES_QDRANT_FILES_USERS_KEYS_BRANDING_LICENSES=PASS'
    Write-Host "CUSTOMER_INSTALL_URLS=$($a.BaseUrl),$($b.BaseUrl) (loopback; synthetic only)"
    Write-Host "CUSTOMER_TEST_FIXTURE=protected local file under $tempRoot; no credentials were printed"
} finally {
    [Environment]::SetEnvironmentVariable('INFRASPINE_CUSTOMER_INSTALL_FIXTURE', $previousFixture, 'Process')
    [Environment]::SetEnvironmentVariable('INFRASPINE_FIRST_RUN_FIXTURE', $previousFirstRunFixture, 'Process')
    foreach ($customer in $started) {
        try {
            $base = Get-ComposeArgs $customer
            Invoke-Docker ($base + @('stop', 'api', 'worker', 'qdrant', 'postgres')) 'Could not stop a synthetic customer test stack; it remains isolated.'
        } catch { Write-Warning 'A generated synthetic customer stack is still running; its unique project name is recorded in the local test folder.' }
    }
}
