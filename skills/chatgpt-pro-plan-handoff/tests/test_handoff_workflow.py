from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from jsonschema import ValidationError
from jsonschema import validate as validate_json_schema

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from handoff_contract import ORCHESTRATOR_RESULT_SCHEMA, PLAN_RESULT_SCHEMA, REVIEW_RESULT_SCHEMA
from run_pro_plan_handoff import (
    AgbrowseRuntime,
    DEFAULT_CONTRACT_PATH,
    ProPlanHandoffDriver,
    STATE_SCHEMA,
    WorkflowError,
    exclusive_workflow_lock,
    sha256_text,
    validate_manifest,
)
from workspace_guard import build_source_archive, build_workspace_snapshot, file_sha256, write_snapshot


def make_manifest(tmp_path: Path, mode="pro-plan-to-gpt-orchestrator") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src.py").write_text("print('ok')\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("# policy\n", encoding="utf-8")
    output = tmp_path / "runs"
    value = {
        "schema": "codex.chatgpt.pro-plan-handoff/v1",
        "workflow_mode": mode,
        "workflow_id": "wf-test",
        "question": "Implement safely.",
        "workspace": {
            "root": str(workspace),
            "handoff_root": str(output),
            "chatgpt_app_name": "CodexPro-Test",
            "allowed_write_paths": [str(workspace)],
            "forbidden_paths": [str(workspace / ".git")],
        },
        "context": {"candidate_paths": ["src.py"], "policy_paths": ["AGENTS.md"]},
        "pro_plan": {"max_attempts": 2},
        "output_dir": str(output),
    }
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def make_v2_manifest(
    tmp_path: Path,
    *,
    research_triggers=None,
    advisory_triggers=None,
    research_policy="auto",
    advisory_policy="auto",
    affected_components=0,
    cross_component_interfaces=0,
    contradiction_evidence=None,
) -> Path:
    path = make_manifest(tmp_path, "gpt-comprehensive")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["schema"] = "codex.chatgpt.comprehensive-workflow/v2"
    value["gates"] = {
        "research": {
            "policy": research_policy,
            "triggers": list(research_triggers or []),
        },
        "advisory": {
            "policy": advisory_policy,
            "triggers": list(advisory_triggers or []),
            "affected_components": affected_components,
            "cross_component_interfaces": cross_component_interfaces,
            "contradiction_evidence": list(contradiction_evidence or []),
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def seed_legacy_comprehensive_recovery(manifest_path: Path) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    workspace_root = Path(manifest["workspace"]["root"])
    workflow_dir = Path(manifest["output_dir"]) / manifest["workflow_id"]
    handoff_dir = workflow_dir / "handoff"
    snapshot = build_workspace_snapshot(
        workspace_root=workspace_root,
        selected_paths=manifest["context"]["candidate_paths"],
        policy_paths=manifest["context"]["policy_paths"],
        question_sha256=sha256_text(manifest["question"]),
    )
    snapshot_path = handoff_dir / "source-snapshot.json"
    write_snapshot(snapshot_path, snapshot)
    archive = build_source_archive(
        workspace_root=workspace_root,
        snapshot=snapshot,
        output_zip=handoff_dir / "source-context.zip",
    )
    state_path = workflow_dir / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema": STATE_SCHEMA,
                "workflow_id": manifest["workflow_id"],
                "status": "PREPARED",
                "question_sha256": sha256_text(manifest["question"]),
                "source_snapshot_path": str(snapshot_path),
                "source_snapshot_sha256": snapshot["snapshot_sha256"],
                "source_archive": archive,
                "stages": {
                    "gpt-plan-attempt-1": {
                        "schema": "codex.chatgpt.stage-checkpoint/v1",
                        "stage": "gpt-plan",
                        "attempt_index": 1,
                        "nonce": "0123456789abcdef0123456789abcdef",
                        "dependency_identity": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return state_path


def add_selected_contract(manifest_path: Path, contract_path: Path) -> None:
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["agbrowse_contract"] = str(contract_path)
    manifest_path.write_text(json.dumps(value), encoding="utf-8")


def add_parallel_execution(manifest_path: Path, *, with_capacity: bool = True) -> tuple[Path, Path, Path | None]:
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = Path(value["workspace"]["root"])
    v3 = manifest_path.parent / "parallel-v3.json"
    graph = manifest_path.parent / "implementation-graph.json"
    receipt = manifest_path.parent / "capacity-receipt.json"
    v3.write_text(
        json.dumps(
            {
                "schema": "codex.chatgpt.comprehensive-workflow/v3",
                "workflow_id": "parallel-test",
                "project_root": str(root),
                "question": value["question"],
                "output_dir": str(manifest_path.parent / "parallel-output"),
                "chatgpt_app_name": value["workspace"]["chatgpt_app_name"],
                "features": {"parallel_implementation_v1": True},
                "parallel_implementation": {
                    "enabled": True,
                    "max_units": 2,
                    "test_registry": {"unit": {"argv": ["python", "-V"], "timeout_seconds": 10}},
                    "full_test_ids": ["unit"],
                },
            }
        ),
        encoding="utf-8",
    )
    graph.write_text(
        json.dumps(
            {
                "schema": "codex.chatgpt.implementation-graph-result/v1",
                "units": [{
                    "unit_id": "unit-a", "required": True, "mission": "implement the approved change",
                    "claimed_paths": ["src.py"], "depends_on": [], "test_ids": ["unit"],
                }],
            }
        ),
        encoding="utf-8",
    )
    if with_capacity:
        receipt.write_text(
            json.dumps(
                {
                    "available_child_sessions": 2,
                    "observed_at": "2026-07-22T00:00:00Z",
                    "source": "test-capacity-observer",
                }
            ),
            encoding="utf-8",
        )
    value["parallel_execution"] = {
        "enabled": True,
        "workflow_v3_manifest": str(v3),
        "implementation_graph": str(graph),
        **({"capacity_receipt": str(receipt)} if with_capacity else {}),
    }
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    return v3, graph, receipt if with_capacity else None


def test_published_schemas_accept_gpt_comprehensive_plan_contract(tmp_path: Path) -> None:
    manifest_path = make_manifest(tmp_path, "gpt-comprehensive")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schemas = Path(__file__).resolve().parents[1] / "schemas"
    workflow_schema = json.loads((schemas / "workflow-v1.schema.json").read_text(encoding="utf-8"))
    correlation_schema = json.loads((schemas / "workflow-correlation-v1.schema.json").read_text(encoding="utf-8"))
    plan_schema = json.loads((schemas / "plan-result-v1.schema.json").read_text(encoding="utf-8"))
    digest = "a" * 64
    correlation = {
        "schema": "codex.chatgpt.workflow-correlation/v1",
        "workflow_id": manifest["workflow_id"],
        "stage": "gpt-plan",
        "attempt_index": 1,
        "nonce": "0123456789abcdef",
        "question_sha256": digest,
        "source_snapshot_sha256": digest,
    }
    plan = {
        "schema": "codex.chatgpt.plan-result/v1",
        **{key: value for key, value in correlation.items() if key != "schema"},
        "status": "complete",
        "sections": ["implementation", "tests"],
    }

    validate_json_schema(manifest, workflow_schema)
    validate_json_schema(correlation, correlation_schema)
    validate_json_schema(plan, plan_schema)
    with pytest.raises(WorkflowError, match="COMPREHENSIVE_V2_REQUIRED"):
        validate_manifest(manifest)


def test_published_workflow_schema_requires_app_only_for_comprehensive(tmp_path: Path) -> None:
    schemas = Path(__file__).resolve().parents[1] / "schemas"
    workflow_schema = json.loads((schemas / "workflow-v1.schema.json").read_text(encoding="utf-8"))
    pro_manifest = json.loads(make_manifest(tmp_path, "pro-plan-to-gpt-orchestrator").read_text(encoding="utf-8"))
    pro_manifest["workspace"].pop("chatgpt_app_name")

    validate_json_schema(pro_manifest, workflow_schema)

    pro_manifest["workflow_mode"] = "gpt-comprehensive"
    with pytest.raises(ValidationError):
        validate_json_schema(pro_manifest, workflow_schema)


class ScriptedRuntime:
    def __init__(self, verdicts=None, reuse_url=False, events=None):
        self.runs = {}
        self.count = 0
        self.verdicts = list(verdicts or ["PASS"])
        self.reuse_url = reuse_url
        self.events = events
        self.manifests = []

    def start(self, manifest_path: Path):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.manifests.append(manifest)
        corr = dict(manifest["workflow_correlation"])
        stage = corr["stage"]
        if self.events is not None:
            self.events.append(stage)
        self.count += 1
        run_id = f"run-{self.count}"
        run_dir = manifest_path.parent / run_id
        run_dir.mkdir()
        common = {
            "workflow_id": corr["workflow_id"],
            "stage": stage,
            "attempt_index": corr["attempt_index"],
            "nonce": corr["nonce"],
            "question_sha256": corr["question_sha256"],
            "source_snapshot_sha256": corr["source_snapshot_sha256"],
        }
        v2 = corr.get("schema") == "codex.chatgpt.workflow-correlation/v2"

        def prompt_hash_value(key: str) -> str:
            text = Path(manifest["prompt_file"]).read_text(encoding="utf-8")
            match = re.search(rf'"{re.escape(key)}"\s*:\s*"([0-9a-f]{{64}})"', text)
            assert match, (key, text)
            return match.group(1)

        if stage == "deep-research":
            envelope = {
                "schema": "codex.chatgpt.research-result/v2",
                **common,
                "status": "complete",
                "findings": [{"claim": "current fact"}],
                "sources": [{"url": "https://example.test/source"}],
            }
        elif stage in {"pro-plan", "gpt-plan"}:
            envelope = {
                "schema": "codex.chatgpt.plan-result/v2" if v2 else PLAN_RESULT_SCHEMA,
                **common,
                "status": "complete",
                "sections": [
                    "blockers", "evidence", "alternatives", "files",
                    "implementation", "tests", "rollback", "conclusion_change_triggers",
                ],
            }
            if v2:
                envelope["input_research_descriptor_sha256"] = prompt_hash_value(
                    "input_research_descriptor_sha256"
                )
        elif stage == "gpt-review":
            plan_path = Path(manifest["read_only_paths"][0])
            envelope = {
                "schema": "codex.chatgpt.review-result/v2" if v2 else REVIEW_RESULT_SCHEMA,
                **common,
                "input_plan_sha256": file_sha256(plan_path),
                "verdict": self.verdicts.pop(0),
            }
            if v2:
                research_descriptor = next(
                    Path(item) for item in manifest["read_only_paths"] if Path(item).name == "research.gate.json"
                )
                advisory_descriptor = next(
                    Path(item) for item in manifest["read_only_paths"] if Path(item).name.startswith("advisory-") and Path(item).name.endswith(".gate.json")
                )
                envelope["input_research_descriptor_sha256"] = file_sha256(research_descriptor)
                envelope["input_advisory_descriptor_sha256"] = file_sha256(advisory_descriptor)
            else:
                advisory_paths = [Path(item) for item in manifest["read_only_paths"][1:] if Path(item).name.startswith("advisory-")]
                if advisory_paths:
                    envelope["input_advisory_sha256"] = file_sha256(advisory_paths[0])
        elif stage == "pro-advisory":
            envelope = {"schema": "codex.chatgpt.pro-advisory-test/v1", **common, "status": "complete"}
        else:
            mission_path = next(Path(p) for p in manifest["read_only_paths"] if Path(p).name == "execution-mission.json")
            mission = json.loads(mission_path.read_text(encoding="utf-8"))
            envelope = {
                "schema": "codex.chatgpt.orchestrator-result/v2" if v2 else ORCHESTRATOR_RESULT_SCHEMA,
                **common,
                "input_plan_sha256": mission["selected_plan"]["sha256"],
                "input_review_sha256": mission["review_transition"]["sha256"],
                "status": "complete",
                "changed_files": [],
                "commands": [],
                "blockers": [],
            }
            if v2:
                envelope["input_research_descriptor_sha256"] = mission["research_descriptor"]["sha256"]
                envelope["input_advisory_descriptor_sha256"] = mission["advisory_descriptor"]["sha256"]
        (run_dir / "transcript.raw.md").write_text(
            "result\n```json\n" + json.dumps(envelope) + "\n```",
            encoding="utf-8",
        )
        url_id = "same" if self.reuse_url else run_id
        result = {
            "run_id": run_id,
            "status": "completed",
            "completion_state": "DONE",
            "completed_final_output": True,
            "final_text_captured": True,
            "effective_mode_label": manifest["mode_label"],
            "effective_mode_variant": manifest.get("mode_variant"),
            "regular_mode_selection": manifest.get("regular_mode_selection"),
            "fallback_reason": None,
            "conversation_url": f"https://chatgpt.com/c/{url_id}",
            "workflow_correlation": corr,
            "gpt_question_policy": {
                "gpt_operation_mode": manifest["gpt_operation_mode"],
                "prompt_profile": manifest["prompt_profile"],
                "prompt_profile_receipt": manifest["prompt_profile_receipt"],
                "unknown_mode_fallback": False,
            },
        }
        (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
        self.runs[run_id] = run_dir
        return {"run_id": run_id, "status": "completed", "run_dir": str(run_dir)}

    def status(self, run_id):
        return {"run_id": run_id, "status": "completed", "run_dir": str(self.runs[run_id])}

    def wait(self, run_id):
        return self.status(run_id)

    def resume(self, run_id):
        return self.status(run_id)

    def recover_run_ids(self, manifest_path):
        return []


class CheckpointRecoveringRuntime(ScriptedRuntime):
    """Models a process restart: the same immutable manifest finds its run."""

    def __init__(self):
        super().__init__(["PASS"])
        self.by_manifest = {}

    def start(self, manifest_path: Path):
        result = super().start(manifest_path)
        self.by_manifest[str(manifest_path.resolve())] = result["run_id"]
        return result

    def recover_run_ids(self, manifest_path: Path):
        run_id = self.by_manifest.get(str(manifest_path.resolve()))
        return [run_id] if run_id else []


class UncertainThenRecoveringRuntime(CheckpointRecoveringRuntime):
    """First driver cannot adjudicate; the next driver recovers the same run."""

    def __init__(self):
        super().__init__()
        self.uncertain_run_id = None
        self.resume_calls = 0

    def start(self, manifest_path: Path):
        result = super().start(manifest_path)
        if self.uncertain_run_id is None:
            self.uncertain_run_id = result["run_id"]
        return result

    def _uncertain(self, run_id):
        return {
            "run_id": run_id,
            "status": "blocked",
            "phase": "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
            "run_dir": str(self.runs[run_id]),
        }

    def status(self, run_id):
        if run_id == self.uncertain_run_id and self.resume_calls < 2:
            return self._uncertain(run_id)
        return super().status(run_id)

    def wait(self, run_id):
        return self.status(run_id)

    def resume(self, run_id):
        if run_id == self.uncertain_run_id:
            self.resume_calls += 1
        return self.status(run_id)


class FakeWebMultiRuntime:
    def __init__(self, events=None):
        self.events = events
        self.manifests = []

    def run(self, manifest_path: Path):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.manifests.append(manifest)
        if self.events is not None:
            self.events.append("web-multi")
        result = {
            "schema": "codex.chatgpt.web-multi-result/v1",
            "workflow_id": manifest["workflow_id"],
            "organizer_result": "advisory",
            "provenance": [{"stage_id": "planner"}],
            "evidence_map_sha256": "e" * 64,
            "max_concurrent_child_generations": 2,
            "advisory_only": True,
        }
        if manifest.get("schema") == "codex.chatgpt.web-multi/v2":
            result.update(
                {
                    "manifest_schema": "codex.chatgpt.web-multi/v2",
                    "semantics_version": "upstream-parity-v1",
                    "planner_policy": "upstream-nonempty-prefix10",
                    "mode_variant": "Very High",
                    "role_session_target_url_provenance": [
                        {
                            "stage_id": "planner",
                            "role": "Planner",
                            "session_id": "session-planner",
                            "target_id": "target-planner",
                            "conversation_url": "https://chatgpt.com/c/planner",
                        }
                    ],
                }
            )
        return result


class FakeParallelImplementationRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path, Path]] = []

    def execute(self, workflow_manifest_path: Path, graph_path: Path, capacity_receipt_path: Path):
        self.calls.append((workflow_manifest_path, graph_path, capacity_receipt_path))
        return {
            "prepared": {"status": "PREPARED", "parent_run_dir": "C:/parallel-parent"},
            "execution": {"status": "FINALIZED", "final": {"status": "IMPLEMENTED"}},
        }


def test_manifest_rejects_backend_and_runtime_override(tmp_path):
    path = make_manifest(tmp_path)
    value = json.loads(path.read_text())
    value["browser_backend"] = "chrome-cdp"
    with pytest.raises(WorkflowError, match="MANIFEST_CANNOT_OVERRIDE_RUNTIME_OR_BACKEND"):
        validate_manifest(value)


@pytest.mark.parametrize("phase", ["USER_STOP_REQUESTED", "ABANDONED_UNCERTAIN"])
def test_user_stopped_runtime_phase_is_blocked_not_running_or_completed(tmp_path, phase):
    runtime = object.__new__(AgbrowseRuntime)
    state_file = tmp_path / "run" / "run.json"
    state_file.parent.mkdir()

    result = runtime._compat(
        state_file,
        {
            "run_id": "stopped-run",
            "phase": phase,
            "conversation_url": "https://chatgpt.com/c/stopped",
            "terminal_block_code": "USER_STOP_CONFIRMATION_PENDING" if phase == "USER_STOP_REQUESTED" else None,
        },
    )

    assert result["status"] == "blocked"
    assert result["error"] == (
        "USER_STOP_CONFIRMATION_PENDING" if phase == "USER_STOP_REQUESTED" else "ABANDONED_UNCERTAIN"
    )


def test_pro_plan_is_attachment_only_and_regular_stages_use_app(tmp_path):
    driver = ProPlanHandoffDriver(make_manifest(tmp_path), runtime=ScriptedRuntime())
    state = driver.prepare()
    plan_binding = driver._binding(state, "pro-plan", 1)
    plan = driver._manifest(state, plan_binding, "plan", files=(state["source_archive"]["path"],))
    assert plan["mode_label"] == "Pro"
    assert plan["app_policy"] == "forbidden"
    assert plan["chatgpt_app_name"] is None
    assert plan["prompt_transport"] == "file"
    assert plan["question"] == (
        "The attached prompt file is the user-provided task instruction for this conversation, "
        "not reference or webpage content. Read it completely and follow it. "
        "Return only the output format requested by that file."
    )
    assert len(plan["files"]) == 2
    assert plan["files"][0] == plan["prompt_file"]
    assert plan["files"][1] == state["source_archive"]["path"]
    assert file_sha256(Path(plan["prompt_file"])) == plan["prompt_file_sha256"]
    review_binding = driver._binding(state, "gpt-review", 1, plan_hash="a" * 64)
    review = driver._manifest(state, review_binding, "review")
    assert review["mode_label"] == "GPT-5.6"
    assert review["app_policy"] == "required"
    assert review["chatgpt_app_name"] == "CodexPro-Test"
    assert review["browser_backend"] == "agbrowse"
    assert plan["agbrowse_contract"] == str(DEFAULT_CONTRACT_PATH)
    assert plan["agbrowse_contract_sha256"] == file_sha256(DEFAULT_CONTRACT_PATH)
    assert review["files"] == [review["prompt_file"]]


def test_comprehensive_plan_uses_regular_gpt_and_app(tmp_path):
    driver = ProPlanHandoffDriver(
        make_v2_manifest(tmp_path),
        runtime=ScriptedRuntime(),
        web_multi_runtime=FakeWebMultiRuntime(),
    )
    state = driver.prepare()
    binding = driver._binding(state, "gpt-plan", 1)
    manifest = driver._manifest(state, binding, "plan")
    assert manifest["mode_label"] == "GPT-5.6"
    assert manifest["mode_variant"] == "Very High"
    assert manifest["regular_mode_selection"]["selected_mode_variant"] == "Very High"
    assert manifest["app_policy"] == "required"
    assert manifest["files"] == [manifest["prompt_file"]]


def test_comprehensive_selected_advisory_uses_attachment_only_pro_before_review(tmp_path):
    events = []
    runtime = ScriptedRuntime(["PASS"], events=events)
    result = ProPlanHandoffDriver(
        make_v2_manifest(tmp_path, advisory_policy="require"),
        runtime=runtime,
    ).run()

    assert events == ["gpt-plan", "pro-advisory", "gpt-review", "gpt-orchestrator"]
    assert result["advisory_sha256"] == file_sha256(Path(result["advisory_path"]))
    manifest = runtime.manifests[1]
    assert manifest["mode_label"] == "Pro"
    assert manifest["app_policy"] == "forbidden"
    assert manifest["chatgpt_app_name"] is None


def test_comprehensive_pass_invokes_parallel_v3_children_when_capacity_is_attested(tmp_path: Path) -> None:
    manifest_path = make_v2_manifest(tmp_path)
    v3, graph, receipt = add_parallel_execution(manifest_path)
    assert receipt is not None
    events: list[str] = []
    runtime = ScriptedRuntime(["PASS"], events=events)
    parallel = FakeParallelImplementationRuntime()

    result = ProPlanHandoffDriver(
        manifest_path,
        runtime=runtime,
        parallel_implementation_runtime=parallel,
    ).run()

    assert events == ["gpt-plan", "gpt-review"]
    assert parallel.calls == [(v3.resolve(), graph.resolve(), receipt.resolve())]
    assert result["implementation_route"]["selected"] is True
    assert result["parallel_implementation_result"]["execution"]["status"] == "FINALIZED"
    mission = json.loads(Path(result["execution_mission_path"]).read_text(encoding="utf-8"))
    assert mission["execution_parallelism"]["owner"] == "parallel-v3-web-gpt-implementers"
    assert mission["execution_parallelism"]["same_project_web_submissions"] == "capacity-bound exact child sessions"


def test_comprehensive_parallel_without_capacity_falls_back_to_single_command(tmp_path: Path) -> None:
    manifest_path = make_v2_manifest(tmp_path)
    add_parallel_execution(manifest_path, with_capacity=False)
    events: list[str] = []
    runtime = ScriptedRuntime(["PASS"], events=events)
    parallel = FakeParallelImplementationRuntime()

    result = ProPlanHandoffDriver(
        manifest_path,
        runtime=runtime,
        parallel_implementation_runtime=parallel,
    ).run()

    assert events == ["gpt-plan", "gpt-review", "gpt-orchestrator"]
    assert parallel.calls == []
    assert result["implementation_route"]["reason"] == "capacity-receipt-absent"
    assert result["orchestrator_envelope"]["status"] == "complete"


def test_comprehensive_revise_repeats_fresh_plan_advisory_review_sequence(tmp_path):
    events = []
    runtime = ScriptedRuntime(["REVISE", "PASS"], events=events)
    result = ProPlanHandoffDriver(
        make_v2_manifest(tmp_path, advisory_policy="require"),
        runtime=runtime,
    ).run()

    assert events == [
        "gpt-plan", "pro-advisory", "gpt-review",
        "gpt-plan", "pro-advisory", "gpt-review",
        "gpt-orchestrator",
    ]
    assert Path(result["advisory_path"]).name == "advisory-2.json"


def test_new_v1_comprehensive_manifest_is_rejected_before_runtime(tmp_path: Path) -> None:
    runtime = ScriptedRuntime()
    with pytest.raises(WorkflowError, match="COMPREHENSIVE_V2_REQUIRED"):
        ProPlanHandoffDriver(
            make_manifest(tmp_path, "gpt-comprehensive"),
            runtime=runtime,
            web_multi_runtime=FakeWebMultiRuntime(),
        )
    assert runtime.manifests == []


def test_matching_persisted_v1_comprehensive_state_remains_recoverable(tmp_path: Path) -> None:
    manifest_path = make_manifest(tmp_path, "gpt-comprehensive")
    state_path = seed_legacy_comprehensive_recovery(manifest_path)
    driver = ProPlanHandoffDriver(
        manifest_path,
        runtime=ScriptedRuntime(),
        web_multi_runtime=FakeWebMultiRuntime(),
    )

    assert driver.prepare()["status"] == "PREPARED"
    assert state_path.is_file()


def test_v2_published_workflow_schema_accepts_explicit_decision_inputs(tmp_path: Path) -> None:
    path = make_v2_manifest(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "workflow-v2.schema.json").read_text(encoding="utf-8")
    )

    validate_json_schema(value, schema)
    assert validate_manifest(value)["schema"] == "codex.chatgpt.comprehensive-workflow/v2"


def test_v2_schema_accepts_strict_optional_parallel_execution_reference(tmp_path: Path) -> None:
    manifest_path = make_v2_manifest(tmp_path)
    add_parallel_execution(manifest_path)
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "workflow-v2.schema.json").read_text(encoding="utf-8")
    )
    validate_json_schema(value, schema)
    value["parallel_execution"] = {"enabled": False, "capacity_receipt": "must-not-exist.json"}
    with pytest.raises(ValidationError):
        validate_json_schema(value, schema)


