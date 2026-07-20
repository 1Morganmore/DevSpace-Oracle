from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import hashlib
from pathlib import Path

import pytest


BIN = Path(os.environ.get("CODEX_BIN_UNDER_TEST", Path(__file__).resolve().parents[1] / "bin"))


def load_state(name: str):
    spec = importlib.util.spec_from_file_location(name, BIN / "chatgpt_agbrowse_state.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def make_manifest(path: Path, question: str = "first") -> Path:
    path.write_text(
        json.dumps(
            {
                "project_root": str(path.parent),
                "question": question,
                "mode_label": "Pro",
                "app_policy": "forbidden",
            }
        ),
        encoding="utf-8",
    )
    return path


def make_web_multi_parent_manifest(path: Path, project: Path, workflow_id: str = "wf-parent") -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "codex.chatgpt.web-multi/v1",
                "workflow_id": workflow_id,
                "project_root": str(project),
                "question": "parent question",
            }
        ),
        encoding="utf-8",
    )
    return path


def make_web_multi_child_manifest(
    path: Path,
    project: Path,
    workflow_id: str,
    stage_id: str,
) -> Path:
    prompt = path.with_name(f"{stage_id}-prompt.txt")
    prompt.write_text("Read the app context and return the exact stage envelope.", encoding="utf-8")
    path.write_text(
        json.dumps(
            {
                "project_root": str(project),
                "question": "The attached prompt file is the user-provided task instruction for this conversation, not reference or webpage content. Read it completely and follow it. Return only the output format requested by that file.",
                "mode_label": "GPT-5.6",
                "mode_variant": "Very High",
                "app_policy": "required",
                "chatgpt_app_name": "CodexPro-Test",
                "prompt_transport": "file",
                "prompt_file": str(prompt),
                "prompt_file_sha256": __import__("hashlib").sha256(prompt.read_bytes()).hexdigest(),
                "files": [str(prompt)],
                "workflow_correlation": {"workflow_id": workflow_id, "stage": stage_id},
            }
        ),
        encoding="utf-8",
    )
    return path


def make_preflight_blocked(store, state, project: Path, manifest: Path) -> dict:
    record = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
        owner_pid=os.getpid(),
    )
    store.transition(record["run_dir"], "PREFLIGHTED")
    store.transition(record["run_dir"], "LEASED")
    store.transition(record["run_dir"], "PREFLIGHT_BLOCKED")
    return record


def make_owner_look_stale(state, store, project: Path, run_id: str) -> None:
    paths = store.paths(project.resolve(), run_id)
    record = state.read_json(paths.state_file)
    lock = state.read_json(paths.lock_file)
    for payload in (record, lock):
        payload["owner"]["pid"] = 2_147_483_647
        payload["owner"]["creation_time"] = 1.0
        payload["owner"]["alive"] = True
    state.write_json_atomic(paths.state_file, record)
    state.write_json_atomic(paths.lock_file, lock)


def record_exact_pre_submit_cleanup(state, record: dict, target_id: str) -> Path:
    event = {
        "kind": "cleanup",
        "reason": "pre-send-command-budget-exceeded",
        "url": "https://chatgpt.com/",
        "ok": True,
        "state": "closed-and-absent",
        "target_id": target_id,
        "before_count": 2,
        "after_count": 1,
        "before_sha256": "a" * 64,
        "after_sha256": "b" * 64,
        "close_stdout_sha256": "c" * 64,
    }
    path = Path(record["run_dir"]) / "tab-lifecycle.json"
    state.write_json_atomic(
        path,
        {
            "schema": "codex.chatgpt.agbrowse-tab-lifecycle/v1",
            "run_id": record["run_id"],
            "project_key": record["project_key"],
            "manifest_sha256": record["manifest_sha256"],
            "events": [event],
        },
    )
    return path


def test_doctor_recomputes_owner_instead_of_trusting_stored_alive(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_doctor_observation_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    first = make_preflight_blocked(store, state, project, manifest)
    make_owner_look_stale(state, store, project, first["run_id"])

    diagnosis = store.reconcile_project_lock(project)

    assert diagnosis["ok"] is True
    assert diagnosis["state"] == "STALE_PRE_SUBMISSION_SAFE_TO_CANCEL"
    assert diagnosis["owner_observation"]["stored"]["alive"] is True
    assert diagnosis["owner_observation"]["observed"]["alive"] is False
    assert diagnosis["owner_observation"]["same_process"] is False


def test_raw_send_started_can_enter_same_run_recovery(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_raw_send_recovery_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    record = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
        owner_pid=os.getpid(),
    )

    store.transition(record["run_dir"], "PREFLIGHTED")
    store.transition(record["run_dir"], "LEASED")
    store.transition(record["run_dir"], "SEND_STARTED")
    recovered = store.transition(record["run_dir"], "RECOVERING")

    assert recovered["run_id"] == record["run_id"]
    assert recovered["phase"] == "RECOVERING"


def test_process_identity_reports_a_recently_exited_windows_pid_as_dead() -> None:
    state = load_state("chatgpt_agbrowse_state_recent_dead_pid_test")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
    stored = state.process_identity(child.pid)
    assert stored["alive"] is True
    assert stored["creation_time"] is not None
    child.wait(timeout=10)

    observed = state.process_identity(child.pid)

    assert observed["pid"] == child.pid
    assert state.same_process(stored) is False


def test_new_run_reports_supported_reconcile_then_explicit_command_releases(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_doctor_explicit_cancel_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    first = make_preflight_blocked(store, state, project, manifest)
    make_owner_look_stale(state, store, project, first["run_id"])
    make_manifest(manifest, question="changed after the blocked preflight")

    with pytest.raises(state.StateError) as caught:
        store.create_run(
            project_root=project,
            manifest_path=manifest,
            agbrowse_contract={"schema": "contract", "version": "0.1.18"},
        )
    assert caught.value.code == "STALE_PRE_SUBMISSION_SAFE_TO_CANCEL"
    assert "--reconcile-project-lock" in caught.value.evidence["supported_reconcile_command"]

    reconciled = store.reconcile_project_lock(project, apply_safe_pre_submission=True)
    assert reconciled["state"] == "STALE_PRE_SUBMISSION_CANCELLED"
    second = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
    )

    _, cancelled = store.load(first["run_dir"])
    assert cancelled["phase"] == "CANCELLED_PRE_SUBMISSION"
    assert cancelled["recovery_events"][-1]["kind"] == "stale-owner-pre-submission-reconciled"
    assert second["run_id"] != first["run_id"]
    lock = state.read_json(store.paths(project.resolve(), second["run_id"]).lock_file)
    assert lock["run_id"] == second["run_id"]


@pytest.mark.parametrize("phase", ["CREATED", "PREFLIGHTED", "LEASED"])
def test_dead_owner_without_target_or_send_evidence_is_safely_cancelled(
    tmp_path: Path,
    phase: str,
) -> None:
    state = load_state(f"chatgpt_agbrowse_state_safe_{phase.lower()}_test")
    project = tmp_path / phase.lower()
    project.mkdir()
    manifest = make_manifest(tmp_path / f"{phase.lower()}.json")
    store = state.RunStore(tmp_path / f"state-{phase.lower()}")
    record = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
        owner_pid=os.getpid(),
    )
    if phase in {"PREFLIGHTED", "LEASED"}:
        store.transition(record["run_dir"], "PREFLIGHTED")
    if phase == "LEASED":
        store.transition(record["run_dir"], "LEASED")
    make_owner_look_stale(state, store, project, record["run_id"])

    diagnosis = store.reconcile_project_lock(project, apply_safe_pre_submission=True)

    assert diagnosis["ok"] is True
    assert diagnosis["state"] == "STALE_PRE_SUBMISSION_CANCELLED"
    _, cancelled = store.load(record["run_dir"])
    assert cancelled["phase"] == "CANCELLED_PRE_SUBMISSION"


