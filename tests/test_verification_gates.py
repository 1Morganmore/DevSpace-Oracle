from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_fast_gate_targets_exist_and_cover_the_pre_submit_contracts() -> None:
    gate = load("fast_gate_test", SCRIPTS / "run_fast_gate.py")

    for target in gate.FAST_TARGETS:
        assert (ROOT / target).is_file(), target

    covered = set(gate.FAST_TARGETS)
    # The buckets that actually blocked runs before submission must be gated.
    assert "tests/test_chatgpt_oracle_state.py" in covered
    assert "tests/test_chatgpt_oracle_run.py" in covered
    assert "tests/test_chatgpt_oracle_compat.py" in covered
    assert "tests/test_chatgpt_oracle_incident.py" in covered
    assert "tests/test_chatgpt_oracle_diagnose.py" in covered
    assert gate.DEFAULT_BUDGET_SECONDS == 60.0


def test_fast_gate_is_a_strict_subset_of_the_full_suite() -> None:
    gate = load("fast_gate_subset_test", SCRIPTS / "run_fast_gate.py")
    all_tests = {
        f"tests/{path.name}" for path in (ROOT / "tests").glob("test_*.py")
    }

    assert set(gate.FAST_TARGETS) < all_tests


def test_posix_containment_failure_is_always_structured() -> None:
    gate = load("fast_gate_posix_failure_shape_test", SCRIPTS / "run_fast_gate.py")
    result = gate._posix_containment_failure(
        started=time.monotonic(),
        hard_timeout_seconds=3.0,
        process_id=12345,
        error_text="expected failure",
    )

    assert result["containment_kind"] == "linux_pid_namespace"
    assert result["containment_established"] is False
    assert result["exit_code"] == 126
    assert result["termination_confirmed"] is False
    assert result["residual_process_id"] == 12345
    assert result["termination_error"] == "expected failure"


def test_fast_gate_hides_windows_console_windows() -> None:
    gate = load("fast_gate_window_test", SCRIPTS / "run_fast_gate.py")

    source = (SCRIPTS / "run_fast_gate.py").read_text(encoding="utf-8")
    assert "CREATE_NO_WINDOW" in source
    assert "SW_HIDE" in source
    assert callable(gate._hidden_process_kwargs)


def test_fast_gate_windows_wrapper_waits_for_target_and_propagates_exit_code(tmp_path: Path) -> None:
    gate = load("fast_gate_wrapper_wait_test", SCRIPTS / "run_fast_gate.py")
    started = time.monotonic()

    result = gate.run_gate_command(
        [sys.executable, "-c", "import time; time.sleep(0.35); raise SystemExit(7)"],
        cwd=tmp_path,
        environment=dict(os.environ),
        hard_timeout_seconds=5,
    )

    assert time.monotonic() - started >= 0.3
    assert result["timed_out"] is False
    assert result["exit_code"] == 7


def test_fast_gate_windows_assignment_failure_never_opens_the_workload_gate(tmp_path: Path, monkeypatch) -> None:
    if os.name != "nt":
        return
    gate = load("fast_gate_assignment_failure_test", SCRIPTS / "run_fast_gate.py")
    marker = tmp_path / "must-not-run.txt"
    monkeypatch.setattr(gate, "_assign_windows_kill_job", lambda _process: None)

    result = gate.run_gate_command(
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
        cwd=tmp_path,
        environment=dict(os.environ),
        hard_timeout_seconds=5,
    )

    assert result["exit_code"] == 126
    assert result["windows_job_assigned"] is False
    assert result["termination_confirmed"] is True
    assert not marker.exists()


