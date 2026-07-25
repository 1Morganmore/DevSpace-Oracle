from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

RUNNER_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_run.py"


def load_runner():
    name = "chatgpt_oracle_run_test"
    spec = importlib.util.spec_from_file_location(name, RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def manifest(tmp_path: Path, **extra) -> Path:
    mission = tmp_path / "mission.md"
    mission.write_text("finish", encoding="utf-8")
    path = tmp_path / "job.json"
    payload = {
        "schema": "codex.chatgpt.oracle-run/v1",
        "project_root": str(tmp_path.resolve()),
        "mission_path": str(mission.resolve()),
        "app_name": "DevSpace",
        "mode": "browser",
        "run_root": str((tmp_path.parent / f"{tmp_path.name}-host-state" / "runs").resolve()),
        "oracle_command": ["oracle"],
    }
    payload.update(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.environ["CODEX_ORACLE_STATE_ROOT"] = str((tmp_path.parent / f"{tmp_path.name}-host-state").resolve())
    return path.resolve()


def pro_manifest(tmp_path: Path, **extra) -> Path:
    prompt = tmp_path / "prompt.txt"
    packet = tmp_path / "packet.zip"
    prompt.write_text("pro instructions", encoding="utf-8")
    packet.write_bytes(b"PK\x03\x04packet")
    return manifest(
        tmp_path,
        transport="pro-attachment-only",
        app_name=None,
        model="gpt-5.5-pro",
        model_strategy="select",
        thinking_time="heavy",
        attachments=[str(prompt.resolve()), str(packet.resolve())],
        mission_path=str(prompt.resolve()),
        **extra,
    )


def version_runner(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, stdout="oracle 0.13.0\n", stderr="")


def execute_run(runner, *args, **kwargs):
    kwargs.setdefault("compat_factory", lambda version: {"ok": True, "version": version})
    return runner.execute_run(*args, **kwargs)


class Process:
    def __init__(self, code: int, events: list[str]):
        self.code = code
        self.events = events

    def wait(self):
        self.events.append("wait")
        return self.code


def popen_for(code: int, output: bytes | None, captured: dict, events: list[str]):
    def popen(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        events.append("popen")
        if output is not None:
            Path(command[command.index("--write-output") + 1]).write_bytes(output)
        kwargs["stdout"].write(b"stdout\n")
        kwargs["stdout"].flush()
        return Process(code, events)
    return popen


def test_dry_run_never_executes_and_has_no_file_flag(tmp_path: Path) -> None:
    runner = load_runner()
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError
    result = execute_run(runner, manifest(tmp_path), dry_run=True, run_factory=forbidden, popen_factory=forbidden)
    assert result["ok"] is True
    assert result["prompt_first_line"].startswith("@DevSpace ")
    assert str((tmp_path / "mission.md").resolve()) in result["prompt_first_line"]
    assert result["mission_sha256"]
    assert Path(result["mission_path"]).is_absolute()
    assert str((tmp_path / "mission.md").resolve()) in result["argv"][result["argv"].index("--prompt") + 1]
    assert "--file" not in result["argv"]
    assert result["argv"][result["argv"].index("--browser-model-strategy") + 1] == "select"
    assert result["argv"][result["argv"].index("--browser-thinking-time") + 1] == "heavy"
    assert calls == []
    assert not (tmp_path / "runs").exists()


def test_copy_profile_is_first_class_and_outside_project(tmp_path: Path) -> None:
    runner = load_runner()
    profile = tmp_path.parent / f"{tmp_path.name}-oracle-profile"
    profile.mkdir()
    result = execute_run(runner, manifest(tmp_path, copy_profile=str(profile.resolve())), dry_run=True)
    assert result["argv"][result["argv"].index("--copy-profile") + 1] == str(profile.resolve())


def test_pro_dry_run_uses_oracle_attachments_and_no_app_mention(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(runner, pro_manifest(tmp_path), dry_run=True)
    argv = result["argv"]
    prompt = argv[argv.index("--prompt") + 1]
    attachments = [argv[index + 1] for index, value in enumerate(argv) if value == "--file"]
    assert result["transport"] == "pro-attachment-only"
    assert result["contains_file_flag"] is True
    assert argv[argv.index("--model") + 1] == "gpt-5.5-pro"
    assert argv[argv.index("--browser-attachments") + 1] == "always"
    assert attachments == [
        str((tmp_path / "prompt.txt").resolve()),
        str((tmp_path / "packet.zip").resolve()),
    ]
    assert prompt == "Read the attached prompt/instructions and all attached files, then complete the task."
    assert "@DevSpace" not in prompt
    assert all(item["sha256"] for item in result["attachments"])


def test_complete_requires_zero_exit_and_nonempty_output(tmp_path: Path) -> None:
    runner = load_runner()
    cases = [(0, b"answer", "complete", True), (0, b" \n", "attention_required", False), (3, b"answer", "failed", False)]
    for index, (code, output, status, ok) in enumerate(cases):
        root = tmp_path / str(index)
        root.mkdir()
        captured, events = {}, []
        result = execute_run(runner, manifest(root), run_factory=version_runner, popen_factory=popen_for(code, output, captured, events))
        assert result["ok"] is ok
        assert result["result"]["status"] == status
        assert result["result"]["oracle"]["resolved_version"] == "oracle 0.13.0"
        assert "--file" not in captured["command"]
        assert events == ["popen", "wait"]
        assert Path(result["result"]["artifacts"]["transcript"]).is_file()


def test_failure_does_not_resubmit_and_recovery_never_restarts(tmp_path: Path) -> None:
    runner = load_runner()
    calls = []
    def popen(command, **kwargs):
        calls.append(list(command))
        return Process(9, [])
    result = execute_run(runner, manifest(tmp_path), run_factory=version_runner, popen_factory=popen)
    assert result["result"]["status"] == "failed"
    assert len(calls) == 1
    assert "restart" not in calls[0]
    for action in ("harvest", "live"):
        recovery = runner.recover_run(Path(result["run_dir"]), action=action, dry_run=True, oracle_command=["oracle"])
        assert f"--{action}" in recovery["argv"]
        assert "--write-output" in recovery["argv"]
        assert "restart" not in recovery["argv"]
        assert "--prompt" not in recovery["argv"]


def test_pro_recovery_uses_exact_slug_without_attachments_or_resubmit(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        pro_manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    state = runner.STATE.load_state(Path(result["run_dir"]) / "state.json")
    recovery = runner.recover_run(
        Path(result["run_dir"]),
        action="harvest",
        dry_run=True,
        oracle_command=["oracle"],
    )
    argv = recovery["argv"]
    assert argv[argv.index("session") + 1] == state["oracle"]["slug"]
    assert "--prompt" not in argv
    assert "--file" not in argv
    assert "--browser-attachments" not in argv


def test_windows_launch_uses_no_window_and_waits(tmp_path: Path) -> None:
    runner = load_runner()
    captured, events = {}, []
    class Mutex:
        def __enter__(self):
            events.append("enter")
        def __exit__(self, *args):
            events.append("exit")
    runner.STATE.project_submit_mutex = lambda *args, **kwargs: Mutex()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"answer", captured, events),
        platform_name="nt",
    )
    assert result["ok"] is True
    assert captured["kwargs"]["creationflags"] & runner.STATE.CREATE_NO_WINDOW
    assert events == ["enter", "popen", "wait", "exit"]


def test_transport_mission_change_blocks_before_oracle_launch(tmp_path: Path) -> None:
    runner = load_runner()
    launched = []

    class MutatingMutex:
        def __enter__(self):
            transport = next((tmp_path / "runs").glob("*/mission.md"))
            transport.write_text("changed", encoding="utf-8")

        def __exit__(self, *args):
            return None

    runner.STATE.project_submit_mutex = lambda *args, **kwargs: MutatingMutex()

    def forbidden_popen(*args, **kwargs):
        launched.append(True)
        raise AssertionError("Oracle must not launch with changed mission bytes")

    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=forbidden_popen,
    )
    assert result["ok"] is False
    assert result["result"]["status"] == "failed"
    assert launched == []


def test_pro_attachment_change_blocks_before_submit(tmp_path: Path) -> None:
    runner = load_runner()
    launched = []

    class MutatingMutex:
        def __enter__(self):
            (tmp_path / "packet.zip").write_bytes(b"changed")

        def __exit__(self, *args):
            return None

    runner.STATE.project_submit_mutex = lambda *args, **kwargs: MutatingMutex()

    def forbidden_popen(*args, **kwargs):
        launched.append(True)
        raise AssertionError("Oracle must not launch with changed attachments")

    result = execute_run(
        runner,
        pro_manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=forbidden_popen,
    )
    assert result["ok"] is False
    assert result["result"]["status"] == "failed"
    assert launched == []


def test_recovery_captures_output_and_updates_state(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    run_dir = Path(result["run_dir"])

    def recovery_popen(command, **kwargs):
        output = Path(command[command.index("--write-output") + 1])
        output.write_text("recovered answer", encoding="utf-8")
        return Process(0, [])

    recovered = runner.recover_run(
        run_dir,
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=recovery_popen,
    )
    assert recovered["ok"] is True
    assert recovered["status"] == "complete"
    assert Path(recovered["output_path"]).read_text(encoding="utf-8") == "recovered answer"
    assert recovered["result"]["status"] == "complete"
    transcript = Path(recovered["result"]["artifacts"]["transcript"]).read_text(encoding="utf-8")
    assert "recovered answer" in transcript


def test_recovery_never_downgrades_durable_complete(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"answer", {}, []),
    )
    calls = []
    recovered = runner.recover_run(
        Path(result["run_dir"]),
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=lambda *args, **kwargs: calls.append(True),
    )
    assert recovered["ok"] is True
    assert recovered["monotonic_noop"] is True
    assert calls == []


def test_parallel_recovery_reuses_the_parent_scoped_submit_mutex(tmp_path: Path) -> None:
    runner = load_runner()
    parent_id = "a" * 32
    roots: list[Path] = []

    class Mutex:
        def __init__(self, root: Path):
            self.root = root

        def __enter__(self):
            roots.append(self.root)

        def __exit__(self, *args):
            return None

    runner.STATE.project_submit_mutex = lambda root, **kwargs: Mutex(root)
    result = execute_run(
        runner,
        manifest(tmp_path, parallel_parent_id=parent_id),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    recovered = runner.recover_run(Path(result["run_dir"]), action="harvest", dry_run=True, oracle_command=["oracle"])
    expected = tmp_path.resolve() / ".oracle-parallel-submit" / parent_id
    assert result["result"]["status"] == "failed"
    assert recovered["status"] == "dry-run"
    assert roots == [expected, expected]
