[CmdletBinding(SupportsShouldProcess=$true)]
param([string]$CodexHome=$(if($env:CODEX_HOME){$env:CODEX_HOME}else{Join-Path $env:USERPROFILE '.codex'}),[string]$Receipt)
# Safe inverse of install: only unchanged created files are removed; unchanged overwritten files are restored.
if(!$Receipt){$dir=Join-Path $CodexHome 'receipts';$Receipt=(Get-ChildItem -LiteralPath $dir -Filter 'codexpro-automation-*.json' -File -ErrorAction SilentlyContinue|Sort-Object LastWriteTimeUtc -Descending|Select-Object -First 1).FullName}
if(!$Receipt){throw 'no install receipt; refusing to remove unowned files'}
& (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'rollback.ps1') -Receipt $Receipt -CodexHome $CodexHome -WhatIf:$WhatIfPreference
exit $LASTEXITCODE
