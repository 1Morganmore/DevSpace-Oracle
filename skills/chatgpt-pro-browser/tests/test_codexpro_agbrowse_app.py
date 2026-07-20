from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[3] / "bin" / "codexpro_agbrowse_app.py"
SPEC = importlib.util.spec_from_file_location("codexpro_agbrowse_app_test", MODULE_PATH)
assert SPEC and SPEC.loader
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


def test_cli_reconcile_accepts_utf8_bom_decision(tmp_path, monkeypatch, capsys):
    decision_path = tmp_path / "decision.json"
    decision = {
        "root": "C:\\\\",
        "app_name": "CodexPro-CDrive-v14",
        "public_url": "https://example.test/mcp?token=exact",
    }
    decision_path.write_text(json.dumps(decision), encoding="utf-8-sig")
    captured = {}

    class FakeConnector:
        def __init__(self, _gateway):
            pass

        def reconcile(self, value):
            captured.update(value)
            return {"phase": "COMPLETE"}

    monkeypatch.setattr(APP, "AppConnector", FakeConnector)
    assert APP.main(["reconcile", "--decision", str(decision_path)]) == 0
    assert captured == decision
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_redacted_bootstrap_decision_uses_exact_pending_registry_candidate(monkeypatch):
    class RegistryModule:
        @staticmethod
        def load_registry():
            return {
                "pending_reconciles": {
                    "tx-14": {
                        "root": "C:\\\\",
                        "candidate": {
                            "app_name": "CodexPro-CDrive-v14",
                            "public_url": "https://example.test/mcp?codexpro_token=exact-secret",
                        },
                    }
                }
            }

    monkeypatch.setattr(APP, "APP_MANAGER", RegistryModule())
    hydrated = APP._hydrate_redacted_decision_url(
        {
            "root": "C:\\\\",
            "app_name": "CodexPro-CDrive-v14",
            "transaction_id": "tx-14",
            "public_url": "https://example.test/mcp?codexpro_token=<redacted>",
        }
    )

    assert hydrated["public_url"].endswith("codexpro_token=exact-secret")


def test_windows_npm_shim_uses_direct_entrypoint_without_cmd_reparsing(tmp_path):
    shim = tmp_path / "agbrowse.CMD"
    entrypoint = tmp_path / "node_modules" / "agbrowse" / "bin" / "agbrowse.mjs"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("// pinned test entrypoint\n", encoding="utf-8")
    url = "https://example.test/mcp?token=one&scope=full"

    result = APP._direct_agbrowse_argv(
        [str(shim), "type", "e17", url],
        platform="nt",
        node_executable="C:\\Program Files\\nodejs\\node.exe",
    )

    assert result == [
        "C:\\Program Files\\nodejs\\node.exe",
        str(entrypoint),
        "type",
        "e17",
        url,
    ]


def node(ref: str, role: str, name: str, value: str = "", checked=None):
    result = {"ref": ref, "role": role, "name": name, "value": value}
    if checked is not None:
        result["checked"] = checked
    return result


def snap(*nodes, url="https://chatgpt.com/#settings/Plugins", target_id=None):
    resolved_target = target_id or ("target-composer" if url == "https://chatgpt.com/" else "target-1")
    return APP.Snapshot({"url": url, "targetId": resolved_target, "snapshotNodes": list(nodes)})


def test_snapshot_accepts_live_agbrowse_refs_field():
    page = APP.Snapshot({
        "url": "https://chatgpt.com/#settings/Plugins",
        "targetId": "target-1",
        "refs": [node("e1", "button", "CodexPro-CDrive-v11 모두 허용")],
    })
    assert page.exact_app("CodexPro-CDrive-v11").ref == "e1"


