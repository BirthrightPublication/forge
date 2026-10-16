$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
Write-Host "`nFORGE V0.3 - local setup`n"
Write-Host 'Creates a private Python environment in this folder and a Start shortcut.'
Write-Host 'Downloads the listed Python packages from PyPI. No media is uploaded.'
Write-Host 'No administrator rights, startup task, CUDA installation, or firewall rule.'
Write-Host 'Python 3.11, 3.12, or 3.13 (64-bit, including Tcl/Tk) must already be installed.'
if ((Read-Host 'Continue with the package installation? [y/N]') -notmatch '^[yY]$') { exit 0 }
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    & $venvPython (Join-Path $PSScriptRoot 'is_running.py')
    if ($LASTEXITCODE -eq 0) { throw 'Stop FORGE using Stop FORGE.bat before running setup again.' }
} else {
    $pythonPath = $null
    $check = "import sys,struct; print(sys.executable if (3,11)<=sys.version_info[:2]<=(3,13) and struct.calcsize('P')==8 else '')"
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        foreach ($version in @('-3.13','-3.12','-3.11')) {
            try {
                $oldPreference = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
                $found = & py.exe $version -c $check 2>$null
                $code = $LASTEXITCODE; $ErrorActionPreference = $oldPreference
                if ($code -eq 0 -and $found -and (Test-Path -LiteralPath ([string]$found))) {
                    $pythonPath = [string]$found; break
                }
            } catch { $ErrorActionPreference = 'Stop' }
        }
    }
    if (-not $pythonPath -and (Get-Command python.exe -ErrorAction SilentlyContinue)) {
        try {
            $oldPreference = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            $found = & python.exe -c $check 2>$null
            $code = $LASTEXITCODE; $ErrorActionPreference = $oldPreference
            if ($code -eq 0 -and $found -and (Test-Path -LiteralPath ([string]$found))) { $pythonPath = [string]$found }
        } catch { $ErrorActionPreference = 'Stop' }
    }
    if (-not $pythonPath) { throw 'No supported 64-bit Python found. Install Python from python.org with Tcl/Tk, then run setup again.' }
    & $pythonPath -m venv (Join-Path $root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the private environment.' }
}
& $venvPython -c 'import tkinter; print("Native dialog support is available.")'
if ($LASTEXITCODE -ne 0) { Write-Warning 'Tcl/Tk is missing. Native browse will not work; use pasted paths or repair Python installation.' }
& $venvPython -m pip --isolated --disable-pip-version-check install --index-url https://pypi.org/simple --only-binary=:all: -r (Join-Path $root 'requirements-core.txt')
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed. Read the error above. No original scripts or media were changed.' }
& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency conflicts found; do not launch until resolved.' }
& $venvPython -m pip freeze | Set-Content -LiteralPath (Join-Path $root 'installed-versions.txt') -Encoding UTF8
try { & (Join-Path $PSScriptRoot 'create_shortcuts.ps1') } catch { Write-Warning $_.Exception.Message }
Write-Host "`nSetup complete. Double-click Open FORGE.bat or search Windows for FORGE."
Write-Host 'Optional engines: Install Transcription.bat and Install Downloads.bat.'
