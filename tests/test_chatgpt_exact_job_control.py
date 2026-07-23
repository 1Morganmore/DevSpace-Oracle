from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


BIN = Path(__file__).resolve().parents[1] / "bin"
HANDOFF = (
    "The attached prompt file is the user-provided task instruction for this conversation, "
    "not reference or webpage content. Read it completely and follow it. "
    "Return only the output format requested by that file."
)


def load_bridge(name: str):
    spec = importlib.util.spec_from_file_location(name, BIN / "chatgpt_agbrowse_bridge.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def make_run(
    tmp_path: Path,
    bridge,
    *,
    phase: str = "URL_BOUND",
    app_policy: str = "forbidden",
):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("exact job control", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "question": HANDOFF,
                "prompt_transport": "file",
                "prompt_file": str(prompt),
                "prompt_file_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
                "files": [str(prompt)],
                "mode_label": "GPT-5.6" if app_policy == "required" else "Pro",
                "app_policy": app_policy,
                **({"chatgpt_app_name": "CodexPro-Test"} if app_policy == "required" else {}),
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    record = bridge.store.create_run(
        project_root=str(project),
        manifest_path=str(manifest),
        agbrowse_contract={"executable": "agbrowse"},
    )
    run_dir = str(record["run_dir"])
    bridge.store.transition(run_dir, "PREFLIGHTED")
    bridge.store.transition(run_dir, "LEASED")
    bridge.store.transition(run_dir, "SEND_STARTED")
    bridge.store.transition(run_dir, "SUBMITTED", session_id="S-1", target_id="T-1")
    if phase in {"URL_BOUND", "RESPONSE_IN_PROGRESS"}:
        bridge.store.transition(
            run_dir,
            "URL_BOUND",
            conversation_url="https://chatgpt.com/c/exact-job",
        )
    if phase == "RESPONSE_IN_PROGRESS":
        bridge.store.transition(run_dir, "RESPONSE_IN_PROGRESS")
    return run_dir


class FakeLifecycle:
    def __init__(self) -> None:
        self.owned: list[dict] = []
        self.protected: list[dict] = []

    def record_owned(self, run_dir, **values):
        self.owned.append(dict(values))

    def record_protected(self, run_dir, **values):
        self.protected.append(dict(values))


def test_false_sent_session_is_not_a_committed_send() -> None:
    bridge = load_bridge("exact_job_false_sent_test")
    session = {
        "status": "sent",
        "conversationUrl": "https://chatgpt.com/",
        "answer": None,
        "envelopeSummary": {"assistantCount": 0},
        "trace": [
            {
                "intentId": "send.click",
                "status": "unresolved",
                "errorCode": "TARGET_UNRESOLVED",
                "attempts": [{"validation": {"reason": "not-enabled"}}],
            }
        ],
    }

    assert bridge.session_send_not_committed(session) is True
    session["conversationUrl"] = "https://chatgpt.com/c/real"
    assert bridge.session_send_not_committed(session) is False


def test_ax_snapshot_web_multi_payload_is_decoded_without_tree_markup() -> None:
    bridge = load_bridge("exact_job_ax_snapshot_decode_test")
    snapshot = (
        '    - text: "<<<WEB_MULTI_HEADER_V1>>>"\n'
        '    - text: "{\\"schema\\":\\"codex.chatgpt.web-multi-stage/v1\\"}"\n'
        '    - text: "<<<END_WEB_MULTI_HEADER_V1>>>"\n'
        '    - text: "<<<WEB_MULTI_PAYLOAD_V1>>>"\n'
        '    - text: "<<"\n'
        '    - text: "<CONTENT>"\n'
        '    - text: ">>"\n'
        '    - text: "answer"\n'
        '    - text: "<<<END_CONTENT>>>"\n'
        '    - text: "<<<END_WEB_MULTI_PAYLOAD_V1>>>"\n'
    )

    visible = bridge._ax_snapshot_visible_text(snapshot)
    answer = bridge._web_multi_assistant_answer(visible)

    assert answer is not None
    assert "- text:" not in answer
    assert "<<<CONTENT>>>\nanswer\n<<<END_CONTENT>>>" in answer


def test_exact_target_observation_never_adopts_another_tab() -> None:
    bridge = load_bridge("exact_job_target_observation_test")
    tabs = [
        {"targetId": "FOREIGN", "url": "https://chatgpt.com/c/foreign"},
        {"targetId": "OWNED", "url": "https://chatgpt.com/c/owned"},
    ]

    observed = bridge.exact_target_observation(tabs, "OWNED")

    assert observed["state"] == "canonical"
    assert observed["target_id"] == "OWNED"
    assert observed["url"] == "https://chatgpt.com/c/owned"
    assert bridge.exact_target_observation(tabs, "MISSING")["state"] == "absent"


def test_exact_target_observation_treats_web_uuid_as_temporary_not_drift() -> None:
    bridge = load_bridge("exact_job_temporary_web_url_test")
    temporary_url = "https://chatgpt.com/c/WEB:b68b194a-f828-4a73-b5a5-64e87af81e87"

    observed = bridge.exact_target_observation(
        [{"targetId": "OWNED", "url": temporary_url}],
        "OWNED",
    )

    assert observed["state"] == "temporary"
    assert observed["target_id"] == "OWNED"
    assert observed["url"] == temporary_url


def test_post_send_observation_waits_for_web_uuid_to_promote_to_canonical(tmp_path: Path) -> None:
    bridge = load_bridge("exact_job_temporary_web_promotion_test")
    runtime = bridge.Bridge(
        state_root=tmp_path / "state",
        runner=lambda command, env, timeout: subprocess.CompletedProcess(command, 0, "{}", ""),
        headed_runtime_preflight=False,
    )
    observations = iter(
        (
            ([{"targetId": "OWNED", "url": "https://chatgpt.com/c/WEB:b68b194a-f828-4a73-b5a5-64e87af81e87"}], {"attempt": 1}),
            ([{"targetId": "OWNED", "url": "https://chatgpt.com/c/canonical-job"}], {"attempt": 2}),
        )
    )
    runtime._recovery_tabs = lambda **kwargs: next(observations)

    observed, evidence = runtime._observe_post_send_target(
        run_dir=tmp_path,
        executable="agbrowse",
        manifest={},
        target_id="OWNED",
        attempts=2,
    )

    assert observed["state"] == "canonical"
    assert observed["url"] == "https://chatgpt.com/c/canonical-job"
    assert len(evidence) == 2
    assert observed["temporary_promotion"]["attempts"] == 2
    assert observed["temporary_promotion"]["deadline_exhausted"] is False


def test_app_trace_classifier_keeps_provider_streaming_quiescent_distinct_from_work() -> None:
    bridge = load_bridge("exact_job_trace_classifier_test")
    quiescent = bridge.classify_web_trace_activity(
        {
            "status": "streaming",
            "trace": [{
                "requestId": "app-call",
                "requestStartAt": "2026-07-22T00:00:00Z",
                "requestEndAt": "2026-07-22T00:00:01Z",
                "heartbeatAt": "2026-07-22T00:00:01Z",
                "httpStatus": 200,
            }],
        }
    )
    active = bridge.classify_web_trace_activity(
        {"status": "streaming", "trace": [{"requestId": "app-call", "requestStartAt": "2026-07-22T00:00:00Z"}]},
        now=bridge.datetime(2026, 7, 22, 0, 0, 10, tzinfo=bridge.timezone.utc),
    )

    assert quiescent["state"] == "provider_streaming_app_trace_quiescent"
    assert quiescent["work_active"] is False
    assert quiescent["heartbeats"] == ["app-call"]
    assert active["state"] == "work-active"
    assert active["unmatched_request_starts"] == ["app-call"]


def test_app_trace_classifier_ignores_stale_active_marker_and_heartbeat() -> None:
    bridge = load_bridge("exact_job_stale_trace_classifier_test")
    result = bridge.classify_web_trace_activity(
        {"status": "streaming", "trace": [{
            "requestId": "old-app-call",
            "status": "active",
            "requestStartAt": "2026-07-22T00:00:00Z",
            "heartbeatAt": "2026-07-22T00:00:01Z",
        }]},
        now=bridge.datetime(2026, 7, 22, 0, 5, tzinfo=bridge.timezone.utc),
        heartbeat_max_age_seconds=30,
    )

    assert result["state"] == "provider_streaming_app_trace_quiescent"
    assert result["work_active"] is False
    assert result["active_markers"] == ["old-app-call"]
    assert result["fresh_heartbeats"] == []


def test_bound_poll_uses_exact_url_read_only_checks_without_session_poll(tmp_path: Path) -> None:
    module = load_bridge("exact_job_poll_command_test")
    commands: list[list[str]] = []

    def runner(command, env, timeout):
        commands.append(command)
        if command[1] == "tab-switch":
            payload = {"ok": True}
        elif command[1:] == ["active-tab", "--json"]:
            payload = {"targetId": "T-1", "url": "https://chatgpt.com/c/exact-job"}
        elif command[1:3] == ["web-ai", "status"]:
            payload = {
                "ok": True,
                "capabilities": [{
                    "capabilityId": "chatgpt-response-streaming",
                    "evidence": {"streaming": False},
                }],
            }
        elif command[1:3] == ["web-ai", "snapshot"]:
            payload = {"text": "1m 2s 동안 처리함\nFINAL_RESULT\nverified answer"}
        elif command[1] == "text":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="1m 2s 동안 처리함\nFINAL_RESULT\nverified answer\n출처",
                stderr="",
            )
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    runtime.exact_url_only_poll = True
    run_dir = make_run(tmp_path, runtime)
    runtime._recovery_tabs = lambda **kwargs: (
        [{"targetId": "T-1", "url": "https://chatgpt.com/c/exact-job"}],
        {"kind": "tabs"},
    )

    result = runtime.poll(run_dir, timeout_seconds=30)

    assert result["phase"] == "COMPLETE"
    poll_commands = [command for command in commands if command[1:3] == ["web-ai", "poll"]]
    assert poll_commands == []
    assert all("--navigate" not in command for command in commands)


