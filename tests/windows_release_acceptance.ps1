<#
Runs the exact supplied Windows updater in a disposable drive-rooted folder.
Only HTTP responses and Read-Host are mocked. Hash verification, Expand-Archive,
path checks, backups and installation execute in the unmodified updater.
#>
param(
  [Parameter(Mandatory=$true)][string]$Archive,
  [Parameter(Mandatory=$true)][string]$Checksum,
  [Parameter(Mandatory=$true)][string]$Output,
  [string]$PythonExecutable,
  [switch]$RefreshBytecode
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version 2.0
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1 -or $PSVersionTable.PSEdition -ne 'Desktop') {
  throw 'Run this acceptance test with Windows PowerShell 5.1 (powershell.exe).'
}
$Archive = (Resolve-Path -LiteralPath $Archive).ProviderPath
$Checksum = (Resolve-Path -LiteralPath $Checksum).ProviderPath
$Output = [IO.Path]::GetFullPath($Output).TrimEnd([char]92, [char]47)
$allowedOutput = $false
foreach ($candidate in @($env:TEMP, $env:RUNNER_TEMP)) {
  if (-not [string]::IsNullOrWhiteSpace($candidate)) {
    $prefix = [IO.Path]::GetFullPath($candidate).TrimEnd([char]92, [char]47) + '\'
    if ($Output.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { $allowedOutput = $true }
  }
}
if (-not $allowedOutput -or $Output -notmatch '^[A-Za-z]:\\') {
  throw 'Output must be a new drive-rooted disposable folder beneath TEMP or RUNNER_TEMP.'
}
if (Test-Path -LiteralPath $Output) { throw 'Output already exists. Choose a fresh disposable folder.' }
if (-not (Test-Path -LiteralPath $Archive -PathType Leaf) -or -not (Test-Path -LiteralPath $Checksum -PathType Leaf)) {
  throw 'Archive and Checksum must be existing files.'
}
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
  $PythonExecutable = (Get-Command python -CommandType Application -ErrorAction Stop).Source
}
$PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).ProviderPath
$fixtureRoot = Join-Path $PSScriptRoot 'fixtures\windows_updater'
$updaterFixture = Join-Path $fixtureRoot 'Update-Application.ps1'
$batchFixture = Join-Path $fixtureRoot 'Update FORGE.bat'
$smokeScript = Join-Path $PSScriptRoot 'windows_installed_smoke.py'
$expectedUpdaterHash = '9d499a9122f179e5ae2d1f19393be84fefeac2d013f5d70fcf7ed3b5b59895db'
$expectedBatchHash = '8eabdd2b4ebff057dfccdbdf646899ce5504d37bc021cb937867ff7c3645cfef'
$nativePowerShell = Join-Path $PSHOME 'powershell.exe'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$checks = New-Object 'System.Collections.Generic.List[string]'
$cases = New-Object 'System.Collections.Generic.List[object]'
$report = [ordered]@{
  schema_version=1; status='failed'; native_windows_execution=$true
  powershell_edition=$PSVersionTable.PSEdition; powershell_version=$PSVersionTable.PSVersion.ToString()
  archive_name=[IO.Path]::GetFileName($Archive); archive_sha256=''; package_version=''; updater_sha256=''; batch_fixture_sha256=''
  bat_launcher_executed=$false; network_mocked=$true; input_mocked=$true; updater_logic_modified=$false
  checks=$checks; passed=0; cases=$cases; installed_app_smoke=$null; failure=$null
  failure_detail=$null; failure_location=$null; installation_evidence=$null
}
New-Item -ItemType Directory -Path $Output | Out-Null

