[CmdletBinding(SupportsShouldProcess=$true)]
param(
 [string]$CodexHome=$(if($env:CODEX_HOME){$env:CODEX_HOME}else{Join-Path $env:USERPROFILE '.codex'}),
 [switch]$InstallLegacyRecoveryDependency,
 [switch]$SkipDependencyInstall
)
$ErrorActionPreference='Stop'
$ManageLegacyDependency=[bool]$InstallLegacyRecoveryDependency -and -not [bool]$SkipDependencyInstall
$RepoRoot=Split-Path -Parent $MyInvocation.MyCommand.Path
$Manifest=Get-Content (Join-Path $RepoRoot 'install-manifest.json') -Raw|ConvertFrom-Json
$HomeRoot=[IO.Path]::GetFullPath($CodexHome)
$Nonce=[guid]::NewGuid().ToString('N'); $TransactionId=$Nonce; $Stamp=[DateTime]::UtcNow.ToString('yyyyMMdd-HHmmssfff')
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
function Copy-FileDurable([string]$Source,[string]$Destination){
  $directory=Split-Path -Parent $Destination;New-Item -ItemType Directory -Force -Path $directory|Out-Null
  $temporary=Join-Path $directory ".codexpro-$([guid]::NewGuid().ToString('N')).tmp";$input=$null;$output=$null
  try{
    $input=[IO.File]::Open($Source,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $output=[IO.File]::Open($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    $input.CopyTo($output);$output.Flush($true);$output.Dispose();$output=$null;$input.Dispose();$input=$null
    if(Test-Path -LiteralPath $Destination){
      $replaceBackup=Join-Path $directory ".codexpro-$([guid]::NewGuid().ToString('N')).bak"
      try{[IO.File]::Replace($temporary,$Destination,$replaceBackup,$true)}finally{if(Test-Path -LiteralPath $replaceBackup){Remove-Item -LiteralPath $replaceBackup -Force}}
    }else{[IO.File]::Move($temporary,$Destination)}
  } finally {
    if($output){$output.Dispose()};if($input){$input.Dispose()};if(Test-Path -LiteralPath $temporary){Remove-Item -LiteralPath $temporary -Force}
  }
}
function Write-JsonDurable([string]$Path,$Value){
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path)|Out-Null;$temporary="$Path.$([guid]::NewGuid().ToString('N')).tmp"
  try{[IO.File]::WriteAllText($temporary,($Value|ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false));$stream=[IO.File]::Open($temporary,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None);try{$stream.Flush($true)}finally{$stream.Dispose()};Copy-FileDurable $temporary $Path}finally{if(Test-Path -LiteralPath $temporary){Remove-Item -LiteralPath $temporary -Force}}
}
function Test-PathEqual([string]$Left,[string]$Right){if([string]::IsNullOrWhiteSpace($Left)-or[string]::IsNullOrWhiteSpace($Right)){return $false};([IO.Path]::GetFullPath($Left)).Equals([IO.Path]::GetFullPath($Right),[StringComparison]::OrdinalIgnoreCase)}
function Test-Sha256Value($Value){$Value -is [string] -and $Value -match '^[a-f0-9]{64}$'}
function Assert-ReceiptBinding([string]$Root,$Journal,$ReceiptValue){
  if($Journal.schema -ne 'codexpro.install-wal/v2'){return}
  $receiptRoot=Join-Path $Root 'receipts'
  if(!(Test-IsWithinRoot $receiptRoot ([string]$Journal.receipt))){throw 'receipt_binding_ambiguous: receipt path is outside CODEX_HOME/receipts'}
  if($ReceiptValue.files -isnot [System.Array]){throw 'receipt_binding_ambiguous: receipt files must be an array'}
  if($ReceiptValue.schema -ne 'codexpro.install-receipt/v3' -or [string]$ReceiptValue.transaction_id -ne [string]$Journal.transaction_id){throw 'receipt_binding_ambiguous: receipt identity mismatch'}
  if(!(Test-PathEqual ([string]$ReceiptValue.wal) ([string]$Journal.wal_path)) -or !(Test-PathEqual ([string]$ReceiptValue.backup) ([string]$Journal.backup))){throw 'receipt_binding_ambiguous: receipt path binding mismatch'}
  if([string]$ReceiptValue.manifest_version -ne [string]$Journal.manifest_version){throw 'receipt_binding_ambiguous: manifest version mismatch'}
  $expected=@($Journal.files);$observed=@($ReceiptValue.files)
  if($expected.Count -ne $observed.Count){throw 'receipt_binding_ambiguous: receipt file count mismatch'}
  for($index=0;$index -lt $expected.Count;$index++){
    $left=$expected[$index];$right=$observed[$index]
    foreach($field in @('path','action','installed_sha256')){if($right.$field -isnot [string]){throw "receipt_binding_ambiguous: receipt file field $field must be a string at index $index"}}
    if($null -ne $right.backup_sha256 -and $right.backup_sha256 -isnot [string]){throw "receipt_binding_ambiguous: receipt backup_sha256 must be null or string at index $index"}
    if(($null -eq $left.backup_sha256) -ne ($null -eq $right.backup_sha256)){throw "receipt_binding_ambiguous: receipt backup_sha256 nullability mismatch at index $index"}
    if([string]$left.path -ne [string]$right.path -or [string]$left.action -ne [string]$right.action -or [string]$left.installed_sha256 -ne [string]$right.installed_sha256 -or [string]$left.backup_sha256 -ne [string]$right.backup_sha256){throw "receipt_binding_ambiguous: receipt file record mismatch at index $index"}
  }
}
function Assert-InstallWal([string]$Root,$Journal,[string]$DiscoveredWalPath){
  foreach($field in @('schema','status','backup')){if($Journal.$field -isnot [string]){throw "install WAL field $field must be a string"}}
  if($Journal.files -isnot [System.Array]){throw 'install WAL files must be an array'}
  if(@('codexpro.install-wal/v1','codexpro.install-wal/v2') -notcontains [string]$Journal.schema){throw 'unsupported install WAL schema'}
  $validStatuses=if($Journal.schema -eq 'codexpro.install-wal/v1'){@('ACTIVE','COMPLETE','ROLLED_BACK_AFTER_CRASH')}else{@('ACTIVE','COMPLETE','ROLLED_BACK_AFTER_CRASH','ROLLED_BACK_AFTER_ERROR')}
  if($validStatuses -notcontains [string]$Journal.status){throw 'invalid install WAL status'}
  if([string]::IsNullOrWhiteSpace([string]$Journal.backup)-or[IO.Path]::IsPathRooted([string]$Journal.backup)-eq $false){throw 'invalid install WAL backup'}
  if(!(Test-IsWithinRoot (Join-Path $Root 'backups') ([string]$Journal.backup))){throw 'install WAL backup is outside CODEX_HOME/backups'}
  $orders=@{
    'codexpro.install-wal/v1'=@('INTENT','MUTATED','VERIFIED','COMPLETE')
    'codexpro.install-wal/v2'=@('INTENT','BACKUP_DURABLE','MUTATED','VERIFIED','REPLACEMENT_RECEIPT_DURABLE','COMPLETE')
  }
  if($Journal.schema -eq 'codexpro.install-wal/v2'){
    foreach($field in @('transaction_id','manifest_version','receipt','wal_path')){if($Journal.$field -isnot [string]){throw "WAL v2 field $field must be a string"}}
    if([string]$Journal.transaction_id -notmatch '^[a-f0-9]{32}$'){throw 'invalid WAL v2 transaction_id'}
    if([string]::IsNullOrWhiteSpace([string]$Journal.manifest_version)){throw 'invalid WAL v2 manifest_version'}
    if(!(Test-PathEqual ([string]$Journal.wal_path) $DiscoveredWalPath)){throw 'WAL v2 serialized wal_path mismatch'}
    if(!(Test-PathEqual ([string]$Journal.backup) (Split-Path -Parent $DiscoveredWalPath))){throw 'WAL v2 backup does not own discovered WAL'}
    if(!(Test-IsWithinRoot (Join-Path $Root 'receipts') ([string]$Journal.receipt))){throw 'receipt_binding_ambiguous: WAL v2 receipt path is outside CODEX_HOME/receipts'}
  }
  $seen=@{};$entries=@($Journal.files)
  for($index=0;$index -lt $entries.Count;$index++){
    $entry=$entries[$index]
    foreach($field in @('path','action','installed_sha256','phase','replacement')){if($entry.$field -isnot [string]){throw "install WAL file field $field must be a string"}}
    if($entry.transitions -isnot [System.Array]){throw 'install WAL transitions must be an array'}
    foreach($transition in @($entry.transitions)){if($transition -isnot [string]){throw 'install WAL transition values must be strings'}}
    if($null -ne $entry.backup_sha256 -and $entry.backup_sha256 -isnot [string]){throw 'install WAL backup_sha256 must be null or string'}
    [void](Get-SafeChild $Root ([string]$entry.path))
    if($seen.ContainsKey([string]$entry.path)){throw 'duplicate install WAL destination'};$seen[[string]$entry.path]=$true
    if(@('created','overwritten') -notcontains [string]$entry.action){throw 'invalid install WAL action'}
    if(!(Test-Sha256Value $entry.installed_sha256)){throw 'invalid installed_sha256 in install WAL'}
    if([string]::IsNullOrWhiteSpace([string]$entry.replacement)-or!(Test-IsWithinRoot ([string]$Journal.backup) ([string]$entry.replacement))){throw 'invalid install WAL replacement path'}
    $order=@($orders[[string]$Journal.schema]);$phaseIndex=[Array]::IndexOf($order,[string]$entry.phase)
    if($phaseIndex -lt 0){throw 'invalid install WAL phase'}
    $transitions=@($entry.transitions);$expectedTransitions=@($order[0..$phaseIndex])
    if(($transitions -join '|') -ne ($expectedTransitions -join '|')){throw 'invalid install WAL transition order'}
    if($Journal.schema -eq 'codexpro.install-wal/v2'){
      if(($entry.sequence_number -isnot [int] -and $entry.sequence_number -isnot [long]) -or [int64]$entry.sequence_number -ne $index){throw 'invalid WAL v2 sequence_number'}
      if($entry.action -eq 'created' -and $null -ne $entry.backup_sha256){throw 'created WAL entry cannot carry backup_sha256'}
      if($entry.action -eq 'overwritten' -and $phaseIndex -eq 0 -and $null -ne $entry.backup_sha256){throw 'overwritten WAL INTENT entry cannot carry backup_sha256'}
      if($entry.action -eq 'overwritten' -and $phaseIndex -ge 1 -and !(Test-Sha256Value $entry.backup_sha256)){throw 'overwritten WAL entry lacks durable backup hash'}
      if($phaseIndex -ge 4){
        if(!(Test-Path -LiteralPath ([string]$entry.replacement) -PathType Leaf)){throw 'install WAL replacement receipt is missing'}
        $replacementValue=Get-Content -LiteralPath ([string]$entry.replacement) -Raw|ConvertFrom-Json
        if($replacementValue.schema -ne 'codexpro.install-replacement/v1'){throw 'install WAL replacement receipt schema mismatch'}
        foreach($field in @('path','action','installed_sha256')){if($replacementValue.$field -isnot [string]){throw "install WAL replacement receipt field $field must be a string"}}
        if($null -ne $replacementValue.backup_sha256 -and $replacementValue.backup_sha256 -isnot [string]){throw 'install WAL replacement receipt backup_sha256 must be null or string'}
        if(($null -eq $entry.backup_sha256) -ne ($null -eq $replacementValue.backup_sha256)){throw 'install WAL replacement receipt backup_sha256 nullability mismatch'}
        if([string]$replacementValue.path -ne [string]$entry.path -or [string]$replacementValue.action -ne [string]$entry.action -or [string]$replacementValue.installed_sha256 -ne [string]$entry.installed_sha256 -or [string]$replacementValue.backup_sha256 -ne [string]$entry.backup_sha256){throw 'install WAL replacement receipt binding mismatch'}
      }
    }
  }
  if($Journal.schema -eq 'codexpro.install-wal/v2'){
    $receiptExists=Test-Path -LiteralPath ([string]$Journal.receipt) -PathType Leaf
    if($Journal.status -eq 'COMPLETE' -and !$receiptExists){throw 'receipt_binding_ambiguous: completed WAL receipt is missing'}
    if($Journal.status -eq 'COMPLETE' -and @($Journal.files|Where-Object{$_.phase -ne 'COMPLETE'}).Count){throw 'receipt_binding_ambiguous: completed WAL contains incomplete entries'}
    if($receiptExists){
      if(@($Journal.files|Where-Object{$_.phase -ne 'COMPLETE'}).Count){throw 'receipt_binding_ambiguous: receipt exists before all WAL entries are complete'}
      $receiptValue=Get-Content -LiteralPath ([string]$Journal.receipt) -Raw|ConvertFrom-Json
      Assert-ReceiptBinding $Root $Journal $receiptValue
    }
  }
}
function Write-WalDurable([string]$Root,[string]$Path,$Journal){
  Write-JsonDurable $Path $Journal
  $readback=Get-Content -LiteralPath $Path -Raw|ConvertFrom-Json
  Assert-InstallWal $Root $readback $Path
}
function Test-IsWithinRoot([string]$Root,[string]$Path){$r=[IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar,[IO.Path]::AltDirectorySeparatorChar);$p=[IO.Path]::GetFullPath($Path);$p.StartsWith($r+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)}
function Get-SafeChild([string]$Root,[string]$Relative){if([string]::IsNullOrWhiteSpace($Relative)-or[IO.Path]::IsPathRooted($Relative)-or$Relative -match '(^|[\\/])\.{1,2}([\\/]|$)'){throw "unsafe relative path: $Relative"};$p=[IO.Path]::GetFullPath((Join-Path $Root $Relative));if(!(Test-IsWithinRoot $Root $p)){throw "path escapes root: $Relative"};$cursor=Split-Path -Parent $p;while((Test-IsWithinRoot $Root $cursor) -and $cursor -ne [IO.Path]::GetFullPath($Root)){if(Test-Path -LiteralPath $cursor){$i=Get-Item -LiteralPath $cursor -Force;if($i.LinkType){throw "symlink/reparse path refused: $cursor"}};$cursor=Split-Path -Parent $cursor};$p}
function Get-ManifestFiles([string]$Root,$Value){$files=@();foreach($pattern in $Value.include){if($pattern -match '(^|/)\.{1,2}($|/)' -or [IO.Path]::IsPathRooted($pattern)){throw "unsafe manifest pattern: $pattern"};$base=if($pattern.StartsWith('bin/')){Join-Path $Root 'bin'}elseif($pattern.StartsWith('skills/')){Join-Path $Root 'skills'}elseif($pattern.StartsWith('mcp_servers/')){Join-Path $Root 'mcp_servers'}elseif($pattern.StartsWith('scripts/')){Join-Path $Root 'scripts'}elseif($pattern.StartsWith('contracts/')){Join-Path $Root 'contracts'}elseif($pattern.StartsWith('tests/fixtures/')){Join-Path $Root 'tests/fixtures'}else{throw "unsupported manifest root: $pattern"};$patternMatches=@();foreach($item in @(Get-ChildItem -LiteralPath $base -File -Recurse -Force)){if($item.LinkType){throw "manifest refuses symlink: $($item.FullName)"};$relative=$item.FullName.Substring($Root.Length).TrimStart([char[]]'\/').Replace('\','/');if($relative -like $pattern){[void](Get-SafeChild $Root $relative);$patternMatches+=$relative}};if(!$patternMatches.Count){throw "manifest pattern matched no files: $pattern"};$files+=$patternMatches};@($files|Sort-Object -Unique)}
function Invoke-InstallFault([string]$Point){if($env:CODEXPRO_INSTALL_FAULT_POINT -eq $Point){throw "INSTALL_FAULT_INJECTED: $Point"}}
function Invoke-WalRollback([string]$Root,$Journal,[string]$FinalStatus){
  $conflicts=@();$entries=@($Journal.files);$receiptToRemove=$null
  try{Assert-InstallWal $Root $Journal ([string]$Journal.wal_path)}catch{return @(@{path='install-wal';action='receipt_binding_ambiguous';detail=$_.Exception.Message})}
  if($Journal.receipt -and (Test-IsWithinRoot (Join-Path $Root 'receipts') ([string]$Journal.receipt)) -and (Test-Path -LiteralPath ([string]$Journal.receipt))){
    if($Journal.schema -eq 'codexpro.install-wal/v2'){
      try{
        $boundReceipt=Get-Content -LiteralPath ([string]$Journal.receipt) -Raw|ConvertFrom-Json
        Assert-ReceiptBinding $Root $Journal $boundReceipt
        $receiptToRemove=[string]$Journal.receipt
      }catch{return @(@{path='install-receipt';action='receipt_binding_ambiguous';detail=$_.Exception.Message})}
    }else{$receiptToRemove=[string]$Journal.receipt}
  }
  # Decide the complete rollback before the first mutation. A tampered backup or
  # user-modified destination in any entry must not allow earlier entries to be
  # partially restored.
  $plans=@()
  for($index=$entries.Count-1;$index -ge 0;$index--){
    $entry=$entries[$index];$destination=Get-SafeChild $Root ([string]$entry.path)
    try{
      $destinationExists=Test-Path -LiteralPath $destination;$destinationHash=if($destinationExists){Get-Hash $destination}else{$null}
      if($entry.action -eq 'created'){
        if(!$destinationExists){continue}
        if($destinationHash -eq [string]$entry.installed_sha256){$plans+=@{operation='remove';destination=$destination;entry=$entry};continue}
        $conflicts+=@{path=$entry.path;action='preserved_modified_created';phase=$entry.phase};continue
      }
      if($entry.phase -eq 'INTENT' -and [string]::IsNullOrWhiteSpace([string]$entry.backup_sha256)){
        if($destinationExists){continue}
        $conflicts+=@{path=$entry.path;action='missing_before_backup';phase=$entry.phase};continue
      }
      $backup=Get-SafeChild ([string]$Journal.backup) ([string]$entry.path)
      if(!(Test-Path -LiteralPath $backup) -or (Get-Hash $backup)-ne [string]$entry.backup_sha256){$conflicts+=@{path=$entry.path;action='missing_interrupted_backup';phase=$entry.phase};continue}
      if(!$destinationExists){
        if($Journal.schema -eq 'codexpro.install-wal/v1'){continue}
        $conflicts+=@{path=$entry.path;action='missing_overwritten_destination';phase=$entry.phase};continue
      }
      if($destinationHash -eq [string]$entry.backup_sha256){continue}
      if($destinationHash -eq [string]$entry.installed_sha256){$plans+=@{operation='restore';destination=$destination;backup=$backup;entry=$entry};continue}
      $conflicts+=@{path=$entry.path;action='preserved_modified_overwritten';phase=$entry.phase}
    }catch{$conflicts+=@{path=$entry.path;action='rollback_error';phase=$entry.phase;detail=$_.Exception.Message}}
  }
  if($conflicts.Count){return @($conflicts)}
  foreach($plan in $plans){
    try{
      if($plan.operation -eq 'remove'){Remove-Item -LiteralPath ([string]$plan.destination) -Force}
      else{Copy-FileDurable ([string]$plan.backup) ([string]$plan.destination)}
    }catch{$conflicts+=@{path=$plan.entry.path;action='rollback_error';phase=$plan.entry.phase;detail=$_.Exception.Message};break}
  }
  if(!$conflicts.Count){
    if($receiptToRemove){Remove-Item -LiteralPath $receiptToRemove -Force}
    $Journal.status=$FinalStatus;$Journal|Add-Member -NotePropertyName rolled_back_at -NotePropertyValue ([DateTime]::UtcNow.ToString('o')) -Force;Write-WalDurable $Root ([string]$Journal.wal_path) $Journal
  }
  @($conflicts)
}
function Resume-PendingInstallTransactions([string]$Root){
  $backupBase=Join-Path $Root 'backups';if(!(Test-Path -LiteralPath $backupBase)){return}
  foreach($journalPath in @(Get-ChildItem -LiteralPath $backupBase -Filter 'install.wal.json' -File -Recurse -Force -ErrorAction SilentlyContinue|Sort-Object FullName)){
    $journal=Get-Content -LiteralPath $journalPath.FullName -Raw|ConvertFrom-Json
    if($journal.schema -eq 'codexpro.install-wal/v1'){$journal|Add-Member -NotePropertyName wal_path -NotePropertyValue $journalPath.FullName -Force}
    Assert-InstallWal $Root $journal $journalPath.FullName
    if($journal.status -eq 'COMPLETE' -or [string]$journal.status -like 'ROLLED_BACK*'){continue}
    $conflicts=@(Invoke-WalRollback $Root $journal 'ROLLED_BACK_AFTER_CRASH')
    if($conflicts.Count){throw ("INSTALL_CRASH_RECOVERY_CONFLICT: "+($conflicts|ConvertTo-Json -Compress))}
  }
}
$Files=@(Get-ManifestFiles $RepoRoot $Manifest)
if($WhatIfPreference){$Files|ForEach-Object{"Would stage and install $_"};if($ManageLegacyDependency){"Would explicitly install and contract-validate recovery-only agbrowse@$($Manifest.external.agbrowse.version)"}else{'Would leave frozen agbrowse/CodexPro legacy dependencies untouched'};exit 0}
$records=@();$receipt=$null;$dependency=$null;$dependencyApplied=$false;$dependencySourceReceipt=$null;$journal=$null;$journalPath=$null
$dependencyPreflightToken=$null
Resume-PendingInstallTransactions $HomeRoot
if($ManageLegacyDependency){
 $preflightOutput=@(& (Join-Path $RepoRoot 'update.ps1') -Preflight -AgbrowseVersion ([string]$Manifest.external.agbrowse.version) -CodexHome $HomeRoot)
 if($LASTEXITCODE){throw "agbrowse dependency preflight failed with exit code ${LASTEXITCODE}: $($preflightOutput -join ' ')"}
 try{$preflight=($preflightOutput -join [Environment]::NewLine)|ConvertFrom-Json}catch{throw 'agbrowse dependency preflight produced invalid output'}
 if($preflight.schema -ne 'codexpro.agbrowse-update-preflight/v1' -or $preflight.status -ne 'READY' -or !$preflight.token){throw 'agbrowse dependency preflight did not provide a ready identity token'}
 $dependencyPreflightToken=[string]$preflight.token
}
try{
 foreach($relative in $Files){$source=Get-SafeChild $RepoRoot $relative;$stage=Get-SafeChild $StageRoot $relative;New-Item -ItemType Directory -Force -Path (Split-Path -Parent $stage)|Out-Null;Copy-FileDurable $source $stage;if((Get-Hash $source)-ne(Get-Hash $stage)){throw "staging hash verification failed: $relative"}}
 New-Item -ItemType Directory -Force -Path $ReceiptRoot|Out-Null;$receipt=Get-SafeChild $ReceiptRoot "codexpro-automation-$Stamp-$Nonce.json"
 $journalPath=Join-Path $BackupRoot 'install.wal.json';$journal=[ordered]@{schema='codexpro.install-wal/v2';transaction_id=$TransactionId;manifest_version=$Manifest.version;status='ACTIVE';backup=$BackupRoot;receipt=$receipt;wal_path=$journalPath;created_at=[DateTime]::UtcNow.ToString('o');files=@()};Write-WalDurable $HomeRoot $journalPath $journal;$stepIndex=0
 foreach($relative in $Files){
  $destination=Get-SafeChild $HomeRoot $relative;$stage=Get-SafeChild $StageRoot $relative;$action='created';$backup=$null;$backupHash=$null
  if(Test-Path -LiteralPath $destination){$i=Get-Item -LiteralPath $destination -Force;if($i.LinkType){throw "destination symlink refused: $relative"};$action='overwritten';$backup=Get-SafeChild $BackupRoot $relative}
  $replacementPath=Join-Path $BackupRoot "steps/$stepIndex/replacement.json";$record=[ordered]@{sequence_number=$stepIndex;path=$relative;action=$action;installed_sha256=(Get-Hash $stage);backup_sha256=$null;phase='INTENT';transitions=@('INTENT');replacement=$replacementPath};$journal.files+=@($record);Write-WalDurable $HomeRoot $journalPath $journal;Invoke-InstallFault 'AFTER_INTENT'
  if($action -eq 'overwritten'){New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backup)|Out-Null;Copy-FileDurable $destination $backup;$backupHash=Get-Hash $backup;$record.backup_sha256=$backupHash};$record.phase='BACKUP_DURABLE';$record.transitions+=@('BACKUP_DURABLE');Write-WalDurable $HomeRoot $journalPath $journal;Invoke-InstallFault 'AFTER_BACKUP_DURABLE'
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination)|Out-Null;Copy-FileDurable $stage $destination;$record.phase='MUTATED';$record.transitions+=@('MUTATED');Write-WalDurable $HomeRoot $journalPath $journal;Invoke-InstallFault 'AFTER_MUTATION'
  if($record.installed_sha256 -ne (Get-Hash $destination)){throw "commit hash verification failed: $relative"};$record.phase='VERIFIED';$record.transitions+=@('VERIFIED');Write-WalDurable $HomeRoot $journalPath $journal;Invoke-InstallFault 'AFTER_VERIFICATION'
  Write-JsonDurable $replacementPath ([ordered]@{schema='codexpro.install-replacement/v1';path=$relative;action=$action;installed_sha256=$record.installed_sha256;backup_sha256=$backupHash;mutated_at=[DateTime]::UtcNow.ToString('o')});$record.phase='REPLACEMENT_RECEIPT_DURABLE';$record.transitions+=@('REPLACEMENT_RECEIPT_DURABLE');Write-WalDurable $HomeRoot $journalPath $journal;Invoke-InstallFault 'AFTER_REPLACEMENT_RECEIPT'
  $record.phase='COMPLETE';$record.transitions+=@('COMPLETE');Write-WalDurable $HomeRoot $journalPath $journal;$receiptRecord=[ordered]@{path=$relative;action=$action;installed_sha256=$record.installed_sha256;backup_sha256=$backupHash};$records+=$receiptRecord;$stepIndex++
 }
 if($ManageLegacyDependency){& (Join-Path $RepoRoot 'update.ps1') -AgbrowseVersion ([string]$Manifest.external.agbrowse.version) -CodexHome $HomeRoot -PreflightToken $dependencyPreflightToken;if($LASTEXITCODE){throw "agbrowse dependency install failed with exit code $LASTEXITCODE"};$dependencyApplied=$true;$dependencySourceReceipt=Join-Path $HomeRoot 'agbrowse-update-receipt.json';if(!(Test-Path -LiteralPath $dependencySourceReceipt)){throw 'agbrowse dependency install produced no update receipt'};$dependencyReceipt=Get-SafeChild $BackupRoot 'dependency-update-receipt.json';New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dependencyReceipt)|Out-Null;Copy-Item -LiteralPath $dependencySourceReceipt -Destination $dependencyReceipt -Force;$dependency=[ordered]@{mode='applied';role='persisted-run-recovery-only';receipt=$dependencyReceipt;receipt_sha256=(Get-Hash $dependencyReceipt)}}else{$dependency=[ordered]@{mode='skipped';reason='legacy-recovery-dependencies-frozen'}}
 Write-JsonDurable $receipt ([ordered]@{schema='codexpro.install-receipt/v3';transaction_id=$TransactionId;installed_at=[DateTime]::UtcNow.ToString('o');manifest_version=$Manifest.version;backup=$BackupRoot;files=$records;dependency=$dependency;dependency_note='CodexPro and agbrowse are frozen for new work; dependency changes require explicit legacy-recovery opt-in.';wal=$journalPath})
 $receiptReadback=Get-Content -LiteralPath $receipt -Raw|ConvertFrom-Json
 Assert-ReceiptBinding $HomeRoot $journal $receiptReadback
 Invoke-InstallFault 'AFTER_INSTALL_RECEIPT'
 $journal.status='COMPLETE';$journal.completed_at=[DateTime]::UtcNow.ToString('o');Write-WalDurable $HomeRoot $journalPath $journal
 "Installed $($Files.Count) files. Receipt: $receipt"
} catch {
  $installError=$_.Exception;$conflicts=@()
  if($journal -and $journalPath){try{$journal.wal_path=$journalPath;$conflicts=@(Invoke-WalRollback $HomeRoot $journal 'ROLLED_BACK_AFTER_ERROR')}catch{$conflicts+=@{path='install-wal';action='rollback_error';detail=$_.Exception.Message}}}
  $dependencyRollbackReceipt=$null;if($dependency -and $dependency.mode -eq 'applied' -and (Test-Path -LiteralPath $dependency.receipt)){$dependencyRollbackReceipt=$dependency.receipt}elseif($dependencyApplied -and $dependencySourceReceipt -and (Test-Path -LiteralPath $dependencySourceReceipt)){$dependencyRollbackReceipt=$dependencySourceReceipt};if(!$conflicts.Count -and $dependencyRollbackReceipt){& (Join-Path $RepoRoot 'update.ps1') -RollbackReceipt $dependencyRollbackReceipt -CodexHome $HomeRoot;if($LASTEXITCODE){$conflicts+=@{path='dependency';action='dependency_rollback_incomplete'}}}
  if($conflicts.Count){throw ("INSTALL_FAILED: $($installError.Message); INSTALL_ROLLBACK_CONFLICT: "+([ordered]@{code='INSTALL_ROLLBACK_CONFLICT';conflicts=$conflicts}|ConvertTo-Json -Compress))}
  elseif($receipt -and (Test-Path -LiteralPath $receipt)){Remove-Item -LiteralPath $receipt -Force}
  throw $installError
} finally {
  if(Test-Path -LiteralPath $StageRoot){Remove-Item -LiteralPath $StageRoot -Recurse -Force}
}