def test_windows_launch_authority_cannot_be_precreated_in_shared_temp(tmp_path: Path, monkeypatch) -> None:
    if os.name != "nt":
        return
    gate = load("fast_gate_unforgeable_launch_authority_test", SCRIPTS / "run_fast_gate.py")
    marker = tmp_path / "must-not-escape-before-assignment.txt"
    stop = threading.Event()

    def precreate_old_gate_names() -> None:
        while not stop.wait(0.001):
            for candidate in Path(gate.tempfile.gettempdir()).glob("cf-job-*.gate"):
                try:
                    candidate.write_text("go", encoding="ascii")
                except OSError:
                    pass

    attacker = threading.Thread(target=precreate_old_gate_names, daemon=True)
    attacker.start()

    def delayed_assignment_failure(_process) -> None:
        time.sleep(0.3)
        return None

    monkeypatch.setattr(gate, "_assign_windows_kill_job", delayed_assignment_failure)
    try:
        result = gate.run_gate_command(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('escaped')"],
            cwd=tmp_path,
            environment=dict(os.environ),
            hard_timeout_seconds=5,
        )
    finally:
        stop.set()
        attacker.join(timeout=1)

    assert result["exit_code"] == 126
    assert result["termination_confirmed"] is True
    assert not marker.exists()


def test_fast_gate_windows_parent_death_before_gate_release_never_starts_workload(tmp_path: Path) -> None:
    if os.name != "nt":
        return
    gate = load("fast_gate_parent_dead_before_release_test", SCRIPTS / "run_fast_gate.py")
    marker = tmp_path / "must-not-run-after-parent-death.txt"
    parent_dead = gate._CancellationSignal()
    parent_dead.set()

    result = gate.run_gate_command(
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
        cwd=tmp_path,
        environment=dict(os.environ),
        hard_timeout_seconds=5,
        cancel_event=parent_dead,
    )

    assert result["exit_code"] == 143
    assert result["timed_out"] is False
    assert result["windows_job_assigned"] is True
    assert result["termination_requested"] is True
    assert result["termination_confirmed"] is True
    assert result["windows_job_active_processes"] == 0
    assert result["residual_process_id"] is None
    assert not marker.exists()


def test_windows_gate_release_is_serialized_with_parent_death(tmp_path: Path, monkeypatch) -> None:
    if os.name != "nt":
        return
    gate = load("fast_gate_parent_death_release_race_test", SCRIPTS / "run_fast_gate.py")
    marker = tmp_path / "must-not-run-after-release-race.txt"
    parent_dead = gate._CancellationSignal()
    original_signal = gate._signal_windows_release

    def parent_dies_during_release(release_handle: int) -> bool:
        parent_dead.set()
        time.sleep(0.1)
        return original_signal(release_handle)

    monkeypatch.setattr(gate, "_signal_windows_release", parent_dies_during_release)
    result = gate.run_gate_command(
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
        cwd=tmp_path,
        environment=dict(os.environ),
        hard_timeout_seconds=5,
        cancel_event=parent_dead,
    )

    assert result["exit_code"] == 143
    assert result["termination_confirmed"] is True
    assert result["windows_job_active_processes"] == 0
    assert not marker.exists()


def test_windows_gate_release_loses_to_normal_cancel_file(tmp_path: Path, monkeypatch) -> None:
    if os.name != "nt":
        return
    gate = load("fast_gate_normal_cancel_release_race_test", SCRIPTS / "run_fast_gate.py")
    marker = tmp_path / "must-not-run-after-normal-cancel.txt"
    cancel_file = tmp_path / "cancel"
    original_signal = gate._signal_windows_release

    def delayed_monitor(_control, _cancel_event, _cancel_file):
        stop = threading.Event()
        thread = threading.Thread(target=lambda: stop.wait(2), daemon=True)
        thread.start()
        return stop, thread

    def cancel_during_release(release_handle: int) -> bool:
        cancel_file.write_text("cancel", encoding="ascii")
        return original_signal(release_handle)

    monkeypatch.setattr(gate, "_start_windows_cancel_monitor", delayed_monitor)
    monkeypatch.setattr(gate, "_signal_windows_release", cancel_during_release)
    result = gate.run_gate_command(
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
        cwd=tmp_path,
        environment=dict(os.environ),
        hard_timeout_seconds=5,
        cancel_file=cancel_file,
    )

    assert result["exit_code"] == 143
    assert result["termination_confirmed"] is True
    assert result["windows_job_active_processes"] == 0
    assert not marker.exists()


