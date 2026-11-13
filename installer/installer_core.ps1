# installer_core.ps1 -- shared engine for the MEWracle installer.
#
# Dot-sourced by BOTH front-ends:
#   * install.ps1   -- the console installer (double-click Install.bat)
#   * setup_gui.ps1 -- the button GUI         (double-click Setup.bat)
#
# So the "how to install / update / repair" logic lives in exactly one place.
# Every operation takes a -Log callback ({ param($msg,$level) ... }) so its
# progress can be routed to a console (coloured Write-Host) or to the GUI's log
# box, and it THROWS on a fatal error instead of calling exit -- the GUI needs
# to catch the failure and stay open rather than have the process die.
#
# Levels passed to -Log: 'Info' (default), 'Ok', 'Warn', 'Err', 'Head'.

# --- app name (read from installer\APP_NAME, with a fallback) ---
function Resolve-AppName($root) {
    $appName = "MEWracle"
    $f = Join-Path $root "installer\APP_NAME"
    if (Test-Path $f) {
        $n = (Get-Content $f -First 1).Trim()
        if ($n) { $appName = $n }
    }
    return $appName
}

# --- Python discovery (3.9+ with tkinter; pip resolves the right vtk wheel) ---
function Test-AppPython($exe, $verArgs) {
    try {
        $out = & $exe @verArgs "-c" "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $parts = $out.Trim().Split(".")
        $maj = [int]$parts[0]; $min = [int]$parts[1]
        if ($maj -eq 3 -and $min -ge 9) {
            # tkinter ships with the python.org installer; confirm it is present
            & $exe @verArgs "-c" "import tkinter" 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { return $null }
            return @{ Exe = $exe; Args = $verArgs; Version = "$maj.$min" }
        }
    } catch { }
    return $null
}

function Find-AppPython {
    # Prefer the py launcher pinned to a known-good version, newest first.
    foreach ($v in @("-3.13", "-3.12", "-3.11", "-3.10", "-3.9")) {
        $p = Test-AppPython "py" @($v)
        if ($p) { return $p }
    }
    foreach ($cmd in @("python", "python3")) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            $p = Test-AppPython $cmd @()
            if ($p) { return $p }
        }
    }
    return $null
}

# Find a usable Python, installing one via winget if none is present.
function Install-AppPython($Log) {
    $py = Find-AppPython
    if ($py) { & $Log "Using Python $($py.Version)" 'Ok'; return $py }

    & $Log "No suitable Python (3.9+) with tkinter was found." 'Warn'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & $Log "Installing Python 3.12 via winget (this can take a minute)..." 'Info'
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --silent 2>&1 |
            ForEach-Object { & $Log ([string]$_) 'Info' }
        $py = Find-AppPython
        if (-not $py) {
            throw "Python was installed but isn't visible yet. Close this window and run the installer again."
        }
        & $Log "Using Python $($py.Version)" 'Ok'
        return $py
    }

    Start-Process "https://www.python.org/downloads/windows/"
    throw "Python 3 is required. The download page was opened -- install Python 3.12 (tick 'Add python.exe to PATH'), then run the installer again."
}

# --- Desktop + Start Menu shortcuts ------------------------------------------
# The app's Windows taskbar identity. The LAUNCH shortcuts carry this so the
# launched window groups under the pinned icon rather than spawning a second
# taskbar button. It MUST match mewracle_gui.py's _APP_USER_MODEL_ID verbatim.
$script:AppUserModelId = "MEWracle.PhysicsBuild"

# Stamp System.AppUserModel.ID onto a .lnk via the shell property store (there
# is no WScript.Shell property for it). Guarded by the caller: if this fails the
# shortcut still launches, the window just won't merge under the pinned icon.
function Set-ShortcutAppId($lnkPath, $appid) {
    if (-not ([System.Management.Automation.PSTypeName]'MEWracle.Lnk').Type) {
        Add-Type -Namespace MEWracle -Name Lnk -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)]
