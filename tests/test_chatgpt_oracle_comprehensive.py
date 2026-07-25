from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_comprehensive.py"


def load():
    spec = importlib.util.spec_from_file_location("oracle_comprehensive_test", PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def manifest(tmp_path: Path) -> Path:
    os.environ["CODEX_ORACLE_STATE_ROOT"] = str((tmp_path.parent / f"{tmp_path.name}-host-state").resolve())
    mission = tmp_path / "initial.md"
    mission.write_text("Plan the work broadly.", encoding="utf-8")
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps({
        "schema": "codex.chatgpt.oracle-comprehensive/v1",
        "workflow_id": "a" * 32,
        "project_root": str(tmp_path.resolve()),
        "workflow_dir": str((tmp_path / "workflow").resolve()),
        "initial_mission_path": str(mission.resolve()),
        "app_name": "DevSpace",
        "model": "gpt-5.6",
        "local_gate_command": ["python", "-c", "raise SystemExit(0)"],
    }), encoding="utf-8")
    return path


def test_web_authored_relay_reaches_complete_without_host_semantic_rewrite(tmp_path: Path) -> None:
    module = load()
    order = ["plan", "review", "implementation", "final-web-gate"]
    seen = []

    def fake_execute(path: Path, *, dry_run: bool):
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model_strategy"] == "select"
        assert config["thinking_time"] == "heavy"
        mission = Path(config["mission_path"])
        text = mission.read_text(encoding="utf-8")
        stage = next(item for item in order if f"stage={item}\n" in text)
        attempt_id = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("attempt_id="))
        input_sha = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("input_mission_sha256="))
        seen.append(stage)
        stage_dir = mission.parent
        output = stage_dir / "web-output.md"
        output.write_text(f"{stage} output", encoding="utf-8")
        next_stage = order[order.index(stage) + 1] if stage != order[-1] else "complete"
        next_mission = tmp_path / f"next-{stage}.md"
        next_mission.write_text(f"web-authored mission after {stage}", encoding="utf-8")
        receipt = {
            "schema": "codex.chatgpt.oracle-stage-result/v1",
            "workflow_id": "a" * 32,
            "stage": stage,
            "attempt_id": attempt_id,
            "input_mission_sha256": input_sha,
            "status": "PASS",
            "output_path": str(output),
            "output_sha256": module.sha(output),
            "next_stage": next_stage,
            "next_mission_path": str(next_mission),
            "next_mission_sha256": module.sha(next_mission),
            "ready_for_next": True,
            "blocker": "",
        }
        (stage_dir / "stage-result.json").write_text(json.dumps(receipt), encoding="utf-8")
        run_dir = stage_dir / "run"
        run_dir.mkdir()
        return {"ok": True, "run_dir": str(run_dir)}

    result = module.run_workflow(
        manifest(tmp_path),
        oracle_execute=fake_execute,
        local_gate_runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "gate ok", ""),
    )
    assert result["ok"] is True
    assert seen == order
    assert result["status"] == "complete"


def test_missing_receipt_fails_closed_without_duplicate_stage(tmp_path: Path) -> None:
    module = load()
    calls = 0

    def fake_execute(path: Path, *, dry_run: bool):
        nonlocal calls
        calls += 1
        return {"ok": True, "run_dir": str(tmp_path / "run")}

    result = module.run_workflow(manifest(tmp_path), oracle_execute=fake_execute)
    assert result["ok"] is False
    assert result["status"] == "awaiting_receipt"
    assert calls == 1


