[CmdletBinding()]
param(
  [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }),
  [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$CodexRoot = [IO.Path]::GetFullPath($CodexHome)
$ReceiptRoot = Join-Path $CodexRoot 'receipts'
$Issues = @()
$Commands = @('powershell -ExecutionPolicy Bypass -File .\install.ps1 -WhatIf')

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
    if (@('codexpro.install-receipt/v2','codexpro.install-receipt/v3') -notcontains [string]$Value.schema) {
      throw 'unsupported install receipt schema'
    }
    foreach ($Record in $Value.files) {
      $Path = Get-SafeChild $CodexRoot ([string]$Record.path)
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

$Agbrowse = Get-Command agbrowse.cmd,agbrowse -ErrorAction SilentlyContinue | Select-Object -First 1
if (!$Agbrowse) {
  $Issues += @{code='AGBROWSE_MISSING'; detail='External dependency not found'}
  $Commands += 'powershell -ExecutionPolicy Bypass -File .\update.ps1 -AgbrowseVersion 0.1.18'
}

$Python = Get-Command python.exe,python -ErrorAction SilentlyContinue | Select-Object -First 1
$UpdateReceiptPath = Join-Path $CodexRoot 'agbrowse-update-receipt.json'
$UpdateReceipt = $null
$SelectedVersion = '0.1.18'
$SelectedIntegrity = $null
$Contract = Join-Path $CodexRoot 'contracts/agbrowse-0.1.18.json'
if (Test-Path -LiteralPath $UpdateReceiptPath) {
  try {
    $UpdateReceipt = Get-Content -LiteralPath $UpdateReceiptPath -Raw | ConvertFrom-Json
    if ($UpdateReceipt.schema -ne 'codexpro.agbrowse-update-receipt/v2') { throw 'unsupported update receipt schema' }
    $SelectedVersion = [string]$UpdateReceipt.selected_version
    $SelectedIntegrity = [string]$UpdateReceipt.integrity
    $Contract = [IO.Path]::GetFullPath([string]$UpdateReceipt.contract)
    if (!(Test-IsWithinRoot (Join-Path $CodexRoot 'contracts') $Contract)) { throw 'update contract path escapes CODEX_HOME' }
  } catch {
    $Issues += @{code='UPDATE_RECEIPT_INVALID'; detail=$_.Exception.Message}
    $UpdateReceipt = $null
  }
}

if (!$Python -or !(Test-Path -LiteralPath $Contract)) {
  $Issues += @{code='CONTRACT_UNVERIFIED'; detail='Python or contract manifest unavailable'}
} else {
  if ($UpdateReceipt -and (Get-Sha256 $Contract) -ne [string]$UpdateReceipt.contract_sha256) {
    $Issues += @{code='CONTRACT_RECEIPT_HASH_MISMATCH'; contract=$Contract}
  } else {
    $Arguments = @(
      (Join-Path $CodexRoot 'bin/chatgpt_agbrowse_contract.py'),
      'validate', '--manifest', $Contract
    )
    if ($SelectedIntegrity) {
      $Arguments += @('--expected-version',$SelectedVersion,'--expected-integrity',$SelectedIntegrity)
    }
    & $Python.Source @Arguments
    if ($LASTEXITCODE) { $Issues += @{code='CONTRACT_INVALID'; contract=$Contract} }
  }

  if ($Agbrowse) {
    try {
      $ContractValue = Get-Content -LiteralPath $Contract -Raw | ConvertFrom-Json
      $ActualExecutableHash = Get-Sha256 $Agbrowse.Source
      if ($ActualExecutableHash -ne $ContractValue.agbrowse.executableSha256) {
        $Issues += @{code='AGBROWSE_EXECUTABLE_HASH_MISMATCH'; actual=$ActualExecutableHash; contract=$ContractValue.agbrowse.executableSha256}
      }
      if ($UpdateReceipt -and $ActualExecutableHash -ne [string]$UpdateReceipt.executable_sha256) {
        $Issues += @{code='AGBROWSE_UPDATE_RECEIPT_EXECUTABLE_MISMATCH'; actual=$ActualExecutableHash}
      }
    } catch {
      $Issues += @{code='CONTRACT_READ_FAILED'; detail=$_.Exception.Message}
    }
  }
}

[ordered]@{
  schema = 'codexpro.doctor/v2'
  codex_home = $CodexRoot
  receipt = $(if ($Receipt) { $Receipt.FullName } else { $null })
  status = $(if ($Issues) { 'FAIL' } else { 'PASS' })
  issues = $Issues
  commands = $Commands
  agbrowse = @{selected_version=$SelectedVersion; contract=$Contract; update_receipt=$UpdateReceiptPath}
  codexpro = @{
    installation = 'external'
    detail = 'CodexPro is not installed by install.ps1; app bootstrap scripts acquire the latest supported external runtime.'
  }
  what_if = [bool]$WhatIf
} | ConvertTo-Json -Depth 7
if ($Issues) { exit 1 }