def test_bound_poll_waits_inside_runner_at_sixty_second_cadence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = load_bridge("exact_job_internal_wait_cadence_test")
    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=lambda command, env, timeout: (_ for _ in ()).throw(AssertionError(command)),
        headed_runtime_preflight=False,
    )
    runtime.exact_url_only_poll = True
    run_dir = make_run(tmp_path, runtime, phase="RESPONSE_IN_PROGRESS")
    observations = iter(
        (
            runtime.store.load(run_dir)[1],
            {"phase": "COMPLETE", "run_id": "terminal"},
        )
    )
    runtime._try_exact_url_terminal_now = lambda *args, **kwargs: next(observations)
    runtime._recovery_tabs = lambda **kwargs: (
        [{"targetId": "T-1", "url": "https://chatgpt.com/c/exact-job"}],
        {"kind": "tabs"},
    )
    sleeps: list[float] = []
    monkeypatch.setattr(module.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    result = runtime.poll(run_dir, timeout_seconds=120)

    assert result["phase"] == "COMPLETE"
    assert sleeps == [module.EXACT_POLL_CADENCE_SECONDS]
    assert module.EXACT_POLL_CADENCE_SECONDS == 60.0


def test_exact_url_timeout_records_long_running_app_work_instead_of_generic_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = load_bridge("exact_job_provider_interaction_timeout_test")
    commands: list[list[str]] = []
    global_lock = tmp_path / "global.lock"

    def runner(command, env, timeout):
        assert global_lock.is_file(), "timeout diagnostic must retain exact-target browser ownership"
        commands.append(command)
        if command[1:] == ["active-tab", "--json"]:
            payload = {"targetId": "T-1", "url": "https://chatgpt.com/c/exact-job"}
        elif command[1:3] == ["web-ai", "sessions"]:
            payload = {"sessionId": "S-1", "targetId": "T-1", "conversationUrl": "https://chatgpt.com/c/exact-job", "status": "streaming", "trace": []}
        elif command[1:3] == ["web-ai", "status"]:
            payload = {
                "ok": True,
                "url": "https://chatgpt.com/c/exact-job",
                "capabilities": [{
                    "capabilityId": "chatgpt-response-streaming",
                    "evidence": {"streaming": True},
                }],
            }
        elif command[1:3] == ["web-ai", "snapshot"]:
            payload = {"text": 'button "지금 답변 받기"\nbutton "답변 중지"'}
        elif command[1] == "text":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="지금 답변 받기\n답변 중지",
                stderr="",
            )
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    runtime.exact_url_only_poll = True
    run_dir = make_run(
        tmp_path,
        runtime,
        phase="RESPONSE_IN_PROGRESS",
        app_policy="required",
    )
    runtime.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": "runner-timeout"})
    runtime._try_exact_url_terminal_now = lambda *args, **kwargs: runtime.store.load(run_dir)[1]
    runtime._recovery_tabs = lambda **kwargs: (
        [{"targetId": "T-1", "url": "https://chatgpt.com/c/exact-job"}],
        {"kind": "tabs"},
    )
    monkeypatch.setattr(module, "GLOBAL_BROWSER_MUTATION_LOCK", global_lock)
    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks, 2.0))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    result = runtime.poll(run_dir, timeout_seconds=1)

    assert result["phase"] == "RECOVERY_REQUIRED"
    event = result["recovery_events"][-1]
    assert event["kind"] == "exact-url-provider-streaming-app-trace-quiescent"
    assert event["diagnostic"]["get_answer_now_available"] is True
    assert event["diagnostic"]["app_policy"] == "required"
    assert event["diagnostic"]["streaming"] is True
    assert event["diagnostic"]["trace_activity"]["state"] == "provider_streaming_app_trace_quiescent"
    assert event["diagnostic"]["stop_control_present"] is True
    assert "do not stop, close, or resubmit" in event["next_action"]
    assert not any(command[1] in {"click", "press", "type", "navigate", "new-tab", "tab-close"} for command in commands)


