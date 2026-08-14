[CmdletBinding()]
param(
  [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }),
  [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$CodexRoot = [IO.Path]::GetFullPath($CodexHome)
$RepoRoot = $PSScriptRoot
$ReceiptRoot = Join-Path $CodexRoot 'receipts'
$BackupRoot = Join-Path $CodexRoot 'backups'
$Issues = @()
$Warnings = @()
$Commands = @('powershell -ExecutionPolicy Bypass -File .\install.ps1 -WhatIf')
$InstallReceiptValue = $null
$ReceiptFiles = @{}
$Manifest = $null
$ManifestVersion = $null
$ManifestOk = $false
$ManifestFiles = @()
$OracleContract = $null
$OraclePackage = $null
$OracleVersion = $null
$OracleValidated = $false
$StateModulePath = $null
$Receipt = $null
$InstallJournal = $null
$ReceiptValid = $false
$ReceiptCurrent = $false
$PathCoverageOk = $false
$StateRecordOk = $false

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

function Test-PathEqual([string]$Left, [string]$Right) {
  if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) { return $false }
  ([IO.Path]::GetFullPath($Left)).Equals([IO.Path]::GetFullPath($Right), [StringComparison]::OrdinalIgnoreCase)
}

function Test-Sha256Value($Value) {
  $Value -is [string] -and $Value -match '^[a-f0-9]{64}$'
}