def test_doctor_does_not_remove_terminal_orphan_lock(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_doctor_read_only_terminal_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    first = make_preflight_blocked(store, state, project, manifest)
    make_owner_look_stale(state, store, project, first["run_id"])
    store.reconcile_project_lock(project, apply_safe_pre_submission=True)
    paths = store.paths(project.resolve(), first["run_id"])
    lock = {
        "schema": state.SCHEMA,
        "run_id": first["run_id"],
        "project_root": str(project.resolve()),
        "project_key": state.project_key(project.resolve()),
        "manifest_sha256": first["manifest_sha256"],
        "owner": first["owner"],
        "phase": "CANCELLED_PRE_SUBMISSION",
        "session_id": None,
        "target_id": None,
        "conversation_url": None,
        "heartbeat_at": first["created_at"],
    }
    state.write_json_atomic(paths.lock_file, lock)

    diagnosis = store.reconcile_project_lock(project, apply_safe_pre_submission=False)

    assert diagnosis["state"] == "TERMINAL_ORPHAN_LOCK_DETECTED"
    assert diagnosis["changed"] is False
    assert paths.lock_file.is_file()


def test_dead_owner_with_unverified_pre_submit_target_is_not_auto_classified_safe(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_doctor_narrow_phase_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    first = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
        owner_pid=os.getpid(),
    )
    store.transition(first["run_dir"], "PREFLIGHTED")
    store.transition(first["run_dir"], "LEASED", target_id="target-without-cleanup-evidence")
    make_owner_look_stale(state, store, project, first["run_id"])

    diagnosis = store.reconcile_project_lock(project, apply_safe_pre_submission=True)

    assert diagnosis["ok"] is False
    assert diagnosis["state"] == "STALE_OWNER_PRE_SUBMIT_TARGET_PRESENT"


def test_live_exact_owner_is_not_reconciled(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_doctor_live_owner_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    first = make_preflight_blocked(store, state, project, manifest)

    diagnosis = store.reconcile_project_lock(project, apply_safe_pre_submission=True)

    assert diagnosis["ok"] is False
    assert diagnosis["state"] == "ACTIVE_PROJECT_OWNER"
    assert store.paths(project.resolve(), first["run_id"]).lock_file.is_file()


def test_dead_owner_after_send_boundary_stays_blocked_with_precise_state(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_doctor_post_send_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    first = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
        owner_pid=os.getpid(),
    )
    store.transition(first["run_dir"], "PREFLIGHTED")
    store.transition(first["run_dir"], "LEASED")
    store.transition(first["run_dir"], "SEND_STARTED")
    make_owner_look_stale(state, store, project, first["run_id"])

    diagnosis = store.reconcile_project_lock(project, apply_safe_pre_submission=True)

    assert diagnosis["ok"] is False
    assert diagnosis["state"] == "STALE_OWNER_UNRESOLVED_SUBMISSION"
    assert "--abandon-uncertain-run" in diagnosis["supported_abandon_command"]
    assert "--explicit-user-request" in diagnosis["supported_abandon_command"]
    assert store.paths(project.resolve(), first["run_id"]).lock_file.is_file()
    with pytest.raises(state.StateError) as caught:
        store.create_run(
            project_root=project,
            manifest_path=manifest,
            agbrowse_contract={"schema": "contract", "version": "0.1.18"},
        )
    assert caught.value.code == "STALE_OWNER_UNRESOLVED_SUBMISSION"


def test_dead_app_transaction_failure_is_reconciled_as_proven_pre_submission(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_doctor_app_transaction_pre_send_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    first = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
        owner_pid=os.getpid(),
    )
    store.transition(first["run_dir"], "PREFLIGHTED")
    store.transition(
        first["run_dir"],
        "BLOCKED_APP_TRANSACTION",
        block_code="APP_TRANSACTION_FAILED",
        recovery_event={"kind": "app-transaction-failed", "detail": "pre-send failure"},
    )
    make_owner_look_stale(state, store, project, first["run_id"])

    diagnosis = store.reconcile_project_lock(project, apply_safe_pre_submission=False)
    assert diagnosis["state"] == "STALE_PRE_SUBMISSION_SAFE_TO_CANCEL"

    reconciled = store.reconcile_project_lock(project, apply_safe_pre_submission=True)
    assert reconciled["state"] == "STALE_PRE_SUBMISSION_CANCELLED"
    _, cancelled = store.load(first["run_dir"])
    assert cancelled["phase"] == "CANCELLED_PRE_SUBMISSION"
    assert cancelled["session_id"] is None
    assert cancelled["conversation_url"] is None
    assert not store.paths(project.resolve(), first["run_id"]).lock_file.exists()


def test_app_transaction_label_cannot_override_historical_send_boundary(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_doctor_app_transaction_post_send_guard_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    first = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
        owner_pid=os.getpid(),
    )
    store.transition(first["run_dir"], "PREFLIGHTED")
    store.transition(first["run_dir"], "LEASED")
    store.transition(first["run_dir"], "SEND_STARTED")
    paths = store.paths(project.resolve(), first["run_id"])
    record = state.read_json(paths.state_file)
    lock = state.read_json(paths.lock_file)
    record["phase"] = "BLOCKED_APP_TRANSACTION"
    lock["phase"] = "BLOCKED_APP_TRANSACTION"
    state.write_json_atomic(paths.state_file, record)
    state.write_json_atomic(paths.lock_file, lock)
    make_owner_look_stale(state, store, project, first["run_id"])

    diagnosis = store.reconcile_project_lock(project, apply_safe_pre_submission=True)

    assert diagnosis["ok"] is False
    assert diagnosis["state"] == "STALE_OWNER_UNRESOLVED_SUBMISSION"
    assert paths.lock_file.is_file()


def test_dead_pre_submit_owner_with_target_is_not_silently_released(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_doctor_target_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    first = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
        owner_pid=os.getpid(),
    )
    store.transition(first["run_dir"], "PREFLIGHTED")
    store.transition(first["run_dir"], "LEASED", target_id="T-PRE-SUBMIT")
    make_owner_look_stale(state, store, project, first["run_id"])

    diagnosis = store.reconcile_project_lock(project, apply_safe_pre_submission=True)

    assert diagnosis["ok"] is False
    assert diagnosis["state"] == "STALE_OWNER_PRE_SUBMIT_TARGET_PRESENT"
    assert diagnosis["target_id"] == "T-PRE-SUBMIT"


def test_dead_pre_submit_owner_accepts_exact_closed_and_absent_evidence(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_doctor_verified_target_cleanup_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    first = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
        owner_pid=os.getpid(),
    )
    target_id = "T-PRE-SUBMIT"
    store.transition(first["run_dir"], "PREFLIGHTED")
    store.transition(first["run_dir"], "LEASED", target_id=target_id)
    ledger_path = record_exact_pre_submit_cleanup(state, first, target_id)
    event = state.read_json(ledger_path)["events"][-1]
    store.transition(
        first["run_dir"],
        "PREFLIGHT_BLOCKED",
        recovery_event={
            "kind": "pre-submit-command-budget-exceeded",
            "cleanup": {
                **event,
                "evidence": {
                    "path": str(ledger_path),
                    "sha256": state.sha256_file(ledger_path),
                    "event": event,
                },
            },
        },
    )
    make_owner_look_stale(state, store, project, first["run_id"])

    diagnosis = store.reconcile_project_lock(project, apply_safe_pre_submission=False)
    assert diagnosis["state"] == "STALE_PRE_SUBMISSION_SAFE_TO_CANCEL"

    reconciled = store.reconcile_project_lock(project, apply_safe_pre_submission=True)
    assert reconciled["state"] == "STALE_PRE_SUBMISSION_CANCELLED"
    _, cancelled = store.load(first["run_dir"])
    assert cancelled["phase"] == "CANCELLED_PRE_SUBMISSION"
    assert cancelled["current_target_id"] is None
    cleanup = cancelled["recovery_events"][-1]["pre_submit_target_cleanup"]
    assert cleanup["target_id"] == target_id
    assert cleanup["sha256"] == state.sha256_file(ledger_path)


def test_dead_pre_submit_owner_rejects_tampered_cleanup_ledger(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_doctor_tampered_target_cleanup_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    first = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
        owner_pid=os.getpid(),
    )
    target_id = "T-PRE-SUBMIT"
    store.transition(first["run_dir"], "PREFLIGHTED")
    store.transition(first["run_dir"], "LEASED", target_id=target_id)
    ledger_path = record_exact_pre_submit_cleanup(state, first, target_id)
    event = state.read_json(ledger_path)["events"][-1]
    ledger_sha256 = state.sha256_file(ledger_path)
    store.transition(
        first["run_dir"],
        "PREFLIGHT_BLOCKED",
        recovery_event={
            "kind": "pre-submit-command-budget-exceeded",
            "cleanup": {
                **event,
                "evidence": {"path": str(ledger_path), "sha256": ledger_sha256, "event": event},
            },
        },
    )
    ledger_path.write_text(ledger_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    make_owner_look_stale(state, store, project, first["run_id"])

    diagnosis = store.reconcile_project_lock(project, apply_safe_pre_submission=True)

    assert diagnosis["ok"] is False
    assert diagnosis["state"] == "STALE_OWNER_PRE_SUBMIT_TARGET_PRESENT"
    assert store.paths(project.resolve(), first["run_id"]).lock_file.is_file()


def test_supported_reconcile_command_releases_safe_stale_run(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_doctor_cli_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    state_root = tmp_path / "state"
    store = state.RunStore(state_root)
    first = make_preflight_blocked(store, state, project, manifest)
    make_owner_look_stale(state, store, project, first["run_id"])

    completed = subprocess.run(
        [
            sys.executable,
            str(BIN / "chatgpt_agbrowse_run.py"),
            "--state-root",
            str(state_root),
            "--reconcile-project-lock",
            str(project),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["result"]["state"] == "STALE_PRE_SUBMISSION_CANCELLED"
    _, cancelled = store.load(first["run_dir"])
    assert cancelled["phase"] == "CANCELLED_PRE_SUBMISSION"


def test_recovery_required_uncommitted_attachment_can_be_reclassified_with_exact_evidence(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_uncommitted_attachment_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    run = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
    )
    store.transition(run["run_dir"], "PREFLIGHTED")
    store.transition(run["run_dir"], "LEASED")
    store.transition(run["run_dir"], "SEND_STARTED")
    store.transition(run["run_dir"], "SUBMITTED", session_id="S-UNSENT", target_id="T-UNSENT")
    store.transition(run["run_dir"], "RECOVERY_REQUIRED")
    evidence_path = Path(run["run_dir"]) / "uncommitted-send-evidence.json"
    evidence_path.write_text('{"send_click":"unresolved","reason":"not-enabled"}', encoding="utf-8")

    rejected = store.transition(
        run["run_dir"],
        "SEND_REJECTED",
        recovery_event={
            "kind": "verified-mutation-disallowed-reclassification",
            "mutation_allowed": False,
            "send_click_status": "unresolved",
            "send_click_reason": "not-enabled",
            "assistant_count": 0,
            "session_id": "S-UNSENT",
            "target_id": "T-UNSENT",
            "session_status": "complete",
            "observed_url": "https://chatgpt.com/?prompt=still-in-composer",
            "evidence_path": str(evidence_path),
            "evidence_sha256": state.sha256_file(evidence_path),
        },
    )
    cancelled = store.transition(run["run_dir"], "CANCELLED_PRE_SUBMISSION")

    assert rejected["phase"] == "SEND_REJECTED"
    assert cancelled["phase"] == "CANCELLED_PRE_SUBMISSION"
    assert not store.paths(project.resolve(), run["run_id"]).lock_file.exists()


def test_recovery_required_expired_sent_session_can_be_reclassified_when_target_is_absent(
    tmp_path: Path,
) -> None:
    state = load_state("chatgpt_agbrowse_state_expired_sent_attachment_test")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    run = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
    )
    store.transition(run["run_dir"], "PREFLIGHTED")
    store.transition(run["run_dir"], "LEASED")
    store.transition(run["run_dir"], "SEND_STARTED")
    store.transition(run["run_dir"], "SUBMITTED", session_id="S-EXPIRED", target_id="T-ABSENT")
    store.transition(run["run_dir"], "RECOVERY_REQUIRED")
    evidence_path = Path(run["run_dir"]) / "expired-uncommitted-send-evidence.json"
    evidence_path.write_text('{"send_click":"unresolved","reason":"not-enabled"}', encoding="utf-8")

    rejected = store.transition(
        run["run_dir"],
        "SEND_REJECTED",
        recovery_event={
            "kind": "verified-mutation-disallowed-reclassification",
            "mutation_allowed": False,
            "send_click_status": "unresolved",
            "send_click_reason": "not-enabled",
            "assistant_count": 0,
            "session_id": "S-EXPIRED",
            "target_id": "T-ABSENT",
            "session_status": "sent",
            "observed_url": "https://chatgpt.com/",
            "session_deadline_expired": True,
            "target_absent": True,
            "evidence_path": str(evidence_path),
            "evidence_sha256": state.sha256_file(evidence_path),
        },
    )

    assert rejected["phase"] == "SEND_REJECTED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mutation_allowed", True),
        ("send_click_status", "ok"),
        ("send_click_reason", "visible"),
        ("assistant_count", 1),
        ("session_id", "S-OTHER"),
        ("target_id", "T-OTHER"),
        ("session_status", "sent"),
        ("observed_url", "https://chatgpt.com/c/6a57121e-76cc-83e8-aaa6-028bea86c3dc"),
    ],
)
def test_recovery_required_reclassification_rejects_incomplete_or_post_send_evidence(
    tmp_path: Path,
    field: str,
    value,
) -> None:
    state = load_state(f"chatgpt_agbrowse_state_uncommitted_reject_{field}")
    project = tmp_path / "project"
    project.mkdir()
    manifest = make_manifest(tmp_path / "manifest.json")
    store = state.RunStore(tmp_path / "state")
    run = store.create_run(
        project_root=project,
        manifest_path=manifest,
        agbrowse_contract={"schema": "contract", "version": "0.1.18"},
    )
    store.transition(run["run_dir"], "PREFLIGHTED")
    store.transition(run["run_dir"], "LEASED")
    store.transition(run["run_dir"], "SEND_STARTED")
    store.transition(run["run_dir"], "SUBMITTED", session_id="S-UNSENT", target_id="T-UNSENT")
    store.transition(run["run_dir"], "RECOVERY_REQUIRED")
    evidence_path = Path(run["run_dir"]) / "uncommitted-send-evidence.json"
    evidence_path.write_text('{"send_click":"unresolved","reason":"not-enabled"}', encoding="utf-8")
    evidence = {
        "kind": "verified-mutation-disallowed-reclassification",
        "mutation_allowed": False,
        "send_click_status": "unresolved",
        "send_click_reason": "not-enabled",
        "assistant_count": 0,
        "session_id": "S-UNSENT",
        "target_id": "T-UNSENT",
        "session_status": "complete",
        "observed_url": "https://chatgpt.com/?prompt=still-in-composer",
        "evidence_path": str(evidence_path),
        "evidence_sha256": state.sha256_file(evidence_path),
    }
    evidence[field] = value

    with pytest.raises(state.StateError) as caught:
        store.transition(run["run_dir"], "SEND_REJECTED", recovery_event=evidence)

    assert caught.value.code == "RECOVERY_RECLASSIFICATION_UNPROVEN"


def test_parent_allows_owned_children_blocks_same_project_and_allows_distinct_project(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_parent_ownership_test")
    store = state.RunStore(tmp_path / "state")
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    parent_a_manifest = make_web_multi_parent_manifest(tmp_path / "parent-a.json", project_a, "wf-a")
    parent_b_manifest = make_web_multi_parent_manifest(tmp_path / "parent-b.json", project_b, "wf-b")
    parent_a = store.create_parent_workflow(
        project_root=project_a,
        manifest_path=parent_a_manifest,
        workflow_id="wf-a",
        agbrowse_contract={"version": "0.1.18"},
    )
    child_manifest = make_web_multi_child_manifest(tmp_path / "child-a.json", project_a, "wf-a", "solver-0")
    child = store.create_child_run(
        parent_run_dir=parent_a["run_dir"], manifest_path=child_manifest,
        agbrowse_contract={"version": "0.1.18"}, role="Solver", lane=0, iteration=0, stage_id="solver-0",
    )
    with pytest.raises(state.StateError) as same_project:
        store.create_parent_workflow(
            project_root=project_a, manifest_path=parent_a_manifest, workflow_id="wf-a",
            agbrowse_contract={"version": "0.1.18"},
        )
    parent_b = store.create_parent_workflow(
        project_root=project_b, manifest_path=parent_b_manifest, workflow_id="wf-b",
        agbrowse_contract={"version": "0.1.18"},
    )

    assert same_project.value.code == "SAME_PROJECT_ACTIVE_OR_UNCERTAIN"
    assert child["parent_run_id"] == parent_a["run_id"]
    assert parent_b["project_key"] != parent_a["project_key"]


def test_resume_parent_recreates_missing_exact_lock_only_after_owner_is_dead(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_parent_missing_lock_resume_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(
        project_root=project,
        manifest_path=parent_manifest,
        workflow_id="wf-parent",
        agbrowse_contract={"version": "0.1.18"},
    )
    make_owner_look_stale(state, store, project, parent["run_id"])
    paths = store.paths(project.resolve(), parent["run_id"])
    paths.lock_file.unlink()

    resumed = store.resume_parent_workflow(parent["run_dir"], parent_manifest)
    lock = state.read_json(paths.lock_file)

    assert resumed["phase"] == "PARENT_ACTIVE"
    assert lock["run_id"] == parent["run_id"]
    assert lock["lease_nonce"] == resumed["lease_nonce"]
    assert lock["manifest_sha256"] == resumed["manifest_sha256"]
    assert resumed["parent_lock_recovery_events"][-1]["kind"] == "missing-parent-lock-recreated"
    assert resumed["parent_lock_recovery_events"][-1]["lease_nonce"] == resumed["lease_nonce"]


def test_resume_parent_never_reconstructs_missing_lock_over_live_foreign_owner(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_parent_live_owner_missing_lock_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(
        project_root=project,
        manifest_path=parent_manifest,
        workflow_id="wf-parent",
        agbrowse_contract={"version": "0.1.18"},
    )
    paths = store.paths(project.resolve(), parent["run_id"])
    paths.lock_file.unlink()

    with pytest.raises(state.StateError) as failure:
        store.resume_parent_workflow(parent["run_dir"], parent_manifest, owner_pid=2_147_483_647)

    assert failure.value.code == "ACTIVE_PROJECT_OWNER"
    assert not paths.lock_file.exists()


def test_resume_parent_restores_original_record_when_final_reconstructed_lock_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = load_state("chatgpt_agbrowse_state_parent_lock_rollback_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(
        project_root=project,
        manifest_path=parent_manifest,
        workflow_id="wf-parent",
        agbrowse_contract={"version": "0.1.18"},
    )
    make_owner_look_stale(state, store, project, parent["run_id"])
    paths = store.paths(project.resolve(), parent["run_id"])
    paths.lock_file.unlink()
    original_record = state.read_json(paths.state_file)
    real_write = state.write_json_atomic

    def injected_write(path: Path, payload: dict) -> None:
        if Path(path) == paths.lock_file:
            raise OSError("injected final reconstructed lock write failure")
        real_write(Path(path), payload)

    monkeypatch.setattr(state, "write_json_atomic", injected_write)

    with pytest.raises(OSError, match="injected final reconstructed lock write failure"):
        store.resume_parent_workflow(parent["run_dir"], parent_manifest)

    assert state.read_json(paths.state_file) == original_record
    assert not paths.lock_file.exists()


def test_child_send_claim_is_durable_and_exactly_once(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_child_send_claim_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(
        project_root=project, manifest_path=parent_manifest, workflow_id="wf-parent",
        agbrowse_contract={"version": "0.1.18"},
    )
    child_manifest = make_web_multi_child_manifest(tmp_path / "child.json", project, "wf-parent", "solver-0")
    child = store.create_child_run(
        parent_run_dir=parent["run_dir"], manifest_path=child_manifest,
        agbrowse_contract={"version": "0.1.18"}, role="Solver", lane=0, iteration=0, stage_id="solver-0",
    )
    store.transition(child["run_dir"], "PREFLIGHTED")
    store.transition(child["run_dir"], "LEASED")
    claimed = store.claim_child_send(child["run_dir"])
    with pytest.raises(state.StateError) as duplicate:
        store.assert_child_send_available(child["run_dir"])

    assert claimed["phase"] == "SEND_STARTED"
    assert claimed["send_attempt_count"] == 1
    assert Path(child["run_dir"], "send.claim").is_file()
    assert duplicate.value.code == "SEND_ALREADY_ATTEMPTED"


def test_mutation_disallowed_child_reuses_same_claim_under_exact_retry_authority(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_child_retry_authority_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(
        project_root=project,
        manifest_path=parent_manifest,
        workflow_id="wf-parent",
        agbrowse_contract={"version": "0.1.18"},
    )
    child_manifest = make_web_multi_child_manifest(tmp_path / "child.json", project, "wf-parent", "solver-0")
    child = store.create_child_run(
        parent_run_dir=parent["run_dir"],
        manifest_path=child_manifest,
        agbrowse_contract={"version": "0.1.18"},
        role="Solver",
        lane=0,
        iteration=0,
        stage_id="solver-0",
    )
    store.transition(child["run_dir"], "PREFLIGHTED")
    store.transition(child["run_dir"], "LEASED", target_id="TARGET-RETRY")
    claimed = store.claim_child_send(child["run_dir"])
    evidence_dir = Path(child["run_dir"]) / "agbrowse-evidence"
    evidence_dir.mkdir()
    (evidence_dir / "send.stdout.txt").write_text("", encoding="utf-8")
    (evidence_dir / "send.stderr.txt").write_text(
        json.dumps(
            {
                "ok": False,
                "status": "error",
                "error": {
                    "errorCode": "provider.active-capacity",
                    "stage": "provider-capacity",
                    "mutationAllowed": False,
                },
            }
        ),
        encoding="utf-8",
    )
    store.transition(
        child["run_dir"],
        "SEND_REJECTED",
        recovery_event={
            "kind": "verified-mutation-disallowed-reclassification",
            "error_code": "provider.active-capacity",
            "error_stage": "provider-capacity",
        },
    )
    failed = store.finalize_parent(
        parent["run_dir"],
        "PARENT_FAILED_CLOSED",
        failure={"code": "CHILD_NOT_COMPLETE", "message": "solver-0: SEND_REJECTED"},
    )
    failed_state = state.read_json(Path(parent["run_dir"]) / "run.json")
    failed_state["owner"]["pid"] = 2_147_483_647
    failed_state["owner"]["creation_time"] = 1.0
    state.write_json_atomic(Path(parent["run_dir"]) / "run.json", failed_state)

    reopened = store.reopen_failed_parent_workflow(parent["run_dir"], parent_manifest)
    lifecycle_path = Path(child["run_dir"]) / "tab-lifecycle.json"
    state.write_json_atomic(
        lifecycle_path,
        {
            "schema": "codex.chatgpt.agbrowse-tab-lifecycle/v1",
            "run_id": child["run_id"],
            "manifest_sha256": child["manifest_sha256"],
            "events": [{"kind": "cleanup", "target_id": "TARGET-RETRY", "state": "closed-and-absent"}],
        },
    )
    cleanup = {
        "ok": True,
        "state": "closed-and-absent",
        "target_id": "TARGET-RETRY",
        "evidence": {"path": str(lifecycle_path), "sha256": state.sha256_file(lifecycle_path)},
    }
    store.record_child_cleanup(child["run_dir"], cleanup)
    authorized = store.authorize_child_pre_submit_retry(child["run_dir"], cleanup)
    store.transition(child["run_dir"], "PREFLIGHTED")
    store.transition(child["run_dir"], "LEASED")
    store.transition(
        child["run_dir"],
        "LEASED",
        target_id="TARGET-REPLACEMENT",
        rebind_reason="pre-submit-composer-retry",
    )
    replacement_evidence = Path(child["run_dir"]) / "composer-app-evidence.json"
    state.write_json_atomic(
        replacement_evidence,
        {
            "state": "composer-app-mention-tab-confirmed",
            "target_id": "TARGET-REPLACEMENT",
            "selection_method": "exact-at-mention-then-tab",
        },
    )
    store.confirm_child_retry_replacement(
        child["run_dir"],
        target_id="TARGET-REPLACEMENT",
        evidence_path=replacement_evidence,
    )
    retried = store.claim_child_send(child["run_dir"])

    assert claimed["send_attempt_count"] == 1
    assert reopened["run_id"] == parent["run_id"]
    assert reopened["pre_submit_retry_candidates"][0]["run_id"] == child["run_id"]
    assert authorized["pre_submit_retry_authority"]["eligible"] is True
    assert retried["send_attempt_count"] == 1
    assert retried["pre_submit_retry_count"] == 1
    assert retried["pre_submit_retry_authority"]["consumed_at"]
    assert retried["pre_submit_retry_authority"]["replacement_target_id"] == "TARGET-REPLACEMENT"
    assert state.sha256_file(Path(child["run_dir"]) / "send.claim") == claimed["send_claim"]["sha256"]
    store.finalize_parent(
        parent["run_dir"],
        "PARENT_FAILED_CLOSED",
        failure={"code": "test-cleanup"},
    )


def test_child_retry_replacement_binds_immutable_deep_research_evidence(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_research_retry_authority_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(
        project_root=project, manifest_path=parent_manifest, workflow_id="wf-parent",
        agbrowse_contract={"version": "0.1.18"},
    )
    child_manifest = make_web_multi_child_manifest(tmp_path / "child.json", project, "wf-parent", "solver-research")
    child = store.create_child_run(
        parent_run_dir=parent["run_dir"], manifest_path=child_manifest,
        agbrowse_contract={"version": "0.1.18"}, role="Solver", lane=0, iteration=0, stage_id="solver-research",
    )
    store.transition(child["run_dir"], "PREFLIGHTED")
    store.transition(child["run_dir"], "LEASED", target_id="TARGET-OLD")
    store.claim_child_send(child["run_dir"])
    evidence_dir = Path(child["run_dir"]) / "agbrowse-evidence"
    evidence_dir.mkdir()
    (evidence_dir / "send.stdout.txt").write_text("", encoding="utf-8")
    (evidence_dir / "send.stderr.txt").write_text(json.dumps({
        "ok": False,
        "error": {"errorCode": "provider.active-capacity", "stage": "provider-capacity", "mutationAllowed": False},
    }), encoding="utf-8")
    store.transition(child["run_dir"], "SEND_REJECTED", recovery_event={
        "kind": "verified-mutation-disallowed-reclassification",
        "error_code": "provider.active-capacity", "error_stage": "provider-capacity",
    })
    lifecycle = Path(child["run_dir"]) / "tab-lifecycle.json"
    state.write_json_atomic(lifecycle, {"events": [{"kind": "cleanup", "target_id": "TARGET-OLD", "state": "closed-and-absent"}]})
    cleanup = {"ok": True, "state": "closed-and-absent", "target_id": "TARGET-OLD", "evidence": {
        "path": str(lifecycle), "sha256": state.sha256_file(lifecycle),
    }}
    store.record_child_cleanup(child["run_dir"], cleanup)
    store.authorize_child_pre_submit_retry(child["run_dir"], cleanup)
    store.transition(child["run_dir"], "PREFLIGHTED")
    store.transition(child["run_dir"], "LEASED")

    token_hash = hashlib.sha256("@심층 리서치".encode("utf-8")).hexdigest()
    hashes = {"token_sha256": token_hash, "before_snapshot_sha256": "a" * 64,
              "after_snapshot_sha256": "b" * 64, "action_transcript_sha256": "c" * 64}

    def bind(evidence: dict, *, target="TARGET-RESEARCH") -> Path:
        path = Path(child["run_dir"]) / f"composer-research-{target}.json"
        state.write_json_atomic(path, evidence)
        store.transition(
            child["run_dir"], "LEASED", target_id=target,
            rebind_reason="pre-submit-composer-retry",
            selection_evidence_ref={"kind": "deep-research-selection", "path": str(path),
                                    "sha256": state.sha256_file(path), "target_id": target},
        )
        return path

    evidence = {
        "schema": "codex.chatgpt.capability-selection/v1", "state": "deep-research-selected",
        "run_id": child["run_id"], "workflow_id": "wf-parent", "target_id": "TARGET-RESEARCH",
        "selection_transport": "preselected-research", "selected_marker": {"name": "심층 리서치"}, **hashes,
        "selection_proof": {"kind": "token-to-pill-transition", "marker_identity_sha256": "d" * 64, **hashes},
    }
    path = bind(evidence)
    confirmed = store.confirm_child_retry_replacement(child["run_dir"], target_id="TARGET-RESEARCH", evidence_path=path)
    authority = confirmed["pre_submit_retry_authority"]
    assert authority["replacement_target_id"] == "TARGET-RESEARCH"
    assert authority["replacement_evidence_path"] == str(path.resolve())
    assert authority["replacement_evidence_sha256"] == state.sha256_file(path)


def test_child_retry_research_replacement_rejects_tampering_without_authority_mutation(tmp_path: Path) -> None:
    # The full successful binding above covers persistence.  Here exercise the
    # validator with a compact, already-authorized run fixture by reusing its
    # exact setup through the state-machine transitions in the preceding test's
    # contract: malformed proof and foreign identities must not bind authority.
    state = load_state("chatgpt_agbrowse_state_research_retry_tamper_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"; project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(project_root=project, manifest_path=parent_manifest, workflow_id="wf-parent", agbrowse_contract={"version": "0.1.18"})
    child_manifest = make_web_multi_child_manifest(tmp_path / "child.json", project, "wf-parent", "solver-tamper")
    child = store.create_child_run(parent_run_dir=parent["run_dir"], manifest_path=child_manifest, agbrowse_contract={"version": "0.1.18"}, role="Solver", lane=0, iteration=0, stage_id="solver-tamper")
    # Directly seed the already-proven authority; validation below is solely the
    # replacement-evidence gate and must not mutate this immutable authority.
    state_file = Path(child["run_dir"]) / "run.json"
    record = state.read_json(state_file)
    record.update({"phase": "LEASED", "current_target_id": "TARGET-RESEARCH", "pre_submit_retry_authority": {
        "eligible": True, "consumed_at": None, "cleanup_target_id": "TARGET-OLD",
    }})
    record["target_rebind_events"].append({"old_target_id": "TARGET-OLD", "new_target_id": "TARGET-RESEARCH", "reason": "pre-submit-composer-retry"})
    state.write_json_atomic(state_file, record)
    hashes = {"token_sha256": hashlib.sha256("@심층 리서치".encode("utf-8")).hexdigest(), "before_snapshot_sha256": "a" * 64, "after_snapshot_sha256": "b" * 64, "action_transcript_sha256": "c" * 64}
    base = {"schema": "codex.chatgpt.capability-selection/v1", "state": "deep-research-selected", "run_id": child["run_id"], "workflow_id": "wf-parent", "target_id": "TARGET-RESEARCH", "selection_transport": "preselected-research", "selected_marker": {"name": "심층 리서치"}, **hashes, "selection_proof": {"kind": "token-to-pill-transition", "marker_identity_sha256": "d" * 64, **hashes}}
    for name, mutate in (("proof", lambda value: value["selection_proof"].update({"after_snapshot_sha256": "0" * 64})), ("workflow", lambda value: value.update({"workflow_id": "foreign"})), ("target", lambda value: value.update({"target_id": "foreign"}))):
        value = json.loads(json.dumps(base))
        mutate(value)
        path = Path(child["run_dir"]) / f"{name}.json"; state.write_json_atomic(path, value)
        record = state.read_json(state_file)
        record["selection_evidence_refs"] = [{"kind": "deep-research-selection", "path": str(path.resolve()), "sha256": state.sha256_file(path), "target_id": "TARGET-RESEARCH"}]
        state.write_json_atomic(state_file, record)
        with pytest.raises(state.StateError) as failure:
            store.confirm_child_retry_replacement(child["run_dir"], target_id="TARGET-RESEARCH", evidence_path=path)
        assert failure.value.code == "PRE_SUBMIT_RETRY_REPLACEMENT_EVIDENCE_INVALID"
        assert state.read_json(state_file)["pre_submit_retry_authority"].get("replacement_target_id") is None


def test_pre_submit_retry_rejects_conflicting_send_stdout(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_child_retry_stdout_conflict_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(
        project_root=project,
        manifest_path=parent_manifest,
        workflow_id="wf-parent",
        agbrowse_contract={"version": "0.1.18"},
    )
    child_manifest = make_web_multi_child_manifest(tmp_path / "child.json", project, "wf-parent", "solver-0")
    child = store.create_child_run(
        parent_run_dir=parent["run_dir"], manifest_path=child_manifest,
        agbrowse_contract={"version": "0.1.18"}, role="Solver", lane=0, iteration=0, stage_id="solver-0",
    )
    store.transition(child["run_dir"], "PREFLIGHTED")
    store.transition(child["run_dir"], "LEASED", target_id="TARGET-CONFLICT")
    store.claim_child_send(child["run_dir"])
    evidence_dir = Path(child["run_dir"]) / "agbrowse-evidence"
    evidence_dir.mkdir()
    (evidence_dir / "send.stdout.txt").write_text('{"ok":true,"sessionId":"conflict"}', encoding="utf-8")
    (evidence_dir / "send.stderr.txt").write_text(
        '{"ok":false,"error":{"errorCode":"provider.active-capacity","stage":"provider-capacity","mutationAllowed":false}}',
        encoding="utf-8",
    )
    store.transition(
        child["run_dir"], "SEND_REJECTED",
        recovery_event={
            "kind": "verified-mutation-disallowed-reclassification",
            "error_code": "provider.active-capacity",
            "error_stage": "provider-capacity",
        },
    )

    with pytest.raises(state.StateError) as conflict:
        store.pre_submit_retry_candidate(child["run_dir"])

    assert conflict.value.code == "PRE_SUBMIT_RETRY_STDOUT_CONFLICT"
    store.finalize_parent(
        parent["run_dir"], "PARENT_FAILED_CLOSED", failure={"code": "test-cleanup"},
    )


def test_create_child_and_parent_drain_share_one_atomic_barrier(tmp_path: Path, monkeypatch) -> None:
    state = load_state("chatgpt_agbrowse_state_create_drain_barrier_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(
        project_root=project, manifest_path=parent_manifest, workflow_id="wf-parent",
        agbrowse_contract={"version": "0.1.18"},
    )
    child_manifest = make_web_multi_child_manifest(tmp_path / "child.json", project, "wf-parent", "solver-0")
    creator_inside = threading.Event()
    release_creator = threading.Event()
    original_replace = state.os.replace

    def delayed_child_publish(source, target):
        if Path(source).name.startswith(".child-") and Path(target).parent.name == "runs":
            creator_inside.set()
            assert release_creator.wait(5)
        return original_replace(source, target)

    monkeypatch.setattr(state.os, "replace", delayed_child_publish)
    results: dict[str, object] = {}

    def create_child() -> None:
        results["child"] = store.create_child_run(
            parent_run_dir=parent["run_dir"], manifest_path=child_manifest,
            agbrowse_contract={"version": "0.1.18"}, role="Solver", lane=0, iteration=0, stage_id="solver-0",
        )

    def drain_parent() -> None:
        results["parent"] = store.finalize_parent(
            parent["run_dir"], "PARENT_COMPLETE", result={"sha256": "a" * 64},
        )

    creator = threading.Thread(target=create_child)
    finalizer = threading.Thread(target=drain_parent)
    creator.start()
    assert creator_inside.wait(5)
    finalizer.start()
    release_creator.set()
    creator.join(5)
    finalizer.join(5)

    assert not creator.is_alive() and not finalizer.is_alive()
    parent_result = results["parent"]
    child_result = results["child"]
    assert isinstance(parent_result, dict) and parent_result["phase"] == "PARENT_RECOVERY_REQUIRED"
    assert isinstance(child_result, dict)
    assert child_result["run_id"] in {item["run_id"] for item in parent_result["child_scan"]}


def test_parent_drain_first_rejects_late_child_without_artifact(tmp_path: Path, monkeypatch) -> None:
    state = load_state("chatgpt_agbrowse_state_drain_first_barrier_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(
        project_root=project, manifest_path=parent_manifest, workflow_id="wf-parent",
        agbrowse_contract={"version": "0.1.18"},
    )
    child_manifest = make_web_multi_child_manifest(tmp_path / "child.json", project, "wf-parent", "solver-late")
    finalizer_scanning = threading.Event()
    release_finalizer = threading.Event()
    original_children = store._parent_children

    def delayed_scan(runs_dir: Path, parent_run_id: str):
        finalizer_scanning.set()
        assert release_finalizer.wait(5)
        return original_children(runs_dir, parent_run_id)

    monkeypatch.setattr(store, "_parent_children", delayed_scan)
    results: dict[str, object] = {}

    def finalize() -> None:
        results["parent"] = store.finalize_parent(
            parent["run_dir"], "PARENT_COMPLETE", result={"sha256": "b" * 64},
        )

    def create_late() -> None:
        try:
            store.create_child_run(
                parent_run_dir=parent["run_dir"], manifest_path=child_manifest,
                agbrowse_contract={"version": "0.1.18"}, role="Solver", lane=0, iteration=0, stage_id="solver-late",
            )
        except state.StateError as exc:
            results["child_error"] = exc.code

    finalizer = threading.Thread(target=finalize)
    creator = threading.Thread(target=create_late)
    finalizer.start()
    assert finalizer_scanning.wait(5)
    creator.start()
    release_finalizer.set()
    finalizer.join(5)
    creator.join(5)

    assert results["parent"]["phase"] == "PARENT_COMPLETE"
    assert results["child_error"] == "PARENT_NOT_ACTIVE"
    run_records = list(store.paths(project.resolve(), parent["run_id"]).runs_dir.glob("*/run.json"))
    assert run_records == [Path(parent["run_dir"]) / "run.json"]


def test_parent_cannot_reactivate_after_draining_started(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_parent_monotonic_drain_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(
        project_root=project,
        manifest_path=parent_manifest,
        workflow_id="wf-parent",
        agbrowse_contract={"version": "0.1.18"},
    )
    child_manifest = make_web_multi_child_manifest(
        tmp_path / "child.json",
        project,
        "wf-parent",
        "solver-0",
    )
    store.create_child_run(
        parent_run_dir=parent["run_dir"],
        manifest_path=child_manifest,
        agbrowse_contract={"version": "0.1.18"},
        role="Solver",
        lane=0,
        iteration=0,
        stage_id="solver-0",
    )
    draining = store.finalize_parent(
        parent["run_dir"],
        "PARENT_COMPLETE",
        result={"sha256": "c" * 64},
    )

    assert draining["phase"] == "PARENT_RECOVERY_REQUIRED"
    with pytest.raises(state.StateError) as caught:
        store.resume_parent_workflow(
            parent["run_dir"],
            parent_manifest,
            reactivate=True,
        )

    assert caught.value.code == "PARENT_REACTIVATION_INVALID"
    _, latest = store.load(parent["run_dir"])
    assert latest["phase"] == "PARENT_RECOVERY_REQUIRED"
    assert not any(
        event.get("from") in {"PARENT_DRAINING", "PARENT_RECOVERY_REQUIRED"}
        and event.get("to") == "PARENT_ACTIVE"
        for event in latest["phase_events"]
    )

def test_child_cleanup_requires_explicit_exact_canonical_url(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_child_cleanup_url_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(
        project_root=project,
        manifest_path=parent_manifest,
        workflow_id="wf-parent",
        agbrowse_contract={"version": "0.1.18"},
    )
    child_manifest = make_web_multi_child_manifest(
        tmp_path / "child.json",
        project,
        "wf-parent",
        "solver-0",
    )
    child = store.create_child_run(
        parent_run_dir=parent["run_dir"],
        manifest_path=child_manifest,
        agbrowse_contract={"version": "0.1.18"},
        role="Solver",
        lane=0,
        iteration=0,
        stage_id="solver-0",
    )
    run_dir = child["run_dir"]
    store.transition(run_dir, "PREFLIGHTED")
    store.transition(run_dir, "LEASED")
    store.claim_child_send(run_dir)
    store.transition(
        run_dir,
        "SUBMITTED",
        session_id="session-cleanup",
        target_id="target-cleanup",
        submission_receipt={"test": True},
    )
    store.transition(
        run_dir,
        "URL_BOUND",
        conversation_url="https://chatgpt.com/c/cleanup-exact",
    )

    with pytest.raises(state.StateError) as caught:
        store.record_child_cleanup(
            run_dir,
            {
                "ok": True,
                "state": "closed-and-absent",
                "target_id": "target-cleanup",
            },
        )

    assert caught.value.code == "CHILD_CLEANUP_URL_MISMATCH"
    cleaned = store.record_child_cleanup(
        run_dir,
        {
            "ok": True,
            "state": "closed-and-absent",
            "target_id": "target-cleanup",
            "conversation_url": "https://chatgpt.com/c/cleanup-exact",
        },
    )
    assert cleaned["cleanup_pending"] is False
    assert cleaned["owned_open_tabs"] == 0


def test_active_parent_runtime_recovery_blocks_new_children_until_exact_clear(tmp_path: Path) -> None:
    state = load_state("chatgpt_agbrowse_state_parent_runtime_recovery_test")
    store = state.RunStore(tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = make_web_multi_parent_manifest(tmp_path / "parent.json", project)
    parent = store.create_parent_workflow(
        project_root=project,
        manifest_path=parent_manifest,
        workflow_id="wf-parent",
        agbrowse_contract={"version": "0.1.18"},
    )
    first_manifest = make_web_multi_child_manifest(
        tmp_path / "first.json",
        project,
        "wf-parent",
        "solver-0",
    )
    first = store.create_child_run(
        parent_run_dir=parent["run_dir"],
        manifest_path=first_manifest,
        agbrowse_contract={"version": "0.1.18"},
        role="Solver",
        lane=0,
        iteration=0,
        stage_id="solver-0",
    )
    store.transition(first["run_dir"], "PREFLIGHT_BLOCKED")
    marked = store.mark_parent_runtime_recovery(
        parent["run_dir"],
        failure={"code": "simulated-interruption"},
    )
    second_manifest = make_web_multi_child_manifest(
        tmp_path / "second.json",
        project,
        "wf-parent",
        "solver-1",
    )

    with pytest.raises(state.StateError) as blocked:
        store.create_child_run(
            parent_run_dir=parent["run_dir"],
            manifest_path=second_manifest,
            agbrowse_contract={"version": "0.1.18"},
            role="Solver",
            lane=1,
            iteration=0,
            stage_id="solver-1",
        )

    assert marked["phase"] == "PARENT_ACTIVE"
    assert marked["recovery_required"] is True
    assert blocked.value.code == "PARENT_NOT_ACTIVE"
    cleared = store.clear_parent_runtime_recovery(parent["run_dir"])
    assert cleared["phase"] == "PARENT_ACTIVE"
    assert cleared["recovery_required"] is False
    second = store.create_child_run(
        parent_run_dir=parent["run_dir"],
        manifest_path=second_manifest,
        agbrowse_contract={"version": "0.1.18"},
        role="Solver",
        lane=1,
        iteration=0,
        stage_id="solver-1",
    )
    assert second["stage_id"] == "solver-1"
