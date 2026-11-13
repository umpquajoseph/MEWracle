# setup_gui.ps1 -- a small WinForms front-end for the MEWracle installer.
#
# Three buttons -- Install, Update, Repair -- over the shared engine in
# installer_core.ps1, with a live log box and a busy indicator. Launch it with
# Setup.bat.
#
# Why WinForms and not the app's own tkinter GUI: this IS the bootstrapper that
# installs Python, so it must run before Python exists. WinForms ships with
# Windows .NET, so the installer has zero prerequisites of its own.
#
# The chosen operation runs on a background runspace and streams its progress
# through a thread-safe queue that a UI timer drains into the log box, so the
# window stays responsive during the (large) vtk download.
#
# Run with -SelfTest to build the window and exit immediately (smoke test).

param([switch]$SelfTest)

$ErrorActionPreference = "Stop"
# The app folder is this script's parent: the installer lives in installer\.
$root = Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot "installer_core.ps1")
$appName = Resolve-AppName $root

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# --- shared state between the UI thread and the worker runspace --------------
$sync = [hashtable]::Synchronized(@{
    Queue   = [System.Collections.Concurrent.ConcurrentQueue[object]]::new()
    Running = $false
    Error   = $null
})
$script:ps     = $null
$script:handle = $null

# --- window ------------------------------------------------------------------
$form = New-Object System.Windows.Forms.Form
$form.Text = "$appName Setup"
$form.Size = New-Object System.Drawing.Size(660, 500)
$form.MinimumSize = New-Object System.Drawing.Size(520, 380)
$form.StartPosition = "CenterScreen"
$form.Font = New-Object System.Drawing.Font("Segoe UI", 9)
try { $form.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon((Join-Path $root ".venv\Scripts\pythonw.exe")) } catch {}

$cw = $form.ClientSize.Width
$ch = $form.ClientSize.Height

$header = New-Object System.Windows.Forms.Label
$header.Text = $appName
$header.Font = New-Object System.Drawing.Font("Segoe UI", 15, [System.Drawing.FontStyle]::Bold)
$header.AutoSize = $true
$header.Location = New-Object System.Drawing.Point(16, 12)
$form.Controls.Add($header)

$status = New-Object System.Windows.Forms.Label
$status.AutoSize = $true
$status.Location = New-Object System.Drawing.Point(18, 46)
$status.ForeColor = [System.Drawing.Color]::DimGray
$form.Controls.Add($status)

# --- action buttons ----------------------------------------------------------
function New-ActionButton($text, $x, $tip) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text
    $b.Size = New-Object System.Drawing.Size(130, 36)
    $b.Location = New-Object System.Drawing.Point($x, 78)
    $b.Anchor = "Top,Left"
    $tt = New-Object System.Windows.Forms.ToolTip
    $tt.SetToolTip($b, $tip)
    $form.Controls.Add($b)
    return $b
}

$btnInstall = New-ActionButton "Install"  16  "Set up Python, the .venv environment, dependencies and shortcuts. Safe to re-run."
$btnUpdate  = New-ActionButton "Update"  156  "git pull the latest version and refresh dependencies."
$btnRepair  = New-ActionButton "Repair"  296  "Delete the .venv environment and reinstall everything from scratch."

# --- log box -----------------------------------------------------------------
$log = New-Object System.Windows.Forms.RichTextBox
$log.Location = New-Object System.Drawing.Point(16, 126)
$log.Size = New-Object System.Drawing.Size(($cw - 32), ($ch - 126 - 48))
$log.Anchor = "Top,Bottom,Left,Right"
$log.ReadOnly = $true
$log.BackColor = [System.Drawing.Color]::White
$log.Font = New-Object System.Drawing.Font("Consolas", 9)
$log.DetectUrls = $false
$form.Controls.Add($log)

# --- busy indicator + close --------------------------------------------------
$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Location = New-Object System.Drawing.Point(16, ($ch - 38))
$progress.Size = New-Object System.Drawing.Size(($cw - 32 - 110), 20)
$progress.Anchor = "Bottom,Left,Right"
$progress.Style = "Blocks"
$form.Controls.Add($progress)

$btnClose = New-Object System.Windows.Forms.Button
$btnClose.Text = "Close"
$btnClose.Size = New-Object System.Drawing.Size(96, 26)
$btnClose.Location = New-Object System.Drawing.Point(($cw - 112), ($ch - 41))
$btnClose.Anchor = "Bottom,Right"
$btnClose.Add_Click({ $form.Close() })
$form.Controls.Add($btnClose)

# --- UI helpers (UI thread only) ---------------------------------------------
function Add-Log($text, $level) {
    $color = switch ("$level") {
        'Ok'   { [System.Drawing.Color]::ForestGreen }
        'Warn' { [System.Drawing.Color]::DarkOrange }
        'Err'  { [System.Drawing.Color]::Firebrick }
        'Head' { [System.Drawing.Color]::RoyalBlue }
        default { [System.Drawing.Color]::Black }
    }
    $prefix = switch ("$level") {
        'Ok'   { "[OK] " }
        'Warn' { "[!]  " }
        'Err'  { "[X]  " }
        default { "" }
    }
    $log.SelectionStart = $log.TextLength
    $log.SelectionLength = 0
    $log.SelectionColor = $color
    $log.AppendText("$prefix$text`r`n")
    $log.SelectionColor = $log.ForeColor
    $log.ScrollToCaret()
}

