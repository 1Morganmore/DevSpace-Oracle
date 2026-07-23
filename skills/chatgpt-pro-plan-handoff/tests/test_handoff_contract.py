from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from handoff_contract import (  # noqa: E402
    ContractError,
    ExpectedBinding,
    ORCHESTRATOR_RESULT_SCHEMA,
    PLAN_RESULT_SCHEMA,
    REVIEW_RESULT_SCHEMA,
    canonical_sha256,
    parse_final_envelope,
    validate_orchestrator_envelope,
    validate_plan_envelope,
    validate_review_envelope,
    validate_plan_envelope_v2,
    validate_review_envelope_v2,
)


def binding(**overrides):
    values = {
        "workflow_id": "wf-1",
        "stage": "pro-plan",
        "attempt_index": 1,
        "nonce": "nonce-0123456789",
        "question_sha256": "a" * 64,
        "source_snapshot_sha256": "b" * 64,
        "plan_sha256": None,
        "advisory_sha256": None,
        "review_sha256": None,
    }
    values.update(overrides)
    return ExpectedBinding(**values)


def fenced(payload: dict, *, suffix: str = "") -> str:
    return "analysis text\n```json\n" + json.dumps(payload) + "\n```" + suffix


def plan_payload() -> dict:
    return {
        "schema": PLAN_RESULT_SCHEMA,
        "workflow_id": "wf-1",
        "stage": "pro-plan",
        "attempt_index": 1,
        "nonce": "nonce-0123456789",
        "question_sha256": "a" * 64,
        "source_snapshot_sha256": "b" * 64,
        "status": "complete",
        "sections": [
            "blockers",
            "evidence",
            "alternatives",
            "files",
            "implementation",
            "tests",
            "rollback",
            "conclusion_change_triggers",
        ],
    }


def test_snapshot_hash_is_stable_under_canonical_serialization():
    left = {"b": [2, 1], "a": {"z": True}}
    right = {"a": {"z": True}, "b": [2, 1]}
    assert canonical_sha256(left) == canonical_sha256(right)


def test_plan_envelope_rejects_wrong_question_hash():
    payload = plan_payload()
    payload["question_sha256"] = "c" * 64
    with pytest.raises(ContractError, match="WRONG_QUESTION_HASH"):
        validate_plan_envelope(payload, binding())


def test_duplicate_final_envelope_is_rejected():
    text = fenced(plan_payload()) + "\n" + fenced(plan_payload())
    with pytest.raises(ContractError, match="DUPLICATE_FINAL_ENVELOPE"):
        parse_final_envelope(text)


def test_final_envelope_must_be_last_content():
    with pytest.raises(ContractError, match="FINAL_ENVELOPE_NOT_LAST"):
        parse_final_envelope(fenced(plan_payload(), suffix="\ntrailing"))


def test_agbrowse_rendered_json_code_block_is_accepted_without_backticks():
    rendered = "JSON\n" + json.dumps(plan_payload())
    assert parse_final_envelope(rendered) == plan_payload()


def test_agbrowse_rendered_json_code_block_rejects_trailing_text():
    rendered = "JSON\n" + json.dumps(plan_payload()) + "\ntrailing"
    with pytest.raises(ContractError, match="FINAL_ENVELOPE_INVALID_JSON"):
        parse_final_envelope(rendered)


def test_agbrowse_rendered_json_accepts_only_known_korean_ui_footer():
    rendered = (
        "JSON\n"
        + json.dumps(plan_payload())
        + "\nChatGPT는 실수할 수 있습니다. 워크스페이스 데이터는 모델 학습에 사용되지 않습니다.\n\n높음\n"
    )
    assert parse_final_envelope(rendered) == plan_payload()


def test_unlabeled_raw_json_is_not_accepted_as_rendered_code_block():
    with pytest.raises(ContractError, match="FINAL_ENVELOPE_MISSING"):
        parse_final_envelope(json.dumps(plan_payload()))


def test_review_envelope_binds_exact_plan_hash():
    expected = binding(stage="gpt-review", plan_sha256="c" * 64)
    payload = {
        "schema": REVIEW_RESULT_SCHEMA,
        "workflow_id": "wf-1",
        "stage": "gpt-review",
        "attempt_index": 1,
        "nonce": "nonce-0123456789",
        "question_sha256": "a" * 64,
        "source_snapshot_sha256": "b" * 64,
        "input_plan_sha256": "d" * 64,
        "verdict": "PASS",
    }
    with pytest.raises(ContractError, match="WRONG_PLAN_HASH"):
        validate_review_envelope(payload, expected)