class FakeGateway:
    def __init__(self, snapshots, dom_text=""):
        self.snapshots = list(snapshots)
        self.dom_text = list(dom_text) if isinstance(dom_text, list) else dom_text
        self.calls = []

    def ensure_started(self, **_kwargs):
        self.calls.append(("ensure-started",))

    def settle(self):
        self.calls.append(("settle",))

    def navigate(self, url):
        self.calls.append(("navigate", url))

    def snapshot(self):
        self.calls.append(("snapshot",))
        return self.snapshots.pop(0)

    def click(self, item):
        self.calls.append(("click", item.name))

    def type(self, item, value):
        self.calls.append(("type", item.name, value))

    def select(self, item, value):
        self.calls.append(("select", item.name, value))

    def check(self, item):
        self.calls.append(("check", item.name))

    def press(self, key):
        self.calls.append(("press", key))

    def new_tab(self, url):
        self.calls.append(("new-tab", url))
        return {"targetId": "target-composer"}

    def activate_target(self, target_id):
        self.calls.append(("activate-target", target_id))
        return {"ok": True, "target_id": target_id}

    def open_utility_target(self, url):
        self.calls.append(("open-utility", url))
        return {"ok": True, "target_id": "target-1", "requested_url": url}

    def close_owned_target(self, target_id):
        self.calls.append(("close-owned", target_id))
        return {"ok": True, "target_id": target_id, "closed": True, "absence_verified": True}

    def dom(self, selector, max_chars=20_000):
        self.calls.append(("get-dom", selector, max_chars))
        if isinstance(self.dom_text, list):
            return self.dom_text.pop(0)
        return self.dom_text


class FakeRegistry:
    def __init__(self):
        self.calls = []

    def record_reconcile_started(self, decision):
        self.calls.append(("start", decision["app_name"]))
        return {"ok": True}

    def record_reconcile_confirmation(self, decision, result):
        self.calls.append(("commit", decision["app_name"], result))
        return {
            "ok": True,
            "action": "candidate-committed" if decision.get("transaction_id") else "reuse-confirmed",
            "root": decision.get("root"),
            "retired_app_name": decision.get("old_app_name"),
        }

    def record_reconcile_failure(self, decision, result):
        self.calls.append(("failure", decision["app_name"]))
        return {"ok": True}

    def record_retired_cleanup(self, decision, result):
        self.calls.append(("cleanup", decision["old_app_name"]))
        return {"ok": True}

    def load_registry(self):
        return {
            "projects": {
                "C:\\": {
                    "app_name": "CodexPro-CDrive-v11",
                    "public_url": "https://example.test/mcp?token=exact",
                    "status": "active",
                }
            },
            "retired_apps": [],
        }


def detail(name="CodexPro-r-v01", url="https://example.test/mcp", full=True):
    return snap(
        node("e1", "textbox", "Server URL", url),
        node("e2", "button", "Disconnect"),
        node("e3", "radio", "Allow all actions", checked=full),
        node("e4", "button", "More"),
        url=f"https://chatgpt.com/plugins/plugin_asdk_app_123?name={name}",
    )


def test_snapshot_exact_fails_closed_on_ambiguous_controls():
    page = snap(node("e1", "button", "Create app"), node("e2", "button", "Create app"))
    try:
        page.exact(roles={"button"}, names=("Create app",))
    except APP.AppBridgeError as exc:
        assert exc.code == "APP_UI_DRIFT"
    else:
        raise AssertionError("ambiguous mutation control must fail closed")


def test_installed_app_match_accepts_permission_suffix_but_remains_exact():
    page = snap(
        node("e1", "button", "CodexPro-CDrive-v11 모두 허용"),
        node("e2", "button", "CodexPro-CDrive-v110 모두 허용"),
        node("e3", "link", "CodexPro-CDrive-v11 Error"),
    )
    assert page.exact_app("CodexPro-CDrive-v11").ref == "e1"


def test_installed_app_match_fails_closed_on_ambiguous_suffixes():
    page = snap(
        node("e1", "button", "CodexPro-CDrive-v11 모두 허용"),
        node("e2", "row", "CodexPro-CDrive-v11 연결됨"),
    )
    try:
        page.exact_app("CodexPro-CDrive-v11")
    except APP.AppBridgeError as exc:
        assert exc.code == "APP_UI_DRIFT"
    else:
        raise AssertionError("ambiguous installed-app controls must fail closed")


