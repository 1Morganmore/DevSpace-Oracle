#!/usr/bin/env python
"""Sub-minute gate for Oracle automation changes.

The full suite takes many minutes, which pushed every repair into one-incident-
at-a-time edits.  This gate covers the contracts that actually broke runs before
submission - launch arguments, lifecycle authority, incident ownership,
compatibility patch shape, and release packaging - and must finish well inside
one minute so it can run after every batch of edits.
"""

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
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAST_TARGETS = [
    "tests/test_chatgpt_oracle_state.py",
    "tests/test_chatgpt_oracle_run.py",
    "tests/test_chatgpt_oracle_diagnose.py",
    "tests/test_chatgpt_oracle_incident.py",
    "tests/test_chatgpt_oracle_compat.py",
    "tests/test_chatgpt_devspace_compat.py",
    "tests/test_chatgpt_oracle_profiles.py",
    "tests/test_global_gpt_browser_policy.py",
    "tests/test_pro_project_context_packet.py",
    "tests/test_release_packaging.py",
]

# Keep the sub-minute gate representative as the full contract files grow.
# Full files still run in the comprehensive gates; these nodes cover the
# pre-submit boundaries that make a fresh run safe or unsafe.
FAST_NODE_IDS = [
    "tests/test_chatgpt_oracle_state.py::test_pro_manifest_is_attachment_only_and_hashes_exact_files",
    "tests/test_chatgpt_oracle_state.py::test_context_manifest_is_required_only_for_pro",
    "tests/test_chatgpt_oracle_state.py::test_oracle_commands_pin_the_active_and_recoverable_versions",
    "tests/test_chatgpt_oracle_run.py::test_new_submission_rejects_recovery_only_oracle_0161_before_launch",
    "tests/test_chatgpt_oracle_run.py::test_validated_package_root_is_the_exact_runtime_popen_target",
    "tests/test_chatgpt_oracle_run.py::test_validated_runtime_rejects_an_unlisted_compatibility_root",
    "tests/test_chatgpt_oracle_run.py::test_pro_runner_rejects_an_unvalidated_packet_before_layout",
    "tests/test_chatgpt_oracle_run.py::test_pro_runner_revalidates_nonattachment_evidence_before_popen",
    "tests/test_chatgpt_oracle_run.py::test_recovery_resolves_and_compat_checks_the_exact_stored_version",
    "tests/test_chatgpt_oracle_run.py::test_recovery_rejects_non_exact_override_without_resolve_or_popen",
    "tests/test_chatgpt_oracle_run.py::test_devspace_patch_change_blocks_before_submission_until_restart",
    "tests/test_chatgpt_oracle_compat.py::test_prompt_composer_patch_applies_to_pristine_0170_and_is_idempotent",
    "tests/test_chatgpt_oracle_compat.py::test_literal_devspace_without_semantic_token_clears_and_fails_before_send",
    "tests/test_chatgpt_oracle_compat.py::test_exact_semantic_devspace_token_proceeds_when_transient_ui_is_absent",
    "tests/test_chatgpt_oracle_compat.py::test_only_visible_exact_devspace_token_inside_editor_can_authorize_send",
    "tests/test_chatgpt_devspace_compat.py::test_service_identity_normalizes_npx_bin_parent_path",
    "tests/test_chatgpt_oracle_diagnose.py",
    "tests/test_chatgpt_oracle_incident.py",
    "tests/test_chatgpt_oracle_profiles.py",
    "tests/test_global_gpt_browser_policy.py",
    "tests/test_pro_project_context_packet.py",
    "tests/test_release_packaging.py",
]

DEFAULT_BUDGET_SECONDS = 60.0
DEFAULT_HARD_TIMEOUT_SECONDS = 75.0
TIMEOUT_EXIT_CODE = 124
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
POSIX_CONTAINMENT_FAILURE_EXIT_CODE = 126
POSIX_SUPERVISOR_SETTLE_SECONDS = 7.0


class _CancellationSignal:
    """Persistent cancellation state, optionally bound to an inheritable wait handle."""

    def __init__(self, *, wait_handle: int | None = None) -> None:
        self._event = threading.Event()
        self.wait_handle = wait_handle

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _hidden_process_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}


