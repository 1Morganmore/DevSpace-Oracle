from __future__ import annotations

"""Deterministic graph and recovery authority for parallel implementation v3.

The browser/provider adapter is deliberately outside this module.  This module
owns feature admission, graph binding, component scheduling, durable unit
state, exactly-once disposition, and the APPLY_READY barrier.
"""

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

FEATURE_ENV = "CODEX_CHATGPT_PARALLEL_IMPLEMENTATION_V1"
FEATURE_KEY = "parallel_implementation_v1"
PARENT_FAMILY = "parallel-implementation"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HASH_RE = re.compile(r"^[0-9a-f]{40,64}$")
TERMINAL_UNIT_STATES = {"INTEGRATED", "SKIPPED", "FAILED_TERMINAL", "RECOVERY_REQUIRED"}
CAPACITY_RECEIPT_SCHEMA = "codex.chatgpt.parallel-implementation-capacity/v1"


class ParallelRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = dict(evidence or {})


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def assert_feature_enabled(manifest: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> None:
    features = manifest.get("features") if isinstance(manifest.get("features"), Mapping) else {}
    env = os.environ if environ is None else environ
    manifest_enabled = features.get(FEATURE_KEY) is True
    environment_enabled = str(env.get(FEATURE_ENV) or "") == "1"
    if not manifest_enabled or not environment_enabled:
        raise ParallelRuntimeError(
            "PARALLEL_IMPLEMENTATION_FEATURE_DISABLED",
            "parallel implementation v3 requires both explicit feature gates",
            {"manifest_gate": manifest_enabled, "environment_gate": environment_enabled},
        )
    if str(manifest.get("schema") or "") != "codex.chatgpt.comprehensive-workflow/v3":
        raise ParallelRuntimeError("PARALLEL_IMPLEMENTATION_SCHEMA_REQUIRED", "parallel implementation requires explicit workflow v3")


def _identifier(label: str, value: Any) -> str:
    text = str(value or "")
    if not ID_RE.fullmatch(text):
        raise ParallelRuntimeError("IMPLEMENTATION_ID_INVALID", f"{label} is not a bounded identifier", {label: text})
    return text


def canonical_relpath(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip("/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ParallelRuntimeError("IMPLEMENTATION_PATH_INVALID", "file claim is not a canonical relative path", {"path": text})
    if path.parts[0].casefold() == ".git":
        raise ParallelRuntimeError("IMPLEMENTATION_GIT_METADATA_FORBIDDEN", "implementation units cannot claim Git metadata")
    return path.as_posix()


def claims_conflict(left: Iterable[str], right: Iterable[str]) -> bool:
    a = sorted({canonical_relpath(item) for item in left})
    b = sorted({canonical_relpath(item) for item in right})
    return any(x == y or x.startswith(y + "/") or y.startswith(x + "/") for x in a for y in b)


class DSU:
    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {item: item for item in items}
        self.rank = {item: 0 for item in items}

    def find(self, item: str) -> str:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1


def _topological_order(unit_ids: Sequence[str], dependencies: Mapping[str, Sequence[str]]) -> list[str]:
    indegree = {unit: 0 for unit in unit_ids}
    outgoing: dict[str, list[str]] = {unit: [] for unit in unit_ids}
    for unit in unit_ids:
        for dep in dependencies[unit]:
            indegree[unit] += 1
            outgoing[dep].append(unit)
    ready = sorted(unit for unit, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while ready:
        unit = ready.pop(0)
        order.append(unit)
        for target in sorted(outgoing[unit]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    if len(order) != len(unit_ids):
        raise ParallelRuntimeError("IMPLEMENTATION_GRAPH_CYCLE", "implementation graph contains a dependency cycle")
    return order


def bind_graph(graph: Mapping[str, Any], *, baseline_oid: str) -> dict[str, Any]:
    graph_keys = set(graph)
    if graph_keys != {"schema", "units"}:
        raise ParallelRuntimeError(
            "IMPLEMENTATION_GRAPH_KEYS_INVALID",
            "implementation graph uses a strict schema",
            {"extra": sorted(graph_keys - {"schema", "units"}), "missing": sorted({"schema", "units"} - graph_keys)},
        )
    if str(graph.get("schema") or "") != "codex.chatgpt.implementation-graph-result/v1":
        raise ParallelRuntimeError("IMPLEMENTATION_GRAPH_SCHEMA_INVALID", "implementation graph schema is not exact")
    raw_units = graph.get("units")
    if not isinstance(raw_units, list) or not raw_units or len(raw_units) > 64:
        raise ParallelRuntimeError("IMPLEMENTATION_UNIT_COUNT_INVALID", "implementation graph requires 1..64 units")
    if not HASH_RE.fullmatch(str(baseline_oid or "")):
        raise ParallelRuntimeError("IMPLEMENTATION_BASELINE_OID_INVALID", "baseline OID is invalid")
    units: dict[str, dict[str, Any]] = {}
    for raw in raw_units:
        if not isinstance(raw, Mapping):
            raise ParallelRuntimeError("IMPLEMENTATION_UNIT_INVALID", "implementation unit must be an object")
        allowed_unit_keys = {"unit_id", "required", "mission", "claimed_paths", "depends_on", "test_ids"}
        unit_keys = set(raw)
        if unit_keys != allowed_unit_keys:
            raise ParallelRuntimeError(
                "IMPLEMENTATION_UNIT_KEYS_INVALID",
                "implementation unit uses a strict schema",
                {"extra": sorted(unit_keys - allowed_unit_keys), "missing": sorted(allowed_unit_keys - unit_keys)},
            )
        unit_id = _identifier("unit_id", raw.get("unit_id"))
        if unit_id in units:
            raise ParallelRuntimeError("IMPLEMENTATION_UNIT_DUPLICATE", "unit_id must be unique", {"unit_id": unit_id})
        if not isinstance(raw.get("required"), bool):
            raise ParallelRuntimeError("IMPLEMENTATION_REQUIRED_INVALID", "unit required must be boolean")
        mission = str(raw.get("mission") or "")
        if not mission or len(mission) > 200000:
            raise ParallelRuntimeError("IMPLEMENTATION_MISSION_INVALID", "unit mission must be non-empty and bounded")
        claims_raw = raw.get("claimed_paths")
        if not isinstance(claims_raw, list) or not claims_raw or len(claims_raw) > 128 or len(claims_raw) != len(set(map(str, claims_raw))):
            raise ParallelRuntimeError("IMPLEMENTATION_CLAIMS_INVALID", "unit claimed_paths must contain 1..128 unique paths")
        claims = sorted(canonical_relpath(item) for item in claims_raw)
        deps_raw = raw.get("depends_on")
        if not isinstance(deps_raw, list) or len(deps_raw) > 64 or len(deps_raw) != len(set(map(str, deps_raw))):
            raise ParallelRuntimeError("IMPLEMENTATION_DEPENDENCIES_INVALID", "unit dependencies are invalid")
        dependencies = sorted(_identifier("depends_on", item) for item in deps_raw)
        tests = raw.get("test_ids")
        if not isinstance(tests, list) or len(tests) > 128 or len(tests) != len(set(map(str, tests))) or not all(ID_RE.fullmatch(str(item or "")) for item in tests):
            raise ParallelRuntimeError("IMPLEMENTATION_TEST_IDS_INVALID", "unit tests must reference unique bounded registry IDs")
        units[unit_id] = {
            "unit_id": unit_id,
            "required": raw["required"],
            "claimed_paths": claims,
            "depends_on": dependencies,
            "test_ids": sorted(str(item) for item in tests),
            "mission": mission,
        }
    for unit in units.values():
        unknown = [dep for dep in unit["depends_on"] if dep not in units]
        if unknown or unit["unit_id"] in unit["depends_on"]:
            raise ParallelRuntimeError("IMPLEMENTATION_DEPENDENCY_UNKNOWN", "unit dependency is unknown or self-referential", {"unit_id": unit["unit_id"], "unknown": unknown})
    unit_ids = sorted(units)
    dependencies = {unit_id: units[unit_id]["depends_on"] for unit_id in unit_ids}
    topo = _topological_order(unit_ids, dependencies)
    dsu = DSU(unit_ids)
    conflict_edges: list[dict[str, str]] = []
    dependency_edges: list[dict[str, str]] = []
    for unit_id in unit_ids:
        for dep in dependencies[unit_id]:
            dsu.union(unit_id, dep)
            dependency_edges.append({"from": dep, "to": unit_id})
    for index, left in enumerate(unit_ids):
        for right in unit_ids[index + 1:]:
            if claims_conflict(units[left]["claimed_paths"], units[right]["claimed_paths"]):
                dsu.union(left, right)
                conflict_edges.append({"left": left, "right": right})
    grouped: dict[str, list[str]] = defaultdict(list)
    for unit_id in unit_ids:
        grouped[dsu.find(unit_id)].append(unit_id)
    topo_rank = {unit_id: index for index, unit_id in enumerate(topo)}
    components: list[dict[str, Any]] = []
    unit_component: dict[str, str] = {}
    for members in sorted((sorted(value, key=lambda item: (topo_rank[item], item)) for value in grouped.values()), key=lambda value: value[0]):
        component_hash = hashlib.sha256("\0".join(sorted(members)).encode("utf-8")).hexdigest()[:16]
        component_id = "c-" + component_hash
        for unit_id in members:
            unit_component[unit_id] = component_id
        components.append({"component_id": component_id, "unit_order": members, "initial_head_oid": baseline_oid, "integration_head_oid": baseline_oid})
    # A dependency/conflict that survived across components is a binder defect.
    for edge in dependency_edges:
        if unit_component[edge["from"]] != unit_component[edge["to"]]:
            raise ParallelRuntimeError("IMPLEMENTATION_CROSS_COMPONENT_DEPENDENCY", "dependency was not unioned into one component")
    for edge in conflict_edges:
        if unit_component[edge["left"]] != unit_component[edge["right"]]:
            raise ParallelRuntimeError("IMPLEMENTATION_CROSS_COMPONENT_CONFLICT", "conflict was not unioned into one component")
    bound_units = []
    for unit_id in topo:
        bound_units.append({**units[unit_id], "component_id": unit_component[unit_id], "topological_index": topo_rank[unit_id]})
    bound = {
        "schema": "codex.chatgpt.bound-implementation-graph/v1",
        "baseline_oid": baseline_oid,
        "units": bound_units,
        "components": components,
        "dependency_edges": sorted(dependency_edges, key=lambda item: (item["from"], item["to"])),
        "conflict_edges": sorted(conflict_edges, key=lambda item: (item["left"], item["right"])),
    }
    bound["bound_graph_sha256"] = canonical_sha256(bound)
    return bound


def initial_runtime_state(bound_graph: Mapping[str, Any], *, parent_run_id: str, canonical_baseline_identity_sha256: str) -> dict[str, Any]:
    _identifier("parent_run_id", parent_run_id)
    if not re.fullmatch(r"[0-9a-f]{64}", canonical_baseline_identity_sha256):
        raise ParallelRuntimeError("CANONICAL_BASELINE_IDENTITY_INVALID", "canonical baseline identity hash is invalid")
    state = {
        "schema": "codex.chatgpt.parallel-implementation-runtime/v1",
        "parent_family": PARENT_FAMILY,
        "parent_run_id": parent_run_id,
        "bound_graph_sha256": str(bound_graph["bound_graph_sha256"]),
        "canonical_baseline_identity_sha256": canonical_baseline_identity_sha256,
        "phase": "GRAPH_BOUND",
        "common_authority_status": "HEALTHY",
        # Queue entries are assigned exactly once and selected by enqueue_seq.
        # Strict FIFO deliberately gives every entry a bounded overtaking count of 0.
        "scheduler": {"next_enqueue_seq": 1, "dispatch_round": 0, "capacity_receipt_sha256": None},
        "queue": {},
        "units": {},
        "components": {},
        "events": [],
    }
    for component in bound_graph["components"]:
        state["components"][component["component_id"]] = {
            "unit_order": list(component["unit_order"]),
            "integration_head_oid": component["integration_head_oid"],
            "active_unit_id": None,
            "blocked": False,
        }
    for unit in bound_graph["units"]:
        state["units"][unit["unit_id"]] = {
            "component_id": unit["component_id"],
            "required": unit["required"],
            "state": "PENDING",
            "input_base_oid": None,
            "attempt_id": None,
            "send_disposition": None,
            "integrated_commit_oid": None,
            "failure": None,
        }
    state["runtime_state_sha256"] = canonical_sha256({key: value for key, value in state.items() if key != "runtime_state_sha256"})
    return state


def _refresh_hash(state: MutableMapping[str, Any]) -> None:
    state["runtime_state_sha256"] = canonical_sha256({key: value for key, value in state.items() if key != "runtime_state_sha256"})


def _component_next(component: Mapping[str, Any], units: Mapping[str, Mapping[str, Any]]) -> str | None:
    for unit_id in component["unit_order"]:
        if units[unit_id]["state"] == "PENDING":
            return unit_id
        if units[unit_id]["state"] not in {"INTEGRATED", "SKIPPED"}:
            return None
    return None


def dispatchable_units(state: Mapping[str, Any]) -> list[dict[str, str]]:
    if state.get("common_authority_status") != "HEALTHY":
        return []
    result: list[dict[str, str]] = []
    components = state.get("components") if isinstance(state.get("components"), Mapping) else {}
    units = state.get("units") if isinstance(state.get("units"), Mapping) else {}
    for component_id in sorted(components):
        component = components[component_id]
        if component.get("blocked") or component.get("active_unit_id"):
            continue
        next_unit = _component_next(component, units)
        if next_unit:
            result.append({"component_id": component_id, "unit_id": next_unit, "input_base_oid": str(component["integration_head_oid"])})
    return result


def validate_capacity_receipt(
    receipt: Mapping[str, Any], *, parent_run_id: str, canonical_baseline_identity_sha256: str
) -> dict[str, Any]:
    """Validate an externally observed, objective child-session capacity receipt.

    Capacity is intentionally not a manifest knob: it is a per-resume observation
    of available independent web sessions.  This avoids a static five-session (or
    any other) assumption and keeps the receipt bound to this parent/workspace.
    """
    required = {"schema", "parent_run_id", "canonical_baseline_identity_sha256", "available_child_sessions", "observed_at", "source"}
    extra = set(receipt) - (required | {"capacity_receipt_sha256"})
    missing = required - set(receipt)
    if extra or missing or receipt.get("schema") != CAPACITY_RECEIPT_SCHEMA:
        raise ParallelRuntimeError("CAPACITY_RECEIPT_INVALID", "capacity receipt schema is invalid", {"extra": sorted(extra), "missing": sorted(missing)})
    if _identifier("parent_run_id", receipt.get("parent_run_id")) != parent_run_id:
        raise ParallelRuntimeError("CAPACITY_RECEIPT_PARENT_MISMATCH", "capacity receipt belongs to another parent")
    if str(receipt.get("canonical_baseline_identity_sha256") or "") != canonical_baseline_identity_sha256:
        raise ParallelRuntimeError("CAPACITY_RECEIPT_WORKSPACE_MISMATCH", "capacity receipt belongs to another canonical workspace")
    available = receipt.get("available_child_sessions")
    if not isinstance(available, int) or isinstance(available, bool) or available < 0 or available > 64:
        raise ParallelRuntimeError("CAPACITY_RECEIPT_INVALID", "available_child_sessions must be 0..64")
    if not isinstance(receipt.get("observed_at"), str) or not receipt["observed_at"] or not isinstance(receipt.get("source"), str) or not receipt["source"]:
        raise ParallelRuntimeError("CAPACITY_RECEIPT_INVALID", "capacity receipt observation is incomplete")
    unsigned = {key: receipt[key] for key in sorted(required)}
    expected = canonical_sha256(unsigned)
    supplied = receipt.get("capacity_receipt_sha256")
    if supplied is not None and supplied != expected:
        raise ParallelRuntimeError("CAPACITY_RECEIPT_HASH_INVALID", "capacity receipt hash does not match")
    return {**unsigned, "capacity_receipt_sha256": expected}


def serial_capacity_receipt(*, parent_run_id: str, canonical_baseline_identity_sha256: str) -> dict[str, Any]:
    """Safe no-observation fallback: dispatch one unit, never speculative parallelism."""
    receipt = {
        "schema": CAPACITY_RECEIPT_SCHEMA,
        "parent_run_id": _identifier("parent_run_id", parent_run_id),
        "canonical_baseline_identity_sha256": canonical_baseline_identity_sha256,
        "available_child_sessions": 1,
        "observed_at": "serial-fallback",
        "source": "safe-serial-fallback",
    }
    return {**receipt, "capacity_receipt_sha256": canonical_sha256(receipt)}


def capacity_dispatchable_units(state: MutableMapping[str, Any], receipt: Mapping[str, Any]) -> list[dict[str, str]]:
    """Drain only objectively available slots in deterministic FIFO queue order."""
    normalized = validate_capacity_receipt(
        receipt,
        parent_run_id=str(state.get("parent_run_id") or ""),
        canonical_baseline_identity_sha256=str(state.get("canonical_baseline_identity_sha256") or ""),
    )
    scheduler = state.setdefault("scheduler", {"next_enqueue_seq": 1, "dispatch_round": 0, "capacity_receipt_sha256": None})
    queue = state.setdefault("queue", {})
    ready = {item["component_id"]: item for item in dispatchable_units(state)}
    # A component can leave readiness only through an active/terminal transition.
    for component_id in list(queue):
        if component_id not in ready:
            queue.pop(component_id, None)
    for component_id in sorted(ready):
        if component_id not in queue:
            queue[component_id] = {"enqueue_seq": int(scheduler["next_enqueue_seq"]), "overtakes": 0}
            scheduler["next_enqueue_seq"] = int(scheduler["next_enqueue_seq"]) + 1
    scheduler["dispatch_round"] = int(scheduler.get("dispatch_round") or 0) + 1
    scheduler["capacity_receipt_sha256"] = normalized["capacity_receipt_sha256"]
    active = sum(1 for unit in state.get("units", {}).values() if unit.get("state") == "ACTIVE")
    slots = max(0, int(normalized["available_child_sessions"]) - active)
    selected = sorted((component_id for component_id in ready), key=lambda component_id: (int(queue[component_id]["enqueue_seq"]), component_id))[:slots]
    _refresh_hash(state)
    return [{**ready[component_id], "queue_enqueue_seq": str(queue[component_id]["enqueue_seq"]), "capacity_receipt_sha256": normalized["capacity_receipt_sha256"]} for component_id in selected]


def start_unit(state: MutableMapping[str, Any], *, component_id: str, unit_id: str, attempt_id: str) -> None:
    component = state["components"].get(component_id)
    unit = state["units"].get(unit_id)
    if not component or not unit or unit["component_id"] != component_id:
        raise ParallelRuntimeError("IMPLEMENTATION_UNIT_COMPONENT_MISMATCH", "unit does not belong to component")
    expected = _component_next(component, state["units"])
    if expected != unit_id or component.get("active_unit_id") is not None or unit["state"] != "PENDING":
        raise ParallelRuntimeError("IMPLEMENTATION_UNIT_NOT_DISPATCHABLE", "unit is not the deterministic next component unit", {"expected": expected, "unit_id": unit_id})
    _identifier("attempt_id", attempt_id)
    unit["state"] = "ACTIVE"
    unit["input_base_oid"] = str(component["integration_head_oid"])
    unit["attempt_id"] = attempt_id
    component["active_unit_id"] = unit_id
    state.get("queue", {}).pop(component_id, None)
    state["events"].append({"kind": "unit-started", "component_id": component_id, "unit_id": unit_id, "attempt_id": attempt_id, "input_base_oid": unit["input_base_oid"]})
    _refresh_hash(state)


def record_send_claim(state: MutableMapping[str, Any], *, unit_id: str, claim_sha256: str, invocation_state: str) -> None:
    if invocation_state not in {"CLAIMED_NOT_INVOKED", "INVOKED_MUTATION_UNKNOWN", "INVOKED_MUTATION_CONFIRMED", "ZERO_MUTATION_PROVEN"}:
        raise ParallelRuntimeError("SEND_DISPOSITION_INVALID", "send invocation state is invalid")
    unit = state["units"].get(unit_id)
    if not unit or unit["state"] not in {"ACTIVE", "RECOVERY_REQUIRED"}:
        raise ParallelRuntimeError("IMPLEMENTATION_UNIT_NOT_ACTIVE", "send claim requires an active unit")
    prior = unit.get("send_disposition")
    if prior and str(prior.get("claim_sha256") or "") != claim_sha256:
        raise ParallelRuntimeError("SEND_CLAIM_IMMUTABLE", "unit send claim cannot change")
    unit["send_disposition"] = {"claim_sha256": claim_sha256, "invocation_state": invocation_state}
    if invocation_state in {"INVOKED_MUTATION_UNKNOWN", "INVOKED_MUTATION_CONFIRMED"}:
        unit["state"] = "RECOVERY_REQUIRED"
        state["components"][unit["component_id"]]["blocked"] = True
    _refresh_hash(state)


def same_claim_retry_allowed(unit: Mapping[str, Any]) -> bool:
    disposition = unit.get("send_disposition") if isinstance(unit.get("send_disposition"), Mapping) else {}
    return unit.get("state") == "ACTIVE" and disposition.get("invocation_state") in {"CLAIMED_NOT_INVOKED", "ZERO_MUTATION_PROVEN"}


def complete_unit(state: MutableMapping[str, Any], *, unit_id: str, commit_oid: str) -> None:
    unit = state["units"].get(unit_id)
    if not unit or unit["state"] != "ACTIVE" or not HASH_RE.fullmatch(commit_oid):
        raise ParallelRuntimeError("IMPLEMENTATION_UNIT_COMPLETION_INVALID", "unit completion identity is invalid")
    component = state["components"][unit["component_id"]]
    if component.get("active_unit_id") != unit_id or unit.get("input_base_oid") != component.get("integration_head_oid"):
        raise ParallelRuntimeError("IMPLEMENTATION_INPUT_BASE_DRIFT", "unit was not built on the current component integration head")
    unit["state"] = "INTEGRATED"
    unit["integrated_commit_oid"] = commit_oid
    component["integration_head_oid"] = commit_oid
    component["active_unit_id"] = None
    state["events"].append({"kind": "unit-integrated", "component_id": unit["component_id"], "unit_id": unit_id, "commit_oid": commit_oid})
    _refresh_hash(state)


def skip_unit(state: MutableMapping[str, Any], *, unit_id: str, reason: str = "NO_CHANGE") -> None:
    unit = state["units"].get(unit_id)
    if not unit or unit["state"] != "ACTIVE" or unit.get("required") is True:
        raise ParallelRuntimeError("IMPLEMENTATION_UNIT_SKIP_INVALID", "only an active optional unit may be skipped")
    component = state["components"][unit["component_id"]]
    if component.get("active_unit_id") != unit_id or unit.get("input_base_oid") != component.get("integration_head_oid"):
        raise ParallelRuntimeError("IMPLEMENTATION_INPUT_BASE_DRIFT", "skipped unit was not bound to the current component head")
    unit["state"] = "SKIPPED"
    unit["failure"] = {"code": reason, "uncertain": False}
    component["active_unit_id"] = None
    state["events"].append({"kind": "unit-skipped", "component_id": unit["component_id"], "unit_id": unit_id, "reason": reason})
    _refresh_hash(state)


def fail_unit(state: MutableMapping[str, Any], *, unit_id: str, code: str, uncertain: bool, common_authority_damage: bool = False) -> None:
    unit = state["units"].get(unit_id)
    if not unit or unit["state"] not in {"ACTIVE", "RECOVERY_REQUIRED"}:
        raise ParallelRuntimeError("IMPLEMENTATION_UNIT_FAILURE_INVALID", "unit is not active or recovering")
    component = state["components"][unit["component_id"]]
    component["active_unit_id"] = None
    component["blocked"] = bool(uncertain or unit["required"])
    unit["state"] = "RECOVERY_REQUIRED" if uncertain else "FAILED_TERMINAL"
    unit["failure"] = {"code": code, "uncertain": uncertain}
    if common_authority_damage:
        state["common_authority_status"] = "RECOVERY_REQUIRED"
        state["phase"] = "PARENT_RECOVERY_REQUIRED"
    _refresh_hash(state)


def apply_ready(state: Mapping[str, Any]) -> bool:
    if state.get("common_authority_status") != "HEALTHY":
        return False
    units = state.get("units") if isinstance(state.get("units"), Mapping) else {}
    for unit in units.values():
        if unit.get("required") is True and unit.get("state") != "INTEGRATED":
            return False
        if unit.get("state") in {"ACTIVE", "RECOVERY_REQUIRED"}:
            return False
    return bool(units)


def mark_apply_ready(state: MutableMapping[str, Any]) -> None:
    if not apply_ready(state):
        unresolved = sorted(unit_id for unit_id, unit in state["units"].items() if unit.get("required") and unit.get("state") != "INTEGRATED")
        raise ParallelRuntimeError("APPLY_READY_BLOCKED", "required implementation units are unresolved", {"unresolved_required_units": unresolved})
    state["phase"] = "APPLY_READY"
    _refresh_hash(state)


def build_send_claim_v2(
    *,
    run_id: str,
    parent_run_id: str,
    unit_id: str,
    attempt_id: str,
    manifest_sha256: str,
    prompt_sha256: str,
    topology_receipt_sha256: str,
    listener_identity_receipt_sha256: str,
    tunnel_identity_receipt_sha256: str,
    server_identity_payload_sha256: str,
    app_scope_receipt_sha256: str,
    claimed_at: str,
) -> dict[str, Any]:
    values = {
        "schema": "codex.chatgpt.child-send-claim/v2",
        "run_id": _identifier("run_id", run_id),
        "parent_run_id": _identifier("parent_run_id", parent_run_id),
        "unit_id": _identifier("unit_id", unit_id),
        "attempt_id": _identifier("attempt_id", attempt_id),
        "manifest_sha256": manifest_sha256,
        "prompt_sha256": prompt_sha256,
        "topology_receipt_sha256": topology_receipt_sha256,
        "listener_identity_receipt_sha256": listener_identity_receipt_sha256,
        "tunnel_identity_receipt_sha256": tunnel_identity_receipt_sha256,
        "server_identity_payload_sha256": server_identity_payload_sha256,
        "app_scope_receipt_sha256": app_scope_receipt_sha256,
        "claimed_at": claimed_at,
        "provider_invocation_state": "CLAIMED_NOT_INVOKED",
    }
    for key, value in values.items():
        if key.endswith("sha256") and re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is None:
            raise ParallelRuntimeError("SEND_CLAIM_HASH_INVALID", "send claim hash binding is invalid", {"field": key})
    values["send_claim_sha256"] = canonical_sha256(values)
    return values


def _main() -> int:
    parser = argparse.ArgumentParser(description="Parallel implementation v3 graph authority")
    sub = parser.add_subparsers(dest="command", required=True)
    bind = sub.add_parser("bind-graph")
    bind.add_argument("--graph", required=True)
    bind.add_argument("--baseline-oid", required=True)
    bind.add_argument("--out")
    gate = sub.add_parser("feature-gate")
    gate.add_argument("--manifest", required=True)
    args = parser.parse_args()
    try:
        payload = json.loads(Path(args.graph if args.command == "bind-graph" else args.manifest).read_text(encoding="utf-8"))
        if args.command == "feature-gate":
            assert_feature_enabled(payload)
            result = {"enabled": True}
        else:
            result = bind_graph(payload, baseline_oid=args.baseline_oid)
            if args.out:
                write_json_atomic(Path(args.out), result)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True))
        return 0
    except (ParallelRuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        code = exc.code if isinstance(exc, ParallelRuntimeError) else "PARALLEL_IMPLEMENTATION_RUNTIME_ERROR"
        evidence = exc.evidence if isinstance(exc, ParallelRuntimeError) else {}
        print(json.dumps({"ok": False, "error": {"errorCode": code, "message": str(exc), "evidence": evidence}}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
