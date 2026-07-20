[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$CodexHome=$(if($env:CODEX_HOME){$env:CODEX_HOME}else{Join-Path $env:USERPROFILE '.codex'}),[switch]$SkipDependencyInstall)
$ErrorActionPreference='Stop'
$RepoRoot=Split-Path -Parent $MyInvocation.MyCommand.Path
$Manifest=Get-Content (Join-Path $RepoRoot 'install-manifest.json') -Raw|ConvertFrom-Json
$HomeRoot=[IO.Path]::GetFullPath($CodexHome)
$Nonce=[guid]::NewGuid().ToString('N'); $Stamp=[DateTime]::UtcNow.ToString('yyyyMMdd-HHmmssfff')
$BackupRoot=Join-Path $HomeRoot "backups/codexpro-automation-$Stamp-$Nonce"; $ReceiptRoot=Join-Path $HomeRoot 'receipts'
$StageRoot=Join-Path ([IO.Path]::GetTempPath()) "codexpro-stage-$Nonce"
function Get-Hash([string]$Path){
  $stream=$null;$sha256=$null
  try{
    $stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $sha256=[Security.Cryptography.SHA256]::Create()
    (([BitConverter]::ToString($sha256.ComputeHash($stream))) -replace '-','').ToLowerInvariant()
  } finally {
    if($sha256){$sha256.Dispose()}
    if($stream){$stream.Dispose()}
  }
}
function Test-IsWithinRoot([string]$Root,[string]$Path){$r=[IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar,[IO.Path]::AltDirectorySeparatorChar);$p=[IO.Path]::GetFullPath($Path);$p.StartsWith($r+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)}
function Get-SafeChild([string]$Root,[string]$Relative){if([string]::IsNullOrWhiteSpace($Relative)-or[IO.Path]::IsPathRooted($Relative)-or$Relative -match '(^|[\\/])\.{1,2}([\\/]|$)'){throw "unsafe relative path: $Relative"};$p=[IO.Path]::GetFullPath((Join-Path $Root $Relative));if(!(Test-IsWithinRoot $Root $p)){throw "path escapes root: $Relative"};$cursor=Split-Path -Parent $p;while((Test-IsWithinRoot $Root $cursor) -and $cursor -ne [IO.Path]::GetFullPath($Root)){if(Test-Path -LiteralPath $cursor){$i=Get-Item -LiteralPath $cursor -Force;if($i.LinkType){throw "symlink/reparse path refused: $cursor"}};$cursor=Split-Path -Parent $cursor};$p}
function Get-ManifestFiles([string]$Root,$Value){$files=@();foreach($pattern in $Value.include){if($pattern -match '(^|/)\.{1,2}($|/)' -or [IO.Path]::IsPathRooted($pattern)){throw "unsafe manifest pattern: $pattern"};$base=if($pattern.StartsWith('bin/')){Join-Path $Root 'bin'}else{Join-Path $Root 'skills'};$patternMatches=@();foreach($item in @(Get-ChildItem -LiteralPath $base -File -Recurse -Force)){if($item.LinkType){throw "manifest refuses symlink: $($item.FullName)"};$relative=$item.FullName.Substring($Root.Length).TrimStart([char[]]'\/').Replace('\','/');if($relative -like $pattern){[void](Get-SafeChild $Root $relative);$patternMatches+=$relative}};if(!$patternMatches.Count){throw "manifest pattern matched no files: $pattern"};$files+=$patternMatches};@($files|Sort-Object -Unique)}
$Files=@(Get-ManifestFiles $RepoRoot $Manifest)
if($WhatIfPreference){$Files|ForEach-Object{"Would stage and install $_"};if(!$SkipDependencyInstall){"Would explicitly install and contract-validate agbrowse@$($Manifest.external.agbrowse.version)"};'CodexPro dependency remains external: bootstrap scripts acquire the latest supported app runtime.';exit 0}
$records=@();$installed=@();$receipt=$null;$dependency=$null;$dependencyApplied=$false;$dependencySourceReceipt=$null
$dependencyPreflightToken=$null
if(!$SkipDependencyInstall){
 $preflightOutput=@(& (Join-Path $RepoRoot 'update.ps1') -Preflight -AgbrowseVersion ([string]$Manifest.external.agbrowse.version) -CodexHome $HomeRoot)
 if($LASTEXITCODE){throw "agbrowse dependency preflight failed with exit code ${LASTEXITCODE}: $($preflightOutput -join ' ')"}
 try{$preflight=($preflightOutput -join [Environment]::NewLine)|ConvertFrom-Json}catch{throw 'agbrowse dependency preflight produced invalid output'}
 if($preflight.schema -ne 'codexpro.agbrowse-update-preflight/v1' -or $preflight.status -ne 'READY' -or !$preflight.token){throw 'agbrowse dependency preflight did not provide a ready identity token'}
 $dependencyPreflightToken=[string]$preflight.token
}
try{
 foreach($relative in $Files){$source=Get-SafeChild $RepoRoot $relative;$stage=Get-SafeChild $StageRoot $relative;New-Item -ItemType Directory -Force -Path (Split-Path -Parent $stage)|Out-Null;Copy-Item -LiteralPath $source -Destination $stage -Force;if((Get-Hash $source)-ne(Get-Hash $stage)){throw "staging hash verification failed: $relative"}}
 foreach($relative in $Files){$destination=Get-SafeChild $HomeRoot $relative;$stage=Get-SafeChild $StageRoot $relative;$action='created';$backup=$null;$backupHash=$null;if(Test-Path -LiteralPath $destination){$i=Get-Item -LiteralPath $destination -Force;if($i.LinkType){throw "destination symlink refused: $relative"};$action='overwritten';$backup=Get-SafeChild $BackupRoot $relative;New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backup)|Out-Null;Copy-Item -LiteralPath $destination -Destination $backup -Force;$backupHash=Get-Hash $backup};New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination)|Out-Null;Copy-Item -LiteralPath $stage -Destination $destination -Force;$hash=Get-Hash $stage;if($hash -ne (Get-Hash $destination)){throw "commit hash verification failed: $relative"};$record=[ordered]@{path=$relative;action=$action;installed_sha256=$hash;backup_sha256=$backupHash};$records+=$record;$installed+=$record}
 if(!$SkipDependencyInstall){& (Join-Path $RepoRoot 'update.ps1') -AgbrowseVersion ([string]$Manifest.external.agbrowse.version) -CodexHome $HomeRoot -PreflightToken $dependencyPreflightToken;if($LASTEXITCODE){throw "agbrowse dependency install failed with exit code $LASTEXITCODE"};$dependencyApplied=$true;$dependencySourceReceipt=Join-Path $HomeRoot 'agbrowse-update-receipt.json';if(!(Test-Path -LiteralPath $dependencySourceReceipt)){throw 'agbrowse dependency install produced no update receipt'};$dependencyReceipt=Get-SafeChild $BackupRoot 'dependency-update-receipt.json';New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dependencyReceipt)|Out-Null;Copy-Item -LiteralPath $dependencySourceReceipt -Destination $dependencyReceipt -Force;$dependency=[ordered]@{mode='applied';receipt=$dependencyReceipt;receipt_sha256=(Get-Hash $dependencyReceipt)}}else{$dependency=[ordered]@{mode='skipped'}}
 New-Item -ItemType Directory -Force -Path $ReceiptRoot|Out-Null;$receipt=Get-SafeChild $ReceiptRoot "codexpro-automation-$Stamp-$Nonce.json";[ordered]@{schema='codexpro.install-receipt/v3';installed_at=[DateTime]::UtcNow.ToString('o');manifest_version=$Manifest.version;backup=$BackupRoot;files=$records;dependency=$dependency;dependency_note='CodexPro is external; normal install records an exact dependency inverse receipt.'}|ConvertTo-Json -Depth 8|Set-Content -LiteralPath $receipt -Encoding utf8
 "Installed $($Files.Count) files. Receipt: $receipt"
} catch {
  $conflicts=@()
  foreach($record in @($installed|Sort-Object -Descending path)) {
    try {
      $destination=Get-SafeChild $HomeRoot $record.path
      if($record.action -eq 'created') {
        if((Test-Path -LiteralPath $destination) -and (Get-Hash $destination)-eq $record.installed_sha256) { Remove-Item -LiteralPath $destination -Force }
        else { $conflicts+=@{path=$record.path;action='preserved_modified_created'} }
      } else {
        $backup=Get-SafeChild $BackupRoot $record.path
        if((Test-Path -LiteralPath $destination) -and (Get-Hash $destination)-eq $record.installed_sha256 -and (Test-Path -LiteralPath $backup) -and (Get-Hash $backup)-eq $record.backup_sha256) { Copy-Item -LiteralPath $backup -Destination $destination -Force }
        else { $conflicts+=@{path=$record.path;action='preserved_modified_overwritten'} }
      }
    } catch { $conflicts+=@{path=$record.path;action='rollback_error';detail=$_.Exception.Message} }
  }
  $dependencyRollbackReceipt=$null;if($dependency -and $dependency.mode -eq 'applied' -and (Test-Path -LiteralPath $dependency.receipt)){$dependencyRollbackReceipt=$dependency.receipt}elseif($dependencyApplied -and $dependencySourceReceipt -and (Test-Path -LiteralPath $dependencySourceReceipt)){$dependencyRollbackReceipt=$dependencySourceReceipt};if(!$conflicts.Count -and $dependencyRollbackReceipt){& (Join-Path $RepoRoot 'update.ps1') -RollbackReceipt $dependencyRollbackReceipt -CodexHome $HomeRoot;if($LASTEXITCODE){$conflicts+=@{path='dependency';action='dependency_rollback_incomplete'}}}
  if($conflicts.Count){[ordered]@{code='INSTALL_ROLLBACK_CONFLICT';conflicts=$conflicts}|ConvertTo-Json -Compress|Write-Error}
  elseif($receipt -and (Test-Path -LiteralPath $receipt)){Remove-Item -LiteralPath $receipt -Force}
  throw
} finally {
  if(Test-Path -LiteralPath $StageRoot){Remove-Item -LiteralPath $StageRoot -Recurse -Force}
}
