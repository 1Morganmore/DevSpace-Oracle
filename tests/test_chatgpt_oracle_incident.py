from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_incident.py"


def load():
    name = "chatgpt_oracle_incident_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_run(
    root: Path,
    run_id: str,
    *,
    status: str,
    stdout: str = "",
    output: str | None = None,
    session_authority: str = "",
    terminal_harvested: bool = False,
) -> Path:
    run_dir = root / "projects" / "projectkey" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_root = root / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    output_path = run_dir / "output.md"
    if output is not None:
        output_path.write_text(output, encoding="utf-8")
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    (run_dir / "state.json").write_text(json.dumps({
        "schema": "codex.chatgpt.oracle-run-state/v1",
        "status": status,
        "run_id": run_id,
        "project_root": str(project_root),
        "session_authority": session_authority,
        "terminal_harvested": terminal_harvested,
        "task_outcome": "blocked" if output and "TASK_OUTCOME: BLOCKED" in output else "",
        "artifacts": {"output": str(output_path), "stdout": str(stdout_path), "stderr": str(stderr_path)},
        "oracle": {"slug": "oracle-project-abc", "conversation_url": "https://chatgpt.com/c/exact"},
    }), encoding="utf-8")
    return run_dir


def write_recursive_self_observation_run(
    root: Path,
    run_id: str,
    *,
    with_sidecar: bool = False,
) -> Path:
    """Persist a run whose durable output reports recursive self-observation.

    The output names the run's own identity and both fork-native preserve
    phrase groups, so diagnosis classifies it as
    ``post-submit-recursive-self-observation``; ``with_sidecar`` also writes
    the append-only authority receipt that ``build_packet`` validates.
    """
    slug = "oracle-project-abc"
    output = (
        f"run ID: {run_id}\nexact slug: {slug}\nstatus: running\n"
        "task_outcome: pending\noutput.md absent\n"
        "observe-or-recover-exact-session-only\n"
        "observe the original process or recover the exact slug\n"
        "TASK_OUTCOME: BLOCKED\n"
    )
    run_dir = write_run(
        root,
        run_id,
        status="attention_required",
        output=output,
        session_authority="terminal",
        terminal_harvested=True,
    )
    if with_sidecar:
        (run_dir / "recursive-self-observation.json").write_text(json.dumps({
            "schema": "codex.chatgpt.oracle-recursive-self-observation/v1",
            "confirmation_token": "user-authorized-fresh-run-after-recursive-self-observation",
            "reason": "user authorized continued progress",
            "expected_state_sha256": "a" * 64,
            "expected_output_sha256": "b" * 64,
            "expected_transcript_sha256": "c" * 64,
        }), encoding="utf-8")
    return run_dir


def test_packet_carries_exact_run_bucket_and_evidence(tmp_path: Path) -> None:
    module = load()
    run_dir = write_run(
        tmp_path,
        "a" * 8,
        status="failed",
        stdout="ERROR: ChatGPT app mention was not confirmed in the composer.\n",
    )

    packet = module.validate_packet(module.build_packet(run_dir))

    assert packet["schema"] == "codex.chatgpt.oracle-incident/v1"
    assert packet["run_dir"] == str(run_dir.resolve())
    assert packet["bucket"] == "pre-submit-ui-contract"
    assert packet["signature"] == "app-mention-not-confirmed"
    assert packet["conversation_url"] == "https://chatgpt.com/c/exact"
    assert packet["safe_for_fresh_run"] is True
    assert str(run_dir / "state.json") in packet["evidence_paths"]


def test_version_resolution_prelaunch_incident_is_safe_to_retry(tmp_path: Path) -> None:
    module = load()
    run_dir = write_run(tmp_path, "v" * 8, status="attention_required")
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pre_submit_failure"] = {
        "code": "ORACLE_VERSION_RESOLUTION_PRELAUNCH_FAILED",
        "output_absent": True,
        "conversation_url_absent": True,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    packet = module.build_packet(run_dir)

    assert packet["bucket"] == "pre-submit-host-environment"
    assert packet["signature"] == "oracle-version-resolution-prelaunch-timeout"
    assert packet["safe_for_fresh_run"] is True


