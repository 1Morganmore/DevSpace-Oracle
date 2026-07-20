param(
    [Parameter(Mandatory = $true)]
    [string]$UnitRoot,
    [Parameter(Mandatory = $true)]
    [string]$TopologyReceipt,
    [int]$Port = 0,
    [string]$NpxPath = "",
    [string]$StateRoot = "",
    [int]$WaitSeconds = 90,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ($TopologyReceipt -notmatch "^[0-9a-f]{64}$") {
    throw "EXACT_UNIT_TOPOLOGY_RECEIPT_INVALID"
}
if (-not (Test-Path -LiteralPath $UnitRoot -PathType Container)) {
    throw "EXACT_UNIT_ROOT_MISSING"
}
$ResolvedRoot = (Resolve-Path -LiteralPath $UnitRoot).Path
$DriveRoot = [System.IO.Path]::GetPathRoot($ResolvedRoot).TrimEnd([char[]]"\/")
$HomeRoot = [System.IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd([char[]]"\/")
$NormalizedRoot = [System.IO.Path]::GetFullPath($ResolvedRoot).TrimEnd([char[]]"\/")
if ($NormalizedRoot.Equals($DriveRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "EXACT_UNIT_ROOT_EQUALS_DRIVE_ROOT"
}
if ($NormalizedRoot.Equals($HomeRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "EXACT_UNIT_ROOT_EQUALS_USER_HOME"
}

$CodexHome = Join-Path $env:USERPROFILE ".codex"
$Manager = Join-Path $CodexHome "bin\codexpro_project_app_manager.py"
$Identity = Join-Path $CodexHome "bin\codexpro_mcp_identity.py"
$ProcessIdentity = Join-Path $CodexHome "bin\codexpro_windows_process_identity.py"
foreach ($required in @($Manager, $Identity, $ProcessIdentity)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "EXACT_UNIT_HELPER_MISSING: $required"
    }
}

$Npx = $NpxPath
if (-not $Npx) {
    $npxCommand = Get-Command "npx.cmd", "npx" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($npxCommand) { $Npx = if ($npxCommand.Source) { $npxCommand.Source } else { $npxCommand.Path } }
}
if (-not $Npx -or -not (Test-Path -LiteralPath $Npx -PathType Leaf)) {
    throw "EXACT_UNIT_NPX_MISSING"
}
if (-not $StateRoot) {
    $StateRoot = Join-Path $CodexHome "state\codexpro-project-apps\exact-units"
}
$UnitKey = ([System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::HashData([System.Text.Encoding]::UTF8.GetBytes($NormalizedRoot + "`0" + $TopologyReceipt)))).Replace("-", "").Substring(0, 24).ToLowerInvariant()
$RunDir = Join-Path $StateRoot $UnitKey
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$OutLog = Join-Path $RunDir "runtime.out.log"
$ErrLog = Join-Path $RunDir "runtime.err.log"
$ReceiptPath = Join-Path $RunDir "authority-receipt.json"

function Invoke-JsonProcess {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [int]$TimeoutMilliseconds = 30000
    )
    $stdout = Join-Path $RunDir ([guid]::NewGuid().ToString("N") + ".out.tmp")
    $stderr = Join-Path $RunDir ([guid]::NewGuid().ToString("N") + ".err.tmp")
    try {
        $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F | Out-Null
            throw "EXACT_UNIT_HELPER_TIMEOUT"
        }
        $process.WaitForExit()
        $raw = if (Test-Path -LiteralPath $stdout) { Get-Content -LiteralPath $stdout -Raw } else { "" }
        $errorText = if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Raw } else { "" }
        if ($process.ExitCode -ne 0) { throw "EXACT_UNIT_HELPER_FAILED: $errorText $raw" }
        return $raw | ConvertFrom-Json
    } finally {
        Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
    }
}

$managerArgs = @(
    $Manager,
    "--root", $ResolvedRoot,
    "--no-git-root",
    "--scope-mode", "parallel-exact-unit",
    "--topology-receipt-sha256", $TopologyReceipt
)
if ($Port -gt 0) { $managerArgs += @("--port", "$Port") }
$Decision = Invoke-JsonProcess -FilePath "python" -Arguments $managerArgs
$ResolvedPort = [int]$Decision.port

