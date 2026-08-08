from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
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
DEVSPACE_APP_NAME = "DevSpace"
ORACLE_ACTIVE_VERSION = "0.17.1"
ORACLE_RECOVERABLE_VERSIONS = ("0.16.1", "0.17.0", ORACLE_ACTIVE_VERSION)
WAIT_CAPABLE_VERSIONS = {"0.17.0", ORACLE_ACTIVE_VERSION}
ORACLE_PACKAGE = "@steipete/oracle"
STATE_SCHEMA = "codex.chatgpt.oracle-run-state/v1"
STATUSES = {"prepared", "running", "complete", "failed", "attention_required", "abandoned"}
# One bounded lifecycle vocabulary.  The stored `status` values above remain the
# on-disk wire format for compatibility, but every consumer and report should
# reason about these four states instead of the historical five statuses times
# five authorities times terminal_harvested combinations.  That combinatorial
# space is what produced "nothing is running yet everything is locked".
LIFECYCLE_STATES = ("running", "complete", "needs_attention", "abandoned")
_STATUS_TO_LIFECYCLE = {
    "prepared": "running",
    "running": "running",
    "complete": "complete",
    "failed": "needs_attention",
    "attention_required": "needs_attention",
    "abandoned": "abandoned",
}
SESSION_AUTHORITY_RANK = {
    "pre_submit": 0,
    "submitted_unknown": 1,
    "live": 2,
    "terminal_observed": 3,
    "terminal": 4,
}
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
SAFE_ORACLE_SWITCHES = {
    "--no-notify",
    "--notify",
    "--no-notify-sound",
    "--notify-sound",
    "--verbose",
    "--browser-hide-window",
}
SAFE_ORACLE_VALUE_OPTIONS = {
    "--heartbeat",
    "--timeout",
    "--zombie-timeout",
    # Tested Oracle versions are compatibility-patched so this is one overall answer
    # budget, including fallback capture.  The host also enforces the same
    # wall-clock deadline with a short grace if CDP evaluation itself wedges.
    "--browser-timeout",
    "--browser-recheck-timeout",
}
# Overall answer budget for a heavy non-Pro run.
DEFAULT_BROWSER_ANSWER_TIMEOUT = "90m"
DEFAULT_BROWSER_ANSWER_CEILING_MINUTES = 90
HOST_WATCHDOG_GRACE_SECONDS = 30
ORACLE_DUPLICATE_PROMPT_RE = re.compile(
    r'A session with the same prompt is already running '
    r'\((?P<locator>oracle-[a-z0-9-]+)\)\.\s*'
    r'Reattach with "oracle session (?P=locator)" or rerun with --force to start another run\.',
    re.IGNORECASE,
)
ORACLE_NO_SESSION_RE = re.compile(
    r"No session found with ID\s+(?P<locator>oracle-[a-z0-9-]+)\.?",
    re.IGNORECASE,
)
ORACLE_PROMPT_NOT_OBSERVED_MARKER = (
    "Prompt did not appear in conversation before timeout (send may have failed)"
)
ORACLE_APP_MENTION_ROUTE_UNCONFIRMED_MARKER = "APP_MENTION_ROUTE_UNCONFIRMED"
ORACLE_THINKING_TIME_UNCONFIRMED_MARKER = (
    "Thinking time: option not found (requested Extra-high); "
    "refusing to submit without confirmed Extra High."
)
ORACLE_PRO_HEAVY_UNCONFIRMED_RE = re.compile(
    r"Thinking time: (?:(?:chip not found|menu not found|option not found|"
    r"selection unverified|model kind not found) for pro \(requested Heavy\)|"
    r"unknown outcome selecting Heavy); refusing to submit without confirmed Pro Heavy\."
)
# The adopted final upstream thinking-time patch (thinkingTime.strict.patch)
# proves the Power-slider effort and refuses pre-send only when the requested
# and required effort labels agree; anything else remains fail-closed.
ORACLE_THINKING_TIME_PRE_SUBMIT_RE = re.compile(
    r"Thinking time: (?:selection unverified \(requested |unknown outcome selecting )"
    r"(?P<requested>[^);]+)\)?; refusing to submit without confirmed (?P<required>[^.]+)\.",
    re.IGNORECASE,
)
ORACLE_MODEL_SWITCHER_PRE_SUBMIT_RE = re.compile(
    r"Unable to find model option matching .+? in the model switcher\."
    r".*?No cookies were applied;",
    re.IGNORECASE | re.DOTALL,
)
ORACLE_COPY_PROFILE_MANUAL_LOGIN_CONFLICT = (
    "--copy-profile cannot be combined with --browser-manual-login: choose either a "
    "throwaway copied profile or the persistent manual-login profile."
)
ORACLE_PROFILE_COPY_RSYNC_MISSING = (
    "--copy-profile requires rsync on PATH (spawn failed): spawn rsync ENOENT"
)
ORACLE_NO_LIVE_TAB_MARKER = "No live ChatGPT tab matched session"
ORACLE_NO_RECOVERABLE_URL_MARKER = (
    "session metadata has no recoverable ChatGPT conversation URL"
)
USER_CONFIRMED_NO_SUBMISSION = "user-confirmed-no-submission"
ORACLE_RECOVERY_STATE_RE = re.compile(r"(?im)^\s*State:\s*[a-z][a-z0-9_-]*\s*$")
ORACLE_PROFILE_COPY_EBUSY_RE = re.compile(
    r"(?im)^(?:ERROR:\s*|User error \(browser-automation\):\s*)?"
    r"EBUSY: resource busy or locked, copyfile ['\"](?P<source>[^'\"]+)['\"] -> ['\"](?P<destination>[^'\"]+)['\"]\s*$"
)
# Upstream Oracle copies a signed-in browser profile with rsync.  On POSIX
# hosts without rsync the copy fails after launch, so feasibility is decided
# while loading the manifest instead of crashing mid-launch.  The pinned
# The versioned `oracle-compat/*/profileCopy.patch` replaces that spawn with Node's
# built-in recursive copy on Windows, so `nt` needs no external dependency.
# Checking PATH there would drop per-run profile isolation and block every
# parallel Web Multi lane, which is the exact failure this guard must avoid.
PROFILE_COPY_DEPENDENCY = "rsync"
PROFILE_COPY_NATIVE_PLATFORMS = ("nt",)


def profile_copy_is_supported(
    *, which_runner: Any = None, platform_name: str | None = None
) -> bool:
    """Report whether Oracle can actually copy a signed-in browser profile."""
    platform = os.name if platform_name is None else platform_name
    if platform in PROFILE_COPY_NATIVE_PLATFORMS:
        return True
    resolver = shutil.which if which_runner is None else which_runner
    return bool(resolver(PROFILE_COPY_DEPENDENCY))
APP_RE = re.compile(r"^[^\r\n]+$")
MODEL_RE = re.compile(r"^[a-zA-Z0-9._ -]+$")
PARENT_ID_RE = re.compile(r"^[a-f0-9]{32,64}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{7,95}$")
WEB_MULTI_CHILD_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[a-f0-9]{12}$")
CHATGPT_CONVERSATION_URL_RE = re.compile(r"https://chatgpt\.com/c/[A-Za-z0-9_-]+", re.IGNORECASE)
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
    app_name: str | None
    mode: str
    transport: str
    attachments: tuple[Path, ...]
    attachment_sha256s: tuple[str, ...]
    project_context_manifest_path: Path | None
    project_context_manifest_sha256: str | None
    run_root: Path
    oracle_command: tuple[str, ...]
    oracle_args: tuple[str, ...]
    submit_mutex_timeout_seconds: float
    model: str
    model_strategy: str
    thinking_time: str
    copy_profile: Path | None
    research: str
    archive: str
    task_outcome_contract: str
    parallel_parent_id: str | None
    requested_run_id: str | None
    manifest_path: Path
    manifest_sha256: str
    expected_manifest_sha256: str | None
    bound_inputs: tuple[Path, ...]
    bound_input_sha256s: tuple[str, ...]
    web_multi_child_provenance_path: Path | None
    web_multi_child_provenance_sha256: str | None


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
    browser_temp_path: Path


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


def exact_regular_file(value: Any, *, label: str) -> Path:
    raw = Path(str(value or "")).expanduser()
    code_prefix = label.upper()
    file_code = "MISSION_FILE_INVALID" if label == "mission_path" else f"{code_prefix}_FILE_INVALID"
    if not raw.is_absolute():
        raise OracleStateError(f"{code_prefix}_ABSOLUTE_REQUIRED", f"{label} must be an absolute path", {"path": str(raw)})
    if raw.is_symlink():
        raise OracleStateError(file_code, f"{label} must not be a symlink", {"path": str(raw)})
    path = absolute_path(raw, label=label, must_exist=True)
    if not path.is_file():
        raise OracleStateError(file_code, f"{label} must identify a regular file", {"path": str(path)})
    return path


def validate_bound_input(value: Any, expected_sha256: Any, project_root: Path) -> tuple[Path, str]:
    raw = Path(str(value or "")).expanduser()
    if not raw.is_absolute():
        raise OracleStateError("BOUND_INPUT_PATH_ABSOLUTE_REQUIRED", "bound input path must be absolute")
    if ".." in raw.parts:
        raise OracleStateError("BOUND_INPUT_PATH_TRAVERSAL", "bound input must not contain parent traversal")
    current = Path(raw.anchor)
    for part in raw.parts[1:]:
        current /= part
        if current.is_symlink():
            raise OracleStateError(
                "BOUND_INPUT_FILE_INVALID",
                "bound input must not use symlink path components",
                {"path": str(current)},
            )
    path = absolute_path(raw, label="bound_input_path", must_exist=True)
    if not path.is_file():
        raise OracleStateError("BOUND_INPUT_FILE_INVALID", "bound input must identify a regular file", {"path": str(path)})
    if not is_within(project_root, path):
        raise OracleStateError("BOUND_INPUT_OUTSIDE_PROJECT", "bound input must stay inside project_root", {"path": str(path)})
    if not isinstance(expected_sha256, str) or SHA256_RE.fullmatch(expected_sha256) is None:
        raise OracleStateError(
            "BOUND_INPUT_SHA256_INVALID",
            "bound input sha256 must be exactly 64 lowercase hexadecimal characters",
            {"path": str(path)},
        )
    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise OracleStateError(
            "BOUND_INPUT_SHA256_MISMATCH",
            "bound input sha256 does not match the current file",
            {"path": str(path), "expected": expected_sha256, "actual": actual_sha256},
        )
    return path, expected_sha256


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
    return pinned_oracle_command(ORACLE_ACTIVE_VERSION, platform_name=platform_name)


def normalize_oracle_version(value: Any) -> str:
    return str(value or "").strip().removeprefix("oracle ").strip()


def pinned_oracle_command(version: str, *, platform_name: str | None = None) -> tuple[str, ...]:
    normalized = normalize_oracle_version(version)
    if normalized not in ORACLE_RECOVERABLE_VERSIONS:
        raise OracleStateError(
            "ORACLE_VERSION_UNVALIDATED",
            "Oracle version is not part of the active or recovery compatibility contract",
            {"version": normalized, "supported": list(ORACLE_RECOVERABLE_VERSIONS)},
        )
    platform = os.name if platform_name is None else platform_name
    return (
        "npx.cmd" if platform == "nt" else "npx",
        "-y",
        f"{ORACLE_PACKAGE}@{normalized}",
    )