def test_gateway_rejects_non_agbrowse_escape_hatch():
    gateway = APP.AgbrowseGateway(runner=lambda *_: None)
    try:
        gateway.call("evaluate", "document.body")
    except APP.AppBridgeError as exc:
        assert exc.code == "APP_COMMAND_FORBIDDEN"
    else:
        raise AssertionError("evaluate/raw CDP escape hatch was accepted")


def test_reuse_requires_full_url_connection_and_permission_before_commit():
    gateway = FakeGateway([
        snap(node("e1", "button", "CodexPro-r-v01")),
        detail(),
    ])
    registry = FakeRegistry()
    connector = APP.AppConnector(gateway, registry=registry)
    result = connector.reconcile({
        "root": "C:\\repo",
        "app_name": "CodexPro-r-v01",
        "public_url": "https://example.test/mcp",
        "old_app_name": None,
    })
    assert result["action"] == "reuse"
    commit = registry.calls[0]
    assert commit[0] == "commit"
    assert commit[2]["state"] == "confirmed-visible"
    assert commit[2]["final_url_check"]["ok"] is True
    assert commit[2]["final_permission_check"]["ok"] is True
    assert gateway.calls[0] == ("open-utility", APP.SETTINGS_URL)
    assert gateway.calls[-1] == ("close-owned", "target-1")
    assert result["utility_cleanup"]["absence_verified"] is True


def test_inspect_composes_live_ui_state_with_exact_detail_dom_url():
    gateway = FakeGateway([
        snap(node("e1", "button", "CodexPro-CDrive-v11 모두 허용")),
        APP.Snapshot({
            "url": "https://chatgpt.com/#settings/Plugins/plugin_asdk_app_123",
            "targetId": "target-1",
            "refs": [node("e2", "button", "권한 모든 액션 허용 위험도 높음")],
            "textSummary": "CodexPro-CDrive-v11에 연결됨\n모든 액션 허용",
        }),
    ], dom_text='<div data-server-url="https://example.test/mcp?token=exact"></div>')
    connector = APP.AppConnector(gateway, registry=FakeRegistry())
    result = connector.inspect(
        "CodexPro-CDrive-v11",
        expected_url="https://example.test/mcp?token=exact",
    )
    assert result["url_source"] == "exact-detail-dom-match"
    assert result["connected"] is True
    assert result["full_access"] is True


def test_inspect_does_not_substitute_registry_url_when_live_ui_url_is_missing():
    gateway = FakeGateway([
        snap(node("e1", "button", "CodexPro-CDrive-v11 모두 허용")),
        APP.Snapshot({
            "url": "https://chatgpt.com/#settings/Plugins/plugin_asdk_app_123",
            "targetId": "target-1",
            "refs": [node("e2", "button", "권한 모든 액션 허용 위험도 높음")],
            "textSummary": "CodexPro-CDrive-v11에 연결됨\n모든 액션 허용",
        }),
    ], dom_text="")
    connector = APP.AppConnector(gateway, registry=FakeRegistry())
    result = connector.inspect(
        "CodexPro-CDrive-v11",
        expected_url="https://example.test/mcp?token=exact",
    )
    assert result["url"] is None
    assert result["url_source"] is None
    assert result["connected"] is True
    assert result["full_access"] is True


