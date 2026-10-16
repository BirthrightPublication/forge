@echo off
setlocal EnableExtensions

set "EXIFTOOL=%~dp0exiftool.exe"
set "FFMPEG=%~dp0ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe"

if "%~1"=="" (
    echo Drag and drop one or more files onto this BAT file.
    pause
    exit /b 1
)

for %%F in (%*) do (
    call :PROCESS "%%~fF"
)

echo.
echo Done.
pause
exit /b

:PROCESS
set "INFILE=%~1"
set "DIR=%~dp1"
set "NAME=%~n1"
set "EXT=%~x1"
set "OUTFILE=%DIR%%NAME%-wiped%EXT%"

echo Processing: %INFILE%

if /I "%EXT%"==".mp3" goto AUDIO_MP3
if /I "%EXT%"==".wav" goto AUDIO_WAV
goto EXIFTOOL_FILE

:AUDIO_MP3
if not exist "%FFMPEG%" (
    echo ffmpeg.exe not found at:
    echo %FFMPEG%
    goto :eof
)
"%FFMPEG%" -y -i "%INFILE%" -map_metadata -1 -map 0:a -c:a libmp3lame -q:a 2 "%OUTFILE%"
if errorlevel 1 (
    echo Failed: %INFILE%
    goto :eof
)
del /f /q "%INFILE%"
goto :eof

:AUDIO_WAV
if not exist "%FFMPEG%" (
    echo ffmpeg.exe not found at:
    echo %FFMPEG%
    goto :eof
)
"%FFMPEG%" -y -i "%INFILE%" -map_metadata -1 -map 0:a -c:a pcm_s16le "%OUTFILE%"
if errorlevel 1 (
    echo Failed: %INFILE%
    goto :eof
)
del /f /q "%INFILE%"
goto :eof

:EXIFTOOL_FILE
if not exist "%EXIFTOOL%" (
    echo exiftool.exe not found at:
    echo %EXIFTOOL%
    goto :eof
)
"%EXIFTOOL%" -overwrite_original -all= "-FileName<%DIR%%NAME%-wiped%EXT%" "%INFILE%"
goto :eof