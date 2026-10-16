@echo off
setlocal DisableDelayedExpansion
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Run Setup FORGE.bat first.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0launcher.py" %*