def test_failing_receipt_cannot_complete(tmp_path: Path) -> None:
    module = load()

    def fake_execute(path: Path, *, dry_run: bool):
        config = json.loads(path.read_text(encoding="utf-8"))
        mission = Path(config["mission_path"])
        text = mission.read_text(encoding="utf-8")
        attempt_id = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("attempt_id="))
        input_sha = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("input_mission_sha256="))
        output = mission.parent / "output.md"
        output.write_text("bad", encoding="utf-8")
        (mission.parent / "stage-result.json").write_text(json.dumps({
            "schema": "codex.chatgpt.oracle-stage-result/v1",
            "workflow_id": "a" * 32,
            "stage": "plan",
            "attempt_id": attempt_id,
            "input_mission_sha256": input_sha,
            "status": "FAIL",
            "output_path": str(output),
            "output_sha256": module.sha(output),
            "next_stage": "review",
            "next_mission_path": str(tmp_path / "none.md"),
            "next_mission_sha256": "0" * 64,
            "ready_for_next": False,
            "blocker": "not ready",
        }), encoding="utf-8")
        return {"ok": True, "run_dir": str(mission.parent / "run")}

    try:
        module.run_workflow(manifest(tmp_path), oracle_execute=fake_execute)
    except module.WorkflowError as exc:
        assert "did not pass" in str(exc)
    else:
        raise AssertionError("FAIL receipt must not advance")