def test_legacy_review_without_advisory_hash_remains_valid():
    expected = binding(stage="gpt-review", plan_sha256="c" * 64)
    payload = {
        "schema": REVIEW_RESULT_SCHEMA,
        "workflow_id": "wf-1",
        "stage": "gpt-review",
        "attempt_index": 1,
        "nonce": "nonce-0123456789",
        "question_sha256": "a" * 64,
        "source_snapshot_sha256": "b" * 64,
        "input_plan_sha256": "c" * 64,
        "verdict": "PASS",
    }
    assert validate_review_envelope(payload, expected)["verdict"] == "PASS"


@pytest.mark.parametrize("advisory", [None, "e" * 64])
def test_advisory_bound_review_requires_exact_hash(advisory):
    expected = binding(stage="gpt-review", plan_sha256="c" * 64, advisory_sha256="d" * 64)
    payload = {
        "schema": REVIEW_RESULT_SCHEMA,
        "workflow_id": "wf-1",
        "stage": "gpt-review",
        "attempt_index": 1,
        "nonce": "nonce-0123456789",
        "question_sha256": "a" * 64,
        "source_snapshot_sha256": "b" * 64,
        "input_plan_sha256": "c" * 64,
        "input_advisory_sha256": advisory,
        "verdict": "PASS",
    }
    with pytest.raises(ContractError, match="WRONG_ADVISORY_HASH|REQUIRED_FIELD_MISSING"):
        validate_review_envelope(payload, expected)


def test_advisory_bound_review_accepts_exact_hash():
    expected = binding(stage="gpt-review", plan_sha256="c" * 64, advisory_sha256="d" * 64)
    payload = {
        "schema": REVIEW_RESULT_SCHEMA,
        "workflow_id": "wf-1",
        "stage": "gpt-review",
        "attempt_index": 1,
        "nonce": "nonce-0123456789",
        "question_sha256": "a" * 64,
        "source_snapshot_sha256": "b" * 64,
        "input_plan_sha256": "c" * 64,
        "input_advisory_sha256": "d" * 64,
        "verdict": "PASS",
    }
    assert validate_review_envelope(payload, expected)["input_advisory_sha256"] == "d" * 64


def test_orchestrator_envelope_binds_plan_review_and_source_hashes():
    expected = binding(
        stage="gpt-orchestrator",
        plan_sha256="c" * 64,
        review_sha256="d" * 64,
    )
    payload = {
        "schema": ORCHESTRATOR_RESULT_SCHEMA,
        "workflow_id": "wf-1",
        "stage": "gpt-orchestrator",
        "attempt_index": 1,
        "nonce": "nonce-0123456789",
        "question_sha256": "a" * 64,
        "source_snapshot_sha256": "b" * 64,
        "input_plan_sha256": "c" * 64,
        "input_review_sha256": "e" * 64,
        "status": "complete",
        "changed_files": [],
        "commands": [],
        "blockers": [],
    }
    with pytest.raises(ContractError, match="WRONG_REVIEW_HASH"):
        validate_orchestrator_envelope(payload, expected)


def test_valid_plan_envelope_parses_and_validates():
    payload = parse_final_envelope(fenced(plan_payload()))
    assert validate_plan_envelope(payload, binding())["status"] == "complete"


def test_v2_plan_rejects_missing_or_null_mismatched_research_descriptor_hash():
    expected = binding(stage="gpt-plan", research_descriptor_sha256="c" * 64)
    payload = {
        "schema": "codex.chatgpt.plan-result/v2", "workflow_id": "wf-1", "stage": "gpt-plan",
        "attempt_index": 1, "nonce": "nonce-0123456789", "question_sha256": "a" * 64,
        "source_snapshot_sha256": "b" * 64, "status": "complete",
    }
    with pytest.raises(ContractError, match="REQUIRED_FIELD_MISSING"):
        validate_plan_envelope_v2(payload, expected)
    payload["input_research_descriptor_sha256"] = None
    with pytest.raises(ContractError, match="WRONG_RESEARCH_DESCRIPTOR_HASH"):
        validate_plan_envelope_v2(payload, expected)


def test_v2_review_binds_skip_descriptor_hash_not_null():
    expected = binding(
        stage="gpt-review", plan_sha256="c" * 64,
        research_descriptor_sha256="d" * 64, advisory_descriptor_sha256="e" * 64,
    )
    payload = {
        "schema": "codex.chatgpt.review-result/v2", "workflow_id": "wf-1", "stage": "gpt-review",
        "attempt_index": 1, "nonce": "nonce-0123456789", "question_sha256": "a" * 64,
        "source_snapshot_sha256": "b" * 64, "input_plan_sha256": "c" * 64,
        "input_research_descriptor_sha256": "d" * 64, "input_advisory_descriptor_sha256": None,
        "verdict": "PASS",
    }
    with pytest.raises(ContractError, match="WRONG_ADVISORY_DESCRIPTOR_HASH"):
        validate_review_envelope_v2(payload, expected)
