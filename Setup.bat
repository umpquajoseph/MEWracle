@echo off
REM MEWracle Setup -- a small window with Install / Update / Repair buttons.
REM Pure PowerShell + WinForms, so no Python is required to run it (installing
REM Python is one of the things it does). For the plain console installer
REM instead, double-click Install.bat.
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0installer\setup_gui.ps1"
