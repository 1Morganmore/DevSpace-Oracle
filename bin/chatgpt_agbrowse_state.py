from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "codex.chatgpt.agbrowse-run/v1"
WEB_MULTI_MANIFEST_SCHEMAS = {
    "codex.chatgpt.web-multi/v1",
    "codex.chatgpt.web-multi/v2",
}
WEB_MULTI_V2_ALLOWED_KEYS = {
    "schema",
    "workflow_id",
    "project_root",
    "question",
    "source_snapshot_path",
    "source_snapshot_sha256",
    "output_dir",
    "chatgpt_app_name",
    "planner_policy",
    "semantics_version",
    "max_iterations",
    "mode_variant",
    "agbrowse_contract",
    "provider_failure_retry_limit",
    "app_decision_path",
    "chatgpt_app_server_url",
    "timeout_seconds",
    "send_timeout_seconds",
    "session_show_timeout_seconds",
    "recovery_timeout_seconds",
    "safe_pre_submit_retry_limit",
    "pre_submit_retry_deadline_seconds",
    "inline_recovery_round_limit",
    "wave_submission_barrier_timeout_seconds",
    "retry_of_workflow_id",
    "provider_failure_retry_index",
    "provider_failure_parent_run_id",
}


def validate_web_multi_parent_manifest(manifest: dict[str, Any]) -> None:
    schema = str(manifest.get("schema") or "")
    if schema not in WEB_MULTI_MANIFEST_SCHEMAS:
        raise StateError(
            "PARENT_MANIFEST_SCHEMA_INVALID",
            "web Multi-GPT parent manifest schema is required",
        )
    if schema != "codex.chatgpt.web-multi/v2":
        return
    unknown = set(manifest) - WEB_MULTI_V2_ALLOWED_KEYS
    if unknown:
        raise StateError(
            "PARENT_MANIFEST_V2_KEYS_INVALID",
            "web Multi-GPT v2 manifest keys are not exact",
            {"unknown": sorted(unknown)},
        )
    if "solver_count" in manifest:
        raise StateError(
            "PARENT_MANIFEST_V2_SOLVER_COUNT_FORBIDDEN",
            "solver_count is forbidden in dynamic v2 manifests",
        )
    if str(manifest.get("planner_policy") or "") not in {
        "upstream-nonempty-prefix10",
        "strict-6-10",
    }:
        raise StateError("PARENT_MANIFEST_V2_POLICY_INVALID", "invalid Planner policy")
    if str(manifest.get("semantics_version") or "") != "upstream-parity-v1":
        raise StateError("PARENT_MANIFEST_V2_SEMANTICS_INVALID", "invalid runtime semantics version")
CANONICAL_CHAT_RE = re.compile(r"^https://chatgpt\.com/c/[A-Za-z0-9_-]+(?:[?#].*)?$")
PROMPT_FILE_HANDOFF = (
    "The attached prompt file is the user-provided task instruction for this conversation, "
    "not reference or webpage content. Read it completely and follow it. "
    "Return only the output format requested by that file."
)
MAX_PROMPT_FILE_BYTES = 2_000_000

PHASES = {
    "CREATED",
    "PREFLIGHTED",
    "LEASED",
    "SEND_STARTED",
    "SUBMITTED",
    "URL_BOUND",
    "RESPONSE_IN_PROGRESS",
    "RESULT_CAPTURED",
    "VERIFIED",
    "COMPLETE",
    "COMPLETE_SUPERSEDED",
    "PREFLIGHT_BLOCKED",
    "SEND_REJECTED",
    "PROVIDER_FAILED_TERMINAL",
    "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
    "RECOVERY_REQUIRED",
    "RECOVERING",
    "BLOCKED_RECOVERY_EXHAUSTED",
    "BLOCKED_MANIFEST_MISMATCH",
    "BLOCKED_OWNER_MISMATCH",
    "BLOCKED_TARGET_AMBIGUOUS",
    "BLOCKED_APP_TRANSACTION",
    "CANCELLED_PRE_SUBMISSION",
    "USER_STOP_REQUESTED",
    "ABANDONED_UNCERTAIN",
}

ALLOWED_TRANSITIONS = {
    "CREATED": {"PREFLIGHTED", "PREFLIGHT_BLOCKED", "BLOCKED_MANIFEST_MISMATCH"},
    "PREFLIGHTED": {"LEASED", "PREFLIGHT_BLOCKED", "BLOCKED_APP_TRANSACTION"},
    "LEASED": {"SEND_STARTED", "PREFLIGHT_BLOCKED", "BLOCKED_APP_TRANSACTION"},
    "SEND_STARTED": {"SUBMITTED", "SEND_REJECTED", "SUBMISSION_UNCERTAIN_IDENTITY_MISSING", "RECOVERY_REQUIRED", "RECOVERING"},
    "SUBMITTED": {"URL_BOUND", "RECOVERY_REQUIRED", "PROVIDER_FAILED_TERMINAL", "SUBMISSION_UNCERTAIN_IDENTITY_MISSING", "BLOCKED_TARGET_AMBIGUOUS"},
    "URL_BOUND": {"RESPONSE_IN_PROGRESS", "RESULT_CAPTURED", "RECOVERY_REQUIRED", "PROVIDER_FAILED_TERMINAL"},
    "RESPONSE_IN_PROGRESS": {"RESULT_CAPTURED", "RECOVERY_REQUIRED", "RECOVERING", "PROVIDER_FAILED_TERMINAL"},
    "RECOVERY_REQUIRED": {
        "RECOVERING",
        "SEND_REJECTED",
        "BLOCKED_RECOVERY_EXHAUSTED",
        "BLOCKED_MANIFEST_MISMATCH",
        "BLOCKED_OWNER_MISMATCH",
        "BLOCKED_TARGET_AMBIGUOUS",
        "PROVIDER_FAILED_TERMINAL",
    },
    "RECOVERING": {
        "URL_BOUND",
        "RESPONSE_IN_PROGRESS",
        "RESULT_CAPTURED",
        "RECOVERY_REQUIRED",
        "BLOCKED_RECOVERY_EXHAUSTED",
        "BLOCKED_TARGET_AMBIGUOUS",
        "PROVIDER_FAILED_TERMINAL",
    },
    "RESULT_CAPTURED": {"VERIFIED", "RECOVERY_REQUIRED"},
    "VERIFIED": {"COMPLETE", "RECOVERY_REQUIRED"},
    "PREFLIGHT_BLOCKED": {"PREFLIGHTED", "CANCELLED_PRE_SUBMISSION"},
    "SEND_REJECTED": {"PREFLIGHTED", "CANCELLED_PRE_SUBMISSION"},
    "BLOCKED_APP_TRANSACTION": {"PREFLIGHTED", "CANCELLED_PRE_SUBMISSION"},
    "SUBMISSION_UNCERTAIN_IDENTITY_MISSING": {"SEND_REJECTED", "RECOVERING"},
    "BLOCKED_RECOVERY_EXHAUSTED": {"RECOVERING", "SEND_REJECTED"},
}

TERMINAL_PHASES = {
    "COMPLETE",
    "COMPLETE_SUPERSEDED",
    "PROVIDER_FAILED_TERMINAL",
    "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
    "BLOCKED_RECOVERY_EXHAUSTED",
    "BLOCKED_MANIFEST_MISMATCH",
    "BLOCKED_OWNER_MISMATCH",
    "BLOCKED_TARGET_AMBIGUOUS",
    "BLOCKED_APP_TRANSACTION",
    "CANCELLED_PRE_SUBMISSION",
    "ABANDONED_UNCERTAIN",
}

PARENT_PHASES = {
    "PARENT_CREATED",
    "PARENT_ACTIVE",
    "PARENT_DRAINING",
    "PARENT_RECOVERY_REQUIRED",
    "PARENT_COMPLETE",
    "PARENT_FAILED_CLOSED",
}

PARENT_TERMINAL_PHASES = {"PARENT_COMPLETE", "PARENT_FAILED_CLOSED"}

CHILD_SAFE_TERMINAL_PHASES = {
    "COMPLETE",
    "CANCELLED_PRE_SUBMISSION",
    "SEND_REJECTED",
    "PROVIDER_FAILED_TERMINAL",
    "PREFLIGHT_BLOCKED",
    "BLOCKED_APP_TRANSACTION",
    "ABANDONED_UNCERTAIN",
}

UNCERTAIN_OR_SUBMITTED_PHASES = {
    "SEND_STARTED",
    "SUBMITTED",
    "URL_BOUND",
    "RESPONSE_IN_PROGRESS",
    "RESULT_CAPTURED",
    "VERIFIED",
    "RECOVERY_REQUIRED",
    "RECOVERING",
    "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
    "BLOCKED_RECOVERY_EXHAUSTED",
    "BLOCKED_MANIFEST_MISMATCH",
    "BLOCKED_OWNER_MISMATCH",
    "BLOCKED_TARGET_AMBIGUOUS",
    "USER_STOP_REQUESTED",
}

SAFE_STALE_PRE_SUBMISSION_PHASES = {
    "CREATED",
    "PREFLIGHTED",
    "LEASED",
    "PREFLIGHT_BLOCKED",
    "BLOCKED_APP_TRANSACTION",
}

REQUIRED_IMMUTABLE = {
    "schema",
    "run_id",
    "project_root",
    "project_key",
    "manifest_path",
    "manifest_sha256",
    "prompt_sha256",
    "requested",
    "agbrowse",
    "owner",
    "created_at",
}


class StateError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 39:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(min(0.01 * (attempt + 1), 0.1))


