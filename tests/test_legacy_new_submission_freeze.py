from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FROZEN = "LEGACY_NEW_SUBMISSION_FROZEN"


def run_script(path: Path, *args: str) -> tuple[int, dict]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(ROOT)
    completed = subprocess.run(
        [sys.executable, str(path), *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        env=env,
    )
    return completed.returncode, json.loads(completed.stdout)


def assert_frozen(path: Path, *args: str) -> None:
    returncode, payload = run_script(path, *args)
    assert returncode == 2
    error = payload["error"]
    assert (error.get("code") or error.get("errorCode")) == FROZEN
    assert "Oracle" in error["message"]


def test_agbrowse_new_config_and_prepare_are_frozen_before_manifest_read() -> None:
    runner = ROOT / "bin" / "chatgpt_agbrowse_run.py"
    assert_frozen(runner, "--config", str(ROOT / "does-not-exist.json"))
    assert_frozen(runner, "--prepare-session")


def test_skill_wrappers_cannot_submit_new_agbrowse_runs() -> None:
    wrappers = (
        ROOT / "skills" / "chatgpt-thinking-browser" / "scripts" / "run_chatgpt_thinking.py",
        ROOT / "skills" / "chatgpt-deep-research-browser" / "scripts" / "run_chatgpt_deep_research.py",
        ROOT / "skills" / "chatgpt-pro-browser" / "scripts" / "run_chatgpt_pro.py",
    )
    for wrapper in wrappers:
        assert_frozen(wrapper, "--config", str(ROOT / "does-not-exist.json"))


def test_legacy_staged_and_multi_submission_entrypoints_are_frozen() -> None:
    assert_frozen(
        ROOT / "skills" / "chatgpt-pro-plan-handoff" / "scripts" / "run_pro_plan_handoff.py",
        "--manifest",
        str(ROOT / "does-not-exist.json"),
    )
    assert_frozen(
        ROOT / "skills" / "chatgpt-pro-plan-handoff" / "scripts" / "run_parallel_implementation.py",
        "prepare",
        "--manifest",
        str(ROOT / "does-not-exist.json"),
        "--graph",
        str(ROOT / "does-not-exist-graph.json"),
    )
    assert_frozen(
        ROOT / "bin" / "chatgpt_web_multi_runtime.py",
        "--manifest",
        str(ROOT / "does-not-exist.json"),
    )


def test_agbrowse_existing_run_operations_reach_recovery_code(monkeypatch, capsys) -> None:
    import importlib.util

    path = ROOT / "bin" / "chatgpt_agbrowse_run.py"
    spec = importlib.util.spec_from_file_location("legacy_recovery_guard_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class Bridge:
        def __init__(self, state_root=None):
            del state_root
            self.store = self

        def load(self, run_dir):
            return Path(run_dir) / "run.json", {
                "run_id": "existing",
                "phase": "COMPLETE",
                "run_dir": str(run_dir),
                "result": {"path": str(Path(run_dir) / "answer.md"), "sha256": "a" * 64},
            }

    monkeypatch.setattr(module.BRIDGE, "Bridge", Bridge)
    assert module.main(["--show-run", "C:/persisted/run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["result"]["run_id"] == "existing"


def load_module(name: str, path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_agbrowse_project_lock_doctor_remains_available(monkeypatch, capsys) -> None:
    module = load_module("legacy_doctor_guard_test", ROOT / "bin" / "chatgpt_agbrowse_run.py")

    class Store:
        def __init__(self, state_root=None):
            del state_root

        def reconcile_project_lock(self, project_root, *, apply_safe_pre_submission):
            return {
                "ok": True,
                "project_root": project_root,
                "applied": apply_safe_pre_submission,
            }

    monkeypatch.setattr(module.BRIDGE.STATE, "RunStore", Store)
    assert module.main(["--doctor-project-lock", "C:/persisted/project"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["applied"] is False


def test_persisted_handoff_workflow_can_resume(monkeypatch, tmp_path: Path, capsys) -> None:
    module = load_module(
        "legacy_handoff_resume_guard_test",
        ROOT / "skills" / "chatgpt-pro-plan-handoff" / "scripts" / "run_pro_plan_handoff.py",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text("{}\n", encoding="utf-8")

    class Driver:
        def __init__(self, manifest_path, *, recovery_only):
            assert manifest_path == manifest
            assert recovery_only is True
            self.state_path = state_path

        def run(self, *, prepare_only):
            return {"status": "RECOVERED", "prepare_only": prepare_only}

    monkeypatch.setattr(module, "ProPlanHandoffDriver", Driver)
    assert module.main(["--manifest", str(manifest)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["status"] == "RECOVERED"


def test_web_multi_exact_parent_resume_remains_available(monkeypatch, tmp_path: Path, capsys) -> None:
    module = load_module("legacy_web_multi_resume_guard_test", ROOT / "bin" / "chatgpt_web_multi_runtime.py")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    parent = tmp_path / "parent"
    parent.mkdir()
    class Runtime:
        def __init__(self, manifest_path, *, recovery_only):
            assert manifest_path == manifest
            assert recovery_only is True

        def run(self, *, resume_parent):
            return {"status": "RECOVERED", "parent": str(resume_parent)}

    monkeypatch.setattr(module, "WebMultiRuntime", Runtime)
    assert module.main(["--manifest", str(manifest), "--resume-parent", str(parent)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["status"] == "RECOVERED"


def test_parallel_resume_is_frozen_before_dispatch(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    receipt = tmp_path / "capacity.json"
    receipt.write_text("{}\n", encoding="utf-8")
    assert_frozen(
        ROOT / "skills" / "chatgpt-pro-plan-handoff" / "scripts" / "run_parallel_implementation.py",
        "resume",
        "--parent-run-dir",
        str(parent),
        "--capacity-receipt",
        str(receipt),
    )


def test_handoff_recovery_never_starts_an_unsubmitted_stage(tmp_path: Path) -> None:
    module = load_module(
        "legacy_handoff_zero_start_test",
        ROOT / "skills" / "chatgpt-pro-plan-handoff" / "scripts" / "run_pro_plan_handoff.py",
    )
    starts = 0

    class Runtime:
        def recover_run_ids(self, manifest_path):
            assert manifest_path.is_file()
            return []

        def start(self, manifest_path):
            nonlocal starts
            starts += 1
            return {"run_id": "must-not-start"}

    driver = object.__new__(module.ProPlanHandoffDriver)
    driver.stages_dir = tmp_path / "stages"
    driver.runtime = Runtime()
    driver.recovery_only = True
    stage_dir = driver.stages_dir / "gpt-plan-attempt-1"
    stage_dir.mkdir(parents=True)
    (stage_dir / "stage.manifest.json").write_text(
        json.dumps({"existing": True}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    try:
        driver._dispatch("gpt-plan", 1, {"existing": True})
    except module.WorkflowError as exc:
        assert exc.code == FROZEN
    else:
        raise AssertionError("unsubmitted legacy stage was not frozen")
    assert starts == 0


def test_handoff_recovery_never_resends_a_rejected_stage(tmp_path: Path) -> None:
    module = load_module(
        "legacy_handoff_zero_resend_test",
        ROOT / "skills" / "chatgpt-pro-plan-handoff" / "scripts" / "run_pro_plan_handoff.py",
    )
    resumes = 0

    class Runtime:
        def recover_run_ids(self, manifest_path):
            return ["existing-run"]

        def status(self, run_id):
            return {"status": "blocked", "phase": "SEND_REJECTED", "run_id": run_id}

        def resume(self, run_id):
            nonlocal resumes
            resumes += 1
            return {"status": "running", "run_id": run_id}

    driver = object.__new__(module.ProPlanHandoffDriver)
    driver.stages_dir = tmp_path / "stages"
    driver.runtime = Runtime()
    driver.recovery_only = True
    stage_dir = driver.stages_dir / "gpt-plan-attempt-1"
    stage_dir.mkdir(parents=True)
    manifest = {"existing": True}
    (stage_dir / "stage.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    try:
        driver._dispatch("gpt-plan", 1, manifest)
    except module.WorkflowError as exc:
        assert exc.code == FROZEN
    else:
        raise AssertionError("rejected legacy stage was resent")
    assert resumes == 0


def test_web_multi_recovery_never_sends_a_pre_submit_child() -> None:
    module = load_module("legacy_web_multi_zero_send_test", ROOT / "bin" / "chatgpt_web_multi_runtime.py")
    sends = 0

    class Store:
        def load(self, run_dir):
            return Path(run_dir) / "run.json", {"phase": "PREFLIGHTED"}

    class Bridge:
        def send(self, run_dir):
            nonlocal sends
            sends += 1
            return {"phase": "SUBMITTED"}

    runtime = object.__new__(module.WebMultiRuntime)
    runtime.store = Store()
    runtime.bridge_factory = Bridge
    runtime.recovery_only = True
    runtime.manifest = {}
    spec = module.StageSpec("solver-0", "Solver", 0, 0, {}, tuple())
    try:
        runtime._actual_execute({"run_dir": "C:/persisted/child"}, spec)
    except module.WebMultiError as exc:
        assert exc.code == FROZEN
    else:
        raise AssertionError("pre-submit legacy child was not frozen")
    assert sends == 0
