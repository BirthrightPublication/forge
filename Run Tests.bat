@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run Setup FORGE.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pytest -q --tb=short
pause
