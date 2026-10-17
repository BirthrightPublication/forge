@echo off
setlocal DisableDelayedExpansion
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_optional.ps1" -Engine transcription
if errorlevel 1 echo Installation did not complete. Review the message above.
pause
