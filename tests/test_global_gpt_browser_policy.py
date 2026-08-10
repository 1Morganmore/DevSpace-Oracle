from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
THINKING = ROOT / "skills/chatgpt-thinking-browser/SKILL.md"
PRO = ROOT / "skills/chatgpt-pro-browser/SKILL.md"
HANDOFF = ROOT / "skills/chatgpt-pro-plan-handoff/SKILL.md"
MULTI = ROOT / "skills/web-multi-gpt/SKILL.md"
RESEARCH = ROOT / "skills/chatgpt-deep-research-browser/SKILL.md"
DESIGNER = ROOT / "skills/chatgpt-question-designer/SKILL.md"
ROUTER = ROOT / "skills/devspace-oracle-router/SKILL.md"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_regular_modes_route_only_through_oracle_and_devspace() -> None:
    value = text(THINKING)
    assert "chatgpt_oracle_dispatch.py" in value
    assert "@DevSpace" in value and "never attaches files" in value
    assert "another backend, Playwright, in-app Browser, or Chrome" in value
    assert "Oracle `0.17.2`" in value and "`Extra High`" in value


def test_pro_is_oracle_attachment_only_heavy_and_has_no_app_fallback() -> None:
    value = text(PRO)
    assert "Oracle is the only backend for a new Pro run" in value
    assert "There is no DevSpace, alternate app, in-app Browser" in value
    assert "gpt-5.6-sol" in value and "heavy" in value
    assert "never downgrade" in value


def test_deep_research_and_web_multi_use_the_active_oracle_entry_points() -> None:
    research = text(RESEARCH)
    multi = text(MULTI)
    assert "chatgpt_oracle_dispatch.py" in research and "--mode deep-research" in research
    assert "visible `Extra High`" in research
    assert "chatgpt_oracle_multi.py" in multi and "waves of at most five" in multi
    assert "distinct pre-created worktree" in multi and "single-GPT role simulation" in multi


def test_comprehensive_keeps_name_and_oracle_v1_semantics() -> None:
    value = text(HANDOFF)
    assert HANDOFF.parent.name == "chatgpt-pro-plan-handoff"
    assert "chatgpt_oracle_comprehensive.py" in value
    assert "plan -> optional Pro or Oracle Web Multi -> review" in value
    assert "never rewrites" in value and "never starts automatically or as a fallback" in value


def test_oracle_recovery_is_exact_slug_monotonic_and_version_specific() -> None:
    value = text(THINKING)
    assert "stored slug" in value and "never restarts/resubmits" in value
    assert "never downgrades durable COMPLETE" in value
    source = text(ROOT / "bin/chatgpt_oracle_state.py")
    assert 'ORACLE_RECOVERABLE_VERSIONS = ("0.16.1", "0.17.0", "0.17.1", ORACLE_ACTIVE_VERSION)' in source
    assert 'WAIT_CAPABLE_VERSIONS = {"0.17.0", "0.17.1", ORACLE_ACTIVE_VERSION}' in source


def test_manifest_exposes_only_active_routing_authorities() -> None:
    manifest = json.loads(text(ROOT / "install-manifest.json"))
    assert manifest["routing"] == {
        "new_work_engine": "oracle",
        "regular_workspace_transport": "devspace",
        "pro_transport": "oracle-attachment-only",
    }
    assert set(manifest["external"]) == {"oracle", "devspace"}


def test_local_oracle_runtime_does_not_inject_host_conversation_metadata() -> None:
    source = "\n".join(
        text(path)
        for path in ROOT.glob("bin/chatgpt_oracle_*.py")
    )
    assert 'openai/session' not in source


def test_question_designer_forbids_alternate_new_work_routes() -> None:
    value = text(DESIGNER)
    assert "New non-Pro direct, plan, review, edit, orchestrator" in value
    assert "Never design a new prompt around an alternate workspace backend" in value
    assert "never authorizes ZIP, another backend, in-app Browser" in value


def test_standalone_pro_stops_after_one_result() -> None:
    value = text(PRO)
    assert "standalone, one-shot Pro route" in value
    assert "returns that durable Pro result to Codex" in value
    assert "never starts a review-to-implementation chain" in value


def test_natural_language_router_covers_every_mode_and_safe_cost_gate() -> None:
    value = text(ROUTER)
    for phrase in (
        "일반 GPT", "계획", "검토", "수정", "지휘모드", "딥 리서치",
        "Web Multi-GPT", "Local Multi-GPT", "종합모드", "Pro",
    ):
        assert phrase in value
    assert "--chatgpt-project <name>" in value
    assert '"when": "explicit-user-request"' in value
    assert "only after explicit authorization" in value
    assert "TASK_OUTCOME: BLOCKED" in value and "do not resubmit" in value


def test_oracle_runs_use_isolated_profiles_and_hidden_windows() -> None:
    value = text(THINKING)
    assert "throwaway" in value and "per-run profile" in value
    assert "hide its owned window" in value
