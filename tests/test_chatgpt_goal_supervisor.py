from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import chatgpt_goal_supervisor
from chatgpt_goal_contract import GoalContractError, artifact_ref, file_sha256, load_json, validate_goal_cycle_result
from chatgpt_goal_supervisor import GoalSupervisor, GoalSupervisorError


def write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def manifest(tmp_path: Path, *, max_cycles: int = 20, repair: bool = False, checks: bool = True) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    registry = write_json(
        tmp_path / "checks.json",
        {
            "schema": "codex.chatgpt.goal-check-registry/v1",
            "checks": {
                "unit": {
                    "argv": [sys.executable, "-c", "print('ok')"],
                    "cwd": ".",
                    "timeout_seconds": 30,
                    "expected_exit_codes": [0],
                }
            },
        },
    )
    value = {
        "schema": "codex.chatgpt.goal-supervisor/v1",
        "goal_id": "goal-test",
        "goal": "Implement the requested durable feature.",
        "project": {
            "root": str(project),
            "chatgpt_app_name": "CodexPro",
            "allowed_write_paths": ["."],
            "automation_repo_root": str(ROOT),
        },
        "context": {"candidate_paths": ["."], "policy_paths": []},
        "gates": {
            "research": {"policy": "skip", "triggers": []},
            "advisory": {
                "policy": "skip",
                "triggers": [],
                "affected_components": 0,
                "cross_component_interfaces": 0,
                "contradiction_evidence": [],
            },
        },
        "acceptance": {"criteria": ["feature works"], "required_check_ids": ["unit"] if checks else []},
        "policy": {
            "max_cycles": max_cycles,
            "stagnation_limit": 3,
            "automatic_repair": repair,
            "repair_attempts_per_family": 2,
            "target_commit": False,
            "target_push": False,
        },
        "output_dir": str(tmp_path / "state"),
    }
    if checks:
        value["check_registry"] = artifact_ref(registry)
    return write_json(tmp_path / "goal.json", value)


def cycle_result(cycle_manifest_path: Path, decision: str, *, mission: str | None = None) -> dict:
    cycle = load_json(cycle_manifest_path)
    index = int(cycle["cycle_index"])
    return {
        "schema": "codex.chatgpt.goal-cycle-result/v1",
        "workflow_id": f"{cycle['goal_id']}-cycle-{index:04d}",
        "goal_id": cycle["goal_id"],
        "cycle_index": index,
        "stage": "gpt-orchestrator",
        "attempt_index": 1,
        "nonce": "a" * 32,
        "question_sha256": "b" * 64,
        "source_snapshot_sha256": "c" * 64,
        "original_goal_sha256": cycle["original_goal"]["sha256"],
        "mission_sha256": cycle["mission"]["sha256"],
        "input_plan_sha256": "d" * 64,
        "input_research_descriptor_sha256": "e" * 64,
        "input_advisory_descriptor_sha256": "f" * 64,
        "input_review_sha256": "1" * 64,
        "implementation_status": "complete",
        "decision": decision,
        "summary": "Cycle finished deterministically.",
        "criterion_claims": [{"criterion": "feature works", "status": "satisfied", "evidence_refs": ["tests"]}],
        "remaining_work": [] if decision == "GOAL_COMPLETE" else ["continue"],
        "changed_files": ["feature.py"],
        "commands": ["pytest"],
        "blockers": [],
        "requested_host_check_ids": ["unit"],
        "next_mission_body": mission if decision == "CONTINUE" else None,
        "next_mission_on_gate_failure": "Fix the deterministic gate failures without changing the goal." if decision == "GOAL_COMPLETE" else None,
        "user_action": None,
    }


