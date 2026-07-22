from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "chatgpt-pro-plan-handoff" / "scripts" / "run_parallel_implementation.py"
SPEC = importlib.util.spec_from_file_location("parallel_implementation_driver_capacity_test", SCRIPT)
assert SPEC and SPEC.loader
DRIVER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DRIVER
SPEC.loader.exec_module(DRIVER)


def test_dispatch_wave_runs_distinct_units_concurrently_and_keeps_result_order() -> None:
    started: list[str] = []
    lock = threading.Lock()
    both_started = threading.Event()

    def executor(dispatch: dict, control: dict) -> dict:
        assert control["parent_run_id"] == "parent"
        with lock:
            started.append(dispatch["unit_id"])
            if len(started) == 2:
                both_started.set()
        assert both_started.wait(timeout=2), "workers were not started concurrently"
        return {"unit_result": {"unit_id": dispatch["unit_id"]}}

    dispatches = [
        {"unit_id": "a", "unit_workspace_root": "C:/isolated/a", "child_manifest_path": "a.json"},
        {"unit_id": "b", "unit_workspace_root": "C:/isolated/b", "child_manifest_path": "b.json"},
    ]
    results = DRIVER.run_dispatch_wave(dispatches, {"parent_run_id": "parent"}, executor=executor)
    assert set(started) == {"a", "b"}
    assert [item["unit_result"]["unit_id"] for item in results] == ["a", "b"]


def test_execution_result_accepts_injected_structured_result_without_browser() -> None:
    result = {"schema": "codex.chatgpt.implementation-unit-result/v1", "unit_id": "u", "attempt_id": "a", "input_base_oid": "1" * 40, "status": "NO_CHANGE", "summary": "no-op", "changed_paths": [], "test_results": []}
    assert DRIVER._unit_result_from_execution({"unit_result": result}) == result


def test_execute_persists_completed_sibling_before_later_child_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A failed sibling must not erase or re-submit an already completed child."""
    success_persisted = threading.Event()
    dispatches = [
        {"unit_id": "a", "attempt_id": "attempt-a", "state": "AWAITING_PROVIDER_RESULT", "unit_workspace_root": "C:/isolated/a"},
        {"unit_id": "b", "attempt_id": "attempt-b", "state": "AWAITING_PROVIDER_RESULT", "unit_workspace_root": "C:/isolated/b"},
    ]
    control = {
        "parent_run_id": "parent",
        "manifest_path": str(tmp_path / "manifest.json"),
        "dispatches": dispatches,
        "capacity_receipt": {},
    }
    state = {"units": {"a": {"state": "ACTIVE"}, "b": {"state": "ACTIVE"}}}
    files = {"results": tmp_path / "results"}
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    persisted: list[str] = []
    recoveries: list[str] = []

    monkeypatch.setattr(DRIVER, "load_control", lambda _parent: (control, files, state, {}))
    monkeypatch.setattr(DRIVER, "read_json", lambda _path: {})
    monkeypatch.setattr(DRIVER, "write_json", lambda _path, _value: None)
    monkeypatch.setattr(DRIVER, "dispatch_ready", lambda *_args, **_kwargs: [])

    def record(_parent: Path, result_path: Path, *, dispatch_next: bool) -> dict:
        unit_id = result_path.parents[1].name
        persisted.append(unit_id)
        next(item for item in dispatches if item["unit_id"] == unit_id)["state"] = "INTEGRATED"
        state["units"][unit_id]["state"] = "INTEGRATED"
        success_persisted.set()
        return {"status": "INTEGRATED", "receipt": {"unit_id": unit_id}}

    def recovery(_parent: Path, dispatch: dict, _error: Exception) -> dict:
        unit_id = dispatch["unit_id"]
        recoveries.append(unit_id)
        next(item for item in dispatches if item["unit_id"] == unit_id)["state"] = "RECOVERY_REQUIRED"
        state["units"][unit_id]["state"] = "RECOVERY_REQUIRED"
        return {"status": "RECOVERY_REQUIRED", "receipt": {"unit_id": unit_id}}

    monkeypatch.setattr(DRIVER, "record_unit", record)
    monkeypatch.setattr(DRIVER, "_record_child_recovery", recovery)
    monkeypatch.setattr(DRIVER.RUNTIME, "apply_ready", lambda _state: False)

    def executor(dispatch: dict, _control: dict) -> dict:
        if dispatch["unit_id"] == "a":
            return {"unit_result": {"unit_id": "a", "attempt_id": "attempt-a"}}
        assert success_persisted.wait(timeout=2), "successful sibling was not persisted before failure"
        raise RuntimeError("child b transport failed after send")

    outcome = DRIVER.execute(tmp_path, executor=executor, finalize_when_ready=False)

    assert outcome["status"] == "DRAINED"
    assert persisted == ["a"]
    assert recoveries == ["b"]
    assert state["units"]["a"]["state"] == "INTEGRATED"
    assert state["units"]["b"]["state"] == "RECOVERY_REQUIRED"
    assert [item["unit_id"] for item in control["dispatches"] if item["state"] == "AWAITING_PROVIDER_RESULT"] == []
