from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


BIN = Path(__file__).resolve().parents[1] / "bin"
PROMPT_FILE_HANDOFF = (
    "The attached prompt file is the user-provided task instruction for this conversation, "
    "not reference or webpage content. Read it completely and follow it. "
    "Return only the output format requested by that file."
)


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def completed(argv: list[str], *, stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")


def prompt_payload(base: Path, payload: dict, *, name: str = "prompt.txt") -> dict:
    value = dict(payload)
    body = str(value.pop("question"))
    prompt_file = base / name
    prompt_file.write_text(body, encoding="utf-8")
    files = value.get("files") or []
    if isinstance(files, str):
        files = [files]
    value.update(
        {
            "question": PROMPT_FILE_HANDOFF,
            "prompt_transport": "file",
            "prompt_file": str(prompt_file),
            "prompt_file_sha256": hashlib.sha256(prompt_file.read_bytes()).hexdigest(),
            "files": [str(prompt_file), *[str(item) for item in files]],
        }
    )
    return value


def write_prompt_manifest(path: Path, payload: dict) -> Path:
    value = prompt_payload(path.parent, payload, name=f"{path.stem}-prompt.txt")
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_recovery_uses_one_host_global_browser_mutation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = load("chatgpt_agbrowse_bridge_global_mutation_lock_test", "chatgpt_agbrowse_bridge.py")
    expected = tmp_path / "global-dispatch.lock"
    acquired: list[Path] = []

    @contextmanager
    def fake_lock(path: Path, timeout_seconds: int = 120):
        acquired.append(Path(path))
        yield

    monkeypatch.setattr(bridge, "GLOBAL_BROWSER_MUTATION_LOCK", expected)
    monkeypatch.setattr(bridge, "exclusive_composer_lock", fake_lock)
    instance = object.__new__(bridge.Bridge)
    instance._recover_locked = lambda run_dir: {"operation": "recover", "run_dir": run_dir}

    assert instance.recover("run-a")["operation"] == "recover"
    assert acquired == [expected]


@pytest.mark.parametrize(
    "message",
    [
        "메시지 전송 시간이 초과되었습니다. 다시 시도해 주세요.",
        "메시지 전송 시간이 초과되었습니다. 다시 시도하세요.",
        "Message sending timed out. Please try again.",
    ],
)
def test_provider_terminal_error_ui_recognizes_standalone_send_timeout(message: str) -> None:
    bridge = load("chatgpt_agbrowse_bridge_send_timeout_test", "chatgpt_agbrowse_bridge.py")

    evidence = bridge.provider_terminal_error_ui(message)

    assert evidence is not None
    assert evidence["signature"] == "chatgpt-send-timeout-v1"
    assert evidence["error_label"] == message


def test_provider_terminal_error_ui_does_not_match_timeout_phrase_inside_real_answer() -> None:
    bridge = load("chatgpt_agbrowse_bridge_send_timeout_prose_test", "chatgpt_agbrowse_bridge.py")
    answer = (
        "실패 복구 설계에서는 아래 문구가 나타날 수 있습니다.\n"
        "메시지 전송 시간이 초과되었습니다. 다시 시도해 주세요.\n"
        "이 문구를 기록하고 동일 실행을 복구해야 합니다."
    )

    assert bridge.provider_terminal_error_ui(answer) is None


def test_app_gateway_starts_browser_before_navigation() -> None:
    app = load("codexpro_agbrowse_app_start_test", "codexpro_agbrowse_app.py")
    calls: list[list[str]] = []
    statuses = iter(["running: false\n", "running: true\n", "running: true\n"])

    def runner(argv, env, timeout):
        calls.append(argv)
        if argv[1] == "status":
            return completed(argv, stdout=next(statuses))
        if argv[1] == "tabs":
            return completed(argv, stdout="[]")
        return completed(argv, stdout="started\n")

    gateway = app.AgbrowseGateway(runner=runner)
    gateway.ensure_started()

    assert [argv[1] for argv in calls].count("start") == 1
    assert calls[1] == ["agbrowse", "start", "--headed"]


def test_app_gateway_does_not_restart_running_browser() -> None:
    app = load("codexpro_agbrowse_app_running_test", "codexpro_agbrowse_app.py")
    calls: list[list[str]] = []

    def runner(argv, env, timeout):
        calls.append(argv)
        return completed(argv, stdout="running: true\n")

    gateway = app.AgbrowseGateway(runner=runner)
    gateway.ensure_started()
    gateway.ensure_started()

    assert [argv[1] for argv in calls] == ["status"]


def test_app_gateway_retries_transient_start_command_failure() -> None:
    app = load("codexpro_agbrowse_app_start_retry_test", "codexpro_agbrowse_app.py")
    calls: list[list[str]] = []
    statuses = iter(["running: false\n", "running: false\n", "running: true\n"])
    starts = 0

    def runner(argv, env, timeout):
        nonlocal starts
        calls.append(argv)
        if argv[1] == "status":
            return completed(argv, stdout=next(statuses))
        if argv[1] == "start":
            starts += 1
            if starts == 1:
                return completed(argv, returncode=1)
            return completed(argv, stdout="started\n")
        if argv[1] == "tabs":
            return completed(argv, stdout="[]")
        return completed(argv)

    monkeypatch_sleep = app.time.sleep
    app.time.sleep = lambda _seconds: None
    try:
        gateway = app.AgbrowseGateway(runner=runner)
        gateway.ensure_started(timeout_seconds=1)
    finally:
        app.time.sleep = monkeypatch_sleep

    assert starts == 2
    assert [argv[1] for argv in calls].count("start") == 2


def test_app_settings_waits_until_exact_route_is_observed() -> None:
    app = load("codexpro_agbrowse_app_route_wait_test", "codexpro_agbrowse_app.py")

    class UI:
        snapshots = 0
        settled = 0
        navigated = None

        def ensure_started(self):
            return None

        def open_utility_target(self, url):
            assert url == app.SETTINGS_URL
            return {"target_id": "T-APP"}

        def close_owned_target(self, target_id):
            assert target_id == "T-APP"
            return {"ok": True, "target_id": target_id, "absence_verified": True}

        def activate_target(self, target_id):
            assert target_id == "T-APP"
            return {"ok": True, "target_id": target_id}

        def navigate(self, url):
            self.navigated = url

        def settle(self):
            self.settled += 1

        def snapshot(self):
            self.snapshots += 1
            url = "https://chatgpt.com/" if self.snapshots == 1 else app.SETTINGS_URL
            return app.Snapshot({"url": url, "targetId": "T-SETTINGS", "refs": []})

    ui = UI()
    connector = app.AppConnector(ui)
    page = connector._settings()

    assert page.url == app.SETTINGS_URL
    assert ui.navigated == app.SETTINGS_URL
    assert ui.snapshots == 2
    assert ui.settled == 2


def test_app_inspect_understands_plugin_actions_and_combined_permission_button() -> None:
    app = load("codexpro_agbrowse_app_new_ui_test", "codexpro_agbrowse_app.py")

    class UI:
        index = 0
        clicks: list[str] = []

        def ensure_started(self):
            return None

        def open_utility_target(self, url):
            assert url == app.SETTINGS_URL
            return {"target_id": "T-APP"}

        def close_owned_target(self, target_id):
            assert target_id == "T-APP"
            return {"ok": True, "target_id": target_id, "absence_verified": True}

        def activate_target(self, target_id):
            assert target_id == "T-APP"
            return {"ok": True, "target_id": target_id}

        def navigate(self, url):
            return None

        def settle(self):
            return None

        def snapshot(self):
            pages = [
                {
                    "url": app.SETTINGS_URL,
                    "targetId": "T-APP",
                    "refs": [{"ref": "app", "role": "button", "name": "CodexPro-CDrive-v11 모두 허용"}],
                },
                {
                    "url": app.SETTINGS_URL + "/plugin_asdk_app_test",
                    "targetId": "T-APP",
                    "refs": [
                        {"ref": "actions", "role": "button", "name": "플러그인 작업"},
                        {
                            "ref": "permission",
                            "role": "button",
                            "name": "권한 이 플러그인을 사용할 때 ChatGPT가 언제 권한을 요청할지 선택하세요. 모든 액션 허용 위험도 높음",
                        },
                    ],
                },
                {
                    "url": app.SETTINGS_URL + "/plugin_asdk_app_test",
                    "targetId": "T-APP",
                    "refs": [{"ref": "disconnect", "role": "menuitem", "name": "연결 해제"}],
                },
            ]
            page = pages[self.index]
            self.index += 1
            return app.Snapshot(page)

        def click(self, node):
            self.clicks.append(node.ref)

    ui = UI()
    result = app.AppConnector(ui).inspect("CodexPro-CDrive-v11")

    assert result["state"] == "detail"
    assert result["connected"] is True
    assert result["full_access"] is True
    assert ui.clicks == ["app"]


def test_app_inspect_waits_for_installed_plugin_list_hydration() -> None:
    app = load("codexpro_agbrowse_app_hydration_test", "codexpro_agbrowse_app.py")

    class UI:
        index = 0
        settled = 0

        def ensure_started(self):
            return None

        def open_utility_target(self, url):
            assert url == app.SETTINGS_URL
            return {"target_id": "T-APP"}

        def close_owned_target(self, target_id):
            assert target_id == "T-APP"
            return {"ok": True, "target_id": target_id, "absence_verified": True}

        def activate_target(self, target_id):
            assert target_id == "T-APP"
            return {"ok": True, "target_id": target_id}

        def navigate(self, url):
            return None

        def settle(self):
            self.settled += 1

        def snapshot(self):
            pages = [
                {"url": app.SETTINGS_URL, "targetId": "T-APP", "refs": []},
                {
                    "url": app.SETTINGS_URL,
                    "targetId": "T-APP",
                    "refs": [{"ref": "app", "role": "button", "name": "CodexPro-CDrive-v11 모두 허용"}],
                },
                {
                    "url": app.SETTINGS_URL + "/plugin_asdk_app_test",
                    "targetId": "T-APP",
                    "refs": [
                        {"ref": "disconnect", "role": "button", "name": "연결 해제"},
                        {"ref": "permission", "role": "button", "name": "모든 액션 허용"},
                    ],
                },
            ]
            page = pages[self.index]
            self.index += 1
            return app.Snapshot(page)

        def click(self, node):
            return None

    ui = UI()
    result = app.AppConnector(ui).inspect("CodexPro-CDrive-v11")

    assert result["state"] == "detail"
    assert result["connected"] is True
    assert result["full_access"] is True
    assert ui.settled >= 3


def test_exact_prepared_target_is_reactivated_and_verified() -> None:
    app = load("codexpro_agbrowse_app_target_test", "codexpro_agbrowse_app.py")
    calls: list[list[str]] = []

    def runner(argv, env, timeout):
        calls.append(argv)
        if argv[1] == "active-tab":
            return completed(argv, stdout=json.dumps({"targetId": "T-PREPARED"}))
        return completed(argv, stdout="ok\n")

    gateway = app.AgbrowseGateway(runner=runner)
    result = gateway.activate_target("T-PREPARED")

    assert result["target_id"] == "T-PREPARED"
    assert [argv[1] for argv in calls] == ["active-tab"]


def test_exact_prepared_target_switches_only_after_active_target_mismatch() -> None:
    app = load("codexpro_agbrowse_app_target_fallback_test", "codexpro_agbrowse_app.py")
    calls: list[list[str]] = []
    active_reads = iter(["T-OTHER", "T-PREPARED"])

    def runner(argv, env, timeout):
        calls.append(argv)
        if argv[1] == "active-tab":
            return completed(argv, stdout=json.dumps({"targetId": next(active_reads)}))
        return completed(argv, stdout="ok\n")

    gateway = app.AgbrowseGateway(runner=runner)
    result = gateway.activate_target("T-PREPARED")

    assert result["target_id"] == "T-PREPARED"
    assert [argv[1] for argv in calls] == ["active-tab", "tab-switch", "active-tab"]


def test_warm_composer_fast_path_uses_five_agbrowse_commands_without_tab_switch() -> None:
    app = load("codexpro_agbrowse_app_composer_fast_path_test", "codexpro_agbrowse_app.py")
    calls: list[list[str]] = []

    def runner(argv, env, timeout):
        calls.append(argv)
        if argv[1] == "tabs":
            return completed(argv, stdout="[]")
        if argv[1] == "new-tab":
            return completed(argv, stdout=json.dumps({"targetId": "T-COMPOSER"}))
        if argv[1] == "active-tab":
            return completed(argv, stdout=json.dumps({"targetId": "T-COMPOSER"}))
        if argv[1] == "observe-bundle":
            return completed(
                argv,
                stdout=json.dumps(
                    {
                        "url": "https://chatgpt.com/",
                        "targetId": "T-COMPOSER",
                        "snapshotNodes": [
                            {
                                "ref": "e1",
                                "role": "textbox",
                                "name": "ChatGPT와 채팅",
                            }
                        ],
                    },
                ),
            )
        return completed(argv, stdout="ok\n")

    gateway = app.AgbrowseGateway(runner=runner)
    gateway._browser_ready = True
    result = app.AppConnector(gateway).prepare_composer_app("CodexPro-CDrive-v14")

    assert result["agbrowse_command_count"] == 6
    assert result["duration_ms"] < 1000
    assert [argv[1] for argv in calls] == ["tabs", "new-tab", "active-tab", "observe-bundle", "type", "press"]


def test_fresh_browser_closes_only_owned_blank_startup_before_composer_new_tab() -> None:
    app = load("codexpro_agbrowse_app_startup_target_test", "codexpro_agbrowse_app.py")
    calls: list[list[str]] = []
    tabs = [
        {"targetId": "T-START", "url": "about:blank"},
        {"targetId": "T-KEEPALIVE", "url": "https://example.com/"},
    ]

    def runner(argv, env, timeout):
        calls.append(argv)
        if argv[1] == "tabs":
            return completed(argv, stdout=json.dumps(tabs))
        if argv[1] == "tab-close":
            assert argv[2] == "T-START"
            tabs[:] = [tab for tab in tabs if tab["targetId"] != "T-START"]
            return completed(argv, stdout=json.dumps({"ok": True}))
        if argv[1] == "new-tab":
            tabs.append({"targetId": "T-COMPOSER", "url": "https://chatgpt.com/"})
            return completed(argv, stdout=json.dumps({"targetId": "T-COMPOSER"}))
        raise AssertionError(f"unexpected command: {argv}")

    gateway = app.AgbrowseGateway(runner=runner)
    gateway._browser_ready = True
    gateway._owned_startup_targets["T-START"] = "about:blank"

    created = gateway.open_composer_target("https://chatgpt.com/")

    assert created["targetId"] == "T-COMPOSER"
    assert created["newTargetProven"] is True
    assert gateway._pinned_target_id == "T-COMPOSER"
    assert [argv[1] for argv in calls] == ["tabs", "tab-close", "tabs", "new-tab"]


def test_sole_owned_startup_target_is_promoted_only_after_exact_url_readback() -> None:
    app = load("codexpro_agbrowse_app_startup_promotion_test", "codexpro_agbrowse_app.py")
    calls: list[list[str]] = []
    tabs = [{"targetId": "T-START", "url": "about:blank"}]

    def runner(argv, env, timeout):
        calls.append(argv)
        if argv[1] == "tabs":
            return completed(argv, stdout=json.dumps(tabs))
        if argv[1] == "new-tab":
            tabs[0]["url"] = "https://chatgpt.com/"
            return completed(argv, stdout=json.dumps({"targetId": "T-START"}))
        raise AssertionError(f"unexpected command: {argv}")

    gateway = app.AgbrowseGateway(runner=runner)
    gateway._browser_ready = True
    gateway._owned_startup_targets["T-START"] = "about:blank"

    created = gateway.open_composer_target("https://chatgpt.com/")

    assert created["targetId"] == "T-START"
    assert created["newTargetProven"] is True
    assert created["startupTargetPromoted"] is True
    assert created["promotionUrlVerified"] is True
    assert not any(argv[1] == "tab-close" for argv in calls)
    assert [argv[1] for argv in calls] == ["tabs", "new-tab", "tabs"]


def test_startup_cleanup_never_closes_unowned_or_navigated_tabs() -> None:
    app = load("codexpro_agbrowse_app_startup_foreign_test", "codexpro_agbrowse_app.py")
    calls: list[list[str]] = []
    tabs = [
        {"targetId": "T-FOREIGN-BLANK", "url": "about:blank"},
        {"targetId": "T-START-NAVIGATED", "url": "https://chatgpt.com/"},
    ]

    def runner(argv, env, timeout):
        calls.append(argv)
        if argv[1] == "tabs":
            return completed(argv, stdout=json.dumps(tabs))
        if argv[1] == "new-tab":
            return completed(argv, stdout=json.dumps({"targetId": "T-NEW"}))
        raise AssertionError(f"unexpected command: {argv}")

    gateway = app.AgbrowseGateway(runner=runner)
    gateway._browser_ready = True
    gateway._owned_startup_targets["T-START-NAVIGATED"] = "about:blank"

    created = gateway.open_composer_target("https://chatgpt.com/")

    assert created["targetId"] == "T-NEW"
    assert not any(argv[1] == "tab-close" for argv in calls)


def test_connected_auto_prepares_fresh_chat_target_without_app_pill() -> None:
    app = load("codexpro_agbrowse_app_connected_chat_test", "codexpro_agbrowse_app.py")

    class UI:
        snapshots = 0
        clicks: list[str] = []

        def ensure_started(self):
            return None

        def new_tab(self, url):
            assert url == app.COMPOSER_URL
            return {"targetId": "T-CONNECTED"}

        def settle(self):
            return None

        def snapshot(self):
            self.snapshots += 1
            refs = [
                {"ref": "chat", "role": "radio", "name": "Chat"},
                {"ref": "box", "role": "textbox", "name": "ChatGPT와 채팅"},
            ]
            return app.Snapshot({"url": app.COMPOSER_URL, "targetId": "T-CONNECTED", "refs": refs})

        def click(self, node):
            self.clicks.append(node.ref)

    ui = UI()
    result = app.AppConnector(ui).prepare_connected_app_chat("CodexPro-CDrive-v11")

    assert result["state"] == "connected-app-chat-ready"
    assert result["target_id"] == "T-CONNECTED"
    assert ui.clicks == ["chat"]
    assert ui.snapshots == 2


def test_expected_app_url_resolves_exact_project_before_drive_scope(tmp_path: Path) -> None:
    app = load("codexpro_agbrowse_app_registry_scope_test", "codexpro_agbrowse_app.py")
    project = tmp_path / "project"
    project.mkdir()

    class Registry:
        @staticmethod
        def load_registry():
            return {
                "projects": {
                    str(project): {
                        "app_name": "CodexPro-Test",
                        "status": "active",
                        "public_url": "https://exact.test/mcp?codexpro_token=exact",
                    },
                    str(Path(project.anchor).resolve()): {
                        "app_name": "CodexPro-Test",
                        "status": "active",
                        "public_url": "https://drive.test/mcp?codexpro_token=drive",
                    },
                }
            }

    connector = app.AppConnector(object(), registry=Registry)

    assert connector.expected_url_for_scope("CodexPro-Test", str(project)).startswith("https://exact.test/")


def test_sensitive_app_urls_are_redacted_but_hashed() -> None:
    bridge = load("chatgpt_agbrowse_bridge_redact_test", "chatgpt_agbrowse_bridge.py")
    secret_url = "https://example.test/mcp?codexpro_token=do-not-store"
    clean = bridge.sanitize_evidence({"url": secret_url, "detail": f"failed at {secret_url}"})

    serialized = json.dumps(clean)
    assert "do-not-store" not in serialized
    assert clean["url"].endswith("?<redacted>")
    assert clean["url_sha256"] == bridge.sha256_bytes(secret_url.encode("utf-8"))


def test_every_fresh_parallel_send_has_explicit_blank_chatgpt_url(tmp_path: Path) -> None:
    bridge = load("chatgpt_agbrowse_bridge_blank_url_test", "chatgpt_agbrowse_bridge.py")
    command = bridge.build_send_command(
        {"requested": {"app_policy": "forbidden"}},
        prompt_payload(tmp_path, {"question": "smoke", "mode_label": "Pro"}),
        "agbrowse",
    )

    assert command[command.index("--url") + 1] == "https://chatgpt.com/"
    assert "--parallel" in command


def test_proven_plain_composer_bypasses_parallel_pool_cleanup(tmp_path: Path) -> None:
    bridge = load("chatgpt_agbrowse_bridge_plain_composer_test", "chatgpt_agbrowse_bridge.py")
    command = bridge.build_send_command(
        {"requested": {"app_policy": "forbidden"}},
        prompt_payload(tmp_path, {"question": "smoke", "mode_label": "Pro"}),
        "agbrowse",
        prepared_target=True,
    )

    assert "--reuse-tab" in command
    assert "--parallel" not in command


def test_explicit_preselected_app_uses_exact_tab_without_plugin_flag(tmp_path: Path) -> None:
    bridge = load("chatgpt_agbrowse_bridge_connected_auto_test", "chatgpt_agbrowse_bridge.py")
    command = bridge.build_send_command(
        {"requested": {"app_policy": "required"}},
        prompt_payload(tmp_path, {
            "question": "use CodexPro-CDrive-v11 and call codexpro_self_test",
            "mode_label": "GPT-5.6",
            "chatgpt_app_name": "CodexPro-CDrive-v11",
        }),
        "agbrowse",
        preselected_app=True,
    )

    assert "--reuse-tab" in command
    assert "--parallel" not in command
    assert "--plugin" not in command


def test_canonical_conversation_url_cannot_be_owned_by_two_projects(tmp_path: Path) -> None:
    bridge = load("chatgpt_agbrowse_bridge_url_owner_test", "chatgpt_agbrowse_bridge.py")
    runtime = bridge.Bridge(state_root=tmp_path / "state")
    runs = []
    for index in (1, 2):
        project = tmp_path / f"project-{index}"
        project.mkdir()
        manifest_path = project / "manifest.json"
        manifest_path.write_text(
            json.dumps({"question": f"smoke-{index}", "mode_label": "Pro", "app_policy": "forbidden"}),
            encoding="utf-8",
        )
        record = runtime.store.create_run(
            project_root=str(project),
            manifest_path=str(manifest_path),
            agbrowse_contract={"executable": "agbrowse"},
        )
        run_dir = str(record["run_dir"])
        runtime.store.transition(run_dir, "PREFLIGHTED")
        runtime.store.transition(run_dir, "LEASED")
        runtime.store.transition(run_dir, "SEND_STARTED")
        runtime.store.transition(run_dir, "SUBMITTED", session_id=f"S-{index}", target_id=f"T-{index}")
        runs.append(run_dir)

    first = runtime._bind_conversation_url(
        runs[0],
        conversation_url="https://chatgpt.com/c/shared-id",
        target_id="T-1",
    )
    second = runtime._bind_conversation_url(
        runs[1],
        conversation_url="https://chatgpt.com/c/shared-id",
        target_id="T-2",
    )

    assert first["phase"] == "URL_BOUND"
    assert second["phase"] == "BLOCKED_TARGET_AMBIGUOUS"
    assert second["terminal_block_code"] == "CONVERSATION_URL_OWNED_BY_FOREIGN_RUN"
    assert second["conversation_url"] is None


def test_stale_same_project_duplicate_complete_owner_is_settled_without_losing_answers(tmp_path: Path) -> None:
    bridge = load("chatgpt_agbrowse_bridge_duplicate_complete_settle_test", "chatgpt_agbrowse_bridge.py")
    runtime = bridge.Bridge(state_root=tmp_path / "state")
    project = tmp_path / "project"
    project.mkdir()
    manifest_path = project / "manifest.json"
    manifest_path.write_text(
        json.dumps({"question": "same packet", "mode_label": "Pro", "app_policy": "forbidden"}),
        encoding="utf-8",
    )

    authoritative = runtime.store.create_run(
        project_root=str(project),
        manifest_path=str(manifest_path),
        agbrowse_contract={"executable": "agbrowse"},
    )
    authoritative_dir = Path(authoritative["run_dir"])
    runtime.store.transition(authoritative_dir, "PREFLIGHTED")
    runtime.store.transition(authoritative_dir, "LEASED")
    runtime.store.transition(authoritative_dir, "SEND_STARTED")
    runtime.store.transition(
        authoritative_dir,
        "SUBMITTED",
        session_id="session-authoritative",
        target_id="target-authoritative",
    )
    runtime.store.transition(
        authoritative_dir,
        "URL_BOUND",
        conversation_url="https://chatgpt.com/c/shared-complete",
    )
    answer_path = authoritative_dir / "answer.md"
    answer_path.write_text("preserved complete answer\n", encoding="utf-8")
    runtime.store.transition(
        authoritative_dir,
        "RESULT_CAPTURED",
        result={
            "path": str(answer_path),
            "sha256": hashlib.sha256(answer_path.read_bytes()).hexdigest(),
            "bytes": answer_path.stat().st_size,
            "provider_status": "complete",
            "evidence": {"status_sha256": "a" * 64},
        },
    )
    runtime.store.transition(authoritative_dir, "VERIFIED")
    runtime.store.transition(authoritative_dir, "COMPLETE")

    stale = runtime.store.create_run(
        project_root=str(project),
        manifest_path=str(manifest_path),
        agbrowse_contract={"executable": "agbrowse"},
        owner_pid=2_147_483_647,
    )
    stale_dir = Path(stale["run_dir"])
    runtime.store.transition(stale_dir, "PREFLIGHTED")
    runtime.store.transition(stale_dir, "LEASED")
    runtime.store.transition(stale_dir, "SEND_STARTED")
    runtime.store.transition(
        stale_dir,
        "SUBMITTED",
        session_id="session-stale",
        target_id="target-stale",
    )
    blocked = runtime._bind_conversation_url(
        str(stale_dir),
        conversation_url="https://chatgpt.com/c/shared-complete",
        target_id="target-stale",
        recovery_event={"kind": "doctor-reattach", "session_id": "session-stale"},
    )
    assert blocked["phase"] == "BLOCKED_TARGET_AMBIGUOUS"

    diagnosis = runtime.store.reconcile_project_lock(project, apply_safe_pre_submission=False)
    settled = runtime.store.reconcile_project_lock(project, apply_safe_pre_submission=True)
    _, final_stale = runtime.store.load(stale_dir)
    _, final_authoritative = runtime.store.load(authoritative_dir)

    assert diagnosis["state"] == "STALE_DUPLICATE_COMPLETE_OWNER_SAFE_TO_SETTLE"
    assert settled["state"] == "STALE_DUPLICATE_COMPLETE_OWNER_SETTLED"
    assert final_stale["phase"] == "COMPLETE_SUPERSEDED"
    assert final_stale["superseded_complete"]["authoritative_run_id"] == authoritative["run_id"]
    assert final_authoritative["phase"] == "COMPLETE"
    assert Path(final_authoritative["result"]["path"]).read_text(encoding="utf-8") == "preserved complete answer\n"
    assert not (stale_dir.parent.parent / "active.lock").exists()


def test_stderr_mutation_disallowed_json_is_send_rejected(tmp_path: Path) -> None:
    bridge = load("chatgpt_agbrowse_bridge_stderr_rejection_test", "chatgpt_agbrowse_bridge.py")
    manifest = tmp_path / "manifest.json"
    write_prompt_manifest(
        manifest,
        {"question": "smoke", "mode_label": "Pro", "app_policy": "forbidden"},
    )
    project = tmp_path / "project"
    project.mkdir()
    error_payload = {
        "ok": False,
        "status": "error",
        "error": {
            "errorCode": "capability.unsupported",
            "stage": "provider-surface-preflight",
            "message": "Chat commands are not supported on the Work surface",
            "mutationAllowed": False,
        },
    }

    def runner(argv, env, timeout):
        if argv[1:] == ["tabs", "--json"]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr=json.dumps(error_payload))

    runtime = bridge.Bridge(state_root=tmp_path / "state", runner=runner, headed_runtime_preflight=False)
    record = runtime.store.create_run(
        project_root=str(project),
        manifest_path=str(manifest),
        agbrowse_contract={"executable": "agbrowse"},
    )
    run_dir = str(record["run_dir"])
    runtime.store.transition(run_dir, "PREFLIGHTED")

    result = runtime.send(run_dir)

    assert result["phase"] == "SEND_REJECTED"
    assert result["session_id"] is None
    assert result["conversation_url"] is None
    assert result["terminal_block_code"] is None


