from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_parallel_implementation_runtime.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_parallel_implementation_runtime_test", MODULE_PATH)
assert SPEC and SPEC.loader
RUNTIME = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNTIME
SPEC.loader.exec_module(RUNTIME)

BASE = "1" * 40


def graph() -> dict:
    return {
        "schema": "codex.chatgpt.implementation-graph-result/v1",
        "units": [
            {
                "unit_id": "u-a",
                "required": True,
                "mission": "change a",
                "claimed_paths": ["src/a.py"],
                "depends_on": [],
                "test_ids": ["unit"],
            },
            {
                "unit_id": "u-a2",
                "required": True,
                "mission": "change child of a",
                "claimed_paths": ["src/a.py/helpers"],
                "depends_on": [],
                "test_ids": ["unit"],
            },
            {
                "unit_id": "u-b",
                "required": True,
                "mission": "change b",
                "claimed_paths": ["src/b.py"],
                "depends_on": [],
                "test_ids": ["unit"],
            },
        ],
    }


def test_double_feature_gate_is_required() -> None:
    manifest = {
        "schema": "codex.chatgpt.comprehensive-workflow/v3",
        "features": {"parallel_implementation_v1": True},
    }
    with pytest.raises(RUNTIME.ParallelRuntimeError) as exc:
        RUNTIME.assert_feature_enabled(manifest, {})
    assert exc.value.code == "PARALLEL_IMPLEMENTATION_FEATURE_DISABLED"
    RUNTIME.assert_feature_enabled(manifest, {RUNTIME.FEATURE_ENV: "1"})


def test_conflict_graph_unions_overlapping_claims_and_keeps_independent_components() -> None:
    bound = RUNTIME.bind_graph(graph(), baseline_oid=BASE)
    by_unit = {item["unit_id"]: item["component_id"] for item in bound["units"]}
    assert by_unit["u-a"] == by_unit["u-a2"]
    assert by_unit["u-b"] != by_unit["u-a"]
    assert len(bound["components"]) == 2
    assert bound["conflict_edges"] == [{"left": "u-a", "right": "u-a2"}]


def test_dependency_is_also_unioned_and_cycles_fail() -> None:
    value = graph()
    value["units"][2]["depends_on"] = ["u-a"]
    bound = RUNTIME.bind_graph(value, baseline_oid=BASE)
    assert len(bound["components"]) == 1
    value["units"][0]["depends_on"] = ["u-b"]
    with pytest.raises(RUNTIME.ParallelRuntimeError) as exc:
        RUNTIME.bind_graph(value, baseline_oid=BASE)
    assert exc.value.code == "IMPLEMENTATION_GRAPH_CYCLE"


def test_one_active_unit_per_component_and_component_head_becomes_next_input() -> None:
    bound = RUNTIME.bind_graph(graph(), baseline_oid=BASE)
    state = RUNTIME.initial_runtime_state(bound, parent_run_id="parent", canonical_baseline_identity_sha256="a" * 64)
    dispatch = RUNTIME.dispatchable_units(state)
    assert len(dispatch) == 2
    first = next(item for item in dispatch if item["unit_id"] == "u-a")
    RUNTIME.start_unit(state, component_id=first["component_id"], unit_id="u-a", attempt_id="try-1")
    with pytest.raises(RUNTIME.ParallelRuntimeError):
        RUNTIME.start_unit(state, component_id=first["component_id"], unit_id="u-a2", attempt_id="try-2")
    RUNTIME.complete_unit(state, unit_id="u-a", commit_oid="2" * 40)
    next_dispatch = next(item for item in RUNTIME.dispatchable_units(state) if item["unit_id"] == "u-a2")
    assert next_dispatch["input_base_oid"] == "2" * 40


def test_uncertain_unit_is_never_resubmitted_and_independent_component_can_continue() -> None:
    bound = RUNTIME.bind_graph(graph(), baseline_oid=BASE)
    state = RUNTIME.initial_runtime_state(bound, parent_run_id="parent", canonical_baseline_identity_sha256="b" * 64)
    dispatch = {item["unit_id"]: item for item in RUNTIME.dispatchable_units(state)}
    RUNTIME.start_unit(state, component_id=dispatch["u-a"]["component_id"], unit_id="u-a", attempt_id="try-a")
    RUNTIME.start_unit(state, component_id=dispatch["u-b"]["component_id"], unit_id="u-b", attempt_id="try-b")
    RUNTIME.record_send_claim(state, unit_id="u-a", claim_sha256="c" * 64, invocation_state="INVOKED_MUTATION_UNKNOWN")
    assert RUNTIME.same_claim_retry_allowed(state["units"]["u-a"]) is False
    RUNTIME.complete_unit(state, unit_id="u-b", commit_oid="3" * 40)
    assert state["units"]["u-b"]["state"] == "INTEGRATED"
    assert RUNTIME.apply_ready(state) is False
    with pytest.raises(RUNTIME.ParallelRuntimeError) as exc:
        RUNTIME.mark_apply_ready(state)
    assert exc.value.code == "APPLY_READY_BLOCKED"


def test_zero_mutation_proof_allows_only_same_claim_retry() -> None:
    bound = RUNTIME.bind_graph(graph(), baseline_oid=BASE)
    state = RUNTIME.initial_runtime_state(bound, parent_run_id="parent", canonical_baseline_identity_sha256="d" * 64)
    item = next(item for item in RUNTIME.dispatchable_units(state) if item["unit_id"] == "u-b")
    RUNTIME.start_unit(state, component_id=item["component_id"], unit_id="u-b", attempt_id="try-b")
    RUNTIME.record_send_claim(state, unit_id="u-b", claim_sha256="e" * 64, invocation_state="ZERO_MUTATION_PROVEN")
    assert RUNTIME.same_claim_retry_allowed(state["units"]["u-b"]) is True
    with pytest.raises(RUNTIME.ParallelRuntimeError) as exc:
        RUNTIME.record_send_claim(state, unit_id="u-b", claim_sha256="f" * 64, invocation_state="ZERO_MUTATION_PROVEN")
    assert exc.value.code == "SEND_CLAIM_IMMUTABLE"


def test_send_claim_v2_binds_every_authority_hash() -> None:
    claim = RUNTIME.build_send_claim_v2(
        run_id="run", parent_run_id="parent", unit_id="unit", attempt_id="attempt",
        manifest_sha256="1" * 64, prompt_sha256="2" * 64,
        topology_receipt_sha256="3" * 64, listener_identity_receipt_sha256="4" * 64,
        tunnel_identity_receipt_sha256="5" * 64, server_identity_payload_sha256="6" * 64,
        app_scope_receipt_sha256="7" * 64, claimed_at="2026-07-21T00:00:00Z",
    )
    assert claim["schema"] == "codex.chatgpt.child-send-claim/v2"
    assert claim["provider_invocation_state"] == "CLAIMED_NOT_INVOKED"
    assert len(claim["send_claim_sha256"]) == 64