public struct PropertyKey { public System.Guid fmtid; public int pid; }
[StructLayout(LayoutKind.Explicit)]
public struct PropVariant { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public System.IntPtr p; }
[ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IPropertyStore {
    int GetCount(out uint c);
    int GetAt(uint i, out PropertyKey k);
    int GetValue(ref PropertyKey k, out PropVariant v);
    int SetValue(ref PropertyKey k, ref PropVariant v);
    int Commit();
}
[DllImport("shell32.dll", CharSet=CharSet.Unicode, PreserveSig=false)]
public static extern void SHGetPropertyStoreFromParsingName(
    string path, System.IntPtr pbc, int flags, ref System.Guid riid, out IPropertyStore ps);
[DllImport("ole32.dll")]
public static extern int PropVariantClear(ref PropVariant pv);
public static void SetAppId(string lnk, string appid) {
    var iid = new System.Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99");
    IPropertyStore ps;
    SHGetPropertyStoreFromParsingName(lnk, System.IntPtr.Zero, 0x2, ref iid, out ps); // GPS_READWRITE
    var key = new PropertyKey {
        fmtid = new System.Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
    var pv = new PropVariant();
    pv.vt = 31;                                    // VT_LPWSTR
    pv.p  = Marshal.StringToCoTaskMemUni(appid);   // freed by PropVariantClear
    ps.SetValue(ref key, ref pv);
    ps.Commit();
    PropVariantClear(ref pv);
    Marshal.ReleaseComObject(ps);
}
'@
    }
    [MEWracle.Lnk]::SetAppId($lnkPath, $appid)
}

function New-AppShortcuts($root, $appName) {
    $venv   = Join-Path $root ".venv"
    $updBat = Join-Path $root "update.bat"
    $setBat = Join-Path $root "Setup.bat"
    $pyw    = Join-Path $venv "Scripts\pythonw.exe"   # launch target (+ fallback icon)

    # Brand the launch shortcuts with the MEWracle scaffold icon. It ships in this
    # app root's own Logo\ folder; older checkouts kept it in the build's sibling
    # Logo\ one level up. Fall back to pythonw.exe's own icon if neither shipped.
    $appIco = $pyw
    foreach ($cand in @((Join-Path $root "Logo\mewracle_scaffold_icon_v4.ico"),
                        (Join-Path (Split-Path $root -Parent) "Logo\mewracle_scaffold_icon_v4.ico"))) {
        if (Test-Path $cand) { $appIco = $cand; break }
    }

    # Modern-UI entry point (physics build); fall back to the classic entry so a
    # build without mewracle_gui.py still gets a working launcher.
    $entry = Join-Path $root "mewracle_gui.py"
    if (-not (Test-Path $entry)) { $entry = Join-Path $root "gui\gcode_to_fibers_gui.py" }
    $entryArg = '"' + $entry + '"'

    # $target may be pythonw (with $arguments) for launch, or a .bat for admin
    # actions. $appid, when set, is stamped so the window groups under the pin.
    $mk = {
        param($linkPath, $target, $arguments, $iconPath, $desc, $appid)
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($linkPath)
        $sc.TargetPath = $target
        if ($arguments) { $sc.Arguments = $arguments }
        $sc.WorkingDirectory = $root
        if ($iconPath -and (Test-Path $iconPath)) { $sc.IconLocation = $iconPath }
        $sc.Description = $desc
        $sc.Save()
        if ($appid) { try { Set-ShortcutAppId $linkPath $appid } catch { } }
    }

    $desktop  = [Environment]::GetFolderPath("Desktop")
    $startDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$appName"
    if (-not (Test-Path $startDir)) { New-Item -ItemType Directory -Path $startDir -Force | Out-Null }

    # Launch shortcuts target pythonw.exe directly: a pinned real exe launches on
    # click, whereas a pinned .bat only flashes and dies.
    & $mk (Join-Path $desktop  "$appName.lnk") $pyw $entryArg $appIco "Launch $appName" $script:AppUserModelId
    & $mk (Join-Path $startDir "$appName.lnk") $pyw $entryArg $appIco "Launch $appName" $script:AppUserModelId
    # Admin shortcuts keep their .bat targets (run in a console, not pinned).
    & $mk (Join-Path $startDir "Update $appName.lnk") $updBat $null $pyw "Pull the latest $appName and refresh dependencies" $null
    if (Test-Path $setBat) {
        & $mk (Join-Path $startDir "Setup $appName.lnk") $setBat $null $pyw "Install / Update / Repair $appName" $null
    }
}

