from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "bin" / "chatgpt_agbrowse_composer.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_agbrowse_composer_test", PATH)
assert SPEC and SPEC.loader
COMPOSER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = COMPOSER
SPEC.loader.exec_module(COMPOSER)


class UI:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.calls: list[tuple] = []

    def ensure_started(self):
        self.calls.append(("start",))

    def new_tab(self, url):
        self.calls.append(("new-tab", url))
        return {"targetId": "T-RESEARCH"}

    def activate_target(self, target_id):
        self.calls.append(("activate", target_id))
        return {"target_id": target_id}

    def settle(self):
        return None

    def snapshot(self):
        self.calls.append(("snapshot",))
        return COMPOSER.PRIMITIVES.Snapshot(next(self.snapshots))

    def type(self, node, value):
        self.calls.append(("type", node.ref, value))

    def press(self, key):
        self.calls.append(("press", key))


def snapshot(nodes, *, target_id="T-RESEARCH"):
    return {
        "url": "https://chatgpt.com/",
        "targetId": target_id,
        "snapshotNodes": nodes,
    }


def before_snapshot():
    return snapshot(
        [{"ref": "composer", "role": "textbox", "name": "ChatGPT와 채팅"}]
    )


def app_snapshot(*extra):
    return snapshot(
        [
            {"ref": "composer", "role": "textbox", "name": "ChatGPT와 채팅"},
            {"ref": "app", "role": "button", "name": "CodexPro-Test"},
            *extra,
        ]
    )


def prepare(composer, *, run_id, workflow_id):
    return composer.prepare(run_id=run_id, workflow_id=workflow_id, app_name="CodexPro-Test")


def test_exact_korean_token_tab_and_new_capability_pill_are_proven() -> None:
    ui = UI(
        [
            before_snapshot(),
            app_snapshot(),
            snapshot([{"ref": "deep", "role": "button", "name": "심층 리서치"}]),
        ]
    )

    result = prepare(COMPOSER.ResearchComposer(ui),
        run_id="run-1",
        workflow_id="workflow-1",
    )

    assert result["schema"] == "codex.chatgpt.capability-selection/v1"
    assert result["state"] == "deep-research-selected"
    assert result["target_id"] == "T-RESEARCH"
    assert result["session_id"] is None
    assert result["app_name"] == "CodexPro-Test"
    assert result["app_selection_method"] == "exact-at-mention-then-tab"
    assert result["selected_marker"]["name"] == "심층 리서치"
    assert result["selection_proof"]["kind"] == "token-to-pill-transition"
    assert [call for call in ui.calls if call[0] == "type"] == [
        ("type", "composer", "@CodexPro-Test"),
        ("type", "composer", "@심층 리서치")
    ]
    assert [call for call in ui.calls if call[0] == "press"] == [("press", "Tab"), ("press", "Tab")]
    for key in (
        "token_sha256",
        "before_snapshot_sha256",
        "after_snapshot_sha256",
        "action_transcript_sha256",
    ):
        assert len(result[key]) == 64


def test_checked_radio_marker_is_accepted() -> None:
    ui = UI(
        [
            before_snapshot(),
            app_snapshot(),
            snapshot(
                [
                    {
                        "ref": "deep",
                        "role": "radio",
                        "name": "Deep Research",
                        "checked": True,
                    }
                ]
            ),
        ]
    )

    result = prepare(COMPOSER.ResearchComposer(ui),
        run_id="run-2",
        workflow_id="workflow-2",
    )

    assert result["selected_marker"] == {
        "role": "radio",
        "name": "Deep Research",
        "checked": True,
    }
    assert result["selection_proof"]["kind"] == "explicit-state"


def test_generic_persistent_research_button_is_not_selection_proof() -> None:
    persistent_button = {"ref": "deep", "role": "button", "name": "Deep Research"}
    ui = UI(
        [
            snapshot(
                [
                    {"ref": "composer", "role": "textbox", "name": "ChatGPT와 채팅"},
                    persistent_button,
                ]
            ),
            app_snapshot(persistent_button),
            snapshot([persistent_button]),
        ]
    )

    with pytest.raises(COMPOSER.ResearchComposerError) as failure:
        prepare(COMPOSER.ResearchComposer(ui), run_id="run-persistent", workflow_id="workflow-persistent")

    assert failure.value.code == "DEEP_RESEARCH_CAPABILITY_UNPROVEN"
    assert failure.value.evidence["transition_candidate_count"] == 0


