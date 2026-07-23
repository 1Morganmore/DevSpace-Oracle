from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


PLAN_RESULT_SCHEMA = "codex.chatgpt.plan-result/v1"
REVIEW_RESULT_SCHEMA = "codex.chatgpt.review-result/v1"
ORCHESTRATOR_RESULT_SCHEMA = "codex.chatgpt.orchestrator-result/v1"
HANDOFF_SCHEMA = "codex.chatgpt.handoff/v1"
WORKFLOW_CORRELATION_SCHEMA = "codex.chatgpt.workflow-correlation/v1"
PLAN_RESULT_V2_SCHEMA = "codex.chatgpt.plan-result/v2"
RESEARCH_RESULT_V2_SCHEMA = "codex.chatgpt.research-result/v2"
REVIEW_RESULT_V2_SCHEMA = "codex.chatgpt.review-result/v2"
ORCHESTRATOR_RESULT_V2_SCHEMA = "codex.chatgpt.orchestrator-result/v2"

_FINAL_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_RENDERED_JSON_PREFIX = re.compile(r"\AJSON[ \t]*\r?\n")
# The ChatGPT Korean UI appends this fixed non-model footer after an otherwise
# complete rendered JSON code block.  Keep this deliberately locale- and
# label-specific: arbitrary trailing prose must remain a hard contract error.
_KOREAN_RENDERED_JSON_FOOTER = re.compile(
    r"\A\s*ChatGPT는 실수할 수 있습니다\. 워크스페이스 데이터는 모델 학습에 사용되지 않습니다\.\s*높음\s*\Z"
)