def test_session_store_lock_is_verified_pre_submit_rejection() -> None:
    bridge = load("chatgpt_agbrowse_bridge_session_store_lock_test", "chatgpt_agbrowse_bridge.py")
    envelope = bridge.normalize_envelope(
        {
            "ok": False,
            "status": "error",
            "error": {
                "errorCode": "internal.unhandled",
                "stage": "internal",
                "message": (
                    "web-ai session store: failed to acquire lock at "
                    r"C:\Users\Example\.browser-agent\web-ai-sessions.json.lock after 200 attempts"
                ),
                "mutationAllowed": False,
            },
        }
    )

    assert bridge.classify_pre_submit_failure(envelope) == "SEND_REJECTED"
    envelope["mutation_allowed"] = True
    assert bridge.classify_pre_submit_failure(envelope) == "SUBMISSION_UNCERTAIN_IDENTITY_MISSING"


def test_internal_reasoning_picker_timeout_with_no_mutation_is_send_rejected() -> None:
    bridge = load("chatgpt_agbrowse_bridge_picker_timeout_test", "chatgpt_agbrowse_bridge.py")
    envelope = {
        "error_code": "internal.unhandled",
        "error_stage": "internal",
        "mutation_allowed": False,
        "message": "locator.click: Timeout 5000ms exceeded while selecting reasoning",
    }

    assert bridge.classify_pre_submit_failure(envelope) == "SEND_REJECTED"


