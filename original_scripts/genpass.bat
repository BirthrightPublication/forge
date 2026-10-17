@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Password Generator

echo ==========================
echo   Interactive Password Generator
echo ==========================
echo.

:askLength
set "len="
set /p "len=Length of password in characters? "
if not defined len goto askLength
for /f "delims=0123456789" %%A in ("%len%") do goto askLength
if %len% LEQ 0 goto askLength

:askUpper
set "useUpper="
set /p "useUpper=Include uppercase? (Y/N): "
if /I "!useUpper!"=="Y" (
    set "useUpper=1"
) else if /I "!useUpper!"=="N" (
    set "useUpper=0"
) else (
    echo Please type Y or N.
    goto askUpper
)

:askNumbers
set "useNumbers="
set /p "useNumbers=Include numbers? (Y/N): "
if /I "!useNumbers!"=="Y" (
    set "useNumbers=1"
) else if /I "!useNumbers!"=="N" (
    set "useNumbers=0"
) else (
    echo Please type Y or N.
    goto askNumbers
)

:askSymbols
echo.
echo Enter the symbols you want to allow.
echo Example: !@#$%%^-_=+[]{}?
set "symbols="
set /p "symbols=Allowed symbols (leave blank for none): "

:askUnicode
set "useUnicode="
set /p "useUnicode=Include unicode? (Y/N): "
if /I "!useUnicode!"=="Y" (
    set "useUnicode=1"
) else if /I "!useUnicode!"=="N" (
    set "useUnicode=0"
) else (
    echo Please type Y or N.
    goto askUnicode
)

set "pool=abcdefghijklmnopqrstuvwxyz"
if "%useUpper%"=="1" set "pool=!pool!ABCDEFGHIJKLMNOPQRSTUVWXYZ"
if "%useNumbers%"=="1" set "pool=!pool!0123456789"
if defined symbols set "pool=!pool!!symbols!"
if "%useUnicode%"=="1" set "pool=!pool!äöüßÆØÅÇñ£¥"

set "password="
call :strlen pool poolLen

for /L %%I in (1,1,%len%) do (
    set /a idx=!random! %% poolLen
    for %%C in ("!idx!") do set "password=!password!!pool:~%%~C,1!"
)

echo.
echo Generated password:
echo !password!
echo.
pause
exit /b

:strlen
setlocal EnableDelayedExpansion
set "s=!%~1!"
set "n=0"
:strlen_loop
if defined s (
    set "s=!s:~1!"
    set /a n+=1
    goto strlen_loop
)
endlocal & set "%~2=%n%"
exit /b