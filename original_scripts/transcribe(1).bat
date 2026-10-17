@echo off
setlocal

set "LOG=%~dp0transcription_log.txt"

echo Starting transcription... > "%LOG%"
echo Date: %date% %time% >> "%LOG%"
echo. >> "%LOG%"

if "%~1"=="" (
    echo Drag and drop an audio or video file onto this BAT file.
    echo Drag and drop an audio or video file onto this BAT file. >> "%LOG%"
    pause
    exit /b
)

set "INPUT=%~1"
set "OUTPUT=%~dpn1_transcript.txt"

echo Input: "%INPUT%" >> "%LOG%"
echo Output: "%OUTPUT%" >> "%LOG%"
echo. >> "%LOG%"

echo Checking Python...
python --version >> "%LOG%" 2>&1
if errorlevel 1 (
    echo Python is not installed or not on PATH.
    echo Python is not installed or not on PATH. >> "%LOG%"
    pause
    exit /b
)

python -m pip install faster-whisper imageio-ffmpeg >> "%LOG%" 2>&1
if errorlevel 1 (
    echo Failed installing dependencies. Check transcription_log.txt.
    pause
    exit /b
)

echo Transcribing. This may take a while...
echo Transcribing... >> "%LOG%"

python -c "from faster_whisper import WhisperModel; import sys; model=WhisperModel('base', device='cpu', compute_type='int8'); segments, info=model.transcribe(sys.argv[1]); f=open(sys.argv[2], 'w', encoding='utf-8'); [f.write(s.text.strip() + '\n') for s in segments]; f.close(); print('Saved transcript to:', sys.argv[2])" "%INPUT%" "%OUTPUT%" >> "%LOG%" 2>&1

if errorlevel 1 (
    echo Transcription failed. Check transcription_log.txt.
    pause
    exit /b
)

echo.
echo Done!
echo Transcript saved to:
echo "%OUTPUT%"
echo Done. >> "%LOG%"
pause