def test_current_permission_summary_does_not_misread_low_risk_as_full_access():
    gateway = FakeGateway(
        [
            snap(node("e1", "button", "CodexPro-r-v01 DEV")),
            APP.Snapshot(
                {
                    "url": "https://chatgpt.com/#settings/Plugins/plugin_asdk_app_123",
                    "targetId": "target-1",
                    "refs": [node("e2", "button", "권한 저위험 액션 허용 기본")],
                    "textSummary": "CodexPro-r-v01에 연결됨",
                }
            ),
            snap(
                node("e3", "radio", "모든 액션 허용 위험도 높음", checked=False),
                url="https://chatgpt.com/#settings/Plugins/plugin_asdk_app_123",
            ),
        ],
        dom_text="",
    )
    connector = APP.AppConnector(gateway, registry=FakeRegistry())
    result = connector.inspect("CodexPro-r-v01", expected_url="https://missing.example.test/mcp")
    assert result["connected"] is True
    assert result["full_access"] is False
    click_index = gateway.calls.index(("click", "권한 저위험 액션 허용 기본"))
    dom_index = next(index for index, call in enumerate(gateway.calls) if call[0] == "get-dom")
    assert click_index < dom_index


def test_current_korean_create_form_uses_optional_description_auth_and_long_trust_label():
    trust_name = (
        "내용을 이해했으며 계속 진행하길 원합니다 OpenAI가 이 MCP 서버를 검토하지 않았습니다. "
        "공격자들이 데이터를 훔치려 하거나 모델을 속여 의도하지 않은 작업을 하게 만들 수 있으며 "
        "여기에는 데이터를 파괴하는 행위가 포함됩니다."
    )
    gateway = FakeGateway(
        [
            snap(node("e1", "button", "앱 만들기"), url=APP.APP_DIRECTORY_URL),
            snap(
                node("e2", "textbox", "이름"),
                node("e3", "textbox", "설명 (선택)"),
                node("e4", "textbox", "MCP 서버 URL"),
                node("e5", "combobox", "인증"),
                node("e6", "option", "인증 없음"),
                node("e7", "checkbox", trust_name, checked=False),
                url=APP.APP_DIRECTORY_URL,
                ),
                snap(node("e8", "button", "만들기"), url=APP.APP_DIRECTORY_URL),
                snap(node("e9", "button", "CodexPro-r-v02 DEV"), url=APP.APP_DIRECTORY_URL),
            ]
        )
    connector = APP.AppConnector(gateway, registry=FakeRegistry())
    connector._fill_create_form(
        {"app_name": "CodexPro-r-v02", "public_url": "https://example.test/mcp?codexpro_token=exact"}
    )
    assert ("type", "설명 (선택)", "Codex project connector") in gateway.calls
    assert ("select", "인증", "인증 없음") in gateway.calls
    assert ("check", trust_name) in gateway.calls


def test_missing_create_surface_reports_developer_mode_setup_instead_of_generic_ui_drift():
    gateway = FakeGateway(
        [
            snap(
                node("e1", "button", "고급 설정"),
                url=APP.APP_DIRECTORY_URL,
            )
        ]
    )
    connector = APP.AppConnector(gateway, registry=FakeRegistry())

    with pytest.raises(APP.AppBridgeError) as failure:
        connector._fill_create_form(
            {"app_name": "CodexPro-r-v02", "public_url": "https://example.test/mcp?codexpro_token=exact"}
        )

    assert failure.value.code == "CHATGPT_DEVELOPER_MODE_REQUIRED"
    assert failure.value.evidence["settings_path"] == "Settings > Apps > Advanced settings > Developer mode"
    assert failure.value.evidence["workspace_admin_path"] == "Workspace settings > Apps > Create"
    assert failure.value.evidence["developer_mode_control_visible"] is True
    assert not any(call[0] == "click" for call in gateway.calls)


def test_prepare_composer_app_accepts_exact_mention_then_tab_without_pill_dom():
    gateway = FakeGateway(
        [snap(node("e1", "textbox", "ChatGPT와 채팅"), url="https://chatgpt.com/")],
        dom_text="",
    )
    connector = APP.AppConnector(gateway, registry=FakeRegistry())
    result = connector.prepare_composer_app("CodexPro-CDrive-v11")
    assert result["state"] == "composer-app-mention-tab-confirmed"
    assert result["selection_method"] == "exact-at-mention-then-tab"
    assert result["mention_text_sha256"] == hashlib.sha256(b"@CodexPro-CDrive-v11").hexdigest()
    assert result["target_id"] == "target-composer"
    assert ("type", "ChatGPT와 채팅", "@CodexPro-CDrive-v11") in gateway.calls
    assert gateway.calls.count(("press", "Tab")) == 1


