from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "codexpro_agbrowse_app.py"
SPEC = importlib.util.spec_from_file_location("codexpro_agbrowse_app_composer_test", MODULE_PATH)
assert SPEC and SPEC.loader
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


class UI:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.command_count = 0
        self.calls: list[tuple] = []

    def ensure_started(self):
        return None

    def new_tab(self, url):
        self.command_count += 1
        self.calls.append(("new-tab", url))
        return {"targetId": "T-RUN"}

    def activate_target(self, target_id):
        self.command_count += 1
        self.calls.append(("active-tab", target_id))
        return {"target_id": target_id}

    def settle(self):
        return None

    def snapshot(self):
        self.command_count += 1
        self.calls.append(("observe-bundle",))
        return APP.Snapshot(next(self.snapshots))

    def click(self, node):
        self.command_count += 1
        self.calls.append(("click", node.ref))

    def type(self, node, value):
        self.command_count += 1
        self.calls.append(("type", node.ref, value))

    def press(self, key):
        self.command_count += 1
        self.calls.append(("press", key))


def snapshot(nodes, *, target_id="T-RUN", text=""):
    return {
        "url": "https://chatgpt.com/",
        "targetId": target_id,
        "textSummary": text,
        "snapshotNodes": nodes,
    }


def test_exact_rate_limit_ack_is_clicked_once_on_run_owned_target() -> None:
    ui = UI(
        [
            snapshot(
                [{"ref": "ack", "role": "button", "name": "알겠습니다"}],
                text="요청 한도에 도달했습니다",
            ),
            snapshot(
                [{"ref": "box", "role": "textbox", "name": "ChatGPT와 채팅"}],
            ),
        ]
    )

    result = APP.AppConnector(ui).prepare_composer_app("CodexPro-CDrive-v14")

    assert result["rate_limit_dismissed"] is True
    assert result["textbox_resolution_attempts"] == 2
    assert result["textbox_ambiguity_counts"] == [0, 1]
    assert [call[0] for call in ui.calls].count("click") == 1
    assert [call[0] for call in ui.calls].count("type") == 1


def test_ambiguous_composer_is_bounded_to_three_fresh_snapshots() -> None:
    duplicate = [
        {"ref": "box1", "role": "textbox", "name": "ChatGPT와 채팅"},
        {"ref": "box2", "role": "textbox", "name": "ChatGPT와 채팅"},
    ]
    ui = UI([snapshot(duplicate), snapshot(duplicate), snapshot(duplicate)])

    with pytest.raises(APP.AppBridgeError) as failure:
        APP.AppConnector(ui).prepare_composer_app("CodexPro-CDrive-v14")

    assert failure.value.code == "APP_UI_DRIFT"
    assert failure.value.evidence["snapshot_attempts"] == 3
    assert failure.value.evidence["ambiguity_counts"] == [2, 2, 2]
    assert not any(call[0] == "type" for call in ui.calls)


def test_foreign_snapshot_target_is_never_clicked_or_typed() -> None:
    ui = UI(
        [
            snapshot(
                [
                    {"ref": "ack", "role": "button", "name": "알겠습니다"},
                    {"ref": "box", "role": "textbox", "name": "ChatGPT와 채팅"},
                ],
                target_id="T-FOREIGN",
                text="rate limit",
            ),
            snapshot([], target_id="T-FOREIGN"),
            snapshot([], target_id="T-FOREIGN"),
        ]
    )

    with pytest.raises(APP.AppBridgeError) as failure:
        APP.AppConnector(ui).prepare_composer_app("CodexPro-CDrive-v14")

    assert failure.value.code == "APP_COMPOSER_TARGET_MISMATCH"
    assert not any(call[0] in {"click", "type"} for call in ui.calls)
