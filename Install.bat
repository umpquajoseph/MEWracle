@echo off
REM One-time setup. Double-click this file.
REM It hands off to installer\install.ps1, which sets up an isolated environment and
REM creates Start Menu / Desktop shortcuts.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1"
echo.
pause
