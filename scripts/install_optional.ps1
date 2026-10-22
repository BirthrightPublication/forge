param([ValidateSet('transcription','downloads','media-privacy')][string]$Engine,[int]$WaitForStop=0)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (Test-Path -LiteralPath (Join-Path $root '.forge-maintenance.json')) {
  throw 'Maintenance is active or interrupted. Wait for its console, or run: py -3 scripts/managed_install.py --recover'
}
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run Setup FORGE.bat first.' }
if ($WaitForStop -eq 1) {
  Start-Sleep -Seconds 2
  for ($i=0; $i -lt 30; $i++) {
    & $python (Join-Path $PSScriptRoot 'is_running.py')
    if ($LASTEXITCODE -ne 0) { break }
    Start-Sleep -Seconds 1
  }
}
& $python (Join-Path $PSScriptRoot 'is_running.py')
if ($LASTEXITCODE -eq 0) {
  & $python (Join-Path $PSScriptRoot 'request_maintenance.py') --engine $Engine
  if ($LASTEXITCODE -ne 0) { throw 'Managed installation request failed. Review the message above.' }
  exit 0
}
Write-Host "`nInstall optional $Engine engine into FORGE only."
Write-Host 'This contacts PyPI and downloads executable Python packages. No media is sent.'
if ($Engine -eq 'downloads') {
    Write-Host 'Resolves the latest stable 2026 yt-dlp version on this explicit installer run.'
    Write-Host 'FFmpeg, FFprobe, and Deno or Node must be supplied separately.'
} elseif ($Engine -eq 'media-privacy') {
    Write-Host 'Installs the optional local C2PA inspection library. No media is uploaded.'
    Write-Host 'Core watermarking, metadata and fingerprint tools use your existing FFmpeg/Pillow.'
} else { Write-Host 'Models download only after consent in the Studio. NVIDIA runtimes are separate.' }
if ((Read-Host 'Continue? [y/N]') -notmatch '^[yY]$') { exit 0 }
$req = Join-Path $root ("requirements-$Engine.txt")
if ($Engine -eq 'media-privacy') { $req = Join-Path $PSScriptRoot 'requirements-media-privacy.txt' }
# Core pins remain constraints; optional engine versions are captured after install.
& $python -m pip --isolated --disable-pip-version-check install --index-url https://pypi.org/simple --only-binary=:all: --upgrade --upgrade-strategy only-if-needed -c (Join-Path $root 'requirements-core.txt') -r $req
if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Review the error. The core UI can be used without this engine.' }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency conflicts found.' }
& $python -m pip freeze | Set-Content -LiteralPath (Join-Path $root 'installed-versions.txt') -Encoding UTF8
Write-Host 'Installation finished. Open FORGE and check Environment.'
