@echo off
setlocal DisableDelayedExpansion
if not exist "%~dp0.venv\Scripts\python.exe" exit /b 0
"%~dp0.venv\Scripts\python.exe" "%~dp0launcher.py" --stop
if errorlevel 1 pause
