from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
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


def app_decision_scope_matches(run_root: Path, decision_root: Path) -> bool:
    """Allow an exact workspace app or the single app scoped to its drive root."""
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
    if variant not in {"high", "높음"}:
        raise BridgeError("MODE_VARIANT_UNSUPPORTED", "new regular GPT work requires mode_variant=High")
    return ["--family", "gpt-5.6-sol", "--model", "thinking", "--effort", "high"]


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
        state_file, record = self.store.load(run_dir)
        if not explicit_user_request:
            raise BridgeError(
                "USER_STOP_AUTHORIZATION_REQUIRED",
                "uncertain post-send work can be abandoned only after an explicit user request",
            )
        if record.get("phase") == "ABANDONED_UNCERTAIN":
            return record
        if record.get("phase") in {"COMPLETE", "CANCELLED_PRE_SUBMISSION"}:
            raise BridgeError(
                "USER_STOP_PHASE_INVALID",
                f"run is already terminal in phase {record.get('phase')}",
            )

        authorization = {
            "schema": "codex.chatgpt.user-stop-authorization/v1",
            "explicit_user_request": True,
            "mutation_may_have_occurred": True,
            "duplicate_risk_acknowledged": True,
            "reason": str(reason or "").strip(),
            "run_id": record.get("run_id"),
            "project_root": record.get("project_root"),
            "session_id": record.get("session_id"),
            "target_id": record.get("current_target_id"),
            "conversation_url": record.get("conversation_url"),
        }
        record = self.store.begin_user_stop(run_dir, authorization=authorization)

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
        terminal_statuses = {"complete", "completed", "done", "cancelled", "canceled"}
        active_statuses = {"created", "sent", "polling", "running", "pending", "response_in_progress", "streaming"}
        terminal_session = bool(session_id and observed_session_id == session_id and session_status in terminal_statuses)

        effective_target_id = stored_target_id or observed_target_id
        matching_tabs = [row for row in tabs if isinstance(row, dict) and str(row.get("targetId") or "") == effective_target_id]
        exact_target_live = len(matching_tabs) == 1
        exact_target_url = str(matching_tabs[0].get("url") or "") if exact_target_live else None
        identity_match = bool(session_id and observed_session_id == session_id)
        if stored_target_id and observed_target_id and stored_target_id != observed_target_id:
            identity_match = False
        if len(matching_tabs) > 1:
            identity_match = False
        if exact_target_live and observed_url and exact_target_url != observed_url:
            identity_match = False
        if exact_target_live and stored_url and exact_target_url != stored_url:
            identity_match = False
        if session_id and observed_session_id == session_id and session_status == "timeout" and not exact_target_live:
            terminal_session = True

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
                    terminal_session = poll_status in terminal_statuses or (
                        poll_status == "timeout" and not exact_target_live
                    )
                    if poll_status:
                        session_status = poll_status
            except Exception as exc:
                commands.append({"name": "stop-or-poll", "exception": _redact_sensitive_text(str(exc))})
            try:
                _, after_payload = run_probe(
                    "session-after",
                    [executable, "web-ai", "sessions", "show", session_id, "--json"],
                )
                if isinstance(after_payload, dict):
                    after_session = after_payload.get("session")
                    after_session = after_session if isinstance(after_session, dict) else after_payload
                    after_id = str(after_session.get("sessionId") or after_session.get("session_id") or "")
                    after_status = str(after_session.get("status") or "").casefold()
                    if after_id == session_id and after_status:
                        session_status = after_status
                        terminal_session = after_status in terminal_statuses or (
                            after_status == "timeout" and not exact_target_live
                        )
            except Exception as exc:
                commands.append({"name": "session-after", "exception": _redact_sensitive_text(str(exc))})

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
        return self.store.finalize_user_stop(run_dir, confirmation=confirmation)

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
    ) -> dict[str, Any]:
        if self.app_identity_probe:
            result = self.app_identity_probe(public_url, expected_root, expected_port, timeout)
        else:
            helper = _load_app_identity_module()
            result = helper.probe_codexpro_identity(
                public_url,
                expected_root,
                expected_port,
                timeout=timeout,
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
                if not app_decision_scope_matches(run_root, decision_root):
                    raise BridgeError("APP_DECISION_MISMATCH", "decision project root does not match run")
                expected_url = str(decision.get("public_url") or "").strip()
                expected_port = int(decision["port"]) if decision.get("port") is not None else None
                identity = self._verify_app_identity(
                    public_url=expected_url,
                    expected_root=str(decision_root),
                    expected_port=expected_port,
                    timeout=int(manifest.get("app_identity_timeout_seconds") or 15),
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
    ) -> tuple[str | None, str | None, dict[str, Any]]:
        command = [executable, "web-ai", "sessions", "show", session_id, "--json"]
        completed = self.runner(command, bridge_env(manifest), int(manifest.get("session_show_timeout_seconds") or 60))
        evidence = self._evidence(run_dir, "session-show", completed)
        payload = _json_output(completed.stdout)
        session = payload.get("session") if isinstance(payload.get("session"), dict) else payload
        target_id = str(session.get("targetId") or session.get("target_id") or "") or None
        url = session.get("conversationUrl") or session.get("conversation_url") or session.get("url")
        canonical_url = str(url) if url and STATE.CANONICAL_CHAT_RE.fullmatch(str(url)) else None
        return target_id, canonical_url, evidence

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
        if record.get("phase") not in {"SUBMISSION_UNCERTAIN_IDENTITY_MISSING", "BLOCKED_RECOVERY_EXHAUSTED"}:
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
            and deadline_expired
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
                "session_deadline_expired": True,
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
            "session_deadline_expired": True,
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

            if len(exact_matches) == 1 and incomplete_candidates:
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
        if envelope["ok"] and session_id and (not target_id or use_prepared_target):
            try:
                shown_target, shown_url, session_identity_evidence = self._show_session_identity(
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
            receipt = {
                "command": command[:4] + ["<redacted-args>"],
                "evidence": evidence,
                "composer_app_evidence": str(composer_evidence_path) if composer_evidence_path else None,
                "session_identity_evidence": session_identity_evidence,
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
        manifest = _load_manifest(record)
        executable = record_executable(record)
        timeout = int(timeout_seconds or manifest.get("timeout_seconds") or 1800)
        command = [
            executable,
            "web-ai",
            "poll",
            "--vendor",
            "chatgpt",
            "--session",
            str(record["session_id"]),
            "--navigate",
            "--timeout",
            str(timeout),
            "--json",
        ]
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
            url = envelope.get("conversation_url") or record.get("conversation_url")
            target_id = str(envelope.get("target_id") or "") or record.get("current_target_id")
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
                return self._bind_conversation_url(
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
