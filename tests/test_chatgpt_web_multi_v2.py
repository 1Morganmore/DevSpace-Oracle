from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "chatgpt_web_multi_runtime.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_web_multi_runtime_v2_test", MODULE_PATH)
assert SPEC and SPEC.loader
RUNTIME = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNTIME
SPEC.loader.exec_module(RUNTIME)

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def hermetic_contract_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    # Runtime behavior is under test here; installation validation has its own
    # contract suite. The scoped patch cannot leak into neighboring modules.
    monkeypatch.setattr(
        RUNTIME.BRIDGE,
        "read_contract",
        lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
    )


def make_v2_manifest(
    tmp_path: Path,
    *,
    planner_policy: str = "strict-6-10",
    extra: dict | None = None,
) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "source.txt"
    source.write_text("source\n", encoding="utf-8")
    snapshot = tmp_path / "source-snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema": "test.snapshot/v1",
                "files": [
                    {
                        "path": str(source),
                        "sha256": sha256(source),
                        "bytes": source.stat().st_size,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    contract = tmp_path / "agbrowse-contract.json"
    contract.write_text("{}\n", encoding="utf-8")
    value = {
        "schema": "codex.chatgpt.web-multi/v2",
        "workflow_id": f"v2-{tmp_path.name}",
        "project_root": str(project),
        "question": "Design a safe implementation.",
        "source_snapshot_path": str(snapshot),
        "source_snapshot_sha256": sha256(snapshot),
        "output_dir": str(tmp_path / "output"),
        "chatgpt_app_name": "CodexPro-Test",
        "planner_policy": planner_policy,
        "semantics_version": "upstream-parity-v1",
        "max_iterations": 1,
        "mode_variant": "High",
        "provider_failure_retry_limit": 0,
        "agbrowse_contract": str(contract),
    }
    value.update(extra or {})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    return manifest


def make_v1_manifest(tmp_path: Path, *, solver_count: int = 2) -> Path:
    manifest = make_v2_manifest(tmp_path)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["schema"] = "codex.chatgpt.web-multi/v1"
    value["mode_variant"] = "Very High"
    value["solver_count"] = solver_count
    value.pop("planner_policy")
    value.pop("semantics_version")
    manifest.write_text(json.dumps(value), encoding="utf-8")
    return manifest


class DynamicExecutor:
    def __init__(self, count: int = 6, *, slow_pairs: bool = True):
        self.count = count
        self.slow_pairs = slow_pairs
        self.events: list[tuple[str, str, float]] = []
        self.lock = threading.Lock()

    def __call__(self, spec, context, child):
        with self.lock:
            self.events.append(("start", spec.stage_id, time.monotonic()))
        if self.slow_pairs and spec.role == "Solver" and spec.lane == self.count - 1:
            time.sleep(5.0)
        elif self.slow_pairs and spec.role == "Merger" and spec.lane == 0:
            time.sleep(5.0)
        elif spec.role == "FinalMerger" and spec.lane == 0:
            time.sleep(0.08)
        else:
            time.sleep(0.005)
        if spec.role == "Planner":
            payload = {
                "problem_analysis": "analysis",
                "approaches": [
                    {
                        "name": f"approach-{index}",
                        "description": f"description-{index}",
                        "methodology": f"method-{index}",
                    }
                    for index in range(self.count)
                ],
            }
        elif spec.role == "Judge":
            if context.get("solver_count") is not None:
                payload = {
                    "is_sufficient": True,
                    "best_stage_id": context["assignment"]["candidate_stage_ids"][0],
                    "outstanding_stage_ids": [],
                }
            else:
                payload = {
                    "is_sufficient": False,
                    "best_id": None,
                    "outstanding_ids": list(range(1, self.count + 1)),
                    "inadequate_ids": [],
                    "rationale": "continue",
                }
        elif spec.role == "Organizer":
            payload = {"final_answer": "organized v2 answer"}
        elif spec.role in {"Merger", "FinalMerger"}:
            payload = {
                "content": f"{spec.role}-{spec.lane}",
                "source_stage_ids": context["assignment"].get("source_stage_ids", []),
            }
        else:
            payload = {
                "content": f"{spec.role}-{spec.lane}",
                "assumptions": [],
                "counterexamples": [],
            }
        with self.lock:
            self.events.append(("end", spec.stage_id, time.monotonic()))
        return {
            "schema": "codex.chatgpt.web-multi-stage/v1",
            "workflow_id": context["workflow_id"],
            "parent_run_id": context["parent_run_id"],
            "stage_id": context["stage_id"],
            "role": context["role"],
            "lane": context["lane"],
            "iteration": context["iteration"],
            "prompt_sha256": context["prompt_sha256"],
            "challenge_nonce": context["challenge_nonce"],
            "evidence_map_sha256": context["evidence_map_sha256"],
            "read_receipts": [dict(item) for item in context["assigned_files"]],
            "payload": payload,
        }


@pytest.mark.parametrize(
    "extra",
    [
        {"solver_count": None},
        {"solver_count": 6},
        {"mode_variant": "Medium"},
        {"unknown_semantic_switch": True},
        {"semantics_version": "future"},
    ],
)
def test_invalid_v2_is_rejected_before_output_or_state_side_effects(
    tmp_path: Path,
    extra: dict,
) -> None:
    manifest = make_v2_manifest(tmp_path, extra=extra)
    state_root = tmp_path / "state"
    output = tmp_path / "output"
    with pytest.raises(RUNTIME.WebMultiError):
        RUNTIME.WebMultiRuntime(manifest, state_root=state_root)
    assert not output.exists()
    assert not state_root.exists()


def test_v2_accepts_very_high_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_CHATGPT_REGULAR_MODE_CAPABILITIES", "Very High,High")
    manifest = make_v2_manifest(tmp_path, extra={"mode_variant": "Very High"})

    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")

    assert runtime.manifest["mode_variant"] == "Very High"


def test_v2_missing_mode_variant_selects_highest_available_regular_level(tmp_path: Path) -> None:
    manifest = make_v2_manifest(tmp_path)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value.pop("mode_variant")
    manifest.write_text(json.dumps(value), encoding="utf-8")

    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")

    assert runtime.manifest["mode_variant"] == "High"


def test_v2_rejects_unattested_very_high_before_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_CHATGPT_REGULAR_MODE_CAPABILITIES", raising=False)
    manifest = make_v2_manifest(tmp_path, extra={"mode_variant": "Very High"})

    with pytest.raises(RUNTIME.WebMultiError) as failure:
        RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")

    assert failure.value.code == "MODE_VARIANT_UNAVAILABLE"
    assert not (tmp_path / "output").exists()
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("value", [None, 0, 6, True, "5"])
def test_provider_parallel_limit_is_strictly_validated_before_side_effects(tmp_path: Path, value: object) -> None:
    manifest = make_v2_manifest(tmp_path, extra={"provider_parallel_limit": value})

    with pytest.raises(RUNTIME.WebMultiError) as failure:
        RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")

    assert failure.value.code.startswith("PROVIDER_PARALLEL_LIMIT_")
    assert not (tmp_path / "output").exists()
    assert not (tmp_path / "state").exists()


def test_provider_parallel_limit_is_in_dry_run_and_stage_receipts(tmp_path: Path) -> None:
    runtime = RUNTIME.WebMultiRuntime(
        make_v2_manifest(tmp_path, extra={"provider_parallel_limit": 3}),
        state_root=tmp_path / "state",
        stage_executor=DynamicExecutor(6, slow_pairs=False),
    )

    assert runtime.dry_run()["provider_parallel_limit"] == 3
    evidence_map = runtime._source_evidence_map()
    runtime.workflow_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME.write_immutable_json(runtime.evidence_map_path, evidence_map)
    artifacts = runtime._stage_artifacts(
        RUNTIME.StageSpec("planner", "Planner", 0, 0, runtime.manifest["question"], tuple()),
        {"run_id": "receipt-parent"},
        evidence_map,
    )
    context = json.loads(Path(artifacts["context_path"]).read_text(encoding="utf-8"))
    receipt = json.loads(Path(artifacts["manifest_path"]).read_text(encoding="utf-8"))
    assert context["provider_parallel_limit"] == 3
    assert receipt["provider_parallel_limit"] == 3


class FallbackExecutor(DynamicExecutor):
    def __init__(self, *, fail_role: str):
        super().__init__(6, slow_pairs=False)
        self.fail_role = fail_role

    def __call__(self, spec, context, child):
        result = super().__call__(spec, context, child)
        if spec.role == self.fail_role:
            result["payload"] = {}
        return result


@pytest.mark.parametrize(
    ("failed_role", "expected_answer", "replacement_role"),
    [
        ("InitialRefiner", "organized v2 answer", "Solver"),
        ("Organizer", "FinalRefiner-0", "FinalRefiner"),
    ],
)
def test_v2_semantic_fallback_records_immutable_provenance(
    tmp_path: Path,
    failed_role: str,
    expected_answer: str,
    replacement_role: str,
) -> None:
    runtime = RUNTIME.WebMultiRuntime(
        make_v2_manifest(tmp_path, planner_policy="upstream-nonempty-prefix10"),
        state_root=tmp_path / "state",
        stage_executor=FallbackExecutor(fail_role=failed_role),
    )

    result = runtime.run()

    fallbacks = result["fallback_provenance"]
    assert fallbacks
    assert all(item["failed_role"] == failed_role for item in fallbacks)
    assert all(item["replacement_role"] == replacement_role for item in fallbacks)
    assert result["organizer_result"] == expected_answer
    assert all(
        Path(item["artifact_path"]).is_file()
        and item["artifact_sha256"] == sha256(Path(item["artifact_path"]))
        for item in fallbacks
    )
    assert all(
        {"role", "session_id", "target_id", "conversation_url"} <= set(item)
        for item in result["role_session_target_url_provenance"]
    )


def test_v2_fallback_classifier_keeps_transport_uncertainty_hard() -> None:
    assert RUNTIME.WebMultiRuntime._is_v2_fallback_error(
        RUNTIME.WebMultiError("CHILD_PROVIDER_FAILED_TERMINAL", "terminal")
    )
    assert not RUNTIME.WebMultiRuntime._is_v2_fallback_error(
        RUNTIME.WebMultiError("CHILD_NOT_COMPLETE", "RECOVERY_REQUIRED")
    )
    assert not RUNTIME.WebMultiRuntime._is_v2_fallback_error(
        RUNTIME.WebMultiError("CHILD_IDENTITY_INCOMPLETE", "identity")
    )


def test_dynamic_pipeline_pairs_lanes_and_selects_exact_seed_zero_result(tmp_path: Path) -> None:
    manifest = make_v2_manifest(tmp_path)
    executor = DynamicExecutor(6)
    runtime = RUNTIME.WebMultiRuntime(
        manifest,
        state_root=tmp_path / "state",
        stage_executor=executor,
    )

    result = runtime.run()

    assert result["actual_solver_count"] == 6
    assert result["observed_solver_count"] == 6
    assert result["provider_parallel_limit"] == 5
    assert result["organizer_result"] == "organized v2 answer"
    assert result["planner_policy"] == "strict-6-10"
    assert len(list(runtime.stages_dir.glob("final-merger-*/stage-result.json"))) == 6
    final_refiner_context = json.loads(
        (runtime.stages_dir / "final-refiner" / "stage-context.json").read_text(encoding="utf-8")
    )
    assert final_refiner_context["assignment"]["source_stage_id"] == "final-merger-0"
    assert final_refiner_context["input_stage_result_paths"][0].endswith(
        "/final-merger-0/stage-result.json"
    )
    starts = {
        stage_id: timestamp
        for kind, stage_id, timestamp in executor.events
        if kind == "start"
    }
    ends = {
        stage_id: timestamp
        for kind, stage_id, timestamp in executor.events
        if kind == "end"
    }
    assert starts["initial-refiner-0"] < ends["solver-5"]
    assert starts["iter-1-refiner-1"] < ends["iter-1-merger-0"] or (
        starts["iter-1-refiner-0"] < ends["iter-1-merger-1"]
    )
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["semantic_outcome"]["state"]
        == "accepted"
        for path in runtime.stages_dir.glob("*/stage-result.json")
    )


def test_dynamic_ten_lane_topology_completes_in_five_or_smaller_capacity_waves(tmp_path: Path) -> None:
    runtime = RUNTIME.WebMultiRuntime(
        make_v2_manifest(tmp_path),
        state_root=tmp_path / "state",
        stage_executor=DynamicExecutor(10, slow_pairs=False),
    )

    result = runtime.run()

    assert result["actual_solver_count"] == 10
    assert len(list(runtime.stages_dir.glob("final-merger-*/stage-result.json"))) == 8
    assert result["provider_parallel_limit"] == 5
    assert result["max_concurrent_child_generations"] <= 5


def test_dynamic_resume_rejects_tampered_planner_descriptor(tmp_path: Path) -> None:
    manifest = make_v2_manifest(tmp_path)
    runtime = RUNTIME.WebMultiRuntime(
        manifest,
        state_root=tmp_path / "state",
        stage_executor=DynamicExecutor(6),
    )
    result = runtime.run()
    planner_result = runtime.stages_dir / "planner" / "stage-result.json"
    value = json.loads(planner_result.read_text(encoding="utf-8"))
    value["payload"]["planner_descriptor"]["actual_count"] = 5
    planner_result.write_text(json.dumps(value), encoding="utf-8")

    resumed = RUNTIME.WebMultiRuntime(
        manifest,
        state_root=tmp_path / "state",
        stage_executor=DynamicExecutor(6),
    )
    planner_spec = RUNTIME.StageSpec(
        "planner",
        "Planner",
        0,
        0,
        resumed.manifest["question"],
        tuple(),
    )
    planner = resumed._execute_stage(planner_spec, resumed._source_evidence_map())
    with pytest.raises(RUNTIME.WebMultiError) as failure:
        resumed._resolve_planner_descriptor(planner)
    assert failure.value.code == "PLANNER_DESCRIPTOR_INVALID"
    assert result["actual_solver_count"] == 6


def test_legacy_v1_pipeline_remains_fixed_and_has_no_v2_result_fields(tmp_path: Path) -> None:
    manifest = make_v1_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(
        manifest,
        state_root=tmp_path / "state",
        stage_executor=DynamicExecutor(2, slow_pairs=False),
    )

    result = runtime.run()

    assert result["organizer_result"] == "organized v2 answer"
    assert "manifest_schema" not in result
    assert "planner_descriptor_sha256" not in result
    planner_context = json.loads(
        (runtime.stages_dir / "planner" / "stage-context.json").read_text(encoding="utf-8")
    )
    assert planner_context["solver_count"] == 2
    assert "planner_descriptor_sha256" not in planner_context
