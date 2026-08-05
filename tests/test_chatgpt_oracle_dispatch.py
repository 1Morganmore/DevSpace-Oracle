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
        assert value["thinking_time"] == "heavy"
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
    assert value["model"] == "gpt-5.5-pro"
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
