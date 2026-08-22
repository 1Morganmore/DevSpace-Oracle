from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

SUPPORTED_VERSION = "1.0.7"
CREATE_NO_WINDOW = 0x08000000
PATCHES = {
    "dist/server.js": {
        "patch": "delete-trash.patch",
        "pristine": "42d340924421182eea7f2580f96c8d1d5aae459061a6a90804e6900905ef2d72",
        "patched": "9485795c98de9ecc29c86113b0e726d2ddf1b1abe1817b3656a37ce5fd84d02f",
    },
    "dist/workspaces.js": {
        "patch": "workspaces.patch",
        "pristine": "e11517f291cac33e37a66e84aeb80e1664a5abd0b6eb1e9bdb933d84c186efad",
        "patched": "68a4c61ae0f509bd40d2a682e0b9bbbac72cb00dc96693f7646e6a535cc872ed",
    },
    "dist/oauth-provider.js": {
        "patch": "oauth-refresh-replay.patch",
        "pristine": "90ff3fd116735e98af5751de1065538964f6eaae913171223e8e19337b9831b8",
        "patched": "30790b1c4e83e7865b3519e4c4a99ca3a182264f405f0eea26c80f0c471252dc",
    },
}


class DevSpaceCompatError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package_version(package_root: Path) -> str:
    try:
        value = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DevSpaceCompatError(
            "DEVSPACE_PACKAGE_INVALID",
            "DevSpace package.json is unreadable",
            {"root": str(package_root)},
        ) from exc
    return str(value.get("version") or "").strip()