def validate_oracle_command(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
        raise OracleStateError("ORACLE_COMMAND_INVALID", "oracle_command must be a nonempty list of strings")
    command = tuple(values)
    executable = Path(command[0]).name.casefold()
    if executable in {"npx", "npx.cmd", "npx.exe"}:
        allowed_specs = {f"{ORACLE_PACKAGE}@{ORACLE_ACTIVE_VERSION}"}
        if command[1:] in {
            *(('-y', spec) for spec in allowed_specs),
            *(('--yes', spec) for spec in allowed_specs),
            *((spec,) for spec in allowed_specs),
        }:
            return command
    raise OracleStateError(
        "ORACLE_COMMAND_FORBIDDEN",
        "oracle_command must use the exact active @steipete/oracle npx version",
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


def load_manifest(
    path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    platform_name: str | None = None,
) -> OracleConfig:
    manifest_path = absolute_path(path, label="manifest_path", must_exist=True)
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise OracleStateError("FILE_READ_FAILED", "file could not be read", {"path": str(manifest_path)}) from exc
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if expected_manifest_sha256 is not None:
        if not isinstance(expected_manifest_sha256, str) or SHA256_RE.fullmatch(expected_manifest_sha256) is None:
            raise OracleStateError(
                "MANIFEST_SHA256_INVALID",
                "expected_manifest_sha256 must be exactly 64 lowercase hexadecimal characters",
            )
        if manifest_sha256 != expected_manifest_sha256:
            raise OracleStateError(
                "MANIFEST_SHA256_MISMATCH",
                "expected_manifest_sha256 does not match the current Oracle manifest",
                {"expected": expected_manifest_sha256, "actual": manifest_sha256},
            )
    try:
        manifest_text = manifest_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise OracleStateError(
            "UTF8_REQUIRED",
            "file must be valid UTF-8",
            {"path": str(manifest_path), "offset": exc.start},
        ) from exc
    try:
        payload = json.loads(manifest_text)
    except json.JSONDecodeError as exc:
        raise OracleStateError("MANIFEST_JSON_INVALID", "manifest must contain one JSON object", {"line": exc.lineno, "column": exc.colno}) from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise OracleStateError("MANIFEST_SCHEMA_INVALID", f"manifest schema must be {SCHEMA}")
    project_root = absolute_path(payload.get("project_root"), label="project_root", must_exist=True)
    if not project_root.is_dir():
        raise OracleStateError("PROJECT_ROOT_NOT_DIRECTORY", "project_root must identify a directory")
    raw_bound_inputs = payload.get("bound_inputs", [])
    if not isinstance(raw_bound_inputs, list):
        raise OracleStateError("BOUND_INPUTS_INVALID", "bound_inputs must be a list")
    validated_bound_inputs: list[tuple[Path, str]] = []
    for index, item in enumerate(raw_bound_inputs):
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise OracleStateError(
                "BOUND_INPUT_INVALID",
                "each bound input must contain only path and sha256",
                {"index": index},
            )
        validated_bound_inputs.append(validate_bound_input(item["path"], item["sha256"], project_root))
    bound_inputs = tuple(item[0] for item in validated_bound_inputs)
    if len(set(bound_inputs)) != len(bound_inputs):
        raise OracleStateError("BOUND_INPUTS_DUPLICATE", "bound input paths must be unique")
    bound_input_sha256s = tuple(item[1] for item in validated_bound_inputs)
    mission_path = exact_regular_file(payload.get("mission_path"), label="mission_path")
    read_utf8_strict(mission_path)
    mission_sha256 = sha256_file(mission_path)
    expected_mission_sha256 = payload.get("mission_sha256")
    if not isinstance(expected_mission_sha256, str) or SHA256_RE.fullmatch(expected_mission_sha256) is None:
        raise OracleStateError(
            "MISSION_SHA256_INVALID",
            "mission_sha256 must be exactly 64 lowercase hexadecimal characters",
        )
    if expected_mission_sha256 != mission_sha256:
        raise OracleStateError(
            "MISSION_SHA256_MISMATCH",
            "mission_sha256 does not match the current mission file",
            {"expected": expected_mission_sha256, "actual": mission_sha256},
        )
    mode = str(payload.get("mode") or "browser").strip().casefold()
    if mode != "browser":
        raise OracleStateError("MODE_INVALID", "Oracle foundation runner supports mode=browser only")
    transport = str(payload.get("transport") or "devspace").strip().casefold()
    if transport not in {"devspace", "pro-attachment-only"}:
        raise OracleStateError("TRANSPORT_INVALID", "transport must be devspace or pro-attachment-only")
    app_name_raw = str(payload.get("app_name") or "").strip().lstrip("@").strip()
    if transport == "devspace":
        if not is_within(project_root, mission_path):
            raise OracleStateError("MISSION_OUTSIDE_PROJECT", "mission_path must stay inside project_root")
        if not app_name_raw or APP_RE.fullmatch(app_name_raw) is None:
            raise OracleStateError("APP_NAME_INVALID", "app_name must be one nonempty line")
        if app_name_raw != DEVSPACE_APP_NAME:
            raise OracleStateError(
                "DEVSPACE_APP_REQUIRED",
                f"new non-Pro Oracle runs require the exact app name {DEVSPACE_APP_NAME}",
                {"app_name": app_name_raw},
            )
        app_name: str | None = app_name_raw
        if payload.get("attachments"):
            raise OracleStateError("REGULAR_ATTACHMENTS_FORBIDDEN", "DevSpace runs must not attach files")
        if payload.get("project_context_manifest_path") not in {None, ""}:
            raise OracleStateError(
                "CONTEXT_MANIFEST_FORBIDDEN",
                "project_context_manifest_path is only valid for Pro attachment-only runs",
            )
        if "attachment_sha256s" in payload:
            raise OracleStateError(
                "ATTACHMENT_SHA256S_FORBIDDEN",
                "attachment_sha256s is only valid for Pro attachment-only runs",
            )
        if "project_context_manifest_sha256" in payload:
            raise OracleStateError(
                "CONTEXT_MANIFEST_SHA256_FORBIDDEN",
                "project_context_manifest_sha256 is only valid for Pro attachment-only runs",
            )
        attachments: tuple[Path, ...] = ()
        attachment_sha256s: tuple[str, ...] = ()
        project_context_manifest_path: Path | None = None
        project_context_manifest_sha256: str | None = None
    else:
        if app_name_raw:
            raise OracleStateError("PRO_APP_FORBIDDEN", "Pro attachment-only runs must not name an app")
        app_name = None
        raw_attachments = payload.get("attachments")
        if not isinstance(raw_attachments, list) or not raw_attachments:
            raise OracleStateError("PRO_ATTACHMENTS_REQUIRED", "Pro requires one or more exact attachment files")
        attachments = tuple(
            exact_regular_file(value, label=f"attachment_{index}")
            for index, value in enumerate(raw_attachments)
        )
        if len(set(attachments)) != len(attachments):
            raise OracleStateError("PRO_ATTACHMENTS_DUPLICATE", "Pro attachment paths must be unique")
        if mission_path not in attachments:
            raise OracleStateError("PRO_MISSION_ATTACHMENT_REQUIRED", "mission_path must be one of the Pro attachments")
        raw_attachment_sha256s = payload.get("attachment_sha256s")
        if (
            not isinstance(raw_attachment_sha256s, list)
            or len(raw_attachment_sha256s) != len(attachments)
            or not all(isinstance(value, str) and SHA256_RE.fullmatch(value) is not None for value in raw_attachment_sha256s)
        ):
            raise OracleStateError(
                "PRO_ATTACHMENT_SHA256S_INVALID",
                "Pro attachment_sha256s must be an ordered lowercase SHA-256 list aligned with attachments",
            )
        attachment_sha256s = tuple(raw_attachment_sha256s)
        for attachment, expected in zip(attachments, attachment_sha256s, strict=True):
            actual = sha256_file(attachment)
            if actual != expected:
                raise OracleStateError(
                    "PRO_ATTACHMENT_SHA256_MISMATCH",
                    "declared Pro attachment sha256 does not match the current file",
                    {"path": str(attachment), "expected": expected, "actual": actual},
                )
        project_context_manifest_path = exact_regular_file(
            payload.get("project_context_manifest_path"),
            label="project_context_manifest_path",
        )
        if not is_within(project_root, project_context_manifest_path):
            raise OracleStateError(
                "PRO_CONTEXT_MANIFEST_OUTSIDE_PROJECT",
                "project_context_manifest_path must stay inside project_root",
            )
        raw_context_manifest_sha256 = payload.get("project_context_manifest_sha256")
        if not isinstance(raw_context_manifest_sha256, str) or SHA256_RE.fullmatch(raw_context_manifest_sha256) is None:
            raise OracleStateError(
                "PRO_CONTEXT_MANIFEST_SHA256_INVALID",
                "project_context_manifest_sha256 must be exactly 64 lowercase hexadecimal characters",
            )
        actual_context_manifest_sha256 = sha256_file(project_context_manifest_path)
        if actual_context_manifest_sha256 != raw_context_manifest_sha256:
            raise OracleStateError(
                "PRO_CONTEXT_MANIFEST_SHA256_MISMATCH",
                "declared Pro context manifest sha256 does not match the current file",
                {
                    "expected": raw_context_manifest_sha256,
                    "actual": actual_context_manifest_sha256,
                },
            )
        project_context_manifest_sha256 = raw_context_manifest_sha256
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
    thinking_time = str(payload.get("thinking_time") or "extra-high").strip().casefold()
    if thinking_time not in {"light", "standard", "extended", "heavy", "extra-high"}:
        raise OracleStateError(
            "THINKING_TIME_INVALID",
            "thinking_time must be light, standard, extended, heavy, or extra-high",
        )
    if transport == "pro-attachment-only":
        if model.casefold() != "gpt-5.6-sol":
            raise OracleStateError(
                "PRO_MODEL_INVALID",
                "Pro attachment-only runs require GPT-5.6 Sol with an explicitly verified Pro effort; no downgrade is allowed",
                {"model": model},
            )
        if model_strategy != "select":
            raise OracleStateError("PRO_MODEL_STRATEGY_INVALID", "Pro requires explicit model selection")
        if thinking_time != "heavy":
            raise OracleStateError("PRO_THINKING_TIME_INVALID", "Pro requires heavy reasoning")
    copy_profile_raw = str(payload.get("copy_profile") or "").strip()
    if copy_profile_raw:
        copy_profile = absolute_path(copy_profile_raw, label="copy_profile", must_exist=True)
    else:
        # The manually signed-in Oracle profile is the immutable seed for a
        # throwaway per-run copy.  This prevents different projects from
        # sharing one Chrome process and closing each other's live work.
        profile_override = str(os.environ.get("ORACLE_BROWSER_PROFILE_DIR") or "").strip()
        default_profile = Path(profile_override).expanduser().resolve() if profile_override else (
            Path.home() / ".oracle" / "browser-profile"
        ).resolve()
        copy_profile = default_profile if default_profile.is_dir() else None
    if copy_profile is not None:
        if not copy_profile.is_dir():
            raise OracleStateError("COPY_PROFILE_NOT_DIRECTORY", "copy_profile must identify a directory")
        if is_within(project_root, copy_profile) or is_within(copy_profile, project_root):
            raise OracleStateError("COPY_PROFILE_OVERLAPS_PROJECT", "copy_profile must be outside the DevSpace project")
        if not profile_copy_is_supported(platform_name=platform_name):
            # Without the copy dependency Oracle aborts after launch, so every
            # run failed before reaching the composer.  Fall back to the
            # signed-in profile directly instead of forcing that failure.
            if copy_profile_raw:
                raise OracleStateError(
                    "COPY_PROFILE_DEPENDENCY_MISSING",
                    f"copy_profile requires {PROFILE_COPY_DEPENDENCY} on PATH; "
                    "install it or omit copy_profile to reuse the signed-in profile",
                    {"dependency": PROFILE_COPY_DEPENDENCY, "copy_profile": str(copy_profile)},
                )
            copy_profile = None
    research = str(payload.get("research") or "off").strip().casefold()
    if research not in {"off", "deep"}:
        raise OracleStateError("RESEARCH_INVALID", "research must be off or deep")
    if transport == "pro-attachment-only" and research != "off":
        raise OracleStateError("PRO_RESEARCH_FORBIDDEN", "Pro attachment-only runs do not enable research mode")
    archive = str(payload.get("archive") or "auto").strip().casefold()
    if archive not in {"auto", "always", "never"}:
        raise OracleStateError("ARCHIVE_INVALID", "archive must be auto, always, or never")
    task_outcome_contract = str(payload.get("task_outcome_contract") or "legacy").strip().casefold()
    if task_outcome_contract not in {"legacy", "v1"}:
        raise OracleStateError(
            "TASK_OUTCOME_CONTRACT_INVALID",
            "task_outcome_contract must be legacy or v1",
        )
    if transport == "pro-attachment-only" and task_outcome_contract != "legacy":
        raise OracleStateError(
            "PRO_TASK_OUTCOME_CONTRACT_FORBIDDEN",
            "Pro attachment-only output is not wrapped in the DevSpace task outcome contract",
        )
    parallel_parent_raw = str(payload.get("parallel_parent_id") or "").strip().casefold()
    parallel_parent_id = parallel_parent_raw or None
    if parallel_parent_id is not None and PARENT_ID_RE.fullmatch(parallel_parent_id) is None:
        raise OracleStateError("PARALLEL_PARENT_ID_INVALID", "parallel_parent_id must be 32-64 lowercase hex characters")
    requested_run_id = str(payload.get("run_id") or "").strip() or None
    if requested_run_id is not None and RUN_ID_RE.fullmatch(requested_run_id) is None:
        raise OracleStateError("RUN_ID_INVALID", "run_id must be a safe 8-96 character identifier")
    provenance_raw = payload.get("web_multi_child_provenance_path")
    provenance_path = exact_regular_file(provenance_raw, label="web_multi_child_provenance_path") if provenance_raw else None
    provenance_sha256 = sha256_file(provenance_path) if provenance_path else None
    return OracleConfig(
        project_root,
        mission_path,
        mission_sha256,
        app_name,
        mode,
        transport,
        attachments,
        attachment_sha256s,
        project_context_manifest_path,
        project_context_manifest_sha256,
        run_root,
        oracle_command,
        validate_oracle_args(payload.get("oracle_args")),
        timeout,
        model,
        model_strategy,
        thinking_time,
        copy_profile,
        research,
        archive,
        task_outcome_contract,
        parallel_parent_id,
        requested_run_id,
        manifest_path,
        manifest_sha256,
        expected_manifest_sha256,
        bound_inputs,
        bound_input_sha256s,
        provenance_path,
        provenance_sha256,
    )


def composer_prompt(config: OracleConfig, mission_path: Path | None = None) -> str:
    if config.transport == "pro-attachment-only":
        identity_material = "\0".join((
            str(config.project_root).casefold(),
            config.mission_sha256,
            config.project_context_manifest_sha256 or "",
            *config.attachment_sha256s,
        ))
        identity = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:24]
        return (
            "Read the attached prompt/instructions and all attached files, then complete the task. "
            f"Task identity: oracle-pro-{identity}."
        )
    effective_path = config.mission_path if mission_path is None else mission_path
    # Single composer authority for regular GPT-5.6 Sol runs: exactly the app
    # mention plus the absolute UTF-8 mission path, with no task body and no
    # operational prose.  Keep the Windows npx.cmd prompt in one argument
    # line: a literal newline truncates the prompt after the app mention
    # before Oracle receives it.
    return f"@{config.app_name} {effective_path}"


def create_layout(config: OracleConfig, *, run_id: str | None = None) -> RunLayout:
    actual = run_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    project_words = (re.findall(r"[a-z0-9]+", config.project_root.name.casefold()) or ["project"])[:3]
    project_token = "-".join(word[:10] for word in project_words)
    # Oracle accepts 3-5 words and normalizes every word to its first ten
    # characters. Generate that exact locator up front so recovery never
    # stores an alias that Oracle cannot resolve later.
    run_token = actual.rsplit("-", 1)[-1][:10]
    slug = f"oracle-{project_token}-{run_token}"
    run_dir = config.run_root / actual
    return RunLayout(
        actual,
        slug,
        run_dir,
        run_dir / "state.json",
        run_dir / "output.md",
        run_dir / "transcript.md",
        run_dir / "stdout.log",
        run_dir / "stderr.log",
        run_dir / "browser-temp",
    )


def state_payload(config: OracleConfig, layout: RunLayout, *, status: str, resolved_version: str, exit_code: int | None = None) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA, "run_id": layout.run_id, "project_root": str(config.project_root),
        "mode": config.mode, "transport": config.transport, "app_name": config.app_name,
        "profile": {
            "model": config.model,
            "model_strategy": config.model_strategy,
            "thinking_time": config.thinking_time,
            "copy_profile": str(config.copy_profile) if config.copy_profile else None,
            "research": config.research,
            "archive": config.archive,
        },
        "parallel_parent_id": config.parallel_parent_id,
        "manifest": {
            "path": str(config.manifest_path),
            "actual_sha256": config.manifest_sha256,
            "expected_sha256": config.expected_manifest_sha256,
        },
        "requested_run_id": config.requested_run_id,
        "web_multi_child_provenance": (
            {"path": str(config.web_multi_child_provenance_path), "sha256": config.web_multi_child_provenance_sha256}
            if config.web_multi_child_provenance_path else None
        ),
        "transport_status": "prepared",
        "task_outcome_contract": config.task_outcome_contract,
        "task_outcome": "not_applicable" if config.transport == "pro-attachment-only" else "pending",
        "task_outcome_reason": None,
        "mission": {
            "path": str(config.mission_path),
            "transport_path": str(layout.run_dir / "mission.md"),
            "sha256": config.mission_sha256,
        },
        "attachments": [
            {"path": str(path), "sha256": digest, "size_bytes": path.stat().st_size}
            for path, digest in zip(config.attachments, config.attachment_sha256s, strict=True)
        ],
        "bound_inputs": [
            {"path": str(path), "sha256": digest}
            for path, digest in zip(config.bound_inputs, config.bound_input_sha256s, strict=True)
        ],
        "project_context_manifest": (
            {
                "path": str(config.project_context_manifest_path),
                "sha256": config.project_context_manifest_sha256,
            }
            if config.project_context_manifest_path is not None
            else None
        ),
        "oracle": {
            "resolved_version": resolved_version,
            "command": list(config.oracle_command),
            "slug": layout.slug,
            "session_locator": layout.slug,
        },
        "artifacts": {
            "output": str(layout.output_path),
            "transcript": str(layout.transcript_path),
            "stdout": str(layout.stdout_path),
            "stderr": str(layout.stderr_path),
            "browser_temp": str(layout.browser_temp_path),
        },
        "status": status,
        "exit_code": exit_code,
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "artifact_sha256": None,
    }