def test_mutation_disallowed_unprepared_send_closes_only_unique_new_root(tmp_path: Path) -> None:
    bridge = load("chatgpt_agbrowse_bridge_unprepared_root_cleanup_test", "chatgpt_agbrowse_bridge.py")
    project = tmp_path / "project"
    project.mkdir()
    manifest = tmp_path / "manifest.json"
    write_prompt_manifest(manifest, {"question": "smoke", "mode_label": "Pro", "app_policy": "forbidden"})
    tabs = [
        {"targetId": "FOREIGN", "url": "https://chatgpt.com/c/foreign", "type": "page"},
        {"targetId": "NEW-ROOT", "url": "https://chatgpt.com/", "type": "page"},
    ]

    def runner(argv, env, timeout):
        if argv[1:] == ["tabs", "--json"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(tabs), "")
        if argv[1] == "tab-close":
            assert argv[2] == "NEW-ROOT"
            tabs[:] = [tab for tab in tabs if tab["targetId"] != "NEW-ROOT"]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), "")
        raise AssertionError(argv)

    runtime = bridge.Bridge(state_root=tmp_path / "state", runner=runner, headed_runtime_preflight=False)
    record = runtime.store.create_run(
        project_root=str(project), manifest_path=str(manifest), agbrowse_contract={"executable": "agbrowse"}
    )
    run_dir = str(record["run_dir"])
    runtime.store.transition(run_dir, "PREFLIGHTED")
    runtime.store.transition(run_dir, "LEASED")
    runtime.store.transition(run_dir, "SEND_STARTED")
    rejected = runtime.store.transition(run_dir, "SEND_REJECTED")

    cleanup = runtime._cleanup_new_root_after_unprepared_rejection(
        run_dir,
        rejected,
        pre_send_tabs=[tabs[0]],
    )

    assert cleanup is not None
    assert cleanup["state"] == "closed-and-absent"
    assert [tab["targetId"] for tab in tabs] == ["FOREIGN"]


