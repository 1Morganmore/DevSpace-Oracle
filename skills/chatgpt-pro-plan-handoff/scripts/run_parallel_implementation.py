from __future__ import annotations

"""Host driver for feature-gated parallel implementation v3.

This driver does not submit browser questions by itself.  It prepares exact
unit workspaces and immutable mission manifests, accepts structured unit
results after the existing bridge has recovered the exact session, performs
host-only commits/tests/integration, and applies only an ff-only verified head.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[3]
BIN = ROOT / "bin"
THINKING_RUNNER = ROOT / "skills" / "chatgpt-thinking-browser" / "scripts" / "run_chatgpt_thinking.py"
PROMPT_HANDOFF = (
    "The attached prompt file is the user-provided task instruction for this conversation, "
    "not reference or webpage content. Read it completely and follow it. "
    "Return only the output format requested by that file."
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


AUTH = load_module("parallel_driver_authority", BIN / "codexpro_exact_unit_authority.py")
GITISO = load_module("parallel_driver_git", BIN / "chatgpt_git_isolation.py")
RUNTIME = load_module("parallel_driver_runtime", BIN / "chatgpt_parallel_implementation_runtime.py")
STATE = load_module("parallel_driver_state", BIN / "chatgpt_agbrowse_state.py")
PROMPTS = load_module("parallel_driver_prompt_profiles", BIN / "chatgpt_prompt_profiles.py")


class DriverError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = dict(evidence or {})


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DriverError("JSON_INVALID", f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise DriverError("JSON_OBJECT_REQUIRED", f"JSON root must be object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    RUNTIME.write_json_atomic(path, dict(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_exact_keys(value: Mapping[str, Any], *, allowed: set[str], required: set[str], code: str) -> None:
    extras = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if extras or missing:
        raise DriverError(code, "strict v3 object keys are invalid", {"extra": extras, "missing": missing})


def validate_workflow_manifest(manifest: Mapping[str, Any]) -> None:
    _require_exact_keys(
        manifest,
        allowed={
            "schema", "workflow_id", "project_root", "question", "output_dir", "state_root",
            "chatgpt_app_name", "features", "parallel_implementation", "agbrowse_contract",
            "timeout_seconds", "max_repair_attempts_per_unit", "max_integration_repair_attempts",
        },
        required={
            "schema", "workflow_id", "project_root", "question", "output_dir",
            "chatgpt_app_name", "features", "parallel_implementation",
        },
        code="WORKFLOW_V3_KEYS_INVALID",
    )
    RUNTIME.assert_feature_enabled(manifest)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(manifest.get("workflow_id") or "")) is None:
        raise DriverError("WORKFLOW_ID_INVALID", "workflow_id is not a bounded identifier")
    for key in ("project_root", "question", "output_dir", "chatgpt_app_name"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise DriverError("WORKFLOW_FIELD_INVALID", "required workflow string is empty", {"field": key})
    features = manifest.get("features")
    if not isinstance(features, Mapping):
        raise DriverError("WORKFLOW_FEATURES_INVALID", "features must be an object")
    _require_exact_keys(
        features,
        allowed={"parallel_implementation_v1"},
        required={"parallel_implementation_v1"},
        code="WORKFLOW_FEATURES_INVALID",
    )
    parallel = manifest.get("parallel_implementation")
    if not isinstance(parallel, Mapping):
        raise DriverError("PARALLEL_IMPLEMENTATION_CONFIG_INVALID", "parallel_implementation must be an object")
    _require_exact_keys(
        parallel,
        allowed={"enabled", "max_units", "test_registry", "full_test_ids", "allowed_claim_roots"},
        required={"enabled", "max_units", "test_registry", "full_test_ids"},
        code="PARALLEL_IMPLEMENTATION_CONFIG_INVALID",
    )
    if parallel.get("enabled") is not True or not isinstance(parallel.get("max_units"), int) or not 1 <= parallel["max_units"] <= 64:
        raise DriverError("PARALLEL_IMPLEMENTATION_CONFIG_INVALID", "parallel implementation enable/max_units contract is invalid")
    registry = parallel.get("test_registry")
    if not isinstance(registry, Mapping) or not 1 <= len(registry) <= 128:
        raise DriverError("TEST_REGISTRY_REQUIRED", "parallel implementation requires 1..128 registered tests")
    for test_id, spec in registry.items():
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(test_id)) is None or not isinstance(spec, Mapping):
            raise DriverError("TEST_REGISTRY_INVALID", "test registry entry is invalid", {"test_id": str(test_id)})
        _require_exact_keys(
            spec,
            allowed={"argv", "cwd", "timeout_seconds"},
            required={"argv", "timeout_seconds"},
            code="TEST_REGISTRY_INVALID",
        )
        argv = spec.get("argv")
        if not isinstance(argv, list) or not 1 <= len(argv) <= 32 or any(not isinstance(item, str) or not item or len(item) > 1024 for item in argv):
            raise DriverError("TEST_ARGV_INVALID", "registered test argv is invalid", {"test_id": str(test_id)})
        timeout = spec.get("timeout_seconds")
        if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            raise DriverError("TEST_TIMEOUT_INVALID", "registered test timeout is invalid", {"test_id": str(test_id)})
        cwd = str(spec.get("cwd") or ".")
        if Path(cwd).is_absolute() or ".." in Path(cwd).parts:
            raise DriverError("TEST_CWD_INVALID", "registered test cwd must be canonical relative", {"test_id": str(test_id)})
    full_ids = parallel.get("full_test_ids")
    if not isinstance(full_ids, list) or not full_ids or len(full_ids) != len(set(map(str, full_ids))):
        raise DriverError("FULL_TEST_IDS_INVALID", "full_test_ids must be a non-empty unique array")
    unknown = sorted(str(item) for item in full_ids if str(item) not in registry)
    if unknown:
        raise DriverError("TEST_ID_UNKNOWN", "full tests reference unknown registry IDs", {"test_ids": unknown})


def _planned_final(path: Path) -> Path:
    current = path.absolute()
    suffix: list[str] = []
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise DriverError("WORKFLOW_PATH_ANCESTOR_MISSING", "planned path has no existing ancestor", {"path": str(path)})
        suffix.append(current.name)
        current = parent
    final = current.resolve(strict=True)
    for name in reversed(suffix):
        final = final / name
    return final


def _paths_overlap(left: Path, right: Path) -> bool:
    a = _planned_final(left)
    b = _planned_final(right)
    try:
        b.relative_to(a)
        return True
    except ValueError:
        pass
    try:
        a.relative_to(b)
        return True
    except ValueError:
        return False


def manifest_paths(manifest_path: Path) -> tuple[dict[str, Any], Path, Path]:
    manifest = read_json(manifest_path)
    # This check is deliberately first: no output directory, lease, clone, app,
    # or browser side effect exists unless both gates are explicit.
    validate_workflow_manifest(manifest)
    project_root = Path(str(manifest.get("project_root") or "")).expanduser().resolve(strict=True)
    output_dir = Path(str(manifest.get("output_dir") or "")).expanduser().absolute()
    state_root = Path(str(manifest.get("state_root") or (Path.home() / ".codex" / "state" / "chatgpt-agbrowse"))).expanduser().absolute()
    for label, path in (("output_dir", output_dir), ("state_root", state_root)):
        if _paths_overlap(project_root, path):
            raise DriverError("WORKFLOW_RUNTIME_PATH_OVERLAP", "v3 runtime/output path overlaps canonical source", {"field": label, "path": str(path)})
    return manifest, project_root, output_dir


def runtime_files(parent_run_dir: Path) -> dict[str, Path]:
    runtime_root = parent_run_dir / AUTH.RUNTIME_DIR_NAME
    return {
        "runtime_root": runtime_root,
        "control": runtime_root / "control.json",
        "bound_graph": runtime_root / "bound-graph.json",
        "runtime_state": runtime_root / "runtime-state.json",
        "baseline": runtime_root / "canonical-baseline.json",
        "clone": runtime_root / "staging-clone.json",
        "staging": runtime_root / AUTH.STAGING_DIR_NAME,
        "worktrees": runtime_root / AUTH.WORKTREES_DIR_NAME,
        "aggregate": runtime_root / AUTH.AGGREGATE_DIR_NAME,
        "missions": runtime_root / "missions",
        "results": runtime_root / "unit-results",
        "recovery": runtime_root / "recovery",
    }


def load_control(parent_run_dir: Path) -> tuple[dict[str, Any], dict[str, Path], dict[str, Any], dict[str, Any]]:
    files = runtime_files(parent_run_dir)
    control = read_json(files["control"])
    state = read_json(files["runtime_state"])
    graph = read_json(files["bound_graph"])
    return control, files, state, graph


def test_registry(manifest: Mapping[str, Any]) -> dict[str, Any]:
    parallel = manifest.get("parallel_implementation") if isinstance(manifest.get("parallel_implementation"), Mapping) else {}
    registry = parallel.get("test_registry") if isinstance(parallel.get("test_registry"), Mapping) else {}
    if not registry:
        raise DriverError("TEST_REGISTRY_REQUIRED", "parallel implementation requires a bounded test registry")
    return {str(key): dict(value) for key, value in registry.items() if isinstance(value, Mapping)}


def validate_graph_against_manifest(graph: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    units = graph.get("units") if isinstance(graph.get("units"), list) else []
    parallel = manifest["parallel_implementation"]
    if len(units) > int(parallel["max_units"]):
        raise DriverError("IMPLEMENTATION_UNIT_COUNT_EXCEEDS_MANIFEST", "graph exceeds manifest max_units")
    registry_ids = set(test_registry(manifest))
    allowed_roots = [RUNTIME.canonical_relpath(item) for item in (parallel.get("allowed_claim_roots") or [])]
    for unit in units:
        if not isinstance(unit, Mapping):
            continue
        unknown_tests = sorted(str(item) for item in (unit.get("test_ids") or []) if str(item) not in registry_ids)
        if unknown_tests:
            raise DriverError("TEST_ID_UNKNOWN", "implementation unit references unknown registry IDs", {"unit_id": unit.get("unit_id"), "test_ids": unknown_tests})
        if allowed_roots:
            for claim in unit.get("claimed_paths") or []:
                canonical = RUNTIME.canonical_relpath(claim)
                if not any(canonical == root or canonical.startswith(root + "/") for root in allowed_roots):
                    raise DriverError("IMPLEMENTATION_CLAIM_OUTSIDE_MANIFEST", "unit claim is outside manifest allowed roots", {"unit_id": unit.get("unit_id"), "path": canonical})


def run_registered_tests(root: Path, registry: Mapping[str, Any], test_ids: list[str], evidence_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for test_id in test_ids:
        spec = registry.get(test_id)
        if not isinstance(spec, Mapping):
            raise DriverError("TEST_ID_UNKNOWN", "unit or full test references an unknown registry ID", {"test_id": test_id})
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
            raise DriverError("TEST_ARGV_INVALID", "test registry argv is invalid", {"test_id": test_id})
        cwd_rel = str(spec.get("cwd") or ".")
        cwd = (root / cwd_rel).resolve(strict=True)
        try:
            cwd.relative_to(root.resolve(strict=True))
        except ValueError as exc:
            raise DriverError("TEST_CWD_ESCAPE", "registered test cwd escapes its workspace", {"test_id": test_id}) from exc
        timeout = int(spec.get("timeout_seconds") or 300)
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env=GITISO.isolated_git_env(root.parent / ".test-home"),
        )
        evidence = {
            "test_id": test_id,
            "argv": argv,
            "cwd": str(cwd),
            "returncode": completed.returncode,
            "stdout": completed.stdout[-20000:],
            "stderr": completed.stderr[-20000:],
        }
        evidence_path = evidence_dir / f"{test_id}.json"
        write_json(evidence_path, evidence)
        result = {"test_id": test_id, "result": "PASS" if completed.returncode == 0 else "FAIL", "evidence_sha256": sha256_file(evidence_path)}
        results.append(result)
        if completed.returncode != 0:
            raise DriverError("REGISTERED_TEST_FAILED", "registered test failed", {"test_id": test_id, "evidence": str(evidence_path)})
    return results


def validate_unit_result(result: Mapping[str, Any]) -> None:
    _require_exact_keys(
        result,
        allowed={
            "schema", "unit_id", "attempt_id", "input_base_oid", "status", "summary",
            "changed_paths", "test_results", "blocked_conditions",
        },
        required={
            "schema", "unit_id", "attempt_id", "input_base_oid", "status", "summary",
            "changed_paths", "test_results",
        },
        code="UNIT_RESULT_KEYS_INVALID",
    )
    if result.get("schema") != "codex.chatgpt.implementation-unit-result/v1":
        raise DriverError("UNIT_RESULT_SCHEMA_INVALID", "unit result schema is not exact")
    for key in ("unit_id", "attempt_id"):
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(result.get(key) or "")) is None:
            raise DriverError("UNIT_RESULT_ID_INVALID", "unit result ID is invalid", {"field": key})
    if re.fullmatch(r"[0-9a-f]{40,64}", str(result.get("input_base_oid") or "")) is None:
        raise DriverError("UNIT_RESULT_INPUT_BASE_INVALID", "unit result input base is invalid")
    if result.get("status") not in {"IMPLEMENTED", "NO_CHANGE", "BLOCKED"}:
        raise DriverError("UNIT_RESULT_STATUS_INVALID", "unit result status is invalid")
    if not isinstance(result.get("summary"), str) or not result["summary"] or len(result["summary"]) > 20000:
        raise DriverError("UNIT_RESULT_SUMMARY_INVALID", "unit result summary is invalid")
    changed = result.get("changed_paths")
    if not isinstance(changed, list) or len(changed) > 256 or len(changed) != len(set(map(str, changed))):
        raise DriverError("UNIT_RESULT_CHANGED_PATHS_INVALID", "unit result changed_paths are invalid")
    for path in changed:
        RUNTIME.canonical_relpath(path)
    tests = result.get("test_results")
    if not isinstance(tests, list) or len(tests) > 128:
        raise DriverError("UNIT_RESULT_TESTS_INVALID", "unit result test_results are invalid")
    for item in tests:
        if not isinstance(item, Mapping):
            raise DriverError("UNIT_RESULT_TESTS_INVALID", "unit result test item must be an object")
        _require_exact_keys(
            item,
            allowed={"test_id", "result", "detail"},
            required={"test_id", "result"},
            code="UNIT_RESULT_TESTS_INVALID",
        )
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(item.get("test_id") or "")) is None or item.get("result") not in {"PASS", "FAIL", "SKIP"}:
            raise DriverError("UNIT_RESULT_TESTS_INVALID", "unit result test identity/result is invalid")


def unit_spec(graph: Mapping[str, Any], unit_id: str) -> dict[str, Any]:
    for unit in graph.get("units") or []:
        if isinstance(unit, Mapping) and str(unit.get("unit_id") or "") == unit_id:
            return dict(unit)
    raise DriverError("UNIT_UNKNOWN", "unit is not present in bound graph", {"unit_id": unit_id})


def dispatch_ready(parent_run_dir: Path, manifest: Mapping[str, Any], capacity_receipt: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    control, files, state, graph = load_control(parent_run_dir)
    staging = files["staging"]
    clone = read_json(files["clone"])
    common = str(clone["staging_common_git_dir"])
    receipt = capacity_receipt or control.get("capacity_receipt")
    if not isinstance(receipt, Mapping):
        receipt = RUNTIME.serial_capacity_receipt(
            parent_run_id=str(control["parent_run_id"]),
            canonical_baseline_identity_sha256=str(control["canonical_baseline_identity_sha256"]),
        )
    receipt = RUNTIME.validate_capacity_receipt(
        receipt,
        parent_run_id=str(control["parent_run_id"]),
        canonical_baseline_identity_sha256=str(control["canonical_baseline_identity_sha256"]),
    )
    dispatched: list[dict[str, Any]] = []
    existing_roots = [str(item.get("unit_workspace_root") or "") for item in control.get("dispatches") or [] if item.get("unit_workspace_root")]
    # This mutates only durable scheduler state.  Each selected unit gets its
    # own child manifest/worktree, so separate provider sessions can genuinely
    # run concurrently when the current receipt exposes more than one slot.
    for item in RUNTIME.capacity_dispatchable_units(state, receipt):
        component_id = item["component_id"]
        unit_id = item["unit_id"]
        attempt_id = f"a-{len(control.get('dispatches') or []) + len(dispatched) + 1:04d}"
        RUNTIME.start_unit(state, component_id=component_id, unit_id=unit_id, attempt_id=attempt_id)
        topology_inputs = {
            "state_root": str(control["state_root"]),
            "canonical_project_key": str(control["project_key"]),
            "parent_run_id": str(control["parent_run_id"]),
            "component_id": component_id,
            "unit_id": unit_id,
            "attempt_id": attempt_id,
            "canonical_project_root": str(control["canonical_project_root"]),
            "staging_common_git_dir": common,
            "allowed_roots": [AUTH.derive_parent_run_topology(control["state_root"], control["project_key"], control["parent_run_id"], component_id, unit_id, attempt_id)["unit_workspace_root"]],
            "sibling_unit_roots": existing_roots,
        }
        planned = AUTH.validate_and_build(topology_inputs, phase="planned")
        unit_root = Path(planned["unit_workspace_root"]["logical"])
        GITISO.create_unit_worktree(staging, unit_root, item["input_base_oid"])
        topology_inputs["allowed_roots"] = [str(unit_root)]
        materialized = AUTH.validate_and_build(topology_inputs, phase="materialized")
        spec = unit_spec(graph, unit_id)
        mission = {
            "schema": "codex.chatgpt.execution-mission/v2",
            "workflow_id": str(manifest["workflow_id"]),
            "parent_run_id": str(control["parent_run_id"]),
            "component_id": component_id,
            "unit_id": unit_id,
            "attempt_id": attempt_id,
            "input_base_oid": item["input_base_oid"],
            "claimed_paths": spec["claimed_paths"],
            "test_ids": spec["test_ids"],
            "topology_receipt_sha256": materialized["topology_receipt_sha256"],
            "capacity_receipt_sha256": item["capacity_receipt_sha256"],
            "instructions": str(spec.get("mission") or "Implement the assigned unit without changing unclaimed paths."),
        }
        mission_dir = files["missions"] / unit_id / attempt_id
        mission_path = mission_dir / "execution-mission-v2.json"
        write_json(mission_path, mission)
        prompt_path = mission_dir / "prompt.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(
            "Implement this exact unit in the connected CodexPro workspace. Do not run Git commands or modify .git. "
            "Return only implementation-unit-result-v1 JSON.\n\n" + json.dumps(mission, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        child_manifest = {
            "schema": "codex.chatgpt.execution-unit/v3",
            "project_root": str(unit_root),
            "canonical_project_root": str(control["canonical_project_root"]),
            "workflow_id": str(manifest["workflow_id"]),
            "parent_run_id": str(control["parent_run_id"]),
            "component_id": component_id,
            "unit_id": unit_id,
            "attempt_id": attempt_id,
            "input_base_oid": item["input_base_oid"],
            "question": PROMPT_HANDOFF,
            "prompt_transport": "file",
            "prompt_file": str(prompt_path),
            "prompt_file_sha256": sha256_file(prompt_path),
            "files": [str(prompt_path)],
            "mode": "GPT-5.6",
            "mode_variant": str(PROMPTS.resolve_regular_mode_selection()["selected_mode_variant"]),
            "app_policy": "required",
            "app_scope_mode": "parallel-exact-unit",
            "topology_receipt_sha256": materialized["topology_receipt_sha256"],
        }
        child_manifest_path = mission_dir / "child-manifest.json"
        write_json(child_manifest_path, child_manifest)
        dispatch = {
            "component_id": component_id,
            "unit_id": unit_id,
            "attempt_id": attempt_id,
            "input_base_oid": item["input_base_oid"],
            "unit_workspace_root": str(unit_root),
            "topology_receipt_sha256": materialized["topology_receipt_sha256"],
            "capacity_receipt_sha256": item["capacity_receipt_sha256"],
            "queue_enqueue_seq": item["queue_enqueue_seq"],
            "mission_path": str(mission_path),
            "child_manifest_path": str(child_manifest_path),
            "state": "AWAITING_PROVIDER_RESULT",
        }
        dispatched.append(dispatch)
        existing_roots.append(str(unit_root))
    control.setdefault("dispatches", []).extend(dispatched)
    control["capacity_receipt"] = receipt
    control["staging_common_metadata"] = GITISO.common_metadata_identity(staging)
    write_json(files["control"], control)
    write_json(files["runtime_state"], state)
    return dispatched


def prepare(
    manifest_path: Path,
    graph_path: Path,
    *,
    initial_capacity_receipt: Mapping[str, Any] | None = None,
    initial_capacity_receipt_provider: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    manifest, project_root, output_dir = manifest_paths(manifest_path)
    graph_result = read_json(graph_path)
    validate_graph_against_manifest(graph_result, manifest)
    baseline = GITISO.canonical_snapshot(project_root)
    if baseline.get("status_empty") is not True:
        raise DriverError("CANONICAL_WORKTREE_DIRTY", "v3 preparation requires a clean canonical worktree")
    bound = RUNTIME.bind_graph(graph_result, baseline_oid=str(baseline["head_oid"]))
    state_root = Path(str(manifest.get("state_root") or (Path.home() / ".codex" / "state" / "chatgpt-agbrowse"))).expanduser().absolute()
    store = STATE.RunStore(state_root)
    parent = store.create_parent_workflow(
        project_root=project_root,
        manifest_path=manifest_path,
        workflow_id=str(manifest["workflow_id"]),
        parent_family="parallel-implementation",
        agbrowse_contract={"version": "v3", "feature": "parallel_implementation_v1"},
    )
    parent_run_dir = Path(parent["run_dir"])
    files = runtime_files(parent_run_dir)
    files["runtime_root"].mkdir(parents=True, exist_ok=False)
    files["worktrees"].mkdir(parents=True, exist_ok=False)
    files["missions"].mkdir(parents=True, exist_ok=False)
    files["results"].mkdir(parents=True, exist_ok=False)
    files["recovery"].mkdir(parents=True, exist_ok=False)
    try:
        clone = GITISO.safe_clone(project_root, files["staging"])
        runtime_state = RUNTIME.initial_runtime_state(
            bound,
            parent_run_id=str(parent["run_id"]),
            canonical_baseline_identity_sha256=str(baseline["baseline_identity_sha256"]),
        )
        control = {
            "schema": "codex.chatgpt.parallel-implementation-control/v1",
            "manifest_path": str(manifest_path.resolve(strict=True)),
            "manifest_sha256": sha256_file(manifest_path),
            "graph_path": str(graph_path.resolve(strict=True)),
            "bound_graph_sha256": bound["bound_graph_sha256"],
            "parent_run_id": str(parent["run_id"]),
            "parent_run_dir": str(parent_run_dir),
            "state_root": str(state_root),
            "project_key": str(parent["project_key"]),
            "canonical_project_root": str(project_root),
            "canonical_baseline_identity_sha256": baseline["baseline_identity_sha256"],
            "staging_clone_receipt_sha256": clone["staging_clone_receipt_sha256"],
            "dispatches": [],
            "unit_receipts": [],
            "full_test_results": [],
        }
        write_json(files["baseline"], baseline)
        write_json(files["clone"], clone)
        write_json(files["bound_graph"], bound)
        write_json(files["runtime_state"], runtime_state)
        write_json(files["control"], control)
        if initial_capacity_receipt is not None and initial_capacity_receipt_provider is not None:
            raise DriverError("INITIAL_CAPACITY_RECEIPT_AMBIGUOUS", "only one initial capacity source is allowed")
        initial_receipt = (
            initial_capacity_receipt_provider(control)
            if initial_capacity_receipt_provider is not None
            else initial_capacity_receipt
        )
        dispatched = dispatch_ready(parent_run_dir, manifest, initial_receipt)
        output_dir.mkdir(parents=True, exist_ok=True)
        pointer = {
            "schema": "codex.chatgpt.parallel-implementation-pointer/v1",
            "parent_run_id": parent["run_id"],
            "parent_run_dir": str(parent_run_dir),
            "runtime_state": str(files["runtime_state"]),
        }
        write_json(output_dir / "parallel-implementation.json", pointer)
        return {"status": "PREPARED", **pointer, "dispatches": dispatched}
    except Exception as exc:
        recovery = {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
        write_json(files["recovery"] / "prepare-failure.json", recovery)
        store.finalize_parent(parent_run_dir, "PARENT_FAILED_CLOSED", failure=recovery)
        raise


def record_unit(parent_run_dir: Path, result_path: Path, *, dispatch_next: bool = True) -> dict[str, Any]:
    control, files, state, graph = load_control(parent_run_dir)
    manifest = read_json(Path(control["manifest_path"]))
    result = read_json(result_path)
    validate_unit_result(result)
    unit_id = str(result.get("unit_id") or "")
    attempt_id = str(result.get("attempt_id") or "")
    dispatch = next((item for item in control.get("dispatches") or [] if item.get("unit_id") == unit_id and item.get("attempt_id") == attempt_id), None)
    if not isinstance(dispatch, dict):
        raise DriverError("UNIT_RESULT_ATTEMPT_UNKNOWN", "unit result does not match a dispatched attempt")
    unit = state["units"].get(unit_id)
    if not isinstance(unit, dict) or unit.get("state") != "ACTIVE" or unit.get("input_base_oid") != result.get("input_base_oid"):
        raise DriverError("UNIT_RESULT_INPUT_BASE_MISMATCH", "unit result is not bound to the active immutable base")
    current_metadata = GITISO.common_metadata_identity(files["staging"])
    GITISO.assert_common_metadata(control["staging_common_metadata"], current_metadata)
    unit_root = Path(dispatch["unit_workspace_root"])
    spec = unit_spec(graph, unit_id)
    status = str(result.get("status") or "")
    if status == "IMPLEMENTED":
        diff_receipt = GITISO.validate_unit_diff(unit_root, spec["claimed_paths"])
        actual_changed = sorted({
            str(item[key])
            for item in diff_receipt["changes"]
            for key in ("path", "old_path")
            if key in item
        })
        if actual_changed != sorted(str(item) for item in result["changed_paths"]):
            raise DriverError(
                "UNIT_RESULT_CHANGED_PATHS_MISMATCH",
                "worker changed_paths do not match host-derived diff",
                {"reported": sorted(result["changed_paths"]), "actual": actual_changed},
            )
        post_validation_metadata = GITISO.common_metadata_identity(files["staging"])
        tests = run_registered_tests(unit_root, test_registry(manifest), list(spec["test_ids"]), files["results"] / unit_id / attempt_id / "tests")
        GITISO.assert_common_metadata(post_validation_metadata, GITISO.common_metadata_identity(files["staging"]))
        commit = GITISO.deterministic_commit(
            unit_root,
            parent_oid=str(unit["input_base_oid"]),
            unit_id=unit_id,
            message=f"Implement unit {unit_id}",
        )
        RUNTIME.complete_unit(state, unit_id=unit_id, commit_oid=commit["commit_oid"])
        receipt = {
            "unit_id": unit_id,
            "attempt_id": attempt_id,
            "status": "INTEGRATED",
            "diff_validation_sha256": diff_receipt["unit_diff_validation_sha256"],
            "commit_receipt_sha256": commit["unit_commit_receipt_sha256"],
            "commit_oid": commit["commit_oid"],
            "tests": tests,
            "result_sha256": sha256_file(result_path),
        }
    elif status == "NO_CHANGE" and unit.get("required") is False:
        RUNTIME.skip_unit(state, unit_id=unit_id, reason="NO_CHANGE")
        receipt = {"unit_id": unit_id, "attempt_id": attempt_id, "status": "SKIPPED", "result_sha256": sha256_file(result_path)}
    else:
        RUNTIME.fail_unit(state, unit_id=unit_id, code="UNIT_RESULT_BLOCKED", uncertain=False)
        receipt = {"unit_id": unit_id, "attempt_id": attempt_id, "status": "FAILED_TERMINAL", "result_sha256": sha256_file(result_path)}
    dispatch["state"] = receipt["status"]
    control.setdefault("unit_receipts", []).append(receipt)
    control["staging_common_metadata"] = GITISO.common_metadata_identity(files["staging"])
    write_json(files["runtime_state"], state)
    write_json(files["control"], control)
    dispatched = dispatch_ready(parent_run_dir, manifest) if dispatch_next else []
    return {"status": receipt["status"], "receipt": receipt, "next_dispatches": dispatched}


def finalize(parent_run_dir: Path) -> dict[str, Any]:
    control, files, state, graph = load_control(parent_run_dir)
    manifest = read_json(Path(control["manifest_path"]))
    if not RUNTIME.apply_ready(state):
        raise DriverError("APPLY_READY_BLOCKED", "required units are unresolved")
    RUNTIME.mark_apply_ready(state)
    write_json(files["runtime_state"], state)
    component_heads = {component_id: str(component["integration_head_oid"]) for component_id, component in state["components"].items()}
    integration = GITISO.integrate_component_heads(
        files["staging"], files["aggregate"], str(graph["baseline_oid"]), component_heads
    )
    parallel = manifest["parallel_implementation"]
    full_test_ids = list(parallel["full_test_ids"])
    full_tests = run_registered_tests(files["aggregate"], test_registry(manifest), full_test_ids, files["runtime_root"] / "full-tests")
    baseline = read_json(files["baseline"])
    current = GITISO.canonical_snapshot(control["canonical_project_root"])
    GITISO.assert_snapshot_equal(baseline, current)
    target = integration["integration_head_oid"]
    ref_name = f"refs/codexpro/parallel/{control['parent_run_id']}"
    imported = GITISO.import_integration_ref(control["canonical_project_root"], files["staging"], target, ref_name)
    current_after_import = GITISO.canonical_snapshot(control["canonical_project_root"])
    GITISO.assert_snapshot_equal(baseline, current_after_import)
    applied = GITISO.ff_only_apply(control["canonical_project_root"], baseline, target)
    result = {
        "schema": "codex.chatgpt.parallel-implementation-result/v1",
        "status": "IMPLEMENTED",
        "summary": "All required units, deterministic integration, full tests, identity revalidation, and ff-only apply passed.",
        "baseline_oid": baseline["head_oid"],
        "integration_head_oid": target,
        "unit_dispositions": [
            {"unit_id": unit_id, "state": unit["state"], "commit_oid": unit.get("integrated_commit_oid")}
            for unit_id, unit in sorted(state["units"].items())
        ],
        "full_test_results": full_tests,
        "canonical_apply": {"performed": True, "ff_only": True, "receipt_sha256": applied["ff_only_apply_receipt_sha256"]},
        "remaining_risks": [],
        "blocked_conditions": [],
        "integration_receipt_sha256": integration["deterministic_integration_sha256"],
        "import_receipt_sha256": imported["integration_ref_import_sha256"],
    }
    result_path = files["runtime_root"] / "parallel-implementation-result.json"
    write_json(result_path, result)
    store = STATE.RunStore(Path(control["state_root"]))
    store.finalize_parent(parent_run_dir, "PARENT_COMPLETE", result={"path": str(result_path), "sha256": sha256_file(result_path)})
    return result


def status(parent_run_dir: Path) -> dict[str, Any]:
    control, files, state, graph = load_control(parent_run_dir)
    return {
        "status": state.get("phase"),
        "parent_run_id": control.get("parent_run_id"),
        "dispatchable": RUNTIME.dispatchable_units(state),
        "apply_ready": RUNTIME.apply_ready(state),
        "units": state.get("units"),
        "components": state.get("components"),
        "queue": state.get("queue"),
        "capacity_receipt_sha256": (state.get("scheduler") or {}).get("capacity_receipt_sha256"),
    }


def resume(parent_run_dir: Path, capacity_receipt_path: Path) -> dict[str, Any]:
    """Record a fresh objective receipt and drain only its available slots."""
    control, _, _, _ = load_control(parent_run_dir)
    receipt = read_json(capacity_receipt_path)
    manifest = read_json(Path(control["manifest_path"]))
    dispatched = dispatch_ready(parent_run_dir, manifest, receipt)
    return {"status": "RESUMED", "parent_run_id": control["parent_run_id"], "dispatches": dispatched}


def _default_child_executor(dispatch: Mapping[str, Any], control: Mapping[str, Any]) -> Mapping[str, Any]:
    """Run one exact child manifest without creating a visible Windows console."""
    command = [
        sys.executable, str(THINKING_RUNNER), "--config", str(dispatch["child_manifest_path"]),
        "--state-root", str(control["state_root"]),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, encoding="utf-8",
        timeout=3600, shell=False, creationflags=creationflags, startupinfo=startupinfo,
    )
    if completed.returncode != 0:
        raise DriverError("CHILD_EXECUTION_FAILED", "exact child runner failed", {"unit_id": dispatch["unit_id"], "stderr": completed.stderr[-4000:]})
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DriverError("CHILD_EXECUTION_OUTPUT_INVALID", "exact child runner did not return JSON", {"unit_id": dispatch["unit_id"]}) from exc
    if not isinstance(value, Mapping) or value.get("ok") is not True:
        raise DriverError("CHILD_EXECUTION_UNRESOLVED", "exact child runner did not complete", {"unit_id": dispatch["unit_id"]})
    return value


def run_dispatch_wave(
    dispatches: list[Mapping[str, Any]], control: Mapping[str, Any],
    *, executor: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] = _default_child_executor,
    on_completion: Callable[[Mapping[str, Any], Mapping[str, Any] | None, Exception | None], None] | None = None,
    raise_on_error: bool = True,
) -> list[Mapping[str, Any]]:
    """Launch a bounded wave and durably observe each child as it completes.

    A separate executor is injected in tests; production uses the exact thinking
    runner with one process/session per already-isolated unit workspace.  The
    completion callback runs once per completed future, before waiting for any
    slower sibling.  ``execute`` uses it to durably record each exact child
    result, so a later sibling failure cannot cause an already completed child
    to be submitted again.
    """
    if not dispatches:
        return []
    roots = [str(item.get("unit_workspace_root") or "") for item in dispatches]
    if any(not root for root in roots) or len(roots) != len(set(roots)):
        raise DriverError("CHILD_WAVE_WORKSPACE_NOT_DISTINCT", "each concurrently launched child requires one distinct exact unit workspace")
    results: list[Mapping[str, Any] | None] = [None] * len(dispatches)
    failures: list[tuple[Mapping[str, Any], Exception]] = []
    with ThreadPoolExecutor(max_workers=len(dispatches), thread_name_prefix="parallel-implementation") as pool:
        future_dispatches = {
            pool.submit(executor, dispatch, control): (index, dispatch)
            for index, dispatch in enumerate(dispatches)
        }
        for future in as_completed(future_dispatches):
            index, dispatch = future_dispatches[future]
            try:
                execution = future.result()
            except Exception as exc:
                failures.append((dispatch, exc))
                if on_completion is not None:
                    on_completion(dispatch, None, exc)
                continue
            results[index] = execution
            if on_completion is not None:
                on_completion(dispatch, execution, None)
    if failures and raise_on_error:
        dispatch, exc = failures[0]
        raise DriverError(
            "CHILD_WAVE_EXECUTION_FAILED",
            "one or more exact child runners failed after completed siblings were observed",
            {"unit_id": dispatch.get("unit_id"), "failure_count": len(failures)},
        ) from exc
    return [item for item in results if item is not None]


def _record_child_recovery(
    parent_run_dir: Path,
    dispatch_identity: Mapping[str, Any],
    error: Exception,
) -> dict[str, Any]:
    """Durably hold only the failed exact child for recovery.

    A worker exception is post-send ambiguous by default: the child may have
    submitted work before its local process failed.  Do not redispatch it here.
    Its siblings are independently persisted by the completion callback.
    """
    control, files, state, _ = load_control(parent_run_dir)
    unit_id = str(dispatch_identity.get("unit_id") or "")
    attempt_id = str(dispatch_identity.get("attempt_id") or "")
    dispatch = next(
        (
            item for item in control.get("dispatches") or []
            if item.get("unit_id") == unit_id and item.get("attempt_id") == attempt_id
        ),
        None,
    )
    unit = state.get("units", {}).get(unit_id)
    if not isinstance(dispatch, dict) or not isinstance(unit, dict):
        raise DriverError("CHILD_RECOVERY_IDENTITY_MISSING", "completed child callback has no exact active dispatch identity")
    if unit.get("state") not in {"ACTIVE", "RECOVERY_REQUIRED"}:
        return {"status": "ALREADY_SETTLED", "unit_id": unit_id, "attempt_id": attempt_id}
    error_code = str(getattr(error, "code", type(error).__name__))
    RUNTIME.fail_unit(state, unit_id=unit_id, code="CHILD_EXECUTION_UNCERTAIN", uncertain=True)
    dispatch["state"] = "RECOVERY_REQUIRED"
    receipt = {
        "unit_id": unit_id,
        "attempt_id": attempt_id,
        "status": "RECOVERY_REQUIRED",
        "error_code": error_code,
        "error_message": str(error),
    }
    control.setdefault("unit_receipts", []).append(receipt)
    control["staging_common_metadata"] = GITISO.common_metadata_identity(files["staging"])
    write_json(files["runtime_state"], state)
    write_json(files["control"], control)
    return {"status": "RECOVERY_REQUIRED", "receipt": receipt}


def _find_answer_path(value: Any) -> Path | None:
    if isinstance(value, Mapping):
        if isinstance(value.get("answer"), Mapping) and isinstance(value["answer"].get("path"), str):
            return Path(value["answer"]["path"])
        if isinstance(value.get("answer_path"), str):
            return Path(value["answer_path"])
        for child in value.values():
            found = _find_answer_path(child)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_answer_path(child)
            if found is not None:
                return found
    return None


def _unit_result_from_execution(value: Mapping[str, Any]) -> dict[str, Any]:
    direct = value.get("unit_result")
    if isinstance(direct, Mapping):
        return dict(direct)
    answer_path = _find_answer_path(value)
    if answer_path is None or not answer_path.is_file():
        raise DriverError("CHILD_RESULT_MISSING", "completed child has no captured exact answer/result")
    text = answer_path.read_text(encoding="utf-8-sig").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DriverError("CHILD_RESULT_JSON_INVALID", "child answer is not implementation-unit-result JSON") from exc
    if not isinstance(result, dict):
        raise DriverError("CHILD_RESULT_JSON_INVALID", "child answer result must be an object")
    return result


def execute(
    parent_run_dir: Path, *, capacity_receipt_provider: Callable[[Mapping[str, Any], Mapping[str, Any], int], Mapping[str, Any] | None] | None = None,
    executor: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] = _default_child_executor,
    finalize_when_ready: bool = True,
) -> dict[str, Any]:
    """Supervise bounded concurrent child waves through deterministic finalization.

    Each wave refreshes an externally supplied objective capacity receipt.  When
    no observer is supplied the runtime's one-slot receipt is used, so a missing
    observation can never create speculative concurrent web sends.
    """
    wave = 0
    receipts: list[str] = []
    unit_outcomes: list[dict[str, Any]] = []
    while True:
        control, _, state, _ = load_control(parent_run_dir)
        manifest = read_json(Path(control["manifest_path"]))
        # `prepare` has already materialized the first bounded wave.  Execute
        # those exact owned child manifests before considering any new slot;
        # otherwise an ACTIVE unit would be mistaken for an external runner.
        dispatches = [
            dict(item)
            for item in control.get("dispatches") or []
            if item.get("state") == "AWAITING_PROVIDER_RESULT"
        ]
        if not dispatches:
            supplied = capacity_receipt_provider(control, state, wave) if capacity_receipt_provider else None
            dispatches = dispatch_ready(parent_run_dir, manifest, supplied)
        control, files, state, _ = load_control(parent_run_dir)
        if not dispatches:
            if any(unit.get("state") == "ACTIVE" for unit in state["units"].values()):
                raise DriverError("CHILD_WAVE_ACTIVE_EXTERNALLY", "cannot supervise a wave while an exact child is already active")
            break
        receipts.append(str(control.get("capacity_receipt", {}).get("capacity_receipt_sha256") or ""))
        def persist_completion(
            dispatch: Mapping[str, Any],
            execution: Mapping[str, Any] | None,
            error: Exception | None,
        ) -> None:
            if error is not None:
                unit_outcomes.append(_record_child_recovery(parent_run_dir, dispatch, error))
                return
            assert execution is not None
            try:
                result = _unit_result_from_execution(execution)
                result_path = files["results"] / str(dispatch["unit_id"]) / str(dispatch["attempt_id"]) / "provider-result.json"
                write_json(result_path, result)
                unit_outcomes.append(record_unit(parent_run_dir, result_path, dispatch_next=False))
            except Exception as exc:
                # A response which cannot be safely captured/validated is also
                # post-send ambiguous.  Keep that exact child for recovery;
                # do not discard already durable sibling outcomes.
                unit_outcomes.append(_record_child_recovery(parent_run_dir, dispatch, exc))

        run_dispatch_wave(
            dispatches,
            control,
            executor=executor,
            on_completion=persist_completion,
            raise_on_error=False,
        )
        wave += 1
    _, _, state, _ = load_control(parent_run_dir)
    final = finalize(parent_run_dir) if finalize_when_ready and RUNTIME.apply_ready(state) else None
    return {"status": "FINALIZED" if final else "DRAINED", "waves": wave, "capacity_receipt_sha256s": receipts, "unit_outcomes": unit_outcomes, "final": final}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the feature-gated parallel implementation v3 host workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--manifest", required=True)
    prepare_parser.add_argument("--graph", required=True)
    prepare_parser.add_argument("--capacity-receipt")
    record_parser = sub.add_parser("record-unit")
    record_parser.add_argument("--parent-run-dir", required=True)
    record_parser.add_argument("--result", required=True)
    finalize_parser = sub.add_parser("finalize")
    finalize_parser.add_argument("--parent-run-dir", required=True)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--parent-run-dir", required=True)
    resume_parser = sub.add_parser("resume")
    resume_parser.add_argument("--parent-run-dir", required=True)
    resume_parser.add_argument("--capacity-receipt", required=True)
    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("--parent-run-dir", required=True)
    execute_parser.add_argument("--capacity-receipt")
    args = parser.parse_args()
    if args.command in {"prepare", "resume", "execute"}:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "errorCode": "LEGACY_NEW_SUBMISSION_FROZEN",
                        "message": (
                            "new or resumed provider submissions through the legacy parallel implementation "
                            "runtime are frozen; use Oracle Web Multi"
                        ),
                        "evidence": {},
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    try:
        if args.command == "prepare":
            initial_receipt = read_json(Path(args.capacity_receipt).resolve(strict=True)) if args.capacity_receipt else None
            value = prepare(
                Path(args.manifest).resolve(strict=True),
                Path(args.graph).resolve(strict=True),
                initial_capacity_receipt=initial_receipt,
            )
        elif args.command == "record-unit":
            value = record_unit(Path(args.parent_run_dir).resolve(strict=True), Path(args.result).resolve(strict=True))
        elif args.command == "finalize":
            value = finalize(Path(args.parent_run_dir).resolve(strict=True))
        elif args.command == "resume":
            value = resume(Path(args.parent_run_dir).resolve(strict=True), Path(args.capacity_receipt).resolve(strict=True))
        elif args.command == "execute":
            receipt_path = Path(args.capacity_receipt).resolve(strict=True) if args.capacity_receipt else None
            provider = (lambda _control, _state, _wave: read_json(receipt_path)) if receipt_path else None
            value = execute(Path(args.parent_run_dir).resolve(strict=True), capacity_receipt_provider=provider)
        else:
            value = status(Path(args.parent_run_dir).resolve(strict=True))
        print(json.dumps({"ok": True, "result": value}, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        evidence = getattr(exc, "evidence", {})
        print(json.dumps({"ok": False, "error": {"errorCode": code, "message": str(exc), "evidence": evidence}}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