function Set-Busy($busy) {
    $btnInstall.Enabled = -not $busy
    $btnUpdate.Enabled  = -not $busy
    $btnRepair.Enabled  = -not $busy
    if ($busy) {
        $progress.Style = "Marquee"
        $progress.MarqueeAnimationSpeed = 30
    } else {
        $progress.MarqueeAnimationSpeed = 0
        $progress.Style = "Blocks"
        $progress.Value = 0
    }
}

function Update-StateLabel {
    $venvPy = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        $status.Text = "Installed  --  .venv is present. Use Update for the latest version, or Repair if something is broken."
        $status.ForeColor = [System.Drawing.Color]::ForestGreen
    } else {
        $status.Text = "Not installed yet  --  click Install to set everything up."
        $status.ForeColor = [System.Drawing.Color]::DimGray
    }
}

# --- worker plumbing ---------------------------------------------------------
function Start-Operation($mode) {
    if ($sync.Running) { return }
    $sync.Running = $true
    $sync.Error   = $null
    Set-Busy $true

    $rs = [runspacefactory]::CreateRunspace()
    $rs.ApartmentState = "STA"
    $rs.ThreadOptions  = "ReuseThread"
    $rs.Open()
    $rs.SessionStateProxy.SetVariable("Sync",      $sync)
    $rs.SessionStateProxy.SetVariable("Root",      $root)
    $rs.SessionStateProxy.SetVariable("Mode",      $mode)

    $work = {
        # A fresh runspace defaults to 'Continue'; match the console installer so
        # a failed Remove-Item / venv create surfaces as a caught error, not a
        # silent skip. (Native stderr merged via 2>&1 stays data, so git/pip
        # writing to stderr does not trip this.)
        $ErrorActionPreference = "Stop"
        # Enqueue every log line; the UI timer drains the queue and paints it.
        $log = { param($m, $lvl = 'Info') $Sync.Queue.Enqueue([pscustomobject]@{ Msg = [string]$m; Level = "$lvl" }) }
        try {
            . (Join-Path $Root "installer\installer_core.ps1")
            switch ($Mode) {
                'Install' { Invoke-AppSetup  -Root $Root -Mode Install -Log $log }
                'Repair'  { Invoke-AppSetup  -Root $Root -Mode Repair  -Log $log }
                'Update'  { Invoke-AppUpdate -Root $Root -Log $log }
            }
        } catch {
            $Sync.Error = $_.Exception.Message
            $Sync.Queue.Enqueue([pscustomobject]@{ Msg = $_.Exception.Message; Level = 'Err' })
        }
    }

    $script:ps = [powershell]::Create()
    $script:ps.Runspace = $rs
    $script:ps.AddScript($work) | Out-Null
    $script:handle = $script:ps.BeginInvoke()
    $timer.Start()
}

# Drain the log queue and detect completion (runs on the UI thread).
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 120
$timer.Add_Tick({
    $item = $null
    while ($sync.Queue.TryDequeue([ref]$item)) { Add-Log $item.Msg $item.Level }

    if ($script:handle -and $script:handle.IsCompleted) {
        $timer.Stop()
        try { $script:ps.EndInvoke($script:handle) } catch { Add-Log $_.Exception.Message 'Err' }
        while ($sync.Queue.TryDequeue([ref]$item)) { Add-Log $item.Msg $item.Level }  # final flush

        $err = $sync.Error
        $rs  = $script:ps.Runspace
        $script:ps.Dispose()
        if ($rs) { $rs.Close() }
        $script:ps = $null
        $script:handle = $null
        $sync.Running = $false

        Set-Busy $false
        if ($err) {
            $status.Text = "Last action failed -- see the log above."
            $status.ForeColor = [System.Drawing.Color]::Firebrick
        } else {
            Update-StateLabel
        }
    }
})

# --- button wiring -----------------------------------------------------------
$btnInstall.Add_Click({ Start-Operation 'Install' })
$btnUpdate.Add_Click({ Start-Operation 'Update' })
$btnRepair.Add_Click({
    $r = [System.Windows.Forms.MessageBox]::Show(
        "Repair deletes the .venv environment and reinstalls everything from scratch. Your code and settings are untouched. Continue?",
        "Repair $appName",
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Question)
    if ($r -eq [System.Windows.Forms.DialogResult]::Yes) { Start-Operation 'Repair' }
})

# Don't let the window close mid-operation (the runspace would be orphaned).
$form.Add_FormClosing({
    if ($sync.Running) {
        $_.Cancel = $true
        [System.Windows.Forms.MessageBox]::Show(
            "An operation is still running. Please wait for it to finish.",
            "$appName Setup",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
    }
})

$form.Add_Shown({ Update-StateLabel })

if ($SelfTest) {
    Update-StateLabel
    Write-Host "setup_gui self-test OK: window built, status = '$($status.Text)'"
    $form.Dispose()
    return
}

[void]$form.ShowDialog()