def test_two_cycle_completion_and_exact_next_mission(tmp_path: Path) -> None:
    seen: list[Path] = []

    def runner(cycle_path: Path, _manifest_path: Path):
        seen.append(cycle_path)
        cycle = load_json(cycle_path)
        if cycle["cycle_index"] == 1:
            result = cycle_result(cycle_path, "CONTINUE", mission="Second-cycle exact mission: finish verification.")
        else:
            assert Path(cycle["mission"]["path"]).read_bytes() == b"Second-cycle exact mission: finish verification."
            result = cycle_result(cycle_path, "GOAL_COMPLETE")
        return {"result": result}

    supervisor = GoalSupervisor(manifest(tmp_path), cycle_runner=runner)
    state = supervisor.run()
    assert state["phase"] == "GOAL_COMPLETE"
    assert state["cycle_index"] == 2
    assert len(seen) == 2
    assert load_json(Path(state["final"]["path"]))["target_push_performed"] is False


def test_restart_reuses_existing_cycle_result_without_duplicate_runner_call(tmp_path: Path) -> None:
    calls = 0

    def runner(cycle_path: Path, _manifest_path: Path):
        nonlocal calls
        calls += 1
        return {"result": cycle_result(cycle_path, "GOAL_COMPLETE")}

    path = manifest(tmp_path)
    first = GoalSupervisor(path, cycle_runner=runner)
    state = first.prepare()
    state = first._run_current_cycle(state)
    assert state["phase"] == "GOAL_COMPLETE"
    assert calls == 1
    second = GoalSupervisor(path, cycle_runner=lambda *_: pytest.fail("duplicate cycle submission"))
    assert second.run()["phase"] == "GOAL_COMPLETE"