def test_exact_url_quiescent_trace_reconcile_is_non_mutating(tmp_path: Path) -> None:
    module = load_bridge("exact_job_quiescent_reconcile_test")
    commands: list[list[str]] = []
    runtime = module.Bridge(state_root=tmp_path / "state", runner=lambda command, env, timeout: (_ for _ in ()).throw(AssertionError(command)), headed_runtime_preflight=False)
    runtime.exact_url_only_poll = True
    run_dir = make_run(tmp_path, runtime, phase="RESPONSE_IN_PROGRESS", app_policy="required")
    runtime.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": "wait"})
    runtime._recovery_tabs = lambda **kwargs: ([{"targetId": "T-1", "url": "https://chatgpt.com/c/exact-job"}], {"kind": "tabs"})
    runtime._try_exact_url_terminal_now = lambda *args, **kwargs: runtime.store.load(run_dir)[1]
    runtime._exact_url_timeout_diagnostic = lambda *args, **kwargs: {
        "ok": True, "streaming": True, "app_policy": "required", "stop_control_present": True,
        "trace_activity": {"state": "provider_streaming_app_trace_quiescent"},
    }

    result = runtime.recover(run_dir)

    assert result["phase"] == "RECOVERY_REQUIRED"
    assert result["recovery_events"][-1]["kind"] == "exact-url-provider-streaming-app-trace-quiescent"
    assert "do not stop, close, or resubmit" in result["recovery_events"][-1]["next_action"]
    assert commands == []


