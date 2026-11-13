@echo off
REM Pull the latest committed version and refresh dependencies.
REM
REM Nothing a researcher made is lost or blocks the pull:
REM   * their presets live in my_presets\ (ignored by git). Presets an older
REM     version saved into the tracked presets\ folder are moved there first
REM     (engine\preset_store.py), before git sees them;
REM   * any edit still left in a tracked file is set aside in a git stash
REM     (never deleted), so the pull cannot refuse or conflict over it, even
REM     when the new version moves or renames that file;
REM   * the pull is fast-forward only: a copy with commits of its own is left
REM     exactly as it is, with a message, rather than merged.
REM This script runs from a copy in %TEMP%, because the pull may replace this
REM very file and cmd reads a running batch file as it goes.
setlocal
if /i "%~1"=="--from-temp" goto :copied
copy /y "%~f0" "%TEMP%\mewracle_update.bat" >nul
if errorlevel 1 goto :in_place
REM without "call": control passes to the copy and never comes back here
"%TEMP%\mewracle_update.bat" --from-temp "%~dp0"
:in_place
set "REPO=%~dp0"
goto :start
:copied
set "REPO=%~2"
:start
cd /d "%REPO%"
echo Updating to the latest version...
echo.

where git >nul 2>nul
if errorlevel 1 (
    echo [!] git is not installed, so this copy cannot auto-update.
    echo     Install git from https://git-scm.com/download/win and run Install.bat again,
    echo     or re-download the latest code manually.
    echo.
    pause
    exit /b 1
)
if not exist ".git" (
    echo [!] This folder is not a git clone, so there is nothing to pull.
    echo     To get updates automatically, clone the repo with git instead of
    echo     downloading a ZIP.
    echo.
    pause
    exit /b 1
)

REM 1. presets an older version saved inside presets\ move to my_presets\
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if defined PY (
    "%PY%" engine\preset_store.py
) else (
    echo [!] No Python found, so presets saved in the presets folder were not
    echo     moved. Any that block the update are named below.
)

REM 2. edits left in tracked files go to a stash, not away
set "STASHED="
git diff --quiet HEAD -- 2>nul
if errorlevel 1 (
    git stash push -m "MEWracle update.bat %DATE% %TIME%: local edits set aside" >nul
    if errorlevel 1 (
        echo [!] Your copy has local changes that could not be set aside.
        echo     Nothing was changed. Run "git status" to see them.
        echo.
        pause
        exit /b 1
    )
    set "STASHED=1"
)

REM 3. the pull itself
git pull --ff-only
if errorlevel 1 (
    echo.
    echo [!] git pull did not run. Nothing of yours was changed or deleted.
    echo     Read the message above: a file it names is in the way, or this
    echo     copy has commits of its own and needs "git pull" run by hand.
    if defined STASHED echo     Your earlier edits to program files are in "git stash list".
    echo.
    pause
    exit /b 1
)
if defined STASHED (
    echo.
    echo Your own edits to program files were set aside, not deleted.
    echo They are in "git stash list"; "git stash show -p" shows them.
)

echo.
echo Refreshing dependencies (in case requirements changed)...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
) else (
    echo No environment found - running full setup...
    call "%REPO%Install.bat"
)

echo.
echo Up to date. Your presets are in the my_presets folder.
echo Launch from the shortcut or MEWracle.bat.
echo.
pause
