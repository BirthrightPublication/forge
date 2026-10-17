$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root 'FORGE.exe'
$icon = Join-Path $root 'web\forge.ico'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (Test-Path -LiteralPath $compiler) {
    & $compiler /nologo /target:winexe /platform:anycpu "/out:$exe" "/win32icon:$icon" /reference:System.Windows.Forms.dll (Join-Path $PSScriptRoot 'ForgeLauncher.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Windows launcher compilation failed. Open FORGE.bat is still available.' }
} else {
    Write-Warning '.NET Framework compiler not found; creating a shortcut to Open FORGE.bat instead.'
}
$programs = [Environment]::GetFolderPath('Programs')
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $programs 'FORGE Local Production Studio.lnk'))
$shortcut.TargetPath = if (Test-Path -LiteralPath $exe) { $exe } else { Join-Path $root 'Open FORGE.bat' }
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'FORGE - Local Production Studio'
$shortcut.Save()
Write-Host 'Start menu shortcut created. Search Windows for FORGE.'
if ((Read-Host 'Also add Explorer > Send to > FORGE? [y/N]') -match '^[yY]$') {
    $sendto = [Environment]::GetFolderPath('SendTo')
    $item = $shell.CreateShortcut((Join-Path $sendto 'FORGE.lnk'))
    $item.TargetPath = $shortcut.TargetPath
    $item.WorkingDirectory = $root
    $item.IconLocation = "$icon,0"
    $item.Save()
    Write-Host 'Send to shortcut created; files passed this way retain original paths.'
}
