from __future__ import annotations

import hashlib
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


def manifest(tmp_path: Path, *, allow_pro: bool | None = None) -> Path:
    os.environ["CODEX_ORACLE_STATE_ROOT"] = str((tmp_path.parent / f"{tmp_path.name}-host-state").resolve())
    mission = tmp_path / "initial.md"
    mission.write_text("Plan the work broadly.", encoding="utf-8")
    path = tmp_path / "workflow.json"
    payload = {
        "schema": "codex.chatgpt.oracle-comprehensive/v1",
        "workflow_id": "a" * 32,
        "project_root": str(tmp_path.resolve()),
        "workflow_dir": str((tmp_path / "workflow").resolve()),
        "initial_mission_path": str(mission.resolve()),
        "initial_mission_sha256": hashlib.sha256(mission.read_bytes()).hexdigest(),
        "app_name": "DevSpace",
        "model": "gpt-5.6",
        "local_gate_command": ["python", "-c", "raise SystemExit(0)"],
    }
    if allow_pro is not None:
        payload["allow_pro"] = allow_pro
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def run_workflow(module, path: Path, **kwargs):
    return module.run_workflow(
        path,
        expected_manifest_sha256=module.sha(path),
        **kwargs,
    )


def test_project_url_is_normalized_and_propagated_to_comprehensive_stages(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["chatgpt_project_url"] = "https://chatgpt.com/g/g-p-example/project/"
    path.write_text(json.dumps(payload), encoding="utf-8")
    config = module.load_manifest(path)
    config["_parallel_parent_id"] = "b" * 64
    mission = config["initial_mission_path"]
    regular = module._oracle_manifest(
        config, mission, tmp_path / "regular", "regular-run-id", stage="plan", mission_sha=module.sha(mission)
    )
    assert json.loads(regular.read_text(encoding="utf-8"))["chatgpt_project_url"] == config["chatgpt_project_url"]


def assert_pro_context(module, payload: dict, mission: Path, evidence: tuple[Path, ...]) -> Path:
    context_manifest = Path(payload["project_context_manifest_path"])
    context = json.loads(context_manifest.read_text(encoding="utf-8"))
    packet = Path(context["packet_path"])
    assert payload["attachments"] == [str(mission), str(packet)]
    assert payload["attachment_sha256s"] == [module.sha(mission), module.sha(packet)]
    assert payload["project_context_manifest_sha256"] == module.sha(context_manifest)
    assert context["mission_path"] == str(mission)
    assert context["mission_sha256"] == module.sha(mission)
    assert [item["path"] for item in context["evidence"]] == [str(item) for item in evidence]
    receipt = module.RUNNER.PRO_CONTEXT_BUILDER.validate(context_manifest)
    assert receipt["packet_sha256"] == module.sha(packet)
    binding = module.RUNNER.validate_pro_context_preflight(
        module.RUNNER.STATE.load_manifest(
            context_manifest.parent / "oracle.json",
            expected_manifest_sha256=module.sha(context_manifest.parent / "oracle.json"),
        )
    )
    assert binding["packet_path"] == str(packet.resolve())
    return packet


def test_manifest_rejects_non_devspace_app_before_workflow_creation(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["app_name"] = "OtherWorkspace"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(module.WorkflowError, match="exactly DevSpace"):
        module.load_manifest(path)


def test_manifest_requires_allow_pro_to_be_a_boolean_explicit_opt_in(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    assert module.load_manifest(path)["allow_pro"] is False
    for invalid in (1, 0, "true", "yes", None, [], {}):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["allow_pro"] = invalid
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(module.WorkflowError, match="allow_pro must be a boolean explicit opt-in"):
            module.load_manifest(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["allow_pro"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert module.load_manifest(path)["allow_pro"] is True


def test_manifest_snapshot_remains_authoritative_across_mutex_entry(monkeypatch, tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    original_bytes = path.read_bytes()
    config = module.load_manifest(path)
    state_path = module._state_path(config, config["workflow_id"])
    module._write(state_path, {
        "schema": module.STATE_SCHEMA,
        "status": "complete",
        "workflow_id": config["workflow_id"],
        "manifest_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "records": [],
    })
    other_root = tmp_path / "other-project"
    other_root.mkdir()
    other_mission = other_root / "initial.md"
    other_mission.write_text("different authority", encoding="utf-8")
    locked_roots = []

    class MutatingMutex:
        def __enter__(self):
            locked_roots.append(config["project_root"])
            path.write_text(json.dumps({
                "schema": module.SCHEMA,
                "workflow_id": "b" * 32,
                "project_root": str(other_root.resolve()),
                "workflow_dir": str((other_root / "workflow").resolve()),
                "initial_mission_path": str(other_mission.resolve()),
                "initial_mission_sha256": hashlib.sha256(other_mission.read_bytes()).hexdigest(),
                "app_name": "DevSpace",
                "model": "gpt-5.6",
                "local_gate_command": ["python", "-c", "raise SystemExit(0)"],
            }), encoding="utf-8")

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(module.RUNNER.STATE, "project_submit_mutex", lambda *args, **kwargs: MutatingMutex())

    result = run_workflow(module,
        path,
        oracle_execute=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("the swapped manifest must never submit")
        ),
    )

    assert result["ok"] is True
    assert result["status"] == "complete"
    assert result["manifest_sha256"] == hashlib.sha256(original_bytes).hexdigest()
    assert locked_roots == [config["project_root"]]


def test_manifest_and_initial_mission_require_previewed_hashes(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    expected = module.sha(path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    payload["max_stages"] = 9
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.WorkflowError, match="changed after dry-run preview"):
        module.load_manifest(path, expected_manifest_sha256=expected)

    payload["initial_mission_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.WorkflowError, match="initial mission changed"):
        module.load_manifest(path, expected_manifest_sha256=module.sha(path))

    del payload["initial_mission_sha256"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.WorkflowError, match="initial_mission_sha256"):
        module.load_manifest(path, expected_manifest_sha256=module.sha(path))


def test_legacy_manifest_requires_exact_stage_zero_host_bindings(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    config = module.load_manifest(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["initial_mission_sha256"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    manifest_sha256 = module.sha(path)
    mission_sha256 = module.sha(config["initial_mission_path"])
    state_path = module._state_path(config, config["workflow_id"])
    state = {
        "schema": module.STATE_SCHEMA,
        "status": "attention_required",
        "workflow_id": config["workflow_id"],
        "manifest_sha256": manifest_sha256,
        "current_stage": "plan",
        "next_index": 0,
        "current_mission_path": str(config["initial_mission_path"]),
        "current_input_sha256": mission_sha256,
        "current_binding_source_path": str(config["initial_mission_path"]),
        "current_binding_source_sha256": "0" * 64,
    }
    module._write(state_path, state)

    with pytest.raises(module.WorkflowError, match="initial_mission_sha256"):
        module.load_manifest(path, expected_manifest_sha256=manifest_sha256)

    state["current_binding_source_sha256"] = mission_sha256
    for invalid in (None, False, "0", 1):
        state["next_index"] = invalid
        module._write(state_path, state)
        with pytest.raises(module.WorkflowError, match="initial_mission_sha256"):
            module.load_manifest(path, expected_manifest_sha256=manifest_sha256)
    state["next_index"] = 0
    module._write(state_path, state)
    resumed = module.load_manifest(path, expected_manifest_sha256=manifest_sha256)

    assert resumed["manifest_sha256"] == manifest_sha256
    assert resumed["initial_mission_sha256"] == mission_sha256


def test_main_requires_and_propagates_comprehensive_preview_hash(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    module = load()
    path = manifest(tmp_path)
    expected = module.sha(path)

    assert module.main(["--manifest", str(path)]) == 1
    assert "MANIFEST_SHA256_REQUIRED" in json.loads(capsys.readouterr().out)["error"]["message"]

    calls = []
    monkeypatch.setattr(
        module,
        "run_workflow",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )
    assert module.main([
        "--manifest", str(path), "--expected-manifest-sha256", expected,
    ]) == 0
    capsys.readouterr()
    assert calls == [((path,), {"expected_manifest_sha256": expected, "dry_run": False})]

    retire_calls = []
    monkeypatch.setattr(
        module,
        "retire_workflow",
        lambda *args, **kwargs: retire_calls.append((args, kwargs)) or {"ok": True},
    )
    confirmation = "retire-comprehensive-workflow:" + "a" * 32
    assert module.main([
        "--manifest", str(path),
        "--expected-manifest-sha256", expected,
        "--retire-workflow",
        "--retirement-confirmation", confirmation,
        "--retirement-reason", "operator-authorized replacement",
    ]) == 0
    capsys.readouterr()
    assert retire_calls == [((path,), {
        "expected_manifest_sha256": expected,
        "confirmation": confirmation,
        "reason": "operator-authorized replacement",
    })]


def test_web_authored_relay_reaches_complete_without_host_semantic_rewrite(tmp_path: Path) -> None:
    module = load()
    order = ["plan", "review", "implementation", "final-web-gate"]
    seen = []

    def fake_execute(path: Path, *, dry_run: bool, **kwargs):
        assert kwargs["expected_manifest_sha256"] == module.sha(path)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model_strategy"] == "select"
        assert config["thinking_time"] == "extra-high"
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

    result = run_workflow(module,
        manifest(tmp_path),
        oracle_execute=fake_execute,
        local_gate_runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "gate ok", ""),
    )
    assert result["ok"] is True
    assert seen == order
    assert result["status"] == "complete"


def test_pro_stage_runs_oracle_attachment_only_and_materializes_bound_receipt(monkeypatch, tmp_path: Path) -> None:
    module = load()
    stages = []
    real_load_manifest = module.RUNNER.STATE.load_manifest

    def exact_child_load(path: Path, **kwargs):
        if path.name == "oracle.json":
            assert kwargs.get("expected_manifest_sha256") == module.sha(path)
        return real_load_manifest(path, **kwargs)

    monkeypatch.setattr(module.RUNNER.STATE, "load_manifest", exact_child_load)

    def regular_receipt(mission: Path, stage: str, next_stage: str, next_mission: Path) -> None:
        text = mission.read_text(encoding="utf-8")
        attempt = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("attempt_id="))
        input_sha = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("input_mission_sha256="))
        output = mission.parent / "regular-output.md"
        output.write_text(stage, encoding="utf-8")
        (mission.parent / "stage-result.json").write_text(json.dumps({
            "schema": module.RECEIPT_SCHEMA, "workflow_id": "a" * 32, "stage": stage,
            "attempt_id": attempt, "input_mission_sha256": input_sha, "status": "PASS",
            "output_path": str(output), "output_sha256": module.sha(output),
            "next_stage": next_stage, "next_mission_path": str(next_mission),
            "next_mission_sha256": module.sha(next_mission), "ready_for_next": True, "blocker": "",
        }), encoding="utf-8")

    def fake_execute(path: Path, *, dry_run: bool, **kwargs):
        payload = json.loads(path.read_text(encoding="utf-8"))
        mission = Path(payload["mission_path"])
        text = mission.read_text(encoding="utf-8")
        stage = next(item for item in ("plan", "pro", "review", "implementation", "final-web-gate") if f"stage={item}\n" in text)
        stages.append(stage)
        if stage == "pro":
            assert payload["transport"] == "pro-attachment-only"
            assert payload["model"] == "gpt-5.6-sol"
            assert_pro_context(module, payload, mission, (tmp_path / "plan-evidence.zip",))
            assert "app_name" not in payload
            attempt = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("attempt_id="))
            input_sha = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("input_mission_sha256="))
            oracle_output = mission.parent / "oracle-output.json"
            oracle_output.write_text(json.dumps({
                "schema": module.PRO_OUTPUT_SCHEMA, "workflow_id": "a" * 32, "stage": "pro",
                "attempt_id": attempt, "input_mission_sha256": input_sha, "status": "PASS",
                "output_text": "Pro decision\nsecond line\n", "next_stage": "review",
                "next_mission_text": "Review the Pro decision independently.\nPreserve LF.\n",
                "ready_for_next": True, "blocker": "",
            }), encoding="utf-8")
            return {"ok": True, "run_dir": str(mission.parent / "run"), "output_path": str(oracle_output)}
        next_stage = {
            "plan": "pro", "review": "implementation",
            "implementation": "final-web-gate", "final-web-gate": "complete",
        }[stage]
        if stage == "plan":
            packet = tmp_path / "plan-evidence.zip"
            packet.write_bytes(b"plan evidence packet")
            next_mission = _pro_attachment_mission(
                module, tmp_path, [{"path": str(packet), "sha256": module.sha(packet)}]
            )
        else:
            next_mission = tmp_path / f"next-{stage}.md"
            next_mission.write_text(f"mission after {stage}", encoding="utf-8")
        regular_receipt(mission, stage, next_stage, next_mission)
        return {"ok": True, "run_dir": str(mission.parent / "run")}

    result = run_workflow(module,
        manifest(tmp_path, allow_pro=True),
        oracle_execute=fake_execute,
        local_gate_runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    assert result["ok"] is True, result
    assert stages == ["plan", "pro", "review", "implementation", "final-web-gate"]
    pro_stage = next((tmp_path / "workflow" / "stages").glob("01-pro-*"))
    assert (pro_stage / "output.md").read_bytes() == b"Pro decision\nsecond line\n"
    assert (pro_stage / "next-mission.md").read_bytes() == b"Review the Pro decision independently.\nPreserve LF.\n"
    receipt = json.loads((pro_stage / "stage-result.json").read_text(encoding="utf-8"))
    assert receipt["stage"] == "pro"
    assert receipt["next_stage"] == "review"


@pytest.mark.parametrize("status", ["PASS", "completed"], ids=["plan-ready", "legacy-completed"])
def test_plan_pro_transition_requires_explicit_opt_in(tmp_path: Path, status: str) -> None:
    module = load()
    workflow = manifest(tmp_path)
    calls = []
    pro_next = tmp_path / "pro-next.md"

    def fake_execute(path: Path, *, dry_run: bool, **kwargs):
        payload = json.loads(path.read_text(encoding="utf-8"))
        mission = Path(payload["mission_path"])
        text = mission.read_text(encoding="utf-8")
        stage = next(item for item in ("plan", "pro") if f"stage={item}\n" in text)
        calls.append(stage)
        if stage != "plan":
            raise AssertionError("Pro stage must never be submitted without explicit opt-in")
        attempt = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("attempt_id="))
        input_sha = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("input_mission_sha256="))
        output = mission.parent / "plan-output.md"
        output.write_text("plan", encoding="utf-8")
        pro_next.write_text("Pro transition without opt-in", encoding="utf-8")
        (mission.parent / "stage-result.json").write_text(json.dumps({
            "schema": module.RECEIPT_SCHEMA, "workflow_id": "a" * 32, "stage": "plan",
            "attempt_id": attempt, "input_mission_sha256": input_sha, "status": status,
            "output_path": str(output), "output_sha256": module.sha(output),
            "next_stage": "pro", "next_mission_path": str(pro_next),
            "next_mission_sha256": module.sha(pro_next), "ready_for_next": True, "blocker": "",
        }), encoding="utf-8")
        return {"ok": True, "run_dir": str(mission.parent / "run")}

    with pytest.raises(module.WorkflowError, match="PRO_EXPLICIT_OPT_IN_REQUIRED"):
        run_workflow(module, workflow, oracle_execute=fake_execute)
    assert calls == ["plan"]


def test_pro_devspace_submits_writable_transport_without_attachments_and_materializes_receipt(
    monkeypatch, tmp_path: Path
) -> None:
    module = load()
    stages = []
    real_load_manifest = module.RUNNER.STATE.load_manifest

    def exact_child_load(path: Path, **kwargs):
        if path.name == "oracle.json":
            assert kwargs.get("expected_manifest_sha256") == module.sha(path)
        return real_load_manifest(path, **kwargs)

    monkeypatch.setattr(module.RUNNER.STATE, "load_manifest", exact_child_load)

    def regular_receipt(mission: Path, stage: str, next_stage: str, next_mission: Path) -> None:
        text = mission.read_text(encoding="utf-8")
        attempt = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("attempt_id="))
        input_sha = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("input_mission_sha256="))
        output = mission.parent / "regular-output.md"
        output.write_text(stage, encoding="utf-8")
        (mission.parent / "stage-result.json").write_text(json.dumps({
            "schema": module.RECEIPT_SCHEMA, "workflow_id": "a" * 32, "stage": stage,
            "attempt_id": attempt, "input_mission_sha256": input_sha, "status": "PASS",
            "output_path": str(output), "output_sha256": module.sha(output),
            "next_stage": next_stage, "next_mission_path": str(next_mission),
            "next_mission_sha256": module.sha(next_mission), "ready_for_next": True, "blocker": "",
        }), encoding="utf-8")

    def fake_execute(path: Path, *, dry_run: bool, **kwargs):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["pro_selection_policy"] == "explicit-only"
        mission = Path(payload["mission_path"])
        text = mission.read_text(encoding="utf-8")
        stage = next(item for item in ("plan", "pro", "review", "implementation", "final-web-gate") if f"stage={item}\n" in text)
        stages.append(stage)
        if stage == "pro":
            assert payload["transport"] == "pro-devspace"
            assert payload["app_name"] == "DevSpace"
            assert payload["model"] == "gpt-5.6-sol"
            assert payload["thinking_time"] == "heavy"
            assert not (set(payload) & {
                "attachments", "attachment_sha256s",
                "project_context_manifest_path", "project_context_manifest_sha256",
            })
            assert "[PRO_DEVSPACE_WRITE_AUTHORITY]" in text
            assert "only within the scope this mission directs" in text
            attempt = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("attempt_id="))
            input_sha = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("input_mission_sha256="))
            oracle_output = mission.parent / "oracle-output.json"
            oracle_output.write_text(json.dumps({
                "schema": module.PRO_OUTPUT_SCHEMA, "workflow_id": "a" * 32, "stage": "pro",
                "attempt_id": attempt, "input_mission_sha256": input_sha, "status": "PASS",
                "output_text": "Pro decision\nsecond line\n", "next_stage": "review",
                "next_mission_text": "Review the Pro decision independently.\nPreserve LF.\n",
                "ready_for_next": True, "blocker": "",
            }), encoding="utf-8")
            return {"ok": True, "run_dir": str(mission.parent / "run"), "output_path": str(oracle_output)}
        next_stage = {
            "plan": "pro", "review": "implementation",
            "implementation": "final-web-gate", "final-web-gate": "complete",
        }[stage]
        next_mission = tmp_path / f"next-{stage}.md"
        next_mission.write_text(f"mission after {stage}", encoding="utf-8")
        regular_receipt(mission, stage, next_stage, next_mission)
        return {"ok": True, "run_dir": str(mission.parent / "run")}

    result = run_workflow(module,
        manifest(tmp_path, allow_pro=True),
        oracle_execute=fake_execute,
        local_gate_runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    assert result["ok"] is True, result
    assert stages == ["plan", "pro", "review", "implementation", "final-web-gate"]
    pro_stage = next((tmp_path / "workflow" / "stages").glob("01-pro-*"))
    assert (pro_stage / "output.md").read_bytes() == b"Pro decision\nsecond line\n"
    assert (pro_stage / "next-mission.md").read_bytes() == b"Review the Pro decision independently.\nPreserve LF.\n"
    receipt = json.loads((pro_stage / "stage-result.json").read_text(encoding="utf-8"))
    assert set(receipt) == {
        "schema", "workflow_id", "stage", "attempt_id", "input_mission_sha256", "status",
        "output_path", "output_sha256", "next_stage", "next_mission_path",
        "next_mission_sha256", "ready_for_next", "blocker",
    }
    assert receipt["stage"] == "pro"
    assert receipt["next_stage"] == "review"


def test_pro_mission_states_write_authority_only_for_the_writable_route(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    source = tmp_path / "pro-source.md"
    source.write_text("Pro review request", encoding="utf-8")
    source_sha = module.sha(source)
    writable_mission, _, _, _ = module._pro_stage_mission(
        config, "a" * 32, 1, source, "b" * 32, source_sha, source.read_bytes(), writable=True,
    )
    text = writable_mission.read_text(encoding="utf-8")
    assert "[PRO_DEVSPACE_WRITE_AUTHORITY]" in text
    assert f"exact_project_root={config['project_root']}" in text
    assert "exact project root" in text
    assert "only inside exact_project_root" in text
    assert "Never substitute a parent root, child directory" in text
    read_only_mission, _, _, _ = module._pro_stage_mission(
        config, "a" * 32, 1, source, "c" * 32, source_sha, source.read_bytes(),
    )
    assert "[PRO_DEVSPACE_WRITE_AUTHORITY]" not in read_only_mission.read_text(encoding="utf-8")


def test_pro_exact_recovery_materializes_output_without_resubmission(tmp_path: Path) -> None:
    module = load()
    workflow_path = manifest(tmp_path)
    config = module.load_manifest(workflow_path)
    config["_parallel_parent_id"] = "b" * 64
    attempt = "c" * 32
    source = tmp_path / "pro-source.md"
    source.write_text("Pro review request", encoding="utf-8")
    mission, receipt, input_sha, mission_sha = module._pro_stage_mission(
        config, "a" * 32, 1, source, attempt, module.sha(source), source.read_bytes()
    )
    oracle_manifest = module._oracle_manifest(
        config, mission, mission.parent, attempt, stage="pro",
        pro_attachments=((source, module.sha(source)),),
        mission_sha=mission_sha,
    )
    payload = json.loads(oracle_manifest.read_text(encoding="utf-8"))
    assert_pro_context(module, payload, mission, (source,))
    run_dir = _oracle_running_state(module, oracle_manifest)
    state_path = module._state_path(config, "a" * 32)
    module._write(state_path, {
        "schema": module.STATE_SCHEMA, "status": "attention_required", "workflow_id": "a" * 32,
        "manifest_sha256": config["manifest_sha256"], "current_stage": "pro",
        "current_attempt_id": attempt, "current_input_sha256": input_sha,
        "current_mission_path": str(source), "receipt_path": str(receipt),
        "oracle_run_id": attempt, "oracle_run_dir": str(run_dir),
        "oracle_manifest_path": str(oracle_manifest),
        "oracle_manifest_sha256": module.sha(oracle_manifest),
        "current_augmented_mission_path": str(mission),
        "current_augmented_mission_sha256": mission_sha,
        "next_index": 1, "records": [],
    })
    oracle_output = run_dir / "recovered-output.json"
    oracle_output.write_text(json.dumps({
        "schema": module.PRO_OUTPUT_SCHEMA, "workflow_id": "a" * 32, "stage": "pro",
        "attempt_id": attempt, "input_mission_sha256": input_sha, "status": "PASS",
        "output_text": "Recovered Pro result", "next_stage": "review",
        "next_mission_text": "Review recovered Pro result.",
        "ready_for_next": True, "blocker": "",
    }), encoding="utf-8")
    submissions = 0

    def no_pro_resubmit(path: Path, *, dry_run: bool, **kwargs):
        nonlocal submissions
        submissions += 1
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["transport"] == "devspace"
        return {"ok": False, "run_dir": str(_oracle_running_state(module, path))}

    def fake_recover(exact_run_dir: Path, *, action: str, dry_run: bool):
        assert exact_run_dir == run_dir
        assert action == "harvest"
        return {"ok": True, "status": "complete", "run_dir": str(run_dir), "output_path": str(oracle_output)}

    result = run_workflow(module,
        workflow_path, oracle_execute=no_pro_resubmit, oracle_recover=fake_recover
    )
    assert result["status"] == "attention_required"
    assert result["current_stage"] == "review"
    assert submissions == 1
    assert receipt.is_file()
    assert (receipt.parent / "output.md").read_text(encoding="utf-8") == "Recovered Pro result"


@pytest.mark.parametrize("mutation", ["duplicate", "additional"])
def test_pro_output_rejects_duplicate_or_additional_keys(tmp_path: Path, mutation: str) -> None:
    module = load()
    workflow_path = manifest(tmp_path)
    config = module.load_manifest(workflow_path)
    config["_parallel_parent_id"] = "b" * 64
    attempt = "d" * 32
    source = tmp_path / "pro-source.md"
    source.write_text("Pro request", encoding="utf-8")
    mission, receipt, input_sha, _ = module._pro_stage_mission(
        config, "a" * 32, 1, source, attempt, module.sha(source), source.read_bytes()
    )
    output = tmp_path / "pro-output.json"
    base = {
        "schema": module.PRO_OUTPUT_SCHEMA, "workflow_id": "a" * 32, "stage": "pro",
        "attempt_id": attempt, "input_mission_sha256": input_sha, "status": "PASS",
        "output_text": "result", "next_stage": "review", "next_mission_text": "review",
        "ready_for_next": True, "blocker": "",
    }
    if mutation == "additional":
        base["unexpected"] = "forbidden"
        output.write_text(json.dumps(base), encoding="utf-8")
    else:
        valid = json.dumps(base)
        output.write_text(valid[:-1] + ',"status":"PASS"}', encoding="utf-8")
    with pytest.raises(module.WorkflowError, match="duplicate key|closed key set"):
        module._materialize_pro_receipt(
            config, receipt, "a" * 32, attempt, input_sha,
            {"output_path": str(output)},
        )
    assert not receipt.exists()


def test_missing_receipt_fails_closed_without_duplicate_stage(tmp_path: Path) -> None:
    module = load()
    calls = 0

    def fake_execute(path: Path, *, dry_run: bool, **kwargs):
        nonlocal calls
        calls += 1
        return {"ok": True, "run_dir": str(tmp_path / "run")}

    result = run_workflow(module, manifest(tmp_path), oracle_execute=fake_execute)
    assert result["ok"] is False
    assert result["status"] == "awaiting_receipt"
    assert calls == 1


def test_failing_receipt_cannot_complete(tmp_path: Path) -> None:
    module = load()

    def fake_execute(path: Path, *, dry_run: bool, **kwargs):
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
        run_workflow(module, manifest(tmp_path), oracle_execute=fake_execute)
    except module.WorkflowError as exc:
        assert "did not pass" in str(exc)
    else:
        raise AssertionError("FAIL receipt must not advance")


def _bound_multi_manifest(module, tmp_path: Path, *, with_receipt: bool) -> Path:
    lanes = [tmp_path / "bound-one.md", tmp_path / "bound-two.md"]
    merger = tmp_path / "bound-merger.md"
    for path in [*lanes, merger]:
        path.write_text(path.stem, encoding="utf-8")
    payload = {
        "schema": module.MULTI.SCHEMA, "project_root": str(tmp_path),
        "output_dir": str(tmp_path / "bound-multi-output"),
        "solvers": [
            {"id": f"lane-{index}", "mission_path": str(path), "mission_sha256": module.sha(path)}
            for index, path in enumerate(lanes)
        ],
        "merger_mission_path": str(merger), "merger_mission_sha256": module.sha(merger),
        "next_stage_binding": {"workflow_id": "a" * 32, "stage": "web-multi"},
    }
    if with_receipt:
        payload["next_stage_result_path"] = str(tmp_path / "bound-multi-receipt.json")
    path = tmp_path / "bound-multi.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


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
            {"id": "one", "mission_path": str(lane_one), "mission_sha256": module.sha(lane_one)},
            {"id": "two", "mission_path": str(lane_two), "mission_sha256": module.sha(lane_two)},
        ],
        "merger_mission_path": str(merger),
        "merger_mission_sha256": module.sha(merger),
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

    def fake_oracle(path: Path, *, dry_run: bool, **kwargs):
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

    def fake_multi(
        path: Path, *, expected_manifest_sha256: str, parent_id: str,
        dry_run: bool, parent_lock_held: bool, terminal_seal,
    ):
        assert parent_lock_held is True
        workflow_config = module.load_manifest(workflow_path)
        stored = module._json(module._state_path(workflow_config, "a" * 32))
        assert stored["multi_execution_id"]
        assert stored["multi_manifest_sha256"] == module.sha(multi_manifest)
        assert expected_manifest_sha256 == stored["multi_manifest_sha256"]
        assert parent_id == stored["multi_execution_id"]
        assert Path(stored["multi_result_path"]).name == "result.json"
        receipt = Path(stored["multi_receipt_path"])
        output = tmp_path / "multi-output.md"
        output.write_text("merged", encoding="utf-8")
        receipt.write_text(json.dumps({
            "schema": "codex.chatgpt.oracle-stage-result/v1",
            "workflow_id": "a" * 32,
            "stage": "web-multi",
            "attempt_id": parent_id,
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
        terminal = {
            "schema": module.MULTI.RESULT_SCHEMA,
            "status": "complete",
            "parent_id": parent_id,
            "manifest_sha256": expected_manifest_sha256,
            "lanes": [],
            "next_stage_result_path": str(receipt),
        }
        module.MULTI._publish_result(Path(stored["multi_result_path"]), terminal, terminal_seal)
        return {
            "ok": True, "parent_id": parent_id, "manifest_sha256": expected_manifest_sha256,
            "next_stage_result_path": str(receipt),
        }

    result = run_workflow(module,
        workflow_path,
        oracle_execute=fake_oracle,
        multi_execute=fake_multi,
        local_gate_runner=lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    assert result["ok"] is True
    assert stages_seen == ["plan", "review", "implementation", "final-web-gate"]


def test_web_multi_requires_receipt_path_before_submission(tmp_path: Path) -> None:
    module = load()
    workflow = manifest(tmp_path)
    config = module.load_manifest(workflow)
    source = _bound_multi_manifest(module, tmp_path, with_receipt=False)
    module._write(module._state_path(config, "a" * 32), {
        "schema": module.STATE_SCHEMA, "status": "prepared", "workflow_id": "a" * 32,
        "manifest_sha256": config["manifest_sha256"], "next_stage": "web-multi",
        "next_mission_path": str(source), "next_mission_sha256": module.sha(source),
        "next_index": 1, "records": [],
    })
    calls = 0

    def never_submit(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("missing Multi receipt path must fail before submission")

    with pytest.raises(module.WorkflowError, match="absolute next_stage_result_path"):
        run_workflow(module, workflow, multi_execute=never_submit)
    assert calls == 0


def test_failed_web_multi_preserves_exact_execution_identity(tmp_path: Path) -> None:
    module = load()
    workflow = manifest(tmp_path)
    config = module.load_manifest(workflow)
    source = _bound_multi_manifest(module, tmp_path, with_receipt=True)
    source_sha = module.sha(source)
    module._write(module._state_path(config, "a" * 32), {
        "schema": module.STATE_SCHEMA, "status": "prepared", "workflow_id": "a" * 32,
        "manifest_sha256": config["manifest_sha256"], "next_stage": "web-multi",
        "next_mission_path": str(source), "next_mission_sha256": source_sha,
        "next_index": 1, "records": [],
    })
    seen: dict[str, str] = {}

    def partial_multi(path: Path, **kwargs):
        seen.update({
            "execution": kwargs["parent_id"],
            "manifest": kwargs["expected_manifest_sha256"],
        })
        terminal = {
            "schema": module.MULTI.RESULT_SCHEMA, "status": "partial",
            "parent_id": kwargs["parent_id"],
            "manifest_sha256": kwargs["expected_manifest_sha256"], "lanes": [],
            "next_stage_result_path": None,
        }
        result_path = tmp_path / "bound-multi-output" / "result.json"
        module.MULTI._publish_result(result_path, terminal, kwargs["terminal_seal"])
        return {"ok": False, **terminal}

    result = run_workflow(module, workflow, multi_execute=partial_multi)
    assert result["status"] == "attention_required"
    assert result["multi_execution_id"] == seen["execution"]
    assert result["multi_manifest_sha256"] == seen["manifest"] == source_sha
    assert Path(result["multi_result_path"]).name == "result.json"
    assert result["multi_receipt_path"] == str(tmp_path / "bound-multi-receipt.json")
    assert result["current_stage"] == "web-multi"
    assert result["multi_terminal_status"] == "partial"
    assert result["multi_result_sha256"] == module.sha(Path(result["multi_result_path"]))
    assert "multi_receipt_sha256" not in result

    receipt_path = Path(result["multi_receipt_path"])
    output = tmp_path / "partial-output.md"
    review = tmp_path / "partial-review.md"
    output.write_text("partial", encoding="utf-8")
    review.write_text("review partial", encoding="utf-8")
    receipt_path.write_text(json.dumps({
        "schema": module.RECEIPT_SCHEMA, "workflow_id": "a" * 32, "stage": "web-multi",
        "attempt_id": seen["execution"], "input_mission_sha256": source_sha, "status": "PASS",
        "output_path": str(output), "output_sha256": module.sha(output), "next_stage": "review",
        "next_mission_path": str(review), "next_mission_sha256": module.sha(review),
        "ready_for_next": True, "blocker": "",
    }), encoding="utf-8")
    result_path = Path(result["multi_result_path"])
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps({
        "schema": module.MULTI.RESULT_SCHEMA, "status": "complete",
        "parent_id": seen["execution"], "manifest_sha256": source_sha,
        "next_stage_result_path": str(receipt_path),
    }), encoding="utf-8")
    calls = {"multi": 0, "oracle": 0}

    def never_multi(*args, **kwargs):
        calls["multi"] += 1
        raise AssertionError("persisted partial Multi must not resubmit")

    def never_oracle(*args, **kwargs):
        calls["oracle"] += 1
        raise AssertionError("persisted partial Multi must not advance")

    recovered = run_workflow(module,
        workflow, multi_execute=never_multi, oracle_execute=never_oracle
    )
    assert recovered["status"] == "attention_required"
    assert recovered["current_stage"] == "web-multi"
    assert recovered["multi_execution_id"] == seen["execution"]
    assert recovered["multi_manifest_sha256"] == source_sha
    assert recovered["multi_result_path"] == str(result_path)
    assert recovered["multi_receipt_path"] == str(receipt_path)
    assert recovered["recovery"]["error"] == "MULTI_TERMINAL_RESULT_CHANGED"
    assert calls == {"multi": 0, "oracle": 0}

    result_path.write_bytes(result_path.read_bytes() + b"\n")
    mutated = run_workflow(module,
        workflow, multi_execute=never_multi, oracle_execute=never_oracle
    )
    assert mutated["status"] == "attention_required"
    assert mutated["recovery"]["error"] == "MULTI_TERMINAL_RESULT_CHANGED"
    assert mutated["multi_result_sha256"] == result["multi_result_sha256"]
    assert calls == {"multi": 0, "oracle": 0}


def test_dry_run_leaves_no_host_workflow_state_and_real_run_can_follow(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    previews = []

    def fake_preview(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        previews.append(dry_run)
        return {"ok": True, "status": "dry-run"}

    preview = run_workflow(module, path, dry_run=True, oracle_execute=fake_preview)
    assert preview["ok"] is True
    assert preview["manifest_sha256"] == module.sha(path)
    assert preview["input_mission_sha256"] == module.load_manifest(path)["initial_mission_sha256"]
    assert previews == [True]
    config = module.load_manifest(path)
    assert not module._state_path(config, "a" * 32).exists()

    calls = 0

    def fake_real(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        nonlocal calls
        calls += 1
        return {"ok": True, "run_dir": str(tmp_path / "fake-run")}

    real = run_workflow(module, path, oracle_execute=fake_real)
    assert real["status"] == "awaiting_receipt"
    assert calls == 1


def _oracle_running_state(module, oracle_manifest: Path) -> Path:
    config = module.RUNNER.STATE.load_manifest(
        oracle_manifest, expected_manifest_sha256=module.sha(oracle_manifest)
    )
    layout = module.RUNNER.STATE.create_layout(config, run_id=config.requested_run_id)
    layout.run_dir.mkdir(parents=True)
    module.RUNNER.STATE.write_json_atomic(
        layout.state_path,
        module.RUNNER.STATE.state_payload(
            config, layout, status="running",
            resolved_version=module.RUNNER.STATE.ORACLE_ACTIVE_VERSION,
        ),
    )
    return layout.run_dir


def _retirable_workflow(module, tmp_path: Path) -> tuple[Path, dict, Path, Path]:
    path = manifest(tmp_path)
    config = module.load_manifest(path)
    config["_review_policy"] = module._default_review_policy()
    config["_parallel_parent_id"] = hashlib.sha256(config["workflow_id"].encode()).hexdigest()
    attempt = "b" * 32
    source = config["initial_mission_path"]
    source_sha = module.sha(source)
    mission, receipt_path, input_sha, mission_sha = module._stage_mission(
        config, config["workflow_id"], 0, "plan", source, attempt,
        source_sha, source.read_bytes(),
    )
    oracle_manifest = module._oracle_manifest(
        config, mission, mission.parent, attempt, stage="plan", mission_sha=mission_sha,
    )
    oracle_config = module.RUNNER.STATE.load_manifest(
        oracle_manifest, expected_manifest_sha256=module.sha(oracle_manifest)
    )
    layout = module.RUNNER.STATE.create_layout(oracle_config, run_id=attempt)
    layout.run_dir.mkdir(parents=True)
    run_state = module.RUNNER.STATE.state_payload(
        oracle_config,
        layout,
        status="attention_required",
        resolved_version=module.RUNNER.STATE.ORACLE_ACTIVE_VERSION,
    )
    run_state.update({
        "exit_code": 1,
        "session_authority": "submitted_unknown",
        "terminal_harvested": False,
        "artifact_sha256": None,
        "transport_status": "incomplete",
    })
    module.RUNNER.STATE.write_json_atomic(layout.state_path, run_state)
    Path(run_state["mission"]["transport_path"]).write_bytes(mission.read_bytes())
    slug = run_state["oracle"]["slug"]
    marker = module.RUNNER.STATE.ORACLE_APP_MENTION_ROUTE_UNCONFIRMED_MARKER
    (layout.run_dir / "stdout.log").write_text(
        f"Session: {slug}\nERROR: {marker}\nUser error (browser-automation): {marker}\n",
        encoding="utf-8",
    )
    (layout.run_dir / "stderr.log").write_text("", encoding="utf-8")
    (layout.run_dir / "transcript.md").write_text("", encoding="utf-8")
    (layout.run_dir / "recovery-harvest-stdout.log").write_text(
        f'No live ChatGPT tab matched session "{slug}". Attempting recovery.\n',
        encoding="utf-8",
    )
    (layout.run_dir / "recovery-harvest-stderr.log").write_text(
        "Cannot recover conversation: session metadata has no recoverable ChatGPT conversation URL.\n",
        encoding="utf-8",
    )
    settled = module.RUNNER.STATE.settle_user_confirmed_no_submission(
        layout.state_path,
        confirmation=module.RUNNER.STATE.USER_CONFIRMED_NO_SUBMISSION,
        reason="user confirmed this exact pre-submit attempt",
    )
    reference = settled["user_confirmed_no_submission"]
    workflow_state = {
        "schema": module.STATE_SCHEMA,
        "status": "attention_required",
        "workflow_id": config["workflow_id"],
        "manifest_sha256": config["manifest_sha256"],
        "current_stage": "plan",
        "current_attempt_id": attempt,
        "current_input_sha256": input_sha,
        "current_mission_path": str(source),
        "receipt_path": str(receipt_path),
        "current_binding_source_path": str(source),
        "current_binding_source_sha256": input_sha,
        "current_augmented_mission_path": str(mission),
        "current_augmented_mission_sha256": mission_sha,
        "oracle_run_id": attempt,
        "oracle_run_dir": str(layout.run_dir),
        "oracle_manifest_path": str(oracle_manifest),
        "oracle_manifest_sha256": module.sha(oracle_manifest),
        "next_index": 0,
        "records": [
            {"stage": "plan", "run_dir": str(layout.run_dir), "ok": False},
            {
                "stage": "plan",
                "run_dir": str(layout.run_dir),
                "settlement": "user-confirmed-no-submission",
                "settlement_path": reference["path"],
                "settlement_sha256": reference["sha256"],
            },
        ],
        "blocker": "exact pre-submit attempt is settled; scientific work was not completed",
    }
    state_path = module._state_path(config, config["workflow_id"])
    module._write_workflow_state(state_path, config, workflow_state)
    return path, config, layout.run_dir, state_path


def test_retirement_releases_scope_with_immutable_idempotent_receipt(tmp_path: Path) -> None:
    module = load()
    path, config, _run_dir, state_path = _retirable_workflow(module, tmp_path)
    confirmation = f"{module.RETIREMENT_CONFIRMATION_PREFIX}{config['workflow_id']}"
    state_before = state_path.read_bytes()

    first = module.retire_workflow(
        path,
        expected_manifest_sha256=module.sha(path),
        confirmation=confirmation,
        reason="user authorized a new workflow ID for the same scientific mission",
    )
    receipt = Path(first["retirement_receipt_path"])
    receipt_before = receipt.read_bytes()
    scope_before = Path(first["scope_path"]).read_bytes()
    second = module.retire_workflow(
        path,
        expected_manifest_sha256=module.sha(path),
        confirmation=confirmation,
        reason="user authorized a new workflow ID for the same scientific mission",
    )

    assert first["status"] == "workflow_retired_scope_released"
    assert first["scope_readback"]["status"] == "released"
    assert first["scope_readback"]["active_workflow_id"] == ""
    assert json.loads(receipt_before)["scientific_work_complete"] is False
    assert second["replayed"] is True
    assert receipt.read_bytes() == receipt_before
    assert Path(first["scope_path"]).read_bytes() == scope_before
    assert state_path.read_bytes() == state_before
    assert module._json(state_path)["status"] == "attention_required"

    replacement = {**config, "workflow_id": "c" * 32, "workflow_dir": tmp_path / "workflow-new"}
    module._claim_scope(replacement, replacement["workflow_id"])
    claimed = module._json(module._scope_path(replacement))
    assert claimed["status"] == "active"
    assert claimed["active_workflow_id"] == replacement["workflow_id"]


@pytest.mark.parametrize(
    "mutation",
    ("submitted", "live", "terminal", "foreign", "mismatched", "unsettled", "terminal-receipt"),
)
def test_retirement_refuses_any_non_pre_submit_or_unbound_attempt(
    tmp_path: Path,
    mutation: str,
) -> None:
    module = load()
    path, config, run_dir, state_path = _retirable_workflow(module, tmp_path)
    run_state_path = run_dir / "state.json"
    run_state = module.RUNNER.STATE.load_state(run_state_path)
    workflow_state = module._json(state_path)
    if mutation in {"submitted", "live"}:
        run_state["session_authority"] = "submitted_unknown" if mutation == "submitted" else "live"
        module.RUNNER.STATE.write_json_atomic(run_state_path, run_state)
    elif mutation == "terminal":
        (run_dir / "output.md").write_text("terminal provider output", encoding="utf-8")
        run_state.update({"session_authority": "terminal", "terminal_harvested": True})
        module.RUNNER.STATE.write_json_atomic(run_state_path, run_state)
    elif mutation == "foreign":
        foreign = tmp_path / "foreign" / ("d" * 32)
        foreign.mkdir(parents=True)
        workflow_state["records"].append({"stage": "plan", "run_dir": str(foreign), "ok": False})
        module._write(state_path, workflow_state)
    elif mutation == "mismatched":
        workflow_state["current_attempt_id"] = "e" * 32
        module._write(state_path, workflow_state)
    elif mutation == "unsettled":
        (run_dir / "user-confirmed-no-submission.json").unlink()
    else:
        Path(workflow_state["receipt_path"]).write_text("{}", encoding="utf-8")

    with pytest.raises(module.WorkflowError):
        module.retire_workflow(
            path,
            expected_manifest_sha256=module.sha(path),
            confirmation=f"{module.RETIREMENT_CONFIRMATION_PREFIX}{config['workflow_id']}",
            reason="explicit replacement workflow authorization",
        )


@pytest.mark.parametrize("authority", ["pre_submit", "submitted_unknown", "live", "terminal"])
def test_retirement_refuses_every_unledgered_exact_workflow_run(
    tmp_path: Path,
    authority: str,
) -> None:
    module = load()
    path, config, _run_dir, _state_path = _retirable_workflow(module, tmp_path)
    attempt = "d" * 32
    source = config["initial_mission_path"]
    mission, _receipt, _input_sha, mission_sha = module._stage_mission(
        config, config["workflow_id"], 0, "plan", source, attempt,
        module.sha(source), source.read_bytes(),
    )
    oracle_manifest = module._oracle_manifest(
        config, mission, mission.parent, attempt, stage="plan", mission_sha=mission_sha,
    )
    oracle_config = module.RUNNER.STATE.load_manifest(
        oracle_manifest, expected_manifest_sha256=module.sha(oracle_manifest)
    )
    layout = module.RUNNER.STATE.create_layout(oracle_config, run_id=attempt)
    layout.run_dir.mkdir(parents=True)
    run_state = module.RUNNER.STATE.state_payload(
        oracle_config,
        layout,
        status="attention_required",
        resolved_version=module.RUNNER.STATE.ORACLE_ACTIVE_VERSION,
    )
    run_state.update({
        "session_authority": authority,
        "terminal_harvested": authority == "terminal",
    })
    module.RUNNER.STATE.write_json_atomic(layout.state_path, run_state)

    with pytest.raises(module.WorkflowError):
        module.retire_workflow(
            path,
            expected_manifest_sha256=module.sha(path),
            confirmation=f"{module.RETIREMENT_CONFIRMATION_PREFIX}{config['workflow_id']}",
            reason="explicit replacement workflow authorization",
        )


def test_retirement_refuses_an_unreadable_unledgered_project_run(tmp_path: Path) -> None:
    module = load()
    path, config, _run_dir, _state_path = _retirable_workflow(module, tmp_path)
    project_key = hashlib.sha256(str(config["project_root"]).casefold().encode("utf-8")).hexdigest()[:24]
    corrupt = module.RUNNER.STATE.oracle_state_root() / "projects" / project_key / "runs" / ("d" * 32)
    corrupt.mkdir(parents=True)
    (corrupt / "state.json").write_text("{", encoding="utf-8")

    with pytest.raises(module.WorkflowError, match="do not exactly match"):
        module.retire_workflow(
            path,
            expected_manifest_sha256=module.sha(path),
            confirmation=f"{module.RETIREMENT_CONFIRMATION_PREFIX}{config['workflow_id']}",
            reason="explicit replacement workflow authorization",
        )


@pytest.mark.parametrize("artifact", ["state", "settlement"])
def test_released_scope_revalidates_sealed_attempts_before_replacement_claim(
    tmp_path: Path,
    artifact: str,
) -> None:
    module = load()
    path, config, run_dir, _state_path = _retirable_workflow(module, tmp_path)
    module.retire_workflow(
        path,
        expected_manifest_sha256=module.sha(path),
        confirmation=f"{module.RETIREMENT_CONFIRMATION_PREFIX}{config['workflow_id']}",
        reason="explicit replacement workflow authorization",
    )
    target = run_dir / ("state.json" if artifact == "state" else "user-confirmed-no-submission.json")
    target.write_bytes(target.read_bytes() + b"\n")
    replacement = {**config, "workflow_id": "c" * 32, "workflow_dir": tmp_path / "workflow-new"}

    with module.RUNNER.STATE.project_submit_mutex(config["project_root"], timeout_seconds=30):
        with pytest.raises(module.WorkflowError, match="valid retirement receipt"):
            module._claim_scope(replacement, replacement["workflow_id"])


def test_retired_workflow_cannot_reclaim_after_valid_replacement_completion(tmp_path: Path) -> None:
    module = load()
    path, config, _run_dir, _state_path = _retirable_workflow(module, tmp_path)
    module.retire_workflow(
        path,
        expected_manifest_sha256=module.sha(path),
        confirmation=f"{module.RETIREMENT_CONFIRMATION_PREFIX}{config['workflow_id']}",
        reason="explicit replacement workflow authorization",
    )
    replacement = {
        **config,
        "workflow_id": "c" * 32,
        "workflow_dir": tmp_path / "workflow-new",
        "manifest_sha256": "c" * 64,
    }
    replacement_output = tmp_path / "replacement-output.md"
    replacement_output.write_text("completed replacement", encoding="utf-8")
    module._claim_scope(replacement, replacement["workflow_id"])
    module._write_workflow_state(
        module._state_path(replacement, replacement["workflow_id"]),
        replacement,
        {
            "schema": module.STATE_SCHEMA,
            "status": "complete",
            "workflow_id": replacement["workflow_id"],
            "manifest_sha256": replacement["manifest_sha256"],
            "records": [],
            "final_output_path": str(replacement_output),
            "local_gate": {"exit_code": 0},
        },
    )

    with pytest.raises(module.WorkflowError, match="retired comprehensive workflow"):
        module._claim_scope(config, config["workflow_id"])

    next_workflow = {
        **replacement,
        "workflow_id": "e" * 32,
        "workflow_dir": tmp_path / "workflow-next",
    }
    module._claim_scope(next_workflow, next_workflow["workflow_id"])
    assert module._json(module._scope_path(next_workflow))["active_workflow_id"] == next_workflow["workflow_id"]


def test_complete_replay_preserves_scope_for_the_next_workflow(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    config = module.load_manifest(path)
    config["_review_policy"] = module._default_review_policy()
    output = tmp_path / "final.md"
    output.write_text("durable result", encoding="utf-8")
    state_path = module._state_path(config, config["workflow_id"])
    module._write_workflow_state(state_path, config, {
        "schema": module.STATE_SCHEMA,
        "status": "complete",
        "workflow_id": config["workflow_id"],
        "manifest_sha256": config["manifest_sha256"],
        "records": [],
        "final_output_path": str(output),
        "local_gate": {"exit_code": 0},
    })
    scope_path = module._scope_path(config)
    scope_before = scope_path.read_bytes()

    result = run_workflow(module, path)

    assert result["status"] == "complete"
    assert scope_path.read_bytes() == scope_before
    replacement = {**config, "workflow_id": "c" * 32, "workflow_dir": tmp_path / "workflow-new"}
    module._claim_scope(replacement, replacement["workflow_id"])
    assert module._json(scope_path)["active_workflow_id"] == replacement["workflow_id"]


def test_completed_scope_with_missing_output_cannot_be_replaced(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    config["_review_policy"] = module._default_review_policy()
    module._write_workflow_state(
        module._state_path(config, config["workflow_id"]),
        config,
        {
            "schema": module.STATE_SCHEMA,
            "status": "complete",
            "workflow_id": config["workflow_id"],
            "manifest_sha256": config["manifest_sha256"],
            "records": [],
            "final_output_path": str(tmp_path / "missing.md"),
            "local_gate": {"exit_code": 0},
        },
    )
    replacement = {**config, "workflow_id": "c" * 32, "workflow_dir": tmp_path / "workflow-new"}

    with pytest.raises(module.WorkflowError, match="valid completion authority"):
        module._claim_scope(replacement, replacement["workflow_id"])


@pytest.mark.parametrize(
    ("status", "owner", "schema"),
    [("active", "", None), ("unknown", "a" * 32, None), ("active", "a" * 32, "invalid")],
)
def test_claim_scope_rejects_malformed_or_ownerless_authority(
    tmp_path: Path,
    status: str,
    owner: str,
    schema: str | None,
) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    config["_review_policy"] = module._default_review_policy()
    module._write(module._scope_path(config), {
        "schema": schema or module.SCOPE_SCHEMA,
        "status": status,
        "active_workflow_id": owner,
        "project_root": str(config["project_root"]),
        "workflow_parent": str(config["workflow_dir"].parent),
    })

    with pytest.raises(module.WorkflowError):
        module._claim_scope(config, config["workflow_id"])


def test_retirement_requires_exact_workflow_bound_confirmation(tmp_path: Path) -> None:
    module = load()
    path, config, _run_dir, _state_path = _retirable_workflow(module, tmp_path)

    with pytest.raises(module.WorkflowError, match="confirmation must be exactly"):
        module.retire_workflow(
            path,
            expected_manifest_sha256=module.sha(path),
            confirmation="retire-comprehensive-workflow:other",
            reason="explicit replacement workflow authorization",
        )
    with pytest.raises(module.WorkflowError, match="reason is required"):
        module.retire_workflow(
            path,
            expected_manifest_sha256=module.sha(path),
            confirmation=f"{module.RETIREMENT_CONFIRMATION_PREFIX}{config['workflow_id']}",
            reason="",
        )


def test_new_workflow_refuses_unreceipted_released_scope(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    config = module.load_manifest(path)
    module._write(module._scope_path(config), {
        "schema": module.SCOPE_SCHEMA,
        "status": "released",
        "active_workflow_id": "",
        "retired_workflow_id": "b" * 32,
        "project_root": str(config["project_root"]),
        "workflow_parent": str(config["workflow_dir"].parent),
    })

    with pytest.raises(module.WorkflowError, match="valid retirement receipt"):
        module._claim_scope(config, config["workflow_id"])


@pytest.mark.parametrize("mutation", ["manifest", "attachment", "context"])
def test_oracle_recovery_rejects_run_state_binding_mismatch(tmp_path: Path, mutation: str) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    config["_parallel_parent_id"] = "b" * 64
    source = config["initial_mission_path"]
    source_sha = module.sha(source)
    attempt = "c" * 32
    mission, receipt, input_sha, mission_sha = module._pro_stage_mission(
        config, "a" * 32, 0, source, attempt, source_sha, source.read_bytes()
    )
    oracle_manifest = module._oracle_manifest(
        config, mission, mission.parent, attempt, stage="pro",
        pro_attachments=((source, source_sha),), mission_sha=mission_sha,
    )
    run_dir = _oracle_running_state(module, oracle_manifest)
    run_state_path = run_dir / "state.json"
    run_state = module.RUNNER.STATE.load_state(run_state_path)
    if mutation == "manifest":
        run_state["manifest"]["expected_sha256"] = "0" * 64
    elif mutation == "attachment":
        run_state["attachments"][1]["sha256"] = "0" * 64
    else:
        run_state["project_context_manifest"]["sha256"] = "0" * 64
    module.RUNNER.STATE.write_json_atomic(run_state_path, run_state)
    recoveries = 0

    def never_recover(*args, **kwargs):
        nonlocal recoveries
        recoveries += 1
        raise AssertionError("mismatched run-state identity must fail before recovery")

    result = module._recover_exact_oracle_stage({
        "oracle_run_dir": str(run_dir),
        "oracle_run_id": attempt,
        "oracle_manifest_path": str(oracle_manifest),
        "oracle_manifest_sha256": module.sha(oracle_manifest),
        "current_augmented_mission_path": str(mission),
        "current_augmented_mission_sha256": mission_sha,
        "current_input_sha256": input_sha,
        "receipt_path": str(receipt),
    }, oracle_recover=never_recover)

    assert result["error"] == "ORACLE_RECOVERY_IDENTITY_MISMATCH"
    assert recoveries == 0


def test_running_oracle_stage_recovers_exact_run_without_resubmission(tmp_path: Path) -> None:
    module = load()
    submitted = 0
    recovered = []

    def fake_execute(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        nonlocal submitted
        submitted += 1
        return {"ok": False, "run_dir": str(_oracle_running_state(module, oracle_manifest))}

    def fake_recover(run_dir: Path, *, action: str, dry_run: bool):
        recovered.append((run_dir, action, dry_run))
        return {"ok": True, "status": "complete", "run_dir": str(run_dir)}

    path = manifest(tmp_path)
    first = run_workflow(module, path, oracle_execute=fake_execute, oracle_recover=fake_recover)
    assert first["status"] == "attention_required"
    assert first["oracle_run_id"] == first["current_attempt_id"]
    second = run_workflow(module, path, oracle_execute=fake_execute, oracle_recover=fake_recover)
    assert second["status"] == "awaiting_receipt"
    assert submitted == 1
    assert [item[1:] for item in recovered] == [("harvest", False)]
    assert second["recovery"]["status"] == "recovered"


def test_post_submit_watchdog_persists_same_attempt_and_only_exact_recovers(
    tmp_path: Path,
) -> None:
    module = load()
    submissions: list[Path] = []
    recoveries: list[tuple[Path, str]] = []

    def watchdog_execute(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        run_dir = _oracle_running_state(module, oracle_manifest)
        submissions.append(run_dir)
        state_path = run_dir / "state.json"
        state = module.RUNNER.STATE.load_state(state_path)
        state.update({
            "status": "attention_required",
            "session_authority": "submitted_unknown",
            "transport_status": "post_submit_watchdog_timeout",
            "task_outcome_reason": "host-wall-clock-expired-process-preserved",
            "host_watchdog": {
                "status": "expired",
                "process_action": "preserved",
            },
        })
        module.RUNNER.STATE.write_json_atomic(state_path, state)
        return {
            "ok": False,
            "status": "post_submit_watchdog_timeout",
            "safe_for_fresh_run": False,
            "process_preserved": True,
            "run_dir": str(run_dir),
            "result": state,
        }

    def exact_recover(run_dir: Path, *, action: str, dry_run: bool):
        recoveries.append((run_dir, action))
        return {"ok": True, "status": "complete", "run_dir": str(run_dir)}

    workflow_manifest = manifest(tmp_path)
    first = run_workflow(module,
        workflow_manifest,
        oracle_execute=watchdog_execute,
        oracle_recover=exact_recover,
    )
    second = run_workflow(module,
        workflow_manifest,
        oracle_execute=watchdog_execute,
        oracle_recover=exact_recover,
    )

    assert first["status"] == "attention_required"
    assert first["current_attempt_id"] == first["oracle_run_id"]
    assert first["oracle_run_dir"] == str(submissions[0])
    assert len(submissions) == 1
    assert recoveries == [(submissions[0], "harvest")]
    assert second["status"] == "awaiting_receipt"
    assert second["recovery"]["status"] == "recovered"


def test_unambiguous_app_mention_pre_submit_failure_retries_once(tmp_path: Path) -> None:
    module = load()
    submitted = 0

    def fake_execute(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        nonlocal submitted
        submitted += 1
        run_dir = _oracle_running_state(module, oracle_manifest)
        stdout = run_dir / "stdout.log"
        if submitted == 1:
            stdout.write_text(
                "ERROR: ChatGPT app mention suggestion did not appear.\n",
                encoding="utf-8",
            )
        else:
            stdout.write_text("ERROR: unrelated terminal failure\n", encoding="utf-8")
        return {"ok": False, "run_dir": str(run_dir)}

    result = run_workflow(module, manifest(tmp_path), oracle_execute=fake_execute)

    assert result["status"] == "attention_required"
    assert submitted == 2
    assert result["next_index"] == 0


@pytest.mark.parametrize(
    "marker",
    [
        'Unable to find model option matching "GPT-5.6 Sol" in the model switcher.',
        "--copy-profile requires rsync on PATH (spawn failed): spawn rsync ENOENT",
        "--copy-profile cannot be combined with --browser-manual-login",
    ],
)
def test_launch_time_pre_submit_failures_also_retry_once(tmp_path: Path, marker: str) -> None:
    module = load()
    submitted = 0

    def fake_execute(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        nonlocal submitted
        submitted += 1
        run_dir = _oracle_running_state(module, oracle_manifest)
        stdout = run_dir / "stdout.log"
        if submitted == 1:
            stdout.write_text(f"ERROR: {marker}\n", encoding="utf-8")
        else:
            stdout.write_text("ERROR: unrelated terminal failure\n", encoding="utf-8")
        return {"ok": False, "run_dir": str(run_dir)}

    result = run_workflow(module, manifest(tmp_path), oracle_execute=fake_execute)

    assert submitted == 2
    assert result["status"] == "attention_required"
    assert result["next_index"] == 0


def test_version_resolution_prelaunch_failure_retries_same_stage_once_then_stops(tmp_path: Path) -> None:
    module = load()
    submissions = 0
    recoveries = 0

    def fake_execute(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        nonlocal submissions
        submissions += 1
        run_dir = _oracle_running_state(module, oracle_manifest)
        state_path = run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "attention_required"
        state["session_authority"] = "pre_submit"
        state["oracle"]["resolved_version"] = "unresolved"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        (run_dir / "stdout.log").write_bytes(b"")
        (run_dir / "stderr.log").write_text(
            "version resolution failed: Command ['npx.cmd', '-y', '@steipete/oracle', '--version'] timed out after 30 seconds\n",
            encoding="utf-8",
        )
        return {"ok": False, "run_dir": str(run_dir)}

    def forbidden_recover(*args, **kwargs):
        nonlocal recoveries
        recoveries += 1
        raise AssertionError("proven pre-submit failures must not invoke exact-session recovery")

    workflow_manifest = manifest(tmp_path)
    result = run_workflow(module,
        workflow_manifest,
        oracle_execute=fake_execute,
        oracle_recover=forbidden_recover,
    )
    config = module.load_manifest(workflow_manifest)

    assert submissions == 2
    assert recoveries == 0
    assert result["status"] == "attention_required"
    assert result["current_stage"] == "plan"
    assert result["next_index"] == 0
    assert result["pre_submit_retries"] == 1
    assert result["current_binding_source_sha256"] == module.sha(config["initial_mission_path"])


def test_pre_submit_retry_budget_is_stage_input_scoped_and_counts_started_replacement(tmp_path: Path) -> None:
    module = load()
    failed_plan = tmp_path / "plan-failed"
    failed_implementation = tmp_path / "implementation-failed"
    replacement = tmp_path / "implementation-replacement"
    for path in (failed_plan, failed_implementation, replacement):
        path.mkdir()
    plan_input = "a" * 64
    implementation_input = "b" * 64
    records = [
        {
            "stage": "plan",
            "run_dir": str(failed_plan),
            "pre_submit_failure": True,
            "pre_submit_retry_consumed": True,
            "input_mission_sha256": plan_input,
        },
        {
            "stage": "implementation",
            "run_dir": str(failed_implementation),
            "settlement": "user-confirmed-no-submission",
        },
    ]

    assert module._pre_submit_retry_count(
        records,
        stage="plan",
        input_sha256=plan_input,
        current_run_dir=failed_plan,
    ) == 1
    # The plan retry does not consume the implementation binding's budget.
    assert module._pre_submit_retry_count(
        records,
        stage="implementation",
        input_sha256=implementation_input,
        current_run_dir=failed_implementation,
    ) == 0
    # Once a different attempt is current, the recorded settlement has already
    # produced its one replacement and no second submission is permitted.
    assert module._pre_submit_retry_count(
        records,
        stage="implementation",
        input_sha256=implementation_input,
        current_run_dir=replacement,
    ) == 1
    # An unattributed legacy global retry is conservatively treated as spent.
    assert module._pre_submit_retry_count(
        [{"stage": "plan", "run_dir": str(failed_plan), "ok": False}],
        stage="implementation",
        input_sha256=implementation_input,
        current_run_dir=failed_implementation,
        legacy_total=1,
    ) == 1


@pytest.mark.parametrize("legacy_manifest", (False, True), ids=("current", "legacy-bound"))
def test_user_confirmed_settlement_submits_one_bound_replacement_then_never_a_second(
    tmp_path: Path,
    legacy_manifest: bool,
) -> None:
    module = load()
    submissions: list[Path] = []

    def fake_execute(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        run_dir = _oracle_running_state(module, oracle_manifest)
        (run_dir / "transcript.md").write_text("", encoding="utf-8")
        submissions.append(run_dir)
        state_path = run_dir / "state.json"
        state = module.RUNNER.STATE.load_state(state_path)
        state.update({
            "status": "attention_required",
            "exit_code": 1,
            "session_authority": "submitted_unknown",
            "transport_status": "incomplete",
        })
        module.RUNNER.STATE.write_json_atomic(state_path, state)
        Path(state["mission"]["transport_path"]).write_bytes(
            Path(state["mission"]["path"]).read_bytes()
        )
        if len(submissions) == 1:
            slug = state["oracle"]["slug"]
            (run_dir / "stdout.log").write_text(
                f"Session: {slug}\n"
                "ERROR: Prompt did not appear in conversation before timeout (send may have failed)\n",
                encoding="utf-8",
            )
            (run_dir / "stderr.log").write_text("", encoding="utf-8")
            (run_dir / "recovery-harvest-stdout.log").write_text(
                f'No live ChatGPT tab matched session "{slug}". Attempting recovery.\n',
                encoding="utf-8",
            )
            (run_dir / "recovery-harvest-stderr.log").write_text(
                "Cannot recover conversation: session metadata has no recoverable ChatGPT conversation URL.\n",
                encoding="utf-8",
            )
        else:
            (run_dir / "stdout.log").write_text("unrelated failure\n", encoding="utf-8")
            (run_dir / "stderr.log").write_text("", encoding="utf-8")
        return {"ok": False, "run_dir": str(run_dir)}

    workflow_manifest = manifest(tmp_path)
    first = run_workflow(module, workflow_manifest, oracle_execute=fake_execute)
    assert first["status"] == "attention_required"
    assert len(submissions) == 1

    module.RUNNER.STATE.settle_user_confirmed_no_submission(
        submissions[0] / "state.json",
        confirmation=module.RUNNER.STATE.USER_CONFIRMED_NO_SUBMISSION,
        reason="user confirmed the exact attempt was not submitted",
    )
    if legacy_manifest:
        config = module.load_manifest(workflow_manifest)
        state_path = module._state_path(config, config["workflow_id"])
        payload = json.loads(workflow_manifest.read_text(encoding="utf-8"))
        del payload["initial_mission_sha256"]
        workflow_manifest.write_text(json.dumps(payload), encoding="utf-8")
        state = module._json(state_path)
        state["manifest_sha256"] = module.sha(workflow_manifest)
        module._write(state_path, state)
    second = run_workflow(module, workflow_manifest, oracle_execute=fake_execute)
    assert second["status"] == "attention_required"
    assert len(submissions) == 2
    settlement_records = [
        record for record in second["records"]
        if isinstance(record, dict) and record.get("settlement") == "user-confirmed-no-submission"
    ]
    assert len(settlement_records) == 1
    assert settlement_records[0]["settlement_path"]
    assert settlement_records[0]["settlement_sha256"]

    recoveries: list[Path] = []

    def exact_recovery_only(run_dir: Path, *, action: str, dry_run: bool):
        recoveries.append(run_dir)
        return {"ok": False, "status": "attention_required", "run_dir": str(run_dir)}

    third = run_workflow(module,
        workflow_manifest,
        oracle_execute=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("a second replacement must never be submitted")
        ),
        oracle_recover=exact_recovery_only,
    )
    assert third["status"] == "attention_required"
    assert recoveries == [submissions[1]]
    assert len(submissions) == 2


def test_user_confirmed_retry_binding_rejects_any_identity_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = load()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    attempt = "a" * 32
    workflow_id = "b" * 32
    input_path = project_root / "input.md"
    input_path.write_text("input", encoding="utf-8")
    input_sha = module.sha(input_path)
    augmented_path = project_root / "augmented.md"
    augmented_path.write_text("augmented", encoding="utf-8")
    augmented_sha = module.sha(augmented_path)
    proof = {
        "project_root": str(project_root.resolve()),
        "workflow_id": workflow_id,
        "stage": "implementation",
        "attempt_id": attempt,
        "run_id": attempt,
        "input_mission_sha256": input_sha,
        "mission_sha256": augmented_sha,
        "_augmented_mission_path": str(augmented_path.resolve()),
        "_input_mission_path": str(input_path.resolve()),
    }
    monkeypatch.setattr(module.RUNNER.STATE, "proven_user_confirmed_no_submission", lambda path: dict(proof))
    config = {"project_root": project_root.resolve()}

    assert module._user_confirmed_retry_binding_matches(
        run_dir,
        config=config,
        workflow_id=workflow_id,
        stage="implementation",
        attempt_id=attempt,
        input_sha256=input_sha,
        augmented_mission_path=augmented_path,
        augmented_mission_sha256=augmented_sha,
        binding_source_path=input_path,
    ) is True
    for field, wrong in (
        ("workflow_id", "d" * 32),
        ("stage", "review"),
        ("attempt_id", "e" * 32),
        ("run_id", "f" * 32),
        ("input_mission_sha256", "0" * 64),
        ("mission_sha256", "1" * 64),
        ("_augmented_mission_path", str(project_root / "wrong-augmented.md")),
        ("_input_mission_path", str(project_root / "wrong-input.md")),
    ):
        changed = dict(proof)
        changed[field] = wrong
        monkeypatch.setattr(
            module.RUNNER.STATE,
            "proven_user_confirmed_no_submission",
            lambda path, value=changed: value,
        )
        assert module._user_confirmed_retry_binding_matches(
            run_dir,
            config=config,
            workflow_id=workflow_id,
            stage="implementation",
            attempt_id=attempt,
            input_sha256=input_sha,
            augmented_mission_path=augmented_path,
            augmented_mission_sha256=augmented_sha,
            binding_source_path=input_path,
        ) is False

    monkeypatch.setattr(
        module.RUNNER.STATE,
        "proven_user_confirmed_no_submission",
        lambda path: dict(proof),
    )
    assert module._user_confirmed_retry_binding_matches(
        run_dir,
        config=config,
        workflow_id=workflow_id,
        stage="implementation",
        attempt_id=attempt,
        input_sha256=input_sha,
        augmented_mission_path=augmented_path,
        augmented_mission_sha256="2" * 64,
        binding_source_path=input_path,
    ) is False


def test_durable_output_prevents_pre_submit_retry_even_with_a_launch_marker(tmp_path: Path) -> None:
    module = load()
    submitted = 0

    def fake_execute(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        nonlocal submitted
        submitted += 1
        run_dir = _oracle_running_state(module, oracle_manifest)
        (run_dir / "stdout.log").write_text(
            'ERROR: Unable to find model option matching "GPT-5.6 Sol" in the model switcher.\n',
            encoding="utf-8",
        )
        (run_dir / "output.md").write_text("partial provider answer", encoding="utf-8")
        return {"ok": False, "run_dir": str(run_dir)}

    result = run_workflow(module, manifest(tmp_path), oracle_execute=fake_execute)

    assert submitted == 1
    assert result["status"] == "attention_required"


def test_running_stage_does_not_trust_existing_receipt_before_terminal_authority(tmp_path: Path) -> None:
    module = load()
    submitted = []
    recovered = []
    next_mission = tmp_path / "review.md"
    next_mission.write_text("review", encoding="utf-8")

    def fake_execute(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        config = json.loads(oracle_manifest.read_text(encoding="utf-8"))
        mission = Path(config["mission_path"])
        submitted.append(mission)
        return {"ok": False, "run_dir": str(_oracle_running_state(module, oracle_manifest))}

    def fake_recover(*args, **kwargs):
        recovered.append((args, kwargs))
        return {"ok": False, "status": "session_live", "run_dir": str(args[0])}

    path = manifest(tmp_path)
    first = run_workflow(module, path, oracle_execute=fake_execute, oracle_recover=fake_recover)
    receipt_path = Path(first["receipt_path"])
    output = receipt_path.parent / "output.md"
    output.write_text("plan", encoding="utf-8")
    receipt_path.write_text(json.dumps({
        "schema": module.RECEIPT_SCHEMA,
        "workflow_id": "a" * 32,
        "stage": "plan",
        "attempt_id": first["current_attempt_id"],
        "input_mission_sha256": first["current_input_sha256"],
        "status": "PASS",
        "output_path": str(output),
        "output_sha256": module.sha(output),
        "next_stage": "review",
        "next_mission_path": str(next_mission),
        "next_mission_sha256": module.sha(next_mission),
        "ready_for_next": True,
        "blocker": "",
    }), encoding="utf-8")

    second = run_workflow(module, path, oracle_execute=fake_execute, oracle_recover=fake_recover)

    assert second["status"] == "running"
    assert second["current_stage"] == "plan"
    assert len(submitted) == 1
    assert len(recovered) == 1
    assert recovered[0][1]["action"] == "harvest"


def test_review_revise_receipt_is_terminal_legacy_compatibility(tmp_path: Path) -> None:
    module = load()
    output = tmp_path / "review-output.md"
    output.write_text("revise", encoding="utf-8")
    next_mission = tmp_path / "next-plan.md"
    next_mission.write_text("fix the plan", encoding="utf-8")
    receipt = tmp_path / "stage-result.json"
    receipt.write_text(json.dumps({
        "schema": module.RECEIPT_SCHEMA,
        "workflow_id": "a" * 32,
        "stage": "review",
        "attempt_id": "b" * 32,
        "input_mission_sha256": "c" * 64,
        "status": "REVISE",
        "output_path": str(output),
        "output_sha256": module.sha(output),
        "next_stage": "plan",
        "next_mission_path": str(next_mission),
        "next_mission_sha256": module.sha(next_mission),
        "ready_for_next": True,
        "blocker": "",
    }), encoding="utf-8")

    value = module._validate_receipt(
        {"project_root": tmp_path},
        receipt,
        "a" * 32,
        "review",
        "b" * 32,
        "c" * 64,
    )

    assert value["status"] == "REVISE"
    assert value["next_stage"] == "plan"
    assert value["_next_mission"] is None
    assert "cannot create a new plan" in value["_terminal_attention"]


def _review_receipt(
    module,
    tmp_path: Path,
    *,
    status: str,
    attempt: str,
    ids: list[str],
    next_stage: str,
    blocker: str = "",
) -> Path:
    output = tmp_path / f"{attempt}-output.md"
    output.write_text(status, encoding="utf-8")
    next_mission = tmp_path / f"{attempt}-next.md"
    next_mission.write_text(next_stage, encoding="utf-8")
    receipt = tmp_path / f"{attempt}-receipt.json"
    value = {
        "schema": module.RECEIPT_SCHEMA,
        "workflow_id": "a" * 32,
        "stage": "review",
        "attempt_id": attempt,
        "input_mission_sha256": "c" * 64,
        "status": status,
        "output_path": str(output),
        "output_sha256": module.sha(output),
        "next_stage": next_stage,
        "next_mission_path": str(next_mission),
        "next_mission_sha256": module.sha(next_mission),
        "ready_for_next": status != "FAIL",
        "blocker": blocker,
        "critical_finding_ids": ids,
        "critical_findings_sha256": module._finding_hash(ids),
    }
    receipt.write_text(json.dumps(value), encoding="utf-8")
    return receipt


def test_legacy_revise_never_creates_another_plan(tmp_path: Path) -> None:
    module = load()
    config = {
        "project_root": tmp_path,
        "_review_policy": module._default_review_policy(),
    }
    first = _review_receipt(
        module, tmp_path, status="REVISE", attempt="1" * 32,
        ids=["critical-input"], next_stage="plan",
    )
    second = _review_receipt(
        module, tmp_path, status="REVISE", attempt="2" * 32,
        ids=["critical-input"], next_stage="plan",
    )
    third = _review_receipt(
        module, tmp_path, status="REVISE", attempt="3" * 32,
        ids=["critical-input"], next_stage="plan",
    )

    values = [
        module._validate_receipt(config, first, "a" * 32, "review", "1" * 32, "c" * 64),
        module._validate_receipt(config, second, "a" * 32, "review", "2" * 32, "c" * 64),
        module._validate_receipt(config, third, "a" * 32, "review", "3" * 32, "c" * 64),
    ]

    assert all(value["_next_mission"] is None for value in values)
    assert all("cannot create a new plan" in value["_terminal_attention"] for value in values)
    assert config["_review_policy"]["plan_revisions_used"] == 0
    assert config["_review_policy"]["plan_revisions_remaining"] == 2


def test_review_mission_assigns_inline_plan_repair_and_exact_workspace_entry(tmp_path: Path) -> None:
    module = load()
    source = tmp_path / "검토-입력.md"
    source.write_text("계획을 검토하세요.", encoding="utf-8")
    config = {
        "project_root": tmp_path,
        "workflow_dir": tmp_path / "workflow",
        "_review_policy": {
            **module._default_review_policy(),
            "plan_revisions_used": 2,
            "plan_revisions_remaining": 0,
        },
    }

    mission, _, _, _ = module._stage_mission(
        config, "a" * 32, 2, "review", source, "b" * 32,
        module.sha(source), source.read_bytes(),
    )
    text = mission.read_text(encoding="utf-8")

    assert f"exact_project_root={tmp_path}" in text
    assert f"exact_input_mission_path={source}" in text
    assert "retry the same exact root at most once" in text
    assert "Never substitute a parent root, child directory" in text
    assert "plan repair and finalization owner" in text
    assert "write the corrected final plan as your output" in text
    assert "next_stage=implementation" in text
    assert "REVISE is legacy compatibility only" in text
    assert "review_repair_owner=review" in text
    assert "new_plan_transition_allowed=false" in text
    assert "plan_revisions_remaining=0" in text


def test_pass_with_notes_proceeds_to_implementation(tmp_path: Path) -> None:
    module = load()
    receipt = _review_receipt(
        module, tmp_path, status="PASS_WITH_NOTES", attempt="4" * 32,
        ids=[], next_stage="implementation",
    )
    value = module._validate_receipt(
        {"project_root": tmp_path},
        receipt,
        "a" * 32,
        "review",
        "4" * 32,
        "c" * 64,
    )
    assert value["status"] == "PASS_WITH_NOTES"
    assert value["next_stage"] == "implementation"


def test_legacy_revise_is_terminal_and_duplicate_finding_ids_are_rejected(tmp_path: Path) -> None:
    module = load()
    config = {
        "project_root": tmp_path,
        "_review_policy": {
            **module._default_review_policy(),
            "plan_revisions_used": 1,
            "plan_revisions_remaining": 1,
            "baseline_critical_finding_ids": ["fixed-a"],
            "baseline_critical_findings_sha256": module._finding_hash(["fixed-a"]),
        },
    }
    added = _review_receipt(
        module, tmp_path, status="REVISE", attempt="5" * 32,
        ids=["new-b"], next_stage="plan",
    )
    added_value = module._validate_receipt(
        config, added, "a" * 32, "review", "5" * 32, "c" * 64
    )
    assert added_value["_next_mission"] is None
    assert "cannot create a new plan" in added_value["_terminal_attention"]

    duplicate = json.loads(added.read_text(encoding="utf-8"))
    duplicate["attempt_id"] = "6" * 32
    duplicate["critical_finding_ids"] = ["fixed-a", "fixed-a"]
    duplicate["critical_findings_sha256"] = module._finding_hash(["fixed-a", "fixed-a"])
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(module.WorkflowError, match="unique and sorted"):
        module._validate_receipt(
            config, duplicate_path, "a" * 32, "review", "6" * 32, "c" * 64
        )


def test_active_scope_blocks_retry_workflow_and_exposes_revision_budget(tmp_path: Path) -> None:
    module = load()
    first_path = manifest(tmp_path)
    first = module.load_manifest(first_path)
    first["_review_policy"] = {
        **module._default_review_policy(),
        "plan_revisions_used": 2,
        "plan_revisions_remaining": 0,
    }
    module._claim_scope(first, first["workflow_id"])
    scope = module._json(module._scope_path(first))
    assert scope["review_policy"]["plan_revisions_remaining"] == 0

    second = dict(first)
    second["workflow_id"] = "b" * 32
    with pytest.raises(module.WorkflowError, match="recover that exact workflow"):
        module._claim_scope(second, second["workflow_id"])


def test_review_history_budget_spans_retry_workflow_directories(tmp_path: Path) -> None:
    module = load()
    root = tmp_path / "project"
    root.mkdir()
    for index, attempt in enumerate(("1" * 32, "2" * 32, "3" * 32), start=1):
        stage_dir = tmp_path / f"workflow-retry{index}" / "stages" / f"001-review-{attempt}"
        stage_dir.mkdir(parents=True)
        output = stage_dir / "review.md"
        output.write_text("critical", encoding="utf-8")
        receipt = {
            "schema": module.RECEIPT_SCHEMA,
            "workflow_id": str(index) * 32,
            "stage": "review",
            "attempt_id": attempt,
            "input_mission_sha256": "c" * 64,
            "status": "REVISE",
            "output_path": str(output),
            "output_sha256": module.sha(output),
            "next_stage": "plan",
            "next_mission_path": str(output),
            "next_mission_sha256": module.sha(output),
            "ready_for_next": True,
            "blocker": "",
        }
        (stage_dir / "stage-result.json").write_text(json.dumps(receipt), encoding="utf-8")

    config = {
        "project_root": root,
        "workflow_dir": tmp_path / "workflow-retry11",
    }
    policy = module._review_policy_from_history(config)

    assert policy["plan_revisions_used"] == 3
    assert policy["plan_revisions_remaining"] == 0
    assert policy["baseline_critical_finding_ids"] == [
        f"legacy-{module.sha(tmp_path / 'workflow-retry1' / 'stages' / ('001-review-' + '1' * 32) / 'review.md')[:24]}"
    ]


def test_blocked_plan_receipt_can_continue_to_bound_source_repair_plan(tmp_path: Path) -> None:
    module = load()
    output = tmp_path / "blocked-plan.md"
    output.write_text("source evidence is incomplete", encoding="utf-8")
    next_mission = tmp_path / "source-repair.md"
    next_mission.write_text("repair the source evidence", encoding="utf-8")
    receipt = tmp_path / "stage-result.json"
    receipt.write_text(json.dumps({
        "schema": module.RECEIPT_SCHEMA,
        "workflow_id": "a" * 32,
        "stage": "plan",
        "attempt_id": "b" * 32,
        "input_mission_sha256": "c" * 64,
        "status": "BLOCKED_PLAN",
        "output_path": str(output),
        "output_sha256": module.sha(output),
        "next_stage": "plan",
        "next_mission_path": str(next_mission),
        "next_mission_sha256": module.sha(next_mission),
        "ready_for_next": True,
        "blocker": "first-party historical rule evidence is incomplete",
    }), encoding="utf-8")

    value = module._validate_receipt(
        {"project_root": tmp_path},
        receipt,
        "a" * 32,
        "plan",
        "b" * 32,
        "c" * 64,
    )

    assert value["status"] == "BLOCKED_PLAN"
    assert value["next_stage"] == "plan"


def test_source_repair_plan_ready_receipt_can_continue_to_review(tmp_path: Path) -> None:
    module = load()
    output = tmp_path / "source-repair-plan.md"
    output.write_text("ready", encoding="utf-8")
    next_mission = tmp_path / "next-review.md"
    next_mission.write_text("review the source repair", encoding="utf-8")
    receipt = tmp_path / "stage-result.json"
    receipt.write_text(json.dumps({
        "schema": module.RECEIPT_SCHEMA,
        "workflow_id": "a" * 32,
        "stage": "plan",
        "attempt_id": "b" * 32,
        "input_mission_sha256": "c" * 64,
        "status": "SOURCE_REPAIR_PLAN_READY",
        "output_path": str(output),
        "output_sha256": module.sha(output),
        "next_stage": "review",
        "next_mission_path": str(next_mission),
        "next_mission_sha256": module.sha(next_mission),
        "ready_for_next": True,
        "blocker": "",
    }), encoding="utf-8")

    value = module._validate_receipt(
        {"project_root": tmp_path},
        receipt,
        "a" * 32,
        "plan",
        "b" * 32,
        "c" * 64,
    )

    assert value["status"] == "SOURCE_REPAIR_PLAN_READY"
    assert value["next_stage"] == "review"


def test_awaiting_receipt_rebind_advances_to_next_stage_without_replaying_plan(tmp_path: Path) -> None:
    module = load()
    calls = []
    review = tmp_path / "review.md"
    review.write_text("review", encoding="utf-8")

    def fake_execute(oracle_manifest: Path, *, dry_run: bool, **kwargs):
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

    result = run_workflow(module, manifest(tmp_path), oracle_execute=fake_execute)
    assert result["status"] == "attention_required"
    assert result["current_stage"] == "review"
    assert calls == ["plan", "review"]
    assert result["next_index"] == 1


def test_awaiting_relative_receipt_resumes_same_workflow_without_replaying_plan(tmp_path: Path) -> None:
    module = load()
    workflow_path = manifest(tmp_path)
    config = module.load_manifest(workflow_path)
    workflow_id = config["workflow_id"]
    attempt_id = "b" * 32
    initial = config["initial_mission_path"]
    mission, receipt_path, input_sha, _ = module._stage_mission(
        config, workflow_id, 0, "plan", initial, attempt_id,
        module.sha(initial), initial.read_bytes(),
    )
    output = mission.parent / "plan.md"
    review = mission.parent / "review.md"
    output.write_text("plan", encoding="utf-8")
    review.write_text("review", encoding="utf-8")
    receipt_path.write_text(json.dumps({
        "schema": module.RECEIPT_SCHEMA,
        "workflow_id": workflow_id,
        "stage": "plan",
        "attempt_id": attempt_id,
        "input_mission_sha256": input_sha,
        "status": "PLAN_READY",
        "output_path": str(output.relative_to(tmp_path)),
        "output_sha256": module.sha(output),
        "next_stage": "review",
        "next_mission_path": str(review.relative_to(tmp_path)),
        "next_mission_sha256": module.sha(review),
        "ready_for_next": True,
        "blocker": None,
    }), encoding="utf-8")
    state_path = module._state_path(config, workflow_id)
    module._write(state_path, {
        "schema": module.STATE_SCHEMA,
        "status": "awaiting_receipt",
        "workflow_id": workflow_id,
        "manifest_sha256": config["manifest_sha256"],
        "current_stage": "plan",
        "current_attempt_id": attempt_id,
        "current_input_sha256": input_sha,
        "current_mission_path": str(config["initial_mission_path"]),
        "receipt_path": str(receipt_path),
        "next_index": 0,
        "records": [],
    })
    calls: list[str] = []

    def review_only(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        data = json.loads(oracle_manifest.read_text(encoding="utf-8"))
        stage_mission = Path(data["mission_path"])
        text = stage_mission.read_text(encoding="utf-8")
        assert "stage=plan\n" not in text
        assert "stage=review\n" in text
        calls.append("review")
        return {"ok": False, "run_dir": str(_oracle_running_state(module, oracle_manifest))}

    result = run_workflow(module, workflow_path, oracle_execute=review_only)

    assert result["status"] == "attention_required"
    assert result["current_stage"] == "review"
    assert calls == ["review"]


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
        "manifest_sha256": module.sha(multi_source), "next_stage_result_path": str(receipt),
    }), encoding="utf-8")
    state_path = module._state_path(config, "a" * 32)
    module._write(state_path, {
        "schema": module.STATE_SCHEMA, "status": "running", "workflow_id": "a" * 32,
        "manifest_sha256": config["manifest_sha256"], "current_stage": "web-multi",
        "current_mission_path": str(multi_source), "next_index": 0, "records": [],
        "multi_execution_id": parent_id, "multi_manifest_sha256": module.sha(multi_source),
        "multi_result_path": str(result_path), "multi_receipt_path": str(receipt),
        "multi_terminal_status": "complete", "multi_result_sha256": module.sha(result_path),
        "multi_receipt_sha256": module.sha(receipt),
    })
    calls = 0

    def fake_oracle(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        nonlocal calls
        calls += 1
        return {"ok": False, "run_dir": str(_oracle_running_state(module, oracle_manifest))}

    def never_multi(*args, **kwargs):
        raise AssertionError("stored Web Multi result must be rebound, not resubmitted")

    result = run_workflow(module, path, oracle_execute=fake_oracle, multi_execute=never_multi)
    assert result["status"] == "attention_required"
    assert result["current_stage"] == "review"
    assert calls == 1
    assert result["records"][0]["parent_id"] == parent_id


def test_running_web_multi_resumes_exact_merger_through_persisted_terminal_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load()
    path = manifest(tmp_path)
    config = module.load_manifest(path)
    source = _bound_multi_manifest(module, tmp_path, with_receipt=True)
    source_sha = module.sha(source)
    multi_config = module.MULTI.load_manifest(
        source,
        expected_manifest_sha256=source_sha,
    )
    parent_id = "b" * 64
    result_path = multi_config["output_dir"] / "result.json"
    receipt_path = multi_config["next_stage_result_path"]
    module._write(multi_config["output_dir"] / "execution.json", {
        "status": "merger_ready",
    })
    state_path = module._state_path(config, config["workflow_id"])
    module._write(state_path, {
        "schema": module.STATE_SCHEMA,
        "status": "attention_required",
        "workflow_id": config["workflow_id"],
        "manifest_sha256": config["manifest_sha256"],
        "current_stage": "web-multi",
        "current_mission_path": str(source),
        "next_index": 0,
        "records": [],
        "multi_execution_id": parent_id,
        "multi_manifest_sha256": source_sha,
        "multi_result_path": str(result_path),
        "multi_receipt_path": str(receipt_path),
    })
    review = tmp_path / "recovered-review.md"
    output = tmp_path / "recovered-output.md"
    calls = {"resume": 0, "multi": 0}

    def resume_once(
        manifest_path: Path,
        *,
        expected_manifest_sha256: str,
        parent_id: str,
        parent_lock_held: bool,
        terminal_seal,
    ):
        calls["resume"] += 1
        assert manifest_path == source
        assert expected_manifest_sha256 == source_sha
        assert parent_id == "b" * 64
        assert parent_lock_held is True
        output.write_text("recovered", encoding="utf-8")
        review.write_text("review recovered merger", encoding="utf-8")
        receipt_path.write_text(json.dumps({
            "schema": module.RECEIPT_SCHEMA,
            "workflow_id": config["workflow_id"],
            "stage": "web-multi",
            "attempt_id": parent_id,
            "input_mission_sha256": source_sha,
            "status": "PASS",
            "output_path": str(output),
            "output_sha256": module.sha(output),
            "next_stage": "review",
            "next_mission_path": str(review),
            "next_mission_sha256": module.sha(review),
            "ready_for_next": True,
            "blocker": "",
        }), encoding="utf-8")
        terminal = {
            "schema": module.MULTI.RESULT_SCHEMA,
            "status": "complete",
            "parent_id": parent_id,
            "manifest_sha256": source_sha,
            "lanes": [],
            "next_stage_result_path": str(receipt_path),
        }
        raw = (json.dumps(terminal, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        terminal_seal(result_path, raw)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_bytes(raw)
        raise RuntimeError("simulated crash after recovered merger seal")

    def never_multi(*args, **kwargs):
        calls["multi"] += 1
        raise AssertionError("comprehensive recovery must not submit solvers")

    monkeypatch.setattr(module.MULTI, "resume_recovered_merger", resume_once)
    with pytest.raises(RuntimeError, match="simulated crash after recovered merger seal"):
        run_workflow(module, path, multi_execute=never_multi)

    sealed = module._json(state_path)
    assert calls == {"resume": 1, "multi": 0}
    assert sealed["multi_terminal_status"] == "complete"
    assert sealed["multi_result_sha256"] == module.sha(result_path)
    assert sealed["multi_receipt_sha256"] == module.sha(receipt_path)

    def no_replacement_resume(*args, **kwargs):
        raise AssertionError("persisted terminal seal must prevent replacement merger")

    def review_only(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        return {
            "ok": False,
            "run_dir": str(_oracle_running_state(module, oracle_manifest)),
        }

    monkeypatch.setattr(module.MULTI, "resume_recovered_merger", no_replacement_resume)
    recovered = run_workflow(
        module,
        path,
        oracle_execute=review_only,
        multi_execute=never_multi,
    )

    assert recovered["status"] == "attention_required"
    assert recovered["current_stage"] == "review"
    assert calls == {"resume": 1, "multi": 0}


def test_running_web_multi_preserves_producer_sealed_failure(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    config = module.load_manifest(path)
    multi_source = tmp_path / "multi.json"
    multi_source.write_text("{}", encoding="utf-8")
    receipt = tmp_path / "multi-receipt.json"
    result_path = tmp_path / "multi-result.json"
    parent_id = "b" * 64
    terminal = {
        "schema": module.MULTI.RESULT_SCHEMA,
        "status": "failed",
        "parent_id": parent_id,
        "manifest_sha256": module.sha(multi_source),
        "lanes": [],
    }
    result_path.write_text(json.dumps(terminal), encoding="utf-8")
    terminal_sha = module.sha(result_path)
    state_path = module._state_path(config, config["workflow_id"])
    module._write(state_path, {
        "schema": module.STATE_SCHEMA,
        "status": "running",
        "workflow_id": config["workflow_id"],
        "manifest_sha256": config["manifest_sha256"],
        "current_stage": "web-multi",
        "current_mission_path": str(multi_source),
        "next_index": 0,
        "records": [],
        "multi_execution_id": parent_id,
        "multi_manifest_sha256": module.sha(multi_source),
        "multi_result_path": str(result_path),
        "multi_receipt_path": str(receipt),
        "multi_terminal_status": "failed",
        "multi_result_sha256": terminal_sha,
    })

    def never_submit(*args, **kwargs):
        raise AssertionError("terminal Multi recovery must never resubmit")

    first = run_workflow(module, path, oracle_execute=never_submit, multi_execute=never_submit)
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert first["status"] == "attention_required"
    assert persisted["multi_terminal_status"] == "failed"
    assert persisted["multi_result_sha256"] == terminal_sha

    output = tmp_path / "multi-output.md"
    review = tmp_path / "review.md"
    output.write_text("merged", encoding="utf-8")
    review.write_text("review", encoding="utf-8")
    receipt.write_text(json.dumps({
        "schema": module.RECEIPT_SCHEMA,
        "workflow_id": config["workflow_id"],
        "stage": "web-multi",
        "attempt_id": parent_id,
        "input_mission_sha256": module.sha(multi_source),
        "status": "PASS",
        "output_path": str(output),
        "output_sha256": module.sha(output),
        "next_stage": "review",
        "next_mission_path": str(review),
        "next_mission_sha256": module.sha(review),
        "ready_for_next": True,
        "blocker": "",
    }), encoding="utf-8")
    result_path.write_text(json.dumps({
        "schema": module.MULTI.RESULT_SCHEMA,
        "status": "complete",
        "parent_id": parent_id,
        "manifest_sha256": module.sha(multi_source),
        "next_stage_result_path": str(receipt),
    }), encoding="utf-8")

    second = run_workflow(module, path, oracle_execute=never_submit, multi_execute=never_submit)
    assert second["status"] == "attention_required"
    assert second["recovery"]["error"] == "MULTI_TERMINAL_RESULT_CHANGED"
    assert second["multi_terminal_status"] == "failed"
    assert second["multi_result_sha256"] == terminal_sha


@pytest.mark.parametrize("replacement", ["result", "receipt"])
def test_web_multi_crash_after_host_seal_rejects_replacement(
    tmp_path: Path, replacement: str
) -> None:
    module = load()
    workflow = manifest(tmp_path)
    config = module.load_manifest(workflow)
    source = _bound_multi_manifest(module, tmp_path, with_receipt=True)
    source_sha = module.sha(source)
    state_path = module._state_path(config, config["workflow_id"])
    module._write(state_path, {
        "schema": module.STATE_SCHEMA,
        "status": "prepared",
        "workflow_id": config["workflow_id"],
        "manifest_sha256": config["manifest_sha256"],
        "next_stage": "web-multi",
        "next_mission_path": str(source),
        "next_mission_sha256": source_sha,
        "next_index": 1,
        "records": [],
    })

    def crashing_multi(path: Path, **kwargs):
        stored = module._json(state_path)
        receipt = Path(stored["multi_receipt_path"])
        output = tmp_path / "sealed-output.md"
        review = tmp_path / "sealed-review.md"
        output.write_text("sealed", encoding="utf-8")
        review.write_text("sealed review", encoding="utf-8")
        receipt_payload = {
            "schema": module.RECEIPT_SCHEMA,
            "workflow_id": config["workflow_id"],
            "stage": "web-multi",
            "attempt_id": kwargs["parent_id"],
            "input_mission_sha256": source_sha,
            "status": "PASS",
            "output_path": str(output),
            "output_sha256": module.sha(output),
            "next_stage": "review",
            "next_mission_path": str(review),
            "next_mission_sha256": module.sha(review),
            "ready_for_next": True,
            "blocker": "",
        }
        receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
        terminal = {
            "schema": module.MULTI.RESULT_SCHEMA,
            "status": "complete",
            "parent_id": kwargs["parent_id"],
            "manifest_sha256": kwargs["expected_manifest_sha256"],
            "lanes": [],
            "next_stage_result_path": str(receipt),
        }
        raw = (json.dumps(terminal, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        result_path = Path(stored["multi_result_path"])
        kwargs["terminal_seal"](result_path, raw)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        if replacement == "result":
            result_path.write_text(
                json.dumps({**terminal, "lanes": [{"id": "replacement"}]}),
                encoding="utf-8",
            )
        else:
            result_path.write_bytes(raw)
            replacement_output = tmp_path / "replacement-output.md"
            replacement_review = tmp_path / "replacement-review.md"
            replacement_output.write_text("replacement", encoding="utf-8")
            replacement_review.write_text("replacement review", encoding="utf-8")
            receipt.write_text(json.dumps({
                **receipt_payload,
                "output_path": str(replacement_output),
                "output_sha256": module.sha(replacement_output),
                "next_mission_path": str(replacement_review),
                "next_mission_sha256": module.sha(replacement_review),
            }), encoding="utf-8")
        raise RuntimeError("simulated crash after host seal")

    with pytest.raises(RuntimeError, match="simulated crash"):
        run_workflow(module, workflow, multi_execute=crashing_multi)

    sealed = module._json(state_path)
    assert sealed["multi_terminal_status"] == "complete"
    if replacement == "result":
        assert sealed["multi_result_sha256"] != module.sha(Path(sealed["multi_result_path"]))
        assert sealed["multi_receipt_sha256"] == module.sha(Path(sealed["multi_receipt_path"]))
    else:
        assert sealed["multi_result_sha256"] == module.sha(Path(sealed["multi_result_path"]))
        assert sealed["multi_receipt_sha256"] != module.sha(Path(sealed["multi_receipt_path"]))
    calls = {"multi": 0, "oracle": 0}

    def never_multi(*args, **kwargs):
        calls["multi"] += 1
        raise AssertionError("sealed Multi must not resubmit")

    def never_oracle(*args, **kwargs):
        calls["oracle"] += 1
        raise AssertionError("replacement result must not advance")

    recovered = run_workflow(module,
        workflow, multi_execute=never_multi, oracle_execute=never_oracle
    )
    assert recovered["status"] == "attention_required"
    assert recovered["recovery"]["error"] == (
        "MULTI_TERMINAL_RESULT_CHANGED" if replacement == "result" else "MULTI_RECEIPT_CHANGED"
    )
    assert recovered["multi_result_sha256"] == sealed["multi_result_sha256"]
    assert calls == {"multi": 0, "oracle": 0}


@pytest.mark.parametrize("mutation", ["parent", "manifest", "receipt"])
def test_web_multi_recovery_rejects_persisted_identity_drift(tmp_path: Path, mutation: str) -> None:
    module = load()
    parent_id = "b" * 64
    manifest_sha = "c" * 64
    receipt = tmp_path / "receipt.json"
    other_receipt = tmp_path / "other-receipt.json"
    result_path = tmp_path / "result.json"
    result = {
        "schema": module.MULTI.RESULT_SCHEMA, "status": "complete", "parent_id": parent_id,
        "manifest_sha256": manifest_sha, "next_stage_result_path": str(receipt),
    }
    if mutation == "parent":
        result["parent_id"] = "d" * 64
    elif mutation == "manifest":
        result["manifest_sha256"] = "d" * 64
    else:
        result["next_stage_result_path"] = str(other_receipt)
    result_path.write_text(json.dumps(result), encoding="utf-8")
    recovered = module._recover_exact_multi_stage({
        "multi_result_path": str(result_path), "multi_execution_id": parent_id,
        "multi_manifest_sha256": manifest_sha, "multi_receipt_path": str(receipt),
        "multi_terminal_status": "complete", "multi_result_sha256": module.sha(result_path),
        "multi_receipt_sha256": "a" * 64,
    })
    assert recovered == {"ok": False, "error": "MULTI_RESULT_IDENTITY_INVALID"}


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


def _pro_attachment_mission(module, tmp_path: Path, attachments: list[dict[str, str]]) -> Path:
    mission = tmp_path / "pro-next.md"
    mission.write_text(
        "Pro decision mission\n\n"
        "[PRO_ATTACHMENT_CONTRACT]\n"
        + json.dumps({"schema": module.PRO_ATTACHMENT_SCHEMA, "attachments": attachments})
        + "\n[/PRO_ATTACHMENT_CONTRACT]\n",
        encoding="utf-8",
    )
    return mission


def test_pro_attachment_contract_includes_only_declared_exact_packet(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    config["_parallel_parent_id"] = "b" * 64
    packet = tmp_path / "packet.zip"
    packet.write_bytes(b"exact packet")
    source = _pro_attachment_mission(module, tmp_path, [{"path": str(packet), "sha256": module.sha(packet)}])
    extras = module._declared_pro_attachments(config, source, source.read_bytes())
    augmented = tmp_path / "augmented-mission.md"
    augmented.write_text("bound pro mission", encoding="utf-8")
    payload = json.loads(module._oracle_manifest(
        config, augmented, tmp_path, "c" * 32, stage="pro", pro_attachments=extras,
        mission_sha=module.sha(augmented),
    ).read_text(encoding="utf-8"))
    context_packet = assert_pro_context(module, payload, augmented, (packet.resolve(),))
    assert context_packet != packet.resolve()


def test_pro_attachment_contract_rejects_hash_mismatch_before_submission(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    packet = tmp_path / "packet.zip"
    packet.write_bytes(b"exact packet")
    source = _pro_attachment_mission(module, tmp_path, [{"path": str(packet), "sha256": "0" * 64}])
    with pytest.raises(module.WorkflowError, match="hash mismatch"):
        module._declared_pro_attachments(config, source, source.read_bytes())

    uppercase = _pro_attachment_mission(
        module, tmp_path, [{"path": str(packet), "sha256": "A" * 64}]
    )
    with pytest.raises(module.WorkflowError, match="exact lowercase"):
        module._declared_pro_attachments(config, uppercase, uppercase.read_bytes())


def test_pro_declared_evidence_hash_mismatch_is_rejected_before_pro_submission(tmp_path: Path) -> None:
    module = load()
    workflow = manifest(tmp_path, allow_pro=True)
    calls = []
    packet = tmp_path / "evidence.zip"
    packet.write_bytes(b"evidence packet")

    def fake_execute(path: Path, *, dry_run: bool, **kwargs):
        payload = json.loads(path.read_text(encoding="utf-8"))
        mission = Path(payload["mission_path"])
        text = mission.read_text(encoding="utf-8")
        stage = next(item for item in ("plan", "pro") if f"stage={item}\n" in text)
        calls.append(stage)
        if stage != "plan":
            raise AssertionError("Pro submission must not happen with a mismatched evidence hash")
        attempt = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("attempt_id="))
        input_sha = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("input_mission_sha256="))
        output = mission.parent / "plan-output.md"
        output.write_text("plan", encoding="utf-8")
        next_mission = _pro_attachment_mission(
            module, tmp_path, [{"path": str(packet), "sha256": "0" * 64}]
        )
        (mission.parent / "stage-result.json").write_text(json.dumps({
            "schema": module.RECEIPT_SCHEMA, "workflow_id": "a" * 32, "stage": "plan",
            "attempt_id": attempt, "input_mission_sha256": input_sha, "status": "PASS",
            "output_path": str(output), "output_sha256": module.sha(output),
            "next_stage": "pro", "next_mission_path": str(next_mission),
            "next_mission_sha256": module.sha(next_mission), "ready_for_next": True, "blocker": "",
        }), encoding="utf-8")
        return {"ok": True, "run_dir": str(mission.parent / "run")}

    with pytest.raises(module.WorkflowError, match="hash mismatch"):
        run_workflow(module, workflow, oracle_execute=fake_execute)
    assert calls == ["plan"]


def test_pro_attachment_contract_rejects_outside_project_and_symlink(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    outside = tmp_path.parent / "outside-packet.zip"
    outside.write_bytes(b"outside")
    source = _pro_attachment_mission(module, tmp_path, [{"path": str(outside)}])
    with pytest.raises(module.WorkflowError, match="outside project"):
        module._declared_pro_attachments(config, source, source.read_bytes())

    target = tmp_path / "packet.zip"
    target.write_bytes(b"packet")
    link = tmp_path / "packet-link.zip"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    source = _pro_attachment_mission(module, tmp_path, [{"path": str(link)}])
    with pytest.raises(module.WorkflowError, match="non-symlink"):
        module._declared_pro_attachments(config, source, source.read_bytes())


def test_pro_rejects_empty_transition_before_submission(tmp_path: Path) -> None:
    module = load()
    workflow = manifest(tmp_path)
    config = module.load_manifest(workflow)
    source = tmp_path / "empty-pro.md"
    source.write_text(" \n\t", encoding="utf-8")
    module._write(module._state_path(config, "a" * 32), {
        "schema": module.STATE_SCHEMA, "status": "prepared", "workflow_id": "a" * 32,
        "manifest_sha256": config["manifest_sha256"], "next_stage": "pro",
        "next_mission_path": str(source), "next_mission_sha256": module.sha(source),
        "next_index": 1, "records": [],
    })
    calls = 0

    def never_submit(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("empty Pro transition must fail before submission")

    with pytest.raises(module.WorkflowError, match="nonempty"):
        run_workflow(module, workflow, oracle_execute=never_submit)
    assert calls == 0


def test_regular_stage_rejects_source_mutation_before_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    workflow = manifest(tmp_path)
    config = module.load_manifest(workflow)
    source = tmp_path / "implementation.md"
    source.write_text("Implement the bound plan", encoding="utf-8")
    module._write(module._state_path(config, "a" * 32), {
        "schema": module.STATE_SCHEMA, "status": "prepared", "workflow_id": "a" * 32,
        "manifest_sha256": config["manifest_sha256"], "next_stage": "implementation",
        "next_mission_path": str(source), "next_mission_sha256": module.sha(source),
        "next_index": 1, "records": [],
    })
    original = module._bound_source_bytes

    def mutate_before_snapshot(path: Path, expected_sha: str) -> bytes:
        source.write_text("mutated implementation", encoding="utf-8")
        return original(path, expected_sha)

    monkeypatch.setattr(module, "_bound_source_bytes", mutate_before_snapshot)
    calls = 0

    def never_submit(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("mutated regular source must fail before submission")

    with pytest.raises(module.WorkflowError, match="changed after validation"):
        run_workflow(module, workflow, oracle_execute=never_submit)
    assert calls == 0


def test_pro_rejects_ancestor_symlink_for_transition_and_evidence(tmp_path: Path) -> None:
    module = load()
    workflow = manifest(tmp_path)
    config = module.load_manifest(workflow)
    target = tmp_path / "real"
    target.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    packet = target / "packet.zip"
    packet.write_bytes(b"packet")
    declaration = _pro_attachment_mission(module, tmp_path, [{"path": str(alias / packet.name)}])
    with pytest.raises(module.WorkflowError, match="non-symlink"):
        module._declared_pro_attachments(config, declaration, declaration.read_bytes())

    root_packet = tmp_path / "root-packet.zip"
    root_packet.write_bytes(b"root packet")
    traversal = _pro_attachment_mission(
        module, tmp_path, [{"path": str(alias / ".." / root_packet.name)}]
    )
    with pytest.raises(module.WorkflowError, match="parent traversal|non-symlink"):
        module._declared_pro_attachments(config, traversal, traversal.read_bytes())

    source = alias / "pro.md"
    source.write_text("Pro transition", encoding="utf-8")
    module._write(module._state_path(config, "a" * 32), {
        "schema": module.STATE_SCHEMA, "status": "prepared", "workflow_id": "a" * 32,
        "manifest_sha256": config["manifest_sha256"], "next_stage": "pro",
        "next_mission_path": str(source), "next_mission_sha256": module.sha(source),
        "next_index": 1, "records": [],
    })
    calls = 0

    def never_submit(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("symlinked Pro transition must fail before submission")

    with pytest.raises(module.WorkflowError, match="non-symlink"):
        run_workflow(module, workflow, oracle_execute=never_submit)
    assert calls == 0


@pytest.mark.parametrize("mutation", ["source", "evidence", "mission"])
def test_pro_packet_rejects_mutation_after_hash_validation(tmp_path: Path, mutation: str) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    config["_parallel_parent_id"] = "b" * 64
    source = tmp_path / "pro-source.md"
    source.write_text("Pro transition", encoding="utf-8")
    expected_source_sha = module.sha(source)
    mission, _, _, mission_sha = module._pro_stage_mission(
        config, "a" * 32, 1, source, "c" * 32, expected_source_sha, source.read_bytes()
    )
    if mutation == "source":
        evidence = ((source, expected_source_sha),)
        source.write_text("mutated transition", encoding="utf-8")
    elif mutation == "evidence":
        packet = tmp_path / "packet.zip"
        packet.write_bytes(b"validated packet")
        declaration = _pro_attachment_mission(
            module, tmp_path, [{"path": str(packet), "sha256": module.sha(packet)}]
        )
        evidence = module._declared_pro_attachments(config, declaration, declaration.read_bytes())
        packet.write_bytes(b"mutated packet")
    else:
        evidence = ((source, expected_source_sha),)
        mission.write_text("mutated augmented mission", encoding="utf-8")
    expected_error = "STALE_MISSION_HASH" if mutation == "mission" else "STALE_HASH"
    with pytest.raises(module.WorkflowError, match=expected_error):
        module._oracle_manifest(
            config, mission, mission.parent, "c" * 32, stage="pro", pro_attachments=evidence,
            mission_sha=mission_sha,
        )
    if mutation == "source":
        assert module.sha(source) != expected_source_sha


def test_pro_retry_reuses_frozen_optional_attachment_hash(tmp_path: Path) -> None:
    module = load()
    workflow = manifest(tmp_path)
    config = module.load_manifest(workflow)
    source = tmp_path / "pro-source.md"
    source.write_text("Pro transition", encoding="utf-8")
    packet = tmp_path / "packet.zip"
    packet.write_bytes(b"validated packet")
    expected_packet_sha = module.sha(packet)
    module._write(module._state_path(config, "a" * 32), {
        "schema": module.STATE_SCHEMA, "status": "prepared", "workflow_id": "a" * 32,
        "manifest_sha256": config["manifest_sha256"], "next_stage": "pro",
        "next_mission_path": str(source), "next_mission_sha256": module.sha(source),
        "pro_attachments": [{"path": str(packet), "sha256": expected_packet_sha}],
        "next_index": 1, "records": [],
    })
    packet.write_bytes(b"mutated packet")
    calls = 0

    def never_submit(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("stale retry evidence must fail before submission")

    with pytest.raises(module.WorkflowError, match="STALE_HASH"):
        run_workflow(module, workflow, oracle_execute=never_submit)
    assert calls == 0


def test_recovered_pro_pre_submit_retry_preserves_frozen_attachment_hash(tmp_path: Path) -> None:
    module = load()
    workflow = manifest(tmp_path)
    config = module.load_manifest(workflow)
    config["_parallel_parent_id"] = "b" * 64
    source = tmp_path / "pro-source.md"
    source.write_text("Pro transition", encoding="utf-8")
    source_sha = module.sha(source)
    packet = tmp_path / "packet.zip"
    packet.write_bytes(b"validated packet")
    packet_sha = module.sha(packet)
    attempt = "c" * 32
    mission, receipt, input_sha, mission_sha = module._pro_stage_mission(
        config, "a" * 32, 1, source, attempt, source_sha, source.read_bytes()
    )
    oracle_manifest = module._oracle_manifest(
        config, mission, mission.parent, attempt, stage="pro",
        pro_attachments=((packet, packet_sha),), mission_sha=mission_sha,
    )
    run_dir = _oracle_running_state(module, oracle_manifest)
    (run_dir / "stdout.log").write_text(
        "ERROR: ChatGPT app mention suggestion did not appear.\n", encoding="utf-8"
    )
    module._write(module._state_path(config, "a" * 32), {
        "schema": module.STATE_SCHEMA, "status": "running", "workflow_id": "a" * 32,
        "manifest_sha256": config["manifest_sha256"], "current_stage": "pro",
        "current_attempt_id": attempt, "current_input_sha256": input_sha,
        "current_mission_path": str(source), "receipt_path": str(receipt),
        "current_binding_source_path": str(source), "current_binding_source_sha256": source_sha,
        "current_augmented_mission_path": str(mission),
        "current_augmented_mission_sha256": mission_sha,
        "pro_attachments": [{"path": str(packet), "sha256": packet_sha}],
        "oracle_run_id": attempt, "oracle_run_dir": str(run_dir),
        "oracle_manifest_path": str(oracle_manifest), "next_index": 1,
        "records": [], "pre_submit_retries": 0,
    })
    packet.write_bytes(b"mutated packet")
    calls = 0

    def never_submit(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("stale recovered evidence must fail before resubmission")

    with pytest.raises(module.WorkflowError, match="STALE_HASH"):
        run_workflow(module,
            workflow,
            oracle_execute=never_submit,
            oracle_recover=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("proven pre-submit failure must not recover")
            ),
        )
    assert calls == 0


def test_regular_manifest_ignores_pro_evidence_and_pro_uses_one_context_packet(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    config["_parallel_parent_id"] = "b" * 64
    mission = tmp_path / "mission.md"
    mission.write_text("mission", encoding="utf-8")
    packet = tmp_path / "packet.zip"
    packet.write_bytes(b"packet")
    regular = json.loads(module._oracle_manifest(
        config, mission, tmp_path / "regular", "c" * 32, stage="plan",
        pro_attachments=(packet,), mission_sha=module.sha(mission),
    ).read_text(encoding="utf-8"))
    assert "attachments" not in regular
    assert regular["transport"] == "devspace"
    assert regular["mission_sha256"] == module.sha(mission)
    pro = json.loads(module._oracle_manifest(
        config, mission, tmp_path / "pro", "d" * 32, stage="pro",
        pro_attachments=((packet, module.sha(packet)),), mission_sha=module.sha(mission),
    ).read_text(encoding="utf-8"))
    assert pro["mission_sha256"] == module.sha(mission)
    assert_pro_context(module, pro, mission, (packet,))


def test_plan_mission_teaches_declared_packet_contract(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    initial = config["initial_mission_path"]
    mission, _, _, _ = module._stage_mission(
        config, "a" * 32, 0, "plan", initial, "b" * 32,
        module.sha(initial), initial.read_bytes(),
    )
    text = mission.read_text(encoding="utf-8")
    assert "[PRO_ATTACHMENT_AUTHORING_CONTRACT]" in text
    assert module.PRO_ATTACHMENT_SCHEMA in text
    assert "every solver entry" in text
    assert "mission_sha256" in text
    assert "merger_mission_sha256" in text
    assert "Canonical plan receipt status is PLAN_READY" in text
    assert "output_path and next_mission_path MUST be absolute paths" in text


def test_plan_mission_declares_pro_selection_policy(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    initial = config["initial_mission_path"]
    mission, _, _, _ = module._stage_mission(
        config, "a" * 32, 0, "plan", initial, "b" * 32,
        module.sha(initial), initial.read_bytes(),
    )
    text = mission.read_text(encoding="utf-8")
    assert "[PRO_SELECTION_POLICY]" in text
    assert "pro_selection_allowed=false" in text
    assert "Do not emit next_stage=pro; continue with review or an authorized web-multi stage." in text

    opted_in = module.load_manifest(manifest(tmp_path, allow_pro=True))
    initial = opted_in["initial_mission_path"]
    mission, _, _, _ = module._stage_mission(
        opted_in, "a" * 32, 0, "plan", initial, "b" * 32,
        module.sha(initial), initial.read_bytes(),
    )
    text = mission.read_text(encoding="utf-8")
    assert "pro_selection_allowed=true" in text
    assert "Do not emit next_stage=pro" not in text


def test_receipt_compatibly_resolves_project_relative_paths_with_hash_binding(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    output = tmp_path / "artifacts" / "plan.md"
    next_mission = tmp_path / "missions" / "review.md"
    output.parent.mkdir(parents=True)
    next_mission.parent.mkdir(parents=True)
    output.write_text("plan", encoding="utf-8")
    next_mission.write_text("review", encoding="utf-8")
    receipt_path = tmp_path / "stage-result.json"
    receipt_path.write_text(json.dumps({
        "schema": module.RECEIPT_SCHEMA,
        "workflow_id": "a" * 32,
        "stage": "plan",
        "attempt_id": "b" * 32,
        "input_mission_sha256": "c" * 64,
        "status": "PLAN_READY",
        "output_path": str(output.relative_to(tmp_path)),
        "output_sha256": module.sha(output),
        "next_stage": "review",
        "next_mission_path": str(next_mission.relative_to(tmp_path)),
        "next_mission_sha256": module.sha(next_mission),
        "ready_for_next": True,
        "blocker": None,
    }), encoding="utf-8")

    value = module._validate_receipt(
        config, receipt_path, "a" * 32, "plan", "b" * 32, "c" * 64
    )

    assert value["output_path"] == str(output.resolve())
    assert value["next_mission_path"] == str(next_mission.resolve())
    assert value["_next_mission"] == next_mission.resolve()
    assert set(value["_receipt_path_compatibility"]) == {"output_path", "next_mission_path"}
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted["output_path"] == str(output.relative_to(tmp_path))
    assert persisted["next_mission_path"] == str(next_mission.relative_to(tmp_path))


def test_receipt_relative_path_escape_remains_fail_closed(tmp_path: Path) -> None:
    module = load()
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(module.WorkflowError, match="path outside project"):
        module._receipt_path(root.resolve(), Path("..") / outside.name)


def test_completed_plan_receipt_is_compatibly_normalized_only_when_fully_valid(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path, allow_pro=True))
    output = tmp_path / "plan-output.md"
    next_mission = tmp_path / "pro-next.md"
    output.write_text("plan", encoding="utf-8")
    next_mission.write_text("pro", encoding="utf-8")
    receipt_path = tmp_path / "stage-result.json"
    receipt = {
        "schema": module.RECEIPT_SCHEMA,
        "workflow_id": "a" * 32,
        "stage": "plan",
        "attempt_id": "b" * 32,
        "input_mission_sha256": "c" * 64,
        "status": "completed",
        "output_path": str(output),
        "output_sha256": module.sha(output),
        "next_stage": "pro",
        "next_mission_path": str(next_mission),
        "next_mission_sha256": module.sha(next_mission),
        "ready_for_next": True,
        "blocker": "",
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    value = module._validate_receipt(
        config, receipt_path, "a" * 32, "plan", "b" * 32, "c" * 64
    )
    assert value["_receipt_status_original"] == "completed"
    assert value["_receipt_status_normalized"] == "PLAN_READY"
    assert value["_next_mission"] == next_mission.resolve()

    receipt["next_mission_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(module.WorkflowError, match="next mission hash mismatch"):
        module._validate_receipt(config, receipt_path, "a" * 32, "plan", "b" * 32, "c" * 64)


def test_regular_stage_rejects_pro_attachment_contract_before_submission(tmp_path: Path) -> None:
    module = load()
    workflow = manifest(tmp_path)
    config = module.load_manifest(workflow)
    config["initial_mission_path"].write_text(
        "regular plan\n[PRO_ATTACHMENT_CONTRACT]\n{}\n[/PRO_ATTACHMENT_CONTRACT]\n",
        encoding="utf-8",
    )
    payload = json.loads(workflow.read_text(encoding="utf-8"))
    payload["initial_mission_path"] = str(config["initial_mission_path"])
    payload["initial_mission_sha256"] = module.sha(config["initial_mission_path"])
    workflow.write_text(json.dumps(payload), encoding="utf-8")
    calls = 0

    def never_submit(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("regular stage contract must fail before submission")

    with pytest.raises(module.WorkflowError, match="forbidden for regular DevSpace stages"):
        run_workflow(module, workflow, oracle_execute=never_submit)
    assert calls == 0


def _pro_envelope(module, *, workflow_id: str = "a" * 32, output_text: str = "decision") -> dict[str, object]:
    return {
        "schema": module.PRO_OUTPUT_SCHEMA,
        "workflow_id": workflow_id,
        "stage": "pro",
        "attempt_id": "b" * 32,
        "input_mission_sha256": "c" * 64,
        "status": "PASS",
        "output_text": output_text,
        "next_stage": "review",
        "next_mission_text": "Review the exact Pro decision.",
        "ready_for_next": True,
        "blocker": "",
    }


def _malformed_pro_output(module, *, workflow_id: str = "a" * 32, truncated: bool = False) -> str:
    value = _pro_envelope(module, workflow_id=workflow_id)
    prefix = {
        key: value[key]
        for key in module.PRO_OUTPUT_PREFIX_KEYS
    }
    serialized = json.dumps(prefix, ensure_ascii=False, separators=(",", ":"))
    nested = 'Decision body\\n\\n{\\n  "schema": "nested/v1",\\n  "verdict": "PASS"\\n}'
    tail = (
        ',"next_stage":"review","next_mission_text":"Review the exact Pro decision.",'
        '"ready_for_next":true,"blocker":""}'
    )
    text = serialized[:-1] + ',"output_text":"' + nested + '"' + tail
    return text[:-24] if truncated else text


def test_malformed_nested_json_in_pro_output_is_recovered_with_audit_receipt(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    stage_dir = tmp_path / "pro-stage"
    stage_dir.mkdir()
    output = stage_dir / "oracle-output.md"
    output.write_text(_malformed_pro_output(module), encoding="utf-8")
    receipt = stage_dir / "stage-result.json"
    module._materialize_pro_receipt(
        config,
        receipt,
        "a" * 32,
        "b" * 32,
        "c" * 64,
        {"output_path": str(output)},
    )
    value = json.loads(receipt.read_text(encoding="utf-8"))
    recovered = value["pro_output_recovery"]
    assert recovered["schema"] == module.PRO_OUTPUT_RECOVERY_SCHEMA
    assert recovered["source_output_sha256"] == module.sha(output)
    assert recovered["strict_error_position"] > 0
    assert '"schema": "nested/v1"' in Path(value["output_path"]).read_text(encoding="utf-8")


def test_malformed_pro_output_recovery_rejects_identity_mismatch(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    output = tmp_path / "oracle-output.md"
    output.write_text(_malformed_pro_output(module, workflow_id="d" * 32), encoding="utf-8")
    receipt = tmp_path / "stage-result.json"
    with pytest.raises(module.WorkflowError, match="identity mismatch"):
        module._materialize_pro_receipt(
            config, receipt, "a" * 32, "b" * 32, "c" * 64, {"output_path": str(output)}
        )
    assert not receipt.exists()


def test_truncated_malformed_pro_output_remains_fail_closed(tmp_path: Path) -> None:
    module = load()
    output = tmp_path / "oracle-output.md"
    output.write_text(_malformed_pro_output(module, truncated=True), encoding="utf-8")
    with pytest.raises(module.WorkflowError, match="ambiguous|incomplete"):
        module._load_pro_envelope(output)


def test_strict_pro_output_uses_original_parser_without_recovery_metadata(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    stage_dir = tmp_path / "strict-pro"
    stage_dir.mkdir()
    output = stage_dir / "oracle-output.md"
    output.write_text(json.dumps(_pro_envelope(module), ensure_ascii=False), encoding="utf-8")
    receipt = stage_dir / "stage-result.json"
    module._materialize_pro_receipt(
        config, receipt, "a" * 32, "b" * 32, "c" * 64, {"output_path": str(output)}
    )
    value = json.loads(receipt.read_text(encoding="utf-8"))
    assert "pro_output_recovery" not in value
    assert value["status"] == "PASS"


def test_web_multi_preflight_failure_stays_prepared_and_rejects_changed_mission(tmp_path: Path) -> None:
    module = load()
    workflow_path = manifest(tmp_path)
    invalid_multi = tmp_path / "multi.json"
    invalid_multi.write_text(json.dumps({"next_stage_binding": {"workflow_id": "wrong", "stage": "web-multi"}}), encoding="utf-8")

    def fake_plan(oracle_manifest: Path, *, dry_run: bool, **kwargs):
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
        run_workflow(module, workflow_path, oracle_execute=fake_plan)
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
        "solvers": [
            {"id": "one", "mission_path": str(lane_one), "mission_sha256": module.sha(lane_one)},
            {"id": "two", "mission_path": str(lane_two), "mission_sha256": module.sha(lane_two)},
        ],
        "merger_mission_path": str(merger), "merger_mission_sha256": module.sha(merger),
        "next_stage_binding": {"workflow_id": "a" * 32, "stage": "web-multi"},
    }), encoding="utf-8")
    calls = 0

    def fake_multi(path: Path, **kwargs):
        nonlocal calls
        calls += 1
        return {"ok": False, "parent_id": "d" * 64}

    with pytest.raises(module.WorkflowError, match="prepared next mission changed"):
        run_workflow(module, workflow_path, oracle_execute=fake_plan, multi_execute=fake_multi)
    assert calls == 0


def test_stage_contract_preserves_upstream_input_mission_hash_semantics(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    config = module.load_manifest(path)
    source = config["initial_mission_path"]
    mission, _, input_sha, _ = module._stage_mission(
        config,
        config["workflow_id"],
        0,
        "plan",
        source,
        "b" * 32,
        module.sha(source),
        source.read_bytes(),
    )
    text = mission.read_text(encoding="utf-8")

    assert input_sha == module.sha(source)
    assert f"input_mission_sha256={input_sha}" in text
    assert "binds the upstream source mission bytes" in text
    assert "do not replace it with a hash of this augmented mission.md" in text


def test_receipt_accepts_legacy_schema_version_but_keeps_upstream_hash_binding(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    config = module.load_manifest(path)
    source = config["initial_mission_path"]
    mission, receipt_path, input_sha, _ = module._stage_mission(
        config, config["workflow_id"], 0, "plan", source, "b" * 32,
        module.sha(source), source.read_bytes(),
    )
    output = config["project_root"] / "plan.md"
    output.write_text("plan", encoding="utf-8")
    next_mission = config["project_root"] / "review.md"
    next_mission.write_text("review", encoding="utf-8")
    receipt_path.write_text(json.dumps({
        "schema_version": module.RECEIPT_SCHEMA,
        "workflow_id": config["workflow_id"],
        "stage": "plan",
        "attempt_id": "b" * 32,
        "input_mission_sha256": input_sha,
        "status": "PLAN_READY",
        "output_path": str(output),
        "output_sha256": module.sha(output),
        "next_stage": "review",
        "next_mission_path": str(next_mission),
        "next_mission_sha256": module.sha(next_mission),
        "ready_for_next": True,
        "blocker": None,
    }), encoding="utf-8")

    receipt = module._validate_receipt(
        config, receipt_path, config["workflow_id"], "plan", "b" * 32, input_sha
    )
    assert receipt["_next_mission"] == next_mission

    value = json.loads(receipt_path.read_text(encoding="utf-8"))
    value["input_mission_sha256"] = module.sha(mission)
    receipt_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(module.WorkflowError, match="stage receipt identity mismatch"):
        module._validate_receipt(
            config, receipt_path, config["workflow_id"], "plan", "b" * 32, input_sha
        )


def test_receipt_rejects_conflicting_schema_aliases(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)
    config = module.load_manifest(path)
    receipt = config["project_root"] / "stage-result.json"
    receipt.write_text(json.dumps({
        "schema": module.RECEIPT_SCHEMA,
        "schema_version": "different",
    }), encoding="utf-8")
    with pytest.raises(module.WorkflowError, match="schema keys conflict"):
        module._validate_receipt(
            config, receipt, config["workflow_id"], "plan", "b" * 32, "c" * 64
        )

    receipt.write_text(json.dumps({
        "schema": None,
        "schema_version": module.RECEIPT_SCHEMA,
    }), encoding="utf-8")
    with pytest.raises(module.WorkflowError, match="schema keys conflict"):
        module._validate_receipt(
            config, receipt, config["workflow_id"], "plan", "b" * 32, "c" * 64
        )


def test_awaiting_receipt_preserves_source_and_augmented_mission_bindings(tmp_path: Path) -> None:
    module = load()
    path = manifest(tmp_path)

    def fake_oracle(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        data = json.loads(oracle_manifest.read_text(encoding="utf-8"))
        mission = Path(data["mission_path"])
        contract = mission.read_text(encoding="utf-8")
        receipt_path = Path(next(
            line.split(": ", 1)[1]
            for line in contract.splitlines()
            if line.startswith("Write the small UTF-8 stage receipt to: ")
        ))
        return {"ok": True, "run_dir": str(receipt_path.parent / "oracle-run")}

    result = run_workflow(module, path, oracle_execute=fake_oracle)
    assert result["status"] == "awaiting_receipt"
    source = Path(result["current_binding_source_path"])
    augmented = Path(result["current_augmented_mission_path"])
    assert result["current_binding_source_sha256"] == module.sha(source)
    assert result["current_augmented_mission_sha256"] == module.sha(augmented)
    assert result["current_binding_source_sha256"] != result["current_augmented_mission_sha256"]


def _user_stop_fixture(module, tmp_path: Path) -> dict[str, object]:
    path = manifest(tmp_path)
    config = module.load_manifest(path)
    config["_review_policy"] = module._default_review_policy()
    workflow_id = str(config["workflow_id"])
    run_id = "b" * 32
    workflow_path = module._state_path(config, workflow_id)
    scope_path = module._scope_path(config)
    project_key = workflow_path.parent.name
    run_dir = (
        module.RUNNER.STATE.oracle_state_root()
        / "projects"
        / project_key
        / "runs"
        / run_id
    )
    run_dir.mkdir(parents=True)
    run_state_path = run_dir / "state.json"
    module._write(run_state_path, {
        "schema": module.RUNNER.STATE.STATE_SCHEMA,
        "run_id": run_id,
        "project_root": str(config["project_root"]),
        "status": "attention_required",
        "transport_status": "complete",
        "session_authority": "terminal",
        "terminal_harvested": True,
        "task_outcome": "blocked",
        "exit_code": 0,
    })
    (run_dir / "stdout.log").write_bytes(b"")
    (run_dir / "stderr.log").write_bytes(b"")
    module._write(workflow_path, {
        "schema": module.STATE_SCHEMA,
        "status": "attention_required",
        "workflow_id": workflow_id,
        "manifest_sha256": config["manifest_sha256"],
        "current_stage": "plan",
        "current_attempt_id": run_id,
        "current_input_sha256": "c" * 64,
        "oracle_run_id": run_id,
        "oracle_run_dir": str(run_dir),
        "records": [{"stage": "plan", "run_dir": str(run_dir), "ok": False}],
        "blocker": "exact recovery retained",
    })
    module._write(scope_path, {
        "schema": module.SCOPE_SCHEMA,
        "status": "active",
        "active_workflow_id": workflow_id,
        "project_root": str(config["project_root"]),
        "workflow_parent": str(config["workflow_dir"].parent),
        "workflow_state_path": str(workflow_path),
        "review_policy": config["_review_policy"],
    })
    return {
        "config": config,
        "workflow_state_path": workflow_path,
        "scope_state_path": scope_path,
        "run_dir": run_dir,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "expected_workflow_sha256": module.sha(workflow_path),
        "expected_scope_sha256": module.sha(scope_path),
        "expected_run_state_sha256": module.sha(run_state_path),
        "confirmation": module.USER_STOP_CONFIRMATION,
        "run_state_path": run_state_path,
        "workflow_before": workflow_path.read_bytes(),
        "scope_before": scope_path.read_bytes(),
    }


def _settlement_args(fixture: dict[str, object]) -> dict[str, object]:
    return {
        key: fixture[key]
        for key in (
            "workflow_state_path",
            "scope_state_path",
            "run_dir",
            "workflow_id",
            "run_id",
            "expected_workflow_sha256",
            "expected_scope_sha256",
            "expected_run_state_sha256",
            "confirmation",
        )
    }


def test_user_stop_settlement_dry_run_then_cancels_and_releases_scope(tmp_path: Path) -> None:
    module = load()
    fixture = _user_stop_fixture(module, tmp_path)
    args = _settlement_args(fixture)

    preview = module.settle_user_stopped_workflow(**args, dry_run=True)
    assert preview["status"] == "dry-run"
    assert preview["evidence_mode"] == "user-stop-terminal"
    assert module.sha(fixture["workflow_state_path"]) == fixture["expected_workflow_sha256"]
    assert module.sha(fixture["scope_state_path"]) == fixture["expected_scope_sha256"]
    assert not Path(preview["settlement_path"]).exists()

    run_state_before = module.sha(fixture["run_state_path"])
    result = module.settle_user_stopped_workflow(**args)
    assert result["ok"] is True
    assert result["terminal_status"] == "CANCELED"
    assert result["scope_released"] is True
    assert result["submission_action"] == "none"
    assert module.sha(fixture["run_state_path"]) == run_state_before
    workflow = module._json(fixture["workflow_state_path"])
    scope = module._json(fixture["scope_state_path"])
    receipt = module._json(Path(result["settlement_path"]))
    completion = module._json(Path(result["completion_path"]))
    assert workflow["status"] == "canceled" and workflow["terminal"] is True
    assert scope["status"] == "released" and scope["scope_released"] is True
    assert scope["active_workflow_id"] == ""
    assert scope["user_stopped_workflow_id"] == fixture["workflow_id"]
    assert receipt["schema"] == module.USER_STOP_SETTLEMENT_SCHEMA
    assert receipt["authority"] == module.USER_STOP_CONFIRMATION
    assert receipt["workflow_state_sha256"] == fixture["expected_workflow_sha256"]
    assert receipt["scope_state_sha256"] == fixture["expected_scope_sha256"]
    assert receipt["evidence_mode"] == "user-stop-terminal"
    assert completion["workflow_state_sha256"] == result["workflow_state_sha256"]
    assert completion["scope_state_sha256"] == result["scope_state_sha256"]

    # The released scope revalidates through _released_scope_is_valid, so a
    # replacement workflow can claim it while the settled one cannot.
    second_config = dict(fixture["config"])
    second_config["workflow_id"] = "d" * 32
    module._claim_scope(second_config, second_config["workflow_id"])
    claimed = module._json(fixture["scope_state_path"])
    assert claimed["status"] == "active"
    assert claimed["active_workflow_id"] == "d" * 32
    with pytest.raises(module.WorkflowError, match="cannot reclaim its scope"):
        module._claim_scope(fixture["config"], fixture["workflow_id"])


def test_user_stop_settlement_is_idempotent(tmp_path: Path) -> None:
    module = load()
    fixture = _user_stop_fixture(module, tmp_path)
    args = _settlement_args(fixture)
    first = module.settle_user_stopped_workflow(**args)
    second = module.settle_user_stopped_workflow(**args)
    assert second["settlement_sha256"] == first["settlement_sha256"]
    assert second["completion_sha256"] == first["completion_sha256"]
    assert second["scope_readback"] == first["scope_readback"]


@pytest.mark.parametrize("mutation", ["run-live", "workflow-run", "foreign-scope"])
def test_user_stop_settlement_rejects_insufficient_or_mismatched_evidence(
    tmp_path: Path, mutation: str
) -> None:
    module = load()
    fixture = _user_stop_fixture(module, tmp_path)
    if mutation == "run-live":
        value = module._json(fixture["run_state_path"])
        value["terminal_harvested"] = False
        value["session_authority"] = "live"
        module._write(fixture["run_state_path"], value)
        fixture["expected_run_state_sha256"] = module.sha(fixture["run_state_path"])
    elif mutation == "workflow-run":
        value = module._json(fixture["workflow_state_path"])
        value["current_attempt_id"] = "e" * 32
        module._write(fixture["workflow_state_path"], value)
        fixture["expected_workflow_sha256"] = module.sha(fixture["workflow_state_path"])
    else:
        value = module._json(fixture["scope_state_path"])
        value["active_workflow_id"] = "e" * 32
        module._write(fixture["scope_state_path"], value)
        fixture["expected_scope_sha256"] = module.sha(fixture["scope_state_path"])

    with pytest.raises(module.WorkflowError):
        module.settle_user_stopped_workflow(**_settlement_args(fixture))


def test_user_stop_settlement_rejects_wrong_confirmation_hash_and_post_settlement_tamper(
    tmp_path: Path,
) -> None:
    module = load()
    fixture = _user_stop_fixture(module, tmp_path)
    args = _settlement_args(fixture)
    wrong = {**args, "confirmation": "user-confirmed-no-submission"}
    with pytest.raises(module.WorkflowError, match="confirmation must be"):
        module.settle_user_stopped_workflow(**wrong)
    wrong_hash = {**args, "expected_scope_sha256": "0" * 64}
    with pytest.raises(module.WorkflowError, match="scope state SHA-256 mismatch"):
        module.settle_user_stopped_workflow(**wrong_hash)

    module.settle_user_stopped_workflow(**args)
    workflow = module._json(fixture["workflow_state_path"])
    workflow["unexpected_external_edit"] = True
    module._write(fixture["workflow_state_path"], workflow)
    with pytest.raises(module.WorkflowError, match="changed outside"):
        module.settle_user_stopped_workflow(**args)


def test_cancel_user_stopped_cli_dry_run_requires_explicit_bound_evidence(
    tmp_path: Path, capsys
) -> None:
    module = load()
    fixture = _user_stop_fixture(module, tmp_path)
    args = _settlement_args(fixture)
    exit_code = module.main([
        "--cancel-user-stopped",
        "--workflow-state", str(args["workflow_state_path"]),
        "--scope-state", str(args["scope_state_path"]),
        "--run-dir", str(args["run_dir"]),
        "--workflow-id", str(args["workflow_id"]),
        "--run-id", str(args["run_id"]),
        "--expected-workflow-sha256", str(args["expected_workflow_sha256"]),
        "--expected-scope-sha256", str(args["expected_scope_sha256"]),
        "--expected-run-state-sha256", str(args["expected_run_state_sha256"]),
        "--confirmation", str(args["confirmation"]),
        "--dry-run",
    ])
    value = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert value["status"] == "dry-run"
    assert value["submission_action"] == "none"
    missing = module.main([
        "--cancel-user-stopped",
        "--workflow-state", str(args["workflow_state_path"]),
    ])
    assert missing == 1


def _make_pre_submit_restart(module, fixture: dict[str, object]) -> None:
    run_dir = Path(fixture["run_dir"])
    state = module._json(fixture["run_state_path"])
    state.update({
        "status": "attention_required",
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "transport_status": "failed_pre_submit",
        "task_outcome": "pending",
    })
    module._write(fixture["run_state_path"], state)
    (run_dir / "stdout.log").write_bytes(b"")
    (run_dir / "stderr.log").write_text(
        module.PRE_SUBMIT_DEVSPACE_SERVICE_RESTART
        + ": DevSpace was safely patched before submission and must be restarted once\n",
        encoding="utf-8",
    )
    fixture["expected_run_state_sha256"] = module.sha(fixture["run_state_path"])
    fixture["confirmation"] = module.PRE_SUBMIT_CANCEL_CONFIRMATION


def test_pre_submit_service_restart_requirement_can_be_canceled_after_restart(
    tmp_path: Path,
) -> None:
    module = load()
    fixture = _user_stop_fixture(module, tmp_path)
    _make_pre_submit_restart(module, fixture)

    result = module.settle_user_stopped_workflow(**_settlement_args(fixture))

    receipt = module._json(Path(result["settlement_path"]))
    assert result["ok"] is True
    assert result["scope_released"] is True
    assert receipt["schema"] == module.USER_STOP_SETTLEMENT_SCHEMA
    assert receipt["authority"] == module.PRE_SUBMIT_CANCEL_CONFIRMATION
    assert receipt["evidence_mode"] == "pre-submit-devspace-service-restart-required"
    scope = module._json(fixture["scope_state_path"])
    assert scope["status"] == "released" and scope["scope_released"] is True


@pytest.mark.parametrize("mutation", ["stderr", "authority", "transport", "outcome"])
def test_pre_submit_cancel_rejects_ambiguous_or_mutated_evidence(
    tmp_path: Path, mutation: str
) -> None:
    module = load()
    fixture = _user_stop_fixture(module, tmp_path)
    _make_pre_submit_restart(module, fixture)
    run_dir = Path(fixture["run_dir"])
    if mutation == "stderr":
        (run_dir / "stderr.log").write_text("version resolution failed: SUBMISSION_NOT_READY\n", encoding="utf-8")
    else:
        state = module._json(fixture["run_state_path"])
        if mutation == "authority":
            state["session_authority"] = "terminal"
        elif mutation == "transport":
            state["transport_status"] = "complete"
        else:
            state["task_outcome"] = "blocked"
        module._write(fixture["run_state_path"], state)
        fixture["expected_run_state_sha256"] = module.sha(fixture["run_state_path"])

    with pytest.raises(module.WorkflowError):
        module.settle_user_stopped_workflow(**_settlement_args(fixture))


def test_review_failed_terminalizes_scope_with_settlement_receipt(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    config["_review_policy"] = module._default_review_policy()
    output = tmp_path / "review-output.md"
    output.write_text("critical blocker", encoding="utf-8")
    receipt_path = tmp_path / "review-receipt.json"
    receipt_path.write_text(json.dumps({
        "schema": module.RECEIPT_SCHEMA,
        "workflow_id": config["workflow_id"],
        "stage": "review",
        "attempt_id": "b" * 32,
        "input_mission_sha256": "c" * 64,
        "status": "FAIL",
        "output_path": str(output),
        "output_sha256": module.sha(output),
        "next_stage": "",
        "ready_for_next": False,
        "blocker": "external authority unavailable",
        "critical_finding_ids": ["review-finding-1"],
        "critical_findings_sha256": module._finding_hash(["review-finding-1"]),
    }), encoding="utf-8")
    receipt = module._validate_receipt(
        config, receipt_path, config["workflow_id"], "review", "b" * 32, "c" * 64
    )
    terminal = module._terminal_review_state(config, config["workflow_id"], receipt, [])
    assert terminal is not None
    assert terminal["status"] == "attention_required"
    assert terminal["terminal_status"] == "REVIEW_FAILED"
    assert terminal["scope_released"] is True
    assert terminal["blocker"] == "external authority unavailable"
    assert terminal["review_receipt_sha256"] == module.sha(receipt_path)

    state_path = module._state_path(config, config["workflow_id"])
    module._write_workflow_state(state_path, config, terminal)
    scope = module._json(module._scope_path(config))
    assert scope["status"] == "blocked"
    assert scope["terminal_status"] == "REVIEW_FAILED"
    assert scope["scope_released"] is True
    assert scope["review_receipt_sha256"] == module.sha(receipt_path)
    settlement = module._json(Path(terminal["review_failed_settlement_path"]))
    assert settlement["schema"] == module.REVIEW_FAILED_SETTLEMENT_SCHEMA
    assert settlement["status"] == "REVIEW_FAILED"
    assert settlement["workflow_id"] == config["workflow_id"]
    assert settlement["review_receipt_path"] == str(receipt_path.resolve())
    assert settlement["review_receipt_sha256"] == module.sha(receipt_path)
    assert module._review_failed_scope_is_valid(config, scope) is True

    replacement = {**config, "workflow_id": "d" * 32, "workflow_dir": tmp_path / "workflow-new"}
    module._claim_scope(replacement, replacement["workflow_id"])
    assert module._json(module._scope_path(replacement))["active_workflow_id"] == replacement["workflow_id"]
    with pytest.raises(module.WorkflowError, match="cannot reclaim its scope"):
        module._claim_scope(config, config["workflow_id"])


def test_regular_stage_mission_binds_self_observation_guard(tmp_path: Path) -> None:
    module = load()
    captured: dict[str, str] = {}

    def preview(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        payload = json.loads(oracle_manifest.read_text(encoding="utf-8"))
        captured["mission"] = Path(payload["mission_path"]).read_text(encoding="utf-8")
        captured["run_id"] = payload["run_id"]
        return {"ok": True}

    result = run_workflow(module, manifest(tmp_path), dry_run=True, oracle_execute=preview)
    expected_slug = module._oracle_slug(tmp_path.resolve(), captured["run_id"])

    assert result["ok"] is True
    assert f"exact_oracle_run_id={captured['run_id']}" in captured["mission"]
    assert f"exact_oracle_slug={expected_slug}" in captured["mission"]
    assert "Do not launch a nested Oracle run" in captured["mission"]
    assert "state.json, output.md, transcript.md, recovery" in captured["mission"]


def test_recursive_self_observation_terminalizes_stage_and_releases_scope(tmp_path: Path) -> None:
    module = load()
    config = module.load_manifest(manifest(tmp_path))
    config["_review_policy"] = module._review_policy_from_history(config)
    run_id = "recursive1234"
    slug = module._oracle_slug(config["project_root"], run_id)
    run_dir = tmp_path / "oracle-run"
    run_dir.mkdir()
    output = run_dir / "output.md"
    output.write_text(
        f"run ID: {run_id}\nexact slug: {slug}\nstatus: running\n"
        "task_outcome: pending\noutput.md absent\n"
        "continue-observing-same-exact-session\n"
        "observe-or-recover-exact-session-only\n"
        "observe the original process or recover the exact slug\n"
        "TASK_OUTCOME: BLOCKED\n",
        encoding="utf-8",
    )
    module.RUNNER.STATE.write_json_atomic(run_dir / "state.json", {
        "schema": module.RUNNER.STATE.STATE_SCHEMA,
        "status": "attention_required",
        "run_id": run_id,
        "project_root": str(config["project_root"]),
        "session_authority": "terminal",
        "terminal_harvested": True,
        "task_outcome": "blocked",
        "oracle": {"slug": slug},
        "artifacts": {"output": str(output)},
    })
    terminal = module._terminal_recursive_self_observation_state(
        config, config["workflow_id"], run_dir, [{"stage": "plan", "ok": False}]
    )
    assert terminal is not None
    assert terminal["terminal_status"] == "ORACLE_RECURSIVE_SELF_OBSERVATION"
    assert terminal["scope_released"] is True
    assert terminal["safe_for_fresh_run"] is False
    assert terminal["auto_retry"] is False

    state_path = module._state_path(config, config["workflow_id"])
    module._write_workflow_state(state_path, config, terminal)
    scope = json.loads(module._scope_path(config).read_text(encoding="utf-8"))
    assert scope["status"] == "blocked"
    assert scope["terminal_status"] == "ORACLE_RECURSIVE_SELF_OBSERVATION"
    assert scope["scope_released"] is True


def test_recursive_self_observation_terminalizes_through_run_workflow_without_retry(
    tmp_path: Path,
) -> None:
    module = load()
    path = manifest(tmp_path)
    submissions: list[str] = []

    def fake_execute(oracle_manifest: Path, *, dry_run: bool, **kwargs):
        payload = json.loads(oracle_manifest.read_text(encoding="utf-8"))
        run_id = payload["run_id"]
        run_dir = _oracle_running_state(module, oracle_manifest)
        submissions.append(run_id)
        output = run_dir / "output.md"
        slug = module._oracle_slug(tmp_path.resolve(), run_id)
        output.write_text(
            f"run ID: {run_id}\nexact slug: {slug}\nstatus: running\n"
            "task_outcome: pending\noutput.md absent\n"
            "continue-observing-same-exact-session\n"
            "observe-or-recover-exact-session-only\n"
            "observe the original process or recover the exact slug\n"
            "TASK_OUTCOME: BLOCKED\n",
            encoding="utf-8",
        )
        state = module.RUNNER.STATE.load_state(run_dir / "state.json")
        state.update({
            "status": "attention_required",
            "session_authority": "terminal",
            "terminal_harvested": True,
            "task_outcome": "blocked",
            "oracle": {"slug": slug},
            "artifacts": {"output": str(output)},
        })
        module.RUNNER.STATE.write_json_atomic(run_dir / "state.json", state)
        return {"ok": False, "run_dir": str(run_dir)}

    result = run_workflow(module, path, oracle_execute=fake_execute)
    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["terminal_status"] == "ORACLE_RECURSIVE_SELF_OBSERVATION"
    assert result["scope_released"] is True
    assert result["auto_retry"] is False
    assert result["submission_action"] == "none"
    assert len(submissions) == 1
    config = module.load_manifest(path)
    scope = module._json(module._scope_path(config))
    assert scope["status"] == "blocked"
    assert scope["scope_released"] is True
