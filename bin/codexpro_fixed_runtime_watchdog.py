#!/usr/bin/env python
"""Keep the local CodexPro server behind one fixed ngrok tunnel available."""

from __future__ import annotations

import argparse
import json
import msvcrt
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CREATE_NO_WINDOW = 0x08000000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tcp_listening(port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def fixed_tunnel_present(hostname: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=timeout) as response:
            payload = json.load(response)
    except Exception:
        return False
    expected_url = f"https://{hostname.strip().lower()}"
    expected_addr = f"http://127.0.0.1:{int(port)}"
    for item in payload.get("tunnels") or []:
        public_url = str(item.get("public_url") or "").rstrip("/").lower()
        addr = str((item.get("config") or {}).get("addr") or "").rstrip("/").lower()
        if public_url == expected_url and addr == expected_addr:
            return True
    return False


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def acquire_singleton(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def repair(bootstrap: Path, root: str, timeout: int) -> subprocess.CompletedProcess[str]:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(bootstrap),
            "-Root",
            root,
            "-TunnelProvider",
            "auto",
            "-WaitSeconds",
            "60",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        startupinfo=startup,
    )


def run(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    slug = args.root.replace(":", "Drive").replace("\\", "").replace("/", "") or "root"
    lock = acquire_singleton(state_dir / f"fixed-runtime-watchdog-{slug}.lock")
    if lock is None:
        return 0
    state_path = state_dir / f"fixed-runtime-watchdog-{slug}.json"
    failures = 0
    restarts = 0
    try:
        while True:
            tunnel_ok = fixed_tunnel_present(args.hostname, args.port)
            listener_ok = tcp_listening(args.port)
            status = "healthy" if tunnel_ok and listener_ok else "waiting"
            detail = "fixed tunnel and local listener are available"
            if not tunnel_ok:
                status = "stopped-fixed-tunnel-absent"
                detail = "the exact fixed ngrok tunnel is absent; watchdog will not create or replace it"
            elif not listener_ok:
                status = "repairing-local-listener"
                detail = "fixed tunnel is present but the local listener is absent"
                try:
                    completed = repair(Path(args.bootstrap), args.root, args.repair_timeout)
                    listener_ok = tcp_listening(args.port, timeout=2.0)
                    if completed.returncode == 0 and listener_ok:
                        restarts += 1
                        failures = 0
                        status = "repaired-local-listener"
                        detail = "reused the fixed tunnel and restarted only the local CodexPro server"
                    else:
                        failures += 1
                        status = "repair-failed"
                        detail = f"bootstrap exit={completed.returncode}; listener={listener_ok}"
                except Exception as exc:
                    failures += 1
                    status = "repair-error"
                    detail = f"{type(exc).__name__}: {exc}"
            atomic_json(
                state_path,
                {
                    "schema": "codexpro.fixed-runtime-watchdog/v1",
                    "pid": os.getpid(),
                    "root": args.root,
                    "port": args.port,
                    "hostname": args.hostname,
                    "status": status,
                    "detail": detail,
                    "listener_ok": listener_ok,
                    "fixed_tunnel_ok": tunnel_ok,
                    "restart_count": restarts,
                    "consecutive_failures": failures,
                    "checked_at": utc_now(),
                },
            )
            if args.once or not tunnel_ok:
                return 0 if listener_ok and tunnel_ok else 2
            time.sleep(max(5, args.interval))
    finally:
        try:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--bootstrap", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--repair-timeout", type=int, default=90)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
