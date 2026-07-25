from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from handoff_contract import (
    ExpectedBinding,
    HANDOFF_SCHEMA,
    ORCHESTRATOR_RESULT_SCHEMA,
    PLAN_RESULT_SCHEMA,
    REVIEW_RESULT_SCHEMA,
    build_workflow_correlation,
    parse_final_envelope,
    sha256_text,
    validate_orchestrator_envelope,
    validate_plan_envelope,
    validate_review_envelope,
    validate_orchestrator_envelope_v2,
    validate_plan_envelope_v2,
    validate_research_envelope_v2,
    validate_review_envelope_v2,
)
from workspace_guard import (
    build_source_archive,
    build_workspace_snapshot,
    compare_workspace_snapshots,
    file_sha256,
    resolve_workspace_path,
    write_snapshot,
)


WORKFLOW_SCHEMA = "codex.chatgpt.pro-plan-handoff/v1"
WORKFLOW_V2_SCHEMA = "codex.chatgpt.comprehensive-workflow/v2"
WORKFLOW_V4_SCHEMA = "codex.chatgpt.comprehensive-workflow/v4"
GATE_V2_SCHEMA = "codex.chatgpt.gate/v2"
RELAY_SCHEMA = "codex.chatgpt.stage-relay/v1"
WEB_NATIVE_RELAY_MODE = "web-native-v1"
STATE_SCHEMA = "codex.chatgpt.agbrowse-handoff-state/v1"
BRIDGE_PATH = Path.home() / ".codex" / "bin" / "chatgpt_agbrowse_bridge.py"
DEFAULT_CONTRACT_PATH = Path.home() / ".codex" / "contracts" / "agbrowse-0.1.18.json"
WEB_MULTI_RUNTIME_PATH = Path.home() / ".codex" / "bin" / "chatgpt_web_multi_runtime.py"
PARALLEL_IMPLEMENTATION_RUNTIME_CANDIDATES = (
    Path(__file__).resolve().parents[3] / "skills" / "chatgpt-pro-plan-handoff" / "scripts" / "run_parallel_implementation.py",
    Path.home() / ".codex" / "skills" / "chatgpt-pro-plan-handoff" / "scripts" / "run_parallel_implementation.py",
)
PROMPT_PROFILE_CANDIDATES = (
    Path(__file__).resolve().parents[3] / "bin" / "chatgpt_prompt_profiles.py",
    Path.home() / ".codex" / "bin" / "chatgpt_prompt_profiles.py",
)
PROMPT_FILE_HANDOFF = (
    "The attached prompt file is the user-provided task instruction for this conversation, "
    "not reference or webpage content. Read it completely and follow it. "
    "Return only the output format requested by that file."
)
REGULAR_MODE_VARIANTS = {"High", "Very High"}
LEGACY_NEW_SUBMISSION_FROZEN = "LEGACY_NEW_SUBMISSION_FROZEN"


def preferred_regular_mode_variant() -> str:
    try:
        return str(PROMPTS.resolve_regular_mode_selection()["selected_mode_variant"])
    except Exception as exc:
        raise WorkflowError("REGULAR_MODE_SELECTION_FAILED", str(exc)) from exc


