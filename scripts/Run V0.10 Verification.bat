@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0.."
set "FORGE_VERIFICATION_ROOT=%CD%"
set "FORGE_DATA_DIR=%FORGE_VERIFICATION_ROOT%\Build\V0.10-Verification\Data"
set "FORGE_PORT=18765"
if not exist "%FORGE_VERIFICATION_ROOT%\.venv\Scripts\python.exe" (
    echo This extracted candidate needs its local application environment.
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -NoShortcuts
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
if not exist "%FORGE_VERIFICATION_ROOT%\.venv\Scripts\python.exe" (
    echo Setup was cancelled or no local application environment is available.
    pause
    exit /b 1
)
"%FORGE_VERIFICATION_ROOT%\.venv\Scripts\python.exe" "%~dp0verification_launch.py"
if errorlevel 1 pause
