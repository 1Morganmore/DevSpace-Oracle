from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from run_pro_plan_handoff import ProPlanHandoffDriver, atomic_write_json, file_sha256, load_mapping, write_immutable_text

GOAL_CYCLE_SCHEMA = "codex.chatgpt.goal-cycle/v1"
WORKFLOW_SCHEMA = "codex.chatgpt.comprehensive-workflow/v4"


class GoalCycleAdapterError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        super().__init__(code if detail is None else f"{code}: {detail}")


def build_workflow_manifest(
    *,
    cycle_manifest: Mapping[str, Any],
    goal_manifest: Mapping[str, Any],
    cycle_dir: Path,
) -> Path:
    if cycle_manifest.get("schema") != GOAL_CYCLE_SCHEMA:
        raise GoalCycleAdapterError("GOAL_CYCLE_SCHEMA_INVALID")
    mission_ref = cycle_manifest.get("mission")
    original_ref = cycle_manifest.get("original_goal")
    if not isinstance(mission_ref, Mapping) or not isinstance(original_ref, Mapping):
        raise GoalCycleAdapterError("GOAL_CYCLE_TEXT_REFS_MISSING")
    mission_path = Path(str(mission_ref.get("path") or ""))
    original_path = Path(str(original_ref.get("path") or ""))
    if (
        not mission_path.is_file()
        or mission_path.is_symlink()
        or file_sha256(mission_path) != mission_ref.get("sha256")
        or not original_path.is_file()
        or original_path.is_symlink()
        or file_sha256(original_path) != original_ref.get("sha256")
    ):
        raise GoalCycleAdapterError("GOAL_CYCLE_TEXT_IDENTITY_INVALID")
    original = original_path.read_bytes().decode("utf-8", errors="strict")
    mission = mission_path.read_bytes().decode("utf-8", errors="strict")
    question = (
        "[ORIGINAL GOAL - EXACT UTF-8]\n"
        + original
        + "\n\n[CURRENT WEB-AUTHORED MISSION - EXACT UTF-8]\n"
        + mission
        + "\n\nTreat the original goal as the acceptance target and the current mission as this cycle's scope."
    )
    project = dict(goal_manifest["project"])
    context = dict(goal_manifest["context"])
    workflow = {
        "schema": WORKFLOW_SCHEMA,
        "workflow_mode": "gpt-comprehensive",
        "workflow_id": f"{goal_manifest['goal_id']}-cycle-{int(cycle_manifest['cycle_index']):04d}",
        "question": question,
        "workspace": {
            "root": project["root"],
            "handoff_root": str(cycle_dir / "inner-v4"),
            "chatgpt_app_name": project["chatgpt_app_name"],
            "allowed_write_paths": list(project["allowed_write_paths"]),
            "forbidden_paths": list(project.get("forbidden_paths") or []),
        },
        "context": {
            "candidate_paths": list(context["candidate_paths"]),
            "policy_paths": list(context.get("policy_paths") or []),
        },
        "gates": dict(goal_manifest["gates"]),
        "relay": {"mode": "web-native-v1"},
        "pro_plan": {"max_attempts": 2},
        "goal_supervisor": {
            "schema": "codex.chatgpt.goal-cycle-binding/v1",
            "goal_id": goal_manifest["goal_id"],
            "cycle_index": int(cycle_manifest["cycle_index"]),
            "original_goal_sha256": original_ref["sha256"],
            "mission_sha256": mission_ref["sha256"],
            "cycle_nonce": cycle_manifest["cycle_nonce"],
            "criteria": list(goal_manifest["acceptance"]["criteria"]),
            "allowed_host_check_ids": list(cycle_manifest["allowed_host_check_ids"]),
        },
        "output_dir": str(cycle_dir / "inner-v4"),
    }
    if goal_manifest.get("agbrowse_contract"):
        workflow["agbrowse_contract"] = goal_manifest["agbrowse_contract"]
    path = cycle_dir / "workflow.json"
    write_immutable_text(path, json.dumps(workflow, ensure_ascii=False, indent=2, sort_keys=True))
    return path


def run_cycle(cycle_manifest_path: Path, goal_manifest_path: Path) -> dict[str, Any]:
    cycle_manifest_path = cycle_manifest_path.resolve(strict=True)
    goal_manifest_path = goal_manifest_path.resolve(strict=True)
    cycle_manifest = load_mapping(cycle_manifest_path)
    goal_manifest = load_mapping(goal_manifest_path)
    cycle_dir = cycle_manifest_path.parent
    workflow_path = build_workflow_manifest(
        cycle_manifest=cycle_manifest,
        goal_manifest=goal_manifest,
        cycle_dir=cycle_dir,
    )
    final = ProPlanHandoffDriver(workflow_path).run()
    result = final.get("goal_cycle_result")
    if not isinstance(result, Mapping):
        raise GoalCycleAdapterError("GOAL_CYCLE_RESULT_MISSING")
    result_path = cycle_dir / "cycle-result.json"
    write_immutable_text(result_path, json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    receipt = {
        "schema": "codex.chatgpt.goal-cycle-adapter-receipt/v1",
        "goal_id": cycle_manifest["goal_id"],
        "cycle_index": cycle_manifest["cycle_index"],
        "workflow": {"path": str(workflow_path), "sha256": file_sha256(workflow_path)},
        "inner_final": {
            "path": str(Path(final.get("output_dir") or workflow_path.parent) / "final.json")
            if final.get("output_dir") else str(Path(workflow["output_dir"]) / workflow["workflow_id"] / "final.json"),
        },
        "cycle_result": {"path": str(result_path), "sha256": file_sha256(result_path)},
    }
    receipt_path = cycle_dir / "cycle-adapter-receipt.json"
    write_immutable_text(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return {"result": dict(result), "receipt": receipt, "receipt_path": str(receipt_path)}
