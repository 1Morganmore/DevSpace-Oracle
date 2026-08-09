from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_projects.py"


def load():
    spec = importlib.util.spec_from_file_location("oracle_projects_test", PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_project_profiles_round_trip_exact_urls_and_fail_closed(tmp_path: Path, monkeypatch) -> None:
    module = load()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "orca-runtime-home"))
    assert module.default_store() == Path.home() / ".codex" / "config" / "chatgpt-oracle-projects.json"
    store = tmp_path / "projects.json"
    url = "https://chatgpt.com/g/g-p-example/project/"
    module.save_profiles({"devspace-oracle": url}, store)

    assert module.resolve_profile("DEVSPACE-ORACLE", store) == url.rstrip("/")
    with pytest.raises(ValueError, match="unknown ChatGPT Project profile"):
        module.resolve_profile("missing", store)
    with pytest.raises(module.STATE.OracleStateError, match="exact https://chatgpt.com"):
        module.save_profiles({"bad": "https://example.com/project"}, store)
