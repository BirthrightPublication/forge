@echo off
setlocal EnableExtensions DisableDelayedExpansion

if "%~1"=="" (
    echo Drag and drop one or more MP3 files onto this BAT file.
    pause
    exit /b 1
)

set "FFMPEG=ffmpeg.exe"
where /q "%FFMPEG%" >nul 2>nul
if errorlevel 1 (
    if exist "%~dp0ffmpeg.exe" (
        set "FFMPEG=%~dp0ffmpeg.exe"
    ) else (
        echo ffmpeg.exe not found.
        echo Put ffmpeg.exe in the same folder as this BAT file or add FFmpeg to PATH.
        pause
        exit /b 1
    )
)

:next
if "%~1"=="" goto end

set "INPUT=%~1"
set "TEMP=%~dp1%~n1.__432tmp__.mp3"

echo.
echo Processing "%INPUT%"

"%FFMPEG%" -y -i "%INPUT%" ^
-map 0:a:0 ^
-map 0:v? ^
-map_metadata 0 ^
-af "asetrate=48000*432/440,aresample=48000,atempo=440/432" ^
-c:a libmp3lame -q:a 2 ^
-c:v copy ^
-id3v2_version 3 ^
-write_id3v1 1 ^
"%TEMP%"

if errorlevel 1 (
    echo Failed "%INPUT%"
    if exist "%TEMP%" del /q "%TEMP%" >nul 2>nul
    shift
    goto next
)

move /y "%TEMP%" "%INPUT%" >nul
if errorlevel 1 (
    echo Could not replace original "%INPUT%"
    if exist "%TEMP%" del /q "%TEMP%" >nul 2>nul
)

shift
goto next

:end
echo.
echo Finished.
pause
endlocal