def test_fast_gate_hard_timeout_settles_without_waiting_for_child_exit(tmp_path: Path) -> None:
    gate = load("fast_gate_timeout_test", SCRIPTS / "run_fast_gate.py")

    result = gate.run_gate_command(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        environment=dict(os.environ),
        hard_timeout_seconds=0.2,
    )

    assert result["timed_out"] is True
    assert result["exit_code"] == gate.TIMEOUT_EXIT_CODE == 124
    assert result["elapsed_seconds"] < 5
    assert result["termination_requested"] is True
    assert result["termination_confirmed"] is True
    assert result["residual_process_id"] is None


def test_fast_gate_timeout_terminates_descendant_process_tree(tmp_path: Path) -> None:
    gate = load("fast_gate_descendant_timeout_test", SCRIPTS / "run_fast_gate.py")
    marker = tmp_path / "descendant-survived.txt"
    descendant = (
        "import pathlib,time;"
        "time.sleep(1);"
        f"pathlib.Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
    )
    parent = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}]);"
        "time.sleep(30)"
    )

    result = gate.run_gate_command(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        environment=dict(os.environ),
        hard_timeout_seconds=0.2,
    )
    time.sleep(1.2)

    assert result["timed_out"] is True
    assert not marker.exists(), "the hard timeout left a grandchild process running"
    if os.name == "nt":
        assert result["windows_job_assigned"] is True


def _run_fast_gate_with_setsid_escape(gate, tmp_path: Path, *, timeout: bool) -> tuple[dict[str, object], Path]:
    ready = tmp_path / ("setsid-timeout-ready.txt" if timeout else "setsid-normal-ready.txt")
    marker = tmp_path / ("setsid-timeout-survived.txt" if timeout else "setsid-normal-survived.txt")
    descendant = (
        "import os,pathlib,time;"
        "os.setsid();"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        "time.sleep(0.5);"
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    parent_tail = "time.sleep(30)" if timeout else "raise SystemExit(0 if ready.exists() else 2)"
    parent = (
        "import pathlib,subprocess,sys,time;"
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}]);"
        f"ready=pathlib.Path({str(ready)!r});"
        "deadline=time.monotonic()+2;"
        "\nwhile not ready.exists() and time.monotonic()<deadline: time.sleep(0.01);"
        f"\n{parent_tail}"
    )

    result = gate.run_gate_command(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        environment=dict(os.environ),
        hard_timeout_seconds=0.2 if timeout else 5,
    )
    assert ready.exists(), "the setsid descendant never reached its ready state"
    time.sleep(0.7)
    return result, marker


def test_fast_gate_posix_contains_setsid_descendant_after_normal_exit(tmp_path: Path) -> None:
    if os.name == "nt":
        return
    gate = load("fast_gate_posix_setsid_normal_test", SCRIPTS / "run_fast_gate.py")

    result, marker = _run_fast_gate_with_setsid_escape(gate, tmp_path, timeout=False)

    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert not marker.exists(), "normal completion left a setsid descendant running"
    assert result["containment_kind"] == "linux_pid_namespace"
    assert result["containment_established"] is True
    assert result["termination_requested"] is True
    assert result["termination_confirmed"] is True


def test_fast_gate_posix_contains_setsid_descendant_after_timeout(tmp_path: Path) -> None:
    if os.name == "nt":
        return
    gate = load("fast_gate_posix_setsid_timeout_test", SCRIPTS / "run_fast_gate.py")

    result, marker = _run_fast_gate_with_setsid_escape(gate, tmp_path, timeout=True)

    assert result["exit_code"] == gate.TIMEOUT_EXIT_CODE == 124
    assert result["timed_out"] is True
    assert not marker.exists(), "hard timeout left a setsid descendant running"
    assert result["containment_kind"] == "linux_pid_namespace"
    assert result["containment_established"] is True
    assert result["termination_requested"] is True
    assert result["termination_confirmed"] is True