def test_recovery_returns_long_running_app_work_without_doctor_or_history(tmp_path: Path) -> None:
    module = load_bridge("exact_job_provider_interaction_recovery_test")
    commands: list[list[str]] = []

    def runner(command, env, timeout):
        commands.append(command)
        raise AssertionError(f"unexpected browser command: {command}")

    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    runtime.exact_url_only_poll = True
    run_dir = make_run(tmp_path, runtime, phase="RESPONSE_IN_PROGRESS")
    runtime.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": "runner-timeout"})
    runtime._recovery_tabs = lambda **kwargs: (
        [{"targetId": "T-1", "url": "https://chatgpt.com/c/exact-job"}],
        {"kind": "tabs"},
    )
    runtime._try_exact_url_terminal_now = lambda *args, **kwargs: runtime.store.load(run_dir)[1]
    runtime._exact_url_timeout_diagnostic = lambda *args, **kwargs: {
        "ok": True,
        "streaming": True,
        "app_policy": "required",
        "get_answer_now_available": True,
        "stop_control_present": True,
    }

    result = runtime.recover(run_dir)

    assert result["phase"] == "RECOVERY_REQUIRED"
    event = result["recovery_events"][-1]
    assert event["kind"] == "exact-url-provider-long-running-app-work"
    assert "do not resubmit or stop" in event["next_action"]
    assert commands == []