def test_failed_completion_gate_uses_web_fallback_mission(tmp_path: Path) -> None:
    calls = 0

    def command_runner(*args, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(args[0], 1 if calls == 1 else 0, "", "failed")

    missions: list[bytes] = []

    def runner(cycle_path: Path, _manifest_path: Path):
        cycle = load_json(cycle_path)
        missions.append(Path(cycle["mission"]["path"]).read_bytes())
        return {"result": cycle_result(cycle_path, "GOAL_COMPLETE")}

    supervisor = GoalSupervisor(manifest(tmp_path, max_cycles=2), cycle_runner=runner, command_runner=command_runner)
    state = supervisor.run()
    assert state["phase"] == "GOAL_COMPLETE"
    assert missions[1] == b"Fix the deterministic gate failures without changing the goal."


def test_cycle_budget_stops_mechanically(tmp_path: Path) -> None:
    def runner(cycle_path: Path, _manifest_path: Path):
        return {"result": cycle_result(cycle_path, "CONTINUE", mission="Continue unchanged.")}

    supervisor = GoalSupervisor(manifest(tmp_path, max_cycles=2), cycle_runner=runner)
    state = supervisor.run()
    assert state["phase"] == "WAITING_USER"
    assert supervisor.status()["boundary_code"] == "CYCLE_BUDGET_EXHAUSTED"


def test_status_is_read_only_with_respect_to_runner(tmp_path: Path) -> None:
    calls = 0

    def runner(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("status must not invoke web runner")

    supervisor = GoalSupervisor(manifest(tmp_path), cycle_runner=runner)
    supervisor.prepare()
    status = supervisor.status()
    assert status["mechanical_only"] is True
    assert status["browser_observation_performed"] is False
    assert calls == 0


def test_unknown_cycle_result_key_rejected(tmp_path: Path) -> None:
    path = manifest(tmp_path)
    supervisor = GoalSupervisor(path, cycle_runner=lambda *_: {})
    supervisor.prepare()
    cycle_path = supervisor._cycle_manifest_path(1)
    result = cycle_result(cycle_path, "GOAL_COMPLETE")
    result["host_command"] = "rm -rf"
    cycle = load_json(cycle_path)
    expected = supervisor._expected_cycle_binding(cycle, result)
    with pytest.raises(GoalContractError, match="UNKNOWN_KEYS"):
        validate_goal_cycle_result(result, expected)


def test_confirmed_repair_requires_second_same_fingerprint_and_explicit_enable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Fault(RuntimeError):
        code = "RUNTIME_ARTIFACT_MISSING"

    cycle_calls = 0

    def runner(cycle_path: Path, _manifest_path: Path):
        nonlocal cycle_calls
        cycle_calls += 1
        if cycle_calls <= 2:
            raise Fault("same stable failure")
        return {"result": cycle_result(cycle_path, "GOAL_COMPLETE")}

    repair_calls = 0

    def repair_runner(_packet_path: Path, _repo_root: Path, _result_path: Path):
        nonlocal repair_calls
        repair_calls += 1
        return {
            "schema": "codex.chatgpt.goal-repair-result/v1",
            "status": "COMPLETE",
            "summary": "The bounded repair passed.",
            "changed_files": ["bin/example.py"],
            "focused_tests": ["pytest tests/test_example.py"],
            "exact_run_preserved": True,
            "new_submission_created": False,
            "installation_synced": True,
            "source_committed": True,
            "source_pushed": True,
            "ci_passed": True,
        }

    path = manifest(tmp_path, repair=True)
    supervisor = GoalSupervisor(path, cycle_runner=runner, repair_runner=repair_runner)
    first = supervisor.run()
    assert first["phase"] == "WAITING_USER"
    assert supervisor.status()["boundary_code"] == "AUTOMATION_FAULT_FIRST_OCCURRENCE"
    monkeypatch.setenv("CODEX_CHATGPT_AUTOMATIC_REPAIR", "1")
    second = GoalSupervisor(path, cycle_runner=runner, repair_runner=repair_runner)
    state = second.resume()
    assert state["phase"] == "GOAL_COMPLETE"
    assert repair_calls == 1
    families = load_json(second.state_path)["repair_families"]
    assert next(iter(families.values()))["attempts"] == 1


def test_repair_result_without_release_invariants_stops_for_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Fault(RuntimeError):
        code = "RUNTIME_ARTIFACT_MISSING"

    def runner(*_args):
        raise Fault("same stable failure")

    def repair_runner(_packet_path: Path, _repo_root: Path, _result_path: Path):
        return {
            "schema": "codex.chatgpt.goal-repair-result/v1",
            "status": "COMPLETE",
            "summary": "Install sync was not proved.",
            "changed_files": ["bin/example.py"],
            "focused_tests": ["pytest tests/test_example.py"],
            "exact_run_preserved": True,
            "new_submission_created": False,
            "installation_synced": False,
            "source_committed": True,
            "source_pushed": True,
            "ci_passed": True,
        }

    path = manifest(tmp_path, repair=True)
    first = GoalSupervisor(path, cycle_runner=runner, repair_runner=repair_runner)
    assert first.run()["phase"] == "WAITING_USER"
    monkeypatch.setenv("CODEX_CHATGPT_AUTOMATIC_REPAIR", "1")
    second = GoalSupervisor(path, cycle_runner=runner, repair_runner=repair_runner)
    assert second.resume()["phase"] == "WAITING_USER"
    assert second.status()["boundary_code"] == "AUTOMATIC_REPAIR_NOT_ACCEPTED"


def test_automatic_repair_uses_sol_medium(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    schema_path = write_json(tmp_path / "repair-schema.json", {"type": "object"})
    result_path = tmp_path / "repair-result.json"
    packet_path = write_json(tmp_path / "incident.json", {"fault": "bounded"})
    captured: dict[str, object] = {}

    def command_runner(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        write_json(result_path, {"status": "COMPLETE"})
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(chatgpt_goal_supervisor, "REPAIR_RESULT_SCHEMA_PATHS", (schema_path,))
    supervisor = GoalSupervisor(manifest(tmp_path), command_runner=command_runner)
    assert supervisor._run_codex_repair(packet_path, ROOT, result_path) == {"status": "COMPLETE"}
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("-m") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="medium"' in argv