class ContractError(ValueError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class ExpectedBinding:
    workflow_id: str
    stage: str
    attempt_index: int
    nonce: str
    question_sha256: str
    source_snapshot_sha256: str
    plan_sha256: str | None = None
    advisory_sha256: str | None = None
    review_sha256: str | None = None
    research_descriptor_sha256: str | None = None
    advisory_descriptor_sha256: str | None = None


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def parse_final_envelope(text: str) -> dict[str, Any]:
    matches = list(_FINAL_JSON_BLOCK.finditer(text or ""))
    if not matches:
        stripped = (text or "").strip()
        rendered = _RENDERED_JSON_PREFIX.match(stripped)
        if rendered is None:
            raise ContractError("FINAL_ENVELOPE_MISSING")
        rendered_json = stripped[rendered.end() :]
        try:
            payload, end = json.JSONDecoder().raw_decode(rendered_json)
        except json.JSONDecodeError as exc:
            raise ContractError("FINAL_ENVELOPE_INVALID_JSON", str(exc)) from exc
        trailing = rendered_json[end:]
        if trailing.strip() and not _KOREAN_RENDERED_JSON_FOOTER.fullmatch(trailing):
            raise ContractError("FINAL_ENVELOPE_INVALID_JSON", "unexpected trailing rendered JSON content")
        if not isinstance(payload, dict):
            raise ContractError("FINAL_ENVELOPE_NOT_OBJECT")
        return payload
    if len(matches) != 1:
        raise ContractError("DUPLICATE_FINAL_ENVELOPE", str(len(matches)))
    match = matches[0]
    if (text or "")[match.end() :].strip():
        raise ContractError("FINAL_ENVELOPE_NOT_LAST")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ContractError("FINAL_ENVELOPE_INVALID_JSON", str(exc)) from exc
    if not isinstance(payload, dict):
        raise ContractError("FINAL_ENVELOPE_NOT_OBJECT")
    return payload


def _require(payload: Mapping[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is None or value == "":
        raise ContractError("REQUIRED_FIELD_MISSING", key)
    return value


def _require_equal(payload: Mapping[str, Any], key: str, expected: Any, code: str) -> None:
    actual = _require(payload, key)
    if actual != expected:
        raise ContractError(code, f"{key}={actual!r}, expected={expected!r}")


def _validate_common(payload: Mapping[str, Any], expected: ExpectedBinding, schema: str) -> None:
    _require_equal(payload, "schema", schema, "WRONG_ENVELOPE_SCHEMA")
    _require_equal(payload, "workflow_id", expected.workflow_id, "WRONG_WORKFLOW")
    _require_equal(payload, "stage", expected.stage, "WRONG_STAGE")
    _require_equal(payload, "attempt_index", expected.attempt_index, "WRONG_ATTEMPT")
    _require_equal(payload, "nonce", expected.nonce, "WRONG_NONCE")
    _require_equal(payload, "question_sha256", expected.question_sha256, "WRONG_QUESTION_HASH")
    _require_equal(
        payload,
        "source_snapshot_sha256",
        expected.source_snapshot_sha256,
        "WRONG_SOURCE_SNAPSHOT_HASH",
    )


def validate_plan_envelope(payload: Mapping[str, Any], expected: ExpectedBinding) -> dict[str, Any]:
    _validate_common(payload, expected, PLAN_RESULT_SCHEMA)
    _require_equal(payload, "status", "complete", "PLAN_NOT_COMPLETE")
    sections = _require(payload, "sections")
    if not isinstance(sections, list):
        raise ContractError("PLAN_SECTIONS_NOT_LIST")
    required_sections = {
        "blockers",
        "evidence",
        "alternatives",
        "files",
        "implementation",
        "tests",
        "rollback",
        "conclusion_change_triggers",
    }
    missing = sorted(required_sections - {str(item) for item in sections})
    if missing:
        raise ContractError("PLAN_REQUIRED_SECTIONS_MISSING", ",".join(missing))
    return dict(payload)


def _require_hash(payload: Mapping[str, Any], key: str, expected: str | None, code: str) -> None:
    """V2 deliberately distinguishes an omitted field from an explicit JSON null."""
    if key not in payload:
        raise ContractError("REQUIRED_FIELD_MISSING", key)
    actual = payload[key]
    if actual != expected:
        raise ContractError(code, f"{key}={actual!r}, expected={expected!r}")
    if actual is not None and (not isinstance(actual, str) or not re.fullmatch(r"[0-9a-f]{64}", actual)):
        raise ContractError("INVALID_SHA256", key)


def validate_research_envelope_v2(payload: Mapping[str, Any], expected: ExpectedBinding) -> dict[str, Any]:
    _validate_common(payload, expected, RESEARCH_RESULT_V2_SCHEMA)
    _require_equal(payload, "status", "complete", "RESEARCH_NOT_COMPLETE")
    if not isinstance(payload.get("findings"), list):
        raise ContractError("RESEARCH_FINDINGS_NOT_LIST")
    if not isinstance(payload.get("sources"), list):
        raise ContractError("RESEARCH_SOURCES_NOT_LIST")
    return dict(payload)


def validate_plan_envelope_v2(payload: Mapping[str, Any], expected: ExpectedBinding) -> dict[str, Any]:
    _validate_common(payload, expected, PLAN_RESULT_V2_SCHEMA)
    _require_equal(payload, "status", "complete", "PLAN_NOT_COMPLETE")
    _require_hash(payload, "input_research_descriptor_sha256", expected.research_descriptor_sha256, "WRONG_RESEARCH_DESCRIPTOR_HASH")
    sections = _require(payload, "sections")
    if not isinstance(sections, list):
        raise ContractError("PLAN_SECTIONS_NOT_LIST")
    required_sections = {
        "blockers",
        "evidence",
        "alternatives",
        "files",
        "implementation",
        "tests",
        "rollback",
        "conclusion_change_triggers",
    }
    missing = sorted(required_sections - {str(item) for item in sections})
    if missing:
        raise ContractError("PLAN_REQUIRED_SECTIONS_MISSING", ",".join(missing))
    return dict(payload)


def validate_review_envelope_v2(payload: Mapping[str, Any], expected: ExpectedBinding) -> dict[str, Any]:
    _validate_common(payload, expected, REVIEW_RESULT_V2_SCHEMA)
    _require_hash(payload, "input_plan_sha256", expected.plan_sha256, "WRONG_PLAN_HASH")
    _require_hash(payload, "input_research_descriptor_sha256", expected.research_descriptor_sha256, "WRONG_RESEARCH_DESCRIPTOR_HASH")
    _require_hash(payload, "input_advisory_descriptor_sha256", expected.advisory_descriptor_sha256, "WRONG_ADVISORY_DESCRIPTOR_HASH")
    verdict = _require(payload, "verdict")
    if verdict not in {"PASS", "REVISE", "BLOCK"}:
        raise ContractError("INVALID_REVIEW_VERDICT", str(verdict))
    return dict(payload)


def validate_orchestrator_envelope_v2(payload: Mapping[str, Any], expected: ExpectedBinding) -> dict[str, Any]:
    _validate_common(payload, expected, ORCHESTRATOR_RESULT_V2_SCHEMA)
    _require_hash(payload, "input_plan_sha256", expected.plan_sha256, "WRONG_PLAN_HASH")
    _require_hash(payload, "input_research_descriptor_sha256", expected.research_descriptor_sha256, "WRONG_RESEARCH_DESCRIPTOR_HASH")
    _require_hash(payload, "input_advisory_descriptor_sha256", expected.advisory_descriptor_sha256, "WRONG_ADVISORY_DESCRIPTOR_HASH")
    _require_hash(payload, "input_review_sha256", expected.review_sha256, "WRONG_REVIEW_HASH")
    _require_equal(payload, "status", "complete", "ORCHESTRATOR_NOT_COMPLETE")
    for list_key in ("changed_files", "commands", "blockers"):
        if not isinstance(payload.get(list_key), list):
            raise ContractError("ORCHESTRATOR_LIST_FIELD_INVALID", list_key)
    return dict(payload)


def validate_review_envelope(payload: Mapping[str, Any], expected: ExpectedBinding) -> dict[str, Any]:
    _validate_common(payload, expected, REVIEW_RESULT_SCHEMA)
    if not expected.plan_sha256:
        raise ContractError("EXPECTED_PLAN_HASH_MISSING")
    _require_equal(payload, "input_plan_sha256", expected.plan_sha256, "WRONG_PLAN_HASH")
    if expected.advisory_sha256 is not None:
        _require_equal(
            payload,
            "input_advisory_sha256",
            expected.advisory_sha256,
            "WRONG_ADVISORY_HASH",
        )
    verdict = _require(payload, "verdict")
    if verdict not in {"PASS", "REVISE", "BLOCK"}:
        raise ContractError("INVALID_REVIEW_VERDICT", str(verdict))
    return dict(payload)


def validate_orchestrator_envelope(
    payload: Mapping[str, Any], expected: ExpectedBinding
) -> dict[str, Any]:
    _validate_common(payload, expected, ORCHESTRATOR_RESULT_SCHEMA)
    if not expected.plan_sha256:
        raise ContractError("EXPECTED_PLAN_HASH_MISSING")
    _require_equal(payload, "input_plan_sha256", expected.plan_sha256, "WRONG_PLAN_HASH")
    expected_review = expected.review_sha256 or None
    actual_review = payload.get("input_review_sha256") or None
    if actual_review != expected_review:
        raise ContractError(
            "WRONG_REVIEW_HASH",
            f"input_review_sha256={actual_review!r}, expected={expected_review!r}",
        )
    status = _require(payload, "status")
    if status not in {"complete", "blocked", "stale-or-conflicted"}:
        raise ContractError("INVALID_ORCHESTRATOR_STATUS", str(status))
    for list_key in ("changed_files", "commands", "blockers"):
        if not isinstance(payload.get(list_key), list):
            raise ContractError("ORCHESTRATOR_LIST_FIELD_INVALID", list_key)
    return dict(payload)


def build_workflow_correlation(
    *,
    workflow_id: str,
    stage: str,
    attempt_index: int,
    nonce: str,
    question_sha256: str,
    source_snapshot_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": WORKFLOW_CORRELATION_SCHEMA,
        "workflow_id": workflow_id,
        "stage": stage,
        "attempt_index": int(attempt_index),
        "nonce": nonce,
        "question_sha256": question_sha256,
        "source_snapshot_sha256": source_snapshot_sha256,
    }


def validate_workflow_correlation(value: Mapping[str, Any]) -> dict[str, Any]:
    _require_equal(
        value,
        "schema",
        WORKFLOW_CORRELATION_SCHEMA,
        "WRONG_WORKFLOW_CORRELATION_SCHEMA",
    )
    for key in (
        "workflow_id",
        "stage",
        "attempt_index",
        "nonce",
        "question_sha256",
        "source_snapshot_sha256",
    ):
        _require(value, key)
    return dict(value)