def _gate_process_kwargs(inherit_handles: list[int] | None = None) -> dict[str, object]:
    kwargs = _hidden_process_kwargs()
    if os.name == "nt":
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | subprocess.CREATE_NEW_PROCESS_GROUP
        if inherit_handles:
            startupinfo = kwargs["startupinfo"]
            startupinfo.lpAttributeList = {"handle_list": inherit_handles}
            kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _assign_windows_kill_job(process: subprocess.Popen[object]) -> int | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    information = _JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    process_handle = kernel32.OpenProcess(0x0001 | 0x0100, False, process.pid)
    assigned = bool(process_handle) and bool(kernel32.AssignProcessToJobObject(job, process_handle))
    if process_handle:
        kernel32.CloseHandle(process_handle)
    if not configured or not assigned:
        kernel32.CloseHandle(job)
        return None
    return int(job)


def _close_windows_job(job: int | None) -> bool:
    if not job or os.name != "nt":
        return True
    return bool(ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(wintypes.HANDLE(job)))


def _windows_job_active_process_count(job: int | None) -> int | None:
    if not job or os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    accounting = _JobObjectBasicAccountingInformation()
    queried = kernel32.QueryInformationJobObject(
        wintypes.HANDLE(job),
        1,
        ctypes.byref(accounting),
        ctypes.sizeof(accounting),
        None,
    )
    return int(accounting.ActiveProcesses) if queried else None


def _terminate_windows_job(
    job: int, process: subprocess.Popen[object], *, grace_seconds: float = 5.0
) -> dict[str, object]:
    """Terminate a Job Object and prove its active-process count reached zero."""
    active = _windows_job_active_process_count(job)
    if active is None:
        return {
            "termination_requested": False,
            "termination_escalated": False,
            "termination_confirmed": False,
            "residual_process_id": process.pid,
            "termination_error": "Windows Job active-process count could not be queried",
            "windows_job_active_processes": None,
        }
    requested = active > 0
    if requested:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.TerminateJobObject(wintypes.HANDLE(job), 1):
            return {
                "termination_requested": True,
                "termination_escalated": False,
                "termination_confirmed": False,
                "residual_process_id": process.pid,
                "termination_error": "TerminateJobObject failed",
                "windows_job_active_processes": active,
            }
    deadline = time.monotonic() + grace_seconds
    while True:
        active = _windows_job_active_process_count(job)
        if active is None:
            return {
                "termination_requested": requested,
                "termination_escalated": False,
                "termination_confirmed": False,
                "residual_process_id": process.pid,
                "termination_error": "Windows Job active-process readback failed",
                "windows_job_active_processes": None,
            }
        if active == 0:
            break
        if time.monotonic() >= deadline:
            return {
                "termination_requested": requested,
                "termination_escalated": False,
                "termination_confirmed": False,
                "residual_process_id": process.pid,
                "termination_error": f"Windows Job retained {active} active process(es)",
                "windows_job_active_processes": active,
            }
        time.sleep(0.02)
    try:
        process.wait(timeout=min(2.0, grace_seconds))
    except subprocess.TimeoutExpired:
        return {
            "termination_requested": requested,
            "termination_escalated": False,
            "termination_confirmed": False,
            "residual_process_id": process.pid,
            "termination_error": "Windows Job leader was not reaped",
            "windows_job_active_processes": 0,
        }
    return {
        "termination_requested": requested,
        "termination_escalated": False,
        "termination_confirmed": True,
        "residual_process_id": None,
        "termination_error": None,
        "windows_job_active_processes": 0,
    }


