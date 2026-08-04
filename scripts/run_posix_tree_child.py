#!/usr/bin/env python3
"""Run one command under a Linux subreaper and settle every descendant."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import shutil
import time
from pathlib import Path


PR_SET_CHILD_SUBREAPER = 36
PR_SET_PDEATHSIG = 1
TIMEOUT_EXIT_CODE = 124
_stop_requested = False


def _request_stop(_signum: int, _frame: object) -> None:
    global _stop_requested
    _stop_requested = True


def _enable_subreaper() -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0


def _bind_to_parent(expected_parent_pid: int) -> bool:
    observed_parent = os.getppid()
    expected = expected_parent_pid if expected_parent_pid > 0 else observed_parent
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        return False
    return os.getppid() == expected


def _proc_table() -> dict[int, tuple[int, str, int]]:
    table: dict[int, tuple[int, str, int]] = {}
    for item in Path('/proc').iterdir():
        if not item.name.isdigit():
            continue
        try:
            raw = (item / 'stat').read_text(encoding='utf-8', errors='replace')
            fields = raw[raw.rfind(')') + 2:].split()
            table[int(item.name)] = (int(fields[1]), fields[0], int(fields[19]))
        except (FileNotFoundError, PermissionError, IndexError, ValueError, OSError):
            continue
    return table


def _descendants(supervisor_pid: int) -> dict[int, int]:
    table = _proc_table()
    descendants: dict[int, int] = {}
    frontier = [supervisor_pid]
    while frontier:
        parent = frontier.pop()
        for pid, (ppid, state, started) in table.items():
            if ppid != parent or pid in descendants or state == 'Z':
                continue
            descendants[pid] = started
            frontier.append(pid)
    return descendants


def _identity_alive(pid: int, started: int) -> bool:
    current = _proc_table().get(pid)
    return bool(current and current[1] != 'Z' and current[2] == started)


def _signal_identity(pid: int, started: int, requested_signal: int) -> str | None:
    """Signal the exact observed process object, never a reused numeric PID."""
    if not _identity_alive(pid, started):
        return None
    try:
        pidfd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return None
    except OSError as error:
        return str(error)
    try:
        if not _identity_alive(pid, started):
            return None
        signal.pidfd_send_signal(pidfd, requested_signal)
        return None
    except ProcessLookupError:
        return None
    except OSError as error:
        return str(error)
    finally:
        os.close(pidfd)


def _reap_orphans() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid <= 0:
            return


def _terminate_descendants(supervisor_pid: int, tracked: dict[int, int]) -> dict[str, object]:
    tracked.update(_descendants(supervisor_pid))
    live = {pid: started for pid, started in tracked.items() if _identity_alive(pid, started)}
    if not live:
        _reap_orphans()
        return {'termination_requested': False, 'termination_escalated': False, 'termination_confirmed': True, 'residual_process_id': None, 'termination_error': None}
    signal_errors = [error for pid, started in sorted(live.items(), reverse=True) if (error := _signal_identity(pid, started, signal.SIGTERM))]
    if signal_errors:
        return {'termination_requested': True, 'termination_escalated': False, 'termination_confirmed': False, 'residual_process_id': next(iter(live), None), 'termination_error': f'pidfd SIGTERM failed: {signal_errors[0]}'}
    deadline = time.monotonic() + 0.75
    while time.monotonic() < deadline:
        _reap_orphans()
        tracked.update(_descendants(supervisor_pid))
        live = {pid: started for pid, started in tracked.items() if _identity_alive(pid, started)}
        if not live:
            return {'termination_requested': True, 'termination_escalated': False, 'termination_confirmed': True, 'residual_process_id': None, 'termination_error': None}
        time.sleep(0.02)
    signal_errors = [error for pid, started in sorted(live.items(), reverse=True) if (error := _signal_identity(pid, started, signal.SIGKILL))]
    if signal_errors:
        return {'termination_requested': True, 'termination_escalated': True, 'termination_confirmed': False, 'residual_process_id': next(iter(live), None), 'termination_error': f'pidfd SIGKILL failed: {signal_errors[0]}'}
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        _reap_orphans()
        tracked.update(_descendants(supervisor_pid))
        live = {pid: started for pid, started in tracked.items() if _identity_alive(pid, started)}
        if not live:
            return {'termination_requested': True, 'termination_escalated': True, 'termination_confirmed': True, 'residual_process_id': None, 'termination_error': None}
        time.sleep(0.02)
    residual = next(iter(live), None)
    return {'termination_requested': True, 'termination_escalated': True, 'termination_confirmed': False, 'residual_process_id': residual, 'termination_error': 'Linux subreaper retained a live descendant'}


def _open_random_result_temp(path: Path) -> tuple[int, Path]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    for _attempt in range(32):
        temporary = path.with_name(
            f'.{path.name}.{os.getpid()}.{secrets.token_hex(16)}.tmp'
        )
        try:
            return os.open(temporary, flags, 0o600), temporary
        except FileExistsError:
            continue
    raise RuntimeError('could not reserve a unique process-supervisor receipt')


def _sync_parent_directory(path: Path) -> None:
    if os.name == 'nt':
        return
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    directory = os.open(path.parent, flags)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_result(path: Path, result: dict[str, object], receipt_nonce: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = _open_random_result_temp(path)
    payload = json.dumps(
        {**result, 'receipt_nonce': receipt_nonce},
        separators=(',', ':'),
    ).encode('utf-8')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        leaf = os.lstat(path)
        if stat.S_ISLNK(leaf.st_mode) or not stat.S_ISREG(leaf.st_mode):
            raise RuntimeError('process-supervisor receipt is not a regular file')
        _sync_parent_directory(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--hard-timeout-seconds', type=float, required=True)
    parser.add_argument('--result-file', type=Path, required=True)
    parser.add_argument('--cancel-file', type=Path, required=True)
    parser.add_argument('--parent-pid', type=int, required=True)
    parser.add_argument('--receipt-nonce', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not args.receipt_nonce:
        parser.error('--receipt-nonce must not be empty')
    command = list(args.command)
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        parser.error('a child command is required')
    if not sys.platform.startswith('linux') or not Path('/proc').is_dir() or not _bind_to_parent(args.parent_pid):
        result = {'containment_kind': 'linux_subreaper', 'containment_established': False, 'exit_code': 126, 'timed_out': False, 'termination_requested': False, 'termination_escalated': False, 'termination_confirmed': False, 'residual_process_id': None, 'termination_error': 'Linux subreaper containment is unavailable'}
        _write_result(args.result_file, result, args.receipt_nonce)
        return 126

    if os.getpid() != 1:
        unshare = shutil.which('unshare')
        if not unshare:
            result = {'containment_kind': 'linux_pid_namespace', 'containment_established': False, 'exit_code': 126, 'timed_out': False, 'termination_requested': False, 'termination_escalated': False, 'termination_confirmed': False, 'residual_process_id': None, 'termination_error': 'unshare is unavailable'}
            _write_result(args.result_file, result, args.receipt_nonce)
            return 126
        namespace_command = [
            unshare, '--user', '--map-root-user', '--pid', '--fork',
            '--kill-child=SIGKILL', '--mount-proc',
            sys.executable, str(Path(__file__).resolve()),
            '--hard-timeout-seconds', str(args.hard_timeout_seconds),
            '--result-file', str(args.result_file),
            '--cancel-file', str(args.cancel_file),
            '--parent-pid', '0',
            '--receipt-nonce', args.receipt_nonce,
            '--', *command,
        ]
        try:
            # Keep the caller's direct child as the unshare lifecycle authority.
            # PR_SET_PDEATHSIG survives exec, and --kill-child ties namespace PID 1
            # to this same process, so there is no intermediate supervisor whose
            # early exit could orphan a detached namespace workload.
            os.execvpe(unshare, namespace_command, dict(os.environ))
        except OSError as error:
            result = {'containment_kind': 'linux_pid_namespace', 'containment_established': False, 'exit_code': 126, 'timed_out': False, 'termination_requested': False, 'termination_escalated': False, 'termination_confirmed': False, 'residual_process_id': None, 'termination_error': f'unshare exec failed: {error}'}
            _write_result(args.result_file, result, args.receipt_nonce)
            return 126

    if not _enable_subreaper():
        result = {'containment_kind': 'linux_pid_namespace', 'containment_established': False, 'exit_code': 126, 'timed_out': False, 'termination_requested': False, 'termination_escalated': False, 'termination_confirmed': False, 'residual_process_id': None, 'termination_error': 'Linux subreaper setup failed inside PID namespace'}
        _write_result(args.result_file, result, args.receipt_nonce)
        return 126
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        result = {'containment_kind': 'linux_pid_namespace', 'containment_established': False, 'exit_code': 126, 'timed_out': False, 'termination_requested': False, 'termination_escalated': False, 'termination_confirmed': False, 'residual_process_id': None, 'termination_error': 'pidfd signaling is unavailable'}
        _write_result(args.result_file, result, args.receipt_nonce)
        return 126

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    started = time.monotonic()
    process = subprocess.Popen(command, stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer, start_new_session=True)
    tracked: dict[int, int] = {}
    deadline = started + args.hard_timeout_seconds
    timed_out = False
    while process.poll() is None and not _stop_requested and not args.cancel_file.exists():
        tracked.update(_descendants(os.getpid()))
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.02)
    leader_code = process.poll()
    termination = _terminate_descendants(os.getpid(), tracked)
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            termination = {**termination, 'termination_confirmed': False, 'residual_process_id': process.pid, 'termination_error': 'Linux subreaper leader was not reaped'}
    if leader_code is None:
        leader_code = process.returncode
    canceled = _stop_requested or args.cancel_file.exists()
    exit_code = TIMEOUT_EXIT_CODE if timed_out else (143 if canceled else int(leader_code or 0))
    result = {
        'containment_kind': 'linux_pid_namespace',
        'containment_established': True,
        'exit_code': exit_code,
        'elapsed_seconds': round(time.monotonic() - started, 2),
        'timed_out': timed_out,
        **termination,
    }
    _write_result(args.result_file, result, args.receipt_nonce)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
