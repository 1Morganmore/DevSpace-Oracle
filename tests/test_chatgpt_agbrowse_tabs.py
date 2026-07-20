from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


BIN = Path(__file__).resolve().parents[1] / "bin"


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TABS = load("chatgpt_agbrowse_tabs_test", "chatgpt_agbrowse_tabs.py")


def write_run(
    state_root: Path,
    *,
    run_id: str,
    project_key: str,
    phase: str,
    target_id: str | None = None,
    conversation_url: str | None = None,
    session_id: str | None = None,
    submission_receipt=None,
    result=None,
) -> Path:
    run_dir = state_root / "projects" / project_key / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "schema": "codex.chatgpt.agbrowse-run/v1",
                "run_id": run_id,
                "project_key": project_key,
                "manifest_sha256": "a" * 64,
                "phase": phase,
                "current_target_id": target_id,
                "conversation_url": conversation_url,
                "session_id": session_id,
                "submission_receipt": submission_receipt,
                "result": result,
                "target_rebind_events": [],
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def set_complete_result(run_dir: Path, text: str = "terminal answer") -> None:
    answer_path = run_dir / "answer.md"
    answer_path.write_text(text, encoding="utf-8")
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    record["result"] = {
        "path": str(answer_path),
        "sha256": hashlib.sha256(answer_path.read_bytes()).hexdigest(),
        "bytes": answer_path.stat().st_size,
        "provider_status": "complete",
        "evidence": {"exit_code": 0, "stdout": "captured"},
    }
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")


def set_provider_failure_proof(run_dir: Path) -> None:
    answer_path = run_dir / "provider-terminal-failure.md"
    answer_path.write_text("The stream encountered an error. Retry.", encoding="utf-8")
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    record["terminal_block_code"] = "PROVIDER_TERMINAL_ERROR_UI"
    record["recovery_events"] = [
        {
            "kind": "provider-terminal-error-ui",
            "signature": "chatgpt-stream-error-retry-v1",
            "provider_status": "complete",
            "answer_path": str(answer_path),
            "answer_sha256": hashlib.sha256(answer_path.read_bytes()).hexdigest(),
            "answer_bytes": answer_path.stat().st_size,
            "session_id": record["session_id"],
            "target_id": record["current_target_id"],
            "conversation_url": record["conversation_url"],
        }
    ]
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")


def write_terminal_parent(state_root: Path, *, run_id: str = "parent-1", project_key: str = "project-b", **overrides) -> Path:
    parent = state_root / "projects" / project_key / "runs" / run_id / "run.json"
    parent.parent.mkdir(parents=True)
    record = {
        "schema": "codex.chatgpt.agbrowse-run/v1",
        "record_kind": "parent",
        "run_id": run_id,
        "parent_run_id": run_id,
        "workflow_id": "workflow-1",
        "lease_nonce": "lease-1",
        "project_root": "C:/project-b",
        "project_key": project_key,
        "manifest_path": "C:/project-b/web-multi.json",
        "manifest_sha256": "a" * 64,
        "prompt_sha256": "b" * 64,
        "requested": {"workflow": "web-multi-gpt", "mode": "GPT-5.6", "app_policy": "required"},
        "agbrowse": {"version": "0.1.18"},
        "owner": {"pid": 1, "nonce": "owner-1", "epoch": 1},
        "phase": "PARENT_COMPLETE",
        "phase_events": [{"from": "PARENT_DRAINING", "to": "PARENT_COMPLETE", "at": "2026-07-18T00:00:00+00:00"}],
        "children": [],
        "child_scan": [],
        "recovery_required": False,
        "owned_open_tabs": 0,
    }
    record.update(overrides)
    parent.write_text(json.dumps(record), encoding="utf-8")
    return parent


class BrowserRunner:
    def __init__(self, tabs: list[dict]):
        self.tabs = [dict(item) for item in tabs]
        self.calls: list[list[str]] = []

    def __call__(self, command, env, timeout):
        self.calls.append(list(command))
        if command[1] == "tabs":
            return subprocess.CompletedProcess(command, 0, json.dumps(self.tabs), "")
        if command[1] == "tab-close":
            target = command[2]
            self.tabs = [item for item in self.tabs if item["targetId"] != target]
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "targetId": target}), "")
        raise AssertionError(command)


