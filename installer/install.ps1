# install.ps1 -- console installer for MEWracle.
#
# A thin wrapper over installer_core.ps1 (the shared engine, also used by the
# button GUI in setup_gui.ps1). Double-click Install.bat to run this; for a
# window with Install / Update / Repair buttons, use Setup.bat instead.
#
# Re-running it is safe: it reuses an existing .venv and just refreshes things.
# Updating the app itself is git pull (update.bat / the "Update" shortcut), so
# this installer never goes stale between commits.

$ErrorActionPreference = "Stop"
# The app folder is this script's parent: the installer lives in installer\.
$root = Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot "installer_core.ps1")

# Route the engine's progress to the console with the original colour scheme.
$consoleLog = {
    param($msg, $level)
    switch ("$level") {
        'Ok'   { Write-Host "[OK] $msg" -ForegroundColor Green }
        'Warn' { Write-Host "[!]  $msg" -ForegroundColor Yellow }
        'Err'  { Write-Host "[X]  $msg" -ForegroundColor Red }
        'Head' {
            Write-Host ""
            Write-Host "==============================================="
            Write-Host "  $msg"
            Write-Host "==============================================="
            Write-Host ""
        }
        default { Write-Host $msg }
    }
}

try {
    Invoke-AppSetup -Root $root -Mode Install -Log $consoleLog
    Write-Host ""
    & $consoleLog "Launch it from the Desktop shortcut or by double-clicking MEWracle.bat." 'Info'
    & $consoleLog "Get the latest version anytime with the 'Update' shortcut, update.bat, or Setup.bat." 'Info'
    Write-Host ""
} catch {
    & $consoleLog $_.Exception.Message 'Err'
    exit 1
}
