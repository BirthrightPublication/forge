param([int]$WaitForStop=0)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
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
if ($LASTEXITCODE -eq 0) { throw 'Stop FORGE before installing optional engines.' }
Write-Host 'Install optional pyannote.audio into a separate .diarization-venv (CPU speaker labeling).'
Write-Host 'This downloads Python/PyTorch packages from PyPI. No media is uploaded.'
Write-Host 'The Community-1 model is NOT included. Obtain its full offline folder from the publisher after accepting its access conditions.'
Write-Host 'No Hugging Face token is requested or stored by this installer.'
if ((Read-Host 'Continue? [y/N]') -notmatch '^[yY]$') { exit 0 }
$envRoot=Join-Path $root '.diarization-venv'
if (-not (Test-Path -LiteralPath (Join-Path $envRoot 'Scripts\python.exe'))) {
  & $python -m venv $envRoot
  if ($LASTEXITCODE -ne 0) { throw 'Failed to create the isolated speaker environment.' }
}
$dp=Join-Path $envRoot 'Scripts\python.exe'
$env:PYANNOTE_METRICS_ENABLED='0';$env:HF_HUB_DISABLE_TELEMETRY='1'
& $dp -m pip --isolated --disable-pip-version-check install --index-url https://pypi.org/simple --only-binary=:all: -r (Join-Path $root 'requirements-diarization.txt')
if ($LASTEXITCODE -ne 0) { throw 'Optional speaker installation failed. The main FORGE environment was not changed.' }
& $dp -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Speaker environment has dependency conflicts.' }
& $dp -m pip freeze | Set-Content -LiteralPath (Join-Path $root 'installed-diarization-versions.txt') -Encoding UTF8
Write-Host 'Installed. Reopen FORGE and select a complete local Community-1 model directory.'