def test_pre_submit_cleanup_closes_only_exact_owned_composer_and_records_absence(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    run_dir = write_run(
        state_root,
        run_id="run-owned",
        project_key="project-a",
        phase="LEASED",
        target_id="T-OWNED",
    )
    runner = BrowserRunner(
        [
            {"targetId": "T-OWNED", "url": "https://chatgpt.com/", "type": "page"},
            {"targetId": "T-FOREIGN", "url": "https://chatgpt.com/c/keep-me", "type": "page"},
        ]
    )
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)
    lifecycle.record_owned(str(run_dir), target_id="T-OWNED", url="https://chatgpt.com/", stage="pre-submit")

    result = lifecycle.close_pre_submit(str(run_dir), target_id="T-OWNED", reason="test-failure")

    assert result["state"] == "closed-and-absent"
    assert [item["targetId"] for item in runner.tabs] == ["T-FOREIGN"]
    assert [call[1] for call in runner.calls] == ["tabs", "tab-close", "tabs"]
    evidence = json.loads((run_dir / "tab-lifecycle.json").read_text(encoding="utf-8"))
    assert [event["kind"] for event in evidence["events"]] == ["owned", "cleanup"]


def test_automatic_cleanup_refuses_any_submitted_identity(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    run_dir = write_run(
        state_root,
        run_id="run-submitted",
        project_key="project-a",
        phase="SUBMITTED",
        target_id="T-SUBMITTED",
        conversation_url="https://chatgpt.com/c/submitted",
        session_id="S-1",
        submission_receipt={"ok": True},
    )
    set_complete_result(run_dir)
    runner = BrowserRunner([{"targetId": "T-SUBMITTED", "url": "https://chatgpt.com/c/submitted"}])
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)

    with pytest.raises(TABS.TabLifecycleError) as failure:
        lifecycle.close_pre_submit(str(run_dir), target_id="T-SUBMITTED", reason="must-not-close")

    assert failure.value.code == "TAB_PRE_SUBMIT_CLOSE_FORBIDDEN"
    assert runner.calls == []


def test_completed_cleanup_is_automatic_and_requires_exact_target_plus_url(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    url = "https://chatgpt.com/c/completed-run"
    run_dir = write_run(
        state_root,
        run_id="run-complete",
        project_key="project-a",
        phase="COMPLETE",
        target_id="T-RECOVERED",
        conversation_url=url,
        session_id="S-1",
        submission_receipt={"ok": True},
    )
    set_complete_result(run_dir)
    runner = BrowserRunner(
        [
            {"targetId": "T-RECOVERED", "url": url, "type": "page"},
            {"targetId": "T-KEEP", "url": "https://chatgpt.com/c/other", "type": "page"},
        ]
    )
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)

    result = lifecycle.close_completed(str(run_dir), explicit_user_request=False)
    assert result["target_id"] == "T-RECOVERED"
    assert [item["targetId"] for item in runner.tabs] == ["T-KEEP"]
    assert result["state"] == "closed-and-absent"


def test_completed_cleanup_rejects_same_url_on_different_target(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    url = "https://chatgpt.com/c/completed-mismatch"
    run_dir = write_run(
        state_root,
        run_id="run-complete-mismatch",
        project_key="project-a",
        phase="COMPLETE",
        target_id="T-EXPECTED",
        conversation_url=url,
        session_id="S-1",
        submission_receipt={"ok": True},
    )
    set_complete_result(run_dir)
    runner = BrowserRunner([{"targetId": "T-OTHER", "url": url, "type": "page"}])
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)

    with pytest.raises(TABS.TabLifecycleError) as failure:
        lifecycle.close_completed(str(run_dir), explicit_user_request=True)

    assert failure.value.code == "TAB_COMPLETED_TARGET_MISMATCH"
    assert runner.tabs[0]["targetId"] == "T-OTHER"


