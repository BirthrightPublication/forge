@echo off
setlocal

set /p "URL=Enter the link for the QR code: "
set /p "NAME=Enter output file name without extension: "

set "QR_URL=%URL%"
set "QR_NAME=%NAME%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$url = $env:QR_URL; $name = $env:QR_NAME; $encoded = [System.Net.WebUtility]::UrlEncode($url); $api = 'https://api.qrserver.com/v1/create-qr-code/?size=1000x1000&data=' + $encoded; Invoke-WebRequest -Uri $api -OutFile ($name + '.png')"

echo QR code saved as %NAME%.png
pause