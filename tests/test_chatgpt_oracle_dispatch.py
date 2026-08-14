from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_dispatch.py"


def load():
    spec = importlib.util.spec_from_file_location("oracle_dispatch_test", PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_regular_and_deep_research_compile_to_oracle_without_attachments(tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    for mode, research in (("direct", "off"), ("edit", "off"), ("orchestrator", "off"), ("deep-research", "deep")):
        target = tmp_path / f"{mode}.json"
        result = module.compile_manifest(
            mode=mode, project_root=tmp_path, mission_path=mission, output_path=target
        )
        value = json.loads(target.read_text(encoding="utf-8"))
        assert result["contract"]["attachments"] == []
        assert value["app_name"] == "DevSpace"
        assert value["task_outcome_contract"] == "v1"
        assert value["model"] == "gpt-5.6"
        assert value["model_strategy"] == "select"
        assert value["thinking_time"] == "extra-high"
        assert value["research"] == research
        assert value["mission_sha256"] == module.RUNNER.STATE.sha256_file(mission)
        assert "project_context_manifest_path" not in value


def test_pro_compiles_attachment_only_oracle_and_manual_never_launches(tmp_path: Path) -> None:
    module = load()
    prompt = tmp_path / "prompt.txt"
    packet = tmp_path / "packet.zip"
    context_manifest = tmp_path / "pro-context-manifest.json"
    prompt.write_text("instructions", encoding="utf-8")
    packet.write_bytes(b"PK\x03\x04packet")
    context_manifest.write_text("{}", encoding="utf-8")
    pro_target = tmp_path / "pro.json"
    pro = module.compile_manifest(
        mode="pro",
        project_root=tmp_path,
        mission_path=prompt,
        output_path=pro_target,
        attachment_paths=[prompt, packet],
        context_manifest_path=context_manifest,
    )
    value = json.loads(pro_target.read_text(encoding="utf-8"))
    assert pro["contract"]["route"] == "oracle-pro-attachment-only"
    assert value["transport"] == "pro-attachment-only"
    assert value["model"] == "gpt-5.6-sol"
    assert value["thinking_time"] == "heavy"
    assert value["mission_sha256"] == module.RUNNER.STATE.sha256_file(prompt)
    assert value["attachments"] == [str(prompt.resolve()), str(packet.resolve())]
    assert value["attachment_sha256s"] == [
        module.RUNNER.STATE.sha256_file(prompt),
        module.RUNNER.STATE.sha256_file(packet),
    ]
    assert value["project_context_manifest_path"] == str(context_manifest.resolve())
    assert value["project_context_manifest_sha256"] == module.RUNNER.STATE.sha256_file(context_manifest)
    assert pro["oracle_manifest_sha256"] == module.RUNNER.STATE.sha256_file(pro_target)
    assert "app_name" not in value

    manual_target = tmp_path / "manual.json"
    manual = module.compile_manifest(
        mode="manual", project_root=tmp_path, mission_path=None, output_path=manual_target
    )
    assert manual["oracle_manifest_path"] is None
    assert not manual_target.exists()


def test_context_manifest_is_required_for_pro_and_forbidden_for_regular_modes(tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    packet = tmp_path / "packet.zip"
    context_manifest = tmp_path / "pro-context-manifest.json"
    mission.write_text("work", encoding="utf-8")
    packet.write_bytes(b"packet")
    context_manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="PRO_CONTEXT_MANIFEST_REQUIRED"):
        module.compile_manifest(
            mode="pro",
            project_root=tmp_path,
            mission_path=mission,
            output_path=tmp_path / "pro.json",
            attachment_paths=[mission, packet],
        )
    with pytest.raises(ValueError, match="CONTEXT_MANIFEST_FORBIDDEN"):
        module.compile_manifest(
            mode="direct",
            project_root=tmp_path,
            mission_path=mission,
            output_path=tmp_path / "direct.json",
            context_manifest_path=context_manifest,
        )
    with pytest.raises(ValueError, match="CONTEXT_MANIFEST_FORBIDDEN"):
        module.compile_manifest(
            mode="pro",
            project_root=tmp_path,
            mission_path=mission,
            output_path=tmp_path / "pro-devspace.json",
            context_manifest_path=context_manifest,
        )


def test_project_url_is_bound_into_a_regular_manifest(tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    project_url = "https://chatgpt.com/g/g-p-example/project"
    target = tmp_path / "regular.json"
    module.compile_manifest(
        mode="GPT", project_root=tmp_path, mission_path=mission, output_path=target,
        chatgpt_project_url=project_url,
    )
    assert json.loads(target.read_text(encoding="utf-8"))["chatgpt_project_url"] == project_url


def test_named_project_profile_resolves_to_the_exact_manifest_url(monkeypatch, capsys, tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    target = tmp_path / "regular.json"
    mission.write_text("work", encoding="utf-8")
    project_url = "https://chatgpt.com/g/g-p-example/project"
    monkeypatch.setattr(module.PROJECTS, "resolve_profile", lambda name, store: project_url)
    monkeypatch.setattr(module.RUNNER, "execute_run", lambda *args, **kwargs: {"ok": True})

    assert module.main([
        "--mode", "direct",
        "--project-root", str(tmp_path),
        "--mission-path", str(mission),
        "--manifest-output", str(target),
        "--chatgpt-project", "devspace-oracle",
        "--dry-run",
    ]) == 0
    capsys.readouterr()
    assert json.loads(target.read_text(encoding="utf-8"))["chatgpt_project_url"] == project_url


def test_live_dispatch_requires_and_propagates_preview_hash(monkeypatch, capsys, tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    target = tmp_path / "oracle.json"
    mission.write_text("work", encoding="utf-8")
    preview = module.compile_manifest(
        mode="direct", project_root=tmp_path, mission_path=mission, output_path=target
    )
    expected = preview["oracle_manifest_sha256"]
    args = [
        "--mode", "direct",
        "--project-root", str(tmp_path),
        "--mission-path", str(mission),
        "--manifest-output", str(target),
    ]
    calls = []
    monkeypatch.setattr(module.RUNNER, "execute_run", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert module.main(args) == 1
    assert "MANIFEST_SHA256_REQUIRED" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert calls == []

    monkeypatch.setattr(
        module.RUNNER,
        "execute_run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )
    assert module.main([*args, "--expected-manifest-sha256", expected]) == 0
    capsys.readouterr()
    assert calls == [((target,), {"expected_manifest_sha256": expected, "dry_run": False})]


def test_live_dispatch_rejects_mission_change_after_preview(monkeypatch, capsys, tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    target = tmp_path / "oracle.json"
    mission.write_text("previewed", encoding="utf-8")
    expected = module.compile_manifest(
        mode="direct", project_root=tmp_path, mission_path=mission, output_path=target
    )["oracle_manifest_sha256"]
    mission.write_text("changed", encoding="utf-8")

    def validate_only(path: Path, *, expected_manifest_sha256: str, dry_run: bool):
        module.RUNNER.STATE.load_manifest(
            path, expected_manifest_sha256=expected_manifest_sha256
        )
        return {"ok": True}

    monkeypatch.setattr(module.RUNNER, "execute_run", validate_only)
    assert module.main([
        "--mode", "direct",
        "--project-root", str(tmp_path),
        "--mission-path", str(mission),
        "--manifest-output", str(target),
        "--expected-manifest-sha256", expected,
    ]) == 1
    assert "does not match the current Oracle manifest" in json.loads(
        capsys.readouterr().out
    )["error"]["message"]


def test_attachment_free_pro_dry_run_is_pro_devspace_with_write_authority(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("pro work", encoding="utf-8")
    target = tmp_path / "pro-devspace.json"

    assert module.main([
        "--mode", "pro",
        "--project-root", str(tmp_path),
        "--mission-path", str(mission),
        "--manifest-output", str(target),
        "--dry-run",
    ]) == 0
    value = json.loads(capsys.readouterr().out)

    assert value["contract"]["route"] == "oracle-pro-devspace"
    assert value["contract"]["pro_selection_policy"] == "explicit-only"
    run = value["run"]
    assert run["status"] == "dry-run"
    assert run["transport"] == "pro-devspace"
    argv = run["argv"]
    assert "--file" not in argv
    assert "--browser-attachments" not in argv
    assert run["contains_file_flag"] is False
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert argv[argv.index("--browser-model-strategy") + 1] == "select"
    assert argv[argv.index("--browser-thinking-time") + 1] == "heavy"
    assert "--browser-hide-window" in argv

    compiled = json.loads(target.read_text(encoding="utf-8"))
    assert compiled["transport"] == "pro-devspace"
    assert compiled["app_name"] == "DevSpace"
    assert compiled["task_outcome_contract"] == "v1"
    assert compiled["model"] == "gpt-5.6-sol"
    assert compiled["thinking_time"] == "heavy"
    assert "attachments" not in compiled
    assert "attachment_sha256s" not in compiled
    assert "project_context_manifest_path" not in compiled
    assert "project_context_manifest_sha256" not in compiled

    config = module.RUNNER.STATE.load_manifest(
        target, expected_manifest_sha256=value["oracle_manifest_sha256"]
    )
    prompt = module.RUNNER.STATE.composer_prompt(config, config.mission_path)
    assert prompt == (
        f"@{module.PROFILES.DEVSPACE_APP_NAME} Read and execute the mission file inside "
        f"exact_project_root={tmp_path.resolve()}. "
        f"{module.PROFILES.PRO_DEVSPACE_WRITE_AUTHORITY} Mission file: {mission.resolve()}"
    )
    assert "create, edit, and remove mission-owned files" in prompt
    assert prompt.splitlines() == [prompt]
    assert run["prompt_first_line"] == prompt


def test_unknown_launch_route_fails_closed(monkeypatch, tmp_path: Path) -> None:
    module = load()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")

    def unknown_route(mode, **kwargs):
        return {
            "mode": mode,
            "task_kind": "direct",
            "oracle_launch": True,
            "devspace_required": True,
            "research": False,
            "route": "oracle-unknown-route",
            "attachments": [],
            "model": "gpt-5.6",
            "thinking_time": "extra-high",
            "mission_path": str(mission),
            "composer_prompt": f"@DevSpace {mission}",
        }

    monkeypatch.setattr(module.PROFILES, "build_launch_contract", unknown_route)
    with pytest.raises(ValueError, match="ORACLE_ROUTE_UNSUPPORTED"):
        module.compile_manifest(
            mode="direct",
            project_root=tmp_path,
            mission_path=mission,
            output_path=tmp_path / "unknown.json",
        )
    assert not (tmp_path / "unknown.json").exists()