function Assert-Check([bool]$Condition, [string]$Label) {
  if (-not $Condition) { throw ('Acceptance failed: ' + $Label) }
  [void]$script:checks.Add($Label)
  Write-Host ('PASS ' + $Label)
}
function Hash-File([string]$Path) {
  $stream = [IO.File]::OpenRead($Path)
  $sha = [Security.Cryptography.SHA256]::Create()
  try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
  finally { $stream.Dispose(); $sha.Dispose() }
}
function Sanitize-Diagnostic([string]$Message) {
  $safe=$Message
  foreach ($replacement in @(
    @($Output, '<acceptance-output>'), @($Archive, '<archive>'), @($Checksum, '<checksum>'),
    @($PythonExecutable, '<python>'), @($PSScriptRoot, 'tests'),
    @($env:RUNNER_TEMP, '<runner-temp>'), @($env:TEMP, '<temp>')
  )) {
    if (-not [string]::IsNullOrWhiteSpace($replacement[0])) {
      $safe=$safe.Replace([string]$replacement[0], [string]$replacement[1])
    }
  }
  return $safe
}
function Escape-Annotation([string]$Message) {
  return $Message.Replace('%', '%25').Replace("`r", '%0D').Replace("`n", '%0A')
}
function Assert-PayloadFile([string]$Root, $Entry) {
  $installed=Join-Path $Root $Entry.relative
  $exists=[IO.File]::Exists($installed)
  $detail=[ordered]@{
    relative_path=$Entry.relative; filesystem_exists=$exists
    installation_root=(Sanitize-Diagnostic $Root); installation_root_type=$Root.GetType().FullName
    checked_path=(Sanitize-Diagnostic $installed)
    provider_exists=[bool](Test-Path -LiteralPath $installed -PathType Leaf)
    attributes=$null; expected_sha256=$Entry.hash; actual_sha256=$null; read_error_type=$null
  }
  if ($exists) {
    try {
      $detail.attributes=[IO.File]::GetAttributes($installed).ToString()
      $detail.actual_sha256=Hash-File $installed
    } catch { $detail.read_error_type=$_.Exception.GetType().FullName }
  }
  if (-not $exists -or $detail.actual_sha256 -ne $Entry.hash) {
    $script:report.failure_detail=$detail
  }
  Assert-Check $exists ('install: payload file exists '+$Entry.relative)
  Assert-Check ($detail.actual_sha256 -eq $Entry.hash) ('install: exact payload bytes '+$Entry.relative)
}
function Write-Json([string]$Path, $Value) {
  [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding($false)))
}
function Read-InstallationEvidence($Case) {
  $caseRoot=[string]$Case.root
  $caseFolder=[string]$Case.folder
  $evidence=[ordered]@{
    case_type=$Case.GetType().FullName; root_type=$Case.root.GetType().FullName
    installation_root=(Sanitize-Diagnostic $caseRoot)
    scenario_folder=(Sanitize-Diagnostic $caseFolder)
    root_exists=[IO.Directory]::Exists($caseRoot); root_entries=@(); files=@()
    rollback_manifests=@(); updater_stdout=''; updater_stderr=''; temp_environment=$null; diagnostic_error=$null
  }
  $prefix=[IO.Path]::GetFullPath($Output).TrimEnd([char]92,[char]47)+[IO.Path]::DirectorySeparatorChar
  if (-not [IO.Path]::GetFullPath($caseRoot).StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) -or
      -not [IO.Path]::GetFullPath($caseFolder).StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) {
    $evidence.diagnostic_error='Refused inventory outside the disposable acceptance output.'
    return $evidence
  }
  try {
    $tempEvidence=Join-Path $caseFolder 'temp-environment.json'
    if ([IO.File]::Exists($tempEvidence)) {
      $evidence.temp_environment=Get-Content -LiteralPath $tempEvidence -Raw | ConvertFrom-Json
    }
    foreach ($channel in @('stdout','stderr')) {
      $logPath=Join-Path $caseFolder ($channel+'.log')
      if ([IO.File]::Exists($logPath)) {
        $evidence['updater_'+$channel]=Sanitize-Diagnostic ([IO.File]::ReadAllText($logPath))
      }
    }
    if ($evidence.root_exists) {
      $evidence.root_entries=@(Get-ChildItem -LiteralPath $caseRoot -Force | ForEach-Object {
        [ordered]@{name=$_.Name; attributes=$_.Attributes.ToString(); directory=$_.PSIsContainer}
      })
      $evidence.files=@(Get-ChildItem -LiteralPath $caseRoot -Recurse -File -Force | ForEach-Object {
        [ordered]@{
          relative_path=$_.FullName.Substring($caseRoot.Length).TrimStart([char]92,[char]47)
          attributes=$_.Attributes.ToString(); size_bytes=$_.Length
        }
      })
      $backupRoot=Join-Path $caseRoot 'Update-Backups'
      if ([IO.Directory]::Exists($backupRoot)) {
        $evidence.rollback_manifests=@(Get-ChildItem -LiteralPath $backupRoot -Recurse -File -Force -Filter 'rollback-manifest.json' | ForEach-Object {
          $manifestFile=$_
          # Windows PowerShell 5.1 emits a JSON array as one pipeline object.
          # Assignment unwraps that object; this separate pipeline enumerates it.
          $manifestEntries=Get-Content -LiteralPath $manifestFile.FullName -Raw | ConvertFrom-Json
          [ordered]@{
            relative_path=$manifestFile.FullName.Substring($caseRoot.Length).TrimStart([char]92,[char]47)
            entries=@($manifestEntries | ForEach-Object {
              [ordered]@{path=(Sanitize-Diagnostic ([string]$_.path)); existed=$_.existed}
            })
          }
        })
      }
    }
  } catch { $evidence.diagnostic_error=Sanitize-Diagnostic $_.Exception.Message }
  return $evidence
}
function Write-Fixture([string]$Path, [string]$Value) {
  New-Item -ItemType Directory -Force (Split-Path -Parent $Path) | Out-Null
  [IO.File]::WriteAllText($Path, $Value, (New-Object Text.UTF8Encoding($false)))
}
function Quote-Argument([string]$Value) {
  if ($Value.Contains('"')) { throw 'A native argument contained an invalid quote.' }
  return '"' + $Value + '"'
}
function Snapshot-Files([string]$Root) {
  $snapshot = @{}
  foreach ($file in @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force)) {
    $relative = $file.FullName.Substring($Root.Length).TrimStart([char]92, [char]47)
    if (($relative -split '[\\/]')[0] -in @('Update-Logs', 'Update-Backups')) { continue }
    $snapshot[$relative] = Hash-File $file.FullName
  }
  return $snapshot
}
function Assert-Unchanged([string]$Root, [hashtable]$Expected, [string]$Label) {
  $actual = Snapshot-Files $Root
  Assert-Check ($actual.Count -eq $Expected.Count) ($Label + ': no application/user files added or removed')
  foreach ($name in $Expected.Keys) {
    Assert-Check ($actual.ContainsKey($name) -and $actual[$name] -eq $Expected[$name]) ($Label + ': unchanged ' + $name)
  }
}
function New-TestZip([string]$Path, [hashtable]$Entries) {
  $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew)
  $zip = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Create, $false)
  try {
    foreach ($name in $Entries.Keys) {
      $entry = $zip.CreateEntry($name)
      $writer = New-Object IO.StreamWriter($entry.Open(), (New-Object Text.UTF8Encoding($false)))
      try { $writer.Write([string]$Entries[$name]) } finally { $writer.Dispose() }
    }
  } finally { $zip.Dispose(); $stream.Dispose() }
}
function Read-Payload([string]$Path) {
  $zip = [IO.Compression.ZipFile]::OpenRead($Path)
  $payload = New-Object 'System.Collections.Generic.List[object]'
  try {
    foreach ($entry in $zip.Entries) {
      if ($entry.FullName.EndsWith('/')) { continue }
      if (-not $entry.FullName.StartsWith('app/', [StringComparison]::Ordinal)) { continue }
      $relative = $entry.FullName.Substring(4).Replace('/', '\')
      $reader = $entry.Open()
      $sha = [Security.Cryptography.SHA256]::Create()
      try { $hash = [BitConverter]::ToString($sha.ComputeHash($reader)).Replace('-', '').ToLowerInvariant() }
      finally { $reader.Dispose(); $sha.Dispose() }
      [void]$payload.Add([pscustomobject]@{ relative=$relative; hash=$hash })
    }
  } finally { $zip.Dispose() }
  return $payload.ToArray()
}
function Read-PackageVersion([string]$Path) {
  $zip=[IO.Compression.ZipFile]::OpenRead($Path)
  try {
    $entry=$zip.GetEntry('app/VERSION.txt')
    if ($null -eq $entry) { throw 'Candidate has no app/VERSION.txt.' }
    $reader=New-Object IO.StreamReader($entry.Open())
    try { return $reader.ReadToEnd().Trim() } finally { $reader.Dispose() }
  } finally { $zip.Dispose() }
}
function New-Case([string]$ScenarioName, [string]$ZipPath, [string]$ShaPath, [string]$Response, [bool]$NoWindowsAsset=$false, [bool]$Junction=$false) {
  $folder = Join-Path $Output $ScenarioName
  $root = Join-Path $folder 'installation'
  New-Item -ItemType Directory -Force $root | Out-Null
  $sentinels = @{
    'Settings\settings.json'='{"private-setting":"keep"}'
    'Presets\presets.json'='{"custom-preset":"keep"}'
    'Data\forge.sqlite3'='Private database sentinel; not a real database.'
    'Queue\queue.json'='{"queued-job":"keep"}'
    'History\history.json'='{"completed-job":"keep"}'
    '.venv\preserved.txt'='Preserve the owner application environment.'
    '.diarization-venv\preserved.txt'='Preserve the owner speech environment.'
    'models\preserved.bin'='Preserve downloaded model bytes.'
    'original_scripts\owner-script.py'='# Preserve original scripts.'
    'tools\owner-tool.txt'='Preserve locally supplied tools.'
    'exports\owner-export.txt'='Preserve completed exports.'
    'legacy-stale-file.py'='# The supplied updater retains unrelated application files.'
  }
  foreach ($name in $sentinels.Keys) { Write-Fixture (Join-Path $root $name) $sentinels[$name] }
  $buildFolder=Join-Path $root 'Build'
  New-Item -ItemType Directory -Force $buildFolder | Out-Null
  New-TestZip (Join-Path $buildFolder 'previous-release.zip') @{'app/VERSION.txt'='Preserve the previous release download.'}
  $sentinels['Build\previous-release.zip']=''
  Copy-Item -LiteralPath $updaterFixture -Destination (Join-Path $root 'Update-Application.ps1')
  Copy-Item -LiteralPath $batchFixture -Destination (Join-Path $root 'Update FORGE.bat')
  $sentinels['Update-Application.ps1']=''
  $sentinels['Update FORGE.bat']=''
  Write-Fixture (Join-Path $root 'VERSION.txt') '0.0.0'
  $oldZip=[IO.Compression.ZipFile]::OpenRead($Archive)
  try {
    $reader=New-Object IO.StreamReader($oldZip.GetEntry('app/forge/__init__.py').Open())
    try { $oldSource=$reader.ReadToEnd() } finally { $reader.Dispose() }
  } finally { $oldZip.Dispose() }
  $oldSource=$oldSource.Replace($script:packageVersion, ($script:packageVersion -replace '[0-9]', '9'))
  $oldInit=Join-Path $root 'forge\__init__.py'
  New-Item -ItemType Directory -Force (Split-Path $oldInit -Parent) | Out-Null
  [IO.File]::WriteAllText($oldInit, $oldSource, (New-Object Text.UTF8Encoding($false)))
  (Get-Item -LiteralPath $oldInit).LastWriteTime=[datetime]'1980-01-01T00:00:00'
  $oldFiles=@{}
  foreach ($name in @('VERSION.txt','forge\__init__.py')) { $oldFiles[$name]=Hash-File (Join-Path $root $name) }
  $preserved=@{}
  foreach ($name in $sentinels.Keys) { $preserved[$name]=Hash-File (Join-Path $root $name) }
  $outside=$null
  if ($Junction) {
    $target=Join-Path $folder 'junction-target'
    Move-Item -LiteralPath (Join-Path $root 'forge') -Destination $target
    New-Item -ItemType Junction -Path (Join-Path $root 'forge') -Target $target | Out-Null
    $outside=Join-Path $target '__init__.py'
  }
  $archiveName=if ($NoWindowsAsset) { 'FORGE-source.zip' } else { [IO.Path]::GetFileName($Archive) }
  $assets=@(
    [pscustomobject]@{name=$archiveName;browser_download_url='https://forge-acceptance.invalid/package.zip'},
    [pscustomobject]@{name=($archiveName+'.sha256');browser_download_url='https://forge-acceptance.invalid/package.sha256'}
  )
  $metadata=[pscustomobject]@{tag_name=('v'+$script:packageVersion);assets=$assets}
  $tempRoot=$buildFolder
  $tempRoot=(Get-Item -LiteralPath $tempRoot -Force).FullName
  $config=[ordered]@{
    Updater=(Join-Path $root 'Update-Application.ps1');Zip=$ZipPath;Checksum=$ShaPath
    Response=$Response;Metadata=$metadata;Calls=(Join-Path $folder 'mock-calls.jsonl')
    TempRoot=$tempRoot;TempEvidence=(Join-Path $folder 'temp-environment.json');AcceptanceOutput=$Output
  }
  $configPath=Join-Path $folder 'scenario.json'
  Write-Json $configPath $config
  return [pscustomobject]@{
    name=$ScenarioName;folder=$folder;root=$root;config=$configPath
    before=(Snapshot-Files $root);preserved=$preserved;oldFiles=$oldFiles;outside=$outside
  }
}

# The three mocks below are the only substitutions made during updater execution.
$wrapper=@'
param([Parameter(Mandatory=$true)][string]$ScenarioFile)
$ErrorActionPreference='Stop'
$config=Get-Content -LiteralPath $ScenarioFile -Raw | ConvertFrom-Json
# Isolate the updater's scratch files as well as its installation. The native
# runner may inherit an 8.3 TEMP alias; the supplied updater derives relative
# paths by subtracting a string length, so capture that behavior before using
# the canonical scratch directory. This does not repair the supplied updater.
function Redact-TempPath([string]$Path) {
  $safe=$Path.Replace([string]$config.AcceptanceOutput,'<acceptance-output>')
  return ($safe -replace '(?i)([A-Z]:\\Users\\)[^\\]+','${1}<profile>')
}
function Measure-UpdaterTemp([string]$TempPath) {
  $probeRoot=Join-Path $TempPath ('ForgeAcceptanceProbe-'+[guid]::NewGuid().ToString('N'))
  try {
    $source=Join-Path $probeRoot 'extracted\app'
    New-Item -ItemType Directory -Force $source | Out-Null
    $probeFile=Join-Path $source 'probe.txt'
    [IO.File]::WriteAllText($probeFile,'Disposable updater path probe.')
    $file=@(Get-ChildItem -LiteralPath $source -Recurse -File -Force)[0]
    $relative=$file.FullName.Substring($source.Length).TrimStart('\','/')
    $itemPath=(Get-Item -LiteralPath $TempPath -Force).FullName
    $resolvedPath=(Resolve-Path -LiteralPath $TempPath).ProviderPath
    $directoryInfo=New-Object IO.DirectoryInfo($TempPath)
    return [ordered]@{
      raw_path=(Redact-TempPath $TempPath);raw_length=$TempPath.Length
      contains_short_alias=($TempPath -match '(?i)~[0-9]+([\\/]|$)')
      get_item_path=(Redact-TempPath $itemPath);get_item_length=$itemPath.Length
      resolve_path=(Redact-TempPath $resolvedPath);resolve_path_length=$resolvedPath.Length
      directory_info_path=(Redact-TempPath $directoryInfo.FullName)
      source_length=$source.Length;enumerated_filename_length=$file.FullName.Length
      filename_length_delta=($file.FullName.Length-$probeFile.Length)
      expected_relative_path='probe.txt';updater_relative_path=$relative
      relative_path_correct=($relative -ceq 'probe.txt')
    }
  } finally { Remove-Item -LiteralPath $probeRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
$tempEvidence=[ordered]@{
  inherited=(Measure-UpdaterTemp $env:TEMP)
  isolated=(Measure-UpdaterTemp ([string]$config.TempRoot))
  updater_fixture_modified=$false
}
$tempEvidence | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $config.TempEvidence -Encoding UTF8
if (-not $tempEvidence.isolated.relative_path_correct) { throw 'Canonical disposable TEMP did not preserve updater relative paths.' }
$env:TEMP=[string]$config.TempRoot
$env:TMP=[string]$config.TempRoot
function Record-Mock([string]$Kind,[string]$Value) {
  [pscustomobject]@{kind=$Kind;value=$Value} | ConvertTo-Json -Compress | Add-Content -LiteralPath $config.Calls -Encoding UTF8
}
function Invoke-RestMethod {
  param([string]$Uri,$Headers,[int]$TimeoutSec)
  if ($Uri -ne 'https://api.github.com/repos/BirthrightPublication/forge/releases/latest') { throw 'Unexpected metadata request.' }
  Record-Mock 'metadata' $Uri
  return $config.Metadata
}
function Invoke-WebRequest {
  param([string]$Uri,[string]$OutFile,$Headers,[int]$TimeoutSec)
  Record-Mock 'download' $Uri
  if ($Uri -eq 'https://forge-acceptance.invalid/package.zip') { Copy-Item -LiteralPath $config.Zip -Destination $OutFile }
  elseif ($Uri -eq 'https://forge-acceptance.invalid/package.sha256') { Copy-Item -LiteralPath $config.Checksum -Destination $OutFile }
  else { throw 'Unexpected download request.' }
}
function Read-Host {
  param([string]$Prompt)
  Record-Mock 'confirmation' $Prompt
  return [string]$config.Response
}
$global:LASTEXITCODE=0
& ([string]$config.Updater) -Project 'forge'
exit $global:LASTEXITCODE
'@
$wrapperPath=Join-Path $Output 'invoke-exact-updater.ps1'
[IO.File]::WriteAllText($wrapperPath,$wrapper,(New-Object Text.UTF8Encoding($false)))

function Run-Case($Case,[int]$ExpectedExit,[string]$ExpectedLog,[int]$Downloads,[int]$Confirmations) {
  $firstCheck=$script:checks.Count
  $result=[ordered]@{name=$Case.name;status='failed';expected_exit=$ExpectedExit;actual_exit=$null;checks=@();temp_environment=$null}
  [void]$script:cases.Add($result)
  $arguments='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File '+(Quote-Argument $wrapperPath)+' -ScenarioFile '+(Quote-Argument $Case.config)
  $process=Start-Process -FilePath $nativePowerShell -ArgumentList $arguments -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $Case.folder 'stdout.log') -RedirectStandardError (Join-Path $Case.folder 'stderr.log')
  $result.actual_exit=$process.ExitCode
  $tempEvidence=Join-Path $Case.folder 'temp-environment.json'
  if ([IO.File]::Exists($tempEvidence)) {
    $result.temp_environment=Get-Content -LiteralPath $tempEvidence -Raw | ConvertFrom-Json
    $tempSummary=$result.temp_environment | ConvertTo-Json -Depth 6 -Compress
    Write-Host ('::notice title=Isolated updater TEMP evidence::'+(Escape-Annotation ($Case.name+': '+$tempSummary)))
  }
  Assert-Check ($process.ExitCode -eq $ExpectedExit) ($Case.name+': expected exit code')
  Assert-Check ($null -ne $result.temp_environment -and $result.temp_environment.isolated.relative_path_correct) ($Case.name+': isolated TEMP preserves exact updater relative paths')
  $logs=@(Get-ChildItem -LiteralPath (Join-Path $Case.root 'Update-Logs') -File)
  Assert-Check ($logs.Count -gt 0) ($Case.name+': real updater log created')
  $content=($logs | ForEach-Object {Get-Content -LiteralPath $_.FullName -Raw}) -join ([Environment]::NewLine)
  Assert-Check ($content.Contains($ExpectedLog)) ($Case.name+': expected real updater outcome')
  $calls=@(Get-Content -LiteralPath (Join-Path $Case.folder 'mock-calls.jsonl') | ForEach-Object {$_ | ConvertFrom-Json})
  Assert-Check (@($calls | Where-Object {$_.kind -eq 'metadata'}).Count -eq 1) ($Case.name+': exact GitHub latest-release request')
  Assert-Check (@($calls | Where-Object {$_.kind -eq 'download'}).Count -eq $Downloads) ($Case.name+': expected ZIP/checksum downloads')
  Assert-Check (@($calls | Where-Object {$_.kind -eq 'confirmation'}).Count -eq $Confirmations) ($Case.name+': expected confirmation behavior')
  $result.status='passed'
  $result.checks=@($script:checks | Select-Object -Skip $firstCheck)
  return $result
}

try {
  $report.archive_sha256=Hash-File $Archive
  $packageVersion=Read-PackageVersion $Archive
  $report.package_version=$packageVersion
  $report.updater_sha256=Hash-File $updaterFixture
  $report.batch_fixture_sha256=Hash-File $batchFixture
  Assert-Check ($report.updater_sha256 -eq $expectedUpdaterHash) 'Exact uploaded PowerShell updater bytes are unchanged'
  Assert-Check ($report.batch_fixture_sha256 -eq $expectedBatchHash) 'Exact uploaded BAT fixture bytes are unchanged'
  Assert-Check ($report.archive_name -match '(?i)(win(dows)?|x64|amd64)' -and $report.archive_name -match '(?i)\.zip$') 'Candidate asset name satisfies the real Windows asset filter'
  $expectedHash=((Get-Content -LiteralPath $Checksum -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
  Assert-Check ($expectedHash -eq $report.archive_sha256) 'Supplied release checksum matches candidate bytes'
  $payload=@(Read-Payload $Archive)
  Assert-Check ($payload.Count -gt 0) 'Candidate has a nonempty app/ payload'
  Assert-Check (@($payload | Where-Object {$_.relative -eq 'VERSION.txt'}).Count -eq 1) 'Candidate contains app/VERSION.txt'
  Assert-Check (@($payload | Where-Object {$_.relative -eq 'forge\__init__.py'}).Count -eq 1) 'Candidate contains the installed application module'

  $positive=New-Case 'install' $Archive $Checksum 'UPDATE'
  & $PythonExecutable -c "import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)" (Join-Path $positive.root 'forge\__init__.py')
  Assert-Check ($LASTEXITCODE -eq 0) 'Seed a real old timestamp-based Python cache before updating'
  $null=Run-Case $positive 0 'SUCCESS: Updated FORGE' 2 1
  $probe="import sys; sys.path.insert(0, sys.argv[1]); from forge import __version__; print(__version__)"
  $cachedVersion=& $PythonExecutable -c $probe $positive.root
  Assert-Check ($LASTEXITCODE -eq 0) 'Probe the installed version with existing bytecode caches'
  if ($RefreshBytecode) {
    Assert-Check ($cachedVersion -eq ($packageVersion -replace '[0-9]', '9')) 'Published fixed-timestamp ZIP reproduces stale-cache behavior'
    & $PythonExecutable -m compileall --invalidation-mode checked-hash -q -f (Join-Path $positive.root 'forge') (Join-Path $positive.root 'scripts') (Join-Path $positive.root 'launcher.py') (Join-Path $positive.root 'server.py')
    Assert-Check ($LASTEXITCODE -eq 0) 'Documented cache refresh compiles application code only'
    $cachedVersion=& $PythonExecutable -c $probe $positive.root
    Assert-Check ($LASTEXITCODE -eq 0) 'Probe the refreshed installed application'
  }
  Assert-Check ($cachedVersion -eq $packageVersion) 'Existing Python cache cannot retain the previous application version'
  $report.cached_upgrade=[ordered]@{status='passed'; refresh_required=[bool]$RefreshBytecode; version=$cachedVersion}
  $report.installation_evidence=Read-InstallationEvidence $positive
  foreach ($file in $payload) {
    Assert-PayloadFile $positive.root $file
  }
  foreach ($name in $positive.preserved.Keys) {
    Assert-Check ((Hash-File (Join-Path $positive.root $name)) -eq $positive.preserved[$name]) ('install: preserved '+$name)
  }
  $backups=@(Get-ChildItem -LiteralPath (Join-Path $positive.root 'Update-Backups') -Directory)
  Assert-Check ($backups.Count -eq 1) 'install: exactly one real backup created'
  $backup=$backups[0].FullName
  foreach ($name in $positive.oldFiles.Keys) {
    Assert-Check ((Hash-File (Join-Path $backup $name)) -eq $positive.oldFiles[$name]) ('install: backup contains old bytes '+$name)
  }
  $rollbackEntries=Get-Content -LiteralPath (Join-Path $backup 'rollback-manifest.json') -Raw | ConvertFrom-Json
  $rollback=@($rollbackEntries | ForEach-Object {$_})
  Assert-Check ($rollback.Count -eq $payload.Count) 'install: rollback manifest records every application path'
  foreach ($file in $payload) {
    $record=@($rollback | Where-Object {$_.path -eq $file.relative})
    Assert-Check ($record.Count -eq 1 -and $record[0].existed -eq $positive.oldFiles.ContainsKey($file.relative)) ('install: rollback existence flag '+$file.relative)
  }

  $cancelled=New-Case 'cancelled' $Archive $Checksum 'CANCEL'
  $null=Run-Case $cancelled 0 'Cancelled. No files modified.' 2 1
  Assert-Unchanged $cancelled.root $cancelled.before 'cancelled'
  Assert-Check (-not (Test-Path -LiteralPath (Join-Path $cancelled.root 'Update-Backups'))) 'cancelled: no application backup or copy phase started'

  $badChecksum=Join-Path $Output 'wrong-checksum.sha256'
  Write-Fixture $badChecksum (('0'*64)+'  '+$report.archive_name)
  $mismatch=New-Case 'wrong-checksum' $Archive $badChecksum 'UPDATE'
  $null=Run-Case $mismatch 1 'SHA256 mismatch. Nothing installed.' 2 0
  Assert-Unchanged $mismatch.root $mismatch.before 'wrong-checksum'
  Assert-Check (-not (Test-Path -LiteralPath (Join-Path $mismatch.root 'Update-Backups'))) 'wrong-checksum: no application backup or copy phase started'

  $noAsset=New-Case 'no-windows-asset' $Archive $Checksum 'UPDATE' $true
  $null=Run-Case $noAsset 1 'Expected exactly one Windows ZIP asset' 0 0
  Assert-Unchanged $noAsset.root $noAsset.before 'no-windows-asset'
  Assert-Check (-not (Test-Path -LiteralPath (Join-Path $noAsset.root 'Update-Backups'))) 'no-windows-asset: no application backup or copy phase started'

  $protectedZip=Join-Path $Output 'protected-payload.zip'
  New-TestZip $protectedZip @{
    'app/forge/__init__.py'='# This file must not be applied when another entry is protected.'
    'app/Settings/settings.json'='{"attempt":"overwrite-private-settings"}'
  }
  $protectedSha=Join-Path $Output 'protected-payload.sha256'
  Write-Fixture $protectedSha ((Hash-File $protectedZip)+'  '+$report.archive_name)
  $protectedCase=New-Case 'protected-payload' $protectedZip $protectedSha 'UPDATE'
  $null=Run-Case $protectedCase 1 'Release attempts to overwrite protected path:' 2 0
  Assert-Unchanged $protectedCase.root $protectedCase.before 'protected-payload'
  Assert-Check (-not (Test-Path -LiteralPath (Join-Path $protectedCase.root 'Update-Backups'))) 'protected-payload: complete preflight rejects all changes before backup/copy'

  $junction=New-Case 'existing-junction' $Archive $Checksum 'UPDATE' $false $true
  $outsideHash=Hash-File $junction.outside
  $null=Run-Case $junction 1 'Refusing to change symbolic link/junction:' 2 0
  Assert-Unchanged $junction.root $junction.before 'existing-junction'
  Assert-Check ((Hash-File $junction.outside) -eq $outsideHash) 'existing-junction: target outside the installation remains unchanged'
  Assert-Check (-not (Test-Path -LiteralPath (Join-Path $junction.root 'Update-Backups'))) 'existing-junction: no application backup or copy phase started'

  $smokeReport=Join-Path $positive.folder 'installed-app-smoke.json'
  $smokeData=Join-Path $positive.folder 'smoke-user-data'
  $version=(Get-Content -LiteralPath (Join-Path $positive.root 'VERSION.txt') -Raw).Trim()
  $arguments=(Quote-Argument $smokeScript)+' --application-root '+(Quote-Argument $positive.root)+' --data-dir '+(Quote-Argument $smokeData)+' --expected-version '+(Quote-Argument $version)+' --report '+(Quote-Argument $smokeReport)
  $smoke=Start-Process -FilePath $PythonExecutable -ArgumentList $arguments -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $positive.folder 'installed-app-smoke.stdout.log') -RedirectStandardError (Join-Path $positive.folder 'installed-app-smoke.stderr.log')
  Assert-Check ($smoke.ExitCode -eq 0) 'Installed application smoke exited successfully'
  $report.installed_app_smoke=Get-Content -LiteralPath $smokeReport -Raw | ConvertFrom-Json
  Assert-Check ($report.installed_app_smoke.status -eq 'passed') 'Installed application imports, authenticates and executes a real QR worker'
  Assert-Check ($report.installed_app_smoke.native_windows_execution -eq $true) 'Installed application smoke ran on native Windows'
  foreach ($name in $positive.preserved.Keys) {
    Assert-Check ((Hash-File (Join-Path $positive.root $name)) -eq $positive.preserved[$name]) ('installed smoke: preserved '+$name)
  }
  $report.status='passed'
} catch {
  $report.failure=Sanitize-Diagnostic $_.Exception.Message
  $report.failure_location=[ordered]@{
    file='tests/windows_release_acceptance.ps1'; line=$_.InvocationInfo.ScriptLineNumber
    stack=(Sanitize-Diagnostic $_.ScriptStackTrace)
  }
  Write-Host ('FAIL '+$report.failure)
  $annotation=$report.failure+'; '+$report.failure_location.stack
  if ($null -ne $report.failure_detail) {
    $annotation+='; '+($report.failure_detail | ConvertTo-Json -Depth 5 -Compress)
  }
  Write-Host ('::error file=tests/windows_release_acceptance.ps1,line='+$report.failure_location.line+',title=Windows release acceptance failed::'+(Escape-Annotation $annotation))
  if ($null -ne $report.installation_evidence) {
    foreach ($field in @('installation_root','root_type','root_exists','root_entries','files','rollback_manifests','updater_stdout','updater_stderr','temp_environment','diagnostic_error')) {
      $value=$report.installation_evidence[$field] | ConvertTo-Json -Depth 8 -Compress
      Write-Host ('::error title=Disposable installation evidence::'+(Escape-Annotation ($field+'='+$value)))
    }
  }
} finally {
  $report.passed=$checks.Count
  Write-Json (Join-Path $Output 'windows-acceptance.json') $report
}
if ($report.status -ne 'passed') { exit 1 }
Write-Host ('Native Windows acceptance passed: '+$checks.Count+' checks. Report: '+(Join-Path $Output 'windows-acceptance.json'))