def test_web_multi_branch_is_bound_and_resumes_at_review(tmp_path: Path) -> None:
    module = load()
    workflow_path = manifest(tmp_path)
    lane_one = tmp_path / "lane-one.md"
    lane_two = tmp_path / "lane-two.md"
    merger = tmp_path / "merger.md"
    for path, body in ((lane_one, "one"), (lane_two, "two"), (merger, "merge")):
        path.write_text(body, encoding="utf-8")
    multi_manifest = tmp_path / "multi.json"
    multi_manifest.write_text(json.dumps({
        "schema": module.MULTI.SCHEMA,
        "project_root": str(tmp_path),
        "output_dir": str(tmp_path / "multi-output"),
        "solvers": [
            {"id": "one", "mission_path": str(lane_one)},
            {"id": "two", "mission_path": str(lane_two)},
        ],
        "merger_mission_path": str(merger),
        "next_stage_result_path": str(tmp_path / "multi-next-receipt.json"),
        "next_stage_binding": {"workflow_id": "a" * 32, "stage": "web-multi"},
    }), encoding="utf-8")
    review_mission = tmp_path / "review-after-multi.md"
    review_mission.write_text("review merged advice", encoding="utf-8")
    stages_seen = []

    def write_receipt(mission: Path, stage: str, next_stage: str, next_path: Path) -> None:
        text = mission.read_text(encoding="utf-8")
        attempt = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("attempt_id="))
        input_sha = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("input_mission_sha256="))
        output = mission.parent / "output.md"
        output.write_text(stage, encoding="utf-8")
        (mission.parent / "stage-result.json").write_text(json.dumps({
            "schema": "codex.chatgpt.oracle-stage-result/v1",
            "workflow_id": "a" * 32,
            "stage": stage,
            "attempt_id": attempt,
            "input_mission_sha256": input_sha,
            "status": "PASS",
            "output_path": str(output),
            "output_sha256": module.sha(output),
            "next_stage": next_stage,
            "next_mission_path": str(next_path),
            "next_mission_sha256": module.sha(next_path),
            "ready_for_next": True,
            "blocker": "",
        }), encoding="utf-8")

    def fake_oracle(path: Path, *, dry_run: bool):
        value = json.loads(path.read_text(encoding="utf-8"))
        mission = Path(value["mission_path"])
        stage = next(item for item in ("plan", "review", "implementation", "final-web-gate") if f"stage={item}\n" in mission.read_text(encoding="utf-8"))
        stages_seen.append(stage)
        if stage == "plan":
            write_receipt(mission, stage, "web-multi", multi_manifest)
        else:
            next_stage = {"review": "implementation", "implementation": "final-web-gate", "final-web-gate": "complete"}[stage]
            next_path = tmp_path / f"next-{stage}.md"
            next_path.write_text(f"after {stage}", encoding="utf-8")
            write_receipt(mission, stage, next_stage, next_path)
        return {"ok": True, "run_dir": str(mission.parent / "run")}

    def fake_multi(path: Path, *, dry_run: bool, parent_lock_held: bool):
        assert parent_lock_held is True
        workflow_config = module.load_manifest(workflow_path)
        stored = module._json(module._state_path(workflow_config, "a" * 32))
        assert stored["multi_execution_id"]
        assert stored["multi_manifest_sha256"] == module.sha(multi_manifest)
        assert Path(stored["multi_result_path"]).name == "result.json"
        receipt = tmp_path / "multi-result.json"
        output = tmp_path / "multi-output.md"
        output.write_text("merged", encoding="utf-8")
        receipt.write_text(json.dumps({
            "schema": "codex.chatgpt.oracle-stage-result/v1",
            "workflow_id": "a" * 32,
            "stage": "web-multi",
            "attempt_id": "b" * 64,
            "input_mission_sha256": module.sha(multi_manifest),
            "status": "PASS",
            "output_path": str(output),
            "output_sha256": module.sha(output),
            "next_stage": "review",
            "next_mission_path": str(review_mission),
            "next_mission_sha256": module.sha(review_mission),
            "ready_for_next": True,
            "blocker": "",
        }), encoding="utf-8")
        return {"ok": True, "parent_id": "b" * 64, "next_stage_result_path": str(receipt)}

    result = module.run_workflow(
        workflow_path,
        oracle_execute=fake_oracle,
        multi_execute=fake_multi,
        local_gate_runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    assert result["ok"] is True
    assert stages_seen == ["plan", "review", "implementation", "final-web-gate"]


def test_dry_run_leaves_no_host_workflow_state_and_real_run_can_follow(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    previews = []

    def fake_preview(oracle_manifest: Path, *, dry_run: bool):
        previews.append(dry_run)
        return {"ok": True, "status": "dry-run"}

    preview = module.run_workflow(path, dry_run=True, oracle_execute=fake_preview)
    assert preview["ok"] is True
    assert previews == [True]
    config = module.load_manifest(path)
    assert not module._state_path(config, "a" * 32).exists()

    calls = 0

    def fake_real(oracle_manifest: Path, *, dry_run: bool):
        nonlocal calls
        calls += 1
        return {"ok": True, "run_dir": str(tmp_path / "fake-run")}

    real = module.run_workflow(path, oracle_execute=fake_real)
    assert real["status"] == "awaiting_receipt"
    assert calls == 1


def _oracle_running_state(module, oracle_manifest: Path) -> Path:
    config = module.RUNNER.STATE.load_manifest(oracle_manifest)
    layout = module.RUNNER.STATE.create_layout(config, run_id=config.requested_run_id)
    layout.run_dir.mkdir(parents=True)
    module.RUNNER.STATE.write_json_atomic(
        layout.state_path,
        module.RUNNER.STATE.state_payload(config, layout, status="running", resolved_version="test"),
    )
    return layout.run_dir


def test_running_oracle_stage_recovers_exact_run_without_resubmission(tmp_path: Path) -> None:
    module = load()
    submitted = 0
    recovered = []

    def fake_execute(oracle_manifest: Path, *, dry_run: bool):
        nonlocal submitted
        submitted += 1
        return {"ok": False, "run_dir": str(_oracle_running_state(module, oracle_manifest))}

    def fake_recover(run_dir: Path, *, action: str, dry_run: bool):
        recovered.append((run_dir, action, dry_run))
        return {"ok": True, "status": "complete", "run_dir": str(run_dir)}

    path = manifest(tmp_path)
    first = module.run_workflow(path, oracle_execute=fake_execute, oracle_recover=fake_recover)
    assert first["status"] == "attention_required"
    assert first["oracle_run_id"] == first["current_attempt_id"]
    second = module.run_workflow(path, oracle_execute=fake_execute, oracle_recover=fake_recover)
    assert second["status"] == "awaiting_receipt"
    assert submitted == 1
    assert len(recovered) == 1
    assert recovered[0][1:] == ("harvest", False)
    assert second["recovery"]["status"] == "recovered"


def test_awaiting_receipt_rebind_advances_to_next_stage_without_replaying_plan(tmp_path: Path) -> None:
    module = load()
    calls = []
    review = tmp_path / "review.md"
    review.write_text("review", encoding="utf-8")

    def fake_execute(oracle_manifest: Path, *, dry_run: bool):
        config = json.loads(oracle_manifest.read_text(encoding="utf-8"))
        mission = Path(config["mission_path"])
        text = mission.read_text(encoding="utf-8")
        stage = "plan" if "stage=plan\n" in text else "review"
        calls.append(stage)
        run_dir = _oracle_running_state(module, oracle_manifest)
        if stage == "plan":
            attempt = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("attempt_id="))
            input_sha = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("input_mission_sha256="))
            output = mission.parent / "out.md"
            output.write_text("plan", encoding="utf-8")
            (mission.parent / "stage-result.json").write_text(json.dumps({
                "schema": "codex.chatgpt.oracle-stage-result/v1", "workflow_id": "a" * 32,
                "stage": "plan", "attempt_id": attempt, "input_mission_sha256": input_sha,
                "status": "PASS", "output_path": str(output), "output_sha256": module.sha(output),
                "next_stage": "review", "next_mission_path": str(review), "next_mission_sha256": module.sha(review),
                "ready_for_next": True, "blocker": "",
            }), encoding="utf-8")
            return {"ok": True, "run_dir": str(run_dir)}
        return {"ok": False, "run_dir": str(run_dir)}

    result = module.run_workflow(manifest(tmp_path), oracle_execute=fake_execute)
    assert result["status"] == "attention_required"
    assert result["current_stage"] == "review"
    assert calls == ["plan", "review"]
    assert result["next_index"] == 1


