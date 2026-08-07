from __future__ import annotations

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_diagnose.py"


def load():
    name = "chatgpt_oracle_diagnose_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_run(
    state_root: Path,
    run_id: str,
    *,
    status: str,
    stdout: str = "",
    output: str | None = None,
    session_authority: str = "",
    terminal_harvested: bool = False,
    task_outcome: str = "",
    project_root: Path | None = None,
) -> Path:
    run_dir = state_root / "projects" / "projectkey" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
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
        "project_root": str(project_root or (state_root / "project")),
        "session_authority": session_authority,
        "terminal_harvested": terminal_harvested,
        "task_outcome": task_outcome,
        "artifacts": {"output": str(output_path), "stdout": str(stdout_path), "stderr": str(stderr_path)},
    }), encoding="utf-8")
    return run_dir


def test_report_buckets_pre_submit_ui_and_host_causes_separately(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    write_run(
        state_root,
        "a" * 8,
        status="failed",
        stdout="ERROR: ChatGPT app mention was not confirmed in the composer.\n",
    )
    write_run(
        state_root,
        "b" * 8,
        status="failed",
        stdout="ERROR: --copy-profile requires rsync on PATH (spawn failed): spawn rsync ENOENT\n",
    )
    write_run(
        state_root,
        "c" * 8,
        status="failed",
        stdout="ERROR: Chrome window closed before oracle finished.\n",
    )

    report = module.diagnose(state_root)

    assert report["schema"] == "codex.chatgpt.oracle-diagnosis/v1"
    assert report["total_runs"] == 3
    assert report["bucket_counts"] == {
        "pre-submit-host-environment": 1,
        "pre-submit-ui-contract": 1,
        "browser-lifetime-lost": 1,
    }
    assert len(report["bucket_counts"]) <= 10
    assert report["safe_for_fresh_run_buckets"] == [
        "pre-submit-host-environment",
        "pre-submit-ui-contract",
    ]


def test_unsettled_app_route_marker_remains_submission_uncertain(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    write_run(
        state_root,
        "d" * 8,
        status="attention_required",
        stdout="ERROR: APP_MENTION_ROUTE_UNCONFIRMED\n",
        session_authority="submitted_unknown",
    )

    report = module.diagnose(state_root)

    assert report["bucket_counts"] == {"active-or-uncertain": 1}
    assert report["unresolved_runs"] == []


def test_durable_terminal_run_is_complete_and_not_executed_is_separated(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    write_run(
        state_root,
        "e" * 8,
        status="complete",
        output="answer",
        session_authority="terminal",
        terminal_harvested=True,
        task_outcome="executed",
    )
    write_run(
        state_root,
        "f" * 8,
        status="complete",
        output="TASK_OUTCOME: not_executed",
        session_authority="terminal",
        terminal_harvested=True,
        task_outcome="not_executed",
    )

    report = module.diagnose(state_root)

    assert report["bucket_counts"] == {"complete": 1, "terminal-task-not-executed": 1}
    assert [run["bucket"] for run in report["unresolved_runs"]] == ["terminal-task-not-executed"]


def test_live_run_keeps_ownership_and_is_not_reported_as_failure(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    write_run(
        state_root,
        "1" * 8,
        status="running",
        session_authority="live",
        stdout="status=response streaming\n",
    )

    report = module.diagnose(state_root)

    assert report["bucket_counts"] == {"active-or-uncertain": 1}
    assert report["unresolved_runs"] == []


def test_legacy_complete_ledger_is_not_reported_as_post_submit_defect(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    write_run(
        state_root,
        "7" * 8,
        status="complete",
        output="legacy answer",
        session_authority="",
        terminal_harvested=False,
    )
    write_run(
        state_root,
        "8" * 8,
        status="complete",
        output="TASK_OUTCOME: not_executed",
        session_authority="",
        terminal_harvested=False,
        task_outcome="not_executed",
    )

    report = module.diagnose(state_root)

    assert report["bucket_counts"] == {
        "complete-legacy-ledger": 1,
        "terminal-task-not-executed": 1,
    }
    assert "post-submit-provider-incomplete" not in report["bucket_counts"]


def test_complete_status_without_output_is_not_treated_as_completion(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    write_run(
        state_root,
        "6" * 8,
        status="complete",
        output=None,
    )

    report = module.diagnose(state_root)

    assert report["bucket_counts"] == {"unclassified": 1}
    assert report["unresolved_runs"][0]["signature"] == "no-recognized-signature"


def test_unreadable_state_stays_visible_as_unclassified(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    run_dir = state_root / "projects" / "projectkey" / "runs" / "broken00"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text("{not json", encoding="utf-8")

    report = module.diagnose(state_root)

    assert report["bucket_counts"] == {"unclassified": 1}
    assert report["unresolved_runs"][0]["signature"] == "state-unreadable"


def test_uncertain_submission_timeout_is_a_post_submit_bucket(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    write_run(
        state_root,
        "2" * 8,
        status="failed",
        stdout="ERROR: Prompt did not appear in conversation before timeout (send may have failed)\n",
    )

    report = module.diagnose(state_root)
    run = report["unresolved_runs"][0]

    assert run["bucket"] == "post-submit-provider-incomplete"
    assert run["signature"] == "submission-uncertain-prompt-not-observed"
    # An uncertain send must never be advertised as safe to repeat.
    assert "post-submit-provider-incomplete" not in report["safe_for_fresh_run_buckets"]


def test_host_watchdog_transition_is_post_submit_and_never_retry_safe() -> None:
    module = load()
    verdict = module.classify_run(
        {
            "status": "attention_required",
            "session_authority": "submitted_unknown",
            "terminal_harvested": False,
            "transport_status": "post_submit_watchdog_timeout",
            "task_outcome": "pending",
        },
        stdout_text="response streaming",
        has_output=False,
    )

    assert verdict == {
        "bucket": "post-submit-provider-incomplete",
        "signature": "host-wall-clock-expired-process-preserved",
    }


def test_version_resolution_prelaunch_failure_is_host_safe_only_with_absence_proof() -> None:
    module = load()
    state = {
        "status": "attention_required",
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "task_outcome": "pending",
    }

    for failure_reason, signature in (
        ("version-resolution-timeout", "oracle-version-resolution-prelaunch-timeout"),
        ("compatibility-version-drift", "oracle-version-resolution-prelaunch-compatibility-drift"),
    ):
        verdict = module.classify_run(
            state,
            stdout_text="",
            has_output=False,
            pre_submit_host_failure={
                "code": "ORACLE_VERSION_RESOLUTION_PRELAUNCH_FAILED",
                "output_absent": True,
                "conversation_url_absent": True,
                "failure_reason": failure_reason,
            },
        )
        assert verdict == {
            "bucket": "pre-submit-host-environment",
            "signature": signature,
        }


def test_oracle_attachment_size_preflight_is_host_safe_with_no_conversation() -> None:
    module = load()
    verdict = module.classify_run(
        {
            "status": "attention_required",
            "session_authority": "submitted_unknown",
            "terminal_harvested": False,
            "task_outcome": "pending",
            "pre_submit_failure": {
                "code": "ORACLE_ATTACHMENT_SIZE_PRELAUNCH_FAILED",
                "output_absent": True,
                "conversation_url_absent": True,
            },
        },
        stdout_text="",
        has_output=False,
    )

    assert verdict == {
        "bucket": "pre-submit-host-environment",
        "signature": "oracle-attachment-size-prelaunch-limit",
    }


def test_version_compatibility_drift_is_a_retry_safe_pre_submit_host_failure(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    write_run(state_root, "d" * 8, status="failed")
    run_dir = state_root / "projects" / "projectkey" / "runs" / ("d" * 8)
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["oracle"] = {"resolved_version": "unresolved"}
    state["session_authority"] = "pre_submit"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "stderr.log").write_text(
        "version resolution failed: Oracle compatibility is validated only for the tested version\n",
        encoding="utf-8",
    )

    verdict = module.diagnose(state_root)["unresolved_runs"][0]

    assert verdict["bucket"] == "pre-submit-host-environment"
    assert verdict["signature"] == "oracle-version-resolution-prelaunch-compatibility-drift"


@pytest.mark.parametrize(
    ("stdout_text", "signature"),
    (
        (
            "ERROR: Prompt did not appear in conversation before timeout (send may have failed)\n",
            "user-confirmed-no-submission-after-prompt-timeout",
        ),
        (
            "ERROR: APP_MENTION_ROUTE_UNCONFIRMED\n",
            "user-confirmed-no-submission-after-app-route-unconfirmed",
        ),
    ),
)
def test_user_confirmed_no_submission_overrides_eligible_failure_only_with_validated_proof(
    stdout_text: str,
    signature: str,
) -> None:
    module = load()
    state = {
        "status": "attention_required",
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "task_outcome": "pending",
    }
    verdict = module.classify_run(
        state,
        stdout_text=stdout_text,
        has_output=False,
        user_confirmed_no_submission=True,
    )

    assert verdict == {
        "bucket": "pre-submit-ui-contract",
        "signature": signature,
    }


def test_answer_without_a_durable_artifact_is_recoverable_not_unknown(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    run_dir = write_run(
        state_root,
        "3" * 8,
        status="attention_required",
        stdout="[browser] Released ChatGPT browser slot.\n",
    )
    (run_dir / "transcript.md").write_text("Answer:\nDevSpace 연결 가능합니다.\n", encoding="utf-8")

    report = module.diagnose(state_root)
    run = report["unresolved_runs"][0]

    assert run["bucket"] == "post-submit-provider-incomplete"
    assert run["signature"] == "answer-observed-without-durable-output"


def test_unreadable_state_is_the_only_unclassified_path(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    run_dir = state_root / "projects" / "projectkey" / "runs" / "broken00"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text("{not json", encoding="utf-8")

    report = module.diagnose(state_root)

    assert report["bucket_counts"] == {"unclassified": 1}
    assert {run["signature"] for run in report["unresolved_runs"]} == {"state-unreadable"}


def test_report_is_read_only_for_persisted_runs(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    run_dir = write_run(
        state_root,
        "9" * 8,
        status="failed",
        stdout="ERROR: ChatGPT app mention was not confirmed in the composer.\n",
    )
    before = {
        path.relative_to(state_root).as_posix(): path.read_bytes()
        for path in sorted(state_root.rglob("*"))
        if path.is_file()
    }

    module.diagnose(state_root)

    after = {
        path.relative_to(state_root).as_posix(): path.read_bytes()
        for path in sorted(state_root.rglob("*"))
        if path.is_file()
    }
    assert after == before
    assert (run_dir / "state.json").is_file()


def test_triage_filters_exact_project_and_returns_existing_safe_commands(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    selected = tmp_path / "selected"
    sibling = tmp_path / "selected-child"
    selected.mkdir()
    sibling.mkdir()
    live = write_run(
        state_root,
        "live0000",
        status="running",
        session_authority="live",
        project_root=selected,
    )
    write_run(
        state_root,
        "other000",
        status="running",
        session_authority="live",
        project_root=sibling,
    )

    report = module.triage(state_root=state_root, project_root=selected)

    assert report["schema"] == "codex.chatgpt.oracle-triage/v1"
    assert [item["run_dir"] for item in report["runs"]] == [str(live.resolve())]
    action = report["runs"][0]["next_action"]
    assert action["kind"] == "watch_exact_run"
    assert action["safe_for_fresh_run"] is False
    assert action["argv"][-2:] == ["--run-dir", str(live.resolve())]


def test_triage_never_allows_fresh_run_while_another_exact_owner_exists(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    project = tmp_path / "project"
    project.mkdir()
    failed = write_run(
        state_root,
        "failed00",
        status="failed",
        stdout="ERROR: ChatGPT app mention was not confirmed in the composer.\n",
        project_root=project,
    )
    owner = write_run(
        state_root,
        "owner000",
        status="running",
        session_authority="submitted_unknown",
        project_root=project,
    )

    record = module.triage(state_root=state_root, run_dir=failed)["runs"][0]

    assert record["next_action"]["safe_for_fresh_run"] is False
    assert record["next_action"]["kind"] == "watch_exact_run"
    assert record["next_action"]["argv"][-1] == str(owner.resolve())


def test_triage_consumes_structured_submission_readiness_failure(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    project = tmp_path / "project"
    project.mkdir()
    run_dir = write_run(
        state_root,
        "notready",
        status="attention_required",
        session_authority="pre_submit",
        project_root=project,
    )
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pre_submit_failure"] = {
        "schema": "codex.chatgpt.oracle-pre-submit-readiness-failure/v1",
        "code": "SUBMISSION_NOT_READY",
        "failed_checks": ["devspace_endpoint"],
        "output_absent": True,
        "conversation_url_absent": True,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    record = module.triage(state_root=state_root, run_dir=run_dir)["runs"][0]

    assert record["bucket"] == "pre-submit-host-environment"
    assert record["signature"] == "submission-readiness-not-ready"
    assert record["next_action"]["kind"] == "fix_then_fresh_run"
    assert record["next_action"]["safe_for_fresh_run"] is True


def test_triage_maps_provider_incomplete_to_exact_live_recovery(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    run_dir = write_run(
        state_root,
        "recover0",
        status="failed",
        stdout="ERROR: timed out before completion\n",
    )

    action = module.triage(state_root=state_root, run_dir=run_dir)["runs"][0]["next_action"]

    assert action["kind"] == "recover_live"
    assert action["argv"][-4:] == ["--run-dir", str(run_dir.resolve()), "--action", "live"]


def test_triage_rejects_arbitrary_directories_outside_host_state(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="RUN_DIR_OUTSIDE_STATE_ROOT"):
        module.triage(state_root=state_root, run_dir=outside)


class TtyBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def test_watch_returns_terminal_codes_bells_and_never_mutates_state(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    complete = write_run(
        state_root,
        "complete",
        status="complete",
        output="answer",
        session_authority="terminal",
        terminal_harvested=True,
        task_outcome="executed",
    )
    attention = write_run(state_root, "attentn0", status="failed")
    before = (complete / "state.json").read_bytes()

    events: list[dict] = []
    stderr = TtyBuffer()
    assert module.watch(complete, state_root=state_root, emit=events.append, stderr=stderr) == 0
    assert events[0]["event"] == "snapshot"
    assert events[0]["run"]["lifecycle"] == "complete"
    assert stderr.getvalue() == "\a"
    assert (complete / "state.json").read_bytes() == before
    assert module.watch(attention, state_root=state_root, emit=lambda value: None) == 2


def test_watch_timeout_emits_one_snapshot_and_timeout(tmp_path: Path) -> None:
    module = load()
    state_root = tmp_path / "oracle-state"
    running = write_run(
        state_root,
        "running0",
        status="running",
        session_authority="live",
    )
    current = -0.25

    def clock() -> float:
        nonlocal current
        current += 0.25
        return current

    events: list[dict] = []
    code = module.watch(
        running,
        state_root=state_root,
        poll_seconds=0.25,
        timeout_seconds=0.5,
        emit=events.append,
        sleep=lambda seconds: None,
        clock=clock,
    )

    assert code == 3
    assert [event["event"] for event in events] == ["snapshot", "timeout"]