def _candidate_roots() -> list[Path]:
    override = str(os.environ.get("DEVSPACE_PACKAGE_ROOT") or "").strip()
    if override:
        return [Path(override).expanduser().resolve()]
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    candidates = [appdata / "npm" / "node_modules" / "@waishnav" / "devspace"]
    local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    candidates.extend((local / "npm-cache" / "_npx").glob("*/node_modules/@waishnav/devspace"))
    return sorted(
        {path.resolve() for path in candidates if path.is_dir()},
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def resolve_package_roots(version: str = SUPPORTED_VERSION) -> list[Path]:
    roots = [path for path in _candidate_roots() if package_version(path) == version]
    if not roots:
        raise DevSpaceCompatError(
            "DEVSPACE_PACKAGE_NOT_FOUND",
            "The tested DevSpace package is not installed",
            {"version": version, "candidates": [str(path) for path in _candidate_roots()[:8]]},
        )
    return roots


def check_oauth_refresh_replay(
    *,
    package_root: Path,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Exercise the bounded refresh-token replay grace against an isolated database."""
    root = package_root.expanduser().resolve(strict=True)
    node = shutil.which("node")
    if not node:
        raise DevSpaceCompatError("DEVSPACE_NODE_MISSING", "Node.js is required for DevSpace")
    source = r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { SingleUserOAuthProvider } from "./dist/oauth-provider.js";
const state = fs.mkdtempSync(path.join(os.tmpdir(), "codex-devspace-oauth-replay-"));
const realDateNow = Date.now;
let provider;
try {
  const resource = new URL("https://example.test/mcp");
  provider = new SingleUserOAuthProvider({
    ownerToken: "synthetic-test-only",
    accessTokenTtlSeconds: 60,
    refreshTokenTtlSeconds: 600,
    scopes: ["devspace", "offline_access"],
    allowedRedirectHosts: ["chatgpt.com"],
  }, resource, state);
  const client = await provider.clientsStore.registerClient({
    redirect_uris: ["https://chatgpt.com/connector/oauth/callback"],
    client_name: "DevSpace isolated refresh replay check",
  });

  const seed = provider.issueTokens(client.client_id, ["devspace"], resource);
  const first = await provider.exchangeRefreshToken(client, seed.refresh_token);
  const replay = await provider.exchangeRefreshToken(client, seed.refresh_token);
  assert.deepEqual(replay, first);
  await assert.rejects(() => provider.exchangeRefreshToken(
    { ...client, client_id: "wrong-client" }, seed.refresh_token));
  await assert.rejects(() => provider.exchangeRefreshToken(
    client, seed.refresh_token, ["offline_access"]));
  await assert.rejects(() => provider.exchangeRefreshToken(
    client, seed.refresh_token, undefined, new URL("https://other.test/mcp")));

  const exactScopeSeed = provider.issueTokens(
    client.client_id, ["devspace", "offline_access"], resource);
  const exactScopeFirst = await provider.exchangeRefreshToken(
    client, exactScopeSeed.refresh_token);
  const reorderedScopeReplay = await provider.exchangeRefreshToken(
    client, exactScopeSeed.refresh_token, ["offline_access", "devspace"]);
  assert.deepEqual(reorderedScopeReplay, exactScopeFirst);
  await assert.rejects(() => provider.exchangeRefreshToken(
    client, exactScopeSeed.refresh_token, ["devspace", "devspace"]));

  const duplicateScopeSeed = provider.issueTokens(
    client.client_id, ["devspace", "offline_access"], resource);
  await provider.exchangeRefreshToken(
    client, duplicateScopeSeed.refresh_token, ["devspace", "devspace"]);
  await assert.rejects(() => provider.exchangeRefreshToken(
    client, duplicateScopeSeed.refresh_token, ["devspace", "offline_access"]));

  const ancestorSeed = provider.issueTokens(client.client_id, ["devspace"], resource);
  const firstGeneration = await provider.exchangeRefreshToken(
    client, ancestorSeed.refresh_token);
  const secondGeneration = await provider.exchangeRefreshToken(
    client, firstGeneration.refresh_token);
  await assert.rejects(() => provider.exchangeRefreshToken(client, ancestorSeed.refresh_token));
  const secondGenerationReplay = await provider.exchangeRefreshToken(
    client, firstGeneration.refresh_token);
  assert.deepEqual(secondGenerationReplay, secondGeneration);

  await provider.verifyAccessToken(first.access_token);
  await provider.revokeToken(client, { token: first.refresh_token });
  await assert.rejects(() => provider.exchangeRefreshToken(client, seed.refresh_token));

  const originalTtl = provider.config.refreshTokenTtlSeconds;
  let testNowMs = Math.floor(realDateNow() / 1000) * 1000;
  Date.now = () => testNowMs;
  let nearExpirySeed;
  let sourceBoundarySeed;
  try {
    provider.config.refreshTokenTtlSeconds = 1;
    nearExpirySeed = provider.issueTokens(client.client_id, ["devspace"], resource);
    sourceBoundarySeed = provider.issueTokens(client.client_id, ["devspace"], resource);
  } finally {
    provider.config.refreshTokenTtlSeconds = originalTtl;
  }
  const nearExpiryFirst = await provider.exchangeRefreshToken(
    client, nearExpirySeed.refresh_token);
  const nearExpiryReplay = [...provider.refreshReplays.values()].find(
    (value) => value.tokens.refresh_token === nearExpiryFirst.refresh_token);
  assert.equal(nearExpiryReplay?.expiresAtMs, testNowMs + 1000);
  testNowMs += 1000;
  await assert.rejects(() => provider.exchangeRefreshToken(client, nearExpirySeed.refresh_token));
  await assert.rejects(() => provider.exchangeRefreshToken(client, sourceBoundarySeed.refresh_token));
  Date.now = realDateNow;

  const expiringSeed = provider.issueTokens(client.client_id, ["devspace"], resource);
  await provider.exchangeRefreshToken(client, expiringSeed.refresh_token);
  for (const value of provider.refreshReplays.values()) value.expiresAtMs = 0;
  await assert.rejects(() => provider.exchangeRefreshToken(client, expiringSeed.refresh_token));

  let expiredSeed;
  try {
    provider.config.refreshTokenTtlSeconds = -1;
    expiredSeed = provider.issueTokens(client.client_id, ["devspace"], resource);
  } finally {
    provider.config.refreshTokenTtlSeconds = originalTtl;
  }
  await assert.rejects(() => provider.exchangeRefreshToken(client, expiredSeed.refresh_token));

  const capacitySeeds = [];
  for (let index = 0; index < 33; index += 1) {
    const capacitySeed = provider.issueTokens(client.client_id, ["devspace"], resource);
    capacitySeeds.push(capacitySeed.refresh_token);
    await provider.exchangeRefreshToken(client, capacitySeed.refresh_token);
  }
  assert.equal(provider.refreshReplays.size, 32);
  await assert.rejects(() => provider.exchangeRefreshToken(client, capacitySeeds[0]));
  await provider.exchangeRefreshToken(client, capacitySeeds.at(-1));

  console.log(JSON.stringify({
    ok: true,
    replayed_same_pair: true,
    wrong_client_rejected: true,
    scope_mismatch_rejected: true,
    resource_mismatch_rejected: true,
    scope_order_independent: true,
    duplicate_requested_scopes_rejected: true,
    duplicate_cached_scopes_rejected: true,
    ancestor_replay_invalidated: true,
    revoke_invalidated: true,
    near_expiry_capped: true,
    near_expiry_boundary_rejected: true,
    source_boundary_rejected: true,
    replay_expired_rejected: true,
    source_expired_rejected: true,
    capacity_bounded: true,
    oldest_evicted: true,
  }));
} finally {
  Date.now = realDateNow;
  try {
    if (provider) provider.close();
  } finally {
    fs.rmSync(state, { recursive: true, force: true });
  }
}
"""
    try:
        completed = runner(
            [node, "--input-type=module", "-e", source],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
            **_git_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise DevSpaceCompatError(
            "DEVSPACE_OAUTH_REFRESH_REPLAY_CHECK_TIMEOUT",
            "DevSpace OAuth refresh replay compatibility check timed out",
            {"root": str(root), "timeout_seconds": exc.timeout},
        ) from exc
    except OSError as exc:
        raise DevSpaceCompatError(
            "DEVSPACE_OAUTH_REFRESH_REPLAY_CHECK_UNAVAILABLE",
            "DevSpace OAuth refresh replay compatibility check could not start",
            {"root": str(root), "errno": exc.errno, "error": str(exc)},
        ) from exc
    if completed.returncode != 0:
        raise DevSpaceCompatError(
            "DEVSPACE_OAUTH_REFRESH_REPLAY_CHECK_FAILED",
            "DevSpace OAuth refresh replay compatibility check failed",
            {"root": str(root), "stderr": (completed.stderr or "").strip()[-1200:]},
        )
    try:
        result = json.loads((completed.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise DevSpaceCompatError(
            "DEVSPACE_OAUTH_REFRESH_REPLAY_CHECK_INVALID",
            "DevSpace OAuth refresh replay check did not return valid JSON",
            {"root": str(root)},
        ) from exc
    expected = {
        "ok": True,
        "replayed_same_pair": True,
        "wrong_client_rejected": True,
        "scope_mismatch_rejected": True,
        "resource_mismatch_rejected": True,
        "scope_order_independent": True,
        "duplicate_requested_scopes_rejected": True,
        "duplicate_cached_scopes_rejected": True,
        "ancestor_replay_invalidated": True,
        "revoke_invalidated": True,
        "near_expiry_capped": True,
        "near_expiry_boundary_rejected": True,
        "source_boundary_rejected": True,
        "replay_expired_rejected": True,
        "source_expired_rejected": True,
        "capacity_bounded": True,
        "oldest_evicted": True,
    }
    if result != expected:
        raise DevSpaceCompatError(
            "DEVSPACE_OAUTH_REFRESH_REPLAY_CHECK_INCOMPLETE",
            "DevSpace OAuth refresh replay check did not prove every safety boundary",
            {"root": str(root), "result": result},
        )
    return {**result, "root": str(root), "status": "bounded-replay-verified"}


def patch_root() -> Path:
    return Path(__file__).resolve().parent / "devspace-compat" / SUPPORTED_VERSION


def compat_state_root() -> Path:
    override = str(os.environ.get("CODEX_DEVSPACE_COMPAT_STATE_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".codex" / "state" / "devspace-compat" / SUPPORTED_VERSION).resolve()


def restart_marker_path() -> Path:
    return compat_state_root() / "restart-required.json"


def _write_restart_marker(roots: Sequence[Path]) -> Path:
    marker = restart_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(f"{marker.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(
            {
                "schema": "codex.chatgpt.devspace-restart-required/v1",
                "version": SUPPORTED_VERSION,
                "created_at_unix_ns": time.time_ns(),
                "package_roots": [str(root) for root in roots],
                "patched_files": {
                    str(root / relative): contract["patched"]
                    for root in roots
                    for relative, contract in PATCHES.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker)
    return marker


def _powershell_json(script: str) -> dict[str, Any] | None:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        **_git_kwargs(),
    )
    if completed.returncode == 3:
        return None
    if completed.returncode != 0:
        raise DevSpaceCompatError(
            "DEVSPACE_SERVICE_PROBE_FAILED",
            "DevSpace listener identity could not be inspected",
            {"exit_code": completed.returncode, "stderr": (completed.stderr or "").strip()[-1200:]},
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DevSpaceCompatError(
            "DEVSPACE_SERVICE_PROBE_INVALID",
            "DevSpace listener identity was not valid JSON",
        ) from exc
    return value if isinstance(value, dict) else None


def current_devspace_service_identity(local_port: int = 7676) -> dict[str, Any] | None:
    if os.name != "nt":
        raise DevSpaceCompatError(
            "DEVSPACE_SERVICE_PROBE_UNSUPPORTED",
            "automatic DevSpace restart proof is currently implemented for Windows only",
        )
    script = (
        f"$c=Get-NetTCPConnection -State Listen -LocalPort {int(local_port)} "
        "-ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if($null -eq $c){exit 3}; "
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=$($c.OwningProcess)\"; "
        "if($null -eq $p){exit 3}; "
        "$started=[DateTimeOffset]::new($p.CreationDate.ToUniversalTime()).ToUnixTimeMilliseconds()*1000000; "
        "[pscustomobject]@{pid=[int]$p.ProcessId;command_line=[string]$p.CommandLine;"
        "started_at_unix_ns=[int64]$started;local_port=[int]$c.LocalPort}|ConvertTo-Json -Compress"
    )
    return _powershell_json(script)


def _assert_devspace_service_identity(
    value: dict[str, Any] | None,
    package_roots: Sequence[Path],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DevSpaceCompatError(
            "DEVSPACE_SERVICE_NOT_LISTENING",
            "DevSpace service is not listening on the expected local port",
        )
    command_line = str(value.get("command_line") or "")
    tokens = [quoted or bare for quoted, bare in re.findall(r'"([^"]*)"|(\S+)', command_line)]
    normalized_tokens = [ntpath.normcase(ntpath.normpath(token)) for token in tokens]
    expected_cli_paths = [
        ntpath.normcase(ntpath.normpath(str(root / "dist" / "cli.js")))
        for root in package_roots
    ]
    if not any(
        token == expected
        and index + 1 < len(normalized_tokens)
        and normalized_tokens[index + 1] == "serve"
        for expected in expected_cli_paths
        for index, token in enumerate(normalized_tokens)
    ):
        raise DevSpaceCompatError(
            "DEVSPACE_SERVICE_IDENTITY_MISMATCH",
            "the expected DevSpace port is owned by another process",
            {
                "pid": value.get("pid"),
                "command_line": command_line,
                "expected_cli_paths": expected_cli_paths,
            },
        )
    return value


def stop_exact_devspace_service(
    *,
    local_port: int = 7676,
    service_probe=current_devspace_service_identity,
    stopper: Any | None = None,
    package_roots: Sequence[Path] | None = None,
) -> dict[str, Any]:
    identity = service_probe(local_port)
    if identity is None:
        return {"ok": True, "stopped": False, "reason": "service-absent"}
    roots = list(package_roots or resolve_package_roots())
    identity = _assert_devspace_service_identity(identity, roots)
    pid = int(identity["pid"])
    if stopper is not None:
        stopper(pid)
    else:
        script = (
            f"Stop-Process -Id {pid} -Force -ErrorAction Stop; "
            f"Wait-Process -Id {pid} -ErrorAction SilentlyContinue"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
            **_git_kwargs(),
        )
        if completed.returncode != 0:
            raise DevSpaceCompatError(
                "DEVSPACE_SERVICE_STOP_FAILED",
                "the exact DevSpace service could not be stopped",
                {"pid": pid, "stderr": (completed.stderr or "").strip()[-1200:]},
            )
    return {"ok": True, "stopped": True, "pid": pid}


def _git_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": startup}


def _apply_patch(package_root: Path, patch_path: Path) -> None:
    isolated_env = os.environ.copy()
    isolated_env["GIT_CEILING_DIRECTORIES"] = str(package_root.parent)
    patch_bytes = patch_path.read_bytes().replace(b"\r\n", b"\n")
    for check_only in (True, False):
        argv = ["git", "-c", "core.autocrlf=false", "apply"]
        if check_only:
            argv.append("--check")
        argv.append("-")
        completed = subprocess.run(
            argv,
            cwd=str(package_root),
            input=patch_bytes,
            capture_output=True,
            check=False,
            env=isolated_env,
            **_git_kwargs(),
        )
        if completed.returncode != 0:
            code = "DEVSPACE_PATCH_CHECK_FAILED" if check_only else "DEVSPACE_PATCH_APPLY_FAILED"
            raise DevSpaceCompatError(
                code,
                "DevSpace compatibility patch could not be validated or applied",
                {
                    "patch": str(patch_path),
                    "stderr": (completed.stderr or b"").decode("utf-8", errors="replace").strip()[-1200:],
                },
            )


def inspect_devspace_compatibility(
    *,
    package_root: Path | None = None,
    service_probe=current_devspace_service_identity,
    local_port: int = 7676,
) -> dict[str, Any]:
    """Inspect exact package and listener identity without patching or restarting."""
    roots = (
        resolve_package_roots()
        if package_root is None
        else [package_root.expanduser().resolve(strict=True)]
    )
    files: list[dict[str, str]] = []
    for root in roots:
        if package_version(root) != SUPPORTED_VERSION:
            raise DevSpaceCompatError(
                "DEVSPACE_VERSION_UNVALIDATED",
                "DevSpace compatibility is validated only for the tested version",
                {"root": str(root), "supported": SUPPORTED_VERSION},
            )
        for relative, contract in PATCHES.items():
            target = root / Path(relative)
            current = sha256_file(target)
            status = (
                "patched"
                if current == contract["patched"]
                else "patch_required"
                if current == contract["pristine"]
                else "drift"
            )
            files.append({
                "path": str(target),
                "status": status,
                "actual_sha256": current,
                "expected_patched_sha256": str(contract["patched"]),
            })
    marker = restart_marker_path()
    service_identity = service_probe(local_port)
    service_status = "absent"
    if service_identity is not None:
        try:
            service_identity = _assert_devspace_service_identity(service_identity, roots)
            service_status = "match"
        except DevSpaceCompatError:
            service_status = "mismatch"
    ready = (
        all(item["status"] == "patched" for item in files)
        and not marker.is_file()
        and service_status == "match"
    )
    return {
        "ok": True,
        "ready": ready,
        "version": SUPPORTED_VERSION,
        "package_roots": [str(root) for root in roots],
        "files": files,
        "service_status": service_status,
        "service_identity": service_identity,
        "service_restart_required": marker.is_file(),
        "restart_marker": str(marker),
    }


def ensure_devspace_compatibility(
    *,
    package_root: Path | None = None,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    roots = (
        resolve_package_roots()
        if package_root is None
        else [package_root.expanduser().resolve(strict=True)]
    )
    backup = backup_root or (
        Path.home() / ".codex" / "state" / "devspace-compat-backups" / SUPPORTED_VERSION
    )
    changed: list[str] = []
    already: list[str] = []
    for root in roots:
        if package_version(root) != SUPPORTED_VERSION:
            raise DevSpaceCompatError(
                "DEVSPACE_VERSION_UNVALIDATED",
                "DevSpace compatibility is validated only for the tested version",
                {"root": str(root), "supported": SUPPORTED_VERSION},
            )
        for relative, contract in PATCHES.items():
            target = root / Path(relative)
            current = sha256_file(target)
            item = relative if len(roots) == 1 else f"{root}:{relative}"
            if current == contract["patched"]:
                already.append(item)
                continue
            if current != contract["pristine"]:
                raise DevSpaceCompatError(
                    "DEVSPACE_FILE_HASH_MISMATCH",
                    "DevSpace compatibility refuses an unknown third-party file",
                    {
                        "path": str(target),
                        "actual": current,
                        "expected": [contract["pristine"], contract["patched"]],
                    },
                )
            backup_path = backup / Path(relative)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            if not backup_path.exists():
                shutil.copy2(target, backup_path)
            _apply_patch(root, patch_root() / str(contract["patch"]))
            actual = sha256_file(target)
            if actual != contract["patched"]:
                raise DevSpaceCompatError(
                    "DEVSPACE_PATCH_HASH_MISMATCH",
                    "DevSpace compatibility patch output hash is unexpected",
                    {"path": str(target), "actual": actual, "expected": contract["patched"]},
                )
            changed.append(item)
    marker = restart_marker_path()
    if changed:
        marker = _write_restart_marker(roots)
    oauth_checks = (
        [check_oauth_refresh_replay(package_root=root) for root in roots]
        if "dist/oauth-provider.js" in PATCHES
        else []
    )
    return {
        "ok": True,
        "version": SUPPORTED_VERSION,
        "package_roots": [str(root) for root in roots],
        "changed": changed,
        "already_patched": already,
        "oauth_refresh_replay_checks": oauth_checks,
        "service_restart_required": marker.is_file(),
        "restart_marker": str(marker),
    }


def confirm_service_restarted(
    *,
    package_root: Path | None = None,
    local_port: int = 7676,
    wait_timeout_seconds: float = 20,
    service_probe=current_devspace_service_identity,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    roots = (
        resolve_package_roots()
        if package_root is None
        else [package_root.expanduser().resolve(strict=True)]
    )
    for root in roots:
        if package_version(root) != SUPPORTED_VERSION:
            raise DevSpaceCompatError(
                "DEVSPACE_VERSION_UNVALIDATED",
                "DevSpace restart confirmation requires the tested version",
                {"root": str(root), "supported": SUPPORTED_VERSION},
            )
        for relative, contract in PATCHES.items():
            actual = sha256_file(root / relative)
            if actual != contract["patched"]:
                raise DevSpaceCompatError(
                    "DEVSPACE_RESTART_CONFIRM_HASH_MISMATCH",
                    "DevSpace restart cannot be confirmed before every tested file is patched",
                    {"path": str(root / relative), "actual": actual, "expected": contract["patched"]},
                )
    marker = restart_marker_path()
    existed = marker.is_file()
    if not existed:
        return {
            "ok": True,
            "version": SUPPORTED_VERSION,
            "package_roots": [str(root) for root in roots],
            "restart_confirmed": False,
            "restart_marker_cleared": False,
            "reason": "restart-marker-absent",
        }
    try:
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        patched_at = int(marker_payload["created_at_unix_ns"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise DevSpaceCompatError(
            "DEVSPACE_RESTART_MARKER_INVALID",
            "DevSpace restart marker is unreadable",
            {"path": str(marker)},
        ) from exc
    deadline = time.monotonic() + max(0, wait_timeout_seconds)
    identity: dict[str, Any] | None = None
    while True:
        candidate = service_probe(local_port)
        if isinstance(candidate, dict) and int(candidate.get("started_at_unix_ns") or 0) > patched_at:
            identity = _assert_devspace_service_identity(candidate, roots)
            break
        if time.monotonic() >= deadline:
            raise DevSpaceCompatError(
                "DEVSPACE_RESTART_NOT_PROVEN",
                "DevSpace listener did not start after the compatibility patch",
                {"marker": str(marker), "observed": candidate},
            )
        sleep(min(0.25, max(0, deadline - time.monotonic())))
    if existed:
        marker.unlink()
    return {
        "ok": True,
        "version": SUPPORTED_VERSION,
        "package_roots": [str(root) for root in roots],
        "restart_confirmed": True,
        "restart_marker_cleared": existed,
        "service_identity": identity,
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Apply the exact DevSpace 1.0.7 ChatGPT compatibility patches."
    )
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--confirm-service-restarted", action="store_true")
    parser.add_argument("--stop-exact-service", action="store_true")
    parser.add_argument("--check-oauth-refresh-replay", action="store_true")
    parser.add_argument("--local-port", type=int, default=7676)
    args = parser.parse_args(argv)
    try:
        selected = sum(bool(value) for value in (
            args.confirm_service_restarted,
            args.stop_exact_service,
            args.check_oauth_refresh_replay,
        ))
        if selected > 1:
            raise DevSpaceCompatError(
                "DEVSPACE_COMPAT_ACTION_CONFLICT",
                "choose only one DevSpace compatibility action",
            )
        if args.check_oauth_refresh_replay:
            roots = (
                [args.package_root.expanduser().resolve(strict=True)]
                if args.package_root is not None
                else resolve_package_roots()
            )
            result = {
                "ok": True,
                "version": SUPPORTED_VERSION,
                "checks": [check_oauth_refresh_replay(package_root=root) for root in roots],
            }
        elif args.confirm_service_restarted:
            result = confirm_service_restarted(
                package_root=args.package_root,
                local_port=args.local_port,
            )
        elif args.stop_exact_service:
            result = stop_exact_devspace_service(local_port=args.local_port)
        else:
            result = ensure_devspace_compatibility(package_root=args.package_root)
    except DevSpaceCompatError as exc:
        result = {
            "ok": False,
            "error": {"code": exc.code, "message": str(exc), "evidence": exc.evidence},
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
