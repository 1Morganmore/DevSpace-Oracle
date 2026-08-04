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


def test_pro_requires_maximum_useful_project_specific_judgment_context() -> None:
    value = text(PRO)
    flat = " ".join(value.split())
    assert "project-specific, judgment-complete" in flat
    assert "maximum-useful-context packet" in flat
    assert "universal default for every project and every Pro consultation" in flat
    assert "requires no project-specific opt-in" in flat
    assert "Fill the verified effective Oracle/Pro attachment and model-context budget" in flat
    assert "up to the practical maximum" in flat
    assert "effective attachment/context budget used for the run" in flat
    assert "included bytes or tokens when measurable" in flat
    assert "remaining capacity could not improve the decision" in flat
    assert "Do not silently stop at a customary file count or convenience bundle size" in flat
    assert "using the domain-appropriate equivalent" in flat
    assert "all non-duplicative, project-specific, decision-relevant project evidence" in flat
    assert "governing rules and exact question" in flat
    assert "canonical current state and measured primary evidence" in flat
    assert "conflicts, failures, controls, and prior decisions" in flat
    assert "source or implementation feasibility" in flat
    assert "compact supporting detail" in flat
    assert "excluded or truncated artifact" in flat
    assert "coverage boundary, priority, and expected decision impact" in flat
    assert "complete applicable `AGENTS.md`/project-rule chain" in flat
    assert "killed, rejected, blocked, or near-duplicate routes" in flat
    assert "exact prior answer" in flat
    assert "evidence index" in flat
    assert "known omissions" in flat
    assert "databases/WAL files" in flat
    assert "credentials, secrets, cookies, browser/profile state" in flat
    assert "volatile logs, caches, and unrelated bulk" in flat
    assert "Preflight fails closed" in flat
    assert "build_project_context_packet.py build" in flat
    assert "build_project_context_packet.py validate" in flat
    assert "explicit evidence allowlist" in flat
    assert "local proven/configured transport envelope" in flat
    assert "not vendor or model limits" in flat


def test_pro_requires_an_evidence_based_web_multi_decision_and_auto_handoff() -> None:
    value = text(PRO)
    assert "WEB_MULTI_NEEDED: YES|NO" in value
    assert "WEB_MULTI_REASON: evidence-based reason" in value
    assert "three to five materially independent" in value
    assert "ready-to-run Web Multi-GPT Very\nHigh mission" in value
    assert "same project maximum-context evidence and the durable Pro answer" in value
    assert "stable lane order, and synthesis/judge criteria" in value
    assert "automatically without a routine user\nchoice" in value
    assert "trivial, single-answer, or purely mechanical question" in value


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
    assert "never restarts/resubmits" in value
    assert "never downgrades durable COMPLETE" in value
    assert "exact persisted" in value
    assert "replacement" in value
    runtime = text(ORACLE)
    assert "`recovery_binding_unavailable`" in runtime
    assert "restore the\nexact persisted conversation URL" in runtime


def test_oracle_runs_use_isolated_profile_copies_and_owned_hidden_windows() -> None:
    value = text(THINKING)
    assert "throwaway" in value
    assert "per-run profile" in value
    assert "hide its owned window" in value


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
    assert "실행 신원으로 정확히 복구" in value


def test_english_readme_maps_modes_to_the_same_oracle_routes() -> None:
    value = text(ROOT / "README.en.md")
    assert "Oracle + DevSpace" in value
    assert "`orchestrator` / orchestrator" in value
    assert "`deep-research` / deep research" in value
    assert "comprehensive mode" in value
    assert "Web Multi-GPT" in value
    assert "Oracle attachments only" in value
    assert "never resubmits the task" in value


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
    assert "one-shot attachment-only Pro review through Oracle" in pro
    assert "allow_implicit_invocation: true" in pro


def test_standalone_pro_never_transitions_into_comprehensive_implementation() -> None:
    pro = text(PRO)
    assert "standalone, one-shot Pro route" in pro
    assert "returns that durable Pro result to Codex\nand stops" in pro
    assert "never starts a review-to-implementation chain" in pro
    assert "If the user asks for comprehensive mode, use `chatgpt-pro-plan-handoff`" in pro
