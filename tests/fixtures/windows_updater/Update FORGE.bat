@echo off
setlocal
set "ROOT=%~dp0"
echo Updating FORGE from verified GitHub Releases...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%Update-Application.ps1" -Project "forge"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Update did not complete. Review the error above and Update-Logs if present.
pause
exit /b %RESULT%