# --- Install / Repair --------------------------------------------------------
# Mode 'Install' reuses an existing .venv; 'Repair' deletes it first so the
# whole environment is rebuilt from scratch.
function Invoke-AppSetup {
    param(
        [Parameter(Mandatory)] [string]      $Root,
        [ValidateSet("Install","Repair")] [string] $Mode = "Install",
        [Parameter(Mandatory)] [scriptblock] $Log
    )

    $appName = Resolve-AppName $Root
    & $Log "Setting up $appName  ($Mode)" 'Head'

    $venv   = Join-Path $Root ".venv"
    $venvPy = Join-Path $venv "Scripts\python.exe"

    # Repair: wipe the environment so it is rebuilt cleanly.
    if ($Mode -eq "Repair" -and (Test-Path $venv)) {
        & $Log "Repair: removing the existing .venv ..." 'Info'
        Remove-Item -Recurse -Force $venv
    }

    # 1. Python
    $py = Install-AppPython $Log

    # 2. virtual environment
    if (-not (Test-Path $venvPy)) {
        & $Log "Creating an isolated environment in .venv ..." 'Info'
        & $py.Exe @($py.Args) "-m" "venv" $venv
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { throw "Could not create the virtual environment." }
        & $Log "Environment created." 'Ok'
    } else {
        & $Log "Reusing existing .venv" 'Ok'
    }

    # 3. dependencies
    & $Log "Upgrading pip ..." 'Info'
    & $venvPy "-m" "pip" "install" "--upgrade" "pip" "--quiet" 2>&1 | ForEach-Object { & $Log ([string]$_) 'Info' }

    & $Log "Installing core dependencies (numpy, vtk) -- vtk is ~100 MB, please wait ..." 'Info'
    & $venvPy "-m" "pip" "install" "-r" (Join-Path $Root "requirements.txt") 2>&1 | ForEach-Object { & $Log ([string]$_) 'Info' }
    if ($LASTEXITCODE -ne 0) { throw "Failed to install core dependencies. Check your internet connection and try again." }
    & $Log "Core dependencies installed." 'Ok'

    # 4. git check (needed for one-click updates)
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        & $Log "git is not installed -- one-click Update won't be able to pull new commits." 'Warn'
    } elseif (-not (Test-Path (Join-Path $Root ".git"))) {
        & $Log "This folder is not a git clone (looks like a downloaded ZIP); Update can't pull here." 'Warn'
    }

    # 5. shortcuts
    try {
        New-AppShortcuts $Root $appName
        & $Log "Created Desktop and Start Menu shortcuts." 'Ok'
    } catch {
        & $Log "Could not create shortcuts ($($_.Exception.Message)). You can still launch with MEWracle.bat." 'Warn'
    }

    & $Log "$appName is ready." 'Ok'
}

# --- Update (git pull + refresh deps) ----------------------------------------
function Invoke-AppUpdate {
    param(
        [Parameter(Mandatory)] [string]      $Root,
        [Parameter(Mandatory)] [scriptblock] $Log
    )

    $appName = Resolve-AppName $Root
    & $Log "Updating $appName to the latest version" 'Head'

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "git is not installed, so this copy cannot auto-update. Install git from https://git-scm.com/download/win, then use Install or Repair."
    }
    if (-not (Test-Path (Join-Path $Root ".git"))) {
        throw "This folder is not a git clone, so there is nothing to pull. Clone the repo with git to enable one-click updates."
    }

    & $Log "Running git pull ..." 'Info'
    Push-Location $Root
    try {
        git pull 2>&1 | ForEach-Object { & $Log ([string]$_) 'Info' }
        if ($LASTEXITCODE -ne 0) { throw "git pull failed. Resolve the message above, then try again." }
    } finally {
        Pop-Location
    }

    & $Log "Refreshing dependencies (in case requirements changed) ..." 'Info'
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        & $venvPy "-m" "pip" "install" "-r" (Join-Path $Root "requirements.txt") 2>&1 | ForEach-Object { & $Log ([string]$_) 'Info' }
    } else {
        & $Log "No environment found -- run Install first." 'Warn'
    }

    & $Log "$appName is up to date." 'Ok'
}