def test_stop_confirmation_modal_requires_one_exact_confirm_ref() -> None:
    bridge = load("chatgpt_agbrowse_bridge_stop_modal_ref_test", "chatgpt_agbrowse_bridge.py")
    snapshot = {
        "textSummary": "응답 생성을 중지할까요?",
        "snapshotNodes": [
            {"ref": "cancel", "role": "button", "name": "취소"},
            {"ref": "stop", "role": "button", "name": "중지"},
        ],
    }

    assert bridge._stop_confirmation_ref(snapshot) == "stop"
    assert bridge._stop_confirmation_ref({"snapshotNodes": []}) is None


def test_blocked_interrupt_envelope_is_not_pre_submit_quiescence(tmp_path: Path) -> None:
    bridge = load("chatgpt_agbrowse_bridge_blocked_stop_not_quiescent_test", "chatgpt_agbrowse_bridge.py")
    session = {
        "sessionId": "S-1",
        "targetId": "T-1",
        "conversationUrl": "https://chatgpt.com/",
        "status": "sent",
        "answer": None,
        "tabId": None,
        "trace": [],
        "envelopeSummary": {"assistantCount": 0},
    }

    def runner(argv, env, timeout):
        if argv[1:4] == ["web-ai", "sessions", "list"]:
            payload = {"sessions": [session]}
        elif argv[1:3] == ["web-ai", "stop"]:
            payload = {
                "ok": True,
                "status": "blocked",
                "interrupt": True,
                "sessionId": "S-1",
                "targetId": "T-1",
                "url": "https://chatgpt.com/",
            }
        elif argv[1:4] == ["web-ai", "sessions", "show"]:
            payload = {"session": session}
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    runtime = bridge.Bridge(state_root=tmp_path / "state", runner=runner, headed_runtime_preflight=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(bridge.BridgeError) as failure:
        runtime._adjudicate_pre_submit_session_artifact(
            run_dir=run_dir,
            executable="agbrowse",
            manifest={},
            target_id="T-1",
        )

    assert failure.value.code == "PRE_SUBMIT_RETRY_SESSION_NOT_QUIESCENT"


def test_web_multi_answer_extractor_requires_complete_payload() -> None:
    bridge = load("chatgpt_agbrowse_bridge_web_multi_extract_test", "chatgpt_agbrowse_bridge.py")
    payload = (
        "navigation text\n"
        "<<<WEB_MULTI_HEADER_V1>>>\n"
        '{"stage_id":"solver-3"}\n'
        "answer body\n"
        "<<<END_WEB_MULTI_PAYLOAD_V1>>>\n"
        "composer footer"
    )

    assert bridge._web_multi_assistant_answer(payload) == (
        "<<<WEB_MULTI_HEADER_V1>>>\n"
        '{"stage_id":"solver-3"}\n'
        "answer body\n"
        "<<<END_WEB_MULTI_PAYLOAD_V1>>>"
    )
    assert bridge._web_multi_assistant_answer(payload.replace("<<<END_WEB_MULTI_PAYLOAD_V1>>>", "")) is None


def test_history_session_identity_uses_exact_run_owned_prompt_alias(tmp_path: Path) -> None:
    bridge = load("chatgpt_agbrowse_bridge_history_session_identity_test", "chatgpt_agbrowse_bridge.py")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    prompt_alias = run_dir / "prompt-run.txt"
    prompt_alias.write_text("task", encoding="utf-8")
    payload = {
        "ok": True,
        "sessions": [
            {
                "sessionId": "S-FOREIGN",
                "targetId": "T-FOREIGN",
                "envelopeSummary": {"filePath": str(run_dir / "prompt-other.txt")},
            },
            {
                "sessionId": "S-RUN",
                "targetId": "T-RUN",
                "status": "sent",
                "envelopeSummary": {"filePath": str(prompt_alias)},
            },
        ],
    }

    runtime = bridge.Bridge(
        state_root=tmp_path / "state",
        runner=lambda argv, env, timeout: completed(argv, stdout=json.dumps(payload)),
    )
    session_id, evidence = runtime._recover_session_identity_from_store(
        run_dir=run_dir,
        executable="agbrowse",
        manifest={},
        record={"recovery_identity": {"attachment_path": str(prompt_alias)}},
    )

    assert session_id == "S-RUN"
    assert evidence["target_id"] == "T-RUN"
    assert Path(evidence["evidence"]["stdout"]).is_file()


def test_uncertain_without_identity_reclassifies_from_verified_stderr_and_retries(tmp_path: Path) -> None:
    bridge = load("chatgpt_agbrowse_bridge_reclassify_test", "chatgpt_agbrowse_bridge.py")
    manifest = tmp_path / "manifest.json"
    write_prompt_manifest(
        manifest,
        {"question": "retry smoke", "mode_label": "Pro", "app_policy": "forbidden"},
    )
    project = tmp_path / "project"
    project.mkdir()
    tabs_calls = 0

    def runner(argv, env, timeout):
        nonlocal tabs_calls
        if argv[1:] == ["tabs", "--json"]:
            tabs_calls += 1
            return completed(
                argv,
                stdout=json.dumps(
                    []
                    if tabs_calls == 1
                    else [{"targetId": "T-RECLASSIFIED", "url": "https://chatgpt.com/c/reclassified"}]
                ),
            )
        if argv[1:4] == ["web-ai", "sessions", "show"]:
            return completed(
                argv,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "session": {
                            "sessionId": "S-RECLASSIFIED",
                            "targetId": "T-RECLASSIFIED",
                            "conversationUrl": "https://chatgpt.com/c/reclassified",
                        },
                    }
                ),
            )
        return completed(
            argv,
            stdout=json.dumps(
                {
                    "ok": True,
                    "status": "submitted",
                    "sessionId": "S-RECLASSIFIED",
                    "targetId": "T-RECLASSIFIED",
                    "conversationUrl": "https://chatgpt.com/c/reclassified",
                }
            ),
        )

    runtime = bridge.Bridge(state_root=tmp_path / "state", runner=runner)
    record = runtime.store.create_run(
        project_root=str(project),
        manifest_path=str(manifest),
        agbrowse_contract={"executable": "agbrowse"},
    )
    run_dir = str(record["run_dir"])
    runtime.store.transition(run_dir, "PREFLIGHTED")
    runtime.store.transition(run_dir, "LEASED")
    runtime.store.transition(run_dir, "SEND_STARTED")
    runtime.store.transition(
        run_dir,
        "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
        block_code="AGBROWSE_JSON_INVALID",
    )
    evidence_dir = Path(run_dir) / "agbrowse-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "send.stdout.txt").write_text("", encoding="utf-8")
    (evidence_dir / "send.stderr.txt").write_text(
        json.dumps(
            {
                "ok": False,
                "status": "error",
                "error": {
                    "errorCode": "capability.unsupported",
                    "stage": "provider-surface-preflight",
                    "message": "Chat commands are not supported on the Work surface",
                    "mutationAllowed": False,
                },
            }
        ),
        encoding="utf-8",
    )

    result = runtime.send(run_dir)

    assert result["phase"] == "URL_BOUND"
    assert result["session_id"] == "S-RECLASSIFIED"
    assert result["terminal_block_code"] is None
    assert any(
        item.get("kind") == "verified-mutation-disallowed-reclassification"
        for item in result["recovery_events"]
    )