def test_recover_complete_cleanup_pending_retries_only_exact_owned_tab_cleanup(tmp_path: Path) -> None:
    module = load_bridge("exact_job_complete_cleanup_retry_test")
    commands: list[list[str]] = []
    url = "https://chatgpt.com/c/exact-job"
    tabs = [
        {"targetId": "T-1", "url": url, "type": "page"},
        {"targetId": "T-KEEP", "url": "https://chatgpt.com/c/foreign", "type": "page"},
    ]

    def runner(command, env, timeout):
        commands.append(command)
        if command[1] == "tabs":
            payload = list(tabs)
        elif command[1] == "tab-close":
            assert command[2] == "T-1"
            tabs[:] = [tab for tab in tabs if tab["targetId"] != "T-1"]
            payload = {"ok": True, "targetId": "T-1"}
        else:
            raise AssertionError(f"unexpected browser command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    run_dir = make_run(tmp_path, runtime)
    state_file = Path(run_dir) / "run.json"
    answer_path = Path(run_dir) / "answer.md"
    answer_path.write_text("terminal answer", encoding="utf-8")
    record = json.loads(state_file.read_text(encoding="utf-8"))
    record.update(
        {
            "phase": "COMPLETE",
            "cleanup_pending": True,
            "owned_tab_state": "cleanup-pending",
            "owned_open_tabs": 1,
            "result": {
                "path": str(answer_path),
                "sha256": hashlib.sha256(answer_path.read_bytes()).hexdigest(),
                "bytes": answer_path.stat().st_size,
                "provider_status": "complete",
                "evidence": {"exit_code": 0, "stdout": "captured"},
            },
        }
    )
    state_file.write_text(json.dumps(record), encoding="utf-8")

    result = runtime.recover(run_dir)

    assert result["phase"] == "COMPLETE"
    assert result["cleanup_pending"] is False
    assert result["owned_tab_state"] == "closed-and-absent"
    assert result["owned_open_tabs"] == 0
    assert tabs == [{"targetId": "T-KEEP", "url": "https://chatgpt.com/c/foreign", "type": "page"}]
    assert [command[1] for command in commands] == ["tabs", "tab-close", "tabs", "tabs"]


def test_settle_exact_terminal_captures_before_global_composer_lock(monkeypatch, tmp_path: Path) -> None:
    module = load_bridge("exact_job_settle_before_global_lock_test")
    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=lambda command, env, timeout: (_ for _ in ()).throw(AssertionError(command)),
        headed_runtime_preflight=False,
    )
    run_dir = make_run(tmp_path, runtime, phase="RESPONSE_IN_PROGRESS")
    runtime._try_exact_url_terminal_now = lambda path: {"phase": "COMPLETE", "conversation_url": "https://chatgpt.com/c/exact-job"}

    def forbidden_lock(*args, **kwargs):
        raise AssertionError("terminal capture must not wait for the global composer lock")

    monkeypatch.setattr(module, "exclusive_composer_lock", forbidden_lock)
    result = runtime.settle_exact_terminal(run_dir)

    assert result["phase"] == "COMPLETE"


def test_exact_terminal_capture_serializes_active_tab_sequence(monkeypatch, tmp_path: Path) -> None:
    module = load_bridge("exact_job_terminal_capture_lock_test")
    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=lambda command, env, timeout: (_ for _ in ()).throw(AssertionError(command)),
        headed_runtime_preflight=False,
    )
    run_dir = make_run(tmp_path, runtime, phase="RESPONSE_IN_PROGRESS")
    runtime._recovery_tabs = lambda **kwargs: (
        [{"targetId": "T-1", "url": "https://chatgpt.com/c/exact-job"}],
        {"kind": "tabs"},
    )
    lock_events: list[str] = []

    @contextmanager
    def tracked_lock(*args, **kwargs):
        lock_events.append("entered")
        yield
        lock_events.append("exited")

    def capture(path, **kwargs):
        assert lock_events == ["entered"]
        return {"phase": "COMPLETE", "conversation_url": "https://chatgpt.com/c/exact-job"}

    monkeypatch.setattr(module, "exclusive_composer_lock", tracked_lock)
    runtime._recover_exact_bound_url_terminal = capture

    result = runtime._try_exact_url_terminal_now(run_dir)

    assert result["phase"] == "COMPLETE"
    assert lock_events == ["entered", "exited"]


def test_bound_recovery_never_runs_navigating_doctor(tmp_path: Path) -> None:
    module = load_bridge("exact_job_recovery_no_doctor_test")
    commands: list[list[str]] = []

    def runner(command, env, timeout):
        commands.append(command)
        raise AssertionError(f"unexpected browser command: {command}")

    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    run_dir = make_run(tmp_path, runtime, phase="RESPONSE_IN_PROGRESS")
    runtime._recovery_tabs = lambda **kwargs: (
        [{"targetId": "T-1", "url": "https://chatgpt.com/c/exact-job"}],
        {"kind": "tabs"},
    )
    runtime._try_exact_url_terminal_now = lambda *args, **kwargs: runtime.store.load(run_dir)[1]

    result = runtime.recover(run_dir)

    assert result["phase"] == "RECOVERING"
    assert commands == []


