from pathlib import Path


CODEX_HOME = Path(__file__).resolve().parents[1]
GPT_SKILLS = [
    CODEX_HOME / "skills" / "chatgpt-thinking-browser" / "SKILL.md",
    CODEX_HOME / "skills" / "chatgpt-pro-browser" / "SKILL.md",
    CODEX_HOME / "skills" / "chatgpt-deep-research-browser" / "SKILL.md",
]
HANDOFF_SKILL = CODEX_HOME / "skills" / "chatgpt-pro-plan-handoff" / "SKILL.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_owner_skills_define_codexpro_identity_and_pro_transport() -> None:
    thinking_text = _text(GPT_SKILLS[0])
    pro_text = _text(GPT_SKILLS[1])
    assert "Every non-Pro ChatGPT mode must use one exact named CodexPro app." in thinking_text
    assert "`app_policy: optional` is invalid" in thinking_text
    assert "exact app name, full server URL, connected state, and `full_access`" in thinking_text
    assert "App identity is drive-scoped." in thinking_text
    assert "A C-drive mismatch blocks" in thinking_text
    assert "One drive must never inherit" in thinking_text
    assert "external dependency only" in thinking_text
    assert "Do not vendor, fork, copy, or reimplement" in thinking_text
    assert "`mode_label: Pro`" in pro_text
    assert "`app_policy: forbidden`" in pro_text
    assert "Local context is attachment-only" in pro_text


def test_all_chatgpt_skills_use_only_explicit_contract_validated_agbrowse() -> None:
    for path in GPT_SKILLS:
        text = _text(path)
        assert "0.1.18" in text, path
        assert "tested default" in text, path
        assert "contract" in text.casefold(), path
        assert "pinned, unmodified" not in text.casefold(), path
        assert "in-app Browser" in text, path
        assert "@chrome" in text, path
        assert "fallback" in text.casefold(), path


def test_fallback_cannot_downgrade_requested_gpt_authority() -> None:
    assert "never reinterpret Pro as regular GPT" in _text(GPT_SKILLS[0])
    assert "Never downgrade Pro to regular GPT" in _text(GPT_SKILLS[1])
    assert "Do not downgrade to ordinary GPT or Pro" in _text(GPT_SKILLS[2])


def test_published_scope_does_not_install_a_chrome_backend() -> None:
    assert not (CODEX_HOME / "skills" / "chrome-stability-guard").exists()
    for path in GPT_SKILLS:
        text = _text(path)
        assert "@chrome" in text
        assert "fallback" in text.casefold()


def test_recovery_is_exact_session_and_never_mixed_backend() -> None:
    required = ["sessions doctor <session>", "exact", "URL"]
    for path in GPT_SKILLS:
        text = _text(path)
        for phrase in required:
            assert phrase in text, (path, phrase)


def test_completed_owned_conversations_are_automatic_but_uncertain_tabs_are_protected() -> None:
    required = ["automatically close", "uncertain", "unique live"]
    for path in GPT_SKILLS:
        text = _text(path)
        for phrase in required:
            assert phrase in text, (path, phrase)


def test_completed_cleanup_has_no_legacy_or_second_request_exception() -> None:
    root = CODEX_HOME
    policy_text = "\n".join(
        _text(path)
        for path in (
            root / "README.md",
            root / "docs" / "ARCHITECTURE_V2.md",
            *GPT_SKILLS,
        )
    ).casefold()
    assert "legacy manifests without it retain explicit-request-only behavior" not in policy_text
    assert "v1 manifests remain recovery-only with their original mode and cleanup rules" not in policy_text
    assert "including recovered legacy runs" in policy_text
    assert "does not require a separate cleanup request" in policy_text


def test_thinking_skill_requires_exact_owned_tab_cleanup_contract() -> None:
    text = _text(GPT_SKILLS[0])
    for phrase in (
        "generic pool/idle/count cleanup limits",
        "exact owned pre-submit root composer",
        "durable `COMPLETE`",
        "cleanup_pending",
        "one unique live URL match",
    ):
        assert phrase in text


def test_thinking_skill_tracks_current_connectors_ui_contract() -> None:
    text = _text(GPT_SKILLS[0])
    for phrase in (
        "two-step",
        "저위험 액션 허용",
        "single-snapshot capabilities",
        "six hydrated settings reads",
        "identity-verified candidate port",
    ):
        assert phrase in text


def test_cold_start_creates_an_owned_parallel_session_instead_of_deferring() -> None:
    for path in (*GPT_SKILLS[:2], HANDOFF_SKILL):
        text = _text(path)
        assert "zero live sessions" in text.casefold() or "session list is empty" in text.casefold(), path
        assert "--url https://chatgpt.com/ --parallel" in text, path
        assert "not a precondition" in text.casefold() or "never a prerequisite" in text.casefold(), path


def test_new_comprehensive_workflows_are_v2_gated_and_v1_is_recovery_only() -> None:
    text = _text(HANDOFF_SKILL)
    assert "codex.chatgpt.comprehensive-workflow/v2" in text
    assert "COMPREHENSIVE_V2_REQUIRED" in text
    assert "v1 is recovery-only" in text
    assert "routine plan does not pay the Web Multi-GPT latency cost" in text


def test_windows_agbrowse_subprocesses_are_created_without_console_windows() -> None:
    text = _text(CODEX_HOME / "bin" / "chatgpt_agbrowse_bridge.py")
    for token in ("CREATE_NO_WINDOW", "STARTUPINFO", "STARTF_USESHOWWINDOW", "SW_HIDE"):
        assert token in text
