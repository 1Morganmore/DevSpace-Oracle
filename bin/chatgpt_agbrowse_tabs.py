from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


DEFAULT_STATE_ROOT = Path.home() / ".codex" / "state" / "chatgpt-agbrowse"
CANONICAL_CHAT_RE = re.compile(r"https://chatgpt\.com/c/[A-Za-z0-9_-]+/?$")
PRE_SUBMIT_PHASES = {
    "CREATED",
    "PREFLIGHTED",
    "LEASED",
    "PREFLIGHT_BLOCKED",
    "SEND_REJECTED",
    "BLOCKED_APP_TRANSACTION",
    "CANCELLED_PRE_SUBMISSION",
}
ACTIVE_PARENT_CLEANUP_PHASES = {
    "PARENT_ACTIVE",
    "PARENT_DRAINING",
    "PARENT_RECOVERY_REQUIRED",
}
WEB_MULTI_MANIFEST_SCHEMAS = {
    "codex.chatgpt.web-multi/v1",
    "codex.chatgpt.web-multi/v2",
}
Runner = Callable[[list[str], dict[str, str], int], subprocess.CompletedProcess[str]]


class TabLifecycleError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_runner(command: list[str], env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def safe_agbrowse_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base or os.environ)
    env["AGBROWSE_JSON_ERRORS"] = "1"
    env["AGBROWSE_UPDATE_CHECK"] = "0"
    # The bridge owns exact cleanup.  Upstream's broad TTL/count cleanup must
    # not close submitted conversations from another completed run.
    env["AGBROWSE_MAX_TABS"] = "100000"
    env["AGBROWSE_TAB_IDLE"] = "999999h"
    env["AGBROWSE_PROVIDER_POOL_MAX_PER_KEY"] = "100000"
    env["AGBROWSE_PROVIDER_POOL_GLOBAL_MAX"] = "100000"
    env["AGBROWSE_PROVIDER_POOL_TTL"] = "999999h"
    return env


def write_immutable_json_exclusive(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        if not path.is_file() or path.is_symlink() or path.read_bytes() != data:
            raise TabLifecycleError("TAB_IMMUTABLE_EVIDENCE_CONFLICT", "cleanup proof differs across retry")
    else:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TabLifecycleError("TAB_STATE_INVALID", f"expected JSON object: {path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def normalized_url(value: str | None) -> str:
    raw = str(value or "").strip()
    if raw == "about:blank":
        return raw
    parts = urlsplit(raw)
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, parts.query, parts.fragment))


def is_pre_submit_composer_url(value: str) -> bool:
    if value == "about:blank":
        return True
    parts = urlsplit(value)
    return parts.scheme.casefold() == "https" and parts.netloc.casefold() == "chatgpt.com" and (parts.path or "/") == "/"


def _tab_id(tab: dict[str, Any]) -> str:
    return str(tab.get("targetId") or tab.get("target_id") or "")


def _tab_url(tab: dict[str, Any]) -> str:
    return str(tab.get("url") or "")


