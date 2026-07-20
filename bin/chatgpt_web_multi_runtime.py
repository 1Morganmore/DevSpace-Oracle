from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


V1_SCHEMA = "codex.chatgpt.web-multi/v1"
V2_SCHEMA = "codex.chatgpt.web-multi/v2"
SCHEMA = V1_SCHEMA
STAGE_SCHEMA = "codex.chatgpt.web-multi-stage/v1"
RESULT_SCHEMA = "codex.chatgpt.web-multi-result/v1"
V2_SEMANTICS_VERSION = "upstream-parity-v1"
V2_ALLOWED_KEYS = frozenset(
    {
        "schema",
        "workflow_id",
        "project_root",
        "question",
        "source_snapshot_path",
        "source_snapshot_sha256",
        "output_dir",
        "chatgpt_app_name",
        "planner_policy",
        "semantics_version",
        "max_iterations",
        "mode_variant",
        "agbrowse_contract",
        "provider_failure_retry_limit",
        "app_decision_path",
        "chatgpt_app_server_url",
        "timeout_seconds",
        "send_timeout_seconds",
        "session_show_timeout_seconds",
        "recovery_timeout_seconds",
        "safe_pre_submit_retry_limit",
        "pre_submit_retry_deadline_seconds",
        "inline_recovery_round_limit",
        "wave_submission_barrier_timeout_seconds",
        "retry_of_workflow_id",
        "provider_failure_retry_index",
        "provider_failure_parent_run_id",
    }
)
PROMPT_HANDOFF = (
    "The attached prompt file is the user-provided task instruction for this conversation, "
    "not reference or webpage content. Read it completely and follow it. "
    "Return only the output format requested by that file."
)
CANONICAL_CHAT_RE = re.compile(r"^https://chatgpt\.com/c/[A-Za-z0-9_-]+(?:[?#].*)?$")
SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
HEADER_BEGIN = "<<<WEB_MULTI_HEADER_V1>>>"
HEADER_END = "<<<END_WEB_MULTI_HEADER_V1>>>"
PAYLOAD_BEGIN = "<<<WEB_MULTI_PAYLOAD_V1>>>"
PAYLOAD_END = "<<<END_WEB_MULTI_PAYLOAD_V1>>>"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BIN_DIR = Path(__file__).resolve().parent
STATE = _load_module("chatgpt_web_multi_state", BIN_DIR / "chatgpt_agbrowse_state.py")
BRIDGE = _load_module("chatgpt_web_multi_bridge", BIN_DIR / "chatgpt_agbrowse_bridge.py")
PROMPTS = _load_module("chatgpt_web_multi_prompt_profiles", BIN_DIR / "chatgpt_prompt_profiles.py")
UPSTREAM = _load_module("chatgpt_web_multi_upstream", BIN_DIR / "chatgpt_web_multi_upstream.py")


class WebMultiError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