$LaunchArgs = @(
    "codexpro@latest", "start",
    "--root", $ResolvedRoot,
    "--no-profile",
    "--bash", "off",
    "--write", "workspace",
    "--tool-mode", "full",
    "--port", "$ResolvedPort",
    "--tunnel", "cloudflare"
)
if ($DryRun) {
    [ordered]@{
        status = "dry-run"
        scope_mode = "parallel-exact-unit"
        unit_root = $ResolvedRoot
        port = $ResolvedPort
        topology_receipt_sha256 = $TopologyReceipt
        argv = @($Npx) + $LaunchArgs
        allow_home_present = $false
    } | ConvertTo-Json -Depth 8
    exit 0
}

$env:CODEXPRO_SCOPE_MODE = "parallel-exact-unit"
$env:CODEXPRO_EXACT_UNIT_TOPOLOGY_RECEIPT_SHA256 = $TopologyReceipt
$Launcher = Start-Process -FilePath $Npx -ArgumentList $LaunchArgs -WorkingDirectory $ResolvedRoot -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -PassThru
$LauncherCreatedAt = $Launcher.StartTime.ToUniversalTime().ToString("o")
$Deadline = (Get-Date).AddSeconds($WaitSeconds)
$PublicUrl = ""
while ((Get-Date) -lt $Deadline) {
    Start-Sleep -Milliseconds 500
    $text = ""
    if (Test-Path -LiteralPath $OutLog) { $text += Get-Content -LiteralPath $OutLog -Raw -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $ErrLog) { $text += "`n" + (Get-Content -LiteralPath $ErrLog -Raw -ErrorAction SilentlyContinue) }
    $match = [regex]::Match($text, "https://[a-z0-9.-]+\.trycloudflare\.com/mcp\?[^\s`"']+", [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if ($match.Success) { $PublicUrl = $match.Value; break }
    if ($Launcher.HasExited) { break }
}
if (-not $PublicUrl) {
    if (-not $Launcher.HasExited) { & "$env:SystemRoot\System32\taskkill.exe" /PID $Launcher.Id /T /F | Out-Null }
    throw "EXACT_UNIT_PUBLIC_URL_MISSING"
}

$IdentityResult = Invoke-JsonProcess -FilePath "python" -Arguments @(
    $Identity,
    "--url", $PublicUrl,
    "--expected-root", $ResolvedRoot,
    "--expected-port", "$ResolvedPort",
    "--scope-mode", "parallel-exact-unit",
    "--topology-receipt-sha256", $TopologyReceipt,
    "--timeout", "12"
)
if (-not [bool]$IdentityResult.ok) {
    & "$env:SystemRoot\System32\taskkill.exe" /PID $Launcher.Id /T /F | Out-Null
    throw "EXACT_UNIT_SERVER_IDENTITY_FAILED"
}

$EndpointKey = [string]$IdentityResult.endpoint_key
$PublicUrlHash = ([System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::HashData([System.Text.Encoding]::UTF8.GetBytes($PublicUrl)))).Replace("-", "").ToLowerInvariant()
$Listener = Invoke-JsonProcess -FilePath "python" -Arguments @(
    $ProcessIdentity, "--kind", "listener", "--port", "$ResolvedPort", "--endpoint-key", $EndpointKey,
    "--topology-receipt-sha256", $TopologyReceipt, "--launcher-pid", "$($Launcher.Id)", "--launcher-created-at", $LauncherCreatedAt
)
$Tunnel = Invoke-JsonProcess -FilePath "python" -Arguments @(
    $ProcessIdentity, "--kind", "tunnel", "--port", "$ResolvedPort", "--endpoint-key", $EndpointKey,
    "--topology-receipt-sha256", $TopologyReceipt, "--launcher-pid", "$($Launcher.Id)", "--launcher-created-at", $LauncherCreatedAt,
    "--public-url-sha256", $PublicUrlHash
)

$UpdatedDecision = Invoke-JsonProcess -FilePath "python" -Arguments ($managerArgs + @("--public-url", $PublicUrl, "--verified-open-port", "--update"))
$Receipt = [ordered]@{
    schema = "codexpro.exact-unit-bootstrap/v1"
    status = "ready"
    scope_mode = "parallel-exact-unit"
    unit_root = $ResolvedRoot
    app_name = [string]$UpdatedDecision.app_name
    port = $ResolvedPort
    endpoint_key = $EndpointKey
    public_url_sha256 = $PublicUrlHash
    topology_receipt_sha256 = $TopologyReceipt
    launcher_pid = $Launcher.Id
    launcher_created_at = $LauncherCreatedAt
    listener_identity = $Listener.receipt
    tunnel_identity = $Tunnel.receipt
    server_identity = $IdentityResult
}
$Receipt | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
$Receipt | ConvertTo-Json -Depth 16