@pytest.mark.parametrize("invalid_kind", ["missing", "empty", "hash-mismatch", "invalid-bytes", "out-of-run"])
def test_completed_cleanup_fails_closed_without_immutable_run_owned_result(tmp_path: Path, invalid_kind: str) -> None:
    state_root = tmp_path / "state"
    url = f"https://chatgpt.com/c/{invalid_kind}"
    run_dir = write_run(
        state_root,
        run_id=f"run-{invalid_kind}",
        project_key="project-a",
        phase="COMPLETE",
        target_id="T-COMPLETE",
        conversation_url=url,
        session_id="S-1",
        submission_receipt={"ok": True},
    )
    if invalid_kind != "missing":
        set_complete_result(run_dir, "" if invalid_kind == "empty" else "terminal answer")
        record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        if invalid_kind == "hash-mismatch":
            record["result"]["sha256"] = "0" * 64
        elif invalid_kind == "invalid-bytes":
            record["result"]["bytes"] = "not-an-integer"
        elif invalid_kind == "out-of-run":
            external = tmp_path / "external-answer.md"
            external.write_text("terminal answer", encoding="utf-8")
            record["result"]["path"] = str(external)
            record["result"]["sha256"] = hashlib.sha256(external.read_bytes()).hexdigest()
            record["result"]["bytes"] = external.stat().st_size
        (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    runner = BrowserRunner([{"targetId": "T-COMPLETE", "url": url, "type": "page"}])

    with pytest.raises(TABS.TabLifecycleError) as failure:
        TABS.TabLifecycle(state_root=state_root, runner=runner).close_completed(str(run_dir))

    assert failure.value.code == "TAB_COMPLETE_RESULT_EVIDENCE_INVALID"
    assert runner.calls == []
    assert runner.tabs[0]["targetId"] == "T-COMPLETE"


def test_completed_cleanup_rejects_symlinked_result_capture(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    url = "https://chatgpt.com/c/symlink-result"
    run_dir = write_run(
        state_root,
        run_id="run-symlink-result",
        project_key="project-a",
        phase="COMPLETE",
        target_id="T-COMPLETE",
        conversation_url=url,
        session_id="S-1",
        submission_receipt={"ok": True},
    )
    external = tmp_path / "external-answer.md"
    external.write_text("terminal answer", encoding="utf-8")
    answer_path = run_dir / "answer.md"
    try:
        answer_path.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    record["result"] = {
        "path": str(answer_path),
        "sha256": hashlib.sha256(external.read_bytes()).hexdigest(),
        "bytes": external.stat().st_size,
        "provider_status": "complete",
        "evidence": {"exit_code": 0},
    }
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    runner = BrowserRunner([{"targetId": "T-COMPLETE", "url": url, "type": "page"}])

    with pytest.raises(TABS.TabLifecycleError) as failure:
        TABS.TabLifecycle(state_root=state_root, runner=runner).close_completed(str(run_dir))

    assert failure.value.code == "TAB_COMPLETE_RESULT_EVIDENCE_INVALID"
    assert runner.calls == []


def test_completed_cleanup_allows_target_reused_from_terminal_different_url(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    target_id = "T-REUSED"
    current_url = "https://chatgpt.com/c/current-complete"
    run_dir = write_run(
        state_root,
        run_id="run-current",
        project_key="project-a",
        phase="COMPLETE",
        target_id=target_id,
        conversation_url=current_url,
        session_id="S-CURRENT",
    )
    set_complete_result(run_dir)
    write_run(
        state_root,
        run_id="run-old",
        project_key="project-b",
        phase="COMPLETE",
        target_id=target_id,
        conversation_url="https://chatgpt.com/c/old-complete",
        session_id="S-OLD",
    )
    runner = BrowserRunner([{"targetId": target_id, "url": current_url, "type": "page"}])
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)

    result = lifecycle.close_completed(str(run_dir), explicit_user_request=True)

    assert result["state"] == "closed-and-absent"
    assert runner.tabs == []


def test_terminal_rebind_candidate_requires_old_absent_and_one_exact_url_match(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    url = "https://chatgpt.com/c/restarted"
    run_dir = write_run(
        state_root,
        run_id="run-restarted",
        project_key="project-a",
        phase="COMPLETE",
        target_id="T-OLD",
        conversation_url=url,
        session_id="S-1",
        submission_receipt={"ok": True},
    )
    runner = BrowserRunner(
        [
            {"targetId": "T-NEW", "url": url, "type": "page"},
            {"targetId": "T-KEEP", "url": "https://chatgpt.com/c/manual", "type": "page"},
        ]
    )
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)

    candidate = lifecycle.terminal_rebind_candidate(str(run_dir))

    assert candidate["old_target_id"] == "T-OLD"
    assert candidate["new_target_id"] == "T-NEW"
    assert candidate["url_match_count"] == 1
    assert candidate["old_target_absent"] is True
    assert candidate["foreign_owner_absent"] is True


def test_terminal_recovery_utility_cleanup_closes_only_exact_owned_utility(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    url = "https://chatgpt.com/c/recovery-duplicate"
    run_dir = write_run(
        state_root,
        run_id="run-recovery-duplicate",
        project_key="project-a",
        phase="COMPLETE",
        target_id="T-UTILITY",
        conversation_url=url,
        session_id="S-1",
        submission_receipt={"ok": True},
    )
    set_complete_result(run_dir)
    runner = BrowserRunner(
        [
            {"targetId": "T-ORIGINAL", "url": url, "type": "page"},
            {"targetId": "T-UTILITY", "url": url, "type": "page"},
            {"targetId": "T-KEEP", "url": "https://chatgpt.com/c/manual", "type": "page"},
        ]
    )
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)
    lifecycle.record_owned(str(run_dir), target_id="T-ORIGINAL", url=url, stage="submitted")
    lifecycle.record_owned(str(run_dir), target_id="T-UTILITY", url=url, stage="history-adjudication-utility")

    result = lifecycle.close_terminal_recovery_utilities(str(run_dir), explicit_user_request=True)

    assert result["closed_target_ids"] == ["T-UTILITY"]
    assert [item["targetId"] for item in runner.tabs] == ["T-ORIGINAL", "T-KEEP"]
    evidence = json.loads((run_dir / "tab-lifecycle.json").read_text(encoding="utf-8"))
    assert evidence["events"][-1]["kind"] == "terminal-recovery-utility-cleanup"


def test_provider_failed_terminal_cleanup_closes_only_exact_failed_target(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    url = "https://chatgpt.com/c/provider-failed"
    run_dir = write_run(
        state_root,
        run_id="run-provider-failed",
        project_key="project-a",
        phase="PROVIDER_FAILED_TERMINAL",
        target_id="T-FAILED",
        conversation_url=url,
        session_id="S-FAILED",
        submission_receipt={"ok": True},
    )
    set_provider_failure_proof(run_dir)
    runner = BrowserRunner(
        [
            {"targetId": "T-FAILED", "url": url, "type": "page"},
            {"targetId": "T-KEEP", "url": "https://chatgpt.com/c/manual", "type": "page"},
        ]
    )
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)

    result = lifecycle.close_completed(str(run_dir), explicit_user_request=True)

    assert result["target_id"] == "T-FAILED"
    assert [item["targetId"] for item in runner.tabs] == ["T-KEEP"]
    evidence = json.loads((run_dir / "tab-lifecycle.json").read_text(encoding="utf-8"))
    assert evidence["events"][-1]["kind"] == "owned-provider-failed-auto-cleanup"


def test_pre_submit_cleanup_refuses_target_recorded_by_foreign_run(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    owned = write_run(
        state_root,
        run_id="run-owned",
        project_key="project-a",
        phase="LEASED",
        target_id="T-SHARED",
    )
    write_run(
        state_root,
        run_id="run-foreign",
        project_key="project-b",
        phase="SUBMITTED",
        target_id="T-SHARED",
        conversation_url="https://chatgpt.com/c/foreign",
        session_id="S-2",
        submission_receipt={"ok": True},
    )
    runner = BrowserRunner([{"targetId": "T-SHARED", "url": "https://chatgpt.com/", "type": "page"}])
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)
    lifecycle.record_owned(str(owned), target_id="T-SHARED", url="https://chatgpt.com/", stage="pre-submit")

    with pytest.raises(TABS.TabLifecycleError) as failure:
        lifecycle.close_pre_submit(str(owned), target_id="T-SHARED", reason="foreign-owner")

    assert failure.value.code == "TAB_FOREIGN_OWNER"
    assert [call[1] for call in runner.calls] == ["tabs"]


@pytest.mark.parametrize(
    ("foreign_target_id", "foreign_url", "expected_match"),
    [
        ("T-SHARED", "https://chatgpt.com/c/other", "target_match"),
        ("T-FOREIGN", "https://chatgpt.com/", "url_match"),
    ],
)
def test_pre_submit_cleanup_refuses_duplicate_run_id_claiming_owned_identity(
    tmp_path: Path,
    foreign_target_id: str,
    foreign_url: str,
    expected_match: str,
) -> None:
    state_root = tmp_path / "state"
    owned = write_run(
        state_root,
        run_id="run-duplicate",
        project_key="project-a",
        phase="LEASED",
        target_id="T-SHARED",
    )
    foreign = write_run(
        state_root,
        run_id="run-duplicate",
        project_key="project-b",
        phase="SUBMITTED",
        target_id=foreign_target_id,
        conversation_url=foreign_url,
        session_id="S-DUPLICATE",
        submission_receipt={"ok": True},
    )
    runner = BrowserRunner([{"targetId": "T-SHARED", "url": "https://chatgpt.com/", "type": "page"}])
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)
    lifecycle.record_owned(str(owned), target_id="T-SHARED", url="https://chatgpt.com/", stage="pre-submit")

    with pytest.raises(TABS.TabLifecycleError) as failure:
        lifecycle.close_pre_submit(str(owned), target_id="T-SHARED", reason="duplicate-run-id")

    assert failure.value.code == "TAB_FOREIGN_OWNER"
    assert failure.value.evidence["state_file"] == str(foreign / "run.json")
    assert failure.value.evidence["run_id"] == "run-duplicate"
    assert failure.value.evidence[expected_match] is True
    assert [call[1] for call in runner.calls] == ["tabs"]


def test_foreign_owner_excludes_only_exact_own_state_file(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    owned = write_run(
        state_root,
        run_id="run-owned",
        project_key="project-a",
        phase="LEASED",
        target_id="T-OWNED",
        conversation_url="https://chatgpt.com/",
    )
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=BrowserRunner([]))

    assert lifecycle._foreign_owner(
        run_id="run-owned",
        target_id="T-OWNED",
        url="https://chatgpt.com/",
        own_state_file=owned / "run.json",
    ) is None


def test_pre_submit_cleanup_fails_closed_on_unreadable_foreign_run_state(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    owned = write_run(state_root, run_id="run-owned", project_key="project-a", phase="LEASED", target_id="T-OWNED")
    corrupt = state_root / "projects" / "project-b" / "runs" / "run-corrupt" / "run.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{not-json", encoding="utf-8")
    runner = BrowserRunner([{"targetId": "T-OWNED", "url": "https://chatgpt.com/", "type": "page"}])
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)
    lifecycle.record_owned(str(owned), target_id="T-OWNED", url="https://chatgpt.com/", stage="pre-submit")

    with pytest.raises(TABS.TabLifecycleError) as failure:
        lifecycle.close_pre_submit(str(owned), target_id="T-OWNED", reason="foreign-corrupt")

    assert failure.value.code == "TAB_FOREIGN_STATE_UNREADABLE"
    assert failure.value.evidence["state_file"] == str(corrupt)
    assert [call[1] for call in runner.calls] == ["tabs"]
    assert runner.tabs[0]["targetId"] == "T-OWNED"


