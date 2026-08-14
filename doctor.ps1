[CmdletBinding()]
param(
  [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }),
  [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$CodexRoot = [IO.Path]::GetFullPath($CodexHome)
$ReceiptRoot = Join-Path $CodexRoot 'receipts'
$Issues = @()
$Warnings = @()
$Commands = @('powershell -ExecutionPolicy Bypass -File .\install.ps1 -WhatIf')
$InstallReceiptValue = $null
$ReceiptFiles = @{}

function Get-Sha256([string]$Path) {
  $stream = $null
  $sha256 = $null
  try {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    (([BitConverter]::ToString($sha256.ComputeHash($stream))) -replace '-', '').ToLowerInvariant()
  } finally {
    if ($sha256) { $sha256.Dispose() }
    if ($stream) { $stream.Dispose() }
  }
}

function Test-IsWithinRoot([string]$Root, [string]$Path) {
  $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar,[IO.Path]::AltDirectorySeparatorChar)
  $candidate = [IO.Path]::GetFullPath($Path)
  $candidate.StartsWith($rootPath + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)
}

function Get-SafeChild([string]$Root, [string]$Relative) {
  if ([string]::IsNullOrWhiteSpace($Relative) -or [IO.Path]::IsPathRooted($Relative) -or $Relative -match '(^|[\/])\.{1,2}([\/]|$)') {
    throw "unsafe receipt path: $Relative"
  }
  $candidate = [IO.Path]::GetFullPath((Join-Path $Root $Relative))
  if (!(Test-IsWithinRoot $Root $candidate)) { throw "receipt path escapes CODEX_HOME: $Relative" }
  $candidate
}

