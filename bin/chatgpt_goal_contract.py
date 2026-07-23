from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Mapping, Sequence

GOAL_MANIFEST_SCHEMA = "codex.chatgpt.goal-supervisor/v1"
GOAL_STATE_SCHEMA = "codex.chatgpt.goal-state/v1"
GOAL_CYCLE_SCHEMA = "codex.chatgpt.goal-cycle/v1"
GOAL_CYCLE_RESULT_SCHEMA = "codex.chatgpt.goal-cycle-result/v1"
GOAL_HOST_GATES_SCHEMA = "codex.chatgpt.goal-host-gates/v1"
GOAL_EVENT_SCHEMA = "codex.chatgpt.goal-event/v1"
GOAL_USER_ACTION_SCHEMA = "codex.chatgpt.goal-user-action/v1"
GOAL_REPAIR_SCHEMA = "codex.chatgpt.goal-repair-message/v1"
GOAL_CHECK_REGISTRY_SCHEMA = "codex.chatgpt.goal-check-registry/v1"

PHASES = {
    "CREATED",
    "CYCLE_READY",
    "WEB_ACTIVE",
    "HOST_VERIFYING",
    "REPAIR_ACTIVE",
    "WAITING_USER",
    "GOAL_COMPLETE",
    "FAILED_CLOSED",
}
TERMINAL_PHASES = {"WAITING_USER", "GOAL_COMPLETE", "FAILED_CLOSED"}
TRANSITIONS = {
    "CREATED": {"CYCLE_READY", "FAILED_CLOSED"},
    "CYCLE_READY": {"WEB_ACTIVE", "WAITING_USER", "FAILED_CLOSED"},
    "WEB_ACTIVE": {"HOST_VERIFYING", "REPAIR_ACTIVE", "WAITING_USER", "FAILED_CLOSED"},
    "HOST_VERIFYING": {"CYCLE_READY", "GOAL_COMPLETE", "REPAIR_ACTIVE", "WAITING_USER", "FAILED_CLOSED"},
    "REPAIR_ACTIVE": {"CYCLE_READY", "WEB_ACTIVE", "HOST_VERIFYING", "WAITING_USER", "FAILED_CLOSED"},
    "WAITING_USER": {"CYCLE_READY", "WEB_ACTIVE", "HOST_VERIFYING", "FAILED_CLOSED"},
    "GOAL_COMPLETE": set(),
    "FAILED_CLOSED": set(),
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class GoalContractError(ValueError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strict_utf8_bytes(value: str, *, field: str, max_bytes: int = 2_000_000) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise GoalContractError("TEXT_REQUIRED", field)
    if "\ufffd" in value or "???" in value:
        raise GoalContractError("TEXT_CORRUPT", field)
    try:
        data = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise GoalContractError("TEXT_UTF8_INVALID", field) from exc
    if len(data) > max_bytes:
        raise GoalContractError("TEXT_TOO_LARGE", field)
    return data


def load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise GoalContractError("JSON_FILE_MISSING", str(path))
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise GoalContractError("JSON_BOM_FORBIDDEN", str(path))
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if isinstance(exc, GoalContractError):
            raise
        raise GoalContractError("JSON_INVALID", str(path)) from exc
    if not isinstance(value, dict):
        raise GoalContractError("JSON_OBJECT_REQUIRED", str(path))
    return value


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
    os.replace(tmp, path)


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(value)
    os.replace(tmp, path)


def write_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    write_immutable_bytes(path, data)


def write_immutable_bytes(path: Path, value: bytes) -> None:
    if path.exists():
        if path.is_symlink() or path.read_bytes() != value:
            raise GoalContractError("IMMUTABLE_ARTIFACT_CONFLICT", str(path))
        return
    atomic_write_bytes(path, value)


def artifact_ref(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise GoalContractError("ARTIFACT_MISSING", str(path))
    return {"path": str(path.resolve()), "sha256": file_sha256(path), "bytes": path.stat().st_size}


def validate_artifact_ref(value: Mapping[str, Any], *, parent: Path | None = None) -> Path:
    if set(value) - {"path", "sha256", "bytes", "role"}:
        raise GoalContractError("ARTIFACT_REF_KEYS_INVALID")
    path_text = value.get("path")
    expected = value.get("sha256")
    if not isinstance(path_text, str) or not _SHA256_RE.fullmatch(str(expected or "")):
        raise GoalContractError("ARTIFACT_REF_INVALID")
    path = Path(path_text).expanduser().resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise GoalContractError("ARTIFACT_REF_INVALID", str(path))
    if parent is not None and parent.resolve() not in path.parents:
        raise GoalContractError("ARTIFACT_REF_OUTSIDE_PARENT", str(path))
    if file_sha256(path) != expected:
        raise GoalContractError("ARTIFACT_HASH_MISMATCH", str(path))
    if "bytes" in value and value["bytes"] != path.stat().st_size:
        raise GoalContractError("ARTIFACT_SIZE_MISMATCH", str(path))
    return path


def _require_exact_keys(value: Mapping[str, Any], required: set[str], optional: set[str] = set()) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise GoalContractError("REQUIRED_KEYS_MISSING", ",".join(sorted(missing)))
    if extra:
        raise GoalContractError("UNKNOWN_KEYS", ",".join(sorted(extra)))


def _require_strings(values: Any, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise GoalContractError("STRING_LIST_INVALID", field)
    if not allow_empty and not values:
        raise GoalContractError("STRING_LIST_EMPTY", field)
    if any(not item.strip() or len(item) > 1000 for item in values):
        raise GoalContractError("STRING_LIST_INVALID", field)
    return list(values)


def validate_goal_manifest(value: Mapping[str, Any], *, manifest_path: Path | None = None) -> dict[str, Any]:
    required = {"schema", "goal_id", "goal", "project", "context", "gates", "acceptance", "policy"}
    optional = {"output_dir", "agbrowse_contract", "check_registry"}
    _require_exact_keys(value, required, optional)
    if value.get("schema") != GOAL_MANIFEST_SCHEMA:
        raise GoalContractError("GOAL_MANIFEST_SCHEMA_INVALID")
    goal_id = value.get("goal_id")
    if not isinstance(goal_id, str) or not _ID_RE.fullmatch(goal_id):
        raise GoalContractError("GOAL_ID_INVALID")
    strict_utf8_bytes(str(value.get("goal") or ""), field="goal")
    project = value.get("project")
    if not isinstance(project, Mapping):
        raise GoalContractError("PROJECT_INVALID")
    _require_exact_keys(
        project,
        {"root", "chatgpt_app_name", "allowed_write_paths"},
        {"handoff_root", "forbidden_paths", "automation_repo_root"},
    )
    root = Path(str(project.get("root") or "")).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise GoalContractError("PROJECT_ROOT_INVALID", str(root))
    if not isinstance(project.get("chatgpt_app_name"), str) or not str(project["chatgpt_app_name"]).strip():
        raise GoalContractError("CHATGPT_APP_REQUIRED")
    allowed = _require_strings(project.get("allowed_write_paths"), "allowed_write_paths", allow_empty=False)
    for item in allowed:
        candidate = (root / item).resolve(strict=True) if not Path(item).is_absolute() else Path(item).resolve(strict=True)
        if root != candidate and root not in candidate.parents:
            raise GoalContractError("ALLOWED_WRITE_PATH_OUTSIDE_PROJECT", item)
    context = value.get("context")
    if not isinstance(context, Mapping):
        raise GoalContractError("CONTEXT_INVALID")
    _require_exact_keys(context, {"candidate_paths"}, {"policy_paths"})
    _require_strings(context.get("candidate_paths"), "candidate_paths", allow_empty=False)
    _require_strings(context.get("policy_paths") or [], "policy_paths")
    gates = value.get("gates")
    if not isinstance(gates, Mapping) or set(gates) != {"research", "advisory"}:
        raise GoalContractError("GATES_INVALID")
    for name in ("research", "advisory"):
        gate = gates.get(name)
        if not isinstance(gate, Mapping):
            raise GoalContractError("GATE_INVALID", name)
        required_gate = {"policy", "triggers"} | ({"affected_components", "cross_component_interfaces", "contradiction_evidence"} if name == "advisory" else set())
        _require_exact_keys(gate, required_gate)
        if gate.get("policy") not in {"auto", "require", "skip"}:
            raise GoalContractError("GATE_POLICY_INVALID", name)
        _require_strings(gate.get("triggers"), f"{name}.triggers")
    acceptance = value.get("acceptance")
    if not isinstance(acceptance, Mapping):
        raise GoalContractError("ACCEPTANCE_INVALID")
    _require_exact_keys(acceptance, {"criteria", "required_check_ids"})
    _require_strings(acceptance.get("criteria"), "criteria", allow_empty=False)
    _require_strings(acceptance.get("required_check_ids"), "required_check_ids")
    policy = value.get("policy")
    if not isinstance(policy, Mapping):
        raise GoalContractError("POLICY_INVALID")
    _require_exact_keys(
        policy,
        set(),
        {"max_cycles", "stagnation_limit", "automatic_repair", "repair_attempts_per_family", "target_commit", "target_push"},
    )
    max_cycles = policy.get("max_cycles", 20)
    stagnation = policy.get("stagnation_limit", 3)
    repairs = policy.get("repair_attempts_per_family", 2)
    if not isinstance(max_cycles, int) or isinstance(max_cycles, bool) or not 1 <= max_cycles <= 20:
        raise GoalContractError("MAX_CYCLES_INVALID")
    if not isinstance(stagnation, int) or isinstance(stagnation, bool) or not 2 <= stagnation <= 10:
        raise GoalContractError("STAGNATION_LIMIT_INVALID")
    if not isinstance(repairs, int) or isinstance(repairs, bool) or not 0 <= repairs <= 2:
        raise GoalContractError("REPAIR_BUDGET_INVALID")
    for flag in ("automatic_repair", "target_commit", "target_push"):
        if flag in policy and not isinstance(policy[flag], bool):
            raise GoalContractError("POLICY_FLAG_INVALID", flag)
    if policy.get("target_push") and not policy.get("target_commit"):
        raise GoalContractError("TARGET_PUSH_REQUIRES_COMMIT")
    if value.get("check_registry") is not None:
        ref = value["check_registry"]
        if not isinstance(ref, Mapping):
            raise GoalContractError("CHECK_REGISTRY_REF_INVALID")
        registry_path = validate_artifact_ref(ref)
        registry = validate_check_registry(load_json(registry_path), project_root=root)
        missing_checks = set(acceptance["required_check_ids"]) - set(registry["checks"])
        if missing_checks:
            raise GoalContractError("REQUIRED_CHECK_UNKNOWN", ",".join(sorted(missing_checks)))
    elif acceptance["required_check_ids"]:
        raise GoalContractError("CHECK_REGISTRY_REQUIRED")
    return dict(value)


def validate_check_registry(value: Mapping[str, Any], *, project_root: Path) -> dict[str, Any]:
    _require_exact_keys(value, {"schema", "checks"})
    if value.get("schema") != GOAL_CHECK_REGISTRY_SCHEMA or not isinstance(value.get("checks"), Mapping):
        raise GoalContractError("CHECK_REGISTRY_INVALID")
    checks: dict[str, Any] = {}
    for check_id, raw in value["checks"].items():
        if not isinstance(check_id, str) or not _ID_RE.fullmatch(check_id) or not isinstance(raw, Mapping):
            raise GoalContractError("CHECK_DEFINITION_INVALID", str(check_id))
        _require_exact_keys(raw, {"argv"}, {"cwd", "timeout_seconds", "expected_exit_codes"})
        argv = _require_strings(raw.get("argv"), f"checks.{check_id}.argv", allow_empty=False)
        cwd_text = str(raw.get("cwd") or ".")
        cwd = (project_root / cwd_text).resolve(strict=True)
        if project_root != cwd and project_root not in cwd.parents:
            raise GoalContractError("CHECK_CWD_OUTSIDE_PROJECT", check_id)
        timeout = raw.get("timeout_seconds", 300)
        expected = raw.get("expected_exit_codes", [0])
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise GoalContractError("CHECK_TIMEOUT_INVALID", check_id)
        if not isinstance(expected, list) or not expected or any(not isinstance(item, int) or isinstance(item, bool) for item in expected):
            raise GoalContractError("CHECK_EXIT_CODES_INVALID", check_id)
        checks[check_id] = {"argv": argv, "cwd": str(cwd), "timeout_seconds": timeout, "expected_exit_codes": list(expected)}
    return {"schema": GOAL_CHECK_REGISTRY_SCHEMA, "checks": checks}


def validate_transition(old_phase: str, new_phase: str) -> None:
    if old_phase not in PHASES or new_phase not in PHASES or new_phase not in TRANSITIONS[old_phase]:
        raise GoalContractError("GOAL_TRANSITION_INVALID", f"{old_phase}->{new_phase}")


def validate_goal_state(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "goal_id", "manifest_sha256", "revision", "phase", "cycle_index", "max_cycles",
        "active_action", "active_workflow", "last_cycle_result", "last_host_gates", "repair_families",
        "progress_fingerprints", "boundary", "final", "created_at", "updated_at",
    }
    _require_exact_keys(value, required)
    if value.get("schema") != GOAL_STATE_SCHEMA:
        raise GoalContractError("GOAL_STATE_SCHEMA_INVALID")
    if not isinstance(value.get("goal_id"), str) or not _ID_RE.fullmatch(str(value["goal_id"])):
        raise GoalContractError("GOAL_STATE_ID_INVALID")
    if not _SHA256_RE.fullmatch(str(value.get("manifest_sha256") or "")):
        raise GoalContractError("GOAL_STATE_MANIFEST_HASH_INVALID")
    if value.get("phase") not in PHASES:
        raise GoalContractError("GOAL_PHASE_INVALID")
    for field in ("revision", "cycle_index", "max_cycles"):
        if not isinstance(value.get(field), int) or isinstance(value.get(field), bool) or value[field] < 0:
            raise GoalContractError("GOAL_STATE_COUNTER_INVALID", field)
    if not isinstance(value.get("repair_families"), Mapping) or not isinstance(value.get("progress_fingerprints"), list):
        raise GoalContractError("GOAL_STATE_COLLECTION_INVALID")
    return dict(value)


def validate_goal_cycle_result(value: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "workflow_id", "goal_id", "cycle_index", "stage", "attempt_index", "nonce", "question_sha256",
        "source_snapshot_sha256", "original_goal_sha256", "mission_sha256", "input_plan_sha256",
        "input_research_descriptor_sha256", "input_advisory_descriptor_sha256", "input_review_sha256",
        "implementation_status", "decision", "summary", "criterion_claims", "remaining_work", "changed_files",
        "commands", "blockers", "requested_host_check_ids", "next_mission_body",
        "next_mission_on_gate_failure", "user_action",
    }
    _require_exact_keys(value, required)
    if value.get("schema") != GOAL_CYCLE_RESULT_SCHEMA:
        raise GoalContractError("GOAL_CYCLE_RESULT_SCHEMA_INVALID")
    for key in (
        "workflow_id", "goal_id", "cycle_index", "stage", "attempt_index", "nonce", "question_sha256",
        "source_snapshot_sha256", "original_goal_sha256", "mission_sha256", "input_plan_sha256",
        "input_research_descriptor_sha256", "input_advisory_descriptor_sha256", "input_review_sha256",
    ):
        if value.get(key) != expected.get(key):
            raise GoalContractError("GOAL_CYCLE_BINDING_MISMATCH", key)
    if value.get("implementation_status") not in {"complete", "blocked", "stale-or-conflicted"}:
        raise GoalContractError("GOAL_IMPLEMENTATION_STATUS_INVALID")
    decision = value.get("decision")
    if decision not in {"CONTINUE", "GOAL_COMPLETE", "USER_ACTION_REQUIRED"}:
        raise GoalContractError("GOAL_DECISION_INVALID")
    if not isinstance(value.get("summary"), str) or not value["summary"].strip() or len(value["summary"]) > 4000:
        raise GoalContractError("GOAL_SUMMARY_INVALID")
    for key in ("remaining_work", "changed_files", "commands", "blockers", "requested_host_check_ids"):
        _require_strings(value.get(key), key)
    claims = value.get("criterion_claims")
    if not isinstance(claims, list):
        raise GoalContractError("CRITERION_CLAIMS_INVALID")
    normalized_claims = []
    for claim in claims:
        if not isinstance(claim, Mapping) or set(claim) != {"criterion", "status", "evidence_refs"}:
            raise GoalContractError("CRITERION_CLAIM_INVALID")
        if claim.get("criterion") not in expected.get("criteria", []):
            raise GoalContractError("CRITERION_CLAIM_UNKNOWN", str(claim.get("criterion")))
        if claim.get("status") not in {"satisfied", "unsatisfied", "unknown"}:
            raise GoalContractError("CRITERION_STATUS_INVALID")
        _require_strings(claim.get("evidence_refs"), "evidence_refs")
        normalized_claims.append(dict(claim))
    if len({claim["criterion"] for claim in normalized_claims}) != len(normalized_claims):
        raise GoalContractError("CRITERION_CLAIM_DUPLICATE")
    next_mission = value.get("next_mission_body")
    fallback_mission = value.get("next_mission_on_gate_failure")
    user_action = value.get("user_action")
    if decision == "CONTINUE":
        strict_utf8_bytes(next_mission, field="next_mission_body")
        if user_action is not None:
            raise GoalContractError("CONTINUE_USER_ACTION_FORBIDDEN")
    elif decision == "GOAL_COMPLETE":
        if value.get("implementation_status") != "complete" or value.get("blockers") or user_action is not None:
            raise GoalContractError("GOAL_COMPLETE_BRANCH_INVALID")
        strict_utf8_bytes(fallback_mission, field="next_mission_on_gate_failure")
        if next_mission is not None:
            raise GoalContractError("GOAL_COMPLETE_NEXT_MISSION_FORBIDDEN")
        claim_map = {item["criterion"]: item["status"] for item in normalized_claims}
        if any(claim_map.get(item) != "satisfied" for item in expected.get("criteria", [])):
            raise GoalContractError("GOAL_COMPLETE_CRITERIA_UNSATISFIED")
    else:
        if not isinstance(user_action, Mapping) or set(user_action) != {"code", "message", "resume_conditions"}:
            raise GoalContractError("USER_ACTION_INVALID")
        if not isinstance(user_action.get("code"), str) or not _ID_RE.fullmatch(user_action["code"]):
            raise GoalContractError("USER_ACTION_CODE_INVALID")
        strict_utf8_bytes(str(user_action.get("message") or ""), field="user_action.message", max_bytes=16000)
        _require_strings(user_action.get("resume_conditions"), "user_action.resume_conditions", allow_empty=False)
        if next_mission is not None or fallback_mission is not None:
            raise GoalContractError("USER_ACTION_MISSION_FORBIDDEN")
    return dict(value)


def progress_fingerprint(cycle_result: Mapping[str, Any], host_gates: Mapping[str, Any], next_mission_sha256: str | None) -> str:
    vector = [
        {"check_id": item.get("check_id"), "passed": item.get("passed"), "returncode": item.get("returncode")}
        for item in host_gates.get("checks", []) if isinstance(item, Mapping)
    ]
    return canonical_sha256({
        "changed_files": cycle_result.get("changed_files", []),
        "remaining_work": cycle_result.get("remaining_work", []),
        "blockers": cycle_result.get("blockers", []),
        "gate_vector": vector,
        "next_mission_sha256": next_mission_sha256,
    })


def project_key(root: Path) -> str:
    normalized = os.path.normcase(str(root.resolve())).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def next_event_path(events_dir: Path) -> Path:
    events_dir.mkdir(parents=True, exist_ok=True)
    indexes = []
    for path in events_dir.glob("*.json"):
        try:
            indexes.append(int(path.stem))
        except ValueError:
            continue
    index = max(indexes, default=0) + 1
    if index > 10_000:
        raise GoalContractError("EVENT_LIMIT_EXHAUSTED")
    return events_dir / f"{index:06d}.json"


def append_event(events_dir: Path, event: Mapping[str, Any]) -> Path:
    path = next_event_path(events_dir)
    write_immutable_json(path, {"schema": GOAL_EVENT_SCHEMA, **dict(event)})
    return path


def ensure_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise GoalContractError("SHA256_INVALID", field)
    return value


def validate_check_ids(requested: Sequence[str], allowed: Sequence[str]) -> list[str]:
    values = _require_strings(list(requested), "requested_host_check_ids")
    unknown = set(values) - set(allowed)
    if unknown:
        raise GoalContractError("HOST_CHECK_NOT_ALLOWED", ",".join(sorted(unknown)))
    return list(dict.fromkeys(values))
