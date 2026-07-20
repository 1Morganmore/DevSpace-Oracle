from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "bin" / "chatgpt_agbrowse_bridge.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_agbrowse_research_bridge_test", PATH)
assert SPEC and SPEC.loader
BRIDGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BRIDGE
SPEC.loader.exec_module(BRIDGE)


def manifest(tmp_path: Path, *, mode_label="Deep Research", mode_variant="High") -> dict:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("연구 지시", encoding="utf-8")
    return {
        "project_root": str(tmp_path),
        "question": BRIDGE.STATE.PROMPT_FILE_HANDOFF,
        "prompt_transport": "file",
        "prompt_file": str(prompt),
        "prompt_file_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
        "files": [str(prompt)],
        "mode_label": mode_label,
        "mode_variant": mode_variant,
        "app_policy": "required",
        "chatgpt_app_name": "CodexPro-Test",
        "research_selection_transport": "preselected-research",
        "research_selection_contract": "codex.chatgpt.capability-selection/v1",
    }


def test_deep_research_command_reuses_exact_preselected_target_at_high(tmp_path: Path) -> None:
    value = manifest(tmp_path)
    command = BRIDGE.build_send_command(
        {"requested": {"app_policy": "required"}},
        value,
        "agbrowse",
        preselected_app=True,
        preselected_research=True,
    )

    assert command[-2:] == ["--reuse-tab", "--json"]
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--research") + 1] == "deep"
    assert "--plugin" not in command


def test_deep_research_without_selection_proof_fails_before_command(tmp_path: Path) -> None:
    with pytest.raises(BRIDGE.BridgeError) as failure:
        BRIDGE.build_send_command(
            {"requested": {"app_policy": "required"}},
            manifest(tmp_path),
            "agbrowse",
            preselected_app=True,
        )

    assert failure.value.code == "RESEARCH_PRESELECTION_REQUIRED"


def test_deep_research_requires_exact_app_preselection(tmp_path: Path) -> None:
    value = manifest(tmp_path)

    with pytest.raises(BRIDGE.BridgeError) as failure:
        BRIDGE.build_send_command(
            {"requested": {"app_policy": "required"}},
            value,
            "agbrowse",
            preselected_research=True,
        )

    assert failure.value.code == "RESEARCH_PRESELECTION_INVALID"


def test_restart_retry_consumes_saved_research_evidence_not_app_evidence(tmp_path: Path) -> None:
    """A crash after binding must retain the selected research tab and proof."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    research_evidence = run_dir / "composer-research-evidence.json"
    research_evidence.write_text("{}", encoding="utf-8")
    record = {
        "record_kind": "child",
        "phase": "LEASED",
        "current_target_id": "research-target",
        "pre_submit_retry_authority": {
            "eligible": True,
            "consumed_at": None,
            "cleanup_target_id": "old-target",
        },
        "selection_evidence_refs": [{
            "kind": "deep-research-app-selection",
            "target_id": "research-target",
            "path": str(research_evidence),
        }],
    }

    class Store:
        def __init__(self):
            self.confirmed = None

        def load(self, _run_dir):
            return run_dir / "run.json", record

        def confirm_child_retry_replacement(self, _run_dir, *, target_id, evidence_path):
            self.confirmed = (target_id, Path(evidence_path))
            record["pre_submit_retry_authority"]["replacement_target_id"] = target_id
            return record

        def assert_child_send_available(self, _run_dir):
            return record

    store = Store()
    bridge = SimpleNamespace(store=store, _send_locked=lambda path: {"reused": path})

    result = BRIDGE.Bridge.send(bridge, str(run_dir))

    assert result == {"reused": str(run_dir)}
    assert store.confirmed == ("research-target", research_evidence)


@pytest.mark.parametrize("variant", ["Very High", "Medium", "Instant", "xhigh"])
def test_new_regular_non_high_mode_is_rejected_without_fallback(tmp_path: Path, variant: str) -> None:
    value = manifest(tmp_path, mode_label="GPT-5.6", mode_variant=variant)
    value.pop("research_selection_transport")
    value.pop("research_selection_contract")

    with pytest.raises(BRIDGE.BridgeError) as failure:
        BRIDGE.build_send_command(
            {"requested": {"app_policy": "required"}},
            value,
            "agbrowse",
            preselected_app=True,
        )

    assert failure.value.code == "MODE_VARIANT_UNSUPPORTED"