def test_prepare_composer_app_does_not_require_or_read_composer_dom():
    gateway = FakeGateway(
        [snap(node("e1", "textbox", "ChatGPT와 채팅"), url="https://chatgpt.com/")],
        dom_text='<div id="prompt-textarea">@CodexPro-CDrive-v11</div>',
    )
    connector = APP.AppConnector(gateway, registry=FakeRegistry())
    result = connector.prepare_composer_app("CodexPro-CDrive-v11")
    assert result["state"] == "composer-app-mention-tab-confirmed"
    assert not any(call[0] == "get-dom" for call in gateway.calls)


def test_prepare_composer_app_uses_single_tab_key_to_confirm_suggestion():
    gateway = FakeGateway(
        [snap(node("e1", "textbox", "ChatGPT와 채팅"), url="https://chatgpt.com/")],
        dom_text="",
    )
    connector = APP.AppConnector(gateway, registry=FakeRegistry())
    result = connector.prepare_composer_app("CodexPro-CDrive-v11")
    assert result["state"] == "composer-app-mention-tab-confirmed"
    assert gateway.calls.count(("press", "Tab")) == 1


def test_prepare_composer_app_retries_transient_ambiguous_textbox_on_exact_owned_target():
    gateway = FakeGateway(
        [
            snap(
                node("e1", "textbox", "ChatGPT와 채팅"),
                node("e2", "textbox", "ChatGPT와 채팅"),
                url="https://chatgpt.com/",
            ),
            snap(node("e3", "textbox", "ChatGPT와 채팅"), url="https://chatgpt.com/"),
        ],
        dom_text="",
    )
    connector = APP.AppConnector(gateway, registry=FakeRegistry())

    result = connector.prepare_composer_app("CodexPro-CDrive-v11")

    assert result["state"] == "composer-app-mention-tab-confirmed"
    assert result["textbox_resolution_attempts"] == 2
    assert gateway.calls.count(("snapshot",)) == 2
    assert gateway.calls.count(("activate-target", "target-composer")) == 2
    assert gateway.calls.count(("press", "Tab")) == 1


def test_prepare_composer_app_resnapshots_same_target_after_transient_type_ref_failure():
    class TransientTypeGateway(FakeGateway):
        def __init__(self, snapshots):
            super().__init__(snapshots)
            self.type_attempts = 0

        def type(self, item, value):
            self.calls.append(("type", item.name, value))
            self.type_attempts += 1
            if self.type_attempts == 1:
                raise APP.AppBridgeError("APP_AGBROWSE_COMMAND_FAILED", "agbrowse type failed")

    gateway = TransientTypeGateway(
        [
            snap(node("e1", "textbox", "ChatGPT와 채팅"), url="https://chatgpt.com/"),
            snap(node("e2", "textbox", "ChatGPT와 채팅"), url="https://chatgpt.com/"),
        ]
    )
    connector = APP.AppConnector(gateway, registry=FakeRegistry())

    result = connector.prepare_composer_app("CodexPro-CDrive-v11")

    assert result["textbox_resolution_attempts"] == 2
    assert gateway.type_attempts == 2
    assert gateway.calls.count(("snapshot",)) == 2
    assert gateway.calls.count(("activate-target", "target-composer")) == 2
    assert gateway.calls.count(("press", "Tab")) == 1


