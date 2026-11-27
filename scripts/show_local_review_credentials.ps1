param([string]$FixturePath = '')

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($FixturePath)) {
    $FixturePath = Join-Path $repoRoot '.verification-local-review-20261027-native\f07-synthetic-role-users.json'
} elseif (-not [IO.Path]::IsPathRooted($FixturePath)) {
    $FixturePath = Join-Path $repoRoot $FixturePath
}
$fixture = [IO.Path]::GetFullPath($FixturePath)

try {
    if (-not (Test-Path -LiteralPath $fixture -PathType Leaf)) {
        throw 'The protected synthetic account fixture is unavailable.'
    }
    $acl = Get-Acl -LiteralPath $fixture
    if (-not $acl.AreAccessRulesProtected) {
        throw 'The account fixture ACL is not protected.'
    }
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $allowedSids = @($currentSid, 'S-1-5-18', 'S-1-5-32-544')
    $rules = @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
    if (-not $rules -or @($rules | Where-Object { $_.IdentityReference.Value -notin $allowedSids }).Count -gt 0) {
        throw 'The fixture ACL is broader than the current user and local system administrators.'
    }

    $fixtureData = [IO.File]::ReadAllText($fixture) | ConvertFrom-Json
    $accounts = @()
    if (-not [string]::IsNullOrWhiteSpace([string]$fixtureData.admin.email) -and -not [string]::IsNullOrWhiteSpace([string]$fixtureData.admin.password)) {
        $accounts += [pscustomobject]@{ role = 'Admin'; email = [string]$fixtureData.admin.email; password = [string]$fixtureData.admin.password }
    }
    foreach ($account in @($fixtureData.managers)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$account.email) -and -not [string]::IsNullOrWhiteSpace([string]$account.password)) {
            $accounts += [pscustomobject]@{ role = 'Manager'; email = [string]$account.email; password = [string]$account.password }
        }
    }
    foreach ($account in @($fixtureData.employees)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$account.email) -and -not [string]::IsNullOrWhiteSpace([string]$account.password)) {
            $accounts += [pscustomobject]@{ role = 'Employee'; email = [string]$account.email; password = [string]$account.password }
        }
    }
    foreach ($account in @($fixtureData.partners)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$account.email) -and -not [string]::IsNullOrWhiteSpace([string]$account.password) -and -not [string]::IsNullOrWhiteSpace([string]$account.workspace_slug)) {
            $accounts += [pscustomobject]@{ role = "Partner ($($account.workspace_slug))"; email = [string]$account.email; password = [string]$account.password }
        }
    }
    if ($accounts.Count -lt 1) { throw 'The protected fixture contains no complete synthetic accounts.' }

    $form = New-Object Windows.Forms.Form
    $form.Text = 'InfraSpine AI - Local Synthetic Review'
    $form.StartPosition = 'CenterScreen'
    $form.Size = New-Object Drawing.Size(560, 285)
    $form.MinimumSize = $form.Size
    $form.TopMost = $true
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false

    $title = New-Object Windows.Forms.Label
    $title.Text = 'Local review account (synthetic data only)'
    $title.Location = New-Object Drawing.Point(18, 16)
    $title.Size = New-Object Drawing.Size(475, 24)
    $title.Font = New-Object Drawing.Font('Segoe UI', 10, [Drawing.FontStyle]::Bold)
    $form.Controls.Add($title)

    $emailLabel = New-Object Windows.Forms.Label
    $emailLabel.Text = 'Account'
    $emailLabel.Location = New-Object Drawing.Point(18, 58)
    $emailLabel.Size = New-Object Drawing.Size(100, 24)
    $form.Controls.Add($emailLabel)

    $accountBox = New-Object Windows.Forms.ComboBox
    $accountBox.DropDownStyle = [Windows.Forms.ComboBoxStyle]::DropDownList
    $accountBox.Location = New-Object Drawing.Point(120, 55)
    $accountBox.Size = New-Object Drawing.Size(410, 26)
    foreach ($account in $accounts) { [void]$accountBox.Items.Add("$($account.role) - $($account.email)") }
    $form.Controls.Add($accountBox)

    $emailLabel = New-Object Windows.Forms.Label
    $emailLabel.Text = 'Email'
    $emailLabel.Location = New-Object Drawing.Point(18, 96)
    $emailLabel.Size = New-Object Drawing.Size(100, 24)
    $form.Controls.Add($emailLabel)

    $emailBox = New-Object Windows.Forms.TextBox
    $emailBox.ReadOnly = $true
    $emailBox.Location = New-Object Drawing.Point(120, 93)
    $emailBox.Size = New-Object Drawing.Size(410, 26)
    $form.Controls.Add($emailBox)

    $passwordLabel = New-Object Windows.Forms.Label
    $passwordLabel.Text = 'Password'
    $passwordLabel.Location = New-Object Drawing.Point(18, 134)
    $passwordLabel.Size = New-Object Drawing.Size(100, 24)
    $form.Controls.Add($passwordLabel)

    $passwordBox = New-Object Windows.Forms.TextBox
    $passwordBox.Text = $adminPassword
    $passwordBox.ReadOnly = $true
    $passwordBox.UseSystemPasswordChar = $true
    $passwordBox.Location = New-Object Drawing.Point(120, 131)
    $passwordBox.Size = New-Object Drawing.Size(410, 26)
    $form.Controls.Add($passwordBox)

    $showPassword = New-Object Windows.Forms.CheckBox
    $showPassword.Text = 'Show password on this screen'
    $showPassword.Location = New-Object Drawing.Point(120, 166)
    $showPassword.Size = New-Object Drawing.Size(250, 24)
    $showPassword.Add_CheckedChanged({ $passwordBox.UseSystemPasswordChar = -not $showPassword.Checked })
    $form.Controls.Add($showPassword)

    $accountBox.Add_SelectedIndexChanged({
        $selectedAccount = $accounts[$accountBox.SelectedIndex]
        $emailBox.Text = $selectedAccount.email
        $passwordBox.Text = $selectedAccount.password
    })
    $accountBox.SelectedIndex = 0

    $note = New-Object Windows.Forms.Label
    $note.Text = 'Synthetic accounts only. No clipboard, file, network, or log export. Close this window when finished.'
    $note.Location = New-Object Drawing.Point(18, 205)
    $note.Size = New-Object Drawing.Size(500, 34)
    $form.Controls.Add($note)

    [void]$form.ShowDialog()
}
catch {
    [Windows.Forms.MessageBox]::Show(
        'The protected synthetic account fixture could not be opened. No account was changed.',
        'InfraSpine local review credentials',
        [Windows.Forms.MessageBoxButtons]::OK,
        [Windows.Forms.MessageBoxIcon]::Warning
    ) | Out-Null
}
finally {
    if ($null -ne $fixtureData) {
        $fixtureData.admin.password = $null
        foreach ($account in @($fixtureData.managers) + @($fixtureData.employees) + @($fixtureData.partners)) { if ($null -ne $account) { $account.password = $null } }
    }
    if ($null -ne $accounts) { foreach ($account in $accounts) { $account.password = $null } }
    if ($null -ne $passwordBox) { $passwordBox.Dispose() }
    if ($null -ne $form) { $form.Dispose() }
}
