#!/usr/bin/env python
"""Run one child command inside the verified Windows Job Object boundary."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
import ctypes
import threading
from ctypes import wintypes
from pathlib import Path

import run_fast_gate


def _open_random_control_temp(path: Path) -> tuple[int, Path]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(32):
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"
        )
        try:
            return os.open(temporary, flags, 0o600), temporary
        except FileExistsError:
            continue
    raise RuntimeError("could not reserve a unique process-supervisor control file")


def _write_atomic_control_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = _open_random_control_temp(path)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        leaf = os.lstat(path)
        if stat.S_ISLNK(leaf.st_mode) or not stat.S_ISREG(leaf.st_mode):
            raise RuntimeError("process-supervisor control file is not a regular file")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_result(path: Path, result: dict[str, object], receipt_nonce: str) -> None:
    payload = json.dumps(
        {**result, "receipt_nonce": receipt_nonce},
        separators=(",", ":"),
    ).encode("utf-8")
    _write_atomic_control_file(path, payload)


def _handle_parent_wait_result(wait_result: int, parent_dead: object) -> None:
    if wait_result != 0:
        raise RuntimeError(f"parent process wait failed: {wait_result}")
    parent_dead.set()


def _watch_parent(parent_pid: int) -> run_fast_gate._CancellationSignal | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x00100000, True, parent_pid)
    if not handle:
        return None
    parent_dead = run_fast_gate._CancellationSignal(wait_handle=int(handle))

    def wait_for_parent() -> None:
        try:
            _handle_parent_wait_result(
                kernel32.WaitForSingleObject(handle, 0xFFFFFFFF),
                parent_dead,
            )
        except BaseException:
            # Closing this wrapper closes its kill-on-close Job handle and
            # therefore fails closed if the parent wait is attacked.
            os._exit(126)

    threading.Thread(target=wait_for_parent, daemon=True).start()
    return parent_dead


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hard-timeout-seconds", type=float, required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    parser.add_argument("--cancel-file", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--receipt-nonce", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not args.receipt_nonce:
        parser.error("--receipt-nonce must not be empty")
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a child command is required")
    if os.name != "nt":
        raise SystemExit("run_windows_job_child.py is Windows-only")
    parent_dead = _watch_parent(args.parent_pid)
    if parent_dead is None:
        result = {
            "containment_kind": "windows_job",
            "containment_established": False,
            "windows_job_assigned": False,
            "exit_code": 126,
            "timed_out": False,
            "termination_requested": False,
            "termination_escalated": False,
            "termination_confirmed": False,
            "residual_process_id": None,
            "termination_error": "parent process identity could not be opened",
        }
        _write_result(args.result_file, result, args.receipt_nonce)
        return 126

    result = run_fast_gate.run_gate_command(
        command,
        cwd=Path.cwd(),
        environment=dict(os.environ),
        hard_timeout_seconds=args.hard_timeout_seconds,
        forward_stdio=True,
        cancel_file=args.cancel_file,
        cancel_event=parent_dead,
    )
    result['containment_kind'] = 'windows_job'
    result['containment_established'] = bool(result['windows_job_assigned'])
    _write_result(args.result_file, result, args.receipt_nonce)
    if not result["windows_job_assigned"]:
        print("Windows Job child could not acquire its required Job Object", file=sys.stderr)
        return 126
    if result["timed_out"]:
        print(
            f"Windows Job child timed out after {args.hard_timeout_seconds} seconds",
            file=sys.stderr,
        )
    if not result["termination_confirmed"] and result["termination_requested"]:
        print(
            f"Windows Job child cleanup was not confirmed: {result['termination_error']}",
            file=sys.stderr,
        )
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