function Test-IsWithinRoot([string]$Root, [string]$Path) {
  $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
  $candidate = [IO.Path]::GetFullPath($Path)
  $candidate.StartsWith($rootPath + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

function Get-SafeChild([string]$Root, [string]$Relative) {
  if ([string]::IsNullOrWhiteSpace($Relative) -or [IO.Path]::IsPathRooted($Relative) -or $Relative -match '(^|[\\/])\.{1,2}([\\/]|$)') {
    throw "unsafe relative path: $Relative"
  }
  $candidate = [IO.Path]::GetFullPath((Join-Path $Root $Relative))
  if (!(Test-IsWithinRoot $Root $candidate)) { throw "path escapes root: $Relative" }
  $cursor = Split-Path -Parent $candidate
  while ((Test-IsWithinRoot $Root $cursor) -and $cursor -ne [IO.Path]::GetFullPath($Root)) {
    if (Test-Path -LiteralPath $cursor) {
      $item = Get-Item -LiteralPath $cursor -Force
      if ($item.LinkType) { throw "symlink/reparse path refused: $cursor" }
    }
    $cursor = Split-Path -Parent $cursor
  }
  $candidate
}

function Get-ManifestFiles([string]$Root, $Value) {
  $files = @()
  $itemsByBase = @{}
  foreach ($pattern in $Value.include) {
    if ($pattern -match '(^|/)\.{1,2}($|/)' -or [IO.Path]::IsPathRooted($pattern)) { throw "unsafe manifest pattern: $pattern" }
    $base = if ($pattern.StartsWith('bin/')) { Join-Path $Root 'bin' }
      elseif ($pattern.StartsWith('skills/')) { Join-Path $Root 'skills' }
      elseif ($pattern.StartsWith('mcp_servers/')) { Join-Path $Root 'mcp_servers' }
      elseif ($pattern.StartsWith('scripts/')) { Join-Path $Root 'scripts' }
      elseif ($pattern.StartsWith('contracts/')) { Join-Path $Root 'contracts' }
      elseif ($pattern.StartsWith('tests/fixtures/')) { Join-Path $Root 'tests/fixtures' }
      else { throw "unsupported manifest root: $pattern" }
    if (!$itemsByBase.ContainsKey($base)) {
      $baseItems = @()
      foreach ($item in @(Get-ChildItem -LiteralPath $base -File -Recurse -Force)) {
        if ($item.LinkType) { throw "manifest refuses symlink: $($item.FullName)" }
        $relative = $item.FullName.Substring($Root.Length).TrimStart([char[]]'\/').Replace('\', '/')
        [void](Get-SafeChild $Root $relative)
        $baseItems += $relative
      }
      $itemsByBase[$base] = @($baseItems)
    }
    $patternMatches = @($itemsByBase[$base] | Where-Object { $_ -like $pattern })
    if (!$patternMatches.Count) { throw "manifest pattern matched no files: $pattern" }
    $files += $patternMatches
  }
  @($files | Sort-Object -Unique)
}

function Assert-ReceiptBinding([string]$Root, $Journal, $ReceiptValue) {
  if ($Journal.schema -eq 'codexpro.install-wal/v1') { return }
  $receiptRoot = Join-Path $Root 'receipts'
  if (!(Test-IsWithinRoot $receiptRoot ([string]$Journal.receipt))) { throw 'receipt_binding_ambiguous: receipt path is outside CODEX_HOME/receipts' }
  if ($ReceiptValue.files -isnot [System.Array]) { throw 'receipt_binding_ambiguous: receipt files must be an array' }
  $expectedSchema = if ($Journal.schema -eq 'codexpro.install-wal/v2') { 'codexpro.install-receipt/v3' } else { 'codexpro.install-receipt/v4' }
  if ($ReceiptValue.schema -ne $expectedSchema -or [string]$ReceiptValue.transaction_id -ne [string]$Journal.transaction_id) { throw 'receipt_binding_ambiguous: receipt identity mismatch' }
  if (!(Test-PathEqual ([string]$ReceiptValue.wal) ([string]$Journal.wal_path)) -or !(Test-PathEqual ([string]$ReceiptValue.backup) ([string]$Journal.backup))) { throw 'receipt_binding_ambiguous: receipt path binding mismatch' }
  if ([string]$ReceiptValue.manifest_version -ne [string]$Journal.manifest_version) { throw 'receipt_binding_ambiguous: manifest version mismatch' }
  $expected = @($Journal.files); $observed = @($ReceiptValue.files)
  if ($expected.Count -ne $observed.Count) { throw 'receipt_binding_ambiguous: receipt file count mismatch' }
  for ($index = 0; $index -lt $expected.Count; $index++) {
    $left = $expected[$index]; $right = $observed[$index]
    foreach ($field in @('path', 'action')) { if ($right.$field -isnot [string]) { throw "receipt_binding_ambiguous: receipt file field $field must be a string at index $index" } }
    if ($null -ne $right.backup_sha256 -and $right.backup_sha256 -isnot [string]) { throw "receipt_binding_ambiguous: receipt backup_sha256 must be null or string at index $index" }
    if (($null -eq $left.backup_sha256) -ne ($null -eq $right.backup_sha256)) { throw "receipt_binding_ambiguous: receipt backup_sha256 nullability mismatch at index $index" }
    $fields = if ($left.action -eq 'removed') { @('path', 'action', 'expected_sha256', 'backup', 'backup_sha256', 'expected_absence', 'transaction_id', 'replacement', 'rollback_binding') } else { @('path', 'action', 'installed_sha256', 'backup_sha256') }
    foreach ($field in $fields) { if ([string]$left.$field -ne [string]$right.$field) { throw "receipt_binding_ambiguous: receipt file record mismatch at index $index" } }
  }
}

function Assert-InstallWal([string]$Root, $Journal, [string]$DiscoveredWalPath) {
  foreach ($field in @('schema', 'status', 'backup')) { if ($Journal.$field -isnot [string]) { throw "install WAL field $field must be a string" } }
  if ($Journal.files -isnot [System.Array]) { throw 'install WAL files must be an array' }
  if (@('codexpro.install-wal/v1', 'codexpro.install-wal/v2', 'codexpro.install-wal/v3') -notcontains [string]$Journal.schema) { throw 'unsupported install WAL schema' }
  $validStatuses = if ($Journal.schema -eq 'codexpro.install-wal/v1') { @('ACTIVE', 'COMPLETE', 'ROLLED_BACK_AFTER_CRASH') } else { @('ACTIVE', 'COMPLETE', 'ROLLED_BACK_AFTER_CRASH', 'ROLLED_BACK_AFTER_ERROR') }
  if ($validStatuses -notcontains [string]$Journal.status) { throw 'invalid install WAL status' }
  if ([string]::IsNullOrWhiteSpace([string]$Journal.backup) -or [IO.Path]::IsPathRooted([string]$Journal.backup) -eq $false) { throw 'invalid install WAL backup' }
  if (!(Test-IsWithinRoot (Join-Path $Root 'backups') ([string]$Journal.backup))) { throw 'install WAL backup is outside CODEX_HOME/backups' }
  $orders = @{
    'codexpro.install-wal/v1' = @('INTENT', 'MUTATED', 'VERIFIED', 'COMPLETE')
    'codexpro.install-wal/v2' = @('INTENT', 'BACKUP_DURABLE', 'MUTATED', 'VERIFIED', 'REPLACEMENT_RECEIPT_DURABLE', 'COMPLETE')
    'codexpro.install-wal/v3' = @('INTENT', 'BACKUP_DURABLE', 'MUTATED', 'VERIFIED', 'REPLACEMENT_RECEIPT_DURABLE', 'COMPLETE')
  }
  if ($Journal.schema -in @('codexpro.install-wal/v2', 'codexpro.install-wal/v3')) {
    foreach ($field in @('transaction_id', 'manifest_version', 'receipt', 'wal_path')) { if ($Journal.$field -isnot [string]) { throw "WAL v2 field $field must be a string" } }
    if ([string]$Journal.transaction_id -notmatch '^[a-f0-9]{32}$') { throw 'invalid WAL v2 transaction_id' }
    if ([string]::IsNullOrWhiteSpace([string]$Journal.manifest_version)) { throw 'invalid WAL v2 manifest_version' }
    if (!(Test-PathEqual ([string]$Journal.wal_path) $DiscoveredWalPath)) { throw 'WAL v2 serialized wal_path mismatch' }
    if (!(Test-PathEqual ([string]$Journal.backup) (Split-Path -Parent $DiscoveredWalPath))) { throw 'WAL v2 backup does not own discovered WAL' }
    if (!(Test-IsWithinRoot (Join-Path $Root 'receipts') ([string]$Journal.receipt))) { throw 'receipt_binding_ambiguous: WAL v2 receipt path is outside CODEX_HOME/receipts' }
  }
  $seen = @{}; $entries = @($Journal.files)
  for ($index = 0; $index -lt $entries.Count; $index++) {
    $entry = $entries[$index]
    foreach ($field in @('path', 'action', 'phase', 'replacement')) { if ($entry.$field -isnot [string]) { throw "install WAL file field $field must be a string" } }
    if ($entry.transitions -isnot [System.Array]) { throw 'install WAL transitions must be an array' }
    foreach ($transition in @($entry.transitions)) { if ($transition -isnot [string]) { throw 'install WAL transition values must be strings' } }
    if ($null -ne $entry.backup_sha256 -and $entry.backup_sha256 -isnot [string]) { throw 'install WAL backup_sha256 must be null or string' }
    $relativePath = [string]$entry.path
    if ([string]::IsNullOrWhiteSpace($relativePath) -or [IO.Path]::IsPathRooted($relativePath) -or $relativePath -match '(^|[\\/])\.{1,2}([\\/]|$)' -or !(Test-IsWithinRoot $Root ([IO.Path]::GetFullPath((Join-Path $Root $relativePath))))) { throw 'unsafe install WAL destination' }
    if ($seen.ContainsKey([string]$entry.path)) { throw 'duplicate install WAL destination' }; $seen[[string]$entry.path] = $true
    $validActions = if ($Journal.schema -eq 'codexpro.install-wal/v3') { @('created', 'overwritten', 'removed') } else { @('created', 'overwritten') }
    if ($validActions -notcontains [string]$entry.action) { throw 'invalid install WAL action' }
    if ($entry.action -ne 'removed' -and !(Test-Sha256Value $entry.installed_sha256)) { throw 'invalid installed_sha256 in install WAL' }
    if ([string]::IsNullOrWhiteSpace([string]$entry.replacement) -or !(Test-IsWithinRoot ([string]$Journal.backup) ([string]$entry.replacement))) { throw 'invalid install WAL replacement path' }
    $order = @($orders[[string]$Journal.schema]); $phaseIndex = [Array]::IndexOf($order, [string]$entry.phase)
    if ($phaseIndex -lt 0) { throw 'invalid install WAL phase' }
    $transitions = @($entry.transitions); $expectedTransitions = @($order[0..$phaseIndex])
    if (($transitions -join '|') -ne ($expectedTransitions -join '|')) { throw 'invalid install WAL transition order' }
    if ($Journal.schema -in @('codexpro.install-wal/v2', 'codexpro.install-wal/v3')) {
      if (($entry.sequence_number -isnot [int] -and $entry.sequence_number -isnot [long]) -or [int64]$entry.sequence_number -ne $index) { throw 'invalid WAL v2 sequence_number' }
      if ($entry.action -eq 'created' -and $null -ne $entry.backup_sha256) { throw 'created WAL entry cannot carry backup_sha256' }
      if ($entry.action -eq 'overwritten' -and $phaseIndex -eq 0 -and $null -ne $entry.backup_sha256) { throw 'overwritten WAL INTENT entry cannot carry backup_sha256' }
      if ($entry.action -eq 'overwritten' -and $phaseIndex -ge 1 -and !(Test-Sha256Value $entry.backup_sha256)) { throw 'overwritten WAL entry lacks durable backup hash' }
      if ($entry.action -eq 'removed') {
        foreach ($field in @('expected_sha256', 'backup', 'transaction_id', 'rollback_binding')) { if ($entry.$field -isnot [string]) { throw "removed WAL entry field $field must be a string" } }
        if (!(Test-Sha256Value $entry.expected_sha256) -or $entry.expected_absence -ne $true) { throw 'invalid removed WAL expected state' }
        if ([string]$entry.transaction_id -ne [string]$Journal.transaction_id -or [string]$entry.rollback_binding -ne [string]$Journal.transaction_id) { throw 'removed WAL transaction binding mismatch' }
        if (!(Test-PathEqual ([string]$entry.backup) (Get-SafeChild ([string]$Journal.backup) ([string]$entry.path)))) { throw 'removed WAL backup path mismatch' }
        if ($phaseIndex -eq 0 -and $null -ne $entry.backup_sha256) { throw 'removed WAL INTENT entry cannot carry backup_sha256' }
        if ($phaseIndex -ge 1 -and !(Test-Sha256Value $entry.backup_sha256)) { throw 'removed WAL entry lacks durable backup hash' }
      }
      if ($phaseIndex -ge 4) {
        if (!(Test-Path -LiteralPath ([string]$entry.replacement) -PathType Leaf)) { throw 'install WAL replacement receipt is missing' }
        $replacementValue = Get-Content -LiteralPath ([string]$entry.replacement) -Raw | ConvertFrom-Json
        $expectedReplacementSchema = if ($entry.action -eq 'removed') { 'codexpro.install-removal/v1' } else { 'codexpro.install-replacement/v1' }
        if ($replacementValue.schema -ne $expectedReplacementSchema) { throw 'install WAL replacement receipt schema mismatch' }
        foreach ($field in @('path', 'action')) { if ($replacementValue.$field -isnot [string]) { throw "install WAL replacement receipt field $field must be a string" } }
        if ($null -ne $replacementValue.backup_sha256 -and $replacementValue.backup_sha256 -isnot [string]) { throw 'install WAL replacement receipt backup_sha256 must be null or string' }
        if (($null -eq $entry.backup_sha256) -ne ($null -eq $replacementValue.backup_sha256)) { throw 'install WAL replacement receipt backup_sha256 nullability mismatch' }
        $bindingFields = if ($entry.action -eq 'removed') { @('path', 'action', 'expected_sha256', 'backup', 'backup_sha256', 'expected_absence', 'transaction_id', 'rollback_binding') } else { @('path', 'action', 'installed_sha256', 'backup_sha256') }
        foreach ($field in $bindingFields) { if ([string]$replacementValue.$field -ne [string]$entry.$field) { throw 'install WAL replacement receipt binding mismatch' } }
      }
    }
  }
  if ($Journal.schema -in @('codexpro.install-wal/v2', 'codexpro.install-wal/v3')) {
    $receiptExists = Test-Path -LiteralPath ([string]$Journal.receipt) -PathType Leaf
    if ($Journal.status -eq 'COMPLETE' -and !$receiptExists) { throw 'receipt_binding_ambiguous: completed WAL receipt is missing' }
    if ($Journal.status -eq 'COMPLETE' -and @($Journal.files | Where-Object { $_.phase -ne 'COMPLETE' }).Count) { throw 'receipt_binding_ambiguous: completed WAL contains incomplete entries' }
    if ($receiptExists) {
      if (@($Journal.files | Where-Object { $_.phase -ne 'COMPLETE' }).Count) { throw 'receipt_binding_ambiguous: receipt exists before all WAL entries are complete' }
      $receiptValue = Get-Content -LiteralPath ([string]$Journal.receipt) -Raw | ConvertFrom-Json
      Assert-ReceiptBinding $Root $Journal $receiptValue
    }
  }
}

# --- Manifest authority ---
try {
  $Manifest = Get-Content -LiteralPath (Join-Path $RepoRoot 'install-manifest.json') -Raw | ConvertFrom-Json
  if ([string]$Manifest.schema -cne 'codexpro.install-manifest/v1') { throw 'install-manifest.json schema is not codexpro.install-manifest/v1' }
  $ManifestVersion = [string]$Manifest.version
  if ([string]::IsNullOrWhiteSpace($ManifestVersion)) { throw 'install-manifest.json version is missing' }
  if ($Manifest.include -isnot [System.Array] -or !@($Manifest.include).Count) { throw 'install-manifest.json include is missing or empty' }
  foreach ($pattern in @($Manifest.include)) {
    if ($pattern -isnot [string] -or [string]::IsNullOrWhiteSpace($pattern)) { throw 'install-manifest.json include contains a non-string pattern' }
  }
  $OracleContract = $Manifest.external.oracle
  if (!$OracleContract) { throw 'install-manifest.json external.oracle is missing' }
  foreach ($field in @('package', 'tested_version', 'installation')) {
    if ($OracleContract.$field -isnot [string] -or [string]::IsNullOrWhiteSpace($OracleContract.$field)) { throw "install-manifest.json external.oracle.$field is missing" }
  }
  $ManifestOk = $true
  $ManifestFiles = @(Get-ManifestFiles $RepoRoot $Manifest)
} catch {
  $ManifestOk = $false
  $Issues += @{code = 'MANIFEST_INVALID'; detail = $_.Exception.Message}
}

# --- Receipt authority: newest receipt must be v4 bound to a COMPLETE v3 WAL ---
$Receipt = Get-ChildItem -LiteralPath $ReceiptRoot -Filter 'codexpro-automation-*.json' -File -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if (!$Receipt) {
  $Issues += @{code = 'RECEIPT_MISSING'; detail = 'No install receipt found'}
} else {
  try {
    $ReceiptItem = Get-Item -LiteralPath $Receipt.FullName -Force
    $ReceiptRelative = $Receipt.FullName.Substring($CodexRoot.Length).TrimStart([char[]]'\/')
    [void](Get-SafeChild $CodexRoot $ReceiptRelative)
    if ($ReceiptItem.LinkType) { throw 'newest install receipt is a reparse point' }
    $Value = Get-Content -LiteralPath $Receipt.FullName -Raw | ConvertFrom-Json
    $InstallReceiptValue = $Value
    if (@('codexpro.install-receipt/v2', 'codexpro.install-receipt/v3', 'codexpro.install-receipt/v4') -notcontains [string]$Value.schema) {
      throw 'unsupported install receipt schema'
    }
    if ([string]$Value.schema -ne 'codexpro.install-receipt/v4') {
      throw 'newest install receipt is not codexpro.install-receipt/v4'
    }
    if ($Value.files -isnot [System.Array]) { throw 'install receipt files must be an array' }
    $WalPath = [string]$Value.wal
    if ([string]::IsNullOrWhiteSpace($WalPath)) { throw 'install receipt wal path is missing' }
    $WalFull = [IO.Path]::GetFullPath($WalPath)
    $WalRelative = $WalFull.Substring($CodexRoot.Length).TrimStart([char[]]'\/')
    [void](Get-SafeChild $CodexRoot $WalRelative)
    if (!(Test-IsWithinRoot $BackupRoot $WalFull)) { throw 'install receipt wal is outside CODEX_HOME/backups' }
    if (!(Test-Path -LiteralPath $WalFull -PathType Leaf)) { throw 'install receipt wal is missing' }
    $WalItem = Get-Item -LiteralPath $WalFull -Force
    if ($WalItem.LinkType) { throw 'install receipt wal is a reparse point' }
    $Journal = Get-Content -LiteralPath $WalFull -Raw | ConvertFrom-Json
    if ([string]$Journal.schema -ne 'codexpro.install-wal/v3') { throw 'current receipt requires codexpro.install-wal/v3' }
    if ([string]$Journal.status -ne 'COMPLETE') { throw 'install WAL status is not COMPLETE' }
    if (!(Test-PathEqual ([string]$Journal.receipt) $Receipt.FullName)) { throw 'install WAL does not bind to the newest receipt' }
    Assert-InstallWal $CodexRoot $Journal $WalFull
    Assert-ReceiptBinding $CodexRoot $Journal $Value
    $InstallJournal = $Journal
    if ([string]::IsNullOrWhiteSpace($ManifestVersion) -or [string]::IsNullOrWhiteSpace([string]$Value.manifest_version) -or [string]$Value.manifest_version -ne $ManifestVersion) {
      $Issues += @{code = 'RECEIPT_MANIFEST_VERSION_MISMATCH'; receipt = [string]$Value.manifest_version; current = $ManifestVersion; detail = 'newest receipt was not installed from the current release manifest'}
      $ReceiptCurrent = $false
    } else {
      $ReceiptCurrent = $true
    }
    foreach ($Record in @($Journal.files)) {
      $ReceiptFiles[([string]$Record.path).Replace('\', '/')] = [string]$Record.action
    }
    $ReceiptValid = $true
  } catch {
    $Issues += @{code = 'RECEIPT_INVALID'; detail = $_.Exception.Message}
    $ReceiptCurrent = $false
    $ReceiptValid = $false
  }
}

# --- Active record authority: every active record must be a regular non-reparse
# destination matching both its bound receipt/WAL hash and the current source ---
if ($ReceiptValid) {
  $PathCoverageOk = $true
  $ActivePaths = @{}
  foreach ($Record in @($InstallJournal.files)) {
    $Relative = ([string]$Record.path).Replace('\', '/')
    $Action = [string]$Record.action
    if ($Action -eq 'removed') {
      try {
        $Path = Get-SafeChild $CodexRoot $Relative
        if (Test-Path -LiteralPath $Path) {
          $Issues += @{code = 'REMOVED_FILE_PRESENT'; path = $Relative}
          $PathCoverageOk = $false
        }
      } catch {
        $Issues += @{code = 'RECEIPT_RECORD_INVALID'; path = $Relative; detail = $_.Exception.Message}
        $PathCoverageOk = $false
      }
      continue
    }
    $ActivePaths[$Relative] = $true
    $RecordOk = $true
    try {
      $Path = Get-SafeChild $CodexRoot $Relative
      if (!(Test-Path -LiteralPath $Path -PathType Leaf)) {
        $Issues += @{code = 'FILE_MISSING'; path = $Relative}
        $RecordOk = $false
      } else {
        $Item = Get-Item -LiteralPath $Path -Force
        if ($Item.LinkType -or $Item.PSIsContainer) {
          $Issues += @{code = 'FILE_NOT_REGULAR'; path = $Relative}
          $RecordOk = $false
        }
      }
      if (!(Test-Sha256Value ([string]$Record.installed_sha256))) {
        $Issues += @{code = 'RECEIPT_RECORD_INVALID'; path = $Relative; detail = 'installed_sha256 is not a valid sha256'}
        $RecordOk = $false
      }
      $Source = Get-SafeChild $RepoRoot $Relative
      if (!(Test-Path -LiteralPath $Source -PathType Leaf)) {
        $Issues += @{code = 'SOURCE_NOT_REGULAR'; path = $Relative}
        $RecordOk = $false
      } else {
        $SourceItem = Get-Item -LiteralPath $Source -Force
        if ($SourceItem.LinkType -or $SourceItem.PSIsContainer) {
          $Issues += @{code = 'SOURCE_NOT_REGULAR'; path = $Relative}
          $RecordOk = $false
        }
      }
      if ($RecordOk -and (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $Actual = Get-Sha256 $Path
        if ($Actual -ne [string]$Record.installed_sha256) {
          $Issues += @{code = 'HASH_MISMATCH'; path = $Relative; actual = $Actual}
          $RecordOk = $false
        }
      }
      if ($RecordOk -and (Test-Path -LiteralPath $Source -PathType Leaf)) {
        $SourceHash = Get-Sha256 $Source
        if ($SourceHash -ne [string]$Record.installed_sha256) {
          $Issues += @{code = 'SOURCE_HASH_MISMATCH'; path = $Relative; source = $SourceHash; expected = [string]$Record.installed_sha256}
          $RecordOk = $false
        }
      }
      if ($RecordOk -and $Relative -eq 'bin/chatgpt_oracle_state.py') {
        $StateRecordOk = $true
      }
    } catch {
      $Issues += @{code = 'RECEIPT_RECORD_INVALID'; path = $Relative; detail = $_.Exception.Message}
      $RecordOk = $false
    }
    if (!$RecordOk) { $PathCoverageOk = $false }
  }
  if ($ManifestOk) {
    $ManifestSet = @{}
    foreach ($file in $ManifestFiles) { $ManifestSet[$file] = $true }
    $MissingFromReceipt = @($ManifestSet.Keys | Where-Object { !$ActivePaths.ContainsKey($_) })
    $ExtraInReceipt = @($ActivePaths.Keys | Where-Object { !$ManifestSet.ContainsKey($_) })
    if ($MissingFromReceipt.Count -or $ExtraInReceipt.Count) {
      $Issues += @{code = 'ACTIVE_RECORD_SET_MISMATCH'; missing = @($MissingFromReceipt); extra = @($ExtraInReceipt); detail = 'active receipt records do not exactly match the expanded current manifest'}
      $PathCoverageOk = $false
    }
  } else {
    $PathCoverageOk = $false
  }
}

$Node = Get-Command node.exe, node -ErrorAction SilentlyContinue | Select-Object -First 1
$Npx = Get-Command npx.cmd, npx -ErrorAction SilentlyContinue | Select-Object -First 1
$GitBash = Get-Item -LiteralPath 'C:\Program Files\Git\bin\bash.exe' -ErrorAction SilentlyContinue
if (!$Node -or !$Npx) {
  $Issues += @{code = 'ORACLE_DEVSPACE_NODE_TOOLING_MISSING'; detail = 'Node and npx are required for Oracle and DevSpace'}
} else {
  try {
    $NodeVersion = (& $Node.Source --version).Trim().TrimStart('v')
    $NodeMajor = [int]($NodeVersion.Split('.')[0])
    if ($NodeMajor -lt 24 -or $NodeMajor -ge 27) {
      $Issues += @{code = 'DEVSPACE_NODE_VERSION_UNSUPPORTED'; actual = $NodeVersion; required = '>=24 <27'}
    }
  } catch {
    $Issues += @{code = 'NODE_VERSION_UNREADABLE'; detail = $_.Exception.Message}
  }
}
if (!$GitBash) {
  $Issues += @{code = 'DEVSPACE_GIT_BASH_MISSING'; detail = 'Windows DevSpace requires Git Bash'}
}
if ($ManifestOk -and $OracleContract) {
  $Commands += "$($OracleContract.installation) --version"
}
$Commands += 'python .\skills\chatgpt-workspace-setup\scripts\devspace_tailscale_setup.py doctor --root C:\project --hostname your-device.your-tailnet.ts.net'

$Python = Get-Command python.exe, python -ErrorAction SilentlyContinue | Select-Object -First 1
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
$StateProbe = @'
import ast,json,sys
root=ast.parse(open(sys.argv[1],encoding='utf-8').read())
names={'ORACLE_ACTIVE_VERSION','ORACLE_PACKAGE'}
accepted={}
for node in root.body:
    if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name) and node.targets[0].id in names:
        accepted.setdefault(node.targets[0].id,[]).append(node)
rejections=[]
for node in ast.walk(root):
    if isinstance(node,ast.Assign):
        kind='ASSIGN';targets=node.targets
    elif isinstance(node,ast.AnnAssign):
        kind='ANNASSIGN';targets=[node.target] if node.target else []
    elif isinstance(node,ast.AugAssign):
        kind='AUGASSIGN';targets=[node.target]
    elif isinstance(node,ast.Delete):
        kind='DELETE';targets=node.targets
    else:
        continue
    for target in targets:
        hit=set()
        for sub in ([target] if isinstance(target,ast.Name) else ast.walk(target)):
            if isinstance(sub,ast.Name) and sub.id in names:
                hit.add(sub.id)
        for name in hit:
            if kind=='ASSIGN' and isinstance(target,ast.Name) and node in root.body and node in accepted.get(name,[]):
                continue
            if kind=='ASSIGN' and isinstance(target,ast.Name) and node in root.body:
                rejections.append('DUPLICATE:'+name)
            elif kind=='ASSIGN':
                rejections.append('NESTED:'+name)
            else:
                rejections.append(kind+':'+name)
accepted_target_ids={id(node.targets[0]) for nodes in accepted.values() for node in nodes}
for node in ast.walk(root):
    if isinstance(node,ast.Name) and node.id in names and isinstance(node.ctx,(ast.Store,ast.Del)) and id(node) not in accepted_target_ids:
        rejections.append('REBIND:'+node.id)
    bound=[]
    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
        bound=[node.name]
    elif isinstance(node,ast.alias):
        bound=[node.asname or node.name.split('.')[0]]
    elif isinstance(node,ast.ExceptHandler) and isinstance(node.name,str):
        bound=[node.name]
    elif isinstance(node,ast.arg):
        bound=[node.arg]
    elif isinstance(node,(ast.MatchAs,ast.MatchStar)) and isinstance(node.name,str):
        bound=[node.name]
    elif isinstance(node,ast.MatchMapping) and isinstance(node.rest,str):
        bound=[node.rest]
    for name in bound:
        if name in names:
            rejections.append('BINDER:'+name)
values={}
for name in names:
    nodes=accepted.get(name,[])
    if len(nodes)>1:
        rejections.append('DUPLICATE:'+name)
    elif len(nodes)==1:
        node=nodes[0]
        if not isinstance(node.value,ast.Constant):
            rejections.append('NONLITERAL:'+name)
        else:
            try:
                values[name]=ast.literal_eval(node.value)
            except Exception:
                rejections.append('NONLITERAL:'+name)
    else:
        rejections.append('MISSING:'+name)
if rejections:
    print(json.dumps({'valid':False,'rejection':rejections[0]}))
else:
    print(json.dumps({'valid':True,'values':values}))
'@
if (!$Python) {
  $Issues += @{code = 'PYTHON_MISSING'; detail = 'Python is required for the installed automation'}
} else {
  foreach ($Compat in @(
    @{module = 'bin/chatgpt_oracle_compat.py'; asset_root = 'bin/oracle-compat'},
    @{module = 'bin/chatgpt_devspace_compat.py'; asset_root = 'bin/devspace-compat'}
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
          $Issues += @{code = 'COMPAT_PATCH_ASSET_MISSING'; module = $Compat.module; path = $Relative}
        }
      }
    } catch {
      $Issues += @{code = 'COMPAT_REFERENCE_INVALID'; module = $Compat.module; detail = $_.Exception.Message}
    }
  }
  if (!$ReceiptValid) {
    # State authority cannot be bound without a fully validated current receipt.
  } elseif (!$ManifestOk) {
    # State authority cannot be compared without a valid manifest.
  } elseif (!$ReceiptFiles.ContainsKey('bin/chatgpt_oracle_state.py') -or $ReceiptFiles['bin/chatgpt_oracle_state.py'] -eq 'removed') {
    $Issues += @{code = 'ORACLE_STATE_MODULE_NOT_RECEIPTED'; path = 'bin/chatgpt_oracle_state.py'}
  } else {
    try {
      $StateModulePath = Get-SafeChild $CodexRoot 'bin/chatgpt_oracle_state.py'
      $StateProbeOutput = @(& $Python.Source -c $StateProbe $StateModulePath)
      if ($LASTEXITCODE) { throw "oracle state probe failed with exit code $LASTEXITCODE" }
      $StateProbeResult = (($StateProbeOutput -join [Environment]::NewLine) | ConvertFrom-Json)
      if ($StateProbeResult.valid -ne $true) {
        $Issues += @{code = 'ORACLE_STATE_INVALID'; path = 'bin/chatgpt_oracle_state.py'; detail = "state authority binding rejected: $([string]$StateProbeResult.rejection)"}
      } else {
        $OracleVersion = [string]$StateProbeResult.values.ORACLE_ACTIVE_VERSION
        $OraclePackage = [string]$StateProbeResult.values.ORACLE_PACKAGE
        if ([string]::IsNullOrWhiteSpace($OracleVersion)) { throw 'ORACLE_ACTIVE_VERSION is missing from the installed state module' }
        if ([string]::IsNullOrWhiteSpace($OraclePackage)) { throw 'ORACLE_PACKAGE is missing from the installed state module' }
        $StateOk = $true
        if ($OracleVersion -cne [string]$OracleContract.tested_version) {
          $StateOk = $false
          $Issues += @{code = 'ORACLE_ACTIVE_VERSION_MISMATCH'; installed = $OracleVersion; expected = [string]$OracleContract.tested_version; path = 'bin/chatgpt_oracle_state.py'; detail = 'installed Oracle active version does not exactly match the release contract tested version'}
        }
        if ($OraclePackage -cne [string]$OracleContract.package) {
          $StateOk = $false
          $Issues += @{code = 'ORACLE_PACKAGE_MISMATCH'; installed = $OraclePackage; expected = [string]$OracleContract.package; path = 'bin/chatgpt_oracle_state.py'; detail = 'installed Oracle package does not exactly match the release contract package'}
        }
        if ($StateOk) { $OracleValidated = $true }
      }
    } catch {
      $Issues += @{code = 'ORACLE_STATE_INVALID'; detail = $_.Exception.Message}
    }
  }
}

$OracleEligible = $ManifestOk -and $ReceiptValid -and $ReceiptCurrent -and $PathCoverageOk -and $StateRecordOk -and $OracleValidated

[ordered]@{
  schema = 'codexpro.doctor/v2'
  codex_home = $CodexRoot
  manifest_version = $ManifestVersion
  receipt = $(if ($Receipt) { $Receipt.FullName } else { $null })
  status = $(if ($Issues) { 'FAIL' } else { 'PASS' })
  issues = $Issues
  warnings = $Warnings
  commands = $Commands
  oracle = $(if ($OracleEligible) { @{package = "$OraclePackage@$OracleVersion"; tested_version = $OracleVersion; command = "$($OracleContract.installation)"; resolution = 'exact npx runtime pin'; evidence = $StateModulePath} } else { $null })
  devspace = $(if ($ManifestOk -and $Manifest.external.devspace) { @{package = [string]$Manifest.external.devspace.package; tested_version = [string]$Manifest.external.devspace.tested_version; setup = 'explicit setup skill only'} } else { @{package = '@waishnav/devspace'; tested_version = '1.0.7'; setup = 'explicit setup skill only'} })
  what_if = [bool]$WhatIf
} | ConvertTo-Json -Depth 7
if ($Issues) { exit 1 }