def canonical_bytes(value: Any) -> bytes:
    return UPSTREAM.canonical_json_bytes(value)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_mapping(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise WebMultiError("JSON_OBJECT_REQUIRED", f"expected JSON object: {path}")
    return value


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    temp.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def write_immutable_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise WebMultiError("IMMUTABLE_ARTIFACT_CONFLICT", f"immutable artifact changed: {path}")
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise WebMultiError("IMMUTABLE_ARTIFACT_CONFLICT", f"immutable artifact raced: {path}")


def write_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    write_immutable_bytes(path, json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def _repair_invalid_json_escapes(text: str) -> tuple[str, list[int]]:
    output: list[str] = []
    repaired_offsets: list[int] = []
    in_string = False
    index = 0
    hexdigits = set("0123456789abcdefABCDEF")
    while index < len(text):
        character = text[index]
        if not in_string:
            output.append(character)
            if character == '"':
                in_string = True
            index += 1
            continue
        if character == '"':
            output.append(character)
            in_string = False
            index += 1
            continue
        if character != "\\":
            output.append(character)
            index += 1
            continue
        if index + 1 >= len(text):
            output.append(character)
            index += 1
            continue
        escaped = text[index + 1]
        if escaped in {'"', "\\", "/", "b", "f", "n", "r", "t"}:
            output.extend((character, escaped))
            index += 2
            continue
        if escaped == "u" and index + 5 < len(text) and all(item in hexdigits for item in text[index + 2:index + 6]):
            output.extend(text[index:index + 6])
            index += 6
            continue
        output.extend(("\\", "\\"))
        repaired_offsets.append(index)
        index += 1
    return "".join(output), repaired_offsets


def parse_json_envelope(text: str, *, repair_evidence_path: Path | None = None) -> dict[str, Any]:
    stripped = (text or "").strip()
    if stripped.startswith("JSON\n"):
        stripped = stripped.split("\n", 1)[1]
    fenced = re.fullmatch(r"```json\s*(\{.*\})\s*```", stripped, re.DOTALL | re.IGNORECASE)
    if fenced:
        stripped = fenced.group(1)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as original_exc:
        if "Invalid \\escape" not in str(original_exc):
            raise WebMultiError("STAGE_ENVELOPE_INVALID_JSON", str(original_exc)) from original_exc
        repaired, offsets = _repair_invalid_json_escapes(stripped)
        if not offsets:
            raise WebMultiError("STAGE_ENVELOPE_INVALID_JSON", str(original_exc)) from original_exc
        try:
            value = json.loads(repaired)
        except json.JSONDecodeError as repaired_exc:
            raise WebMultiError("STAGE_ENVELOPE_INVALID_JSON", str(repaired_exc)) from repaired_exc
        if repair_evidence_path is not None:
            write_immutable_json(
                repair_evidence_path,
                {
                    "schema": "codex.chatgpt.json-transport-repair/v1",
                    "repair_kind": "invalid-backslash-escape-only",
                    "original_error": str(original_exc),
                    "original_sha256": sha256_bytes(stripped.encode("utf-8")),
                    "repaired_sha256": sha256_bytes(repaired.encode("utf-8")),
                    "repair_count": len(offsets),
                    "original_offsets": offsets,
                },
            )
    if not isinstance(value, dict):
        raise WebMultiError("STAGE_ENVELOPE_NOT_OBJECT", "stage answer must be one JSON object")
    return value


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    role: str
    lane: int
    iteration: int
    assignment: Any
    input_results: tuple[Path, ...]


@dataclass
class StageResult:
    spec: StageSpec
    payload: dict[str, Any]
    artifact_path: Path
    provenance: dict[str, Any]
    generation_started_at: float
    generation_ended_at: float


StageExecutor = Callable[[StageSpec, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]


def _ordered_marked_sections(text: str, names: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    cursor = 0
    nested_marker = re.compile(r"<<<(?:END_)?[A-Z][A-Z0-9_]*>>>")
    for name in names:
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        begin = f"<<<{name}>>>"
        end = f"<<<END_{name}>>>"
        if not text.startswith(begin, cursor):
            raise WebMultiError("STAGE_PAYLOAD_MARKER_ORDER_INVALID", name)
        value_start = cursor + len(begin)
        finish = text.find(end, value_start)
        if finish < 0:
            raise WebMultiError("STAGE_PAYLOAD_MARKER_MISSING", name)
        value = text[value_start:finish].strip()
        if not value:
            raise WebMultiError("STAGE_PAYLOAD_MARKER_EMPTY", name)
        if nested_marker.search(value):
            raise WebMultiError("STAGE_PAYLOAD_MARKER_NESTED", name)
        values[name] = value
        cursor = finish + len(end)
    if text[cursor:].strip():
        raise WebMultiError("STAGE_PAYLOAD_MARKER_EXTRA", "unexpected text or duplicate marker after final role section")
    return values


def _tagged_transport_parts(text: str) -> tuple[str, str, dict[str, int]]:
    starts = [match.start() for match in re.finditer(re.escape(HEADER_BEGIN), text)]
    for header_start in reversed(starts):
        header_end = text.find(HEADER_END, header_start + len(HEADER_BEGIN))
        if header_end < 0:
            continue
        payload_start = text.find(PAYLOAD_BEGIN, header_end + len(HEADER_END))
        if payload_start < 0:
            continue
        payload_end = text.find(PAYLOAD_END, payload_start + len(PAYLOAD_BEGIN))
        if payload_end < 0:
            continue
        header = text[header_start + len(HEADER_BEGIN):header_end].strip()
        payload = text[payload_start + len(PAYLOAD_BEGIN):payload_end].strip()
        if header and payload:
            return header, payload, {
                "header_start": header_start,
                "header_end": header_end,
                "payload_start": payload_start,
                "payload_end": payload_end,
            }
    raise WebMultiError("STAGE_TAGGED_TRANSPORT_INCOMPLETE", "complete tagged stage transport was not found")


def _tagged_role_payload(
    spec: StageSpec,
    payload_text: str,
    solver_count: int | None,
    manifest_schema: str,
) -> dict[str, Any]:
    if spec.role == "Planner":
        if manifest_schema == V2_SCHEMA:
            return parse_json_envelope(payload_text)
        if solver_count is None:
            raise WebMultiError("LEGACY_SOLVER_COUNT_MISSING", spec.stage_id)
        names = ["PROBLEM_ANALYSIS", *[f"APPROACH_{index}" for index in range(solver_count)]]
        sections = _ordered_marked_sections(payload_text, names)
        return {
            "problem_analysis": sections["PROBLEM_ANALYSIS"],
            "approaches": [sections[f"APPROACH_{index}"] for index in range(solver_count)],
        }
    if spec.role == "Judge":
        sections = _ordered_marked_sections(payload_text, ["JUDGE_DECISION", "RATIONALE"])
        decision = sections["JUDGE_DECISION"]
        fields: dict[str, str] = {}
        for line in decision.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            fields[key.strip().upper()] = value.strip()
        raw_sufficient = fields.get("IS_SUFFICIENT", "").casefold()
        if raw_sufficient not in {"true", "false"}:
            raise WebMultiError("JUDGE_DECISION_INVALID", "IS_SUFFICIENT must be true or false")
        if manifest_schema == V2_SCHEMA:
            def numeric_ids(name: str) -> list[int]:
                result: list[int] = []
                for item in fields.get(name, "").split(","):
                    stripped = item.strip()
                    if re.fullmatch(r"[+-]?\d+", stripped):
                        result.append(int(stripped))
                return result

            raw_best = fields.get("BEST_ID", "").strip()
            return {
                "is_sufficient": raw_sufficient == "true",
                "best_id": int(raw_best) if re.fullmatch(r"[+-]?\d+", raw_best) else None,
                "outstanding_ids": numeric_ids("OUTSTANDING_IDS"),
                "inadequate_ids": numeric_ids("INADEQUATE_IDS"),
                "rationale": sections["RATIONALE"],
            }
        outstanding = [item.strip() for item in fields.get("OUTSTANDING_STAGE_IDS", "").split(",") if item.strip()]
        return {
            "is_sufficient": raw_sufficient == "true",
            "best_stage_id": fields.get("BEST_STAGE_ID", ""),
            "outstanding_stage_ids": outstanding,
            "rationale": sections["RATIONALE"],
        }
    if spec.role == "Organizer":
        return {"final_answer": _ordered_marked_sections(payload_text, ["FINAL_ANSWER"])["FINAL_ANSWER"]}
    content = _ordered_marked_sections(payload_text, ["CONTENT"])["CONTENT"]
    if spec.role in {"Merger", "FinalMerger"}:
        assignment = spec.assignment if isinstance(spec.assignment, Mapping) else {}
        return {"content": content, "source_stage_ids": list(assignment.get("source_stage_ids") or [])}
    return {"content": content, "assumptions": [], "counterexamples": []}


def parse_stage_answer(
    text: str,
    spec: StageSpec,
    *,
    solver_count: int | None,
    manifest_schema: str = V1_SCHEMA,
    repair_evidence_path: Path | None = None,
    transport_evidence_path: Path | None = None,
) -> dict[str, Any]:
    if HEADER_BEGIN not in text:
        return parse_json_envelope(text, repair_evidence_path=repair_evidence_path)
    header_text, payload_text, positions = _tagged_transport_parts(text)
    header = parse_json_envelope(header_text, repair_evidence_path=repair_evidence_path)
    expected_header_keys = {
        "schema", "workflow_id", "parent_run_id", "stage_id", "role", "lane", "iteration",
        "prompt_sha256", "challenge_nonce", "evidence_map_sha256", "read_receipts",
    }
    if set(header) != expected_header_keys:
        raise WebMultiError(
            "STAGE_HEADER_KEYS_INVALID",
            "tagged header keys are not exact",
            {"expected": sorted(expected_header_keys), "actual": sorted(header)},
        )
    envelope = {
        **header,
        "payload": _tagged_role_payload(spec, payload_text, solver_count, manifest_schema),
    }
    if transport_evidence_path is not None:
        write_immutable_json(
            transport_evidence_path,
            {
                "schema": "codex.chatgpt.web-multi-stage-transport/v1",
                "transport": "tagged-header-plus-raw-payload",
                "role": spec.role,
                "raw_answer_sha256": sha256_bytes(text.encode("utf-8")),
                "header_sha256": sha256_bytes(header_text.encode("utf-8")),
                "payload_sha256": sha256_bytes(payload_text.encode("utf-8")),
                **positions,
            },
        )
    return envelope


def validate_manifest(value: Mapping[str, Any], manifest_path: Path) -> dict[str, Any]:
    schema = str(value.get("schema") or "")
    if schema not in {V1_SCHEMA, V2_SCHEMA}:
        raise WebMultiError("MANIFEST_SCHEMA_INVALID", "web Multi-GPT manifest schema mismatch")
    required = (
        "workflow_id", "project_root", "question", "source_snapshot_path",
        "source_snapshot_sha256", "output_dir", "chatgpt_app_name",
    )
    for key in required:
        if value.get(key) in (None, ""):
            raise WebMultiError("MANIFEST_FIELD_MISSING", key)
    if schema == V2_SCHEMA:
        unknown = set(value) - V2_ALLOWED_KEYS
        if unknown:
            raise WebMultiError(
                "MANIFEST_V2_KEYS_INVALID",
                "web Multi-GPT v2 manifest keys are not exact",
                {"unknown": sorted(unknown)},
            )
        if "solver_count" in value:
            raise WebMultiError(
                "MANIFEST_V2_SOLVER_COUNT_FORBIDDEN",
                "solver_count is forbidden in dynamic v2 manifests, including null",
            )
        planner_policy = str(value.get("planner_policy") or "")
        semantics_version = str(value.get("semantics_version") or "")
        if planner_policy not in UPSTREAM.PLANNER_POLICIES:
            raise WebMultiError("PLANNER_POLICY_INVALID", planner_policy)
        if semantics_version != V2_SEMANTICS_VERSION:
            raise WebMultiError("SEMANTICS_VERSION_INVALID", semantics_version)
        solver_count: int | None = None
    else:
        solver_count = int(value.get("solver_count") or 3)
        planner_policy = "legacy-fixed"
        semantics_version = "legacy-v1"
    max_iterations = int(value.get("max_iterations") or 2)
    provider_failure_retry_limit = int(
        value.get("provider_failure_retry_limit")
        if value.get("provider_failure_retry_limit") is not None
        else 1
    )
    if schema == V1_SCHEMA and solver_count not in {2, 3, 4}:
        raise WebMultiError("SOLVER_COUNT_UNSUPPORTED", "solver_count must be 2..4")
    if not 1 <= max_iterations <= 5:
        raise WebMultiError("MAX_ITERATIONS_UNSUPPORTED", "max_iterations must be 1..5")
    if not 0 <= provider_failure_retry_limit <= 2:
        raise WebMultiError(
            "PROVIDER_FAILURE_RETRY_LIMIT_UNSUPPORTED",
            "provider_failure_retry_limit must be 0..2",
        )
    # V2 is the public High-only path.  V1 keeps its frozen historical
    # Very High default for recovery/comparison compatibility.
    mode_variant = str(value.get("mode_variant") or ("High" if schema == V2_SCHEMA else "Very High"))
    if schema == V2_SCHEMA and mode_variant != "High":
        raise WebMultiError(
            "MANIFEST_V2_MODE_VARIANT_INVALID",
            "upstream-parity v2 requires exact mode_variant High",
        )
    if schema == V1_SCHEMA and mode_variant not in {"Very High", "High"}:
        raise WebMultiError("MODE_VARIANT_UNSUPPORTED", "web Multi-GPT supports Very High or High")
    root = STATE.canonical_project_root(str(value["project_root"]))
    snapshot = Path(str(value["source_snapshot_path"])).expanduser().resolve(strict=True)
    output = Path(str(value["output_dir"])).expanduser().resolve()
    contract = Path(str(value.get("agbrowse_contract") or Path.home() / ".codex" / "contracts" / "agbrowse-0.1.18.json")).resolve()
    if not contract.is_file():
        raise WebMultiError("AGBROWSE_CONTRACT_MISSING", str(contract))
    result = dict(value)
    result.update(
        {
            **({"solver_count": solver_count} if solver_count is not None else {}),
            "manifest_schema": schema,
            "planner_policy": planner_policy,
            "semantics_version": semantics_version,
            "max_iterations": max_iterations,
            "provider_failure_retry_limit": provider_failure_retry_limit,
            "mode_variant": mode_variant,
            "project_root": str(root),
            "source_snapshot_path": str(snapshot),
            "output_dir": str(output),
            "agbrowse_contract": str(contract),
            "manifest_path": str(manifest_path.resolve()),
        }
    )
    return result


class WebMultiRuntime:
    def __init__(
        self,
        manifest_path: Path,
        *,
        state_root: Path | None = None,
        stage_executor: StageExecutor | None = None,
        bridge_factory: Callable[[], Any] | None = None,
    ):
        self.manifest_path = manifest_path.expanduser().resolve(strict=True)
        self.manifest = validate_manifest(read_mapping(self.manifest_path), self.manifest_path)
        self.store = STATE.RunStore(state_root)
        self.stage_executor = stage_executor
        self.bridge_factory = bridge_factory or (lambda: BRIDGE.Bridge(state_root=self.store.root))
        self.workflow_id = str(self.manifest["workflow_id"])
        self.workflow_dir = Path(self.manifest["output_dir"]) / self.workflow_id
        self.stages_dir = self.workflow_dir / "stages"
        self.runtime_state_path = self.workflow_dir / "runtime-state.json"
        self.evidence_map_path = self.workflow_dir / "evidence-map.json"
        self.result_path = self.workflow_dir / "result.json"
        self.parent: dict[str, Any] | None = None
        self.contract = BRIDGE.read_contract(Path(self.manifest["agbrowse_contract"]))
        self._accepted: dict[str, StageResult] = {}
        self._children: dict[str, dict[str, Any]] = {}
        self._identities: dict[str, set[str]] = {"session_id": set(), "target_id": set(), "conversation_url": set()}
        self._intervals: list[tuple[float, float, str]] = []
        self._planner_descriptor: dict[str, Any] | None = None
        self._fallback_provenance: list[dict[str, Any]] = []
        self._mutex = threading.RLock()

    @property
    def _is_v2(self) -> bool:
        return self.manifest["manifest_schema"] == V2_SCHEMA

    def _lane_count(self, spec: StageSpec | None = None) -> int:
        if not self._is_v2:
            return int(self.manifest["solver_count"])
        if self._planner_descriptor is not None:
            return int(self._planner_descriptor["actual_count"])
        if spec is not None and spec.role == "Planner":
            return 10
        raise WebMultiError(
            "PLANNER_TOPOLOGY_NOT_RESOLVED",
            "dynamic lane count is unavailable before the accepted Planner descriptor",
        )

    def _build_planner_descriptor(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        adapted = UPSTREAM.apply_planner_policy(payload, str(self.manifest["planner_policy"]))
        base = {
            "schema": "codex.chatgpt.web-multi-planner-descriptor/v1",
            "policy": self.manifest["planner_policy"],
            "problem_analysis": adapted["problem_analysis"],
            "observed_count": adapted["observed_count"],
            "retained_ordered_approaches": adapted["approaches"],
            "actual_count": adapted["retained_count"],
            "source_payload_sha256": sha256_bytes(canonical_bytes(dict(payload))),
        }
        return {**base, "descriptor_sha256": sha256_bytes(canonical_bytes(base))}

    def _resolve_planner_descriptor(self, planner: StageResult) -> dict[str, Any]:
        if not self._is_v2:
            raise WebMultiError("PLANNER_DESCRIPTOR_NOT_V2", planner.spec.stage_id)
        descriptor = planner.payload.get("planner_descriptor")
        if not isinstance(descriptor, dict):
            raise WebMultiError("PLANNER_DESCRIPTOR_MISSING", planner.spec.stage_id)
        supplied_hash = str(descriptor.get("descriptor_sha256") or "")
        hash_input = {key: value for key, value in descriptor.items() if key != "descriptor_sha256"}
        expected_hash = sha256_bytes(canonical_bytes(hash_input))
        required = {
            "schema",
            "policy",
            "problem_analysis",
            "observed_count",
            "retained_ordered_approaches",
            "actual_count",
            "source_payload_sha256",
            "descriptor_sha256",
        }
        approaches = descriptor.get("retained_ordered_approaches")
        if (
            set(descriptor) != required
            or descriptor.get("schema") != "codex.chatgpt.web-multi-planner-descriptor/v1"
            or descriptor.get("policy") != self.manifest["planner_policy"]
            or supplied_hash != expected_hash
            or not isinstance(approaches, list)
            or int(descriptor.get("actual_count") or 0) != len(approaches)
            or not 1 <= len(approaches) <= 10
        ):
            raise WebMultiError(
                "PLANNER_DESCRIPTOR_INVALID",
                planner.spec.stage_id,
                {"expected_sha256": expected_hash, "actual_sha256": supplied_hash},
            )
        self._planner_descriptor = dict(descriptor)
        return self._planner_descriptor

    def _source_evidence_map(self) -> dict[str, Any]:
        snapshot_path = Path(self.manifest["source_snapshot_path"])
        snapshot_bytes = snapshot_path.read_bytes()
        snapshot = read_mapping(snapshot_path)
        expected_snapshot = str(self.manifest["source_snapshot_sha256"])
        observed_file_hash = sha256_bytes(snapshot_bytes)
        observed_semantic = str(snapshot.get("snapshot_sha256") or "")
        if expected_snapshot not in {observed_file_hash, observed_semantic}:
            raise WebMultiError(
                "SOURCE_SNAPSHOT_HASH_MISMATCH",
                "source snapshot bytes or semantic hash changed",
                {"expected": expected_snapshot, "file_sha256": observed_file_hash, "semantic_sha256": observed_semantic},
            )
        raw_files = snapshot.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise WebMultiError("SOURCE_SNAPSHOT_FILES_EMPTY", "source snapshot needs exact file entries")
        files: list[dict[str, Any]] = []
        seen: set[str] = set()
        snapshot_root = Path(str(snapshot.get("workspace_root") or self.manifest["project_root"])).expanduser().resolve()
        for item in raw_files:
            if not isinstance(item, dict) or not item.get("path"):
                raise WebMultiError("SOURCE_ENTRY_INVALID", "source entries require a path")
            raw_path = Path(str(item["path"])).expanduser()
            path = (raw_path if raw_path.is_absolute() else snapshot_root / raw_path).resolve(strict=True)
            normalized = os.path.normcase(str(path))
            if normalized in seen or not path.is_file() or path.is_symlink():
                raise WebMultiError("SOURCE_ENTRY_INVALID", f"source path duplicated or unsafe: {path}")
            seen.add(normalized)
            data = path.read_bytes()
            observed_hash = sha256_bytes(data)
            expected_hash = str(item.get("sha256") or item.get("file_sha256") or observed_hash)
            expected_bytes = int(item.get("bytes") if item.get("bytes") is not None else len(data))
            if observed_hash != expected_hash or len(data) != expected_bytes:
                raise WebMultiError(
                    "SOURCE_ENTRY_DRIFT",
                    f"source bytes changed: {path}",
                    {"expected_sha256": expected_hash, "actual_sha256": observed_hash, "expected_bytes": expected_bytes, "actual_bytes": len(data)},
                )
            files.append({"path": str(path), "sha256": observed_hash, "bytes": len(data)})
        value = {
            "schema": "codex.chatgpt.web-multi-evidence-map/v1",
            "workflow_id": self.workflow_id,
            "source_snapshot_path": str(snapshot_path),
            "source_snapshot_sha256": expected_snapshot,
            "files": files,
        }
        value["evidence_map_sha256"] = sha256_bytes(canonical_bytes(value))
        return value

    def _role_instruction(self, spec: StageSpec) -> str:
        instructions = {
            "Planner": (
                "Create branch briefs, not full solutions, under the declared Planner policy. "
                "Include one direct baseline and one wildcard reframe among materially distinct approaches."
                if self._is_v2
                else "Create exactly solver_count materially distinct branch briefs, including a direct baseline and a wildcard reframe."
            ),
            "Solver": (
                "Produce a complete standalone solution for the assigned branch. "
                "Reason independently from the original task; mention assumptions, risks, or counterexamples only when material."
            ),
            "InitialRefiner": "Engineer feasibility for only the assigned Solver result and return concrete corrections or additions.",
            "Merger": "Create a coherent new synthesis from the assigned anonymous candidates; never concatenate or average them.",
            "LoopRefiner": "Close the most consequential gaps in the assigned synthesis without importing unassigned sibling content.",
            "Judge": "Adversarially judge sufficiency and relative quality against the original task and evidence.",
            "FinalMerger": "Create one bounded alternative synthesis from the assigned anonymous finalists.",
            "FinalRefiner": "Author one clear implementation-ready advisory decision from the selected synthesis.",
            "Organizer": (
                "Answer the original user request faithfully. Repair material omissions when evidence supports it, "
                "and do not expose internal transcripts."
            ),
        }
        return instructions[spec.role]

    @staticmethod
    def _role_profile_name(role: str) -> str:
        profiles = {
            "Planner": "web-branch-designer",
            "Solver": "web-proposal-builder",
            "InitialRefiner": "web-feasibility-engineer",
            "Merger": "web-synthesis-architect",
            "LoopRefiner": "web-gap-closer",
            "Judge": "web-rubric-judge",
            "FinalMerger": "web-alternative-synthesizer",
            "FinalRefiner": "web-decision-author",
            "Organizer": "web-final-responder",
        }
        try:
            return profiles[role]
        except KeyError as exc:
            raise WebMultiError("WEB_MULTI_ROLE_PROFILE_UNKNOWN", role) from exc

    @staticmethod
    def _role_gets_source_evidence(role: str) -> bool:
        return role in {"Planner", "Solver", "Judge", "Organizer"}

    @staticmethod
    def _payload_reliability_instruction(spec: StageSpec) -> str:
        if spec.role == "Planner":
            return (
                "For stream reliability, keep the complete raw payload near 16,000 characters or less: "
                "state shared invariants once in PROBLEM_ANALYSIS and keep each approach distinct without repeating the full problem."
            )
        return (
            "Be complete but non-repetitive; avoid copying assigned inputs verbatim so the provider can finish the tagged payload reliably."
        )

    def _role_payload_contract(self, spec: StageSpec) -> str:
        if spec.role == "Planner":
            if self._is_v2:
                return (
                    '{"problem_analysis":"<analysis>",'
                    '"approaches":[{"name":"<name>","description":"<description>",'
                    '"methodology":"<methodology>"}]}\n'
                    f"Policy: {self.manifest['planner_policy']}. Return one strict JSON object here; "
                    "do not use role-section markers inside it."
                )
            approach_sections = "\n".join(
                f"<<<APPROACH_{index}>>>\n<approach {index} raw text>\n<<<END_APPROACH_{index}>>>"
                for index in range(int(self.manifest["solver_count"]))
            )
            return (
                "<<<PROBLEM_ANALYSIS>>>\n<raw problem analysis>\n<<<END_PROBLEM_ANALYSIS>>>\n"
                f"{approach_sections}"
            )
        if spec.role == "Judge":
            if self._is_v2:
                return (
                    "<<<JUDGE_DECISION>>>\n"
                    "IS_SUFFICIENT=true or false\n"
                    "BEST_ID=<one 1-based candidate id or empty>\n"
                    "OUTSTANDING_IDS=<comma-separated 1-based candidate ids or empty>\n"
                    "INADEQUATE_IDS=<comma-separated 1-based candidate ids or empty>\n"
                    "<<<END_JUDGE_DECISION>>>\n"
                    "<<<RATIONALE>>>\n<raw rationale>\n<<<END_RATIONALE>>>"
                )
            return (
                "<<<JUDGE_DECISION>>>\n"
                "IS_SUFFICIENT=true or false\n"
                "BEST_STAGE_ID=<one assigned stage id or empty>\n"
                "OUTSTANDING_STAGE_IDS=<comma-separated assigned stage ids or empty>\n"
                "<<<END_JUDGE_DECISION>>>\n"
                "<<<RATIONALE>>>\n<raw rationale>\n<<<END_RATIONALE>>>"
            )
        if spec.role == "Organizer":
            return "<<<FINAL_ANSWER>>>\n<raw user-facing final answer>\n<<<END_FINAL_ANSWER>>>"
        return "<<<CONTENT>>>\n<complete raw role result; include assumptions, risks, and counterexamples only when material>\n<<<END_CONTENT>>>"

    def _reuse_stage_artifacts(
        self,
        spec: StageSpec,
        parent: Mapping[str, Any],
        evidence_map: Mapping[str, Any],
        *,
        stage_dir: Path,
        context_path: Path,
        prompt_path: Path,
        manifest_path: Path,
        assigned_files: list[dict[str, Any]],
        input_paths: list[str],
    ) -> dict[str, Any] | None:
        profile = PROMPTS.resolve_profile(self._role_profile_name(spec.role))
        existing = [path.exists() for path in (context_path, prompt_path, manifest_path)]
        if not any(existing):
            return None
        if not all(existing):
            raise WebMultiError(
                "STAGE_ARTIFACT_SET_INCOMPLETE",
                spec.stage_id,
                {"context": existing[0], "prompt": existing[1], "manifest": existing[2]},
            )
        for path in (context_path, prompt_path, manifest_path):
            if not path.is_file() or path.is_symlink():
                raise WebMultiError("STAGE_ARTIFACT_REUSE_UNSAFE", str(path))
        context = read_mapping(context_path)
        manifest = read_mapping(manifest_path)
        expected_context = {
            "schema": "codex.chatgpt.web-multi-stage-context/v1",
            "workflow_id": self.workflow_id,
            "parent_run_id": parent["run_id"],
            "stage_id": spec.stage_id,
            "role": spec.role,
            "lane": spec.lane,
            "iteration": spec.iteration,
            "root_question": self.manifest["question"],
            "evidence_map_path": str(self.evidence_map_path),
            "evidence_map_sha256": evidence_map["evidence_map_sha256"],
            "assigned_files": assigned_files,
            "input_stage_result_paths": input_paths,
            "assignment": spec.assignment,
            "prompt_profile": profile.name,
            "prompt_profile_receipt": profile.receipt(),
            "context_policy": profile.context_policy,
        }
        if self._is_v2:
            expected_context["planner_descriptor_sha256"] = (
                self._planner_descriptor.get("descriptor_sha256")
                if self._planner_descriptor is not None
                else None
            )
        else:
            expected_context["solver_count"] = self.manifest["solver_count"]
        mismatches = {
            key: {"expected": value, "actual": context.get(key)}
            for key, value in expected_context.items()
            if context.get(key) != value
        }
        prompt_hash = sha256_file(prompt_path)
        nonce = str(context.get("challenge_nonce") or "")
        if (
            mismatches
            or str(context.get("prompt_sha256") or "") != prompt_hash
            or not re.fullmatch(r"[0-9a-f]{64}", nonce)
            or nonce.encode("utf-8") in prompt_path.read_bytes()
        ):
            raise WebMultiError(
                "STAGE_ARTIFACT_CONTEXT_MISMATCH",
                spec.stage_id,
                {"mismatches": mismatches, "prompt_hash": prompt_hash},
            )
        correlation = manifest.get("workflow_correlation") if isinstance(manifest.get("workflow_correlation"), dict) else {}
        expected_manifest = {
            "project_root": self.manifest["project_root"],
            "question": PROMPT_HANDOFF,
            "mode_label": "GPT-5.6",
            "mode_variant": self.manifest["mode_variant"],
            "app_policy": "required",
            "chatgpt_app_name": self.manifest["chatgpt_app_name"],
            "app_selection_transport": "inline-pill-reuse",
            "prompt_transport": "file",
            "prompt_file": str(prompt_path),
            "prompt_file_sha256": prompt_hash,
            "files": [str(prompt_path)],
            "read_only_paths": [str(context_path), str(self.evidence_map_path), *input_paths],
            "gpt_operation_mode": profile.task_kind,
            "prompt_profile": profile.name,
            "prompt_profile_receipt": profile.receipt(),
            "write_mode": "read-only",
            "allowed_paths": [],
            "agbrowse_contract": self.manifest["agbrowse_contract"],
            "provider_url": "https://chatgpt.com/",
            "send_limit": 1,
        }
        manifest_mismatches = {
            key: {"expected": value, "actual": manifest.get(key)}
            for key, value in expected_manifest.items()
            if manifest.get(key) != value
        }
        correlation_expected = {
            "schema": "codex.chatgpt.workflow-correlation/v1",
            "workflow_id": self.workflow_id,
            "stage": spec.stage_id,
            "attempt_index": 1,
            "question_sha256": sha256_bytes(canonical_bytes(self.manifest["question"])),
            "source_snapshot_sha256": self.manifest["source_snapshot_sha256"],
        }
        correlation_mismatches = {
            key: {"expected": value, "actual": correlation.get(key)}
            for key, value in correlation_expected.items()
            if correlation.get(key) != value
        }
        if (
            manifest_mismatches
            or correlation_mismatches
            or not re.fullmatch(r"[0-9a-f]{32}", str(correlation.get("nonce") or ""))
        ):
            raise WebMultiError(
                "STAGE_ARTIFACT_MANIFEST_MISMATCH",
                spec.stage_id,
                {"manifest": manifest_mismatches, "correlation": correlation_mismatches},
            )
        return {
            "stage_dir": stage_dir,
            "context_path": context_path,
            "prompt_path": prompt_path,
            "manifest_path": manifest_path,
            "context": context,
            "manifest": manifest,
        }

    def _stage_artifacts(self, spec: StageSpec, parent: Mapping[str, Any], evidence_map: Mapping[str, Any]) -> dict[str, Any]:
        stage_dir = self.stages_dir / SAFE_COMPONENT_RE.sub("-", spec.stage_id)
        context_path = stage_dir / "stage-context.json"
        prompt_path = stage_dir / "prompt.txt"
        manifest_path = stage_dir / "stage.manifest.json"
        profile = PROMPTS.resolve_profile(self._role_profile_name(spec.role))
        assigned_files = [
            {**dict(item), "path": Path(str(item["path"])).resolve().as_posix()}
            for item in (evidence_map["files"] if self._role_gets_source_evidence(spec.role) else [])
        ]
        assigned_keys = {
            os.path.normcase(str(Path(str(item["path"])).resolve()))
            for item in assigned_files
        }
        input_paths: list[str] = []
        for raw_input in spec.input_results:
            input_path = Path(raw_input).resolve(strict=True)
            if not input_path.is_file() or input_path.is_symlink():
                raise WebMultiError("INPUT_STAGE_RESULT_INVALID", str(input_path))
            normalized = os.path.normcase(str(input_path))
            if normalized in assigned_keys:
                raise WebMultiError("ASSIGNED_FILE_DUPLICATED", str(input_path))
            assigned_keys.add(normalized)
            input_paths.append(input_path.as_posix())
            assigned_files.append(
                {
                    "path": input_path.as_posix(),
                    "sha256": sha256_file(input_path),
                    "bytes": input_path.stat().st_size,
                }
            )
        reused = self._reuse_stage_artifacts(
            spec,
            parent,
            evidence_map,
            stage_dir=stage_dir,
            context_path=context_path,
            prompt_path=prompt_path,
            manifest_path=manifest_path,
            assigned_files=assigned_files,
            input_paths=input_paths,
        )
        if reused is not None:
            return reused
        nonce = secrets.token_hex(32)
        prompt = PROMPTS.render_prompt(
            profile.name,
            original_task=str(self.manifest["question"]),
            context_note=(
                f"Read the un-attached stage context through the selected app: {context_path}. "
                "Read every assigned immutable file and input result listed there. "
                "Do not use sibling results or incumbent narratives that are not assigned."
            ),
            stage_mission=(
                f"You are the fresh {spec.role} node for web Multi-GPT workflow {self.workflow_id}. "
                f"{self._role_instruction(spec)} {self._payload_reliability_instruction(spec)}"
            ),
            output_instructions=(
                "Return no prose outside the tagged transport and do not use a Markdown fence.\n"
                f"Start with the exact line {HEADER_BEGIN} and end the small JSON header with the exact line {HEADER_END}.\n"
                f"Then start the raw payload with {PAYLOAD_BEGIN} and finish it with {PAYLOAD_END}.\n"
                f"The header must be one compact JSON object using schema {STAGE_SCHEMA}; do not put the long role payload in that JSON.\n"
                "The header keys must be exactly: schema, workflow_id, parent_run_id, stage_id, role, lane, iteration, "
                "prompt_sha256, challenge_nonce, evidence_map_sha256, read_receipts.\n"
                "Copy every header binding from stage-context.json exactly; never guess or regenerate a binding.\n"
                "read_receipts must contain path, sha256, and bytes for every assigned immutable file.\n"
                "Copy every assigned_files.path exactly with forward slashes; never convert a path to Windows backslashes.\n"
                "Do not JSON-escape the long payload. Put it as raw text only between the payload markers and never repeat a global transport marker inside it.\n"
                "Role markers must appear exactly once in the shown order; never nest, repeat, quote, or discuss a role marker inside a section.\n"
                f"Inside the payload markers, follow this exact role layout:\n{self._role_payload_contract(spec)}"
            ),
        )
        prompt_bytes = prompt.encode("utf-8")
        prompt_hash = sha256_bytes(prompt_bytes)
        context = {
            "schema": "codex.chatgpt.web-multi-stage-context/v1",
            "workflow_id": self.workflow_id,
            "parent_run_id": parent["run_id"],
            "stage_id": spec.stage_id,
            "role": spec.role,
            "lane": spec.lane,
            "iteration": spec.iteration,
            "prompt_sha256": prompt_hash,
            "challenge_nonce": nonce,
            "root_question": self.manifest["question"],
            "evidence_map_path": str(self.evidence_map_path),
            "evidence_map_sha256": evidence_map["evidence_map_sha256"],
            "assigned_files": assigned_files,
            "input_stage_result_paths": input_paths,
            "assignment": spec.assignment,
            "prompt_profile": profile.name,
            "prompt_profile_receipt": profile.receipt(),
            "context_policy": profile.context_policy,
        }
        if self._is_v2:
            context["planner_descriptor_sha256"] = (
                self._planner_descriptor.get("descriptor_sha256")
                if self._planner_descriptor is not None
                else None
            )
        else:
            context["solver_count"] = self.manifest["solver_count"]
        if nonce.encode("utf-8") in prompt_bytes:
            raise WebMultiError("NONCE_EXPOSED_IN_PROMPT", spec.stage_id)
        write_immutable_bytes(prompt_path, prompt_bytes)
        write_immutable_json(context_path, context)
        manifest = {
            "project_root": self.manifest["project_root"],
            "question": PROMPT_HANDOFF,
            "mode_label": "GPT-5.6",
            "mode_variant": self.manifest["mode_variant"],
            "app_policy": "required",
            "chatgpt_app_name": self.manifest["chatgpt_app_name"],
            "app_decision_path": self.manifest.get("app_decision_path"),
            "chatgpt_app_server_url": self.manifest.get("chatgpt_app_server_url"),
            "app_selection_transport": "inline-pill-reuse",
            "app_attestation_scope": "parent-workflow",
            "prompt_transport": "file",
            "prompt_file": str(prompt_path),
            "prompt_file_sha256": prompt_hash,
            "files": [str(prompt_path)],
            "read_only_paths": [str(context_path), str(self.evidence_map_path), *input_paths],
            "gpt_operation_mode": profile.task_kind,
            "prompt_profile": profile.name,
            "prompt_profile_receipt": profile.receipt(),
            "workflow_correlation": {
                "schema": "codex.chatgpt.workflow-correlation/v1",
                "workflow_id": self.workflow_id,
                "stage": spec.stage_id,
                "attempt_index": 1,
                "nonce": secrets.token_hex(16),
                "question_sha256": sha256_bytes(canonical_bytes(self.manifest["question"])),
                "source_snapshot_sha256": self.manifest["source_snapshot_sha256"],
            },
            "write_mode": "read-only",
            "allowed_paths": [],
            "agbrowse_contract": self.manifest["agbrowse_contract"],
            "provider_url": "https://chatgpt.com/",
            "send_limit": 1,
            "parallel_lane_count": self._lane_count(spec),
            "timeout_seconds": int(self.manifest.get("timeout_seconds") or 3600),
            "send_timeout_seconds": int(self.manifest.get("send_timeout_seconds") or 600),
            "session_show_timeout_seconds": int(self.manifest.get("session_show_timeout_seconds") or 90),
            "recovery_timeout_seconds": int(self.manifest.get("recovery_timeout_seconds") or 180),
        }
        write_immutable_json(manifest_path, {k: v for k, v in manifest.items() if v is not None})
        return {
            "stage_dir": stage_dir,
            "context_path": context_path,
            "prompt_path": prompt_path,
            "manifest_path": manifest_path,
            "context": context,
            "manifest": {k: v for k, v in manifest.items() if v is not None},
        }

    def _load_child_index(self, parent: Mapping[str, Any]) -> None:
        paths = self.store.paths(STATE.canonical_project_root(parent["project_root"]), str(parent["run_id"]))
        children: dict[str, dict[str, Any]] = {}
        for _, record in self.store._parent_children(paths.runs_dir, str(parent["run_id"])):
            stage_id = str(record.get("stage_id") or "")
            if not stage_id or stage_id in children:
                raise WebMultiError("DUPLICATE_CHILD_STAGE", stage_id)
            record["run_dir"] = str(paths.runs_dir / str(record["run_id"]))
            children[stage_id] = record
        self._children = children

    def _existing_child(self, stage_id: str) -> dict[str, Any] | None:
        with self._mutex:
            child = self._children.get(stage_id)
            return dict(child) if child else None

    def _fake_execute(self, child: dict[str, Any], spec: StageSpec, artifacts: Mapping[str, Any]) -> tuple[dict[str, Any], float, float]:
        run_dir = str(child["run_dir"])
        _, current = self.store.load(run_dir)
        if current["phase"] == "CREATED":
            current = self.store.transition(run_dir, "PREFLIGHTED")
        if current["phase"] == "PREFLIGHTED":
            current = self.store.transition(run_dir, "LEASED")
        if current["phase"] == "LEASED":
            current = self.store.claim_child_send(run_dir)
        started = time.monotonic()
        returned = dict(self.stage_executor(spec, artifacts["context"], child)) if self.stage_executor else {}
        ended = time.monotonic()
        envelope = returned.get("envelope") if isinstance(returned.get("envelope"), dict) else returned
        session_id = str(returned.get("session_id") or f"S-{child['run_id']}")
        target_id = str(returned.get("target_id") or f"T-{child['run_id']}")
        url = str(returned.get("conversation_url") or f"https://chatgpt.com/c/{child['run_id']}")
        answer_path = Path(run_dir) / "answer.md"
        answer_bytes = ("JSON\n" + json.dumps(envelope, ensure_ascii=False, indent=2)).encode("utf-8")
        write_immutable_bytes(answer_path, answer_bytes)
        _, current = self.store.load(run_dir)
        if current["phase"] == "SEND_STARTED":
            current = self.store.transition(
                run_dir, "SUBMITTED", session_id=session_id, target_id=target_id,
                submission_receipt={"fake": True, "stage_id": spec.stage_id},
            )
        if current["phase"] == "SUBMITTED":
            current = self.store.transition(run_dir, "URL_BOUND", conversation_url=url)
        descriptor = {"path": str(answer_path), "sha256": sha256_bytes(answer_bytes), "bytes": len(answer_bytes), "provider_status": "complete"}
        if current["phase"] in {"URL_BOUND", "RESPONSE_IN_PROGRESS"}:
            current = self.store.transition(run_dir, "RESULT_CAPTURED", result=descriptor)
        if current["phase"] == "RESULT_CAPTURED":
            current = self.store.transition(run_dir, "VERIFIED")
        if current["phase"] == "VERIFIED":
            self.store.transition(run_dir, "COMPLETE")
        self.store.record_child_cleanup(
            run_dir,
            {"ok": True, "state": "closed-and-absent", "target_id": target_id, "conversation_url": url},
        )
        return dict(envelope), started, ended

    def _actual_execute(
        self,
        child: dict[str, Any],
        spec: StageSpec,
        *,
        submission_barrier: threading.Barrier | None = None,
    ) -> tuple[dict[str, Any], float, float]:
        run_dir = str(child["run_dir"])
        bridge = self.bridge_factory()
        try:
            _, current = self.store.load(run_dir)
            if current["phase"] == "CREATED":
                current = self.store.transition(run_dir, "PREFLIGHTED")
            if current["phase"] == "SEND_REJECTED":
                current = bridge.authorize_pre_submit_retry(run_dir)
            if current["phase"] in {"PREFLIGHTED", "LEASED", "PREFLIGHT_BLOCKED", "BLOCKED_APP_TRANSACTION", "SEND_REJECTED"}:
                retry_limit = max(0, min(5, int(self.manifest.get("safe_pre_submit_retry_limit") or 5)))
                retry_started = time.monotonic()
                retry_deadline_seconds = max(
                    15.0,
                    min(
                        600.0,
                        float(self.manifest.get("pre_submit_retry_deadline_seconds") or 120),
                    ),
                )
                retry_index = 0
                while True:
                    try:
                        current = bridge.send(run_dir)
                    except Exception:
                        _, latest = self.store.load(run_dir)
                        safe_retry = bool(
                            latest.get("phase") in {"PREFLIGHT_BLOCKED", "BLOCKED_APP_TRANSACTION"}
                            and int(latest.get("send_attempt_count") or 0) == 0
                            and not latest.get("session_id")
                            and not latest.get("conversation_url")
                            and latest.get("submission_receipt") is None
                            and retry_index < retry_limit
                        )
                        if not safe_retry:
                            raise
                        if time.monotonic() - retry_started >= retry_deadline_seconds:
                            raise WebMultiError(
                                "PRE_SUBMIT_RETRY_DEADLINE_EXHAUSTED",
                                spec.stage_id,
                                {
                                    "run_dir": run_dir,
                                    "retry_count": retry_index,
                                    "deadline_seconds": retry_deadline_seconds,
                                    "send_attempt_count": int(latest.get("send_attempt_count") or 0),
                                },
                            ) from exc
                        retry_index += 1
                        time.sleep(min(2.0, 0.5 * retry_index))
                        current = latest
                        continue
                    safe_retry = bool(
                        current.get("phase") in {"PREFLIGHT_BLOCKED", "BLOCKED_APP_TRANSACTION"}
                        and int(current.get("send_attempt_count") or 0) == 0
                        and not current.get("session_id")
                        and not current.get("conversation_url")
                        and current.get("submission_receipt") is None
                        and retry_index < retry_limit
                    )
                    if not safe_retry:
                        break
                    if time.monotonic() - retry_started >= retry_deadline_seconds:
                        raise WebMultiError(
                            "PRE_SUBMIT_RETRY_DEADLINE_EXHAUSTED",
                            spec.stage_id,
                            {
                                "run_dir": run_dir,
                                "retry_count": retry_index,
                                "deadline_seconds": retry_deadline_seconds,
                                "send_attempt_count": int(current.get("send_attempt_count") or 0),
                            },
                        )
                    retry_index += 1
                    time.sleep(min(2.0, 0.5 * retry_index))
        except BaseException:
            if submission_barrier is not None:
                submission_barrier.abort()
            raise
        started = time.monotonic()
        if submission_barrier is not None:
            barrier_timeout = int(
                self.manifest.get("wave_submission_barrier_timeout_seconds")
                or int(self.manifest.get("send_timeout_seconds") or 600) + 600
            )
            try:
                submission_barrier.wait(timeout=barrier_timeout)
            except threading.BrokenBarrierError:
                # If a sibling fails before its one allowed send, finish exact
                # recovery/poll/cleanup for this already-submitted child. The
                # sibling exception remains the wave failure authority.
                pass
        recovery_round_limit = max(1, min(3, int(self.manifest.get("inline_recovery_round_limit") or 2)))
        recovery_round = 0
        while True:
            if current["phase"] in {
                "SEND_STARTED",
                "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
                "RECOVERY_REQUIRED",
                "RECOVERING",
                "BLOCKED_RECOVERY_EXHAUSTED",
            }:
                if recovery_round >= recovery_round_limit:
                    break
                current = bridge.recover(run_dir)
                recovery_round += 1
            if current["phase"] in {"SUBMITTED", "URL_BOUND", "RESPONSE_IN_PROGRESS", "RECOVERING"}:
                current = bridge.poll(run_dir)
                if current["phase"] in {
                    "SEND_STARTED",
                    "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
                    "RECOVERY_REQUIRED",
                    "RECOVERING",
                    "BLOCKED_RECOVERY_EXHAUSTED",
                }:
                    continue
            break
        ended = time.monotonic()
        if current["phase"] == "PROVIDER_FAILED_TERMINAL":
            try:
                cleanup = bridge.cleanup_completed(run_dir, explicit_user_request=True)
                current = self.store.record_child_cleanup(run_dir, cleanup)
            except Exception as exc:
                self.store.record_child_cleanup(run_dir, {"ok": False, "state": "cleanup-pending", "detail": str(exc)})
                raise WebMultiError(
                    "CHILD_PROVIDER_FAILED_TERMINAL_CLEANUP_FAILED",
                    spec.stage_id,
                    {"run_dir": run_dir, "detail": str(exc)},
                ) from exc
            raise WebMultiError(
                "CHILD_PROVIDER_FAILED_TERMINAL",
                spec.stage_id,
                {
                    "run_dir": run_dir,
                    "conversation_url": current.get("conversation_url"),
                    "terminal_block_code": current.get("terminal_block_code"),
                    "cleanup_state": current.get("owned_tab_state"),
                },
            )
        if current["phase"] != "COMPLETE":
            raise WebMultiError("CHILD_NOT_COMPLETE", f"{spec.stage_id}: {current['phase']}")
        result = current.get("result") if isinstance(current.get("result"), dict) else {}
        answer_path = Path(str(result.get("path") or ""))
        if not answer_path.is_file() or sha256_file(answer_path) != str(result.get("sha256") or ""):
            raise WebMultiError("CHILD_ANSWER_IDENTITY_INVALID", spec.stage_id)
        answer_text = answer_path.read_text(encoding="utf-8")
        try:
            cleanup = bridge.cleanup_completed(run_dir, explicit_user_request=True)
            self.store.record_child_cleanup(run_dir, cleanup)
        except Exception as exc:
            self.store.record_child_cleanup(run_dir, {"ok": False, "state": "cleanup-pending", "detail": str(exc)})
            raise WebMultiError("CHILD_COMPLETED_TAB_CLEANUP_FAILED", spec.stage_id, {"detail": str(exc)}) from exc
        envelope = parse_stage_answer(
            answer_text,
            spec,
            solver_count=(
                int(self.manifest["solver_count"])
                if self.manifest["manifest_schema"] == V1_SCHEMA
                else None
            ),
            manifest_schema=str(self.manifest["manifest_schema"]),
            repair_evidence_path=Path(run_dir) / "json-transport-repair.json",
            transport_evidence_path=Path(run_dir) / "stage-transport.json",
        )
        return envelope, started, ended

    def _validate_envelope(
        self,
        envelope: Mapping[str, Any],
        spec: StageSpec,
        artifacts: Mapping[str, Any],
        child: Mapping[str, Any],
        evidence_map: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str]:
        context = artifacts["context"]
        expected = {
            "schema": STAGE_SCHEMA,
            "workflow_id": self.workflow_id,
            "parent_run_id": self.parent["run_id"],
            "stage_id": spec.stage_id,
            "role": spec.role,
            "lane": spec.lane,
            "iteration": spec.iteration,
            "prompt_sha256": context["prompt_sha256"],
            "challenge_nonce": context["challenge_nonce"],
            "evidence_map_sha256": evidence_map["evidence_map_sha256"],
        }
        mismatches = {key: {"expected": value, "actual": envelope.get(key)} for key, value in expected.items() if envelope.get(key) != value}
        if mismatches:
            raise WebMultiError("STAGE_BINDING_MISMATCH", spec.stage_id, mismatches)
        expected_keys = set(expected) | {"read_receipts", "payload"}
        if set(envelope) != expected_keys:
            raise WebMultiError(
                "STAGE_ENVELOPE_KEYS_INVALID",
                spec.stage_id,
                {"expected": sorted(expected_keys), "actual": sorted(envelope)},
            )
        receipts = envelope.get("read_receipts")
        if not isinstance(receipts, list):
            raise WebMultiError("APP_READ_RECEIPTS_MISSING", spec.stage_id)
        actual_receipts = {
            str(item.get("path")): {
                "sha256": str(item.get("sha256") or ""),
                "bytes": int(item.get("bytes")) if item.get("bytes") is not None else -1,
            }
            for item in receipts
            if isinstance(item, dict) and isinstance(item.get("path"), str) and item.get("path")
        }
        expected_receipts = {
            str(item["path"]): {"sha256": item["sha256"], "bytes": item["bytes"]}
            for item in context["assigned_files"]
        }
        for exact_path, descriptor in expected_receipts.items():
            path = Path(exact_path).resolve()
            if (
                not path.is_file()
                or path.is_symlink()
                or sha256_file(path) != descriptor["sha256"]
                or path.stat().st_size != descriptor["bytes"]
            ):
                raise WebMultiError("ASSIGNED_FILE_DRIFT", spec.stage_id, {"path": str(path)})
        if (
            len(actual_receipts) != len(receipts)
            or len(actual_receipts) != len(expected_receipts)
            or actual_receipts != expected_receipts
        ):
            raise WebMultiError("APP_READ_RECEIPTS_MISMATCH", spec.stage_id)
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise WebMultiError("STAGE_PAYLOAD_INVALID", spec.stage_id)
        if spec.role == "Planner":
            approaches = payload.get("approaches")
            if self._is_v2:
                try:
                    descriptor = self._build_planner_descriptor(payload)
                except (TypeError, ValueError) as exc:
                    raise WebMultiError(
                        "PLANNER_POLICY_REJECTED",
                        spec.stage_id,
                        {"detail": str(exc), "policy": self.manifest["planner_policy"]},
                    ) from exc
                payload = {
                    "problem_analysis": descriptor["problem_analysis"],
                    "approaches": descriptor["retained_ordered_approaches"],
                    "planner_descriptor": descriptor,
                }
            elif not isinstance(approaches, list) or len(approaches) != int(self.manifest["solver_count"]):
                raise WebMultiError("PLANNER_APPROACH_COUNT_INVALID", spec.stage_id)
        elif spec.role == "Judge":
            if not isinstance(payload.get("is_sufficient"), bool):
                raise WebMultiError("JUDGE_PAYLOAD_INVALID", spec.stage_id)
            if self._is_v2:
                for key in ("outstanding_ids", "inadequate_ids"):
                    if not isinstance(payload.get(key), list):
                        raise WebMultiError("JUDGE_PAYLOAD_INVALID", spec.stage_id, {"field": key})
            else:
                assignment = spec.assignment if isinstance(spec.assignment, Mapping) else {}
                assigned = {str(item) for item in assignment.get("candidate_stage_ids") or []}
                best = str(payload.get("best_stage_id") or "")
                outstanding = {str(item) for item in payload.get("outstanding_stage_ids") or []}
                if (best and best not in assigned) or not outstanding.issubset(assigned):
                    raise WebMultiError("JUDGE_STAGE_ID_INVALID", spec.stage_id)
                if payload["is_sufficient"] and not best:
                    raise WebMultiError("JUDGE_BEST_STAGE_MISSING", spec.stage_id)
        elif spec.role == "Organizer":
            if not str(payload.get("final_answer") or "").strip():
                raise WebMultiError("ORGANIZER_ANSWER_MISSING", spec.stage_id)
        elif not payload:
            raise WebMultiError("STAGE_PAYLOAD_EMPTY", spec.stage_id)
        proof = {
            "challenge_nonce": envelope["challenge_nonce"],
            "evidence_map_sha256": envelope["evidence_map_sha256"],
            "read_receipts": receipts,
        }
        return dict(payload), sha256_bytes(canonical_bytes(proof))

    def _accept_identity(self, record: Mapping[str, Any], stage_id: str) -> None:
        values = {
            "session_id": str(record.get("session_id") or ""),
            "target_id": str(record.get("current_target_id") or record.get("target_id") or ""),
            "conversation_url": str(record.get("conversation_url") or ""),
        }
        if not values["session_id"] or not values["target_id"] or not CANONICAL_CHAT_RE.fullmatch(values["conversation_url"]):
            raise WebMultiError("CHILD_IDENTITY_INCOMPLETE", stage_id, values)
        with self._mutex:
            for key, value in values.items():
                if value in self._identities[key]:
                    raise WebMultiError("CHILD_IDENTITY_REUSED", stage_id, {"field": key, "value": value})
                self._identities[key].add(value)

    def _execute_stage(
        self,
        spec: StageSpec,
        evidence_map: Mapping[str, Any],
        *,
        submission_barrier: threading.Barrier | None = None,
    ) -> StageResult:
        cached_path = self.stages_dir / SAFE_COMPONENT_RE.sub("-", spec.stage_id) / "stage-result.json"
        if cached_path.is_file():
            value = read_mapping(cached_path)
            if self._is_v2:
                outcome = value.get("semantic_outcome")
                if (
                    not isinstance(outcome, dict)
                    or outcome.get("state") != "accepted"
                    or outcome.get("stage_id") != spec.stage_id
                ):
                    raise WebMultiError("STAGE_SEMANTIC_OUTCOME_INVALID", spec.stage_id)
            result = StageResult(
                spec=spec,
                payload=dict(value["payload"]),
                artifact_path=cached_path,
                provenance=dict(value["provenance"]),
                generation_started_at=float(value.get("generation_started_at") or 0.0),
                generation_ended_at=float(value.get("generation_ended_at") or 0.0),
            )
            self._accept_identity(result.provenance, spec.stage_id)
            with self._mutex:
                self._accepted[spec.stage_id] = result
                self._intervals.append((result.generation_started_at, result.generation_ended_at, spec.stage_id))
            return result

        artifacts = self._stage_artifacts(spec, self.parent, evidence_map)
        child = self._existing_child(spec.stage_id)
        if child is None:
            with self._mutex:
                child = self._children.get(spec.stage_id)
                if child is None:
                    child = self.store.create_child_run(
                        parent_run_dir=self.parent["run_dir"],
                        manifest_path=artifacts["manifest_path"],
                        agbrowse_contract=self.contract,
                        role=spec.role,
                        lane=spec.lane,
                        iteration=spec.iteration,
                        stage_id=spec.stage_id,
                        send_limit=1,
                    )
                    self._children[spec.stage_id] = dict(child)
                else:
                    child = dict(child)
        if self.stage_executor:
            envelope, started, ended = self._fake_execute(child, spec, artifacts)
        else:
            envelope, started, ended = self._actual_execute(
                child,
                spec,
                submission_barrier=submission_barrier,
            )
        _, latest = self.store.load(child["run_dir"])
        payload, proof_hash = self._validate_envelope(envelope, spec, artifacts, latest, evidence_map)
        self._accept_identity(latest, spec.stage_id)
        envelope_path = artifacts["stage_dir"] / "stage-envelope.json"
        write_immutable_json(envelope_path, dict(envelope))
        provenance = {
            "stage_id": spec.stage_id,
            "role": spec.role,
            "lane": spec.lane,
            "iteration": spec.iteration,
            "parent_run_id": latest["parent_run_id"],
            "run_id": latest["run_id"],
            "session_id": latest["session_id"],
            "target_id": latest["current_target_id"],
            "conversation_url": latest["conversation_url"],
            "prompt_sha256": latest["prompt_sha256"],
            "answer_sha256": latest["result"]["sha256"],
            "app_read_proof_sha256": proof_hash,
            "send_attempt_count": latest["send_attempt_count"],
        }
        result_value = {
            "schema": "codex.chatgpt.web-multi-stage-result/v1",
            "stage_id": spec.stage_id,
            "payload": payload,
            "provenance": provenance,
            "generation_started_at": started,
            "generation_ended_at": ended,
        }
        if self._is_v2:
            result_value["semantic_outcome"] = {
                "schema": "codex.chatgpt.web-multi-semantic-outcome/v1",
                "state": "accepted",
                "stage_id": spec.stage_id,
                "source_stage_ids": [path.parent.name for path in spec.input_results],
                "payload_sha256": sha256_bytes(canonical_bytes(payload)),
            }
        result_path = artifacts["stage_dir"] / "stage-result.json"
        write_immutable_json(result_path, result_value)
        result = StageResult(spec, payload, result_path, provenance, started, ended)
        with self._mutex:
            self._accepted[spec.stage_id] = result
            self._intervals.append((started, ended, spec.stage_id))
        self._checkpoint("STAGE_ACCEPTED")
        return result

    def _stage_needs_wave_submission(self, spec: StageSpec) -> bool:
        cached_path = self.stages_dir / SAFE_COMPONENT_RE.sub("-", spec.stage_id) / "stage-result.json"
        if cached_path.is_file():
            return False
        child = self._existing_child(spec.stage_id)
        if child is None:
            return True
        _, current = self.store.load(str(child["run_dir"]))
        return str(current.get("phase") or "") in {
            "CREATED",
            "PREFLIGHTED",
            "LEASED",
            "PREFLIGHT_BLOCKED",
            "BLOCKED_APP_TRANSACTION",
            "SEND_REJECTED",
        }

    def _wave(self, specs: list[StageSpec], evidence_map: Mapping[str, Any]) -> list[StageResult]:
        if len(specs) == 1:
            return [self._execute_stage(specs[0], evidence_map)]
        results: dict[str, StageResult] = {}
        submission_stage_ids = {
            spec.stage_id for spec in specs if self._stage_needs_wave_submission(spec)
        }
        submission_barrier = (
            threading.Barrier(len(submission_stage_ids))
            if self.stage_executor is None and len(submission_stage_ids) > 1
            else None
        )
        with ThreadPoolExecutor(max_workers=len(specs), thread_name_prefix="web-multi-gpt") as pool:
            futures = {
                pool.submit(
                    self._execute_stage,
                    spec,
                    evidence_map,
                    submission_barrier=(
                        submission_barrier if spec.stage_id in submission_stage_ids else None
                    ),
                ): spec.stage_id
                for spec in specs
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return [results[spec.stage_id] for spec in specs]

    def _paired_wave(
        self,
        first_specs: list[StageSpec],
        second_spec_factory: Callable[[int, StageResult], StageSpec],
        evidence_map: Mapping[str, Any],
    ) -> list[StageResult]:
        if not first_specs:
            return []
        submission_ids = {
            spec.stage_id for spec in first_specs if self._stage_needs_wave_submission(spec)
        }
        first_barrier = (
            threading.Barrier(len(submission_ids))
            if self.stage_executor is None and len(submission_ids) > 1
            else None
        )
        slots: list[StageResult | None] = [None] * len(first_specs)
        failures: list[BaseException | None] = [None] * len(first_specs)

        def run_pair(index: int, first_spec: StageSpec) -> StageResult:
            first = self._execute_stage(
                first_spec,
                evidence_map,
                submission_barrier=(
                    first_barrier if first_spec.stage_id in submission_ids else None
                ),
            )
            return self._execute_stage(second_spec_factory(index, first), evidence_map)

        with ThreadPoolExecutor(
            max_workers=len(first_specs),
            thread_name_prefix="web-multi-gpt-pair",
        ) as pool:
            future_indexes = {
                pool.submit(run_pair, index, spec): index
                for index, spec in enumerate(first_specs)
            }
            for future in as_completed(future_indexes):
                index = future_indexes[future]
                try:
                    slots[index] = future.result()
                except BaseException as exc:
                    failures[index] = exc
        for failure in failures:
            if failure is not None:
                raise failure
        if any(item is None for item in slots):
            raise WebMultiError(
                "ORDERED_STAGE_SLOT_MISSING",
                "paired wave did not fill every input-index slot",
            )
        return [item for item in slots if item is not None]

    def _paired_wave_with_initial_fallback(
        self,
        solver_specs: list[StageSpec],
        refiner_factory: Callable[[int, StageResult], StageSpec],
        evidence_map: Mapping[str, Any],
    ) -> list[StageResult]:
        """Keep Solver→InitialRefiner lanes parallel while retaining upstream fallback."""
        if not solver_specs:
            return []
        submission_ids = {spec.stage_id for spec in solver_specs if self._stage_needs_wave_submission(spec)}
        barrier = (
            threading.Barrier(len(submission_ids))
            if self.stage_executor is None and len(submission_ids) > 1
            else None
        )
        slots: list[StageResult | None] = [None] * len(solver_specs)
        failures: list[BaseException | None] = [None] * len(solver_specs)

        def run_pair(index: int, solver_spec: StageSpec) -> StageResult:
            solver = self._execute_stage(
                solver_spec,
                evidence_map,
                submission_barrier=(barrier if solver_spec.stage_id in submission_ids else None),
            )
            refiner_spec = refiner_factory(index, solver)
            try:
                return self._execute_stage(refiner_spec, evidence_map)
            except BaseException as exc:
                if not self._is_v2_fallback_error(exc):
                    raise
                self._record_v2_fallback(
                    failed_spec=refiner_spec,
                    replacement=solver,
                    exc=exc,
                )
                return solver

        with ThreadPoolExecutor(max_workers=len(solver_specs), thread_name_prefix="web-multi-gpt-pair") as pool:
            futures = {pool.submit(run_pair, index, spec): index for index, spec in enumerate(solver_specs)}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    slots[index] = future.result()
                except BaseException as exc:
                    failures[index] = exc
        for failure in failures:
            if failure is not None:
                raise failure
        if any(item is None for item in slots):
            raise WebMultiError("ORDERED_STAGE_SLOT_MISSING", "initial fallback wave did not fill every input-index slot")
        return [item for item in slots if item is not None]

    @staticmethod
    def _candidate(stage: StageResult, logical_id: int, approach_name: str) -> dict[str, Any]:
        content = str(
            stage.payload.get("content")
            or stage.payload.get("final_answer")
            or ""
        )
        return {
            "id": logical_id,
            "approachName": approach_name,
            "content": content,
            "stage_id": stage.spec.stage_id,
            "_stage_result": stage,
        }

    def _checkpoint(self, phase: str) -> None:
        with self._mutex:
            value = {
                "schema": "codex.chatgpt.web-multi-runtime-state/v1",
                "workflow_id": self.workflow_id,
                "phase": phase,
                "parent_run_dir": self.parent.get("run_dir") if self.parent else None,
                "accepted_stage_ids": sorted(self._accepted),
                "updated_at": STATE.utc_now(),
            }
            write_json_atomic(self.runtime_state_path, value)

    @staticmethod
    def _is_v2_fallback_error(exc: BaseException) -> bool:
        """Only completed-provider and answer-semantics failures may degrade.

        In particular, transport, identity, receipt, cleanup, and every
        uncertain-send state retain their normal fail-closed behavior.
        """
        if not isinstance(exc, WebMultiError):
            return False
        if exc.code == "CHILD_PROVIDER_FAILED_TERMINAL":
            return True
        return exc.code in {
            "STAGE_PAYLOAD_MARKER_ORDER_INVALID",
            "STAGE_PAYLOAD_MARKER_MISSING",
            "STAGE_PAYLOAD_MARKER_EMPTY",
            "STAGE_PAYLOAD_MARKER_NESTED",
            "STAGE_PAYLOAD_MARKER_EXTRA",
            "STAGE_TAGGED_TRANSPORT_INCOMPLETE",
            "STAGE_PAYLOAD_INVALID",
            "STAGE_PAYLOAD_EMPTY",
            "ORGANIZER_ANSWER_MISSING",
        }

    def _record_v2_fallback(
        self,
        *,
        failed_spec: StageSpec,
        replacement: StageResult,
        exc: BaseException,
    ) -> dict[str, Any]:
        if not self._is_v2 or not self._is_v2_fallback_error(exc):
            raise exc
        error = exc if isinstance(exc, WebMultiError) else None
        value = {
            "schema": "codex.chatgpt.web-multi-fallback-provenance/v1",
            "kind": "provider-terminal" if error and error.code == "CHILD_PROVIDER_FAILED_TERMINAL" else "semantic",
            "failed_stage_id": failed_spec.stage_id,
            "failed_role": failed_spec.role,
            "failure_code": error.code if error else type(exc).__name__,
            "failure_evidence": dict(error.evidence) if error else {},
            "replacement_stage_id": replacement.spec.stage_id,
            "replacement_role": replacement.spec.role,
            "replacement_provenance": dict(replacement.provenance),
        }
        path = self.stages_dir / SAFE_COMPONENT_RE.sub("-", failed_spec.stage_id) / "fallback-provenance.json"
        write_immutable_json(path, value)
        recorded = {**value, "artifact_path": str(path), "artifact_sha256": sha256_file(path)}
        with self._mutex:
            self._fallback_provenance.append(recorded)
        self._checkpoint("STAGE_FALLBACK_ACCEPTED")
        return recorded

    def _max_concurrency(self) -> int:
        events: list[tuple[float, int]] = []
        for start, end, _ in self._intervals:
            if end >= start:
                events.append((start, 1))
                events.append((end, -1))
        active = maximum = 0
        for _, delta in sorted(events, key=lambda item: (item[0], -item[1])):
            active += delta
            maximum = max(maximum, active)
        return maximum

    def _run_v2_result(self, evidence_map: Mapping[str, Any]) -> dict[str, Any]:
        planner = self._execute_stage(
            StageSpec("planner", "Planner", 0, 0, self.manifest["question"], tuple()),
            evidence_map,
        )
        descriptor = self._resolve_planner_descriptor(planner)
        approaches = list(descriptor["retained_ordered_approaches"])
        solver_specs = [
            StageSpec(
                f"solver-{lane}",
                "Solver",
                lane,
                0,
                {
                    "planner_index": lane,
                    "approach": approaches[lane],
                    "planner_descriptor_sha256": descriptor["descriptor_sha256"],
                },
                tuple(),
            )
            for lane in range(len(approaches))
        ]

        def initial_refiner(index: int, solver: StageResult) -> StageSpec:
            return StageSpec(
                f"initial-refiner-{index}",
                "InitialRefiner",
                index,
                0,
                {
                    "solver_stage_id": solver.spec.stage_id,
                    "planner_descriptor_sha256": descriptor["descriptor_sha256"],
                },
                (solver.artifact_path,),
            )

        initial = self._paired_wave_with_initial_fallback(
            solver_specs,
            initial_refiner,
            evidence_map,
        )
        candidates = [
            self._candidate(result, index, str(approaches[index]["name"]))
            for index, result in enumerate(initial)
        ]
        outstanding_ids: list[int] = list(range(len(candidates)))
        sufficient = False

        for iteration in range(1, int(self.manifest["max_iterations"]) + 1):
            groups = UPSTREAM.build_merger_groups_upstream(
                candidates,
                outstanding_ids=outstanding_ids,
            )
            if not groups:
                raise WebMultiError("CANDIDATE_SET_EMPTY", f"iteration {iteration}")
            merger_specs = [
                StageSpec(
                    f"iter-{iteration}-merger-{seed}",
                    "Merger",
                    seed,
                    iteration,
                    {
                        "seed": seed,
                        "source_stage_ids": [str(item["stage_id"]) for item in group],
                        "source_candidate_ids": [int(item["id"]) for item in group],
                    },
                    tuple(item["_stage_result"].artifact_path for item in group),
                )
                for seed, group in enumerate(groups)
            ]

            def loop_refiner(index: int, merger: StageResult) -> StageSpec:
                return StageSpec(
                    f"iter-{iteration}-refiner-{index}",
                    "LoopRefiner",
                    index,
                    iteration,
                    {"merger_stage_id": merger.spec.stage_id, "seed": index},
                    (merger.artifact_path,),
                )

            refined = self._paired_wave(merger_specs, loop_refiner, evidence_map)
            candidates = [
                self._candidate(result, index, f"Merger {index + 1}")
                for index, result in enumerate(refined)
            ]
            judge = self._execute_stage(
                StageSpec(
                    f"iter-{iteration}-judge",
                    "Judge",
                    0,
                    iteration,
                    {
                        "candidate_catalog": [
                            {
                                "id": index + 1,
                                "stage_id": item["stage_id"],
                            }
                            for index, item in enumerate(candidates)
                        ]
                    },
                    tuple(item["_stage_result"].artifact_path for item in candidates),
                ),
                evidence_map,
            )
            transition = UPSTREAM.apply_judgment_upstream(candidates, judge.payload)
            candidates = list(transition["candidates"])
            outstanding_ids = list(transition["outstanding_ids"])
            if transition["is_sufficient"]:
                sufficient = True
                break

        if not sufficient:
            final_candidates = UPSTREAM.select_final_subset_upstream(candidates, outstanding_ids)
            final_candidates = [
                {**dict(candidate), "id": index}
                for index, candidate in enumerate(final_candidates)
            ]
            if not final_candidates:
                raise WebMultiError("FINAL_CANDIDATE_SET_EMPTY", self.workflow_id)
            if len(final_candidates) > 1:
                final_groups = UPSTREAM.build_merger_groups_upstream(final_candidates)
                final_specs = [
                    StageSpec(
                        f"final-merger-{seed}",
                        "FinalMerger",
                        seed,
                        int(self.manifest["max_iterations"]) + 1,
                        {
                            "seed": seed,
                            "source_stage_ids": [str(item["stage_id"]) for item in group],
                            "source_candidate_ids": [int(item["id"]) for item in group],
                        },
                        tuple(item["_stage_result"].artifact_path for item in group),
                    )
                    for seed, group in enumerate(final_groups)
                ]
                final_mergers = self._wave(final_specs, evidence_map)
                if len(final_mergers) != len(final_specs):
                    raise WebMultiError(
                        "FINAL_MERGER_SLOT_INVALID",
                        "seed-ordered FinalMerger slots are incomplete",
                    )
                # Upstream consumes finalMerge.solutions[0].  The collector above
                # projects results by captured seed input index, never by
                # completion order, stage ID, content, or returned source IDs.
                final_input = final_mergers[0]
            else:
                final_input = final_candidates[0]["_stage_result"]
            final_refiner = self._execute_stage(
                StageSpec(
                    "final-refiner",
                    "FinalRefiner",
                    0,
                    int(self.manifest["max_iterations"]) + 1,
                    {"source_stage_id": final_input.spec.stage_id},
                    (final_input.artifact_path,),
                ),
                evidence_map,
            )
            candidates = [self._candidate(final_refiner, 0, "Final")]

        organizer_spec = StageSpec(
            "organizer",
            "Organizer",
            0,
            int(self.manifest["max_iterations"]) + 2,
            {"candidate_stage_ids": [str(item["stage_id"]) for item in candidates]},
            tuple(item["_stage_result"].artifact_path for item in candidates),
        )
        try:
            organizer = self._execute_stage(organizer_spec, evidence_map)
            organizer_result = organizer.payload["final_answer"]
        except BaseException as exc:
            if not self._is_v2_fallback_error(exc):
                raise
            if not candidates:
                raise WebMultiError("ORGANIZER_FALLBACK_CANDIDATE_MISSING", self.workflow_id) from exc
            # `candidates` preserves the last valid Judge selection order; if
            # no Judge narrows it, it is the durable input order.
            fallback_candidate = candidates[0]["_stage_result"]
            self._record_v2_fallback(
                failed_spec=organizer_spec,
                replacement=fallback_candidate,
                exc=exc,
            )
            organizer_result = str(candidates[0]["content"])
        return {
            "schema": RESULT_SCHEMA,
            "workflow_id": self.workflow_id,
            "organizer_result": organizer_result,
            "provenance": [self._accepted[key].provenance for key in sorted(self._accepted)],
            "role_session_target_url_provenance": [
                {
                    "stage_id": self._accepted[key].provenance["stage_id"],
                    "role": self._accepted[key].provenance["role"],
                    "session_id": self._accepted[key].provenance["session_id"],
                    "target_id": self._accepted[key].provenance["target_id"],
                    "conversation_url": self._accepted[key].provenance["conversation_url"],
                }
                for key in sorted(self._accepted)
            ],
            "fallback_provenance": list(self._fallback_provenance),
            "evidence_map_sha256": evidence_map["evidence_map_sha256"],
            "max_concurrent_child_generations": self._max_concurrency(),
            "advisory_only": True,
            "mode_variant": self.manifest["mode_variant"],
            "manifest_schema": V2_SCHEMA,
            "semantics_version": self.manifest["semantics_version"],
            "planner_policy": self.manifest["planner_policy"],
            "planner_descriptor_sha256": descriptor["descriptor_sha256"],
            "observed_solver_count": descriptor["observed_count"],
            "actual_solver_count": descriptor["actual_count"],
        }

    def dry_run(self) -> dict[str, Any]:
        evidence_map = self._source_evidence_map()
        if self._is_v2:
            iterations = int(self.manifest["max_iterations"])
            minimum = 6 if self.manifest["planner_policy"] == "strict-6-10" else 1
            return {
                "ok": True,
                "status": "dry-run",
                "workflow_id": self.workflow_id,
                "manifest_schema": V2_SCHEMA,
                "planner_policy": self.manifest["planner_policy"],
                "semantics_version": self.manifest["semantics_version"],
                "solver_count": None,
                "planner_solver_count_range": [minimum, 10],
                "max_iterations": iterations,
                "provider_failure_retry_limit": int(self.manifest["provider_failure_retry_limit"]),
                "worst_case_conversation_count": 1 + 10 + 10 + iterations * 17 + 9 + 1,
                "evidence_map_sha256": evidence_map["evidence_map_sha256"],
                "source_file_count": len(evidence_map["files"]),
                "app_policy": "required",
                "mode_label": "GPT-5.6",
                "mode_variant": self.manifest["mode_variant"],
                "browser_backend": "contract-validated-unmodified-agbrowse",
                "agbrowse_contract": self.manifest["agbrowse_contract"],
                "browser_started": False,
            }
        n = int(self.manifest["solver_count"])
        iterations = int(self.manifest["max_iterations"])
        worst_case = 1 + n + n + iterations * (2 * n + 1) + 2 + 1
        return {
            "ok": True,
            "status": "dry-run",
            "workflow_id": self.workflow_id,
            "solver_count": n,
            "max_iterations": iterations,
            "provider_failure_retry_limit": int(self.manifest["provider_failure_retry_limit"]),
            "worst_case_conversation_count": worst_case,
            "evidence_map_sha256": evidence_map["evidence_map_sha256"],
            "source_file_count": len(evidence_map["files"]),
            "app_policy": "required",
            "mode_label": "GPT-5.6",
            "mode_variant": self.manifest["mode_variant"],
            "browser_backend": "contract-validated-unmodified-agbrowse",
            "agbrowse_contract": self.manifest["agbrowse_contract"],
            "browser_started": False,
        }

    def _recover_parent_children(self, parent: dict[str, Any]) -> None:
        paths = self.store.paths(STATE.canonical_project_root(parent["project_root"]), str(parent["run_id"]))
        unresolved: list[dict[str, Any]] = []
        for _, child in self.store._parent_children(paths.runs_dir, str(parent["run_id"])):
            phase = str(child.get("phase") or "")
            if phase in {"CREATED", "PREFLIGHTED", "LEASED"}:
                child_dir = paths.runs_dir / str(child["run_id"])
                safe_zero_send = bool(
                    not (child_dir / "send.claim").exists()
                    and int(child.get("send_attempt_count") or 0) == 0
                    and not child.get("session_id")
                    and not child.get("conversation_url")
                    and child.get("submission_receipt") is None
                    and child.get("result") is None
                    and (
                        not child.get("current_target_id")
                        or (
                            str(child.get("owned_tab_state") or "") in {"closed-and-absent", "already-absent"}
                            and not bool(child.get("cleanup_pending"))
                            and int(child.get("owned_open_tabs") or 0) == 0
                        )
                    )
                )
                if safe_zero_send:
                    if str(parent.get("phase") or "") in {"PARENT_RECOVERY_REQUIRED", "PARENT_DRAINING"}:
                        self.store.transition(
                            str(child_dir),
                            "PREFLIGHT_BLOCKED",
                            block_code="PARENT_RECOVERY_ZERO_SEND_CHILD",
                        )
                else:
                    unresolved.append({"run_id": child["run_id"], "phase": phase})
                continue
            if phase == "COMPLETE":
                if (
                    str(child.get("owned_tab_state") or "") not in {"closed-and-absent", "already-absent"}
                    or bool(child.get("cleanup_pending"))
                    or int(child.get("owned_open_tabs") or 0) != 0
                ):
                    bridge = self.bridge_factory()
                    run_dir = str(paths.runs_dir / str(child["run_id"]))
                    cleanup = bridge.cleanup_completed(run_dir, explicit_user_request=True)
                    self.store.record_child_cleanup(run_dir, cleanup)
                continue
            if phase == "SEND_REJECTED":
                bridge = self.bridge_factory()
                run_dir = str(paths.runs_dir / str(child["run_id"]))
                authorized = bridge.authorize_pre_submit_retry(run_dir)
                if not (
                    isinstance(authorized.get("pre_submit_retry_authority"), dict)
                    and authorized["pre_submit_retry_authority"].get("eligible") is True
                ):
                    unresolved.append({"run_id": child["run_id"], "phase": authorized.get("phase")})
                continue
            if phase in {"PREFLIGHT_BLOCKED", "BLOCKED_APP_TRANSACTION", "CANCELLED_PRE_SUBMISSION"}:
                continue
            if phase in STATE.UNCERTAIN_OR_SUBMITTED_PHASES:
                bridge = self.bridge_factory()
                run_dir = str(paths.runs_dir / str(child["run_id"]))
                recovered = bridge.recover(run_dir)
                if recovered["phase"] in {"SUBMITTED", "URL_BOUND", "RESPONSE_IN_PROGRESS", "RECOVERING"}:
                    recovered = bridge.poll(run_dir)
                if recovered["phase"] == "COMPLETE":
                    cleanup = bridge.cleanup_completed(run_dir, explicit_user_request=True)
                    self.store.record_child_cleanup(run_dir, cleanup)
                else:
                    unresolved.append({"run_id": child["run_id"], "phase": recovered["phase"]})
        if unresolved:
            raise WebMultiError("PARENT_CHILD_RECOVERY_PENDING", "exact children remain unresolved", {"children": unresolved})

    def _fail_closed_draining_parent(
        self,
        *,
        code: str,
        message: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        failure = {
            "code": code,
            "message": message,
            "evidence": dict(evidence or {}),
        }
        parent = self.store.finalize_parent(
            self.parent["run_dir"],
            "PARENT_FAILED_CLOSED",
            failure=failure,
        )
        if parent["phase"] != "PARENT_FAILED_CLOSED":
            raise WebMultiError("PARENT_FINALIZATION_PENDING", parent["phase"], parent.get("failure"))
        self._checkpoint("PARENT_FAILED_CLOSED")
        raise WebMultiError(
            code,
            message,
            {**dict(evidence or {}), "parent_phase": parent["phase"], "project_lock_released": True},
        )

    def _parent_requires_runtime_recovery(self, parent: Mapping[str, Any]) -> bool:
        paths = self.store.paths(STATE.canonical_project_root(parent["project_root"]), str(parent["run_id"]))
        for child_state, child in self.store._parent_children(paths.runs_dir, str(parent["run_id"])):
            phase = str(child.get("phase") or "")
            if phase in STATE.UNCERTAIN_OR_SUBMITTED_PHASES:
                return True
            if bool(child.get("cleanup_pending")) or int(child.get("owned_open_tabs") or 0) != 0:
                return True
            if (child_state.parent / "send.claim").exists() and phase not in {
                "SEND_REJECTED",
                "COMPLETE",
                "PROVIDER_FAILED_TERMINAL",
                "ABANDONED_UNCERTAIN",
            }:
                return True
        return False

    def run(self, *, resume_parent: Path | None = None) -> dict[str, Any]:
        evidence_map = self._source_evidence_map()
        self.workflow_dir.mkdir(parents=True, exist_ok=True)
        write_immutable_json(self.evidence_map_path, evidence_map)
        if resume_parent:
            _, resume_record = self.store.load(resume_parent)
            if resume_record.get("phase") == "PARENT_FAILED_CLOSED":
                self.parent = self.store.reopen_failed_parent_workflow(
                    resume_parent,
                    self.manifest_path,
                )
            else:
                self.parent = self.store.resume_parent_workflow(resume_parent, self.manifest_path)
            self.parent["run_dir"] = str(Path(resume_parent).resolve())
            if self.parent["phase"] in {"PARENT_RECOVERY_REQUIRED", "PARENT_DRAINING"}:
                self._recover_parent_children(self.parent)
                if not self.result_path.is_file() or self.result_path.is_symlink():
                    self._fail_closed_draining_parent(
                        code="PARENT_DRAIN_RESULT_MISSING",
                        message="a draining parent cannot be reactivated and has no immutable result to finalize",
                        evidence={"parent_run_dir": self.parent["run_dir"], "result_path": str(self.result_path)},
                    )
                try:
                    result = read_mapping(self.result_path)
                except Exception as exc:
                    self._fail_closed_draining_parent(
                        code="PARENT_DRAIN_RESULT_INVALID",
                        message="the interrupted draining result cannot be read as an immutable mapping",
                        evidence={"result_path": str(self.result_path), "detail": str(exc)},
                    )
                if (
                    result.get("schema") != RESULT_SCHEMA
                    or result.get("workflow_id") != self.workflow_id
                    or result.get("advisory_only") is not True
                ):
                    self._fail_closed_draining_parent(
                        code="PARENT_DRAIN_RESULT_INVALID",
                        message="the interrupted draining result is not bound to this advisory workflow",
                        evidence={"result_path": str(self.result_path)},
                    )
                descriptor = {
                    "path": str(self.result_path),
                    "sha256": sha256_file(self.result_path),
                    "bytes": self.result_path.stat().st_size,
                }
                parent = self.store.finalize_parent(
                    self.parent["run_dir"],
                    "PARENT_COMPLETE",
                    result=descriptor,
                )
                if parent["phase"] != "PARENT_COMPLETE":
                    raise WebMultiError("PARENT_FINALIZATION_PENDING", parent["phase"], parent.get("failure"))
                self._checkpoint("PARENT_COMPLETE")
                return {
                    **result,
                    "parent_run_dir": self.parent["run_dir"],
                    "result_path": str(self.result_path),
                    "result_sha256": descriptor["sha256"],
                }
            if self.parent["phase"] == "PARENT_ACTIVE" and bool(self.parent.get("recovery_required")):
                self._recover_parent_children(self.parent)
                self.parent = self.store.clear_parent_runtime_recovery(resume_parent)
                self.parent["run_dir"] = str(Path(resume_parent).resolve())
        else:
            self.parent = self.store.create_parent_workflow(
                project_root=self.manifest["project_root"],
                manifest_path=self.manifest_path,
                workflow_id=self.workflow_id,
                agbrowse_contract=self.contract,
            )
        self._load_child_index(self.parent)
        self._checkpoint("PARENT_ACTIVE")
        try:
            if self._is_v2:
                result = self._run_v2_result(evidence_map)
                write_immutable_json(self.result_path, result)
                descriptor = {
                    "path": str(self.result_path),
                    "sha256": sha256_file(self.result_path),
                    "bytes": self.result_path.stat().st_size,
                }
                parent = self.store.finalize_parent(
                    self.parent["run_dir"],
                    "PARENT_COMPLETE",
                    result=descriptor,
                )
                if parent["phase"] != "PARENT_COMPLETE":
                    raise WebMultiError(
                        "PARENT_FINALIZATION_PENDING",
                        parent["phase"],
                        parent.get("failure"),
                    )
                self._checkpoint("PARENT_COMPLETE")
                return {
                    **result,
                    "parent_run_dir": self.parent["run_dir"],
                    "result_path": str(self.result_path),
                    "result_sha256": descriptor["sha256"],
                }
            planner = self._execute_stage(
                StageSpec("planner", "Planner", 0, 0, self.manifest["question"], tuple()), evidence_map,
            )
            approaches = list(planner.payload["approaches"])
            solver_specs = [
                StageSpec(f"solver-{lane}", "Solver", lane, 0, approaches[lane], tuple())
                for lane in range(int(self.manifest["solver_count"]))
            ]
            solvers = self._wave(solver_specs, evidence_map)
            refiner_specs = [
                StageSpec(
                    f"initial-refiner-{lane}", "InitialRefiner", lane, 0,
                    {"solver_stage_id": solvers[lane].spec.stage_id},
                    (solvers[lane].artifact_path,),
                )
                for lane in range(len(solvers))
            ]
            candidates = self._wave(refiner_specs, evidence_map)
            sufficient = False
            for iteration in range(1, int(self.manifest["max_iterations"]) + 1):
                ordered = sorted(candidates, key=lambda item: item.spec.stage_id)
                merger_specs: list[StageSpec] = []
                for lane in range(len(ordered)):
                    group = [ordered[(lane + offset) % len(ordered)] for offset in range(min(3, len(ordered)))]
                    merger_specs.append(
                        StageSpec(
                            f"iter-{iteration}-merger-{lane}", "Merger", lane, iteration,
                            {"source_stage_ids": [item.spec.stage_id for item in group]},
                            tuple(item.artifact_path for item in group),
                        )
                    )
                mergers = self._wave(merger_specs, evidence_map)
                loop_specs = [
                    StageSpec(
                        f"iter-{iteration}-refiner-{lane}", "LoopRefiner", lane, iteration,
                        {"merger_stage_id": merger.spec.stage_id}, (merger.artifact_path,),
                    )
                    for lane, merger in enumerate(mergers)
                ]
                refined = self._wave(loop_specs, evidence_map)
                judge = self._execute_stage(
                    StageSpec(
                        f"iter-{iteration}-judge", "Judge", 0, iteration,
                        {"candidate_stage_ids": [item.spec.stage_id for item in refined]},
                        tuple(item.artifact_path for item in refined),
                    ),
                    evidence_map,
                )
                if judge.payload["is_sufficient"]:
                    best_id = str(judge.payload.get("best_stage_id") or "")
                    selected = [item for item in refined if item.spec.stage_id == best_id]
                    candidates = selected or refined[:1]
                    sufficient = True
                    break
                outstanding = {str(item) for item in (judge.payload.get("outstanding_stage_ids") or [])}
                candidates = [item for item in refined if item.spec.stage_id in outstanding] or refined
                if len(candidates) == 1:
                    sufficient = True
                    break
            if not sufficient:
                final_merger = self._execute_stage(
                    StageSpec(
                        "final-merger", "FinalMerger", 0, int(self.manifest["max_iterations"]) + 1,
                        {"source_stage_ids": [item.spec.stage_id for item in candidates]},
                        tuple(item.artifact_path for item in candidates),
                    ),
                    evidence_map,
                )
                candidates = [
                    self._execute_stage(
                        StageSpec(
                            "final-refiner", "FinalRefiner", 0, int(self.manifest["max_iterations"]) + 1,
                            {"merger_stage_id": final_merger.spec.stage_id}, (final_merger.artifact_path,),
                        ),
                        evidence_map,
                    )
                ]
            organizer = self._execute_stage(
                StageSpec(
                    "organizer", "Organizer", 0, int(self.manifest["max_iterations"]) + 2,
                    {"candidate_stage_ids": [item.spec.stage_id for item in candidates]},
                    tuple(item.artifact_path for item in candidates),
                ),
                evidence_map,
            )
            max_concurrency = self._max_concurrency()
            result = {
                "schema": RESULT_SCHEMA,
                "workflow_id": self.workflow_id,
                "organizer_result": organizer.payload["final_answer"],
                "provenance": [self._accepted[key].provenance for key in sorted(self._accepted)],
                "evidence_map_sha256": evidence_map["evidence_map_sha256"],
                "max_concurrent_child_generations": max_concurrency,
                "advisory_only": True,
                "mode_variant": self.manifest["mode_variant"],
            }
            write_immutable_json(self.result_path, result)
            descriptor = {"path": str(self.result_path), "sha256": sha256_file(self.result_path), "bytes": self.result_path.stat().st_size}
            parent = self.store.finalize_parent(self.parent["run_dir"], "PARENT_COMPLETE", result=descriptor)
            if parent["phase"] != "PARENT_COMPLETE":
                raise WebMultiError("PARENT_FINALIZATION_PENDING", parent["phase"], parent.get("failure"))
            self._checkpoint("PARENT_COMPLETE")
            return {**result, "parent_run_dir": self.parent["run_dir"], "result_path": str(self.result_path), "result_sha256": descriptor["sha256"]}
        except BaseException as exc:
            try:
                root = STATE.canonical_project_root(self.parent["project_root"])
                paths = self.store.paths(root, str(self.parent["run_id"]))
                for child_state, child in self.store._parent_children(paths.runs_dir, str(self.parent["run_id"])):
                    if str(child.get("phase") or "") in {"CREATED", "PREFLIGHTED", "LEASED"} and not (child_state.parent / "send.claim").exists():
                        self.store.transition(str(child_state.parent), "PREFLIGHT_BLOCKED", block_code="WEB_MULTI_STAGE_FAILED_PRE_SEND")
                failure = {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
                if self._parent_requires_runtime_recovery(self.parent):
                    self.store.mark_parent_runtime_recovery(self.parent["run_dir"], failure=failure)
                else:
                    self.store.finalize_parent(self.parent["run_dir"], "PARENT_FAILED_CLOSED", failure=failure)
            except Exception:
                pass
            raise


def _provider_retry_eligibility(engine: WebMultiRuntime) -> dict[str, Any]:
    if not engine.parent or not engine.parent.get("run_dir"):
        raise WebMultiError("PROVIDER_RETRY_PARENT_MISSING", "failed runtime has no exact parent run")
    parent_run_dir = Path(str(engine.parent["run_dir"])).resolve(strict=True)
    _, parent = engine.store.load(parent_run_dir)
    if parent.get("phase") != "PARENT_FAILED_CLOSED":
        raise WebMultiError(
            "PROVIDER_RETRY_PARENT_NOT_TERMINAL",
            str(parent.get("phase") or ""),
        )
    failure = parent.get("failure") if isinstance(parent.get("failure"), dict) else {}
    if str(failure.get("code") or "") != "CHILD_PROVIDER_FAILED_TERMINAL":
        raise WebMultiError(
            "PROVIDER_RETRY_FAILURE_NOT_EXPLICIT",
            str(failure.get("code") or ""),
        )
    root = STATE.canonical_project_root(str(parent["project_root"]))
    paths = engine.store.paths(root, str(parent["run_id"]))
    if paths.lock_file.exists():
        raise WebMultiError(
            "PROVIDER_RETRY_PARENT_LOCK_REMAINS",
            str(paths.lock_file),
        )
    failed_children: list[dict[str, Any]] = []
    unsafe_children: list[dict[str, Any]] = []
    for _, child in engine.store._parent_children(paths.runs_dir, str(parent["run_id"])):
        phase = str(child.get("phase") or "")
        summary = {
            "run_id": child.get("run_id"),
            "stage_id": child.get("stage_id"),
            "phase": phase,
            "send_attempt_count": int(child.get("send_attempt_count") or 0),
            "conversation_url": child.get("conversation_url"),
            "owned_tab_state": child.get("owned_tab_state"),
            "cleanup_pending": bool(child.get("cleanup_pending")),
            "owned_open_tabs": int(child.get("owned_open_tabs") or 0),
        }
        if phase == "PROVIDER_FAILED_TERMINAL":
            if (
                summary["send_attempt_count"] != 1
                or summary["owned_tab_state"] not in {"closed-and-absent", "already-absent"}
                or summary["cleanup_pending"]
                or summary["owned_open_tabs"] != 0
            ):
                unsafe_children.append(summary)
            else:
                failed_children.append(summary)
        elif phase not in STATE.CHILD_SAFE_TERMINAL_PHASES:
            unsafe_children.append(summary)
        elif (
            phase == "COMPLETE"
            and (
                summary["owned_tab_state"] not in {"closed-and-absent", "already-absent"}
                or summary["cleanup_pending"]
                or summary["owned_open_tabs"] != 0
            )
        ):
            unsafe_children.append(summary)
    if not failed_children or unsafe_children:
        raise WebMultiError(
            "PROVIDER_RETRY_CHILD_EVIDENCE_UNSAFE",
            "automatic retry requires an explicit provider-failed child and no active, uncertain, or unclean child",
            {"failed_children": failed_children, "unsafe_children": unsafe_children},
        )
    return {
        "parent_run_id": parent["run_id"],
        "parent_run_dir": str(parent_run_dir),
        "parent_failure": failure,
        "failed_children": failed_children,
        "parent_lock_absent": True,
    }


def _provider_retry_manifest(
    base_manifest_path: Path,
    *,
    base_workflow_id: str,
    output_dir: Path,
    retry_index: int,
    prior: Mapping[str, Any],
) -> Path:
    raw = read_mapping(base_manifest_path)
    raw["workflow_id"] = f"{base_workflow_id}-provider-retry-{retry_index}"
    raw["retry_of_workflow_id"] = base_workflow_id
    raw["provider_failure_retry_index"] = retry_index
    raw["provider_failure_parent_run_id"] = str(prior["parent_run_id"])
    retry_path = output_dir / base_workflow_id / "provider-retries" / f"retry-{retry_index}.manifest.json"
    write_immutable_json(retry_path, raw)
    return retry_path


def run_with_provider_failure_retries(
    manifest_path: Path,
    *,
    resume_parent: Path | None = None,
) -> dict[str, Any]:
    base_manifest_path = manifest_path.expanduser().resolve(strict=True)
    base = validate_manifest(read_mapping(base_manifest_path), base_manifest_path)
    retry_limit = int(base["provider_failure_retry_limit"])
    base_workflow_id = str(base["workflow_id"])
    output_dir = Path(str(base["output_dir"]))
    current_manifest = base_manifest_path
    current_resume = resume_parent
    chain: list[dict[str, Any]] = []
    for retry_index in range(retry_limit + 1):
        engine = WebMultiRuntime(current_manifest)
        try:
            result = engine.run(resume_parent=current_resume)
        except WebMultiError as exc:
            if exc.code != "CHILD_PROVIDER_FAILED_TERMINAL" or retry_index >= retry_limit:
                raise
            prior = _provider_retry_eligibility(engine)
            next_index = retry_index + 1
            next_manifest = _provider_retry_manifest(
                base_manifest_path,
                base_workflow_id=base_workflow_id,
                output_dir=output_dir,
                retry_index=next_index,
                prior=prior,
            )
            chain.append(
                {
                    "retry_index": next_index,
                    "reason": exc.code,
                    "failed_workflow_id": engine.workflow_id,
                    "failed_parent_run_id": prior["parent_run_id"],
                    "failed_children": prior["failed_children"],
                    "next_manifest": str(next_manifest),
                    "next_manifest_sha256": sha256_file(next_manifest),
                }
            )
            current_manifest = next_manifest
            current_resume = None
            continue
        if not chain:
            return result
        report = {
            "schema": "codex.chatgpt.web-multi-provider-retry-chain/v1",
            "base_workflow_id": base_workflow_id,
            "retry_limit": retry_limit,
            "attempts": chain,
            "completed_workflow_id": engine.workflow_id,
            "completed_parent_run_dir": result.get("parent_run_dir"),
            "completed_result_path": result.get("result_path"),
            "completed_result_sha256": result.get("result_sha256"),
        }
        report_path = output_dir / base_workflow_id / "provider-retries" / "retry-chain.json"
        write_immutable_json(report_path, report)
        return {
            **result,
            "provider_retry_chain": chain,
            "provider_retry_report": str(report_path),
            "provider_retry_report_sha256": sha256_file(report_path),
        }
    raise WebMultiError("PROVIDER_RETRY_LOOP_EXHAUSTED", base_workflow_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="App-only parallel ChatGPT web Multi-GPT over one exact contract-validated agbrowse installation.")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume-parent", type=Path)
    parser.add_argument("--show-parent", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.show_parent:
            store = STATE.RunStore()
            _, record = store.load(args.show_parent)
            print(json.dumps({"ok": True, "result": record}, ensure_ascii=False, indent=2))
            return 0
        if not args.manifest:
            raise WebMultiError("MANIFEST_REQUIRED", "--manifest is required")
        runtime = WebMultiRuntime(args.manifest)
        result = (
            runtime.dry_run()
            if args.dry_run
            else run_with_provider_failure_retries(args.manifest, resume_parent=args.resume_parent)
        )
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        code = getattr(exc, "code", "WEB_MULTI_FAILED")
        evidence = getattr(exc, "evidence", {})
        print(json.dumps({"ok": False, "error": {"code": code, "message": str(exc), "evidence": evidence}}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