def test_v2_selected_contract_is_exactly_propagated_and_identity_bound(tmp_path: Path) -> None:
    contract = tmp_path / "captured-agbrowse-0.2.0.json"
    contract.write_text(DEFAULT_CONTRACT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    manifest_path = make_v2_manifest(
        tmp_path,
        research_triggers=["current_external_facts"],
        advisory_triggers=["public_release"],
    )
    add_selected_contract(manifest_path, contract)
    runtime = ScriptedRuntime(["PASS"])
    result = ProPlanHandoffDriver(
        manifest_path, runtime=runtime
    ).run()

    expected_path = str(contract)
    expected_hash = file_sha256(contract)
    assert result["agbrowse_contract_path"] == expected_path
    assert result["agbrowse_contract_sha256"] == expected_hash
    assert all(item["agbrowse_contract"] == expected_path for item in runtime.manifests)
    assert all(item["agbrowse_contract_sha256"] == expected_hash for item in runtime.manifests)
    advisory_manifest = next(item for item in runtime.manifests if item["workflow_route"] == "attachment-only-pro" and item["mode_label"] == "Pro")
    assert advisory_manifest["agbrowse_contract"] == expected_path
    assert advisory_manifest["agbrowse_contract_sha256"] == expected_hash


def test_v2_relative_contract_is_canonicalized_once_for_all_children(tmp_path: Path, monkeypatch) -> None:
    contract = tmp_path / "captured-agbrowse-relative.json"
    contract.write_text(DEFAULT_CONTRACT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    manifest_path = make_v2_manifest(tmp_path)
    add_selected_contract(manifest_path, Path(contract.name))
    monkeypatch.chdir(tmp_path)
    runtime = ScriptedRuntime(["PASS"])

    result = ProPlanHandoffDriver(
        manifest_path, runtime=runtime, web_multi_runtime=FakeWebMultiRuntime()
    ).run()

    expected = str(contract.resolve())
    assert result["agbrowse_contract_path"] == expected
    assert all(item["agbrowse_contract"] == expected for item in runtime.manifests)


def test_v2_contract_path_must_be_regular_json_file(tmp_path: Path) -> None:
    manifest_path = make_v2_manifest(tmp_path)
    add_selected_contract(manifest_path, tmp_path / "missing.json")

    with pytest.raises(WorkflowError, match="AGBROWSE_CONTRACT_PATH_INVALID"):
        ProPlanHandoffDriver(manifest_path, runtime=ScriptedRuntime(), web_multi_runtime=FakeWebMultiRuntime())


def test_v2_rerun_rejects_selected_contract_content_drift(tmp_path: Path) -> None:
    contract = tmp_path / "captured-agbrowse-0.2.0.json"
    contract.write_text(DEFAULT_CONTRACT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    manifest_path = make_v2_manifest(tmp_path)
    add_selected_contract(manifest_path, contract)
    driver = ProPlanHandoffDriver(manifest_path, runtime=ScriptedRuntime(), web_multi_runtime=FakeWebMultiRuntime())
    driver.run(prepare_only=True)
    contract.write_text('{"changed": true}', encoding="utf-8")

    with pytest.raises(WorkflowError, match="AGBROWSE_CONTRACT_IDENTITY_CONFLICT"):
        ProPlanHandoffDriver(manifest_path, runtime=ScriptedRuntime(), web_multi_runtime=FakeWebMultiRuntime()).run(prepare_only=True)


def test_v2_auto_skips_optional_stages_and_binds_skip_descriptors(tmp_path: Path) -> None:
    events: list[str] = []
    runtime = ScriptedRuntime(["PASS"], events=events)
    web_multi = FakeWebMultiRuntime(events=events)

    result = ProPlanHandoffDriver(
        make_v2_manifest(tmp_path),
        runtime=runtime,
        web_multi_runtime=web_multi,
    ).run()

    assert events == ["gpt-plan", "gpt-review", "gpt-orchestrator"]
    assert web_multi.manifests == []
    research = json.loads(Path(result["research_descriptor_path"]).read_text(encoding="utf-8"))
    advisory = json.loads(Path(result["advisory_descriptor_path"]).read_text(encoding="utf-8"))
    assert research["decision"] == advisory["decision"] == "skip"
    assert research["artifact"] is None and advisory["artifact"] is None
    assert result["orchestrator_envelope"]["input_research_descriptor_sha256"] == result["research_descriptor_sha256"]
    assert result["orchestrator_envelope"]["input_advisory_descriptor_sha256"] == result["advisory_descriptor_sha256"]

    orchestrator_manifest = runtime.manifests[-1]
    prompt = Path(orchestrator_manifest["prompt_file"]).read_text(encoding="utf-8")
    mission_path = next(
        Path(item) for item in orchestrator_manifest["read_only_paths"]
        if Path(item).name == "execution-mission.json"
    )
    mission = json.loads(mission_path.read_text(encoding="utf-8"))
    parallelism = mission["execution_parallelism"]
    assert parallelism["owner"] == "web-gpt-orchestrator"
    assert parallelism["scope"] == "within-single-execution-mission"
    assert parallelism["same_project_web_submissions"] == "one-active-or-uncertain-run"
    assert "no delegated strategy search" in parallelism["local_codex"]
    assert "internal lanes or parallel tool calls" in prompt
    assert "do not return strategy search or implementation to local codex" in prompt.casefold()
    assert orchestrator_manifest["workflow_route"] == "command"


def test_v2_research_trigger_runs_app_backed_deep_stage_at_high(tmp_path: Path) -> None:
    events: list[str] = []
    runtime = ScriptedRuntime(["PASS"], events=events)
    driver = ProPlanHandoffDriver(
        make_v2_manifest(tmp_path, research_triggers=["current_external_facts"]),
        runtime=runtime,
        web_multi_runtime=FakeWebMultiRuntime(events=events),
    )

    result = driver.run()

    assert events == ["deep-research", "gpt-plan", "gpt-review", "gpt-orchestrator"]
    descriptor = json.loads(Path(result["research_descriptor_path"]).read_text(encoding="utf-8"))
    assert descriptor["decision"] == "run"
    assert Path(descriptor["artifact"]["path"]).is_file()
    stage_manifest = json.loads(
        (driver.stages_dir / "deep-research-attempt-1" / "stage.manifest.json").read_text(encoding="utf-8")
    )
    assert stage_manifest["mode_variant"] == "Very High"
    assert stage_manifest["app_policy"] == "required"
    assert stage_manifest["chatgpt_app_name"] == "CodexPro-Test"
    assert stage_manifest["research_selection_transport"] == "preselected-research"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"advisory_triggers": ["public_release"]},
        {"affected_components": 3},
        {"cross_component_interfaces": 2},
    ],
)
def test_v2_advisory_risk_gate_runs_attachment_only_pro_advisory(tmp_path: Path, kwargs: dict) -> None:
    events: list[str] = []
    runtime = ScriptedRuntime(["PASS"], events=events)
    result = ProPlanHandoffDriver(
        make_v2_manifest(tmp_path, **kwargs),
        runtime=runtime,
    ).run()

    assert events == ["gpt-plan", "pro-advisory", "gpt-review", "gpt-orchestrator"]
    advisory_manifest = runtime.manifests[1]
    assert advisory_manifest["workflow_route"] == "attachment-only-pro"
    assert advisory_manifest["app_policy"] == "forbidden"
    descriptor = json.loads(Path(result["advisory_descriptor_path"]).read_text(encoding="utf-8"))
    assert descriptor["decision"] == "run"


