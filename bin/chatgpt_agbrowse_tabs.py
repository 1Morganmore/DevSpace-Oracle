from __future__ import annotations

import argparse
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
from typing import Any, Callable, Iterable
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

    def _foreign_owner(
        self,
        *,
        run_id: str | None,
        target_id: str,
        url: str,
        own_state_file: Path | None = None,
    ) -> dict[str, Any] | None:
        projects = self.state_root / "projects"
        if not projects.exists():
            return None
        wanted_url = normalized_url(url)
        own_path = own_state_file.absolute() if own_state_file is not None else None
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
                if self._foreign_parent_terminal_coordinator_valid(candidate):
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