def host_uptime_ms(*, platform_name: str | None = None) -> int:
    platform = os.name if platform_name is None else platform_name
    if platform == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return int(kernel32.GetTickCount64())
    return int(time.monotonic() * 1000)


def browser_temp_environment(
    browser_temp_path: Path,
    *,
    platform_name: str | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    root = browser_temp_path.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = {
        "schema": "codex.chatgpt.oracle-browser-temp-owner/v1",
        "controller_pid": os.getpid(),
        "host_uptime_ms": host_uptime_ms(platform_name=platform_name),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json_atomic(root / ".owner.json", marker)
    env = dict(os.environ if base_env is None else base_env)
    value = str(root)
    env.update({"TEMP": value, "TMP": value, "TMPDIR": value})
    return env


def cleanup_owned_browser_temp(browser_temp_path: Path) -> bool:
    root = browser_temp_path.expanduser().resolve()
    if not root.exists():
        return True
    marker = root / ".owner.json"
    if not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if payload.get("schema") != "codex.chatgpt.oracle-browser-temp-owner/v1":
        return False
    try:
        shutil.rmtree(root)
    except OSError:
        return False
    return not root.exists()


def cleanup_prior_boot_browser_temps(
    run_root: Path,
    *,
    platform_name: str | None = None,
    current_uptime_ms: int | None = None,
) -> list[str]:
    root = run_root.expanduser().resolve()
    if not root.is_dir():
        return []
    now_uptime = host_uptime_ms(platform_name=platform_name) if current_uptime_ms is None else int(current_uptime_ms)
    cleaned: list[str] = []
    for run_dir in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name):
        browser_temp = run_dir / "browser-temp"
        marker = browser_temp / ".owner.json"
        if not marker.is_file():
            continue
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            owner_uptime = int(payload["host_uptime_ms"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        # GetTickCount/monotonic reset on reboot. Only a prior-boot owner is
        # eligible here; same-boot crashes remain preserved for exact recovery.
        if now_uptime >= owner_uptime:
            continue
        if cleanup_owned_browser_temp(browser_temp):
            cleaned.append(str(browser_temp))
    return cleaned


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


def update_state(
    state_path: Path,
    *,
    status: str,
    resolved_version: str | None = None,
    exit_code: int | None = None,
    session_authority: str | None = None,
    terminal_harvested: bool | None = None,
    artifact_sha256: str | None = None,
    transport_status: str | None = None,
    task_outcome: str | None = None,
    task_outcome_reason: str | None = None,
    host_watchdog: dict[str, Any] | None = None,
    submission_readiness: dict[str, Any] | None = None,
    conversation_url: str | None = None,
    conversation_url_conflict: dict[str, str] | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise OracleStateError("STATUS_INVALID", "invalid Oracle run status")
    payload = load_state(state_path)
    payload["status"] = status
    payload["exit_code"] = exit_code
    if resolved_version is not None:
        payload["oracle"]["resolved_version"] = resolved_version
    if session_authority is not None:
        current_authority = str(payload.get("session_authority") or "")
        current_rank = SESSION_AUTHORITY_RANK.get(current_authority, -1)
        requested_rank = SESSION_AUTHORITY_RANK.get(session_authority, -1)
        # Exact session authority is monotonic: a persisted terminal_observed
        # may not regress to live, and a durable harvested terminal is final.
        # Observer disagreement stays attention-required under the same lock
        # until a later exact terminal harvest produces fresh output.
        payload["session_authority"] = (
            current_authority if current_rank > requested_rank else session_authority
        )
        if current_rank > requested_rank and status == "running":
            payload["status"] = (
                "complete"
                if current_authority == "terminal" and payload.get("terminal_harvested") is True
                else "attention_required"
            )
    if terminal_harvested is not None:
        payload["terminal_harvested"] = terminal_harvested
    if artifact_sha256 is not None:
        payload["artifact_sha256"] = artifact_sha256
    if transport_status is not None:
        payload["transport_status"] = transport_status
    if task_outcome is not None:
        payload["task_outcome"] = task_outcome
    if task_outcome_reason is not None:
        payload["task_outcome_reason"] = task_outcome_reason
    if host_watchdog is not None:
        payload["host_watchdog"] = host_watchdog
    if submission_readiness is not None:
        payload["submission_readiness"] = submission_readiness
    if conversation_url is not None:
        oracle = payload.get("oracle") if isinstance(payload.get("oracle"), dict) else {}
        existing_url = str(oracle.get("conversation_url") or "").strip()
        if existing_url and existing_url != conversation_url:
            payload["conversation_url_conflict"] = {
                "persisted": existing_url,
                "observed": conversation_url,
            }
        else:
            payload["oracle"] = {**oracle, "conversation_url": conversation_url}
    if conversation_url_conflict is not None:
        payload["conversation_url_conflict"] = dict(conversation_url_conflict)
    write_json_atomic(state_path, payload)
    return payload


def output_is_nonempty(path: Path) -> bool:
    try:
        return bool(path.read_bytes().strip())
    except OSError:
        return False


def _state_has_conversation_url(state: dict[str, Any]) -> bool:
    """Recognize only explicit persisted conversation URL fields."""
    url_keys = {"conversation_url", "conversationUrl", "canonical_url", "canonicalUrl"}

    def walk(value: Any) -> bool:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in url_keys and str(nested or "").strip():
                    return True
                if isinstance(nested, (dict, list)) and walk(nested):
                    return True
        elif isinstance(value, list):
            return any(walk(item) for item in value)
        return False

    return walk(state)


def _artifact_bytes(state: dict[str, Any], name: str) -> tuple[Path, bytes] | None:
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    raw = str(artifacts.get(name) or "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        return path, path.read_bytes()
    except OSError:
        return None


def _comprehensive_no_submission_evidence(state_path: Path) -> dict[str, Any] | None:
    """Return exact evidence for a user-adjudicable Oracle composer timeout.

    The accepted messages do not release ownership on their own.  This helper
    only proves that the run is eligible for an explicit user adjudication: no
    output or conversation URL exists, Oracle reported an eligible composer
    failure, and exact recovery has neither a live tab nor a saved URL.
    """
    state = load_state(state_path)
    authority = str(state.get("session_authority") or "")
    if authority not in {"submitted_unknown", "pre_submit"}:
        return None
    if state.get("terminal_harvested") is True or _state_has_conversation_url(state):
        return None
    run_dir = state_path.parent
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    if output.resolve() != (run_dir / "output.md").resolve() or output.is_symlink():
        return None
    if str(output) and output_is_nonempty(output):
        return None
    stdout_record = _artifact_bytes(state, "stdout")
    stderr_record = _artifact_bytes(state, "stderr")
    if stdout_record is None or stderr_record is None:
        return None
    stdout_path, stdout_bytes = stdout_record
    stderr_path, stderr_bytes = stderr_record
    if (
        stdout_path.resolve() != (run_dir / "stdout.log").resolve()
        or stderr_path.resolve() != (run_dir / "stderr.log").resolve()
        or stdout_path.is_symlink()
        or stderr_path.is_symlink()
    ):
        return None
    try:
        stdout_text = stdout_bytes.decode("utf-8", errors="strict")
        stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    locator = str(oracle.get("session_locator") or oracle.get("slug") or "").strip()
    stdout_lines = {line.strip() for line in stdout_text.splitlines()}
    app_route_unconfirmed = (
        str(state.get("transport") or "").casefold() == "devspace"
        and str(state.get("app_name") or "").casefold() == "devspace"
        and normalize_oracle_version(oracle.get("resolved_version")) == ORACLE_ACTIVE_VERSION
        and {
            f"ERROR: {ORACLE_APP_MENTION_ROUTE_UNCONFIRMED_MARKER}",
            (
                "User error (browser-automation): "
                f"{ORACLE_APP_MENTION_ROUTE_UNCONFIRMED_MARKER}"
            ),
        }.issubset(stdout_lines)
    )
    if not locator or not (
        ORACLE_PROMPT_NOT_OBSERVED_MARKER in stdout_text or app_route_unconfirmed
    ):
        return None
    if f"Session: {locator}" not in stdout_text:
        return None
    mission = state.get("mission") if isinstance(state.get("mission"), dict) else {}
    transport_path = Path(str(mission.get("transport_path") or ""))
    if transport_path.resolve() != (run_dir / "mission.md").resolve() or transport_path.is_symlink():
        return None
    try:
        mission_bytes = transport_path.read_bytes()
        mission_text = mission_bytes.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    mission_sha256 = hashlib.sha256(mission_bytes).hexdigest()
    if mission_sha256 != str(mission.get("sha256") or ""):
        return None
    host_marker = "[HOST_STAGE_CONTRACT]"
    workspace_marker = "[DEVSPACE_WORKSPACE_ENTRY_CONTRACT]"
    if mission_text.count(host_marker) != 1 or mission_text.count(workspace_marker) != 1:
        return None
    host_start = mission_text.index(host_marker) + len(host_marker)
    workspace_start = mission_text.index(workspace_marker)
    if workspace_start <= host_start:
        return None
    host_contract = mission_text[host_start:workspace_start]
    binding: dict[str, str] = {}
    for key, pattern in {
        "workflow_id": (
            r"(?m)^workflow_id=((?:[a-f0-9]{32,64}|"
            r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}))\r?$"
        ),
        "stage": r"(?m)^stage=([a-z][a-z0-9-]*)\r?$",
        "attempt_id": r"(?m)^attempt_id=([a-f0-9]{32,64})\r?$",
        "input_mission_sha256": r"(?m)^input_mission_sha256=([a-f0-9]{64})\r?$",
    }.items():
        matches = re.findall(pattern, host_contract)
        if len(matches) != 1:
            return None
        binding[key] = matches[0]
    if binding["attempt_id"] != str(state.get("run_id") or ""):
        return None
    expected_parent = hashlib.sha256(binding["workflow_id"].encode("utf-8")).hexdigest()
    if str(state.get("parallel_parent_id") or "") != expected_parent:
        return None
    contract_paths: dict[str, str] = {}
    for key, pattern in {
        "project_root": r"(?m)^exact_project_root=([^\r\n]+)\r?$",
        "input_mission": r"(?m)^exact_input_mission_path=([^\r\n]+)\r?$",
        "receipt": r"(?m)^Write the small UTF-8 stage receipt to: ([^\r\n]+)\r?$",
    }.items():
        matches = re.findall(pattern, host_contract)
        if len(matches) != 1:
            return None
        contract_paths[key] = matches[0]
    try:
        project_root = Path(str(state.get("project_root") or ""))
        contract_project_root = Path(contract_paths["project_root"])
        if (
            not project_root.is_absolute()
            or not contract_project_root.is_absolute()
            or project_root.resolve(strict=True) != contract_project_root.resolve(strict=True)
            or not project_root.resolve(strict=True).is_dir()
        ):
            return None
        canonical_root = project_root.resolve(strict=True)
        source_mission = Path(str(mission.get("path") or ""))
        input_mission = Path(contract_paths["input_mission"])
        receipt_path = Path(contract_paths["receipt"])
        if (
            not source_mission.is_absolute()
            or source_mission.is_symlink()
            or not input_mission.is_absolute()
            or input_mission.is_symlink()
            or not receipt_path.is_absolute()
            or receipt_path.is_symlink()
        ):
            return None
        source_mission = source_mission.resolve(strict=True)
        input_mission = input_mission.resolve(strict=True)
        receipt_path = receipt_path.resolve(strict=False)
        if (
            not source_mission.is_file()
            or not input_mission.is_file()
            or not is_within(canonical_root, source_mission)
            or not is_within(canonical_root, input_mission)
            or not is_within(canonical_root, receipt_path)
            or receipt_path != source_mission.parent / "stage-result.json"
            or source_mission.read_bytes() != mission_bytes
            or sha256_file(input_mission) != binding["input_mission_sha256"]
        ):
            return None
    except OSError:
        return None
    recovery_records: list[dict[str, str]] = []
    for recovery_stdout in sorted(run_dir.glob("recovery-*-stdout.log"), key=lambda item: item.name):
        recovery_stderr = recovery_stdout.with_name(
            recovery_stdout.name.replace("-stdout.log", "-stderr.log")
        )
        try:
            if recovery_stdout.is_symlink() or recovery_stderr.is_symlink():
                continue
            recovery_stdout_bytes = recovery_stdout.read_bytes()
            recovery_stderr_bytes = recovery_stderr.read_bytes()
            combined = b"\n".join((recovery_stdout_bytes, recovery_stderr_bytes))
            recovery_text = combined.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            return None
        if ORACLE_RECOVERY_STATE_RE.search(recovery_text):
            return None
        if (
            ORACLE_NO_LIVE_TAB_MARKER not in recovery_text
            or f'"{locator}"' not in recovery_text
            or ORACLE_NO_RECOVERABLE_URL_MARKER not in recovery_text
        ):
            return None
        recovery_records.append({
            "stdout_name": recovery_stdout.name,
            "stdout_sha256": hashlib.sha256(recovery_stdout_bytes).hexdigest(),
            "stderr_name": recovery_stderr.name,
            "stderr_sha256": hashlib.sha256(recovery_stderr_bytes).hexdigest(),
        })
    if not recovery_records:
        return None
    return {
        "project_root": str(state.get("project_root") or ""),
        "run_id": str(state.get("run_id") or ""),
        **binding,
        "mission_sha256": mission_sha256,
        "oracle_locator": locator,
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "recovery_evidence": recovery_records,
        "output_absent": True,
        "conversation_url_absent": True,
        "_task_outcome_reason": (
            "user-confirmed-no-submission-after-app-route-unconfirmed"
            if app_route_unconfirmed
            else "user-confirmed-no-submission-after-prompt-timeout"
        ),
        "_augmented_mission_path": str(source_mission),
        "_input_mission_path": str(input_mission),
        "_receipt_path": str(receipt_path),
    }


def _web_multi_child_provenance(
    state: dict[str, Any], run_dir: Path, project_root: Path, source_path: Path, parent_id: str, locator: str,
) -> dict[str, Any] | None:
    """Validate new provenance, or the exact legacy result/lane pair when present."""
    raw = state.get("web_multi_child_provenance")
    candidates: list[tuple[Path, dict[str, Any] | None, Path | None]] = []
    if isinstance(raw, dict):
        path = Path(str(raw.get("path") or ""))
        try:
            if not path.is_absolute() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != str(raw.get("sha256") or ""):
                return None
            candidates.append((path, json.loads(path.read_text(encoding="utf-8", errors="strict")), None))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
    else:
        # Legacy Oracle Multi did not copy lane provenance into state.  Its
        # run-owned result entry and lane manifest are sufficient only when
        # they identify this exact run directory and Oracle locator.
        for result_path in project_root.glob("runtime/*/oracle_output/result.json"):
            try:
                result = json.loads(result_path.read_text(encoding="utf-8", errors="strict"))
                lanes = result.get("lanes") if isinstance(result.get("lanes"), list) else []
                matching = [lane for lane in lanes if isinstance(lane, dict) and Path(str(lane.get("run_dir") or "")).resolve() == run_dir and str(lane.get("session_locator") or "") == locator]
                if result.get("schema") != "codex.chatgpt.oracle-multi-result/v1" or str(result.get("parent_id") or "") != parent_id or len(matching) != 1:
                    continue
                lane_id = str(matching[0].get("id") or "")
                lane_manifest = result_path.parent / "lanes" / lane_id / "oracle.json"
                candidates.append((lane_manifest, json.loads(lane_manifest.read_text(encoding="utf-8", errors="strict")), result_path))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return None
    if len(candidates) != 1:
        return None
    path, value, legacy_result_path = candidates[0]
    if not isinstance(value, dict):
        return None
    if value.get("schema") == "codex.chatgpt.oracle-multi-child-provenance/v1":
        parent_manifest = Path(str(value.get("parent_manifest_path") or ""))
        try:
            if hashlib.sha256(parent_manifest.read_bytes()).hexdigest() != str(value.get("parent_manifest_sha256") or ""):
                return None
            parent = json.loads(parent_manifest.read_text(encoding="utf-8", errors="strict"))
            lanes = parent.get("solvers") if isinstance(parent.get("solvers"), list) else []
            lane = next((item for item in lanes if isinstance(item, dict) and str(item.get("id") or "") == str(value.get("lane_id") or "")), None)
            if not isinstance(lane, dict) or parent.get("schema") != "codex.chatgpt.oracle-multi/v1":
                return None
            if Path(str(parent.get("project_root") or "")).resolve() != project_root or Path(str(lane.get("mission_path") or "")).resolve() != source_path:
                return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
    else:
        lane = value
    if str(value.get("parent_id") or value.get("parallel_parent_id") or "") != parent_id:
        return None
    if Path(str(value.get("project_root") or "")).resolve() != project_root or Path(str(value.get("mission_path") or "")).resolve() != source_path:
        return None
    if value.get("schema") == "codex.chatgpt.oracle-multi-child-provenance/v1":
        return {
            "provenance_mode": "new-child-provenance/v1",
            "child_provenance_path": str(path.resolve()), "child_provenance_sha256": sha256_file(path),
            "parent_manifest_path": str(parent_manifest.resolve()), "parent_manifest_sha256": sha256_file(parent_manifest),
        }
    if legacy_result_path is None:
        return None
    return {
        "provenance_mode": "legacy-result-lane/v1",
        "legacy_result_path": str(legacy_result_path.resolve()), "legacy_result_sha256": sha256_file(legacy_result_path),
        "legacy_lane_manifest_path": str(path.resolve()), "legacy_lane_manifest_sha256": sha256_file(path),
    }


def _web_multi_child_no_submission_evidence(state_path: Path) -> dict[str, Any] | None:
    """Return fail-closed settlement evidence for a direct Oracle Multi child."""
    state = load_state(state_path)
    if str(state.get("session_authority") or "") not in {"submitted_unknown", "pre_submit"}:
        return None
    parent_id = str(state.get("parallel_parent_id") or "").strip().casefold()
    run_id = str(state.get("run_id") or "")
    if PARENT_ID_RE.fullmatch(parent_id) is None or WEB_MULTI_CHILD_RUN_ID_RE.fullmatch(run_id) is None or state.get("requested_run_id") is not None:
        return None
    if state.get("terminal_harvested") is True or _state_has_conversation_url(state):
        return None
    run_dir = state_path.parent
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    if output.resolve() != (run_dir / "output.md").resolve() or output.is_symlink() or output_is_nonempty(output):
        return None
    stdout_record = _artifact_bytes(state, "stdout")
    stderr_record = _artifact_bytes(state, "stderr")
    if stdout_record is None or stderr_record is None:
        return None
    stdout_path, stdout_bytes = stdout_record
    stderr_path, stderr_bytes = stderr_record
    if (stdout_path.resolve() != (run_dir / "stdout.log").resolve() or stderr_path.resolve() != (run_dir / "stderr.log").resolve() or stdout_path.is_symlink() or stderr_path.is_symlink()):
        return None
    try:
        stdout_text = stdout_bytes.decode("utf-8", errors="strict")
        stderr_text = stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    transcript_record = _artifact_bytes(state, "transcript")
    if transcript_record is None:
        return None
    transcript_path, transcript_bytes = transcript_record
    try:
        transcript_text = transcript_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if transcript_path.resolve() != (run_dir / "transcript.md").resolve() or transcript_path.is_symlink() or any(CHATGPT_CONVERSATION_URL_RE.search(text) for text in (stdout_text, stderr_text, transcript_text)):
        return None
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    locator = str(oracle.get("session_locator") or oracle.get("slug") or "").strip()
    if not locator or ORACLE_PROMPT_NOT_OBSERVED_MARKER not in stdout_text or f"Session: {locator}" not in stdout_text:
        return None
    mission = state.get("mission") if isinstance(state.get("mission"), dict) else {}
    source_path = Path(str(mission.get("path") or ""))
    transport_path = Path(str(mission.get("transport_path") or ""))
    try:
        project_root = Path(str(state.get("project_root") or "")).resolve(strict=True)
        if not project_root.is_dir() or not source_path.is_absolute() or not transport_path.is_absolute() or source_path.is_symlink() or transport_path.is_symlink():
            return None
        source_path = source_path.resolve(strict=True)
        transport_path = transport_path.resolve(strict=True)
        if not source_path.is_file() or transport_path != (run_dir / "mission.md").resolve() or not is_within(project_root, source_path):
            return None
        source_bytes = source_path.read_bytes()
        transport_bytes = transport_path.read_bytes()
    except OSError:
        return None
    mission_sha256 = str(mission.get("sha256") or "")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    transport_sha256 = hashlib.sha256(transport_bytes).hexdigest()
    if not re.fullmatch(r"[a-f0-9]{64}", mission_sha256) or source_sha256 != mission_sha256 or transport_sha256 != mission_sha256 or source_bytes != transport_bytes:
        return None
    provenance = _web_multi_child_provenance(state, run_dir, project_root, source_path, parent_id, locator)
    if provenance is None:
        return None
    recovery_records: list[dict[str, str]] = []
    for recovery_stdout in sorted(run_dir.glob("recovery-*-stdout.log"), key=lambda item: item.name):
        recovery_stderr = recovery_stdout.with_name(recovery_stdout.name.replace("-stdout.log", "-stderr.log"))
        try:
            if recovery_stdout.is_symlink() or recovery_stderr.is_symlink():
                continue
            recovery_stdout_bytes = recovery_stdout.read_bytes()
            recovery_stderr_bytes = recovery_stderr.read_bytes()
            recovery_text = b"\n".join((recovery_stdout_bytes, recovery_stderr_bytes)).decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            return None
        if CHATGPT_CONVERSATION_URL_RE.search(recovery_text) or ORACLE_RECOVERY_STATE_RE.search(recovery_text) or ORACLE_NO_LIVE_TAB_MARKER not in recovery_text or f'"{locator}"' not in recovery_text or ORACLE_NO_RECOVERABLE_URL_MARKER not in recovery_text:
            return None
        recovery_records.append({"stdout_name": recovery_stdout.name, "stdout_sha256": hashlib.sha256(recovery_stdout_bytes).hexdigest(), "stderr_name": recovery_stderr.name, "stderr_sha256": hashlib.sha256(recovery_stderr_bytes).hexdigest()})
    if not recovery_records:
        return None
    return {
        "settlement_eligibility": "oracle-web-multi-child/v1",
        "project_root": str(project_root), "run_id": run_id, "parallel_parent_id": parent_id,
        "source_mission_path": str(source_path), "source_mission_sha256": source_sha256,
        "transport_mission_path": str(transport_path), "transport_mission_sha256": transport_sha256,
        "mission_sha256": mission_sha256, "oracle_locator": locator,
        **provenance,
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(), "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "recovery_evidence": recovery_records, "output_absent": True, "conversation_url_absent": True,
        "_task_outcome_reason": "user-confirmed-no-submission-after-prompt-timeout",
        "_source_mission_path": str(source_path), "_transport_mission_path": str(transport_path),
    }


def _settlement_logs_have_conversation_url(state_path: Path) -> bool:
    state = load_state(state_path)
    for name in ("stdout", "stderr"):
        record = _artifact_bytes(state, name)
        if record is None:
            continue
        try:
            if CHATGPT_CONVERSATION_URL_RE.search(record[1].decode("utf-8", errors="strict")):
                return True
        except UnicodeDecodeError:
            return True
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    transcript_raw = str(artifacts.get("transcript") or "").strip()
    canonical_transcript = state_path.parent / "transcript.md"
    if transcript_raw:
        try:
            if Path(transcript_raw).resolve() != canonical_transcript.resolve():
                return True
        except OSError:
            return True
    if canonical_transcript.is_symlink():
        return True
    if canonical_transcript.exists():
        try:
            if CHATGPT_CONVERSATION_URL_RE.search(canonical_transcript.read_text(encoding="utf-8", errors="strict")):
                return True
        except (OSError, UnicodeDecodeError):
            return True
    try:
        for path in state_path.parent.glob("recovery-*-*.log"):
            if path.is_symlink() or CHATGPT_CONVERSATION_URL_RE.search(path.read_text(encoding="utf-8", errors="strict")):
                return True
    except (OSError, UnicodeDecodeError):
        return True
    return False


def _direct_app_route_no_submission_evidence(state_path: Path) -> dict[str, Any] | None:
    """Bind an exact direct pre-send app-route rejection to user adjudication."""
    state = load_state(state_path)
    if (
        str(state.get("session_authority") or "") not in {"submitted_unknown", "pre_submit"}
        or state.get("parallel_parent_id") not in {None, ""}
        or state.get("terminal_harvested") is True
        or _state_has_conversation_url(state)
        or str(state.get("mode") or "").casefold() != "browser"
        or str(state.get("transport") or "").casefold() != "devspace"
        or str(state.get("app_name") or "").casefold() != "devspace"
    ):
        return None
    run_dir = state_path.parent
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    if output.resolve() != (run_dir / "output.md").resolve() or output.is_symlink() or output_is_nonempty(output):
        return None
    records = {name: _artifact_bytes(state, name) for name in ("stdout", "stderr", "transcript")}
    if any(record is None for record in records.values()):
        return None
    stdout_path, stdout_bytes = records["stdout"]  # type: ignore[misc]
    stderr_path, stderr_bytes = records["stderr"]  # type: ignore[misc]
    transcript_path, transcript_bytes = records["transcript"]  # type: ignore[misc]
    if (
        stdout_path.resolve() != (run_dir / "stdout.log").resolve()
        or stderr_path.resolve() != (run_dir / "stderr.log").resolve()
        or transcript_path.resolve() != (run_dir / "transcript.md").resolve()
        or any(path.is_symlink() for path in (stdout_path, stderr_path, transcript_path))
    ):
        return None
    try:
        stdout_text = stdout_bytes.decode("utf-8", errors="strict")
        stderr_text = stderr_bytes.decode("utf-8", errors="strict")
        transcript_text = transcript_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if any(CHATGPT_CONVERSATION_URL_RE.search(text) for text in (stdout_text, stderr_text, transcript_text)):
        return None
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    locator = str(oracle.get("session_locator") or oracle.get("slug") or "").strip()
    stdout_lines = {line.strip() for line in stdout_text.splitlines()}
    if (
        normalize_oracle_version(oracle.get("resolved_version")) != ORACLE_ACTIVE_VERSION
        or not locator
        or f"Session: {locator}" not in stdout_text
        or {
            f"ERROR: {ORACLE_APP_MENTION_ROUTE_UNCONFIRMED_MARKER}",
            f"User error (browser-automation): {ORACLE_APP_MENTION_ROUTE_UNCONFIRMED_MARKER}",
        }.issubset(stdout_lines) is False
    ):
        return None
    mission = state.get("mission") if isinstance(state.get("mission"), dict) else {}
    manifest = state.get("manifest") if isinstance(state.get("manifest"), dict) else {}
    try:
        project_root = Path(str(state.get("project_root") or "")).resolve(strict=True)
        source_path = Path(str(mission.get("path") or ""))
        transport_path = Path(str(mission.get("transport_path") or ""))
        manifest_path = Path(str(manifest.get("path") or ""))
        if (
            not project_root.is_dir()
            or not all(path.is_absolute() for path in (source_path, transport_path, manifest_path))
            or any(path.is_symlink() for path in (source_path, transport_path, manifest_path))
        ):
            return None
        source_path = source_path.resolve(strict=True)
        transport_path = transport_path.resolve(strict=True)
        manifest_path = manifest_path.resolve(strict=True)
        if (
            transport_path != (run_dir / "mission.md").resolve()
            or not is_within(project_root, source_path)
            or not is_within(project_root, manifest_path)
        ):
            return None
        source_bytes = source_path.read_bytes()
        transport_bytes = transport_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
    except OSError:
        return None
    mission_sha256 = hashlib.sha256(source_bytes).hexdigest()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    expected_manifest_sha256 = str(manifest.get("expected_sha256") or "")
    if (
        source_bytes != transport_bytes
        or mission_sha256 != str(mission.get("sha256") or "")
        or manifest_sha256 != str(manifest.get("actual_sha256") or "")
        or (expected_manifest_sha256 and expected_manifest_sha256 != manifest_sha256)
    ):
        return None
    return {
        "settlement_eligibility": "oracle-direct-app-route-unconfirmed/v1",
        "project_root": str(project_root),
        "run_id": str(state.get("run_id") or ""),
        "source_mission_path": str(source_path),
        "source_mission_sha256": mission_sha256,
        "transport_mission_path": str(transport_path),
        "transport_mission_sha256": hashlib.sha256(transport_bytes).hexdigest(),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "mission_sha256": mission_sha256,
        "oracle_locator": locator,
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "transcript_sha256": hashlib.sha256(transcript_bytes).hexdigest(),
        "recovery_evidence": [],
        "output_absent": True,
        "conversation_url_absent": True,
        "_task_outcome_reason": "user-confirmed-no-submission-after-app-route-unconfirmed",
    }


def _user_confirmable_no_submission_evidence(state_path: Path) -> dict[str, Any] | None:
    """Return exact evidence for an eligible user-adjudicated run."""
    if _settlement_logs_have_conversation_url(state_path):
        return None
    comprehensive = _comprehensive_no_submission_evidence(state_path)
    if comprehensive is not None:
        return comprehensive
    try:
        mission_text = (state_path.parent / "mission.md").read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    # A partial or malformed comprehensive contract must never fall through.
    if "[HOST_STAGE_CONTRACT]" in mission_text or "[DEVSPACE_WORKSPACE_ENTRY_CONTRACT]" in mission_text:
        return None
    direct = _direct_app_route_no_submission_evidence(state_path)
    if direct is not None:
        return direct
    return _web_multi_child_no_submission_evidence(state_path)


def proven_user_confirmed_no_submission(state_path: Path) -> dict[str, Any] | None:
    """Revalidate a persisted user confirmation against immutable run artifacts."""
    state = load_state(state_path)
    reference = state.get("user_confirmed_no_submission")
    if not isinstance(reference, dict):
        return None
    expected_path = state_path.parent / "user-confirmed-no-submission.json"
    if (
        reference.get("schema") != "codex.chatgpt.oracle-settlement-reference/v1"
        or Path(str(reference.get("path") or "")).resolve() != expected_path.resolve()
        or expected_path.is_symlink()
    ):
        return None
    try:
        artifact_bytes = expected_path.read_bytes()
        if hashlib.sha256(artifact_bytes).hexdigest() != str(reference.get("sha256") or ""):
            return None
        recorded = json.loads(artifact_bytes.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        recorded.get("schema") != "codex.chatgpt.oracle-user-confirmed-no-submission/v1"
        or recorded.get("code") != "ORACLE_USER_CONFIRMED_NO_SUBMISSION"
        or recorded.get("confirmation") != USER_CONFIRMED_NO_SUBMISSION
        or not str(recorded.get("reason") or "").strip()
    ):
        return None
    current = _user_confirmable_no_submission_evidence(state_path)
    if current is None:
        return None
    for key in (
        "project_root",
        "run_id",
        "mission_sha256",
        "oracle_locator",
        "stdout_sha256",
        "stderr_sha256",
        "recovery_evidence",
        "output_absent",
        "conversation_url_absent",
    ):
        if recorded.get(key) != current.get(key):
            return None
    if current.get("settlement_eligibility") == "oracle-direct-app-route-unconfirmed/v1":
        required = (
            "settlement_eligibility", "source_mission_path", "source_mission_sha256",
            "transport_mission_path", "transport_mission_sha256", "manifest_path",
            "manifest_sha256", "transcript_sha256",
        )
    elif current.get("settlement_eligibility") == "oracle-web-multi-child/v1":
        required = (
            "settlement_eligibility", "parallel_parent_id", "source_mission_path",
            "source_mission_sha256", "transport_mission_path", "transport_mission_sha256",
        )
        if current.get("provenance_mode") == "new-child-provenance/v1":
            required += ("provenance_mode", "child_provenance_path", "child_provenance_sha256", "parent_manifest_path", "parent_manifest_sha256")
        elif current.get("provenance_mode") == "legacy-result-lane/v1":
            required += ("provenance_mode", "legacy_result_path", "legacy_result_sha256", "legacy_lane_manifest_path", "legacy_lane_manifest_sha256")
        else:
            return None
    else:
        required = ("workflow_id", "stage", "attempt_id", "input_mission_sha256")
    if any(recorded.get(key) != current.get(key) for key in required):
        return None
    return {**recorded, **{key: value for key, value in current.items() if key.startswith("_")}}


def settle_user_confirmed_no_submission(
    state_path: Path,
    *,
    confirmation: str,
    reason: str,
) -> dict[str, Any]:
    """Release one ambiguous send only after explicit user adjudication.

    Mechanical evidence remains fail-closed: it merely makes the run eligible.
    The exact confirmation token is the authority that resolves non-submission.
    """
    if confirmation.strip().casefold() != USER_CONFIRMED_NO_SUBMISSION:
        raise OracleStateError(
            "NO_SUBMISSION_CONFIRMATION_REQUIRED",
            f"confirmation must be exactly {USER_CONFIRMED_NO_SUBMISSION}",
        )
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise OracleStateError("NO_SUBMISSION_REASON_REQUIRED", "confirmation reason is required")
    payload = load_state(state_path)
    existing = proven_user_confirmed_no_submission(state_path)
    if existing is not None:
        return payload
    if str(payload.get("session_authority") or "") != "submitted_unknown":
        raise OracleStateError(
            "NO_SUBMISSION_AUTHORITY_INVALID",
            "only a submitted_unknown run may be adjudicated as not submitted",
        )
    evidence = _user_confirmable_no_submission_evidence(state_path)
    if evidence is None:
        raise OracleStateError(
            "NO_SUBMISSION_EVIDENCE_INCOMPLETE",
            "run lacks exact eligible composer-failure and binding evidence for user adjudication",
        )
    recorded = {
        "schema": "codex.chatgpt.oracle-user-confirmed-no-submission/v1",
        "code": "ORACLE_USER_CONFIRMED_NO_SUBMISSION",
        "confirmation": USER_CONFIRMED_NO_SUBMISSION,
        "reason": normalized_reason,
        **{key: value for key, value in evidence.items() if not key.startswith("_")},
    }
    settlement_path = state_path.parent / "user-confirmed-no-submission.json"
    write_json_atomic(settlement_path, recorded)
    settlement_sha256 = sha256_file(settlement_path)
    payload.update({
        "status": "attention_required",
        "exit_code": int(payload.get("exit_code") or 1),
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "artifact_sha256": None,
        "transport_status": "not_submitted_user_confirmed",
        "task_outcome": "pending",
        "task_outcome_reason": evidence["_task_outcome_reason"],
        "user_confirmed_no_submission": {
            "schema": "codex.chatgpt.oracle-settlement-reference/v1",
            "path": str(settlement_path),
            "sha256": settlement_sha256,
        },
    })
    write_json_atomic(state_path, payload)
    return payload


def proven_pre_submit_rejection(state_path: Path) -> dict[str, Any] | None:
    """Return immutable evidence only for Oracle's own pre-submit prompt dedup rejection."""
    state = load_state(state_path)
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    if str(output) and output_is_nonempty(output):
        return None
    stdout = Path(str(artifacts.get("stdout") or ""))
    try:
        stdout_bytes = stdout.read_bytes()
        stdout_text = stdout_bytes.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    match = ORACLE_DUPLICATE_PROMPT_RE.search(stdout_text)
    if match is None:
        return None
    return {
        "schema": "codex.chatgpt.oracle-pre-submit-rejection/v1",
        "code": "ORACLE_GLOBAL_PROMPT_DUPLICATE",
        "oracle_locator": match.group("locator"),
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "output_absent": True,
    }


def proven_pre_submit_ui_failure(state_path: Path) -> dict[str, Any] | None:
    """Prove Oracle refused an unconfirmed regular or Pro effort before send.

    This binds the markers emitted by the two previously shipped local
    thinking-time patch levels (extra-high fail-closed and Pro-heavy
    upgrade).  In-flight runs launched before the final strict patch may
    still carry their legacy profile model ``gpt-5.5-pro``, so both legacy
    and current Pro model values are accepted here; the current
    strict-patch markers are proven by ``proven_pre_submit_thinking_time_failure``.
    """
    state = load_state(state_path)
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    profile = state.get("profile") if isinstance(state.get("profile"), dict) else {}
    regular_contract = (
        str(state.get("transport") or "").casefold() == "devspace"
        and str(state.get("app_name") or "").casefold() == "devspace"
        and str(profile.get("model") or "").casefold() == "gpt-5.6"
        and str(profile.get("thinking_time") or "").casefold() == "extra-high"
    )
    pro_contract = (
        str(state.get("transport") or "").casefold() == "pro-attachment-only"
        and state.get("app_name") is None
        and str(profile.get("model") or "").casefold() in {"gpt-5.5-pro", "gpt-5.6-sol"}
        and str(profile.get("thinking_time") or "").casefold() == "heavy"
    )
    if (
        str(state.get("session_authority") or "") not in {"pre_submit", "submitted_unknown"}
        or state.get("terminal_harvested") is True
        or _state_has_conversation_url(state)
        or str(state.get("mode") or "").casefold() != "browser"
        or not (regular_contract or pro_contract)
        or normalize_oracle_version(oracle.get("resolved_version")) != ORACLE_ACTIVE_VERSION
        or _settlement_logs_have_conversation_url(state_path)
    ):
        return None
    run_dir = state_path.parent
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    stdout_record = _artifact_bytes(state, "stdout")
    stderr_record = _artifact_bytes(state, "stderr")
    transcript_record = _artifact_bytes(state, "transcript")
    if (
        output.resolve() != (run_dir / "output.md").resolve()
        or output.is_symlink()
        or output_is_nonempty(output)
        or stdout_record is None
        or stderr_record is None
        or transcript_record is None
    ):
        return None
    stdout_path, stdout_bytes = stdout_record
    stderr_path, stderr_bytes = stderr_record
    transcript_path, transcript_bytes = transcript_record
    if (
        stdout_path.resolve() != (run_dir / "stdout.log").resolve()
        or stderr_path.resolve() != (run_dir / "stderr.log").resolve()
        or transcript_path.resolve() != (run_dir / "transcript.md").resolve()
        or any(path.is_symlink() for path in (stdout_path, stderr_path, transcript_path))
    ):
        return None
    try:
        stdout_text = stdout_bytes.decode("utf-8", errors="strict")
        stderr_bytes.decode("utf-8", errors="strict")
        transcript_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    locator = str(oracle.get("session_locator") or oracle.get("slug") or "").strip()
    lines = {line.strip() for line in stdout_text.splitlines()}
    if regular_contract:
        marker = ORACLE_THINKING_TIME_UNCONFIRMED_MARKER
        code = "ORACLE_THINKING_TIME_UNCONFIRMED_PRE_SUBMIT"
        failure_reason = "extra-high-ui-option-unconfirmed"
    else:
        error_messages = {
            line.removeprefix("ERROR: ")
            for line in lines
            if line.startswith("ERROR: ")
        }
        user_prefix = "User error (browser-automation): "
        user_messages = {
            line.removeprefix(user_prefix)
            for line in lines
            if line.startswith(user_prefix)
        }
        matches = [
            message
            for message in error_messages & user_messages
            if ORACLE_PRO_HEAVY_UNCONFIRMED_RE.fullmatch(message)
        ]
        if len(matches) != 1:
            return None
        marker = matches[0]
        code = "ORACLE_PRO_HEAVY_UNCONFIRMED_PRE_SUBMIT"
        failure_reason = "pro-heavy-ui-option-unconfirmed"
    if (
        not locator
        or f"Session: {locator}" not in stdout_text
        or {
            f"ERROR: {marker}",
            f"User error (browser-automation): {marker}",
        }.issubset(lines) is False
    ):
        return None
    return {
        "schema": "codex.chatgpt.oracle-pre-submit-ui-failure/v1",
        "code": code,
        "oracle_locator": locator,
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "transcript_sha256": hashlib.sha256(transcript_bytes).hexdigest(),
        "output_absent": True,
        "conversation_url_absent": True,
        "failure_reason": failure_reason,
    }


def proven_pre_submit_host_failure(state_path: Path) -> dict[str, Any] | None:
    """Prove a structured readiness or host failure happened before Oracle launch.

    `execute_run` emits the version-resolution prefix itself before the Oracle
    process is created.  The additional immutable-state checks keep this from
    reclassifying a real submitted or live session.
    """
    state = load_state(state_path)
    authority = str(state.get("session_authority") or "")
    if authority not in {"pre_submit", "submitted_unknown"}:
        return None
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    if _state_has_conversation_url(state):
        return None
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    if str(output) and output_is_nonempty(output):
        return None
    stdout_record = _artifact_bytes(state, "stdout")
    stderr_record = _artifact_bytes(state, "stderr")
    if stdout_record is None or stderr_record is None:
        return None
    _, stdout_bytes = stdout_record
    _, stderr_bytes = stderr_record
    readiness = state.get("submission_readiness")
    if (
        authority == "pre_submit"
        and isinstance(readiness, dict)
        and readiness.get("schema") == "codex.chatgpt.oracle-submission-readiness/v1"
        and readiness.get("ready") is False
        and isinstance(readiness.get("checks"), list)
        and isinstance(readiness.get("failed_checks"), list)
        and readiness["failed_checks"]
        and isinstance(readiness.get("error"), dict)
        and readiness["error"].get("code") in {
            "SUBMISSION_NOT_READY",
            "DEVSPACE_SERVICE_RESTART_REQUIRED",
        }
        and not stdout_bytes
    ):
        return {
            "schema": "codex.chatgpt.oracle-pre-submit-readiness-failure/v1",
            "code": str(readiness["error"]["code"]),
            "failed_checks": list(readiness["failed_checks"]),
            "readiness": readiness,
            "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
            "output_absent": True,
            "conversation_url_absent": True,
        }
    # Oracle prints this version banner before validating local attachments;
    # it is not browser/session evidence.  Any other stdout remains fail-closed.
    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    attachment_limit_banner_only = stdout_text == "🧿 oracle 0.16.1 — Questions in, clarity out."
    if stdout_text and not attachment_limit_banner_only:
        return None
    try:
        stderr_text = stderr_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    normalized_error = stderr_text.lstrip()
    if (
        str(state.get("transport") or "") == "pro-attachment-only"
        and "The following files exceed the 1 MB limit:" in normalized_error
        and attachment_limit_banner_only
    ):
        attachments = state.get("attachments")
        if not isinstance(attachments, list) or not any(
            isinstance(item, dict) and int(item.get("size_bytes") or 0) > 1024 * 1024
            for item in attachments
        ):
            return None
        failure_reason = "oracle-attachment-size-limit"
        code = "ORACLE_ATTACHMENT_SIZE_PRELAUNCH_FAILED"
    elif str(oracle.get("resolved_version") or "") != "unresolved":
        return None
    elif not normalized_error.startswith("version resolution failed:"):
        return None
    elif "Oracle compatibility is validated only for the tested version" in normalized_error:
        failure_reason = "compatibility-version-drift"
        code = "ORACLE_VERSION_RESOLUTION_PRELAUNCH_FAILED"
    elif (
        "ORACLE_VERSION_TIMEOUT:" in normalized_error
        or ("--version" in normalized_error and "timed out after" in normalized_error)
    ):
        failure_reason = "version-resolution-timeout"
        code = "ORACLE_VERSION_RESOLUTION_PRELAUNCH_FAILED"
    else:
        return None
    return {
        "schema": "codex.chatgpt.oracle-pre-submit-host-failure/v1",
        "code": code,
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "output_absent": True,
        "conversation_url_absent": True,
        "resolved_version": "unresolved",
        "failure_reason": failure_reason,
    }


def proven_pre_submit_profile_copy_ebusy(state_path: Path) -> dict[str, Any] | None:
    """Prove Oracle failed while copying its profile, before it could open ChatGPT."""
    state = load_state(state_path)
    if str(state.get("session_authority") or "") not in {"pre_submit", "submitted_unknown"}:
        return None
    if state.get("terminal_harvested") is True or _state_has_conversation_url(state):
        return None
    profile = state.get("profile") if isinstance(state.get("profile"), dict) else {}
    copy_profile = Path(str(profile.get("copy_profile") or ""))
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    browser_temp = Path(str(artifacts.get("browser_temp") or ""))
    output = Path(str(artifacts.get("output") or ""))
    stdout_record = _artifact_bytes(state, "stdout")
    stderr_record = _artifact_bytes(state, "stderr")
    if (
        not str(copy_profile)
        or not str(browser_temp)
        or output_is_nonempty(output)
        or stdout_record is None
        or stderr_record is None
    ):
        return None
    _, stdout_bytes = stdout_record
    _, stderr_bytes = stderr_record
    text = (stdout_bytes + b"\n" + stderr_bytes).decode("utf-8", errors="replace")
    if "https://chatgpt.com/c/" in text.casefold():
        return None
    match = ORACLE_PROFILE_COPY_EBUSY_RE.search(text)
    if match is None:
        return None
    source = Path(match.group("source"))
    destination = Path(match.group("destination"))
    expected_source = copy_profile / "Default" / "Network" / "Cookies"
    if (
        source.resolve() != expected_source.resolve()
        or not is_within(browser_temp.resolve(), destination.resolve())
        or destination.name.casefold() != "cookies"
    ):
        return None
    return {
        "schema": "codex.chatgpt.oracle-pre-submit-host-failure/v1",
        "code": "ORACLE_PROFILE_COPY_EBUSY_PRELAUNCH_FAILED",
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "output_absent": True,
        "conversation_url_absent": True,
        "failure_reason": "oracle-profile-copy-ebusy",
        "copy_source": str(source.resolve()),
        "copy_destination": str(destination.resolve()),
    }


def proven_pre_submit_thinking_time_failure(state_path: Path) -> dict[str, Any] | None:
    """Prove the final strict Power-slider selector refused before send.

    Accepts only Oracle's exact selection-unverified/unknown-outcome
    diagnostics from the adopted upstream thinking-time patch, with the
    requested and required effort labels agreeing, and only while the exact
    conversation URL and any durable output are absent.  Anything else keeps
    submitted-unknown ownership and therefore the project lock fail-closed.
    """
    state = load_state(state_path)
    if str(state.get("session_authority") or "") not in {"pre_submit", "submitted_unknown"}:
        return None
    if state.get("terminal_harvested") is True or _state_has_conversation_url(state):
        return None
    profile = state.get("profile") if isinstance(state.get("profile"), dict) else {}
    regular_contract = (
        str(state.get("transport") or "").casefold() == "devspace"
        and str(state.get("app_name") or "").casefold() == "devspace"
        and str(profile.get("model") or "").casefold() == "gpt-5.6"
        and str(profile.get("thinking_time") or "").casefold() == "extra-high"
    )
    pro_contract = (
        str(state.get("transport") or "").casefold() == "pro-attachment-only"
        and state.get("app_name") is None
        and str(profile.get("model") or "").casefold() == "gpt-5.6-sol"
        and str(profile.get("thinking_time") or "").casefold() == "heavy"
    )
    if not (regular_contract or pro_contract):
        return None
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    stdout_record = _artifact_bytes(state, "stdout")
    stderr_record = _artifact_bytes(state, "stderr")
    if output_is_nonempty(output) or stdout_record is None or stderr_record is None:
        return None
    _, stdout_bytes = stdout_record
    _, stderr_bytes = stderr_record
    combined = (stdout_bytes + b"\n" + stderr_bytes).decode("utf-8", errors="replace")
    if CHATGPT_CONVERSATION_URL_RE.search(combined):
        return None
    match = ORACLE_THINKING_TIME_PRE_SUBMIT_RE.search(combined)
    if match is None or match.group("requested").casefold() != match.group("required").casefold():
        return None
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    locator = str(oracle.get("session_locator") or oracle.get("slug") or "").strip()
    if not locator or normalize_oracle_version(oracle.get("resolved_version")) != ORACLE_ACTIVE_VERSION:
        return None
    return {
        "schema": "codex.chatgpt.oracle-pre-submit-ui-failure/v1",
        "code": "ORACLE_THINKING_TIME_PRE_SUBMIT_FAILED",
        "oracle_locator": locator,
        "requested_level": match.group("requested"),
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "output_absent": True,
        "conversation_url_absent": True,
        "failure_reason": "oracle-thinking-time-selection-unverified",
    }


def proven_pre_submit_model_switcher_failure(state_path: Path) -> dict[str, Any] | None:
    """Prove Oracle failed selecting a model before it could send a prompt.

    This intentionally accepts only Oracle's exact model-switcher/no-cookie
    diagnostic, with both output and conversation evidence absent.  A generic
    browser error, a recorded conversation URL, or any durable output remains
    submitted-unknown and therefore keeps the project lock fail-closed.
    """
    state = load_state(state_path)
    if str(state.get("session_authority") or "") not in {"pre_submit", "submitted_unknown"}:
        return None
    if state.get("terminal_harvested") is True or _state_has_conversation_url(state):
        return None
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    stdout_record = _artifact_bytes(state, "stdout")
    stderr_record = _artifact_bytes(state, "stderr")
    if output_is_nonempty(output) or stdout_record is None or stderr_record is None:
        return None
    _, stdout_bytes = stdout_record
    _, stderr_bytes = stderr_record
    combined = (stdout_bytes + b"\n" + stderr_bytes).decode("utf-8", errors="replace")
    if CHATGPT_CONVERSATION_URL_RE.search(combined):
        return None
    if ORACLE_MODEL_SWITCHER_PRE_SUBMIT_RE.search(combined) is None:
        return None
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    locator = str(oracle.get("session_locator") or oracle.get("slug") or "").strip()
    if not locator or normalize_oracle_version(oracle.get("resolved_version")) != ORACLE_ACTIVE_VERSION:
        return None
    return {
        "schema": "codex.chatgpt.oracle-pre-submit-ui-failure/v1",
        "code": "ORACLE_MODEL_SWITCHER_PRE_SUBMIT_FAILED",
        "oracle_locator": locator,
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "output_absent": True,
        "conversation_url_absent": True,
        "failure_reason": "oracle-model-switcher-no-cookies",
    }


def proven_pre_submit_copy_profile_manual_login_conflict(state_path: Path) -> dict[str, Any] | None:
    """Prove Oracle rejected mutually exclusive profile modes before browser launch."""
    state = load_state(state_path)
    if str(state.get("session_authority") or "") not in {"pre_submit", "submitted_unknown"}:
        return None
    if state.get("terminal_harvested") is True or _state_has_conversation_url(state):
        return None
    profile = state.get("profile") if isinstance(state.get("profile"), dict) else {}
    copy_profile = str(profile.get("copy_profile") or "").strip()
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    stdout_record = _artifact_bytes(state, "stdout")
    stderr_record = _artifact_bytes(state, "stderr")
    if (
        not copy_profile
        or str(oracle.get("resolved_version") or "").removeprefix("oracle ").strip() != ORACLE_ACTIVE_VERSION
        or output_is_nonempty(output)
        or stdout_record is None
        or stderr_record is None
    ):
        return None
    _, stdout_bytes = stdout_record
    _, stderr_bytes = stderr_record
    combined = (stdout_bytes + b"\n" + stderr_bytes).decode("utf-8", errors="replace")
    if CHATGPT_CONVERSATION_URL_RE.search(combined):
        return None
    if ORACLE_COPY_PROFILE_MANUAL_LOGIN_CONFLICT not in combined:
        return None
    return {
        "schema": "codex.chatgpt.oracle-pre-submit-host-failure/v1",
        "code": "ORACLE_LAUNCH_FLAGS_MUTUALLY_EXCLUSIVE_PRELAUNCH_FAILED",
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "output_absent": True,
        "conversation_url_absent": True,
        "failure_reason": "copy-profile-manual-login-default-conflict",
        "copy_profile": str(Path(copy_profile).resolve()),
    }


def proven_pre_submit_profile_copy_rsync_missing(state_path: Path) -> dict[str, Any] | None:
    """Prove profile copy failed before Chrome because Oracle invoked rsync."""
    state = load_state(state_path)
    if str(state.get("session_authority") or "") not in {"pre_submit", "submitted_unknown"}:
        return None
    if state.get("terminal_harvested") is True or _state_has_conversation_url(state):
        return None
    profile = state.get("profile") if isinstance(state.get("profile"), dict) else {}
    copy_profile = str(profile.get("copy_profile") or "").strip()
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    stdout_record = _artifact_bytes(state, "stdout")
    stderr_record = _artifact_bytes(state, "stderr")
    if (
        not copy_profile
        or str(oracle.get("resolved_version") or "").removeprefix("oracle ").strip() != ORACLE_ACTIVE_VERSION
        or output_is_nonempty(output)
        or stdout_record is None
        or stderr_record is None
    ):
        return None
    _, stdout_bytes = stdout_record
    _, stderr_bytes = stderr_record
    combined = (stdout_bytes + b"\n" + stderr_bytes).decode("utf-8", errors="replace")
    if CHATGPT_CONVERSATION_URL_RE.search(combined):
        return None
    if ORACLE_PROFILE_COPY_RSYNC_MISSING not in combined:
        return None
    return {
        "schema": "codex.chatgpt.oracle-pre-submit-host-failure/v1",
        "code": "ORACLE_PROFILE_COPY_RSYNC_PRELAUNCH_FAILED",
        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
        "output_absent": True,
        "conversation_url_absent": True,
        "failure_reason": "oracle-profile-copy-requires-rsync-on-windows",
        "copy_profile": str(Path(copy_profile).resolve()),
    }


def proven_pre_submit_failure(state_path: Path) -> dict[str, Any] | None:
    return (
        proven_pre_submit_rejection(state_path)
        or proven_pre_submit_ui_failure(state_path)
        or proven_pre_submit_thinking_time_failure(state_path)
        or proven_pre_submit_model_switcher_failure(state_path)
        or proven_pre_submit_copy_profile_manual_login_conflict(state_path)
        or proven_pre_submit_profile_copy_rsync_missing(state_path)
        or proven_pre_submit_profile_copy_ebusy(state_path)
        or proven_pre_submit_host_failure(state_path)
        or proven_user_confirmed_no_submission(state_path)
    )


def settle_proven_pre_submit_rejection(state_path: Path) -> dict[str, Any] | None:
    """Correct submitted_unknown only when exact Oracle stdout proves no send occurred."""
    evidence = proven_pre_submit_rejection(state_path)
    if evidence is None:
        return None
    payload = load_state(state_path)
    payload.update({
        "status": "attention_required",
        "exit_code": int(payload.get("exit_code") or 1),
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "artifact_sha256": None,
        "transport_status": "rejected_pre_submit",
        "task_outcome": "pending",
        "task_outcome_reason": "oracle-global-prompt-duplicate",
        "pre_submit_rejection": evidence,
    })
    write_json_atomic(state_path, payload)
    return payload


def settle_proven_pre_submit_failure(state_path: Path) -> dict[str, Any] | None:
    """Settle either supported immutable proof without preserving a false lock."""
    rejection = proven_pre_submit_rejection(state_path)
    if rejection is not None:
        return settle_proven_pre_submit_rejection(state_path)
    confirmed = proven_user_confirmed_no_submission(state_path)
    if confirmed is not None:
        return load_state(state_path)
    evidence = proven_pre_submit_host_failure(state_path)
    if evidence is None:
        evidence = proven_pre_submit_profile_copy_ebusy(state_path)
    if evidence is None:
        evidence = proven_pre_submit_ui_failure(state_path)
    if evidence is None:
        evidence = proven_pre_submit_thinking_time_failure(state_path)
    if evidence is None:
        evidence = proven_pre_submit_model_switcher_failure(state_path)
    if evidence is None:
        evidence = proven_pre_submit_copy_profile_manual_login_conflict(state_path)
    if evidence is None:
        evidence = proven_pre_submit_profile_copy_rsync_missing(state_path)
    if evidence is None:
        return None
    payload = load_state(state_path)
    payload.update({
        "status": "attention_required",
        "exit_code": int(payload.get("exit_code") or 1),
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "artifact_sha256": None,
        "transport_status": "failed_pre_submit",
        "task_outcome": "not_executed" if evidence["code"] in {
            "ORACLE_PROFILE_COPY_EBUSY_PRELAUNCH_FAILED",
            "ORACLE_PROFILE_COPY_RSYNC_PRELAUNCH_FAILED",
            "ORACLE_THINKING_TIME_UNCONFIRMED_PRE_SUBMIT",
            "ORACLE_PRO_HEAVY_UNCONFIRMED_PRE_SUBMIT",
            "ORACLE_THINKING_TIME_PRE_SUBMIT_FAILED",
            "ORACLE_MODEL_SWITCHER_PRE_SUBMIT_FAILED",
            "ORACLE_LAUNCH_FLAGS_MUTUALLY_EXCLUSIVE_PRELAUNCH_FAILED",
        } else "pending",
        "task_outcome_reason": (
            "oracle-profile-copy-ebusy-pre-submit"
            if evidence["code"] == "ORACLE_PROFILE_COPY_EBUSY_PRELAUNCH_FAILED"
            else "oracle-profile-copy-rsync-pre-submit"
            if evidence["code"] == "ORACLE_PROFILE_COPY_RSYNC_PRELAUNCH_FAILED"
            else "extra-high-ui-option-unconfirmed-pre-submit"
            if evidence["code"] == "ORACLE_THINKING_TIME_UNCONFIRMED_PRE_SUBMIT"
            else "pro-heavy-ui-option-unconfirmed-pre-submit"
            if evidence["code"] == "ORACLE_PRO_HEAVY_UNCONFIRMED_PRE_SUBMIT"
            else "oracle-thinking-time-pre-submit"
            if evidence["code"] == "ORACLE_THINKING_TIME_PRE_SUBMIT_FAILED"
            else "oracle-model-switcher-pre-submit"
            if evidence["code"] == "ORACLE_MODEL_SWITCHER_PRE_SUBMIT_FAILED"
            else "oracle-launch-flags-mutually-exclusive-pre-submit"
            if evidence["code"] == "ORACLE_LAUNCH_FLAGS_MUTUALLY_EXCLUSIVE_PRELAUNCH_FAILED"
            else "prelaunch-host-failure"
        ),
        "pre_submit_failure": evidence,
    })
    write_json_atomic(state_path, payload)
    return payload


def settle_pre_submit_session_absent(
    state_path: Path,
    *,
    locator: str,
    recovery_stdout: Path,
    recovery_stderr: Path,
) -> dict[str, Any] | None:
    """Keep pre-submit authority when exact recovery proves no Oracle session exists."""
    payload = load_state(state_path)
    if str(payload.get("session_authority") or "") != "pre_submit":
        return None
    if _state_has_conversation_url(payload):
        return None
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    output = Path(str(artifacts.get("output") or ""))
    if str(output) and output_is_nonempty(output):
        return None
    chunks: list[bytes] = []
    for path in (recovery_stdout, recovery_stderr):
        try:
            chunks.append(path.read_bytes())
        except OSError:
            chunks.append(b"")
    combined = b"\n".join(chunks)
    try:
        text = combined.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    matches = [match.group("locator") for match in ORACLE_NO_SESSION_RE.finditer(text)]
    if matches != [locator]:
        return None
    evidence = {
        "schema": "codex.chatgpt.oracle-pre-submit-session-absence/v1",
        "code": "ORACLE_EXACT_SESSION_NOT_FOUND",
        "oracle_locator": locator,
        "recovery_sha256": hashlib.sha256(combined).hexdigest(),
        "output_absent": True,
        "conversation_url_absent": True,
    }
    payload.update({
        "status": "attention_required",
        "exit_code": int(payload.get("exit_code") or 1),
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "artifact_sha256": None,
        "transport_status": "not_submitted",
        "task_outcome": "pending",
        "task_outcome_reason": "exact-session-absent-before-submit",
        "pre_submit_session_absence": evidence,
    })
    write_json_atomic(state_path, payload)
    return payload


def resolve_lifecycle(state: dict[str, Any], *, output_is_present: bool | None = None) -> dict[str, Any]:
    """Collapse the stored run record into one bounded lifecycle verdict.

    Authority order is fixed and single-sourced: exact terminal web evidence
    outranks a durable stored artifact, which outranks the local ledger.  PIDs,
    heartbeats, locks and poll results are diagnostics and never appear here.
    """
    status = str(state.get("status") or "")
    authority = str(state.get("session_authority") or "")
    harvested = state.get("terminal_harvested") is True
    outcome = str(state.get("task_outcome") or "")
    if output_is_present is None:
        artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
        output_path = Path(str(artifacts.get("output") or ""))
        has_output = bool(str(output_path)) and output_is_nonempty(output_path)
    else:
        has_output = bool(output_is_present)

    if status == "abandoned":
        return {"lifecycle": "abandoned", "authority_source": "explicit-abandonment"}
    # 1. Exact terminal web evidence.
    if authority == "terminal" and harvested and has_output:
        if outcome == "not_executed":
            return {"lifecycle": "needs_attention", "authority_source": "exact-terminal-evidence"}
        return {"lifecycle": "complete", "authority_source": "exact-terminal-evidence"}
    # 2. Durable stored artifact, including ledgers written before authority
    #    tracking existed.  A finished answer on disk is not a defect.
    if has_output and status == "complete":
        if outcome == "not_executed":
            return {"lifecycle": "needs_attention", "authority_source": "durable-artifact"}
        return {"lifecycle": "complete", "authority_source": "durable-artifact"}
    # 3. An owned session that is still live keeps running regardless of a
    #    local nonzero exit; only web state may end it.
    if authority in {"live", "submitted_unknown", "terminal_observed"}:
        return {"lifecycle": "running", "authority_source": "exact-session-ownership"}
    # 4. Local ledger, lowest authority.
    if status == "complete":
        # A ledger that claims completion without a durable artifact has not
        # proven anything.  Never let the weakest authority assert completion.
        return {"lifecycle": "needs_attention", "authority_source": "local-ledger"}
    return {
        "lifecycle": _STATUS_TO_LIFECYCLE.get(status, "needs_attention"),
        "authority_source": "local-ledger",
    }


TASK_OUTCOME_RE = re.compile(
    r"TASK_OUTCOME:\s*(EXECUTED|NOT_EXECUTED|BLOCKED)",
    re.IGNORECASE,
)


def classify_task_outcome(path: Path, *, contract: str, transport: str) -> str:
    if transport == "pro-attachment-only":
        return "not_applicable"
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return "unknown"
    final_line = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
    marker = TASK_OUTCOME_RE.fullmatch(final_line)
    if marker:
        return marker.group(1).casefold()
    return "unknown" if contract == "v1" else "legacy_unclassified"


def unresolved_project_sessions(
    run_root: Path,
    project_root: Path,
    *,
    parallel_parent_id: str | None = None,
    exclude_run_id: str | None = None,
) -> list[dict[str, str]]:
    """Return exact submitted sessions that still own this project.

    A local Oracle exit is not web-terminal authority.  Ownership therefore
    survives ``running``/``attention_required`` host states until exact-session
    recovery records terminal completion.  Parallel children from the same
    persisted parent are allowed to coexist; a different parent is not.
    """
    root = run_root.expanduser().resolve()
    expected_project = str(project_root.expanduser().resolve()).casefold()
    expected_parent = str(parallel_parent_id or "").strip().casefold()
    active_authorities = {"submitted_unknown", "live", "terminal_observed"}
    owners: list[dict[str, str]] = []
    if not root.is_dir():
        return owners
    for candidate in sorted(root.glob("*/state.json"), key=lambda item: str(item)):
        try:
            payload = load_state(candidate)
        except (OSError, OracleStateError):
            continue
        run_id = str(payload.get("run_id") or "")
        if run_id == exclude_run_id or str(payload.get("project_root") or "").casefold() != expected_project:
            continue
        authority = str(payload.get("session_authority") or "").strip().casefold()
        settlement_artifact = candidate.parent / "user-confirmed-no-submission.json"
        settlement_derived = (
            "user_confirmed_no_submission" in payload
            or str(payload.get("transport_status") or "") == "not_submitted_user_confirmed"
            or str(payload.get("task_outcome_reason") or "") in {
                "user-confirmed-no-submission-after-prompt-timeout",
                "user-confirmed-no-submission-after-app-route-unconfirmed",
            }
            or settlement_artifact.exists()
        )
        invalid_settlement = False
        if (
            authority == "pre_submit"
            and settlement_derived
            and proven_user_confirmed_no_submission(candidate) is None
        ):
            # A missing or changed settlement artifact revokes the release and
            # restores fail-closed ownership before any new submission.
            authority = "submitted_unknown"
            invalid_settlement = True
        # Legacy running records fail closed because the provider may still be
        # active. Legacy attention-required records predate explicit session
        # authority and must not become permanent project locks; new runs
        # persist submitted_unknown/live explicitly before reaching attention.
        if not authority and str(payload.get("status") or "").casefold() == "running":
            authority = "submitted_unknown"
        if authority not in active_authorities:
            continue
        owner_parent = str(payload.get("parallel_parent_id") or "").strip().casefold()
        if expected_parent and owner_parent == expected_parent and not invalid_settlement:
            continue
        owners.append({
            "run_id": run_id,
            "session_locator": str((payload.get("oracle") or {}).get("session_locator") or ""),
            "session_authority": authority,
            "state_path": str(candidate),
        })
    return owners


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