def test_prepare_composer_app_does_not_reactivate_between_exact_type_and_tab():
    gateway = FakeGateway(
        [snap(node("e1", "textbox", "ChatGPT와 채팅"), url="https://chatgpt.com/")],
        dom_text="",
    )
    connector = APP.AppConnector(gateway, registry=FakeRegistry())

    connector.prepare_composer_app("CodexPro-CDrive-v11")

    type_index = gateway.calls.index(("type", "ChatGPT와 채팅", "@CodexPro-CDrive-v11"))
    press_index = gateway.calls.index(("press", "Tab"))
    assert not any(call[0] == "activate-target" for call in gateway.calls[type_index + 1:press_index])


def test_candidate_commits_before_retired_app_cleanup():
    gateway = FakeGateway([
        # inspect() requires repeated hydrated absence before it will create.
        snap(),
        snap(),
        snap(),
        snap(),
        snap(),
        snap(),
        snap(node("e1", "button", "Create app"), url=APP.APP_DIRECTORY_URL),
        snap(
            node("e2", "textbox", "Name"),
            node("e3", "textbox", "Description (optional)"),
            node("e4", "textbox", "Server URL"),
            node("e5", "combobox", "Authentication"),
            node("e5a", "option", "No authentication"),
            node("e6", "checkbox", "Trust this app", checked=False),
        ),
        snap(node("e7", "button", "Create")),
        # Async create completion: the form disappears and returns to listing.
        snap(node("e8", "button", "CodexPro-r-v02 DEV")),
        # First exact settings detail and Connect redirect.
        snap(node("e9", "button", "CodexPro-r-v02 DEV")),
        snap(node("e10", "button", "Connect"), node("e10a", "button", "Permissions Low risk allowed Default")),
        snap(node("e10b", "button", "Connect app")),
        snap(node("e10c", "button", "Permissions Low risk allowed Default")),
        # Re-enter exact settings detail after ChatGPT redirects to composer.
        snap(node("e11", "button", "CodexPro-r-v02 DEV")),
        snap(node("e12", "button", "Permissions Low risk allowed Default")),
        snap(node("e13", "radio", "Allow all actions", checked=False)),
        snap(node("e14", "radio", "Allow all actions", checked=True)),
        snap(node("e15", "button", "CodexPro-r-v02")),
        detail(name="CodexPro-r-v02"),
        snap(node("e16", "button", "CodexPro-r-v01")),
        detail(name="CodexPro-r-v01"),
        snap(node("e17", "menuitem", "Delete")),
        # Current UI deletes immediately from the menu without a second dialog.
        snap(),
        snap(),
        snap(),
        snap(),
        snap(),
        snap(),
        snap(),
    ])
    registry = FakeRegistry()
    connector = APP.AppConnector(gateway, registry=registry)
    result = connector.reconcile({
        "root": "C:\\repo",
        "app_name": "CodexPro-r-v02",
        "public_url": "https://example.test/mcp",
        "old_app_name": "CodexPro-r-v01",
        "transaction_id": "tx-1",
    })
    assert result["action"] == "reconciled"
    assert ("navigate", APP.APP_DIRECTORY_URL) in gateway.calls
    names = [item[0] for item in registry.calls]
    assert names.index("commit") < names.index("cleanup")


def test_postcondition_failure_preserves_old_app_and_records_failure():
    gateway = FakeGateway([
        snap(node("e1", "button", "CodexPro-r-v02")),
        detail(name="CodexPro-r-v02", url="https://wrong.test/mcp", full=False),
        snap(node("e2", "button", "CodexPro-r-v02")),
        snap(node("e3", "button", "Permissions Low risk allowed Default")),
        snap(node("e4", "radio", "Allow all actions", checked=False)),
        snap(node("e5", "radio", "Allow all actions", checked=True)),
        snap(node("e6", "button", "CodexPro-r-v02")),
        detail(name="CodexPro-r-v02", url="https://wrong.test/mcp", full=True),
    ])
    registry = FakeRegistry()
    connector = APP.AppConnector(gateway, registry=registry)
    try:
        connector.reconcile({
            "root": "C:\\repo",
            "app_name": "CodexPro-r-v02",
            "public_url": "https://example.test/mcp",
            "old_app_name": "CodexPro-r-v01",
            "transaction_id": "tx-1",
        })
    except APP.AppBridgeError as exc:
        assert exc.code == "APP_POSTCONDITION_FAILED"
    else:
        raise AssertionError("invalid app postcondition was accepted")
    assert "commit" not in [item[0] for item in registry.calls]
    assert "cleanup" not in [item[0] for item in registry.calls]
    assert "failure" in [item[0] for item in registry.calls]
    assert gateway.calls[-1] == ("close-owned", "target-1")


