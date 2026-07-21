from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


PINNED_VERSION = "0.1.18"
WINDOWS_CMD_WRAPPER_SAFE_CHARS = 7000
GLOBAL_APP_CONTRACT_SCHEMA = "codex.chatgpt.global-app-contract-state/v1"
GLOBAL_APP_CONTRACT_MAX_ENTRIES = 32
GLOBAL_APP_CONTRACT_MAX_EVENTS = 20
BIN_DIR = Path(__file__).resolve().parent
STATE_PATH = BIN_DIR / "chatgpt_agbrowse_state.py"
APP_PATH = BIN_DIR / "codexpro_agbrowse_app.py"
COMPOSER_PATH = BIN_DIR / "chatgpt_agbrowse_composer.py"
APP_IDENTITY_PATH = BIN_DIR / "codexpro_mcp_identity.py"
CONTRACT_VALIDATOR_PATH = BIN_DIR / "chatgpt_agbrowse_contract.py"
TABS_PATH = BIN_DIR / "chatgpt_agbrowse_tabs.py"


def _load_state_module():
    spec = importlib.util.spec_from_file_location("chatgpt_agbrowse_state_bridge", STATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"state module unavailable: {STATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STATE = _load_state_module()


def _load_app_module():
    spec = importlib.util.spec_from_file_location("codexpro_agbrowse_app_bridge", APP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"app connector unavailable: {APP_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_composer_module():
    spec = importlib.util.spec_from_file_location("chatgpt_agbrowse_composer_bridge", COMPOSER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"research composer unavailable: {COMPOSER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_contract_validator_module():
    spec = importlib.util.spec_from_file_location("chatgpt_agbrowse_contract_bridge", CONTRACT_VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"contract validator unavailable: {CONTRACT_VALIDATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_tabs_module():
    spec = importlib.util.spec_from_file_location("chatgpt_agbrowse_tabs_bridge", TABS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"tab lifecycle helper unavailable: {TABS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_app_identity_module():
    spec = importlib.util.spec_from_file_location("codexpro_mcp_identity_bridge", APP_IDENTITY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"app identity helper unavailable: {APP_IDENTITY_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


@contextmanager
def exclusive_composer_lock(path: Path, timeout_seconds: int = 120):
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
                    raise BridgeError("GLOBAL_COMPOSER_LOCK_TIMEOUT", "timed out waiting for the global ChatGPT composer lock")
                time.sleep(0.25)
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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _redact_sensitive_text(value: str) -> str:
    import re

    def redact_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return "<redacted-url>"
        if not parsed.query and not parsed.fragment:
            return raw
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<redacted>", ""))

    value = re.sub(r"https://[^\s\"'<>]+", redact_url, value)
    return re.sub(r"(?i)(codexpro_token|access_token|api[_-]?key)=([^&\s\"']+)", r"\1=<redacted>", value)


def sanitize_evidence(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    if isinstance(value, list):
        return [sanitize_evidence(item) for item in value]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            sanitized = sanitize_evidence(item)
            clean[key] = sanitized
            if isinstance(item, str) and "url" in str(key).casefold() and sanitized != item:
                clean[f"{key}_sha256"] = sha256_bytes(item.encode("utf-8"))
        return clean
    return value


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    STATE.write_json_atomic(path, payload)


def read_contract(path: Path) -> dict[str, Any]:
    value = STATE.read_json(path)
    result = value.get("result") if isinstance(value.get("result"), dict) else value
    if value.get("ok") is False or result.get("ok") is False:
        raise BridgeError("AGBROWSE_CONTRACT_FAILED", "agbrowse contract manifest did not pass", {"path": str(path)})
    package = result.get("agbrowse") if isinstance(result.get("agbrowse"), dict) else {}
    version = str(result.get("version") or result.get("package_version") or package.get("version") or "")
    selected_version = str(package.get("expectedVersion") or "")
    selected_integrity = str(package.get("expectedNpmIntegrity") or "")
    if not selected_version or version != selected_version:
        raise BridgeError(
            "AGBROWSE_VERSION_MISMATCH",
            f"contract selected {selected_version or 'unknown'}, got {version or 'unknown'}",
        )
    if not selected_integrity or package.get("npmIntegrity") != selected_integrity:
        raise BridgeError(
            "AGBROWSE_CONTRACT_INVALID",
            "contract integrity does not match its selected immutable value",
            {"version": selected_version},
        )
    commands = result.get("allowedCommandManifest") or result.get("allowed_commands") or result.get("commands")
    if not commands:
        raise BridgeError("AGBROWSE_CONTRACT_INCOMPLETE", "allowed command contract missing")
    try:
        validator = _load_contract_validator_module()
        validator.validate_manifest(
            result,
            expected_version=selected_version,
            expected_npm_integrity=selected_integrity,
        )
        package = result.get("agbrowse") if isinstance(result.get("agbrowse"), dict) else {}
        installed = validator.resolve_installation(
            executable_path=package.get("executablePath"),
            package_path=package.get("packagePath"),
            expected_version=selected_version,
            expected_npm_integrity=selected_integrity,
        )
    except Exception as exc:
        evidence = getattr(exc, "details", None) or {"detail": str(exc)}
        raise BridgeError("AGBROWSE_CONTRACT_INVALID", "full selected agbrowse contract validation failed", evidence) from exc
    expected = {
        "version": package.get("version"),
        "npmIntegrity": package.get("npmIntegrity"),
        "packageSha256": package.get("packageSha256"),
        "packageFileCount": package.get("packageFileCount"),
        "executableSha256": package.get("executableSha256"),
        "packageEntrypointSha256": package.get("packageEntrypointSha256"),
    }
    actual = {
        "version": installed.version,
        "npmIntegrity": installed.npm_integrity,
        "packageSha256": installed.package_sha256,
        "packageFileCount": installed.package_file_count,
        "executableSha256": installed.executable_sha256,
        "packageEntrypointSha256": installed.package_entrypoint_sha256,
    }
    if actual != expected:
        mismatches = {key: {"expected": expected[key], "actual": actual[key]} for key in expected if expected[key] != actual[key]}
        raise BridgeError("AGBROWSE_INSTALLATION_DRIFT", "installed agbrowse no longer matches captured Gate-0 contract", mismatches)
    return result


def _load_manifest(record: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(record["manifest_path"]))
    return STATE.load_manifest(path)


def _json_output(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise BridgeError("AGBROWSE_JSON_MISSING", "agbrowse returned no JSON")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BridgeError("AGBROWSE_JSON_INVALID", "agbrowse returned non-JSON stdout", {"preview": text[:500]}) from exc
    if not isinstance(value, dict):
        raise BridgeError("AGBROWSE_JSON_INVALID", "agbrowse JSON must be an object")
    return value


def _completed_json_output(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    errors: list[BridgeError] = []
    for text in (completed.stdout or "", completed.stderr or ""):
        if not text.strip():
            continue
        try:
            return _json_output(text)
        except BridgeError as exc:
            errors.append(exc)
    if errors:
        raise errors[-1]
    raise BridgeError("AGBROWSE_JSON_MISSING", "agbrowse returned no JSON on stdout or stderr")


def _find_first(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item not in (None, ""):
                return item
        for item in value.values():
            found = _find_first(item, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first(item, keys)
            if found not in (None, ""):
                return found
    return None


def normalize_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    return {
        "ok": payload.get("ok") is True,
        "status": str(payload.get("status") or ""),
        "session_id": _find_first(payload, {"sessionId", "session_id"}),
        "target_id": _find_first(payload, {"targetId", "target_id", "tabId", "tab_id"}),
        "conversation_url": _find_first(payload, {"conversationUrl", "conversation_url"}),
        "answer_text": _find_first(payload, {"answerText", "answer_text", "finalText", "final_text"}),
        "error_code": str(error.get("errorCode") or error.get("code") or ""),
        "error_stage": str(error.get("stage") or ""),
        "retry_hint": str(error.get("retryHint") or ""),
        "mutation_allowed": error.get("mutationAllowed"),
        "message": str(error.get("message") or payload.get("error") or ""),
        "raw": payload,
    }


def _json_value(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        raise BridgeError("AGBROWSE_JSON_MISSING", "agbrowse returned no JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BridgeError("AGBROWSE_JSON_INVALID", "agbrowse returned non-JSON stdout", {"preview": text[:500]}) from exc


def _tabs_from_payload(payload: Any) -> list[dict[str, Any]]:
    tabs = payload.get("tabs") if isinstance(payload, dict) else payload
    if not isinstance(tabs, list) or not all(isinstance(item, dict) for item in tabs):
        raise BridgeError("TAB_LIST_JSON_INVALID", "agbrowse tabs JSON must be a list")
    return tabs


def _tab_id(tab: dict[str, Any]) -> str:
    return str(tab.get("targetId") or tab.get("target_id") or "")


def _tab_url(tab: dict[str, Any]) -> str:
    return str(tab.get("url") or "")


def _recovery_marker_contract(record: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    identity = record.get("recovery_identity") or {}
    attachment_name = str(identity.get("attachment_name") or "").strip()
    if attachment_name:
        expected = f"prompt-{record['run_id']}.txt"
        if attachment_name != expected:
            raise BridgeError("RECOVERY_IDENTITY_INVALID", "run-owned recovery attachment name is inconsistent")
        return {
            "kind": "run-owned-attachment",
            "primary": attachment_name,
            "corroborators": [],
            "minimum_corroborators": 0,
        }

    prompt_text = str(STATE.prompt_contract(manifest, require_file=True)["prompt_text"])

    def value(key: str, pattern: str = r'[^"\r\n]+') -> str | None:
        match = re.search(rf'"{re.escape(key)}"\s*:\s*"({pattern})"', prompt_text)
        return match.group(1) if match else None

    nonce = value("nonce", r"[A-Za-z0-9_-]{16,128}")
    workflow_id = value("workflow_id")
    stage = value("stage")
    hashes = [
        item
        for item in (
            value("input_plan_sha256", r"[0-9a-fA-F]{64}"),
            value("question_sha256", r"[0-9a-fA-F]{64}"),
            value("source_snapshot_sha256", r"[0-9a-fA-F]{64}"),
        )
        if item
    ]
    if nonce:
        corroborators = [item for item in (workflow_id, stage, *hashes) if item]
        if len(corroborators) < 2:
            raise BridgeError(
                "RECOVERY_IDENTITY_INSUFFICIENT",
                "legacy prompt needs a high-entropy nonce plus two independent corroborating values",
            )
        return {
            "kind": "legacy-prompt-envelope",
            "primary": nonce,
            "corroborators": corroborators,
            "minimum_corroborators": 2,
        }
    if len(hashes) >= 2:
        return {
            "kind": "legacy-prompt-hashes",
            "primary": hashes[0],
            "corroborators": hashes[1:] + ([workflow_id] if workflow_id else []),
            "minimum_corroborators": 1,
        }
    raise BridgeError(
        "RECOVERY_IDENTITY_INSUFFICIENT",
        "the historical prompt has no run-owned filename or sufficiently strong immutable output markers",
    )


def _candidate_matches_recovery_contract(text: str, contract: dict[str, Any]) -> bool:
    if str(contract.get("primary") or "") not in text:
        return False
    corroborators = [str(item) for item in contract.get("corroborators") or []]
    matched = sum(1 for item in corroborators if item and item in text)
    return matched >= int(contract.get("minimum_corroborators") or 0)


def _recent_chat_refs(snapshot: dict[str, Any], *, limit: int) -> list[dict[str, str]]:
    text = str(snapshot.get("text") or "")
    core_markers = list(
        re.finditer(r'(?m)^e\d+\s+button\s+"(?:채팅|Chats?|Conversations?)"\s*$', text)
    )
    if core_markers:
        core_segment = text[core_markers[-1].end():]
        core_end = re.search(r'(?m)^e\d+\s+button\s+".*(?:프로필|Profile)', core_segment)
        if core_end:
            core_segment = core_segment[:core_end.start()]
        core_refs: list[dict[str, str]] = []
        for match in re.finditer(r'(?m)^(e\d+)\s+link\s+"([^"]+)"\s*$', core_segment):
            name = match.group(2).strip()
            if name.casefold() in {"새 채팅", "new chat", "home", "홈", "library", "라이브러리"}:
                continue
            core_refs.append({"ref": match.group(1), "name": name})
            if len(core_refs) >= limit:
                break
        if core_refs:
            return core_refs

    markers = list(
        re.finditer(
            r'(?m)^\s*- button "(?:채팅|Chats?|Conversations?)" \[ref=@?e\d+[^\]]*expanded=true[^\]]*\]:',
            text,
        )
    )
    segment = text[markers[-1].end():] if markers else text
    end = re.search(r'(?m)^\s{2}- (?:button ".*(?:프로필|Profile)|main\b)', segment)
    if end:
        segment = segment[:end.start()]
    refs: list[dict[str, str]] = []
    for match in re.finditer(r'(?m)^\s*- link "([^"]+)" \[ref=(@?e\d+)\]:', segment):
        name = match.group(1).strip()
        if name.casefold() in {"새 채팅", "new chat", "home", "홈", "library", "라이브러리"}:
            continue
        refs.append({"ref": match.group(2).lstrip("@"), "name": name})
        if len(refs) >= limit:
            break
    if refs:
        return refs

    static_names = {
        "새 채팅", "new chat", "home", "홈", "library", "라이브러리", "일정", "calendar",
        "플러그인", "plugins", "콘텐츠로 건너뛰기", "skip to content",
    }
    for ref, item in (snapshot.get("refs") or {}).items():
        if not isinstance(item, dict) or str(item.get("role") or "") != "link":
            continue
        name = str(item.get("name") or "").strip()
        if not name or name.casefold() in static_names:
            continue
        refs.append({"ref": str(ref).lstrip("@"), "name": name})
        if len(refs) >= limit:
            break
    return refs


def _streaming_state(status: dict[str, Any]) -> bool | None:
    for capability in status.get("capabilities") or []:
        if not isinstance(capability, dict):
            continue
        if str(capability.get("capabilityId") or "") != "chatgpt-response-streaming":
            continue
        evidence = capability.get("evidence") or {}
        value = evidence.get("streaming") if isinstance(evidence, dict) else None
        return value if isinstance(value, bool) else None
    return None


def _matching_json_answer(page_text: str, contract: dict[str, Any]) -> str | None:
    decoder = json.JSONDecoder()
    for index, character in enumerate(page_text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(page_text[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if _candidate_matches_recovery_contract(rendered, contract):
            return json.dumps(value, ensure_ascii=False, indent=2)
    return None


def _plain_assistant_answer(page_text: str) -> str | None:
    labels = ("ChatGPT의 말:", "ChatGPT said:", "Assistant:")
    positions = [(page_text.rfind(label), label) for label in labels]
    start, label = max(positions, key=lambda item: item[0])
    if start < 0:
        return None
    answer = page_text[start + len(label):]
    end_markers = (
        "\n출처\n", "\nSources\n", "\nChatGPT는 실수를 할 수 있습니다.",
        "\nChatGPT can make mistakes", "\n응답 작업\n",
    )
    ends = [answer.find(marker) for marker in end_markers if answer.find(marker) >= 0]
    if ends:
        answer = answer[:min(ends)]
    answer = answer.strip()
    return answer or None


def _terminal_visible_assistant_answer(page_text: str) -> str | None:
    """Extract a completed answer from ChatGPT's visible-text layout.

    ``agbrowse text`` does not always prefix the assistant block with
    ``ChatGPT said:``.  Completed thinking responses do expose a stable
    elapsed-work line immediately before the answer and sources/footer
    controls immediately after it.
    """
    lines = [line.rstrip() for line in str(page_text or "").splitlines()]
    start_indexes: list[int] = []
    elapsed_patterns = (
        re.compile(r"^\s*\d+(?:\s*[hms]\s*\d*)*\s*동안\s*(?:처리함|작업함|생각함)\s*$", re.IGNORECASE),
        re.compile(r"^\s*(?:Thought|Worked|Reasoned)\s+for\s+\d+.*$", re.IGNORECASE),
    )
    for index, line in enumerate(lines):
        if any(pattern.fullmatch(line) for pattern in elapsed_patterns):
            start_indexes.append(index)
    if not start_indexes:
        return None

    start = start_indexes[-1] + 1
    terminal_markers = {
        "출처",
        "Sources",
        "응답 작업",
        "ChatGPT는 실수를 할 수 있습니다. 중요한 정보는 재차 확인하세요.",
        "ChatGPT can make mistakes. Check important info.",
    }
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].strip() in terminal_markers:
            end = index
            break
    answer = "\n".join(lines[start:end]).strip()
    return answer or None


def _web_multi_assistant_answer(page_text: str) -> str | None:
    """Extract one complete Web Multi payload from visible ChatGPT text."""
    start_marker = "<<<WEB_MULTI_HEADER_V1>>>"
    end_marker = "<<<END_WEB_MULTI_PAYLOAD_V1>>>"
    start = page_text.rfind(start_marker)
    if start < 0:
        return None
    end = page_text.find(end_marker, start + len(start_marker))
    if end < 0:
        return None
    answer = page_text[start:end + len(end_marker)].strip()
    return answer or None


def provider_terminal_error_ui(answer_text: str) -> dict[str, Any] | None:
    """Recognize only exact provider-error controls at the answer tail.

    ChatGPT can expose a long partial assistant message followed by its own
    stream-error banner and Retry button.  agbrowse 0.1.18 currently reports
    that stable DOM as ``status=complete``.  Matching a tight terminal pair
    avoids treating ordinary prose that merely discusses errors as provider
    UI authority.
    """
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(answer_text or "").splitlines()]
    lines = [line for line in lines if line]
    standalone_errors = {
        "메시지 전송 시간이 초과되었습니다. 다시 시도해 주세요.",
        "메시지 전송 시간이 초과되었습니다. 다시 시도하세요.",
        "Message sending timed out. Please try again.",
    }
    if len(lines) == 1 and lines[0] in standalone_errors:
        error_line = lines[0]
        return {
            "signature": "chatgpt-send-timeout-v1",
            "error_label": error_line,
            "retry_label": "",
            "tail_sha256": hashlib.sha256(error_line.encode("utf-8")).hexdigest(),
        }
    if len(lines) < 2:
        return None
    retry_labels = {"다시 시도", "Retry", "Try again", "Regenerate"}
    error_labels = {
        "메시지 스트림에 오류 발생",
        "응답을 생성하는 중에 오류가 발생했습니다.",
        "There was an error generating a response",
        "Something went wrong while generating the response",
    }
    retry_line = lines[-1]
    if retry_line not in retry_labels:
        return None
    candidates = lines[max(0, len(lines) - 4):-1]
    error_line = next((line for line in reversed(candidates) if line in error_labels), None)
    if error_line is None:
        return None
    tail = "\n".join(lines[-4:])
    return {
        "signature": "chatgpt-stream-error-retry-v1",
        "error_label": error_line,
        "retry_label": retry_line,
        "tail_sha256": hashlib.sha256(tail.encode("utf-8")).hexdigest(),
    }


def classify_pre_submit_failure(envelope: dict[str, Any]) -> str:
    code = str(envelope.get("error_code") or "")
    stage = str(envelope.get("error_stage") or "")
    message = str(envelope.get("message") or "").lower()
    mutation_allowed = envelope.get("mutation_allowed")
    if mutation_allowed is False:
        # The runner explicitly proves that it did not cross the provider
        # mutation boundary. Capacity/backpressure is therefore a safe
        # pre-submit rejection, never an uncertain user submission.
        if code == "provider.active-capacity":
            return "SEND_REJECTED"
        if code == "capability.unsupported" and stage == "provider-surface-preflight":
            return "SEND_REJECTED"
        if code == "cdp.unreachable" and stage == "connect":
            return "SEND_REJECTED"
        if code == "cdp.headless" and stage == "connect":
            return "SEND_REJECTED"
        if (
            code == "internal.unhandled"
            and stage == "internal"
            and message.startswith("web-ai session store: failed to acquire lock at ")
            and re.search(r" after \d+ attempts$", message)
        ):
            return "SEND_REJECTED"
        if "no session record" in message or stage in {
            "preflight",
            "provider-surface-preflight",
            "target-resolution",
            "attachment-preflight",
        }:
            return "SEND_REJECTED"
    return "SUBMISSION_UNCERTAIN_IDENTITY_MISSING"


def session_send_not_committed(session: Mapping[str, Any]) -> bool:
    """Return true only for agbrowse's false ``status=sent`` envelope.

    agbrowse 0.1.18 can persist ``sent`` even when its send-button resolver
    reports ``TARGET_UNRESOLVED/not-enabled``.  A bare ChatGPT root page with
    no assistant turn is therefore pre-submit evidence, not a running job.
    """
    summary = session.get("envelopeSummary") if isinstance(session.get("envelopeSummary"), dict) else {}
    trace = session.get("trace") if isinstance(session.get("trace"), list) else []
    send_click = next(
        (
            item
            for item in trace
            if isinstance(item, dict)
            and str(item.get("intentId") or "") == "send.click"
            and str(item.get("status") or "") == "unresolved"
            and str(item.get("errorCode") or "") == "TARGET_UNRESOLVED"
        ),
        None,
    )
    attempts = send_click.get("attempts") if isinstance(send_click, dict) and isinstance(send_click.get("attempts"), list) else []
    not_enabled = any(
        isinstance(item, dict)
        and isinstance(item.get("validation"), dict)
        and str(item["validation"].get("reason") or "") == "not-enabled"
        for item in attempts
    )
    observed_url = str(
        session.get("conversationUrl")
        or session.get("conversation_url")
        or session.get("originalUrl")
        or ""
    )
    return bool(
        str(session.get("status") or "") == "sent"
        and observed_url.rstrip("/") in {"https://chatgpt.com", "https://chat.openai.com"}
        and session.get("answer") in (None, "")
        and int(summary.get("assistantCount") or 0) == 0
        and not_enabled
    )


def exact_target_observation(tabs: Iterable[Mapping[str, Any]], target_id: str) -> dict[str, Any]:
    """Classify one recorded target without consulting the active browser tab."""
    matches = [dict(tab) for tab in tabs if _tab_id(tab) == str(target_id or "")]
    if len(matches) != 1:
        return {"state": "absent" if not matches else "ambiguous", "match_count": len(matches)}
    tab = matches[0]
    url = _tab_url(tab)
    if STATE.CANONICAL_CHAT_RE.fullmatch(url):
        return {
            "state": "canonical",
            "match_count": 1,
            "target_id": target_id,
            "url": STATE.canonical_conversation_url(url),
            "tab": tab,
        }
    if url.rstrip("/") in {"https://chatgpt.com", "https://chat.openai.com"}:
        return {"state": "root", "match_count": 1, "target_id": target_id, "url": url, "tab": tab}
    return {"state": "drifted", "match_count": 1, "target_id": target_id, "url": url, "tab": tab}


def build_exact_poll_command(executable: str, session_id: str, timeout: int) -> list[str]:
    """Poll the recorded session target without navigating to stale stored URLs."""
    return [
        executable,
        "web-ai",
        "poll",
        "--vendor",
        "chatgpt",
        "--session",
        session_id,
        "--timeout",
        str(timeout),
        "--json",
    ]


def app_decision_scope_matches(run_root: Path, decision_root: Path, scope_mode: str = "legacy-drive") -> bool:
    """Match legacy drive scope or fail-closed v3 exact-unit scope."""
    if scope_mode == "parallel-exact-unit":
        return decision_root == run_root
    if scope_mode != "legacy-drive":
        return False
    if decision_root == run_root:
        return True
    anchor = run_root.anchor
    return bool(anchor) and decision_root == Path(anchor).resolve()


def _mode_args(manifest: dict[str, Any]) -> list[str]:
    label = str(manifest.get("mode_label") or "GPT-5.6").strip().lower()
    variant = str(manifest.get("mode_variant") or "High").strip().lower().replace("_", " ")
    if label == "pro":
        return ["--model", "pro"]
    if label in {"deep research", "deep-research"}:
        if variant not in {"high", "높음"}:
            raise BridgeError("MODE_VARIANT_UNSUPPORTED", "new Deep Research work requires mode_variant=High")
        return ["--family", "gpt-5.6-sol", "--model", "thinking", "--effort", "high", "--research", "deep"]
    regular_efforts = {
        "high": "high",
        "높음": "high",
        "very high": "xhigh",
        "매우 높음": "xhigh",
        "xhigh": "xhigh",
        "extra high": "xhigh",
        "heavy": "xhigh",
    }
    effort = regular_efforts.get(variant)
    if effort is None:
        raise BridgeError(
            "MODE_VARIANT_UNSUPPORTED",
            "new regular GPT work requires mode_variant=High or Very High",
        )
    return ["--family", "gpt-5.6-sol", "--model", "thinking", "--effort", effort]


def build_send_command(
    record: dict[str, Any],
    manifest: dict[str, Any],
    executable: str,
    *,
    preselected_app: bool = False,
    connected_app_auto: bool = False,
    preselected_research: bool = False,
) -> list[str]:
    try:
        prompt_contract = STATE.prompt_contract(manifest, require_file=True)
    except STATE.StateError as exc:
        raise BridgeError(exc.code, str(exc), exc.evidence) from exc
    question = str(prompt_contract["dispatch_text"])
    command = [executable, "web-ai", "send", "--vendor", "chatgpt", "--surface", "chat"]
    files = manifest.get("files") or []
    if isinstance(files, str):
        files = [files]
    source_prompt_path = Path(str(prompt_contract["prompt_file"])).expanduser().resolve()
    recovery_identity = record.get("recovery_identity") or {}
    recovery_prompt_path = None
    if recovery_identity:
        recovery_prompt_path = Path(str(recovery_identity.get("attachment_path") or "")).expanduser().resolve()
    if files:
        for item in files:
            path = Path(str(item)).expanduser().resolve()
            if recovery_prompt_path is not None and path == source_prompt_path:
                path = recovery_prompt_path
            if not path.is_file() or path.is_symlink():
                raise BridgeError("ATTACHMENT_INVALID", f"attachment must be a regular file: {path}")
            if path == recovery_prompt_path and STATE.sha256_file(path) != str(record.get("prompt_sha256") or ""):
                raise BridgeError("RECOVERY_PROMPT_ALIAS_HASH_MISMATCH", "run-owned prompt alias bytes changed")
            command.extend(["--file", str(path)])
    else:
        command.append("--inline-only")
    command.extend(["--prompt", question])
    command.extend(_mode_args(manifest))
    provider_url = str(manifest.get("provider_url") or manifest.get("chatgpt_url") or "https://chatgpt.com/").strip()
    if not provider_url.startswith("https://chatgpt.com/"):
        raise BridgeError("PROVIDER_URL_INVALID", "provider_url must use the exact https://chatgpt.com/ origin")
    command.extend(["--url", provider_url])

    requested = record.get("requested") or {}
    mode_label = str(manifest.get("mode_label") or "GPT-5.6").strip().casefold()
    app_policy = str(requested.get("app_policy") or ("forbidden" if mode_label == "pro" else "required"))
    app_name = str(manifest.get("chatgpt_app_name") or manifest.get("app_name") or "").strip()
    research_mode = mode_label in {"deep research", "deep-research"}
    if mode_label == "pro":
        if app_policy != "forbidden" or app_name:
            raise BridgeError("APP_POLICY_FORBIDDEN", "Pro requires app_policy=forbidden and no app name")
    else:
        if app_policy != "required":
            raise BridgeError("APP_POLICY_REQUIRED", "every non-Pro ChatGPT mode requires app_policy=required")
        if not app_name:
            raise BridgeError("APP_REQUIRED", "every non-Pro ChatGPT mode requires an exact app name")
    if preselected_app and (app_policy != "required" or not app_name):
        raise BridgeError("APP_PRESELECTION_INVALID", "preselected app requires app_policy=required and an exact app name")
    if connected_app_auto and (app_policy != "required" or not app_name):
        raise BridgeError("APP_AUTO_SELECTION_INVALID", "connected-app auto mode requires app_policy=required and an exact app name")
    if preselected_app and connected_app_auto:
        raise BridgeError("APP_SELECTION_TRANSPORT_CONFLICT", "app selection transports are mutually exclusive")
    if preselected_research and (not research_mode or app_policy != "required" or not app_name or not preselected_app):
        raise BridgeError(
            "RESEARCH_PRESELECTION_INVALID",
            "preselected research requires Deep Research plus the exact required app selection",
        )
    if research_mode and not preselected_research:
        raise BridgeError("RESEARCH_PRESELECTION_REQUIRED", "Deep Research requires exact preselected capability evidence")
    if app_name and app_policy != "forbidden" and not preselected_app and not connected_app_auto:
        command.extend(["--plugin", app_name])
    if bool(manifest.get("search_enabled") or manifest.get("web_search")):
        command.append("--web-search")
    inline_metadata = [
        source
        for source in ("project", "goal", "constraints", "output")
        if str(manifest.get(source) or "").strip()
    ]
    if inline_metadata:
        raise BridgeError(
            "PROMPT_METADATA_INLINE_FORBIDDEN",
            "prompt-like metadata must be included in the hashed prompt file, not command-line fields",
            {"fields": inline_metadata},
        )
    command.extend([
        "--reuse-tab" if (preselected_app or connected_app_auto or preselected_research) else "--parallel",
        "--json",
    ])
    return command


def bridge_env(manifest: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["AGBROWSE_JSON_ERRORS"] = "1"
    env["AGBROWSE_UPDATE_CHECK"] = "0"
    env["AGBROWSE_WEB_AI_AUTO_START"] = str(manifest.get("agbrowse_auto_start", "1"))
    env["BROWSER_AGENT_HOME"] = str(
        Path(str(manifest.get("browser_agent_home") or (Path.home() / ".browser-agent"))).expanduser().resolve()
    )
    env["CDP_PORT"] = str(int(manifest.get("cdp_port") or 9222))
    # Exact run ownership below replaces agbrowse's generic idle/count cleanup.
    # These high limits prevent a later send from auto-closing a completed or
    # uncertain conversation owned by another run.
    env["AGBROWSE_MAX_TABS"] = "100000"
    env["AGBROWSE_TAB_IDLE"] = "999999h"
    env["AGBROWSE_PROVIDER_POOL_MAX_PER_KEY"] = "100000"
    env["AGBROWSE_PROVIDER_POOL_GLOBAL_MAX"] = "100000"
    env["AGBROWSE_PROVIDER_POOL_TTL"] = "999999h"
    return env


def composer_lock_timeout_seconds(manifest: Mapping[str, Any]) -> int:
    explicit = manifest.get("composer_lock_timeout_seconds")
    if explicit not in (None, ""):
        return max(1, int(explicit))
    send_timeout = int(manifest.get("send_timeout_seconds") or 600)
    return max(120, send_timeout + 600)


Runner = Callable[[list[str], dict[str, str], int], subprocess.CompletedProcess[str]]


def pre_send_command_budget(command: list[str], *, platform_name: str | None = None) -> dict[str, Any]:
    """Return a redacted command-line budget check before the mutation boundary."""
    platform = platform_name or os.name
    executable_suffix = Path(str(command[0])).suffix.casefold() if command else ""
    command_line_chars = len(subprocess.list2cmdline([str(item) for item in command]))
    guarded = platform == "nt" and executable_suffix in {".cmd", ".bat"}
    limit = WINDOWS_CMD_WRAPPER_SAFE_CHARS if guarded else None
    return {
        "platform": platform,
        "executable_suffix": executable_suffix,
        "argv_count": len(command),
        "command_line_chars": command_line_chars,
        "limit_chars": limit,
        "within_budget": limit is None or command_line_chars <= limit,
    }


def default_runner(command: list[str], env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs.update(
            creationflags=subprocess.CREATE_NO_WINDOW,
            startupinfo=startupinfo,
        )
    return subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
        **kwargs,
    )


def contract_executable(contract: Mapping[str, Any] | None) -> str:
    value = contract if isinstance(contract, Mapping) else {}
    package = value.get("agbrowse") if isinstance(value.get("agbrowse"), Mapping) else {}
    return str(
        value.get("executable")
        or value.get("executable_path")
        or package.get("executablePath")
        or "agbrowse"
    )


def record_executable(record: Mapping[str, Any]) -> str:
    contract = record.get("agbrowse") if isinstance(record.get("agbrowse"), Mapping) else {}
    return contract_executable(contract)


class Bridge:
    def __init__(
        self,
        *,
        state_root: Path | None = None,
        runner: Runner | None = None,
        app_connector_factory=None,
        research_composer_factory=None,
        app_identity_probe=None,
        tab_lifecycle_factory=None,
        headed_runtime_preflight: bool = True,
    ):
        self.store = STATE.RunStore(state_root)
        self.runner = runner or default_runner
        self.app_connector_factory = app_connector_factory
        self.research_composer_factory = research_composer_factory
        self.app_identity_probe = app_identity_probe
        self.tab_lifecycle_factory = tab_lifecycle_factory
        self.headed_runtime_preflight = bool(headed_runtime_preflight)

    def _record_provider_terminal_failure(
        self,
        run_dir: str,
        *,
        answer_text: str,
        provider_status: str,
        command_evidence: Mapping[str, Any],
        detection: Mapping[str, Any],
    ) -> dict[str, Any]:
        state_file, record = self.store.load(run_dir)
        data = answer_text.rstrip().encode("utf-8") + b"\n"
        answer_path = state_file.parent / "provider-terminal-failure.md"
        if answer_path.exists():
            if answer_path.is_symlink() or answer_path.read_bytes() != data:
                raise BridgeError(
                    "PROVIDER_FAILURE_EVIDENCE_CONFLICT",
                    "provider terminal failure evidence changed",
                    {"path": str(answer_path)},
                )
        else:
            answer_path.write_bytes(data)
        event = {
            "kind": "provider-terminal-error-ui",
            "signature": str(detection["signature"]),
            "provider_status": str(provider_status).lower(),
            "answer_path": str(answer_path),
            "answer_sha256": STATE.sha256_file(answer_path),
            "answer_bytes": answer_path.stat().st_size,
            "tail_sha256": str(detection.get("tail_sha256") or ""),
            "error_label_sha256": hashlib.sha256(str(detection.get("error_label") or "").encode("utf-8")).hexdigest(),
            "retry_label_sha256": hashlib.sha256(str(detection.get("retry_label") or "").encode("utf-8")).hexdigest(),
            "command_evidence": sanitize_evidence(dict(command_evidence)),
            "session_id": record.get("session_id"),
            "target_id": record.get("current_target_id"),
            "conversation_url": record.get("conversation_url"),
        }
        return self.store.transition(
            run_dir,
            "PROVIDER_FAILED_TERMINAL",
            block_code="PROVIDER_TERMINAL_ERROR_UI",
            recovery_event=event,
        )

    def _settle_stale_project_before_prepare(self, project_root: str) -> dict[str, Any]:
        diagnosis = self.store.reconcile_project_lock(project_root, apply_safe_pre_submission=False)
        state = str(diagnosis.get("state") or "")
        if state == "CLEAR":
            return diagnosis
        if state in {
            "STALE_PRE_SUBMISSION_SAFE_TO_CANCEL",
            "STALE_DUPLICATE_COMPLETE_OWNER_SAFE_TO_SETTLE",
            "TERMINAL_ORPHAN_LOCK_DETECTED",
        }:
            settled = self.store.reconcile_project_lock(project_root, apply_safe_pre_submission=True)
            if str(settled.get("state") or "") not in {
                "STALE_PRE_SUBMISSION_CANCELLED",
                "STALE_DUPLICATE_COMPLETE_OWNER_SETTLED",
                "TERMINAL_ORPHAN_LOCK_REMOVED",
            }:
                raise BridgeError(
                    "STALE_PROJECT_RECONCILE_FAILED",
                    "safe stale project state could not be reconciled before prepare",
                    {"diagnosis": sanitize_evidence(settled)},
                )
            return settled
        if state != "STALE_OWNER_UNRESOLVED_SUBMISSION":
            return diagnosis

        run_id = str(diagnosis.get("run_id") or "")
        if not run_id:
            raise BridgeError(
                "STALE_PROJECT_RECOVERY_IDENTITY_MISSING",
                "stale submitted project lock has no exact run id",
                {"diagnosis": sanitize_evidence(diagnosis)},
            )
        root = STATE.canonical_project_root(project_root)
        run_dir = self.store.paths(root, run_id).runs_dir / run_id
        recovered = self.recover(str(run_dir))
        history_rebind = any(
            str(item.get("reason") or "") == "history-fingerprint-adjudication"
            for item in recovered.get("target_rebind_events") or []
            if isinstance(item, dict)
        )
        if recovered.get("phase") in {"URL_BOUND", "RESPONSE_IN_PROGRESS"} and not history_rebind:
            recovered = self.poll(str(run_dir))
        if recovered.get("phase") == "BLOCKED_TARGET_AMBIGUOUS":
            duplicate = self.store.reconcile_project_lock(project_root, apply_safe_pre_submission=False)
            if str(duplicate.get("state") or "") == "STALE_DUPLICATE_COMPLETE_OWNER_SAFE_TO_SETTLE":
                settled = self.store.reconcile_project_lock(project_root, apply_safe_pre_submission=True)
                if str(settled.get("state") or "") != "STALE_DUPLICATE_COMPLETE_OWNER_SETTLED":
                    raise BridgeError(
                        "STALE_DUPLICATE_COMPLETE_OWNER_SETTLE_FAILED",
                        "completed duplicate URL ownership could not be settled deterministically",
                        {"diagnosis": sanitize_evidence(settled)},
                    )
                return settled
        if recovered.get("phase") != "COMPLETE":
            raise BridgeError(
                "STALE_PROJECT_RECOVERY_PENDING",
                "the exact previous run was adjudicated but is not safely complete; no replacement was submitted",
                {
                    "run_id": run_id,
                    "phase": recovered.get("phase"),
                    "conversation_url": recovered.get("conversation_url"),
                    "terminal_block_code": recovered.get("terminal_block_code"),
                },
            )
        return {
            "ok": True,
            "state": "STALE_SUBMISSION_ADJUDICATED_COMPLETE",
            "run_id": run_id,
            "conversation_url": recovered.get("conversation_url"),
        }

    def prepare(self, *, project_root: str, manifest_path: str, contract_path: str) -> dict[str, Any]:
        contract = read_contract(Path(contract_path))
        package = contract.get("agbrowse") if isinstance(contract.get("agbrowse"), dict) else {}
        executable = contract_executable(contract)
        prepared_contract = {
            "schema": str(contract.get("schema") or "codex.chatgpt.agbrowse-contract/v1"),
            "version": str(package.get("version") or ""),
            "executable": executable,
            "executable_sha256": contract.get("executable_sha256") or package.get("executableSha256"),
            "package_integrity": (
                contract.get("package_integrity")
                or contract.get("npm_integrity")
                or package.get("npmIntegrity")
            ),
            "contract_sha256": STATE.sha256_file(Path(contract_path)),
        }
        self._settle_stale_project_before_prepare(project_root)
        record = self.store.create_run(
            project_root=project_root,
            manifest_path=manifest_path,
            agbrowse_contract=prepared_contract,
        )
        run_dir = str(record["run_dir"])
        prepared = self.store.transition(run_dir, "PREFLIGHTED")
        return {**prepared, "run_dir": run_dir, "state_file": str(Path(run_dir) / "run.json")}

    def _app_connector(self, executable: str):
        if self.app_connector_factory:
            return self.app_connector_factory(executable)
        module = _load_app_module()
        return module.AppConnector(module.AgbrowseGateway(executable=executable))

    def _research_composer(self, executable: str):
        if self.research_composer_factory:
            return self.research_composer_factory(executable)
        module = _load_composer_module()
        return module.ResearchComposer(
            module.AgbrowseGateway(executable=executable, runner=self.runner)
        )

    def _tab_lifecycle(self, executable: str, manifest: dict[str, Any]):
        if self.tab_lifecycle_factory:
            return self.tab_lifecycle_factory(executable, manifest)
        module = _load_tabs_module()
        return module.TabLifecycle(
            state_root=self.store.root,
            executable=executable,
            runner=self.runner,
            env=bridge_env(manifest),
        )

    def _browser_runtime_blockers(self, *, exclude_run_id: str) -> list[dict[str, Any]]:
        safe_terminal = {
            "COMPLETE",
            "COMPLETE_SUPERSEDED",
            "CANCELLED_PRE_SUBMISSION",
            "ABANDONED_UNCERTAIN",
            "PROVIDER_FAILED_TERMINAL",
        }
        blockers: list[dict[str, Any]] = []
        projects_root = self.store.root / "projects"
        if not projects_root.exists():
            return blockers
        for state_file in projects_root.glob("*/runs/*/run.json"):
            try:
                candidate = STATE.read_json(state_file)
            except Exception:
                blockers.append({"run_id": state_file.parent.name, "phase": "STATE_UNREADABLE"})
                continue
            if str(candidate.get("run_id") or "") == exclude_run_id:
                continue
            if str(candidate.get("record_kind") or "standalone") == "parent":
                continue
            phase = str(candidate.get("phase") or "")
            if phase in safe_terminal:
                continue
            owner_observation = self.store._owner_observation(candidate)
            if (
                owner_observation.get("same_process") is False
                and self.store._safe_stale_pre_submission(state_file, candidate)
            ):
                # A dead, provably pre-submit record has no provider work and
                # cannot make a blank headless runtime unsafe to restart.  Its
                # own project lock remains for that project's normal reconcile.
                continue
            blockers.append(
                {
                    "run_id": candidate.get("run_id"),
                    "parent_run_id": candidate.get("parent_run_id"),
                    "project_key": candidate.get("project_key"),
                    "phase": phase,
                    "session_id": candidate.get("session_id"),
                    "target_id": candidate.get("current_target_id"),
                    "owner_observation": owner_observation,
                }
            )
        return blockers

    @staticmethod
    def _blank_runtime_tab(tab: Mapping[str, Any]) -> bool:
        url = str(tab.get("url") or "").strip().casefold()
        return url in {"", "about:blank", "chrome://newtab/", "chrome://new-tab-page/"}

    def _ensure_headed_runtime(
        self,
        *,
        run_dir: str,
        state_file: Path,
        record: dict[str, Any],
        manifest: dict[str, Any],
        executable: str,
        lifecycle,
    ) -> dict[str, Any]:
        env = bridge_env(manifest)
        port = int(manifest.get("cdp_port") or 9222)
        timeout = int(manifest.get("browser_start_timeout_seconds") or 90)
        preflight_index = 1 + sum(
            1
            for event in record.get("recovery_events") or []
            if isinstance(event, dict)
            and str(event.get("kind") or "").startswith(("headed-runtime-", "headless-runtime-"))
        )
        start_command = [executable, "start", "--headed", "--port", str(port)]
        started = self.runner(start_command, env, timeout)
        start_evidence = self._evidence(
            state_file.parent,
            f"headed-runtime-start-{preflight_index:02d}",
            started,
        )
        if started.returncode == 0:
            return self.store.transition(
                run_dir,
                str(record["phase"]),
                recovery_event={
                    "kind": "headed-runtime-ready",
                    "port": port,
                    "evidence": start_evidence,
                },
            )

        failure_text = f"{started.stderr or ''}\n{started.stdout or ''}"
        headless_conflict = "already backed by a headless agbrowse Chrome" in failure_text
        if not headless_conflict:
            return self.store.transition(
                run_dir,
                "PREFLIGHT_BLOCKED",
                block_code="HEADED_BROWSER_START_FAILED",
                recovery_event={
                    "kind": "headed-runtime-start-failed",
                    "port": port,
                    "evidence": start_evidence,
                },
            )

        blockers = self._browser_runtime_blockers(exclude_run_id=str(record["run_id"]))
        try:
            tabs = lifecycle.list_tabs()
            nonblank_tabs = [
                {
                    "target_id": str(tab.get("targetId") or tab.get("target_id") or tab.get("id") or ""),
                    "url": str(tab.get("url") or ""),
                }
                for tab in tabs
                if not self._blank_runtime_tab(tab)
            ]
        except Exception as exc:
            tabs = []
            nonblank_tabs = [{"target_id": "", "url": "", "error": _redact_sensitive_text(str(exc))}]

        if blockers or nonblank_tabs:
            return self.store.transition(
                run_dir,
                "PREFLIGHT_BLOCKED",
                block_code="HEADED_BROWSER_RESTART_UNSAFE",
                recovery_event={
                    "kind": "headed-runtime-restart-deferred",
                    "port": port,
                    "active_or_uncertain_runs": blockers,
                    "nonblank_tabs": nonblank_tabs,
                    "start_evidence": start_evidence,
                },
            )

        stopped = self.runner([executable, "stop"], env, timeout)
        stop_evidence = self._evidence(
            state_file.parent,
            f"headless-runtime-stop-{preflight_index:02d}",
            stopped,
        )
        if stopped.returncode != 0:
            return self.store.transition(
                run_dir,
                "PREFLIGHT_BLOCKED",
                block_code="HEADLESS_BROWSER_STOP_FAILED",
                recovery_event={
                    "kind": "headless-runtime-stop-failed",
                    "port": port,
                    "start_evidence": start_evidence,
                    "stop_evidence": stop_evidence,
                },
            )
        restarted = self.runner(start_command, env, timeout)
        restart_evidence = self._evidence(
            state_file.parent,
            f"headed-runtime-restart-{preflight_index:02d}",
            restarted,
        )
        if restarted.returncode != 0:
            return self.store.transition(
                run_dir,
                "PREFLIGHT_BLOCKED",
                block_code="HEADED_BROWSER_RESTART_FAILED",
                recovery_event={
                    "kind": "headed-runtime-restart-failed",
                    "port": port,
                    "start_evidence": start_evidence,
                    "stop_evidence": stop_evidence,
                    "restart_evidence": restart_evidence,
                },
            )
        return self.store.transition(
            run_dir,
            str(record["phase"]),
            recovery_event={
                "kind": "headless-runtime-safely-restarted-headed",
                "port": port,
                "start_evidence": start_evidence,
                "stop_evidence": stop_evidence,
                "restart_evidence": restart_evidence,
                "verified_blank_tab_count": len(tabs),
            },
        )

    @staticmethod
    def _owned_target_from_exception(exc: Exception) -> str | None:
        evidence = getattr(exc, "evidence", None)
        if not isinstance(evidence, dict):
            return None
        return str(evidence.get("owned_target_id") or "") or None

    @staticmethod
    def _safe_tab_cleanup(lifecycle, run_dir: str, *, target_id: str | None, url: str, reason: str) -> dict[str, Any]:
        if not target_id:
            return {"ok": True, "skipped": True, "reason": "target-id-missing"}
        try:
            lifecycle.record_owned(run_dir, target_id=target_id, url=url, stage="pre-submit")
            return lifecycle.close_pre_submit(run_dir, target_id=target_id, reason=reason)
        except Exception as exc:
            return {
                "ok": False,
                "error_code": str(getattr(exc, "code", type(exc).__name__)),
                "detail": _redact_sensitive_text(str(exc)),
                "target_id": target_id,
            }

    def cleanup_completed(self, run_dir: str, *, explicit_user_request: bool = False) -> dict[str, Any]:
        _, record = self.store.load(run_dir)
        manifest = _load_manifest(record)
        executable = record_executable(record)
        lifecycle = self._tab_lifecycle(executable, manifest)
        utilities_closed = False
        try:
            cleanup = lifecycle.close_completed(run_dir, explicit_user_request=explicit_user_request)
        except Exception as exc:
            code = str(getattr(exc, "code", ""))
            if code == "TAB_COMPLETED_URL_AMBIGUOUS":
                lifecycle.close_terminal_recovery_utilities(
                    run_dir,
                    explicit_user_request=explicit_user_request,
                )
                utilities_closed = True
                try:
                    cleanup = lifecycle.close_completed(run_dir, explicit_user_request=explicit_user_request)
                except Exception as retry_exc:
                    exc = retry_exc
                    code = str(getattr(retry_exc, "code", ""))
                else:
                    code = ""
            if code and code != "TAB_COMPLETED_TARGET_MISMATCH":
                raise exc
            if code == "TAB_COMPLETED_TARGET_MISMATCH":
                close_utilities = getattr(lifecycle, "close_terminal_recovery_utilities", None)
                if callable(close_utilities) and not utilities_closed:
                    try:
                        close_utilities(
                            run_dir,
                            explicit_user_request=explicit_user_request,
                        )
                    except Exception as utility_exc:
                        utility_code = str(getattr(utility_exc, "code", ""))
                        if utility_code != "TAB_RECOVERY_UTILITY_OWNERSHIP_MISSING":
                            raise utility_exc
                    else:
                        utilities_closed = True
                        try:
                            cleanup = lifecycle.close_completed(
                                run_dir,
                                explicit_user_request=explicit_user_request,
                            )
                        except Exception as retry_exc:
                            exc = retry_exc
                            code = str(getattr(retry_exc, "code", ""))
                        else:
                            code = ""
                if code == "TAB_COMPLETED_TARGET_MISMATCH":
                    candidate = lifecycle.terminal_rebind_candidate(run_dir)
                    self.store.rebind_terminal_target(run_dir, candidate)
                    cleanup = lifecycle.close_completed(run_dir, explicit_user_request=explicit_user_request)
        self.store.record_terminal_cleanup(run_dir, cleanup)
        return cleanup

    def abandon_uncertain(
        self,
        run_dir: str,
        *,
        explicit_user_request: bool,
        reason: str,
    ) -> dict[str, Any]:
        return self.confirm_user_stop(run_dir, explicit_user_request=explicit_user_request, reason=reason)

    def _finish_target_drift_parent(self, state_file: Path, finalized: dict[str, Any]) -> dict[str, Any]:
        parent_dir = state_file.parent.parent / str(finalized.get("parent_run_id") or "")
        self._settle_user_stop_send_rejected_siblings(
            parent_dir=parent_dir, excluded_run_id=str(finalized.get("run_id") or "")
        )
        final_tabs = self._parent_stop_final_tab_scan(parent_dir)
        drained = self.store.finalize_user_stopped_parent(parent_dir, tab_absence_evidence=final_tabs)
        if str(drained.get("phase") or "") == "PARENT_DRAINING":
            retry_tabs = self._parent_stop_final_tab_scan(parent_dir)
            drained = self.store.finalize_user_stopped_parent(
                parent_dir, tab_absence_evidence=retry_tabs
            )
        if str(drained.get("phase") or "") != "PARENT_FAILED_CLOSED":
            raise BridgeError("USER_STOP_PARENT_DRAIN_PENDING", "target-drift child settled but parent drain remains pending")
        return finalized

    def _try_user_stop_target_drift(
        self, run_dir: str, state_file: Path, record: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Use only two read-only proof rounds; never adopt or close the survivor."""
        stop = record.get("user_stop") if isinstance(record.get("user_stop"), dict) else {}
        existing = stop.get("target_drift_abandonment") if isinstance(stop.get("target_drift_abandonment"), dict) else {}
        if existing:
            finalized = self.store.finalize_user_stop_target_drift(run_dir, abandonment=existing)
            return self._finish_target_drift_parent(state_file, finalized)
        published_path = state_file.parent / "user-stop" / "target-drift-abandonment.json"
        if published_path.exists():
            published = {
                "path": str(published_path),
                "sha256": STATE.sha256_file(published_path),
                "bytes": published_path.stat().st_size,
            }
            finalized = self.store.finalize_user_stop_target_drift(run_dir, abandonment=published)
            return self._finish_target_drift_parent(state_file, finalized)
        owner_observation = self.store._owner_observation(record)
        observed_owner = owner_observation.get("observed") if isinstance(owner_observation.get("observed"), dict) else {}
        if observed_owner.get("alive") is not False:
            return None
        candidate = self.store.user_stop_target_drift_candidate(run_dir)
        recorded = candidate["recorded"]
        session_id = str(recorded.get("session_id") or "")
        stored_target = str(recorded.get("target_id") or "")
        stored_url = str(recorded.get("conversation_url") or "")
        if not all((session_id, stored_target, stored_url)):
            return None
        try:
            manifest = _load_manifest(record)
        except Exception:
            manifest = {}
        executable = record_executable(record)
        env = bridge_env(manifest)
        timeout = int(manifest.get("user_stop_probe_timeout_seconds") or 30)
        attempt = str(time.time_ns())
        commands: list[dict[str, Any]] = []

        def probe(name: str, command: list[str]) -> tuple[subprocess.CompletedProcess[str], Any]:
            completed = self.runner(command, env, timeout)
            evidence = self._evidence(state_file.parent, f"target-drift-{attempt}-{name}", completed)
            payload: Any = None
            for output in (completed.stdout or "", completed.stderr or ""):
                try:
                    payload = json.loads(output)
                    break
                except (json.JSONDecodeError, TypeError):
                    continue
            commands.append({"name": name, "command": command, "evidence": evidence, "payload": sanitize_evidence(payload)})
            return completed, payload

        def canonical(value: Any) -> str:
            try:
                return STATE.canonical_conversation_url(str(value or ""))
            except Exception:
                return ""

        def proof_round(index: int) -> dict[str, Any]:
            tabs_completed, tabs_payload = probe(f"tabs-{index}", [executable, "tabs", "--json"])
            session_completed, session_payload = probe(
                f"session-{index}", [executable, "web-ai", "sessions", "show", session_id, "--json"]
            )
            tabs = tabs_payload.get("tabs") if isinstance(tabs_payload, dict) else tabs_payload
            wrapper = session_payload if isinstance(session_payload, dict) else {}
            session = wrapper.get("session") if isinstance(wrapper.get("session"), dict) else wrapper
            survivor = str(session.get("targetId") or session.get("target_id") or "")
            session_url = str(session.get("conversationUrl") or session.get("conversation_url") or "")
            matching_target = [row for row in tabs or [] if isinstance(row, dict) and str(row.get("targetId") or "") == survivor]
            matching_url = [row for row in tabs or [] if isinstance(row, dict) and canonical(row.get("url")) == canonical(stored_url)]
            stored_live = [row for row in tabs or [] if isinstance(row, dict) and str(row.get("targetId") or "") == stored_target]
            helper_values = [value for value in (wrapper.get("activeCommand"), session.get("activeCommand")) if value not in (None, {})]
            core_valid = bool(
                tabs_completed.returncode == 0 and session_completed.returncode == 0
                and isinstance(tabs, list) and all(isinstance(row, dict) for row in tabs)
                and str(session.get("sessionId") or session.get("session_id") or "") == session_id
                and str(session.get("status") or "").casefold() == "complete"
                and bool(session.get("completedAt") or session.get("completed_at"))
                and bool(str(session.get("answer") or "").strip())
                and survivor and survivor != stored_target
                and canonical(session_url) == canonical(stored_url)
                and not stored_live
                and not helper_values
            )
            live_survivor_valid = bool(
                core_valid and len(matching_target) == 1 and len(matching_url) == 1
                and str(matching_target[0].get("type") or "") == "page"
                and canonical(matching_target[0].get("url")) == canonical(stored_url)
            )
            no_live_target_valid = bool(core_valid and not matching_target and not matching_url)
            return {
                "valid": bool(live_survivor_valid or no_live_target_valid),
                "live_survivor_valid": live_survivor_valid,
                "no_live_target_valid": no_live_target_valid,
                "survivor_target_id": survivor,
                "session_url": session_url,
                "last_streaming_state": session.get("lastStreamingState"),
                "status": session.get("status"),
                "completed_at": session.get("completedAt") or session.get("completed_at"),
                "stored_target_absent": not stored_live,
                "helper_values": sanitize_evidence(helper_values),
                "tabs": sanitize_evidence(tabs),
                "command_refs": [commands[-2]["evidence"], commands[-1]["evidence"]],
            }

        first = proof_round(1)
        drift_observed = bool(first["stored_target_absent"] and first["survivor_target_id"] and first["survivor_target_id"] != stored_target)
        if not drift_observed:
            return None
        second = proof_round(2)
        survivor_id = str(first.get("survivor_target_id") or "")
        live_survivor_mode = bool(first["live_survivor_valid"] and second["live_survivor_valid"])
        no_live_target_mode = bool(first["no_live_target_valid"] and second["no_live_target_valid"])
        if (
            not first["valid"] or not second["valid"]
            or not (live_survivor_mode or no_live_target_mode)
            or second.get("survivor_target_id") != survivor_id
            or canonical(second.get("session_url")) != canonical(stored_url)
            or stored_target not in candidate["historical_owned_target_ids"]
            or survivor_id in candidate["historical_owned_target_ids"]
            or self.store.user_stop_target_drift_candidate(run_dir) != candidate
        ):
            raise BridgeError("USER_STOP_TARGET_DRIFT_UNPROVEN", "target drift is not stable and unowned; no mutation authorized", {"commands": commands})
        descriptor_payload = {
            "schema": "codex.chatgpt.user-stop-target-drift-abandonment/v1",
            "decision": (
                "abandon-without-close"
                if live_survivor_mode
                else "abandon-without-close-no-live-target"
            ),
            "child_identity": candidate["child_identity"],
            "parent_identity": candidate["parent_identity"],
            "lock_identity": candidate["lock_identity"],
            "authorization": candidate["authorization"],
            "authorization_sha256": candidate["authorization_sha256"],
            "stop_epoch_nonce": candidate["stop_epoch_nonce"],
            "parent_stop_scope": candidate["parent_stop_scope"],
            "preimages": candidate["preimages"],
            "recorded": recorded,
            "proof_rounds": [first, second],
            "commands": commands,
            "dead_owner_proof": owner_observation,
            "historical_owned_target_ids": candidate["historical_owned_target_ids"],
            "required_absent_target_ids": sorted(
                {
                    *candidate["historical_owned_target_ids"],
                    *([survivor_id] if no_live_target_mode else []),
                }
            ),
            "historical_target_absence_union": sorted(
                {
                    *candidate["historical_owned_target_ids"],
                    *([survivor_id] if no_live_target_mode else []),
                }
            ),
            "historical_target_absent": True,
            **(
                {"protected_survivor": {"target_id": survivor_id, "conversation_url": stored_url, "ownership_adopted": False, "close_authorized": False, "tab_closed": False, "classification": "unowned-or-foreign-protected"}}
                if live_survivor_mode
                else {"reported_stale_target": {"target_id": survivor_id, "conversation_url": stored_url, "ownership_adopted": False, "close_authorized": False, "tab_closed": False, "proven_absent": True, "classification": "unowned-reported-stale-target-absent"}}
            ),
            "submission_outcome": "unknown",
            "provider_terminal_asserted": False,
            "provider_mutation_may_have_occurred": True,
            "send_authorized": False,
            "retry_authorized": False,
            "recovery_authorized": False,
            "result_capture_authorized": False,
            "result_promotion_authorized": False,
        }
        descriptor = STATE.write_immutable_json_exclusive(
            state_file.parent / "user-stop" / "target-drift-abandonment.json", descriptor_payload
        )
        finalized = self.store.finalize_user_stop_target_drift(run_dir, abandonment=descriptor)
        return self._finish_target_drift_parent(state_file, finalized)

    def confirm_user_stop(
        self,
        run_dir: str,
        *,
        explicit_user_request: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        state_file, record = self.store.load(run_dir)
        if not explicit_user_request:
            raise BridgeError(
                "USER_STOP_AUTHORIZATION_REQUIRED",
                "uncertain post-send work can be abandoned only after an explicit user request",
            )
        if record.get("phase") == "ABANDONED_UNCERTAIN":
            if str(record.get("record_kind") or "") == "child":
                parent_dir = state_file.parent.parent / str(record.get("parent_run_id") or "")
                try:
                    _, parent = self.store.load(parent_dir)
                    paths = self.store.paths(
                        STATE.canonical_project_root(parent["project_root"]), str(parent["run_id"])
                    )
                    if paths.lock_file.exists() and str(parent.get("phase") or "") in {
                        "USER_STOP_REQUESTED", "PARENT_DRAINING", "PARENT_FAILED_CLOSED"
                    }:
                        if str(parent.get("phase") or "") == "USER_STOP_REQUESTED":
                            self._settle_user_stop_send_rejected_siblings(
                                parent_dir=parent_dir,
                                excluded_run_id=str(record.get("run_id") or ""),
                            )
                        final_tabs = self._parent_stop_final_tab_scan(parent_dir)
                        self.store.finalize_user_stopped_parent(
                            parent_dir, tab_absence_evidence=final_tabs
                        )
                except (BridgeError, STATE.StateError):
                    raise
            return record
        if record.get("phase") in {"COMPLETE", "CANCELLED_PRE_SUBMISSION"}:
            raise BridgeError(
                "USER_STOP_PHASE_INVALID",
                f"run is already terminal in phase {record.get('phase')}",
            )

        # A stop request must not enter the heavier abandonment protocol when
        # the exact conversation has already finished. One direct exact-URL
        # adjudication captures the answer, releases the lock, and performs
        # the normal owned-tab cleanup.
        if record.get("conversation_url") and record.get("phase") in {
            "SEND_STARTED",
            "RECOVERY_REQUIRED",
            "RESPONSE_IN_PROGRESS",
            "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
            "BLOCKED_RECOVERY_EXHAUSTED",
        }:
            if record.get("phase") != "RECOVERING":
                self.store.transition(run_dir, "RECOVERING")
            direct = self._try_exact_url_terminal_now(run_dir)
            if direct.get("phase") == "COMPLETE":
                self.cleanup_completed(run_dir, explicit_user_request=False)
                return self.store.load(run_dir)[1]
            state_file, record = self.store.load(run_dir)

        authorization = {
            "schema": "codex.chatgpt.user-stop-authorization/v1",
            "explicit_user_request": True,
            "mutation_may_have_occurred": True,
            "duplicate_risk_acknowledged": True,
            "reason": str(reason or "confirm explicit user stop").strip(),
            "run_id": record.get("run_id"),
            "project_root": record.get("project_root"),
            "session_id": record.get("session_id"),
            "target_id": record.get("current_target_id"),
            "conversation_url": record.get("conversation_url"),
        }
        if record.get("phase") != "USER_STOP_REQUESTED":
            record = self.store.begin_user_stop(run_dir, authorization=authorization)
        elif str(record.get("record_kind") or "") == "child":
            legacy_parent_dir = state_file.parent.parent / str(record.get("parent_run_id") or "")
            try:
                _, legacy_parent = self.store.load(legacy_parent_dir)
            except Exception:
                legacy_parent = {}
            if str(legacy_parent.get("phase") or "") == "PARENT_ACTIVE":
                self.store.adopt_legacy_user_stop(legacy_parent_dir)
                _, record = self.store.load(run_dir)
        if str(record.get("record_kind") or "") == "child":
            parent_dir_for_scope = state_file.parent.parent / str(
                record.get("parent_run_id") or ""
            )
            _, parent_for_scope = self.store.load(parent_dir_for_scope)
            stop_epoch = str(
                (parent_for_scope.get("user_stop_requests") or {})
                .get(str(record.get("run_id") or ""), {})
                .get("stop_epoch_nonce")
                or (record.get("user_stop") or {}).get("stop_epoch_nonce")
                or ""
            )
            if not isinstance(parent_for_scope.get("parent_stop_scope"), dict):
                manager_authorization = {
                    "schema": "codex.chatgpt.parent-wide-manager-authorization/v1",
                    "authorization_id": STATE.sha256_bytes(
                        f"{parent_for_scope.get('run_id')}:{stop_epoch}".encode("utf-8")
                    ),
                    "issued_at": str(parent_for_scope.get("phase_at") or record.get("phase_at") or record.get("created_at") or ""),
                    "explicit_user_request": True,
                    "scope_kind": "exact-parent-and-listed-children",
                    "parent_run_id": parent_for_scope.get("run_id"),
                    "target_child_run_id": record.get("run_id"),
                    "reason": str(reason or "confirm explicit user stop").strip(),
                }
                self.store.establish_parent_wide_user_stop_scope(
                    parent_dir_for_scope,
                    manager_authorization=manager_authorization,
                    target_child_run_id=str(record.get("run_id") or ""),
                )
            _, record = self.store.load(run_dir)
        if (
            str(record.get("record_kind") or "") == "child"
            and (state_file.parent / "send.claim").is_file()
            and self.store._send_claim_proof(state_file, record) is None
        ):
            record = self.store.adopt_legacy_send_claim_for_user_stop(run_dir)
        if str(record.get("record_kind") or "") == "child":
            drift_finalized = self._try_user_stop_target_drift(run_dir, state_file, record)
            if drift_finalized is not None:
                return drift_finalized

        try:
            manifest = _load_manifest(record)
        except Exception:
            manifest = {}
        env = bridge_env(manifest)
        executable = record_executable(record)
        timeout = int(manifest.get("user_stop_probe_timeout_seconds") or 30)
        attempt = str(time.time_ns())
        session_id = str(record.get("session_id") or "")
        stored_target_id = str(record.get("current_target_id") or "") or None
        stored_url = str(record.get("conversation_url") or "") or None
        commands: list[dict[str, Any]] = []

        def run_probe(name: str, command: list[str]) -> tuple[subprocess.CompletedProcess[str], Any]:
            completed = self.runner(command, env, timeout)
            descriptor = self._evidence(state_file.parent, f"user-stop-{attempt}-{name}", completed)
            payload: Any = None
            for output in (completed.stdout or "", completed.stderr or ""):
                if not output.strip():
                    continue
                try:
                    payload = json.loads(output)
                    break
                except json.JSONDecodeError:
                    continue
            commands.append(
                {
                    "name": name,
                    "command": [command[0], *command[1:]],
                    "evidence": descriptor,
                    "payload": sanitize_evidence(payload),
                }
            )
            return completed, payload

        tabs_payload: Any = []
        tabs_probe_ok = False
        try:
            tabs_completed, tabs_payload = run_probe("tabs-before", [executable, "tabs", "--json"])
            tabs_probe_ok = tabs_completed.returncode == 0 and isinstance(tabs_payload, list)
        except Exception as exc:
            commands.append({"name": "tabs-before", "exception": _redact_sensitive_text(str(exc))})
        tabs = tabs_payload if isinstance(tabs_payload, list) else []

        session_payload: Any = None
        session: dict[str, Any] = {}
        session_probe_completed = False
        session_missing_proven = False
        if session_id:
            try:
                session_completed, session_payload = run_probe(
                    "session-before",
                    [executable, "web-ai", "sessions", "show", session_id, "--json"],
                )
                session_probe_completed = True
                if session_completed.returncode != 0 and isinstance(session_payload, dict):
                    error = session_payload.get("error")
                    message = str(error.get("message") if isinstance(error, dict) else error or "").casefold()
                    session_missing_proven = "no session record" in message
            except Exception as exc:
                commands.append({"name": "session-before", "exception": _redact_sensitive_text(str(exc))})
            if isinstance(session_payload, dict):
                candidate = session_payload.get("session")
                session = candidate if isinstance(candidate, dict) else session_payload

        observed_session_id = str(session.get("sessionId") or session.get("session_id") or "") or None
        observed_target_id = str(session.get("targetId") or session.get("target_id") or session.get("tabId") or "") or None
        observed_url = str(
            session.get("conversationUrl") or session.get("conversation_url") or session.get("originalUrl") or ""
        ) or None
        session_status = str(session.get("status") or "").casefold()
        terminal_statuses = {"complete", "completed", "done", "cancelled", "canceled", "stopped", "aborted"}
        active_statuses = {"created", "sent", "polling", "running", "pending", "response_in_progress", "streaming"}

        def canonical_exact(candidate: str | None, expected: str | None) -> bool:
            if not candidate or not expected:
                return False
            try:
                return STATE.canonical_conversation_url(candidate) == STATE.canonical_conversation_url(expected)
            except Exception:
                return False

        def helper_check(payload: Any, *, source: str) -> dict[str, Any]:
            container = payload if isinstance(payload, dict) else {}
            nested = container.get("session") if isinstance(container.get("session"), dict) else {}
            values = [
                value
                for value in (container.get("activeCommand"), nested.get("activeCommand"))
                if value not in (None, {})
            ]
            if not values:
                return {"source": source, "present": False, "valid": True}
            if len(values) != 1 or not isinstance(values[0], dict):
                return {"source": source, "present": True, "valid": False, "reason": "active-command-ambiguous"}
            command = values[0]
            helper_session = str(command.get("sessionId") or command.get("session_id") or "")
            helper_target = str(command.get("targetId") or command.get("target_id") or command.get("tabId") or "")
            helper_url = str(command.get("conversationUrl") or command.get("conversation_url") or command.get("url") or "")
            valid = bool(
                helper_session == session_id
                and (not helper_target or helper_target == stored_target_id)
                and (not helper_url or canonical_exact(helper_url, stored_url))
            )
            return {
                "source": source,
                "present": True,
                "valid": valid,
                "session_id": helper_session or None,
                "target_id": helper_target or None,
                "conversation_url": helper_url or None,
                "reason": None if valid else "active-command-identity-mismatch",
            }

        helper_checks = [helper_check(session_payload, source="session-before")]
        exact_session_identity = bool(
            session_id
            and stored_target_id
            and stored_url
            and observed_session_id == session_id
            and observed_target_id == stored_target_id
            and canonical_exact(observed_url, stored_url)
        )
        exact_session_identity = bool(exact_session_identity and helper_checks[-1]["valid"])
        terminal_session = bool(
            exact_session_identity
            and session_status in terminal_statuses
            and helper_checks[-1]["present"] is False
        )

        effective_target_id = stored_target_id or observed_target_id
        matching_tabs = [row for row in tabs if isinstance(row, dict) and str(row.get("targetId") or "") == effective_target_id]
        exact_target_live = len(matching_tabs) == 1
        exact_target_url = str(matching_tabs[0].get("url") or "") if exact_target_live else None
        identity_match = exact_session_identity
        if len(matching_tabs) > 1:
            identity_match = False
        if exact_target_live and observed_url and exact_target_url != observed_url:
            identity_match = False
        if exact_target_live and stored_url and exact_target_url != stored_url:
            identity_match = False

        if session_id and identity_match and (exact_target_live or not terminal_session):
            try:
                run_probe(
                    "stop",
                    [executable, "web-ai", "stop", "--vendor", "chatgpt", "--session", session_id, "--json"],
                )
                poll_command = [
                    executable,
                    "web-ai",
                    "poll",
                    "--vendor",
                    "chatgpt",
                    "--session",
                    session_id,
                    "--timeout",
                    "5",
                    "--json",
                ]
                if stored_url:
                    poll_command.insert(-1, "--navigate")
                _, poll_payload = run_probe("poll-after-stop", poll_command)
                if isinstance(poll_payload, dict):
                    poll_status = str(poll_payload.get("status") or "").casefold()
                    poll_session_id = str(poll_payload.get("sessionId") or poll_payload.get("session_id") or "")
                    poll_target_id = str(
                        poll_payload.get("targetId") or poll_payload.get("target_id") or poll_payload.get("tabId") or ""
                    )
                    poll_url = str(
                        poll_payload.get("conversationUrl")
                        or poll_payload.get("conversation_url")
                        or poll_payload.get("originalUrl")
                        or ""
                    )
                    poll_helper = helper_check(poll_payload, source="poll-after-stop")
                    helper_checks.append(poll_helper)
                    poll_identity = bool(
                        poll_session_id == session_id
                        and poll_target_id == stored_target_id
                        and canonical_exact(poll_url, stored_url)
                    )
                    identity_match = bool(identity_match and poll_identity and poll_helper["valid"])
                    terminal_session = bool(
                        identity_match
                        and poll_status in terminal_statuses
                        and poll_helper["present"] is False
                    )
                    if poll_status:
                        session_status = poll_status
                else:
                    helper_checks.append(
                        {"source": "poll-after-stop", "present": False, "valid": False, "reason": "payload-ambiguous"}
                    )
                    identity_match = False
                    terminal_session = False
            except Exception as exc:
                commands.append({"name": "stop-or-poll", "exception": _redact_sensitive_text(str(exc))})
                helper_checks.append(
                    {"source": "poll-after-stop", "present": False, "valid": False, "reason": "probe-failed"}
                )
                identity_match = False
                terminal_session = False
            try:
                _, after_payload = run_probe(
                    "session-after",
                    [executable, "web-ai", "sessions", "show", session_id, "--json"],
                )
                if isinstance(after_payload, dict):
                    after_session = after_payload.get("session")
                    after_session = after_session if isinstance(after_session, dict) else after_payload
                    after_id = str(after_session.get("sessionId") or after_session.get("session_id") or "")
                    after_target = str(
                        after_session.get("targetId") or after_session.get("target_id") or after_session.get("tabId") or ""
                    )
                    after_url = str(
                        after_session.get("conversationUrl")
                        or after_session.get("conversation_url")
                        or after_session.get("originalUrl")
                        or ""
                    )
                    after_status = str(after_session.get("status") or "").casefold()
                    after_helper = helper_check(after_payload, source="session-after")
                    helper_checks.append(after_helper)
                    after_identity = bool(
                        after_id == session_id
                        and after_target == stored_target_id
                        and canonical_exact(after_url, stored_url)
                    )
                    identity_match = bool(identity_match and after_identity and after_helper["valid"])
                    terminal_session = bool(
                        identity_match
                        and after_status in terminal_statuses
                        and after_helper["present"] is False
                    )
                    if after_status:
                        session_status = after_status
                else:
                    helper_checks.append(
                        {"source": "session-after", "present": False, "valid": False, "reason": "payload-ambiguous"}
                    )
                    identity_match = False
                    terminal_session = False
            except Exception as exc:
                commands.append({"name": "session-after", "exception": _redact_sensitive_text(str(exc))})
                helper_checks.append(
                    {"source": "session-after", "present": False, "valid": False, "reason": "probe-failed"}
                )
                identity_match = False
                terminal_session = False

        owner_observation = self.store._owner_observation(record)
        target_absent = not exact_target_live
        identity_missing_owner_dead = bool(
            not identity_match
            and not owner_observation.get("same_process")
            and target_absent
            and tabs_probe_ok
            and (
                not session_id
                or (session_probe_completed and session_missing_proven)
            )
        )
        generation_active = bool(
            session_status in active_statuses
            or (session.get("lastStreamingState") in {True, "true", "streaming", "active"})
            or (exact_target_live and not terminal_session)
        )
        if terminal_session:
            generation_active = False
        helper_active_or_invalid = any(
            check.get("present") is True or check.get("valid") is not True for check in helper_checks
        )
        if helper_active_or_invalid:
            generation_active = True
        classification = {
            "identity_match": bool(identity_match or identity_missing_owner_dead),
            "identity_missing_owner_dead": identity_missing_owner_dead,
            "terminal_session": terminal_session,
            "generation_active": generation_active,
            "session_status": session_status or None,
            "observed_session_id": observed_session_id,
            "observed_target_id": observed_target_id,
            "observed_url": observed_url,
            "exact_target_live": exact_target_live,
            "exact_target_url": exact_target_url,
            "owner_observation": owner_observation,
            "helper_checks": helper_checks,
        }
        evidence_payload = {
            "schema": "codex.chatgpt.user-stop-evidence/v1",
            "run_id": record.get("run_id"),
            "session_id": record.get("session_id"),
            "target_id": record.get("current_target_id"),
            "conversation_url": record.get("conversation_url"),
            "authorization_sha256": (record.get("user_stop") or {}).get("authorization_sha256"),
            "challenge_nonce": (record.get("user_stop") or {}).get("challenge_nonce"),
            "mutation_may_have_occurred": True,
            "tab_closed": False,
            "classification": sanitize_evidence(classification),
            "commands": commands,
        }
        evidence_dir = state_file.parent / "agbrowse-evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_dir / f"user-stop-{attempt}.json"
        write_json_atomic(evidence_path, evidence_payload)
        confirmation = {
            "path": str(evidence_path),
            "sha256": STATE.sha256_file(evidence_path),
            "bytes": evidence_path.stat().st_size,
        }
        if classification["generation_active"] or not classification["identity_match"] or not (
            classification["terminal_session"] or classification["identity_missing_owner_dead"]
        ):
            raise BridgeError(
                "USER_STOP_CONFIRMATION_REQUIRED",
                "the exact run is still active or its terminal identity is not yet proven; project lock retained",
                {"confirmation": confirmation, "classification": classification},
            )
        if str(record.get("record_kind") or "") != "child":
            return self.store.finalize_user_stop(run_dir, confirmation=confirmation)
        pre_adjudication = STATE.write_immutable_json_exclusive(
            state_file.parent / "user-stop" / f"adjudication-{attempt}-preclose.json",
            {
                "schema": "codex.chatgpt.user-stop-adjudication/v2",
                "run_id": record.get("run_id"),
                "parent_run_id": record.get("parent_run_id"),
                "session_id": record.get("session_id"),
                "target_id": record.get("current_target_id"),
                "conversation_url": record.get("conversation_url"),
                "authorization_sha256": (record.get("user_stop") or {}).get("authorization_sha256"),
                "stop_epoch_nonce": (record.get("user_stop") or {}).get("stop_epoch_nonce"),
                "terminal": True,
                "classification": sanitize_evidence(classification),
                "commands": commands,
                "cleanup_required": True,
            },
        )
        _, stopped_record = self.store.load(run_dir)
        stopped_record.setdefault("user_stop", {})["pending_adjudication"] = pre_adjudication
        stopped_record["updated_at"] = STATE.utc_now()
        STATE.write_json_atomic(state_file, stopped_record)
        lifecycle = self._tab_lifecycle(executable, manifest)
        cleanup = lifecycle.close_user_stopped(run_dir)
        cleanup = {**cleanup, "target_id": str(record.get("current_target_id") or ""), "conversation_url": str(record.get("conversation_url") or "")}
        terminal_evidence = {
            "schema": "codex.chatgpt.user-stop-adjudication/v2",
            "run_id": record.get("run_id"), "parent_run_id": record.get("parent_run_id"),
            "session_id": record.get("session_id"), "target_id": record.get("current_target_id"),
            "conversation_url": record.get("conversation_url"),
            "authorization_sha256": (record.get("user_stop") or {}).get("authorization_sha256"),
            "stop_epoch_nonce": (record.get("user_stop") or {}).get("stop_epoch_nonce"),
            "terminal": True, "classification": sanitize_evidence(classification),
            "commands": commands, "cleanup": cleanup,
        }
        immutable = STATE.write_immutable_json_exclusive(
            state_file.parent / "user-stop" / f"adjudication-{attempt}.json", terminal_evidence
        )
        finalized = self.store.finalize_user_stop(run_dir, confirmation=immutable)
        parent_dir = state_file.parent.parent / str(finalized.get("parent_run_id") or "")
        self._settle_user_stop_send_rejected_siblings(
            parent_dir=parent_dir,
            excluded_run_id=str(finalized.get("run_id") or ""),
        )
        final_tabs = self._parent_stop_final_tab_scan(parent_dir)
        drained = self.store.finalize_user_stopped_parent(
            str(parent_dir), tab_absence_evidence=final_tabs
        )
        if str(drained.get("phase") or "") != "PARENT_FAILED_CLOSED":
            raise BridgeError(
                "USER_STOP_PARENT_DRAIN_PENDING",
                "target child is settled but another exact child still blocks parent drain",
                {"parent_run_dir": str(parent_dir), "child_run_id": finalized.get("run_id"), "child_scan": drained.get("child_scan")},
            )
        return finalized

    def _parent_stop_final_tab_scan(self, parent_dir: Path) -> dict[str, Any]:
        parent_file, parent = self.store.load(parent_dir)
        paths = self.store.paths(
            STATE.canonical_project_root(parent["project_root"]), str(parent["run_id"])
        )
        lock = STATE.read_json(paths.lock_file)
        children = self.store._strict_parent_children(paths, parent)
        known_targets = set(self.store.parent_historical_owned_target_ids(paths, parent))
        boundary_urls: set[str] = set()
        def canonical_boundary_url(value: Any) -> str:
            try:
                return STATE.canonical_conversation_url(str(value or ""))
            except Exception:
                return str(value or "")
        protected_survivors: list[dict[str, Any]] = []
        for child_file, child in children:
            if child.get("conversation_url"):
                boundary_urls.add(canonical_boundary_url(child.get("conversation_url")))
            for event in child.get("target_rebind_events") or []:
                if isinstance(event, dict):
                    for key in ("conversation_url", "old_conversation_url", "new_conversation_url", "url"):
                        if event.get(key):
                            boundary_urls.add(canonical_boundary_url(event.get(key)))
            for evidence_name in ("tab-lifecycle.json", "composer-app-evidence.json"):
                evidence_path = child_file.parent / evidence_name
                if evidence_path.is_file() and not evidence_path.is_symlink():
                    evidence = STATE.read_json(evidence_path)
                    values = evidence.get("events") if isinstance(evidence.get("events"), list) else [evidence]
                    for value in values:
                        if isinstance(value, dict) and value.get("url"):
                            boundary_urls.add(canonical_boundary_url(value.get("url")))
            stop = child.get("user_stop") if isinstance(child.get("user_stop"), dict) else {}
            drift_ref = stop.get("target_drift_abandonment") if isinstance(stop.get("target_drift_abandonment"), dict) else {}
            if drift_ref:
                drift_path = Path(str(drift_ref.get("path") or ""))
                if (
                    not drift_path.is_file() or drift_path.is_symlink()
                    or STATE.sha256_file(drift_path) != str(drift_ref.get("sha256") or "")
                ):
                    raise BridgeError("PARENT_STOP_PROTECTED_SURVIVOR_INVALID", "protected survivor descriptor is invalid")
                drift = STATE.read_json(drift_path)
                recorded = drift.get("recorded") if isinstance(drift.get("recorded"), dict) else {}
                if recorded.get("conversation_url"):
                    boundary_urls.add(canonical_boundary_url(recorded.get("conversation_url")))
                required = drift.get("required_absent_target_ids")
                if not isinstance(required, list) or not all(isinstance(item, str) and item for item in required):
                    raise BridgeError("PARENT_STOP_REQUIRED_ABSENCE_INVALID", "target-drift absence set is invalid")
                historical = set(known_targets)
                survivor = drift.get("protected_survivor")
                if isinstance(survivor, dict):
                    if str(survivor.get("target_id") or "") in historical:
                        raise BridgeError("PARENT_STOP_PROTECTED_SURVIVOR_OWNED", "survivor appears in old-parent ownership")
                    protected_survivors.append(survivor)
                    boundary_urls.add(canonical_boundary_url(survivor.get("conversation_url")))
                stale = drift.get("reported_stale_target")
                if isinstance(stale, dict):
                    boundary_urls.add(canonical_boundary_url(stale.get("conversation_url")))
                known_targets.update(required)
        exemplar = children[0][1]
        try:
            manifest = _load_manifest(exemplar)
        except Exception:
            manifest = {}
        executable = record_executable(exemplar)
        completed = self.runner(
            [executable, "tabs", "--json"],
            bridge_env(manifest),
            int(manifest.get("tab_cleanup_timeout_seconds") or 30),
        )
        if completed.returncode != 0:
            raise BridgeError("PARENT_STOP_FINAL_TAB_SCAN_FAILED", "final tabs enumeration failed")
        try:
            payload = json.loads(completed.stdout or "")
        except json.JSONDecodeError as exc:
            raise BridgeError("PARENT_STOP_FINAL_TAB_SCAN_INVALID", "final tabs JSON is invalid") from exc
        tabs = payload.get("tabs") if isinstance(payload, dict) else payload
        if not isinstance(tabs, list) or not all(isinstance(item, dict) for item in tabs):
            raise BridgeError("PARENT_STOP_FINAL_TAB_SCAN_INVALID", "final tabs payload is incomplete")
        tab_ids = [_tab_id(tab) for tab in tabs]
        if any(not target_id for target_id in tab_ids) or len(set(tab_ids)) != len(tab_ids):
            raise BridgeError("PARENT_STOP_FINAL_TAB_SCAN_INVALID", "final tabs contain missing or duplicate target identities")
        live_ids = set(tab_ids)
        absent = sorted(known_targets - live_ids)
        if len(absent) != len(known_targets):
            raise BridgeError(
                "PARENT_STOP_KNOWN_TARGET_LIVE",
                "an old-workflow owned target remains live",
                {"live_known_target_ids": sorted(known_targets & live_ids)},
            )
        raw_stdout = (completed.stdout or "").encode("utf-8")
        raw_stderr = (completed.stderr or "").encode("utf-8")
        scan_payload = {
            "schema": "codex.chatgpt.parent-stop-final-tab-scan/v1",
            "parent_run_id": parent.get("run_id"),
            "parent_stop_scope": parent.get("parent_stop_scope"),
            "stop_epoch_nonce": str((parent.get("parent_stop_scope") or {}).get("stop_epoch_nonce") or ""),
            "known_target_ids": sorted(known_targets),
            "all_known_targets_absent": True,
            "protected_survivors": protected_survivors,
            "normalized_tabs": tabs,
            "tabs_sha256": STATE.sha256_bytes(
                json.dumps(tabs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ),
            "command": [executable, "tabs", "--json"],
            "exit_code": completed.returncode,
            "stdout_sha256": STATE.sha256_bytes(raw_stdout),
            "stdout_bytes": len(raw_stdout),
            "stdout_base64": base64.b64encode(raw_stdout).decode("ascii"),
            "stderr_sha256": STATE.sha256_bytes(raw_stderr),
            "stderr_bytes": len(raw_stderr),
            "stderr_base64": base64.b64encode(raw_stderr).decode("ascii"),
            "scanned_at": STATE.utc_now(),
        }
        scan_path = parent_file.parent / "user-stop" / "final-tab-scan.json"
        if scan_path.exists():
            retry_paths = sorted(scan_path.parent.glob("final-tab-scan-retry-*.json"))
            chain_paths = [scan_path, *retry_paths]
            previous_descriptor: dict[str, Any] | None = None
            existing: dict[str, Any] = {}
            for index, chain_path in enumerate(chain_paths):
                existing = STATE.read_json(chain_path)
                descriptor = {"path": str(chain_path), "sha256": STATE.sha256_file(chain_path), "bytes": chain_path.stat().st_size}
                if index == 0:
                    if existing.get("schema") != "codex.chatgpt.parent-stop-final-tab-scan/v1":
                        raise BridgeError("PARENT_STOP_FINAL_TAB_SCAN_CONFLICT", "base final tab scan is invalid")
                elif (
                    existing.get("schema") != "codex.chatgpt.parent-stop-final-tab-scan/v2"
                    or existing.get("previous_scan") != previous_descriptor
                ):
                    raise BridgeError("PARENT_STOP_FINAL_TAB_SCAN_CONFLICT", "final tab scan retry chain is invalid")
                if (
                    existing.get("parent_stop_scope") != parent.get("parent_stop_scope")
                    or str(existing.get("stop_epoch_nonce") or (existing.get("parent_stop_scope") or {}).get("stop_epoch_nonce") or "")
                    != str((parent.get("parent_stop_scope") or {}).get("stop_epoch_nonce") or "")
                    or list(existing.get("command") or [])[1:] != ["tabs", "--json"]
                    or (
                        existing.get("schema") == "codex.chatgpt.parent-stop-final-tab-scan/v2"
                        and (existing.get("external_inventory_drift") or {}).get("mutation_commands") != []
                    )
                ):
                    raise BridgeError("PARENT_STOP_FINAL_TAB_SCAN_CONFLICT", "final tab scan authority or command history is invalid")
                previous_descriptor = descriptor
            if (
                existing.get("parent_run_id") != scan_payload["parent_run_id"]
                or existing.get("parent_stop_scope") != scan_payload["parent_stop_scope"]
                or existing.get("known_target_ids") != scan_payload["known_target_ids"]
                or existing.get("protected_survivors", []) != scan_payload["protected_survivors"]
                or existing.get("all_known_targets_absent") is not True
            ):
                raise BridgeError(
                    "PARENT_STOP_FINAL_TAB_SCAN_CONFLICT",
                    "final tab ownership boundary changed across retry",
                )
            def row_key(row: dict[str, Any]) -> str:
                return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            old_rows = {row_key(row): row for row in existing.get("normalized_tabs") or [] if isinstance(row, dict)}
            new_rows = {row_key(row): row for row in tabs}
            if set(old_rows) == set(new_rows):
                return previous_descriptor or {}
            parent_phase = str(parent.get("phase") or "")
            pre_drain_allowed = False
            if parent_phase == "USER_STOP_REQUESTED":
                scope = parent.get("parent_stop_scope") if isinstance(parent.get("parent_stop_scope"), dict) else {}
                stop_epoch = str(scope.get("stop_epoch_nonce") or "")
                target_abandoned = any(
                    str(child.get("phase") or "") == "ABANDONED_UNCERTAIN"
                    and isinstance((child.get("user_stop") or {}).get("target_drift_abandonment"), dict)
                    for _, child in children
                )
                parent_scan_files = [
                    *scan_path.parent.glob("parent-scan.json"),
                    *scan_path.parent.glob("parent-scan-retry-*.json"),
                ]
                read_only_scan = self.store.finalize_user_stopped_parent(parent_dir, dry_run=True)
                pre_drain_allowed = bool(
                    str(lock.get("phase") or "") == "USER_STOP_REQUESTED"
                    and parent.get("parent_stop_scope") == lock.get("parent_stop_scope")
                    and stop_epoch
                    and str(lock.get("stop_epoch_nonce") or "") == stop_epoch
                    and parent.get("user_stop_scan") in (None, {})
                    and lock.get("user_stop_scan") in (None, {})
                    and not parent_scan_files
                    and target_abandoned
                    and read_only_scan.get("strict_terminal_scan_ready") is True
                )
            if parent_phase != "PARENT_DRAINING" and not pre_drain_allowed:
                raise BridgeError("PARENT_STOP_FINAL_TAB_SCAN_CONFLICT", "foreign tab drift is retryable only while parent is draining")
            changed = [old_rows[key] for key in sorted(set(old_rows) - set(new_rows))] + [new_rows[key] for key in sorted(set(new_rows) - set(old_rows))]
            protected_ids = {str(item.get("target_id") or "") for item in protected_survivors}
            forbidden_ids = known_targets | protected_ids
            if any(
                _tab_id(row) in forbidden_ids
                or canonical_boundary_url(row.get("url")) in boundary_urls
                for row in changed
            ):
                raise BridgeError("PARENT_STOP_FINAL_TAB_SCAN_CONFLICT", "tab drift overlaps owned, required-absent, or protected identity")
            retry_payload = {
                **scan_payload,
                "schema": "codex.chatgpt.parent-stop-final-tab-scan/v2",
                "previous_scan": previous_descriptor,
                "external_inventory_drift": {
                    "classification": (
                        "foreign-unowned-tab-inventory-drift-pre-drain"
                        if pre_drain_allowed
                        else "foreign-unowned-tab-inventory-drift"
                    ),
                    "added": [new_rows[key] for key in sorted(set(new_rows) - set(old_rows))],
                    "removed": [old_rows[key] for key in sorted(set(old_rows) - set(new_rows))],
                    "mutation_commands": [],
                    "ownership_adopted": False,
                    "close_authorized": False,
                    "prior_scan_attached_or_consumed": False if pre_drain_allowed else None,
                },
            }
            retry_path = scan_path.parent / f"final-tab-scan-retry-{len(retry_paths) + 1:03d}.json"
            return STATE.write_immutable_json_exclusive(retry_path, retry_payload)
        return STATE.write_immutable_json_exclusive(scan_path, scan_payload)

    def _settle_user_stop_send_rejected_siblings(
        self,
        *,
        parent_dir: Path,
        excluded_run_id: str,
    ) -> list[dict[str, Any]]:
        """Settle exact zero-provider siblings without send, retry, or recovery."""
        parent_file, parent = self.store.load(parent_dir)
        paths = self.store.paths(
            STATE.canonical_project_root(parent["project_root"]),
            str(parent["run_id"]),
        )
        outcomes: list[dict[str, Any]] = []
        for child_file, child in self.store._strict_parent_children(paths, parent):
            if (
                str(child.get("run_id") or "") == excluded_run_id
                or str(child.get("phase") or "") != "SEND_REJECTED"
            ):
                continue
            if self.store._send_rejected_zero_provider_settled(child_file, child):
                outcomes.append(
                    {
                        "run_id": child.get("run_id"),
                        "settled": True,
                        "classification": "zero-provider-already-settled",
                    }
                )
                continue
            child_dir = str(child_file.parent)
            try:
                if (
                    self.store._send_rejected_failure_evidence_proof(child_file, child)
                    is not None
                    and self.store._legacy_send_claim_adoption_proof(child_file, child)
                    is None
                ):
                    child = self.store.adopt_legacy_send_claim_for_user_stop(child_dir)
                try:
                    proof = self.store.user_stop_send_rejected_candidate(child_dir)
                except Exception:
                    if self.store._send_rejected_failure_evidence_proof(child_file, child) is not None:
                        child = self.store.adopt_legacy_send_claim_for_user_stop(child_dir)
                        proof = self.store.user_stop_send_rejected_candidate(child_dir)
                    else:
                        settled = self._settle_parent_stop_submission_uncertain_sibling(
                            child_dir=child_dir,
                            child_file=child_file,
                            child=child,
                        )
                        outcomes.append(
                            {
                                "run_id": settled.get("run_id"),
                                "settled": True,
                                "classification": "submission-uncertain",
                            }
                        )
                        continue
            except Exception as exc:
                outcomes.append(
                    {
                        "run_id": child.get("run_id"),
                        "settled": False,
                        "error_code": str(getattr(exc, "code", type(exc).__name__)),
                    }
                )
                continue
            target_id = str(child.get("current_target_id") or "")
            if not target_id:
                outcomes.append(
                    {
                        "run_id": child.get("run_id"),
                        "settled": False,
                        "error_code": "SEND_REJECTED_TARGET_ID_MISSING",
                    }
                )
                continue
            try:
                manifest = _load_manifest(child)
            except Exception:
                manifest = {}
            lifecycle = self._tab_lifecycle(record_executable(child), manifest)
            cleanup = self._safe_tab_cleanup(
                lifecycle,
                child_dir,
                target_id=target_id,
                url=str(child.get("conversation_url") or "https://chatgpt.com/"),
                reason="explicit-user-stop-zero-provider-sibling",
            )
            if cleanup.get("ok") is not True:
                try:
                    self.store.record_child_cleanup(child_dir, cleanup)
                except Exception:
                    pass
                outcomes.append(
                    {
                        "run_id": child.get("run_id"),
                        "settled": False,
                        "error_code": cleanup.get("error_code") or "SEND_REJECTED_CLEANUP_UNPROVEN",
                    }
                )
                continue
            try:
                settled = self.store.settle_user_stop_send_rejected(
                    child_dir,
                    cleanup=cleanup,
                )
                outcomes.append(
                    {
                        "run_id": settled.get("run_id"),
                        "settled": True,
                        "proof_sha256": STATE.sha256_bytes(
                            json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        ),
                    }
                )
            except Exception as exc:
                outcomes.append(
                    {
                        "run_id": child.get("run_id"),
                        "settled": False,
                        "error_code": str(getattr(exc, "code", type(exc).__name__)),
                    }
                )
        return outcomes

    def _settle_parent_stop_submission_uncertain_sibling(
        self, *, child_dir: str, child_file: Path, child: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            manifest = _load_manifest(child)
        except Exception:
            manifest = {}
        lifecycle = self._tab_lifecycle(record_executable(child), manifest)
        evidence_dir = child_file.parent / "user-stop"
        preclose_path = evidence_dir / "parent-stop-submission-uncertain-preclose.json"
        if preclose_path.exists():
            preclose = {
                "path": str(preclose_path),
                "sha256": STATE.sha256_file(preclose_path),
                "bytes": preclose_path.stat().st_size,
            }
            preclose_payload = STATE.read_json(preclose_path)
            candidate = preclose_payload.get("candidate")
            live_candidate = self.store.parent_stop_submission_uncertain_candidate(
                child_dir
            )
            stable_candidate = dict(candidate or {})
            stable_live = dict(live_candidate)
            stable_candidate.pop("source_state", None)
            stable_live.pop("source_state", None)
            if (
                preclose_payload.get("schema")
                != "codex.chatgpt.parent-stop-submission-uncertain-preclose/v1"
                or not isinstance(candidate, dict)
                or stable_candidate != stable_live
            ):
                raise BridgeError(
                    "PARENT_STOP_UNCERTAIN_PRECLOSE_CONFLICT",
                    "existing pre-close adjudication differs from current immutable evidence",
                )
        else:
            candidate = self.store.parent_stop_submission_uncertain_candidate(child_dir)
            inspection = lifecycle.inspect_parent_stopped_submission_uncertain(child_dir)
            implementation = {}
            for name in (
                "chatgpt_agbrowse_bridge.py",
                "chatgpt_agbrowse_state.py",
                "chatgpt_agbrowse_tabs.py",
            ):
                path = Path(__file__).resolve().with_name(name)
                implementation[name] = {
                    "path": str(path),
                    "sha256": STATE.sha256_file(path),
                    "bytes": path.stat().st_size,
                }
            preclose_payload = {
                "schema": "codex.chatgpt.parent-stop-submission-uncertain-preclose/v1",
                "descriptor_nonce": uuid.uuid4().hex,
                "created_at": STATE.utc_now(),
                "decision": "abandon-under-parent-wide-user-stop",
                "source_phase": "SEND_REJECTED",
                "submission_outcome": "unknown",
                "provider_mutation_may_have_occurred": True,
                "zero_provider_asserted": False,
                "pre_submit_asserted": False,
                "send_authorized": False,
                "retry_authorized": False,
                "recovery_authorized": False,
                "result_capture_authorized": False,
                "result_promotion_authorized": False,
                "candidate": candidate,
                "parent_stop_scope": candidate["parent_stop_scope"],
                "parent_identity": candidate["parent_identity"],
                "parent_child_entry": candidate["parent_child_entry"],
                "child_identity": candidate["child_identity"],
                "claim": candidate["claim"],
                "source_state": candidate["source_state"],
                "stderr_discrepancy": candidate["stderr_discrepancy"],
                "stdout_discrepancy": candidate["stdout_discrepancy"],
                "ownership": inspection["ownership"],
                "tabs_before": inspection["tabs_before"],
                "observed_target": {
                    "target_id": inspection["target_id"],
                    "url": inspection["observed_target_url"],
                    "match_count": inspection["target_match_count"],
                },
                "foreign_owner_scan": inspection["foreign_owner_scan"],
                "implementation": implementation,
            }
            if self.store.parent_stop_submission_uncertain_candidate(child_dir) != candidate:
                raise BridgeError(
                    "PARENT_STOP_UNCERTAIN_SOURCE_CHANGED",
                    "source state or evidence changed before pre-close publication",
                )
            preclose = STATE.write_immutable_json_exclusive(
                preclose_path, preclose_payload
            )
        self.store.attach_parent_stop_submission_uncertain_preclose(
            child_dir, preclose=preclose
        )
        settlement_path = evidence_dir / "parent-stop-submission-uncertain-settlement.json"
        if settlement_path.exists():
            settlement = {
                "path": str(settlement_path),
                "sha256": STATE.sha256_file(settlement_path),
                "bytes": settlement_path.stat().st_size,
            }
        else:
            cleanup = lifecycle.close_parent_stopped_submission_uncertain(
                child_dir, preclose=preclose
            )
            _, current = self.store.load(child_dir)
            settlement_payload = {
                "schema": "codex.chatgpt.parent-stop-submission-uncertain-settlement/v1",
                "preclose": preclose,
                "parent_stop_scope": candidate["parent_stop_scope"],
                "stop_epoch_nonce": candidate["parent_stop_scope"]["stop_epoch_nonce"],
                "child_identity": candidate["child_identity"],
                "target_id": candidate["child_identity"]["target_id"],
                "recorded_session_id": None,
                "recorded_conversation_url": None,
                "observed_target_url": cleanup.get("observed_target_url"),
                "cleanup": cleanup,
                "claim_revalidated": candidate["claim"],
                "stderr_revalidated": candidate["stderr_discrepancy"],
                "post_cleanup_preimages": {
                    "child": {
                        "path": str(child_file),
                        "sha256": STATE.sha256_file(child_file),
                        "bytes": child_file.stat().st_size,
                    }
                },
                "zero_provider_asserted": False,
                "provider_mutation_may_have_occurred": True,
                "result_promoted": False,
                "settled_at": STATE.utc_now(),
            }
            if any(
                current.get(key) is not None
                for key in ("session_id", "conversation_url", "submission_receipt", "result")
            ):
                raise BridgeError(
                    "PARENT_STOP_UNCERTAIN_IDENTITY_CHANGED",
                    "provider identity or result appeared after pre-close snapshot",
                )
            settlement = STATE.write_immutable_json_exclusive(
                settlement_path, settlement_payload
            )
        return self.store.settle_parent_stopped_submission_uncertain_child(
            child_dir, preclose=preclose, settlement=settlement
        )

    def _bind_prepared_composer(
        self,
        *,
        run_dir: str,
        state_file: Path,
        record: dict[str, Any],
        composer_result: dict[str, Any],
        lifecycle,
        evidence_filename: str = "composer-app-evidence.json",
        selection_kind: str = "app",
    ) -> tuple[dict[str, Any], str, Path]:
        target_id = str(composer_result.get("target_id") or "")
        old_target_id = str(record.get("current_target_id") or "") or None
        record = self.store.transition(
            run_dir,
            "LEASED",
            target_id=target_id,
            rebind_reason=("pre-submit-composer-retry" if old_target_id and old_target_id != target_id else None),
        )
        evidence_path = state_file.parent / evidence_filename
        try:
            lifecycle.record_owned(
                run_dir,
                target_id=target_id,
                url=str(composer_result.get("url") or "https://chatgpt.com/"),
                stage="pre-submit-composer",
            )
            write_json_atomic(evidence_path, sanitize_evidence(composer_result))
            if selection_kind == "app":
                record = self.store.transition(run_dir, "LEASED", app_evidence_ref=str(evidence_path))
            else:
                fields: dict[str, Any] = {
                    "selection_evidence_ref": {
                        "kind": selection_kind,
                        "path": str(evidence_path),
                        "sha256": STATE.sha256_file(evidence_path),
                        "target_id": target_id,
                    }
                }
                if selection_kind == "deep-research-app-selection":
                    fields["app_evidence_ref"] = str(evidence_path)
                record = self.store.transition(
                    run_dir,
                    "LEASED",
                    **fields,
                )
        except Exception as exc:
            cleanup = self._safe_tab_cleanup(
                lifecycle,
                run_dir,
                target_id=target_id,
                url=str(composer_result.get("url") or "https://chatgpt.com/"),
                reason="prepared-target-evidence-failed",
            )
            blocked = self.store.transition(
                run_dir,
                "PREFLIGHT_BLOCKED",
                block_code="TAB_OWNERSHIP_EVIDENCE_FAILED",
                recovery_event={
                    "kind": "prepared-target-evidence-failed",
                    "detail": _redact_sensitive_text(str(exc)),
                    "cleanup": cleanup,
                },
            )
            raise BridgeError(
                "TAB_OWNERSHIP_EVIDENCE_FAILED",
                "prepared target ownership evidence could not be persisted",
                {"phase": blocked["phase"], "cleanup": cleanup},
            ) from exc
        if old_target_id and old_target_id != target_id:
            try:
                old_cleanup = lifecycle.close_pre_submit(
                    run_dir,
                    target_id=old_target_id,
                    reason="superseded-pre-submit-composer",
                )
            except Exception as exc:
                blocked = self.store.transition(
                    run_dir,
                    "PREFLIGHT_BLOCKED",
                    block_code="SUPERSEDED_TAB_CLEANUP_FAILED",
                    recovery_event={
                        "kind": "superseded-pre-submit-tab-cleanup-failed",
                        "target_id": old_target_id,
                        "detail": _redact_sensitive_text(str(exc)),
                    },
                )
                raise BridgeError(
                    "SUPERSEDED_TAB_CLEANUP_FAILED",
                    "superseded pre-submit composer could not be safely cleaned",
                    {"phase": blocked["phase"], "target_id": old_target_id},
                ) from exc
            authority = record.get("pre_submit_retry_authority")
            if isinstance(authority, dict) and authority.get("eligible") is True and authority.get("consumed_at") is None:
                record = self.store.confirm_child_retry_replacement(
                    run_dir,
                    target_id=target_id,
                    evidence_path=evidence_path,
                )
        return record, target_id, evidence_path

    def _verify_app_identity(
        self,
        *,
        public_url: str,
        expected_root: str,
        expected_port: int | None,
        timeout: int,
        scope_mode: str = "legacy-drive",
        topology_receipt_sha256: str | None = None,
    ) -> dict[str, Any]:
        if self.app_identity_probe:
            if scope_mode == "legacy-drive":
                result = self.app_identity_probe(public_url, expected_root, expected_port, timeout)
            else:
                result = self.app_identity_probe(
                    public_url,
                    expected_root,
                    expected_port,
                    timeout,
                    scope_mode,
                    topology_receipt_sha256,
                )
        else:
            helper = _load_app_identity_module()
            result = helper.probe_codexpro_identity(
                public_url,
                expected_root,
                expected_port,
                timeout=timeout,
                scope_mode=scope_mode,
                topology_receipt_sha256=topology_receipt_sha256,
            )
        return sanitize_evidence(result)

    @staticmethod
    def _app_attestation_scope(manifest: Mapping[str, Any]) -> str:
        explicit = str(manifest.get("app_attestation_scope") or "").strip()
        if explicit:
            return explicit
        correlation = manifest.get("workflow_correlation") if isinstance(manifest.get("workflow_correlation"), Mapping) else {}
        stage = str(correlation.get("stage") or "").strip()
        if re.fullmatch(r"solver-\d+", stage):
            return "solver-wave"
        if re.fullmatch(r"initial-refiner-\d+", stage):
            return "initial-refiner-wave"
        match = re.fullmatch(r"iter-(\d+)-(merger|refiner)-\d+", stage)
        if match:
            return f"iter-{match.group(1)}-{match.group(2)}-wave"
        return stage

    def _parent_app_attestation_path(
        self,
        state_file: Path,
        record: Mapping[str, Any],
        manifest: Mapping[str, Any],
    ) -> Path | None:
        parent_run_id = str(record.get("parent_run_id") or "")
        scope = self._app_attestation_scope(manifest)
        if not parent_run_id or not scope:
            return None
        parent_dir = state_file.parent.parent / parent_run_id
        if not parent_dir.is_dir():
            return None
        scope_key = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
        return parent_dir / "app-attestations" / f"{scope_key}.json"

    @staticmethod
    def _registration_fingerprint(registration: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        payload = {
            "root": str(registration.get("root") or ""),
            "app_name": str(registration.get("app_name") or ""),
            "public_url": str(registration.get("public_url") or ""),
            "port": registration.get("port"),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return payload, hashlib.sha256(encoded).hexdigest()

    def _global_app_contract_path(self) -> Path:
        return self.store.root / "app-contract-state.json"

    @staticmethod
    def _global_app_contract_key(registration: Mapping[str, Any]) -> str:
        identity = {
            "root": str(registration.get("root") or ""),
            "app_name": str(registration.get("app_name") or ""),
        }
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]

    @staticmethod
    def _public_endpoint_key(public_url: str) -> str:
        parsed = urlsplit(public_url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    def _read_global_app_contract_state(self) -> dict[str, Any]:
        path = self._global_app_contract_path()
        empty = {"schema": GLOBAL_APP_CONTRACT_SCHEMA, "entries": {}, "events": []}
        if not path.is_file() or path.is_symlink():
            return empty
        try:
            state = STATE.read_json(path)
        except (OSError, RuntimeError, ValueError):
            return empty
        if (
            state.get("schema") != GLOBAL_APP_CONTRACT_SCHEMA
            or not isinstance(state.get("entries"), Mapping)
            or not isinstance(state.get("events"), list)
        ):
            return empty
        return {
            "schema": GLOBAL_APP_CONTRACT_SCHEMA,
            "entries": dict(state["entries"]),
            "events": list(state["events"])[-GLOBAL_APP_CONTRACT_MAX_EVENTS:],
        }

    @staticmethod
    def _append_global_app_contract_event(state: dict[str, Any], event: Mapping[str, Any]) -> None:
        events = list(state.get("events") or [])
        events.append(dict(event))
        state["events"] = events[-GLOBAL_APP_CONTRACT_MAX_EVENTS:]

    def _global_app_contract_candidate(
        self,
        *,
        state_file: Path,
        record: Mapping[str, Any],
        manifest: Mapping[str, Any],
        app_name: str,
        connector: Any,
    ) -> dict[str, Any] | None:
        if manifest.get("force_app_ui_verify") is True or manifest.get("app_decision_path"):
            return None
        try:
            registration = connector.expected_registration_for_scope(app_name, record["project_root"])
        except Exception:
            return None
        if not isinstance(registration, Mapping):
            return None
        registration_payload, registration_sha = self._registration_fingerprint(registration)
        expected_url = str(registration_payload.get("public_url") or "").strip()
        expected_root = str(registration_payload.get("root") or "").strip()
        expected_name = str(registration_payload.get("app_name") or "").strip()
        if not expected_url or not expected_root or expected_name != app_name:
            return None
        explicit_url = str(manifest.get("chatgpt_app_server_url") or "").strip()
        if explicit_url and explicit_url != expected_url:
            return None
        cache_path = self._global_app_contract_path()
        state = self._read_global_app_contract_state()
        key = self._global_app_contract_key(registration_payload)
        entry = state["entries"].get(key)
        if not isinstance(entry, Mapping):
            return None
        try:
            source_path = Path(str(entry.get("source_evidence_path") or "")).expanduser().resolve(strict=True)
            source_path.relative_to(self.store.root)
            source_sha = STATE.sha256_file(source_path)
        except (OSError, RuntimeError, ValueError):
            return None
        expected_url_sha = hashlib.sha256(expected_url.encode("utf-8")).hexdigest()
        if not (
            entry.get("schema") == "codex.chatgpt.global-app-contract-entry/v1"
            and str(entry.get("app_name") or "") == app_name
            and str(entry.get("root") or "") == expected_root
            and entry.get("port") == registration_payload.get("port")
            and str(entry.get("registration_sha256") or "") == registration_sha
            and str(entry.get("registered_url_sha256") or "") == expected_url_sha
            and str(entry.get("endpoint_key") or "") == self._public_endpoint_key(expected_url)
            and entry.get("connected") is True
            and entry.get("full_access") is True
            and source_sha == str(entry.get("source_evidence_sha256") or "")
        ):
            return None
        identity = self._verify_app_identity(
            public_url=expected_url,
            expected_root=expected_root,
            expected_port=(int(registration_payload["port"]) if registration_payload.get("port") is not None else None),
            timeout=int(manifest.get("app_identity_timeout_seconds") or 15),
            scope_mode=str(registration_payload.get("scope_mode") or "legacy-drive"),
            topology_receipt_sha256=(str(registration_payload.get("topology_receipt_sha256")) if registration_payload.get("topology_receipt_sha256") else None),
        )
        return {
            "cache_path": cache_path,
            "cache_key": key,
            "state": state,
            "entry": dict(entry),
            "registration": registration_payload,
            "registration_sha256": registration_sha,
            "expected_url": expected_url,
            "expected_root": expected_root,
            "expected_port": registration_payload.get("port"),
            "identity": identity,
            "source_evidence_path": source_path,
            "source_evidence_sha256": source_sha,
        }

    def _touch_global_app_contract_reuse(self, candidate: dict[str, Any]) -> None:
        state = candidate["state"]
        key = str(candidate["cache_key"])
        entry = dict(candidate["entry"])
        entry["last_reused_at"] = STATE.utc_now()
        entry["reuse_count"] = int(entry.get("reuse_count") or 0) + 1
        state["entries"][key] = entry
        self._append_global_app_contract_event(
            state,
            {"at": entry["last_reused_at"], "app": entry["app_name"], "result": "reused"},
        )
        write_json_atomic(candidate["cache_path"], state)

    def _record_global_app_contract(
        self,
        *,
        state_file: Path,
        record: Mapping[str, Any],
        manifest: Mapping[str, Any],
        app_name: str,
        expected_url: str,
        result: Mapping[str, Any],
        evidence_path: Path,
        connector: Any,
    ) -> None:
        if manifest.get("app_decision_path"):
            return
        inspection = result.get("inspection") if isinstance(result.get("inspection"), Mapping) else {}
        identity = result.get("identity") if isinstance(result.get("identity"), Mapping) else {}
        if not (
            identity.get("ok") is True
            and inspection.get("state") == "detail"
            and str(inspection.get("url") or "") == expected_url
            and inspection.get("connected") is True
            and inspection.get("full_access") is True
        ):
            return
        try:
            registration = connector.expected_registration_for_scope(app_name, record["project_root"])
        except Exception:
            return
        if not isinstance(registration, Mapping):
            return
        registration_payload, registration_sha = self._registration_fingerprint(registration)
        if (
            str(registration_payload.get("app_name") or "") != app_name
            or str(registration_payload.get("public_url") or "") != expected_url
        ):
            return
        now = STATE.utc_now()
        cache_path = self._global_app_contract_path()
        state = self._read_global_app_contract_state()
        key = self._global_app_contract_key(registration_payload)
        prior = state["entries"].get(key)
        state["entries"][key] = {
            "schema": "codex.chatgpt.global-app-contract-entry/v1",
            "app_name": app_name,
            "root": str(registration_payload.get("root") or ""),
            "port": registration_payload.get("port"),
            "endpoint_key": self._public_endpoint_key(expected_url),
            "registered_url_sha256": hashlib.sha256(expected_url.encode("utf-8")).hexdigest(),
            "registration_sha256": registration_sha,
            "connected": True,
            "full_access": True,
            "source_evidence_path": str(evidence_path),
            "source_evidence_sha256": STATE.sha256_file(evidence_path),
            "verified_at": now,
            "last_reused_at": (prior.get("last_reused_at") if isinstance(prior, Mapping) else None),
            "reuse_count": int(prior.get("reuse_count") or 0) if isinstance(prior, Mapping) else 0,
        }
        if len(state["entries"]) > GLOBAL_APP_CONTRACT_MAX_ENTRIES:
            ordered = sorted(
                state["entries"].items(),
                key=lambda item: str(item[1].get("last_reused_at") or item[1].get("verified_at") or ""),
                reverse=True,
            )
            state["entries"] = dict(ordered[:GLOBAL_APP_CONTRACT_MAX_ENTRIES])
        self._append_global_app_contract_event(
            state,
            {"at": now, "app": app_name, "result": "verified"},
        )
        write_json_atomic(cache_path, state)

    def _reuse_parent_app_attestation(
        self,
        *,
        run_dir: str,
        state_file: Path,
        record: dict[str, Any],
        manifest: dict[str, Any],
        app_name: str,
        connector: Any,
    ) -> dict[str, Any] | None:
        path = self._parent_app_attestation_path(state_file, record, manifest)
        if path is None or not path.is_file() or path.is_symlink():
            return None
        try:
            attestation = STATE.read_json(path)
            source_path = Path(str(attestation.get("source_evidence_path") or "")).expanduser().resolve(strict=True)
            source_path.relative_to(state_file.parent.parent)
        except (OSError, RuntimeError, ValueError):
            return None
        explicit_url = str(manifest.get("chatgpt_app_server_url") or "").strip()
        decision_path = str(manifest.get("app_decision_path") or "").strip()
        decision_sha = None
        if decision_path:
            try:
                decision_sha = STATE.sha256_file(Path(decision_path).expanduser().resolve(strict=True))
            except (OSError, RuntimeError, ValueError):
                return None
        try:
            registration = connector.expected_registration_for_scope(app_name, record["project_root"])
        except Exception:
            return None
        if not isinstance(registration, Mapping):
            return None
        registration_payload, registration_sha = self._registration_fingerprint(registration)
        if not (
            attestation.get("schema") == "codex.chatgpt.parent-wave-app-attestation/v1"
            and str(attestation.get("parent_run_id") or "") == str(record.get("parent_run_id") or "")
            and str(attestation.get("project_root") or "") == str(record.get("project_root") or "")
            and str(attestation.get("app_name") or "") == app_name
            and str(attestation.get("scope") or "") == self._app_attestation_scope(manifest)
            and (not explicit_url or str(attestation.get("expected_url") or "") == explicit_url)
            and str(attestation.get("decision_sha256") or "") == str(decision_sha or "")
            and attestation.get("registration") == registration_payload
            and str(attestation.get("registration_sha256") or "") == registration_sha
            and STATE.sha256_file(source_path) == str(attestation.get("source_evidence_sha256") or "")
            and attestation.get("identity_ok") is True
            and attestation.get("connected") is True
            and attestation.get("full_access") is True
        ):
            return None
        evidence_path = state_file.parent / "app-evidence.json"
        write_json_atomic(
            evidence_path,
            {
                "app_name": app_name,
                "result": {
                    "ok": True,
                    "phase": "COMPLETE",
                    "action": "parent-wave-attestation-reuse",
                    "attestation_path": str(path),
                    "attestation_sha256": STATE.sha256_file(path),
                    "source_evidence_path": str(source_path),
                    "source_evidence_sha256": STATE.sha256_file(source_path),
                },
            },
        )
        return self.store.transition(run_dir, "PREFLIGHTED", app_evidence_ref=str(evidence_path))

    def _record_parent_app_attestation(
        self,
        *,
        state_file: Path,
        record: Mapping[str, Any],
        manifest: Mapping[str, Any],
        app_name: str,
        expected_url: str,
        result: Mapping[str, Any],
        evidence_path: Path,
        connector: Any,
    ) -> None:
        path = self._parent_app_attestation_path(state_file, record, manifest)
        if path is None:
            return
        inspection = result.get("inspection") if isinstance(result.get("inspection"), Mapping) else {}
        identity = result.get("identity") if isinstance(result.get("identity"), Mapping) else {}
        if not (
            identity.get("ok") is True
            and inspection.get("state") == "detail"
            and str(inspection.get("url") or "") == expected_url
            and inspection.get("connected") is True
            and inspection.get("full_access") is True
        ):
            return
        try:
            registration = connector.expected_registration_for_scope(app_name, record["project_root"])
        except Exception:
            return
        if not isinstance(registration, Mapping) or str(registration.get("public_url") or "") != expected_url:
            return
        registration_payload, registration_sha = self._registration_fingerprint(registration)
        decision_path = str(manifest.get("app_decision_path") or "").strip()
        decision_sha = ""
        if decision_path:
            decision_sha = STATE.sha256_file(Path(decision_path).expanduser().resolve(strict=True))
        payload = {
            "schema": "codex.chatgpt.parent-wave-app-attestation/v1",
            "parent_run_id": record.get("parent_run_id"),
            "project_root": record.get("project_root"),
            "app_name": app_name,
            "scope": self._app_attestation_scope(manifest),
            "expected_url": expected_url,
            "decision_sha256": decision_sha,
            "registration": registration_payload,
            "registration_sha256": registration_sha,
            "identity_ok": True,
            "connected": True,
            "full_access": True,
            "source_run_id": record.get("run_id"),
            "source_evidence_path": str(evidence_path),
            "source_evidence_sha256": STATE.sha256_file(evidence_path),
            "created_at": STATE.utc_now(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = STATE.read_json(path)
            if existing != payload:
                # The first exact verifier owns this immutable wave attestation.
                return
        else:
            write_json_atomic(path, payload)

    def ensure_app(
        self,
        run_dir: str,
        *,
        _wave_attestation_lock_held: bool = False,
        _global_app_contract_lock_held: bool = False,
    ) -> dict[str, Any]:
        state_file, record = self.store.load(run_dir)
        self.store.verify_manifest(record)
        if record["phase"] != "PREFLIGHTED":
            raise BridgeError("APP_PREFLIGHT_PHASE_INVALID", f"app preflight requires PREFLIGHTED, got {record['phase']}")
        requested = record.get("requested") or {}
        manifest = _load_manifest(record)
        mode_label = str(manifest.get("mode_label") or "GPT-5.6").strip().casefold()
        policy = str(requested.get("app_policy") or ("forbidden" if mode_label == "pro" else "required"))
        app_name = str(manifest.get("chatgpt_app_name") or manifest.get("app_name") or "").strip()
        if mode_label == "pro":
            if policy != "forbidden" or app_name:
                raise BridgeError("APP_POLICY_FORBIDDEN", "Pro requires app_policy=forbidden and no app name")
            return record
        if policy != "required":
            raise BridgeError("APP_POLICY_REQUIRED", "every non-Pro ChatGPT mode requires app_policy=required")
        if not app_name:
            raise BridgeError("APP_REQUIRED", "every non-Pro ChatGPT mode requires chatgpt_app_name")

        connector = self._app_connector(record_executable(record))
        reused = self._reuse_parent_app_attestation(
            run_dir=run_dir,
            state_file=state_file,
            record=record,
            manifest=manifest,
            app_name=app_name,
            connector=connector,
        )
        if reused is not None:
            return reused

        attestation_path = self._parent_app_attestation_path(state_file, record, manifest)
        if attestation_path is not None and not _wave_attestation_lock_held:
            wait_timeout = max(
                120,
                int(manifest.get("app_attestation_wait_timeout_seconds") or 300),
            )
            with STATE.exclusive_state_lock(
                attestation_path.with_name(f"{attestation_path.name}.lock"),
                timeout_seconds=wait_timeout,
            ):
                return self.ensure_app(run_dir, _wave_attestation_lock_held=True)

        global_contract_path = self._global_app_contract_path()
        if not _global_app_contract_lock_held:
            with STATE.exclusive_state_lock(
                global_contract_path.with_name(f"{global_contract_path.name}.lock"),
                timeout_seconds=max(120, int(manifest.get("app_contract_wait_timeout_seconds") or 300)),
            ):
                return self.ensure_app(
                    run_dir,
                    _wave_attestation_lock_held=_wave_attestation_lock_held,
                    _global_app_contract_lock_held=True,
                )

        cached = self._global_app_contract_candidate(
            state_file=state_file,
            record=record,
            manifest=manifest,
            app_name=app_name,
            connector=connector,
        )
        if cached is not None:
            identity = cached["identity"]
            if identity.get("ok") is not True:
                blocked = self.store.transition(
                    run_dir,
                    "PREFLIGHT_BLOCKED",
                    block_code="APP_ENDPOINT_UNHEALTHY",
                    recovery_event={"kind": "cached-app-endpoint-unhealthy", "identity": identity},
                )
                raise BridgeError(
                    "APP_ENDPOINT_UNHEALTHY",
                    "cached CodexPro app contract matched, but the current public endpoint identity check failed",
                    {"phase": blocked["phase"], "identity": identity},
                )
            self._touch_global_app_contract_reuse(cached)
            result = {
                "ok": True,
                "phase": "COMPLETE",
                "action": "global-app-contract-reuse",
                "inspection": {
                    "state": "detail",
                    "app_name": app_name,
                    "url": cached["expected_url"],
                    "connected": True,
                    "full_access": True,
                },
                "identity": identity,
                "contract_state": {
                    "path": str(cached["cache_path"]),
                    "sha256": STATE.sha256_file(cached["cache_path"]),
                    "entry_key": cached["cache_key"],
                    "source_evidence_path": str(cached["source_evidence_path"]),
                    "source_evidence_sha256": cached["source_evidence_sha256"],
                },
            }
            evidence_path = state_file.parent / "app-evidence.json"
            write_json_atomic(evidence_path, sanitize_evidence({"app_name": app_name, "result": result}))
            self._record_parent_app_attestation(
                state_file=state_file,
                record=record,
                manifest=manifest,
                app_name=app_name,
                expected_url=cached["expected_url"],
                result=result,
                evidence_path=evidence_path,
                connector=connector,
            )
            return self.store.transition(run_dir, "PREFLIGHTED", app_evidence_ref=str(evidence_path))

        decision_path_value = manifest.get("app_decision_path")
        try:
            if decision_path_value:
                decision_path = Path(str(decision_path_value)).expanduser().resolve()
                decision = STATE.read_json(decision_path)
                if str(decision.get("app_name") or "") != app_name:
                    raise BridgeError("APP_DECISION_MISMATCH", "decision app_name does not match manifest")
                decision_root = STATE.canonical_project_root(str(decision.get("root") or ""))
                run_root = STATE.canonical_project_root(record["project_root"])
                if not app_decision_scope_matches(run_root, decision_root, str(decision.get("scope_mode") or "legacy-drive")):
                    raise BridgeError("APP_DECISION_MISMATCH", "decision project root does not match run")
                expected_url = str(decision.get("public_url") or "").strip()
                expected_port = int(decision["port"]) if decision.get("port") is not None else None
                identity = self._verify_app_identity(
                    public_url=expected_url,
                    expected_root=str(decision_root),
                    expected_port=expected_port,
                    timeout=int(manifest.get("app_identity_timeout_seconds") or 15),
                    scope_mode=str(decision.get("scope_mode") or "legacy-drive"),
                    topology_receipt_sha256=(str(decision.get("topology_receipt_sha256")) if decision.get("topology_receipt_sha256") else None),
                )
                if identity.get("ok") is not True:
                    blocked = self.store.transition(
                        run_dir,
                        "PREFLIGHT_BLOCKED",
                        block_code="APP_ENDPOINT_UNHEALTHY",
                        recovery_event={"kind": "app-endpoint-unhealthy", "identity": identity},
                    )
                    raise BridgeError(
                        "APP_ENDPOINT_UNHEALTHY",
                        "CodexPro MCP endpoint identity or reachability check failed before submission",
                        {"phase": blocked["phase"], "identity": identity},
                    )
                result = connector.reconcile(decision)
            else:
                expected_url = str(manifest.get("chatgpt_app_server_url") or "").strip()
                expected_port = None
                identity_root = record["project_root"]
                registration = None
                if not expected_url:
                    registration = connector.expected_registration_for_scope(app_name, record["project_root"])
                    if registration:
                        expected_url = str(registration.get("public_url") or "").strip()
                        expected_port = int(registration["port"]) if registration.get("port") is not None else None
                        identity_root = str(registration.get("root") or identity_root)
                if not expected_url:
                    blocked = self.store.transition(
                        run_dir,
                        "PREFLIGHT_BLOCKED",
                        recovery_event={"kind": "app-expected-url-missing", "app_name": app_name},
                    )
                    raise BridgeError(
                        "APP_EXPECTED_URL_MISSING",
                        "app-backed submission needs chatgpt_app_server_url or app_decision_path",
                        {"phase": blocked["phase"]},
                    )
                identity = self._verify_app_identity(
                    public_url=expected_url,
                    expected_root=identity_root,
                    expected_port=expected_port,
                    timeout=int(manifest.get("app_identity_timeout_seconds") or 15),
                    scope_mode=str((registration or {}).get("scope_mode") or manifest.get("app_scope_mode") or "legacy-drive"),
                    topology_receipt_sha256=(
                        str((registration or {}).get("topology_receipt_sha256") or manifest.get("topology_receipt_sha256"))
                        if ((registration or {}).get("topology_receipt_sha256") or manifest.get("topology_receipt_sha256"))
                        else None
                    ),
                )
                if identity.get("ok") is not True:
                    blocked = self.store.transition(
                        run_dir,
                        "PREFLIGHT_BLOCKED",
                        block_code="APP_ENDPOINT_UNHEALTHY",
                        recovery_event={"kind": "app-endpoint-unhealthy", "identity": identity},
                    )
                    raise BridgeError(
                        "APP_ENDPOINT_UNHEALTHY",
                        "CodexPro MCP endpoint identity or reachability check failed before submission",
                        {"phase": blocked["phase"], "identity": identity},
                    )
                inspection = connector.inspect(app_name, expected_url=expected_url)
                if not (
                    inspection.get("state") == "detail"
                    and inspection.get("url") == expected_url
                    and inspection.get("connected") is True
                    and inspection.get("full_access") is True
                ):
                    # With no external decision file, an exact active
                    # registration is itself the narrow authority to repair
                    # only this app's connection/permission state. This does
                    # not create a replacement name or alter another scope.
                    # A missing, foreign, or ambiguous registration remains
                    # blocked below.
                    if inspection.get("state") == "detail" and registration and app_decision_scope_matches(
                        STATE.canonical_project_root(record["project_root"]),
                        STATE.canonical_project_root(str(registration["root"])),
                        str(registration.get("scope_mode") or "legacy-drive"),
                    ):
                        repair_decision = {
                            "root": str(registration["root"]),
                            "app_name": app_name,
                            "public_url": expected_url,
                            "port": expected_port,
                            "action": "repair-active-exact-registration",
                        }
                        repair_result = connector.reconcile(repair_decision)
                        inspection = connector.inspect(app_name, expected_url=expected_url)
                        if (
                            inspection.get("state") == "detail"
                            and inspection.get("url") == expected_url
                            and inspection.get("connected") is True
                            and inspection.get("full_access") is True
                        ):
                            result = {
                                "ok": True,
                                "phase": "COMPLETE",
                                "action": "registry-exact-reconcile",
                                "inspection": inspection,
                                "identity": identity,
                                "repair": sanitize_evidence(repair_result),
                            }
                            evidence_path = state_file.parent / "app-evidence.json"
                            write_json_atomic(evidence_path, sanitize_evidence({"app_name": app_name, "result": result}))
                            self._record_global_app_contract(
                                state_file=state_file, record=record, manifest=manifest, app_name=app_name,
                                expected_url=expected_url, result=result, evidence_path=evidence_path, connector=connector,
                            )
                            self._record_parent_app_attestation(
                                state_file=state_file,
                                record=record,
                                manifest=manifest,
                                app_name=app_name,
                                expected_url=expected_url,
                                result=result,
                                evidence_path=evidence_path,
                                connector=connector,
                            )
                            return self.store.transition(run_dir, "PREFLIGHTED", app_evidence_ref=str(evidence_path))
                    blocked = self.store.transition(
                        run_dir,
                        "PREFLIGHT_BLOCKED",
                        recovery_event={"kind": "app-mismatch-needs-decision", "inspection": sanitize_evidence(inspection)},
                    )
                    raise BridgeError(
                        "APP_RECONCILE_DECISION_REQUIRED",
                        "app mismatch requires deterministic app_decision_path",
                        {"phase": blocked["phase"], "inspection": sanitize_evidence(inspection)},
                    )
                result = {
                    "ok": True,
                    "phase": "COMPLETE",
                    "action": "inspect-match",
                    "inspection": inspection,
                    "identity": identity,
                }
        except BridgeError:
            raise
        except Exception as exc:
            blocked = self.store.transition(
                run_dir,
                "BLOCKED_APP_TRANSACTION",
                block_code="APP_TRANSACTION_FAILED",
                recovery_event={"kind": "app-transaction-failed", "detail": _redact_sensitive_text(str(exc))},
            )
            raise BridgeError("APP_TRANSACTION_FAILED", "app transaction failed closed", {"phase": blocked["phase"], "detail": _redact_sensitive_text(str(exc))}) from exc

        evidence_path = state_file.parent / "app-evidence.json"
        write_json_atomic(evidence_path, sanitize_evidence({"app_name": app_name, "result": result}))
        self._record_global_app_contract(
            state_file=state_file, record=record, manifest=manifest, app_name=app_name,
            expected_url=expected_url, result=result, evidence_path=evidence_path, connector=connector,
        )
        self._record_parent_app_attestation(
            state_file=state_file,
            record=record,
            manifest=manifest,
            app_name=app_name,
            expected_url=expected_url,
            result=result,
            evidence_path=evidence_path,
            connector=connector,
        )
        return self.store.transition(run_dir, "PREFLIGHTED", app_evidence_ref=str(evidence_path))

    def _evidence(self, run_dir: Path, name: str, completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        evidence_dir = run_dir / "agbrowse-evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = evidence_dir / f"{name}.stdout.txt"
        stderr_path = evidence_dir / f"{name}.stderr.txt"
        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")
        return {
            "exit_code": completed.returncode,
            "stdout": str(stdout_path),
            "stdout_sha256": sha256_bytes((completed.stdout or "").encode("utf-8")),
            "stderr": str(stderr_path),
            "stderr_sha256": sha256_bytes((completed.stderr or "").encode("utf-8")),
        }

    def _show_session_identity(
        self,
        *,
        executable: str,
        manifest: dict[str, Any],
        session_id: str,
        run_dir: Path,
    ) -> tuple[str | None, str | None, dict[str, Any], dict[str, Any]]:
        command = [executable, "web-ai", "sessions", "show", session_id, "--json"]
        completed = self.runner(command, bridge_env(manifest), int(manifest.get("session_show_timeout_seconds") or 60))
        evidence = self._evidence(run_dir, "session-show", completed)
        payload = _json_output(completed.stdout)
        session = payload.get("session") if isinstance(payload.get("session"), dict) else payload
        target_id = str(session.get("targetId") or session.get("target_id") or "") or None
        url = session.get("conversationUrl") or session.get("conversation_url") or session.get("url")
        canonical_url = str(url) if url and STATE.CANONICAL_CHAT_RE.fullmatch(str(url)) else None
        return target_id, canonical_url, evidence, dict(session)

    def _observe_post_send_target(
        self,
        *,
        run_dir: Path,
        executable: str,
        manifest: dict[str, Any],
        target_id: str,
        attempts: int = 4,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Observe only the send-returned target until it binds or settles."""
        evidence: list[dict[str, Any]] = []
        observation: dict[str, Any] = {"state": "absent", "match_count": 0}
        for index in range(max(1, attempts)):
            tabs, tabs_evidence = self._recovery_tabs(
                run_dir=run_dir,
                name=f"post-send-tabs-{index:02d}",
                executable=executable,
                env=bridge_env(manifest),
            )
            evidence.append(tabs_evidence)
            observation = exact_target_observation(tabs, target_id)
            if observation.get("state") in {"canonical", "ambiguous", "drifted"}:
                return observation, evidence
            if index + 1 < attempts:
                time.sleep(0.25)
        return observation, evidence

    def _recover_session_identity_from_store(
        self,
        *,
        run_dir: Path,
        executable: str,
        manifest: dict[str, Any],
        record: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        command = [executable, "web-ai", "sessions", "list", "--vendor", "chatgpt", "--limit", "100", "--json"]
        completed = self.runner(command, bridge_env(manifest), int(manifest.get("session_show_timeout_seconds") or 60))
        evidence = self._evidence(run_dir, "history-session-list", completed)
        payload = _completed_json_output(completed)
        sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
        identity = record.get("recovery_identity") if isinstance(record.get("recovery_identity"), dict) else {}
        expected_path = str(identity.get("attachment_path") or "")
        if not expected_path:
            raise BridgeError("RECOVERY_SESSION_IDENTITY_MISSING", "run-owned prompt alias is missing")
        expected_normalized = os.path.normcase(os.path.abspath(expected_path))
        matches: list[dict[str, Any]] = []
        for session in sessions:
            if not isinstance(session, dict):
                continue
            summary = session.get("envelopeSummary") if isinstance(session.get("envelopeSummary"), dict) else {}
            candidate_path = str(summary.get("filePath") or "")
            if not candidate_path:
                continue
            if os.path.normcase(os.path.abspath(candidate_path)) == expected_normalized:
                matches.append(session)
        if len(matches) != 1:
            raise BridgeError(
                "RECOVERY_SESSION_IDENTITY_AMBIGUOUS" if matches else "RECOVERY_SESSION_IDENTITY_NOT_FOUND",
                "exactly one persisted agbrowse session must claim the run-owned prompt alias",
                {"match_count": len(matches), "evidence": evidence},
            )
        match = matches[0]
        session_id = str(match.get("sessionId") or match.get("session_id") or "")
        if not session_id:
            raise BridgeError("RECOVERY_SESSION_IDENTITY_MISSING", "matched session has no session id", {"evidence": evidence})
        return session_id, {
            "kind": "run-owned-prompt-session-match",
            "session_id": session_id,
            "target_id": str(match.get("targetId") or match.get("target_id") or ""),
            "status": str(match.get("status") or ""),
            "evidence": evidence,
        }

    def _reclassify_mutation_disallowed(self, run_dir: str, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("session_id") or record.get("conversation_url"):
            raise BridgeError("UNCERTAIN_RECLASSIFICATION_UNSAFE", "submission identity exists; pre-submit reclassification is forbidden")
        stdout_path = Path(run_dir) / "agbrowse-evidence" / "send.stdout.txt"
        stderr_path = Path(run_dir) / "agbrowse-evidence" / "send.stderr.txt"
        if not stdout_path.is_file() or not stderr_path.is_file():
            raise BridgeError("UNCERTAIN_RECLASSIFICATION_EVIDENCE_MISSING", "send stdout/stderr evidence is missing")
        if stdout_path.read_text(encoding="utf-8").strip():
            raise BridgeError("UNCERTAIN_RECLASSIFICATION_CONFLICT", "nonempty send stdout conflicts with a pre-submit rejection")
        payload = _json_output(stderr_path.read_text(encoding="utf-8"))
        envelope = normalize_envelope(payload)
        if classify_pre_submit_failure(envelope) != "SEND_REJECTED" or envelope.get("mutation_allowed") is not False:
            raise BridgeError("UNCERTAIN_RECLASSIFICATION_UNPROVEN", "stderr does not prove mutationAllowed=false pre-submit rejection")
        return self.store.transition(
            run_dir,
            "SEND_REJECTED",
            recovery_event={
                "kind": "verified-mutation-disallowed-reclassification",
                "error_code": envelope.get("error_code"),
                "error_stage": envelope.get("error_stage"),
                "send_stdout_path": str(stdout_path),
                "send_stdout_sha256": STATE.sha256_file(stdout_path),
                "send_stderr_path": str(stderr_path),
                "send_stderr_sha256": STATE.sha256_file(stderr_path),
            },
        )

    def reclassify_pre_submit(self, run_dir: str) -> dict[str, Any]:
        _, record = self.store.load(run_dir)
        if record.get("phase") not in {"SUBMISSION_UNCERTAIN_IDENTITY_MISSING", "BLOCKED_RECOVERY_EXHAUSTED"}:
            raise BridgeError("PRE_SUBMIT_RECLASSIFY_PHASE_INVALID", "only uncertain or exhausted runs can be reclassified", {"phase": record.get("phase")})
        return self._reclassify_mutation_disallowed(run_dir, record)

    def retire_uncommitted_session(self, run_dir: str) -> dict[str, Any]:
        state_file, record = self.store.load(run_dir)
        if record.get("phase") == "SEND_REJECTED":
            return self.store.transition(run_dir, "CANCELLED_PRE_SUBMISSION")
        if record.get("phase") not in {
            "SUBMITTED",
            "RECOVERY_REQUIRED",
            "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
            "BLOCKED_RECOVERY_EXHAUSTED",
            "USER_STOP_REQUESTED",
        }:
            raise BridgeError(
                "UNCOMMITTED_SESSION_PHASE_INVALID",
                "only an uncertain or exhausted run can retire an uncommitted session",
                {"phase": record.get("phase")},
            )
        session_id = str(record.get("session_id") or "")
        target_id = str(record.get("current_target_id") or "")
        if not session_id or not target_id or record.get("conversation_url") or record.get("result") is not None:
            raise BridgeError("UNCOMMITTED_SESSION_IDENTITY_INVALID", "exact session and target without provider result are required")
        manifest = _load_manifest(record)
        executable = record_executable(record)
        shown = self.runner(
            [executable, "web-ai", "sessions", "show", session_id, "--json"],
            bridge_env(manifest),
            int(manifest.get("session_show_timeout_seconds") or 60),
        )
        show_evidence = self._evidence(state_file.parent, "uncommitted-session-show", shown)
        payload = _completed_json_output(shown)
        session = payload.get("session") if isinstance(payload.get("session"), dict) else payload
        summary = session.get("envelopeSummary") if isinstance(session.get("envelopeSummary"), dict) else {}
        trace = session.get("trace") if isinstance(session.get("trace"), list) else []
        send_click = next(
            (
                item
                for item in trace
                if isinstance(item, dict)
                and str(item.get("intentId") or "") == "send.click"
                and str(item.get("status") or "") == "unresolved"
            ),
            None,
        )
        attempts = send_click.get("attempts") if isinstance(send_click, dict) and isinstance(send_click.get("attempts"), list) else []
        not_enabled = any(
            isinstance(item, dict)
            and isinstance(item.get("validation"), dict)
            and str(item["validation"].get("reason") or "") == "not-enabled"
            for item in attempts
        )
        observed_url = str(session.get("conversationUrl") or session.get("originalUrl") or "")
        deadline_text = str(session.get("deadlineAt") or "")
        try:
            deadline = datetime.fromisoformat(deadline_text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BridgeError("UNCOMMITTED_SESSION_DEADLINE_INVALID", "persisted session deadline is invalid") from exc
        deadline_expired = deadline <= datetime.now(timezone.utc)
        tabs, tabs_evidence = self._recovery_tabs(
            run_dir=state_file.parent,
            name="uncommitted-session-tabs",
            executable=executable,
            env=bridge_env(manifest),
        )
        target_absent = not any(_tab_id(tab) == target_id for tab in tabs)
        if not (
            str(session.get("sessionId") or "") == session_id
            and str(session.get("targetId") or session.get("tabId") or "") == target_id
            and str(session.get("status") or "") == "sent"
            and not STATE.CANONICAL_CHAT_RE.fullmatch(observed_url)
            and session.get("answer") in (None, "")
            and int(summary.get("assistantCount") or 0) == 0
            and isinstance(send_click, dict)
            and str(send_click.get("errorCode") or "") == "TARGET_UNRESOLVED"
            and not_enabled
            and target_absent
        ):
            raise BridgeError(
                "UNCOMMITTED_SESSION_NOT_PROVEN",
                "exact session does not prove an expired, absent, never-clicked provider submission",
                {
                    "session_id_match": str(session.get("sessionId") or "") == session_id,
                    "target_id_match": str(session.get("targetId") or session.get("tabId") or "") == target_id,
                    "status": session.get("status"),
                    "observed_url": observed_url,
                    "assistant_count": int(summary.get("assistantCount") or 0),
                    "send_click_unresolved": isinstance(send_click, dict),
                    "send_click_not_enabled": not_enabled,
                    "deadline_expired": deadline_expired,
                    "target_absent": target_absent,
                },
            )
        proof_path = state_file.parent / "uncommitted-session-proof.json"
        write_json_atomic(
            proof_path,
            {
                "schema": "codex.chatgpt.uncommitted-session-proof/v1",
                "run_id": record.get("run_id"),
                "session_id": session_id,
                "target_id": target_id,
                "session_status": "sent",
                "observed_url": observed_url,
                "assistant_count": 0,
                "send_click_status": "unresolved",
                "send_click_reason": "not-enabled",
                "session_deadline": deadline_text,
                "session_deadline_expired": deadline_expired,
                "session_deadline_wait_required": False,
                "target_absent": True,
                "show_evidence": show_evidence,
                "tabs_evidence": tabs_evidence,
            },
        )
        proof_event = {
            "kind": "verified-mutation-disallowed-reclassification",
            "mutation_allowed": False,
            "send_click_status": "unresolved",
            "send_click_reason": "not-enabled",
            "assistant_count": 0,
            "session_id": session_id,
            "target_id": target_id,
            "session_status": "sent",
            "observed_url": observed_url,
            "session_deadline_expired": deadline_expired,
            "session_deadline_wait_required": False,
            "target_absent": True,
            "evidence_path": str(proof_path),
            "evidence_sha256": STATE.sha256_file(proof_path),
        }
        self.store.transition(run_dir, "RECOVERING")
        self.store.transition(
            run_dir,
            "RECOVERY_REQUIRED",
            recovery_event={"kind": "uncommitted-session-proof-captured", "proof": proof_event},
        )
        self.store.transition(run_dir, "SEND_REJECTED", recovery_event=proof_event)
        if str(record.get("record_kind") or "standalone") == "child":
            self.store.record_child_cleanup(
                run_dir,
                {
                    "ok": True,
                    "state": "already-absent",
                    "target_id": target_id,
                    "evidence": {
                        "path": str(proof_path),
                        "sha256": STATE.sha256_file(proof_path),
                    },
                },
            )
        return self.store.transition(run_dir, "CANCELLED_PRE_SUBMISSION")

    def _adjudicate_pre_submit_session_artifact(
        self,
        *,
        run_dir: Path,
        executable: str,
        manifest: dict[str, Any],
        target_id: str,
    ) -> dict[str, Any]:
        command = [executable, "web-ai", "sessions", "list", "--vendor", "chatgpt", "--limit", "100", "--json"]
        listed = self.runner(command, bridge_env(manifest), int(manifest.get("session_show_timeout_seconds") or 60))
        list_evidence = self._evidence(run_dir, "pre-submit-retry-sessions-list", listed)
        payload = _completed_json_output(listed)
        sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
        matches = [item for item in sessions if isinstance(item, dict) and str(item.get("targetId") or "") == target_id]
        if len(matches) > 1:
            raise BridgeError("PRE_SUBMIT_RETRY_SESSION_AMBIGUOUS", "multiple persisted sessions claim the exact retry target")
        if not matches:
            return {"state": "no-session-artifact", "target_id": target_id, "list_evidence": list_evidence}
        session = matches[0]
        session_id = str(session.get("sessionId") or "")
        conversation_url = str(session.get("conversationUrl") or session.get("originalUrl") or "")
        envelope_summary = session.get("envelopeSummary") if isinstance(session.get("envelopeSummary"), dict) else {}
        if (
            not session_id
            or STATE.CANONICAL_CHAT_RE.fullmatch(conversation_url)
            or session.get("answer") not in (None, "")
            or int(envelope_summary.get("assistantCount") or 0) != 0
        ):
            raise BridgeError(
                "PRE_SUBMIT_RETRY_SESSION_CONFLICT",
                "persisted session contains conversation or answer evidence",
                {"session_id": session_id, "status": session.get("status"), "conversation_url": conversation_url},
            )
        status = str(session.get("status") or "")
        stop_evidence = None
        stop_quiescence = False
        if status in {"sent", "active", "running", "pending"}:
            stopped = self.runner(
                [executable, "web-ai", "stop", "--vendor", "chatgpt", "--session", session_id, "--json"],
                bridge_env(manifest),
                int(manifest.get("session_show_timeout_seconds") or 60),
            )
            stop_evidence = self._evidence(run_dir, "pre-submit-retry-session-stop", stopped)
            stopped_payload = _completed_json_output(stopped)
            stop_url = str(stopped_payload.get("url") or "")
            stop_error = stopped_payload.get("error") if isinstance(stopped_payload.get("error"), dict) else {}
            stop_error_evidence = stop_error.get("evidence") if isinstance(stop_error.get("evidence"), dict) else {}
            stop_quiescence = bool(
                stopped_payload.get("ok") is True
                and str(stopped_payload.get("status") or "") == "blocked"
                and stopped_payload.get("interrupt") is True
                and str(stopped_payload.get("sessionId") or "") == session_id
                and str(stopped_payload.get("targetId") or "") == target_id
                and stop_url.startswith("https://chatgpt.com/")
                and not STATE.CANONICAL_CHAT_RE.fullmatch(stop_url)
            )
            stop_quiescence = stop_quiescence or bool(
                stopped_payload.get("ok") is False
                and str(stop_error.get("errorCode") or "") == "cdp.target-mismatch"
                and str(stop_error.get("stage") or "") == "target-resolution"
                and stop_error.get("mutationAllowed") is False
                and str(stop_error_evidence.get("sessionId") or "") == session_id
                and str(stop_error_evidence.get("expectedTargetId") or stop_error_evidence.get("targetId") or "") == target_id
                and stop_error_evidence.get("actualTargetId") in (None, "")
                and str(stop_error_evidence.get("conversationUrl") or "").startswith("https://chatgpt.com/")
                and not STATE.CANONICAL_CHAT_RE.fullmatch(str(stop_error_evidence.get("conversationUrl") or ""))
            )
        shown = self.runner(
            [executable, "web-ai", "sessions", "show", session_id, "--json"],
            bridge_env(manifest),
            int(manifest.get("session_show_timeout_seconds") or 60),
        )
        show_evidence = self._evidence(run_dir, "pre-submit-retry-session-show", shown)
        shown_payload = _completed_json_output(shown)
        shown_session = shown_payload.get("session") if isinstance(shown_payload.get("session"), dict) else shown_payload
        shown_url = str(shown_session.get("conversationUrl") or shown_session.get("originalUrl") or "")
        shown_summary = shown_session.get("envelopeSummary") if isinstance(shown_session.get("envelopeSummary"), dict) else {}
        shown_status = str(shown_session.get("status") or "")
        stale_sent_record_quiescent = bool(
            shown_status == "sent"
            and stop_quiescence
            and shown_session.get("tabId") in (None, "")
            and not (shown_session.get("trace") or [])
        )
        if (
            str(shown_session.get("targetId") or "") != target_id
            or STATE.CANONICAL_CHAT_RE.fullmatch(shown_url)
            or shown_session.get("answer") not in (None, "")
            or int(shown_summary.get("assistantCount") or 0) != 0
            or (shown_status in {"sent", "active", "running", "pending"} and not stale_sent_record_quiescent)
        ):
            raise BridgeError("PRE_SUBMIT_RETRY_SESSION_NOT_QUIESCENT", "exact session artifact is still active or carries provider output")
        return {
            "state": "exact-session-quiescent",
            "session_id": session_id,
            "target_id": target_id,
            "status": shown_status,
            "stale_sent_record_quiescent": stale_sent_record_quiescent,
            "conversation_url": shown_url,
            "list_evidence": list_evidence,
            "stop_evidence": stop_evidence,
            "show_evidence": show_evidence,
        }

    def authorize_pre_submit_retry(self, run_dir: str) -> dict[str, Any]:
        state_file, record = self.store.load(run_dir)
        candidate = self.store.pre_submit_retry_candidate(run_dir)
        target_id = str(candidate.get("target_id") or "")
        if not target_id:
            raise BridgeError("PRE_SUBMIT_RETRY_TARGET_MISSING", "retry candidate has no exact owned target")
        manifest = _load_manifest(record)
        executable = record_executable(record)
        session_artifact = self._adjudicate_pre_submit_session_artifact(
            run_dir=state_file.parent,
            executable=executable,
            manifest=manifest,
            target_id=target_id,
        )
        lifecycle = self._tab_lifecycle(executable, manifest)
        cleanup = lifecycle.close_pre_submit(
            run_dir,
            target_id=target_id,
            reason="verified-mutation-disallowed-retry-authority",
        )
        cleanup = {**cleanup, "session_artifact": session_artifact}
        self.store.record_child_cleanup(run_dir, cleanup)
        return self.store.authorize_child_pre_submit_retry(run_dir, cleanup)

    def _parent_owned_target_ids(self, record: Mapping[str, Any]) -> set[str]:
        parent_run_id = str(record.get("parent_run_id") or "")
        if not parent_run_id:
            return {str(record.get("current_target_id") or "")} - {""}
        paths = self.store.paths(STATE.canonical_project_root(str(record["project_root"])), parent_run_id)
        owned: set[str] = set()
        for _, child in self.store._parent_children(paths.runs_dir, parent_run_id):
            owned.add(str(child.get("current_target_id") or ""))
            for event in child.get("target_rebind_events") or []:
                if isinstance(event, dict):
                    owned.add(str(event.get("old_target_id") or ""))
                    owned.add(str(event.get("new_target_id") or ""))
        owned.discard("")
        return owned

    def _capacity_retry_environment(
        self,
        *,
        run_dir: str,
        record: dict[str, Any],
        manifest: dict[str, Any],
        lifecycle,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        env = bridge_env(manifest)
        authority = record.get("pre_submit_retry_authority") if isinstance(record.get("pre_submit_retry_authority"), dict) else {}
        if str(authority.get("error_code") or "") != "provider.active-capacity":
            return env, record
        current = int(authority.get("capacity_current") or 0)
        reason = str(authority.get("capacity_reason") or "")
        if current <= 0 or reason not in {"active-max-per-key", "active-global-max"}:
            return env, record
        tabs = lifecycle.list_tabs()
        provider_tabs = []
        for tab in tabs:
            url = str(tab.get("url") or "")
            try:
                host = (urlsplit(url).hostname or "").casefold()
            except ValueError:
                host = ""
            if host == "chatgpt.com" or host.endswith(".chatgpt.com"):
                provider_tabs.append(
                    {
                        "target_id": str(tab.get("targetId") or tab.get("target_id") or tab.get("id") or ""),
                        "url": url,
                        "title": str(tab.get("title") or ""),
                    }
                )
        owned_targets = self._parent_owned_target_ids(record)
        foreign = [tab for tab in provider_tabs if not tab["target_id"] or tab["target_id"] not in owned_targets]
        if foreign:
            raise BridgeError(
                "PROVIDER_CAPACITY_LIVE_FOREIGN_TABS",
                "capacity override is forbidden while foreign ChatGPT tabs are live",
                {"foreign_count": len(foreign), "provider_tab_count": len(provider_tabs)},
            )
        headroom = max(1, min(4, int(manifest.get("parallel_lane_count") or 4)))
        override = current + headroom
        variable = (
            "AGBROWSE_PROVIDER_ACTIVE_MAX_PER_KEY"
            if reason == "active-max-per-key"
            else "AGBROWSE_PROVIDER_ACTIVE_GLOBAL_MAX"
        )
        env[variable] = str(override)
        evidence_path = Path(run_dir) / "agbrowse-evidence" / "provider-capacity-retry.json"
        evidence = {
            "schema": "codex.chatgpt.provider-capacity-retry/v1",
            "run_id": record.get("run_id"),
            "parent_run_id": record.get("parent_run_id"),
            "reason": reason,
            "reported_current": current,
            "reported_limit": int(authority.get("capacity_limit") or 0),
            "override_variable": variable,
            "override_value": override,
            "headroom": headroom,
            "provider_tabs": provider_tabs,
            "owned_target_count": len(owned_targets),
            "foreign_provider_tab_count": 0,
        }
        write_json_atomic(evidence_path, evidence)
        record = self.store.transition(
            run_dir,
            "LEASED",
            recovery_event={
                "kind": "owned-live-tab-checked-capacity-headroom",
                "evidence_path": str(evidence_path),
                "evidence_sha256": STATE.sha256_file(evidence_path),
                "override_variable": variable,
                "override_value": override,
            },
        )
        return env, record

    def _conversation_url_owner(self, url: str, *, exclude_run_id: str) -> dict[str, Any] | None:
        projects_root = self.store.root / "projects"
        if not projects_root.exists():
            return None
        owners: list[dict[str, Any]] = []
        for state_file in sorted(projects_root.glob("*/runs/*/run.json")):
            try:
                candidate = STATE.read_json(state_file)
            except Exception:
                continue
            if str(candidate.get("run_id") or "") == exclude_run_id:
                continue
            if str(candidate.get("conversation_url") or "") == url:
                owners.append({
                    "run_id": candidate.get("run_id"),
                    "project_key": candidate.get("project_key"),
                    "project_root": candidate.get("project_root"),
                    "prompt_sha256": candidate.get("prompt_sha256"),
                    "phase": candidate.get("phase"),
                    "state_file": str(state_file),
                })
        if not owners:
            return None
        if len(owners) == 1:
            return owners[0]
        return {
            "run_id": None,
            "phase": "MULTIPLE_URL_OWNERS",
            "owner_count": len(owners),
            "owners": owners,
        }

    def _bind_conversation_url(
        self,
        run_dir: str,
        *,
        conversation_url: str,
        session_id: str | None = None,
        target_id: str | None = None,
        rebind_reason: str | None = None,
        recovery_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        canonical = STATE.canonical_conversation_url(conversation_url)
        _, record = self.store.load(run_dir)
        with exclusive_composer_lock(self.store.root / "global-conversation-identity.lock"):
            owner = self._conversation_url_owner(canonical, exclude_run_id=str(record["run_id"]))
            if owner:
                return self.store.transition(
                    run_dir,
                    "BLOCKED_TARGET_AMBIGUOUS",
                    block_code="CONVERSATION_URL_OWNED_BY_FOREIGN_RUN",
                    recovery_event={
                        "kind": "conversation-url-owned-by-foreign-run",
                        "conversation_url": canonical,
                        "foreign_owner": owner,
                        "candidate_recovery": sanitize_evidence(recovery_event or {}),
                    },
                )
            return self.store.transition(
                run_dir,
                "URL_BOUND",
                session_id=session_id,
                conversation_url=canonical,
                target_id=target_id,
                rebind_reason=rebind_reason,
                recovery_event=recovery_event,
            )

    def _run_recovery_command(
        self,
        *,
        run_dir: Path,
        name: str,
        command: list[str],
        env: dict[str, str],
        timeout: int,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        completed = self.runner(command, env, timeout)
        return completed, self._evidence(run_dir, name, completed)

    def _try_exact_url_terminal_now(
        self,
        run_dir: str,
        *,
        tabs: list[dict[str, Any]] | None = None,
        tabs_evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Finish an exact, uniquely live conversation before waiting on stale session state."""
        state_file, record = self.store.load(run_dir)
        saved_url = str(record.get("conversation_url") or "")
        if not STATE.CANONICAL_CHAT_RE.fullmatch(saved_url):
            return record
        canonical = STATE.canonical_conversation_url(saved_url)
        manifest = _load_manifest(record)
        executable = record_executable(record)
        if tabs is None:
            try:
                tabs, tabs_evidence = self._recovery_tabs(
                    run_dir=state_file.parent,
                    name="exact-url-tabs",
                    executable=executable,
                    env=bridge_env(manifest),
                )
            except BridgeError:
                return record
        matches: list[dict[str, Any]] = []
        for tab in tabs:
            try:
                if STATE.canonical_conversation_url(_tab_url(tab)) == canonical:
                    matches.append(tab)
            except STATE.StateError:
                continue
        if len(matches) != 1 or not _tab_id(matches[0]):
            return record
        target_id = _tab_id(matches[0])
        if record.get("phase") == "SUBMITTED" or target_id != str(record.get("current_target_id") or ""):
            record = self._bind_conversation_url(
                run_dir,
                conversation_url=canonical,
                target_id=target_id,
                rebind_reason="unique-exact-url-terminal-preflight",
                recovery_event={
                    "kind": "unique-exact-url-terminal-preflight",
                    "tabs_evidence": sanitize_evidence(dict(tabs_evidence or {})),
                },
            )
            if record.get("phase") == "BLOCKED_TARGET_AMBIGUOUS":
                return record
        return self._recover_exact_bound_url_terminal(
            run_dir,
            doctor_evidence={
                "kind": "unique-exact-url-terminal-preflight",
                "tabs_evidence": sanitize_evidence(dict(tabs_evidence or {})),
            },
        )

    def _recover_exact_bound_url_terminal(
        self,
        run_dir: str,
        *,
        doctor_evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Capture a completed exact doctor-bound conversation without another session poll."""
        state_file, record = self.store.load(run_dir)
        conversation_url = str(record.get("conversation_url") or "")
        target_id = str(record.get("current_target_id") or "")
        if (
            record.get("phase") not in {"URL_BOUND", "RESPONSE_IN_PROGRESS", "RECOVERING"}
            or not target_id
            or not STATE.CANONICAL_CHAT_RE.fullmatch(conversation_url)
        ):
            return record

        manifest = _load_manifest(record)
        executable = record_executable(record)
        env = bridge_env(manifest)
        try:
            switched, switch_evidence = self._run_recovery_command(
                run_dir=state_file.parent,
                name="exact-url-terminal-switch",
                command=[executable, "tab-switch", target_id, "--json"],
                env=env,
                timeout=30,
            )
            active, active_evidence = self._run_recovery_command(
                run_dir=state_file.parent,
                name="exact-url-terminal-active",
                command=[executable, "active-tab", "--json"],
                env=env,
                timeout=30,
            )
            status_completed, status_evidence = self._run_recovery_command(
                run_dir=state_file.parent,
                name="exact-url-terminal-status",
                command=[
                    executable,
                    "web-ai",
                    "status",
                    "--vendor",
                    "chatgpt",
                    "--url",
                    conversation_url,
                    "--json",
                ],
                env=env,
                timeout=60,
            )
            snapshot_completed, snapshot_evidence = self._run_recovery_command(
                run_dir=state_file.parent,
                name="exact-url-terminal-snapshot",
                command=[executable, "web-ai", "snapshot", "--vendor", "chatgpt", "--json"],
                env=env,
                timeout=60,
            )
            text_completed, text_evidence = self._run_recovery_command(
                run_dir=state_file.parent,
                name="exact-url-terminal-text",
                command=[executable, "text"],
                env=env,
                timeout=60,
            )
        except Exception:
            # The exact-session poll remains the conservative fallback when
            # public read-only page inspection is temporarily unavailable.
            return self.store.load(run_dir)[1]

        if any(
            completed.returncode != 0
            for completed in (switched, active, status_completed, snapshot_completed, text_completed)
        ):
            return self.store.load(run_dir)[1]
        try:
            active_payload = _json_output(active.stdout)
            status_payload = _json_output(status_completed.stdout)
            snapshot_payload = _json_output(snapshot_completed.stdout)
        except BridgeError:
            return self.store.load(run_dir)[1]

        active_target = str(active_payload.get("targetId") or active_payload.get("target_id") or "")
        active_url = str(active_payload.get("url") or active_payload.get("tab", {}).get("url") or "")
        try:
            active_url = STATE.canonical_conversation_url(active_url)
        except STATE.StateError:
            return self.store.load(run_dir)[1]
        if active_target != target_id or active_url != conversation_url:
            return self.store.load(run_dir)[1]

        page_text = text_completed.stdout or ""
        answer = (
            _web_multi_assistant_answer(page_text)
            or _plain_assistant_answer(page_text)
            or _terminal_visible_assistant_answer(page_text)
            or _web_multi_assistant_answer(str(snapshot_payload.get("text") or ""))
            or _plain_assistant_answer(str(snapshot_payload.get("text") or ""))
            or _terminal_visible_assistant_answer(str(snapshot_payload.get("text") or ""))
        )
        streaming = _streaming_state(status_payload)
        if streaming is not False or not answer:
            return self.store.load(run_dir)[1]

        command_evidence = {
            "doctor": sanitize_evidence(dict(doctor_evidence or {})),
            "switch": switch_evidence,
            "active": active_evidence,
            "status": status_evidence,
            "snapshot": snapshot_evidence,
            "text": text_evidence,
        }
        terminal_error = provider_terminal_error_ui(answer)
        if terminal_error is not None:
            return self._record_provider_terminal_failure(
                run_dir,
                answer_text=answer,
                provider_status="exact-url-adjudicated-terminal",
                command_evidence=command_evidence,
                detection=terminal_error,
            )

        answer_path = state_file.parent / "answer.md"
        answer_path.write_text(answer.rstrip() + "\n", encoding="utf-8")
        descriptor = {
            "path": str(answer_path),
            "sha256": STATE.sha256_file(answer_path),
            "bytes": answer_path.stat().st_size,
            "provider_status": "exact-url-adjudicated-terminal",
            "evidence": command_evidence,
        }
        adjudication_path = state_file.parent / "exact-url-adjudication.json"
        write_json_atomic(
            adjudication_path,
            sanitize_evidence(
                {
                    "schema": "codex.chatgpt.exact-url-adjudication/v1",
                    "run_id": record["run_id"],
                    "session_id": record.get("session_id"),
                    "target_id": target_id,
                    "conversation_url": conversation_url,
                    "streaming": False,
                    "answer": descriptor,
                    "evidence": command_evidence,
                }
            ),
        )
        self.store.transition(run_dir, "RESULT_CAPTURED", result=descriptor)
        self.store.transition(run_dir, "VERIFIED")
        return self.store.transition(
            run_dir,
            "COMPLETE",
            recovery_event={
                "kind": "exact-url-adjudication-complete",
                "adjudication_evidence": str(adjudication_path),
                "adjudication_sha256": STATE.sha256_file(adjudication_path),
            },
        )

    def _recovery_tabs(
        self,
        *,
        run_dir: Path,
        name: str,
        executable: str,
        env: dict[str, str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        completed, evidence = self._run_recovery_command(
            run_dir=run_dir,
            name=name,
            command=[executable, "tabs", "--json"],
            env=env,
            timeout=30,
        )
        if completed.returncode != 0:
            raise BridgeError(
                "RECOVERY_TAB_LIST_FAILED",
                "agbrowse tabs was unavailable during recovery adjudication",
                {"evidence": evidence},
            )
        return _tabs_from_payload(_json_value(completed.stdout)), evidence

    def _open_recovery_utility_target(
        self,
        *,
        run_dir: Path,
        executable: str,
        manifest: dict[str, Any],
        known_preexisting_target_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        env = bridge_env(manifest)
        initial_tabs: list[dict[str, Any]] = []
        browser_started = False
        evidence: list[dict[str, Any]] = []
        try:
            initial_tabs, listed = self._recovery_tabs(
                run_dir=run_dir,
                name="history-tabs-before",
                executable=executable,
                env=env,
            )
            evidence.append(listed)
        except BridgeError as first_error:
            evidence.append({"initial_tabs_error": first_error.envelope()})
            started, start_evidence = self._run_recovery_command(
                run_dir=run_dir,
                name="history-browser-start",
                command=[executable, "start", "--headed", "--json"],
                env=env,
                timeout=90,
            )
            evidence.append(start_evidence)
            if started.returncode != 0:
                raise BridgeError(
                    "RECOVERY_BROWSER_START_FAILED",
                    "headed agbrowse browser could not be started for read-only adjudication",
                    {"evidence": evidence},
                )
            browser_started = True

        current_tabs, listed = self._recovery_tabs(
            run_dir=run_dir,
            name="history-tabs-after-start",
            executable=executable,
            env=env,
        )
        evidence.append(listed)
        initial_by_id = {_tab_id(tab): tab for tab in initial_tabs if _tab_id(tab)}
        current_by_id = {_tab_id(tab): tab for tab in current_tabs if _tab_id(tab)}
        known_preexisting = set(known_preexisting_target_ids) if known_preexisting_target_ids is not None else set(initial_by_id)
        implicit_startup_blank_ids = {
            _tab_id(tab)
            for tab in initial_tabs
            if len(initial_tabs) == 1
            and _tab_url(tab) in {"", "about:blank"}
            and tab.get("lastActiveAt") is None
            and _tab_id(tab)
        }
        known_preexisting -= implicit_startup_blank_ids
        created_targets = set(current_by_id) - known_preexisting
        utility_id = ""
        borrowed_original_url: str | None = None

        eligible = sorted(
            target_id
            for target_id in created_targets
            if target_id in current_by_id
            and _tab_url(current_by_id[target_id]) in {"", "about:blank", "https://chatgpt.com/"}
        )
        utility_id = eligible[0] if eligible else ""

        if not utility_id:
            opened, open_evidence = self._run_recovery_command(
                run_dir=run_dir,
                name="history-new-tab",
                command=[executable, "new-tab", "https://chatgpt.com/", "--json"],
                env=env,
                timeout=30,
            )
            evidence.append(open_evidence)
            if opened.returncode != 0:
                raise BridgeError("RECOVERY_UTILITY_TARGET_FAILED", "agbrowse could not open a recovery utility target")
            opened_payload = _json_output(opened.stdout)
            utility_id = str(opened_payload.get("targetId") or opened_payload.get("target_id") or "")
            if not utility_id:
                raise BridgeError("RECOVERY_UTILITY_TARGET_FAILED", "new-tab returned no exact target id")
            if utility_id in initial_by_id and utility_id in known_preexisting:
                borrowed_original_url = _tab_url(initial_by_id[utility_id])
                if borrowed_original_url not in {"", "about:blank"}:
                    raise BridgeError(
                        "RECOVERY_UTILITY_TARGET_FOREIGN",
                        "new-tab unexpectedly reused a nonblank pre-existing target",
                        {"target_id": utility_id, "url": borrowed_original_url},
                    )
            else:
                created_targets.add(utility_id)

        switched, switch_evidence = self._run_recovery_command(
            run_dir=run_dir,
            name="history-utility-switch",
            command=[executable, "tab-switch", utility_id, "--json"],
            env=env,
            timeout=30,
        )
        evidence.append(switch_evidence)
        if switched.returncode != 0:
            raise BridgeError("RECOVERY_UTILITY_TARGET_FAILED", "exact recovery utility target could not be activated")
        navigated, navigate_evidence = self._run_recovery_command(
            run_dir=run_dir,
            name="history-utility-root",
            command=[executable, "navigate", "https://chatgpt.com/", "--wait-until", "domcontentloaded", "--timeout", "30000"],
            env=env,
            timeout=40,
        )
        evidence.append(navigate_evidence)
        if navigated.returncode != 0:
            raise BridgeError("RECOVERY_UTILITY_NAVIGATION_FAILED", "recovery utility target could not open ChatGPT history")
        return {
            "target_id": utility_id,
            "created_targets": sorted(created_targets),
            "borrowed_original_url": borrowed_original_url,
            "initial_tab_ids": sorted(initial_by_id),
            "evidence": evidence,
        }

    def _cleanup_recovery_utility_targets(
        self,
        *,
        run_dir: Path,
        executable: str,
        manifest: dict[str, Any],
        utility: dict[str, Any],
        keep_target: str | None = None,
    ) -> dict[str, Any]:
        env = bridge_env(manifest)
        closed: list[str] = []
        failures: list[dict[str, Any]] = []
        for target_id in utility.get("created_targets") or []:
            if target_id == keep_target:
                continue
            completed, evidence = self._run_recovery_command(
                run_dir=run_dir,
                name=f"history-close-{target_id[:12]}",
                command=[executable, "tab-close", str(target_id), "--json"],
                env=env,
                timeout=30,
            )
            if completed.returncode == 0:
                closed.append(str(target_id))
            else:
                failures.append({"target_id": target_id, "evidence": evidence})

        borrowed_url = utility.get("borrowed_original_url")
        borrowed_target = str(utility.get("target_id") or "")
        if borrowed_url is not None and borrowed_target != keep_target:
            switched, switch_evidence = self._run_recovery_command(
                run_dir=run_dir,
                name="history-restore-borrowed-switch",
                command=[executable, "tab-switch", borrowed_target, "--json"],
                env=env,
                timeout=30,
            )
            restored, restore_evidence = self._run_recovery_command(
                run_dir=run_dir,
                name="history-restore-borrowed-url",
                command=[executable, "navigate", str(borrowed_url or "about:blank")],
                env=env,
                timeout=30,
            )
            if switched.returncode != 0 or restored.returncode != 0:
                failures.append({"target_id": borrowed_target, "evidence": [switch_evidence, restore_evidence]})

        try:
            remaining, listed = self._recovery_tabs(
                run_dir=run_dir,
                name="history-tabs-after-cleanup",
                executable=executable,
                env=env,
            )
            live_ids = {_tab_id(tab) for tab in remaining}
            expected_absent = {str(item) for item in utility.get("created_targets") or [] if item != keep_target}
            still_live = sorted(expected_absent & live_ids)
            if still_live:
                failures.append({"state": "targets-still-live", "target_ids": still_live, "evidence": listed})
        except BridgeError as exc:
            failures.append({"state": "post-cleanup-list-failed", "error": exc.envelope()})
        return {
            "ok": not failures,
            "closed_targets": closed,
            "kept_target": keep_target,
            "failures": failures,
        }

    def _recover_from_history(
        self,
        run_dir: str,
        *,
        doctor_evidence: dict[str, Any] | None = None,
        known_preexisting_target_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        state_file, record = self.store.load(run_dir)
        if record.get("phase") != "RECOVERING":
            raise BridgeError("RECOVERY_PHASE_INVALID", "history adjudication requires RECOVERING")
        manifest = _load_manifest(record)
        executable = record_executable(record)
        marker_contract = _recovery_marker_contract(record, manifest)
        candidate_limit = max(1, min(50, int(manifest.get("recovery_candidate_limit") or 20)))
        utility: dict[str, Any] | None = None
        cleanup_done = False
        try:
            utility = self._open_recovery_utility_target(
                run_dir=state_file.parent,
                executable=executable,
                manifest=manifest,
                known_preexisting_target_ids=known_preexisting_target_ids,
            )
            utility_target = str(utility["target_id"])
            tab_lifecycle = self._tab_lifecycle(executable, manifest)
            tab_lifecycle.record_owned(
                run_dir,
                target_id=utility_target,
                url="https://chatgpt.com/",
                stage="history-adjudication-utility",
            )
            seen_urls: set[str] = set()
            checked: list[dict[str, Any]] = []
            exact_matches: list[dict[str, Any]] = []
            incomplete_candidates: list[dict[str, Any]] = []
            live_tabs, live_tabs_evidence = self._recovery_tabs(
                run_dir=state_file.parent,
                name="history-live-tabs",
                executable=executable,
                env=bridge_env(manifest),
            )
            for index, tab in enumerate(live_tabs[:candidate_limit]):
                candidate_url = _tab_url(tab)
                candidate_target = _tab_id(tab)
                if (
                    candidate_target == utility_target
                    or not candidate_target
                    or not STATE.CANONICAL_CHAT_RE.fullmatch(candidate_url)
                    or candidate_url in seen_urls
                ):
                    continue
                seen_urls.add(candidate_url)
                switched, switch_evidence = self._run_recovery_command(
                    run_dir=state_file.parent,
                    name=f"history-live-switch-{index:02d}",
                    command=[executable, "tab-switch", candidate_target, "--json"],
                    env=bridge_env(manifest),
                    timeout=30,
                )
                if switched.returncode != 0:
                    incomplete = {
                        "index": f"live-{index}",
                        "title": str(tab.get("title") or ""),
                        "url": candidate_url,
                        "state": "live-tab-switch-failed",
                        "evidence": switch_evidence,
                    }
                    checked.append(incomplete)
                    incomplete_candidates.append(incomplete)
                    continue
                status_completed, status_evidence = self._run_recovery_command(
                    run_dir=state_file.parent,
                    name=f"history-live-status-{index:02d}",
                    command=[executable, "web-ai", "status", "--vendor", "chatgpt", "--url", candidate_url, "--json"],
                    env=bridge_env(manifest),
                    timeout=60,
                )
                snapshot_completed, snapshot_evidence = self._run_recovery_command(
                    run_dir=state_file.parent,
                    name=f"history-live-snapshot-{index:02d}",
                    command=[executable, "web-ai", "snapshot", "--vendor", "chatgpt", "--json"],
                    env=bridge_env(manifest),
                    timeout=60,
                )
                text_completed, text_evidence = self._run_recovery_command(
                    run_dir=state_file.parent,
                    name=f"history-live-text-{index:02d}",
                    command=[executable, "text"],
                    env=bridge_env(manifest),
                    timeout=60,
                )
                if status_completed.returncode != 0 or snapshot_completed.returncode != 0 or text_completed.returncode != 0:
                    incomplete = {
                        "index": f"live-{index}",
                        "title": str(tab.get("title") or ""),
                        "url": candidate_url,
                        "state": "live-candidate-read-failed",
                    }
                    checked.append(incomplete)
                    incomplete_candidates.append(incomplete)
                    continue
                status_payload = _json_output(status_completed.stdout)
                snapshot_payload = _json_output(snapshot_completed.stdout)
                page_text = text_completed.stdout or ""
                combined = str(snapshot_payload.get("text") or "") + "\n" + page_text
                matched = _candidate_matches_recovery_contract(combined, marker_contract)
                checked.append({
                    "index": f"live-{index}",
                    "source": "live-tab",
                    "title": str(tab.get("title") or ""),
                    "url": candidate_url,
                    "matched": matched,
                    "tabs_evidence": live_tabs_evidence,
                    "status_evidence": status_evidence,
                    "snapshot_evidence": snapshot_evidence,
                    "text_evidence": text_evidence,
                })
                if matched:
                    exact_matches.append({
                        "index": f"live-{index}",
                        "source": "live-tab",
                        "title": str(tab.get("title") or ""),
                        "url": candidate_url,
                        "target_id": candidate_target,
                        "active_evidence": switch_evidence,
                        "status_evidence": status_evidence,
                        "snapshot_evidence": snapshot_evidence,
                        "text_evidence": text_evidence,
                        "status_payload": status_payload,
                        "page_text": page_text,
                    })
                    if marker_contract.get("kind") == "run-owned-attachment":
                        break

            restored, restore_evidence = self._run_recovery_command(
                run_dir=state_file.parent,
                name="history-live-restore-utility",
                command=[executable, "tab-switch", utility_target, "--json"],
                env=bridge_env(manifest),
                timeout=30,
            )
            if restored.returncode != 0:
                raise BridgeError(
                    "RECOVERY_UTILITY_TARGET_FAILED",
                    "exact recovery utility target could not be restored after live-tab inspection",
                    {"evidence": restore_evidence},
                )

            for index in range(candidate_limit if not exact_matches else 0):
                if index:
                    switched, _ = self._run_recovery_command(
                        run_dir=state_file.parent,
                        name=f"history-root-switch-{index:02d}",
                        command=[executable, "tab-switch", utility_target, "--json"],
                        env=bridge_env(manifest),
                        timeout=30,
                    )
                    navigated, _ = self._run_recovery_command(
                        run_dir=state_file.parent,
                        name=f"history-root-navigate-{index:02d}",
                        command=[executable, "navigate", "https://chatgpt.com/", "--wait-until", "domcontentloaded", "--timeout", "30000"],
                        env=bridge_env(manifest),
                        timeout=40,
                    )
                    if switched.returncode != 0 or navigated.returncode != 0:
                        raise BridgeError("RECOVERY_UTILITY_NAVIGATION_FAILED", "could not return to the exact history utility target")

                root_snapshot, root_evidence = self._run_recovery_command(
                    run_dir=state_file.parent,
                    name=f"history-root-snapshot-{index:02d}",
                    command=[executable, "snapshot", "--interactive", "--max-nodes", "500"],
                    env=bridge_env(manifest),
                    timeout=60,
                )
                if root_snapshot.returncode != 0:
                    raise BridgeError("RECOVERY_HISTORY_SNAPSHOT_FAILED", "ChatGPT history snapshot failed", {"evidence": root_evidence})
                root_payload = {"text": root_snapshot.stdout or ""}
                recent_refs = _recent_chat_refs(root_payload, limit=candidate_limit)
                if index >= len(recent_refs):
                    break
                candidate_ref = recent_refs[index]
                clicked, click_evidence = self._run_recovery_command(
                    run_dir=state_file.parent,
                    name=f"history-candidate-click-{index:02d}",
                    command=[executable, "click", candidate_ref["ref"]],
                    env=bridge_env(manifest),
                    timeout=40,
                )
                if clicked.returncode != 0:
                    incomplete = {"index": index, "title": candidate_ref["name"], "state": "click-failed", "evidence": click_evidence}
                    checked.append(incomplete)
                    incomplete_candidates.append(incomplete)
                    continue
                active, active_evidence = self._run_recovery_command(
                    run_dir=state_file.parent,
                    name=f"history-candidate-active-{index:02d}",
                    command=[executable, "active-tab", "--json"],
                    env=bridge_env(manifest),
                    timeout=30,
                )
                if active.returncode != 0:
                    incomplete = {"index": index, "title": candidate_ref["name"], "state": "active-tab-failed", "evidence": active_evidence}
                    checked.append(incomplete)
                    incomplete_candidates.append(incomplete)
                    continue
                active_payload = _json_output(active.stdout)
                candidate_url = str(active_payload.get("url") or active_payload.get("tab", {}).get("url") or "")
                candidate_target = str(active_payload.get("targetId") or active_payload.get("target_id") or "")
                if candidate_target != utility_target or not STATE.CANONICAL_CHAT_RE.fullmatch(candidate_url):
                    incomplete = {"index": index, "title": candidate_ref["name"], "state": "noncanonical-or-target-mismatch", "url": candidate_url}
                    checked.append(incomplete)
                    incomplete_candidates.append(incomplete)
                    continue
                if candidate_url in seen_urls:
                    checked.append({"index": index, "title": candidate_ref["name"], "state": "duplicate-url", "url": candidate_url})
                    continue
                seen_urls.add(candidate_url)

                status_completed, status_evidence = self._run_recovery_command(
                    run_dir=state_file.parent,
                    name=f"history-candidate-status-{index:02d}",
                    command=[executable, "web-ai", "status", "--vendor", "chatgpt", "--url", candidate_url, "--json"],
                    env=bridge_env(manifest),
                    timeout=60,
                )
                snapshot_completed, snapshot_evidence = self._run_recovery_command(
                    run_dir=state_file.parent,
                    name=f"history-candidate-snapshot-{index:02d}",
                    command=[executable, "web-ai", "snapshot", "--vendor", "chatgpt", "--json"],
                    env=bridge_env(manifest),
                    timeout=60,
                )
                text_completed, text_evidence = self._run_recovery_command(
                    run_dir=state_file.parent,
                    name=f"history-candidate-text-{index:02d}",
                    command=[executable, "text"],
                    env=bridge_env(manifest),
                    timeout=60,
                )
                if status_completed.returncode != 0 or snapshot_completed.returncode != 0 or text_completed.returncode != 0:
                    incomplete = {"index": index, "title": candidate_ref["name"], "url": candidate_url, "state": "candidate-read-failed"}
                    checked.append(incomplete)
                    incomplete_candidates.append(incomplete)
                    continue
                status_payload = _json_output(status_completed.stdout)
                snapshot_payload = _json_output(snapshot_completed.stdout)
                page_text = text_completed.stdout or ""
                combined = str(snapshot_payload.get("text") or "") + "\n" + page_text
                matched = _candidate_matches_recovery_contract(combined, marker_contract)
                checked.append({
                    "index": index,
                    "title": candidate_ref["name"],
                    "url": candidate_url,
                    "matched": matched,
                    "status_evidence": status_evidence,
                    "snapshot_evidence": snapshot_evidence,
                    "text_evidence": text_evidence,
                })
                if not matched:
                    continue

                exact_matches.append({
                    "index": index,
                    "source": "history-utility",
                    "title": candidate_ref["name"],
                    "url": candidate_url,
                    "target_id": candidate_target,
                    "active_evidence": active_evidence,
                    "status_evidence": status_evidence,
                    "snapshot_evidence": snapshot_evidence,
                    "text_evidence": text_evidence,
                    "status_payload": status_payload,
                    "page_text": page_text,
                })

            if len(exact_matches) > 1:
                cleanup = self._cleanup_recovery_utility_targets(
                    run_dir=state_file.parent,
                    executable=executable,
                    manifest=manifest,
                    utility=utility,
                )
                cleanup_done = True
                adjudication_path = state_file.parent / "history-adjudication.json"
                write_json_atomic(adjudication_path, sanitize_evidence({
                    "schema": "codex.chatgpt.history-adjudication/v1",
                    "run_id": record["run_id"],
                    "outcome": "ambiguous-exact-matches",
                    "marker_contract": marker_contract,
                    "exact_match_urls": [item["url"] for item in exact_matches],
                    "checked": checked,
                    "cleanup": cleanup,
                    "doctor_evidence": doctor_evidence,
                }))
                return self.store.transition(
                    run_dir,
                    "BLOCKED_TARGET_AMBIGUOUS",
                    block_code="HISTORY_FINGERPRINT_AMBIGUOUS",
                    recovery_event={
                        "kind": "history-fingerprint-ambiguous",
                        "exact_match_count": len(exact_matches),
                        "adjudication_evidence": str(adjudication_path),
                        "adjudication_sha256": STATE.sha256_file(adjudication_path),
                        "cleanup": cleanup,
                    },
                )

            if (
                len(exact_matches) == 1
                and incomplete_candidates
                and marker_contract.get("kind") != "run-owned-attachment"
            ):
                cleanup = self._cleanup_recovery_utility_targets(
                    run_dir=state_file.parent,
                    executable=executable,
                    manifest=manifest,
                    utility=utility,
                )
                cleanup_done = True
                adjudication_path = state_file.parent / "history-adjudication.json"
                write_json_atomic(adjudication_path, sanitize_evidence({
                    "schema": "codex.chatgpt.history-adjudication/v1",
                    "run_id": record["run_id"],
                    "outcome": "incomplete-candidate-classification",
                    "marker_contract": marker_contract,
                    "exact_match_urls": [exact_matches[0]["url"]],
                    "incomplete_candidates": incomplete_candidates,
                    "checked": checked,
                    "cleanup": cleanup,
                    "doctor_evidence": doctor_evidence,
                }))
                return self.store.transition(
                    run_dir,
                    "BLOCKED_RECOVERY_EXHAUSTED",
                    block_code="HISTORY_CANDIDATE_CLASSIFICATION_INCOMPLETE",
                    recovery_event={
                        "kind": "history-candidate-classification-incomplete",
                        "exact_match_count": 1,
                        "incomplete_candidate_count": len(incomplete_candidates),
                        "adjudication_evidence": str(adjudication_path),
                        "adjudication_sha256": STATE.sha256_file(adjudication_path),
                        "cleanup": cleanup,
                    },
                )

            if exact_matches:
                match = exact_matches[0]
                candidate_url = str(match["url"])
                status_payload = dict(match["status_payload"])
                page_text = str(match["page_text"])
                match_evidence = {
                    "kind": "history-fingerprint-match",
                    "marker_kind": marker_contract["kind"],
                    "candidate_index": match["index"],
                    "candidate_title": match["title"],
                    "candidate_url": candidate_url,
                    "doctor_evidence": doctor_evidence,
                    "active_evidence": match["active_evidence"],
                    "status_evidence": match["status_evidence"],
                    "snapshot_evidence": match["snapshot_evidence"],
                    "text_evidence": match["text_evidence"],
                    "exact_match_count": 1,
                }
                recovered_session_id = None
                if not record.get("session_id"):
                    recovered_session_id, session_identity_evidence = self._recover_session_identity_from_store(
                        run_dir=state_file.parent,
                        executable=executable,
                        manifest=manifest,
                        record=record,
                    )
                    match_evidence["session_identity"] = session_identity_evidence
                bound = self._bind_conversation_url(
                    run_dir,
                    conversation_url=candidate_url,
                    session_id=recovered_session_id,
                    # A live-tab match identifies the original conversation
                    # target.  A history click navigates the separate utility
                    # target and must never adopt it as the submitted target.
                    target_id=(
                        str(match.get("target_id") or "") or None
                        if match.get("source") == "live-tab"
                        else None
                    ),
                    rebind_reason="history-fingerprint-match",
                    recovery_event=match_evidence,
                )
                if bound["phase"] == "BLOCKED_TARGET_AMBIGUOUS":
                    cleanup = self._cleanup_recovery_utility_targets(
                        run_dir=state_file.parent,
                        executable=executable,
                        manifest=manifest,
                        utility=utility,
                    )
                    cleanup_done = True
                    return bound

                answer = (
                    _web_multi_assistant_answer(page_text)
                    or _matching_json_answer(page_text, marker_contract)
                    or _plain_assistant_answer(page_text)
                )
                streaming = _streaming_state(status_payload)
                if streaming is False and answer:
                    terminal_error = provider_terminal_error_ui(answer)
                    if terminal_error is not None:
                        failed = self._record_provider_terminal_failure(
                            run_dir,
                            answer_text=answer,
                            provider_status="history-adjudicated-terminal",
                            command_evidence={
                                "status": match["status_evidence"],
                                "snapshot": match["snapshot_evidence"],
                                "text": match["text_evidence"],
                            },
                            detection=terminal_error,
                        )
                        cleanup = self._cleanup_recovery_utility_targets(
                            run_dir=state_file.parent,
                            executable=executable,
                            manifest=manifest,
                            utility=utility,
                        )
                        cleanup_done = True
                        return failed
                    answer_path = state_file.parent / "answer.md"
                    answer_path.write_text(answer.rstrip() + "\n", encoding="utf-8")
                    descriptor = {
                        "path": str(answer_path),
                        "sha256": STATE.sha256_file(answer_path),
                        "bytes": answer_path.stat().st_size,
                        "provider_status": "history-adjudicated-terminal",
                        "evidence": {
                            "status": match["status_evidence"],
                            "snapshot": match["snapshot_evidence"],
                            "text": match["text_evidence"],
                        },
                    }
                    self.store.transition(run_dir, "RESULT_CAPTURED", result=descriptor)
                    cleanup = self._cleanup_recovery_utility_targets(
                        run_dir=state_file.parent,
                        executable=executable,
                        manifest=manifest,
                        utility=utility,
                    )
                    cleanup_done = True
                    adjudication_path = state_file.parent / "history-adjudication.json"
                    write_json_atomic(adjudication_path, sanitize_evidence({
                        "schema": "codex.chatgpt.history-adjudication/v1",
                        "run_id": record["run_id"],
                        "outcome": "matched-complete",
                        "conversation_url": candidate_url,
                        "utility_target_id": utility_target,
                        "owned_target_id": record.get("current_target_id"),
                        "marker_contract": marker_contract,
                        "checked": checked,
                        "cleanup": cleanup,
                        "answer": descriptor,
                    }))
                    if not cleanup.get("ok"):
                        return self.store.transition(
                            run_dir,
                            "RECOVERY_REQUIRED",
                            recovery_event={
                                "kind": "history-result-captured-cleanup-pending",
                                "adjudication_evidence": str(adjudication_path),
                                "cleanup": cleanup,
                            },
                        )
                    self.store.transition(run_dir, "VERIFIED")
                    return self.store.transition(
                        run_dir,
                        "COMPLETE",
                        recovery_event={
                            "kind": "history-adjudication-complete",
                            "adjudication_evidence": str(adjudication_path),
                            "adjudication_sha256": STATE.sha256_file(adjudication_path),
                        },
                    )

                switched, switch_evidence = self._run_recovery_command(
                    run_dir=state_file.parent,
                    name="history-match-switch",
                    command=[executable, "tab-switch", utility_target, "--json"],
                    env=bridge_env(manifest),
                    timeout=30,
                )
                navigated, navigate_evidence = self._run_recovery_command(
                    run_dir=state_file.parent,
                    name="history-match-navigate",
                    command=[executable, "navigate", candidate_url, "--wait-until", "domcontentloaded", "--timeout", "30000"],
                    env=bridge_env(manifest),
                    timeout=40,
                )
                if switched.returncode != 0 or navigated.returncode != 0:
                    raise BridgeError(
                        "RECOVERY_UTILITY_NAVIGATION_FAILED",
                        "the exact unique history match could not be restored on the utility target",
                        {"switch": switch_evidence, "navigate": navigate_evidence},
                    )
                tab_lifecycle.record_protected(
                    run_dir,
                    target_id=utility_target,
                    conversation_url=candidate_url,
                    stage="history-fingerprint-observer",
                )
                cleanup = self._cleanup_recovery_utility_targets(
                    run_dir=state_file.parent,
                    executable=executable,
                    manifest=manifest,
                    utility=utility,
                    keep_target=utility_target,
                )
                cleanup_done = True
                if bound["phase"] == "URL_BOUND":
                    return self.store.transition(
                        run_dir,
                        "RESPONSE_IN_PROGRESS",
                        recovery_event={
                            "kind": "history-fingerprint-match-response-not-terminal",
                            "streaming": streaming,
                            "answer_present": bool(answer),
                            "observer_target_id": utility_target,
                            "cleanup": cleanup,
                        },
                    )
                return bound

            cleanup = self._cleanup_recovery_utility_targets(
                run_dir=state_file.parent,
                executable=executable,
                manifest=manifest,
                utility=utility,
            )
            cleanup_done = True
            adjudication_path = state_file.parent / "history-adjudication.json"
            write_json_atomic(adjudication_path, sanitize_evidence({
                "schema": "codex.chatgpt.history-adjudication/v1",
                "run_id": record["run_id"],
                "outcome": "no-exact-match",
                "marker_contract": marker_contract,
                "checked": checked,
                "cleanup": cleanup,
                "doctor_evidence": doctor_evidence,
            }))
            return self.store.transition(
                run_dir,
                "BLOCKED_RECOVERY_EXHAUSTED",
                block_code="HISTORY_FINGERPRINT_NOT_FOUND",
                recovery_event={
                    "kind": "history-fingerprint-not-found",
                    "candidate_count": len(checked),
                    "adjudication_evidence": str(adjudication_path),
                    "adjudication_sha256": STATE.sha256_file(adjudication_path),
                    "cleanup": cleanup,
                },
            )
        except Exception as exc:
            cleanup = None
            if utility is not None and not cleanup_done:
                cleanup = self._cleanup_recovery_utility_targets(
                    run_dir=state_file.parent,
                    executable=executable,
                    manifest=manifest,
                    utility=utility,
                )
            if isinstance(exc, (BridgeError, STATE.StateError)):
                detail = exc.envelope() if hasattr(exc, "envelope") else {"message": str(exc)}
            else:
                detail = {"message": _redact_sensitive_text(str(exc))}
            _, latest = self.store.load(run_dir)
            if latest.get("phase") == "RECOVERING":
                return self.store.transition(
                    run_dir,
                    "BLOCKED_RECOVERY_EXHAUSTED",
                    block_code="HISTORY_ADJUDICATION_FAILED",
                    recovery_event={"kind": "history-adjudication-failed", "detail": detail, "cleanup": cleanup},
                )
            raise

    def send(self, run_dir: str) -> dict[str, Any]:
        state_file, record = self.store.load(run_dir)
        if str(record.get("record_kind") or "standalone") == "child":
            # The per-child guard begins before app/composer preparation.  A
            # concurrent or crash-retry caller therefore observes the durable
            # claim/count and fails before any browser mutation.
            with exclusive_composer_lock(state_file.parent / "child-dispatch.lock"):
                authority = record.get("pre_submit_retry_authority")
                if (
                    record.get("phase") == "LEASED"
                    and isinstance(authority, dict)
                    and authority.get("eligible") is True
                    and authority.get("consumed_at") is None
                    and str(record.get("current_target_id") or "")
                    != str(authority.get("cleanup_target_id") or "")
                    and not authority.get("replacement_target_id")
                ):
                    target_id = str(record.get("current_target_id") or "")
                    research_refs = [
                        ref for ref in record.get("selection_evidence_refs") or []
                        if isinstance(ref, dict)
                        and str(ref.get("kind") or "") in {
                            "deep-research-selection",
                            "deep-research-app-selection",
                        }
                        and str(ref.get("target_id") or "") == target_id
                    ]
                    evidence_path = (
                        Path(str(research_refs[-1].get("path") or ""))
                        if len(research_refs) == 1
                        else state_file.parent / "composer-app-evidence.json"
                    )
                    record = self.store.confirm_child_retry_replacement(
                        run_dir,
                        target_id=target_id,
                        evidence_path=evidence_path,
                    )
                self.store.assert_child_send_available(run_dir)
                return self._send_locked(run_dir)
        return self._send_locked(run_dir)

    def _send_locked(self, run_dir: str) -> dict[str, Any]:
        state_file, record = self.store.load(run_dir)
        self.store.verify_manifest(record)
        if record["phase"] in {"SUBMISSION_UNCERTAIN_IDENTITY_MISSING", "BLOCKED_RECOVERY_EXHAUSTED"}:
            record = self._reclassify_mutation_disallowed(run_dir, record)
        initial_manifest = _load_manifest(record)
        mode_label = str(initial_manifest.get("mode_label") or "GPT-5.6").strip().casefold()
        app_policy = str(
            record.get("requested", {}).get("app_policy")
            or ("forbidden" if mode_label == "pro" else "required")
        )
        app_name = str(initial_manifest.get("chatgpt_app_name") or initial_manifest.get("app_name") or "").strip()
        if mode_label == "pro":
            if app_policy != "forbidden" or app_name:
                raise BridgeError("APP_POLICY_FORBIDDEN", "Pro requires app_policy=forbidden and no app name")
        else:
            if app_policy != "required":
                raise BridgeError("APP_POLICY_REQUIRED", "every non-Pro ChatGPT mode requires app_policy=required")
            if not app_name:
                raise BridgeError("APP_REQUIRED", "every non-Pro ChatGPT mode requires an exact app name")
        required_app = app_policy == "required"
        selection_transport = str(initial_manifest.get("app_selection_transport") or "inline-pill-reuse").strip()
        allowed_transports = {"inline-pill-reuse"}
        if required_app and selection_transport not in allowed_transports:
            raise BridgeError("APP_SELECTION_TRANSPORT_INVALID", f"unsupported app_selection_transport: {selection_transport}")
        use_preselected_app = required_app and selection_transport == "inline-pill-reuse"
        use_connected_app_auto = False
        research_mode = mode_label in {"deep research", "deep-research"}
        research_transport = str(initial_manifest.get("research_selection_transport") or "").strip()
        research_contract = str(initial_manifest.get("research_selection_contract") or "").strip()
        if research_mode:
            if app_policy != "required" or not app_name:
                raise BridgeError("RESEARCH_APP_POLICY_INVALID", "Deep Research requires the exact CodexPro app")
            if research_transport != "preselected-research":
                raise BridgeError("RESEARCH_SELECTION_TRANSPORT_INVALID", "Deep Research requires preselected-research")
            if research_contract != "codex.chatgpt.capability-selection/v1":
                raise BridgeError("RESEARCH_SELECTION_CONTRACT_INVALID", "Deep Research capability contract is missing or unsupported")
        elif research_transport or research_contract:
            raise BridgeError("RESEARCH_SELECTION_MODE_INVALID", "research selection fields require Deep Research mode")
        use_preselected_research = research_mode and research_transport == "preselected-research"
        if use_preselected_research and not use_preselected_app:
            raise BridgeError("RESEARCH_APP_SELECTION_REQUIRED", "Deep Research must preselect the exact app before its capability")
        use_prepared_target = use_preselected_app or use_connected_app_auto or use_preselected_research
        lock_context = exclusive_composer_lock(
            self.store.root / "global-dispatch.lock",
            timeout_seconds=composer_lock_timeout_seconds(initial_manifest),
        )
        prepared_target_id: str | None = None
        pre_send_target_ids: set[str] = set()
        pre_send_tabs_evidence: dict[str, Any] | None = None
        composer_evidence_path: Path | None = None
        tab_lifecycle = None
        composer_url = "https://chatgpt.com/"
        with lock_context:
            if record["phase"] in {"PREFLIGHT_BLOCKED", "BLOCKED_APP_TRANSACTION"}:
                if record.get("session_id") or record.get("conversation_url"):
                    raise BridgeError("PRE_SUBMIT_RETRY_IDENTITY_CONFLICT", "blocked pre-submit run unexpectedly carries submission identity")
                record = self.store.transition(run_dir, "PREFLIGHTED")
            if record["phase"] == "SEND_REJECTED":
                record = self.store.transition(run_dir, "PREFLIGHTED")
            if record["phase"] not in {"PREFLIGHTED", "LEASED"}:
                raise BridgeError("SEND_PHASE_INVALID", f"send not allowed in phase {record['phase']}")
            manifest = _load_manifest(record)
            executable = record_executable(record)
            composer_url = str(manifest.get("provider_url") or manifest.get("chatgpt_url") or "https://chatgpt.com/")
            tab_lifecycle = self._tab_lifecycle(executable, manifest)
            if self.headed_runtime_preflight:
                record = self._ensure_headed_runtime(
                    run_dir=run_dir,
                    state_file=state_file,
                    record=record,
                    manifest=manifest,
                    executable=executable,
                    lifecycle=tab_lifecycle,
                )
                if record["phase"] == "PREFLIGHT_BLOCKED":
                    return record
            if record["phase"] == "PREFLIGHTED":
                record = self.ensure_app(run_dir)
                record = self.store.transition(run_dir, "LEASED")
            authority = record.get("pre_submit_retry_authority") if isinstance(record.get("pre_submit_retry_authority"), dict) else {}
            reuse_prepared_retry = bool(
                record.get("phase") == "LEASED"
                and authority.get("eligible") is True
                and authority.get("consumed_at") is None
                and str(authority.get("replacement_target_id") or "") == str(record.get("current_target_id") or "")
                and str(authority.get("replacement_evidence_sha256") or "")
                == STATE.sha256_file(Path(str(authority.get("replacement_evidence_path") or "")))
            ) if authority.get("replacement_evidence_path") else False
            if authority.get("replacement_target_id") and not reuse_prepared_retry:
                # A retry replacement is immutable once bound.  In particular,
                # do not turn missing/tampered persisted research proof into a
                # fresh tab (and therefore a possible duplicate send attempt).
                raise BridgeError(
                    "PRE_SUBMIT_RETRY_REPLACEMENT_EVIDENCE_INVALID",
                    "prepared retry replacement evidence is missing or no longer immutable",
                )
            if use_preselected_research:
                connector = self._research_composer(executable)
            elif use_prepared_target:
                connector = self._app_connector(executable)
            else:
                connector = None
            if reuse_prepared_retry:
                prepared_target_id = str(record.get("current_target_id") or "")
                composer_evidence_path = Path(str(authority["replacement_evidence_path"]))
                if use_preselected_research:
                    try:
                        selection = json.loads(composer_evidence_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                        raise BridgeError("RESEARCH_SELECTION_EVIDENCE_INVALID", "retry research evidence is unreadable") from exc
                    if not (
                        isinstance(selection, dict)
                        and selection.get("schema") == "codex.chatgpt.capability-selection/v1"
                        and selection.get("state") == "deep-research-selected"
                        and str(selection.get("run_id") or "") == str(record.get("run_id") or "")
                        and str(selection.get("target_id") or "") == prepared_target_id
                    ):
                        raise BridgeError("RESEARCH_SELECTION_EVIDENCE_INVALID", "retry research evidence identity is not exact")
                    try:
                        connector.restore_selection_evidence(selection)
                    except Exception as exc:
                        raise BridgeError(
                            "RESEARCH_SELECTION_EVIDENCE_INVALID",
                            "retry research transition proof could not be restored",
                        ) from exc
            elif use_preselected_research:
                correlation = manifest.get("workflow_correlation") if isinstance(manifest.get("workflow_correlation"), dict) else {}
                workflow_id = str(
                    correlation.get("workflow_id")
                    or record.get("parent_workflow_id")
                    or record.get("workflow_id")
                    or record.get("run_id")
                    or ""
                )
                try:
                    composer_result = connector.prepare(
                        run_id=str(record.get("run_id") or ""),
                        workflow_id=workflow_id,
                        app_name=app_name,
                        composer_url=composer_url,
                    )
                except Exception as exc:
                    cleanup = self._safe_tab_cleanup(
                        tab_lifecycle,
                        run_dir,
                        target_id=self._owned_target_from_exception(exc),
                        url=composer_url,
                        reason="research-composer-preparation-failed",
                    )
                    blocked = self.store.transition(
                        run_dir,
                        "PREFLIGHT_BLOCKED",
                        block_code="DEEP_RESEARCH_CAPABILITY_UNPROVEN",
                        recovery_event={
                            "kind": "research-composer-preparation-failed",
                            "detail": _redact_sensitive_text(str(exc)),
                            "cleanup": cleanup,
                        },
                    )
                    raise BridgeError(
                        "DEEP_RESEARCH_CAPABILITY_UNPROVEN",
                        "exact @심층 리서치 selection could not be proven before send",
                        {"phase": blocked["phase"], "cleanup": cleanup},
                    ) from exc
                required_hashes = (
                    "token_sha256",
                    "before_snapshot_sha256",
                    "after_snapshot_sha256",
                    "action_transcript_sha256",
                )
                if not (
                    composer_result.get("schema") == "codex.chatgpt.capability-selection/v1"
                    and composer_result.get("state") == "deep-research-selected"
                    and str(composer_result.get("run_id") or "") == str(record.get("run_id") or "")
                    and str(composer_result.get("workflow_id") or "") == workflow_id
                    and str(composer_result.get("app_name") or "") == app_name
                    and str(composer_result.get("app_selection_method") or "") == "exact-at-mention-then-tab"
                    and str(composer_result.get("app_mention_text_sha256") or "")
                    == hashlib.sha256(f"@{app_name}".encode("utf-8")).hexdigest()
                    and str(composer_result.get("target_id") or "")
                    and str(composer_result.get("selection_transport") or "") == "preselected-research"
                    and str(composer_result.get("token_sha256") or "")
                    == hashlib.sha256("@심층 리서치".encode("utf-8")).hexdigest()
                    and all(re.fullmatch(r"[0-9a-f]{64}", str(composer_result.get(key) or "")) for key in required_hashes)
                    and isinstance(composer_result.get("selected_marker"), dict)
                ):
                    invalid_target = str(composer_result.get("target_id") or "") or None
                    cleanup = self._safe_tab_cleanup(
                        tab_lifecycle,
                        run_dir,
                        target_id=invalid_target,
                        url=str(composer_result.get("url") or composer_url),
                        reason="research-selection-evidence-invalid",
                    )
                    blocked = self.store.transition(
                        run_dir,
                        "PREFLIGHT_BLOCKED",
                        block_code="RESEARCH_SELECTION_EVIDENCE_INVALID",
                        recovery_event={"kind": "research-selection-evidence-invalid", "cleanup": cleanup},
                    )
                    raise BridgeError(
                        "RESEARCH_SELECTION_EVIDENCE_INVALID",
                        "Deep Research selection evidence did not bind exact token, run, workflow, and target identities",
                        {"phase": blocked["phase"], "cleanup": cleanup},
                    )
                evidence_suffix = hashlib.sha256(str(composer_result["target_id"]).encode("utf-8")).hexdigest()[:12]
                record, prepared_target_id, composer_evidence_path = self._bind_prepared_composer(
                    run_dir=run_dir,
                    state_file=state_file,
                    record=record,
                    composer_result=composer_result,
                    lifecycle=tab_lifecycle,
                    evidence_filename=f"composer-research-evidence-{evidence_suffix}.json",
                    selection_kind="deep-research-app-selection",
                )
            elif use_preselected_app:
                app_name = str(manifest.get("chatgpt_app_name") or manifest.get("app_name") or "").strip()
                try:
                    composer_result = connector.prepare_composer_app(
                        app_name,
                        composer_url=composer_url,
                    )
                except Exception as exc:
                    cleanup = self._safe_tab_cleanup(
                        tab_lifecycle,
                        run_dir,
                        target_id=self._owned_target_from_exception(exc),
                        url=composer_url,
                        reason="app-composer-preparation-failed",
                    )
                    blocked = self.store.transition(
                        run_dir,
                        "PREFLIGHT_BLOCKED",
                        block_code="APP_COMPOSER_PREP_FAILED",
                        recovery_event={
                            "kind": "app-composer-preparation-failed",
                            "detail": _redact_sensitive_text(str(exc)),
                            "cleanup": cleanup,
                        },
                    )
                    raise BridgeError(
                        "APP_COMPOSER_PREP_FAILED",
                        "exact app mention could not be entered and confirmed with Tab",
                        {"phase": blocked["phase"], "detail": _redact_sensitive_text(str(exc)), "cleanup": cleanup},
                    ) from exc
                expected_mention_hash = hashlib.sha256(f"@{app_name}".encode("utf-8")).hexdigest()
                if not (
                    composer_result.get("state") == "composer-app-mention-tab-confirmed"
                    and str(composer_result.get("app_name") or "") == app_name
                    and str(composer_result.get("target_id") or "")
                    and str(composer_result.get("selection_method") or "") == "exact-at-mention-then-tab"
                    and str(composer_result.get("mention_text_sha256") or "") == expected_mention_hash
                ):
                    invalid_target = str(composer_result.get("target_id") or "") or None
                    cleanup = self._safe_tab_cleanup(
                        tab_lifecycle,
                        run_dir,
                        target_id=invalid_target,
                        url=str(composer_result.get("url") or composer_url),
                        reason="app-selection-evidence-missing",
                    )
                    blocked = self.store.transition(
                        run_dir,
                        "PREFLIGHT_BLOCKED",
                        block_code="APP_SELECTION_EVIDENCE_MISSING",
                        recovery_event={"kind": "app-selection-evidence-missing", "cleanup": cleanup},
                    )
                    raise BridgeError(
                        "APP_SELECTION_EVIDENCE_MISSING",
                        "required app send needs an exact @app mention, one Tab confirmation, and matching immutable mention evidence",
                        {"phase": blocked["phase"], "app_name": app_name, "cleanup": cleanup},
                    )
                record, prepared_target_id, composer_evidence_path = self._bind_prepared_composer(
                    run_dir=run_dir,
                    state_file=state_file,
                    record=record,
                    composer_result=composer_result,
                    lifecycle=tab_lifecycle,
                )
            elif use_connected_app_auto:
                app_name = str(manifest.get("chatgpt_app_name") or manifest.get("app_name") or "").strip()
                try:
                    composer_result = connector.prepare_connected_app_chat(
                        app_name,
                        composer_url=composer_url,
                    )
                except Exception as exc:
                    cleanup = self._safe_tab_cleanup(
                        tab_lifecycle,
                        run_dir,
                        target_id=self._owned_target_from_exception(exc),
                        url=composer_url,
                        reason="app-chat-surface-preparation-failed",
                    )
                    blocked = self.store.transition(
                        run_dir,
                        "PREFLIGHT_BLOCKED",
                        block_code="APP_CHAT_SURFACE_PREP_FAILED",
                        recovery_event={
                            "kind": "app-chat-surface-preparation-failed",
                            "detail": _redact_sensitive_text(str(exc)),
                            "cleanup": cleanup,
                        },
                    )
                    raise BridgeError(
                        "APP_CHAT_SURFACE_PREP_FAILED",
                        "connected-app Chat surface could not be prepared",
                        {"phase": blocked["phase"], "detail": _redact_sensitive_text(str(exc)), "cleanup": cleanup},
                    ) from exc
                record, prepared_target_id, composer_evidence_path = self._bind_prepared_composer(
                    run_dir=run_dir,
                    state_file=state_file,
                    record=record,
                    composer_result=composer_result,
                    lifecycle=tab_lifecycle,
                )
            command = build_send_command(
                record,
                manifest,
                executable,
                preselected_app=use_preselected_app,
                connected_app_auto=use_connected_app_auto,
                preselected_research=use_preselected_research,
            )
            command_budget = pre_send_command_budget(command)
            if not command_budget["within_budget"]:
                evidence_path = state_file.parent / "agbrowse-evidence" / "pre-send-command-budget.json"
                write_json_atomic(evidence_path, command_budget)
                cleanup = self._safe_tab_cleanup(
                    tab_lifecycle,
                    run_dir,
                    target_id=prepared_target_id,
                    url=composer_url,
                    reason="pre-send-command-budget-exceeded",
                )
                return self.store.transition(
                    run_dir,
                    "PREFLIGHT_BLOCKED",
                    block_code="WINDOWS_COMMAND_LINE_TOO_LONG",
                    recovery_event={
                        "kind": "pre-submit-command-budget-exceeded",
                        "error_code": "WINDOWS_COMMAND_LINE_TOO_LONG",
                        "evidence": str(evidence_path),
                        "cleanup": cleanup,
                        **command_budget,
                    },
                )
            if use_prepared_target:
                try:
                    if connector is None:
                        raise BridgeError("PREPARED_CONTROLLER_MISSING", "prepared target activation requires a controller")
                    if use_preselected_research:
                        final_check = connector.verify_selected(str(prepared_target_id or ""))
                        if not (
                            final_check.get("schema") == "codex.chatgpt.capability-selection-final-check/v1"
                            and final_check.get("state") == "deep-research-selected"
                            and str(final_check.get("target_id") or "") == str(prepared_target_id or "")
                            and re.fullmatch(r"[0-9a-f]{64}", str(final_check.get("snapshot_sha256") or ""))
                        ):
                            raise BridgeError("RESEARCH_SELECTION_STALE", "final Deep Research selection state is not exact")
                        final_path = state_file.parent / "composer-research-final-check.json"
                        write_json_atomic(final_path, sanitize_evidence(final_check))
                        record = self.store.transition(
                            run_dir,
                            "LEASED",
                            selection_evidence_ref={
                                "kind": "deep-research-final-check",
                                "path": str(final_path),
                                "sha256": STATE.sha256_file(final_path),
                                "target_id": str(prepared_target_id or ""),
                            },
                        )
                    else:
                        connector.activate_composer_target(str(prepared_target_id or ""))
                except Exception as exc:
                    cleanup = self._safe_tab_cleanup(
                        tab_lifecycle,
                        run_dir,
                        target_id=prepared_target_id,
                        url=composer_url,
                        reason="prepared-target-activation-failed",
                    )
                    blocked = self.store.transition(
                        run_dir,
                        "PREFLIGHT_BLOCKED",
                        block_code=("RESEARCH_SELECTION_STALE" if use_preselected_research else "APP_COMPOSER_TARGET_ACTIVATION_FAILED"),
                        recovery_event={
                            "kind": ("research-selection-final-check-failed" if use_preselected_research else "app-composer-target-activation-failed"),
                            "detail": _redact_sensitive_text(str(exc)),
                            "cleanup": cleanup,
                        },
                    )
                    raise BridgeError(
                        "RESEARCH_SELECTION_STALE" if use_preselected_research else "APP_COMPOSER_TARGET_ACTIVATION_FAILED",
                        "exact prepared research/app composer target could not be verified",
                        {"phase": blocked["phase"], "detail": _redact_sensitive_text(str(exc)), "cleanup": cleanup},
                    ) from exc
            send_env = bridge_env(manifest)
            # The explicit headed preflight above owns browser creation.  If
            # Chrome disappears after that proof, fail before mutation instead
            # of silently starting a different runtime mode.
            send_env["AGBROWSE_WEB_AI_AUTO_START"] = "0"
            if str(record.get("record_kind") or "standalone") == "child":
                send_env, record = self._capacity_retry_environment(
                    run_dir=run_dir,
                    record=record,
                    manifest=manifest,
                    lifecycle=tab_lifecycle,
                )
            if not use_prepared_target:
                pre_send_tabs, pre_send_tabs_evidence = self._recovery_tabs(
                    run_dir=state_file.parent,
                    name="pre-send-tabs",
                    executable=executable,
                    env=send_env,
                )
                pre_send_target_ids = {_tab_id(tab) for tab in pre_send_tabs if _tab_id(tab)}
            if str(record.get("record_kind") or "standalone") == "child":
                record = self.store.claim_child_send(run_dir)
            else:
                record = self.store.transition(run_dir, "SEND_STARTED")
            try:
                completed = self.runner(command, send_env, int(manifest.get("send_timeout_seconds") or 180))
            except FileNotFoundError as exc:
                evidence_path = state_file.parent / "agbrowse-evidence" / "send-process-not-created.json"
                evidence_payload = {
                    "schema": "codex.chatgpt.send-process-not-created/v1",
                    "kind": "send-runner-process-not-created",
                    "mutation_allowed": False,
                    "exception_type": type(exc).__name__,
                    "errno": exc.errno,
                    "filename": str(exc.filename or ""),
                    "command_executable": str(command[0] if command else ""),
                    "command_executable_exists": bool(command and Path(str(command[0])).is_file()),
                    "command_line_sha256": sha256_bytes(subprocess.list2cmdline(command).encode("utf-8")),
                }
                write_json_atomic(evidence_path, evidence_payload)
                recovery_event = {
                    **evidence_payload,
                    "evidence_path": str(evidence_path),
                    "evidence_sha256": STATE.sha256_file(evidence_path),
                }
                rejected = self.store.transition(
                    run_dir,
                    "SEND_REJECTED",
                    block_code="SEND_PROCESS_NOT_CREATED",
                    recovery_event=recovery_event,
                )
                cleanup = self._safe_tab_cleanup(
                    tab_lifecycle,
                    run_dir,
                    target_id=prepared_target_id,
                    url=composer_url,
                    reason="send-process-not-created",
                )
                return self.store.transition(
                    run_dir,
                    "SEND_REJECTED",
                    recovery_event={
                        "kind": "verified-pre-submit-tab-cleanup",
                        "prior_phase": rejected["phase"],
                        "cleanup": cleanup,
                    },
                )
            except Exception as exc:
                self.store.transition(run_dir, "SUBMISSION_UNCERTAIN_IDENTITY_MISSING", block_code="SEND_RUNNER_EXCEPTION")
                raise BridgeError("SEND_RUNNER_EXCEPTION", "agbrowse send runner raised after mutation boundary", {"detail": str(exc)}) from exc
        evidence = self._evidence(state_file.parent, "send", completed)
        try:
            payload = _completed_json_output(completed)
        except BridgeError:
            self.store.transition(run_dir, "SUBMISSION_UNCERTAIN_IDENTITY_MISSING", block_code="AGBROWSE_JSON_INVALID")
            raise
        envelope = normalize_envelope(payload)
        session_id = str(envelope.get("session_id") or "")
        target_id = str(envelope.get("target_id") or "") or None
        url = envelope.get("conversation_url")
        warnings = payload.get("warnings") if isinstance(payload, dict) else []
        warnings = warnings if isinstance(warnings, list) else []
        app_selection_failed = any("plugin not selected" in str(item).casefold() for item in warnings)
        if required_app and selection_transport == "legacy-plugin-parallel" and app_selection_failed:
            return self.store.transition(
                run_dir,
                "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
                block_code="APP_SELECTION_UNCERTAIN",
                recovery_event={"kind": "required-app-not-selected", "warnings": warnings, "evidence": evidence},
            )
        session_identity_evidence = None
        shown_session: dict[str, Any] = {}
        if envelope["ok"] and session_id:
            try:
                shown_target, shown_url, session_identity_evidence, shown_session = self._show_session_identity(
                    executable=executable,
                    manifest=manifest,
                    session_id=session_id,
                    run_dir=state_file.parent,
                )
                target_id = target_id or shown_target
                url = url if url and STATE.CANONICAL_CHAT_RE.fullmatch(str(url)) else shown_url
            except Exception as exc:
                session_identity_evidence = {"error": str(exc)}
        if envelope["ok"] and session_id and use_prepared_target and target_id != prepared_target_id:
            return self.store.transition(
                run_dir,
                "RECOVERY_REQUIRED",
                session_id=session_id,
                recovery_event={
                    "kind": "prepared-target-send-target-mismatch",
                    "expected_target_id": prepared_target_id,
                    "actual_target_id": target_id,
                    "session_identity_evidence": session_identity_evidence,
                },
            )
        if envelope["ok"] and session_id and not target_id:
            return self.store.transition(
                run_dir,
                "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
                block_code="TARGET_ID_MISSING_AFTER_SEND",
                recovery_event={"kind": "session-target-id-missing", "evidence": session_identity_evidence},
            )
        if envelope["ok"] and session_id:
            if not use_prepared_target and str(target_id) in pre_send_target_ids:
                return self.store.transition(
                    run_dir,
                    "RECOVERY_REQUIRED",
                    session_id=session_id,
                    recovery_event={
                        "kind": "send-returned-preexisting-target",
                        "target_id": target_id,
                        "pre_send_tabs_evidence": pre_send_tabs_evidence,
                        "session_identity_evidence": session_identity_evidence,
                    },
                )
            post_send_observation, post_send_tabs_evidence = self._observe_post_send_target(
                run_dir=state_file.parent,
                executable=executable,
                manifest=manifest,
                target_id=str(target_id),
            )
            if post_send_observation.get("state") == "canonical":
                url = str(post_send_observation["url"])
            elif post_send_observation.get("state") in {"absent", "ambiguous", "drifted"}:
                return self.store.transition(
                    run_dir,
                    "RECOVERY_REQUIRED",
                    session_id=session_id,
                    recovery_event={
                        "kind": "post-send-target-unusable",
                        "target_id": target_id,
                        "observation": sanitize_evidence(post_send_observation),
                        "tabs_evidence": post_send_tabs_evidence,
                    },
                )
            elif session_send_not_committed(shown_session):
                rejected = self.store.transition(
                    run_dir,
                    "SEND_REJECTED",
                    session_id=session_id,
                    target_id=target_id,
                    recovery_event={
                        "kind": "verified-send-click-not-committed",
                        "observation": sanitize_evidence(post_send_observation),
                        "session_identity_evidence": session_identity_evidence,
                        "tabs_evidence": post_send_tabs_evidence,
                    },
                )
                if not use_prepared_target:
                    tab_lifecycle.record_owned(
                        run_dir,
                        target_id=str(target_id),
                        url=str(post_send_observation.get("url") or composer_url),
                        stage="rejected-send-root",
                    )
                cleanup = self._safe_tab_cleanup(
                    tab_lifecycle,
                    run_dir,
                    target_id=str(target_id),
                    url=str(post_send_observation.get("url") or composer_url),
                    reason="verified-send-click-not-committed",
                )
                if cleanup.get("ok"):
                    return self.store.transition(
                        run_dir,
                        "CANCELLED_PRE_SUBMISSION",
                        recovery_event={"kind": "verified-send-root-cleaned", "cleanup": cleanup},
                    )
                return rejected
            if not use_prepared_target:
                tab_lifecycle.record_owned(
                    run_dir,
                    target_id=str(target_id),
                    url=str(url or composer_url),
                    stage="send-created-target",
                )
            receipt = {
                "command": command[:4] + ["<redacted-args>"],
                "evidence": evidence,
                "composer_app_evidence": str(composer_evidence_path) if composer_evidence_path else None,
                "session_identity_evidence": session_identity_evidence,
                "pre_send_tabs_evidence": pre_send_tabs_evidence,
                "post_send_observation": sanitize_evidence(post_send_observation),
                "post_send_tabs_evidence": post_send_tabs_evidence,
                "payload_sha256": sha256_bytes(json.dumps(payload, sort_keys=True).encode("utf-8")),
            }
            submitted = self.store.transition(
                run_dir,
                "SUBMITTED",
                session_id=session_id,
                target_id=target_id,
                submission_receipt=receipt,
            )
            if url and STATE.CANONICAL_CHAT_RE.fullmatch(str(url)):
                submitted = self._bind_conversation_url(
                    run_dir,
                    conversation_url=str(url),
                    target_id=target_id,
                )
            try:
                tab_lifecycle.record_protected(
                    run_dir,
                    target_id=str(target_id or ""),
                    conversation_url=str(submitted.get("conversation_url") or url or ""),
                    stage="submitted",
                )
            except Exception as exc:
                return self.store.transition(
                    run_dir,
                    "RECOVERY_REQUIRED",
                    recovery_event={
                        "kind": "submitted-tab-protection-evidence-failed",
                        "target_id": target_id,
                        "detail": _redact_sensitive_text(str(exc)),
                    },
                )
            return submitted

        phase = classify_pre_submit_failure(envelope)
        if phase == "SEND_REJECTED":
            rejected = self.store.transition(
                run_dir,
                "SEND_REJECTED",
                recovery_event={"kind": "pre-submit-rejection", "error": envelope, "evidence": evidence},
            )
            cleanup = self._safe_tab_cleanup(
                tab_lifecycle,
                run_dir,
                target_id=prepared_target_id,
                url=composer_url,
                reason="verified-pre-submit-send-rejection",
            )
            return self.store.transition(
                run_dir,
                "SEND_REJECTED",
                recovery_event={"kind": "verified-pre-submit-tab-cleanup", "cleanup": cleanup},
            )
        return self.store.transition(run_dir, "SUBMISSION_UNCERTAIN_IDENTITY_MISSING", block_code=envelope["error_code"] or "SEND_UNCERTAIN")

    def poll(self, run_dir: str, *, timeout_seconds: int | None = None) -> dict[str, Any]:
        state_file, record = self.store.load(run_dir)
        self.store.verify_manifest(record)
        if not record.get("session_id"):
            raise BridgeError("SESSION_ID_MISSING", "poll requires exact session_id")
        if record["phase"] == "RECOVERY_REQUIRED":
            record = self.store.transition(run_dir, "RECOVERING")
        if record["phase"] not in {"SUBMITTED", "URL_BOUND", "RESPONSE_IN_PROGRESS", "RECOVERING"}:
            raise BridgeError("POLL_PHASE_INVALID", f"poll not allowed in phase {record['phase']}")
        # A completed exact URL is stronger evidence than a stale/crashed
        # session command. Settle it before any hours-long poll timeout.
        if record.get("conversation_url") and record.get("phase") == "RECOVERING":
            direct = self._try_exact_url_terminal_now(run_dir)
            if direct.get("phase") in {"COMPLETE", "PROVIDER_FAILED_TERMINAL", "BLOCKED_TARGET_AMBIGUOUS"}:
                return direct
            _, record = self.store.load(run_dir)
        manifest = _load_manifest(record)
        executable = record_executable(record)
        timeout = int(timeout_seconds or manifest.get("timeout_seconds") or 1800)
        saved_url = str(record.get("conversation_url") or "")
        target_id = str(record.get("current_target_id") or "")
        if not STATE.CANONICAL_CHAT_RE.fullmatch(saved_url) or not target_id:
            return self.store.transition(
                run_dir,
                "RECOVERY_REQUIRED",
                recovery_event={
                    "kind": "poll-requires-exact-bound-conversation",
                    "session_id": record.get("session_id"),
                    "target_id": target_id or None,
                    "conversation_url": saved_url or None,
                },
            )
        try:
            tabs, tabs_evidence = self._recovery_tabs(
                run_dir=state_file.parent,
                name="poll-exact-target-preflight",
                executable=executable,
                env=bridge_env(manifest),
            )
            observation = exact_target_observation(tabs, target_id)
        except BridgeError as exc:
            return self.store.transition(
                run_dir,
                "RECOVERY_REQUIRED",
                recovery_event={"kind": "poll-target-preflight-failed", "detail": exc.envelope()},
            )
        if (
            observation.get("state") != "canonical"
            or str(observation.get("url") or "") != STATE.canonical_conversation_url(saved_url)
        ):
            return self.store.transition(
                run_dir,
                "RECOVERY_REQUIRED",
                recovery_event={
                    "kind": "poll-exact-target-drift",
                    "observation": sanitize_evidence(observation),
                    "tabs_evidence": tabs_evidence,
                },
            )
        command = build_exact_poll_command(executable, str(record["session_id"]), timeout)
        try:
            completed = self.runner(command, bridge_env(manifest), timeout + 30)
        except Exception as exc:
            self.store.transition(
                run_dir,
                "RECOVERY_REQUIRED",
                recovery_event={"kind": "poll-runner-exception", "detail": str(exc)},
            )
            raise BridgeError("POLL_RUNNER_EXCEPTION", "poll interrupted; exact session retained", {"detail": str(exc)}) from exc
        evidence = self._evidence(state_file.parent, "poll", completed)
        try:
            payload = _json_output(completed.stdout)
        except BridgeError as exc:
            self.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": exc.code, "evidence": evidence})
            raise
        envelope = normalize_envelope(payload)
        url = envelope.get("conversation_url")
        target_id = str(envelope.get("target_id") or "") or None
        if url and not record.get("conversation_url") and STATE.CANONICAL_CHAT_RE.fullmatch(str(url)):
            record = self._bind_conversation_url(
                run_dir,
                conversation_url=str(url),
                target_id=target_id,
            )
            if record["phase"] == "BLOCKED_TARGET_AMBIGUOUS":
                return record
        status = str(envelope.get("status") or "").lower()
        answer = str(envelope.get("answer_text") or "").strip()
        terminal = status in {"complete", "completed", "done", "response_ready"}
        if envelope["ok"] and terminal and answer:
            terminal_error = provider_terminal_error_ui(answer)
            if terminal_error is not None:
                return self._record_provider_terminal_failure(
                    run_dir,
                    answer_text=answer,
                    provider_status=status,
                    command_evidence=evidence,
                    detection=terminal_error,
                )
            answer_path = state_file.parent / "answer.md"
            answer_path.write_text(answer + "\n", encoding="utf-8")
            descriptor = {
                "path": str(answer_path),
                "sha256": STATE.sha256_file(answer_path),
                "bytes": answer_path.stat().st_size,
                "provider_status": status,
                "evidence": evidence,
            }
            if record["phase"] == "SUBMITTED":
                if not record.get("conversation_url"):
                    return self.store.transition(
                        run_dir,
                        "RECOVERY_REQUIRED",
                        recovery_event={"kind": "terminal-without-canonical-url", "evidence": evidence},
                    )
                record = self.store.transition(run_dir, "URL_BOUND")
            record = self.store.transition(run_dir, "RESULT_CAPTURED", result=descriptor)
            record = self.store.transition(run_dir, "VERIFIED")
            return self.store.transition(run_dir, "COMPLETE")
        if envelope["ok"] and status in {"sent", "polling", "running", "pending", "response_in_progress"}:
            if record.get("conversation_url") and record["phase"] in {"SUBMITTED", "URL_BOUND", "RECOVERING"}:
                if record["phase"] == "SUBMITTED":
                    record = self.store.transition(run_dir, "URL_BOUND")
                return self.store.transition(run_dir, "RESPONSE_IN_PROGRESS")
            return record
        return self.store.transition(
            run_dir,
            "RECOVERY_REQUIRED",
            recovery_event={"kind": "poll-not-terminal", "error": envelope, "evidence": evidence},
        )

    def recover(self, run_dir: str) -> dict[str, Any]:
        state_file, record = self.store.load(run_dir)
        self.store.verify_manifest(record)
        if record["phase"] in {
            "SEND_STARTED",
            "RECOVERY_REQUIRED",
            "RESPONSE_IN_PROGRESS",
            "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
            "BLOCKED_RECOVERY_EXHAUSTED",
        }:
            record = self.store.transition(run_dir, "RECOVERING")
        if record["phase"] != "RECOVERING":
            raise BridgeError("RECOVERY_PHASE_INVALID", "recovery requires a recoverable uncertain or interrupted phase")
        manifest = _load_manifest(record)
        executable = record_executable(record)
        known_preexisting_target_ids: set[str] = set()
        try:
            pre_doctor_tabs, _ = self._recovery_tabs(
                run_dir=state_file.parent,
                name="recovery-tabs-before-doctor",
                executable=executable,
                env=bridge_env(manifest),
            )
            known_preexisting_target_ids = {_tab_id(tab) for tab in pre_doctor_tabs if _tab_id(tab)}
        except BridgeError:
            known_preexisting_target_ids = set()
        # The exact canonical URL is the shortest recovery authority. Do not
        # wait for doctor/history or stale command expiry when it is terminal.
        direct = self._try_exact_url_terminal_now(
            run_dir,
            tabs=pre_doctor_tabs if "pre_doctor_tabs" in locals() else None,
        )
        if direct.get("phase") in {"COMPLETE", "PROVIDER_FAILED_TERMINAL", "BLOCKED_TARGET_AMBIGUOUS"}:
            return direct
        # Once an exact conversation URL exists, stale agbrowse session URLs
        # are never allowed to navigate that target.  A later invocation will
        # observe the same exact URL again; doctor/history are only identity
        # discovery paths for runs that still lack a canonical conversation.
        if STATE.CANONICAL_CHAT_RE.fullmatch(str(direct.get("conversation_url") or "")):
            return direct
        doctor_evidence: dict[str, Any] | None = None
        if record.get("session_id"):
            command = [
                executable,
                "web-ai",
                "sessions",
                "doctor",
                str(record["session_id"]),
                "--navigate",
                "--json",
            ]
            completed = self.runner(command, bridge_env(manifest), int(manifest.get("recovery_timeout_seconds") or 120))
            doctor_evidence = self._evidence(state_file.parent, "recovery-doctor", completed)
            try:
                payload = _json_output(completed.stdout)
            except BridgeError as exc:
                payload = {"ok": False, "status": "doctor-json-invalid", "error": exc.envelope()}
            envelope = normalize_envelope(payload)
            doctor_url = str(envelope.get("conversation_url") or "")
            saved_url = str(record.get("conversation_url") or "")
            doctor_canonical = (
                STATE.canonical_conversation_url(doctor_url)
                if STATE.CANONICAL_CHAT_RE.fullmatch(doctor_url)
                else ""
            )
            saved_canonical = (
                STATE.canonical_conversation_url(saved_url)
                if STATE.CANONICAL_CHAT_RE.fullmatch(saved_url)
                else ""
            )
            if doctor_canonical and saved_canonical and doctor_canonical != saved_canonical:
                return self.store.transition(
                    run_dir,
                    "BLOCKED_TARGET_AMBIGUOUS",
                    block_code="RECOVERY_DOCTOR_CANONICAL_URL_MISMATCH",
                    recovery_event={
                        "kind": "doctor-canonical-url-mismatch",
                        "doctor_url": doctor_canonical,
                        "saved_url": saved_canonical,
                        "evidence": doctor_evidence,
                    },
                )
            # A rebooted/stale session doctor may report the ChatGPT root
            # composer.  It must never displace an already persisted exact
            # conversation identity.
            url = saved_canonical or doctor_canonical
            doctor_target_id = str(envelope.get("target_id") or "")
            target_id = (
                record.get("current_target_id")
                if saved_canonical and not doctor_canonical
                else doctor_target_id or record.get("current_target_id")
            )
            if envelope["ok"] and url and STATE.CANONICAL_CHAT_RE.fullmatch(str(url)):
                live_target_evidence: dict[str, Any] | None = None
                try:
                    after_tabs, tabs_evidence = self._recovery_tabs(
                        run_dir=state_file.parent,
                        name="recovery-tabs-after-doctor",
                        executable=executable,
                        env=bridge_env(manifest),
                    )
                    canonical = STATE.canonical_conversation_url(str(url))
                    matches = []
                    for tab in after_tabs:
                        try:
                            if STATE.canonical_conversation_url(_tab_url(tab)) == canonical:
                                matches.append(tab)
                        except STATE.StateError:
                            continue
                    if len(matches) > 1:
                        return self.store.transition(
                            run_dir,
                            "BLOCKED_TARGET_AMBIGUOUS",
                            block_code="RECOVERY_DOCTOR_URL_AMBIGUOUS",
                            recovery_event={
                                "kind": "doctor-live-target-ambiguous",
                                "conversation_url": canonical,
                                "target_ids": [_tab_id(tab) for tab in matches],
                                "tabs_evidence": tabs_evidence,
                            },
                        )
                    if len(matches) == 1:
                        candidate_target = _tab_id(matches[0])
                        if (
                            candidate_target
                            and (
                                candidate_target == str(record.get("current_target_id") or "")
                                or candidate_target == str(target_id or "")
                                or candidate_target not in known_preexisting_target_ids
                            )
                        ):
                            target_id = candidate_target
                            live_target_evidence = {
                                "target_id": candidate_target,
                                "conversation_url": canonical,
                                "new_after_doctor": candidate_target not in known_preexisting_target_ids,
                                "tabs_evidence": tabs_evidence,
                            }
                        else:
                            return self.store.transition(
                                run_dir,
                                "BLOCKED_TARGET_AMBIGUOUS",
                                block_code="RECOVERY_DOCTOR_PREEXISTING_TARGET_UNOWNED",
                                recovery_event={
                                    "kind": "doctor-live-target-preexisting-unowned",
                                    "conversation_url": canonical,
                                    "target_id": candidate_target,
                                    "tabs_evidence": tabs_evidence,
                                },
                            )
                except BridgeError:
                    live_target_evidence = None
                bound = self._bind_conversation_url(
                    run_dir,
                    conversation_url=str(url),
                    target_id=str(target_id) if target_id else None,
                    rebind_reason="agbrowse-session-doctor",
                    recovery_event={
                        "kind": "doctor-reattach",
                        "evidence": doctor_evidence,
                        "live_target": live_target_evidence,
                    },
                )
                if bound.get("phase") == "BLOCKED_TARGET_AMBIGUOUS":
                    return bound
                return self._recover_exact_bound_url_terminal(
                    run_dir,
                    doctor_evidence=doctor_evidence,
                )
            doctor_evidence = {"command_evidence": doctor_evidence, "envelope": sanitize_evidence(envelope)}
        return self._recover_from_history(
            run_dir,
            doctor_evidence=doctor_evidence,
            known_preexisting_target_ids=known_preexisting_target_ids,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single-backend ChatGPT bridge for an exact contract-validated agbrowse CLI.")
    parser.add_argument("--state-root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--project-root", required=True)
    prepare.add_argument("--manifest", required=True)
    prepare.add_argument("--contract", required=True)
    for name in (
        "send",
        "poll",
        "recover",
        "show",
        "reclassify-pre-submit",
        "authorize-pre-submit-retry",
        "retire-uncommitted-session",
    ):
        item = sub.add_parser(name)
        item.add_argument("--run", required=True)
        if name == "poll":
            item.add_argument("--timeout-seconds", type=int)
    cleanup = sub.add_parser("cleanup-completed")
    cleanup.add_argument("--run", required=True)
    # Backward-compatible no-op; durable COMPLETE plus exact ownership is authority.
    cleanup.add_argument("--explicit-user-request", action="store_true", help=argparse.SUPPRESS)
    abandon = sub.add_parser("abandon-uncertain")
    abandon.add_argument("--run", required=True)
    abandon.add_argument("--explicit-user-request", action="store_true")
    abandon.add_argument("--reason", required=True)
    confirm_stop = sub.add_parser("confirm-user-stop")
    confirm_stop.add_argument("--run", required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    bridge = Bridge(state_root=args.state_root)
    try:
        if args.command == "prepare":
            result = bridge.prepare(project_root=args.project_root, manifest_path=args.manifest, contract_path=args.contract)
        elif args.command == "send":
            result = bridge.send(args.run)
        elif args.command == "poll":
            result = bridge.poll(args.run, timeout_seconds=args.timeout_seconds)
        elif args.command == "recover":
            result = bridge.recover(args.run)
        elif args.command == "reclassify-pre-submit":
            result = bridge.reclassify_pre_submit(args.run)
        elif args.command == "authorize-pre-submit-retry":
            result = bridge.authorize_pre_submit_retry(args.run)
        elif args.command == "retire-uncommitted-session":
            result = bridge.retire_uncommitted_session(args.run)
        elif args.command == "cleanup-completed":
            result = bridge.cleanup_completed(args.run, explicit_user_request=args.explicit_user_request)
        elif args.command == "abandon-uncertain":
            result = bridge.abandon_uncertain(
                args.run,
                explicit_user_request=args.explicit_user_request,
                reason=args.reason,
            )
        elif args.command == "confirm-user-stop":
            result = bridge.confirm_user_stop(args.run)
        else:
            _, result = bridge.store.load(args.run)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except (BridgeError, STATE.StateError) as exc:
        envelope = exc.envelope() if hasattr(exc, "envelope") else {"ok": False, "error": {"message": str(exc)}}
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