def test_running_web_multi_rebinds_only_persisted_parent_result(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    config = module.load_manifest(path)
    multi_source = tmp_path / "multi.json"
    multi_source.write_text("{}", encoding="utf-8")
    review = tmp_path / "review.md"
    review.write_text("review", encoding="utf-8")
    output = tmp_path / "multi-output.md"
    output.write_text("merged", encoding="utf-8")
    receipt = tmp_path / "multi-receipt.json"
    parent_id = "b" * 64
    receipt.write_text(json.dumps({
        "schema": "codex.chatgpt.oracle-stage-result/v1", "workflow_id": "a" * 32,
        "stage": "web-multi", "attempt_id": parent_id, "input_mission_sha256": module.sha(multi_source),
        "status": "PASS", "output_path": str(output), "output_sha256": module.sha(output),
        "next_stage": "review", "next_mission_path": str(review), "next_mission_sha256": module.sha(review),
        "ready_for_next": True, "blocker": "",
    }), encoding="utf-8")
    result_path = tmp_path / "multi-result.json"
    result_path.write_text(json.dumps({
        "schema": module.MULTI.RESULT_SCHEMA, "status": "complete", "parent_id": parent_id,
        "next_stage_result_path": str(receipt),
    }), encoding="utf-8")
    state_path = module._state_path(config, "a" * 32)
    module._write(state_path, {
        "schema": module.STATE_SCHEMA, "status": "running", "workflow_id": "a" * 32,
        "manifest_sha256": config["manifest_sha256"], "current_stage": "web-multi",
        "current_mission_path": str(multi_source), "next_index": 0, "records": [],
        "multi_execution_id": "c" * 64, "multi_manifest_sha256": module.sha(multi_source),
        "multi_result_path": str(result_path), "multi_receipt_path": str(receipt),
    })
    calls = 0

    def fake_oracle(oracle_manifest: Path, *, dry_run: bool):
        nonlocal calls
        calls += 1
        return {"ok": False, "run_dir": str(_oracle_running_state(module, oracle_manifest))}

    def never_multi(*args, **kwargs):
        raise AssertionError("stored Web Multi result must be rebound, not resubmitted")

    result = module.run_workflow(path, oracle_execute=fake_oracle, multi_execute=never_multi)
    assert result["status"] == "attention_required"
    assert result["current_stage"] == "review"
    assert calls == 1
    assert result["records"][0]["parent_id"] == parent_id


def test_default_recovery_uses_the_persisted_parallel_child_mutex(monkeypatch, tmp_path: Path) -> None:
    module = load()
    calls = []
    run_dir = tmp_path / "exact-run"
    run_dir.mkdir()
    (run_dir / "state.json").write_text(
        json.dumps({"schema": module.RUNNER.STATE.STATE_SCHEMA, "parallel_parent_id": "a" * 64}),
        encoding="utf-8",
    )

    def fake_recover(run_dir: Path, *, action: str, dry_run: bool):
        calls.append((run_dir, action, dry_run))
        return {"ok": True}

    monkeypatch.setattr(module.RUNNER, "recover_run", fake_recover)
    value = module._recover_oracle_under_workflow_mutex(run_dir, action="harvest", dry_run=False)
    assert value["ok"] is True
    assert calls == [(run_dir.resolve(), "harvest", False)]


def test_default_recovery_rejects_a_nonparallel_child(tmp_path: Path) -> None:
    module = load()
    run_dir = tmp_path / "exact-run"
    run_dir.mkdir()
    (run_dir / "state.json").write_text(
        json.dumps({"schema": module.RUNNER.STATE.STATE_SCHEMA, "run_id": "x"}),
        encoding="utf-8",
    )
    value = module._recover_oracle_under_workflow_mutex(run_dir, action="harvest", dry_run=False)
    assert value["ok"] is False
    assert value["error"] == "ORACLE_RECOVERY_PARALLEL_PARENT_MISSING"


def test_web_multi_preflight_failure_stays_prepared_and_rejects_changed_mission(tmp_path: Path) -> None:
    module = load()
    workflow_path = manifest(tmp_path)
    invalid_multi = tmp_path / "multi.json"
    invalid_multi.write_text(json.dumps({"next_stage_binding": {"workflow_id": "wrong", "stage": "web-multi"}}), encoding="utf-8")

    def fake_plan(oracle_manifest: Path, *, dry_run: bool):
        payload = json.loads(oracle_manifest.read_text(encoding="utf-8"))
        mission = Path(payload["mission_path"])
        text = mission.read_text(encoding="utf-8")
        assert "stage=plan\n" in text
        attempt = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("attempt_id="))
        input_sha = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("input_mission_sha256="))
        output = mission.parent / "plan-out.md"
        output.write_text("plan", encoding="utf-8")
        (mission.parent / "stage-result.json").write_text(json.dumps({
            "schema": module.RECEIPT_SCHEMA, "workflow_id": "a" * 32, "stage": "plan",
            "attempt_id": attempt, "input_mission_sha256": input_sha, "status": "PASS",
            "output_path": str(output), "output_sha256": module.sha(output), "next_stage": "web-multi",
            "next_mission_path": str(invalid_multi), "next_mission_sha256": module.sha(invalid_multi),
            "ready_for_next": True, "blocker": "",
        }), encoding="utf-8")
        return {"ok": True, "run_dir": str(mission.parent / "run")}

    with pytest.raises(module.MULTI.MultiError):
        module.run_workflow(workflow_path, oracle_execute=fake_plan)
    config = module.load_manifest(workflow_path)
    stored = module._json(module._state_path(config, "a" * 32))
    assert stored["status"] == "prepared"
    assert stored["next_stage"] == "web-multi"
    assert "multi_execution_id" not in stored

    lane_one = tmp_path / "one.md"
    lane_two = tmp_path / "two.md"
    merger = tmp_path / "merger.md"
    for path in (lane_one, lane_two, merger):
        path.write_text(path.stem, encoding="utf-8")
    invalid_multi.write_text(json.dumps({
        "schema": module.MULTI.SCHEMA, "project_root": str(tmp_path),
        "output_dir": str(tmp_path / "multi-output"),
        "solvers": [{"id": "one", "mission_path": str(lane_one)}, {"id": "two", "mission_path": str(lane_two)}],
        "merger_mission_path": str(merger),
        "next_stage_binding": {"workflow_id": "a" * 32, "stage": "web-multi"},
    }), encoding="utf-8")
    calls = 0

    def fake_multi(path: Path, *, dry_run: bool, parent_lock_held: bool):
        nonlocal calls
        calls += 1
        return {"ok": False, "parent_id": "d" * 64}

    with pytest.raises(module.WorkflowError, match="prepared next mission changed"):
        module.run_workflow(workflow_path, oracle_execute=fake_plan, multi_execute=fake_multi)
    assert calls == 0