def test_pre_submit_app_block_can_resume_and_clears_terminal_code(tmp_path: Path) -> None:
    state = load("chatgpt_agbrowse_state_retry_test", "chatgpt_agbrowse_state.py")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "question": "smoke",
                "app_policy": "required",
                "chatgpt_app_name": "CodexPro-Test",
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    store = state.RunStore(tmp_path / "state")
    record = store.create_run(project_root=str(project), manifest_path=str(manifest), agbrowse_contract={})
    run_dir = str(record["run_dir"])
    store.transition(run_dir, "PREFLIGHTED")
    blocked = store.transition(run_dir, "BLOCKED_APP_TRANSACTION", block_code="APP_TRANSACTION_FAILED")

    resumed = store.transition(run_dir, "PREFLIGHTED")

    assert blocked["terminal_block_code"] == "APP_TRANSACTION_FAILED"
    assert resumed["phase"] == "PREFLIGHTED"
    assert resumed["terminal_block_code"] is None


def test_app_send_target_mismatch_retains_session_for_recovery(tmp_path: Path) -> None:
    bridge = load("chatgpt_agbrowse_bridge_target_test", "chatgpt_agbrowse_bridge.py")
    manifest = tmp_path / "manifest.json"
    write_prompt_manifest(
        manifest,
        {
                "question": "return smoke",
                "mode_label": "GPT-5.6",
                "mode_variant": "High",
                "app_policy": "required",
                "app_selection_transport": "inline-pill-reuse",
                "chatgpt_app_name": "CodexPro-Test",
                "chatgpt_app_server_url": "https://example.test/mcp?codexpro_token=secret",
        },
    )
    project = tmp_path / "project"
    project.mkdir()

    class Connector:
        def inspect(self, app_name, expected_url=None):
            return {
                "ok": True,
                "state": "detail",
                "app_name": app_name,
                "url": expected_url,
                "connected": True,
                "full_access": True,
            }

        def prepare_composer_app(self, app_name, composer_url):
            return {
                "ok": True,
                "state": "composer-app-mention-tab-confirmed",
                "app_name": app_name,
                "target_id": "T-PREPARED",
                "url": composer_url,
                "selection_method": "exact-at-mention-then-tab",
                "mention_text_sha256": hashlib.sha256(f"@{app_name}".encode("utf-8")).hexdigest(),
            }

        def activate_composer_target(self, target_id):
            assert target_id == "T-PREPARED"
            return {"ok": True, "target_id": target_id}

    def runner(argv, env, timeout):
        if argv[1:3] == ["web-ai", "send"]:
            return completed(
                argv,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "status": "submitted",
                        "sessionId": "S-EXACT",
                        "targetId": "T-OTHER",
                        "conversationUrl": "https://chatgpt.com/c/exact-smoke",
                    }
                ),
            )
        if argv[1:4] == ["web-ai", "sessions", "show"]:
            return completed(
                argv,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "session": {
                            "sessionId": "S-EXACT",
                            "targetId": "T-OTHER",
                            "conversationUrl": "https://chatgpt.com/c/exact-smoke",
                        },
                    }
                ),
            )
        raise AssertionError(argv)

    runtime = bridge.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        app_connector_factory=lambda executable: Connector(),
        app_identity_probe=lambda url, root, port, timeout: {"ok": True, "reason": "identity-ok"},
        headed_runtime_preflight=False,
    )
    record = runtime.store.create_run(
        project_root=str(project),
        manifest_path=str(manifest),
        agbrowse_contract={"executable": "agbrowse"},
    )
    run_dir = str(record["run_dir"])
    runtime.store.transition(run_dir, "PREFLIGHTED")

    result = runtime.send(run_dir)

    assert result["phase"] == "RECOVERY_REQUIRED"
    assert result["session_id"] == "S-EXACT"
    assert result["current_target_id"] == "T-PREPARED"
    assert result["recovery_events"][-1]["actual_target_id"] == "T-OTHER"


