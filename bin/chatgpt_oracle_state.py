from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SCHEMA = "codex.chatgpt.oracle-run/v1"
STATE_SCHEMA = "codex.chatgpt.oracle-run-state/v1"
STATUSES = {"prepared", "running", "complete", "failed", "attention_required"}
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102
CREATE_NO_WINDOW = 0x08000000
BLOCKED_OPTIONS = {
    "-f", "--file", "--files", "--path", "--paths", "--include", "-p",
    "--prompt", "--message", "--write-output", "--slug", "-e", "--engine",
    "--mode", "--browser-model-strategy", "--browser-follow-up", "--followup",
    "--dry-run", "--render", "--render-markdown", "--copy",
}
BLOCKED_COMMANDS = {"restart", "session", "status", "serve", "tui"}
SAFE_ORACLE_SWITCHES = {"--no-notify", "--notify", "--no-notify-sound", "--notify-sound", "--verbose"}
SAFE_ORACLE_VALUE_OPTIONS = {"--heartbeat", "--timeout", "--zombie-timeout"}
APP_RE = re.compile(r"^[^\r\n]+$")
MODEL_RE = re.compile(r"^[a-zA-Z0-9._ -]+$")
PARENT_ID_RE = re.compile(r"^[a-f0-9]{32,64}$")
RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{7,95}$")
_THREAD_MUTEXES: dict[str, threading.Lock] = {}
_THREAD_MUTEXES_GUARD = threading.Lock()


class OracleStateError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


@dataclass(frozen=True)
class OracleConfig:
    project_root: Path
    mission_path: Path
    mission_sha256: str
    app_name: str
    mode: str
    run_root: Path
    oracle_command: tuple[str, ...]
    oracle_args: tuple[str, ...]
    submit_mutex_timeout_seconds: float
    model: str
    model_strategy: str
    research: str
    archive: str
    parallel_parent_id: str | None
    requested_run_id: str | None


