@echo off
title MP3 Downloader

echo ================================
echo        MP3 Downloader
echo ================================
echo.

:: Ask for video URL
set /p URL=Paste video link: 

echo.
:: Ask for output directory
set /p OUTDIR=Enter folder to save MP3: 

:: Create folder if it doesn't exist
if not exist "%OUTDIR%" (
    mkdir "%OUTDIR%"
)

echo.
echo Downloading audio...
echo.

:: Download and convert to MP3
yt-dlp ^
-x ^
--audio-format mp3 ^
--audio-quality 0 ^
-o "%OUTDIR%\%%(title)s.%%(ext)s" ^
"%URL%"

echo.
echo Finished!
pause