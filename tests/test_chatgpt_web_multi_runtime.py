from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_web_multi_runtime.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_web_multi_runtime_test", MODULE_PATH)
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


def make_manifest(tmp_path: Path, *, solver_count: int, max_iterations: int = 1, large: bool = False) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "source.txt"
    source.write_bytes((b"x" * (1_200_000 if large else 1024)) + b"\n")
    snapshot = tmp_path / "source-snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema": "test.snapshot/v1",
                "files": [{"path": str(source), "sha256": sha256(source), "bytes": source.stat().st_size}],
            }
        ),
        encoding="utf-8",
    )
    contract = tmp_path / "agbrowse-contract.json"
    contract.write_text("{}\n", encoding="utf-8")
    manifest = tmp_path / "web-multi.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "codex.chatgpt.web-multi/v1",
                "workflow_id": f"wf-{solver_count}-{tmp_path.name}",
                "project_root": str(project),
                "question": "Find the safest implementation architecture.",
                "source_snapshot_path": str(snapshot),
                "source_snapshot_sha256": sha256(snapshot),
                "output_dir": str(tmp_path / "output"),
                "chatgpt_app_name": "CodexPro-Test",
                "solver_count": solver_count,
                "max_iterations": max_iterations,
                "mode_variant": "Very High",
                "agbrowse_contract": str(contract),
            }
        ),
        encoding="utf-8",
    )
    return manifest


class FakeStageExecutor:
    def __init__(self, solver_count: int, *, duplicate_identity: bool = False, bad_receipt_role: str | None = None):
        self.solver_count = solver_count
        self.duplicate_identity = duplicate_identity
        self.bad_receipt_role = bad_receipt_role
        self.condition = threading.Condition()
        self.entered: dict[str, int] = {}

    def _barrier(self, role: str) -> None:
        if role not in {"Solver", "InitialRefiner", "Merger", "LoopRefiner"}:
            return
        with self.condition:
            self.entered[role] = self.entered.get(role, 0) + 1
            if self.entered[role] >= self.solver_count:
                self.condition.notify_all()
            else:
                assert self.condition.wait_for(lambda: self.entered.get(role, 0) >= self.solver_count, timeout=5)

    def __call__(self, spec, context, child):
        self._barrier(spec.role)
        if spec.role == "Planner":
            payload = {"problem_analysis": "analysis", "approaches": [f"approach-{i}" for i in range(self.solver_count)]}
        elif spec.role == "Judge":
            payload = {"is_sufficient": True, "best_stage_id": context["assignment"]["candidate_stage_ids"][0]}
        elif spec.role == "Organizer":
            payload = {"final_answer": "organized answer"}
        elif spec.role in {"Merger", "FinalMerger"}:
            payload = {"content": f"{spec.role} result", "source_stage_ids": context["assignment"].get("source_stage_ids", [])}
        else:
            payload = {"content": f"{spec.role} result", "assumptions": [], "counterexamples": []}
        receipts = [dict(item) for item in context["assigned_files"]]
        if spec.role == self.bad_receipt_role:
            receipts[0]["sha256"] = "0" * 64
        envelope = {
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
            "read_receipts": receipts,
            "payload": payload,
        }
        result = {"envelope": envelope}
        if self.duplicate_identity and spec.role == "Solver":
            result.update(
                {
                    "session_id": "S-DUPLICATE",
                    "target_id": "T-DUPLICATE",
                    "conversation_url": "https://chatgpt.com/c/duplicate",
                }
            )
        return result


class CleanupOnlyBridge:
    def __init__(self, store):
        self.store = store
        self.cleanup_calls: list[tuple[str, bool]] = []

    def cleanup_completed(self, run_dir, *, explicit_user_request):
        _, record = self.store.load(run_dir)
        self.cleanup_calls.append((str(run_dir), explicit_user_request))
        return {
            "ok": True,
            "state": "closed-and-absent",
            "target_id": record["current_target_id"],
            "conversation_url": record["conversation_url"],
        }


def make_completed_child(runtime, *, answer_text: str):
    evidence_map = runtime._source_evidence_map()
    runtime.workflow_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME.write_immutable_json(runtime.evidence_map_path, evidence_map)
    runtime.parent = runtime.store.create_parent_workflow(
        project_root=runtime.manifest["project_root"],
        manifest_path=runtime.manifest_path,
        workflow_id=runtime.workflow_id,
        agbrowse_contract=runtime.contract,
    )
    spec = RUNTIME.StageSpec("planner", "Planner", 0, 0, runtime.manifest["question"], tuple())
    artifacts = runtime._stage_artifacts(spec, runtime.parent, evidence_map)
    child = runtime.store.create_child_run(
        parent_run_dir=runtime.parent["run_dir"],
        manifest_path=artifacts["manifest_path"],
        agbrowse_contract=runtime.contract,
        role=spec.role,
        lane=spec.lane,
        iteration=spec.iteration,
        stage_id=spec.stage_id,
        send_limit=1,
    )
    run_dir = child["run_dir"]
    runtime.store.transition(run_dir, "PREFLIGHTED")
    runtime.store.transition(run_dir, "LEASED")
    runtime.store.claim_child_send(run_dir)
    runtime.store.transition(
        run_dir,
        "SUBMITTED",
        session_id="session-cleanup-order",
        target_id="target-cleanup-order",
        submission_receipt={"test": True},
    )
    runtime.store.transition(
        run_dir,
        "URL_BOUND",
        conversation_url="https://chatgpt.com/c/cleanup-order",
    )
    answer_path = Path(run_dir) / "answer.md"
    answer_path.write_text(answer_text, encoding="utf-8")
    descriptor = {
        "path": str(answer_path),
        "sha256": sha256(answer_path),
        "bytes": answer_path.stat().st_size,
        "provider_status": "complete",
    }
    runtime.store.transition(run_dir, "RESULT_CAPTURED", result=descriptor)
    runtime.store.transition(run_dir, "VERIFIED")
    runtime.store.transition(run_dir, "COMPLETE")
    _, latest = runtime.store.load(run_dir)
    latest["run_dir"] = run_dir
    return spec, latest


