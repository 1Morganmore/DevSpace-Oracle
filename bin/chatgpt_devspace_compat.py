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

SUPPORTED_VERSION = "1.0.6"
CREATE_NO_WINDOW = 0x08000000
PATCHES = {
    "dist/server.js": {
        "patch": "server.patch",
        "pristine": "84cd96ad4a021abd29dc028c0fb74acce17ab92a4a653d033d5dd830630c2096",
        "patched": "fbe241bc6ef1c91e9aa4866637d9b3890de20adef30fd4d5d0920bf5306e5f1b",
    },
    "dist/workspaces.js": {
        "patch": "workspaces.patch",
        "pristine": "0da528d01555ab3cda0ddc71b749ff30db74497165fffb78e36ca84c97c38d8f",
        "patched": "6f2610f22bb678ab768dde9ab4558296f65bf8cbcc247aa9a9d03b4133fab21d",
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
    return {
        "ok": True,
        "version": SUPPORTED_VERSION,
        "package_roots": [str(root) for root in roots],
        "changed": changed,
        "already_patched": already,
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
        description="Apply the exact DevSpace 1.0.6 ChatGPT compatibility patches."
    )
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--confirm-service-restarted", action="store_true")
    parser.add_argument("--stop-exact-service", action="store_true")
    parser.add_argument("--local-port", type=int, default=7676)
    args = parser.parse_args(argv)
    try:
        if args.confirm_service_restarted and args.stop_exact_service:
            raise DevSpaceCompatError(
                "DEVSPACE_COMPAT_ACTION_CONFLICT",
                "choose only one DevSpace compatibility action",
            )
        if args.confirm_service_restarted:
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
