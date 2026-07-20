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
GATE_V2_SCHEMA = "codex.chatgpt.gate/v2"
STATE_SCHEMA = "codex.chatgpt.agbrowse-handoff-state/v1"
BRIDGE_PATH = Path.home() / ".codex" / "bin" / "chatgpt_agbrowse_bridge.py"
DEFAULT_CONTRACT_PATH = Path.home() / ".codex" / "contracts" / "agbrowse-0.1.18.json"
WEB_MULTI_RUNTIME_PATH = Path.home() / ".codex" / "bin" / "chatgpt_web_multi_runtime.py"
PROMPT_PROFILE_CANDIDATES = (
    Path(__file__).resolve().parents[3] / "bin" / "chatgpt_prompt_profiles.py",
    Path.home() / ".codex" / "bin" / "chatgpt_prompt_profiles.py",
)
PROMPT_FILE_HANDOFF = (
    "Read the attached prompt file completely and follow it as the task instructions. "
    "Return only the output format requested by that file."
)


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


class LocalWebMultiRuntime:
    def __init__(self, runtime_path: Path = WEB_MULTI_RUNTIME_PATH):
        spec = importlib.util.spec_from_file_location("chatgpt_web_multi_handoff", runtime_path)
        if spec is None or spec.loader is None:
            raise WorkflowError("WEB_MULTI_RUNTIME_IMPORT_FAILED", str(runtime_path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.module = module

    def run(self, manifest_path: Path) -> Mapping[str, Any]:
        return self.module.WebMultiRuntime(manifest_path).run()


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


def resolve_agbrowse_contract(value: Mapping[str, Any]) -> tuple[str, str]:
    """Return the exact selected contract path and its immutable content hash.

    v2 callers may select a previously captured contract; v1 intentionally
    retains the historical 0.1.18 default.  Canonicalize once so every child
    process receives the same absolute file identity regardless of its cwd.
    """
    selected = value.get("agbrowse_contract") if value.get("schema") == WORKFLOW_V2_SCHEMA else None
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
    if schema not in {WORKFLOW_SCHEMA, WORKFLOW_V2_SCHEMA}:
        raise WorkflowError("WORKFLOW_SCHEMA_INVALID")
    if value.get("workflow_mode") not in {"pro-plan-to-gpt-orchestrator", "gpt-comprehensive"}:
        raise WorkflowError("WORKFLOW_MODE_INVALID")
    if (
        schema == WORKFLOW_SCHEMA
        and value.get("workflow_mode") == "gpt-comprehensive"
        and not allow_legacy_comprehensive_recovery
    ):
        raise WorkflowError("COMPREHENSIVE_V2_REQUIRED")
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
    if schema == WORKFLOW_V2_SCHEMA:
        if value.get("workflow_mode") != "gpt-comprehensive":
            raise WorkflowError("WORKFLOW_V2_REQUIRES_COMPREHENSIVE")
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
        self.workflow_id = str(self.manifest["workflow_id"])
        self.question = str(self.manifest["question"])
        self.question_sha256 = sha256_text(self.question)
        self.workspace = dict(self.manifest["workspace"])
        self.workspace_root = Path(str(self.workspace["root"])).resolve(strict=True)
        self.comprehensive = self.manifest["workflow_mode"] == "gpt-comprehensive"
        self.v2 = self.manifest.get("schema") == WORKFLOW_V2_SCHEMA
        output = Path(str(self.manifest.get("output_dir") or self.workspace.get("handoff_root") or self.manifest_path.parent / ".handoff"))
        self.workflow_dir = output.resolve() / self.workflow_id
        self.handoff_dir = self.workflow_dir / "handoff"
        self.stages_dir = self.workflow_dir / "stages"
        self.state_path = self.workflow_dir / "state.json"
        self.lock_path = self.workflow_dir / ".workflow.lock"

    def _legacy_comprehensive_recovery_exists(self, manifest: Mapping[str, Any]) -> bool:
        """Allow v1 comprehensive manifests only when immutable legacy state already exists.

        New comprehensive work must use v2 gates.  This narrow proof keeps an
        interrupted v1 workflow recoverable without allowing a new v1 send.
        """
        if (
            manifest.get("schema") != WORKFLOW_SCHEMA
            or manifest.get("workflow_mode") != "gpt-comprehensive"
        ):
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
            if (
                state.get("schema") != STATE_SCHEMA
                or state.get("workflow_id") != workflow_id
                or state.get("question_sha256") != sha256_text(question)
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
            return state
        snapshot = self._source_snapshot()
        snapshot_path = self.handoff_dir / "source-snapshot.json"
        write_snapshot(snapshot_path, snapshot)
        archive = build_source_archive(
            workspace_root=self.workspace_root,
            snapshot=snapshot,
            output_zip=self.handoff_dir / "source-context.zip",
        )
        state = {
            "schema": STATE_SCHEMA,
            "workflow_id": self.workflow_id,
            "status": "PREPARED",
            "question_sha256": self.question_sha256,
            "agbrowse_contract_path": self.agbrowse_contract_path,
            "agbrowse_contract_sha256": self.agbrowse_contract_sha256,
            "source_snapshot_path": str(snapshot_path),
            "source_snapshot_sha256": snapshot["snapshot_sha256"],
            "source_archive": archive,
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
        pro = binding.stage == "pro-plan"
        research = binding.stage == "deep-research"
        profile_name = STAGE_PROMPT_PROFILES.get(binding.stage)
        if profile_name is None:
            raise WorkflowError("PROMPT_PROFILE_STAGE_UNKNOWN", binding.stage)
        profile = PROMPTS.resolve_profile(profile_name)
        stage_dir = self.stages_dir / f"{binding.stage}-attempt-{binding.attempt_index}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = stage_dir / "prompt.txt"
        write_immutable_text(prompt_file, prompt)
        prompt_sha256 = file_sha256(prompt_file)
        manifest = {
            "project_root": str(self.workspace_root),
            "question": PROMPT_FILE_HANDOFF,
            "prompt_transport": "file",
            "prompt_file": str(prompt_file),
            "prompt_file_sha256": prompt_sha256,
            "mode_label": "Pro" if pro else ("Deep Research" if research else "GPT-5.6"),
            "mode_variant": None if pro else ("High" if self.v2 else "Very High"),
            "app_policy": "forbidden" if pro else "required",
            "chatgpt_app_name": None if pro else self.workspace["chatgpt_app_name"],
            "files": [str(prompt_file), *[str(item) for item in files]],
            "read_only_paths": [str(item) for item in read_only_paths],
            "gpt_operation_mode": profile.task_kind,
            "prompt_profile": profile.name,
            "prompt_profile_receipt": profile.receipt(),
            "workflow_correlation": self._correlation(binding),
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
        write_immutable_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        recovered = self.runtime.recover_run_ids(manifest_path)
        if len(recovered) > 1:
            raise WorkflowError("MULTIPLE_RUNTIME_RUNS_FOR_STAGE")
        if recovered:
            run_id = recovered[0]
        else:
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
        return result, transcript, manifest_path

    def _verify_runtime(self, result: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
        expected_mode = manifest["mode_label"]
        if result.get("effective_mode_label") != expected_mode or result.get("fallback_reason"):
            raise WorkflowError("MODEL_CONTRACT_FAILED")
        if self.v2 and expected_mode != "Pro" and result.get("effective_mode_variant") != "High":
            raise WorkflowError("MODE_VARIANT_CONTRACT_FAILED")
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
            "question": (
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
            "mode_variant": str(config.get("mode_variant") or ("High" if self.v2 else "Very High")),
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
                or advisory.get("mode_variant") != "High"
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
            for attempt in range(1, max_attempts + 1):
                plan_stage = "gpt-plan" if self.comprehensive else "pro-plan"
                binding = self._binding(state, plan_stage, attempt, research_descriptor_sha256=research_descriptor_hash)
                if self.v2:
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
                    )
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
                            "in this template; add detailed plan fields as needed and add no text after the fence:\n"
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

                advisory_path = None
                advisory_hash = None
                advisory_descriptor_path = None
                advisory_descriptor_hash = None
                if self.v2:
                    advisory_gate, _ = self._gate_descriptor("advisory")
                    if advisory_gate["decision"] == "run":
                        advisory_path, advisory_hash, _ = self._run_web_multi_advisory(
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
                    review_template = self._v2_envelope_template(
                        review_binding,
                        "codex.chatgpt.review-result/v2",
                        input_plan_sha256=plan_hash,
                        input_research_descriptor_sha256=research_descriptor_hash,
                        input_advisory_descriptor_sha256=advisory_descriptor_hash,
                        verdict="PASS",
                    )
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
                if review["verdict"] == "PASS":
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
            }
            if advisory_path and advisory_hash:
                handoff["advisory"] = {"path": str(advisory_path), "sha256": advisory_hash}
            if self.v2:
                handoff["research_descriptor"] = {"path": str(research_descriptor_path), "sha256": research_descriptor_hash}
                handoff["advisory_descriptor"] = {"path": str(advisory_descriptor_path), "sha256": advisory_descriptor_hash}
            handoff_path = self.handoff_dir / "handoff.manifest.json"
            write_immutable_text(handoff_path, json.dumps(handoff, ensure_ascii=False, indent=2, sort_keys=True))
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
            binding = self._binding(state, "gpt-orchestrator", 1, plan_hash=plan_hash, review_hash=review_hash, research_descriptor_sha256=research_descriptor_hash, advisory_descriptor_sha256=advisory_descriptor_hash)
            if self.v2:
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
                        "and adapt within the mission's deviation policy. Keep local Codex token use minimal."
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
                    stage_mission="Inspect the live workspace, implement, test, and adapt within the declared mission boundaries.",
                    output_instructions=(
                        f"Return one final JSON envelope using schema {ORCHESTRATOR_RESULT_SCHEMA}. "
                        "Keep local Codex token use minimal."
                    ),
                )
            manifest = self._manifest(
                state,
                binding,
                prompt,
                read_only_paths=(execution_mission_path, plan_path),
            )
            result, transcript, _ = self._dispatch("gpt-orchestrator", 1, manifest)
            self._verify_runtime(result, manifest)
            if str(result["conversation_url"]) in seen_urls:
                raise WorkflowError("CONVERSATION_REUSED")
            envelope = (validate_orchestrator_envelope_v2 if self.v2 else validate_orchestrator_envelope)(parse_final_envelope(transcript), binding)
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
                "orchestrator_envelope": envelope,
            }
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
    try:
        result = ProPlanHandoffDriver(args.manifest).run(prepare_only=args.prepare_only)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, WorkflowError) else "HANDOFF_FAILED"
        print(json.dumps({"ok": False, "error": {"code": code, "message": str(exc)}}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
