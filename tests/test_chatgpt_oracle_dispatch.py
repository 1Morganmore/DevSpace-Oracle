from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


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
        assert value["model"] == "gpt-5.6"
        assert value["model_strategy"] == "select"
        assert value["thinking_time"] == "heavy"
        assert value["research"] == research


def test_pro_and_manual_never_compile_oracle_manifest(tmp_path: Path) -> None:
    module = load()
    for mode in ("pro", "manual"):
        target = tmp_path / f"{mode}.json"
        result = module.compile_manifest(
            mode=mode, project_root=tmp_path, mission_path=None, output_path=target
        )
        assert result["oracle_manifest_path"] is None
        assert not target.exists()