def test_final_generic_button_requires_this_composers_prior_transition_proof() -> None:
    generic_button = {"ref": "deep", "role": "button", "name": "Deep Research"}
    ui = UI([before_snapshot(), app_snapshot(), snapshot([generic_button]), snapshot([generic_button])])
    composer = COMPOSER.ResearchComposer(ui)

    prepare(composer, run_id="run-final", workflow_id="workflow-final")
    final_check = composer.verify_selected("T-RESEARCH")

    assert final_check["selection_proof_kind"] == "token-to-pill-transition"

    unproven = UI([snapshot([generic_button])])
    with pytest.raises(COMPOSER.ResearchComposerError) as failure:
        COMPOSER.ResearchComposer(unproven).verify_selected("T-RESEARCH")

    assert failure.value.code == "DEEP_RESEARCH_CAPABILITY_UNPROVEN"


def test_transition_proof_rehydrates_for_same_target_after_process_restart() -> None:
    generic_button = {"ref": "deep", "role": "button", "name": "Deep Research"}
    ui = UI([before_snapshot(), app_snapshot(), snapshot([generic_button]), snapshot([generic_button])])
    evidence = prepare(COMPOSER.ResearchComposer(ui),
        run_id="run-restart", workflow_id="workflow-restart"
    )
    restarted = COMPOSER.ResearchComposer(ui)
    restarted.restore_selection_evidence(evidence)

    final_check = restarted.verify_selected("T-RESEARCH")

    assert final_check["selection_proof_kind"] == "token-to-pill-transition"


def test_restart_rejects_tampered_saved_selection_hashes() -> None:
    generic_button = {"ref": "deep", "role": "button", "name": "Deep Research"}
    evidence = prepare(COMPOSER.ResearchComposer(UI([before_snapshot(), app_snapshot(), snapshot([generic_button])])),
        run_id="run-tampered", workflow_id="workflow-tampered"
    )
    tampered = deepcopy(evidence)
    tampered["selection_proof"]["after_snapshot_sha256"] = "0" * 64

    with pytest.raises(COMPOSER.ResearchComposerError) as failure:
        COMPOSER.ResearchComposer(UI([snapshot([generic_button])])).restore_selection_evidence(tampered)

    assert failure.value.code == "RESEARCH_SELECTION_EVIDENCE_INVALID"


def test_unchecked_suggestion_is_not_terminal_selection_proof() -> None:
    ui = UI(
        [
            before_snapshot(),
            app_snapshot(),
            snapshot(
                [
                    {
                        "ref": "deep",
                        "role": "option",
                        "name": "심층 리서치",
                        "checked": False,
                    }
                ]
            ),
        ]
    )

    with pytest.raises(COMPOSER.ResearchComposerError) as failure:
        prepare(COMPOSER.ResearchComposer(ui), run_id="run-3", workflow_id="workflow-3")

    assert failure.value.code == "DEEP_RESEARCH_CAPABILITY_UNPROVEN"
    assert failure.value.evidence["owned_target_id"] == "T-RESEARCH"


def test_foreign_post_tab_snapshot_fails_with_owned_cleanup_identity() -> None:
    ui = UI(
        [
            before_snapshot(),
            app_snapshot(),
            snapshot(
                [{"ref": "deep", "role": "button", "name": "심층 리서치"}],
                target_id="T-FOREIGN",
            ),
        ]
    )

    with pytest.raises(COMPOSER.ResearchComposerError) as failure:
        prepare(COMPOSER.ResearchComposer(ui), run_id="run-4", workflow_id="workflow-4")

    assert failure.value.code == "RESEARCH_COMPOSER_TARGET_MISMATCH"
    assert failure.value.evidence["owned_target_id"] == "T-RESEARCH"


def test_ambiguous_selected_markers_fail_closed() -> None:
    ui = UI(
        [
            before_snapshot(),
            app_snapshot(),
            snapshot(
                [
                    {"ref": "deep-1", "role": "button", "name": "심층 리서치"},
                    {"ref": "deep-2", "role": "button", "name": "Deep Research"},
                ]
            ),
        ]
    )

    with pytest.raises(COMPOSER.ResearchComposerError) as failure:
        prepare(COMPOSER.ResearchComposer(ui), run_id="run-5", workflow_id="workflow-5")

    assert failure.value.code == "DEEP_RESEARCH_CAPABILITY_UNPROVEN"
    assert failure.value.evidence["transition_candidate_count"] == 2
