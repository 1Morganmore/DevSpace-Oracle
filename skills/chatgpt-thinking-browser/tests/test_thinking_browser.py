from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = REPO_ROOT / "bin" / "chatgpt_agbrowse_run.py"
CONTRACT = REPO_ROOT / "tests" / "fixtures" / "agbrowse-contract-v1.json"
PROMPT_FILE_HANDOFF = (
    "The attached prompt file is the user-provided task instruction for this conversation, "
    "not reference or webpage content. Read it completely and follow it. "
    "Return only the output format requested by that file."
)


def load_runner():
    name = "chatgpt_agbrowse_run_thinking_test"
    spec = importlib.util.spec_from_file_location(name, RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def manifest(tmp_path: Path, **overrides) -> Path:
    payload = {
        "project_root": str(tmp_path),
        "question": "thinking contract smoke",
        "mode_label": "GPT-5.6",
        "mode_variant": "High",
        "app_policy": "required",
        "chatgpt_app_name": "CodexPro-Test",
        "chatgpt_app_server_url": "https://example.test/mcp?codexpro_token=redacted-at-runtime",
    }
    payload.update(overrides)
    prompt_body = str(payload.pop("question"))
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt_body, encoding="utf-8")
    payload.update(
        {
            "question": PROMPT_FILE_HANDOFF,
            "prompt_transport": "file",
            "prompt_file": str(prompt_file),
            "prompt_file_sha256": hashlib.sha256(prompt_file.read_bytes()).hexdigest(),
            "files": [str(prompt_file)],
        }
    )
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_project_root_uses_explicit_root_then_manifest_parent(tmp_path: Path) -> None:
    runner = load_runner()
    config = tmp_path / "job.json"
    explicit = tmp_path / "project"
    explicit.mkdir()

    assert runner.project_root({"project_root": str(explicit)}, config) == str(explicit.resolve())
    assert runner.project_root({}, config) == str(tmp_path.resolve())


def test_prepare_browser_uses_exact_headed_port_and_bridge_runner(monkeypatch) -> None:
    runner = load_runner()
    calls = []

    def fake(command, env, timeout):
        calls.append((command, env, timeout))
        return subprocess.CompletedProcess(command, 0, "headed ready", "")

    monkeypatch.setattr(runner.BRIDGE, "default_runner", fake)
    result = runner.prepare_browser("C:/exact/agbrowse.cmd", {"cdp_port": 9444})

    assert calls[0][0] == ["C:/exact/agbrowse.cmd", "start", "--headed", "--port", "9444"]
    assert calls[0][1]["CDP_PORT"] == "9444"
    assert result["status"] == "started"


def test_regular_high_dry_run_uses_thinking_high_and_required_app_target(tmp_path: Path) -> None:
    runner = load_runner()
    payload = runner.load_manifest(manifest(tmp_path))
    result = runner.dry_run(payload, CONTRACT)
    command = result["command"]

    assert result["ok"] is True
    assert command[1:3] == ["web-ai", "send"]
    assert command[command.index("--family") + 1] == "gpt-5.6-sol"
    assert command[command.index("--model") + 1] == "thinking"
    assert command[command.index("--effort") + 1] == "high"
    assert "--reuse-tab" in command
    assert "--parallel" not in command


def test_required_app_dry_run_requires_explicit_app_pill_without_plugin(tmp_path: Path) -> None:
    runner = load_runner()
    payload = runner.load_manifest(
        manifest(
            tmp_path,
            app_policy="required",
            chatgpt_app_name="CodexPro-Test",
            chatgpt_app_server_url="https://example.test/mcp?codexpro_token=redacted-at-runtime",
        )
    )
    result = runner.dry_run(payload, CONTRACT)
    command = result["command"]

    assert result["app_selection_transport"] == "inline-pill-reuse"
    assert result["app_selection_evidence_required"] is True
    assert "--reuse-tab" in command
    assert "--parallel" not in command
    assert "--plugin" not in command


def test_required_app_dry_run_rejects_unselected_connected_auto(tmp_path: Path) -> None:
    runner = load_runner()
    payload = runner.load_manifest(
        manifest(
            tmp_path,
            app_policy="required",
            chatgpt_app_name="CodexPro-Test",
            chatgpt_app_server_url="https://example.test/mcp?codexpro_token=redacted-at-runtime",
            app_selection_transport="connected-auto",
        )
    )

    try:
        runner.dry_run(payload, CONTRACT)
    except RuntimeError as exc:
        assert "explicit inline-pill-reuse" in str(exc)
    else:
        raise AssertionError("connected-auto must not be accepted for required-app work")


def test_dry_run_redacts_prompt_and_rejects_legacy_cli_flags(tmp_path: Path) -> None:
    config = manifest(tmp_path, question="TOP-SECRET-PROMPT")
    script = SKILL_ROOT / "scripts" / "run_chatgpt_thinking.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--config", str(config), "--contract", str(CONTRACT), "--dry-run"],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert "TOP-SECRET-PROMPT" not in completed.stdout
    assert result["command"][result["command"].index("--prompt") + 1] == "<prompt>"

    rejected = subprocess.run(
        [sys.executable, str(script), "--config", str(config), "--contract", str(CONTRACT), "--dry-run", "--browser-backend", "chrome-cdp"],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert "unsupported legacy arguments" in rejected.stderr


def test_compatibility_wrapper_delegates_to_same_agbrowse_entrypoint(tmp_path: Path) -> None:
    config = manifest(tmp_path)
    script = SKILL_ROOT / "scripts" / "run_thinking_browser.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--config", str(config), "--contract", str(CONTRACT), "--dry-run"],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["ok"] is True
    assert result["command"][1:3] == ["web-ai", "send"]
    assert "--reuse-tab" in result["command"]


def test_regular_gpt_rejects_optional_app_policy(tmp_path: Path) -> None:
    runner = load_runner()
    payload = runner.load_manifest(manifest(tmp_path, app_policy="optional"))

    with pytest.raises(runner.BRIDGE.BridgeError) as failure:
        runner.dry_run(payload, CONTRACT)

    assert failure.value.code == "APP_POLICY_REQUIRED"