@dataclass(frozen=True)
class RunLayout:
    run_id: str
    slug: str
    run_dir: Path
    state_path: Path
    output_path: Path
    transcript_path: Path
    stdout_path: Path
    stderr_path: Path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_utf8_strict(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise OracleStateError("UTF8_REQUIRED", "file must be valid UTF-8", {"path": str(path), "offset": exc.start}) from exc
    except OSError as exc:
        raise OracleStateError("FILE_READ_FAILED", "file could not be read", {"path": str(path)}) from exc


def absolute_path(value: Any, *, label: str, must_exist: bool) -> Path:
    raw = Path(str(value or "")).expanduser()
    if not raw.is_absolute():
        raise OracleStateError(f"{label.upper()}_ABSOLUTE_REQUIRED", f"{label} must be an absolute path", {"path": str(raw)})
    try:
        return raw.resolve(strict=must_exist)
    except OSError as exc:
        raise OracleStateError(f"{label.upper()}_INVALID", f"{label} could not be resolved", {"path": str(raw)}) from exc


def is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def oracle_state_root() -> Path:
    override = str(os.environ.get("CODEX_ORACLE_STATE_ROOT") or "").strip()
    return Path(override).expanduser().resolve() if override else (Path.home() / ".codex" / "state" / "chatgpt-oracle").resolve()


def default_oracle_command(platform_name: str | None = None) -> tuple[str, ...]:
    platform = os.name if platform_name is None else platform_name
    return ("npx.cmd" if platform == "nt" else "npx", "-y", "@steipete/oracle")


def validate_oracle_command(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
        raise OracleStateError("ORACLE_COMMAND_INVALID", "oracle_command must be a nonempty list of strings")
    command = tuple(values)
    executable = Path(command[0]).name.casefold()
    if executable in {"oracle", "oracle.cmd", "oracle.exe"} and len(command) == 1:
        return command
    if executable in {"npx", "npx.cmd", "npx.exe"} and command[1:] in {
        ("-y", "@steipete/oracle"),
        ("--yes", "@steipete/oracle"),
        ("@steipete/oracle",),
    }:
        return command
    raise OracleStateError(
        "ORACLE_COMMAND_FORBIDDEN",
        "oracle_command must resolve directly to Oracle or npx @steipete/oracle",
        {"command": command_for_display(command)},
    )


def validate_oracle_args(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
        raise OracleStateError("ORACLE_ARGS_INVALID", "oracle_args must be a list of nonempty strings")
    index = 0
    while index < len(values):
        item = values[index]
        option, separator, inline_value = item.partition("=")
        if option in SAFE_ORACLE_SWITCHES and not separator:
            index += 1
            continue
        if option in SAFE_ORACLE_VALUE_OPTIONS:
            if separator:
                if not inline_value:
                    raise OracleStateError("ORACLE_ARG_VALUE_MISSING", "safe Oracle option requires a value", {"argument": item})
                index += 1
                continue
            if index + 1 >= len(values) or values[index + 1].startswith("-"):
                raise OracleStateError("ORACLE_ARG_VALUE_MISSING", "safe Oracle option requires a value", {"argument": item})
            index += 2
            continue
        raise OracleStateError(
            "ORACLE_ARG_FORBIDDEN",
            "oracle_args accepts only bounded timing, heartbeat, verbosity, and notification options",
            {"argument": item},
        )
    return tuple(values)


def load_manifest(path: Path, *, platform_name: str | None = None) -> OracleConfig:
    manifest_path = absolute_path(path, label="manifest_path", must_exist=True)
    try:
        payload = json.loads(read_utf8_strict(manifest_path))
    except json.JSONDecodeError as exc:
        raise OracleStateError("MANIFEST_JSON_INVALID", "manifest must contain one JSON object", {"line": exc.lineno, "column": exc.colno}) from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise OracleStateError("MANIFEST_SCHEMA_INVALID", f"manifest schema must be {SCHEMA}")
    project_root = absolute_path(payload.get("project_root"), label="project_root", must_exist=True)
    if not project_root.is_dir():
        raise OracleStateError("PROJECT_ROOT_NOT_DIRECTORY", "project_root must identify a directory")
    mission_path = absolute_path(payload.get("mission_path"), label="mission_path", must_exist=True)
    if not mission_path.is_file() or mission_path.is_symlink():
        raise OracleStateError("MISSION_FILE_INVALID", "mission_path must identify a regular non-symlink file")
    if not is_within(project_root, mission_path):
        raise OracleStateError("MISSION_OUTSIDE_PROJECT", "mission_path must stay inside project_root")
    read_utf8_strict(mission_path)
    app_name = str(payload.get("app_name") or "").strip().lstrip("@").strip()
    if not app_name or APP_RE.fullmatch(app_name) is None:
        raise OracleStateError("APP_NAME_INVALID", "app_name must be one nonempty line")
    mode = str(payload.get("mode") or "browser").strip().casefold()
    if mode != "browser":
        raise OracleStateError("MODE_INVALID", "Oracle foundation runner supports mode=browser only")
    state_root = oracle_state_root()
    if is_within(project_root, state_root) or is_within(state_root, project_root):
        raise OracleStateError(
            "HOST_STATE_OVERLAPS_PROJECT",
            "Oracle host state must be disjoint from the DevSpace-writable project",
        )
    project_key = hashlib.sha256(str(project_root).casefold().encode("utf-8")).hexdigest()[:24]
    run_root = absolute_path(payload.get("run_root") or (state_root / "projects" / project_key / "runs"), label="run_root", must_exist=False)
    if not is_within(state_root, run_root):
        raise OracleStateError("RUN_ROOT_OUTSIDE_HOST_STATE", "run_root must stay inside the host-only Oracle state root")
    command_value = payload.get("oracle_command")
    if command_value is None:
        oracle_command = default_oracle_command(platform_name)
    else:
        oracle_command = validate_oracle_command(command_value)
    try:
        timeout = float(payload.get("submit_mutex_timeout_seconds", 30))
    except (TypeError, ValueError) as exc:
        raise OracleStateError("MUTEX_TIMEOUT_INVALID", "submit_mutex_timeout_seconds must be numeric") from exc
    if not 0 < timeout <= 300:
        raise OracleStateError("MUTEX_TIMEOUT_INVALID", "submit_mutex_timeout_seconds must be within 0..300")
    model = str(payload.get("model") or "gpt-5.6").strip()
    if not model or MODEL_RE.fullmatch(model) is None:
        raise OracleStateError("MODEL_INVALID", "model must be one safe Oracle browser model label")
    model_strategy = str(payload.get("model_strategy") or "select").strip().casefold()
    if model_strategy not in {"select", "current", "ignore"}:
        raise OracleStateError("MODEL_STRATEGY_INVALID", "model_strategy must be select, current, or ignore")
    research = str(payload.get("research") or "off").strip().casefold()
    if research not in {"off", "deep"}:
        raise OracleStateError("RESEARCH_INVALID", "research must be off or deep")
    archive = str(payload.get("archive") or "auto").strip().casefold()
    if archive not in {"auto", "always", "never"}:
        raise OracleStateError("ARCHIVE_INVALID", "archive must be auto, always, or never")
    parallel_parent_raw = str(payload.get("parallel_parent_id") or "").strip().casefold()
    parallel_parent_id = parallel_parent_raw or None
    if parallel_parent_id is not None and PARENT_ID_RE.fullmatch(parallel_parent_id) is None:
        raise OracleStateError("PARALLEL_PARENT_ID_INVALID", "parallel_parent_id must be 32-64 lowercase hex characters")
    requested_run_id = str(payload.get("run_id") or "").strip() or None
    if requested_run_id is not None and RUN_ID_RE.fullmatch(requested_run_id) is None:
        raise OracleStateError("RUN_ID_INVALID", "run_id must be a safe 8-96 character identifier")
    return OracleConfig(
        project_root,
        mission_path,
        sha256_file(mission_path),
        app_name,
        mode,
        run_root,
        oracle_command,
        validate_oracle_args(payload.get("oracle_args")),
        timeout,
        model,
        model_strategy,
        research,
        archive,
        parallel_parent_id,
        requested_run_id,
    )


def composer_prompt(config: OracleConfig, mission_path: Path | None = None) -> str:
    effective_path = config.mission_path if mission_path is None else mission_path
    return f"@{config.app_name}\n{effective_path} 파일을 읽고 끝까지 수행하세요."


def create_layout(config: OracleConfig, *, run_id: str | None = None) -> RunLayout:
    actual = run_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    token = re.sub(r"[^a-z0-9]+", "-", config.project_root.name.casefold()).strip("-") or "project"
    slug = f"oracle-{token[:24]}-{actual[-12:]}"
    run_dir = config.run_root / actual
    return RunLayout(actual, slug, run_dir, run_dir / "state.json", run_dir / "output.md", run_dir / "transcript.md", run_dir / "stdout.log", run_dir / "stderr.log")


def state_payload(config: OracleConfig, layout: RunLayout, *, status: str, resolved_version: str, exit_code: int | None = None) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA, "run_id": layout.run_id, "project_root": str(config.project_root),
        "mode": config.mode, "app_name": config.app_name,
        "profile": {
            "model": config.model,
            "model_strategy": config.model_strategy,
            "research": config.research,
            "archive": config.archive,
        },
        "parallel_parent_id": config.parallel_parent_id,
        "mission": {
            "path": str(config.mission_path),
            "transport_path": str(layout.run_dir / "mission.md"),
            "sha256": config.mission_sha256,
        },
        "oracle": {
            "resolved_version": resolved_version,
            "command": list(config.oracle_command),
            "slug": layout.slug,
            "session_locator": layout.slug,
        },
        "artifacts": {"output": str(layout.output_path), "transcript": str(layout.transcript_path), "stdout": str(layout.stdout_path), "stderr": str(layout.stderr_path)},
        "status": status, "exit_code": exit_code,
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(read_utf8_strict(absolute_path(path, label="state_path", must_exist=True)))
    except json.JSONDecodeError as exc:
        raise OracleStateError("STATE_JSON_INVALID", "state file is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA:
        raise OracleStateError("STATE_SCHEMA_INVALID", f"state schema must be {STATE_SCHEMA}")
    return payload


def update_state(state_path: Path, *, status: str, resolved_version: str | None = None, exit_code: int | None = None) -> dict[str, Any]:
    if status not in STATUSES:
        raise OracleStateError("STATUS_INVALID", "invalid Oracle run status")
    payload = load_state(state_path)
    payload["status"] = status
    payload["exit_code"] = exit_code
    if resolved_version is not None:
        payload["oracle"]["resolved_version"] = resolved_version
    write_json_atomic(state_path, payload)
    return payload


def output_is_nonempty(path: Path) -> bool:
    try:
        return bool(path.read_bytes().strip())
    except OSError:
        return False


def write_transcript(layout: RunLayout) -> None:
    chunks = []
    for source in (layout.stdout_path, layout.stderr_path):
        try:
            data = source.read_bytes()
        except OSError:
            data = b""
        if data:
            chunks.append(data.rstrip() + b"\n")
    if layout.output_path.is_file():
        data = layout.output_path.read_bytes()
        if data:
            chunks.append(data.rstrip() + b"\n")
    layout.transcript_path.write_bytes(b"".join(chunks))


def windows_subprocess_kwargs(*, platform_name: str | None = None) -> dict[str, Any]:
    if (os.name if platform_name is None else platform_name) != "nt":
        return {}
    kwargs: dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW)}
    startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_type is not None:
        startupinfo = startupinfo_type()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo
    return kwargs


def mutex_wait_succeeded(wait_result: int) -> bool:
    return wait_result in {WAIT_OBJECT_0, WAIT_ABANDONED}


def submit_mutex_name(project_root: Path) -> str:
    digest = hashlib.sha256(str(project_root).casefold().encode("utf-8")).hexdigest()[:32]
    return f"Local\\codexpro-oracle-submit-{digest}"


class WindowsSubmitMutex(AbstractContextManager["WindowsSubmitMutex"]):
    def __init__(self, name: str, timeout_seconds: float):
        self.name, self.timeout_seconds, self.handle, self.acquired = name, timeout_seconds, None, False

    def __enter__(self) -> "WindowsSubmitMutex":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OracleStateError("SUBMIT_MUTEX_CREATE_FAILED", "Windows submit mutex could not be created")
        self.handle = int(handle)
        result = int(kernel32.WaitForSingleObject(handle, max(1, int(self.timeout_seconds * 1000))))
        if not mutex_wait_succeeded(result):
            kernel32.CloseHandle(handle)
            self.handle = None
            raise OracleStateError("SUBMIT_MUTEX_TIMEOUT" if result == WAIT_TIMEOUT else "SUBMIT_MUTEX_WAIT_FAILED", "project submit mutex could not be acquired")
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if self.acquired:
                kernel32.ReleaseMutex(ctypes.c_void_p(self.handle))
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
        self.handle, self.acquired = None, False
        return None


class ThreadSubmitMutex(AbstractContextManager["ThreadSubmitMutex"]):
    def __init__(self, name: str, timeout_seconds: float):
        self.name, self.timeout_seconds, self.lock = name, timeout_seconds, None

    def __enter__(self) -> "ThreadSubmitMutex":
        with _THREAD_MUTEXES_GUARD:
            lock = _THREAD_MUTEXES.setdefault(self.name, threading.Lock())
        if not lock.acquire(timeout=self.timeout_seconds):
            raise OracleStateError("SUBMIT_MUTEX_TIMEOUT", "project submit mutex could not be acquired")
        self.lock = lock
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.lock is not None:
            self.lock.release()
        self.lock = None
        return None


class FileSubmitMutex(AbstractContextManager["FileSubmitMutex"]):
    def __init__(self, name: str, timeout_seconds: float):
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / f"codexpro-oracle-submit-{digest}.lock"
        self.timeout_seconds = timeout_seconds
        self.handle = None

    def __enter__(self) -> "FileSubmitMutex":
        import fcntl

        self.handle = self.path.open("a+b")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise OracleStateError("SUBMIT_MUTEX_TIMEOUT", "project submit mutex could not be acquired")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is not None:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        self.handle = None
        return None


def project_submit_mutex(
    project_root: Path,
    *,
    timeout_seconds: float,
    platform_name: str | None = None,
) -> AbstractContextManager[Any]:
    name = submit_mutex_name(project_root)
    platform = os.name if platform_name is None else platform_name
    if platform == "nt":
        return WindowsSubmitMutex(name, timeout_seconds)
    return FileSubmitMutex(name, timeout_seconds)


def command_for_display(command: Sequence[str]) -> list[str]:
    return [str(item) for item in command]