$Receipt = Get-ChildItem -LiteralPath $ReceiptRoot -Filter 'codexpro-automation-*.json' -File -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if (!$Receipt) {
  $Issues += @{code='RECEIPT_MISSING'; detail='No install receipt found'}
} else {
  try {
    $Value = Get-Content -LiteralPath $Receipt.FullName -Raw | ConvertFrom-Json
    $InstallReceiptValue = $Value
    if (@('codexpro.install-receipt/v2','codexpro.install-receipt/v3','codexpro.install-receipt/v4') -notcontains [string]$Value.schema) {
      throw 'unsupported install receipt schema'
    }
    foreach ($Record in $Value.files) {
      $Relative = ([string]$Record.path).Replace('\','/')
      $ReceiptFiles[$Relative] = $true
      $Path = Get-SafeChild $CodexRoot $Relative
      if ($Record.action -eq 'removed') {
        if (Test-Path -LiteralPath $Path) { $Issues += @{code='REMOVED_FILE_PRESENT'; path=$Record.path} }
        continue
      }
      if (!(Test-Path -LiteralPath $Path)) {
        $Issues += @{code='FILE_MISSING'; path=$Record.path}
        continue
      }
      $Actual = Get-Sha256 $Path
      if ($Actual -ne $Record.installed_sha256) {
        $Issues += @{code='HASH_MISMATCH'; path=$Record.path; actual=$Actual}
      }
    }
  } catch {
    $Issues += @{code='RECEIPT_INVALID'; detail=$_.Exception.Message}
  }
}

$Node = Get-Command node.exe,node -ErrorAction SilentlyContinue | Select-Object -First 1
$Npx = Get-Command npx.cmd,npx -ErrorAction SilentlyContinue | Select-Object -First 1
$GitBash = Get-Item -LiteralPath 'C:\Program Files\Git\bin\bash.exe' -ErrorAction SilentlyContinue
if (!$Node -or !$Npx) {
  $Issues += @{code='ORACLE_DEVSPACE_NODE_TOOLING_MISSING'; detail='Node and npx are required for Oracle and DevSpace'}
} else {
  try {
    $NodeVersion = (& $Node.Source --version).Trim().TrimStart('v')
    $NodeMajor = [int]($NodeVersion.Split('.')[0])
    if ($NodeMajor -lt 24 -or $NodeMajor -ge 27) {
      $Issues += @{code='DEVSPACE_NODE_VERSION_UNSUPPORTED'; actual=$NodeVersion; required='>=24 <27'}
    }
  } catch {
    $Issues += @{code='NODE_VERSION_UNREADABLE'; detail=$_.Exception.Message}
  }
}
if (!$GitBash) {
  $Issues += @{code='DEVSPACE_GIT_BASH_MISSING'; detail='Windows DevSpace requires Git Bash'}
}
$Commands += 'npx -y @steipete/oracle@0.17.3 --version'
$Commands += 'python .\skills\chatgpt-workspace-setup\scripts\devspace_tailscale_setup.py doctor --root C:\project --hostname your-device.your-tailnet.ts.net'

$Python = Get-Command python.exe,python -ErrorAction SilentlyContinue | Select-Object -First 1
$CompatibilityProbe = @'
import ast,json,sys
root=ast.parse(open(sys.argv[1],encoding='utf-8').read())
values={}
refs={}
for node in root.body:
    if not isinstance(node,ast.Assign) or len(node.targets)!=1 or not isinstance(node.targets[0],ast.Name):
        continue
    name=node.targets[0].id
    try:
        values[name]=ast.literal_eval(node.value)
    except (ValueError,TypeError):
        if isinstance(node.value,ast.Dict):
            refs[name]={ast.literal_eval(key):value.id for key,value in zip(node.value.keys,node.value.values) if isinstance(value,ast.Name)}
versions={version:values[name] for version,name in refs.get('VERSION_PATCHES',{}).items()} or {values['SUPPORTED_VERSION']:values['PATCHES']}
print(json.dumps([{'version':str(version),'patch':str(contract['patch'])} for version,patches in versions.items() for contract in patches.values()]))
'@
if ($Python) {
  foreach ($Compat in @(
    @{module='bin/chatgpt_oracle_compat.py'; asset_root='bin/oracle-compat'},
    @{module='bin/chatgpt_devspace_compat.py'; asset_root='bin/devspace-compat'}
  )) {
    if (!$ReceiptFiles.ContainsKey($Compat.module)) { continue }
    try {
      $ModulePath = Get-SafeChild $CodexRoot $Compat.module
      $ProbeOutput = @(& $Python.Source -c $CompatibilityProbe $ModulePath)
      if ($LASTEXITCODE) { throw "compatibility reference probe failed with exit code $LASTEXITCODE" }
      $References = (($ProbeOutput -join [Environment]::NewLine) | ConvertFrom-Json)
      foreach ($Reference in $References) {
        $Relative = "$($Compat.asset_root)/$($Reference.version)/$($Reference.patch)"
        $PatchPath = Get-SafeChild $CodexRoot $Relative
        if (!$ReceiptFiles.ContainsKey($Relative) -or !(Test-Path -LiteralPath $PatchPath -PathType Leaf)) {
          $Issues += @{code='COMPAT_PATCH_ASSET_MISSING'; module=$Compat.module; path=$Relative}
        }
      }
    } catch {
      $Issues += @{code='COMPAT_REFERENCE_INVALID'; module=$Compat.module; detail=$_.Exception.Message}
    }
  }
}
if (!$Python) {
  $Issues += @{code='PYTHON_MISSING'; detail='Python is required for the installed automation'}
}

[ordered]@{
  schema = 'codexpro.doctor/v2'
  codex_home = $CodexRoot
  receipt = $(if ($Receipt) { $Receipt.FullName } else { $null })
  status = $(if ($Issues) { 'FAIL' } else { 'PASS' })
  issues = $Issues
  warnings = $Warnings
  commands = $Commands
  oracle = @{package='@steipete/oracle@0.17.3';tested_version='0.17.3';resolution='exact npx runtime pin'}
  devspace = @{package='@waishnav/devspace';tested_version='1.0.7';setup='explicit setup skill only'}
  what_if = [bool]$WhatIf
} | ConvertTo-Json -Depth 7
if ($Issues) { exit 1 }