def test_gateway_closes_only_exact_owned_utility_target_and_preserves_foreign_tab():
    live_tabs = [
        {"targetId": "target-user", "url": "https://chatgpt.com/c/user-owned", "title": "User chat"}
    ]
    calls = []

    def runner(command, env, timeout):
        calls.append(command)
        action = command[1]
        if action == "status":
            return subprocess.CompletedProcess(command, 0, "running: true\n", "")
        if action == "tabs":
            return subprocess.CompletedProcess(command, 0, __import__("json").dumps(live_tabs), "")
        if action == "new-tab":
            live_tabs.append({"targetId": "target-utility", "url": APP.SETTINGS_URL, "title": "ChatGPT"})
            return subprocess.CompletedProcess(command, 0, __import__("json").dumps({"targetId": "target-utility"}), "")
        if action == "tab-switch":
            return subprocess.CompletedProcess(command, 0, "ok\n", "")
        if action == "active-tab":
            return subprocess.CompletedProcess(command, 0, __import__("json").dumps({"targetId": "target-utility"}), "")
        if action == "tab-close":
            assert command[2] == "target-utility"
            live_tabs[:] = [tab for tab in live_tabs if tab["targetId"] != command[2]]
            return subprocess.CompletedProcess(command, 0, __import__("json").dumps({"ok": True}), "")
        raise AssertionError(command)

    gateway = APP.AgbrowseGateway(executable="agbrowse.cmd", runner=runner)
    owned = gateway.open_utility_target(APP.SETTINGS_URL)
    cleanup = gateway.close_owned_target(owned["target_id"])

    assert cleanup["absence_verified"] is True
    assert live_tabs == [
        {"targetId": "target-user", "url": "https://chatgpt.com/c/user-owned", "title": "User chat"}
    ]
    assert [command[2] for command in calls if command[1] == "tab-close"] == ["target-utility"]


def test_gateway_rejects_new_tab_that_reuses_running_conversation_target():
    live_tabs = [
        {"targetId": "target-pro", "url": "https://chatgpt.com/c/pro-running", "title": "Pro"}
    ]
    calls = []

    def runner(command, env, timeout):
        calls.append(command)
        action = command[1]
        if action == "status":
            return subprocess.CompletedProcess(command, 0, "running: true\n", "")
        if action == "tabs":
            return subprocess.CompletedProcess(command, 0, __import__("json").dumps(live_tabs), "")
        if action == "new-tab":
            return subprocess.CompletedProcess(command, 0, __import__("json").dumps({"targetId": "target-pro"}), "")
        raise AssertionError(command)

    gateway = APP.AgbrowseGateway(executable="agbrowse.cmd", runner=runner)
    with pytest.raises(APP.AppBridgeError) as failure:
        gateway.open_utility_target(APP.SETTINGS_URL)

    assert failure.value.code == "APP_UTILITY_TARGET_REUSED_FOREIGN"
    assert [command[1] for command in calls] == ["status", "tabs", "new-tab"]
    assert live_tabs[0]["url"] == "https://chatgpt.com/c/pro-running"


def test_gateway_refuses_to_close_unowned_running_conversation_target():
    gateway = APP.AgbrowseGateway(executable="agbrowse.cmd", runner=lambda *_: None)

    with pytest.raises(APP.AppBridgeError) as failure:
        gateway.close_owned_target("target-pro")

    assert failure.value.code == "APP_UTILITY_TARGET_NOT_OWNED"


