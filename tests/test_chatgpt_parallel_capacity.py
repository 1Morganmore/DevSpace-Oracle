from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path


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