def test_completed_cleanup_fails_closed_on_partial_foreign_run_state(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    url = "https://chatgpt.com/c/owned-complete"
    owned = write_run(
        state_root,
        run_id="run-owned",
        project_key="project-a",
        phase="COMPLETE",
        target_id="T-OWNED",
        conversation_url=url,
        session_id="S-OWNED",
        submission_receipt={"ok": True},
    )
    set_complete_result(owned)
    partial = state_root / "projects" / "project-b" / "runs" / "run-partial" / "run.json"
    partial.parent.mkdir(parents=True)
    partial.write_text(json.dumps({"schema": "codex.chatgpt.agbrowse-run/v1", "run_id": "run-partial"}), encoding="utf-8")
    runner = BrowserRunner([{"targetId": "T-OWNED", "url": url, "type": "page"}])

    with pytest.raises(TABS.TabLifecycleError) as failure:
        TABS.TabLifecycle(state_root=state_root, runner=runner).close_completed(str(owned))

    assert failure.value.code == "TAB_OWNERSHIP_AMBIGUOUS"
    assert failure.value.evidence["state_file"] == str(partial)
    assert [call[1] for call in runner.calls] == ["tabs"]
    assert runner.tabs[0]["targetId"] == "T-OWNED"


def test_completed_cleanup_skips_valid_unrelated_foreign_run_state(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    url = "https://chatgpt.com/c/owned-valid-unrelated"
    owned = write_run(
        state_root,
        run_id="run-owned",
        project_key="project-a",
        phase="COMPLETE",
        target_id="T-OWNED",
        conversation_url=url,
        session_id="S-OWNED",
        submission_receipt={"ok": True},
    )
    set_complete_result(owned)
    write_run(
        state_root,
        run_id="run-unrelated",
        project_key="project-b",
        phase="COMPLETE",
        target_id="T-UNRELATED",
        conversation_url="https://chatgpt.com/c/unrelated",
        session_id="S-UNRELATED",
        submission_receipt={"ok": True},
    )
    runner = BrowserRunner([{"targetId": "T-OWNED", "url": url, "type": "page"}])

    result = TABS.TabLifecycle(state_root=state_root, runner=runner).close_completed(str(owned))

    assert result["state"] == "closed-and-absent"
    assert runner.tabs == []


@pytest.mark.parametrize(
    ("phase", "recovery_required"),
    [
        ("PARENT_COMPLETE", False),
        ("PARENT_COMPLETE", None),
        ("PARENT_FAILED_CLOSED", True),
    ],
    ids=["complete", "legacy-recovery-omitted", "failed-closed-recovery-true"],
)
def test_completed_cleanup_skips_valid_terminal_parent_coordinator_without_target_fields(
    tmp_path: Path, phase: str, recovery_required: bool | None
) -> None:
    state_root = tmp_path / "state"
    url = "https://chatgpt.com/c/owned-with-parent"
    owned = write_run(
        state_root,
        run_id="run-owned",
        project_key="project-a",
        phase="COMPLETE",
        target_id="T-OWNED",
        conversation_url=url,
        session_id="S-OWNED",
        submission_receipt={"ok": True},
    )
    set_complete_result(owned)
    parent = write_terminal_parent(
        state_root,
        phase=phase,
        phase_events=[{"from": "PARENT_DRAINING", "to": phase, "at": "2026-07-18T00:00:00+00:00"}],
        recovery_required=recovery_required,
    )
    if recovery_required is None:
        record = json.loads(parent.read_text(encoding="utf-8"))
        del record["recovery_required"]
        parent.write_text(json.dumps(record), encoding="utf-8")
    runner = BrowserRunner([{"targetId": "T-OWNED", "url": url, "type": "page"}])

    result = TABS.TabLifecycle(state_root=state_root, runner=runner).close_completed(str(owned))

    assert result["state"] == "closed-and-absent"


def test_completed_cleanup_skips_valid_active_parent_and_non_owning_child_summary(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    url = "https://chatgpt.com/c/owned-with-active-parent"
    owned = write_run(
        state_root,
        run_id="run-owned",
        project_key="project-a",
        phase="COMPLETE",
        target_id="T-OWNED",
        conversation_url=url,
        session_id="S-OWNED",
        submission_receipt={"ok": True},
    )
    set_complete_result(owned)
    write_terminal_parent(
        state_root,
        phase="PARENT_ACTIVE",
        phase_events=[{"from": None, "to": "PARENT_ACTIVE", "at": "2026-07-18T00:00:00+00:00"}],
        child_scan=[{"run_id": "child-1", "phase": "COMPLETE", "cleanup_pending": False, "owned_open_tabs": 1}],
    )
    runner = BrowserRunner([{"targetId": "T-OWNED", "url": url, "type": "page"}])

    result = TABS.TabLifecycle(state_root=state_root, runner=runner).close_completed(str(owned))

    assert result["state"] == "closed-and-absent"


@pytest.mark.parametrize(
    ("overrides", "target_id", "url"),
    [
        (
            {"current_target_id": "T-OWNED", "conversation_url": "https://chatgpt.com/c/owned-parent-identity"},
            "T-OWNED",
            "https://chatgpt.com/c/owned-parent-identity",
        ),
        ({"children": "not-a-list"}, "T-OWNED", "https://chatgpt.com/c/owned-parent-malformed"),
    ],
    ids=["direct-identity", "malformed"],
)
def test_completed_cleanup_fails_closed_for_nonterminal_or_owning_parent(
    tmp_path: Path, overrides: dict, target_id: str, url: str
) -> None:
    state_root = tmp_path / "state"
    owned = write_run(
        state_root,
        run_id="run-owned",
        project_key="project-a",
        phase="COMPLETE",
        target_id=target_id,
        conversation_url=url,
        session_id="S-OWNED",
        submission_receipt={"ok": True},
    )
    set_complete_result(owned)
    write_terminal_parent(state_root, **overrides)
    runner = BrowserRunner([{"targetId": target_id, "url": url, "type": "page"}])

    with pytest.raises(TABS.TabLifecycleError) as failure:
        TABS.TabLifecycle(state_root=state_root, runner=runner).close_completed(str(owned))

    assert failure.value.code == "TAB_OWNERSHIP_AMBIGUOUS"
    assert failure.value.evidence["reason"] == "invalid-parent-coordinator"
    assert [call[1] for call in runner.calls] == ["tabs"]
    assert runner.tabs[0]["targetId"] == target_id


def test_pre_submit_cleanup_is_idempotent_when_owned_target_is_already_absent(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    run_dir = write_run(
        state_root,
        run_id="run-absent",
        project_key="project-a",
        phase="PREFLIGHT_BLOCKED",
        target_id="T-ABSENT",
    )
    runner = BrowserRunner([{"targetId": "T-KEEP", "url": "https://chatgpt.com/c/keep"}])
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)
    lifecycle.record_owned(str(run_dir), target_id="T-ABSENT", url="https://chatgpt.com/", stage="pre-submit")

    result = lifecycle.close_pre_submit(str(run_dir), target_id="T-ABSENT", reason="retry")

    assert result["state"] == "already-absent"
    assert [call[1] for call in runner.calls] == ["tabs"]


def test_pre_submit_target_id_reuse_on_conversation_url_fails_closed(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    run_dir = write_run(
        state_root,
        run_id="run-reused",
        project_key="project-a",
        phase="LEASED",
        target_id="T-REUSED",
    )
    runner = BrowserRunner([{"targetId": "T-REUSED", "url": "https://chatgpt.com/c/reused-target"}])
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)
    lifecycle.record_owned(str(run_dir), target_id="T-REUSED", url="https://chatgpt.com/", stage="pre-submit")

    with pytest.raises(TABS.TabLifecycleError) as failure:
        lifecycle.close_pre_submit(str(run_dir), target_id="T-REUSED", reason="must-not-close")

    assert failure.value.code == "TAB_PRE_SUBMIT_URL_FORBIDDEN"
    assert [call[1] for call in runner.calls] == ["tabs"]


def test_tab_close_failure_is_reported_without_false_absence_evidence(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    run_dir = write_run(
        state_root,
        run_id="run-close-fail",
        project_key="project-a",
        phase="LEASED",
        target_id="T-FAIL",
    )

    class FailingRunner(BrowserRunner):
        def __call__(self, command, env, timeout):
            if command[1] == "tab-close":
                self.calls.append(list(command))
                return subprocess.CompletedProcess(command, 2, "", "close failed")
            return super().__call__(command, env, timeout)

    runner = FailingRunner([{"targetId": "T-FAIL", "url": "https://chatgpt.com/"}])
    lifecycle = TABS.TabLifecycle(state_root=state_root, runner=runner)
    lifecycle.record_owned(str(run_dir), target_id="T-FAIL", url="https://chatgpt.com/", stage="pre-submit")

    with pytest.raises(TABS.TabLifecycleError) as failure:
        lifecycle.close_pre_submit(str(run_dir), target_id="T-FAIL", reason="close-failure")

    assert failure.value.code == "TAB_CLOSE_FAILED"
    events = json.loads((run_dir / "tab-lifecycle.json").read_text(encoding="utf-8"))["events"]
    assert [event["kind"] for event in events] == ["owned"]


def test_bridge_env_neutralizes_upstream_broad_auto_cleanup() -> None:
    bridge = load("chatgpt_agbrowse_bridge_env_test", "chatgpt_agbrowse_bridge.py")
    env = bridge.bridge_env({})
    assert env["AGBROWSE_MAX_TABS"] == "100000"
    assert env["AGBROWSE_TAB_IDLE"] == "999999h"
    assert env["AGBROWSE_PROVIDER_POOL_MAX_PER_KEY"] == "100000"
    assert env["AGBROWSE_PROVIDER_POOL_GLOBAL_MAX"] == "100000"
    assert env["AGBROWSE_PROVIDER_POOL_TTL"] == "999999h"