@pytest.mark.parametrize("solver_count", [2, 4])
def test_full_pipeline_uses_fresh_children_and_real_parallel_generation(tmp_path: Path, solver_count: int) -> None:
    manifest = make_manifest(tmp_path, solver_count=solver_count)
    executor = FakeStageExecutor(solver_count)
    runtime = RUNTIME.WebMultiRuntime(
        manifest,
        state_root=tmp_path / "state",
        stage_executor=executor,
    )

    result = runtime.run()

    assert result["schema"] == "codex.chatgpt.web-multi-result/v1"
    assert result["organizer_result"] == "organized answer"
    assert result["advisory_only"] is True
    assert result["provider_parallel_limit"] == 5
    assert result["max_concurrent_child_generations"] >= 2
    provenance = result["provenance"]
    assert len({item["run_id"] for item in provenance}) == len(provenance)
    assert len({item["session_id"] for item in provenance}) == len(provenance)
    assert len({item["target_id"] for item in provenance}) == len(provenance)
    assert len({item["conversation_url"] for item in provenance}) == len(provenance)
    assert all(item["send_attempt_count"] == 1 for item in provenance)
    assert not Path(runtime.store.paths(Path(runtime.manifest["project_root"]), "unused").lock_file).exists()
    for stage_manifest in runtime.stages_dir.glob("*/stage.manifest.json"):
        value = json.loads(stage_manifest.read_text(encoding="utf-8"))
        assert value["app_policy"] == "required"
        assert value["mode_label"] == "GPT-5.6"
        assert len(value["files"]) == 1
        assert value["files"][0].endswith("prompt.txt")
        prompt = Path(value["prompt_file"]).read_text(encoding="utf-8")
        context = json.loads(Path(value["read_only_paths"][0]).read_text(encoding="utf-8"))
        assert context["challenge_nonce"] not in prompt
        assert context["root_question"] == runtime.manifest["question"]
        assert "The header keys must be exactly" in prompt
        assert "Inside the payload markers, follow this exact role layout" in prompt
        assert RUNTIME.HEADER_BEGIN in prompt
        assert RUNTIME.PAYLOAD_BEGIN in prompt
        assert "never convert a path to Windows backslashes" in prompt
        expected_source_files = 1 if context["role"] in {"Planner", "Solver", "Judge", "Organizer"} else 0
        expected_assigned = expected_source_files + len(context["input_stage_result_paths"])
        assert len(context["assigned_files"]) == expected_assigned
        assert value["prompt_profile"] == context["prompt_profile"]
        assert value["prompt_profile_receipt"] == context["prompt_profile_receipt"]
        assert value["gpt_operation_mode"] == context["prompt_profile_receipt"]["task_kind"]
        if context["role"] == "Judge":
            assert "strongest material objection" in prompt
        else:
            assert "strongest material objection" not in prompt
        if context["role"] == "Solver":
            assert context["input_stage_result_paths"] == []
            assert "including assumptions and counterexamples" not in prompt
        if context["role"] == "InitialRefiner":
            assert len(context["input_stage_result_paths"]) == 1
            assert "planner" not in Path(context["input_stage_result_paths"][0]).name.casefold()
        assigned_paths = {Path(item["path"]).resolve() for item in context["assigned_files"]}
        assert {
            Path(path).resolve() for path in context["input_stage_result_paths"]
        }.issubset(assigned_paths)
        for item in context["assigned_files"]:
            assigned_path = Path(item["path"])
            assert "\\" not in item["path"]
            assert item["sha256"] == sha256(assigned_path)
            assert item["bytes"] == assigned_path.stat().st_size