def test_known_url_root_target_is_restored_in_place_without_new_tab_or_doctor(tmp_path: Path) -> None:
    module = load_bridge("exact_job_known_url_restore_test")
    commands: list[list[str]] = []
    restored = {"value": False}

    def runner(command, env, timeout):
        commands.append(command)
        if command[1:] == ["tabs", "--json"]:
            url = "https://chatgpt.com/c/exact-job" if restored["value"] else "https://chatgpt.com/"
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps([{"targetId": "T-1", "url": url}]),
                stderr="",
            )
        if command[1] == "tab-switch":
            payload = {"ok": True}
        elif command[1:] == ["active-tab", "--json"]:
            url = "https://chatgpt.com/c/exact-job" if restored["value"] else "https://chatgpt.com/"
            payload = {"targetId": "T-1", "url": url}
        elif command[1] == "navigate":
            assert command[2] == "https://chatgpt.com/c/exact-job"
            restored["value"] = True
            payload = {"ok": True, "targetId": "T-1", "url": command[2]}
        elif command[1:3] == ["web-ai", "status"]:
            payload = {
                "ok": True,
                "capabilities": [{
                    "capabilityId": "chatgpt-response-streaming",
                    "evidence": {"streaming": True},
                }],
            }
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    run_dir = make_run(tmp_path, runtime, phase="RESPONSE_IN_PROGRESS")
    runtime.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": "stale-root"})

    result = runtime.recover(run_dir)

    assert result["phase"] == "RECOVERING"
    assert restored["value"] is True
    assert sum(command[1] == "navigate" for command in commands) == 1
    assert not any(command[1] == "new-tab" for command in commands)
    assert not any(command[1:3] == ["web-ai", "sessions"] for command in commands)


def test_known_url_is_reopened_once_when_recorded_target_is_absent(tmp_path: Path) -> None:
    module = load_bridge("exact_job_known_url_reopen_test")
    commands: list[list[str]] = []
    opened = {"value": False}

    def runner(command, env, timeout):
        commands.append(command)
        if command[1:] == ["tabs", "--json"]:
            tabs = (
                [{"targetId": "T-2", "url": "https://chatgpt.com/c/exact-job"}]
                if opened["value"]
                else [{"targetId": "FOREIGN", "url": "https://chatgpt.com/c/foreign"}]
            )
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(tabs), stderr="")
        if command[1] == "new-tab":
            assert command[2] == "https://chatgpt.com/c/exact-job"
            opened["value"] = True
            payload = {"targetId": "T-2", "url": command[2]}
        elif command[1] == "tab-switch":
            payload = {"ok": True}
        elif command[1:] == ["active-tab", "--json"]:
            payload = {"targetId": "T-2", "url": "https://chatgpt.com/c/exact-job"}
        elif command[1:3] == ["web-ai", "status"]:
            payload = {
                "ok": True,
                "capabilities": [{
                    "capabilityId": "chatgpt-response-streaming",
                    "evidence": {"streaming": True},
                }],
            }
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    run_dir = make_run(tmp_path, runtime, phase="RESPONSE_IN_PROGRESS")
    runtime.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": "target-absent"})

    result = runtime.recover(run_dir)

    assert result["phase"] == "URL_BOUND"
    assert result["current_target_id"] == "T-2"
    opens = [command for command in commands if command[1] == "new-tab"]
    assert opens == [["agbrowse", "new-tab", "https://chatgpt.com/c/exact-job", "--json"]]
    assert not any(command[1] == "navigate" for command in commands)
    assert not any(command[1:3] == ["web-ai", "sessions"] for command in commands)


def test_history_utility_recovers_after_host_reboot_and_reuses_startup_blank(tmp_path: Path) -> None:
    module = load_bridge("exact_job_history_runtime_restart_test")
    commands: list[list[str]] = []
    restarted = {"value": False}

    def runner(command, env, timeout):
        commands.append(command)
        if command[1:] == ["tabs", "--json"]:
            payload = (
                [{"targetId": "T-UTILITY", "url": "about:blank", "lastActiveAt": None}]
                if restarted["value"]
                else []
            )
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        if command[1] == "new-tab":
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="browserType.connectOverCDP: connect ECONNREFUSED 127.0.0.1:9222",
            )
        if command[1] == "start":
            restarted["value"] = True
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True}), stderr="")
        if command[1] in {"tab-switch", "navigate"}:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"ok": True}), stderr="")
        raise AssertionError(f"unexpected command: {command}")

    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    utility = runtime._open_recovery_utility_target(
        run_dir=run_dir,
        executable="agbrowse",
        manifest={},
        known_preexisting_target_ids=set(),
    )

    assert utility["target_id"] == "T-UTILITY"
    assert utility["created_targets"] == ["T-UTILITY"]
    assert sum(command[1] == "new-tab" for command in commands) == 1
    assert sum(command[1] == "start" for command in commands) == 1
    assert commands[-2][1:] == ["tab-switch", "T-UTILITY", "--json"]
    assert commands[-1][1:3] == ["navigate", "https://chatgpt.com/"]