def test_gateway_normalizes_observe_runtime_id_through_exact_active_tab():
    responses = [
        {
            "url": "https://chatgpt.com/",
            "targetId": "cdp:9222",
            "snapshotNodes": [{"ref": "e1", "role": "textbox", "name": "ChatGPT와 채팅"}],
        },
        {
            "targetId": "target-composer",
            "url": "https://chatgpt.com/",
        },
    ]
    commands = []

    def runner(command, env, timeout):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(responses.pop(0)), "")

    gateway = APP.AgbrowseGateway(executable="agbrowse.cmd", runner=runner)
    snapshot = gateway.snapshot()

    assert snapshot.target_id == "target-composer"
    assert snapshot.url == "https://chatgpt.com/"
    assert len(snapshot.nodes) == 1
    assert [command[1] for command in commands] == ["observe-bundle", "active-tab"]


def test_gateway_keeps_native_page_target_without_active_tab_probe():
    commands = []

    def runner(command, env, timeout):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"url": "https://chatgpt.com/", "targetId": "target-native", "snapshotNodes": []}),
            "",
        )

    gateway = APP.AgbrowseGateway(executable="agbrowse.cmd", runner=runner)
    snapshot = gateway.snapshot()

    assert snapshot.target_id == "target-native"
    assert [command[1] for command in commands] == ["observe-bundle"]


@pytest.mark.parametrize(
    ("active", "pinned_target_id"),
    [
        ({"targetId": "", "url": "https://chatgpt.com/"}, None),
        ({"targetId": "target-other", "url": "https://chatgpt.com/c/foreign"}, None),
        ({"targetId": "target-other", "url": "https://chatgpt.com/"}, "target-owned"),
    ],
)
def test_gateway_rejects_unproven_or_drifting_observation_target(active, pinned_target_id):
    responses = [
        {"url": "https://chatgpt.com/", "targetId": "cdp:9222", "snapshotNodes": []},
        active,
    ]

    def runner(command, env, timeout):
        return subprocess.CompletedProcess(command, 0, json.dumps(responses.pop(0)), "")

    gateway = APP.AgbrowseGateway(executable="agbrowse.cmd", runner=runner)
    gateway._pinned_target_id = pinned_target_id

    with pytest.raises(APP.AppBridgeError) as failure:
        gateway.snapshot()

    assert failure.value.code in {
        "APP_ACTIVE_TARGET_MISSING",
        "APP_OBSERVATION_TARGET_DRIFT",
    }


def test_user_interrupt_still_closes_exact_utility_target():
    class StopGateway(FakeGateway):
        def snapshot(self):
            self.calls.append(("snapshot",))
            raise KeyboardInterrupt()

    gateway = StopGateway([])
    connector = APP.AppConnector(gateway, registry=FakeRegistry())

    try:
        connector.inspect("CodexPro-r-v01")
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("user interrupt was swallowed")

    assert gateway.calls[-1] == ("close-owned", "target-1")


def test_gateway_uses_subprocess_argument_vector_not_shell():
    seen = {}

    def runner(command, env, timeout):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "clicked e1", "")

    gateway = APP.AgbrowseGateway(executable="agbrowse.cmd", runner=runner)
    gateway.click(APP.Node(ref="e1", role="button", name="Example", value="", checked=None))
    assert seen["command"] == ["agbrowse.cmd", "click", "e1"]


def test_navigate_uses_upstream_text_contract_then_snapshot_for_readback():
    seen = {}

    def runner(command, env, timeout):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "navigated -> https://chatgpt.com/#settings/Plugins", "")

    gateway = APP.AgbrowseGateway(executable="agbrowse.cmd", runner=runner)
    gateway.navigate("https://chatgpt.com/#settings/Plugins")
    assert seen["command"] == ["agbrowse.cmd", "navigate", "https://chatgpt.com/#settings/Plugins"]