def test_duplicate_child_identity_fails_without_replacement_send(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(
        manifest,
        state_root=tmp_path / "state",
        stage_executor=FakeStageExecutor(2, duplicate_identity=True),
    )

    with pytest.raises(RUNTIME.WebMultiError) as failure:
        runtime.run()

    assert failure.value.code == "CHILD_IDENTITY_REUSED"
    parent = runtime.parent
    paths = runtime.store.paths(Path(runtime.manifest["project_root"]), parent["run_id"])
    children = runtime.store._parent_children(paths.runs_dir, parent["run_id"])
    solver_children = [child for _, child in children if child["role"] == "Solver"]
    assert len(solver_children) == 2
    assert all(child["send_attempt_count"] == 1 for child in solver_children)


def test_cached_stage_provenance_target_id_is_accepted(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")

    runtime._accept_identity(
        {
            "session_id": "session-cached",
            "target_id": "target-cached",
            "conversation_url": "https://chatgpt.com/c/cached",
        },
        "planner",
    )

    assert runtime._identities["target_id"] == {"target-cached"}


def test_stage_artifacts_reuse_exact_immutable_context_on_resume(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    evidence_map = runtime._source_evidence_map()
    runtime.workflow_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME.write_immutable_json(runtime.evidence_map_path, evidence_map)
    parent = runtime.store.create_parent_workflow(
        project_root=runtime.manifest["project_root"],
        manifest_path=runtime.manifest_path,
        workflow_id=runtime.workflow_id,
        agbrowse_contract=runtime.contract,
    )
    spec = RUNTIME.StageSpec("planner", "Planner", 0, 0, runtime.manifest["question"], tuple())

    first = runtime._stage_artifacts(spec, parent, evidence_map)
    second = runtime._stage_artifacts(spec, parent, evidence_map)

    assert second["context"] == first["context"]
    assert second["context"]["challenge_nonce"] == first["context"]["challenge_nonce"]
    assert second["manifest"] == first["manifest"]
    assert sha256(second["prompt_path"]) == second["context"]["prompt_sha256"]
    runtime.store.finalize_parent(
        parent["run_dir"],
        "PARENT_FAILED_CLOSED",
        failure={"code": "test-cleanup"},
    )


def test_all_stages_share_one_parent_workflow_app_attestation_scope(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    evidence_map = runtime._source_evidence_map()
    runtime.workflow_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME.write_immutable_json(runtime.evidence_map_path, evidence_map)
    parent = runtime.store.create_parent_workflow(
        project_root=runtime.manifest["project_root"],
        manifest_path=runtime.manifest_path,
        workflow_id=runtime.workflow_id,
        agbrowse_contract=runtime.contract,
    )
    specs = [
        RUNTIME.StageSpec("planner", "Planner", 0, 0, runtime.manifest["question"], tuple()),
        RUNTIME.StageSpec("solver-0", "Solver", 0, 0, "approach", tuple()),
        RUNTIME.StageSpec("iter-1-judge", "Judge", 0, 1, {"candidate_stage_ids": []}, tuple()),
        RUNTIME.StageSpec("organizer", "Organizer", 0, 2, {}, tuple()),
    ]

    scopes = {
        runtime._stage_artifacts(spec, parent, evidence_map)["manifest"]["app_attestation_scope"]
        for spec in specs
    }

    assert scopes == {"parent-workflow"}
    runtime.store.finalize_parent(
        parent["run_dir"],
        "PARENT_FAILED_CLOSED",
        failure={"code": "test-cleanup"},
    )


def test_failed_parent_reopens_only_after_all_children_are_complete_hashed_and_clean(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    evidence_map = runtime._source_evidence_map()
    runtime.workflow_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME.write_immutable_json(runtime.evidence_map_path, evidence_map)
    parent = runtime.store.create_parent_workflow(
        project_root=runtime.manifest["project_root"],
        manifest_path=runtime.manifest_path,
        workflow_id=runtime.workflow_id,
        agbrowse_contract=runtime.contract,
        owner_pid=999999,
    )
    spec = RUNTIME.StageSpec("planner", "Planner", 0, 0, runtime.manifest["question"], tuple())
    artifacts = runtime._stage_artifacts(spec, parent, evidence_map)
    child = runtime.store.create_child_run(
        parent_run_dir=parent["run_dir"],
        manifest_path=artifacts["manifest_path"],
        agbrowse_contract=runtime.contract,
        role=spec.role,
        lane=spec.lane,
        iteration=spec.iteration,
        stage_id=spec.stage_id,
        send_limit=1,
    )
    child_dir = Path(child["run_dir"])
    runtime.store.transition(child_dir, "PREFLIGHTED")
    runtime.store.transition(child_dir, "LEASED")
    runtime.store.claim_child_send(child_dir)
    runtime.store.transition(
        child_dir,
        "SUBMITTED",
        session_id="session-reopen",
        target_id="target-reopen",
        submission_receipt={"test": True},
    )
    runtime.store.transition(
        child_dir,
        "URL_BOUND",
        conversation_url="https://chatgpt.com/c/reopen",
    )
    answer_path = child_dir / "answer.md"
    answer_path.write_text("complete child answer\n", encoding="utf-8")
    descriptor = {
        "path": str(answer_path),
        "sha256": sha256(answer_path),
        "bytes": answer_path.stat().st_size,
        "provider_status": "complete",
    }
    runtime.store.transition(child_dir, "RESULT_CAPTURED", result=descriptor)
    runtime.store.transition(child_dir, "VERIFIED")
    runtime.store.transition(child_dir, "COMPLETE")
    runtime.store.record_child_cleanup(
        child_dir,
        {
            "ok": True,
            "state": "closed-and-absent",
            "target_id": "target-reopen",
            "conversation_url": "https://chatgpt.com/c/reopen",
        },
    )
    failed = runtime.store.finalize_parent(
        parent["run_dir"],
        "PARENT_FAILED_CLOSED",
        failure={"code": "CHILD_IDENTITY_INCOMPLETE", "message": "planner"},
    )
    assert failed["phase"] == "PARENT_FAILED_CLOSED"
    old_lease = failed["lease_nonce"]

    reopened = runtime.store.reopen_failed_parent_workflow(parent["run_dir"], runtime.manifest_path)

    assert reopened["phase"] == "PARENT_ACTIVE"
    assert reopened["lease_nonce"] == old_lease
    assert reopened["failure"] is None
    assert reopened["prior_failures"][-1]["failure"]["code"] == "CHILD_IDENTITY_INCOMPLETE"
    assert runtime.store.paths(Path(runtime.manifest["project_root"]), parent["run_id"]).lock_file.is_file()
    runtime.store.finalize_parent(
        parent["run_dir"],
        "PARENT_FAILED_CLOSED",
        failure={"code": "test-cleanup"},
    )


def test_failed_parent_reopens_exact_zero_send_parallel_app_composer_children(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    evidence_map = runtime._source_evidence_map()
    runtime.workflow_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME.write_immutable_json(runtime.evidence_map_path, evidence_map)
    parent = runtime.store.create_parent_workflow(
        project_root=runtime.manifest["project_root"],
        manifest_path=runtime.manifest_path,
        workflow_id=runtime.workflow_id,
        agbrowse_contract=runtime.contract,
        owner_pid=999999,
    )
    children = []
    for lane in range(3):
        spec = RUNTIME.StageSpec(f"iter-1-refiner-{lane}", "LoopRefiner", lane, 1, {}, tuple())
        artifacts = runtime._stage_artifacts(spec, parent, evidence_map)
        child = runtime.store.create_child_run(
            parent_run_dir=parent["run_dir"],
            manifest_path=artifacts["manifest_path"],
            agbrowse_contract=runtime.contract,
            role=spec.role,
            lane=spec.lane,
            iteration=spec.iteration,
            stage_id=spec.stage_id,
            send_limit=1,
        )
        runtime.store.transition(child["run_dir"], "PREFLIGHT_BLOCKED")
        children.append(child)
    failed = runtime.store.finalize_parent(
        parent["run_dir"],
        "PARENT_FAILED_CLOSED",
        failure={"code": "APP_COMPOSER_PREP_FAILED", "message": "exact app mention could not be confirmed"},
    )

    reopened = runtime.store.reopen_failed_parent_workflow(parent["run_dir"], runtime.manifest_path)

    assert failed["phase"] == "PARENT_FAILED_CLOSED"
    assert reopened["phase"] == "PARENT_ACTIVE"
    for child in children:
        _, preserved_child = runtime.store.load(child["run_dir"])
        assert preserved_child["phase"] == "PREFLIGHT_BLOCKED"
        assert preserved_child["send_attempt_count"] == 0
        assert preserved_child["session_id"] is None
        assert preserved_child["conversation_url"] is None
    runtime.store.finalize_parent(
        parent["run_dir"],
        "PARENT_FAILED_CLOSED",
        failure={"code": "test-cleanup"},
    )


def test_bad_app_read_proof_fails_closed_after_one_send(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(
        manifest,
        state_root=tmp_path / "state",
        stage_executor=FakeStageExecutor(2, bad_receipt_role="Planner"),
    )

    with pytest.raises(RUNTIME.WebMultiError) as failure:
        runtime.run()

    assert failure.value.code == "APP_READ_RECEIPTS_MISMATCH"
    parent = runtime.parent
    paths = runtime.store.paths(Path(runtime.manifest["project_root"]), parent["run_id"])
    children = runtime.store._parent_children(paths.runs_dir, parent["run_id"])
    assert len(children) == 1
    assert children[0][1]["send_attempt_count"] == 1


def test_provider_failure_supervisor_retries_once_with_fresh_workflow_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    instances = []

    class FakeRuntime:
        def __init__(self, manifest_path):
            self.manifest_path = Path(manifest_path)
            self.workflow_id = json.loads(self.manifest_path.read_text(encoding="utf-8"))["workflow_id"]
            self.parent = {"run_dir": str(tmp_path / f"parent-{len(instances)}")}
            instances.append(self)

        def run(self, *, resume_parent=None):
            if len(instances) == 1:
                raise RUNTIME.WebMultiError("CHILD_PROVIDER_FAILED_TERMINAL", "planner")
            return {
                "schema": RUNTIME.RESULT_SCHEMA,
                "workflow_id": self.workflow_id,
                "parent_run_dir": str(tmp_path / "completed-parent"),
                "result_path": str(tmp_path / "completed-result.json"),
                "result_sha256": "a" * 64,
            }

    monkeypatch.setattr(RUNTIME, "WebMultiRuntime", FakeRuntime)
    monkeypatch.setattr(
        RUNTIME,
        "_provider_retry_eligibility",
        lambda engine: {
            "parent_run_id": "failed-parent",
            "failed_children": [
                {
                    "run_id": "failed-child",
                    "stage_id": "planner",
                    "phase": "PROVIDER_FAILED_TERMINAL",
                    "send_attempt_count": 1,
                    "conversation_url": "https://chatgpt.com/c/failed",
                    "owned_tab_state": "closed-and-absent",
                    "cleanup_pending": False,
                    "owned_open_tabs": 0,
                }
            ],
        },
    )

    result = RUNTIME.run_with_provider_failure_retries(manifest)

    assert len(instances) == 2
    assert instances[0].workflow_id != instances[1].workflow_id
    assert instances[1].workflow_id.endswith("-provider-retry-1")
    assert len(result["provider_retry_chain"]) == 1
    retry_manifest = Path(result["provider_retry_chain"][0]["next_manifest"])
    retry_payload = json.loads(retry_manifest.read_text(encoding="utf-8"))
    assert retry_payload["retry_of_workflow_id"] == instances[0].workflow_id
    assert retry_payload["provider_failure_parent_run_id"] == "failed-parent"
    report = Path(result["provider_retry_report"])
    assert report.is_file()
    assert result["provider_retry_report_sha256"] == sha256(report)


def test_provider_failure_supervisor_never_retries_uncertain_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    calls = []

    class FakeRuntime:
        def __init__(self, manifest_path):
            self.workflow_id = "uncertain"
            calls.append(Path(manifest_path))

        def run(self, *, resume_parent=None):
            raise RUNTIME.WebMultiError("CHILD_NOT_COMPLETE", "planner: RECOVERY_REQUIRED")

    monkeypatch.setattr(RUNTIME, "WebMultiRuntime", FakeRuntime)
    monkeypatch.setattr(
        RUNTIME,
        "_provider_retry_eligibility",
        lambda _engine: pytest.fail("uncertain work must never enter provider-terminal retry"),
    )

    with pytest.raises(RUNTIME.WebMultiError) as error:
        RUNTIME.run_with_provider_failure_retries(manifest)

    assert error.value.code == "CHILD_NOT_COMPLETE"
    assert len(calls) == 1


def test_large_context_dry_run_has_no_attachment_or_size_rejection(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2, large=True)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state", stage_executor=FakeStageExecutor(2))

    result = runtime.dry_run()

    assert result["status"] == "dry-run"
    assert result["source_file_count"] == 1
    assert result["browser_started"] is False
    assert not (tmp_path / "state").exists()


def test_completed_tab_cleanup_precedes_invalid_envelope_failure(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    bridge = CleanupOnlyBridge(runtime.store)
    runtime.bridge_factory = lambda: bridge
    spec, child = make_completed_child(runtime, answer_text='{"bad":"C:\\Users\\broken"')

    with pytest.raises(RUNTIME.WebMultiError) as failure:
        runtime._actual_execute(child, spec)

    assert failure.value.code == "STAGE_ENVELOPE_INVALID_JSON"
    _, latest = runtime.store.load(child["run_dir"])
    assert latest["owned_tab_state"] == "closed-and-absent"
    assert latest["owned_open_tabs"] == 0
    assert bridge.cleanup_calls == [(child["run_dir"], True)]


def test_parent_recovery_cleans_complete_child_missing_cleanup_evidence(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    bridge = CleanupOnlyBridge(runtime.store)
    runtime.bridge_factory = lambda: bridge
    _, child = make_completed_child(runtime, answer_text='{"valid":true}')

    runtime._recover_parent_children(runtime.parent)

    _, latest = runtime.store.load(child["run_dir"])
    assert latest["owned_tab_state"] == "closed-and-absent"
    assert latest["cleanup_pending"] is False
    assert bridge.cleanup_calls == [(child["run_dir"], True)]


def test_actual_parallel_wave_shares_one_submission_barrier(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    observed_barriers: list[int] = []

    def fake_execute(spec, evidence_map, *, submission_barrier=None):
        assert submission_barrier is not None
        observed_barriers.append(id(submission_barrier))
        submission_barrier.wait(timeout=5)
        return spec

    runtime._execute_stage = fake_execute
    specs = [
        RUNTIME.StageSpec("solver-0", "Solver", 0, 0, "a", tuple()),
        RUNTIME.StageSpec("solver-1", "Solver", 1, 0, "b", tuple()),
    ]

    results = runtime._wave(specs, {})

    assert [item.stage_id for item in results] == ["solver-0", "solver-1"]
    assert len(observed_barriers) == 2
    assert len(set(observed_barriers)) == 1


def test_provider_capacity_chunks_six_lane_submission_barriers_without_deadlock(tmp_path: Path) -> None:
    runtime = RUNTIME.WebMultiRuntime(make_manifest(tmp_path, solver_count=2), state_root=tmp_path / "state")
    observed: dict[str, object | None] = {}

    def fake_execute(spec, evidence_map, *, submission_barrier=None):
        observed[spec.stage_id] = submission_barrier
        if submission_barrier is not None:
            submission_barrier.wait(timeout=5)
        return spec

    runtime._execute_stage = fake_execute
    specs = [
        RUNTIME.StageSpec(f"solver-{lane}", "Solver", lane, 0, f"approach-{lane}", tuple())
        for lane in range(6)
    ]

    results = runtime._wave(specs, {})

    assert [item.stage_id for item in results] == [item.stage_id for item in specs]
    first_chunk = [observed[f"solver-{lane}"] for lane in range(5)]
    assert all(barrier is first_chunk[0] for barrier in first_chunk)
    assert first_chunk[0] is not None
    assert first_chunk[0].parties == 5
    assert observed["solver-5"] is None


def test_paired_wave_never_exceeds_provider_parallel_limit(tmp_path: Path) -> None:
    runtime = RUNTIME.WebMultiRuntime(make_manifest(tmp_path, solver_count=2), state_root=tmp_path / "state")
    runtime.stage_executor = lambda *_args: None
    active = 0
    maximum = 0
    lock = threading.Lock()
    starts: list[str] = []

    def fake_execute(spec, evidence_map, *, submission_barrier=None):
        nonlocal active, maximum
        with lock:
            starts.append(spec.stage_id)
            active += 1
            maximum = max(maximum, active)
        try:
            threading.Event().wait(0.01)
        finally:
            with lock:
                active -= 1
        return RUNTIME.StageResult(spec, {}, tmp_path / f"{spec.stage_id}.json", {}, 0.0, 0.0)

    runtime._execute_stage = fake_execute
    solvers = [RUNTIME.StageSpec(f"solver-{lane}", "Solver", lane, 0, "approach", tuple()) for lane in range(10)]

    result = runtime._paired_wave(
        solvers,
        lambda lane, solver: RUNTIME.StageSpec(f"refiner-{lane}", "InitialRefiner", lane, 0, {}, tuple()),
        {},
    )

    assert [item.spec.stage_id for item in result] == [f"refiner-{lane}" for lane in range(10)]
    assert maximum == 5
    assert starts.index("refiner-0") < starts.index("solver-5")


def test_stage_artifacts_reuse_legacy_default_capacity_but_reject_nondefault(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    evidence_map = runtime._source_evidence_map()
    runtime.workflow_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME.write_immutable_json(runtime.evidence_map_path, evidence_map)
    parent = runtime.store.create_parent_workflow(
        project_root=runtime.manifest["project_root"],
        manifest_path=runtime.manifest_path,
        workflow_id=runtime.workflow_id,
        agbrowse_contract=runtime.contract,
    )
    spec = RUNTIME.StageSpec("planner", "Planner", 0, 0, runtime.manifest["question"], tuple())
    artifacts = runtime._stage_artifacts(spec, parent, evidence_map)
    for path_key in ("context_path", "manifest_path"):
        path = artifacts[path_key]
        value = json.loads(path.read_text(encoding="utf-8"))
        value.pop("provider_parallel_limit")
        path.write_text(json.dumps(value), encoding="utf-8")

    reused = runtime._stage_artifacts(spec, parent, evidence_map)
    assert "provider_parallel_limit" not in reused["context"]

    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value["provider_parallel_limit"] = 4
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
    changed = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    with pytest.raises(RUNTIME.WebMultiError) as failure:
        changed._stage_artifacts(spec, parent, evidence_map)
    assert failure.value.code == "STAGE_ARTIFACT_CONTEXT_MISMATCH"
    runtime.store.finalize_parent(
        parent["run_dir"],
        "PARENT_FAILED_CLOSED",
        failure={"code": "test-cleanup"},
    )

def test_transient_app_pre_send_failure_retries_same_child_five_bounded_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    evidence_map = runtime._source_evidence_map()
    runtime.workflow_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME.write_immutable_json(runtime.evidence_map_path, evidence_map)
    runtime.parent = runtime.store.create_parent_workflow(
        project_root=runtime.manifest["project_root"],
        manifest_path=runtime.manifest_path,
        workflow_id=runtime.workflow_id,
        agbrowse_contract=runtime.contract,
    )
    runtime.parent["run_dir"] = str(Path(runtime.parent["run_dir"]).resolve())
    spec = RUNTIME.StageSpec("planner", "Planner", 0, 0, runtime.manifest["question"], tuple())
    artifacts = runtime._stage_artifacts(spec, runtime.parent, evidence_map)
    child = runtime.store.create_child_run(
        parent_run_dir=runtime.parent["run_dir"],
        manifest_path=artifacts["manifest_path"],
        agbrowse_contract=runtime.contract,
        role=spec.role,
        lane=spec.lane,
        iteration=spec.iteration,
        stage_id=spec.stage_id,
        send_limit=1,
    )
    runtime.store.transition(child["run_dir"], "PREFLIGHTED")

    class TransientAppBridge:
        def __init__(self):
            self.send_calls = 0

        def send(self, run_dir):
            self.send_calls += 1
            _, current = runtime.store.load(run_dir)
            if self.send_calls == 1:
                current = runtime.store.transition(run_dir, "BLOCKED_APP_TRANSACTION")
            if self.send_calls <= 5:
                return current
            return {**current, "phase": "CANCELLED_PRE_SUBMISSION"}

    bridge = TransientAppBridge()
    runtime.bridge_factory = lambda: bridge
    monkeypatch.setattr(RUNTIME.time, "sleep", lambda _seconds: None)

    with pytest.raises(RUNTIME.WebMultiError) as failure:
        runtime._actual_execute(child, spec)

    assert failure.value.code == "CHILD_NOT_COMPLETE"
    assert bridge.send_calls == 6
    _, latest = runtime.store.load(child["run_dir"])
    assert latest["run_id"] == child["run_id"]
    assert latest["phase"] == "BLOCKED_APP_TRANSACTION"
    assert latest["send_attempt_count"] == 0
    assert latest["session_id"] is None
    assert latest["conversation_url"] is None


def test_app_composer_preparation_failure_never_opens_a_replacement_target(
    tmp_path: Path,
) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    evidence_map = runtime._source_evidence_map()
    runtime.workflow_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME.write_immutable_json(runtime.evidence_map_path, evidence_map)
    runtime.parent = runtime.store.create_parent_workflow(
        project_root=runtime.manifest["project_root"],
        manifest_path=runtime.manifest_path,
        workflow_id=runtime.workflow_id,
        agbrowse_contract=runtime.contract,
    )
    runtime.parent["run_dir"] = str(Path(runtime.parent["run_dir"]).resolve())
    spec = RUNTIME.StageSpec("planner", "Planner", 0, 0, runtime.manifest["question"], tuple())
    artifacts = runtime._stage_artifacts(spec, runtime.parent, evidence_map)
    child = runtime.store.create_child_run(
        parent_run_dir=runtime.parent["run_dir"],
        manifest_path=artifacts["manifest_path"],
        agbrowse_contract=runtime.contract,
        role=spec.role,
        lane=spec.lane,
        iteration=spec.iteration,
        stage_id=spec.stage_id,
        send_limit=1,
    )
    runtime.store.transition(child["run_dir"], "PREFLIGHTED")

    class ComposerFailureBridge:
        def __init__(self):
            self.send_calls = 0

        def send(self, run_dir):
            self.send_calls += 1
            return runtime.store.transition(
                run_dir,
                "BLOCKED_APP_TRANSACTION",
                recovery_event={
                    "kind": "app-composer-preparation-failed",
                    "cleanup": {"ok": True, "state": "closed-and-absent"},
                },
            )

    bridge = ComposerFailureBridge()
    runtime.bridge_factory = lambda: bridge

    with pytest.raises(RUNTIME.WebMultiError) as failure:
        runtime._actual_execute(child, spec)

    assert failure.value.code == "CHILD_NOT_COMPLETE"
    assert bridge.send_calls == 1
    _, latest = runtime.store.load(child["run_dir"])
    assert latest["send_attempt_count"] == 0
    assert latest["session_id"] is None
    assert latest["conversation_url"] is None


def test_resumed_wave_does_not_make_one_unfinished_stage_wait_for_cached_lanes(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=4)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    observed: dict[str, object | None] = {}

    def fake_execute(spec, evidence_map, *, submission_barrier=None):
        observed[spec.stage_id] = submission_barrier
        return spec

    runtime._execute_stage = fake_execute
    runtime._stage_needs_wave_submission = lambda spec: spec.stage_id == "solver-3"
    specs = [
        RUNTIME.StageSpec(f"solver-{lane}", "Solver", lane, 0, f"approach-{lane}", tuple())
        for lane in range(4)
    ]

    results = runtime._wave(specs, {})

    assert [item.stage_id for item in results] == [f"solver-{lane}" for lane in range(4)]
    assert observed == {f"solver-{lane}": None for lane in range(4)}


def test_resumed_wave_barrier_contains_only_two_stages_that_can_submit(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=4)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    observed: dict[str, object | None] = {}

    def fake_execute(spec, evidence_map, *, submission_barrier=None):
        observed[spec.stage_id] = submission_barrier
        if submission_barrier is not None:
            submission_barrier.wait(timeout=5)
        return spec

    runtime._execute_stage = fake_execute
    runtime._stage_needs_wave_submission = lambda spec: spec.stage_id in {"solver-2", "solver-3"}
    specs = [
        RUNTIME.StageSpec(f"solver-{lane}", "Solver", lane, 0, f"approach-{lane}", tuple())
        for lane in range(4)
    ]

    runtime._wave(specs, {})

    assert observed["solver-0"] is None
    assert observed["solver-1"] is None
    assert observed["solver-2"] is observed["solver-3"]
    assert observed["solver-2"] is not None
    assert observed["solver-2"].parties == 2


def test_json_transport_repairs_only_invalid_backslash_escapes(tmp_path: Path) -> None:
    evidence = tmp_path / "json-transport-repair.json"
    raw = r'{"schema":"x","payload":{"regex":"^https://chatgpt\.com/c/$"}}'

    value = RUNTIME.parse_json_envelope(raw, repair_evidence_path=evidence)

    assert value["payload"]["regex"] == r"^https://chatgpt\.com/c/$"
    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert record["repair_kind"] == "invalid-backslash-escape-only"
    assert record["repair_count"] == 1
    assert len(record["original_offsets"]) == 1
    assert record["original_sha256"] != record["repaired_sha256"]


def test_json_transport_never_repairs_structural_invalidity(tmp_path: Path) -> None:
    evidence = tmp_path / "json-transport-repair.json"

    with pytest.raises(RUNTIME.WebMultiError) as failure:
        RUNTIME.parse_json_envelope(r'{"payload":{"regex":"x\q"}', repair_evidence_path=evidence)

    assert failure.value.code == "STAGE_ENVELOPE_INVALID_JSON"
    assert not evidence.exists()


def test_tagged_transport_keeps_long_payload_out_of_json(tmp_path: Path) -> None:
    spec = RUNTIME.StageSpec("planner", "Planner", 0, 0, "question", tuple())
    header = {
        "schema": RUNTIME.STAGE_SCHEMA,
        "workflow_id": "wf-tagged",
        "parent_run_id": "parent-tagged",
        "stage_id": "planner",
        "role": "Planner",
        "lane": 0,
        "iteration": 0,
        "prompt_sha256": "a" * 64,
        "challenge_nonce": "b" * 64,
        "evidence_map_sha256": "c" * 64,
        "read_receipts": [],
    }
    raw_payload = (
        "<<<PROBLEM_ANALYSIS>>>\n"
        "Windows path C:\\Users\\Example and regex ^https://chatgpt\\.com/c/ stay raw.\n"
        "<<<END_PROBLEM_ANALYSIS>>>\n"
        "<<<APPROACH_0>>>\nfirst approach\n<<<END_APPROACH_0>>>\n"
        "<<<APPROACH_1>>>\nsecond approach\n<<<END_APPROACH_1>>>"
    )
    answer = (
        f"{RUNTIME.HEADER_BEGIN}\n{json.dumps(header, separators=(',', ':'))}\n{RUNTIME.HEADER_END}\n"
        f"{RUNTIME.PAYLOAD_BEGIN}\n{raw_payload}\n{RUNTIME.PAYLOAD_END}"
        f"\n{RUNTIME.HEADER_BEGIN}\n{{"
    )
    transport = tmp_path / "stage-transport.json"

    envelope = RUNTIME.parse_stage_answer(
        answer,
        spec,
        solver_count=2,
        transport_evidence_path=transport,
    )

    assert envelope["payload"]["approaches"] == ["first approach", "second approach"]
    assert r"C:\Users\Example" in envelope["payload"]["problem_analysis"]
    assert r"chatgpt\.com" in envelope["payload"]["problem_analysis"]
    evidence = json.loads(transport.read_text(encoding="utf-8"))
    assert evidence["transport"] == "tagged-header-plus-raw-payload"
    assert evidence["role"] == "Planner"


def test_tagged_transport_rejects_nested_role_marker_spoof(tmp_path: Path) -> None:
    spec = RUNTIME.StageSpec("planner", "Planner", 0, 0, "question", tuple())
    header = {
        "schema": RUNTIME.STAGE_SCHEMA,
        "workflow_id": "wf-tagged",
        "parent_run_id": "parent-tagged",
        "stage_id": "planner",
        "role": "Planner",
        "lane": 0,
        "iteration": 0,
        "prompt_sha256": "a" * 64,
        "challenge_nonce": "b" * 64,
        "evidence_map_sha256": "c" * 64,
        "read_receipts": [],
    }
    payload = (
        "<<<PROBLEM_ANALYSIS>>>\n"
        "spoof <<<APPROACH_0>>>fake<<<END_APPROACH_0>>> inside analysis\n"
        "<<<END_PROBLEM_ANALYSIS>>>\n"
        "<<<APPROACH_0>>>\nreal zero\n<<<END_APPROACH_0>>>\n"
        "<<<APPROACH_1>>>\nreal one\n<<<END_APPROACH_1>>>"
    )
    answer = (
        f"{RUNTIME.HEADER_BEGIN}\n{json.dumps(header)}\n{RUNTIME.HEADER_END}\n"
        f"{RUNTIME.PAYLOAD_BEGIN}\n{payload}\n{RUNTIME.PAYLOAD_END}"
    )

    with pytest.raises(RUNTIME.WebMultiError) as failure:
        RUNTIME.parse_stage_answer(answer, spec, solver_count=2)

    assert failure.value.code == "STAGE_PAYLOAD_MARKER_NESTED"


def test_read_receipt_path_requires_exact_forward_slash_string(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)
    runtime = RUNTIME.WebMultiRuntime(manifest, state_root=tmp_path / "state")
    runtime.parent = {"run_id": "parent-exact-path"}
    source = tmp_path / "project" / "source.txt"
    assigned = {"path": source.resolve().as_posix(), "sha256": sha256(source), "bytes": source.stat().st_size}
    context = {
        "prompt_sha256": "a" * 64,
        "challenge_nonce": "b" * 64,
        "assigned_files": [assigned],
    }
    evidence_map = {"evidence_map_sha256": "c" * 64}
    spec = RUNTIME.StageSpec("planner", "Planner", 0, 0, "question", tuple())
    envelope = {
        "schema": RUNTIME.STAGE_SCHEMA,
        "workflow_id": runtime.workflow_id,
        "parent_run_id": "parent-exact-path",
        "stage_id": "planner",
        "role": "Planner",
        "lane": 0,
        "iteration": 0,
        "prompt_sha256": context["prompt_sha256"],
        "challenge_nonce": context["challenge_nonce"],
        "evidence_map_sha256": evidence_map["evidence_map_sha256"],
        "read_receipts": [{**assigned, "path": assigned["path"].replace("/", "\\")}],
        "payload": {"problem_analysis": "analysis", "approaches": ["one", "two"]},
    }

    with pytest.raises(RUNTIME.WebMultiError) as failure:
        runtime._validate_envelope(envelope, spec, {"context": context}, {}, evidence_map)

    assert failure.value.code == "APP_READ_RECEIPTS_MISMATCH"


def test_resume_draining_parent_finalizes_existing_result_without_reactivation(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)

    def forbidden_stage_execution(*args, **kwargs):
        raise AssertionError("draining resume must not execute or create a stage")

    runtime = RUNTIME.WebMultiRuntime(
        manifest,
        state_root=tmp_path / "state",
        stage_executor=forbidden_stage_execution,
    )
    evidence_map = runtime._source_evidence_map()
    parent = runtime.store.create_parent_workflow(
        project_root=runtime.manifest["project_root"],
        manifest_path=runtime.manifest_path,
        workflow_id=runtime.workflow_id,
        agbrowse_contract=runtime.contract,
    )
    result = {
        "schema": RUNTIME.RESULT_SCHEMA,
        "workflow_id": runtime.workflow_id,
        "organizer_result": "already organized",
        "provenance": [],
        "evidence_map_sha256": evidence_map["evidence_map_sha256"],
        "max_concurrent_child_generations": 2,
        "advisory_only": True,
        "mode_variant": runtime.manifest["mode_variant"],
    }
    RUNTIME.write_immutable_json(runtime.result_path, result)
    state_file, parent_record = runtime.store.load(parent["run_dir"])
    paths = runtime.store.paths(Path(runtime.manifest["project_root"]), parent["run_id"])
    lock = RUNTIME.STATE.read_json(paths.lock_file)
    parent_record["phase_events"].append({
        "from": "PARENT_ACTIVE",
        "to": "PARENT_DRAINING",
        "at": RUNTIME.STATE.utc_now(),
    })
    parent_record["phase"] = "PARENT_DRAINING"
    lock["phase"] = "PARENT_DRAINING"
    RUNTIME.STATE.write_json_atomic(state_file, parent_record)
    RUNTIME.STATE.write_json_atomic(paths.lock_file, lock)

    resumed = runtime.run(resume_parent=Path(parent["run_dir"]))

    assert resumed["organizer_result"] == "already organized"
    assert resumed["result_sha256"] == sha256(runtime.result_path)
    _, latest = runtime.store.load(parent["run_dir"])
    assert latest["phase"] == "PARENT_COMPLETE"
    drain_index = next(
        index
        for index, event in enumerate(latest["phase_events"])
        if event.get("to") == "PARENT_DRAINING"
    )
    assert not any(
        event.get("to") == "PARENT_ACTIVE"
        for event in latest["phase_events"][drain_index + 1:]
    )


def test_resume_active_runtime_recovery_continues_same_parent_without_drain_reactivation(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2, max_iterations=1)
    runtime = RUNTIME.WebMultiRuntime(
        manifest,
        state_root=tmp_path / "state",
        stage_executor=FakeStageExecutor(2),
    )
    evidence_map = runtime._source_evidence_map()
    runtime.workflow_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME.write_immutable_json(runtime.evidence_map_path, evidence_map)
    parent = runtime.store.create_parent_workflow(
        project_root=runtime.manifest["project_root"],
        manifest_path=runtime.manifest_path,
        workflow_id=runtime.workflow_id,
        agbrowse_contract=runtime.contract,
    )
    spec = RUNTIME.StageSpec("planner", "Planner", 0, 0, runtime.manifest["question"], tuple())
    artifacts = runtime._stage_artifacts(spec, parent, evidence_map)
    child = runtime.store.create_child_run(
        parent_run_dir=parent["run_dir"],
        manifest_path=artifacts["manifest_path"],
        agbrowse_contract=runtime.contract,
        role=spec.role,
        lane=spec.lane,
        iteration=spec.iteration,
        stage_id=spec.stage_id,
    )
    runtime.store.transition(child["run_dir"], "PREFLIGHTED")
    runtime.store.mark_parent_runtime_recovery(
        parent["run_dir"],
        failure={"code": "simulated-interruption"},
    )

    result = runtime.run(resume_parent=Path(parent["run_dir"]))

    assert result["parent_run_dir"] == str(Path(parent["run_dir"]).resolve())
    _, latest = runtime.store.load(parent["run_dir"])
    assert latest["phase"] == "PARENT_COMPLETE"
    assert latest["recovery_required"] is False
    assert any(event.get("kind") == "runtime-recovery-cleared" for event in latest["runtime_recovery_events"])
    assert not any(
        event.get("from") in {"PARENT_DRAINING", "PARENT_RECOVERY_REQUIRED"}
        and event.get("to") == "PARENT_ACTIVE"
        for event in latest["phase_events"]
    )


def test_completed_exact_capture_can_derive_tagged_answer_from_snapshot(tmp_path: Path) -> None:
    answer = (
        "<<<WEB_MULTI_HEADER_V1>>>\n"
        '{"stage_id":"solver-1"}\n'
        "<<<END_WEB_MULTI_HEADER_V1>>>\n"
        "<<<WEB_MULTI_PAYLOAD_V1>>>\n"
        "<<<CONTENT>>>\nwide answer\n<<<END_CONTENT>>>\n"
        "<<<END_WEB_MULTI_PAYLOAD_V1>>>"
    )
    snapshot = tmp_path / "exact-url-terminal-snapshot.stdout.txt"
    snapshot.write_text(
        json.dumps({"text": f'- text: {json.dumps(answer)}'}),
        encoding="utf-8",
    )
    result = {
        "evidence": {
            "snapshot": {
                "stdout": str(snapshot),
                "stdout_sha256": RUNTIME.sha256_file(snapshot),
            }
        }
    }

    assert RUNTIME.normalized_stage_answer_from_result(result) == answer


def test_resume_resultless_recovery_required_parent_fails_closed_and_releases_lock(tmp_path: Path) -> None:
    manifest = make_manifest(tmp_path, solver_count=2)

    def forbidden_stage_execution(*args, **kwargs):
        raise AssertionError("resultless draining recovery must not execute or create a stage")

    runtime = RUNTIME.WebMultiRuntime(
        manifest,
        state_root=tmp_path / "state",
        stage_executor=forbidden_stage_execution,
    )
    evidence_map = runtime._source_evidence_map()
    runtime.workflow_dir.mkdir(parents=True, exist_ok=True)
    RUNTIME.write_immutable_json(runtime.evidence_map_path, evidence_map)
    parent = runtime.store.create_parent_workflow(
        project_root=runtime.manifest["project_root"],
        manifest_path=runtime.manifest_path,
        workflow_id=runtime.workflow_id,
        agbrowse_contract=runtime.contract,
    )
    spec = RUNTIME.StageSpec("planner", "Planner", 0, 0, runtime.manifest["question"], tuple())
    artifacts = runtime._stage_artifacts(spec, parent, evidence_map)
    child = runtime.store.create_child_run(
        parent_run_dir=parent["run_dir"],
        manifest_path=artifacts["manifest_path"],
        agbrowse_contract=runtime.contract,
        role=spec.role,
        lane=spec.lane,
        iteration=spec.iteration,
        stage_id=spec.stage_id,
    )
    draining = runtime.store.finalize_parent(
        parent["run_dir"],
        "PARENT_FAILED_CLOSED",
        failure={"code": "simulated-interruption"},
    )
    assert draining["phase"] == "PARENT_RECOVERY_REQUIRED"

    with pytest.raises(RUNTIME.WebMultiError) as failure:
        runtime.run(resume_parent=Path(parent["run_dir"]))

    assert failure.value.code == "PARENT_DRAIN_RESULT_MISSING"
    assert failure.value.evidence["project_lock_released"] is True
    _, latest = runtime.store.load(parent["run_dir"])
    assert latest["phase"] == "PARENT_FAILED_CLOSED"
    assert latest["failure"]["code"] == "PARENT_DRAIN_RESULT_MISSING"
    paths = runtime.store.paths(Path(runtime.manifest["project_root"]), parent["run_id"])
    assert not paths.lock_file.exists()
    assert not any(
        event.get("from") in {"PARENT_DRAINING", "PARENT_RECOVERY_REQUIRED"}
        and event.get("to") == "PARENT_ACTIVE"
        for event in latest["phase_events"]
    )