def test_version_compatibility_drift_incident_is_safe_to_retry(tmp_path: Path) -> None:
    module = load()
    run_dir = write_run(tmp_path, "c" * 8, status="failed")
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["oracle"] = {"resolved_version": "unresolved"}
    state["session_authority"] = "pre_submit"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "stderr.log").write_text(
        "version resolution failed: Oracle compatibility is validated only for the tested version\n",
        encoding="utf-8",
    )

    packet = module.build_packet(run_dir)

    assert packet["bucket"] == "pre-submit-host-environment"
    assert packet["signature"] == "oracle-version-resolution-prelaunch-compatibility-drift"
    assert packet["safe_for_fresh_run"] is True


def test_packet_never_marks_fresh_run_safe_while_another_session_owns_project(tmp_path: Path) -> None:
    module = load()
    failed = write_run(
        tmp_path,
        "1" * 8,
        status="failed",
        stdout="ERROR: ChatGPT app mention was not confirmed in the composer.\n",
    )
    owner = write_run(
        tmp_path,
        "2" * 8,
        status="running",
        session_authority="submitted_unknown",
    )

    packet = module.build_packet(failed)

    assert packet["safe_for_fresh_run"] is False
    assert [item["run_id"] for item in packet["unresolved_owners"]] == [owner.name]


def test_unsettled_app_route_marker_never_marks_fresh_run_safe(tmp_path: Path) -> None:
    module = load()
    run_dir = write_run(
        tmp_path,
        "u" * 8,
        status="attention_required",
        stdout="ERROR: APP_MENTION_ROUTE_UNCONFIRMED\n",
        session_authority="submitted_unknown",
    )

    packet = module.build_packet(run_dir)

    assert packet["bucket"] == "active-or-uncertain"
    assert packet["safe_for_fresh_run"] is False


def test_reporter_is_never_the_repair_owner(tmp_path: Path) -> None:
    module = load()
    run_dir = write_run(tmp_path, "b" * 8, status="failed", stdout="ERROR: unknown\n")

    packet = module.build_packet(run_dir)

    assert packet["reporter_role"] == module.REPORTER_ROLE
    assert packet["repair_owner"] == module.MAINTENANCE_OWNER
    assert packet["reporter_may_edit_automation_sources"] is False


def test_packet_claiming_reporter_repair_rights_is_rejected(tmp_path: Path) -> None:
    module = load()
    run_dir = write_run(tmp_path, "c" * 8, status="failed", stdout="ERROR: unknown\n")
    packet = module.build_packet(run_dir)

    packet["reporter_may_edit_automation_sources"] = True
    with pytest.raises(module.IncidentError) as exc:
        module.validate_packet(packet)
    assert exc.value.code == "INCIDENT_REPORTER_SCOPE_INVALID"


def test_packet_reassigning_the_repair_owner_is_rejected(tmp_path: Path) -> None:
    module = load()
    run_dir = write_run(tmp_path, "d" * 8, status="failed", stdout="ERROR: unknown\n")
    packet = module.build_packet(run_dir)

    packet["repair_owner"] = "some-other-project-session"
    with pytest.raises(module.IncidentError) as exc:
        module.validate_packet(packet)
    assert exc.value.code == "INCIDENT_REPAIR_OWNER_INVALID"


def test_unclassified_bucket_value_is_rejected(tmp_path: Path) -> None:
    module = load()
    run_dir = write_run(tmp_path, "e" * 8, status="failed", stdout="ERROR: unknown\n")
    packet = module.build_packet(run_dir)

    packet["bucket"] = "made-up-bucket"
    with pytest.raises(module.IncidentError) as exc:
        module.validate_packet(packet)
    assert exc.value.code == "INCIDENT_BUCKET_UNKNOWN"


def test_active_run_is_not_marked_safe_for_a_fresh_run(tmp_path: Path) -> None:
    module = load()
    run_dir = write_run(
        tmp_path,
        "f" * 8,
        status="running",
        session_authority="submitted_unknown",
        stdout="status=response streaming\n",
    )

    packet = module.build_packet(run_dir)

    assert packet["lifecycle"] == "running"
    assert packet["safe_for_fresh_run"] is False