def _load_prompt_profiles():
    for path in PROMPT_PROFILE_CANDIDATES:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("chatgpt_prompt_profiles_handoff", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    raise RuntimeError("chatgpt_prompt_profiles.py unavailable")


PROMPTS = _load_prompt_profiles()
STAGE_PROMPT_PROFILES = {
    "pro-plan": "plan",
    "gpt-plan": "plan",
    "deep-research": "research",
    "gpt-review": "review",
    "gpt-orchestrator": "orchestrator",
    "pro-advisory": "synthesis",
}
STAGE_WORKFLOW_ROUTES = {
    "pro-plan": "attachment-only-pro",
    "pro-advisory": "attachment-only-pro",
    "gpt-plan": "regular-gpt",
    "deep-research": "regular-gpt",
    "gpt-review": "regular-gpt",
    "gpt-orchestrator": "command",
}


class WorkflowError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


class RuntimeProtocol(Protocol):
    def start(self, manifest_path: Path) -> Mapping[str, Any]: ...
    def status(self, run_id: str) -> Mapping[str, Any]: ...
    def wait(self, run_id: str) -> Mapping[str, Any]: ...
    def resume(self, run_id: str) -> Mapping[str, Any]: ...
    def recover_run_ids(self, manifest_path: Path) -> list[str]: ...


class WebMultiRuntimeProtocol(Protocol):
    def run(self, manifest_path: Path) -> Mapping[str, Any]: ...


class ParallelImplementationRuntimeProtocol(Protocol):
    def execute(
        self,
        workflow_manifest_path: Path,
        graph_path: Path,
        capacity_receipt_path: Path,
    ) -> Mapping[str, Any]: ...


class LocalWebMultiRuntime:
    def __init__(self, runtime_path: Path = WEB_MULTI_RUNTIME_PATH):
        spec = importlib.util.spec_from_file_location("chatgpt_web_multi_handoff", runtime_path)
        if spec is None or spec.loader is None:
            raise WorkflowError("WEB_MULTI_RUNTIME_IMPORT_FAILED", str(runtime_path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.module = module

    @staticmethod
    def _resume_parent_from_runtime_state(manifest_path: Path) -> Path | None:
        """Reuse only the exact persisted Web Multi parent for this manifest.

        A v4 handoff can be interrupted after a safe pre-submit rejection.  The
        Web Multi runtime records that parent beneath the immutable output
        identity; passing it back lets its state machine reopen only a proven
        retryable child instead of creating a second parent workflow.
        """
        manifest = load_mapping(manifest_path)
        workflow_id = manifest.get("workflow_id")
        output_dir = manifest.get("output_dir")
        if not isinstance(workflow_id, str) or not workflow_id or not isinstance(output_dir, str) or not output_dir:
            return None
        state_path = Path(output_dir) / workflow_id / "runtime-state.json"
        if not state_path.is_file() or state_path.is_symlink():
            return None
        try:
            state = load_mapping(state_path)
        except (OSError, WorkflowError):
            return None
        if (
            state.get("schema") != "codex.chatgpt.web-multi-runtime-state/v1"
            or state.get("workflow_id") != workflow_id
            or not isinstance(state.get("parent_run_dir"), str)
            or not state["parent_run_dir"]
        ):
            return None
        return Path(state["parent_run_dir"])

    def run(self, manifest_path: Path) -> Mapping[str, Any]:
        resume_parent = self._resume_parent_from_runtime_state(manifest_path)
        return self.module.run_with_provider_failure_retries(
            manifest_path,
            resume_parent=resume_parent,
        )


class LocalParallelImplementationRuntime:
    """Bridge a passed v2 plan/review gate to the exact v3 child supervisor."""

    def __init__(self, runtime_path: Path | None = None):
        selected = runtime_path or next(
            (path for path in PARALLEL_IMPLEMENTATION_RUNTIME_CANDIDATES if path.is_file()),
            None,
        )
        if selected is None:
            raise WorkflowError("PARALLEL_IMPLEMENTATION_RUNTIME_IMPORT_FAILED")
        spec = importlib.util.spec_from_file_location("chatgpt_parallel_implementation_handoff", selected)
        if spec is None or spec.loader is None:
            raise WorkflowError("PARALLEL_IMPLEMENTATION_RUNTIME_IMPORT_FAILED", str(selected))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.module = module

    def execute(
        self,
        workflow_manifest_path: Path,
        graph_path: Path,
        capacity_receipt_path: Path,
    ) -> Mapping[str, Any]:
        def capacity_receipt(control: Mapping[str, Any], _state: Mapping[str, Any] | None = None, _wave: int | None = None) -> Mapping[str, Any]:
            """Bind a fresh observer reading to the newly-created parent identity."""
            observation = load_mapping(capacity_receipt_path)
            if "parent_run_id" in observation or "canonical_baseline_identity_sha256" in observation:
                return observation
            allowed = {"available_child_sessions", "observed_at", "source"}
            if set(observation) != allowed:
                raise WorkflowError("PARALLEL_CAPACITY_OBSERVATION_INVALID")
            available = observation.get("available_child_sessions")
            if not isinstance(available, int) or isinstance(available, bool) or not 1 <= available <= 64:
                raise WorkflowError("PARALLEL_CAPACITY_OBSERVATION_INVALID")
            source = observation.get("source")
            observed_at = observation.get("observed_at")
            if not isinstance(source, str) or not source or not isinstance(observed_at, str) or not observed_at:
                raise WorkflowError("PARALLEL_CAPACITY_OBSERVATION_INVALID")
            return {
                "schema": "codex.chatgpt.parallel-implementation-capacity/v1",
                "parent_run_id": str(control["parent_run_id"]),
                "canonical_baseline_identity_sha256": str(control["canonical_baseline_identity_sha256"]),
                "available_child_sessions": available,
                "observed_at": observed_at,
                "source": source,
            }
        prepared = self.module.prepare(
            workflow_manifest_path,
            graph_path,
            initial_capacity_receipt_provider=lambda control: capacity_receipt(control),
        )
        parent_run_dir = Path(str(prepared.get("parent_run_dir") or ""))
        if not parent_run_dir.is_dir():
            raise WorkflowError("PARALLEL_IMPLEMENTATION_PARENT_RUN_MISSING")
        execution = self.module.execute(
            parent_run_dir,
            capacity_receipt_provider=capacity_receipt,
        )
        return {"prepared": prepared, "execution": execution}


def load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as exc:
            raise WorkflowError("YAML_REQUIRES_PYYAML", str(path)) from exc
        value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise WorkflowError("MANIFEST_NOT_OBJECT", str(path))
    return value


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def write_immutable_text(path: Path, value: str) -> None:
    if path.exists():
        if path.read_text(encoding="utf-8") != value:
            raise WorkflowError("IMMUTABLE_ARTIFACT_CONFLICT", str(path))
        return
    atomic_write_text(path, value)


def write_immutable_utf8_bytes(path: Path, value: str) -> None:
    """Persist web-authored text without platform newline translation."""
    data = value.encode("utf-8", errors="strict")
    if path.exists():
        if path.read_bytes() != data:
            raise WorkflowError("IMMUTABLE_ARTIFACT_CONFLICT", str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def resolve_agbrowse_contract(value: Mapping[str, Any]) -> tuple[str, str]:
    """Return the exact selected contract path and its immutable content hash.

    v2 callers may select a previously captured contract; v1 intentionally
    retains the historical 0.1.18 default.  Canonicalize once so every child
    process receives the same absolute file identity regardless of its cwd.
    """
    selected = value.get("agbrowse_contract") if value.get("schema") in {WORKFLOW_V2_SCHEMA, WORKFLOW_V4_SCHEMA} else None
    if selected is None:
        selected = str(DEFAULT_CONTRACT_PATH)
    if not isinstance(selected, str) or not selected.strip():
        raise WorkflowError("AGBROWSE_CONTRACT_PATH_INVALID")
    raw_path = Path(selected).expanduser()
    if raw_path.is_symlink() or not raw_path.is_file():
        raise WorkflowError("AGBROWSE_CONTRACT_PATH_INVALID", selected)
    try:
        path = raw_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkflowError("AGBROWSE_CONTRACT_PATH_INVALID", selected) from exc
    try:
        parsed = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError("AGBROWSE_CONTRACT_JSON_INVALID", selected) from exc
    if not isinstance(parsed, dict):
        raise WorkflowError("AGBROWSE_CONTRACT_JSON_INVALID", selected)
    return str(path), file_sha256(path)


@contextmanager
def exclusive_workflow_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise WorkflowError("WORKFLOW_ALREADY_ACTIVE", str(path)) from exc
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def validate_manifest(
    value: Mapping[str, Any],
    *,
    allow_legacy_comprehensive_recovery: bool = False,
) -> dict[str, Any]:
    schema = value.get("schema")
    if schema not in {WORKFLOW_SCHEMA, WORKFLOW_V2_SCHEMA, WORKFLOW_V4_SCHEMA}:
        raise WorkflowError("WORKFLOW_SCHEMA_INVALID")
    if value.get("workflow_mode") not in {"pro-plan-to-gpt-orchestrator", "gpt-comprehensive"}:
        raise WorkflowError("WORKFLOW_MODE_INVALID")
    if (
        schema == WORKFLOW_SCHEMA
        and value.get("workflow_mode") == "gpt-comprehensive"
        and not allow_legacy_comprehensive_recovery
    ):
        raise WorkflowError("COMPREHENSIVE_V4_REQUIRED")
    if (
        schema == WORKFLOW_V2_SCHEMA
        and value.get("workflow_mode") == "gpt-comprehensive"
        and not allow_legacy_comprehensive_recovery
    ):
        raise WorkflowError("COMPREHENSIVE_V4_REQUIRED")
    if not str(value.get("workflow_id") or "").strip():
        raise WorkflowError("WORKFLOW_ID_MISSING")
    if not str(value.get("question") or "").strip():
        raise WorkflowError("QUESTION_MISSING")
    workspace = value.get("workspace")
    context = value.get("context")
    if not isinstance(workspace, dict) or not workspace.get("root"):
        raise WorkflowError("WORKSPACE_ROOT_MISSING")
    if not isinstance(context, dict) or not context.get("candidate_paths"):
        raise WorkflowError("SOURCE_PATHS_EMPTY")
    if not workspace.get("chatgpt_app_name"):
        raise WorkflowError("NON_PRO_APP_REQUIRED")
    root = Path(str(workspace["root"])).resolve(strict=True)
    if schema in {WORKFLOW_V2_SCHEMA, WORKFLOW_V4_SCHEMA}:
        if value.get("workflow_mode") != "gpt-comprehensive":
            raise WorkflowError("STRUCTURED_WORKFLOW_REQUIRES_COMPREHENSIVE")
        if schema == WORKFLOW_V4_SCHEMA:
            relay = value.get("relay")
            if (
                not isinstance(relay, dict)
                or set(relay) != {"mode"}
                or relay.get("mode") != WEB_NATIVE_RELAY_MODE
            ):
                raise WorkflowError("COMPREHENSIVE_RELAY_INVALID")
        gates = value.get("gates")
        if not isinstance(gates, dict):
            raise WorkflowError("GATES_REQUIRED")
        valid_research = {"explicit_request", "current_external_facts", "broad_source_synthesis", "legal", "market", "standards", "recommendation_uncertainty"}
        valid_advisory = {"explicit_request", "verified_contradiction_evidence", "affected_components", "cross_component_interfaces", "security", "privacy", "credentials", "legal", "financial", "irreversible_external_state", "shared_routing", "schema_migration", "public_release"}
        for name, allowed in (("research", valid_research), ("advisory", valid_advisory)):
            gate = gates.get(name)
            if not isinstance(gate, dict) or gate.get("policy") not in {"auto", "require", "skip"}:
                raise WorkflowError("GATE_POLICY_INVALID", name)
            triggers = gate.get("triggers")
            if not isinstance(triggers, list) or any(item not in allowed for item in triggers):
                raise WorkflowError("GATE_TRIGGERS_INVALID", name)
            if name == "advisory":
                for metric in ("affected_components", "cross_component_interfaces"):
                    if metric not in gate or not isinstance(gate[metric], int) or isinstance(gate[metric], bool) or gate[metric] < 0:
                        raise WorkflowError("GATE_DECISION_INPUT_INVALID", metric)
                evidence_items = gate.get("contradiction_evidence")
                if not isinstance(evidence_items, list):
                    raise WorkflowError("GATE_DECISION_INPUT_INVALID", "contradiction_evidence")
                for index, item in enumerate(evidence_items):
                    if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                        raise WorkflowError("GATE_EVIDENCE_INVALID", str(index))
                    expected = str(item.get("sha256") or "")
                    if not re.fullmatch(r"[0-9a-f]{64}", expected):
                        raise WorkflowError("GATE_EVIDENCE_INVALID", str(index))
                    try:
                        evidence_path = resolve_workspace_path(root, str(item.get("path") or ""), require_exists=True)
                    except Exception as exc:
                        raise WorkflowError("GATE_EVIDENCE_INVALID", str(index)) from exc
                    if not evidence_path.is_file() or evidence_path.is_symlink():
                        raise WorkflowError("GATE_EVIDENCE_INVALID", str(index))
                    if file_sha256(evidence_path) != expected:
                        raise WorkflowError("GATE_EVIDENCE_HASH_MISMATCH", str(index))
                if "verified_contradiction_evidence" in triggers and not evidence_items:
                    raise WorkflowError("GATE_EVIDENCE_REQUIRED")
    if value.get("runtime_run_identity") or value.get("browser_backend"):
        raise WorkflowError("MANIFEST_CANNOT_OVERRIDE_RUNTIME_OR_BACKEND")
    goal_binding = value.get("goal_supervisor")
    if goal_binding is not None:
        expected_keys = {
            "schema", "goal_id", "cycle_index", "original_goal_sha256", "mission_sha256",
            "cycle_nonce", "criteria", "allowed_host_check_ids",
        }
        if (
            schema != WORKFLOW_V4_SCHEMA
            or not isinstance(goal_binding, Mapping)
            or set(goal_binding) != expected_keys
            or goal_binding.get("schema") != "codex.chatgpt.goal-cycle-binding/v1"
        ):
            raise WorkflowError("GOAL_SUPERVISOR_BINDING_INVALID")
        if not re.fullmatch(r"[0-9a-f]{32}", str(goal_binding.get("cycle_nonce") or "")):
            raise WorkflowError("GOAL_SUPERVISOR_BINDING_INVALID", "cycle_nonce")
        for field in ("original_goal_sha256", "mission_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(goal_binding.get(field) or "")):
                raise WorkflowError("GOAL_SUPERVISOR_BINDING_INVALID", field)
        if not isinstance(goal_binding.get("cycle_index"), int) or isinstance(goal_binding.get("cycle_index"), bool) or not 1 <= goal_binding["cycle_index"] <= 20:
            raise WorkflowError("GOAL_SUPERVISOR_BINDING_INVALID", "cycle_index")
        for field in ("criteria", "allowed_host_check_ids"):
            items = goal_binding.get(field)
            if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
                raise WorkflowError("GOAL_SUPERVISOR_BINDING_INVALID", field)
        if not goal_binding["criteria"] or len(set(goal_binding["criteria"])) != len(goal_binding["criteria"]):
            raise WorkflowError("GOAL_SUPERVISOR_BINDING_INVALID", "criteria")
    allowed = workspace.get("allowed_write_paths") or []
    if not allowed:
        raise WorkflowError("ALLOWED_WRITE_PATHS_REQUIRED")
    for item in allowed:
        try:
            resolve_workspace_path(root, str(item), require_exists=True)
        except Exception as exc:
            raise WorkflowError("INVALID_ALLOWED_WRITE_PATH", str(item)) from exc
    return dict(value)


def assert_no_write_capability(manifest: Mapping[str, Any]) -> None:
    if manifest.get("write_mode") not in {None, "none"} or manifest.get("allowed_paths"):
        raise WorkflowError("READ_ONLY_STAGE_HAS_WRITE_CAPABILITY")


class AgbrowseRuntime:
    def __init__(self, bridge_path: Path = BRIDGE_PATH):
        spec = importlib.util.spec_from_file_location("chatgpt_agbrowse_bridge_handoff", bridge_path)
        if spec is None or spec.loader is None:
            raise WorkflowError("RUNTIME_IMPORT_FAILED", str(bridge_path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.module = module
        self.bridge = module.Bridge()

    def _find(self, run_id: str) -> tuple[Path, dict[str, Any]]:
        root = Path.home() / ".codex" / "state" / "chatgpt-agbrowse" / "projects"
        for path in root.glob(f"*/runs/{run_id}/run.json"):
            return path, load_mapping(path)
        raise WorkflowError("RUNTIME_RUN_NOT_FOUND", run_id)

    def _compat(self, state_file: Path, record: Mapping[str, Any]) -> dict[str, Any]:
        phase = str(record.get("phase") or "")
        status = "completed" if phase == "COMPLETE" else ("blocked" if phase.startswith("BLOCKED") or phase in {
            "SEND_REJECTED", "SUBMISSION_UNCERTAIN_IDENTITY_MISSING", "RECOVERY_REQUIRED",
            "USER_STOP_REQUESTED", "ABANDONED_UNCERTAIN",
        } else "running")
        return {
            "run_id": record.get("run_id"),
            "run_dir": str(state_file.parent),
            "status": status,
            "phase": phase,
            "conversation_url": record.get("conversation_url"),
            "error": record.get("terminal_block_code") or (phase if phase == "ABANDONED_UNCERTAIN" else None),
        }

    def _write_result(self, manifest_path: Path, state_file: Path, record: Mapping[str, Any]) -> None:
        if record.get("phase") != "COMPLETE":
            return
        manifest = load_mapping(manifest_path)
        answer = state_file.parent / "answer.md"
        if not answer.is_file() or not answer.read_text(encoding="utf-8").strip():
            raise WorkflowError("FINAL_TRANSCRIPT_MISSING")
        atomic_write_text(state_file.parent / "transcript.raw.md", answer.read_text(encoding="utf-8"))
        mode = str(manifest.get("mode_label") or "GPT-5.6")
        result = {
            "run_id": record.get("run_id"),
            "status": "completed",
            "completion_state": "DONE",
            "completed_final_output": True,
            "final_text_captured": True,
            "effective_mode_label": mode,
            "effective_mode_variant": None if mode == "Pro" else manifest.get("mode_variant"),
            "regular_mode_selection": None if mode == "Pro" else manifest.get("regular_mode_selection"),
            "fallback_reason": None,
            "conversation_url": record.get("conversation_url"),
            "workflow_correlation": manifest.get("workflow_correlation") or {},
            "gpt_question_policy": {
                "gpt_operation_mode": manifest.get("gpt_operation_mode"),
                "prompt_profile": manifest.get("prompt_profile"),
                "prompt_profile_receipt": manifest.get("prompt_profile_receipt"),
                "unknown_mode_fallback": False,
                "final_attachment_manifest": [
                    {"path": str(item), "sha256": file_sha256(Path(str(item)))}
                    for item in manifest.get("files") or []
                ],
            },
        }
        atomic_write_json(state_file.parent / "result.json", result)
        try:
            cleanup = self.bridge.cleanup_completed(str(state_file.parent), explicit_user_request=False)
        except Exception as exc:
            cleanup = {
                "ok": False,
                "state": "cleanup-pending",
                "error_code": str(getattr(exc, "code", type(exc).__name__)),
            }
            self.bridge.store.record_terminal_cleanup(str(state_file.parent), cleanup)
            raise WorkflowError("STAGE_TAB_CLEANUP_PENDING", cleanup["error_code"]) from exc

    def start(self, manifest_path: Path) -> Mapping[str, Any]:
        manifest = load_mapping(manifest_path)
        contract_path = manifest.get("agbrowse_contract")
        # Older v1 stage manifests recorded the version label instead of the
        # path.  Continue recovering those submissions through their original
        # tested default without changing their immutable manifest bytes.
        if not isinstance(contract_path, str) or contract_path == "0.1.18":
            contract_path = str(DEFAULT_CONTRACT_PATH)
        record = self.bridge.prepare(
            project_root=str(manifest.get("project_root")),
            manifest_path=str(manifest_path),
            contract_path=contract_path,
        )
        run_dir = str(record["run_dir"])
        record = self.bridge.send(run_dir)
        if record.get("phase") in {"SUBMITTED", "URL_BOUND", "RESPONSE_IN_PROGRESS"}:
            record = self.bridge.poll(run_dir)
        state_file, record = self.bridge.store.load(run_dir)
        self._write_result(manifest_path, state_file, record)
        return self._compat(state_file, record)

    def status(self, run_id: str) -> Mapping[str, Any]:
        state_file, record = self._find(run_id)
        return self._compat(state_file, record)

    def wait(self, run_id: str) -> Mapping[str, Any]:
        state_file, record = self._find(run_id)
        if record.get("phase") in {"SUBMITTED", "URL_BOUND", "RESPONSE_IN_PROGRESS"}:
            record = self.bridge.poll(str(state_file.parent))
            state_file, record = self.bridge.store.load(str(state_file.parent))
        self._write_result(Path(str(record["manifest_path"])), state_file, record)
        return self._compat(state_file, record)

    def resume(self, run_id: str) -> Mapping[str, Any]:
        state_file, record = self._find(run_id)
        phase = str(record.get("phase") or "")
        if phase == "SEND_REJECTED":
            record = self.bridge.send(str(state_file.parent))
        elif phase in {
            "SEND_STARTED",
            "RECOVERY_REQUIRED",
            "RECOVERING",
            "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
            "BLOCKED_RECOVERY_EXHAUSTED",
        }:
            record = self.bridge.recover(str(state_file.parent))
        return self._compat(state_file, record)

    def recover_run_ids(self, manifest_path: Path) -> list[str]:
        expected = str(manifest_path.resolve()).casefold()
        root = Path.home() / ".codex" / "state" / "chatgpt-agbrowse" / "projects"
        matches = []
        for path in root.glob("*/runs/*/run.json"):
            try:
                record = load_mapping(path)
            except Exception:
                continue
            if str(Path(str(record.get("manifest_path") or "")).resolve()).casefold() == expected:
                matches.append(str(record.get("run_id")))
        return sorted({item for item in matches if item})


class ProPlanHandoffDriver:
    def __init__(
        self,
        manifest_path: Path,
        runtime: RuntimeProtocol | None = None,
        web_multi_runtime: WebMultiRuntimeProtocol | None = None,
        parallel_implementation_runtime: ParallelImplementationRuntimeProtocol | None = None,
        recovery_only: bool = False,
    ):
        self.manifest_path = manifest_path.resolve()
        raw_manifest = load_mapping(self.manifest_path)
        self.manifest = validate_manifest(
            raw_manifest,
            allow_legacy_comprehensive_recovery=self._legacy_comprehensive_recovery_exists(raw_manifest),
        )
        self.agbrowse_contract_path, self.agbrowse_contract_sha256 = resolve_agbrowse_contract(self.manifest)
        self.runtime = runtime or AgbrowseRuntime()
        self.web_multi_runtime = web_multi_runtime or (LocalWebMultiRuntime() if self.manifest["workflow_mode"] == "gpt-comprehensive" else None)
        self.parallel_implementation_runtime = parallel_implementation_runtime
        self.recovery_only = bool(recovery_only)
        self.workflow_id = str(self.manifest["workflow_id"])
        self.question = str(self.manifest["question"])
        self.question_sha256 = sha256_text(self.question)
        self.workspace = dict(self.manifest["workspace"])
        self.workspace_root = Path(str(self.workspace["root"])).resolve(strict=True)
        self.parallel_execution = self._parallel_execution_selection()
        self.comprehensive = self.manifest["workflow_mode"] == "gpt-comprehensive"
        self.v2 = self.manifest.get("schema") in {WORKFLOW_V2_SCHEMA, WORKFLOW_V4_SCHEMA}
        self.web_native_relay = bool(
            self.manifest.get("schema") == WORKFLOW_V4_SCHEMA
            and isinstance(self.manifest.get("relay"), Mapping)
            and self.manifest["relay"].get("mode") == WEB_NATIVE_RELAY_MODE
        )
        try:
            self.regular_mode_selection = dict(PROMPTS.resolve_regular_mode_selection())
        except Exception as exc:
            raise WorkflowError("REGULAR_MODE_SELECTION_FAILED", str(exc)) from exc
        output = Path(str(self.manifest.get("output_dir") or self.workspace.get("handoff_root") or self.manifest_path.parent / ".handoff"))
        self.workflow_dir = output.resolve() / self.workflow_id
        self.handoff_dir = self.workflow_dir / "handoff"
        self.stages_dir = self.workflow_dir / "stages"
        self.state_path = self.workflow_dir / "state.json"
        self.lock_path = self.workflow_dir / ".workflow.lock"

    def _resolve_manifest_file(self, value: Any, field: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise WorkflowError("PARALLEL_IMPLEMENTATION_CONFIG_INVALID", field)
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.manifest_path.parent / path
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkflowError("PARALLEL_IMPLEMENTATION_FILE_MISSING", field) from exc
        if not resolved.is_file() or resolved.is_symlink():
            raise WorkflowError("PARALLEL_IMPLEMENTATION_FILE_MISSING", field)
        return resolved

    def _parallel_execution_selection(self) -> dict[str, Any]:
        """Validate an explicitly supplied v3 implementation handoff.

        A missing capacity receipt deliberately returns the legacy command path:
        no parallel parent, child workspace, app, or browser side effect exists.
        """
        raw = self.manifest.get("parallel_execution")
        if raw is None:
            return {"selected": False, "reason": "parallel-not-enabled"}
        if not isinstance(raw, Mapping) or set(raw) - {
            "enabled", "workflow_v3_manifest", "implementation_graph", "capacity_receipt"
        } or "enabled" not in raw or not isinstance(raw.get("enabled"), bool):
            raise WorkflowError("PARALLEL_IMPLEMENTATION_CONFIG_INVALID")
        if raw["enabled"] is False:
            if len(raw) != 1:
                raise WorkflowError("PARALLEL_IMPLEMENTATION_CONFIG_INVALID", "disabled config must not carry execution inputs")
            return {"selected": False, "reason": "parallel-disabled"}
        required = {"enabled", "workflow_v3_manifest", "implementation_graph"}
        if not required.issubset(raw):
            raise WorkflowError("PARALLEL_IMPLEMENTATION_CONFIG_INVALID", "enabled config is incomplete")
        workflow_path = self._resolve_manifest_file(raw["workflow_v3_manifest"], "workflow_v3_manifest")
        graph_path = self._resolve_manifest_file(raw["implementation_graph"], "implementation_graph")
        workflow = load_mapping(workflow_path)
        if workflow.get("schema") != "codex.chatgpt.comprehensive-workflow/v3":
            raise WorkflowError("PARALLEL_IMPLEMENTATION_WORKFLOW_SCHEMA_INVALID")
        try:
            workflow_root = Path(str(workflow.get("project_root") or "")).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkflowError("PARALLEL_IMPLEMENTATION_WORKSPACE_INVALID") from exc
        if workflow_root != self.workspace_root:
            raise WorkflowError("PARALLEL_IMPLEMENTATION_WORKSPACE_MISMATCH")
        if sha256_text(str(workflow.get("question") or "")) != self.question_sha256:
            raise WorkflowError("PARALLEL_IMPLEMENTATION_QUESTION_MISMATCH")
        if workflow.get("chatgpt_app_name") != self.workspace.get("chatgpt_app_name"):
            raise WorkflowError("PARALLEL_IMPLEMENTATION_APP_MISMATCH")
        capacity = raw.get("capacity_receipt")
        if capacity is None:
            return {
                "selected": False,
                "reason": "capacity-receipt-absent",
                "workflow_v3_manifest": str(workflow_path),
                "implementation_graph": str(graph_path),
            }
        capacity_path = self._resolve_manifest_file(capacity, "capacity_receipt")
        return {
            "selected": True,
            "reason": "explicit-v3-capacity-receipt",
            "workflow_v3_manifest": str(workflow_path),
            "implementation_graph": str(graph_path),
            "capacity_receipt": str(capacity_path),
        }

    def _legacy_comprehensive_recovery_exists(self, manifest: Mapping[str, Any]) -> bool:
        """Allow v1 comprehensive manifests only when immutable legacy state already exists.

        New comprehensive work must use v4 relay. This narrow proof keeps an
        interrupted v1/v2 workflow recoverable without allowing a new legacy send.
        """
        schema = manifest.get("schema")
        if manifest.get("workflow_mode") != "gpt-comprehensive":
            return False
        if schema == WORKFLOW_SCHEMA:
            pass
        elif schema == WORKFLOW_V2_SCHEMA and manifest.get("relay") is None:
            pass
        else:
            return False
        try:
            workspace = manifest.get("workspace")
            if not isinstance(workspace, Mapping):
                return False
            workflow_id = str(manifest.get("workflow_id") or "").strip()
            question = str(manifest.get("question") or "").strip()
            workspace_root = Path(str(workspace.get("root") or "")).resolve(strict=True)
            output = Path(
                str(
                    manifest.get("output_dir")
                    or workspace.get("handoff_root")
                    or self.manifest_path.parent / ".handoff"
                )
            ).resolve()
            workflow_dir = output / workflow_id
            state_path = workflow_dir / "state.json"
            state = load_mapping(state_path)
            recorded_manifest_schema = state.get("workflow_manifest_schema")
            recorded_manifest_sha256 = state.get("workflow_manifest_sha256")
            if (
                state.get("schema") != STATE_SCHEMA
                or state.get("workflow_id") != workflow_id
                or state.get("question_sha256") != sha256_text(question)
                or (recorded_manifest_schema is None) != (recorded_manifest_sha256 is None)
                or (
                    recorded_manifest_schema is not None
                    and (
                        recorded_manifest_schema != schema
                        or recorded_manifest_sha256 != file_sha256(self.manifest_path)
                    )
                )
            ):
                return False
            stages = state.get("stages")
            if not isinstance(stages, Mapping) or not stages:
                return False
            if any(
                not isinstance(checkpoint, Mapping)
                or checkpoint.get("schema") != "codex.chatgpt.stage-checkpoint/v1"
                or not re.fullmatch(r"[0-9a-f]{32}", str(checkpoint.get("nonce") or ""))
                for checkpoint in stages.values()
            ):
                return False
            snapshot_path = Path(str(state.get("source_snapshot_path") or "")).resolve(strict=True)
            if workflow_dir.resolve() not in snapshot_path.parents:
                return False
            snapshot = load_mapping(snapshot_path)
            if (
                Path(str(snapshot.get("workspace_root") or "")).resolve(strict=True) != workspace_root
                or snapshot.get("question_sha256") != sha256_text(question)
                or state.get("source_snapshot_sha256") != snapshot.get("snapshot_sha256")
            ):
                return False
            archive = state.get("source_archive")
            if not isinstance(archive, Mapping):
                return False
            archive_path = Path(str(archive.get("path") or "")).resolve(strict=True)
            return (
                workflow_dir.resolve() in archive_path.parents
                and file_sha256(archive_path) == archive.get("sha256")
            )
        except (OSError, ValueError, TypeError, WorkflowError):
            return False

    def _gate_descriptor(self, name: str) -> tuple[dict[str, Any], str]:
        gate = dict(self.manifest["gates"][name])
        policy = gate["policy"]
        triggers = list(gate["triggers"])
        if name == "advisory":
            if gate.get("affected_components", 0) >= 3 and "affected_components" not in triggers:
                triggers.append("affected_components")
            if gate.get("cross_component_interfaces", 0) >= 2 and "cross_component_interfaces" not in triggers:
                triggers.append("cross_component_interfaces")
            verified_evidence = []
            for item in gate.get("contradiction_evidence") or []:
                path = resolve_workspace_path(self.workspace_root, str(item["path"]), require_exists=True)
                verified_evidence.append(
                    {"path": str(path), "sha256": file_sha256(path)}
                )
            if verified_evidence and "verified_contradiction_evidence" not in triggers:
                triggers.append("verified_contradiction_evidence")
        else:
            verified_evidence = []
        selected = policy == "require" or (policy == "auto" and bool(triggers))
        decision_inputs = {
            "triggers": triggers,
            **(
                {
                    "affected_components": gate["affected_components"],
                    "cross_component_interfaces": gate["cross_component_interfaces"],
                    "contradiction_evidence": verified_evidence,
                }
                if name == "advisory"
                else {}
            ),
        }
        descriptor = {
            "schema": GATE_V2_SCHEMA,
            "workflow_id": self.workflow_id,
            "gate": name,
            "policy": policy,
            "decision_inputs": decision_inputs,
            "decision_inputs_sha256": sha256_text(
                json.dumps(decision_inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            ),
            "decision": "run" if selected else "skip",
            "artifact": None,
        }
        return descriptor, sha256_text(json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    @staticmethod
    def _v2_envelope_template(binding: ExpectedBinding, schema: str, **extra: Any) -> str:
        value = {
            "schema": schema,
            "workflow_id": binding.workflow_id,
            "stage": binding.stage,
            "attempt_index": binding.attempt_index,
            "nonce": binding.nonce,
            "question_sha256": binding.question_sha256,
            "source_snapshot_sha256": binding.source_snapshot_sha256,
            **extra,
        }
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)

    @staticmethod
    def _relay_template(
        binding: ExpectedBinding,
        *,
        next_stage: str,
        prompt_profile: str,
        input_bindings: Mapping[str, str],
        include_review_continuation: bool = False,
        include_mission: bool = False,
        include_revision_delta: bool = False,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": RELAY_SCHEMA,
            "workflow_id": binding.workflow_id,
            "from_stage": binding.stage,
            "attempt_index": binding.attempt_index,
            "nonce": binding.nonce,
            "next_stage": next_stage,
            "prompt_profile": prompt_profile,
            "input_bindings": dict(input_bindings),
            "next_prompt_body": "<write the complete semantic instructions for the next web GPT stage>",
        }
        if include_review_continuation:
            value["review_prompt_body"] = "<write the complete semantic instructions for the later independent review stage>"
        if include_mission:
            value["mission_body"] = "<write the complete implementation mission for the orchestrator>"
        if include_revision_delta:
            value["revision_delta"] = {
                "required_changes": [],
                "conditions": [],
                "evidence_gaps": [],
                "preserve": [],
            }
        return value

    @staticmethod
    def _relay_text(value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise WorkflowError("STAGE_RELAY_INVALID", field)
        if not value.strip() or len(value) > 200_000 or "???" in value or "\ufffd" in value:
            raise WorkflowError("STAGE_RELAY_INVALID", field)
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise WorkflowError("STAGE_RELAY_INVALID", field) from exc
        return value

    def _relay_bound_parallel_graph(
        self,
        graph_path: Path,
        relay_receipt: Mapping[str, Any],
    ) -> tuple[Path, dict[str, Any]]:
        """Bind the reviewer's exact approved mission into every v3 child unit."""
        mission_ref = relay_receipt.get("mission")
        if not isinstance(mission_ref, Mapping):
            raise WorkflowError("PARALLEL_IMPLEMENTATION_RELAY_MISSION_MISSING")
        mission_path = Path(str(mission_ref.get("path") or ""))
        mission_sha256 = str(mission_ref.get("sha256") or "")
        if (
            not mission_path.is_file()
            or mission_path.is_symlink()
            or file_sha256(mission_path) != mission_sha256
        ):
            raise WorkflowError("PARALLEL_IMPLEMENTATION_RELAY_MISSION_IDENTITY_INVALID")
        reviewer_mission = mission_path.read_bytes().decode("utf-8", errors="strict")
        graph = load_mapping(graph_path)
        units = graph.get("units")
        if graph.get("schema") != "codex.chatgpt.implementation-graph-result/v1" or not isinstance(units, list):
            raise WorkflowError("PARALLEL_IMPLEMENTATION_GRAPH_INVALID")
        derived_units: list[dict[str, Any]] = []
        for item in units:
            if not isinstance(item, Mapping) or not isinstance(item.get("mission"), str):
                raise WorkflowError("PARALLEL_IMPLEMENTATION_GRAPH_INVALID")
            original_mission = str(item["mission"])
            bound_mission = (
                "[REVIEWER-AUTHORED GLOBAL MISSION]\n"
                + reviewer_mission
                + "\n\n[PRE-APPROVED UNIT ASSIGNMENT]\n"
                + original_mission
            )
            if len(bound_mission) > 200_000:
                raise WorkflowError("PARALLEL_IMPLEMENTATION_RELAY_MISSION_TOO_LARGE")
            derived_units.append({**dict(item), "mission": bound_mission})
        derived_graph = {**graph, "units": derived_units}
        derived_path = self.handoff_dir / "implementation-graph.relay-bound.json"
        write_immutable_text(
            derived_path,
            json.dumps(derived_graph, ensure_ascii=False, indent=2, sort_keys=True),
        )
        receipt = {
            "schema": "codex.chatgpt.parallel-relay-binding/v1",
            "workflow_id": self.workflow_id,
            "source_graph": {"path": str(graph_path), "sha256": file_sha256(graph_path)},
            "reviewer_mission": {"path": str(mission_path), "sha256": mission_sha256},
            "relay_receipt": {
                "path": str(relay_receipt.get("receipt_path") or ""),
                "sha256": str(relay_receipt.get("receipt_sha256") or ""),
            },
            "derived_graph": {"path": str(derived_path), "sha256": file_sha256(derived_path)},
        }
        receipt_path = self.handoff_dir / "implementation-graph.relay-bound.receipt.json"
        write_immutable_text(
            receipt_path,
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True),
        )
        return derived_path, {
            **receipt,
            "receipt_path": str(receipt_path),
            "receipt_sha256": file_sha256(receipt_path),
        }

    def _validate_and_materialize_relay(
        self,
        envelope: Mapping[str, Any],
        binding: ExpectedBinding,
        *,
        next_stage: str,
        prompt_profile: str,
        input_bindings: Mapping[str, str],
        require_review_continuation: bool = False,
        require_mission: bool = False,
        require_revision_delta: bool = False,
    ) -> dict[str, Any]:
        relay = envelope.get("relay")
        if not isinstance(relay, dict):
            raise WorkflowError("STAGE_RELAY_MISSING", binding.stage)
        expected_keys = {
            "schema", "workflow_id", "from_stage", "attempt_index", "nonce",
            "next_stage", "prompt_profile", "input_bindings", "next_prompt_body",
            *({"review_prompt_body"} if require_review_continuation else set()),
            *({"mission_body"} if require_mission else set()),
            *({"revision_delta"} if require_revision_delta else set()),
        }
        if set(relay) != expected_keys:
            raise WorkflowError("STAGE_RELAY_KEYS_INVALID", binding.stage)
        if (
            relay.get("schema") != RELAY_SCHEMA
            or relay.get("workflow_id") != binding.workflow_id
            or relay.get("from_stage") != binding.stage
            or relay.get("attempt_index") != binding.attempt_index
            or relay.get("nonce") != binding.nonce
            or relay.get("next_stage") != next_stage
            or relay.get("prompt_profile") != prompt_profile
            or relay.get("input_bindings") != dict(input_bindings)
        ):
            raise WorkflowError("STAGE_RELAY_BINDING_MISMATCH", binding.stage)
        next_prompt = self._relay_text(relay.get("next_prompt_body"), "next_prompt_body")
        review_prompt = (
            self._relay_text(relay.get("review_prompt_body"), "review_prompt_body")
            if require_review_continuation else None
        )
        mission_body = (
            self._relay_text(relay.get("mission_body"), "mission_body")
            if require_mission else None
        )
        revision_delta = relay.get("revision_delta") if require_revision_delta else None
        if require_revision_delta:
            expected_delta_keys = {"required_changes", "conditions", "evidence_gaps", "preserve"}
            if (
                not isinstance(revision_delta, dict)
                or set(revision_delta) != expected_delta_keys
                or any(
                    not isinstance(revision_delta[key], list)
                    or any(not isinstance(item, str) or not item.strip() for item in revision_delta[key])
                    for key in expected_delta_keys
                )
            ):
                raise WorkflowError("STAGE_RELAY_REVISION_DELTA_INVALID", binding.stage)

        stem = f"relay-{binding.stage}-attempt-{binding.attempt_index}"
        relay_path = self.handoff_dir / f"{stem}.json"
        next_prompt_path = self.handoff_dir / f"{stem}-next-prompt.txt"
        write_immutable_text(relay_path, json.dumps(relay, ensure_ascii=False, indent=2, sort_keys=True))
        write_immutable_utf8_bytes(next_prompt_path, next_prompt)
        receipt: dict[str, Any] = {
            "schema": "codex.chatgpt.stage-relay-receipt/v1",
            "workflow_id": self.workflow_id,
            "from_stage": binding.stage,
            "attempt_index": binding.attempt_index,
            "nonce": binding.nonce,
            "relay": {"path": str(relay_path), "sha256": file_sha256(relay_path)},
            "next_prompt": {"path": str(next_prompt_path), "sha256": file_sha256(next_prompt_path)},
            "next_stage": next_stage,
            "prompt_profile": prompt_profile,
            "input_bindings": dict(input_bindings),
        }
        if review_prompt is not None:
            review_path = self.handoff_dir / f"{stem}-review-prompt.txt"
            write_immutable_utf8_bytes(review_path, review_prompt)
            receipt["review_prompt"] = {"path": str(review_path), "sha256": file_sha256(review_path)}
        if mission_body is not None:
            mission_path = self.handoff_dir / f"{stem}-mission.txt"
            write_immutable_utf8_bytes(mission_path, mission_body)
            receipt["mission"] = {"path": str(mission_path), "sha256": file_sha256(mission_path)}
        if revision_delta is not None:
            delta_path = self.handoff_dir / f"{stem}-revision-delta.json"
            write_immutable_text(delta_path, json.dumps(revision_delta, ensure_ascii=False, indent=2, sort_keys=True))
            receipt["revision_delta"] = {"path": str(delta_path), "sha256": file_sha256(delta_path)}
        receipt_path = self.handoff_dir / f"{stem}.receipt.json"
        write_immutable_text(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        return {**receipt, "receipt_path": str(receipt_path), "receipt_sha256": file_sha256(receipt_path)}

    @staticmethod
    def _bound_relay_prompt(
        prompt_ref: Mapping[str, Any],
        *,
        binding: ExpectedBinding,
        immutable_inputs: Iterable[Mapping[str, Any]],
        output_template: str,
    ) -> str:
        path = Path(str(prompt_ref.get("path") or ""))
        expected = str(prompt_ref.get("sha256") or "")
        if not path.is_file() or path.is_symlink() or file_sha256(path) != expected:
            raise WorkflowError("STAGE_RELAY_PROMPT_IDENTITY_INVALID", str(path))
        body = path.read_text(encoding="utf-8")
        host_binding = {
            "schema": "codex.chatgpt.relay-host-binding/v1",
            "workflow_id": binding.workflow_id,
            "stage": binding.stage,
            "attempt_index": binding.attempt_index,
            "nonce": binding.nonce,
            "question_sha256": binding.question_sha256,
            "source_snapshot_sha256": binding.source_snapshot_sha256,
            "immutable_inputs": list(immutable_inputs),
        }
        return (
            body.rstrip()
            + "\n\n[HOST-VERIFIED IMMUTABLE BINDING]\n"
            + json.dumps(host_binding, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n\n[REQUIRED FINAL ENVELOPE]\n"
            + output_template.strip()
            + "\n"
        )

    def _write_gate_descriptor(self, name: str, descriptor: Mapping[str, Any]) -> tuple[Path, str]:
        path = self.handoff_dir / f"{name}.gate.json"
        write_immutable_text(path, json.dumps(descriptor, ensure_ascii=False, indent=2, sort_keys=True))
        return path, file_sha256(path)

    def _source_snapshot(self) -> dict[str, Any]:
        context = dict(self.manifest["context"])
        return build_workspace_snapshot(
            workspace_root=self.workspace_root,
            selected_paths=context.get("candidate_paths") or [],
            policy_paths=context.get("policy_paths") or [],
            question_sha256=self.question_sha256,
        )

    def prepare(self) -> dict[str, Any]:
        if self.state_path.is_file():
            state = load_mapping(self.state_path)
            recorded_manifest_schema = state.get("workflow_manifest_schema")
            recorded_manifest_sha256 = state.get("workflow_manifest_sha256")
            if recorded_manifest_schema is None and recorded_manifest_sha256 is None and not self.web_native_relay:
                # Pre-v4 states could not persist this identity. The constructor
                # already proved their immutable snapshot/archive/checkpoints;
                # pin the exact manifest on the first upgraded recovery so a
                # later config edit cannot change the continuation contract.
                state["workflow_manifest_schema"] = self.manifest.get("schema")
                state["workflow_manifest_sha256"] = file_sha256(self.manifest_path)
                state["legacy_manifest_identity_upgrade"] = "validated-pre-v4-state"
                atomic_write_json(self.state_path, state)
            elif (
                recorded_manifest_schema != self.manifest.get("schema")
                or recorded_manifest_sha256 != file_sha256(self.manifest_path)
            ):
                raise WorkflowError("WORKFLOW_MANIFEST_IDENTITY_CONFLICT")
            recorded_path = state.get("agbrowse_contract_path")
            recorded_hash = state.get("agbrowse_contract_sha256")
            if recorded_path is None and recorded_hash is None and not self.v2:
                # A pre-v2 state can still recover its legacy manifests.  Do
                # not retrofit a different contract into those stage files.
                return state
            if (
                recorded_path != self.agbrowse_contract_path
                or recorded_hash != self.agbrowse_contract_sha256
            ):
                raise WorkflowError("AGBROWSE_CONTRACT_IDENTITY_CONFLICT")
            recorded_selection = state.get("regular_mode_selection")
            if recorded_selection is not None and recorded_selection != self.regular_mode_selection:
                # A completed or persisted legacy stage keeps its own immutable
                # stage.manifest receipt.  When the public account capability
                # contracts from Very High down to High, preserve that old
                # receipt for recovery but let *new* regular stages use High.
                # This is a one-way safety migration, never an upgrade and
                # never a rewrite of a submitted stage identity.
                recorded_mode = str(recorded_selection.get("selected_mode_variant") or "") if isinstance(recorded_selection, Mapping) else ""
                current_mode = str(self.regular_mode_selection.get("selected_mode_variant") or "")
                if recorded_mode == "Very High" and current_mode == "High":
                    history = state.setdefault("legacy_regular_mode_selections", [])
                    if not isinstance(history, list):
                        raise WorkflowError("REGULAR_MODE_SELECTION_IDENTITY_CONFLICT")
                    if recorded_selection not in history:
                        history.append(dict(recorded_selection))
                    state["regular_mode_selection"] = self.regular_mode_selection
                    state["regular_mode_selection_migration"] = {
                        "kind": "capability-downgrade-preserve-existing-stage-receipts",
                        "from": "Very High",
                        "to": "High",
                    }
                    atomic_write_json(self.state_path, state)
                else:
                    raise WorkflowError("REGULAR_MODE_SELECTION_IDENTITY_CONFLICT")
            return state
        snapshot = self._source_snapshot()
        snapshot_path = self.handoff_dir / "source-snapshot.json"
        write_snapshot(snapshot_path, snapshot)
        archive = None if self.comprehensive else build_source_archive(
            workspace_root=self.workspace_root,
            snapshot=snapshot,
            output_zip=self.handoff_dir / "source-context.zip",
        )
        state = {
            "schema": STATE_SCHEMA,
            "workflow_id": self.workflow_id,
            "status": "PREPARED",
            "question_sha256": self.question_sha256,
            "workflow_manifest_schema": self.manifest.get("schema"),
            "workflow_manifest_sha256": file_sha256(self.manifest_path),
            "agbrowse_contract_path": self.agbrowse_contract_path,
            "agbrowse_contract_sha256": self.agbrowse_contract_sha256,
            "source_snapshot_path": str(snapshot_path),
            "source_snapshot_sha256": snapshot["snapshot_sha256"],
            "source_archive": archive,
            "regular_mode_selection": self.regular_mode_selection,
            "stages": {},
        }
        atomic_write_json(self.state_path, state)
        return state

    def _binding(
        self,
        state: Mapping[str, Any],
        stage: str,
        attempt: int,
        plan_hash=None,
        advisory_hash=None,
        review_hash=None,
        research_descriptor_sha256=None,
        advisory_descriptor_sha256=None,
    ) -> ExpectedBinding:
        """Durably allocate a stage identity before writing its prompt or manifest.

        A retry is an attempt to continue this exact stage, never permission to
        create a second conversation with a fresh nonce.
        """
        dependency_identity = {
            key: value
            for key, value in {
                "plan_sha256": plan_hash,
                "advisory_sha256": advisory_hash,
                "review_sha256": review_hash,
                "research_descriptor_sha256": research_descriptor_sha256,
                "advisory_descriptor_sha256": advisory_descriptor_sha256,
            }.items()
            if value is not None
        }
        key = f"{stage}-attempt-{attempt}"
        stages = state.get("stages")
        if not isinstance(stages, dict):
            raise WorkflowError("STAGE_CHECKPOINTS_INVALID")
        checkpoint = stages.get(key)
        if checkpoint is None:
            if self.recovery_only:
                raise WorkflowError(
                    LEGACY_NEW_SUBMISSION_FROZEN,
                    f"legacy recovery cannot create an unsubmitted stage: {key}",
                )
            checkpoint = {
                "schema": "codex.chatgpt.stage-checkpoint/v1",
                "stage": stage,
                "attempt_index": attempt,
                "nonce": secrets.token_hex(16),
                "dependency_identity": dependency_identity,
                "agbrowse_contract_path": self.agbrowse_contract_path,
                "agbrowse_contract_sha256": self.agbrowse_contract_sha256,
            }
            stages[key] = checkpoint
            atomic_write_json(self.state_path, dict(state))
        if not isinstance(checkpoint, dict) or checkpoint.get("schema") != "codex.chatgpt.stage-checkpoint/v1":
            raise WorkflowError("STAGE_CHECKPOINT_INVALID", key)
        if checkpoint.get("stage") != stage or checkpoint.get("attempt_index") != attempt:
            raise WorkflowError("STAGE_CHECKPOINT_IDENTITY_CONFLICT", key)
        if checkpoint.get("dependency_identity") != dependency_identity:
            raise WorkflowError("STAGE_CHECKPOINT_DEPENDENCY_CONFLICT", key)
        if (
            checkpoint.get("agbrowse_contract_path") != self.agbrowse_contract_path
            or checkpoint.get("agbrowse_contract_sha256") != self.agbrowse_contract_sha256
        ):
            raise WorkflowError("STAGE_CHECKPOINT_CONTRACT_IDENTITY_CONFLICT", key)
        nonce = str(checkpoint.get("nonce") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise WorkflowError("STAGE_CHECKPOINT_NONCE_INVALID", key)
        return ExpectedBinding(
            workflow_id=self.workflow_id,
            stage=stage,
            attempt_index=attempt,
            nonce=nonce,
            question_sha256=self.question_sha256,
            source_snapshot_sha256=str(state["source_snapshot_sha256"]),
            plan_sha256=plan_hash,
            advisory_sha256=advisory_hash,
            review_sha256=review_hash,
            research_descriptor_sha256=research_descriptor_sha256,
            advisory_descriptor_sha256=advisory_descriptor_sha256,
        )

    def _correlation(self, binding: ExpectedBinding) -> dict[str, Any]:
        correlation = build_workflow_correlation(
            workflow_id=binding.workflow_id,
            stage=binding.stage,
            attempt_index=binding.attempt_index,
            nonce=binding.nonce,
            question_sha256=binding.question_sha256,
            source_snapshot_sha256=binding.source_snapshot_sha256,
        )
        if self.v2:
            correlation["schema"] = "codex.chatgpt.workflow-correlation/v2"
        return correlation

    def _manifest(self, state: Mapping[str, Any], binding: ExpectedBinding, prompt: str, *, files=(), read_only_paths=()) -> dict[str, Any]:
        pro = binding.stage in {"pro-plan", "pro-advisory"}
        research = binding.stage == "deep-research"
        profile_name = STAGE_PROMPT_PROFILES.get(binding.stage)
        if profile_name is None:
            raise WorkflowError("PROMPT_PROFILE_STAGE_UNKNOWN", binding.stage)
        workflow_route = STAGE_WORKFLOW_ROUTES.get(binding.stage)
        if workflow_route is None:
            raise WorkflowError("WORKFLOW_ROUTE_STAGE_UNKNOWN", binding.stage)
        profile = PROMPTS.resolve_profile(profile_name)
        stage_dir = self.stages_dir / f"{binding.stage}-attempt-{binding.attempt_index}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = stage_dir / "prompt.txt"
        write_immutable_text(prompt_file, prompt)
        prompt_sha256 = file_sha256(prompt_file)
        stage_manifest_path = stage_dir / "stage.manifest.json"
        regular_mode_variant = None
        regular_mode_selection = None
        if not pro:
            if stage_manifest_path.is_file():
                existing_stage = load_mapping(stage_manifest_path)
                regular_mode_variant = str(existing_stage.get("mode_variant") or "")
                if regular_mode_variant not in REGULAR_MODE_VARIANTS:
                    raise WorkflowError("MODE_VARIANT_INVALID", regular_mode_variant)
                regular_mode_selection = existing_stage.get("regular_mode_selection")
                if not isinstance(regular_mode_selection, dict):
                    raise WorkflowError("REGULAR_MODE_SELECTION_RECEIPT_MISSING")
            else:
                regular_mode_selection = dict(self.regular_mode_selection or PROMPTS.resolve_regular_mode_selection())
                regular_mode_variant = str(regular_mode_selection["selected_mode_variant"])
        manifest = {
            "project_root": str(self.workspace_root),
            "question": PROMPT_FILE_HANDOFF,
            "prompt_transport": "file",
            "prompt_file": str(prompt_file),
            "prompt_file_sha256": prompt_sha256,
            "mode_label": "Pro" if pro else ("Deep Research" if research else "GPT-5.6"),
            "mode_variant": None if pro else regular_mode_variant,
            "regular_mode_selection": regular_mode_selection,
            "app_policy": "forbidden" if pro else "required",
            "chatgpt_app_name": None if pro else self.workspace["chatgpt_app_name"],
            "files": [str(prompt_file), *[str(item) for item in files]],
            "read_only_paths": [str(item) for item in read_only_paths],
            "gpt_operation_mode": profile.task_kind,
            "prompt_profile": profile.name,
            "prompt_profile_receipt": profile.receipt(),
            "workflow_correlation": self._correlation(binding),
            "workflow_route": workflow_route,
            "write_mode": "none" if binding.stage != "gpt-orchestrator" else "app-owned",
            "allowed_paths": [] if binding.stage != "gpt-orchestrator" else list(self.workspace.get("allowed_write_paths") or []),
            "browser_backend": "agbrowse",
            "agbrowse_contract": self.agbrowse_contract_path,
            "agbrowse_contract_sha256": self.agbrowse_contract_sha256,
        }
        if research:
            manifest["research_selection_transport"] = "preselected-research"
            manifest["research_selection_contract"] = "codex.chatgpt.capability-selection/v1"
        if binding.stage != "gpt-orchestrator":
            assert_no_write_capability(manifest)
        return manifest

    def _dispatch(self, stage: str, attempt: int, manifest: dict[str, Any]) -> tuple[dict[str, Any], str, Path]:
        stage_dir = self.stages_dir / f"{stage}-attempt-{attempt}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = stage_dir / "stage.manifest.json"
        # The checkpoint has already committed the nonce/dependencies.  The
        # manifest must therefore be byte-stable too, so a rerun cannot alter
        # the identity that agbrowse uses to locate the original submission.
        if self.recovery_only and not manifest_path.is_file():
            raise WorkflowError(
                LEGACY_NEW_SUBMISSION_FROZEN,
                f"legacy recovery cannot create an unsubmitted stage manifest: {stage}",
            )
        write_immutable_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        capture_path = stage_dir / "runtime-capture.json"
        result_capture = stage_dir / "runtime-result.json"
        transcript_capture = stage_dir / "runtime-transcript.raw.md"
        if capture_path.is_file():
            capture = load_mapping(capture_path)
            if (
                capture.get("schema") == "codex.chatgpt.handoff-runtime-capture/v1"
                and capture.get("manifest_sha256") == file_sha256(manifest_path)
                and result_capture.is_file()
                and transcript_capture.is_file()
                and capture.get("result_sha256") == file_sha256(result_capture)
                and capture.get("transcript_sha256") == file_sha256(transcript_capture)
            ):
                return load_mapping(result_capture), transcript_capture.read_text(encoding="utf-8"), manifest_path
            raise WorkflowError("STAGE_RUNTIME_CAPTURE_INVALID", str(capture_path))
        recovered = self.runtime.recover_run_ids(manifest_path)
        if len(recovered) > 1:
            raise WorkflowError("MULTIPLE_RUNTIME_RUNS_FOR_STAGE")
        if recovered:
            run_id = recovered[0]
        else:
            if self.recovery_only:
                raise WorkflowError(
                    LEGACY_NEW_SUBMISSION_FROZEN,
                    f"legacy recovery found no already-sent run for stage: {stage}",
                )
            started = dict(self.runtime.start(manifest_path))
            run_id = str(started.get("run_id") or "")
        if not run_id:
            raise WorkflowError("RUNTIME_DID_NOT_RETURN_RUN_ID")
        status = dict(self.runtime.status(run_id))
        if status.get("status") in {"running", "started", "pending"}:
            status = dict(self.runtime.wait(run_id))
        if status.get("phase") in {
            "SEND_STARTED",
            "SEND_REJECTED",
            "RECOVERY_REQUIRED",
            "RECOVERING",
            "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
            "BLOCKED_RECOVERY_EXHAUSTED",
        }:
            if self.recovery_only and status.get("phase") == "SEND_REJECTED":
                raise WorkflowError(
                    LEGACY_NEW_SUBMISSION_FROZEN,
                    f"legacy recovery cannot retry a pre-submit rejected stage: {stage}",
                )
            self.runtime.resume(run_id)
            status = dict(self.runtime.wait(run_id))
        if status.get("status") != "completed":
            raise WorkflowError("RUNTIME_NOT_COMPLETED", json.dumps(status, ensure_ascii=False))
        run_dir = Path(str(status.get("run_dir") or ""))
        result_path = run_dir / "result.json"
        transcript_path = run_dir / "transcript.raw.md"
        if not result_path.is_file() or not transcript_path.is_file():
            raise WorkflowError("RUNTIME_ARTIFACT_MISSING", str(run_dir))
        result = load_mapping(result_path)
        transcript = transcript_path.read_text(encoding="utf-8")
        url = str(result.get("conversation_url") or "")
        if not url.startswith("https://chatgpt.com/c/"):
            raise WorkflowError("CANONICAL_CONVERSATION_URL_MISSING", url)
        write_immutable_text(result_capture, json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        write_immutable_text(transcript_capture, transcript)
        write_immutable_text(
            capture_path,
            json.dumps(
                {
                    "schema": "codex.chatgpt.handoff-runtime-capture/v1",
                    "manifest_sha256": file_sha256(manifest_path),
                    "result_sha256": file_sha256(result_capture),
                    "transcript_sha256": file_sha256(transcript_capture),
                    "canonical_url": url,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        )
        return result, transcript, manifest_path

    def _verify_runtime(self, result: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
        expected_mode = manifest["mode_label"]
        if result.get("effective_mode_label") != expected_mode or result.get("fallback_reason"):
            raise WorkflowError("MODEL_CONTRACT_FAILED")
        if expected_mode != "Pro" and result.get("effective_mode_variant") != manifest.get("mode_variant"):
            raise WorkflowError("MODE_VARIANT_CONTRACT_FAILED")
        if expected_mode != "Pro" and result.get("regular_mode_selection") != manifest.get("regular_mode_selection"):
            raise WorkflowError("REGULAR_MODE_SELECTION_RECEIPT_FAILED")
        policy = dict(result.get("gpt_question_policy") or {})
        if policy.get("gpt_operation_mode") != manifest.get("gpt_operation_mode"):
            raise WorkflowError("OPERATION_MODE_FAILED")
        if (
            policy.get("prompt_profile") != manifest.get("prompt_profile")
            or policy.get("prompt_profile_receipt") != manifest.get("prompt_profile_receipt")
        ):
            raise WorkflowError("PROMPT_PROFILE_RECEIPT_FAILED")
        if result.get("workflow_correlation") != manifest.get("workflow_correlation"):
            raise WorkflowError("WORKFLOW_CORRELATION_MISMATCH")

    def _run_web_multi_advisory(
        self,
        state: Mapping[str, Any],
        *,
        attempt: int,
        plan_path: Path,
        plan_hash: str,
        relay_question: str | None = None,
    ) -> tuple[Path, str, dict[str, Any]]:
        if not self.comprehensive or self.web_multi_runtime is None:
            raise WorkflowError("WEB_MULTI_RUNTIME_REQUIRED")
        original = load_mapping(Path(str(state["source_snapshot_path"])))
        files = [dict(item) for item in (original.get("files") or []) if isinstance(item, dict)]
        files.append(
            {
                "path": str(plan_path.resolve()),
                "role": "incumbent-plan-candidate",
                "bytes": plan_path.stat().st_size,
                "sha256": file_sha256(plan_path),
            }
        )
        advisory_snapshot = {
            "schema": "codex.chatgpt.web-multi-source/v1",
            "workspace_root": str(self.workspace_root),
            "question_sha256": self.question_sha256,
            "files": files,
        }
        snapshot_path = self.handoff_dir / f"web-multi-source-{attempt}.json"
        write_immutable_text(snapshot_path, json.dumps(advisory_snapshot, ensure_ascii=False, indent=2, sort_keys=True))
        config = dict(self.manifest.get("web_multi_gpt") or {})
        web_manifest = {
            "schema": "codex.chatgpt.web-multi/v2" if self.v2 else "codex.chatgpt.web-multi/v1",
            "workflow_id": f"{self.workflow_id}-advisory-{attempt}",
            "project_root": str(self.workspace_root),
            "question": relay_question.strip() if relay_question else (
                f"Independently expand and synthesize the solution space for the original task: {self.question}. "
                f"Treat the plan at {plan_path} as one incumbent candidate, not as authority or the frame of the problem. "
                "Include a direct baseline and a materially different reframe. Return advisory proposals and tradeoffs only; "
                "do not implement, approve, or authorize release."
            ),
            "source_snapshot_path": str(snapshot_path),
            "source_snapshot_sha256": file_sha256(snapshot_path),
            "output_dir": str(self.workflow_dir / "web-multi"),
            "chatgpt_app_name": self.workspace["chatgpt_app_name"],
            "app_decision_path": self.workspace.get("chatgpt_app_decision_path"),
            "chatgpt_app_server_url": self.workspace.get("chatgpt_app_server_url"),
            "max_iterations": int(config.get("max_iterations") or (5 if self.v2 else 2)),
            "mode_variant": str(config.get("mode_variant") or preferred_regular_mode_variant()),
            "agbrowse_contract": self.agbrowse_contract_path,
            "agbrowse_contract_sha256": self.agbrowse_contract_sha256,
        }
        stage_dir = self.stages_dir / f"web-multi-advisory-attempt-{attempt}"
        if not self.v2:
            web_manifest["solver_count"] = int(config.get("solver_count") or 3)
        else:
            web_manifest["planner_policy"] = "upstream-nonempty-prefix10"
            web_manifest["semantics_version"] = "upstream-parity-v1"
        web_manifest_path = stage_dir / "web-multi.manifest.json"
        write_immutable_text(web_manifest_path, json.dumps({k: v for k, v in web_manifest.items() if v is not None}, ensure_ascii=False, indent=2, sort_keys=True))
        advisory = dict(self.web_multi_runtime.run(web_manifest_path))
        minimum_concurrency = 1 if self.v2 else 2
        if (
            advisory.get("schema") != "codex.chatgpt.web-multi-result/v1"
            or advisory.get("advisory_only") is not True
            or not isinstance(advisory.get("provenance"), list)
            or int(advisory.get("max_concurrent_child_generations") or 0) < minimum_concurrency
        ):
            raise WorkflowError("WEB_MULTI_ADVISORY_CONTRACT_FAILED")
        if self.v2:
            provenance = advisory["provenance"]
            if (
                advisory.get("manifest_schema") != "codex.chatgpt.web-multi/v2"
                or advisory.get("semantics_version") != "upstream-parity-v1"
                or advisory.get("planner_policy") != "upstream-nonempty-prefix10"
                or advisory.get("mode_variant") != web_manifest["mode_variant"]
                or not provenance
                or not all(isinstance(item, Mapping) and item.get("stage_id") for item in provenance)
                or not isinstance(advisory.get("role_session_target_url_provenance"), list)
            ):
                raise WorkflowError("WEB_MULTI_V2_PROVENANCE_INVALID")
            if "solver_count" in advisory:
                raise WorkflowError("WEB_MULTI_V2_DYNAMIC_CONCURRENCY_INVALID")
        advisory["input_plan_sha256"] = plan_hash
        advisory_path = self.handoff_dir / f"advisory-{attempt}.json"
        write_immutable_text(advisory_path, json.dumps(advisory, ensure_ascii=False, indent=2, sort_keys=True))
        return advisory_path, file_sha256(advisory_path), advisory

    def _run_pro_advisory(
        self,
        state: Mapping[str, Any],
        *,
        attempt: int,
        plan_path: Path,
        plan_hash: str,
    ) -> tuple[Path, str, dict[str, Any]]:
        """Run selected comprehensive advisory through attachment-only Pro.

        This is intentionally a single read-only advisory conversation.  Web
        Multi-GPT is not a fallback for this route: Pro's attachment-only
        policy and app prohibition are part of the immutable stage manifest.
        """
        binding = self._binding(state, "pro-advisory", attempt, plan_hash=plan_hash)
        prompt = PROMPTS.render_prompt(
            "synthesis",
            original_task=self.question,
            context_note=f"Candidate plan attachment: {plan_path}. It is non-binding guidance.",
            stage_mission=(
                "Independently assess the solution space, provide a direct baseline and a materially different reframe, "
                "then state advisory tradeoffs only. Do not implement, approve, or authorize release."
            ),
            output_instructions="Return a concise advisory suitable for a later independent reviewer.",
        )
        manifest = self._manifest(state, binding, prompt, files=(plan_path,))
        if manifest["mode_label"] != "Pro" or manifest["app_policy"] != "forbidden" or manifest["chatgpt_app_name"] is not None:
            raise WorkflowError("PRO_ADVISORY_ROUTE_CONTRACT_FAILED")
        result, transcript, _ = self._dispatch("pro-advisory", attempt, manifest)
        self._verify_runtime(result, manifest)
        advisory = {
            "schema": "codex.chatgpt.pro-advisory/v1",
            "workflow_id": self.workflow_id,
            "attempt": attempt,
            "input_plan_sha256": plan_hash,
            "mode_label": "Pro",
            "app_policy": "forbidden",
            "conversation_url": result["conversation_url"],
            "transcript_sha256": sha256_text(transcript),
            "transcript": transcript,
        }
        advisory_path = self.handoff_dir / f"advisory-{attempt}.json"
        write_immutable_text(advisory_path, json.dumps(advisory, ensure_ascii=False, indent=2, sort_keys=True))
        return advisory_path, file_sha256(advisory_path), advisory

    def run(self, *, prepare_only: bool = False) -> dict[str, Any]:
        with exclusive_workflow_lock(self.lock_path):
            state = self.prepare()
            if prepare_only:
                return state
            seen_urls: set[str] = set()
            research_descriptor_path = None
            research_descriptor_hash = None
            research_path = None
            if self.v2:
                research_gate, _ = self._gate_descriptor("research")
                if research_gate["decision"] == "run":
                    research_binding = self._binding(state, "deep-research", 1)
                    research_template = self._v2_envelope_template(
                        research_binding,
                        "codex.chatgpt.research-result/v2",
                        status="complete",
                        findings=[],
                        sources=[],
                    )
                    research_manifest = self._manifest(
                        state, research_binding,
                        PROMPTS.render_prompt(
                            "research",
                            original_task=self.question,
                            stage_mission=(
                                "Research externally current facts that materially affect the task. "
                                "Do not evaluate an incumbent plan unless the evidence itself requires it."
                            ),
                            output_instructions=(
                                "Return exactly one final fenced JSON object. Preserve every identity/hash value in this template, "
                                "replace findings and sources with evidence-backed arrays, and add no text after the fence:\n"
                                f"{research_template}"
                            ),
                        ),
                    )
                    research_result, research_text, _ = self._dispatch("deep-research", 1, research_manifest)
                    self._verify_runtime(research_result, research_manifest)
                    research = validate_research_envelope_v2(parse_final_envelope(research_text), research_binding)
                    research_path = self.handoff_dir / "research-1.json"
                    write_immutable_text(research_path, json.dumps(research, ensure_ascii=False, indent=2, sort_keys=True))
                    research_gate["artifact"] = {"path": str(research_path), "sha256": file_sha256(research_path)}
                research_descriptor_path, research_descriptor_hash = self._write_gate_descriptor("research", research_gate)
            max_attempts = int((self.manifest.get("pro_plan") or {}).get("max_attempts") or 2)
            revision_delta_path: Path | None = None
            plan_path = None
            review_path = None
            plan_hash = None
            advisory_path = None
            advisory_hash = None
            advisory_descriptor_path = None
            advisory_descriptor_hash = None
            review_hash = None
            revision_relay: dict[str, Any] | None = None
            passed_review_relay: dict[str, Any] | None = None
            for attempt in range(1, max_attempts + 1):
                plan_stage = "gpt-plan" if self.comprehensive else "pro-plan"
                binding = self._binding(state, plan_stage, attempt, research_descriptor_sha256=research_descriptor_hash)
                if self.v2:
                    advisory_gate, _ = self._gate_descriptor("advisory")
                    advisory_selected = advisory_gate["decision"] == "run"
                    plan_relay_bindings = {
                        "question_sha256": self.question_sha256,
                        "source_snapshot_sha256": str(state["source_snapshot_sha256"]),
                        "input_research_descriptor_sha256": str(research_descriptor_hash),
                    }
                    plan_template = self._v2_envelope_template(
                        binding,
                        "codex.chatgpt.plan-result/v2",
                        status="complete",
                        input_research_descriptor_sha256=research_descriptor_hash,
                        sections=[
                            "blockers",
                            "evidence",
                            "alternatives",
                            "files",
                            "implementation",
                            "tests",
                            "rollback",
                            "conclusion_change_triggers",
                        ],
                        **(
                            {
                                "relay": self._relay_template(
                                    binding,
                                    next_stage="web-multi-advisory" if advisory_selected else "gpt-review",
                                    prompt_profile="web-branch-designer" if advisory_selected else "review",
                                    input_bindings=plan_relay_bindings,
                                    include_review_continuation=advisory_selected,
                                )
                            }
                            if self.web_native_relay else {}
                        ),
                    )
                    if self.web_native_relay and revision_relay is not None:
                        prompt = self._bound_relay_prompt(
                            revision_relay["next_prompt"],
                            binding=binding,
                            immutable_inputs=([revision_relay["revision_delta"]] if revision_relay.get("revision_delta") else []),
                            output_template=plan_template,
                        )
                    else:
                        prompt = PROMPTS.render_prompt(
                            "plan",
                            original_task=self.question,
                            context_note=(
                                f"A prior review produced the compact revision delta at {revision_delta_path}. "
                                "Use only that delta as corrective input; do not inherit the prior review's prose or framing."
                                if revision_delta_path else
                                "Start from the original task and evidence. No prior plan is authoritative."
                            ),
                            stage_mission=(
                                "Develop alternatives before selecting one coherent implementation path. "
                                "Place material risks and conclusion-change triggers after the constructive design."
                            ),
                            output_instructions=(
                                "Return exactly one final fenced JSON object. Preserve every identity/hash and every required section name "
                                "in this template; add detailed plan fields as needed. When relay is present, author its prompt text for the "
                                "declared next web stage rather than asking local Codex to rewrite it. Add no text after the fence:\n"
                                f"{plan_template}"
                            ),
                        )
                else:
                    prompt = PROMPTS.render_prompt(
                        "plan",
                        original_task=self.question,
                        context_note=(
                            f"Use the compact revision delta at {revision_delta_path}; do not inherit the full prior review."
                            if revision_delta_path else "Start from the original task and evidence."
                        ),
                        stage_mission="Compare viable approaches, then choose one coherent executable plan. Put risks last.",
                        output_instructions=f"Return one final JSON envelope using schema {PLAN_RESULT_SCHEMA}.",
                    )
                files = () if self.comprehensive else tuple(
                    item for item in (state["source_archive"]["path"], revision_delta_path) if item is not None
                )
                manifest = self._manifest(
                    state,
                    binding,
                    prompt,
                    files=files,
                    read_only_paths=((revision_delta_path,) if self.comprehensive and revision_delta_path else ()),
                )
                result, transcript, _ = self._dispatch(plan_stage, attempt, manifest)
                self._verify_runtime(result, manifest)
                url = str(result["conversation_url"])
                if url in seen_urls:
                    raise WorkflowError("CONVERSATION_REUSED")
                seen_urls.add(url)
                plan = (validate_plan_envelope_v2 if self.v2 else validate_plan_envelope)(parse_final_envelope(transcript), binding)
                plan_path = self.handoff_dir / f"plan-{attempt}.json"
                write_immutable_text(plan_path, json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
                plan_hash = file_sha256(plan_path)
                plan_relay = None
                if self.web_native_relay:
                    plan_relay = self._validate_and_materialize_relay(
                        plan,
                        binding,
                        next_stage="web-multi-advisory" if advisory_selected else "gpt-review",
                        prompt_profile="web-branch-designer" if advisory_selected else "review",
                        input_bindings=plan_relay_bindings,
                        require_review_continuation=advisory_selected,
                    )

                advisory_path = None
                advisory_hash = None
                advisory_descriptor_path = None
                advisory_descriptor_hash = None
                if self.v2:
                    if advisory_gate["decision"] == "run":
                        if self.web_native_relay:
                            assert plan_relay is not None
                            relay_question = Path(str(plan_relay["next_prompt"]["path"])).read_text(encoding="utf-8")
                            advisory_path, advisory_hash, _ = self._run_web_multi_advisory(
                                state,
                                attempt=attempt,
                                plan_path=plan_path,
                                plan_hash=plan_hash,
                                relay_question=relay_question,
                            )
                        else:
                            advisory_path, advisory_hash, _ = self._run_pro_advisory(
                                state, attempt=attempt, plan_path=plan_path, plan_hash=plan_hash,
                            )
                        advisory_gate["artifact"] = {"path": str(advisory_path), "sha256": advisory_hash}
                    advisory_descriptor_path, advisory_descriptor_hash = self._write_gate_descriptor(
                        f"advisory-{attempt}", advisory_gate
                    )
                elif self.comprehensive:
                    advisory_path, advisory_hash, _ = self._run_web_multi_advisory(
                        state,
                        attempt=attempt,
                        plan_path=plan_path,
                        plan_hash=plan_hash,
                    )

                review_binding = self._binding(
                    state,
                    "gpt-review",
                    attempt,
                    plan_hash=plan_hash,
                    advisory_hash=advisory_hash,
                    research_descriptor_sha256=research_descriptor_hash,
                    advisory_descriptor_sha256=advisory_descriptor_hash,
                )
                if self.v2:
                    review_relay_bindings = {
                        "question_sha256": self.question_sha256,
                        "source_snapshot_sha256": str(state["source_snapshot_sha256"]),
                        "input_plan_sha256": str(plan_hash),
                        "input_research_descriptor_sha256": str(research_descriptor_hash),
                        "input_advisory_descriptor_sha256": str(advisory_descriptor_hash),
                    }
                    if self.web_native_relay:
                        pass_relay_template = self._relay_template(
                            review_binding,
                            next_stage="gpt-orchestrator",
                            prompt_profile="orchestrator",
                            input_bindings=review_relay_bindings,
                            include_mission=True,
                        )
                        revise_relay_template = self._relay_template(
                            review_binding,
                            next_stage="gpt-plan",
                            prompt_profile="plan",
                            input_bindings=review_relay_bindings,
                            include_revision_delta=True,
                        )
                        review_template = self._v2_envelope_template(
                            review_binding,
                            "codex.chatgpt.review-result/v2",
                            input_plan_sha256=plan_hash,
                            input_research_descriptor_sha256=research_descriptor_hash,
                            input_advisory_descriptor_sha256=advisory_descriptor_hash,
                            verdict="<PASS|REVISE|BLOCK>",
                            relay="<replace with exactly the matching branch object below; omit relay only for BLOCK>",
                        ) + (
                            "\n\nPASS relay object:\n"
                            + json.dumps(pass_relay_template, ensure_ascii=False, indent=2, sort_keys=True)
                            + "\n\nREVISE relay object:\n"
                            + json.dumps(revise_relay_template, ensure_ascii=False, indent=2, sort_keys=True)
                            + "\n\nBLOCK branch: omit relay and explain the blocker in ordinary review fields."
                        )
                    else:
                        review_template = self._v2_envelope_template(
                            review_binding,
                            "codex.chatgpt.review-result/v2",
                            input_plan_sha256=plan_hash,
                            input_research_descriptor_sha256=research_descriptor_hash,
                            input_advisory_descriptor_sha256=advisory_descriptor_hash,
                            verdict="PASS",
                        )
                    if self.web_native_relay:
                        assert plan_relay is not None
                        prompt_ref = (
                            plan_relay["review_prompt"]
                            if advisory_selected else plan_relay["next_prompt"]
                        )
                        immutable_inputs = [
                            {"role": "candidate-plan", "path": str(plan_path), "sha256": plan_hash},
                            {"role": "research-descriptor", "path": str(research_descriptor_path), "sha256": research_descriptor_hash},
                            {"role": "advisory-descriptor", "path": str(advisory_descriptor_path), "sha256": advisory_descriptor_hash},
                            *(
                                [{"role": "web-multi-advisory", "path": str(advisory_path), "sha256": advisory_hash}]
                                if advisory_path and advisory_hash else []
                            ),
                        ]
                        review_prompt = self._bound_relay_prompt(
                            prompt_ref,
                            binding=review_binding,
                            immutable_inputs=immutable_inputs,
                            output_template=review_template,
                        )
                    else:
                        review_prompt = PROMPTS.render_prompt(
                            "review",
                            original_task=self.question,
                            context_note=(
                                f"Candidate plan: {plan_path}. "
                                + (f"Independent advisory: {advisory_path}." if advisory_path else "Advisory was skipped by its immutable gate.")
                            ),
                            stage_mission=(
                                "Review against the original task, declared constraints, and evidence. "
                                "PASS only when implementation-ready; otherwise REVISE or BLOCK."
                            ),
                            output_instructions=(
                                "Return exactly one final fenced JSON object, preserve all identity/hash values in this template, "
                                "and add no text after the fence:\n" + review_template
                            ),
                        )
                else:
                    review_prompt = PROMPTS.render_prompt(
                        "review",
                        original_task=self.question,
                        context_note=(
                            f"Candidate plan: {plan_path}. "
                            + (f"Independent advisory: {advisory_path}; bind input_advisory_sha256={advisory_hash}." if advisory_path else "")
                        ),
                        stage_mission="Return PASS only if the candidate is implementation-ready; otherwise return REVISE or BLOCK with evidence.",
                        output_instructions=f"Return one final JSON envelope using schema {REVIEW_RESULT_SCHEMA}.",
                    )
                review_inputs = tuple(item for item in (plan_path, research_descriptor_path, advisory_descriptor_path, advisory_path) if item is not None)
                review_manifest = self._manifest(state, review_binding, review_prompt, read_only_paths=review_inputs)
                review_result, review_text, _ = self._dispatch("gpt-review", attempt, review_manifest)
                self._verify_runtime(review_result, review_manifest)
                review_url = str(review_result["conversation_url"])
                if review_url in seen_urls:
                    raise WorkflowError("CONVERSATION_REUSED")
                seen_urls.add(review_url)
                review = (validate_review_envelope_v2 if self.v2 else validate_review_envelope)(parse_final_envelope(review_text), review_binding)
                review_path = self.handoff_dir / f"review-{attempt}.json"
                write_immutable_text(review_path, json.dumps(review, ensure_ascii=False, indent=2, sort_keys=True))
                review_hash = file_sha256(review_path)
                current_review_relay = None
                if self.web_native_relay and review["verdict"] in {"PASS", "REVISE"}:
                    current_review_relay = self._validate_and_materialize_relay(
                        review,
                        review_binding,
                        next_stage="gpt-orchestrator" if review["verdict"] == "PASS" else "gpt-plan",
                        prompt_profile="orchestrator" if review["verdict"] == "PASS" else "plan",
                        input_bindings=review_relay_bindings,
                        require_mission=review["verdict"] == "PASS",
                        require_revision_delta=review["verdict"] == "REVISE",
                    )
                if review["verdict"] == "PASS":
                    passed_review_relay = current_review_relay
                    break
                if review["verdict"] == "BLOCK":
                    raise WorkflowError("REVIEW_BLOCKED")
                revision_delta = {
                    "schema": "codex.chatgpt.plan-revision-delta/v1",
                    "workflow_id": self.workflow_id,
                    "attempt": attempt,
                    "source_review": {"path": str(review_path), "sha256": review_hash},
                    "verdict": review.get("verdict"),
                    "required_changes": review.get("required_changes") or review.get("findings") or review.get("blockers") or [],
                    "conditions": review.get("conditions") or [],
                    "evidence_gaps": review.get("evidence_gaps") or [],
                    "preserve": review.get("preserve") or review.get("strengths") or [],
                }
                if self.web_native_relay:
                    assert current_review_relay is not None
                    authored_delta = json.loads(
                        Path(str(current_review_relay["revision_delta"]["path"])).read_text(encoding="utf-8")
                    )
                    revision_delta.update(authored_delta)
                    revision_relay = current_review_relay
                revision_delta_path = self.handoff_dir / f"revision-delta-{attempt}.json"
                write_immutable_text(
                    revision_delta_path,
                    json.dumps(revision_delta, ensure_ascii=False, indent=2, sort_keys=True),
                )
            else:
                raise WorkflowError("PLAN_REVIEW_DID_NOT_PASS")

            original = load_mapping(Path(str(state["source_snapshot_path"])))
            compare_workspace_snapshots(original, self._source_snapshot())
            assert plan_path and review_path and plan_hash and review_hash
            if self.comprehensive and not self.v2 and not (advisory_path and advisory_hash):
                raise WorkflowError("WEB_MULTI_ADVISORY_MISSING_AFTER_PASS")
            handoff = {
                "schema": "codex.chatgpt.handoff/v2" if self.v2 else HANDOFF_SCHEMA,
                "workflow_id": self.workflow_id,
                "state": "PLAN_READY",
                "question_sha256": self.question_sha256,
                "source_snapshot_sha256": state["source_snapshot_sha256"],
                "plan": {"path": str(plan_path), "sha256": plan_hash},
                "review": {"path": str(review_path), "sha256": review_hash},
                "parallel_execution": self.parallel_execution,
            }
            if advisory_path and advisory_hash:
                handoff["advisory"] = {"path": str(advisory_path), "sha256": advisory_hash}
            if self.v2:
                handoff["research_descriptor"] = {"path": str(research_descriptor_path), "sha256": research_descriptor_hash}
                handoff["advisory_descriptor"] = {"path": str(advisory_descriptor_path), "sha256": advisory_descriptor_hash}
            if self.web_native_relay:
                if passed_review_relay is None:
                    raise WorkflowError("ORCHESTRATOR_RELAY_MISSING")
                handoff["stage_relay"] = {
                    "receipt_path": str(passed_review_relay["receipt_path"]),
                    "receipt_sha256": str(passed_review_relay["receipt_sha256"]),
                    "next_prompt": dict(passed_review_relay["next_prompt"]),
                    "mission": dict(passed_review_relay["mission"]),
                }
            handoff_path = self.handoff_dir / "handoff.manifest.json"
            write_immutable_text(handoff_path, json.dumps(handoff, ensure_ascii=False, indent=2, sort_keys=True))
            parallel_selected = bool(self.parallel_execution.get("selected"))
            execution_mission = {
                "schema": "codex.chatgpt.execution-mission/v1",
                "workflow_id": self.workflow_id,
                "original_task": self.question,
                "source_snapshot_sha256": state["source_snapshot_sha256"],
                "selected_plan": {"path": str(plan_path), "sha256": plan_hash, "authority": "guidance-not-cage"},
                "review_transition": {
                    "path": str(review_path),
                    "sha256": review_hash,
                    "verdict": review.get("verdict"),
                    "mandatory_conditions": review.get("conditions") or review.get("required_changes") or [],
                },
                "web_authored_mission": (
                    {
                        "path": str(passed_review_relay["mission"]["path"]),
                        "sha256": str(passed_review_relay["mission"]["sha256"]),
                        "relay_receipt_path": str(passed_review_relay["receipt_path"]),
                        "relay_receipt_sha256": str(passed_review_relay["receipt_sha256"]),
                    }
                    if self.web_native_relay and passed_review_relay is not None else None
                ),
                "research_descriptor": (
                    {"path": str(research_descriptor_path), "sha256": research_descriptor_hash}
                    if research_descriptor_path and research_descriptor_hash else None
                ),
                "advisory_descriptor": (
                    {"path": str(advisory_descriptor_path), "sha256": advisory_descriptor_hash}
                    if advisory_descriptor_path and advisory_descriptor_hash else None
                ),
                "write_scope": list(self.workspace.get("allowed_write_paths") or []),
                "deviation_policy": {
                    "allowed": "bounded implementation adaptation supported by live workspace evidence and tests",
                    "must_escalate": [
                        "user-visible scope change",
                        "public schema or compatibility break",
                        "security-boundary change",
                        "destructive or irreversible action",
                        "violation of an explicit hard constraint",
                    ],
                },
                "acceptance": {
                    "inspect_live_workspace": True,
                    "run_proportionate_tests": True,
                    "report_changed_files_commands_and_blockers": True,
                },
                "execution_parallelism": {
                    "owner": "parallel-v3-web-gpt-implementers" if parallel_selected else "web-gpt-orchestrator",
                    "scope": "multiple exact child sessions after plan/review PASS" if parallel_selected else "within-single-execution-mission",
                    "lane_policy": (
                        "run only isolated, graph-approved units in capacity-bound concurrent waves; host integrates deterministically"
                        if parallel_selected else
                        "partition safe independent exploration, editing, and tests into internal lanes or parallel tool calls, then integrate"
                    ),
                    "same_project_web_submissions": "capacity-bound exact child sessions" if parallel_selected else "one-active-or-uncertain-run",
                    "local_codex": "host-control-only; no delegated strategy search, code authoring, alternate implementation paths, or execution",
                },
                "host_only_boundaries": [
                    "project lock and immutable hashes",
                    "exact browser run/session/target/canonical-URL ownership",
                    "deterministic local verification and release",
                    "irreversible external actions",
                ],
            }
            execution_mission_path = self.handoff_dir / "execution-mission.json"
            write_immutable_text(
                execution_mission_path,
                json.dumps(execution_mission, ensure_ascii=False, indent=2, sort_keys=True),
            )
            if parallel_selected:
                if passed_review_relay is None:
                    raise WorkflowError("PARALLEL_IMPLEMENTATION_RELAY_MISSING")
                relay_bound_graph, parallel_relay_binding = self._relay_bound_parallel_graph(
                    Path(str(self.parallel_execution["implementation_graph"])),
                    passed_review_relay,
                )
                parallel_runtime = self.parallel_implementation_runtime or LocalParallelImplementationRuntime()
                parallel_result = dict(parallel_runtime.execute(
                    Path(str(self.parallel_execution["workflow_v3_manifest"])),
                    relay_bound_graph,
                    Path(str(self.parallel_execution["capacity_receipt"])),
                ))
                final = {
                    **state,
                    "status": "LOCAL_VERIFY_REQUIRED",
                    "plan_path": str(plan_path),
                    "plan_sha256": plan_hash,
                    "review_path": str(review_path),
                    "review_sha256": review_hash,
                    "advisory_path": str(advisory_path) if advisory_path else None,
                    "advisory_sha256": advisory_hash,
                    "research_descriptor_path": str(research_descriptor_path) if research_descriptor_path else None,
                    "research_descriptor_sha256": research_descriptor_hash,
                    "advisory_descriptor_path": str(advisory_descriptor_path) if advisory_descriptor_path else None,
                    "advisory_descriptor_sha256": advisory_descriptor_hash,
                    "handoff_path": str(handoff_path),
                    "execution_mission_path": str(execution_mission_path),
                    "implementation_route": self.parallel_execution,
                    "parallel_relay_binding": parallel_relay_binding,
                    "relay_receipt_path": str(passed_review_relay["receipt_path"]) if passed_review_relay else None,
                    "relay_receipt_sha256": str(passed_review_relay["receipt_sha256"]) if passed_review_relay else None,
                    "parallel_implementation_result": parallel_result,
                }
                atomic_write_json(self.workflow_dir / "final.json", final)
                latest = load_mapping(self.state_path)
                final["stages"] = latest.get("stages", state.get("stages", {}))
                atomic_write_json(self.state_path, final)
                return final
            binding = self._binding(state, "gpt-orchestrator", 1, plan_hash=plan_hash, review_hash=review_hash, research_descriptor_sha256=research_descriptor_hash, advisory_descriptor_sha256=advisory_descriptor_hash)
            if self.v2:
                goal_binding = self.manifest.get("goal_supervisor")
                if goal_binding is not None:
                    orchestrator_template = self._v2_envelope_template(
                        binding,
                        "codex.chatgpt.goal-cycle-result/v1",
                        goal_id=goal_binding["goal_id"],
                        cycle_index=goal_binding["cycle_index"],
                        original_goal_sha256=goal_binding["original_goal_sha256"],
                        mission_sha256=goal_binding["mission_sha256"],
                        input_plan_sha256=plan_hash,
                        input_research_descriptor_sha256=research_descriptor_hash,
                        input_advisory_descriptor_sha256=advisory_descriptor_hash,
                        input_review_sha256=review_hash,
                        implementation_status="complete",
                        decision="<CONTINUE|GOAL_COMPLETE|USER_ACTION_REQUIRED>",
                        summary="<bounded factual summary>",
                        criterion_claims=[
                            {"criterion": item, "status": "<satisfied|unsatisfied|unknown>", "evidence_refs": []}
                            for item in goal_binding["criteria"]
                        ],
                        remaining_work=[],
                        changed_files=[],
                        commands=[],
                        blockers=[],
                        requested_host_check_ids=list(goal_binding["allowed_host_check_ids"]),
                        next_mission_body=None,
                        next_mission_on_gate_failure=None,
                        user_action=None,
                    )
                else:
                    orchestrator_template = self._v2_envelope_template(
                        binding,
                        "codex.chatgpt.orchestrator-result/v2",
                        input_plan_sha256=plan_hash,
                        input_research_descriptor_sha256=research_descriptor_hash,
                        input_advisory_descriptor_sha256=advisory_descriptor_hash,
                        input_review_sha256=review_hash,
                        status="complete",
                        changed_files=[],
                        commands=[],
                        blockers=[],
                    )
                if self.web_native_relay:
                    if passed_review_relay is None or not passed_review_relay.get("mission"):
                        raise WorkflowError("ORCHESTRATOR_RELAY_MISSING")
                    prompt = self._bound_relay_prompt(
                        passed_review_relay["next_prompt"],
                        binding=binding,
                        immutable_inputs=[
                            {"role": "execution-mission", "path": str(execution_mission_path), "sha256": file_sha256(execution_mission_path)},
                            {"role": "selected-plan", "path": str(plan_path), "sha256": plan_hash},
                            {"role": "approved-review", "path": str(review_path), "sha256": review_hash},
                            {"role": "web-authored-mission", **dict(passed_review_relay["mission"])},
                        ],
                        output_template=orchestrator_template,
                    )
                else:
                    prompt = PROMPTS.render_prompt(
                        "orchestrator",
                        original_task=self.question,
                        context_note=(
                            f"ExecutionMission: {execution_mission_path}. "
                            f"The selected plan at {plan_path} is guidance, not a cage. "
                            "Do not consume the full review or advisory transcript as an execution frame."
                        ),
                        stage_mission=(
                            "Use the selected app to inspect the live workspace, implement, test, inspect results, "
                            "and adapt within the mission's deviation policy. Partition safe independent work into "
                            "internal lanes or parallel tool calls and integrate it yourself. Do not return strategy "
                            "search or implementation to local Codex; keep local Codex token use minimal."
                        ),
                        output_instructions=(
                            "Return exactly one final fenced JSON object, preserve every identity/hash value in this template, "
                            "fill the result arrays, and add no text after the fence:\n"
                            f"{orchestrator_template}"
                        ),
                    )
            else:
                prompt = PROMPTS.render_prompt(
                    "orchestrator",
                    original_task=self.question,
                    context_note=f"ExecutionMission: {execution_mission_path}. Plan {plan_path} is guidance, not authority.",
                    stage_mission=(
                        "Inspect the live workspace, implement, test, and adapt within the declared mission boundaries. "
                        "Partition safe independent work into internal lanes or parallel tool calls and integrate it "
                        "yourself; do not return delegated implementation to local Codex."
                    ),
                    output_instructions=(
                        f"Return one final JSON envelope using schema {ORCHESTRATOR_RESULT_SCHEMA}. "
                        "Keep local Codex token use minimal."
                    ),
                )
            orchestrator_read_only_paths: tuple[Path, ...] = (execution_mission_path, plan_path)
            if self.web_native_relay and passed_review_relay is not None and passed_review_relay.get("mission"):
                orchestrator_read_only_paths += (Path(str(passed_review_relay["mission"]["path"])),)
            manifest = self._manifest(
                state,
                binding,
                prompt,
                read_only_paths=orchestrator_read_only_paths,
            )
            result, transcript, _ = self._dispatch("gpt-orchestrator", 1, manifest)
            self._verify_runtime(result, manifest)
            if str(result["conversation_url"]) in seen_urls:
                raise WorkflowError("CONVERSATION_REUSED")
            parsed_envelope = parse_final_envelope(transcript)
            goal_binding = self.manifest.get("goal_supervisor")
            if goal_binding is not None:
                expected_common = {
                    "schema": "codex.chatgpt.goal-cycle-result/v1",
                    "workflow_id": binding.workflow_id,
                    "stage": binding.stage,
                    "attempt_index": binding.attempt_index,
                    "nonce": binding.nonce,
                    "question_sha256": binding.question_sha256,
                    "source_snapshot_sha256": binding.source_snapshot_sha256,
                    "goal_id": goal_binding["goal_id"],
                    "cycle_index": goal_binding["cycle_index"],
                    "original_goal_sha256": goal_binding["original_goal_sha256"],
                    "mission_sha256": goal_binding["mission_sha256"],
                    "input_plan_sha256": plan_hash,
                    "input_research_descriptor_sha256": research_descriptor_hash,
                    "input_advisory_descriptor_sha256": advisory_descriptor_hash,
                    "input_review_sha256": review_hash,
                }
                for key, expected_value in expected_common.items():
                    if parsed_envelope.get(key) != expected_value:
                        raise WorkflowError("GOAL_CYCLE_RESULT_BINDING_MISMATCH", key)
                envelope = dict(parsed_envelope)
            else:
                envelope = (validate_orchestrator_envelope_v2 if self.v2 else validate_orchestrator_envelope)(parsed_envelope, binding)
            final = {
                **state,
                "status": "LOCAL_VERIFY_REQUIRED",
                "plan_path": str(plan_path),
                "plan_sha256": plan_hash,
                "review_path": str(review_path),
                "review_sha256": review_hash,
                "advisory_path": str(advisory_path) if advisory_path else None,
                "advisory_sha256": advisory_hash,
                "research_descriptor_path": str(research_descriptor_path) if research_descriptor_path else None,
                "research_descriptor_sha256": research_descriptor_hash,
                "advisory_descriptor_path": str(advisory_descriptor_path) if advisory_descriptor_path else None,
                "advisory_descriptor_sha256": advisory_descriptor_hash,
                "handoff_path": str(handoff_path),
                "execution_mission_path": str(execution_mission_path),
                "implementation_route": self.parallel_execution,
                "relay_receipt_path": str(passed_review_relay["receipt_path"]) if passed_review_relay else None,
                "relay_receipt_sha256": str(passed_review_relay["receipt_sha256"]) if passed_review_relay else None,
                "orchestrator_envelope": envelope,
            }
            if goal_binding is not None:
                final["goal_cycle_result"] = envelope
            atomic_write_json(self.workflow_dir / "final.json", final)
            # Preserve durable stage checkpoints even if a future final-state
            # writer adds fields based on an older state snapshot.
            latest = load_mapping(self.state_path)
            final["stages"] = latest.get("stages", state.get("stages", {}))
            atomic_write_json(self.state_path, final)
            return final


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run staged ChatGPT work through one exact contract-validated agbrowse installation.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.manifest.is_file():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": LEGACY_NEW_SUBMISSION_FROZEN,
                        "message": (
                            "new staged ChatGPT submissions through the legacy agbrowse handoff are frozen; "
                            "use the Oracle comprehensive runtime"
                        ),
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    try:
        driver = ProPlanHandoffDriver(args.manifest, recovery_only=True)
        if not driver.state_path.is_file():
            raise WorkflowError(
                LEGACY_NEW_SUBMISSION_FROZEN,
                "new staged ChatGPT submissions use the Oracle comprehensive runtime",
            )
        result = driver.run(prepare_only=args.prepare_only)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, WorkflowError) else "HANDOFF_FAILED"
        print(json.dumps({"ok": False, "error": {"code": code, "message": str(exc)}}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