def test_history_utility_does_not_restart_for_non_connection_failure(tmp_path: Path) -> None:
    module = load_bridge("exact_job_history_nonconnection_failure_test")
    commands: list[list[str]] = []

    def runner(command, env, timeout):
        commands.append(command)
        if command[1:] == ["tabs", "--json"]:
            return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")
        if command[1] == "new-tab":
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="permission denied")
        raise AssertionError(f"unexpected command: {command}")

    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    try:
        runtime._open_recovery_utility_target(
            run_dir=run_dir,
            executable="agbrowse",
            manifest={},
            known_preexisting_target_ids=set(),
        )
    except module.BridgeError as exc:
        assert exc.code == "RECOVERY_UTILITY_TARGET_FAILED"
        assert exc.evidence.get("evidence")
    else:
        raise AssertionError("expected recovery utility failure")

    assert not any(command[1] == "start" for command in commands)


def test_recorded_target_canonical_url_binds_before_doctor_or_history(tmp_path: Path) -> None:
    module = load_bridge("exact_job_visible_target_bind_test")
    commands: list[list[str]] = []

    def runner(command, env, timeout):
        commands.append(command)
        if command[1:] == ["tabs", "--json"]:
            payload = [{"targetId": "T-1", "url": "https://chatgpt.com/c/exact-job"}]
        elif command[1] == "tab-switch":
            payload = {"ok": True}
        elif command[1:] == ["active-tab", "--json"]:
            payload = {"targetId": "T-1", "url": "https://chatgpt.com/c/exact-job"}
        elif command[1:3] == ["web-ai", "status"]:
            payload = {
                "ok": True,
                "capabilities": [{
                    "capabilityId": "chatgpt-response-streaming",
                    "evidence": {"streaming": True},
                }],
            }
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    run_dir = make_run(tmp_path, runtime, phase="SUBMITTED")
    runtime.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": "url-envelope-late"})

    result = runtime.recover(run_dir)

    assert result["phase"] == "URL_BOUND"
    assert result["conversation_url"] == "https://chatgpt.com/c/exact-job"
    assert any(
        event.get("kind") == "recorded-target-canonical-before-doctor"
        for event in result["recovery_events"]
    )
    assert not any(command[1:3] == ["web-ai", "sessions"] for command in commands)
    assert not any(command[1] in {"new-tab", "navigate", "click"} for command in commands)


