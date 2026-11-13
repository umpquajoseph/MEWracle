@echo off
REM Start MEWracle. The first launch sets it up (Install.bat) and then opens it.
setlocal
set "HERE=%~dp0"
if not exist "%HERE%.venv\Scripts\pythonw.exe" (
    echo First run: setting things up...
    call "%HERE%Install.bat"
)
start "" "%HERE%.venv\Scripts\pythonw.exe" "%HERE%mewracle_gui.py"
endlocal