def test_app_activation_failure_retry_accepts_fresh_pre_submit_target(tmp_path: Path) -> None:
    bridge = load("chatgpt_agbrowse_bridge_activation_retry_test", "chatgpt_agbrowse_bridge.py")
    manifest = tmp_path / "manifest.json"
    write_prompt_manifest(
        manifest,
        {
                "question": "return retry smoke",
                "mode_label": "GPT-5.6",
                "mode_variant": "High",
                "app_policy": "required",
                "app_selection_transport": "inline-pill-reuse",
                "chatgpt_app_name": "CodexPro-Test",
                "chatgpt_app_server_url": "https://example.test/mcp?codexpro_token=secret",
        },
    )
    project = tmp_path / "project"
    project.mkdir()

    class Connector:
        prepared = 0

        def inspect(self, app_name, expected_url=None):
            return {
                "ok": True,
                "state": "detail",
                "app_name": app_name,
                "url": expected_url,
                "connected": True,
                "full_access": True,
            }

        def prepare_composer_app(self, app_name, composer_url):
            self.prepared += 1
            return {
                "ok": True,
                "state": "composer-app-mention-tab-confirmed",
                "app_name": app_name,
                "target_id": f"T-{self.prepared}",
                "url": composer_url,
                "selection_method": "exact-at-mention-then-tab",
                "mention_text_sha256": hashlib.sha256(f"@{app_name}".encode("utf-8")).hexdigest(),
            }

        def activate_composer_target(self, target_id):
            if target_id == "T-1":
                raise RuntimeError("transient activation failure")
            return {"ok": True, "target_id": target_id}

    connector = Connector()

    class TabLifecycle:
        def __init__(self):
            self.owned: set[str] = set()
            self.closed: list[str] = []
            self.protected: list[str] = []

        def record_owned(self, run_dir, *, target_id, url, stage):
            self.owned.add(target_id)
            return {"ok": True}

        def close_pre_submit(self, run_dir, *, target_id, reason):
            self.closed.append(target_id)
            return {"ok": True, "state": "closed-and-absent", "target_id": target_id}

        def record_protected(self, run_dir, *, target_id, conversation_url, stage):
            self.protected.append(target_id)
            return {"ok": True}

    tabs = TabLifecycle()

    def runner(argv, env, timeout):
        if argv[1:3] == ["web-ai", "send"]:
            return completed(
                argv,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "status": "submitted",
                        "sessionId": "S-RETRY",
                        "targetId": "T-2",
                        "conversationUrl": "https://chatgpt.com/c/retry-smoke",
                    }
                ),
            )
        if argv[1:4] == ["web-ai", "sessions", "show"]:
            return completed(
                argv,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "session": {
                            "sessionId": "S-RETRY",
                            "targetId": "T-2",
                            "conversationUrl": "https://chatgpt.com/c/retry-smoke",
                        },
                    }
                ),
            )
        if argv[1:] == ["tabs", "--json"]:
            return completed(
                argv,
                stdout=json.dumps(
                    [{"targetId": "T-2", "url": "https://chatgpt.com/c/retry-smoke"}]
                ),
            )
        raise AssertionError(argv)

    runtime = bridge.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        app_connector_factory=lambda executable: connector,
        app_identity_probe=lambda url, root, port, timeout: {"ok": True, "reason": "identity-ok"},
        tab_lifecycle_factory=lambda executable, manifest: tabs,
        headed_runtime_preflight=False,
    )
    record = runtime.store.create_run(
        project_root=str(project),
        manifest_path=str(manifest),
        agbrowse_contract={"executable": "agbrowse"},
    )
    run_dir = str(record["run_dir"])
    runtime.store.transition(run_dir, "PREFLIGHTED")

    with pytest.raises(bridge.BridgeError) as failure:
        runtime.send(run_dir)
    assert failure.value.code == "APP_COMPOSER_TARGET_ACTIVATION_FAILED"
    _, blocked = runtime.store.load(run_dir)
    assert blocked["phase"] == "PREFLIGHT_BLOCKED"
    assert blocked["current_target_id"] == "T-1"
    assert tabs.closed == ["T-1"]

    retried = runtime.send(run_dir)

    assert retried["phase"] == "URL_BOUND"
    assert retried["current_target_id"] == "T-2"
    assert retried["session_id"] == "S-RETRY"
    assert retried["target_rebind_events"][-1]["reason"] == "pre-submit-composer-retry"
    assert tabs.closed == ["T-1", "T-1"]
    assert tabs.protected == ["T-2"]


