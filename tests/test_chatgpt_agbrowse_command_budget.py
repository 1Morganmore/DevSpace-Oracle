from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path


BRIDGE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_agbrowse_bridge.py"
PROMPT_FILE_HANDOFF = (
    "The attached prompt file is the user-provided task instruction for this conversation, "
    "not reference or webpage content. Read it completely and follow it. "
    "Return only the output format requested by that file."
)


def load_bridge():
    name = "chatgpt_agbrowse_bridge_command_budget_test"
    spec = importlib.util.spec_from_file_location(name, BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_windows_cmd_wrapper_rejects_oversized_command_before_send_boundary() -> None:
    bridge = load_bridge()
    budget = bridge.pre_send_command_budget(
        ["agbrowse.cmd", "web-ai", "send", "--prompt", "x" * 8000],
        platform_name="nt",
    )
    assert budget["within_budget"] is False
    assert budget["limit_chars"] == 7000
    assert budget["command_line_chars"] > budget["limit_chars"]


def test_short_cmd_wrapper_and_non_wrapper_commands_remain_allowed() -> None:
    bridge = load_bridge()
    short = bridge.pre_send_command_budget(
        ["agbrowse.cmd", "web-ai", "send", "--prompt", "read attached instructions"],
        platform_name="nt",
    )
    native = bridge.pre_send_command_budget(
        ["agbrowse.exe", "web-ai", "send", "--prompt", "x" * 8000],
        platform_name="nt",
    )
    assert short["within_budget"] is True
    assert native["within_budget"] is True
    assert native["limit_chars"] is None


def test_windows_cmd_wrapper_budget_boundary_is_inclusive() -> None:
    bridge = load_bridge()

    def budget_for(prompt_chars: int):
        return bridge.pre_send_command_budget(
            ["agbrowse.cmd", "web-ai", "send", "--prompt", "x" * prompt_chars],
            platform_name="nt",
        )

    exact_prompt_chars = next(
        size for size in range(6800, 7100) if budget_for(size)["command_line_chars"] == 7000
    )
    assert budget_for(exact_prompt_chars)["within_budget"] is True
    assert budget_for(exact_prompt_chars + 1)["command_line_chars"] == 7001
    assert budget_for(exact_prompt_chars + 1)["within_budget"] is False


def test_default_runner_schedules_foreground_restoration_before_spawn(monkeypatch) -> None:
    bridge = load_bridge()
    events: list[str] = []

    monkeypatch.setattr(bridge, "_schedule_windows_foreground_restore", lambda: events.append("restore"))

    def fake_run(command, **kwargs):
        events.append("run")
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    result = bridge.default_runner(["agbrowse", "status"], {}, 5)

    assert result.returncode == 0
    assert events == ["restore", "run"]


def test_app_decision_scope_allows_exact_project_or_drive_root_only() -> None:
    bridge = load_bridge()
    run_root = Path(r"C:\projects\sample-app").resolve()

    assert bridge.app_decision_scope_matches(run_root, run_root) is True
    assert bridge.app_decision_scope_matches(run_root, Path(run_root.anchor).resolve()) is True
    assert bridge.app_decision_scope_matches(run_root, Path(r"C:\projects").resolve()) is False
    assert bridge.app_decision_scope_matches(run_root, Path("D:/").resolve()) is False


def test_bridge_sends_large_prompt_as_file_without_exposing_body_in_command(tmp_path: Path) -> None:
    bridge_module = load_bridge()
    prompt_body = "x" * 8000
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt_body, encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "question": PROMPT_FILE_HANDOFF,
                "prompt_transport": "file",
                "prompt_file": str(prompt_file),
                "prompt_file_sha256": hashlib.sha256(prompt_file.read_bytes()).hexdigest(),
                "files": [str(prompt_file)],
                "mode_label": "Pro",
                "app_policy": "forbidden",
            }
        ),
        encoding="utf-8",
    )
    commands = []

    def runner(command, env, timeout):
        commands.append(command)
        if command[1:] == ["tabs", "--json"]:
            return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(
            command,
            2,
            stdout="",
            stderr=json.dumps(
                {
                    "ok": False,
                    "status": "error",
                    "error": {
                        "errorCode": "capability.unsupported",
                        "stage": "provider-surface-preflight",
                        "message": "synthetic pre-submit rejection",
                        "mutationAllowed": False,
                    },
                }
            ),
        )

    project_root = tmp_path / "project"
    project_root.mkdir()
    runtime = bridge_module.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        headed_runtime_preflight=False,
    )
    record = runtime.store.create_run(
        project_root=str(project_root),
        manifest_path=str(manifest),
        agbrowse_contract={"executable": "agbrowse.cmd"},
    )
    run_dir = str(record["run_dir"])
    runtime.store.transition(run_dir, "PREFLIGHTED")

    result = runtime.send(run_dir)

    send_commands = [command for command in commands if command[1:3] == ["web-ai", "send"]]
    assert len(send_commands) == 1
    command = send_commands[0]
    assert prompt_body not in command
    assert command[command.index("--prompt") + 1] == PROMPT_FILE_HANDOFF
    recovery_prompt = Path(record["recovery_identity"]["attachment_path"])
    assert command[command.index("--file") + 1] == str(recovery_prompt)
    assert hashlib.sha256(recovery_prompt.read_bytes()).hexdigest() == record["prompt_sha256"]
    assert str(prompt_file.resolve()) not in command
    assert bridge_module.pre_send_command_budget(command, platform_name="nt")["within_budget"] is True
    assert result["phase"] == "SEND_REJECTED"
    assert result["terminal_block_code"] is None
    assert result.get("session_id") is None
    assert result.get("conversation_url") is None