@contextmanager
def exclusive_state_lock(path: Path, timeout_seconds: int = 120):
    """Cross-process one-byte lock used for parent create/drain transitions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = time.monotonic() + max(1, timeout_seconds)
    locked = False
    try:
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise StateError(
                        "PARENT_TRANSITION_LOCK_TIMEOUT",
                        "timed out waiting for the parent transition lock",
                        {"path": str(path)},
                    )
                time.sleep(0.05)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise StateError("STATE_UNREADABLE", f"cannot read JSON state: {path}", {"detail": str(exc)}) from exc
    if not isinstance(value, dict):
        raise StateError("STATE_INVALID", f"JSON object required: {path}")
    return value


def load_manifest(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore

            value = yaml.safe_load(raw)
        except Exception as exc:
            raise StateError("MANIFEST_INVALID", f"manifest is not valid JSON/YAML: {path}", {"detail": str(exc)}) from exc
    if not isinstance(value, dict):
        raise StateError("MANIFEST_INVALID", "manifest root must be an object")
    return value


def prompt_contract(manifest: dict[str, Any], *, require_file: bool = False) -> dict[str, Any]:
    transport = str(manifest.get("prompt_transport") or "inline").strip().casefold()
    if transport == "file":
        raw_path = str(manifest.get("prompt_file") or "").strip()
        expected_sha256 = str(manifest.get("prompt_file_sha256") or "").strip().casefold()
        if not raw_path or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise StateError(
                "PROMPT_FILE_CONTRACT_INVALID",
                "file prompt transport requires prompt_file and lowercase prompt_file_sha256",
            )
        try:
            prompt_file = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise StateError("PROMPT_FILE_INVALID", "prompt file is unavailable", {"path": raw_path}) from exc
        if not prompt_file.is_file() or prompt_file.is_symlink():
            raise StateError("PROMPT_FILE_INVALID", "prompt file must be a regular non-symlink file")
        data = prompt_file.read_bytes()
        if not data or len(data) > MAX_PROMPT_FILE_BYTES:
            raise StateError(
                "PROMPT_FILE_INVALID",
                f"prompt file must contain 1..{MAX_PROMPT_FILE_BYTES} bytes",
                {"bytes": len(data)},
            )
        actual_sha256 = sha256_bytes(data)
        if actual_sha256 != expected_sha256:
            raise StateError(
                "PROMPT_FILE_HASH_MISMATCH",
                "prompt file bytes do not match prompt_file_sha256",
                {"expected": expected_sha256, "actual": actual_sha256, "path": str(prompt_file)},
            )
        try:
            prompt_text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StateError("PROMPT_FILE_ENCODING_INVALID", "prompt file must be strict UTF-8") from exc
        if not prompt_text.strip() or "\ufffd" in prompt_text:
            raise StateError("PROMPT_FILE_ENCODING_INVALID", "prompt file is empty or contains replacement characters")
        outer_prompt = str(manifest.get("question") or manifest.get("prompt") or "")
        if outer_prompt != PROMPT_FILE_HANDOFF:
            raise StateError(
                "PROMPT_HANDOFF_INVALID",
                "file prompt transport requires the exact short composer handoff",
            )
        files = manifest.get("files") or []
        if isinstance(files, str):
            files = [files]
        resolved_files: list[Path] = []
        for item in files:
            try:
                resolved_files.append(Path(str(item)).expanduser().resolve(strict=True))
            except (OSError, RuntimeError, ValueError) as exc:
                raise StateError("ATTACHMENT_INVALID", "prompt attachment list contains an unavailable path") from exc
        if sum(path == prompt_file for path in resolved_files) != 1:
            raise StateError(
                "PROMPT_FILE_ATTACHMENT_MISMATCH",
                "prompt_file must appear exactly once in files, including when a ZIP is also attached",
                {"prompt_file": str(prompt_file)},
            )
        return {
            "transport": "file",
            "prompt_text": prompt_text,
            "prompt_sha256": actual_sha256,
            "prompt_file": str(prompt_file),
            "prompt_file_bytes": len(data),
            "dispatch_text": PROMPT_FILE_HANDOFF,
        }
    if require_file:
        raise StateError(
            "PROMPT_FILE_REQUIRED",
            "ChatGPT web submissions require an immutable prompt file; inline task prompts are forbidden",
            {"transport": transport},
        )
    for key in ("question", "prompt"):
        if manifest.get(key) is not None:
            prompt_text = str(manifest[key])
            return {
                "transport": "inline-legacy",
                "prompt_text": prompt_text,
                "prompt_sha256": sha256_bytes(prompt_text.encode("utf-8")),
                "prompt_file": None,
                "prompt_file_bytes": None,
                "dispatch_text": prompt_text,
            }
    raise StateError("PROMPT_MISSING", "manifest requires question or prompt")


def canonical_project_root(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.exists() or not path.is_dir():
        raise StateError("PROJECT_ROOT_INVALID", f"project root must be an existing directory: {path}")
    return path.resolve()


def project_key(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve())).rstrip("\\/")
    return sha256_bytes(normalized.encode("utf-8"))[:24]


def canonical_conversation_url(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not CANONICAL_CHAT_RE.fullmatch(text):
        raise StateError("CONVERSATION_URL_INVALID", "canonical https://chatgpt.com/c/<id> URL required", {"value": text})
    return text


def process_identity(pid: int | None = None) -> dict[str, Any]:
    selected = os.getpid() if pid is None else int(pid)
    creation_time: float | None = None
    alive = False
    try:
        import psutil  # type: ignore

        try:
            proc = psutil.Process(selected)
            creation_time = float(proc.create_time())
            alive = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
            return {"pid": selected, "creation_time": creation_time, "alive": alive}
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return {"pid": selected, "creation_time": None, "alive": False}
        except psutil.AccessDenied:
            return {"pid": selected, "creation_time": None, "alive": True}
    except ImportError:
        pass

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(process_query_limited_information, False, selected)
            if not handle:
                return {"pid": selected, "creation_time": None, "alive": False}
            try:
                exit_code = wintypes.DWORD()
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    alive = int(exit_code.value) == still_active
                if kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
                    creation_time = (ticks / 10_000_000) - 11_644_473_600
            finally:
                kernel32.CloseHandle(handle)
            return {"pid": selected, "creation_time": creation_time, "alive": alive}
        except Exception:
            return {"pid": selected, "creation_time": None, "alive": False}

    try:
        os.kill(selected, 0)
        alive = True
    except Exception:
        alive = False
    return {"pid": selected, "creation_time": creation_time, "alive": alive}


def same_process(identity: dict[str, Any]) -> bool:
    pid = int(identity.get("pid") or 0)
    if pid <= 0:
        return False
    current = process_identity(pid)
    if not current["alive"]:
        return False
    expected = identity.get("creation_time")
    actual = current.get("creation_time")
    if expected is None or actual is None:
        return True
    return abs(float(expected) - float(actual)) < 0.01


def _prompt_text(manifest: dict[str, Any]) -> str:
    return str(prompt_contract(manifest)["prompt_text"])


def _requested_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    mode = str(manifest.get("mode_label") or manifest.get("model") or "GPT-5.6")
    mode_key = mode.strip().casefold()
    app_policy = str(manifest.get("app_policy") or ("forbidden" if mode_key == "pro" else "required"))
    app_name = str(manifest.get("chatgpt_app_name") or manifest.get("app_name") or "").strip()
    if mode_key == "pro":
        if app_policy != "forbidden" or app_name:
            raise StateError("APP_POLICY_FORBIDDEN", "Pro requires app_policy=forbidden and no app name")
    else:
        if app_policy != "required":
            raise StateError(
                "APP_POLICY_REQUIRED",
                "every non-Pro ChatGPT mode requires app_policy=required",
            )
        if not app_name:
            raise StateError("APP_REQUIRED", "every non-Pro ChatGPT mode requires chatgpt_app_name")
    return {
        "workflow": str(manifest.get("workflow_mode") or "direct"),
        "mode": mode,
        "reasoning": manifest.get("mode_variant") or manifest.get("effort"),
        "search": bool(manifest.get("search_enabled") or manifest.get("web_search")),
        "transport": str(manifest.get("prompt_transport") or ("attachment" if manifest.get("files") else "inline")),
        "app_policy": app_policy,
    }


class RunPaths:
    def __init__(self, project_dir: Path, runs_dir: Path, run_dir: Path, state_file: Path, lock_file: Path):
        self.project_dir = project_dir
        self.runs_dir = runs_dir
        self.run_dir = run_dir
        self.state_file = state_file
        self.lock_file = lock_file
        self.parent_transition_lock = project_dir / "parent-transition.lock"


class RunStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or (Path.home() / ".codex" / "state" / "chatgpt-agbrowse")).resolve()

    def paths(self, project_root: Path, run_id: str) -> RunPaths:
        key = project_key(project_root)
        project_dir = self.root / "projects" / key
        runs_dir = project_dir / "runs"
        run_dir = runs_dir / run_id
        return RunPaths(project_dir, runs_dir, run_dir, run_dir / "run.json", project_dir / "active.lock")

    def _read_existing_lock(self, lock_file: Path) -> dict[str, Any] | None:
        if not lock_file.exists():
            return None
        try:
            return read_json(lock_file)
        except StateError:
            if not lock_file.exists():
                return None
            raise

    @staticmethod
    def _provider_failed_terminal_settled(record: dict[str, Any], state_file: Path) -> bool:
        if str(record.get("phase") or "") != "PROVIDER_FAILED_TERMINAL":
            return False
        cleanup = record.get("cleanup_evidence") if isinstance(record.get("cleanup_evidence"), dict) else {}
        state = str(cleanup.get("state") or "")
        target = str(cleanup.get("target_id") or "")
        url = str(cleanup.get("conversation_url") or "")
        if (
            cleanup.get("ok") is not True
            or state not in {"closed-and-absent", "already-absent"}
            or bool(record.get("cleanup_pending"))
            or int(record.get("owned_open_tabs") or 0) != 0
            or str(record.get("owned_tab_state") or "") != state
            or not target
            or target != str(record.get("current_target_id") or "")
            or not url
            or url != str(record.get("conversation_url") or "")
            or record.get("result") is not None
            or str(record.get("terminal_block_code") or "") != "PROVIDER_TERMINAL_ERROR_UI"
        ):
            return False
        evidence = cleanup.get("evidence") if isinstance(cleanup.get("evidence"), dict) else {}
        try:
            evidence_path = Path(str(evidence.get("path") or "")).expanduser().resolve(strict=True)
            evidence_path.relative_to(state_file.parent)
            if (
                not evidence_path.is_file()
                or evidence_path.is_symlink()
                or sha256_file(evidence_path) != str(evidence.get("sha256") or "")
            ):
                return False
        except (OSError, RuntimeError, ValueError):
            return False
        failure_events = [
            item
            for item in record.get("recovery_events") or []
            if isinstance(item, dict) and str(item.get("kind") or "") == "provider-terminal-error-ui"
        ]
        if len(failure_events) != 1:
            return False
        failure = failure_events[0]
        try:
            failure_path = Path(str(failure.get("answer_path") or "")).expanduser().resolve(strict=True)
            failure_path.relative_to(state_file.parent)
            if (
                not failure_path.is_file()
                or failure_path.is_symlink()
                or sha256_file(failure_path) != str(failure.get("answer_sha256") or "")
                or failure_path.stat().st_size != int(failure.get("answer_bytes") or -1)
            ):
                return False
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return True

    def _active_or_uncertain_records(self, runs_dir: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not runs_dir.exists():
            return rows
        for path in sorted(runs_dir.glob("*/run.json")):
            try:
                record = read_json(path)
            except StateError:
                rows.append({"run_id": path.parent.name, "phase": "STATE_UNREADABLE", "path": str(path)})
                continue
            kind = str(record.get("record_kind") or "standalone")
            phase = str(record.get("phase") or "")
            if kind == "child":
                # Children are owned by the one parent project lease and never
                # independently block a later parent after that lease settles.
                continue
            settled = {"COMPLETE", "COMPLETE_SUPERSEDED", "CANCELLED_PRE_SUBMISSION", "ABANDONED_UNCERTAIN"} | PARENT_TERMINAL_PHASES
            if phase == "PROVIDER_FAILED_TERMINAL" and self._provider_failed_terminal_settled(record, path):
                continue
            if phase not in settled:
                rows.append({"run_id": record.get("run_id"), "phase": record.get("phase"), "path": str(path)})
        return rows

    @staticmethod
    def _owner_observation(record: dict[str, Any]) -> dict[str, Any]:
        stored = record.get("owner") if isinstance(record.get("owner"), dict) else {}
        pid = int(stored.get("pid") or 0)
        observed = process_identity(pid) if pid > 0 else {"pid": pid, "creation_time": None, "alive": False}
        return {
            "stored": {
                "pid": pid,
                "creation_time": stored.get("creation_time"),
                "alive": stored.get("alive"),
            },
            "observed": observed,
            "same_process": same_process(stored),
        }

    @staticmethod
    def _crossed_send_boundary(record: dict[str, Any]) -> bool:
        if str(record.get("phase") or "") in UNCERTAIN_OR_SUBMITTED_PHASES | {"SEND_REJECTED"}:
            return True
        if any(
            str(item.get("to") or "") == "SEND_STARTED"
            for item in (record.get("phase_events") or [])
            if isinstance(item, dict)
        ):
            return True
        return bool(
            record.get("session_id")
            or record.get("conversation_url")
            or record.get("submission_receipt")
            or record.get("result")
        )

    @classmethod
    def _verified_pre_submit_target_cleanup(
        cls,
        state_file: Path,
        record: dict[str, Any],
    ) -> dict[str, Any] | None:
        target_id = str(record.get("current_target_id") or "")
        if not target_id or cls._crossed_send_boundary(record):
            return None
        expected_path = (state_file.parent / "tab-lifecycle.json").resolve()
        allowed_recovery_kinds = {
            "app-chat-surface-preparation-failed",
            "app-composer-preparation-failed",
            "app-composer-target-activation-failed",
            "app-selection-evidence-missing",
            "pre-submit-command-budget-exceeded",
            "pre-submit-rejection",
            "prepared-target-evidence-failed",
            "verified-pre-submit-tab-cleanup",
        }
        for recovery in reversed(record.get("recovery_events") or []):
            if not isinstance(recovery, dict) or str(recovery.get("kind") or "") not in allowed_recovery_kinds:
                continue
            cleanup = recovery.get("cleanup")
            if not isinstance(cleanup, dict):
                continue
            if not (
                cleanup.get("ok") is True
                and cleanup.get("state") == "closed-and-absent"
                and str(cleanup.get("target_id") or "") == target_id
            ):
                continue
            evidence = cleanup.get("evidence")
            if not isinstance(evidence, dict):
                continue
            try:
                evidence_path = Path(str(evidence.get("path") or "")).expanduser().resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
            if evidence_path != expected_path or not evidence_path.is_file() or evidence_path.is_symlink():
                continue
            expected_sha256 = str(evidence.get("sha256") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or sha256_file(evidence_path) != expected_sha256:
                continue
            try:
                ledger = read_json(evidence_path)
            except StateError:
                continue
            if not (
                ledger.get("schema") == "codex.chatgpt.agbrowse-tab-lifecycle/v1"
                and ledger.get("run_id") == record.get("run_id")
                and ledger.get("project_key") == record.get("project_key")
                and ledger.get("manifest_sha256") == record.get("manifest_sha256")
            ):
                continue
            matching_events = [
                item
                for item in (ledger.get("events") or [])
                if isinstance(item, dict)
                and item.get("kind") == "cleanup"
                and item.get("ok") is True
                and item.get("state") == "closed-and-absent"
                and str(item.get("target_id") or "") == target_id
            ]
            if not matching_events:
                continue
            event = matching_events[-1]
            nested_event = evidence.get("event")
            compared_keys = (
                "kind",
                "reason",
                "url",
                "ok",
                "state",
                "target_id",
                "before_count",
                "after_count",
                "before_sha256",
                "after_sha256",
                "close_stdout_sha256",
            )
            if not isinstance(nested_event, dict) or any(
                nested_event.get(key) != event.get(key) for key in compared_keys
            ) or any(
                cleanup.get(key) != event.get(key) for key in compared_keys if key in cleanup
            ):
                continue
            before_count = event.get("before_count")
            after_count = event.get("after_count")
            if not (
                isinstance(before_count, int)
                and isinstance(after_count, int)
                and before_count == after_count + 1
                and re.fullmatch(r"[0-9a-f]{64}", str(event.get("before_sha256") or ""))
                and re.fullmatch(r"[0-9a-f]{64}", str(event.get("after_sha256") or ""))
                and re.fullmatch(r"[0-9a-f]{64}", str(event.get("close_stdout_sha256") or ""))
            ):
                continue
            return {
                "path": str(evidence_path),
                "sha256": expected_sha256,
                "target_id": target_id,
                "reason": event.get("reason"),
                "before_count": before_count,
                "after_count": after_count,
            }
        return None

    @classmethod
    def _safe_stale_pre_submission(cls, state_file: Path, record: dict[str, Any]) -> bool:
        return bool(
            str(record.get("phase") or "") in SAFE_STALE_PRE_SUBMISSION_PHASES
            and not cls._crossed_send_boundary(record)
            and (
                not record.get("current_target_id")
                or cls._verified_pre_submit_target_cleanup(state_file, record) is not None
            )
        )

    @staticmethod
    def _complete_result_capture_valid(state_file: Path, record: dict[str, Any]) -> bool:
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        try:
            result_path = Path(str(result.get("path") or "")).expanduser().resolve(strict=True)
            result_path.relative_to(state_file.parent.resolve(strict=True))
            if not result_path.is_file() or result_path.is_symlink():
                return False
            actual_bytes = result_path.stat().st_size
            actual_sha256 = sha256_file(result_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return bool(
            actual_bytes > 0
            and actual_bytes == int(result.get("bytes") or -1)
            and re.fullmatch(r"[0-9a-f]{64}", str(result.get("sha256") or ""))
            and actual_sha256 == str(result.get("sha256") or "")
            and str(result.get("provider_status") or "").lower()
            in {"complete", "completed", "done", "response_ready", "history-adjudicated-terminal"}
            and isinstance(result.get("evidence"), dict)
            and bool(result["evidence"])
        )

    def _duplicate_completed_owner_proof(self, state_file: Path, record: dict[str, Any]) -> dict[str, Any] | None:
        if (
            str(record.get("phase") or "") != "BLOCKED_TARGET_AMBIGUOUS"
            or str(record.get("terminal_block_code") or "") != "CONVERSATION_URL_OWNED_BY_FOREIGN_RUN"
        ):
            return None
        collision = next(
            (
                item
                for item in reversed(record.get("recovery_events") or [])
                if isinstance(item, dict)
                and str(item.get("kind") or "") == "conversation-url-owned-by-foreign-run"
            ),
            None,
        )
        if not isinstance(collision, dict):
            return None
        candidate_recovery = collision.get("candidate_recovery")
        if not isinstance(candidate_recovery, dict) or str(candidate_recovery.get("kind") or "") not in {
            "doctor-reattach",
            "history-fingerprint-match",
        }:
            return None
        foreign = collision.get("foreign_owner")
        if not isinstance(foreign, dict):
            return None
        foreign_run_id = str(foreign.get("run_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", foreign_run_id):
            return None
        owner_state_file = state_file.parent.parent / foreign_run_id / "run.json"
        try:
            owner = read_json(owner_state_file)
        except StateError:
            return None
        candidate_url = str(collision.get("conversation_url") or "")
        if (
            str(owner.get("run_id") or "") != foreign_run_id
            or str(owner.get("phase") or "") != "COMPLETE"
            or str(owner.get("project_key") or "") != str(record.get("project_key") or "")
            or str(owner.get("project_root") or "") != str(record.get("project_root") or "")
            or str(owner.get("prompt_sha256") or "") != str(record.get("prompt_sha256") or "")
            or str(owner.get("conversation_url") or "") != candidate_url
            or not record.get("session_id")
            or not self._complete_result_capture_valid(owner_state_file, owner)
        ):
            return None
        result = dict(owner["result"])
        return {
            "schema": "codex.chatgpt.duplicate-completed-owner-proof/v1",
            "superseded_run_id": record.get("run_id"),
            "authoritative_run_id": foreign_run_id,
            "project_key": record.get("project_key"),
            "prompt_sha256": record.get("prompt_sha256"),
            "conversation_url": candidate_url,
            "candidate_recovery_kind": candidate_recovery.get("kind"),
            "authoritative_result": {
                "path": result.get("path"),
                "sha256": result.get("sha256"),
                "bytes": result.get("bytes"),
                "provider_status": result.get("provider_status"),
            },
        }

    def _settle_duplicate_completed_owner(
        self,
        state_file: Path,
        record: dict[str, Any],
        lock_file: Path,
        lock: dict[str, Any],
        owner_observation: dict[str, Any],
    ) -> dict[str, Any]:
        if owner_observation.get("same_process"):
            raise StateError("ACTIVE_PROJECT_OWNER", "the recorded owner process is still the same live process")
        proof = self._duplicate_completed_owner_proof(state_file, record)
        if proof is None:
            raise StateError(
                "DUPLICATE_COMPLETE_OWNER_UNPROVEN",
                "completed duplicate URL ownership could not be proven exactly",
            )
        evidence_path = state_file.parent / "duplicate-completed-owner-proof.json"
        write_json_atomic(evidence_path, proof)
        now = utc_now()
        prior_phase = str(record.get("phase") or "")
        descriptor = {
            "path": str(evidence_path),
            "sha256": sha256_file(evidence_path),
            "authoritative_run_id": proof["authoritative_run_id"],
            "conversation_url": proof["conversation_url"],
        }
        record.setdefault("phase_events", []).append({"from": prior_phase, "to": "COMPLETE_SUPERSEDED", "at": now})
        record.setdefault("recovery_events", []).append(
            {
                "at": now,
                "kind": "duplicate-completed-owner-settled",
                "owner_observation": owner_observation,
                "proof": descriptor,
            }
        )
        record["superseded_complete"] = descriptor
        record["recovery_count"] = int(record.get("recovery_count") or 0) + 1
        record["phase"] = "COMPLETE_SUPERSEDED"
        record["phase_at"] = now
        record["updated_at"] = now
        record["terminal_block_code"] = None
        write_json_atomic(state_file, record)

        current_lock = read_json(lock_file)
        if (
            current_lock.get("run_id") != record.get("run_id")
            or current_lock.get("manifest_sha256") != lock.get("manifest_sha256")
            or current_lock.get("owner", {}).get("nonce") != record.get("owner", {}).get("nonce")
            or current_lock.get("owner", {}).get("epoch") != record.get("owner", {}).get("epoch")
        ):
            raise StateError(
                "BLOCKED_OWNER_MISMATCH",
                "project lease changed while settling duplicate completed ownership",
            )
        lock_file.unlink()
        return record

    def _cancel_stale_pre_submission(
        self,
        state_file: Path,
        record: dict[str, Any],
        lock_file: Path,
        lock: dict[str, Any],
        owner_observation: dict[str, Any],
    ) -> dict[str, Any]:
        if owner_observation.get("same_process"):
            raise StateError("ACTIVE_PROJECT_OWNER", "the recorded owner process is still the same live process")
        cleanup_evidence = self._verified_pre_submit_target_cleanup(state_file, record)
        if not self._safe_stale_pre_submission(state_file, record):
            raise StateError(
                "STALE_OWNER_NOT_SAFE_TO_CANCEL",
                "stale run is not a proven pre-submission run",
                {
                    "run_id": record.get("run_id"),
                    "phase": record.get("phase"),
                    "session_id": record.get("session_id"),
                    "target_id": record.get("current_target_id"),
                    "conversation_url": record.get("conversation_url"),
                },
            )
        now = utc_now()
        prior_phase = str(record.get("phase") or "")
        record.setdefault("phase_events", []).append({"from": prior_phase, "to": "CANCELLED_PRE_SUBMISSION", "at": now})
        record.setdefault("recovery_events", []).append(
            {
                "at": now,
                "kind": "stale-owner-pre-submission-reconciled",
                "owner_observation": owner_observation,
                "manifest_current_sha256": (
                    sha256_file(Path(str(record.get("manifest_path"))))
                    if Path(str(record.get("manifest_path"))).is_file()
                    else None
                ),
                "manifest_recorded_sha256": record.get("manifest_sha256"),
                "pre_submit_target_cleanup": cleanup_evidence,
            }
        )
        record["recovery_count"] = int(record.get("recovery_count") or 0) + 1
        record["phase"] = "CANCELLED_PRE_SUBMISSION"
        record["phase_at"] = now
        record["updated_at"] = now
        record["terminal_block_code"] = None
        if cleanup_evidence is not None:
            record["current_target_id"] = None
        write_json_atomic(state_file, record)

        current_lock = read_json(lock_file)
        if (
            current_lock.get("run_id") != record.get("run_id")
            or current_lock.get("manifest_sha256") != lock.get("manifest_sha256")
            or current_lock.get("owner", {}).get("nonce") != record.get("owner", {}).get("nonce")
            or current_lock.get("owner", {}).get("epoch") != record.get("owner", {}).get("epoch")
        ):
            raise StateError(
                "BLOCKED_OWNER_MISMATCH",
                "project lease changed while reconciling a stale pre-submission run",
            )
        lock_file.unlink()
        return record

    @staticmethod
    def _user_stop_authorization(record: dict[str, Any], authorization: dict[str, Any]) -> dict[str, Any]:
        required_true = (
            "explicit_user_request",
            "mutation_may_have_occurred",
            "duplicate_risk_acknowledged",
        )
        missing_true = [key for key in required_true if authorization.get(key) is not True]
        expected = {
            "run_id": record.get("run_id"),
            "project_root": record.get("project_root"),
            "session_id": record.get("session_id"),
            "target_id": record.get("current_target_id"),
            "conversation_url": record.get("conversation_url"),
        }
        mismatches = {
            key: {"expected": value, "actual": authorization.get(key)}
            for key, value in expected.items()
            if authorization.get(key) != value
        }
        if authorization.get("schema") != "codex.chatgpt.user-stop-authorization/v1" or missing_true or mismatches:
            raise StateError(
                "USER_STOP_AUTHORIZATION_INVALID",
                "an exact, explicit user-stop authorization bound to this run is required",
                {"missing_true": missing_true, "mismatches": mismatches},
            )
        reason = str(authorization.get("reason") or "").strip()
        reason_bytes = reason.encode("utf-8")
        if (
            not reason
            or len(reason_bytes) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in reason)
        ):
            raise StateError(
                "USER_STOP_AUTHORIZATION_INVALID",
                "user-stop reason must be 1..512 UTF-8 bytes without control characters",
            )
        clean = dict(authorization)
        clean["reason"] = reason
        return clean

    def begin_user_stop(
        self,
        run_dir: str | os.PathLike[str],
        *,
        authorization: dict[str, Any],
    ) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if record.get("phase") == "ABANDONED_UNCERTAIN":
            return record
        lock_file, lock = self._verify_lock(state_file, record)
        current = str(record.get("phase") or "")
        if current == "USER_STOP_REQUESTED":
            return record
        if current in {"COMPLETE", "CANCELLED_PRE_SUBMISSION"}:
            raise StateError("USER_STOP_PHASE_INVALID", f"cannot abandon terminal run in phase {current}")
        if not self._crossed_send_boundary(record):
            raise StateError(
                "USER_STOP_PHASE_INVALID",
                "pre-submission runs must use the supported pre-submission cancellation path",
                {"phase": current},
            )
        clean = self._user_stop_authorization(record, authorization)
        now = utc_now()
        clean["recorded_at"] = now
        auth_sha256 = sha256_bytes(
            json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        record["user_stop"] = {
            "status": "requested",
            "authorization": clean,
            "authorization_sha256": auth_sha256,
            "challenge_nonce": uuid.uuid4().hex,
            "confirmation": None,
        }
        record.setdefault("phase_events", []).append({"from": current, "to": "USER_STOP_REQUESTED", "at": now})
        record.setdefault("recovery_events", []).append(
            {
                "at": now,
                "kind": "explicit-user-stop-requested",
                "authorization_sha256": auth_sha256,
                "source_phase": current,
            }
        )
        record["recovery_count"] = int(record.get("recovery_count") or 0) + 1
        record["phase"] = "USER_STOP_REQUESTED"
        record["phase_at"] = now
        record["updated_at"] = now
        record["terminal_block_code"] = "USER_STOP_CONFIRMATION_PENDING"
        write_json_atomic(state_file, record)
        lock.update(
            {
                "phase": "USER_STOP_REQUESTED",
                "session_id": record.get("session_id"),
                "target_id": record.get("current_target_id"),
                "conversation_url": record.get("conversation_url"),
                "heartbeat_at": now,
            }
        )
        write_json_atomic(lock_file, lock)
        return record

    def finalize_user_stop(
        self,
        run_dir: str | os.PathLike[str],
        *,
        confirmation: dict[str, Any],
    ) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if record.get("phase") == "ABANDONED_UNCERTAIN":
            return record
        lock_file, lock = self._verify_lock(state_file, record)
        if record.get("phase") != "USER_STOP_REQUESTED":
            raise StateError("USER_STOP_PHASE_INVALID", "finalization requires USER_STOP_REQUESTED")

        try:
            evidence_path = Path(str(confirmation.get("path") or "")).expanduser().resolve(strict=True)
            evidence_path.relative_to(state_file.parent)
        except (OSError, RuntimeError, ValueError) as exc:
            raise StateError("USER_STOP_EVIDENCE_INVALID", "confirmation evidence must be inside the exact run directory") from exc
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise StateError("USER_STOP_EVIDENCE_INVALID", "confirmation evidence must be a regular non-symlink file")
        actual_hash = sha256_file(evidence_path)
        actual_bytes = evidence_path.stat().st_size
        if actual_hash != str(confirmation.get("sha256") or "") or actual_bytes != int(confirmation.get("bytes") or -1):
            raise StateError(
                "USER_STOP_EVIDENCE_INVALID",
                "confirmation evidence hash or byte count does not match",
                {"actual_sha256": actual_hash, "actual_bytes": actual_bytes},
            )
        evidence = read_json(evidence_path)
        classification = evidence.get("classification") if isinstance(evidence.get("classification"), dict) else {}
        user_stop = record.get("user_stop") if isinstance(record.get("user_stop"), dict) else {}
        expected_identity = {
            "run_id": record.get("run_id"),
            "session_id": record.get("session_id"),
            "target_id": record.get("current_target_id"),
            "conversation_url": record.get("conversation_url"),
            "authorization_sha256": user_stop.get("authorization_sha256"),
            "challenge_nonce": user_stop.get("challenge_nonce"),
        }
        mismatches = {
            key: {"expected": value, "actual": evidence.get(key)}
            for key, value in expected_identity.items()
            if evidence.get(key) != value
        }
        valid_classification = bool(
            evidence.get("schema") == "codex.chatgpt.user-stop-evidence/v1"
            and evidence.get("mutation_may_have_occurred") is True
            and evidence.get("tab_closed") is False
            and classification.get("identity_match") is True
            and classification.get("generation_active") is False
            and (
                classification.get("terminal_session") is True
                or classification.get("identity_missing_owner_dead") is True
            )
        )
        if mismatches or not valid_classification:
            raise StateError(
                "USER_STOP_EVIDENCE_INVALID",
                "evidence does not prove the exact run is no longer generating",
                {"mismatches": mismatches, "classification": classification},
            )

        now = utc_now()
        descriptor = {
            "path": str(evidence_path),
            "sha256": actual_hash,
            "bytes": actual_bytes,
        }
        user_stop["status"] = "confirmed-abandoned-uncertain"
        user_stop["confirmation"] = descriptor
        user_stop["confirmed_at"] = now
        record["user_stop"] = user_stop
        record.setdefault("phase_events", []).append(
            {"from": "USER_STOP_REQUESTED", "to": "ABANDONED_UNCERTAIN", "at": now}
        )
        record.setdefault("recovery_events", []).append(
            {
                "at": now,
                "kind": "explicit-user-abandoned-uncertain",
                "confirmation": descriptor,
                "mutation_may_have_occurred": True,
            }
        )
        record["recovery_count"] = int(record.get("recovery_count") or 0) + 1
        record["phase"] = "ABANDONED_UNCERTAIN"
        record["phase_at"] = now
        record["updated_at"] = now
        record["terminal_block_code"] = None
        write_json_atomic(state_file, record)

        current_lock = read_json(lock_file)
        if (
            current_lock.get("run_id") != record.get("run_id")
            or current_lock.get("manifest_sha256") != lock.get("manifest_sha256")
            or current_lock.get("owner", {}).get("nonce") != record.get("owner", {}).get("nonce")
            or current_lock.get("owner", {}).get("epoch") != record.get("owner", {}).get("epoch")
        ):
            raise StateError("BLOCKED_OWNER_MISMATCH", "project lease changed while finalizing user stop")
        lock_file.unlink()
        return record

    def reconcile_project_lock(
        self,
        project_root: str | os.PathLike[str],
        *,
        apply_safe_pre_submission: bool = False,
    ) -> dict[str, Any]:
        root = canonical_project_root(project_root)
        paths = self.paths(root, "unused")
        lock = self._read_existing_lock(paths.lock_file)
        records = self._active_or_uncertain_records(paths.runs_dir)
        if lock is None:
            if records:
                return {
                    "ok": False,
                    "state": "PROJECT_LOCK_STATE_AMBIGUOUS",
                    "reason": "active or uncertain records exist without the project lock",
                    "records": records[:10],
                }
            return {"ok": True, "state": "CLEAR", "project_root": str(root), "changed": False}

        run_id = str(lock.get("run_id") or "")
        if not run_id:
            return {
                "ok": False,
                "state": "PROJECT_LOCK_STATE_AMBIGUOUS",
                "reason": "project lock has no run_id",
            }
        state_file = paths.runs_dir / run_id / "run.json"
        if not state_file.is_file():
            return {
                "ok": False,
                "state": "PROJECT_LOCK_STATE_AMBIGUOUS",
                "reason": "project lock points to a missing run record",
                "run_id": run_id,
            }
        _, record = self.load(state_file)
        try:
            verified_lock_file, verified_lock = self._verify_lock(state_file, record)
        except StateError as exc:
            return {
                "ok": False,
                "state": "PROJECT_LOCK_STATE_AMBIGUOUS",
                "reason": exc.code,
                "run_id": run_id,
            }
        observation = self._owner_observation(record)
        evidence = {
            "run_id": run_id,
            "phase": record.get("phase"),
            "session_id": record.get("session_id"),
            "target_id": record.get("current_target_id"),
            "conversation_url": record.get("conversation_url"),
            "owner_observation": observation,
        }
        if str(record.get("phase") or "") == "USER_STOP_REQUESTED":
            return {
                "ok": False,
                "state": "USER_STOP_CONFIRMATION_PENDING",
                "changed": False,
                "reason": "the exact run must be observed terminal before its project lock can be released",
                "supported_abandon_command": (
                    f'python "{Path(__file__).resolve().with_name("chatgpt_agbrowse_run.py")}" '
                    f'--abandon-uncertain-run "{state_file.parent}" --explicit-user-request '
                    f'--reason "confirm explicit user stop"'
                ),
                **evidence,
            }
        if observation["same_process"]:
            return {"ok": False, "state": "ACTIVE_PROJECT_OWNER", "changed": False, **evidence}

        duplicate_proof = self._duplicate_completed_owner_proof(state_file, record)
        if duplicate_proof is not None:
            duplicate_evidence = {
                **evidence,
                "authoritative_run_id": duplicate_proof["authoritative_run_id"],
                "conversation_url": duplicate_proof["conversation_url"],
            }
            if not apply_safe_pre_submission:
                return {
                    "ok": True,
                    "state": "STALE_DUPLICATE_COMPLETE_OWNER_SAFE_TO_SETTLE",
                    "changed": False,
                    **duplicate_evidence,
                }
            settled = self._settle_duplicate_completed_owner(
                state_file,
                record,
                verified_lock_file,
                verified_lock,
                observation,
            )
            return {
                "ok": True,
                "state": "STALE_DUPLICATE_COMPLETE_OWNER_SETTLED",
                "changed": True,
                **duplicate_evidence,
                "phase": settled.get("phase"),
            }

        if str(record.get("phase") or "") in {"COMPLETE", "COMPLETE_SUPERSEDED", "CANCELLED_PRE_SUBMISSION", "ABANDONED_UNCERTAIN"}:
            if not apply_safe_pre_submission:
                return {"ok": True, "state": "TERMINAL_ORPHAN_LOCK_DETECTED", "changed": False, **evidence}
            verified_lock_file.unlink()
            return {"ok": True, "state": "TERMINAL_ORPHAN_LOCK_REMOVED", "changed": True, **evidence}

        if self._safe_stale_pre_submission(state_file, record):
            if not apply_safe_pre_submission:
                return {"ok": True, "state": "STALE_PRE_SUBMISSION_SAFE_TO_CANCEL", "changed": False, **evidence}
            cancelled = self._cancel_stale_pre_submission(
                state_file,
                record,
                verified_lock_file,
                verified_lock,
                observation,
            )
            return {
                "ok": True,
                "state": "STALE_PRE_SUBMISSION_CANCELLED",
                "changed": True,
                **evidence,
                "phase": cancelled.get("phase"),
            }

        if self._crossed_send_boundary(record):
            return {
                "ok": False,
                "state": "STALE_OWNER_UNRESOLVED_SUBMISSION",
                "changed": False,
                "reason": "dead owner is after or at the send boundary; recover only the original exact run",
                "supported_abandon_command": (
                    f'python "{Path(__file__).resolve().with_name("chatgpt_agbrowse_run.py")}" '
                    f'--abandon-uncertain-run "{state_file.parent}" --explicit-user-request '
                    f'--reason "explicitly abandon stale uncertain run"'
                ),
                **evidence,
            }
        if record.get("current_target_id"):
            return {
                "ok": False,
                "state": "STALE_OWNER_PRE_SUBMIT_TARGET_PRESENT",
                "changed": False,
                "reason": "an owned pre-submit target requires exact target cleanup evidence before release",
                **evidence,
            }
        return {
            "ok": False,
            "state": "PROJECT_LOCK_STATE_AMBIGUOUS",
            "changed": False,
            "reason": "stale owner could not be classified safely",
            **evidence,
        }

    def _parent_children(self, runs_dir: Path, parent_run_id: str) -> list[tuple[Path, dict[str, Any]]]:
        children: list[tuple[Path, dict[str, Any]]] = []
        if not runs_dir.is_dir():
            return children
        for state_file in sorted(runs_dir.glob("*/run.json")):
            try:
                record = read_json(state_file)
            except StateError:
                continue
            if (
                str(record.get("record_kind") or "") == "child"
                and str(record.get("parent_run_id") or "") == parent_run_id
            ):
                children.append((state_file, record))
        return children

    def create_parent_workflow(
        self,
        *,
        project_root: str | os.PathLike[str],
        manifest_path: str | os.PathLike[str],
        workflow_id: str,
        agbrowse_contract: dict[str, Any] | None = None,
        owner_pid: int | None = None,
    ) -> dict[str, Any]:
        root = canonical_project_root(project_root)
        manifest_file = Path(manifest_path).expanduser().resolve()
        if not manifest_file.is_file():
            raise StateError("MANIFEST_MISSING", f"manifest file missing: {manifest_file}")
        manifest = load_manifest(manifest_file)
        validate_web_multi_parent_manifest(manifest)
        if str(manifest.get("workflow_id") or "") != str(workflow_id or ""):
            raise StateError("PARENT_WORKFLOW_ID_MISMATCH", "parent workflow_id does not match its manifest")
        manifest_root = canonical_project_root(str(manifest.get("project_root") or root))
        if manifest_root != root:
            raise StateError("PARENT_PROJECT_ROOT_MISMATCH", "parent manifest project root is not exact")
        manifest_hash = sha256_file(manifest_file)
        question_hash = sha256_bytes(
            json.dumps(manifest.get("question"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        run_id = uuid.uuid4().hex
        paths = self.paths(root, run_id)
        paths.project_dir.mkdir(parents=True, exist_ok=True)
        with exclusive_state_lock(paths.parent_transition_lock):
            existing_lock = self._read_existing_lock(paths.lock_file)
            existing_records = self._active_or_uncertain_records(paths.runs_dir)
            if existing_lock or existing_records:
                raise StateError(
                    "SAME_PROJECT_ACTIVE_OR_UNCERTAIN",
                    "same project already has an active or uncertain parent/standalone workflow",
                    {"lock": existing_lock, "records": existing_records[:10]},
                )
            identity = process_identity(owner_pid)
            owner_nonce = uuid.uuid4().hex
            lease_nonce = uuid.uuid4().hex
            epoch = int(time.time_ns())
            now = utc_now()
            owner = {**identity, "nonce": owner_nonce, "epoch": epoch}
            lock = {
                "schema": SCHEMA,
                "record_kind": "parent",
                "run_id": run_id,
                "parent_run_id": run_id,
                "project_root": str(root),
                "project_key": project_key(root),
                "workflow_id": workflow_id,
                "manifest_sha256": manifest_hash,
                "lease_nonce": lease_nonce,
                "owner": owner,
                "phase": "PARENT_ACTIVE",
                "recovery_required": False,
                "heartbeat_at": now,
            }
            try:
                fd = os.open(paths.lock_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(lock, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
            except FileExistsError as exc:
                raise StateError("SAME_PROJECT_ACTIVE_OR_UNCERTAIN", "project parent lock appeared during creation") from exc
            record = {
                "schema": SCHEMA,
                "record_kind": "parent",
                "run_id": run_id,
                "parent_run_id": run_id,
                "workflow_id": workflow_id,
                "lease_nonce": lease_nonce,
                "project_root": str(root),
                "project_key": project_key(root),
                "manifest_path": str(manifest_file),
                "manifest_sha256": manifest_hash,
                "prompt_sha256": question_hash,
                "requested": {"workflow": "web-multi-gpt", "mode": "GPT-5.6", "app_policy": "required"},
                "agbrowse": dict(agbrowse_contract or {}),
                "owner": owner,
                "created_at": now,
                "updated_at": now,
                "phase": "PARENT_ACTIVE",
                "phase_at": now,
                "phase_events": [
                    {"from": None, "to": "PARENT_CREATED", "at": now},
                    {"from": "PARENT_CREATED", "to": "PARENT_ACTIVE", "at": now},
                ],
                "children": [],
                "result": None,
                "failure": None,
                "recovery_required": False,
                "runtime_recovery_failure": None,
                "owned_open_tabs": 0,
            }
            try:
                write_json_atomic(paths.state_file, record)
            except Exception:
                try:
                    paths.lock_file.unlink()
                except OSError:
                    pass
                raise
        return {**record, "run_dir": str(paths.run_dir), "state_file": str(paths.state_file)}

    def create_child_run(
        self,
        *,
        parent_run_dir: str | os.PathLike[str],
        manifest_path: str | os.PathLike[str],
        agbrowse_contract: dict[str, Any],
        role: str,
        lane: int,
        iteration: int,
        stage_id: str,
        send_limit: int = 1,
        owner_pid: int | None = None,
    ) -> dict[str, Any]:
        parent_state_file, initial_parent = self.load(parent_run_dir)
        if str(initial_parent.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "child creation requires an exact parent run")
        root = canonical_project_root(initial_parent["project_root"])
        parent_paths = self.paths(root, str(initial_parent["run_id"]))
        manifest_file = Path(manifest_path).expanduser().resolve()
        if not manifest_file.is_file():
            raise StateError("MANIFEST_MISSING", f"child manifest file missing: {manifest_file}")
        manifest = load_manifest(manifest_file)
        prompt = prompt_contract(manifest, require_file=True)
        if int(send_limit) != 1:
            raise StateError("CHILD_SEND_LIMIT_INVALID", "web Multi-GPT children require send_limit=1")
        if str(manifest.get("app_policy") or "") != "required":
            raise StateError("CHILD_APP_POLICY_INVALID", "web Multi-GPT child app_policy must be required")
        manifest_root = canonical_project_root(str(manifest.get("project_root") or root))
        if manifest_root != root:
            raise StateError("CHILD_PROJECT_ROOT_MISMATCH", "child manifest project root is not exact")
        correlation = manifest.get("workflow_correlation") if isinstance(manifest.get("workflow_correlation"), dict) else {}
        if str(correlation.get("workflow_id") or manifest.get("workflow_id") or "") != str(initial_parent.get("workflow_id") or ""):
            raise StateError("CHILD_WORKFLOW_ID_MISMATCH", "child workflow binding does not match the parent")

        with exclusive_state_lock(parent_paths.parent_transition_lock):
            parent = read_json(parent_state_file)
            if not parent_paths.lock_file.is_file():
                raise StateError("PARENT_NOT_ACTIVE", "parent project lock is absent; child creation is forbidden")
            lock = read_json(parent_paths.lock_file)
            if (
                str(parent.get("phase") or "") != "PARENT_ACTIVE"
                or str(lock.get("phase") or "") != "PARENT_ACTIVE"
                or bool(parent.get("recovery_required"))
                or bool(lock.get("recovery_required"))
                or str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(parent.get("run_id") or "")
                or str(lock.get("lease_nonce") or "") != str(parent.get("lease_nonce") or "")
                or str(lock.get("workflow_id") or "") != str(parent.get("workflow_id") or "")
                or str(lock.get("manifest_sha256") or "") != str(parent.get("manifest_sha256") or "")
            ):
                raise StateError(
                    "PARENT_NOT_ACTIVE",
                    "child creation is forbidden unless the exact parent is active and recovery-free",
                    {
                        "parent_phase": parent.get("phase"),
                        "lock_phase": lock.get("phase"),
                        "recovery_required": bool(parent.get("recovery_required") or lock.get("recovery_required")),
                    },
                )
            existing_stages = {
                str(child.get("stage_id") or "")
                for _, child in self._parent_children(parent_paths.runs_dir, str(parent["run_id"]))
            }
            if not stage_id or stage_id in existing_stages:
                raise StateError("CHILD_STAGE_DUPLICATE", "stage_id must be nonempty and unique within the parent")

            run_id = uuid.uuid4().hex
            child_paths = self.paths(root, run_id)
            identity = process_identity(owner_pid)
            now = utc_now()
            manifest_hash = sha256_file(manifest_file)
            prompt_hash = str(prompt["prompt_sha256"])
            alias_name = f"prompt-{run_id}.txt"
            alias_path = child_paths.run_dir / alias_name
            owner = {**identity, "nonce": uuid.uuid4().hex, "epoch": int(time.time_ns())}
            record = {
                "schema": SCHEMA,
                "record_kind": "child",
                "run_id": run_id,
                "parent_run_id": str(parent["run_id"]),
                "parent_workflow_id": str(parent["workflow_id"]),
                "parent_lease_nonce": str(parent["lease_nonce"]),
                "role": str(role),
                "lane": int(lane),
                "iteration": int(iteration),
                "stage_id": str(stage_id),
                "send_limit": 1,
                "send_attempt_count": 0,
                "send_claim": None,
                "project_root": str(root),
                "project_key": project_key(root),
                "manifest_path": str(manifest_file),
                "manifest_sha256": manifest_hash,
                "prompt_sha256": prompt_hash,
                "recovery_identity": {
                    "schema": "codex.chatgpt.recovery-identity/v1",
                    "token": run_id,
                    "attachment_name": alias_name,
                    "attachment_path": str(alias_path),
                    "attachment_sha256": prompt_hash,
                    "source_prompt_path": str(prompt["prompt_file"]),
                },
                "requested": _requested_contract(manifest),
                "agbrowse": dict(agbrowse_contract),
                "owner": owner,
                "created_at": now,
                "updated_at": now,
                "phase": "CREATED",
                "phase_at": now,
                "session_id": None,
                "current_target_id": None,
                "conversation_url": None,
                "submission_receipt": None,
                "result": None,
                "terminal_block_code": None,
                "recovery_count": 0,
                "phase_events": [{"from": None, "to": "CREATED", "at": now}],
                "target_rebind_events": [],
                "recovery_events": [],
                "app_evidence_refs": [],
                "selection_evidence_refs": [],
                "cleanup_pending": False,
                "owned_tab_state": None,
                "owned_open_tabs": 0,
                "pre_submit_retry_count": 0,
                "pre_submit_retry_authority": None,
            }
            staging = parent_paths.runs_dir / f".child-{run_id}.tmp"
            try:
                staging.mkdir(parents=True, exist_ok=False)
                prompt_bytes = Path(str(prompt["prompt_file"])).read_bytes()
                if sha256_bytes(prompt_bytes) != prompt_hash:
                    raise StateError("CHILD_PROMPT_HASH_MISMATCH", "child prompt changed before durable creation")
                (staging / alias_name).write_bytes(prompt_bytes)
                write_json_atomic(staging / "run.json", record)
                os.replace(staging, child_paths.run_dir)
            except Exception:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                raise

            children = list(parent.get("children") or [])
            children.append({"run_id": run_id, "stage_id": stage_id, "role": role, "lane": int(lane), "iteration": int(iteration)})
            parent["children"] = children
            parent["updated_at"] = utc_now()
            write_json_atomic(parent_state_file, parent)
        return {**record, "run_dir": str(child_paths.run_dir), "state_file": str(child_paths.state_file)}

    def assert_child_send_available(self, run_dir: str | os.PathLike[str]) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "standalone") != "child":
            return record
        self._verify_lock(state_file, record)
        claim_file = state_file.parent / "send.claim"
        authority = record.get("pre_submit_retry_authority")
        if claim_file.exists() and isinstance(authority, dict):
            candidate = self.pre_submit_retry_candidate(run_dir)
            if (
                authority.get("eligible") is True
                and authority.get("consumed_at") is None
                and str(authority.get("run_id") or "") == str(record.get("run_id") or "")
                and str(authority.get("parent_run_id") or "") == str(record.get("parent_run_id") or "")
                and str(authority.get("claim_sha256") or "") == str(candidate["claim_sha256"])
                and str(authority.get("send_stderr_sha256") or "") == str(candidate["send_stderr_sha256"])
                and str(authority.get("send_stdout_sha256") or "") == str(candidate["send_stdout_sha256"])
                and str(authority.get("replacement_target_id") or authority.get("cleanup_target_id") or "")
                == str(record.get("current_target_id") or "")
                and (
                    not authority.get("replacement_target_id")
                    or (
                        bool(authority.get("replacement_evidence_sha256"))
                        and Path(str(authority.get("replacement_evidence_path") or "")).is_file()
                        and sha256_file(Path(str(authority.get("replacement_evidence_path"))))
                        == str(authority.get("replacement_evidence_sha256"))
                    )
                )
                and str(record.get("owned_tab_state") or "") in {"closed-and-absent", "already-absent"}
                and not bool(record.get("cleanup_pending"))
                and int(record.get("owned_open_tabs") or 0) == 0
            ):
                return record
        if claim_file.exists() or int(record.get("send_attempt_count") or 0) >= int(record.get("send_limit") or 1):
            raise StateError(
                "SEND_ALREADY_ATTEMPTED",
                "parent-owned child permits exactly one authoritative send",
                {"run_id": record.get("run_id"), "phase": record.get("phase")},
            )
        return record

    def pre_submit_retry_candidate(self, run_dir: str | os.PathLike[str]) -> dict[str, Any]:
        """Prove that the existing child claim stopped before provider mutation.

        This is deliberately read-only.  It does not authorize another dispatch;
        authorization additionally requires exact owned-tab cleanup under the
        same active parent lease.
        """
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") != "child":
            raise StateError("CHILD_RECORD_REQUIRED", "pre-submit retry proof requires a child run")
        phase = str(record.get("phase") or "")
        existing_authority = record.get("pre_submit_retry_authority")
        authorized_pre_send = bool(
            phase in {"PREFLIGHTED", "LEASED"}
            and isinstance(existing_authority, dict)
            and existing_authority.get("eligible") is True
            and existing_authority.get("consumed_at") is None
        )
        if phase != "SEND_REJECTED" and not authorized_pre_send:
            raise StateError("PRE_SUBMIT_RETRY_PHASE_INVALID", "pre-submit retry proof requires SEND_REJECTED or an authorized pre-send phase")
        if any(
            record.get(key) is not None
            for key in ("session_id", "conversation_url", "submission_receipt", "result")
        ):
            raise StateError("PRE_SUBMIT_RETRY_IDENTITY_CONFLICT", "submission identity or result already exists")
        if int(record.get("send_attempt_count") or 0) != 1 or int(record.get("send_limit") or 1) != 1:
            raise StateError("PRE_SUBMIT_RETRY_COUNT_INVALID", "retry candidate must preserve one authoritative send claim")

        claim_file = state_file.parent / "send.claim"
        if not claim_file.is_file() or claim_file.is_symlink():
            raise StateError("CHILD_SEND_CLAIM_MISSING", "retry candidate is missing its immutable send claim")
        claim = read_json(claim_file)
        if (
            claim.get("schema") != "codex.chatgpt.child-send-claim/v1"
            or str(claim.get("run_id") or "") != str(record.get("run_id") or "")
            or str(claim.get("parent_run_id") or "") != str(record.get("parent_run_id") or "")
            or str(claim.get("stage_id") or "") != str(record.get("stage_id") or "")
            or str(claim.get("manifest_sha256") or "") != str(record.get("manifest_sha256") or "")
            or str(claim.get("prompt_sha256") or "") != str(record.get("prompt_sha256") or "")
        ):
            raise StateError("CHILD_SEND_CLAIM_INVALID", "retry candidate send claim identity is not exact")

        evidence_dir = state_file.parent / "agbrowse-evidence"
        stdout_path = evidence_dir / "send.stdout.txt"
        stderr_path = evidence_dir / "send.stderr.txt"
        if not stdout_path.is_file() or stdout_path.is_symlink() or not stderr_path.is_file() or stderr_path.is_symlink():
            raise StateError("PRE_SUBMIT_RETRY_EVIDENCE_MISSING", "send stdout/stderr evidence is incomplete")
        stdout_text = stdout_path.read_text(encoding="utf-8")
        if stdout_text.strip():
            raise StateError("PRE_SUBMIT_RETRY_STDOUT_CONFLICT", "nonempty send stdout conflicts with a pre-submit rejection")
        try:
            payload = json.loads(stderr_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateError("PRE_SUBMIT_RETRY_STDERR_INVALID", "send stderr is not one exact JSON failure envelope") from exc
        error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
        error_code = str(error.get("errorCode") or error.get("error_code") or "")
        error_stage = str(error.get("stage") or "")
        error_evidence = error.get("evidence") if isinstance(error.get("evidence"), dict) else {}
        if payload.get("ok") is not False or error.get("mutationAllowed") is not False or not error_code or not error_stage:
            raise StateError("PRE_SUBMIT_RETRY_MUTATION_UNPROVEN", "failure envelope does not prove mutationAllowed=false")
        conflicting_keys = {
            "sessionId", "session_id", "conversationUrl", "conversation_url", "answer", "result"
        }
        if any(payload.get(key) not in (None, "", [], {}) for key in conflicting_keys):
            raise StateError("PRE_SUBMIT_RETRY_IDENTITY_CONFLICT", "failure envelope contains submission identity or result")
        matching_events = [
            event
            for event in record.get("recovery_events") or []
            if isinstance(event, dict)
            and str(event.get("kind") or "") == "verified-mutation-disallowed-reclassification"
            and str(event.get("error_code") or "") == error_code
            and str(event.get("error_stage") or "") == error_stage
        ]
        if not matching_events:
            raise StateError("PRE_SUBMIT_RETRY_RECLASSIFICATION_MISSING", "verified reclassification does not match send evidence")
        return {
            "schema": "codex.chatgpt.pre-submit-retry-candidate/v1",
            "run_id": record["run_id"],
            "parent_run_id": record["parent_run_id"],
            "stage_id": record["stage_id"],
            "target_id": record.get("current_target_id"),
            "claim_path": str(claim_file),
            "claim_sha256": sha256_file(claim_file),
            "send_stdout_path": str(stdout_path),
            "send_stdout_sha256": sha256_file(stdout_path),
            "send_stderr_path": str(stderr_path),
            "send_stderr_sha256": sha256_file(stderr_path),
            "error_code": error_code,
            "error_stage": error_stage,
            "capacity_reason": str(error_evidence.get("reason") or ""),
            "capacity_current": int(error_evidence.get("current") or 0),
            "capacity_limit": int(error_evidence.get("limit") or 0),
        }

    def authorize_child_pre_submit_retry(
        self,
        run_dir: str | os.PathLike[str],
        cleanup: dict[str, Any],
    ) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        self._verify_lock(state_file, record)
        candidate = self.pre_submit_retry_candidate(run_dir)
        target_id = str(record.get("current_target_id") or "")
        if (
            cleanup.get("ok") is not True
            or str(cleanup.get("state") or "") not in {"closed-and-absent", "already-absent"}
            or str(cleanup.get("target_id") or "") != target_id
        ):
            raise StateError("PRE_SUBMIT_RETRY_CLEANUP_UNPROVEN", "exact owned pre-submit target cleanup is required")
        lifecycle = cleanup.get("evidence") if isinstance(cleanup.get("evidence"), dict) else {}
        lifecycle_path = Path(str(lifecycle.get("path") or ""))
        try:
            lifecycle_path = lifecycle_path.expanduser().resolve(strict=True)
            lifecycle_path.relative_to(state_file.parent)
        except (OSError, RuntimeError, ValueError) as exc:
            raise StateError("PRE_SUBMIT_RETRY_CLEANUP_UNPROVEN", "cleanup lifecycle evidence path is invalid") from exc
        if (
            not lifecycle_path.is_file()
            or lifecycle_path.is_symlink()
            or sha256_file(lifecycle_path) != str(lifecycle.get("sha256") or "")
        ):
            raise StateError("PRE_SUBMIT_RETRY_CLEANUP_UNPROVEN", "cleanup lifecycle evidence hash is invalid")
        now = utc_now()
        prior = record.get("pre_submit_retry_authority") if isinstance(record.get("pre_submit_retry_authority"), dict) else {}
        authority = {
            **candidate,
            "schema": "codex.chatgpt.pre-submit-retry-authority/v1",
            "eligible": True,
            "authorized_at": now,
            "consumed_at": None,
            "retry_sequence": int(prior.get("retry_sequence") or 0) + 1,
            "cleanup_target_id": target_id,
            "cleanup_state": cleanup["state"],
            "cleanup_lifecycle_path": str(lifecycle_path),
            "cleanup_lifecycle_sha256": sha256_file(lifecycle_path),
        }
        record["pre_submit_retry_authority"] = authority
        record["cleanup_pending"] = False
        record["owned_tab_state"] = str(cleanup["state"])
        record["owned_open_tabs"] = 0
        record["cleanup_evidence"] = cleanup
        record["updated_at"] = now
        write_json_atomic(state_file, record)
        return record

    def confirm_child_retry_replacement(
        self,
        run_dir: str | os.PathLike[str],
        *,
        target_id: str,
        evidence_path: str | os.PathLike[str],
    ) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        self._verify_lock(state_file, record)
        authority = record.get("pre_submit_retry_authority")
        if not isinstance(authority, dict) or authority.get("eligible") is not True or authority.get("consumed_at") is not None:
            raise StateError("PRE_SUBMIT_RETRY_AUTHORITY_MISSING", "replacement target requires unconsumed retry authority")
        cleanup_target = str(authority.get("cleanup_target_id") or "")
        target_id = str(target_id or "")
        if not cleanup_target or not target_id or target_id == cleanup_target or target_id != str(record.get("current_target_id") or ""):
            raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_INVALID", "replacement target identity is not exact")
        matching_rebind = any(
            isinstance(event, dict)
            and str(event.get("old_target_id") or "") == cleanup_target
            and str(event.get("new_target_id") or "") == target_id
            and str(event.get("reason") or "") == "pre-submit-composer-retry"
            for event in record.get("target_rebind_events") or []
        )
        if not matching_rebind:
            raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_INVALID", "exact retry target rebind event is missing")
        path = Path(evidence_path).expanduser().resolve(strict=True)
        try:
            path.relative_to(state_file.parent)
        except ValueError as exc:
            raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_EVIDENCE_INVALID", "replacement evidence escaped the run directory") from exc
        evidence = read_json(path)
        is_app_selection = str(evidence.get("state") or "") == "composer-app-mention-tab-confirmed"
        is_research_selection = str(evidence.get("state") or "") == "deep-research-selected"
        if path.is_symlink() or not (is_app_selection or is_research_selection):
            raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_EVIDENCE_INVALID", "replacement composer evidence is not an approved capability selection")
        if is_app_selection:
            if (
                str(evidence.get("target_id") or "") != target_id
                or str(evidence.get("selection_method") or "") != "exact-at-mention-then-tab"
            ):
                raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_EVIDENCE_INVALID", "replacement app composer evidence is not exact")
        else:
            # A generic visible pill is not authority after a process restart.  The
            # persisted transition proof, all selection hashes, and the immutable
            # selection-evidence reference must bind this exact child/workflow/tab.
            self.verify_manifest(record)
            manifest = load_manifest(Path(str(record["manifest_path"])))
            correlation = manifest.get("workflow_correlation") if isinstance(manifest.get("workflow_correlation"), dict) else {}
            workflow_id = str(
                correlation.get("workflow_id")
                or record.get("parent_workflow_id")
                or record.get("workflow_id")
                or record.get("run_id")
                or ""
            )
            proof = evidence.get("selection_proof") if isinstance(evidence.get("selection_proof"), dict) else {}
            required_hashes = ("token_sha256", "before_snapshot_sha256", "after_snapshot_sha256", "action_transcript_sha256")
            expected_token_hash = sha256_bytes("@심층 리서치".encode("utf-8"))
            exact_hashes = all(re.fullmatch(r"[0-9a-f]{64}", str(evidence.get(key) or "")) for key in required_hashes)
            proof_hashes_match = (
                str(proof.get("token_sha256") or "") == expected_token_hash
                and str(proof.get("before_snapshot_sha256") or "") == str(evidence.get("before_snapshot_sha256") or "")
                and str(proof.get("after_snapshot_sha256") or "") == str(evidence.get("after_snapshot_sha256") or "")
                and str(proof.get("action_transcript_sha256") or "") == str(evidence.get("action_transcript_sha256") or "")
                and bool(re.fullmatch(r"[0-9a-f]{64}", str(proof.get("marker_identity_sha256") or "")))
            )
            evidence_hash = sha256_file(path)
            immutable_ref = any(
                isinstance(ref, dict)
                and str(ref.get("kind") or "") == "deep-research-selection"
                and str(ref.get("path") or "") == str(path)
                and str(ref.get("sha256") or "") == evidence_hash
                and str(ref.get("target_id") or "") == target_id
                for ref in record.get("selection_evidence_refs") or []
            )
            if not (
                evidence.get("schema") == "codex.chatgpt.capability-selection/v1"
                and str(evidence.get("run_id") or "") == str(record.get("run_id") or "")
                and str(evidence.get("workflow_id") or "") == workflow_id
                and str(evidence.get("target_id") or "") == target_id
                and str(evidence.get("selection_transport") or "") == "preselected-research"
                and str(evidence.get("token_sha256") or "") == expected_token_hash
                and exact_hashes
                and isinstance(evidence.get("selected_marker"), dict)
                and str(proof.get("kind") or "") == "token-to-pill-transition"
                and proof_hashes_match
                and immutable_ref
            ):
                raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_EVIDENCE_INVALID", "replacement Deep Research evidence does not bind exact immutable capability authority")
        authority = dict(authority)
        existing_replacement = str(authority.get("replacement_target_id") or "")
        if existing_replacement and existing_replacement != target_id:
            raise StateError("PRE_SUBMIT_RETRY_REPLACEMENT_IMMUTABLE", "replacement target cannot change")
        authority.update(
            {
                "replacement_target_id": target_id,
                "replacement_bound_at": utc_now(),
                "replacement_evidence_path": str(path),
                "replacement_evidence_sha256": sha256_file(path),
            }
        )
        record["pre_submit_retry_authority"] = authority
        record["updated_at"] = utc_now()
        write_json_atomic(state_file, record)
        return record

    def claim_child_send(self, run_dir: str | os.PathLike[str]) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") != "child":
            return self.transition(run_dir, "SEND_STARTED")
        self.assert_child_send_available(run_dir)
        if str(record.get("phase") or "") != "LEASED":
            raise StateError("SEND_PHASE_INVALID", "child send claim requires LEASED")
        now = utc_now()
        claim = {
            "schema": "codex.chatgpt.child-send-claim/v1",
            "run_id": record["run_id"],
            "parent_run_id": record["parent_run_id"],
            "stage_id": record["stage_id"],
            "manifest_sha256": record["manifest_sha256"],
            "prompt_sha256": record["prompt_sha256"],
            "claimed_at": now,
        }
        claim_file = state_file.parent / "send.claim"
        if claim_file.exists():
            return self.transition(run_dir, "SEND_STARTED")
        try:
            fd = os.open(claim_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(claim, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except FileExistsError as exc:
            raise StateError("SEND_ALREADY_ATTEMPTED", "child send claim already exists") from exc
        return self.transition(run_dir, "SEND_STARTED")

    def record_child_cleanup(self, run_dir: str | os.PathLike[str], cleanup: dict[str, Any]) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "") != "child":
            raise StateError("CHILD_RECORD_REQUIRED", "cleanup evidence can be attached only to a child")
        self._verify_lock(state_file, record)
        state = str(cleanup.get("state") or "")
        if cleanup.get("ok") is not True or state not in {"closed-and-absent", "already-absent"}:
            record["cleanup_pending"] = True
            record["owned_tab_state"] = state or "cleanup-pending"
            record["owned_open_tabs"] = 1 if record.get("current_target_id") else 0
        else:
            cleanup_target = str(cleanup.get("target_id") or record.get("current_target_id") or "")
            if record.get("current_target_id") and cleanup_target != str(record.get("current_target_id")):
                raise StateError("CHILD_CLEANUP_TARGET_MISMATCH", "cleanup target does not match the child target")
            cleanup_url = str(cleanup.get("conversation_url") or "")
            expected_url = str(record.get("conversation_url") or "")
            if expected_url and (not cleanup_url or cleanup_url != expected_url):
                raise StateError("CHILD_CLEANUP_URL_MISMATCH", "cleanup URL does not match the child canonical URL")
            if str(record.get("phase") or "") == "COMPLETE" and not expected_url:
                raise StateError("CHILD_CLEANUP_URL_MISMATCH", "cleanup URL does not match the child canonical URL")
            record["cleanup_pending"] = False
            record["owned_tab_state"] = state
            record["owned_open_tabs"] = 0
        record["cleanup_evidence"] = cleanup
        record["updated_at"] = utc_now()
        write_json_atomic(state_file, record)
        return record

    def record_terminal_cleanup(self, run_dir: str | os.PathLike[str], cleanup: dict[str, Any]) -> dict[str, Any]:
        state_file, record = self.load(run_dir)
        if str(record.get("record_kind") or "standalone") == "child":
            return self.record_child_cleanup(run_dir, cleanup)
        if str(record.get("phase") or "") not in {"COMPLETE", "PROVIDER_FAILED_TERMINAL"}:
            raise StateError(
                "TERMINAL_CLEANUP_PHASE_INVALID",
                "standalone cleanup evidence requires a safe terminal run",
            )
        state = str(cleanup.get("state") or "")
        target = str(cleanup.get("target_id") or record.get("current_target_id") or "")
        url = str(cleanup.get("conversation_url") or record.get("conversation_url") or "")
        if record.get("current_target_id") and target != str(record.get("current_target_id")):
            raise StateError("TERMINAL_CLEANUP_TARGET_MISMATCH", "cleanup target does not match the run target")
        if record.get("conversation_url") and url != str(record.get("conversation_url")):
            raise StateError("TERMINAL_CLEANUP_URL_MISMATCH", "cleanup URL does not match the canonical run URL")
        clean = cleanup.get("ok") is True and state in {"closed-and-absent", "already-absent"}
        record["cleanup_pending"] = not clean
        record["owned_tab_state"] = state or "cleanup-pending"
        record["owned_open_tabs"] = 0 if clean else (1 if record.get("current_target_id") else 0)
        record["cleanup_evidence"] = {
            **cleanup,
            "target_id": target,
            "conversation_url": url,
        }
        record["updated_at"] = utc_now()
        write_json_atomic(state_file, record)
        return record

    def rebind_terminal_target(
        self,
        run_dir: str | os.PathLike[str],
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        state_file, initial = self.load(run_dir)
        project_lock = state_file.parent.parent.parent / "parent-transition.lock"
        with exclusive_state_lock(project_lock):
            record = read_json(state_file)
            self.verify_manifest(record)
            if str(record.get("record_kind") or "standalone") == "parent":
                raise StateError("TERMINAL_TARGET_REBIND_RUN_INVALID", "parent records do not own conversation targets")
            if str(record.get("record_kind") or "standalone") == "child":
                self._verify_lock(state_file, record)
            phase = str(record.get("phase") or "")
            old_target_id = str(record.get("current_target_id") or "")
            new_target_id = str(candidate.get("new_target_id") or "")
            url = str(record.get("conversation_url") or "")
            evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
            evidence_error: str | None = None
            try:
                evidence_path = Path(str(evidence.get("path") or "")).expanduser().resolve(strict=True)
                evidence_path.relative_to(state_file.parent)
                if (
                    not evidence_path.is_file()
                    or evidence_path.is_symlink()
                    or sha256_file(evidence_path) != str(evidence.get("sha256") or "")
                ):
                    evidence_error = "evidence-invalid"
            except (OSError, RuntimeError, ValueError):
                evidence_error = "evidence-path-invalid"
            if (
                phase not in {"COMPLETE", "PROVIDER_FAILED_TERMINAL"}
                or candidate.get("ok") is not True
                or str(candidate.get("phase") or "") != phase
                or str(candidate.get("conversation_url") or "") != url
                or str(candidate.get("old_target_id") or "") != old_target_id
                or not new_target_id
                or new_target_id == old_target_id
                or candidate.get("old_target_absent") is not True
                or int(candidate.get("url_match_count") or 0) != 1
                or candidate.get("foreign_owner_absent") is not True
                or evidence_error is not None
            ):
                raise StateError(
                    "TERMINAL_TARGET_REBIND_UNPROVEN",
                    "terminal target rebind requires one exact URL match and immutable absence/ownership evidence",
                    {"evidence_error": evidence_error},
                )
            now = utc_now()
            record["target_rebind_events"].append(
                {
                    "at": now,
                    "old_target_id": old_target_id,
                    "new_target_id": new_target_id,
                    "conversation_url": url,
                    "reason": "terminal-exact-url-after-browser-restart",
                    "evidence": evidence,
                }
            )
            record["current_target_id"] = new_target_id
            record["updated_at"] = now
            write_json_atomic(state_file, record)
            return record

    def resume_parent_workflow(
        self,
        parent_run_dir: str | os.PathLike[str],
        manifest_path: str | os.PathLike[str],
        owner_pid: int | None = None,
        *,
        reactivate: bool = False,
    ) -> dict[str, Any]:
        state_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "resume requires a parent record")
        root = canonical_project_root(initial["project_root"])
        paths = self.paths(root, str(initial["run_id"]))
        manifest_file = Path(manifest_path).expanduser().resolve()
        with exclusive_state_lock(paths.parent_transition_lock):
            record = read_json(state_file)
            original_record = json.loads(json.dumps(record))
            lock = self._read_existing_lock(paths.lock_file)
            if str(manifest_file) != str(Path(str(record["manifest_path"])).resolve()) or sha256_file(manifest_file) != record["manifest_sha256"]:
                raise StateError("BLOCKED_MANIFEST_MISMATCH", "resume manifest does not match the immutable parent")
            if str(record.get("phase") or "") in PARENT_TERMINAL_PHASES:
                raise StateError("PARENT_ALREADY_TERMINAL", "terminal parent cannot be resumed")
            if reactivate:
                raise StateError(
                    "PARENT_REACTIVATION_INVALID",
                    "a parent that entered draining or recovery-required cannot return to active",
                    {"phase": str(record.get("phase") or "")},
                )
            requested_pid = int(owner_pid or os.getpid())
            if same_process(record.get("owner") or {}) and requested_pid != int((record.get("owner") or {}).get("pid") or 0):
                raise StateError("ACTIVE_PROJECT_OWNER", "live parent owner cannot be replaced")
            if lock is not None and (
                str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(record["run_id"])
                or str(lock.get("lease_nonce") or "") != str(record["lease_nonce"])
                or str(lock.get("workflow_id") or "") != str(record["workflow_id"])
            ):
                raise StateError("BLOCKED_OWNER_MISMATCH", "resume parent lease identity is not exact")
            if lock is None:
                foreign_records = [
                    item
                    for item in self._active_or_uncertain_records(paths.runs_dir)
                    if str(item.get("run_id") or "") != str(record["run_id"])
                ]
                if foreign_records:
                    raise StateError(
                        "SAME_PROJECT_ACTIVE_OR_UNCERTAIN",
                        "another active or uncertain project run prevents parent lock reconstruction",
                        {"records": foreign_records[:10]},
                    )
            identity = process_identity(requested_pid)
            owner = {**identity, "nonce": uuid.uuid4().hex, "epoch": int(time.time_ns())}
            record["owner"] = owner
            now = utc_now()
            recreated_lock = lock is None
            if lock is None:
                lock = {
                    "schema": SCHEMA,
                    "record_kind": "parent",
                    "run_id": record["run_id"],
                    "parent_run_id": record["run_id"],
                    "project_root": record["project_root"],
                    "project_key": record["project_key"],
                    "workflow_id": record["workflow_id"],
                    "manifest_sha256": record["manifest_sha256"],
                    "lease_nonce": record["lease_nonce"],
                    "owner": owner,
                    "phase": record["phase"],
                    "heartbeat_at": now,
                }
                try:
                    fd = os.open(paths.lock_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(lock, handle, ensure_ascii=False, indent=2)
                        handle.write("\n")
                except FileExistsError as exc:
                    raise StateError(
                        "SAME_PROJECT_ACTIVE_OR_UNCERTAIN",
                        "project parent lock appeared during exact resume reconstruction",
                    ) from exc
                recovery_events = record.setdefault("parent_lock_recovery_events", [])
                recovery_events.append(
                    {
                        "kind": "missing-parent-lock-recreated",
                        "at": now,
                        "lease_nonce": record["lease_nonce"],
                        "owner_pid": requested_pid,
                    }
                )
                record["parent_lock_recovery_events"] = recovery_events[-20:]
            else:
                lock["owner"] = owner
            record["updated_at"] = now
            lock["heartbeat_at"] = now
            try:
                write_json_atomic(state_file, record)
                write_json_atomic(paths.lock_file, lock)
            except Exception:
                if recreated_lock:
                    try:
                        write_json_atomic(state_file, original_record)
                    except Exception as rollback_exc:
                        raise StateError(
                            "PARENT_LOCK_RECONSTRUCTION_ROLLBACK_FAILED",
                            "failed to restore the original parent record after lock reconstruction failure",
                            {"detail": str(rollback_exc)},
                        ) from rollback_exc
                    try:
                        paths.lock_file.unlink()
                    except OSError as rollback_exc:
                        raise StateError(
                            "PARENT_LOCK_RECONSTRUCTION_ROLLBACK_FAILED",
                            "restored the parent record but could not remove the reconstructed lock",
                            {"detail": str(rollback_exc)},
                        ) from rollback_exc
                raise
        return record

    def mark_parent_runtime_recovery(
        self,
        parent_run_dir: str | os.PathLike[str],
        *,
        failure: dict[str, Any],
    ) -> dict[str, Any]:
        state_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "runtime recovery requires a parent record")
        root = canonical_project_root(initial["project_root"])
        paths = self.paths(root, str(initial["run_id"]))
        with exclusive_state_lock(paths.parent_transition_lock):
            record = read_json(state_file)
            lock = read_json(paths.lock_file)
            if (
                str(record.get("phase") or "") != "PARENT_ACTIVE"
                or str(lock.get("phase") or "") != "PARENT_ACTIVE"
                or str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(record.get("run_id") or "")
                or str(lock.get("lease_nonce") or "") != str(record.get("lease_nonce") or "")
                or str(lock.get("manifest_sha256") or "") != str(record.get("manifest_sha256") or "")
            ):
                raise StateError(
                    "PARENT_RUNTIME_RECOVERY_PHASE_INVALID",
                    "runtime recovery can be marked only on the exact active parent",
                )
            now = utc_now()
            events = list(record.get("runtime_recovery_events") or [])
            events.append({"kind": "runtime-recovery-required", "at": now, "failure": failure})
            record["runtime_recovery_events"] = events[-20:]
            record["recovery_required"] = True
            record["runtime_recovery_failure"] = failure
            record["updated_at"] = now
            lock["recovery_required"] = True
            lock["heartbeat_at"] = now
            write_json_atomic(state_file, record)
            write_json_atomic(paths.lock_file, lock)
        return record

    def clear_parent_runtime_recovery(self, parent_run_dir: str | os.PathLike[str]) -> dict[str, Any]:
        state_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "runtime recovery requires a parent record")
        root = canonical_project_root(initial["project_root"])
        paths = self.paths(root, str(initial["run_id"]))
        with exclusive_state_lock(paths.parent_transition_lock):
            record = read_json(state_file)
            lock = read_json(paths.lock_file)
            if (
                str(record.get("phase") or "") != "PARENT_ACTIVE"
                or str(lock.get("phase") or "") != "PARENT_ACTIVE"
                or str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(record.get("run_id") or "")
                or str(lock.get("lease_nonce") or "") != str(record.get("lease_nonce") or "")
            ):
                raise StateError(
                    "PARENT_RUNTIME_RECOVERY_PHASE_INVALID",
                    "runtime recovery can be cleared only on the exact active parent",
                )
            unsafe: list[dict[str, Any]] = []
            for child_state, child in self._parent_children(paths.runs_dir, str(record["run_id"])):
                phase = str(child.get("phase") or "")
                summary = {
                    "run_id": child.get("run_id"),
                    "stage_id": child.get("stage_id"),
                    "phase": phase,
                }
                if phase == "COMPLETE":
                    if (
                        bool(child.get("cleanup_pending"))
                        or int(child.get("owned_open_tabs") or 0) != 0
                        or str(child.get("owned_tab_state") or "") not in {"closed-and-absent", "already-absent"}
                    ):
                        unsafe.append({**summary, "reason": "completed child cleanup is not durable"})
                    continue
                if phase == "SEND_REJECTED":
                    try:
                        self.pre_submit_retry_candidate(child_state.parent)
                    except StateError as exc:
                        unsafe.append({**summary, "reason": exc.code})
                    continue
                if phase in {
                    "CREATED",
                    "PREFLIGHTED",
                    "LEASED",
                    "PREFLIGHT_BLOCKED",
                    "BLOCKED_APP_TRANSACTION",
                    "CANCELLED_PRE_SUBMISSION",
                }:
                    claim_exists = (child_state.parent / "send.claim").exists()
                    target_absent = not child.get("current_target_id")
                    target_clean = bool(
                        str(child.get("owned_tab_state") or "") in {"closed-and-absent", "already-absent"}
                        and not bool(child.get("cleanup_pending"))
                        and int(child.get("owned_open_tabs") or 0) == 0
                    )
                    if (
                        claim_exists
                        or int(child.get("send_attempt_count") or 0) != 0
                        or child.get("session_id")
                        or child.get("conversation_url")
                        or child.get("submission_receipt") is not None
                        or child.get("result") is not None
                        or not (target_absent or target_clean)
                    ):
                        unsafe.append({**summary, "reason": "pre-submit child carries send or unclean target evidence"})
                    continue
                unsafe.append({**summary, "reason": "child remains active or uncertain"})
            if unsafe:
                raise StateError(
                    "PARENT_RUNTIME_RECOVERY_PENDING",
                    "existing children are not yet safe to continue the parent workflow",
                    {"children": unsafe},
                )
            now = utc_now()
            events = list(record.get("runtime_recovery_events") or [])
            events.append({"kind": "runtime-recovery-cleared", "at": now})
            record["runtime_recovery_events"] = events[-20:]
            record["recovery_required"] = False
            record["runtime_recovery_failure"] = None
            record["updated_at"] = now
            lock["recovery_required"] = False
            lock["heartbeat_at"] = now
            write_json_atomic(state_file, record)
            write_json_atomic(paths.lock_file, lock)
        return record

    def reopen_failed_parent_workflow(
        self,
        parent_run_dir: str | os.PathLike[str],
        manifest_path: str | os.PathLike[str],
        owner_pid: int | None = None,
    ) -> dict[str, Any]:
        state_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "failed-parent reopen requires a parent record")
        root = canonical_project_root(initial["project_root"])
        paths = self.paths(root, str(initial["run_id"]))
        manifest_file = Path(manifest_path).expanduser().resolve(strict=True)
        with exclusive_state_lock(paths.parent_transition_lock):
            record = read_json(state_file)
            failure = record.get("failure") if isinstance(record.get("failure"), dict) else {}
            failure_code = str(failure.get("code") or "")
            if str(record.get("phase") or "") != "PARENT_FAILED_CLOSED":
                raise StateError("PARENT_REOPEN_PHASE_INVALID", "only a failed-closed parent can use deterministic reopen")
            if failure_code not in {
                "CHILD_IDENTITY_INCOMPLETE",
                "IMMUTABLE_ARTIFACT_CONFLICT",
                "CHILD_NOT_COMPLETE",
                "PRE_SUBMIT_RETRY_SESSION_NOT_QUIESCENT",
                "APP_TRANSACTION_FAILED",
                "APP_COMPOSER_PREP_FAILED",
            }:
                raise StateError(
                    "PARENT_REOPEN_FAILURE_UNSUPPORTED",
                    "failed parent reason is not a proven local post-child integration failure",
                    {"failure_code": failure_code},
                )
            if record.get("result") is not None:
                raise StateError("PARENT_REOPEN_RESULT_PRESENT", "a parent with a durable result cannot be reopened")
            if (
                str(Path(str(record.get("manifest_path") or "")).resolve()) != str(manifest_file)
                or sha256_file(manifest_file) != str(record.get("manifest_sha256") or "")
            ):
                raise StateError("BLOCKED_MANIFEST_MISMATCH", "failed-parent manifest identity changed")
            if same_process(record.get("owner") or {}):
                raise StateError("ACTIVE_PROJECT_OWNER", "live failed-parent owner cannot be replaced")
            if paths.lock_file.exists():
                raise StateError("SAME_PROJECT_ACTIVE_OR_UNCERTAIN", "project lock already exists during failed-parent reopen")
            existing_records = self._active_or_uncertain_records(paths.runs_dir)
            if existing_records:
                raise StateError(
                    "SAME_PROJECT_ACTIVE_OR_UNCERTAIN",
                    "another active or uncertain project run prevents failed-parent reopen",
                    {"records": existing_records[:10]},
                )
            children = self._parent_children(paths.runs_dir, str(record["run_id"]))
            if not children:
                raise StateError("PARENT_REOPEN_CHILDREN_MISSING", "failed-parent reopen requires completed child evidence")
            unsafe: list[dict[str, Any]] = []
            retry_candidates: list[dict[str, Any]] = []
            safe_pre_submit_candidates: list[dict[str, Any]] = []
            for child_state, child in children:
                result = child.get("result") if isinstance(child.get("result"), dict) else {}
                result_path = Path(str(result.get("path") or ""))
                summary = {
                    "run_id": child.get("run_id"),
                    "stage_id": child.get("stage_id"),
                    "phase": child.get("phase"),
                    "send_attempt_count": int(child.get("send_attempt_count") or 0),
                    "cleanup_pending": bool(child.get("cleanup_pending")),
                    "owned_tab_state": child.get("owned_tab_state"),
                    "owned_open_tabs": int(child.get("owned_open_tabs") or 0),
                }
                if str(child.get("phase") or "") in {"PREFLIGHT_BLOCKED", "BLOCKED_APP_TRANSACTION"}:
                    claim_exists = (child_state.parent / "send.claim").exists()
                    target_absent = not child.get("current_target_id")
                    target_clean = bool(
                        str(child.get("owned_tab_state") or "") in {"closed-and-absent", "already-absent"}
                        and not bool(child.get("cleanup_pending"))
                        and int(child.get("owned_open_tabs") or 0) == 0
                    )
                    safe = bool(
                        int(child.get("send_attempt_count") or 0) == 0
                        and not claim_exists
                        and not child.get("session_id")
                        and not child.get("conversation_url")
                        and child.get("submission_receipt") is None
                        and child.get("result") is None
                        and (target_absent or target_clean)
                    )
                    if safe:
                        safe_pre_submit_candidates.append(summary)
                    else:
                        unsafe.append({**summary, "reason": "pre-submit child carries send, identity, result, or unclean target evidence"})
                    continue
                if str(child.get("phase") or "") == "SEND_REJECTED":
                    try:
                        retry_candidate = self.pre_submit_retry_candidate(child_state.parent)
                    except StateError as exc:
                        unsafe.append({**summary, "retry_error": exc.code})
                    else:
                        retry_candidates.append(
                            {
                                **summary,
                                "claim_sha256": retry_candidate["claim_sha256"],
                                "send_stderr_sha256": retry_candidate["send_stderr_sha256"],
                                "target_id": retry_candidate.get("target_id"),
                            }
                        )
                    continue
                valid_result = False
                try:
                    result_path = result_path.expanduser().resolve(strict=True)
                    result_path.relative_to(child_state.parent)
                    valid_result = (
                        result_path.is_file()
                        and not result_path.is_symlink()
                        and sha256_file(result_path) == str(result.get("sha256") or "")
                        and result_path.stat().st_size == int(result.get("bytes") or -1)
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    valid_result = False
                if (
                    str(child.get("phase") or "") != "COMPLETE"
                    or summary["send_attempt_count"] != 1
                    or summary["cleanup_pending"]
                    or summary["owned_tab_state"] not in {"closed-and-absent", "already-absent"}
                    or summary["owned_open_tabs"] != 0
                    or not child.get("session_id")
                    or not child.get("current_target_id")
                    or not CANONICAL_CHAT_RE.fullmatch(str(child.get("conversation_url") or ""))
                    or not valid_result
                ):
                    unsafe.append(summary)
            if failure_code == "CHILD_NOT_COMPLETE":
                failure_text = str(failure.get("message") or "")
                retry_stage_ids = {str(item.get("stage_id") or "") for item in retry_candidates}
                if not retry_candidates or not any(stage_id and stage_id in failure_text for stage_id in retry_stage_ids):
                    unsafe.append(
                        {
                            "parent_failure": failure_code,
                            "reason": "failure does not identify an exact retryable SEND_REJECTED child",
                        }
                    )
            elif failure_code == "PRE_SUBMIT_RETRY_SESSION_NOT_QUIESCENT":
                if len(retry_candidates) != 1:
                    unsafe.append(
                        {
                            "parent_failure": failure_code,
                            "reason": "session-quiescence retry requires exactly one proven SEND_REJECTED child",
                        }
                    )
            elif failure_code in {"APP_TRANSACTION_FAILED", "APP_COMPOSER_PREP_FAILED"}:
                if not safe_pre_submit_candidates or retry_candidates:
                    unsafe.append(
                        {
                            "parent_failure": failure_code,
                            "reason": "app-transaction reopen requires one or more zero-send safe pre-submit children",
                        }
                    )
            elif retry_candidates:
                unsafe.extend(
                    {**item, "reason": "retryable child is incompatible with the recorded parent failure"}
                    for item in retry_candidates
                )
            if failure_code not in {"APP_TRANSACTION_FAILED", "APP_COMPOSER_PREP_FAILED"} and safe_pre_submit_candidates:
                unsafe.extend(
                    {**item, "reason": "safe pre-submit child is incompatible with the recorded parent failure"}
                    for item in safe_pre_submit_candidates
                )
            if unsafe:
                raise StateError(
                    "PARENT_REOPEN_CHILD_EVIDENCE_UNSAFE",
                    "all existing children must be complete, one-send, exact, hashed, and cleaned",
                    {"children": unsafe},
                )
            identity = process_identity(owner_pid)
            owner = {**identity, "nonce": uuid.uuid4().hex, "epoch": int(time.time_ns())}
            now = utc_now()
            lock = {
                "schema": SCHEMA,
                "record_kind": "parent",
                "run_id": record["run_id"],
                "parent_run_id": record["run_id"],
                "project_root": record["project_root"],
                "project_key": record["project_key"],
                "workflow_id": record["workflow_id"],
                "manifest_sha256": record["manifest_sha256"],
                "lease_nonce": record["lease_nonce"],
                "owner": owner,
                "phase": "PARENT_ACTIVE",
                "heartbeat_at": now,
            }
            try:
                fd = os.open(paths.lock_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(lock, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
            except FileExistsError as exc:
                raise StateError("SAME_PROJECT_ACTIVE_OR_UNCERTAIN", "project lock appeared during failed-parent reopen") from exc
            prior_failures = list(record.get("prior_failures") or [])
            prior_failures.append({"at": now, "phase": "PARENT_FAILED_CLOSED", "failure": failure})
            record["prior_failures"] = prior_failures
            record["failure"] = None
            record["owner"] = owner
            record["phase_events"].append(
                {
                    "from": "PARENT_FAILED_CLOSED",
                    "to": "PARENT_ACTIVE",
                    "at": now,
                    "reason": "deterministic-local-post-child-reopen",
                    "retry_candidate_count": len(retry_candidates),
                    "safe_pre_submit_candidate_count": len(safe_pre_submit_candidates),
                }
            )
            record["phase"] = "PARENT_ACTIVE"
            record["phase_at"] = now
            record["updated_at"] = now
            record["child_scan"] = [
                {
                    "run_id": child.get("run_id"),
                    "stage_id": child.get("stage_id"),
                    "phase": child.get("phase"),
                    "send_attempt_count": int(child.get("send_attempt_count") or 0),
                    "cleanup_pending": bool(child.get("cleanup_pending")),
                    "owned_open_tabs": int(child.get("owned_open_tabs") or 0),
                }
                for _, child in children
            ]
            record["pre_submit_retry_candidates"] = retry_candidates
            record["safe_pre_submit_candidates"] = safe_pre_submit_candidates
            try:
                write_json_atomic(state_file, record)
            except Exception:
                try:
                    paths.lock_file.unlink()
                except OSError:
                    pass
                raise
            return record

    def finalize_parent(
        self,
        parent_run_dir: str | os.PathLike[str],
        phase: str,
        *,
        result: dict[str, Any] | None = None,
        failure: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if phase not in {"PARENT_COMPLETE", "PARENT_FAILED_CLOSED"}:
            raise StateError("PARENT_TERMINAL_PHASE_INVALID", "parent finalization requires a clean or failed-closed phase")
        state_file, initial = self.load(parent_run_dir)
        if str(initial.get("record_kind") or "") != "parent":
            raise StateError("PARENT_RECORD_REQUIRED", "finalization requires a parent record")
        root = canonical_project_root(initial["project_root"])
        paths = self.paths(root, str(initial["run_id"]))
        with exclusive_state_lock(paths.parent_transition_lock):
            record = read_json(state_file)
            lock = read_json(paths.lock_file)
            if (
                str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(record["run_id"])
                or str(lock.get("lease_nonce") or "") != str(record["lease_nonce"])
                or str(lock.get("manifest_sha256") or "") != str(record["manifest_sha256"])
            ):
                raise StateError("BLOCKED_OWNER_MISMATCH", "parent finalization lease is not exact")
            current = str(record.get("phase") or "")
            if current not in {"PARENT_ACTIVE", "PARENT_RECOVERY_REQUIRED", "PARENT_DRAINING"}:
                raise StateError("PARENT_PHASE_INVALID", f"cannot finalize parent from {current}")
            now = utc_now()
            if current != "PARENT_DRAINING":
                record["phase_events"].append({"from": current, "to": "PARENT_DRAINING", "at": now})
                record["phase"] = "PARENT_DRAINING"
                record["phase_at"] = now
                record["updated_at"] = now
                lock["phase"] = "PARENT_DRAINING"
                lock["heartbeat_at"] = now
                write_json_atomic(state_file, record)
                write_json_atomic(paths.lock_file, lock)

            children = self._parent_children(paths.runs_dir, str(record["run_id"]))
            unresolved: list[dict[str, Any]] = []
            cleanup_pending: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            summaries: list[dict[str, Any]] = []
            for child_state, child in children:
                child_phase = str(child.get("phase") or "")
                claim_exists = (child_state.parent / "send.claim").exists()
                identity_exists = bool(child.get("current_target_id") or child.get("conversation_url"))
                summary = {
                    "run_id": child.get("run_id"),
                    "stage_id": child.get("stage_id"),
                    "phase": child_phase,
                    "send_attempt_count": int(child.get("send_attempt_count") or 0),
                    "cleanup_pending": bool(child.get("cleanup_pending")),
                    "owned_open_tabs": int(child.get("owned_open_tabs") or 0),
                }
                summaries.append(summary)
                if (
                    child_phase in UNCERTAIN_OR_SUBMITTED_PHASES
                    or child_phase not in CHILD_SAFE_TERMINAL_PHASES
                    or (claim_exists and int(child.get("send_attempt_count") or 0) == 0)
                ):
                    unresolved.append(summary)
                    continue
                if child_phase == "COMPLETE" and identity_exists and str(child.get("owned_tab_state") or "") not in {"closed-and-absent", "already-absent"}:
                    cleanup_pending.append(summary)
                if bool(child.get("cleanup_pending")) or int(child.get("owned_open_tabs") or 0) != 0:
                    cleanup_pending.append(summary)
                if child_phase != "COMPLETE":
                    failed.append(summary)

            if unresolved or cleanup_pending:
                recovery_at = utc_now()
                record["phase_events"].append({"from": "PARENT_DRAINING", "to": "PARENT_RECOVERY_REQUIRED", "at": recovery_at})
                record["phase"] = "PARENT_RECOVERY_REQUIRED"
                record["phase_at"] = recovery_at
                record["updated_at"] = recovery_at
                record["child_scan"] = summaries
                record["failure"] = {
                    "code": "PARENT_CHILDREN_UNRESOLVED_OR_CLEANUP_PENDING",
                    "unresolved": unresolved,
                    "cleanup_pending": cleanup_pending,
                }
                lock["phase"] = "PARENT_RECOVERY_REQUIRED"
                lock["heartbeat_at"] = recovery_at
                write_json_atomic(state_file, record)
                write_json_atomic(paths.lock_file, lock)
                return record

            terminal_phase = phase
            if phase == "PARENT_COMPLETE" and failed:
                terminal_phase = "PARENT_FAILED_CLOSED"
                failure = failure or {"code": "PARENT_CHILD_STAGE_FAILED", "children": failed}
            if terminal_phase == "PARENT_COMPLETE" and result is None:
                raise StateError("PARENT_RESULT_MISSING", "clean parent completion requires a result descriptor")
            terminal_at = utc_now()
            record["phase_events"].append({"from": "PARENT_DRAINING", "to": terminal_phase, "at": terminal_at})
            record["phase"] = terminal_phase
            record["phase_at"] = terminal_at
            record["updated_at"] = terminal_at
            record["child_scan"] = summaries
            record["result"] = result
            record["failure"] = failure
            record["owned_open_tabs"] = 0
            lock["phase"] = terminal_phase
            lock["heartbeat_at"] = terminal_at
            write_json_atomic(state_file, record)
            write_json_atomic(paths.lock_file, lock)
            latest_lock = read_json(paths.lock_file)
            if (
                str(latest_lock.get("parent_run_id") or latest_lock.get("run_id") or "") == str(record["run_id"])
                and str(latest_lock.get("lease_nonce") or "") == str(record["lease_nonce"])
            ):
                paths.lock_file.unlink()
        return record

    def create_run(
        self,
        *,
        project_root: str | os.PathLike[str],
        manifest_path: str | os.PathLike[str],
        agbrowse_contract: dict[str, Any],
        owner_pid: int | None = None,
    ) -> dict[str, Any]:
        root = canonical_project_root(project_root)
        manifest_file = Path(manifest_path).expanduser().resolve()
        if not manifest_file.is_file():
            raise StateError("MANIFEST_MISSING", f"manifest file missing: {manifest_file}")
        manifest = load_manifest(manifest_file)
        manifest_hash = sha256_file(manifest_file)
        prompt = prompt_contract(manifest)
        prompt_hash = str(prompt["prompt_sha256"])
        run_id = uuid.uuid4().hex
        paths = self.paths(root, run_id)
        paths.project_dir.mkdir(parents=True, exist_ok=True)

        existing_lock = self._read_existing_lock(paths.lock_file)
        existing_records = self._active_or_uncertain_records(paths.runs_dir)
        if existing_lock or existing_records:
            diagnosis = self.reconcile_project_lock(root, apply_safe_pre_submission=False)
            code = str(diagnosis.get("state") or "SAME_PROJECT_ACTIVE_OR_UNCERTAIN")
            if code not in {
                "ACTIVE_PROJECT_OWNER",
                "STALE_PRE_SUBMISSION_SAFE_TO_CANCEL",
                "STALE_DUPLICATE_COMPLETE_OWNER_SAFE_TO_SETTLE",
                "STALE_OWNER_UNRESOLVED_SUBMISSION",
                "STALE_OWNER_PRE_SUBMIT_TARGET_PRESENT",
                "PROJECT_LOCK_STATE_AMBIGUOUS",
                "TERMINAL_ORPHAN_LOCK_DETECTED",
            }:
                code = "SAME_PROJECT_ACTIVE_OR_UNCERTAIN"
            raise StateError(
                code,
                "same project already has a verified active, uncertain, or ambiguous run",
                {
                    "diagnosis": diagnosis,
                    "supported_reconcile_command": (
                        f'python "{Path(__file__).resolve().with_name("chatgpt_agbrowse_run.py")}" '
                        f'--reconcile-project-lock "{root}"'
                        if code in {
                            "STALE_PRE_SUBMISSION_SAFE_TO_CANCEL",
                            "STALE_DUPLICATE_COMPLETE_OWNER_SAFE_TO_SETTLE",
                        }
                        else None
                    ),
                    "lock": existing_lock,
                    "records": existing_records[:10],
                },
            )

        identity = process_identity(owner_pid)
        nonce = uuid.uuid4().hex
        epoch = int(time.time_ns())
        created = utc_now()
        lock = {
            "schema": SCHEMA,
            "record_kind": "standalone",
            "run_id": run_id,
            "project_root": str(root),
            "project_key": project_key(root),
            "manifest_sha256": manifest_hash,
            "owner": {**identity, "nonce": nonce, "epoch": epoch},
            "phase": "CREATED",
            "session_id": None,
            "target_id": None,
            "conversation_url": None,
            "heartbeat_at": created,
        }
        try:
            fd = os.open(paths.lock_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(lock, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except FileExistsError as exc:
            raise StateError("SAME_PROJECT_ACTIVE_OR_UNCERTAIN", "project lock appeared during dispatch") from exc

        recovery_identity: dict[str, Any] | None = None
        if prompt.get("transport") == "file":
            source = Path(str(prompt["prompt_file"]))
            alias_name = f"prompt-{run_id}.txt"
            alias_path = paths.run_dir / alias_name
            alias_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_bytes = source.read_bytes()
            alias_path.write_bytes(prompt_bytes)
            alias_hash = sha256_file(alias_path)
            if alias_hash != prompt_hash:
                try:
                    paths.lock_file.unlink()
                except OSError:
                    pass
                raise StateError(
                    "RECOVERY_PROMPT_ALIAS_HASH_MISMATCH",
                    "run-owned recovery prompt alias does not match the immutable prompt",
                    {"expected": prompt_hash, "actual": alias_hash},
                )
            recovery_identity = {
                "schema": "codex.chatgpt.recovery-identity/v1",
                "token": run_id,
                "attachment_name": alias_name,
                "attachment_path": str(alias_path),
                "attachment_sha256": alias_hash,
                "source_prompt_path": str(source),
            }

        record = {
            "schema": SCHEMA,
            "record_kind": "standalone",
            "run_id": run_id,
            "project_root": str(root),
            "project_key": project_key(root),
            "manifest_path": str(manifest_file),
            "manifest_sha256": manifest_hash,
            "prompt_sha256": prompt_hash,
            "recovery_identity": recovery_identity,
            "requested": _requested_contract(manifest),
            "agbrowse": dict(agbrowse_contract),
            "owner": {**identity, "nonce": nonce, "epoch": epoch},
            "created_at": created,
            "updated_at": created,
            "phase": "CREATED",
            "phase_at": created,
            "session_id": None,
            "current_target_id": None,
            "conversation_url": None,
            "submission_receipt": None,
            "result": None,
            "terminal_block_code": None,
            "recovery_count": 0,
            "phase_events": [{"from": None, "to": "CREATED", "at": created}],
            "target_rebind_events": [],
            "recovery_events": [],
            "app_evidence_refs": [],
            "selection_evidence_refs": [],
        }
        try:
            write_json_atomic(paths.state_file, record)
        except Exception:
            try:
                paths.lock_file.unlink()
            except OSError:
                pass
            raise
        return {**record, "run_dir": str(paths.run_dir), "state_file": str(paths.state_file)}

    def load(self, run_dir: str | os.PathLike[str]) -> tuple[Path, dict[str, Any]]:
        path = Path(run_dir).expanduser().resolve()
        state_file = path if path.name == "run.json" else path / "run.json"
        record = read_json(state_file)
        if record.get("schema") != SCHEMA or not REQUIRED_IMMUTABLE.issubset(record):
            raise StateError("STATE_SCHEMA_INVALID", f"invalid agbrowse run state: {state_file}")
        return state_file, record

    def verify_manifest(self, record: dict[str, Any]) -> None:
        path = Path(str(record["manifest_path"]))
        actual = sha256_file(path) if path.is_file() else None
        if actual != record["manifest_sha256"]:
            raise StateError(
                "BLOCKED_MANIFEST_MISMATCH",
                "manifest changed after run creation",
                {"expected": record["manifest_sha256"], "actual": actual, "path": str(path)},
            )
        manifest = load_manifest(path)
        if str(record.get("record_kind") or "standalone") == "parent":
            try:
                validate_web_multi_parent_manifest(manifest)
            except StateError as exc:
                raise StateError(
                    "BLOCKED_MANIFEST_MISMATCH",
                    "parent workflow manifest contract changed after creation",
                    {"cause": exc.code, **exc.evidence},
                ) from exc
            if str(manifest.get("workflow_id") or "") != str(record.get("workflow_id") or ""):
                raise StateError("BLOCKED_MANIFEST_MISMATCH", "parent workflow binding changed after creation")
            return
        try:
            contract = prompt_contract(manifest)
        except StateError as exc:
            raise StateError(
                "BLOCKED_MANIFEST_MISMATCH",
                "prompt file contract changed after run creation",
                {"cause": exc.code, **exc.evidence},
            ) from exc
        if contract["prompt_sha256"] != record["prompt_sha256"]:
            raise StateError(
                "BLOCKED_MANIFEST_MISMATCH",
                "prompt file changed after run creation",
                {
                    "expected": record["prompt_sha256"],
                    "actual": contract["prompt_sha256"],
                    "path": contract.get("prompt_file"),
                },
            )
        recovery_identity = record.get("recovery_identity")
        if recovery_identity:
            expected_name = f"prompt-{record['run_id']}.txt"
            alias_path = Path(str(recovery_identity.get("attachment_path") or ""))
            try:
                alias_path = alias_path.expanduser().resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise StateError(
                    "BLOCKED_MANIFEST_MISMATCH",
                    "run-owned recovery prompt alias is unavailable",
                ) from exc
            if (
                recovery_identity.get("schema") != "codex.chatgpt.recovery-identity/v1"
                or str(recovery_identity.get("token") or "") != str(record["run_id"])
                or str(recovery_identity.get("attachment_name") or "") != expected_name
                or alias_path.name != expected_name
                or not alias_path.is_file()
                or alias_path.is_symlink()
                or sha256_file(alias_path) != record["prompt_sha256"]
                or str(recovery_identity.get("attachment_sha256") or "") != record["prompt_sha256"]
            ):
                raise StateError(
                    "BLOCKED_MANIFEST_MISMATCH",
                    "run-owned recovery prompt alias identity or bytes changed",
                    {"path": str(alias_path), "expected_sha256": record["prompt_sha256"]},
                )

    def _verify_lock(self, state_file: Path, record: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        lock_file = state_file.parent.parent.parent / "active.lock"
        lock = read_json(lock_file)
        kind = str(record.get("record_kind") or "standalone")
        if kind == "child":
            if (
                str(lock.get("record_kind") or "") != "parent"
                or str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(record.get("parent_run_id") or "")
                or str(lock.get("lease_nonce") or "") != str(record.get("parent_lease_nonce") or "")
                or str(lock.get("workflow_id") or "") != str(record.get("parent_workflow_id") or "")
            ):
                raise StateError("BLOCKED_OWNER_MISMATCH", "child does not belong to the exact active parent lease")
            return lock_file, lock
        if kind == "parent":
            if (
                str(lock.get("parent_run_id") or lock.get("run_id") or "") != str(record.get("run_id") or "")
                or str(lock.get("lease_nonce") or "") != str(record.get("lease_nonce") or "")
                or str(lock.get("workflow_id") or "") != str(record.get("workflow_id") or "")
            ):
                raise StateError("BLOCKED_OWNER_MISMATCH", "project parent lease does not match parent record")
            return lock_file, lock
        owner = record["owner"]
        if (
            lock.get("run_id") != record.get("run_id")
            or lock.get("manifest_sha256") != record.get("manifest_sha256")
            or lock.get("owner", {}).get("nonce") != owner.get("nonce")
            or lock.get("owner", {}).get("epoch") != owner.get("epoch")
        ):
            raise StateError("BLOCKED_OWNER_MISMATCH", "project lease does not match run owner")
        return lock_file, lock

    def transition(
        self,
        run_dir: str | os.PathLike[str],
        phase: str,
        *,
        session_id: str | None = None,
        target_id: str | None = None,
        conversation_url: str | None = None,
        submission_receipt: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        block_code: str | None = None,
        recovery_event: dict[str, Any] | None = None,
        rebind_reason: str | None = None,
        app_evidence_ref: str | None = None,
        selection_evidence_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if phase not in PHASES:
            raise StateError("PHASE_INVALID", f"unknown phase: {phase}")
        state_file, record = self.load(run_dir)
        self.verify_manifest(record)
        lock_file, lock = self._verify_lock(state_file, record)
        current = str(record["phase"])
        if phase != current and phase not in ALLOWED_TRANSITIONS.get(current, set()):
            raise StateError("PHASE_TRANSITION_INVALID", f"cannot transition {current} -> {phase}")
        if current in {"SUBMISSION_UNCERTAIN_IDENTITY_MISSING", "BLOCKED_RECOVERY_EXHAUSTED"} and phase == "SEND_REJECTED":
            event_kind = str((recovery_event or {}).get("kind") or "")
            if (
                record.get("session_id")
                or record.get("conversation_url")
                or event_kind != "verified-mutation-disallowed-reclassification"
            ):
                raise StateError(
                    "UNCERTAIN_RECLASSIFICATION_UNPROVEN",
                    "uncertain or exhausted recovery can be reclassified only with verified mutationAllowed=false evidence and no identity",
                )
        if (
            current == "SEND_REJECTED"
            and phase == "PREFLIGHTED"
            and str(record.get("record_kind") or "standalone") == "child"
            and (state_file.parent / "send.claim").exists()
        ):
            authority = record.get("pre_submit_retry_authority")
            if not isinstance(authority, dict) or authority.get("eligible") is not True or authority.get("consumed_at") is not None:
                raise StateError(
                    "PRE_SUBMIT_RETRY_AUTHORITY_MISSING",
                    "claimed SEND_REJECTED child requires unconsumed exact retry authority",
                )
        if current == "RECOVERY_REQUIRED" and phase == "SEND_REJECTED":
            event = recovery_event or {}
            required = {
                "kind": "verified-mutation-disallowed-reclassification",
                "mutation_allowed": False,
                "send_click_status": "unresolved",
                "send_click_reason": "not-enabled",
                "assistant_count": 0,
                "session_id": record.get("session_id"),
                "target_id": record.get("current_target_id"),
            }
            mismatches = {
                key: {"actual": event.get(key), "expected": value}
                for key, value in required.items()
                if event.get(key) != value
            }
            session_status = str(event.get("session_status") or "")
            observed_url = str(event.get("observed_url") or "")
            expired_sent_session = bool(
                session_status == "sent"
                and event.get("session_deadline_expired") is True
                and event.get("target_absent") is True
            )
            evidence_error: str | None = None
            try:
                evidence_path = Path(str(event.get("evidence_path") or "")).expanduser().resolve(strict=True)
                evidence_path.relative_to(state_file.parent)
                if not evidence_path.is_file() or evidence_path.is_symlink():
                    evidence_error = "evidence-not-regular"
                elif sha256_file(evidence_path) != str(event.get("evidence_sha256") or ""):
                    evidence_error = "evidence-hash-mismatch"
            except (OSError, RuntimeError, ValueError):
                evidence_error = "evidence-path-invalid"
            if (
                mismatches
                or (session_status not in {"complete", "timeout"} and not expired_sent_session)
                or record.get("conversation_url")
                or not record.get("session_id")
                or not record.get("current_target_id")
                or not observed_url.startswith("https://chatgpt.com/")
                or CANONICAL_CHAT_RE.fullmatch(observed_url)
                or evidence_error is not None
            ):
                raise StateError(
                    "RECOVERY_RECLASSIFICATION_UNPROVEN",
                    "recovery-required run can be reclassified only when the exact session proves the send click never mutated",
                    {
                        "mismatches": mismatches,
                        "session_status": session_status,
                        "observed_url": observed_url,
                        "evidence_error": evidence_error,
                    },
                )
        if phase == "PROVIDER_FAILED_TERMINAL":
            event = recovery_event or {}
            evidence_error: str | None = None
            try:
                evidence_path = Path(str(event.get("answer_path") or "")).expanduser().resolve(strict=True)
                evidence_path.relative_to(state_file.parent)
                if not evidence_path.is_file() or evidence_path.is_symlink():
                    evidence_error = "evidence-not-regular"
                elif sha256_file(evidence_path) != str(event.get("answer_sha256") or ""):
                    evidence_error = "evidence-hash-mismatch"
                elif evidence_path.stat().st_size != int(event.get("answer_bytes") or -1):
                    evidence_error = "evidence-size-mismatch"
            except (OSError, RuntimeError, TypeError, ValueError):
                evidence_error = "evidence-path-invalid"
            if (
                str(event.get("kind") or "") != "provider-terminal-error-ui"
                or str(event.get("signature") or "") != "chatgpt-stream-error-retry-v1"
                or str(event.get("provider_status") or "").lower()
                not in {"complete", "completed", "done", "response_ready", "history-adjudicated-terminal"}
                or not record.get("session_id")
                or not record.get("current_target_id")
                or not record.get("conversation_url")
                or record.get("result") is not None
                or evidence_error is not None
            ):
                raise StateError(
                    "PROVIDER_TERMINAL_FAILURE_UNPROVEN",
                    "provider terminal failure requires exact identity and immutable provider-error answer evidence",
                    {"evidence_error": evidence_error},
                )

        now = utc_now()
        if session_id:
            existing_session = record.get("session_id")
            if existing_session and existing_session != session_id:
                raise StateError("SESSION_ID_IMMUTABLE", "session_id cannot change")
            record["session_id"] = session_id
        if conversation_url is not None:
            url = canonical_conversation_url(conversation_url)
            existing_url = record.get("conversation_url")
            if existing_url and existing_url != url:
                raise StateError("CONVERSATION_URL_IMMUTABLE", "canonical conversation URL cannot change")
            record["conversation_url"] = url
        if phase == "URL_BOUND" and not record.get("conversation_url"):
            raise StateError("CONVERSATION_IDENTITY_MISSING", "URL_BOUND requires canonical conversation URL")
        if phase in {"SUBMITTED", "URL_BOUND", "RESPONSE_IN_PROGRESS"} and not (session_id or record.get("session_id")):
            raise StateError("SESSION_ID_MISSING", f"{phase} requires session_id")

        if target_id:
            previous = record.get("current_target_id")
            if previous and previous != target_id:
                pre_submit_retry = bool(
                    current == "LEASED"
                    and not record.get("session_id")
                    and not record.get("conversation_url")
                    and rebind_reason == "pre-submit-composer-retry"
                )
                recovery_event_kind = str((recovery_event or {}).get("kind") or "")
                recovery_rebind = bool(
                    current == "RECOVERING"
                    and rebind_reason
                    and (
                        record.get("conversation_url")
                        or recovery_event_kind == "history-fingerprint-match"
                    )
                )
                if not (pre_submit_retry or recovery_rebind):
                    raise StateError(
                        "TARGET_REBIND_UNAUTHORIZED",
                        "target change requires a proven pre-submit retry or RECOVERING with exact URL and reason",
                    )
                record["target_rebind_events"].append(
                    {
                        "at": now,
                        "old_target_id": previous,
                        "new_target_id": target_id,
                        "conversation_url": record["conversation_url"],
                        "reason": rebind_reason,
                    }
                )
            record["current_target_id"] = target_id

        if submission_receipt is not None:
            if record.get("submission_receipt") not in (None, submission_receipt):
                raise StateError("SUBMISSION_RECEIPT_IMMUTABLE", "submission receipt cannot be rewritten")
            record["submission_receipt"] = submission_receipt
        if phase == "SUBMISSION_UNCERTAIN_IDENTITY_MISSING" and record.get("conversation_url"):
            raise StateError("UNCERTAIN_PHASE_INVALID", "identity-missing block cannot carry canonical URL")
        if recovery_event is not None:
            record["recovery_events"].append({"at": now, **recovery_event})
            record["recovery_count"] = int(record.get("recovery_count") or 0) + 1
        if app_evidence_ref:
            record["app_evidence_refs"].append({"at": now, "ref": app_evidence_ref})
        if selection_evidence_ref is not None:
            if not isinstance(selection_evidence_ref, dict):
                raise StateError("SELECTION_EVIDENCE_REF_INVALID", "selection evidence ref must be an object")
            path = Path(str(selection_evidence_ref.get("path") or ""))
            expected_sha256 = str(selection_evidence_ref.get("sha256") or "")
            try:
                resolved = path.expanduser().resolve(strict=True)
                resolved.relative_to(state_file.parent)
            except (OSError, RuntimeError, ValueError) as exc:
                raise StateError("SELECTION_EVIDENCE_REF_INVALID", "selection evidence path is outside the run") from exc
            if (
                not resolved.is_file()
                or resolved.is_symlink()
                or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                or sha256_file(resolved) != expected_sha256
            ):
                raise StateError("SELECTION_EVIDENCE_REF_INVALID", "selection evidence hash is invalid")
            refs = record.setdefault("selection_evidence_refs", [])
            candidate = {
                "at": now,
                "kind": str(selection_evidence_ref.get("kind") or "selection"),
                "path": str(resolved),
                "sha256": expected_sha256,
                "target_id": str(selection_evidence_ref.get("target_id") or record.get("current_target_id") or ""),
            }
            if any(
                str(item.get("path") or "") == candidate["path"]
                and str(item.get("sha256") or "") != candidate["sha256"]
                for item in refs
                if isinstance(item, dict)
            ):
                raise StateError("SELECTION_EVIDENCE_REF_CONFLICT", "selection evidence path changed hash")
            refs.append(candidate)
        if result is not None:
            if record.get("result") not in (None, result):
                raise StateError("RESULT_IMMUTABLE", "captured result descriptor cannot be rewritten")
            record["result"] = result
        if phase == "COMPLETE" and not record.get("result"):
            raise StateError("COMPLETION_EVIDENCE_MISSING", "COMPLETE requires result descriptor")

        if phase == "SEND_STARTED" and str(record.get("record_kind") or "standalone") == "child":
            claim_file = state_file.parent / "send.claim"
            if not claim_file.is_file() or claim_file.is_symlink():
                raise StateError("CHILD_SEND_CLAIM_MISSING", "child SEND_STARTED requires its durable O_EXCL send claim")
            claim = read_json(claim_file)
            if (
                claim.get("schema") != "codex.chatgpt.child-send-claim/v1"
                or str(claim.get("run_id") or "") != str(record.get("run_id") or "")
                or str(claim.get("parent_run_id") or "") != str(record.get("parent_run_id") or "")
                or str(claim.get("manifest_sha256") or "") != str(record.get("manifest_sha256") or "")
                or str(claim.get("prompt_sha256") or "") != str(record.get("prompt_sha256") or "")
            ):
                raise StateError("CHILD_SEND_CLAIM_INVALID", "child send claim identity is not exact")
            authority = record.get("pre_submit_retry_authority")
            reuse_claim = bool(
                int(record.get("send_attempt_count") or 0) == 1
                and isinstance(authority, dict)
                and authority.get("eligible") is True
                and authority.get("consumed_at") is None
                and str(authority.get("claim_sha256") or "") == sha256_file(claim_file)
                and str(authority.get("run_id") or "") == str(record.get("run_id") or "")
                and str(authority.get("parent_run_id") or "") == str(record.get("parent_run_id") or "")
            )
            if reuse_claim:
                authority = dict(authority)
                authority["consumed_at"] = now
                record["pre_submit_retry_authority"] = authority
                record["pre_submit_retry_count"] = int(record.get("pre_submit_retry_count") or 0) + 1
            else:
                attempts = int(record.get("send_attempt_count") or 0) + 1
                if attempts > int(record.get("send_limit") or 1):
                    raise StateError("SEND_ALREADY_ATTEMPTED", "child send attempt exceeds its immutable limit")
                record["send_attempt_count"] = attempts
            record["send_claim"] = {
                "path": str(claim_file),
                "sha256": sha256_file(claim_file),
                "claimed_at": claim.get("claimed_at"),
            }

        if phase != current:
            record["phase_events"].append({"from": current, "to": phase, "at": now})
        record["phase"] = phase
        record["phase_at"] = now
        record["updated_at"] = now
        if phase.startswith("BLOCKED_") or phase in {
            "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
            "PROVIDER_FAILED_TERMINAL",
        }:
            record["terminal_block_code"] = block_code or phase
        else:
            record["terminal_block_code"] = None

        write_json_atomic(state_file, record)
        if str(record.get("record_kind") or "standalone") != "child":
            lock.update(
                {
                    "phase": phase,
                    "session_id": record.get("session_id"),
                    "target_id": record.get("current_target_id"),
                    "conversation_url": record.get("conversation_url"),
                    "heartbeat_at": now,
                }
            )
            write_json_atomic(lock_file, lock)
        if (
            str(record.get("record_kind") or "standalone") != "child"
            and phase in {
                "COMPLETE",
                "PROVIDER_FAILED_TERMINAL",
                "CANCELLED_PRE_SUBMISSION",
                "ABANDONED_UNCERTAIN",
            }
        ):
            current_lock = read_json(lock_file)
            if current_lock.get("run_id") == record["run_id"] and current_lock.get("owner", {}).get("nonce") == record["owner"]["nonce"]:
                lock_file.unlink()
        return record

    def list_project_runs(self, project_root: str | os.PathLike[str]) -> list[dict[str, Any]]:
        root = canonical_project_root(project_root)
        paths = self.paths(root, "unused")
        rows: list[dict[str, Any]] = []
        if not paths.runs_dir.exists():
            return rows
        for path in sorted(paths.runs_dir.glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            record = read_json(path)
            rows.append({"run_id": record.get("run_id"), "phase": record.get("phase"), "run_dir": str(path.parent)})
        return rows


def _json_arg(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise StateError("ARGUMENT_INVALID", "JSON argument must be an object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Immutable project/session state for the agbrowse ChatGPT bridge.")
    parser.add_argument("--state-root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--project-root", required=True)
    start.add_argument("--manifest", required=True)
    start.add_argument("--contract", required=True)

    show = sub.add_parser("show")
    show.add_argument("--run", required=True)

    trans = sub.add_parser("transition")
    trans.add_argument("--run", required=True)
    trans.add_argument("--phase", required=True, choices=sorted(PHASES))
    trans.add_argument("--session-id")
    trans.add_argument("--target-id")
    trans.add_argument("--conversation-url")
    trans.add_argument("--submission-receipt-json")
    trans.add_argument("--result-json")
    trans.add_argument("--block-code")
    trans.add_argument("--recovery-event-json")
    trans.add_argument("--rebind-reason")
    trans.add_argument("--app-evidence-ref")

    ls = sub.add_parser("list")
    ls.add_argument("--project-root", required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    store = RunStore(args.state_root)
    try:
        if args.command == "start":
            contract = read_json(Path(args.contract))
            result = store.create_run(project_root=args.project_root, manifest_path=args.manifest, agbrowse_contract=contract)
        elif args.command == "show":
            _, result = store.load(args.run)
        elif args.command == "transition":
            result = store.transition(
                args.run,
                args.phase,
                session_id=args.session_id,
                target_id=args.target_id,
                conversation_url=args.conversation_url,
                submission_receipt=_json_arg(args.submission_receipt_json),
                result=_json_arg(args.result_json),
                block_code=args.block_code,
                recovery_event=_json_arg(args.recovery_event_json),
                rebind_reason=args.rebind_reason,
                app_evidence_ref=args.app_evidence_ref,
            )
        else:
            result = {"ok": True, "runs": store.list_project_runs(args.project_root)}
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except StateError as exc:
        print(json.dumps(exc.envelope(), ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