def test_expected_app_url_never_crosses_drive_scope() -> None:
    app = load("codexpro_agbrowse_app_cross_drive_scope_test", "codexpro_agbrowse_app.py")

    class Registry:
        @staticmethod
        def load_registry():
            return {
                "projects": {
                    r"C:\\": {
                        "app_name": "CodexPro-CDrive-v11",
                        "status": "active",
                        "public_url": "https://fixed.example.test/mcp?codexpro_token=[REDACTED_SECRET]",
                        "port": 8790,
                    }
                }
            }

    connector = app.AppConnector(object(), registry=Registry)

    assert connector.expected_registration_for_scope("CodexPro-CDrive-v11", "D:\\project") is None


def test_old_app_cleanup_requires_candidate_commit_for_same_root() -> None:
    app = load("codexpro_agbrowse_app_cleanup_fence_test", "codexpro_agbrowse_app.py")

    class Registry:
        failures: list[dict] = []

        @staticmethod
        def record_reconcile_started(decision):
            return {"ok": True}

        @staticmethod
        def record_reconcile_confirmation(decision, result):
            return {
                "ok": True,
                "action": "candidate-committed",
                "root": decision["root"],
                "retired_app_name": "CodexPro-Other-v01",
            }

        @classmethod
        def record_reconcile_failure(cls, decision, result):
            cls.failures.append(result)
            return {"ok": True}

        @staticmethod
        def record_retired_cleanup(decision, cleanup):
            raise AssertionError("cleanup record must not be written")

    class Connector(app.AppConnector):
        inspections = 0
        deleted: list[str] = []

        def inspect(self, app_name, expected_url=None):
            self.inspections += 1
            if self.inspections == 1:
                return {"ok": True, "state": "missing", "app_name": app_name}
            return {
                "ok": True,
                "state": "detail",
                "app_name": app_name,
                "url": expected_url,
                "connected": True,
                "full_access": True,
                "route": app.SETTINGS_URL,
            }

        def _fill_create_form(self, decision):
            return None

        def _connect_and_maximize_permission(self, app_name):
            return None

        def _delete_retired(self, app_name):
            self.deleted.append(app_name)
            return {"ok": True}

    class UtilityUI:
        @staticmethod
        def open_utility_target(url):
            assert url == app.SETTINGS_URL
            return {"target_id": "T-UTILITY"}

        @staticmethod
        def close_owned_target(target_id):
            assert target_id == "T-UTILITY"
            return {"ok": True, "target_id": target_id, "absence_verified": True}

    connector = Connector(UtilityUI(), registry=Registry)
    decision = {
        "root": "D:\\",
        "app_name": "CodexPro-DDrive-v01",
        "public_url": "https://dynamic.trycloudflare.com/mcp?codexpro_token=[REDACTED_SECRET]",
        "old_app_name": "CodexPro-CDrive-v11",
        "transaction_id": "tx-d-drive",
    }

    with pytest.raises(app.AppBridgeError) as failure:
        connector.reconcile(decision)

    assert failure.value.code == "APP_RETIRE_OWNERSHIP_UNPROVEN"
    assert connector.deleted == []