def _create_inheritable_windows_event() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.restype = wintypes.HANDLE
    handle = kernel32.CreateEventW(None, True, False, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateEventW failed")
    if not kernel32.SetHandleInformation(handle, 0x00000001, 0x00000001):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(error, "SetHandleInformation failed")
    return int(handle)


def _set_windows_event(handle: int) -> bool:
    return bool(
        ctypes.WinDLL("kernel32", use_last_error=True).SetEvent(wintypes.HANDLE(handle))
    )


def _close_windows_event(handle: int | None) -> bool:
    if not handle or os.name != "nt":
        return True
    return bool(
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(wintypes.HANDLE(handle))
    )


def _windows_gated_command(
    command: list[str],
    parent_wait_handle: int | None,
    cancel_file: Path | None,
) -> tuple[list[str], dict[str, object]]:
    cancel_handle = _create_inheritable_windows_event()
    try:
        release_handle = _create_inheritable_windows_event()
    except BaseException:
        _close_windows_event(cancel_handle)
        raise
    wait_handles = [cancel_handle, release_handle]
    if parent_wait_handle:
        wait_handles.insert(0, parent_wait_handle)
    release_index = len(wait_handles) - 1
    bootstrap = (
        "import ctypes,os,subprocess,sys;"
        "count=int(sys.argv[1]);release_index=int(sys.argv[2]);cancel_path=sys.argv[3];"
        "handles=[int(value) for value in sys.argv[4:4+count]];command=sys.argv[4+count:];"
        "array=(ctypes.c_void_p*count)(*handles);kernel32=ctypes.WinDLL('kernel32',use_last_error=True);"
        "wait_result=kernel32.WaitForMultipleObjects(count,array,False,30000);"
        "\nif 0 <= wait_result < count and wait_result != release_index: raise SystemExit(143);"
        "\nif wait_result != release_index: raise SystemExit(125);"
        "\nif cancel_path:"
        "\n try: os.lstat(cancel_path)"
        "\n except FileNotFoundError: pass"
        "\n except OSError: raise SystemExit(126)"
        "\n else: raise SystemExit(143)"
        "\nshell=command[0].lower().endswith(('.cmd','.bat'));"
        "\nraise SystemExit(subprocess.call(command,shell=shell,stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr))"
    )
    launch_command = [
        sys.executable,
        "-c",
        bootstrap,
        str(len(wait_handles)),
        str(release_index),
        str(cancel_file) if cancel_file is not None else "",
        *[str(handle) for handle in wait_handles],
        *command,
    ]
    return launch_command, {
        "cancel_handle": cancel_handle,
        "release_handle": release_handle,
        "inherit_handles": wait_handles,
    }


def _signal_windows_release(release_handle: int) -> bool:
    return _set_windows_event(release_handle)


def _release_windows_gate(
    control: dict[str, object],
    cancel_event: threading.Event | _CancellationSignal | None,
    cancel_file: Path | None,
) -> bool:
    if (
        (cancel_event is not None and cancel_event.is_set())
        or (cancel_file is not None and cancel_file.exists())
    ):
        _set_windows_event(int(control["cancel_handle"]))
        return False
    return _signal_windows_release(int(control["release_handle"]))


def _start_windows_cancel_monitor(
    control: dict[str, object],
    cancel_event: threading.Event | _CancellationSignal | None,
    cancel_file: Path | None,
) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()

    def monitor() -> None:
        while not stop.wait(0.002):
            if (
                (cancel_event is not None and cancel_event.is_set())
                or (cancel_file is not None and cancel_file.exists())
            ):
                _set_windows_event(int(control["cancel_handle"]))
                return

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    return stop, thread


def _close_windows_launch_control(control: dict[str, object] | None) -> bool:
    if not control:
        return True
    cancel_closed = _close_windows_event(int(control["cancel_handle"]))
    release_closed = _close_windows_event(int(control["release_handle"]))
    return cancel_closed and release_closed


def _posix_process_group_exists(process_group_id: int) -> bool:
    proc_root = Path("/proc")
    if sys.platform.startswith("linux") and proc_root.is_dir():
        saw_member = False
        for candidate in proc_root.iterdir():
            if not candidate.name.isdigit():
                continue
            try:
                raw = (candidate / "stat").read_text(encoding="utf-8", errors="replace")
                fields = raw[raw.rfind(")") + 2 :].split()
                state = fields[0]
                member_group = int(fields[2])
            except (FileNotFoundError, PermissionError, IndexError, ValueError, OSError):
                continue
            if member_group != process_group_id:
                continue
            saw_member = True
            if state != "Z":
                return True
        if saw_member:
            # Zombies cannot execute, write artifacts, or spawn descendants. They are
            # awaiting adoption/reaping by init and are not residual live workload.
            return False
    try:
        os.killpg(process_group_id, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def _terminate_process_tree(
    process: subprocess.Popen[object], *, process_group_id: int | None = None, grace_seconds: float = 2.0
) -> dict[str, object]:
    leader_exited = process.poll() is not None
    residual_group = os.name != "nt" and process_group_id is not None and _posix_process_group_exists(process_group_id)
    if leader_exited and not residual_group:
        return {
            "termination_requested": False,
            "termination_escalated": False,
            "termination_confirmed": True,
            "residual_process_id": None,
            "termination_error": None,
        }
    escalated = False
    error_text = None
    if os.name == "nt":
        try:
            killed = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(5.0, grace_seconds),
                **_hidden_process_kwargs(),
            )
            if killed.returncode != 0:
                error_text = f"taskkill exited with {killed.returncode}"
        except (OSError, subprocess.TimeoutExpired) as error:
            error_text = str(error)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            error_text = error_text or "process remained alive after taskkill"
    else:
        # Popen uses start_new_session=True, so the launch-time PID is also the
        # process-group id. Avoid racing getpgid() against a parent that has
        # already exited while descendants are still running.
        process_group_id = process_group_id or process.pid
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline and _posix_process_group_exists(process_group_id):
            time.sleep(0.02)
        if _posix_process_group_exists(process_group_id):
            escalated = True
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            kill_deadline = time.monotonic() + grace_seconds
            while time.monotonic() < kill_deadline and _posix_process_group_exists(process_group_id):
                time.sleep(0.02)
        if _posix_process_group_exists(process_group_id):
            error_text = "process group remained alive after SIGKILL"
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            error_text = error_text or "parent process was not reaped"
    if process.poll() is None and os.name == "nt":
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            error_text = error_text or "direct process remained alive after kill"
    confirmed = process.poll() is not None and error_text is None
    return {
        "termination_requested": True,
        "termination_escalated": escalated,
        "termination_confirmed": confirmed,
        "residual_process_id": None if confirmed else process.pid,
        "termination_error": error_text,
    }


def _posix_containment_failure(
    *,
    started: float,
    hard_timeout_seconds: float,
    process_id: int | None,
    error_text: str,
    termination: dict[str, object] | None = None,
) -> dict[str, object]:
    cleanup = termination or {
        "termination_requested": False,
        "termination_escalated": False,
        "termination_confirmed": False,
        "residual_process_id": process_id,
        "termination_error": error_text,
    }
    return {
        "containment_kind": "linux_pid_namespace",
        "containment_established": False,
        "exit_code": POSIX_CONTAINMENT_FAILURE_EXIT_CODE,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "timed_out": False,
        "hard_timeout_seconds": hard_timeout_seconds,
        "windows_job_assigned": False,
        **cleanup,
        "termination_error": cleanup.get("termination_error") or error_text,
    }


def _read_posix_supervisor_receipt(path: Path) -> dict[str, object]:
    leaf = os.lstat(path)
    if stat.S_ISLNK(leaf.st_mode) or not stat.S_ISREG(leaf.st_mode):
        raise OSError("cleanup evidence is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("cleanup evidence is not a regular file")
        if (leaf.st_dev, leaf.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("cleanup evidence identity changed before read")
        payload = os.read(descriptor, 16 * 1024 + 1)
        if len(payload) > 16 * 1024:
            raise OSError("cleanup evidence exceeds the byte limit")
        return json.loads(payload.decode("utf-8"))
    finally:
        os.close(descriptor)


def _run_posix_gate_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    hard_timeout_seconds: float,
    forward_stdio: bool,
    cancel_file: Path | None,
) -> dict[str, object]:
    """Run a Linux workload inside a PID namespace and require cleanup evidence."""
    started = time.monotonic()
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        return _posix_containment_failure(
            started=started,
            hard_timeout_seconds=hard_timeout_seconds,
            process_id=None,
            error_text="complete POSIX containment requires Linux /proc and PID namespaces",
        )

    supervisor = ROOT / "scripts" / "run_posix_tree_child.py"
    if not supervisor.is_file():
        return _posix_containment_failure(
            started=started,
            hard_timeout_seconds=hard_timeout_seconds,
            process_id=None,
            error_text=f"Linux PID namespace supervisor is missing: {supervisor}",
        )

    with tempfile.TemporaryDirectory(prefix="cf-posix-") as raw:
        control_dir = Path(raw)
        result_file = control_dir / "result.json"
        effective_cancel_file = cancel_file or control_dir / "cancel"
        receipt_nonce = secrets.token_hex(32)
        supervisor_command = [
            sys.executable,
            str(supervisor),
            "--hard-timeout-seconds",
            str(hard_timeout_seconds),
            "--result-file",
            str(result_file),
            "--cancel-file",
            str(effective_cancel_file),
            "--parent-pid",
            str(os.getpid()),
            "--receipt-nonce",
            receipt_nonce,
            "--",
            *command,
        ]
        stdio = {}
        if forward_stdio:
            stdio = {
                "stdin": getattr(sys.stdin, "buffer", sys.stdin),
                "stdout": getattr(sys.stdout, "buffer", sys.stdout),
                "stderr": getattr(sys.stderr, "buffer", sys.stderr),
            }
        try:
            process = subprocess.Popen(
                supervisor_command,
                cwd=str(cwd),
                env=environment,
                **stdio,
                **_gate_process_kwargs(),
            )
        except OSError as error:
            return _posix_containment_failure(
                started=started,
                hard_timeout_seconds=hard_timeout_seconds,
                process_id=None,
                error_text=f"Linux PID namespace supervisor could not start: {error}",
            )

        watchdog_deadline = started + hard_timeout_seconds + POSIX_SUPERVISOR_SETTLE_SECONDS
        while process.poll() is None and time.monotonic() < watchdog_deadline:
            time.sleep(0.02)
        if process.poll() is None:
            termination = _terminate_process_tree(process, process_group_id=process.pid)
            return _posix_containment_failure(
                started=started,
                hard_timeout_seconds=hard_timeout_seconds,
                process_id=process.pid,
                error_text="Linux PID namespace supervisor exceeded its cleanup deadline",
                termination=termination,
            )

        try:
            evidence = _read_posix_supervisor_receipt(result_file)
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
            return _posix_containment_failure(
                started=started,
                hard_timeout_seconds=hard_timeout_seconds,
                process_id=process.pid,
                error_text=f"Linux PID namespace supervisor returned no valid cleanup evidence: {error}",
            )
        if not isinstance(evidence, dict):
            return _posix_containment_failure(
                started=started,
                hard_timeout_seconds=hard_timeout_seconds,
                process_id=process.pid,
                error_text="Linux PID namespace supervisor returned invalid cleanup evidence",
            )

        if evidence.get("receipt_nonce") != receipt_nonce:
            return _posix_containment_failure(
                started=started,
                hard_timeout_seconds=hard_timeout_seconds,
                process_id=process.pid,
                error_text="Linux PID namespace supervisor receipt nonce mismatch",
            )

        evidence = {
            **evidence,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "hard_timeout_seconds": hard_timeout_seconds,
            "windows_job_assigned": False,
        }
        containment_complete = (
            evidence.get("containment_kind") == "linux_pid_namespace"
            and evidence.get("containment_established") is True
            and evidence.get("termination_confirmed") is True
            and evidence.get("residual_process_id") is None
            and evidence.get("termination_error") is None
            and type(evidence.get("termination_requested")) is bool
            and type(evidence.get("termination_escalated")) is bool
            and type(evidence.get("timed_out")) is bool
        )
        if not containment_complete:
            return {
                **evidence,
                "exit_code": POSIX_CONTAINMENT_FAILURE_EXIT_CODE,
                "termination_error": evidence.get("termination_error")
                or "Linux PID namespace containment was not authoritatively confirmed",
            }
        evidence_exit_code = evidence.get("exit_code")
        if not isinstance(evidence_exit_code, int) or isinstance(evidence_exit_code, bool):
            return {
                **evidence,
                "exit_code": POSIX_CONTAINMENT_FAILURE_EXIT_CODE,
                "termination_error": "Linux PID namespace supervisor returned an invalid exit code",
            }
        fields_consistent = (
            (not evidence["termination_escalated"] or evidence["termination_requested"])
            and (
                not evidence["timed_out"]
                or (evidence["termination_requested"] and evidence_exit_code == TIMEOUT_EXIT_CODE)
            )
        )
        if not fields_consistent:
            return {
                **evidence,
                "exit_code": POSIX_CONTAINMENT_FAILURE_EXIT_CODE,
                "termination_error": "Linux PID namespace supervisor returned inconsistent cleanup evidence",
            }
        if (
            not isinstance(process.returncode, int)
            or process.returncode < 0
            or evidence_exit_code != process.returncode
        ):
            return {
                **evidence,
                "exit_code": POSIX_CONTAINMENT_FAILURE_EXIT_CODE,
                "termination_error": "Linux PID namespace supervisor exit code disagreed with cleanup evidence",
            }
        return evidence


def run_gate_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    hard_timeout_seconds: float,
    forward_stdio: bool = False,
    cancel_file: Path | None = None,
    cancel_event: threading.Event | _CancellationSignal | None = None,
) -> dict[str, object]:
    if hard_timeout_seconds <= 0:
        raise ValueError("hard_timeout_seconds must be positive")
    if os.name != "nt":
        return _run_posix_gate_command(
            command,
            cwd=cwd,
            environment=environment,
            hard_timeout_seconds=hard_timeout_seconds,
            forward_stdio=forward_stdio,
            cancel_file=cancel_file,
        )
    started = time.monotonic()
    launch_command = command
    launch_control = None
    if os.name == "nt":
        parent_wait_handle = (
            getattr(cancel_event, "wait_handle", None)
            if cancel_event is not None
            else None
        )
        launch_command, launch_control = _windows_gated_command(
            command,
            parent_wait_handle,
            cancel_file,
        )
    stdio = {}
    if forward_stdio:
        stdio = {
            "stdin": getattr(sys.stdin, "buffer", sys.stdin),
            "stdout": getattr(sys.stdout, "buffer", sys.stdout),
            "stderr": getattr(sys.stderr, "buffer", sys.stderr),
        }
    try:
        process = subprocess.Popen(
            launch_command,
            cwd=str(cwd),
            env=environment,
            **stdio,
            **_gate_process_kwargs(
                list(launch_control["inherit_handles"])
                if launch_control is not None
                else None
            ),
        )
    except BaseException:
        _close_windows_launch_control(launch_control)
        raise
    process_group_id = process.pid if os.name != "nt" else None
    windows_job = _assign_windows_kill_job(process)
    windows_job_assigned = bool(windows_job)
    if os.name == "nt" and not windows_job_assigned:
        # The bootstrap is still waiting on the gate, so terminate it before the
        # real command can observe any authority to run.
        termination = _terminate_process_tree(process, process_group_id=process_group_id)
        control_closed = _close_windows_launch_control(launch_control)
        if not control_closed:
            termination = {
                **termination,
                "termination_confirmed": False,
                "residual_process_id": termination.get("residual_process_id") or process.pid,
                "termination_error": "Windows launch-control handles could not be closed",
            }
        return {
            "exit_code": 126,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "timed_out": False,
            "hard_timeout_seconds": hard_timeout_seconds,
            "windows_job_assigned": False,
            **termination,
        }
    monitor_stop, monitor_thread = _start_windows_cancel_monitor(
        launch_control,
        cancel_event,
        cancel_file,
    )
    timed_out = False
    termination = {
        "termination_requested": False,
        "termination_escalated": False,
        "termination_confirmed": False,
        "residual_process_id": None,
        "termination_error": None,
    }
    canceled = (
        (cancel_event is not None and cancel_event.is_set())
        or (cancel_file is not None and cancel_file.exists())
    )
    if launch_control is not None and not canceled:
        canceled = not _release_windows_gate(
            launch_control,
            cancel_event,
            cancel_file,
        )
    deadline = time.monotonic() + hard_timeout_seconds
    while process.poll() is None and not canceled:
        if (
            (cancel_event is not None and cancel_event.is_set())
            or (cancel_file is not None and cancel_file.exists())
        ):
            canceled = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.02)
    canceled = canceled or (
        (cancel_event is not None and cancel_event.is_set())
        or (cancel_file is not None and cancel_file.exists())
    )
    try:
        if canceled:
            if windows_job:
                termination = _terminate_windows_job(windows_job, process)
            else:
                termination = _terminate_process_tree(process, process_group_id=process_group_id)
            exit_code = 143
        elif process.poll() is not None:
            exit_code = int(process.returncode)
        elif timed_out:
            if windows_job:
                termination = _terminate_windows_job(windows_job, process)
            else:
                termination = _terminate_process_tree(process, process_group_id=process_group_id)
            exit_code = TIMEOUT_EXIT_CODE if timed_out else 143
        else:
            raise RuntimeError('gate process entered an impossible lifecycle state')
    except subprocess.TimeoutExpired:
        timed_out = True
        if windows_job:
            termination = _terminate_windows_job(windows_job, process)
        else:
            termination = _terminate_process_tree(process, process_group_id=process_group_id)
        exit_code = TIMEOUT_EXIT_CODE
    finally:
        monitor_stop.set()
        monitor_thread.join(timeout=1.0)
        if not timed_out and not canceled and windows_job:
            termination = _terminate_windows_job(windows_job, process)
        elif not timed_out and not canceled and os.name != "nt" and process_group_id is not None:
            termination = _terminate_process_tree(process, process_group_id=process_group_id)
        if not _close_windows_job(windows_job):
            termination = {
                **termination,
                "termination_confirmed": False,
                "residual_process_id": termination.get("residual_process_id") or process.pid,
                "termination_error": "Windows Job handle could not be closed",
            }
        if not _close_windows_launch_control(launch_control):
            termination = {
                **termination,
                "termination_confirmed": False,
                "residual_process_id": termination.get("residual_process_id") or process.pid,
                "termination_error": "Windows launch-control handles could not be closed",
            }
    return {
        "exit_code": exit_code,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "timed_out": timed_out,
        "hard_timeout_seconds": hard_timeout_seconds,
        "windows_job_assigned": windows_job_assigned,
        **termination,
    }


