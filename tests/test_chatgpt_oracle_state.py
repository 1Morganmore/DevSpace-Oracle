from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

STATE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_state.py"


def load_state():
    name = "chatgpt_oracle_state_test"
    spec = importlib.util.spec_from_file_location(name, STATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def manifest(tmp_path: Path, mission_path: Path | str, **extra) -> Path:
    os.environ["CODEX_ORACLE_STATE_ROOT"] = str((tmp_path.parent / f"{tmp_path.name}-host-state").resolve())
    value = {
        "schema": "codex.chatgpt.oracle-run/v1",
        "project_root": str(tmp_path.resolve()),
        "mission_path": str(mission_path),
        "app_name": "DevSpace",
        "mode": "browser",
        "oracle_command": ["oracle"],
    }
    value.update(extra)
    path = tmp_path / "job.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path.resolve()


def test_invalid_utf8_and_relative_mission_are_rejected(tmp_path: Path) -> None:
    state = load_state()
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff")
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, bad.resolve()))
    assert exc.value.code == "UTF8_REQUIRED"
    good = tmp_path / "good.md"
    good.write_text("work", encoding="utf-8")
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, "good.md"))
    assert exc.value.code == "MISSION_PATH_ABSOLUTE_REQUIRED"


def test_prompt_is_plain_app_plus_absolute_mission_instruction(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    config = state.load_manifest(manifest(tmp_path, mission.resolve()))
    prompt = state.composer_prompt(config)
    assert prompt == f"@DevSpace {mission.resolve()} 파일을 읽고 끝까지 수행하세요."
    assert "\n" not in prompt


def test_pro_manifest_is_attachment_only_and_hashes_exact_files(tmp_path: Path) -> None:
    state = load_state()
    prompt = tmp_path / "prompt.txt"
    packet = tmp_path / "packet.zip"
    prompt.write_text("instructions", encoding="utf-8")
    packet.write_bytes(b"PK\x03\x04packet")
    config = state.load_manifest(
        manifest(
            tmp_path,
            prompt.resolve(),
            transport="pro-attachment-only",
            app_name=None,
            model="gpt-5.5-pro",
            thinking_time="heavy",
            attachments=[str(prompt.resolve()), str(packet.resolve())],
        )
    )
    assert config.app_name is None
    assert config.transport == "pro-attachment-only"
    assert config.attachments == (prompt.resolve(), packet.resolve())
    assert config.attachment_sha256s == (
        state.sha256_file(prompt.resolve()),
        state.sha256_file(packet.resolve()),
    )
    assert state.composer_prompt(config) == (
        "Read the attached prompt/instructions and all attached files, then complete the task."
    )
    assert "@DevSpace" not in state.composer_prompt(config)
    layout = state.create_layout(config, run_id="20260725T151414Z-a3aeba967d99")
    payload = state.state_payload(config, layout, status="prepared", resolved_version="oracle 0.16.1")
    assert payload["transport"] == "pro-attachment-only"
    assert payload["attachments"][1]["sha256"] == state.sha256_file(packet.resolve())


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"attachments": []}, "PRO_ATTACHMENTS_REQUIRED"),
        ({"attachments": None}, "PRO_ATTACHMENTS_REQUIRED"),
        ({"attachments": ["missing.txt"]}, "ATTACHMENT_0_ABSOLUTE_REQUIRED"),
        ({"model": "gpt-5.6"}, "PRO_MODEL_INVALID"),
        ({"model_strategy": "current"}, "PRO_MODEL_STRATEGY_INVALID"),
        ({"thinking_time": "extended"}, "PRO_THINKING_TIME_INVALID"),
        ({"research": "deep"}, "PRO_RESEARCH_FORBIDDEN"),
        ({"app_name": "DevSpace"}, "PRO_APP_FORBIDDEN"),
    ],
)
def test_pro_manifest_fails_closed_without_exact_contract(tmp_path: Path, extra: dict, code: str) -> None:
    state = load_state()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("instructions", encoding="utf-8")
    value = {
        "transport": "pro-attachment-only",
        "app_name": None,
        "model": "gpt-5.5-pro",
        "thinking_time": "heavy",
        "attachments": [str(prompt.resolve())],
    }
    value.update(extra)
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, prompt.resolve(), **value))
    assert exc.value.code == code


def test_regular_manifest_requires_exact_devspace_app(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), app_name="OtherWorkspace"))

    assert exc.value.code == "DEVSPACE_APP_REQUIRED"


def test_layout_uses_oracle_exact_ten_character_session_suffix(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    config = state.load_manifest(manifest(tmp_path, mission.resolve()))
    layout = state.create_layout(config, run_id="20260725T151414Z-a3aeba967d99")
    assert layout.slug == "oracle-test-layout-uses-a3aeba967d"


def test_nonempty_output_mutex_and_windows_flags(tmp_path: Path) -> None:
    state = load_state()
    output = tmp_path / "output.md"
    assert state.output_is_nonempty(output) is False
    output.write_text(" \n", encoding="utf-8")
    assert state.output_is_nonempty(output) is False
    output.write_text("answer", encoding="utf-8")
    assert state.output_is_nonempty(output) is True
    assert state.mutex_wait_succeeded(state.WAIT_ABANDONED) is True
    assert state.mutex_wait_succeeded(state.WAIT_TIMEOUT) is False
    assert state.windows_subprocess_kwargs(platform_name="nt")["creationflags"] & state.CREATE_NO_WINDOW


def test_unsafe_oracle_args_are_rejected(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    for unsafe in (
        ["--file", "x"],
        ["restart"],
        ["--browser-tab", "current"],
        ["--force"],
        ["--chatgpt-url=https://chatgpt.com/c/foreign"],
    ):
        with pytest.raises(state.OracleStateError) as exc:
            state.load_manifest(manifest(tmp_path, mission.resolve(), oracle_args=unsafe))
        assert exc.value.code == "ORACLE_ARG_FORBIDDEN"
    config = state.load_manifest(
        manifest(tmp_path, mission.resolve(), oracle_args=["--timeout", "45m", "--no-notify", "--heartbeat=20"])
    )
    assert config.oracle_args == ("--timeout", "45m", "--no-notify", "--heartbeat=20")
    assert config.model_strategy == "select"
    assert config.thinking_time == "heavy"
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), thinking_time="xhigh"))
    assert exc.value.code == "THINKING_TIME_INVALID"
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), oracle_command=["powershell", "-Command", "echo unsafe"]))
    assert exc.value.code == "ORACLE_COMMAND_FORBIDDEN"


def test_control_state_must_be_outside_devspace_project(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(
            manifest(tmp_path, mission.resolve(), run_root=str((tmp_path / ".ai-bridge" / "runs").resolve()))
        )
    assert exc.value.code in {"RUN_ROOT_OUTSIDE_HOST_STATE", "HOST_STATE_OVERLAPS_PROJECT"}
    mission = tmp_path / "mission.md"
    overlap_manifest = manifest(tmp_path, mission.resolve())
    os.environ["CODEX_ORACLE_STATE_ROOT"] = str((tmp_path / "host-state").resolve())
    with pytest.raises(state.OracleStateError) as overlap:
        state.load_manifest(overlap_manifest)
    assert overlap.value.code == "HOST_STATE_OVERLAPS_PROJECT"