def test_non_pro_selection_is_immutable_and_rejects_downgrade(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_CHATGPT_REGULAR_MODE_CAPABILITIES", "Very High,High")
    driver = ProPlanHandoffDriver(make_v2_manifest(tmp_path), runtime=ScriptedRuntime())
    state = driver.prepare()
    manifest = driver._manifest(state, driver._binding(state, "gpt-plan", 1), "plan")
    assert manifest["mode_variant"] == "Very High"
    assert manifest["regular_mode_selection"]["selection_rule"] == "highest-supported:Very High>High"

    monkeypatch.setenv("CODEX_CHATGPT_REGULAR_MODE_CAPABILITIES", "High")
    with pytest.raises(WorkflowError, match="REGULAR_MODE_SELECTION_IDENTITY_CONFLICT"):
        ProPlanHandoffDriver(driver.manifest_path, runtime=ScriptedRuntime()).prepare()


def test_v2_contradiction_evidence_hash_is_verified_before_runtime(tmp_path: Path) -> None:
    base = make_v2_manifest(tmp_path)
    value = json.loads(base.read_text(encoding="utf-8"))
    evidence = Path(value["workspace"]["root"]) / "contradiction.txt"
    evidence.write_text("conflict", encoding="utf-8")
    value["gates"]["advisory"].update(
        {
            "triggers": ["verified_contradiction_evidence"],
            "contradiction_evidence": [
                {"path": "contradiction.txt", "sha256": "0" * 64}
            ],
        }
    )
    base.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(WorkflowError, match="GATE_EVIDENCE_HASH_MISMATCH"):
        ProPlanHandoffDriver(base, runtime=ScriptedRuntime(), web_multi_runtime=FakeWebMultiRuntime())


def test_v2_require_and_skip_policies_override_empty_or_present_triggers(tmp_path: Path) -> None:
    require_events: list[str] = []
    ProPlanHandoffDriver(
        make_v2_manifest(tmp_path / "require", research_policy="require"),
        runtime=ScriptedRuntime(["PASS"], events=require_events),
        web_multi_runtime=FakeWebMultiRuntime(events=require_events),
    ).run()
    assert require_events[0] == "deep-research"

    skip_events: list[str] = []
    ProPlanHandoffDriver(
        make_v2_manifest(
            tmp_path / "skip",
            advisory_policy="skip",
            advisory_triggers=["public_release"],
        ),
        runtime=ScriptedRuntime(["PASS"], events=skip_events),
        web_multi_runtime=FakeWebMultiRuntime(events=skip_events),
    ).run()
    assert "web-multi" not in skip_events


def test_end_to_end_requires_pass_then_orchestrates(tmp_path):
    runtime = ScriptedRuntime(["PASS"])
    result = ProPlanHandoffDriver(make_manifest(tmp_path), runtime=runtime).run()
    assert result["status"] == "LOCAL_VERIFY_REQUIRED"
    assert runtime.count == 3
    assert result["orchestrator_envelope"]["status"] == "complete"


def test_exact_rerun_reuses_checkpoint_nonce_manifest_and_runtime_run(tmp_path: Path) -> None:
    runtime = CheckpointRecoveringRuntime()
    manifest = make_manifest(tmp_path)

    first = ProPlanHandoffDriver(manifest, runtime=runtime).run()
    first_count = runtime.count
    second = ProPlanHandoffDriver(manifest, runtime=runtime).run()

    assert runtime.count == first_count == 3
    assert second["stages"] == first["stages"]
    stage_manifest = json.loads(
        (tmp_path / "runs" / "wf-test" / "stages" / "pro-plan-attempt-1" / "stage.manifest.json").read_text(encoding="utf-8")
    )
    assert stage_manifest["workflow_correlation"]["nonce"] == first["stages"]["pro-plan-attempt-1"]["nonce"]


def test_uncertain_rerun_recovers_exact_stage_without_duplicate_start(tmp_path: Path) -> None:
    runtime = UncertainThenRecoveringRuntime()
    manifest = make_manifest(tmp_path)
    driver = ProPlanHandoffDriver(manifest, runtime=runtime)

    with pytest.raises(WorkflowError, match="RUNTIME_NOT_COMPLETED"):
        driver.run()

    assert not driver.lock_path.exists()
    state_after_failure = json.loads(driver.state_path.read_text(encoding="utf-8"))
    first_checkpoint = dict(state_after_failure["stages"]["pro-plan-attempt-1"])
    stage_manifest_path = driver.stages_dir / "pro-plan-attempt-1" / "stage.manifest.json"
    first_manifest_bytes = stage_manifest_path.read_bytes()
    first_run_id = runtime.uncertain_run_id
    assert runtime.count == 1

    result = ProPlanHandoffDriver(manifest, runtime=runtime).run()

    assert result["status"] == "LOCAL_VERIFY_REQUIRED"
    assert runtime.count == 3
    assert runtime.by_manifest[str(stage_manifest_path.resolve())] == first_run_id
    assert stage_manifest_path.read_bytes() == first_manifest_bytes
    assert result["stages"]["pro-plan-attempt-1"] == first_checkpoint


def test_revise_creates_fresh_plan_and_review_before_orchestrator(tmp_path):
    runtime = ScriptedRuntime(["REVISE", "PASS"])
    result = ProPlanHandoffDriver(make_manifest(tmp_path), runtime=runtime).run()
    assert result["status"] == "LOCAL_VERIFY_REQUIRED"
    assert runtime.count == 5
    assert Path(result["plan_path"]).name == "plan-2.json"


def test_block_review_stops_before_orchestrator(tmp_path):
    runtime = ScriptedRuntime(["BLOCK"])
    with pytest.raises(WorkflowError, match="REVIEW_BLOCKED"):
        ProPlanHandoffDriver(make_manifest(tmp_path), runtime=runtime).run()
    assert runtime.count == 2


def test_fresh_stage_rejects_conversation_reuse(tmp_path):
    runtime = ScriptedRuntime(["PASS"], reuse_url=True)
    with pytest.raises(WorkflowError, match="CONVERSATION_REUSED"):
        ProPlanHandoffDriver(make_manifest(tmp_path), runtime=runtime).run()


def test_prepare_only_never_dispatches(tmp_path):
    runtime = ScriptedRuntime()
    result = ProPlanHandoffDriver(make_manifest(tmp_path), runtime=runtime).run(prepare_only=True)
    assert result["status"] == "PREPARED"
    assert runtime.count == 0
    assert Path(result["source_archive"]["path"]).is_file()


def test_workflow_lock_blocks_second_driver(tmp_path):
    lock = tmp_path / ".lock"
    with exclusive_workflow_lock(lock):
        with pytest.raises(WorkflowError, match="WORKFLOW_ALREADY_ACTIVE"):
            with exclusive_workflow_lock(lock):
                pass