def run_fast_gate(
    *,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    hard_timeout_seconds: float = DEFAULT_HARD_TIMEOUT_SECONDS,
) -> dict[str, object]:
    environment = dict(os.environ)
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    # A short prefix avoids MAX_PATH failures in Windows tests with hash-bound
    # nested artifact names.
    with tempfile.TemporaryDirectory(prefix="cf-") as basetemp:
        command = [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            *FAST_NODE_IDS,
            "--basetemp", basetemp,
        ]
        execution = run_gate_command(
            command,
            cwd=ROOT,
            environment=environment,
            hard_timeout_seconds=hard_timeout_seconds,
        )
    return {
        **execution,
        "budget_seconds": budget_seconds,
        "within_budget": float(execution["elapsed_seconds"]) <= budget_seconds,
        "targets": list(FAST_TARGETS),
        "selected_tests": list(FAST_NODE_IDS),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the sub-minute Oracle automation gate.")
    parser.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument(
        "--hard-timeout-seconds",
        type=float,
        default=DEFAULT_HARD_TIMEOUT_SECONDS,
        help="Terminate the pytest process tree and return 124 after this many seconds.",
    )
    parser.add_argument(
        "--enforce-budget",
        action="store_true",
        help="Fail when the gate exceeds its wall-clock budget even if tests pass.",
    )
    args = parser.parse_args(argv)
    result = run_fast_gate(
        budget_seconds=args.budget_seconds,
        hard_timeout_seconds=args.hard_timeout_seconds,
    )
    print(
        f"fast-gate exit={result['exit_code']} "
        f"elapsed={result['elapsed_seconds']}s budget={result['budget_seconds']}s "
        f"within_budget={result['within_budget']} timed_out={result['timed_out']} "
        f"hard_timeout={result['hard_timeout_seconds']}s"
    )
    if result["exit_code"] != 0:
        return int(result["exit_code"])
    if args.enforce_budget and not result["within_budget"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