def test_fast_gate_linux_fails_closed_before_launch_without_unshare(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        return
    gate = load("fast_gate_linux_unshare_failure_test", SCRIPTS / "run_fast_gate.py")
    marker = tmp_path / "must-not-run.txt"
    environment = dict(os.environ)
    environment["PATH"] = ""

    result = gate.run_gate_command(
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"],
        cwd=tmp_path,
        environment=environment,
        hard_timeout_seconds=5,
    )

    assert result["exit_code"] == gate.POSIX_CONTAINMENT_FAILURE_EXIT_CODE == 126
    assert result["containment_established"] is False
    assert result["termination_confirmed"] is False
    assert not marker.exists(), "the workload ran without a containment boundary"


def test_fast_gate_posix_escalates_for_sigterm_resistant_descendant(tmp_path: Path) -> None:
    if os.name == "nt":
        return
    gate = load("fast_gate_posix_resistant_test", SCRIPTS / "run_fast_gate.py")
    ready = tmp_path / "ready.txt"
    marker = tmp_path / "descendant-survived.txt"
    descendant = (
        "import pathlib,signal,time;"
        "signal.signal(signal.SIGTERM, lambda *_: None);"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        "time.sleep(10);"
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    parent = (
        "import pathlib,subprocess,sys,time;"
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}]);"
        f"ready=pathlib.Path({str(ready)!r});"
        "deadline=time.monotonic()+2;"
        "\nwhile not ready.exists() and time.monotonic()<deadline: time.sleep(0.01);"
        "\nraise SystemExit(0 if ready.exists() else 2)"
    )

    result = gate.run_gate_command(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        environment=dict(os.environ),
        hard_timeout_seconds=5,
    )
    time.sleep(0.2)

    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert result["termination_requested"] is True
    assert result["termination_escalated"] is True
    assert result["termination_confirmed"] is True
    assert not marker.exists()


def test_golden_path_smoke_passes_against_the_source_tree() -> None:
    smoke = load("golden_path_smoke_test", SCRIPTS / "run_golden_path_smoke.py")

    result = smoke.run_smoke(bin_root=ROOT / "bin")

    assert result["ok"] is True, result["failed_checks"]
    assert result["submitted_question"] is False
    names = [item["check"] for item in result["checks"]]
    for required in (
        "mode_contract_compiles",
        "manifest_loads",
        "devspace_transport_selected",
        "prompt_is_one_line_with_app_mention",
        "dry_run_preview_ok",
        "argv_never_submits_files",
        "argv_hides_browser_window",
        "argv_selects_a_model",
        "profile_copy_matches_host_capability",
        "lifecycle_vocabulary_is_bounded",
    ):
        assert required in names


def test_golden_path_smoke_never_submits_or_launches_a_browser() -> None:
    source = (SCRIPTS / "run_golden_path_smoke.py").read_text(encoding="utf-8")

    assert "dry_run=True" in source
    assert "dry_run=False" not in source
    assert '"submitted_question": False' in source


def test_ci_workflow_runs_the_fast_gate_and_golden_path_smoke() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-portability.yml").read_text(encoding="utf-8")

    assert "scripts/run_fast_gate.py" in workflow
    assert "scripts/run_golden_path_smoke.py" in workflow
    # The fast gate must run before the long suite so a broken launch contract
    # fails in seconds instead of minutes.
    assert workflow.index("run_fast_gate.py") < workflow.index("run_release_contract_tests.py --full")


def test_release_runner_uses_an_isolated_pytest_basetemp() -> None:
    source = (SCRIPTS / "run_release_contract_tests.py").read_text(encoding="utf-8")

    assert "TemporaryDirectory" in source
    assert '"--basetemp", basetemp' in source
    assert '"no:cacheprovider"' in source


def test_release_manifest_ships_the_new_verification_scripts() -> None:
    manifest = json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))

    assert "bin/chatgpt_oracle_incident.py" in manifest["include"]
    assert "bin/chatgpt_oracle_incident.py" in package["files"]