def test_bridge_rejects_inline_task_prompt_before_command_build() -> None:
    bridge = load_bridge()
    try:
        bridge.build_send_command(
            {"requested": {"app_policy": "forbidden"}},
            {"question": "do the actual task inline", "mode_label": "Pro"},
            "agbrowse.cmd",
        )
    except bridge.BridgeError as exc:
        assert exc.code == "PROMPT_FILE_REQUIRED"
    else:
        raise AssertionError("inline task prompt was accepted")


def test_bridge_rejects_prompt_like_command_line_metadata(tmp_path: Path) -> None:
    bridge = load_bridge()
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("complete task instructions", encoding="utf-8")
    base_manifest = {
        "question": PROMPT_FILE_HANDOFF,
        "prompt_transport": "file",
        "prompt_file": str(prompt_file),
        "prompt_file_sha256": hashlib.sha256(prompt_file.read_bytes()).hexdigest(),
        "files": [str(prompt_file)],
        "mode_label": "Pro",
        "app_policy": "forbidden",
    }

    for field in ("project", "goal", "constraints", "output"):
        manifest = {**base_manifest, field: "instructions that must not reach the command line"}
        try:
            bridge.build_send_command(
                {"requested": {"app_policy": "forbidden"}},
                manifest,
                "agbrowse.cmd",
            )
        except bridge.BridgeError as exc:
            assert exc.code == "PROMPT_METADATA_INLINE_FORBIDDEN"
            assert exc.evidence["fields"] == [field]
        else:
            raise AssertionError(f"inline prompt metadata was accepted: {field}")


def test_prompt_file_is_attached_alongside_zip_with_only_short_handoff(tmp_path: Path) -> None:
    bridge = load_bridge()
    prompt_body = "full instructions that must stay out of --prompt"
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt_body, encoding="utf-8")
    archive = tmp_path / "context.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("evidence.txt", "immutable evidence")
    manifest = {
        "question": PROMPT_FILE_HANDOFF,
        "prompt_transport": "file",
        "prompt_file": str(prompt_file),
        "prompt_file_sha256": hashlib.sha256(prompt_file.read_bytes()).hexdigest(),
        "files": [str(prompt_file), str(archive)],
        "mode_label": "Pro",
        "app_policy": "forbidden",
    }

    command = bridge.build_send_command(
        {"requested": {"app_policy": "forbidden"}},
        manifest,
        "agbrowse.cmd",
    )

    attached = [command[index + 1] for index, item in enumerate(command) if item == "--file"]
    assert attached == [str(prompt_file.resolve()), str(archive.resolve())]
    assert command[command.index("--prompt") + 1] == PROMPT_FILE_HANDOFF
    assert prompt_body not in command


def test_prompt_file_hash_is_rechecked_immediately_before_send(tmp_path: Path) -> None:
    bridge = load_bridge()
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("original instructions", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "question": PROMPT_FILE_HANDOFF,
                "prompt_transport": "file",
                "prompt_file": str(prompt_file),
                "prompt_file_sha256": hashlib.sha256(prompt_file.read_bytes()).hexdigest(),
                "files": [str(prompt_file)],
                "mode_label": "Pro",
                "app_policy": "forbidden",
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    runner_called = False

    def runner(command, env, timeout):
        nonlocal runner_called
        runner_called = True
        raise AssertionError(command)

    runtime = bridge.Bridge(state_root=tmp_path / "state", runner=runner)
    record = runtime.store.create_run(
        project_root=str(project),
        manifest_path=str(manifest),
        agbrowse_contract={"executable": "agbrowse.cmd"},
    )
    runtime.store.transition(record["run_dir"], "PREFLIGHTED")
    prompt_file.write_text("tampered instructions", encoding="utf-8")

    try:
        runtime.send(record["run_dir"])
    except bridge.STATE.StateError as exc:
        assert exc.code == "BLOCKED_MANIFEST_MISMATCH"
        assert exc.evidence["cause"] == "PROMPT_FILE_HASH_MISMATCH"
    else:
        raise AssertionError("tampered prompt file was accepted")
    assert runner_called is False