def test_recursive_self_observation_sidecar_absent_never_grants_fresh_authority(tmp_path: Path) -> None:
    module = load()
    run_dir = write_recursive_self_observation_run(tmp_path, "recursive1234")

    packet = module.build_packet(run_dir)

    assert packet["signature"] == "post-submit-recursive-self-observation"
    assert packet["fresh_run_authority"] is False
    assert packet["recursive_fresh_safe"] is False


def test_recursive_self_observation_valid_sidecar_grants_fresh_run_authority(tmp_path: Path) -> None:
    module = load()
    run_dir = write_recursive_self_observation_run(tmp_path, "recursive1235", with_sidecar=True)

    packet = module.build_packet(run_dir)

    assert packet["signature"] == "post-submit-recursive-self-observation"
    assert packet["fresh_run_authority"] is True
    assert packet["recursive_fresh_safe"] is True
    assert packet["safe_for_fresh_run"] is False


def test_recursive_fresh_run_stays_unsafe_while_another_session_owns_project(tmp_path: Path) -> None:
    module = load()
    run_dir = write_recursive_self_observation_run(tmp_path, "recursive1236", with_sidecar=True)
    write_run(
        tmp_path,
        "owner0000",
        status="running",
        session_authority="submitted_unknown",
    )

    packet = module.build_packet(run_dir)

    assert packet["fresh_run_authority"] is True
    assert packet["recursive_fresh_safe"] is False
    assert [item["run_id"] for item in packet["unresolved_owners"]] == ["owner0000"]


def test_recursive_self_observation_sidecar_requires_exact_schema(tmp_path: Path) -> None:
    module = load()
    run_dir = write_recursive_self_observation_run(tmp_path, "recursive1237")
    (run_dir / "recursive-self-observation.json").write_text(json.dumps({
        "schema": "codex.chatgpt.oracle-other-schema/v1",
        "confirmation_token": "user-authorized-fresh-run-after-recursive-self-observation",
        "expected_state_sha256": "a" * 64,
        "expected_output_sha256": "b" * 64,
        "expected_transcript_sha256": "c" * 64,
    }), encoding="utf-8")

    packet = module.build_packet(run_dir)

    assert packet["fresh_run_authority"] is False
    assert packet["recursive_fresh_safe"] is False


def test_recursive_self_observation_sidecar_requires_all_binding_fields(tmp_path: Path) -> None:
    module = load()
    run_dir = write_recursive_self_observation_run(tmp_path, "recursive1238")
    (run_dir / "recursive-self-observation.json").write_text(json.dumps({
        "schema": "codex.chatgpt.oracle-recursive-self-observation/v1",
        "confirmation_token": "",
        "expected_state_sha256": "a" * 64,
        "expected_output_sha256": "b" * 64,
        "expected_transcript_sha256": "c" * 64,
    }), encoding="utf-8")

    packet = module.build_packet(run_dir)

    assert packet["fresh_run_authority"] is False
    assert packet["recursive_fresh_safe"] is False


def test_packet_build_requires_the_exact_persisted_run(tmp_path: Path) -> None:
    module = load()
    empty = tmp_path / "no-run"
    empty.mkdir()

    with pytest.raises(module.IncidentError) as exc:
        module.build_packet(empty)
    assert exc.value.code == "INCIDENT_RUN_STATE_MISSING"


def test_build_is_read_only_for_the_reported_run(tmp_path: Path) -> None:
    module = load()
    run_dir = write_run(
        tmp_path,
        "9" * 8,
        status="failed",
        stdout="ERROR: --copy-profile requires rsync on PATH\n",
    )
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }

    module.build_packet(run_dir)

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_non_project_reporter_role_is_rejected(tmp_path: Path) -> None:
    module = load()
    run_dir = write_run(tmp_path, "8" * 8, status="failed", stdout="ERROR: unknown\n")

    with pytest.raises(module.IncidentError) as exc:
        module.build_packet(run_dir, reporter_role=module.MAINTENANCE_OWNER)
    assert exc.value.code == "INCIDENT_REPORTER_ROLE_INVALID"
