Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = 'Stop'
$fixture = Join-Path $env:LOCALAPPDATA 'Temp\infraspine-qa-29a24ed3\qa-users.json'

try {
    if (-not (Test-Path -LiteralPath $fixture -PathType Leaf)) {
        throw 'The protected synthetic account fixture is unavailable.'
    }

    $acl = Get-Acl -LiteralPath $fixture
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if (-not $acl.AreAccessRulesProtected) {
        throw 'The synthetic account fixture does not have a protected ACL.'
    }
    $rules = @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
    if (-not $rules -or @($rules | Where-Object {
        $_.IdentityReference.Value -ne $currentSid -and
        $_.IdentityReference.Value -ne 'S-1-5-18' -and
        $_.IdentityReference.Value -ne 'S-1-5-32-544'
    }).Count -gt 0) {
        throw 'The synthetic account fixture ACL is broader than the current user and local system administrators.'
    }

    $fixtureData = [IO.File]::ReadAllText($fixture) | ConvertFrom-Json
    $adminEmail = [string]$fixtureData.admin.email
    $adminPassword = [string]$fixtureData.admin.password
    if ([string]::IsNullOrWhiteSpace($adminEmail) -or [string]::IsNullOrWhiteSpace($adminPassword)) {
        throw 'The synthetic fixture does not contain an Admin account.'
    }

    $form = New-Object Windows.Forms.Form
    $form.Text = 'InfraSpine AI — Synthetic QA Admin'
    $form.StartPosition = 'CenterScreen'
    $form.Size = New-Object Drawing.Size(530, 235)
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
    $emailLabel.Text = 'Admin email'
    $emailLabel.Location = New-Object Drawing.Point(18, 58)
    $emailLabel.Size = New-Object Drawing.Size(100, 24)
    $form.Controls.Add($emailLabel)

    $emailBox = New-Object Windows.Forms.TextBox
    $emailBox.Text = $adminEmail
    $emailBox.ReadOnly = $true
    $emailBox.Location = New-Object Drawing.Point(120, 55)
    $emailBox.Size = New-Object Drawing.Size(365, 26)
    $form.Controls.Add($emailBox)

    $passwordLabel = New-Object Windows.Forms.Label
    $passwordLabel.Text = 'Admin password'
    $passwordLabel.Location = New-Object Drawing.Point(18, 96)
    $passwordLabel.Size = New-Object Drawing.Size(100, 24)
    $form.Controls.Add($passwordLabel)

    $passwordBox = New-Object Windows.Forms.TextBox
    $passwordBox.Text = $adminPassword
    $passwordBox.ReadOnly = $true
    $passwordBox.UseSystemPasswordChar = $true
    $passwordBox.Location = New-Object Drawing.Point(120, 93)
    $passwordBox.Size = New-Object Drawing.Size(365, 26)
    $form.Controls.Add($passwordBox)

    $showPassword = New-Object Windows.Forms.CheckBox
    $showPassword.Text = 'Show password on this screen'
    $showPassword.Location = New-Object Drawing.Point(120, 126)
    $showPassword.Size = New-Object Drawing.Size(250, 24)
    $showPassword.Add_CheckedChanged({ $passwordBox.UseSystemPasswordChar = -not $showPassword.Checked })
    $form.Controls.Add($showPassword)

    $note = New-Object Windows.Forms.Label
    $note.Text = 'No clipboard, file, network, or log export is provided. Close this window when finished.'
    $note.Location = New-Object Drawing.Point(18, 157)
    $note.Size = New-Object Drawing.Size(475, 34)
    $form.Controls.Add($note)

    [void]$form.ShowDialog()
}
catch {
    [Windows.Forms.MessageBox]::Show(
        'The protected synthetic Admin fixture could not be opened. No account was changed.',
        'InfraSpine QA credentials',
        [Windows.Forms.MessageBoxButtons]::OK,
        [Windows.Forms.MessageBoxIcon]::Warning
    ) | Out-Null
}
finally {
    if ($null -ne $fixtureData) { $fixtureData.admin.password = $null }
    if ($null -ne $passwordBox) { $passwordBox.Dispose() }
    if ($null -ne $form) { $form.Dispose() }
}
