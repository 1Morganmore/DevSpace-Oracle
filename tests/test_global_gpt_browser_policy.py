from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THINKING = ROOT / "skills" / "chatgpt-thinking-browser" / "SKILL.md"
PRO = ROOT / "skills" / "chatgpt-pro-browser" / "SKILL.md"
HANDOFF = ROOT / "skills" / "chatgpt-pro-plan-handoff" / "SKILL.md"
MULTI = ROOT / "skills" / "web-multi-gpt" / "SKILL.md"
RESEARCH = ROOT / "skills" / "chatgpt-deep-research-browser" / "SKILL.md"
ORACLE = ROOT / "skills" / "chatgpt-oracle-runtime" / "SKILL.md"
DESIGNER = ROOT / "skills" / "chatgpt-question-designer" / "SKILL.md"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_new_regular_modes_route_only_to_oracle_devspace() -> None:
    value = text(THINKING)
    assert "chatgpt_oracle_dispatch.py" in value
    assert "@DevSpace" in value
    assert "never attaches files" in value
    assert "create a new agbrowse run" in value
    assert "app picker" not in value.casefold()


def test_pro_is_oracle_attachment_only_and_never_uses_devspace_or_codexpro() -> None:
    value = text(PRO)
    assert "attachment" in value.casefold()
    assert "Oracle is the only backend for a new Pro run" in value
    assert "must never invoke DevSpace or CodexPro" not in value
    assert "There is no new agbrowse, CodexPro, DevSpace" in value
    handoff = text(HANDOFF)
    assert "Pro is attachment-only through `chatgpt-pro-browser` and Oracle" in handoff
    assert "never uses\nDevSpace or CodexPro" in handoff


def test_deep_research_uses_oracle_deep_without_silent_fallback() -> None:
    value = text(RESEARCH)
    assert "chatgpt_oracle_dispatch.py" in value
    assert "--mode deep-research" in value
    assert "--browser-research deep" in value
    assert '--reasoning-level "Very High"' in value
    assert "visible `Extra High`" in value
    assert "Do not silently replace Deep Research" in value


def test_web_multi_is_genuine_sessions_with_wave_cap_and_worktrees() -> None:
    value = text(MULTI)
    assert "chatgpt_oracle_multi.py" in value
    assert "waves of at most five" in value
    assert "worktree-write" in value
    assert "distinct pre-created worktree" in value
    assert "single-GPT role simulation" in value


def test_comprehensive_is_web_native_relay_with_one_local_gate() -> None:
    value = text(HANDOFF)
    assert "chatgpt_oracle_comprehensive.py" in value
    assert "plan -> optional Pro or Oracle Web Multi -> review" in value
    assert "final web PASS plus a zero-exit local" in value
    assert "host validates" in value
    assert "never rewrites the semantic prompt" in value


def test_host_control_state_is_outside_devspace_project() -> None:
    value = text(ORACLE)
    assert "%USERPROFILE%\\.codex\\state\\chatgpt-oracle" in value
    source = text(ROOT / "bin" / "chatgpt_oracle_state.py")
    assert "HOST_STATE_OVERLAPS_PROJECT" in source


def test_oracle_recovery_is_exact_slug_no_restart_and_monotonic() -> None:
    value = text(THINKING)
    assert "stored slug" in value
    assert "--no-recover" in value
    assert "never restarts/resubmits" in value
    assert "never downgrades durable COMPLETE" in value


def test_install_inventory_contains_new_active_runtime_and_keeps_legacy_recovery() -> None:
    manifest = json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))
    include = set(manifest["include"])
    for path in (
        "bin/chatgpt_oracle_dispatch.py",
        "bin/chatgpt_oracle_multi.py",
        "bin/chatgpt_oracle_comprehensive.py",
        "skills/chatgpt-workspace-setup/SKILL.md",
    ):
        assert path in include
    assert "bin/chatgpt_agbrowse_run.py" in include
    assert manifest["routing"] == {
        "new_work_engine": "oracle",
        "regular_workspace_transport": "devspace",
        "pro_transport": "oracle-attachment-only",
        "agbrowse": "persisted-run-recovery-only",
        "codexpro": "persisted-run-recovery-only",
    }
    assert manifest["external"]["oracle"]["license"] == "MIT"
    assert manifest["external"]["devspace"]["license"] == "MIT"
    assert manifest["external"]["agbrowse"]["role"] == "persisted-run-recovery-only"
    assert manifest["external"]["agbrowse"]["default_install"] is False
    assert manifest["external"]["codexpro"]["frozen"] is True


def test_no_new_skill_routes_to_chrome_playwright_or_in_app_fallback() -> None:
    combined = "\n".join(text(path) for path in (THINKING, HANDOFF, MULTI, RESEARCH)).casefold()
    assert "@chrome" not in combined
    assert "falls back to\nagbrowse, playwright, in-app browser, or chrome" in combined


def test_readme_declares_manual_one_time_registration_not_ui_automation() -> None:
    value = text(ROOT / "README.md")
    assert "최초 한 번 수동 등록" in value
    assert "ChatGPT 설정·앱 목록·권한·삭제·선택 UI를 자동화하지 않습니다" in value
    assert "기존 실행의 정확한 복구" in value


def test_question_designer_cannot_route_new_work_through_codexpro_or_legacy_sessions() -> None:
    value = text(DESIGNER)
    assert "CodexPro is frozen for new work" in value
    assert "never design a new prompt around CodexPro" in value
    assert "Every new Oracle stage is a one-shot session" in value
    assert "Do not add legacy `session_policy`" in value
    assert "verified CodexPro live connector context remains the default" not in value


def test_agent_metadata_exposes_oracle_active_routes() -> None:
    thinking = text(ROOT / "skills" / "chatgpt-thinking-browser" / "agents" / "openai.yaml")
    multi = text(ROOT / "skills" / "web-multi-gpt" / "agents" / "openai.yaml")
    pro = text(ROOT / "skills" / "chatgpt-pro-browser" / "agents" / "openai.yaml")
    assert "Oracle and DevSpace" in thinking
    assert "parallel Oracle GPT sessions" in multi
    assert "attachment-only Pro through Oracle" in pro
