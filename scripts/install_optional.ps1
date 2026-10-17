param([ValidateSet('transcription','downloads')][string]$Engine)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run Setup FORGE.bat first.' }
& $python (Join-Path $PSScriptRoot 'is_running.py')
if ($LASTEXITCODE -eq 0) { throw 'Stop FORGE using Stop FORGE.bat, then run this installer again.' }
Write-Host "`nInstall optional $Engine engine into FORGE only."
Write-Host 'This contacts PyPI and downloads executable Python packages. No media is sent.'
if ($Engine -eq 'downloads') {
    Write-Host 'Resolves the latest stable 2026 yt-dlp version on this explicit installer run.'
    Write-Host 'FFmpeg, FFprobe, and Deno or Node must be supplied separately.'
} else { Write-Host 'Models download only after consent in the Studio. NVIDIA runtimes are separate.' }
if ((Read-Host 'Continue? [y/N]') -notmatch '^[yY]$') { exit 0 }
$req = Join-Path $root ("requirements-$Engine.txt")
# Core pins remain constraints; optional engine versions are captured after install.
& $python -m pip --isolated --disable-pip-version-check install --index-url https://pypi.org/simple --only-binary=:all: --upgrade --upgrade-strategy only-if-needed -c (Join-Path $root 'requirements-core.txt') -r $req
if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Review the error. The core UI can be used without this engine.' }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency conflicts found.' }
& $python -m pip freeze | Set-Content -LiteralPath (Join-Path $root 'installed-versions.txt') -Encoding UTF8
Write-Host 'Installation finished. Open FORGE and check Environment.'