def _tabs_hash(tabs: list[dict[str, Any]]) -> str:
    safe = sorted(
        ({"target_id": _tab_id(tab), "url": normalized_url(_tab_url(tab)), "type": str(tab.get("type") or "")} for tab in tabs),
        key=lambda item: (item["target_id"], item["url"]),
    )
    return hashlib.sha256(json.dumps(safe, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    """Reject Windows reparse points as well as portable symlinks."""
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return path.is_symlink() or bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


class TabLifecycle:
    def __init__(
        self,
        *,
        state_root: Path | None = None,
        executable: str = "agbrowse",
        runner: Runner | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ):
        self.state_root = (state_root or DEFAULT_STATE_ROOT).expanduser().resolve()
        self.executable = shutil.which(executable) or executable
        self.runner = runner or default_runner
        self.env = safe_agbrowse_env(env)
        self.timeout = timeout

    @staticmethod
    def _state_file(run_dir: str | os.PathLike[str]) -> Path:
        path = Path(run_dir).expanduser().resolve()
        return path if path.name == "run.json" else path / "run.json"

    def _run(self, run_dir: str | os.PathLike[str]) -> tuple[Path, dict[str, Any]]:
        state_file = self._state_file(run_dir)
        return state_file, read_json(state_file)

    @staticmethod
    def _owned_regular_file(state_file: Path, value: Any) -> Path | None:
        """Return a resolved, immutable run-owned file, otherwise None."""
        raw = Path(str(value or "")).expanduser()
        if not str(value or ""):
            return None
        try:
            run_dir = state_file.parent.resolve(strict=True)
            candidate = raw.resolve(strict=True)
            candidate.relative_to(run_dir)
            if not candidate.is_file() or _is_reparse_point(raw):
                return None
            # A reparse point in a run-local parent can otherwise escape the
            # run before the resolved containment check sees it.
            lexical = raw.absolute()
            while True:
                if _is_reparse_point(lexical):
                    return None
                if lexical == run_dir:
                    break
                parent = lexical.parent
                if parent == lexical:
                    return None
                lexical = parent
            if not stat.S_ISREG(candidate.stat().st_mode):
                return None
            return candidate
        except (OSError, RuntimeError, ValueError):
            return None

    def _complete_result_capture_valid(self, state_file: Path, record: dict[str, Any]) -> bool:
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        result_path = self._owned_regular_file(state_file, result.get("path"))
        if result_path is None:
            return False
        try:
            size = result_path.stat().st_size
            expected_bytes = int(result.get("bytes") or -1)
            actual_sha256 = hashlib.sha256(result_path.read_bytes()).hexdigest()
        except (OSError, TypeError, ValueError):
            return False
        return (
            size > 0
            and size == expected_bytes
            and re.fullmatch(r"[0-9a-f]{64}", str(result.get("sha256") or "")) is not None
            and actual_sha256 == str(result.get("sha256") or "")
            and str(result.get("provider_status") or "").lower()
            in {"complete", "completed", "done", "response_ready", "history-adjudicated-terminal"}
            and isinstance(result.get("evidence"), dict)
            and bool(result["evidence"])
        )

    def _provider_failed_terminal_proof_valid(self, state_file: Path, record: dict[str, Any]) -> bool:
        if record.get("result") is not None or str(record.get("terminal_block_code") or "") != "PROVIDER_TERMINAL_ERROR_UI":
            return False
        failures = [
            event
            for event in record.get("recovery_events") or []
            if isinstance(event, dict) and str(event.get("kind") or "") == "provider-terminal-error-ui"
        ]
        if len(failures) != 1:
            return False
        failure = failures[0]
        answer_path = self._owned_regular_file(state_file, failure.get("answer_path"))
        if answer_path is None:
            return False
        try:
            return (
                answer_path.stat().st_size > 0
                and answer_path.stat().st_size == int(failure.get("answer_bytes") or -1)
                and hashlib.sha256(answer_path.read_bytes()).hexdigest() == str(failure.get("answer_sha256") or "")
                and str(failure.get("signature") or "") == "chatgpt-stream-error-retry-v1"
                and str(failure.get("provider_status") or "").lower()
                in {"complete", "completed", "done", "response_ready", "history-adjudicated-terminal"}
                and str(failure.get("session_id") or "") == str(record.get("session_id") or "")
                and str(failure.get("target_id") or "") == str(record.get("current_target_id") or "")
                and normalized_url(str(failure.get("conversation_url") or ""))
                == normalized_url(str(record.get("conversation_url") or ""))
            )
        except (OSError, TypeError, ValueError):
            return False

    @staticmethod
    def _lifecycle_file(state_file: Path) -> Path:
        return state_file.parent / "tab-lifecycle.json"

    def _append_event(self, state_file: Path, record: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        path = self._lifecycle_file(state_file)
        if path.is_file():
            lifecycle = read_json(path)
            if (
                lifecycle.get("run_id") != record.get("run_id")
                or lifecycle.get("manifest_sha256") != record.get("manifest_sha256")
            ):
                raise TabLifecycleError("TAB_LIFECYCLE_OWNER_MISMATCH", "tab lifecycle owner does not match run state")
        else:
            lifecycle = {
                "schema": "codex.chatgpt.agbrowse-tab-lifecycle/v1",
                "run_id": record.get("run_id"),
                "project_key": record.get("project_key"),
                "manifest_sha256": record.get("manifest_sha256"),
                "events": [],
            }
        lifecycle["events"].append({"at": utc_now(), **event})
        lifecycle["updated_at"] = utc_now()
        write_json_atomic(path, lifecycle)
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "event": event}

    def record_owned(self, run_dir: str, *, target_id: str, url: str, stage: str) -> dict[str, Any]:
        if not target_id:
            raise TabLifecycleError("TAB_TARGET_MISSING", "owned target id is required")
        state_file, record = self._run(run_dir)
        return self._append_event(
            state_file,
            record,
            {"kind": "owned", "target_id": target_id, "url": normalized_url(url), "stage": stage},
        )

    def record_protected(self, run_dir: str, *, target_id: str, conversation_url: str | None, stage: str) -> dict[str, Any]:
        state_file, record = self._run(run_dir)
        return self._append_event(
            state_file,
            record,
            {
                "kind": "protected",
                "target_id": target_id,
                "url": normalized_url(conversation_url),
                "stage": stage,
            },
        )

    def list_tabs(self) -> list[dict[str, Any]]:
        command = [self.executable, "tabs", "--json"]
        completed = self.runner(command, self.env, self.timeout)
        if completed.returncode != 0:
            raise TabLifecycleError(
                "TAB_LIST_FAILED",
                "agbrowse tabs failed",
                {"exit_code": completed.returncode, "stderr": (completed.stderr or "")[-1000:]},
            )
        try:
            payload = json.loads((completed.stdout or "").strip())
        except json.JSONDecodeError as exc:
            raise TabLifecycleError("TAB_LIST_JSON_INVALID", "agbrowse tabs returned invalid JSON") from exc
        tabs = payload.get("tabs") if isinstance(payload, dict) else payload
        if not isinstance(tabs, list) or not all(isinstance(item, dict) for item in tabs):
            raise TabLifecycleError("TAB_LIST_JSON_INVALID", "agbrowse tabs JSON must be a list")
        return tabs

    @staticmethod
    def _foreign_state_evidence(path: Path, *, target_id: str, url: str, reason: str) -> dict[str, Any]:
        return {
            "state_file": str(path),
            "target_id": target_id,
            "url": normalized_url(url),
            "reason": reason,
        }

    @staticmethod
    def _foreign_run_schema_valid(candidate: dict[str, Any]) -> bool:
        """Require the common identity fields before classifying a foreign run."""
        return (
            candidate.get("schema") == "codex.chatgpt.agbrowse-run/v1"
            and isinstance(candidate.get("run_id"), str)
            and bool(candidate["run_id"])
            and isinstance(candidate.get("project_key"), str)
            and bool(candidate["project_key"])
            and isinstance(candidate.get("phase"), str)
        )

    @staticmethod
    def _foreign_parent_terminal_coordinator_valid(candidate: dict[str, Any]) -> bool:
        """Return whether a foreign parent is provably not a tab owner.

        Web-Multi parent records are coordinators, not browser-owning runs.  A
        ``record_kind`` declaration is not sufficient evidence for that
        exception: cleanup must stop for an active, incomplete, malformed, or
        directly-identifying parent record.  The child run records remain in
        the bounded scan and are still checked independently.
        """
        # Parent coordinators have no browser/session/submission identity at
        # all.  Treat future direct identity field names conservatively too.
        if any(
            token in str(field).casefold()
            for field in candidate
            for token in ("target", "session", "conversation", "submission", "url")
        ):
            return False

        phase = str(candidate.get("phase") or "")
        if (
            str(candidate.get("record_kind") or "") != "parent"
            or phase not in {"PARENT_COMPLETE", "PARENT_FAILED_CLOSED"}
            or str(candidate.get("parent_run_id") or "") != str(candidate.get("run_id") or "")
            or not isinstance(candidate.get("workflow_id"), str)
            or not candidate["workflow_id"]
            or not isinstance(candidate.get("lease_nonce"), str)
            or not candidate["lease_nonce"]
            or not isinstance(candidate.get("project_root"), str)
            or not candidate["project_root"]
            or not isinstance(candidate.get("manifest_path"), str)
            or not candidate["manifest_path"]
            or re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("manifest_sha256") or "")) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("prompt_sha256") or "")) is None
            or not isinstance(candidate.get("requested"), dict)
            or candidate["requested"].get("workflow") != "web-multi-gpt"
            or not isinstance(candidate.get("agbrowse"), dict)
            or not isinstance(candidate.get("owner"), dict)
            or not isinstance(candidate.get("children"), list)
            or not isinstance(candidate.get("child_scan"), list)
            or type(candidate.get("owned_open_tabs")) is not int
            or candidate["owned_open_tabs"] != 0
            or not isinstance(candidate.get("phase_events"), list)
            or not candidate["phase_events"]
        ):
            return False

        final_event = candidate["phase_events"][-1]
        if (
            not isinstance(final_event, dict)
            or final_event.get("from") != "PARENT_DRAINING"
            or final_event.get("to") != phase
            or not isinstance(final_event.get("at"), str)
            or not final_event["at"]
        ):
            return False

        for child in candidate["child_scan"]:
            if (
                not isinstance(child, dict)
                or not isinstance(child.get("run_id"), str)
                or not child["run_id"]
                or str(child.get("phase") or "")
                not in {
                    "COMPLETE",
                    "CANCELLED_PRE_SUBMISSION",
                    "SEND_REJECTED",
                    "PROVIDER_FAILED_TERMINAL",
                    "PREFLIGHT_BLOCKED",
                    "BLOCKED_APP_TRANSACTION",
                    "ABANDONED_UNCERTAIN",
                }
                or type(child.get("owned_open_tabs")) is not int
                or child["owned_open_tabs"] != 0
                or child.get("cleanup_pending") is not False
            ):
                return False
        return True

    @staticmethod
    def _foreign_parent_identityless_coordinator_valid(candidate: dict[str, Any]) -> bool:
        """Recognize a parent coordinator that cannot directly own a tab.

        Parent phase and child summaries describe orchestration state, not
        browser ownership. Exact child run records are scanned separately.
        """
        if any(
            token in str(field).casefold()
            for field in candidate
            for token in ("target", "session", "conversation", "submission", "url")
        ):
            return False
        family_field_present = "parent_family" in candidate
        family = str(candidate.get("parent_family") or "") if family_field_present else ""
        requested = candidate.get("requested") if isinstance(candidate.get("requested"), dict) else {}
        if family_field_present:
            if family not in {"web-multi", "parallel-implementation"}:
                return False
            expected_workflow = "web-multi-gpt" if family == "web-multi" else "parallel-implementation-v1"
            if requested.get("workflow") != expected_workflow:
                return False
        elif requested.get("workflow") != "web-multi-gpt":
            return False
        return bool(
            str(candidate.get("record_kind") or "") == "parent"
            and str(candidate.get("parent_run_id") or "") == str(candidate.get("run_id") or "")
            and isinstance(candidate.get("workflow_id"), str)
            and bool(candidate["workflow_id"])
            and isinstance(candidate.get("lease_nonce"), str)
            and bool(candidate["lease_nonce"])
            and isinstance(candidate.get("project_root"), str)
            and bool(candidate["project_root"])
            and isinstance(candidate.get("manifest_path"), str)
            and bool(candidate["manifest_path"])
            and re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("manifest_sha256") or "")) is not None
            and re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("prompt_sha256") or "")) is not None
            and isinstance(candidate.get("agbrowse"), dict)
            and isinstance(candidate.get("owner"), dict)
            and isinstance(candidate.get("children"), list)
            and type(candidate.get("owned_open_tabs")) is int
            and candidate["owned_open_tabs"] == 0
            and isinstance(candidate.get("phase_events"), list)
            and bool(candidate["phase_events"])
        )

    _foreign_parent_coordinator_valid = _foreign_parent_identityless_coordinator_valid

    @staticmethod
    def _normalized_existing_path(value: Any) -> str:
        try:
            return os.path.normcase(os.path.normpath(str(Path(str(value)).expanduser().resolve(strict=True))))
        except (OSError, RuntimeError, ValueError):
            return ""

    @staticmethod
    def _project_key_for_root(normalized_root: str) -> str:
        return hashlib.sha256(normalized_root.rstrip("\\/").encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _coordinator_owner_valid(owner: Any) -> bool:
        if not isinstance(owner, dict):
            return False
        creation_time = owner.get("creation_time")
        return (
            type(owner.get("pid")) is int
            and owner["pid"] > 0
            and (creation_time is None or (type(creation_time) in {int, float} and creation_time >= 0))
            and type(owner.get("alive")) is bool
            and re.fullmatch(r"[0-9a-f]{32}", str(owner.get("nonce") or "")) is not None
            and type(owner.get("epoch")) is int
            and owner["epoch"] > 0
        )

    @classmethod
    def _web_multi_manifest_valid(
        cls,
        *,
        record: Mapping[str, Any],
        expected_workflow_id: str,
        expected_project_root: str,
        child: bool,
    ) -> bool:
        raw_path = Path(str(record.get("manifest_path") or "")).expanduser()
        expected_sha256 = str(record.get("manifest_sha256") or "")
        if (
            not str(record.get("manifest_path") or "")
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or _is_reparse_point(raw_path)
        ):
            return False
        try:
            manifest_path = raw_path.resolve(strict=True)
            if not manifest_path.is_file() or not stat.S_ISREG(manifest_path.stat().st_mode):
                return False
            manifest_bytes = manifest_path.read_bytes()
            if hashlib.sha256(manifest_bytes).hexdigest() != expected_sha256:
                return False
            manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
        except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError, ValueError):
            return False
        if not isinstance(manifest, dict):
            return False
        manifest_root = cls._normalized_existing_path(manifest.get("project_root"))
        if not manifest_root or manifest_root != expected_project_root:
            return False
        if child:
            correlation = manifest.get("workflow_correlation")
            return (
                isinstance(correlation, dict)
                and str(correlation.get("workflow_id") or "") == expected_workflow_id
                and str(correlation.get("stage") or "") == str(record.get("stage_id") or "")
                and type(correlation.get("attempt_index")) is int
                and correlation["attempt_index"] == 1
                and str(manifest.get("app_policy") or "") == "required"
                and type(manifest.get("send_limit")) is int
                and manifest["send_limit"] == 1
                and str(manifest.get("prompt_file_sha256") or "") == str(record.get("prompt_sha256") or "")
            )
        if (
            str(manifest.get("schema") or "") not in WEB_MULTI_MANIFEST_SCHEMAS
            or str(manifest.get("workflow_id") or "") != expected_workflow_id
        ):
            return False
        expected_prompt_sha256 = hashlib.sha256(
            json.dumps(manifest.get("question"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return expected_prompt_sha256 == str(record.get("prompt_sha256") or "")

    @classmethod
    def _foreign_parent_active_coordinator_owned_by_child(
        cls,
        *,
        candidate_path: Path,
        candidate: dict[str, Any],
        child_state_file: Path,
        child: Mapping[str, Any],
    ) -> bool:
        """Allow only the exact active coordinator durably bound to this child."""
        phase = str(candidate.get("phase") or "")
        parent_run_id = str(child.get("parent_run_id") or "")
        child_run_id = str(child.get("run_id") or "")
        parent_workflow_id = str(child.get("parent_workflow_id") or "")
        parent_lease_nonce = str(child.get("parent_lease_nonce") or "")
        if (
            phase not in ACTIVE_PARENT_CLEANUP_PHASES
            or str(child.get("schema") or "") != "codex.chatgpt.agbrowse-run/v1"
            or str(child.get("record_kind") or "") != "child"
            or str(child.get("phase") or "") not in {"COMPLETE", "PROVIDER_FAILED_TERMINAL"}
            or not parent_run_id
            or not child_run_id
            or not parent_workflow_id
            or re.fullmatch(r"[0-9a-f]{32}", parent_lease_nonce) is None
        ):
            return False

        try:
            child_file = child_state_file.resolve(strict=True)
            candidate_file = candidate_path.resolve(strict=True)
            expected_parent_file = (child_file.parent.parent / parent_run_id / "run.json").resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return False
        if (
            child_file.name != "run.json"
            or child_file.parent.name != child_run_id
            or candidate_file != expected_parent_file
            or candidate_file.name != "run.json"
            or candidate_file.parent.name != parent_run_id
            or candidate_file.parent.parent.name != "runs"
        ):
            return False

        parent_project_root = cls._normalized_existing_path(candidate.get("project_root"))
        child_project_root = cls._normalized_existing_path(child.get("project_root"))
        project_key = str(child.get("project_key") or "")
        if (
            str(candidate.get("record_kind") or "") != "parent"
            or str(candidate.get("run_id") or "") != parent_run_id
            or str(candidate.get("parent_run_id") or "") != parent_run_id
            or str(candidate.get("project_key") or "") != project_key
            or candidate_file.parent.parent.parent.name != project_key
            or not parent_project_root
            or parent_project_root != child_project_root
            or cls._project_key_for_root(parent_project_root) != project_key
            or str(candidate.get("workflow_id") or "") != parent_workflow_id
            or str(candidate.get("lease_nonce") or "") != parent_lease_nonce
            or re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("prompt_sha256") or "")) is None
            or not isinstance(candidate.get("requested"), dict)
            or candidate["requested"].get("workflow") != "web-multi-gpt"
            or candidate["requested"].get("mode") != "GPT-5.6"
            or candidate["requested"].get("app_policy") != "required"
            or not isinstance(candidate.get("agbrowse"), dict)
            or not candidate["agbrowse"]
            or candidate["agbrowse"] != child.get("agbrowse")
            or not cls._coordinator_owner_valid(candidate.get("owner"))
            or not isinstance(candidate.get("children"), list)
            or type(candidate.get("owned_open_tabs")) is not int
            or candidate["owned_open_tabs"] != 0
            or not isinstance(candidate.get("phase_events"), list)
            or not candidate["phase_events"]
        ):
            return False

        if any(
            token in str(field).casefold()
            for field in candidate
            for token in ("target", "session", "conversation", "submission", "url")
        ):
            return False

        final_event = candidate["phase_events"][-1]
        if (
            not isinstance(final_event, dict)
            or str(final_event.get("to") or "") != phase
            or not isinstance(final_event.get("at"), str)
            or not final_event["at"]
        ):
            return False

        child_entries = [
            item
            for item in candidate["children"]
            if isinstance(item, dict) and str(item.get("run_id") or "") == child_run_id
        ]
        if len(child_entries) != 1:
            return False
        child_entry = child_entries[0]
        try:
            child_entry_matches = (
                str(child_entry.get("stage_id") or "") == str(child.get("stage_id") or "")
                and str(child_entry.get("role") or "") == str(child.get("role") or "")
                and int(child_entry.get("lane")) == int(child.get("lane"))
                and int(child_entry.get("iteration")) == int(child.get("iteration"))
            )
        except (TypeError, ValueError):
            return False
        if not child_entry_matches:
            return False

        return (
            cls._web_multi_manifest_valid(
                record=candidate,
                expected_workflow_id=parent_workflow_id,
                expected_project_root=parent_project_root,
                child=False,
            )
            and cls._web_multi_manifest_valid(
                record=child,
                expected_workflow_id=parent_workflow_id,
                expected_project_root=child_project_root,
                child=True,
            )
        )

    @staticmethod
    def _foreign_parent_user_stop_coordinator_owned_by_child(
        *,
        candidate_path: Path,
        candidate: dict[str, Any],
        child_state_file: Path,
        child: Mapping[str, Any],
    ) -> bool:
        """Allow only the exact parent that owns this child's immutable stop epoch."""
        try:
            child_file = child_state_file.resolve(strict=True)
            expected_parent = (
                child_file.parent.parent / str(child.get("parent_run_id") or "") / "run.json"
            ).resolve(strict=True)
            candidate_file = candidate_path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return False
        stop = child.get("user_stop") if isinstance(child.get("user_stop"), dict) else {}
        request = (
            (candidate.get("user_stop_requests") or {}).get(str(child.get("run_id") or ""))
            if isinstance(candidate.get("user_stop_requests"), dict)
            else None
        )
        return bool(
            candidate_file == expected_parent
            and str(candidate.get("record_kind") or "") == "parent"
            and str(candidate.get("phase") or "") == "USER_STOP_REQUESTED"
            and str(child.get("record_kind") or "") == "child"
            and str(child.get("phase") or "") == "USER_STOP_REQUESTED"
            and str(candidate.get("run_id") or "") == str(child.get("parent_run_id") or "")
            and str(candidate.get("project_root") or "") == str(child.get("project_root") or "")
            and str(candidate.get("project_key") or "") == str(child.get("project_key") or "")
            and str(candidate.get("workflow_id") or "") == str(child.get("parent_workflow_id") or "")
            and str(candidate.get("lease_nonce") or "") == str(child.get("parent_lease_nonce") or "")
            and isinstance(request, dict)
            and str(request.get("stop_epoch_nonce") or "") == str(stop.get("stop_epoch_nonce") or "")
        )

    @staticmethod
    def _foreign_parent_user_stop_rejected_coordinator_owned_by_child(
        *,
        candidate_path: Path,
        candidate: dict[str, Any],
        child_state_file: Path,
        child: Mapping[str, Any],
    ) -> bool:
        """Recognize only an immutably adopted zero-provider sibling owner."""
        try:
            child_file = child_state_file.resolve(strict=True)
            expected_parent = (
                child_file.parent.parent
                / str(child.get("parent_run_id") or "")
                / "run.json"
            ).resolve(strict=True)
            if candidate_path.resolve(strict=True) != expected_parent:
                return False
            descriptor = (
                child.get("legacy_send_claim_adoption")
                if isinstance(child.get("legacy_send_claim_adoption"), dict)
                else {}
            )
            adoption_path = Path(str(descriptor.get("path") or "")).resolve(strict=True)
            if (
                adoption_path
                != (child_file.parent / "user-stop" / "legacy-send-claim-adoption.json").resolve(
                    strict=True
                )
                or adoption_path.is_symlink()
                or hashlib.sha256(adoption_path.read_bytes()).hexdigest()
                != str(descriptor.get("sha256") or "")
                or adoption_path.stat().st_size != int(descriptor.get("bytes") or -1)
            ):
                return False
            adoption = read_json(adoption_path)
            claim_file = child_file.parent / "send.claim"
            claim_bytes = claim_file.read_bytes()
            claim = adoption.get("claim") if isinstance(adoption.get("claim"), dict) else {}
            authority = (
                adoption.get("authority")
                if isinstance(adoption.get("authority"), dict)
                else {}
            )
            zero = (
                authority.get("zero_provider_evidence")
                if isinstance(authority.get("zero_provider_evidence"), dict)
                else {}
            )
            stdout = zero.get("stdout") if isinstance(zero.get("stdout"), dict) else {}
            stderr = zero.get("stderr") if isinstance(zero.get("stderr"), dict) else {}
            modern_zero_valid = False
            legacy_zero_valid = False
            if stdout and stderr:
                stdout_path = Path(str(stdout.get("path") or "")).resolve(strict=True)
                stderr_path = Path(str(stderr.get("path") or "")).resolve(strict=True)
                modern_zero_valid = bool(
                    stdout_path
                    == (
                        child_file.parent / "agbrowse-evidence" / "send.stdout.txt"
                    ).resolve(strict=True)
                    and stderr_path
                    == (
                        child_file.parent / "agbrowse-evidence" / "send.stderr.txt"
                    ).resolve(strict=True)
                    and not stdout_path.is_symlink()
                    and not stderr_path.is_symlink()
                    and hashlib.sha256(stdout_path.read_bytes()).hexdigest()
                    == str(stdout.get("sha256") or "")
                    and hashlib.sha256(stderr_path.read_bytes()).hexdigest()
                    == str(stderr.get("sha256") or "")
                )
            else:
                legacy = (
                    zero.get("legacy_process_not_created")
                    if isinstance(zero.get("legacy_process_not_created"), dict)
                    else {}
                )
                pinned = (
                    zero.get("pinned_executable")
                    if isinstance(zero.get("pinned_executable"), dict)
                    else {}
                )
                legacy_path = Path(str(legacy.get("path") or "")).resolve(strict=True)
                legacy_zero_valid = bool(
                    legacy_path
                    == (
                        child_file.parent
                        / "agbrowse-evidence"
                        / "send-process-not-created-legacy-evidence.json"
                    ).resolve(strict=True)
                    and not legacy_path.is_symlink()
                    and legacy_path.stat().st_size == int(legacy.get("bytes") or -1)
                    and hashlib.sha256(legacy_path.read_bytes()).hexdigest()
                    == str(legacy.get("sha256") or "")
                    and Path(str(pinned.get("path") or "")).is_absolute()
                    and re.fullmatch(
                        r"[0-9a-f]{64}", str(pinned.get("sha256") or "")
                    )
                    is not None
                )
            child_identity = adoption.get("child_identity")
            parent_identity = adoption.get("parent_identity")
            child_entry = adoption.get("parent_child_entry")
        except (OSError, RuntimeError, TypeError, ValueError, TabLifecycleError):
            return False
        expected_child_identity = {
            "run_id": child.get("run_id"),
            "parent_run_id": child.get("parent_run_id"),
            "parent_workflow_id": child.get("parent_workflow_id"),
            "parent_lease_nonce": child.get("parent_lease_nonce"),
            "project_root": child.get("project_root"),
            "project_key": child.get("project_key"),
            "stage_id": child.get("stage_id"),
            "role": child.get("role"),
            "lane": child.get("lane"),
            "iteration": child.get("iteration"),
            "manifest_sha256": child.get("manifest_sha256"),
            "prompt_sha256": child.get("prompt_sha256"),
            "send_limit": child.get("send_limit"),
        }
        expected_parent_identity = {
            "run_id": candidate.get("run_id"),
            "workflow_id": candidate.get("workflow_id"),
            "lease_nonce": candidate.get("lease_nonce"),
            "project_root": candidate.get("project_root"),
            "project_key": candidate.get("project_key"),
            "manifest_sha256": candidate.get("manifest_sha256"),
        }
        matching_entries = [
            entry
            for entry in candidate.get("children") or []
            if isinstance(entry, dict)
            and str(entry.get("run_id") or "") == str(child.get("run_id") or "")
        ]
        expected_child_entry = (
            {
                key: matching_entries[0].get(key)
                for key in ("run_id", "stage_id", "role", "lane", "iteration")
            }
            if len(matching_entries) == 1
            else None
        )
        return bool(
            adoption.get("schema") == "codex.chatgpt.legacy-send-claim-adoption/v1"
            and str(candidate.get("record_kind") or "") == "parent"
            and str(candidate.get("phase") or "") == "USER_STOP_REQUESTED"
            and str(child.get("record_kind") or "") == "child"
            and str(child.get("phase") or "") == "SEND_REJECTED"
            and child_identity == expected_child_identity
            and parent_identity == expected_parent_identity
            and expected_child_entry is not None
            and child_entry == expected_child_entry
            and authority.get("source_phase") == "SEND_REJECTED"
            and zero.get("schema")
            == "codex.chatgpt.zero-provider-failure-evidence/v1"
            and str(zero.get("run_id") or "") == str(child.get("run_id") or "")
            and str(zero.get("parent_run_id") or "")
            == str(child.get("parent_run_id") or "")
            and str(zero.get("stage_id") or "") == str(child.get("stage_id") or "")
            and isinstance(zero.get("exit_code"), int)
            and int(zero["exit_code"]) != 0
            and bool(zero.get("error_code"))
            and bool(zero.get("error_stage"))
            and (modern_zero_valid or legacy_zero_valid)
            and claim.get("path") == str(claim_file)
            and claim.get("sha256") == hashlib.sha256(claim_bytes).hexdigest()
            and claim.get("bytes") == len(claim_bytes)
            and claim.get("bytes_base64")
            == base64.b64encode(claim_bytes).decode("ascii")
        )

    @staticmethod
    def _foreign_parent_stop_scope_owned_by_child(
        *, candidate_path: Path, candidate: dict[str, Any], child_state_file: Path, child: Mapping[str, Any]
    ) -> bool:
        try:
            expected = (child_state_file.resolve(strict=True).parent.parent / str(child.get("parent_run_id") or "") / "run.json").resolve(strict=True)
            if candidate_path.resolve(strict=True) != expected:
                return False
            reference = candidate.get("parent_stop_scope") if isinstance(candidate.get("parent_stop_scope"), dict) else {}
            scope_path = Path(str(reference.get("path") or "")).resolve(strict=True)
            if (
                scope_path.is_symlink()
                or hashlib.sha256(scope_path.read_bytes()).hexdigest() != str(reference.get("sha256") or "")
                or scope_path.stat().st_size != int(reference.get("bytes") or -1)
            ):
                return False
            scope = read_json(scope_path)
        except (OSError, RuntimeError, TypeError, ValueError, TabLifecycleError):
            return False
        entries = [entry for entry in scope.get("ordered_children") or [] if isinstance(entry, dict) and str(entry.get("run_id") or "") == str(child.get("run_id") or "")]
        return bool(
            str(candidate.get("record_kind") or "") == "parent"
            and str(candidate.get("phase") or "") == "USER_STOP_REQUESTED"
            and str(child.get("record_kind") or "") == "child"
            and str(child.get("phase") or "") == "SEND_REJECTED"
            and scope.get("schema") == "codex.chatgpt.parent-wide-user-stop/v1"
            and scope.get("explicit_user_request") is True
            and str(candidate.get("run_id") or "") == str(child.get("parent_run_id") or "")
            and str(candidate.get("workflow_id") or "") == str(child.get("parent_workflow_id") or "")
            and str(candidate.get("lease_nonce") or "") == str(child.get("parent_lease_nonce") or "")
            and str(candidate.get("project_root") or "") == str(child.get("project_root") or "")
            and str(candidate.get("project_key") or "") == str(child.get("project_key") or "")
            and len(entries) == 1
            and all(entries[0].get(key) == child.get(key) for key in ("run_id", "stage_id", "role", "lane", "iteration"))
        )

    def _foreign_owner(
        self,
        *,
        run_id: str | None,
        target_id: str,
        url: str,
        own_state_file: Path | None = None,
        own_record: Mapping[str, Any] | None = None,
        allow_exact_active_parent_coordinator: bool = True,
    ) -> dict[str, Any] | None:
        projects = self.state_root / "projects"
        if not projects.exists():
            return None
        wanted_url = normalized_url(url)
        own_path = own_state_file.absolute() if own_state_file is not None else None
        caller_record = own_record
        if caller_record is None and own_state_file is not None:
            try:
                caller_record = read_json(own_state_file)
            except Exception:
                caller_record = None
        for path in sorted(projects.glob("*/runs/*/run.json")):
            # This is the sole record provably irrelevant without reading
            # target/URL identity: the caller's exact state file.
            if own_path is not None and path.absolute() == own_path:
                continue
            if _is_reparse_point(path):
                raise TabLifecycleError(
                    "TAB_FOREIGN_STATE_UNREADABLE",
                    "foreign run state is not a regular bounded file; automatic cleanup is unsafe",
                    self._foreign_state_evidence(path, target_id=target_id, url=url, reason="reparse-point"),
                )
            try:
                candidate = read_json(path)
            except Exception as exc:
                raise TabLifecycleError(
                    "TAB_FOREIGN_STATE_UNREADABLE",
                    "foreign run state could not be read; automatic cleanup is unsafe",
                    self._foreign_state_evidence(path, target_id=target_id, url=url, reason=type(exc).__name__),
                ) from exc
            if not self._foreign_run_schema_valid(candidate):
                raise TabLifecycleError(
                    "TAB_OWNERSHIP_AMBIGUOUS",
                    "foreign run state lacks a valid ownership schema; automatic cleanup is unsafe",
                    self._foreign_state_evidence(path, target_id=target_id, url=url, reason="invalid-run-schema"),
                )
            # Only a fully terminal Web-Multi coordinator with no direct
            # browser identity can be ignored.  A parent declaration alone is
            # ambiguous ownership evidence and must fail closed before close.
            if str(candidate.get("record_kind") or "") == "parent":
                if self._foreign_parent_identityless_coordinator_valid(candidate):
                    continue
                if self._foreign_parent_terminal_coordinator_valid(candidate):
                    continue
                if (
                    allow_exact_active_parent_coordinator
                    and own_state_file is not None
                    and caller_record is not None
                    and self._foreign_parent_active_coordinator_owned_by_child(
                        candidate_path=path,
                        candidate=candidate,
                        child_state_file=own_state_file,
                        child=caller_record,
                    )
                ):
                    continue
                if (
                    own_state_file is not None
                    and caller_record is not None
                    and self._foreign_parent_stop_scope_owned_by_child(
                        candidate_path=path,
                        candidate=candidate,
                        child_state_file=own_state_file,
                        child=caller_record,
                    )
                ):
                    continue
                if (
                    own_state_file is not None
                    and caller_record is not None
                    and self._foreign_parent_user_stop_coordinator_owned_by_child(
                        candidate_path=path,
                        candidate=candidate,
                        child_state_file=own_state_file,
                        child=caller_record,
                    )
                ):
                    continue
                if (
                    own_state_file is not None
                    and caller_record is not None
                    and self._foreign_parent_user_stop_rejected_coordinator_owned_by_child(
                        candidate_path=path,
                        candidate=candidate,
                        child_state_file=own_state_file,
                        child=caller_record,
                    )
                ):
                    continue
                raise TabLifecycleError(
                    "TAB_OWNERSHIP_AMBIGUOUS",
                    "foreign parent state is not a proven terminal coordinator; automatic cleanup is unsafe",
                    self._foreign_state_evidence(path, target_id=target_id, url=url, reason="invalid-parent-coordinator"),
                )
            if "current_target_id" not in candidate or "conversation_url" not in candidate:
                raise TabLifecycleError(
                    "TAB_OWNERSHIP_AMBIGUOUS",
                    "foreign non-parent run state lacks target ownership fields",
                    self._foreign_state_evidence(path, target_id=target_id, url=url, reason="missing-target-identity"),
                )
            candidate_url = normalized_url(candidate.get("conversation_url"))
            candidate_phase = str(candidate.get("phase") or "")
            url_match = wanted_url and candidate_url == wanted_url
            target_match = bool(target_id and str(candidate.get("current_target_id") or "") == target_id)
            if (
                target_match
                and candidate_phase in {"COMPLETE", "PROVIDER_FAILED_TERMINAL", "CANCELLED_PRE_SUBMISSION"}
                and candidate_url
                and wanted_url
                and candidate_url != wanted_url
            ):
                # Browser target ids can be reused after a terminal tab is
                # navigated.  A durable, different canonical URL proves that
                # this terminal record no longer owns the live target.
                target_match = False
            if target_match or url_match:
                return {
                    "state_file": str(path),
                    "run_id": candidate.get("run_id"),
                    "project_key": candidate.get("project_key"),
                    "phase": candidate_phase,
                    "target_match": bool(target_match),
                    "url_match": bool(url_match),
                }
        return None

    def _owned_targets(self, state_file: Path, record: dict[str, Any]) -> set[str]:
        targets = {str(record.get("current_target_id") or "")}
        for event in record.get("target_rebind_events") or []:
            if isinstance(event, dict):
                targets.add(str(event.get("old_target_id") or ""))
                targets.add(str(event.get("new_target_id") or ""))
        lifecycle_file = self._lifecycle_file(state_file)
        if lifecycle_file.is_file():
            lifecycle = read_json(lifecycle_file)
            for event in lifecycle.get("events") or []:
                if isinstance(event, dict) and event.get("kind") == "owned":
                    targets.add(str(event.get("target_id") or ""))
        targets.discard("")
        return targets

    def _close_and_verify(self, *, target_id: str, before: list[dict[str, Any]]) -> dict[str, Any]:
        command = [self.executable, "tab-close", target_id, "--json"]
        completed = self.runner(command, self.env, self.timeout)
        if completed.returncode != 0:
            raise TabLifecycleError(
                "TAB_CLOSE_FAILED",
                "agbrowse tab-close failed",
                {"target_id": target_id, "exit_code": completed.returncode, "stderr": (completed.stderr or "")[-1000:]},
            )
        after = self.list_tabs()
        if any(_tab_id(tab) == target_id for tab in after):
            raise TabLifecycleError("TAB_CLOSE_NOT_CONFIRMED", "target remained live after tab-close", {"target_id": target_id})
        return {
            "ok": True,
            "state": "closed-and-absent",
            "target_id": target_id,
            "before_count": len(before),
            "after_count": len(after),
            "before_sha256": _tabs_hash(before),
            "after_sha256": _tabs_hash(after),
            "close_stdout_sha256": hashlib.sha256((completed.stdout or "").encode("utf-8")).hexdigest(),
        }

    def close_pre_submit(self, run_dir: str, *, target_id: str, reason: str) -> dict[str, Any]:
        state_file, record = self._run(run_dir)
        if (
            str(record.get("phase") or "") not in PRE_SUBMIT_PHASES
            or record.get("session_id")
            or record.get("conversation_url")
            or record.get("submission_receipt")
        ):
            raise TabLifecycleError(
                "TAB_PRE_SUBMIT_CLOSE_FORBIDDEN",
                "submitted or uncertain runs cannot use automatic tab cleanup",
                {"phase": record.get("phase"), "target_id": target_id},
            )
        if target_id not in self._owned_targets(state_file, record):
            raise TabLifecycleError(
                "TAB_OWNERSHIP_UNPROVEN",
                "pre-submit cleanup target is not owned by this run",
                {"target_id": target_id, "run_id": record.get("run_id")},
            )
        tabs = self.list_tabs()
        matches = [tab for tab in tabs if _tab_id(tab) == target_id]
        if not matches:
            evidence = self._append_event(
                state_file,
                record,
                {"kind": "cleanup-already-absent", "target_id": target_id, "reason": reason, "tabs_sha256": _tabs_hash(tabs)},
            )
            return {"ok": True, "state": "already-absent", "target_id": target_id, "evidence": evidence}
        if len(matches) != 1:
            raise TabLifecycleError("TAB_TARGET_AMBIGUOUS", "target id matched multiple live tabs", {"target_id": target_id})
        url = _tab_url(matches[0])
        if not is_pre_submit_composer_url(url):
            raise TabLifecycleError(
                "TAB_PRE_SUBMIT_URL_FORBIDDEN",
                "automatic cleanup is limited to unsubmitted ChatGPT composer tabs",
                {"target_id": target_id, "url": normalized_url(url)},
            )
        foreign = self._foreign_owner(
            run_id=str(record.get("run_id") or ""), target_id=target_id, url=url, own_state_file=state_file
        )
        if foreign:
            raise TabLifecycleError("TAB_FOREIGN_OWNER", "target belongs to another run", foreign)
        result = self._close_and_verify(target_id=target_id, before=tabs)
        evidence = self._append_event(
            state_file,
            record,
            {"kind": "cleanup", "reason": reason, "url": normalized_url(url), **result},
        )
        return {**result, "evidence": evidence}

    @staticmethod
    def _snapshot_bytes(path: Path) -> dict[str, Any]:
        data = path.read_bytes()
        return {
            "path": str(path),
            "resolved_path": str(path.resolve(strict=True)),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "bytes_base64": base64.b64encode(data).decode("ascii"),
        }

    def _tabs_with_raw_evidence(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        command = [self.executable, "tabs", "--json"]
        completed = self.runner(command, self.env, self.timeout)
        stdout = (completed.stdout or "").encode("utf-8")
        stderr = (completed.stderr or "").encode("utf-8")
        if completed.returncode != 0:
            raise TabLifecycleError("TAB_LIST_FAILED", "agbrowse tabs failed during uncertain stop cleanup")
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise TabLifecycleError("TAB_LIST_JSON_INVALID", "agbrowse tabs returned invalid JSON") from exc
        tabs = payload.get("tabs") if isinstance(payload, dict) else payload
        if not isinstance(tabs, list) or not all(isinstance(item, dict) for item in tabs):
            raise TabLifecycleError("TAB_LIST_JSON_INVALID", "agbrowse tabs JSON must be a complete list")
        executable_path = shutil.which(self.executable) or self.executable
        executable = Path(executable_path)
        executable_sha = hashlib.sha256(executable.read_bytes()).hexdigest() if executable.is_file() else None
        normalized = json.loads(json.dumps(tabs, ensure_ascii=False, sort_keys=True))
        evidence = {
            "argv": command,
            "executable_path": str(executable_path),
            "executable_sha256": executable_sha,
            "exit_code": completed.returncode,
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stdout_bytes": len(stdout),
            "stdout_base64": base64.b64encode(stdout).decode("ascii"),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "stderr_bytes": len(stderr),
            "stderr_base64": base64.b64encode(stderr).decode("ascii"),
            "normalized_tabs": normalized,
            "tabs_sha256": _tabs_hash(normalized),
        }
        return tabs, evidence

    def inspect_parent_stopped_submission_uncertain(
        self, run_dir: str
    ) -> dict[str, Any]:
        """Read-only exact-target adjudication; never manufactures ownership."""
        state_file, record = self._run(run_dir)
        if (
            str(record.get("phase") or "") != "SEND_REJECTED"
            or record.get("session_id") is not None
            or record.get("conversation_url") is not None
            or record.get("submission_receipt") is not None
            or record.get("result") is not None
        ):
            raise TabLifecycleError("PARENT_STOP_UNCERTAIN_PHASE_INVALID", "historical rejected identity changed")
        target_id = str(record.get("current_target_id") or "")
        claim_time = str((record.get("send_claim") or {}).get("claimed_at") or "")
        lifecycle_path = self._lifecycle_file(state_file)
        lifecycle_owned = self._owned_regular_file(state_file, lifecycle_path)
        if lifecycle_owned is None:
            raise TabLifecycleError("TAB_PREEXISTING_OWNERSHIP_MISSING", "pre-existing lifecycle ownership is unavailable")
        lifecycle = read_json(lifecycle_owned)
        if (
            lifecycle.get("schema") != "codex.chatgpt.agbrowse-tab-lifecycle/v1"
            or str(lifecycle.get("run_id") or "") != str(record.get("run_id") or "")
            or str(lifecycle.get("project_key") or "") != str(record.get("project_key") or "")
            or str(lifecycle.get("manifest_sha256") or "") != str(record.get("manifest_sha256") or "")
        ):
            raise TabLifecycleError("TAB_PREEXISTING_OWNERSHIP_INVALID", "lifecycle owner identity differs")
        ownership = [
            (index, event)
            for index, event in enumerate(lifecycle.get("events") or [])
            if isinstance(event, dict)
            and event.get("kind") == "owned"
            and str(event.get("target_id") or "") == target_id
            and str(event.get("at") or "")
            and str(event.get("at") or "") <= claim_time
        ]
        if not ownership:
            raise TabLifecycleError("TAB_PREEXISTING_OWNERSHIP_MISSING", "ownership must predate the send claim")
        if any(
            str(event.get("new_target_id") or "") and str(event.get("old_target_id") or "") == target_id
            for event in record.get("target_rebind_events") or []
            if isinstance(event, dict)
        ):
            raise TabLifecycleError("TAB_PREEXISTING_OWNERSHIP_REBOUND", "target ownership was transferred")
        owner_index, owner_event = ownership[0]
        composer_path = state_file.parent / "composer-app-evidence.json"
        composer = self._owned_regular_file(state_file, composer_path)
        if composer is None:
            raise TabLifecycleError("TAB_COMPOSER_EVIDENCE_MISSING", "immutable composer evidence is unavailable")
        composer_value = read_json(composer)
        if str(composer_value.get("target_id") or "") != target_id:
            raise TabLifecycleError("TAB_COMPOSER_EVIDENCE_MISMATCH", "composer target differs")
        tabs, tabs_evidence = self._tabs_with_raw_evidence()
        matches = [tab for tab in tabs if _tab_id(tab) == target_id]
        if len(matches) > 1:
            raise TabLifecycleError("TAB_TARGET_AMBIGUOUS", "exact target id matched multiple live tabs")
        observed = matches[0] if matches else None
        observed_url = _tab_url(observed) if observed else None
        if observed and not (
            normalized_url(observed_url) == "https://chatgpt.com/"
            or CANONICAL_CHAT_RE.fullmatch(normalized_url(observed_url))
        ):
            raise TabLifecycleError("TAB_TARGET_REUSE_SUSPECTED", "owned target now has a non-ChatGPT URL")
        foreign = self._foreign_owner(
            run_id=str(record.get("run_id") or ""), target_id=target_id,
            url=str(observed_url or owner_event.get("url") or "https://chatgpt.com/"),
            own_state_file=state_file, own_record=record,
        )
        if foreign:
            raise TabLifecycleError("TAB_FOREIGN_OWNER", "target or observed URL belongs to another run", foreign)
        historical_epoch = str(owner_event.get("browser_process_epoch") or "")
        observed_epoch = str((observed or {}).get("browserProcessEpoch") or (observed or {}).get("browser_process_epoch") or "")
        if observed and (not historical_epoch or not observed_epoch):
            raise TabLifecycleError("TAB_BROWSER_CONTINUITY_UNPROVEN", "live target lacks exact browser-process continuity")
        if observed and historical_epoch != observed_epoch:
            raise TabLifecycleError("TAB_BROWSER_EPOCH_MISMATCH", "same target id belongs to another browser epoch")
        expected_profile = str((record.get("requested") or {}).get("browser_profile") or "9222")
        observed_profile = str((observed or {}).get("browserProfileKey") or (observed or {}).get("browser_profile") or "")
        if observed_profile and observed_profile != expected_profile:
            raise TabLifecycleError("TAB_BROWSER_PROFILE_MISMATCH", "live target belongs to another browser profile")
        return {
            "target_id": target_id,
            "observed_target_url": observed_url,
            "target_match_count": len(matches),
            "ownership": {
                "lifecycle": self._snapshot_bytes(lifecycle_owned),
                "event_index": owner_index,
                "event": owner_event,
                "composer_evidence": self._snapshot_bytes(composer),
                "target_rebind_events_sha256": hashlib.sha256(json.dumps(record.get("target_rebind_events") or [], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
                "browser_profile": expected_profile,
                "cdp_endpoint": str((record.get("requested") or {}).get("cdp_endpoint") or "http://127.0.0.1:9222"),
                "browser_process_epoch": historical_epoch or None,
                "contract_sha256": hashlib.sha256(json.dumps(record.get("agbrowse") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            },
            "tabs_before": tabs_evidence,
            "foreign_owner_scan": {"result": None, "scan_sha256": hashlib.sha256((target_id + str(observed_url or "")).encode("utf-8")).hexdigest()},
        }

    def close_parent_stopped_submission_uncertain(
        self, run_dir: str, *, preclose: dict[str, Any]
    ) -> dict[str, Any]:
        """Close one exact historically owned target; issue no other mutation."""
        state_file, record = self._run(run_dir)
        if record.get("pending_parent_stop_submission_uncertain") != preclose:
            raise TabLifecycleError("TAB_UNCERTAIN_PRECLOSE_MISSING", "child does not reference the immutable pre-close decision")
        preclose_path = Path(str(preclose.get("path") or ""))
        if (
            not preclose_path.is_file() or preclose_path.is_symlink()
            or hashlib.sha256(preclose_path.read_bytes()).hexdigest() != str(preclose.get("sha256") or "")
            or preclose_path.stat().st_size != int(preclose.get("bytes") or -1)
        ):
            raise TabLifecycleError("TAB_UNCERTAIN_PRECLOSE_INVALID", "pre-close descriptor changed")
        preclose_payload = read_json(preclose_path)
        candidate = preclose_payload.get("candidate") if isinstance(preclose_payload.get("candidate"), dict) else {}
        claim = candidate.get("claim") if isinstance(candidate.get("claim"), dict) else {}
        stderr = candidate.get("stderr_discrepancy", {}).get("actual") if isinstance(candidate.get("stderr_discrepancy"), dict) else {}
        stdout = candidate.get("stdout_discrepancy", {}).get("actual") if isinstance(candidate.get("stdout_discrepancy"), dict) else {}
        ownership = preclose_payload.get("ownership") if isinstance(preclose_payload.get("ownership"), dict) else {}
        proof_path = state_file.parent / "user-stop" / "parent-stop-submission-uncertain-cleanup-proof.json"
        source_descriptors = [claim, stderr, stdout, ownership.get("composer_evidence") or {}]
        if not proof_path.exists():
            source_descriptors.append(ownership.get("lifecycle") or {})
        for descriptor in source_descriptors:
            path = Path(str(descriptor.get("path") or ""))
            if (
                not path.is_file() or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != str(descriptor.get("sha256") or "")
                or path.stat().st_size
                != int(
                    descriptor.get("bytes")
                    if descriptor.get("bytes") is not None
                    else -1
                )
            ):
                raise TabLifecycleError("TAB_UNCERTAIN_SOURCE_CHANGED", "claim or evidence changed after pre-close snapshot")
        if any(record.get(key) is not None for key in ("session_id", "conversation_url", "submission_receipt", "result")):
            raise TabLifecycleError("TAB_UNCERTAIN_IDENTITY_CHANGED", "provider identity or result appeared after pre-close snapshot")
        before = self.inspect_parent_stopped_submission_uncertain(run_dir)
        target_id = before["target_id"]
        if proof_path.exists():
            if not proof_path.is_file() or proof_path.is_symlink():
                raise TabLifecycleError("TAB_IMMUTABLE_EVIDENCE_CONFLICT", "cleanup proof path is invalid")
            proof_payload = read_json(proof_path)
            cleanup = proof_payload.get("cleanup") if isinstance(proof_payload.get("cleanup"), dict) else {}
            if (
                proof_payload.get("schema") != "codex.chatgpt.parent-stop-submission-uncertain-cleanup-proof/v1"
                or proof_payload.get("preclose") != preclose
                or cleanup.get("target_id") != target_id
                or cleanup.get("target_absent_after") is not True
                or before["target_match_count"] != 0
            ):
                raise TabLifecycleError("TAB_IMMUTABLE_EVIDENCE_CONFLICT", "persisted cleanup proof is not reusable")
            descriptor = {"path": str(proof_path), "sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest(), "bytes": proof_path.stat().st_size}
            return {**cleanup, "durable_cleanup_proof": descriptor}
        expected_target = preclose_payload.get("observed_target") if isinstance(preclose_payload.get("observed_target"), dict) else {}
        if (
            int(expected_target.get("match_count") or 0) != int(before["target_match_count"])
            or str(expected_target.get("target_id") or "") != target_id
            or (expected_target.get("url") or None) != (before.get("observed_target_url") or None)
            or preclose_payload.get("tabs_before", {}).get("normalized_tabs")
            != before["tabs_before"]["normalized_tabs"]
        ):
            raise TabLifecycleError("TAB_UNCERTAIN_PRECLOSE_DRIFT", "tab inventory changed before exact close")
        tabs_before = before["tabs_before"]["normalized_tabs"]
        observed_url = before.get("observed_target_url")
        close_evidence = None
        if before["target_match_count"] == 1:
            command = [self.executable, "tab-close", target_id, "--json"]
            completed = self.runner(command, self.env, self.timeout)
            stdout = (completed.stdout or "").encode("utf-8")
            stderr = (completed.stderr or "").encode("utf-8")
            close_evidence = {
                "argv": command, "exit_code": completed.returncode,
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(), "stdout_bytes": len(stdout),
                "stdout_base64": base64.b64encode(stdout).decode("ascii"),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest(), "stderr_bytes": len(stderr),
                "stderr_base64": base64.b64encode(stderr).decode("ascii"),
            }
            if completed.returncode != 0:
                raise TabLifecycleError("TAB_CLOSE_FAILED", "exact target close failed")
        tabs_after, after_evidence = self._tabs_with_raw_evidence()
        if any(_tab_id(tab) == target_id for tab in tabs_after):
            raise TabLifecycleError("TAB_CLOSE_NOT_CONFIRMED", "exact target remained present")
        before_foreign = {_tab_id(tab): (_tab_url(tab), tab.get("type")) for tab in tabs_before if _tab_id(tab) != target_id}
        after_by_id = {_tab_id(tab): (_tab_url(tab), tab.get("type")) for tab in tabs_after}
        if any(after_by_id.get(key) != value for key, value in before_foreign.items()):
            raise TabLifecycleError("TAB_FOREIGN_TARGET_CHANGED", "a pre-existing foreign target disappeared or changed")
        state = "closed-and-absent" if close_evidence is not None else "already-absent"
        event = {
            "kind": "parent-stop-submission-uncertain-cleanup", "target_id": target_id,
            "observed_target_url": observed_url, "state": state, "target_absent_after": True,
            "preclose_sha256": preclose.get("sha256"),
        }
        lifecycle_path = self._lifecycle_file(state_file)
        lifecycle_value = read_json(lifecycle_path)
        existing_cleanup = [
            item
            for item in lifecycle_value.get("events") or []
            if isinstance(item, dict)
            and item.get("kind") == event["kind"]
            and str(item.get("target_id") or "") == target_id
            and str(item.get("preclose_sha256") or "") == str(preclose.get("sha256") or "")
        ]
        if existing_cleanup:
            persisted_event = {key: value for key, value in existing_cleanup[-1].items() if key != "at"}
            lifecycle_evidence = {
                "path": str(lifecycle_path),
                "sha256": hashlib.sha256(lifecycle_path.read_bytes()).hexdigest(),
                "event": persisted_event,
            }
        else:
            lifecycle_evidence = self._append_event(state_file, record, event)
        cleanup = {
            "state": state, "target_id": target_id, "observed_target_url": observed_url,
            "target_absent_after": True, "target_match_count_before": before["target_match_count"],
            "tabs_before": before["tabs_before"], "close": close_evidence,
            "tabs_after": after_evidence, "preexisting_foreign_targets": before_foreign,
            "lifecycle": lifecycle_evidence,
        }
        descriptor = write_immutable_json_exclusive(
            proof_path,
            {
                "schema": "codex.chatgpt.parent-stop-submission-uncertain-cleanup-proof/v1",
                "preclose": preclose,
                "cleanup": cleanup,
            },
        )
        return {**cleanup, "durable_cleanup_proof": descriptor}

    def close_completed(self, run_dir: str, *, explicit_user_request: bool = False) -> dict[str, Any]:
        # Kept as a compatibility argument for older callers.  Durable
        # COMPLETE plus exact run ownership is now the cleanup authority.
        del explicit_user_request
        state_file, record = self._run(run_dir)
        url = normalized_url(record.get("conversation_url"))
        expected_target_id = str(record.get("current_target_id") or "")
        phase = str(record.get("phase") or "")
        if (
            phase not in {"COMPLETE", "PROVIDER_FAILED_TERMINAL"}
            or not CANONICAL_CHAT_RE.fullmatch(url)
            or not expected_target_id
        ):
            raise TabLifecycleError(
                "TAB_COMPLETED_IDENTITY_INVALID",
                "automatic completed-tab cleanup requires a safe terminal phase plus exact target and canonical URL identities",
                {"phase": phase, "target_id": expected_target_id, "url": url},
            )
        if phase == "COMPLETE" and not self._complete_result_capture_valid(state_file, record):
            raise TabLifecycleError(
                "TAB_COMPLETE_RESULT_EVIDENCE_INVALID",
                "COMPLETE cleanup requires a nonempty immutable result capture owned by this run",
                {"phase": phase, "url": url},
            )
        if phase == "PROVIDER_FAILED_TERMINAL" and not self._provider_failed_terminal_proof_valid(state_file, record):
            raise TabLifecycleError(
                "TAB_PROVIDER_FAILED_EVIDENCE_INVALID",
                "provider-failed cleanup requires its distinct immutable terminal-failure proof",
                {"phase": phase, "url": url},
            )
        tabs = self.list_tabs()
        url_matches = [tab for tab in tabs if normalized_url(_tab_url(tab)) == url]
        matches = [tab for tab in url_matches if _tab_id(tab) == expected_target_id]
        if url_matches and not matches:
            raise TabLifecycleError(
                "TAB_COMPLETED_TARGET_MISMATCH",
                "canonical URL is live only on a different target",
                {
                    "url": url,
                    "expected_target_id": expected_target_id,
                    "actual_target_ids": [_tab_id(tab) for tab in url_matches],
                },
            )
        if not matches:
            evidence = self._append_event(
                state_file,
                record,
                {"kind": "owned-complete-cleanup-already-absent", "url": url, "tabs_sha256": _tabs_hash(tabs)},
            )
            return {"ok": True, "state": "already-absent", "conversation_url": url, "evidence": evidence}
        if len(matches) != 1 or len(url_matches) != 1:
            raise TabLifecycleError(
                "TAB_COMPLETED_URL_AMBIGUOUS",
                "canonical URL and exact target did not identify one unique live tab",
                {"url": url, "target_id": expected_target_id, "url_match_count": len(url_matches)},
            )
        target_id = _tab_id(matches[0])
        foreign = self._foreign_owner(
            run_id=str(record.get("run_id") or ""), target_id=target_id, url=url, own_state_file=state_file
        )
        if foreign:
            raise TabLifecycleError("TAB_FOREIGN_OWNER", "completed target belongs to another run", foreign)
        result = self._close_and_verify(target_id=target_id, before=tabs)
        remaining = self.list_tabs()
        if any(normalized_url(_tab_url(tab)) == url for tab in remaining):
            raise TabLifecycleError("TAB_CLOSE_NOT_CONFIRMED", "canonical conversation URL remained after close", {"url": url})
        evidence = self._append_event(
            state_file,
            record,
            {
                "kind": "owned-complete-auto-cleanup" if phase == "COMPLETE" else "owned-provider-failed-auto-cleanup",
                "terminal_phase": phase,
                "conversation_url": url,
                **result,
            },
        )
        return {**result, "conversation_url": url, "evidence": evidence}

    def close_user_stopped(self, run_dir: str) -> dict[str, Any]:
        """Close only an exact target+URL after state-layer stop adjudication."""
        state_file, record = self._run(run_dir)
        if str(record.get("phase") or "") != "USER_STOP_REQUESTED":
            raise TabLifecycleError("TAB_USER_STOP_PHASE_INVALID", "exact user-stop cleanup requires USER_STOP_REQUESTED")
        url = normalized_url(record.get("conversation_url"))
        target_id = str(record.get("current_target_id") or "")
        if not target_id or not CANONICAL_CHAT_RE.fullmatch(url):
            raise TabLifecycleError("TAB_USER_STOP_IDENTITY_INVALID", "user-stop cleanup requires exact target and canonical URL")
        stop = record.get("user_stop") if isinstance(record.get("user_stop"), dict) else {}
        auth = stop.get("authorization") if isinstance(stop.get("authorization"), dict) else {}
        parent_file = state_file.parent.parent / str(record.get("parent_run_id") or "") / "run.json"
        lock_file = state_file.parent.parent.parent / "active.lock"
        auth_path = Path(str(auth.get("path") or ""))
        try:
            auth_resolved = auth_path.resolve(strict=True)
            auth_resolved.relative_to(parent_file.parent / "user-stop")
        except (OSError, RuntimeError, ValueError):
            raise TabLifecycleError("TAB_USER_STOP_AUTHORIZATION_INVALID", "immutable stop authorization path is not exact")
        auth_sha256 = hashlib.sha256(auth_resolved.read_bytes()).hexdigest()
        if (
            not auth_resolved.is_file()
            or auth_resolved.is_symlink()
            or not parent_file.is_file()
            or not lock_file.is_file()
            or auth_sha256 != str(auth.get("sha256") or "")
            or auth_sha256 != str(stop.get("authorization_sha256") or "")
            or auth_resolved.stat().st_size != int(auth.get("bytes") or -1)
        ):
            raise TabLifecycleError("TAB_USER_STOP_AUTHORIZATION_INVALID", "missing immutable stop binding")
        auth_value = read_json(auth_resolved)
        auth_url = normalized_url(
            auth_value.get("canonical_conversation_url") or auth_value.get("conversation_url")
        )
        if (
            str(auth_value.get("schema") or "")
            not in {
                "codex.chatgpt.user-stop-authorization/v2",
                "codex.chatgpt.user-stop-legacy-binding-adjudication/v1",
            }
            or str(auth_value.get("child_run_id") or "") != str(record.get("run_id") or "")
            or str(auth_value.get("parent_run_id") or "") != str(record.get("parent_run_id") or "")
            or str(auth_value.get("session_id") or "") != str(record.get("session_id") or "")
            or str(auth_value.get("target_id") or "") != target_id
            or auth_url != url
            or str(auth_value.get("stop_epoch_nonce") or "") != str(stop.get("stop_epoch_nonce") or "")
        ):
            raise TabLifecycleError("TAB_USER_STOP_AUTHORIZATION_INVALID", "immutable stop authorization identity differs")
        adjudication = stop.get("pending_adjudication") if isinstance(stop.get("pending_adjudication"), dict) else {}
        adjudication_path = Path(str(adjudication.get("path") or ""))
        try:
            adjudication_resolved = adjudication_path.resolve(strict=True)
            adjudication_resolved.relative_to(state_file.parent / "user-stop")
        except (OSError, RuntimeError, ValueError):
            raise TabLifecycleError("TAB_USER_STOP_ADJUDICATION_REQUIRED", "terminal adjudication path is not exact")
        adjudication_sha256 = hashlib.sha256(adjudication_resolved.read_bytes()).hexdigest()
        if (
            not adjudication_resolved.is_file()
            or adjudication_resolved.is_symlink()
            or adjudication_sha256 != str(adjudication.get("sha256") or "")
            or adjudication_resolved.stat().st_size != int(adjudication.get("bytes") or -1)
        ):
            raise TabLifecycleError("TAB_USER_STOP_ADJUDICATION_REQUIRED", "terminal adjudication must precede stopped-tab close")
        adjudication_value = read_json(adjudication_resolved)
        if (
            adjudication_value.get("schema") != "codex.chatgpt.user-stop-adjudication/v2"
            or adjudication_value.get("terminal") is not True
            or adjudication_value.get("cleanup_required") is not True
            or str(adjudication_value.get("run_id") or "") != str(record.get("run_id") or "")
            or str(adjudication_value.get("parent_run_id") or "") != str(record.get("parent_run_id") or "")
            or str(adjudication_value.get("session_id") or "") != str(record.get("session_id") or "")
            or str(adjudication_value.get("target_id") or "") != target_id
            or normalized_url(adjudication_value.get("conversation_url")) != url
            or str(adjudication_value.get("authorization_sha256") or "") != auth_sha256
            or str(adjudication_value.get("stop_epoch_nonce") or "") != str(stop.get("stop_epoch_nonce") or "")
        ):
            raise TabLifecycleError("TAB_USER_STOP_ADJUDICATION_REQUIRED", "pre-close adjudication does not bind exact stopped target")
        parent = read_json(parent_file); lock = read_json(lock_file)
        parent_request = (
            (parent.get("user_stop_requests") or {}).get(str(record.get("run_id") or ""))
            if isinstance(parent.get("user_stop_requests"), dict)
            else None
        )
        lock_request = (
            (lock.get("user_stop_requests") or {}).get(str(record.get("run_id") or ""))
            if isinstance(lock.get("user_stop_requests"), dict)
            else None
        )
        if (
            str(parent.get("phase") or "") != "USER_STOP_REQUESTED"
            or str(lock.get("phase") or "") != "USER_STOP_REQUESTED"
            or str(lock.get("stop_epoch_nonce") or "") != str(stop.get("stop_epoch_nonce") or "")
            or not isinstance(parent_request, dict)
            or not isinstance(lock_request, dict)
            or str(parent_request.get("stop_epoch_nonce") or "") != str(stop.get("stop_epoch_nonce") or "")
            or str(lock_request.get("stop_epoch_nonce") or "") != str(stop.get("stop_epoch_nonce") or "")
            or str((parent_request.get("authorization") or {}).get("sha256") or "") != auth_sha256
            or str((lock_request.get("authorization") or {}).get("sha256") or "") != auth_sha256
        ):
            raise TabLifecycleError("TAB_USER_STOP_AUTHORIZATION_INVALID", "parent/lock stop epoch is not exact")
        foreign = self._foreign_owner(
            run_id=str(record.get("run_id") or ""),
            target_id=target_id,
            url=url,
            own_state_file=state_file,
            own_record=record,
        )
        if foreign:
            raise TabLifecycleError("TAB_FOREIGN_OWNER", "stopped target belongs to another run", foreign)
        tabs = self.list_tabs()
        url_matches = [tab for tab in tabs if normalized_url(_tab_url(tab)) == url]
        matches = [tab for tab in url_matches if _tab_id(tab) == target_id]
        if url_matches and not matches:
            raise TabLifecycleError("TAB_USER_STOP_TARGET_MISMATCH", "canonical URL is live on another target")
        if not matches:
            evidence = self._append_event(state_file, record, {"kind": "user-stop-cleanup-already-absent", "target_id": target_id, "conversation_url": url, "tabs_sha256": _tabs_hash(tabs)})
            return {"ok": True, "state": "already-absent", "target_id": target_id, "conversation_url": url, "evidence": evidence}
        if len(matches) != 1 or len(url_matches) != 1:
            raise TabLifecycleError("TAB_USER_STOP_URL_AMBIGUOUS", "exact stopped target and URL are not unique")
        result = self._close_and_verify(target_id=target_id, before=tabs)
        remaining = self.list_tabs()
        if any(normalized_url(_tab_url(tab)) == url for tab in remaining):
            raise TabLifecycleError("TAB_CLOSE_NOT_CONFIRMED", "stopped canonical URL remained after close", {"url": url})
        evidence = self._append_event(state_file, record, {"kind": "user-stop-exact-cleanup", "target_id": target_id, "conversation_url": url, **result})
        return {**result, "conversation_url": url, "evidence": evidence}

    def close_terminal_recovery_utilities(self, run_dir: str, *, explicit_user_request: bool = False) -> dict[str, Any]:
        # Compatibility argument only.  Durable terminal result evidence and
        # exact utility-target ownership authorize this automatic cleanup.
        del explicit_user_request
        state_file, record = self._run(run_dir)
        phase = str(record.get("phase") or "")
        url = normalized_url(record.get("conversation_url"))
        if (
            phase != "COMPLETE"
            or not CANONICAL_CHAT_RE.fullmatch(url)
            or not self._complete_result_capture_valid(state_file, record)
        ):
            raise TabLifecycleError(
                "TAB_RECOVERY_UTILITY_TERMINAL_EVIDENCE_INVALID",
                "recovery utility cleanup requires durable terminal result evidence",
                {"phase": phase, "url": url},
            )
        lifecycle_file = self._lifecycle_file(state_file)
        if not lifecycle_file.is_file() or lifecycle_file.is_symlink():
            raise TabLifecycleError("TAB_LIFECYCLE_EVIDENCE_MISSING", "recovery utility ownership ledger is missing")
        lifecycle = read_json(lifecycle_file)
        utility_targets = {
            str(event.get("target_id") or "")
            for event in lifecycle.get("events") or []
            if isinstance(event, dict)
            and event.get("kind") == "owned"
            and str(event.get("stage") or "") == "history-adjudication-utility"
        }
        utility_targets.discard("")
        if not utility_targets:
            raise TabLifecycleError("TAB_RECOVERY_UTILITY_OWNERSHIP_MISSING", "no exact recovery utility target is recorded")
        closed: list[dict[str, Any]] = []
        tabs = self.list_tabs()
        for target_id in sorted(utility_targets):
            matches = [tab for tab in tabs if _tab_id(tab) == target_id]
            if not matches:
                continue
            if len(matches) != 1 or normalized_url(_tab_url(matches[0])) != url:
                raise TabLifecycleError(
                    "TAB_RECOVERY_UTILITY_TARGET_MISMATCH",
                    "recorded recovery utility target does not match the terminal canonical URL",
                    {"target_id": target_id, "url": url, "match_count": len(matches)},
                )
            foreign = self._foreign_owner(
                run_id=str(record.get("run_id") or ""), target_id=target_id, url=url, own_state_file=state_file
            )
            if foreign:
                raise TabLifecycleError("TAB_FOREIGN_OWNER", "recovery utility target belongs to another run", foreign)
            close_result = self._close_and_verify(target_id=target_id, before=tabs)
            closed.append(close_result)
            tabs = self.list_tabs()
        remaining_utility = [tab for tab in tabs if _tab_id(tab) in utility_targets]
        if remaining_utility:
            raise TabLifecycleError(
                "TAB_RECOVERY_UTILITY_CLOSE_NOT_CONFIRMED",
                "recovery utility target remained live after exact close",
                {"target_ids": [_tab_id(tab) for tab in remaining_utility]},
            )
        evidence = self._append_event(
            state_file,
            record,
            {
                "kind": "terminal-recovery-utility-cleanup",
                "terminal_phase": phase,
                "conversation_url": url,
                "utility_target_ids": sorted(utility_targets),
                "closed_target_ids": [item["target_id"] for item in closed],
                "remaining_tabs_sha256": _tabs_hash(tabs),
            },
        )
        return {
            "ok": True,
            "state": "closed-and-absent" if closed else "already-absent",
            "closed_target_ids": [item["target_id"] for item in closed],
            "conversation_url": url,
            "evidence": evidence,
        }

    def terminal_rebind_candidate(self, run_dir: str) -> dict[str, Any]:
        state_file, record = self._run(run_dir)
        phase = str(record.get("phase") or "")
        url = normalized_url(record.get("conversation_url"))
        old_target_id = str(record.get("current_target_id") or "")
        if (
            phase not in {"COMPLETE", "PROVIDER_FAILED_TERMINAL"}
            or not CANONICAL_CHAT_RE.fullmatch(url)
            or not old_target_id
        ):
            raise TabLifecycleError(
                "TAB_TERMINAL_REBIND_IDENTITY_INVALID",
                "terminal target rebind requires a safe terminal run with exact URL and prior target",
                {"phase": phase, "url": url, "old_target_id": old_target_id},
            )
        tabs = self.list_tabs()
        if any(_tab_id(tab) == old_target_id for tab in tabs):
            raise TabLifecycleError(
                "TAB_TERMINAL_OLD_TARGET_STILL_LIVE",
                "recorded terminal target is still live and cannot be replaced",
                {"old_target_id": old_target_id, "url": url},
            )
        url_matches = [tab for tab in tabs if normalized_url(_tab_url(tab)) == url]
        if len(url_matches) != 1:
            raise TabLifecycleError(
                "TAB_TERMINAL_REBIND_AMBIGUOUS",
                "browser restart recovery requires exactly one live canonical URL match",
                {"url": url, "url_match_count": len(url_matches)},
            )
        new_target_id = _tab_id(url_matches[0])
        if not new_target_id or new_target_id == old_target_id:
            raise TabLifecycleError(
                "TAB_TERMINAL_REBIND_TARGET_INVALID",
                "terminal rebind candidate did not identify a changed target",
                {"old_target_id": old_target_id, "new_target_id": new_target_id},
            )
        foreign = self._foreign_owner(
            run_id=str(record.get("run_id") or ""),
            target_id=new_target_id,
            url=url,
            own_state_file=state_file,
        )
        if foreign:
            raise TabLifecycleError("TAB_FOREIGN_OWNER", "terminal rebind target belongs to another run", foreign)
        evidence = self._append_event(
            state_file,
            record,
            {
                "kind": "terminal-exact-url-rebind-candidate",
                "phase": phase,
                "conversation_url": url,
                "old_target_id": old_target_id,
                "new_target_id": new_target_id,
                "old_target_absent": True,
                "url_match_count": 1,
                "foreign_owner_absent": True,
                "tabs_sha256": _tabs_hash(tabs),
            },
        )
        return {
            "ok": True,
            "phase": phase,
            "conversation_url": url,
            "old_target_id": old_target_id,
            "new_target_id": new_target_id,
            "old_target_absent": True,
            "url_match_count": 1,
            "foreign_owner_absent": True,
            "tabs_sha256": _tabs_hash(tabs),
            "evidence": evidence,
        }

    def close_explicit_target(self, *, target_id: str, expected_url: str, explicit_user_request: bool) -> dict[str, Any]:
        if not explicit_user_request:
            raise TabLifecycleError("TAB_EXPLICIT_REQUEST_REQUIRED", "target cleanup requires explicit user request")
        expected = normalized_url(expected_url)
        if CANONICAL_CHAT_RE.fullmatch(expected):
            raise TabLifecycleError(
                "TAB_COMPLETED_RUN_REQUIRED",
                "conversation tabs must be closed through a COMPLETE run identity",
                {"url": expected},
            )
        tabs = self.list_tabs()
        matches = [tab for tab in tabs if _tab_id(tab) == target_id]
        if not matches:
            return {"ok": True, "state": "already-absent", "target_id": target_id}
        if len(matches) != 1 or normalized_url(_tab_url(matches[0])) != expected:
            raise TabLifecycleError(
                "TAB_EXPLICIT_TARGET_MISMATCH",
                "live target did not match the exact expected URL",
                {"target_id": target_id, "expected_url": expected, "match_count": len(matches)},
            )
        foreign = self._foreign_owner(run_id=None, target_id=target_id, url=expected)
        if foreign:
            raise TabLifecycleError("TAB_FOREIGN_OWNER", "explicit target belongs to a recorded run", foreign)
        result = self._close_and_verify(target_id=target_id, before=tabs)
        audit = self.state_root / "explicit-tab-cleanups" / f"{uuid.uuid4().hex}.json"
        write_json_atomic(audit, {"schema": "codex.chatgpt.explicit-tab-cleanup/v1", "at": utc_now(), "url": expected, **result})
        return {**result, "url": expected, "evidence": str(audit)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Exact tab ownership and cleanup over public agbrowse commands.")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--executable", default="agbrowse")
    sub = parser.add_subparsers(dest="command", required=True)
    completed = sub.add_parser("close-completed")
    completed.add_argument("--run", required=True)
    # Accepted invisibly for older callers; completed cleanup is automatic.
    completed.add_argument("--explicit-user-request", action="store_true", help=argparse.SUPPRESS)
    target = sub.add_parser("close-target")
    target.add_argument("--target-id", required=True)
    target.add_argument("--expected-url", required=True)
    target.add_argument("--explicit-user-request", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    lifecycle = TabLifecycle(state_root=args.state_root, executable=args.executable)
    try:
        if args.command == "close-completed":
            result = lifecycle.close_completed(args.run, explicit_user_request=args.explicit_user_request)
        else:
            result = lifecycle.close_explicit_target(
                target_id=args.target_id,
                expected_url=args.expected_url,
                explicit_user_request=args.explicit_user_request,
            )
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except TabLifecycleError as exc:
        print(json.dumps(exc.envelope(), ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
