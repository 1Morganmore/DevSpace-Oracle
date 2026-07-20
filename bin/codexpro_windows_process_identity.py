from __future__ import annotations

"""Windows listener and Cloudflare process identity receipts for exact-unit apps."""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

LISTENER_SCHEMA = "codexpro.listener-identity/v1"
TUNNEL_SCHEMA = "codexpro.tunnel-identity/v1"


class ProcessIdentityError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = dict(evidence or {})


class ProcessRunner(Protocol):
    def run_fixed_query(self, query_id: str, arguments: Mapping[str, Any]) -> Any: ...


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: str | os.PathLike[str]) -> str:
    with open(path, "rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


class WindowsProcessRunner:
    """Executes only bounded, fixed PowerShell inventory queries."""

    _SCRIPTS = {
        "listeners": r"""
$port = [int]$args[0]
@(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop | ForEach-Object {
  [pscustomobject]@{ LocalAddress=$_.LocalAddress; LocalPort=$_.LocalPort; OwningProcess=$_.OwningProcess }
}) | ConvertTo-Json -Compress -Depth 4
""",
        "process": r"""
$pidValue = [int]$args[0]
$p = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction Stop
if ($null -eq $p) { throw "process missing" }
[pscustomobject]@{ ProcessId=$p.ProcessId; ParentProcessId=$p.ParentProcessId; CreationDate=$p.CreationDate; ExecutablePath=$p.ExecutablePath; CommandLine=$p.CommandLine } | ConvertTo-Json -Compress -Depth 4
""",
        "children": r"""
$pidValue = [int]$args[0]
@(Get-CimInstance Win32_Process -Filter "ParentProcessId=$pidValue" -ErrorAction Stop | ForEach-Object {
 [pscustomobject]@{ ProcessId=$_.ProcessId; ParentProcessId=$_.ParentProcessId; CreationDate=$_.CreationDate; ExecutablePath=$_.ExecutablePath; CommandLine=$_.CommandLine }
}) | ConvertTo-Json -Compress -Depth 4
""",
    }

    def run_fixed_query(self, query_id: str, arguments: Mapping[str, Any]) -> Any:
        if os.name != "nt":
            raise ProcessIdentityError("WINDOWS_PROCESS_QUERY_UNAVAILABLE", "Windows process identity requires Windows")
        if query_id not in self._SCRIPTS:
            raise ProcessIdentityError("WINDOWS_PROCESS_QUERY_FORBIDDEN", "unregistered process query")
        if query_id == "listeners":
            argv = [str(int(arguments["port"]))]
        else:
            argv = [str(int(arguments["pid"]))]
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", self._SCRIPTS[query_id], *argv],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise ProcessIdentityError("WINDOWS_PROCESS_QUERY_FAILED", completed.stderr.strip() or query_id)
        text = completed.stdout.strip()
        if not text:
            return [] if query_id in {"listeners", "children"} else {}
        return json.loads(text)


def _items(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return list(value)
    raise ProcessIdentityError("WINDOWS_PROCESS_QUERY_INVALID", "process query returned invalid JSON")


def _path_final(value: str) -> str:
    try:
        return str(Path(value).resolve(strict=True))
    except OSError as exc:
        raise ProcessIdentityError("APP_LISTENER_IMAGE_UNREADABLE", "process executable is unreadable", {"path": value}) from exc


def _normalized_command_hash(command_line: str, executable_final: str) -> str:
    # CommandLineToArgvW semantics are supplied by Windows; shlex(posix=False)
    # is deterministic for fixtures and does not authorize shell execution.
    try:
        argv = shlex.split(command_line or "", posix=False)
    except ValueError as exc:
        raise ProcessIdentityError("APP_PROCESS_COMMAND_INVALID", "process command line is malformed") from exc
    if not argv:
        argv = [executable_final]
    else:
        argv[0] = executable_final
    return canonical_sha256(argv)


def process_snapshot(runner: ProcessRunner, pid: int) -> dict[str, Any]:
    rows = _items(runner.run_fixed_query("process", {"pid": int(pid)}))
    if len(rows) != 1:
        raise ProcessIdentityError("APP_PROCESS_IDENTITY_MISSING", "process identity is not unique", {"pid": pid})
    row = rows[0]
    executable = str(row.get("ExecutablePath") or row.get("executable_path") or "")
    if not executable:
        raise ProcessIdentityError("APP_LISTENER_IMAGE_UNREADABLE", "process executable path is missing", {"pid": pid})
    final = _path_final(executable)
    return {
        "pid": int(row.get("ProcessId") or row.get("process_id") or pid),
        "parent_pid": int(row.get("ParentProcessId") or row.get("parent_process_id") or 0),
        "creation_time_utc": str(row.get("CreationDate") or row.get("creation_time_utc") or ""),
        "executable_final_path": final,
        "executable_sha256": file_sha256(final),
        "normalized_command_line_sha256": _normalized_command_hash(str(row.get("CommandLine") or row.get("command_line") or ""), final),
        "command_line": str(row.get("CommandLine") or row.get("command_line") or ""),
    }


def parent_chain(runner: ProcessRunner, start_pid: int, launcher_pid: int, launcher_creation_time_utc: str, *, max_depth: int = 16) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    pid = int(start_pid)
    seen: set[int] = set()
    while pid > 0 and len(chain) < max_depth:
        if pid in seen:
            raise ProcessIdentityError("APP_LISTENER_PARENT_CHAIN_INVALID", "process parent chain contains a cycle")
        seen.add(pid)
        snapshot = process_snapshot(runner, pid)
        chain.append({key: value for key, value in snapshot.items() if key != "command_line"})
        if pid == int(launcher_pid):
            if launcher_creation_time_utc and snapshot["creation_time_utc"] != launcher_creation_time_utc:
                raise ProcessIdentityError("APP_LISTENER_PARENT_CHAIN_INVALID", "launcher creation identity differs")
            return chain
        pid = int(snapshot["parent_pid"])
    raise ProcessIdentityError("APP_LISTENER_PARENT_CHAIN_INVALID", "listener is not a descendant of the launcher", {"launcher_pid": launcher_pid, "start_pid": start_pid})


def collect_listener_identity(
    *,
    runner: ProcessRunner,
    port: int,
    endpoint_key: str,
    topology_receipt_sha256: str,
    launcher_pid: int,
    launcher_creation_time_utc: str,
) -> dict[str, Any]:
    rows = _items(runner.run_fixed_query("listeners", {"port": int(port)}))
    matching = [row for row in rows if int(row.get("LocalPort") or row.get("local_port") or port) == int(port)]
    pids = sorted({int(row.get("OwningProcess") or row.get("owning_process") or 0) for row in matching if int(row.get("OwningProcess") or row.get("owning_process") or 0) > 0})
    if not pids:
        raise ProcessIdentityError("APP_LISTENER_OWNER_MISSING", "no process owns the exact-unit listener port", {"port": port})
    if len(pids) != 1:
        raise ProcessIdentityError("APP_LISTENER_OWNER_AMBIGUOUS", "multiple processes own the exact-unit listener port", {"port": port, "pids": pids})
    listener = process_snapshot(runner, pids[0])
    if listener["pid"] == int(launcher_pid):
        raise ProcessIdentityError("APP_LISTENER_WRAPPER_ONLY", "launcher wrapper is not accepted as listener identity")
    chain = parent_chain(runner, listener["pid"], int(launcher_pid), launcher_creation_time_utc)
    receipt = {
        "schema": LISTENER_SCHEMA,
        "listener_pid": listener["pid"],
        "listener_creation_time_utc": listener["creation_time_utc"],
        "listener_executable_final_path": listener["executable_final_path"],
        "listener_executable_sha256": listener["executable_sha256"],
        "listener_normalized_command_line_sha256": listener["normalized_command_line_sha256"],
        "listener_parent_pid": listener["parent_pid"],
        "launcher_pid": int(launcher_pid),
        "launcher_creation_time_utc": launcher_creation_time_utc,
        "parent_process_chain_sha256": canonical_sha256(chain),
        "port": int(port),
        "local_addresses": sorted({str(row.get("LocalAddress") or row.get("local_address") or "") for row in matching}),
        "endpoint_key": endpoint_key,
        "topology_receipt_sha256": topology_receipt_sha256,
    }
    receipt["listener_identity_receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def collect_tunnel_identity(
    *,
    runner: ProcessRunner,
    launcher_pid: int,
    launcher_creation_time_utc: str,
    port: int,
    endpoint_key: str,
    topology_receipt_sha256: str,
    public_url_sha256: str,
) -> dict[str, Any]:
    descendants: list[dict[str, Any]] = []
    queue = [int(launcher_pid)]
    seen: set[int] = set()
    while queue and len(seen) < 128:
        parent = queue.pop(0)
        if parent in seen:
            continue
        seen.add(parent)
        for row in _items(runner.run_fixed_query("children", {"pid": parent})):
            pid = int(row.get("ProcessId") or row.get("process_id") or 0)
            if pid <= 0:
                continue
            snapshot = process_snapshot(runner, pid)
            descendants.append(snapshot)
            queue.append(pid)
    upstream = f"127.0.0.1:{int(port)}"
    matches = [
        item for item in descendants
        if Path(item["executable_final_path"]).name.casefold() == "cloudflared.exe"
        and upstream.casefold() in str(item.get("command_line") or "").casefold()
    ]
    if not matches:
        raise ProcessIdentityError("APP_TUNNEL_IDENTITY_MISSING", "no exact Cloudflare tunnel process was found")
    if len(matches) != 1:
        raise ProcessIdentityError("APP_TUNNEL_IDENTITY_AMBIGUOUS", "multiple exact Cloudflare tunnel processes were found")
    tunnel = matches[0]
    chain = parent_chain(runner, tunnel["pid"], int(launcher_pid), launcher_creation_time_utc)
    receipt = {
        "schema": TUNNEL_SCHEMA,
        "provider": "cloudflare",
        "tunnel_pid": tunnel["pid"],
        "tunnel_creation_time_utc": tunnel["creation_time_utc"],
        "tunnel_executable_final_path": tunnel["executable_final_path"],
        "tunnel_executable_sha256": tunnel["executable_sha256"],
        "tunnel_normalized_command_line_sha256": tunnel["normalized_command_line_sha256"],
        "tunnel_parent_process_chain_sha256": canonical_sha256(chain),
        "local_upstream": upstream,
        "public_url_sha256": public_url_sha256,
        "endpoint_key": endpoint_key,
        "topology_receipt_sha256": topology_receipt_sha256,
    }
    receipt["tunnel_identity_receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def assert_identity_unchanged(before: Mapping[str, Any], after: Mapping[str, Any], *, kind: str) -> None:
    key = "listener_identity_receipt_sha256" if kind == "listener" else "tunnel_identity_receipt_sha256"
    if str(before.get(key) or "") != str(after.get(key) or ""):
        code = "APP_LISTENER_IDENTITY_DRIFT" if kind == "listener" else "APP_TUNNEL_IDENTITY_DRIFT"
        raise ProcessIdentityError(code, f"{kind} process identity changed")


def _main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Windows exact-unit process identities")
    parser.add_argument("--kind", choices=("listener", "tunnel"), default="listener")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--endpoint-key", required=True)
    parser.add_argument("--topology-receipt-sha256", required=True)
    parser.add_argument("--launcher-pid", type=int, required=True)
    parser.add_argument("--launcher-created-at", required=True)
    parser.add_argument("--public-url-sha256")
    args = parser.parse_args()
    try:
        if args.kind == "listener":
            receipt = collect_listener_identity(
                runner=WindowsProcessRunner(), port=args.port, endpoint_key=args.endpoint_key,
                topology_receipt_sha256=args.topology_receipt_sha256, launcher_pid=args.launcher_pid,
                launcher_creation_time_utc=args.launcher_created_at,
            )
        else:
            if not args.public_url_sha256:
                raise ProcessIdentityError("APP_TUNNEL_PUBLIC_URL_HASH_MISSING", "tunnel identity requires public URL hash")
            receipt = collect_tunnel_identity(
                runner=WindowsProcessRunner(), launcher_pid=args.launcher_pid,
                launcher_creation_time_utc=args.launcher_created_at, port=args.port,
                endpoint_key=args.endpoint_key, topology_receipt_sha256=args.topology_receipt_sha256,
                public_url_sha256=args.public_url_sha256,
            )
        print(json.dumps({"ok": True, "receipt": receipt}, ensure_ascii=False, sort_keys=True))
        return 0
    except ProcessIdentityError as exc:
        print(json.dumps({"ok": False, "error": {"errorCode": exc.code, "message": str(exc), "evidence": exc.evidence}}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