def test_send_binds_new_exact_target_before_poll(tmp_path: Path) -> None:
    module = load_bridge("exact_job_send_binding_test")
    lifecycle = FakeLifecycle()

    def runner(command, env, timeout):
        assert command[1:3] == ["web-ai", "send"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "status": "sent",
                    "sessionId": "S-NEW",
                    "targetId": "T-NEW",
                    "conversationUrl": "https://chatgpt.com/",
                }
            ),
            stderr="",
        )

    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("bind exact target", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "question": HANDOFF,
                "prompt_transport": "file",
                "prompt_file": str(prompt),
                "prompt_file_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
                "files": [str(prompt)],
                "mode_label": "Pro",
                "app_policy": "forbidden",
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    record = runtime.store.create_run(
        project_root=str(project),
        manifest_path=str(manifest),
        agbrowse_contract={"executable": "agbrowse"},
    )
    run_dir = str(record["run_dir"])
    runtime.store.transition(run_dir, "PREFLIGHTED")
    runtime._tab_lifecycle = lambda executable, values: lifecycle
    runtime.capture_pre_send_tabs = True
    runtime.observe_post_send_targets = True
    runtime.exact_url_only_poll = True
    observations = iter(
        [
            ([{"targetId": "FOREIGN", "url": "https://chatgpt.com/c/foreign"}], {"kind": "before"}),
            (
                [
                    {"targetId": "FOREIGN", "url": "https://chatgpt.com/c/foreign"},
                    {"targetId": "T-NEW", "url": "https://chatgpt.com/c/new-job"},
                ],
                {"kind": "after"},
            ),
        ]
    )
    runtime._recovery_tabs = lambda **kwargs: next(observations)
    runtime._show_session_identity = lambda **kwargs: (
        "T-NEW",
        None,
        {"kind": "session"},
        {
            "status": "sent",
            "conversationUrl": "https://chatgpt.com/",
            "answer": None,
            "envelopeSummary": {"assistantCount": 0},
            "trace": [],
        },
    )

    result = runtime.send(run_dir)

    assert result["phase"] == "URL_BOUND"
    assert result["conversation_url"] == "https://chatgpt.com/c/new-job"
    assert result["current_target_id"] == "T-NEW"
    assert lifecycle.owned[-1]["target_id"] == "T-NEW"
    assert lifecycle.protected[-1]["target_id"] == "T-NEW"


def test_false_sent_root_is_closed_and_never_polled(tmp_path: Path) -> None:
    module = load_bridge("exact_job_false_sent_cleanup_test")
    lifecycle = FakeLifecycle()

    def runner(command, env, timeout):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "status": "sent",
                    "sessionId": "S-ROOT",
                    "targetId": "T-ROOT",
                    "conversationUrl": "https://chatgpt.com/",
                }
            ),
            stderr="",
        )

    runtime = module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("reject false sent", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "question": HANDOFF,
                "prompt_transport": "file",
                "prompt_file": str(prompt),
                "prompt_file_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
                "files": [str(prompt)],
                "mode_label": "Pro",
                "app_policy": "forbidden",
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    record = runtime.store.create_run(
        project_root=str(project),
        manifest_path=str(manifest),
        agbrowse_contract={"executable": "agbrowse"},
    )
    run_dir = str(record["run_dir"])
    runtime.store.transition(run_dir, "PREFLIGHTED")
    runtime._tab_lifecycle = lambda executable, values: lifecycle
    runtime.capture_pre_send_tabs = True
    runtime.observe_post_send_targets = True
    runtime.exact_url_only_poll = True
    runtime._recovery_tabs = lambda **kwargs: (
        ([{"targetId": "FOREIGN", "url": "https://chatgpt.com/c/foreign"}], {"kind": "before"})
        if kwargs["name"] == "pre-send-tabs"
        else ([{"targetId": "T-ROOT", "url": "https://chatgpt.com/"}], {"kind": kwargs["name"]})
    )
    runtime._show_session_identity = lambda **kwargs: (
        "T-ROOT",
        None,
        {"kind": "session"},
        {
            "status": "sent",
            "conversationUrl": "https://chatgpt.com/",
            "answer": None,
            "envelopeSummary": {"assistantCount": 0},
            "trace": [
                {
                    "intentId": "send.click",
                    "status": "unresolved",
                    "errorCode": "TARGET_UNRESOLVED",
                    "attempts": [{"validation": {"reason": "not-enabled"}}],
                }
            ],
        },
    )
    cleanups: list[dict] = []
    runtime._safe_tab_cleanup = lambda lifecycle_arg, run_dir_arg, **kwargs: (
        cleanups.append(dict(kwargs)) or {"ok": True, "state": "closed-and-absent"}
    )

    result = runtime.send(run_dir)

    assert result["phase"] == "CANCELLED_PRE_SUBMISSION"
    assert result["conversation_url"] is None
    assert cleanups == [
        {
            "target_id": "T-ROOT",
            "url": "https://chatgpt.com/",
            "reason": "verified-send-click-not-committed",
        }
    ]


def test_dead_exact_session_command_lock_is_reclaimed_without_waiting(tmp_path: Path, monkeypatch) -> None:
    module = load_bridge("dead_exact_session_command_lock_test")
    home = tmp_path / "home"
    lock_dir = home / ".browser-agent"
    lock_dir.mkdir(parents=True)
    session_id = "SESSION-DEAD"
    lock = lock_dir / f"web-ai-sessions.json.cmd.{session_id}.lock"
    lock.write_text(
        json.dumps({
            "pid": 424242,
            "sessionId": session_id,
            "acquiredAt": "2026-07-21T00:00:00Z",
            "heartbeatAt": "2026-07-21T00:00:01Z",
            "expiresAt": "2099-01-01T00:00:00Z",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(module.STATE, "process_identity", lambda pid: {"pid": pid, "creation_time": None, "alive": False})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    runtime = module.Bridge(state_root=tmp_path / "state", headed_runtime_preflight=False)

    result = runtime._reclaim_dead_session_command_lock(run_dir=run_dir, session_id=session_id)

    assert result["state"] == "reclaimed-and-absent"
    assert not lock.exists()
    assert (run_dir / "agbrowse-evidence" / "dead-session-command-lock.json").is_file()


def test_live_exact_session_command_lock_is_never_reclaimed(tmp_path: Path, monkeypatch) -> None:
    module = load_bridge("live_exact_session_command_lock_test")
    home = tmp_path / "home"
    lock_dir = home / ".browser-agent"
    lock_dir.mkdir(parents=True)
    session_id = "SESSION-LIVE"
    lock = lock_dir / f"web-ai-sessions.json.cmd.{session_id}.lock"
    lock.write_text(
        json.dumps({"pid": 12, "sessionId": session_id, "acquiredAt": "2026-07-21T00:00:00Z"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(module.STATE, "process_identity", lambda pid: {"pid": pid, "creation_time": 1784592000.0, "alive": True})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    runtime = module.Bridge(state_root=tmp_path / "state", headed_runtime_preflight=False)

    result = runtime._reclaim_dead_session_command_lock(run_dir=run_dir, session_id=session_id)

    assert result["state"] == "owner-alive"
    assert lock.is_file()
