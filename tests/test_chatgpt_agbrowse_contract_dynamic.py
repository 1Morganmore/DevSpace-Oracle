from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "chatgpt_agbrowse_contract.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_agbrowse_contract_dynamic_test", MODULE_PATH)
assert SPEC and SPEC.loader
CONTRACT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CONTRACT
SPEC.loader.exec_module(CONTRACT)


def fixture() -> dict:
    return json.loads((ROOT / "tests" / "fixtures" / "agbrowse-contract-v1.json").read_text(encoding="utf-8"))


def test_baseline_contract_still_validates_by_default() -> None:
    result = CONTRACT.validate_manifest(fixture())
    assert result["version"] == "0.1.18"


def test_explicit_agent_selected_version_and_integrity_validate() -> None:
    value = fixture()
    package = value["agbrowse"]
    package["version"] = package["expectedVersion"] = "0.2.0"
    package["npmIntegrity"] = package["expectedNpmIntegrity"] = "sha512-agent-selected"

    result = CONTRACT.validate_manifest(
        value,
        expected_version="0.2.0",
        expected_npm_integrity="sha512-agent-selected",
    )

    assert result["version"] == "0.2.0"


def test_selected_contract_rejects_declared_or_actual_drift() -> None:
    value = fixture()
    value["agbrowse"]["expectedVersion"] = "0.2.0"
    with pytest.raises(CONTRACT.ContractError) as failure:
        CONTRACT.validate_manifest(
            value,
            expected_version="0.2.0",
            expected_npm_integrity=value["agbrowse"]["expectedNpmIntegrity"],
        )
    assert failure.value.code == "MANIFEST_INVALID"
